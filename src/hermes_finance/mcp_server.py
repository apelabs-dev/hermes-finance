"""The finance_* tools over the Model Context Protocol (TASK-17), with the official MCP Python SDK (the optional `mcp`
extra): `finance mcp` serves stdio, `finance serve` serves streamable HTTP at /mcp behind its access gate.

The tools are tooldefs.py's, the same ones (names, schemas, answers) as the Hermes plugin. Read-only by default: the
write tools are listed only while the mcp.allow_writes setting is on, and even then a call changes nothing: it is
recorded in pending_actions for the owner to decide (applying one is not done here). TASK-22's finance_sync is always
listed and runs directly, except a refresh on a paid Plaid plan (billed), which is a pending action too. Every
connection's clientInfo
(from `initialize`, or the 2026 protocol's `server/discover` envelope) is saved as mcp.last_used.
"""

from __future__ import annotations

import asyncio
import logging
import threading
from pathlib import Path
from typing import Any

import anyio
import mcp_types as types
from mcp.server import Server
from mcp.server.stdio import stdio_server
from mcp.server.streamable_http_manager import StreamableHTTPSessionManager
from mcp.server.transport_security import TransportSecuritySettings
from mcp.shared.exceptions import MCPError

from . import __version__, mcp_state, redact, tooldefs

log = logging.getLogger(__name__)

NAME = "hermes-finance"
OPENING_METHODS = ("initialize", "server/discover")   # a connection's handshake, legacy and 2026-07-28
HTTP_TIMEOUT = 60.0                                     # seconds for one /mcp request


def writes_allowed(path: Path) -> bool:
    try:
        conn = tooldefs.read_connection(path)
    except tooldefs.ToolError:
        return False
    try:
        return mcp_state.allow_writes(conn)
    finally:
        conn.close()


def _listing(t: tooldefs.Tool) -> types.Tool:
    return types.Tool(name=t.name, description=t.description, input_schema=t.schema(),
                      annotations=types.ToolAnnotations(read_only_hint=not (t.write or t.action), destructive_hint=False,
                                                        idempotent_hint=not t.write, open_world_hint=t.action))


def _client_info(ctx: Any) -> dict[str, Any] | None:
    """The connecting client's clientInfo: the initialize params, else the connection's (2026 envelope)."""
    if ctx.method == "initialize":
        info = (ctx.params or {}).get("clientInfo")
        return info if isinstance(info, dict) else None
    params = ctx.session.client_params
    if params is None or params.client_info is None:
        return None
    return params.client_info.model_dump(exclude_none=True)


def _client_name(ctx: Any) -> str | None:
    info = _client_info(ctx)
    return info.get("name") if info else None


def _record(path: Path, info: dict[str, Any], transport: str) -> None:
    try:
        conn = tooldefs.write_connection(path)
    except tooldefs.ToolError as e:
        log.info("mcp: not recording the client (%s)", e)
        return
    try:
        mcp_state.record_last_used(conn, info, transport)
    finally:
        conn.close()


def _pending(path: Path, name: str, args: Any, client: str | None,
             described: tuple[dict[str, Any], str] | None = None) -> dict[str, Any]:
    checked, summary = described or tooldefs.describe(name, args, path)
    conn = tooldefs.write_connection(path)
    try:
        action = mcp_state.record_pending(conn, name, checked, summary, client)
    finally:
        conn.close()
    return {"status": "pending_approval", "applied": False, "pending_action": action,
            "note": "Nothing was changed: the change waits for the owner's approval in hermes-finance."}


def _action(path: Path, name: str, args: Any, client: str | None) -> dict[str, Any]:
    """TASK-22, finance_sync: runs directly; a billed refresh (a paid Plaid plan) is a pending action instead."""
    checked, summary = tooldefs.approval_needed(name, args, path)
    if summary is None:
        return tooldefs.run_action(name, checked, path, tooldefs.today())
    return _pending(path, name, checked, client, (checked, summary))


