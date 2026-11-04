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
other manager's id, book or decisions (pre-registration §1): none of them is an input — nor the
manager's own mirror book (Amendment 2 b), which the manager never sees.

**Desk order** (Amendment 2 c). The screens and the composite shortlist are listed in a shuffle
fixed by (manager id, session) — `DeskOrder` — so four managers reading the same Commons do not
anchor on the same top rows. Every name is still listed, with its rank, and the manager's own
starting screens come first. The shuffle is a sort on ``sha256(domain | manager | session |
ISIN)``: deterministic across processes and hash seeds, different for every manager and session,
and never a draw from a random state.
"""

from __future__ import annotations

import hashlib
import re
from collections.abc import Callable, Collection, Mapping, Sequence
from dataclasses import dataclass
from datetime import date
from decimal import Decimal
from enum import StrEnum
from pathlib import Path
from typing import Final

from analyst.commons import (
    BaseRateCell,
    BaseRateTable,
    CommonsScreens,
    Dossier,
    DossierItem,
    EventFact,
    FilingDigest,
    MarketSheet,
    RegimeReading,
    Screen,
    Shortlist,
    Snapshot,
    UniverseRow,
)
from analyst.commons.digests import FilingKind
from analyst.commons.dossier import (
    DOSSIER_FIELDS,
    MARKET_FIELDS,
    OUTSIDE_UNIVERSE_SOURCE,
    announcement_field,
)
from analyst.commons.screens import EventKind
from analyst.fundmanager.bundle import (
    UNRANKED_TIER,
    CostHurdle,
    Holding,
    ManagerBook,
    ResearchBundle,
)
from analyst.websearch import UNTRUSTED_CONTENT_RULE, wrap_untrusted

__all__ = [
    "ALIAS_FIELDS",
    "DESK_ORDER_DOMAIN",
    "FO_FIELDS",
    "NO_RESEARCH",
    "PROMPT_PATH",
    "REGIME_DEFINITION",
    "ROUND_FINAL",
    "ROUND_RESEARCH",
    "ROUND_ZERO",
    "S5_RANKED_MAX",
    "SYSTEM_PROMPT",
    "TWIN_FIELDS",
    "DeskOrder",
    "DigestShow",
    "PromptTemplate",
    "TemplateError",
    "digest_show",
    "render_base_rates",
    "render_bundles",
    "render_cost_hurdles",
    "render_forced_reviews",
    "render_holdings",
    "render_market",
    "render_s5",
    "render_screens",
    "render_shortlist",
    "shown_digests",
    "shown_dossier_fields",
]

PROMPT_PATH: Final = Path(__file__).with_name("prompts") / "manager.md"

ROUND_ZERO: Final = "0"
ROUND_RESEARCH: Final = "RESEARCH"
ROUND_FINAL: Final = "FINAL"

#: The system prompt of every manager call. Fixed, and part of `schemas.schema_bytes`.
#: The system text of every manager call. It carries `UNTRUSTED_CONTENT_RULE` (Amendment 5 (d)):
#: a fetched page reaches the manager inside `<web_content>` (`_render_snapshot`), and the page is
#: data, never an instruction. It is a `mandate_hash` input (`schemas.schema_bytes`).
SYSTEM_PROMPT: Final = (
    "You are the fund manager the user message describes, in a forward paper test. The user "
    "message is your whole brief: your mandate, your own book and the evidence bundle. Answer "
    "only through the structured output you are given, and cite every number from the bundle. "
    + UNTRUSTED_CONTENT_RULE
)

#: What the research section shows before any research is fulfilled.
NO_RESEARCH: Final = "None yet: what you request this round is fulfilled here."

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


#: The hash domain of the desk shuffle; part of what a changed presentation would change.
DESK_ORDER_DOMAIN: Final = "m17-desk-order/v1"
#: Every screen section `render_screens` lists, in the Commons' own order.
_SECTIONS: Final[tuple[str, ...]] = (*(s.value for s in Screen), "S5")


@dataclass(frozen=True, slots=True)
class DeskOrder:
    """How one manager's desk lists the shared screens and shortlist on one session.

    ``first_screens`` (the manager's starting screens, in its playbook's order) are listed before
    the others; within every listing the names are in the (``manager_id``, ``session``) shuffle,
    each still printed with its rank. Nothing is dropped or added.
    """

    manager_id: str
    session: date
    first_screens: tuple[str, ...] = ()

    def key(self, isin: str) -> str:
        """The shuffle key of ``isin``: a sha256 over the domain, manager, session and ISIN."""
        material = f"{DESK_ORDER_DOMAIN}|{self.manager_id}|{self.session.isoformat()}|{isin}"
        return hashlib.sha256(material.encode("utf-8")).hexdigest()

    def shuffle[T](self, items: Sequence[T], isin: Callable[[T], str]) -> list[T]:
        """``items`` in this desk's order (stable for several items of one ISIN)."""
        return sorted(items, key=lambda item: self.key(isin(item)))

    def sections(self) -> tuple[str, ...]:
        """The screen sections in listing order: the starting screens first, then the rest."""
        first = tuple(s for s in self.first_screens if s in _SECTIONS)
        return (*first, *(s for s in _SECTIONS if s not in first))


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


