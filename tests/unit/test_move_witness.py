"""D7: the PR-bundle witness annotates an unexplained move and never suppresses it.

The pair that matters: with a `corp_ind:XB` witness the sentinel returns *both* the ERROR
`unexplained_move` finding and an INFO `unexplained_move_witness` finding. A rule that resolved
or swallowed the flag instead would fail `test_a_witness_annotates_and_never_suppresses`.
Offline: `read_move_witnesses` is tested against parquet written into `tmp_path`.
"""

from __future__ import annotations

from datetime import date
from decimal import Decimal
from pathlib import Path
from typing import Final

import pyarrow as pa
import pyarrow.parquet as pq
import pytest

from dataplatform.quality import CloseToCloseMove, SentinelInput, default_rules, run_sentinel
from dataplatform.quality.pr_witnesses import read_move_witnesses
from dataplatform.quality.rules.move_witness import (
    MOVE_WITNESS_CHECK,
    MoveWitnessRule,
    applicable_witnesses,
    best_strength,
    witness_strength,
)
from dataplatform.quality.rules.unexplained_move import UNEXPLAINED_MOVE_CHECK
from dataplatform.store.paths import l1_partition_path

INFY: Final = "INE009A01021"
SESSION: Final = date(2015, 6, 15)


def _move(prev: str, close: str) -> CloseToCloseMove:
    return CloseToCloseMove(
        isin=INFY,
        date=SESSION,
        prev_close=Decimal(prev),
        close=Decimal(close),
        source="prices_raw",
    )


def _run(move: CloseToCloseMove, *witnesses: str) -> dict[str, list[object]]:
    data = SentinelInput(moves=(move,), move_witnesses={(INFY, SESSION): witnesses})
    out: dict[str, list[object]] = {}
    for finding in run_sentinel(data):
        out.setdefault(finding.check_name, []).append(finding)
    return out


def test_the_rule_self_registers() -> None:
    assert MOVE_WITNESS_CHECK in {rule.name for rule in default_rules()}


def test_a_witness_annotates_and_never_suppresses() -> None:
    found = _run(_move("100", "50"), "corp_ind:XB")  # -50% on a bonus ex-date
    assert len(found[UNEXPLAINED_MOVE_CHECK]) == 1  # the ERROR stays
    (note,) = found[MOVE_WITNESS_CHECK]
    assert note.severity == "INFO"  # type: ignore[attr-defined]
    assert note.detail["witnesses"] == ["corp_ind:XB"]  # type: ignore[attr-defined]
    assert note.detail["strength"] == ["strong"]  # type: ignore[attr-defined]
    assert note.detail["resolves"] is False  # type: ignore[attr-defined]


def test_no_witness_means_no_annotation() -> None:
    found = _run(_move("100", "50"))
    assert set(found) == {UNEXPLAINED_MOVE_CHECK}


def test_a_move_the_move_rule_does_not_flag_is_not_annotated() -> None:
    assert _run(_move("100", "110"), "corp_ind:XB") == {}


def test_a_band_hit_counts_only_on_its_own_side() -> None:
    up = _move("100", "130")
    assert MOVE_WITNESS_CHECK not in _run(up, "band_hit:L")
    (note,) = _run(up, "band_hit:H", "band_hit:L")[MOVE_WITNESS_CHECK]
    assert note.detail["witnesses"] == ["band_hit:H"]  # type: ignore[attr-defined]
    assert applicable_witnesses(["band_hit:H", "band_hit:L"], Decimal("-0.3")) == ("band_hit:L",)


@pytest.mark.parametrize(
    ("witness", "strength"),
    [
        ("corp_ind:XB", "strong"),
        ("corp_ind:XR", "strong"),
        ("corp_ind:XO", "other"),
        ("corp_ind:XDO", "other"),
        ("corp_ind:XDBO", "strong"),
        ("corp_ind:XD", "weak"),
        ("corp_ind:XI", "weak"),
        ("band_hit:H", "band"),
        ("ca_broadcast:SPLIT", "strong"),
        ("ca_broadcast:BONUS", "strong"),
        ("ca_broadcast:BUYBACK", "other"),
        ("ca_broadcast:DIVIDEND", "weak"),
        ("ca_broadcast:OTHER", "weak"),
    ],
)
def test_witness_strength(witness: str, strength: str) -> None:
    assert witness_strength(witness) == strength


