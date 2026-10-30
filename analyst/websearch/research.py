"""The router: one search or one fetch, across the configured vendors, into a Commons snapshot.

`WebResearch` is a `analyst.commons.Fetcher`, so every result goes through
`SnapshotStore.get_or_fetch` exactly as the Claude CLI fetcher's do: cache first (a hit costs no
request and no budget), else routed once, bounded, redacted, written content-addressed and
indexed. The snapshot records the request (`kind` query or url, the normalised target, the
session), `fetched_at` from the injected clock, and `fetcher` = `<vendor>:<capability>` — e.g.
`brave:search`, `firecrawl:fetch`, `direct:fetch` — and its id is the sha256 of its bytes. A
manager citing it produces the same `commons_fetch` evidence item as before (id, kind, target,
`knowable_at` = `fetched_at`), so the evidence trail is unchanged by the switch of fetcher.

**Routing, per call.** Vendors are tried in the configured order (`WEBSEARCH_SEARCH_VENDORS`,
`WEBSEARCH_FETCH_VENDORS`). A vendor is skipped when it has no key (warned once per process), when
it refused us earlier in this process (401/403/429/plan limit: never retried until a restart, the
way `ForbiddenWatch` stops a source), or when its monthly budget would be exceeded. Then it is
called once; a refusal or a failure (5xx, timeout, bad body) moves on to the next vendor, and a
vendor with nothing to say (no hits, empty page) does too. Two outcomes end the call at once,
without trying anyone else: the page's own site refusing (401/403/429 or robots.txt), and a URL
the guard refuses (SSRF, a scheduler host, a redirect into either). Asking again by another route
is the workaround AGENTIC_CONTEXT §8 forbids.

**Caps.** Before any vendor is asked, one request is reserved against the session's cap for the
capability (`WEBSEARCH_SESSION_MAX_SEARCHES` / `_FETCHES`), and before each vendor call its units
are reserved against its monthly budget (`WEBSEARCH_BUDGET_*`). Every vendor call logs one
`websearch.call` event: vendor, capability, outcome, units, remaining budget, status, latency.
Keys never appear in an event, a repr or an error.

**Which vendor answered** is per call, and the store reads `name` after `fetch` returns on the
same thread, so the name is kept thread-locally: the M17 runtime fetches on up to three threads
through one fetcher.
"""

from __future__ import annotations

import threading
import time
from collections.abc import Sequence
from datetime import date
from typing import Final

import httpx
from pydantic import BaseModel, ConfigDict, Field

from analyst.commons import (
    FetchedPage,
    FetchError,
    FetchKind,
    FetchOutcome,
    FetchRequest,
    FetchResponse,
    SnapshotStore,
)
from analyst.websearch.guard import (
    Resolver,
    UrlGuard,
    UrlRefusedError,
    host_is_blocked,
    system_resolver,
)
from analyst.websearch.http import HttpClient, HttpxClient
from analyst.websearch.ledger import UsageLedger
from analyst.websearch.models import (
    CallOutcome,
    Capability,
    FetchedDocument,
    SearchResults,
    VendorName,
    VendorUsage,
    month_of,
)
from analyst.websearch.vendors import (
    BraveSearch,
    DirectFetch,
    FetchVendor,
    FirecrawlScrape,
    FirecrawlSearch,
    SearchVendor,
    TavilyExtract,
    TavilySearch,
    VendorApi,
    VendorError,
)
from dataplatform.clock import Clock
from dataplatform.config import Settings
from dataplatform.ingest.policy import RateLimiter, Sleeper
from dataplatform.logging import get_logger

__all__ = ["SEARCH_RESULT_COUNT", "WebResearch", "WebResearchConfig"]

_LOG = get_logger(__name__)

#: Hits asked of a search vendor. Fixed rather than chosen per call because the snapshot cache
#: key is (query, session): two counts for one query would be one cache entry.
SEARCH_RESULT_COUNT: Final[int] = 8

_SEARCHERS: Final[frozenset[VendorName]] = frozenset(
    {VendorName.BRAVE, VendorName.TAVILY, VendorName.FIRECRAWL}
)
_FETCHERS: Final[frozenset[VendorName]] = frozenset(
    {VendorName.FIRECRAWL, VendorName.TAVILY, VendorName.DIRECT}
)
#: Outcomes that move on to the next vendor; the rest end the call.
_NEXT: Final[frozenset[CallOutcome]] = frozenset(
    {CallOutcome.REFUSING, CallOutcome.FAILED, CallOutcome.EMPTY}
)

