"""The router: one search or one fetch, to one vendor, into a Commons snapshot.

`WebResearch` is a `analyst.commons.Fetcher`, so every result goes through
`SnapshotStore.get_or_fetch` exactly as the Claude CLI fetcher's do: cache first (a hit costs no
request and no budget), else routed, bounded, redacted, written content-addressed and indexed. The
snapshot records the request (`kind` query or url, the normalised target, the session),
`fetched_at` from the injected clock, and `fetcher` = `<vendor>:<capability>` — e.g.
`brave:search`, `firecrawl:fetch`, `direct:fetch` — and its id is the sha256 of its bytes. A
manager citing it produces the same `commons_fetch` evidence item as before (id, kind, target,
`knowable_at` = `fetched_at`), so the evidence trail is unchanged by the switch of fetcher.

**Vendors share the load; they do not cross-check each other** (owner, 2026-10-30). Each request
goes to ONE vendor, picked by a smooth weighted round robin over the vendors in the capability's
rotation (`WEBSEARCH_SEARCH_VENDORS`, `WEBSEARCH_FETCH_VENDORS`) that have a key, have not refused
us, and have budget left. Each vendor's weight is its remaining monthly budget in requests, read
from the ledger at every pick, so the share each vendor gets follows what it has left and every
vendor lasts to the end of the month instead of the first one draining.

**At most one fallback.** If the chosen vendor fails (5xx, timeout, bad body), refuses us (401,
402, 403, 429 or a plan limit — that vendor then leaves the rotation for the rest of the process,
never retried), or returns nothing usable (no hits; a page under `MIN_PAGE_CHARS` of text), exactly
one other vendor is picked from the rotation and asked. If that one also gives nothing, the request
ends with `NoResultError`, naming both reasons; the tool returns that to the model as an empty
result. A third vendor is never called for one request. The direct fetch has no quota: it is a
fetch's single fallback only, and only when no fetch vendor in the rotation has budget left (and,
with every fetch vendor exhausted, the one call). Two outcomes end a request at once with no
fallback at all: the page's own site refusing (401/403/429, or robots.txt on the direct fetch) and
a URL the guard refuses — asking again by another route is the workaround AGENTIC_CONTEXT §8
forbids.

**Caps.** Before any vendor is asked, one request is reserved against the session's cap for the
capability (`WEBSEARCH_SESSION_MAX_SEARCHES` / `_FETCHES`). Each vendor call then reserves, in one
atomic step, one request against the owner's monthly total for all vendors
(`WEBSEARCH_MONTHLY_REQUEST_CAP`, fallbacks included) and its units against the vendor's own
budget (`WEBSEARCH_BUDGET_*`). At the monthly total the request is refused with a
`websearch.monthly_cap` WARN event. Every vendor call logs one `websearch.call` event: vendor,
capability, outcome, units, remaining vendor budget, remaining monthly total, status, latency. Keys
never appear in an event, a repr or an error.

**Which vendor answered** is per call, and the store reads `name` after `fetch` returns on the
same thread, so the name is kept thread-locally: the M17 runtime fetches on up to three threads
through one fetcher.
"""

from __future__ import annotations

