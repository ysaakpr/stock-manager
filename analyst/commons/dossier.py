"""A10 · M17.9 — the per-name dossier: every number a manager may cite, under a stable field id.

Study §2 ("per-name features in every dossier") and §7: the platform computes every number, the
model cites it as ``[F:<field_id>]``, and code rejects an id that does not exist. This module
builds the dossier of a set of ISINs on one session and resolves a field id back to its value
(:func:`resolve_field`), which is what M17.4 uses to validate a decision's citations.

**Field ids.** ``<ISIN>.<field>`` for a name's scalar field, for example ``INE002A01018.ret_13w``;
``<ISIN>.ann_<ref>``, ``<ISIN>.ca_<n>`` and ``<ISIN>.deal_<n>`` for one listed announcement,
corporate action or deal; and ``market.<field>`` for the regime and breadth of the session
(``market.state``, ``market.breadth_above_sma50``...). :data:`DOSSIER_FIELDS` lists every scalar
field name. A field that could not be computed is present with the value ``None``: citing it is
citing "unknown", which a validator can refuse on its own terms.

**What a dossier holds** (study §2): momentum (1, 4, 13 and 52 weeks from the universe sheet; 6-1
and 12-1), distance from the 52-week high and the SMA200, ATR(14) and ATR % of price, 20-session
volatility, the 5-session and day-0 volume ratios, delivery % and the delivered-quantity z-score
against the name's own 60 sessions, the F&O block (open-interest build-up category, put-call
ratio on OI, rollover) for F&O names from L2 ``fo_aggregates``, P/E against the sector median,
TTM revenue and PAT growth, ROE, the net-margin trend, the SUE and EAR of the last results, the
debt-equity ratio (the only leverage field XBRL carries), announcements of the last 20 sessions
(subject lines), corporate actions due, the cap tier, the screens it is on and any exclusion.

**OI build-up** from the day's F&O aggregate against the previous session's: the underlier's
spot change and the total open-interest change give LONG_BUILDUP (price up, OI up),
SHORT_BUILDUP (down, up), SHORT_COVERING (up, down), LONG_UNWINDING (down, down), or NEUTRAL when
either is unchanged. The open interest is summed over every contract, futures and options, as the
L2 aggregate stores it.

Every read crosses the PIT guard as of the session. What it never does: read a wall clock, write,
compute a number the model is then trusted to re-derive, or import `analyst.fundmanager`.
"""

from __future__ import annotations

import hashlib
import re
from collections.abc import Callable, Iterable, Sequence
from datetime import date, timedelta
from decimal import Decimal
from enum import StrEnum
from typing import Any, Final

from pydantic import BaseModel, ConfigDict

from analyst.commons.events import KeywordTable, classify, load_event_keywords, subject_line
from analyst.commons.features import FEATURE_FIELDS, feature_values
from analyst.commons.inputs import ScreenSource
from analyst.commons.regime import RegimeReading
from analyst.commons.screens import CommonsScreens
from analyst.commons.sheets import CommonsSheets, Gap, SourceUnavailableError, canonical_bytes
from dataplatform.query import Dataset, PitContext
from dataplatform.query.fundamentals_metrics import compute_metrics

__all__ = [
    "ANNOUNCEMENT_SESSIONS",
    "DOSSIER_FIELDS",
    "DOSSIER_VERSION",
    "FIELD_ID_PATTERN",
    "MARKET_FIELDS",
    "Dossier",
    "DossierItem",
    "OiBuildup",
    "UnknownFieldError",
    "announcement_field",
    "build_dossiers",
    "oi_buildup",
    "resolve_field",
]

DOSSIER_VERSION: Final = "commons-dossier/1"
#: Announcements listed in a dossier: those knowable in the last 20 sessions.
ANNOUNCEMENT_SESSIONS: Final = 20
_DEAL_SESSIONS: Final = 20
_CA_LOOKBACK_DAYS: Final = 120
_FO_SESSIONS: Final = 5
_Q: Final = Decimal("0.00000001")
_MAX_SUBJECT: Final = 300

