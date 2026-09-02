"""M5.8: the ratification UX (§8.1) — review one proposal, edit it, approve it in one act.

§8.1 asks for "minimal web pages: review a proposal, see each policy and thesis with its evidence,
edit, approve." §5.1 fixes what approval *is*: one document, one ratification, pinned to the exact
content approved. This module is those pages plus the framework-free core they and the CLI
(`analyst.cases.cli`) share, so a headless ratification and a browser ratification produce the same
governance artifact (acceptance 2).

Three things it guarantees, one per acceptance criterion:

* **A proposal renders, an edit round-trips, approval pins a content hash.** The review page shows
  the universe with purity scores, a thesis per holding with its break conditions, and the seven
  §5.2 policies. An edit changes an interview answer and rebuilds the proposal (`ProposalDraft`),
  which changes the content hash exactly as `proposal.py` describes — "an edit that changes an
  answer changes the policy it drives, and so changes the hash". Approval writes a
  `RatificationRecord` pinning `Proposal.content_hash`.

* **The record shape is identical whoever grants it.** `ratify_proposal` is the one place a
  ratification is minted; the web route and the typer CLI both call it and both emit the same
  `RatificationRecord`. Neither branches on "web vs CLI" — the difference is who calls, not what is
  produced.

* **A ratification cannot be granted for a proposal that changed since it was displayed.** The
  review page embeds the content hash it rendered; approval sends it back, and `ratify_proposal`
  refuses when the proposal's current hash differs (`StaleProposalError`). `Proposal.ratified_with`
  enforces the same at the model level, so the guard holds even if a caller skips the displayed
  hash — this is belt and suspenders around invariant "one document, one ratification".

Clock is injected (B10): the only time in a ratification is `Ratification.at`, taken from the
`Clock` the app or CLI was built with, never from the wall clock here. Nothing in this module
reaches a database or the network — a proposal is held in a `ProposalStore` the caller supplies,
which a test wires in memory and a deployment could back with the A1 case service. What approval
*means* for the case lifecycle (`RATIFIED`, funding) is `analyst.cases.service`'s job; this module
produces the artifact that records the human's approval and hands it back.
"""

from __future__ import annotations

import html
from collections.abc import Mapping
from decimal import Decimal, InvalidOperation
from enum import StrEnum
from pathlib import Path
from typing import Any, Final
from urllib.parse import parse_qsl

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import HTMLResponse
from pydantic import BaseModel, ConfigDict, Field

from analyst.cases.policies import POLICY_FIELDS, Ratification, RatificationKind
from analyst.interview import (
    ConcentrationTolerance,
    InterviewAnswers,
    Proposal,
    ProposalRatificationMismatchError,
    RiskAppetite,
    build_proposal,
)
from analyst.mapper import ThemeMap
from analyst.thesis import BreakCondition, Thesis
from dataplatform.clock import Clock, SystemClock

__all__ = [
    "EDITABLE_ANSWER_FIELDS",
    "InMemoryProposalStore",
    "ProposalDraft",
    "ProposalNotFoundError",
    "ProposalStore",
    "RatificationRecord",
    "RatifyUxError",
    "StaleProposalError",
    "apply_answer_edits",
    "create_app",
    "ratify_proposal",
    "render_proposal",
    "render_ratified",
]

_TEMPLATES: Final = Path(__file__).parent / "templates"


class RatifyUxError(Exception):
    """Base for every ratification-UX failure, so a caller can catch the module."""


class ProposalNotFoundError(RatifyUxError):
    """No proposal draft is staged for that case id."""


class StaleProposalError(RatifyUxError):
    """Approval was attempted against a content hash that is no longer the proposal's.

    The whole point of acceptance 3: a human approves the document they read, not whatever it
    became after they read it. When an edit lands between rendering the page and clicking approve,
    the displayed hash no longer matches and the approval is refused rather than silently applied to
    the changed document.
    """


