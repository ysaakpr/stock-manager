"""M17.0 / M17.14: the roster is pre-registration §2/§3 as §8 Amendment 2 replaces them, refuses
bad input, and hashes stably.

The expected values below are transcribed from
`ops/studies/preregistration-m17-ai-fund-managers-2026-10-09.md` (§2, §3 and Amendment 2 a, b, d),
not from `roster.yaml`: if the YAML drifts from the ratified document, these fail.
"""

from __future__ import annotations

import copy
import os
import subprocess
import sys
from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest
import yaml

from analyst.fundmanager import (
    ROSTER_PATH,
    BenchMandate,
    BookKind,
    BookRole,
    ControlMandate,
    HorizonStyle,
    ManagerMandate,
    ManagerStyle,
    RebalanceUnit,
    RosterError,
    StyleMandate,
    load_roster,
    mandate_hash,
    parse_roster,
)

REPO = Path(__file__).resolve().parents[2]
PREREG = "ops/studies/preregistration-m17-ai-fund-managers-2026-10-09.md"

LAKH = Decimal("100000")
CRORE = Decimal("10000000")

# manager: (style, family, horizon band, starting screens) — Amendment 2 (a).
MANAGERS = {
    "FM-SWING-BRK": (ManagerStyle.SWING_BREAKOUT, HorizonStyle.SWING, (5, 20), ("S2", "S3")),
    "FM-SWING-EVT": (ManagerStyle.SWING_EVENT, HorizonStyle.SWING, (5, 20), ("S4", "S5")),
    "FM-POS-TREND": (ManagerStyle.POSITIONAL_TREND, HorizonStyle.POSITIONAL, (20, 60), ("S1",)),
    "FM-POS-FUND": (
        ManagerStyle.POSITIONAL_FUNDAMENTAL,
        HorizonStyle.POSITIONAL,
        (20, 60),
        ("S4",),
    ),
}
# book suffix: (role, capital) — Amendment 2 (b): both books hold at most 15 positions.
BOOKS = {"10L": (BookRole.PRIMARY, 10 * LAKH), "1CR": (BookRole.MIRROR, CRORE)}
MANAGER_BOOKS = [f"{m}-{suffix}" for m in MANAGERS for suffix in BOOKS]


def _raw() -> dict[str, Any]:
    raw = yaml.safe_load(ROSTER_PATH.read_text(encoding="utf-8"))
    assert isinstance(raw, dict)
    return copy.deepcopy(raw)


def _book(raw: dict[str, Any], book_id: str) -> dict[str, Any]:
    [book] = [b for b in raw["books"] if b["id"] == book_id]
    assert isinstance(book, dict)
    return book


def _parse(raw: dict[str, Any]) -> None:
    parse_roster(yaml.safe_dump(raw, sort_keys=False))


# ── the roster is the pre-registration ───────────────────────────────────────────────────────────


def test_roster_has_exactly_the_preregistered_books() -> None:
    roster = load_roster()
    assert roster.preregistration == PREREG
    assert (REPO / PREREG).is_file()
    assert roster.manager_ids == tuple(MANAGERS)
    assert [m.id for m in roster.manager_books] == MANAGER_BOOKS
    assert [m.id for m in roster.primaries] == [f"{m}-10L" for m in MANAGERS]
    assert [m.id for m in roster.mirrors] == [f"{m}-1CR" for m in MANAGERS]
    assert [c.id for c in roster.controls] == [f"CTRL-{b}" for b in MANAGER_BOOKS]
    assert [s.id for s in roster.styles] == [f"STYLE-{m}" for m in MANAGERS]
    assert [b.id for b in roster.benches] == ["BENCH-N500"]
    assert len(roster.books) == 8 + 8 + 4 + 1


