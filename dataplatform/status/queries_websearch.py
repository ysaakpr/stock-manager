"""`GET /status/websearch`: month-to-date web research spend per vendor against its budget.

Reads migration 0021's `websearch_vendor_month` for the IST calendar month of `as_of`, and the
budgets and routing order from `Settings` (WEBSEARCH_*). Reports whether each vendor's key is
configured — a boolean, never the key. The counters are written by `analyst.websearch.ledger`;
this module only reads them (no import of `analyst`, which sits above the platform).
"""

from __future__ import annotations

from datetime import date, datetime

from pydantic import BaseModel, ConfigDict, Field

from dataplatform.config import Settings
from dataplatform.store.db import Connection

__all__ = ["WebsearchOut", "WebsearchVendorOut", "read_websearch_usage"]


class WebsearchVendorOut(BaseModel):
    """One vendor's month so far."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    vendor: str
    configured: bool = Field(description="A key is set (always true for direct, which needs none)")
    used: int = Field(description="Units reserved this month: requests, or credits for Firecrawl")
    budget: int | None = Field(description="Monthly budget; None for direct, which is uncapped")
    remaining: int | None
    search_rank: int | None = Field(description="1-based place in the search order, if listed")
    fetch_rank: int | None = Field(description="1-based place in the fetch order, if listed")


class WebsearchOut(BaseModel):
    """`GET /status/websearch`."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    as_of: datetime
    month: date = Field(description="First day of the IST calendar month reported")
    vendors: list[WebsearchVendorOut]
    session_max_searches: int
    session_max_fetches: int


def _order(raw: str) -> list[str]:
    return [part.strip().lower() for part in raw.split(",") if part.strip()]


def read_websearch_usage(conn: Connection, settings: Settings, *, as_of: datetime) -> WebsearchOut:
    """Month-to-date usage per vendor. A vendor with no row this month has used 0."""
    month = as_of.astimezone(settings.tzinfo).date().replace(day=1)
    rows = conn.execute(
        "SELECT vendor, used FROM websearch_vendor_month WHERE month = %s", (month,)
    ).fetchall()
    used = {str(vendor): int(count) for vendor, count in rows}
    budgets: dict[str, int | None] = {
        "brave": settings.websearch_budget_brave,
        "tavily": settings.websearch_budget_tavily,
        "firecrawl": settings.websearch_budget_firecrawl,
        "direct": None,
    }
    configured = {
        "brave": settings.brave_search_api_key is not None,
        "tavily": settings.tavily_api_key is not None,
        "firecrawl": settings.firecrawl_api_key is not None,
        "direct": True,
    }
    search = _order(settings.websearch_search_vendors)
    fetch = _order(settings.websearch_fetch_vendors)
    vendors = []
    for vendor, budget in budgets.items():
        spent = used.get(vendor, 0)
        vendors.append(
            WebsearchVendorOut(
                vendor=vendor,
                configured=configured[vendor],
                used=spent,
                budget=budget,
                remaining=None if budget is None else max(budget - spent, 0),
                search_rank=search.index(vendor) + 1 if vendor in search else None,
                fetch_rank=fetch.index(vendor) + 1 if vendor in fetch else None,
            )
        )
    return WebsearchOut(
        as_of=as_of,
        month=month,
        vendors=vendors,
        session_max_searches=settings.websearch_session_max_searches,
        session_max_fetches=settings.websearch_session_max_fetches,
    )
