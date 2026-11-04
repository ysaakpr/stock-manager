"""Amendment 6 (2026-11-04): S5 compaction, digest and dossier de-duplication, the holding outside
the universe. The one schema and the template reorder are tested beside the rest of the session
in `test_fm_runtime.py`.

Each rule has a test that fails if its logic is inverted: every S5 name stays reachable, a digest
with a figure is never dropped, a twin field is hidden only when its value equals its twin's, and
a holding outside the universe gets a dossier.
"""

from __future__ import annotations

import dataclasses
import re
from datetime import timedelta
from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest

from analyst.commons import DigestBody, FilingDigest, FilingKind, build_dossiers
from analyst.commons.digests import DigestFigure
from analyst.commons.dossier import Dossier, DossierItem
from analyst.commons.screens import EventFact, EventKind, Screen
from analyst.fundmanager import render
from analyst.fundmanager.bundle import Holding, InvalidationStatus, ManagerBook, ResearchBundle
from analyst.fundmanager.render import (
    ALIAS_FIELDS,
    FO_FIELDS,
    TWIN_FIELDS,
    DeskOrder,
    DigestShow,
    digest_show,
    render_bundles,
    render_s5,
    shown_digests,
    shown_dossier_fields,
)
from analyst.fundmanager.runtime import RESEARCH_EVENT, SessionStatus, run_manager
from tests.unit.commons_screen_world import (
    BREAKOUT,
    LEADER,
    SESSION,
    FakeScreenSource,
    clock,
)
from tests.unit.commons_screen_world import screen_world as _screen_world
from tests.unit.test_fm_runtime import (
    HELD,
    NAV,
    ListJournal,
    ScriptedStub,
    StubFetcher,
    World,
    _commons,
    _mandate,
    final,
    hold,
    passed,
    research,
)
from tests.unit.test_fm_runtime import world as world

OUTSIDE = "INE229A01017"


# ── (a) S5: compact, ranked, bounded, every name reachable ───────────────────────────────────────


def _facts(world: World, n: int) -> tuple[EventFact, ...]:
    """``n`` synthetic S5 facts over ``n`` names, mixed kinds, two of them on a ranked screen."""
    ranked = [e.isin for s in Screen for e in world.screens.ranked(s)][:2]
    isins = ranked + [f"INE{k:06d}A01{k % 10}" for k in range(n - len(ranked))]
    kinds = (EventKind.BOARD_MEETING, EventKind.BULK_DEAL, EventKind.BLOCK_DEAL)
    out = []
    for k, isin in enumerate(isins):
        kind = kinds[k % 3] if k % 7 else EventKind.ANNOUNCEMENT
        out.append(
            EventFact(
                isin=isin,
                kind=kind,
                categories=("order_win",) if kind is EventKind.ANNOUNCEMENT else (kind.value,),
                knowable_date=SESSION - timedelta(days=k % 5),
                ref=None,
                subject="To consider and approve the financial results" if k % 3 == 0 else None,
                meeting_date=SESSION + timedelta(days=1 + k % 4)
                if kind is EventKind.BOARD_MEETING
                else None,
                client_name="SOME CLIENT LLP" if kind is not EventKind.BOARD_MEETING else None,
                side="BUY" if k % 2 else "SELL",
                quantity=1000 + k,
                price=Decimal("10.5"),
                pct_equity=None,
            )
        )
    return tuple(out)


@pytest.mark.parametrize("cap", [0, 1, 40])
def test_every_s5_name_stays_reachable_however_few_are_ranked(
    world: World, monkeypatch: pytest.MonkeyPatch, cap: int
) -> None:
    monkeypatch.setattr(render, "S5_RANKED_MAX", cap)
    facts = _facts(world, 120)
    screens = world.screens.model_copy(update={"s5": facts})
    text = "\n".join(
        render_s5(screens, {}, DeskOrder("FM-SWING-EVT", SESSION), shortlist=world.shortlist)
    )
    for fact in facts:
        assert fact.isin in text
        day = fact.meeting_date or fact.knowable_date
        assert any(fact.isin in line and str(day) in line for line in text.splitlines()) or (
            # an ISIN listed by event: its date heads the group it is in
            str(day) in text
        )
    # Bounded: at most `cap` names get a line of their own, and only names with a membership.
    own = [line for line in text.splitlines() if " · on " in line]
    assert len(own) <= cap
    assert all(("on S" in line) or ("shortlist" in line) for line in own)
    # The boilerplate purpose and the deal's client are left to the dossier.
    assert "SOME CLIENT LLP" not in text and "approve the financial results" not in text


