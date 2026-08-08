"""The crawl policy engine behaves like a guest, not an intruder (§4.1, M1.2).

Four properties are load-bearing, and each is asserted here rather than assumed:

1. **Spacing.** Requests to one host are at least the register's minimum apart — proved against a
   `FrozenClock` and a sleeper that records instead of sleeping, so the three-second gap costs
   microseconds and cannot become flaky on a slow machine.
2. **The hard stop.** A 403 spike stops this process talking to that host and wakes a human. The
   tests check both the behaviour and the *absence* of the alternative: no second user agent
   exists in the module, headers are byte-identical on every attempt, and a 403 is never retried.
3. **L0 first.** `fetch` returns an `L0Ref` whose payload is already on disk with a matching
   checksum. The caller is never handed bytes, so nothing can parse a response that was not
   stored (invariant #1).
4. **Offline.** The socket ban below is asserted, not hoped for: every response comes from a
   `RecordedTransport` (B8).

The register these tests read is the real checked-in `source_register.yaml`, so a C.1 row that
drifts out of shape — a host losing its robots record, a source losing its browser agent — fails
here rather than at 3 a.m. against a live source. Its *hosts* are never contacted.
"""

from __future__ import annotations

import hashlib
import socket
from collections.abc import Iterator
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any, Final

import pytest

from dataplatform.alerts import Alerter, AlertOutcome, Severity
from dataplatform.clock import IST, Clock, FrozenClock
from dataplatform.config import Settings
from dataplatform.ingest import fetcher as fetcher_module
from dataplatform.ingest import policy as policy_module
from dataplatform.ingest.fetcher import (
    Fetcher,
    FetchHTTPError,
    ForbiddenError,
    ForbiddenSpikeError,
    RecordedResponse,
    RecordedTransport,
    RetryableFetchError,
    ServerError,
    TransportError,
    UnrecordedRequestError,
)
from dataplatform.ingest.policy import (
    MIN_SPACING_FLOOR_SECONDS,
    HostMismatchError,
    RateLimiter,
    RobotsDisallowedError,
    RobotsPolicy,
    UnknownSourceError,
    resolve_policy,
)
from dataplatform.ingest.source_register import SourceRegister
from dataplatform.ingest.source_register import load as load_register
from dataplatform.store.l0 import L0Ref, L0Store
from tests.conftest import SettingsLoader

# ── the URLs the C.1 sweep verified; nothing here ever requests them ──────────────────────────
ARCHIVES: Final = "nsearchives.nseindia.com"
LEGACY: Final = "https://nsearchives.nseindia.com/content/historical/EQUITIES/2024/JAN/"
UDIFF: Final = (
    "https://nsearchives.nseindia.com/content/cm/BhavCopy_NSE_CM_0_0_0_20260807_F_0000.csv.zip"
)
CORP_ACTIONS: Final = (
    "https://www.nseindia.com/api/corporates-corporateActions"
    "?index=equities&from_date=01-08-2026&to_date=07-08-2026"
)
WARM: Final = "https://www.nseindia.com/"
SCREENER_COMPANY: Final = "https://www.screener.in/company/INFY/"

NOW: Final = datetime(2026, 8, 7, 18, 0, tzinfo=IST)
TRADE_DATE: Final = date(2026, 8, 7)
ZIP_BYTES: Final = b"PK\x03\x04 not really a zip, but bytes are bytes"


def legacy_url(day: int) -> str:
    """The legacy bhavcopy URL for a January 2024 session."""
    return f"{LEGACY}cm{day:02d}JAN2024bhav.csv.zip"


class Sleeps:
    """A sleeper that records what it was asked to wait and moves the frozen clock instead.

    Both the rate limiter and tenacity's backoff sleep through this one callable, so `calls` is
    the complete waiting history of a fetch in order — which is exactly what a test about spacing
    composing with backoff needs to see.
    """

    def __init__(self, clock: FrozenClock) -> None:
        self.clock = clock
        self.calls: list[float] = []

    def __call__(self, seconds: float) -> None:
        self.calls.append(round(seconds, 3))
        self.clock.advance(timedelta(seconds=seconds))


