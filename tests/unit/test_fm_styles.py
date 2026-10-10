"""M17.14 — pre-registration §8 Amendment 2: four styles, each trading a primary and a mirror book.

The acceptance criteria, one section each:

1. each manager's rendered prompt carries only its own ``[[STYLE]]`` playbook, its own starting
   screens are listed first, and its mirror book never appears in any prompt;
2. a mirror order the participation rail refuses is journaled on the ``-1CR`` book only, and the
   ``-10L`` book is untouched (book level, then through the whole daily job);
3. the desk shuffle is deterministic per (manager, session), differs across managers, and drops
   no name;
4. the §6 rule runs per book against its own ``CTRL-<book>`` and the Amendment 2 (e) graduation
   floor counts primary books only — inverting either fails a test here — and the scoreboard
   rebuilt from the journal equals the job's byte for byte.

Plus the style books' lists (Amendment 2 d) and the FUND quality filter, inversion-tested.
"""

from __future__ import annotations

import os
import re
import subprocess
import sys
from collections.abc import Mapping, Sequence
from datetime import date
from decimal import Decimal
from pathlib import Path
from typing import Any, cast

import pytest

from analyst.commons import CommonsScreens
from analyst.commons.screens import ScreenEntry
from analyst.fundmanager import BookRole, ManagerStyle, Roster, load_roster
from analyst.fundmanager.books import BookOrder, FundBook
from analyst.fundmanager.controls import (
    FUND_MAX_DEBT_EQUITY,
    ControlError,
    QualityFacts,
    passes_quality,
    quality_from_dossier,
    style_candidates,
)
from analyst.fundmanager.mirror import (
    MIRROR_DIVERGENCE_EVENT,
    MirrorState,
    MirrorStatus,
    divergence_entry,
    plan_mirror,
    post_decision_quantities,
    settle_plan,
    target_weights,
)
from analyst.fundmanager.render import (
    ROUND_FINAL,
    ROUND_RESEARCH,
    ROUND_ZERO,
    DeskOrder,
    PromptTemplate,
    render_screens,
    render_shortlist,
)
from analyst.fundmanager.runtime import ManagerCommons
from analyst.fundmanager.scoreboard import (
    WINDOW_SESSIONS,
    BookMark,
    BooksPassed,
    DecisionAction,
    DecisionOutcome,
    OutcomeReason,
    ScoreboardInputs,
    ScoredDecision,
    ScoreVerdict,
    build_scoreboard,
    inputs_from_journal,
)
from analyst.journal.models import Decision
from backtest.fm_job import RunOutcome, SessionCommons
from dataplatform.clock import FrozenClock
from execution.broker import Side
from tests.fm_books_support import Bar, FmMarket, ListJournal, open_book, switch_at, weekdays
from tests.fm_scoreboard_support import isin, mini_roster, shortlist_of
from tests.unit.commons_screen_world import NAMES
from tests.unit.commons_screen_world import SESSION as WORLD_SESSION
from tests.unit.test_fm_job import (
    CALENDAR,
    Builder,
    Desk,
    FakeWorld,
    PerManagerLLM,
    ScriptedRunner,
    _decision,
    _Sentinel,
    _verdict,
    flat_market,
    staged,
)
from tests.unit.test_fm_runtime import World, _book, _commons, _mandate, _session
from tests.unit.test_fm_runtime import world as world  # the M17.9 synthetic Commons fixture

ROSTER = load_roster()
PRIMARIES = [b.id for b in ROSTER.primaries]
PROMPT = PromptTemplate.load()
_BLOCK = re.compile(r"\[\[STYLE:(?P<key>[A-Z_]+)\]\]\n(?P<body>.*?)\[\[/STYLE\]\]", re.DOTALL)
#: Each style's playbook, verbatim from the frozen prompt.
PLAYBOOKS: Mapping[str, str] = {m["key"]: m["body"].strip() for m in _BLOCK.finditer(PROMPT.text)}
_SECTION = re.compile(r"^(?:(S[1-4]):|(S5) event watch .*)$")


# ── 1. the prompt: own playbook only, own screens first, the mirror never shown ──────────────────


def _prompts(world: World, tmp_path: Path, book_id: str) -> dict[str, str]:
    run = _session(world, tmp_path, _mandate(book_id), _book(book_id))
    return {key: run._prompt(key) for key in (ROUND_ZERO, ROUND_RESEARCH, ROUND_FINAL)}


def _screen_sections(prompt: str) -> list[str]:
    block = prompt[prompt.index("### Screens") : prompt.index("### Composite shortlist")]
    found: list[str] = []
    for line in block.splitlines():
        match = _SECTION.match(line)
        if match:
            found.append(match.group(1) or match.group(2))
    return found


def test_the_prompt_has_exactly_the_four_amendment_2_playbooks() -> None:
    assert set(PLAYBOOKS) == {s.value for s in ManagerStyle}
    assert all(body.startswith("### Your playbook:") for body in PLAYBOOKS.values())


