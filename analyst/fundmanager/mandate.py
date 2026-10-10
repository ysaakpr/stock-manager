"""M17 fund-manager mandates: the typed, frozen form of the pre-registration roster.

A fund manager is a configuration, not code (pre-registration §1). This module is that
configuration's schema: `roster.yaml` holds the four managers of §2, the four control books and
`BENCH-N500` of §3, and the M17 rails of §4 step 5; `load_roster` turns it into a `Roster` of
frozen pydantic models and refuses anything that does not fit.

What it does: parses, type-checks and cross-checks the roster, and fingerprints a mandate with
`mandate_hash` (pre-registration §7: a changed hash is a new manager, journaled at S0 by M17.8).
What it assumes: percentages are percentage points (`Decimal("10")` is 10 %), money is whole or
decimal rupees written as a YAML integer or a quoted string, and a session count is an integer.
What it never does: accept a float where money or a percentage goes (a binary float in a capital
or a cap is a bug, CLAUDE.md), accept an unknown key (a typo must not become a silent default),
or read anything about another manager's book or decisions — it is configuration only.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Hashable
from decimal import Decimal
from enum import StrEnum
from pathlib import Path, PurePosixPath
from typing import Annotated, Any, Final, Literal

import yaml
from pydantic import (
    BaseModel,
    BeforeValidator,
    ConfigDict,
    Field,
    StrictInt,
    StrictStr,
    ValidationError,
    model_validator,
)

ROSTER_PATH: Final = Path(__file__).with_name("roster.yaml")

_HASH_DOMAIN: Final = b"m17-mandate-hash/v1"


class RosterError(ValueError):
    """The roster is missing, malformed, or contradicts itself — always fatal, never skipped."""


def _refuse_float(value: object) -> object:
    """Reject a binary float (and a bool) before pydantic coerces it into a `Decimal`.

    `Decimal(0.1)` is `0.1000000000000000055511151231257827...`; a capital or a cap that went
    through a float is already wrong. YAML integers and quoted decimal strings are exact and pass.
    """
    if isinstance(value, bool | float):
        raise ValueError(
            f"{value!r} is a {type(value).__name__}; money and percentages must be an integer "
            "or a quoted decimal string, never a float"
        )
    return value


#: An exact amount in rupees: never a float, never NaN/inf, never negative.
Money = Annotated[Decimal, BeforeValidator(_refuse_float), Field(ge=0, allow_inf_nan=False)]
#: Percentage points in (0, 100]: `Decimal("10")` means 10 %. Never a float.
Pct = Annotated[Decimal, BeforeValidator(_refuse_float), Field(gt=0, le=100, allow_inf_nan=False)]
#: A count of trading sessions, ISINs, calls or positions.
Count = Annotated[StrictInt, Field(ge=1)]


class _Frozen(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid", strict=False)


class BookKind(StrEnum):
    """What a book is: an LLM manager, its no-LLM control, or the market benchmark (§2/§3)."""

    MANAGER = "MANAGER"
    CONTROL = "CONTROL"
    BENCH = "BENCH"


class HorizonStyle(StrEnum):
    """The manager's horizon family; it fixes its control book's rebalance cadence (§3)."""

    SWING = "SWING"
    POSITIONAL = "POSITIONAL"


class HorizonBand(_Frozen):
    """The horizon a manager *thinks in*, in sessions. Guidance, checked by the scoreboard, never
    enforced as a rail (pre-registration §2)."""

    min_sessions: Count
    max_sessions: Count

    @model_validator(mode="after")
    def _ordered(self) -> HorizonBand:
        if self.min_sessions > self.max_sessions:
            raise ValueError(
                f"horizon min_sessions {self.min_sessions} exceeds max_sessions {self.max_sessions}"
            )
        return self


class UniverseFloor(_Frozen):
    """The investable-universe screen of pre-registration §2.

    NSE `series` names priced on the decision session, with a `median_lookback_sessions` median
    traded value at or above `min_median_traded_value_inr`, excluding `excluded_surveillance`
    stages and flagging (never excluding) `flagged_surveillance` ones.
    """

    series: StrictStr
    min_median_traded_value_inr: Money
    median_lookback_sessions: Count
    excluded_surveillance: tuple[StrictStr, ...]
    flagged_surveillance: tuple[StrictStr, ...]


class ModelIds(_Frozen):
    """The model behind the manager's decisions and the one behind Commons digests (§2, §9)."""

    decision: StrictStr = Field(min_length=1)
    digest: StrictStr = Field(min_length=1)


