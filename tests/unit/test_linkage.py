"""M6.2 — news → holding linkage and the break-condition matcher.

Three acceptance criteria are proved here:

1. the labelled 60-item sample yields measured precision/recall (recorded in `linkage.py`'s
   docstring and re-asserted below to stay honest);
2. an integrity-class keyword hit (auditor resignation, fraud, promoter pledge) escalates
   immediately per §5.3 BC3 — `Urgency.IMMEDIATE`, while a fundamental/structural hit is `ROUTINE`;
3. alias handling covers renamed companies (former-name links) and dual-listed ones (a BSE-only
   ticker and an NSE-only ticker both resolve to the same ISIN).

Everything is offline: the identity master and alias table are built in-memory, and the news rows
are constructed from the labelled fixture. No network, no Postgres.
"""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import yaml

from analyst.monitor.linkage import (
    AliasTable,
    MatchKind,
    NameResolver,
    load_aliases,
)
from analyst.monitor.matcher import (
    BreakConditionWatch,
    Urgency,
    default_integrity_watch,
    match_links,
)
from analyst.thesis import BreakConditionType
from dataplatform.identity.master import (
    Exchange,
    IdentityMaster,
    ListingStatus,
    Security,
    SymbolWindow,
)
from dataplatform.ingest.news import NewsRow
from dataplatform.query.announcement_search import KeywordQuery

_FIXTURE = Path(__file__).parents[1] / "fixtures" / "news_linkage" / "labelled_sample.yaml"

# The held universe the sample is labelled against.
INFOSYS = "INE009A01021"
EICHER = "INE066A01021"
LTIM = "INE214T01019"
RELIANCE = "INE002A01018"
TCS = "INE467B01029"
ADANI = "INE423A01024"

_SNAPSHOT = datetime(2026, 9, 1, tzinfo=UTC).date()


def _security(isin: str, name: str, exchange: Exchange = Exchange.NSE) -> Security:
    return Security(
        isin=isin,
        name=name,
        primary_exchange=exchange,
        status=ListingStatus.ACTIVE,
        first_seen_date=_SNAPSHOT,
    )


def _window(isin: str, exchange: Exchange, symbol: str) -> SymbolWindow:
    from datetime import date

    return SymbolWindow(
        exchange=exchange,
        symbol=symbol,
        valid_from=date(2000, 1, 1),
        valid_to=None,
        isin=isin,
    )


def _master() -> IdentityMaster:
    """The six-name held universe, each dual-listed NSE + BSE (distinct symbols where needed)."""
    securities = (
        _security(INFOSYS, "Infosys Limited"),
        _security(EICHER, "Eicher Motors Limited"),
        _security(LTIM, "LTIMindtree Limited"),
        _security(RELIANCE, "Reliance Industries Limited"),
        _security(TCS, "Tata Consultancy Services Limited"),
        _security(ADANI, "Adani Enterprises Limited"),
    )
    windows = (
        _window(INFOSYS, Exchange.NSE, "INFY"),
        _window(INFOSYS, Exchange.BSE, "INFY"),
        _window(EICHER, Exchange.NSE, "EICHERMOT"),
        # A distinct BSE symbol — the dual-listing case the resolver must draw from both exchanges.
        _window(EICHER, Exchange.BSE, "EICHER"),
        _window(LTIM, Exchange.NSE, "LTIM"),
        _window(LTIM, Exchange.BSE, "540005"),
        _window(RELIANCE, Exchange.NSE, "RELIANCE"),
        _window(RELIANCE, Exchange.BSE, "500325"),
        _window(TCS, Exchange.NSE, "TCS"),
        _window(TCS, Exchange.BSE, "532540"),
        _window(ADANI, Exchange.NSE, "ADANIENT"),
        _window(ADANI, Exchange.BSE, "512599"),
    )
    return IdentityMaster(windows, securities=securities)


def _aliases() -> AliasTable:
    return load_aliases()


def _resolver() -> NameResolver:
    return NameResolver.build((INFOSYS, EICHER, LTIM, RELIANCE, TCS, ADANI), _master(), _aliases())


def _row(item: dict[str, Any]) -> NewsRow:
    return NewsRow(
        ts=datetime(2026, 9, 1, 10, 0, tzinfo=UTC),
        source=item["source"],
        title=item.get("title"),
        url=f"https://example.test/{item['id']}",
        entities=tuple(item.get("entities", ()) or ()),
    )


def _labelled() -> list[dict[str, Any]]:
    with _FIXTURE.open(encoding="utf-8") as handle:
        loaded = yaml.safe_load(handle)
    return list(loaded["items"])


# ── acceptance 1: measured precision/recall on a 50+ item labelled sample ────────────────────────


def test_sample_is_large_enough_and_balanced() -> None:
    """The sample is 50+ items, with both real links and distractors — otherwise the numbers lie."""
    items = _labelled()
    assert len(items) >= 50
    linkable = [it for it in items if it["gold"]]
    distractors = [it for it in items if not it["gold"]]
    assert len(linkable) >= 25
    assert len(distractors) >= 10  # precision is meaningless without negatives to get wrong


