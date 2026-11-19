"""M14.3 (XBRL): owner ruling D44 (2026-11-16) — class F2, a rename D2's history does not record.

NSE's symbol-change file omits some renames, so D2 holds no window of the old symbol for the ISIN:
Shriram EPC's documents say `SHRIRAMEPC` under index entries that now say `SEPC`. A rename is
accepted only on D2 identity evidence effective on the filing date: NSE's own bhavcopy of the last
session on or before it lists the document's symbol, in every series, under exactly the entry's
ISIN (`fundamentals_backfill.rename_evidence_of`, read through `SessionIdentity`). Each rule is
pinned from both sides, so a test fails if its logic is inverted.

Every document is a byte-for-byte copy of a production L0 payload (`tests/fixtures/xbrl/f2/`, B8);
the index page is a verbatim slice of a stored legacy results-index chunk. Offline: no socket, and
the re-queue's fetcher records any request it is asked for.
"""

from __future__ import annotations

import re
import socket
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import date, datetime
from pathlib import Path
from typing import Any, Final, cast

import pyarrow as pa
import pyarrow.parquet as pq
import pytest
import yaml

from dataplatform.clock import IST, FrozenClock
from dataplatform.identity.master import Exchange, IdentityMaster, SymbolWindow
from dataplatform.identity.session import SessionIdentity
from dataplatform.ingest import fundamentals_backfill as fb
from dataplatform.ingest import fundamentals_requeue as rq
from dataplatform.ingest.calendar import trading_calendar
from dataplatform.ingest.models import ParseError
from dataplatform.ingest.pr_bundle_l1 import SessionStatementRow
from dataplatform.ingest.xbrl import parser
from dataplatform.ingest.xbrl.discovery import FilingIndexEntry
from dataplatform.ingest.xbrl.models import Filing, Nature
from dataplatform.status.sync_state import SyncState
from dataplatform.store.l0 import L0Ref, L0Store
from dataplatform.store.paths import l1_partition_path

F2: Final = Path(__file__).resolve().parents[1] / "fixtures" / "xbrl" / "f2"
DEVIATIONS: Final = (
    Path(__file__).resolve().parents[2] / "dataplatform" / "status" / "accepted_deviations.yaml"
)

# SEPC (Shriram EPC renamed), Q1 FY21 standalone and consolidated, filed Wed 2020-09-09.
SEPC: Final = "INE964H01014"
SEPC_STAN_DOC: Final = "INDAS_61151_314900_09092020053421_WEB.xml"
SEPC_CONS_DOC: Final = "INDAS_61152_314905_09092020053712_WEB.xml"
SEPC_FILED: Final = date(2020, 9, 9)
LEGACY_INDEX: Final = "corporates-financial-results_Quarterly_20200701_20200930.json"
LEGACY_KEY: Final = f"nse_financial_results_index/2020-07-01/{LEGACY_INDEX}"
SEPC_UNITS: Final = ("1091297", "1091298")
# ESSENTIA (Integra Essentia): the document says INTEGRA; NSE's bhavcopy does not list it.
ESSENTIA: Final = "INE418N01035"
ESSENTIA_DOC: Final = "INDAS_69687_446036_06052021105028_WEB.xml"
# MOTHERSON 974: a debt-format document whose every identifier is blank.
MOTHERSON: Final = "INE775A01035"
MOTHERSON_DOC: Final = "INDAS_DEBT_647_854550_27052023110003_WEB.xml"
# DTIL under Dhunseri Ventures (D37): D2 holds DTIL as DVL's symbol until 2010.
DVL: Final = "INE477B01010"
DTIL_TODAY: Final = "INE341R01014"
DTIL_DOC: Final = "INDAS_35436_10399_22052018123437_WEB.xml"
# Class G: GILLETTE's document states P&G Hygiene's ISIN.
GILLETTE: Final = "INE322A01010"
PG_HYGIENE: Final = "INE179A01014"
GILLETTE_DOC: Final = "INTEGRATED_FILING_INDAS_1498822_31072025011108_WEB.xml"
ASHAPURMIN: Final = "INE348A01023"
ORIENT_CERATECH: Final = "INE569C01020"
ASHAPURMIN_DOC: Final = "INTEGRATED_FILING_INDAS_1510335_12082025075003_WEB.xml"
INTEGRATED: Final = Path(__file__).resolve().parents[1] / "fixtures" / "xbrl" / "integrated"

