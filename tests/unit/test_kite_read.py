"""M19.2 — the read-only Kite client: GET-only allowlisted transport, typed models (I1, I3, I4).

Offline: an `httpx.MockTransport` serves the recorded, synthetic envelopes in
``tests/fixtures/kite_read/`` and records every request.
"""

from __future__ import annotations

import json
from decimal import Decimal
from pathlib import Path
from typing import Final

import httpx
import pytest
import structlog
from pydantic import SecretStr

from dataplatform.clock import IST
from execution.kite_broker import (
    KiteMalformedResponseError,
    KiteRequestError,
    KiteSessionError,
)
from execution.kite_read import (
    READ_ALLOWLIST,
    Endpoint,
    KiteConnectionError,
    KiteRawResponse,
    KiteReader,
    KiteReadNotAllowedError,
    KiteReadTransport,
    KiteTimeoutError,
    parse_holdings,
)

FIXTURES: Final[Path] = Path(__file__).resolve().parents[1] / "fixtures" / "kite_read"
API_KEY: Final = SecretStr("synthkey")
TOKEN: Final = SecretStr("synthtoken0001")
INFY: Final = "INE009A01021"
TCS: Final = "INE467B01029"

SPEC_ALLOWLIST: Final = frozenset(
    {
        "/user/profile",
        "/user/margins",
        "/portfolio/holdings",
        "/portfolio/positions",
        "/orders",
        "/trades",
        "/mf/holdings",
        "/mf/sips",
        "/mf/orders",
        "/gtt/triggers",
    }
)


class FixtureKite:
    """Serves ``<stem>.json`` for each allowlisted path; records every request."""

    def __init__(self, overrides: dict[str, list[tuple[int, bytes]]] | None = None) -> None:
        self.requests: list[httpx.Request] = []
        self._overrides = {k: list(v) for k, v in (overrides or {}).items()}

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        queued = self._overrides.get(request.url.path)
        if queued:
            status, body = queued.pop(0)
            return httpx.Response(status, content=body)
        stem = Endpoint(request.url.path).stem
        return httpx.Response(200, content=(FIXTURES / f"{stem}.json").read_bytes())


def _reader(
    kite: FixtureKite, sleeps: list[float] | None = None
) -> tuple[KiteReader, httpx.Client]:
    client = httpx.Client(transport=httpx.MockTransport(kite))
    record = sleeps if sleeps is not None else []
    transport = KiteReadTransport(
        api_key=API_KEY, access_token=TOKEN, client=client, sleep=record.append
    )
    return KiteReader(transport), client


# ── the allowlist (I1) ─────────────────────────────────────────────────────────────────────


def test_allowlist_is_exactly_the_spec() -> None:
    assert READ_ALLOWLIST == SPEC_ALLOWLIST


@pytest.mark.parametrize(
    "path",
    [
        "/orders/regular",
        "/session/token",
        "/user/profile/../../orders/regular",
        "/portfolio/holdings/authorise",
        "/gtt/triggers/1",
        "/instruments",
        "/quote",
        "/ledger",
    ],
)
def test_a_non_allowlisted_path_raises_before_any_io(path: str) -> None:
    kite = FixtureKite()
    reader, client = _reader(kite)
    transport = reader._transport
    with client, pytest.raises(KiteReadNotAllowedError):
        transport.get(path)
    assert kite.requests == []


def test_transport_has_no_write_method() -> None:
    public = {n for n in dir(KiteReadTransport) if not n.startswith("_")}
    assert public == {"get"}
    assert not {"post", "put", "delete", "patch", "send", "request"} & set(dir(KiteReader))


def test_every_read_is_a_get_with_the_version_and_auth_headers() -> None:
    kite = FixtureKite()
    reader, client = _reader(kite)
    with client:
        reader.profile()
        reader.funds()
        reader.holdings()
        reader.positions()
        reader.orders()
        reader.trades()
        reader.mf_holdings()
        reader.mf_sips()
        reader.mf_orders()
        reader.gtt_triggers()
    assert {r.url.path for r in kite.requests} == SPEC_ALLOWLIST
    for request in kite.requests:
        assert request.method == "GET"
        assert request.headers["X-Kite-Version"] == "3"
        assert request.headers["Authorization"] == "token synthkey:synthtoken0001"
        assert TOKEN.get_secret_value() not in str(request.url)


# ── errors (G11 for reads) ─────────────────────────────────────────────────────────────────


def test_429_and_5xx_are_retried_with_bounded_backoff() -> None:
    body = (FIXTURES / "holdings.json").read_bytes()
    busy = json.dumps({"status": "error", "error_type": "NetworkException", "message": "busy"})
    kite = FixtureKite(
        {"/portfolio/holdings": [(429, busy.encode()), (502, b"bad gateway"), (200, body)]}
    )
    sleeps: list[float] = []
    reader, client = _reader(kite, sleeps)
    with client:
        holdings = reader.holdings()
    assert len(kite.requests) == 3
    assert sleeps == [0.5, 1.0]
    assert len(holdings) == 6


def test_retries_are_bounded_then_the_api_error_surfaces() -> None:
    busy = json.dumps({"status": "error", "error_type": "NetworkException", "message": "busy"})
    kite = FixtureKite({"/portfolio/holdings": [(503, busy.encode())] * 10})
    sleeps: list[float] = []
    reader, client = _reader(kite, sleeps)
    with client, pytest.raises(KiteRequestError) as info:
        reader.holdings()
    assert len(kite.requests) == 4
    assert len(sleeps) == 3
    assert info.value.status_code == 503


