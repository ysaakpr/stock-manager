"""A10 · M17.6 — the owner's daily M17 digest: ``~/campaign/m17/digest-<date>.md``.

One markdown page per session: where the test stands (S0, the phase, "*k* of 4 passed"), each
manager's §6 numbers against its control and the bench, each book's latest mark, and the session's
decisions. It renders a `Scoreboard` and the session's `DecisionLine` rows and nothing else, so it
holds no rationale, no prompt and no evidence text — what was decided, never what the model read.
A voided decision shows the contract's reason codes (``Refused for``), never the breach messages.

What it never does: read a clock (the session is given), choose its own directory (it is
injected; `DIGEST_DIR` is the default the daily job passes), or leave a half-written page behind
(it writes a sibling temp file and renames it into place).
"""

from __future__ import annotations

from collections.abc import Sequence
from datetime import date
from decimal import Decimal
from pathlib import Path
from typing import Final

from analyst.fundmanager.scoreboard import DecisionLine, ManagerScore, Scoreboard, WindowScore
from dataplatform.logging import get_logger

__all__ = ["DIGEST_DIR", "digest_path", "render_digest", "write_digest"]

_LOG = get_logger(__name__)

#: Where the daily job writes the digest unless told otherwise (outside the repo, never committed).
DIGEST_DIR: Final = Path.home() / "campaign" / "m17"


def digest_path(directory: Path, session: date) -> Path:
    """``<directory>/digest-<YYYY-MM-DD>.md``."""
    return directory / f"digest-{session.isoformat()}.md"


def _num(value: Decimal | int | date | None, suffix: str = "") -> str:
    return "—" if value is None else f"{value}{suffix}"


def _window_row(score: ManagerScore, window: WindowScore | None) -> str:
    if window is None:
        return f"| {score.manager_id} | {score.verdict.value} | — | — | — | — | — | — |"
    cells = (
        score.manager_id,
        score.verdict.value,
        f"{window.label} {window.sessions}/{window.end.isoformat()}",
        _num(window.excess_vs_control_pp, " pp"),
        _num(window.secondary.excess_vs_bench_pp, " pp"),
        f"{_num(window.manager_max_drawdown_pp, ' pp')} vs "
        f"{_num(window.bench_max_drawdown_pp, ' pp')}",
        _num(window.brier),
        str(window.resolved_decisions),
    )
    return "| " + " | ".join(cells) + " |"


def _decision_row(line: DecisionLine) -> str:
    event = "" if line.event is None else f" ({line.event})"
    cells = (
        line.book_id,
        f"{line.decision}{event}",
        line.action or "",
        line.isin or "",
        line.target_weight or "",
        line.p_beat_bench or "",
        line.horizon_sessions or "",
        line.rails or "",
        line.refused_for or "",
    )
    return "| " + " | ".join(cells) + " |"


def render_digest(scoreboard: Scoreboard, session: date, decisions: Sequence[DecisionLine]) -> str:
    """The digest page for ``session`` as markdown text."""
    lines = [
        f"# M17 daily digest — {session.isoformat()}",
        "",
        f"- S0: {_num(scoreboard.s0)} · sessions scored: {scoreboard.sessions_elapsed} · "
        f"latest mark: {_num(scoreboard.as_of)}",
        f"- Result so far: **{scoreboard.k_of_n}** (pre-registration §6; a verdict is final only "
        "at the close of its window)",
        f"- Scoreboard digest: `{scoreboard.digest()[:16]}`",
        "",
        "## Managers",
        "",
        "| Manager | Verdict | Window | Excess vs control | Excess vs bench | Max DD vs bench "
        "| Brier | Resolved |",
        "|---|---|---|---|---|---|---|---|",
    ]
    for score in scoreboard.managers:
        lines.append(_window_row(score, score.extension or score.primary))
    lines += [
        "",
        "## Books",
        "",
        "| Book | Kind | Session | NAV | Return | Cash | Positions |",
        "|---|---|---|---|---|---|---|",
    ]
    for book in scoreboard.books:
        lines.append(
            f"| {book.book_id} | {book.kind} | {_num(book.latest_session)} | {_num(book.nav)} | "
            f"{_num(book.return_pct, ' %')} | {_num(book.cash)} | {_num(book.positions)} |"
        )
    lines += [
        "",
        f"## Decisions on {session.isoformat()}",
        "",
    ]
    if not decisions:
        lines.append("No decision journaled for this session.")
    else:
        lines += [
            "| Book | Decision | Action | ISIN | Target weight | p(beat bench) | Horizon | Rails "
            "| Refused for |",
            "|---|---|---|---|---|---|---|---|---|",
        ]
        lines += [_decision_row(line) for line in decisions]
    return "\n".join(lines) + "\n"


def write_digest(
    scoreboard: Scoreboard,
    session: date,
    decisions: Sequence[DecisionLine],
    *,
    directory: Path,
) -> Path:
    """Write the digest for ``session`` under ``directory`` (created if missing); its path."""
    directory.mkdir(parents=True, exist_ok=True)
    path = digest_path(directory, session)
    tmp = path.with_name(f".{path.name}.tmp")
    tmp.write_text(render_digest(scoreboard, session, decisions), encoding="utf-8")
    tmp.replace(path)
    _LOG.info(
        "fm_digest.written",
        session=session.isoformat(),
        path=str(path),
        scoreboard=scoreboard.digest()[:16],
        decisions=len(decisions),
    )
    return path
