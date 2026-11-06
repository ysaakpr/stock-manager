"""M19.3-M19.5 — snapshots, the lake join, and the review tools (CLI and MCP), all offline.

Kite is the recorded fixtures behind an `httpx.MockTransport`; the lake is `FakeLake`; the clock is
frozen. Values are synthetic (INFY/TCS-style), and every expected number is worked by hand.
"""

from __future__ import annotations

import asyncio
import io
import json
import stat
import subprocess
from datetime import timedelta
from decimal import Decimal
from pathlib import Path

import httpx
import pytest

from analyst.owner_portfolio.__main__ import main as cli_main
from analyst.owner_portfolio.context import (
    InstrumentKind,
    SizeBucket,
    build_review,
    hhi,
    instrument_kind,
    sma_distance,
    trailing_return,
)
from analyst.owner_portfolio.mcp_server import build_server
from analyst.owner_portfolio.snapshot import (
    MIN_REFRESH_INTERVAL,
    RefreshTooSoonError,
    SnapshotError,
    SnapshotIntegrityError,
    SnapshotStore,
    default_snapshot_root,
)
from analyst.owner_portfolio.tools import TOOL_NAMES
from dataplatform.clock import FrozenClock
from execution.kite_read import Endpoint, KiteRequestError, KiteSessionError
from tests.unit.owner_portfolio_fakes import (
    FIXTURES,
    INFY,
    LIQUIDBEES,
    NIFTYBEES,
    NOW,
    SME,
    TCS,
    WIPRO,
    FakeLake,
    fixture_kite,
    fixture_reader,
    service,
)

REPO = Path(__file__).resolve().parents[2]


def _store(tmp_path: Path, clock: FrozenClock | None = None) -> SnapshotStore:
    return SnapshotStore(tmp_path / "private" / "kite", clock=clock or FrozenClock(NOW))


def _refresh(store: SnapshotStore, kite: httpx.MockTransport | None = None) -> str:
    reader, client = fixture_reader(kite)
    with client:
        return store.refresh(reader).snapshot_id


def _override(path: str, status: int, body: bytes) -> httpx.MockTransport:
    def serve(request: httpx.Request) -> httpx.Response:
        if request.url.path == path:
            return httpx.Response(status, content=body)
        stem = Endpoint(request.url.path).stem
        return httpx.Response(200, content=(FIXTURES / f"{stem}.json").read_bytes())

    return httpx.MockTransport(serve)


# ── M19.3 snapshots ────────────────────────────────────────────────────────────────────────


def test_refresh_writes_raw_envelopes_byte_for_byte_with_a_manifest(tmp_path: Path) -> None:
    store = _store(tmp_path)
    snapshot_id = _refresh(store)
    assert snapshot_id == "2026-11-06/154500"
    target = store.root / "2026-11-06" / "154500"
    manifest = json.loads((target / "manifest.json").read_text())
    assert manifest["taken_at"] == "2026-11-06T15:45:00+05:30"
    assert set(manifest["files"]) == {f"{e.stem}.json" for e in Endpoint}
    for endpoint in Endpoint:
        name = f"{endpoint.stem}.json"
        written = (target / name).read_bytes()
        assert written == (FIXTURES / name).read_bytes()  # byte for byte
        import hashlib

        assert manifest["files"][name]["sha256"] == hashlib.sha256(written).hexdigest()
        assert stat.S_IMODE((target / name).stat().st_mode) == 0o400  # write-once
    assert stat.S_IMODE(target.stat().st_mode) == 0o700


def test_snapshots_are_write_once(tmp_path: Path) -> None:
    store = _store(tmp_path)
    _refresh(store)
    reader, client = fixture_reader()
    with client:
        responses = {e: reader.raw(e) for e in Endpoint}
    with pytest.raises(SnapshotError, match="write-once"):
        store.write(responses, taken_at=NOW)


