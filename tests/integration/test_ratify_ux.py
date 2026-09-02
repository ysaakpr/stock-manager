"""M5.8 acceptance: the ratification UX (FastAPI + HTMX) and its headless CLI twin (§8.1, §5.1).

Three things are proved here, one per acceptance criterion:

1. **A proposal renders, an edit round-trips, approval pins a content hash.** The review page shows
   the universe with purity, a thesis per holding with its break conditions, and the seven §5.2
   policies; an HTMX edit rebuilds the proposal and returns a *different* content hash; approval
   writes a `RatificationRecord` pinning that hash.
2. **The CLI ratifies headlessly and produces an identical record shape.** The same draft driven
   through the browser approve endpoint and through `analyst.cases.cli` — same approver, same
   injected instant — yields byte-identical records.
3. **A ratification cannot be created for a proposal that changed since it was displayed.** Approval
   against a stale hash answers 409 on the web and exits non-zero on the CLI, and the model-level
   guard refuses a ratification minted for one proposal against an edited one.

No database and no network: the proposal lives in an in-memory `ProposalStore`, time is a
`FrozenClock` (B10), and the theme map is hand-built rather than fetched. The test therefore runs
in the same offline, deterministic conditions as the unit suite (B8) — it lives under
`tests/integration/` because it exercises the HTTP stack end to end, not because it needs Postgres,
so it is intentionally *not* marked `integration` and always runs.
"""

from __future__ import annotations

import json
from datetime import datetime
from decimal import Decimal

from fastapi.testclient import TestClient
from typer.testing import CliRunner

from analyst.cases.cli import app as cli_app
from analyst.cases.policies import RatificationKind
from analyst.cases.web import (
    InMemoryProposalStore,
    ProposalDraft,
    RatificationRecord,
    StaleProposalError,
    apply_answer_edits,
    create_app,
    ratify_proposal,
)
from analyst.interview import (
    InterviewField,
    ProposalRatificationMismatchError,
    conduct_interview,
)
from analyst.journal.models import TokenSpend
from analyst.mapper import (
    ProxyCandidate,
    PurityEvidence,
    PurityEvidenceKind,
    ThemeMap,
    ValueChainStage,
    score_purity,
)
from analyst.thesis import (
    BreakCondition,
    BreakConditionType,
    EvaluationTier,
    Thesis,
    ThesisStatus,
)
from dataplatform.clock import IST, FrozenClock

CASE_ID = "case-ai-robotics"
THEME = "AI/Robotics"
AS_OF = datetime(2026, 8, 3, tzinfo=IST).date()
COMPUTE = "INE100A01010"
SENSORS = "INE200B01020"
INTEG = "INE300C01030"
#: The frozen instant every approval in this module is stamped at (B10).
RATIFIED_AT = datetime(2026, 9, 2, 10, 30, tzinfo=IST)


def _candidate(
    isin: str, name: str, stage: str, *, theme_share: str, other_share: str
) -> ProxyCandidate:
    """A proxy with a disclosed purity computed from one theme segment and one that is not."""
    evidence = (
        PurityEvidence(
            kind=PurityEvidenceKind.SEGMENT_DISCLOSURE,
            source="FY26 annual report segment note",
            label="theme segment",
            revenue_fraction=Decimal(theme_share),
            expresses_theme=True,
        ),
        PurityEvidence(
            kind=PurityEvidenceKind.SEGMENT_DISCLOSURE,
            source="FY26 annual report segment note",
            label="other segment",
            revenue_fraction=Decimal(other_share),
            expresses_theme=False,
        ),
    )
    return ProxyCandidate(
        isin=isin,
        name=name,
        value_chain_stage=stage,
        purity=score_purity(isin, evidence),
    )


def _theme_map() -> ThemeMap:
    """A hand-built A3 theme map — three purity-scored proxies across three value-chain stages."""
    value_chain = (
        ValueChainStage(name="compute & silicon", description="AI accelerators and silicon."),
        ValueChainStage(name="sensors & actuators", description="Robotics sensing and motion."),
        ValueChainStage(name="systems integration", description="Autonomous systems integration."),
    )
    candidates = (
        _candidate(
            COMPUTE,
            "Compute Silicon Ltd",
            "compute & silicon",
            theme_share="0.6",
            other_share="0.4",
        ),
        _candidate(
            SENSORS,
            "Sensor Actuator Corp",
            "sensors & actuators",
            theme_share="0.25",
            other_share="0.75",
        ),
        _candidate(
            INTEG,
            "Integration Systems Ltd",
            "systems integration",
            theme_share="0.1",
            other_share="0.9",
        ),
    )
    return ThemeMap(
        theme=THEME,
        as_of=AS_OF,
        value_chain=value_chain,
        candidates=tuple(sorted(candidates, key=lambda c: c.isin)),
        universe_size=42,
        provider="stub",
        model="stub-model",
        tokens=TokenSpend(tokens_in=100, tokens_out=50, cost_inr=Decimal("0.10")),
        rendered_prompt="map the AI/Robotics value chain to listed proxies",
    )


