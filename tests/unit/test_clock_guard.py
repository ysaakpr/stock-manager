"""B10 / invariant #11: the wall clock is read in exactly one file.

`dataplatform/clock.py` reads the host clock; every other module takes an injected `Clock`. That
is what makes a replay byte-reproducible (§8.3.3) and what stops a component from quietly deciding
that "today" is the day the code happens to run rather than the day being reasoned about (§8.3.6).

A convention nobody checks is a convention that lasts one milestone, so this scans the repo. It
works on tokenized source with comments and string literals blanked out, which means prose about
wall-clock calls (this docstring, for one) is not a violation, while the same text as code is.

Two tests keep the guard itself honest: one proves it flags a call introduced into a real repo
file, the other proves the allowlisted module genuinely contains the call it is allowlisted for —
so the guard cannot pass because the scanner silently stopped matching anything.
"""

from __future__ import annotations

import io
import re
import tokenize
from collections.abc import Iterator
from pathlib import Path
from typing import NamedTuple

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]

#: The only file allowed to read the host clock, relative to the repo root.
ALLOWED = frozenset({"dataplatform/clock.py"})

#: `orchestrator/` is the build machinery, not product code: it is excluded from ruff and mypy for
#: the same reason (pyproject.toml, M0.1) and none of its timestamps reach a trading decision.
#: Hidden directories (`.venv`, `.git`, caches) are pruned separately.
SKIPPED_DIRS = frozenset({"__pycache__", "build", "data", "dist", "node_modules", "orchestrator"})

#: Wall-clock calls, by the receiver they are called on. The leading `(?<![\w.])(?:[A-Za-z_]\w*\.)*`
#: lets an alias through (`dt.datetime.now()`, `pd.Timestamp.now()`) while refusing to match a name
#: that merely ends in the receiver — `datetime.time(9, 15)` builds a time, it does not read one.
#: `time.monotonic()` and `perf_counter()` are deliberately absent: they measure an elapsed
#: interval, which is not a date and cannot leak into a decision.
_RECEIVER = r"(?<![\w.])(?:[A-Za-z_]\w*\.)*"
FORBIDDEN: tuple[re.Pattern[str], ...] = (
    re.compile(_RECEIVER + r"datetime\.(?:now|utcnow|today)\s*\("),
    re.compile(_RECEIVER + r"date\.today\s*\("),
    re.compile(_RECEIVER + r"time\.time\s*\("),
    re.compile(_RECEIVER + r"Timestamp\.(?:now|today|utcnow)\s*\("),
)


class Violation(NamedTuple):
    """One wall-clock call outside `dataplatform/clock.py`."""

    name: str
    line: int
    text: str

    def __str__(self) -> str:
        return f"{self.name}:{self.line}: {self.text.strip()}"


def code_lines(source: str) -> list[str]:
    """Return `source`'s lines with comments and string literals blanked to spaces.

    Blanking rather than deleting keeps line and column numbers intact, so a violation still
    reports where it is. Replacement fields inside f-strings survive, because those are code.
    """
    lines = source.splitlines()
    ignored = {tokenize.COMMENT, tokenize.STRING, tokenize.FSTRING_MIDDLE}

    def blank(row: int, start: int, end: int) -> None:
        line = lines[row - 1]
        lines[row - 1] = line[:start] + " " * (end - start) + line[end:]

    for token in tokenize.generate_tokens(io.StringIO(source).readline):
        if token.type not in ignored:
            continue
        (start_row, start_col), (end_row, end_col) = token.start, token.end
        if start_row == end_row:
            blank(start_row, start_col, end_col)
            continue
        blank(start_row, start_col, len(lines[start_row - 1]))
        for row in range(start_row + 1, end_row):
            lines[row - 1] = " " * len(lines[row - 1])
        blank(end_row, 0, end_col)
    return lines


def scan_source(source: str, *, name: str) -> list[Violation]:
    """Every wall-clock call in `source`, ignoring comments and string literals."""
    return [
        Violation(name, number, line)
        for number, line in enumerate(code_lines(source), start=1)
        for pattern in FORBIDDEN
        if pattern.search(line)
    ]


