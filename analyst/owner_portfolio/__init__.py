"""M19 phase A — a read-only, agent-facing review of the owner's self-managed Zerodha portfolio.

Spec: ``ops/studies/kite-portfolio-review-integration-2026-11-06.md``.

- `snapshot` (M19.3): private, write-once snapshots of the account under
  ``data/private/kite/`` and their parse, `OwnerPortfolio`.
- `context` (M19.4): the lake join — per-holding market, liquidity, sector and PIT fundamentals
  context, and the portfolio-level weights and concentration.
- `tools` / `mcp_server` (M19.5): the tools an agent calls (MCP server ``kite-portfolio``) and the
  same functions as a CLI, ``uv run python -m analyst.owner_portfolio <tool>``.

Read-only by construction (I1): nothing here can send a non-GET or a non-allowlisted request, and
nothing imports `KiteBroker`. Private (I2): snapshots stay under gitignored ``data/private/``;
nothing is written to Postgres, the journal or the status API; logs carry counts and snapshot ids
only; and `analyst.fundmanager` (M17) never imports this package.
"""

from analyst.owner_portfolio.context import (
    HoldingReview,
    LakeContext,
    LakeSource,
    PortfolioReview,
    build_review,
)
from analyst.owner_portfolio.snapshot import (
    EndpointUnavailable,
    OwnerPortfolio,
    RefreshTooSoonError,
    SnapshotError,
    SnapshotInfo,
    SnapshotIntegrityError,
    SnapshotNotFoundError,
    SnapshotStore,
)
from analyst.owner_portfolio.tools import TOOL_NAMES, ReviewService, default_service

__all__ = [
    "TOOL_NAMES",
    "EndpointUnavailable",
    "HoldingReview",
    "LakeContext",
    "LakeSource",
    "OwnerPortfolio",
    "PortfolioReview",
    "RefreshTooSoonError",
    "ReviewService",
    "SnapshotError",
    "SnapshotInfo",
    "SnapshotIntegrityError",
    "SnapshotNotFoundError",
    "SnapshotStore",
    "build_review",
    "default_service",
]