F2_REQUEUE: Final = "xbrl-f2-renames-2026-11-20"


@pytest.fixture(autouse=True)
def no_network(monkeypatch: pytest.MonkeyPatch) -> None:
    def forbidden(*args: Any, **kwargs: Any) -> Any:
        raise AssertionError("a test opened a socket; these tests are offline")

    monkeypatch.setattr(socket, "socket", forbidden)
    monkeypatch.setattr(socket, "create_connection", forbidden)


def _entry(
    *,
    isin: str,
    symbol: str,
    start: date,
    end: date,
    filed: date,
    seq: str,
    doc: str,
    nature: Nature = Nature.STANDALONE,
    period: str = "Quarterly",
) -> FilingIndexEntry:
    return FilingIndexEntry(
        isin=isin,
        symbol=symbol,
        name=symbol,
        period_start=start,
        period_end=end,
        filing_date=filed,
        nature=nature,
        audited=False,
        period=period,
        xbrl_url=f"https://nsearchives.nseindia.com/corporate/xbrl/{doc}",
        seq_number=seq,
    )


def _sepc_entry() -> FilingIndexEntry:
    return _entry(
        isin=SEPC,
        symbol="SEPC",
        start=date(2020, 4, 1),
        end=date(2020, 6, 30),
        filed=SEPC_FILED,
        seq="1091297",
        doc=SEPC_STAN_DOC,
    )


def _proven(detail: str = "proven") -> parser.RenameProof:
    return parser.RenameProof(True, detail, SEPC_FILED)


def _parse(
    doc: str,
    entry: FilingIndexEntry,
    *,
    known: frozenset[str],
    evidence: parser.RenameEvidence | None,
    folder: Path = F2,
) -> Filing:
    return parser.parse(
        (folder / doc).read_bytes(),
        entry=entry,
        known_symbols=known,
        rename_evidence=evidence,
        filename=doc,
    )


# ── the parser: only a proof lets a document symbol D2 does not place through ─────────────────


def test_a_rename_the_session_proves_is_stored_with_the_documents_own_values() -> None:
    asked: list[str] = []

    def evidence(symbol: str) -> parser.RenameProof:
        asked.append(symbol)
        return _proven()

    filing = _parse(SEPC_STAN_DOC, _sepc_entry(), known=frozenset({"SEPC"}), evidence=evidence)
    assert asked == ["SHRIRAMEPC"]
    assert filing.isin == SEPC  # the join key is the index's, never the symbol's
    assert filing.symbol == "SHRIRAMEPC"
    # Exactly the figures the document states: the same parse D2 would give had it recorded the
    # old symbol — no value is touched by the rename.
    as_if_recorded = _parse(
        SEPC_STAN_DOC, _sepc_entry(), known=frozenset({"SHRIRAMEPC"}), evidence=None
    )
    assert filing.facts == as_if_recorded.facts and len(filing.facts) > 0


def test_without_evidence_the_rename_stays_refused_with_the_old_message() -> None:
    with pytest.raises(ParseError) as refused:
        _parse(SEPC_STAN_DOC, _sepc_entry(), known=frozenset({"SEPC"}), evidence=None)
    assert str(refused.value).endswith(
        "but the document says SHRIRAMEPC; the announcements index and the XBRL must name the "
        "same company"
    )


