"""DQ-ISIN: re-keying NSE's stated (original) ISINs through the lineage, point in time.

Offline (B8): edges are built in memory, and the lake is a `tmp_path`. HDFCBANK's real chain is
the worked case: NSE files as `INE040A01018` (retired 2011-07-15) while the company trades as
`INE040A01034` (since 2019-09-20), through `INE040A01026`.
"""

from __future__ import annotations

import hashlib
from datetime import date, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from typing import Final

import pyarrow.parquet as pq
import pytest

from dataplatform.clock import IST
from dataplatform.identity.filing_isin import (
    MAX_EXPLAINED_GAP_SESSIONS,
    FilingEdge,
    FilingIsinResolver,
    FilingIsinStatus,
    rekey_rows,
)
from dataplatform.ingest import announcements as ann
from dataplatform.ingest import shareholding as sh
from dataplatform.ingest.filing_rekey import rekey_lake
from dataplatform.ingest.nse import board_meetings as bm
from dataplatform.store.paths import l1_partition_path

HDFC_1: Final = "INE040A01018"  # as NSE files it, retired 2011-07-15
HDFC_2: Final = "INE040A01026"  # 2011-07-15 .. 2019-09-19
HDFC_3: Final = "INE040A01034"  # live since 2019-09-20


def _edge(
    p: str,
    s: str,
    eff: date,
    *,
    by: str = "L1_CONTIGUITY",
    conf: str = "DERIVED",
    gap: int = 0,
) -> FilingEdge:
    return FilingEdge(p, s, eff, by, conf, gap)


HDFC_EDGES: Final = (
    _edge(HDFC_1, HDFC_2, date(2011, 7, 15)),
    _edge(HDFC_2, HDFC_3, date(2019, 9, 20)),
)


def _resolver(*edges: FilingEdge) -> FilingIsinResolver:
    return FilingIsinResolver(edges or HDFC_EDGES)


# ── the walk ───────────────────────────────────────────────────────────────────────────────────


def test_a_stated_original_isin_maps_forward_to_the_live_one_never_backward() -> None:
    """Fails if the mapping direction is inverted: the predecessor goes to the successor, and the
    successor (the live ISIN) never moves back to the one it replaced."""
    resolver = _resolver()
    on = date(2026, 11, 3)
    forward = resolver.resolve(HDFC_1, on)
    assert (forward.isin, forward.as_filed, forward.status, forward.hops) == (
        HDFC_3,
        HDFC_1,
        FilingIsinStatus.REKEYED,
        2,
    )
    live = resolver.resolve(HDFC_3, on)
    assert (live.isin, live.status) == (HDFC_3, FilingIsinStatus.AS_FILED)
    assert resolver.resolve(HDFC_2, on).isin == HDFC_3


def test_a_filing_before_the_reissue_stays_on_the_isin_live_that_day() -> None:
    """PIT: a filing knowable the day before the successor's first session cannot know it."""
    resolver = _resolver()
    before = resolver.resolve(HDFC_2, date(2019, 9, 19))
    assert (before.isin, before.status) == (HDFC_2, FilingIsinStatus.PRE_REISSUE)
    on_the_day = resolver.resolve(HDFC_2, date(2019, 9, 20))
    assert (on_the_day.isin, on_the_day.status) == (HDFC_3, FilingIsinStatus.REKEYED)
    # Mid-chain: the 2011 hop is known in 2015, the 2019 one is not.
    mid = resolver.resolve(HDFC_1, date(2015, 1, 1))
    assert (mid.isin, mid.status, mid.hops) == (HDFC_2, FilingIsinStatus.REKEYED, 1)
    first = resolver.resolve(HDFC_1, date(2011, 7, 14))
    assert (first.isin, first.status) == (HDFC_1, FilingIsinStatus.PRE_REISSUE)


