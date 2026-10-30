"""M17 web research: our own `web_search` and `web_fetch`, over Brave, Tavily, Firecrawl and a
direct fetch, for a model that has no server-side web tools (Claude on Amazon Bedrock).

- `research.WebResearch` sends each search or fetch to ONE vendor, picked by a rotation weighted
  by remaining monthly budget, with at most one fallback, under per-vendor budgets, a monthly
  total and a per-session cap, and stores every answer as a Research Commons snapshot
  (`analyst.commons.SnapshotStore`), so the evidence trail is the one the Claude CLI fetcher left.
- `tools.WebTools` is the pair of LLM tools (`analyst.llm.ToolSpec` shape) and their handlers.
- `guard.UrlGuard` is the SSRF and scheduler-host gate every fetched URL and redirect passes.
- `ledger` keeps the counts (Postgres, migration 0021, or in memory).

Not part of any `mandate_hash`: this package changes how a manager's research is fetched, not what
a manager is, and the pre-registration amendment records the switch.

This package never imports `analyst.fundmanager`, and edits nothing in `analyst.llm`.
"""

from analyst.websearch.guard import UrlGuard, UrlRefusedError, host_is_blocked, ip_refusal
from analyst.websearch.http import (
    HttpClient,
    HttpReply,
    HttpxClient,
    RecordedHttpClient,
    WebTimeoutError,
    WebTransportError,
)
from analyst.websearch.ledger import (
    InMemoryUsageLedger,
    PostgresUsageLedger,
    Reservation,
    UsageLedger,
)
from analyst.websearch.models import (
    CallOutcome,
    Capability,
    FetchedDocument,
    SearchResult,
    SearchResults,
    VendorName,
    VendorUsage,
    month_of,
    within_budget,
)
from analyst.websearch.research import (
    MAX_CALLS_PER_REQUEST,
    MIN_PAGE_CHARS,
    SEARCH_RESULT_COUNT,
    NoResultError,
    WebResearch,
    WebResearchConfig,
    usable_page,
    usable_results,
)
from analyst.websearch.tools import (
    UNTRUSTED_CONTENT_RULE,
    WEB_FETCH,
    WEB_SEARCH,
    ToolResult,
    WebTools,
    wrap_untrusted,
)
from analyst.websearch.vendors import FetchVendor, SearchVendor, VendorError

__all__ = [
    "MAX_CALLS_PER_REQUEST",
    "MIN_PAGE_CHARS",
    "SEARCH_RESULT_COUNT",
    "UNTRUSTED_CONTENT_RULE",
    "WEB_FETCH",
    "WEB_SEARCH",
    "CallOutcome",
    "Capability",
    "FetchVendor",
    "FetchedDocument",
    "HttpClient",
    "HttpReply",
    "HttpxClient",
    "InMemoryUsageLedger",
    "NoResultError",
    "PostgresUsageLedger",
    "RecordedHttpClient",
    "Reservation",
    "SearchResult",
    "SearchResults",
    "SearchVendor",
    "ToolResult",
    "UrlGuard",
    "UrlRefusedError",
    "UsageLedger",
    "VendorError",
    "VendorName",
    "VendorUsage",
    "WebResearch",
    "WebResearchConfig",
    "WebTimeoutError",
    "WebTools",
    "WebTransportError",
    "host_is_blocked",
    "ip_refusal",
    "month_of",
    "usable_page",
    "usable_results",
    "within_budget",
    "wrap_untrusted",
]
