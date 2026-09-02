"""M3.8 — corporate announcements + keyword index.

The file is laid out as the three acceptance criteria, because each is a property a plausible wrong
implementation would quietly violate:

1. **Searchable by ISIN, date range and keyword within an hour of the poll.** Proved end to end: a
   scripted NSE poll runs fetch → L0 → parse → L1, and the same job builds the index straight from
   L1 and answers all three query shapes — so a disclosure is searchable the moment its partition is
   written, not a batch later. The date window filters on the source dissemination date (the natural
   PIT), so the 06-Aug auditor notice drops out of a 07-Aug-only window.
2. **Source timestamps are preserved exactly — never re-stamped with the ingest clock.** Proved by
   running the whole poll under a clock set almost a year away and asserting every row's `ts` is the
   exchange's own dissemination instant, and by an L1 round trip that returns the same instant. A
   parser that reached for the clock on a missing/blank source time would fail these — instead it
   refuses the row.
3. **A break-condition keyword set matches the intended announcements and not obvious false
   positives.** Proved against §5.3's BC2 ("exit/divestment of robotics business line") and BC3
   ("auditor resignation"): BC2 fires on the robotics divestment and on *neither* the robotics
   order-win (shares "robotics", not the event) nor the chemicals divestment (shares the event, not
   the subject), and `none_of` sheds a crafted denial. A keyword set that names no required term is
   rejected at construction — a break condition that matches everything is mis-specified.
"""

from __future__ import annotations

import json
import socket
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any, Final

import pytest

from dataplatform.alerts import AlertOutcome, Severity
from dataplatform.clock import IST, FrozenClock
from dataplatform.config import Settings
from dataplatform.identity.master import (
    Exchange,
    IdentityMaster,
    Listing,
    ListingStatus,
    Security,
)
from dataplatform.ingest.announcements import (
    ANNOUNCEMENTS_DATASET,
    BSE_SOURCE_ID,
    NSE_SOURCE_ID,
    AnnouncementBatch,
    AnnouncementRow,
    SyncTracker,
    dedupe,
    ingest_bse,
    ingest_nse,
    parse_bse,
    parse_nse,
    read_l1,
    write_l1,
)
from dataplatform.ingest.corp_actions import build_scrip_index
from dataplatform.ingest.fetcher import (
    Fetcher,
    RecordedResponse,
    RecordedTransport,
    ScriptedOutcome,
)
from dataplatform.ingest.models import ParseError
from dataplatform.ingest.source_register import SourceRegister
from dataplatform.ingest.source_register import load as load_register
from dataplatform.query.announcement_search import (
    AnnouncementIndex,
    KeywordQuery,
    build_from_l1,
    normalize,
)
from dataplatform.status.sync_state import SyncRecord, SyncState, SyncStateStore
from dataplatform.store.l0 import L0Store
from tests.conftest import SettingsLoader

NSE_FIXTURE: Final = Path(
    "tests/fixtures/announcements/nse/2026-08-07/corporate-announcements.json"
)
BSE_FIXTURE: Final = Path("tests/fixtures/announcements/bse/2026-08-07/AnnSubCategoryGetData.json")

POLL: Final = date(2026, 8, 7)
#: The ingest clock is set almost a year past the poll on purpose: any row whose `ts` came from the
#: clock rather than the source would be date(2027, ...) and fail acceptance 2 loudly.
NOW: Final = datetime(2027, 6, 1, 3, 0, tzinfo=IST)

WARM: Final = "https://www.nseindia.com/"
NSE_API: Final = (
    "https://www.nseindia.com/api/corporate-announcements"
    "?index=equities&from_date=07-08-2026&to_date=07-08-2026"
)
BSE_API: Final = (
    "https://api.bseindia.com/BseIndiaAPI/api/AnnSubCategoryGetData/w"
    "?pageno=1&strCat=-1&strPrevDate=20260807&strScrip=&strSearch=P&strToDate=20260807"
    "&strType=C&subcategory=-1"
)

#: ISINs and scrips in the fixtures, for readable assertions.
ROBO: Final = "INE001A01018"  # Acme Robotics — NSE ACMEROBO / BSE scrip 543210
CHEM: Final = "INE002A01018"  # Northern Chemicals — NSE NORTHCHEM / BSE scrip 500325


# ── a §4.4 test double, so a whole poll runs without Postgres (B8) ──────────────────────────────


