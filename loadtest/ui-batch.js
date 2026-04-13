import http from 'k6/http';
import { check, sleep } from 'k6';

/*
ENV:
HASURA_URL
HASURA_ADMIN_SECRET

ITER_RATE   – iterations/sec (page loads per second)
PARALLEL    – requests per page load (batch size)
DURATION
VUS_MAX
TIMEOUT

W_BLOCKS / W_TXS / W_EXPORT   (sum ~100)

Logging:
LOG_ERRORS / LOG_LIMIT / LOG_BODY / LOG_SLOW_MS
*/

const HASURA_URL = __ENV.HASURA_URL;
const ADMIN_SECRET = __ENV.HASURA_ADMIN_SECRET;

const ITER_RATE = Number(__ENV.ITER_RATE || 20);
const PARALLEL  = Number(__ENV.PARALLEL || 10);
const DURATION  = __ENV.DURATION || '2m';
const VUS_MAX   = Number(__ENV.VUS_MAX || 300);
const TIMEOUT   = __ENV.TIMEOUT || '60s';

// Mix
const W_BLOCKS = Number(__ENV.W_BLOCKS || 50);
const W_TXS    = Number(__ENV.W_TXS || 50);
const W_EXPORT = Number(__ENV.W_EXPORT || 0);

// Logging
const LOG_ERRORS  = (__ENV.LOG_ERRORS || '1') === '1';
const LOG_LIMIT   = Number(__ENV.LOG_LIMIT || 50);
const LOG_BODY    = (__ENV.LOG_BODY || '0') === '1';
const LOG_SLOW_MS = Number(__ENV.LOG_SLOW_MS || 1500);
let logged = 0;

// If true, we don't store/parse response bodies (best for heavy export throughput tests)
const NO_BODY = (__ENV.NO_BODY || '1') === '1';

export const options = {
  scenarios: {
    ui_page_loads: {
      executor: 'constant-arrival-rate',
      rate: ITER_RATE,
      timeUnit: '1s',
      duration: DURATION,
      preAllocatedVUs: Math.min(50, VUS_MAX),
      maxVUs: VUS_MAX,
    },
  },
  thresholds: {
    http_req_failed: ['rate<0.01'],
    http_req_duration: ['p(95)<1500'],
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

function gqlBlocks() {
  return JSON.stringify({
    operationName: 'GetLatestBlocks',
    variables: { limit: 20, offset: 0 },
    query: `
query GetLatestBlocks($limit: Int!, $offset: Int!) {
  blocks(limit: $limit, offset: $offset, order_by: {block_number: desc}) {
    block_number
    block_hash
    timestamp
    proposer
    deployment_count
    shard_id
    deployments(limit: 10, order_by: {timestamp: desc}) {
      deploy_id
      deployer
      timestamp
      status
      errored
    }
  }
}`,
  });
}

function gqlTxs() {
  return JSON.stringify({
    operationName: 'GetPaginatedTransactions',
    variables: { deploymentLimit: 50, deploymentOffset: 0, transferLimit: 50, transferOffset: 0 },
    query: `
query GetPaginatedTransactions($deploymentLimit:Int!,$deploymentOffset:Int!,$transferLimit:Int!,$transferOffset:Int!) {
  deployments(limit:$deploymentLimit, offset:$deploymentOffset, order_by:{timestamp:desc}) {
    deploy_id
    deployer
    timestamp
    status
    errored
  }
  transfers(limit:$transferLimit, offset:$transferOffset, order_by:{created_at:desc}) {
    id
    deploy_id
    from_address
    to_address
    amount_asi
    status
    created_at
  }
}`,
  });
}

/**
 * EXACT frontend export
 */
function gqlExportFront() {
  return JSON.stringify({
    variables: {},
    query: `{
  get_transactions_by_blocks(args: {p_limit_blocks: 5000}) {
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

function safeJson(fn, fallback = null) { try { return fn(); } catch (_) { return fallback; } }

function logEvent(kind, op, r, reqBody, gqlErrors) {
  if (!LOG_ERRORS || logged >= LOG_LIMIT) return;
  logged++;

  const evt = {
    ts: new Date().toISOString(),
    kind, // http_error | graphql_error | slow
    op,
    status: r && r.status,
    duration_ms: r && r.timings ? r.timings.duration : undefined,
    waiting_ms: r && r.timings ? r.timings.waiting : undefined,
    receiving_ms: r && r.timings ? r.timings.receiving : undefined,
    error_code: r && r.error_code ? r.error_code : undefined,
    error: r && r.error ? r.error : undefined,
  };

  // With responseType:'none' there is no body to parse/log.
  if (!NO_BODY) {
    evt.graphql_errors = gqlErrors ?? safeJson(() => r.json('errors'), null);
    evt.bytes_received = r && r.body ? r.body.length : undefined;
  }

  if (LOG_BODY && !NO_BODY) {
    evt.req_body_snippet = reqBody ? String(reqBody).slice(0, 800) : null;
    evt.resp_body_snippet = r && r.body ? String(r.body).slice(0, 800) : null;
  }

  console.error(JSON.stringify(evt));
}

export default function () {
  const reqs = [];
  const meta = [];

  for (let i = 0; i < PARALLEL; i++) {
    const op = pickOp();
    const body = op === 'blocks' ? gqlBlocks() : op === 'txs' ? gqlTxs() : gqlExportFront();

    meta.push({ op, body });

    const reqParams = {
      headers,
      timeout: TIMEOUT,
      tags: { op },
    };

    if (NO_BODY) reqParams.responseType = 'none';

    reqs.push(['POST', HASURA_URL, body, reqParams]);
  }

  const resps = http.batch(reqs);

  for (let i = 0; i < resps.length; i++) {
    const r = resps[i];
    const { op, body } = meta[i];

    // IMPORTANT:
    // - If NO_BODY=1, DO NOT call r.json() at all.
    // - If NO_BODY=0, we can parse errors for all ops (including export).
    let gqlErrors = null;
    let hasGqlErrors = false;

    if (!NO_BODY) {
      gqlErrors = safeJson(() => r.json('errors'), null);
      hasGqlErrors = !!gqlErrors;
    }

    const ok = check(r, {
      'status 200': (x) => x.status === 200,
      'no graphql errors': () => (NO_BODY ? true : !hasGqlErrors),
    });

    if ((r && (r.error || r.error_code)) || r.status !== 200) {
      logEvent('http_error', op, r, body, gqlErrors);
    } else if (!NO_BODY && hasGqlErrors) {
      logEvent('graphql_error', op, r, body, gqlErrors);
    } else if (r.timings && r.timings.duration >= LOG_SLOW_MS) {
      logEvent('slow', op, r, body, gqlErrors);
    }

    // keep eslint quiet
    void ok;
  }

  sleep(0.1);
}