class RoundLimits(_Frozen):
    """The bounded research protocol of pre-registration §4 step 3.

    Round 0 asks for at most `max_isins` deep-dives and `max_queries` web queries/URLs; at most
    `max_research_rounds` are fulfilled; then one final decision call.
    """

    max_isins: Count
    max_queries: Count
    max_research_rounds: Count

    @property
    def max_calls(self) -> int:
        """Model calls per session at most: round 0, every research round, the final call."""
        return 1 + self.max_research_rounds + 1


class RebalanceUnit(StrEnum):
    WEEKLY = "WEEKLY"
    SESSIONS = "SESSIONS"


class RebalanceCadence(_Frozen):
    """A control book's rebalance schedule: weekly (`every` must be 1) or every *n* sessions."""

    unit: RebalanceUnit
    every: Count

    @model_validator(mode="after")
    def _weekly_is_every_week(self) -> RebalanceCadence:
        if self.unit is RebalanceUnit.WEEKLY and self.every != 1:
            raise ValueError(f"a WEEKLY cadence rebalances every week, not every {self.every}")
        return self


class M17Rails(_Frozen):
    """The rails every M17 book shares (pre-registration §4 step 5), beyond the per-book caps.

    The per-book caps (position %, sector %, max positions) live on each mandate. Shorting, F&O
    and margin are not parameters that could be switched on: the type admits only `False`.
    """

    participation_max_pct: Pct
    participation_lookback_sessions: Count
    min_hold_sessions: Count
    allow_short: Literal[False]
    allow_fno: Literal[False]
    allow_margin: Literal[False]


class _BookCaps(_Frozen):
    """What a manager and its control share: capital, caps and universe (§3 "same capital")."""

    id: StrictStr = Field(min_length=1)
    opening_capital_inr: Money
    max_positions: Count
    max_position_pct: Pct
    max_sector_pct: Pct
    universe: UniverseFloor


class ManagerMandate(_BookCaps):
    """One LLM fund manager of pre-registration §2."""

    kind: Literal[BookKind.MANAGER]
    id: StrictStr = Field(pattern=r"^FM-[A-Z0-9]+(-[A-Z0-9]+)*$")
    style: HorizonStyle
    horizon: HorizonBand
    models: ModelIds
    prompt_path: StrictStr = Field(min_length=1)
    rounds: RoundLimits

    @model_validator(mode="after")
    def _prompt_path_is_repo_relative(self) -> ManagerMandate:
        path = PurePosixPath(self.prompt_path)
        if path.is_absolute() or ".." in path.parts:
            raise ValueError(f"prompt_path {self.prompt_path!r} must be repo-relative, no '..'")
        return self


class ControlMandate(_BookCaps):
    """`CTRL-<manager>`: equal weight over the top *N* of the same shortlist, no LLM (§3)."""

    kind: Literal[BookKind.CONTROL]
    id: StrictStr = Field(pattern=r"^CTRL-FM-[A-Z0-9]+(-[A-Z0-9]+)*$")
    controls_for: StrictStr = Field(min_length=1)
    shortlist_top_n: Count
    rebalance: RebalanceCadence

    @model_validator(mode="after")
    def _id_names_its_manager(self) -> ControlMandate:
        if self.id != f"CTRL-{self.controls_for}":
            raise ValueError(f"control {self.id!r} must be named CTRL-{self.controls_for}")
        return self


class BenchMandate(_Frozen):
    """`BENCH-N500`: buy-and-hold of the backtests' NIFTY 500 TRI proxy from S0 (§3)."""

    kind: Literal[BookKind.BENCH]
    id: StrictStr = Field(pattern=r"^BENCH-[A-Z0-9]+$")
    benchmark: StrictStr = Field(min_length=1)
    opening_capital_inr: Money


