"""D2: the exchange's own same-session statement of which ISIN traded as `(symbol, series)`.

The identity master is built from `EQUITY_L.csv` and `symbolchange.csv`, which list *equities that
trade today*. Two populations are therefore absent from it by construction: every ETF and index
fund (NSE publishes those in a different list the platform does not ingest), and every security
that has delisted. A delivery row for either cannot be resolved through the master, and the 2026-10
audit counted the cost — BANKBEES, AUTOBEES and every other ETF had 411 of 411 sessions of delivery
quarantined in 2025-26, and 1.59 M EQ-like delivery rows across the decade were `symbol_unresolved`.

The same session's bhavcopy does know them. Both bhavcopy eras print the ISIN on every row, so the
exchange states, for that session and that exchange, exactly which ISIN traded as
`(symbol, series)`.
`SessionIdentity` is that statement, held as an identity source: it answers only for the session
it was built from, keyed by `(exchange, symbol, series, date)`.

**Why this is not a symbol join.** Invariant #2 forbids joining on a raw symbol because a symbol
is not stable *across time* — it is recycled and renamed, so a symbol-keyed table stitches two
companies together. Within one session on one exchange the symbol-and-series pair is the
exchange's own key for the instrument; the ISIN it names is the exchange's own fact for that date.
The delivery row is still placed on its price row by `(isin, series)`, and the master is still
asked first (`delivery.resolve`) — this answers only where the master has nothing to say.

**What it never does.** Answer for any other date, any other exchange, or a `(symbol, series)` the
file stated twice with different ISINs — that is ambiguous and is refused (logged and `None`),
never picked.
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass, field
from datetime import date
from typing import Protocol

from dataplatform.identity.master import Exchange
from dataplatform.logging import get_logger

__all__ = ["SessionIdentity", "SessionStatement"]

_LOG = get_logger(__name__)


class SessionStatement(Protocol):
    """One row of a session file that states an identity: what `PriceRow` already is."""

    @property
    def isin(self) -> str: ...

    @property
    def symbol(self) -> str: ...

    @property
    def series(self) -> str: ...

    @property
    def trade_date(self) -> date: ...


@dataclass(frozen=True, slots=True)
class SessionIdentity:
    """`(symbol, series) → ISIN` as one exchange's file for one session stated it.

    Build it with `from_statements` from the session's parsed bhavcopy rows. Lookups normalise the
    symbol and series the way the master does (strip, upper-case), because the delivery file and
    the bhavcopy are typed by the same exchange but not always with the same padding.
    """

    exchange: Exchange
    trade_date: date
    _isins: dict[tuple[str, str], str] = field(default_factory=dict)

    @classmethod
    def from_statements(
        cls, rows: Iterable[SessionStatement], *, exchange: Exchange, trade_date: date
    ) -> SessionIdentity:
        """Index one session's statements, dropping any `(symbol, series)` stated ambiguously.

        Raises `ValueError` on a row from another session: a statement is only evidence for the
        date it was published on, and mixing sessions is how a recycled symbol would get back in.
        """
        stated: dict[tuple[str, str], set[str]] = {}
        for row in rows:
            if row.trade_date != trade_date:
                raise ValueError(
                    f"a {trade_date.isoformat()} session identity was handed a "
                    f"{row.trade_date.isoformat()} row ({row.symbol}/{row.series}); a session's "
                    "statement is evidence for that session only"
                )
            stated.setdefault(_key(row.symbol, row.series), set()).add(row.isin)
        ambiguous = sorted(f"{s}/{r}" for (s, r), isins in stated.items() if len(isins) > 1)
        if ambiguous:
            _LOG.warning(
                "identity.session_ambiguous",
                exchange=exchange.value,
                trade_date=trade_date.isoformat(),
                keys=ambiguous,
            )
        return cls(
            exchange=exchange,
            trade_date=trade_date,
            _isins={key: next(iter(isins)) for key, isins in stated.items() if len(isins) == 1},
        )

    def try_resolve(
        self, symbol: str, series: str, on_date: date, *, exchange: Exchange
    ) -> str | None:
        """The ISIN this session's file stated for `(symbol, series)`, or `None`.

        `None` for another date or exchange as well as for an unknown or ambiguous key — this
        source has no opinion outside the session it was read from.
        """
        if on_date != self.trade_date or exchange is not self.exchange:
            return None
        return self._isins.get(_key(symbol, series))

    def __len__(self) -> int:
        return len(self._isins)


def _key(symbol: str, series: str) -> tuple[str, str]:
    return (symbol.strip().upper(), series.strip().upper())
