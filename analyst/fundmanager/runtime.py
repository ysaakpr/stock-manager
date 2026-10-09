"""A10 · M17.4 — one fund manager, one session: bounded research rounds, then the decisions.

Pre-registration §4 steps 3-4 with §8 Amendment 1 (d), (g). `run_manager` is the whole session of
one manager, in isolation:

1. **Round 0** — one call with `schemas.research_tool`: the manager sees its mandate, its own book
   and open rationales (`bundle.ManagerBook`, no cost basis or P&L), the market sheet and regime,
   the screens, the shortlist, the base-rate cells and the cost hurdles, and returns its holdings
   triage and research requests.
2. **Fulfilment** — the harness, not the model: a dossier for every requested name in the
   session's universe and for every holding (`analyst.commons.build_dossiers`), the filing digests
   of those names (on demand, `analyst.commons.digest_for_isins`, cache first), and each web query
   or URL through the Commons snapshot store, cache first (`SnapshotStore.get_or_fetch`). A request
   over the mandate's caps is truncated and the truncation journaled.
3. **Research rounds** — at most ``rounds.max_research_rounds`` (2) calls, each showing every
   bundle so far and allowed one more request within the same caps; an empty request ends them.
4. **Final call** — one call with `schemas.decision_tool`. Each well-formed decision then passes
   `contract.validate_decision`; one that fails is voided (journaled ``FM_DECISION_REFUSED``,
   never staged) and the rest stand.

So a session costs at most ``1 + max_research_rounds + 1`` = 4 calls. **One repair retry** per
session sits outside that budget: a malformed output (wrong shape, no structured output, a
truncated answer) is re-asked once with the error appended; a second malformed output — or a
failed model call — ends the session with ``MANAGER_ERROR`` and nothing accepted.

**The journal** (every entry in the manager's own stream, ``case_id`` = its id, tagged ``PAPER``):

- ``FM_CALL`` — one per model call, repair included: its round, the prompt digest, the token
  counts, and as evidence the call's bundle with the exact rendered prompt (the evidence pack).
  ``tokens`` carries the priced spend when the dated price card knows the model; when it does not,
  the counts are in the payload with ``priced = false`` and no rupee figure is invented.
- ``FM_RESEARCH_TRUNCATED`` — what an over-long request asked for and what was dropped.
- ``FM_RESEARCH_FULFILLED`` — per fulfilment: the kept request, every dossier digest, digest id
  and snapshot id handed over, cache hits, and what could not be fulfilled and why.
- ``FM_DECISION`` — one per accepted decision, with the flat payload
  `scoreboard.decision_payload` defines (so `scoreboard.scored_decision_from_entry` reads it back)
  plus the whole decision record, its citations, its round trip and its stop.
- ``FM_DECISION_REFUSED`` — a voided decision and every reason.
- ``NO_ACTION`` — the whole-session "nothing today" and its reason.
- ``MANAGER_ERROR`` — the session failed; nothing is accepted, so nothing can be staged.

What it never does: stage an order (M17.7 turns accepted decisions into book orders through the
M17.5 rails), read a wall clock, show the manager anything about another manager, or let a
decision that failed the contract through.
"""

from __future__ import annotations

import json
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from datetime import date
from decimal import Decimal
from enum import StrEnum
from typing import Any, Final, Protocol

from accounting import AccountingError, TokenPricer, load_price_card
from analyst.commons import (
    BaseRateTable,
    CommonsRefusedError,
    CommonsScreens,
    CommonsSheets,
    Dossier,
    Fetcher,
    FetchError,
    FetchOutcome,
    FetchRequest,
    FilingDigest,
    OnDemandDigests,
    ScreenSource,
    Shortlist,
    Snapshot,
    UniverseRow,
    build_dossiers,
)
from analyst.commons.base_rates import HORIZONS
from analyst.fundmanager.books import PAPER_MODE, BookJournal
from analyst.fundmanager.bundle import (
    UNRANKED_TIER,
    ManagerBook,
    ResearchBundle,
    SlippageCurve,
    Unfulfilled,
    tier_hurdles,
)
from analyst.fundmanager.contract import (
    CitationIndex,
    ContractVerdict,
    DecisionContext,
    NameFacts,
    shown_cells,
    validate_decision,
)
from analyst.fundmanager.mandate import HorizonBand, ManagerMandate
from analyst.fundmanager.render import (
    REGIME_DEFINITION,
    ROUND_FINAL,
    ROUND_RESEARCH,
    ROUND_ZERO,
    SYSTEM_PROMPT,
    PromptTemplate,
    render_base_rates,
    render_bundles,
    render_cost_hurdles,
    render_forced_reviews,
    render_holdings,
    render_market,
    render_screens,
    render_shortlist,
)
from analyst.fundmanager.schemas import (
    SCHEMA_VERSION,
    Action,
    MalformedOutputError,
    ManagerDecisions,
    QueryItem,
    QueryKind,
    ResearchRequest,
    decision_tool,
    parse_decisions,
    parse_research,
    research_tool,
)
from analyst.fundmanager.scoreboard import (
    DECISION_EVENT,
    DecisionAction,
    ScoredDecision,
    decision_payload,
)
from analyst.journal.evidence import EvidenceBundle, EvidenceItem, EvidenceKind
from analyst.journal.models import Actor, Decision, JournalEntry, Sleeve, TokenSpend
from analyst.llm import LLM, LLMError, LLMResponse, Message, Role, ToolSpec, prompt_digest
from dataplatform.clock import Clock
from dataplatform.logging import get_logger
from execution.costs import CostModel

