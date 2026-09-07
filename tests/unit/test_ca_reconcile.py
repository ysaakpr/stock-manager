"""M2.3 — reconcile NSE's and BSE's descriptions of the same corporate action.

§4.1 row 5: "the two exchanges describe the same action differently — reconciliation needed." The
danger is not that a disagreement exists; it is that reconciliation might *resolve* one by guessing
a winner, and a wrong guess silently rewrites one ISIN's ten years of adjusted history (risk
register row 1). So the three things asserted here are the three acceptance criteria, and the
third is the load-bearing one:

1. **Agreement collapses to one row with both sources kept.** Two feed rows that agree on ISIN,
   type, ex-date (within tolerance) and terms become one `ReconciledAction` carrying *both* raw
   `CorporateAction`s — never a reconciled claim divorced from the evidence for it.

2. **Every kind of disagreement lands in the queue and is visible via `/status/quality`.** A ratio
   mismatch, an ex-date mismatch and a single-source action each produce a `ReconciliationConflict`
   with the raw records side by side; persisted, each becomes a `quality_flag` that
   `read_quality` (the query behind `GET /status/quality`) returns with both records in `detail`.

3. **The factor chain provably cannot see an unreconciled action.** `eligible_for_factor_chain`
   returns only the agreed actions, and `load_reconciled_actions` — the database door M2.4 reads
   through — returns only rows marked `reconciled = true`. A disagreed-on action is physically
   absent from both. `test_a_disagreed_action_never_reaches_the_factor_chain` fails if either gate
   is loosened to let one through.

Offline and deterministic: no network, no Postgres. The database seam is exercised against
`_FakeConn`, an in-memory stand-in that speaks exactly the SQL `reconcile.py` and `read_quality`
issue, so "written by reconciliation, read back by the status endpoint" is a real round trip here.
"""

from __future__ import annotations

import json
from collections.abc import Sequence
from datetime import UTC, date, datetime
from decimal import Decimal
from typing import Any, cast

import pytest

from dataplatform.clock import FrozenClock
from dataplatform.corpactions import (
    ActionType,
    FaceValueTerms,
    RatioTerms,
    ReconcileError,
    ReconciliationReason,
    SingleSourcePolicy,
    UnquantifiedTerms,
    eligible_for_factor_chain,
    load_reconciled_actions,
    persist_reconciliation,
    reconcile,
)
from dataplatform.corpactions.taxonomy import DividendKind, DividendTerms
from dataplatform.ingest.corp_actions import CorporateAction
from dataplatform.status.queries import read_quality
from dataplatform.store.db import Connection

NSE = "nse_corp_actions"
BSE = "bse_corp_actions"

INFY = "INE009A01021"
TCS = "INE467B01029"
HDFC = "INE040A01034"

# A frozen instant for every clock-reading path, so `raised_at` and `as_of` are deterministic.
NOW = datetime(2026, 9, 2, 9, 30, tzinfo=UTC)


def ca(
    *,
    isin: str,
    source: str,
    ex_date: date,
    action_type: ActionType,
    terms: object,
    raw_text: str,
    knowable_date: date | None = None,
    record_date: date | None = None,
) -> CorporateAction:
    """A `CorporateAction` for the table below, defaulting the fields the tests do not vary."""
    return CorporateAction(
        isin=isin,
        ex_date=ex_date,
        action_type=action_type,
        terms=cast("Any", terms),
        source=source,
        raw_text=raw_text,
        knowable_date=ex_date if knowable_date is None else knowable_date,
        record_date=record_date,
        source_ref=None,
        l0_key=f"{source}/2026/{raw_text[:8]}",
    )


def split(isin: str, source: str, ex: date, from_v: str, to_v: str, raw: str) -> CorporateAction:
    return ca(
        isin=isin,
        source=source,
        ex_date=ex,
        action_type=ActionType.SPLIT,
        terms=FaceValueTerms(from_value=Decimal(from_v), to_value=Decimal(to_v)),
        raw_text=raw,
    )


# ── acceptance 1 — matching actions reconcile into one row with both sources ─────────────────


