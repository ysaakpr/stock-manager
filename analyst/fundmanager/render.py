"""A10 · M17.4 — rendering `prompts/manager.md` for one manager, one round.

The template is the v1 prompt, frozen at S0 (pre-registration §8 Amendment 1 (g)); this module
renders it and never rewrites its guidance. Rendering is three steps, in this order:

1. strip every HTML comment (the template's notes to its renderer are not the model's to read);
2. keep exactly one ``[[STYLE:x]]`` block — the manager's own style — and exactly one
   ``[[ROUND:x]]`` block — ``0``, ``RESEARCH`` or ``FINAL`` — and drop every other block whole;
3. substitute each ``{{name}}`` in one pass, so text injected from the bundle (a web page that
   happens to contain ``{{`` or ``[[STYLE:…]]``) is never itself re-scanned or re-selected.

Every placeholder left in the selected text must have a value: a missing one raises
:class:`TemplateError` rather than reaching the model as a literal ``{{holdings}}``.

The section renderers below turn the Commons builds, the book view and the research bundles into
the text that fills those placeholders. Every number the manager may cite is printed beside its
citable id — ``[F:<field_id>]`` for a dossier field, ``market.<field>``, a filing digest, a
base-rate cell or a cost tier, and ``[S:<snapshot_id>]`` for a web snapshot — so a citation is a
copy, never a reconstruction. Nothing here renders a cost basis, a P&L, the book's value, or any
other manager's id, book or decisions (pre-registration §1): none of them is an input.
"""

from __future__ import annotations

import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from decimal import Decimal
from pathlib import Path
from typing import Final

from analyst.commons import (
    BaseRateCell,
    BaseRateTable,
    CommonsScreens,
    Dossier,
    FilingDigest,
    MarketSheet,
    RegimeReading,
    Screen,
    Shortlist,
    UniverseRow,
)
from analyst.commons.dossier import DOSSIER_FIELDS, MARKET_FIELDS
from analyst.fundmanager.bundle import (
    UNRANKED_TIER,
    CostHurdle,
    Holding,
    ManagerBook,
    ResearchBundle,
)

__all__ = [
    "PROMPT_PATH",
    "REGIME_DEFINITION",
    "ROUND_FINAL",
    "ROUND_RESEARCH",
    "ROUND_ZERO",
    "SYSTEM_PROMPT",
    "PromptTemplate",
    "TemplateError",
    "render_base_rates",
    "render_bundles",
    "render_cost_hurdles",
    "render_forced_reviews",
    "render_holdings",
    "render_market",
    "render_screens",
    "render_shortlist",
]

PROMPT_PATH: Final = Path(__file__).with_name("prompts") / "manager.md"

ROUND_ZERO: Final = "0"
ROUND_RESEARCH: Final = "RESEARCH"
ROUND_FINAL: Final = "FINAL"

#: The system prompt of every manager call. Fixed, and part of `schemas.schema_bytes`.
SYSTEM_PROMPT: Final = (
    "You are the fund manager the user message describes, in a forward paper test. The user "
    "message is your whole brief: your mandate, your own book and the evidence bundle. Answer "
    "only through the structured output you are given, and cite every number from the bundle."
)

#: Study §4's three-state rule, as `analyst.commons.regime` computes it.
REGIME_DEFINITION: Final = (
    "RISK-ON when NIFTY 500 is above a rising 200-session mean and at least half the universe is "
    "above its 50-session mean; RISK-OFF when NIFTY 500 is below its 200-session mean and its "
    "24-month return is negative, or its 126-session volatility is in its top 20% with the index "
    "below its 200-session mean; NEUTRAL otherwise"
)

_COMMENT: Final = re.compile(r"[ \t]*<!--.*?-->[ \t]*\n?", re.DOTALL)
_BLOCK: Final = re.compile(
    r"\[\[(?P<kind>STYLE|ROUND):(?P<key>[A-Z0-9_]+)\]\]\n?(?P<body>.*?)\[\[/(?P=kind)\]\]\n?",
    re.DOTALL,
)
_PLACEHOLDER: Final = re.compile(r"\{\{([a-z_][a-z0-9_]*)\}\}")
_MARKER: Final = re.compile(r"\[\[/?(?:STYLE|ROUND)\b")


class TemplateError(ValueError):
    """The template cannot be rendered as asked: an unknown block key or a missing value."""


