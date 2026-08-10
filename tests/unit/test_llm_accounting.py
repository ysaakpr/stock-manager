"""M5.4: the LLM client abstraction and per-decision token accounting (X3).

This file is M5.4's declared verify command, and it exists to make three acceptance claims
checkable rather than asserted:

1. **`StubLLM` is deterministic, needs no credential, and every response carries usage.** Proved
   by asking the same question twice and comparing the whole response, token counts included.
2. **An unconfigured `AnthropicLLM` raises a named, actionable error and never silently falls
   back.** Proved at every door into it — the constructor, `from_settings`, and `build_llm` with
   `LLM_PROVIDER=anthropic` — and paired with the leak checks, because "fails loud" and "fails
   without printing the key" are both required (AGENTIC_CONTEXT §6 invariant #13).
3. **A decision's journal line carries token and cost fields taken from the actual call.** Proved
   end to end: a metered call is priced once, and that one figure is asserted to appear both in
   the `token_usage` row's parameters and in the `JournalEntry` built from `journal_fields()`.

Two properties get more attention than the acceptance criteria ask for, because CLAUDE.md says
they must:

**Money is `Decimal`, never `float`.** `test_no_monetary_value_is_ever_a_float` walks every
monetary field the accounting layer produces — the whole loaded price card, a `PricedUsage`, its
`TokenSpend`, and the parameters that reach the `token_usage` INSERT — and fails on the first
`float` it finds, wherever a future edit introduces one. The price card is held to the rule its
own header states: `accounting/model_prices.yaml` says every number in it is a quoted string, and
`test_price_card_contains_no_unquoted_number` reads the file's YAML node tree and fails if any
scalar resolves to an int or a float tag. An unquoted `0.10` in that file is a binary float the
moment PyYAML sees it, which is the leak the rule guards.

**The cost arithmetic fails when it is inverted.** A test that passes under a reversed
implementation is worth nothing, so each arithmetic test asserts the correct figure *and* asserts
it differs from what the specific inversion would produce: input and output rates swapped, cache
write and cache read swapped, the FX rate divided instead of multiplied, the rounding applied
after the FX conversion instead of before, and `schedule_for` returning the oldest schedule in
force instead of the newest. Every one of those is a plausible edit and every one of them is
caught here.

Nothing here touches the network — `no_sockets` is autouse for the whole module, so a call that
tried would fail rather than hang. `AnthropicLLM` is exercised against frozen response fixtures
(`tests/fixtures/anthropic/messages_2026_06/`) served through an `httpx.MockTransport`, which is
the only way to check the request this client actually builds while no credential exists (B4).
Time is injected everywhere; `datetime.now()` appears nowhere (B10).
"""

from __future__ import annotations

import json
import socket
from collections.abc import Iterator, Sequence
from datetime import date, datetime
from decimal import ROUND_HALF_UP, Decimal
from pathlib import Path
from typing import Any, cast

import httpx
import pytest
import yaml
from pydantic import BaseModel, SecretStr, ValidationError

from accounting import (
    PRICES_PATH,
    MeteredCompletion,
    MeteredLLM,
    ModelPrice,
    NoPriceScheduleError,
    PriceCard,
    PricedUsage,
    TokenLedger,
    TokenPricer,
    UnknownModelError,
    UnknownUsageError,
    load_price_card,
)
from analyst.journal import Actor, Decision, JournalEntry, TokenSpend
from analyst.llm import (
    ANTHROPIC_PROVIDER,
    DEFAULT_MODEL,
    STUB_PROVIDER,
    TRIAGE_MODEL,
    AnthropicLLM,
    LLMCredentialError,
    LLMError,
    LLMRefusalError,
    LLMResponse,
    Message,
    Role,
    StopReason,
    StubLLM,
    StubReply,
    ToolSpec,
    UnknownPromptError,
    Usage,
    build_llm,
    prompt_digest,
)
from dataplatform.clock import IST, FrozenClock
from dataplatform.config import LlmProvider
from dataplatform.store.db import Connection
from tests.conftest import SettingsLoader

REPO_ROOT = Path(__file__).resolve().parents[2]
FIXTURES = REPO_ROOT / "tests" / "fixtures" / "anthropic" / "messages_2026_06"

TRADING_DATE = date(2026, 8, 7)

#: When the model call happened, and — deliberately two seconds later — when the row recording it
#: landed. They must differ: `ts` and `recorded_at` are separate columns precisely so a replayed
#: ledger is distinguishable from the original, and a test that set the ledger's clock to the same
#: instant it passed as `ts` could not tell the two columns apart. Same shape as
#: `tests/unit/test_journal.py`, for the same reason.
CALLED_AT = datetime(2026, 8, 7, 19, 30, tzinfo=IST)
RECORDED_AT = datetime(2026, 8, 7, 19, 30, 2, tzinfo=IST)

CASE_ID = "AI_ROBOTICS"

#: Not a credential and not shaped like one. A string that looked like a real key would be a
#: credential-shaped literal in a public repo (invariant #13), which is a defect even when fake.
FAKE_KEY = "test-value-that-is-not-a-credential"

#: A date inside the first schedule, before the Sonnet 5 introductory rate ends.
INTRO_DATE = date(2026, 8, 10)


# ── offline guarantee ────────────────────────────────────────────────────────────────────────


@pytest.fixture(autouse=True)
def no_sockets(monkeypatch: pytest.MonkeyPatch) -> None:
    """Make any network use in this module an immediate failure rather than a slow one.

    B8: the suite is offline and deterministic. B4: there is no credential to spend anyway, so a
    request that escaped would be a bug in the test, not a call that happened to work.
    """

    def forbidden(*args: Any, **kwargs: Any) -> Any:
        raise AssertionError("the LLM tests must never touch the network")

    monkeypatch.setattr(socket, "socket", forbidden)
    monkeypatch.setattr(socket, "create_connection", forbidden)
    monkeypatch.setattr(socket, "getaddrinfo", forbidden)


# ── helpers ──────────────────────────────────────────────────────────────────────────────────