__all__ = [
    "CALL_EVENT",
    "MANAGER_ERROR_EVENT",
    "NO_ACTION_EVENT",
    "REFUSED_EVENT",
    "RESEARCH_EVENT",
    "TOKEN_PURPOSE",
    "TRUNCATION_EVENT",
    "DigestProvider",
    "DossierProvider",
    "ManagerCommons",
    "ManagerSessionResult",
    "SessionStatus",
    "SnapshotCache",
    "manager_horizons",
    "run_manager",
]

_LOG = get_logger(__name__)

CALL_EVENT: Final = "FM_CALL"
TRUNCATION_EVENT: Final = "FM_RESEARCH_TRUNCATED"
RESEARCH_EVENT: Final = "FM_RESEARCH_FULFILLED"
REFUSED_EVENT: Final = "FM_DECISION_REFUSED"
NO_ACTION_EVENT: Final = "NO_ACTION"
MANAGER_ERROR_EVENT: Final = "MANAGER_ERROR"
#: ``token_usage.purpose`` of every manager call.
TOKEN_PURPOSE: Final = "m17_manager"
#: The decision line's sleeve: an M17 book is all tactical (pre-registration §1).
_SLEEVE: Final = Sleeve.TACTICAL

_REPAIR_SUFFIX: Final = (
    "\n\n## Your previous answer was refused\n\n"
    "It did not match the required schema:\n\n{errors}\n\n"
    "Return the complete answer again, in the required schema. This is the only retry."
)

DossierProvider = Callable[[Sequence[str]], tuple[Dossier, ...]]
#: On-demand filing digests for named ISINs: `analyst.commons.digest_for_isins` bound to the
#: session, its source, the digest model, the digest store and the green gate.
DigestProvider = Callable[[Sequence[str]], OnDemandDigests]


class SnapshotCache(Protocol):
    """The slice of `analyst.commons.SnapshotStore` the runtime fetches through: cache first."""

    def get_or_fetch(self, request: FetchRequest, fetcher: Fetcher) -> FetchOutcome: ...


@dataclass(frozen=True, slots=True)
class ManagerCommons:
    """The session's shared facts, as one manager's runtime reads them.

    The builds are the Commons' own (sheets, screens, shortlist, the frozen base-rate table). The
    base-rate table is injected rather than loaded here: until its digest is pinned,
    `base_rates.load_frozen` refuses, and the caller decides what that means for the session.
    ``dossiers`` and ``digests`` fulfil research for named ISINs; ``snapshots`` is the web cache.
    ``cost_model`` and ``slippage`` are what the paper book fills with (the shared cost model and
    the SimBroker slippage), so a BUY's hurdle is the round trip it will actually pay.
    """

    sheets: CommonsSheets
    screens: CommonsScreens
    shortlist: Shortlist
    base_rates: BaseRateTable
    dossiers: DossierProvider
    snapshots: SnapshotCache
    cost_model: CostModel
    slippage: SlippageCurve
    digests: DigestProvider | None = None

    @classmethod
    def from_builds(
        cls,
        *,
        sheets: CommonsSheets,
        screens: CommonsScreens,
        shortlist: Shortlist,
        base_rates: BaseRateTable,
        source: ScreenSource,
        snapshots: SnapshotCache,
        cost_model: CostModel,
        slippage: SlippageCurve,
        digests: DigestProvider | None = None,
    ) -> ManagerCommons:
        """Commons whose dossiers are `build_dossiers` over ``source`` for these builds."""

        def dossiers(isins: Sequence[str]) -> tuple[Dossier, ...]:
            return build_dossiers(isins, screens=screens, sheets=sheets, source=source)

        return cls(
            sheets=sheets,
            screens=screens,
            shortlist=shortlist,
            base_rates=base_rates,
            dossiers=dossiers,
            snapshots=snapshots,
            cost_model=cost_model,
            digests=digests,
            slippage=slippage,
        )

    def verify(self, session: date) -> None:
        """Every build reproduces its digest, is ``session``'s, and reads the same sheets build."""
        self.sheets.verify()
        self.screens.verify()
        self.shortlist.verify()
        self.base_rates.verify()
        dates = {
            "sheets": self.sheets.trading_date,
            "screens": self.screens.trading_date,
            "shortlist": self.shortlist.trading_date,
        }
        wrong = {k: v.isoformat() for k, v in dates.items() if v != session}
        if wrong:
            raise ValueError(f"commons builds are not {session.isoformat()}'s: {wrong}")
        if self.screens.build_digest != self.sheets.build_digest:
            raise ValueError("the screens were built on a different sheets build")
        if self.shortlist.build_digest != self.sheets.build_digest:
            raise ValueError("the shortlist was built on a different sheets build")


