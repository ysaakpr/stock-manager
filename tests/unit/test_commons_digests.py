"""M17.2 — factual filing digests in the Commons (pre-registration §1, §4 step 2).

What these tests pin, against the acceptance criteria:

1. **Digested once.** A filing is digested by one model call the first time a build sees it.
   Rebuilding the same session makes no call. The next session's build calls only for the filing
   that is new. Every manager reads the same stored digest, keyed by filing id.
2. **Opinions are refused.** The output schema has no recommendation, rating or price-view field
   and forbids extra keys at every level. A model answer that carries one (``recommendation``,
   ``rating``, ``target_price``) is refused and never stored. So is opinion wording in any text
   field, and so is a stored record built around the validator. A refused filing is a named
   failure, retried by the next build.
3. **Bounded, metered, PIT.** A long announcement is cut to the bound and marked truncated, and an
   attachment is named, never fetched. Each digest carries the provider's token usage. Only
   universe names are digested. A future-dated filing raises `PitError`, and a red day refuses
   the build.

`StubLLM` answers every call (registered replies keyed by prompt digest), so nothing touches a
model or the network. The lake readers run on a scratch lake under ``tmp_path``.
"""

from __future__ import annotations

from collections.abc import Callable, Sequence
from dataclasses import dataclass, field, replace
from datetime import UTC, date, datetime
from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest

from analyst.commons import (
    DIGEST_MODEL,
    DIGEST_OUTPUT_SCHEMA,
    AnnouncementText,
    CommonsRefusedError,
    DigestBody,
    DigestRefusedError,
    DigestRun,
    DigestStore,
    FilingDigest,
    FilingInput,
    FilingKind,
    InMemoryDigestStore,
    LakeCommonsSource,
    SourceUnavailableError,
    build_digests,
)
from analyst.commons.digests import (
    DIGEST_SYSTEM_PROMPT,
    DIGEST_TOOL,
    MAX_FILING_CHARS,
    digest_messages,
    filing_inputs,
    opinion_terms,
    parse_digest,
)
from analyst.commons.sheets import FilingFact
from analyst.llm import (
    LLM,
    LLMResponse,
    Message,
    StopReason,
    StubLLM,
    StubReply,
    ToolCall,
    ToolSpec,
    Usage,
    prompt_digest,
)
from analyst.llm.client import DEFAULT_MAX_TOKENS
from dataplatform.clock import IST, FrozenClock
from dataplatform.ingest.announcements import AnnouncementBatch, AnnouncementRow, write_l1
from dataplatform.ingest.xbrl.models import Filing, FundamentalFact, Nature, Taxonomy
from dataplatform.query import Dataset, PitError
from dataplatform.store.pit_fundamentals import write_pit
from tests.unit.test_commons_sheets import FALLING, RISING, SPLIT, Gate, _weekdays

CALENDAR = _weekdays(date(2026, 10, 9), 12)
DAY1, DAY2, DAY3 = CALENDAR[-3], CALENDAR[-2], CALENDAR[-1]
UNIVERSE = frozenset({RISING, FALLING})
OUTSIDER = SPLIT

GOOD_BODY: dict[str, Any] = {
    "headline": "Order received",
    "disclosed": "The company received a letter of award for a road project worth Rs 120 crore.",
    "period": None,
    "figures": [{"label": "Order value", "value": "120", "unit": "INR crore", "period": None}],
}


def _ts(day: date, hour: int = 15) -> datetime:
    return datetime(day.year, day.month, day.day, hour, 0, tzinfo=IST)


def _clock(day: date = DAY3, hour: int = 21) -> FrozenClock:
    return FrozenClock(_ts(day, hour))


def _ann(
    isin: str, ref: str, day: date, body: str | None = "Award letter received."
) -> AnnouncementText:
    return AnnouncementText(
        isin=isin,
        ref=ref,
        ts=_ts(day),
        knowable_date=day,
        category="Bagging/Receiving of orders/contracts",
        subject="Award of order",
        body=body,
        attachment_ref=f"https://nsearchives.nseindia.com/corporate/{ref}.pdf",
    )


def _results(isin: str, filing_id: str, filed: date) -> list[FilingFact]:
    end = date(2026, 6, 30)
    return [
        FilingFact(isin, date(2026, 4, 1), end, filed, filing_id, Nature.STANDALONE, c, None, v)
        for c, v in (
            ("revenue_from_operations", Decimal("12500000000")),
            ("profit_after_tax", Decimal("1500000000")),
            ("eps_basic", Decimal("12.5")),
        )
    ]