Mandate = Annotated[ManagerMandate | ControlMandate | BenchMandate, Field(discriminator="kind")]


class Roster(_Frozen):
    """The whole M17 roster: every book, the shared rails, and the pre-registration it encodes.

    Construction cross-checks the books, so a `Roster` that exists is internally consistent: ids
    are unique, every manager has exactly one control, and each control carries its manager's
    capital, caps and universe with `shortlist_top_n` equal to its max positions and the cadence
    its horizon style fixes (weekly for swing, every 21 sessions for positional).
    """

    preregistration: StrictStr = Field(min_length=1)
    rails: M17Rails
    books: tuple[Mandate, ...] = Field(min_length=1)

    @property
    def managers(self) -> tuple[ManagerMandate, ...]:
        return tuple(b for b in self.books if isinstance(b, ManagerMandate))

    @property
    def controls(self) -> tuple[ControlMandate, ...]:
        return tuple(b for b in self.books if isinstance(b, ControlMandate))

    @property
    def benches(self) -> tuple[BenchMandate, ...]:
        return tuple(b for b in self.books if isinstance(b, BenchMandate))

    def get(self, book_id: str) -> ManagerMandate | ControlMandate | BenchMandate:
        """The book with this id; `KeyError` naming it if there is none."""
        for book in self.books:
            if book.id == book_id:
                return book
        raise KeyError(f"no M17 book {book_id!r} in the roster")

    def control_for(self, manager_id: str) -> ControlMandate:
        """The control book of one manager; `KeyError` if it has none."""
        for control in self.controls:
            if control.controls_for == manager_id:
                return control
        raise KeyError(f"manager {manager_id!r} has no control book")

    @model_validator(mode="after")
    def _consistent(self) -> Roster:
        seen: set[str] = set()
        for book in self.books:
            if book.id in seen:
                raise ValueError(f"duplicate book id {book.id!r}")
            seen.add(book.id)

        managers = {m.id: m for m in self.managers}
        controlled: dict[str, str] = {}
        for control in self.controls:
            manager = managers.get(control.controls_for)
            if manager is None:
                raise ValueError(
                    f"{control.id} controls_for {control.controls_for!r}, which is not a manager"
                )
            if control.controls_for in controlled:
                raise ValueError(f"manager {control.controls_for!r} has two control books")
            controlled[control.controls_for] = control.id
            _check_control_matches(control, manager)

        orphans = sorted(set(managers) - set(controlled))
        if orphans:
            raise ValueError(f"managers without a control book: {orphans}")
        return self


#: The control cadence each horizon style fixes (pre-registration §3).
CONTROL_CADENCE: Final[dict[HorizonStyle, tuple[RebalanceUnit, int]]] = {
    HorizonStyle.SWING: (RebalanceUnit.WEEKLY, 1),
    HorizonStyle.POSITIONAL: (RebalanceUnit.SESSIONS, 21),
}


def _check_control_matches(control: ControlMandate, manager: ManagerMandate) -> None:
    for field in _BookCaps.model_fields:
        if field == "id":
            continue
        if getattr(control, field) != getattr(manager, field):
            raise ValueError(
                f"{control.id}.{field} {getattr(control, field)!r} differs from "
                f"{manager.id}.{field} {getattr(manager, field)!r}; a control runs on the same "
                "capital, caps and universe as its manager"
            )
    if control.shortlist_top_n != manager.max_positions:
        raise ValueError(
            f"{control.id}.shortlist_top_n {control.shortlist_top_n} must equal "
            f"{manager.id}.max_positions {manager.max_positions}"
        )
    unit, every = CONTROL_CADENCE[manager.style]
    if (control.rebalance.unit, control.rebalance.every) != (unit, every):
        raise ValueError(
            f"{control.id} must rebalance {unit.value} every {every} for a "
            f"{manager.style.value} manager, not {control.rebalance.unit.value} "
            f"every {control.rebalance.every}"
        )


class _UniqueKeyLoader(yaml.SafeLoader):
    """`yaml.SafeLoader` that refuses a repeated mapping key instead of keeping the last one."""


