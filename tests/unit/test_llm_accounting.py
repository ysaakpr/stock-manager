"""M5.4: the LLM boundary is substitutable, credential-safe, and every call is priced.

X3 makes two promises and this file holds them to both. The first is B4's: the analyst must be
buildable, testable and replayable with no Anthropic key, so `StubLLM` is a real member of the
`LLM` protocol that answers deterministically and the live `AnthropicLLM` refuses to exist until a
credential is configured — it never silently degrades to the stub, because a system that looks like
it is reasoning and is not is worse than one that stops. The second is decision #12's: a model
choice is cost-blind but never cost-*unknown*, so every call is priced on a dated card and lands
both as a `token_usage` row and in the journal line for the decision it bought, from one figure, so
the two cannot disagree. A model the card does not list is a loud failure, never a silent ₹0.

The three acceptance criteria map to the three section headers below. Everything is offline
(CLAUDE.md, B8): the stub needs no network, the live client is never actually called (its
construction is what is tested), and the ledger writes through a recording connection that echoes
an `INSERT ... RETURNING` back the way Postgres would — the same stand-in `test_journal.py` uses.
"""

from __future__ import annotations

import re
from collections.abc import Sequence
from datetime import date, datetime
from decimal import Decimal
from pathlib import Path
from typing import Any, cast

import pytest
from pydantic import SecretStr, ValidationError

from accounting import (
    MeteredCompletion,
    MeteredLLM,
    NoPriceScheduleError,
    PriceCard,
    TokenLedger,
    TokenPricer,
    UnknownModelError,
    load_price_card,
)
from accounting.tokens import PRICES_PATH
from analyst.journal import Actor, Decision, JournalEntry, Sleeve, TokenSpend
from analyst.llm import (
    DEFAULT_MODEL,
    LLM,
    STUB_PROVIDER,
    AnthropicLLM,
    LLMCredentialError,
    Message,
    Role,
    StubLLM,
    StubReply,
    UnknownPromptError,
    Usage,
    build_llm,
    prompt_digest,
)
from dataplatform.clock import IST, FrozenClock
from dataplatform.config import LlmProvider, Settings
from dataplatform.store.db import Connection

REPO_ROOT = Path(__file__).resolve().parents[2]

TRADING_DATE = date(2026, 9, 2)
DECIDED_AT = datetime(2026, 9, 2, 19, 30, tzinfo=IST)
RECORDED_AT = datetime(2026, 9, 2, 19, 30, 5, tzinfo=IST)
CASE_ID = "AI_ROBOTICS"
ISIN = "INE009A01021"


def a_question(text: str = "Is the thesis on this driver still intact?") -> list[Message]:
    return [Message(role=Role.USER, content=text)]


# ── the recording connection (the DB stand-in, as in test_journal.py) ──────────────────────────


class FakeCursor:
    """The one cursor method `TokenLedger` uses."""

    def __init__(self, rows: Sequence[tuple[Any, ...]]) -> None:
        self._rows = list(rows)

    def fetchone(self) -> tuple[Any, ...] | None:
        return self._rows[0] if self._rows else None


class RecordingConnection:
    """Records every statement and echoes an insert back as its returned row.

    Faithful in the way that matters: `INSERT ... RETURNING id` hands back the row the server
    built, so returning `(next_id, *params)` is what inserting those params yields. Non-inserts
    return whatever `rows` is set to, which is how the write-once `attach_decision` UPDATE is made
    to succeed (a row) or fail (no row).
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

    def insert_params(self, table: str) -> list[Any]:
        """Parameters of the one INSERT into `table`, for asserting what a row was written with."""
        inserts = [
            p
            for sql, p in self.calls
            if sql.lstrip().upper().startswith(f"INSERT INTO {table.upper()}")
        ]
        assert len(inserts) == 1, f"expected exactly one INSERT INTO {table}, saw {len(inserts)}"
        return inserts[0]


@pytest.fixture
def conn() -> RecordingConnection:
    return RecordingConnection()


@pytest.fixture
def clock() -> FrozenClock:
    return FrozenClock(RECORDED_AT)


@pytest.fixture(scope="module")
def card() -> PriceCard:
    return load_price_card()


@pytest.fixture
def clean_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """Remove every `Settings` key from the environment so a stray export cannot decide a test.

    A developer's exported `ANTHROPIC_API_KEY` would otherwise make the unconfigured-provider test
    observe a key that this suite must prove is absent.
    """
    for field in Settings.model_fields:
        monkeypatch.delenv(field.upper(), raising=False)
        monkeypatch.delenv(field, raising=False)


# ═══ acceptance 1: StubLLM is deterministic, needs no credential, always reports usage ══════════


def test_the_stub_needs_no_credential_and_answers() -> None:
    """B4: this is the implementation the whole analyst is built against with no key present."""
    response = StubLLM().complete(a_question(), model=DEFAULT_MODEL)

    assert response.text
    assert response.provider == STUB_PROVIDER
    assert response.model == DEFAULT_MODEL


def test_the_stub_is_a_member_of_the_llm_protocol() -> None:
    """Substitutable for the real client — the property invariant #5 rests on."""
    assert isinstance(StubLLM(), LLM)


