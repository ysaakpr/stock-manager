"""M17 commons digests, ``commons-digest/2`` (2026-11-01): every figure's number is in the filing.

What these tests pin:

1. **A misread number is refused, never dropped.** A figure whose number the filing does not
   state fails the digest as a whole (:class:`DigestRefusedError`, naming the figure), so the run
   records the filing as a failure and stores nothing for it. DeepSeek's one misread revenue line
   of the 2026-10-30 comparison (``5,039,600,000`` for ``5030600000``) is the first case.
2. **A computed value is refused.** The schema asks for stated numbers only, so a margin or a
   growth rate the filing does not state fails like a misread.
3. **The reading is numeric.** Indian and western grouping, scale words written out or in words,
   trailing zeros, a rounded rescale, a percent sign and a date in any common format all match
   the text they came from. Each accepted form has a refused near-miss beside it, so the tests
   fail if the check is inverted or switched off.
4. **The prompt and schema ask for what the check needs:** results headlines naming revenue and
   profit after tax, and an announcement's dates, percentages and quantities as figures.

Every filing text here is a real one, rebuilt from the lake by ``filing_inputs`` (exchange
announcements and an XBRL results filing of October 2026; public disclosures).
"""

from __future__ import annotations

from datetime import date
from typing import Any

import pytest

from analyst.commons import FilingInput, FilingKind, InMemoryDigestStore, build_digests
from analyst.commons.digests import (
    DIGEST_OUTPUT_SCHEMA,
    DIGEST_SYSTEM_PROMPT,
    DIGEST_TOOL,
    DigestBody,
    DigestRefusedError,
    digest_filing,
    digest_messages,
    ground_digest,
    parse_digest,
    ungrounded_figures,
)
from analyst.fundmanager import load_roster
from analyst.llm import StubLLM, StubReply, ToolCall, Usage, prompt_digest
from dataplatform.clock import FrozenClock
from tests.unit.test_commons_digests import (
    DAY2,
    UNIVERSE,
    DigestWorld,
    FakeDigestSource,
    _ann,
    _clock,
)
from tests.unit.test_commons_sheets import RISING, Gate

DIGEST_MODEL = load_roster().primaries[0].models.digest

#: pit_fundamentals:IF203896 (filed 2026-10-24), the known hard case.
RESULTS_TEXT = """Results filing IF203896, filed 2026-10-24.
nature | period | concept | value (INR unless the concept is per share or a count)
consolidated | 2026-07-01 to 2026-09-30 | debt_equity_ratio | 0.0000
consolidated | 2026-07-01 to 2026-09-30 | eps_basic | 7.0300
consolidated | 2026-07-01 to 2026-09-30 | eps_diluted | 7.0300
consolidated | 2026-07-01 to 2026-09-30 | face_value_per_share | 2.0000
consolidated | 2026-07-01 to 2026-09-30 | other_income | 118900000.0000
consolidated | 2026-07-01 to 2026-09-30 | paid_up_equity_capital | 120400000.0000
consolidated | 2026-07-01 to 2026-09-30 | profit_after_tax | 423100000.0000
consolidated | 2026-07-01 to 2026-09-30 | profit_attributable_to_owners | 0.0000
consolidated | 2026-07-01 to 2026-09-30 | profit_before_tax | 468200000.0000
consolidated | 2026-07-01 to 2026-09-30 | revenue_from_operations | 5030600000.0000
consolidated | 2026-07-01 to 2026-09-30 | shares_outstanding | 60200000.0000
consolidated | 2026-07-01 to 2026-09-30 | total_expenses | 4681300000.0000
consolidated | 2026-07-01 to 2026-09-30 | total_income | 5149500000.0000"""

#: nse_announcements:106813483 (2026-10-10): Indian grouping, and the amount in words.
ORDER_TEXT = (
    "Subject: Bagging/Receiving of orders/contracts\n"
    "Disseminated: 2026-10-10T13:30:23+05:30\n"
    "Text: Viviana Power Tech Limited has informed the Exchange about receiving a new turnkey "
    "contract aggregating to Rs. 466,73,61,738.00 (Rupees Four Hundred Sixty-Six Crore "
    "Seventy-Three Lakh Sixty-One Thousand Seven Hundred Thirty-Eight Only), inclusive of taxes, "
    "from existing client M/s. Madhya Gujarat Vij Company Limited (MGVCL).\n"
    "Attachment (not included): https://nsearchives.nseindia.com/corporate/"
    "VIVIANA_10102026133007_VIVIANA__Intimation_of_Receiving_Order_MGVCL_10102026.pdf"
)

