"""M17 fund-manager mandates: the typed, frozen form of the pre-registration roster.

A fund manager is a configuration, not code (pre-registration §1). This module is that
configuration's schema: `roster.yaml` holds the roster of pre-registration §8 Amendment 2 — four
managers with distinct styles, each trading a primary ₹10 L book and a mirror ₹1 cr book (8
manager books), a `CTRL-<book>` control per manager book, a secondary `STYLE-<manager>` book per
manager, and `BENCH-N500` — plus the M17 rails of §4 step 5; `load_roster` turns it into a
`Roster` of frozen pydantic models and refuses anything that does not fit.

**A manager is not a book** (Amendment 2 b). The manager (``FM-SWING-BRK``) is the decision maker:
one style, one set of starting screens, one model, one prompt. It trades two books,
``<manager>-10L`` (role ``PRIMARY``: the book it sees and decides on) and ``<manager>-1CR`` (role
``MIRROR``: driven mechanically toward the primary's weights, never shown to the manager). Each of
those books is a `ManagerMandate` carrying its manager's id, its role and the manager-level
configuration, which the roster checks is identical across the pair.

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
    computed_field,
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
    """What a book is: a manager's book, its no-LLM control, a manager's style book (Amendment 2
    d, secondary), or the market benchmark (§2/§3)."""

    MANAGER = "MANAGER"
    CONTROL = "CONTROL"
    STYLE = "STYLE"
    BENCH = "BENCH"


class HorizonStyle(StrEnum):
    """The manager's horizon family; it fixes its horizon band and its control books' rebalance
    cadence (§3, Amendment 2 d)."""

    SWING = "SWING"
    POSITIONAL = "POSITIONAL"


class ManagerStyle(StrEnum):
    """A manager's style (Amendment 2 a). Each value names the ``[[STYLE:<value>]]`` playbook of
    ``prompts/manager.md`` the manager — and only that manager — is shown (Amendment 2 f)."""

    SWING_BREAKOUT = "SWING_BREAKOUT"
    SWING_EVENT = "SWING_EVENT"
    POSITIONAL_TREND = "POSITIONAL_TREND"
    POSITIONAL_FUNDAMENTAL = "POSITIONAL_FUNDAMENTAL"

    @property
    def family(self) -> HorizonStyle:
        """The horizon family the style belongs to (swing or positional)."""
        return STYLE_FAMILY[self]


class BookRole(StrEnum):
    """Which of its manager's two books a manager book is (Amendment 2 b)."""

    PRIMARY = "PRIMARY"
    """``<manager>-10L``: the book the manager sees and decides on."""
    MIRROR = "MIRROR"
    """``<manager>-1CR``: driven mechanically toward the primary's target weights."""