@dataclass
class DigestWorld:
    calendar: list[date] = field(default_factory=lambda: list(CALENDAR))
    announcements: list[AnnouncementText] = field(default_factory=list)
    results: list[FilingFact] = field(default_factory=list)


class FakeDigestSource:
    """Serves everything unfiltered, so the window and the PIT guard are what select."""

    def __init__(self, world: DigestWorld, *, missing: frozenset[str] = frozenset()) -> None:
        self.world = world
        self.missing = missing

    def _serve[R](
        self, name: str, records: Sequence[R], knowable: Callable[[R], date]
    ) -> Dataset[R]:
        if name in self.missing:
            raise SourceUnavailableError(name, "not built in this fixture")
        return Dataset.declaring(name, records, knowable_date=knowable)

    def sessions(self, through: date, count: int) -> Dataset[date]:
        upto = [d for d in self.world.calendar if d <= through]
        return self._serve("sessions", upto[-count:], lambda d: d)

    def announcement_texts(self, start: date, through: date) -> Dataset[AnnouncementText]:
        return self._serve("announcements", self.world.announcements, lambda r: r.knowable_date)

    def results_filings(self, start: date, through: date) -> Dataset[FilingFact]:
        return self._serve("pit_fundamentals", self.world.results, lambda f: f.filing_date)


def _world() -> DigestWorld:
    return DigestWorld(
        announcements=[
            _ann(RISING, "r-old", CALENDAR[-6]),  # before the first window
            _ann(RISING, "r-1", DAY2),
            _ann(RISING, "r-1", DAY2),  # the same disclosure polled twice
            _ann(FALLING, "f-1", DAY2),
            _ann(OUTSIDER, "o-1", DAY2),  # not in the universe
        ],
        results=_results(FALLING, "F-Q1", DAY2),
    )


def _reply(body: dict[str, Any], usage: Usage | None = None) -> StubReply:
    return StubReply(
        text="",
        tool_calls=(ToolCall(id="structured_output", name=DIGEST_TOOL.name, arguments=body),),
        usage=usage or Usage(input_tokens=321, output_tokens=54),
    )


def _key(item: FilingInput) -> str:
    return prompt_digest(
        digest_messages(item), model=DIGEST_MODEL, tools=(DIGEST_TOOL,), system=DIGEST_SYSTEM_PROMPT
    )


def _stub(world: DigestWorld, overrides: dict[str, StubReply] | None = None) -> StubLLM:
    """A strict stub with a good digest registered for every filing the world holds."""
    items = filing_inputs(
        world.announcements,
        world.results,
        isins=frozenset({RISING, FALLING, OUTSIDER}),
        after=date.min,
        through=date.max,
    )
    replies = {_key(i): (overrides or {}).get(i.filing_id, _reply(GOOD_BODY)) for i in items}
    return StubLLM(replies, synthesize_unknown=False)


def _run(
    world: DigestWorld,
    store: DigestStore,
    llm: LLM,
    session: date = DAY2,
    **kwargs: Any,
) -> DigestRun:
    return build_digests(
        session,
        isins=UNIVERSE,
        source=FakeDigestSource(world, missing=kwargs.pop("missing", frozenset())),
        llm=llm,
        store=store,
        gate=kwargs.pop("gate", Gate()),
        clock=kwargs.pop("clock", _clock(session)),
        **kwargs,
    )


# ── digested once ────────────────────────────────────────────────────────────────────────────────


def test_each_new_filing_is_digested_by_one_call() -> None:
    world, store = _world(), InMemoryDigestStore()
    llm = _stub(world)
    run = _run(world, store, llm)
    expected = {
        "nse_announcements:r-1",
        "nse_announcements:f-1",
        "pit_fundamentals:F-Q1",
    }
    assert set(run.filing_ids) == set(run.digested) == expected
    assert run.cached == () and run.failures == () and run.gaps == ()
    assert len(llm.calls) == 3
    assert {c.model for c in llm.calls} == {DIGEST_MODEL}
    assert run.since == CALENDAR[-3]  # no earlier run: the window opens after the previous session
    for filing_id in expected:
        digest = store.get(filing_id)
        assert digest is not None and digest.body == DigestBody.model_validate(GOOD_BODY)


def test_a_rebuild_of_the_same_session_makes_no_call() -> None:
    world, store = _world(), InMemoryDigestStore()
    _run(world, store, _stub(world))
    again = _stub(world)
    run = _run(world, store, again)
    assert again.calls == ()
    assert run.digested == () and set(run.cached) == set(run.filing_ids)
    assert len(store.runs()) == 1  # the same window and outcome is the same run