def test_refresh_respects_the_minimum_interval(tmp_path: Path) -> None:
    clock = FrozenClock(NOW)
    store = _store(tmp_path, clock)
    first = _refresh(store)
    later = _store(tmp_path, FrozenClock(NOW + MIN_REFRESH_INTERVAL - timedelta(seconds=1)))
    with pytest.raises(RefreshTooSoonError) as info:
        _refresh(later)
    assert info.value.latest.snapshot_id == first
    after = _store(tmp_path, FrozenClock(NOW + MIN_REFRESH_INTERVAL))
    assert _refresh(after) == "2026-11-06/155000"
    assert [s.snapshot_id for s in after.snapshots()] == [first, "2026-11-06/155000"]


def test_an_expired_session_writes_nothing(tmp_path: Path) -> None:
    store = _store(tmp_path)
    kite = _override("/portfolio/holdings", 403, (FIXTURES / "error_token.json").read_bytes())
    with pytest.raises(KiteSessionError):
        _refresh(store, kite)
    assert store.snapshots() == ()
    assert not store.root.exists() or not any(store.root.rglob("*.json"))


def test_a_core_endpoint_error_writes_nothing(tmp_path: Path) -> None:
    store = _store(tmp_path)
    kite = _override("/user/margins", 400, (FIXTURES / "error_permission.json").read_bytes())
    with pytest.raises(KiteRequestError):
        _refresh(store, kite)
    assert store.snapshots() == ()


def test_an_optional_endpoint_error_is_kept_and_reported_not_emptied(tmp_path: Path) -> None:
    store = _store(tmp_path)
    # Kite answers a call the app may not make with HTTP 403 PermissionException: a refusal of
    # that one endpoint, not an expired session.
    kite = _override("/mf/holdings", 403, (FIXTURES / "error_permission.json").read_bytes())
    snapshot_id = _refresh(store, kite)
    portfolio = store.load(snapshot_id)
    assert portfolio.mf_holdings == ()
    (gap,) = portfolio.unavailable
    assert gap.endpoint == "mf_holdings" and gap.error_type == "PermissionException"
    raw = store.root / "2026-11-06" / "154500" / "mf_holdings.json"
    assert raw.read_bytes() == (FIXTURES / "error_permission.json").read_bytes()


def test_a_token_exception_on_an_optional_endpoint_still_aborts(tmp_path: Path) -> None:
    store = _store(tmp_path)
    kite = _override("/gtt/triggers", 403, (FIXTURES / "error_token.json").read_bytes())
    with pytest.raises(KiteSessionError):
        _refresh(store, kite)
    assert store.snapshots() == ()


def test_load_verifies_the_manifest(tmp_path: Path) -> None:
    store = _store(tmp_path)
    snapshot_id = _refresh(store)
    path = store.root / "2026-11-06" / "154500" / "holdings.json"
    path.chmod(0o600)
    path.write_bytes(path.read_bytes().replace(b"1500.50", b"1400.50"))
    with pytest.raises(SnapshotIntegrityError):
        store.load(snapshot_id)


def test_load_parses_an_isin_keyed_owner_portfolio(tmp_path: Path) -> None:
    store = _store(tmp_path)
    _refresh(store)
    portfolio = store.load()
    assert [h.isin for h in portfolio.held] == sorted([INFY, TCS, NIFTYBEES, LIQUIDBEES, SME])
    assert portfolio.holding(WIPRO) is not None  # sold out today, still in the row set
    infy = portfolio.holding(INFY)
    assert infy is not None and infy.held_quantity == 45
    assert portfolio.funds.equity.net == Decimal("125000.75")
    assert portfolio.taken_at.utcoffset() == timedelta(hours=5, minutes=30)


def test_snapshot_ids_cannot_escape_the_root(tmp_path: Path) -> None:
    store = _store(tmp_path)
    with pytest.raises(SnapshotError):
        store.load("../../etc/154500")


def test_snapshots_live_under_gitignored_data_private(tmp_path: Path) -> None:
    root = default_snapshot_root(REPO / "data")
    assert root == REPO / "data" / "private" / "kite"
    assert "L0" not in root.parts
    probe = subprocess.run(
        ["git", "check-ignore", "-q", "data/private/kite/2026-11-06/154500/holdings.json"],
        cwd=REPO,
        check=False,
    )
    assert probe.returncode == 0, "data/private/ must be gitignored"


