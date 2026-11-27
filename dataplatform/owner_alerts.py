"""Owner alerts (OPS-ALERTS): short, plain-English messages to the owner's phone.

The platform's other alerts (`alert_triggers`, `ops_guardian`) speak to an operator: source ids,
job names, ISO timestamps. The owner reads none of that. This module turns what the platform
already records into messages the owner can act on — IST times, ₹ in lakh and crore, no internal
ids — each ending in what, if anything, the owner should do. It only READS (Postgres, the lake,
the kill-switch files, one box-local markdown file) and SENDS through the configured alerter; it
changes no decision path, book, prompt, mandate or rail.

Two kinds of message, both deduplicated in `alert_condition` (0027) so a restart never repeats one:

* **states**, reconciled onset/clear like the M13.2 triggers (`alert_triggers.tick`): a kill switch
  tripped (and reset), a book past its loss limit (drawdown from its own peak, in steps), a key
  dataset more than N trading sessions old;
* **one-shot news**, sent once per key with `alert_triggers.send_once`: the 08:00 summary, a paper
  book's activity after its session, the M17 nightly summary, a degraded night, a one-session drop,
  the Monday list of decisions waiting.

Owner's exclusion (16 Nov 2026): no LLM cost, token or spend figure appears in any message.
"""

from __future__ import annotations

import argparse
import re
import shutil
import sys
from collections import Counter
from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from decimal import ROUND_HALF_UP, Decimal
from pathlib import Path
from typing import TYPE_CHECKING, Any, Final

from dataplatform.alert_triggers import (
    AlertCondition,
    ConditionLedger,
    ResolutionFormatter,
    TickReport,
    Trigger,
    TriggerEvaluationError,
    TriggerEvaluator,
    redact,
    send_once,
    tick,
)
from dataplatform.alerts import Alerter, Severity
from dataplatform.clock import IST, Clock
from dataplatform.logging import get_logger

if TYPE_CHECKING:  # the scheduler imports this module lazily; keep the edge one-way at runtime
    from dataplatform.config import Settings
    from dataplatform.scheduler.registry import JobContext
    from dataplatform.status.queries_portfolio import SessionPrices
    from dataplatform.store.db import Connection

__all__ = [
    "MAX_MESSAGE_CHARS",
    "PAPER_BOOKS",
    "POLLED_DATASETS",
    "DatasetFreshness",
    "M17Night",
    "Message",
    "PaperActivity",
    "PaperFigures",
    "book_name",
    "build_morning_summary",
    "degraded_message",
    "drawdown_conditions",
    "fit",
    "freshness",
    "inr",
    "kill_switch_conditions",
    "m17_night_message",
    "main",
    "paper_activity_message",
    "pending_decisions_message",
    "session_drop_message",
    "stale_data_conditions",
]

log = get_logger(__name__)

#: Telegram's hard limit is 4096; the alerter prefixes a severity tag and the title. Every message
#: this module builds is cut to this, so a long night can never be the message that fails to send.
MAX_MESSAGE_CHARS: Final = 3800

#: The strategy paper books, by the name the owner knows them by.
PAPER_BOOKS: Final[dict[str, str]] = {
    "momentum_v2_paper_2026_09_06": "Current momentum book (D13)",
    "momentum_v2_turn_srefill_paper_2026_11_09": "Second momentum book (early entry)",
}

#: The kill-switch accounts that are not strategy books.
_ACCOUNTS: Final[dict[str, str]] = {
    "m17_fund_managers": "AI fund managers (all their books)",
    "m17_dry_fund_managers": "AI fund managers rehearsal",
    "m17_dry2_fund_managers": "AI fund managers rehearsal",
}

#: The M17 managers, by style rather than id.
_MANAGERS: Final[dict[str, str]] = {
    "FM-SWING-BRK": "Breakout swing",
    "FM-SWING-EVT": "Event swing",
    "FM-POS-TREND": "Trend",
    "FM-POS-FUND": "Fundamentals",
}

#: The live M17 desk's record in `paper_session`.
M17_DESK: Final = "m17_fund_managers"
BENCH: Final = "BENCH-N500"

_KEY = "owner:"


# ── wording ──────────────────────────────────────────────────────────────────────────────────


def _group_indian(whole: int) -> str:
    """12345678 → '1,23,45,678' (the Indian grouping)."""
    digits = str(abs(whole))
    if len(digits) <= 3:
        grouped = digits
    else:
        head, tail = digits[:-3], digits[-3:]
        parts: list[str] = []
        while len(head) > 2:
            parts.insert(0, head[-2:])
            head = head[:-2]
        if head:
            parts.insert(0, head)
        grouped = ",".join([*parts, tail])
    return ("-" if whole < 0 else "") + grouped


def inr(amount: Decimal) -> str:
    """₹ the way the owner reads it: '₹1.02 Cr', '₹9.86 L', '₹45,300'."""
    sign = "-" if amount < 0 else ""
    value = abs(amount)
    if value >= Decimal(10_000_000):
        return f"{sign}₹{(value / Decimal(10_000_000)).quantize(Decimal('0.01'), ROUND_HALF_UP)} Cr"
    if value >= Decimal(100_000):
        return f"{sign}₹{(value / Decimal(100_000)).quantize(Decimal('0.01'), ROUND_HALF_UP)} L"
    return f"{sign}₹{_group_indian(int(value.quantize(Decimal(1), ROUND_HALF_UP)))}"


def pct(value: Decimal | None, *, signed: bool = True) -> str:
    """A percentage with one decimal: '+1.2%', '-0.4%'; 'n/a' when unknown."""
    if value is None:
        return "n/a"
    rounded = value.quantize(Decimal("0.1"), ROUND_HALF_UP)
    if rounded == 0:
        rounded = abs(rounded)
    text = f"{rounded}%"
    if signed and rounded > 0:
        text = "+" + text
    return text


def day(on: date) -> str:
    """'Wed 18 Nov'."""
    return f"{on:%a} {on.day} {on:%b}"


def _ist_day(at: datetime) -> str:
    return day(at.astimezone(IST).date())


def ist_time(at: datetime) -> str:
    """'22:22 IST'."""
    return f"{at.astimezone(IST):%H:%M} IST"


def _change(new: Decimal, old: Decimal) -> Decimal | None:
    return None if old == 0 else (new - old) / old * Decimal(100)


def fit(text: str, limit: int = MAX_MESSAGE_CHARS) -> str:
    """`text` cut to `limit` characters at a line boundary, saying it was cut."""
    if len(text) <= limit:
        return text
    marker = "\n… (shortened)"
    cut = text[: limit - len(marker)]
    if "\n" in cut:
        cut = cut[: cut.rfind("\n")]
    return cut + marker


def book_name(book_id: str) -> str:
    """The owner's name for a book or kill-switch account; the id itself only as a last resort."""
    if book_id in PAPER_BOOKS:
        return PAPER_BOOKS[book_id]
    if book_id in _ACCOUNTS:
        return _ACCOUNTS[book_id]
    if book_id == BENCH:
        return "Market (NIFTY 500)"
    control = book_id.startswith("CTRL-")
    stem = book_id.removeprefix("CTRL-")
    for manager_id, name in _MANAGERS.items():
        if stem.startswith(manager_id + "-"):
            size = stem.removeprefix(manager_id + "-")
            label = f"{name} {'₹10 L' if size == '10L' else '₹1 Cr' if size == '1CR' else size}"
            return f"{label} (no-AI control)" if control else label
    return book_id.replace("_", " ")


@dataclass(frozen=True, slots=True)
class Message:
    """One owner message: what it is about and what it says."""

    severity: Severity
    title: str
    body: str

    def __post_init__(self) -> None:
        object.__setattr__(self, "title", redact(self.title, limit=150))
        object.__setattr__(self, "body", fit(_clean(self.body)))


def _clean(body: str) -> str:
    """`redact` line by line, keeping the line breaks the owner reads by."""
    return "\n".join(redact(line, limit=MAX_MESSAGE_CHARS) for line in body.split("\n"))


# ── the paper books ──────────────────────────────────────────────────────────────────────────


@dataclass(frozen=True, slots=True)
class PaperOrder:
    side: str
    name: str
    value: Decimal | None


@dataclass(frozen=True, slots=True)
class PaperActivity:
    """One paper book's newest session as `paper_session` recorded it."""

    book_id: str
    session: date
    outcome: str
    reason: str
    rebalanced: bool
    orders: tuple[PaperOrder, ...]
    next_session: date | None
    was_in_cash: bool
    """The book held no stocks before this session's orders."""