class SpyAlerter:
    """Records alerts instead of delivering them, and satisfies the C.3 `Alerter` protocol."""

    def __init__(self) -> None:
        self.sent: list[tuple[Severity, str, str, str]] = []

    def send(self, severity: Severity, title: str, body: str, dedup_key: str) -> AlertOutcome:
        self.sent.append((severity, title, body, dedup_key))
        return AlertOutcome.SENT


@pytest.fixture(autouse=True)
def no_network(monkeypatch: pytest.MonkeyPatch) -> None:
    """Any socket in this module is a bug: every response is scripted (B8, acceptance 4)."""

    def forbidden(*args: Any, **kwargs: Any) -> Any:
        raise AssertionError("ingestion tests must never touch the network")

    monkeypatch.setattr(socket, "socket", forbidden)
    monkeypatch.setattr(socket, "create_connection", forbidden)


@pytest.fixture(scope="session")
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
def sleeps(clock: FrozenClock) -> Sleeps:
    return Sleeps(clock)


@pytest.fixture
def alerter() -> SpyAlerter:
    return SpyAlerter()


@pytest.fixture
def store(clock: FrozenClock, tmp_path: Path) -> L0Store:
    return L0Store(clock=clock, data_root=tmp_path)


@pytest.fixture
def build(
    store: L0Store,
    alerter: SpyAlerter,
    clock: FrozenClock,
    sleeps: Sleeps,
    register: SourceRegister,
    settings: Settings,
) -> Iterator[Any]:
    """Builds a `Fetcher` over a scripted transport, wired to the frozen clock and spy alerter."""

    def make(script: dict[str, Any]) -> tuple[Fetcher, RecordedTransport]:
        transport = RecordedTransport(script)
        return (
            Fetcher(
                transport=transport,
                l0=store,
                alerter=alerter,
                clock=clock,
                register=register,
                settings=settings,
                sleep=sleeps,
            ),
            transport,
        )

    yield make


def ok(body: bytes = ZIP_BYTES, content_type: str = "application/zip") -> RecordedResponse:
    """A 200 carrying `body`."""
    return RecordedResponse(status_code=200, body=body, headers={"Content-Type": content_type})


# ── acceptance 1: the rate limiter enforces per-host spacing, on a fake clock ─────────────────


def test_the_first_request_to_a_host_never_waits(clock: FrozenClock, sleeps: Sleeps) -> None:
    limiter = RateLimiter(clock=clock, sleep=sleeps)
    assert limiter.acquire(ARCHIVES, 3.0) == 0.0
    assert sleeps.calls == []


def test_a_second_request_waits_the_whole_interval(clock: FrozenClock, sleeps: Sleeps) -> None:
    limiter = RateLimiter(clock=clock, sleep=sleeps)
    limiter.acquire(ARCHIVES, 3.0)
    assert limiter.acquire(ARCHIVES, 3.0) == pytest.approx(3.0)
    assert sleeps.calls == [3.0]
    assert clock.now() == NOW + timedelta(seconds=3)


def test_time_already_spent_counts_toward_the_interval(clock: FrozenClock, sleeps: Sleeps) -> None:
    limiter = RateLimiter(clock=clock, sleep=sleeps)
    limiter.acquire(ARCHIVES, 3.0)
    clock.advance(timedelta(seconds=1.2))
    assert limiter.acquire(ARCHIVES, 3.0) == pytest.approx(1.8)


def test_a_host_left_alone_long_enough_is_not_made_to_wait(
    clock: FrozenClock, sleeps: Sleeps
) -> None:
    limiter = RateLimiter(clock=clock, sleep=sleeps)
    limiter.acquire(ARCHIVES, 3.0)
    clock.advance(timedelta(seconds=30))
    assert limiter.acquire(ARCHIVES, 3.0) == 0.0
    assert sleeps.calls == []


def test_hosts_are_spaced_independently(clock: FrozenClock, sleeps: Sleeps) -> None:
    """Spacing is a promise to one host; waiting for NSE must not slow BSE down."""
    limiter = RateLimiter(clock=clock, sleep=sleeps)
    limiter.acquire(ARCHIVES, 3.0)
    assert limiter.acquire("www.bseindia.com", 3.0) == 0.0
    assert sleeps.calls == []


