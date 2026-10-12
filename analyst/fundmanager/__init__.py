"""A10 fund managers: private judgment (pre-registration M17, 2026-10-09).

Each M17 manager is a configuration — a mandate, a model, a frozen prompt, rails and a tool budget
— that owns one paper book and one journal stream. This package holds that private side: the
mandates (`roster.yaml`, `mandate.py`), and later the runtime, books, controls and scoreboard.

It may read `analyst.commons` (shared facts). The reverse is forbidden and AST-enforced
(`tests/unit/test_commons_isolation.py`), so no manager's decision content can reach the layer
every manager reads.
"""

from analyst.fundmanager.mandate import (
    CONTROL_CADENCE,
    FAMILY_HORIZON,
    ROLE_SUFFIX,
    ROSTER_PATH,
    STYLE_FAMILY,
    STYLE_STARTING_SCREENS,
    SUFFIX_CAPITAL_INR,
    AnyMandate,
    BenchMandate,
    BookKind,
    BookRole,
    ControlMandate,
    HorizonBand,
    HorizonStyle,
    M17Rails,
    ManagerMandate,
    ManagerStyle,
    Mandate,
    ModelIds,
    RebalanceCadence,
    RebalanceUnit,
    Roster,
    RosterError,
    RoundLimits,
    StyleMandate,
    TradableMandate,
    UniverseFloor,
    canonical_json,
    load_roster,
    mandate_hash,
    parse_roster,
)

__all__ = [
    "CONTROL_CADENCE",
    "FAMILY_HORIZON",
    "ROLE_SUFFIX",
    "ROSTER_PATH",
    "STYLE_FAMILY",
    "STYLE_STARTING_SCREENS",
    "SUFFIX_CAPITAL_INR",
    "AnyMandate",
    "BenchMandate",
    "BookKind",
    "BookRole",
    "ControlMandate",
    "HorizonBand",
    "HorizonStyle",
    "M17Rails",
    "ManagerMandate",
    "ManagerStyle",
    "Mandate",
    "ModelIds",
    "RebalanceCadence",
    "RebalanceUnit",
    "Roster",
    "RosterError",
    "RoundLimits",
    "StyleMandate",
    "TradableMandate",
    "UniverseFloor",
    "canonical_json",
    "load_roster",
    "mandate_hash",
    "parse_roster",
]