def test_token_exception_is_a_session_error() -> None:
    kite = FixtureKite(
        {"/portfolio/holdings": [(403, (FIXTURES / "error_token.json").read_bytes())]}
    )
    reader, client = _reader(kite)
    with client, pytest.raises(KiteSessionError):
        reader.holdings()


@pytest.mark.parametrize(
    ("exc", "typed"),
    [
        (httpx.ReadTimeout, KiteTimeoutError),
        (httpx.ConnectTimeout, KiteTimeoutError),
        (httpx.ConnectError, KiteConnectionError),
        (httpx.RemoteProtocolError, KiteConnectionError),
    ],
)
def test_network_failures_are_typed_and_carry_no_secret(
    exc: type[httpx.TransportError], typed: type[Exception]
) -> None:
    def boom(request: httpx.Request) -> httpx.Response:
        raise exc(f"failed {request.headers['Authorization']}", request=request)

    client = httpx.Client(transport=httpx.MockTransport(boom))
    transport = KiteReadTransport(api_key=API_KEY, access_token=TOKEN, client=client)
    with client, pytest.raises(typed) as info:
        transport.get("/portfolio/holdings")
    assert TOKEN.get_secret_value() not in str(info.value)
    assert info.value.__cause__ is None and info.value.__suppress_context__


def test_logs_never_carry_the_token_or_the_payload() -> None:
    kite = FixtureKite()
    reader, client = _reader(kite)
    with client, structlog.testing.capture_logs() as logs:
        reader.holdings()
    assert TOKEN.get_secret_value() not in repr(logs)
    assert "INFY" not in repr(logs) and INFY not in repr(logs)
    # Only the path, the status and a byte count — never a row of the payload.
    for event in logs:
        assert set(event) <= {"event", "log_level", "path", "status", "bytes", "attempt"}


# ── models (I4) ────────────────────────────────────────────────────────────────────────────


def test_holdings_parse_to_isin_keyed_decimal_models_with_t1_held() -> None:
    kite = FixtureKite()
    reader, client = _reader(kite)
    with client:
        holdings = {h.isin: h for h in reader.holdings()}
    infy = holdings[INFY]
    assert infy.symbol == "INFY"
    assert infy.quantity == 40 and infy.t1_quantity == 5
    assert infy.held_quantity == 45  # the G7 lesson: T+1 shares are held
    assert infy.average_price == Decimal("1500.50")
    assert isinstance(infy.last_price, Decimal) and isinstance(infy.kite_pnl, Decimal)
    assert infy.mtf_quantity == 0
    assert holdings[TCS].collateral_quantity == 2
    assert holdings["INE075A01022"].held_quantity == 0  # sold out today, still reported


def test_other_endpoints_parse() -> None:
    kite = FixtureKite()
    reader, client = _reader(kite)
    with client:
        funds = reader.funds()
        (position,) = reader.positions()
        (order,) = reader.orders()
        (trade,) = reader.trades()
        (mf,) = reader.mf_holdings()
        (sip,) = reader.mf_sips()
        (mf_order,) = reader.mf_orders()
        (gtt,) = reader.gtt_triggers()
        profile = reader.profile()
    assert funds.equity.net == Decimal("125000.75")
    assert funds.commodity is not None and funds.commodity.enabled is False
    assert position.quantity == -25 and position.pnl == Decimal("-2.50")
    assert order.order_timestamp is not None and order.order_timestamp.tzinfo == IST
    assert trade.quantity == 25
    assert mf.isin == "INF000S01011" and mf.quantity == Decimal("500.123")
    assert sip.instalment_amount == Decimal("5000") and sip.next_instalment is not None
    assert mf_order.amount == Decimal("5000")
    assert gtt.trigger_values == (Decimal("2900.00"),) and gtt.orders[0].quantity == 10
    assert profile.user_id == "AB1234"
    assert not hasattr(profile, "email")


def _holdings_payload() -> list[dict[str, object]]:
    data = json.loads((FIXTURES / "holdings.json").read_text())["data"]
    assert isinstance(data, list)
    return data


@pytest.mark.parametrize(
    "missing",
    [
        "isin",
        "quantity",
        "t1_quantity",
        "used_quantity",
        "collateral_quantity",
        "average_price",
        "pnl",
    ],
)
def test_a_missing_documented_field_is_malformed_never_zero(missing: str) -> None:
    rows = _holdings_payload()
    del rows[0][missing]
    body = json.dumps({"status": "success", "data": rows}).encode()
    with pytest.raises(KiteMalformedResponseError, match=missing):
        from execution.kite_read import parse_envelope

        parse_holdings(parse_envelope(KiteRawResponse("/portfolio/holdings", 200, body)))


def test_a_float_never_reaches_a_money_field() -> None:
    rows = _holdings_payload()
    rows[0]["average_price"] = 1500.5  # a native float, as if parse_float=Decimal were skipped
    with pytest.raises(KiteMalformedResponseError, match="never float"):
        parse_holdings(rows)


def test_absent_mtf_block_is_none_not_zero() -> None:
    from execution.kite_read import parse_envelope

    rows = _holdings_payload()
    del rows[0]["mtf"]
    body = json.dumps({"status": "success", "data": rows}).encode()
    parsed = parse_holdings(parse_envelope(KiteRawResponse("/portfolio/holdings", 200, body)))
    assert parsed[0].mtf_quantity is None and parsed[0].mtf_average_price is None