_warned_unconfigured: set[VendorName] = set()
_warn_lock = threading.Lock()


def _vendor_list(raw: str, allowed: frozenset[VendorName], what: str) -> tuple[VendorName, ...]:
    names = [part.strip().lower() for part in raw.split(",") if part.strip()]
    out: list[VendorName] = []
    for name in names:
        try:
            vendor = VendorName(name)
        except ValueError:
            raise ValueError(f"{what}: unknown vendor {name!r}") from None
        if vendor not in allowed:
            raise ValueError(f"{what}: {name} cannot be used for this capability")
        if vendor in out:
            raise ValueError(f"{what}: {name} is listed twice")
        out.append(vendor)
    return tuple(out)


class WebResearchConfig(BaseModel):
    """Everything the router is configured with, read from `Settings` (no credential in here)."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    search_vendors: tuple[VendorName, ...]
    fetch_vendors: tuple[VendorName, ...]
    budgets: dict[VendorName, int]
    session_max_searches: int = Field(ge=0)
    session_max_fetches: int = Field(ge=0)
    max_result_chars: int = Field(ge=500)
    timeout_seconds: float = Field(gt=0)
    max_fetch_bytes: int = Field(ge=1024)
    max_redirects: int = Field(ge=0)
    blocked_hosts: tuple[str, ...]
    user_agent: str
    direct_min_interval_seconds: float = Field(ge=0)

    @classmethod
    def from_settings(cls, settings: Settings) -> WebResearchConfig:
        """Parse and validate the WEBSEARCH_* settings. Raises ValueError on an unknown vendor."""
        return cls(
            search_vendors=_vendor_list(
                settings.websearch_search_vendors, _SEARCHERS, "WEBSEARCH_SEARCH_VENDORS"
            ),
            fetch_vendors=_vendor_list(
                settings.websearch_fetch_vendors, _FETCHERS, "WEBSEARCH_FETCH_VENDORS"
            ),
            budgets={
                VendorName.BRAVE: settings.websearch_budget_brave,
                VendorName.TAVILY: settings.websearch_budget_tavily,
                VendorName.FIRECRAWL: settings.websearch_budget_firecrawl,
            },
            session_max_searches=settings.websearch_session_max_searches,
            session_max_fetches=settings.websearch_session_max_fetches,
            max_result_chars=settings.websearch_max_result_chars,
            timeout_seconds=settings.websearch_timeout_seconds,
            max_fetch_bytes=settings.websearch_max_fetch_bytes,
            max_redirects=settings.websearch_max_redirects,
            blocked_hosts=tuple(
                h.strip() for h in settings.websearch_blocked_hosts.split(",") if h.strip()
            ),
            user_agent=settings.http_user_agent,
            direct_min_interval_seconds=settings.http_min_interval_seconds,
        )

    def budget_of(self, vendor: VendorName) -> int | None:
        """The vendor's monthly budget; None (uncapped, still counted) for `direct`."""
        return self.budgets.get(vendor)


def _warn_unconfigured(vendor: VendorName, setting: str) -> None:
    with _warn_lock:
        if vendor in _warned_unconfigured:
            return
        _warned_unconfigured.add(vendor)
    _LOG.warning(
        "websearch.vendor_unconfigured",
        vendor=vendor.value,
        setting=setting,
        detail="no key configured; this vendor is skipped",
    )