class RecordingTracker:
    """An in-memory §4.4 state machine that delegates every rule to `SyncRecord.transition`.

    The same pure state machine M1.3's store applies, so the end-to-end tests are not a fiction.
    `history` is the ordered list of states a `(source, date)` passed through.
    """

    def __init__(self, clock: FrozenClock) -> None:
        self._clock = clock
        self.rows: dict[tuple[str, date], SyncRecord] = {}
        self.history: list[SyncState] = []

    def begin(self, source: str, logical_date: date) -> SyncRecord:
        existing = self.rows.get((source, logical_date))
        now = self._clock.now()
        record = (
            SyncRecord(
                source=source,
                logical_date=logical_date,
                state=SyncState.PENDING,
                updated_at=now,
                attempts=1,
                first_attempt_at=now,
            )
            if existing is None
            else existing.transition(SyncState.PENDING, at=now)
        )
        return self._store(record)

    def mark_fetched(
        self, source: str, logical_date: date, *, checksum: str, l0_path: str | None = None
    ) -> SyncRecord:
        return self._advance(
            source, logical_date, SyncState.FETCHED, checksum=checksum, l0_path=l0_path
        )

    def mark_validated(self, source: str, logical_date: date) -> SyncRecord:
        return self._advance(source, logical_date, SyncState.VALIDATED)

    def mark_normalized(self, source: str, logical_date: date) -> SyncRecord:
        return self._advance(source, logical_date, SyncState.NORMALIZED)

    def mark_published(self, source: str, logical_date: date) -> SyncRecord:
        return self._advance(source, logical_date, SyncState.PUBLISHED)

    def mark_failed(
        self, source: str, logical_date: date, error: str, *, retryable: bool = True
    ) -> SyncRecord:
        return self._advance(
            source, logical_date, SyncState.FAILED, error=error, retryable=retryable
        )

    def _advance(
        self, source: str, logical_date: date, to_state: SyncState, **kwargs: Any
    ) -> SyncRecord:
        current = self.rows[(source, logical_date)]
        return self._store(current.transition(to_state, at=self._clock.now(), **kwargs))

    def _store(self, record: SyncRecord) -> SyncRecord:
        self.rows[record.key] = record
        self.history.append(record.state)
        return record


def _tracker_protocol_is_satisfied_by_the_real_store(store: SyncStateStore) -> SyncTracker:
    """`mypy --strict` fails here if M1.3's store ever stops fitting the runner's protocol."""
    return store


def _tracker_protocol_is_satisfied_by_the_double(tracker: RecordingTracker) -> SyncTracker:
    """And the double in this file fits it too, so the end-to-end tests are not a fiction."""
    return tracker


class SpyAlerter:
    """Records alerts instead of delivering them, and satisfies the C.3 `Alerter` protocol."""

    def __init__(self) -> None:
        self.sent: list[tuple[Severity, str, str, str]] = []

    def send(self, severity: Severity, title: str, body: str, dedup_key: str) -> AlertOutcome:
        self.sent.append((severity, title, body, dedup_key))
        return AlertOutcome.SENT


# ── fixtures ─────────────────────────────────────────────────────────────────────────────────


@pytest.fixture(autouse=True)
def no_network(monkeypatch: pytest.MonkeyPatch) -> None:
    """Any socket in this module is a bug: every response is scripted (B8)."""

    def forbidden(*args: Any, **kwargs: Any) -> Any:
        raise AssertionError("a test opened a socket; announcement tests are offline")

    monkeypatch.setattr(socket, "socket", forbidden)
    monkeypatch.setattr(socket, "create_connection", forbidden)


@pytest.fixture(scope="session")
def nse_bytes(repo_root: Path) -> bytes:
    return (repo_root / NSE_FIXTURE).read_bytes()


@pytest.fixture(scope="session")
def bse_bytes(repo_root: Path) -> bytes:
    return (repo_root / BSE_FIXTURE).read_bytes()


@pytest.fixture
def register() -> SourceRegister:
    """The real, checked-in Source Register (C.1). Read for its shape, never for its hosts."""
    return load_register()


@pytest.fixture
def settings(load_settings: SettingsLoader) -> Settings:
    """Defaults only — the developer's `.env` must not decide what these tests observe."""
    return load_settings(None)


@pytest.fixture
def clock() -> FrozenClock:
    return FrozenClock(NOW)


@pytest.fixture
def tracker(clock: FrozenClock) -> RecordingTracker:
    return RecordingTracker(clock)


