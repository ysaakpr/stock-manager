"""M3.9 — index constituents history + benchmark TRI.

The file is laid out as the three acceptance criteria, each a property a plausible wrong
implementation would quietly violate:

1. **A monthly snapshot is stored immutably with its as-of date.** Proved by writing a month, then
   showing a re-write of the *same* membership is a no-op and a write of *different* membership for
   that month raises `ImmutableSnapshotError` — "never overwrite a prior month's membership" (§4.1)
   made a property of the writer, not a convention.
2. **Membership as-of a historical date is queryable and returns the snapshot in force then.** Shown
   against L1 with two monthly snapshots whose membership differs: a date between them sees the
   earlier one, a date after sees the later one, and a date before the first sees nothing — never
   today's list, which is what kills survivorship bias in M4's PIT universe (invariant #7).
3. **§4.1's computed TRI fallback behaves as an estimate should.** The *published* series is
   `nifty_tri_history` and it is M3.9.b's — parser, PIT boundary and the spot-check against
   published levels live in `tests/unit/test_benchmark_tri.py`. What stays here is the fallback:
   the series is seeded to the published closing index value, and a positive dividend yield makes
   it exceed the price return by the accrued amount — an inverted dividend sign fails these tests.

Money assertions are written so inverting the logic fails them: index values and yields stay
`Decimal`, never `float`; a missing yield is `None`, never `0`; and the TRI seed equals a published
close exactly rather than approximately.
"""

from __future__ import annotations

import socket
from datetime import date, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from typing import Any, Final

import pytest

from dataplatform.alerts import AlertOutcome, Severity
from dataplatform.clock import IST, FrozenClock
from dataplatform.config import Settings
from dataplatform.ingest.fetcher import (
    Fetcher,
    RecordedResponse,
    RecordedTransport,
    ScriptedOutcome,
)
from dataplatform.ingest.indices import (
    ConstituentSnapshot,
    ImmutableSnapshotError,
    IndexCloseRow,
    SyncTracker,
    close_snapshot_url,
    compute_tri,
    constituents_url,
    extend_tri,
    ingest_constituents,
    ingest_tri_from_close,
    l0_constituents_filename,
    membership_asof,
    parse_close_snapshot,
    parse_constituents,
    read_constituents_l1,
    read_tri_series,
    tri_url,
    write_constituents_l1,
)
from dataplatform.ingest.models import ParseError
from dataplatform.ingest.source_register import SourceRegister
from dataplatform.ingest.source_register import load as load_register
from dataplatform.status.sync_state import SyncRecord, SyncState, SyncStateStore
from dataplatform.store.l0 import L0Store
from tests.conftest import SettingsLoader

FIXTURES: Final = Path("tests/fixtures/nifty_indices")
NIFTY50_JUL: Final = FIXTURES / "constituents/ind_nifty50list_20260701.csv"
NIFTY50_AUG: Final = FIXTURES / "constituents/ind_nifty50list_20260801.csv"
NIFTYIT_AUG: Final = FIXTURES / "constituents/ind_niftyitlist_20260801.csv"
CLOSE_03: Final = FIXTURES / "close/ind_close_all_03082026.csv"
CLOSE_04: Final = FIXTURES / "close/ind_close_all_04082026.csv"
CLOSE_05: Final = FIXTURES / "close/ind_close_all_05082026.csv"

JUL: Final = date(2026, 7, 1)
AUG: Final = date(2026, 8, 1)
NOW: Final = datetime(2026, 8, 1, 19, 0, tzinfo=IST)

RELIANCE: Final = "INE002A01018"
KOTAKBANK: Final = "INE237A01028"
ADANIENT: Final = "INE423A01024"


# ── a §4.4 test double, so a whole poll runs without Postgres (B8) ──────────────────────────────


class RecordingTracker:
    """An in-memory §4.4 state machine delegating every rule to `SyncRecord.transition`."""

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
    return tracker


# ── fixtures ─────────────────────────────────────────────────────────────────────────────────


@pytest.fixture(autouse=True)
def no_network(monkeypatch: pytest.MonkeyPatch) -> None:
    """Any socket in this module is a bug: every response is scripted or a checked-in file (B8)."""

    def forbidden(*args: Any, **kwargs: Any) -> Any:
        raise AssertionError("a test opened a socket; indices tests are offline")

    monkeypatch.setattr(socket, "socket", forbidden)
    monkeypatch.setattr(socket, "create_connection", forbidden)


