"""M6.3 — the evidence bundle builder (§5.4 T1/T2 input, §5.7 reconstruction).

The three acceptance criteria, made concrete and each with an inversion that fails if the logic
is reversed (the CLAUDE.md rule for anything touching a PIT boundary or a cost bound):

1. **Reproducible from its content hash, and contains exactly what was sent.** The bundle is an
   `EvidenceBundle`, so its identity is the sha256 of its canonical bytes: two builds of the same
   request address the same bundle, a stored bundle comes back byte-identically, and the rendered
   prompt (what the model is sent) is carried in the snapshot alongside the structured items it was
   built from (`test_reproducible_*`, `test_bundle_contains_*`).
2. **Token count measured and bounded by a configured budget.** The count is reported on every
   build and never exceeds the budget: a filings/news pool far larger than the budget is trimmed
   most-recent-first and the overflow is *reported*, and a budget too small for the essentials is
   refused rather than truncating the flag or the break conditions (`test_budget_*`, `test_trim_*`).
3. **PIT discipline.** A bundle for date D contains nothing knowable after D — a future-dated news
   item, filing or price fact makes the whole build refuse, and same-day and earlier facts are kept
   (`test_pit_*`).

Nothing here hits the network or a database: the store is a real `EvidenceStore` rooted at a
`tmp_path`, which is how M5.1's own suite proves reconstruction offline.
"""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path

import pytest

from analyst.journal import Actor, EvidenceKind, EvidenceStore
from analyst.monitor import (
    CHARS_PER_TOKEN,
    DEFAULT_BUDGET,
    BundleBudget,
    BundleBudgetError,
    BundleBuilder,
    BundlePitError,
    BundleRequest,
    PriceFact,
    T0Check,
    T0Flag,
    count_tokens,
)
from analyst.thesis import (
    BreakCondition,
    BreakConditionType,
    EvaluationTier,
    Thesis,
)
from dataplatform.clock import IST
from dataplatform.ingest.announcements import AnnouncementRow
from dataplatform.ingest.news import NewsRow

CASE_ID = "AI_ROBOTICS"
ISIN = "INE001A01001"
TRADING_DATE = datetime(2026, 8, 7, 19, 30, tzinfo=IST).date()


# ── builders for the inputs ────────────────────────────────────────────────────────────────────


def make_thesis() -> Thesis:
    """A minimal ratifiable thesis with two falsifiable break conditions."""
    return Thesis(
        case_id=CASE_ID,
        isin=ISIN,
        version=1,
        driver="pure-play exposure to industrial automation capex",
        theme_purity=Decimal("0.8"),
        expected_evidence=("order book growth two consecutive quarters",),
        break_conditions=(
            BreakCondition(
                id="BC1",
                type=BreakConditionType.FUNDAMENTAL,
                condition="segment revenue falls two consecutive quarters",
                evaluation_tier=EvaluationTier.T1,
                evaluation="T1 on the quarterly results filing",
            ),
            BreakCondition(
                id="BC3",
                type=BreakConditionType.INTEGRITY,
                condition="auditor resignation disclosed",
                evaluation_tier=EvaluationTier.T0,
                evaluation="T0 keyword on the announcement feed",
            ),
        ),
    )


def make_flag() -> T0Flag:
    """A T0 announcement flag — the trigger a T1 review reads."""
    return T0Flag(
        check=T0Check.ANNOUNCEMENT,
        isin=ISIN,
        case_id=CASE_ID,
        summary="break condition BC1 keyword hit on 1 announcement: Quarterly results",
        detail={"break_condition_id": "BC1", "hits": "1"},
    )


def make_price(label: str = "close", value: str = "1234.55") -> PriceFact:
    return PriceFact(
        isin=ISIN,
        label=label,
        value=Decimal(value),
        as_of=TRADING_DATE,
        knowable_at=datetime(2026, 8, 7, 15, 30, tzinfo=IST),
    )


def make_news(day: int = 6, hour: int = 10, minute: int = 0, tag: str = "1") -> NewsRow:
    return NewsRow(
        ts=datetime(2026, 8, day, hour, minute, tzinfo=IST),
        source="rss_moneycontrol",
        title=f"Company reports development {tag}",
        url=f"https://example.test/{tag}",
    )


def make_announcement(day: int = 7, hour: int = 9, tag: str = "A") -> AnnouncementRow:
    return AnnouncementRow(
        ts=datetime(2026, 8, day, hour, 0, tzinfo=IST),
        source="nse_announcements",
        isin=ISIN,
        subject=f"Board meeting outcome {tag}",
        body="The board approved the audited financial results for the quarter.",
    )


def make_request(**overrides: object) -> BundleRequest:
    base: dict[str, object] = {
        "case_id": CASE_ID,
        "isin": ISIN,
        "trading_date": TRADING_DATE,
        "flag": make_flag(),
        "thesis": make_thesis(),
        "prices": (make_price(),),
        "announcements": (make_announcement(),),
        "news": (make_news(),),
        "actor": Actor.T1,
    }
    base.update(overrides)
    return BundleRequest(**base)  # type: ignore[arg-type]


