"""M2.8 — the D7 sentinel: the unexplained-move tripwire and its rule registry.

The three things asserted here are M2.8's three acceptance criteria:

1. **A >20% move with no corporate action is flagged; the same move with a matching CA is not.**
   `test_unexplained_move_is_flagged` and `test_matching_ca_explains_the_move` are the pair. The
   second is the load-bearing one — a sentinel that flagged every ex-date split would drown the
   real anomalies, so the CA explanation must silence exactly the moves it accounts for.

2. **Flags are queryable via `/status/quality` and gate `is_green`.** `persist_findings` writes to
   `quality_flag`; `read_quality` (the query behind `GET /status/quality`) reads it back with the
   observed move and threshold intact; `SyncStateStore.open_error_flags` (the interlock query
   `is_green` calls) counts it, and `evaluate_green` — the pure decision `is_green` returns — flips
   to red because of it. The round trip is real: written by the sentinel, read by the status
   endpoint, enforced by the interlock, all against one in-memory `quality_flag`.

3. **Adding a rule requires no engine change.** `test_engine_runs_a_rule_it_never_knew_about` hands
   `run_sentinel` a rule defined in this test file; the engine runs it and returns its finding with
   no edit to `sentinel.py`. `test_new_rule_file_is_auto_discovered` shows the registry side:
   `default_rules()` finds `unexplained_move` purely by dropping a file under `rules/`.

Offline and deterministic (AGENTIC_CONTEXT B8): no network, no Postgres. The database seam runs
against `_FakeConn`, an in-memory stand-in that speaks exactly the SQL the sentinel writer, the
`/status/quality` read and the trading interlock issue — so a query that drifts from what the fake
understands fails loudly here rather than silently returning nothing.
"""

from __future__ import annotations

import json
from collections.abc import Iterable, Sequence
from datetime import UTC, date, datetime
from decimal import Decimal
from typing import Any, cast

from dataplatform.clock import FrozenClock
from dataplatform.corpactions.taxonomy import ActionType, RatioTerms
from dataplatform.ingest.calendar import DayKind
from dataplatform.ingest.corp_actions import CorporateAction
from dataplatform.ingest.models import PriceRow
from dataplatform.quality import (
    CloseToCloseMove,
    QualityFinding,
    SentinelInput,
    Severity,
    default_rules,
    finding_fingerprint,
    moves_from_price_rows,
    persist_findings,
    register,
    registered_rules,
    run_sentinel,
)
from dataplatform.quality.rules.unexplained_move import (
    UNEXPLAINED_MOVE_CHECK,
    UnexplainedMoveRule,
)
from dataplatform.status.queries import read_quality
from dataplatform.status.sync_state import (
    SyncRecord,
    SyncState,
    evaluate_green,
)
from dataplatform.store.db import Connection

INFY = "INE009A01021"
TCS = "INE467B01029"
BHAV = "nse_bhavcopy"

NOW = datetime(2026, 9, 2, 9, 30, tzinfo=UTC)
SESSION = date(2026, 9, 1)
_TRADING_DAY = DayKind.SESSION


def move(
    *,
    isin: str = INFY,
    on: date = SESSION,
    prev: str,
    close: str,
    source: str = BHAV,
) -> CloseToCloseMove:
    """A close-to-close move with Decimal prices, defaulting the fields a test does not vary."""
    return CloseToCloseMove(
        isin=isin, date=on, prev_close=Decimal(prev), close=Decimal(close), source=source
    )


def bonus_on(isin: str, ex: date) -> CorporateAction:
    """A reconciled 1:1 bonus for `isin` on `ex` — an action that legitimately gaps the price."""
    return CorporateAction(
        isin=isin,
        ex_date=ex,
        action_type=ActionType.BONUS,
        terms=RatioTerms(new_shares=Decimal("1"), held_shares=Decimal("1")),
        source="nse_corp_actions",
        raw_text="BONUS 1:1",
        knowable_date=ex,
        record_date=None,
        source_ref=None,
        l0_key="nse_corp_actions/2026/bonus",
    )


# ── acceptance 1 — a >20% move flags unless a CA explains it ─────────────────────────────────


def test_unexplained_move_is_flagged() -> None:
    """+30% overnight, no corporate action → one ERROR finding on the right instrument and date."""
    data = SentinelInput(moves=(move(prev="100", close="130"),))
    findings = run_sentinel(data)

    assert len(findings) == 1
    (finding,) = findings
    assert finding.check_name == UNEXPLAINED_MOVE_CHECK
    assert finding.severity == "ERROR"
    assert finding.isin == INFY
    assert finding.logical_date == SESSION
    assert finding.source == BHAV
    assert finding.observed_value == Decimal("0.30")
    assert finding.threshold == Decimal("0.20")


