"""M19.5 · ``kite-portfolio``: the stdio MCP server an agent calls to review the owner's portfolio.

Registered in the project ``.mcp.json`` (``uv run python -m analyst.owner_portfolio.mcp_server``),
so a Claude Code session in this repo can call it. Every tool is read-only and annotated so; the
tool list is exactly `TOOL_NAMES`, pinned by a test. The tools are thin wrappers over
`ReviewService` — the CLI (``python -m analyst.owner_portfolio <tool>``) runs the same functions.

stdout is the protocol channel, so logging goes to stderr (`configure_logging`'s default).
Tool results reach the model provider as conversation content: that is the point of an agentic
review, and the owner opts in each time they ask (spec §4 I2).
"""

from __future__ import annotations

from typing import Any, Final

from mcp.server.mcpserver import MCPServer
from mcp.types import ToolAnnotations

from analyst.owner_portfolio.tools import TOOL_NAMES, ReviewService

__all__ = ["build_server", "main"]

_INSTRUCTIONS: Final[str] = (
    "Read-only review of the owner's own Zerodha (Kite) portfolio. Start with session_status; "
    "if there is no recent snapshot, call refresh (it says 'login needed' when the owner must "
    "run `uv run python -m execution.kite_session login`). Every other tool reads a stored "
    "snapshot. Holdings are keyed by ISIN; symbols are display labels. Money values are strings "
    "of exact decimals; weights and returns are fractions (0.05 = 5%). Nothing here can place, "
    "modify or cancel an order: recommend, never trade."
)
_READ_ONLY: Final = ToolAnnotations(
    read_only_hint=True, destructive_hint=False, idempotent_hint=True, open_world_hint=False
)
#: `refresh` reads Kite (an outside system) and writes a new private snapshot; still no account
#: change.
_REFRESH: Final = ToolAnnotations(
    read_only_hint=True, destructive_hint=False, idempotent_hint=False, open_world_hint=True
)


def build_server(service: ReviewService) -> MCPServer:
    """An `MCPServer` exposing exactly `TOOL_NAMES` over ``service``."""
    server: MCPServer = MCPServer(name="kite-portfolio", instructions=_INSTRUCTIONS)

    @server.tool(annotations=_READ_ONLY)
    def session_status() -> dict[str, Any]:
        """Is there a valid Kite session (expiry in IST), and the last snapshot's id and age."""
        return service.session_status()

    @server.tool(annotations=_REFRESH)
    def refresh() -> dict[str, Any]:
        """Take a new snapshot of the account (GET-only). Refused within 5 minutes of the last."""
        return service.refresh()

    @server.tool(annotations=_READ_ONLY)
    def portfolio_overview(snapshot: str | None = None) -> dict[str, Any]:
        """Totals, cash, and every holding with value, weight, P&L and a lake-context summary."""
        return service.portfolio_overview(snapshot)

    @server.tool(annotations=_READ_ONLY)
    def holding_detail(isin_or_symbol: str, snapshot: str | None = None) -> dict[str, Any]:
        """Kite's full row and the full lake context (returns, DMAs, 52w, liquidity, fundamentals,
        corporate actions) for one holding, by ISIN or display symbol."""
        return service.holding_detail(isin_or_symbol, snapshot)

    @server.tool(annotations=_READ_ONLY)
    def exposures(snapshot: str | None = None) -> dict[str, Any]:
        """Sector, size-bucket and ETF/direct-equity weights; top-5 weight and HHI; cash share."""
        return service.exposures(snapshot)

    @server.tool(annotations=_READ_ONLY)
    def changes(since: str, until: str | None = None) -> dict[str, Any]:
        """Holdings bought, sold or resized between two snapshots (ids or dates)."""
        return service.changes(since, until)

    @server.tool(annotations=_READ_ONLY)
    def orders_today(snapshot: str | None = None) -> dict[str, Any]:
        """Today's order book as captured in the snapshot."""
        return service.orders_today(snapshot)

    @server.tool(annotations=_READ_ONLY)
    def trades_today(snapshot: str | None = None) -> dict[str, Any]:
        """Today's fills as captured in the snapshot."""
        return service.trades_today(snapshot)

    @server.tool(annotations=_READ_ONLY)
    def mf_holdings(snapshot: str | None = None) -> dict[str, Any]:
        """Mutual-fund folios, SIPs and recent MF orders."""
        return service.mf_holdings(snapshot)

    @server.tool(annotations=_READ_ONLY)
    def gtt_triggers(snapshot: str | None = None) -> dict[str, Any]:
        """Standing GTT triggers."""
        return service.gtt_triggers(snapshot)

    registered = (
        session_status,
        refresh,
        portfolio_overview,
        holding_detail,
        exposures,
        changes,
        orders_today,
        trades_today,
        mf_holdings,
        gtt_triggers,
    )
    assert tuple(f.__name__ for f in registered) == TOOL_NAMES
    return server


def main() -> None:
    """Run the stdio server over the configured session file, snapshots and lake."""
    from analyst.owner_portfolio.tools import default_service, quiet_settings
    from dataplatform.clock import SystemClock
    from dataplatform.config import get_settings
    from dataplatform.logging import configure_logging

    settings = get_settings()
    clock = SystemClock(settings.tzinfo)
    # stderr (stdout is the MCP channel), warnings and up only: the lake's own INFO lines name
    # the ISINs they read, and the owner's holdings stay out of every log (spec §4 I2).
    configure_logging(quiet_settings(settings), clock=clock)
    build_server(default_service(settings, clock)).run("stdio")


if __name__ == "__main__":  # pragma: no cover
    main()
