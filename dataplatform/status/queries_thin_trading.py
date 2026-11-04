"""M13.8: `GET /status/thin-trading` — how many ISINs are thinly traded, and which.

The body of the endpoint and of the console Data page's "Thinly traded" panel: one function, so the
two cannot disagree. It reads the newest `thin_trading_label` session on or before the requested
date through the query layer (`dataplatform.query.thin_trading_asof`), counts it, and lists the
thin names (lowest traded share first) and the identity seams — gaps that are ours, reported
separately and never labelled.

Information only, like the label: nothing here gates trading or turns a row red. Like `queries.py`:
takes an open connection, never opens one, reads the clock or writes.
"""

from __future__ import annotations

from collections import Counter
from datetime import date
from decimal import Decimal
from typing import Final

from pydantic import BaseModel, ConfigDict, Field

from dataplatform.quality.thin_trading import (
    MIN_OBSERVED_SESSIONS,
    THIN_GAP_SESSIONS,
    THIN_RATIO_BELOW,
    WINDOW_SESSIONS,
    ThinTradingLabel,
)
from dataplatform.query.thin_trading import thin_trading_asof
from dataplatform.store.db import Connection

__all__ = [
    "THIN_TRADING_LIMIT",
    "ThinTradingOut",
    "ThinTradingRowOut",
    "ThinTradingThresholdsOut",
    "read_thin_trading_status",
]

#: The default cap on the listed names; the counts are always over every row.
THIN_TRADING_LIMIT: Final = 500

_FROZEN = ConfigDict(extra="forbid", frozen=True)


class ThinTradingThresholdsOut(BaseModel):
    """The rule the label applies, stated beside every answer so a count is never unexplained."""

    model_config = _FROZEN

    window_sessions: int
    thin_ratio_below: Decimal
    thin_gap_sessions: int
    min_observed_sessions: int


class ThinTradingRowOut(BaseModel):
    """One ISIN's label, as the endpoint lists it."""

    model_config = _FROZEN

    isin: str
    name: str | None
    traded_ratio: Decimal
    nse_traded_ratio: Decimal | None
    bse_traded_ratio: Decimal | None
    sessions_observed: int
    sessions_traded: int
    capture_holes: int
    longest_gap_sessions: int
    longest_gap_days: int
    longest_gap_from: date | None
    longest_gap_to: date | None
    current_gap_sessions: int
    last_traded: date | None
    nse_series: str | None
    bse_series: str | None
    trade_to_trade: bool
    listing_status: str | None
    reasons: list[str]


class ThinTradingOut(BaseModel):
    """The label's counts for one session and the names behind them."""

    model_config = _FROZEN

    as_of: date | None = Field(description="The label session read; None when none exists yet")
    thresholds: ThinTradingThresholdsOut
    labelled: int = Field(description="ISINs with an equity bar in the window")
    thin: int
    thin_by_reason: dict[str, int] = Field(description="ratio, gap (a name can carry both)")
    thin_nse_listed: int = Field(description="Thin names that have an NSE bar in the window")
    thin_trade_to_trade: int
    thin_by_listing_status: dict[str, int]
    trade_to_trade: int
    insufficient_history: int
    identity_seams: int = Field(description="Gaps that are ours (a reissue without lineage)")
    with_capture_holes: int = Field(description="Labels whose window has a capture hole of ours")
    names: list[ThinTradingRowOut] = Field(description="Thin names, lowest traded share first")
    seams: list[ThinTradingRowOut] = Field(description="Identity seams, reported not labelled")
    truncated: bool


def read_thin_trading_status(
    conn: Connection, *, as_of: date, limit: int = THIN_TRADING_LIMIT, nse_only: bool = False
) -> ThinTradingOut:
    """The newest label session on or before `as_of`: counts, thin names and identity seams.

    `nse_only` narrows the listed names (not the counts) to ISINs with an NSE bar in the window.
    """
    labels = thin_trading_asof(conn, as_of)
    rows = list(labels.values())
    session = rows[0].as_of if rows else None
    names = _names(conn, [lab.isin for lab in rows if lab.thin or lab.identity_seam])
    thin = sorted((lab for lab in rows if lab.thin), key=lambda lab: (lab.traded_ratio, lab.isin))
    listed = [lab for lab in thin if not nse_only or lab.nse_series is not None]
    seams = sorted((lab for lab in rows if lab.identity_seam), key=lambda lab: lab.isin)
    reasons: Counter[str] = Counter(r for lab in thin for r in lab.reasons)
    status: Counter[str] = Counter(lab.listing_status or "UNKNOWN" for lab in thin)
    return ThinTradingOut(
        as_of=session,
        thresholds=ThinTradingThresholdsOut(
            window_sessions=WINDOW_SESSIONS,
            thin_ratio_below=THIN_RATIO_BELOW,
            thin_gap_sessions=THIN_GAP_SESSIONS,
            min_observed_sessions=MIN_OBSERVED_SESSIONS,
        ),
        labelled=len(rows),
        thin=len(thin),
        thin_by_reason=dict(sorted(reasons.items())),
        thin_nse_listed=sum(1 for lab in thin if lab.nse_series is not None),
        thin_trade_to_trade=sum(1 for lab in thin if lab.trade_to_trade),
        thin_by_listing_status=dict(sorted(status.items())),
        trade_to_trade=sum(1 for lab in rows if lab.trade_to_trade),
        insufficient_history=sum(1 for lab in rows if lab.insufficient_history),
        identity_seams=len(seams),
        with_capture_holes=sum(1 for lab in rows if lab.capture_holes),
        names=[_row(lab, names) for lab in listed[:limit]],
        seams=[_row(lab, names) for lab in seams[:limit]],
        truncated=len(listed) > limit or len(seams) > limit,
    )


def _names(conn: Connection, isins: list[str]) -> dict[str, str]:
    if not isins:
        return {}
    rows = conn.execute(
        "SELECT isin, name FROM security_master WHERE isin = ANY(%s)", (isins,)
    ).fetchall()
    return {str(r[0]): str(r[1]) for r in rows}


def _row(lab: ThinTradingLabel, names: dict[str, str]) -> ThinTradingRowOut:
    return ThinTradingRowOut(
        isin=lab.isin,
        name=names.get(lab.isin),
        traded_ratio=lab.traded_ratio,
        nse_traded_ratio=lab.nse_traded_ratio,
        bse_traded_ratio=lab.bse_traded_ratio,
        sessions_observed=lab.sessions_observed,
        sessions_traded=lab.sessions_traded,
        capture_holes=lab.capture_holes,
        longest_gap_sessions=lab.longest_gap_sessions,
        longest_gap_days=lab.longest_gap_days,
        longest_gap_from=lab.longest_gap_from,
        longest_gap_to=lab.longest_gap_to,
        current_gap_sessions=lab.current_gap_sessions,
        last_traded=lab.last_traded,
        nse_series=lab.nse_series,
        bse_series=lab.bse_series,
        trade_to_trade=lab.trade_to_trade,
        listing_status=lab.listing_status,
        reasons=list(lab.reasons),
    )
