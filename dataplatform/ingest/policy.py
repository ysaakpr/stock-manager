"""Crawl policy (§4.1 "Crawl policy engine", AGENTIC_CONTEXT §8) — the rules, minus the socket.

`source_register.yaml` (C.1) records what each source needs and what each host's robots.txt
permits; this module turns those rows into the three mechanisms a polite crawler is made of, and
`fetcher.py` is the only thing that drives them:

* **`CrawlPolicy`** — the resolved headers, spacing, timeout and retry budget for one source, with
  the robots check that runs *before* a request is built rather than after it fails.
* **`RateLimiter`** — per-host minimum spacing, measured on an injected `Clock` and waited on an
  injected sleeper, so a test proves the 3-second gap without spending three seconds.
* **`ForbiddenWatch`** — the 403 detector whose only reaction is to stop. It has no reset: once a
  host has refused us `HTTP_FORBIDDEN_STREAK_LIMIT` times inside a window, this process does not
  talk to it again.

The last one is the point of the whole file. A 403 means a source has told us to go away, and the
only legitimate responses are to slow down, to stop, and to tell a human (AGENTIC_CONTEXT §8).
Rotating the user agent, cycling an IP, or retrying "just once more with different headers" is
evasion; none of it exists here and none of it may be added. There is exactly one user agent in
the platform, it comes from `Settings.http_user_agent`, and the register's `User-Agent: browser`
is a *reference* to it rather than a value of its own.

Nothing in this module opens a socket, reads the wall clock, or writes a file.
"""

from __future__ import annotations

import re
import time
from collections import deque
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import datetime, timedelta
from functools import lru_cache
from threading import Lock
from types import MappingProxyType
from typing import Final
from urllib.parse import urlsplit

from dataplatform.clock import Clock
from dataplatform.config import Settings, get_settings
from dataplatform.ingest.source_register import HostPolicy, Source, SourceRegister
from dataplatform.logging import get_logger

__all__ = [
    "DEFAULT_FORBIDDEN_WINDOW",
    "MIN_SPACING_FLOOR_SECONDS",
    "USER_AGENT_SENTINEL",
    "CrawlPolicy",
    "ForbiddenWatch",
    "HostMismatchError",
    "MissingHostPolicyError",
    "PolicyError",
    "RateLimiter",
    "RobotsDisallowedError",
    "RobotsPolicy",
    "Sleeper",
    "UnknownSourceError",
    "resolve_policy",
    "robots_for",
]

_LOG = get_logger(__name__)

#: What `required_headers: {User-Agent: browser}` in the register means: "the configured browser
#: agent", not a literal. One agent for the whole platform, so a 403 cannot be answered by
#: inventing a second one.
USER_AGENT_SENTINEL: Final = "browser"

#: §4.1's floor — 2-3 s between requests to one host. A register row claiming faster than this is
#: a defect in the row, not permission to go faster, so spacing is the *slowest* of the floor, the
#: configured interval and the host's own record.
MIN_SPACING_FLOOR_SECONDS: Final = 2.0

#: How long a 403 counts toward the streak. Long enough that a block spread across a slow backfill
#: still trips (at 3 s spacing, a run makes ~600 requests in half an hour), short enough that a
#: single stale refusal from this morning does not stop tonight's ingestion.
DEFAULT_FORBIDDEN_WINDOW: Final = timedelta(minutes=30)

#: Waits for a number of seconds. Injected everywhere, so tests exercise the real spacing logic
#: against a `FrozenClock` instead of sleeping (B8/B10).
Sleeper = Callable[[float], None]


class PolicyError(RuntimeError):
    """Base for every refusal to make a request. Raised before anything reaches the network."""


class UnknownSourceError(PolicyError):
    """No entry with this id in the Source Register — the register is the list of what we fetch."""


class MissingHostPolicyError(PolicyError):
    """A host with no robots record. C.1 records one per host; fetching one it never checked
    would mean crawling a host whose rules nobody has read."""


class RobotsDisallowedError(PolicyError):
    """The URL is under a path the host's robots.txt disallows. Not retryable, not overridable."""


