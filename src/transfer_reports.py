"""Native movements from authenticated, phase-marked observer replay traces.

Compact TransferInfo is int64 on current nodes and drops BigInt calls. Never
infer execution from source text or treat an unavailable report as an empty one.
"""

from collections import defaultdict, deque
import json

from src.amounts import amount

# The 11th SystemVault name: node/src/rust/web/transaction.rs at 98eb647e.
# Derived from its public genesis deployer/timestamp, not from user input.
TRANSFER_CHANNEL = "72d0f333c719323406901bca34c2935e4d92c31402fa80a2c273422e923af550"


class ReportUnavailable(ValueError):
    pass


def private_name(par):
    if not isinstance(par, dict) or par.get("exprs") or par.get("bundles"):
        return None
    names = par.get("unforgeables", [])
    if len(names) != 1:
        return None
    data = names[0].get("unf_instance", {}).get("GPrivateBody", {}).get("id")
    if not isinstance(data, list) or len(data) != 32:
        return None
    return bytes(data).hex()


def channel_key(par):
    return json.dumps(par, sort_keys=True, separators=(",", ":")) if isinstance(par, dict) else None


def expression(par):
    exprs = par.get("exprs", [])
    if len(exprs) != 1 or par.get("unforgeables"):
        return {}
    return exprs[0].get("expr_instance", {})


def signed_integer(par):
    expr = expression(par)
    if "GInt" in expr and type(expr["GInt"]) is int:
        return expr["GInt"]
    if "GBigInt" in expr and isinstance(expr["GBigInt"], list):
        return int.from_bytes(bytes(expr["GBigInt"]), "big", signed=True)
    raise ReportUnavailable("Unsupported native transfer amount in replay trace")


def transfer_results(deploy_report):
    """Return successful user movements, preserving repeated identical calls.

    Failed signed attempts are decoded but never inserted into unsigned ledger
    columns. A deploy failure rolls back its user effects. Every recorded call
    must have an explicit result; absence of an error is not proof of success.
    """
    deploy = deploy_report["deployInfo"]
    if deploy.get("errored") or deploy.get("systemDeployError"):
        return []
    segments = deploy_report.get("report", [])
    phases = {s.get("phase") for s in segments}
    known = {
        "REPORT_PHASE_USER",
        "REPORT_PHASE_PRECHARGE",
        "REPORT_PHASE_REFUND",
        "REPORT_PHASE_UNSPECIFIED",
    }
    if not phases <= known or "REPORT_PHASE_USER" not in phases:
        raise ReportUnavailable(
            "Native transfer indexing requires phase-marked replay reports"
        )
    calls = []
    waiting = defaultdict(deque)
    # A response is associated with the outstanding invocation, not with an
    # address/amount pair (two identical payments are two distinct events).
    responses = defaultdict(list)
    for segment in segments:
        if segment["phase"] == "REPORT_PHASE_UNSPECIFIED":
            # The initial replay rig can emit housekeeping before precharge.
            # Never guess the phase of an unmarked native call.
            for event in segment["events"]:
                channels = (event.get("Comm", {}).get("consume") or {}).get(
                    "channels", []
                )
                if any(
                    private_name(channel) == TRANSFER_CHANNEL for channel in channels
                ):
                    raise ReportUnavailable("Unmarked native transfer in replay report")
            continue
        if segment["phase"] != "REPORT_PHASE_USER":
            continue
        for event in segment["events"]:
            comm = event.get("Comm")
            if comm:
                channels = (comm.get("consume") or {}).get("channels", [])
                if len(channels) == 1 and private_name(channels[0]) == TRANSFER_CHANNEL:
                    produces = comm.get("produces", [])
                    if len(produces) != 1:
                        raise ReportUnavailable("Malformed native transfer COMM")
                    pars = (produces[0].get("data") or {}).get("pars", [])
                    if len(pars) != 6:
                        raise ReportUnavailable("Unsupported native transfer arguments")
                    source = expression(pars[0]).get("GString")
                    target = expression(pars[2]).get("GString")
                    ret = channel_key(pars[5])
                    if not source or not target or ret is None:
                        raise ReportUnavailable("Malformed native transfer identity")
                    call = {
                        "event_index": len(calls),
                        "from_address": source,
                        "to_address": target,
                        "amount": signed_integer(pars[3]),
                        "return_channel": ret,
                        "success": None,
                    }
                    calls.append(call)
                    if waiting[ret]:
                        raise ReportUnavailable("Concurrent native calls share a result channel")
                    waiting[ret].append(call)
            produce = event.get("Produce")
            if produce:
                ret = channel_key(produce.get("channel"))
                if ret is None:
                    continue
                pars = (produce.get("data") or {}).get("pars", [])
                if len(pars) != 1:
                    continue
                values = expression(pars[0]).get("ETupleBody", {}).get("ps", [])
                if len(values) != 2:
                    continue
                success = expression(values[0]).get("GBool")
                if type(success) is not bool:
                    continue
                responses[ret].append(success)
                if waiting[ret]:
                    waiting[ret].popleft()["success"] = success
    for call in calls:
        ret = call["return_channel"]
        if call["success"] is None or len(responses[ret]) != sum(
            c["return_channel"] == ret for c in calls
        ):
            raise ReportUnavailable("Missing or ambiguous native transfer result")
    result = []
    for call in calls:
        if call["success"]:
            raw = amount(call["amount"])
            if raw == 0:
                raise ReportUnavailable("Node reported a successful zero transfer")
            result.append({key: call[key] for key in ("event_index", "from_address", "to_address", "amount")})
    return result


def block_transfers(report, block_hash, deploy_ids):
    if report.get("blockInfo", {}).get("blockHash") != block_hash:
        raise ReportUnavailable("Replay trace block hash mismatch")
    reports = report.get("deploys", [])
    ids = [d["deployInfo"]["sig"] for d in reports]
    if len(ids) != len(set(ids)) or set(ids) != set(deploy_ids):
        raise ReportUnavailable("Replay trace deployment set mismatch")
    return {d["deployInfo"]["sig"]: transfer_results(d) for d in reports}