class SessionStatus(StrEnum):
    DECIDED = "DECIDED"
    NO_ACTION = "NO_ACTION"
    MANAGER_ERROR = "MANAGER_ERROR"


@dataclass(frozen=True, slots=True)
class ManagerSessionResult:
    """What one manager decided on one session, and what it cost in calls.

    ``accepted`` is what M17.7 may stage; it is empty unless ``status`` is ``DECIDED``.
    """

    book_id: str
    session: date
    status: SessionStatus
    calls: int
    repairs: int
    accepted: tuple[ContractVerdict, ...] = ()
    refused: tuple[ContractVerdict, ...] = ()
    bundles: tuple[ResearchBundle, ...] = ()
    reason: str | None = None


def manager_horizons(band: HorizonBand) -> tuple[int, ...]:
    """The base-rate horizons inside the manager's band (all of them if none falls inside)."""
    inside = tuple(h for h in HORIZONS if band.min_sessions <= h <= band.max_sessions)
    return inside or tuple(HORIZONS)


class _ManagerError(Exception):
    def __init__(self, stage: str, message: str, evidence: EvidenceBundle | None) -> None:
        super().__init__(message)
        self.stage = stage
        self.evidence = evidence


def _canonical(document: Any) -> str:
    return json.dumps(document, sort_keys=True, separators=(",", ":"), ensure_ascii=True)


def _fetch_request(query: QueryItem, session: date) -> FetchRequest:
    if query.kind is QueryKind.URL:
        return FetchRequest.url(query.target, session)
    return FetchRequest.query(query.target, session)