def test_s5_is_far_smaller_than_one_line_per_fact(world: World) -> None:
    facts = _facts(world, 300)
    screens = world.screens.model_copy(update={"s5": facts})
    compact = "\n".join(render_s5(screens, {}, DeskOrder("FM-SWING-EVT", SESSION)))
    per_fact = sum(
        len(f"- {f.isin} {f.kind.value} {f.subject or ''} {f.client_name}") for f in facts
    )
    assert len(compact) < per_fact / 2


def test_s5_ranks_names_with_more_memberships_first(world: World) -> None:
    ranked = list(world.screens.ranked(Screen.S1))
    assert ranked, "the test world has an S1"
    on_s1 = ranked[0].isin
    facts = _facts(world, 10)
    facts = (*facts, facts[0].model_copy(update={"isin": on_s1}))
    screens = world.screens.model_copy(update={"s5": facts})
    held = frozenset({on_s1})
    text = render_s5(screens, {}, DeskOrder("M", SESSION), shortlist=world.shortlist, holdings=held)
    own = [line for line in text if " · on " in line]
    assert own and on_s1 in own[0] and "your book" in own[0]


# ── (b) digests ───────────────────────────────────────────────────────────────────────────────


def _dossier(world: World, *, ann_text: str, ref: str = "777") -> Dossier:
    (d,) = build_dossiers(
        [LEADER],
        screens=world.screens,
        sheets=world.sheets,
        source=FakeScreenSource(_screen_world()),
    )
    item = DossierItem(field_id=f"{LEADER}.ann_{ref}", knowable_date=SESSION, text=ann_text)
    return d.model_copy(update={"announcements": (item,)})


def _ann_digest(
    *,
    headline: str,
    disclosed: str,
    figures: tuple[DigestFigure, ...] = (),
    ref: str = "777",
    kind: FilingKind = FilingKind.ANNOUNCEMENT,
) -> FilingDigest:
    return FilingDigest(
        filing_id=f"nse_announcements:{ref}",
        kind=kind,
        isin=LEADER,
        knowable_date=SESSION,
        trading_date=SESSION,
        digest_version="commons-digest/2",
        input_digest="a" * 64,
        input_truncated=False,
        prompt_digest="b" * 64,
        provider="stub",
        model="claude-sonnet-5-5",
        body=DigestBody(headline=headline, disclosed=disclosed, figures=figures),
        input_tokens=1,
        output_tokens=1,
        cache_write_tokens=0,
        cache_read_tokens=0,
        digested_at=clock().now(),
    )


LINE = "Trading Window | Leader Ltd has informed the Exchange regarding the Trading Window closure"
NO_DETAIL = (
    "Leader Ltd informed the Exchange of a trading window closure. "
    "The text gives no further detail."
)


def test_a_digest_saying_no_further_detail_beside_its_dossier_line_is_dropped(world: World) -> None:
    d = _dossier(world, ann_text=LINE)
    g = _ann_digest(
        headline="Leader Ltd informs Exchange of trading window closure", disclosed=NO_DETAIL
    )
    assert digest_show(g, {LEADER: d}) is DigestShow.DROPPED
    # Without the dossier line it is the only mention: cut to its headline, never dropped.
    assert digest_show(g, {}) is DigestShow.HEADLINE


def test_a_digest_with_a_figure_is_always_shown_in_full(world: World) -> None:
    d = _dossier(world, ann_text=LINE)
    figure = (DigestFigure(label="Order value", value="120", unit="crore"),)
    g = _ann_digest(headline="Order win", disclosed=NO_DETAIL, figures=figure)
    assert digest_show(g, {LEADER: d}) is DigestShow.FULL
    results = _ann_digest(headline="Q2 results", disclosed=NO_DETAIL, kind=FilingKind.RESULTS)
    assert digest_show(results, {LEADER: d}) is DigestShow.FULL


def test_a_digest_that_only_restates_its_line_is_dropped_and_one_adding_a_fact_is_kept(
    world: World,
) -> None:
    d = _dossier(world, ann_text=LINE)
    restates = _ann_digest(
        headline="Leader Ltd: trading window closure",
        disclosed="Leader Ltd has informed the Exchange regarding the trading window closure.",
    )
    assert digest_show(restates, {LEADER: d}) is DigestShow.DROPPED
    adds_number = _ann_digest(
        headline="Leader Ltd: trading window closure",
        disclosed="The window closes from 1 October 2026 for 48 hours after the results.",
    )
    assert digest_show(adds_number, {LEADER: d}) is DigestShow.FULL
    adds_fact = _ann_digest(
        headline="Leader Ltd appoints Deloitte as statutory auditor",
        disclosed="Leader Ltd informed the Exchange of an auditor appointment.",
    )
    assert digest_show(adds_fact, {LEADER: d}) is DigestShow.FULL


