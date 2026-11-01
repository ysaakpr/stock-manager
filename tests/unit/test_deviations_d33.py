"""D33: the share-basis flags of the guard's first historical sweep show as accepted, new ones red.

The owner's ruling (2026-11-02): an `l2_continuity` share-basis step, or a quarantine the
`share_basis_guard` raised itself, about a step dated before 2026-11-01 comes from the first
full-history sweep (PR #160) and shows as a warning with the remark; one dated on or after
2026-11-01 stays an ERROR, and so does any other kind of ERROR either check raises. The flags here
have the shape the live rows have (`quality.l2_continuity.findings`, `share_basis.guard_findings`).
"""

from __future__ import annotations

from datetime import date, datetime
from pathlib import Path
from typing import Any, Final, cast

import pytest

from dataplatform.clock import IST
from dataplatform.status.deviations import (
    AlertEntry,
    DeviationJudge,
    DeviationKind,
    DeviationRegistryError,
    DeviationStatus,
    OpenFlag,
    QualityEntry,
    load_deviations,
)
from dataplatform.status.models import QualityFlagOut, QualitySeverity
from dataplatform.status.queries import _flag_verdict
from dataplatform.status.sync_state import SyncStateStore
from dataplatform.store.db import Connection

REGISTRY: Final = load_deviations()
CONTINUITY: Final = "l2_continuity"
GUARD: Final = "share_basis_guard"
TODAY: Final = date(2026, 11, 2)
CUTOFF: Final = date(2026, 11, 1)
#: When the first sweep raised them: 2026-11-01 21:01 IST.
SWEPT: Final = datetime(2026, 11, 1, 21, 1, 26, tzinfo=IST)
GUARD_KINDS: Final = ("stated_terms_disagree", "reissue_face_value_disagrees")


class _Rows:
    def __init__(self, rows: list[tuple[Any, ...]]) -> None:
        self._rows = rows

    def fetchall(self) -> list[tuple[Any, ...]]:
        return self._rows


class _Flags:
    """The open ERROR `quality_flag` rows of each check, as the judge reads them."""

    def __init__(self, by_check: dict[str, list[OpenFlag]]) -> None:
        self._by_check = by_check

    def execute(self, sql: str, params: tuple[object, ...] = ()) -> _Rows:
        assert "FROM quality_flag" in sql
        flags = self._by_check.get(str(params[0]), [])
        return _Rows(
            [(f.id, f.fingerprint, f.isin, f.logical_date, f.raised_at, f.kind) for f in flags]
        )


def _judge(by_check: dict[str, list[OpenFlag]] | None = None) -> DeviationJudge:
    conn = _Flags(by_check or {})
    return DeviationJudge(REGISTRY, cast(Connection, conn), cast(SyncStateStore, object()), TODAY)


def _flag(kind: str | None, on: date, *, flag_id: int = 1, raised: datetime = SWEPT) -> OpenFlag:
    return OpenFlag(
        id=flag_id, fingerprint="f" * 32, isin="INE000A01010", logical_date=on, raised_at=raised,
        kind=kind,
    )  # fmt: skip


def _accepted_id(check: str, flag: OpenFlag) -> str | None:
    verdict = _judge().quality_flag(check, flag)
    return verdict.id if verdict is not None and verdict.accepted else None


# ── the entries ────────────────────────────────────────────────────────────────────────────


def test_d33_is_a_class_and_a_date_bound_and_nothing_broader() -> None:
    continuity = REGISTRY.get("d33-share-basis-history-continuity")
    guard = REGISTRY.get("d33-share-basis-history-guard")
    assert isinstance(continuity, QualityEntry) and isinstance(guard, QualityEntry)
    assert continuity.target == CONTINUITY and guard.target == GUARD
    assert continuity.match.kinds == frozenset({"share_basis_step"})
    assert guard.match.kinds == frozenset(GUARD_KINDS)
    for entry in (continuity, guard):
        assert entry.match.dated_before == CUTOFF
        assert entry.match.fingerprints is None and entry.match.ids is None
        assert entry.match.isins is None and entry.match.raised_before is None
        assert entry.review_by is None  # the owner set no review date
        assert entry.basis.agreed_by == "owner" and entry.basis.agreed_on == date(2026, 11, 2)
        assert any("D33" in line for line in entry.basis.recorded_in)


def test_the_remark_says_where_the_flag_came_from_and_what_it_means() -> None:
    remark = REGISTRY.get("d33-share-basis-history-continuity").remark
    assert "first historical sweep" in remark
    assert "quarantined (withheld from decisions)" in remark
    assert "clears" in remark and "automatically" in remark
    assert "corporate-action record" in remark
    assert REGISTRY.get("d33-share-basis-history-guard").remark == remark


def test_no_other_entry_targets_either_check() -> None:
    ids = {
        entry.id
        for check in (CONTINUITY, GUARD)
        for entry in REGISTRY.entries(DeviationKind.QUALITY, check)
    }
    assert ids == {"d33-share-basis-history-continuity", "d33-share-basis-history-guard"}


# ── pre- and post-cutoff flags ─────────────────────────────────────────────────────────────


@pytest.mark.parametrize("on", [date(2011, 8, 25), date(2026, 8, 17), date(2026, 10, 31)])
def test_a_share_basis_step_dated_before_the_cutoff_is_accepted(on: date) -> None:
    verdict = _judge().quality_flag(CONTINUITY, _flag("share_basis_step", on))
    assert verdict is not None and verdict.status is DeviationStatus.ACCEPTED
    assert verdict.id == "d33-share-basis-history-continuity"
    assert "first historical sweep" in verdict.remark