def test_every_stub_response_carries_usage() -> None:
    """Usage is not optional: a call whose cost is unknowable is exactly what X3 forbids."""
    response = StubLLM().complete(a_question(), model=DEFAULT_MODEL)

    assert isinstance(response.usage, Usage)
    assert response.usage.total_tokens > 0
    assert response.usage.output_tokens > 0


def test_the_same_question_gets_a_byte_identical_answer_across_instances() -> None:
    """Determinism across processes is what makes a replayed agent run reproducible (§8.3.3)."""
    first = StubLLM().complete(a_question(), model=DEFAULT_MODEL)
    second = StubLLM().complete(a_question(), model=DEFAULT_MODEL)

    assert first == second
    assert first.text == second.text
    assert first.usage == second.usage


def test_a_registered_reply_is_returned_exactly() -> None:
    """A test that cares about the content of an answer pins it by prompt digest."""
    digest = prompt_digest(a_question(), model=DEFAULT_MODEL)
    stub = StubLLM(
        {digest: StubReply(text="the thesis holds", usage=Usage(input_tokens=40, output_tokens=6))}
    )

    response = stub.complete(a_question(), model=DEFAULT_MODEL)

    assert response.text == "the thesis holds"
    assert response.usage == Usage(input_tokens=40, output_tokens=6)


def test_a_different_question_gets_a_different_answer() -> None:
    """Different prompts must not collapse to one canned reply; the digest is the key."""
    stub = StubLLM()
    one = stub.complete(a_question("Has break condition BC1 been met?"), model=DEFAULT_MODEL)
    two = stub.complete(a_question("Has break condition BC2 been met?"), model=DEFAULT_MODEL)

    assert one.text != two.text


def test_a_strict_stub_refuses_an_unregistered_prompt() -> None:
    """A test may demand that only prompts it anticipated are asked; the digest names the fix."""
    stub = StubLLM(synthesize_unknown=False)

    with pytest.raises(UnknownPromptError, match="no canned reply"):
        stub.complete(a_question(), model=DEFAULT_MODEL)


def test_the_stub_records_the_calls_it_answered() -> None:
    """What a test asserts the agent actually asked, in order."""
    stub = StubLLM()
    stub.complete(a_question("first"), model=DEFAULT_MODEL)
    stub.complete(a_question("second"), model=DEFAULT_MODEL)

    assert [call.model for call in stub.calls] == [DEFAULT_MODEL, DEFAULT_MODEL]
    assert stub.calls[0].digest != stub.calls[1].digest


# ═══ acceptance 2: an unconfigured AnthropicLLM raises a named, actionable error ════════════════


def test_constructing_the_live_client_without_a_key_raises_a_named_error() -> None:
    """The refusal is the whole point of the file: no key, no client, no silent fallback."""
    with pytest.raises(LLMCredentialError) as exc:
        AnthropicLLM(api_key=None)

    message = str(exc.value)
    assert "ANTHROPIC_API_KEY" in message  # actionable: it names the key to set
    assert "StubLLM" in message  # and the credential-free alternative


def test_a_blank_key_is_not_a_key() -> None:
    """`ANTHROPIC_API_KEY=` in a .env means unconfigured, not a credential of the empty string."""
    with pytest.raises(LLMCredentialError):
        AnthropicLLM(api_key="   ")


