"""MCP host.

Starts every enabled MCP server from config.json over stdio, keeps the sessions
open in one background event loop, and exposes a thread-safe call() so the web
app and the report builder can use the tools from ordinary (sync) code.

Two tool sets per server:
  - all tools            -> callable by the host itself (report builder, connect)
  - llm_tools allowlist  -> the only tools the language model can see and call
"""
from __future__ import annotations

import asyncio
import json
import threading
from contextlib import AsyncExitStack
from datetime import timedelta
from pathlib import Path

from mcp import ClientSession
from mcp.client.stdio import StdioServerParameters, stdio_client


class McpHub:
    def __init__(self, servers_cfg: dict, log_dir: Path, start_timeout_s: float = 90.0,
                 descriptions: dict[str, str] | None = None):
        self.servers_cfg = {k: v for k, v in servers_cfg.items() if v.get("enabled", True)}
        self.log_dir = log_dir
        self.descriptions = descriptions or {}   # clearer wording for confusing tool docs
        self.start_timeout_s = start_timeout_s
        self.status: dict[str, dict] = {}
        self._sessions: dict[str, ClientSession] = {}
        self._locks: dict[str, asyncio.Lock] = {}  # one call at a time per server
        self._tools: dict[str, tuple[str, object]] = {}  # tool name -> (server, mcp Tool)
        self._llm_allow: set[str] = set()      # read-only tools
        self._write_allow: set[str] = set()    # tools that change the model
        self._loop: asyncio.AbstractEventLoop | None = None
        self._stop: asyncio.Event | None = None
        self._ready = threading.Event()
        self._thread: threading.Thread | None = None

    # ------------------------------------------------------------------ start / stop
    def start(self) -> None:
        self._thread = threading.Thread(target=self._thread_main, name="mcp-host", daemon=True)
        self._thread.start()
        if not self._ready.wait(self.start_timeout_s):
            raise RuntimeError("MCP servers did not start in time. See the logs folder.")

    def stop(self) -> None:
        if self._loop and self._stop:
            self._loop.call_soon_threadsafe(self._stop.set)
        if self._thread:
            self._thread.join(timeout=10)

    def _thread_main(self) -> None:
        try:
            asyncio.run(self._main())
        finally:
            self._ready.set()

    async def _main(self) -> None:
        self._loop = asyncio.get_running_loop()
        self._stop = asyncio.Event()
        self.log_dir.mkdir(parents=True, exist_ok=True)
        async with AsyncExitStack() as stack:
            for name, cfg in self.servers_cfg.items():
                await self._start_server(stack, name, cfg)
            self._ready.set()
            await self._stop.wait()

    async def _start_server(self, stack: AsyncExitStack, name: str, cfg: dict) -> None:
        errlog = open(self.log_dir / f"mcp_{name}.log", "a", encoding="utf-8")
        stack.callback(errlog.close)
        params = StdioServerParameters(
            command=cfg["command"],
            args=cfg.get("args", []),
            env=cfg.get("env") or None,
            cwd=cfg.get("cwd"),
        )
        try:
            read, write = await stack.enter_async_context(stdio_client(params, errlog=errlog))
            session = await stack.enter_async_context(ClientSession(read, write))
            await asyncio.wait_for(session.initialize(), timeout=60)
            tools = (await asyncio.wait_for(session.list_tools(), timeout=30)).tools
        except Exception as exc:  # noqa: BLE001 - report any start failure to the UI
            self.status[name] = {"ok": False, "error": f"{type(exc).__name__}: {exc}", "tools": 0}
            return
        self._sessions[name] = session
        self._locks[name] = asyncio.Lock()
        allow = set(cfg.get("llm_tools", []))
        write = set(cfg.get("write_tools", []))
        for tool in tools:
            self._tools[tool.name] = (name, tool)
            if tool.name in allow:
                self._llm_allow.add(tool.name)
            if tool.name in write:
                self._write_allow.add(tool.name)
        present = {t.name for t in tools}
        self.status[name] = {
            "ok": True,
            "tools": len(tools),
            "llm_tools": sorted(allow & present),
            "write_tools": sorted(write & present),
            "missing_llm_tools": sorted((allow | write) - present),
        }

    # ------------------------------------------------------------------ tools
    def llm_tools(self, write: bool = False) -> list[dict]:
        """Allowlisted tools in the Ollama / OpenAI function format."""
        out = []
        names = self._llm_allow | self._write_allow if write else self._llm_allow
        for tool_name in sorted(names):
            _, tool = self._tools[tool_name]
            schema = dict(tool.inputSchema or {"type": "object", "properties": {}})
            schema.setdefault("type", "object")
            schema.setdefault("properties", {})
            out.append({
                "type": "function",
                "function": {
                    "name": tool.name,
                    "description": self.descriptions.get(
                        tool.name, (tool.description or tool.name)).strip(),
                    "parameters": schema,
                },
            })
        return out

    def is_llm_tool(self, name: str, write: bool = False) -> bool:
        return name in self._llm_allow or (write and name in self._write_allow)

    def is_write_tool(self, name: str) -> bool:
        return name in self._write_allow

    def has_tool(self, name: str) -> bool:
        return name in self._tools

    def call(self, name: str, args: dict | None = None, timeout_s: float = 300.0) -> str:
        """Call any tool of any running server. Returns the tool's text output."""
        if name not in self._tools:
            return json.dumps({"ok": False, "error": f"Unknown tool: {name}"})
        if self._loop is None:
            return json.dumps({"ok": False, "error": "MCP host is not running"})
        fut = asyncio.run_coroutine_threadsafe(self._call(name, args or {}, timeout_s), self._loop)
        try:
            return fut.result(timeout_s + 5)
        except Exception as exc:  # noqa: BLE001
            return json.dumps({"ok": False, "error": f"{type(exc).__name__}: {exc}"})

    async def _call(self, name: str, args: dict, timeout_s: float) -> str:
        server, _ = self._tools[name]
        async with self._locks[server]:
            result = await self._sessions[server].call_tool(
                name, args, read_timeout_seconds=timedelta(seconds=timeout_s)
            )
        parts = [getattr(c, "text", "") for c in result.content if getattr(c, "type", "") == "text"]
        text = "\n".join(p for p in parts if p)
        if result.isError:
            return json.dumps({"ok": False, "error": text or "tool error"})
        return text or "(no output)"


def parse_json(text: str):
    """Parse a tool's JSON output; also finds JSON after log lines. None if absent."""
    try:
        return json.loads(text)
    except (TypeError, ValueError):
        pass
    decoder = json.JSONDecoder()
    for i, ch in enumerate(text or ""):
        if ch in "[{":
            try:
                return decoder.raw_decode(text[i:])[0]
            except ValueError:
                continue
    return None