_WHY: Final[dict[str, str]] = {
    "rebalance": "the monthly rebalance moved the book to this month's strongest stocks.",
    "rebalance_risk_off": "the monthly rebalance found the market filter negative, so the book "
    "goes to (or stays in) cash.",
    "early_reentry": "the market filter turned positive before month-end, so the book "
    "re-entered the market early.",
    "decided": "routine top-up or trim by the book's own rules.",
}


def paper_activity_message(activity: PaperActivity) -> Message | None:
    """The message for a paper book's session — None when nothing happened worth telling."""
    name = book_name(activity.book_id)
    when = day(activity.session)
    if activity.outcome == "SKIPPED_DATA_RED":
        return Message(
            Severity.WARNING,
            f"{name}: skipped {when}",
            f"{name} did not trade on {when}: the day's data was not ready or not clean "
            f"({_short_reason(activity.reason)}).\n"
            "Nothing was bought or sold. It runs again at the next session.\n"
            "Nothing for you to do unless this repeats.",
        )
    if activity.outcome == "RECON_BREAK":
        return Message(
            Severity.CRITICAL,
            f"{name}: book check failed on {when}",
            f"After {when}'s fills, {name} and its paper broker disagree, so the book is paused "
            "(no new orders) until the difference is explained.\n"
            "Needs you: ask for the mismatch to be checked.",
        )
    buys = [o for o in activity.orders if o.side == "BUY"]
    sells = [o for o in activity.orders if o.side == "SELL"]
    if not activity.orders and not activity.rebalanced:
        return None
    fill = (
        "the next open" if activity.next_session is None else f"{day(activity.next_session)}'s open"
    )
    lines: list[str] = []
    if activity.reason == "rebalance_risk_off" and not activity.orders:
        title = f"{name}: monthly rebalance, stays in cash"
        lines.append(
            f"Monthly rebalance on {when}: the market filter is negative, so the book "
            "stays fully in cash. No orders."
        )
    elif activity.rebalanced and not activity.orders:
        title = f"{name}: monthly rebalance, no changes"
        lines.append(f"Monthly rebalance on {when}: the holdings already match. No orders.")
    else:
        parts = []
        if buys:
            parts.append(f"{len(buys)} buy{'s' if len(buys) != 1 else ''}")
        if sells:
            parts.append(f"{len(sells)} sell{'s' if len(sells) != 1 else ''}")
        moving = ""
        if buys and not sells and activity.was_in_cash:
            moving = " (moving into the market)"
        elif sells and not buys and activity.reason == "rebalance_risk_off":
            moving = " (moving out of the market)"
        title = f"{name}: {' and '.join(parts)}{moving}"
        lines.append(f"On {when} the book placed {' and '.join(parts)}, to fill at {fill}.")
        for side, orders, verb in (("BUY", buys, "to invest"), ("SELL", sells, "to raise")):
            if not orders:
                continue
            known = [o.value for o in orders if o.value is not None]
            total = sum(known, Decimal(0))
            approx = "about " if len(known) == len(orders) else "at least "
            lines.append(f"{'Buys' if side == 'BUY' else 'Sells'}: {approx}{inr(total)} {verb}.")
            top = sorted(orders, key=lambda o: o.value or Decimal(0), reverse=True)[:3]
            lines.append(
                "Largest: "
                + ", ".join(f"{o.name} {inr(o.value)}" if o.value else o.name for o in top)
                + "."
            )
    if activity.orders:
        lines.append("Why: " + _WHY.get(activity.reason, "the book's own rules."))
    lines.append("Nothing for you to do.")
    return Message(Severity.INFO, title, "\n".join(lines))


def _short_reason(reason: str) -> str:
    text = redact(reason, limit=160)
    return text.rstrip(".")


# ── the M17 night ────────────────────────────────────────────────────────────────────────────

#: Journal events that mean a manager did not do its job that night.
_MANAGER_ERRORS: Final = (
    "MANAGER_ERROR",
    "MISSED_SESSION",
    "MANAGER_CRASHED",
    "COMMONS_UNAVAILABLE",
)
#: Journal events that put an order on the book for the next open.
_STAGED_EVENTS: Final = ("STAGED", "MIRROR_FOLLOW", "STOP_EXIT")


@dataclass(frozen=True, slots=True)
class BookNight:
    """One M17 book's night: what it staged, what was refused, what went wrong."""

    buys: int = 0
    sells: int = 0
    refused: int = 0
    errors: int = 0


@dataclass(frozen=True, slots=True)
class M17Night:
    """One M17 session as the live journal and the desk record show it."""

    session: date
    outcome: str
    """The desk record's outcome: COMPLETED, SKIPPED_DATA_RED or RECON_BREAK."""
    recorded_at: datetime
    books: Mapping[str, BookNight]
    fallback_calls: Mapping[str, int] = field(default_factory=dict)
    """Manager calls the backup model answered, by the provider that could not serve them."""
    digest_fallbacks: int = 0
    """Filing digests not written by the primary model."""
    data_gaps: str | None = None
    """What was still missing when the desk ran anyway past its data wait."""


@dataclass(frozen=True, slots=True)
class BookReturn:
    """A book's return since S0, with its control's when it is a manager book."""

    book_id: str
    nav: Decimal
    return_pct: Decimal | None
    control_return_pct: Decimal | None


def _manager_books(books: Iterable[str]) -> list[str]:
    """Manager books in roster order, each ₹10 L book before its ₹1 Cr copy."""
    order = list(_MANAGERS)

    def rank(book_id: str) -> tuple[int, bool, str]:
        manager = book_id.rsplit("-", 1)[0]
        return (
            order.index(manager) if manager in order else len(order),
            book_id.endswith("1CR"),
            book_id,
        )

    return sorted((b for b in books if b.startswith("FM-")), key=rank)


def _count(n: int, noun: str) -> str:
    return f"{n} {noun}{'' if n == 1 else 's'}"


def m17_night_message(night: M17Night, returns: Sequence[BookReturn], s0: date | None) -> Message:
    """The M17 nightly summary: per manager, what was staged and refused; errors; vs control."""
    when = day(night.session)
    if night.outcome == "SKIPPED_DATA_RED":
        return Message(
            Severity.WARNING,
            f"AI fund managers: skipped {when}",
            f"The AI fund managers did not decide on {when}: the day's data was not ready or not "
            "clean. Nothing was traded; the books were still valued.\n"
            "Nothing for you to do unless this repeats.",
        )
    lines = [f"Night of {when}: finished at {ist_time(night.recorded_at)}."]
    by_return = {r.book_id: r for r in returns}
    errors = 0
    for book_id in _manager_books(night.books):
        if book_id.endswith("1CR"):
            continue
        primary = night.books[book_id]
        mirror_id = book_id[: -len("10L")] + "1CR"
        mirror = night.books.get(mirror_id, BookNight())
        errors += primary.errors + mirror.errors
        orders = f"{_count(primary.buys, 'buy')}, {_count(primary.sells, 'sell')}"
        copy = mirror.buys + mirror.sells
        line = f"{book_name(book_id).removesuffix(' ₹10 L')}: {orders} (₹1 Cr copy {copy})"
        refused = primary.refused + mirror.refused
        if refused:
            line += f", {refused} refused by the risk rules"
        if primary.errors or mirror.errors:
            line += f", {primary.errors + mirror.errors} error(s)"
        ret = by_return.get(book_id)
        if ret is not None and ret.return_pct is not None:
            line += f"; ₹10 L book {pct(ret.return_pct)} vs control {pct(ret.control_return_pct)}"
        lines.append(line + ".")
    bench = by_return.get(BENCH)
    if bench is not None and bench.return_pct is not None and s0 is not None:
        lines.append(f"Market (NIFTY 500) since {day(s0)}: {pct(bench.return_pct)}.")
    lines.append("Orders fill at the next open.")
    fallbacks = sum(night.fallback_calls.values()) + night.digest_fallbacks
    if errors:
        lines.append(
            f"Needs you: {errors} manager error(s) tonight — ask for a check if it repeats."
        )
    elif fallbacks or night.data_gaps:
        lines.append("Ran degraded — see the separate message. Nothing for you to do.")
    else:
        lines.append("All normal. Nothing for you to do.")
    severity = Severity.WARNING if errors else Severity.INFO
    return Message(severity, f"AI fund managers: night of {when}", "\n".join(lines))