def test_an_unknown_witness_raises() -> None:
    with pytest.raises(ValueError, match="unknown move witness"):
        witness_strength("rumour:yes")
    with pytest.raises(ValueError, match="unknown move witness"):
        witness_strength("band_hit:X")


def test_best_strength_orders_strong_band_other_weak() -> None:
    assert best_strength(["corp_ind:XD", "band_hit:H"]) == "band"
    assert best_strength(["corp_ind:XO", "corp_ind:XD"]) == "other"
    assert best_strength(["band_hit:H", "ca_broadcast:SPLIT"]) == "strong"


def test_threshold_is_a_construction_argument() -> None:
    strict = MoveWitnessRule(threshold=Decimal("0.05"))
    data = SentinelInput(
        moves=(_move("100", "110"),), move_witnesses={(INFY, SESSION): ("corp_ind:XD",)}
    )
    assert len(list(strict.evaluate(data))) == 1


def _write(
    root: Path, dataset: str, on: date, schema: pa.Schema, rows: list[dict[str, object]]
) -> None:
    path = l1_partition_path(dataset, on, data_root=root)
    path.parent.mkdir(parents=True, exist_ok=True)
    pq.write_table(pa.Table.from_pylist(rows, schema=schema), path)


def test_read_move_witnesses_reads_all_three_datasets_point_in_time(tmp_path: Path) -> None:
    marks = pa.schema([("isin", pa.string()), ("session", pa.date32()), ("corp_ind", pa.string())])
    hits = pa.schema([("isin", pa.string()), ("session", pa.date32()), ("side", pa.string())])
    bc = pa.schema(
        [
            ("isin", pa.string()),
            ("knowable_date", pa.date32()),
            ("ex_date", pa.date32()),
            ("purpose", pa.string()),
        ]
    )
    _write(
        tmp_path,
        "pr_security_marks",
        SESSION,
        marks,
        [
            {"isin": INFY, "session": SESSION, "corp_ind": "XDBO"},
            {"isin": "INE467B01029", "session": SESSION, "corp_ind": None},
        ],
    )
    _write(
        tmp_path, "pr_band_hits", SESSION, hits, [{"isin": INFY, "session": SESSION, "side": "L"}]
    )
    _write(
        tmp_path,
        "pr_ca_broadcasts",
        date(2015, 6, 1),
        bc,
        [
            {
                "isin": INFY,
                "knowable_date": date(2015, 6, 1),
                "ex_date": SESSION,
                "purpose": "BONUS 1:1 AND DIVIDEND RS 29.50",
            },
            # broadcast after its own ex-date: not knowable when the move happened
            {
                "isin": "INE467B01029",
                "knowable_date": date(2015, 6, 20),
                "ex_date": SESSION,
                "purpose": "SPLIT",
            },
        ],
    )
    found = read_move_witnesses(SESSION, SESSION, data_root=tmp_path)
    assert found == {
        (INFY, SESSION): (
            "band_hit:L",
            "ca_broadcast:BONUS",
            "ca_broadcast:DIVIDEND",
            "corp_ind:XDBO",
        )
    }


def test_read_move_witnesses_on_an_unbuilt_lake_is_empty(tmp_path: Path) -> None:
    assert read_move_witnesses(SESSION, SESSION, data_root=tmp_path) == {}


def test_nses_abbreviated_face_value_split_is_tagged_a_split() -> None:
    """`FVSPLT`/`FV SPLT` was untagged before 2026-10-06, so a split ex-date read as `OTHER`."""
    from dataplatform.ingest.nse.pr_bundle.survey import purpose_tags

    assert "SPLIT" in purpose_tags("FVSPLT FRM RS 10 TO RE 1")
    assert "SPLIT" in purpose_tags("FV SPLT FRM RS 10 TO RS 2")
    assert witness_strength("ca_broadcast:SPLIT") == "strong"