# ── M19.4 the lake join ────────────────────────────────────────────────────────────────────


def test_pure_arithmetic() -> None:
    closes = [Decimal(100)] * 252 + [Decimal(110)]
    assert trailing_return(closes, 21) == Decimal("0.1000")
    assert trailing_return(closes, 252) == Decimal("0.1000")
    assert trailing_return(closes[:10], 21) is None
    assert sma_distance([Decimal(100)] * 49 + [Decimal(150)], 50) == Decimal(
        "0.4851"
    )  # 150 / 101 - 1
    assert sma_distance([Decimal(1)] * 10, 50) is None
    assert hhi([Decimal("0.5"), Decimal("0.5")]) == Decimal("0.5000")
    assert hhi([Decimal(1)]) == Decimal("1.0000")
    assert instrument_kind(INFY, "Infosys", "INFY") is InstrumentKind.EQUITY
    assert instrument_kind(NIFTYBEES, "Nifty 50 BeES", "NIFTYBEES") is InstrumentKind.ETF
    assert instrument_kind(LIQUIDBEES, None, "LIQUIDBEES") is InstrumentKind.LIQUID_ETF


def test_review_values_holdings_on_the_lake_close_and_names_the_unpriced(tmp_path: Path) -> None:
    store = _store(tmp_path)
    _refresh(store)
    review = build_review(store.load(), FakeLake(), today=NOW.date())
    by = {r.isin: r for r in review.holdings}
    # Lake close x held quantity (INFY holds 40 settled + 5 T+1).
    assert by[INFY].value == Decimal("69750.00") and by[INFY].value_basis == "lake_close"
    assert by[INFY].value_kite == Decimal("69444.00")  # Kite's mark beside it
    assert by[TCS].value == Decimal("33000.00")
    assert by[NIFTYBEES].value == Decimal("52400.00")
    assert by[LIQUIDBEES].value == Decimal("50000.00")
    # The SME scrip is not in the lake: listed with Kite's numbers, never dropped.
    sme = by[SME]
    assert sme.lake is None
    assert sme.lake_context_label == "unavailable(ISIN not in the lake's identity master)"
    assert sme.value == Decimal("110760.00") and sme.value_basis == "kite_last_price"
    assert review.unpriced == ((SME, "SMEXYZ-SM", "ISIN not in the lake's identity master"),)
    assert WIPRO not in by  # sold out today: not a holding
    # Totals: 69750 + 33000 + 52400 + 50000 + 110760 = 315910; cash 125000.75.
    assert review.holdings_value == Decimal("315910.00")
    assert review.cash == Decimal("125000.75")
    assert review.total == Decimal("440910.75")
    # Unrealised P&L on Kite's average cost: INFY (1550 - 1500.50) x 45 = 2227.50.
    assert by[INFY].cost == Decimal("67522.50")
    assert by[INFY].unrealised_pnl == Decimal("2227.50")
    assert review.liquid_etf_value == Decimal("50000.00")
    assert review.liquid_etf_share == Decimal("0.1134")  # 50000 / 440910.75
    assert review.cash_and_liquid_share == Decimal("0.3969")
    total_weight = sum((r.weight for r in review.holdings), Decimal(0))
    assert abs(total_weight - 1) <= Decimal("0.0005")  # 4dp rounding