def build_server(path: Path, transport: str) -> Server:
    """The MCP server for the database at `path`; `transport` (stdio | http) is what mcp.last_used says."""

    async def list_tools(ctx, params) -> types.ListToolsResult:
        writes = await anyio.to_thread.run_sync(writes_allowed, path)
        return types.ListToolsResult(tools=[_listing(t) for t in tooldefs.TOOLS.values() if writes or not t.write])

    async def call_tool(ctx, params: types.CallToolRequestParams) -> types.CallToolResult:
        t = tooldefs.TOOLS.get(params.name)
        if t is None or (t.write and not await anyio.to_thread.run_sync(writes_allowed, path)):
            raise MCPError(code=types.INVALID_PARAMS, message=f"Unknown tool: {params.name[:200]}")
        try:
            if t.write:
                payload = await anyio.to_thread.run_sync(_pending, path, t.name, params.arguments, _client_name(ctx))
            elif t.action:
                payload = await anyio.to_thread.run_sync(_action, path, t.name, params.arguments, _client_name(ctx))
            else:
                payload = await anyio.to_thread.run_sync(tooldefs.run_read, t.name, params.arguments, path,
                                                         tooldefs.today())
        except tooldefs.ToolError as e:
            return types.CallToolResult(content=[types.TextContent(type="text", text=redact.scrub(str(e)))],
                                        is_error=True)
        return types.CallToolResult(content=[types.TextContent(type="text", text=tooldefs.render(payload))])

    async def record_client(ctx, call_next):
        result = await call_next(ctx)
        if ctx.method in OPENING_METHODS:
            try:
                info = _client_info(ctx)
                if info:
                    await anyio.to_thread.run_sync(_record, path, info, transport)
            except Exception:  # never fail a handshake over the record
                log.warning("mcp: couldn't record the client", exc_info=True)
        return result

    server = Server(NAME, version=__version__, instructions=tooldefs.instructions(),
                    on_list_tools=list_tools, on_call_tool=call_tool)
    server.middleware.append(record_client)
    return server


def serve_stdio(path: Path) -> None:
    """`finance mcp`: serve one client on stdin/stdout until it disconnects."""
    server = build_server(path, "stdio")

    async def main() -> None:
        async with stdio_server() as (read, write):
            await server.run(read, write, server.create_initialization_options())

    anyio.run(main)


class HttpBridge:
    """/mcp on `finance serve`: the SDK's streamable HTTP transport (stateless, JSON responses) on its own event-loop
    thread. web.py applies its access gate (Host, Tailscale login, Origin) first and hands each request over; the
    SDK's own Host check is off because the gate already made that decision, for the tailnet host too."""

    def __init__(self, path: Path):
        self.manager = StreamableHTTPSessionManager(
            build_server(path, "http"), json_response=True, stateless=True,
            security_settings=TransportSecuritySettings(enable_dns_rebinding_protection=False))
        self._lock = threading.Lock()
        self._loop: asyncio.AbstractEventLoop | None = None
        self._stop: asyncio.Event | None = None
        self._thread: threading.Thread | None = None

    def _start(self) -> asyncio.AbstractEventLoop:
        with self._lock:
            if self._loop is None:
                loop, ready = asyncio.new_event_loop(), threading.Event()
                self._thread = threading.Thread(target=loop.run_until_complete, args=(self._main(ready),),
                                                name="mcp-http", daemon=True)
                self._thread.start()
                if not ready.wait(10):
                    raise RuntimeError("the MCP HTTP transport didn't start")
                self._loop = loop
            return self._loop

    async def _main(self, ready: threading.Event) -> None:
        self._stop = asyncio.Event()
        async with self.manager.run():
            ready.set()
            await self._stop.wait()

    def close(self) -> None:
        with self._lock:
            loop, self._loop = self._loop, None
        if loop is not None:
            loop.call_soon_threadsafe(self._stop.set)
            if self._thread is not None:
                self._thread.join(5)

    def handle(self, method: str, query: str, headers: dict[str, str], body: bytes) -> tuple[int, str, bytes]:
        """One request (headers lowercased) -> (status, content type, body)."""
        if method != "POST":  # no server-sent stream: stateless, one JSON answer per POST (spec: 405 for the GET)
            return 405, "application/json", b'{"error": "method not allowed: POST JSON-RPC messages to /mcp"}'
        loop = self._start()
        return asyncio.run_coroutine_threadsafe(self._call(query, headers, body), loop).result(HTTP_TIMEOUT)

    async def _call(self, query: str, headers: dict[str, str], body: bytes) -> tuple[int, str, bytes]:
        scope = {"type": "http", "asgi": {"version": "3.0"}, "http_version": "1.1", "method": "POST", "scheme": "http",
                 "path": "/mcp", "raw_path": b"/mcp", "root_path": "", "query_string": query.encode("latin-1"),
                 "headers": [(k.encode("latin-1"), v.encode("latin-1")) for k, v in headers.items()],
                 "client": ("127.0.0.1", 0), "server": ("127.0.0.1", 0)}
        done = asyncio.Event()
        sent = False
        status, ctype, chunks = 500, "application/json", []

        async def receive() -> dict[str, Any]:
            nonlocal sent
            if not sent:
                sent = True
                return {"type": "http.request", "body": body, "more_body": False}
            await done.wait()
            return {"type": "http.disconnect"}

        async def send(message: dict[str, Any]) -> None:
            nonlocal status, ctype
            if message["type"] == "http.response.start":
                status = message["status"]
                ctype = next((v.decode("latin-1") for k, v in message.get("headers", []) if k.lower() == b"content-type"),
                             ctype)
            elif message["type"] == "http.response.body":
                chunks.append(message.get("body", b""))
                if not message.get("more_body"):
                    done.set()

        try:
            await self.manager.handle_request(scope, receive, send)
        finally:
            done.set()
        return status, ctype, b"".join(chunks)