#: Interview answers a reviewer may edit from the page. Exactly the answers that drive the seven
#: §5.2 policies without changing the *universe* — a policy edit re-derives the dial, the rails, the
#: cadence and so changes the content hash, while leaving the theme map's candidates and their
#: per-holding theses untouched, so the rebuilt proposal stays internally consistent. Removing a
#: holding (an exclusions edit) changes the universe and the thesis set together and is a separate,
#: heavier operation (noted in ops/BACKLOG.md), deliberately not offered here.
EDITABLE_ANSWER_FIELDS: Final[tuple[str, ...]] = (
    "sip_amount_inr",
    "sip_day_of_month",
    "horizon_years",
    "risk_appetite",
    "concentration_tolerance",
    "benchmark_secondary",
    "top_up_rule",
)


class ProposalDraft(BaseModel):
    """The inputs one ratifiable proposal is built from, so an edit rebuilds it deterministically.

    What it does: carries the interview answers, the theme map (A3) and the per-holding theses (A4)
    a `Proposal` is assembled from, and rebuilds that proposal on demand. Holding the inputs rather
    than the built document is what lets an edit be "change an answer, rebuild" — the model §5.1
    prescribes, where a change is a new document with a new hash, not a mutation of a frozen one.
    What it assumes: the theme map was mapped for `answers.theme` as-of its date, and there is one
    `PROPOSAL`/`CORE` thesis per holding the answers do not exclude — `build_proposal` re-checks
    both, so an inconsistent draft fails loudly when built.
    What it never does: hold the built `Proposal` as state (it is derived), or ratify — approval is
    `ratify_proposal`.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    case_id: str = Field(min_length=1, description="The case this proposal is for.")
    answers: InterviewAnswers = Field(
        description="The §5.1 interview answers the proposal derives."
    )
    theme_map: ThemeMap = Field(description="The A3 theme map the universe is drawn from.")
    theses: tuple[Thesis, ...] = Field(
        min_length=1, description="One §5.3 thesis per proposed holding, keyed by ISIN on build."
    )

    def build(self) -> Proposal:
        """Assemble the `Proposal` from these inputs (deterministic; no clock, no network)."""
        return build_proposal(
            case_id=self.case_id,
            answers=self.answers,
            theme_map=self.theme_map,
            theses={thesis.isin: thesis for thesis in self.theses},
        )

    def with_answers(self, answers: InterviewAnswers) -> ProposalDraft:
        """This draft with new answers — the shape an edit produces (a fresh, immutable draft)."""
        return self.model_copy(update={"answers": answers})


class RatificationRecord(BaseModel):
    """The governance artifact §5.1 requires: who approved what, when, and pinned to which content.

    What it does: records one approval — the `Ratification` (who/when/kind/hash) alongside the case
    it is for and the *exact* ratifiable content that hash covers, so the approved document is
    recoverable from the record and not merely referenced by a digest.
    What it assumes: `ratification.content_hash` was computed from `ratifiable_content` — minted by
    `ratify_proposal`, which derives both from one `Proposal`, so they cannot disagree.
    What it never does: differ by how it was granted. The web page and the CLI both produce this
    shape, which is what "an identical record shape" (acceptance 2) means.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    case_id: str = Field(min_length=1, description="The case the approval is for.")
    theme: str = Field(min_length=1, description="The theme approved — for a human reading it.")
    content_hash: str = Field(
        pattern=r"^sha256:[0-9a-f]{64}$", description="The proposal content the approval pins."
    )
    ratification: Ratification = Field(description="Who approved, when, and of what kind.")
    ratifiable_content: dict[str, Any] = Field(
        description="The exact document the hash covers — the universe, theses and policies."
    )