def test_the_next_session_digests_only_what_is_new() -> None:
    world, store = _world(), InMemoryDigestStore()
    _run(world, store, _stub(world))
    world.announcements.append(_ann(RISING, "r-2", DAY3))
    llm = _stub(world)
    run = _run(world, store, llm, session=DAY3)
    assert run.since == DAY2  # the last clean run
    assert run.digested == ("nse_announcements:r-2",)
    assert len(llm.calls) == 1


def test_every_manager_reads_the_one_stored_digest() -> None:
    world, store = _world(), InMemoryDigestStore()
    _run(world, store, _stub(world))
    first = store.get("nse_announcements:r-1")
    _run(world, store, _stub(world))
    assert store.get("nse_announcements:r-1") is first
    assert store.record(first, recorded_at=_ts(DAY3)) is False  # type: ignore[arg-type]


def test_token_usage_is_recorded_per_digest() -> None:
    world, store = _world(), InMemoryDigestStore()
    usage = Usage(input_tokens=900, output_tokens=120, cache_write_tokens=7, cache_read_tokens=3)
    _run(world, store, _stub(world, {"nse_announcements:r-1": _reply(GOOD_BODY, usage)}))
    digest = store.get("nse_announcements:r-1")
    assert digest is not None
    assert (digest.input_tokens, digest.output_tokens) == (900, 120)
    assert (digest.cache_write_tokens, digest.cache_read_tokens) == (7, 3)
    assert digest.provider == "stub" and digest.model == DIGEST_MODEL
    assert len(digest.prompt_digest) == 64 and len(digest.input_digest) == 64


# ── opinions are refused ─────────────────────────────────────────────────────────────────────────


def _walk_objects(schema: Any) -> list[dict[str, Any]]:
    found: list[dict[str, Any]] = []
    if isinstance(schema, dict):
        if schema.get("type") == "object":
            found.append(schema)
        for value in schema.values():
            found += _walk_objects(value)
    elif isinstance(schema, list):
        for value in schema:
            found += _walk_objects(value)
    return found


def test_the_schema_has_no_opinion_field_and_forbids_extras_everywhere() -> None:
    objects = _walk_objects(dict(DIGEST_OUTPUT_SCHEMA))
    assert len(objects) == 2  # the digest and a figure
    names: set[str] = set()
    for obj in objects:
        assert obj["additionalProperties"] is False
        names |= set(obj["properties"])
    assert names == {"headline", "disclosed", "period", "figures", "label", "value", "unit"}
    for word in ("recommend", "rating", "target", "view", "action", "signal", "score", "outlook"):
        assert not any(word in name for name in names)
    assert set(DigestBody.model_fields) == {"headline", "disclosed", "period", "figures"}


@pytest.mark.parametrize(
    "extra",
    [
        {"recommendation": "BUY"},
        {"rating": "OUTPERFORM"},
        {"target_price": "1450"},
        {"price_view": "cheap"},
    ],
)
def test_a_digest_with_an_opinion_field_is_refused(extra: dict[str, str]) -> None:
    with pytest.raises(DigestRefusedError, match="outside the schema"):
        parse_digest({**GOOD_BODY, **extra})


def test_an_opinion_field_inside_a_figure_is_refused() -> None:
    body = {**GOOD_BODY, "figures": [{**GOOD_BODY["figures"][0], "rating": "BUY"}]}
    with pytest.raises(DigestRefusedError, match="outside the schema"):
        parse_digest(body)


@pytest.mark.parametrize(
    "text",
    [
        "Strong order win; we recommend accumulating.",
        "Revenue rose 12 %, and the shares look undervalued.",
        "Brokers set a target price of Rs 1,450.",
        "The quarter was bullish for the stock.",
        "The stock should outperform its peers.",
    ],
)
def test_opinion_wording_is_refused(text: str) -> None:
    with pytest.raises(DigestRefusedError, match="opinion"):
        parse_digest({**GOOD_BODY, "disclosed": text})


def test_a_credit_rating_action_is_a_fact_and_passes() -> None:
    body = parse_digest(
        {
            **GOOD_BODY,
            "disclosed": "CRISIL reaffirmed the long-term rating at AA+/Stable on bank loans "
            "of Rs 500 crore. Investments are carried at fair value.",
        }
    )
    assert opinion_terms(body) == ()


def test_a_stored_record_cannot_carry_an_opinion() -> None:
    world, store = _world(), InMemoryDigestStore()
    _run(world, store, _stub(world))
    digest = store.get("nse_announcements:r-1")
    assert digest is not None
    payload = digest.model_dump()
    payload["body"] = {**GOOD_BODY, "disclosed": "Shares are overvalued."}
    with pytest.raises(ValueError, match="opinion"):
        FilingDigest.model_validate(payload)
    payload["body"] = GOOD_BODY
    payload["recommendation"] = "SELL"
    with pytest.raises(ValueError, match="recommendation"):
        FilingDigest.model_validate(payload)