#: Each style's horizon family (Amendment 2 a: BRK/EVT 5-20 sessions, TREND/FUND 20-60).
STYLE_FAMILY: Final[dict[ManagerStyle, HorizonStyle]] = {
    ManagerStyle.SWING_BREAKOUT: HorizonStyle.SWING,
    ManagerStyle.SWING_EVENT: HorizonStyle.SWING,
    ManagerStyle.POSITIONAL_TREND: HorizonStyle.POSITIONAL,
    ManagerStyle.POSITIONAL_FUNDAMENTAL: HorizonStyle.POSITIONAL,
}
#: Each family's horizon band in sessions, (min, max): §2's 1-4 weeks and 1-3 months.
FAMILY_HORIZON: Final[dict[HorizonStyle, tuple[int, int]]] = {
    HorizonStyle.SWING: (5, 20),
    HorizonStyle.POSITIONAL: (20, 60),
}
#: Each style's starting screens, in the order its playbook names them (Amendment 2 a). FUND's
#: "S4 + dossier fundamentals" starts from S4; the fundamentals are in every dossier.
STYLE_STARTING_SCREENS: Final[dict[ManagerStyle, tuple[str, ...]]] = {
    ManagerStyle.SWING_BREAKOUT: ("S2", "S3"),
    ManagerStyle.SWING_EVENT: ("S4", "S5"),
    ManagerStyle.POSITIONAL_TREND: ("S1",),
    ManagerStyle.POSITIONAL_FUNDAMENTAL: ("S4",),
}
#: A manager book's id suffix by role, and the opening capital each suffix names (Amendment 2 b).
ROLE_SUFFIX: Final[dict[BookRole, str]] = {BookRole.PRIMARY: "10L", BookRole.MIRROR: "1CR"}
SUFFIX_CAPITAL_INR: Final[dict[str, Decimal]] = {
    "10L": Decimal(1_000_000),
    "1CR": Decimal(10_000_000),
}


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
    """One book an LLM manager trades (pre-registration §2 as Amendment 2 replaces it).

    ``manager`` is the decision maker's id and ``role`` which of its two books this is; the id is
    ``<manager>-<suffix>`` with the suffix the role's (`ROLE_SUFFIX`) and the capital the suffix
    names (`SUFFIX_CAPITAL_INR`). ``style``, ``starting_screens``, ``models``, ``prompt_path`` and
    ``rounds`` are the manager's, carried on both its books and checked equal by the `Roster`.
    ``horizon`` is not configured: the style fixes it (`STYLE_FAMILY`, `FAMILY_HORIZON`), and it is
    a computed field so it is still in the canonical bytes `mandate_hash` reads.
    """

    kind: Literal[BookKind.MANAGER]
    id: StrictStr = Field(pattern=r"^FM-[A-Z0-9]+(-[A-Z0-9]+)*$")
    manager: StrictStr = Field(pattern=r"^FM-[A-Z0-9]+(-[A-Z0-9]+)*$")
    role: BookRole
    style: ManagerStyle
    starting_screens: tuple[StrictStr, ...] = Field(min_length=1)
    models: ModelIds
    prompt_path: StrictStr = Field(min_length=1)
    rounds: RoundLimits

    @computed_field  # type: ignore[prop-decorator]
    @property
    def horizon(self) -> HorizonBand:
        """The horizon the manager thinks in, fixed by its style's family (guidance, not a rail)."""
        low, high = FAMILY_HORIZON[self.style.family]
        return HorizonBand(min_sessions=low, max_sessions=high)

    @property
    def family(self) -> HorizonStyle:
        return self.style.family

    @property
    def is_primary(self) -> bool:
        return self.role is BookRole.PRIMARY

    @model_validator(mode="after")
    def _prompt_path_is_repo_relative(self) -> ManagerMandate:
        path = PurePosixPath(self.prompt_path)
        if path.is_absolute() or ".." in path.parts:
            raise ValueError(f"prompt_path {self.prompt_path!r} must be repo-relative, no '..'")
        return self

    @model_validator(mode="after")
    def _paired_by_name_and_capital(self) -> ManagerMandate:
        suffix = ROLE_SUFFIX[self.role]
        if self.id != f"{self.manager}-{suffix}":
            raise ValueError(
                f"the {self.role.value} book of {self.manager} must be named "
                f"{self.manager}-{suffix}, not {self.id}"
            )
        if self.opening_capital_inr != SUFFIX_CAPITAL_INR[suffix]:
            raise ValueError(
                f"{self.id} opens with {self.opening_capital_inr}; a -{suffix} book opens with "
                f"{SUFFIX_CAPITAL_INR[suffix]}"
            )
        return self

    @model_validator(mode="after")
    def _screens_are_the_styles(self) -> ManagerMandate:
        wanted = STYLE_STARTING_SCREENS[self.style]
        if tuple(self.starting_screens) != wanted:
            raise ValueError(
                f"{self.id}: a {self.style.value} manager starts from screens {list(wanted)}, "
                f"not {list(self.starting_screens)} (Amendment 2 a)"
            )
        return self


class ControlMandate(_BookCaps):
    """`CTRL-<book>`: equal weight over the top *N* of the composite shortlist, no LLM (§3), one
    per manager book (Amendment 2 d) — ``controls_for`` is the manager *book* it controls."""

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


class StyleMandate(_BookCaps):
    """`STYLE-<manager>`: equal weight over up to ``max_names`` names of the manager's starting
    screens, no LLM (Amendment 2 d, **secondary**: reported, never used for pass/fail).

    Which names, per style (`analyst.fundmanager.controls.style_candidates`): BRK S2 then S3, S2
    first; EVT S4; TREND S1; FUND the S4 names passing the playbook's quality filter. It runs on
    its manager's primary book's capital, caps and universe, rebalanced on its family's cadence.
    """

    kind: Literal[BookKind.STYLE]
    id: StrictStr = Field(pattern=r"^STYLE-FM-[A-Z0-9]+(-[A-Z0-9]+)*$")
    style_for: StrictStr = Field(min_length=1)
    max_names: Count
    rebalance: RebalanceCadence

    @model_validator(mode="after")
    def _id_names_its_manager(self) -> StyleMandate:
        if self.id != f"STYLE-{self.style_for}":
            raise ValueError(f"style book {self.id!r} must be named STYLE-{self.style_for}")
        return self


