import http from 'k6/http';
import { check, sleep } from 'k6';
import { Counter } from 'k6/metrics';

/*
ENV:
HASURA_URL
HASURA_ADMIN_SECRET

TARGET_RPS   – target *HTTP* requests per second total
DURATION     – test duration (e.g. 5m)
VUS_MAX      – max VUs
TIMEOUT      – request timeout (e.g. 30s)

Mix weights (sum ~100):
W_BLOCKS
W_TXS
W_EXPORT

Per-op params:
BLOCKS_LIMIT
BLOCKS_DEPLOYMENTS_LIMIT
TXS_DEPLOY_LIMIT
TXS_TRANSFER_LIMIT
EXPORT_LIMIT_BLOCKS

Validation:
CHECK_SAMPLE   – fraction of requests where we parse JSON and check GraphQL errors (0..1)

Logging:
LOG_ERRORS   – 1/0
LOG_LIMIT    – max error logs per test
LOG_BODY     – 1/0 include request/response snippets (only when body is available)
LOG_SLOW_MS  – log slow responses too (e.g. 2000)
*/

const HASURA_URL = __ENV.HASURA_URL;
const ADMIN_SECRET = __ENV.HASURA_ADMIN_SECRET;

const TARGET_RPS = Number(__ENV.TARGET_RPS || 50);
const DURATION   = __ENV.DURATION || '3m';
const VUS_MAX    = Number(__ENV.VUS_MAX || 300);
const TIMEOUT    = __ENV.TIMEOUT || '30s';

// Traffic mix weights
const W_BLOCKS = Number(__ENV.W_BLOCKS || 60);
const W_TXS    = Number(__ENV.W_TXS || 39);
const W_EXPORT = Number(__ENV.W_EXPORT || 1);

// Per-op tuning (make requests "not too light")
const BLOCKS_LIMIT             = Number(__ENV.BLOCKS_LIMIT || 200);
const BLOCKS_DEPLOYMENTS_LIMIT = Number(__ENV.BLOCKS_DEPLOYMENTS_LIMIT || 20);
const TXS_DEPLOY_LIMIT         = Number(__ENV.TXS_DEPLOY_LIMIT || 500);
const TXS_TRANSFER_LIMIT       = Number(__ENV.TXS_TRANSFER_LIMIT || 500);
const EXPORT_LIMIT_BLOCKS      = Number(__ENV.EXPORT_LIMIT_BLOCKS || 5000);

// Validation sampling
const CHECK_SAMPLE = Number(__ENV.CHECK_SAMPLE || 0.1); // 10% by default

// Logging
const LOG_ERRORS  = (__ENV.LOG_ERRORS || '1') === '1';
const LOG_LIMIT   = Number(__ENV.LOG_LIMIT || 100);
const LOG_BODY    = (__ENV.LOG_BODY || '0') === '1';
const LOG_SLOW_MS = Number(__ENV.LOG_SLOW_MS || 2000);
let logged = 0;

// Per-op counters (real RPS in summary)
const req_blocks = new Counter('req_blocks');
const req_txs    = new Counter('req_txs');
const req_export = new Counter('req_export');

export const options = {
    scenarios: {
        graphql_http_rps: {
            executor: 'constant-arrival-rate',
            rate: TARGET_RPS,
            timeUnit: '1s',
            duration: DURATION,
            preAllocatedVUs: Math.min(100, VUS_MAX),
            maxVUs: VUS_MAX,
        },
    },
    thresholds: {
        http_req_failed: ['rate<0.01'],
        http_req_duration: ['p(95)<1500'],

        // per-op latency expectations (tune later)
        'http_req_duration{op:blocks}': ['p(95)<1200'],
        'http_req_duration{op:txs}': ['p(95)<1500'],
        'http_req_duration{op:export}': ['p(95)<30000'],
    },
};

const headers = {
    'Content-Type': 'application/json',
    'x-hasura-admin-secret': ADMIN_SECRET,
};

function pickOp() {
    const r = Math.random() * 100;
    if (r < W_BLOCKS) return 'blocks';
    if (r < W_BLOCKS + W_TXS) return 'txs';
    return 'export';
}

/* ---------------------- Queries ---------------------- */

function gqlBlocks() {
    return JSON.stringify({
        operationName: 'GetLatestBlocks',
        variables: { limit: BLOCKS_LIMIT, offset: 0, depLimit: BLOCKS_DEPLOYMENTS_LIMIT },
        query: `
query GetLatestBlocks($limit: Int!, $offset: Int!, $depLimit: Int!) {
  blocks(limit: $limit, offset: $offset, order_by: {block_number: desc}) {
    block_number
    block_hash
    parent_hash
    timestamp
    proposer
    deployment_count
    shard_id
    created_at
    deployments(limit: $depLimit, order_by: {timestamp: desc}) {
      deploy_id
      deployer
      timestamp
      deployment_type
      status
      block_number
      errored
      error_message
    }
  }
}`,
    });
}

