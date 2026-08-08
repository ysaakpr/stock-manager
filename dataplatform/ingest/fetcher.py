"""D1 crawl engine — the only code in the platform that fetches from a source (§4.1).

Everything the platform ever ingests comes through `Fetcher.fetch`, and what it hands back is an
`L0Ref`, never bytes and never parsed rows. That single design choice is what makes invariant #1
true in practice rather than in principle: a caller cannot parse a response it was never given,
so no L1 row can exist that was not derived from a checksummed payload already on disk. A parser
that wants the bytes reads them back out of L0, verified (`L0Store.get`).

The policy the fetcher enforces comes from `source_register.yaml` (C.1) via `policy.py`:

* browser user agent and the row's Referer on every request, identical on every attempt;
* a warm session for the NSE JSON endpoints — visit the site, keep the cookies, then ask for the
  data, exactly as the row's `needs_session_cookie` says;
* 2-3 s minimum spacing per host, enforced by `RateLimiter`;
* tenacity exponential backoff for timeouts, 5xx and 429 — the failures that mean "later";
* and, for 403, a hard stop: `ForbiddenWatch` counts refusals and, at the limit, this process
  stops talking to that host and a CRITICAL alert goes out (C.3).

That last rule is the one to read twice. A 403 spike is answered by stopping and telling a human
(AGENTIC_CONTEXT §8) — never by a different user agent, a different header set, a proxy, or a
faster retry. There is one user agent, it comes from `Settings`, and 403 is not in the retryable
set. `tests/unit/test_fetcher.py` asserts all of that, including that no second agent exists in
this file to rotate to. The one exception is narrow and documented at `_warm_session`: NSE answers
its own homepage with 403 and hands over the usable cookie anyway, so the *handshake's* 403 is
kept rather than counted. The data request that follows is classified exactly as before.

Offline by construction (B8): the network lives behind `Transport`. `HttpxTransport` is the real
one, `RecordedTransport` replays scripted responses, and the test suite only ever wires the
second — no fixture in this repo is produced by a socket opening during a test run.
"""

from __future__ import annotations

import time
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from datetime import date, timedelta
from typing import Final, NoReturn, Protocol
from urllib.parse import unquote, urlsplit

import httpx
from tenacity import (
    RetryCallState,
    Retrying,
    retry_if_exception_type,
    stop_after_attempt,
    wait_exponential,
)

from dataplatform.alerts import Alerter, Severity, build_alerter
from dataplatform.clock import Clock
from dataplatform.config import Settings, get_settings
from dataplatform.ingest.policy import (
    DEFAULT_FORBIDDEN_WINDOW,
    CrawlPolicy,
    ForbiddenWatch,
    RateLimiter,
    Sleeper,
    resolve_policy,
    robots_for,
)
from dataplatform.ingest.source_register import SourceRegister
from dataplatform.ingest.source_register import load as load_register
from dataplatform.logging import get_logger
from dataplatform.store.l0 import DEFAULT_CONTENT_TYPE, L0Ref, L0Store

__all__ = [
    "MAX_BACKOFF_SECONDS",
    "FetchError",
    "FetchHTTPError",
    "FetchResponse",
    "Fetcher",
    "ForbiddenError",
    "ForbiddenSpikeError",
    "HttpxTransport",
    "RecordedRequest",
    "RecordedResponse",
    "RecordedTransport",
    "RetryableFetchError",
    "ServerError",
    "Transport",
    "TransportError",
    "UnrecordedRequestError",
    "build_fetcher",
]

_LOG = get_logger(__name__)

#: Ceiling on one backoff wait. At base 2 the fifth attempt would otherwise wait 32 s; an EOD
#: window has room for that, but nothing is served by a wait longer than a minute — if the source
#: is still 500ing after a minute it is down, and the sync state machine (D5) should say so.
MAX_BACKOFF_SECONDS: Final = 60.0

_FORBIDDEN: Final = 403
_TOO_MANY_REQUESTS: Final = 429


