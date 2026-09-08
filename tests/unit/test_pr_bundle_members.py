"""`Ix` index membership and `mcap` market cap — what they carry, and what they do not (W2).

The two tests that matter most here are the ones that pin a *negative* result, because both
members were fetched on a hypothesis that the measurement then narrowed:

* `test_ix_is_intermittent_not_a_daily_complete_snapshot` — `Ix` carries five indices in January
  2010 and one in April and July. It is not a membership series.
* `test_mcap_carries_no_delisting_category` — `Category` is only ever `Listed` or `Permitted`, and
  `Last Trade Date` is an illiquidity marker. `mcap` is not a delisting-event source.

Both would be easy to quietly forget once the code is written, and both change what a later task
should spend requests on. They are asserted so the conclusion is checked, not remembered.

Offline and deterministic (B8): every byte read here comes from `tests/fixtures/nse_pr_bundle/`.
"""

from __future__ import annotations

from datetime import date
from decimal import Decimal
from pathlib import Path
from typing import Final

import pytest

from dataplatform.ingest.models import ParseError
from dataplatform.ingest.nse import pr_bundle
from dataplatform.ingest.nse.pr_bundle.bundle import MemberKind, PrBundle
from dataplatform.ingest.nse.pr_bundle.ix import parse_ix, parse_ix_bundle
from dataplatform.ingest.nse.pr_bundle.mcap import MCAP_COLUMNS, parse_mcap, parse_mcap_bundle

FIXTURES: Final = Path(__file__).resolve().parents[1] / "fixtures" / "nse_pr_bundle"


def _bundle(era: str, name: str) -> PrBundle:
    return PrBundle((FIXTURES / era / name).read_bytes(), filename=name)


# ── Ix ───────────────────────────────────────────────────────────────────────────────────────


def test_ix_parses_the_2010_membership_with_weights() -> None:
    with _bundle("ix_era", "PR040110.zip") as bundle:
        parsed = parse_ix_bundle(bundle)

    assert parsed.publication_date == date(2010, 1, 4)
    assert parsed.indices == (
        "BANK Nifty",
        "CNX IT",
        "CNX 500",
        "CNX Midcap",
        "Nifty Midcap 50",
    )
    banks = parsed.constituents("BANK Nifty")
    assert len(banks) == 12
    sbi = next(row for row in banks if row.symbol == "SBIN")
    assert sbi.series == "EQ"
    assert sbi.issue_cap == 634880222
    assert sbi.close_price == Decimal("2291.20")
    assert sbi.market_cap == Decimal("1454637564646.40")
    assert sbi.weightage == Decimal("29.14")


def test_ix_is_intermittent_not_a_daily_complete_snapshot() -> None:
    """The index *set* varies over 2010 — measured 2026-09-08 across eleven probes.

    2010-01-04..08 carry five indices; 2010-04-08, 2010-04-09 and 2010-07-01 carry only CNX 500;
    2010-10-01 and 2010-10-04 carry CNX 500 and CNX Infrastructure; from 2010-10-18 the member is
    gone entirely and never returns. Nine months of a varying, incomplete index set is a set of
    dated anchor points to validate a reconstruction against — not a constituent history.
    """
    with _bundle("ix_era", "PR040110.zip") as bundle:
        parsed = parse_ix_bundle(bundle)

    assert len(parsed.indices) == 5
    assert "CNX 500" in parsed.indices
    assert "NIFTY 50" not in parsed.indices, (
        "the headline index is not in this member; membership for it is not reconstructible here"
    )


def test_ix_money_is_decimal_never_float() -> None:
    with _bundle("ix_era", "PR040110.zip") as bundle:
        parsed = parse_ix_bundle(bundle)
    for row in parsed.rows[:50]:
        assert isinstance(row.close_price, Decimal)
        assert isinstance(row.market_cap, Decimal)
        assert isinstance(row.weightage, Decimal)
        assert isinstance(row.issue_cap, int)


def test_ix_rows_carry_no_isin_field() -> None:
    """2010 symbols belonging to since-delisted companies; a current listing would be biased."""
    assert "isin" not in pr_bundle.IxRow.model_fields


def test_ix_banner_lines_do_not_become_constituents() -> None:
    """The `' , , ,BANK Nifty, , , , '` lines announce an index; they are not securities."""
    with _bundle("ix_era", "PR040110.zip") as bundle:
        parsed = parse_ix_bundle(bundle)
    assert all(row.symbol.strip() for row in parsed.rows)
    assert all(row.index_name.strip() for row in parsed.rows)
    assert len(parsed.rows) == 12 + 20 + 500 + 100 + 50


def test_ix_rejects_a_wrong_header() -> None:
    with pytest.raises(ParseError, match="unexpected header"):
        parse_ix(b"A,B,C\n1,2,3\n", filename="Ix010110.csv", publication_date=date(2010, 1, 1))


# ── mcap ─────────────────────────────────────────────────────────────────────────────────────


def test_mcap_parses_issue_size_and_market_cap() -> None:
    with _bundle("lowercase", "PR040926.zip") as bundle:
        parsed = parse_mcap_bundle(bundle)

    assert parsed.publication_date == date(2026, 9, 4)
    row = next(r for r in parsed.rows if r.symbol == "20MICRONS")
    assert row.trade_date == date(2026, 9, 4)
    assert row.series == "EQ"
    assert row.category == "Listed"
    assert row.face_value == Decimal("5.00")
    assert row.issue_size == 35286502
    assert row.close_price == Decimal("222.50")
    assert row.market_cap == Decimal("7850893830.00")
    assert row.last_trade_date == date(2026, 9, 4)


