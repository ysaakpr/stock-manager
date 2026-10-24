"""Accepted deviations: failures the owner has knowingly accepted, shown as warnings, not errors.

The owner's rule for the operator surfaces: **an ERROR must mean something deviated from an agreed
plan.** A failure the owner has accepted — today, the 403 that `api.bseindia.com` has answered this
deployment since 2026-09-24, which AGENTIC_CONTEXT §8 forbids working around and the owner said to
ignore on 2026-10-12 — is shown as a WARNING (`ACCEPTED`), with the reason beside it, so that a
real new failure is not lost among the known ones.

The agreements live in `accepted_deviations.yaml` beside this module, one entry per accepted
cause, reviewed in the diff like every other checked-in register. Each entry names what it applies
to (`kind` and `target`), a **narrow matcher** for the known cause, the remark shown on the page,
its basis (who agreed, when, where it is recorded), `since`, and an optional `review_by` after
which it stops matching — the item turns red again and the page says the acceptance expired.

**Matching is narrow by construction.** No entry matches on a name alone: a source is accepted
only while *every* FAILED row of its current streak carries the known error, a job only while its
failed run raised the known exception class *and* every leg it failed is itself an accepted
source failure, an alert only while the entry it points at accepts the alert's subject right now.
Anything else — a new error on an accepted source, a non-BSE leg of a mixed job, a stuck row, a
timed-out run — stays an ERROR. Structured fields are used wherever they exist (`sync_state` rows,
their source and state, `job_run` state and window, `quality_flag` ISINs and dates, the gap
reason); the error *class* of a stored failure exists only as the `ClassName: ` prefix its writer
put on the text, so reading it — and the per-entry message pattern — is a text fallback, and is
said so where it happens (`ErrorPattern`).

**Display and alerting only.** Nothing here is read by a gate: not `sync_state.is_green`
(invariant #10), not the kill switch, not the paper or M17 session gating, not `job_run`'s
recording of a run, not `/health`. Every `healthy` field keeps its strict meaning; an accepted item
is still `healthy: false`, and carries a `deviation` beside it. `tests/unit/test_deviations.py`
pins that no gate module imports this one.

Loaded and validated once per process (`default_deviations`): the status API loads it at startup
and refuses to start on a malformed entry; the `failure_alerts` tick loads it on first use and, if
it cannot, treats nothing as accepted and fails its run loudly — a broken registry must never
silence a page.
"""

from __future__ import annotations

import re
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import date, datetime
from enum import StrEnum
from functools import cache
from pathlib import Path
from typing import Annotated, Any, Final, Literal

import yaml
from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator, model_validator

from dataplatform.clock import IST
from dataplatform.logging import get_logger
from dataplatform.status.sync_state import (
    SourceStatus,
    SyncKey,
    SyncRecord,
    SyncState,
    SyncStateStore,
)
from dataplatform.store.db import Connection

__all__ = [
    "DEVIATIONS_PATH",
    "Basis",
    "DeviationJudge",
    "DeviationKind",
    "DeviationOut",
    "DeviationRegistry",
    "DeviationRegistryError",
    "DeviationStatus",
    "ErrorPattern",
    "GapItem",
    "JobAttempt",
    "OpenConflict",
    "OpenFlag",
    "accepted_note",
    "conflicts_known",
    "default_deviations",
    "error_class",
    "flags_known",
    "gap_matches",
    "load_deviations",
]

DEVIATIONS_PATH: Final[Path] = Path(__file__).with_name("accepted_deviations.yaml")

log = get_logger(__name__)

#: An entry id: lower-case, stable, quotable in a URL fragment and a log line.
_ID: Final = r"^[a-z0-9][a-z0-9._-]{2,79}$"

#: The `ClassName: ` prefix the runner (`f"{type(error).__name__}: {error}"`) and most ingestion
#: writers put on a stored failure. Upper-case first letter, so a message that merely starts with
#: a lower-case source id and a colon ("bse_scrip_master: https://…") is not mistaken for one.
_CLASS_PREFIX: Final = re.compile(r"^([A-Z][A-Za-z0-9_]*): ")

#: The alert keys `alert_triggers` writes, and the (kind, subject) each one is about.
_ALERT_KEY: Final = re.compile(
    r"^(?:ingest:(?P<source>[a-z0-9][a-z0-9._-]*):failed_streak"
    r"|job:(?P<job>[a-z0-9_]+):failed"
    r"|quality:(?P<check>[A-Za-z0-9_.-]+):red)$"
)

