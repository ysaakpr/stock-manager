"""D34-D39: the per-filing XBRL failures the owner accepted on 2026-11-02, and nothing beside them.

Each entry accepts its own recorded evidence: D34 a fixed group of filings (unit, filing date and
error shape together), D35-D38 an error shape bounded by the filing dates it was seen on, D39 an
old index error only where the same document landed under its other entry. Every failure class
the triage did not clear — the quarter labelled as a half-year (A), the identity refusals (E, F1,
F2, G), the MSEI scheme (H), the old half-year parses (M), the never-fetched payloads — stays an
error under every entry, even with every document published. The messages are verbatim from the
live `sync_state` rows (2026-11-02).
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import date, datetime
from pathlib import Path
from typing import Any, Final, cast

import pytest

from dataplatform.clock import IST
from dataplatform.ingest.calendar import DayKind
from dataplatform.quality.gaps import GapEntry, GapReason, GapReport
from dataplatform.status.deviations import (
    DeviationJudge,
    DeviationRegistryError,
    GapItem,
    document_name,
    gap_matches,
    load_deviations,
)
from dataplatform.status.models import GapsOut
from dataplatform.status.sync_state import SyncStateStore
from dataplatform.store.db import Connection

REGISTRY: Final = load_deviations()
TODAY: Final = date(2026, 11, 2)
LONG_AGO: Final = datetime(2026, 9, 6, 16, 19, tzinfo=IST)

_404: Final = (
    "FetchHTTPError: nse_xbrl_filing: https://nsearchives.nseindia.com/corporate/xbrl/"
    "INTEGRATED_FILING_INDAS_{}_WEB.xml returned 404, which no retry would change"
)
UPL_404: Final = _404.format("1442647_19052025120108")
HCG_404: Final = _404.format("1500429_01082025060000")
LAURUS_TRUNCATED: Final = (
    "parse failed: INTEGRATED_FILING_INDAS_1555947_23102025113639_WEB.xml: body is not "
    "well-formed XML: unclosed token: line 1, column 65227"
)


def _no_column(doc: str, ask: str, unit: str, offers: str, *, new: bool = True) -> str:
    """The parser's period refusal, in the shape its writer used (new: since 2025)."""
    end = ask.split("→")[1]
    either = f", and no single column ends on {end} either" if new else ""
    return (
        f"parse failed: {doc}: no results column covers {ask} as the index entry ({unit}) "
        f"says{either}; the document offers {offers}"
    )


# ── verbatim live failures, by the triage's class ─────────────────────────────────────────