def usage(
    input_tokens: int = 1000,
    output_tokens: int = 7,
    cache_write_tokens: int = 13,
    cache_read_tokens: int = 29,
) -> Usage:
    """Four deliberately distinct, coprime-ish counts, so swapping any two changes the total."""
    return Usage(
        input_tokens=input_tokens,
        output_tokens=output_tokens,
        cache_write_tokens=cache_write_tokens,
        cache_read_tokens=cache_read_tokens,
    )


def response(model: str = DEFAULT_MODEL, **overrides: Any) -> LLMResponse:
    fields: dict[str, Any] = {
        "provider": STUB_PROVIDER,
        "model": model,
        "text": "the driver is intact",
        "usage": usage(),
    }
    fields.update(overrides)
    return LLMResponse(**fields)


def rate(schedule_id: str, model: str, bucket: str) -> Decimal:
    """One rate off the real card, so the arithmetic tests assert against shipped numbers."""
    card = load_price_card()
    schedule = next(item for item in card.schedules if item.id == schedule_id)
    return cast(Decimal, getattr(schedule.price_for(model), bucket))


def micro(amount: Decimal) -> Decimal:
    return amount.quantize(Decimal("0.000001"), rounding=ROUND_HALF_UP)


class FakeCursor:
    """The one cursor method the ledger uses."""

    def __init__(self, rows: Sequence[tuple[Any, ...]]) -> None:
        self._rows = list(rows)

    def fetchone(self) -> tuple[Any, ...] | None:
        return self._rows[0] if self._rows else None


class RecordingConnection:
    """Records every statement and echoes an insert's parameters back as the inserted row.

    Faithful in the way that matters: `INSERT ... RETURNING id` hands back a row the server built
    from the parameters, so returning `(id, *parameters)` is what a real insert yields. `rows` is
    what a non-INSERT statement returns, which is how the `attach_decision` guard — an UPDATE
    whose WHERE clause may match nothing — is exercised in both directions.
    """

    def __init__(self) -> None:
        self.calls: list[tuple[str, list[Any]]] = []
        self.rows: list[tuple[Any, ...]] = []
        self.next_id = 1

    def execute(self, sql: str, params: Sequence[Any] | None = None) -> FakeCursor:
        recorded = list(params or ())
        self.calls.append((sql, recorded))
        if sql.lstrip().upper().startswith("INSERT"):
            row = (self.next_id, *recorded)
            self.next_id += 1
            return FakeCursor([row])
        return FakeCursor(self.rows)

    @property
    def last(self) -> tuple[str, list[Any]]:
        return self.calls[-1]

    def params_by_column(self, columns: Sequence[str]) -> dict[str, Any]:
        return dict(zip(columns, self.last[1], strict=True))


#: `accounting.tokens._WRITE_COLUMNS`, restated here on purpose. Importing the private tuple would
#: make the test agree with the code by construction; writing it out means a reordered INSERT has
#: to be noticed by a human.
WRITE_COLUMNS = (
    "ts",
    "provider",
    "model",
    "purpose",
    "case_id",
    "decision_journal_id",
    "tokens_in",
    "tokens_out",
    "cached_tokens",
    "cost_inr",
    "cost_usd",
    "recorded_at",
)


@pytest.fixture
def clock() -> FrozenClock:
    """Stopped at `RECORDED_AT`, which is *not* `CALLED_AT`. See the note on those constants."""
    return FrozenClock(RECORDED_AT)


@pytest.fixture
def conn() -> RecordingConnection:
    return RecordingConnection()


@pytest.fixture
def card() -> PriceCard:
    return load_price_card()


@pytest.fixture
def pricer(card: PriceCard) -> TokenPricer:
    return TokenPricer(card)


@pytest.fixture
def ledger(conn: RecordingConnection, clock: FrozenClock) -> TokenLedger:
    return TokenLedger(cast(Connection, conn), clock=clock)


# ── the price card keeps its own promise ─────────────────────────────────────────────────────


def test_price_card_states_the_quoted_number_rule() -> None:
    """The rule the next test enforces has to still be written down in the file it governs.

    Deleting the header comment and deleting the guard are the same mistake made twice; this
    fails on the first half of it.
    """
    header = PRICES_PATH.read_text(encoding="utf-8")
    assert "QUOTED STRING" in header
    assert "tests/unit/test_llm_accounting.py" in header


def test_price_card_contains_no_unquoted_number() -> None:
    """`model_prices.yaml` line 14 claims this test exists. It does, and this is the claim.

    Checked on the YAML *node* tree rather than the loaded document, because loading throws the
    quoting away: by the time `yaml.safe_load` returns, an unquoted `0.10` is already a binary
    float and indistinguishable from a `Decimal("0.10")` that a validator built from a string.
    The composer still knows — a bare `0.10` resolves to the float tag, a quoted `"0.10"` to the
    str tag — so the rule is enforceable only here.
    """
    numeric = {"tag:yaml.org,2002:int", "tag:yaml.org,2002:float"}
    with PRICES_PATH.open(encoding="utf-8") as handle:
        root = yaml.compose(handle)
    assert root is not None, "model_prices.yaml is empty"

    offenders = [
        (node.start_mark.line + 1, node.value)
        for node in _scalar_nodes(root)
        if node.tag in numeric
    ]
    assert offenders == [], (
        f"unquoted numbers survive in {PRICES_PATH.name} at line(s) "
        f"{[line for line, _ in offenders]}: {offenders}. YAML parses a bare number into a binary "
        "float, and a float in the cost model is a bug (CLAUDE.md) — quote it."
    )


def _scalar_nodes(node: yaml.Node) -> Iterator[yaml.ScalarNode]:
    """Every scalar in a composed YAML document, keys and values alike."""
    if isinstance(node, yaml.ScalarNode):
        yield node
    elif isinstance(node, yaml.SequenceNode):
        for child in node.value:
            yield from _scalar_nodes(child)
    elif isinstance(node, yaml.MappingNode):
        for key, value in node.value:
            yield from _scalar_nodes(key)
            yield from _scalar_nodes(value)