@pytest.mark.parametrize("book_id", PRIMARIES)
def test_each_managers_prompt_carries_only_its_own_playbook_and_lists_its_screens_first(
    world: World, tmp_path: Path, book_id: str
) -> None:
    mandate = _mandate(book_id)
    own = PLAYBOOKS[mandate.style.value]
    for prompt in _prompts(world, tmp_path, book_id).values():
        assert own in prompt, "the whole of its own playbook, verbatim"
        for style, body in PLAYBOOKS.items():
            if style != mandate.style.value:
                assert body.splitlines()[0] not in prompt, f"{style}'s playbook leaked"
        sections = _screen_sections(prompt)
        starting = list(mandate.starting_screens)
        assert sections[: len(starting)] == starting, sections
        assert sorted(sections) == ["S1", "S2", "S3", "S4", "S5"], "every screen still listed"


def test_the_screen_order_follows_the_style_not_a_fixed_list(world: World, tmp_path: Path) -> None:
    firsts = {
        book_id: _screen_sections(_prompts(world, tmp_path, book_id)[ROUND_ZERO])[0]
        for book_id in PRIMARIES
    }
    assert firsts == {
        "FM-SWING-BRK-10L": "S2",
        "FM-SWING-EVT-10L": "S4",
        "FM-POS-TREND-10L": "S1",
        "FM-POS-FUND-10L": "S4",
    }


@pytest.mark.parametrize("book_id", PRIMARIES)
def test_the_mirror_never_appears_in_any_prompt(world: World, tmp_path: Path, book_id: str) -> None:
    mandate = _mandate(book_id)
    mirror = ROSTER.mirror_of(mandate.manager)
    others = [b.id for b in ROSTER.books if b.id != book_id]
    for prompt in _prompts(world, tmp_path, book_id).values():
        assert mirror.id not in prompt
        assert "1CR" not in prompt and "mirror" not in prompt.lower()
        assert f"{mirror.opening_capital_inr:,}" not in prompt  # ₹1,00,00,000 in any form
        assert "10,000,000" not in prompt and "1,00,00,000" not in prompt
        assert not [other for other in others if other in prompt]
        assert f"# You are {mandate.manager}," in prompt


# ── 3. the desk shuffle ──────────────────────────────────────────────────────────────────────────

FORTY = [isin(n) for n in range(1, 41)]


def _ranked_lines(text: str) -> list[str]:
    return [line for line in text.splitlines() if line.startswith("- rank ")]


def test_the_desk_shuffle_is_deterministic_per_manager_and_session() -> None:
    shortlist = shortlist_of(WORLD_SESSION, FORTY)
    for manager in ROSTER.manager_ids:
        order = DeskOrder(manager, WORLD_SESSION)
        assert render_shortlist(shortlist, {}, order) == render_shortlist(
            shortlist, {}, DeskOrder(manager, WORLD_SESSION)
        )
    listings = {
        m: tuple(_ranked_lines(render_shortlist(shortlist, {}, DeskOrder(m, WORLD_SESSION))))
        for m in ROSTER.manager_ids
    }
    assert len(set(listings.values())) == len(ROSTER.manager_ids), "differs across managers"
    tops = {listing[0] for listing in listings.values()}
    assert len(tops) > 1, "the managers do not all see the same name at the top"
    next_day = DeskOrder("FM-SWING-BRK", date(2026, 10, 9))
    assert (
        tuple(_ranked_lines(render_shortlist(shortlist, {}, next_day))) != listings["FM-SWING-BRK"]
    ), "and across sessions"


def test_the_desk_shuffle_drops_no_name_and_keeps_every_rank() -> None:
    shortlist = shortlist_of(WORLD_SESSION, FORTY)
    plain = _ranked_lines(render_shortlist(shortlist, {}))
    for manager in ROSTER.manager_ids:
        shuffled = _ranked_lines(render_shortlist(shortlist, {}, DeskOrder(manager, WORLD_SESSION)))
        assert sorted(shuffled) == sorted(plain) and len(shuffled) == 40
        assert shuffled != plain, "a shuffle, not the ranked order"
        for line in shuffled:  # each name with its own rank, as the Commons ranked it
            rank, name = re.match(r"- rank (\d+): (\S+)", line).groups()  # type: ignore[union-attr]
            assert FORTY[int(rank) - 1] == name


def test_the_screens_shuffle_keeps_every_name_on_every_screen(world: World) -> None:
    rows = {r.isin: r for r in world.sheets.universe}
    plain = render_screens(world.screens, rows)
    for manager in ROSTER.manager_ids:
        order = DeskOrder(manager, WORLD_SESSION, ("S2", "S3"))
        text = render_screens(world.screens, rows, order)
        assert text == render_screens(world.screens, rows, order)
        assert sorted(_ranked_lines(text)) == sorted(_ranked_lines(plain))
        facts = [line for line in text.splitlines() if " knowable " in line]
        assert sorted(facts) == sorted(line for line in plain.splitlines() if " knowable " in line)


_CHILD = """
from datetime import date
from analyst.fundmanager.render import DeskOrder
names = [f"INE{n:05d}A010" for n in range(1, 41)]
print(",".join(DeskOrder("FM-POS-FUND", date(2026, 10, 9)).shuffle(names, lambda x: x)))
"""