class BenchMandate(_Frozen):
    """`BENCH-N500`: buy-and-hold of the backtests' NIFTY 500 TRI proxy from S0 (§3)."""

    kind: Literal[BookKind.BENCH]
    id: StrictStr = Field(pattern=r"^BENCH-[A-Z0-9]+$")
    benchmark: StrictStr = Field(min_length=1)
    opening_capital_inr: Money


Mandate = Annotated[
    ManagerMandate | ControlMandate | StyleMandate | BenchMandate, Field(discriminator="kind")
]
#: Any one roster book.
AnyMandate = ManagerMandate | ControlMandate | StyleMandate | BenchMandate
#: A roster book that trades a paper account (everything but the bench).
TradableMandate = ManagerMandate | ControlMandate | StyleMandate


class Roster(_Frozen):
    """The whole M17 roster: every book, the shared rails, and the pre-registration it encodes.

    Construction cross-checks the books, so a `Roster` that exists is internally consistent:

    - ids are unique;
    - every manager has exactly one ``PRIMARY`` and one ``MIRROR`` book, which agree on the
      manager's style, starting screens, models, prompt, research limits, universe and caps
      (both hold at most the same number of positions; only the capital differs);
    - every manager book has exactly one control, carrying that book's capital, caps and
      universe, with ``shortlist_top_n`` equal to its max positions and the cadence its family
      fixes (weekly for swing, every 21 sessions for positional);
    - every manager has exactly one style book, on its primary book's capital, caps and universe,
      with ``max_names`` equal to the primary's max positions and the family's cadence;
    - there is no control or style book for anything that is not a manager book / manager.
    """

    preregistration: StrictStr = Field(min_length=1)
    rails: M17Rails
    books: tuple[Mandate, ...] = Field(min_length=1)

    @property
    def manager_books(self) -> tuple[ManagerMandate, ...]:
        """Every book a manager trades, primary and mirror, in roster order."""
        return tuple(b for b in self.books if isinstance(b, ManagerMandate))

    @property
    def primaries(self) -> tuple[ManagerMandate, ...]:
        """Each manager's primary book, in roster order: the books the managers decide on."""
        return tuple(b for b in self.manager_books if b.role is BookRole.PRIMARY)

    @property
    def mirrors(self) -> tuple[ManagerMandate, ...]:
        return tuple(b for b in self.manager_books if b.role is BookRole.MIRROR)

    @property
    def manager_ids(self) -> tuple[str, ...]:
        """The managers (decision makers), in roster order."""
        return tuple(b.manager for b in self.primaries)

    @property
    def controls(self) -> tuple[ControlMandate, ...]:
        return tuple(b for b in self.books if isinstance(b, ControlMandate))

    @property
    def styles(self) -> tuple[StyleMandate, ...]:
        return tuple(b for b in self.books if isinstance(b, StyleMandate))

    @property
    def benches(self) -> tuple[BenchMandate, ...]:
        return tuple(b for b in self.books if isinstance(b, BenchMandate))

    def get(self, book_id: str) -> AnyMandate:
        """The book with this id; `KeyError` naming it if there is none."""
        for book in self.books:
            if book.id == book_id:
                return book
        raise KeyError(f"no M17 book {book_id!r} in the roster")

    def control_for(self, book_id: str) -> ControlMandate:
        """The control book of one manager book; `KeyError` if it has none."""
        for control in self.controls:
            if control.controls_for == book_id:
                return control
        raise KeyError(f"manager book {book_id!r} has no control book")

    def books_of(self, manager_id: str) -> tuple[ManagerMandate, ...]:
        """One manager's books, primary first."""
        books = sorted(
            (b for b in self.manager_books if b.manager == manager_id),
            key=lambda b: b.role is not BookRole.PRIMARY,
        )
        if not books:
            raise KeyError(f"no M17 manager {manager_id!r} in the roster")
        return tuple(books)

    def primary_of(self, manager_id: str) -> ManagerMandate:
        """One manager's primary (₹10 L) book."""
        return self.books_of(manager_id)[0]

    def mirror_of(self, manager_id: str) -> ManagerMandate:
        """One manager's mirror (₹1 cr) book."""
        for book in self.books_of(manager_id):
            if book.role is BookRole.MIRROR:
                return book
        raise KeyError(f"manager {manager_id!r} has no mirror book")

    def style_for(self, manager_id: str) -> StyleMandate:
        """One manager's style book; `KeyError` if it has none."""
        for style in self.styles:
            if style.style_for == manager_id:
                return style
        raise KeyError(f"manager {manager_id!r} has no style book")

    @model_validator(mode="after")
    def _consistent(self) -> Roster:
        seen: set[str] = set()
        for book in self.books:
            if book.id in seen:
                raise ValueError(f"duplicate book id {book.id!r}")
            seen.add(book.id)

        by_manager: dict[str, dict[BookRole, ManagerMandate]] = {}
        for book in self.manager_books:
            roles = by_manager.setdefault(book.manager, {})
            if book.role in roles:
                raise ValueError(f"manager {book.manager!r} has two {book.role.value} books")
            roles[book.role] = book
        for manager, roles in by_manager.items():
            missing = [r.value for r in BookRole if r not in roles]
            if missing:
                raise ValueError(f"manager {manager!r} has no {missing} book")
            _check_pair(roles[BookRole.PRIMARY], roles[BookRole.MIRROR])

        manager_books = {m.id: m for m in self.manager_books}
        controlled: dict[str, str] = {}
        for control in self.controls:
            controlled_book = manager_books.get(control.controls_for)
            if controlled_book is None:
                raise ValueError(
                    f"{control.id} controls_for {control.controls_for!r}, which is not a manager "
                    "book"
                )
            if control.controls_for in controlled:
                raise ValueError(f"manager book {control.controls_for!r} has two control books")
            controlled[control.controls_for] = control.id
            _check_control_matches(control, controlled_book)
        orphans = sorted(set(manager_books) - set(controlled))
        if orphans:
            raise ValueError(f"manager books without a control book: {orphans}")

        styled: set[str] = set()
        for style in self.styles:
            pair = by_manager.get(style.style_for)
            if pair is None:
                raise ValueError(
                    f"{style.id} style_for {style.style_for!r}, which is not a manager"
                )
            if style.style_for in styled:
                raise ValueError(f"manager {style.style_for!r} has two style books")
            styled.add(style.style_for)
            _check_style_matches(style, pair[BookRole.PRIMARY])
        unstyled = sorted(set(by_manager) - styled)
        if unstyled:
            raise ValueError(f"managers without a style book: {unstyled}")
        return self