#: class B: synthetic `-FY` entries of companies whose year ends in Dec, Jun or Sep.
B_ACCELYA: Final = _no_column(
    "INTEGRATED_FILING_INDAS_1424228_24042025072900_WEB.xml", "2024-04-01→2025-03-31",
    "IF86623-FY",
    "OneD=2025-01-01→2025-03-31/Standalone, FourD=2024-07-01→2025-03-31/Standalone",
)  # fmt: skip
B_FOSECO: Final = _no_column(
    "INTEGRATED_FILING_INDAS_1666142_12052026071724_WEB.xml", "2025-04-01→2026-03-31",
    "IF157007-FY", "OneD=2026-01-01→2026-03-31/Standalone",
)  # fmt: skip
B_SIEMENS: Final = _no_column(
    "INTEGRATED_FILING_INDAS_1437393_13052025050510_WEB.xml", "2024-04-01→2025-03-31",
    "IF90486-FY",
    "OneD=2024-01-01→2025-03-31/Standalone, FourD=2024-10-01→2025-03-31/Standalone",
)  # fmt: skip
B_PGHH: Final = _no_column(
    "INTEGRATED_FILING_INDAS_1467565_17062025035157_WEB.xml", "2024-04-01→2025-03-31",
    "IF98435-FY",
    "OneD=2024-10-01→2025-03-31/Standalone, FourD=2024-07-01→2025-03-31/Standalone",
)  # fmt: skip
#: ICICIPRULI's -FY: its FY column is stated with day and month swapped (2025-01-04 = 1 April).
#: A date typo (class C), not a non-March year.
C_ICICIPRULI_FY: Final = _no_column(
    "INTEGRATED_FILING_LI_1650890_14042026065512_WEB.xml", "2025-04-01→2026-03-31",
    "IF150750-FY",
    "OneD=2026-01-01→2026-03-31/Consolidated, FourD=2025-01-04→2026-03-31/Consolidated",
)  # fmt: skip
C_SIEMENS_Q4: Final = _no_column(
    "INTEGRATED_FILING_INDAS_1437366_13052025045441_WEB.xml", "2025-01-01→2025-03-31", "IF90482",
    "OneD=2024-01-01→2025-03-31/Consolidated, FourD=2024-10-01→2025-03-31/Consolidated",
)  # fmt: skip
#: class A: a Q4 document whose quarter column is labelled as the Oct-Mar half-year.
A_LTTS: Final = _no_column(
    "INTEGRATED_FILING_INDAS_1424301_24042025091235_WEB.xml", "2025-01-01→2025-03-31", "IF86656",
    "OneD=2024-10-01→2025-03-31/Standalone, FourD=2024-04-01→2025-03-31/Standalone",
)  # fmt: skip
#: class D: the document reports another period.
D_CGPOWER: Final = _no_column(
    "INTEGRATED_FILING_INDAS_1434119_08052025061546_WEB.xml", "2025-01-01→2025-03-31", "IF89776",
    "OneD=2024-04-01→2024-09-30/Standalone",
)  # fmt: skip
D_AARTIDRUGS: Final = _no_column(
    "INTEGRATED_FILING_INDAS_1487066_18072025063655_WEB.xml", "2025-04-01→2025-06-30", "IF103986",
    "OneD=2024-04-01→2024-06-30/Consolidated",
)  # fmt: skip
D_VENKEYS_FY: Final = _no_column(
    "INTEGRATED_FILING_INDAS_1527650_16092025121824_WEB.xml", "2024-04-01→2025-03-31",
    "IF115186-FY", "OneD=2025-04-01→2025-06-30/Standalone",
)  # fmt: skip
#: class F3: Dhunseri Tea's document filed under Dhunseri Ventures.
F3_DTIL: Final = (
    "parse failed: INDAS_35436_10399_22052018123437_WEB.xml: index says this filing is 'DVL' "
    "(ISIN INE477B01010, symbols accepted on 2018-05-26: DPL, DVL) but the document says DTIL; "
    "the announcements index and the XBRL must name the same company"
)
#: class I: paid-up capital in scientific notation.
I_LT: Final = (
    "parse failed: INTEGRATED_FILING_INDAS_1663898_05052026094858_WEB.xml: "
    "paid_up_equity_capital/None value '1.37563219E+16' is not a plain decimal"
)
#: class K: the 2018-2020 feed's index errors (period stale, old two-quarter layout).
K_STALE_YEAR_DOC: Final = "INDAS_35213_9682_16052018061608_WEB.xml"
K_STALE_YEAR: Final = _no_column(
    K_STALE_YEAR_DOC, "2016-04-01→2017-03-31", "1040496",
    "OneD=2018-01-01→2018-03-31/Standalone, FourD=2017-04-01→2018-03-31/Standalone", new=False,
)  # fmt: skip
K_OLD_LAYOUT: Final = _no_column(
    "INDAS_35652_11308_24052018094926_WEB.xml", "2017-04-01→2018-03-31", "1042004",
    "OneD=2018-01-01→2018-03-31/Standalone, FourD=2017-01-01→2017-03-31/Standalone", new=False,
)  # fmt: skip
K_QUARTER_BEHIND: Final = _no_column(
    "INDAS_39687_55968_29102018074157_WEB_2.xml", "2018-04-01→2018-06-30", "1050463",
    "OneD=2018-07-01→2018-09-30/Consolidated, FourD=2018-04-01→2018-09-30/Consolidated",
    new=False,
)  # fmt: skip
K_QUARTER_BEHIND_Q3: Final = _no_column(
    "INDAS_41990_78987_31012019053738_WEB.xml", "2018-07-01→2018-09-30", "1055464",
    "OneD=2018-10-01→2018-12-31/Standalone, FourD=2018-04-01→2018-12-31/Standalone", new=False,
)  # fmt: skip
#: class L: an annual-only document under a Quarterly entry.
L_DOC: Final = "BANKING_92113_847047_19052023053402.xml"
L_ANNUAL_ONLY: Final = _no_column(
    L_DOC, "2023-01-01→2023-03-31", "1147803", "FourD=2022-04-01→2023-03-31/Standalone", new=False
)
#: class M: a Sep quarter today's parser would store as an Apr-Sep half-year. Never accepted.
M_DOC: Final = "NONINDAS_39357_52711_18102018055921_WEB.xml"
M_HALF_YEAR: Final = _no_column(
    M_DOC, "2018-07-01→2018-09-30", "1049750",
    "OneD=2018-04-01→2018-09-30/Standalone, FourD=2018-04-01→2019-03-31/Standalone", new=False,
)  # fmt: skip
E_MULTI_ENTITY: Final = (
    "parse failed: INTEGRATED_FILING_INDAS_1551013_16102025101226_WEB.xml: filing names more "
    "than one entity (544488, VIKRAMSOLR); one results filing is about one company"
)
F1_TYPO: Final = (
    "parse failed: INTEGRATED_FILING_INDAS_1664223_06052026082322_WEB.xml: index says this filing "
    "is 'BAJAJ-AUTO' (ISIN INE917I01010, symbols accepted on 2026-05-06: BAJAJ-AUTO) but the "
    "document says BAJAJAUTO; the announcements index and the XBRL must name the same company"
)
F2_OLD_NAME: Final = (
    "parse failed: INDAS_35930_12141_28052018040812_WEB.xml: index says this filing is "
    "'ZFCVINDIA' (ISIN INE342J01019, symbols accepted on 2018-06-21: ZFCVINDIA) but the document "
    "says WABCOINDIA; the announcements index and the XBRL must name the same company"
)
G_OTHER_ISSUER: Final = (
    "parse failed: INTEGRATED_FILING_INDAS_1460429_30052025084115_WEB.xml: index says this filing "
    "is 'ASHAPURMIN' (ISIN INE348A01023) but the document states ISIN INE569C01020 — a different "
    "issuer; the announcements index and the XBRL must name the same company"
)
G_INDBANK: Final = (
    "parse failed: INTEGRATED_FILING_NBFC_INDAS_1689424_08072026062603_WEB.xml: index says this "
    "filing is 'INDBANK' (ISIN INE841B01017) but the document states ISIN INE841801017 — a "
    "different issuer; the announcements index and the XBRL must name the same company"
)
H_MSEI: Final = (
    "parse failed: INTEGRATED_FILING_INDAS_1666227_12052026091214_WEB.xml: entity identifier "
    "scheme is 'http://www.sebi.gov.in/in-capmkt/MSEISymbol', expected one of "
    "['http://www.nseindia.com/NSESymbol']; this parser reads the identifier as an NSE symbol"
)
MISSING_PAYLOAD: Final = (
    "MissingPayloadError: filing INE792B01012 2017-09-30 Consolidated (1040180): no L0 payload "
    "for INDAS_35122_9315_12052018032943_WEB.xml and --rebuild-from-l0 forbids fetching; the lake "
    "this was rebuilt from is incomplete"
)


