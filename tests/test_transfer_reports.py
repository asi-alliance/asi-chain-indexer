import hashlib
from pathlib import Path

import pytest

from src.amounts import MAX_AMOUNT
from src.block_indexer import BlockIndexer
from src.transfer_reports import (
    TRANSFER_CHANNEL,
    ReportUnavailable,
    block_transfers,
    transfer_results,
)


def expr(kind, value):
    return {"exprs": [{"expr_instance": {kind: value}}]}


def name(value):
    return {
        "unforgeables": [
            {"unf_instance": {"GPrivateBody": {"id": list(bytes.fromhex(value))}}}
        ]
    }


def payment(
    raw=2**64, success=True, channel=TRANSFER_CHANNEL, ret="12" * 32, bigint=True
):
    value = (
        expr(
            "GBigInt",
            list(raw.to_bytes(max(1, (raw.bit_length() + 8) // 8), "big", signed=True)),
        )
        if bigint
        else expr("GInt", raw)
    )
    call = {
        "Comm": {
            "consume": {"channels": [name(channel)]},
            "produces": [
                {
                    "channel": name(channel),
                    "data": {
                        "pars": [
                            expr("GString", "sender"),
                            {},
                            expr("GString", "receiver"),
                            value,
                            {},
                            name(ret),
                        ]
                    },
                }
            ],
        }
    }
    reply = {
        "Produce": {
            "channel": name(ret),
            "data": {
                "pars": [
                    expr(
                        "ETupleBody",
                        {
                            "ps": [
                                expr("GBool", success),
                                expr(
                                    "GString", "" if success else "Insufficient funds"
                                ),
                            ]
                        },
                    )
                ]
            },
        }
    }
    return [call, reply]


def report(events):
    return {
        "deployInfo": {"sig": "test", "errored": False},
        "report": [
            {
                "phase": "REPORT_PHASE_PRECHARGE",
                "events": payment(1000000, ret="ab" * 32),
            },
            {"phase": "REPORT_PHASE_USER", "events": events},
            {"phase": "REPORT_PHASE_REFUND", "events": payment(800000, ret="cd" * 32)},
        ],
    }


@pytest.mark.parametrize(
    "raw", [1, 2**53 + 1, 2**63 - 1, 2**63, 2**64, 2**128, MAX_AMOUNT]
)
def test_exact_executed_bigints_and_excluded_fees(raw):
    assert [t["amount"] for t in transfer_results(report(payment(raw)))] == [raw]


def test_mixed_failed_signed_attempts_repeats_and_spoofed_channel():
    events = (
        payment(1)
        + payment(-1, False)
        + payment(2**256, False)
        + payment(2000 * 10**18, False)
    )
    events += payment(1) + payment(100, channel="34" * 32, ret="56" * 32)
    results = transfer_results(report(events))
    assert [(t["event_index"], t["amount"]) for t in results] == [(0, 1), (4, 1)]


@pytest.mark.parametrize("raw", [-1, 0, MAX_AMOUNT + 1])
def test_invalid_successful_amount_fails_closed(raw):
    with pytest.raises(ValueError):
        transfer_results(report(payment(raw)))


def test_dead_branch_and_deploy_rollback_have_no_movements():
    assert transfer_results(report([])) == []
    r = report(payment())
    r["deployInfo"]["errored"] = True
    assert transfer_results(r) == []


def test_unanswered_call_is_pending_not_success():
    assert transfer_results(report(payment()[:1])) == []


def test_ambiguous_result_is_not_success():
    with pytest.raises(ReportUnavailable):
        transfer_results(report(payment() + payment()[1:]))


def test_rejected_empty_destination_does_not_require_ledger_identity():
    events = payment(1, False)
    events[0]["Comm"]["produces"][0]["data"]["pars"][2] = expr("GString", "")
    assert transfer_results(report(events)) == []
    events[1]["Produce"]["data"]["pars"][0] = expr(
        "ETupleBody", {"ps": [expr("GBool", True), expr("GString", "")]}
    )
    with pytest.raises(ReportUnavailable, match="identity"):
        transfer_results(report(events))


def test_requires_matching_complete_report_and_marked_phases():
    r = {"blockInfo": {"blockHash": "hash"}, "deploys": [report(payment())]}
    assert block_transfers(r, "hash", ["test"])["test"][0]["amount"] == 2**64
    for block, ids in [("wrong", ["test"]), ("hash", []), ("hash", ["test", "other"])]:
        with pytest.raises(ReportUnavailable):
            block_transfers(r, block, ids)
    r["deploys"][0]["report"][1]["phase"] = "REPORT_PHASE_UNSPECIFIED"
    with pytest.raises(ReportUnavailable):
        block_transfers(r, "hash", ["test"])


def test_standard_system_vault_channel_derivation():
    # DeployDataProto(deployer=SystemVault public key,timestamp=1559156183943),
    # zero-padded seed; Blake2b512Random's sixth hash, first half = 11th name.
    pk = bytes.fromhex(
        "040f035630a5d2199184890b4b6b83440c842da0b6becca539f788f7b35d6e873561"
        "f673cd6ebe2e32236398a86f29dad992e8fba32534734300fcc5104bcfea0e"
    )

    def varint(n):
        result = bytearray()
        while n >= 128:
            result.append((n & 127) | 128)
            n >>= 7
        result.append(n)
        return bytes(result)

    seed = b"\x0a" + varint(len(pk)) + pk + b"\x18" + varint(1559156183943)
    result = hashlib.blake2b(
        seed.ljust(128, b"\0") + b"\0" * 112 + (5).to_bytes(16, "little"),
        fanout=0,
        depth=255,
        inner_size=32,
        last_node=True,
    ).digest()[:32]
    assert result.hex() == TRANSFER_CHANNEL


@pytest.mark.asyncio
async def test_actual_genesis_generator_duplicate_addresses(monkeypatch):
    monkeypatch.setattr("src.block_indexer.settings.native_token_decimals", 18)
    indexer = BlockIndexer()
    term = (Path(__file__).parent / "fixtures/genesis-vaults.rho").read_text()
    data = await indexer._extract_genesis_data(
        {
            "deploys": [{"term": term}],
            "bonds": [{"validator": "v", "stake": "10000000000000"}],
        }
    )
    assert len(data["allocations"]) == 21
    assert len({a for a, _, _ in data["allocations"]}) == 20
    assert (
        sum(raw for _, raw, _ in data["allocations"]) == 10550031996790000000000000000
    )
    assert data["bonds"][0][1] == 10000000000000


def test_public_result_channel_and_concurrent_ambiguity():
    events = payment()
    public = expr("GString", "public-return-channel")
    events[0]["Comm"]["produces"][0]["data"]["pars"][5] = public
    events[1]["Produce"]["channel"] = public
    assert transfer_results(report(events))[0]["amount"] == 2**64
    first, second = payment(1), payment(2, False)
    with pytest.raises(ReportUnavailable, match="Concurrent"):
        transfer_results(report([first[0], second[0], first[1], second[1]]))
