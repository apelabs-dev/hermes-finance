"""`hermes finance completion`: the Ask panel's completion bridge. One structured completion through Hermes's own model
and auth (ctx.llm.complete_structured), for the finance app, which starts this command itself (bridge.py) once per
request of a Hermes-mode Ask turn.

A transport, nothing more: it reads one request (JSON, stdin, at most MAX_REQUEST bytes), makes one call and writes one
envelope (JSON, one line, stdout). It never touches the finance database, runs no finance tool and applies nothing: the
app validates what the model said (never trusting Hermes's `parsed`: without the optional jsonschema package Hermes
skips the schema check), runs its reads itself and turns a proposed change into an approval card.

The request has exactly these fields, and nothing else is accepted (no provider, model, profile, agent or task: the
call always uses the profile's own default model, with the plugin's default trust):

  protocol      PROTOCOL
  instructions  what the model is told (at most MAX_INSTRUCTIONS characters)
  input         the conversation and the app's own results (at most MAX_INPUT characters)
  schema        the JSON schema of the one decision the model answers with (an object)
  schema_name   [a-z_]{1,64}
  max_tokens    1-8192 (Hermes may drop it for providers that take none: auxiliary_client._build_call_kwargs)
  timeout       seconds, more than 0 and at most MAX_TIMEOUT

The envelope: {"protocol", "ok": true, "text", "provider", "model", "usage"} or {"protocol", "ok": false, "error"} with
error one of ERRORS (never the exception's message: it can carry the prompt or the model's text). Only the envelope goes
to stdout: while Hermes works, anything written to stdout goes to stderr instead (which the app never reads). Nothing is
logged here; Hermes's own plugin_llm log line names the plugin, provider, model and token count, never the text.

This process ends by itself: an alarm (SIGALRM, not caught) at the request's timeout plus ALARM_GRACE seconds, and at
FALLBACK_ALARM while the request is read, or at the app's own alarm if that one comes first. The app also kills its
process group at the turn's deadline. Hermes loads every enabled plugin to build its command line (plugin discovery):
this is not a sandbox."""

from __future__ import annotations

import argparse
import contextlib
import json
import math
import os
import re
import signal
import sys
from typing import Any

PROTOCOL = "hermes-finance-completion/1"
MAX_REQUEST = 512 * 1024          # bytes of the request
MAX_INSTRUCTIONS = 32 * 1024      # characters
MAX_INPUT = 256 * 1024            # characters
MAX_SCHEMA = 128 * 1024           # bytes of the schema, as JSON
MAX_TEXT = 128 * 1024             # characters of the model's answer passed back (more: output_too_large)
MAX_TIMEOUT = 60.0
MAX_TOKENS = 8192
FALLBACK_ALARM = 90               # seconds while the request is read
ALARM_GRACE = 3                   # seconds past the request's timeout
PURPOSE = "hermes-finance Ask"
FIELDS = ("protocol", "instructions", "input", "schema", "schema_name", "max_tokens", "timeout")
SCHEMA_NAME = re.compile(r"[a-z_]{1,64}")
# what went wrong (the app decides what each costs: bridge.ERRORS)
ERRORS = ("bad_request", "request_too_large", "no_provider", "trust", "auth_refused", "rate_limited", "schema_mismatch",
          "output_too_large", "timeout", "unreachable", "failed")
HELP = "one structured completion for the finance app's Ask panel (JSON on stdin, one JSON envelope on stdout)"


class _Refused(Exception):
    def __init__(self, code: str):
        super().__init__(code)
        self.code = code


def setup(parser: argparse.ArgumentParser, ctx: Any) -> None:
    """No options: the command line is fixed, the request comes on stdin."""
    parser.description = HELP + ". Started by the finance app; not meant to be run by hand."
    parser.set_defaults(finance_handler=lambda args: run(ctx))


def _request(raw: bytes) -> dict[str, Any]:
    if len(raw) > MAX_REQUEST:
        raise _Refused("request_too_large")
    try:
        req = json.loads(raw)
    except ValueError:
        raise _Refused("bad_request") from None
    if not isinstance(req, dict) or sorted(req) != sorted(FIELDS) or req.get("protocol") != PROTOCOL:
        raise _Refused("bad_request")   # an unknown field (a provider, model, profile ...) included
    ok = (isinstance(req["instructions"], str) and 0 < len(req["instructions"].strip()) and
          len(req["instructions"]) <= MAX_INSTRUCTIONS and isinstance(req["input"], str) and
          0 < len(req["input"].strip()) and len(req["input"]) <= MAX_INPUT and isinstance(req["schema"], dict) and
          len(json.dumps(req["schema"])) <= MAX_SCHEMA and isinstance(req["schema_name"], str) and
          SCHEMA_NAME.fullmatch(req["schema_name"]) is not None and
          isinstance(req["max_tokens"], int) and not isinstance(req["max_tokens"], bool) and
          0 < req["max_tokens"] <= MAX_TOKENS and isinstance(req["timeout"], (int, float)) and
          not isinstance(req["timeout"], bool) and 0 < req["timeout"] <= MAX_TIMEOUT)
    if not ok:
        raise _Refused("bad_request")
    return req


