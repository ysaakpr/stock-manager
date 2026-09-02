"""D4 point-in-time access rules + leak guard (M4.3) — invariant #7, §8.3 (6).

Invariant #7: *no data with `knowable_date > decision_date` reaches a decision.* This module is
where that stops being a promise and becomes a mechanism. It is deliberately **not** a convention
consumers must remember — a backtest that forgets to filter would flatter itself with hindsight
(§10, look-ahead bias), so the guard is structural: a PIT read that carries future data raises
here, in the query layer, before the record can reach a decision.

Two ideas, both enforced by types:

* **Every queryable dataset declares when each record became knowable.** A `Dataset` pairs its
  rows with a `knowable_date` extractor — the source timestamp / filing_date / publication date,
  whichever is the first moment the figure could honestly have been used. A dataset that declares
  *no* such extractor is not point-in-time data and is **unusable** in PIT mode: `admit` refuses it
  rather than guessing that "no declaration" means "always knowable" (acceptance #1). A per-record
  hole (the extractor returns `None` for a row) is refused the same way — a row that cannot say
  when it was knowable cannot be trusted not to leak.

* **A PIT query carries an `as_of`, and the guard raises on a leak — it does not silently drop.**
  `PitContext(as_of=…)` is the decision date. `admit(dataset)` returns the dataset's records only
  when *every* one is knowable on or before `as_of`; the instant it meets a record with
  `knowable_date > as_of` it raises `PitError`. Raising, not filtering-and-continuing, is the whole
  point of acceptance #2: a silent filter would turn "you leaked future data into this query" into
  "this query happened to return fewer rows", and a bug that returns a plausible-looking answer is
  the one that never gets caught. In correct operation a PIT query is *constructed* to ask only for
  knowable data (a cross-section for a session on or before `as_of`, a filing filtered to its
  filing_date), so the guard passes silently and the caller sees exactly the knowable rows —
  filtered `knowable_date <= as_of` by construction, with the guard as the tripwire that fires when
  the construction was wrong.

`as_of` is an explicit decision date, never a wall clock (B10 / CLAUDE.md — the clock is injected
and a query is clockless). Dates are `datetime.date`: a source with a finer timestamp reduces it to
the date the figure became knowable, matching every other `knowable_date` in the platform (corp
actions, fundamentals). This is the general guard the backtest harness (M4.8) and the PIT
fundamentals store (M7.2) read through; the fundamentals *join* surface has its own structural
quarantine for restated data (`screen.PitFundamentals`, invariant #8) — a narrower, orthogonal
boundary.
"""

from __future__ import annotations

from collections.abc import Callable, Iterable
from dataclasses import dataclass
from datetime import date

from dataplatform.logging import get_logger
from dataplatform.query.errors import QueryError

__all__ = ["Dataset", "KnowableDate", "PitContext", "PitError"]

_LOG = get_logger(__name__)


#: Reads the date a record became knowable, or `None` when that record carries no such date.
type KnowableDate[R] = Callable[[R], date | None]


class PitError(QueryError):
    """A point-in-time access rule was violated in a PIT-mode query.

    Raised for the two ways a PIT read can be unsafe: a dataset that declares no `knowable_date`
    (so it cannot be shown not to leak — acceptance #1), and a record whose `knowable_date` is
    after the query's `as_of` (future data reaching a decision — invariant #7, acceptance #2).
    Subclasses `QueryError`: it is the query layer refusing to answer, not an identity or ingestion
    fault. It is raised, never logged-and-swallowed, because a silently dropped leak hides the bug
    that caused it.
    """