@pytest.fixture
def master() -> IdentityMaster:
    """A tiny D2 master carrying the two BSE scrip codes the fixture keys on (invariant #2).

    Both securities are also listed on BSE with a scrip code, so `build_scrip_index` yields the
    scrip→ISIN map that resolves the BSE feed — the only legitimate path off a raw scrip code.
    """
    securities = [
        Security(
            ROBO, "Acme Robotics Limited", Exchange.BSE, ListingStatus.ACTIVE, date(2020, 1, 1)
        ),
        Security(
            CHEM, "Northern Chemicals Limited", Exchange.BSE, ListingStatus.ACTIVE, date(2020, 1, 1)
        ),
    ]
    listings = [
        Listing(ROBO, Exchange.BSE, ListingStatus.ACTIVE, security_code="543210"),
        Listing(CHEM, Exchange.BSE, ListingStatus.ACTIVE, security_code="500325"),
    ]
    return IdentityMaster((), securities=securities, listings=listings)


@pytest.fixture
def scrip_index(master: IdentityMaster) -> dict[str, str]:
    return build_scrip_index(master)


@pytest.fixture
def build(clock: FrozenClock, settings: Settings, register: SourceRegister, tmp_path: Path) -> Any:
    """Build a real `Fetcher` over a recorded transport and a real L0 store under `tmp_path`."""

    def _build(
        script: dict[str, ScriptedOutcome | list[ScriptedOutcome]],
    ) -> tuple[Fetcher, L0Store, RecordedTransport]:
        transport = RecordedTransport(script)
        l0 = L0Store(clock=clock, data_root=tmp_path)
        fetcher = Fetcher(
            transport=transport,
            l0=l0,
            alerter=SpyAlerter(),
            clock=clock,
            register=register,
            settings=settings,
            sleep=lambda seconds: clock.advance(timedelta(seconds=seconds)),
        )
        return fetcher, l0, transport

    return _build


@pytest.fixture
def nse_rows(nse_bytes: bytes) -> tuple[AnnouncementRow, ...]:
    return parse_nse(nse_bytes, filename=NSE_FIXTURE.name, l0_key="k").rows


def ok(body: bytes) -> RecordedResponse:
    return RecordedResponse(body=body, headers={"content-type": "application/json; charset=utf-8"})


# ── acceptance 1: searchable by ISIN, date range and keyword, straight off the poll ───────────


def test_a_whole_nse_poll_runs_to_published_and_is_immediately_searchable(
    build: Any,
    nse_bytes: bytes,
    tracker: RecordingTracker,
    tmp_path: Path,
) -> None:
    """Fetch → L0 → parse → L1 → PUBLISHED, then the index is built from L1 in the same job.

    This is the M3-box-3 promise ("searchable within 1 hour of the poll") made concrete: indexing is
    part of the poll, not a downstream batch, so the rows are queryable the moment L1 is written.
    """
    fetcher, l0, transport = build({WARM: ok(b"<html>"), NSE_API: ok(nse_bytes)})
    result = ingest_nse(
        fetcher=fetcher, l0=l0, tracker=tracker, poll_date=POLL, url=NSE_API, data_root=tmp_path
    )

    assert [request.url for request in transport.requests] == [WARM, NSE_API]
    assert tracker.history[-1] is SyncState.PUBLISHED
    assert len(result.rows) == 5 and len(result.unresolved) == 1

    index = build_from_l1(data_root=tmp_path)
    assert index.size == 5


def test_search_by_isin_returns_only_that_security(nse_rows: tuple[AnnouncementRow, ...]) -> None:
    index = AnnouncementIndex(nse_rows)
    hits = index.search(isin=ROBO)
    assert {row.isin for row in hits} == {ROBO}
    assert len(hits) == 4  # the four ACMEROBO disclosures; the chemicals one is a different ISIN


def test_search_by_date_range_filters_on_the_source_dissemination_date(
    nse_rows: tuple[AnnouncementRow, ...],
) -> None:
    """A 07-Aug-only window excludes the auditor notice disseminated on 06-Aug.

    The window is on the announcement's own `ts` (natural PIT), not the poll date — each fixture row
    was polled on 07-Aug, so a filter on the poll date could not tell them apart. The 06-Aug row
    dropping out is what proves the filter reads the source instant.
    """
    index = AnnouncementIndex(nse_rows)
    same_day = index.search(isin=ROBO, start=POLL, end=POLL)
    assert len(same_day) == 3
    assert all(row.ts.astimezone(IST).date() == POLL for row in same_day)

    with_prior_day = index.search(isin=ROBO, start=date(2026, 8, 6), end=POLL)
    assert len(with_prior_day) == 4


