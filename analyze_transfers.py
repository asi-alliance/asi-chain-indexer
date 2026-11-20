#!/usr/bin/env python3
"""Report successful ledger transfers using exact raw units and recorded precision."""

import json
import os
from collections import defaultdict
from fractions import Fraction
from urllib.request import Request, urlopen

from src.amounts import amount, token_decimal

QUERY = """
query($after: bigint!, $limit: Int!) {
  transfers(
    where: {
      id: {_gt: $after}
      status: {_in: ["success", "genesis_mint", "genesis_bond"]}
    }
    order_by: {id: asc}, limit: $limit
  ) {
    id deploy_id block_number from_address to_address
    amount_dust token_decimals
  }
}
"""


def fetch_transfers(url, secret=None):
    """Read every page; reject HTTP/GraphQL errors instead of reporting partial data."""
    headers = {"Content-Type": "application/json"}
    if secret:
        headers["x-hasura-admin-secret"] = secret
    transfers = []
    after = 0
    while True:
        request = Request(
            url,
            data=json.dumps(
                {"query": QUERY, "variables": {"after": after, "limit": 1000}}
            ).encode(),
            headers=headers,
        )
        with urlopen(request, timeout=30) as response:
            result = json.load(response)
        if result.get("errors"):
            raise ValueError("GraphQL returned errors; transfer report is incomplete")
        page = result["data"]["transfers"]
        if not page:
            return transfers
        for row in page:
            row_id = int(row["id"])
            if row_id <= after:
                raise ValueError("GraphQL transfer pagination did not advance")
            after = row_id
            transfers.append(row)


def format_units(raw, decimals):
    """Format a signed aggregate without Decimal context rounding or floats."""
    sign = "-" if raw < 0 else ""
    digits = str(abs(raw)).zfill(decimals + 1)
    return sign + (
        digits if decimals == 0 else digits[:-decimals] + "." + digits[-decimals:]
    )


def transfer_report(transfers):
    # Each row carries its historical precision. Sum at a common scale without
    # reinterpreting native8 history as native18 or bounding aggregate totals.
    validated = []
    for row in transfers:
        raw = amount(row["amount_dust"])
        decimals = row["token_decimals"]
        token_decimal(raw, decimals)  # Validate per-row precision before scaling.
        validated.append((row, raw, decimals))
    scale = max((decimals for _, _, decimals in validated), default=0)
    entries = [
        (row, raw * 10 ** (scale - decimals)) for row, raw, decimals in validated
    ]
    genesis = [(row, raw) for row, raw in entries if int(row["block_number"]) == 0]
    users = [(row, raw) for row, raw in entries if int(row["block_number"]) != 0]
    user_total = sum(raw for _, raw in users)
    lines = [
        "ASI-CHAIN SUCCESSFUL LEDGER TRANSFER REPORT",
        f"Total transfers: {len(entries)}",
        f"Total ASI moved: {format_units(sum(raw for _, raw in entries), scale)} ASI",
        f"Genesis transfers: {len(genesis)}",
        f"Genesis total: {format_units(sum(raw for _, raw in genesis), scale)} ASI",
        f"User transfers: {len(users)}",
        f"User total: {format_units(user_total, scale)} ASI",
        "Details (recorded precision):",
    ]
    for row, raw, decimals in validated:
        lines.append(
            f"  Block {row['block_number']} / {row['deploy_id']}: "
            f"{format_units(raw, decimals)} ASI "
            f"(raw={raw}, token_decimals={decimals}) "
            f"{row['from_address']} -> {row['to_address']}"
        )
    flows = defaultdict(int)
    sent = defaultdict(int)
    received = defaultdict(int)
    for row, raw in users:
        flows[row["from_address"]] -= raw
        flows[row["to_address"]] += raw
        sent[row["from_address"]] += 1
        received[row["to_address"]] += 1
    lines.append("Address net flows (user transfers, excluding fees):")
    for address in sorted(flows):
        net = flows[address]
        lines.append(
            f"  {address}: sent={sent[address]}, received={received[address]}, "
            f"net={'+' if net >= 0 else ''}{format_units(net, scale)} ASI"
        )
    if users:
        lines.extend(
            [
                f"Blocks with user transfers: {len({row['block_number'] for row, _ in users})}",
                "Average user transfer (exact): "
                f"{Fraction(user_total, len(users) * 10**scale)} ASI",
                f"Largest user transfer: {format_units(max(raw for _, raw in users), scale)} ASI",
                f"Smallest user transfer: {format_units(min(raw for _, raw in users), scale)} ASI",
            ]
        )
    return "\n".join(lines)


def main():
    url = os.environ.get("HASURA_URL", "http://localhost:8080/v1/graphql")
    print(transfer_report(fetch_transfers(url, os.environ.get("HASURA_ADMIN_SECRET"))))


if __name__ == "__main__":
    main()