#: Gap reasons that are already explained and so never need accepting (`quality.gaps.GapReason`).
_UNEXPLAINED_GAP_REASONS: Final = frozenset(
    {"NEVER_ATTEMPTED", "FAILED", "IN_PROGRESS", "GAP_ON_A_TRADING_DAY", "L0_PRESENT_L1_ABSENT"}
)

_IN_FLIGHT: Final = (SyncState.FETCHED, SyncState.VALIDATED, SyncState.NORMALIZED)

#: One leg of a capture run's own account, `source@YYYY-MM-DD=STATE` (`CaptureReport.summary`).
_REPORTED_LEG: Final = re.compile(r"\b([a-z0-9][a-z0-9_]*)@(\d{4}-\d{2}-\d{2})=([A-Z_]+)\b")

#: The leg outcomes that mean the date landed (`daily_capture.CaptureStatus`).
_LANDED_LEG_STATES: Final = frozenset({"CAPTURED", "ALREADY_PUBLISHED"})


class DeviationRegistryError(ValueError):
    """The checked-in registry is malformed: a bad pattern, a dangling reference, a gap."""


class DeviationKind(StrEnum):
    """What an entry applies to — one of the things the operator surfaces show red."""

    SOURCE = "source"  # a `/status/sources` line (a sync_state source id)
    JOB = "job"  # a registered scheduler job on `/status/jobs`
    ALERT = "alert"  # an `alert_condition` row raised by `failure_alerts` (its dedup key)
    QUALITY = "quality"  # a quality check's open ERROR flags (its check name)
    GAP = "gap"  # unexplained `/status/gaps` entries of one source


class DeviationStatus(StrEnum):
    """What an entry says about an item it recognises."""

    ACCEPTED = "ACCEPTED"  # a known, agreed cause: shown amber, alerts at warning
    EXPIRED = "EXPIRED"  # it would match, but `review_by` has passed: shown red, says so


# ── the checked-in schema ─────────────────────────────────────────────────────────────────


class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class Basis(_Strict):
    """Who agreed to the deviation, when, and where that agreement is written down."""

    agreed_by: str = Field(min_length=1, max_length=80, description="A role, never a name")
    agreed_on: date
    recorded_in: tuple[Annotated[str, Field(min_length=1, max_length=200)], ...] = Field(
        min_length=1
    )

    def summary(self) -> str:
        """One line for a tooltip: `owner, 2026-10-12 — where; where`."""
        return f"{self.agreed_by}, {self.agreed_on.isoformat()} — {'; '.join(self.recorded_in)}"


class ErrorPattern(_Strict):
    """One known shape of a stored failure: its exception class, its message, optionally its dates.

    Text fallback, on purpose and only here: `sync_state.last_error` and `job_run.error` are text,
    and neither table has an error-class column. The class is read from the `ClassName: ` prefix
    the writer put on the text (`class`), and `pattern` is searched in the message after it. A
    stored failure that carries no prefix — the CA backfill records a 403 spike as `str(spike)` —
    is matched only by an alternative that says `class: null`, never by a pattern that happens to
    fit, so a new exception type with a similar message is not accepted.

    `dated_from` / `dated_to` (inclusive) bound the row's logical date — a cause that was true
    only for a known window (BSE's empty-array soft block, 2026-09-08..09-23) is not accepted on a
    date outside it. A row whose date is unknown never matches a bounded pattern.
    """

    error_class: str | None = Field(alias="class", description="null: the text has no prefix")
    pattern: str = Field(min_length=1)
    dated_from: date | None = None
    dated_to: date | None = None

    model_config = ConfigDict(extra="forbid", frozen=True, populate_by_name=True)

    @field_validator("error_class")
    @classmethod
    def _class_name(cls, value: str | None) -> str | None:
        if value is not None and not re.fullmatch(r"[A-Z][A-Za-z0-9_]*", value):
            raise ValueError(f"class {value!r} is not an exception class name")
        return value

    @field_validator("pattern")
    @classmethod
    def _compiles(cls, value: str) -> str:
        _compile(value)
        return value

    @model_validator(mode="after")
    def _window(self) -> ErrorPattern:
        bounds = (self.dated_from, self.dated_to)
        if bounds[0] is not None and bounds[1] is not None and bounds[1] < bounds[0]:
            raise ValueError(f"dated_to {bounds[1]} is before dated_from {bounds[0]}")
        return self

    def matches(self, text: str | None, logical_date: date | None = None) -> bool:
        """Whether `text` (of a row about `logical_date`) is this failure."""
        if text is None:
            return False
        if self.dated_from is not None or self.dated_to is not None:
            if logical_date is None:
                return False
            if self.dated_from is not None and logical_date < self.dated_from:
                return False
            if self.dated_to is not None and logical_date > self.dated_to:
                return False
        found, message = error_class(text)
        if found != self.error_class:
            return False
        return re.search(self.pattern, message) is not None