# ── a connection that answers the published-documents read ────────────────────────────────


@dataclass
class DocsConn:
    """`sync_state`'s PUBLISHED l0_paths of one source, as `same_document_published` reads them."""

    published: Sequence[str] = ()
    queries: list[tuple[str, tuple[Any, ...]]] = field(default_factory=list)

    def execute(self, sql: str, params: Sequence[Any] = ()) -> _Rows:
        self.queries.append((sql, tuple(params)))
        assert "FROM sync_state" in sql and "state = 'PUBLISHED'" in sql, sql
        return _Rows([(path,) for path in self.published])


@dataclass
class _Rows:
    rows: list[tuple[Any, ...]]

    def fetchall(self) -> list[tuple[Any, ...]]:
        return self.rows


#: Every document above published under some other entry — the most permissive lake.
_NAMED_DOCUMENTS: Final = tuple(
    text.split(": ", 2)[1]  # "parse failed: <document>: ..."
    for text in (
        A_LTTS, C_SIEMENS_Q4, C_ICICIPRULI_FY, E_MULTI_ENTITY, F1_TYPO, F2_OLD_NAME,
        G_OTHER_ISSUER, G_INDBANK, H_MSEI, K_OLD_LAYOUT, K_QUARTER_BEHIND, K_QUARTER_BEHIND_Q3,
    )
)  # fmt: skip
EVERYTHING: Final = tuple(
    f"nse_xbrl_filing/2018-05-25/{name}"
    for name in (K_STALE_YEAR_DOC, L_DOC, M_DOC, *_NAMED_DOCUMENTS)
)