@pytest.mark.parametrize("book_id", MANAGER_BOOKS)
def test_each_manager_book_matches_amendment_2(book_id: str) -> None:
    manager_id, suffix = book_id.rsplit("-", 1)
    style, family, (lo, hi), screens = MANAGERS[manager_id]
    role, capital = BOOKS[suffix]
    manager = load_roster().get(book_id)
    assert isinstance(manager, ManagerMandate)
    assert manager.kind is BookKind.MANAGER
    assert manager.manager == manager_id
    assert manager.role is role
    assert manager.style is style
    assert manager.family is family
    assert manager.starting_screens == screens
    assert (manager.horizon.min_sessions, manager.horizon.max_sessions) == (lo, hi)
    assert manager.opening_capital_inr == capital
    assert type(manager.opening_capital_inr) is Decimal
    assert manager.max_positions == 15
    assert manager.max_position_pct == Decimal("10")
    assert manager.max_sector_pct == Decimal("30")
    universe = manager.universe
    assert universe.series == "EQ"
    assert universe.min_median_traded_value_inr == CRORE  # ₹1 cr/day
    assert universe.median_lookback_sessions == 20
    assert set(universe.excluded_surveillance) == {"GSM", "ESM"}
    assert universe.flagged_surveillance == ("ASM",)
    assert manager.models.decision == "claude-opus-5-5"
    assert manager.models.digest == "claude-sonnet-5-5"
    assert manager.prompt_path == "analyst/fundmanager/prompts/manager.md"
    assert (manager.rounds.max_isins, manager.rounds.max_queries) == (12, 8)
    assert manager.rounds.max_research_rounds == 2
    assert manager.rounds.max_calls == 4  # §4: at most 4 model calls per session


def test_every_style_names_a_playbook_in_the_prompt() -> None:
    from analyst.fundmanager.render import PromptTemplate

    assert set(PromptTemplate.load().keys("STYLE")) == {s.value for s in ManagerStyle}


@pytest.mark.parametrize("book_id", MANAGER_BOOKS)
def test_each_control_matches_section_3(book_id: str) -> None:
    roster = load_roster()
    manager = roster.get(book_id)
    control = roster.control_for(book_id)
    assert isinstance(manager, ManagerMandate)
    assert isinstance(control, ControlMandate)
    assert control.id == f"CTRL-{book_id}"
    assert control.shortlist_top_n == manager.max_positions == 15
    assert control.opening_capital_inr == manager.opening_capital_inr
    assert control.max_position_pct == manager.max_position_pct
    assert control.universe == manager.universe
    if manager.family is HorizonStyle.SWING:
        assert (control.rebalance.unit, control.rebalance.every) == (RebalanceUnit.WEEKLY, 1)
    else:
        assert (control.rebalance.unit, control.rebalance.every) == (RebalanceUnit.SESSIONS, 21)


@pytest.mark.parametrize("manager_id", list(MANAGERS))
def test_each_style_book_matches_amendment_2d(manager_id: str) -> None:
    roster = load_roster()
    style = roster.style_for(manager_id)
    primary = roster.primary_of(manager_id)
    assert isinstance(style, StyleMandate)
    assert style.kind is BookKind.STYLE
    assert style.id == f"STYLE-{manager_id}"
    assert style.opening_capital_inr == 10 * LAKH
    assert style.max_names == style.max_positions == 15
    assert style.universe == primary.universe
    if primary.family is HorizonStyle.SWING:
        assert (style.rebalance.unit, style.rebalance.every) == (RebalanceUnit.WEEKLY, 1)
    else:
        assert (style.rebalance.unit, style.rebalance.every) == (RebalanceUnit.SESSIONS, 21)


def test_a_managers_books_are_found_by_its_id() -> None:
    roster = load_roster()
    assert roster.primary_of("FM-POS-FUND").id == "FM-POS-FUND-10L"
    assert roster.mirror_of("FM-POS-FUND").id == "FM-POS-FUND-1CR"
    assert [b.id for b in roster.books_of("FM-SWING-EVT")] == [
        "FM-SWING-EVT-10L",
        "FM-SWING-EVT-1CR",
    ]
    with pytest.raises(KeyError):
        roster.primary_of("FM-SWING-10L")


def test_bench_and_rails_match_sections_3_and_4() -> None:
    roster = load_roster()
    bench = roster.get("BENCH-N500")
    assert isinstance(bench, BenchMandate)
    assert bench.kind is BookKind.BENCH
    assert "nifty500" in bench.benchmark
    rails = roster.rails
    assert rails.participation_max_pct == Decimal("5")
    assert rails.participation_lookback_sessions == 20
    assert rails.min_hold_sessions == 2
    assert (rails.allow_short, rails.allow_fno, rails.allow_margin) == (False, False, False)