def _compile(pattern: str) -> re.Pattern[str]:
    try:
        return re.compile(pattern)
    except re.error as error:
        raise ValueError(f"pattern {pattern!r} does not compile: {error}") from error


def error_class(text: str) -> tuple[str | None, str]:
    """Split a stored failure into its `ClassName: ` prefix (or None) and the message after it."""
    prefix = _CLASS_PREFIX.match(text)
    if prefix is None:
        return None, text
    return prefix.group(1), text[prefix.end() :]


def _any(patterns: Sequence[ErrorPattern], text: str | None, logical_date: date | None) -> bool:
    return any(pattern.matches(text, logical_date) for pattern in patterns)


def _young(logical_date: date, today: date, max_age_days: int | None) -> bool:
    """Within `max_age_days` of today — "a warning while it is still being retried"."""
    return max_age_days is None or (today - logical_date).days <= max_age_days


class SourceMatch(_Strict):
    """A source is accepted while every FAILED row of its current streak is one of `errors`.

    `max_age_days`, when given, also requires every one of those rows to be dated within that
    many days of today: a failure still inside its retry window is a warning, one older than that
    is an error again ("WARN while retrying, ERROR after N days").
    """

    errors: tuple[ErrorPattern, ...] = Field(min_length=1)
    max_age_days: int | None = Field(default=None, ge=0)


class IdentityConflictSet(_Strict):
    """A known set of open identity conflicts (`identity_reconciliation`, unresolved rows).

    Set-valued: matches while there is at least one unresolved row in the filter and every such
    row is in the set — its `id` in `ids`, and every ISIN it names in `isins` (each given criterion
    must hold). A new conflict — a new row, or an ISIN outside the set — does not match.
    """

    ids: frozenset[int] | None = None
    isins: frozenset[str] | None = None
    exchange: Literal["NSE", "BSE"] | None = None
    detected_by: Literal["INGEST", "RESOLVE"] | None = None

    @model_validator(mode="after")
    def _bounded(self) -> IdentityConflictSet:
        if not self.ids and not self.isins:
            raise ValueError("identity_conflicts needs a non-empty ids or isins set")
        return self


class JobMatch(_Strict):
    """A job is accepted while its newest finished attempt FAILED with the known cause.

    `error_classes` is the exception the run raised (`job_run.error`'s prefix). At least one more
    condition must hold, so that the class alone never accepts a run — and every one given must:

    * `legs` — the sync_state sources the job writes. The rows the run wrote on them (`updated_at`
      between its start and finish) must include at least one FAILED row, and every one must be
      PUBLISHED, a GAP, or an accepted source failure (a `source` entry for that source, in date,
      whose `errors` match the row). A FAILED leg of any other source — the NSE leg of a mixed job
      — or a row left mid-pipeline keeps the job an ERROR. Structured: no text is read.
    * `reported_legs` — the run's error enumerates its legs as `source@YYYY-MM-DD=STATE` (the
      `DailyCaptureError` summary). Every FAILED leg must be a FAILED sync_state row that is an
      accepted source failure, every other leg CAPTURED or ALREADY_PUBLISHED, and the text must
      not be truncated. A cross-check of `legs` from the job's own account (text fallback).
    * `published_on_run_date` — sync_state sources (`source` or `source/unit`) that must be
      PUBLISHED for the run's date (its IST start date), e.g. the NSE refresh a CA run did land.
    * `error_pattern` — searched in the run's error message (text fallback, for a job that leaves
      no sync_state rows behind).
    * `identity_conflicts` — the unresolved identity conflicts are a known set.
    """

    error_classes: tuple[str, ...] = Field(min_length=1)
    legs: tuple[str, ...] = ()
    reported_legs: bool = False
    published_on_run_date: tuple[str, ...] = ()
    error_pattern: str | None = None
    identity_conflicts: IdentityConflictSet | None = None

    @model_validator(mode="after")
    def _narrow(self) -> JobMatch:
        if not (
            self.legs
            or self.reported_legs
            or self.error_pattern is not None
            or self.identity_conflicts is not None
        ):
            raise ValueError(
                "a job matcher needs legs, reported_legs, error_pattern or identity_conflicts "
                "besides error_classes — a class alone would accept any failure of that type"
            )
        if self.error_pattern is not None:
            _compile(self.error_pattern)
        return self


class AlertMatch(_Strict):
    """An alert is accepted while the entry `while_accepted` names accepts the alert's subject."""

    while_accepted: str = Field(pattern=_ID)