def _judge(conn: DocsConn | None = None, today: date = TODAY) -> DeviationJudge:
    return DeviationJudge(
        REGISTRY,
        cast(Connection, conn if conn is not None else DocsConn(EVERYTHING)),
        cast(SyncStateStore, object()),
        today,
    )


def _gap(
    error: str, on: date, unit: str, *, first: datetime = LONG_AGO, reason: str = "FAILED"
) -> GapItem:
    return GapItem(
        source="nse_xbrl_filing", logical_date=on, reason=reason, last_error=error,
        retryable=True, first_attempt_at=first, unit=unit,
    )  # fmt: skip


def _id(item: GapItem, conn: DocsConn | None = None, today: date = TODAY) -> str | None:
    verdict = _judge(conn, today).gap(item)
    return verdict.id if verdict is not None and verdict.accepted else None


# ── the loader: the two new criteria are validated ────────────────────────────────────────

_BASIS = """
    remark: x
    basis: {agreed_by: owner, agreed_on: 2026-11-02, recorded_in: ["HUMAN_DECISIONS.md D34"]}
    since: 2025-05-19"""


@pytest.mark.parametrize(
    ("match", "message"),
    [
        ("{reasons: [FAILED], units: []}", "non-empty set of sync_state unit names"),
        ("{reasons: [FAILED], units: ['IF91762 OR 1=1']}", "non-empty set of sync_state unit"),
        ("{reasons: [FAILED], same_document_published: true, before: 2021-01-01}", "reads the"),
        (
            "{reasons: [FAILED], same_document_published: true, "
            "errors: [{class: null, pattern: '^parse failed: \\S+\\.xml: '}]}",
            "does not capture the document",
        ),
    ],
)
def test_the_loader_refuses_a_malformed_new_criterion(
    tmp_path: Path, match: str, message: str
) -> None:
    path = tmp_path / "deviations.yaml"
    entry = f"\n  - id: bad-gap\n    kind: gap\n    target: nse_xbrl_filing\n    match: {match}"
    path.write_text(f"version: 1\ndeviations:{entry}{_BASIS}\n", encoding="utf-8")
    with pytest.raises(DeviationRegistryError, match=message):
        load_deviations(path)


def test_a_unit_set_alone_narrows_a_gap_matcher(tmp_path: Path) -> None:
    path = tmp_path / "deviations.yaml"
    entry = (
        "\n  - id: units-only\n    kind: gap\n    target: nse_xbrl_filing"
        "\n    match: {reasons: [FAILED], units: [IF91762]}"
    )
    path.write_text(f"version: 1\ndeviations:{entry}{_BASIS}\n", encoding="utf-8")
    match = load_deviations(path).deviations[0].match
    on = date(2025, 5, 19)
    assert gap_matches(match, _gap(UPL_404, on, "IF91762"), TODAY)  # type: ignore[arg-type]
    for other in ("IF91762-FY", "IF9176", ""):
        assert not gap_matches(match, _gap(UPL_404, on, other), TODAY)  # type: ignore[arg-type]


# ── D34: the filings the 2026-11-02 re-check still could not fetch ───────────────────────

D34_404: Final = (
    ("IF91762", date(2025, 5, 19)),
    ("IF91762-FY", date(2025, 5, 19)),
    ("IF93702", date(2025, 5, 25)),
    ("IF93702-FY", date(2025, 5, 25)),
    ("IF98124", date(2025, 6, 11)),
    ("IF98124-FY", date(2025, 6, 11)),
    ("IF108019", date(2025, 8, 1)),
)


@pytest.mark.parametrize(("unit", "on"), D34_404)
def test_d34_accepts_each_recheck_404_by_unit_date_and_shape(unit: str, on: date) -> None:
    assert _id(_gap(UPL_404, on, unit)) == "d34-xbrl-recheck-404"
    assert _id(_gap(UPL_404, on, unit), today=date(2030, 1, 1)) == "d34-xbrl-recheck-404"