def test_an_unproven_rename_is_refused_and_says_why() -> None:
    entry = _entry(
        isin=ESSENTIA,
        symbol="ESSENTIA",
        start=date(2021, 1, 1),
        end=date(2021, 3, 31),
        filed=date(2021, 5, 6),
        seq="1107015",
        doc=ESSENTIA_DOC,
    )
    why = "D2 holds no window of INTEGRA for INE418N01035 and NSE's bhavcopy of 2021-05-06 ..."
    with pytest.raises(ParseError, match=re.escape(f"; rename not proven: {why}")):
        _parse(
            ESSENTIA_DOC,
            entry,
            known=frozenset({"ESSENTIA"}),
            evidence=lambda symbol: parser.RenameProof(False, why),
        )


def test_a_document_stating_no_symbol_proves_nothing() -> None:
    entry = _entry(
        isin=MOTHERSON,
        symbol="MOTHERSON",
        start=date(2022, 4, 1),
        end=date(2023, 3, 31),
        filed=date(2023, 5, 27),
        seq="974",
        doc=MOTHERSON_DOC,
        period="Annual",
    )

    def never(symbol: str) -> parser.RenameProof:
        raise AssertionError("a blank symbol must never reach the session statement")

    with pytest.raises(ParseError, match="rename not proven: the document states no symbol"):
        _parse(MOTHERSON_DOC, entry, known=frozenset({"MOTHERSON"}), evidence=never)


# ── rename_evidence_of: D2's session statement, exact ISIN, unambiguous, fail closed ─────────


def _window(symbol: str, start: date, end: date | None, isin: str) -> SymbolWindow:
    return SymbolWindow(
        exchange=Exchange.NSE, symbol=symbol, valid_from=start, valid_to=end, isin=isin
    )


def _statement(day: date, *rows: tuple[str, str, str]) -> SessionIdentity:
    return SessionIdentity.from_statements(
        [SessionStatementRow(isin=i, symbol=s, series=r, trade_date=day) for s, r, i in rows],
        exchange=Exchange.NSE,
        trade_date=day,
    )


@dataclass
class _Sessions:
    """A stand-in session reader: the statement per date, counting each read."""

    by_day: dict[date, SessionIdentity | None] = field(default_factory=dict)
    raises: bool = False
    reads: list[date] = field(default_factory=list)

    def __call__(self, day: date) -> SessionIdentity | None:
        self.reads.append(day)
        if self.raises:
            raise OSError("parquet unreadable")
        return self.by_day.get(day)


_SEPC_MASTER: Final = IdentityMaster([_window("SEPC", date(2008, 2, 20), None, SEPC)])


def _evidence(
    sessions: _Sessions, master: IdentityMaster = _SEPC_MASTER
) -> Callable[[str, date, str], parser.RenameProof | None]:
    return fb.rename_evidence_of(master, trading_calendar(), sessions)


def test_the_session_proves_a_rename_only_under_the_exact_isin() -> None:
    sessions = _Sessions({SEPC_FILED: _statement(SEPC_FILED, ("SHRIRAMEPC", "EQ", SEPC))})
    proof = _evidence(sessions)(SEPC, SEPC_FILED, "shriramepc ")
    assert proof is not None and proof.proven and proof.session == SEPC_FILED
    assert f"lists SHRIRAMEPC under {SEPC} and no other ISIN" in proof.detail
    # The same statement does not vouch for another ISIN: the inversion.
    other = _evidence(sessions)("INE000A01011", SEPC_FILED, "SHRIRAMEPC")
    assert other is not None and not other.proven
    assert f"lists SHRIRAMEPC under {SEPC}, not INE000A01011" in other.detail


def test_the_last_session_on_or_before_the_filing_date_is_the_one_read() -> None:
    friday, sunday = date(2020, 9, 11), date(2020, 9, 13)
    sessions = _Sessions({friday: _statement(friday, ("SHRIRAMEPC", "EQ", SEPC))})
    proof = _evidence(sessions)(SEPC, sunday, "SHRIRAMEPC")
    assert proof is not None and proof.proven and proof.session == friday
    assert sessions.reads == [friday]


