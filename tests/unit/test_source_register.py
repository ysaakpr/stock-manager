"""The Source Register's own acceptance criteria (C.1), asserted offline.

These tests never touch the network (B8). They assert the *shape and honesty* of
`dataplatform/ingest/source_register.yaml` — that every EXECUTION_PLAN.md §4.1 row is covered,
that no entry claims VERIFIED without recorded evidence of a real successful fetch, and that
every host carries a robots record. The mutation tests below exist so that deleting or
inverting one of those checks fails here instead of silently letting a downstream task trust an
unverified endpoint.
"""

from __future__ import annotations

import copy
from datetime import datetime
from pathlib import Path
from typing import Any

import pytest
import yaml

from dataplatform.ingest.source_register import (
    PLAN_ROWS,
    REGISTER_PATH,
    Source,
    SourceRegister,
    Status,
    load,
    problems,
)


@pytest.fixture(scope="module")
def register() -> SourceRegister:
    return load()


@pytest.fixture(scope="module")
def raw() -> dict[str, Any]:
    with REGISTER_PATH.open(encoding="utf-8") as fh:
        parsed: dict[str, Any] = yaml.safe_load(fh)
    return parsed


def _mutate(raw: dict[str, Any], source_id: str, **changes: Any) -> SourceRegister:
    """A copy of the register with one entry altered — for testing the validator itself."""
    doc = copy.deepcopy(raw)
    for entry in doc["sources"]:
        if entry["id"] == source_id:
            entry.update(changes)
            break
    else:  # pragma: no cover - a typo in a test id, not a product path
        raise AssertionError(f"no source {source_id!r}")
    return SourceRegister.model_validate(doc)


# ── the register as checked in ───────────────────────────────────────────────────────────────


def test_register_passes_its_own_acceptance_criteria(register: SourceRegister) -> None:
    assert problems(register) == []


def test_every_plan_row_is_covered(register: SourceRegister) -> None:
    """Acceptance 1: every §4.1 row has an entry."""
    uncovered = [row for row in PLAN_ROWS if not register.by_plan_row(row)]
    assert uncovered == []


@pytest.mark.parametrize("source_id", [s.id for s in load().sources])
def test_every_entry_carries_evidence_or_a_failure_note(
    register: SourceRegister, source_id: str
) -> None:
    """Acceptance 1: verified_at + last_http_status + sample_bytes, or an explicit failure."""
    source = next(s for s in register.sources if s.id == source_id)
    assert isinstance(source.verified_at, datetime)
    assert source.verified_at.tzinfo is not None, "timestamps must be tz-aware (Asia/Kolkata)"
    # Compared against the sweep's own end time, not the host clock: this suite is offline and
    # deterministic, and evidence dated after the sweep that produced it is fabricated.
    assert source.verified_at <= register.sweep.swept_at
    assert source.last_http_status is not None
    assert source.sample_bytes or source.failure_note


def test_no_row_is_verified_without_a_successful_fetch(register: SourceRegister) -> None:
    """Acceptance 2, stated directly rather than through the validator."""
    for source in register.sources:
        if source.status is Status.VERIFIED:
            assert source.last_http_status == 200, source.id
            assert source.sample_bytes and source.sample_bytes > 0, source.id
            assert source.content_type, source.id
            assert source.parse_check, source.id
            assert source.sample_sha256, source.id
            assert source.failure_note is None, source.id


def test_unverified_rows_name_a_failure_and_a_way_forward(register: SourceRegister) -> None:
    """A non-VERIFIED row is only useful if it says what broke and what to try instead."""
    for source in register.sources:
        if source.status is not Status.VERIFIED:
            assert source.failure_note, source.id
            assert source.candidate_alternatives, source.id
            assert source.owner_task, source.id


def test_every_host_used_has_a_robots_record(register: SourceRegister) -> None:
    """Acceptance 3: robots.txt was checked per host and what it permits is recorded."""
    for host in sorted({s.host for s in register.sources}):
        policy = register.host_policy(host)
        assert policy is not None, host
        assert policy.permits.strip(), host
        assert policy.min_spacing_seconds >= 2.5, f"{host} spacing violates the §4.1 crawl policy"


def test_screener_limits_match_the_robots_file(register: SourceRegister) -> None:
    """AGENTIC_CONTEXT §8 names Screener's limits explicitly; the register must carry them."""
    policy = register.host_policy("www.screener.in")
    assert policy is not None
    for path in ("/user/*", "/*?q=", "/*?page="):
        assert path in policy.disallow


def test_nse_sources_send_the_referer_the_archives_require(register: SourceRegister) -> None:
    """The one header that makes the NSE hosts answer at all (AGENTIC_CONTEXT §8)."""
    for source in register.sources:
        if source.host.endswith("nseindia.com"):
            assert source.required_headers.get("Referer") == "https://www.nseindia.com/", source.id
            assert source.required_headers.get("User-Agent") == "browser", source.id


