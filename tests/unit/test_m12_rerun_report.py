"""M12.R report: old figures parse as struck, and ranks compare within one arm set."""

from __future__ import annotations

from decimal import Decimal
from pathlib import Path

from backtest.m12_rerun_report import (
    D13,
    HIGH,
    LOW,
    SWEEP_SET,
    Ladder,
    OldFigure,
    Row,
    WindowResult,
    parse_duration_report,
    parse_sweep_report,
    parse_verdict_report,
    render,
)

GATES = Path(__file__).resolve().parents[2] / "ops" / "gates"


def _row(label: str, xirr: str, dd: str) -> Row:
    x, d = Decimal(xirr), Decimal(dd)
    return Row(label, "f", True, None, x, d, x / d, 10, 20, Decimal("1000"), Decimal("0.01"))


def _window(name: str, rows: list[Row]) -> WindowResult:
    ranked = sorted(rows, key=lambda r: -r.ratio)
    w = WindowResult(name, "2016-09-02", "2026-08-31", 2470, "Nifty 50", Decimal("0.12"))
    w.floors = {LOW: ranked, HIGH: ranked}
    return w


def test_old_reports_parse_as_struck() -> None:
    decade = parse_sweep_report(GATES / "M12-strategy-sweep-decade.md", "decade")
    assert len(decade) == 46
    m107 = next(f for f in decade if f.label == "Swing composite (M10.7)" and f.floor == LOW)
    assert (m107.rank, m107.xirr, m107.ratio) == (1, Decimal("0.1711"), Decimal("0.69"))
    verdict = parse_verdict_report(GATES / "M12-strategy-verdict.md")
    chosen = next(f for f in verdict if f.label == "M10.7 + regime gate")
    assert chosen.window == "wf-selection" and chosen.rank == 1
    duration = parse_duration_report(GATES / "M12-swing-duration-window-report.md")
    assert {f.window for f in duration} == {
        "decade",
        "six-year",
        "wf-selection",
        "wf-verification",
    }
    assert len(duration) == 13 * 2 * 4


def test_a_row_does_not_move_because_arms_were_added_around_it() -> None:
    # Old set: A then B. New run adds C above both; A and B keep their order and XIRR.
    windows = {
        "decade": _window(
            "decade",
            [_row("A", "0.20", "0.20"), _row("B", "0.10", "0.20"), _row("C", "0.30", "0.10")],
        )
    }
    old = [
        OldFigure("x.md", SWEEP_SET, "decade", LOW, "A", 1, Decimal("0.20"), None, None),
        OldFigure("x.md", SWEEP_SET, "decade", LOW, "B", 2, Decimal("0.10"), None, None),
        OldFigure("x.md", SWEEP_SET, "decade", LOW, "B2", 3, Decimal("0.05"), None, None),
    ]
    text = render(windows, "", old, Ladder(), facts=[])
    moved = text.split("## What moved since 2026-09-07")[1]
    assert "| A |" not in moved and "| B |" not in moved


def test_a_move_is_listed_with_its_measured_cause() -> None:
    windows = {"decade": _window("decade", [_row("A", "0.15", "0.20"), _row(D13, "0.12", "0.2")])}
    old = [OldFigure("x.md", SWEEP_SET, "decade", LOW, "A", 1, Decimal("0.20"), None, None)]
    ladder = Ladder(
        reference={("decade", LOW, "A"): Decimal("0.19")},
        middle={("decade", LOW, "A"): Decimal("0.14")},
        middle_label="lake #74/#75, engine since",
        last_label="cash interest",
    )
    text = render(windows, "", old, ladder, facts=[])
    assert "| A | 20.00% | 15.00% | -5.00 |" in text
    assert "mostly lake #74/#75, engine since" in text
    # The paper configuration is marked in every table it appears in.
    assert f"**{D13}** ◆" in text


def test_without_the_no_interest_run_the_ladder_has_two_rungs() -> None:
    windows = {"decade": _window("decade", [_row("A", "0.15", "0.20")])}
    old = [OldFigure("x.md", SWEEP_SET, "decade", LOW, "A", 1, Decimal("0.20"), None, None)]
    ladder = Ladder(reference={("decade", LOW, "A"): Decimal("0.16")})
    text = render(windows, "", old, ladder, facts=[])
    assert "mostly engine to 2026-09-28 (engine to 2026-09-28 -4.00, everything since" in text


def test_universe_check_is_reported_apart_from_the_attribution() -> None:
    # A NIFTY 500 run is not a rung: the move is split old -> reference -> today, both floor-only.
    windows = {"six-year": _window("six-year", [_row(D13, "0.30", "0.20")])}
    check = {"six-year": _window("six-year", [_row(D13, "0.25", "0.20")])}
    old = [OldFigure("x.md", SWEEP_SET, "six-year", LOW, D13, 1, Decimal("0.20"), None, None)]
    ladder = Ladder(reference={("six-year", LOW, D13): Decimal("0.22")})
    text = render(windows, "", old, ladder, facts=[], universe_check=check)
    assert "## Universe check" in text
    assert "| +5.00 |" in text  # floor-only 30 % minus NIFTY 500 25 %
    assert "engine to 2026-09-28 +2.00, everything since 2026-09-28 +8.00 pp" in text
    assert (
        "universe"
        not in text.split("## What moved")[1].split("## Universe check")[0].split("| --- |")[-1]
    )