def test_review_lake_context_fields(tmp_path: Path) -> None:
    store = _store(tmp_path)
    _refresh(store)
    review = build_review(store.load(), FakeLake(), today=NOW.date())
    infy = review.holding(INFY)
    assert infy is not None and infy.lake is not None
    ctx = infy.lake
    assert ctx.close == Decimal("1550.00") and ctx.session == NOW.date()
    assert ctx.returns == {
        "1m": Decimal("0.0333"),
        "3m": Decimal("0.0333"),
        "6m": Decimal("0.0333"),
        "1y": Decimal("0.0333"),
    }
    assert ctx.high_52w == Decimal("1560.00") and ctx.low_52w == Decimal("1490.00")
    assert ctx.from_52w_high == Decimal("-0.0064")
    assert ctx.median_traded_value_20 == Decimal("1500000.00")
    assert ctx.sector == "Information Technology"
    assert ctx.size_bucket is SizeBucket.LARGE
    assert ctx.indices == ("nifty50", "nifty100", "nifty500")
    assert ctx.fundamentals is not None and ctx.fundamentals.pe_ttm == Decimal("31.0000")
    assert ctx.fundamentals.roe == "BALANCE_SHEET_ABSENT"  # a reason, never a zero
    assert ctx.thin_trading is not None and ctx.thin_trading.thin is False
    assert ctx.corporate_actions_due == ()  # INFY's dividend already went ex
    tcs = review.holding(TCS)
    assert tcs is not None and tcs.lake is not None
    assert tcs.lake.thin_trading is not None and tcs.lake.thin_trading.thin is True
    (due,) = tcs.lake.corporate_actions_due
    assert due.purpose.startswith("Interim Dividend")
    etf = review.holding(NIFTYBEES)
    assert etf is not None and etf.lake is not None and etf.lake.size_bucket is SizeBucket.ETF
    assert dict(review.kind_weights).keys() == {"EQUITY", "ETF", "LIQUID_ETF"}
    assert dict(review.sector_weights)["UNCLASSIFIED"] == Decimal("0.3506")  # the SME
    assert review.top5_weight == Decimal("1.0000")


def test_a_broken_lake_piece_degrades_only_that_piece(tmp_path: Path) -> None:
    store = _store(tmp_path)
    _refresh(store)
    review = build_review(
        store.load(), FakeLake(broken=frozenset({"thin", "fundamentals"})), today=NOW.date()
    )
    infy = review.holding(INFY)
    assert infy is not None and infy.lake is not None
    assert infy.lake.thin_trading is None and infy.lake.fundamentals is None
    assert infy.lake.returns["1m"] == Decimal("0.0333")
    assert any(n.startswith("thin-trading label: unavailable") for n in review.lake_notes)
    assert any(n.startswith("PIT fundamentals: unavailable") for n in review.lake_notes)


def test_no_lake_still_reviews_every_holding_on_kites_numbers(tmp_path: Path) -> None:
    store = _store(tmp_path)
    _refresh(store)
    review = build_review(store.load(), None, today=NOW.date())
    assert len(review.holdings) == 5
    assert all(r.lake is None and r.value_basis == "kite_last_price" for r in review.holdings)
    assert len(review.unpriced) == 5
    assert review.lake_notes == ("lake: not wired",)


# ── M19.5 the tools ────────────────────────────────────────────────────────────────────────


def test_tools_end_to_end(tmp_path: Path) -> None:
    svc = service(tmp_path)
    status = svc.session_status()
    assert status["logged_in"] is True and status["last_snapshot"] is None
    assert status["user"] == "AB****"
    first = svc.refresh()
    assert first == {
        "snapshot_id": "2026-11-06/154500",
        "taken_at_ist": "2026-11-06T15:45:00+05:30",
    }
    assert "error" in svc.refresh()  # too soon
    overview = svc.portfolio_overview()
    assert overview["totals"]["total"] == "440910.75"
    assert overview["holdings"][0]["isin"] == SME  # largest value first
    assert overview["unpriced_by_lake"][0]["isin"] == SME
    detail = svc.holding_detail("infy")  # a display symbol, resolved through the Kite row
    assert detail["isin"] == INFY and detail["held_quantity"] == 45
    assert detail["lake"]["returns"]["1m"] == "0.0333"
    assert "error" in svc.holding_detail("RELIANCE")
    exposures = svc.exposures()
    assert exposures["size_bucket"]["LARGE"] == "0.3253"  # (69750 + 33000) / 315910
    assert exposures["concentration"]["holdings"] == 5
    assert svc.orders_today()["orders"][0]["status"] == "COMPLETE"
    assert svc.trades_today()["trades"][0]["quantity"] == 25
    assert svc.mf_holdings()["mf_holdings"][0]["isin"] == "INF000S01011"
    assert svc.gtt_triggers()["gtt_triggers"][0]["trigger_values"] == ["2900.00"]


