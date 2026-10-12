"""A10 · M17.6 — the owner's daily M17 digest: ``~/campaign/m17/digest-<date>.md``.

One markdown page per session: where the test stands (S0, "*k* of 8 books passed", the Amendment 2
(e) graduation floor), each manager book's §6 numbers against its own control and the bench, each
manager's both/one/neither and its Brier (once, on its decisions), the secondary style books, each
book's latest mark, and the session's decisions. It renders a `Scoreboard` and the session's
`DecisionLine` rows and nothing else, so it holds no rationale, no prompt and no evidence text —
what was decided, never what the model read. A voided decision shows the contract's reason codes
(``Refused for``), never the breach messages. A held name suspended on the session (M17.13) is
listed as "held, not trading since <date>".

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

from analyst.fundmanager.scoreboard import (
    BookScore,
    DecisionLine,
    ManagerResult,
    Scoreboard,
    StyleBookScore,
    SuspendedHoldingLine,
    WindowScore,
)
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


def _window_row(score: BookScore, window: WindowScore | None) -> str:
    head = (score.book_id, score.manager_id, score.role.lower(), score.control_id)
    if window is None:
        return "| " + " | ".join((*head, score.verdict.value, *("—",) * 5)) + " |"
    cells = (
        *head,
        score.verdict.value,
        f"{window.label} {window.sessions}/{window.end.isoformat()}",
        _num(window.excess_vs_control_pp, " pp"),
        _num(window.secondary.excess_vs_bench_pp, " pp"),
        f"{_num(window.manager_max_drawdown_pp, ' pp')} vs "
        f"{_num(window.bench_max_drawdown_pp, ' pp')}",
        str(sum(window.secondary.rail_refusals.values())),
    )
    return "| " + " | ".join(cells) + " |"


def _manager_row(result: ManagerResult) -> str:
    cells = (
        result.manager_id,
        result.primary_verdict.value,
        result.mirror_verdict.value,
        result.passed_on.value.lower(),
        str(result.decisions),
        _num(result.brier),
        _num(result.resolved_decisions),
        _num(result.suspended_resolved_decisions),
        "yes" if result.extension_confirmed else "no",
    )
    return "| " + " | ".join(cells) + " |"


def _style_row(style: StyleBookScore) -> str:
    cells = (
        style.style_id,
        style.primary_book,
        "—" if style.window is None else f"{style.window} {style.sessions}",
        _num(style.style_return_pct, " %"),
        _num(style.style_max_drawdown_pp, " pp"),
        _num(style.primary_return_pct, " %"),
        _num(style.primary_excess_vs_style_pp, " pp"),
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


def render_digest(
    scoreboard: Scoreboard,
    session: date,
    decisions: Sequence[DecisionLine],
    suspended: Sequence[SuspendedHoldingLine] = (),
) -> str:
    """The digest page for ``session`` as markdown text."""
    lines = [
        f"# M17 daily digest — {session.isoformat()}",
        "",
        f"- S0: {_num(scoreboard.s0)} · sessions scored: {scoreboard.sessions_elapsed} · "
        f"latest mark: {_num(scoreboard.as_of)}",
        f"- Result so far: **{scoreboard.k_of_n}** (pre-registration §6 per book, Amendment 2 e; "
        "a verdict is final only at the close of its window)",
        f"- Graduation floor: **{'met' if scoreboard.graduation.met else 'not met'}** — "
        f"{scoreboard.graduation.primary_passes} of {len(scoreboard.managers)} managers pass on "
        f"their primary book (needs {scoreboard.graduation.primary_passes_needed}), or one passes "
        "across the extension window as well"
        + (
            f" ({', '.join(scoreboard.graduation.extension_confirmed)})"
            if scoreboard.graduation.extension_confirmed
            else ""
        ),
        f"- Scoreboard digest: `{scoreboard.digest()[:16]}`",
        "",
        "## Managers",
        "",
        "Brier is the manager's, counted once on its decisions; both its books' verdicts read it.",
        "",
        "| Manager | Primary (10 L) | Mirror (1 cr) | Passed on | Decisions | Brier | Resolved "
        "| Resolved while suspended | Passed across the extension |",
        "|---|---|---|---|---|---|---|---|---|",
    ]
    lines += [_manager_row(result) for result in scoreboard.managers]
    lines += [
        "",
        "## Books against their controls",
        "",
        "| Book | Manager | Role | Control | Verdict | Window | Excess vs control "
        "| Excess vs bench | Max DD vs bench | Rail refusals |",
        "|---|---|---|---|---|---|---|---|---|---|",
    ]
    for score in scoreboard.book_scores:
        lines.append(_window_row(score, score.extension or score.primary))
    lines += [
        "",
        "## Style books (secondary — never used for pass/fail)",
        "",
        "| Style book | Primary | Window | Style return | Style max DD | Primary return "
        "| Primary excess vs style |",
        "|---|---|---|---|---|---|---|",
    ]
    lines += [_style_row(style) for style in scoreboard.style_books]
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
    if suspended:
        lines += [
            "",
            "## Suspended holdings",
            "",
            "| Book | ISIN | Status | Sessions suspended |",
            "|---|---|---|---|",
        ]
        lines += [
            f"| {h.book_id} | {h.isin} | held, not trading since {h.last_trade_date.isoformat()} "
            f"| {h.sessions_suspended} |"
            for h in suspended
        ]
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
    suspended: Sequence[SuspendedHoldingLine] = (),
) -> Path:
    """Write the digest for ``session`` under ``directory`` (created if missing); its path."""
    directory.mkdir(parents=True, exist_ok=True)
    path = digest_path(directory, session)
    tmp = path.with_name(f".{path.name}.tmp")
    tmp.write_text(render_digest(scoreboard, session, decisions, suspended), encoding="utf-8")
    tmp.replace(path)
    _LOG.info(
        "fm_digest.written",
        session=session.isoformat(),
        path=str(path),
        scoreboard=scoreboard.digest()[:16],
        decisions=len(decisions),
        suspended=len(suspended),
    )
    return path
