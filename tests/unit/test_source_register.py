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
import re
from datetime import datetime
from pathlib import Path
from typing import Any

import pytest
import yaml
from pydantic import ValidationError

from dataplatform.clock import IST
from dataplatform.ingest.source_register import (
    PLAN_ROWS,
    REGISTER_PATH,
    REPROBE_STATUSES,
    Source,
    SourceRegister,
    Status,
    declined_sources,
    load,
    main,
    problems,
    reprobe_candidates,
)

#: The build horizon: the latest date any verification in the register may legitimately carry.
#: C.1 verified its rows in a single sweep (`register.sweep`), but a source added by a later task
#: is flipped to VERIFIED by that task's own real fetch (AGENTIC_CONTEXT §8), whose date
#: legitimately postdates the sweep. The honest anti-fabrication invariant is therefore not
#: "before the C.1 sweep" but "not after the build actually ran" — a fixed, checked-in ceiling so
#: the suite stays offline and deterministic (B10). Bump this when a task records a verification on
#: a newer date (M6.1 did, on 2026-09-02: curated_rss; M11.1 on 2026-09-04: the macro probe;
#: M3.9.b on 2026-09-08: nifty_tri_history, re-probed at D8's corrected path).
# 2026-10-05: nse_announcement_attachment, verified by the merger-terms fetch (11 documents);
# nifty_index_press_releases, verified by M10.2 the same day.
# 2026-10-06: the macro-probes rows (FBIL, RBI rates and archive, WPI, GST, India VIX spot).
LATEST_VERIFICATION: datetime = datetime(2026, 10, 6, 23, 59, 59, tzinfo=IST)


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
    # Not after the build horizon (offline, deterministic): evidence dated in the future is
    # fabricated. C.1's rows were gathered at/before its sweep; a source added by a later task
    # (AGENTIC_CONTEXT §8) carries that task's own verification date, which postdates the sweep but
    # not the build. Both are non-fabricated; only a future date is.
    assert register.sweep.swept_at <= LATEST_VERIFICATION, "bump LATEST_VERIFICATION for new sweeps"
    assert source.verified_at <= LATEST_VERIFICATION, source.id
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

    A **wave** id (`W1`, `W2`, …) also resolves. Waves are owner-directed units of work that
    postdate the graph — the git history carries `[W1]` and `[W2]` commits and CLAUDE.md's commit
    rule covers them — and TASK_GRAPH.yaml does not model them. Accepting the id keeps the
    no-dangling-promise property (a wave is a real, named, auditable unit) without misattributing
    a wave's parser to whichever graph task happens to sit nearest it.
    """
    with (repo_root / "TASK_GRAPH.yaml").open(encoding="utf-8") as fh:
        graph = yaml.safe_load(fh)
    known = {task["id"] for task in graph["tasks"]}
    wave = re.compile(r"^W\d+$")

    def resolves(task_id: str) -> bool:
        return task_id in known or bool(wave.match(task_id))

    for source in register.sources:
        if source.parser.archive_only:
            # No parser is named, so `owner_task` stands alone as the accountable task. It still
            # has to resolve: an archive-only entry nobody owns is a payload nobody will parse.
            assert source.owner_task is not None, f"{source.id}: archive-only with no owner_task"
            assert resolves(source.owner_task), f"{source.id}: owner_task {source.owner_task}"
            continue
        assert source.parser.task is not None  # not archive-only, so the validator required it
        assert resolves(source.parser.task), f"{source.id}: parser.task {source.parser.task}"
        assert source.owner_task == source.parser.task, (
            f"{source.id}: owner_task {source.owner_task} != parser.task {source.parser.task}"
        )
        if source.fixture.task is not None:
            assert resolves(source.fixture.task), f"{source.id}: fixture.task {source.fixture.task}"


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
    broken = _mutate(raw, "gdelt_doc_api", failure_note=None)
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
    """A 200 carrying HTML must never read as success — with or without a recorded parse.

    This was written against `nifty_tri_history`, which the 2026-08-08 sweep recorded FAILED at
    200 + 92 KB of markup. D8 established the markup came from a *stale URL path*, not a gate, and
    M3.9.b re-verified the row at the corrected path — so the specimen is now the general property
    instead of that one row: strip the `parse_check` off a VERIFIED entry and what is left is
    exactly the soft-404 signature (a 2xx, a body, and nothing that parsed), which must not read
    as a successful fetch. `content_type` is no help here and is deliberately left alone: this
    source answers `text/html` on success too (D9).
    """
    tri = next(s for s in load().sources if s.id == "nifty_tri_history")
    assert tri.last_http_status == 200
    assert tri.status is Status.VERIFIED
    assert tri.content_type is not None and tri.content_type.startswith("text/html")
    assert tri.fetch_succeeded is True

    soft_404 = _mutate(raw, "nifty_tri_history", parse_check=None)
    unparsed = next(s for s in soft_404.sources if s.id == "nifty_tri_history")
    assert unparsed.fetch_succeeded is False
    assert any(
        "marked VERIFIED without a recorded successful fetch" in p for p in problems(soft_404)
    )


def test_fetch_succeeded_requires_all_three_signals() -> None:
    """Status code alone is not evidence — bytes and a parse are part of the definition."""
    template = load().sources[0].model_dump()
    ok = Source.model_validate(template)
    assert ok.fetch_succeeded is True
    for change in ({"last_http_status": 500}, {"sample_bytes": 0}, {"parse_check": None}):
        assert Source.model_validate({**template, **change}).fetch_succeeded is False


# ── DECLINED: a source we choose not to take, on policy grounds (D12, taken by D19) ─────────────

SCREENER: str = "screener_company_fundamentals"


def test_screener_is_declined_with_its_robots_evidence(register: SourceRegister) -> None:
    """D12: the row is declined by robots policy, not blocked on a credential."""
    row = next(s for s in register.sources if s.id == SCREENER)
    assert row.status is Status.DECLINED
    assert row.is_declined
    assert row.declined is not None
    assert row.declined.reason.strip()
    assert "D12" in row.declined.decision and "D19" in row.declined.decision
    assert row.declined.robots_rule == "/user/*"
    host = register.host_policy(row.host)
    assert host is not None and row.declined.robots_rule in host.disallow
    assert row.declined.declined_url is not None and "/user/" in row.declined.declined_url
    assert declined_sources() == {SCREENER: row.declined}


def test_every_status_is_placed_on_one_side_of_the_reprobe_line() -> None:
    """Every switch on status handles every value: a new status must be classified deliberately.

    VERIFIED works, DECLINED is permanent, everything else is re-probed. A status in none of those
    (or in two) fails here rather than silently falling into whichever branch a reader defaulted to.
    """
    for status in Status:
        sides = [
            status is Status.VERIFIED,
            status is Status.DECLINED,
            status in REPROBE_STATUSES,
        ]
        assert sum(sides) == 1, status
    assert Status.DECLINED not in REPROBE_STATUSES


def test_a_reprobe_sweep_never_selects_a_declined_row(register: SourceRegister) -> None:
    selected = {s.id for s in reprobe_candidates(register)}
    assert SCREENER not in selected
    assert all(not s.is_declined for s in reprobe_candidates(register))
    # Every non-VERIFIED, non-DECLINED row *is* selected — the sweep skips only what it must.
    expected = {
        s.id for s in register.sources if s.status not in (Status.VERIFIED, Status.DECLINED)
    }
    assert selected == expected


def test_reprobe_selection_is_not_inverted(raw: dict[str, Any]) -> None:
    """Inverted check: the same row, as FAILED, is selected — the skip is the status, not the id."""
    failed = _mutate(raw, SCREENER, status="FAILED", declined=None)
    assert SCREENER in {s.id for s in reprobe_candidates(failed)}
    assert problems(failed) == []


def _changed(raw: dict[str, Any], source_id: str, **changes: Any) -> dict[str, Any]:
    """A deep copy of the raw register document with one entry altered — not yet validated."""
    doc = copy.deepcopy(raw)
    next(entry for entry in doc["sources"] if entry["id"] == source_id).update(changes)
    return doc


def _screener_record(raw: dict[str, Any]) -> dict[str, Any]:
    record: dict[str, Any] = next(s for s in raw["sources"] if s["id"] == SCREENER)["declined"]
    return record


def _bad_declines(raw: dict[str, Any]) -> dict[str, dict[str, Any]]:
    """Every DECLINED rule broken once, as a raw document the register must refuse to build."""
    record = _screener_record(raw)
    return {
        "declined-without-record": _changed(raw, SCREENER, declined=None),
        "record-on-a-live-row": _changed(raw, "nifty_tri_history", declined=record),
        "empty-reason": _changed(raw, SCREENER, declined={**record, "reason": ""}),
        "blank-reason": _changed(raw, SCREENER, declined={**record, "reason": "   "}),
        "empty-decision": _changed(raw, SCREENER, declined={**record, "decision": ""}),
        "blank-decision": _changed(raw, SCREENER, declined={**record, "decision": "  "}),
        "free-text-decision": _changed(
            raw, SCREENER, declined={**record, "decision": "polly said so"}
        ),
        "decision-without-number": _changed(
            raw, SCREENER, declined={**record, "decision": "HUMAN_DECISIONS D twelve"}
        ),
        "invented-robots-rule": _changed(
            raw, SCREENER, declined={**record, "robots_rule": "/not-a-rule/*"}
        ),
    }


BAD_DECLINE_CASES: list[str] = [
    "declined-without-record",
    "record-on-a-live-row",
    "empty-reason",
    "blank-reason",
    "empty-decision",
    "blank-decision",
    "free-text-decision",
    "decision-without-number",
    "invented-robots-rule",
]


def test_the_case_list_covers_every_bad_decline(raw: dict[str, Any]) -> None:
    assert sorted(BAD_DECLINE_CASES) == sorted(_bad_declines(raw))


@pytest.mark.parametrize("case", BAD_DECLINE_CASES)
def test_a_bad_decline_is_refused_by_model_validate(raw: dict[str, Any], case: str) -> None:
    """The DECLINED rules are schema, not an optional `problems()` pass: construction refuses."""
    with pytest.raises(ValidationError):
        SourceRegister.model_validate(_bad_declines(raw)[case])


@pytest.mark.parametrize("case", BAD_DECLINE_CASES)
def test_a_bad_decline_is_refused_by_load(raw: dict[str, Any], case: str, tmp_path: Path) -> None:
    """`load()` — what the fetcher, scheduler and status API all call — refuses it too."""
    path = tmp_path / "source_register.yaml"
    path.write_text(yaml.safe_dump(_bad_declines(raw)[case], allow_unicode=True), encoding="utf-8")
    with pytest.raises(ValidationError):
        load(path)


def test_the_unbroken_document_round_trips_through_load(
    raw: dict[str, Any], tmp_path: Path
) -> None:
    """Control for the two tests above: the refusal is the broken rule, not the round trip."""
    path = tmp_path / "source_register.yaml"
    path.write_text(yaml.safe_dump(raw, allow_unicode=True), encoding="utf-8")
    assert load(path).declined().keys() == {SCREENER}


#: HUMAN_DECISIONS.md, where every decline's cited decision must be readable by the owner.
HUMAN_DECISIONS: Path = REGISTER_PATH.parents[2] / "HUMAN_DECISIONS.md"


def _decision_section(number: int) -> str | None:
    """The text of `### D<number>` in HUMAN_DECISIONS.md, up to the next `### ` heading."""
    text = HUMAN_DECISIONS.read_text(encoding="utf-8")
    match = re.search(rf"^### D{number}\b.*?(?=^### |\Z)", text, flags=re.MULTILINE | re.DOTALL)
    return None if match is None else match.group(0)


@pytest.mark.parametrize("source_id", sorted(load().declined()))
def test_every_decline_cites_a_real_decision_that_names_the_source(
    register: SourceRegister, source_id: str
) -> None:
    """Mislabelling a failing source DECLINED would hide it from the red aggregation, so the
    decline must point at a HUMAN_DECISIONS entry that exists and is about this very source.

    Deliberately silent on the entry's answered/open status: that line belongs to the decisions
    file's owner (PR #64 answers D12), and this test holds the citation, not the bookkeeping.
    """
    record = register.declined()[source_id]
    section = _decision_section(record.decision_number)
    assert section is not None, f"{source_id} cites D{record.decision_number}, which is absent"
    assert source_id in section, f"D{record.decision_number} never mentions {source_id}"


def test_validate_lists_declined_apart_from_open_rows(capsys: pytest.CaptureFixture[str]) -> None:
    assert main(["validate"]) == 0
    out = capsys.readouterr().out
    assert f"declined: {SCREENER}" in out
    assert f"open: {SCREENER}" not in out
