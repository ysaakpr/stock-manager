"""One-time, journaled re-queues of named XBRL filings, consumed by the scheduled forward job.

The forward job (`fundamentals_forward`) retries a failed filing only while its index window is
inside the 7-day re-read horizon — D28's retry window. A filing that failed for a reason since
fixed, or one the owner wants checked once more, is outside that horizon for good: nothing in the
platform will ever ask for it again, and asking by hand would be a fetch outside the scheduler
(AGENTIC_CONTEXT §8). This module is the sanctioned way back in: a re-queue is a checked-in,
reviewed list of filings (`fundamentals_requeue.yaml`), and the *next scheduled run* of the job on
or after its `not_before` date drives each one once, under the job's own host leases, rate limiter
and deadline — no other code path fetches.

What makes it bounded, one-time and journaled:

* **Bounded.** A re-queue names its filings explicitly, at most `MAX_FILINGS` of them, and the file
  as a whole at most `MAX_TOTAL_FILINGS` — far under B1's 200-request campaign threshold. Each item
  names the stored index page its entry is re-read from (no index request), so the only request an
  item can make is its own document's, once. The one exception is a `reuse_only` re-queue the owner
  approved by id (`RAISED_BOUNDS`): it may name more, because it cannot make a request at all —
  an item whose stored document is missing, empty or truncated is journaled `FAILED` instead of
  fetched — and it counts against its own bound, not the file's.
* **One-time.** Every item's outcome is written to `fundamentals_requeue_log`, keyed
  `(requeue_id, unit, filing_date)`; an item with a row is never driven again, whatever happened.
  An item the run did not reach (the deadline, a 403 park) has no row and rides with the next run.
* **Journaled.** The row records the outcome, the sync_state error it left, and when — the record
  an operator reads to see what the re-check found. It is append-only by use: nothing updates or
  deletes a row.

Two actions. `refetch` drives the filing through the runner's normal per-filing path — reuse what
L0 holds, re-download a stored document that is empty or truncated beside it, parse, publish or
fail — so the outcome is exactly what a nightly retry would have produced. `record_evidence` makes
**no request**: it re-reads the filing's index entry from L0 and, when the index states the XBRL
file as zero bytes and the row's failure is that document's 404, re-records the failure with the
index's statement appended (`fundamentals_backfill.zero_byte_evidence`) — the evidence the D31
accepted deviation matches, for failures recorded before the runner started writing it.
"""

from __future__ import annotations

from collections.abc import Callable, Sequence
from dataclasses import dataclass
from datetime import date
from enum import StrEnum
from pathlib import Path
from types import MappingProxyType
from typing import Final

import yaml
from pydantic import BaseModel, ConfigDict, Field, ValidationError, model_validator

from dataplatform.clock import Clock
from dataplatform.logging import get_logger
from dataplatform.store.db import Connection

__all__ = [
    "MAX_FILINGS",
    "MAX_TOTAL_FILINGS",
    "RAISED_BOUNDS",
    "REQUEUE_PATH",
    "Journal",
    "RequeueAction",
    "RequeueError",
    "RequeueItem",
    "RequeueOutcome",
    "journal_to",
    "load_requeues",
    "pending_items",
]

_LOG = get_logger(__name__)

#: The checked-in re-queue register, beside this module.
REQUEUE_PATH: Final = Path(__file__).with_name("fundamentals_requeue.yaml")

#: Filings one re-queue may name, and the whole file. A re-queue is a re-check of a known handful,
#: never a campaign: anything near B1's 200 requests to one host is a human go, not a YAML edit.
MAX_FILINGS: Final = 50
MAX_TOTAL_FILINGS: Final = 100

#: Owner-approved, one-off raised bounds, by re-queue id — each for one reviewed list, never a
#: standing allowance. Only a `reuse_only` re-queue may carry one: the bounds above exist to cap
#: requests to NSE, and a reuse-only item makes none (an item without a complete stored document
#: is journaled FAILED, not fetched). Such a re-queue counts against its own bound here, not
#: against `MAX_TOTAL_FILINGS`.
RAISED_BOUNDS: Final = MappingProxyType(
    {
        # 2026-11-02 (owner): the class E (multi-entity BSE code) and F1 (symbol typo, ISIN
        # agrees) parse failures, re-parsed from their complete L0 documents in one night.
        "xbrl-identity-e-f1-2026-11-03": 205,
    }
)