def degraded_message(
    session: date, night: M17Night | None, skipped_papers: Sequence[PaperActivity]
) -> Message | None:
    """One message per night when anything ran on a backup or with data missing; else None."""
    when = day(session)
    lines: list[str] = []
    if night is not None:
        if night.fallback_calls:
            calls = sum(night.fallback_calls.values())
            lines.append(
                f"AI fund managers: {calls} decision call(s) were answered by the backup model "
                "because the main model (Bedrock Opus) could not serve them."
            )
        if night.digest_fallbacks:
            lines.append(
                f"Filing summaries: {night.digest_fallbacks} were written by the backup model "
                "instead of the main one (Luna)."
            )
        if night.data_gaps:
            lines.append(
                "AI fund managers ran past their data wait with data still missing: "
                f"{_short_reason(night.data_gaps)}."
            )
        if night.outcome == "SKIPPED_DATA_RED":
            lines.append("AI fund managers skipped the night: data not ready or not clean.")
    for paper in skipped_papers:
        lines.append(
            f"{book_name(paper.book_id)} skipped the session: data not ready or not clean."
        )
    if not lines:
        return None
    lines.append(
        "Decisions still went through the same risk rules. Nothing for you to do "
        "unless this happens several nights in a row."
    )
    return Message(Severity.WARNING, f"Ran degraded on {when}", "\n".join(lines))


# ── loss limits ──────────────────────────────────────────────────────────────────────────────


@dataclass(frozen=True, slots=True)
class BookMarks:
    """A book's value at each session's close since it started, oldest first."""

    book_id: str
    opening: Decimal
    marks: tuple[tuple[date, Decimal], ...]

    def peak(self) -> tuple[date | None, Decimal]:
        best: tuple[date | None, Decimal] = (None, self.opening)
        for on, nav in self.marks:
            if nav > best[1]:
                best = (on, nav)
        return best

    def drawdown_pct(self) -> Decimal | None:
        if not self.marks:
            return None
        _, peak = self.peak()
        latest = self.marks[-1][1]
        return None if peak <= 0 else (peak - latest) / peak * Decimal(100)


def _bands(drawdown: Decimal, threshold: Decimal, step: Decimal) -> list[Decimal]:
    """The limit levels `drawdown` is past: 10, 15, 20, … (threshold, then each further step)."""
    levels: list[Decimal] = []
    level = threshold
    while drawdown > level:
        levels.append(level)
        level += step
    return levels


def _band_key(book_id: str, level: Decimal) -> str:
    return f"{_KEY}loss:{book_id}:drawdown:{int(level)}"


def drawdown_conditions(
    books: Iterable[BookMarks], *, threshold: Decimal, step: Decimal
) -> list[AlertCondition]:
    """One WARNING per limit level a book is past: its onset is the page, each deeper level is a
    new one. The base level's clearing is the "back within limits" message
    (`drawdown_resolution`)."""
    conditions: list[AlertCondition] = []
    for book in books:
        drawdown = book.drawdown_pct()
        if drawdown is None:
            continue
        peak_on, peak = book.peak()
        latest_on, latest = book.marks[-1]
        name = book_name(book.book_id)
        for level in _bands(drawdown, threshold, step):
            peak_when = "its start" if peak_on is None else day(peak_on)
            conditions.append(
                AlertCondition(
                    trigger=Trigger.OWNER_LOSS,
                    dedup_key=_band_key(book.book_id, level),
                    severity=Severity.CRITICAL if level > threshold else Severity.WARNING,
                    title=f"{name}: down more than {int(level)}% from its peak",
                    body=(
                        f"{name} is worth {inr(latest)} on {day(latest_on)}, "
                        f"{pct(drawdown, signed=False)} below its peak of {inr(peak)} "
                        f"({peak_when}). That is past the {int(level)}% loss limit.\n"
                        "Nothing is sold because of this message; the book follows its own rules.\n"
                        "Needs you: decide whether to keep running it or pause it."
                    ),
                )
            )
    return conditions


def drawdown_resolution(
    books: Mapping[str, BookMarks], *, threshold: Decimal
) -> ResolutionFormatter:
    """Clearing the base level says the book is back within limits; deeper levels clear quietly."""

    def word(key: str, title: str, at: datetime) -> tuple[Severity, str, str] | None:
        _, _, rest = key.partition(f"{_KEY}loss:")
        book_id, _, level = rest.rpartition(":drawdown:")
        if level != str(int(threshold)):
            return None
        name = book_name(book_id)
        book = books.get(book_id)
        now = (
            ""
            if book is None or book.drawdown_pct() is None
            else (f" It is now {pct(book.drawdown_pct(), signed=False)} below its peak.")
        )
        return (
            Severity.INFO,
            f"{name}: back within its loss limit",
            f"{name} is back within its {int(threshold)}% loss limit.{now}\nNothing for you to do.",
        )

    return word


def session_drop_message(book: BookMarks, *, threshold: Decimal) -> Message | None:
    """A one-session fall of more than `threshold` %, else None."""
    if len(book.marks) >= 2:
        (_, before), (on, after) = book.marks[-2], book.marks[-1]
    elif len(book.marks) == 1:
        before, (on, after) = book.opening, book.marks[-1]
    else:
        return None
    change = _change(after, before)
    if change is None or -change <= threshold:
        return None
    name = book_name(book.book_id)
    return Message(
        Severity.WARNING,
        f"{name}: fell {pct(-change, signed=False)} on {day(on)}",
        f"{name} went from {inr(before)} to {inr(after)} on {day(on)} "
        f"({pct(change)}), more than the {pct(threshold, signed=False)} one-day limit.\n"
        "Nothing is sold because of this message. Nothing for you to do unless it keeps falling.",
    )


# ── kill switches ────────────────────────────────────────────────────────────────────────────

_SOURCES: Final[dict[str, str]] = {
    "MANUAL": "by hand",
    "RAILS": "by the risk rules",
    "RECON": "by the end-of-day book check",
}


@dataclass(frozen=True, slots=True)
class SwitchState:
    """A kill switch as read from its file: tripped, armed, or unreadable (tripped is None)."""

    account: str
    tripped: bool | None
    source: str | None = None
    reason: str | None = None
    tripped_at: datetime | None = None


def kill_switch_conditions(switches: Iterable[SwitchState]) -> list[AlertCondition]:
    """One CRITICAL condition per switch that is tripped, or whose state cannot be read."""
    conditions = []
    for switch in switches:
        if switch.tripped is False:
            continue
        name = book_name(switch.account)
        key = f"{_KEY}kill:{switch.account}"
        if switch.tripped is None:
            conditions.append(
                AlertCondition(
                    Trigger.OWNER_KILL_SWITCH,
                    key,
                    Severity.CRITICAL,
                    f"Trading halted: {name} (switch unreadable)",
                    f"The kill switch for {name} cannot be read, so it is treated as tripped: no "
                    "orders will be placed for it.\nNeeds you: ask for the switch to be checked.",
                )
            )
            continue
        how = _SOURCES.get(switch.source or "", "")
        when = (
            ""
            if switch.tripped_at is None
            else f" at {ist_time(switch.tripped_at)} on {_ist_day(switch.tripped_at)}"
        )
        why = "" if not switch.reason else f"\nReason given: {redact(switch.reason, limit=200)}"
        conditions.append(
            AlertCondition(
                Trigger.OWNER_KILL_SWITCH,
                key,
                Severity.CRITICAL,
                f"Trading halted: {name}",
                f"The kill switch for {name} was tripped{(' ' + how) if how else ''}{when}.{why}\n"
                "No orders will be placed for it until the switch is reset; its books are still "
                "valued every night.\n"
                "Needs you: once the cause is understood, decide whether to reset it.",
            )
        )
    return conditions


def kill_switch_resolution(read_reset: Callable[[str], str | None]) -> ResolutionFormatter:
    """A switch that is no longer tripped was reset: CRITICAL, like its trip."""

    def word(key: str, title: str, at: datetime) -> tuple[Severity, str, str]:
        account = key.removeprefix(f"{_KEY}kill:")
        name = book_name(account)
        note = read_reset(account)
        return (
            Severity.CRITICAL,
            f"Trading resumed: {name}",
            f"The kill switch for {name} was reset (seen {ist_time(at)} on {_ist_day(at)})"
            + ("" if not note else f". Note: {redact(note, limit=200)}")
            + ".\nIt can place orders again from its next session. Nothing for you to do if you "
            "reset it; if you did not, ask who did.",
        )

    return word


# ── stale data ───────────────────────────────────────────────────────────────────────────────


@dataclass(frozen=True, slots=True)
class DatasetFreshness:
    """A key dataset's newest date and how many trading sessions it is behind."""

    key: str
    name: str
    latest: date | None
    sessions_behind: int | None
    known_note: str | None = None
    #: True when `latest` is the newest successful *poll* (`POLLED_DATASETS`), not the newest
    #: filing: the dataset is current when its poll is, however long ago the last filing landed.
    polled: bool = False


