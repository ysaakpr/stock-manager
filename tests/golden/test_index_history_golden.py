"""Golden membership facts across the pre-2021 history — each one stated by an exchange release.

The history is rebuilt from the lake (L0 releases, the 2026-10-05 anchors and listing, L1 identity
evidence) exactly as `index_history_backfill --build-only` does, and held to facts the releases
state outright, on both sides of their effective dates:

* 2017-03-31 (ind_prs16022017): NIFTY 50 swaps BHEL and IDEA for IBULHSGFIN and IOC.
* 2020-03-19 (ind_prs16032020): Yes Bank leaves NIFTY 50 for Shree Cement, a week ahead of the
  2020-03-27 review that ind_prs13052020 later voided — and Yes Bank is never in Midcap 150.
* 2020-06-26 (ind_prs10062020): Tata Motors' DVR leaves NIFTY 100, where it sat as a 101st line.
* 2021-09-30 (ind_prs23082021, the image-only release read from its transcription): Next 50's five
  swaps; and NIFTY 500 / Midcap 150 per the 2021-09-15 restatement — GILLETTE stays, the REITs
  never enter, HIKAL does.

Each fact is checked on the eve, on the day, and between announcement and effective date (no
announcement leakage). Symbols map to ISINs through the build's own resolved events — the identity
evidence of the date — never a remembered ISIN. Sizes and the composition identities are checked on
every segment. Needs the lake; skipped where there is none (CI).
"""

from __future__ import annotations

from datetime import date, timedelta
from pathlib import Path
from typing import Final

import pytest

from dataplatform.clock import SystemClock
from dataplatform.ingest.index_changes import ChangeAction
from dataplatform.ingest.index_history import HistoryBuild, build_membership_history
from dataplatform.store.l0 import L0Store

pytestmark = pytest.mark.golden

_LAKE: Final = Path("/home/ubuntu/stock-manager/data")
AS_OF: Final = date(2026, 10, 5)

#: The earliest provable date per index: each is the effective date of the first release, going
#: back, whose section for that index cannot be read (2016 detached layouts).
COVERAGE: Final = {
    "nifty50": date(2016, 4, 1),
    "niftynext50": date(2016, 10, 24),
    "nifty100": date(2016, 10, 24),
    "nifty200": date(2016, 10, 24),
    "nifty500": date(2016, 10, 24),
    "niftymidcap150": date(2016, 10, 24),
    "niftysmallcap250": date(2016, 9, 30),
}


@pytest.fixture(scope="module")
def build() -> HistoryBuild:
    if not (_LAKE / "L0/nifty_index_press_releases").is_dir() or not (_LAKE / "L1").is_dir():
        pytest.skip("no lake on this host (e.g. CI)")
    l0 = L0Store(clock=SystemClock(), data_root=_LAKE)
    return build_membership_history(l0=l0, as_of=AS_OF, data_root=_LAKE)


def _isin(build: HistoryBuild, symbol: str, slug: str, effective: date) -> str:
    found = {
        r.isin
        for r in build.events
        if r.event.symbol == symbol
        and r.event.index_slug == slug
        and r.event.effective == effective
        and r.isin is not None
    }
    assert len(found) == 1, (symbol, slug, effective, found)
    return found.pop()


def _members(build: HistoryBuild, slug: str, on: date) -> frozenset[str]:
    members = build.histories[slug].members_on(on)
    assert members is not None, (slug, on)
    return members


def _switch(
    build: HistoryBuild,
    slug: str,
    effective: date,
    announced: date,
    out: tuple[str, ...],
    into: tuple[str, ...],
    *,
    lead: timedelta = timedelta(days=3),
) -> None:
    """`out` held and `into` not from `lead` before the announcement to the eve; then reversed."""
    gone = {_isin(build, s, slug, effective) for s in out}
    came = {_isin(build, s, slug, effective) for s in into}
    for before in (announced - lead, announced, effective - timedelta(days=1)):
        held = _members(build, slug, before)
        assert gone <= held and not came & held, (slug, before)
    on_day = _members(build, slug, effective)
    assert came <= on_day and not gone & on_day, (slug, effective)


def test_coverage_reaches_2016_and_answers_nothing_before(build: HistoryBuild) -> None:
    for slug, start in COVERAGE.items():
        history = build.histories[slug]
        assert history.coverage_start == start, slug
        assert history.members_on(start - timedelta(days=1)) is None, slug
        assert history.members_on(start) is not None, slug


def test_the_image_only_release_was_read_from_its_transcription(build: HistoryBuild) -> None:
    assert build.transcribed == ("ind_prs23082021.pdf",)
    assert not [
        r for r in build.events if r.event.release == "ind_prs23082021.pdf" and r.isin is None
    ]