def test_the_guard_would_catch_an_unquoted_number(tmp_path: Path) -> None:
    """The guard above passes on a good file; this shows it fails on a bad one.

    Without this, a broken `_scalar_nodes` would make the claim vacuously true.
    """
    bad = tmp_path / "model_prices.yaml"
    bad.write_text("models:\n  claude-opus-5:\n    input: 5.00\n", encoding="utf-8")
    with bad.open(encoding="utf-8") as handle:
        root = yaml.compose(handle)
    assert root is not None
    tags = {node.tag for node in _scalar_nodes(root)}
    assert "tag:yaml.org,2002:float" in tags


def test_a_float_rate_is_refused_at_load(card: PriceCard) -> None:
    """The loader rejects a float even if one reached it another way — belt as well as braces."""
    with pytest.raises(ValidationError, match="quoted strings"):
        ModelPrice.model_validate(
            {"input": 5.0, "output": "25.00", "cache_write": "6.25", "cache_read": "0.50"}
        )


def test_the_shipped_card_prices_every_model_this_client_names(card: PriceCard) -> None:
    """`DEFAULT_MODEL` and `TRIAGE_MODEL` must be priced, or the first real call raises at ₹0."""
    schedule = card.schedule_for(INTRO_DATE)
    for model in (DEFAULT_MODEL, TRIAGE_MODEL):
        assert schedule.price_for(model).input > 0


# ── money is Decimal, never float ────────────────────────────────────────────────────────────


MONETARY_FIELDS = frozenset(
    {"input", "output", "cache_write", "cache_read", "usd_inr", "cost_usd", "cost_inr"}
)


def _monetary_values(value: object, path: str = "") -> Iterator[tuple[str, object]]:
    """Every value the accounting layer treats as money, with the path that reached it."""
    if isinstance(value, BaseModel):
        for name in type(value).model_fields:
            yield from _monetary_values(getattr(value, name), f"{path}.{name}")
    elif isinstance(value, PricedUsage):
        for name in ("usd_inr", "cost_usd", "cost_inr"):
            yield from _monetary_values(getattr(value, name), f"{path}.{name}")
    elif isinstance(value, dict):
        for key, item in value.items():
            yield from _monetary_values(item, f"{path}[{key!r}]")
    elif isinstance(value, list | tuple):
        for index, item in enumerate(value):
            yield from _monetary_values(item, f"{path}[{index}]")
    elif path.rsplit(".", 1)[-1].split("[")[0] in MONETARY_FIELDS:
        yield path, value


def test_no_monetary_value_is_ever_a_float(pricer: TokenPricer, card: PriceCard) -> None:
    """Fails on the first float in any monetary field, wherever a future edit puts one.

    Covers the whole shipped card, a priced call, and the journal fields derived from it. A
    `float` here is a defect of the same order as a credential in a commit (CLAUDE.md).
    """
    priced = pricer.price(response(), on=INTRO_DATE, purpose="t1_review")
    subjects: dict[str, object] = {
        "card": card,
        "priced": priced,
        "token_spend": priced.token_spend,
    }

    checked = 0
    for name, subject in subjects.items():
        for path, value in _monetary_values(subject, name):
            checked += 1
            assert not isinstance(value, float), f"{path} is a float: {value!r}"
            assert isinstance(value, Decimal), f"{path} is {type(value).__name__}, not Decimal"

    # A walk that silently stopped reaching anything would make this test pass on an empty set.
    # The expected count is derived from the card, so adding a model or a schedule keeps it exact:
    # one `usd_inr` and four rates per model per schedule, plus three on the priced call and one
    # on its journal fields.
    expected = sum(1 + 4 * len(schedule.models) for schedule in card.schedules) + 4
    assert checked == expected, f"the walk reached {checked} monetary fields, expected {expected}"


def test_the_ledger_writes_decimals_not_floats(
    pricer: TokenPricer, ledger: TokenLedger, conn: RecordingConnection
) -> None:
    """The two money columns reach the database as `Decimal`, so NUMERIC(20, 6) stores exactly."""
    priced = pricer.price(response(), on=INTRO_DATE, purpose="t1_review")
    ledger.record(priced, ts=CALLED_AT, case_id=CASE_ID)

    written = conn.params_by_column(WRITE_COLUMNS)
    for column in ("cost_inr", "cost_usd"):
        assert isinstance(written[column], Decimal), f"{column} reached the ledger as a non-Decimal"
        assert not isinstance(written[column], float)


# ── the cost arithmetic, and every way of inverting it ───────────────────────────────────────


def test_cost_usd_is_the_exact_sum_of_the_four_buckets(pricer: TokenPricer) -> None:
    priced = pricer.price(response(), on=INTRO_DATE, purpose="t1_review")
    expected = micro(
        (
            Decimal(1000) * rate("sonnet-5-introductory", DEFAULT_MODEL, "input")
            + Decimal(7) * rate("sonnet-5-introductory", DEFAULT_MODEL, "output")
            + Decimal(13) * rate("sonnet-5-introductory", DEFAULT_MODEL, "cache_write")
            + Decimal(29) * rate("sonnet-5-introductory", DEFAULT_MODEL, "cache_read")
        )
        / Decimal(1_000_000)
    )
    assert priced.cost_usd == expected


def test_input_and_output_rates_are_not_swapped(pricer: TokenPricer) -> None:
    """1000 prompt tokens and 7 completion tokens, at $5 in and $25 out.

    Correct: $0.005271. With the two rates swapped: $0.025131 — nearly five times as much. A test
    using symmetric token counts would pass under both, which is why the counts are lopsided.
    """
    priced = pricer.price(response(), on=INTRO_DATE, purpose="t1_review")
    inverted = micro(
        (
            Decimal(1000) * rate("sonnet-5-introductory", DEFAULT_MODEL, "output")
            + Decimal(7) * rate("sonnet-5-introductory", DEFAULT_MODEL, "input")
            + Decimal(13) * rate("sonnet-5-introductory", DEFAULT_MODEL, "cache_write")
            + Decimal(29) * rate("sonnet-5-introductory", DEFAULT_MODEL, "cache_read")
        )
        / Decimal(1_000_000)
    )
    assert priced.cost_usd == Decimal("0.005271")
    assert priced.cost_usd != inverted


