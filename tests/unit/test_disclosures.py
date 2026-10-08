"""M15.2 — the T1 evidence bundle carries a disclosure's text, not only its headline.

The M6.8 fire drill built its T1 bundle from the T0 flag alone, so the reviewer saw "Resignation of
Auditor" and nothing else (BACKLOG M6.8 F1). These tests pin the production path that replaces it
(`request_for_escalation` → `select_disclosures` → `BundleBuilder`):

* the drill's case shape — T0 raises a keyword escalation on a held name, the bundle is built from
  that escalation and the day's announcement index — now puts the disclosure's text in the prompt;
* only disclosures disseminated at or before the decision's as-of instant are selected, and the
  boundary tests fail if the comparison is inverted or removed;
* a disclosure without usable text is marked `text_unavailable` with a reason, never shown as a
  bare headline;
* the text is bounded per item and per bundle, and the same inputs build a byte-identical bundle.

Offline: the journal runs over `test_t0`'s recording connection and no model is called.
"""

from __future__ import annotations

import ast
import inspect
from datetime import date, datetime, timedelta
from pathlib import Path

import pytest

from analyst.journal import Actor, Journal
from analyst.monitor import (
    DEFAULT_DISCLOSURE_POLICY,
    BundleBuilder,
    BundlePitError,
    BundleRequest,
    DisclosurePolicy,
    DocumentTextStatus,
    InMemoryEscalationQueue,
    KeywordWatch,
    T0Escalation,
    TextUnavailableReason,
    disclosure_text,
    request_for_escalation,
    select_disclosures,
)
from analyst.monitor import bundle as bundle_module
from analyst.monitor import disclosures as disclosures_module
from dataplatform.clock import IST
from dataplatform.ingest.announcements import AnnouncementRow
from dataplatform.query import AnnouncementIndex, KeywordQuery
from tests.unit.test_bundle import make_flag, make_thesis
from tests.unit.test_t0 import (
    _RecordingConnection,
    clean_inputs,
    green_gate,
    green_status,
    holding,
    make_journal,
    make_monitor,
)

ISIN = "INE001A01001"
TRADING_DATE = date(2026, 8, 7)
#: The decision's as-of instant: the EOD review after the close on the trading date.
AS_OF = datetime(2026, 8, 7, 19, 30, tzinfo=IST)

RESIGNATION_BODY = (
    "The statutory auditor has resigned with immediate effect, mid-term, citing inability to "
    "obtain sufficient appropriate audit evidence on related-party transactions with a "
    "promoter-group entity for the quarter ended 30 June 2026."
)


def row(
    *,
    ts: datetime = datetime(2026, 8, 7, 10, 0, tzinfo=IST),
    subject: str = "Resignation of Auditor",
    body: str | None = RESIGNATION_BODY,
    attachment_ref: str | None = None,
    ref: str = "1",
    isin: str = ISIN,
) -> AnnouncementRow:
    return AnnouncementRow(
        ts=ts,
        source="nse_announcements",
        isin=isin,
        subject=subject,
        body=body,
        attachment_ref=attachment_ref,
        source_ref=ref,
    )


def escalation(trading_date: date = TRADING_DATE) -> T0Escalation:
    return T0Escalation(trading_date=trading_date, flag=make_flag(), journal_entry_id=1)


# ── the drill's case shape: a routine check that saw only a headline now sees the text ──────────


def _t0_escalation(journal: Journal, index: AnnouncementIndex) -> T0Escalation:
    """Run the real T0 sweep over `index` with an auditor watch on the held name."""
    watch = KeywordWatch(
        break_condition_id="BC3",
        query=KeywordQuery(all_of=("auditor",), any_of=("resign", "resignation")),
    )
    queue = InMemoryEscalationQueue()
    monitor = make_monitor(green_gate(green_status(green=True, reason="ok")), journal, queue)
    inputs = clean_inputs(holdings=(holding(ISIN, watches=(watch,)),), announcements=index)
    monitor.run(TRADING_DATE, lambda: inputs)
    (queued,) = queue.pending
    return queued


def test_drill_shape_bundle_now_carries_the_disclosure_text(tmp_path: Path) -> None:
    index = AnnouncementIndex((row(),))
    queued = _t0_escalation(make_journal(_RecordingConnection(), tmp_path), index)

    # Before: the drill handed T1 the flag only, so the prompt held the headline and no text.
    headline_only = BundleBuilder().build(
        BundleRequest(
            case_id=queued.flag.case_id,
            isin=ISIN,
            trading_date=TRADING_DATE,
            flag=queued.flag,
            thesis=make_thesis(),
        )
    )
    assert "Resignation of Auditor" in headline_only.rendered_prompt
    assert RESIGNATION_BODY not in headline_only.rendered_prompt

    # After: the production path builds the request from the escalation and the index.
    built = BundleBuilder().build(
        request_for_escalation(queued, thesis=make_thesis(), announcements=index, as_of=AS_OF)
    )
    assert f"  text: {RESIGNATION_BODY}" in built.rendered_prompt
    assert "text_unavailable" not in built.rendered_prompt
    (filing,) = [item for item in built.bundle.items if item.source == "nse_announcements"]
    assert filing.detail["text"] == RESIGNATION_BODY
    assert filing.detail["text_status"] == "available"


