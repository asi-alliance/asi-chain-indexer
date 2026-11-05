"""Native invocations and results from phase-marked observer replay traces."""

from collections import defaultdict
import json

from src.amounts import amount

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
    return (
        json.dumps(par, sort_keys=True, separators=(",", ":"))
        if isinstance(par, dict)
        else None
    )


def expression(par):
    if not isinstance(par, dict):
        return {}
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


def deploy_events(deploy_report):
    """Decode committed observations without assuming every call has answered."""
    deploy = deploy_report["deployInfo"]
    failed = bool(deploy.get("errored") or deploy.get("systemDeployError"))
    segments = deploy_report.get("report", [])
    phases = {s.get("phase") for s in segments}
    known = {
        "REPORT_PHASE_USER",
        "REPORT_PHASE_PRECHARGE",
        "REPORT_PHASE_REFUND",
        "REPORT_PHASE_UNSPECIFIED",
    }
    # Replay reports omit phases without events, including an empty USER phase.
    if not phases <= known or not phases - {"REPORT_PHASE_UNSPECIFIED"}:
        raise ReportUnavailable(
            "Native transfer indexing requires phase-marked replay reports"
        )
    observations = []
    call_index = response_index = 0
    for segment in segments:
        phase = segment["phase"]
        if failed and phase == "REPORT_PHASE_USER":
            continue
        for event in segment["events"]:
            comm = event.get("Comm") or {}
            channels = (comm.get("consume") or {}).get("channels", [])
            native = any(
                private_name(channel) == TRANSFER_CHANNEL for channel in channels
            )
            if phase == "REPORT_PHASE_UNSPECIFIED":
                if native:
                    raise ReportUnavailable("Unmarked native transfer in replay report")
                continue
            if native:
                produces = comm.get("produces", [])
                if len(channels) != 1 or len(produces) != 1:
                    raise ReportUnavailable("Malformed native transfer COMM")
                pars = (produces[0].get("data") or {}).get("pars", [])
                if len(pars) != 6 or channel_key(pars[5]) is None:
                    raise ReportUnavailable("Unsupported native transfer arguments")
                ret = channel_key(pars[5])
                if phase != "REPORT_PHASE_USER":
                    observations.append(("fee", {"return_channel": ret}))
                else:
                    observations.append(
                        (
                            "call",
                            {
                                "event_index": call_index,
                                "from_address": expression(pars[0]).get("GString"),
                                "to_address": expression(pars[2]).get("GString"),
                                "amount": signed_integer(pars[3]),
                                "return_channel": ret,
                            },
                        )
                    )
                    call_index += 1
            produce = event.get("Produce")
            if produce and channel_key(produce.get("channel")) is not None:
                observations.append(
                    (
                        "response",
                        {
                            "return_channel": channel_key(produce["channel"]),
                            "pars": (produce.get("data") or {}).get("pars", []),
                            "response_index": response_index,
                            "phase": phase,
                        },
                    )
                )
                response_index += 1
    return observations


def validate_movement(call):
    for field in ("from_address", "to_address"):
        value = call.get(field)
        if not isinstance(value, str) or not value or len(value) > 150:
            raise ReportUnavailable("Malformed successful native transfer identity")
    raw = amount(call["amount"])
    if raw == 0:
        raise ReportUnavailable("Node reported a successful zero transfer")
    return raw


class TransferTracker:
    """Match only invocations in the current execution's ancestry."""

    def __init__(self, calls=(), completed=()):
        self.calls = defaultdict(list)
        self.completed = set(completed)
        self.fees = defaultdict(int)
        for call in calls:
            self.calls[call["return_channel"]].append(call)

    def observe(self, kind, data):
        ret = data["return_channel"]
        calls = self.calls[ret]
        waiting = [call for call in calls if call["id"] not in self.completed]
        if kind in {"call", "fee"}:
            if waiting or self.fees[ret]:
                raise ReportUnavailable(
                    "Concurrent native calls share a result channel"
                )
            if kind == "fee":
                self.fees[ret] += 1
            else:
                calls.append(data)
            return None
        if self.fees[ret]:
            self.fees[ret] -= 1
            return None
        if not calls:
            return None
        if len(waiting) != 1:
            raise ReportUnavailable("Missing or ambiguous native transfer result")
        pars = data["pars"]
        values = (
            expression(pars[0]).get("ETupleBody", {}).get("ps", [])
            if len(pars) == 1
            else []
        )
        success = expression(values[0]).get("GBool") if len(values) == 2 else None
        if type(success) is not bool:
            raise ReportUnavailable("Malformed native transfer result")
        call = waiting[0]
        if success:
            validate_movement(call)
        self.completed.add(call["id"])
        return call, success


def transfer_results(deploy_report):
    """Return immediate successes; durable indexing also retains pending calls."""
    tracker = TransferTracker()
    results = []
    for kind, data in deploy_events(deploy_report):
        if kind == "call":
            data = {**data, "id": data["event_index"]}
        matched = tracker.observe(kind, data)
        if matched and matched[1]:
            results.append(
                {
                    key: matched[0][key]
                    for key in ("event_index", "from_address", "to_address", "amount")
                }
            )
    return results


def block_events(report, block_hash, deploy_ids):
    if report.get("blockInfo", {}).get("blockHash") != block_hash:
        raise ReportUnavailable("Replay trace block hash mismatch")
    reports = report.get("deploys", [])
    ids = [d["deployInfo"]["sig"] for d in reports]
    if len(ids) != len(set(ids)) or ids != list(deploy_ids):
        raise ReportUnavailable("Replay trace deployment set or order mismatch")
    return [(d["deployInfo"]["sig"], deploy_events(d)) for d in reports]


def block_transfers(report, block_hash, deploy_ids):
    block_events(report, block_hash, deploy_ids)
    return {d["deployInfo"]["sig"]: transfer_results(d) for d in report["deploys"]}