def test_search_by_keyword_narrows_within_an_isin(nse_rows: tuple[AnnouncementRow, ...]) -> None:
    index = AnnouncementIndex(nse_rows)
    query = KeywordQuery(any_of=("board meeting",))
    hits = index.search(isin=ROBO, query=query)
    assert [row.subject for row in hits] == ["Board Meeting Intimation"]


def test_results_are_returned_in_dissemination_order(nse_rows: tuple[AnnouncementRow, ...]) -> None:
    index = AnnouncementIndex(nse_rows)
    hits = index.search(isin=ROBO)
    assert [row.ts for row in hits] == sorted(row.ts for row in hits)


def test_the_index_can_be_rebuilt_from_l1_over_a_date_range(
    nse_rows: tuple[AnnouncementRow, ...], tmp_path: Path
) -> None:
    """The build-from-L1 path a real poll uses, exercised without the fetcher."""
    write_l1(
        AnnouncementBatch(logical_date=POLL, source=NSE_SOURCE_ID, l0_key="k", rows=nse_rows),
        data_root=tmp_path,
    )
    index = build_from_l1(start=POLL, end=POLL, data_root=tmp_path)
    assert index.size == len(nse_rows)
    assert index.search(isin=ROBO, start=POLL, end=POLL)


# ── acceptance 2: source timestamps preserved exactly, never re-stamped with ingest time ──────


def test_the_ingest_clock_never_overwrites_a_source_timestamp(
    build: Any, nse_bytes: bytes, tracker: RecordingTracker, tmp_path: Path
) -> None:
    """The whole poll runs under a 2027 clock; every row's `ts` is still its 2026 source instant.

    `NOW` is set almost a year past the poll, so any row dated from the clock would be date(2027, …)
    and this assertion would fail. Instead each row carries the exchange's own dissemination time,
    to the second, in Asia/Kolkata.
    """
    fetcher, l0, _ = build({WARM: ok(b"<html>"), NSE_API: ok(nse_bytes)})
    ingest_nse(
        fetcher=fetcher, l0=l0, tracker=tracker, poll_date=POLL, url=NSE_API, data_root=tmp_path
    )

    index = build_from_l1(data_root=tmp_path)
    divestment = index.search(isin=ROBO, query=KeywordQuery(all_of=("divestment",)))[0]
    assert divestment.ts == datetime(2026, 8, 7, 15, 35, 22, tzinfo=IST)
    assert all(row.ts.year == 2026 for row in index.search())


def test_source_timestamp_is_read_only_from_the_source_not_the_clock(
    nse_rows: tuple[AnnouncementRow, ...],
) -> None:
    """The parser takes no clock at all — `ts` can only come from the payload (invariant #7)."""
    auditor = next(row for row in nse_rows if "Auditor" in row.subject)
    assert auditor.ts == datetime(2026, 8, 6, 18, 2, 44, tzinfo=IST)


def test_a_naive_or_undatable_row_is_refused_never_stamped_with_now() -> None:
    """A record the source did not date is not point-in-time usable; the parser raises, never fills.

    The alternative a wrong implementation reaches for — stamping the ingest time — is exactly the
    look-ahead leak invariant #7 forbids, so "no source time" must be a loud failure, not a default.
    """
    undatable = json.dumps(
        [{"sm_isin": ROBO, "desc": "No timestamp here", "attchmntText": "x"}]
    ).encode("utf-8")
    with pytest.raises(ParseError, match="no resolvable source dissemination timestamp"):
        parse_nse(undatable, filename="x.json")


def test_l1_round_trip_preserves_the_exact_instant(
    nse_rows: tuple[AnnouncementRow, ...], tmp_path: Path
) -> None:
    """Writing to L1 and reading back returns the same instants — no re-stamping in transit."""
    write_l1(
        AnnouncementBatch(logical_date=POLL, source=NSE_SOURCE_ID, l0_key="k", rows=nse_rows),
        data_root=tmp_path,
    )
    read_back = read_l1(POLL, data_root=tmp_path)
    original = {(row.isin, row.subject): row.ts for row in nse_rows}
    for row in read_back.rows:
        assert row.ts == original[(row.isin, row.subject)]


# ── acceptance 3: a break-condition keyword set matches the intended, not the false positives ──