#: Gaps the owner already knows about: alerted once, said so.
KNOWN_GAPS: Final[dict[str, str]] = {
    "tri_midsmall": "This is a known gap (stuck since Thu 1 Oct); nothing for you to do.",
}

#: Datasets judged by their poll rather than by their newest partition: key → the sync_state
#: source whose PUBLISHED poll dates say "checked, and current". Shareholding patterns are a
#: quarterly filing partitioned by *filing* date; once the quarter's deadline (21 days after
#: quarter-end) has passed, late filers trickle in days apart, so "newest filing more than two
#: sessions old" fired on a daily poll that was succeeding (26 Nov: newest filing 19 Nov, poll
#: PUBLISHED every day since). A poll that stops succeeding still goes stale on the same session
#: budget as every other dataset.
POLLED_DATASETS: Final[dict[str, str]] = {
    "shareholding": "nse_shareholding_pattern",
}


def stale_data_conditions(
    datasets: Iterable[DatasetFreshness], *, max_sessions: int
) -> list[AlertCondition]:
    """One WARNING per dataset more than `max_sessions` trading sessions behind (or empty).

    A polled dataset (`DatasetFreshness.polled`) is measured from its last successful poll, so it
    is stale when the poll stops succeeding, not when filings are merely sparse.
    """
    conditions = []
    for dataset in datasets:
        behind = dataset.sessions_behind
        if dataset.latest is not None and behind is not None and behind <= max_sessions:
            continue
        plural = "s" if behind != 1 else ""
        if dataset.polled and dataset.latest is None:
            detail = f"The daily check for new {dataset.name} has never succeeded."
        elif dataset.latest is None:
            detail = f"There is no {dataset.name} data at all."
        elif dataset.polled:
            detail = (
                f"The daily check for new {dataset.name} last succeeded on "
                f"{day(dataset.latest)}, {behind} trading session{plural} ago."
            )
        else:
            detail = (
                f"The newest {dataset.name} is from {day(dataset.latest)}, "
                f"{behind} trading session{plural} behind."
            )
        note = dataset.known_note or (
            "Books that use it may skip a session or run with a gap. Nothing for you to do; "
            "the fix is on the operator side."
        )
        conditions.append(
            AlertCondition(
                Trigger.OWNER_STALE_DATA,
                f"{_KEY}stale:{dataset.key}",
                Severity.WARNING,
                f"Data is late: {dataset.name}",
                f"{detail}\n{note}",
            )
        )
    return conditions


def stale_resolution(datasets: Mapping[str, DatasetFreshness]) -> ResolutionFormatter:
    def word(key: str, title: str, at: datetime) -> tuple[Severity, str, str]:
        dataset = datasets.get(key.removeprefix(f"{_KEY}stale:"))
        name = title.removeprefix("Data is late: ") if dataset is None else dataset.name
        label = "last checked" if dataset is not None and dataset.polled else "newest"
        latest = (
            ""
            if dataset is None or dataset.latest is None
            else f" ({label}: {day(dataset.latest)})"
        )
        return (
            Severity.INFO,
            f"Data caught up: {name}",
            f"{name} is up to date again{latest}.\nNothing for you to do.",
        )

    return word


# ── the morning summary and the Monday list ──────────────────────────────────────────────────


@dataclass(frozen=True, slots=True)
class JobsDay:
    """Yesterday's scheduled jobs: how many ended well, and which ended failed."""

    on: date
    ok: int
    failed: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class PaperFigures:
    """A paper book at its newest decided session."""

    book_id: str
    session: date
    nav: Decimal | None
    invested_pct: Decimal | None
    day_change_pct: Decimal | None
    since_start_pct: Decimal | None
    nifty_tri_pct: Decimal | None
    first_session: date | None


@dataclass(frozen=True, slots=True)
class OpenAlert:
    title: str
    severity: str
    opened_at: datetime


@dataclass(frozen=True, slots=True)
class SummaryInputs:
    """Everything the 08:00 summary reads; any part may be None when it could not be read."""

    today: date
    jobs: JobsDay | None
    night: M17Night | None
    m17_returns: Sequence[BookReturn] | None
    s0: date | None
    papers: Sequence[PaperFigures] | None
    open_alerts: Sequence[OpenAlert] | None
    disk_pct: int | None
    tripped: Sequence[str] = ()
    loss_limits: Sequence[str] = ()
    unreadable: Sequence[str] = ()


def _job_label(name: str) -> str:
    return name.replace("_", " ")


def build_morning_summary(
    inputs: SummaryInputs, *, disk_warn_pct: int, title: str | None = None
) -> Message:
    """The 08:00 summary: yesterday's jobs, the last M17 night, the books, open alerts, disk —
    ending in "All normal" or "Needs you: …". Saturday and Sunday are shorter."""
    weekend = inputs.today.weekday() >= 5
    lines: list[str] = []
    needs: list[str] = []
    now = datetime.combine(inputs.today, datetime.min.time(), tzinfo=IST)

    if inputs.jobs is None:
        lines.append("Scheduled jobs: could not be read.")
        needs.append("the job history could not be read")
    elif inputs.jobs.failed:
        lines.append(
            f"Scheduled jobs on {day(inputs.jobs.on)}: {inputs.jobs.ok} fine, failed: "
            + ", ".join(_job_label(j) for j in inputs.jobs.failed)
            + "."
        )
        needs.append(
            "a scheduled job failed (" + ", ".join(_job_label(j) for j in inputs.jobs.failed) + ")"
        )
    else:
        lines.append(f"Scheduled jobs on {day(inputs.jobs.on)}: all {inputs.jobs.ok} fine.")

    night = inputs.night
    if night is not None and (inputs.today - night.session).days <= 4:
        errors = sum(b.errors for b in night.books.values())
        if night.outcome == "SKIPPED_DATA_RED":
            lines.append(
                f"AI fund managers, night of {day(night.session)}: skipped (data not ready)."
            )
            needs.append("the AI fund managers skipped a night")
        else:
            per = []
            for book_id in _manager_books(night.books):
                if book_id.endswith("1CR"):
                    continue
                b = night.books[book_id]
                per.append(f"{book_name(book_id).removesuffix(' ₹10 L')} {b.buys + b.sells}")
            fallbacks = sum(night.fallback_calls.values()) + night.digest_fallbacks
            text = (
                f"AI fund managers, night of {day(night.session)}: completed "
                f"{ist_time(night.recorded_at)}. Orders: {', '.join(per) or 'none'}. "
                f"Errors: {errors or 'none'}."
            )
            if fallbacks:
                text += f" Backup model used {fallbacks} time(s)."
            lines.append(text)
            if errors:
                needs.append(f"{errors} AI manager error(s) on {day(night.session)}")
    elif not weekend or inputs.today.weekday() == 5:
        lines.append("AI fund managers: no finished night found for the last session.")
        if not weekend:
            needs.append("no AI fund managers result for the last session")

    if inputs.papers is None:
        lines.append("Momentum books: could not be read.")
        needs.append("the momentum books could not be read")
    else:
        for paper in inputs.papers:
            name = book_name(paper.book_id)
            if paper.nav is None:
                lines.append(f"{name}: value unknown on {day(paper.session)} (a price is missing).")
                continue
            text = f"{name}: {inr(paper.nav)}, {pct(paper.invested_pct, signed=False)} in stocks"
            if not weekend:
                text += f"; day {pct(paper.day_change_pct)}"
            text += (
                f"; since start {pct(paper.since_start_pct)}"
                f" vs NIFTY 50 TRI {pct(paper.nifty_tri_pct)}."
            )
            lines.append(text)

    if inputs.m17_returns:
        by_id = {r.book_id: r for r in inputs.m17_returns}
        since = "" if inputs.s0 is None else f" since {day(inputs.s0)}"
        managers = [by_id[b] for b in _manager_books(by_id)]
        if weekend:
            primaries = [
                r for r in managers if r.book_id.endswith("10L") and r.return_pct is not None
            ]
            if primaries:
                best = max(primaries, key=lambda r: r.return_pct or Decimal(0))
                worst = min(primaries, key=lambda r: r.return_pct or Decimal(0))
                lines.append(
                    f"AI books{since}: best {book_name(best.book_id)} {pct(best.return_pct)}, "
                    f"weakest {book_name(worst.book_id)} {pct(worst.return_pct)}."
                )
        else:
            lines.append(f"AI books{since} (vs their no-AI control):")
            for r in managers:
                lines.append(
                    f"• {book_name(r.book_id)}: {inr(r.nav)}, {pct(r.return_pct)} "
                    f"(control {pct(r.control_return_pct)})"
                )
        bench = by_id.get(BENCH)
        if bench is not None:
            lines.append(f"Market (NIFTY 500){since}: {pct(bench.return_pct)}.")
    elif inputs.m17_returns is None:
        lines.append("AI books: could not be read.")

    for account in inputs.tripped:
        needs.append(f"trading is halted for {book_name(account)}")
    for item in inputs.loss_limits:
        needs.append(item)

    if inputs.open_alerts is None:
        lines.append("Open alerts: could not be read.")
    else:
        fresh = [a for a in inputs.open_alerts if now - a.opened_at < timedelta(hours=24)]
        if not inputs.open_alerts:
            lines.append("Open alerts: none.")
        else:
            oldest = min(a.opened_at for a in inputs.open_alerts)
            text = f"Open alerts: {len(inputs.open_alerts)} (oldest since {_ist_day(oldest)})"
            text += (
                "; none new."
                if not fresh
                else f"; new: {'; '.join(redact(a.title, limit=80) for a in fresh[:3])}."
            )
            lines.append(text)
            serious = [a for a in fresh if a.severity == Severity.CRITICAL.value]
            if serious:
                needs.append(f"{len(serious)} new serious alert(s)")

    if inputs.disk_pct is None:
        lines.append("Disk: could not be read.")
    else:
        lines.append(f"Disk: {inputs.disk_pct}% used.")
        if inputs.disk_pct >= disk_warn_pct:
            needs.append(f"the disk is {inputs.disk_pct}% full")
    for what in inputs.unreadable:
        needs.append(f"{what} could not be read")

    lines.append("")
    lines.append(
        "All normal." if not needs else "Needs you: " + "; ".join(dict.fromkeys(needs)) + "."
    )
    severity = Severity.WARNING if needs else Severity.INFO
    return Message(severity, title or f"Morning summary — {day(inputs.today)}", "\n".join(lines))


