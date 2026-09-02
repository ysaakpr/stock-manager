"""D7 (M2.8): the sentinel — the tripwire that catches a value nobody can explain.

The M1 gap report (`gaps.py`) answers "which *days* are we missing"; this module answers the other
half of D7: "which *values* look wrong". Its charter case (M2 gate box 3) is the price move that a
corporate action would explain but that has no corporate action behind it — a 30% close-to-close
jump on a day with no split, bonus, merger or demerger. That is either a real market event the
platform is about to trade on blind, or (far more often) an unadjusted price the CA feed missed,
and either way it must reach a human before it reaches a decision. A raised ERROR flag lands in
`quality_flag`, surfaces on `GET /status/quality`, and — because `SyncStateStore.is_green` counts
open ERROR flags for the date — blocks the agent from trading that dataset (invariant #10).

The design is a **rule registry**, not a hard-coded check, for one reason stated in the task: the
continuous track's premise is that "sentinel rules grow every time something surprises". A new
anomaly class — a volume spike, a constituent that vanished, a price that disagrees between
exchanges — must be a *new file*, not an edit to this engine. So:

* A `SentinelRule` is anything with a `name`, a `severity`, and an `evaluate(data) -> findings`.
* `register` adds one to the module registry; rules live one-per-file under
  `dataplatform/quality/rules/`, which is auto-imported so a dropped-in file self-registers.
* `run_sentinel` is the engine. It iterates the rules it is given (the registry by default) and
  collects their findings. It knows *nothing* about what any rule checks — adding a rule never
  touches this function, which is acceptance criterion 3 made structural.

Two halves, as elsewhere in D7. Everything above the "database seam" comment is pure: it takes
already-gathered facts (`SentinelInput`) and returns `QualityFinding`s, with no database and no
clock, so every rule is tested offline. `persist_findings` is the thin write — idempotent by a
per-finding fingerprint, exactly as the reconciliation queue (M2.3) is — and takes the clock
injected (B10). The read side already exists: `read_quality` (M1.11/D5) serves these flags, and
`SyncStateStore.open_error_flags` is the interlock that gates on them.

Money is `Decimal` (a float price move is the exact bug this module exists to catch). Dates are
`datetime.date`. Nothing here reads `datetime.now()`.
"""

from __future__ import annotations

import hashlib
import importlib
import json
import pkgutil
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import date
from decimal import Decimal
from typing import TYPE_CHECKING, Final, Literal, Protocol, runtime_checkable

from pydantic import BaseModel, ConfigDict, Field

from dataplatform.clock import Clock
from dataplatform.identity.master import Exchange
from dataplatform.ingest.models import ISIN_PATTERN, PriceRow
from dataplatform.logging import get_logger
from dataplatform.store.db import Connection

if TYPE_CHECKING:
    # Type-only: importing `dataplatform.ingest.corp_actions` at runtime would pull in the whole
    # `corpactions` package (its `__init__` reaches back into `ingest.corp_actions` mid-load), so a
    # module that imports the quality package before corpactions is loaded hits a circular import.
    # `SentinelInput.corporate_actions` needs the name only as an annotation, which
    # `from __future__ import annotations` keeps a string — no runtime import required.
    from dataplatform.ingest.corp_actions import CorporateAction

__all__ = [
    "CloseToCloseMove",
    "ExchangeClose",
    "PersistFindingCounts",
    "QualityFinding",
    "SentinelInput",
    "SentinelRule",
    "Severity",
    "default_rules",
    "finding_fingerprint",
    "moves_from_price_rows",
    "persist_findings",
    "register",
    "registered_rules",
    "run_sentinel",
]

_LOG = get_logger(__name__)

#: The three `quality_flag.severity` levels. Only ERROR stops trading (`open_error_flags`); WARN
#: and INFO are operator-attention signals that leave the interlock green.
Severity = Literal["INFO", "WARN", "ERROR"]