@dataclass(frozen=True, slots=True)
class PromptTemplate:
    """The manager prompt's text, with its block structure checked at load."""

    text: str

    @classmethod
    def load(cls, path: Path = PROMPT_PATH) -> PromptTemplate:
        return cls(path.read_text(encoding="utf-8"))

    @property
    def raw_bytes(self) -> bytes:
        """The template's bytes: what `mandate_hash` takes as ``prompt_bytes``."""
        return self.text.encode("utf-8")

    def _stripped(self) -> str:
        return _COMMENT.sub("", self.text)

    def keys(self, kind: str) -> tuple[str, ...]:
        """The block keys of one kind (``STYLE`` or ``ROUND``), in template order."""
        return tuple(m.group("key") for m in _BLOCK.finditer(self._stripped()) if m["kind"] == kind)

    def render(self, *, style: str, round_key: str, values: Mapping[str, str]) -> str:
        """The prompt for one manager's ``style`` and one round, with every placeholder filled.

        Raises :class:`TemplateError` for a style or round the template has no block for, a
        placeholder with no value, or a stray block marker.
        """
        stripped = self._stripped()
        if style not in self.keys("STYLE"):
            raise TemplateError(f"the template has no [[STYLE:{style}]] block")
        if round_key not in self.keys("ROUND"):
            raise TemplateError(f"the template has no [[ROUND:{round_key}]] block")

        def keep(match: re.Match[str]) -> str:
            wanted = style if match["kind"] == "STYLE" else round_key
            return match["body"] if match["key"] == wanted else ""

        selected = _BLOCK.sub(keep, stripped)
        if _MARKER.search(selected):
            raise TemplateError("the template has an unbalanced [[STYLE]]/[[ROUND]] marker")
        missing = sorted({n for n in _PLACEHOLDER.findall(selected) if n not in values})
        if missing:
            raise TemplateError(f"no value for placeholders {missing}")
        rendered = _PLACEHOLDER.sub(lambda m: values[m.group(1)], selected)
        return re.sub(r"\n{3,}", "\n\n", rendered).strip() + "\n"


# ── value formatting ─────────────────────────────────────────────────────────────────────────────


def _fmt(value: object) -> str:
    if value is None:
        return "unknown"
    if isinstance(value, bool):
        return "yes" if value else "no"
    return str(value)


def _pct(value: Decimal) -> str:
    return f"{value.quantize(Decimal('0.01'))}%"


def _inr(value: Decimal) -> str:
    return f"₹{value.quantize(Decimal(1)):,}"


# ── the market ───────────────────────────────────────────────────────────────────────────────────


def render_market(market: MarketSheet, regime: RegimeReading) -> str:
    """The market sheet and the regime readings, each regime input under ``[F:market.<field>]``."""
    lines = ["### Market sheet", ""]
    for trend in market.index_trends:
        lines.append(
            f"- {trend.series_id} on {trend.session}: close {_fmt(trend.close)}, vs 50-session "
            f"mean {_fmt(trend.vs_mean_50)}, vs 200-session mean {_fmt(trend.vs_mean_200)}"
        )
    if market.breadth is not None:
        b = market.breadth
        lines.append(
            f"- Breadth: {b.above_mean_50} of {b.measured} measured names above their 50-session "
            f"mean (share {_fmt(b.above_mean_share)}); advancers {b.advancers}, decliners "
            f"{b.decliners}, unchanged {b.unchanged}"
        )
    if market.india_vix is not None:
        lines.append(f"- India VIX {_fmt(market.india_vix.value)} ({market.india_vix.observed})")
    if market.policy_rate is not None:
        lines.append(
            f"- Policy repo rate {_fmt(market.policy_rate.value)}% ({market.policy_rate.observed})"
        )
    for sector in market.sector_returns:
        lines.append(
            f"- Sector {sector.series_id}: 1m {_fmt(sector.return_1m)}, 3m "
            f"{_fmt(sector.return_3m)}, 6m {_fmt(sector.return_6m)}"
        )
    if market.delivery_anomalies is not None:
        d = market.delivery_anomalies
        lines.append(
            f"- Delivery anomalies: {d.count} of {d.measured} names at or above {_fmt(d.threshold)}"
            f"x their own median delivered quantity"
            + (
                "; largest: " + ", ".join(f"{a.isin} {_fmt(a.ratio)}x" for a in d.top)
                if d.top
                else ""
            )
        )
    lines += ["", "Regime inputs (returns and shares are fractions; 0.05 = 5%):"]
    for name in MARKET_FIELDS:
        lines.append(f"- [F:market.{name}] {_fmt(getattr(regime, name))}")
    return "\n".join(lines)


# ── the book ─────────────────────────────────────────────────────────────────────────────────────


