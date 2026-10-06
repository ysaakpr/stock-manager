"""The cap-tier report's 'stuck at end' column: window-bounded, merger-flagged, honestly headed.

An audit of the 0200e7e cap-tier report found three defects, each pinned here by a test that fails
on the code before the fix:

1. **The last print was lake-wide.** ``render`` took each holding's last print from
   ``_L1Reader.listing_windows`` (the whole lake), so a name that stopped printing inside a fold's
   test window and printed again after it read as live at that window's end. The last print is now
   bounded by the window's end (``_L1Reader.last_prints``) before ``stuck_holdings`` compares it
   with the run's terminal date.
2. **Unsourced mergers went unflagged.** The flag only matched store ``MERGER`` rows, so a scheme
   the curated table lists as unsourced (``MERGER:unsourced`` — Cairn India → Vedanta) left its
   holding stuck and unflagged. ``merger_flags`` names both kinds, distinctly.
3. **The header said mergers are never credited.** False since PR #41: sourced terms convert the
   holding. The header now says which mergers convert and which do not.
"""

from __future__ import annotations

from collections.abc import Iterator
from datetime import date
from decimal import Decimal
from pathlib import Path
from typing import Final

import pyarrow as pa
import pyarrow.parquet as pq
import pytest

import backtest.cap_tier_campaign as campaign
from backtest.book_actions import BookActionCalendar, CashDividend, UnmodelledAction
from backtest.cap_tier_campaign import (
    MERGER_IN_STORE,
    MERGER_TERMS_UNSOURCED,
    CapTierCampaignError,
    StuckHolding,
    cap_tier_plan,
    merger_flags,
    stuck_holdings,
)
from backtest.run import _L1Reader
from backtest.sweep import HIGH_FLOOR, LOW_FLOOR
from dataplatform.store.paths import l1_partition_path
from dataplatform.store.schemas import PRICES_RAW_DATASET, PRICES_RAW_SCHEMA

D = Decimal

#: Stopped printing inside the window, printed again after it (a suspension that lifted).
RESUMED: Final = "INE100A01010"
#: Printed on the window's last session.
LIVE: Final = "INE200A01010"
#: Stopped inside the window for good.
GONE: Final = "INE300A01010"
#: Only ever printed on BE (trade-for-trade): never an EQ print.
BE_ONLY: Final = "INE400A01010"

STOPPED: Final = date(2021, 3, 1)
WINDOW_END: Final = date(2022, 8, 31)
LATER: Final = date(2024, 1, 2)


def _record(isin: str, series: str = "EQ", close: str = "100") -> dict[str, object]:
    price = D(close).quantize(D("0.0001"))
    return {
        "isin": isin,
        "exchange": "NSE",
        "symbol": isin[:6],
        "series": series,
        "trade_date": None,
        "open": price,
        "high": price,
        "low": price,
        "close": price,
        "last": price,
        "prev_close": price,
        "total_traded_qty": 10,
        "total_traded_value": price * 10,
        "total_trades": 10,
        "deliv_qty": None,
        "deliv_pct": None,
    }


def _write(root: Path, session: date, records: list[dict[str, object]]) -> None:
    path = l1_partition_path(PRICES_RAW_DATASET, session, data_root=root)
    path.parent.mkdir(parents=True, exist_ok=True)
    rows = [{**r, "trade_date": session} for r in records]
    pq.write_table(pa.Table.from_pylist(rows, schema=PRICES_RAW_SCHEMA), path)


@pytest.fixture
def reader(tmp_path: Path) -> Iterator[_L1Reader]:
    _write(tmp_path, STOPPED, [_record(RESUMED), _record(LIVE), _record(GONE)])
    _write(tmp_path, WINDOW_END, [_record(LIVE), _record(BE_ONLY, series="BE")])
    _write(tmp_path, LATER, [_record(RESUMED), _record(LIVE)])
    reader = _L1Reader(data_root=tmp_path)
    try:
        yield reader
    finally:
        reader.close()


# ── defect 1: the last print is bounded by the window's end ──────────────────────────────────────


def test_last_prints_are_bounded_by_the_window_end(reader: _L1Reader) -> None:
    """RESUMED printed after the window; inside it, its last print is the day it stopped."""
    assert reader.last_prints(WINDOW_END) == {RESUMED: STOPPED, LIVE: WINDOW_END, GONE: STOPPED}
    assert reader.last_prints(LATER)[RESUMED] == LATER


def test_a_holding_that_stopped_inside_the_window_is_stuck_though_it_printed_later(
    reader: _L1Reader,
) -> None:
    """The audit's case: held at a fold's end, last print before it, printing again years later.

    The lake-wide listing window says RESUMED is live at the window's end — the old reading, which
    missed it. The window-bounded last print says it stopped, and it is counted.
    """
    lake_wide = {w.isin: w for w in reader.listing_windows()}
    assert lake_wide[RESUMED].delisted_on is None  # the old source: "still trading"
    values = {RESUMED: D("1000"), LIVE: D("2000"), GONE: D("300")}
    stuck = stuck_holdings(
        values,
        last_prints=reader.last_prints(WINDOW_END),
        terminal_date=WINDOW_END,
        mergers={},
    )
    assert stuck == (
        StuckHolding(RESUMED, STOPPED, D("1000"), None),
        StuckHolding(GONE, STOPPED, D("300"), None),
    )