_OPEN_LINE: Final = re.compile(r"^\s*[-*]\s*\[open\]\s*(.+?)\s*$", re.IGNORECASE)


def pending_decisions_message(path: Path, today: date) -> Message | None:
    """Monday's "Decisions waiting on you" from the `[open]` lines; None when there are none.

    A missing or unreadable file is itself the message: the reminder never goes quiet because
    its list moved.
    """
    try:
        text = path.read_text(encoding="utf-8")
    except OSError as error:
        return Message(
            Severity.WARNING,
            "Decisions waiting on you",
            f"The list of decisions waiting on you could not be read ({type(error).__name__}: "
            f"{path.name} is missing or unreadable), so this week's reminder is empty.\n"
            "Needs you: ask for the list to be restored.",
        )
    items = [m.group(1) for line in text.splitlines() if (m := _OPEN_LINE.match(line))]
    if not items:
        return None
    lines = [
        f"{len(items)} decision{'s' if len(items) != 1 else ''} waiting on you ({day(today)}):"
    ]
    lines += [f"{n}. {item}" for n, item in enumerate(items, 1)]
    lines.append("Reply with your decision on any of these when you can.")
    return Message(Severity.INFO, "Decisions waiting on you", "\n".join(lines))


# ── the reads ────────────────────────────────────────────────────────────────────────────────

_PAPER_ROWS_SQL: Final = """
SELECT DISTINCT ON (book_id) book_id, trading_date, outcome, reason, rebalanced, orders, recorded_at
FROM paper_session WHERE book_id = ANY(%s)
ORDER BY book_id, trading_date DESC
"""

_SYMBOLS_SQL: Final = """
SELECT DISTINCT ON (isin) isin, symbol FROM symbol_history
WHERE isin = ANY(%s)
ORDER BY isin, (exchange = 'NSE') DESC, (valid_to IS NULL) DESC, valid_from DESC
"""

_STATE_SQL: Final = """
SELECT book_state, expected_book FROM paper_session WHERE book_id = %s AND trading_date = %s
"""

_S0_SQL: Final = """
SELECT max(trading_date) FROM decision_journal
WHERE case_id = %s AND payload->>'event' = 'M17_S0'
"""

_MARKS_SQL: Final = """
SELECT case_id, trading_date, payload->>'record' FROM decision_journal
WHERE payload->>'event' = 'BOOK_MARK' AND trading_date >= %s AND case_id = ANY(%s)
ORDER BY id
"""

_DESK_SQL: Final = """
SELECT trading_date, outcome, recorded_at FROM paper_session
WHERE book_id = %s ORDER BY trading_date DESC LIMIT 1
"""

_NIGHT_SQL: Final = """
SELECT case_id, payload->>'event', decision, count(*) FROM decision_journal
WHERE trading_date = %s AND case_id = ANY(%s)
GROUP BY 1, 2, 3
"""

_FALLBACK_SQL: Final = """
SELECT payload->>'fallback_from', count(*) FROM decision_journal
WHERE trading_date = %s AND case_id = ANY(%s) AND payload->>'event' = 'FM_CALL'
  AND payload ? 'fallback_from'
GROUP BY 1
"""

_DIGEST_FALLBACK_SQL: Final = """
SELECT count(*) FROM commons_filing_digest WHERE trading_date = %s AND provider <> %s
"""

_GAPS_SQL: Final = """
SELECT payload->>'missing' FROM decision_journal
WHERE trading_date = %s AND case_id IS NULL AND payload->>'event' = 'M17_DATA_GAPS'
  AND coalesce(payload->>'stream', '') NOT LIKE '%%dry%%'
ORDER BY id DESC LIMIT 1
"""

_JOBS_SQL: Final = """
SELECT DISTINCT ON (job_name) job_name, state FROM job_run
WHERE started_at >= %s AND started_at < %s AND state NOT IN ('RUNNING', 'SKIPPED_LOCKED')
ORDER BY job_name, started_at DESC
"""

_OPEN_ALERTS_SQL: Final = """
SELECT title, severity, opened_at FROM alert_condition
WHERE resolved_at IS NULL AND trigger <> 'owner_event'
ORDER BY opened_at
"""


def _symbols(conn: Connection, isins: Sequence[str]) -> dict[str, str]:
    if not isins:
        return {}
    return {str(i): str(s) for i, s in conn.execute(_SYMBOLS_SQL, (sorted(isins),)).fetchall()}


def read_paper_activity(
    conn: Connection, prices: SessionPrices, book_ids: Sequence[str]
) -> list[tuple[PaperActivity, datetime]]:
    """Each paper book's newest session, with its orders valued at that session's close."""
    out: list[tuple[PaperActivity, datetime]] = []
    for book_id, on, outcome, reason, rebalanced, orders, recorded_at in conn.execute(
        _PAPER_ROWS_SQL, (list(book_ids),)
    ).fetchall():
        docs = [o for o in (orders or []) if isinstance(o, Mapping)]
        isins = sorted({str(o.get("isin")) for o in docs})
        closes = prices.closes(on, isins) if isins else {}
        names = _symbols(conn, isins)
        parsed = []
        for doc in docs:
            isin = str(doc.get("isin"))
            quantity = Decimal(str(doc.get("quantity") or 0))
            close = closes.get(isin)
            parsed.append(
                PaperOrder(
                    side=str(doc.get("side")),
                    name=names.get(isin, isin),
                    value=None if close is None else quantity * close,
                )
            )
        # The session's own book is its holdings at the close, before tonight's orders fill.
        state = conn.execute(_STATE_SQL, (book_id, on)).fetchone()
        was_in_cash = True if state is None or state[0] is None else _holds_nothing(state)
        out.append(
            (
                PaperActivity(
                    book_id=str(book_id),
                    session=on,
                    outcome=str(outcome),
                    reason=str(reason),
                    rebalanced=bool(rebalanced),
                    orders=tuple(parsed),
                    next_session=prices.next_session(on),
                    was_in_cash=was_in_cash,
                ),
                recorded_at,
            )
        )
    return out


def _holds_nothing(row: Sequence[Any]) -> bool:
    """Whether a session's book (accounting side first, else the broker's) holds no stock."""
    state, expected = row
    if isinstance(expected, Mapping):
        return not any(int(p.get("quantity") or 0) > 0 for p in expected.get("positions") or [])
    broker = state.get("broker") if isinstance(state, Mapping) else None
    if not isinstance(broker, Mapping):
        return True
    lots = [*(broker.get("holdings") or []), *(broker.get("pending") or [])]
    return not any(int(lot.get("quantity") or 0) > 0 for lot in lots)