def test_an_ambiguous_session_is_refused() -> None:
    """A symbol the session lists under two ISINs (any series) is not unambiguously this one."""
    sessions = _Sessions(
        {
            SEPC_FILED: _statement(
                SEPC_FILED, ("SHRIRAMEPC", "EQ", SEPC), ("SHRIRAMEPC", "N1", "INE964H07011")
            )
        }
    )
    proof = _evidence(sessions)(SEPC, SEPC_FILED, "SHRIRAMEPC")
    assert proof is not None and not proof.proven
    assert "under more than one ISIN (INE964H01014, INE964H07011)" in proof.detail


def test_a_symbol_the_session_does_not_list_is_refused() -> None:
    sessions = _Sessions({SEPC_FILED: _statement(SEPC_FILED, ("SEPC", "EQ", SEPC))})
    proof = _evidence(sessions)(SEPC, SEPC_FILED, "SHRIRAMEPC")
    assert proof is not None and not proof.proven
    assert proof.detail == (
        f"D2 holds no window of SHRIRAMEPC for {SEPC} and NSE's bhavcopy of 2020-09-09 does not "
        "list SHRIRAMEPC"
    )


def test_a_missing_session_is_refused() -> None:
    proof = _evidence(_Sessions())(SEPC, SEPC_FILED, "SHRIRAMEPC")
    assert proof is not None and not proof.proven
    assert proof.detail.endswith("NSE's bhavcopy of 2020-09-09 is not stored")


def test_an_unreadable_session_is_refused_and_read_once() -> None:
    sessions = _Sessions(raises=True)
    evidence = _evidence(sessions)
    for _ in range(2):
        proof = evidence(SEPC, SEPC_FILED, "SHRIRAMEPC")
        assert proof is not None and not proof.proven
        assert "could not be read (OSError: parquet unreadable)" in proof.detail
    assert sessions.reads == [SEPC_FILED]  # cached: one read per session, failure included


def test_a_symbol_d2_places_under_another_isin_that_day_is_refused() -> None:
    master = IdentityMaster(
        [
            _window("SEPC", date(2008, 2, 20), None, SEPC),
            _window("SHRIRAMEPC", date(2020, 1, 1), None, "INE000A01011"),
        ]
    )
    sessions = _Sessions({SEPC_FILED: _statement(SEPC_FILED, ("SHRIRAMEPC", "EQ", SEPC))})
    proof = _evidence(sessions, master)(SEPC, SEPC_FILED, "SHRIRAMEPC")
    assert proof is not None and not proof.proven
    assert "D2 places SHRIRAMEPC under INE000A01011 that day" in proof.detail


_DVL_MASTER: Final = IdentityMaster(
    [
        _window("DTIL", date(2008, 1, 11), date(2010, 7, 25), DVL),
        _window("DPTL", date(2010, 7, 26), date(2014, 11, 11), DVL),
        _window("DPL", date(2014, 11, 12), date(2019, 1, 1), DVL),
        _window("DVL", date(2019, 1, 2), None, DVL),
        _window("DTIL", date(2015, 1, 20), None, DTIL_TODAY),
    ]
)


def test_a_symbol_d2_holds_for_the_isin_is_d2s_to_decide_and_the_session_is_not_read() -> None:
    sessions = _Sessions()
    assert _evidence(sessions, _DVL_MASTER)(DVL, date(2018, 5, 26), "DTIL") is None
    assert sessions.reads == []