def test_linkage_precision_and_recall() -> None:
    """Precision/recall over (item, ISIN) pairs meet the recorded, precision-first thresholds.

    Precision must be near-perfect (a false T0 link buys an expensive T1 review of the wrong name);
    recall is allowed to trail — the deliberate cost of the whole-word, curated-alias matcher.
    """
    resolver = _resolver()
    items = _labelled()

    true_positives = 0
    false_positives = 0
    false_negatives = 0
    for item in items:
        gold = set(item["gold"])
        predicted = {link.isin for link in resolver.link(_row(item))}
        true_positives += len(gold & predicted)
        false_positives += len(predicted - gold)
        false_negatives += len(gold - predicted)

    precision = true_positives / (true_positives + false_positives)
    recall = true_positives / (true_positives + false_negatives)

    # The numbers recorded in linkage.py's docstring. Kept as assertions so a regression that makes
    # the matcher noisier (precision) or blinder (recall) fails the task rather than pass quietly.
    assert precision >= 0.95, f"precision {precision:.3f} below floor"
    assert recall >= 0.85, f"recall {recall:.3f} below floor"
    # Precision-first: precision is at least as high as recall on this sample.
    assert precision >= recall


def test_no_false_positive_on_any_distractor() -> None:
    """Every distractor (empty gold) links to nothing — each probes a specific precision guard."""
    resolver = _resolver()
    for item in _labelled():
        if item["gold"]:
            continue
        links = resolver.link(_row(item))
        assert links == (), f"{item['id']!r} ({item['title']!r}) should not link, got {links}"


# ── acceptance 2: integrity-class hit escalates immediately (§5.3 BC3) ────────────────────────────


def _watches() -> dict[str, list[BreakConditionWatch]]:
    """Every holding carries the universal integrity watch (BC3) plus a thesis-specific one."""
    structural = BreakConditionWatch(
        break_condition_id="BC2",
        condition_type=BreakConditionType.STRUCTURAL,
        query=KeywordQuery(all_of=("demerge",)),
    )
    return {
        isin: [default_integrity_watch(), structural]
        for isin in (INFOSYS, EICHER, LTIM, RELIANCE, TCS, ADANI)
    }


def test_integrity_keyword_escalates_immediately() -> None:
    """An auditor-resignation headline on a held name is an IMMEDIATE escalation (BC3, §5.6)."""
    resolver = _resolver()
    row = _row(
        {"id": "t_audit", "source": "rss_bs", "title": "Auditor resigns at Infosys amid probe"}
    )
    links = resolver.link(row)
    assert {link.isin for link in links} == {INFOSYS}

    matches = match_links(links, _watches())
    integrity = [m for m in matches if m.break_condition_id == "BC3"]
    assert len(integrity) == 1
    assert integrity[0].condition_type is BreakConditionType.INTEGRITY
    assert integrity[0].urgency is Urgency.IMMEDIATE
    assert integrity[0].escalates_immediately

    flag = integrity[0].to_t0_flag(case_id="CASE-1")
    assert flag.isin == INFOSYS
    assert flag.detail["urgency"] == "IMMEDIATE"
    assert flag.break_condition_id == "BC3"


def test_fraud_and_pledge_also_escalate_immediately() -> None:
    """Fraud and promoter-pledge hits are integrity-class too — all three §5.3 BC3 triggers."""
    resolver = _resolver()
    watches = _watches()

    fraud = _row(
        {
            "id": "t_fraud",
            "source": "rss_bs",
            "title": "Fraud allegations resurface against Adani Enterprises",
        }
    )
    pledge = _row(
        {
            "id": "t_pledge",
            "source": "rss_bs",
            "title": "Promoter pledge raised at Eicher Motors, filing shows",
        }
    )
    for row, isin in ((fraud, ADANI), (pledge, EICHER)):
        matches = match_links(resolver.link(row), watches)
        immediate = [m for m in matches if m.escalates_immediately]
        assert len(immediate) == 1
        assert immediate[0].link.isin == isin
        assert immediate[0].condition_type is BreakConditionType.INTEGRITY


def test_structural_hit_is_routine_not_immediate() -> None:
    """A demerge (structural) escalates, but on the normal T1 cadence — not immediately."""
    resolver = _resolver()
    row = _row(
        {
            "id": "t_demerge",
            "source": "rss_bs",
            "title": "Reliance Industries to demerge financial services arm",
        }
    )
    matches = match_links(resolver.link(row), _watches())
    assert [m.urgency for m in matches] == [Urgency.ROUTINE]
    assert matches[0].condition_type is BreakConditionType.STRUCTURAL
    assert not matches[0].escalates_immediately