class HostMismatchError(PolicyError):
    """The URL's host is not the host this source's policy (and robots record) describes."""


@lru_cache(maxsize=256)
def _rule_pattern(rule: str) -> re.Pattern[str]:
    """Compile one robots `Disallow:` value into a matcher over path + query.

    Implements the two wildcards the standard defines and nothing else: `*` matches any run of
    characters, a trailing `$` anchors the end, and everything else is literal. The match is a
    prefix match, which is what makes `/user/` cover `/user/anything`.
    """
    anchored = rule.endswith("$")
    body = rule.removesuffix("$") if anchored else rule
    expression = ".*".join(re.escape(part) for part in body.split("*"))
    return re.compile(f"^{expression}{'$' if anchored else ''}")


@dataclass(frozen=True, slots=True)
class RobotsPolicy:
    """What one host's robots.txt permits, as recorded by the C.1 sweep.

    What it does: answers "may we request this URL" for a host, and names the rule that said no.
    What it assumes: the register's record is current — `checked_at` is in the file, and a
    campaign against a host whose record is old re-checks first (AGENTIC_CONTEXT §8).
    What it never does: fetch robots.txt itself, or treat an absent robots.txt as an absent
    policy. `robots_served=False` hosts are still crawled under the §4.1 spacing rules; the
    register's `permits` prose is the human-readable record of why that is defensible.
    """

    host: str
    disallow: tuple[str, ...]
    permits: str

    @classmethod
    def from_record(cls, record: HostPolicy) -> RobotsPolicy:
        """Build from a register `hosts:` row."""
        return cls(host=record.host, disallow=tuple(record.disallow), permits=record.permits)

    def rule_for(self, url: str) -> str | None:
        """The disallow rule this URL falls under, or None when nothing matches.

        Matches against path + query together, because the rules that matter most here are query
        rules: Screener disallows `/*?page=` and `/*?q=`, and a matcher that only saw the path
        would wave those through (AGENTIC_CONTEXT §8).
        """
        parts = urlsplit(url)
        target = parts.path or "/"
        if parts.query:
            target = f"{target}?{parts.query}"
        return next((rule for rule in self.disallow if _rule_pattern(rule).search(target)), None)

    def allows(self, url: str) -> bool:
        """Whether this URL is outside every disallow rule."""
        return self.rule_for(url) is None

    def check(self, url: str) -> None:
        """Raise `RobotsDisallowedError` if the URL is disallowed. The only enforcement point."""
        rule = self.rule_for(url)
        if rule is not None:
            raise RobotsDisallowedError(
                f"{url} is disallowed for {self.host} by robots rule {rule!r}; "
                f"the host permits: {self.permits.strip()}"
            )


@dataclass(frozen=True, slots=True)
class CrawlPolicy:
    """Everything the fetcher is allowed to know about how to talk to one source.

    What it does: carries the resolved request headers (browser agent and Referer from the
    register), the effective per-host spacing, the timeout and retry budget, and the robots record
    for the host.
    What it assumes: the register row is accurate — C.1 verified the URL pattern, the headers and
    the host's robots.txt against the live source and recorded the evidence.
    What it never does: vary by attempt. The headers a retry sends are byte-identical to the ones
    the first attempt sent; there is no second user agent to fall back to and no code path that
    would choose one.
    """

    source_id: str
    host: str
    method: str
    headers: Mapping[str, str]
    needs_session_cookie: bool
    warm_url: str | None
    min_interval_seconds: float
    timeout_seconds: float
    max_attempts: int
    backoff_base_seconds: float
    robots: RobotsPolicy

    def check_url(self, url: str) -> None:
        """Refuse, before any socket exists, a URL this policy does not cover.

        Two ways it can fail: the URL points at a different host than the register row describes
        (so this policy's robots record and spacing would not apply to it), or the host's
        robots.txt disallows the path.
        """
        host = urlsplit(url).hostname
        if host != self.host:
            raise HostMismatchError(
                f"source {self.source_id} is registered for host {self.host}, but {url} targets "
                f"{host!r}; a URL on another host needs that host's own register entry"
            )
        self.robots.check(url)