def render_screens(
    screens: CommonsScreens,
    rows: Mapping[str, UniverseRow],
    order: DeskOrder | None = None,
    *,
    shortlist: Shortlist | None = None,
    holdings: Collection[str] = (),
) -> str:
    """Every screen, each name with its rank. With ``order``, the manager's starting screens come
    first and each listing is in its desk shuffle (Amendment 2 c); without, the Commons' order.

    S5 is listed in its compact form (`render_s5`, Amendment 6 (a)): ``shortlist`` and
    ``holdings`` only decide which S5 names come first, never which are listed."""
    lines = [f"### Screens ({screens.trading_date}, digest {screens.screens_digest[:12]})"]
    if order is not None:
        lines.append(
            "Each screen lists its names in an order shuffled for your desk; a name's rank on its "
            "screen is the number after 'rank'. Your starting screens come first."
        )
    if screens.exclusions.buys_blocked:
        lines.append(
            "**No new BUY is admitted this session**: the newest GSM/ESM list is missing or stale."
        )
    sections = _SECTIONS if order is None else order.sections()
    for section in sections:
        if section == "S5":
            lines += render_s5(
                screens, rows, order, shortlist=shortlist, holdings=frozenset(holdings)
            )
            continue
        entries = list(screens.ranked(Screen(section)))
        if order is not None:
            entries = order.shuffle(entries, lambda e: e.isin)
        lines += ["", f"{section}:"]
        if not entries:
            lines.append("- (none)")
        lines += [
            f"- rank {e.position}: {e.isin}{_row_note(rows.get(e.isin))} (score {_fmt(e.score)})"
            for e in entries
        ]
    return "\n".join(lines)


#: Amendment 6 (a): at most this many S5 names get a line of their own (the ones also on S1-S4,
#: the shortlist or the book, most memberships first); every other S5 name is listed by event.
S5_RANKED_MAX: Final = 40

_S5_LIST_TITLES: Final[dict[str, str]] = {
    "results_meeting": "Results board meetings (meeting date: names)",
    "bulk_deal": "Bulk deals (trade date: name side; B buy, S sell)",
    "block_deal": "Block deals (trade date: name side; B buy, S sell)",
}


def _s5_event(fact: EventFact) -> tuple[str, date, str]:
    """One S5 fact as (event type, its date, a side suffix): a results meeting is dated by its
    meeting, a deal by its trade date, an announcement by when it became knowable."""
    if fact.kind is EventKind.BOARD_MEETING:
        return "results_meeting", fact.meeting_date or fact.knowable_date, ""
    if fact.kind in (EventKind.BULK_DEAL, EventKind.BLOCK_DEAL):
        side = (fact.side or "?").strip().upper()[:1] or "?"
        return fact.kind.value.lower(), fact.knowable_date, side
    return ",".join(fact.categories) or "announcement", fact.knowable_date, ""


def _s5_label(kind: str) -> str:
    return {"results_meeting": "results meeting", "bulk_deal": "bulk deal"}.get(
        kind, kind.replace("_", " ")
    )


def _sides(sides: set[str]) -> str:
    """A deal's sides on one date (``" B/S"``), or nothing for an event without a side."""
    shown = sorted(s for s in sides if s)
    return f" {'/'.join(shown)}" if shown else ""


