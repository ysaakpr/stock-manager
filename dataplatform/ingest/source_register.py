"""D1: the machine-readable Source Register — typed loader and validator.

`source_register.yaml` mirrors EXECUTION_PLAN.md §4.1 and is the single place the crawl policy
engine (M1.2) and every parser task read their endpoint, headers and era ranges from. This
module gives that file a schema and, more importantly, enforces the three rules C.1 exists to
guarantee:

  1. every §4.1 row is covered by at least one entry, and every entry carries evidence;
  2. no entry claims VERIFIED without a real, successful, parsed fetch recorded against it;
  3. every host an entry talks to has a robots.txt record saying what it permits.

It never fetches anything. The live sweep that produced the evidence is a one-off recorded in
`ops/gates/source-verification.md`; re-verification belongs to M1.2's fetcher, which has the
rate limiting, backoff and 403 hard stop this module deliberately does not reimplement.
"""

from __future__ import annotations

import argparse
import sys
from collections.abc import Iterator, Sequence
from datetime import date, datetime
from enum import StrEnum
from pathlib import Path
from typing import Final

import yaml
from pydantic import BaseModel, ConfigDict, Field, ValidationError

REGISTER_PATH: Final[Path] = Path(__file__).with_name("source_register.yaml")

# The §4.1 table, verbatim, in table order. A row that gains no entry is a hole in the plan's
# coverage, so this list — not the YAML — is the authority for "did we cover everything".
PLAN_ROWS: Final[tuple[str, ...]] = (
    "NSE equity OHLCV (≤ ~08-Jul-2024)",
    "NSE equity OHLCV (UDiFF, ≥ Jul-2024)",
    "NSE delivery % (sec_bhavdata_full)",
    "BSE equity OHLCV",
    "Corporate actions",
    "Symbol / ISIN master",
    "Index constituents",
    "Benchmark TRI series",
    "FII/DII daily flows",
    "Bulk & block deals",
    "Shareholding pattern",
    "F&O EOD (OI, PCR, basis)",
    "Corporate announcements",
    "News / geopolitical",
    "Fundamentals (restated)",
    "Fundamentals (point-in-time)",
)


class Status(StrEnum):
    """What the sweep actually established about a source, never what we hope is true."""

    VERIFIED = "VERIFIED"
    """A real request returned a usable payload that parsed. Requires full evidence."""

    FAILED = "FAILED"
    """A real request was made and the pattern does not work. Requires a failure note."""

    BLOCKED_CREDENTIAL = "BLOCKED_CREDENTIAL"
    """Reachable only behind an account or key that does not exist (B4 / §3.7)."""


class Era(BaseModel):
    """Half-open date range a URL pattern is valid for; `None` means unbounded."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    start: date | None = None
    end: date | None = None


class Parser(BaseModel):
    """The module that will read this source, and the task that writes it."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    id: str
    task: str


class Fixture(BaseModel):
    """Frozen-sample tracking. AGENTIC_CONTEXT §8 wants a fixture per format era (B8); C.1
    verifies reachability, the parser task named here freezes the sample."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    frozen: bool
    task: str | None = None
    path: str | None = None


class HostPolicy(BaseModel):
    """What one host's robots.txt says, and how fast we are allowed to talk to it.

    `robots_served=False` covers both "404, no such file" and the nastier "HTTP 200 carrying a
    web page" — a soft 404 is not a policy document and must never be parsed as one.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    host: str
    robots_url: str
    checked_at: datetime
    robots_http_status: int
    robots_served: bool
    permits: str
    disallow: list[str] = Field(default_factory=list)
    min_spacing_seconds: float


class Source(BaseModel):
    """One fetchable surface of one §4.1 dataset, with the evidence for its status."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    id: str
    plan_row: str
    description: str
    host: str
    method: str
    url_template: str
    verified_url: str
    cadence: str
    era: Era
    fallback: list[str] = Field(default_factory=list)
    required_headers: dict[str, str] = Field(default_factory=dict)
    needs_session_cookie: bool
    parser: Parser
    robots_notes: str
    pit_notes: str

    status: Status
    verified_at: datetime
    last_http_status: int | None = None
    sample_bytes: int | None = None
    content_type: str | None = None
    sample_sha256: str | None = None
    parse_check: str | None = None
    fixture: Fixture
    failure_note: str | None = None
    candidate_alternatives: list[str] = Field(default_factory=list)
    owner_task: str | None = None

    @property
    def fetch_succeeded(self) -> bool:
        """True only for a 2xx that carried a body we could structurally parse."""
        return (
            self.last_http_status is not None
            and 200 <= self.last_http_status < 300
            and bool(self.sample_bytes)
            and bool(self.parse_check)
        )


class Sweep(BaseModel):
    """Provenance of the evidence in this file: when, how much, and under what rules."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    task: str
    swept_at: datetime
    host_timezone: str
    reference_trading_date: date
    total_requests: int
    requests_per_host: dict[str, int]
    bulk_threshold_note: str
    user_agent: str
    method_note: str
    evidence_log: str