class QualityMatch(_Strict):
    """A check is accepted while it has open ERROR flags and every one is in the known set.

    Set-valued: each criterion given must hold for every open ERROR flag of the check — its
    `fingerprint` (the sentinel's dedupe key, `detail->>'fingerprint'`) in `fingerprints`, its row
    id in `ids`, its ISIN in `isins`, raised before `raised_before`, about a logical date before
    `dated_before`. A flag without the field asked about (no ISIN, no fingerprint) never satisfies
    it, so a new flag outside the set keeps the check red.
    """

    fingerprints: frozenset[str] | None = None
    ids: frozenset[int] | None = None
    isins: frozenset[str] | None = None
    raised_before: datetime | None = None
    dated_before: date | None = None

    @model_validator(mode="after")
    def _bounded(self) -> QualityMatch:
        if not (
            self.fingerprints
            or self.ids
            or self.isins
            or self.raised_before is not None
            or self.dated_before is not None
        ):
            raise ValueError(
                "a quality matcher needs fingerprints, ids, isins, raised_before or dated_before"
            )
        if self.raised_before is not None and self.raised_before.tzinfo is None:
            raise ValueError("raised_before must carry a UTC offset (e.g. +05:30)")
        return self


class GapMatch(_Strict):
    """Unexplained gap entries of the target source, each accepted on its own evidence.

    `reasons` are the gap reasons it covers; at least one of `errors` (the entry's sync_state
    error must be one of these), `dates` (a known set) or `before` (dated before) must narrow it.
    `max_age_days` additionally limits it to entries dated within that many days of today.
    """

    reasons: frozenset[str] = Field(min_length=1)
    errors: tuple[ErrorPattern, ...] = ()
    dates: frozenset[date] | None = None
    before: date | None = None
    max_age_days: int | None = Field(default=None, ge=0)

    @model_validator(mode="after")
    def _bounded(self) -> GapMatch:
        unknown = self.reasons - _UNEXPLAINED_GAP_REASONS
        if unknown:
            raise ValueError(f"gap reasons {sorted(unknown)} are not unexplained gap reasons")
        if not self.errors and self.dates is None and self.before is None:
            raise ValueError("a gap matcher needs errors, dates or before besides reasons")
        return self


class _Entry(_Strict):
    id: str = Field(pattern=_ID)
    target: str = Field(min_length=1, max_length=120)
    remark: str = Field(min_length=1, max_length=400, description="Shown beside the item")
    basis: Basis
    since: date
    review_by: date | None = None

    @model_validator(mode="after")
    def _review_after_since(self) -> _Entry:
        if self.review_by is not None and self.review_by < self.since:
            raise ValueError(f"review_by {self.review_by} is before since {self.since}")
        return self

    def expired(self, today: date) -> bool:
        """Past `review_by` the entry accepts nothing; on the day itself it still does."""
        return self.review_by is not None and today > self.review_by


class SourceEntry(_Entry):
    kind: Literal[DeviationKind.SOURCE]
    match: SourceMatch


class JobEntry(_Entry):
    kind: Literal[DeviationKind.JOB]
    match: JobMatch


class AlertEntry(_Entry):
    kind: Literal[DeviationKind.ALERT]
    match: AlertMatch

    @field_validator("target")
    @classmethod
    def _alert_key(cls, value: str) -> str:
        if _ALERT_KEY.match(value) is None:
            raise ValueError(
                f"alert target {value!r} is not an ingest:<source>:failed_streak, "
                "job:<job>:failed or quality:<check>:red key"
            )
        return value


class QualityEntry(_Entry):
    kind: Literal[DeviationKind.QUALITY]
    match: QualityMatch


class GapEntry(_Entry):
    kind: Literal[DeviationKind.GAP]
    match: GapMatch


Deviation = Annotated[
    SourceEntry | JobEntry | AlertEntry | QualityEntry | GapEntry, Field(discriminator="kind")
]


def _alert_subject(key: str) -> tuple[DeviationKind, str]:
    found = _ALERT_KEY.match(key)
    if found is None:
        raise ValueError(f"{key!r} is not an alert key this registry can accept")
    if found.group("source"):
        return DeviationKind.SOURCE, found.group("source")
    if found.group("job"):
        return DeviationKind.JOB, found.group("job")
    return DeviationKind.QUALITY, found.group("check")