def test_roster_is_frozen() -> None:
    manager = load_roster().manager_books[0]
    with pytest.raises(Exception, match="frozen"):
        manager.max_positions = 99


# ── refusals ─────────────────────────────────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    ("book_id", "path", "value"),
    [
        ("FM-SWING-BRK-10L", ("opening_capital_inr",), 1000000.0),
        ("FM-SWING-BRK-10L", ("max_position_pct",), 10.0),
        ("FM-POS-TREND-1CR", ("universe", "min_median_traded_value_inr"), 1.0e7),
        ("CTRL-FM-POS-TREND-10L", ("opening_capital_inr",), 1000000.0),
        ("BENCH-N500", ("opening_capital_inr",), 1000000.0),
        ("FM-SWING-BRK-10L", ("opening_capital_inr",), True),
    ],
)
def test_a_float_in_a_money_or_percentage_field_is_refused(
    book_id: str, path: tuple[str, ...], value: object
) -> None:
    raw = _raw()
    target = _book(raw, book_id)
    for key in path[:-1]:
        target[key] = dict(target[key])
        target = target[key]
    target[path[-1]] = value
    with pytest.raises(RosterError, match="never a float"):
        _parse(raw)


def test_a_float_in_a_rail_is_refused() -> None:
    raw = _raw()
    raw["rails"]["participation_max_pct"] = 5.0
    with pytest.raises(RosterError, match="never a float"):
        _parse(raw)


def test_a_quoted_decimal_string_is_accepted_exactly() -> None:
    raw = _raw()
    _book(raw, "FM-SWING-BRK-10L")["opening_capital_inr"] = "1000000.00"
    _book(raw, "CTRL-FM-SWING-BRK-10L")["opening_capital_inr"] = "1000000.00"
    _parse(raw)


@pytest.mark.parametrize(
    "where",
    ["root", "rails", "manager", "manager.universe", "manager.rounds", "control", "bench"],
)
def test_an_unknown_key_is_refused(where: str) -> None:
    raw = _raw()
    targets: dict[str, dict[str, Any]] = {
        "root": raw,
        "rails": raw["rails"],
        "manager": _book(raw, "FM-SWING-BRK-10L"),
        "control": _book(raw, "CTRL-FM-SWING-BRK-10L"),
        "bench": _book(raw, "BENCH-N500"),
    }
    if where in {"manager.universe", "manager.rounds"}:
        manager = _book(raw, "FM-SWING-BRK-10L")
        field = where.split(".")[1]
        manager[field] = dict(manager[field])
        targets[where] = manager[field]
    targets[where]["max_leverage"] = 2
    with pytest.raises(RosterError, match="max_leverage"):
        _parse(raw)


def test_a_duplicate_id_is_refused() -> None:
    raw = _raw()
    raw["books"].append(copy.deepcopy(_book(raw, "BENCH-N500")))
    with pytest.raises(RosterError, match="duplicate book id 'BENCH-N500'"):
        _parse(raw)


def test_a_duplicate_manager_id_is_refused() -> None:
    raw = _raw()
    raw["books"].append(copy.deepcopy(_book(raw, "FM-POS-TREND-1CR")))
    with pytest.raises(RosterError, match="duplicate book id 'FM-POS-TREND-1CR'"):
        _parse(raw)


def test_a_duplicate_yaml_key_is_refused() -> None:
    text = ROSTER_PATH.read_text(encoding="utf-8").replace(
        "    max_positions: 15\n", "    max_positions: 15\n    max_positions: 99\n", 1
    )
    with pytest.raises(RosterError, match="duplicate key 'max_positions'"):
        parse_roster(text)


def test_shorting_cannot_be_switched_on() -> None:
    raw = _raw()
    raw["rails"]["allow_short"] = True
    with pytest.raises(RosterError, match="allow_short"):
        _parse(raw)


def test_a_control_must_mirror_its_manager() -> None:
    raw = _raw()
    _book(raw, "CTRL-FM-SWING-BRK-1CR")["opening_capital_inr"] = 1000000
    with pytest.raises(RosterError, match="same capital"):
        _parse(raw)


def test_a_control_top_n_must_equal_its_managers_max_positions() -> None:
    raw = _raw()
    _book(raw, "CTRL-FM-POS-TREND-10L")["shortlist_top_n"] = 40
    with pytest.raises(RosterError, match="shortlist_top_n"):
        _parse(raw)


