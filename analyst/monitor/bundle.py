"""A5 · Evidence bundle builder: the T1/T2 input, assembled, bounded, and content-addressed.

§5.4's T1 tier reads a "evidence bundle: the flag, thesis + break conditions, recent
filings/news, price/flow context" and returns a verdict; T2 reads the same shape over a whole
case. This module assembles that bundle. It sits between T0 (`t0.py`, which raises the flag) and
T1 (`t1.py`, M6.4, which sends the bundle to a strong model), and it owns three things the tiers
either side of it must not have to re-do:

* **Content-addressing.** The output is an `analyst.journal.EvidenceBundle`, whose identity *is*
  the sha256 of its canonical bytes (M5.1). So a bundle is reproducible from its content hash and
  a journal entry's `evidence_snapshot_ref` reconstructs exactly what the model saw (§5.7) — the
  builder does not invent a second addressing scheme, it fills the one the journal already has.

* **A bounded, measured token budget.** §5.4's T1 uses a strong model, and decision #12 is
  "cost-blind for now" — quality-first — but the risk register is explicit that cost-blind must
  not become cost-*blowout*. An unbounded bundle is exactly that failure: a monitor that pastes
  every recent filing into one prompt spends without limit. So the builder measures the rendered
  prompt's token count and holds it under a configured budget, keeping the essential context (the
  flag, the thesis, its break conditions, the price/flow numbers) always and trimming the recent
  filings/news pool — most-recent first — until it fits. What is trimmed is *reported*, never
  silently lost, and the stored bundle contains exactly what remained, i.e. exactly what the model
  was sent. When the essentials alone will not fit, the builder refuses loudly rather than truncate
  a break condition mid-sentence: a budget too small for the essentials is a configuration bug, not
  something to paper over.

* **Point-in-time discipline.** A bundle for trading date D must contain nothing knowable after D
  (invariant #7). Every dated input — a news row's `ts`, an announcement's dissemination `ts`, a
  price fact's `knowable_at` — carries the instant it became knowable, and the builder refuses the
  whole request if any of them falls after D. It refuses rather than silently drops, because a
  future-dated item reaching a bundle for D is a query bug in the caller (a monitoring window that
  ran past the session), and a decision quietly made on a filtered-down view of a broken query is
  the leak the invariant exists to make impossible.

The token count here is an *estimate*, not the provider's measurement: no tokenizer is available
offline (tests never hit the network) and the authoritative count of a real call is X3's
post-call `Usage` written to the journal (`accounting.tokens`). This pre-flight estimate exists to
*bound* the prompt before it is sent — a budget you can only check after paying it is not a budget
— and uses the same fixed characters-per-token heuristic the `StubLLM` prices with, so the two
never drift. Money would be `Decimal` here (CLAUDE.md); the price facts carry `Decimal` values and
nothing is coerced through a float. Time is injected — the PIT cutoff comes from the trading date,
not a wall clock — and nothing here reads a database or the network.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from datetime import date, datetime
from decimal import Decimal
from typing import Final

from analyst.journal import (
    Actor,
    EvidenceBundle,
    EvidenceItem,
    EvidenceKind,
)
from analyst.monitor.t0 import T0Check, T0Flag
from analyst.thesis import Thesis
from dataplatform.clock import IST
from dataplatform.ingest.announcements import AnnouncementRow
from dataplatform.ingest.news import NewsRow
from dataplatform.logging import get_logger

__all__ = [
    "CHARS_PER_TOKEN",
    "DEFAULT_BUDGET",
    "BuiltBundle",
    "BundleBudget",
    "BundleBudgetError",
    "BundleBuilder",
    "BundleError",
    "BundlePitError",
    "BundleRequest",
    "PriceFact",
    "count_tokens",
]

_LOG = get_logger(__name__)

#: Characters per token in the pre-flight estimate. Deliberately the same constant `StubLLM`
#: prices with, so the number this module bounds a prompt by and the number the burn report later
#: shows for the same prompt are computed the same way. Roughly right for English prose and — the
#: property that actually matters — fixed, so the same bundle always estimates the same.
CHARS_PER_TOKEN: Final[int] = 4

#: Which evidence kind a flag's snapshot item takes, per T0 check — so the evidence pack can still
#: slice a T1 bundle by what triggered it ("which reviews were opened on a filing" vs "on a price
#: move", §5.7). Mirrors `t0._EVIDENCE_KIND`; kept here so the builder does not reach into T0's
#: internals for it.
_FLAG_KIND: Final[dict[T0Check, EvidenceKind]] = {
    T0Check.RAILS: EvidenceKind.RAIL,
    T0Check.DRAWDOWN: EvidenceKind.PRICE,
    T0Check.CORPORATE_ACTION: EvidenceKind.CORPORATE_ACTION,
    T0Check.ANNOUNCEMENT: EvidenceKind.FILING,
    T0Check.FLOW: EvidenceKind.PRICE,
    T0Check.DATA_QUALITY: EvidenceKind.STATUS,
}


class BundleError(Exception):
    """Base for every refusal to assemble a bundle, so a caller can catch the module."""


class BundlePitError(BundleError):
    """A supplied input is knowable after the bundle's trading date (invariant #7).

    Carries the offending item and the two instants, because "PIT leak" tells an operator nothing
    they can act on: the fix is the caller's monitoring window, and the message names it.
    """


class BundleBudgetError(BundleError):
    """The essential context alone will not fit the configured token budget.

    Not raised for a large *optional* pool — that is trimmed and reported. Raised only when the
    flag, the thesis, its break conditions and the price context together exceed the budget, which
    means the budget is misconfigured (too small to review anything at all), not that this
    particular day had too much news.
    """


def count_tokens(text: str) -> int:
    """A stable pre-flight token estimate for a string — `ceil(len / CHARS_PER_TOKEN)`.

    What it does: turns a character count into a token count with a fixed ratio, so a prompt can be
    bounded before it is sent.
    What it assumes: this is an estimate for *budgeting*, not the billed figure — the authoritative
    count is the provider's post-call `Usage` (X3). It is intentionally deterministic so the same
    bundle always estimates the same, which is what makes a budget check reproducible.
    What it never does: return non-zero for empty text — an absent section costs nothing — or reach
    a tokenizer library (there is none offline, and tests never hit the network).
    """
    if not text:
        return 0
    return math.ceil(len(text) / CHARS_PER_TOKEN)


@dataclass(frozen=True, slots=True)
class BundleBudget:
    """The ceiling on a bundle's estimated token count — the cost guard §5.4/#12 requires.

    One knob, `max_tokens`, because the budget is a policy about spend, not about content: what
    fills it is decided by the assembly, what caps it is decided here. A non-positive budget is
    refused — a bundle that may hold zero tokens can hold no evidence, which is not a review.
    """

    max_tokens: int

    def __post_init__(self) -> None:
        if isinstance(self.max_tokens, bool) or not isinstance(self.max_tokens, int):
            raise TypeError(f"max_tokens must be a whole number of tokens, got {self.max_tokens!r}")
        if self.max_tokens <= 0:
            raise ValueError(f"max_tokens must be positive, got {self.max_tokens}")


#: The default T1/T2 bundle budget. Generous enough for a flag, a thesis with its break conditions,
#: a page of price/flow numbers and a handful of recent filings, but a hard ceiling all the same —
#: a monitor that wants more context on one holding on one day has a query problem, not a budget
#: problem. Configurable per case/tier; this is only the value a caller who names none inherits.
DEFAULT_BUDGET: Final[BundleBudget] = BundleBudget(max_tokens=12_000)


@dataclass(frozen=True, slots=True)
class PriceFact:
    """One point-in-time price or flow number for the flagged holding — the price/flow context.

    Kept as an explicit PIT-stamped value rather than pulled live so the bundle records exactly the
    number the model saw and when it became knowable: `value` is a `Decimal` (never a float —
    CLAUDE.md), `as_of` is the session it is about, and `knowable_at` is when it could first be
    known, which is what the PIT guard checks against the trading date.
    """

    isin: str
    label: str
    value: Decimal
    as_of: date
    knowable_at: datetime
    source: str = "L1"

    def __post_init__(self) -> None:
        if not isinstance(self.value, Decimal):
            raise TypeError(
                f"price fact {self.label!r} must be a Decimal, never a float, got {self.value!r}"
            )
        if self.knowable_at.tzinfo is None or self.knowable_at.utcoffset() is None:
            raise ValueError(
                f"price fact {self.label!r} has a naive knowable_at; a PIT stamp must be tz-aware"
            )


@dataclass(frozen=True, slots=True)
class BundleRequest:
    """Everything the builder assembles one T1/T2 bundle from, for one holding on one session.

    What it does: gathers the trigger (the T0 flag), the ratified thesis and its break conditions,
    the price/flow context, and the pool of recent filings and news — the four §5.4 inputs.
    What it assumes: `thesis` is the version in force on `trading_date`, and every dated input is a
    real point-in-time fact (the parsers guarantee tz-aware timestamps). The filings/news pool may
    be larger than the budget allows — that is the builder's job to bound, not the caller's.
    What it never does: hold a bare dict as an interface, or a float where a price belongs.
    """

    case_id: str
    isin: str
    trading_date: date
    flag: T0Flag
    thesis: Thesis
    prices: tuple[PriceFact, ...] = ()
    announcements: tuple[AnnouncementRow, ...] = ()
    news: tuple[NewsRow, ...] = ()
    actor: Actor = Actor.T1

    def __post_init__(self) -> None:
        if self.actor not in (Actor.T1, Actor.T2):
            raise ValueError(
                "an evidence bundle is assembled for a review tier, T1 or T2, not "
                f"{self.actor.value}"
            )


@dataclass(frozen=True, slots=True)
class BuiltBundle:
    """A finished bundle: the addressable snapshot, its measured cost, and what was trimmed.

    `token_count` is the estimate over the rendered prompt — the text a T1/T2 review must send, so
    the stored snapshot is what the model saw. `dropped` counts the recent filings/news the budget
    could not fit; it is the number an operator watches to know a case is generating more evidence
    than one review can hold, and a persistently non-zero drop is a signal to raise the budget or
    tighten the monitoring window, not a silent loss.
    """

    bundle: EvidenceBundle
    budget: BundleBudget
    token_count: int
    included: int
    dropped: int

    @property
    def ref(self) -> str:
        """`sha256:<hex>` — the reference a journal entry's `evidence_snapshot_ref` carries."""
        return self.bundle.ref().ref

    @property
    def rendered_prompt(self) -> str:
        """The exact prompt text the review must send — what the token count was measured over."""
        assert self.bundle.rendered_prompt is not None  # set by the builder, always
        return self.bundle.rendered_prompt