def test_the_live_client_never_resolves_an_ambient_credential(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A stray `ANTHROPIC_API_KEY` on a developer machine must not make a paper run spend money."""
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-stray-profile-key")

    with pytest.raises(LLMCredentialError):
        AnthropicLLM(api_key=None)


@pytest.mark.usefixtures("clean_env")
def test_build_llm_refuses_anthropic_without_a_key_rather_than_downgrading() -> None:
    """`build_llm` picks the provider but never returns a stub where anthropic was asked."""
    settings = Settings(llm_provider=LlmProvider.ANTHROPIC, anthropic_api_key=None, _env_file=None)  # type: ignore[call-arg]

    with pytest.raises(LLMCredentialError):
        build_llm(settings)


@pytest.mark.usefixtures("clean_env")
def test_build_llm_returns_the_stub_for_the_stub_provider() -> None:
    """The default path (B4): no credential needed, and it is genuinely the stub."""
    settings = Settings(llm_provider=LlmProvider.STUB, _env_file=None)  # type: ignore[call-arg]

    assert isinstance(build_llm(settings), StubLLM)


@pytest.mark.usefixtures("clean_env")
def test_build_llm_returns_the_live_client_once_a_key_is_present() -> None:
    """The day a key lands, nothing above the interface changes — the same call now returns it."""
    settings = Settings(
        llm_provider=LlmProvider.ANTHROPIC,
        anthropic_api_key=SecretStr("sk-ant-test-not-a-real-key"),
        _env_file=None,  # type: ignore[call-arg]
    )

    built = build_llm(settings)

    assert isinstance(built, AnthropicLLM)
    assert isinstance(built, LLM)


# ═══ the dated cost table: unknown model → loud error, never a silent ₹0 ════════════════════════


def test_the_checked_in_card_loads_and_prices_the_default_model(card: PriceCard) -> None:
    """The card the analyst actually ships must cover the model it calls by default."""
    schedule = card.schedule_for(TRADING_DATE)

    assert schedule.price_for(DEFAULT_MODEL).input > 0


def test_an_unknown_model_raises_rather_than_booking_zero(card: PriceCard) -> None:
    """The ₹0 this module exists to prevent: an unpriced call reads as cheap, not as unmeasured."""
    pricer = TokenPricer(card)
    response = StubLLM().complete(a_question(), model="claude-does-not-exist")

    with pytest.raises(UnknownModelError, match="no price for model"):
        pricer.price(response, on=TRADING_DATE, purpose="t1_review")


def test_a_date_before_the_card_begins_raises(card: PriceCard) -> None:
    """Pricing a call at rates not yet in force is a wrong number in a right one's costume."""
    pricer = TokenPricer(card)
    response = StubLLM().complete(a_question(), model=DEFAULT_MODEL)

    with pytest.raises(NoPriceScheduleError, match="no price schedule covers"):
        pricer.price(response, on=date(2020, 1, 1), purpose="t1_review")


def test_cost_is_decimal_and_matches_the_rate_card_exactly(card: PriceCard) -> None:
    """Money is Decimal end to end; cost_inr is cost_usd * usd_inr on the rounded dollar figure."""
    pricer = TokenPricer(card)
    reply = StubReply(text="…", usage=Usage(input_tokens=1000, output_tokens=500))
    response = StubLLM({prompt_digest(a_question(), model=DEFAULT_MODEL): reply}).complete(
        a_question(), model=DEFAULT_MODEL
    )

    priced = pricer.price(response, on=date(2026, 9, 2), purpose="t1_review")

    # claude-opus-5 on the 2026-09-01 schedule: $5/$25 per MTok, usd_inr 88.00.
    assert isinstance(priced.cost_usd, Decimal)
    assert isinstance(priced.cost_inr, Decimal)
    assert priced.cost_usd == Decimal("0.017500")  # (1000*5 + 500*25) / 1e6
    assert priced.cost_inr == Decimal("1.540000")  # 0.0175 * 88.00
    # The applied rate is recoverable by division, which is the reason both columns are stored.
    assert priced.cost_inr / priced.cost_usd == priced.usd_inr


def test_the_same_model_prices_differently_across_the_dated_boundary(card: PriceCard) -> None:
    """A dated card is the point: a decision on 2026-08-31 must not price like one on 2026-09-01."""
    pricer = TokenPricer(card)
    reply = StubReply(text="…", usage=Usage(input_tokens=1000, output_tokens=500))
    response = StubLLM({prompt_digest(a_question(), model="claude-sonnet-5"): reply}).complete(
        a_question(), model="claude-sonnet-5"
    )

    introductory = pricer.price(response, on=date(2026, 8, 31), purpose="t1_review")
    list_price = pricer.price(response, on=date(2026, 9, 1), purpose="t1_review")

    assert introductory.cost_inr == Decimal("0.616000")  # $2/$10 introductory
    assert list_price.cost_inr == Decimal("0.924000")  # $3/$15 list
    assert list_price.cost_inr > introductory.cost_inr


def test_the_cache_buckets_are_priced_separately_not_collapsed(card: PriceCard) -> None:
    """Folding cache reads into plain input would hide the discount the burn report exists for."""
    pricer = TokenPricer(card)
    usage = Usage(input_tokens=100, output_tokens=50, cache_write_tokens=200, cache_read_tokens=400)
    reply = StubReply(text="…", usage=usage)
    response = StubLLM({prompt_digest(a_question(), model=DEFAULT_MODEL): reply}).complete(
        a_question(), model=DEFAULT_MODEL
    )

    priced = pricer.price(response, on=date(2026, 9, 2), purpose="t1_review")

    # opus-5: input 5, output 25, cache_write 6.25, cache_read 0.50 per MTok.
    assert priced.cost_usd == Decimal("0.003200")  # (500 + 1250 + 1250 + 200) / 1e6
    # Every prompt token billed — uncached, written and read — is what token_spend reports as in.
    assert priced.token_spend.tokens_in == 700


def test_a_purpose_is_required_on_every_priced_call(card: PriceCard) -> None:
    """The burn report is per-purpose; an unlabelled row cannot be attributed."""
    pricer = TokenPricer(card)
    response = StubLLM().complete(a_question(), model=DEFAULT_MODEL)

    with pytest.raises(ValueError, match="needs a purpose"):
        pricer.price(response, on=TRADING_DATE, purpose="   ")


def test_the_price_card_quotes_every_number() -> None:
    """A bare 0.10 in the YAML would parse as a float, and a float in the cost model is a bug."""
    rate_line = re.compile(r"^\s*(input|output|cache_write|cache_read|usd_inr):\s*(\S.*)$")
    offenders: list[str] = []
    for line in PRICES_PATH.read_text(encoding="utf-8").splitlines():
        match = rate_line.match(line)
        if match:
            value = match.group(2).strip()
            if not (value.startswith('"') and value.endswith('"')):
                offenders.append(line.strip())

    assert not offenders, f"unquoted numeric rates in model_prices.yaml: {offenders}"


def test_the_card_rejects_a_float_rate() -> None:
    """The loader refuses a float on the way in, not after it has already lost its last digits."""
    with pytest.raises(ValidationError, match="quoted strings"):
        PriceCard.model_validate(
            {
                "version": 1,
                "card": {
                    "provider": "anthropic",
                    "currency": "INR",
                    "basis": "usd_per_million_tokens",
                    "scope": "test",
                },
                "schedules": [
                    {
                        "id": "s1",
                        "effective_from": "2026-01-01",
                        "label": "test",
                        "provenance": "reconstructed",
                        "figures_current_as_of": "2026-01-01",
                        "sources": ["test"],
                        "notes": "test",
                        "usd_inr": "88.00",
                        "models": {
                            "m": {
                                "input": 5.0,  # a float, not a quoted string
                                "output": "25.00",
                                "cache_write": "6.25",
                                "cache_read": "0.50",
                            }
                        },
                    }
                ],
            }
        )


# ═══ acceptance 3: the decision's journal line carries the token/cost of the actual call ════════


@pytest.fixture
def metered(conn: RecordingConnection, clock: FrozenClock, card: PriceCard) -> MeteredLLM:
    """A stub wrapped so every call is priced on the real card and recorded through the ledger."""
    reply = StubReply(text="BC1 is met", usage=Usage(input_tokens=1000, output_tokens=500))
    inner = StubLLM({prompt_digest(a_question(), model=DEFAULT_MODEL): reply})
    ledger = TokenLedger(cast(Connection, conn), clock=clock)
    return MeteredLLM(inner, pricer=TokenPricer(card), ledger=ledger, clock=clock)


def test_a_metered_call_writes_a_token_usage_row_and_returns_its_cost(
    metered: MeteredLLM, conn: RecordingConnection
) -> None:
    """One call in, one priced row out: the burn report's raw material (§5.7)."""
    completion = metered.complete(
        a_question(), model=DEFAULT_MODEL, purpose="t1_review", case_id=CASE_ID
    )

    assert isinstance(completion, MeteredCompletion)
    assert completion.usage_id == 1  # the ledger's RETURNING id
    assert completion.text == "BC1 is met"
    assert completion.priced.cost_inr == Decimal("1.540000")

    params = conn.insert_params("token_usage")
    # _WRITE_COLUMNS order: ts, provider, model, purpose, case_id, decision_journal_id,
    #                       tokens_in, tokens_out, cached_tokens, cost_inr, cost_usd, recorded_at
    assert params[1] == STUB_PROVIDER
    assert params[2] == DEFAULT_MODEL
    assert params[3] == "t1_review"
    assert params[4] == CASE_ID
    assert params[5] is None  # decision id is not known until the entry is written
    assert params[6] == 1000  # tokens_in
    assert params[7] == 500  # tokens_out
    assert params[9] == Decimal("1.540000")  # cost_inr


def test_the_journal_line_and_the_token_usage_row_carry_the_same_figure(
    metered: MeteredLLM, conn: RecordingConnection
) -> None:
    """Acceptance 3. Both records come from one priced figure, so they cannot disagree."""
    completion = metered.complete(
        a_question(), model=DEFAULT_MODEL, purpose="t1_review", case_id=CASE_ID
    )

    entry = JournalEntry(
        ts=DECIDED_AT,
        trading_date=TRADING_DATE,
        case_id=CASE_ID,
        actor=Actor.T1,
        decision=Decision.SELL,
        isin=ISIN,
        sleeve=Sleeve.CORE,
        rationale="BC1 is met; reducing the position",
        **completion.journal_fields(),
    )

    assert entry.model == DEFAULT_MODEL
    assert entry.tokens is not None
    assert entry.tokens.tokens_in == 1000
    assert entry.tokens.tokens_out == 500
    assert entry.tokens.cost_inr == Decimal("1.540000")

    # The line and the ledger row are written from the same PricedUsage; equal by construction.
    row_cost_inr = conn.insert_params("token_usage")[9]
    assert entry.tokens.cost_inr == row_cost_inr


def test_journal_fields_populate_the_model_and_tokens_from_the_actual_call(
    metered: MeteredLLM,
) -> None:
    """Not a hardcoded shape: the fields are exactly what this call's usage priced to."""
    completion = metered.complete(a_question(), model=DEFAULT_MODEL, purpose="t1_review")

    fields = completion.journal_fields()

    assert fields["model"] == DEFAULT_MODEL
    assert isinstance(fields["tokens"], TokenSpend)
    assert fields["tokens"] == completion.priced.token_spend


def test_a_recorded_call_can_be_attached_to_the_decision_it_informed(
    metered: MeteredLLM, conn: RecordingConnection
) -> None:
    """The entry id does not exist until the decision is written, so attachment is a second step."""
    completion = metered.complete(a_question(), model=DEFAULT_MODEL, purpose="t1_review")
    ledger = metered.ledger
    assert ledger is not None and completion.usage_id is not None

    conn.rows = [(completion.usage_id,)]  # the UPDATE ... RETURNING id matched a row
    ledger.attach_decision(completion.usage_id, decision_journal_id=77)

    attach_sql, attach_params = conn.calls[-1]
    assert attach_sql.lstrip().upper().startswith("UPDATE TOKEN_USAGE")
    assert attach_params == [77, completion.usage_id]


def test_attaching_an_unknown_or_already_attached_row_raises(
    metered: MeteredLLM, conn: RecordingConnection
) -> None:
    """Write-once: a second attach cannot silently re-point a spend at a different decision."""
    from accounting import UnknownUsageError

    completion = metered.complete(a_question(), model=DEFAULT_MODEL, purpose="t1_review")
    ledger = metered.ledger
    assert ledger is not None and completion.usage_id is not None

    conn.rows = []  # the guarded UPDATE matched nothing
    with pytest.raises(UnknownUsageError, match="already attached"):
        ledger.attach_decision(completion.usage_id, decision_journal_id=77)


def test_an_unpriced_model_fails_the_whole_metered_call(metered: MeteredLLM) -> None:
    """The loud failure propagates: a metered call to an unlisted model never books ₹0 quietly."""
    with pytest.raises(UnknownModelError):
        metered.complete(a_question(), model="claude-not-on-the-card", purpose="t1_review")


def test_a_metered_call_uses_the_injected_clocks_date_for_pricing(
    conn: RecordingConnection, card: PriceCard
) -> None:
    """Rates apply as of the decision's date, taken from the injected clock, not the wall clock."""
    reply = StubReply(text="…", usage=Usage(input_tokens=1000, output_tokens=500))
    inner = StubLLM({prompt_digest(a_question(), model="claude-sonnet-5"): reply})
    on_intro = FrozenClock(datetime(2026, 8, 31, 19, 0, tzinfo=IST))
    metered = MeteredLLM(
        inner,
        pricer=TokenPricer(card),
        ledger=TokenLedger(cast(Connection, conn), clock=on_intro),
        clock=on_intro,
    )

    completion = metered.complete(a_question(), model="claude-sonnet-5", purpose="t1_review")

    assert completion.priced.cost_inr == Decimal("0.616000")  # introductory rate, from the clock