def test_a_build_refuses_an_opinionated_answer_and_stores_nothing_for_it() -> None:
    world, store = _world(), InMemoryDigestStore()
    bad = {"nse_announcements:r-1": _reply({**GOOD_BODY, "recommendation": "BUY"})}
    run = _run(world, store, _stub(world, bad))
    assert [f.filing_id for f in run.failures] == ["nse_announcements:r-1"]
    assert "recommendation" in run.failures[0].reason
    assert store.get("nse_announcements:r-1") is None
    assert store.get("nse_announcements:f-1") is not None  # one refusal does not stop the others
    # The run was not clean, so the next session's window reopens it, and a good answer lands.
    retry = _run(world, store, _stub(world), session=DAY3)
    assert retry.since == CALENDAR[-3]
    assert retry.digested == ("nse_announcements:r-1",)
    assert store.get("nse_announcements:r-1") is not None


@pytest.mark.parametrize(
    "reply",
    [
        StubReply(text='{"headline": "free text, not the schema"}'),
        StubReply(text="", tool_calls=(ToolCall("x", "something_else", GOOD_BODY),)),
        StubReply(
            text="",
            tool_calls=(ToolCall("x", DIGEST_TOOL.name, GOOD_BODY),),
            stop_reason=StopReason.MAX_TOKENS,
        ),
    ],
)
def test_an_answer_without_the_structured_digest_is_a_failure(reply: StubReply) -> None:
    world, store = _world(), InMemoryDigestStore()
    run = _run(world, store, _stub(world, {"nse_announcements:f-1": reply}))
    assert [f.filing_id for f in run.failures] == ["nse_announcements:f-1"]
    assert store.get("nse_announcements:f-1") is None


class _BrokenLLM:
    def complete(
        self,
        messages: Sequence[Message],
        *,
        model: str,
        tools: Sequence[ToolSpec] = (),
        system: str | None = None,
        max_tokens: int = DEFAULT_MAX_TOKENS,
    ) -> LLMResponse:
        from analyst.llm import LLMError

        raise LLMError("provider unavailable")


def test_a_provider_failure_is_named_per_filing() -> None:
    world, store = _world(), InMemoryDigestStore()
    run = _run(world, store, _BrokenLLM())
    assert len(run.failures) == 3 and run.digested == ()
    assert all("provider unavailable" in f.reason for f in run.failures)


# ── bounded, PIT, refusals ───────────────────────────────────────────────────────────────────────


def test_a_long_announcement_is_bounded_and_marked() -> None:
    (item,) = filing_inputs(
        [_ann(RISING, "long", DAY2, body="x" * (MAX_FILING_CHARS * 3))],
        [],
        isins=UNIVERSE,
        after=DAY1,
        through=DAY2,
    )
    assert item.truncated is True and len(item.text) == MAX_FILING_CHARS
    (short,) = filing_inputs(
        [_ann(RISING, "short", DAY2)], [], isins=UNIVERSE, after=DAY1, through=DAY2
    )
    assert short.truncated is False
    # The attachment is named, never fetched: its link is in the text, its content is not.
    assert "Attachment (not included): https://nsearchives" in short.text
    # The dissemination time is stated in IST whatever zone the store returned it in.
    utc = _ann(RISING, "utc", DAY2)
    utc = replace(utc, ts=utc.ts.astimezone(UTC))
    (item_utc,) = filing_inputs([utc], [], isins=UNIVERSE, after=DAY1, through=DAY2)
    assert f"Disseminated: {_ts(DAY2).isoformat()}" in item_utc.text


def test_a_results_filing_is_one_input_with_its_facts() -> None:
    (item,) = filing_inputs(
        [], _results(FALLING, "F-Q1", DAY2), isins=UNIVERSE, after=DAY1, through=DAY2
    )
    assert item.kind is FilingKind.RESULTS and item.filing_id == "pit_fundamentals:F-Q1"
    assert "revenue_from_operations | 12500000000" in item.text
    assert "eps_basic | 12.5" in item.text
    assert item.knowable_date == DAY2


def test_only_universe_names_in_the_window_are_digested() -> None:
    world, store = _world(), InMemoryDigestStore()
    run = _run(world, store, _stub(world))
    assert "nse_announcements:o-1" not in run.filing_ids
    assert "nse_announcements:r-old" not in run.filing_ids