class CloseToCloseMove(BaseModel):
    """One security's close-to-close price change on one session — a rule's atomic input.

    A daily bhavcopy row already carries both ends of the move (`prev_close`, `close`), so one
    `PriceRow` maps to exactly one of these (`moves_from_price_rows`). `date` is the session of
    `close`; a corporate action explains this move when its ex-date equals `date`, because that is
    the session the price gap actually lands on. `source` is the dataset the row came from, carried
    so a raised flag scopes to that dataset in the trading interlock rather than halting the whole
    market.

    Both prices are the raw, unadjusted closes from L1 (invariant #3). Comparing adjusted prices
    here would hide the very split the sentinel is trying to notice went unrecorded.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    isin: str = Field(pattern=ISIN_PATTERN)
    date: date
    prev_close: Decimal = Field(gt=0, description="Prior session's raw close, INR.")
    close: Decimal = Field(gt=0, description="This session's raw close, INR.")
    source: str = Field(min_length=1, description="Originating dataset id, for flag scoping.")

    @property
    def pct_change(self) -> Decimal:
        """Signed close-to-close return, e.g. ``Decimal('0.30')`` for a +30% jump."""
        return (self.close - self.prev_close) / self.prev_close


class ExchangeClose(BaseModel):
    """One security's raw close on one exchange for one session — the cross-exchange rule's input.

    A dual-listed ISIN prints on both NSE and BSE, and the two closes should agree: it is the same
    company, and cross-listing arbitrage keeps them within a fraction of a percent. When they do
    not, one side's close is wrong (a missed corporate-action adjustment, a decimal shift, a stale
    print), which is exactly the kind of value a sentinel exists to catch. So the cross-exchange
    rule needs each exchange's close *tagged with its exchange*, which `CloseToCloseMove` (a
    single-series, single-exchange move) deliberately is not — hence this second, exchange-keyed
    close type.

    `turnover` (rupee value traded) is carried because the whole difficulty of this check is thin
    liquidity: a BSE close backed by a handful of shares is a stale sliver, not a second opinion on
    price, and a divergence there is noise, not a data error (D2's `LiquidityMetric` already fixes
    turnover as the platform's liquidity yardstick). `volume` is kept alongside for diagnostics.
    `close` is the raw, unadjusted close from L1 (invariant #3): comparing adjusted prices would
    paper over the very cross-exchange adjustment gap the rule is trying to notice.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    isin: str = Field(pattern=ISIN_PATTERN)
    date: date
    exchange: Exchange
    close: Decimal = Field(gt=0, description="This session's raw close on this exchange, INR.")
    turnover: Decimal = Field(
        ge=0, description="Rupee value traded in the session (the liquidity yardstick)."
    )
    volume: int = Field(
        default=0, ge=0, description="Shares traded; diagnostic, turnover is the measure."
    )
    source: str = Field(min_length=1, description="Originating dataset id, for flag scoping.")


@dataclass(frozen=True)
class SentinelInput:
    """Everything the sentinel's rules read, gathered once and passed to each rule unchanged.

    A deliberately open container: the move rule reads `moves`, `corporate_actions` and
    `circuit_bands`; the cross-exchange rule (M3.3) reads `exchange_closes`; a future volume rule
    would read a field this dataclass grows. Growing the *input* is not changing the *engine* —
    `run_sentinel` never inspects these fields, it only hands the whole object to each rule.
    `corporate_actions` should be the reconciled set (the only kind M2.4 trusts); `circuit_bands`
    maps ISIN → the fractional daily price band (e.g. `0.05` for a 5% band), used to recognise a
    move that merely rode the exchange's own limit. `exchange_closes` carries each exchange's raw
    close for an ISIN/session so the cross-exchange rule can compare NSE against BSE.
    """

    moves: tuple[CloseToCloseMove, ...] = ()
    corporate_actions: tuple[CorporateAction, ...] = ()
    circuit_bands: Mapping[str, Decimal] = field(default_factory=dict)
    exchange_closes: tuple[ExchangeClose, ...] = ()


def finding_fingerprint(check_name: str, isin: str | None, logical_date: date) -> str:
    """A stable id for "this check, this instrument, this date" — what makes a re-run idempotent.

    Re-running the sentinel over the same session must recognise a flag it already raised rather
    than stack a duplicate, so the fingerprint deliberately excludes the observed value: the same
    anomaly on the same (check, isin, date) is the same finding even if a later re-parse nudges the
    number. `persist_findings` dedupes on it, exactly as the reconciliation queue does (M2.3).
    """
    parts = [check_name, isin or "*", logical_date.isoformat()]
    digest = hashlib.sha256("\x1f".join(parts).encode("utf-8")).hexdigest()
    return digest[:32]


class QualityFinding(BaseModel):
    """One thing a rule found wrong — the pure result, before it becomes a `quality_flag` row.

    Mirrors the columns `persist_findings` writes and `read_quality` reads back: `observed_value`
    is what the check measured (the signed move, exact — never a float), `threshold` what it was
    measured against, `detail` the check-specific context a human needs, and `fingerprint` the
    dedupe key (folded into `detail` on write so the store's `detail->>'fingerprint'` predicate can
    find it). `source` is set to the finding's dataset so an ERROR scopes to that dataset in the
    interlock; a `None` source would count market-wide and let one bad ISIN halt everything.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    logical_date: date
    check_name: str = Field(min_length=1)
    severity: Severity
    isin: str | None = Field(default=None, pattern=ISIN_PATTERN)
    source: str | None = None
    observed_value: Decimal | None = None
    threshold: Decimal | None = None
    detail: dict[str, object] = Field(default_factory=dict)
    fingerprint: str = Field(min_length=1)


@runtime_checkable
class SentinelRule(Protocol):
    """The one contract a sentinel rule satisfies. Structural, so a rule is any object with these.

    `name` is its `quality_flag.check_name` (one name per rule, so `/status/quality` filters to a
    rule with one predicate); `severity` is the level it raises at; `evaluate` maps the shared
    input to zero or more findings. A rule is pure — it reads `data`, it does not touch a database
    or a clock. Adding one is writing a new class that satisfies this and registering it; the engine
    does not change.

    `name` and `severity` are read-only properties in the protocol so a frozen dataclass — the
    natural way to write a pure, immutable rule — satisfies it; a plain class with class attributes
    satisfies it too, since a settable attribute is a superset of a read-only one.
    """

    @property
    def name(self) -> str:
        """The rule's `quality_flag.check_name`."""
        ...

    @property
    def severity(self) -> Severity:
        """The level this rule raises findings at."""
        ...

    def evaluate(self, data: SentinelInput) -> Iterable[QualityFinding]:
        """Findings this rule draws from `data`. Empty when it finds nothing to flag."""
        ...


# The registry. A rule module calls `register` at import time; `default_rules` returns the whole
# set, sorted by name so a run's findings are deterministic regardless of import order.
_REGISTRY: dict[str, SentinelRule] = {}
_RULES_PACKAGE: Final = "dataplatform.quality.rules"
_discovered = False


def register(rule: SentinelRule) -> SentinelRule:
    """Add `rule` to the registry, keyed by its `name`. Returns it, so it reads as a decorator.

    Raises on a duplicate name rather than silently overwriting: two rules under one `check_name`
    would make `/status/quality` ambiguous and one of them invisible. Re-registering the *same*
    object (a module imported twice) is a no-op, which keeps auto-discovery idempotent.
    """
    existing = _REGISTRY.get(rule.name)
    if existing is not None and existing is not rule:
        raise ValueError(
            f"a different sentinel rule is already registered under {rule.name!r}; each rule needs "
            f"a unique check_name so /status/quality can filter to it unambiguously"
        )
    _REGISTRY[rule.name] = rule
    return rule


def _discover() -> None:
    """Import every module under `rules/` once, so dropped-in rule files self-register.

    This is what makes "adding a rule is one file" literally true: a new file in the package is
    picked up with no edit to this engine or to any manifest. Import side effects do the
    registration; `register`'s duplicate handling keeps a re-import safe.
    """
    global _discovered
    if _discovered:
        return
    package = importlib.import_module(_RULES_PACKAGE)
    for info in pkgutil.iter_modules(package.__path__):
        if not info.name.startswith("_"):
            importlib.import_module(f"{_RULES_PACKAGE}.{info.name}")
    _discovered = True


def registered_rules() -> tuple[SentinelRule, ...]:
    """Every registered rule, name-sorted. Triggers discovery on first use."""
    _discover()
    return tuple(_REGISTRY[name] for name in sorted(_REGISTRY))


def default_rules() -> tuple[SentinelRule, ...]:
    """The rule set `run_sentinel` uses when a caller does not name one — the whole registry."""
    return registered_rules()


def run_sentinel(
    data: SentinelInput, *, rules: Sequence[SentinelRule] | None = None
) -> tuple[QualityFinding, ...]:
    """Run every rule over `data` and return the findings, deduped and deterministically ordered.

    What it does: hands the shared input to each rule and gathers what they return. Passing an
    explicit `rules` runs exactly those (how a test proves the engine is rule-agnostic); omitting
    it runs the registry (`default_rules`). Findings are deduped by fingerprint — two rules, or one
    rule twice, reporting the same (check, isin, date) yield one finding — and sorted by
    (date, isin, check_name) so a run is reproducible.

    What it never does: know what any rule checks, write anything, or read a clock. This is the
    whole point: a new rule changes the *inputs* to this loop, never the loop.
    """
    active = tuple(rules) if rules is not None else default_rules()
    seen: dict[str, QualityFinding] = {}
    for rule in active:
        for finding in rule.evaluate(data):
            seen.setdefault(finding.fingerprint, finding)
    findings = sorted(
        seen.values(),
        key=lambda f: (f.logical_date, f.isin or "", f.check_name),
    )
    _LOG.info(
        "sentinel.run",
        rules=[rule.name for rule in active],
        moves=len(data.moves),
        findings=len(findings),
        state="EVALUATED",
    )
    return tuple(findings)


# ── database seam ─────────────────────────────────────────────────────────────────────────────
#
# Everything above is pure. Below is the thin write: turn findings into `quality_flag` rows,
# skipping any whose fingerprint already has an open flag (idempotent re-runs). The caller owns the
# transaction, exactly as the reconciliation writer (M2.3) does, so a day's ingest and its sentinel
# pass can share one commit. The read side is somebody else's module already (read_quality).

_FLAG_EXISTS_SQL: Final = (
    "SELECT 1 FROM quality_flag "
    "WHERE check_name = %s AND NOT resolved AND detail->>'fingerprint' = %s LIMIT 1"
)

_INSERT_FLAG_SQL: Final = (
    "INSERT INTO quality_flag "
    "(logical_date, check_name, severity, isin, source, observed_value, threshold, detail, "
    "raised_at) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)"
)


class PersistFindingCounts(BaseModel):
    """What `persist_findings` changed. `skipped` are findings already open from a prior run."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    written: int = 0
    skipped: int = 0


def persist_findings(
    conn: Connection, findings: Iterable[QualityFinding], *, clock: Clock
) -> PersistFindingCounts:
    """Write sentinel findings to `quality_flag`, one row each, skipping ones already open.

    For each finding, checks for an open flag with the same fingerprint and inserts only if none
    exists — so re-running the sentinel over a session it already scanned adds nothing. The
    fingerprint is folded into `detail` so the store's `detail->>'fingerprint'` predicate (the same
    one `read_quality` neighbours) can find it. `raised_at` comes from the injected clock, never the
    wall clock. Does not commit: the caller owns the transaction.
    """
    written = 0
    skipped = 0
    now = clock.now()
    for finding in findings:
        exists = conn.execute(
            _FLAG_EXISTS_SQL, (finding.check_name, finding.fingerprint)
        ).fetchone()
        if exists is not None:
            skipped += 1
            continue
        detail = {**finding.detail, "fingerprint": finding.fingerprint}
        conn.execute(
            _INSERT_FLAG_SQL,
            (
                finding.logical_date,
                finding.check_name,
                finding.severity,
                finding.isin,
                finding.source,
                finding.observed_value,
                finding.threshold,
                json.dumps(detail),
                now,
            ),
        )
        written += 1

    counts = PersistFindingCounts(written=written, skipped=skipped)
    _LOG.info(
        "sentinel.persisted",
        written=counts.written,
        skipped=counts.skipped,
        state="FLAGGED",
    )
    return counts


def moves_from_price_rows(rows: Iterable[PriceRow], *, source: str) -> tuple[CloseToCloseMove, ...]:
    """Build close-to-close moves from a day's price rows, using each row's own `prev_close`.

    A bhavcopy row self-contains both ends of the session's move, so this is a per-row map, not a
    cross-session join. Rows whose `prev_close` is not positive are skipped: the odd-lot and
    thinly-traded series publish `0` there (see `PriceRow.last`), and a move is undefined against a
    zero base — a divide-by-zero, not an anomaly. `source` is stamped on each move so a raised flag
    scopes to the dataset the row came from.
    """
    moves: list[CloseToCloseMove] = []
    for row in rows:
        if row.prev_close <= 0 or row.close <= 0:
            continue
        moves.append(
            CloseToCloseMove(
                isin=row.isin,
                date=row.trade_date,
                prev_close=row.prev_close,
                close=row.close,
                source=source,
            )
        )
    return tuple(moves)