def test_the_desk_shuffle_is_stable_across_processes_and_hash_seeds() -> None:
    expected = ",".join(DeskOrder("FM-POS-FUND", date(2026, 10, 9)).shuffle(FORTY, lambda x: x))
    for seed in ("0", "7", "31337"):
        out = subprocess.run(
            [sys.executable, "-c", _CHILD],
            capture_output=True,
            text=True,
            check=True,
            cwd=Path(__file__).resolve().parents[2],
            env={**os.environ, "PYTHONHASHSEED": seed},
        ).stdout.strip()
        assert out == expected


# ── the style books' lists (Amendment 2 d) ───────────────────────────────────────────────────────


def _restated(screens: CommonsScreens, **lists: Sequence[str]) -> CommonsScreens:
    """``screens`` with the named ranked lists replaced (ranked in the order given), re-digested."""
    update = {
        name: tuple(
            ScreenEntry(position=k, isin=i, score=Decimal(100 - k)) for k, i in enumerate(isins, 1)
        )
        for name, isins in lists.items()
    }
    changed = screens.model_copy(update=update)
    return changed.model_copy(update={"screens_digest": CommonsScreens.digest_of(changed.body())})


def test_each_style_list_is_cut_from_its_starting_screens(world: World) -> None:
    a, b, c, d, e = (isin(n) for n in range(901, 906))
    screens = _restated(world.screens, s1=[a, b], s2=[c, d], s3=[d, e], s4=[e, a])
    lists = {
        style: style_candidates(style, screens, quality={i: _quality(i) for i in (a, b, c, d, e)})
        for style in ManagerStyle
    }
    assert lists[ManagerStyle.SWING_BREAKOUT].isins == (c, d, e), "S2 first, then S3, no repeat"
    assert lists[ManagerStyle.SWING_EVENT].isins == (e, a)
    assert lists[ManagerStyle.POSITIONAL_TREND].isins == (a, b)
    assert lists[ManagerStyle.POSITIONAL_FUNDAMENTAL].isins == (e, a)
    assert all(r.digest == screens.screens_digest for r in lists.values())
    with pytest.raises(ControlError, match="quality facts"):
        style_candidates(ManagerStyle.POSITIONAL_FUNDAMENTAL, screens)
    # an S4 name with no quality facts at all never passes
    only_e = style_candidates(
        ManagerStyle.POSITIONAL_FUNDAMENTAL, screens, quality={e: _quality(e)}
    )
    assert only_e.isins == (e,)


def _quality(name: str, **over: Any) -> QualityFacts:
    base: dict[str, Any] = {
        "isin": name,
        "sector": "Capital Goods",
        "debt_equity": Decimal("0.4"),
        "net_margin_ttm": Decimal("0.12"),
        "pe_ttm": Decimal("25"),
    }
    base.update(over)
    return QualityFacts(**base)


@pytest.mark.parametrize(
    ("over", "passes"),
    [
        ({}, True),
        ({"debt_equity": FUND_MAX_DEBT_EQUITY}, False),  # strictly below 1.5
        ({"debt_equity": Decimal("1.49")}, True),
        ({"debt_equity": Decimal("3.2")}, False),
        ({"debt_equity": None}, False),  # an unknown never passes a quality filter
        ({"sector": "Financial Services", "debt_equity": Decimal("8")}, True),  # exempt
        ({"sector": "Financial Services", "debt_equity": None}, True),
        ({"net_margin_ttm": Decimal("-0.02"), "pe_ttm": None}, False),  # a loss
        ({"net_margin_ttm": None, "pe_ttm": Decimal("14")}, True),  # either sign proxy
        ({"net_margin_ttm": Decimal("0.03"), "pe_ttm": None}, True),
        ({"net_margin_ttm": None, "pe_ttm": None}, False),
        ({"net_margin_ttm": Decimal(0), "pe_ttm": None}, False),  # zero profit is not positive
        ({"sector": "Financial Services", "net_margin_ttm": None, "pe_ttm": None}, False),
    ],
)
def test_the_fund_quality_filter(over: dict[str, Any], passes: bool) -> None:
    assert passes_quality(_quality(isin(1), **over)) is passes


def test_quality_facts_are_read_off_the_dossier(world: World, tmp_path: Path) -> None:
    commons = _commons(world, tmp_path)
    universe = [r.isin for r in world.sheets.universe]
    for dossier in commons.dossiers(universe[:3]):
        facts = quality_from_dossier(dossier)
        assert facts.isin == dossier.isin
        assert facts.sector == dossier.fields.get("sector")
        for name in ("debt_equity", "net_margin_ttm", "pe_ttm"):
            value = dossier.fields.get(name)
            assert getattr(facts, name) == (value if isinstance(value, Decimal) else None)


# ── 2. the mirror: driven toward the primary's weights, through its own rails ────────────────────

#: The test calendar: the books trade from `DAYS[0]`, which has the 20 sessions of traded-value
#: history the participation rail needs behind it.
HISTORY = weekdays(date(2026, 8, 3), 50)
DAYS = HISTORY[25:]
THIN, DEEP = isin(801), isin(802)


