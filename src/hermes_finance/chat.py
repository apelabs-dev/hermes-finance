"""The Ask panel's server side (TASK-23): a chat about the owner's own money, with the app owning the tool loop.

    GET  /api/chat              the brain (mode, the header chip's label), whether Ask is on, this month's AI spend
    POST /api/chat              {messages: [{role: user|assistant, content}]}: one turn -> {reply, sources, cards, stats}
    GET  /api/chat/cards?ids=   the approval cards' current state (one decided in Settings shows its final state)
    POST /api/chat/decide       {id, approve}: Approve / Deny on a card

Brains (prefs ai.mode):

  api     an OpenAI-compatible /chat/completions at ai.base_url with ai.model and the secrets file's AI_API_KEY. The app
          sends the finance_* tool schemas (tooldefs.TOOLS) and runs every tool call itself, up to MAX_ROUNDS rounds:
          a read goes through tooldefs.run_read (exactly `finance <command> --json`); a write (and a finance_sync that
          needs approval) goes through tooldefs.describe / approval_needed and becomes a pending action
          (mcp_state.record_pending, origin "ask", client "Ask"), shown in the panel as an approval card; nothing is
          applied here. The card is held (held_by the turn) until the turn answers: only then does Settings list it and
          can anyone decide it (mcp_state.release); a turn that fails withdraws it (mcp_state.withdraw).
          Tool results sent to the model go through tooldefs.render (redact.scrub), exactly as the MCP server and the
          Hermes plugin send them. Each request is counted before it's sent (usage.ai_reserve) and its cost recorded
          (usage.ai_finish): the response's usage.cost when the endpoint reports one (OpenRouter), else an estimate from
          its token counts at EST_IN / EST_OUT dollars per million tokens (deliberately high); a request that timed out,
          at its worst case (its input and MAX_OUTPUT_TOKENS). ai.monthly_cap is a soft cap, a stop after settlement:
          the requests go out one at a time per database (a turn waits while another's request is out), each asks for
          at most MAX_OUTPUT_TOKENS, and none is sent once the settled spend reached the cap. So the month can end over
          the cap by at most the one request that crossed it. At the cap Ask pauses with a one-line message.
  hermes  Hermes's own model and auth, through the hermes-finance plugin's completion bridge (bridge.py: the app runs
          `hermes finance completion`, one child process per request, which makes one ctx.llm.complete_structured
          call). Hermes's model gets no tools: each request asks for exactly one JSON decision (an answer, one read,
          or one proposed change), which the app checks itself (bridge.decision: never Hermes's own schema check, which
          the optional jsonschema package decides). The app owns the rest, as in the api mode: a read runs here
          (_isolated), its result goes back with the next request, and its source line is the app's own; a proposed
          change is an approval card (origin "ask_hermes", held by the turn), every finance_sync included (the turn
          never syncs itself). The same rounds, deadline, spend cap and at-most-one-request-out rule as the api mode
          (each request reserved before its child starts, its cost an estimate from Hermes's token counts: Hermes
          reports no price). The app never reads Hermes's keys or settings. Without Hermes, or without the plugin's
          bridge, nothing is answered: one line says so (never a made-up or canned answer).
  off     no Ask at all: the page has no button and every /api/chat route answers 404.

A turn has one deadline, TIMEOUT from its start (time.monotonic()), for all of it (chat-deadline correction and its
follow-up):
  * a request gets what's left of it for the whole request, not for each socket read: the name looked up (in a child
    process, killed and waited for at the deadline: getaddrinfo can't be interrupted), connecting, TLS, sending, and the
    answer's headers and body however slowly they trickle in (_Wire sets each read and write's timeout to what's left);
    an answer bigger than MAX_RESPONSE, cut off or garbled isn't read;
  * no step starts after it (a request's reservation, a tool, a Hermes read, finance_sync, a database try: only the
    bookkeeping's may, until SETTLE after it), and the answer is published only before it: the cards are released under
    the write lock, and only if the deadline hasn't passed and every card the turn recorded is still its own
    (_publish), so reconcile() (HELD_STALE) can never withdraw a card its turn then tells the owner about;
  * a tool step that only reads (a read tool, a write's plan and card text, finance_sync's approval check) runs in a
    child process killed at the deadline (_isolated): in this process nothing could cut it short (a model's rule regex
    can backtrack for hours, a query or a plan over many rows has no clock, a read waits on a lock by SQLite's);
  * the turn's own connections never wait for a lock themselves (busy_timeout 0 from their first statement, the schema
    check and db.connect's pragmas included: _writer) and are tried again by the turn's clock (_tries: not SQLite's
    busy timeout, which counts the sleeps it asks for, not the time they take).
What that bounds: a turn publishes by TIMEOUT or not at all, and a request is out for at most TIMEOUT (what
usage.AI_INFLIGHT_STALE relies on). What it doesn't:
  * the bookkeeping after a request (its cost; withdrawing a failed turn's cards) is TRIED until SETTLE after the
    deadline, not guaranteed written by then: a database another connection keeps locked past that leaves the request's
    reservation 'pending' (ai_reserve settles it 'interrupted' after AI_INFLIGHT_STALE, at no cost: its worst-case
    estimate is never recorded) and the cards held, undecidable, until reconcile() withdraws them after HELD_STALE;
    both stale sweeps compare wall-clock timestamps. A cost is an estimate (EST_IN / EST_OUT) when the endpoint reports
    none, and a timed-out request is charged its worst case although closing the socket cancels nothing at the provider;
  * a finance_sync tool call (the app's own Plaid sync, in this process: it writes) never starts after the deadline but
    can't be cut short once started: a turn it holds past the deadline answers 504 and withdraws its cards;
  * the in-process work between steps (json, rendering and scrubbing a tool's result, making cards) is bounded by its
    size, not by the clock; nor are starting a child process, the scheduler, or reaping a SIGKILLed child.
A Hermes request is a child process too (bridge.complete): started only with bridge.MIN_CALL seconds left, killed with
its process group at the deadline (LATE) and after its answer (none outlives the request), its answer read to
bridge.MAX_OUT bytes at most; it also ends by itself (an alarm set before Hermes starts) if the app is gone.

Every expected failure (Ask off, no key or model, the endpoint down or refusing the key, a timeout, the cap) is a
ChatError: one friendly line for the panel, never a traceback. A turn that fails after it recorded a card (a ChatError or
anything unexpected) withdraws that card, which no one could decide yet, so nothing is left half-done; if even that
can't be written, the card stays held and mcp_state.reconcile() withdraws it later, and the turn keeps its own error.
The panel reads and decides only Ask's own cards (origin "ask" or "ask_hermes", which no client can claim by calling
itself "Ask"); another agent's pending actions are decided in Settings.
"""

from __future__ import annotations

import contextlib
import hashlib
import http.client
import io
import json
import logging
import math
import os
import re
import secrets
import selectors
import signal
import socket
import sqlite3
import ssl
import subprocess
import sys
import threading
import time
import weakref
from datetime import date
from pathlib import Path
from typing import Any, Callable
from urllib.parse import urlsplit