def test_mcap_carries_no_delisting_category() -> None:
    """W2 opened on the hypothesis that `mcap` dates delistings. It does not.

    `Category` takes only `Listed` and `Permitted`; a delisted security stops appearing rather
    than being marked. The only delisting signal here is disappearance, which needs the full daily
    series to observe and is a much weaker claim than a dated event.
    """
    with _bundle("lowercase", "PR040926.zip") as bundle:
        parsed = parse_mcap_bundle(bundle)

    categories = {row.category for row in parsed.rows}
    assert categories == {"Listed", "Permitted"}
    assert not any("delist" in c.lower() for c in categories)
    assert not any("suspend" in c.lower() for c in categories)


def test_mcap_last_trade_date_is_an_illiquidity_marker() -> None:
    """Stale rows exist and are ordinary thin trading, not terminal events."""
    with _bundle("lowercase", "PR040926.zip") as bundle:
        parsed = parse_mcap_bundle(bundle)

    stale = [r for r in parsed.rows if not r.traded_on_trade_date and not r.never_traded]
    assert stale, "the 2026-09-04 file carries securities that did not trade that day"
    assert all(r.last_trade_date is not None and r.last_trade_date < r.trade_date for r in stale)
    assert all(r.category in {"Listed", "Permitted"} for r in stale), (
        "a stale last-trade-date does not change a security's category"
    )


def test_mcap_not_traded_is_a_published_sentinel_not_a_missing_value() -> None:
    """3-6 rows of every `mcap` file carry the literal `Not Traded` in `Last Trade Date`.

    It means the security has never traded, which is a different fact from a stale date and from
    an absent one — an empty `Last Trade Date` never occurs in any file measured.
    """
    with _bundle("mcap_upper", "PR010724.zip") as bundle:
        parsed = parse_mcap_bundle(bundle)

    never = [r for r in parsed.rows if r.never_traded]
    assert len(never) == 5, "measured: five such rows in MCAP01072024.csv"
    assert all(r.last_trade_date is None for r in never)
    assert all(not r.traded_on_trade_date for r in never)


def test_mcap_trailing_subtotals_are_separated_from_securities() -> None:
    """The file ends with `Listed`, `Permitted` and `Total` lines carrying only a market cap."""
    with _bundle("lowercase", "PR040926.zip") as bundle:
        parsed = parse_mcap_bundle(bundle)

    assert set(parsed.totals) == {"Listed", "Permitted", "Total"}
    assert parsed.total_market_cap == Decimal("487808779620881.40")
    assert not any(r.symbol in {"Total", "Listed", "Permitted"} for r in parsed.rows)

    # The rows we parsed must reconstruct the exchange's own total — but only to the rupee, not
    # to the paisa. NSE's file does not tie to itself: measured 2026-09-08, its published
    # `Listed` + `Permitted` subtotals miss its own `Total` by Rs 0.05 here and Rs 0.08 in the
    # 2024 file, and our row sum sits Rs 0.10-0.34 above the published `Total`. That is the
    # exchange rounding each line independently, on a base of Rs 4.9e14 — one part in 5e15. A
    # tolerance is the honest assertion; an exact tie is not available from this source. The
    # check still bites: a dropped or double-counted security moves this by billions.
    total = parsed.total_market_cap
    assert total is not None
    assert abs(parsed.summed_market_cap - total) < Decimal("1.00")


def test_mcap_survives_the_casing_cutover() -> None:
    """`MCAP01072024.csv` and `mcap04092026.csv` have identical headers; only the name changed."""
    with _bundle("mcap_upper", "PR010724.zip") as bundle:
        old = parse_mcap_bundle(bundle)
        assert bundle.member(MemberKind.MCAP) is not None
    with _bundle("lowercase", "PR040926.zip") as bundle:
        new = parse_mcap_bundle(bundle)

    assert old.publication_date == date(2024, 7, 1)
    assert new.publication_date == date(2026, 9, 4)
    assert old.rows and new.rows
    assert len(new.rows) > len(old.rows), "the universe grew between 2024 and 2026"


def test_mcap_money_is_decimal_never_float() -> None:
    with _bundle("mcap_upper", "PR010724.zip") as bundle:
        parsed = parse_mcap_bundle(bundle)
    for row in parsed.rows[:50]:
        assert isinstance(row.face_value, Decimal)
        assert isinstance(row.close_price, Decimal)
        assert isinstance(row.market_cap, Decimal)
        assert isinstance(row.issue_size, int)


def test_mcap_rows_carry_no_isin_field() -> None:
    assert "isin" not in pr_bundle.McapRow.model_fields


def test_mcap_rejects_a_wrong_header() -> None:
    with pytest.raises(ParseError, match="unexpected header"):
        parse_mcap(b"A,B\n1,2\n", filename="mcap01012020.csv", publication_date=date(2020, 1, 1))


def test_mcap_rejects_an_unreadable_date() -> None:
    header = ",".join(MCAP_COLUMNS)
    body = "2020-01-01,FOO,EQ,Foo Ltd,Listed,01 JAN 2020,10.00,100,1.00,100.00"
    with pytest.raises(ParseError, match="not a DD MMM YYYY date"):
        parse_mcap(
            f"{header}\n{body}\n".encode(),
            filename="mcap01012020.csv",
            publication_date=date(2020, 1, 1),
        )