def test_a_future_filing_trips_the_pit_guard() -> None:
    world, store = _world(), InMemoryDigestStore()
    world.announcements.append(_ann(RISING, "tomorrow", DAY3))
    with pytest.raises(PitError):
        _run(world, store, _stub(world), session=DAY2)


def test_a_red_day_and_a_future_session_are_refused() -> None:
    world, store = _world(), InMemoryDigestStore()
    with pytest.raises(CommonsRefusedError, match="red"):
        _run(world, store, _stub(world), gate=Gate(green=False, reason="EOD late"))
    with pytest.raises(CommonsRefusedError, match="after today"):
        _run(world, store, _stub(world), session=DAY3, clock=_clock(DAY2))
    assert store.runs() == ()


def test_a_missing_source_is_a_gap_and_the_rest_is_digested() -> None:
    world, store = _world(), InMemoryDigestStore()
    run = _run(world, store, _stub(world), missing=frozenset({"pit_fundamentals"}))
    assert [g.source for g in run.gaps] == ["results:pit_fundamentals"]
    assert set(run.digested) == {"nse_announcements:r-1", "nse_announcements:f-1"}


def test_a_long_absence_caps_the_window_and_says_so() -> None:
    world, store = _world(), InMemoryDigestStore()
    _run(DigestWorld(), store, _stub(world), session=CALENDAR[-10])  # a quiet day, long ago
    run = _run(world, store, _stub(world), session=DAY3)
    assert run.since == CALENDAR[-6]
    assert [g.source for g in run.gaps] == ["digests"]


# ── the lake readers ─────────────────────────────────────────────────────────────────────────────


def _lake(root: Path) -> Path:
    rows = (
        AnnouncementRow(
            ts=_ts(DAY2),
            source="nse_announcements",
            isin=RISING,
            symbol="RISE",
            category="Updates",
            subject="Board meeting outcome",
            body="The board approved a dividend of Rs 5 per share.",
            attachment_ref="https://nsearchives.nseindia.com/corporate/x.pdf",
            source_ref="seq-1",
        ),
        AnnouncementRow(
            ts=_ts(DAY2),
            source="bse_announcements",
            isin=RISING,
            subject="Same thing on BSE",
            source_ref="bse-1",
        ),
    )
    write_l1(
        AnnouncementBatch(logical_date=DAY2, source="nse_announcements", rows=rows),
        data_root=root,
    )
    end = date(2026, 6, 30)
    facts = tuple(
        FundamentalFact(
            isin=FALLING,
            period_start=date(2026, 4, 1),
            period_end=end,
            filing_date=DAY2,
            nature=Nature.STANDALONE,
            taxonomy=Taxonomy.IND_AS,
            filing_id="F-Q1",
            concept=concept,
            segment=None,
            value=Decimal(value),
            derived=False,
            source="nse_filings",
            l0_key=None,
        )
        for concept, value in (("revenue_from_operations", "1000"), ("face_value_per_share", "10"))
    )
    write_pit(
        Filing(
            isin=FALLING,
            symbol="FALL",
            taxonomy=Taxonomy.IND_AS,
            name="Fixture Co",
            period_start=date(2026, 4, 1),
            period_end=end,
            filing_date=DAY2,
            nature=Nature.STANDALONE,
            filing_id="F-Q1",
            source="nse_filings",
            facts=facts,
        ),
        data_root=root,
    )
    return root


def test_the_lake_reads_nse_texts_and_every_fact_of_a_results_filing(tmp_path: Path) -> None:
    root = _lake(tmp_path)
    with LakeCommonsSource(clock=_clock(), data_root=root) as source:
        (text,) = source.announcement_texts(DAY1, DAY2).records
        assert (text.isin, text.ref, text.knowable_date) == (RISING, "seq-1", DAY2)
        assert text.body == "The board approved a dividend of Rs 5 per share."
        facts = source.results_filings(DAY2, DAY2).records
        # Every concept, not only the ones the metrics read: face value is in the digest input.
        assert {f.concept for f in facts} == {"revenue_from_operations", "face_value_per_share"}
        assert source.results_filings(DAY3, DAY3).records == ()
        assert source.announcement_texts(DAY3, DAY3).records == ()


def test_the_lake_reports_absent_datasets(tmp_path: Path) -> None:
    with LakeCommonsSource(clock=_clock(), data_root=tmp_path) as source:
        with pytest.raises(SourceUnavailableError, match="announcements"):
            source.announcement_texts(DAY1, DAY2)
        with pytest.raises(SourceUnavailableError, match="pit_fundamentals"):
            source.results_filings(DAY1, DAY2)