def test_matching_actions_reconcile_into_one_row_with_both_sources() -> None:
    nse = split(INFY, NSE, date(2024, 1, 25), "10", "2", "FV SPLIT FROM RS.10/- TO RS.2/-")
    bse = split(INFY, BSE, date(2024, 1, 25), "10", "2", "Stock Split From Rs.10/- to Rs.2/-")

    result = reconcile([nse, bse])

    assert len(result.reconciled) == 1
    assert result.queue == ()
    assert result.is_clean

    reconciled = result.reconciled[0]
    assert reconciled.isin == INFY
    assert reconciled.action_type is ActionType.SPLIT
    assert reconciled.terms == FaceValueTerms(from_value=Decimal("10"), to_value=Decimal("2"))
    # both sources recorded, and their two different raw strings preserved verbatim
    assert reconciled.source_ids == (BSE, NSE)
    assert {a.raw_text for a in reconciled.sources} == {nse.raw_text, bse.raw_text}


def test_reconciled_action_is_the_only_mintable_reconciled_type() -> None:
    """`reconciled` is a `True` literal, so a factor consumer can trust it without re-checking."""
    nse = split(TCS, NSE, date(2023, 3, 1), "1", "5", "SPLIT")
    bse = split(TCS, BSE, date(2023, 3, 1), "1", "5", "Split")
    (reconciled,) = reconcile([nse, bse]).reconciled
    assert reconciled.reconciled is True


def test_numerically_equal_terms_agree_across_feed_spellings() -> None:
    """A feed writing `1` and one writing `1.0` describe the same bonus; they must reconcile."""
    nse = ca(
        isin=HDFC,
        source=NSE,
        ex_date=date(2022, 7, 14),
        action_type=ActionType.BONUS,
        terms=RatioTerms(new_shares=Decimal("1"), held_shares=Decimal("1")),
        raw_text="BONUS 1:1",
    )
    bse = ca(
        isin=HDFC,
        source=BSE,
        ex_date=date(2022, 7, 14),
        action_type=ActionType.BONUS,
        terms=RatioTerms(new_shares=Decimal("1.0"), held_shares=Decimal("1.0")),
        raw_text="Bonus issue 1:1",
    )
    result = reconcile([nse, bse])
    assert len(result.reconciled) == 1
    assert result.queue == ()


def test_within_tolerance_exdate_difference_reconciles_and_is_noted() -> None:
    """A one-day ex-date drift is absorbed; the earlier date is canonical and the drift recorded.

    `knowable_date` is the *later* of the two — the reconciled verdict is not knowable until both
    feeds have arrived (invariant #7).
    """
    nse = split(INFY, NSE, date(2024, 1, 25), "10", "2", "FV SPLIT 10 TO 2")
    bse = split(INFY, BSE, date(2024, 1, 26), "10", "2", "Split 10 to 2")
    # give the two rows different knowable dates to check the max() rule
    nse = nse.model_copy(update={"knowable_date": date(2024, 1, 10)})
    bse = bse.model_copy(update={"knowable_date": date(2024, 1, 12)})

    (reconciled,) = reconcile([nse, bse]).reconciled
    assert reconciled.ex_date == date(2024, 1, 25)
    assert reconciled.knowable_date == date(2024, 1, 12)
    assert reconciled.reconciliation_note is not None
    assert "within tolerance" in reconciled.reconciliation_note


# ── acceptance 2 — disagreements reach the queue, visible via /status/quality ────────────────


def test_a_ratio_disagreement_lands_in_the_queue() -> None:
    nse = split(INFY, NSE, date(2024, 1, 25), "10", "2", "FV SPLIT FROM RS.10/- TO RS.2/-")
    bse = split(INFY, BSE, date(2024, 1, 25), "10", "5", "Stock Split From Rs.10/- to Rs.5/-")

    result = reconcile([nse, bse])

    assert result.reconciled == ()
    assert len(result.queue) == 1
    conflict = result.queue[0]
    assert conflict.reason is ReconciliationReason.RATIO_MISMATCH
    assert conflict.isin == INFY
    # both raw records, side by side
    assert len(conflict.records) == 2
    assert {r.source for r in conflict.records} == {NSE, BSE}
    assert conflict.severity == "ERROR"