def test_dtil_filed_as_dvl_stays_refused_under_d37s_exact_text() -> None:
    """D37: even a session statement wrongly listing DTIL under DVL's ISIN cannot let it in, and
    the refusal is word for word the one D37's accepted deviation matches."""
    filed = date(2018, 5, 26)
    sessions = _Sessions({date(2018, 5, 25): _statement(date(2018, 5, 25), ("DTIL", "EQ", DVL))})
    evidence = _evidence(sessions, _DVL_MASTER)
    entry = _entry(
        isin=DVL,
        symbol="DVL",
        start=date(2018, 1, 1),
        end=date(2018, 3, 31),
        filed=filed,
        seq="1041285",
        doc=DTIL_DOC,
    )
    known = fb.symbols_accepted_on(
        _DVL_MASTER.windows_for(DVL),
        filed,
        isin=DVL,
        owner_on=lambda symbol: _DVL_MASTER.try_resolve(symbol, filed),
    ) | {"DVL"}
    with pytest.raises(ParseError) as refused:
        _parse(DTIL_DOC, entry, known=known, evidence=lambda s: evidence(DVL, filed, s))
    (d37,) = [
        d for d in yaml.safe_load(DEVIATIONS.read_text())["deviations"] if d["id"].startswith("d37")
    ]
    assert re.fullmatch(d37["match"]["errors"][0]["pattern"], f"parse failed: {refused.value}")
    assert sessions.reads == []


@pytest.mark.parametrize(
    ("doc", "folder", "isin", "symbol", "stated", "filed", "period"),
    [
        (
            GILLETTE_DOC,
            F2,
            GILLETTE,
            "GILLETTE",
            PG_HYGIENE,
            date(2025, 7, 31),
            (date(2025, 4, 1), date(2025, 6, 30)),
        ),
        (
            ASHAPURMIN_DOC,
            INTEGRATED,
            ASHAPURMIN,
            "ASHAPURMIN",
            ORIENT_CERATECH,
            date(2025, 8, 12),
            (date(2025, 4, 1), date(2025, 6, 30)),
        ),
    ],
    ids=["GILLETTE", "ASHAPURMIN"],
)
def test_class_g_stays_refused_whatever_the_session_says(
    doc: str,
    folder: Path,
    isin: str,
    symbol: str,
    stated: str,
    filed: date,
    period: tuple[date, date],
) -> None:
    """A sister company's stated ISIN is not a rename: a proof that would vouch for anything
    changes nothing."""
    entry = _entry(
        isin=isin, symbol=symbol, start=period[0], end=period[1], filed=filed, seq="IF1", doc=doc
    )
    with pytest.raises(ParseError, match=f"states ISIN {stated} — a different issuer"):
        _parse(doc, entry, known=frozenset({symbol}), evidence=lambda s: _proven(), folder=folder)


def test_the_session_loader_reads_nse_rows_from_l1_prices_raw(tmp_path: Path) -> None:
    """The real reader: NSE rows of the session only — a BSE row naming the symbol differently
    is another exchange's statement and does not make the NSE one ambiguous."""
    day = SEPC_FILED
    path = l1_partition_path("prices_raw", day, data_root=tmp_path)
    path.parent.mkdir(parents=True)
    pq.write_table(
        pa.table(
            {
                "isin": [SEPC, "INE999Z01011", "INE111A01011"],
                "exchange": ["NSE", "BSE", "NSE"],
                "symbol": ["SHRIRAMEPC", "SHRIRAMEPC", "OTHER"],
                "series": ["EQ", "A", "EQ"],
                "trade_date": [day, day, day],
            }
        ),
        path,
    )
    clock = FrozenClock(datetime(2026, 11, 20, 2, 0, tzinfo=IST))
    loader = fb.session_loader(L0Store(clock=clock, data_root=tmp_path), data_root=tmp_path)
    proof = _evidence(cast(Any, loader))(SEPC, day, "SHRIRAMEPC")
    assert proof is not None and proof.proven
    missing = _evidence(cast(Any, loader))(SEPC, date(2020, 9, 8), "SHRIRAMEPC")
    assert missing is not None and not missing.proven and "is not stored" in missing.detail


# ── the re-queue: legacy results-index units, reuse-only, zero requests ──────────────────────


class _Fetcher:
    def __init__(self) -> None:
        self.requests: list[tuple[str, str]] = []

    def fetch(
        self, source_id: str, url: str, logical_date: date, *, filename: str | None = None
    ) -> L0Ref:
        self.requests.append((source_id, url))
        raise AssertionError("a reuse-only re-queue made a request")


@dataclass
class _Row:
    state: SyncState
    last_error: str | None = None
    retryable: bool = True


