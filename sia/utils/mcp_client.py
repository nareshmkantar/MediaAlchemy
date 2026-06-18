"""
MCP Client for SchemaAgent.

- **Option C (preferred for pipelines)**: `run_session(runner)` opens one stdio
  subprocess, one ``initialize``, reuses the same ``ClientSession`` for every
  ``runner`` callback invocation — used by ``execute_tools_node`` for the full
  tool chain (single handshake per run).

- **Standalone calls**: `call_tool` / `list_tools` use a **one-shot** session each
  (for orchestrator and other isolated callers).

Child processes inherit **full ``os.environ``** merged with MCP defaults so Windows
/ Conda / PYTHONPATH behave like the parent (avoids immediate exit → connection
closed on ``initialize``).
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import sys
import time
import traceback
from pathlib import Path
from typing import Any, Awaitable, Callable, Dict, List, Optional

logger = logging.getLogger(__name__)

TOOL_TIMEOUT_SEC = 300.0


def _stdio_subprocess_env() -> Dict[str, str]:
    try:
        from mcp.client.stdio import get_default_environment

        base = get_default_environment()
    except Exception:
        base = {}
    merged: Dict[str, str] = dict(base)
    for k, v in os.environ.items():
        if isinstance(v, str):
            merged[k] = v
    merged.setdefault("PYTHONUNBUFFERED", "1")
    merged.setdefault("PYTHONIOENCODING", "utf-8")
    return merged


def _retryable_stdio_failure(exc: BaseException) -> bool:
    try:
        from mcp.shared.exceptions import McpError

        if isinstance(exc, McpError):
            return True
    except Exception:
        pass
    msg = str(exc).lower()
    if "connection closed" in msg or "cancel scope" in msg:
        return True
    nested = getattr(exc, "exceptions", None)
    if nested:
        return any(
            _retryable_stdio_failure(x) for x in nested if isinstance(x, BaseException)
        )
    return False


def _log_bundled_exceptions(where: str, exc: BaseException) -> None:
    """
    anyio TaskGroups often surface failures as ExceptionGroup.
    The top message is unhelpful ("unhandled errors in a TaskGroup"); log leaves.
    """
    nested = getattr(exc, "exceptions", None)
    if nested:
        logger.error(
            "%s: %s with %s sub-exception(s)",
            where,
            type(exc).__name__,
            len(nested),
        )
        for i, sub in enumerate(nested, 1):
            if not isinstance(sub, BaseException):
                continue
            if getattr(sub, "exceptions", None):
                _log_bundled_exceptions(f"{where} :: group[{i}]", sub)
            else:
                logger.error("%s :: [%s] %s: %s", where, i, type(sub).__name__, sub)
                tb = getattr(sub, "__traceback__", None)
                if tb:
                    logger.error(
                        "%s",
                        "".join(traceback.format_exception(type(sub), sub, tb)).rstrip(),
                    )
    else:
        logger.error("%s: %s: %s", where, type(exc).__name__, exc)


def parse_mcp_call_tool_result(result: Any) -> Dict[str, Any]:
    """Turn MCP ``CallToolResult`` into the JSON dict envelope used by nodes/orchestrator."""
    if result.content and len(result.content) > 0:
        content = result.content[0]
        if hasattr(content, "text"):
            try:
                return json.loads(content.text)
            except json.JSONDecodeError:
                return {"ok": True, "success": True, "message": content.text}
        return {"ok": True, "success": True, "data": str(content)}
    return {"ok": True, "success": True, "message": "Tool executed successfully"}


class MCPClient:
    _instance: Optional["MCPClient"] = None

    def __new__(cls) -> "MCPClient":
        if cls._instance is None:
            cls._instance = super().__new__(cls)
            cls._instance._initialized = False
        return cls._instance

    def __init__(self) -> None:
        if getattr(self, "_initialized", False):
            return
        self._initialized = True
        self._server_script = Path(__file__).parent.parent / "mcp_server.py"

    def _stdio_params(self) -> Any:
        from mcp import StdioServerParameters

        return StdioServerParameters(
            command=sys.executable,
            args=["-u", str(self._server_script)],
            cwd=str(self._server_script.parent.parent),
            env=_stdio_subprocess_env(),
            encoding_error_handler="replace",
        )

    def _run_isolated(self, coro: Awaitable[Any]) -> Any:
        try:
            return asyncio.run(coro)
        except RuntimeError as e:
            msg = str(e).lower()
            if "cannot be called from a running event loop" not in msg and "already running" not in msg:
                raise
            loop = asyncio.new_event_loop()
            try:
                asyncio.set_event_loop(loop)
                return loop.run_until_complete(coro)
            finally:
                try:
                    loop.close()
                except Exception:
                    pass

    async def _one_shot_call_tool(self, tool_name: str, params: Dict[str, Any]) -> Dict[str, Any]:
        from mcp import ClientSession
        from mcp.client.stdio import stdio_client

        sp = self._stdio_params()
        last_err: Optional[BaseException] = None
        for attempt in range(1, 4):
            try:
                async with stdio_client(sp) as (read, write):
                    async with ClientSession(read, write) as session:
                        await session.initialize()
                        tr = await session.list_tools()
                        cache = {t.name: t for t in tr.tools}
                        if tool_name not in cache:
                            return {
                                "ok": False,
                                "success": False,
                                "message": f"Unknown tool: {tool_name}. Available: {list(cache.keys())}",
                            }
                        raw = await asyncio.wait_for(
                            session.call_tool(tool_name, params),
                            timeout=TOOL_TIMEOUT_SEC,
                        )
                        return parse_mcp_call_tool_result(raw)
            except asyncio.TimeoutError:
                err = f"MCP tool '{tool_name}' timed out after {TOOL_TIMEOUT_SEC:.0f}s"
                logger.error(err)
                return {"ok": False, "success": False, "message": err}
            except BaseException as e:
                last_err = e
                _log_bundled_exceptions(f"MCP one-shot {tool_name} attempt {attempt}/3", e)
                if attempt < 3 and _retryable_stdio_failure(e):
                    logger.warning(
                        "MCP one-shot attempt %s/3 failed for %s: %s — retrying…",
                        attempt,
                        tool_name,
                        e,
                    )
                    time.sleep(0.25 * attempt)
                    continue
                err = f"{type(e).__name__}: {e}"
                logger.error("MCP one-shot call failed: %s", err)
                return {"ok": False, "success": False, "message": err}
        return {"ok": False, "success": False, "message": str(last_err or "unknown")}

    async def _one_shot_list_tools(self) -> List[Dict[str, Any]]:
        from mcp import ClientSession
        from mcp.client.stdio import stdio_client

        sp = self._stdio_params()
        for attempt in range(1, 4):
            try:
                async with stdio_client(sp) as (read, write):
                    async with ClientSession(read, write) as session:
                        await session.initialize()
                        tr = await session.list_tools()
                        return [
                            {
                                "name": t.name,
                                "description": t.description,
                                "parameters": t.inputSchema,
                            }
                            for t in tr.tools
                        ]
            except BaseException as e:
                _log_bundled_exceptions(f"MCP list_tools attempt {attempt}/3", e)
                if attempt < 3 and _retryable_stdio_failure(e):
                    logger.warning("MCP list_tools attempt %s/3: %s — retrying…", attempt, e)
                    time.sleep(0.25 * attempt)
                    continue
                logger.error("MCP list_tools failed: %s", e)
                return []
        return []

    async def _run_shared_session(
        self,
        runner: Callable[[Any, Dict[str, Any]], Awaitable[None]],
    ) -> None:
        """One subprocess + one initialize; ``runner`` receives (session, tools_cache)."""
        from mcp import ClientSession
        from mcp.client.stdio import stdio_client

        sp = self._stdio_params()
        last_err: Optional[BaseException] = None
        for attempt in range(1, 4):
            try:
                async with stdio_client(sp) as (read, write):
                    async with ClientSession(read, write) as session:
                        await session.initialize()
                        tr = await session.list_tools()
                        tools_cache = {t.name: t for t in tr.tools}
                        await runner(session, tools_cache)
                        return
            except BaseException as e:
                last_err = e
                _log_bundled_exceptions(f"MCP shared session attempt {attempt}/3", e)
                if attempt < 3 and _retryable_stdio_failure(e):
                    logger.warning(
                        "MCP shared session attempt %s/3 failed: %s — retrying…",
                        attempt,
                        e,
                    )
                    time.sleep(0.35 * attempt)
                    continue
                logger.error("MCP shared session failed (giving up): %s", e)
                raise
        if last_err:
            raise last_err

    def run_session(
        self,
        runner: Callable[[Any, Dict[str, Any]], Awaitable[None]],
    ) -> None:
        """Sync entry: one MCP stdio session for the whole ``runner`` coroutine."""
        self._run_isolated(self._run_shared_session(runner))

    def list_tools(self) -> List[Dict[str, Any]]:
        return self._run_isolated(self._one_shot_list_tools())

    def call_tool(self, tool_name: str, params: Dict[str, Any]) -> Dict[str, Any]:
        return self._run_isolated(self._one_shot_call_tool(tool_name, params))

    def is_connected(self) -> bool:
        return False

    def get_tool_schemas(self) -> Dict[str, Any]:
        tools = self.list_tools()
        return {
            "tools": [
                {
                    "type": "function",
                    "function": {
                        "name": t["name"],
                        "description": t["description"],
                        "parameters": t["parameters"],
                    },
                }
                for t in tools
            ]
        }


_client: Optional[MCPClient] = None


def get_mcp_client() -> MCPClient:
    global _client
    if _client is None:
        _client = MCPClient()
    return _client