@pytest.mark.parametrize(
    ("control_id", "cadence"),
    [
        ("CTRL-FM-SWING-BRK-10L", {"unit": "SESSIONS", "every": 21}),
        ("CTRL-FM-POS-TREND-1CR", {"unit": "WEEKLY", "every": 1}),
        ("CTRL-FM-POS-TREND-1CR", {"unit": "SESSIONS", "every": 5}),
    ],
)
def test_a_control_cadence_must_follow_its_managers_style(
    control_id: str, cadence: dict[str, Any]
) -> None:
    raw = _raw()
    _book(raw, control_id)["rebalance"] = cadence
    with pytest.raises(RosterError, match="must rebalance"):
        _parse(raw)


def test_a_manager_without_a_control_is_refused() -> None:
    raw = _raw()
    raw["books"] = [b for b in raw["books"] if b["id"] != "CTRL-FM-POS-TREND-10L"]
    with pytest.raises(RosterError, match="without a control book"):
        _parse(raw)


def test_a_control_for_an_unknown_manager_is_refused() -> None:
    raw = _raw()
    raw["books"] = [
        b for b in raw["books"] if b["id"] not in ("FM-POS-TREND-10L", "FM-POS-TREND-1CR")
    ]
    with pytest.raises(RosterError, match="not a manager"):
        _parse(raw)


def test_a_configured_horizon_is_refused_because_the_style_fixes_it() -> None:
    # The band was configurable (and an inverted one refused); under Amendment 2 the style fixes it.
    raw = _raw()
    _book(raw, "FM-POS-TREND-10L")["horizon"] = {"min_sessions": 60, "max_sessions": 20}
    with pytest.raises(RosterError, match="horizon"):
        _parse(raw)
    roster = load_roster()
    for book in roster.manager_books:
        band = book.horizon
        assert band.min_sessions <= band.max_sessions
        assert book.model_dump(mode="json")["horizon"] == band.model_dump(mode="json")


def test_an_inverted_horizon_band_is_refused() -> None:
    from analyst.fundmanager import HorizonBand

    with pytest.raises(ValueError, match="exceeds max_sessions"):
        HorizonBand(min_sessions=60, max_sessions=20)


# ── Amendment 2: every pairing is validated ──────────────────────────────────────────────────────


@pytest.mark.parametrize(
    ("field", "value", "match"),
    [
        ("style", "SWING_EVENT", "starts from screens"),
        ("starting_screens", ["S4"], "starts from screens"),
        ("max_positions", 20, "differs from"),
        ("prompt_path", "analyst/fundmanager/prompts/v2.md", "differs from"),
        ("rounds", {"max_isins": 13, "max_queries": 8, "max_research_rounds": 2}, "differs from"),
    ],
)
def test_a_mirror_that_differs_from_its_primary_is_refused(
    field: str, value: object, match: str
) -> None:
    raw = _raw()
    _book(raw, "FM-SWING-BRK-1CR")[field] = value
    with pytest.raises(RosterError, match=match):
        _parse(raw)


def test_both_books_of_a_manager_differing_in_style_together_is_still_refused() -> None:
    raw = _raw()
    for book_id in ("FM-SWING-BRK-10L", "FM-SWING-BRK-1CR"):
        _book(raw, book_id)["style"] = "SWING_EVENT"  # but the screens are still S2, S3
    with pytest.raises(RosterError, match="starts from screens"):
        _parse(raw)


def test_a_manager_needs_both_a_primary_and_a_mirror() -> None:
    raw = _raw()
    raw["books"] = [
        b for b in raw["books"] if b["id"] not in ("FM-POS-FUND-1CR", "CTRL-FM-POS-FUND-1CR")
    ]
    with pytest.raises(RosterError, match="has no \\['MIRROR'\\] book"):
        _parse(raw)


def test_two_primaries_for_one_manager_are_refused() -> None:
    raw = _raw()
    book = _book(raw, "FM-POS-FUND-1CR")
    book["role"] = "PRIMARY"
    with pytest.raises(RosterError, match="must be named FM-POS-FUND-10L"):
        _parse(raw)