def render_s5(
    screens: CommonsScreens,
    rows: Mapping[str, UniverseRow],
    order: DeskOrder | None = None,
    *,
    shortlist: Shortlist | None = None,
    holdings: frozenset[str] = frozenset(),
) -> list[str]:
    """S5 in its compact, ranked and bounded form (Amendment 6 (a)); every S5 name is listed.

    Each fact keeps its event type and date (and a deal its side); the meeting's boilerplate
    purpose, a deal's client, quantity and price are left to the name's dossier, where research
    shows them in full. Names also on S1-S4, the shortlist or the book come first, one line each
    with that membership, most memberships first and then in the desk shuffle, at most
    `S5_RANKED_MAX` of them. Every other S5 name is listed by event type and date, in the desk
    shuffle. What it never does: drop an S5 name, or rank one by anything but its membership.
    """
    lines = [
        "",
        "S5 event watch (facts; every S5 name below, its filings and deals in full in "
        "its dossier):",
    ]
    facts = list(screens.s5)
    if not facts:
        lines.append("- (none)")
        return lines
    key = (lambda isin: order.key(isin)) if order is not None else (lambda isin: isin)
    events: dict[str, set[tuple[str, date, str]]] = {}
    for fact in facts:
        events.setdefault(fact.isin, set()).add(_s5_event(fact))
    listed = {e.isin for e in (shortlist.entries if shortlist is not None else ())}

    def memberships(isin: str) -> list[str]:
        out = [s.value for s in Screen if any(e.isin == isin for e in screens.ranked(s))]
        if isin in listed:
            out.append("shortlist")
        if isin in holdings:
            out.append("your book")
        return out

    member = {isin: memberships(isin) for isin in events}
    ranked = sorted((i for i in events if member[i]), key=lambda i: (-len(member[i]), key(i)))
    ranked = ranked[:S5_RANKED_MAX]
    if ranked:
        lines.append("Also on S1-S4, the shortlist or your book (most memberships first):")
    for isin in ranked:
        sides: dict[tuple[date, str], set[str]] = {}
        for kind, day, side in events[isin]:
            sides.setdefault((day, kind), set()).add(side)
        detail = "; ".join(
            f"{_s5_label(kind)} {day}" + _sides(sides[(day, kind)]) for day, kind in sorted(sides)
        )
        lines.append(
            f"- {isin}{_row_note(rows.get(isin))} · on {', '.join(member[isin])}: {detail}"
        )
    rest = [i for i in events if i not in set(ranked)]
    if not rest:
        return lines
    lines.append("Every other S5 name, by event:")
    by_kind: dict[str, dict[date, dict[str, set[str]]]] = {}
    for isin in rest:
        for kind, day, side in events[isin]:
            by_kind.setdefault(kind, {}).setdefault(day, {}).setdefault(isin, set()).add(side)
    kinds = [k for k in _S5_LIST_TITLES if k in by_kind]
    kinds += sorted(k for k in by_kind if k not in _S5_LIST_TITLES)
    for kind in kinds:
        title = _S5_LIST_TITLES.get(kind, f"{_s5_label(kind).capitalize()} (date known: names)")
        days = []
        for day in sorted(by_kind[kind]):
            names = sorted(by_kind[kind][day], key=key)
            shown = [isin + _sides(by_kind[kind][day][isin]) for isin in names]
            days.append(f"{day}: {', '.join(shown)}")
        lines.append(f"- {title}: " + "; ".join(days))
    return lines