@dataclass
class _Sync:
    rows: dict[tuple[str, date], _Row] = field(default_factory=dict)

    def get(self, source: str, logical_date: date) -> _Row | None:
        return self.rows.get((source, logical_date))

    def begin(self, source: str, logical_date: date) -> _Row:
        row = self.rows.setdefault((source, logical_date), _Row(SyncState.PENDING))
        assert row.state is not SyncState.PUBLISHED
        row.state, row.last_error = SyncState.PENDING, None
        return row

    def _to(self, source: str, logical_date: date, state: SyncState) -> _Row:
        row = self.rows[(source, logical_date)]
        row.state = state
        return row

    def mark_fetched(self, source: str, logical_date: date, **_: Any) -> _Row:
        return self._to(source, logical_date, SyncState.FETCHED)

    def mark_validated(self, source: str, logical_date: date) -> _Row:
        return self._to(source, logical_date, SyncState.VALIDATED)

    def mark_normalized(self, source: str, logical_date: date) -> _Row:
        return self._to(source, logical_date, SyncState.NORMALIZED)

    def mark_published(self, source: str, logical_date: date) -> _Row:
        return self._to(source, logical_date, SyncState.PUBLISHED)

    def mark_failed(
        self, source: str, logical_date: date, error: str, *, retryable: bool = True
    ) -> _Row:
        row = self._to(source, logical_date, SyncState.FAILED)
        row.last_error, row.retryable = error, retryable
        return row


@dataclass
class _Journal:
    rows: list[tuple[str, rq.RequeueOutcome, str | None]] = field(default_factory=list)

    def __call__(
        self, item: rq.RequeueItem, outcome: rq.RequeueOutcome, detail: str | None
    ) -> None:
        self.rows.append((item.unit, outcome, detail))


def _lake(tmp_path: Path) -> tuple[L0Store, _Sync]:
    clock = FrozenClock(datetime(2026, 11, 20, 2, 0, 5, tzinfo=IST))
    l0 = L0Store(clock=clock, data_root=tmp_path)
    l0.put(
        "nse_financial_results_index",
        date(2020, 7, 1),
        LEGACY_INDEX,
        (F2 / LEGACY_INDEX).read_bytes(),
    )
    for doc in (SEPC_STAN_DOC, SEPC_CONS_DOC):
        l0.put("nse_xbrl_filing", SEPC_FILED, doc, (F2 / doc).read_bytes())
    sync = _Sync()
    for unit in SEPC_UNITS:
        sync.rows[(f"nse_xbrl_filing/{unit}", SEPC_FILED)] = _Row(
            SyncState.FAILED, "parse failed: ... but the document says SHRIRAMEPC; ..."
        )
    return l0, sync


def _runner(tmp_path: Path, l0: L0Store, sync: _Sync, sessions: _Sessions) -> tuple[Any, Any]:
    fetcher = _Fetcher()
    runner = fb.FundamentalsBackfillRunner(
        fetcher=cast(Any, fetcher),
        l0=l0,
        sync=cast(Any, sync),
        universe=set(),  # a re-queue item is driven whatever the night's universe
        commit=lambda: None,
        data_root=tmp_path,
        symbol_history=lambda isin, on: fb.symbols_accepted_on(
            _SEPC_MASTER.windows_for(isin),
            on,
            isin=isin,
            owner_on=lambda symbol: _SEPC_MASTER.try_resolve(symbol, on),
        ),
        rename_evidence=_evidence(sessions),
        clock=FrozenClock(datetime(2026, 11, 20, 2, 0, 5, tzinfo=IST)),
    )
    return runner, fetcher


def _legacy_item(unit: str, *, reuse_only: bool = True) -> rq.RequeueItem:
    return rq.RequeueItem(
        F2_REQUEUE, rq.RequeueAction.REFETCH, unit, SEPC_FILED, LEGACY_KEY, reuse_only=reuse_only
    )