class DeviationRegistry(_Strict):
    """Every accepted deviation, validated as a whole: unique ids, references that resolve."""

    version: Literal[1]
    deviations: tuple[Deviation, ...]

    @model_validator(mode="after")
    def _consistent(self) -> DeviationRegistry:
        ids = [entry.id for entry in self.deviations]
        duplicates = sorted({entry for entry in ids if ids.count(entry) > 1})
        if duplicates:
            raise ValueError(f"duplicate deviation ids: {duplicates}")
        by_id = {entry.id: entry for entry in self.deviations}
        for entry in self.deviations:
            if not isinstance(entry, AlertEntry):
                continue
            ref = by_id.get(entry.match.while_accepted)
            subject = _alert_subject(entry.target)
            if ref is None:
                raise ValueError(
                    f"{entry.id}: while_accepted names {entry.match.while_accepted!r}, "
                    "which is not an entry"
                )
            if (ref.kind, ref.target) != subject:
                raise ValueError(
                    f"{entry.id}: alert {entry.target!r} is about {subject[0].value} "
                    f"{subject[1]!r}, but {ref.id} is a {ref.kind.value} entry for {ref.target!r}"
                )
        return self

    def entries(self, kind: DeviationKind, target: str) -> tuple[Deviation, ...]:
        """The entries of one kind for one target, in file order."""
        return tuple(e for e in self.deviations if e.kind is kind and e.target == target)

    def get(self, entry_id: str) -> Deviation:
        """The entry with this id; `KeyError` when there is none."""
        for entry in self.deviations:
            if entry.id == entry_id:
                return entry
        raise KeyError(entry_id)


def load_deviations(path: Path = DEVIATIONS_PATH) -> DeviationRegistry:
    """Read and validate the registry at `path`.

    What it does: parses the YAML and validates every entry (closed fields, compiled patterns,
    resolving references, a narrow matcher per kind).
    What it never does: return a partial registry. One malformed entry raises
    `DeviationRegistryError` naming the file and what is wrong — there is no "skip the bad entry",
    because a dropped acceptance and a dropped ERROR look the same on the page.
    """
    try:
        raw = yaml.safe_load(path.read_text(encoding="utf-8"))
    except (OSError, yaml.YAMLError) as error:
        raise DeviationRegistryError(f"{path}: unreadable: {error}") from error
    if isinstance(raw, Mapping):
        # `x-…` keys hold YAML anchors shared between entries (one 403 shape, one basis); they
        # are inputs to the entries, not entries, and every other unknown key is still refused.
        raw = {key: value for key, value in raw.items() if not str(key).startswith("x-")}
    try:
        return DeviationRegistry.model_validate(raw)
    except ValidationError as error:
        raise DeviationRegistryError(f"{path}: {_describe(error, raw)}") from error


def _describe(error: ValidationError, raw: Any) -> str:
    """The pydantic errors, each naming the entry id rather than only its list index."""
    lines = []
    entries = raw.get("deviations") if isinstance(raw, Mapping) else None
    for item in error.errors():
        loc = list(item["loc"])
        where = ".".join(str(part) for part in loc)
        if len(loc) >= 2 and loc[0] == "deviations" and isinstance(loc[1], int):
            index = loc[1]
            if isinstance(entries, list) and index < len(entries):
                entry = entries[index]
                if isinstance(entry, Mapping) and "id" in entry:
                    where = f"entry {entry['id']!r} ({where})"
        lines.append(f"{where}: {item['msg']}")
    return "; ".join(lines)


@cache
def default_deviations() -> DeviationRegistry:
    """The checked-in registry, read once per process. A YAML edit needs a restart to apply."""
    registry = load_deviations()
    log.info("deviations.loaded", entries=len(registry.deviations), path=str(DEVIATIONS_PATH))
    return registry


# ── the verdict, as served ────────────────────────────────────────────────────────────────


class DeviationOut(BaseModel):
    """An accepted-deviation entry's verdict on one item, as the status API serves it.

    Present only when an entry recognises the item. `ACCEPTED`: the item is a known, agreed
    failure — show it amber with `remark`. `EXPIRED`: it would be, but `review_by` has passed —
    show it red and say the acceptance needs a review. Never changes the item's own `healthy`.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    id: str
    kind: DeviationKind
    target: str
    status: DeviationStatus
    remark: str
    basis: str = Field(description="Who agreed, when, and where it is recorded")
    since: date
    review_by: date | None

    @property
    def accepted(self) -> bool:
        return self.status is DeviationStatus.ACCEPTED


def _verdict(entry: Deviation, today: date, *, expired: bool | None = None) -> DeviationOut:
    gone = entry.expired(today) if expired is None else expired
    return DeviationOut(
        id=entry.id,
        kind=entry.kind,
        target=entry.target,
        status=DeviationStatus.EXPIRED if gone else DeviationStatus.ACCEPTED,
        remark=entry.remark,
        basis=entry.basis.summary(),
        since=entry.since,
        review_by=entry.review_by,
    )


def _first(verdicts: Iterable[DeviationOut]) -> DeviationOut | None:
    """The first ACCEPTED verdict, else the first EXPIRED one, else None."""
    found = list(verdicts)
    return next((v for v in found if v.accepted), found[0] if found else None)


# ── evidence ──────────────────────────────────────────────────────────────────────────────


@dataclass(frozen=True, slots=True)
class JobAttempt:
    """A job's newest attempt that reached an outcome (not RUNNING, not SKIPPED_LOCKED)."""

    job_name: str
    state: str
    started_at: datetime
    finished_at: datetime | None
    error: str | None