from . import access, aisession, api, bridge, config, mcp_state, prefs, redact, secretstore, tooldefs, usage
from .money import money

log = logging.getLogger(__name__)

CLIENT = "Ask"                     # pending_actions.client (the name shown) for a change asked for in the Ask panel
ORIGIN = "ask"                     # pending_actions.origin: what makes it the panel's own card (mcp_state.ORIGINS)
ORIGIN_HERMES = "ask_hermes"       # ... one a Hermes-mode turn recorded (through the completion bridge)
ASK_ORIGINS = (ORIGIN, ORIGIN_HERMES)
VIA_HERMES = "hermes"              # usage.ai_finish's `via` for a request through the bridge
MAX_OUTPUT_TOKENS = 1024           # the most a request asks the model to write (max_tokens; OpenAI: max_completion_tokens)
WAIT_STEP = 0.2                    # seconds between two looks while another turn's request is out
DB_STEP = 0.05                     # seconds between two tries of a turn's write while another writer holds the database
MAX_ROUNDS = 6                     # tool rounds per turn
TIMEOUT = 60.0                     # seconds for a whole turn: its deadline (each request gets what's left, for all of it)
SETTLE = 30.0                      # seconds past the deadline the bookkeeping is still tried (not sure to be written)
MAX_RESPONSE = 1 << 20             # bytes of an answer at most (its headers included): a bigger one isn't read
MAX_WORKER_OUT = 128 * 1024        # bytes read from one tool worker, before its JSON is decoded
MAX_TURNS = 2                      # whole turns admitted at once per app; excess requests retry instead of piling up
MAX_MESSAGES = 40                  # history the page may send
MAX_CONTENT = 4000                 # characters per message
EST_IN, EST_OUT = 3.0, 15.0        # $ per million tokens when the endpoint reports no cost (a mid-range model's price)
PRESET_NAMES = {"openrouter": "OpenRouter", "openai": "OpenAI", "local": "Local", "custom": "Custom"}
TOOL_TITLES = {"finance_set_cap": "Set a budget cap", "finance_set_category": "File a transaction",
               "finance_add_rule": "Add a rule", "finance_track_bill": "Track a bill", "finance_sync": "Ask banks for fresh data"}
MONTHS = ("Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec")
LATE = "Ask took too long to answer (over a minute). Try again."
# what _lookup's child process runs: getaddrinfo's answer for argv[1]:argv[2] as JSON, [family, type, proto, address] each
LOOKUP = ("import json, socket, sys; print(json.dumps([[f, t, p, list(a)] for f, t, p, _, a in "
          "socket.getaddrinfo(sys.argv[1], int(sys.argv[2]), type=socket.SOCK_STREAM)]))")
# what _isolated's child process runs: its own alarm first (argv[2] seconds: it ends by then even if the app that
# started it is gone; SIGALRM isn't caught, so it ends the process), then _worker from this same code (argv[1]: its
# directory)
WORKER = ("import signal, sys; signal.alarm(int(sys.argv[2])); sys.path.insert(0, sys.argv[1]); "
          "from hermes_finance import chat; chat._worker()")
CODE = str(Path(__file__).resolve().parent.parent)   # the directory hermes_finance is imported from


class ChatError(Exception):
    """A turn that can't be answered: `status` for the HTTP answer, the message is the panel's one line."""

    def __init__(self, message: str, status: int = 502):
        super().__init__(message)
        self.status = status


class ToolOutputTooLarge(Exception):
    """A read/plan worker exceeded MAX_WORKER_OUT before its stdout could be buffered."""


_admission_lock = threading.Lock()
_admission: weakref.WeakKeyDictionary[Any, threading.BoundedSemaphore] = weakref.WeakKeyDictionary()


def _turn_slot(app):
    """The bounded whole-turn admission slot for this App instance."""
    with _admission_lock:
        slots = _admission.setdefault(app, threading.BoundedSemaphore(MAX_TURNS))
    return slots


# --- the brain -------------------------------------------------------------------------------------------------------

def brain(conn) -> dict[str, Any]:
    """What the header chip says and where a turn goes."""
    p = prefs.get_many(conn, ["ai.mode", "ai.preset", "ai.base_url", "ai.model", "ai.monthly_cap"])
    mode = p["ai.mode"]
    if mode == "hermes":   # Hermes's own model (its choice, never named here), under the same spend cap
        return {"mode": mode, "label": "via Hermes", "cap": p["ai.monthly_cap"]}
    if mode == "api":
        model = p["ai.model"]
        name = PRESET_NAMES.get(p["ai.preset"], "API")
        short = model.split("/")[-1] if model else "no model set"
        return {"mode": mode, "label": f"via {name} · {short}", "url": p["ai.base_url"], "model": model,
                "preset": p["ai.preset"], "cap": p["ai.monthly_cap"]}
    return {"mode": "off", "label": None}


def state(app, conn) -> dict[str, Any]:
    b = brain(conn)
    out = {"mode": b["mode"], "enabled": b["mode"] != "off", "label": b["label"]}
    if b["mode"] in ("api", "hermes"):
        spent = usage.ai_spent(conn, app.today())
        out.update(cap=b["cap"], spent=round(spent, 2), paused=spent >= b["cap"])
    if b["mode"] == "api":
        out.update(key_set=bool(secretstore.status()["ai_key"]["set"]), model=b["model"])
    return out


# --- one request -----------------------------------------------------------------------------------------------------

def _left(deadline: float) -> float:
    """Seconds until `deadline` (time.monotonic()); TimeoutError (socket.timeout) once there are none."""
    left = deadline - time.monotonic()
    if left <= 0:
        raise TimeoutError("out of time")
    return left


class _TooBig(http.client.HTTPException):
    """The answer is longer than MAX_RESPONSE bytes (it isn't read any further)."""


def _lookup(host: str, port: int, deadline: float) -> list[tuple]:
    """A name's addresses (getaddrinfo), looked up in a child process: here, getaddrinfo can't be interrupted or given a
    timeout. One that hasn't answered by the deadline is killed (it's alone in its own process group) and waited for,
    so none outlives the request: TimeoutError (at once, with none started, when there's no time left). A name it
    can't find: OSError."""
    _left(deadline)
    proc = subprocess.Popen([sys.executable, "-I", "-S", "-c", LOOKUP, host, str(port)], stdin=subprocess.DEVNULL,
                            stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, start_new_session=True)
    try:
        out, _ = proc.communicate(timeout=max(0.0, deadline - time.monotonic()))
    except subprocess.TimeoutExpired:
        raise TimeoutError(f"looking up {host} took too long") from None
    finally:
        if proc.poll() is None:   # still running, so never reaped: its pid and its group are still its own
            # (gone, or a zombie-only group: macOS refuses a killpg there with EPERM rather than ESRCH)
            with contextlib.suppress(ProcessLookupError, PermissionError):
                os.killpg(proc.pid, signal.SIGKILL)
            proc.wait()           # (SIGKILL can't be caught or ignored)
        proc.stdout.close()
    if proc.returncode != 0:
        raise OSError(f"no address for {host}")
    return [(family, kind, proto, "", tuple(addr)) for family, kind, proto, addr in json.loads(out)]