def test_a_legacy_item_is_reread_from_its_stored_chunk_and_published_with_no_request(
    tmp_path: Path,
) -> None:
    l0, sync = _lake(tmp_path)
    sessions = _Sessions({SEPC_FILED: _statement(SEPC_FILED, ("SHRIRAMEPC", "EQ", SEPC))})
    runner, fetcher = _runner(tmp_path, l0, sync, sessions)
    journal = _Journal()
    report = fb.FundamentalsBackfillReport(index_requested=0)
    runner.requeue([_legacy_item(u) for u in SEPC_UNITS], journal=journal, report=report)
    assert fetcher.requests == []
    assert [(u, o) for u, o, _ in journal.rows] == [
        (u, rq.RequeueOutcome.PUBLISHED) for u in SEPC_UNITS
    ]
    assert all(
        sync.rows[(f"nse_xbrl_filing/{u}", SEPC_FILED)].state is SyncState.PUBLISHED
        for u in SEPC_UNITS
    )
    assert report.filings_published == 2 and report.filings_l0_reused == 2
    assert sessions.reads == [SEPC_FILED]  # both filings, one session read


def test_an_unproven_legacy_item_is_journaled_failed_naming_why(tmp_path: Path) -> None:
    """The inversion: the same chunk and documents, but the session does not list the symbol."""
    l0, sync = _lake(tmp_path)
    runner, fetcher = _runner(tmp_path, l0, sync, _Sessions())
    journal = _Journal()
    runner.requeue(
        [_legacy_item(SEPC_UNITS[0])],
        journal=journal,
        report=fb.FundamentalsBackfillReport(index_requested=0),
    )
    assert fetcher.requests == []
    ((_, outcome, detail),) = journal.rows
    assert outcome is rq.RequeueOutcome.FAILED
    assert detail is not None and "rename not proven" in detail and "is not stored" in detail


def test_a_legacy_item_that_is_not_reuse_only_fetches_nothing(tmp_path: Path) -> None:
    l0, sync = _lake(tmp_path)
    runner, fetcher = _runner(tmp_path, l0, sync, _Sessions())
    journal = _Journal()
    runner.requeue(
        [_legacy_item(SEPC_UNITS[0], reuse_only=False)],
        journal=journal,
        report=fb.FundamentalsBackfillReport(index_requested=0),
    )
    assert fetcher.requests == []
    ((_, outcome, detail),) = journal.rows
    assert outcome is rq.RequeueOutcome.FAILED and detail is not None and "reuse_only" in detail


def test_a_legacy_item_without_its_stored_document_fails_and_fetches_nothing(
    tmp_path: Path,
) -> None:
    clock = FrozenClock(datetime(2026, 11, 20, 2, 0, 5, tzinfo=IST))
    l0 = L0Store(clock=clock, data_root=tmp_path)
    l0.put(
        "nse_financial_results_index",
        date(2020, 7, 1),
        LEGACY_INDEX,
        (F2 / LEGACY_INDEX).read_bytes(),
    )
    runner, fetcher = _runner(tmp_path, l0, _Sync(), _Sessions())
    journal = _Journal()
    runner.requeue(
        [_legacy_item(SEPC_UNITS[0])],
        journal=journal,
        report=fb.FundamentalsBackfillReport(index_requested=0),
    )
    assert fetcher.requests == []
    ((_, outcome, detail),) = journal.rows
    assert outcome is rq.RequeueOutcome.FAILED and detail is not None and "reuse_only" in detail


# ── the register ─────────────────────────────────────────────────────────────────────────────


def _register(tmp_path: Path, filing: str, *, reuse_only: bool = True) -> Path:
    path = tmp_path / "requeue.yaml"
    path.write_text(
        "version: 1\nrequeues:\n"
        "  - id: legacy-test\n    action: refetch\n"
        + ("    reuse_only: true\n" if reuse_only else "")
        + "    not_before: 2026-11-20\n    basis: test\n    filings:\n"
        + f"      - {filing}\n"
    )
    return path