def test_an_exdate_disagreement_lands_in_the_queue() -> None:
    nse = split(INFY, NSE, date(2024, 1, 25), "10", "2", "FV SPLIT 10 TO 2")
    bse = split(INFY, BSE, date(2024, 3, 25), "10", "2", "Split 10 to 2")  # months apart

    (conflict,) = reconcile([nse, bse]).queue
    assert conflict.reason is ReconciliationReason.EX_DATE_MISMATCH
    assert len(conflict.records) == 2
    assert conflict.severity == "ERROR"


def test_an_action_only_one_exchange_published_lands_in_the_queue() -> None:
    nse = split(INFY, NSE, date(2024, 1, 25), "10", "2", "FV SPLIT 10 TO 2")
    result = reconcile([nse])  # QUEUE is the default
    (conflict,) = result.queue
    assert conflict.reason is ReconciliationReason.SINGLE_SOURCE
    assert len(conflict.records) == 1
    assert conflict.records[0].source == NSE
    assert conflict.severity == "WARN"
    assert not result.reconciled, "the strict default never lets one feed reach the factor chain"


def test_accept_policy_admits_a_single_source_action_marked_not_cross_verified() -> None:
    """SingleSourcePolicy.ACCEPT trusts one feed but records that it was not cross-verified."""
    nse = split(INFY, NSE, date(2024, 1, 25), "10", "2", "FV SPLIT 10 TO 2")
    result = reconcile([nse], single_source_policy=SingleSourcePolicy.ACCEPT)

    assert not result.queue, "an accepted single-source action is not also queued as a conflict"
    (accepted,) = result.reconciled
    assert accepted.reconciled is True  # cleared for the factor chain
    assert accepted.cross_verified is False  # but never mistaken for a two-feed agreement
    assert accepted.source_ids == (NSE,)
    assert accepted.isin == INFY
    assert accepted.reconciliation_note and "single-source" in accepted.reconciliation_note


def test_accept_policy_still_queues_a_real_disagreement() -> None:
    """The policy relaxes only single-source; a two-feed contradiction is never accepted."""
    nse = split(INFY, NSE, date(2024, 1, 25), "10", "2", "FV SPLIT 10 TO 2")
    bse = split(INFY, BSE, date(2024, 1, 25), "10", "5", "Split 10 to 5")  # terms disagree

    result = reconcile([nse, bse], single_source_policy=SingleSourcePolicy.ACCEPT)
    assert not result.reconciled
    (conflict,) = result.queue
    assert conflict.reason is ReconciliationReason.RATIO_MISMATCH


def test_a_ratio_disagreement_is_visible_via_status_quality() -> None:
    """The clincher for acceptance 2: persist a mismatch, read it back through /status/quality.

    `read_quality` is the exact query the endpoint calls. The flag must come back with both raw
    records side by side in `detail`, so a human sees what disagreed without re-opening the feeds.
    """
    nse = split(INFY, NSE, date(2024, 1, 25), "10", "2", "FV SPLIT FROM RS.10/- TO RS.2/-")
    bse = split(INFY, BSE, date(2024, 1, 25), "10", "5", "Stock Split From Rs.10/- to Rs.5/-")
    conn = _FakeConn([nse, bse])
    clock = FrozenClock(NOW)

    result = reconcile([nse, bse])
    counts = persist_reconciliation(cast("Connection", conn), result, clock=clock)
    assert counts.flags_written == 1

    quality = read_quality(cast("Connection", conn), as_of=clock.now(), limit=100)
    assert quality.open_total == 1
    (flag,) = quality.flags
    assert flag.check_name == "ca_reconciliation"
    assert flag.severity.value == "ERROR"
    assert flag.isin == INFY
    detail = cast("dict[str, Any]", flag.detail)
    assert detail["reason"] == "RATIO_MISMATCH"
    records = cast("list[dict[str, Any]]", detail["records"])
    assert len(records) == 2
    by_source = {r["source"]: r for r in records}
    assert by_source[NSE]["raw_text"] == nse.raw_text
    assert by_source[BSE]["raw_text"] == bse.raw_text
    # the two feeds' structured terms are both present, not merged into one
    assert by_source[NSE]["terms"] != by_source[BSE]["terms"]