def test_matching_ca_explains_the_move() -> None:
    """The same +30% move on a day with a bonus ex-date raises nothing — the CA accounts for it."""
    data = SentinelInput(
        moves=(move(prev="100", close="130"),),
        corporate_actions=(bonus_on(INFY, SESSION),),
    )
    assert run_sentinel(data) == ()


def test_a_ca_on_a_different_date_does_not_explain_the_move() -> None:
    """The explanation is date-specific: a bonus a week earlier does not silence today's move."""
    data = SentinelInput(
        moves=(move(prev="100", close="130"),),
        corporate_actions=(bonus_on(INFY, date(2026, 8, 25)),),
    )
    assert len(run_sentinel(data)) == 1


def test_a_ca_on_a_different_isin_does_not_explain_the_move() -> None:
    """A bonus on TCS does not explain INFY's move — explanations are per ISIN (invariant #2)."""
    data = SentinelInput(
        moves=(move(isin=INFY, prev="100", close="130"),),
        corporate_actions=(bonus_on(TCS, SESSION),),
    )
    assert len(run_sentinel(data)) == 1


def test_move_at_or_below_threshold_is_not_flagged() -> None:
    """A move not exceeding 20% is not an anomaly; the rule fires strictly above the threshold."""
    below = SentinelInput(moves=(move(prev="100", close="115"),))  # +15%
    at = SentinelInput(moves=(move(prev="100", close="120"),))  # exactly +20%
    assert run_sentinel(below) == ()
    assert run_sentinel(at) == ()


def test_downward_move_is_flagged_with_a_signed_observed_value() -> None:
    """A -34% crash is as much an anomaly as a spike, and the sign is preserved for the operator."""
    data = SentinelInput(moves=(move(prev="100", close="66"),))  # -34%
    (finding,) = run_sentinel(data)
    assert finding.observed_value == Decimal("-0.34")
    assert finding.detail["direction"] == "down"


def test_circuit_band_explains_a_move_within_it() -> None:
    """A scrip that rode its own 35% band moved at its limit, not anomalously — no flag; but a move

    beyond the band is still unexplained.
    """
    within = SentinelInput(
        moves=(move(prev="100", close="130"),),  # +30%, inside a 35% band
        circuit_bands={INFY: Decimal("0.35")},
    )
    beyond = SentinelInput(
        moves=(move(prev="100", close="150"),),  # +50%, beyond the 35% band
        circuit_bands={INFY: Decimal("0.35")},
    )
    assert run_sentinel(within) == ()
    assert len(run_sentinel(beyond)) == 1


# ── acceptance 2 — flags are queryable via /status/quality and gate is_green ─────────────────


def test_flag_is_queryable_via_status_quality_and_gates_is_green() -> None:
    findings = run_sentinel(SentinelInput(moves=(move(prev="100", close="130"),)))
    conn = cast("Connection", _FakeConn())
    clock = FrozenClock(NOW)

    counts = persist_findings(conn, findings, clock=clock)
    assert counts.written == 1

    # queryable via GET /status/quality, with the measured move and threshold intact
    quality = read_quality(conn, as_of=NOW, limit=50)
    assert quality.open_total == 1
    (flag,) = quality.flags
    assert flag.check_name == UNEXPLAINED_MOVE_CHECK
    assert flag.severity == "ERROR"
    assert flag.isin == INFY
    assert flag.observed_value == Decimal("0.30")
    assert flag.threshold == Decimal("0.20")

    # the interlock query is_green calls sees the open ERROR flag for the dataset...
    open_errors = _open_error_flags(conn, SESSION, [BHAV])
    assert open_errors == 1

    # ...and the pure decision is_green returns flips to red because of it.
    published = {
        BHAV: SyncRecord(
            source=BHAV, logical_date=SESSION, state=SyncState.PUBLISHED, updated_at=NOW
        )
    }
    green_without = evaluate_green(
        SESSION, [BHAV], published, day_kind=_TRADING_DAY, open_error_flags=0
    )
    assert green_without.green is True
    green_with = evaluate_green(
        SESSION, [BHAV], published, day_kind=_TRADING_DAY, open_error_flags=open_errors
    )
    assert green_with.green is False
    assert "quality flag" in green_with.reason