def robots_for(host: str, register: SourceRegister) -> RobotsPolicy:
    """The robots record for a host, or `MissingHostPolicyError` if C.1 never checked it."""
    record = register.host_policy(host)
    if record is None:
        raise MissingHostPolicyError(
            f"host {host} has no robots record in the Source Register; add one (with the "
            "robots.txt evidence) before fetching from it — AGENTIC_CONTEXT §8"
        )
    return RobotsPolicy.from_record(record)


def resolve_policy(
    source_id: str,
    register: SourceRegister,
    settings: Settings | None = None,
) -> CrawlPolicy:
    """Resolve one source's row plus its host's row into the policy the fetcher enforces.

    Spacing is the slowest of three numbers — §4.1's floor, the configured interval, and the
    host's own recorded minimum — because each is a claim about a lower bound and the binding one
    is whichever is largest. The rest comes straight from the row.
    """
    settings = get_settings() if settings is None else settings
    source = next((entry for entry in register.sources if entry.id == source_id), None)
    if source is None:
        raise UnknownSourceError(
            f"no source {source_id!r} in the Source Register; known ids: "
            f"{', '.join(sorted(entry.id for entry in register.sources))}"
        )
    record = register.host_policy(source.host)
    if record is None:
        raise MissingHostPolicyError(
            f"source {source_id} fetches from {source.host}, which has no robots record in the "
            "Source Register; C.1 records one per host and this one is missing"
        )

    return CrawlPolicy(
        source_id=source.id,
        host=source.host,
        method=source.method.upper(),
        headers=_resolve_headers(source, settings),
        needs_session_cookie=source.needs_session_cookie,
        warm_url=_warm_url(source),
        min_interval_seconds=max(
            record.min_spacing_seconds,
            settings.http_min_interval_seconds,
            MIN_SPACING_FLOOR_SECONDS,
        ),
        timeout_seconds=settings.http_timeout_seconds,
        max_attempts=settings.http_max_attempts,
        backoff_base_seconds=settings.http_backoff_base_seconds,
        robots=RobotsPolicy.from_record(record),
    )


def _resolve_headers(source: Source, settings: Settings) -> Mapping[str, str]:
    """The exact headers every request for this source carries.

    `User-Agent: browser` resolves to the one configured agent; a row that names no agent still
    gets it, because an unidentified request is worse than an identified one. The result is a
    read-only mapping, so nothing downstream can mutate a policy's headers between attempts.
    """
    headers = {
        name: settings.http_user_agent if _is_user_agent_sentinel(name, value) else value
        for name, value in source.required_headers.items()
    }
    if not any(name.lower() == "user-agent" for name in headers):
        headers["User-Agent"] = settings.http_user_agent
    return MappingProxyType(headers)


def _is_user_agent_sentinel(name: str, value: str) -> bool:
    """Whether this register header is the `User-Agent: browser` reference."""
    return name.lower() == "user-agent" and value.strip().lower() == USER_AGENT_SENTINEL


def _warm_url(source: Source) -> str | None:
    """The page a browser would have been on before this request, for sources that need cookies.

    NSE's JSON endpoints reject a request that arrives without the cookies its own site sets, so
    the fetcher visits this URL first and reuses the session (§4.1 "warm session cookie"). The
    register already names that page: it is the `Referer` the row requires, which is exactly the
    page the browser was on. Falling back to the host root covers a row that requires cookies but
    names no Referer.
    """
    if not source.needs_session_cookie:
        return None
    referer = next(
        (value for name, value in source.required_headers.items() if name.lower() == "referer"),
        None,
    )
    return referer or f"https://{source.host}/"