def test_three_fetches_from_one_host_are_spaced_by_the_configured_minimum(
    build: Any, sleeps: Sleeps, clock: FrozenClock
) -> None:
    """The acceptance criterion end to end: consecutive fetches are 3 s apart, on a fake clock."""
    urls = [legacy_url(day) for day in (2, 3, 4)]
    fetcher, transport = build(dict.fromkeys(urls, ok()))

    started = clock.now()
    for day, url in zip((2, 3, 4), urls, strict=True):
        fetcher.fetch("nse_bhavcopy_legacy", url, date(2024, 1, day))

    assert [request.url for request in transport.requests] == urls
    assert sleeps.calls == [3.0, 3.0]
    assert clock.now() - started == timedelta(seconds=6)


def test_every_registered_source_is_spaced_at_least_the_plan_floor(
    register: SourceRegister, settings: Settings
) -> None:
    """§4.1 says 2-3 s per host; a register row may be slower than that, never faster."""
    for source in register.sources:
        policy = resolve_policy(source.id, register, settings)
        assert policy.min_interval_seconds >= MIN_SPACING_FLOOR_SECONDS, source.id
        assert policy.min_interval_seconds >= settings.http_min_interval_seconds, source.id


def test_a_registered_host_that_asks_for_more_spacing_gets_it(
    register: SourceRegister, settings: Settings
) -> None:
    """Screener's 5 s beats the configured 3 s: the binding limit is whichever is slowest."""
    policy = resolve_policy("screener_company_fundamentals", register, settings)
    assert policy.min_interval_seconds == 5.0


# ── acceptance 2: a 403 spike hard-stops and alerts, and nothing evades ───────────────────────


def test_a_403_spike_trips_a_hard_stop_and_alerts(
    build: Any, alerter: SpyAlerter, settings: Settings
) -> None:
    fetcher, transport = build({UDIFF: RecordedResponse(status_code=403)})
    assert isinstance(alerter, Alerter)
    limit = settings.http_forbidden_streak_limit

    for _ in range(limit - 1):
        with pytest.raises(ForbiddenError):
            fetcher.fetch("nse_bhavcopy_udiff", UDIFF, TRADE_DATE)
    assert not fetcher.is_stopped(ARCHIVES)
    assert alerter.sent == []

    with pytest.raises(ForbiddenSpikeError, match="stopped for the life of this process"):
        fetcher.fetch("nse_bhavcopy_udiff", UDIFF, TRADE_DATE)

    assert fetcher.is_stopped(ARCHIVES)
    assert len(transport.requests) == limit
    [(severity, title, body, dedup_key)] = alerter.sent
    assert severity is Severity.CRITICAL
    assert ARCHIVES in title
    assert dedup_key == f"ingest:403-spike:{ARCHIVES}"
    assert "user-agent rotation" in body


def test_a_hard_stopped_host_is_never_requested_again(
    build: Any, settings: Settings, tmp_path: Path
) -> None:
    """The stop belongs to the host, not the call: another source on it is refused too."""
    fetcher, transport = build(
        {UDIFF: RecordedResponse(status_code=403), legacy_url(2): ok()},
    )
    for _ in range(settings.http_forbidden_streak_limit - 1):
        with pytest.raises(ForbiddenError):
            fetcher.fetch("nse_bhavcopy_udiff", UDIFF, TRADE_DATE)
    with pytest.raises(ForbiddenSpikeError):
        fetcher.fetch("nse_bhavcopy_udiff", UDIFF, TRADE_DATE)

    requests_before = len(transport.requests)
    with pytest.raises(ForbiddenSpikeError):
        fetcher.fetch("nse_bhavcopy_legacy", legacy_url(2), date(2024, 1, 2))

    assert len(transport.requests) == requests_before
    assert list(tmp_path.rglob("*.zip")) == []


def test_a_success_clears_the_403_streak(build: Any, settings: Settings) -> None:
    """Refusals interleaved with answers are a flaky endpoint, not a block — do not stop on it."""
    assert settings.http_forbidden_streak_limit == 3
    fetcher, _ = build(
        {
            UDIFF: [RecordedResponse(status_code=403), RecordedResponse(status_code=403)],
            legacy_url(2): ok(),
        }
    )
    for _ in range(2):
        with pytest.raises(ForbiddenError):
            fetcher.fetch("nse_bhavcopy_udiff", UDIFF, TRADE_DATE)

    fetcher.fetch("nse_bhavcopy_legacy", legacy_url(2), date(2024, 1, 2))

    with pytest.raises(ForbiddenError):
        fetcher.fetch("nse_bhavcopy_udiff", UDIFF, TRADE_DATE)
    assert not fetcher.is_stopped(ARCHIVES)