def tri_level(data_root: Path, slug: str, on: date) -> Decimal | None:
    """The published TRI of index `slug` on `on`, from L1 `benchmark_tri`; None when absent."""
    from dataplatform.store.l2 import open_connection

    folder = data_root / "L1" / "benchmark_tri" / f"date={on.isoformat()}"
    files = sorted(folder.glob(f"{slug}.*.parquet")) if folder.is_dir() else []
    if not files:
        return None
    con = open_connection()
    try:
        row = con.execute(
            "SELECT tri_value FROM read_parquet($path) WHERE tri_value IS NOT NULL LIMIT 1",
            {"path": str(files[0])},
        ).fetchone()
    finally:
        con.close()
    return None if row is None else Decimal(str(row[0]))


def read_paper_figures(
    conn: Connection, prices: SessionPrices, data_root: Path, book_ids: Sequence[str]
) -> tuple[list[PaperFigures], list[BookMarks]]:
    """Each paper book's newest figures, and its marks for the loss limits."""
    from dataplatform.status.queries_portfolio_paper import paper_nav_history

    figures: list[PaperFigures] = []
    marks: list[BookMarks] = []
    for book_id in book_ids:
        history = paper_nav_history(conn, book_id, prices)
        if not history:
            continue
        first, latest = history[0], history[-1]
        opening = first.cash
        known = [(m.trading_date, m.nav) for m in history if m.nav is not None]
        marks.append(BookMarks(book_id, opening, tuple((d, n) for d, n in known)))
        previous = history[-2] if len(history) > 1 else None
        nav = latest.nav
        tri_start = tri_level(data_root, "nifty50", first.trading_date)
        tri_end = tri_level(data_root, "nifty50", latest.trading_date)
        figures.append(
            PaperFigures(
                book_id=book_id,
                session=latest.trading_date,
                nav=nav,
                invested_pct=None if nav is None or nav == 0 else (nav - latest.cash) / nav * 100,
                day_change_pct=None
                if nav is None or previous is None or previous.nav is None
                else _change(nav, previous.nav),
                since_start_pct=None if nav is None else _change(nav, opening),
                nifty_tri_pct=None
                if tri_start is None or tri_end is None
                else _change(tri_end, tri_start),
                first_session=first.trading_date,
            )
        )
    return figures, marks


def _m17_book_ids(conn: Connection) -> tuple[list[str], dict[str, Decimal]]:
    """The live M17 books (roster order) and their opening capital, from the roster."""
    from dataplatform.status.queries import read_m17_roster
    from dataplatform.status.queries_desk import read_mandates

    mandates = read_mandates(read_m17_roster())
    return [m.book_id for m in mandates], {m.book_id: m.opening_capital_inr for m in mandates}


def read_m17_marks(conn: Connection) -> tuple[date | None, list[BookMarks]]:
    """S0 and every live M17 book's BOOK_MARK NAVs since S0 (managers, controls, the bench)."""
    import json

    books, capital = _m17_book_ids(conn)
    row = conn.execute(_S0_SQL, (BENCH,)).fetchone()
    s0 = None if row is None else row[0]
    if s0 is None:
        return None, []
    navs: dict[str, dict[date, Decimal]] = {}
    for case_id, on, record in conn.execute(_MARKS_SQL, (s0, books)).fetchall():
        try:
            nav = Decimal(str(json.loads(record)["nav"]))
        except (TypeError, ValueError, KeyError):
            continue
        navs.setdefault(str(case_id), {})[on] = nav
    out = []
    for book_id in books:
        series = navs.get(book_id)
        if not series:
            continue
        out.append(BookMarks(book_id, capital[book_id], tuple(sorted(series.items()))))
    return s0, out


def m17_returns(marks: Sequence[BookMarks]) -> list[BookReturn]:
    """Manager books and the bench since S0; each manager book with its control's return."""
    by_id = {m.book_id: m for m in marks}

    def ret(book: BookMarks | None) -> Decimal | None:
        if book is None or not book.marks:
            return None
        return _change(book.marks[-1][1], book.opening)

    out = []
    for book in marks:
        if not (book.book_id.startswith("FM-") or book.book_id == BENCH) or not book.marks:
            continue
        out.append(
            BookReturn(
                book_id=book.book_id,
                nav=book.marks[-1][1],
                return_pct=ret(book),
                control_return_pct=None
                if book.book_id == BENCH
                else ret(by_id.get(f"CTRL-{book.book_id}")),
            )
        )
    return out


def read_m17_night(conn: Connection, *, primary_provider: str) -> M17Night | None:
    """The newest night the live desk recorded, with each manager book's counts."""
    desk = conn.execute(_DESK_SQL, (M17_DESK,)).fetchone()
    if desk is None:
        return None
    session, outcome, recorded_at = desk
    books, _ = _m17_book_ids(conn)
    managers = [b for b in books if b.startswith("FM-")]
    counts: dict[str, dict[str, int]] = {b: Counter() for b in managers}
    for case_id, event, decision, n in conn.execute(_NIGHT_SQL, (session, managers)).fetchall():
        c = counts.setdefault(str(case_id), Counter())
        if event in _STAGED_EVENTS and decision in ("BUY", "SELL"):
            c[decision] += int(n)
        if decision == "RAIL_BLOCK" or event == "FM_DECISION_REFUSED":
            c["refused"] += int(n)
        if event in _MANAGER_ERRORS:
            c["errors"] += int(n)
    fallback = {
        str(provider): int(n)
        for provider, n in conn.execute(_FALLBACK_SQL, (session, managers)).fetchall()
    }
    digest_row = conn.execute(_DIGEST_FALLBACK_SQL, (session, primary_provider)).fetchone()
    gaps_row = conn.execute(_GAPS_SQL, (session,)).fetchone()
    return M17Night(
        session=session,
        outcome=str(outcome),
        recorded_at=recorded_at,
        books={
            b: BookNight(buys=c["BUY"], sells=c["SELL"], refused=c["refused"], errors=c["errors"])
            for b, c in counts.items()
        },
        fallback_calls=fallback,
        digest_fallbacks=0 if digest_row is None else int(digest_row[0]),
        data_gaps=None if gaps_row is None or not gaps_row[0] else str(gaps_row[0]),
    )


def read_switches(conn: Connection, data_root: Path, now: datetime) -> list[SwitchState]:
    """Every kill switch `/status/paper` knows of, as plain states."""
    from dataplatform.status.queries import read_paper_status

    out = []
    for book in read_paper_status(conn, data_root=data_root, as_of=now).books:
        switch = book.kill_switch
        out.append(
            SwitchState(
                account=book.book_id,
                tripped=switch.tripped,
                source=switch.source,
                reason=switch.reason,
                tripped_at=switch.tripped_at,
            )
        )
    return out


def read_reset_note(data_root: Path) -> Callable[[str], str | None]:
    def note(account: str) -> str | None:
        try:
            from execution.kill_switch import KillSwitch, kill_switch_path

            reset = KillSwitch(kill_switch_path(data_root, account)).state.last_reset
        except Exception:  # a note is a courtesy; the reset message goes out without one
            return None
        return None if reset is None else getattr(reset, "note", None)

    return note


def read_jobs_day(conn: Connection, on: date) -> JobsDay:
    start = datetime.combine(on, datetime.min.time(), tzinfo=IST)
    rows = conn.execute(_JOBS_SQL, (start, start + timedelta(days=1))).fetchall()
    failed = tuple(sorted(str(name) for name, state in rows if state != "SUCCEEDED"))
    return JobsDay(on=on, ok=len(rows) - len(failed), failed=failed)


def read_open_alerts(conn: Connection) -> list[OpenAlert]:
    return [
        OpenAlert(title=str(t), severity=str(s), opened_at=o)
        for t, s, o in conn.execute(_OPEN_ALERTS_SQL).fetchall()
    ]


def disk_used_pct(path: Path) -> int:
    usage = shutil.disk_usage(path)
    return round(usage.used / usage.total * 100)


# ── dataset freshness (the lake) ─────────────────────────────────────────────────────────────

_PARTITION: Final = re.compile(r"^date=(\d{4}-\d{2}-\d{2})$")


def _partitions(folder: Path) -> list[date]:
    """The `date=` partitions of a dataset, newest first."""
    if not folder.is_dir():
        return []
    days = [
        date.fromisoformat(m.group(1)) for p in folder.iterdir() if (m := _PARTITION.match(p.name))
    ]
    return sorted(days, reverse=True)


def _newest_with(folder: Path, pattern: str) -> date | None:
    for on in _partitions(folder):
        if any((folder / f"date={on.isoformat()}").glob(pattern)):
            return on
    return None