def _market(thin_value: Decimal = Decimal("2000000")) -> FmMarket:
    """Every name at 100. THIN trades ₹20 L a day, so 5 % participation is ₹1 L: room for a 10 L
    book's 4 % buy (₹40,000) and none for a 1 cr book's (₹4 L). DEEP trades ₹1,000 cr a day."""
    bars: dict[tuple[str, date], Bar] = {}
    for day in HISTORY:
        bars[(THIN, day)] = Bar(Decimal(100), Decimal(100), thin_value)
        bars[(DEEP, day)] = Bar(Decimal(100), Decimal(100), Decimal("10000000000"))
    return FmMarket(HISTORY, bars, {THIN: "Tech", DEEP: "Banks"})


def _pair(tmp_path: Path, market: FmMarket) -> tuple[FundBook, FundBook, ListJournal, FrozenClock]:
    clock = FrozenClock(DAYS[0])
    journal = ListJournal()
    switch = switch_at(tmp_path, clock)
    primary, _ = open_book(
        "FM-SWING-BRK-10L", market=market, clock=clock, kill_switch=switch, journal=journal
    )
    mirror, _ = open_book(
        "FM-SWING-BRK-1CR", market=market, clock=clock, kill_switch=switch, journal=journal
    )
    return primary, mirror, journal, clock


def _follow(
    primary: FundBook, mirror: FundBook, report: Any, day: date, state: MirrorState
) -> tuple[Any, Any, Any]:
    targets = target_weights(primary, report, day)
    plan = plan_mirror(mirror, targets, day, state, max_positions=15)
    mirror_report = mirror.decide(day, plan.orders)
    lines = settle_plan(plan, mirror_report, mirror)
    entry, evidence = divergence_entry(mirror, targets, lines, mirror_report, day)
    mirror.journal.append(entry, evidence=evidence)
    return targets, mirror_report, {line.isin: line for line in lines}


def test_a_mirror_refusal_by_participation_is_journaled_on_the_1cr_book_only(
    tmp_path: Path,
) -> None:
    primary, mirror, journal, _ = _pair(tmp_path, _market())
    day = DAYS[0]
    report = primary.decide(day, [BookOrder(THIN, Side.BUY, 400, "a 4 % position in THIN")])
    assert [o.isin for o, _ in report.staged] == [THIN] and not report.refused
    primary_entries = [e for e in journal.entries if e.case_id == primary.book_id]
    primary_orders = dict(primary.account.quantities()), primary.account.spendable_cash

    targets, mirror_report, lines = _follow(primary, mirror, report, day, MirrorState())

    assert targets.weights[THIN] == Decimal("0.04")
    blocks = [e for e in journal.entries if e.decision is Decision.RAIL_BLOCK]
    assert [e.case_id for e in blocks] == [mirror.book_id], "the refusal is the 1CR's alone"
    assert "PARTICIPATION" in blocks[0].payload["rails"] and blocks[0].isin == THIN
    ((refused, _),) = mirror_report.refused
    assert refused.quantity == 4000, "the mirror asked for the whole 4 %, never resized"
    assert lines[THIN].status is MirrorStatus.REFUSED and lines[THIN].rails == ("PARTICIPATION",)
    (divergence,) = [
        e for e in journal.entries if e.payload.get("event") == MIRROR_DIVERGENCE_EVENT
    ]
    assert divergence.case_id == mirror.book_id
    assert divergence.payload["primary"] == primary.book_id
    assert Decimal(divergence.payload["tracking_gap_pp"]) == Decimal("4.0000")
    assert '"status":"REFUSED"' in divergence.payload["names"]
    # the 10L book is untouched: no new line in its stream, the same account
    assert [e for e in journal.entries if e.case_id == primary.book_id] == primary_entries
    assert (dict(primary.account.quantities()), primary.account.spendable_cash) == primary_orders


def test_the_mirror_buys_the_primarys_weight_where_participation_allows(tmp_path: Path) -> None:
    primary, mirror, journal, _ = _pair(tmp_path, _market())
    day = DAYS[0]
    report = primary.decide(day, [BookOrder(DEEP, Side.BUY, 700, "a 7 % position in DEEP")])
    _, mirror_report, lines = _follow(primary, mirror, report, day, MirrorState())
    ((order, _),) = mirror_report.staged
    assert (order.isin, order.side, order.quantity) == (DEEP, Side.BUY, 7000)  # 7 % of ₹1 cr
    assert order.event == "MIRROR_FOLLOW" and lines[DEEP].status is MirrorStatus.STAGED
    assert not [e for e in journal.entries if e.decision is Decision.RAIL_BLOCK]


def _hold(
    primary: FundBook, mirror: FundBook, clock: FrozenClock, quantities: tuple[int, int]
) -> None:
    """Fill a THIN position in both books: buy on DAYS[0], fill on DAYS[1]."""
    clock.freeze_at(DAYS[0])
    primary.decide(DAYS[0], [BookOrder(THIN, Side.BUY, quantities[0], "setup")])
    mirror.decide(DAYS[0], [BookOrder(THIN, Side.BUY, quantities[1], "setup")])
    clock.freeze_at(DAYS[1])
    primary.execute(DAYS[1])
    mirror.execute(DAYS[1])