#: The only index whose pages an item may be re-read from: the integrated feed (2025-03 on), the one
#: the forward job drives. Its entries are what the runner's per-filing path takes.
_INDEX_SOURCE: Final = "nse_integrated_filing_index"
_FILING_SOURCE: Final = "nse_xbrl_filing"
_ID: Final = r"^[a-z0-9][a-z0-9-]{2,79}$"
_UNIT: Final = r"^IF\d+(?:-FY)?$"


class RequeueError(ValueError):
    """The re-queue register is malformed; the job fails loudly rather than guess."""


class RequeueAction(StrEnum):
    """What the job does with one item."""

    REFETCH = "refetch"
    """Drive the filing once through the normal per-filing path (may make one request)."""
    RECORD_EVIDENCE = "record_evidence"
    """Re-record a 404 with the index's zero-byte statement; never makes a request."""


class RequeueOutcome(StrEnum):
    """What happened to one item — the journal's `outcome` column."""

    PUBLISHED = "PUBLISHED"
    FAILED = "FAILED"
    ALREADY_PUBLISHED = "ALREADY_PUBLISHED"
    ATTEMPTED_THIS_RUN = "ATTEMPTED_THIS_RUN"
    NOT_IN_INDEX = "NOT_IN_INDEX"
    EVIDENCE_RECORDED = "EVIDENCE_RECORDED"
    NO_EVIDENCE = "NO_EVIDENCE"


class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class _Filing(_Strict):
    unit: str = Field(pattern=_UNIT, description="the nse_xbrl_filing unit: IF<seq>[-FY]")
    filing_date: date
    index: str = Field(
        min_length=1, description="the L0 key of the index page holding its entry: source/date/name"
    )
    note: str | None = Field(default=None, max_length=200)

    @model_validator(mode="after")
    def _index_key(self) -> _Filing:
        source, _, rest = self.index.partition("/")
        day, _, filename = rest.partition("/")
        if source != _INDEX_SOURCE or not filename or "/" in filename:
            raise ValueError(f"index {self.index!r} is not {_INDEX_SOURCE}/<YYYY-MM-DD>/<file>")
        date.fromisoformat(day)
        return self


class _Requeue(_Strict):
    id: str = Field(pattern=_ID)
    action: RequeueAction
    not_before: date = Field(description="the first day a scheduled run may consume it")
    basis: str = Field(min_length=1, max_length=400)
    reuse_only: bool = Field(
        default=False,
        description="refetch from the stored document only; never request (FAILED if none)",
    )
    filings: tuple[_Filing, ...] = Field(min_length=1)

    @property
    def raised(self) -> bool:
        """Whether this re-queue runs under an owner-approved `RAISED_BOUNDS` entry."""
        return self.id in RAISED_BOUNDS

    @model_validator(mode="after")
    def _unique(self) -> _Requeue:
        keys = [(f.unit, f.filing_date) for f in self.filings]
        if len(keys) != len(set(keys)):
            raise ValueError(f"re-queue {self.id} names a filing twice")
        if self.reuse_only and self.action is not RequeueAction.REFETCH:
            raise ValueError(f"re-queue {self.id}: reuse_only applies to refetch only")
        if self.raised and not self.reuse_only:
            raise ValueError(
                f"re-queue {self.id} has a raised bound but is not reuse_only; a raised bound is "
                "for a list that cannot make a request"
            )
        bound = RAISED_BOUNDS.get(self.id, MAX_FILINGS)
        if len(self.filings) > bound:
            raise ValueError(
                f"re-queue {self.id} names {len(self.filings)} filings; its bound is {bound}"
            )
        return self


class _Register(_Strict):
    version: int = Field(ge=1, le=1)
    requeues: tuple[_Requeue, ...] = ()

    @model_validator(mode="after")
    def _bounded(self) -> _Register:
        ids = [r.id for r in self.requeues]
        if len(ids) != len(set(ids)):
            raise ValueError("two re-queues share an id")
        total = sum(len(r.filings) for r in self.requeues if not r.raised)
        if total > MAX_TOTAL_FILINGS:
            raise ValueError(
                f"{total} filings re-queued; the register's bound is {MAX_TOTAL_FILINGS}"
            )
        return self