def test_nifty_50_on_2017_03_31(build: HistoryBuild) -> None:
    _switch(
        build,
        "nifty50",
        date(2017, 3, 31),
        date(2017, 2, 16),
        out=("BHEL", "IDEA"),
        into=("IBULHSGFIN", "IOC"),
    )


def test_yes_bank_leaves_nifty_50_on_2020_03_19_and_never_sits_in_midcap_150(
    build: HistoryBuild,
) -> None:
    _switch(
        build,
        "nifty50",
        date(2020, 3, 19),
        date(2020, 3, 16),
        out=("YESBANK",),
        into=("SHREECEM",),
        # From the announcement only: the shares were reissued that day (INE528G01027 ->
        # INE528G01035), so the days before hold the predecessor ISIN.
        lead=timedelta(0),
    )
    # Every ISIN of the issuer (by issuer code): a 2017 split and the 2020 reissue each retired one.
    issuer = _isin(build, "YESBANK", "nifty50", date(2020, 3, 19))[:9]
    yes = {i.isin for i in build.histories["nifty50"].intervals if i.isin[:9] == issuer}
    assert len(yes) == 3
    midcap = build.histories["niftymidcap150"]
    d = midcap.coverage_start
    while d < date(2020, 9, 25):  # its first real Midcap 150 seat (ind_prs20082020)
        assert not yes & _members(build, "niftymidcap150", d), d
        d += timedelta(days=7)


def test_tata_motors_dvr_leaves_nifty_100_on_2020_06_26(build: HistoryBuild) -> None:
    dvr = _isin(build, "TATAMTRDVR", "nifty100", date(2020, 6, 26))
    assert dvr in _members(build, "nifty100", date(2020, 6, 25))
    assert dvr not in _members(build, "nifty100", date(2020, 6, 26))
    assert len(_members(build, "nifty100", date(2020, 6, 25))) == 101  # the second share class
    assert len(_members(build, "nifty100", date(2020, 6, 26))) == 100


def test_next_50_on_2021_09_30_from_the_transcription(build: HistoryBuild) -> None:
    _switch(
        build,
        "niftynext50",
        date(2021, 9, 30),
        date(2021, 8, 23),
        out=("ABBOTINDIA", "ALKEM", "MRF", "PETRONET", "UBL"),
        into=("BANKBARODA", "CHOLAFIN", "JINDALSTEL", "PIIND", "SAIL"),
    )


def test_the_2021_09_15_restatement_governs_nifty_500_and_midcap_150(build: HistoryBuild) -> None:
    eff = date(2021, 9, 30)
    _switch(
        build,
        "nifty500",
        eff,
        date(2021, 9, 15),
        out=("AKZOINDIA", "VSTIND"),
        into=("HIKAL", "HGS", "LODHA"),
    )
    gillette = _isin(build, "GILLETTE", "nifty500", date(2022, 3, 31))
    for slug in ("nifty500", "niftymidcap150"):
        assert gillette in _members(build, slug, eff), slug  # the restatement kept it
    reits = {r.isin for r in build.events if r.event.symbol in {"EMBASSY", "MINDSPACE", "BIRET"}}
    for on in (eff, date(2022, 9, 30), date(2026, 9, 29)):
        assert not reits & _members(build, "nifty500", on), on


def test_every_segment_has_the_index_size_and_the_composition_holds(build: HistoryBuild) -> None:
    """Off-size only by a clipped spin-off stand-in or the DVR's second line; unions exact."""
    dvr = _isin(build, "TATAMTRDVR", "nifty100", date(2020, 6, 26))
    for slug, history in build.histories.items():
        for start, count in history.segments:
            members = _members(build, slug, start)
            stand_ins = {
                i.isin
                for i in history.intervals
                if i.entry_basis.startswith("first_seen")
                and i.effective_from <= start
                and (i.effective_to is None or start < i.effective_to)
            }
            extra = len(stand_ins) + (1 if dvr in members else 0)
            assert count - extra == history.expected_size, (slug, start, count)
    for start, _ in build.histories["nifty500"].segments:
        n100 = _members(build, "nifty100", start)
        n50_next = _members(build, "nifty50", start) | _members(build, "niftynext50", start)
        assert n100 - {dvr} == n50_next - {dvr}, start
        union = n100 | _members(build, "niftymidcap150", start)
        union |= _members(build, "niftysmallcap250", start)
        assert union - {dvr} == _members(build, "nifty500", start), start


def test_no_change_is_ever_visible_before_it_was_announced(build: HistoryBuild) -> None:
    """An interval opened by a release is knowable from that release's announcement, no earlier."""
    for history in build.histories.values():
        for interval in history.intervals:
            if interval.entry_basis.endswith(".pdf"):
                announced = {
                    r.event.announced
                    for r in build.events
                    if r.event.release == interval.entry_basis
                    and r.event.action is ChangeAction.INCLUDE
                }
                assert interval.knowable_from in announced, interval