def test_a_403_is_never_retried(build: Any, settings: Settings) -> None:
    """Five attempts are budgeted for a timeout; a refusal gets exactly one."""
    assert settings.http_max_attempts > 1
    fetcher, transport = build({UDIFF: RecordedResponse(status_code=403)})
    with pytest.raises(ForbiddenError):
        fetcher.fetch("nse_bhavcopy_udiff", UDIFF, TRADE_DATE)
    assert len(transport.requests) == 1
    assert not issubclass(ForbiddenError, RetryableFetchError)
    assert not issubclass(ForbiddenSpikeError, RetryableFetchError)


def test_every_attempt_presents_the_same_identity(
    build: Any, settings: Settings, register: SourceRegister
) -> None:
    """The anti-evasion property: refusals change nothing about how the next request looks."""
    fetcher, transport = build({UDIFF: RecordedResponse(status_code=403)})
    for _ in range(settings.http_forbidden_streak_limit - 1):
        with pytest.raises(ForbiddenError):
            fetcher.fetch("nse_bhavcopy_udiff", UDIFF, TRADE_DATE)
    with pytest.raises(ForbiddenSpikeError):
        fetcher.fetch("nse_bhavcopy_udiff", UDIFF, TRADE_DATE)

    sent = [request.headers for request in transport.requests]
    assert len({tuple(sorted(headers.items())) for headers in sent}) == 1
    assert {headers["User-Agent"] for headers in sent} == {settings.http_user_agent}


def test_no_second_user_agent_exists_to_rotate_to() -> None:
    """Mechanical: the engine has one agent (from `Settings`) and no source of randomness."""
    for module in (fetcher_module, policy_module):
        source = Path(str(module.__file__)).read_text(encoding="utf-8")
        code = "\n".join(line for line in source.splitlines() if not line.lstrip().startswith("#"))
        assert "Mozilla" not in code, f"{module.__name__} contains a user-agent literal"
        assert "import random" not in code, f"{module.__name__} imports randomness"
        assert "user_agents" not in code, f"{module.__name__} looks like it holds a UA list"


def test_the_alert_channel_failing_does_not_swallow_the_hard_stop(
    store: L0Store,
    clock: FrozenClock,
    sleeps: Sleeps,
    register: SourceRegister,
    settings: Settings,
) -> None:
    """A broken alerter must not become a reason to keep crawling a host that refused us."""

    class BrokenAlerter:
        def send(self, severity: Severity, title: str, body: str, dedup_key: str) -> AlertOutcome:
            raise RuntimeError("smtp is down")

    transport = RecordedTransport({UDIFF: RecordedResponse(status_code=403)})
    fetcher = Fetcher(
        transport=transport,
        l0=store,
        alerter=BrokenAlerter(),
        clock=clock,
        register=register,
        settings=settings,
        sleep=sleeps,
    )
    for _ in range(settings.http_forbidden_streak_limit - 1):
        with pytest.raises(ForbiddenError):
            fetcher.fetch("nse_bhavcopy_udiff", UDIFF, TRADE_DATE)
    with pytest.raises(ForbiddenSpikeError):
        fetcher.fetch("nse_bhavcopy_udiff", UDIFF, TRADE_DATE)
    assert fetcher.is_stopped(ARCHIVES)


def test_the_forbidden_window_forgets_stale_refusals(
    build: Any, clock: FrozenClock, settings: Settings
) -> None:
    """Three 403s a day apart are not a spike; the streak is counted inside a window."""
    fetcher, _ = build({UDIFF: RecordedResponse(status_code=403)})
    for _ in range(settings.http_forbidden_streak_limit + 2):
        with pytest.raises(ForbiddenError):
            fetcher.fetch("nse_bhavcopy_udiff", UDIFF, TRADE_DATE)
        clock.advance(timedelta(days=1))
    assert not fetcher.is_stopped(ARCHIVES)