def test_d34_is_the_named_filings_only() -> None:
    on = date(2025, 8, 1)
    assert _id(_gap(HCG_404, on, "IF108019")) == "d34-xbrl-recheck-404"
    # another filing's plain 404 on the same day, the unit on another date, a whole-source row
    assert _id(_gap(HCG_404, on, "IF108020")) is None
    assert _id(_gap(HCG_404, date(2025, 8, 2), "IF108019")) is None
    assert _id(_gap(HCG_404, on, "")) is None
    # the unit failing any other way
    assert _id(_gap(HCG_404.replace("returned 404", "returned 500"), on, "IF108019")) is None
    assert _id(_gap(LAURUS_TRUNCATED, on, "IF108019")) is None
    assert _id(_gap(HCG_404, on, "IF108019", reason="L0_PRESENT_L1_ABSENT")) is None


def test_d34_accepts_the_twice_truncated_laurus_body_and_nothing_else() -> None:
    on = date(2025, 10, 23)
    assert _id(_gap(LAURUS_TRUNCATED, on, "IF121347")) == "d34-xbrl-recheck-truncated"
    # a later retry cut at another byte is still the same short file
    other_cut = LAURUS_TRUNCATED.replace("column 65227", "column 65231")
    assert _id(_gap(other_cut, on, "IF121347")) == "d34-xbrl-recheck-truncated"
    assert _id(_gap(UPL_404, on, "IF121347")) is None
    assert _id(_gap(LAURUS_TRUNCATED, on, "IF121348")) is None
    malformed = LAURUS_TRUNCATED.replace("unclosed token", "mismatched tag")
    assert _id(_gap(malformed, on, "IF121347")) is None
    another_doc = LAURUS_TRUNCATED.replace("1555947", "1555948")
    assert _id(_gap(another_doc, on, "IF121347")) is None


def test_d34_is_listed_before_d28_so_a_retried_row_says_why_it_stays_amber() -> None:
    fresh = datetime(2026, 11, 2, 2, 0, tzinfo=IST)
    item = _gap(LAURUS_TRUNCATED, date(2025, 10, 23), "IF121347", first=fresh)
    assert _id(item) == "d34-xbrl-recheck-truncated"
    elsewhere = _gap(LAURUS_TRUNCATED, date(2025, 10, 23), "IF999999", first=fresh)
    assert _id(elsewhere) == "d28-xbrl-filing-retrying"


# ── D35: the synthetic Apr-Mar entry of a non-March year ─────────────────────────────────


@pytest.mark.parametrize(
    ("error", "on"),
    [
        (B_ACCELYA, date(2025, 4, 24)),  # Jul-Jun year: a Jul-Mar YTD column
        (B_FOSECO, date(2026, 5, 12)),  # Jan-Dec year: the quarter alone
        (B_SIEMENS, date(2025, 5, 13)),  # Oct-Sep year
        (B_PGHH, date(2025, 6, 17)),  # Jul-Jun year
    ],
)
def test_d35_accepts_a_non_march_years_synthetic_fy_entry(error: str, on: date) -> None:
    unit = error.split("index entry (")[1].split(")")[0]
    assert _id(_gap(error, on, unit)) == "d35-xbrl-synthetic-fy-non-march-year"


def test_d35_stays_inside_its_shape_and_window() -> None:
    on = date(2025, 4, 24)
    # inverted: a column starting 1 April would be the year itself, so this is not the cause
    april = B_ACCELYA.replace("FourD=2024-07-01", "FourD=2024-04-01")
    assert _id(_gap(april, on, "IF86623-FY")) is None
    # the same shape on the quarter's own entry (no -FY) is not the synthetic entry
    assert _id(_gap(B_ACCELYA.replace("IF86623-FY", "IF86623"), on, "IF86623")) is None
    # a column ending on another day is a different document
    assert _id(_gap(B_FOSECO.replace("→2026-03-31/", "→2026-06-30/"), on, "IF157007-FY")) is None
    # outside the dates it was seen on: the next season's stay red until reviewed
    for outside in (date(2025, 4, 20), date(2026, 5, 16), date(2027, 5, 12)):
        assert _id(_gap(B_FOSECO, outside, "IF157007-FY")) is None
    # ICICIPRULI's day/month-swapped year (class C) is not a non-March year
    assert _id(_gap(C_ICICIPRULI_FY, date(2026, 4, 14), "IF150750-FY")) is None


# ── D36: the document reports another period ──────────────────────────────────────────────


