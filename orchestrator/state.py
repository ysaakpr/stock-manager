"""BUILD_STATE.json: what has actually happened, with concurrency-safe transitions.

Several builder agents run at once and each records its own outcome, so every write
takes an exclusive file lock and re-reads before merging. Losing a state write means
a task silently re-runs or a wave deadlocks, so this is deliberately paranoid.
"""

from __future__ import annotations

import contextlib
import fcntl
import json
import os
import tempfile
from collections.abc import Iterator
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

REPO = Path(__file__).resolve().parent.parent
STATE_PATH = REPO / "BUILD_STATE.json"
LOCK_PATH = REPO / ".build_state.lock"

# PENDING  never attempted
# IN_PROGRESS an agent holds it
# DONE     acceptance verified (the only state downstream tasks may trust)
# FAILED   attempt failed; retryable until MAX_ATTEMPTS
# PARKED   waiting on a human (see HUMAN_DECISIONS.md)
# SPLIT    replaced by child tasks
TERMINAL = {"DONE", "PARKED", "SPLIT"}
VALID = {"PENDING", "IN_PROGRESS", "DONE", "FAILED", "PARKED", "SPLIT"}

VERIFY_OUTPUT_LIMIT = 4000


def _now() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds")


@contextlib.contextmanager
def _locked() -> Iterator[None]:
    LOCK_PATH.touch(exist_ok=True)
    with LOCK_PATH.open("r+") as fh:
        fcntl.flock(fh, fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(fh, fcntl.LOCK_UN)


class BuildState:
    def __init__(self, path: Path = STATE_PATH) -> None:
        self.path = path

    # ── io ───────────────────────────────────────────────────────────────────

    def _read(self) -> dict[str, Any]:
        if not self.path.exists():
            return {"version": 1, "updated_at": _now(), "wave": 0, "tasks": {}}
        return json.loads(self.path.read_text())

    def _write(self, doc: dict[str, Any]) -> None:
        doc["updated_at"] = _now()
        fd, tmp = tempfile.mkstemp(dir=str(self.path.parent), prefix=".build_state.")
        try:
            with os.fdopen(fd, "w") as fh:
                json.dump(doc, fh, indent=2, sort_keys=True)
                fh.write("\n")
            os.replace(tmp, self.path)
        except BaseException:
            with contextlib.suppress(FileNotFoundError):
                os.unlink(tmp)
            raise

    def load(self) -> dict[str, Any]:
        with _locked():
            return self._read()

    # ── queries ──────────────────────────────────────────────────────────────

    def states(self) -> dict[str, str]:
        doc = self.load()
        return {tid: rec.get("state", "PENDING") for tid, rec in doc["tasks"].items()}

    def attempts(self) -> dict[str, int]:
        doc = self.load()
        return {tid: int(rec.get("attempts", 0)) for tid, rec in doc["tasks"].items()}

    def record(self, task_id: str) -> dict[str, Any]:
        return self.load()["tasks"].get(task_id, {"state": "PENDING", "attempts": 0})

    # ── transitions ──────────────────────────────────────────────────────────

    def set(self, task_id: str, state: str, **fields: Any) -> dict[str, Any]:
        if state not in VALID:
            raise ValueError(f"invalid state {state!r}; expected one of {sorted(VALID)}")
        with _locked():
            doc = self._read()
            rec = doc["tasks"].setdefault(task_id, {"state": "PENDING", "attempts": 0})
            previous = rec.get("state", "PENDING")

            if previous == "DONE" and state not in ("DONE", "SPLIT"):
                # Downstream tasks have already trusted this. Regressing it silently would
                # corrupt the build; make the operator do it deliberately.
                raise ValueError(
                    f"{task_id} is DONE; refusing to move it to {state}. "
                    "Edit BUILD_STATE.json by hand if this is genuinely intended."
                )

            if state == "IN_PROGRESS":
                rec["attempts"] = int(rec.get("attempts", 0)) + 1
                rec["last_started"] = _now()
                rec.setdefault("first_started", rec["last_started"])
            elif state in TERMINAL or state == "FAILED":
                rec["finished"] = _now()

            if "verify_output" in fields and fields["verify_output"]:
                out = str(fields["verify_output"])
                if len(out) > VERIFY_OUTPUT_LIMIT:
                    fields["verify_output"] = out[:VERIFY_OUTPUT_LIMIT] + "\n…[truncated]"

            rec["state"] = state
            rec["previous_state"] = previous
            rec.update({k: v for k, v in fields.items() if v is not None})
            self._write(doc)
            return rec

    def bump_wave(self) -> int:
        with _locked():
            doc = self._read()
            doc["wave"] = int(doc.get("wave", 0)) + 1
            self._write(doc)
            return int(doc["wave"])

    def release_stale(self, ids: list[str]) -> list[str]:
        """Move IN_PROGRESS tasks back to FAILED after a runner crash.

        An abandoned IN_PROGRESS row is invisible to `ready()` forever, which looks
        exactly like a deadlock, so the runner clears its own leftovers on startup.
        """
        released = []
        with _locked():
            doc = self._read()
            for tid in ids:
                rec = doc["tasks"].get(tid)
                if rec and rec.get("state") == "IN_PROGRESS":
                    rec["state"] = "FAILED"
                    rec["reason"] = "runner exited while task was in progress"
                    released.append(tid)
            if released:
                self._write(doc)
        return released
