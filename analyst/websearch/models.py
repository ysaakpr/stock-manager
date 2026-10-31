"""The values that cross this package's boundary: results, fetched documents, call records.

Pydantic models, frozen, so a result handed to the router, the snapshot store or a test is the
same object with the same fields whichever vendor produced it.
"""

from __future__ import annotations

from datetime import date, datetime
from enum import StrEnum

from pydantic import BaseModel, ConfigDict, Field

__all__ = [
    "CallOutcome",
    "Capability",
    "FetchedDocument",
    "SearchResult",
    "SearchResults",
    "VendorName",
    "VendorUsage",
    "month_of",
    "within_budget",
]


class VendorName(StrEnum):
    """Every backend this package can route to. `DIRECT` is our own HTTP fetch: no vendor."""

    BRAVE = "brave"
    TAVILY = "tavily"
    FIRECRAWL = "firecrawl"
    DIRECT = "direct"


class Capability(StrEnum):
    """What a vendor is asked to do."""

    SEARCH = "search"
    FETCH = "fetch"


class CallOutcome(StrEnum):
    """How one routed call to one vendor ended — the `outcome` of every `websearch.call` event."""

    OK = "ok"
    NO_KEY = "no_key"
    """No credential configured: skipped, nothing sent."""
    OVER_BUDGET = "over_budget"
    """The vendor's monthly budget would be exceeded: skipped, nothing sent."""
    REFUSING = "refusing"
    """The vendor answered 401/403/429 to us, now or earlier in this process: skipped for good."""
    FAILED = "failed"
    """5xx, timeout, connection error or an unreadable body: skipped for this call."""
    EMPTY = "empty"
    """The vendor answered but had nothing (no results, page not extractable)."""
    TARGET_REFUSED = "target_refused"
    """The page's own site refused (401/403/429), or its robots.txt disallows us. Final: no other
    vendor is tried, because routing around a refusal is what AGENTIC_CONTEXT §8 forbids."""


class _Frozen(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")


class SearchResult(_Frozen):
    """One search hit, as the vendor ranked it."""

    title: str
    url: str
    snippet: str
    published: date | None = None
    """The page's own date when the vendor gave one; never inferred."""
    vendor: VendorName
    rank: int = Field(ge=1)


class SearchResults(_Frozen):
    """A vendor's answer to one query: its hits in its order."""

    query: str
    vendor: VendorName
    results: tuple[SearchResult, ...]


class FetchedDocument(_Frozen):
    """One page as fetched and cleaned to text or markdown. Untrusted: it is a page, not advice."""

    requested_url: str
    final_url: str
    status: int | None
    """The page's HTTP status as fetched, or None when the vendor does not report it (Tavily)."""
    content_type: str
    bytes: int = Field(ge=0)
    """Bytes of the body as received (or as the vendor reported the cleaned text)."""
    title: str = ""
    text: str
    vendor: VendorName


class VendorUsage(_Frozen):
    """One vendor's month-to-date spend against its budget."""

    vendor: VendorName
    month: date
    used: int = Field(ge=0)
    budget: int = Field(ge=0)

    @property
    def remaining(self) -> int:
        return max(self.budget - self.used, 0)

    def fits(self, units: int) -> bool:
        """Whether `units` more stay within the budget. The one budget comparison, shared."""
        return within_budget(self.used, units, self.budget)

    @classmethod
    def at(cls, vendor: VendorName, when: datetime, used: int, budget: int) -> VendorUsage:
        return cls(vendor=vendor, month=month_of(when), used=used, budget=budget)


def month_of(when: datetime) -> date:
    """The calendar month `when` falls in, as its first day, in `when`'s own timezone (IST)."""
    return when.date().replace(day=1)


def within_budget(used: int, units: int, budget: int) -> bool:
    """True when spending `units` on top of `used` keeps the total at or under `budget`."""
    return used + units <= budget