def apply_answer_edits(answers: InterviewAnswers, edits: Mapping[str, str]) -> InterviewAnswers:
    """Return `answers` with the given edits applied and fully re-validated.

    What it does: parses each edit (a form field name to its raw string value) into the type the
    `InterviewAnswers` field needs, merges it over the current answers, and re-validates the whole
    object — so an out-of-range SIP day or an unknown risk level is refused here, not stored.
    What it assumes: `edits` keys are in `EDITABLE_ANSWER_FIELDS`; any other key is rejected, so the
    universe-driving `exclusions` (and identity fields like `theme`) cannot be changed through this
    path.
    What it never does: mutate `answers` (it is frozen) or default a value — a blank edit for a
    required field is a parse error, not a silent reset.

    Raises `ValueError` on an unknown field or an unparseable value.
    """
    unknown = sorted(set(edits) - set(EDITABLE_ANSWER_FIELDS))
    if unknown:
        raise ValueError(
            f"cannot edit {', '.join(unknown)}; editable answers are "
            f"{', '.join(EDITABLE_ANSWER_FIELDS)}"
        )
    merged: dict[str, Any] = answers.model_dump()
    for field, raw in edits.items():
        merged[field] = _parse_answer_field(field, raw)
    return InterviewAnswers.model_validate(merged)


def _parse_answer_field(field: str, raw: str) -> Any:
    """Parse one raw edit value into the type its `InterviewAnswers` field requires."""
    value = raw.strip()
    if field in ("benchmark_secondary", "top_up_rule"):
        return value or None
    if field == "risk_appetite":
        return _parse_enum(field, value, RiskAppetite)
    if field == "concentration_tolerance":
        return None if not value else _parse_enum(field, value, ConcentrationTolerance)
    if field == "sip_amount_inr":
        try:
            return Decimal(value.replace(",", ""))
        except (InvalidOperation, ValueError) as exc:
            raise ValueError(f"{field}: {raw!r} is not a rupee amount") from exc
    # sip_day_of_month, horizon_years
    try:
        return int(value)
    except ValueError as exc:
        raise ValueError(f"{field}: {raw!r} is not a whole number") from exc


def _parse_enum[E: StrEnum](field: str, value: str, enum: type[E]) -> E:
    """Parse a stated level into `enum`, case-insensitively, or raise `ValueError`."""
    for member in enum:
        if member.value.casefold() == value.casefold():
            return member
    allowed = ", ".join(member.value for member in enum)
    raise ValueError(f"{field}: {value!r} is not one of {allowed}")


def ratify_proposal(
    draft: ProposalDraft,
    *,
    by: str,
    clock: Clock,
    kind: RatificationKind = RatificationKind.HUMAN,
    displayed_hash: str | None = None,
) -> tuple[Proposal, RatificationRecord]:
    """Mint the one ratification for a draft's proposal — the core the web page and CLI share.

    What it does: builds the proposal, refuses it if `displayed_hash` no longer matches (acceptance
    3), stamps a `Ratification` at `clock.now()` pinned to the proposal's content hash, ratifies the
    proposal against that hash, and returns the ratified proposal with its `RatificationRecord`.
    What it assumes: `by` names the human (or the B9 fixture) granting it; `kind` is the truth about
    who approved — `FIXTURE` is paper/test only (B9), and the funding path, not this function,
    enforces that real money needs `HUMAN`.
    What it never does: read the wall clock (time comes from `clock`), persist anything (the caller
    owns the store), or produce a different record shape for the CLI than for the page.

    Raises `StaleProposalError` when `displayed_hash` is given and stale.
    """
    proposal = draft.build()
    content_hash = proposal.content_hash
    if displayed_hash is not None and displayed_hash != content_hash:
        raise StaleProposalError(
            f"approval pins {displayed_hash} but the proposal for case {draft.case_id} now hashes "
            f"to {content_hash}: it changed after it was displayed, so re-review before approving"
        )
    ratification = Ratification(by=by, at=clock.now(), kind=kind, content_hash=content_hash)
    # `ratified_with` re-checks the hash: even a caller that passed no displayed hash cannot ratify
    # content other than what it built, so "one document, one ratification" holds unconditionally.
    ratified = proposal.ratified_with(ratification)
    record = RatificationRecord(
        case_id=draft.case_id,
        theme=proposal.theme,
        content_hash=content_hash,
        ratification=ratification,
        ratifiable_content=dict(proposal.ratifiable_content),
    )
    return ratified, record