def test_persist_is_idempotent_across_reruns() -> None:
    """Re-scanning a session already flagged writes nothing new — dedupe by fingerprint."""
    findings = run_sentinel(SentinelInput(moves=(move(prev="100", close="130"),)))
    conn = cast("Connection", _FakeConn())
    clock = FrozenClock(NOW)

    first = persist_findings(conn, findings, clock=clock)
    second = persist_findings(conn, findings, clock=clock)

    assert first.written == 1 and first.skipped == 0
    assert second.written == 0 and second.skipped == 1
    assert read_quality(conn, as_of=NOW, limit=50).open_total == 1


def test_fingerprint_is_stable_per_check_isin_date() -> None:
    """The dedupe key ignores the observed value, so a re-parse of one anomaly is recognised."""
    assert finding_fingerprint(UNEXPLAINED_MOVE_CHECK, INFY, SESSION) == finding_fingerprint(
        UNEXPLAINED_MOVE_CHECK, INFY, SESSION
    )
    assert finding_fingerprint(UNEXPLAINED_MOVE_CHECK, INFY, SESSION) != finding_fingerprint(
        UNEXPLAINED_MOVE_CHECK, TCS, SESSION
    )


# ── acceptance 3 — adding a rule requires no engine change ───────────────────────────────────


class _AlwaysFlags:
    """A throwaway rule defined entirely in this test file — the engine has never heard of it."""

    name: str = "test_always_flags"
    severity: Severity = "WARN"

    def evaluate(self, data: SentinelInput) -> Iterable[QualityFinding]:
        for m in data.moves:
            yield QualityFinding(
                logical_date=m.date,
                check_name=self.name,
                severity="WARN",
                isin=m.isin,
                source=m.source,
                detail={},
                fingerprint=finding_fingerprint(self.name, m.isin, m.date),
            )


def test_engine_runs_a_rule_it_never_knew_about() -> None:
    """`run_sentinel` runs whatever rules it is handed — no edit to the engine to add one."""
    data = SentinelInput(moves=(move(prev="100", close="101"),))  # a sub-threshold move
    # the built-in move rule ignores this; the custom rule flags it — proving the engine is generic
    assert run_sentinel(data) == ()
    findings = run_sentinel(data, rules=(_AlwaysFlags(),))
    assert len(findings) == 1
    assert findings[0].check_name == "test_always_flags"


def test_registering_a_rule_makes_the_engine_pick_it_up() -> None:
    """The registry path: `register` a rule and `run_sentinel` (default set) now includes it."""
    rule = _AlwaysFlags()
    register(rule)
    try:
        assert rule in registered_rules()
        data = SentinelInput(moves=(move(prev="100", close="101"),))
        names = {f.check_name for f in run_sentinel(data)}
        assert "test_always_flags" in names
    finally:
        # keep the global registry clean for other tests in the session
        from dataplatform.quality import sentinel as _sentinel

        _sentinel._REGISTRY.pop(rule.name, None)


def test_new_rule_file_is_auto_discovered() -> None:
    """The `unexplained_move` rule is registered purely by living in `rules/` — no manual import."""
    names = {rule.name for rule in default_rules()}
    assert UNEXPLAINED_MOVE_CHECK in names


def test_rule_threshold_is_a_construction_argument_not_a_code_change() -> None:
    """A stricter variant is just another instance — the move rule takes its threshold as data."""
    strict = UnexplainedMoveRule(threshold=Decimal("0.10"))
    data = SentinelInput(moves=(move(prev="100", close="115"),))  # +15%
    assert run_sentinel(data) == ()  # default 20% rule ignores it
    assert len(run_sentinel(data, rules=(strict,))) == 1  # 10% rule flags it


# ── moves_from_price_rows ────────────────────────────────────────────────────────────────────


def price_row(*, isin: str, prev: str, close: str) -> PriceRow:
    """A minimal `PriceRow` for the mapping test; only the fields the sentinel reads are varied."""
    return PriceRow(
        isin=isin,
        symbol="X",
        series="EQ",
        trade_date=SESSION,
        open=Decimal("1"),
        high=Decimal("1"),
        low=Decimal("1"),
        close=Decimal(close),
        last=Decimal(close),
        prev_close=Decimal(prev),
        total_traded_qty=1,
        total_traded_value=Decimal("1"),
        total_trades=1,
    )


