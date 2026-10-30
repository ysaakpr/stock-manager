"""The vendors: Brave, Tavily and Firecrawl over their official APIs, and our own direct fetch.

Endpoints and fields as each vendor's documentation gave them on 2026-10-30:

- **Brave Search API** (search) — `GET https://api.search.brave.com/res/v1/web/search?q=&count=`,
  header `X-Subscription-Token`. Hits in `web.results[]`: `title`, `url`, `description` (may hold
  `<strong>` highlighting), optional `page_age` (the page's published or modified date).
  Docs: api-dashboard.search.brave.com/app/documentation/web-search/get-started.
- **Tavily search** — `POST https://api.tavily.com/search`, `Authorization: Bearer`. Body
  `query`, `max_results`, `search_depth: basic` (1 credit; `advanced` is 2, and `auto_parameters`
  could pick it, so both are pinned), `include_published_date`. Hits in `results[]`: `title`,
  `url`, `content`, `score`, `published_date`. Docs: docs.tavily.com/.../endpoint/search.
- **Tavily extract** (fetch) — `POST https://api.tavily.com/extract`, body `urls`,
  `extract_depth: basic`, `format: markdown`, `timeout`. `results[]` (`url`, `raw_content`) and
  `failed_results[]` (`url`, `error`), both possible on a 200. Docs: .../endpoint/extract.
- **Firecrawl scrape** (fetch) — `POST https://api.firecrawl.dev/v2/scrape`, `Authorization:
  Bearer`. Body `url`, `formats: ["markdown"]`, `onlyMainContent`, `timeout` (ms), `maxAge`, and
  `proxy: "basic"`. Response `data.markdown` and `data.metadata` (`title`, `sourceURL`, `url` the
  final URL, `statusCode` the page's own status, `contentType`). Docs:
  docs.firecrawl.dev/api-reference/endpoint/scrape.
- **Firecrawl search** — `POST https://api.firecrawl.dev/v2/search`, body `query`, `limit`.
  Hits in `data.web[]`: `title`, `description`, `url`, `position`. 2 credits per 10 results.

**`proxy: "basic"` on Firecrawl is deliberate.** Its default, `auto`, retries a blocked page
through "enhanced" proxies: that is routing around a site's refusal, which AGENTIC_CONTEXT §8
forbids. Pinned to `basic`, a site that refuses Firecrawl refuses us.

**How a failure is classified** (`VendorError.outcome`):

- the vendor refuses *us* — 401, 403, 429, or a plan/credit limit (402, Tavily's 432/433) — is
  `REFUSING`: the router skips that vendor for the rest of the process and never retries it;
- 5xx, a timeout, a broken connection or an unreadable body is `FAILED`: skipped for this call;
- the *page's own site* refusing (a 401/403/429 the vendor reports for the target, or robots.txt
  disallowing the direct fetcher) is `TARGET_REFUSED`, which ends the fetch: another vendor
  asking the same site again is the workaround §8 forbids;
- nothing usable (an empty page, a 404) is `EMPTY`; zero hits and near-empty pages are judged
  by the router (`research.usable_results`, `usable_page`), which owns the one fallback.

What this module never does: put a key anywhere but its request header, log a key, retry, or
fetch a URL the `UrlGuard` has not passed.
"""

from __future__ import annotations

import html
import re
from collections.abc import Mapping
from datetime import date, datetime
from typing import Any, Final, Protocol
from urllib.parse import urlencode, urljoin, urlsplit
from urllib.robotparser import RobotFileParser

from pydantic import SecretStr

from analyst.websearch.guard import UrlGuard
from analyst.websearch.html_text import html_to_text
from analyst.websearch.http import HttpClient, HttpReply, WebTransportError
from analyst.websearch.models import (
    CallOutcome,
    FetchedDocument,
    SearchResult,
    SearchResults,
    VendorName,
)
from dataplatform.ingest.policy import RateLimiter

__all__ = [
    "BRAVE_SEARCH_URL",
    "FIRECRAWL_SCRAPE_URL",
    "FIRECRAWL_SEARCH_URL",
    "TAVILY_EXTRACT_URL",
    "TAVILY_SEARCH_URL",
    "BraveSearch",
    "DirectFetch",
    "FetchVendor",
    "FirecrawlScrape",
    "FirecrawlSearch",
    "SearchVendor",
    "TavilyExtract",
    "TavilySearch",
    "VendorApi",
    "VendorError",
]

