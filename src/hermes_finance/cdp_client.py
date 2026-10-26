"""A tiny Chrome DevTools client (stdlib only) for the lookups that only work from a real browser page (TASK-13).

Carfax's value API sits behind bot protection (DataDome): it answers the carfax.com page's own requests, not a script's.
So a lookup runs inside the always-on local Chrome (127.0.0.1:9222):

    chrome = Chrome()
    if chrome.available() is None:
        with chrome.tab("https://www.carfax.com/value/") as tab:     # a NEW background tab, closed on the way out
            status, text = tab.fetch("https://consumerapi.carfax.com/hbv/cow/trims/<VIN>")

It only ever touches the tab it created: never another tab, never an existing tab's URL. The tab is created in the
background (Target.createTarget background=true) and closed in a `finally`, whatever happened inside.

HERMES_CHROME_DEVTOOLS (read at each use) moves the endpoint ("host:port") or switches it off ("off": nothing is ever
connected to). The test suite sets "off" for itself and every process it starts, so no test can reach the real Chrome.
"""

from __future__ import annotations

import base64
import http.client
import json
import os
import socket
import struct
import time
from contextlib import contextmanager
from typing import Any, Iterator
from urllib.parse import urlsplit

HOST = "127.0.0.1"
PORT = 9222                   # the always-on Hermes Chrome
ENV = "HERMES_CHROME_DEVTOOLS"  # "host:port", or "off"
LOAD_TIMEOUT = 30.0           # the page's load event
EVENT_WAIT = 2.0              # how long a failed fetch waits for the network log's events
CLOSE_WAIT = 5.0              # how long closing a tab waits to see it gone
FETCH_TIMEOUT = 20.0          # one in-page fetch
MAX_TEXT = 512 * 1024         # characters of a response body kept


class CDPError(Exception):
    """Talking to Chrome failed (not running, the tab crashed, a command errored, a timeout)."""


class WebSocketTimeout(TimeoutError):
    """WebSocket.send() / recv() ran out of time. `torn`: part of a message had been read or sent by then, so the
    connection was closed (see WebSocket)."""

    def __init__(self, message: str, torn: bool):
        super().__init__(message)
        self.torn = torn


