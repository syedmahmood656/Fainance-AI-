"""Calls the reporting tools through MCP.

For each report request we start the MCP server as a stdio subprocess, call the
chosen tool(s) in ONE session, and shut it down. That costs ~1s of start-up but
has no long-lived connection to manage - ideal for a small tracker. (If report
volume ever grows, keep a persistent session or switch to streamable HTTP.)

USE_MCP=false calls the very same tool functions in-process (debugging aid).
"""
import asyncio
import json
import os
import sys
import time
from pathlib import Path
from typing import Any, Dict, List, Tuple

from app.config.settings import settings
from app.utils.logging import logger
from app.utils.tracing import set_trace_metadata, traced

PROJECT_ROOT = str(Path(__file__).resolve().parents[2])

ToolCall = Tuple[str, Dict[str, Any]]


class ReportingToolError(RuntimeError):
    """A reporting tool failed (message is safe to log, not necessarily to show)."""


def _roundtrip(result: Any) -> Dict[str, Any]:
    return json.loads(json.dumps(result, default=str))


async def _call_direct(calls: List[ToolCall]) -> List[Dict[str, Any]]:
    from app.reporting.tools import TOOLS

    results = []
    for name, args in calls:
        if name not in TOOLS:
            raise ReportingToolError(f"Unknown tool: {name}")
        try:
            results.append(_roundtrip(await asyncio.to_thread(TOOLS[name], **args)))
        except Exception as exc:  # noqa: BLE001
            raise ReportingToolError(f"{name} failed: {exc}") from exc
    return results


async def _call_mcp(calls: List[ToolCall]) -> List[Dict[str, Any]]:
    from mcp import ClientSession, StdioServerParameters
    from mcp.client.stdio import stdio_client

    # The SDK only passes a minimal environment by default, so hand over ours
    # (DATABASE_URL etc.) explicitly.
    params = StdioServerParameters(
        command=sys.executable,
        args=["-m", "app.mcp_server.server"],
        env={**os.environ, "PYTHONUNBUFFERED": "1"},
        cwd=PROJECT_ROOT,
    )
    results: List[Dict[str, Any]] = []
    async with stdio_client(params) as (read, write):
        async with ClientSession(read, write) as session:
            await session.initialize()
            available = {tool.name for tool in (await session.list_tools()).tools}
            for name, args in calls:
                if name not in available:
                    raise ReportingToolError(f"MCP server has no tool named {name}")
                response = await session.call_tool(name, args)
                text = "".join(getattr(block, "text", "") for block in response.content)
                if response.isError:
                    raise ReportingToolError(f"{name} failed: {text[:300]}")
                try:
                    results.append(json.loads(text))
                except json.JSONDecodeError as exc:
                    raise ReportingToolError(f"{name} returned invalid JSON") from exc
    return results


@traced("mcp_tool_calls", run_type="tool")
async def call_reporting_tools(calls: List[ToolCall]) -> List[Dict[str, Any]]:
    started = time.perf_counter()
    names = [name for name, _ in calls]
    mode = "mcp" if settings.USE_MCP else "direct"
    try:
        if settings.USE_MCP:
            results = await asyncio.wait_for(_call_mcp(calls), timeout=settings.MCP_CALL_TIMEOUT_SECONDS)
        else:
            results = await _call_direct(calls)
    except asyncio.TimeoutError as exc:
        set_trace_metadata(tools=names, mode=mode, error="timeout")
        raise ReportingToolError("reporting tools timed out") from exc
    except ReportingToolError as exc:
        set_trace_metadata(tools=names, mode=mode, error=str(exc)[:200])
        raise
    except Exception as exc:  # noqa: BLE001 - e.g. subprocess failed to start
        logger.error("MCP call failed: %s", exc, exc_info=True)
        set_trace_metadata(tools=names, mode=mode, error=type(exc).__name__)
        raise ReportingToolError(f"reporting service unavailable: {exc}") from exc
    set_trace_metadata(tools=names, mode=mode, latency_ms=int((time.perf_counter() - started) * 1000))
    return results