def test_ids_are_unique(register: SourceRegister) -> None:
    ids = [s.id for s in register.sources]
    assert len(ids) == len(set(ids))


def test_every_referenced_task_exists_in_the_graph(
    register: SourceRegister, repo_root: Path
) -> None:
    """A register pointing at a task id that does not exist is a dangling promise.

    Every entry names the task that will build its parser, freeze its fixture and — for the
    rows this sweep could not verify — resolve the failure. Those ids must resolve in
    TASK_GRAPH.yaml, or the handoff goes nowhere.
    """
    with (repo_root / "TASK_GRAPH.yaml").open(encoding="utf-8") as fh:
        graph = yaml.safe_load(fh)
    known = {task["id"] for task in graph["tasks"]}

    for source in register.sources:
        assert source.parser.task in known, f"{source.id}: parser.task {source.parser.task}"
        assert source.owner_task == source.parser.task, (
            f"{source.id}: owner_task {source.owner_task} != parser.task {source.parser.task}"
        )
        if source.fixture.task is not None:
            assert source.fixture.task in known, f"{source.id}: fixture.task {source.fixture.task}"


def test_era_ranges_are_ordered(register: SourceRegister) -> None:
    for source in register.sources:
        if source.era.start and source.era.end:
            assert source.era.start <= source.era.end, source.id


def test_dual_era_price_sources_do_not_leave_a_gap(register: SourceRegister) -> None:
    """The UDiFF cutover is the seam a backfill falls through; assert the eras meet."""
    legacy = next(s for s in register.sources if s.id == "nse_bhavcopy_legacy")
    udiff = next(s for s in register.sources if s.id == "nse_bhavcopy_udiff")
    assert legacy.era.end is not None
    assert udiff.era.start is not None
    assert udiff.era.start <= legacy.era.end, "a date between the two eras has no parser"


# ── the validator itself: each check must fail when its condition is violated ────────────────


def test_validator_rejects_verified_without_a_successful_fetch(raw: dict[str, Any]) -> None:
    broken = _mutate(raw, "nse_bhavcopy_udiff", last_http_status=403, sample_bytes=0)
    assert any("VERIFIED without a recorded successful fetch" in p for p in problems(broken))


def test_validator_rejects_verified_without_a_parse(raw: dict[str, Any]) -> None:
    """A 200 that was never parsed is an HTML error page as often as it is data."""
    broken = _mutate(raw, "nse_sec_bhavdata_full", parse_check=None)
    assert any("VERIFIED without a recorded successful fetch" in p for p in problems(broken))


def test_validator_rejects_a_failure_with_no_note(raw: dict[str, Any]) -> None:
    broken = _mutate(raw, "nifty_tri_history", failure_note=None)
    assert any("no explicit failure note" in p for p in problems(broken))


def test_validator_rejects_an_uncovered_plan_row(raw: dict[str, Any]) -> None:
    doc = copy.deepcopy(raw)
    doc["sources"] = [s for s in doc["sources"] if s["plan_row"] != "F&O EOD (OI, PCR, basis)"]
    broken = SourceRegister.model_validate(doc)
    assert any("F&O EOD" in p for p in problems(broken))


def test_validator_rejects_a_host_without_a_robots_record(raw: dict[str, Any]) -> None:
    doc = copy.deepcopy(raw)
    doc["hosts"] = [h for h in doc["hosts"] if h["host"] != "nsearchives.nseindia.com"]
    broken = SourceRegister.model_validate(doc)
    assert any("no robots record" in p for p in problems(broken))


def test_validator_rejects_a_duplicate_id(raw: dict[str, Any]) -> None:
    doc = copy.deepcopy(raw)
    doc["sources"].append(copy.deepcopy(doc["sources"][0]))
    broken = SourceRegister.model_validate(doc)
    assert any("duplicate source id" in p for p in problems(broken))


def test_schema_rejects_an_unknown_field(raw: dict[str, Any]) -> None:
    """extra='forbid' — a typo'd key must not be silently ignored by every consumer."""
    doc = copy.deepcopy(raw)
    doc["sources"][0]["verifed_at"] = "2026-08-08T00:00:00+05:30"
    with pytest.raises(Exception, match="verifed_at"):
        SourceRegister.model_validate(doc)


def test_fetch_succeeded_is_false_for_a_soft_404(raw: dict[str, Any]) -> None:
    """niftyindices answered the TRI POST with 200 + HTML; that must never read as success."""
    tri = next(s for s in load().sources if s.id == "nifty_tri_history")
    assert tri.last_http_status == 200
    assert tri.status is Status.FAILED
    assert tri.fetch_succeeded is False


def test_fetch_succeeded_requires_all_three_signals() -> None:
    """Status code alone is not evidence — bytes and a parse are part of the definition."""
    template = load().sources[0].model_dump()
    ok = Source.model_validate(template)
    assert ok.fetch_succeeded is True
    for change in ({"last_http_status": 500}, {"sample_bytes": 0}, {"parse_check": None}):
        assert Source.model_validate({**template, **change}).fetch_succeeded is False