def scan_file(path: Path) -> list[Violation]:
    """Every wall-clock call in one file, named relative to the repo root when it is inside it."""
    name = path.as_posix()
    if path.is_relative_to(REPO_ROOT):
        name = path.relative_to(REPO_ROOT).as_posix()
    return scan_source(path.read_text(encoding="utf-8"), name=name)


def python_files(root: Path) -> Iterator[Path]:
    """Every `.py` file under `root` that is product, test or ops code."""
    for parent, dirnames, filenames in root.walk():
        dirnames[:] = [d for d in dirnames if d not in SKIPPED_DIRS and not d.startswith(".")]
        for filename in sorted(filenames):
            if filename.endswith(".py"):
                yield parent / filename


def test_wall_clock_is_read_only_in_the_clock_module() -> None:
    """The invariant itself: nothing outside `dataplatform/clock.py` reads the host clock."""
    scanned = list(python_files(REPO_ROOT))
    assert len(scanned) > 10, (
        f"the walk found only {len(scanned)} files — this would pass vacuously"
    )

    violations = [
        violation
        for path in scanned
        if path.relative_to(REPO_ROOT).as_posix() not in ALLOWED
        for violation in scan_file(path)
    ]
    assert not violations, "inject a Clock instead of reading the wall clock:\n" + "\n".join(
        str(violation) for violation in violations
    )


def test_the_allowlisted_module_really_does_read_the_clock() -> None:
    """A vacuous allowlist would hide a scanner that has quietly stopped matching anything."""
    assert scan_file(REPO_ROOT / "dataplatform" / "clock.py"), (
        "dataplatform/clock.py no longer contains a wall-clock call: either the clock moved and "
        "ALLOWED is stale, or the patterns in this file stopped matching"
    )


def test_a_call_introduced_into_a_real_repo_file_is_caught(tmp_path: Path) -> None:
    """The acceptance criterion, mechanically: a real module plus one wall-clock call fails."""
    clean = (REPO_ROOT / "dataplatform" / "config.py").read_text(encoding="utf-8")
    assert scan_source(clean, name="dataplatform/config.py") == []

    offender = tmp_path / "config.py"
    offender.write_text(clean + "\n_started_at = " + "datetime.now()\n", encoding="utf-8")
    caught = scan_file(offender)
    assert [violation.line for violation in caught] == [len(clean.splitlines()) + 2]


@pytest.mark.parametrize(
    "snippet",
    [
        "from datetime import datetime\nx = datetime.now()\n",
        "import datetime\nx = datetime.datetime.now(tz=None)\n",
        "import datetime as dt\nx = dt.datetime.utcnow()\n",
        "from datetime import date\nd = date.today()\n",
        "import datetime\nd = datetime.date.today()\n",
        "import time\nt = time.time()\n",
        "import pandas as pd\nts = pd.Timestamp.now()\n",
        "def f():\n    return {'as_of': datetime.now()}\n",
    ],
)
def test_every_forbidden_form_is_caught(snippet: str) -> None:
    assert scan_source(snippet, name="offender.py"), snippet


@pytest.mark.parametrize(
    "snippet",
    [
        # An injected clock is the whole point.
        "def f(clock):\n    return clock.now()\n",
        "def f(self):\n    return self._clock.today()\n",
        # Elapsed intervals are not dates.
        "import time\nstarted = time.monotonic()\n",
        "import time\nstarted = time.perf_counter()\n",
        # Constructors and parsers that merely look like the forbidden forms.
        "import datetime\nopen_ = datetime.time(9, 15)\n",
        "import pandas as pd\nts = pd.Timestamp('2026-08-07')\n",
        "import datetime\nd = datetime.date(2026, 8, 7)\n",
    ],
)
def test_legitimate_time_handling_is_not_flagged(snippet: str) -> None:
    assert scan_source(snippet, name="innocent.py") == [], snippet


def test_prose_about_wall_clock_calls_is_not_a_violation() -> None:
    """Docstrings and comments have to be able to name the thing they forbid — this file does."""
    snippet = (
        '"""Never call datetime.now() here."""\n'
        "# date.today() is banned too\n"
        "URL = 'https://example.test/?ts=time.time()'\n"
    )
    assert scan_source(snippet, name="prose.py") == []