def _open(host: str, port: int, deadline: float) -> socket.socket:
    """A TCP connection to host:port by the deadline: an IP address as it is, a name through _lookup; each address
    tried gets what's left."""
    try:
        addresses = socket.getaddrinfo(host, port, type=socket.SOCK_STREAM, flags=socket.AI_NUMERICHOST)
    except socket.gaierror:   # not an IP address
        addresses = _lookup(host, port, deadline)
    err = OSError(f"no address for {host}")
    for family, kind, proto, _, addr in addresses:
        s = socket.socket(family, kind, proto)
        try:
            s.settimeout(_left(deadline))
            s.connect(addr)
        except TimeoutError:
            s.close()
            raise
        except OSError as e:
            s.close()
            err = e
            continue
        with contextlib.suppress(OSError):
            s.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)   # (as http.client does)
        return s
    raise err


class _Wire:
    """One request's socket as http.client uses it (sendall, makefile): each write and each read waits only what's left
    until the deadline (TimeoutError after it), so sending and the whole answer end by then, however slowly the bytes
    come; and the answer is read to MAX_RESPONSE bytes at most (_TooBig)."""

    def __init__(self, sock: socket.socket, deadline: float):
        self.sock, self.deadline, self.got = sock, deadline, 0

    def sendall(self, data) -> None:
        view = memoryview(data).cast("B")
        while view:
            self.sock.settimeout(_left(self.deadline))
            view = view[self.sock.send(view):]

    def makefile(self, mode: str) -> io.BufferedReader:   # HTTPResponse reads the answer through it
        return io.BufferedReader(_Reader(self))

    def close(self) -> None:
        """Nothing: http.client closes its connection before the answer's body is read; _post closes the socket."""


class _Reader(io.RawIOBase):
    def __init__(self, wire: _Wire):
        self.wire = wire

    def readable(self) -> bool:
        return True

    def readinto(self, b) -> int:
        w = self.wire
        w.sock.settimeout(_left(w.deadline))
        n = w.sock.recv_into(b, min(len(b), MAX_RESPONSE + 1 - w.got))
        w.got += n
        if w.got > MAX_RESPONSE:
            raise _TooBig(f"the answer is over {MAX_RESPONSE} bytes")
        return n


class _Connection(http.client.HTTPConnection):
    """http.client's request and answer over a _Wire: connect() looks the name up, connects and does TLS by the
    deadline too."""

    def __init__(self, host: str, port: int | None, tls: bool, deadline: float):
        self.default_port = http.client.HTTPS_PORT if tls else http.client.HTTP_PORT   # (read by __init__ and Host:)
        super().__init__(host, port or self.default_port)
        self.tls, self.deadline, self.raw = tls, deadline, None

    def connect(self) -> None:
        self.raw = _open(self.host, self.port, self.deadline)
        if self.tls:
            context = ssl.create_default_context()   # (what http.client's HTTPSConnection uses, with its ALPN)
            context.set_alpn_protocols(["http/1.1"])
            self.raw.settimeout(_left(self.deadline))   # for the whole handshake: ssl keeps its own deadline for it
            self.raw = context.wrap_socket(self.raw, server_hostname=self.host)
        self.sock = _Wire(self.raw, self.deadline)


def _post(url: str, key: str | None, body: dict[str, Any], timeout: float,
          extra: dict[str, str] | None = None) -> dict[str, Any]:
    """POST JSON to an OpenAI-compatible endpoint (http.client over a _Wire, no proxy), all of it within `timeout`
    seconds from now: the lookup, connecting, TLS, sending, the answer's headers and its body (not `timeout` for each
    socket read: a reply trickling in never outlasts it). An answer bigger than MAX_RESPONSE, cut off or garbled isn't
    used. `extra`: more request headers (aisession.headers). ChatError with one friendly line on any failure."""
    deadline = time.monotonic() + timeout
    parts = urlsplit(url)
    try:
        port = parts.port
    except ValueError:   # not a port number
        port = -1
    if parts.scheme not in ("http", "https") or not parts.hostname or port == -1:
        raise ChatError("The AI provider's address in Settings isn't an http(s) URL.", 400)
    headers = {**(extra or {}), "Content-Type": "application/json", "Accept": "application/json"}
    if key:
        headers["Authorization"] = f"Bearer {key}"
    target = (parts.path or "/") + (f"?{parts.query}" if parts.query else "")
    down = "Couldn't reach the AI provider. Is it running, and is the address in Settings right?"
    slow = "The AI provider took too long to answer (over a minute). Try again."
    unreadable = "The AI provider sent an answer the app couldn't read."
    try:
        c = _Connection(parts.hostname, port, parts.scheme == "https", deadline)
        try:
            c.request("POST", target, json.dumps(body).encode(), headers)
            r = c.getresponse()
            status, raw = r.status, r.read(MAX_RESPONSE)
            if r.length:   # its Content-Length promised more than came before the connection closed
                raise http.client.IncompleteRead(raw, r.length)
        finally:
            c.close()
            if c.raw is not None:
                c.raw.close()
    except TimeoutError:
        raise ChatError(slow, 504) from None
    except OSError:
        raise ChatError(down) from None
    except http.client.HTTPException:   # garbled, cut off, or too big (_TooBig)
        raise ChatError(unreadable) from None
    if status in (401, 403):
        raise ChatError(f"The AI provider refused the key ({status}). Check it in Settings \u2192 AI provider.")
    if status == 404:
        raise ChatError("The AI provider doesn't know that model or address (404). Check Settings \u2192 AI provider.")
    if status == 429:
        raise ChatError("The AI provider says too many requests (429). Try again in a minute.", 429)
    if status >= 400:
        raise ChatError(f"The AI provider answered with an error ({status}). Try again in a minute.")
    try:
        out = json.loads(raw)
        msg = out["choices"][0]["message"]
        if not isinstance(msg, dict):
            raise TypeError
    except (ValueError, KeyError, IndexError, TypeError):
        raise ChatError(unreadable) from None
    return out


def _cost(resp: dict[str, Any], sent: dict[str, Any]) -> tuple[float, int, int, bool]:
    """(dollars, input tokens, output tokens, estimated?) for one response."""
    u = resp.get("usage") if isinstance(resp.get("usage"), dict) else {}
    tin = u.get("prompt_tokens") if isinstance(u.get("prompt_tokens"), int) else None
    tout = u.get("completion_tokens") if isinstance(u.get("completion_tokens"), int) else None
    if tin is None:
        tin = len(json.dumps(sent)) // 3            # no token counts: about 3 characters a token, rounded up in cost
    if tout is None:
        tout = len(json.dumps(resp["choices"][0]["message"])) // 3
    cost = u.get("cost")
    if isinstance(cost, (int, float)) and not isinstance(cost, bool) and cost >= 0:
        return float(cost), tin, tout, False
    return tin * EST_IN / 1e6 + tout * EST_OUT / 1e6, tin, tout, True


# --- source lines ----------------------------------------------------------------------------------------------------

def _month_label(month: str) -> str:
    y, m = month.split("-")
    return f"{MONTHS[int(m) - 1]} {y}"