def test_a_mirror_sell_the_participation_rail_slices_is_worked_over_sessions(
    tmp_path: Path,
) -> None:
    # THIN deep enough for the setup buys (₹1 cr a day: 5 % is ₹5 L), then — restated over the
    # whole lookback — thin when the primary sells out (₹20 L a day: 5 % is ₹1 L, 1,000 shares)
    market = _market(Decimal("10000000"))
    primary, mirror, journal, clock = _pair(tmp_path, market)
    _hold(primary, mirror, clock, (400, 4000))
    for day in HISTORY:
        market.bars[(THIN, day)] = Bar(Decimal(100), Decimal(100), Decimal("2000000"))
    state = MirrorState()
    day = DAYS[4]  # past the min-hold window
    clock.freeze_at(day)
    report = primary.decide(day, [BookOrder(THIN, Side.SELL, 400, "the thesis broke")])
    assert post_decision_quantities(primary, report) == {}
    targets, mirror_report, lines = _follow(primary, mirror, report, day, state)
    assert targets.weights == {}
    assert lines[THIN].status is MirrorStatus.SLICED
    ((order, uid),) = mirror_report.staged
    assert order.quantity == 4000 and THIN in mirror.pending_exits
    child = next(e for e in journal.entries if e.orders_ref == uid)
    assert int(child.payload["quantity"]) == 1000, "₹1 L of participation buys 1,000 at 100"
    # the next session the open exit is left to work: no new parent, one more child
    nxt = DAYS[5]
    clock.freeze_at(nxt)
    primary.execute(nxt)
    mirror.execute(nxt)
    report = primary.decide(nxt, [])
    _, mirror_report, lines = _follow(primary, mirror, report, nxt, state)
    assert lines[THIN].status is MirrorStatus.EXITING
    assert [int(e.payload.get("exit_child", "0")) for e in journal.entries if e.orders_ref][-1] == 2
    assert not [e for e in journal.entries if e.decision is Decision.RAIL_BLOCK]


def test_drift_inside_the_band_is_left_alone_and_outside_it_is_traded(tmp_path: Path) -> None:
    primary, mirror, _, clock = _pair(tmp_path, _market(Decimal("2000000000")))
    _hold(
        primary, mirror, clock, (500, 4700)
    )  # 5 % vs 4.7 %: 6 % of target off, inside the 10 % band
    day = DAYS[3]
    clock.freeze_at(day)
    report = primary.decide(day, [])
    _, _, lines = _follow(primary, mirror, report, day, MirrorState())
    assert lines[THIN].status is MirrorStatus.ON_TARGET and lines[THIN].side is None

    primary2, mirror2, _, clock2 = _pair(tmp_path / "b", _market(Decimal("2000000000")))
    _hold(primary2, mirror2, clock2, (500, 4400))  # 12 % of target off: outside the band
    clock2.freeze_at(day)
    report = primary2.decide(day, [])
    _, mirror_report, lines = _follow(primary2, mirror2, report, day, MirrorState())
    assert lines[THIN].status is MirrorStatus.STAGED
    ((order, _),) = mirror_report.staged
    assert (order.side, order.quantity) == (Side.BUY, 600)


def test_a_name_the_primary_acted_on_is_followed_exactly_even_inside_the_band(
    tmp_path: Path,
) -> None:
    primary, mirror, _, clock = _pair(tmp_path, _market(Decimal("2000000000")))
    _hold(primary, mirror, clock, (500, 5000))
    day = DAYS[4]
    clock.freeze_at(day)
    report = primary.decide(day, [BookOrder(THIN, Side.SELL, 30, "trim a little")])
    _, mirror_report, lines = _follow(primary, mirror, report, day, MirrorState())
    ((order, _),) = mirror_report.staged
    assert (order.side, order.quantity) == (Side.SELL, 300) and lines[THIN].status is (
        MirrorStatus.STAGED
    )


def test_a_name_the_mirrors_own_stop_sold_is_not_bought_back_while_the_primary_holds(
    tmp_path: Path,
) -> None:
    primary, mirror, _, clock = _pair(tmp_path, _market(Decimal("2000000000")))
    primary.decide(DAYS[0], [BookOrder(THIN, Side.BUY, 500, "setup")])
    clock.freeze_at(DAYS[1])
    primary.execute(DAYS[1])
    state = MirrorState(stopped={THIN})
    day = DAYS[3]
    clock.freeze_at(day)
    report = primary.decide(day, [])
    _, mirror_report, lines = _follow(primary, mirror, report, day, state)
    assert lines[THIN].status is MirrorStatus.STOPPED and not mirror_report.staged
    assert state.stopped == {THIN}
    # once the primary holds none, the name is released
    clock.freeze_at(DAYS[4])
    report = primary.decide(DAYS[4], [BookOrder(THIN, Side.SELL, 500, "out")])
    _follow(primary, mirror, report, DAYS[4], state)
    assert state.stopped == set()


