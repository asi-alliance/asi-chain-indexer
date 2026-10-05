import http from 'k6/http';
import { check, sleep } from 'k6';
import { Counter } from 'k6/metrics';

/*
ENV:
HASURA_URL
HASURA_ADMIN_SECRET

ITER_RATE   – iterations/sec (page loads per second)
PARALLEL    – requests per page load (batch size)
DURATION
VUS_MAX
TIMEOUT

CHECK_SAMPLE – fraction [0..1] where we parse JSON and check GraphQL errors

REPORT_JSON – 1/0 write report.json
REPORT_TXT  – 1/0 write report.txt

Logging:
LOG_ERRORS / LOG_LIMIT / LOG_BODY / LOG_SLOW_MS
*/

const HASURA_URL = __ENV.HASURA_URL;
const ADMIN_SECRET = __ENV.HASURA_ADMIN_SECRET;

const ITER_RATE = Number(__ENV.ITER_RATE || 20);
const PARALLEL  = Number(__ENV.PARALLEL || 10);
const DURATION  = __ENV.DURATION || '2m';
const VUS_MAX   = Number(__ENV.VUS_MAX || 400);
const TIMEOUT   = __ENV.TIMEOUT || '90s';

const CHECK_SAMPLE = Number(__ENV.CHECK_SAMPLE || 0.1);

const REPORT_JSON = (__ENV.REPORT_JSON || '1') === '1';
const REPORT_TXT  = (__ENV.REPORT_TXT  || '1') === '1';

const LOG_ERRORS  = (__ENV.LOG_ERRORS || '1') === '1';
const LOG_LIMIT   = Number(__ENV.LOG_LIMIT || 50);
const LOG_BODY    = (__ENV.LOG_BODY || '0') === '1';
const LOG_SLOW_MS = Number(__ENV.LOG_SLOW_MS || 2000);
let logged = 0;

const headers = {
    'Content-Type': 'application/json',
    'x-hasura-admin-secret': ADMIN_SECRET,
};

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

/* ---------------------- Counters (per-op RPS/COUNT) ---------------------- */
const req_total = new Counter('req_total');

const req_recent_blocks      = new Counter('req_recent_blocks');
const req_latest_blocks      = new Counter('req_latest_blocks');
const req_latest_deployments = new Counter('req_latest_deployments');
const req_paginated_txs      = new Counter('req_paginated_txs');
const req_tx_counts          = new Counter('req_tx_counts');
const req_tx_details         = new Counter('req_tx_details');
const req_stats              = new Counter('req_stats');
const req_indexer_status     = new Counter('req_indexer_status');
const req_export_5k          = new Counter('req_export_5k');

function bump(tag) {
    req_total.add(1);
    if (tag === 'recent_blocks') req_recent_blocks.add(1);
    else if (tag === 'latest_blocks') req_latest_blocks.add(1);
    else if (tag === 'latest_deployments') req_latest_deployments.add(1);
    else if (tag === 'paginated_txs') req_paginated_txs.add(1);
    else if (tag === 'tx_counts') req_tx_counts.add(1);
    else if (tag === 'tx_details') req_tx_details.add(1);
    else if (tag === 'stats') req_stats.add(1);
    else if (tag === 'indexer_status') req_indexer_status.add(1);
    else if (tag === 'export_5k') req_export_5k.add(1);
}

/* ---------------------- EXACT frontend payloads ---------------------- */

function op_GetRecentBlocks() {
    return JSON.stringify({
        operationName: "GetRecentBlocks",
        variables: {},
        query: "query GetRecentBlocks {\n  blocks(limit: 20, order_by: {block_number: desc}) {\n    block_number\n    timestamp\n    __typename\n  }\n}"
    });
}

function op_GetLatestBlocks() {
    return JSON.stringify({
        operationName: "GetLatestBlocks",
        variables: { limit: 20, offset: 0 },
        query:
            "fragment BlockFragment on blocks {\n  block_number\n  block_hash\n  parent_hash\n  timestamp\n  proposer\n  deployment_count\n  state_hash\n  pre_state_hash\n  state_root_hash\n  bonds_map\n  fault_tolerance\n  finalization_status\n  justifications\n  seq_num\n  shard_id\n  sig\n  sig_algorithm\n  version\n  extra_bytes\n  created_at\n  __typename\n}\n\nfragment DeploymentFragment on deployments {\n  deploy_id\n  deployer\n  term\n  timestamp\n  deployment_type\n  phlo_cost\n  phlo_price\n  phlo_limit\n  valid_after_block_number\n  status\n  block_number\n  block_hash\n  seq_num\n  shard_id\n  sig\n  sig_algorithm\n  errored\n  error_message\n  created_at\n  __typename\n}\n\nquery GetLatestBlocks($limit: Int = 10, $offset: Int = 0) {\n  blocks(limit: $limit, offset: $offset, order_by: {block_number: desc}) {\n    ...BlockFragment\n    deployments {\n      ...DeploymentFragment\n      __typename\n    }\n    __typename\n  }\n}"
    });
}