def _thesis(isin: str) -> Thesis:
    """A minimal §5.3 PROPOSAL/CORE thesis with one falsifiable break condition."""
    return Thesis(
        case_id=CASE_ID,
        isin=isin,
        version=1,
        status=ThesisStatus.PROPOSAL,
        driver="Indian EMS capex cycle x robotics component localization",
        theme_purity=Decimal("0.6"),
        expected_evidence=("order-book growth >20% YoY",),
        break_conditions=(
            BreakCondition(
                id="BC1",
                type=BreakConditionType.FUNDAMENTAL,
                condition="segment revenue falls two consecutive quarters",
                evaluation_tier=EvaluationTier.T1,
                evaluation="T1 on results filing",
            ),
        ),
    )


def _transcript(**overrides: str) -> dict[InterviewField, str]:
    base: dict[InterviewField, str] = {
        InterviewField.SIP_AMOUNT: "10000",
        InterviewField.SIP_DAY: "1",
        InterviewField.HORIZON: "5",
        InterviewField.THEME: THEME,
        InterviewField.RISK_APPETITE: "aggressive",
        InterviewField.CONCENTRATION: "medium",
        InterviewField.BENCHMARK_SECONDARY: "NIFTY IT/CPSE blend",
    }
    base.update(overrides)  # type: ignore[arg-type]
    return base


def _draft(**overrides: str) -> ProposalDraft:
    """A ready-to-review `ProposalDraft` for the reference case."""
    answers = conduct_interview(_transcript(**overrides))
    return ProposalDraft(
        case_id=CASE_ID,
        answers=answers,
        theme_map=_theme_map(),
        theses=(_thesis(COMPUTE), _thesis(SENSORS), _thesis(INTEG)),
    )


def _client(store: InMemoryProposalStore) -> TestClient:
    return TestClient(create_app(store, clock=FrozenClock(RATIFIED_AT)))


# ── acceptance 1 ─────────────────────────────────────────────────────────────────────────────────


def test_proposal_renders_edit_round_trips_and_approval_pins_a_hash() -> None:
    store = InMemoryProposalStore()
    draft = _draft()
    store.put(draft)
    original_hash = draft.build().content_hash
    client = _client(store)

    review = client.get(f"/cases/{CASE_ID}")
    assert review.status_code == 200
    page = review.text
    # the universe with purity, a thesis with its break condition, the policies, and the hash
    assert THEME in page
    assert COMPUTE in page
    assert "0.6" in page  # a disclosed purity score
    assert "BC1" in page  # a falsifiable break condition is shown
    assert "rotation_dial" in page  # a §5.2 policy is shown
    assert original_hash in page  # the displayed hash the approval must pin

    # an edit round-trips: change the risk appetite, get the proposal back with a *different* hash
    edited = client.post(f"/cases/{CASE_ID}/edit", data={"risk_appetite": "conservative"})
    assert edited.status_code == 200
    new_hash = draft.build().content_hash  # unchanged draft still hashes to the original...
    assert new_hash == original_hash
    stored_after_edit = store.get(CASE_ID)
    edited_hash = stored_after_edit.build().content_hash
    assert edited_hash != original_hash  # ...but the stored, edited draft does not
    assert edited_hash in edited.text
    assert stored_after_edit.answers.risk_appetite.value == "CONSERVATIVE"

    # approval pins the (current) content hash and writes a governance record
    approve = client.post(
        f"/cases/{CASE_ID}/approve",
        data={"by": "Asha", "content_hash": edited_hash, "kind": "HUMAN"},
    )
    assert approve.status_code == 200
    assert "Ratified" in approve.text
    assert edited_hash in approve.text

    record = store.ratification(CASE_ID)
    assert record is not None
    assert record.content_hash == edited_hash
    assert record.ratification.content_hash == edited_hash
    assert record.ratification.by == "Asha"
    assert record.ratification.kind is RatificationKind.HUMAN
    assert record.ratification.at == RATIFIED_AT


# ── acceptance 2 ─────────────────────────────────────────────────────────────────────────────────


