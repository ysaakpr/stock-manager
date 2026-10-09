"""M17.3: the Commons fetcher from a shell. Look something up, show a snapshot, audit the store.

Four commands, all over the one `SnapshotStore` that the M17 harness uses:

* `query TEXT --session D` / `url URL --session D` return the snapshot for that (request, session).
  They fetch live through `ClaudeWebFetcher` only on a miss, and print a JSON summary: id,
  cache_hit, source URLs, byte length and truncation. Without `--allow-fetch`, a miss exits 1
  rather than spend a model call, so an operator browsing the cache never fetches by accident.
* `show ID` prints a stored snapshot's text after its digest check.
* `verify` walks every snapshot and index line and exits 1 if anything fails its digest.

The session is required rather than defaulted to today: a session is a trading date, and the
calendar date when someone happens to run this may not be one.
"""

from __future__ import annotations

import json
from datetime import date
from pathlib import Path
from typing import Annotated

import typer

from analyst.commons.fetch import (
    DEFAULT_FETCH_MODEL,
    ClaudeWebFetcher,
    FetchError,
    FetchRequest,
    Snapshot,
    SnapshotIntegrityError,
    SnapshotStore,
    default_store_root,
)
from dataplatform.clock import SystemClock
from dataplatform.config import get_settings

__all__ = ["app", "main"]

app = typer.Typer(
    add_completion=False,
    help="The Research Commons web-fetch cache (pre-registration §5).",
    no_args_is_help=True,
)

_RootOption = Annotated[
    Path | None,
    typer.Option("--root", help="Store root (default: <data_root>/commons/fetch)."),
]
_SessionOption = Annotated[
    str, typer.Option("--session", help="The trading session the request belongs to (ISO date).")
]
_AllowFetchOption = Annotated[
    bool,
    typer.Option("--allow-fetch", help="On a cache miss, fetch live through the Claude CLI."),
]
_ModelOption = Annotated[str, typer.Option("--model", help="The fetcher's model.")]


def _store(root: Path | None) -> SnapshotStore:
    base = default_store_root(get_settings().data_root) if root is None else root
    return SnapshotStore(base, clock=SystemClock())


def _session(value: str) -> date:
    try:
        return date.fromisoformat(value)
    except ValueError as exc:
        typer.echo(f"error: --session {value!r} is not an ISO date", err=True)
        raise typer.Exit(code=2) from exc


def _summary(snapshot: Snapshot, *, cache_hit: bool) -> str:
    return json.dumps(
        {
            "id": snapshot.id,
            "cache_hit": cache_hit,
            "kind": snapshot.request.kind.value,
            "target": snapshot.request.target,
            "session": snapshot.request.session.isoformat(),
            "fetched_at": snapshot.fetched_at.isoformat(),
            "fetcher": snapshot.fetcher,
            "source_urls": list(snapshot.source_urls),
            "byte_length": snapshot.byte_length,
            "truncated": snapshot.truncated,
            "pages_dropped": snapshot.pages_dropped,
        },
        indent=2,
    )


def _resolve(request: FetchRequest, root: Path | None, allow_fetch: bool, model: str) -> None:
    store = _store(root)
    try:
        stored = store.lookup(request)
        if stored is not None:
            typer.echo(_summary(stored, cache_hit=True))
            return
        if not allow_fetch:
            typer.echo("error: not in the cache; pass --allow-fetch to fetch it live", err=True)
            raise typer.Exit(code=1)
        outcome = store.get_or_fetch(request, ClaudeWebFetcher(model=model))
    except (FetchError, SnapshotIntegrityError) as exc:
        typer.echo(f"error: {exc}", err=True)
        raise typer.Exit(code=1) from exc
    typer.echo(_summary(outcome.snapshot, cache_hit=outcome.cache_hit))


@app.command()
def query(
    text: Annotated[str, typer.Argument(help="The search terms.")],
    session: _SessionOption,
    root: _RootOption = None,
    allow_fetch: _AllowFetchOption = False,
    model: _ModelOption = DEFAULT_FETCH_MODEL,
) -> None:
    """The snapshot for a web search on a session."""
    try:
        request = FetchRequest.query(text, _session(session))
    except ValueError as exc:
        typer.echo(f"error: {exc}", err=True)
        raise typer.Exit(code=2) from exc
    _resolve(request, root, allow_fetch, model)


@app.command()
def url(
    target: Annotated[str, typer.Argument(help="The page URL (http or https).")],
    session: _SessionOption,
    root: _RootOption = None,
    allow_fetch: _AllowFetchOption = False,
    model: _ModelOption = DEFAULT_FETCH_MODEL,
) -> None:
    """The snapshot for one page on a session."""
    try:
        request = FetchRequest.url(target, _session(session))
    except ValueError as exc:
        typer.echo(f"error: {exc}", err=True)
        raise typer.Exit(code=2) from exc
    _resolve(request, root, allow_fetch, model)


@app.command()
def show(
    snapshot_id: Annotated[str, typer.Argument(help="The snapshot id (sha256).")],
    root: _RootOption = None,
) -> None:
    """Print a stored snapshot's text, after its digest check."""
    try:
        snapshot = _store(root).get(snapshot_id)
    except KeyError as exc:
        typer.echo(f"error: no snapshot {snapshot_id}", err=True)
        raise typer.Exit(code=1) from exc
    except (SnapshotIntegrityError, ValueError) as exc:
        typer.echo(f"error: {exc}", err=True)
        raise typer.Exit(code=1) from exc
    typer.echo(snapshot.text)


@app.command()
def verify(root: _RootOption = None) -> None:
    """Check every snapshot's digest and every index line; exit 1 on any problem."""
    problems = _store(root).verify()
    for problem in problems:
        typer.echo(problem, err=True)
    typer.echo(f"{len(problems)} problem(s)")
    if problems:
        raise typer.Exit(code=1)


def main() -> None:
    """Console entry point."""
    app()


if __name__ == "__main__":  # pragma: no cover - module CLI entry
    app()