def test_a_buy_waits_for_cash_and_slots_rather_than_being_resized(tmp_path: Path) -> None:
    primary, mirror, _, _ = _pair(tmp_path, _market(Decimal("2000000000")))
    day = DAYS[0]
    report = primary.decide(day, [BookOrder(DEEP, Side.BUY, 900, "9 %")])
    targets = target_weights(primary, report, day)
    poor = plan_mirror(
        mirror,
        targets,
        day,
        MirrorState(),
        max_positions=0,
    )
    assert not poor.orders and poor.lines[DEEP].status is MirrorStatus.SLOTS


def test_the_mirror_state_round_trips() -> None:
    state = MirrorState(stop_pcts={THIN: Decimal("4.5")}, stopped={DEEP})
    assert MirrorState.from_document(state.to_document()) == state


# ── 2, through the whole daily job ───────────────────────────────────────────────────────────────


def test_through_the_job_the_mirror_is_refused_on_1cr_only_and_the_scoreboard_rebuilds(
    tmp_path: Path,
) -> None:
    desk = Desk(tmp_path)
    roster = mini_roster("FM-SWING-BRK")
    primary, mirror = roster.primaries[0].id, roster.mirrors[0].id
    thin = NAMES[4]
    s1, s2, s3 = date(2026, 10, 5), date(2026, 10, 6), date(2026, 10, 7)
    market = flat_market(
        {(thin, d): Bar(Decimal(100), Decimal(100), Decimal("2000000")) for d in CALENDAR}
    )
    runner = ScriptedRunner(
        {(primary, s1): [_verdict(_decision(thin, "BUY", stop_pct=4), quantity=400)]}
    )
    builder = Builder(
        lambda day: SessionCommons(
            shortlist=shortlist_of(day, NAMES[:2]), manager=cast(ManagerCommons, _Sentinel())
        )
    )
    results = [
        desk.run(
            day,
            FakeWorld(market),
            builder,
            PerManagerLLM({}),
            roster=roster,
            runner=runner,
            start=day == s1,
        )
        for day in (s1, s2, s3)
    ]
    assert all(r.outcome is RunOutcome.COMPLETED for r in results)
    # the runner — the manager — was only ever asked about its primary book
    assert {book for book, _ in runner.asked} == {primary}
    blocks = [e for e in desk.entries() if e.decision is Decision.RAIL_BLOCK]
    assert blocks and {e.case_id for e in blocks} == {mirror}
    assert all("PARTICIPATION" in e.payload["rails"] for e in blocks)
    assert all(e.trading_date in (s1, s2, s3) for e in blocks), "offered again each session"
    (outcome,) = results[0].managers
    assert (outcome.refused, outcome.mirror_refused, outcome.mirror_id) == (0, 1, mirror)
    # the primary bought and holds; the mirror holds nothing and says why, every session
    assert [e.isin for e in staged(desk, primary)] == [thin]
    assert not staged(desk, mirror)
    divergence = desk.events(MIRROR_DIVERGENCE_EVENT)
    assert [e.trading_date for e in divergence] == [s1, s2, s3]
    assert {e.case_id for e in divergence} == {mirror}
    assert all('"status":"REFUSED"' in e.payload["names"] for e in divergence)
    # rebuild-from-journal equality, with the mirror's lines in the journal
    for result in results:
        assert result.scoreboard is not None and result.scoreboard_error is None
        assert result.rebuilt_digest == result.scoreboard.digest()
    rebuilt = build_scoreboard(roster, inputs_from_journal(desk.entries(), roster))
    final = results[-1].scoreboard
    assert final is not None and rebuilt.canonical_bytes() == final.canonical_bytes()
    refusals = inputs_from_journal(desk.entries(), roster).refusals
    assert {r.book_id for r in refusals} == {mirror}


def test_through_the_job_both_books_hold_the_same_stop_percent(tmp_path: Path) -> None:
    desk = Desk(tmp_path)
    roster = mini_roster("FM-SWING-BRK")
    primary, mirror = roster.primaries[0].id, roster.mirrors[0].id
    held = NAMES[4]
    s1 = date(2026, 10, 5)
    runner = ScriptedRunner(
        {(primary, s1): [_verdict(_decision(held, "BUY", stop_pct=4), quantity=500)]}
    )
    builder = Builder(
        lambda day: SessionCommons(
            shortlist=shortlist_of(day, NAMES[:2]), manager=cast(ManagerCommons, _Sentinel())
        )
    )
    desk.run(s1, FakeWorld(flat_market()), builder, PerManagerLLM({}), roster=roster, runner=runner)
    record = desk.store.latest_completed("m17_dry2_fund_managers", before=date(2026, 10, 6))
    assert record is not None and record.book_state is not None
    books = record.book_state["books"]
    stops = {book: books[book]["stops"] for book in (primary, mirror)}
    assert [s["level"] for s in stops[primary]] == [s["level"] for s in stops[mirror]] == ["96"]
    assert books[mirror]["mirror"]["stop_pcts"] == {held: "4"}


# ── 4. the scoreboard: per book against its own control, the graduation floor ────────────────────