import threading
import time
from collections.abc import Sequence
from datetime import date
from typing import Final, cast

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
from analyst.websearch.ledger import Reservation, UsageLedger
from analyst.websearch.models import (
    CallOutcome,
    Capability,
    FetchedDocument,
    SearchResults,
    VendorName,
    VendorUsage,
    month_of,
    within_budget,
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

__all__ = [
    "MAX_CALLS_PER_REQUEST",
    "MIN_PAGE_CHARS",
    "SEARCH_RESULT_COUNT",
    "NoResultError",
    "WebResearch",
    "WebResearchConfig",
    "usable_page",
    "usable_results",
]

_LOG = get_logger(__name__)

#: Hits asked of a search vendor. Fixed rather than chosen per call because the snapshot cache
#: key is (query, session): two counts for one query would be one cache entry.
SEARCH_RESULT_COUNT: Final[int] = 8
#: A fetched page with less text than this is "near-empty": a cookie wall, a JS shell, an error
#: stub. It is not a usable result, so it triggers the fallback.
MIN_PAGE_CHARS: Final[int] = 200
#: The chosen vendor and at most one fallback. Never a third.
MAX_CALLS_PER_REQUEST: Final[int] = 2

_SEARCHERS: Final[frozenset[VendorName]] = frozenset(
    {VendorName.BRAVE, VendorName.TAVILY, VendorName.FIRECRAWL}
)
_FETCHERS: Final[frozenset[VendorName]] = frozenset({VendorName.FIRECRAWL, VendorName.TAVILY})
#: Outcomes after which the one fallback is tried; the rest end the request.
_FALL_BACK_ON: Final[frozenset[CallOutcome]] = frozenset(
    {CallOutcome.REFUSING, CallOutcome.FAILED, CallOutcome.EMPTY}
)

_warned_unconfigured: set[VendorName] = set()
_warn_lock = threading.Lock()


class NoResultError(FetchError):
    """Neither the chosen vendor nor the one fallback produced a usable result (or no vendor had
    budget). Not a failure of the tool: the model is told there is nothing, and why."""


def usable_results(results: SearchResults) -> bool:
    """A search answer worth storing: at least one hit."""
    return len(results.results) > 0


def usable_page(document: FetchedDocument) -> bool:
    """A fetched page worth storing: at least `MIN_PAGE_CHARS` characters of text."""
    return len(document.text.strip()) >= MIN_PAGE_CHARS


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
    direct_fallback: bool
    budgets: dict[VendorName, int]
    monthly_request_cap: int = Field(ge=0)
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
            direct_fallback=settings.websearch_direct_fallback,
            budgets={
                VendorName.BRAVE: settings.websearch_budget_brave,
                VendorName.TAVILY: settings.websearch_budget_tavily,
                VendorName.FIRECRAWL: settings.websearch_budget_firecrawl,
            },
            monthly_request_cap=settings.websearch_monthly_request_cap,
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
        detail="no key configured; this vendor is out of the rotation",
    )


#: Anything the router can call: a search vendor or a fetch vendor.
_Vendor = SearchVendor | FetchVendor