class FetchError(RuntimeError):
    """Base for every fetch failure, so a caller can catch the layer without catching the world."""


class RetryableFetchError(FetchError):
    """A failure that means "later": the request may be repeated, unchanged, after a wait."""


class TransportError(RetryableFetchError):
    """The request never produced a response — timeout, DNS, connection reset, TLS."""


class ServerError(RetryableFetchError):
    """The source answered 5xx, or 429 asking us to slow down. Both are answered by waiting."""


class FetchHTTPError(FetchError):
    """A response that is final: 404, 400, anything else a retry would just repeat."""

    def __init__(self, message: str, *, status_code: int, url: str) -> None:
        super().__init__(message)
        self.status_code = status_code
        self.url = url


class ForbiddenError(FetchHTTPError):
    """One 403 — the source refused this client. Never retried, and never retried differently.

    Deliberately a subclass of the *non*-retryable error: repeating a refusal is at best noise and
    at worst the thing that turns a rate-limit into a ban. Enough of these and the next one is a
    `ForbiddenSpikeError` instead.
    """


class ForbiddenSpikeError(FetchError):
    """The hard stop (§4.1). This process will not talk to that host again.

    Raised both by the 403 that trips the limit and by every later attempt to fetch from the
    stopped host, so the stop is a property of the fetcher rather than of one call site. There is
    no bypass: clearing it means restarting the process, which is a decision a human makes after
    understanding why a source started refusing us.
    """


class UnrecordedRequestError(FetchError):
    """A `RecordedTransport` was asked for a URL nobody recorded — a test's bug, loudly."""


@dataclass(frozen=True, slots=True)
class FetchResponse:
    """One response, as the fetcher sees it: a status, the bytes, and the headers that describe
    them. Transports normalize header names to lower case so callers need not guess."""

    status_code: int
    body: bytes
    headers: Mapping[str, str]
    url: str

    @property
    def content_type(self) -> str:
        """`Content-Type` exactly as the source declared it, or L0's default when it declared
        none. Recorded rather than guessed from the extension — L0 stores what the source said."""
        return self.headers.get("content-type", "").strip() or DEFAULT_CONTENT_TYPE


class Transport(Protocol):
    """The seam between crawl policy and the network.

    One method, and the implementations are the real HTTP client and a recorded stand-in. A
    transport is *session-scoped*: it keeps cookies across requests, which is what makes the NSE
    warm-up meaningful, and the fetcher assumes one transport per fetcher.

    Raises `TransportError` when no response was produced; a 4xx/5xx is a response and is
    returned like any other.
    """

    def request(
        self,
        method: str,
        url: str,
        *,
        headers: Mapping[str, str],
        timeout: float,
        payload: bytes | None = None,
    ) -> FetchResponse:
        """Perform one request. No retries, no rate limiting — the fetcher owns both."""


class HttpxTransport:
    """The real network. The only socket on the ingestion path.

    What it does: issues one request on a shared `httpx.Client`, follows redirects, and returns
    the status, body and headers whatever they are.
    What it assumes: the client is long-lived, because its cookie jar *is* the warm NSE session.
    What it never does: retry, sleep, decide anything about the response, or add a header the
    policy did not ask for.
    """

    def __init__(self, *, client: httpx.Client | None = None) -> None:
        self._client = httpx.Client(follow_redirects=True) if client is None else client

    def request(
        self,
        method: str,
        url: str,
        *,
        headers: Mapping[str, str],
        timeout: float,
        payload: bytes | None = None,
    ) -> FetchResponse:
        """Issue one request, translating every "no response" outcome into `TransportError`."""
        try:
            response = self._client.request(
                method, url, headers=dict(headers), timeout=timeout, content=payload
            )
        except httpx.HTTPError as exc:
            raise TransportError(f"{method} {url}: {type(exc).__name__}: {exc}") from exc
        return FetchResponse(
            status_code=response.status_code,
            body=response.content,
            headers={name.lower(): value for name, value in response.headers.items()},
            url=str(response.url),
        )

    def close(self) -> None:
        """Close the underlying client and, with it, the session."""
        self._client.close()

    def __enter__(self) -> HttpxTransport:
        return self

    def __exit__(self, *exc_info: object) -> None:
        self.close()


