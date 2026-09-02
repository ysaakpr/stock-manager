"""M4.3 — PIT access rules + leak guard (invariant #7, §8.3 (6)).

Every acceptance criterion of the task is a test here:

  1. a dataset without a declared knowable_date is unusable in PIT mode — it raises
     (`test_undeclared_dataset_is_unusable_in_pit_mode`,
     `test_record_without_knowable_date_raises`)
  2. reading future data in PIT mode raises rather than returning a filtered-but-silent result
     that would hide the leak (`test_future_data_raises_not_silently_filtered`,
     `test_leak_raises_even_when_some_rows_are_knowable`)
  3. the guard is enforced in the query layer, not in caller code — the only way to read a dataset
     in PIT mode is `PitContext.admit`, exported from `dataplatform.query`
     (`test_guard_lives_in_query_layer`, `test_admit_returns_knowable_rows_in_order`)

The datasets here are deliberately tiny, hand-built rows so the boundary cases (a record exactly on
`as_of`, a record one day after, a `None` knowable date) are unambiguous. No I/O, no network.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date

import pytest

from dataplatform.query import Dataset, PitContext, PitError, QueryError


@dataclass(frozen=True)
class _Filing:
    """A minimal record: a metric value that became knowable on `knowable`."""

    isin: str
    value: int
    knowable: date | None


def _filings() -> tuple[_Filing, ...]:
    return (
        _Filing("INE001A01001", 10, date(2024, 1, 10)),
        _Filing("INE002A01002", 20, date(2024, 3, 31)),  # exactly on a typical as_of
        _Filing("INE003A01003", 30, date(2024, 6, 30)),  # a future filing relative to Q1 as_of
    )


# ── acceptance #1: a dataset without a declared knowable_date is unusable in PIT mode ───────────


def test_undeclared_dataset_is_unusable_in_pit_mode() -> None:
    """A dataset declaring no knowable_date extractor cannot be admitted — it raises."""
    dataset = Dataset.undeclared("restated_screener", _filings())
    ctx = PitContext(as_of=date(2024, 3, 31))
    with pytest.raises(PitError, match="declares no knowable_date"):
        ctx.admit(dataset)


def test_undeclared_via_bare_constructor_also_raises() -> None:
    """`knowable_date=None` on the raw constructor is the same undeclared state — still refused."""
    dataset: Dataset[_Filing] = Dataset("no_tag", _filings(), knowable_date=None)
    with pytest.raises(PitError):
        PitContext(as_of=date(2024, 12, 31)).admit(dataset)


def test_record_without_knowable_date_raises() -> None:
    """A declared dataset with a per-record hole (extractor yields None) is refused, not skipped."""
    rows = (_Filing("INE001A01001", 10, date(2024, 1, 10)), _Filing("INE004A01004", 40, None))
    dataset = Dataset.declaring("holed", rows, lambda f: f.knowable)
    ctx = PitContext(as_of=date(2024, 12, 31))
    with pytest.raises(PitError, match="no knowable_date"):
        ctx.admit(dataset)


# ── acceptance #2: reading future data in PIT mode raises (never a silent filter that hides it) ──


def test_future_data_raises_not_silently_filtered() -> None:
    """A dataset carrying a record newer than as_of raises — it does not quietly return the rest."""
    dataset = Dataset.declaring("filings", _filings(), lambda f: f.knowable)
    ctx = PitContext(as_of=date(2024, 3, 31))  # the June filing is not yet knowable
    with pytest.raises(PitError, match="leak"):
        ctx.admit(dataset)


def test_leak_raises_even_when_some_rows_are_knowable() -> None:
    """The guard fails the whole read on any leak — a partial, filtered answer hides the bug."""
    dataset = Dataset.declaring("filings", _filings(), lambda f: f.knowable)
    ctx = PitContext(as_of=date(2024, 4, 1))
    with pytest.raises(PitError) as excinfo:
        ctx.admit(dataset)
    # The message names the offending future date, so the leak is diagnosable, not silent.
    assert "2024-06-30" in str(excinfo.value)


def test_record_exactly_on_as_of_is_knowable() -> None:
    """`knowable_date == as_of` is knowable — the boundary is inclusive (`<= as_of`)."""
    rows = (_Filing("INE002A01002", 20, date(2024, 3, 31)),)
    dataset = Dataset.declaring("boundary", rows, lambda f: f.knowable)
    admitted = PitContext(as_of=date(2024, 3, 31)).admit(dataset)
    assert admitted == rows


def test_record_one_day_after_as_of_leaks() -> None:
    """`knowable_date == as_of + 1 day` is a leak — the boundary excludes the day after."""
    rows = (_Filing("INE002A01002", 20, date(2024, 4, 1)),)
    dataset = Dataset.declaring("boundary", rows, lambda f: f.knowable)
    with pytest.raises(PitError, match="leak"):
        PitContext(as_of=date(2024, 3, 31)).admit(dataset)


# ── acceptance #3: the guard is in the query layer, and admits the knowable rows correctly ──────


def test_admit_returns_knowable_rows_in_order() -> None:
    """When every record is knowable, admit returns them unchanged, in original order."""
    dataset = Dataset.declaring("filings", _filings(), lambda f: f.knowable)
    admitted = PitContext(as_of=date(2024, 12, 31)).admit(dataset)
    assert admitted == _filings()


def test_empty_dataset_admits_to_empty() -> None:
    """A declared but empty dataset admits to nothing — no rows, no leak, no error."""
    dataset: Dataset[_Filing] = Dataset.declaring("empty", (), lambda f: f.knowable)
    assert PitContext(as_of=date(2024, 3, 31)).admit(dataset) == ()


def test_guard_lives_in_query_layer() -> None:
    """The guard is exported from the query package — the enforcement point, not caller code."""
    import dataplatform.query as query

    assert query.PitContext is PitContext
    assert query.Dataset is Dataset
    assert query.PitError is PitError
    # The module physically lives under the query package.
    assert PitContext.__module__ == "dataplatform.query.pit"


def test_pit_error_is_a_query_error() -> None:
    """A leak is the query layer refusing the request — PitError is a QueryError."""
    assert issubclass(PitError, QueryError)


def test_context_is_frozen() -> None:
    """A PIT context's as_of cannot be mutated after construction (the decision date is fixed)."""
    ctx = PitContext(as_of=date(2024, 3, 31))
    with pytest.raises((AttributeError, TypeError)):
        ctx.as_of = date(2024, 6, 30)  # type: ignore[misc]
