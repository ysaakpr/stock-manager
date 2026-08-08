"""HUMAN_DECISIONS.md: the append-only queue of things agents are not allowed to decide.

One entry per blocked task. The entry has to be decidable by reading it alone -- the
owner should never have to reconstruct context from a transcript. See AGENTIC_CONTEXT.md §3.
"""

from __future__ import annotations

import re
from datetime import UTC, datetime
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
QUEUE_PATH = REPO / "HUMAN_DECISIONS.md"

HEADER = """# Human Decisions Queue

Append-only. Each entry is a decision an agent is not permitted to make
([AGENTIC_CONTEXT.md](AGENTIC_CONTEXT.md) §3), written to be decidable without reading any
transcript.

**To answer one:**

```bash
./orch answer <task-id> --decision "your call, in a sentence"
```

That records the decision, marks the entry ANSWERED, and returns the task to the build queue
so the next wave picks it up. Nothing else is needed.

---
"""


def _now() -> str:
    return datetime.now(UTC).strftime("%Y-%m-%d %H:%M UTC")


def _ensure_header(path: Path) -> None:
    if not path.exists() or not path.read_text().strip():
        path.write_text(HEADER)


def add(
    task_id: str,
    *,
    title: str,
    question: str,
    why_blocked: str,
    options: str = "",
    recommendation: str = "",
    unblocks: str = "",
) -> bool:
    """Append an OPEN entry. Returns False if this task already has an open entry."""
    _ensure_header(QUEUE_PATH)
    text = QUEUE_PATH.read_text()
    if re.search(rf"^### \[{re.escape(task_id)}\].*\n(?:.*\n)*?- \*\*Status:\*\* OPEN", text, re.M):
        return False

    block = [f"\n### [{task_id}] {title}", "", "- **Status:** OPEN", f"- **Opened:** {_now()}"]
    if unblocks:
        block.append(f"- **Unblocks:** {unblocks}")
    block += [
        "",
        f"**Decision needed:** {question}",
        "",
        f"**Why an agent can't decide this:** {why_blocked}",
    ]
    if options:
        block += ["", "**Options:**", ""]
        block += [f"- {opt.strip()}" for opt in options.split("|") if opt.strip()]
    if recommendation:
        block += ["", f"**Agent recommendation:** {recommendation}"]
    block += ["", "**Your decision:** _(unanswered)_", "", "---"]

    with QUEUE_PATH.open("a") as fh:
        fh.write("\n".join(block) + "\n")
    return True


def answer(task_id: str, decision: str) -> bool:
    """Mark the newest open entry for a task ANSWERED and record the decision inline."""
    if not QUEUE_PATH.exists():
        return False
    text = QUEUE_PATH.read_text()
    blocks = text.split("\n### ")
    for i in range(len(blocks) - 1, 0, -1):
        if blocks[i].startswith(f"[{task_id}]") and "**Status:** OPEN" in blocks[i]:
            blocks[i] = blocks[i].replace("**Status:** OPEN", f"**Status:** ANSWERED {_now()}", 1)
            blocks[i] = blocks[i].replace(
                "**Your decision:** _(unanswered)_", f"**Your decision:** {decision}", 1
            )
            QUEUE_PATH.write_text("\n### ".join(blocks))
            return True
    return False


def open_entries() -> list[str]:
    """Task ids with an open decision, for the run loop's stop report."""
    if not QUEUE_PATH.exists():
        return []
    out = []
    for block in QUEUE_PATH.read_text().split("\n### ")[1:]:
        if "**Status:** OPEN" in block:
            match = re.match(r"\[([^\]]+)\]", block)
            if match:
                out.append(match.group(1))
    return out