def test_persisting_the_same_disagreement_twice_writes_one_flag() -> None:
    """Idempotent by fingerprint: re-running reconciliation must not stack duplicate flags."""
    nse = split(INFY, NSE, date(2024, 1, 25), "10", "2", "FV SPLIT 10 TO 2")
    bse = split(INFY, BSE, date(2024, 1, 25), "10", "5", "Split 10 to 5")
    conn = _FakeConn([nse, bse])
    clock = FrozenClock(NOW)

    first = persist_reconciliation(cast("Connection", conn), reconcile([nse, bse]), clock=clock)
    second = persist_reconciliation(cast("Connection", conn), reconcile([nse, bse]), clock=clock)

    assert first.flags_written == 1
    assert second.flags_written == 0
    assert second.flags_skipped == 1
    quality = read_quality(cast("Connection", conn), as_of=clock.now(), limit=100)
    assert quality.open_total == 1


# ── acceptance 3 — the factor chain provably ignores unreconciled CAs ────────────────────────


def test_eligible_for_factor_chain_excludes_the_queue() -> None:
    agree_nse = split(INFY, NSE, date(2024, 1, 25), "10", "2", "SPLIT 10 2")
    agree_bse = split(INFY, BSE, date(2024, 1, 25), "10", "2", "Split 10 2")
    disagree_nse = split(TCS, NSE, date(2023, 3, 1), "10", "1", "SPLIT 10 1")
    disagree_bse = split(TCS, BSE, date(2023, 3, 1), "10", "5", "Split 10 5")
    single = split(HDFC, NSE, date(2022, 6, 1), "2", "1", "SPLIT 2 1")

    result = reconcile([agree_nse, agree_bse, disagree_nse, disagree_bse, single])
    eligible = eligible_for_factor_chain(result)

    eligible_isins = {a.isin for a in eligible}
    assert eligible_isins == {INFY}
    # neither the disagreed ISIN nor the single-source ISIN is eligible
    assert TCS not in eligible_isins
    assert HDFC not in eligible_isins


def test_a_disagreed_action_never_reaches_the_factor_chain() -> None:
    """The database gate: after persistence, `load_reconciled_actions` returns only the agreed row.

    This is the invariant M2.4 leans on — it reads corporate actions through this door, so a row
    the exchanges disagreed on is absent from the factor chain's input until a human resolves it.
    """
    agree_nse = split(INFY, NSE, date(2024, 1, 25), "10", "2", "SPLIT 10 2")
    agree_bse = split(INFY, BSE, date(2024, 1, 25), "10", "2", "Split 10 2")
    disagree_nse = split(TCS, NSE, date(2023, 3, 1), "10", "1", "SPLIT 10 1")
    disagree_bse = split(TCS, BSE, date(2023, 3, 1), "10", "5", "Split 10 5")
    conn = _FakeConn([agree_nse, agree_bse, disagree_nse, disagree_bse])
    clock = FrozenClock(NOW)

    result = reconcile([agree_nse, agree_bse, disagree_nse, disagree_bse])
    persist_reconciliation(cast("Connection", conn), result, clock=clock)

    loaded = load_reconciled_actions(cast("Connection", conn))
    assert {a.isin for a in loaded} == {INFY}
    # both agreeing source rows are marked reconciled; the two disagreeing ones are not
    assert len(loaded) == 2
    assert all(a.action_type is ActionType.SPLIT for a in loaded)

    loaded_tcs = load_reconciled_actions(cast("Connection", conn), isin=TCS)
    assert loaded_tcs == ()


def test_persistence_forces_a_conflicting_row_back_to_unreconciled() -> None:
    """A row wrongly left `reconciled = true` by a prior run is reset when it now conflicts."""
    nse = split(TCS, NSE, date(2023, 3, 1), "10", "1", "SPLIT 10 1")
    bse = split(TCS, BSE, date(2023, 3, 1), "10", "5", "Split 10 5")
    conn = _FakeConn([nse, bse])
    conn.set_reconciled(TCS, date(2023, 3, 1), ActionType.SPLIT, NSE, value=True)
    clock = FrozenClock(NOW)

    persist_reconciliation(cast("Connection", conn), reconcile([nse, bse]), clock=clock)

    assert load_reconciled_actions(cast("Connection", conn)) == ()


# ── other behaviours ─────────────────────────────────────────────────────────────────────────