class SourceRegister(BaseModel):
    """The whole file."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    version: int
    sweep: Sweep
    hosts: list[HostPolicy]
    sources: list[Source]

    def host_policy(self, host: str) -> HostPolicy | None:
        """The robots/rate policy for a host, or None if the host was never checked."""
        return next((h for h in self.hosts if h.host == host), None)

    def by_plan_row(self, plan_row: str) -> list[Source]:
        """Every entry covering one §4.1 row (a row can have several: NSE and BSE, or two eras)."""
        return [s for s in self.sources if s.plan_row == plan_row]


def load(path: Path = REGISTER_PATH) -> SourceRegister:
    """Parse and type-check the register.

    Assumes the file is checked in and trusted. Raises `ValidationError` on a schema break and
    `FileNotFoundError` if the register is missing — both loud, neither recoverable here.
    """
    with path.open(encoding="utf-8") as fh:
        raw = yaml.safe_load(fh)
    return SourceRegister.model_validate(raw)


def _acceptance_problems(reg: SourceRegister) -> Iterator[str]:
    """Yield one line per violated C.1 acceptance criterion. Empty means the register holds."""
    # Criterion 1 — every §4.1 row has an entry, and every entry carries evidence.
    for row in PLAN_ROWS:
        if not reg.by_plan_row(row):
            yield f"§4.1 row not covered by any entry: {row!r}"
    for source in reg.sources:
        if source.plan_row not in PLAN_ROWS:
            yield f"{source.id}: plan_row {source.plan_row!r} is not a §4.1 row"
        if source.last_http_status is None:
            yield f"{source.id}: no last_http_status — the pattern was never actually requested"
        if not source.fetch_succeeded and not source.failure_note:
            yield f"{source.id}: unsuccessful fetch with no explicit failure note"
        if source.status is not Status.VERIFIED and not source.candidate_alternatives:
            yield f"{source.id}: status {source.status} with no candidate alternative recorded"

    # Criterion 2 — VERIFIED is a claim about a real successful fetch, and nothing else.
    for source in reg.sources:
        if source.status is not Status.VERIFIED:
            continue
        if not source.fetch_succeeded:
            yield (
                f"{source.id}: marked VERIFIED without a recorded successful fetch "
                f"(http={source.last_http_status}, bytes={source.sample_bytes}, "
                f"parsed={bool(source.parse_check)})"
            )
        if not source.content_type:
            yield f"{source.id}: marked VERIFIED without a content_type"
        if not source.sample_sha256:
            yield f"{source.id}: marked VERIFIED without a payload checksum"
        if source.failure_note:
            yield f"{source.id}: marked VERIFIED but carries a failure_note"

    # Criterion 3 — robots was checked per host, and the register says what it permits.
    for host in {s.host for s in reg.sources}:
        policy = reg.host_policy(host)
        if policy is None:
            yield f"host {host} is used by a source but has no robots record"
        elif not policy.permits.strip():
            yield f"host {host}: robots record does not say what it permits"

    # Integrity of the file itself.
    seen: set[str] = set()
    for source in reg.sources:
        if source.id in seen:
            yield f"duplicate source id: {source.id}"
        seen.add(source.id)


def problems(reg: SourceRegister) -> list[str]:
    """All acceptance violations in the register. An empty list is the pass condition."""
    return list(_acceptance_problems(reg))


def _summarise(reg: SourceRegister) -> str:
    counts = {status: sum(s.status is status for s in reg.sources) for status in Status}
    parts = ", ".join(f"{n} {status.value}" for status, n in counts.items() if n)
    return (
        f"{len(reg.sources)} entries over {len(PLAN_ROWS)} §4.1 rows ({parts}); "
        f"{len(reg.hosts)} hosts with a robots record; "
        f"swept {reg.sweep.swept_at.date()} in {reg.sweep.total_requests} requests"
    )


def main(argv: Sequence[str] | None = None) -> int:
    """CLI entry point. `validate` is C.1's verification command."""
    parser = argparse.ArgumentParser(prog="source_register", description=__doc__)
    parser.add_argument("command", choices=["validate"])
    parser.add_argument("--path", type=Path, default=REGISTER_PATH)
    args = parser.parse_args(argv)

    try:
        register = load(args.path)
    except (OSError, ValidationError) as exc:
        print(f"source register does not load: {exc}", file=sys.stderr)
        return 2

    found = problems(register)
    if found:
        print(f"source register INVALID — {len(found)} problem(s):", file=sys.stderr)
        for problem in found:
            print(f"  - {problem}", file=sys.stderr)
        return 1

    print(f"source register OK — {_summarise(register)}")
    for source in register.sources:
        if source.status is not Status.VERIFIED:
            print(f"  open: {source.id} [{source.status.value}] -> {source.owner_task}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