def _day_label(iso: str) -> str:
    y, m, d = iso.split("-")
    return f"{MONTHS[int(m) - 1]} {int(d)}, {y}"


def source_line(name: str, payload: Any) -> str:
    """'from budget-status · Sep 2026': the read's command and the period it covered."""
    cmd = tooldefs.TOOLS[name].command or name
    p = payload if isinstance(payload, dict) else {}
    when = None
    period = p.get("period")
    if isinstance(period, dict):
        if period.get("kind") == "month" and period.get("month"):
            when = _month_label(period["month"])
        elif period.get("kind") == "all":
            when = "all time"
        elif period.get("start") and period.get("end"):
            when = f"{_day_label(period['start'])} – {_day_label(period['end'])}"
    elif isinstance(p.get("month"), str) and re.fullmatch(r"\d{4}-\d{2}", p["month"]):
        when = _month_label(p["month"])
    elif isinstance(p.get("next_days"), int):
        when = f"next {p['next_days']} days"
    elif isinstance(p.get("months"), list) and p["months"]:
        first, last = p["months"][0].get("month"), p["months"][-1].get("month")
        if first and last:
            when = f"{_month_label(first)} – {_month_label(last)}"
    if when is None and isinstance(p.get("today"), str):
        when = f"as of {_day_label(p['today'])}"
    return f"from {cmd}" + (f" · {when}" if when else "")


def _stats(reads: list[tuple[str, Any]], reply: str) -> list[dict[str, Any]]:
    """The budget rings under an answer: each capped category the answer names, from the budget-status it read."""
    out: list[dict[str, Any]] = []
    low = reply.lower()
    for name, payload in reads:
        if name != "finance_budget_status" or not isinstance(payload, dict):
            continue
        for c in payload.get("caps", []):
            if c["category"].lower() in low and all(s["category"] != c["category"] for s in out):
                out.append({k: c[k] for k in ("category", "cap", "spent", "due", "count", "status")})
    return out[:2]


# --- approval cards --------------------------------------------------------------------------------------------------

def _usd(x: Any) -> str:
    return money(x, none="no cap")


def _title(action: dict[str, Any]) -> str:
    a, tool = action["arguments"], action["tool"]
    if tool == "finance_set_cap":
        m = re.search(r"\(now \$([0-9,]+(?:\.[0-9]+)?)\)", action["summary"])
        was = float(m.group(1).replace(",", "")) if m else None
        if a.get("monthly_cap") is None:
            return f"Clear the {a['category']} cap?"
        verb = "Raise" if was is not None and a["monthly_cap"] > was else "Lower" if was is not None and a["monthly_cap"] < was else "Set"
        return f"{verb} {a['category']} cap to {_usd(a['monthly_cap'])}?"
    if tool == "finance_set_category":
        return f"File it under {a['category']}?"
    if tool == "finance_add_rule":
        return f"Always file {a['pattern']} under {a['category']}?"
    if tool == "finance_track_bill":
        return {"track": f"Track {a['merchant']} as a bill?", "update": f"Change the {a['merchant']} bill?",
                "stop": f"Stop tracking {a['merchant']}?"}.get(a.get("action"), f"Change {a['merchant']}?")
    if tool == "finance_sync":   # (a plain sync is a card only in a Hermes turn: bridge.sync_summary)
        return "Ask your banks for fresh data?" if a.get("refresh") else "Sync your banks now?"
    return TOOL_TITLES.get(tool, tool)


def _result_line(action: dict[str, Any]) -> str | None:
    status, r, a = action["status"], action.get("result") or {}, action["arguments"]
    if status == "rejected":
        return "Withdrawn with the answer that failed, nothing changed" if r.get("withdrawn") else "Declined, nothing changed"
    if status == "approved":   # claimed by an Approve whose change is being made right now (mcp_state.decide)
        return "Approved, applying it…"
    if status == "failed" and r.get("uncertain"):   # interrupted (mcp_state.decide / reconcile): what was done is unknown
        return f"Interrupted while applying it ({r.get('error')}). It may be partly done: check before asking again"
    if status == "failed":
        return f"Not applied: {r.get('error', 'it could not be done')}. Nothing changed"
    if status != "applied":
        return None
    tool = action["tool"]
    if tool == "finance_set_cap":
        cat = r.get("category", a["category"])
        cap = r.get("monthly_cap", a.get("monthly_cap"))
        return f"Saved: {cat} cap is now {_usd(cap)}" if cap is not None else f"Saved: {cat} has no cap now"
    if tool == "finance_set_category":
        return f"Saved: filed under {r.get('category', a['category'])}"
    if tool == "finance_add_rule":
        n = r.get("matched")
        return f"Saved: the rule files {n} transaction{'s' if n != 1 else ''} under {r.get('category', a['category'])}"
    if tool == "finance_track_bill":
        return f"Saved: {r.get('summary') or 'the bill is updated'}"
    if tool == "finance_sync":
        return "Done: your banks were asked for fresh data"
    return "Saved"


def card(action: dict[str, Any]) -> dict[str, Any]:
    """What the panel draws for a pending action (live buttons only while it's pending)."""
    return {"id": action["id"], "tool": action["tool"], "title": _title(action), "summary": action["summary"],
            "status": action["status"], "result_line": _result_line(action), "client": action["client"]}


def _record(path: Path, name: str, checked: dict[str, Any], summary: str, turn_id: str, deadline: float,
            origin: str = ORIGIN, owner: str | None = None) -> dict[str, Any]:
    conn = _writer(path, deadline)
    try:
        client = f"{mcp_state.ASK_OWNER_PREFIX}{owner}" if owner else CLIENT
        return _write(deadline, conn, mcp_state.record_pending, name, checked, summary, client, origin=origin,
                      held_by=turn_id)
    finally:
        conn.close()


# --- one turn --------------------------------------------------------------------------------------------------------

def _history(data: Any) -> list[dict[str, str]]:
    msgs = data.get("messages") if isinstance(data, dict) else None
    if not isinstance(msgs, list) or not msgs:
        raise api.ApiError("messages must be a list of {role, content}", field="messages")
    if len(msgs) > MAX_MESSAGES:
        msgs = msgs[-MAX_MESSAGES:]
    out = []
    for m in msgs:
        if not isinstance(m, dict) or m.get("role") not in ("user", "assistant") or not isinstance(m.get("content"), str):
            raise api.ApiError("each message is {role: user or assistant, content: text}", field="messages")
        text = m["content"].strip()
        if len(text) > MAX_CONTENT:
            raise api.ApiError(f"a message is at most {MAX_CONTENT} characters", field="messages")
        if text:
            out.append({"role": m["role"], "content": text})
    if not out or out[-1]["role"] != "user":
        raise api.ApiError("the last message must be the question", field="messages")
    return out


def _system(today: date, mode: str) -> str:
    base = (f"You are Ask, inside the owner's own personal finance app. Today is {today.isoformat()}. "
            + tooldefs.instructions() + " Answer in plain, short sentences (no tables, no markdown headings), with the "
            "exact figures the data gives, in dollars. Never guess a number.")
    if mode == "hermes":
        return base + (" Look things up before answering. Suggest at most one change per answer, only when it clearly "
                       "helps.\n\n" + bridge.contract())
    return base + (" Use the finance_* tools to look things up before answering. A write tool (finance_set_cap, "
                   "finance_set_category, finance_add_rule, finance_track_bill) changes nothing when you call it: the owner "
                   "sees an approval card and decides. Suggest at most one change per answer, only when it clearly helps, "
                   "and never say a change was made.")