# ── acceptance 3: the body is in L0, checksummed, before the caller sees anything ─────────────


def test_fetch_returns_a_ref_whose_payload_is_already_stored_and_checksummed(
    build: Any, store: L0Store, clock: FrozenClock
) -> None:
    fetcher, _ = build({UDIFF: ok(body=ZIP_BYTES)})
    ref = fetcher.fetch("nse_bhavcopy_udiff", UDIFF, TRADE_DATE)

    assert isinstance(ref, L0Ref)
    assert ref.sha256 == hashlib.sha256(ZIP_BYTES).hexdigest()
    assert ref.size_bytes == len(ZIP_BYTES)
    assert ref.content_type == "application/zip"
    assert ref.filename == "BhavCopy_NSE_CM_0_0_0_20260807_F_0000.csv.zip"
    assert ref.fetched_at == clock.now()
    assert store.path_of(ref).read_bytes() == ZIP_BYTES
    assert store.get(ref) == ZIP_BYTES  # re-verifies the checksum on the way out


def test_the_caller_is_handed_a_reference_and_never_the_bytes(build: Any) -> None:
    """A parser that cannot be given a response cannot parse one that was never stored (#1)."""
    fetcher, _ = build({UDIFF: ok()})
    ref = fetcher.fetch("nse_bhavcopy_udiff", UDIFF, TRADE_DATE)
    assert set(type(ref).model_fields) == {
        "source",
        "logical_date",
        "filename",
        "sha256",
        "size_bytes",
        "fetched_at",
        "content_type",
    }
    assert not hasattr(ref, "body")


def test_a_failed_l0_write_fails_the_fetch(
    build: Any, store: L0Store, monkeypatch: pytest.MonkeyPatch
) -> None:
    """If the bytes could not be stored, the caller gets an error rather than a ref."""

    def refuse(*args: Any, **kwargs: Any) -> L0Ref:
        raise OSError("disk full")

    monkeypatch.setattr(store, "put", refuse)
    fetcher, _ = build({UDIFF: ok()})
    with pytest.raises(OSError, match="disk full"):
        fetcher.fetch("nse_bhavcopy_udiff", UDIFF, TRADE_DATE)


def test_the_content_type_the_source_declared_is_what_l0_records(build: Any) -> None:
    fetcher, _ = build({UDIFF: RecordedResponse(status_code=200, body=b"x", headers={})})
    ref = fetcher.fetch("nse_bhavcopy_udiff", UDIFF, TRADE_DATE)
    assert ref.content_type == "application/octet-stream"


def test_a_url_with_no_usable_filename_is_refused_rather_than_named_by_the_fetcher(
    build: Any,
) -> None:
    fetcher, transport = build({WARM: ok()})
    with pytest.raises(ValueError, match="filename="):
        fetcher.fetch("nse_corp_actions", WARM, TRADE_DATE)
    assert transport.requests == []


def test_an_api_url_carrying_no_date_takes_an_explicit_filename(build: Any, store: L0Store) -> None:
    """L0 partitions by month, so a daily API payload needs a name that distinguishes the day."""
    fetcher, _ = build(
        {WARM: ok(body=b"<html>"), CORP_ACTIONS: ok(body=b"{}", content_type="application/json")}
    )
    first = fetcher.fetch(
        "nse_corp_actions", CORP_ACTIONS, TRADE_DATE, filename="corp_actions_2026-08-07.json"
    )
    assert first.filename == "corp_actions_2026-08-07.json"
    assert store.get(first) == b"{}"


# ── acceptance 4: everything above ran offline; robots and hosts are checked before requests ──


def test_a_robots_disallowed_url_is_refused_before_any_request_exists(build: Any) -> None:
    """Screener disallows `?page=`; AGENTIC_CONTEXT §8 makes respecting that non-negotiable."""
    fetcher, transport = build({})
    with pytest.raises(RobotsDisallowedError, match=r"\?page="):
        fetcher.fetch(
            "screener_company_fundamentals",
            "https://www.screener.in/company/INFY/?page=2",
            TRADE_DATE,
        )
    assert transport.requests == []


def test_the_permitted_screener_surface_is_still_allowed(
    register: SourceRegister, settings: Settings
) -> None:
    policy = resolve_policy("screener_company_fundamentals", register, settings)
    policy.check_url(SCREENER_COMPANY)
    assert policy.robots.allows(SCREENER_COMPANY)


