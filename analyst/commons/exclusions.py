"""A10 · M17.9 — the universe exclusions of pre-registration §8 Amendment 1 (b).

Three rules, applied on top of the M17.1 universe sheet (which already removes GSM/ESM names on a
list at most a week old):

- **Price band at most 5 %.** The newest NSE price-band list dated on or before the session
  (L0 ``nse_price_bands``, read point in time). A name in the universe series with a band of 2 %
  or 5 % is excluded; "No Band" (the F&O names) is not a band.
- **An integrity event in the last 60 sessions** (`analyst.commons.events`): an announcement whose
  subject line matches the frozen integrity table excludes its ISIN on the session it became
  knowable and the 59 sessions after it. An announcement disseminated on a non-session day counts
  from the next session.
- **The GSM/ESM list must be fresh.** Each excluded surveillance list is read as its newest
  snapshot on or before the session, and it must be dated within the last
  :data:`SURVEILLANCE_FRESH_SESSIONS` NSE sessions, the session included. A fresh list's members
  are excluded. With a list missing or older than that, the session admits **no new BUY**:
  ``buys_blocked`` is set and the missing list is recorded as a gap. Sells and holds are not
  blocked, because a stale list says nothing new about a name already held.

A source that cannot be read is a gap, never an empty list. A missing band list is a gap and the
band rule is not applied; it does not block buys, because the amendment ties that only to the
surveillance list. The announcements in the lake start late (L1 ``announcements`` opens
2026-09-28): when the earliest announcement read is more than a week after the 60-session window
opens, a gap says how much of the window is covered.

What it never does: read a clock, guess an ISIN from a symbol, or exclude a name for an event
dated after the session (the PIT guard has already refused one).
"""

from __future__ import annotations

from collections import defaultdict
from collections.abc import Mapping, Sequence
from datetime import date, timedelta
from decimal import Decimal
from enum import StrEnum
from typing import Final

from pydantic import BaseModel, ConfigDict

from analyst.commons.digests import AnnouncementText
from analyst.commons.events import KeywordTable, classify
from analyst.commons.inputs import PriceBandEntry
from analyst.commons.sheets import Gap, SurveillanceEntry

__all__ = [
    "INTEGRITY_EXCLUSION_SESSIONS",
    "PRICE_BAND_MAX_PCT",
    "SURVEILLANCE_FRESH_SESSIONS",
    "Exclusion",
    "ExclusionReason",
    "Exclusions",
    "IntegrityEvent",
    "compute_exclusions",
    "integrity_events",
]

PRICE_BAND_MAX_PCT: Final = Decimal(5)
INTEGRITY_EXCLUSION_SESSIONS: Final = 60
SURVEILLANCE_FRESH_SESSIONS: Final = 5
#: An announcements read whose first record is this many days after the window opens has not
#: covered the window (a week of an exchange's announcements is never empty).
_COVERAGE_SLACK_DAYS: Final = 7
_MAX_SUBJECT: Final = 300


class ExclusionReason(StrEnum):
    PRICE_BAND = "PRICE_BAND"
    INTEGRITY_EVENT = "INTEGRITY_EVENT"
    SURVEILLANCE = "SURVEILLANCE"