class ProposalStore:
    """Where the pages find the proposal under review and leave the record of its approval.

    An interface, deliberately small: `get`/`put` a draft, `record` an approval, read it back. A
    test wires the in-memory subclass; a deployment could back it with the A1 case service's
    `pending_policy`. Keeping it abstract is what lets this module stay free of a database and a
    clock — the pages operate on drafts and records, and where those live is the caller's choice.
    """

    def get(self, case_id: str) -> ProposalDraft:
        """The draft staged for `case_id`, or raise `ProposalNotFoundError`."""
        raise NotImplementedError

    def put(self, draft: ProposalDraft) -> None:
        """Stage (or replace) the draft for its case — how an edit is saved."""
        raise NotImplementedError

    def record(self, record: RatificationRecord) -> None:
        """Persist one approval's governance artifact."""
        raise NotImplementedError

    def ratification(self, case_id: str) -> RatificationRecord | None:
        """The recorded approval for `case_id`, or None if it has not been ratified here."""
        raise NotImplementedError


class InMemoryProposalStore(ProposalStore):
    """A `ProposalStore` backed by two dicts — for tests and a single-process review session."""

    def __init__(self) -> None:
        self._drafts: dict[str, ProposalDraft] = {}
        self._records: dict[str, RatificationRecord] = {}

    def get(self, case_id: str) -> ProposalDraft:
        try:
            return self._drafts[case_id]
        except KeyError as exc:
            raise ProposalNotFoundError(f"no proposal staged for case {case_id!r}") from exc

    def put(self, draft: ProposalDraft) -> None:
        self._drafts[draft.case_id] = draft

    def record(self, record: RatificationRecord) -> None:
        self._records[record.case_id] = record

    def ratification(self, case_id: str) -> RatificationRecord | None:
        return self._records.get(case_id)


# ── rendering (templates/, no engine: a small tokeniser keeps this dependency-free) ──────────────


def _template(name: str) -> str:
    """Read one template file from `templates/`."""
    return (_TEMPLATES / name).read_text(encoding="utf-8")


def _fill(name: str, fields: Mapping[str, str]) -> str:
    """Fill `{{token}}` placeholders in a template with already-prepared strings.

    A deliberately tiny substitution rather than Jinja: the pages are fixed and few, every dynamic
    value is escaped by its builder before it arrives here, and adding a templating engine (and its
    typing) to the dependency set would be gold-plating for three static pages.
    """
    text = _template(name)
    for key, value in fields.items():
        text = text.replace("{{" + key + "}}", value)
    return text


def _esc(value: object) -> str:
    """HTML-escape any value for safe interpolation into a page."""
    return html.escape(str(value))


def render_proposal(proposal: Proposal, *, message: str = "") -> str:
    """The review page: the universe with purity, a thesis per holding, the seven policies, forms.

    The rendered page embeds `proposal.content_hash` in the approve form, which is what carries the
    displayed hash back to the server and makes the staleness guard (acceptance 3) possible.
    """
    universe_rows = "\n".join(
        _row(c.isin, c.name, c.value_chain_stage, c.purity.score) for c in proposal.universe
    )
    thesis_blocks = "\n".join(_render_thesis(thesis) for thesis in proposal.theses)
    policy_rows = "\n".join(
        _policy_row(field, proposal.policy_set.policies[field]) for field in POLICY_FIELDS
    )
    recommendation_rows = "\n".join(
        _row(rec.policy, rec.traced_from, rec.stated_value, rec.reasoning)
        for rec in proposal.recommendations
    )
    answers = proposal.answers
    body = _fill(
        "proposal.html",
        {
            "case_id": _esc(proposal.case_id),
            "theme": _esc(proposal.theme),
            "as_of": _esc(proposal.as_of.isoformat()),
            "content_hash": _esc(proposal.content_hash),
            "message": _esc(message) if message else "",
            "universe_rows": universe_rows,
            "thesis_blocks": thesis_blocks,
            "policy_rows": policy_rows,
            "recommendation_rows": recommendation_rows,
            "sip_amount_inr": _esc(answers.sip_amount_inr),
            "sip_day_of_month": _esc(answers.sip_day_of_month),
            "horizon_years": _esc(answers.horizon_years),
            "risk_options": _options(RiskAppetite, answers.risk_appetite.value),
            "concentration_options": _concentration_options(answers),
            "benchmark_secondary": _esc(answers.benchmark_secondary or ""),
            "top_up_rule": _esc(answers.top_up_rule or ""),
        },
    )
    return _fill("base.html", {"title": _esc(f"Ratify {proposal.case_id}"), "body": body})