class WebResearch:
    """Search and fetch across vendors, cached and evidenced through the Commons snapshot store.

    What it does: `search(query, session)` and `fetch_page(url, session)` return the Commons
    snapshot for that request — the stored one, or one routed, stored and returned now. As a
    `Fetcher` it can also stand in for `ClaudeWebFetcher` wherever a `SnapshotStore` is given one.
    What it assumes: one instance per process (or per M17 run); the refusing-vendor set lives on it.
    What it never does: retry a vendor, try another vendor after the page's own site refused, fetch
    a URL the guard refused, or exceed a budget or a session cap.
    """

    def __init__(
        self,
        *,
        config: WebResearchConfig,
        clock: Clock,
        ledger: UsageLedger,
        guard: UrlGuard,
        search_vendors: Sequence[SearchVendor],
        fetch_vendors: Sequence[FetchVendor],
        store: SnapshotStore | None = None,
    ) -> None:
        self._config = config
        self._clock = clock
        self._ledger = ledger
        self._guard = guard
        self._search = tuple(search_vendors)
        self._fetch = tuple(fetch_vendors)
        self._store = store
        self._refusing: set[tuple[VendorName, Capability]] = set()
        self._lock = threading.Lock()
        self._local = threading.local()

    @classmethod
    def from_settings(
        cls,
        settings: Settings,
        *,
        clock: Clock,
        ledger: UsageLedger,
        store: SnapshotStore | None = None,
        http: HttpClient | None = None,
        resolver: Resolver = system_resolver,
        sleep: Sleeper = time.sleep,
    ) -> WebResearch:
        """The router this configuration describes. A vendor without a key is left out (warned
        once per process); the direct fetcher needs none."""
        config = WebResearchConfig.from_settings(settings)
        client: HttpClient = (
            http if http is not None else HttpxClient(httpx.Client(follow_redirects=False))
        )
        limiter = RateLimiter(clock=clock, sleep=sleep)
        guard = UrlGuard(config.blocked_hosts, resolver=resolver)
        keys = {
            VendorName.BRAVE: (settings.brave_search_api_key, "BRAVE_SEARCH_API_KEY"),
            VendorName.TAVILY: (settings.tavily_api_key, "TAVILY_API_KEY"),
            VendorName.FIRECRAWL: (settings.firecrawl_api_key, "FIRECRAWL_API_KEY"),
        }

        def api(vendor: VendorName) -> VendorApi:
            return VendorApi(
                vendor,
                client,
                limiter,
                timeout=config.timeout_seconds,
                max_bytes=config.max_fetch_bytes,
            )

        searchers: list[SearchVendor] = []
        for vendor in config.search_vendors:
            key, setting = keys[vendor]
            if key is None:
                _warn_unconfigured(vendor, setting)
            elif vendor is VendorName.BRAVE:
                searchers.append(BraveSearch(key, api(vendor)))
            elif vendor is VendorName.TAVILY:
                searchers.append(TavilySearch(key, api(vendor)))
            else:
                searchers.append(FirecrawlSearch(key, api(vendor)))
        fetchers: list[FetchVendor] = []
        for vendor in config.fetch_vendors:
            if vendor is VendorName.DIRECT:
                fetchers.append(
                    DirectFetch(
                        client,
                        guard,
                        limiter,
                        user_agent=config.user_agent,
                        min_interval_seconds=config.direct_min_interval_seconds,
                        timeout=config.timeout_seconds,
                        max_bytes=config.max_fetch_bytes,
                        max_redirects=config.max_redirects,
                    )
                )
                continue
            key, setting = keys[vendor]
            if key is None:
                _warn_unconfigured(vendor, setting)
            elif vendor is VendorName.TAVILY:
                fetchers.append(TavilyExtract(key, api(vendor)))
            else:
                fetchers.append(FirecrawlScrape(key, api(vendor)))
        return cls(
            config=config,
            clock=clock,
            ledger=ledger,
            guard=guard,
            search_vendors=searchers,
            fetch_vendors=fetchers,
            store=store,
        )

    @property
    def config(self) -> WebResearchConfig:
        return self._config

    @property
    def guard(self) -> UrlGuard:
        return self._guard

    @property
    def search_vendors(self) -> tuple[SearchVendor, ...]:
        """The configured search vendors that have a key, in routing order."""
        return self._search

    @property
    def fetch_vendors(self) -> tuple[FetchVendor, ...]:
        """The configured fetch vendors that can run (key set, or direct), in routing order."""
        return self._fetch

    # ── the public calls ─────────────────────────────────────────────────────────────────────

    def search(self, query: str, session: date) -> FetchOutcome:
        """The snapshot of a web search for `query` on `session`. Raises `FetchError` when no
        vendor could serve it, and ValueError on a query the Commons refuses."""
        return self._require_store().get_or_fetch(FetchRequest.query(query, session), self)

    def fetch_page(self, url: str, session: date) -> FetchOutcome:
        """The snapshot of `url` on `session`. Raises `UrlRefusedError` (a ValueError) for a URL
        the guard refuses — before the cache is consulted — and `FetchError` when no vendor could
        serve it."""
        self._guard.check_form(url)
        return self._require_store().get_or_fetch(FetchRequest.url(url, session), self)

    def usage(self) -> tuple[VendorUsage, ...]:
        """Month-to-date units per budgeted vendor, at the clock's now."""
        now = self._clock.now()
        used = self._ledger.month_usage(month_of(now))
        return tuple(
            VendorUsage.at(vendor, now, used.get(vendor, 0), budget)
            for vendor, budget in self._config.budgets.items()
        )

    def _require_store(self) -> SnapshotStore:
        if self._store is None:
            raise RuntimeError("this WebResearch has no SnapshotStore; pass one to search/fetch")
        return self._store

    # ── the Fetcher protocol ─────────────────────────────────────────────────────────────────

    @property
    def name(self) -> str:
        """`<vendor>:<capability>` of the last call that succeeded on this thread."""
        served = getattr(self._local, "served", None)
        return served if isinstance(served, str) else "websearch"

    def fetch(self, request: FetchRequest) -> FetchResponse:
        """Route one Commons request. Raises `FetchError` when nothing could be stored."""
        self._local.served = None
        if request.kind is FetchKind.QUERY:
            return self._route_search(request)
        return self._route_fetch(request)

    # ── routing ──────────────────────────────────────────────────────────────────────────────

    def _reserve_session(self, request: FetchRequest, capability: Capability) -> None:
        limit = (
            self._config.session_max_searches
            if capability is Capability.SEARCH
            else self._config.session_max_fetches
        )
        used = self._ledger.reserve_session(
            request.session, capability, limit, at=self._clock.now()
        )
        if used is None:
            _LOG.warning(
                "websearch.session_cap",
                capability=capability.value,
                session=request.session.isoformat(),
                limit=limit,
                key=request.key,
            )
            raise FetchError(
                f"the session's {capability.value} cap ({limit}) is reached; nothing was sent"
            )

    def _event(
        self,
        request: FetchRequest,
        vendor: VendorName,
        capability: Capability,
        outcome: CallOutcome,
        *,
        units: int = 0,
        status: int | None = None,
        started: float | None = None,
        detail: str | None = None,
    ) -> None:
        budget = self._config.budget_of(vendor)
        remaining: int | None = None
        if budget is not None:
            used = self._ledger.month_usage(month_of(self._clock.now())).get(vendor, 0)
            remaining = max(budget - used, 0)
        fields: dict[str, object] = {
            "vendor": vendor.value,
            "capability": capability.value,
            "outcome": outcome.value,
            "units": units,
            "remaining_budget": remaining,
            "session": request.session.isoformat(),
            "key": request.key,
        }
        if status is not None:
            fields["status"] = status
        if started is not None:
            fields["latency_ms"] = round((time.perf_counter() - started) * 1000)
        if detail:
            fields["detail"] = detail[:300]
        log = _LOG.info if outcome is CallOutcome.OK else _LOG.warning
        log("websearch.call", **fields)

    def _admit(
        self, request: FetchRequest, vendor: VendorName, capability: Capability, units: int
    ) -> bool:
        """Whether this vendor may be called now; reserves its units when it may."""
        with self._lock:
            refusing = (vendor, capability) in self._refusing
        if refusing:
            self._event(
                request,
                vendor,
                capability,
                CallOutcome.REFUSING,
                detail="refused earlier in this process; skipped until restart",
            )
            return False
        now = self._clock.now()
        reserved = self._ledger.reserve_month(
            vendor, month_of(now), units, self._config.budget_of(vendor), at=now
        )
        if reserved is None:
            self._event(request, vendor, capability, CallOutcome.OVER_BUDGET)
            return False
        return True

    def _failed(
        self,
        request: FetchRequest,
        vendor: VendorName,
        capability: Capability,
        error: VendorError,
        units: int,
        started: float,
    ) -> None:
        if error.outcome is CallOutcome.REFUSING:
            with self._lock:
                self._refusing.add((vendor, capability))
        self._event(
            request,
            vendor,
            capability,
            error.outcome,
            units=units,
            status=error.status,
            started=started,
            detail=str(error),
        )

    def _route_search(self, request: FetchRequest) -> FetchResponse:
        self._reserve_session(request, Capability.SEARCH)
        tried: list[str] = []
        for vendor in self._search:
            if not self._admit(request, vendor.name, Capability.SEARCH, vendor.units):
                tried.append(f"{vendor.name.value}: skipped")
                continue
            started = time.perf_counter()
            try:
                results = vendor.search(request.target, SEARCH_RESULT_COUNT)
            except VendorError as exc:
                self._failed(request, vendor.name, Capability.SEARCH, exc, vendor.units, started)
                tried.append(f"{vendor.name.value}: {exc.outcome.value}")
                continue
            self._event(
                request,
                vendor.name,
                Capability.SEARCH,
                CallOutcome.OK,
                units=vendor.units,
                started=started,
            )
            self._local.served = f"{vendor.name.value}:{Capability.SEARCH.value}"
            return FetchResponse(pages=(_results_page(results),))
        raise FetchError(f"no search vendor could serve the query ({'; '.join(tried) or 'none'})")

    def _route_fetch(self, request: FetchRequest) -> FetchResponse:
        url = request.target
        try:
            self._guard.check(url)
        except UrlRefusedError as exc:
            raise FetchError(f"refused: {exc.reason}") from exc
        self._reserve_session(request, Capability.FETCH)
        tried: list[str] = []
        for vendor in self._fetch:
            if not self._admit(request, vendor.name, Capability.FETCH, vendor.units):
                tried.append(f"{vendor.name.value}: skipped")
                continue
            started = time.perf_counter()
            try:
                document = vendor.fetch(url)
                self._check_final(document)
            except UrlRefusedError as exc:
                self._event(
                    request,
                    vendor.name,
                    Capability.FETCH,
                    CallOutcome.TARGET_REFUSED,
                    units=vendor.units,
                    started=started,
                    detail=exc.reason,
                )
                raise FetchError(f"refused: {exc.reason}") from exc
            except VendorError as exc:
                self._failed(request, vendor.name, Capability.FETCH, exc, vendor.units, started)
                if exc.outcome not in _NEXT:
                    raise FetchError(f"{vendor.name.value}: {exc}") from exc
                tried.append(f"{vendor.name.value}: {exc.outcome.value}")
                continue
            self._event(
                request,
                vendor.name,
                Capability.FETCH,
                CallOutcome.OK,
                units=vendor.units,
                status=document.status,
                started=started,
            )
            self._local.served = f"{vendor.name.value}:{Capability.FETCH.value}"
            return FetchResponse(pages=(_document_page(document),))
        raise FetchError(f"no fetch vendor could serve the page ({'; '.join(tried) or 'none'})")

    def _check_final(self, document: FetchedDocument) -> None:
        """A vendor that followed a redirect onto a scheduler host has fetched a page we refuse."""
        host = httpx.URL(document.final_url).host if document.final_url else ""
        if host and host_is_blocked(host, self._guard.blocked_hosts):
            raise UrlRefusedError(
                f"the page redirected to {host}, which the data scheduler owns; not used"
            )


def _results_page(results: SearchResults) -> FetchedPage:
    """A search answer as one Commons page: every hit, ranked, with its URL and date."""
    lines: list[str] = []
    for hit in results.results:
        lines.append(f"{hit.rank}. {hit.title or '(untitled)'}")
        lines.append(f"   {hit.url}")
        if hit.published is not None:
            lines.append(f"   published {hit.published.isoformat()}")
        if hit.snippet:
            lines.append(f"   {hit.snippet}")
        lines.append("")
    return FetchedPage(
        url=f"websearch:{results.vendor.value}",
        title=f"Web search results from {results.vendor.value} ({len(results.results)} hits)",
        text="\n".join(lines).strip(),
    )


def _document_page(document: FetchedDocument) -> FetchedPage:
    """A fetched page as one Commons page, headed by how it was fetched."""
    status = "not reported" if document.status is None else str(document.status)
    header = (
        f"[fetched by {document.vendor.value}; HTTP status {status}; {document.content_type}; "
        f"{document.bytes} bytes; final URL {document.final_url}]"
    )
    return FetchedPage(
        url=document.final_url, title=document.title, text=f"{header}\n\n{document.text}"
    )