@dataclass(frozen=True, slots=True)
class GapItem:
    """What a gap matcher reads off one `/status/gaps` entry."""

    source: str
    logical_date: date
    reason: str
    last_error: str | None


@dataclass(frozen=True, slots=True)
class OpenConflict:
    """One unresolved `identity_reconciliation` row, as an identity-conflict set reads it."""

    id: int
    exchange: str
    detected_by: str
    isins: frozenset[str]


@dataclass(frozen=True, slots=True)
class OpenFlag:
    """One open ERROR `quality_flag` row, as a quality set reads it."""

    id: int
    fingerprint: str | None
    isin: str | None
    logical_date: date
    raised_at: datetime


_NEWEST_FINISHED_SQL: Final = """
SELECT state, started_at, finished_at, error
FROM job_run
WHERE job_name = %s AND state NOT IN ('RUNNING', 'SKIPPED_LOCKED')
ORDER BY started_at DESC
LIMIT 1
"""

_OPEN_ERROR_FLAGS_SQL: Final = """
SELECT id, detail->>'fingerprint', isin, logical_date, raised_at
FROM quality_flag
WHERE NOT resolved AND severity = 'ERROR' AND check_name = %s
"""

_OPEN_CONFLICTS_SQL: Final = """
SELECT id, exchange, detected_by, isins
FROM identity_reconciliation
WHERE NOT resolved
"""


