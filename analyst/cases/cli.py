"""M5.8: the headless ratification path (§8.1) — a typer CLI over the same core as the web pages.

The web UX (`analyst.cases.web`) is for a human at a browser; the agent build and the tests need a
ratification that runs with no browser and no server (AGENTIC_CONTEXT B9's fixture ratification, and
M5.13's end-to-end paper case, both drive it from here). This CLI is that path: it reads a proposal
draft, shows its content hash, or grants a ratification — and because it calls the exact same
`ratify_proposal` the web route calls, the `RatificationRecord` it emits has the identical shape a
browser approval produces (acceptance 2).

Two commands:

* `hash --draft PATH` prints the proposal's content hash. Headless parity for "what would I be
  approving" — the same digest the page embeds and the approval must pin.
* `ratify --draft PATH --by NAME` grants the ratification and writes the record as JSON. Pass
  `--expect-hash` to enforce the staleness guard headlessly (acceptance 3): if the draft no longer
  hashes to what the caller last saw, it refuses with a non-zero exit rather than approve a changed
  document. Time is injected via `--at` (an ISO instant) so a test or a replay is deterministic
  (B10); without it the system clock stamps the approval.

A "draft" on disk is a `ProposalDraft` as JSON — the interview answers, the theme map and the
theses a proposal is built from — so the CLI rebuilds the proposal deterministically rather than
trusting a serialized document nobody can re-derive.
"""

from __future__ import annotations

from datetime import datetime
from pathlib import Path
from typing import Annotated

import typer

from analyst.cases.policies import RatificationKind
from analyst.cases.web import ProposalDraft, StaleProposalError, ratify_proposal
from analyst.interview import Proposal, ProposalError
from dataplatform.clock import IST, Clock, FrozenClock, SystemClock

__all__ = ["app", "main"]

app = typer.Typer(
    add_completion=False,
    help="Headless ratification of a proposal draft (§8.1).",
    no_args_is_help=True,
)

_DraftOption = Annotated[
    Path,
    typer.Option(
        "--draft",
        exists=True,
        dir_okay=False,
        readable=True,
        help="Path to a ProposalDraft JSON (answers + theme map + theses).",
    ),
]


def _load(draft_path: Path) -> ProposalDraft:
    """Read and validate a `ProposalDraft` from JSON, failing loud on a bad file."""
    try:
        return ProposalDraft.model_validate_json(draft_path.read_text(encoding="utf-8"))
    except ValueError as exc:  # pydantic ValidationError is a ValueError
        typer.echo(f"error: {draft_path} is not a valid proposal draft: {exc}", err=True)
        raise typer.Exit(code=2) from exc


def _clock(at: str | None) -> Clock:
    """The clock approvals are stamped from: a frozen instant when `--at` is given, else system."""
    if at is None:
        return SystemClock()
    try:
        return FrozenClock(datetime.fromisoformat(at), timezone=IST)
    except ValueError as exc:
        typer.echo(f"error: --at {at!r} is not an ISO-8601 instant", err=True)
        raise typer.Exit(code=2) from exc


@app.command()
def hash(draft: _DraftOption) -> None:
    """Print the proposal's content hash — what an approval would have to pin."""
    proposal = _build(draft)
    typer.echo(proposal.content_hash)


@app.command()
def ratify(
    draft: _DraftOption,
    by: Annotated[str, typer.Option("--by", help="Who is granting the ratification.")],
    kind: Annotated[
        RatificationKind,
        typer.Option("--kind", help="HUMAN, or FIXTURE for the B9 paper/test path."),
    ] = RatificationKind.HUMAN,
    expect_hash: Annotated[
        str | None,
        typer.Option(
            "--expect-hash",
            help="Refuse unless the draft still hashes to this (the staleness guard).",
        ),
    ] = None,
    at: Annotated[
        str | None,
        typer.Option("--at", help="ISO-8601 instant to stamp the approval (default: now)."),
    ] = None,
    out: Annotated[
        Path | None,
        typer.Option("--out", help="Write the record JSON here (default: stdout)."),
    ] = None,
) -> None:
    """Grant a ratification for the draft and emit the record as JSON."""
    proposal_draft = _load(draft)
    try:
        _, record = ratify_proposal(
            proposal_draft,
            by=by,
            clock=_clock(at),
            kind=kind,
            displayed_hash=expect_hash,
        )
    except StaleProposalError as exc:
        typer.echo(f"error: {exc}", err=True)
        raise typer.Exit(code=1) from exc
    except (ProposalError, ValueError) as exc:
        typer.echo(f"error: cannot ratify: {exc}", err=True)
        raise typer.Exit(code=2) from exc

    payload = record.model_dump_json(indent=2)
    if out is None:
        typer.echo(payload)
    else:
        out.write_text(payload + "\n", encoding="utf-8")
        typer.echo(f"wrote {out}", err=True)


def _build(draft_path: Path) -> Proposal:
    """Load a draft and build its proposal, turning a build error into a clean CLI failure."""
    proposal_draft = _load(draft_path)
    try:
        return proposal_draft.build()
    except ProposalError as exc:
        typer.echo(f"error: cannot build proposal: {exc}", err=True)
        raise typer.Exit(code=2) from exc


def main() -> None:
    """Console entry point."""
    app()


if __name__ == "__main__":  # pragma: no cover - module CLI entry
    app()