SESSIONS = weekdays(date(2025, 1, 1), 200)


def _marks(
    book_id: str,
    capital: Decimal,
    sessions: Sequence[date],
    pct: str | Mapping[int, str] = "0",
) -> list[BookMark]:
    """``capital`` grown by ``pct`` from the first session on (or, keyed by session index, from
    that index on), flat otherwise."""
    steps = {0: pct} if isinstance(pct, str) else dict(pct)
    level = "0"
    out: list[BookMark] = []
    for i, session in enumerate(sessions):
        level = steps.get(i, level)
        nav = (capital * (1 + Decimal(level) / 100)).quantize(Decimal("0.000001"))
        out.append(
            BookMark(
                book_id=book_id,
                session=session,
                nav=nav,
                cash=nav,
                positions=0,
                turnover=Decimal(0),
                costs=Decimal(0),
                interest=Decimal(0),
            )
        )
    return out


def _calibrated(
    book_id: str, sessions: Sequence[date]
) -> tuple[list[ScoredDecision], list[DecisionOutcome]]:
    """40 PASS decisions at p = 0.6 that all beat the bench: Brier 0.16, so Brier passes."""
    decisions: list[ScoredDecision] = []
    outcomes: list[DecisionOutcome] = []
    for i in range(40):
        decision = ScoredDecision(
            book_id=book_id,
            decided_on=sessions[i],
            isin=isin(i + 1),
            action=DecisionAction.PASS,
            horizon_sessions=5,
            p_beat_bench=Decimal("0.6"),
            edge_type="NONE",
        )
        decisions.append(decision)
        outcomes.append(
            DecisionOutcome(
                decision_key=decision.key,
                book_id=book_id,
                isin=decision.isin,
                decided_on=decision.decided_on,
                resolved_on=sessions[i + 5],
                sessions=5,
                name_return=Decimal("0.02"),
                bench_return=Decimal(0),
                reason=OutcomeReason.HORIZON,
            )
        )
    return decisions, outcomes


def _board(
    roster: Roster, paths: Mapping[str, str | Mapping[int, str]], *, sessions: int = WINDOW_SESSIONS
) -> Any:
    window = SESSIONS[:sessions]
    marks: list[BookMark] = []
    for book in roster.books:
        marks += _marks(book.id, book.opening_capital_inr, window, paths.get(book.id, "0"))
    decisions: list[ScoredDecision] = []
    outcomes: list[DecisionOutcome] = []
    for primary in roster.primaries:
        d, o = _calibrated(primary.id, window)
        decisions += d
        outcomes += o
    inputs = ScoreboardInputs(
        s0=window[0], marks=tuple(marks), decisions=tuple(decisions), outcomes=tuple(outcomes)
    )
    return build_scoreboard(roster, inputs)


def test_each_book_is_judged_against_its_own_control() -> None:
    roster = mini_roster("FM-SWING-BRK")
    # primary +5 vs its control +1: passes. The mirror +5 too, but its own control made +6.
    board = _board(
        roster,
        {
            "FM-SWING-BRK-10L": "5",
            "CTRL-FM-SWING-BRK-10L": "1",
            "FM-SWING-BRK-1CR": "5",
            "CTRL-FM-SWING-BRK-1CR": "6",
            "BENCH-N500": "2",
        },
    )
    primary, mirror = board.book_scores
    assert (primary.control_id, mirror.control_id) == (
        "CTRL-FM-SWING-BRK-10L",
        "CTRL-FM-SWING-BRK-1CR",
    )
    assert primary.primary.excess_vs_control_pp == Decimal("4.0000")  # +5 - +1, not +1 - +5
    assert mirror.primary.excess_vs_control_pp == Decimal("-1.0000")  # its own control, not 10L's
    assert primary.verdict is ScoreVerdict.PASS
    assert mirror.primary.verdict is ScoreVerdict.INCONCLUSIVE  # so it runs its extension
    (result,) = board.managers
    assert result.passed_on is BooksPassed.ONE
    assert (result.primary_verdict, result.mirror_verdict) == (
        ScoreVerdict.PASS,
        ScoreVerdict.IN_PROGRESS,
    )
    assert board.k_of_n == "1 of 2 books passed" and board.passed == 1


def test_brier_is_computed_once_per_manager_and_both_books_read_it() -> None:
    roster = mini_roster("FM-SWING-BRK")
    board = _board(roster, {"FM-SWING-BRK-10L": "5", "FM-SWING-BRK-1CR": "5"})
    primary, mirror = board.book_scores
    (result,) = board.managers
    assert result.decisions == 40, "each decision counted once, not once per book"
    assert primary.primary.brier == mirror.primary.brier == result.brier == Decimal("0.16000000")
    assert primary.primary.resolved_decisions == mirror.primary.resolved_decisions == 40
    assert result.passed_on is BooksPassed.BOTH and board.k_of_n == "2 of 2 books passed"