class _Session:
    """The state of one `run_manager` call. Not reusable."""

    def __init__(
        self,
        mandate: ManagerMandate,
        session: date,
        commons: ManagerCommons,
        book: ManagerBook,
        llm: LLM,
        fetcher: Fetcher,
        *,
        journal: BookJournal,
        clock: Clock,
        pricer: TokenPricer,
        template: PromptTemplate,
    ) -> None:
        self.mandate = mandate
        self.session = session
        self.commons = commons
        self.book = book
        self.llm = llm
        self.fetcher = fetcher
        self.journal = journal
        self.clock = clock
        self.pricer = pricer
        self.template = template
        self.limits = mandate.rounds
        self.rows: dict[str, UniverseRow] = {r.isin: r for r in commons.sheets.universe}
        self.closes = {i: r.close for i, r in self.rows.items()}
        regime = commons.screens.regime.state
        self.regime = None if regime is None else regime.value
        self.horizons = manager_horizons(mandate.horizon)
        self.cells = shown_cells(
            commons.base_rates.cells, regime=self.regime, horizons=self.horizons
        )
        self.hurdles = tier_hurdles(
            commons.sheets.universe,
            session=session,
            notional=book.nav / Decimal(mandate.max_positions),
            cost_model=commons.cost_model,
            slippage=commons.slippage,
        )
        self.research_tool = research_tool(self.limits)
        self.decision_tool = decision_tool()
        self.calls = 0
        self.repairs = 0
        self.bundles: list[ResearchBundle] = []
        self.dossiers: dict[str, Dossier] = {}
        self.digests: dict[str, FilingDigest] = {}
        self.snapshots: dict[str, Snapshot] = {}
        self.queried: set[str] = set()
        self.researched: list[str] = []
        self._static = self._static_values()

    # -- the journal ------------------------------------------------------------------------------

    def _write(
        self,
        *,
        decision: Decision,
        payload: Mapping[str, str],
        rationale: str | None = None,
        isin: str | None = None,
        evidence: EvidenceBundle | None = None,
        evidence_ref: str | None = None,
        model: str | None = None,
        tokens: TokenSpend | None = None,
    ) -> None:
        ref = evidence.ref().ref if evidence is not None else evidence_ref
        entry = JournalEntry(
            ts=self.clock.now(),
            trading_date=self.session,
            case_id=self.mandate.id,
            actor=Actor.T2,
            decision=decision,
            isin=isin,
            sleeve=_SLEEVE if decision in (Decision.BUY, Decision.SELL) else None,
            evidence_snapshot_ref=ref,
            rationale=rationale,
            model=model,
            tokens=tokens,
            payload={**payload, "book": self.mandate.id, "mode": PAPER_MODE},
        )
        self.journal.append(entry, evidence=evidence)

    def _evidence(self, prompt: str | None) -> EvidenceBundle:
        """Every input digest shown so far, and the exact prompt when a model call is the reader."""
        commons = self.commons
        items = [
            EvidenceItem(
                kind=EvidenceKind.STATUS,
                source="commons",
                label=label,
                as_of=self.session,
                text=digest,
            )
            for label, digest in (
                ("sheets_build", commons.sheets.build_digest),
                ("screens", commons.screens.screens_digest),
                ("shortlist", commons.shortlist.shortlist_digest),
                ("base_rates", commons.base_rates.digest),
            )
        ]
        items += [
            EvidenceItem(
                kind=EvidenceKind.POSITION,
                source="book",
                label="weight_pct",
                isin=h.isin,
                as_of=self.session,
                value=h.weight_pct,
            )
            for h in self.book.holdings
        ]
        items += [
            EvidenceItem(
                kind=EvidenceKind.PRICE,
                source="commons_dossier",
                label="dossier",
                isin=d.isin,
                as_of=d.trading_date,
                text=d.dossier_digest,
            )
            for d in self.dossiers.values()
        ]
        items += [
            EvidenceItem(
                kind=EvidenceKind.FILING,
                source="commons_digest",
                label=g.filing_id,
                isin=g.isin,
                as_of=g.knowable_date,
                text=g.input_digest,
            )
            for g in self.digests.values()
        ]
        items += [
            EvidenceItem(
                kind=EvidenceKind.NEWS,
                source="commons_fetch",
                label="snapshot",
                as_of=s.request.session,
                knowable_at=s.fetched_at,
                text=s.id,
                detail={"kind": s.request.kind.value, "target": s.request.target},
            )
            for s in self.snapshots.values()
        ]
        return EvidenceBundle(
            case_id=self.mandate.id,
            trading_date=self.session,
            actor=Actor.T2,
            rendered_prompt=prompt,
            items=tuple(items),
        )

    def _tokens(self, response: LLMResponse) -> tuple[TokenSpend | None, str | None]:
        try:
            priced = self.pricer.price(response, on=self.session, purpose=TOKEN_PURPOSE)
        except AccountingError as exc:
            _LOG.warning(
                "fm.call_unpriced",
                book=self.mandate.id,
                session=self.session.isoformat(),
                model=response.model,
                reason=str(exc),
            )
            return None, str(exc)
        return priced.token_spend, None

    # -- the prompt -------------------------------------------------------------------------------

    def _static_values(self) -> dict[str, str]:
        m = self.mandate
        commons = self.commons
        return {
            "manager_id": m.id,
            "opening_capital_inr": f"₹{m.opening_capital_inr:,}",
            "style": m.style.value,
            "horizon_min": str(m.horizon.min_sessions),
            "horizon_max": str(m.horizon.max_sessions),
            "max_positions": str(m.max_positions),
            "max_position_pct": str(m.max_position_pct),
            "max_sector_pct": str(m.max_sector_pct),
            "session_date": self.session.isoformat(),
            "max_rounds": str(self.limits.max_calls - 1),
            "market_sheet": render_market(commons.sheets.market, commons.screens.regime),
            "regime_state": self.regime or "UNKNOWN",
            "regime_definition": REGIME_DEFINITION,
            "holdings": render_holdings(self.book, self.closes),
            "forced_reviews": render_forced_reviews(self.book),
            "screens": render_screens(commons.screens, self.rows),
            "shortlist": render_shortlist(commons.shortlist, self.rows),
            "base_rate_table": render_base_rates(commons.base_rates, self.cells),
            "cost_hurdles": render_cost_hurdles(self.hurdles),
            "max_isins": str(self.limits.max_isins),
            "max_queries": str(self.limits.max_queries),
        }

    def _prompt(self, round_key: str) -> str:
        values = {
            **self._static,
            "round": str(self.calls),
            "research_bundles": render_bundles(self.bundles),
        }
        return self.template.render(
            style=self.mandate.style.value, round_key=round_key, values=values
        )

    # -- one call ---------------------------------------------------------------------------------

    def _complete(
        self, prompt: str, tool: ToolSpec, stage: str, *, repair: bool
    ) -> tuple[LLMResponse, str]:
        """One model call, journaled as ``FM_CALL`` with its bundle; the response and bundle ref."""
        if repair:
            self.repairs += 1
        else:
            self.calls += 1
            if self.calls > self.limits.max_calls:
                raise RuntimeError(
                    f"{self.mandate.id}: call {self.calls} exceeds the "
                    f"{self.limits.max_calls}-call budget; the round loop is wrong"
                )
        evidence = self._evidence(prompt)
        messages = [Message(Role.USER, prompt)]
        model = self.mandate.models.decision
        try:
            response = self.llm.complete(messages, model=model, tools=(tool,), system=SYSTEM_PROMPT)
        except LLMError as exc:
            raise _ManagerError(stage, f"the {stage} call failed: {exc}", evidence) from exc
        tokens, unpriced = self._tokens(response)
        usage = response.usage
        self._write(
            decision=Decision.HEARTBEAT,
            evidence=evidence,
            model=response.model,
            tokens=tokens,
            payload={
                "event": CALL_EVENT,
                "stage": stage,
                "call": str(self.calls),
                "repair": "true" if repair else "false",
                "tool": tool.name,
                "provider": response.provider,
                "prompt_digest": prompt_digest(
                    messages, model=model, tools=(tool,), system=SYSTEM_PROMPT
                ),
                "input_tokens": str(usage.input_tokens),
                "output_tokens": str(usage.output_tokens),
                "cache_write_tokens": str(usage.cache_write_tokens),
                "cache_read_tokens": str(usage.cache_read_tokens),
                "stop_reason": response.stop_reason.value,
                "priced": "false" if tokens is None else "true",
                **({"unpriced_reason": unpriced} if unpriced is not None else {}),
            },
        )
        _LOG.info(
            "fm.call",
            book=self.mandate.id,
            session=self.session.isoformat(),
            stage=stage,
            call=self.calls,
            repair=repair,
            tokens_in=usage.prompt_tokens,
            tokens_out=usage.output_tokens,
        )
        return response, evidence.ref().ref

    @staticmethod
    def _arguments(response: LLMResponse, tool: ToolSpec) -> Mapping[str, Any]:
        if response.truncated:
            raise MalformedOutputError("the answer hit the output ceiling and is incomplete")
        calls = [c for c in response.tool_calls if c.name == tool.name]
        if len(calls) != 1:
            raise MalformedOutputError(
                f"expected one {tool.name} structured output, got {len(calls)}"
            )
        return calls[0].arguments

    def _ask[T](
        self,
        round_key: str,
        tool: ToolSpec,
        stage: str,
        parse: Callable[[Mapping[str, Any]], T],
    ) -> tuple[T, str]:
        """Ask one round's question; repair once if the session has a retry left."""
        prompt = self._prompt(round_key)
        response, ref = self._complete(prompt, tool, stage, repair=False)
        try:
            return parse(self._arguments(response, tool)), ref
        except MalformedOutputError as exc:
            first = exc
        if self.repairs:
            raise _ManagerError(
                stage,
                f"malformed {stage} output and the session's one repair retry is spent: {first}",
                self._evidence(prompt),
            )
        repair_prompt = prompt + _REPAIR_SUFFIX.format(errors=str(first))
        response, ref = self._complete(repair_prompt, tool, stage, repair=True)
        try:
            return parse(self._arguments(response, tool)), ref
        except MalformedOutputError as exc:
            raise _ManagerError(
                stage,
                f"malformed {stage} output after the repair retry: {exc}",
                self._evidence(repair_prompt),
            ) from exc

    # -- research ---------------------------------------------------------------------------------

    def _research(self, round_key: str, stage: str) -> ResearchRequest:
        (request, truncation), _ = self._ask(
            round_key, self.research_tool, stage, lambda a: parse_research(a, self.limits)
        )
        if truncation.truncated:
            self._write(
                decision=Decision.HOLD,
                rationale=(
                    f"the {stage} request asked for {truncation.isins_asked} names and "
                    f"{truncation.queries_asked} queries; the mandate allows "
                    f"{self.limits.max_isins} and {self.limits.max_queries}, so the rest were "
                    "dropped"
                ),
                payload={
                    "event": TRUNCATION_EVENT,
                    "stage": stage,
                    "isins_asked": str(truncation.isins_asked),
                    "isins_dropped": ",".join(truncation.isins_dropped),
                    "queries_asked": str(truncation.queries_asked),
                    "queries_dropped": _canonical(list(truncation.queries_dropped)),
                    "max_isins": str(self.limits.max_isins),
                    "max_queries": str(self.limits.max_queries),
                },
            )
            _LOG.info(
                "fm.research_truncated",
                book=self.mandate.id,
                session=self.session.isoformat(),
                stage=stage,
                isins_dropped=len(truncation.isins_dropped),
                queries_dropped=len(truncation.queries_dropped),
            )
        return request

    def _fulfil(self, request: ResearchRequest, round_index: int) -> None:
        unfulfilled: list[Unfulfilled] = []
        wanted: list[str] = []
        if round_index == 0:
            wanted += [i for i in self.book.isins if i not in self.dossiers]
        wanted += [r.isin for r in request.requests if r.isin not in self.dossiers]
        wanted = list(dict.fromkeys(wanted))
        in_universe = [i for i in wanted if i in self.rows]
        for isin in wanted:
            if isin not in self.rows:
                unfulfilled.append(Unfulfilled(isin, "not in today's universe; no dossier"))
        new_dossiers = self.commons.dossiers(in_universe) if in_universe else ()
        for dossier in new_dossiers:
            self.dossiers[dossier.isin] = dossier
        held = set(self.book.isins)
        for item in request.requests:
            if (
                item.isin in self.dossiers
                and item.isin not in held
                and item.isin not in self.researched
            ):
                self.researched.append(item.isin)

        new_digests: list[FilingDigest] = []
        if self.commons.digests is not None and in_universe:
            try:
                on_demand = self.commons.digests(in_universe)
            except (CommonsRefusedError, LLMError) as exc:
                unfulfilled.append(Unfulfilled("filing digests", str(exc)))
            else:
                for digest in on_demand.digests:
                    if digest.filing_id not in self.digests:
                        self.digests[digest.filing_id] = digest
                        new_digests.append(digest)
                unfulfilled += [
                    Unfulfilled(f"filing digest {f.filing_id}", f.reason)
                    for f in on_demand.failures
                ]
                unfulfilled += [
                    Unfulfilled(f"filing digests: {g.source}", g.reason) for g in on_demand.gaps
                ]

        fetched: list[tuple[QueryItem, Snapshot]] = []
        hits = 0
        fetch_in = fetch_out = 0
        for query in request.queries:
            try:
                fetch = _fetch_request(query, self.session)
            except ValueError as exc:
                unfulfilled.append(Unfulfilled(f"{query.kind.value} {query.target!r}", str(exc)))
                continue
            if fetch.key in self.queried:
                continue
            self.queried.add(fetch.key)
            try:
                outcome = self.commons.snapshots.get_or_fetch(fetch, self.fetcher)
            except FetchError as exc:
                unfulfilled.append(Unfulfilled(f"{query.kind.value} {query.target!r}", str(exc)))
                continue
            hits += int(outcome.cache_hit)
            if outcome.usage is not None:
                fetch_in += outcome.usage.prompt_tokens
                fetch_out += outcome.usage.output_tokens
            self.snapshots[outcome.snapshot.id] = outcome.snapshot
            fetched.append((query, outcome.snapshot))

        bundle = ResearchBundle(
            round=round_index,
            requests=request.requests,
            queries=request.queries,
            dossiers=new_dossiers,
            digests=tuple(new_digests),
            snapshots=tuple(fetched),
            unfulfilled=tuple(unfulfilled),
        )
        self.bundles.append(bundle)
        evidence = self._evidence(None)
        self._write(
            decision=Decision.HEARTBEAT,
            evidence=evidence,
            payload={
                "event": RESEARCH_EVENT,
                "round": str(round_index),
                "request": _canonical(request.model_dump(mode="json")),
                "dossiers": ",".join(f"{d.isin}:{d.dossier_digest}" for d in new_dossiers),
                "digests": ",".join(g.filing_id for g in new_digests),
                "snapshots": ",".join(s.id for _, s in fetched),
                "cache_hits": str(hits),
                "fetch_tokens_in": str(fetch_in),
                "fetch_tokens_out": str(fetch_out),
                "unfulfilled": _canonical([[u.what, u.reason] for u in unfulfilled]),
            },
        )
        _LOG.info(
            "fm.research_fulfilled",
            book=self.mandate.id,
            session=self.session.isoformat(),
            round=round_index,
            dossiers=len(new_dossiers),
            digests=len(new_digests),
            snapshots=len(fetched),
            cache_hits=hits,
            unfulfilled=len(unfulfilled),
        )

    # -- decisions --------------------------------------------------------------------------------

    def _facts(self) -> dict[str, NameFacts]:
        commons = self.commons
        shortlisted = {e.isin for e in commons.shortlist.entries}
        excluded = commons.screens.exclusions.isins
        features = {f.isin: f for f in commons.screens.features}
        out: dict[str, NameFacts] = {}
        for isin in dict.fromkeys([*self.book.isins, *self.researched]):
            row = self.rows.get(isin)
            if row is None:
                continue
            dossier = self.dossiers.get(isin)
            atr = dossier.fields.get("atr14_pct") if dossier is not None else None
            if atr is None and isin in features:
                atr = features[isin].atr14_pct
            out[isin] = NameFacts(
                isin=isin,
                close=row.close,
                median_traded_value=row.median_traded_value,
                cap_tier=row.cap_tier or UNRANKED_TIER,
                screens=frozenset(commons.screens.screens_of(isin)),
                shortlisted=isin in shortlisted,
                excluded=isin in excluded,
                atr14_pct=atr if isinstance(atr, Decimal) else None,
            )
        return out

    def _context(self) -> DecisionContext:
        return DecisionContext(
            session=self.session,
            book=self.book,
            researched=frozenset(self.researched),
            facts=self._facts(),
            citations=CitationIndex(
                dossiers=tuple(self.dossiers.values()),
                screens=self.commons.screens,
                digest_ids=frozenset(self.digests),
                cell_ids=frozenset(self.cells),
                cost_ids=frozenset(h.field_id for h in self.hurdles),
                snapshot_ids=frozenset(self.snapshots),
            ),
            cells=self.cells,
            regime=self.regime,
            buys_blocked=self.commons.screens.exclusions.buys_blocked,
            max_position_pct=self.mandate.max_position_pct,
            cost_model=self.commons.cost_model,
            slippage=self.commons.slippage,
        )

    def _decision_payload(
        self, verdict: ContractVerdict, facts: NameFacts | None
    ) -> dict[str, str]:
        d = verdict.decision
        scored = ScoredDecision(
            book_id=self.mandate.id,
            decided_on=self.session,
            isin=d.isin,
            action=DecisionAction(d.action.value),
            horizon_sessions=d.horizon_sessions,
            p_beat_bench=d.p_beat_bench,
            target_weight=None if d.action is Action.HOLD else d.target_weight,
            expected_excess_pct=d.expected_excess_pct,
            edge_type=d.edge_type.value,
            base_rate_p=verdict.base_rate.p_beat if verdict.base_rate is not None else None,
            regime=self.regime,
            cap_tier=facts.cap_tier if facts is not None else None,
            in_shortlist=facts.shortlisted if facts is not None else None,
            stop_pct=d.stop_pct if d.action is Action.BUY else None,
        )
        payload = decision_payload(scored)
        if verdict.base_rate is not None:
            cell = verdict.base_rate
            payload["base_rate_cell"] = _canonical(
                {
                    "cell_id": cell.cell_id,
                    "n": cell.n,
                    "p": str(cell.p_beat),
                    "median_excess": None
                    if cell.median_excess is None
                    else str(cell.median_excess),
                    "iqr_excess": None if cell.iqr_excess is None else str(cell.iqr_excess),
                    "thin": cell.thin,
                }
            )
        payload["schema"] = SCHEMA_VERSION
        payload["record"] = _canonical(d.model_dump(mode="json"))
        payload["citations"] = ",".join(f"{k}:{i}" for k, i in verdict.citations)
        if d.what_changed is not None:
            payload["what_changed"] = d.what_changed.kind.value
        if d.new_stop_pct is not None:
            payload["new_stop_pct"] = str(d.new_stop_pct)
        if verdict.stop_price is not None:
            payload["stop_price"] = str(verdict.stop_price)
        if verdict.round_trip is not None:
            payload["round_trip_pct"] = str(verdict.round_trip.total_pct)
            payload["quantity"] = str(verdict.round_trip.quantity)
        return payload

    def _decide(self, answer: ManagerDecisions, evidence_ref: str) -> ManagerSessionResult:
        model = self.mandate.models.decision
        if answer.no_action:
            reason = answer.no_action_reason or "no action"
            self._write(
                decision=Decision.HEARTBEAT,
                evidence_ref=evidence_ref,
                rationale=reason,
                model=model,
                payload={"event": NO_ACTION_EVENT, "reason": reason},
            )
            return self._result(SessionStatus.NO_ACTION, reason=reason)
        ctx = self._context()
        accepted: list[ContractVerdict] = []
        refused: list[ContractVerdict] = []
        for decision in answer.decisions:
            verdict = validate_decision(decision, ctx)
            if verdict.accepted:
                accepted.append(verdict)
                self._journal_decision(verdict, ctx.facts.get(decision.isin), evidence_ref)
            else:
                refused.append(verdict)
                self._journal_refusal(verdict, evidence_ref)
        _LOG.info(
            "fm.decided",
            book=self.mandate.id,
            session=self.session.isoformat(),
            accepted=len(accepted),
            refused=len(refused),
        )
        return self._result(SessionStatus.DECIDED, accepted=accepted, refused=refused)

    def _journal_decision(
        self, verdict: ContractVerdict, facts: NameFacts | None, evidence_ref: str
    ) -> None:
        d = verdict.decision
        kind = {
            Action.BUY: Decision.BUY,
            Action.SELL: Decision.SELL,
            Action.TRIM: Decision.SELL,
        }.get(d.action, Decision.HOLD)
        self._write(
            decision=kind,
            isin=d.isin,
            rationale=d.rationale,
            evidence_ref=evidence_ref,
            model=self.mandate.models.decision,
            payload={**self._decision_payload(verdict, facts), "event": DECISION_EVENT},
        )

    def _journal_refusal(self, verdict: ContractVerdict, evidence_ref: str) -> None:
        d = verdict.decision
        self._write(
            decision=Decision.HOLD,
            isin=d.isin,
            rationale=f"{d.action.value} {d.isin} voided by the decision contract: "
            + "; ".join(verdict.reasons),
            evidence_ref=evidence_ref,
            model=self.mandate.models.decision,
            payload={
                "event": REFUSED_EVENT,
                "action": d.action.value,
                "reasons": _canonical(list(verdict.reasons)),
                "record": _canonical(d.model_dump(mode="json")),
                "schema": SCHEMA_VERSION,
            },
        )
        _LOG.info(
            "fm.decision_refused",
            book=self.mandate.id,
            session=self.session.isoformat(),
            isin=d.isin,
            action=d.action.value,
            reasons=len(verdict.reasons),
        )

    def _result(
        self,
        status: SessionStatus,
        *,
        accepted: Sequence[ContractVerdict] = (),
        refused: Sequence[ContractVerdict] = (),
        reason: str | None = None,
    ) -> ManagerSessionResult:
        return ManagerSessionResult(
            book_id=self.mandate.id,
            session=self.session,
            status=status,
            calls=self.calls,
            repairs=self.repairs,
            accepted=tuple(accepted),
            refused=tuple(refused),
            bundles=tuple(self.bundles),
            reason=reason,
        )

    # -- the session ------------------------------------------------------------------------------

    def run(self) -> ManagerSessionResult:
        try:
            request = self._research(ROUND_ZERO, "round0")
            self._fulfil(request, 0)
            for index in range(1, self.limits.max_research_rounds + 1):
                if request.empty:
                    break
                request = self._research(ROUND_RESEARCH, f"research{index}")
                if request.empty:
                    break
                self._fulfil(request, index)
            answer, final_ref = self._ask(
                ROUND_FINAL,
                self.decision_tool,
                "final",
                lambda a: parse_decisions(a, holdings=self.book.isins, researched=self.researched),
            )
        except _ManagerError as exc:
            self._write(
                decision=Decision.ESCALATE,
                rationale=f"{exc}; nothing is staged this session",
                evidence=exc.evidence,
                model=self.mandate.models.decision,
                payload={
                    "event": MANAGER_ERROR_EVENT,
                    "stage": exc.stage,
                    "calls": str(self.calls),
                    "repairs": str(self.repairs),
                },
            )
            _LOG.warning(
                "fm.manager_error",
                book=self.mandate.id,
                session=self.session.isoformat(),
                stage=exc.stage,
                calls=self.calls,
                repairs=self.repairs,
            )
            return self._result(SessionStatus.MANAGER_ERROR, reason=str(exc))
        return self._decide(answer, final_ref)