class BundleBuilder:
    """Assembles §5.4's T1/T2 evidence bundle: bounded, PIT-clean, content-addressed.

    What it does: on `build()`, checks every dated input against the trading date (PIT), renders
    the essential context and as many recent filings/news as the budget allows into one prompt,
    measures its token count, and returns a content-addressed `EvidenceBundle` carrying both the
    structured items and that rendered prompt.
    What it assumes: the request's thesis is the one in force on the session, and the caller sends
    exactly `BuiltBundle.rendered_prompt` to the model — the stored snapshot claims to be what the
    model saw, and that claim holds only if the same text is sent.
    What it never does: exceed the budget (it trims, or refuses when the essentials alone overflow),
    include a fact knowable after the session (it refuses), or read a clock, a database or the
    network — the PIT cutoff is the trading date, supplied on the request.
    """

    __slots__ = ("_budget",)

    def __init__(self, *, budget: BundleBudget = DEFAULT_BUDGET) -> None:
        self._budget = budget

    def __repr__(self) -> str:
        return f"{type(self).__name__}(budget={self._budget.max_tokens})"

    @property
    def budget(self) -> BundleBudget:
        """The token ceiling this builder holds every bundle under."""
        return self._budget

    def build(self, request: BundleRequest) -> BuiltBundle:
        """Assemble the bundle for `request`, PIT-checked and held under the budget.

        Raises `BundlePitError` if any dated input is knowable after the trading date, and
        `BundleBudgetError` if the essential context alone will not fit.
        """
        self._reject_future_inputs(request)

        essential_items, essential_text = self._essentials(request)
        optionals = self._optionals(request)

        # The section header lives in the prefix so its tokens are budgeted before any snippet is
        # weighed — otherwise a bundle with one filing costs more than the accounting expected.
        prefix = essential_text + "\n\n## Recent filings & news"
        none_note = "\n(none within the token budget)"
        base_tokens = count_tokens(prefix)
        floor_tokens = count_tokens(prefix + none_note)
        if floor_tokens > self._budget.max_tokens:
            raise BundleBudgetError(
                f"the essential context for {request.isin} on "
                f"{request.trading_date.isoformat()} estimates at {base_tokens} tokens, over the "
                f"{self._budget.max_tokens}-token budget before a single filing is added — the "
                "budget is too small to review anything; raise it rather than truncating the "
                "flag or the break conditions"
            )

        remaining = self._budget.max_tokens - base_tokens
        included_items: list[EvidenceItem] = []
        included_snippets: list[str] = []
        dropped = 0
        for item, snippet in optionals:
            cost = count_tokens("\n" + snippet)
            if cost <= remaining:
                remaining -= cost
                included_items.append(item)
                included_snippets.append(snippet)
            else:
                dropped += 1

        if included_snippets:
            body = "".join("\n" + snippet for snippet in included_snippets)
        else:
            body = none_note
        rendered_prompt = prefix + body
        token_count = count_tokens(rendered_prompt)

        # The greedy loop budgets each snippet's estimate independently; the whole-string estimate
        # is subadditive over those parts (ceil is), so this holds by construction. Asserted, not
        # trusted: a bundle that claims to be within budget and is not is the exact failure this
        # module exists to prevent.
        if token_count > self._budget.max_tokens:  # pragma: no cover - guarded by construction
            raise BundleBudgetError(
                f"assembled bundle estimates at {token_count} tokens, over the "
                f"{self._budget.max_tokens}-token budget after trimming; this is a builder bug"
            )

        bundle = EvidenceBundle(
            case_id=request.case_id,
            trading_date=request.trading_date,
            actor=request.actor,
            rendered_prompt=rendered_prompt,
            items=tuple(essential_items) + tuple(included_items),
        )
        _LOG.info(
            "bundle.built",
            case_id=request.case_id,
            isin=request.isin,
            trading_date=request.trading_date.isoformat(),
            actor=request.actor.value,
            sha256=bundle.ref().sha256,
            tokens=token_count,
            budget=self._budget.max_tokens,
            included=len(included_items),
            dropped=dropped,
        )
        return BuiltBundle(
            bundle=bundle,
            budget=self._budget,
            token_count=token_count,
            included=len(included_items),
            dropped=dropped,
        )

    # ── point-in-time guard ────────────────────────────────────────────────────────────────────

    def _reject_future_inputs(self, request: BundleRequest) -> None:
        """Refuse the whole request if any dated input is knowable after the session (#7).

        The cutoff is the trading date itself: an EOD decision on D may see anything dated on or
        before D and nothing dated after. Refusing loudly — rather than dropping the future item —
        surfaces the caller's real bug, a monitoring window that ran past the session.
        """
        for news_row in request.news:
            self._assert_not_future(
                request.trading_date, news_row.ts, f"news {news_row.url!r} ({news_row.source})"
            )
        for announcement in request.announcements:
            self._assert_not_future(
                request.trading_date,
                announcement.ts,
                f"announcement {announcement.subject!r} ({announcement.source})",
            )
        for price in request.prices:
            self._assert_not_future(
                request.trading_date,
                price.knowable_at,
                f"price fact {price.label!r} on {price.isin}",
            )

    @staticmethod
    def _assert_not_future(trading_date: date, knowable_at: datetime, what: str) -> None:
        """Raise `BundlePitError` if `knowable_at` falls, in IST, after `trading_date`."""
        knowable_date = knowable_at.astimezone(IST).date()
        if knowable_date > trading_date:
            raise BundlePitError(
                f"{what} became knowable {knowable_at.isoformat()} (IST date "
                f"{knowable_date.isoformat()}), after the bundle's trading date "
                f"{trading_date.isoformat()}; a decision for {trading_date.isoformat()} cannot see "
                "it (invariant #7). Fix the monitoring window that supplied it — the builder will "
                "not silently drop a future fact"
            )

    # ── assembly ───────────────────────────────────────────────────────────────────────────────

    def _essentials(self, request: BundleRequest) -> tuple[list[EvidenceItem], str]:
        """The context that is always in the bundle: the flag, the thesis, the price/flow numbers.

        These are never trimmed for budget — a review without its trigger or its break conditions is
        not the review §5.4 describes — so they are rendered first and their tokens counted before
        any optional filing is considered.
        """
        items: list[EvidenceItem] = []
        lines: list[str] = [
            f"# Break-condition review — case {request.case_id} · {request.isin} · "
            f"{request.trading_date.isoformat()}",
        ]

        flag = request.flag
        flag_kind = _FLAG_KIND.get(flag.check, EvidenceKind.STATUS)
        items.append(
            EvidenceItem(
                kind=flag_kind,
                source=f"t0:{flag.check.value}",
                label=flag.check.value,
                isin=flag.isin,
                as_of=request.trading_date,
                text=flag.summary,
                detail=dict(flag.detail),
            )
        )
        lines.append("\n## Trigger")
        lines.append(f"[{flag.check.value}] {flag.summary}")
        for key, value in sorted(flag.detail.items()):
            lines.append(f"- {key}: {value}")

        thesis = request.thesis
        items.append(
            EvidenceItem(
                kind=EvidenceKind.THESIS,
                source="thesis",
                label="driver",
                isin=thesis.isin,
                text=thesis.driver,
                detail={
                    "version": str(thesis.version),
                    "status": thesis.status.value,
                    "theme_purity": str(thesis.theme_purity),
                    "content_hash": thesis.content_hash,
                },
            )
        )
        lines.append(f"\n## Thesis (v{thesis.version}, {thesis.status.value})")
        lines.append(f"Driver: {thesis.driver}")
        lines.append(f"Theme purity: {thesis.theme_purity}")
        lines.append("Expected evidence:")
        for evidence in thesis.expected_evidence:
            lines.append(f"- {evidence}")
        lines.append("Break conditions:")
        for condition in thesis.break_conditions:
            items.append(
                EvidenceItem(
                    kind=EvidenceKind.THESIS,
                    source="thesis",
                    label=f"break_condition:{condition.id}",
                    isin=thesis.isin,
                    text=condition.condition,
                    detail={
                        "id": condition.id,
                        "type": condition.type.value,
                        "evaluation_tier": condition.evaluation_tier.value,
                        "evaluation": condition.evaluation,
                    },
                )
            )
            lines.append(
                f"- {condition.id} ({condition.type.value}, {condition.evaluation_tier.value}): "
                f"{condition.condition} — watched: {condition.evaluation}"
            )

        if request.prices:
            lines.append("\n## Price / flow context")
            for price in request.prices:
                items.append(
                    EvidenceItem(
                        kind=EvidenceKind.PRICE,
                        source=price.source,
                        label=price.label,
                        isin=price.isin,
                        as_of=price.as_of,
                        knowable_at=price.knowable_at,
                        value=price.value,
                    )
                )
                lines.append(
                    f"- {price.isin} {price.label}: {price.value} (as of {price.as_of.isoformat()})"
                )

        return items, "\n".join(lines)

    def _optionals(self, request: BundleRequest) -> list[tuple[EvidenceItem, str]]:
        """The trimmable pool — recent filings and news — as (item, rendered-line) pairs.

        Ordered most-recent-first by knowable instant so the budget, when it runs out, spends on the
        newest evidence and drops the oldest — the ordering the plan implies by calling these
        "recent" filings/news. Ties are broken deterministically (source, then subject/url) so the
        same pool always trims to the same bundle — what keeps the content hash reproducible.
        """
        pool: list[tuple[datetime, str, EvidenceItem, str]] = []

        for announcement in request.announcements:
            item = EvidenceItem(
                kind=EvidenceKind.FILING,
                source=announcement.source,
                label="announcement",
                isin=announcement.isin,
                as_of=announcement.ts.astimezone(IST).date(),
                knowable_at=announcement.ts,
                text=announcement.subject,
                detail=_announcement_detail(announcement),
            )
            snippet = (
                f"- [FILING {announcement.ts.isoformat()}] {announcement.isin} "
                f"{announcement.subject}"
            )
            if announcement.body:
                snippet += f"\n  {announcement.body}"
            pool.append(
                (announcement.ts, f"{announcement.source}\x00{announcement.subject}", item, snippet)
            )

        for news_row in request.news:
            title = news_row.title or news_row.url
            item = EvidenceItem(
                kind=EvidenceKind.NEWS,
                source=news_row.source,
                label="news",
                as_of=news_row.ts.astimezone(IST).date(),
                knowable_at=news_row.ts,
                text=title,
                detail=_news_detail(news_row),
            )
            snippet = (
                f"- [NEWS {news_row.ts.isoformat()}] {news_row.source}: {title} ({news_row.url})"
            )
            pool.append((news_row.ts, f"{news_row.source}\x00{news_row.url}", item, snippet))

        pool.sort(key=lambda entry: (entry[0], entry[1]), reverse=True)
        return [(item, snippet) for _, _, item, snippet in pool]


def _announcement_detail(announcement: AnnouncementRow) -> dict[str, str]:
    """The strings-only detail for a filing evidence item (no float can enter a snapshot)."""
    detail = {
        "ts": announcement.ts.isoformat(),
        "subject": announcement.subject,
    }
    if announcement.category is not None:
        detail["category"] = announcement.category
    if announcement.body is not None:
        detail["body"] = announcement.body
    if announcement.attachment_ref is not None:
        detail["attachment_ref"] = announcement.attachment_ref
    if announcement.source_ref is not None:
        detail["source_ref"] = announcement.source_ref
    return detail


def _news_detail(news_row: NewsRow) -> dict[str, str]:
    """The strings-only detail for a news evidence item."""
    detail = {
        "ts": news_row.ts.isoformat(),
        "url": news_row.url,
    }
    if news_row.entities:
        detail["entities"] = ", ".join(news_row.entities)
    if news_row.tone is not None:
        detail["tone"] = str(news_row.tone)
    return detail
