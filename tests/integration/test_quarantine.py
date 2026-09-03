"""M7.2 — restated-data quarantine enforcement (invariant #8, §7; §8.3's PIT leak test extended).

Invariant #8 says restated (Screener) fundamentals are monitoring-only and *physically unreachable*
from a backtest or a decision. M7.1 put restated data in its own store root and M4.3 gave the PIT
read its `knowable_date` leak guard; this test proves the third leg the spec asks for — that the
PIT/backtest *query context* has no reachable path to the restated store, structurally, and that a
deliberate attempt to read it raises.

The file is organised by the task's two acceptance criteria:

* **Acceptance 1 — a deliberate attempt to read restated data from a PIT/backtest context raises.**
  A `backtest_catalog` registers no restated view and constructs no restated reader, so every way of
  asking it for restated data — the reader capability, the convenience read, and a hand-written SQL
  query naming the restated view — fails, while the same asks succeed on a `monitoring_catalog`. The
  restated data is on disk the whole time (written before both catalogs open), so the backtest's
  empty reach is quarantine, not absence of data. This is §8.3's PIT leak test extended from
  "future data cannot reach a decision" to "restated data cannot reach a backtest."

* **Acceptance 2 — monitoring (T1/T2) can read it, and the journal records which store each datum
  came from.** The monitoring catalog reads the restated figures back as provenance-tagged data, and
  a T2 evidence bundle built from them records the `RESTATED` store on every item — so a number that
  is restated is labelled as restated wherever it surfaces (§5.7's evidence pack).

Offline and deterministic: every byte read here is written into `tmp_path` by the test. Nothing
touches the network or a database.
"""

from __future__ import annotations

from datetime import date, datetime
from decimal import Decimal
from pathlib import Path
from typing import Final

import duckdb
import pytest

from analyst.journal.evidence import EvidenceBundle, EvidenceItem, EvidenceKind, EvidenceStore
from analyst.journal.models import Actor
from dataplatform.clock import IST
from dataplatform.ingest.screener import SCREENER_SOURCE_ID, SCREENER_SOURCE_TAG
from dataplatform.ingest.xbrl.models import Filing, FundamentalFact, Nature, Taxonomy
from dataplatform.query.quarantine import (
    PIT_FUNDAMENTALS_VIEW,
    RESTATED_FUNDAMENTALS_VIEW,
    ProvenancedFundamental,
    QuarantineError,
    QueryContext,
    backtest_catalog,
    monitoring_catalog,
)
from dataplatform.store.pit_fundamentals import write_pit
from dataplatform.store.restated import (
    RESTATED_ROOT_NAME,
    RestatedFundamental,
    RestatedStore,
)

RELIANCE_ISIN: Final = "INE002A01018"
FETCHED_AT: Final = datetime(2026, 8, 7, 19, 30, tzinfo=IST)
L0_LOGICAL_DATE: Final = date(2026, 8, 7)
L0_SHA256: Final = "a" * 64


# ── fixtures: both stores populated under one scratch lake ───────────────────────────────────────


def _restated_row(metric: str, value: Decimal) -> RestatedFundamental:
    """One restated RELIANCE figure with a full L0 lineage — never to reach a backtest."""
    return RestatedFundamental(
        isin=RELIANCE_ISIN,
        symbol="RELIANCE",
        statement="profit_loss",
        metric=metric,
        period="Mar 2024",
        value=value,
        source=SCREENER_SOURCE_TAG,
        l0_source=SCREENER_SOURCE_ID,
        l0_filename="RELIANCE.html",
        l0_logical_date=L0_LOGICAL_DATE,
        l0_sha256=L0_SHA256,
        fetched_at=FETCHED_AT,
    )


def _restated_rows() -> tuple[RestatedFundamental, ...]:
    """Two restated figures for RELIANCE — the numbers that must never reach a backtest."""
    return (
        _restated_row("Sales", Decimal("900000.00")),
        _restated_row("Net Profit", Decimal("60000.00")),
    )


