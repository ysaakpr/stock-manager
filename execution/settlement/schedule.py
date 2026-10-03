"""X1: the settlement cycle as a dated schedule — how many trading sessions a trade takes to settle.

India's equity cash market was T+2 from 2003-04-01, phased to T+1 by market-cap tranche between
2022-02-25 and 2023-01-27, and T+1 for everything since. A backtest that settles 2010 trades T+1
spends sale proceeds a session before the market paid them out, so the cycle is data, read per
trade date from `cycles.yaml`, exactly as the cost model reads its dated rate card.

What it does: given a trade date (and the ISIN, for per-security eras) it returns N in T+N — a
count of *trading sessions*, which the caller walks on the exchange calendar.

What it assumes: `cycles.yaml` is checked in and its eras are ascending; each era is in force from
its `effective_from` until the next one starts, and the last era is open-ended.

What it never does: guess. A trade date before the first era raises `NoSettlementCycleError`
rather than borrowing a later cycle. It never counts calendar days and never reads a clock.
"""

from __future__ import annotations

from datetime import date
from enum import StrEnum
from functools import lru_cache
from pathlib import Path
from typing import Final

import yaml
from pydantic import BaseModel, ConfigDict, Field, model_validator

#: The dated settlement schedule this module is meaningless without.
CYCLES_PATH: Final[Path] = Path(__file__).with_name("cycles.yaml")

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


class SettlementError(Exception):
    """Base for every refusal to say when a trade settles."""


class NoSettlementCycleError(SettlementError):
    """The trade date is before the earliest era in the settlement schedule."""


class Provenance(StrEnum):
    """How much of an era was read off a source (see `cycles.yaml`'s header)."""

    VERIFIED = "verified"
    RECONSTRUCTED = "reconstructed"


class Approximation(StrEnum):
    """Which way a reconstructed era errs. Only `conservative` (never grants liquidity) is allowed.

    An era that could settle a trade *faster* than the market did would hand a backtest cash it
    never had, which is the defect this schedule exists to remove — so the schema cannot say it.
    """

    CONSERVATIVE = "conservative"


class _Frozen(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class ScheduleMeta(_Frozen):
    """What this schedule is a schedule *for*. Documentation with a schema."""

    market: str
    scope: str


class SettlementEra(_Frozen):
    """One settlement regime, in force from `effective_from` until the next era starts."""

    id: str
    effective_from: date
    lag_sessions: int = Field(ge=1, strict=True)
    label: str
    provenance: Provenance
    approximation: Approximation | None = None
    sources_read_on: date
    sources: list[str] = Field(min_length=1)
    notes: str

    @model_validator(mode="after")
    def _reconstructed_states_its_direction(self) -> SettlementEra:
        if self.provenance is Provenance.RECONSTRUCTED and self.approximation is None:
            raise ValueError(
                f"era {self.id} is reconstructed and must state the direction of its approximation"
            )
        if self.provenance is Provenance.VERIFIED and self.approximation is not None:
            raise ValueError(
                f"era {self.id} is verified; an approximation direction is meaningless"
            )
        return self


class SettlementSchedule(_Frozen):
    """The whole dated schedule: the eras, oldest first."""

    version: int
    schedule: ScheduleMeta
    eras: list[SettlementEra] = Field(min_length=1)

    @model_validator(mode="after")
    def _eras_are_ordered_and_unique(self) -> SettlementSchedule:
        dates = [era.effective_from for era in self.eras]
        if dates != sorted(dates):
            raise ValueError("eras must be listed in ascending effective_from order")
        if len(set(dates)) != len(dates):
            raise ValueError("two eras share an effective_from; the one in force is ambiguous")
        ids = [era.id for era in self.eras]
        if len(set(ids)) != len(ids):
            raise ValueError("duplicate era id")
        return self

    def era_for(self, trade_date: date) -> SettlementEra:
        """The era in force on `trade_date`. Raises `NoSettlementCycleError` before the first."""
        in_force = [era for era in self.eras if era.effective_from <= trade_date]
        if not in_force:
            earliest = self.eras[0].effective_from
            raise NoSettlementCycleError(
                f"no settlement cycle covers {trade_date.isoformat()}; the schedule starts at "
                f"{earliest.isoformat()}. Add an era to {CYCLES_PATH.name} rather than settling "
                "the trade on a later cycle."
            )
        return in_force[-1]

    def lag_for(self, trade_date: date, isin: str) -> int:
        """N in T+N — trading sessions from `trade_date` to settlement — for `isin`.

        `isin` is taken so a per-security era (the 2022 T+1 tranches) can be expressed without
        changing a call site; no era is per-security today, so it does not yet change the answer.
        """
        del isin  # no per-security era on file yet; see `t1-phase-in` in cycles.yaml
        return self.era_for(trade_date).lag_sessions


@lru_cache(maxsize=1)
def load_settlement_schedule(path: Path = CYCLES_PATH) -> SettlementSchedule:
    """Parse and validate the dated settlement schedule, once per process.

    Assumes the file is checked in and trusted. Raises `ValidationError` on a schema break and
    `FileNotFoundError` if it is missing — both loud, neither recoverable here.
    """
    with path.open(encoding="utf-8") as handle:
        raw = yaml.safe_load(handle)
    return SettlementSchedule.model_validate(raw)
