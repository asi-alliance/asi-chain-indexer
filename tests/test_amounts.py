from decimal import Decimal, localcontext
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from src.amounts import MAX_AMOUNT, amount, integer_json, token_decimal
from src.block_indexer import BlockIndexer
from src.grpc_node_client import GrpcNodeClient, _to_dict, common
from src.models import Amount
from src.monitoring import MonitoringServer
import DeployServiceV1_pb2 as service


@pytest.mark.parametrize("raw", [0, 2**53 + 1, 2**63, 2**64, 2**128, MAX_AMOUNT])
def test_exact_amount_storage_and_api(raw):
    assert amount(str(raw)) == raw
    assert Amount().process_result_value(Decimal(raw), None) == raw
    assert Amount().process_bind_param(raw, None) == raw
    # Even a small ambient Decimal context must not round amounts.
    with localcontext() as context:
        context.prec = 6
        human = token_decimal(raw, 18)
    assert format(human, "f").replace(".", "").lstrip("0") == str(raw).lstrip("0")
    payload = {"raw": raw, "human": human, "ok": True}
    assert integer_json(payload) == {
        "raw": str(raw),
        "human": format(human, "f"),
        "ok": True,
    }
    monitor = MonitoringServer.__new__(MonitoringServer)
    assert monitor._serialize_result({"amount": Decimal(raw)}) == {"amount": str(raw)}


@pytest.mark.parametrize(
    "value",
    [
        -1,
        MAX_AMOUNT + 1,
        1.0,
        True,
        "01",
        "1e9",
        Decimal("1.1"),
        Decimal("NaN"),
        Decimal("Infinity"),
        None,
    ],
)
def test_invalid_amounts_rejected(value):
    with pytest.raises(ValueError):
        amount(value)


@pytest.mark.parametrize("decimals", [-1, 78, 1.5, True, "18"])
def test_invalid_precision_rejected(decimals):
    with pytest.raises(ValueError):
        token_decimal(1, decimals)


def test_published_int64_protobuf_amounts_remain_exact():
    raw = 2**63 - 1
    assert _to_dict(common.BondInfo(stake=raw))["stake"] == raw
    assert _to_dict(common.TransferInfo(amount=raw))["amount"] == raw
    assert _to_dict(common.TransferInfo(amount=0))["amount"] == 0
    # Widening storage does not invent new fields in the node's wire protocol.
    with pytest.raises(ValueError):
        common.TransferInfo(amount=2**63)


@pytest.mark.parametrize(
    "actual,expected,valid",
    [
        (8, 8, True),
        (18, 18, True),
        (8, 18, False),
        (0, 0, True),
        (None, 8, True),
        (None, 18, False),
    ],
)
@pytest.mark.asyncio
async def test_precision_checked_against_node_metadata(actual, expected, valid):
    metadata = (
        {}
        if actual is None
        else {"nativeTokenSymbol": "ASI", "nativeTokenDecimals": actual}
    )
    response = service.StatusResponse(status=common.Status(**metadata))
    client = GrpcNodeClient.__new__(GrpcNodeClient)
    client.timeout = 1
    client.stub = SimpleNamespace(status=AsyncMock(return_value=response))
    if valid:
        await client.require_native_precision(expected)
    else:
        with pytest.raises(ValueError, match="NATIVE_TOKEN_DECIMALS"):
            await client.require_native_precision(expected)


@pytest.mark.parametrize("decimals", [8, 10, 18, 77])
def test_extracts_exact_reported_transfers(monkeypatch, decimals):
    indexer = BlockIndexer.__new__(BlockIndexer)
    result = indexer._extract_transfers(
        {
            "sig": "test",
            "timestamp": 1,
            "executedTransfers": [
                {
                    "event_index": 0,
                    "from_address": "sender",
                    "to_address": "receiver",
                    "amount": MAX_AMOUNT,
                }
            ],
        },
        10,
        decimals,
    )
    assert result[0].amount_dust == MAX_AMOUNT
    assert result[0].amount_asi == token_decimal(MAX_AMOUNT, decimals)
    assert result[0].token_decimals == decimals


def test_source_text_never_proves_a_transfer():
    indexer = BlockIndexer.__new__(BlockIndexer)
    with pytest.raises(ValueError, match="Missing executed"):
        indexer._extract_transfers(
            {"sig": "test", "term": 'match ("sender", "receiver", 001n)'}, 10
        )
    assert (
        indexer._extract_transfers(
            {
                "sig": "test",
                "timestamp": 1,
                "term": 'if (false) { @vault!("transfer", "receiver", 001n, *ret) }',
                "executedTransfers": [],
            },
            10,
        )
        == []
    )


def test_aggregate_display_can_exceed_single_amount_limit():
    raw = MAX_AMOUNT * 2
    assert format(token_decimal(raw, 18, aggregate=True), "f").replace(".", "") == str(
        raw
    )
    with pytest.raises(ValueError):
        token_decimal(raw, 18)


def test_small_decimal_api_values_use_plain_decimal_strings():
    monitor = MonitoringServer.__new__(MonitoringServer)
    for decimals in [8, 10, 18, 77]:
        expected = "0." + "0" * (decimals - 1) + "1"
        assert monitor._serialize_result(token_decimal(1, decimals)) == expected
    assert monitor._serialize_result({"gas": 2**63 - 1, "count": 2, "ok": True}) == {
        "gas": str(2**63 - 1),
        "count": 2,
        "ok": True,
    }