def render_shortlist(
    shortlist: Shortlist,
    rows: Mapping[str, UniverseRow],
    order: DeskOrder | None = None,
) -> str:
    """The composite shortlist, each name with its rank; in the desk shuffle with ``order``."""
    lines = [
        f"### Composite shortlist ({shortlist.trading_date}, digest "
        f"{shortlist.shortlist_digest[:12]}; your control book's input"
        + ("; listed in your desk's shuffled order, rank shown" if order is not None else "")
        + ")"
    ]
    entries = list(shortlist.entries)
    if order is not None:
        entries = order.shuffle(entries, lambda e: e.isin)
    if not entries:
        lines.append("- (empty)")
    lines += [
        f"- rank {e.position}: {e.isin}{_row_note(rows.get(e.isin))} (composite "
        f"{_fmt(e.composite)})"
        for e in entries
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


#: Amendment 6 (c): dossier fields not rendered because another rendered field always carries the
#: same value (`dossier._ALIASES`). Each still resolves when cited (`Dossier.resolve`).
ALIAS_FIELDS: Final[dict[str, str]] = {
    "ret_1w": "return_1w",
    "ret_4w": "return_4w",
    "ret_13w": "return_13w",
    "ret_52w": "return_52w",
}
#: Amendment 6 (c): a field not rendered in a dossier where its value equals its twin's, compared
#: at the coarser of the two precisions — the two definitions agree there, so the line repeats
#: its twin. Where they differ both are shown. None of these was cited on 2-3 Nov, none is read
#: by a contract check or a decision rule, and each still resolves when cited.
TWIN_FIELDS: Final[dict[str, str]] = {
    "ret_5": "return_1w",
    "volatility_20": "vol_20",
    "filing_date": "results_filing_date",
    "results_day0": "results_day",
    "volume_ratio_day0_ex_matched": "volume_ratio_day0",
    "results_day_volume_ratio_ex_matched": "results_day_volume_ratio",
}
#: Amendment 6 (c): the F&O lines, not rendered when ``fo_listed`` is ``no`` — each is then
#: ``unknown`` by definition, which ``fo_listed`` already says.
FO_FIELDS: Final[tuple[str, ...]] = (
    "fo_session",
    "fo_oi_buildup",
    "fo_pcr_oi",
    "fo_rollover_pct",
    "fo_oi_change",
    "fo_spot_change",
)
#: The gap source a dossier of a holding outside the session's universe carries (Amendment 6 (e)).
OUTSIDE_UNIVERSE_GAP: Final = OUTSIDE_UNIVERSE_SOURCE


def _same_value(a: object, b: object) -> bool:
    """Whether two dossier values are the same figure: equal, or two decimals equal at the
    coarser of their precisions (a sheet return at 6 places against a feature at 8)."""
    if a is None or b is None:
        return False
    if isinstance(a, Decimal) and isinstance(b, Decimal):
        exp = max(int(a.as_tuple().exponent), int(b.as_tuple().exponent))
        quantum = Decimal(1).scaleb(exp)
        return a.quantize(quantum) == b.quantize(quantum)
    return type(a) is type(b) and a == b


def shown_dossier_fields(d: Dossier) -> tuple[str, ...]:
    """The dossier fields rendered for ``d``, in `DOSSIER_FIELDS` order (Amendment 6 (c)):
    every field but an alias, a twin equal to its rendered twin, and the F&O lines of a name
    that is not F&O-listed. What it never does: hide a field whose value differs from its twin."""
    out = []
    for name in DOSSIER_FIELDS:
        if name in ALIAS_FIELDS:
            continue
        twin = TWIN_FIELDS.get(name)
        if twin is not None and _same_value(d.fields.get(name), d.fields.get(twin)):
            continue
        if name in FO_FIELDS and d.fields.get("fo_listed") is False:
            continue
        out.append(name)
    return tuple(out)


def _outside_universe(d: Dossier) -> str | None:
    """The reason ``d`` is a holding outside the session's universe, or None."""
    for gap in d.gaps:
        if gap.source == OUTSIDE_UNIVERSE_GAP:
            return gap.reason
    return None


def _render_dossier(d: Dossier, digested: Mapping[str, str] | None = None) -> list[str]:
    """One dossier. ``digested`` maps an announcement's field id to the digest shown for it in the
    same bundle: that announcement's line becomes a pointer to the digest (Amendment 6 (b)), so
    its text is shown once and its id still resolves."""
    outside = _outside_universe(d)
    lines = [
        "",
        f"#### Dossier {d.isin} ({d.trading_date}, digest {d.dossier_digest[:12]}; returns and "
        "ratios are fractions)",
    ]
    if outside is not None:
        lines.append(
            f"HOLDING OUTSIDE TODAY'S UNIVERSE ({outside}). Only the fields the lake has for it "
            "are listed; every other field is unknown. It is your holding: decide HOLD, TRIM or "
            "SELL on this dossier and your book line."
        )
    names = shown_dossier_fields(d)
    if outside is not None:
        names = tuple(n for n in names if d.fields.get(n) is not None)
    lines += [f"- [F:{d.field_id(name)}] {_fmt(d.fields.get(name))}" for name in names]
    pointers = digested or {}
    for title, items in (
        ("Announcements", d.announcements),
        ("Corporate actions due", d.corporate_actions),
        ("Bulk and block deals", d.deals),
    ):
        if items:
            lines.append(f"{title}:")
            lines += [_render_item(i, pointers.get(i.field_id)) for i in items]
    other_gaps = [g for g in d.gaps if g.source != OUTSIDE_UNIVERSE_GAP]
    if other_gaps:
        lines.append(
            "Sources unavailable: " + "; ".join(f"{g.source} ({g.reason})" for g in other_gaps)
        )
    return lines


def _render_item(i: DossierItem, digest_id: str | None) -> str:
    if digest_id is not None:
        return f"- [F:{i.field_id}] knowable {i.knowable_date}: digested below as [F:{digest_id}]"
    return (
        f"- [F:{i.field_id}] knowable {i.knowable_date}: {i.text}"
        + (f" [{', '.join(i.categories)}]" if i.categories else "")
        + (f" (due {i.due_date})" if i.due_date else "")
    )


# ── Amendment 6 (b): which filing digests are shown, and how ──────────────────────────────────


#: A digest body that says the announcement text carries nothing beyond its subject.
_NO_DETAIL: Final = re.compile(
    r"no (?:further|other|more) detail|gives no\b|gives only|states no\b|does not state|"
    r"do not state|names only|only names|not stated|not included in the text|"
    r"(?:was|is|were) not included|no further information",
    re.IGNORECASE,
)
_URL: Final = re.compile(r"https?://\S+|\b\S+\.pdf\b", re.IGNORECASE)
_TIMESTAMP: Final = re.compile(r"\d{4}-\d{2}-\d{2}[T ]\d{2}:\d{2}(?::\d{2})?(?:[+-]\d{2}:\d{2})?")
_NUMBER: Final = re.compile(r"\d[\d,]*(?:\.\d+)?")
_WORD: Final = re.compile(r"[a-z][a-z0-9&']{2,}")
#: Words a digest headline adds to any announcement without adding a fact.
_FILLER: Final = frozenset(
    [
        "the",
        "and",
        "for",
        "with",
        "from",
        "into",
        "about",
        "has",
        "have",
        "its",
        "this",
        "that",
        "was",
        "were",
        "are",
        "been",
        "being",
        "under",
        "limited",
        "ltd",
        "company",
        "exchange",
        "informed",
        "informs",
        "inform",
        "intimates",
        "intimated",
        "intimation",
        "submits",
        "submitted",
        "submission",
        "files",
        "filed",
        "filing",
        "announcement",
        "announces",
        "announced",
        "nse",
        "regarding",
        "regulation",
        "regulations",
        "sebi",
        "lodr",
        "per",
        "details",
        "detail",
        "attached",
        "attachment",
        "pdf",
        "copy",
        "text",
        "dated",
        "date",
    ]
)


class DigestShow(StrEnum):
    """How a filing digest is rendered (Amendment 6 (b))."""

    FULL = "FULL"
    HEADLINE = "HEADLINE"
    DROPPED = "DROPPED"


def _announcement_item(g: FilingDigest, dossiers: Mapping[str, Dossier]) -> DossierItem | None:
    """The dossier line of the announcement ``g`` digests, when that dossier is shown."""
    dossier = dossiers.get(g.isin)
    if dossier is None or ":" not in g.filing_id:
        return None
    field_id = f"{g.isin}.{announcement_field(g.filing_id.split(':', 1)[1])}"
    return next((i for i in dossier.announcements if i.field_id == field_id), None)


def _restates(g: FilingDigest, line: str) -> bool:
    """Whether ``g`` adds nothing to its dossier line: no number the line lacks (the
    dissemination time and links aside) and no headline word the line lacks (filler aside)."""
    said = _URL.sub(" ", _TIMESTAMP.sub(" ", f"{g.body.headline} {g.body.disclosed}"))
    known = line.casefold()
    known_numbers = {n.replace(",", "") for n in _NUMBER.findall(known)}
    if any(n.replace(",", "") not in known_numbers for n in _NUMBER.findall(said)):
        return False
    words = set(_WORD.findall(g.body.headline.casefold())) - _FILLER
    return all(w in known for w in words)


def digest_show(g: FilingDigest, dossiers: Mapping[str, Dossier]) -> DigestShow:
    """How ``g`` is shown beside ``dossiers`` (the dossiers of the same bundle, by ISIN).

    Amendment 6 (b). A results digest, and an announcement digest with any figure, is shown in
    full: it adds a figure. An announcement digest with no figure is dropped when its own
    announcement is a line of a dossier shown with it and the digest either says the text gives
    no further detail or adds no number and no headline word to that line; with no such dossier
    line, a digest that says the text gives no further detail is cut to its headline (still
    citable). Every other digest is shown in full. What it never does: drop a digest with a
    figure, a results digest, or the only line that names an announcement.
    """
    if g.kind is not FilingKind.ANNOUNCEMENT or g.body.figures:
        return DigestShow.FULL
    no_detail = _NO_DETAIL.search(g.body.disclosed) is not None
    item = _announcement_item(g, dossiers)
    if item is not None and (no_detail or _restates(g, item.text)):
        return DigestShow.DROPPED
    return DigestShow.HEADLINE if no_detail else DigestShow.FULL


def shown_digests(bundle: ResearchBundle) -> tuple[tuple[FilingDigest, DigestShow], ...]:
    """The bundle's digests that are rendered, each with how (`digest_show`), in bundle order."""
    dossiers = {d.isin: d for d in bundle.dossiers}
    out = [(g, digest_show(g, dossiers)) for g in bundle.digests]
    return tuple((g, how) for g, how in out if how is not DigestShow.DROPPED)


def _render_digest(g: FilingDigest, how: DigestShow = DigestShow.FULL) -> list[str]:
    head = (
        f"- [F:{g.filing_id}] {g.isin} {g.kind.value} knowable {g.knowable_date}: "
        f"{g.body.headline}" + (" (input truncated)" if g.input_truncated else "")
    )
    if how is DigestShow.HEADLINE:
        return [head + " (the text gives no further detail)"]
    lines = [head, f"  {g.body.disclosed}"]
    lines += [
        f"  · {f.label}: {f.value}"
        + (f" {f.unit}" if f.unit else "")
        + (f" ({f.period})" if f.period else "")
        for f in g.body.figures
    ]
    return lines


def _render_snapshot(snapshot: Snapshot) -> str:
    """A snapshot's pages inside `<web_content>`, every copy of the tag in the text defused, so a
    page can neither close the wrapper early nor pass for the brief. Never cut: the Commons store
    has already bounded it."""
    text = snapshot.text or "(no pages)"
    wrapped, _ = wrap_untrusted(
        text,
        attributes={"snapshot": f"S:{snapshot.id}", "fetcher": snapshot.fetcher},
        max_chars=len(text),
    )
    return wrapped


def render_bundles(bundles: Sequence[ResearchBundle]) -> str:
    """Every fulfilled bundle so far, oldest first, with the requests that produced it.

    A bundle's rendering depends only on that bundle, so bundle k's text is the same in every
    later round (the prompt cache reads it)."""
    if not bundles:
        return NO_RESEARCH
    lines: list[str] = []
    for b in bundles:
        lines += ["", f"### Research fulfilled after round {b.round}"]
        if b.requests or b.queries:
            lines.append("Your requests:")
            lines += [
                f"- {r.isin}: claim — {r.claim}; kill test — {r.kill_test}" for r in b.requests
            ]
            lines += [f"- {q.kind.value} {q.target!r} ({q.purpose})" for q in b.queries]
        digests = shown_digests(b)
        dossiers = {d.isin: d for d in b.dossiers}
        digested = {
            item.field_id: g.filing_id
            for g, how in digests
            if how is DigestShow.FULL and (item := _announcement_item(g, dossiers)) is not None
        }
        for dossier in b.dossiers:
            lines += _render_dossier(dossier, digested)
        if digests:
            lines += ["", "#### Filing digests (factual, from the Commons)"]
            for digest, how in digests:
                lines += _render_digest(digest, how)
        for query, snapshot in b.snapshots:
            lines += [
                "",
                f"#### [S:{snapshot.id}] {query.kind.value} {snapshot.request.target!r}, fetched "
                f"{snapshot.fetched_at.isoformat()}, {len(snapshot.pages)} page(s)"
                + (" — truncated" if snapshot.truncated else ""),
                _render_snapshot(snapshot),
            ]
        if b.unfulfilled:
            lines += ["", "Not fulfilled:"]
            lines += [f"- {u.what}: {u.reason}" for u in b.unfulfilled]
    return "\n".join(lines).strip()