def test_an_unexplained_long_gap_edge_is_not_walked_and_is_counted() -> None:
    a, b = "INE891A01014", "INE891A01022"
    long_gap = _resolver(_edge(a, b, date(2026, 8, 3), gap=2072))
    out = long_gap.resolve(a, date(2026, 11, 3))
    assert (out.isin, out.status) == (a, FilingIsinStatus.UNEXPLAINED)
    at_limit = _resolver(_edge(a, b, date(2026, 8, 3), gap=MAX_EXPLAINED_GAP_SESSIONS))
    assert at_limit.resolve(a, date(2026, 11, 3)).isin == b
    corroborated = _resolver(_edge(a, b, date(2026, 8, 3), conf="CORROBORATED", gap=40))
    assert corroborated.resolve(a, date(2026, 11, 3)).isin == b
    manual = _resolver(_edge(a, b, date(2026, 8, 3), by="MANUAL", gap=40))
    assert manual.resolve(a, date(2026, 11, 3)).isin == b


def test_an_edge_across_issuers_or_security_types_is_never_walked() -> None:
    on = date(2026, 11, 3)
    fund = _resolver(_edge("INF732E01011", "INF204KB14I2", date(2019, 12, 20), conf="CORROBORATED"))
    assert fund.resolve("INF732E01011", on).status is FilingIsinStatus.CROSS_ISSUER
    other_issuer = _resolver(_edge("INE111A01011", "INE222B01012", date(2020, 1, 1)))
    out = other_issuer.resolve("INE111A01011", on)
    assert (out.isin, out.status) == ("INE111A01011", FilingIsinStatus.CROSS_ISSUER)
    debenture = _resolver(_edge("INE031A07873", "INE031A01017", date(2020, 1, 1)))
    assert debenture.resolve("INE031A07873", on).status is FilingIsinStatus.CROSS_ISSUER


def test_a_successor_claimed_by_two_predecessors_is_ambiguous() -> None:
    resolver = _resolver(
        _edge("INE500A01011", "INE500A01029", date(2020, 1, 1)),
        _edge("INE500A01037", "INE500A01029", date(2021, 1, 1)),
    )
    out = resolver.resolve("INE500A01011", date(2026, 1, 1))
    assert (out.isin, out.status) == ("INE500A01011", FilingIsinStatus.AMBIGUOUS)


def test_a_cycle_stops_the_walk_instead_of_looping() -> None:
    resolver = _resolver(
        _edge("INE600A01011", "INE600A01029", date(2020, 1, 1)),
        _edge("INE600A01029", "INE600A01011", date(2021, 1, 1)),
    )
    out = resolver.resolve("INE600A01011", date(2026, 1, 1))
    assert out.status is FilingIsinStatus.AMBIGUOUS


def test_a_rekey_that_would_collide_on_a_unique_key_keeps_the_stated_isin() -> None:
    rows = [(HDFC_1, "Q"), (HDFC_3, "Q"), (HDFC_1, "R")]
    out, tally = rekey_rows(
        rows,
        _resolver(),
        as_filed=lambda r: r[0],
        knowable=lambda _r: date(2026, 11, 3),
        current=lambda r: (r[0], None),
        rebuild=lambda r, o: (o.isin, r[1]),
        unique_key=lambda r: r,
    )
    assert out == ((HDFC_1, "Q"), (HDFC_3, "Q"), (HDFC_3, "R"))
    assert tally.by_status == {"ambiguous": 1, "as_filed": 1, "rekeyed": 1}
    assert tally.unmapped == {HDFC_1: "ambiguous"}


# ── the three datasets ─────────────────────────────────────────────────────────────────────────


def _meeting(isin: str, *, disseminated: datetime, meeting: date) -> bm.BoardMeetingRow:
    return bm.BoardMeetingRow(
        isin=isin,
        symbol="HDFCBANK",
        meeting_date=meeting,
        purpose="Financial Results",
        description="to consider financial results",
        disseminated_at=disseminated,
        knowable_date=bm.knowable_date_of(disseminated),
        is_results=True,
        l0_key="nse_board_meetings/2026-10-01/x.json",
    )


def _announcement(isin: str, ts: datetime, ref: str) -> ann.AnnouncementRow:
    return ann.AnnouncementRow(
        ts=ts,
        source=ann.NSE_SOURCE_ID,
        isin=isin,
        symbol="HDFCBANK",
        subject="Outcome of Board Meeting",
        source_ref=ref,
        l0_key="nse_announcements/2026-10-20/y.json",
    )