@dataclass(frozen=True, slots=True)
class RequeueItem:
    """One filing of one re-queue, as the runner drives it."""

    requeue_id: str
    action: RequeueAction
    unit: str
    filing_date: date
    index_key: str
    reuse_only: bool = False
    """Never request: drive the filing only from a complete stored document (`refetch` only)."""

    @property
    def state_source(self) -> str:
        """The filing's `sync_state` source: `nse_xbrl_filing/<unit>`."""
        return f"{_FILING_SOURCE}/{self.unit}"

    @property
    def index_ref(self) -> tuple[str, date, str]:
        """`(source, logical_date, filename)` of the index page — `L0Store.ref_for`'s arguments."""
        source, rest = self.index_key.split("/", 1)
        day, filename = rest.split("/", 1)
        return source, date.fromisoformat(day), filename


def load_requeues(path: Path = REQUEUE_PATH) -> tuple[RequeueItem, ...]:
    """Every item of every re-queue in the register, validated. Raises `RequeueError`."""
    return tuple(_items(_register(path).requeues))


def _register(path: Path) -> _Register:
    try:
        raw = yaml.safe_load(path.read_text(encoding="utf-8"))
        return _Register.model_validate(raw)
    except (OSError, yaml.YAMLError, ValidationError) as exc:
        raise RequeueError(f"{path.name}: {exc}") from exc


def _items(requeues: Sequence[_Requeue], *, today: date | None = None) -> list[RequeueItem]:
    return [
        RequeueItem(
            requeue_id=r.id,
            action=r.action,
            unit=f.unit,
            filing_date=f.filing_date,
            index_key=f.index,
            reuse_only=r.reuse_only,
        )
        for r in requeues
        if today is None or r.not_before <= today
        for f in r.filings
    ]


_CONSUMED_SQL: Final = "SELECT requeue_id, unit, logical_date FROM fundamentals_requeue_log"

_JOURNAL_SQL: Final = """
INSERT INTO fundamentals_requeue_log
    (requeue_id, unit, logical_date, action, outcome, detail, consumed_at)
VALUES (%s, %s, %s, %s, %s, %s, %s)
ON CONFLICT (requeue_id, unit, logical_date) DO NOTHING
"""


def pending_items(
    conn: Connection, today: date, *, path: Path = REQUEUE_PATH
) -> tuple[RequeueItem, ...]:
    """The items a run on `today` owes: due (`not_before <= today`) and not yet journaled.

    Reads nothing from the database when nothing is due, so a register with no due re-queue costs
    the nightly run no query.
    """
    due = _items(_register(path).requeues, today=today)
    if not due:
        return ()
    consumed = {(str(r[0]), str(r[1]), r[2]) for r in conn.execute(_CONSUMED_SQL).fetchall()}
    return tuple(
        item for item in due if (item.requeue_id, item.unit, item.filing_date) not in consumed
    )


#: Records one item's outcome: `(item, outcome, detail)`.
Journal = Callable[[RequeueItem, RequeueOutcome, "str | None"], None]


def journal_to(conn: Connection, clock: Clock) -> Journal:
    """A `Journal` that appends to `fundamentals_requeue_log` and commits each row at once.

    Committed per item, like the runner's per-filing checkpoint, so a run killed mid-re-queue
    keeps the outcomes it reached and the next run drives only the rest.
    """

    def record(item: RequeueItem, outcome: RequeueOutcome, detail: str | None) -> None:
        conn.execute(
            _JOURNAL_SQL,
            (
                item.requeue_id,
                item.unit,
                item.filing_date,
                item.action.value,
                outcome.value,
                detail,
                clock.now(),
            ),
        )
        conn.commit()
        _LOG.info(
            "fundamentals_requeue.journaled",
            source=_FILING_SOURCE,
            requeue_id=item.requeue_id,
            unit=item.unit,
            date=item.filing_date.isoformat(),
            action=item.action.value,
            outcome=outcome.value,
            detail=detail,
            state=outcome.value,
        )

    return record