FieldValue = Decimal | bool | int | str | date | None

_SHEET_FIELDS: Final[tuple[str, ...]] = (
    "return_1w",
    "return_4w",
    "return_13w",
    "return_52w",
    "volatility_20",
    "median_traded_value",
    "cap_tier",
    "size_rank",
    "sector",
    "asm",
    "revenue_ttm_growth",
    "pat_ttm_growth",
    "roe",
    "pe_ttm",
    "sector_median_pe",
    "pe_vs_sector",
    "filing_date",
)
#: Short aliases the study's vocabulary uses, each the sheet field it names.
_ALIASES: Final[dict[str, str]] = {
    "ret_1w": "return_1w",
    "ret_4w": "return_4w",
    "ret_13w": "return_13w",
    "ret_52w": "return_52w",
}
_EXTRA_FIELDS: Final[tuple[str, ...]] = (
    "net_margin_ttm",
    "net_margin_trend",
    "debt_equity",
    "sue",
    "ear",
    "results_filing_date",
    "results_day0",
    "results_day0_volume_ratio",
    "fo_listed",
    "fo_session",
    "fo_oi_buildup",
    "fo_pcr_oi",
    "fo_rollover_pct",
    "fo_oi_change",
    "fo_spot_change",
    "screens",
    "excluded",
    "exclusion_reasons",
)
#: Every scalar field a dossier carries, in a fixed order.
DOSSIER_FIELDS: Final[tuple[str, ...]] = (
    *_SHEET_FIELDS,
    *_ALIASES,
    *FEATURE_FIELDS,
    *_EXTRA_FIELDS,
)
MARKET_FIELDS: Final[tuple[str, ...]] = tuple(
    n for n in RegimeReading.model_fields if n != "trading_date"
)
#: ``<ISIN>.<field>``, ``<ISIN>.ann_<ref>`` / ``ca_<n>`` / ``deal_<n>``, or ``market.<field>``.
FIELD_ID_PATTERN: Final = re.compile(
    r"^(?P<owner>[A-Z]{2}[A-Z0-9]{9}[0-9]|market)\.(?P<field>[a-z][a-z0-9_]*|ann_[A-Za-z0-9]+)$"
)


class UnknownFieldError(KeyError):
    """A cited field id that no dossier (or the market block) defines."""


class OiBuildup(StrEnum):
    LONG_BUILDUP = "LONG_BUILDUP"
    SHORT_BUILDUP = "SHORT_BUILDUP"
    SHORT_COVERING = "SHORT_COVERING"
    LONG_UNWINDING = "LONG_UNWINDING"
    NEUTRAL = "NEUTRAL"


def oi_buildup(spot_change: Decimal, oi_change: int) -> OiBuildup:
    """The standard four-way OI build-up classification of a day's price and OI changes."""
    if spot_change == 0 or oi_change == 0:
        return OiBuildup.NEUTRAL
    if oi_change > 0:
        return OiBuildup.LONG_BUILDUP if spot_change > 0 else OiBuildup.SHORT_BUILDUP
    return OiBuildup.SHORT_COVERING if spot_change > 0 else OiBuildup.LONG_UNWINDING