@pytest.fixture
def store(tmp_path: Path) -> EvidenceStore:
    return EvidenceStore(tmp_path / "evidence")


# ── the token estimator ──────────────────────────────────────────────────────────────────────


def test_count_tokens_is_ceil_of_chars_over_ratio() -> None:
    assert count_tokens("") == 0
    assert count_tokens("a") == 1
    assert count_tokens("a" * CHARS_PER_TOKEN) == 1
    assert count_tokens("a" * (CHARS_PER_TOKEN + 1)) == 2


def test_count_tokens_is_deterministic() -> None:
    text = "the same prompt always estimates the same"
    assert count_tokens(text) == count_tokens(text)


# ── acceptance 1: reproducible from content hash, contains exactly what was sent ────────────────


def test_reproducible_two_builds_of_the_same_request_address_the_same_bundle() -> None:
    first = BundleBuilder().build(make_request())
    second = BundleBuilder().build(make_request())
    assert first.ref == second.ref
    assert first.bundle.canonical_bytes() == second.bundle.canonical_bytes()


def test_reproducible_a_stored_bundle_comes_back_byte_identically(store: EvidenceStore) -> None:
    built = BundleBuilder().build(make_request())
    ref = store.put(built.bundle)
    assert store.get(ref) == built.bundle.canonical_bytes()
    assert store.load(ref) == built.bundle


def test_reproducible_the_ref_is_the_reference_a_journal_entry_carries(
    store: EvidenceStore,
) -> None:
    built = BundleBuilder().build(make_request())
    ref = store.put(built.bundle)
    assert built.ref == ref.ref
    assert built.ref.startswith("sha256:")


def test_bundle_contains_the_rendered_prompt_that_was_measured() -> None:
    built = BundleBuilder().build(make_request())
    assert built.bundle.rendered_prompt == built.rendered_prompt
    assert built.token_count == count_tokens(built.rendered_prompt)


def test_bundle_contains_the_flag_thesis_break_conditions_and_price() -> None:
    built = BundleBuilder().build(make_request())
    prompt = built.rendered_prompt
    # the flag (trigger)
    assert "break condition BC1 keyword hit" in prompt
    # the thesis and its break conditions
    assert "pure-play exposure to industrial automation capex" in prompt
    assert "segment revenue falls two consecutive quarters" in prompt
    assert "auditor resignation disclosed" in prompt
    # the price/flow context
    assert "1234.55" in prompt
    # the recent filing and news
    assert "Board meeting outcome A" in prompt
    assert "Company reports development 1" in prompt


def test_bundle_items_mirror_the_rendered_evidence() -> None:
    built = BundleBuilder().build(make_request())
    kinds = [item.kind for item in built.bundle.items]
    assert EvidenceKind.THESIS in kinds  # driver + each break condition
    assert EvidenceKind.PRICE in kinds
    assert EvidenceKind.FILING in kinds
    assert EvidenceKind.NEWS in kinds
    # a T1 bundle is a review tier's snapshot
    assert built.bundle.actor is Actor.T1
    assert built.bundle.trading_date == TRADING_DATE


def test_a_t2_bundle_is_accepted_and_a_non_review_actor_is_refused() -> None:
    built = BundleBuilder().build(make_request(actor=Actor.T2))
    assert built.bundle.actor is Actor.T2
    with pytest.raises(ValueError, match="review tier"):
        make_request(actor=Actor.T0)


# ── acceptance 2: token count measured and bounded ─────────────────────────────────────────────


def test_budget_token_count_is_reported_and_within_budget() -> None:
    built = BundleBuilder().build(make_request())
    assert built.token_count > 0
    assert built.token_count <= DEFAULT_BUDGET.max_tokens
    assert built.budget == DEFAULT_BUDGET


def test_trim_a_pool_larger_than_the_budget_is_bounded_and_the_overflow_reported() -> None:
    pool = tuple(make_news(hour=(i % 12) + 1, minute=i % 60, tag=str(i)) for i in range(300))
    budget = BundleBudget(max_tokens=600)
    built = BundleBuilder(budget=budget).build(make_request(news=pool, announcements=()))
    assert built.token_count <= budget.max_tokens
    assert built.dropped > 0
    assert built.included < len(pool)
    assert built.included + built.dropped == len(pool)


def test_trim_is_the_inversion_guard_a_bundle_never_comes_out_over_budget() -> None:
    """If the bound were applied with the comparison reversed, this over-full pool would leak."""
    pool = tuple(make_news(hour=(i % 12) + 1, minute=i % 60, tag=str(i) * 30) for i in range(400))
    for cap in (300, 700, 2500):
        built = BundleBuilder(budget=BundleBudget(max_tokens=cap)).build(
            make_request(news=pool, announcements=())
        )
        assert built.token_count <= cap, (cap, built.token_count)