@pytest.mark.parametrize("on", [CUTOFF, date(2026, 11, 2), date(2027, 3, 1)])
def test_a_share_basis_step_dated_on_or_after_the_cutoff_stays_an_error(on: date) -> None:
    assert _judge().quality_flag(CONTINUITY, _flag("share_basis_step", on)) is None


def test_the_bound_is_the_steps_date_not_when_the_flag_was_raised() -> None:
    # Raised by the sweep before the ruling, but about a step dated after the cutoff: red.
    late_step = _flag("share_basis_step", date(2026, 11, 3), raised=SWEPT)
    assert _accepted_id(CONTINUITY, late_step) is None
    # Raised later (a rebuild re-raising an old step), about a step before the cutoff: accepted.
    old_step = _flag("share_basis_step", date(2015, 1, 5), raised=datetime(2026, 12, 1, tzinfo=IST))
    assert _accepted_id(CONTINUITY, old_step) == "d33-share-basis-history-continuity"


@pytest.mark.parametrize("kind", ["unexplained_step", None, "stated_terms_disagree"])
def test_any_other_continuity_error_stays_an_error_whatever_its_date(kind: str | None) -> None:
    assert _judge().quality_flag(CONTINUITY, _flag(kind, date(2015, 1, 5))) is None


@pytest.mark.parametrize("kind", GUARD_KINDS)
def test_a_guard_quarantine_before_the_cutoff_is_accepted_and_after_it_is_not(kind: str) -> None:
    assert _accepted_id(GUARD, _flag(kind, date(2024, 1, 1))) == "d33-share-basis-history-guard"
    assert _accepted_id(GUARD, _flag(kind, CUTOFF)) is None
    assert _accepted_id(GUARD, _flag(kind, date(2026, 11, 20))) is None


def test_a_guard_flag_of_another_kind_stays_an_error() -> None:
    assert _accepted_id(GUARD, _flag("share_basis_step", date(2024, 1, 1))) is None
    assert _accepted_id(GUARD, _flag(None, date(2024, 1, 1))) is None


# ── the check, its alert and the Ops listing ───────────────────────────────────────────────


def _sweep() -> dict[str, list[OpenFlag]]:
    return {
        CONTINUITY: [
            _flag("share_basis_step", date(2011, 8, 25), flag_id=1),
            _flag("share_basis_step", date(2026, 8, 17), flag_id=2),
        ],
        GUARD: [
            _flag("stated_terms_disagree", date(2025, 8, 11), flag_id=3),
            _flag("reissue_face_value_disagrees", date(2021, 8, 24), flag_id=4),
        ],
    }


def test_the_sweep_alone_turns_both_checks_and_their_alerts_amber() -> None:
    judge = _judge(_sweep())
    for check in (CONTINUITY, GUARD):
        assert judge.quality(check) is not None
        assert judge.quality_accepted(check) == 2
        alert = judge.alert(f"quality:{check}:red")
        assert alert is not None and alert.accepted


def test_a_new_step_keeps_the_check_and_its_alert_red_beside_the_accepted_ones() -> None:
    flags = _sweep()
    flags[CONTINUITY].append(_flag("share_basis_step", date(2026, 11, 4), flag_id=5))
    flags[GUARD].append(_flag("stated_terms_disagree", date(2026, 11, 4), flag_id=6))
    judge = _judge(flags)
    for check in (CONTINUITY, GUARD):
        assert judge.quality(check) is None
        assert judge.quality_accepted(check) == 2  # the old ones still show accepted
        assert judge.alert(f"quality:{check}:red") is None  # the alert stays critical


def test_the_alert_entries_follow_their_quality_entries() -> None:
    for check, suffix in ((CONTINUITY, "continuity"), (GUARD, "guard")):
        (alert,) = REGISTRY.entries(DeviationKind.ALERT, f"quality:{check}:red")
        assert isinstance(alert, AlertEntry)
        assert alert.match.while_accepted == f"d33-share-basis-history-{suffix}"


def test_the_ops_listing_reads_the_kind_from_the_flags_detail() -> None:
    def listed(on: date) -> QualityFlagOut:
        return QualityFlagOut(
            id=7, date=on, check_name=CONTINUITY, severity=QualitySeverity.ERROR,
            isin="INE000A01010", detail={"kind": "share_basis_step", "fingerprint": "f" * 32},
            raised_at=SWEPT,
        )  # fmt: skip

    old = _flag_verdict(listed(date(2019, 6, 3)), _judge())
    assert old.deviation is not None and old.deviation.id == "d33-share-basis-history-continuity"
    assert _flag_verdict(listed(CUTOFF), _judge()).deviation is None


# ── the matcher ────────────────────────────────────────────────────────────────────────────


def test_a_kind_alone_is_refused(tmp_path: Path) -> None:
    path = tmp_path / "deviations.yaml"
    path.write_text(
        """version: 1
deviations:
  - id: too-broad
    kind: quality
    target: l2_continuity
    match:
      kinds: [share_basis_step]
    remark: every share-basis step, forever
    basis: {agreed_by: owner, agreed_on: 2026-11-02, recorded_in: [x]}
    since: 2026-11-01
"""
    )
    with pytest.raises(DeviationRegistryError, match="kinds alone"):
        load_deviations(path)