@pytest.fixture
def register() -> SourceRegister:
    return load_register()


@pytest.fixture
def settings(load_settings: SettingsLoader) -> Settings:
    return load_settings(None)


@pytest.fixture
def clock() -> FrozenClock:
    return FrozenClock(NOW)


@pytest.fixture
def tracker(clock: FrozenClock) -> RecordingTracker:
    return RecordingTracker(clock)


class SpyAlerter:
    def __init__(self) -> None:
        self.sent: list[tuple[Severity, str, str, str]] = []

    def send(self, severity: Severity, title: str, body: str, dedup_key: str) -> AlertOutcome:
        self.sent.append((severity, title, body, dedup_key))
        return AlertOutcome.SENT


@pytest.fixture
def build(clock: FrozenClock, settings: Settings, register: SourceRegister, tmp_path: Path) -> Any:
    """Build a real `Fetcher` over a recorded transport and a real L0 store under `tmp_path`."""

    def _build(
        script: dict[str, ScriptedOutcome | list[ScriptedOutcome]],
    ) -> tuple[Fetcher, L0Store]:
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
        return fetcher, l0

    return _build


def ok_csv(path: Path, repo_root: Path) -> RecordedResponse:
    return RecordedResponse(
        body=(repo_root / path).read_bytes(),
        headers={"content-type": "application/octet-stream"},
    )


def parse_fixture(
    path: Path, repo_root: Path, *, slug: str, name: str, as_of: date
) -> ConstituentSnapshot:
    return parse_constituents(
        (repo_root / path).read_bytes(),
        index_slug=slug,
        index_name=name,
        as_of=as_of,
        filename=path.name,
    )


# ── the constituents CSV parses ────────────────────────────────────────────────────────────────


def test_the_nifty50_list_parses_every_constituent(repo_root: Path) -> None:
    snap = parse_fixture(NIFTY50_JUL, repo_root, slug="nifty50", name="NIFTY 50", as_of=JUL)
    assert len(snap.rows) == 10
    reliance = next(r for r in snap.rows if r.isin == RELIANCE)
    assert reliance.symbol == "RELIANCE"
    assert reliance.series == "EQ"
    assert reliance.industry == "Oil Gas & Consumable Fuels"
    assert KOTAKBANK in snap.members
    assert ADANIENT not in snap.members


def test_constituents_are_isin_keyed_not_symbol(repo_root: Path) -> None:
    """Membership is a set of ISINs (invariant #2), never symbols."""
    snap = parse_fixture(NIFTY50_JUL, repo_root, slug="nifty50", name="NIFTY 50", as_of=JUL)
    assert all(m.startswith("INE") for m in snap.members)


def test_a_sectoral_list_parses_too(repo_root: Path) -> None:
    snap = parse_fixture(NIFTYIT_AUG, repo_root, slug="niftyit", name="NIFTY IT", as_of=AUG)
    assert len(snap.rows) == 5
    assert {r.symbol for r in snap.rows} == {"TCS", "INFY", "HCLTECH", "WIPRO", "TECHM"}


def test_a_wrong_header_is_rejected(repo_root: Path) -> None:
    with pytest.raises(ParseError, match="unexpected header"):
        parse_constituents(
            b"Name,Ticker,ISIN\nX,Y,INE002A01018\n",
            index_slug="nifty50",
            index_name="NIFTY 50",
            as_of=JUL,
            filename="bad.csv",
        )


def test_a_malformed_isin_names_the_line(repo_root: Path) -> None:
    body = b"Company Name,Industry,Symbol,Series,ISIN Code\nBad Co,Misc,BAD,EQ,NOTANISIN\n"
    with pytest.raises(ParseError, match=r"bad\.csv:2"):
        parse_constituents(
            body, index_slug="nifty50", index_name="NIFTY 50", as_of=JUL, filename="bad.csv"
        )


def test_a_duplicate_isin_is_rejected(repo_root: Path) -> None:
    body = (
        b"Company Name,Industry,Symbol,Series,ISIN Code\n"
        b"Reliance,Oil,RELIANCE,EQ,INE002A01018\n"
        b"Reliance Again,Oil,RELIANCE,EQ,INE002A01018\n"
    )
    with pytest.raises(ParseError, match="listed more than once"):
        parse_constituents(
            body, index_slug="nifty50", index_name="NIFTY 50", as_of=JUL, filename="dup.csv"
        )