def _isolated(step: str, name: str, args: Any, path: Path, day: date, deadline: float) -> Any:
    """One tool step that only reads, run in a child process by the turn's deadline: "read" (tooldefs.run_read: its
    payload), "describe" (a write's (checked arguments, summary)) or "approval" (finance_sync's approval_needed). In
    this process nothing could cut it short: re has no timeout (a model's rule regex can backtrack for hours over one
    merchant name), a query or a plan over many rows has no clock, and a read waits for a lock by SQLite's. The child is
    alone in its own process group; at the deadline it's killed and waited for (LATE), and none is started after it. It
    changes nothing: tooldefs' read connection is read-only, and the card (_record) and finance_sync stay here. What it
    answers is the step's own: its value, or its ToolError; anything else that went wrong there is a RuntimeError here
    (its type named, never its message)."""
    _in_time(deadline)
    ask = json.dumps({"step": step, "name": name, "args": args, "path": str(path), "day": day.isoformat(),
                      "today": tooldefs.today().isoformat()}).encode()
    alarm = str(math.ceil(max(0.0, deadline - time.monotonic())) + 1)
    proc = subprocess.Popen([sys.executable, "-I", "-c", WORKER, CODE, alarm], stdin=subprocess.PIPE,
                            stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, start_new_session=True)
    try:
        out = _worker_exchange(proc, ask, deadline)
    finally:
        if proc.poll() is None:   # still running, so never reaped: its pid and its group are still its own
            # (gone, or a zombie-only group: macOS refuses a killpg there with EPERM rather than ESRCH)
            with contextlib.suppress(ProcessLookupError, PermissionError):
                os.killpg(proc.pid, signal.SIGKILL)
            proc.wait()           # (SIGKILL can't be caught or ignored)
        proc.stdin.close()
        proc.stdout.close()
    try:
        answer = json.loads(out)
    except ValueError:
        answer = None
    if isinstance(answer, dict) and "ok" in answer:
        return answer["ok"]
    if isinstance(answer, dict) and isinstance(answer.get("refused"), str):
        raise tooldefs.ToolError(answer["refused"])
    why = answer.get("crashed") if isinstance(answer, dict) else f"exit {proc.returncode}, no answer"
    raise RuntimeError(f"the {step} step of {name} failed in its child process ({why})")


def _worker_exchange(proc: subprocess.Popen, body: bytes, deadline: float) -> bytes:
    """Send a worker request and read at most MAX_WORKER_OUT bytes, through one absolute deadline."""
    sel = selectors.DefaultSelector()
    os.set_blocking(proc.stdin.fileno(), False)
    os.set_blocking(proc.stdout.fileno(), False)
    sel.register(proc.stdin, selectors.EVENT_WRITE)
    sel.register(proc.stdout, selectors.EVENT_READ)
    view, chunks, got = memoryview(body), [], 0
    try:
        while sel.get_map():
            left = deadline - time.monotonic()
            if left <= 0:
                raise ChatError(LATE, 504)
            for key, _ in sel.select(left):
                if key.fileobj is proc.stdin:
                    try:
                        view = view[os.write(key.fd, view[:64 * 1024]):]
                    except BlockingIOError:
                        continue
                    except (BrokenPipeError, ConnectionResetError):
                        view = view[:0]
                    if not view:
                        sel.unregister(proc.stdin)
                        proc.stdin.close()
                else:
                    try:
                        data = os.read(key.fd, min(64 * 1024, MAX_WORKER_OUT + 1 - got))
                    except BlockingIOError:
                        continue
                    if not data:
                        sel.unregister(proc.stdout)
                        continue
                    got += len(data)
                    if got > MAX_WORKER_OUT:
                        raise ToolOutputTooLarge()
                    chunks.append(data)
    finally:
        sel.close()
    return b"".join(chunks)


def _worker() -> None:
    """_isolated's child: the step asked on stdin, its answer on stdout, JSON both ({"ok": value}, {"refused": the
    ToolError's message} or {"crashed": the exception's type}). The turn's `today` is tooldefs.today()'s there."""
    ask = json.loads(sys.stdin.buffer.read())
    tooldefs.today = lambda: date.fromisoformat(ask["today"])
    name, args, path = ask["name"], ask["args"], Path(ask["path"])
    try:
        if ask["step"] == "read":
            answer = {"ok": tooldefs.run_read(name, args, path, date.fromisoformat(ask["day"]))}
        elif ask["step"] == "describe":
            answer = {"ok": list(tooldefs.describe(name, args, path))}
        else:
            answer = {"ok": list(tooldefs.approval_needed(name, args, path))}
    except tooldefs.ToolError as e:
        answer = {"refused": str(e)}
    except Exception as e:
        answer = {"crashed": type(e).__name__}
    sys.stdout.write(json.dumps(answer, default=str))
    sys.stdout.flush()


def _run_tool(path: Path, today: date, name: str, raw_args: Any, turn_id: str,
              deadline: float, owner: str | None = None) -> tuple[str, Any, dict[str, Any] | None]:
    """(what the model gets back, the read's payload or None, the pending action (held by turn_id) or None)."""
    try:
        args = json.loads(raw_args) if isinstance(raw_args, str) and raw_args.strip() else (raw_args or {})
    except ValueError:
        return tooldefs.render({"error": "the arguments are not valid JSON"}), None, None
    try:
        t = tooldefs.tool(name)
        if t.write:
            checked, summary = _isolated("describe", name, args, path, today, deadline)
            action = _record(path, name, checked, summary, turn_id, deadline, owner=owner)
            return tooldefs.render({"status": "pending_approval", "applied": False, "summary": summary,
                                    "note": "Shown to the owner as an approval card. Nothing changes until they approve; "
                                            "don't say it's done."}), None, action
        if t.action:
            checked, summary = _isolated("approval", name, args, path, today, deadline)
            if summary is not None:
                action = _record(path, name, checked, summary, turn_id, deadline, owner=owner)
                return tooldefs.render({"status": "pending_approval", "applied": False, "summary": summary}), None, action
            # (the check may have taken until the deadline: the sync, not cut short once started, never starts after it)
            _in_time(deadline)
            payload = tooldefs.run_action(name, checked, path, today)
            return tooldefs.render(payload), None, None
        payload = _isolated("read", name, args, path, today, deadline)
        return tooldefs.render(payload), payload, None
    except ToolOutputTooLarge:
        return tooldefs.render({"error": "the tool result was too large; ask a narrower question"}), None, None
    except tooldefs.ToolError as e:
        return tooldefs.render({"error": redact.scrub(str(e))}), None, None