BRAVE_SEARCH_URL: Final[str] = "https://api.search.brave.com/res/v1/web/search"
TAVILY_SEARCH_URL: Final[str] = "https://api.tavily.com/search"
TAVILY_EXTRACT_URL: Final[str] = "https://api.tavily.com/extract"
FIRECRAWL_SCRAPE_URL: Final[str] = "https://api.firecrawl.dev/v2/scrape"
FIRECRAWL_SEARCH_URL: Final[str] = "https://api.firecrawl.dev/v2/search"

#: Statuses with which a vendor refuses *us*: auth, rate limit, plan or credit limit.
_REFUSING: Final[frozenset[int]] = frozenset({401, 402, 403, 429, 432, 433})
#: Statuses with which a page's own site refuses whoever asked.
_TARGET_REFUSING: Final[frozenset[int]] = frozenset({401, 403, 429})
#: Spacing between two calls to one vendor API, and between two direct requests to one host is
#: the repo's crawl spacing (`HTTP_MIN_INTERVAL_SECONDS`).
VENDOR_MIN_INTERVAL_SECONDS: Final[float] = 1.0
_TEXT_TYPES: Final[frozenset[str]] = frozenset(
    {"text/plain", "text/markdown", "application/json", "text/csv", "application/xml", "text/xml"}
)
_HTML_TYPES: Final[frozenset[str]] = frozenset({"text/html", "application/xhtml+xml"})
_TAGS = re.compile(r"<[^>]+>")


class VendorError(RuntimeError):
    """One vendor could not serve one call. The message is safe to log: it never holds a key."""

    def __init__(self, message: str, *, outcome: CallOutcome, status: int | None = None) -> None:
        super().__init__(message)
        self.outcome = outcome
        self.status = status


class SearchVendor(Protocol):
    """A search backend: a query in, ranked results out."""

    @property
    def name(self) -> VendorName: ...

    @property
    def units(self) -> int:
        """What one call costs against the vendor's monthly budget (its billing unit)."""
        ...

    def search(self, query: str, count: int) -> SearchResults: ...


class FetchVendor(Protocol):
    """A fetch backend: a URL in, the page as clean text or markdown out."""

    @property
    def name(self) -> VendorName: ...

    @property
    def units(self) -> int: ...

    def fetch(self, url: str) -> FetchedDocument: ...


# ── shared plumbing ──────────────────────────────────────────────────────────────────────────


class VendorApi:
    """One vendor's HTTP access: spacing, the call, and the status classification."""

    def __init__(
        self,
        vendor: VendorName,
        http: HttpClient,
        limiter: RateLimiter,
        *,
        timeout: float,
        max_bytes: int,
    ) -> None:
        self.vendor = vendor
        self._http = http
        self._limiter = limiter
        self.timeout = timeout
        self._max_bytes = max_bytes

    def call(
        self,
        method: str,
        url: str,
        headers: Mapping[str, str],
        json_body: Mapping[str, Any] | None = None,
    ) -> Any:
        self._limiter.acquire(f"api:{self.vendor.value}", VENDOR_MIN_INTERVAL_SECONDS)
        try:
            reply = self._http.send(
                method,
                url,
                headers=headers,
                timeout=self.timeout,
                max_bytes=self._max_bytes,
                json_body=json_body,
            )
        except WebTransportError as exc:
            raise VendorError(f"{self.vendor.value}: {exc}", outcome=CallOutcome.FAILED) from exc
        _raise_for_vendor_status(self.vendor, reply)
        if reply.truncated:
            raise VendorError(
                f"{self.vendor.value}: response larger than {self._max_bytes} bytes",
                outcome=CallOutcome.FAILED,
                status=reply.status,
            )
        try:
            return reply.json_body()
        except ValueError as exc:
            raise VendorError(
                f"{self.vendor.value}: response is not JSON",
                outcome=CallOutcome.FAILED,
                status=reply.status,
            ) from exc


def _raise_for_vendor_status(vendor: VendorName, reply: HttpReply) -> None:
    status = reply.status
    if 200 <= status < 300:
        return
    if status in _REFUSING:
        raise VendorError(
            f"{vendor.value} refused the request (HTTP {status}); not retried",
            outcome=CallOutcome.REFUSING,
            status=status,
        )
    raise VendorError(
        f"{vendor.value} answered HTTP {status}", outcome=CallOutcome.FAILED, status=status
    )


def _bearer(key: SecretStr) -> dict[str, str]:
    return {
        "Authorization": f"Bearer {key.get_secret_value()}",
        "Content-Type": "application/json",
        "Accept": "application/json",
    }


def _clean_snippet(text: str) -> str:
    return " ".join(html.unescape(_TAGS.sub("", text)).split())