def test_changes_between_two_snapshots(tmp_path: Path) -> None:
    svc = service(tmp_path)
    svc.refresh()
    body = json.loads((FIXTURES / "holdings.json").read_text())
    rows = body["data"]
    rows[0]["quantity"] = 60  # INFY: 45 -> 65
    rows[:] = [r for r in rows if r["tradingsymbol"] != "TCS"]  # TCS sold
    rows.append({**rows[-1], "tradingsymbol": "HDFCBANK", "isin": "INE040A01034", "quantity": 7})
    next_day = service(
        tmp_path,
        clock=FrozenClock(NOW + timedelta(days=1)),
        kite=_override("/portfolio/holdings", 200, json.dumps(body).encode()),
    )
    assert next_day.refresh()["snapshot_id"] == "2026-11-07/154500"
    out = next_day.changes("2026-11-06")  # a date: the latest snapshot taken on or before it
    assert (out["from_snapshot"], out["to_snapshot"]) == ("2026-11-06/154500", "2026-11-07/154500")
    assert out["bought"] == [{"isin": "INE040A01034", "symbol": "HDFCBANK", "quantity": 7}]
    assert out["sold"] == [{"isin": TCS, "symbol": "TCS", "quantity": 10}]
    assert out["quantity_changed"] == [
        {"isin": INFY, "symbol": "INFY", "from": 45, "to": 65, "delta": 20}
    ]


def test_login_needed_is_an_answer_not_a_crash(tmp_path: Path) -> None:
    svc = service(tmp_path, logged_in=False)
    out = svc.refresh()
    assert out["error"].startswith("login needed")
    assert "execution.kite_session login" in out["error"]
    assert "error" in svc.portfolio_overview()  # no snapshot yet: said, not an empty portfolio


def test_cli_runs_the_same_functions(tmp_path: Path) -> None:
    svc = service(tmp_path)
    buf = io.StringIO()
    assert cli_main(["refresh"], service=svc, stdout=buf) == 0
    buf = io.StringIO()
    assert cli_main(["holding_detail", "TCS"], service=svc, stdout=buf) == 0
    assert json.loads(buf.getvalue())["isin"] == TCS
    buf = io.StringIO()
    assert cli_main(["--markdown", "portfolio_overview"], service=svc, stdout=buf) == 0
    assert "| isin |" in buf.getvalue()
    buf = io.StringIO()
    assert cli_main(["changes", "2026-11-01"], service=svc, stdout=buf) == 1  # no such snapshot


def test_mcp_server_serves_the_tools(tmp_path: Path) -> None:
    svc = service(tmp_path)
    server = build_server(svc)

    async def run() -> tuple[list[str], str]:
        tools = await server.list_tools()
        await server.call_tool("refresh", {})
        result = await server.call_tool("exposures", {})
        return [t.name for t in tools], json.dumps(result.model_dump(mode="json"))

    names, exposures = asyncio.run(run())
    assert tuple(names) == TOOL_NAMES
    assert "concentration" in exposures


def test_review_output_never_carries_a_float(tmp_path: Path) -> None:
    svc = service(tmp_path)
    svc.refresh()

    def walk(value: object) -> None:
        assert not isinstance(value, float), value
        if isinstance(value, dict):
            for v in value.values():
                walk(v)
        elif isinstance(value, list):
            for v in value:
                walk(v)

    for out in (svc.portfolio_overview(), svc.exposures(), svc.holding_detail(INFY)):
        walk(out)


def test_fixture_kite_has_every_endpoint() -> None:
    kite = fixture_kite()
    assert isinstance(kite, httpx.MockTransport)
    assert {f"{e.stem}.json" for e in Endpoint} <= {p.name for p in FIXTURES.glob("*.json")}