def _error_code(e: BaseException) -> str:
    """What went wrong, by the exception's type (and, for the two plain ones Hermes raises, its fixed wording)."""
    kind, text = type(e).__name__, str(e)
    if kind == "PluginLlmTrustError":
        return "trust"
    if isinstance(e, ValueError):
        return "schema_mismatch" if "did not match schema" in text else "bad_request"
    if isinstance(e, RuntimeError) and ("No LLM provider configured" in text or "no API key was found" in text):
        return "no_provider"
    if kind in ("AuthenticationError", "PermissionDeniedError"):
        return "auth_refused"
    if kind == "RateLimitError":
        return "rate_limited"
    if kind in ("APITimeoutError", "TimeoutError", "ReadTimeout", "ConnectTimeout") or isinstance(e, TimeoutError):
        return "timeout"
    if kind in ("APIConnectionError", "ConnectError", "ConnectionError") or isinstance(e, ConnectionError):
        return "unreachable"
    return "failed"


def _number(value: Any) -> int | None:
    """A valid reported count, preserving reported zero separately from unavailable/invalid."""
    return value if isinstance(value, int) and not isinstance(value, bool) and value >= 0 else None


def _usage(u: Any) -> dict[str, Any]:
    cost = getattr(u, "cost_usd", None)
    return {"input_tokens": _number(getattr(u, "input_tokens", None)),
            "output_tokens": _number(getattr(u, "output_tokens", None)),
            "total_tokens": _number(getattr(u, "total_tokens", None)),
            "cost_usd": float(cost) if isinstance(cost, (int, float)) and not isinstance(cost, bool) and
            (not isinstance(cost, float) or math.isfinite(cost)) and 0 <= cost < 1000 else None}


def _call(ctx: Any, req: dict[str, Any]) -> dict[str, Any]:
    """The one call, with stdout sent to stderr meanwhile (sys.stdout, whatever it is bound to, and fd 1). The
    envelope, ok or not."""
    sys.stdout.flush()
    saved = os.dup(1)
    os.dup2(2, 1)
    try:
        with contextlib.redirect_stdout(sys.stderr):
            result = ctx.llm.complete_structured(instructions=req["instructions"],
                                                 input=[{"type": "text", "text": req["input"]}],
                                                 json_schema=req["schema"], schema_name=req["schema_name"],
                                                 max_tokens=req["max_tokens"], timeout=float(req["timeout"]),
                                                 purpose=PURPOSE)
    except Exception as e:
        return {"ok": False, "error": _error_code(e)}
    finally:
        sys.stdout.flush()
        os.dup2(saved, 1)
        os.close(saved)
    text = result.text if isinstance(result.text, str) else ""
    usage = _usage(getattr(result, "usage", None))
    if len(text) > MAX_TEXT:
        return {"ok": False, "error": "output_too_large", "usage": usage}
    return {"ok": True, "text": text, "provider": str(result.provider or "")[:100], "model": str(result.model or "")[:200],
            "usage": usage}


def _emit(envelope: dict[str, Any]) -> int:
    sys.stdout.write(json.dumps({"protocol": PROTOCOL, **envelope}, ensure_ascii=True, separators=(",", ":")) + "\n")
    sys.stdout.flush()
    return 0


def _arm(seconds: int) -> None:
    """Select the smaller whole-second alarm. The app's monotonic deadline remains the precise authoritative bound."""
    earlier = signal.alarm(0)
    signal.alarm(min(seconds, earlier) if earlier else seconds)


def run(ctx: Any) -> int:
    _arm(FALLBACK_ALARM)   # a request that never comes
    try:
        req = _request(sys.stdin.buffer.read(MAX_REQUEST + 1))
    except _Refused as e:
        return _emit({"ok": False, "error": e.code})
    _arm(math.ceil(req["timeout"]) + ALARM_GRACE)
    return _emit(_call(ctx, req))