function op_GetLatestDeployments() {
    return JSON.stringify({
        operationName: "GetLatestDeployments",
        variables: { limit: 5 },
        query:
            "fragment DeploymentFragment on deployments {\n  deploy_id\n  deployer\n  term\n  timestamp\n  deployment_type\n  phlo_cost\n  phlo_price\n  phlo_limit\n  valid_after_block_number\n  status\n  block_number\n  block_hash\n  seq_num\n  shard_id\n  sig\n  sig_algorithm\n  errored\n  error_message\n  created_at\n  __typename\n}\n\nquery GetLatestDeployments($limit: Int = 5) {\n  deployments(limit: $limit, order_by: {timestamp: desc}) {\n    ...DeploymentFragment\n    __typename\n  }\n}"
    });
}

function op_GetPaginatedTransactions() {
    return JSON.stringify({
        operationName: "GetPaginatedTransactions",
        variables: { deploymentLimit: 20, deploymentOffset: 0, transferLimit: 0, transferOffset: 0 },
        query:
            "query GetPaginatedTransactions($deploymentLimit: Int!, $deploymentOffset: Int!, $transferLimit: Int!, $transferOffset: Int!) {\n  deployments(\n    limit: $deploymentLimit\n    offset: $deploymentOffset\n    order_by: {timestamp: desc}\n  ) {\n    deploy_id\n    deployer\n    term\n    timestamp\n    deployment_type\n    phlo_cost\n    phlo_price\n    phlo_limit\n    status\n    block_number\n    errored\n    error_message\n    __typename\n  }\n  transfers(\n    limit: $transferLimit\n    offset: $transferOffset\n    order_by: {created_at: desc}\n  ) {\n    id\n    deploy_id\n    from_address\n    to_address\n    amount_asi\n    status\n    block_number\n    created_at\n    __typename\n  }\n}"
    });
}

function op_GetTransactionCounts() {
    return JSON.stringify({
        operationName: "GetTransactionCounts",
        variables: {},
        query:
            "query GetTransactionCounts {\n  deployments_aggregate {\n    aggregate {\n      count\n      __typename\n    }\n    __typename\n  }\n  transfers_aggregate {\n    aggregate {\n      count\n      __typename\n    }\n    __typename\n  }\n}"
    });
}

const TX_DETAILS_DEPLOY_ID = __ENV.TX_DETAILS_DEPLOY_ID || "genesis_bond_3";
function op_GetTransactionDetails() {
    return JSON.stringify({
        operationName: "GetTransactionDetails",
        variables: { deployId: TX_DETAILS_DEPLOY_ID },
        query:
            "query GetTransactionDetails($deployId: String!) {\n  deployments(where: {deploy_id: {_eq: $deployId}}) {\n    deploy_id\n    deployer\n    term\n    timestamp\n    deployment_type\n    phlo_cost\n    phlo_price\n    phlo_limit\n    valid_after_block_number\n    status\n    block_number\n    block_hash\n    seq_num\n    shard_id\n    sig\n    sig_algorithm\n    errored\n    error_message\n    created_at\n    transfers {\n      id\n      from_address\n      to_address\n      amount_asi\n      amount_dust\n      status\n      created_at\n      __typename\n    }\n    block {\n      block_number\n      block_hash\n      parent_hash\n      timestamp\n      proposer\n      deployment_count\n      state_hash\n      finalization_status\n      justifications\n      created_at\n      __typename\n    }\n    __typename\n  }\n}"
    });
}

const STATS_HOURS = Number(__ENV.STATS_HOURS || 24);
const STATS_DIVISIONS = Number(__ENV.STATS_DIVISIONS || 8);
function op_GetStats() {
    return JSON.stringify({
        operationName: "GetStats",
        variables: { hours: STATS_HOURS, divisions: STATS_DIVISIONS },
        query:
            "query GetStats($hours: Int!, $divisions: Int!) {\n  get_network_metrics(args: {p_range_hours: $hours, p_divisions: $divisions}) {\n    bucket_start\n    avg_block_time_seconds\n    avg_tps\n    deployments_count\n    transfers_count\n    __typename\n  }\n  network_stats(limit: 1, order_by: {id: desc}) {\n    id\n    total_validators\n    active_validators\n    validators_in_quarantine\n    consensus_participation\n    consensus_status\n    block_number\n    timestamp\n    __typename\n  }\n  blocks(limit: 1, order_by: {block_number: desc}) {\n    block_number\n    __typename\n  }\n}"
    });
}

