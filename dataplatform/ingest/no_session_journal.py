"""The append-only journal of dates an archive served nothing for — shared by every sweep.

W1 established the rule this file exists to hold: over a decade-long archive sweep a 404 is not a
failure, it is a *measurement*, and the set of dates a dated archive refuses is the highest-
authority record of that archive's publication history that exists. Recording it once means the
campaign never pays for the same absence twice, and means the evidence outlives the run.

Extracted from `legacy_backfill` when W2's PR-bundle campaign needed the same journal over a
different source. The semantics are the source-independent half — append-only, validated on read,
idempotent per date, zero-cost resume — and the two source-dependent halves stay at the call site:

* the **filename**, so two campaigns over two archives never share a journal (`journal_path`), and
* the **evidence label**, because what an absence *means* is a claim about the source. For the
  bhavcopy archive a 404 means the exchange was shut; for the PR bundle, whose plan is built from
  sessions the calendar already vouched for, it means the exchange traded and no bundle was
  published. Recording both as one word would destroy the distinction that makes the record worth
  keeping.

Nothing here fetches, parses, or reads a clock of its own: the observation instant is injected
(B10), so a replayed run writes the same line.
"""

from __future__ import annotations

import json
from collections.abc import Iterable
from datetime import date, datetime
from pathlib import Path
from typing import Final

from pydantic import BaseModel, ConfigDict, Field, ValidationError

from dataplatform.clock import Clock, SystemClock

__all__ = [
    "HOLIDAY_OR_NO_SESSION",
    "NoSessionJournal",
    "NoSessionRecord",
    "journal_path",
]

#: The bhavcopy sweep's label: the archive serves a file for every session it had, so an absence is
#: a closed exchange. A campaign whose plan is not itself the archive's session list must not reuse
#: this — see the module docstring.
HOLIDAY_OR_NO_SESSION: Final = "HOLIDAY_OR_NO_SESSION"


class NoSessionRecord(BaseModel):
    """One dated observation that an archive served no file, and what that absence means.

    Written as one JSON object per line so the journal is append-only in the strongest sense the
    filesystem offers: a crash mid-campaign truncates at a line boundary and loses one
    observation, never the file. It is validated on the way back in because it is *evidence* — a
    line this schema does not recognise is a corrupt journal, and reading past it would quietly
    turn a lost 404 into a re-fetch.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    trade_date: date = Field(description="the candidate session the archive had no file for")
    era: str | None = Field(
        default=None,
        description=(
            "the format-era label the date falls in ('E1') for a source whose eras are "
            "dispatchable; None for a source whose readers sniff format per file"
        ),
    )
    url: str = Field(description="the exact URL that answered")
    http_status: int = Field(ge=100, le=599, description="the status observed, normally 404")
    observed_at: datetime = Field(description="tz-aware instant of the observation (injected)")
    evidence: str = Field(
        default=HOLIDAY_OR_NO_SESSION,
        description="what the absence means for *this* source — a claim, not a status code",
    )


def journal_path(data_root: Path, *, filename: str) -> Path:
    """Where one campaign's evidence journal lives for a lake root.

    Beside the lake rather than inside `L0/`: `L0Store.verify_checksums` walks the L0 tree looking
    for payloads without sidecars, and a stray file there would be reported as an orphan defect.
    The journal is derived observation, not a fetched payload.
    """
    return data_root / "campaign" / filename


class NoSessionJournal:
    """The append-only record of which candidate dates an archive answered 404 for.

    What it does: remembers, across runs and without a database, that a date has already been
    proved unpublished — so the resume path costs zero requests for it — and hands the whole
    observation set to a reconciler or a coverage report.
    What it assumes: it owns its file. Two concurrent campaigns over the same range would both
    append, which is harmless for the date set but duplicates lines.
    What it never does: forget, rewrite, or delete a line. This is the only record of what the
    archive refused, and the campaign only gets to observe each date once cheaply.
    """

    def __init__(
        self,
        path: Path,
        *,
        clock: Clock | None = None,
        evidence: str = HOLIDAY_OR_NO_SESSION,
    ) -> None:
        self._path = path
        self._clock = SystemClock() if clock is None else clock
        self._evidence = evidence
        self._records: list[NoSessionRecord] = list(_read_journal(path))
        self._dates = {record.trade_date for record in self._records}

    def __repr__(self) -> str:
        return f"NoSessionJournal(path={str(self._path)!r}, records={len(self._records)})"

    @property
    def path(self) -> Path:
        """The journal file, which may not exist yet."""
        return self._path

    @property
    def evidence(self) -> str:
        """The label this journal stamps on an absence — what a 404 means for its source."""
        return self._evidence

    @property
    def dates(self) -> frozenset[date]:
        """Every date observed to have no file."""
        return frozenset(self._dates)

    @property
    def records(self) -> tuple[NoSessionRecord, ...]:
        """Every observation, in the order it was written."""
        return tuple(self._records)

    def knows(self, day: date) -> bool:
        """Whether `day` has already been proved unpublished — the free half of resume."""
        return day in self._dates

    def record(
        self, day: date, *, era: str | None = None, url: str, http_status: int
    ) -> NoSessionRecord:
        """Append one observation and return it. Idempotent for a date already recorded."""
        if day in self._dates:
            existing = next(rec for rec in self._records if rec.trade_date == day)
            return existing
        record = NoSessionRecord(
            trade_date=day,
            era=era,
            url=url,
            http_status=http_status,
            observed_at=self._clock.now(),
            evidence=self._evidence,
        )
        self._path.parent.mkdir(parents=True, exist_ok=True)
        with self._path.open("a", encoding="utf-8") as handle:
            handle.write(record.model_dump_json() + "\n")
        self._records.append(record)
        self._dates.add(day)
        return record


def _read_journal(path: Path) -> Iterable[NoSessionRecord]:
    """Parse an existing journal, failing loud on a line this schema does not recognise."""
    if not path.is_file():
        return ()
    records: list[NoSessionRecord] = []
    with path.open(encoding="utf-8") as handle:
        for number, line in enumerate(handle, start=1):
            if not line.strip():
                continue
            try:
                records.append(NoSessionRecord.model_validate(json.loads(line)))
            except (ValidationError, json.JSONDecodeError) as exc:
                raise ValueError(
                    f"{path}:{number} is not a no-session record ({exc}); the 404 evidence "
                    "journal is append-only and is not repaired automatically"
                ) from exc
    return records