def test_a_decision_in_a_mirror_stream_is_refused_rather_than_double_counted() -> None:
    from analyst.fundmanager.scoreboard import ScoreboardError

    roster = mini_roster("FM-SWING-BRK")
    window = SESSIONS[:10]
    decisions, _ = _calibrated("FM-SWING-BRK-1CR", SESSIONS[:50])
    with pytest.raises(ScoreboardError, match="primary book only"):
        build_scoreboard(
            roster,
            ScoreboardInputs(s0=window[0], decisions=tuple(decisions[:1])),
        )


def _passing(*book_ids: str) -> dict[str, str]:
    """Every listed manager book +5 % over flat controls (and a flat-ish bench): each passes."""
    return {"BENCH-N500": "2", **dict.fromkeys(book_ids, "5")}


def test_the_graduation_floor_needs_two_managers_passing_on_their_primary_book() -> None:
    one = _board(ROSTER, _passing("FM-SWING-BRK-10L"))
    assert one.graduation.primary_passes == 1 and not one.graduation.met
    two = _board(ROSTER, _passing("FM-SWING-BRK-10L", "FM-POS-FUND-10L"))
    assert two.graduation.primary_passes == 2 and two.graduation.met
    assert two.k_of_n == "2 of 8 books passed"


def test_mirror_passes_never_count_toward_the_graduation_floor() -> None:
    mirrors = _board(ROSTER, _passing(*(m.id for m in ROSTER.mirrors)))
    assert mirrors.passed == 4 and mirrors.k_of_n == "4 of 8 books passed"
    assert mirrors.graduation.primary_passes == 0 and not mirrors.graduation.met
    # one manager on both books is still one manager on its primary
    both = _board(ROSTER, _passing("FM-SWING-EVT-10L", "FM-SWING-EVT-1CR"))
    assert both.graduation.primary_passes == 1 and not both.graduation.met
    by_manager = {r.manager_id: r.passed_on for r in both.managers}
    assert by_manager["FM-SWING-EVT"] is BooksPassed.BOTH
    assert by_manager["FM-SWING-BRK"] is BooksPassed.NEITHER


def test_one_manager_passing_across_the_extension_window_as_well_meets_the_floor() -> None:
    # +5 for all 126 sessions: passes the primary window and the rule over S0..S0+125 too
    held = _board(ROSTER, _passing("FM-POS-TREND-10L"), sessions=126)
    (trend,) = [s for s in held.book_scores if s.book_id == "FM-POS-TREND-10L"]
    assert trend.verdict is ScoreVerdict.PASS and trend.confirmation is not None
    assert trend.confirmation.complete and trend.confirmation.verdict is ScoreVerdict.PASS
    assert held.graduation.extension_confirmed == ("FM-POS-TREND",) and held.graduation.met
    # +5 through the primary window, then given back: passed at 63, not across the extension
    faded = _board(
        ROSTER,
        {"BENCH-N500": "2", "FM-POS-TREND-10L": {0: "5", WINDOW_SESSIONS: "0"}},
        sessions=126,
    )
    (trend,) = [s for s in faded.book_scores if s.book_id == "FM-POS-TREND-10L"]
    assert trend.verdict is ScoreVerdict.PASS
    assert trend.confirmation is not None and trend.confirmation.verdict is not ScoreVerdict.PASS
    assert faded.graduation.extension_confirmed == () and not faded.graduation.met
    # before S0 + 125 the extension cannot have confirmed anything yet
    early = _board(ROSTER, _passing("FM-POS-TREND-10L"), sessions=100)
    assert not early.graduation.met and early.graduation.primary_passes == 1
    # a mirror passing across the extension is not a primary book
    mirror = _board(ROSTER, _passing("FM-POS-TREND-1CR"), sessions=126)
    assert mirror.graduation.extension_confirmed == () and not mirror.graduation.met


def test_style_books_are_reported_and_never_decide_a_verdict() -> None:
    roster = mini_roster("FM-SWING-BRK")
    quiet = _board(roster, {"FM-SWING-BRK-10L": "5", "BENCH-N500": "2"})
    loud = _board(roster, {"FM-SWING-BRK-10L": "5", "STYLE-FM-SWING-BRK": "40", "BENCH-N500": "2"})
    assert [s.verdict for s in quiet.book_scores] == [s.verdict for s in loud.book_scores]
    assert quiet.graduation == loud.graduation and quiet.k_of_n == loud.k_of_n
    (style,) = loud.style_books
    assert style.style_id == "STYLE-FM-SWING-BRK" and style.primary_book == "FM-SWING-BRK-10L"
    assert style.style_return_pct == Decimal("40.0000")
    assert style.primary_excess_vs_style_pp == Decimal("-35.0000")


def test_the_scoreboard_names_every_book_of_the_full_roster() -> None:
    board = _board(ROSTER, {})
    assert [s.book_id for s in board.book_scores] == [b.id for b in ROSTER.manager_books]
    assert {s.role for s in board.book_scores} == {BookRole.PRIMARY.value, BookRole.MIRROR.value}
    assert [r.manager_id for r in board.managers] == list(ROSTER.manager_ids)
    assert [s.style_id for s in board.style_books] == [s.id for s in ROSTER.styles]
    assert {b.book_id for b in board.books} == {b.id for b in ROSTER.books}