class RateLimiter:
    """Per-host minimum spacing between requests (§4.1: 2-3 s).

    What it does: `acquire` blocks until the configured interval has passed since the previous
    acquire for that host, then records the new request time.
    What it assumes: it is the only gate in front of the transport — spacing enforced anywhere
    else is spacing that a second caller can skip.
    What it never does: read the wall clock (B10) or call `time.sleep` directly. Both are
    injected, which is what makes "three requests are spaced 3 s apart" a microsecond-long test
    with a `FrozenClock` rather than a nine-second one.

    The lock is held across the wait, so two threads asking for the same host cannot both decide
    they may go now. It is one lock rather than one per host: at 2-3 s per request an EOD platform
    has no throughput to lose, and one lock has no ordering to get wrong.
    """

    def __init__(self, *, clock: Clock, sleep: Sleeper = time.sleep) -> None:
        self._clock = clock
        self._sleep = sleep
        self._last: dict[str, datetime] = {}
        self._lock = Lock()

    def acquire(self, host: str, min_interval_seconds: float) -> float:
        """Wait until `host` may be requested again; return the seconds actually waited."""
        with self._lock:
            waited = 0.0
            last = self._last.get(host)
            if last is not None:
                elapsed = (self._clock.now() - last).total_seconds()
                waited = max(min_interval_seconds - elapsed, 0.0)
                if waited > 0:
                    _LOG.debug("crawl.spacing", host=host, waited_seconds=round(waited, 3))
                    self._sleep(waited)
            # Re-read the clock rather than adding `waited`: an injected sleeper advances the
            # clock by whatever it advances it by, and the record must be when the request is
            # actually being made.
            self._last[host] = self._clock.now()
            return waited

    def last_request_at(self, host: str) -> datetime | None:
        """When this limiter last let a request through for `host`, if it ever did."""
        return self._last.get(host)


class ForbiddenWatch:
    """The 403 spike detector, and the hard stop it arms (§4.1, AGENTIC_CONTEXT §8).

    What it does: counts consecutive 403s per host inside a rolling window, and once the count
    reaches the configured limit marks that host stopped — permanently, for the life of the
    process.
    What it assumes: 403 means the host is refusing this client, not this URL. That is why the
    counter is per host and why one success clears it: a source that is answering us is not
    blocking us.
    What it never does: reset a stop. There is deliberately no `resume()` and no override flag,
    because the correct response to a block is a human understanding why it happened
    (AGENTIC_CONTEXT §3) — and an agent that can clear its own hard stop does not have one.
    Restarting the process is the reset, and a restart is a decision someone made.

    Windowed *and* consecutive: three refusals a week apart are not a spike, and three refusals
    interleaved with successes are a flaky endpoint rather than a block.
    """

    def __init__(
        self,
        *,
        clock: Clock,
        limit: int,
        window: timedelta = DEFAULT_FORBIDDEN_WINDOW,
    ) -> None:
        if limit < 1:
            raise ValueError(f"forbidden streak limit must be at least 1, got {limit}")
        if window <= timedelta(0):
            raise ValueError(f"forbidden window must be positive, got {window!r}")
        self._clock = clock
        self.limit = limit
        self.window = window
        self._hits: dict[str, deque[datetime]] = {}
        self._stopped: set[str] = set()
        self._lock = Lock()

    def record_forbidden(self, host: str) -> int:
        """Record a 403 from `host` and return the streak it now has inside the window.

        Reaching the limit arms the hard stop as part of this call: there is no window between
        counting the spike and refusing to fetch, so no request can slip through in between.
        """
        with self._lock:
            now = self._clock.now()
            hits = self._hits.setdefault(host, deque())
            hits.append(now)
            cutoff = now - self.window
            while hits and hits[0] < cutoff:
                hits.popleft()
            if len(hits) >= self.limit:
                self._stopped.add(host)
            return len(hits)

    def record_success(self, host: str) -> None:
        """Record a non-403 response, clearing the streak. Never clears a hard stop."""
        with self._lock:
            self._hits.pop(host, None)

    def streak(self, host: str) -> int:
        """Consecutive 403s from `host` still inside the window."""
        with self._lock:
            hits = self._hits.get(host)
            if not hits:
                return 0
            cutoff = self._clock.now() - self.window
            return sum(1 for at in hits if at >= cutoff)

    def stopped(self, host: str) -> bool:
        """Whether ingestion from `host` is hard-stopped. Once true, true until restart."""
        return host in self._stopped
