"""X1: the dated settlement cycle (T+2, T+1 phase-in, T+1), read per trade date from `cycles.yaml`.

`SimBroker` asks this for N in T+N on every fill; nothing else in the repo may hard-code a
settlement lag, for the same reason nothing else may compute a transaction cost.
"""

from execution.settlement.schedule import (
    CYCLES_PATH,
    Approximation,
    NoSettlementCycleError,
    Provenance,
    SettlementEra,
    SettlementError,
    SettlementSchedule,
    load_settlement_schedule,
)

__all__ = [
    "CYCLES_PATH",
    "Approximation",
    "NoSettlementCycleError",
    "Provenance",
    "SettlementEra",
    "SettlementError",
    "SettlementSchedule",
    "load_settlement_schedule",
]