def test_a_legacy_unit_validates_in_a_reuse_only_requeue(tmp_path: Path) -> None:
    (item,) = rq.load_requeues(
        _register(tmp_path, f'{{unit: "1091297", filing_date: 2020-09-09, index: {LEGACY_KEY}}}')
    )
    assert item.legacy and item.legacy_period == "Quarterly" and item.reuse_only
    assert item.index_ref == ("nse_financial_results_index", date(2020, 7, 1), LEGACY_INDEX)


@pytest.mark.parametrize(
    ("filing", "reuse_only", "error"),
    [
        (
            f'{{unit: "1091297", filing_date: 2020-09-09, index: {LEGACY_KEY}}}',
            False,
            "not reuse_only",
        ),
        (
            '{unit: "1091297", filing_date: 2020-09-09, index: '
            "nse_integrated_filing_index/2020-09-09/p.json}",
            True,
            "not an integrated-feed unit",
        ),
        (f"{{unit: IF97542, filing_date: 2020-09-09, index: {LEGACY_KEY}}}", True, "not a legacy"),
        (
            '{unit: "1091297", filing_date: 2020-09-09, index: '
            "nse_financial_results_index/2020-07-01/other.json}",
            True,
            "not a stored corporates-financial-results chunk",
        ),
        (f"{{unit: 1091297, filing_date: 2020-09-09, index: {LEGACY_KEY}}}", True, "string"),
        (f'{{unit: "10x91", filing_date: 2020-09-09, index: {LEGACY_KEY}}}', True, "pattern"),
    ],
    ids=[
        "not-reuse-only",
        "legacy-unit-integrated-page",
        "if-unit-legacy-page",
        "bad-chunk",
        "unquoted-unit",
        "bad-unit",
    ],
)
def test_a_legacy_unit_is_refused_outside_its_rules(
    tmp_path: Path, filing: str, reuse_only: bool, error: str
) -> None:
    with pytest.raises(rq.RequeueError, match=error):
        rq.load_requeues(_register(tmp_path, filing, reuse_only=reuse_only))


def test_the_f2_requeue_is_reuse_only_legacy_and_under_its_own_bound() -> None:
    """D44 (owner, 2026-11-16): the 89 F2 filings, once, with zero requests, Fri 20 Nov 02:00."""
    items = [i for i in rq.load_requeues() if i.requeue_id == F2_REQUEUE]
    assert len(items) == 89 == rq.RAISED_BOUNDS[F2_REQUEUE]
    assert all(i.reuse_only and i.action is rq.RequeueAction.REFETCH for i in items)
    assert all(i.legacy and i.index_ref[0] == "nse_financial_results_index" for i in items)
    assert len({(i.unit, i.filing_date) for i in items}) == 89
    raw = yaml.safe_load(rq.REQUEUE_PATH.read_text())
    (entry,) = [r for r in raw["requeues"] if r["id"] == F2_REQUEUE]
    assert entry["basis"].startswith("HUMAN_DECISIONS.md D44 (owner, 2026-11-16)")
    pairs: dict[str, int] = {}
    for filing in entry["filings"]:
        index_symbol = filing["note"].split(" as ")[1].split(" ")[0]
        pairs[index_symbol] = pairs.get(index_symbol, 0) + 1
    assert pairs == {
        "SEPC": 34,
        "ZFCVINDIA": 21,
        "PATANJALI": 13,
        "KEEPLEARN": 10,
        "RBA": 5,
        "ESSENTIA": 5,
        "MOTHERSON": 1,
    }
    due_thu = rq.pending_items(cast(Any, _EmptyConn()), date(2026, 11, 19))
    assert not any(i.requeue_id == F2_REQUEUE for i in due_thu)
    due_fri = rq.pending_items(cast(Any, _EmptyConn()), date(2026, 11, 20))
    assert sum(i.requeue_id == F2_REQUEUE for i in due_fri) == 89


class _EmptyConn:
    def execute(self, sql: str, params: object = ()) -> Any:
        class _Result:
            def fetchall(self) -> list[tuple[str, str, date]]:
                return []

        return _Result()