@dataclass
class DeviationJudge:
    """The registry bound to one connection and one `today`, answering per item.

    What it does: for an item the registry has an entry for, reads the evidence that entry's
    matcher needs — the source's streak rows, the job's newest finished attempt and the rows it
    wrote, a check's open ERROR flags, the open identity conflicts — and returns the verdict.
    Evidence is read at most once per judge.
    What it assumes: `store` is a `SyncStateStore` on `conn`, and `today` is the injected clock's.
    What it never does: read anything for an item no entry targets (an unrelated FAILING job costs
    no query), or write. A verdict is never consulted by a gate; see the module docstring.
    """

    registry: DeviationRegistry
    conn: Connection
    store: SyncStateStore
    today: date
    _streaks: dict[str, tuple[SyncRecord, ...]] = field(default_factory=dict)
    _statuses: dict[str, SourceStatus] | None = None
    _attempts: dict[str, JobAttempt | None] = field(default_factory=dict)
    _flags: dict[str, list[OpenFlag]] = field(default_factory=dict)
    _conflicts: list[OpenConflict] | None = None

    # ── sources ────────────────────────────────────────────────────────────────────────────

    def source(self, status: SourceStatus) -> DeviationOut | None:
        """The verdict on one `/status/sources` line, or None when no entry recognises it."""
        entries = self.registry.entries(DeviationKind.SOURCE, status.source)
        if not entries or status.healthy:
            return None
        return _first(
            _verdict(entry, self.today)
            for entry in entries
            if isinstance(entry, SourceEntry) and self._source_matches(entry, status)
        )

    def _source_matches(self, entry: SourceEntry, status: SourceStatus) -> bool:
        if status.declined is not None or status.failure_streak == 0:
            return False  # unhealthy for a reason that is not a failure streak: not this cause
        if any(status.counts.get(state, 0) for state in _IN_FLIGHT):
            return False  # a row stuck mid-pipeline is a different failure mode
        streak = self._streak(status.source)
        if len(streak) != status.failure_streak:
            log.warning(
                "deviations.streak_mismatch",
                source=status.source,
                failure_streak=status.failure_streak,
                rows=len(streak),
            )
            return False  # the evidence and the summary disagree: do not guess
        return all(
            _any(entry.match.errors, row.last_error, row.logical_date)
            and _young(row.logical_date, self.today, entry.match.max_age_days)
            for row in streak
        )

    def _streak(self, source: str) -> tuple[SyncRecord, ...]:
        if source not in self._streaks:
            self._streaks[source] = self.store.failure_streak_rows(source)
        return self._streaks[source]

    def _status(self, source: str) -> SourceStatus | None:
        if self._statuses is None:
            self._statuses = {s.source: s for s in self.store.source_statuses()}
        return self._statuses.get(source)

    def leg_accepted(self, row: SyncRecord) -> bool:
        """Whether one sync_state row is an accepted source failure: FAILED, known, in date."""
        if row.state is not SyncState.FAILED:
            return False
        base = SyncKey.parse(row.source).base
        return any(
            isinstance(entry, SourceEntry)
            and not entry.expired(self.today)
            and _any(entry.match.errors, row.last_error, row.logical_date)
            and _young(row.logical_date, self.today, entry.match.max_age_days)
            for entry in self.registry.entries(DeviationKind.SOURCE, base)
        )

    # ── jobs ───────────────────────────────────────────────────────────────────────────────

    def job(self, name: str, *, started_at: datetime | None = None) -> DeviationOut | None:
        """The verdict on a job's newest finished attempt, or None.

        `started_at`, when given, is the attempt the caller is showing (`/status/jobs`'s newest
        attempt); a verdict about any other attempt is not about that line, so it is None.
        """
        entries = self.registry.entries(DeviationKind.JOB, name)
        if not entries:
            return None
        attempt = self._attempt(name)
        if attempt is None or (started_at is not None and attempt.started_at != started_at):
            return None
        return _first(
            _verdict(entry, self.today)
            for entry in entries
            if isinstance(entry, JobEntry) and self._job_matches(entry.match, attempt)
        )

    def _job_matches(self, match: JobMatch, attempt: JobAttempt) -> bool:
        if attempt.state != "FAILED" or attempt.error is None:
            return False  # TIMED_OUT, or a failure with no recorded cause: not a known cause
        found, message = error_class(attempt.error)
        if found not in match.error_classes:
            return False
        if match.error_pattern is not None and re.search(match.error_pattern, message) is None:
            return False
        if match.legs and not self._legs_known(match.legs, attempt):
            return False
        if match.reported_legs and not self._reported_legs_known(message):
            return False
        run_date = attempt.started_at.astimezone(IST).date()
        for source in match.published_on_run_date:
            record = self.store.get(source, run_date)
            if record is None or record.state is not SyncState.PUBLISHED:
                return False
        return match.identity_conflicts is None or conflicts_known(
            match.identity_conflicts, self._open_conflicts()
        )

    def _legs_known(self, legs: Sequence[str], attempt: JobAttempt) -> bool:
        """The run's own rows: at least one accepted FAILED leg, and nothing else gone wrong."""
        if attempt.finished_at is None:
            return False
        rows = self.store.rows_updated_between(legs, attempt.started_at, attempt.finished_at)
        failed = [row for row in rows if row.state is SyncState.FAILED]
        settled = (SyncState.PUBLISHED, SyncState.GAP)
        return bool(failed) and all(row.state in settled or self.leg_accepted(row) for row in rows)

    def _reported_legs_known(self, message: str) -> bool:
        """The run's own account of its legs (text fallback): every failure an accepted one."""
        if message.endswith("…"):
            return False  # truncated: legs past the cut are unknown, so the run is not known
        legs = _REPORTED_LEG.findall(message)
        if not legs:
            return False
        for source, day, state in legs:
            if state == "FAILED":
                record = self.store.get(source, date.fromisoformat(day))
                if record is None or not self.leg_accepted(record):
                    return False
            elif state not in _LANDED_LEG_STATES:
                return False
        return True

    def _attempt(self, name: str) -> JobAttempt | None:
        if name not in self._attempts:
            row = self.conn.execute(_NEWEST_FINISHED_SQL, (name,)).fetchone()
            self._attempts[name] = (
                None
                if row is None
                else JobAttempt(
                    job_name=name,
                    state=str(row[0]),
                    started_at=row[1],
                    finished_at=row[2],
                    error=None if row[3] is None else str(row[3]),
                )
            )
        return self._attempts[name]

    def _open_conflicts(self) -> list[OpenConflict]:
        if self._conflicts is None:
            self._conflicts = [
                OpenConflict(
                    id=int(r[0]), exchange=str(r[1]), detected_by=str(r[2]), isins=frozenset(r[3])
                )
                for r in self.conn.execute(_OPEN_CONFLICTS_SQL).fetchall()
            ]
        return self._conflicts

    # ── quality checks ─────────────────────────────────────────────────────────────────────

    def quality(self, check_name: str) -> DeviationOut | None:
        """The verdict on one quality check's open ERROR flags, or None."""
        entries = self.registry.entries(DeviationKind.QUALITY, check_name)
        if not entries:
            return None
        flags = self._open_flags(check_name)
        return _first(
            _verdict(entry, self.today)
            for entry in entries
            if isinstance(entry, QualityEntry) and flags_known(entry.match, flags)
        )

    def _open_flags(self, check_name: str) -> list[OpenFlag]:
        if check_name not in self._flags:
            self._flags[check_name] = [
                OpenFlag(
                    id=int(r[0]),
                    fingerprint=None if r[1] is None else str(r[1]),
                    isin=None if r[2] is None else str(r[2]),
                    logical_date=r[3],
                    raised_at=r[4],
                )
                for r in self.conn.execute(_OPEN_ERROR_FLAGS_SQL, (check_name,)).fetchall()
            ]
        return self._flags[check_name]

    # ── gaps ───────────────────────────────────────────────────────────────────────────────

    def gap(self, item: GapItem) -> DeviationOut | None:
        """The verdict on one unexplained gap entry, from the entry's own fields (no read)."""
        return _first(
            _verdict(entry, self.today)
            for entry in self.registry.entries(DeviationKind.GAP, item.source)
            if isinstance(entry, GapEntry) and gap_matches(entry.match, item, self.today)
        )

    # ── alerts ─────────────────────────────────────────────────────────────────────────────

    def alert(self, dedup_key: str) -> DeviationOut | None:
        """The verdict on one alert condition, or None.

        An alert entry accepts its key only while the entry it names (`while_accepted`) accepts
        the alert's subject *now*: the source's streak, the job's newest finished attempt, the
        check's flags. Either one expired makes the verdict EXPIRED.
        """
        entries = self.registry.entries(DeviationKind.ALERT, dedup_key)
        if not entries:
            return None
        verdicts = []
        for entry in entries:
            if not isinstance(entry, AlertEntry):
                continue
            subject = self._subject_verdict(self.registry.get(entry.match.while_accepted))
            if subject is None:
                continue
            gone = entry.expired(self.today) or not subject.accepted
            verdicts.append(_verdict(entry, self.today, expired=gone))
        return _first(verdicts)

    def _subject_verdict(self, ref: Deviation) -> DeviationOut | None:
        if isinstance(ref, SourceEntry):
            status = self._status(ref.target)
            if status is None or status.healthy or not self._source_matches(ref, status):
                return None
            return _verdict(ref, self.today)
        if isinstance(ref, JobEntry):
            attempt = self._attempt(ref.target)
            if attempt is None or not self._job_matches(ref.match, attempt):
                return None
            return _verdict(ref, self.today)
        if isinstance(ref, QualityEntry):
            if not flags_known(ref.match, self._open_flags(ref.target)):
                return None
            return _verdict(ref, self.today)
        return None


