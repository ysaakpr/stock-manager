"""X2: a run's daily NAV, persisted beside its ledger — pre-tax as replayed, after-tax as derived.

Every runner samples its book's NAV after each session's fills to strike a max drawdown
(``backtest.run``), and until this module the path was thrown away once the drawdown was known.
A Sharpe ratio — and the deflated Sharpe ratio the round-2 decision rule needs
(``ops/studies/preregistration-signals-2026-09-29.md`` §4) — is struck from the *daily* return
series, so the path is now kept: ``<dir>/navs/<digest>.json`` next to ``ledgers/<digest>.json``,
written by ``backtest.run_ledger.persist_run`` before the run's summary (the summary is the
completion marker, so a summary on disk means its NAV file is complete too).

**Pre-tax is what the book did.** A point is ``(session, NAV)`` marked at each held name's last seen
close; a session on which some held name had not yet printed is skipped by the sampler rather than
guessed, so the series is dated and may have gaps. It is money, so it stays ``Decimal``.

**After-tax is derived, never replayed.** Tax in this repo is an investor-level outflow paid from
outside the book on the ``PaymentTiming`` date (``backtest.tax``), so the book's walk is untouched.
The after-tax NAV is the investor's wealth on the same terms: the pre-tax NAV less every tax payment
made on or before that session. Tax that falls due *after* the run's last session (the FY the run
ended in) is charged on the last point, because the after-tax XIRR counts it too and a series that
stopped before the bill would be kinder than the figure it sits beside. Written as
``navs/<digest>.after-tax.<profile>.json``, one file per investor profile.

**Same inputs, same bytes.** Sorted keys, ISO dates, money as exact ``Decimal`` strings, no clock.

What this module never does: replay a run, read a clock, or turn a NAV into a float — only
:func:`daily_returns` does that, for the statistics in ``backtest.sharpe``, which are not money.
"""

from __future__ import annotations

import json
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from datetime import date
from decimal import Decimal
from itertools import pairwise
from pathlib import Path
from typing import Any

from backtest.tax import FyTax, InvestorProfile

__all__ = [
    "PRE_TAX",
    "NavFormatError",
    "NavSeries",
    "after_tax_nav",
    "after_tax_nav_file",
    "daily_returns",
    "nav_file",
    "profile_key",
    "read_nav",
    "write_nav",
]

_ZERO = Decimal("0")
_VERSION = 1

PRE_TAX = "pre-tax"


class NavFormatError(ValueError):
    """A NAV file is not the version-1 document this module writes, or disagrees with its name."""


@dataclass(frozen=True, slots=True)
class NavSeries:
    """One run's dated NAV path on one basis (pre-tax, or after-tax for a named profile)."""

    digest: str
    basis: str
    points: tuple[tuple[date, Decimal], ...]

    def __post_init__(self) -> None:
        for (earlier, _), (later, _) in pairwise(self.points):
            if later <= earlier:
                raise NavFormatError(
                    f"NAV {self.digest[:12]}: sessions not strictly increasing at {later}"
                )

    def to_document(self) -> dict[str, Any]:
        return {
            "version": _VERSION,
            "digest": self.digest,
            "basis": self.basis,
            "points": [[when.isoformat(), str(nav)] for when, nav in self.points],
        }

    @classmethod
    def from_document(cls, doc: Mapping[str, Any]) -> NavSeries:
        if doc.get("version") != _VERSION:
            raise NavFormatError("not a version-1 NAV document")
        return cls(
            digest=str(doc["digest"]),
            basis=str(doc["basis"]),
            points=tuple((date.fromisoformat(d), Decimal(v)) for d, v in doc["points"]),
        )


def nav_file(out_dir: Path, digest: str) -> Path:
    """Where the run with ``digest`` keeps its pre-tax NAV path."""
    return out_dir / "navs" / f"{digest}.json"


def profile_key(profile: InvestorProfile) -> str:
    """A file-name-safe, human-readable identity of ``profile`` — every field that moves the tax."""
    cess = "schedule" if profile.cess_override is None else str(profile.cess_override)
    return (
        f"slab{profile.slab_rate}-cgsur{profile.cg_surcharge_rate}-"
        f"divsur{profile.dividend_surcharge_rate}-{profile.payment_timing.value}-cess{cess}"
    )


def after_tax_nav_file(out_dir: Path, digest: str, profile: InvestorProfile) -> Path:
    """Where the after-tax NAV of run ``digest`` for ``profile`` is written."""
    return out_dir / "navs" / f"{digest}.after-tax.{profile_key(profile)}.json"


def write_nav(series: NavSeries, path: Path) -> None:
    """Write ``series`` atomically; the same series always writes the same bytes."""
    path.parent.mkdir(parents=True, exist_ok=True)
    partial = path.with_name(path.name + ".partial")
    partial.write_text(
        json.dumps(series.to_document(), indent=1, sort_keys=True) + "\n", encoding="utf-8"
    )
    partial.replace(path)


def read_nav(path: Path, *, digest: str | None = None) -> NavSeries:
    """Read a NAV file; ``NavFormatError`` if it names a different run than ``digest``."""
    series = NavSeries.from_document(json.loads(path.read_text(encoding="utf-8")))
    if digest is not None and series.digest != digest:
        raise NavFormatError(f"{path}: carries digest {series.digest}, not {digest}")
    return series


def after_tax_nav(
    series: NavSeries, fy_taxes: Iterable[FyTax], profile: InvestorProfile
) -> NavSeries:
    """``series`` less the cumulative tax paid on or before each point (see the module docstring).

    Assumes ``fy_taxes`` were struck from this run's own ledger for ``profile``
    (``backtest.tax.compute_after_tax``). Tax due after the last point is charged on the last
    point. Never changes the pre-tax series.
    """
    if not series.points:
        return NavSeries(series.digest, f"after-tax {profile_key(profile)}", ())
    payments = sorted((f.payment_date, f.total) for f in fy_taxes if f.total > _ZERO)
    last = series.points[-1][0]
    out: list[tuple[date, Decimal]] = []
    paid = _ZERO
    index = 0
    for when, nav in series.points:
        while index < len(payments) and (payments[index][0] <= when or when == last):
            paid += payments[index][1]
            index += 1
        out.append((when, nav - paid))
    return NavSeries(series.digest, f"after-tax {profile_key(profile)}", tuple(out))


def daily_returns(points: Sequence[tuple[date, Decimal]]) -> list[float]:
    """Simple returns between consecutive points, as floats for the Sharpe statistics.

    Float on purpose: a Sharpe ratio is a statistic over returns, not money, and the normal
    quantiles it is compared against are floats anyway. Raises ``ValueError`` on a non-positive
    NAV, which would make a return meaningless.
    """
    returns: list[float] = []
    for (_, prior), (when, nav) in pairwise(points):
        if prior <= _ZERO:
            raise ValueError(f"non-positive NAV {prior} before {when}; no return is defined")
        returns.append(float(nav / prior - 1))
    return returns