class _Model(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")


class DossierItem(_Model):
    """One listed fact in a dossier (an announcement, a corporate action or a deal), citable."""

    field_id: str
    knowable_date: date
    text: str
    categories: tuple[str, ...] = ()
    due_date: date | None = None


class Dossier(_Model):
    """One name's dossier on one session: scalar fields by name and the listed facts."""

    isin: str
    trading_date: date
    dossier_version: str
    screens_digest: str
    fields: dict[str, FieldValue]
    announcements: tuple[DossierItem, ...]
    corporate_actions: tuple[DossierItem, ...]
    deals: tuple[DossierItem, ...]
    gaps: tuple[Gap, ...]
    dossier_digest: str

    def field_id(self, name: str) -> str:
        return f"{self.isin}.{name}"

    def items(self) -> Iterable[DossierItem]:
        yield from self.announcements
        yield from self.corporate_actions
        yield from self.deals

    def resolve(self, field_id: str) -> FieldValue | DossierItem:
        """The value ``field_id`` names in this dossier; :class:`UnknownFieldError` otherwise."""
        prefix = f"{self.isin}."
        if not field_id.startswith(prefix):
            raise UnknownFieldError(field_id)
        name = field_id[len(prefix) :]
        if name in self.fields:
            return self.fields[name]
        for item in self.items():
            if item.field_id == field_id:
                return item
        raise UnknownFieldError(field_id)

    def body(self) -> dict[str, Any]:
        return self.model_dump(mode="json", exclude={"dossier_digest"})


def resolve_field(
    field_id: str, *, dossiers: Iterable[Dossier], screens: CommonsScreens | None = None
) -> FieldValue | DossierItem:
    """The value a cited ``[F:<field_id>]`` names, from the dossiers a manager was shown.

    ``market.<field>`` resolves against ``screens.regime``. Anything else must be a field or a
    listed fact of one of ``dossiers``. Raises :class:`UnknownFieldError` for an id that is
    malformed, names an ISIN with no dossier here, or names a field the dossier does not carry.
    """
    match = FIELD_ID_PATTERN.match(field_id)
    if match is None:
        raise UnknownFieldError(field_id)
    owner = match.group("owner")
    if owner == "market":
        name = match.group("field")
        if screens is None or name not in MARKET_FIELDS:
            raise UnknownFieldError(field_id)
        value: FieldValue = getattr(screens.regime, name)
        return value
    for dossier in dossiers:
        if dossier.isin == owner:
            return dossier.resolve(field_id)
    raise UnknownFieldError(field_id)


# ── the builder ──────────────────────────────────────────────────────────────────────────────────


def _admit[R](
    read: Callable[[], Dataset[R]], pit: PitContext, gaps: list[Gap], label: str
) -> tuple[R, ...] | None:
    try:
        return pit.admit(read())
    except SourceUnavailableError as exc:
        gaps.append(Gap(source=f"{label}:{exc.source}", reason=exc.reason))
        return None


def announcement_field(ref: str) -> str:
    """The field name of one announcement: ``ann_<ref>`` for the exchange's numeric id.

    A ref that is not plain alphanumeric (the store's ``ts|subject`` fallback for a row with no
    exchange id) is named by the first 16 hex digits of its sha256, so the id stays citable.
    """
    if ref.isascii() and ref.isalnum():
        return f"ann_{ref}"
    return f"ann_{hashlib.sha256(ref.encode('utf-8')).hexdigest()[:16]}"


def _categories(subject: str, body: str | None, keywords: KeywordTable) -> tuple[str, ...]:
    found = classify(subject, body, keywords)
    return found.integrity + found.event_watch


def _decimal(value: object) -> Decimal | None:
    return value.quantize(_Q) if isinstance(value, Decimal) else None


def build_dossiers(
    isins: Sequence[str],
    *,
    screens: CommonsScreens,
    sheets: CommonsSheets,
    source: ScreenSource,
) -> tuple[Dossier, ...]:
    """The dossiers of ``isins`` on the screens' session, in ``isins`` order without repeats.

    What it does: verifies both builds and that they are the same session's, takes every feature
    the screens build already computed, and reads the rest (F&O, filings for the margin trend and
    debt-equity, announcements, corporate actions, deals) through ``source`` and the PIT guard.
    What it assumes: each ISIN is in the session's universe; one that is not raises
    ``ValueError``, because a dossier for a name a manager cannot trade is a defect upstream.
    What it never does: read past the session, or fill a missing value. A source that cannot be
    read is named in each dossier's ``gaps`` and its fields are ``None``.
    """
    sheets.verify()
    screens.verify()
    if screens.build_digest != sheets.build_digest:
        raise ValueError("the screens were built on a different sheets build")
    session = screens.trading_date
    wanted = list(dict.fromkeys(isins))
    rows = {r.isin: r for r in sheets.universe}
    if missing := [i for i in wanted if i not in rows]:
        raise ValueError(f"not in the {session} universe: {missing}")
    pit = PitContext(as_of=session)
    gaps: list[Gap] = []
    member_set = frozenset(wanted)
    features = {f.isin: f for f in screens.features}
    earnings = {e.isin: e for e in screens.earnings}
    excluded = {e.isin: e for e in screens.exclusions.excluded}

    calendar_read = _admit(
        lambda: source.sessions(session, ANNOUNCEMENT_SESSIONS + 1), pit, gaps, "sessions"
    )
    calendar = sorted(set(calendar_read or (session,)))
    start = (
        calendar[0] + timedelta(days=1) if len(calendar) > ANNOUNCEMENT_SESSIONS else calendar[0]
    )

    # The latest two F&O sessions within the last five NSE sessions: L2 can lag L1 by a day or two,
    # and a lagging reading is shown under its own date (``fo_session``) rather than dropped.
    fo_today: dict[str, Any] = {}
    fo_prev: dict[str, Any] = {}
    fo = _admit(lambda: source.fo_readings(calendar[-_FO_SESSIONS:]), pit, gaps, "fo_aggregates")
    fo_days = sorted({r.trade_date for r in fo or ()})
    if fo_days and fo_days[-1] != session:
        gaps.append(
            Gap(source="fo_aggregates", reason=f"latest F&O session is {fo_days[-1].isoformat()}")
        )
    for reading in fo or ():
        if reading.isin not in member_set:
            continue
        if reading.trade_date == fo_days[-1]:
            fo_today[reading.isin] = reading
        elif len(fo_days) > 1 and reading.trade_date == fo_days[-2]:
            fo_prev[reading.isin] = reading

    facts = _admit(lambda: source.filings(session), pit, gaps, "filings")
    metrics = (
        compute_metrics(
            (f for f in facts if f.isin in member_set),
            as_of=session,
            prices={i: rows[i].close for i in wanted},
        )
        if facts is not None
        else None
    )
    leverage = _admit(
        lambda: source.concept_facts(frozenset({"debt_equity_ratio"}), session),
        pit,
        gaps,
        "debt_equity",
    )
    debt_equity: dict[str, tuple[tuple[date, date, str], Decimal]] = {}
    for fact in leverage or ():
        if fact.isin not in member_set:
            continue
        rank = (fact.period_end, fact.filing_date, fact.filing_id)
        if fact.isin not in debt_equity or rank > debt_equity[fact.isin][0]:
            debt_equity[fact.isin] = (rank, fact.value)

    announcements = _admit(
        lambda: source.announcement_texts(start, session), pit, gaps, "announcements"
    )
    actions = _admit(
        lambda: source.corporate_actions(session - timedelta(days=_CA_LOOKBACK_DAYS), session),
        pit,
        gaps,
        "corporate_actions",
    )
    deal_start = calendar[-_DEAL_SESSIONS] if len(calendar) >= _DEAL_SESSIONS else calendar[0]
    deals = _admit(lambda: source.deals(deal_start, session), pit, gaps, "deals")
    keywords = load_event_keywords()

    ordered_gaps = tuple(sorted(gaps, key=lambda g: (g.source, g.reason)))
    out: list[Dossier] = []
    for isin in wanted:
        row = rows[isin]
        values: dict[str, FieldValue] = {name: getattr(row, name) for name in _SHEET_FIELDS}
        for alias, name in _ALIASES.items():
            values[alias] = values[name]
        feature = features.get(isin)
        values.update(
            feature_values(feature) if feature is not None else dict.fromkeys(FEATURE_FIELDS)
        )
        fm = metrics.get(isin) if metrics is not None else None
        values["net_margin_ttm"] = _decimal(fm.net_margin_ttm) if fm is not None else None
        values["net_margin_trend"] = _decimal(fm.net_margin_trend) if fm is not None else None
        values["debt_equity"] = debt_equity[isin][1] if isin in debt_equity else None
        e = earnings.get(isin)
        values["sue"] = e.sue if e is not None else None
        values["ear"] = e.ear if e is not None else None
        values["results_filing_date"] = e.filing_date if e is not None else None
        values["results_day0"] = e.day0 if e is not None else None
        values["results_day0_volume_ratio"] = e.day0_volume_ratio if e is not None else None
        today, prev = fo_today.get(isin), fo_prev.get(isin)
        values["fo_listed"] = None if fo is None else today is not None
        values["fo_session"] = today.trade_date if today is not None else None
        values["fo_pcr_oi"] = today.pcr_oi if today is not None else None
        values["fo_rollover_pct"] = today.rollover_pct if today is not None else None
        values["fo_oi_change"] = today.total_oi_change if today is not None else None
        spot_change = today.spot - prev.spot if today is not None and prev is not None else None
        values["fo_spot_change"] = spot_change
        values["fo_oi_buildup"] = (
            oi_buildup(spot_change, today.total_oi_change).value
            if today is not None and spot_change is not None
            else None
        )
        values["screens"] = ",".join(screens.screens_of(isin)) or None
        values["excluded"] = isin in excluded
        values["exclusion_reasons"] = (
            ",".join(r.value for r in excluded[isin].reasons) if isin in excluded else None
        )

        listed = sorted(
            (a for a in announcements or () if a.isin == isin),
            key=lambda a: (a.knowable_date, a.ref),
        )
        ann_items = tuple(
            DossierItem(
                field_id=f"{isin}.{announcement_field(a.ref)}",
                knowable_date=a.knowable_date,
                text=subject_line(a.subject, a.body)[:_MAX_SUBJECT],
                categories=_categories(a.subject, a.body, keywords),
            )
            for a in listed
        )
        due = [
            n
            for n in actions or ()
            if n.isin == isin
            and any(d is not None and d > session for d in (n.ex_date, n.record_date))
        ]
        ca_items = tuple(
            DossierItem(
                field_id=f"{isin}.ca_{k}",
                knowable_date=n.knowable_date,
                text=n.purpose[:_MAX_SUBJECT],
                due_date=n.ex_date or n.record_date,
            )
            for k, n in enumerate(
                sorted(due, key=lambda n: (n.ex_date or n.record_date or date.max, n.purpose)),
                start=1,
            )
        )
        deal_items = tuple(
            DossierItem(
                field_id=f"{isin}.deal_{k}",
                knowable_date=d.trade_date,
                text=f"{d.deal_type} {d.side} {d.quantity} @ {d.price} by {d.client_name}"[
                    :_MAX_SUBJECT
                ],
                categories=(d.deal_type.lower(),),
            )
            for k, d in enumerate(
                sorted(
                    (d for d in deals or () if d.isin == isin),
                    key=lambda d: (d.trade_date, d.deal_type, d.client_name, d.side, d.quantity),
                ),
                start=1,
            )
        )
        draft = Dossier(
            isin=isin,
            trading_date=session,
            dossier_version=DOSSIER_VERSION,
            screens_digest=screens.screens_digest,
            fields={name: values.get(name) for name in DOSSIER_FIELDS},
            announcements=ann_items,
            corporate_actions=ca_items,
            deals=deal_items,
            gaps=ordered_gaps,
            dossier_digest="0" * 64,
        )
        digest = hashlib.sha256(canonical_bytes(draft.body())).hexdigest()
        out.append(draft.model_copy(update={"dossier_digest": digest}))
    return tuple(out)