def test_actions_of_different_types_on_one_isin_do_not_cross_match() -> None:
    """A split on NSE and a dividend on BSE for one ISIN are two events, not a disagreement."""
    nse_split = split(INFY, NSE, date(2024, 1, 25), "10", "2", "SPLIT 10 2")
    bse_div = ca(
        isin=INFY,
        source=BSE,
        ex_date=date(2024, 1, 25),
        action_type=ActionType.DIVIDEND,
        terms=DividendTerms(dividend_kind=DividendKind.FINAL, amount_inr=Decimal("18")),
        raw_text="Dividend - Rs 18",
    )
    result = reconcile([nse_split, bse_div])
    # each is single-source within its own type
    assert result.reconciled == ()
    assert len(result.queue) == 2
    assert {c.reason for c in result.queue} == {ReconciliationReason.SINGLE_SOURCE}


def test_both_feeds_agreeing_on_an_unquantified_action_reconciles() -> None:
    """Both saying "amalgamation, terms not stated" is agreement on the event; it reconciles.

    The reconciled row carries `UnquantifiedTerms`, which the factor chain refuses to compute from
    anyway — but the *event* is confirmed by both exchanges, which is what reconciliation decides.
    """
    nse = ca(
        isin=INFY,
        source=NSE,
        ex_date=date(2024, 5, 1),
        action_type=ActionType.MERGER,
        terms=UnquantifiedTerms(),
        raw_text="SCHEME OF AMALGAMATION",
    )
    bse = ca(
        isin=INFY,
        source=BSE,
        ex_date=date(2024, 5, 1),
        action_type=ActionType.MERGER,
        terms=UnquantifiedTerms(),
        raw_text="Amalgamation",
    )
    result = reconcile([nse, bse])
    assert len(result.reconciled) == 1
    assert isinstance(result.reconciled[0].terms, UnquantifiedTerms)


def test_multiple_dividends_pair_by_ex_date() -> None:
    """Two dividends a year on one ISIN pair to their same-date counterpart, not each other."""

    def div(source: str, ex: date, amount: str) -> CorporateAction:
        return ca(
            isin=INFY,
            source=source,
            ex_date=ex,
            action_type=ActionType.DIVIDEND,
            terms=DividendTerms(dividend_kind=DividendKind.INTERIM, amount_inr=Decimal(amount)),
            raw_text=f"Dividend {amount}",
        )

    actions = [
        div(NSE, date(2024, 2, 10), "18"),
        div(NSE, date(2024, 8, 10), "20"),
        div(BSE, date(2024, 2, 10), "18"),
        div(BSE, date(2024, 8, 10), "20"),
    ]
    result = reconcile(actions)
    assert len(result.reconciled) == 2
    assert result.queue == ()


def test_more_than_two_sources_is_a_defect_not_a_disagreement() -> None:
    nse = split(INFY, NSE, date(2024, 1, 25), "10", "2", "SPLIT 10 2")
    bse = split(INFY, BSE, date(2024, 1, 25), "10", "2", "Split 10 2")
    third = split(INFY, "msei_corp_actions", date(2024, 1, 25), "10", "2", "split")
    with pytest.raises(ReconcileError):
        reconcile([nse, bse, third])


def test_reconcile_of_nothing_is_clean_and_empty() -> None:
    result = reconcile([])
    assert result.reconciled == ()
    assert result.queue == ()
    assert result.is_clean
    assert eligible_for_factor_chain(result) == ()


# ── the in-memory database stand-in ──────────────────────────────────────────────────────────


class _FakeCursor:
    """A cursor over a fixed result set — only `fetchone`/`fetchall`, which is all callers use."""

    def __init__(self, rows: list[tuple[Any, ...]]) -> None:
        self._rows = rows

    def fetchone(self) -> tuple[Any, ...] | None:
        return self._rows[0] if self._rows else None

    def fetchall(self) -> list[tuple[Any, ...]]:
        return list(self._rows)