def test_drill_shape_without_a_body_says_text_unavailable(tmp_path: Path) -> None:
    index = AnnouncementIndex((row(body=None),))
    queued = _t0_escalation(make_journal(_RecordingConnection(), tmp_path), index)
    built = BundleBuilder().build(
        request_for_escalation(queued, thesis=make_thesis(), announcements=index, as_of=AS_OF)
    )
    assert "Resignation of Auditor\n  text_unavailable: no_body" in built.rendered_prompt
    (filing,) = [item for item in built.bundle.items if item.source == "nse_announcements"]
    assert filing.detail["text_unavailable"] == "no_body"
    assert "text" not in filing.detail


# ── point in time: at or before the as-of instant, never after ─────────────────────────────────


def test_pit_includes_a_disclosure_at_the_as_of_instant_and_excludes_one_after() -> None:
    at = row(ts=AS_OF, ref="at")
    before = row(ts=AS_OF - timedelta(hours=9), ref="before")
    after = row(ts=AS_OF + timedelta(seconds=1), ref="after")  # same IST day, after the decision
    index = AnnouncementIndex((at, before, after))

    selected = select_disclosures(index, isin=ISIN, as_of=AS_OF)

    assert [d.announcement.source_ref for d in selected] == ["at", "before"]


def test_pit_boundary_holds_through_the_production_request() -> None:
    later_today = row(ts=AS_OF + timedelta(minutes=5), body="Later the same evening.", ref="x")
    index = AnnouncementIndex((row(ref="kept"), later_today))
    request = request_for_escalation(
        escalation(), thesis=make_thesis(), announcements=index, as_of=AS_OF
    )
    assert [d.announcement.source_ref for d in request.disclosures] == ["kept"]
    assert "Later the same evening." not in BundleBuilder().build(request).rendered_prompt


def test_pit_builder_refuses_a_disclosure_from_after_the_trading_date() -> None:
    (future,) = select_disclosures(
        AnnouncementIndex((row(ts=datetime(2026, 8, 8, 9, 0, tzinfo=IST)),)),
        isin=ISIN,
        as_of=datetime(2026, 8, 8, 19, 30, tzinfo=IST),
    )
    request = BundleRequest(
        case_id="AI_ROBOTICS",
        isin=ISIN,
        trading_date=TRADING_DATE,
        flag=make_flag(),
        thesis=make_thesis(),
        disclosures=(future,),
    )
    with pytest.raises(BundlePitError):
        BundleBuilder().build(request)


def test_as_of_must_be_aware_and_on_the_escalation_date() -> None:
    index = AnnouncementIndex(())
    with pytest.raises(ValueError, match="tz-aware"):
        select_disclosures(index, isin=ISIN, as_of=datetime(2026, 8, 7, 19, 30))
    with pytest.raises(ValueError, match="not on the escalation's trading date"):
        request_for_escalation(
            escalation(),
            thesis=make_thesis(),
            announcements=index,
            as_of=AS_OF + timedelta(days=1),
        )


def test_lookback_window_and_other_isins_are_excluded() -> None:
    old = row(ts=AS_OF - timedelta(days=31), ref="old")
    other = row(isin="INE002A01009", ref="other")
    recent = row(ts=AS_OF - timedelta(days=29), ref="recent")
    selected = select_disclosures(AnnouncementIndex((old, other, recent)), isin=ISIN, as_of=AS_OF)
    assert [d.announcement.source_ref for d in selected] == ["recent"]


# ── text unavailable is said, with a reason ────────────────────────────────────────────────────


def test_headline_echo_is_text_unavailable() -> None:
    echo = row(
        subject="General Updates",
        body="Kalyan Jewellers India Limited has informed the Exchange about General Updates",
    )
    item = disclosure_text(echo)
    assert item.text is None
    assert item.unavailable is TextUnavailableReason.HEADLINE_ECHO


def test_headline_wrapped_in_filler_is_still_an_echo() -> None:
    """The real feed's shape for an auditor change: it cannot tell a rotation from a resignation."""
    wrapped = row(
        subject="Change in Auditors",
        body="Advit Jewels Limited has informed the Exchange regarding Change in Auditors of the "
        "company.",
    )
    assert disclosure_text(wrapped).unavailable is TextUnavailableReason.HEADLINE_ECHO


def test_exchange_text_that_adds_to_the_headline_is_available() -> None:
    body = (
        "Inox Green Energy Services Limited has informed the Exchange about General Updates on "
        "the Resolution Plan for Wind World (India) Limited"
    )
    item = disclosure_text(row(subject="General Updates", body=body))
    assert item.text == body
    assert item.unavailable is None


def test_blank_body_is_no_body() -> None:
    assert disclosure_text(row(body="   ")).unavailable is TextUnavailableReason.NO_BODY