def flags_known(match: QualityMatch, flags: Sequence[OpenFlag]) -> bool:
    """At least one open ERROR flag, and every one inside the set (every given criterion)."""
    if not flags:
        return False
    for flag in flags:
        if match.fingerprints and flag.fingerprint not in match.fingerprints:
            return False
        if match.ids and flag.id not in match.ids:
            return False
        if match.isins and (flag.isin is None or flag.isin not in match.isins):
            return False
        if match.raised_before is not None and flag.raised_at >= match.raised_before:
            return False
        if match.dated_before is not None and flag.logical_date >= match.dated_before:
            return False
    return True


def conflicts_known(match: IdentityConflictSet, conflicts: Sequence[OpenConflict]) -> bool:
    """At least one open conflict in the filter, and every one inside the set."""
    chosen = [
        c
        for c in conflicts
        if (match.exchange is None or c.exchange == match.exchange)
        and (match.detected_by is None or c.detected_by == match.detected_by)
    ]
    return bool(chosen) and all(
        (not match.ids or c.id in match.ids) and (not match.isins or c.isins <= match.isins)
        for c in chosen
    )


def gap_matches(match: GapMatch, item: GapItem, today: date) -> bool:
    """Whether one unexplained gap entry is the known cause."""
    if item.reason not in match.reasons:
        return False
    if match.errors and not _any(match.errors, item.last_error, item.logical_date):
        return False
    if match.dates is not None and item.logical_date not in match.dates:
        return False
    if match.before is not None and item.logical_date >= match.before:
        return False
    return _young(item.logical_date, today, match.max_age_days)


def accepted_note(verdict: DeviationOut) -> str:
    """One line for an alert body: which agreement makes this condition a warning."""
    return (
        f"Accepted deviation {verdict.id} (warning, not critical): {verdict.remark} "
        f"[basis: {verdict.basis}]"
    )