def test_cli_ratifies_headlessly_with_an_identical_record_shape(tmp_path) -> None:  # type: ignore[no-untyped-def]
    draft = _draft()
    content_hash = draft.build().content_hash

    # the web path: approve through the HTTP endpoint, read back the stored record
    store = InMemoryProposalStore()
    store.put(draft)
    web = _client(store).post(
        f"/cases/{CASE_ID}/approve",
        data={"by": "Asha", "content_hash": content_hash, "kind": "HUMAN"},
    )
    assert web.status_code == 200
    web_record = store.ratification(CASE_ID)
    assert web_record is not None

    # the headless path: the same draft, approver and instant, through the CLI
    draft_path = tmp_path / "draft.json"
    draft_path.write_text(draft.model_dump_json(), encoding="utf-8")
    result = CliRunner().invoke(
        cli_app,
        [
            "ratify",
            "--draft",
            str(draft_path),
            "--by",
            "Asha",
            "--kind",
            "HUMAN",
            "--at",
            RATIFIED_AT.isoformat(),
        ],
    )
    assert result.exit_code == 0, result.output
    cli_record = RatificationRecord.model_validate(json.loads(result.stdout))

    # identical record shape — same fields, and here the same values too (identical inputs)
    assert set(cli_record.model_dump().keys()) == set(web_record.model_dump().keys())
    assert cli_record.model_dump() == web_record.model_dump()
    assert cli_record.content_hash == content_hash


def test_cli_hash_matches_the_built_proposal(tmp_path) -> None:  # type: ignore[no-untyped-def]
    draft = _draft()
    draft_path = tmp_path / "draft.json"
    draft_path.write_text(draft.model_dump_json(), encoding="utf-8")
    result = CliRunner().invoke(cli_app, ["hash", "--draft", str(draft_path)])
    assert result.exit_code == 0, result.output
    assert result.stdout.strip() == draft.build().content_hash


# ── acceptance 3 ─────────────────────────────────────────────────────────────────────────────────


def test_web_refuses_to_ratify_a_proposal_that_changed_since_it_was_displayed() -> None:
    store = InMemoryProposalStore()
    draft = _draft()
    store.put(draft)
    stale_hash = draft.build().content_hash
    client = _client(store)

    # the reviewer read `stale_hash`, then an edit lands (dial changes → hash changes)
    client.post(f"/cases/{CASE_ID}/edit", data={"risk_appetite": "conservative"})
    current_hash = store.get(CASE_ID).build().content_hash
    assert current_hash != stale_hash

    # approving against the hash they saw is refused, and nothing is recorded
    refused = client.post(
        f"/cases/{CASE_ID}/approve",
        data={"by": "Asha", "content_hash": stale_hash, "kind": "HUMAN"},
    )
    assert refused.status_code == 409
    assert store.ratification(CASE_ID) is None

    # approving against the current hash succeeds
    ok = client.post(
        f"/cases/{CASE_ID}/approve",
        data={"by": "Asha", "content_hash": current_hash, "kind": "HUMAN"},
    )
    assert ok.status_code == 200
    assert store.ratification(CASE_ID) is not None


def test_cli_expect_hash_refuses_a_changed_draft(tmp_path) -> None:  # type: ignore[no-untyped-def]
    draft = _draft()
    stale_hash = draft.build().content_hash
    draft_path = tmp_path / "draft.json"

    # the draft the caller last saw is replaced by an edited one before they ratify
    edited = draft.with_answers(
        apply_answer_edits(draft.answers, {"risk_appetite": "conservative"})
    )
    assert edited.build().content_hash != stale_hash
    draft_path.write_text(edited.model_dump_json(), encoding="utf-8")

    runner = CliRunner()
    refused = runner.invoke(
        cli_app,
        ["ratify", "--draft", str(draft_path), "--by", "Asha", "--expect-hash", stale_hash],
    )
    assert refused.exit_code == 1
    assert "changed after it was displayed" in refused.output

    # without the stale guard, ratifying the current draft succeeds
    ok = runner.invoke(
        cli_app,
        [
            "ratify",
            "--draft",
            str(draft_path),
            "--by",
            "Asha",
            "--expect-hash",
            edited.build().content_hash,
            "--at",
            RATIFIED_AT.isoformat(),
        ],
    )
    assert ok.exit_code == 0, ok.output


def test_the_model_guard_refuses_a_ratification_minted_for_another_version() -> None:
    original = _draft()
    changed = original.with_answers(
        apply_answer_edits(original.answers, {"risk_appetite": "conservative"})
    )
    # a ratification minted for the changed proposal cannot be moved onto the original
    _, record = ratify_proposal(changed, by="Asha", clock=FrozenClock(RATIFIED_AT))
    try:
        original.build().ratified_with(record.ratification)
    except ProposalRatificationMismatchError:
        pass
    else:  # pragma: no cover - the guard must fire
        raise AssertionError("a ratification for the edited proposal must not ratify the original")


def test_ratify_proposal_rejects_a_stale_displayed_hash_directly() -> None:
    draft = _draft()
    try:
        ratify_proposal(
            draft,
            by="Asha",
            clock=FrozenClock(RATIFIED_AT),
            displayed_hash="sha256:" + "0" * 64,
        )
    except StaleProposalError:
        pass
    else:  # pragma: no cover - the guard must fire
        raise AssertionError("a stale displayed hash must be refused")
