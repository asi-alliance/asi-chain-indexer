#!/usr/bin/env python3
"""Benchmark public Hasura queries and optional indexer HTTP endpoints.

The script sends no admin secret by default, so it measures the role a browser
uses. Run it after seeding and ANALYZE. Example:

    python scripts/benchmark_public_api.py \
        --hasura http://localhost:8080/v1/graphql \
        --indexer http://localhost:9090 \
        --iterations 30 --concurrency 5 \
        --search-pattern '%seed%'
"""

import argparse
import asyncio
import json
import statistics
import time

import aiohttp

QUERIES = {
    "blocks_exact_count": """
        query($height: bigint!) {
          blocks_aggregate(where: {block_number: {_eq: $height}}) {
            aggregate { count }
          }
        }
    """,
    "blocks_total_count": "{ blocks_aggregate { aggregate { count } } }",
    "block_heights_page": """
        { block_heights_view(limit: 20, order_by: {height: desc}) {
            height block_count latest_block_hash latest_timestamp finalized_count
          } }
    """,
    "deployments_total_count": "{ deployments_aggregate { aggregate { count } } }",
    "transfers_total_count": "{ transfers_aggregate { aggregate { count } } }",
    "blocks_max_height": "{ blocks_aggregate { aggregate { max { block_number } } } }",
    "deployments_phlo_sum": """
        { deployments_aggregate { aggregate { count sum { phlo_cost } } } }
    """,
    "transfers_amount_sum": """
        { transfers_aggregate { aggregate { count sum { amount_asi } } } }
    """,
    "deployment_action_count": """
        { deployments_aggregate(where: {deployment_action: {_eq: "pos_delegate"}}) {
            aggregate { count }
          } }
    """,
    "search_blocks": """
        query($pattern: String!) {
          blocks(where: {block_hash: {_ilike: $pattern}}, limit: 20,
                 order_by: [{timestamp: desc}, {block_hash: asc}]) {
            block_hash block_number timestamp proposer
          }
        }
    """,
    "search_deployments": """
        query($pattern: String!) {
          deployments(where: {deploy_id: {_ilike: $pattern}}, limit: 20,
                      order_by: [{timestamp: desc}, {deploy_id: asc}]) {
            deploy_id block_number timestamp deployer deployment_type deployment_action
          }
        }
    """,
    "search_transfers": """
        query($pattern: String!) {
          transfers(where: {_or: [{deploy_id: {_ilike: $pattern}},
                                  {from_address: {_ilike: $pattern}},
                                  {to_address: {_ilike: $pattern}}]},
                    limit: 20, order_by: [{timestamp: desc}, {id: asc}]) {
            id deploy_id block_number timestamp from_address to_address amount_dust
          }
        }
    """,
}


def percentile(values, fraction):
    if not values:
        return None
    ordered = sorted(values)
    index = min(len(ordered) - 1, max(0, round((len(ordered) - 1) * fraction)))
    return ordered[index]


async def graphql(session, url, query, variables=None):
    started = time.perf_counter()
    async with session.post(
        url, json={"query": query, "variables": variables or {}}
    ) as response:
        body = await response.text()
        elapsed = (time.perf_counter() - started) * 1000
        payload = json.loads(body) if body else {}
        errors = payload.get("errors")
        if response.status >= 400 or errors:
            raise RuntimeError(f"HTTP {response.status}: {errors or body[:300]}")
        return elapsed, len(body)


async def measure(graphql_url, name, query, variables, iterations, concurrency):
    semaphore = asyncio.Semaphore(concurrency)
    samples = []

    async with aiohttp.ClientSession(
        timeout=aiohttp.ClientTimeout(total=60)
    ) as session:

        async def one():
            async with semaphore:
                return await graphql(session, graphql_url, query, variables)

        # Warm the connection/plan cache before recording.
        for _ in range(3):
            await one()
        started = time.perf_counter()
        results = await asyncio.gather(*(one() for _ in range(iterations)))
        wall = (time.perf_counter() - started) * 1000
    samples = [latency for latency, _ in results]
    bytes_read = [size for _, size in results]
    return {
        "query": name,
        "count": len(samples),
        "p50_ms": round(statistics.median(samples), 2),
        "p95_ms": round(percentile(samples, 0.95), 2),
        "max_ms": round(max(samples), 2),
        "wall_ms": round(wall, 2),
        "response_bytes_avg": round(statistics.mean(bytes_read), 1),
    }


async def indexer_probe(session, base, path):
    started = time.perf_counter()
    async with session.get(base.rstrip("/") + path) as response:
        await response.read()
        return round((time.perf_counter() - started) * 1000, 2), response.status


async def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--hasura", default="http://localhost:8080/v1/graphql")
    parser.add_argument("--indexer", default=None)
    parser.add_argument("--iterations", type=int, default=20)
    parser.add_argument("--concurrency", type=int, default=5)
    parser.add_argument("--search-pattern", default="%seed%")
    parser.add_argument("--out", default=None)
    args = parser.parse_args()
    if args.iterations < 1 or args.concurrency < 1:
        parser.error("iterations and concurrency must be positive")

    async with aiohttp.ClientSession(
        timeout=aiohttp.ClientTimeout(total=60)
    ) as session:
        async with session.post(
            args.hasura,
            json={
                "query": "{ blocks(limit: 1, order_by: {block_number: desc}) { block_number } }"
            },
        ) as response:
            payload = await response.json()
    height = payload["data"]["blocks"][0]["block_number"]

    result = {"height": height, "queries": [], "indexer": None}
    for name, query in QUERIES.items():
        variables = {}
        if name == "blocks_exact_count":
            variables["height"] = height
        elif name.startswith("search_"):
            variables["pattern"] = args.search_pattern
        result["queries"].append(
            await measure(
                args.hasura,
                name,
                query,
                variables,
                args.iterations,
                args.concurrency,
            )
        )

    if args.indexer:
        async with aiohttp.ClientSession(
            timeout=aiohttp.ClientTimeout(total=30)
        ) as session:
            status_latency, status_code = await indexer_probe(
                session, args.indexer, "/status"
            )
            metrics_latency, metrics_code = await indexer_probe(
                session, args.indexer, "/metrics"
            )
        result["indexer"] = {
            "status_ms": status_latency,
            "status_http": status_code,
            "metrics_ms": metrics_latency,
            "metrics_http": metrics_code,
        }

    text = json.dumps(result, indent=2)
    print(text)
    if args.out:
        with open(args.out, "w", encoding="utf-8") as output:
            output.write(text + "\n")


if __name__ == "__main__":
    asyncio.run(main())