class _FakeConn:
    """An in-memory stand-in for `store.db.Connection` speaking the SQL this module and

    `read_quality` issue. It is not a general Postgres emulator: it recognises exactly the handful
    of statements the reconciliation seam and the `/status/quality` read use, and raises on
    anything else, so a query that drifts from what the fake understands fails loudly here rather
    than silently returning nothing. Cast to `Connection` at the call site; the tests never open a
    real database (AGENTIC_CONTEXT B8).
    """

    def __init__(self, seed: Sequence[CorporateAction]) -> None:
        # corporate_actions keyed by the table's natural key, each with a `reconciled` flag.
        self._ca: dict[tuple[str, date, str, str], dict[str, Any]] = {}
        for action in seed:
            self._ca[self._key(action)] = {
                "action": action,
                "reconciled": False,
                "note": None,
            }
        self._flags: list[dict[str, Any]] = []
        self._next_id = 1

    @staticmethod
    def _key(action: CorporateAction) -> tuple[str, date, str, str]:
        return (action.isin, action.ex_date, action.action_type.value, action.source)

    def set_reconciled(
        self, isin: str, ex_date: date, action_type: ActionType, source: str, *, value: bool
    ) -> None:
        self._ca[(isin, ex_date, action_type.value, source)]["reconciled"] = value

    def execute(self, sql: str, params: Sequence[Any] = ()) -> _FakeCursor:
        p = tuple(params)
        if sql.startswith("UPDATE corporate_actions SET reconciled = true"):
            note, isin, ex_date, action_type, source = p
            row = self._ca.get((isin, ex_date, action_type, source))
            if row is not None:
                row["reconciled"] = True
                row["note"] = note
            return _FakeCursor([])
        if sql.startswith("UPDATE corporate_actions SET reconciled = false"):
            isin, ex_date, action_type, source = p
            row = self._ca.get((isin, ex_date, action_type, source))
            if row is not None:
                row["reconciled"] = False
            return _FakeCursor([])
        if "INSERT INTO quality_flag" in sql:
            logical_date, check_name, severity, isin, source, detail_json, raised_at = p
            self._flags.append(
                {
                    "id": self._next_id,
                    "logical_date": logical_date,
                    "check_name": check_name,
                    "severity": severity,
                    "isin": isin,
                    "source": source,
                    "detail": json.loads(detail_json),
                    "raised_at": raised_at,
                    "resolved": False,
                }
            )
            self._next_id += 1
            return _FakeCursor([])
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
        if "GROUP BY check_name, severity" in sql:  # read_quality's by-check grouping
            since_midnight, since_week = p
            grouped: dict[tuple[str, str], list[dict[str, Any]]] = {}
            for f in self._flags:
                if not f["resolved"]:
                    grouped.setdefault((f["check_name"], f["severity"]), []).append(f)
            return _FakeCursor(
                [
                    (
                        check_name,
                        severity,
                        len(flags),
                        min(f["logical_date"] for f in flags),
                        max(f["logical_date"] for f in flags),
                        sum(1 for f in flags if f["raised_at"] >= since_midnight),
                        sum(1 for f in flags if f["raised_at"] >= since_week),
                    )
                    for (check_name, severity), flags in sorted(
                        grouped.items(), key=lambda item: (-len(item[1]), item[0][0])
                    )
                ]
            )
        if "count(*)" in sql and "quality_flag" in sql:
            counts: dict[str, int] = {}
            for f in self._flags:
                if not f["resolved"]:
                    counts[f["severity"]] = counts.get(f["severity"], 0) + 1
            return _FakeCursor([(sev, n) for sev, n in sorted(counts.items())])
        if "FROM quality_flag" in sql:  # the flags listing behind /status/quality
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
                    None,  # observed_value
                    None,  # threshold
                    f["detail"],
                    f["raised_at"],
                )
                for f in openf[:limit]
            ]
            return _FakeCursor(rows)
        if "FROM corporate_actions" in sql:  # load_reconciled_actions
            isin_filter = p[0] if p else None
            ca_rows: list[tuple[Any, ...]] = []
            for row in self._ca.values():
                if not row["reconciled"]:
                    continue
                action = cast("CorporateAction", row["action"])
                if isin_filter is not None and action.isin != isin_filter:
                    continue
                ca_rows.append(
                    (
                        action.isin,
                        action.ex_date,
                        action.action_type.value,
                        action.ratio_terms_json(),
                        action.record_date,
                        action.announcement_date,
                        action.knowable_date,
                        action.source,
                        action.source_ref,
                        action.raw_text,
                        action.l0_key,
                    )
                )
            ca_rows.sort(key=lambda r: (r[0], r[1], r[2], r[7]))
            return _FakeCursor(ca_rows)
        raise AssertionError(f"_FakeConn does not know this SQL: {sql!r}")