def test_cache_write_and_cache_read_rates_are_not_swapped(pricer: TokenPricer) -> None:
    """Cache write is a 1.25x premium and cache read a 0.1x discount; swapping them hides both.

    The burn report exists to answer "is caching working". Reversed, it would answer the opposite
    and still look like a plausible number.
    """
    priced = pricer.price(
        response(
            usage=usage(
                input_tokens=0, output_tokens=0, cache_write_tokens=1000, cache_read_tokens=0
            )
        ),
        on=INTRO_DATE,
        purpose="t1_review",
    )
    write_rate = rate("sonnet-5-introductory", DEFAULT_MODEL, "cache_write")
    read_rate = rate("sonnet-5-introductory", DEFAULT_MODEL, "cache_read")
    assert write_rate > read_rate  # the premium and the discount, not two similar numbers
    assert priced.cost_usd == micro(Decimal(1000) * write_rate / Decimal(1_000_000))
    assert priced.cost_usd != micro(Decimal(1000) * read_rate / Decimal(1_000_000))


def test_rupees_are_dollars_multiplied_by_the_rate_not_divided(pricer: TokenPricer) -> None:
    """`cost_inr = cost_usd * usd_inr`. Inverted, a ₹0.46 call books as ₹0.00006.

    Also asserts the property the module's docstring promises an auditor: dividing the two stored
    columns returns the exact rate that was applied, years later.
    """
    priced = pricer.price(response(), on=INTRO_DATE, purpose="t1_review")
    assert priced.cost_inr == micro(priced.cost_usd * priced.usd_inr)
    assert priced.cost_inr != micro(priced.cost_usd / priced.usd_inr)
    assert priced.cost_inr > priced.cost_usd  # the rupee is worth less than the dollar
    assert priced.cost_inr / priced.cost_usd == priced.usd_inr


def test_rounding_is_half_up_and_happens_before_the_fx_conversion(pricer: TokenPricer) -> None:
    """One cache-read token on Opus 5 costs exactly $0.0000005 — a tie at the stored scale.

    Two decisions are pinned by one figure. Half-up rounds the tie to $0.000001; banker's rounding
    would round it to $0.000000, because zero is even. And `cost_inr` is that *rounded* dollar
    figure times the rate — ₹0.000088. Converting first and rounding after would give ₹0.000044,
    exactly half, and would make the stored rate unrecoverable by division.
    """
    priced = pricer.price(
        response(
            usage=usage(input_tokens=0, output_tokens=0, cache_write_tokens=0, cache_read_tokens=1)
        ),
        on=INTRO_DATE,
        purpose="t1_review",
    )
    exact_usd = rate("sonnet-5-introductory", DEFAULT_MODEL, "cache_read") / Decimal(1_000_000)
    assert exact_usd == Decimal("0.0000005")  # the tie this test is built on

    assert priced.cost_usd == Decimal("0.000001")  # half-up; half-even would give 0.000000
    assert priced.cost_inr == Decimal("0.000088")  # rounded-then-converted
    assert priced.cost_inr != micro(exact_usd * priced.usd_inr)  # converted-then-rounded: 0.000044


def test_prompt_tokens_count_every_billed_prompt_bucket() -> None:
    """`tokens_in` is uncached + cache-written + cache-read, so it is the real prompt volume.

    Dropping either cached bucket is the plausible edit, and with distinct counts it is visible.
    """
    counted = usage()
    assert counted.prompt_tokens == 1000 + 13 + 29
    assert counted.prompt_tokens != counted.input_tokens
    assert counted.total_tokens == counted.prompt_tokens + counted.output_tokens


# ── the dated card ───────────────────────────────────────────────────────────────────────────


def test_the_schedule_in_force_is_the_newest_one_that_has_started(pricer: TokenPricer) -> None:
    """Sonnet 5's introductory rate runs through 2026-08-31; the card says the two must differ.

    A decision made on the last introductory day and one made the next day cannot price the same.
    This fails if `schedule_for` takes the first schedule in force instead of the last, or if the
    boundary comparison flips.
    """
    one_million = usage(
        input_tokens=1_000_000, output_tokens=0, cache_write_tokens=0, cache_read_tokens=0
    )
    call = response(model="claude-sonnet-5", usage=one_million)

    last_intro_day = pricer.price(call, on=date(2026, 8, 31), purpose="t1_review")
    first_list_day = pricer.price(call, on=date(2026, 9, 1), purpose="t1_review")

    assert last_intro_day.schedule_id == "sonnet-5-introductory"
    assert first_list_day.schedule_id == "sonnet-5-list"
    assert last_intro_day.cost_usd == Decimal("2.000000")
    assert first_list_day.cost_usd == Decimal("3.000000")
    assert last_intro_day.cost_usd < first_list_day.cost_usd


def test_a_date_before_the_card_starts_raises_rather_than_borrowing_a_later_rate(
    pricer: TokenPricer, card: PriceCard
) -> None:
    """Pricing with rates that were not yet in force is a wrong number wearing a right costume."""
    before = card.schedules[0].effective_from.replace(day=1)
    with pytest.raises(NoPriceScheduleError, match="no price schedule covers"):
        pricer.price(response(), on=before, purpose="t1_review")


def test_an_unpriced_model_raises_instead_of_booking_zero(pricer: TokenPricer) -> None:
    """The whole point of X3: ₹0 reads as a bargain, not as an unmeasured call."""
    with pytest.raises(UnknownModelError, match="no price for model 'claude-not-on-the-card'"):
        pricer.price(response(model="claude-not-on-the-card"), on=INTRO_DATE, purpose="t1_review")


def test_an_unlabelled_call_is_refused(pricer: TokenPricer) -> None:
    """The burn report groups by purpose; an unattributed row cannot be read."""
    with pytest.raises(ValueError, match="needs a purpose"):
        pricer.price(response(), on=INTRO_DATE, purpose="   ")