def _date_of(value: Any) -> date | None:
    """A vendor's date string as a date, or None. Never guessed from "2 days ago"."""
    if not isinstance(value, str) or not value.strip():
        return None
    text = value.strip()
    try:
        return datetime.fromisoformat(text.replace("Z", "+00:00")).date()
    except ValueError:
        pass
    for fmt in ("%a, %d %b %Y %H:%M:%S %Z", "%a, %d %b %Y %H:%M:%S %z"):
        try:
            return datetime.strptime(text, fmt).date()
        except ValueError:
            continue
    return None


def _items(payload: Any, *path: str) -> list[dict[str, Any]]:
    node = payload
    for key in path:
        if not isinstance(node, dict):
            return []
        node = node.get(key)
    return [item for item in node if isinstance(item, dict)] if isinstance(node, list) else []


def _results(
    vendor: VendorName, query: str, raw: list[dict[str, Any]], snippet_key: str, date_key: str
) -> SearchResults:
    hits: list[SearchResult] = []
    for item in raw:
        url, title = item.get("url"), item.get("title")
        if not isinstance(url, str) or not url.strip():
            continue
        snippet = item.get(snippet_key)
        hits.append(
            SearchResult(
                title=_clean_snippet(title) if isinstance(title, str) else "",
                url=url.strip(),
                snippet=_clean_snippet(snippet) if isinstance(snippet, str) else "",
                published=_date_of(item.get(date_key)),
                vendor=vendor,
                rank=len(hits) + 1,
            )
        )
    # No hits is returned, not raised: whether a result is usable is the router's one decision.
    return SearchResults(query=query, vendor=vendor, results=tuple(hits))


# ── Brave ────────────────────────────────────────────────────────────────────────────────────


class BraveSearch:
    """Brave Search API web search. One request per call."""

    name = VendorName.BRAVE
    units = 1

    def __init__(self, key: SecretStr, api: VendorApi) -> None:
        self._key = key
        self._api = api

    def __repr__(self) -> str:
        return "BraveSearch(key=**********)"

    def search(self, query: str, count: int) -> SearchResults:
        url = f"{BRAVE_SEARCH_URL}?{urlencode({'q': query, 'count': min(max(count, 1), 20)})}"
        headers = {
            "Accept": "application/json",
            "X-Subscription-Token": self._key.get_secret_value(),
        }
        payload = self._api.call("GET", url, headers)
        return _results(
            self.name, query, _items(payload, "web", "results"), "description", "page_age"
        )


# ── Tavily ───────────────────────────────────────────────────────────────────────────────────


class TavilySearch:
    """Tavily search at `basic` depth: 1 credit per call."""

    name = VendorName.TAVILY
    units = 1

    def __init__(self, key: SecretStr, api: VendorApi) -> None:
        self._key = key
        self._api = api

    def __repr__(self) -> str:
        return "TavilySearch(key=**********)"

    def search(self, query: str, count: int) -> SearchResults:
        body = {
            "query": query,
            "max_results": min(max(count, 1), 20),
            "search_depth": "basic",
            "auto_parameters": False,
            "topic": "general",
            "include_answer": False,
            "include_raw_content": False,
            "include_published_date": True,
        }
        payload = self._api.call("POST", TAVILY_SEARCH_URL, _bearer(self._key), body)
        return _results(self.name, query, _items(payload, "results"), "content", "published_date")


class TavilyExtract:
    """Tavily extract of one URL at `basic` depth, as markdown. Counted as 1 credit per call
    (Tavily bills 1 per 5 successful URLs, so this over-counts, never under-counts)."""

    name = VendorName.TAVILY
    units = 1

    def __init__(self, key: SecretStr, api: VendorApi) -> None:
        self._key = key
        self._api = api

    def __repr__(self) -> str:
        return "TavilyExtract(key=**********)"

    def fetch(self, url: str) -> FetchedDocument:
        body = {
            "urls": [url],
            "extract_depth": "basic",
            "format": "markdown",
            "timeout": min(max(self._api.timeout, 1.0), 60.0),
        }
        payload = self._api.call("POST", TAVILY_EXTRACT_URL, _bearer(self._key), body)
        for item in _items(payload, "results"):
            content = item.get("raw_content")
            if isinstance(content, str) and content.strip():
                final = item.get("url")
                return FetchedDocument(
                    requested_url=url,
                    final_url=final if isinstance(final, str) and final else url,
                    status=None,
                    content_type="text/markdown",
                    bytes=len(content.encode("utf-8")),
                    text=content,
                    vendor=self.name,
                )
        failed = _items(payload, "failed_results")
        reason = failed[0].get("error") if failed else None
        raise VendorError(
            f"tavily could not extract the page: {str(reason or 'no content')[:200]}",
            outcome=CallOutcome.EMPTY,
        )