@dataclass(frozen=True, slots=True)
class RecordedResponse:
    """A scripted response for `RecordedTransport`."""

    status_code: int = 200
    body: bytes = b""
    headers: Mapping[str, str] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class RecordedRequest:
    """A request a `RecordedTransport` was asked to make — the record tests assert against."""

    method: str
    url: str
    headers: Mapping[str, str]
    payload: bytes | None = None


#: One scripted outcome: a response, or an exception the transport raises instead of answering
#: (a `TransportError`, say, to exercise the backoff path).
ScriptedOutcome = RecordedResponse | Exception


class RecordedTransport:
    """The offline transport (B8): responses come from a script, never from a socket.

    What it does: answers each URL from its recorded script — one outcome reused for every
    request, or a sequence consumed in order whose last entry then repeats (so "403 from now on"
    is one entry) — and appends every request it was asked to make to `requests`.
    What it assumes: the test knows every URL the code under test will reach. One it does not is
    an `UnrecordedRequestError`, not a silent 404, because a fetcher quietly reaching an
    unexpected URL is exactly the bug this class exists to catch.
    What it never does: import or touch the network. This class is why the whole ingestion suite
    passes with sockets monkeypatched out.

    It is product code rather than test scaffolding on purpose: the same replay mechanism is how
    a recorded L0 payload can be re-served to a parser without a fetch.
    """

    def __init__(self, responses: Mapping[str, ScriptedOutcome | Sequence[ScriptedOutcome]]):
        self._script: dict[str, list[ScriptedOutcome]] = {}
        for url, outcome in responses.items():
            scripted = (
                [outcome] if isinstance(outcome, RecordedResponse | Exception) else list(outcome)
            )
            if not scripted:
                raise ValueError(f"empty response script for {url}")
            self._script[url] = scripted
        self._cursor: dict[str, int] = {}
        self.requests: list[RecordedRequest] = []

    def request(
        self,
        method: str,
        url: str,
        *,
        headers: Mapping[str, str],
        timeout: float,
        payload: bytes | None = None,
    ) -> FetchResponse:
        """Return (or raise) the next scripted outcome for `url`, recording the request first."""
        self.requests.append(
            RecordedRequest(method=method, url=url, headers=dict(headers), payload=payload)
        )
        script = self._script.get(url)
        if script is None:
            raise UnrecordedRequestError(
                f"no recorded response for {method} {url}; recorded urls: "
                f"{', '.join(sorted(self._script))}"
            )
        index = min(self._cursor.get(url, 0), len(script) - 1)
        self._cursor[url] = index + 1
        outcome = script[index]
        if isinstance(outcome, Exception):
            raise outcome
        return FetchResponse(
            status_code=outcome.status_code,
            body=outcome.body,
            headers={name.lower(): value for name, value in outcome.headers.items()},
            url=url,
        )