# ── one feed states the numbers, the other does not ─────────────────────────────────────────────


def _silent_split(isin: str, source: str, ex: date, raw: str) -> CorporateAction:
    """A split the feed announced without ever saying by how much — BSE's usual shape."""
    return ca(
        isin=isin,
        source=source,
        ex_date=ex,
        action_type=ActionType.SPLIT,
        terms=UnquantifiedTerms(),
        raw_text=raw,
    )


def test_terms_are_filled_from_the_feed_that_stated_them() -> None:
    """BSE writes `Sub Division of Equity shares` and stops; NSE says 10 → 2. That is not a clash.

    155 splits and 3 bonuses arrived this way. Queuing them as RATIO_MISMATCH asked a human to
    copy a number across from the other feed already on the screen.
    """
    ex = date(2021, 10, 28)
    result = reconcile(
        [
            split("INE335Y01020", NSE, ex, "10", "2", "FV SPLIT FROM RS.10/- TO RS.2/-"),
            _silent_split("INE335Y01020", BSE, ex, "Sub Division of Equity shares"),
        ]
    )
    assert result.queue == ()
    (action,) = result.reconciled
    assert action.terms == FaceValueTerms(from_value=Decimal("10"), to_value=Decimal("2"))
    assert action.cross_verified is True
    assert action.reconciliation_note is not None
    assert NSE in action.reconciliation_note, "the note must name which feed supplied the numbers"


def test_the_fill_works_in_either_direction() -> None:
    ex = date(2021, 10, 28)
    result = reconcile(
        [
            _silent_split("INE335Y01020", NSE, ex, "Sub-division of equity shares"),
            split("INE335Y01020", BSE, ex, "10", "2", "Stock Split From Rs.10/- to Rs.2/-"),
        ]
    )
    (action,) = result.reconciled
    assert action.terms == FaceValueTerms(from_value=Decimal("10"), to_value=Decimal("2"))
    assert action.reconciliation_note is not None
    assert BSE in action.reconciliation_note


def test_two_feeds_that_both_state_terms_and_disagree_are_still_a_mismatch() -> None:
    """The fill must never paper over a contradiction — that is the queue's whole job."""
    ex = date(2021, 10, 28)
    result = reconcile(
        [
            split("INE335Y01020", NSE, ex, "10", "2", "FV SPLIT FROM RS.10/- TO RS.2/-"),
            split("INE335Y01020", BSE, ex, "10", "5", "Stock Split From Rs.10/- to Rs.5/-"),
        ]
    )
    assert result.reconciled == ()
    assert [c.reason for c in result.queue] == [ReconciliationReason.RATIO_MISMATCH]


def test_two_silent_feeds_have_nothing_to_fill_from() -> None:
    """Both feeds confirming an event neither quantified still reconciles — the *event* agrees.

    That is `test_both_feeds_agreeing_on_an_unquantified_action_reconciles`'s rule and it stands.
    The fill adds nothing here because there is nothing to copy, and the factor chain refuses the
    row later — per-ISIN now, rather than by aborting everyone else's recompute.
    """
    ex = date(2021, 10, 28)
    result = reconcile(
        [
            _silent_split("INE335Y01020", NSE, ex, "Sub-division of equity shares"),
            _silent_split("INE335Y01020", BSE, ex, "Sub Division of Equity shares"),
        ]
    )
    (action,) = result.reconciled
    assert isinstance(action.terms, UnquantifiedTerms)


def test_accept_still_queues_a_single_source_split_that_states_no_ratio() -> None:
    """ACCEPT cannot admit what can never become a factor.

    `_event_factors` raises on an unquantified split, and that raise happens inside the recompute —
    so admitting it here is how one bad action took 19,034 reconciled ones down with it.
    """
    result = reconcile(
        [_silent_split("INE335Y01020", BSE, date(2021, 10, 28), "Sub Division of Equity shares")],
        single_source_policy=SingleSourcePolicy.ACCEPT,
    )
    assert result.reconciled == ()
    assert [c.reason for c in result.queue] == [ReconciliationReason.SINGLE_SOURCE]