# ── Firecrawl ────────────────────────────────────────────────────────────────────────────────


class FirecrawlScrape:
    """Firecrawl scrape of one URL, main content as markdown, basic proxy only: 1 credit."""

    name = VendorName.FIRECRAWL
    units = 1

    def __init__(self, key: SecretStr, api: VendorApi) -> None:
        self._key = key
        self._api = api

    def __repr__(self) -> str:
        return "FirecrawlScrape(key=**********)"

    def fetch(self, url: str) -> FetchedDocument:
        body = {
            "url": url,
            "formats": ["markdown"],
            "onlyMainContent": True,
            "timeout": int(min(max(self._api.timeout, 1.0), 300.0) * 1000),
            "maxAge": 0,
            "proxy": "basic",
        }
        payload = self._api.call("POST", FIRECRAWL_SCRAPE_URL, _bearer(self._key), body)
        if not isinstance(payload, dict) or payload.get("success") is False:
            raise VendorError("firecrawl reported failure", outcome=CallOutcome.FAILED)
        data = payload.get("data")
        if not isinstance(data, dict):
            raise VendorError("firecrawl returned no data", outcome=CallOutcome.FAILED)
        raw_meta = data.get("metadata")
        meta: dict[str, Any] = raw_meta if isinstance(raw_meta, dict) else {}
        status = meta.get("statusCode")
        status = status if isinstance(status, int) and not isinstance(status, bool) else None
        if status in _TARGET_REFUSING:
            raise VendorError(
                f"the site refused the page (HTTP {status}); not asked again by another route",
                outcome=CallOutcome.TARGET_REFUSED,
                status=status,
            )
        if status is not None and status >= 400:
            raise VendorError(
                f"the page answered HTTP {status}", outcome=CallOutcome.EMPTY, status=status
            )
        markdown = data.get("markdown")
        if not isinstance(markdown, str) or not markdown.strip():
            raise VendorError("firecrawl extracted no text", outcome=CallOutcome.EMPTY)
        title = meta.get("title")
        if isinstance(title, list):
            title = next((t for t in title if isinstance(t, str)), "")
        final = meta.get("url") or meta.get("sourceURL") or url
        content_type = meta.get("contentType")
        return FetchedDocument(
            requested_url=url,
            final_url=final if isinstance(final, str) else url,
            status=status,
            content_type=content_type if isinstance(content_type, str) else "text/markdown",
            bytes=len(markdown.encode("utf-8")),
            title=title if isinstance(title, str) else "",
            text=markdown,
            vendor=self.name,
        )


class FirecrawlSearch:
    """Firecrawl web search, at most 10 results: 2 credits per call."""

    name = VendorName.FIRECRAWL
    units = 2

    def __init__(self, key: SecretStr, api: VendorApi) -> None:
        self._key = key
        self._api = api

    def __repr__(self) -> str:
        return "FirecrawlSearch(key=**********)"

    def search(self, query: str, count: int) -> SearchResults:
        body = {"query": query[:500], "limit": min(max(count, 1), 10)}
        payload = self._api.call("POST", FIRECRAWL_SEARCH_URL, _bearer(self._key), body)
        if isinstance(payload, dict) and payload.get("success") is False:
            raise VendorError("firecrawl search reported failure", outcome=CallOutcome.FAILED)
        raw = _items(payload, "data", "web")
        raw.sort(key=_position)
        return _results(self.name, query, raw, "description", "publishedDate")


def _position(item: dict[str, Any]) -> int:
    position = item.get("position")
    return position if isinstance(position, int) and not isinstance(position, bool) else 10**6


# ── direct ───────────────────────────────────────────────────────────────────────────────────