class Fetcher:
    """The crawl policy engine: policy in, `L0Ref` out.

    What it does: resolves a source's policy from the register, refuses URLs robots disallows,
    warms a session when the row needs cookies, spaces requests per host, retries the failures
    that mean "later", hard-stops on a 403 spike, and writes every successful body to L0 before
    returning its ref.
    What it assumes: one fetcher owns one transport (the session), and its clock is the same
    clock the rest of the run uses (B10). Sources are identified by their register id.
    What it never does: return response bytes, parse anything, retry a 403, vary its headers
    between attempts, or offer a way to resume after a hard stop.
    """

    def __init__(
        self,
        *,
        transport: Transport,
        l0: L0Store,
        alerter: Alerter,
        clock: Clock,
        register: SourceRegister | None = None,
        settings: Settings | None = None,
        sleep: Sleeper = time.sleep,
        forbidden_window: timedelta = DEFAULT_FORBIDDEN_WINDOW,
    ) -> None:
        self._transport = transport
        self._l0 = l0
        self._alerter = alerter
        self._settings = get_settings() if settings is None else settings
        self._register = load_register() if register is None else register
        self._sleep = sleep
        self._limiter = RateLimiter(clock=clock, sleep=sleep)
        self._watch = ForbiddenWatch(
            clock=clock,
            limit=self._settings.http_forbidden_streak_limit,
            window=forbidden_window,
        )
        self._policies: dict[str, CrawlPolicy] = {}
        self._warmed: set[str] = set()

    def policy_for(self, source_id: str) -> CrawlPolicy:
        """The resolved crawl policy for a register source id, cached per fetcher."""
        policy = self._policies.get(source_id)
        if policy is None:
            policy = resolve_policy(source_id, self._register, self._settings)
            self._policies[source_id] = policy
        return policy

    def is_stopped(self, host: str) -> bool:
        """Whether a 403 spike has hard-stopped this host for the life of the process."""
        return self._watch.stopped(host)

    def fetch(
        self,
        source_id: str,
        url: str,
        logical_date: date,
        *,
        filename: str | None = None,
        payload: bytes | None = None,
    ) -> L0Ref:
        """Fetch one URL under `source_id`'s policy and return its L0 ref.

        The body is in L0, checksummed, before this returns — the ref is the caller's only handle
        on it, and reading it back through `L0Store.get` re-verifies the checksum. Nothing here
        parses, and nothing here hands out bytes.

        `filename` defaults to the last path segment of the URL, which is what every dated archive
        file already is. Pass it explicitly for endpoints whose URL carries no date (the NSE JSON
        APIs): L0 partitions by month, so two dates sharing a filename inside one month collide
        rather than merge, by design (`L0Store.put`).

        Raises `RobotsDisallowedError` or `HostMismatchError` before any request is made,
        `ForbiddenSpikeError` if this host is hard-stopped, `ForbiddenError` on a single 403,
        `ServerError`/`TransportError` when the retry budget is exhausted, and `FetchHTTPError`
        on any other non-2xx.
        """
        policy = self.policy_for(source_id)
        policy.check_url(url)
        name = _filename_from_url(url) if filename is None else filename
        self._guard(policy.host)

        if policy.needs_session_cookie:
            self._warm_session(policy)

        response = self._request(policy, policy.method, url, payload)
        ref = self._l0.put(
            source_id,
            logical_date,
            name,
            response.body,
            content_type=response.content_type,
        )
        _LOG.info(
            "fetch.stored",
            source=source_id,
            logical_date=logical_date.isoformat(),
            url=url,
            status=response.status_code,
            size_bytes=ref.size_bytes,
            sha256=ref.sha256,
            l0_key=ref.key,
            state="FETCHED",
        )
        return ref

    # ── internals ────────────────────────────────────────────────────────────────────────────

    def _warm_session(self, policy: CrawlPolicy) -> None:
        """Visit the source's own page once so its cookies are on the transport's session.

        §4.1's "warm session cookie for NSE": the JSON endpoints answer 401/403 to a client that
        arrives without the cookies the site sets on a normal page view. The response body is
        deliberately *not* stored — it is a handshake, not data, and writing a homepage that
        changes hourly under one L0 key would conflict with itself on the second day
        (invariant #1). Once per host per process; the transport keeps the jar.

        The warm-up is a *handshake*, so its status is not a verdict on our access: NSE answers
        `https://www.nseindia.com/` with a 403 **and sets the usable cookie anyway**
        (`ops/gates/source-verification.md` §5.7, re-observed live on 2026-08-08 — the 403 warm-up
        is followed by a 200 on `/api/fiidiiTradeReact`). Counting that toward the 403 spike, or
        aborting on it, would stop every cookie source in the register from ever fetching. The
        hard stop is unweakened for the request that matters: if the source is genuinely refusing
        us, the *data* request 403s too and trips it there.
        """
        warm_url = policy.warm_url
        if warm_url is None:
            return
        host = urlsplit(warm_url).hostname or policy.host
        if host in self._warmed:
            return
        robots_for(host, self._register).check(warm_url)
        self._guard(host)
        response = self._request(policy, "GET", warm_url, None, handshake=True)
        self._warmed.add(host)
        _LOG.info(
            "fetch.session_warmed",
            source=policy.source_id,
            host=host,
            url=warm_url,
            status=response.status_code,
            state="WARMED",
        )

    def _request(
        self,
        policy: CrawlPolicy,
        method: str,
        url: str,
        payload: bytes | None,
        *,
        handshake: bool = False,
    ) -> FetchResponse:
        """One logical request: rate-limited, retried on the failures that mean "later"."""
        retrying = Retrying(
            stop=stop_after_attempt(policy.max_attempts),
            wait=wait_exponential(
                multiplier=1.0,
                exp_base=policy.backoff_base_seconds,
                max=MAX_BACKOFF_SECONDS,
            ),
            retry=retry_if_exception_type(RetryableFetchError),
            sleep=self._sleep,
            before_sleep=_log_retry,
            reraise=True,
        )
        return retrying(self._attempt, policy, method, url, payload, handshake)

    def _attempt(
        self,
        policy: CrawlPolicy,
        method: str,
        url: str,
        payload: bytes | None,
        handshake: bool,
    ) -> FetchResponse:
        """One attempt, with the same headers as every other attempt for this source."""
        host = urlsplit(url).hostname or policy.host
        self._guard(host)
        self._limiter.acquire(host, policy.min_interval_seconds)
        response = self._transport.request(
            method,
            url,
            headers=policy.headers,
            timeout=policy.timeout_seconds,
            payload=payload,
        )
        return self._classify(policy, host, url, response, handshake=handshake)

    def _classify(
        self,
        policy: CrawlPolicy,
        host: str,
        url: str,
        response: FetchResponse,
        *,
        handshake: bool = False,
    ) -> FetchResponse:
        """Turn a status code into "keep it", "wait and repeat", "stop", or "give up".

        `handshake` marks the session warm-up, whose 403 is a documented NSE behaviour that still
        yields the cookie (see `_warm_session`). It changes exactly one thing: a 403 on the
        handshake is kept instead of counted. Every other status, and every status on a data
        request, is classified identically — the spike detector is not reachable from here by any
        other route, and there is still no code path that varies a header or retries a refusal.
        """
        status = response.status_code
        if 200 <= status < 300:
            self._watch.record_success(host)
            return response
        if status == _FORBIDDEN and handshake:
            _LOG.info(
                "fetch.handshake_forbidden",
                source=policy.source_id,
                host=host,
                url=url,
                status=status,
                state="WARMED",
            )
            return response
        if status == _FORBIDDEN:
            self._on_forbidden(policy, host, url)
        if status == _TOO_MANY_REQUESTS or 500 <= status < 600:
            raise ServerError(
                f"{policy.source_id}: {url} returned {status}; retrying after a backoff at "
                f"least {policy.min_interval_seconds}s apart"
            )
        raise FetchHTTPError(
            f"{policy.source_id}: {url} returned {status}, which no retry would change",
            status_code=status,
            url=url,
        )

    def _on_forbidden(self, policy: CrawlPolicy, host: str, url: str) -> NoReturn:
        """Count a refusal, hard-stop if it was the last one we will accept, and raise.

        Never returns, and there is no branch here that changes anything about the request. The
        only escalation path a 403 has in this codebase is towards stopping.
        """
        streak = self._watch.record_forbidden(host)
        _LOG.warning(
            "fetch.forbidden",
            source=policy.source_id,
            host=host,
            url=url,
            streak=streak,
            limit=self._watch.limit,
            state="FORBIDDEN",
        )
        if self._watch.stopped(host):
            self._hard_stop(policy, host, url, streak)
        raise ForbiddenError(
            f"{policy.source_id}: {url} returned 403 ({streak} of {self._watch.limit} inside "
            f"{self._watch.window}); not retried — a refusal is answered by waiting or stopping, "
            "never by another user agent",
            status_code=_FORBIDDEN,
            url=url,
        )

    def _hard_stop(self, policy: CrawlPolicy, host: str, url: str, streak: int) -> NoReturn:
        """Stop this process fetching from `host`, and wake a human (§4.1, §8.1)."""
        message = (
            f"{streak} consecutive 403s from {host} within {self._watch.window} "
            f"(latest: {policy.source_id} {url}). Ingestion from this host is stopped for the "
            "life of this process. Do not work around it: no user-agent rotation, no proxy, no "
            "faster retry (AGENTIC_CONTEXT §8). Check whether the source changed its access "
            "rules, then restart ingestion deliberately."
        )
        _LOG.critical(
            "fetch.hard_stop",
            source=policy.source_id,
            host=host,
            url=url,
            streak=streak,
            limit=self._watch.limit,
            state="HARD_STOPPED",
        )
        try:
            self._alerter.send(
                Severity.CRITICAL,
                f"ingestion hard stop: 403 spike on {host}",
                message,
                f"ingest:403-spike:{host}",
            )
        except Exception as exc:  # the stop matters more than the channel that failed to say so
            _LOG.error("fetch.alert_failed", host=host, error=str(exc), state="HARD_STOPPED")
        raise ForbiddenSpikeError(message)

    def _guard(self, host: str) -> None:
        """Refuse, without a request, to touch a host this process has hard-stopped."""
        if self._watch.stopped(host):
            raise ForbiddenSpikeError(
                f"ingestion from {host} is hard-stopped after a 403 spike; this process will not "
                "request it again. Restart only after the block is understood (AGENTIC_CONTEXT §8)"
            )