def test_the_one_path_nse_disallows_is_refused(
    register: SourceRegister, settings: Settings
) -> None:
    policy = resolve_policy("nse_corp_actions", register, settings)
    assert not policy.robots.allows("https://www.nseindia.com/market-data-test")
    assert policy.robots.allows(CORP_ACTIONS)


@pytest.mark.parametrize(
    ("rule", "url", "allowed"),
    [
        ("/user/*", "https://h.test/user/vysh", False),
        ("/user/*", "https://h.test/users", True),
        ("/*?q=", "https://h.test/company/INFY/?q=x", False),
        ("/*?q=", "https://h.test/company/INFY/", True),
        ("/exact$", "https://h.test/exact", False),
        ("/exact$", "https://h.test/exactly", True),
        ("/dir", "https://h.test/dir/deep/page", False),
    ],
)
def test_robots_wildcards_and_anchors_match_the_standard(
    rule: str, url: str, allowed: bool
) -> None:
    robots = RobotsPolicy(host="h.test", disallow=(rule,), permits="test")
    assert robots.allows(url) is allowed


def test_a_url_on_another_host_is_refused(build: Any) -> None:
    """A source's policy — its spacing, its robots record — describes one host only."""
    fetcher, transport = build({})
    with pytest.raises(HostMismatchError):
        fetcher.fetch("nse_bhavcopy_udiff", "https://example.test/bhav.zip", TRADE_DATE)
    assert transport.requests == []


def test_an_unknown_source_id_is_refused(build: Any) -> None:
    fetcher, _ = build({})
    with pytest.raises(UnknownSourceError, match="nse_bhavcopy_legacy"):
        fetcher.fetch("nse_bhavcopy_imaginary", UDIFF, TRADE_DATE)


def test_the_recorded_transport_refuses_a_url_nobody_scripted(build: Any) -> None:
    """A fetch reaching an unexpected URL is the bug this transport exists to surface."""
    fetcher, _ = build({legacy_url(2): ok()})
    with pytest.raises(UnrecordedRequestError):
        fetcher.fetch("nse_bhavcopy_udiff", UDIFF, TRADE_DATE)


# ── the warm NSE session (§4.1) ───────────────────────────────────────────────────────────────


def test_a_cookie_source_visits_the_site_before_asking_for_data(build: Any) -> None:
    fetcher, transport = build({WARM: ok(body=b"<html>"), CORP_ACTIONS: ok(body=b"{}")})
    fetcher.fetch("nse_corp_actions", CORP_ACTIONS, TRADE_DATE, filename="ca-2026-08-07.json")
    assert [request.url for request in transport.requests] == [WARM, CORP_ACTIONS]


def test_the_session_is_warmed_once_per_process(build: Any) -> None:
    fetcher, transport = build({WARM: ok(body=b"<html>"), CORP_ACTIONS: ok(body=b"{}")})
    for day in (6, 7):
        fetcher.fetch(
            "nse_corp_actions", CORP_ACTIONS, date(2026, 8, day), filename=f"ca-{day}.json"
        )
    assert [request.url for request in transport.requests].count(WARM) == 1


def test_the_warm_up_response_is_never_stored_in_l0(build: Any, tmp_path: Path) -> None:
    """It is a handshake, not data: an hourly-changing homepage under one L0 key would conflict
    with itself on the second day (invariant #1)."""
    fetcher, _ = build({WARM: ok(body=b"<html>"), CORP_ACTIONS: ok(body=b"{}")})
    fetcher.fetch("nse_corp_actions", CORP_ACTIONS, TRADE_DATE, filename="ca-2026-08-07.json")
    stored = sorted(path.name for path in tmp_path.rglob("*") if path.is_file())
    assert stored == ["ca-2026-08-07.json", "ca-2026-08-07.json.meta.json"]


def test_an_archive_source_does_not_warm_a_session(build: Any) -> None:
    """The register says the archive host needs no cookie; an extra request is not politeness."""
    fetcher, transport = build({UDIFF: ok()})
    fetcher.fetch("nse_bhavcopy_udiff", UDIFF, TRADE_DATE)
    assert [request.url for request in transport.requests] == [UDIFF]