@pytest.mark.parametrize(
    ("book_id", "update", "match"),
    [
        ("FM-POS-FUND-1CR", {"opening_capital_inr": 1000000}, "a -1CR book opens with"),
        ("FM-POS-FUND-10L", {"opening_capital_inr": 10000000}, "a -10L book opens with"),
        ("FM-POS-FUND-10L", {"role": "MIRROR"}, "must be named FM-POS-FUND-1CR"),
        ("FM-POS-FUND-10L", {"manager": "FM-POS-OTHER"}, "must be named FM-POS-OTHER-10L"),
    ],
)
def test_a_manager_book_is_named_and_capitalised_by_its_role(
    book_id: str, update: dict[str, Any], match: str
) -> None:
    raw = _raw()
    _book(raw, book_id).update(update)
    with pytest.raises(RosterError, match=match):
        _parse(raw)


def test_every_manager_book_needs_its_own_control() -> None:
    raw = _raw()
    raw["books"] = [b for b in raw["books"] if b["id"] != "CTRL-FM-SWING-EVT-1CR"]
    with pytest.raises(RosterError, match="FM-SWING-EVT-1CR"):
        _parse(raw)


def test_a_control_of_a_mirror_runs_on_the_mirrors_capital() -> None:
    raw = _raw()
    _book(raw, "CTRL-FM-SWING-EVT-1CR")["opening_capital_inr"] = 1000000
    with pytest.raises(RosterError, match="same capital"):
        _parse(raw)


def test_a_manager_without_a_style_book_is_refused() -> None:
    raw = _raw()
    raw["books"] = [b for b in raw["books"] if b["id"] != "STYLE-FM-POS-TREND"]
    with pytest.raises(RosterError, match="without a style book"):
        _parse(raw)


@pytest.mark.parametrize(
    ("update", "match"),
    [
        ({"opening_capital_inr": 10000000}, "primary capital"),
        ({"max_names": 20}, "max_names"),
        ({"rebalance": {"unit": "WEEKLY", "every": 1}}, "must rebalance"),
        ({"style_for": "FM-POS-NONE", "id": "STYLE-FM-POS-NONE"}, "not a manager"),
        ({"id": "STYLE-FM-POS-OTHER"}, "must be named STYLE-FM-POS-TREND"),
    ],
)
def test_a_style_book_must_match_its_managers_primary(update: dict[str, Any], match: str) -> None:
    raw = _raw()
    _book(raw, "STYLE-FM-POS-TREND").update(update)
    with pytest.raises(RosterError, match=match):
        _parse(raw)


def test_an_escaping_prompt_path_is_refused() -> None:
    raw = _raw()
    _book(raw, "FM-POS-TREND-10L")["prompt_path"] = "../outside.md"
    with pytest.raises(RosterError, match="repo-relative"):
        _parse(raw)


def test_a_missing_roster_file_is_loud(tmp_path: Path) -> None:
    with pytest.raises(RosterError, match="cannot read"):
        load_roster(tmp_path / "absent.yaml")


# ── mandate_hash ─────────────────────────────────────────────────────────────────────────────────

PROMPT = b"You are FM-{id}. Your mandate follows.\n"
SCHEMA = b'{"type":"object"}'
RULE = "shortlist-rule-source-sha256:abc123"


def _hash_inputs() -> dict[str, Any]:
    roster = load_roster()
    return {
        "mandate": roster.manager_books[0],
        "prompt_bytes": PROMPT,
        "schema_bytes": SCHEMA,
        "shortlist_rule": RULE,
        "rails": roster.rails,
    }


def test_mandate_hash_is_a_sha256_hex_and_deterministic() -> None:
    first = mandate_hash(**_hash_inputs())
    assert len(first) == 64
    int(first, 16)
    assert mandate_hash(**_hash_inputs()) == first


def test_every_book_hashes_differently() -> None:
    roster = load_roster()
    digests = {mandate_hash(b, b"", b"", RULE, roster.rails) for b in roster.books}
    assert len(digests) == len(roster.books)


@pytest.mark.parametrize(
    ("name", "changed"),
    [
        ("prompt_bytes", PROMPT + b" "),
        ("schema_bytes", b'{"type":"array"}'),
        ("shortlist_rule", RULE + "d"),
        ("shortlist_rule", RULE.encode("utf-8") + b"\x00"),
    ],
)
def test_mandate_hash_changes_with_each_byte_input(name: str, changed: object) -> None:
    inputs = _hash_inputs()
    before = mandate_hash(**inputs)
    inputs[name] = changed
    assert mandate_hash(**inputs) != before