def test_integrity_keyword_without_a_held_name_does_not_escalate() -> None:
    """An auditor-resignation headline about no held name links to nothing and escalates nothing.

    Precision at the escalation boundary: the keyword alone is not the trigger — it must land on a
    holding. This is why d06 ("India Inc sees auditor resignations rise") is a distractor.
    """
    resolver = _resolver()
    row = _row(
        {
            "id": "t_generic",
            "source": "rss_bs",
            "title": "India Inc sees auditor resignations rise this quarter",
        }
    )
    links = resolver.link(row)
    assert links == ()
    assert match_links(links, _watches()) == ()


# ── acceptance 3: alias handling — renamed and dual-listed companies ──────────────────────────────


def test_renamed_company_links_via_former_name() -> None:
    """A headline using a company's former/merged name links to its current ISIN (rename case)."""
    resolver = _resolver()

    former = _row(
        {
            "id": "t_lti",
            "source": "rss_bs",
            "title": "Larsen & Toubro Infotech legacy contracts migrate over",
        }
    )
    links = resolver.link(former)
    assert {link.isin for link in links} == {LTIM}
    assert links[0].kind is MatchKind.FORMER_NAME

    mindtree = _row({"id": "t_mt", "source": "rss_bs", "title": "Mindtree co-founder steps back"})
    assert {link.isin for link in resolver.link(mindtree)} == {LTIM}

    infosys_old = _row(
        {"id": "t_it", "source": "rss_bs", "title": "Infosys Technologies alumni fund launched"}
    )
    assert {link.isin for link in resolver.link(infosys_old)} == {INFOSYS}


def test_former_parent_name_does_not_link_to_the_subsidiary() -> None:
    """'Larsen & Toubro' (the parent) is not 'Larsen & Toubro Infotech' (the former name)."""
    resolver = _resolver()
    row = _row(
        {
            "id": "t_lt",
            "source": "rss_bs",
            "title": "Larsen & Toubro wins a large infrastructure order",
        }
    )
    assert resolver.link(row) == ()


def test_dual_listed_company_resolves_from_either_exchange_symbol() -> None:
    """A BSE-only ticker and an NSE-only ticker for one ISIN both resolve to that ISIN."""
    resolver = _resolver()

    bse = _row(
        {"id": "t_bse", "source": "rss_bs", "title": "On the BSE, EICHER climbs 3% intraday"}
    )
    bse_links = resolver.link(bse)
    assert {link.isin for link in bse_links} == {EICHER}
    assert bse_links[0].kind is MatchKind.TICKER

    nse = _row({"id": "t_nse", "source": "rss_bs", "title": "EICHERMOT hits a fresh 52-week high"})
    assert {link.isin for link in resolver.link(nse)} == {EICHER}


def test_curated_short_form_below_derived_floor_still_links() -> None:
    """'TCS' (length 3, below the derived-ticker floor) links because the alias table vets it."""
    resolver = _resolver()
    row = _row({"id": "t_tcs", "source": "rss_bs", "title": "TCS bags a multi-year deal"})
    links = resolver.link(row)
    assert {link.isin for link in links} == {TCS}
    assert links[0].kind is MatchKind.ALIAS


def test_whole_word_anchor_rejects_substring_hits() -> None:
    """'Infosystems' does not link to Infosys — the matcher is whole-word, not substring."""
    resolver = _resolver()
    row = _row(
        {"id": "t_sub", "source": "rss_bs", "title": "Infosystems Global unveils a refreshed logo"}
    )
    assert resolver.link(row) == ()


def test_ordinary_word_in_a_name_does_not_link() -> None:
    """'reliance' the ordinary word never links; only the phrase 'Reliance Industries' does."""
    resolver = _resolver()
    ordinary = _row(
        {
            "id": "t_word",
            "source": "rss_bs",
            "title": "Reliance on imported coal falls to a decade low",
        }
    )
    assert resolver.link(ordinary) == ()
    real = _row(
        {"id": "t_ril", "source": "rss_bs", "title": "Reliance Industries commissions a unit"}
    )
    assert {link.isin for link in resolver.link(real)} == {RELIANCE}


def test_ambiguous_form_shared_by_two_holdings_is_dropped() -> None:
    """A surface form claimed by two held ISINs links to neither — ambiguity is never a guess."""
    # Two securities sharing the alias-worthy name "ACME" — the resolver must drop it, not pick one.
    from datetime import date

    master = IdentityMaster(
        (
            SymbolWindow(
                exchange=Exchange.NSE,
                symbol="ACMEONE",
                valid_from=date(2000, 1, 1),
                valid_to=None,
                isin="INE111A01011",
            ),
            SymbolWindow(
                exchange=Exchange.NSE,
                symbol="ACMEONE",
                valid_from=date(2000, 1, 1),
                valid_to=None,
                isin="INE222A01012",
            ),
        ),
        securities=(
            _security("INE111A01011", "Acme Foods Limited"),
            _security("INE222A01012", "Acme Foods Limited"),
        ),
    )
    resolver = NameResolver.build(
        ("INE111A01011", "INE222A01012"), master, AliasTable(version=1, aliases=())
    )
    row = _row({"id": "t_amb", "source": "rss_bs", "title": "Acme Foods Limited posts results"})
    assert resolver.link(row) == ()