def _construct_unique_mapping(loader: yaml.SafeLoader, node: yaml.MappingNode) -> dict[Any, Any]:
    keys: set[Hashable] = set()
    for key_node, _ in node.value:
        key = loader.construct_object(key_node, deep=True)
        if key in keys:
            raise yaml.constructor.ConstructorError(
                None, None, f"duplicate key {key!r}", key_node.start_mark
            )
        keys.add(key)
    return loader.construct_mapping(node, deep=True)


_UniqueKeyLoader.add_constructor(
    yaml.resolver.BaseResolver.DEFAULT_MAPPING_TAG, _construct_unique_mapping
)


def parse_roster(text: str, *, source: str = "<roster>") -> Roster:
    """Parse roster YAML text into a `Roster`; `RosterError` naming `source` on any defect."""
    try:
        raw = yaml.load(text, Loader=_UniqueKeyLoader)  # SafeLoader subclass
    except yaml.YAMLError as exc:
        raise RosterError(f"{source}: not valid roster YAML: {exc}") from exc
    try:
        return Roster.model_validate(raw)
    except ValidationError as exc:
        raise RosterError(f"{source}: roster refused:\n{exc}") from exc


def load_roster(path: Path | None = None) -> Roster:
    """Load and validate the M17 roster (default: the `roster.yaml` beside this module).

    Raises `RosterError` on a missing file, a duplicate key or id, an unknown key, a float in a
    money or percentage field, or a control that does not mirror its manager.
    """
    where = ROSTER_PATH if path is None else path
    try:
        text = where.read_text(encoding="utf-8")
    except OSError as exc:
        raise RosterError(f"cannot read the M17 roster at {where}: {exc}") from exc
    return parse_roster(text, source=str(where))


def _frame(label: str, payload: bytes) -> bytes:
    # Length-prefixed, labelled sections: no two different input tuples share a byte stream.
    head = label.encode("ascii")
    return len(head).to_bytes(4, "big") + head + len(payload).to_bytes(8, "big") + payload


def canonical_json(model: BaseModel) -> bytes:
    """A model's canonical bytes: JSON mode (Decimals as strings), sorted keys, no whitespace."""
    return json.dumps(
        model.model_dump(mode="json"), sort_keys=True, separators=(",", ":"), ensure_ascii=True
    ).encode("ascii")


def mandate_hash(
    mandate: ManagerMandate | ControlMandate | BenchMandate,
    prompt_bytes: bytes,
    schema_bytes: bytes,
    shortlist_rule: str | bytes,
    rails: M17Rails,
) -> str:
    """The sha256 identity of one book's frozen policy (pre-registration §7), as lowercase hex.

    Covers the mandate's every field, the rendered prompt template, the decision schema, the
    shortlist rule (its source or source hash, M17.2) and the shared rails. Stable across
    processes and Python hash seeds: it hashes canonical bytes only, never `hash()` or a repr.
    Changing any one input changes the digest, and a changed digest is a new manager.
    A control or bench book has no prompt or schema; pass `b""`.
    """
    rule = shortlist_rule.encode("utf-8") if isinstance(shortlist_rule, str) else shortlist_rule
    digest = hashlib.sha256(_HASH_DOMAIN)
    for label, payload in (
        ("mandate", canonical_json(mandate)),
        ("prompt", prompt_bytes),
        ("schema", schema_bytes),
        ("shortlist_rule", rule),
        ("rails", canonical_json(rails)),
    ):
        digest.update(_frame(label, payload))
    return digest.hexdigest()


__all__ = [
    "CONTROL_CADENCE",
    "ROSTER_PATH",
    "BenchMandate",
    "BookKind",
    "ControlMandate",
    "HorizonBand",
    "HorizonStyle",
    "M17Rails",
    "ManagerMandate",
    "Mandate",
    "ModelIds",
    "RebalanceCadence",
    "RebalanceUnit",
    "Roster",
    "RosterError",
    "RoundLimits",
    "UniverseFloor",
    "canonical_json",
    "load_roster",
    "mandate_hash",
    "parse_roster",
]