function op_GetIndexerStatus() {
    return JSON.stringify({
        operationName: "GetIndexerStatus",
        variables: {},
        query:
            "query GetIndexerStatus {\n  blocks(order_by: {block_number: desc}, limit: 1) {\n    block_number\n    timestamp\n    __typename\n  }\n}"
    });
}

// EXACT export you provided, limit 5000 blocks
function op_Export5k() {
    return JSON.stringify({
        variables: {},
        query: "{\n  get_transactions_by_blocks(args: {p_limit_blocks: 5000}) {\n    block_number\n    block_hash\n    block_timestamp\n    proposer\n    deploy_id\n    deployer\n    deployment_status\n    deployment_type\n    errored\n    from_address\n    to_address\n    amount_dust\n    amount_asi\n    shard_id\n    phlo_cost\n    phlo_limit\n    phlo_price\n    transfer_id\n    transfer_status\n    seq_num\n    __typename\n  }\n}"
    });
}

/* ---------------------- Ops list ---------------------- */

const OPS = [
    { tag: 'recent_blocks',      body: op_GetRecentBlocks },
    { tag: 'latest_blocks',      body: op_GetLatestBlocks },
    { tag: 'latest_deployments', body: op_GetLatestDeployments },
    { tag: 'paginated_txs',      body: op_GetPaginatedTransactions },
    { tag: 'tx_counts',          body: op_GetTransactionCounts },
    { tag: 'tx_details',         body: op_GetTransactionDetails },
    { tag: 'stats',              body: op_GetStats },
    { tag: 'indexer_status',     body: op_GetIndexerStatus },
    { tag: 'export_5k',          body: op_Export5k },
];

function pickOp() {
    return OPS[Math.floor(Math.random() * OPS.length)];
}

/* ---------------------- IMPORTANT: thresholds to force per-op metrics in summary ---------------------- */

export const optionsWithPerOp = {
    scenarios: options.scenarios,
    thresholds: {
        ...options.thresholds,

        // Big threshold values just to make k6 print tagged sub-metrics in the output.
        'http_req_duration{op:recent_blocks}': ['p(95)<60000'],
        'http_req_duration{op:latest_blocks}': ['p(95)<60000'],
        'http_req_duration{op:latest_deployments}': ['p(95)<60000'],
        'http_req_duration{op:paginated_txs}': ['p(95)<60000'],
        'http_req_duration{op:tx_counts}': ['p(95)<60000'],
        'http_req_duration{op:tx_details}': ['p(95)<60000'],
        'http_req_duration{op:stats}': ['p(95)<60000'],
        'http_req_duration{op:indexer_status}': ['p(95)<60000'],
        'http_req_duration{op:export_5k}': ['p(95)<60000'],

        'http_req_failed{op:recent_blocks}': ['rate<1'],
        'http_req_failed{op:latest_blocks}': ['rate<1'],
        'http_req_failed{op:latest_deployments}': ['rate<1'],
        'http_req_failed{op:paginated_txs}': ['rate<1'],
        'http_req_failed{op:tx_counts}': ['rate<1'],
        'http_req_failed{op:tx_details}': ['rate<1'],
        'http_req_failed{op:stats}': ['rate<1'],
        'http_req_failed{op:indexer_status}': ['rate<1'],
        'http_req_failed{op:export_5k}': ['rate<1'],
    },
};

// k6 reads `export const options`
export const options = optionsWithPerOp;

/* ---------------------- Test ---------------------- */

export default function () {
    const reqs = [];
    const meta = [];

    for (let i = 0; i < PARALLEL; i++) {
        const op = pickOp();
        const body = op.body();
        meta.push({ tag: op.tag, body });

        reqs.push([
            'POST',
            HASURA_URL,
            body,
            { headers, timeout: TIMEOUT, tags: { op: op.tag } },
        ]);
    }

    const resps = http.batch(reqs);

    for (let i = 0; i < resps.length; i++) {
        const r = resps[i];
        const { tag, body } = meta[i];

        bump(tag);

        const doCheck = Math.random() < CHECK_SAMPLE;
        const gqlErrors = doCheck ? safeJson(() => r.json('errors'), null) : null;
        const hasGqlErrors = doCheck ? !!gqlErrors : false;

        check(r, {
            'status 200': (x) => x.status === 200,
            'no graphql errors': () => (doCheck ? !hasGqlErrors : true),
        });

        if ((r && (r.error || r.error_code)) || r.status !== 200) {
            logEvent('http_error', tag, r, body, gqlErrors);
        } else if (doCheck && hasGqlErrors) {
            logEvent('graphql_error', tag, r, body, gqlErrors);
        } else if (r.timings && r.timings.duration >= LOG_SLOW_MS) {
            logEvent('slow', tag, r, body, null);
        }
    }

    sleep(0.05 + Math.random() * 0.05);
}