def run_manager(
    mandate: ManagerMandate,
    session: date,
    commons: ManagerCommons,
    book: ManagerBook,
    llm: LLM,
    fetcher: Fetcher,
    *,
    journal: BookJournal,
    clock: Clock,
    pricer: TokenPricer | None = None,
    template: PromptTemplate | None = None,
) -> ManagerSessionResult:
    """One manager's whole session: round 0, at most two research rounds, the final call.

    What it does: verifies the Commons builds are ``session``'s and reproduce their digests,
    renders the manager's prompt for each round, fulfils its research from the Commons (cache
    first), validates its decisions against the contract, and journals every call, bundle,
    truncation, decision, refusal, ``NO_ACTION`` or ``MANAGER_ERROR`` (module docstring).
    What it assumes: ``book`` is this manager's own book (its ``book_id`` is ``mandate.id``,
    checked), ``clock`` is frozen on the session's evening, and ``journal`` stores evidence.
    What it never does: stage an order, retry more than once, exceed ``rounds.max_calls`` calls
    (repair aside), or render anything about another manager.
    """
    if book.book_id != mandate.id:
        raise ValueError(
            f"{mandate.id} was handed the book of {book.book_id!r}; a manager sees only its own"
        )
    commons.verify(session)
    run = _Session(
        mandate,
        session,
        commons,
        book,
        llm,
        fetcher,
        journal=journal,
        clock=clock,
        pricer=pricer or TokenPricer(load_price_card()),
        template=template or PromptTemplate.load(),
    )
    return run.run()