def test_a_holding_with_no_eq_print_by_the_window_end_fails_loud(reader: _L1Reader) -> None:
    """Never passed as live by default: the old code read a missing last print as the end."""
    with pytest.raises(CapTierCampaignError, match=BE_ONLY):
        stuck_holdings(
            {BE_ONLY: D("1")},
            last_prints=reader.last_prints(WINDOW_END),
            terminal_date=WINDOW_END,
            mergers={},
        )


# ── defect 2: a merger with no sourced terms is flagged, distinctly ──────────────────────────────


def _calendar() -> BookActionCalendar:
    when = date(2017, 4, 27)
    return BookActionCalendar(
        [
            UnmodelledAction("INE910H01017", when, "MERGER:unsourced"),  # Cairn India → Vedanta
            UnmodelledAction("INE974H01013", date(2021, 5, 18), "MERGER"),
            UnmodelledAction("INE500A01010", when, "MERGER"),
            UnmodelledAction("INE500A01010", when, "MERGER:unsourced"),
            UnmodelledAction("INE600A01010", when, "DEMERGER"),
            CashDividend("INE700A01010", when, D("1")),
        ]
    )


def test_an_unsourced_merger_is_flagged_and_told_apart_from_a_store_merger() -> None:
    assert merger_flags(_calendar()) == {
        "INE910H01017": MERGER_TERMS_UNSOURCED,
        "INE974H01013": MERGER_IN_STORE,
        "INE500A01010": MERGER_TERMS_UNSOURCED,  # the curated table's word wins
    }
    assert merger_flags(None) == {}


def test_a_stuck_holding_carries_its_flag_into_the_report_cell() -> None:
    flags = merger_flags(_calendar())
    end = date(2017, 12, 29)
    stuck = stuck_holdings(
        {"INE910H01017": D("5"), "INE974H01013": D("6"), "INE600A01010": D("7")},
        last_prints={
            k: date(2017, 4, 26) for k in ("INE910H01017", "INE974H01013", "INE600A01010")
        },
        terminal_date=end,
        mergers=flags,
    )
    assert [x.merger for x in stuck] == [None, MERGER_TERMS_UNSOURCED, MERGER_IN_STORE]
    assert campaign._stuck_cell(stuck) == "3 (1 merger in store, 1 merger terms unsourced)"


# ── defect 3: the header no longer says mergers are never credited ───────────────────────────────


def test_the_header_says_sourced_mergers_convert_and_unsourced_ones_do_not(tmp_path: Path) -> None:
    plan = cap_tier_plan(tmp_path, data_root=tmp_path)
    text = "\n".join(campaign._header(plan, commit="abc", stores=None))
    assert "never credited a merger consideration" not in text
    assert "PR #41" in text and "converts the holding" in text
    assert "no sourced terms is not converted" in text
    assert MERGER_TERMS_UNSOURCED in text and MERGER_IN_STORE in text
    assert "matched to arms" not in text
    matched = "\n".join(campaign._header(plan, commit="abc", stores=("calendar[1]:X",)))
    assert "`calendar[1]:X`" in matched


# ── re-rendering saved runs after the store moved ────────────────────────────────────────────────


def test_saved_runs_match_by_strategy_spec_with_store_fields_set_aside(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The store moved since the runs (new book actions, a new spec key): each arm still finds its
    one run; a second run differing only in a store field makes the match ambiguous and fails."""
    import json
    import types

    plan = cap_tier_plan(tmp_path, data_root=tmp_path)

    def spec(
        arm: object, *, start: date, end: date, universe: object, **_: object
    ) -> dict[str, str]:
        floor = universe.median_turnover_floor  # type: ignore[attr-defined]
        label = arm.label  # type: ignore[attr-defined]
        return {
            "arm": label,
            "window": f"{start}..{end}",
            "floor": str(floor),
            "book_actions": "today",
            "index_membership": "new-key",
        }

    monkeypatch.setattr(campaign, "_arm_spec", spec)
    monkeypatch.setattr(campaign, "_contexts", lambda stack, actions: None)
    saved: dict[str, dict[str, str]] = {}
    (tmp_path / "runs").mkdir()
    for window in plan.windows:
        for floor in (LOW_FLOOR, HIGH_FLOOR):
            for arm in plan.arms:
                then = spec(
                    arm,
                    start=window.start,
                    end=window.end,
                    universe=types.SimpleNamespace(median_turnover_floor=floor),
                )
                then["book_actions"] = "then"
                del then["index_membership"]
                digest = f"{len(saved):064x}"
                saved[digest] = then
                (tmp_path / "runs" / f"{digest}.json").write_text(
                    json.dumps({"digest": digest, "spec": then})
                )
    monkeypatch.setattr(
        campaign, "load_run", lambda out, d: (types.SimpleNamespace(spec=saved[d]), None)
    )
    digests, stores = campaign.saved_run_digests(plan, None)
    assert stores == ("then",)
    assert len(digests) == len(saved) and set(digests.values()) == set(saved)
    arm = plan.arms[0]
    key = (arm.label, plan.windows[0].name, LOW_FLOOR)
    assert saved[digests[key]]["arm"] == arm.label

    twin = dict(saved[digests[key]], book_actions="other")
    (tmp_path / "runs" / f"{'f' * 64}.json").write_text(
        json.dumps({"digest": "f" * 64, "spec": twin})
    )
    with pytest.raises(CapTierCampaignError, match="2 saved runs"):
        campaign.saved_run_digests(plan, None)