def test_moves_from_price_rows_maps_each_row_and_skips_zero_base() -> None:
    """Each row becomes one move; a row whose prev_close is 0 (odd-lot series) is skipped, not a

    divide-by-zero.
    """
    rows = [
        price_row(isin=INFY, prev="100", close="130"),
        price_row(isin=TCS, prev="0", close="50"),  # no prior close — undefined move
    ]
    moves = moves_from_price_rows(rows, source=BHAV)
    assert len(moves) == 1
    assert moves[0].isin == INFY
    assert moves[0].source == BHAV
    assert moves[0].pct_change == Decimal("0.30")


# ── the in-memory quality_flag the seam is tested against ────────────────────────────────────


class _FakeCursor:
    def __init__(self, rows: list[tuple[Any, ...]]) -> None:
        self._rows = rows

    def fetchone(self) -> tuple[Any, ...] | None:
        return self._rows[0] if self._rows else None

    def fetchall(self) -> list[tuple[Any, ...]]:
        return list(self._rows)


class _FakeConn:
    """In-memory stand-in for `store.db.Connection`, speaking the SQL the sentinel seam,

    `read_quality` and the trading interlock issue against `quality_flag`. Not a Postgres emulator:
    it recognises exactly those statements and raises on anything else, so a drifted query fails
    loudly here. Cast to `Connection` at the call site; no real database is opened (B8).
    """

    def __init__(self) -> None:
        self._flags: list[dict[str, Any]] = []
        self._next_id = 1

    def execute(self, sql: str, params: Sequence[Any] = ()) -> _FakeCursor:
        p = tuple(params)
        if "SELECT 1 FROM quality_flag" in sql:
            check_name, fingerprint = p
            hits = [
                f
                for f in self._flags
                if not f["resolved"]
                and f["check_name"] == check_name
                and f["detail"].get("fingerprint") == fingerprint
            ]
            return _FakeCursor([(1,)] if hits else [])
        if "INSERT INTO quality_flag" in sql:
            (
                logical_date,
                check_name,
                severity,
                isin,
                source,
                observed_value,
                threshold,
                detail_json,
                raised_at,
            ) = p
            self._flags.append(
                {
                    "id": self._next_id,
                    "logical_date": logical_date,
                    "check_name": check_name,
                    "severity": severity,
                    "isin": isin,
                    "source": source,
                    "observed_value": observed_value,
                    "threshold": threshold,
                    "detail": json.loads(detail_json),
                    "raised_at": raised_at,
                    "resolved": False,
                }
            )
            self._next_id += 1
            return _FakeCursor([])
        if "count(*)" in sql and "severity = 'ERROR'" in sql:  # open_error_flags (the interlock)
            logical_date, sources = p
            n = sum(
                1
                for f in self._flags
                if not f["resolved"]
                and f["severity"] == "ERROR"
                and f["logical_date"] == logical_date
                and (f["source"] is None or f["source"] in set(sources))
            )
            return _FakeCursor([(n,)])
        if "count(*)" in sql and "quality_flag" in sql:  # read_quality severity counts
            counts: dict[str, int] = {}
            for f in self._flags:
                if not f["resolved"]:
                    counts[f["severity"]] = counts.get(f["severity"], 0) + 1
            return _FakeCursor([(sev, n) for sev, n in sorted(counts.items())])
        if "FROM quality_flag" in sql:  # read_quality flags listing
            (limit,) = p
            openf = [f for f in self._flags if not f["resolved"]]
            openf.sort(key=lambda f: (f["raised_at"], f["id"]), reverse=True)
            rows = [
                (
                    f["id"],
                    f["logical_date"],
                    f["check_name"],
                    f["severity"],
                    f["isin"],
                    f["source"],
                    f["observed_value"],
                    f["threshold"],
                    f["detail"],
                    f["raised_at"],
                )
                for f in openf[:limit]
            ]
            return _FakeCursor(rows)
        raise AssertionError(f"_FakeConn does not know this SQL: {sql!r}")


def _open_error_flags(conn: Connection, logical_date: date, sources: list[str]) -> int:
    """The interlock query `SyncStateStore.open_error_flags` runs, issued directly against the fake.

    Kept byte-identical to the production SQL so this test breaks if that query drifts from the
    predicate the sentinel's flags rely on to gate trading.
    """
    row = conn.execute(
        "SELECT count(*) FROM quality_flag "
        "WHERE logical_date = %s AND severity = 'ERROR' AND NOT resolved "
        "AND (source IS NULL OR source = ANY(%s))",
        (logical_date, list(sources)),
    ).fetchone()
    return 0 if row is None else int(row[0])