#: nse_announcements:106829115 (2026-10-26): a date written "the 02nd of November 2026".
CALL_TEXT = (
    "Subject: Analysts/Institutional Investor Meet/Con. Call Updates\n"
    "Disseminated: 2026-10-26T19:34:44+05:30\n"
    "Text: Updater Services Limited has informed the exchange about Intimation under Regulation "
    "30 of SEBI (Listing Obligation and Disclosure Requirement) Regulation, 2015 about Earnings "
    "Conference Call to be held on Monday, the 02nd of November 2026, at 11:00 A.M\n"
    "Attachment (not included): https://nsearchives.nseindia.com/corporate/"
    "COMPLIANCEOFFICERUDS_26102026193432_Reg_30_6__Earnings_Call_Intimation_Sd.pdf"
)

#: nse_announcements:106824680 (2026-10-22): an effective date.
RESIGNATION_TEXT = (
    "Subject: Resignation\n"
    "Disseminated: 2026-10-22T14:08:01+05:30\n"
    "Text: Mahindra Lifespace Developers Limited has informed the Exchange regarding Resignation "
    "of Ms Parveen Mahtani as Chief Legal Officer of the company w.e.f. October 28, 2026.\n"
    "Attachment (not included): https://nsearchives.nseindia.com/corporate/"
    "MAHLIFE1_22102026140751_OutcomeofBoardMeetingOthersBPsg.pdf"
)

#: nse_announcements:106827907 (2026-10-26): a percentage and a rank.
SHARE_TEXT = (
    "Subject: General Updates\n"
    "Disseminated: 2026-10-26T08:57:14+05:30\n"
    "Text: Skyways Air Services Limited has informed the Exchange that Skyways Air Services "
    "Limited Strengthens International Cargo Leadership with 6.3% Market Share and Advances to "
    "42nd\n"
    "Attachment (not included): https://nsearchives.nseindia.com/corporate/"
    "SKYWAYS_26102026085642_Intimation_to_SE_1.pdf"
)

#: nse_announcements:106828081 (2026-10-26): the quantity Sonnet left out of figures.
PRESS_TEXT = (
    "Subject: Press Release\n"
    "Disseminated: 2026-10-26T11:32:18+05:30\n"
    "Text: Jyoti Structures Limited has informed the Exchange regarding a press release dated "
    'October 26, 2026, titled "Press Release on notifications of award from the Power Grid '
    'Corporation of India Limited for 800 kV HDVC Transmission Lines".\n'
    "Attachment (not included): https://nsearchives.nseindia.com/corporate/"
    "JYOTISTRUC_26102026113202_JSL_Press_Release.pdf"
)

TEXTS = {
    "results": (RESULTS_TEXT, FilingKind.RESULTS, date(2026, 10, 24)),
    "order": (ORDER_TEXT, FilingKind.ANNOUNCEMENT, date(2026, 10, 10)),
    "call": (CALL_TEXT, FilingKind.ANNOUNCEMENT, date(2026, 10, 26)),
    "resignation": (RESIGNATION_TEXT, FilingKind.ANNOUNCEMENT, date(2026, 10, 22)),
    "share": (SHARE_TEXT, FilingKind.ANNOUNCEMENT, date(2026, 10, 26)),
    "press": (PRESS_TEXT, FilingKind.ANNOUNCEMENT, date(2026, 10, 26)),
}


def _item(name: str) -> FilingInput:
    text, kind, knowable = TEXTS[name]
    return FilingInput(
        filing_id=f"fixture:{name}",
        kind=kind,
        isin=RISING,
        knowable_date=knowable,
        text=text,
        truncated=False,
    )


def _body(value: str, unit: str | None = None) -> dict[str, Any]:
    return {
        "headline": "Fixture",
        "disclosed": "Fixture.",
        "period": None,
        "figures": [{"label": "figure", "value": value, "unit": unit, "period": None}],
    }