def _bc2() -> KeywordQuery:
    """§5.3 BC2: exit/divestment of the robotics business line → T0 announcement keywords → T1.

    `all_of` ties the hit to the *subject* (robotics), `any_of` to the *event* (a divestment/exit),
    and `none_of` sheds the obvious collateral hit — a robotics division winning an order.
    """
    return KeywordQuery(
        all_of=("robotics",),
        any_of=("divest", "exit", "slump sale", "sale", "hive off", "demerger"),
        none_of=("order", "wins", "awarded"),
    )


def test_bc2_fires_on_the_robotics_divestment(nse_rows: tuple[AnnouncementRow, ...]) -> None:
    index = AnnouncementIndex(nse_rows)
    hits = index.matches(_bc2())
    assert [row.subject for row in hits] == ["Divestment of Robotics Business Line"]


def test_bc2_does_not_fire_on_a_robotics_order_win(nse_rows: tuple[AnnouncementRow, ...]) -> None:
    """Shares the word 'robotics' but not the event — the `any_of`/`none_of` gates shed it."""
    order = next(row for row in nse_rows if "Award of Order" in row.subject)
    assert not _bc2().matches_row(order)


def test_bc2_does_not_fire_on_a_divestment_of_a_different_business(
    nse_rows: tuple[AnnouncementRow, ...],
) -> None:
    """Shares the event (divestment) but not the subject (robotics) — the `all_of` gate sheds it.

    This is the criterion that separates a real break-condition matcher from a naive keyword grep:
    a chemicals divestment is a true divestment announcement and would trip any 'divest' filter, but
    it is not this thesis's break, and BC2 must not escalate it.
    """
    chem = next(row for row in nse_rows if row.isin == CHEM)
    assert "divest" in normalize(chem.searchable_text)
    assert not _bc2().matches_row(chem)


def test_none_of_sheds_a_crafted_false_positive() -> None:
    """A denial that name-checks every required term is excluded by `none_of`."""
    denial = AnnouncementRow(
        ts=datetime(2026, 8, 7, 12, 0, tzinfo=IST),
        source=NSE_SOURCE_ID,
        isin=ROBO,
        subject="Clarification on media reports",
        body="The Company clarifies that the reported divestment or exit of its robotics business "
        "line is only a rumour; the Board has approved no such transaction.",
    )
    without_guard = KeywordQuery(all_of=("robotics",), any_of=("divest", "exit"))
    assert without_guard.matches_row(denial)  # the naive query would (wrongly) flag it
    with_guard = KeywordQuery(
        all_of=("robotics",), any_of=("divest", "exit"), none_of=("rumour", "clarifies", "no such")
    )
    assert not with_guard.matches_row(denial)  # the ratified query sheds it


def test_bc3_auditor_resignation_fires_on_the_auditor_notice(
    nse_rows: tuple[AnnouncementRow, ...],
) -> None:
    """§5.3 BC3 (integrity): auditor resignation → immediate T1."""
    bc3 = KeywordQuery(
        any_of=(
            "resignation of statutory auditor",
            "auditor has resigned",
            "auditor resignation",
            "fraud investigation",
        )
    )
    index = AnnouncementIndex(nse_rows)
    hits = index.matches(bc3)
    assert [row.subject for row in hits] == ["Resignation of Statutory Auditor"]


def test_a_stem_matches_its_morphological_variants() -> None:
    """`divest` catches 'divestment'/'divesting'/'divested' without a stemmer (word-anchored)."""
    query = KeywordQuery(any_of=("divest",))
    assert query.matches("The Board approved a divestment")
    assert query.matches("The Company is divesting the unit")
    assert query.matches("Having divested the business")


def test_a_term_is_anchored_at_a_word_boundary() -> None:
    """A term does not fire mid-word: 'auditor' must not match inside an unrelated longer word."""
    assert not KeywordQuery(any_of=("auditor",)).matches("The coauditorium was inaugurated")


def test_a_keyword_query_that_would_match_everything_is_rejected() -> None:
    """A break condition with no required term (only `none_of`, or nothing) is mis-specified."""
    with pytest.raises(ValueError, match="at least one of all_of/any_of"):
        KeywordQuery(none_of=("delisting",))
    with pytest.raises(ValueError, match="at least one of all_of/any_of"):
        KeywordQuery()


# ── the two exchanges converge on one row, and identity goes through D2 ────────────────────────