def _newest_vix(folder: Path, look: int = 30) -> date | None:
    from dataplatform.store.l2 import open_connection

    con = open_connection()
    try:
        for on in _partitions(folder)[:look]:
            files = sorted((folder / f"date={on.isoformat()}").glob("*.parquet"))
            for path in files:
                row = con.execute(
                    "SELECT count(*) FROM read_parquet($path) "
                    "WHERE series_id = 'IN.NSE.INDIA_VIX.CLOSE'",
                    {"path": str(path)},
                ).fetchone()
                if row is not None and row[0]:
                    return on
    finally:
        con.close()
    return None


def _newest_nse_prices(folder: Path, look: int = 30) -> date | None:
    from dataplatform.store.l2 import open_connection

    con = open_connection()
    try:
        for on in _partitions(folder)[:look]:
            files = sorted((folder / f"date={on.isoformat()}").glob("*.parquet"))
            for path in files:
                row = con.execute(
                    "SELECT count(*) FROM read_parquet($path) WHERE exchange = 'NSE'",
                    {"path": str(path)},
                ).fetchone()
                if row is not None and row[0]:
                    return on
    finally:
        con.close()
    return None


def lake_freshness(data_root: Path) -> list[tuple[str, str, date | None]]:
    """(key, owner's name, newest date) of each key dataset in the lake."""
    l1 = data_root / "L1"
    tri = l1 / "benchmark_tri"
    mid = _newest_with(tri, "niftymidcap150.*.parquet")
    small = _newest_with(tri, "niftysmallcap250.*.parquet")
    midsmall = None if mid is None or small is None else min(mid, small)
    return [
        ("tri_nifty50", "NIFTY 50 total-return index", _newest_with(tri, "nifty50.*.parquet")),
        ("tri_nifty500", "NIFTY 500 total-return index", _newest_with(tri, "nifty500.*.parquet")),
        ("tri_midsmall", "Midcap 150 / Smallcap 250 total-return index", midsmall),
        ("india_vix", "India VIX", _newest_vix(l1 / "macro_series")),
        ("eod_prices", "end-of-day share prices", _newest_nse_prices(l1 / "prices_raw")),
        (
            "announcements",
            "NSE company announcements",
            _newest_with(l1 / "announcements", "*.parquet"),
        ),
        ("shareholding", "shareholding patterns", _newest_with(l1 / "shareholding", "*.parquet")),
        ("board_meetings", "board-meeting dates", _newest_with(l1 / "board_meetings", "*.parquet")),
    ]


def sessions_behind(
    latest: date, today: date, sessions: Callable[[date, date], Sequence[date]]
) -> int:
    """Trading sessions after `latest` and before `today` — today's own data lands tonight."""
    start, end = latest + timedelta(days=1), today - timedelta(days=1)
    if end < start:
        return 0
    return len(sessions(start, end))


def freshness(
    lake: Iterable[tuple[str, str, date | None]],
    today: date,
    sessions: Callable[[date, date], Sequence[date]],
    last_polls: Mapping[str, date | None],
) -> list[DatasetFreshness]:
    """Each lake dataset's freshness; a `POLLED_DATASETS` one measured from its last good poll.

    What it does: for an ordinary dataset, `latest` is its newest partition. For a polled one that
    holds any data at all, `latest` is the newest PUBLISHED poll date in `last_polls` (None when
    the poll has never succeeded) — the filing dates say when companies filed, the poll says
    whether we are still looking.
    What it never does: call a polled dataset fresh on the strength of its filings alone, or call
    an empty one fresh on the strength of its poll: with no partition there is no data to be
    current, and it stays "no data at all".
    """
    out = []
    for key, name, newest in lake:
        poll_source = POLLED_DATASETS.get(key)
        polled = poll_source is not None and newest is not None
        latest = last_polls.get(poll_source) if polled and poll_source is not None else newest
        behind = None if latest is None else sessions_behind(latest, today, sessions)
        out.append(DatasetFreshness(key, name, latest, behind, KNOWN_GAPS.get(key), polled))
    return out


def read_last_polls(conn: Connection) -> dict[str, date | None]:
    """The newest PUBLISHED logical date of each `POLLED_DATASETS` source (None if never)."""
    sources = sorted(set(POLLED_DATASETS.values()))
    rows = conn.execute(
        "SELECT source, max(logical_date) FROM sync_state "
        "WHERE source = ANY(%s) AND unit = '' AND state = 'PUBLISHED' GROUP BY source",
        (sources,),
    ).fetchall()
    found = {str(row[0]): row[1] for row in rows}
    return {source: found.get(source) for source in sources}


def read_freshness(
    data_root: Path, today: date, last_polls: Mapping[str, date | None]
) -> list[DatasetFreshness]:
    from dataplatform.ingest.calendar import trading_calendar

    calendar = trading_calendar()
    return freshness(
        lake_freshness(data_root),
        today,
        lambda a, b: list(calendar.expected_sessions(a, b)),
        last_polls,
    )


# ── the jobs ─────────────────────────────────────────────────────────────────────────────────


def _fresh(recorded_at: datetime, now: datetime, hours: int) -> bool:
    return now - recorded_at <= timedelta(hours=hours)


def run_owner_alerts(
    conn: Connection, *, settings: Settings, clock: Clock, alerter: Alerter
) -> TickReport:
    """One `owner_alerts` tick: the states (kill switches, loss limits, stale data) reconciled,
    and the fresh news (paper activity, the M17 night, a degraded night, a one-day drop) sent
    once. Each part is isolated; errors are collected in the report."""
    from dataplatform.status.queries_portfolio import LakeSessionPrices
    from dataplatform.status.queries_portfolio_paper import MemoPrices

    now = clock.now()
    data_root = settings.data_root
    prices = MemoPrices(LakeSessionPrices(data_root))
    ledger = ConditionLedger(conn)
    report = TickReport()
    max_age = settings.owner_alert_event_max_age_hours

    def guarded(what: str, step: Callable[[], None]) -> None:
        try:
            step()
        except Exception as error:
            report.errors.append(f"{what}: {type(error).__name__}")
            log.error(
                "owner_alerts.step_failed",
                step=what,
                error_type=type(error).__name__,
                error=redact(str(error)),
            )

    def once(key: str, message: Message) -> None:
        send_once(
            ledger=ledger,
            alerter=alerter,
            trigger=Trigger.OWNER_EVENT,
            key=key,
            severity=message.severity,
            title=message.title,
            body=message.body,
            at=now,
            report=report,
        )

    books: dict[str, BookMarks] = {}
    datasets: dict[str, DatasetFreshness] = {}

    def loss_marks() -> list[AlertCondition]:
        _, paper_marks = read_paper_figures(conn, prices, data_root, list(PAPER_BOOKS))
        _, m17 = read_m17_marks(conn)
        managers = [m for m in m17 if m.book_id.startswith("FM-")]
        for book in [*paper_marks, *managers]:
            books[book.book_id] = book
        return drawdown_conditions(
            books.values(),
            threshold=settings.owner_alert_drawdown_pct,
            step=settings.owner_alert_drawdown_step_pp,
        )

    def stale() -> list[AlertCondition]:
        for dataset in read_freshness(data_root, clock.today(), read_last_polls(conn)):
            datasets[dataset.key] = dataset
        return stale_data_conditions(
            datasets.values(), max_sessions=settings.owner_alert_stale_sessions
        )

    evaluators: list[TriggerEvaluator] = [
        (
            Trigger.OWNER_KILL_SWITCH,
            lambda: kill_switch_conditions(read_switches(conn, data_root, now)),
        ),
        (Trigger.OWNER_LOSS, loss_marks),
        (Trigger.OWNER_STALE_DATA, stale),
    ]
    resolutions: dict[Trigger, ResolutionFormatter] = {
        Trigger.OWNER_KILL_SWITCH: kill_switch_resolution(read_reset_note(data_root)),
        Trigger.OWNER_LOSS: drawdown_resolution(books, threshold=settings.owner_alert_drawdown_pct),
        Trigger.OWNER_STALE_DATA: stale_resolution(datasets),
    }
    states = tick(evaluators, ledger=ledger, alerter=alerter, at=now, resolutions=resolutions)
    report.opened += states.opened
    report.resolved += states.resolved
    report.still_open += states.still_open
    report.errors += states.errors

    def drops() -> None:
        for book in books.values():
            if not book.marks or (clock.today() - book.marks[-1][0]).days > 4:
                continue
            message = session_drop_message(book, threshold=settings.owner_alert_session_drop_pct)
            if message is not None:
                once(f"{_KEY}drop:{book.book_id}:{book.marks[-1][0].isoformat()}", message)

    skipped: list[PaperActivity] = []

    def papers() -> None:
        for activity, recorded_at in read_paper_activity(conn, prices, list(PAPER_BOOKS)):
            if activity.outcome == "SKIPPED_DATA_RED":
                skipped.append(activity)
            if not _fresh(recorded_at, now, max_age):
                continue
            message = paper_activity_message(activity)
            if message is not None:
                once(f"{_KEY}paper:{activity.book_id}:{activity.session.isoformat()}", message)

    def m17() -> None:
        night = read_m17_night(conn, primary_provider=settings.m17_llm_provider.value)
        if night is None or not _fresh(night.recorded_at, now, max_age):
            return
        s0, marks = read_m17_marks(conn)
        once(
            f"{_KEY}m17-night:{night.session.isoformat()}",
            m17_night_message(night, m17_returns(marks), s0),
        )
        papers_that_night = [p for p in skipped if p.session == night.session]
        degraded = degraded_message(night.session, night, papers_that_night)
        if degraded is not None:
            once(f"{_KEY}degraded:{night.session.isoformat()}", degraded)

    guarded("one-day drops", drops)
    guarded("paper activity", papers)
    guarded("M17 night", m17)
    log.info(
        "owner_alerts.tick",
        opened=report.opened,
        resolved=report.resolved,
        still_open=len(report.still_open),
        errors=len(report.errors),
    )
    return report