/* ---------------------- Summary (detailed) ---------------------- */

function fmtBytes(n) {
    if (!Number.isFinite(n)) return '0 B';
    const units = ['B', 'KB', 'MB', 'GB', 'TB'];
    let v = n, i = 0;
    while (v >= 1024 && i < units.length - 1) { v /= 1024; i++; }
    return `${v.toFixed(v >= 10 ? 1 : 2)} ${units[i]}`;
}

function fmtDurMs(ms) {
    if (!Number.isFinite(ms)) return 'n/a';
    if (ms < 1000) return `${ms.toFixed(2)}ms`;
    return `${(ms / 1000).toFixed(2)}s`;
}

function get(data, key) {
    const v = data.metrics?.[key]?.values;
    return v || null;
}

function findTaggedMetric(data, base, tag) {
    const k = `${base}{op:${tag}}`;
    return get(data, k);
}

export function handleSummary(data) {
    const durMs = data.state?.testRunDurationMs ?? 0;

    const httpReqs = get(data, 'http_reqs');
    const iters    = get(data, 'iterations');
    const dropped  = get(data, 'dropped_iterations');
    const vus      = get(data, 'vus');
    const vusMax   = get(data, 'vus_max');

    const rx = get(data, 'data_received');
    const tx = get(data, 'data_sent');

    const dur = get(data, 'http_req_duration');
    const wait = get(data, 'http_req_waiting');

    const lines = [];
    lines.push('');
    lines.push('=== REPORT (DETAILED) ===');
    lines.push(`DURATION=${(durMs / 1000).toFixed(2)}s`);

    if (httpReqs) lines.push(`HTTP_RPS_AVG=${(httpReqs.rate ?? 0).toFixed(2)}  HTTP_COUNT=${httpReqs.count ?? 0}`);
    if (iters)    lines.push(`ITER_RPS_AVG=${(iters.rate ?? 0).toFixed(2)}  ITER_COUNT=${iters.count ?? 0}`);
    if (dropped)  lines.push(`DROPPED_ITERATIONS=${dropped.count ?? 0}  DROPPED_RPS=${(dropped.rate ?? 0).toFixed(2)}`);

    if (vus)      lines.push(`VUS_MIN=${vus.min ?? 0}  VUS_MAX=${vus.max ?? 0}`);
    if (vusMax)   lines.push(`VUS_MAX_SETTING=${vusMax.value ?? 0}`);

    if (rx) lines.push(`DATA_RECEIVED_TOTAL=${fmtBytes(rx.count ?? 0)}  RX_RATE=${fmtBytes(rx.rate ?? 0)}/s`);
    if (tx) lines.push(`DATA_SENT_TOTAL=${fmtBytes(tx.count ?? 0)}  TX_RATE=${fmtBytes(tx.rate ?? 0)}/s`);

    if (dur) {
        lines.push(`HTTP_DURATION_AVG=${fmtDurMs(dur.avg)}  P90=${fmtDurMs(dur['p(90)'])}  P95=${fmtDurMs(dur['p(95)'])}  MAX=${fmtDurMs(dur.max)}`);
    }
    if (wait) {
        lines.push(`HTTP_WAITING_AVG=${fmtDurMs(wait.avg)}  P90=${fmtDurMs(wait['p(90)'])}  P95=${fmtDurMs(wait['p(95)'])}  MAX=${fmtDurMs(wait.max)}`);
    }

    lines.push('');
    lines.push('--- PER OP ---');
    for (const { tag } of OPS) {
        const c = get(data, `req_${tag}`);
        const t = findTaggedMetric(data, 'http_req_duration', tag);
        const f = findTaggedMetric(data, 'http_req_failed', tag);

        const rps = c?.rate ?? 0;
        const cnt = c?.count ?? 0;

        const p95 = t ? fmtDurMs(t['p(95)']) : 'n/a';
        const avg = t ? fmtDurMs(t.avg) : 'n/a';
        const fail = f ? `${((f.value ?? 0) * 100).toFixed(2)}%` : 'n/a';

        lines.push(`${tag}: RPS=${rps.toFixed(2)}  COUNT=${cnt}  DUR_AVG=${avg}  DUR_P95=${p95}  FAIL=${fail}`);
    }

    lines.push('========================');
    lines.push('');

    const out = { stdout: lines.join('\n') };
    if (REPORT_JSON) out['report.json'] = JSON.stringify(data, null, 2);
    if (REPORT_TXT)  out['report.txt']  = lines.join('\n');
    return out;
}