function gqlTxs() {
    return JSON.stringify({
        operationName: 'GetPaginatedTransactions',
        variables: {
            deploymentLimit: TXS_DEPLOY_LIMIT,
            deploymentOffset: 0,
            transferLimit: TXS_TRANSFER_LIMIT,
            transferOffset: 0,
        },
        query: `
query GetPaginatedTransactions(
  $deploymentLimit: Int!
  $deploymentOffset: Int!
  $transferLimit: Int!
  $transferOffset: Int!
) {
  deployments(
    limit: $deploymentLimit
    offset: $deploymentOffset
    order_by: {timestamp: desc}
  ) {
    deploy_id
    deployer
    timestamp
    deployment_type
    status
    block_number
    errored
    error_message
  }

  transfers(
    limit: $transferLimit
    offset: $transferOffset
    order_by: {created_at: desc}
  ) {
    id
    deploy_id
    from_address
    to_address
    amount_asi
    status
    block_number
    created_at
  }
}`,
    });
}

// Export: 5000 blocks (heavy)
function gqlExport() {
    return JSON.stringify({
        operationName: 'ExportTxByBlocks',
        variables: { limitBlocks: EXPORT_LIMIT_BLOCKS },
        query: `
query ExportTxByBlocks($limitBlocks: Int!) {
  get_transactions_by_blocks(args: {p_limit_blocks: $limitBlocks}) {
    block_number
    block_hash
    block_timestamp
    proposer
    deploy_id
    deployer
    deployment_status
    deployment_type
    errored
    from_address
    to_address
    amount_dust
    amount_asi
    shard_id
    phlo_cost
    phlo_limit
    phlo_price
    transfer_id
    transfer_status
    seq_num
    __typename
  }
}`,
    });
}

/* ---------------------- Logging ---------------------- */

function safeJson(fn, fallback = null) { try { return fn(); } catch (_) { return fallback; } }

function logEvent(kind, op, r, reqBody, gqlErrors) {
    if (!LOG_ERRORS || logged >= LOG_LIMIT) return;
    logged++;

    const evt = {
        ts: new Date().toISOString(),
        kind, // http_error | graphql_error | slow
        op,
        status: r?.status,
        duration_ms: r?.timings?.duration,
        waiting_ms: r?.timings?.waiting,
        receiving_ms: r?.timings?.receiving,
        error_code: r?.error_code,
        error: r?.error,
        bytes_received: r?.body ? r.body.length : undefined,
        graphql_errors: gqlErrors,
    };

    if (LOG_BODY) {
        evt.req_body_snippet = reqBody ? String(reqBody).slice(0, 800) : null;
        evt.resp_body_snippet = r?.body ? String(r.body).slice(0, 800) : null;
    }

    console.error(JSON.stringify(evt));
}

/* ---------------------- Test ---------------------- */

export default function () {
    const op = pickOp();

    let body;
    if (op === 'blocks') body = gqlBlocks();
    else if (op === 'txs') body = gqlTxs();
    else body = gqlExport();

    const params = {
        headers,
        timeout: TIMEOUT,
        tags: { op },
    };

    const r = http.post(HASURA_URL, body, params);

    // per-op counters
    if (op === 'blocks') req_blocks.add(1);
    else if (op === 'txs') req_txs.add(1);
    else req_export.add(1);

    // Sampled GraphQL error parsing (avoid parsing huge bodies every time)
    const doCheck = Math.random() < CHECK_SAMPLE;
    const gqlErrors = doCheck ? safeJson(() => r.json('errors'), null) : null;
    const hasGqlErrors = doCheck ? !!gqlErrors : false;

    check(r, {
        'status 200': (x) => x.status === 200,
        'no graphql errors': () => (doCheck ? !hasGqlErrors : true),
    });

    if ((r && (r.error || r.error_code)) || r.status !== 200) {
        logEvent('http_error', op, r, body, gqlErrors);
    } else if (doCheck && hasGqlErrors) {
        logEvent('graphql_error', op, r, body, gqlErrors);
    } else if (r.timings && r.timings.duration >= LOG_SLOW_MS) {
        // for slow logs we don't need to force JSON parsing
        logEvent('slow', op, r, body, null);
    }

    sleep(0.05 + Math.random() * 0.05);
}

/* ---------------------- Summary: print real RPS ---------------------- */

export function handleSummary(data) {
    const m = (name) =>
        (data.metrics && data.metrics[name] && data.metrics[name].values) ? data.metrics[name].values : null;

    const http   = m('http_reqs');
    const blocks = m('req_blocks');
    const txs    = m('req_txs');
    const exp    = m('req_export');

    const httpCount = http?.count ?? 0;
    const httpRate  = http?.rate  ?? 0;

    const lines = [];
    lines.push('');
    lines.push('=== ACTUAL LOAD ===');
    lines.push(`HTTP_RPS_AVG=${httpRate.toFixed(2)}  HTTP_COUNT=${httpCount}`);

    if (blocks) lines.push(`BLOCKS_RPS=${(blocks.rate ?? 0).toFixed(2)}  BLOCKS_COUNT=${blocks.count ?? 0}`);
    if (txs)    lines.push(`TXS_RPS=${(txs.rate ?? 0).toFixed(2)}  TXS_COUNT=${txs.count ?? 0}`);
    if (exp)    lines.push(`EXPORT_RPS=${(exp.rate ?? 0).toFixed(2)}  EXPORT_COUNT=${exp.count ?? 0}`);

    lines.push('===============');
    lines.push('');

    return { stdout: lines.join('\n') };
}