def test_an_html_soft_404_is_not_a_membership(repo_root: Path) -> None:
    with pytest.raises(ParseError, match="markup, not CSV"):
        parse_constituents(
            b"<!DOCTYPE html><html><body>app</body></html>",
            index_slug="nifty50",
            index_name="NIFTY 50",
            as_of=JUL,
            filename="soft404.csv",
        )


# ── acceptance 1: a snapshot is stored immutably with its as-of date ───────────────────────────


def test_a_snapshot_is_written_and_read_back_with_its_as_of(
    repo_root: Path, tmp_path: Path
) -> None:
    snap = parse_fixture(NIFTY50_JUL, repo_root, slug="nifty50", name="NIFTY 50", as_of=JUL)
    write_constituents_l1(snap, data_root=tmp_path)
    back = read_constituents_l1("nifty50", JUL, data_root=tmp_path)
    assert back.as_of == JUL
    assert back.index_name == "NIFTY 50"
    assert back.members == snap.members


def test_re_writing_the_same_membership_is_a_noop(repo_root: Path, tmp_path: Path) -> None:
    snap = parse_fixture(NIFTY50_JUL, repo_root, slug="nifty50", name="NIFTY 50", as_of=JUL)
    write_constituents_l1(snap, data_root=tmp_path)
    # Idempotent re-derivation from the same bytes must not raise.
    write_constituents_l1(snap, data_root=tmp_path)
    assert read_constituents_l1("nifty50", JUL, data_root=tmp_path).members == snap.members


def test_overwriting_a_stored_month_with_different_membership_raises(
    repo_root: Path, tmp_path: Path
) -> None:
    """§4.1: never overwrite a prior month's membership — the immutability contract."""
    original = parse_fixture(NIFTY50_JUL, repo_root, slug="nifty50", name="NIFTY 50", as_of=JUL)
    write_constituents_l1(original, data_root=tmp_path)
    # A *different* membership dated to the same month must be refused, not silently written.
    mutated = parse_constituents(
        (repo_root / NIFTY50_AUG).read_bytes(),
        index_slug="nifty50",
        index_name="NIFTY 50",
        as_of=JUL,  # same month, different members (drops KOTAKBANK, adds ADANIENT)
        filename=NIFTY50_AUG.name,
    )
    with pytest.raises(ImmutableSnapshotError, match="immutable"):
        write_constituents_l1(mutated, data_root=tmp_path)
    # The original survives untouched.
    assert KOTAKBANK in read_constituents_l1("nifty50", JUL, data_root=tmp_path).members


# ── acceptance 2: membership as-of a historical date returns the snapshot in force then ─────────


@pytest.fixture
def two_months(repo_root: Path, tmp_path: Path) -> Path:
    """Two monthly snapshots whose membership differs, plus a sectoral index in one partition."""
    write_constituents_l1(
        parse_fixture(NIFTY50_JUL, repo_root, slug="nifty50", name="NIFTY 50", as_of=JUL),
        data_root=tmp_path,
    )
    write_constituents_l1(
        parse_fixture(NIFTY50_AUG, repo_root, slug="nifty50", name="NIFTY 50", as_of=AUG),
        data_root=tmp_path,
    )
    write_constituents_l1(
        parse_fixture(NIFTYIT_AUG, repo_root, slug="niftyit", name="NIFTY IT", as_of=AUG),
        data_root=tmp_path,
    )
    return tmp_path


def test_membership_asof_returns_the_snapshot_in_force(two_months: Path) -> None:
    """A mid-July date sees July's membership; a mid-August date sees August's."""
    jul_view = membership_asof("nifty50", date(2026, 7, 15), data_root=two_months)
    aug_view = membership_asof("nifty50", date(2026, 8, 15), data_root=two_months)
    assert jul_view is not None and aug_view is not None
    assert KOTAKBANK in jul_view.members and ADANIENT not in jul_view.members
    assert ADANIENT in aug_view.members and KOTAKBANK not in aug_view.members
    assert jul_view.as_of == JUL and aug_view.as_of == AUG