# ── retries: the failures that mean "later" ───────────────────────────────────────────────────


def test_a_5xx_is_retried_with_exponential_backoff_on_top_of_the_spacing(
    build: Any, sleeps: Sleeps, clock: FrozenClock
) -> None:
    """Backoff and spacing compose: the gap between two attempts is never under the minimum."""
    fetcher, transport = build(
        {
            UDIFF: [
                RecordedResponse(status_code=503),
                RecordedResponse(status_code=500),
                ok(),
            ]
        }
    )
    started = clock.now()
    ref = fetcher.fetch("nse_bhavcopy_udiff", UDIFF, TRADE_DATE)

    assert ref.size_bytes == len(ZIP_BYTES)
    assert len(transport.requests) == 3
    # backoff 1 s, then spacing tops it up to 3 s; backoff 2 s, then spacing tops it up again.
    assert sleeps.calls == [1.0, 2.0, 2.0, 1.0]
    assert clock.now() - started == timedelta(seconds=6)


def test_a_timeout_is_retried(build: Any) -> None:
    fetcher, transport = build({UDIFF: [TransportError("read timed out"), ok()]})
    fetcher.fetch("nse_bhavcopy_udiff", UDIFF, TRADE_DATE)
    assert len(transport.requests) == 2


def test_the_retry_budget_is_finite_and_the_last_error_reaches_the_caller(
    build: Any, settings: Settings
) -> None:
    fetcher, transport = build({UDIFF: RecordedResponse(status_code=502)})
    with pytest.raises(ServerError, match="502"):
        fetcher.fetch("nse_bhavcopy_udiff", UDIFF, TRADE_DATE)
    assert len(transport.requests) == settings.http_max_attempts


def test_a_429_is_a_request_to_slow_down_not_a_dead_end(build: Any) -> None:
    fetcher, transport = build({UDIFF: [RecordedResponse(status_code=429), ok()]})
    fetcher.fetch("nse_bhavcopy_udiff", UDIFF, TRADE_DATE)
    assert len(transport.requests) == 2


def test_a_404_is_final(build: Any) -> None:
    fetcher, transport = build({UDIFF: RecordedResponse(status_code=404)})
    with pytest.raises(FetchHTTPError, match="404") as raised:
        fetcher.fetch("nse_bhavcopy_udiff", UDIFF, TRADE_DATE)
    assert raised.value.status_code == 404
    assert len(transport.requests) == 1


# ── policy resolution from the register (C.1 -> M1.2) ─────────────────────────────────────────


def test_the_register_user_agent_sentinel_resolves_to_the_configured_agent(
    register: SourceRegister, settings: Settings
) -> None:
    policy = resolve_policy("nse_bhavcopy_udiff", register, settings)
    assert policy.headers["User-Agent"] == settings.http_user_agent
    assert policy.headers["Referer"] == "https://www.nseindia.com/"


def test_the_rows_own_headers_are_sent_verbatim(build: Any) -> None:
    fetcher, transport = build({WARM: ok(), CORP_ACTIONS: ok(body=b"{}")})
    fetcher.fetch("nse_corp_actions", CORP_ACTIONS, TRADE_DATE, filename="ca.json")
    headers = transport.requests[-1].headers
    assert headers["Accept"] == "application/json, text/plain, */*"
    assert headers["X-Requested-With"] == "XMLHttpRequest"
    assert headers["Referer"] == "https://www.nseindia.com/"


def test_every_source_in_the_register_resolves_to_a_usable_policy(
    register: SourceRegister, settings: Settings
) -> None:
    """Guards the C.1/M1.2 seam: a row that loses its host record or its agent fails here."""
    assert register.sources
    for source in register.sources:
        policy = resolve_policy(source.id, register, settings)
        assert policy.host == source.host
        assert policy.headers["User-Agent"] == settings.http_user_agent
        assert policy.method in {"GET", "POST"}
        assert policy.robots.permits.strip()
        assert (policy.warm_url is not None) is source.needs_session_cookie


def test_a_clock_is_injected_rather_than_read(clock: FrozenClock) -> None:
    """B10: the fetcher's time comes from the same clock as the rest of the run."""
    assert isinstance(clock, Clock)
    assert clock.now() == NOW