def _publish(path: Path, turn_id: str, recorded: int, deadline: float) -> list[dict[str, Any]]:
    """The turn answered: its cards become decidable (and listed in Settings), as they are now; returns the reply's
    cards. They're made inside the release (review N2): if making one fails, nothing is released, so the turn's
    _withdraw still finds them held and no card is left decidable that the owner never saw. It's the turn's last step,
    taken in time or not at all: the release is committed only if, with the write lock held (so no reconcile() comes
    between), the deadline hasn't passed and all `recorded` cards were still the turn's to release (one reconcile()
    withdrew would be missing from an answer that tells the owner about it). Else LATE, and nothing is released."""
    def in_time(released: int) -> None:
        if released != recorded or time.monotonic() >= deadline:
            raise ChatError(LATE, 504)

    conn = _writer(path, deadline)
    try:
        return _write(deadline, conn, mcp_state.release, turn_id, card, in_time)
    finally:
        conn.close()


def _withdraw(path: Path, turn_id: str, until: float) -> None:
    """The turn failed: its cards, which no one could decide yet, are withdrawn (tried until `until` at most, the
    connection's setup included). If that can't be written, they stay held (still undecidable) until
    mcp_state.reconcile(), and the turn's own error is the one reported."""
    try:
        conn = _writer(path, until)
        try:
            _write(until, conn, mcp_state.withdraw, turn_id)
        finally:
            conn.close()
    except Exception:
        log.warning("chat: couldn't withdraw the cards of a failed turn (reconcile() will)", exc_info=True)


def _in_time(deadline: float) -> None:
    """LATE once the turn's deadline has passed: no step starts, and no answer is given, after it."""
    if time.monotonic() >= deadline:
        raise ChatError(LATE, 504)


def _busy(e: BaseException) -> bool:
    """Another connection holds the database (SQLITE_BUSY): e itself, or the sqlite3 error a tooldefs.read_connection
    ToolError was raised from (raised `from None`, which hides that context but keeps it in __context__)."""
    return any(isinstance(x, sqlite3.OperationalError)
               and getattr(x, "sqlite_errorcode", 0) & 0xFF == sqlite3.SQLITE_BUSY for x in (e, e.__context__))


def _tries(until: float, attempt: Callable[[], Any]) -> Any:
    """attempt(), tried again DB_STEP later while another connection holds the database, each try (the first
    included) started only before `until` (time.monotonic()). Out of time: the last try's "database is locked", or
    LATE when none could start. Not SQLite's busy timeout: that adds up the sleeps it asks for, not the time they take
    (a busy_timeout of 1 s waited 2.5 s on this Mac under load), so it bounds nothing; this looks at the clock."""
    locked = None
    while time.monotonic() < until:
        try:
            return attempt()
        except (sqlite3.OperationalError, tooldefs.ToolError) as e:
            if not _busy(e):
                raise
            locked = e
        if time.monotonic() + DB_STEP >= until:
            break
        time.sleep(DB_STEP)
    if locked is None:
        raise ChatError(LATE, 504)
    raise locked


def _writer(path: Path, until: float) -> sqlite3.Connection:
    """tooldefs.write_connection (an existing database at the current schema: never created or upgraded here) with
    busy_timeout 0 from its first statement, so neither its schema check nor db.connect's pragmas wait for another
    connection's lock (30 s each by SQLite's clock); tried again by _tries until `until`. The connection keeps
    busy_timeout 0: _write waits for the lock instead."""
    return _tries(until, lambda: tooldefs.write_connection(path, timeout=0))


def _write(until: float, conn: sqlite3.Connection, fn: Callable[..., Any], *args: Any, **kwargs: Any) -> Any:
    """fn(conn, ...), one transaction on a connection that doesn't wait for the database itself (busy_timeout 0), tried
    by _tries until `until`. A try that failed committed nothing (it's rolled back), so another is safe."""
    def attempt() -> Any:
        try:
            return fn(conn, *args, **kwargs)
        except sqlite3.OperationalError:
            if conn.in_transaction:
                conn.rollback()
            raise

    return _tries(until, attempt)


def _reserve(conn, today: date, cap: float, deadline: float) -> usage.Reservation:
    """usage.ai_reserve, tried again while another turn's request is out or another writer holds the database (conn
    doesn't wait for it itself), each try (the first included) started only before the turn's deadline: LATE when none
    could start, else the last refusal."""
    refused = None
    while time.monotonic() < deadline:
        try:
            return usage.ai_reserve(conn, today, cap)
        except usage.Refused as e:
            if e.reason not in ("inflight", "busy"):
                raise ChatError(str(e), 429) from None
            refused = e
        if time.monotonic() + WAIT_STEP >= deadline:
            break
        time.sleep(WAIT_STEP)
    if refused is None:
        raise ChatError(LATE, 504)
    raise ChatError("Ask is still answering another question. Try again in a moment." if refused.reason == "inflight"
                    else str(refused), 429)


def _worst(body: dict[str, Any]) -> tuple[float, int, int]:
    """A request with no answer (timed out: the endpoint may still bill it): its input estimate and the most output it
    could have had, in dollars at EST_IN / EST_OUT."""
    tin = len(json.dumps(body)) // 3
    return tin * EST_IN / 1e6 + MAX_OUTPUT_TOKENS * EST_OUT / 1e6, tin, MAX_OUTPUT_TOKENS


def _schemas() -> list[dict[str, Any]]:
    return [{"type": "function", "function": {"name": t.name, "description": t.description, "parameters": t.schema()}}
            for t in tooldefs.TOOLS.values()]


def _text(msg: dict[str, Any]) -> str:
    c = msg.get("content")
    if isinstance(c, list):   # content parts
        c = "".join(p.get("text", "") for p in c if isinstance(p, dict))
    return (c or "").strip() if isinstance(c, str) else ""


def turn(app, conn, data: Any, owner: str | None = None) -> dict[str, Any]:
    """One question: the brain's answer, its source lines, the approval cards it asked for."""
    b = brain(conn)
    if b["mode"] == "off":
        raise ChatError("Ask is off (Settings \u2192 AI provider).", 404)
    history = _history(data)
    slots = _turn_slot(app)
    if not slots.acquire(blocking=False):
        raise ChatError("Ask is already handling as many questions as it can. Try again in a moment.", 429)
    try:
        path, today = app.db_path, app.today()
        deadline = time.monotonic() + TIMEOUT
        if b["mode"] == "hermes":
            return _hermes_turn(conn, b, history, path, today, deadline, owner)
        return _api_turn(conn, b, history, path, today, deadline, owner)
    finally:
        slots.release()


NO_HERMES = ("Ask can't find Hermes on this computer (no hermes command where Hermes installs it). Install Hermes, or "
             "use an API key in Settings → AI provider.")
PENDING_NOTE = "Shown to the owner as an approval card. Nothing changes until they approve; don't say it's done."