def _log_retry(state: RetryCallState) -> None:
    """Log every backoff, so a slow source is visible before it becomes a missing day."""
    outcome = state.outcome
    error = outcome.exception() if outcome is not None else None
    sleeping = state.next_action.sleep if state.next_action is not None else 0.0
    _LOG.warning(
        "fetch.retry",
        attempt=state.attempt_number,
        sleeping_seconds=round(sleeping, 3),
        error=f"{type(error).__name__}: {error}" if error is not None else None,
        state="RETRYING",
    )


def _filename_from_url(url: str) -> str:
    """The L0 filename a URL implies: its last path segment, kept exactly as the source names it.

    Raises when the URL has no usable segment (a bare host, or a trailing slash), because the
    alternative is inventing a name for a payload whose identity L0 depends on.
    """
    name = unquote(urlsplit(url).path.rsplit("/", 1)[-1])
    if not name or name in {".", ".."} or "/" in name:
        raise ValueError(
            f"cannot derive an L0 filename from {url!r}; pass filename= explicitly (it must "
            "distinguish the trading dates it is used for — see L0Store.put)"
        )
    return name


def build_fetcher(
    *,
    clock: Clock,
    settings: Settings | None = None,
    transport: Transport | None = None,
    alerter: Alerter | None = None,
    l0: L0Store | None = None,
    register: SourceRegister | None = None,
) -> Fetcher:
    """Wire a production fetcher: real HTTP, the configured lake, the configured alert channel.

    The clock is required rather than defaulted, because a fetcher that picks its own clock is a
    fetcher a replay cannot reproduce (B10).
    """
    settings = get_settings() if settings is None else settings
    return Fetcher(
        transport=HttpxTransport() if transport is None else transport,
        l0=L0Store(clock=clock) if l0 is None else l0,
        alerter=build_alerter(settings, clock=clock) if alerter is None else alerter,
        clock=clock,
        register=register,
        settings=settings,
    )