def test_a_digest_shown_in_full_turns_its_dossier_line_into_a_pointer_and_a_dropped_one_vanishes(
    world: World,
) -> None:
    d = _dossier(world, ann_text=LINE, ref="777")
    kept = _ann_digest(
        headline="Order win",
        disclosed="An order.",
        figures=(DigestFigure(label="Order value", value="120", unit="crore"),),
        ref="777",
    )
    bundle = ResearchBundle(0, (), (), (d,), (kept,), (), ())
    text = render_bundles([bundle])
    assert (
        f"[F:{LEADER}.ann_777] knowable {SESSION}: digested below as [F:nse_announcements:777]"
        in text
    )
    assert LINE not in text and text.count("[F:nse_announcements:777]") == 2
    dropped = _ann_digest(
        headline="Leader Ltd informs Exchange of trading window closure", disclosed=NO_DETAIL
    )
    bundle = ResearchBundle(0, (), (), (d,), (dropped,), (), ())
    text = render_bundles([bundle])
    assert LINE in text and "nse_announcements:777" not in text
    assert shown_digests(bundle) == ()


def test_a_dropped_digest_is_not_citable_and_is_journaled_as_not_shown(
    world: World, tmp_path: Path
) -> None:
    """The shown-citation set follows the rendering: the runtime cites only shown digests."""
    from analyst.commons import OnDemandDigests

    (d,) = build_dossiers(
        [LEADER],
        screens=world.screens,
        sheets=world.sheets,
        source=FakeScreenSource(_screen_world()),
    )
    line = d.announcements[0] if d.announcements else None
    assert line is not None, "the fake lake lists an announcement for the leader"
    ref = line.field_id.split(".ann_", 1)[1]
    silent = _ann_digest(headline=line.text[:60], disclosed=NO_DETAIL, ref=ref)

    def on_demand(isins: Any) -> OnDemandDigests:
        return OnDemandDigests(
            trading_date=SESSION,
            isins=tuple(isins),
            since=SESSION,
            digests=(silent,) if LEADER in isins else (),
            digested=(),
            cached=(),
            failures=(),
            gaps=(),
        )

    commons = dataclasses.replace(_commons(world, tmp_path), digests=on_demand)
    cites = passed(LEADER, evidence_refs=[f"[F:{silent.filing_id}]"])
    journal = ListJournal()
    llm = ScriptedStub([research([LEADER]), research(), final(hold(), cites)])
    result = run_manager(
        _mandate(),
        SESSION,
        commons,
        _book_with(),
        llm,
        StubFetcher(),
        journal=journal,
        clock=clock(),
    )
    fulfilled = journal.events(RESEARCH_EVENT)[0].payload
    assert silent.filing_id in fulfilled["digests_not_shown"]
    assert silent.filing_id not in fulfilled["digests"]
    assert silent.filing_id not in llm.prompts[-1]
    assert LEADER in {v.decision.isin for v in result.refused}


# ── (c) dossier fields ───────────────────────────────────────────────────────────────────────────


def _leader(world: World) -> Dossier:
    (d,) = build_dossiers(
        [LEADER],
        screens=world.screens,
        sheets=world.sheets,
        source=FakeScreenSource(_screen_world()),
    )
    return d


def test_aliases_are_never_shown_and_still_resolve(world: World) -> None:
    d = _leader(world)
    shown = shown_dossier_fields(d)
    for alias, twin in ALIAS_FIELDS.items():
        assert alias not in shown and twin in shown
        assert d.resolve(d.field_id(alias)) == d.resolve(d.field_id(twin))


def test_a_twin_is_hidden_only_where_it_equals_its_twin(world: World) -> None:
    d = _leader(world)
    same = dict(d.fields, ret_5=Decimal("0.10248586"), return_1w=Decimal("0.102486"))
    differ = dict(same, ret_5=Decimal("0.11"))
    assert "ret_5" not in shown_dossier_fields(d.model_copy(update={"fields": same}))
    assert "ret_5" in shown_dossier_fields(d.model_copy(update={"fields": differ}))
    dates = dict(d.fields, results_day0=SESSION, results_day=SESSION + timedelta(days=1))
    assert "results_day0" in shown_dossier_fields(d.model_copy(update={"fields": dates}))
    unknown = dict(d.fields, filing_date=None, results_filing_date=None)
    assert "filing_date" in shown_dossier_fields(d.model_copy(update={"fields": unknown}))