class _Model(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")


class IntegrityEvent(_Model):
    """One announcement the integrity table matched, and the last session it excludes for."""

    isin: str
    ref: str
    knowable_date: date
    categories: tuple[str, ...]
    subject: str
    excluded_through: date | None


class Exclusion(_Model):
    """One universe name removed this session, with every reason that removed it."""

    isin: str
    reasons: tuple[ExclusionReason, ...]
    band_pct: Decimal | None
    surveillance: tuple[str, ...]
    events: tuple[IntegrityEvent, ...]


class Exclusions(_Model):
    """The session's exclusions, whether new buys are blocked, and what could not be read."""

    excluded: tuple[Exclusion, ...]
    buys_blocked: bool
    block_reason: str | None
    surveillance_lists: dict[str, date | None]
    band_list: date | None
    integrity_window_start: date | None
    gaps: tuple[Gap, ...]

    @property
    def isins(self) -> frozenset[str]:
        return frozenset(e.isin for e in self.excluded)


def integrity_events(
    announcements: Sequence[AnnouncementText],
    *,
    calendar: Sequence[date],
    keywords: KeywordTable,
    future_sessions: Sequence[date] = (),
) -> tuple[IntegrityEvent, ...]:
    """Every announcement in ``announcements`` that matches the integrity table.

    ``excluded_through`` is the 60th session counting the first session on or after the
    announcement as the first. It is looked up in ``calendar`` (past sessions) and then in
    ``future_sessions`` (the published holiday calendar's next sessions), and is ``None`` when
    neither reaches that far.
    """
    sessions = sorted({*calendar, *future_sessions})
    out: list[IntegrityEvent] = []
    for row in announcements:
        found = classify(row.subject, row.body, keywords).integrity
        if not found:
            continue
        first = next((i for i, s in enumerate(sessions) if s >= row.knowable_date), None)
        last_i = None if first is None else first + INTEGRITY_EXCLUSION_SESSIONS - 1
        out.append(
            IntegrityEvent(
                isin=row.isin,
                ref=row.ref,
                knowable_date=row.knowable_date,
                categories=found,
                subject=_subject(row),
                excluded_through=sessions[last_i]
                if last_i is not None and last_i < len(sessions)
                else None,
            )
        )
    return tuple(sorted(out, key=lambda e: (e.isin, e.knowable_date, e.ref)))


def _subject(row: AnnouncementText) -> str:
    text = f"{row.subject} | {row.body}" if row.body else row.subject
    return text[:_MAX_SUBJECT]


def compute_exclusions(
    universe: Sequence[str],
    *,
    calendar: Sequence[date],
    series: str,
    stages: Sequence[str],
    surveillance: Mapping[str, Sequence[SurveillanceEntry] | None],
    bands: Sequence[PriceBandEntry] | None,
    announcements: Sequence[AnnouncementText] | None,
    keywords: KeywordTable,
    future_sessions: Sequence[date] = (),
    gaps: Sequence[Gap] = (),
) -> Exclusions:
    """Apply Amendment 1 (b) to ``universe`` on ``calendar[-1]`` (module docstring).

    What it assumes: every input has passed the PIT guard as of the session. ``surveillance``
    maps each excluded stage to its newest list, or ``None`` when it could not be read;
    ``bands`` and ``announcements`` are ``None`` when they could not be read. ``gaps`` are the
    read gaps the caller already has; they are carried into the result. ``future_sessions`` (the
    published calendar's next sessions) only dates each event's ``excluded_through``.
    What it never does: exclude a name outside ``universe``, or unblock buys on a stale list.
    """
    session = calendar[-1]
    members = set(universe)
    found_gaps = list(gaps)
    reasons: dict[str, set[ExclusionReason]] = defaultdict(set)
    band_of: dict[str, Decimal | None] = {}
    on_lists: dict[str, set[str]] = defaultdict(set)

    fresh_from = (
        calendar[-SURVEILLANCE_FRESH_SESSIONS]
        if len(calendar) >= SURVEILLANCE_FRESH_SESSIONS
        else None
    )
    listed: dict[str, date | None] = {}
    stale: list[str] = []
    for stage in sorted(stages):
        entries = surveillance.get(stage)
        if entries is None:
            listed[stage] = None
            stale.append(f"{stage} list unavailable")
            continue
        dated = max((e.knowable_date for e in entries), default=None)
        listed[stage] = dated
        if dated is None or fresh_from is None or dated < fresh_from:
            stale.append(
                f"{stage} list "
                + ("is empty" if dated is None else f"dated {dated.isoformat()}")
                + f", not within the last {SURVEILLANCE_FRESH_SESSIONS} sessions"
            )
            continue
        for entry in entries:
            if entry.isin in members and entry.stage == stage:
                reasons[entry.isin].add(ExclusionReason.SURVEILLANCE)
                on_lists[entry.isin].add(stage)
    for reason in stale:
        found_gaps.append(Gap(source="surveillance", reason=f"{reason}; no new BUY this session"))

    band_list: date | None = None
    if bands is not None:
        band_list = max((b.knowable_date for b in bands), default=None)
        for band in bands:
            if band.isin not in members or band.series != series or band.band_pct is None:
                continue
            if band.band_pct <= PRICE_BAND_MAX_PCT:
                reasons[band.isin].add(ExclusionReason.PRICE_BAND)
                band_of[band.isin] = band.band_pct

    window_start: date | None = None
    events_of: dict[str, list[IntegrityEvent]] = defaultdict(list)
    if len(calendar) > INTEGRITY_EXCLUSION_SESSIONS:
        # Knowable after the 61st session back: on or after the first of the last 60 sessions.
        opens_after = calendar[-INTEGRITY_EXCLUSION_SESSIONS - 1]
        window_start = calendar[-INTEGRITY_EXCLUSION_SESSIONS]
        if announcements is not None:
            earliest = min((a.knowable_date for a in announcements), default=None)
            if earliest is None or earliest > window_start + timedelta(days=_COVERAGE_SLACK_DAYS):
                found_gaps.append(
                    Gap(
                        source="integrity_events",
                        reason=(
                            "no announcement in the lake"
                            if earliest is None
                            else f"announcements start {earliest.isoformat()}"
                        )
                        + f"; the 60-session window opens {window_start.isoformat()}, so earlier "
                        "integrity events cannot be seen",
                    )
                )
            in_window = [a for a in announcements if opens_after < a.knowable_date <= session]
            for event in integrity_events(
                in_window, calendar=calendar, keywords=keywords, future_sessions=future_sessions
            ):
                if event.isin in members:
                    reasons[event.isin].add(ExclusionReason.INTEGRITY_EVENT)
                    events_of[event.isin].append(event)
    else:
        found_gaps.append(
            Gap(source="integrity_events", reason="fewer sessions than the 60-session window")
        )

    excluded = tuple(
        Exclusion(
            isin=isin,
            reasons=tuple(sorted(reasons[isin])),
            band_pct=band_of.get(isin),
            surveillance=tuple(sorted(on_lists[isin])),
            events=tuple(events_of[isin]),
        )
        for isin in sorted(reasons)
    )
    return Exclusions(
        excluded=excluded,
        buys_blocked=bool(stale),
        block_reason="; ".join(stale) if stale else None,
        surveillance_lists=listed,
        band_list=band_list,
        integrity_window_start=window_start,
        gaps=tuple(sorted(found_gaps, key=lambda g: (g.source, g.reason))),
    )