@dataclass(frozen=True, slots=True)
class Dataset[R]:
    """A named, queryable dataset paired with the rule for reading each record's knowable date.

    What it does: carry the dataset's `records` alongside `knowable_date`, the extractor that
    returns when a given record became knowable (its source timestamp / filing_date / publication
    date).
    What it assumes: nothing about the records' own type — the extractor is the only contract, so
    any row shape (a price bar, a filing, a flow) becomes PIT-checkable by naming how to read its
    date.
    What it never does: default a missing declaration to "always knowable". `knowable_date=None`
    marks a dataset that declares no knowable date *at all*; such a dataset is structurally unusable
    in PIT mode (`PitContext.admit` refuses it) rather than being waved through — the safe default
    is to refuse, because assuming knowability is exactly the look-ahead bug the guard exists to
    stop.
    """

    name: str
    records: tuple[R, ...]
    knowable_date: KnowableDate[R] | None = None

    @classmethod
    def declaring(
        cls, name: str, records: Iterable[R], knowable_date: KnowableDate[R]
    ) -> Dataset[R]:
        """A PIT-usable dataset: `records` plus the extractor that dates each one's knowability."""
        return cls(name=name, records=tuple(records), knowable_date=knowable_date)

    @classmethod
    def undeclared(cls, name: str, records: Iterable[R]) -> Dataset[R]:
        """A dataset that declares no `knowable_date` — constructible, but unusable in PIT mode.

        Exists so a caller can represent "this source has no point-in-time tag" honestly and let
        the guard refuse it (acceptance #1), instead of the source silently masquerading as
        knowable.
        """
        return cls(name=name, records=tuple(records), knowable_date=None)


@dataclass(frozen=True, slots=True)
class PitContext:
    """A point-in-time query context — the decision date a PIT read is answered as of.

    What it does: hold `as_of`, the date beyond which nothing may be known, and expose `admit`, the
    one gate a PIT read passes through. Only data knowable on or before `as_of` may reach a decision
    (invariant #7).
    What it assumes: `as_of` is an explicit decision date supplied by the caller (a backtest step, a
    replay session), never `datetime.now()` — the query layer is clockless and deterministic (B10).
    What it never does: return future data. `admit` raises on the first leak rather than dropping
    it.
    """

    as_of: date

    def admit[R](self, dataset: Dataset[R]) -> tuple[R, ...]:
        """Return `dataset`'s records, having proven every one is knowable on or before `as_of`.

        The guard, and the only sanctioned way to read a dataset in PIT mode:

        * a dataset that declares no `knowable_date` extractor is refused — it is not point-in-time
          data and cannot be shown safe (acceptance #1, `PitError`);
        * a record whose extractor yields `None` is refused — a row that cannot state when it was
          knowable is a hole that could hide a leak (`PitError`);
        * a record with `knowable_date > as_of` is refused — future data must not reach a decision,
          and the guard *raises* rather than silently dropping it, so a leak surfaces as a loud
          failure instead of a quietly short answer that hides the bug (invariant #7, acceptance
          #2);
        * otherwise the record is admitted.

        Returns the admitted records in their original order. In correct use every record passes and
        the caller receives exactly the dataset filtered to `knowable_date <= as_of`; a raise means
        the query was constructed to reach data it should not have — the caller's bug to fix.
        """
        if dataset.knowable_date is None:
            raise PitError(
                f"dataset {dataset.name!r} declares no knowable_date and is unusable in point-in-"
                f"time mode (as_of {self.as_of.isoformat()}): a PIT query may only read data whose "
                f"first-knowable date is declared, so it can be proven not to leak future data "
                f"(invariant #7). Declare a knowable_date extractor via Dataset.declaring, or read "
                f"this source outside PIT mode."
            )
        admitted: list[R] = []
        for record in dataset.records:
            knowable = dataset.knowable_date(record)
            if knowable is None:
                raise PitError(
                    f"a record in dataset {dataset.name!r} has no knowable_date (as_of "
                    f"{self.as_of.isoformat()}): every record in a point-in-time dataset must "
                    f"declare when it became knowable — a record that cannot is a hole that could "
                    f"leak future data into a decision (invariant #7)."
                )
            if knowable > self.as_of:
                raise PitError(
                    f"point-in-time leak in dataset {dataset.name!r}: a record with knowable_date "
                    f"{knowable.isoformat()} is not yet knowable as of {self.as_of.isoformat()} "
                    f"and must not reach a decision (invariant #7). The guard raises rather than "
                    f"silently dropping the row: a filtered-but-silent result would hide the query "
                    f"that reached future data. Scope the query to knowable_date <= as_of before "
                    f"admitting it."
                )
            admitted.append(record)
        _LOG.info(
            "query.pit.admit",
            dataset=dataset.name,
            as_of=self.as_of.isoformat(),
            admitted=len(admitted),
        )
        return tuple(admitted)