#: (filing, value, unit) a faithful digest may write, each beside a refused near-miss below.
STATED = [
    ("results", "5030600000.0000", "INR"),  # verbatim
    ("results", "5030600000", "INR"),  # trailing zeros
    ("results", "5,030,600,000", "INR"),  # western grouping
    ("results", "5,03,06,00,000", "INR"),  # Indian grouping
    ("results", "5,030.6", "INR million"),  # a scale in the unit
    ("results", "INR 503.06 crore", None),  # a scale in the value
    ("results", "503.06", "Rs. crore"),
    ("results", "50,30,600", "INR thousand"),
    ("results", "5.03 billion", "INR"),  # rounded to the precision it is written at
    ("results", "7.03", "INR per share"),
    ("results", "(42.31) crore", "INR"),  # a bracketed figure reads as its magnitude
    ("results", "30 September 2026", None),  # a date from the period column
    ("order", "466,73,61,738.00", "INR"),  # the filing's own odd grouping
    ("order", "Rs. 466.74 crore", None),
    ("order", "4,667,361,738", "INR"),
    ("order", "466 crore 73 lakh", None),
    ("call", "2 November 2026 (Monday)", None),
    ("call", "2026-11-02", None),
    ("call", "02/11/2026", None),
    ("call", "Nov 2, 2026, 11:00 AM", None),
    ("call", "30", "SEBI LODR regulation"),
    ("resignation", "October 28, 2026", None),
    ("resignation", "28-Oct-2026", None),
    ("resignation", "28th October, 2026", None),
    ("share", "6.3", "%"),
    ("share", "6.3%", None),
    ("share", "6.30 per cent", None),
    ("share", "42nd", None),
    ("press", "800", "kV"),
    ("press", "26 October 2026", None),
]

#: Near-misses of STATED: a misread digit, a wrong scale or date, or a value the model computed.
UNSTATED = [
    ("results", "5,039,600,000", "INR"),  # DeepSeek's merged, misread revenue (2026-10-30)
    ("results", "5039600000.0000", "INR"),
    ("results", "5,130.6", "INR million"),
    ("results", "503.6 crore", "INR"),
    ("results", "7.30", "INR per share"),
    ("results", "8.41", "%"),  # PAT margin, 423.1 / 5,030.6: computed, not stated
    ("results", "31 October 2026", None),
    ("order", "476,73,61,738.00", "INR"),
    ("order", "Rs. 467.74 crore", None),
    ("call", "3 November 2026 (Monday)", None),
    ("call", "2026-12-02", None),
    ("resignation", "October 29, 2026", None),
    ("share", "6.8", "%"),
    ("share", "43rd", None),
    ("press", "900", "kV"),
]


@pytest.mark.parametrize(("name", "value", "unit"), STATED)
def test_a_stated_number_in_any_common_form_is_grounded(
    name: str, value: str, unit: str | None
) -> None:
    body = parse_digest(_body(value, unit))
    assert ungrounded_figures(body, _item(name)) == ()
    assert ground_digest(body, _item(name)) is body


@pytest.mark.parametrize(("name", "value", "unit"), UNSTATED)
def test_a_number_the_filing_does_not_state_is_refused(
    name: str, value: str, unit: str | None
) -> None:
    body = parse_digest(_body(value, unit))
    assert [f.value for f in ungrounded_figures(body, _item(name))] == [value]
    with pytest.raises(DigestRefusedError, match="not in the filing text") as refused:
        ground_digest(body, _item(name))
    assert repr(value) in str(refused.value)  # the refusal names the figure


def test_one_bad_figure_refuses_the_whole_digest_and_drops_nothing() -> None:
    good = [
        {"label": "revenue_from_operations", "value": "5030600000.0000", "unit": "INR"},
        {"label": "profit_after_tax", "value": "423100000.0000", "unit": "INR"},
    ]
    bad = {"label": "total_income", "value": "5148500000.0000", "unit": "INR"}
    figures = [{**f, "period": "2026-07-01 to 2026-09-30"} for f in [*good, bad]]
    body = DigestBody.model_validate({**_body("0"), "figures": figures})
    assert [f.label for f in ungrounded_figures(body, _item("results"))] == ["total_income"]
    with pytest.raises(DigestRefusedError, match="1 figure"):
        ground_digest(body, _item("results"))