def test_trim_keeps_the_most_recent_evidence_and_drops_the_oldest() -> None:
    newest = make_news(day=7, hour=14, tag="newest")
    middle = make_news(day=6, hour=10, tag="middle")
    oldest = make_news(day=5, hour=8, tag="oldest")
    # a budget with room for the essentials plus exactly one short news line
    essentials_only = BundleBuilder(budget=BundleBudget(max_tokens=10_000)).build(
        make_request(prices=(), announcements=(), news=())
    )
    cap = (
        essentials_only.token_count
        + count_tokens(
            "\n- [NEWS " + newest.ts.isoformat() + "] " + newest.source + ": " + str(newest.title)
        )
        + 5
    )
    built = BundleBuilder(budget=BundleBudget(max_tokens=cap)).build(
        make_request(prices=(), announcements=(), news=(oldest, middle, newest))
    )
    assert built.included == 1
    assert built.dropped == 2
    assert "newest" in built.rendered_prompt
    assert "oldest" not in built.rendered_prompt


def test_budget_too_small_for_the_essentials_is_refused_not_truncated() -> None:
    with pytest.raises(BundleBudgetError, match="too small"):
        BundleBuilder(budget=BundleBudget(max_tokens=5)).build(make_request())


def test_budget_rejects_a_non_positive_ceiling() -> None:
    with pytest.raises(ValueError, match="positive"):
        BundleBudget(max_tokens=0)


def test_an_empty_optional_pool_still_builds_within_budget() -> None:
    built = BundleBuilder().build(make_request(news=(), announcements=()))
    assert built.included == 0
    assert built.dropped == 0
    assert built.token_count <= DEFAULT_BUDGET.max_tokens
    assert "none within the token budget" in built.rendered_prompt


# ── acceptance 3: PIT discipline — nothing knowable after D ─────────────────────────────────────


def test_pit_a_future_news_item_refuses_the_whole_bundle() -> None:
    future = make_news(day=8, tag="tomorrow")
    with pytest.raises(BundlePitError, match="knowable"):
        BundleBuilder().build(make_request(news=(future,)))


def test_pit_a_future_filing_refuses_the_whole_bundle() -> None:
    future = make_announcement(day=8, tag="tomorrow")
    with pytest.raises(BundlePitError, match="trading date"):
        BundleBuilder().build(make_request(announcements=(future,)))


def test_pit_a_future_price_fact_refuses_the_whole_bundle() -> None:
    future = PriceFact(
        isin=ISIN,
        label="close",
        value=Decimal("1300"),
        as_of=TRADING_DATE,
        knowable_at=datetime(2026, 8, 8, 15, 30, tzinfo=IST),
    )
    with pytest.raises(BundlePitError):
        BundleBuilder().build(make_request(prices=(future,)))


def test_pit_same_day_and_earlier_facts_are_kept() -> None:
    same_day = make_news(day=7, hour=23, minute=59, tag="eod")
    earlier = make_news(day=1, tag="early")
    built = BundleBuilder().build(make_request(news=(same_day, earlier), announcements=()))
    assert built.dropped == 0
    assert "eod" in built.rendered_prompt
    assert "early" in built.rendered_prompt


def test_pit_a_late_utc_timestamp_that_is_next_day_ist_is_a_leak() -> None:
    """23:00 UTC on D is 04:30 IST on D+1 — the leak the IST-date rule must catch."""
    late_utc = NewsRow(
        ts=datetime(2026, 8, 7, 23, 0, tzinfo=UTC),
        source="rss_moneycontrol",
        title="published late UTC",
        url="https://example.test/late",
    )
    with pytest.raises(BundlePitError):
        BundleBuilder().build(make_request(news=(late_utc,)))


def test_pit_a_utc_timestamp_still_on_the_session_in_ist_is_kept() -> None:
    utc_on_d = NewsRow(
        ts=datetime(2026, 8, 7, 12, 0, tzinfo=UTC),  # 17:30 IST on D
        source="rss_moneycontrol",
        title="published midday UTC on D",
        url="https://example.test/mid",
    )
    built = BundleBuilder().build(make_request(news=(utc_on_d,), announcements=()))
    assert "midday UTC on D" in built.rendered_prompt


# ── input guards ────────────────────────────────────────────────────────────────────────────


def test_price_fact_rejects_a_float_value() -> None:
    with pytest.raises(TypeError, match="Decimal"):
        PriceFact(
            isin=ISIN,
            label="close",
            value=1234.55,  # type: ignore[arg-type]
            as_of=TRADING_DATE,
            knowable_at=datetime(2026, 8, 7, 15, 30, tzinfo=IST),
        )


def test_price_fact_rejects_a_naive_knowable_at() -> None:
    with pytest.raises(ValueError, match="tz-aware"):
        PriceFact(
            isin=ISIN,
            label="close",
            value=Decimal("1234.55"),
            as_of=TRADING_DATE,
            knowable_at=datetime(2026, 8, 7, 15, 30),
        )