@pytest.mark.parametrize(
    ("error", "on", "unit"),
    [
        (D_CGPOWER, date(2025, 5, 8), "IF89776"),
        (D_AARTIDRUGS, date(2025, 7, 18), "IF103986"),
        (D_VENKEYS_FY, date(2025, 9, 16), "IF115186-FY"),
    ],
)
def test_d36_accepts_a_document_of_another_period(error: str, on: date, unit: str) -> None:
    assert _id(_gap(error, on, unit)) == "d36-xbrl-wrong-period-document"
    assert _id(_gap(error, date(2025, 9, 17), unit)) is None
    assert _id(_gap(error, date(2025, 5, 4), unit)) is None


def test_d36_needs_the_single_column_to_miss_the_asked_period() -> None:
    on = date(2025, 7, 18)
    # inverted: the one column ends on the asked date — not another period's document
    ends_on_ask = D_AARTIDRUGS.replace("OneD=2024-04-01→2024-06-30", "OneD=2024-04-01→2025-06-30")
    assert _id(_gap(ends_on_ask, on, "IF103986")) is None
    two_columns = D_AARTIDRUGS + ", FourD=2024-04-01→2025-06-30/Consolidated"
    assert _id(_gap(two_columns, on, "IF103986")) is None


# ── D37: Dhunseri Tea's document filed under Dhunseri Ventures ────────────────────────────


def test_d37_accepts_dtil_under_dvl_only() -> None:
    on = date(2018, 5, 26)
    assert _id(_gap(F3_DTIL, on, "1041285")) == "d37-xbrl-dtil-filed-as-dvl"
    assert _id(_gap(F3_DTIL, date(2025, 5, 23), "IF93198-FY")) == "d37-xbrl-dtil-filed-as-dvl"
    assert _id(_gap(F3_DTIL, date(2025, 5, 24), "1041285")) is None
    assert _id(_gap(F3_DTIL.replace("says DTIL", "says DVLX"), on, "1041285")) is None
    assert _id(_gap(F3_DTIL.replace("INE477B01010", "INE477B01011"), on, "1041285")) is None
    assert _id(_gap(F3_DTIL.replace("'DVL'", "'DTIL'"), on, "1041285")) is None


# ── D38: paid-up capital in scientific notation ───────────────────────────────────────────


def test_d38_accepts_the_scientific_notation_seen_and_nothing_else() -> None:
    assert _id(_gap(I_LT, date(2026, 5, 5), "IF155701")) == "d38-xbrl-scientific-notation"
    assert _id(_gap(I_LT, date(2026, 5, 6), "IF155701")) is None
    plain = I_LT.replace("1.37563219E+16", "1,37,563")
    assert _id(_gap(plain, date(2026, 5, 5), "IF155701")) is None
    other_concept = I_LT.replace("paid_up_equity_capital", "revenue_from_operations")
    assert _id(_gap(other_concept, date(2026, 5, 5), "IF155701")) is None


# ── D39: old index errors, only where the same document landed ───────────────────────────


@pytest.mark.parametrize(
    ("error", "on", "unit"),
    [
        (K_STALE_YEAR, date(2018, 5, 25), "1040496"),
        (K_OLD_LAYOUT, date(2018, 5, 25), "1042004"),
        (K_QUARTER_BEHIND, date(2018, 11, 5), "1050463"),
        (K_QUARTER_BEHIND_Q3, date(2019, 2, 1), "1055464"),
    ],
)
def test_d39_accepts_an_old_index_error_whose_document_landed(
    error: str, on: date, unit: str
) -> None:
    assert _id(_gap(error, on, unit)) == "d39-xbrl-old-index-period"
    # inverted: the same failure whose document no other entry published stays red
    assert _id(_gap(error, on, unit), DocsConn()) is None
    assert _id(_gap(error, date(2021, 1, 1), unit)) is None


def test_d39_accepts_an_annual_only_document_whose_year_landed() -> None:
    on = date(2023, 5, 19)
    assert _id(_gap(L_ANNUAL_ONLY, on, "1147803")) == "d39-xbrl-old-annual-only-document"
    assert _id(_gap(L_ANNUAL_ONLY, on, "1147803"), DocsConn()) is None
    assert _id(_gap(L_ANNUAL_ONLY, date(2024, 1, 1), "1147803")) is None