def _bridge_cost(reply: bridge.Reply, req: dict[str, Any], failed: bool = False) -> tuple[float, int, int, bool]:
    """(dollars, input tokens, output tokens, estimated?) for one bridge request: Hermes's own price when it reports
    one, else an estimate from its token counts (from the characters when it reports none) at EST_IN / EST_OUT."""
    missing = reply.tokens_in is None or reply.tokens_out is None
    tin = reply.tokens_in if reply.tokens_in is not None else len(json.dumps(req)) // 3
    tout = reply.tokens_out if reply.tokens_out is not None else (MAX_OUTPUT_TOKENS if failed else len(reply.text) // 3)
    if reply.cost_usd is not None:
        return reply.cost_usd, tin, tout, missing
    return tin * EST_IN / 1e6 + tout * EST_OUT / 1e6, tin, tout, True


def _propose(path: Path, today: date, name: str, args: dict[str, Any], turn_id: str,
             deadline: float, owner: str | None = None) -> tuple[str, dict[str, Any] | None]:
    """A Hermes turn's proposed change: (what the model gets back, the card or None). Its plan and card text are made
    in a child (_isolated), as in the api mode; the card is recorded here, held by the turn, origin ask_hermes. A
    finance_sync is a card too, even one an api turn would run without asking: a Hermes turn never syncs itself (a sync
    can't be cut short). A change that can't be made: its error, and no card."""
    try:
        if tooldefs.tool(name).write:
            checked, summary = _isolated("describe", name, args, path, today, deadline)
        else:
            checked, summary = _isolated("approval", name, args, path, today, deadline)
            summary = summary or bridge.sync_summary(checked)
        action = _record(path, name, checked, summary, turn_id, deadline, origin=ORIGIN_HERMES, owner=owner)
    except ToolOutputTooLarge:
        return tooldefs.render({"error": "the proposed change was too large to validate safely"}), None
    except tooldefs.ToolError as e:
        return tooldefs.render({"error": redact.scrub(str(e))}), None
    return tooldefs.render({"status": "pending_approval", "applied": False, "summary": summary,
                            "note": PENDING_NOTE}), action


def _hermes_turn(conn, b, history, path, today, deadline, owner: str | None = None) -> dict[str, Any]:
    """A Hermes-mode turn: up to MAX_ROUNDS bridge requests, each answered with one decision the app checks, by the
    turn's deadline; the same reservation, cost, card and publication steps as _api_turn."""
    exe = bridge.executable()
    if exe is None:
        raise ChatError(NO_HERMES, 503)
    instructions = _system(today, "hermes")
    reads: list[tuple[str, Any]] = []
    actions: list[dict[str, Any]] = []
    steps: list[tuple[bridge.Decision, str]] = []
    turn_id = secrets.token_hex(8)
    settle = deadline + SETTLE
    busy = conn.execute("PRAGMA busy_timeout").fetchone()[0]   # (conn is the route's: given back as it was)
    conn.execute("PRAGMA busy_timeout = 0")                    # _reserve / _write wait for the lock, by the clock
    try:
        for _ in range(MAX_ROUNDS):
            try:
                req = bridge.request(instructions, history, steps, MAX_OUTPUT_TOKENS)
            except bridge.BridgeError as e:
                raise ChatError(str(e), e.status) from None
            _in_time(deadline)
            res = _reserve(conn, today, b["cap"], deadline)   # before the child starts: at the cap, none does
            try:
                reply = bridge.complete(exe, req, deadline)
            except bridge.BridgeError as e:
                outcome = "timeout" if e.code == "timeout" else "error"
                if e.usage is not None:   # Hermes reported some usage/price: preserve known zero and estimate gaps
                    _write(settle, conn, usage.ai_finish, res, outcome, None,
                           *_bridge_cost(e.usage, req, failed=True), via=VIA_HERMES)
                elif e.cost == "worst":   # the provider may have been asked, and nothing says what it cost
                    _write(settle, conn, usage.ai_finish, res, outcome, None, *_worst(req), via=VIA_HERMES)
                else:
                    _write(settle, conn, usage.ai_finish, res, "error", None, 0.0, via=VIA_HERMES)
                raise ChatError(LATE if e.code in ("timeout", "no_time") else str(e), e.status) from None
            except BaseException:
                # Once reserved and dispatched, an unexpected transport/parser failure cannot prove the provider was
                # never called. Settle conservatively instead of fabricating a free request.
                _write(settle, conn, usage.ai_finish, res, "error", None, *_worst(req), via=VIA_HERMES)
                raise
            usd, tin, tout, est = _bridge_cost(reply, req)
            _write(settle, conn, usage.ai_finish, res, "ok", 200, usd, tin, tout, est, via=VIA_HERMES)
            try:
                d = bridge.decision(reply.text)   # the app's own check, whatever Hermes checked (nothing logged: the
            except bridge.Invalid:                # answer can carry the owner's data)
                raise ChatError(bridge.INVALID) from None
            if d.kind == "answer":
                out = {"reply": d.text, "sources": list(dict.fromkeys(source_line(n, p) for n, p in reads)),
                       "stats": _stats(reads, d.text), "brain": b["label"]}
                if actions:
                    out["cards"] = _publish(path, turn_id, len(actions), deadline)   # the last step, in time or not at all
                else:
                    _in_time(deadline)
                    out["cards"] = []
                return out
            _in_time(deadline)                  # no step starts after the deadline
            if d.kind == "read":
                try:
                    payload = _isolated("read", d.tool, d.arguments, path, today, deadline)
                except ToolOutputTooLarge:
                    result = tooldefs.render({"error": "the result is too large to send; ask a narrower question"})
                except tooldefs.ToolError as e:
                    result = tooldefs.render({"error": redact.scrub(str(e))})
                else:
                    result = tooldefs.render(payload)
                    if bridge.bounded(result) == result:   # a source only when the model got all of it
                        reads.append((d.tool, payload))
            else:
                result, action = _propose(path, today, d.tool, d.arguments, turn_id, deadline, owner)
                if action is not None:
                    actions.append(action)
            steps.append((d, bridge.bounded(result)))
            _in_time(deadline)
        raise ChatError(f"That took more than {MAX_ROUNDS} look-ups, so Ask stopped. Try a narrower question.")
    except Exception:   # a ChatError or anything unexpected: no card is left waiting from a turn never answered
        if actions:
            _withdraw(path, turn_id, settle)
        raise
    finally:
        conn.execute(f"PRAGMA busy_timeout = {busy}")


def _api_turn(conn, b, history, path, today, deadline, owner: str | None = None) -> dict[str, Any]:
    if not b["model"]:
        raise ChatError("Pick a model in Settings \u2192 AI provider first.", 400)
    if not b["url"]:
        raise ChatError("Set the provider's address in Settings \u2192 AI provider first.", 400)
    key = config.key_values().get(secretstore.AI_KEY)
    if not key and b.get("preset") != "local":
        raise ChatError("Add your API key in Settings \u2192 AI provider first.", 400)
    redact.register(key)
    messages: list[dict[str, Any]] = [{"role": "system", "content": _system(today, "api")}, *history]
    tools = _schemas()
    reads: list[tuple[str, Any]] = []
    actions: list[dict[str, Any]] = []
    turn_id = secrets.token_hex(8)          # what holds this turn's cards until it answers
    # the output bound's name: OpenAI's newer models refuse max_tokens and take only max_completion_tokens
    bound = "max_completion_tokens" if b.get("preset") == "openai" else "max_tokens"
    settle = deadline + SETTLE              # a request's cost, withdrawing a failed turn's cards: tried until then
    busy = conn.execute("PRAGMA busy_timeout").fetchone()[0]   # (conn is the route's: given back as it was)
    conn.execute("PRAGMA busy_timeout = 0")                    # _reserve / _write wait for the lock, by the clock
    try:
        extra = _write(deadline, conn, aisession.headers, b["url"])   # an OpenCode host's session id, once a turn
        for _ in range(MAX_ROUNDS):
            body = {"model": b["model"], "messages": messages, "tools": tools, "tool_choice": "auto",
                    bound: MAX_OUTPUT_TOKENS}
            if b.get("preset") == "openrouter":
                body["usage"] = {"include": True}      # OpenRouter reports the request's cost in usage.cost
            res = _reserve(conn, today, b["cap"], deadline)
            try:
                resp = _post(f"{b['url']}/chat/completions", key, body, deadline - time.monotonic(), extra)
            except ChatError as e:
                if e.status == 504:
                    _write(settle, conn, usage.ai_finish, res, "timeout", None, *_worst(body))
                else:
                    _write(settle, conn, usage.ai_finish, res, "error", None, 0.0)
                raise e
            except BaseException:
                _write(settle, conn, usage.ai_finish, res, "error", None, 0.0)
                raise
            usd, tin, tout, est = _cost(resp, body)
            _write(settle, conn, usage.ai_finish, res, "ok", 200, usd, tin, tout, est)
            msg = resp["choices"][0]["message"]
            calls = msg.get("tool_calls") if isinstance(msg.get("tool_calls"), list) else []
            if not all(isinstance(c, dict) and isinstance(c.get("function"), dict) for c in calls):
                raise ChatError("The AI provider sent an answer the app couldn't read.")
            if not calls:
                reply = _text(msg)
                if not reply:
                    raise ChatError("The AI provider sent an empty answer. Try again.")
                out = {"reply": reply, "sources": list(dict.fromkeys(source_line(n, p) for n, p in reads)),
                       "stats": _stats(reads, reply), "brain": b["label"]}
                if actions:
                    out["cards"] = _publish(path, turn_id, len(actions), deadline)   # the last step, in time or not at all
                else:
                    _in_time(deadline)
                    out["cards"] = []
                return out
            messages.append({"role": "assistant", "content": msg.get("content") or None, "tool_calls": calls})
            for c in calls:
                _in_time(deadline)                  # no tool starts after the deadline
                fn = c["function"]
                name = fn.get("name") if isinstance(fn.get("name"), str) else ""
                result, payload, action = _run_tool(path, today, name, fn.get("arguments"), turn_id, deadline, owner)
                if payload is not None:
                    reads.append((name, payload))
                if action is not None:
                    actions.append(action)
                messages.append({"role": "tool", "tool_call_id": c.get("id") or "", "content": result})
            _in_time(deadline)
        raise ChatError(f"That took more than {MAX_ROUNDS} look-ups, so Ask stopped. Try a narrower question.")
    except Exception:   # a ChatError or anything unexpected (web.py answers that with a bare 500): no card is left
        if actions:         # waiting from a turn the owner never saw answered
            _withdraw(path, turn_id, settle)
        raise
    finally:
        conn.execute(f"PRAGMA busy_timeout = {busy}")


# --- the cards' routes -----------------------------------------------------------------------------------------------

def _ids(q: str) -> list[int]:
    out = []
    for x in (q or "").split(","):
        if x.strip().isdigit():
            out.append(int(x))
    return out[:50]


def _own(conn, action_id: Any, owner: str | None = None) -> dict[str, Any] | None:
    """Ask's pending action #action_id (any status), or None: the panel only reads and decides the cards its own turns
    recorded (origin ask or ask_hermes, set by _record: never what a client calls itself) and that turn has answered
    (not held); another agent's actions are decided in Settings."""
    if isinstance(action_id, bool) or not isinstance(action_id, int):
        return None
    found = mcp_state.pending(conn, action_id)
    return (found[0] if found and found[0]["origin"] in ASK_ORIGINS and not found[0]["held"]
            and mcp_state.ask_owner(conn, action_id) == owner else None)


def cards(conn, ids: list[int], owner: str | None = None) -> list[dict[str, Any]]:
    return [card(a) for a in (_own(conn, i, owner) for i in ids) if a]


def decide(app, conn, data: Any, owner: str | None = None) -> tuple[int, dict[str, Any]]:
    """Approve / Deny on a card: mcp_state.decide, exactly Settings' path (admin.pending_decide): claimed once, then
    applied with tooldefs.apply. A card decided already answers 409 with its final state; an id that isn't one of Ask's
    cards, 404 (nothing is decided)."""
    d = data if isinstance(data, dict) else {}
    if isinstance(d.get("id"), int) and not isinstance(d["id"], bool) and _own(conn, d["id"], owner) is None:
        return 404, {"error": f"no Ask card #{d['id']}"}
    try:
        action = mcp_state.decide(conn, d.get("id"), d.get("approve"),
                                  lambda tool, args: tooldefs.apply(tool, args, app.db_path, app.today()),
                                  (tooldefs.ToolError,))
    except mcp_state.DecideError as e:
        found = _own(conn, d.get("id"), owner)
        return e.status, {"error": str(e), **({"card": card(found)} if found else {})}
    return 200, {"card": card(action)}


PATHS = ("/api/chat", "/api/chat/cards", "/api/chat/decide")


def handles(path: str) -> bool:
    return path in PATHS


def _request_owner(req) -> str | None:
    """Opaque binding for an authenticated browser session or trusted originating proxy identity."""
    if req.session:
        return f"session:{req.session['token_hash']}"
    if req.acc.mode == "proxy":
        identity = access.canonical_identity(req.headers.get(req.acc.header_key) or "")
        if identity:
            return "proxy:" + hashlib.sha256(identity.encode()).hexdigest()
    return None


def route(app, req) -> tuple[int, str, bytes]:
    """The /api/chat routes (web.App calls this after its access gate and, with the built-in login, the CSRF check)."""
    method, path = req.method, req.path
    owner = _request_owner(req)
    with app._conn() as conn:
        if brain(conn)["mode"] == "off":   # no Ask: no button on the page, and every route refuses
            return app._json(404, {"error": "Ask is off (Settings \u2192 AI provider)."})
        if path == "/api/chat/cards" or (path == "/api/chat" and method == "POST"):   # (a decide does it itself)
            mcp_state.reconcile(conn)   # settle what a stopped process left: a card asked about again reaches its end
        if method == "GET" and path == "/api/chat":
            return app._json(200, state(app, conn))
        if method == "GET" and path == "/api/chat/cards":
            return app._json(200, {"cards": cards(conn, _ids(req.params().get("ids", "")), owner)})
        if method != "POST" or path == "/api/chat/cards":
            return app._json(405, {"error": "method not allowed"})
        data = req.data()
        if path == "/api/chat/decide":
            status, payload = decide(app, conn, data, owner)
            return app._json(status, payload)
        try:
            return app._json(200, turn(app, conn, data, owner))
        except ChatError as e:
            return app._json(e.status, {"error": redact.scrub(str(e))})