def test_bse_resolves_scrip_to_isin_through_d2_and_converges_with_nse(
    bse_bytes: bytes, scrip_index: dict[str, str]
) -> None:
    """BSE keys on a scrip code; resolved through D2 it lands on the same ISIN NSE keyed natively.

    The robotics divestment appears in both feeds and resolves to the same `ROBO` ISIN, so a T0
    matcher searching by ISIN sees one security's disclosures regardless of which exchange carried
    them (invariant #2 — nothing joined on the raw scrip).
    """
    result = parse_bse(bse_bytes, scrip_index=scrip_index, filename=BSE_FIXTURE.name)
    assert {row.isin for row in result.rows} == {ROBO, CHEM}
    divestment = next(row for row in result.rows if row.isin == ROBO)
    assert divestment.source == BSE_SOURCE_ID
    assert _bc2().matches_row(divestment)


def test_bse_unknown_scrip_is_surfaced_unresolved_not_guessed(
    bse_bytes: bytes, scrip_index: dict[str, str]
) -> None:
    """A scrip the master has never seen lands in `unresolved`, never under a guessed ISIN."""
    result = parse_bse(bse_bytes, scrip_index=scrip_index, filename=BSE_FIXTURE.name)
    assert len(result.unresolved) == 1
    assert result.unresolved[0].raw_identifier == "999999"
    assert not result.is_clean


def test_a_whole_bse_poll_runs_to_published(
    build: Any,
    bse_bytes: bytes,
    scrip_index: dict[str, str],
    tracker: RecordingTracker,
    tmp_path: Path,
) -> None:
    """BSE needs no session cookie, so the poll is one request — no warm-up."""
    fetcher, l0, transport = build({BSE_API: ok(bse_bytes)})
    result = ingest_bse(
        fetcher=fetcher,
        l0=l0,
        tracker=tracker,
        scrip_index=scrip_index,
        poll_date=POLL,
        url=BSE_API,
        data_root=tmp_path,
    )
    assert [request.url for request in transport.requests] == [BSE_API]
    assert tracker.history[-1] is SyncState.PUBLISHED
    assert len(result.rows) == 2


def test_the_index_merges_both_exchanges_under_one_isin(
    nse_rows: tuple[AnnouncementRow, ...], bse_bytes: bytes, scrip_index: dict[str, str]
) -> None:
    bse_rows = parse_bse(bse_bytes, scrip_index=scrip_index, filename=BSE_FIXTURE.name).rows
    index = AnnouncementIndex((*nse_rows, *bse_rows))
    sources = {row.source for row in index.search(isin=ROBO)}
    assert sources == {NSE_SOURCE_ID, BSE_SOURCE_ID}


# ── structural: dedupe, empty-success guard, HTML-200 guard, empty poll ────────────────────────


def test_an_overlapping_re_poll_is_deduplicated_by_the_exchange_id(
    nse_rows: tuple[AnnouncementRow, ...],
) -> None:
    """An intraday re-poll repeats disclosures; the exchange's own id de-duplicates them."""
    doubled = dedupe([*nse_rows, *nse_rows])
    assert len(doubled) == len(nse_rows)


def test_the_bse_empty_success_body_is_a_named_failure_not_an_empty_poll(
    scrip_index: dict[str, str],
) -> None:
    """The range endpoint answers a bad window with a 200 carrying `{}` — a failure, not success."""
    with pytest.raises(ParseError, match="empty body"):
        parse_bse(b"{}", scrip_index=scrip_index, filename="empty.json")


def test_an_html_page_wearing_a_200_never_becomes_a_row() -> None:
    with pytest.raises(ParseError, match="markup, not JSON"):
        parse_nse(b"<html><body>Access Denied</body></html>", filename="blocked.html")


def test_an_empty_poll_writes_a_well_formed_partition(tmp_path: Path) -> None:
    """A genuinely quiet poll is a partition with zero rows, distinct from 'never polled'."""
    write_l1(
        AnnouncementBatch(logical_date=POLL, source=NSE_SOURCE_ID, l0_key="k", rows=()),
        data_root=tmp_path,
    )
    assert read_l1(POLL, data_root=tmp_path).rows == ()
    with pytest.raises(FileNotFoundError):
        read_l1(date(2026, 8, 8), data_root=tmp_path)


def test_the_l1_dataset_name_is_stable() -> None:
    """Downstream (M3.10 gate, M5.11) reads this dataset by name; it must not drift."""
    assert ANNOUNCEMENTS_DATASET == "announcements"