def test_d39_coverage_is_the_named_document_and_a_redownload_of_it() -> None:
    on = date(2018, 5, 25)
    item = _gap(K_STALE_YEAR, on, "1040496")
    stem = K_STALE_YEAR_DOC.removesuffix(".xml")
    redownload = DocsConn([f"nse_xbrl_filing/2018-05-25/{stem}_at20261102T020103.xml"])
    assert _id(item, redownload) == "d39-xbrl-old-index-period"
    neighbour = DocsConn([f"nse_xbrl_filing/2018-05-25/{stem}_2.xml"])
    assert _id(item, neighbour) is None
    assert document_name("nse_xbrl_filing/2025/10/X_WEB_at20261102T020103.xml") == "X_WEB.xml"


def test_d39_reads_the_published_documents_once_and_only_when_needed() -> None:
    conn = DocsConn(EVERYTHING)
    judge = _judge(conn)
    judge.gap(_gap(UPL_404, date(2025, 5, 19), "IF91762"))
    judge.gap(_gap(A_LTTS, date(2025, 4, 24), "IF86656"))
    assert conn.queries == []  # no entry needing coverage got that far
    judge.gap(_gap(K_STALE_YEAR, date(2018, 5, 25), "1040496"))
    judge.gap(_gap(L_ANNUAL_ONLY, date(2023, 5, 19), "1147803"))
    assert len(conn.queries) == 1 and conn.queries[0][1] == ("nse_xbrl_filing",)


def test_d39_without_a_coverage_reader_fails_closed() -> None:
    entry = REGISTRY.get("d39-xbrl-old-index-period")
    item = _gap(K_STALE_YEAR, date(2018, 5, 25), "1040496")
    assert gap_matches(entry.match, item, TODAY, published=lambda s, d: True)  # type: ignore[arg-type]
    assert not gap_matches(entry.match, item, TODAY)  # type: ignore[arg-type]


# ── what the triage did not clear stays red under every entry ─────────────────────────────


@pytest.mark.parametrize(
    ("error", "on", "unit"),
    [
        (A_LTTS, date(2025, 4, 24), "IF86656"),  # A: re-parsed by the reuse-only re-queue
        (C_SIEMENS_Q4, date(2025, 5, 13), "IF90482"),  # C
        (C_ICICIPRULI_FY, date(2026, 4, 14), "IF150750-FY"),  # C, though filed under -FY
        (E_MULTI_ENTITY, date(2025, 10, 16), "IF120292"),  # E
        (F1_TYPO, date(2026, 5, 6), "IF155890"),  # F1
        (F2_OLD_NAME, date(2018, 6, 21), "1042910"),  # F2
        (G_OTHER_ISSUER, date(2025, 5, 30), "IF97119"),  # G
        (G_INDBANK, date(2026, 7, 8), "IF173185"),  # G: re-parsed by the reuse-only re-queue
        (H_MSEI, date(2026, 5, 12), "IF157074-FY"),  # H
        (M_HALF_YEAR, date(2018, 10, 22), "1049750"),  # M: would be mis-stored as a half-year
        (MISSING_PAYLOAD, date(2018, 6, 12), "1040180"),  # never fetched
    ],
)
def test_a_failure_the_triage_did_not_clear_stays_an_error(error: str, on: date, unit: str) -> None:
    assert _id(_gap(error, on, unit), DocsConn(EVERYTHING)) is None


# ── the status API hands the judge the unit ───────────────────────────────────────────────


def test_the_gaps_payload_judges_a_filing_by_its_unit() -> None:
    def entry(unit: str) -> GapEntry:
        return GapEntry(
            source="nse_xbrl_filing", logical_date=date(2025, 5, 19), reason=GapReason.FAILED,
            detail="FAILED", day_kind=DayKind.SESSION, unit=unit, last_error=UPL_404,
            retryable=True, first_attempt_at=LONG_AGO,
        )  # fmt: skip

    report = GapReport(
        from_date=date(2025, 5, 19),
        to_date=date(2025, 5, 19),
        sources=("nse_xbrl_filing",),
        entries=(entry("IF91762"), entry("IF91763")),
        pairs_examined=1,
        complete=0,
    )
    body = GapsOut.of(report, limit=10, judge=_judge())
    verdicts = [e.deviation.id if e.deviation is not None else None for e in body.unexplained]
    assert verdicts == ["d34-xbrl-recheck-404", None]
    assert body.accepted_total == 1 and body.fully_explained is False