class WebSocket:
    """Just enough RFC 6455 for a DevTools connection: masked text frames out; text, ping and close in.

    TEST-SPEED review B1: send() and recv() take an optional `deadline` (time.monotonic()) for the whole message, every
    read and write of it included (without one, each socket read waits the socket's own timeout, as before). When the
    time runs out (WebSocketTimeout) with nothing of a message in hand, the connection is as it was and can still be
    used (the late answer is the caller's to skip). With part of one in hand (a frame's header or payload, a fragment
    of a message, a frame half-sent), what follows could no longer be parsed: the connection is closed, `broken` says
    why, and every later send() / recv() raises ConnectionError at once.

    Review round 3: the deadline is looked at before each frame is taken, the ones already buffered included (a message
    whose fragments were all in the buffer was put together past it), and a message begun is known to be open whether
    or not any of its bytes came yet (a zero-length first fragment, then the deadline: the connection was kept, and the
    continuation read later as a message of its own). Frames out of order (a continuation with no message begun, a new
    message inside one) close the connection too (ConnectionError). What the deadline can't cut short is work already
    begun on one frame: taking it off the buffer, masking one to send, decoding the finished message (and the caller's
    json.loads of it). That is CPU time, not waiting, and not interrupted: a call can end that much after its deadline."""

    def __init__(self, url: str, timeout: float = 60):
        parts = urlsplit(url)
        self.timeout = timeout
        self.broken: str | None = None
        self.sock = socket.create_connection((parts.hostname, parts.port), timeout=timeout)
        key = base64.b64encode(os.urandom(16)).decode()
        self.sock.sendall((f"GET {parts.path} HTTP/1.1\r\nHost: {parts.hostname}:{parts.port}\r\nUpgrade: websocket\r\n"
                           f"Connection: Upgrade\r\nSec-WebSocket-Key: {key}\r\nSec-WebSocket-Version: 13\r\n\r\n").encode())
        head = b""
        while b"\r\n\r\n" not in head:
            chunk = self.sock.recv(4096)
            if not chunk:
                raise ConnectionError("DevTools closed the connection during the handshake")
            head += chunk
        head, rest = head.split(b"\r\n\r\n", 1)
        self.buf = bytearray(rest)
        if b" 101 " not in head.split(b"\r\n", 1)[0]:
            raise ConnectionError(f"websocket handshake failed: {head[:200]!r}")

    def _check(self) -> None:
        if self.broken:
            raise ConnectionError(f"DevTools connection closed: {self.broken}")

    def _wait(self, deadline: float | None) -> None:
        """Before each blocking socket call: its timeout, what is left of `deadline` (socket.timeout when nothing is),
        or the socket's own."""
        if deadline is None:
            self.sock.settimeout(self.timeout)
            return
        left = deadline - time.monotonic()
        if left <= 0:
            raise socket.timeout("out of time")
        self.sock.settimeout(left)

    def _tear(self, why: str) -> None:
        self.broken = why
        self.sock.close()

    def _timed_out(self, what: str, held: bool) -> WebSocketTimeout:
        if held and not self.broken:
            self._tear(f"out of time part-way through a message ({what})")
        return WebSocketTimeout(f"DevTools {what}: out of time" + (f"; the connection is closed ({self.broken})"
                                                                   if self.broken else ""), torn=bool(self.broken))

    def _frame_in(self) -> tuple[int, bytes] | int:
        """The whole frame at the front of buf, taken off it, (first byte, payload); or how many bytes it still lacks
        (at least 1), buf untouched: a frame is taken whole or not at all."""
        b = self.buf
        if len(b) < 2:
            return 2 - len(b)
        n, at = b[1] & 0x7F, 2
        if n == 126:
            if len(b) < 4:
                return 4 - len(b)
            n, at = struct.unpack_from("!H", b, 2)[0], 4
        elif n == 127:
            if len(b) < 10:
                return 10 - len(b)
            n, at = struct.unpack_from("!Q", b, 2)[0], 10
        if len(b) < at + n:
            return at + n - len(b)
        out = b[0], bytes(b[at:at + n])
        del b[:at + n]
        return out

    def _frame(self, opcode: int, payload: bytes, deadline: float | None = None) -> None:
        mask = os.urandom(4)
        n = len(payload)
        head = bytes([0x80 | opcode]) + (bytes([0x80 | n]) if n < 126 else struct.pack("!BH", 0x80 | 126, n)
                                         if n < 65536 else struct.pack("!BQ", 0x80 | 127, n))
        masked = (int.from_bytes(payload, "big") ^ int.from_bytes((mask * (n // 4 + 1))[:n], "big")).to_bytes(n, "big")
        self._wait(deadline)   # (socket.timeout here: nothing sent)
        try:
            self.sock.sendall(head + mask + masked)
        except socket.timeout:
            self._tear("out of time part-way through sending a frame")
            raise

    def send(self, text: str, deadline: float | None = None) -> None:
        self._check()
        try:
            self._frame(0x1, text.encode(), deadline)
        except socket.timeout:
            raise self._timed_out("send", held=False) from None

    def recv(self, deadline: float | None = None) -> str:
        """The next text message (a ping is answered on the way, a pong skipped). With `deadline`: by then, or
        WebSocketTimeout, even with messages already buffered (a stream of them never ends a wait otherwise), and
        looked at before each frame (see the class)."""
        self._check()
        parts: list[bytes] | None = None   # the message's fragments so far; None: no message begun
        try:
            while True:
                if deadline is not None and time.monotonic() >= deadline:
                    raise socket.timeout("out of time")
                frame = self._frame_in()
                if isinstance(frame, int):
                    self._wait(deadline)
                    chunk = self.sock.recv(max(65536, frame))
                    if not chunk:
                        raise ConnectionError("DevTools connection closed")
                    self.buf += chunk
                    continue
                b0, payload = frame
                opcode = b0 & 0x0F
                if opcode == 0x9:
                    self._frame(0xA, payload, deadline)
                    continue
                if opcode == 0xA:
                    continue
                if opcode == 0x8:
                    raise ConnectionError("DevTools sent close")
                if opcode not in (0x0, 0x1, 0x2) or (opcode == 0x0) != (parts is not None):
                    self._tear(f"protocol error: frame opcode {opcode:#x} "
                               + ("inside a message" if parts is not None else "with no message begun"))
                    raise ConnectionError(f"DevTools connection closed: {self.broken}")
                if parts is None:
                    parts = []
                parts.append(payload)   # (a list, joined once: appending to bytes copied the whole message each time)
                if b0 & 0x80:
                    return b"".join(parts).decode()
        except socket.timeout:
            raise self._timed_out("recv", held=parts is not None or bool(self.buf)) from None

    def settimeout(self, seconds: float) -> None:
        self.timeout = seconds
        self.sock.settimeout(seconds)

    def close(self) -> None:
        if not self.broken:
            try:
                self._frame(0x8, b"")
            except OSError:
                pass
            self.broken = self.broken or "closed"
        self.sock.close()


# In-page fetch: credentials included (the page's own cookies), aborted after `timeout` ms, the body read as text.
FETCH_JS = """(async (req) => {
  const ctl = new AbortController();
  const timer = setTimeout(() => ctl.abort(), req.timeout);
  try {
    const opts = {method: req.method, credentials: 'include', cache: 'no-store', signal: ctl.signal,
                  headers: req.headers || {}};
    if (req.body !== null) opts.body = req.body;
    const resp = await fetch(req.url, opts);
    const text = await resp.text();
    return JSON.stringify({status: resp.status, type: resp.headers.get('content-type') || '',
                           text: text.slice(0, req.max)});
  } catch (e) {
    return JSON.stringify({status: 0, error: String(e && e.name || e)});
  } finally { clearTimeout(timer); }
})(%s)"""


class Tab:
    """One background tab we created (a flat CDP session on the browser connection)."""

    def __init__(self, chrome: "Chrome", ws: WebSocket, target_id: str, session_id: str):
        self.chrome, self.ws, self.target_id, self.session_id = chrome, ws, target_id, session_id
        self.events: list[dict[str, Any]] = []   # this tab's CDP events seen while waiting for answers
        self._network = False

    def call(self, method: str, timeout: float = 30.0, **params: Any) -> dict[str, Any]:
        return self.chrome._call(self.ws, method, params, self.session_id, timeout, self.events)

    def evaluate(self, expression: str, timeout: float = 30.0) -> Any:
        r = self.call("Runtime.evaluate", timeout=timeout, expression=expression, returnByValue=True, awaitPromise=True)
        if "exceptionDetails" in r:
            d = r["exceptionDetails"]
            raise CDPError(f"page script failed: {d.get('text', '')} "
                           f"{str(d.get('exception', {}).get('description', ''))[:200]}")
        return r.get("result", {}).get("value")

    def wait_loaded(self, timeout: float = LOAD_TIMEOUT) -> str:
        """Wait until document.readyState is 'complete'; returns the page's final URL."""
        deadline = time.monotonic() + timeout
        while True:
            try:
                state = self.evaluate("[document.readyState, location.href]", timeout=5)
            except CDPError:
                state = None  # mid-navigation: the context went away; ask again
            if state and state[0] == "complete" and state[1] != "about:blank":  # not the blank page it opened on
                return state[1]
            if time.monotonic() > deadline:
                raise CDPError(f"the page did not finish loading in {timeout:.0f} s")
            time.sleep(0.25)

    def fetch(self, url: str, method: str = "GET", headers: dict[str, str] | None = None, body: str | None = None,
              timeout: float = FETCH_TIMEOUT) -> tuple[int, str]:
        """fetch() from inside the page: (HTTP status, body text). When the page's fetch() fails (a cross-origin 403
        without CORS headers, as a bot wall answers, looks exactly like that) the status and body come from the tab's
        own network log; status 0 = no response at all. Raises CDPError only when the tab can't be talked to."""
        if not self._network:
            self.call("Network.enable")
            self._network = True
        # a background tab can be frozen (Chrome's memory saver; the loaded CI runner's did it every time): its fetch()
        # and its own abort timer then never run, and the call waited out its whole timeout. Thawed first, it runs.
        self.call("Page.setWebLifecycleState", state="active")
        self.events.clear()
        req = {"url": url, "method": method, "headers": headers or {}, "body": body, "timeout": int(timeout * 1000),
               "max": MAX_TEXT}
        raw = self.evaluate(FETCH_JS % json.dumps(req), timeout=timeout + 5)
        try:
            out = json.loads(raw)
        except (TypeError, ValueError):
            raise CDPError("the page returned no fetch result") from None
        status, text = int(out.get("status") or 0), str(out.get("text") or out.get("error") or "")
        if status == 0:
            # the network log's events can land just after fetch() gave up: keep reading them for a moment
            deadline = time.monotonic() + EVENT_WAIT
            while True:
                seen = self._seen_response(url, method)
                if seen or time.monotonic() > deadline or self._request_over(url):
                    return seen or (status, text)
                self.call("Runtime.evaluate", timeout=5, expression="0")   # a round trip: pending events arrive
                time.sleep(0.05)
        return status, text

    def _request_over(self, url: str) -> bool:
        """The network log has the end of a request to `url` (loadingFailed / loadingFinished) and its response."""
        ids = {e["params"]["requestId"] for e in self.events if e.get("method") == "Network.requestWillBeSent"
               and e["params"]["request"].get("url") == url}
        ended = {e["params"].get("requestId") for e in self.events
                 if e.get("method") in ("Network.loadingFailed", "Network.loadingFinished")}
        return bool(ids) and ids <= ended and self._seen_response(url, None) is not None

    def _seen_response(self, url: str, method: str | None) -> tuple[int, str] | None:
        """The (status, body) the network log saw for the last `method` request to `url` (not a CORS preflight);
        `method` None: any method, and no body is read (just "is there one?")."""
        methods = {e["params"]["requestId"]: e["params"]["request"].get("method")
                   for e in self.events if e.get("method") == "Network.requestWillBeSent"}
        for e in reversed(self.events):
            if e.get("method") != "Network.responseReceived":
                continue
            resp = e["params"].get("response", {})
            rid = e["params"].get("requestId")
            if resp.get("url") == url and (method is None or methods.get(rid, method) == method) \
                    and e["params"].get("type") != "Preflight":
                if method is None:
                    return int(resp.get("status") or 0), ""
                try:
                    body = str(self.call("Network.getResponseBody", timeout=10, requestId=rid).get("body", ""))
                except CDPError:
                    body = ""  # a blocked response's body often isn't kept
                return int(resp.get("status") or 0), body[:MAX_TEXT]
        urls = {e["params"]["requestId"]: e["params"]["request"].get("url")
                for e in self.events if e.get("method") == "Network.requestWillBeSent"}
        for e in reversed(self.events):  # no responseReceived for it: the raw response line still has the status
            if e.get("method") == "Network.responseReceivedExtraInfo":
                rid = e["params"].get("requestId")
                if urls.get(rid) == url and (method is None or methods.get(rid) == method) and e["params"].get("statusCode"):
                    return int(e["params"]["statusCode"]), ""
        return None


class Chrome:
    """The local Chrome's DevTools endpoint. available() is a local check (no outside request). Without an explicit
    host / port it follows HERMES_CHROME_DEVTOOLS, else 127.0.0.1:9222."""

    def __init__(self, host: str | None = None, port: int | None = None):
        self._host, self._port = host, port
        self._n = 0

    def endpoint(self) -> tuple[str, int] | None:
        """(host, port) to use now, or None when switched off."""
        if self._host is not None or self._port is not None:
            return self._host or HOST, self._port or PORT
        raw = os.environ.get(ENV, "").strip()
        if raw.lower() == "off":
            return None
        if raw:
            host, _, port = raw.rpartition(":")
            if port.isdigit():
                return host or HOST, int(port)
        return HOST, PORT

    @property
    def host(self) -> str:
        return (self.endpoint() or (HOST, 0))[0]

    @property
    def port(self) -> int:
        return (self.endpoint() or (HOST, 0))[1]

    def _version(self, timeout: float = 2.0) -> dict[str, Any]:
        if self.endpoint() is None:
            raise CDPError(f"Chrome is switched off here ({ENV}=off)")
        c = http.client.HTTPConnection(self.host, self.port, timeout=timeout)
        try:
            c.request("GET", "/json/version")
            resp = c.getresponse()
            if resp.status != 200:
                raise CDPError(f"DevTools answered HTTP {resp.status}")
            return json.loads(resp.read(65536))
        finally:
            c.close()

    def available(self) -> str | None:
        """None when Chrome's DevTools answers on host:port; else why not, in words."""
        if self.endpoint() is None:
            return f"Chrome not running here ({ENV}=off)"
        try:
            info = self._version()
        except (OSError, ValueError, CDPError, http.client.HTTPException):
            return f"Chrome not running (nothing answers on {self.host}:{self.port})"
        return None if info.get("webSocketDebuggerUrl") else "Chrome's DevTools gave no browser endpoint"

    def _call(self, ws: WebSocket, method: str, params: dict[str, Any], session_id: str | None,
              timeout: float, sink: list[dict[str, Any]] | None = None) -> dict[str, Any]:
        self._n += 1
        mine = self._n
        msg: dict[str, Any] = {"id": mine, "method": method, "params": params}
        if session_id:
            msg["sessionId"] = session_id
        ws.send(json.dumps(msg))
        deadline = time.monotonic() + timeout
        while True:
            left = deadline - time.monotonic()
            if left <= 0:
                raise CDPError(f"{method}: no answer in {timeout:.0f} s")
            ws.settimeout(left)
            try:
                m = json.loads(ws.recv())
            except (socket.timeout, TimeoutError):
                raise CDPError(f"{method}: no answer in {timeout:.0f} s") from None
            except (OSError, ValueError, ConnectionError) as e:
                raise CDPError(f"{method}: the DevTools connection failed ({type(e).__name__})") from None
            if "method" in m:
                if sink is not None and m.get("sessionId") == session_id and len(sink) < 5000:
                    sink.append(m)
                continue
            if m.get("id") == mine:  # other sessions' answers are skipped
                if "error" in m:
                    raise CDPError(f"{method}: {m['error'].get('message', m['error'])}")
                return m.get("result", {})

    @contextmanager
    def tab(self, url: str, load_timeout: float = LOAD_TIMEOUT) -> Iterator[Tab]:
        """A new background tab on `url`, loaded; closed when the block ends (also on an error)."""
        if self.endpoint() is None:
            raise CDPError(f"Chrome not running here ({ENV}=off)")
        try:
            ws_url = self._version()["webSocketDebuggerUrl"]
            ws = WebSocket(ws_url, timeout=10)
        except (OSError, ValueError, KeyError, CDPError, http.client.HTTPException, ConnectionError):
            raise CDPError(f"Chrome not running (nothing answers on {self.host}:{self.port})") from None
        target_id = None
        try:
            target_id = self._call(ws, "Target.createTarget", {"url": "about:blank", "background": True,
                                                               "newWindow": False}, None, 10)["targetId"]
            session_id = self._call(ws, "Target.attachToTarget", {"targetId": target_id, "flatten": True},
                                    None, 10)["sessionId"]
            tab = Tab(self, ws, target_id, session_id)
            tab.call("Page.navigate", url=url)
            tab.wait_loaded(load_timeout)
            yield tab
        finally:
            if target_id:
                try:
                    self._call(ws, "Target.closeTarget", {"targetId": target_id}, None, 10)
                except CDPError:
                    self._close_over_http(target_id)  # the websocket broke: close it by id over plain HTTP
                if not self._gone(target_id):         # closing is asynchronous: make sure it happened
                    self._close_over_http(target_id)
            ws.close()

    def _targets(self) -> set[str] | None:
        c = http.client.HTTPConnection(self.host, self.port, timeout=5)
        try:
            c.request("GET", "/json/list")
            return {t.get("id") for t in json.loads(c.getresponse().read())}
        except (OSError, ValueError, http.client.HTTPException):
            return None
        finally:
            c.close()

    def _gone(self, target_id: str) -> bool:
        """Wait (up to CLOSE_WAIT) until the tab is no longer listed."""
        deadline = time.monotonic() + CLOSE_WAIT
        while True:
            ids = self._targets()
            if ids is not None and target_id not in ids:
                return True
            if time.monotonic() > deadline:
                return False
            time.sleep(0.1)

    def _close_over_http(self, target_id: str) -> None:
        if self.endpoint() is None:
            return
        c = http.client.HTTPConnection(self.host, self.port, timeout=5)
        try:
            c.request("GET", f"/json/close/{target_id}")
            c.getresponse().read()
        except (OSError, http.client.HTTPException):
            pass
        finally:
            c.close()