def _reply(body: dict[str, Any]) -> StubReply:
    return StubReply(
        text="",
        tool_calls=(ToolCall(id="structured_output", name=DIGEST_TOOL.name, arguments=body),),
        usage=Usage(input_tokens=300, output_tokens=60),
    )


def _key(item: FilingInput) -> str:
    return prompt_digest(
        digest_messages(item), model=DIGEST_MODEL, tools=(DIGEST_TOOL,), system=DIGEST_SYSTEM_PROMPT
    )


def test_digest_filing_grounds_every_answer() -> None:
    item = _item("press")
    clock = FrozenClock(_clock().now())
    good = StubLLM({_key(item): _reply(_body("800", "kV"))}, synthesize_unknown=False)
    digest = digest_filing(item, llm=good, trading_date=DAY2, clock=clock, model=DIGEST_MODEL)
    assert [f.value for f in digest.body.figures] == ["800"]
    bad = StubLLM({_key(item): _reply(_body("900", "kV"))}, synthesize_unknown=False)
    with pytest.raises(DigestRefusedError, match="'900'"):
        digest_filing(item, llm=bad, trading_date=DAY2, clock=clock, model=DIGEST_MODEL)


def test_a_build_names_an_ungrounded_digest_as_a_failure_and_stores_nothing() -> None:
    world = DigestWorld(announcements=[_ann(RISING, "r-1", DAY2, body="Order of Rs 120 crore.")])
    item = _ann_input(world)
    store = InMemoryDigestStore()
    llm = StubLLM({_key(item): _reply(_body("Rs 210 crore"))}, synthesize_unknown=False)
    run = build_digests(
        DAY2,
        isins=UNIVERSE,
        source=FakeDigestSource(world),
        llm=llm,
        store=store,
        gate=Gate(),
        clock=_clock(DAY2),
        model=DIGEST_MODEL,
    )
    assert run.digested == () and store.get(item.filing_id) is None
    assert [f.filing_id for f in run.failures] == [item.filing_id]
    assert "'Rs 210 crore'" in run.failures[0].reason
    # The same build with the stated number stores the digest.
    fixed = StubLLM({_key(item): _reply(_body("Rs 120 crore"))}, synthesize_unknown=False)
    again = build_digests(
        DAY2,
        isins=UNIVERSE,
        source=FakeDigestSource(world),
        llm=fixed,
        store=InMemoryDigestStore(),
        gate=Gate(),
        clock=_clock(DAY2),
        model=DIGEST_MODEL,
    )
    assert again.digested == (item.filing_id,) and again.failures == ()


def _ann_input(world: DigestWorld) -> FilingInput:
    from analyst.commons.digests import filing_inputs

    (item,) = filing_inputs(
        world.announcements, (), isins=UNIVERSE, after=date.min, through=date.max
    )
    return item


def _descriptions() -> dict[str, str]:
    properties = DIGEST_OUTPUT_SCHEMA["properties"]
    value = properties["figures"]["items"]["properties"]["value"]
    return {
        "headline": properties["headline"]["description"],
        "period": properties["period"]["description"],
        "figures": properties["figures"]["description"],
        "value": value["description"],
    }


def test_results_headlines_are_asked_to_name_revenue_and_profit_after_tax() -> None:
    for text in (DIGEST_SYSTEM_PROMPT, _descriptions()["headline"]):
        assert "revenue from operations" in text and "profit after tax" in text
    assert "200 characters" in _descriptions()["headline"]  # the parser's bound, stated


def test_announcements_are_asked_for_dates_percentages_and_quantities_as_figures() -> None:
    for text in (DIGEST_SYSTEM_PROMPT, _descriptions()["figures"]):
        assert all(word in text for word in ("date", "percentage", "quantity"))
        assert "already says it" in text
    assert "computed" in _descriptions()["value"] and "computed" in DIGEST_SYSTEM_PROMPT
    assert "effective" in _descriptions()["period"]