def _pit_filing() -> Filing:
    """A true point-in-time filing for the same ISIN — the backtest-safe fundamentals."""
    period_end = date(2024, 3, 31)
    filing_date = date(2024, 5, 3)
    fact = FundamentalFact(
        isin=RELIANCE_ISIN,
        period_end=period_end,
        filing_date=filing_date,
        nature=Nature.STANDALONE,
        filing_id="RELIANCE-2024Q4-STD",
        concept="revenue_from_operations",
        segment=None,
        value=Decimal("905000.00"),
        source="nse_filings",
        l0_key="nse_filings/2024-05-03/RELIANCE.xml",
    )
    return Filing(
        isin=RELIANCE_ISIN,
        symbol="RELIANCE",
        taxonomy=Taxonomy.IND_AS,
        name="Reliance Industries",
        period_end=period_end,
        filing_date=filing_date,
        nature=Nature.STANDALONE,
        filing_id="RELIANCE-2024Q4-STD",
        source="nse_filings",
        l0_key="nse_filings/2024-05-03/RELIANCE.xml",
        facts=(fact,),
    )


@pytest.fixture
def lake(tmp_path: Path) -> Path:
    """A scratch lake with both stores populated: restated (RESTATED/…) and PIT (L1/…)."""
    RestatedStore(data_root=tmp_path).write(_restated_rows())
    write_pit(_pit_filing(), data_root=tmp_path)
    return tmp_path


# ── acceptance 1: a deliberate attempt to read restated data from a backtest context raises ───────


def test_backtest_catalog_has_no_restated_view_but_has_pit(lake: Path) -> None:
    """The catalogs differ by exactly the restated view — invariant #8 as a catalog fact."""
    with backtest_catalog(data_root=lake) as backtest:
        assert backtest.context is QueryContext.BACKTEST
        assert PIT_FUNDAMENTALS_VIEW in backtest.views
        assert RESTATED_FUNDAMENTALS_VIEW not in backtest.views


def test_restated_reader_capability_is_absent_from_a_backtest(lake: Path) -> None:
    """Asking a backtest for the restated reader raises: the capability was never constructed."""
    with backtest_catalog(data_root=lake) as backtest, pytest.raises(QuarantineError) as exc:
        backtest.restated()
    assert "restated" in str(exc.value)


def test_deliberate_restated_read_from_a_backtest_raises(lake: Path) -> None:
    """The convenience read raises too — there is no receiver, not a late-checked guard."""
    with backtest_catalog(data_root=lake) as backtest, pytest.raises(QuarantineError) as exc:
        backtest.read_restated(RELIANCE_ISIN)
    assert "monitoring-only" in str(exc.value)


def test_backtest_sql_cannot_name_the_restated_view(lake: Path) -> None:
    """Even a hand-written SQL query cannot reach restated data — the view is not in the catalog.

    This is the "separate connection/catalog" guarantee at the SQL layer: the restated view is not
    registered on a backtest connection, so DuckDB raises a catalog error naming an unknown table.
    """
    with backtest_catalog(data_root=lake) as backtest, pytest.raises(duckdb.Error):
        backtest.sql(f'SELECT count(*) FROM "{RESTATED_FUNDAMENTALS_VIEW}"')


def test_backtest_can_still_read_the_pit_store(lake: Path) -> None:
    """The quarantine removes only the restated path; PIT fundamentals remain fully readable."""
    with backtest_catalog(data_root=lake) as backtest:
        rows = backtest.sql(f'SELECT count(*) FROM "{PIT_FUNDAMENTALS_VIEW}"')
        assert rows[0][0] == 1  # the one filing fact written


def test_the_restated_data_exists_on_disk_so_the_empty_reach_is_quarantine(lake: Path) -> None:
    """A control: the restated figures ARE on disk — the backtest's inability to see them is the
    quarantine, not simply an empty store. Read directly through the store to prove they exist."""
    stored = RestatedStore(data_root=lake).read_isin(RELIANCE_ISIN)
    assert len(stored) == 2
    # And yet a backtest, pointed at the very same lake, cannot reach them.
    with backtest_catalog(data_root=lake) as backtest, pytest.raises(QuarantineError):
        backtest.read_restated(RELIANCE_ISIN)


# ── acceptance 2: monitoring can read it, and the journal records each datum's store ──────────


def test_monitoring_catalog_registers_both_views(lake: Path) -> None:
    with monitoring_catalog(data_root=lake) as monitoring:
        assert monitoring.context is QueryContext.MONITORING
        assert PIT_FUNDAMENTALS_VIEW in monitoring.views
        assert RESTATED_FUNDAMENTALS_VIEW in monitoring.views