def _holding(isin: str, filed: date) -> sh.ShareholdingRow:
    return sh.ShareholdingRow(
        isin=isin,
        name="HDFC Bank Limited",
        period_end=date(2026, 9, 30),
        filing_date=filed,
        promoter_holding_pct=Decimal("0.00"),
        public_pct=Decimal("100.00"),
        source=sh.SOURCE_ID,
        l0_key="nse_shareholding/2026-10-15/z.json",
    )


def test_board_meetings_merge_rekeys_and_a_later_capture_does_not_double_a_row(
    tmp_path: Path,
) -> None:
    when = datetime(2026, 10, 1, 17, 0, tzinfo=IST)
    row = _meeting(HDFC_1, disseminated=when, meeting=date(2026, 10, 18))
    assert bm.merge_l1([row], data_root=tmp_path, resolver=_resolver()) == 1
    (held,) = bm.read_l1(when.date(), data_root=tmp_path)
    assert (held.isin, held.isin_as_filed) == (HDFC_3, HDFC_1)
    # The next capture re-serves the same intimation, as stated: same key, nothing new.
    assert bm.merge_l1([row], data_root=tmp_path, resolver=_resolver()) == 0
    assert len(bm.read_pit(when.date(), data_root=tmp_path)) == 1


def test_a_partition_written_before_the_column_existed_reads_as_stated(tmp_path: Path) -> None:
    when = datetime(2026, 10, 1, 17, 0, tzinfo=IST)
    bm.merge_l1(
        [_meeting(HDFC_1, disseminated=when, meeting=date(2026, 10, 18))], data_root=tmp_path
    )
    path = l1_partition_path(bm.BOARD_MEETINGS_DATASET, when.date(), data_root=tmp_path)
    table = pq.read_table(path)
    pq.write_table(table.drop_columns(["isin_as_filed"]), path)
    (row,) = bm.read_l1(when.date(), data_root=tmp_path)
    assert (row.isin, row.isin_as_filed, row.filed_isin) == (HDFC_1, None, HDFC_1)


def test_announcements_and_shareholding_rekey_on_their_own_knowable_dates() -> None:
    late = datetime(2026, 10, 20, 23, 30, tzinfo=IST)  # 18:00 UTC: the IST date is the 20th
    rows, tally = ann.rekey([_announcement(HDFC_2, late, "1")], _resolver())
    assert (rows[0].isin, rows[0].isin_as_filed, tally.changed) == (HDFC_3, HDFC_2, 1)
    old = datetime(2019, 9, 19, 23, 30, tzinfo=IST)  # IST date 2019-09-19: before the reissue
    (pre,), _ = ann.rekey([_announcement(HDFC_2, old, "2")], _resolver())
    assert pre.isin == HDFC_2
    held, _ = sh.rekey([_holding(HDFC_1, date(2026, 10, 15))], _resolver())
    assert (held[0].isin, held[0].isin_as_filed) == (HDFC_3, HDFC_1)


def _seed_lake(root: Path) -> None:
    when = datetime(2026, 10, 1, 17, 0, tzinfo=IST)
    bm.merge_l1(
        [
            _meeting(HDFC_1, disseminated=when, meeting=date(2026, 10, 18)),
            _meeting("INE009A01021", disseminated=when, meeting=date(2026, 10, 16)),
        ],
        data_root=root,
    )
    ann.write_l1(
        ann.AnnouncementBatch(
            logical_date=date(2026, 10, 20),
            source=ann.ANNOUNCEMENTS_DATASET,
            rows=(
                _announcement(HDFC_1, datetime(2026, 10, 20, 9, 0, tzinfo=IST), "a"),
                _announcement("INE009A01021", datetime(2026, 10, 20, 10, 0, tzinfo=IST), "b"),
            ),
        ),
        data_root=root,
    )
    sh.write_l1(
        sh.ShareholdingSnapshot(source=sh.SOURCE_ID, rows=(_holding(HDFC_1, date(2026, 10, 15)),)),
        data_root=root,
    )