class DirectFetch:
    """Our own GET plus HTML-to-text: no vendor and no quota, so the last fetch fallback.

    What it does: passes every hop through `UrlGuard` (scheme, port, scheduler hosts, resolution,
    and the connected peer), follows at most `max_redirects` redirects itself, honours robots.txt
    on every hop (fetched once per origin per process; a 3xx or 4xx there means no rules, a 5xx
    or no answer means disallow, as RFC 9309 has it), spaces requests to one host by the repo's
    crawl spacing, sends the repo's one user agent, and reads at most `max_bytes` of body.
    What it never does: retry, change its user agent, or fetch on after a 401/403/429.
    """

    name = VendorName.DIRECT
    units = 1

    def __init__(
        self,
        http: HttpClient,
        guard: UrlGuard,
        limiter: RateLimiter,
        *,
        user_agent: str,
        min_interval_seconds: float,
        timeout: float,
        max_bytes: int,
        max_redirects: int,
    ) -> None:
        self._http = http
        self._guard = guard
        self._limiter = limiter
        self._user_agent = user_agent
        self._interval = min_interval_seconds
        self._timeout = timeout
        self._max_bytes = max_bytes
        self._max_redirects = max_redirects
        self._robots: dict[str, RobotFileParser | None] = {}

    def _get(self, url: str) -> HttpReply:
        self._guard.check(url)
        host = urlsplit(url).hostname or ""
        self._limiter.acquire(f"direct:{host}", self._interval)
        try:
            return self._http.send(
                "GET",
                url,
                headers={"User-Agent": self._user_agent, "Accept": "text/html,text/plain,*/*"},
                timeout=self._timeout,
                max_bytes=self._max_bytes,
                check_peer=self._guard.check_peer,
            )
        except WebTransportError as exc:
            raise VendorError(f"direct: {exc}", outcome=CallOutcome.FAILED) from exc

    def _allowed_by_robots(self, url: str) -> bool:
        parts = urlsplit(url)
        origin = f"{parts.scheme}://{parts.netloc}"
        if origin not in self._robots:
            parser: RobotFileParser | None
            try:
                reply = self._get(f"{origin}/robots.txt")
            except VendorError:
                parser = None
            else:
                parser = RobotFileParser()
                if 200 <= reply.status < 300:
                    parser.parse(reply.body.decode("utf-8", errors="replace").splitlines())
                elif 400 <= reply.status < 500 or 300 <= reply.status < 400:
                    parser.parse([])  # no rules published: everything allowed
                else:
                    parser = None
            self._robots[origin] = parser
        rules = self._robots[origin]
        return rules is not None and rules.can_fetch(self._user_agent, url)

    def fetch(self, url: str) -> FetchedDocument:
        current = url
        for _ in range(self._max_redirects + 1):
            if not self._allowed_by_robots(current):
                raise VendorError(
                    "the site's robots.txt does not allow this page (or could not be read)",
                    outcome=CallOutcome.TARGET_REFUSED,
                )
            reply = self._get(current)
            location = reply.headers.get("location")
            if 300 <= reply.status < 400 and location:
                current = urljoin(current, location)
                continue
            return self._document(url, current, reply)
        raise VendorError(
            f"more than {self._max_redirects} redirects; stopped", outcome=CallOutcome.FAILED
        )

    def _document(self, requested: str, final: str, reply: HttpReply) -> FetchedDocument:
        if reply.status in _TARGET_REFUSING:
            raise VendorError(
                f"the site refused the page (HTTP {reply.status}); not retried",
                outcome=CallOutcome.TARGET_REFUSED,
                status=reply.status,
            )
        if reply.status >= 500:
            raise VendorError(
                f"the page answered HTTP {reply.status}",
                outcome=CallOutcome.FAILED,
                status=reply.status,
            )
        if reply.status >= 300:
            raise VendorError(
                f"the page answered HTTP {reply.status}",
                outcome=CallOutcome.EMPTY,
                status=reply.status,
            )
        content_type = reply.content_type or "application/octet-stream"
        raw = reply.body.decode(_charset(reply) or "utf-8", errors="replace")
        if content_type in _HTML_TYPES:
            title, text = html_to_text(raw)
        elif content_type in _TEXT_TYPES:
            title, text = "", raw
        else:
            raise VendorError(
                f"the page is {content_type}, which the direct fetcher does not read",
                outcome=CallOutcome.EMPTY,
                status=reply.status,
            )
        if not text.strip():
            raise VendorError("the page has no readable text", outcome=CallOutcome.EMPTY)
        if reply.truncated:
            text += f"\n\n[body cut at {self._max_bytes} bytes; the rest was not downloaded]"
        return FetchedDocument(
            requested_url=requested,
            final_url=final,
            status=reply.status,
            content_type=content_type,
            bytes=len(reply.body),
            title=title,
            text=text,
            vendor=self.name,
        )


def _charset(reply: HttpReply) -> str | None:
    match = re.search(r"charset=([\w-]+)", reply.headers.get("content-type", ""), re.IGNORECASE)
    if match is None:
        return None
    name = match.group(1)
    try:
        "".encode(name)
    except LookupError:
        return None
    return name