def _row(*cells: object) -> str:
    """One `<tr>` of plain-text cells, each escaped."""
    return "<tr>" + "".join(f"<td>{_esc(cell)}</td>" for cell in cells) + "</tr>"


def _policy_row(field: str, policy: object) -> str:
    """One policy's `<tr>`: the name and its document in a `<pre>` block."""
    return (
        f'<tr><td>{_esc(field)}</td><td><pre class="policy">{_esc(_pretty(policy))}</pre></td></tr>'
    )


def _break_condition(bc: BreakCondition) -> str:
    """One break condition as an `<li>` — id, type/tier, and the falsifiable condition."""
    tags = f"{_esc(bc.type.value)}/{_esc(bc.evaluation_tier.value)}"
    return f"<li><b>{_esc(bc.id)}</b> [{tags}]: {_esc(bc.condition)}</li>"


def _render_thesis(thesis: Thesis) -> str:
    """One holding's thesis with its break conditions — the evidence a reviewer reads."""
    evidence = "".join(f"<li>{_esc(item)}</li>" for item in thesis.expected_evidence)
    conditions = "".join(_break_condition(bc) for bc in thesis.break_conditions)
    return (
        '<div class="thesis">'
        f"<h4>{_esc(thesis.isin)} — theme purity {_esc(thesis.theme_purity)}</h4>"
        f"<p><b>Driver:</b> {_esc(thesis.driver)}</p>"
        f"<p><b>Expected evidence:</b></p><ul>{evidence}</ul>"
        f"<p><b>Break conditions:</b></p><ul>{conditions}</ul>"
        "</div>"
    )


def _pretty(value: object) -> str:
    """A compact, human-readable rendering of one policy's JSON-safe document."""
    if isinstance(value, Mapping):
        return "\n".join(f"{key}: {inner}" for key, inner in value.items())
    return str(value)


def _options(enum: type[StrEnum], selected: str) -> str:
    """`<option>`s for an enum, with the current value pre-selected."""
    return "".join(
        '<option value="{v}"{sel}>{v}</option>'.format(
            v=_esc(member.value),
            sel=" selected" if member.value == selected else "",
        )
        for member in enum
    )


def _concentration_options(answers: InterviewAnswers) -> str:
    """Concentration `<option>`s, with a blank "match risk appetite" entry when it was derived."""
    derived = answers.concentration_was_derived
    stated = answers.concentration_tolerance
    selected = "" if derived or stated is None else stated.value
    blank_selected = " selected" if derived else ""
    head = f'<option value=""{blank_selected}>(match risk appetite)</option>'
    return head + _options(ConcentrationTolerance, selected)


def render_ratified(record: RatificationRecord) -> str:
    """The confirmation page: the governance artifact just written, in full."""
    body = _fill(
        "ratified.html",
        {
            "case_id": _esc(record.case_id),
            "theme": _esc(record.theme),
            "by": _esc(record.ratification.by),
            "at": _esc(record.ratification.at.isoformat()),
            "kind": _esc(record.ratification.kind.value),
            "content_hash": _esc(record.content_hash),
        },
    )
    return _fill("base.html", {"title": _esc(f"Ratified {record.case_id}"), "body": body})


# ── the FastAPI app ──────────────────────────────────────────────────────────────────────────────