def test_attached_document_is_reported_not_captured_and_never_fetched() -> None:
    ref = "https://nsearchives.nseindia.com/corporate/X_07082026_AUDITOR.pdf"
    item = disclosure_text(row(attachment_ref=ref))
    assert item.document is DocumentTextStatus.NOT_CAPTURED
    request = BundleRequest(
        case_id="AI_ROBOTICS",
        isin=ISIN,
        trading_date=TRADING_DATE,
        flag=make_flag(),
        thesis=make_thesis(),
        disclosures=(item,),
    )
    built = BundleBuilder().build(request)
    assert f"attached document: text not captured ({ref})" in built.rendered_prompt
    assert disclosure_text(row()).document is DocumentTextStatus.NONE


# ── bounded: per item, per bundle, and in count ────────────────────────────────────────────────


def test_text_is_truncated_per_item_and_marked() -> None:
    long_body = "word " * 1_000
    item = disclosure_text(row(body=long_body), max_chars=200)
    assert item.text is not None
    assert len(item.text) <= 200
    assert item.text.endswith("[…truncated]")
    assert item.truncated
    assert item.original_chars == len(long_body.strip())


@pytest.mark.parametrize("max_chars", [1, 10, 13])
def test_a_cap_no_longer_than_the_truncation_mark_is_refused(max_chars: int) -> None:
    """Below the mark's length the slice index went negative and returned nearly the whole text."""
    with pytest.raises(ValueError, match="truncation mark"):
        disclosure_text(row(body="x" * 49), max_chars=max_chars)


def test_smallest_allowed_cap_still_bounds_the_text() -> None:
    item = disclosure_text(row(body="word " * 20), max_chars=14)
    assert item.text is not None
    assert len(item.text) <= 14


def test_bundle_text_cap_marks_the_overflow_unavailable() -> None:
    policy = DisclosurePolicy(max_chars_per_item=100, max_chars_per_bundle=250, max_items=5)
    rows = tuple(
        row(ts=AS_OF - timedelta(hours=h), body=f"Distinct text {h} " + "x" * 200, ref=str(h))
        for h in range(1, 5)
    )
    selected = select_disclosures(AnnouncementIndex(rows), isin=ISIN, as_of=AS_OF, policy=policy)
    texts = [d.text for d in selected if d.text is not None]
    assert sum(len(t) for t in texts) <= policy.max_chars_per_bundle
    assert len(texts) == 2  # newest first; the rest are named, not dropped
    assert [d.unavailable for d in selected[2:]] == [TextUnavailableReason.BUNDLE_TEXT_CAP] * 2


def test_max_items_keeps_the_newest() -> None:
    policy = DisclosurePolicy(max_items=2)
    rows = tuple(row(ts=AS_OF - timedelta(hours=h), ref=str(h)) for h in range(1, 6))
    selected = select_disclosures(AnnouncementIndex(rows), isin=ISIN, as_of=AS_OF, policy=policy)
    assert [d.announcement.source_ref for d in selected] == ["1", "2"]


def test_default_policy_text_fits_well_inside_the_default_token_budget() -> None:
    from analyst.monitor import CHARS_PER_TOKEN, DEFAULT_BUDGET

    text_tokens = DEFAULT_DISCLOSURE_POLICY.max_chars_per_bundle // CHARS_PER_TOKEN
    assert text_tokens <= DEFAULT_BUDGET.max_tokens // 4


@pytest.mark.parametrize(
    "kwargs",
    [
        {"max_chars_per_item": 0},
        {"max_chars_per_item": 13},
        {"max_items": -1},
        {"lookback_days": 0},
        {"max_chars_per_item": 500, "max_chars_per_bundle": 400},
    ],
)
def test_policy_refuses_unbounded_or_inconsistent_settings(kwargs: dict[str, int]) -> None:
    with pytest.raises(ValueError):
        DisclosurePolicy(**kwargs)


# ── determinism and secrets ────────────────────────────────────────────────────────────────────


def test_same_inputs_build_a_byte_identical_bundle() -> None:
    rows = (row(ref="a"), row(ts=AS_OF - timedelta(days=2), subject="Outcome", ref="b"))

    def build() -> bytes:
        index = AnnouncementIndex(reversed(rows))
        request = request_for_escalation(
            escalation(), thesis=make_thesis(), announcements=index, as_of=AS_OF, actor=Actor.T1
        )
        return BundleBuilder().build(request).bundle.canonical_bytes()

    assert build() == build()


def test_disclosure_path_never_imports_settings() -> None:
    """Nothing from `Settings` can reach a bundle (or the journal) if the path never imports it."""
    for module in (disclosures_module, bundle_module):
        imported: set[str] = set()
        for node in ast.walk(ast.parse(inspect.getsource(module))):
            if isinstance(node, ast.ImportFrom) and node.module:
                imported.add(node.module)
                imported.update(alias.name for alias in node.names)
            elif isinstance(node, ast.Import):
                imported.update(alias.name for alias in node.names)
        assert not any(name.startswith("dataplatform.config") for name in imported)
        assert "Settings" not in imported
        assert "os" not in imported