def test_membership_before_the_first_snapshot_is_none_not_todays_list(two_months: Path) -> None:
    """The survivorship-bias guard: no snapshot before June means no membership, not today's."""
    assert membership_asof("nifty50", date(2026, 6, 30), data_root=two_months) is None


def test_membership_asof_never_leaks_a_future_snapshot(two_months: Path) -> None:
    """Invariant #7: a date before August cannot see the August snapshot."""
    view = membership_asof("nifty50", date(2026, 7, 31), data_root=two_months)
    assert view is not None and view.as_of == JUL
    assert ADANIENT not in view.members


def test_exact_as_of_date_sees_that_snapshot(two_months: Path) -> None:
    view = membership_asof("nifty50", AUG, data_root=two_months)
    assert view is not None and view.as_of == AUG


def test_two_indices_coexist_in_one_date_partition(two_months: Path) -> None:
    """The August partition holds both nifty50 and niftyit, each in its own file."""
    it_view = membership_asof("niftyit", AUG, data_root=two_months)
    assert it_view is not None
    assert {r.symbol for r in it_view.rows} == {"TCS", "INFY", "HCLTECH", "WIPRO", "TECHM"}


# ── acceptance 1+2 end to end: the constituents runner ─────────────────────────────────────────


def test_the_runner_takes_a_snapshot_to_published(
    build: Any, tracker: RecordingTracker, register: SourceRegister, repo_root: Path, tmp_path: Path
) -> None:
    url = constituents_url("nifty50", register)
    fetcher, l0 = build({url: ok_csv(NIFTY50_JUL, repo_root)})
    snap = ingest_constituents(
        fetcher=fetcher,
        l0=l0,
        tracker=tracker,
        index_slug="nifty50",
        index_name="NIFTY 50",
        as_of=JUL,
        data_root=tmp_path,
        register=register,
    )
    assert tracker.history == [
        SyncState.PENDING,
        SyncState.FETCHED,
        SyncState.VALIDATED,
        SyncState.NORMALIZED,
        SyncState.PUBLISHED,
    ]
    assert read_constituents_l1("nifty50", JUL, data_root=tmp_path).members == snap.members


# ── the close-all snapshot (§4.1's TRI fallback input) ─────────────────────────────────────────


def test_close_snapshot_parses_close_and_div_yield(repo_root: Path) -> None:
    rows = parse_close_snapshot((repo_root / CLOSE_03).read_bytes(), filename=CLOSE_03.name)
    nifty = next(r for r in rows if r.index_name == "Nifty 50")
    assert nifty.index_date == date(2026, 8, 3)
    assert nifty.close == Decimal("24000.00")
    assert nifty.div_yield == Decimal("1.20")
    assert isinstance(nifty.close, Decimal)


def test_a_blank_div_yield_is_none_not_zero(repo_root: Path) -> None:
    body = (
        b"Index Name,Index Date,Open Index Value,High Index Value,Low Index Value,"
        b"Closing Index Value,Points Change,Change(%),Volume,Turnover (Rs. Cr.),P/E,P/B,Div Yield\n"
        b"Weird Idx,03-Aug-2026,1,1,1,100.00,0,0,0,0,0,0,-\n"
    )
    rows = parse_close_snapshot(body, filename="weird.csv")
    assert rows[0].div_yield is None  # a real absence, never 0


# ── acceptance 3: a NIFTY-TRI series is ingested and spot-checked against a published value ─────


def _nifty50_closes(repo_root: Path) -> list[IndexCloseRow]:
    out: list[IndexCloseRow] = []
    for path in (CLOSE_03, CLOSE_04, CLOSE_05):
        rows = parse_close_snapshot((repo_root / path).read_bytes(), filename=path.name)
        out.append(next(r for r in rows if r.index_name == "Nifty 50"))
    return out


def test_computed_tri_is_seeded_to_the_published_close(repo_root: Path) -> None:
    """Acceptance 3: the TRI series is anchored to a real published index value.

    The direct TRI endpoint is session-gated (register FAILED), so §4.1's computed fallback runs.
    Its first value is the published closing index value from the close-all snapshot — a published
    number, ingested exactly, not approximately.
    """
    series = compute_tri(_nifty50_closes(repo_root), index_slug="nifty50")
    seed = series.points[0]
    assert seed.as_of == date(2026, 8, 3)
    assert seed.tri_value == Decimal("24000.00")  # the published Nifty 50 close on 03-Aug
    assert seed.method == "computed_price_plus_div"


