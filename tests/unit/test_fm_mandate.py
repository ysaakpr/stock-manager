"""M17.0: the roster is pre-registration §2/§3 exactly, refuses bad input, and hashes stably.

The expected values below are transcribed from
`ops/studies/preregistration-m17-ai-fund-managers-2026-10-09.md`, not from `roster.yaml`: if the
YAML drifts from the ratified document, these fail.
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
    ControlMandate,
    HorizonStyle,
    ManagerMandate,
    RebalanceUnit,
    RosterError,
    load_roster,
    mandate_hash,
    parse_roster,
)

REPO = Path(__file__).resolve().parents[2]
PREREG = "ops/studies/preregistration-m17-ai-fund-managers-2026-10-09.md"

LAKH = Decimal("100000")
CRORE = Decimal("10000000")

# id: (style, horizon band, capital, max positions) — pre-registration §2.
MANAGERS = {
    "FM-SWING-10L": (HorizonStyle.SWING, (5, 20), 10 * LAKH, 15),
    "FM-SWING-1CR": (HorizonStyle.SWING, (5, 20), CRORE, 20),
    "FM-POS-10L": (HorizonStyle.POSITIONAL, (20, 60), 10 * LAKH, 15),
    "FM-POS-1CR": (HorizonStyle.POSITIONAL, (20, 60), CRORE, 20),
}


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
    assert [m.id for m in roster.managers] == list(MANAGERS)
    assert [c.id for c in roster.controls] == [f"CTRL-{m}" for m in MANAGERS]
    assert [b.id for b in roster.benches] == ["BENCH-N500"]
    assert len(roster.books) == 9


@pytest.mark.parametrize("manager_id", list(MANAGERS))
def test_each_manager_matches_section_2(manager_id: str) -> None:
    style, (lo, hi), capital, max_positions = MANAGERS[manager_id]
    manager = load_roster().get(manager_id)
    assert isinstance(manager, ManagerMandate)
    assert manager.kind is BookKind.MANAGER
    assert manager.style is style
    assert (manager.horizon.min_sessions, manager.horizon.max_sessions) == (lo, hi)
    assert manager.opening_capital_inr == capital
    assert type(manager.opening_capital_inr) is Decimal
    assert manager.max_positions == max_positions
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


@pytest.mark.parametrize("manager_id", list(MANAGERS))
def test_each_control_matches_section_3(manager_id: str) -> None:
    roster = load_roster()
    manager = roster.get(manager_id)
    control = roster.control_for(manager_id)
    assert isinstance(manager, ManagerMandate)
    assert isinstance(control, ControlMandate)
    assert control.id == f"CTRL-{manager_id}"
    assert control.shortlist_top_n == manager.max_positions
    assert control.opening_capital_inr == manager.opening_capital_inr
    assert control.max_position_pct == manager.max_position_pct
    assert control.universe == manager.universe
    if manager.style is HorizonStyle.SWING:
        assert (control.rebalance.unit, control.rebalance.every) == (RebalanceUnit.WEEKLY, 1)
    else:
        assert (control.rebalance.unit, control.rebalance.every) == (RebalanceUnit.SESSIONS, 21)


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
    manager = load_roster().managers[0]
    with pytest.raises(Exception, match="frozen"):
        manager.max_positions = 99


# ── refusals ─────────────────────────────────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    ("book_id", "path", "value"),
    [
        ("FM-SWING-10L", ("opening_capital_inr",), 1000000.0),
        ("FM-SWING-10L", ("max_position_pct",), 10.0),
        ("FM-POS-1CR", ("universe", "min_median_traded_value_inr"), 1.0e7),
        ("CTRL-FM-POS-10L", ("opening_capital_inr",), 1000000.0),
        ("BENCH-N500", ("opening_capital_inr",), 1000000.0),
        ("FM-SWING-10L", ("opening_capital_inr",), True),
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
    _book(raw, "FM-SWING-10L")["opening_capital_inr"] = "1000000.00"
    _book(raw, "CTRL-FM-SWING-10L")["opening_capital_inr"] = "1000000.00"
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
        "manager": _book(raw, "FM-SWING-10L"),
        "control": _book(raw, "CTRL-FM-SWING-10L"),
        "bench": _book(raw, "BENCH-N500"),
    }
    if where in {"manager.universe", "manager.rounds"}:
        manager = _book(raw, "FM-SWING-10L")
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
    raw["books"].append(copy.deepcopy(_book(raw, "FM-POS-1CR")))
    with pytest.raises(RosterError, match="duplicate book id 'FM-POS-1CR'"):
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
    _book(raw, "CTRL-FM-SWING-1CR")["opening_capital_inr"] = 1000000
    with pytest.raises(RosterError, match="same capital"):
        _parse(raw)


def test_a_control_top_n_must_equal_its_managers_max_positions() -> None:
    raw = _raw()
    _book(raw, "CTRL-FM-POS-10L")["shortlist_top_n"] = 40
    with pytest.raises(RosterError, match="shortlist_top_n"):
        _parse(raw)


@pytest.mark.parametrize(
    ("control_id", "cadence"),
    [
        ("CTRL-FM-SWING-10L", {"unit": "SESSIONS", "every": 21}),
        ("CTRL-FM-POS-1CR", {"unit": "WEEKLY", "every": 1}),
        ("CTRL-FM-POS-1CR", {"unit": "SESSIONS", "every": 5}),
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
    raw["books"] = [b for b in raw["books"] if b["id"] != "CTRL-FM-POS-10L"]
    with pytest.raises(RosterError, match="without a control book"):
        _parse(raw)


def test_a_control_for_an_unknown_manager_is_refused() -> None:
    raw = _raw()
    raw["books"] = [b for b in raw["books"] if b["id"] != "FM-POS-10L"]
    with pytest.raises(RosterError, match="not a manager"):
        _parse(raw)


def test_an_inverted_horizon_band_is_refused() -> None:
    raw = _raw()
    _book(raw, "FM-POS-10L")["horizon"] = {"min_sessions": 60, "max_sessions": 20}
    with pytest.raises(RosterError, match="exceeds max_sessions"):
        _parse(raw)


def test_an_escaping_prompt_path_is_refused() -> None:
    raw = _raw()
    _book(raw, "FM-POS-10L")["prompt_path"] = "../outside.md"
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
        "mandate": roster.managers[0],
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
        lambda m: m.model_copy(update={"id": "FM-SWING-10L-V2"}),
        lambda m: m.model_copy(update={"opening_capital_inr": Decimal("1000001")}),
        lambda m: m.model_copy(update={"max_positions": 14}),
        lambda m: m.model_copy(update={"max_position_pct": Decimal("9")}),
        lambda m: m.model_copy(update={"max_sector_pct": Decimal("25")}),
        lambda m: m.model_copy(update={"style": HorizonStyle.POSITIONAL}),
        lambda m: m.model_copy(update={"prompt_path": "analyst/fundmanager/prompts/v2.md"}),
        lambda m: m.model_copy(
            update={"horizon": m.horizon.model_copy(update={"max_sessions": 21})}
        ),
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
