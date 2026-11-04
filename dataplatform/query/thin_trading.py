"""D4: read the thinly-traded label (D7) as of a date — what strategies and Ops ask.

`dataplatform.quality.thin_trading` writes one `thin_trading_label` row per (session, ISIN) each
evening. This module is the read side: the label as it stood on the newest labelled session on or
before a date, so a backtest asking about 2026-11-03 never sees a label computed later (invariant
#7). A label session is knowable the evening it is computed, which is after the close it counts.

A label is information. Nothing here filters a universe, drops a name or changes a price; a strategy
that wants to avoid thinly-traded names asks for the label and decides for itself (owner ruling,
2026-11-01, backlog M13.8). It is not part of anything the M17 managers read.
"""

from __future__ import annotations

from collections.abc import Iterable, Sequence
from datetime import date
from typing import Any, Final

from dataplatform.quality.thin_trading import ThinTradingLabel
from dataplatform.store.db import Connection

__all__ = ["label_session_asof", "thin_trading_asof"]

_SELECT: Final = (
    "SELECT as_of, isin, window_start, sessions_observed, sessions_traded, capture_holes, "
    "traded_ratio, nse_traded_ratio, bse_traded_ratio, longest_gap_sessions, longest_gap_days, "
    "longest_gap_from, longest_gap_to, current_gap_sessions, last_traded, nse_series, bse_series, "
    "trade_to_trade, listing_status, insufficient_history, identity_seam, thin, reasons "
    "FROM thin_trading_label"
)


def label_session_asof(conn: Connection, as_of: date) -> date | None:
    """The newest labelled session on or before `as_of`, or `None` when there is none yet."""
    row = conn.execute(
        "SELECT max(as_of) FROM thin_trading_label WHERE as_of <= %s", (as_of,)
    ).fetchone()
    value: date | None = None if row is None else row[0]
    return value


def thin_trading_asof(
    conn: Connection,
    as_of: date,
    *,
    isins: Iterable[str] | None = None,
    thin_only: bool = False,
) -> dict[str, ThinTradingLabel]:
    """Every ISIN's label from the newest labelled session on or before `as_of`, keyed by ISIN.

    What it does: finds that session (`label_session_asof`) and returns its rows, optionally only
    the listed `isins` and/or only those marked thin. An ISIN absent from the result had no equity
    bar in the label's window — which is itself an answer, not an error.
    What it assumes: the store is migrated through 0025.
    What it never does: return a label computed for a session after `as_of`, or fall back to an
    older session for an ISIN the chosen session does not carry (that would mix two windows).
    """
    session = label_session_asof(conn, as_of)
    if session is None:
        return {}
    sql = _SELECT + " WHERE as_of = %s"
    params: list[Any] = [session]
    if isins is not None:
        sql += " AND isin = ANY(%s)"
        params.append(sorted(set(isins)))
    if thin_only:
        sql += " AND thin"
    rows = conn.execute(sql + " ORDER BY isin", params).fetchall()
    return {str(r[1]): _label(r) for r in rows}


def _label(row: Sequence[Any]) -> ThinTradingLabel:
    return ThinTradingLabel(
        as_of=row[0],
        isin=str(row[1]),
        window_start=row[2],
        sessions_observed=int(row[3]),
        sessions_traded=int(row[4]),
        capture_holes=int(row[5]),
        traded_ratio=row[6],
        nse_traded_ratio=row[7],
        bse_traded_ratio=row[8],
        longest_gap_sessions=int(row[9]),
        longest_gap_days=int(row[10]),
        longest_gap_from=row[11],
        longest_gap_to=row[12],
        current_gap_sessions=int(row[13]),
        last_traded=row[14],
        nse_series=row[15],
        bse_series=row[16],
        trade_to_trade=bool(row[17]),
        listing_status=row[18],
        insufficient_history=bool(row[19]),
        identity_seam=bool(row[20]),
        thin=bool(row[21]),
        reasons=tuple(str(x) for x in row[22]),
    )