@pytest.mark.parametrize(
    "update",
    [
        {"participation_max_pct": Decimal("6")},
        {"participation_lookback_sessions": 21},
        {"min_hold_sessions": 3},
    ],
)
def test_mandate_hash_changes_with_the_rails(update: dict[str, Any]) -> None:
    inputs = _hash_inputs()
    before = mandate_hash(**inputs)
    inputs["rails"] = inputs["rails"].model_copy(update=update)
    assert mandate_hash(**inputs) != before


@pytest.mark.parametrize(
    "mutate",
    [
        lambda m: m.model_copy(update={"id": "FM-SWING-BRK-10L-V2"}),
        lambda m: m.model_copy(update={"opening_capital_inr": Decimal("1000001")}),
        lambda m: m.model_copy(update={"max_positions": 14}),
        lambda m: m.model_copy(update={"max_position_pct": Decimal("9")}),
        lambda m: m.model_copy(update={"max_sector_pct": Decimal("25")}),
        # Amendment 2: the style (within a family and across), its screens, the manager, the role
        lambda m: m.model_copy(update={"style": ManagerStyle.SWING_EVENT}),
        lambda m: m.model_copy(update={"style": ManagerStyle.POSITIONAL_TREND}),
        lambda m: m.model_copy(update={"starting_screens": ("S3", "S2")}),
        lambda m: m.model_copy(update={"manager": "FM-SWING-BRK2"}),
        lambda m: m.model_copy(update={"role": BookRole.MIRROR}),
        lambda m: m.model_copy(update={"prompt_path": "analyst/fundmanager/prompts/v2.md"}),
        lambda m: m.model_copy(
            update={"models": m.models.model_copy(update={"decision": "claude-sonnet-5-5"})}
        ),
        lambda m: m.model_copy(
            update={"models": m.models.model_copy(update={"digest": "claude-opus-5-5"})}
        ),
        lambda m: m.model_copy(update={"rounds": m.rounds.model_copy(update={"max_isins": 13})}),
        lambda m: m.model_copy(update={"rounds": m.rounds.model_copy(update={"max_queries": 9})}),
        lambda m: m.model_copy(
            update={"rounds": m.rounds.model_copy(update={"max_research_rounds": 3})}
        ),
        lambda m: m.model_copy(
            update={
                "universe": m.universe.model_copy(
                    update={"min_median_traded_value_inr": Decimal("5000000")}
                )
            }
        ),
        lambda m: m.model_copy(
            update={"universe": m.universe.model_copy(update={"flagged_surveillance": ()})}
        ),
    ],
)
def test_mandate_hash_changes_with_each_mandate_field(mutate: Any) -> None:
    inputs = _hash_inputs()
    before = mandate_hash(**inputs)
    inputs["mandate"] = mutate(inputs["mandate"])
    assert mandate_hash(**inputs) != before


def test_section_boundaries_are_unambiguous() -> None:
    # Moving bytes from the prompt into the schema must not collide.
    inputs = _hash_inputs()
    a = mandate_hash(**{**inputs, "prompt_bytes": b"ab", "schema_bytes": b"c"})
    b = mandate_hash(**{**inputs, "prompt_bytes": b"a", "schema_bytes": b"bc"})
    assert a != b


_CHILD = """
import sys
from analyst.fundmanager import load_roster, mandate_hash
roster = load_roster()
for book in roster.books:
    print(book.id, mandate_hash(book, b"prompt", b"schema", "rule", roster.rails))
"""


def test_mandate_hash_is_stable_across_processes() -> None:
    roster = load_roster()
    expected = "".join(
        f"{b.id} {mandate_hash(b, b'prompt', b'schema', 'rule', roster.rails)}\n"
        for b in roster.books
    )
    for seed in ("0", "1", "4242"):
        env = {**os.environ, "PYTHONHASHSEED": seed}
        out = subprocess.run(
            [sys.executable, "-c", _CHILD],
            capture_output=True,
            text=True,
            check=True,
            cwd=REPO,
            env=env,
        ).stdout
        assert out == expected, f"hash differs in a child process with PYTHONHASHSEED={seed}"