class WebResearch:
    """Search and fetch across vendors, cached and evidenced through the Commons snapshot store.

    What it does: `search(query, session)` and `fetch_page(url, session)` return the Commons
    snapshot for that request — the stored one, or one routed to one vendor (at most one fallback),
    stored and returned now. As a `Fetcher` it can also stand in for `ClaudeWebFetcher` wherever a
    `SnapshotStore` is given one.
    What it assumes: one instance per process (or per M17 run); the rotation state and the
    refusing-vendor set live on it.
    What it never does: retry a vendor, call a third vendor for one request, try another vendor
    after the page's own site refused, fetch a URL the guard refused, or exceed a budget or a cap.
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
        direct: DirectFetch | None = None,
        store: SnapshotStore | None = None,
    ) -> None:
        self._config = config
        self._clock = clock
        self._ledger = ledger
        self._guard = guard
        self._search = tuple(search_vendors)
        self._fetch = tuple(fetch_vendors)
        self._direct = direct
        self._store = store
        self._refusing: set[tuple[VendorName, Capability]] = set()
        self._rotation: dict[Capability, dict[VendorName, int]] = {
            Capability.SEARCH: {},
            Capability.FETCH: {},
        }
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
        """The router this configuration describes. A vendor without a key is left out of the
        rotation (warned once per process); the direct fetcher needs none."""
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
            key, setting = keys[vendor]
            if key is None:
                _warn_unconfigured(vendor, setting)
            elif vendor is VendorName.TAVILY:
                fetchers.append(TavilyExtract(key, api(vendor)))
            else:
                fetchers.append(FirecrawlScrape(key, api(vendor)))
        direct = (
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
            if config.direct_fallback
            else None
        )
        return cls(
            config=config,
            clock=clock,
            ledger=ledger,
            guard=guard,
            search_vendors=searchers,
            fetch_vendors=fetchers,
            direct=direct,
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
        """The search vendors in the rotation (those with a key)."""
        return self._search

    @property
    def fetch_vendors(self) -> tuple[FetchVendor, ...]:
        """The fetch vendors in the rotation (those with a key); the direct fetch is not one."""
        return self._fetch

    # ── the public calls ─────────────────────────────────────────────────────────────────────

    def search(self, query: str, session: date) -> FetchOutcome:
        """The snapshot of a web search for `query` on `session`. Raises `NoResultError` when
        neither vendor asked had a usable answer, another `FetchError` when the request was
        refused (a cap), and ValueError on a query the Commons refuses."""
        return self._require_store().get_or_fetch(FetchRequest.query(query, session), self)

    def fetch_page(self, url: str, session: date) -> FetchOutcome:
        """The snapshot of `url` on `session`. Raises `UrlRefusedError` (a ValueError) for a URL
        the guard refuses — before the cache is consulted — `NoResultError` when neither vendor
        asked returned a usable page, and another `FetchError` for a refusal or a cap."""
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
            return self._route(request, Capability.SEARCH)
        try:
            self._guard.check(request.target)
        except UrlRefusedError as exc:
            raise FetchError(f"refused: {exc.reason}") from exc
        return self._route(request, Capability.FETCH)

    # ── the rotation ─────────────────────────────────────────────────────────────────────────

    def _pool(self, capability: Capability) -> tuple[_Vendor, ...]:
        return self._search if capability is Capability.SEARCH else self._fetch

    def _remaining_calls(self, vendor: _Vendor, used: dict[VendorName, int]) -> int:
        """How many more calls the vendor's monthly budget allows; 0 when it allows none."""
        budget = self._config.budget_of(vendor.name)
        spent = used.get(vendor.name, 0)
        if budget is None or not within_budget(spent, vendor.units, budget):
            return 0
        return (budget - spent) // vendor.units

    def _pick(self, capability: Capability, exclude: set[VendorName]) -> _Vendor | None:
        """The next vendor by smooth weighted round robin, weighted by remaining budget.

        Each pick adds every eligible vendor's weight to its running credit, takes the vendor
        with the most credit (ties to the configured order), and charges it the total weight.
        Over many picks each vendor's share is its weight over the total, and the sequence is
        interleaved rather than bursty — and deterministic, which keeps a replay a replay.
        """
        used = self._ledger.month_usage(month_of(self._clock.now()))
        with self._lock:
            eligible = [
                (v, self._remaining_calls(v, used))
                for v in self._pool(capability)
                if v.name not in exclude and (v.name, capability) not in self._refusing
            ]
            eligible = [(v, w) for v, w in eligible if w > 0]
            if not eligible:
                return None
            credit = self._rotation[capability]
            total = sum(w for _, w in eligible)
            for vendor, weight in eligible:
                credit[vendor.name] = credit.get(vendor.name, 0) + weight
            chosen = max(eligible, key=lambda vw: credit[vw[0].name])[0]
            credit[chosen.name] -= total
            return chosen

    # ── one request ──────────────────────────────────────────────────────────────────────────

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

    def _monthly_cap_reached(self, request: FetchRequest, capability: Capability) -> FetchError:
        cap = self._config.monthly_request_cap
        _LOG.warning(
            "websearch.monthly_cap",
            capability=capability.value,
            cap=cap,
            month=month_of(self._clock.now()).isoformat(),
            session=request.session.isoformat(),
            key=request.key,
        )
        return FetchError(
            f"the monthly web research cap of {cap} vendor requests is reached; nothing was sent"
        )

    def _route(self, request: FetchRequest, capability: Capability) -> FetchResponse:
        self._reserve_session(request, capability)
        month = month_of(self._clock.now())
        if not within_budget(self._ledger.month_total(month), 1, self._config.monthly_request_cap):
            raise self._monthly_cap_reached(request, capability)
        reasons: list[str] = []
        asked: set[VendorName] = set()
        calls = 0
        while calls < MAX_CALLS_PER_REQUEST:
            vendor: _Vendor | DirectFetch | None = self._pick(capability, asked)
            if vendor is None and capability is Capability.FETCH and self._direct is not None:
                # No fetch vendor has budget left: the direct fetch is the one fallback (or,
                # with every fetch vendor exhausted, the one call).
                vendor = self._direct if VendorName.DIRECT not in asked else None
            if vendor is None:
                if not reasons:
                    reasons.append(f"no {capability.value} vendor has a key and budget left")
                break
            asked.add(vendor.name)
            reservation = self._reserve(vendor)
            if reservation is Reservation.OVER_MONTHLY_CAP:
                raise self._monthly_cap_reached(request, capability)
            if reservation is Reservation.OVER_VENDOR_BUDGET:
                # Lost a race for the vendor's last unit: nothing was sent, so not a call.
                self._event(request, vendor.name, capability, CallOutcome.OVER_BUDGET)
                continue
            calls += 1
            response, reason = self._call(request, capability, vendor)
            if response is not None:
                return response
            reasons.append(f"{vendor.name.value}: {reason}")
        raise NoResultError(f"no usable {capability.value} result ({'; '.join(reasons)})")

    def _reserve(self, vendor: _Vendor | DirectFetch) -> Reservation:
        now = self._clock.now()
        direct = vendor.name is VendorName.DIRECT
        return self._ledger.reserve_call(
            vendor.name,
            month_of(now),
            vendor.units,
            None if direct else self._config.budget_of(vendor.name),
            None if direct else self._config.monthly_request_cap,
            at=now,
        )

    def _call(
        self, request: FetchRequest, capability: Capability, vendor: _Vendor | DirectFetch
    ) -> tuple[FetchResponse | None, str]:
        """One vendor call: the response, or None and why it is not usable.

        Raises `FetchError` for the outcomes that end the request with no fallback: the page's
        own site refused, or the URL (or where it redirected) is refused by the guard.
        """
        started = time.perf_counter()
        try:
            page, status = self._produce(request, capability, vendor)
        except UrlRefusedError as exc:
            self._event(
                request,
                vendor.name,
                capability,
                CallOutcome.TARGET_REFUSED,
                units=vendor.units,
                started=started,
                detail=exc.reason,
            )
            raise FetchError(f"refused: {exc.reason}") from exc
        except VendorError as exc:
            if exc.outcome is CallOutcome.REFUSING:
                with self._lock:
                    self._refusing.add((vendor.name, capability))
            self._event(
                request,
                vendor.name,
                capability,
                exc.outcome,
                units=vendor.units,
                status=exc.status,
                started=started,
                detail=str(exc),
            )
            if exc.outcome not in _FALL_BACK_ON:
                raise FetchError(f"{vendor.name.value}: {exc}") from exc
            return None, f"{exc.outcome.value} ({exc})"
        self._event(
            request,
            vendor.name,
            capability,
            CallOutcome.OK,
            units=vendor.units,
            status=status,
            started=started,
        )
        self._local.served = f"{vendor.name.value}:{capability.value}"
        return FetchResponse(pages=(page,)), ""

    def _produce(
        self, request: FetchRequest, capability: Capability, vendor: _Vendor | DirectFetch
    ) -> tuple[FetchedPage, int | None]:
        """Ask the vendor; the Commons page, or `VendorError(EMPTY)` when the answer is unusable.

        `usable_results` / `usable_page` are the one place "nothing usable" is decided, and an
        unusable answer is what triggers the single fallback.
        """
        if capability is Capability.SEARCH:
            results = cast(SearchVendor, vendor).search(request.target, SEARCH_RESULT_COUNT)
            if not usable_results(results):
                raise VendorError("no results", outcome=CallOutcome.EMPTY)
            return _results_page(results), None
        document = cast(FetchVendor, vendor).fetch(request.target)
        self._check_final(document)
        if not usable_page(document):
            raise VendorError(
                f"near-empty page ({len(document.text.strip())} characters of text)",
                outcome=CallOutcome.EMPTY,
                status=document.status,
            )
        return _document_page(document), document.status

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
        month = month_of(self._clock.now())
        budget = self._config.budget_of(vendor)
        remaining: int | None = None
        if budget is not None:
            remaining = max(budget - self._ledger.month_usage(month).get(vendor, 0), 0)
        fields: dict[str, object] = {
            "vendor": vendor.value,
            "capability": capability.value,
            "outcome": outcome.value,
            "units": units,
            "remaining_budget": remaining,
            "remaining_monthly_requests": max(
                self._config.monthly_request_cap - self._ledger.month_total(month), 0
            ),
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

    def _check_final(self, document: FetchedDocument) -> None:
        """A vendor that followed a redirect onto a scheduler host has fetched a page we refuse."""
        try:
            host = httpx.URL(document.final_url).host if document.final_url else ""
        except httpx.InvalidURL:
            host = ""
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