def test_fo_lines_are_hidden_only_for_a_name_that_is_not_fo_listed(world: World) -> None:
    d = _leader(world)
    for listed, hidden in ((False, True), (True, False), (None, False)):
        shown = shown_dossier_fields(
            d.model_copy(update={"fields": dict(d.fields, fo_listed=listed)})
        )
        assert all((name not in shown) is hidden for name in FO_FIELDS)
        assert "fo_listed" in shown


def test_no_field_a_contract_check_or_decision_rule_reads_is_ever_hidden(world: World) -> None:
    """`contract` reads close and atr14_pct; stops and invalidations are written against sma20,
    sma50, high_20, high_252, volume_median_50; the playbooks name return_1w and the delivery
    fields. None is an alias or a twin, so none can be hidden."""
    read = {
        "close",
        "atr14_pct",
        "atr14",
        "sma20",
        "sma50",
        "sma200",
        "high_20",
        "high_252",
        "volume_median_50",
        "return_1w",
        "return_4w",
        "deliv_pct",
        "next_results_date",
        "last_results_date",
        "results_day",
        "volume_ratio_day0",
        "results_day_volume_ratio",
    }
    assert not read & (set(ALIAS_FIELDS) | set(TWIN_FIELDS) | set(FO_FIELDS))
    assert read <= set(shown_dossier_fields(_leader(world)))


# ── (e) a holding outside the universe ───────────────────────────────────────────────────────────


def _book_with(*extra: str) -> ManagerBook:
    holdings = [
        Holding(
            isin=isin,
            sector=None,
            weight_pct=Decimal("2.28"),
            sessions_held=8,
            opening_thesis="An event drift.",
            invalidation=(InvalidationStatus("close below the gap", "not judged"),),
            stop_price=Decimal("399.98"),
        )
        for isin in (HELD, *extra)
    ]
    return ManagerBook("FM-SWING-BRK-10L", NAV, Decimal("90.00"), tuple(holdings))


def test_a_holding_outside_the_universe_gets_a_dossier_and_is_decided(
    world: World, tmp_path: Path
) -> None:
    assert OUTSIDE not in {r.isin for r in world.sheets.universe}
    journal = ListJournal()
    llm = ScriptedStub([research(), final(hold(), hold(OUTSIDE))])
    result = run_manager(
        _mandate(),
        SESSION,
        _commons(world, tmp_path),
        _book_with(OUTSIDE),
        llm,
        StubFetcher(),
        journal=journal,
        clock=clock(),
    )
    assert result.status is SessionStatus.DECIDED and result.repairs == 0
    assert OUTSIDE in {v.decision.isin for v in result.accepted}
    fulfilled = journal.events(RESEARCH_EVENT)[0].payload
    assert f"{OUTSIDE}:" in fulfilled["dossiers"]
    assert "no dossier" not in fulfilled["unfulfilled"]
    final_prompt = llm.prompts[-1]
    assert re.search(rf"#### Dossier {OUTSIDE} .*\nHOLDING OUTSIDE TODAY'S UNIVERSE", final_prompt)
    assert "Every holding needs a decision, including one outside today's universe" in final_prompt


def test_without_the_outside_dossier_the_holding_is_left_without_one(
    world: World, tmp_path: Path
) -> None:
    """The inverted case: no outside provider, and the name is unfulfilled as before."""
    commons = dataclasses.replace(_commons(world, tmp_path), outside_dossiers=None)
    journal = ListJournal()
    llm = ScriptedStub([research(), final(hold(), hold(OUTSIDE))])
    run_manager(
        _mandate(),
        SESSION,
        commons,
        _book_with(OUTSIDE),
        llm,
        StubFetcher(),
        journal=journal,
        clock=clock(),
    )
    fulfilled = journal.events(RESEARCH_EVENT)[0].payload
    assert f"{OUTSIDE}:" not in fulfilled["dossiers"]
    assert "not in today's universe; no dossier" in fulfilled["unfulfilled"]


def test_an_outside_dossier_refuses_a_name_inside_the_universe(world: World) -> None:
    with pytest.raises(ValueError, match="not an outside dossier"):
        build_dossiers(
            [LEADER],
            screens=world.screens,
            sheets=world.sheets,
            source=FakeScreenSource(_screen_world()),
            outside_universe=True,
        )
    (d,) = build_dossiers(
        [OUTSIDE],
        screens=world.screens,
        sheets=world.sheets,
        source=FakeScreenSource(_screen_world()),
        outside_universe=True,
    )
    assert d.fields["close"] is None and any(g.source == "universe" for g in d.gaps)
    assert BREAKOUT  # imported for symmetry with the runtime tests
