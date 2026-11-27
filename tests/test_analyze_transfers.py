import io
import json
from decimal import localcontext

import pytest

from analyze_transfers import fetch_transfers, transfer_report
from src.amounts import MAX_AMOUNT


def transfer(raw, decimals=18, sender="A", recipient="B", block=1):
    return {
        "id": "1",
        "deploy_id": "deploy",
        "block_number": str(block),
        "from_address": sender,
        "to_address": recipient,
        "amount_dust": str(raw),
        "token_decimals": decimals,
    }


def test_report_preserves_one_atomic_unit_net_flow():
    result = transfer_report(
        [
            transfer(10**18 + 1),
            transfer(10**18, sender="B", recipient="C"),
        ]
    )
    assert "B: sent=1, received=1, net=+0.000000000000000001 ASI" in result
    assert (
        "1.000000000000000001 ASI (raw=1000000000000000001, token_decimals=18)"
        in result
    )
    assert "Total ASI moved: 2.000000000000000001 ASI" in result


def test_report_combines_recorded_precision_without_decimal_context_rounding():
    with localcontext() as context:
        context.prec = 2
        result = transfer_report(
            [
                transfer(123456789, 8, block=0),
                transfer(1),
                transfer(MAX_AMOUNT, 0),
                transfer(MAX_AMOUNT, 0),
            ]
        )
    assert "Genesis total: 1.234567890000000000 ASI" in result
    assert "1.23456789 ASI (raw=123456789, token_decimals=8)" in result
    assert f"Total ASI moved: {2 * MAX_AMOUNT + 1}.234567890000000001 ASI" in result


@pytest.mark.parametrize("records", [[], [transfer(1, block=0)]])
def test_empty_or_genesis_only_report(records):
    result = transfer_report(records)
    assert "User transfers: 0" in result
    assert "Average" not in result


@pytest.mark.parametrize("raw,decimals", [(1.0, 18), (MAX_AMOUNT + 1, 18), (1, None)])
def test_report_rejects_lossy_or_missing_units(raw, decimals):
    row = transfer(1)
    row.update(amount_dust=raw, token_decimals=decimals)
    with pytest.raises(ValueError):
        transfer_report([row])


def test_fetch_uses_all_pages_and_recorded_units(monkeypatch):
    pages = [[transfer(1)], [{**transfer(2), "id": "2"}], []]
    requests = []

    def respond(request, timeout):
        requests.append(json.loads(request.data))
        return io.BytesIO(json.dumps({"data": {"transfers": pages.pop(0)}}).encode())

    monkeypatch.setattr("analyze_transfers.urlopen", respond)
    assert len(fetch_transfers("http://unused.invalid/v1/graphql")) == 2
    assert [request["variables"]["after"] for request in requests] == [0, 1, 2]
    assert "token_decimals" in requests[0]["query"]


def test_graphql_errors_do_not_produce_a_partial_report(monkeypatch):
    monkeypatch.setattr(
        "analyze_transfers.urlopen",
        lambda *args, **kwargs: io.BytesIO(b'{"errors": [{"message": "unavailable"}]}'),
    )
    with pytest.raises(ValueError, match="incomplete"):
        fetch_transfers("http://unused.invalid/v1/graphql")
