"""One live probe per configured vendor: does each key work, and how fast is each vendor?

    uv run python -m analyst.websearch.live_check [--ledger postgres|memory] [--url URL]

Reports, per vendor key, only *set* or *not set* (never a value). Then, for each search vendor
with a key, one search for a neutral query, and for each fetch vendor with a key, one fetch of a
public news page: at most five requests. This probes connectivity and keys, so it deliberately
bypasses the rotation and asks every keyed vendor once. Each call is reserved against the vendor's
budget and the monthly total first (`--ledger postgres`, the default, needs migration 0021
applied; `memory` counts in-process only), and the result or byte count, the latency and the HTTP
status are printed. Vendors without a key are skipped and said so. The direct fetcher is not
probed: it has no key and no quota.

Nothing is stored in the Research Commons: this is a probe, not research.
"""

from __future__ import annotations

import time
from typing import Annotated

import typer

from analyst.websearch.ledger import (
    InMemoryUsageLedger,
    PostgresUsageLedger,
    Reservation,
    UsageLedger,
)
from analyst.websearch.models import VendorName, month_of
from analyst.websearch.research import SEARCH_RESULT_COUNT, WebResearch
from analyst.websearch.vendors import VendorError
from dataplatform.clock import SystemClock
from dataplatform.config import get_settings
from dataplatform.logging import configure_logging
from dataplatform.store.db import connect

__all__ = ["DEFAULT_QUERY", "DEFAULT_URL", "app", "main"]

DEFAULT_QUERY = "Reserve Bank of India repo rate"
#: A public news page on a host the scheduler does not own.
DEFAULT_URL = "https://www.bbc.com/news/business"

app = typer.Typer(add_completion=False, help=__doc__)


@app.command()
def main(
    ledger: Annotated[str, typer.Option(help="postgres | memory")] = "postgres",
    query: Annotated[str, typer.Option(help="The neutral search query.")] = DEFAULT_QUERY,
    url: Annotated[str, typer.Option(help="The public page to fetch.")] = DEFAULT_URL,
) -> None:
    """Probe each configured vendor once."""
    settings = get_settings()
    configure_logging(settings)
    keys = {
        "BRAVE_SEARCH_API_KEY": settings.brave_search_api_key,
        "TAVILY_API_KEY": settings.tavily_api_key,
        "FIRECRAWL_API_KEY": settings.firecrawl_api_key,
    }
    for name, value in keys.items():
        typer.echo(f"{name}: {'set' if value is not None else 'not set'}")
    clock = SystemClock(timezone=settings.tzinfo)
    usage: UsageLedger = (
        PostgresUsageLedger(lambda: connect(settings, autocommit=True))
        if ledger == "postgres"
        else InMemoryUsageLedger()
    )
    research = WebResearch.from_settings(settings, clock=clock, ledger=usage)
    searchers = research.search_vendors
    fetchers = [v for v in research.fetch_vendors if v.name is not VendorName.DIRECT]
    if not searchers and not fetchers:
        typer.echo("no vendor has a key: nothing probed")
        return
    research.guard.check(url)

    def admitted(vendor: VendorName, units: int, capability: str) -> bool:
        now = clock.now()
        budget = research.config.budget_of(vendor)
        cap = research.config.monthly_request_cap
        outcome = usage.reserve_call(vendor, month_of(now), units, budget, cap, at=now)
        if outcome is not Reservation.OK:
            typer.echo(f"{vendor.value} {capability}: {outcome.value}, not sent")
            return False
        return True

    for searcher in searchers:
        if not admitted(searcher.name, searcher.units, "search"):
            continue
        started = time.perf_counter()
        try:
            found = searcher.search(query, SEARCH_RESULT_COUNT)
            detail, status = f"{len(found.results)} results", "200"
        except VendorError as exc:
            detail, status = f"{exc.outcome.value}: {exc}", str(exc.status or "-")
        _report(searcher.name, "search", detail, status, started)
    for fetcher in fetchers:
        if not admitted(fetcher.name, fetcher.units, "fetch"):
            continue
        started = time.perf_counter()
        try:
            document = fetcher.fetch(url)
            detail = f"{document.bytes} bytes of text"
            status = "not reported" if document.status is None else str(document.status)
        except VendorError as exc:
            detail, status = f"{exc.outcome.value}: {exc}", str(exc.status or "-")
        _report(fetcher.name, "fetch", detail, status, started)


def _report(vendor: VendorName, capability: str, detail: str, status: str, started: float) -> None:
    latency = round((time.perf_counter() - started) * 1000)
    typer.echo(f"{vendor.value} {capability}: {detail}; status {status}; {latency} ms")


if __name__ == "__main__":
    app()