def test_monitoring_reads_restated_with_store_provenance(lake: Path) -> None:
    """T1/T2 monitoring reads the restated figures, each tagged with the store it came from."""
    with monitoring_catalog(data_root=lake) as monitoring:
        data = monitoring.read_restated(RELIANCE_ISIN)
    assert len(data) == 2
    assert all(isinstance(d, ProvenancedFundamental) for d in data)
    assert all(d.store == RESTATED_ROOT_NAME for d in data)
    assert all(d.source == SCREENER_SOURCE_TAG for d in data)
    assert all(isinstance(d.value, Decimal) for d in data)
    by_metric = {d.metric: d for d in data}
    assert by_metric["Sales"].value == Decimal("900000.00")
    # The L0 lineage travels with the datum, so the journal can name the bytes it derives from.
    assert (
        by_metric["Sales"].l0_key
        == f"{SCREENER_SOURCE_ID}/{L0_LOGICAL_DATE.isoformat()}/RELIANCE.html"
    )


def test_monitoring_sql_can_read_the_restated_view(lake: Path) -> None:
    """The restated view is genuinely registered on the monitoring connection (symmetry check)."""
    with monitoring_catalog(data_root=lake) as monitoring:
        rows = monitoring.sql(f'SELECT count(*) FROM "{RESTATED_FUNDAMENTALS_VIEW}"')
        assert rows[0][0] == 2


def test_journal_records_which_store_each_datum_came_from(lake: Path) -> None:
    """A T2 evidence bundle built from monitoring's restated reads records the store on every item.

    This is acceptance 2's second half: a restated number that reaches a monitoring evidence bundle
    is labelled with the store it came from, so the journal (and the §5.7 evidence pack) can tell a
    restated figure apart from a PIT one after the fact. The bundle is stored content-addressed and
    read back, proving the provenance survives the round-trip.
    """
    with monitoring_catalog(data_root=lake) as monitoring:
        data = monitoring.read_restated(RELIANCE_ISIN)

    items = tuple(
        EvidenceItem(
            kind=EvidenceKind.FUNDAMENTAL,
            source=datum.store,  # the store this datum came from — recorded on the journal item
            label=f"{datum.statement}.{datum.metric}.{datum.period}",
            isin=datum.isin,
            value=datum.value,
            detail=dict(datum.as_evidence_fields()),
        )
        for datum in data
    )
    bundle = EvidenceBundle(trading_date=date(2026, 8, 7), actor=Actor.T2, items=items)

    store = EvidenceStore(root=lake / "evidence")
    ref = store.put(bundle)
    reloaded = store.load(ref)

    assert len(reloaded.items) == 2
    # Every fundamental item names the RESTATED store, as its source and in its provenance detail.
    for item in reloaded.items:
        assert item.kind is EvidenceKind.FUNDAMENTAL
        assert item.source == RESTATED_ROOT_NAME
        assert item.detail["store"] == RESTATED_ROOT_NAME
        assert item.detail["source"] == SCREENER_SOURCE_TAG


def test_a_pit_and_a_restated_figure_are_distinguishable_by_store(lake: Path) -> None:
    """The point of recording the store: the same company's PIT and restated revenue are told apart.

    A monitoring bundle can legitimately carry both a PIT filing figure and a restated one; the
    store label is what keeps the restated number from being mistaken for point-in-time knowledge.
    """
    with monitoring_catalog(data_root=lake) as monitoring:
        restated = monitoring.read_restated(RELIANCE_ISIN)
        pit_rows = monitoring.sql(
            f'SELECT source, value FROM "{PIT_FUNDAMENTALS_VIEW}" '
            "WHERE concept = 'revenue_from_operations'"
        )

    # The restated revenue and the PIT revenue are both readable by monitoring, and each is
    # attributable to its own store: the restated figure to RESTATED, the PIT figure to the filing.
    restated_stores = {d.store for d in restated}
    assert restated_stores == {RESTATED_ROOT_NAME}
    pit_source, pit_value = pit_rows[0]
    assert pit_source == "nse_filings"  # the PIT figure names its filing store, not RESTATED
    restated_sales = next(d.value for d in restated if d.metric == "Sales")
    assert restated_sales != pit_value  # different numbers, told apart by their store provenance
