"""`GET /status/websearch`: month-to-date web research use per vendor and in total.

Reads migration 0021's `websearch_vendor_month` and `websearch_month_total` for the IST calendar
month of `as_of`, and the budgets, the monthly cap and the rotations from `Settings`
(WEBSEARCH_*). Reports whether each vendor's key is configured — a boolean, never the key — when
this process holds any vendor key, and `null` when it holds none: the status app container is never
handed the keys (ops/docker-compose.yml blanks them), so there it cannot know. The
counters are written by `analyst.websearch.ledger`; this module only reads them (no import of
`analyst`, which sits above the platform).
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
    configured: bool | None = Field(
        description=(
            "A key is set (always true for direct, which needs none); null when this process holds "
            "no vendor key at all, as the status app never does"
        )
    )
    used: int = Field(description="Units reserved this month, in the vendor's billing unit")
    requests: int = Field(description="Calls sent this month, fallbacks included")
    budget: int | None = Field(description="Monthly budget; None for direct, which is uncapped")
    remaining: int | None
    in_search_rotation: bool
    in_fetch_rotation: bool = Field(description="For direct: allowed as the last-resort fallback")


class WebsearchOut(BaseModel):
    """`GET /status/websearch`."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    as_of: datetime
    month: date = Field(description="First day of the IST calendar month reported")
    vendors: list[WebsearchVendorOut]
    total_requests: int = Field(description="Vendor requests this month, all vendors (not direct)")
    monthly_request_cap: int
    remaining_requests: int
    session_max_searches: int
    session_max_fetches: int


def _order(raw: str) -> list[str]:
    return [part.strip().lower() for part in raw.split(",") if part.strip()]


def read_websearch_usage(conn: Connection, settings: Settings, *, as_of: datetime) -> WebsearchOut:
    """Month-to-date usage per vendor. A vendor with no row this month has used 0."""
    month = as_of.astimezone(settings.tzinfo).date().replace(day=1)
    rows = conn.execute(
        "SELECT vendor, used, requests FROM websearch_vendor_month WHERE month = %s", (month,)
    ).fetchall()
    used = {str(vendor): int(units) for vendor, units, _ in rows}
    requests = {str(vendor): int(calls) for vendor, _, calls in rows}
    total_row = conn.execute(
        "SELECT requests FROM websearch_month_total WHERE month = %s", (month,)
    ).fetchone()
    total = 0 if total_row is None else int(total_row[0])
    cap = settings.websearch_monthly_request_cap
    budgets: dict[str, int | None] = {
        "brave": settings.websearch_budget_brave,
        "tavily": settings.websearch_budget_tavily,
        "firecrawl": settings.websearch_budget_firecrawl,
        "direct": None,
    }
    held = {
        "brave": settings.brave_search_api_key is not None,
        "tavily": settings.tavily_api_key is not None,
        "firecrawl": settings.firecrawl_api_key is not None,
    }
    # No key at all is the status app's normal state (it is never handed them), not a desk with
    # every vendor unconfigured: say "unknown" rather than three false negatives.
    knows = any(held.values())
    configured: dict[str, bool | None] = {
        **{vendor: (key if knows else None) for vendor, key in held.items()},
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
                requests=requests.get(vendor, 0),
                budget=budget,
                remaining=None if budget is None else max(budget - spent, 0),
                in_search_rotation=vendor in search,
                in_fetch_rotation=(
                    settings.websearch_direct_fallback if vendor == "direct" else vendor in fetch
                ),
            )
        )
    return WebsearchOut(
        as_of=as_of,
        month=month,
        vendors=vendors,
        total_requests=total,
        monthly_request_cap=cap,
        remaining_requests=max(cap - total, 0),
        session_max_searches=settings.websearch_session_max_searches,
        session_max_fetches=settings.websearch_session_max_fetches,
    )