def create_app(store: ProposalStore, *, clock: Clock | None = None) -> FastAPI:
    """The ratification web app over `store`, timestamping approvals from the injected `clock`.

    What it does: serves the three §8.1 pages — review (`GET /cases/{id}`), edit
    (`POST /cases/{id}/edit`, an HTMX round-trip that rebuilds the proposal and re-renders it) and
    approve (`POST /cases/{id}/approve`, which mints the ratification and records it).
    What it assumes: the store already holds the draft to review; staging a draft is A2's job, not a
    page's.
    What it never does: read the wall clock (approvals are stamped from `clock`), or approve a
    proposal that changed since it was displayed — a stale hash answers 409, not 200.
    """
    the_clock = SystemClock() if clock is None else clock
    app = FastAPI(title="Ratification UX", version="1")

    @app.get("/cases/{case_id}", response_class=HTMLResponse)
    def review(case_id: str) -> HTMLResponse:
        draft = _require(store, case_id)
        return HTMLResponse(render_proposal(draft.build()))

    @app.post("/cases/{case_id}/edit", response_class=HTMLResponse)
    async def edit(case_id: str, request: Request) -> HTMLResponse:
        draft = _require(store, case_id)
        form = await _form(request)
        # Only fields the reviewer actually submitted count as edits. A blank string is a real edit
        # for the optional free-text/concentration fields (it clears them) but a no-op for the
        # required numeric/enum ones, so the latter are skipped when blank rather than parsed.
        always = {"concentration_tolerance", "benchmark_secondary", "top_up_rule"}
        edits = {
            field: form[field]
            for field in EDITABLE_ANSWER_FIELDS
            if field in form and (field in always or form[field].strip())
        }
        try:
            answers = apply_answer_edits(draft.answers, edits)
        except ValueError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc
        edited = draft.with_answers(answers)
        # Rebuild before storing: an edit that produced an inconsistent proposal (it will not, since
        # the universe is untouched) must fail here rather than be saved and break approval later.
        proposal = edited.build()
        store.put(edited)
        return HTMLResponse(
            render_proposal(proposal, message="Edit applied — re-review, then approve.")
        )

    @app.post("/cases/{case_id}/approve", response_class=HTMLResponse)
    async def approve(case_id: str, request: Request) -> HTMLResponse:
        draft = _require(store, case_id)
        form = await _form(request)
        by = form.get("by", "").strip()
        content_hash = form.get("content_hash", "")
        kind = form.get("kind", RatificationKind.HUMAN.value)
        if not by:
            raise HTTPException(status_code=422, detail="an approval must name who granted it")
        if not content_hash:
            raise HTTPException(status_code=422, detail="an approval must pin a content hash")
        try:
            ratification_kind = RatificationKind(kind)
        except ValueError as exc:
            raise HTTPException(
                status_code=422, detail=f"unknown ratification kind {kind!r}"
            ) from exc
        try:
            _, record = ratify_proposal(
                draft,
                by=by,
                clock=the_clock,
                kind=ratification_kind,
                displayed_hash=content_hash,
            )
        except StaleProposalError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        except (ProposalRatificationMismatchError, ValueError) as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc
        store.record(record)
        return HTMLResponse(render_ratified(record))

    return app


async def _form(request: Request) -> dict[str, str]:
    """Parse a urlencoded POST body into a flat dict.

    Read manually rather than via FastAPI's `Form(...)`, which would pull in `python-multipart` for
    what an HTMX form posts as plain `application/x-www-form-urlencoded` — one stdlib call
    (`parse_qsl`) does it, and keeps the ratification UX and its offline test free of that
    dependency. Later values win, matching how a form serialises repeated names.
    """
    body = (await request.body()).decode("utf-8")
    return dict(parse_qsl(body, keep_blank_values=True))


def _require(store: ProposalStore, case_id: str) -> ProposalDraft:
    """The staged draft, or a 404 — the one place a missing proposal becomes an HTTP status."""
    try:
        return store.get(case_id)
    except ProposalNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
