"""The investable universe is an explicit, named choice: ``nifty500`` or ``turnover_floor``.

Owner decision 2026-10-06 (option A). PR #47 made every backtest screen on point-in-time NIFTY 500
membership, whose history starts 2016-10-24, and refuse earlier dates — which correctly fails every
window that opens before it. The fix is a second, separately named universe, not a fallback:

1. ``turnover_floor`` runs a 2012 window that ``nifty500`` refuses, and ``nifty500`` still raises
   ``IndexCoverageError`` there. The two screens differ on a covered date (an outsider is in one).
2. The default changed nothing: every sweep digest over every fold and campaign window, every arm
   and both floors is byte-identical to ``main`` 1028241.
3. A ``turnover_floor`` spec carries no ``index_membership`` and names its universe twice (the
   ``index_slug=None`` parameters and an ``investable_universe`` key), so it never shares a digest
   with a ``nifty500`` run — nor with a pre-PR-#47 one, whose spec claimed ``nifty500``.
4. The cap-tier campaign takes the flag on both verbs, records it in its manifest and header.

Offline: a fixture lake of raw ``prices_raw`` partitions and a history written through the real
``write_membership_history``. No postgres, no network.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import replace
from datetime import date
from decimal import Decimal
from pathlib import Path
from typing import Final

import pyarrow as pa
import pyarrow.parquet as pq
import pytest

import backtest.cap_tier_campaign as ctc
from backtest.folds import load_folds
from backtest.policies.naive_momentum import MomentumParameters
from backtest.run import (
    DEFAULT_UNIVERSE,
    INDEX_MEMBERSHIP_IDENTITY,
    TURNOVER_FLOOR_UNIVERSE_IDENTITY,
    UNIVERSE_CHOICES,
    BacktestError,
    BacktestResult,
    IndexCoverageError,
    UniverseParameters,
    _InvestableUniverse,
    _L1Reader,
    run_naive_momentum,
)
from backtest.run_ledger import run_digest
from backtest.sweep import (
    _DEFAULT_OPENING_CASH,
    ARMS,
    HIGH_FLOOR,
    LOW_FLOOR,
    REDEPLOY_ARMS,
    _arm_spec,
    run_digests,
)
from backtest.windows import load_windows
from dataplatform.store.paths import l1_partition_path
from dataplatform.store.schemas import PRICES_RAW_DATASET, PRICES_RAW_SCHEMA
from tests.index_history_support import write_index_history

_TF: Final = "turnover_floor"
_N500: Final = "nifty500"

# ── 1. the runtime: turnover_floor runs where nifty500 refuses ───────────────────────────────

#: Where the real NIFTY 500 membership history starts (DQ-5); the fixture history matches it.
_COVERAGE: Final = date(2016, 10, 24)
MEMBER: Final = "INE100A01010"  # in the fixture NIFTY 500 from its coverage start
OUTSIDER: Final = "INE900A01010"  # liquid and listed, never in the index
_GAIN: Final = {MEMBER: Decimal("0.03"), OUTSIDER: Decimal("0.05")}
_FLOOR: Final = Decimal("100000")
_VOLUME: Final = 100_000
_PRICE_Q: Final = Decimal("0.0001")

#: First session of each month 2011-01 → 2012-12 (a full look-back before the 2012 window), a
#: fill-headroom session, then two covered sessions in late 2016.
_SESSIONS: Final = [
    *(date(year, month, 1 if month != 1 else 3) for year in (2011, 2012) for month in range(1, 13)),
    date(2012, 12, 3),
    date(2016, 11, 1),
    date(2016, 12, 1),
]
_WINDOW: Final = (date(2012, 7, 2), date(2012, 12, 3))


def _write_prices(data_root: Path) -> None:
    for session in sorted(set(_SESSIONS)):
        months = (session.year - 2011) * 12 + session.month - 1
        records = []
        for isin, gain in _GAIN.items():
            close = (Decimal("100") * (1 + gain * months)).quantize(_PRICE_Q)
            records.append(
                {
                    "isin": isin,
                    "exchange": "NSE",
                    "symbol": isin[:6],
                    "series": "EQ",
                    "trade_date": session,
                    "open": close,
                    "high": close,
                    "low": close,
                    "close": close,
                    "last": close,
                    "prev_close": close,
                    "total_traded_qty": _VOLUME,
                    "total_traded_value": (close * _VOLUME).quantize(_PRICE_Q),
                    "total_trades": _VOLUME,
                    "deliv_qty": None,
                    "deliv_pct": None,
                }
            )
        path = l1_partition_path(PRICES_RAW_DATASET, session, data_root=data_root)
        path.parent.mkdir(parents=True, exist_ok=True)
        pq.write_table(pa.Table.from_pylist(records, schema=PRICES_RAW_SCHEMA), path)


@pytest.fixture
def lake(tmp_path: Path) -> Path:
    _write_prices(tmp_path)
    write_index_history(
        tmp_path, _N500, coverage_start=_COVERAGE, anchor=date(2026, 9, 1), members=(MEMBER,)
    )
    return tmp_path


def _universe(name: str) -> UniverseParameters:
    return UniverseParameters.for_universe(name, median_turnover_floor=_FLOOR)


def _run(lake: Path, name: str) -> BacktestResult:
    return run_naive_momentum(
        start=_WINDOW[0],
        end=_WINDOW[1],
        parameters=MomentumParameters(top_n=2),
        data_root=lake,
        adjusted=False,
        universe=_universe(name),
    )


def test_nifty500_still_refuses_a_2012_window(lake: Path) -> None:
    with pytest.raises(IndexCoverageError) as raised:
        _run(lake, _N500)
    assert raised.value.index_slug == _N500
    assert raised.value.coverage_start == _COVERAGE
    assert raised.value.as_of < _COVERAGE


def test_turnover_floor_runs_the_2012_window_nifty500_refuses(lake: Path) -> None:
    run = _run(lake, _TF)
    held = {row["isin"] for row in (*run.book.holdings, *run.book.positions)}
    # Both liquid names were bought — the outsider too, which no index screen would admit.
    assert held == {MEMBER, OUTSIDER}


def test_the_two_screens_differ_on_a_covered_date_and_never_fall_back(lake: Path) -> None:
    on = date(2016, 12, 1)
    reader = _L1Reader(data_root=lake)
    try:
        nifty = _InvestableUniverse(reader, _universe(_N500), data_root=lake)
        floor = _InvestableUniverse(reader, _universe(_TF), data_root=lake)
        assert nifty.constrain(on, {MEMBER, OUTSIDER}) == {MEMBER}
        assert floor.constrain(on, {MEMBER, OUTSIDER}) == {MEMBER, OUTSIDER}
        # The turnover-floor screen has no membership to answer with — it raises, never guesses.
        with pytest.raises(BacktestError, match="turnover_floor"):
            floor.members_asof(on)
        with pytest.raises(IndexCoverageError):
            nifty.constrain(date(2012, 7, 2), {MEMBER, OUTSIDER})
    finally:
        reader.close()


def test_the_universe_is_asked_for_by_name_only() -> None:
    assert UNIVERSE_CHOICES == (_N500, _TF) and DEFAULT_UNIVERSE == _N500
    assert UniverseParameters.for_universe(_N500, median_turnover_floor=HIGH_FLOOR) == (
        UniverseParameters(median_turnover_floor=HIGH_FLOOR)
    )
    assert _universe(_TF).index_slug is None and _universe(_TF).universe == _TF
    assert _universe(_N500).universe == _N500
    with pytest.raises(ValueError, match="unknown universe"):
        UniverseParameters.for_universe("nifty50", median_turnover_floor=HIGH_FLOOR)


# ── 2. the default moved no digest ────────────────────────────────────────────────────────────

#: sha256 of the canonical JSON ``{"start|end|label|floor": digest}`` over every arm in ``ARMS``
#: and ``REDEPLOY_ARMS`` (29), both floors, and the eight windows below (464 digests), computed at
#: ``main`` 1028241 with default contexts — before the universe option existed.
_ALL_DIGESTS_AT_1028241: Final = "f9241249cf65f9c692c1ef02a3d81c9a224a62c6f466787be8334400fe33bb83"
#: Two of them in full, so a mismatch names a run rather than only a fingerprint.
_BASELINE_AT_1028241: Final = {
    ("Swing composite (M10.7)", date(2016, 9, 1), date(2019, 8, 30)): (
        "1c28f871ca1a94bae8e3542eea6a885b65845e7aaecfb1f4bf4fd1bf478d8f44"
    ),
    ("M10.7 + regime gate", date(2016, 9, 1), date(2019, 8, 30)): (
        "13fee5f0f248d9049de87938d8187f22f3b55c6c5397565e79d16ed4c82a6489"
    ),
}


def _campaign_windows() -> list[tuple[date, date]]:
    folds = load_folds()
    windows = {(f.selection.start, f.selection.end) for f in folds.folds}
    windows |= {(f.test.start, f.test.end) for f in folds.folds}
    windows.add((folds.folds[0].test.start, folds.folds[-1].test.end))
    full = load_windows().named("full")
    windows.add((full.start, full.end))
    return sorted(windows)


def _table(**kwargs: str) -> dict[str, str]:
    table: dict[str, str] = {}
    for start, end in _campaign_windows():
        digests = run_digests(
            start=start,
            end=end,
            arms=(*ARMS, *REDEPLOY_ARMS),
            floors=(LOW_FLOOR, HIGH_FLOOR),
            **kwargs,  # type: ignore[arg-type]
        )
        for (label, floor), digest in digests.items():
            table[f"{start}|{end}|{label}|{floor}"] = digest
    return table


def _fingerprint(table: dict[str, str]) -> str:
    return hashlib.sha256(json.dumps(table, sort_keys=True).encode()).hexdigest()


def test_every_default_digest_is_byte_identical_to_main_1028241() -> None:
    default = _table()
    assert len(default) == 8 * 29 * 2
    assert _fingerprint(default) == _ALL_DIGESTS_AT_1028241
    assert _table(universe_name=_N500) == default  # naming the default is the default
    for (label, start, end), pinned in _BASELINE_AT_1028241.items():
        assert default[f"{start}|{end}|{label}|{HIGH_FLOOR}"] == pinned


def test_turnover_floor_moves_every_digest_off_the_default() -> None:
    default, floor = _table(), _table(universe_name=_TF)
    assert default.keys() == floor.keys()
    assert all(default[key] != floor[key] for key in default)
    assert not set(default.values()) & set(floor.values())


# ── 3. what a turnover_floor spec records ─────────────────────────────────────────────────────

#: Pre-PR-#47 pins (``main`` 59e8cf5, ``tests/unit/test_round2_arms.py``): F1-test, ₹10 crore.
_PRE_47: Final = {
    "Swing composite (M10.7)": "1bf1e0c2e6c789688daa28240694e7b180122c340867e63874007ac529e61de8",
    "M10.7 + regime gate": "3056c55417be64e4c1170e80e4775f3f8be9250146adac61b047ee1a7216f013",
}


def _spec(label: str, name: str) -> dict[str, str]:
    arm = next(a for a in ARMS if a.label == label)
    return _arm_spec(
        arm,
        start=date(2016, 9, 1),
        end=date(2019, 8, 30),
        universe=UniverseParameters.for_universe(name, median_turnover_floor=HIGH_FLOOR),
        opening_cash=_DEFAULT_OPENING_CASH,
        adjusted=True,
    )


@pytest.mark.parametrize("label", sorted(_PRE_47))
def test_a_turnover_floor_spec_names_its_universe_and_carries_no_index_membership(
    label: str,
) -> None:
    nifty, floor = _spec(label, _N500), _spec(label, _TF)
    assert nifty["index_membership"] == INDEX_MEMBERSHIP_IDENTITY
    assert "investable_universe" not in nifty
    assert "index_membership" not in floor
    assert floor["investable_universe"] == TURNOVER_FLOOR_UNIVERSE_IDENTITY
    assert "index_slug=None" in floor["universe"]
    assert "index_slug='nifty500'" in nifty["universe"]
    assert run_digest(floor) != run_digest(nifty)


@pytest.mark.parametrize("label", sorted(_PRE_47))
def test_a_turnover_floor_digest_is_not_a_pre_pr47_digest_and_why(label: str) -> None:
    """It differs from the pre-#47 digest of the same run — on purpose, and only by the universe.

    Before PR #47 the spec recorded ``index_slug='nifty500'`` for a screen that was a no-op until
    the 2026-09 snapshots: the very ambiguity the named choice removes. Strip the keys every run
    gained since (``holding_marks``) and the new ``investable_universe``, and the turnover-floor
    spec still differs from the pin by its ``universe`` parameters alone — put the old
    ``index_slug='nifty500'`` back and it *is* the pin. So nothing but the universe's name moved.
    """
    floor = _spec(label, _TF)
    stripped = {k: v for k, v in floor.items() if k not in ("holding_marks", "investable_universe")}
    assert run_digest(floor) != _PRE_47[label]
    assert run_digest(stripped) != _PRE_47[label]
    as_before = {**stripped, "universe": _spec(label, _N500)["universe"]}
    assert run_digest(as_before) == _PRE_47[label]


# ── 4. the cap-tier campaign ─────────────────────────────────────────────────────────────────


def test_the_cap_tier_plan_carries_the_universe_into_every_digest(tmp_path: Path) -> None:
    default = ctc.cap_tier_plan(tmp_path, data_root=None, book_actions=False)
    floor = ctc.cap_tier_plan(tmp_path, data_root=None, book_actions=False, universe=_TF)
    assert default.universe == _N500 and floor.universe == _TF
    one, two = ctc._digests(default, None), ctc._digests(floor, None)
    assert one.keys() == two.keys() and not set(one.values()) & set(two.values())
    with pytest.raises(ctc.CapTierCampaignError, match="unknown universe"):
        ctc.cap_tier_plan(tmp_path, data_root=None, universe="nifty50")


@pytest.mark.parametrize("name", [_N500, _TF])
def test_the_cap_tier_report_header_names_the_universe(tmp_path: Path, name: str) -> None:
    plan = ctc.cap_tier_plan(tmp_path, data_root=None, book_actions=False, universe=name)
    header = "\n".join(ctc._header(plan, commit="c" * 7, stores=None))
    assert f"- Universe: **`{name}`**" in header
    assert f"**`{_TF if name == _N500 else _N500}`**" not in header


def test_the_cap_tier_manifest_refuses_to_resume_on_another_universe(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(ctc, "run_units", lambda plan, *, workers: [])
    monkeypatch.setattr(ctc, "_git_commit", lambda: "c" * 40)
    argv = ["run", "--out", str(tmp_path / "out"), "--data-root", str(tmp_path / "lake")]
    assert ctc.main([*argv, "--universe", _TF]) == 0
    manifest = json.loads((tmp_path / "out" / "manifest.json").read_text(encoding="utf-8"))
    assert manifest["universe"] == _TF
    with pytest.raises(ctc.CapTierCampaignError, match="different campaign"):
        ctc.main(argv)  # the default universe, into a turnover_floor directory
    with pytest.raises(SystemExit):
        ctc.main([*argv, "--universe", "nifty50"])


def test_saved_runs_are_matched_on_the_universe(tmp_path: Path) -> None:
    """``--match-saved-runs`` sets store fields aside, never the universe's parameters."""
    assert "investable_universe" not in ctc.STORE_SPEC_FIELDS
    nifty = ctc._strategy_identity(_spec("Swing composite (M10.7)", _N500))
    floor = ctc._strategy_identity(_spec("Swing composite (M10.7)", _TF))
    assert nifty != floor
    assert replace(ctc.cap_tier_plan(tmp_path, data_root=None), universe=_TF).universe == _TF