def test_dividends_lift_tri_above_the_price_return(repo_root: Path) -> None:
    """With a positive yield, day two's TRI exceeds the pure price return — the sign that matters.

    03→04-Aug is a +1.00% price move (24000 → 24240). The TRI adds one day of the 1.20% annual
    yield on top, so it must land strictly above 24240. If the dividend term were subtracted (an
    inverted sign) or dropped, this fails.
    """
    series = compute_tri(_nifty50_closes(repo_root), index_slug="nifty50")
    day2 = series.points[1]
    assert day2.as_of == date(2026, 8, 4)
    assert day2.tri_value > Decimal("24240.00")
    # And the excess is the accrued dividend, not something larger — a sanity band.
    assert day2.tri_value < Decimal("24242.00")


def test_zero_dividends_reproduce_the_price_index(repo_root: Path) -> None:
    """The invertibility check: with no dividends, TRI == the price index, exactly."""
    closes = _nifty50_closes(repo_root)
    no_div = [row.model_copy(update={"div_yield": Decimal(0)}) for row in closes]
    series = compute_tri(no_div, index_slug="nifty50")
    assert series.points[1].tri_value == Decimal("24240.00")  # pure price return, no dividend leg


def test_tri_rises_even_when_price_returns_flat(repo_root: Path) -> None:
    """05-Aug closes back at 24000 (flat vs 03-Aug), yet TRI is higher — dividends accrued."""
    series = compute_tri(_nifty50_closes(repo_root), index_slug="nifty50")
    assert series.points[2].price_close == Decimal("24000.00")
    assert series.points[2].tri_value > series.points[0].tri_value


def test_extend_tri_matches_a_full_recompute(repo_root: Path) -> None:
    """The daily-incremental path equals the batch path over the same days (determinism)."""
    closes = _nifty50_closes(repo_root)
    full = compute_tri(closes, index_slug="nifty50")
    partial = compute_tri(closes[:2], index_slug="nifty50")
    extended = extend_tri(partial, closes[2], prev_close=closes[1])
    assert [p.tri_value for p in extended.points] == [p.tri_value for p in full.points]
    assert [p.as_of for p in extended.points] == [p.as_of for p in full.points]


def test_tri_series_ingests_to_l1_and_reads_back_point_in_time(
    repo_root: Path, tmp_path: Path
) -> None:
    """The series is stored and a PIT read cannot see a future TRI value (invariant #7)."""
    ingest_tri_from_close(_nifty50_closes(repo_root), index_slug="nifty50", data_root=tmp_path)
    through_04 = read_tri_series("nifty50", date(2026, 8, 4), data_root=tmp_path)
    assert through_04 is not None
    assert [p.as_of for p in through_04.points] == [date(2026, 8, 3), date(2026, 8, 4)]
    # 05-Aug's value is physically absent from a read dated 04-Aug.
    assert all(p.as_of <= date(2026, 8, 4) for p in through_04.points)
    full = read_tri_series("nifty50", date(2026, 8, 5), data_root=tmp_path)
    assert full is not None and len(full.points) == 3


def test_tri_series_before_the_first_point_is_none(repo_root: Path, tmp_path: Path) -> None:
    ingest_tri_from_close(_nifty50_closes(repo_root), index_slug="nifty50", data_root=tmp_path)
    assert read_tri_series("nifty50", date(2026, 8, 2), data_root=tmp_path) is None


# ── URL helpers read the register, not a second copy in code (C.1) ─────────────────────────────


def test_urls_come_from_the_register(register: SourceRegister) -> None:
    assert constituents_url("nifty50", register).endswith("ind_nifty50list.csv")
    assert constituents_url("niftyit", register).endswith("ind_niftyitlist.csv")
    assert close_snapshot_url(date(2026, 8, 3), register).endswith("ind_close_all_03082026.csv")
    assert tri_url(register).endswith("/BackPage/getTotalReturnIndexString")


def test_l0_filename_carries_the_snapshot_date(register: SourceRegister) -> None:
    """The URL has no date, so the L0 name must, or two months collide (`L0Store.put`)."""
    assert l0_constituents_filename("nifty50", AUG) == "ind_nifty50list_20260801.csv"