def _stop_line(holding: Holding, close: Decimal | None) -> str:
    if holding.stop_price is None:
        return "no stop set"
    if close is None or close <= 0:
        return "stop set; today's close is unknown"
    distance = (close - holding.stop_price) / close * 100
    return f"stop {_pct(distance)} below today's close"


def render_holdings(book: ManagerBook, closes: Mapping[str, Decimal]) -> str:
    """Each holding without cost price or P&L (module docstring of `bundle`), and the cash share."""
    lines = [f"Cash: {_pct(book.cash_pct)} of the book."]
    if not book.holdings:
        lines.append("You hold no positions.")
        return "\n".join(lines)
    for h in book.holdings:
        lines += [
            "",
            f"- {h.isin} · {h.sector or 'sector unknown'} · weight {_pct(h.weight_pct)} · held "
            f"{h.sessions_held} sessions · {_stop_line(h, closes.get(h.isin))}",
            f"  Opening thesis: {h.opening_thesis}",
        ]
        if h.suspended_since is not None:
            lines.append(
                f"  SUSPENDED: held, not trading since {h.suspended_since.isoformat()}. There is "
                "no price today; the weight is at its last traded close. A sell is held and "
                "offered each session until it trades again; it cannot be bought."
            )
        if h.invalidation:
            lines.append("  Invalidation conditions:")
            lines += [f"  - {i.condition} — status: {i.status}" for i in h.invalidation]
        if h.evidence_since_entry:
            lines.append("  Evidence since entry:")
            lines += [f"  - {e}" for e in h.evidence_since_entry]
        if h.forced_review is not None:
            lines.append(f"  FORCED REVIEW: {h.forced_review}")
    return "\n".join(lines)


def render_forced_reviews(book: ManagerBook) -> str:
    forced = [h for h in book.holdings if h.forced_review is not None]
    if not forced:
        return "Forced reviews: none today."
    lines = ["Forced reviews (decide these first):"]
    lines += [f"- {h.isin}: {h.forced_review}" for h in forced]
    return "\n".join(lines)


# ── shared research ──────────────────────────────────────────────────────────────────────────────


def _row_note(row: UniverseRow | None) -> str:
    if row is None:
        return ""
    return f" · {row.sector or 'sector unknown'} · {row.cap_tier or UNRANKED_TIER}"


def render_screens(screens: CommonsScreens, rows: Mapping[str, UniverseRow]) -> str:
    lines = [f"### Screens ({screens.trading_date}, digest {screens.screens_digest[:12]})"]
    if screens.exclusions.buys_blocked:
        lines.append(
            "**No new BUY is admitted this session**: the newest GSM/ESM list is missing or stale."
        )
    for screen in Screen:
        entries = screens.ranked(screen)
        lines += ["", f"{screen.value}:"]
        if not entries:
            lines.append("- (none)")
        lines += [
            f"{e.position}. {e.isin}{_row_note(rows.get(e.isin))} (score {_fmt(e.score)})"
            for e in entries
        ]
    lines += ["", "S5 event watch (facts, unranked):"]
    if not screens.s5:
        lines.append("- (none)")
    for fact in screens.s5:
        detail = fact.subject or (
            f"{fact.side} {fact.quantity} @ {_fmt(fact.price)} by {fact.client_name}"
            if fact.client_name
            else ""
        )
        lines.append(
            f"- {fact.isin} {fact.kind.value} {','.join(fact.categories)} knowable "
            f"{fact.knowable_date}: {detail}"
            + (f" (meeting {fact.meeting_date})" if fact.meeting_date else "")
        )
    return "\n".join(lines)


def render_shortlist(shortlist: Shortlist, rows: Mapping[str, UniverseRow]) -> str:
    lines = [
        f"### Composite shortlist ({shortlist.trading_date}, digest "
        f"{shortlist.shortlist_digest[:12]}; your control book's input)"
    ]
    if not shortlist.entries:
        lines.append("- (empty)")
    lines += [
        f"{e.position}. {e.isin}{_row_note(rows.get(e.isin))} (composite {_fmt(e.composite)})"
        for e in shortlist.entries
    ]
    return "\n".join(lines)