def test_schedules_must_be_ordered_and_unique() -> None:
    """Both ways of making "the schedule in force" ambiguous are refused at load.

    Two schedules on the same date leave no answer to which one applies; two sharing an id leave
    `token_usage.schedule_id` unable to say which rates a historical row was priced at.
    """
    original = load_price_card().model_dump()

    same_date = dict(original)
    same_date["schedules"] = [original["schedules"][0], dict(original["schedules"][0])]
    with pytest.raises(ValidationError, match="share an effective_from"):
        PriceCard.model_validate(same_date)

    same_id = dict(original)
    later = dict(original["schedules"][0])
    later["effective_from"] = date(2027, 1, 1)
    same_id["schedules"] = [original["schedules"][0], later]
    with pytest.raises(ValidationError, match="duplicate schedule id"):
        PriceCard.model_validate(same_id)


# ── StubLLM: deterministic, credential-free, always metered ──────────────────────────────────


def test_the_stub_answers_the_same_question_identically_every_time() -> None:
    """Replay determinism (§8.3.3, invariant #11) is the reason the stub is not a mock."""
    question = [Message(role=Role.USER, content="Is the AI robotics thesis broken?")]
    first = StubLLM().complete(question, model=DEFAULT_MODEL)
    second = StubLLM().complete(question, model=DEFAULT_MODEL)

    assert first == second
    assert first.usage == second.usage
    assert first.provider == STUB_PROVIDER


def test_every_stub_response_carries_usage() -> None:
    """Usage has no default anywhere in this package: an unmeasured call is the failure mode."""
    reply = StubLLM().complete([Message(role=Role.USER, content="hello")], model=DEFAULT_MODEL)
    assert isinstance(reply.usage, Usage)
    assert reply.usage.total_tokens > 0


def test_a_registered_reply_is_returned_verbatim_for_its_digest() -> None:
    question = [Message(role=Role.USER, content="Name the break conditions.")]
    digest = prompt_digest(question, model=DEFAULT_MODEL)
    stub = StubLLM({digest: StubReply(text="one, two, three", usage=usage())})

    reply = stub.complete(question, model=DEFAULT_MODEL)

    assert reply.text == "one, two, three"
    assert reply.usage == usage()
    assert stub.calls[0].digest == digest


def test_a_synthesized_reply_can_never_be_mistaken_for_a_model_answer() -> None:
    reply = StubLLM().complete(
        [Message(role=Role.USER, content="unregistered")], model=DEFAULT_MODEL
    )
    assert reply.text.startswith("STUB[")
    assert reply.provider == STUB_PROVIDER


def test_a_strict_stub_refuses_an_unregistered_prompt_loudly() -> None:
    stub = StubLLM(synthesize_unknown=False)
    with pytest.raises(UnknownPromptError, match=r"register one with StubLLM\.register"):
        stub.complete([Message(role=Role.USER, content="unregistered")], model=DEFAULT_MODEL)


def test_the_digest_separates_questions_that_would_be_answered_differently() -> None:
    """Model, system prompt, turns and offered tools all change the answer, so all are hashed."""
    turns = [Message(role=Role.USER, content="Is it broken?")]
    tool = ToolSpec(name="adjusted_close", description="Adjusted closes by ISIN.", input_schema={})
    base = prompt_digest(turns, model=DEFAULT_MODEL)

    assert prompt_digest(turns, model=TRIAGE_MODEL) != base
    assert prompt_digest(turns, model=DEFAULT_MODEL, system="be terse") != base
    assert prompt_digest(turns, model=DEFAULT_MODEL, tools=[tool]) != base
    assert prompt_digest(turns, model=DEFAULT_MODEL) == base  # and is stable


def test_the_stub_needs_no_credential(load_settings: SettingsLoader) -> None:
    """B4's whole point: the analyst is buildable and testable with no key in the environment."""
    settings = load_settings(None)
    assert settings.anthropic_api_key is None
    assert isinstance(build_llm(settings), StubLLM)


# ── AnthropicLLM: refuses to exist unkeyed, and never leaks the key ──────────────────────────


def test_an_unkeyed_client_raises_a_named_actionable_error() -> None:
    with pytest.raises(LLMCredentialError) as raised:
        AnthropicLLM(None)
    message = str(raised.value)
    assert "ANTHROPIC_API_KEY" in message  # names the key to set
    assert "StubLLM" in message  # and the alternative that needs none


@pytest.mark.parametrize("blank", ["", "   "])
def test_a_blank_key_is_treated_as_no_key(blank: str) -> None:
    with pytest.raises(LLMCredentialError):
        AnthropicLLM(blank)