#: The control cadence each horizon family fixes (pre-registration §3, Amendment 2 d).
CONTROL_CADENCE: Final[dict[HorizonStyle, tuple[RebalanceUnit, int]]] = {
    HorizonStyle.SWING: (RebalanceUnit.WEEKLY, 1),
    HorizonStyle.POSITIONAL: (RebalanceUnit.SESSIONS, 21),
}

#: The manager-level fields both of a manager's books carry, checked equal by the roster.
_MANAGER_FIELDS: Final = (
    "style",
    "starting_screens",
    "models",
    "prompt_path",
    "rounds",
    "universe",
    "max_positions",
    "max_position_pct",
    "max_sector_pct",
)


def _check_pair(primary: ManagerMandate, mirror: ManagerMandate) -> None:
    for field in _MANAGER_FIELDS:
        if getattr(primary, field) != getattr(mirror, field):
            raise ValueError(
                f"{mirror.id}.{field} {getattr(mirror, field)!r} differs from {primary.id}."
                f"{field} {getattr(primary, field)!r}; a manager's two books share its style, "
                "screens, model, prompt, limits, universe and caps (Amendment 2 b)"
            )


def _check_style_matches(style: StyleMandate, primary: ManagerMandate) -> None:
    for field in _BookCaps.model_fields:
        if field == "id":
            continue
        if getattr(style, field) != getattr(primary, field):
            raise ValueError(
                f"{style.id}.{field} {getattr(style, field)!r} differs from "
                f"{primary.id}.{field} {getattr(primary, field)!r}; a style book runs on its "
                "manager's primary capital, caps and universe (Amendment 2 d)"
            )
    if style.max_names != primary.max_positions:
        raise ValueError(
            f"{style.id}.max_names {style.max_names} must equal {primary.id}.max_positions "
            f"{primary.max_positions}"
        )
    unit, every = CONTROL_CADENCE[primary.family]
    if (style.rebalance.unit, style.rebalance.every) != (unit, every):
        raise ValueError(
            f"{style.id} must rebalance {unit.value} every {every} for a "
            f"{primary.family.value} manager, not {style.rebalance.unit.value} "
            f"every {style.rebalance.every}"
        )


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
    unit, every = CONTROL_CADENCE[manager.family]
    if (control.rebalance.unit, control.rebalance.every) != (unit, every):
        raise ValueError(
            f"{control.id} must rebalance {unit.value} every {every} for a "
            f"{manager.family.value} manager, not {control.rebalance.unit.value} "
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
    mandate: AnyMandate,
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
    A control or bench book has no prompt or schema; pass `b""`. A manager book's canonical
    bytes carry its manager, role, style, starting screens and the horizon its style fixes.
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