def render_base_rates(table: BaseRateTable, cells: Mapping[str, BaseRateCell]) -> str:
    """The base-rate cells the manager may cite (`contract.shown_cells`), in table order, by id."""
    lines = [
        f"### Base rates (table {table.digest[:12]}, {table.first_session} to "
        f"{table.last_session}; P(beat) and excess are fractions, 0.03 = 3%; ALL pools a "
        f"dimension; thin = fewer than 30 observations)"
    ]
    for cell in table.cells:
        if cell.cell_id not in cells:
            continue
        lines.append(
            f"- [F:{cell.cell_id}] n={cell.n} P(beat)={_fmt(cell.p_beat)} median "
            f"excess={_fmt(cell.median_excess)} IQR={_fmt(cell.iqr_excess)}"
            + (" (thin)" if cell.thin else "")
        )
    return "\n".join(lines)


def render_cost_hurdles(hurdles: Sequence[CostHurdle]) -> str:
    lines = []
    for h in hurdles:
        if h.trip is None:
            lines.append(
                f"- [F:{h.field_id}] {h.tier}: a {_inr(h.notional)} order buys no whole share at "
                f"the tier's median price {_fmt(h.price)}"
            )
            continue
        lines.append(
            f"- [F:{h.field_id}] {h.tier} ({h.names} names): round trip {_pct(h.trip.total_pct)} "
            f"= charges {_pct(h.trip.charges_pct)} + slippage {_pct(h.trip.slippage_pct)} "
            f"({_fmt(h.trip.slippage_bps_per_side)} bp a side), for a {_inr(h.notional)} order "
            f"against a median traded value of {_inr(h.median_traded_value)}"
        )
    lines.append(
        "A BUY's own hurdle is computed the same way for that name's price, liquidity and your "
        "target weight."
    )
    return "\n".join(lines)


# ── the research bundles ─────────────────────────────────────────────────────────────────────────


def _render_dossier(d: Dossier) -> list[str]:
    lines = [
        "",
        f"#### Dossier {d.isin} ({d.trading_date}, digest {d.dossier_digest[:12]}; returns and "
        "ratios are fractions)",
    ]
    lines += [f"- [F:{d.field_id(name)}] {_fmt(d.fields.get(name))}" for name in DOSSIER_FIELDS]
    for title, items in (
        ("Announcements", d.announcements),
        ("Corporate actions due", d.corporate_actions),
        ("Bulk and block deals", d.deals),
    ):
        if items:
            lines.append(f"{title}:")
            lines += [
                f"- [F:{i.field_id}] knowable {i.knowable_date}: {i.text}"
                + (f" [{', '.join(i.categories)}]" if i.categories else "")
                + (f" (due {i.due_date})" if i.due_date else "")
                for i in items
            ]
    if d.gaps:
        lines.append(
            "Sources unavailable: " + "; ".join(f"{g.source} ({g.reason})" for g in d.gaps)
        )
    return lines


def _render_digest(g: FilingDigest) -> list[str]:
    lines = [
        f"- [F:{g.filing_id}] {g.isin} {g.kind.value} knowable {g.knowable_date}: "
        f"{g.body.headline}" + (" (input truncated)" if g.input_truncated else ""),
        f"  {g.body.disclosed}",
    ]
    lines += [
        f"  · {f.label}: {f.value}"
        + (f" {f.unit}" if f.unit else "")
        + (f" ({f.period})" if f.period else "")
        for f in g.body.figures
    ]
    return lines


def render_bundles(bundles: Sequence[ResearchBundle]) -> str:
    """Every fulfilled bundle so far, oldest first, with the requests that produced it."""
    if not bundles:
        return "No research was requested or fulfilled."
    lines: list[str] = []
    for b in bundles:
        lines += ["", f"### Research fulfilled after round {b.round}"]
        if b.requests or b.queries:
            lines.append("Your requests:")
            lines += [
                f"- {r.isin}: claim — {r.claim}; kill test — {r.kill_test}" for r in b.requests
            ]
            lines += [f"- {q.kind.value} {q.target!r} ({q.purpose})" for q in b.queries]
        for dossier in b.dossiers:
            lines += _render_dossier(dossier)
        if b.digests:
            lines += ["", "#### Filing digests (factual, from the Commons)"]
            for digest in b.digests:
                lines += _render_digest(digest)
        for query, snapshot in b.snapshots:
            lines += [
                "",
                f"#### [S:{snapshot.id}] {query.kind.value} {snapshot.request.target!r}, fetched "
                f"{snapshot.fetched_at.isoformat()}, {len(snapshot.pages)} page(s)"
                + (" — truncated" if snapshot.truncated else ""),
                snapshot.text or "(no pages)",
            ]
        if b.unfulfilled:
            lines += ["", "Not fulfilled:"]
            lines += [f"- {u.what}: {u.reason}" for u in b.unfulfilled]
    return "\n".join(lines).strip()