def test_from_settings_raises_when_the_environment_has_no_key(
    load_settings: SettingsLoader, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("LLM_PROVIDER", "anthropic")
    settings = load_settings(None)
    assert settings.llm_provider is LlmProvider.ANTHROPIC
    with pytest.raises(LLMCredentialError):
        AnthropicLLM.from_settings(settings)


def test_selecting_anthropic_without_a_key_raises_and_never_falls_back_to_the_stub(
    load_settings: SettingsLoader, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A silent downgrade would give an operator a system that looks like it is reasoning."""
    monkeypatch.setenv("LLM_PROVIDER", "anthropic")
    settings = load_settings(None)
    with pytest.raises(LLMCredentialError):
        build_llm(settings)


def test_a_configured_client_holds_the_key_as_a_secret_and_never_prints_it(
    load_settings: SettingsLoader, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Invariant #13: a key in a repr reaches a log, and the journal is append-only."""
    monkeypatch.setenv("LLM_PROVIDER", "anthropic")
    monkeypatch.setenv("ANTHROPIC_API_KEY", FAKE_KEY)
    settings = load_settings(None)

    assert isinstance(settings.anthropic_api_key, SecretStr)
    assert FAKE_KEY not in repr(settings.anthropic_api_key)
    assert FAKE_KEY not in str(settings.anthropic_api_key)

    client = AnthropicLLM.from_settings(settings)
    assert isinstance(client, AnthropicLLM)
    assert FAKE_KEY not in repr(client)


def test_no_error_this_module_raises_carries_the_key(
    stub_transport: _Transport,
) -> None:
    """A key interpolated into an exception message ends up wherever that message is logged."""
    client = AnthropicLLM(FAKE_KEY, http_client=stub_transport.client("refusal.json"))
    with pytest.raises(LLMRefusalError) as raised:
        client.complete([Message(role=Role.USER, content="hello")], model=DEFAULT_MODEL)
    assert FAKE_KEY not in str(raised.value)
    assert FAKE_KEY not in repr(raised.value)


# ── AnthropicLLM against frozen fixtures, over a transport that cannot reach a socket ────────


class _Transport:
    """Serves a frozen fixture and keeps the request the client built, for assertions."""

    def __init__(self) -> None:
        self.requests: list[httpx.Request] = []

    def client(self, fixture: str, *, status: int = 200) -> httpx.Client:
        payload = json.loads((FIXTURES / fixture).read_text(encoding="utf-8"))

        def handler(request: httpx.Request) -> httpx.Response:
            self.requests.append(request)
            return httpx.Response(status, json=payload)

        return httpx.Client(transport=httpx.MockTransport(handler))

    @property
    def body(self) -> dict[str, Any]:
        return cast(dict[str, Any], json.loads(self.requests[-1].content))


@pytest.fixture
def stub_transport() -> _Transport:
    return _Transport()


def test_a_completed_call_maps_every_token_bucket(stub_transport: _Transport) -> None:
    """All four buckets survive the boundary; collapsing them would hide the cache discount."""
    client = AnthropicLLM(FAKE_KEY, http_client=stub_transport.client("end_turn.json"))

    reply = client.complete(
        [Message(role=Role.USER, content="Is the driver intact?")], model=DEFAULT_MODEL
    )

    assert reply.provider == ANTHROPIC_PROVIDER
    assert reply.model == DEFAULT_MODEL
    assert reply.stop_reason is StopReason.END_TURN
    assert reply.truncated is False
    assert reply.usage == Usage(
        input_tokens=1201, output_tokens=317, cache_write_tokens=4096, cache_read_tokens=8192
    )


def test_adaptive_thinking_is_sent_only_to_a_model_that_has_it(stub_transport: _Transport) -> None:
    """Haiku 4.5 — `TRIAGE_MODEL` — rejects `thinking={"type": "adaptive"}` with a 400.

    The parameter is therefore chosen from the model rather than sent unconditionally, and this
    is the only place that choice can be checked while no credential exists.
    """
    turns = [Message(role=Role.USER, content="Classify this filing.")]

    AnthropicLLM(FAKE_KEY, http_client=stub_transport.client("end_turn.json")).complete(
        turns, model=DEFAULT_MODEL
    )
    assert stub_transport.body["thinking"] == {"type": "adaptive"}

    AnthropicLLM(FAKE_KEY, http_client=stub_transport.client("end_turn.json")).complete(
        turns, model=TRIAGE_MODEL
    )
    assert "thinking" not in stub_transport.body


def test_thinking_is_disabled_explicitly_rather_than_omitted(stub_transport: _Transport) -> None:
    """On the current Opus and Sonnet models, omitting `thinking` means adaptive is *on*.

    So the off switch has to say `disabled` out loud; omitting the parameter would make
    `adaptive_thinking=False` a no-op that reads like a setting.
    """
    client = AnthropicLLM(
        FAKE_KEY, adaptive_thinking=False, http_client=stub_transport.client("end_turn.json")
    )
    client.complete([Message(role=Role.USER, content="hello")], model=DEFAULT_MODEL)
    assert stub_transport.body["thinking"] == {"type": "disabled"}


def test_absent_optional_parameters_are_omitted_from_the_wire(stub_transport: _Transport) -> None:
    """`system` and `tools` must be absent, not null — the API rejects a null where one is due."""
    client = AnthropicLLM(FAKE_KEY, http_client=stub_transport.client("end_turn.json"))
    client.complete([Message(role=Role.USER, content="hello")], model=TRIAGE_MODEL)

    body = stub_transport.body
    assert "system" not in body
    assert "tools" not in body
    assert body["messages"] == [{"role": "user", "content": "hello"}]


def test_a_system_prompt_and_tools_reach_the_wire_when_given(stub_transport: _Transport) -> None:
    client = AnthropicLLM(FAKE_KEY, http_client=stub_transport.client("tool_use.json"))
    tool = ToolSpec(
        name="adjusted_close",
        description="Adjusted closes for an ISIN over a date range.",
        input_schema={"type": "object", "properties": {"isin": {"type": "string"}}},
    )

    reply = client.complete(
        [Message(role=Role.USER, content="Is it broken?")],
        model=DEFAULT_MODEL,
        tools=[tool],
        system="You are the T1 reviewer.",
    )

    body = stub_transport.body
    assert body["system"] == "You are the T1 reviewer."
    assert body["tools"][0]["name"] == "adjusted_close"
    assert reply.stop_reason is StopReason.TOOL_USE
    assert reply.tool_calls[0].name == "adjusted_close"
    assert reply.tool_calls[0].arguments["isin"] == "INE009A01021"


def test_the_model_that_answered_is_reported_not_the_one_that_was_asked_for(
    stub_transport: _Transport, pricer: TokenPricer
) -> None:
    """A provider may resolve an alias to a dated snapshot, and that is what gets priced.

    Two consequences, both asserted here. The response carries the id the provider used, not the
    alias the caller sent — otherwise a burn report attributes spend to a model that never ran.
    And because the card lists aliases, a resolved id it does not list raises at pricing time
    rather than booking ₹0: loud, which is the designed behaviour (`model_prices.yaml`'s `scope`),
    and the operator's fix is to add the id to the card.
    """
    client = AnthropicLLM(FAKE_KEY, http_client=stub_transport.client("resolved_model_id.json"))

    reply = client.complete([Message(role=Role.USER, content="Classify.")], model=TRIAGE_MODEL)

    assert reply.model == "claude-haiku-4-5-20251001"
    assert reply.model != TRIAGE_MODEL
    with pytest.raises(UnknownModelError, match=r"Add the model to accounting/model_prices\.yaml"):
        pricer.price(reply, on=INTRO_DATE, purpose="theme_mapping")


def test_a_truncated_answer_is_visible_and_still_billed(stub_transport: _Transport) -> None:
    """A cut-off answer is half an argument; silently returning it is the failure to prevent."""
    client = AnthropicLLM(FAKE_KEY, http_client=stub_transport.client("max_tokens.json"))
    reply = client.complete([Message(role=Role.USER, content="List them.")], model=DEFAULT_MODEL)

    assert reply.stop_reason is StopReason.MAX_TOKENS
    assert reply.truncated is True
    assert reply.usage.output_tokens == 16000


def test_a_refusal_raises_rather_than_returning_a_plausible_nothing(
    stub_transport: _Transport,
) -> None:
    """A refusal arrives as a 200 with empty content; `content[0]` would be a silent no-answer."""
    client = AnthropicLLM(FAKE_KEY, http_client=stub_transport.client("refusal.json"))
    with pytest.raises(LLMRefusalError, match="declined the request"):
        client.complete([Message(role=Role.USER, content="hello")], model=DEFAULT_MODEL)


def test_an_unmapped_stop_reason_fails_loud(stub_transport: _Transport) -> None:
    """`model_context_window_exceeded` is a real provider stop reason this client does not map.

    Treating an unknown termination as a normal end of turn would accept a partial answer as a
    complete one, so it raises until someone establishes what it should mean here.
    """
    client = AnthropicLLM(
        FAKE_KEY, http_client=stub_transport.client("context_window_exceeded.json")
    )
    with pytest.raises(LLMError, match="unknown stop_reason"):
        client.complete([Message(role=Role.USER, content="hello")], model=DEFAULT_MODEL)


@pytest.mark.parametrize(
    ("kwargs", "match"),
    [
        ({"messages": [], "model": DEFAULT_MODEL}, "at least one message"),
        (
            {"messages": None, "model": DEFAULT_MODEL, "max_tokens": 0},
            "max_tokens must be positive",
        ),
    ],
)
def test_a_malformed_request_is_refused_before_a_call_is_made(
    stub_transport: _Transport, kwargs: dict[str, Any], match: str
) -> None:
    """Argument validation happens client-side, so a bad call costs nothing and names itself."""
    client = AnthropicLLM(FAKE_KEY, http_client=stub_transport.client("end_turn.json"))
    call = dict(kwargs)
    if call["messages"] is None:
        call["messages"] = [Message(role=Role.USER, content="hello")]
    with pytest.raises(ValueError, match=match):
        client.complete(**call)
    assert stub_transport.requests == []


# ── the ledger ───────────────────────────────────────────────────────────────────────────────


def test_the_ledger_writes_one_row_per_call_with_the_priced_figures(
    pricer: TokenPricer, ledger: TokenLedger, conn: RecordingConnection
) -> None:
    priced = pricer.price(response(), on=INTRO_DATE, purpose="t1_review")

    usage_id = ledger.record(priced, ts=CALLED_AT, case_id=CASE_ID)

    assert usage_id == 1
    written = conn.params_by_column(WRITE_COLUMNS)
    assert written["ts"] == CALLED_AT  # when the call happened, from the caller
    assert written["recorded_at"] == RECORDED_AT  # when the row landed, from the injected clock
    assert written["ts"] != written["recorded_at"]  # two columns, two instants, never one value
    assert written["provider"] == STUB_PROVIDER
    assert written["model"] == DEFAULT_MODEL
    assert written["purpose"] == "t1_review"
    assert written["case_id"] == CASE_ID
    assert written["decision_journal_id"] is None  # the entry does not exist yet
    assert written["tokens_in"] == priced.usage.prompt_tokens
    assert written["tokens_out"] == priced.usage.output_tokens
    assert written["cached_tokens"] == priced.usage.cache_read_tokens
    assert written["cost_inr"] == priced.cost_inr
    assert written["cost_usd"] == priced.cost_usd


def test_the_ledger_defaults_its_timestamp_to_the_injected_clock(
    pricer: TokenPricer, ledger: TokenLedger, conn: RecordingConnection
) -> None:
    """B10: no module reads the wall clock, so an omitted `ts` comes from the clock it was given."""
    priced = pricer.price(response(), on=INTRO_DATE, purpose="t1_review")
    ledger.record(priced)
    written = conn.params_by_column(WRITE_COLUMNS)
    assert written["ts"] == RECORDED_AT
    assert written["recorded_at"] == RECORDED_AT  # a live call: both instants are "now"


def test_a_replayed_row_keeps_the_old_call_time_and_takes_a_new_recorded_at(
    pricer: TokenPricer, ledger: TokenLedger, conn: RecordingConnection
) -> None:
    """`recorded_at` is the ledger's clock, never an echo of the caller's `ts`.

    This is the whole reason there are two timestamp columns: a replay re-writes a call that
    happened days ago, so its `ts` is old while its `recorded_at` is now, and that difference is
    what distinguishes a replayed ledger from the original run. Reading `recorded_at` off the
    caller's `ts` instead would make every replayed row claim to have been written when the call
    was made — indistinguishable from the original, and the audit trail is gone.
    """
    priced = pricer.price(response(), on=INTRO_DATE, purpose="t1_review")
    long_ago = datetime(2026, 7, 1, 9, 15, tzinfo=IST)

    ledger.record(priced, ts=long_ago)

    written = conn.params_by_column(WRITE_COLUMNS)
    assert written["ts"] == long_ago  # the call's own instant, preserved
    assert written["recorded_at"] == RECORDED_AT  # the clock's instant, not the caller's
    assert written["recorded_at"] > written["ts"]


def test_a_naive_timestamp_is_refused(pricer: TokenPricer, ledger: TokenLedger) -> None:
    priced = pricer.price(response(), on=INTRO_DATE, purpose="t1_review")
    with pytest.raises(ValueError, match="ts must be tz-aware"):
        ledger.record(priced, ts=datetime(2026, 8, 7, 19, 30))


def test_attaching_a_decision_is_write_once(
    pricer: TokenPricer, ledger: TokenLedger, conn: RecordingConnection
) -> None:
    """One spend belongs to one decision; a silent re-point would break burn attribution.

    The guard lives in the UPDATE's `decision_journal_id IS NULL`, so a second attempt matches no
    row — which is what an empty result from the fake connection stands for here.
    """
    priced = pricer.price(response(), on=INTRO_DATE, purpose="t1_review")
    usage_id = ledger.record(priced, ts=CALLED_AT)

    conn.rows = [(usage_id,)]  # the UPDATE matched: still unattached
    ledger.attach_decision(usage_id, 42)
    sql, params = conn.last
    assert "decision_journal_id IS NULL" in sql
    assert params == [42, usage_id]

    conn.rows = []  # the UPDATE matched nothing: already attached, or gone
    with pytest.raises(UnknownUsageError, match="already attached"):
        ledger.attach_decision(usage_id, 43)


def test_the_ledger_repr_names_no_state() -> None:
    """It holds a live connection whose DSN is a credential; its repr must stay boring."""
    assert repr(TokenLedger(cast(Connection, RecordingConnection()))) == "TokenLedger()"


# ── end to end: one figure, two records ──────────────────────────────────────────────────────


def test_a_metered_call_puts_the_same_cost_in_the_ledger_and_the_journal_line(
    pricer: TokenPricer, ledger: TokenLedger, conn: RecordingConnection, clock: FrozenClock
) -> None:
    """M5.4 acceptance 3: the journal line's token fields come from the call that was made.

    Both records are built from one `PricedUsage`, so this asserts they are the same number — not
    two numbers that happen to agree today.

    The stub's reply is registered with an explicit `Usage` whose four buckets are all different
    and all non-zero. That is load-bearing rather than tidy: the stub's own estimate leaves both
    cache buckets at zero, which makes `prompt_tokens` and `input_tokens` accidentally equal, and
    a `tokens_in` that had quietly dropped the cached buckets would pass unnoticed.
    """
    question = [Message(role=Role.USER, content="Is the AI robotics thesis broken?")]
    digest = prompt_digest(question, model=DEFAULT_MODEL)
    stub = StubLLM({digest: StubReply(text="Intact — inflow grew again.", usage=usage())})
    metered = MeteredLLM(stub, pricer=pricer, ledger=ledger, clock=clock)

    completed = metered.complete(
        question,
        model=DEFAULT_MODEL,
        purpose="t1_review",
        case_id=CASE_ID,
        on=INTRO_DATE,
    )
    spent = completed.response.usage
    assert spent.prompt_tokens > spent.input_tokens > 0  # the cached buckets are really there

    entry = JournalEntry(
        ts=clock.now(),
        trading_date=TRADING_DATE,
        case_id=CASE_ID,
        actor=Actor.T1,
        decision=Decision.HOLD,
        rationale="The driver is intact; no break condition fired.",
        **completed.journal_fields(),
    )

    written = conn.params_by_column(WRITE_COLUMNS)
    assert entry.model == DEFAULT_MODEL == written["model"]
    assert entry.tokens == TokenSpend(
        tokens_in=spent.prompt_tokens,
        tokens_out=spent.output_tokens,
        cost_inr=completed.priced.cost_inr,
    )
    assert entry.tokens is not None
    # Spelled out as well as compared, so the assertion above cannot be satisfied by a
    # `TokenSpend` that is wrong in the same way on both sides.
    assert entry.tokens.tokens_in == 1000 + 13 + 29
    assert entry.tokens.tokens_out == 7
    assert entry.tokens.cost_inr == written["cost_inr"] == completed.priced.cost_inr
    assert written["tokens_in"] == entry.tokens.tokens_in  # the row and the line agree
    assert isinstance(entry.tokens.cost_inr, Decimal)
    assert completed.usage_id == 1
    assert completed.text == completed.response.text


def test_a_stub_call_is_priced_by_the_same_card_as_a_real_one(pricer: TokenPricer) -> None:
    """Invariant #5 for money: paper spend is a forecast of live spend, not a column of zeros.

    `provider` is what separates them afterwards, which is why it travels on the response.
    """
    stub_call = response(provider=STUB_PROVIDER)
    real_call = response(provider=ANTHROPIC_PROVIDER)

    stub_priced = pricer.price(stub_call, on=INTRO_DATE, purpose="t1_review")
    real_priced = pricer.price(real_call, on=INTRO_DATE, purpose="t1_review")

    assert stub_priced.cost_inr == real_priced.cost_inr > 0
    assert stub_priced.provider != real_priced.provider


def test_a_meter_without_a_ledger_still_prices_the_call(
    pricer: TokenPricer, clock: FrozenClock
) -> None:
    """A dry run measures cost without writing it: `usage_id` is None, and nothing is lost."""
    metered = MeteredLLM(StubLLM(), pricer=pricer, clock=clock)
    completed = metered.complete(
        [Message(role=Role.USER, content="hello")],
        model=DEFAULT_MODEL,
        purpose="t1_review",
        on=INTRO_DATE,
    )
    assert completed.usage_id is None
    assert completed.priced.cost_inr > 0
    assert metered.ledger is None


def test_the_metered_client_returns_a_completion_not_a_response(
    pricer: TokenPricer, clock: FrozenClock
) -> None:
    """Deliberate: `complete` hands back a `MeteredCompletion`, so a caller cannot drop the cost.

    If `MeteredLLM` satisfied the `LLM` protocol, a module could take the metered client, use it
    as an ordinary one, and the spend would never reach the journal. The static half of that — a
    `MeteredLLM` is not assignable where an `LLM` is wanted, because the return types differ — is
    enforced by `mypy --strict` rather than here; what this pins is that the two types stay
    distinct, with the `LLMResponse` reachable only through `.response`.
    """
    metered = MeteredLLM(StubLLM(), pricer=pricer, clock=clock)
    completed = metered.complete(
        [Message(role=Role.USER, content="hello")],
        model=DEFAULT_MODEL,
        purpose="t1_review",
        on=INTRO_DATE,
    )
    assert type(completed) is MeteredCompletion
    assert isinstance(completed.response, LLMResponse)
