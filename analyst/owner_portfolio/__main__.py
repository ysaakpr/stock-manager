"""``uv run python -m analyst.owner_portfolio <tool> [args]`` — the review tools as a CLI.

The same functions the MCP server exposes (`analyst.owner_portfolio.tools`), emitting JSON
(default) or a markdown rendering (``--markdown``). ``mcp`` starts the stdio MCP server.
"""

from __future__ import annotations

import argparse
import json
import sys
from typing import TYPE_CHECKING, Any, TextIO

if TYPE_CHECKING:  # pragma: no cover
    from analyst.owner_portfolio.tools import ReviewService


def _markdown(value: Any, depth: int = 0) -> str:
    """A plain markdown rendering: a list of flat dicts becomes a table, a dict a bullet list."""
    pad = "  " * depth
    if isinstance(value, dict):
        lines = []
        for key, item in value.items():
            if isinstance(item, (dict, list)) and item:
                lines.append(f"{pad}- **{key}**:")
                lines.append(_markdown(item, depth + 1))
            else:
                lines.append(f"{pad}- **{key}**: {item if item not in (None, [], {}) else '—'}")
        return "\n".join(lines)
    if isinstance(value, list):
        if value and all(isinstance(v, dict) for v in value):
            columns = list(value[0])
            if all(not isinstance(v, (dict, list)) for row in value for v in row.values()):
                head = "| " + " | ".join(columns) + " |"
                rule = "|" + "---|" * len(columns)
                body = [
                    "| "
                    + " | ".join("—" if row.get(c) is None else str(row.get(c)) for c in columns)
                    + " |"
                    for row in value
                ]
                return "\n".join([head, rule, *body])
        return "\n".join(
            _markdown(v, depth) if isinstance(v, (dict, list)) else f"{pad}- {v}" for v in value
        )
    return f"{pad}{value}"


def run_tool(service: ReviewService, args: argparse.Namespace) -> dict[str, Any]:
    tool: str = args.tool
    if tool == "session_status":
        return service.session_status()
    if tool == "refresh":
        return service.refresh()
    if tool == "holding_detail":
        return service.holding_detail(args.isin_or_symbol, snapshot=args.snapshot)
    if tool == "changes":
        return service.changes(args.since, until=args.until)
    method = getattr(service, tool)
    result: dict[str, Any] = method(snapshot=args.snapshot)
    return result


def _parser() -> argparse.ArgumentParser:
    from analyst.owner_portfolio.tools import TOOL_NAMES

    parser = argparse.ArgumentParser(
        prog="python -m analyst.owner_portfolio",
        description="Read-only review of the owner's Kite portfolio (the MCP server's tools).",
    )
    parser.add_argument("--markdown", action="store_true", help="render markdown instead of JSON")
    sub = parser.add_subparsers(dest="tool", required=True)
    for name in TOOL_NAMES:
        p = sub.add_parser(name)
        if name == "holding_detail":
            p.add_argument("isin_or_symbol")
        if name == "changes":
            p.add_argument("since", help="snapshot id (YYYY-MM-DD/HHMMSS) or a date")
            p.add_argument("--until", default=None)
        elif name not in ("session_status", "refresh"):
            p.add_argument("--snapshot", default=None, help="snapshot id; default the latest")
    sub.add_parser("mcp", help="run the stdio MCP server (kite-portfolio)")
    return parser


def main(
    argv: list[str] | None = None,
    *,
    service: ReviewService | None = None,
    stdout: TextIO | None = None,
) -> int:
    args = _parser().parse_args(argv)
    out = stdout if stdout is not None else sys.stdout
    if args.tool == "mcp":
        from analyst.owner_portfolio.mcp_server import main as serve

        serve()
        return 0
    if service is None:
        from analyst.owner_portfolio.tools import default_service, quiet_settings
        from dataplatform.clock import SystemClock
        from dataplatform.config import get_settings
        from dataplatform.logging import configure_logging

        settings = get_settings()
        clock = SystemClock(settings.tzinfo)
        configure_logging(quiet_settings(settings), clock=clock)  # holdings stay out of logs
        service = default_service(settings, clock)
    result = run_tool(service, args)
    out.write((_markdown(result) if args.markdown else json.dumps(result, indent=2)) + "\n")
    return 1 if "error" in result else 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