def _digests(root: Path) -> dict[str, str]:
    return {
        str(p.relative_to(root)): hashlib.sha256(p.read_bytes()).hexdigest()
        for p in sorted((root / "L1").rglob("*.parquet"))
    }


def test_the_lake_rekey_is_deterministic_idempotent_and_touches_only_the_isin(
    tmp_path: Path,
) -> None:
    """Replay determinism: two lakes re-keyed over the same lineage are byte-identical, and a
    second run over either rewrites nothing."""
    one, two = tmp_path / "one", tmp_path / "two"
    _seed_lake(one)
    _seed_lake(two)
    before = {
        name: [r.model_dump(exclude={"isin", "isin_as_filed"}) for r in rows]
        for name, rows in (
            ("bm", bm.read_l1(date(2026, 10, 1), data_root=one)),
            ("ann", ann.read_l1(date(2026, 10, 20), data_root=one).rows),
            ("sh", sh.read_l1(date(2026, 10, 15), data_root=one)),
        )
    }
    reports = {r.dataset: r for r in rekey_lake(_resolver(), data_root=one)}
    rekey_lake(_resolver(), data_root=two)
    assert _digests(one) == _digests(two)
    assert {k: (r.rewritten, r.tally.by_status.get("rekeyed")) for k, r in reports.items()} == {
        "board_meetings": (1, 1),
        "announcements": (1, 1),
        "shareholding": (1, 1),
    }
    first = _digests(one)
    again = rekey_lake(_resolver(), data_root=one)
    assert all(r.rewritten == 0 and r.tally.changed == 0 for r in again)
    assert _digests(one) == first

    (meeting_hdfc,) = [
        r for r in bm.read_l1(date(2026, 10, 1), data_root=one) if r.filed_isin == HDFC_1
    ]
    assert meeting_hdfc.isin == HDFC_3
    after_ann = ann.read_l1(date(2026, 10, 20), data_root=one).rows
    assert {r.isin for r in after_ann} == {HDFC_3, "INE009A01021"}
    assert {r.isin_as_filed for r in after_ann} == {HDFC_1, "INE009A01021"}
    after = {
        "bm": [
            r.model_dump(exclude={"isin", "isin_as_filed"})
            for r in bm.read_l1(date(2026, 10, 1), data_root=one)
        ],
        "ann": sorted(
            (r.model_dump(exclude={"isin", "isin_as_filed"}) for r in after_ann),
            key=lambda d: d["source_ref"],
        ),
        "sh": [
            r.model_dump(exclude={"isin", "isin_as_filed"})
            for r in sh.read_l1(date(2026, 10, 15), data_root=one)
        ],
    }
    before["ann"] = sorted(before["ann"], key=lambda d: d["source_ref"])
    assert after == before


def test_a_dry_run_counts_and_writes_nothing(tmp_path: Path) -> None:
    _seed_lake(tmp_path)
    first = _digests(tmp_path)
    reports = rekey_lake(_resolver(), data_root=tmp_path, dry_run=True)
    assert sum(r.tally.by_status.get("rekeyed", 0) for r in reports) == 3
    assert _digests(tmp_path) == first


@pytest.mark.parametrize("days_before", [1, 30])
def test_the_lake_rekey_never_moves_a_pre_reissue_filing(tmp_path: Path, days_before: int) -> None:
    """PIT through the whole path: a filing knowable before the successor existed stays put."""
    reissue = date(2026, 10, 28)
    a, b = "INE700A01011", "INE700A01029"
    when = datetime.combine(reissue - timedelta(days=days_before), datetime.min.time(), IST)
    when = when.replace(hour=12)
    bm.merge_l1([_meeting(a, disseminated=when, meeting=date(2026, 11, 5))], data_root=tmp_path)
    rekey_lake(FilingIsinResolver([_edge(a, b, reissue)]), data_root=tmp_path)
    (row,) = bm.read_l1(when.date(), data_root=tmp_path)
    assert (row.isin, row.isin_as_filed) == (a, a)