def gather_summary(conn: Connection, *, settings: Settings, clock: Clock) -> SummaryInputs:
    """Read everything the morning summary needs; a part that fails to read is None."""
    from dataplatform.status.queries_portfolio import LakeSessionPrices
    from dataplatform.status.queries_portfolio_paper import MemoPrices

    today = clock.today()
    now = clock.now()
    prices = MemoPrices(LakeSessionPrices(settings.data_root))
    unreadable: list[str] = []

    def attempt(what: str, read: Callable[[], Any]) -> Any:
        try:
            return read()
        except Exception as error:
            log.error(
                "owner_alerts.summary_read_failed",
                part=what,
                error_type=type(error).__name__,
                error=redact(str(error)),
            )
            unreadable.append(what)
            return None

    jobs = attempt("the job history", lambda: read_jobs_day(conn, today - timedelta(days=1)))
    night = attempt(
        "the AI fund managers' night",
        lambda: read_m17_night(conn, primary_provider=settings.m17_llm_provider.value),
    )
    m17 = attempt("the AI books", lambda: read_m17_marks(conn))
    paper = attempt(
        "the momentum books",
        lambda: read_paper_figures(conn, prices, settings.data_root, list(PAPER_BOOKS)),
    )
    alerts = attempt("the open alerts", lambda: read_open_alerts(conn))
    disk = attempt("the disk", lambda: disk_used_pct(settings.data_root))
    switches = (
        attempt("the kill switches", lambda: read_switches(conn, settings.data_root, now)) or []
    )
    loss_rows = (
        attempt(
            "the loss limits",
            lambda: conn.execute(
                "SELECT title FROM alert_condition "
                "WHERE trigger = 'owner_loss' AND resolved_at IS NULL ORDER BY opened_at"
            ).fetchall(),
        )
        or []
    )
    return SummaryInputs(
        today=today,
        jobs=jobs,
        night=night,
        m17_returns=None if m17 is None else m17_returns(m17[1]),
        s0=None if m17 is None else m17[0],
        papers=None if paper is None else paper[0],
        open_alerts=alerts,
        disk_pct=disk,
        tripped=[s.account for s in switches if s.tripped is not False],
        loss_limits=[str(r[0]) for r in loss_rows][:3],
        unreadable=unreadable,
    )


def _job_wiring(context: JobContext) -> tuple[Settings, Clock, Alerter]:
    from dataplatform.alerts import build_alerter

    return context.settings, context.clock, build_alerter(context.settings, clock=context.clock)


def run_owner_alerts_job(context: JobContext) -> None:
    """The `owner_alerts` scheduler job body; raises when a part could not be evaluated or sent."""
    from dataplatform.store.db import connection

    settings, clock, alerter = _job_wiring(context)
    with connection(settings, autocommit=True) as conn:
        report = run_owner_alerts(conn, settings=settings, clock=clock, alerter=alerter)
    if not report.ok:
        raise TriggerEvaluationError("; ".join(report.errors))


def run_morning_summary(
    conn: Connection,
    *,
    settings: Settings,
    clock: Clock,
    alerter: Alerter,
    title: str | None = None,
    record: bool = True,
) -> tuple[Message, TickReport, list[str]]:
    """Build and send the summary. Recorded once per day unless `record` is False (a sample)."""
    inputs = gather_summary(conn, settings=settings, clock=clock)
    unreadable = list(inputs.unreadable)
    message = build_morning_summary(
        inputs, disk_warn_pct=settings.owner_alert_disk_warn_pct, title=title
    )
    report = TickReport()
    if record:
        send_once(
            ledger=ConditionLedger(conn),
            alerter=alerter,
            trigger=Trigger.OWNER_EVENT,
            key=f"{_KEY}summary:{clock.today().isoformat()}",
            severity=message.severity,
            title=message.title,
            body=message.body,
            at=clock.now(),
            report=report,
        )
    else:
        alerter.send(
            message.severity,
            message.title,
            message.body,
            f"{_KEY}summary-sample:{clock.now().isoformat()}",
        )
    return message, report, unreadable


def run_morning_summary_job(context: JobContext) -> None:
    """The `owner_morning_summary` job body: the summary is sent even when a part could not be
    read (it says so), and the run then fails so `failure_alerts` names what broke."""
    from dataplatform.store.db import connection

    settings, clock, alerter = _job_wiring(context)
    with connection(settings, autocommit=True) as conn:
        _, report, unreadable = run_morning_summary(
            conn, settings=settings, clock=clock, alerter=alerter
        )
    errors = [*report.errors, *(f"{what} could not be read" for what in unreadable)]
    if errors:
        raise TriggerEvaluationError("; ".join(errors))


def run_weekly_decisions_job(context: JobContext) -> None:
    """The `owner_weekly_decisions` job body: Monday's list of decisions waiting on the owner."""
    from dataplatform.store.db import connection

    settings, clock, alerter = _job_wiring(context)
    message = pending_decisions_message(settings.owner_pending_decisions_file, clock.today())
    if message is None:
        log.info("owner_alerts.no_pending_decisions")
        return
    report = TickReport()
    with connection(settings, autocommit=True) as conn:
        send_once(
            ledger=ConditionLedger(conn),
            alerter=alerter,
            trigger=Trigger.OWNER_EVENT,
            key=f"{_KEY}decisions:{clock.today().isoformat()}",
            severity=message.severity,
            title=message.title,
            body=message.body,
            at=clock.now(),
            report=report,
        )
    if not report.ok:
        raise TriggerEvaluationError("; ".join(report.errors))


# ── the CLI: a preview that sends nothing, and the one sample the deploy sends ──────────────


def main(argv: Sequence[str] | None = None) -> int:
    """`preview` prints the morning summary without sending it; `sample` sends it once, titled
    "Sample morning summary", without recording it as the day's summary."""
    from dataplatform.alerts import build_alerter
    from dataplatform.clock import SystemClock
    from dataplatform.config import get_settings
    from dataplatform.store.db import connection

    parser = argparse.ArgumentParser(prog="python -m dataplatform.owner_alerts")
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("preview", help="print the morning summary; send nothing")
    sample = sub.add_parser("sample", help="send the morning summary once as a sample")
    sample.add_argument("--title", default="Sample morning summary")
    args = parser.parse_args(argv)

    settings = get_settings()
    clock = SystemClock()
    with connection(settings, autocommit=True) as conn:
        if args.command == "preview":
            inputs = gather_summary(conn, settings=settings, clock=clock)
            message = build_morning_summary(
                inputs, disk_warn_pct=settings.owner_alert_disk_warn_pct
            )
            print(f"[{message.severity.value.upper()}] {message.title}\n\n{message.body}")
            if inputs.unreadable:
                print(f"\n(unreadable: {', '.join(inputs.unreadable)})", file=sys.stderr)
            return 0
        alerter = build_alerter(settings, clock=clock)
        message, _, unreadable = run_morning_summary(
            conn, settings=settings, clock=clock, alerter=alerter, title=args.title, record=False
        )
    print(f"sent: {message.title} ({len(message.body)} chars)")
    return 0 if not unreadable else 1


if __name__ == "__main__":
    from dataplatform.logging import configure_logging

    configure_logging()
    raise SystemExit(main())
