"""hermes-finance for Hermes: the finance_* tools and the hermes-finance:usage skill.

The reads answer from the local hermes-finance database, opened read-only, with exactly the numbers of
`finance <command> --json`. The four writes (finance_set_category, finance_add_rule, finance_set_cap and TASK-21's
finance_track_bill) go through Hermes's human approval gate: a pre_tool_call hook asks for approval with the change in
plain words, and the tool only runs once it is approved (a call that can't be done is blocked with the reason, without
asking). TASK-22's finance_sync runs without asking, except a refresh (ask the banks for fresh data) on a paid Plaid
plan, which Plaid bills: the hook asks for that one too.

The tools themselves (names, schemas, code) are hermes-finance's own, hermes_finance.tooldefs, shared with its MCP
server; this package only wires them into Hermes. hermes_finance is found, in this order:
  1. the HERMES_FINANCE_APP_DIR plugin setting, else that environment variable: a checkout (its src/ is loaded);
  2. an installed hermes_finance package;
  3. the checkout this directory sits in (when Hermes loads it in place, e.g. through a symlink).
The database is the HERMES_FINANCE_DATA_DIR setting's finance.db, else the app's own choice (config.db_path()).

Where nobody can answer an approval the hook blocks instead of asking, because Hermes's approvals.cron_mode /
unattended_mode "approve" would turn the request into a yes nobody gave: in a cron session (a scheduled job's agent) the
writes and finance_sync (the jobs already ran their own sync); on an unattended platform (the API server, webhooks) and
in a single-query `hermes chat -q` run, the writes and a billed refresh, so a change asked for there only happens
through the app's own approval card. Reads answer as usual everywhere.

It also registers `hermes finance` (finance_cli.py): `hermes finance crons status|plan|apply` sets up the daily and
weekly report jobs, only when asked (crons/); `hermes finance completion` is the Ask panel's bridge to Hermes's own model
(completion.py): the app starts it for each request of a Hermes-mode turn, and it makes one structured completion and
nothing else (no database, no tool). Nothing is written by importing or registering the plugin.
"""

from __future__ import annotations

import importlib
import json
import logging
import os
import sys
from pathlib import Path
from typing import Any

log = logging.getLogger(__name__)

PLUGIN_DIR = Path(__file__).resolve().parent
TOOLSET = "hermes-finance"
APP_DIR = "HERMES_FINANCE_APP_DIR"
DATA_DIR = "HERMES_FINANCE_DATA_DIR"


def _setting(ctx: Any, key: str) -> str | None:
    try:
        value = ctx.get_config(key)
    except Exception:  # an older Hermes without plugin settings, or no config file
        value = None
    return value.strip() if isinstance(value, str) and value.strip() else None


def _load_app(ctx: Any):
    """hermes_finance.tooldefs, found as the module docstring says."""
    configured = _setting(ctx, APP_DIR) or (os.environ.get(APP_DIR) or "").strip() or None
    roots = [Path(configured).expanduser()] if configured else []
    if not configured:
        try:
            return importlib.import_module("hermes_finance.tooldefs")
        except ImportError:
            roots.append(PLUGIN_DIR.parent)
    for root in roots:
        src = root / "src"
        if (src / "hermes_finance" / "tooldefs.py").is_file():
            if str(src) not in sys.path:
                sys.path.insert(0, str(src))
            return importlib.import_module("hermes_finance.tooldefs")
    where = f"{APP_DIR}={configured} has no src/hermes_finance" if configured else "hermes_finance isn't installed"
    raise ImportError(f"hermes-finance plugin: can't load the app ({where}). First prepare a trusted hermes-finance "
                      "checkout with Python 3.12 and uv sync. Set "
                      f"plugins.entries.hermes-finance.settings.{APP_DIR} (or the {APP_DIR} environment variable) "
                      "to that checkout, then run uv run finance doctor there. No bank or API key is required "
                      "to load the plugin; integrations are optional. See README.md#install-as-a-hermes-plugin.")


CRON_BLOCK = ("{tool} isn't available in a scheduled job: a scheduled run has nobody to approve a change, and the job "
              "has already synced. Nothing was changed; report what needs the owner's decision instead.")
UNATTENDED_BLOCK = ("{tool} isn't available here: there is nobody here to approve a change ({platform}). Nothing was "
                    "changed; the owner can make it in the finance app (a proposed change there waits for their OK).")
# Hermes's own list (tools/approval.py _UNATTENDED_APPROVAL_PLATFORMS), used when that module can't be read
UNATTENDED_PLATFORMS = frozenset({"webhook", "msgraph_webhook", "api_server"})


def _session_env(name: str) -> str:
    """A session variable, read the way Hermes's own approval code reads it (the session context, then the
    environment). Never raises."""
    try:
        from gateway.session_context import get_session_env
        return str(get_session_env(name, "") or "")
    except Exception:
        return str(os.environ.get(name, "") or "")


def _truthy(value: str) -> bool:
    return value.strip().lower() in ("1", "true", "yes", "on")


def _in_cron_session() -> bool:
    return _truthy(_session_env("HERMES_CRON_SESSION"))


def _no_human_here() -> str | None:
    """Where an approval request has nobody to answer it, other than cron: an unattended platform (Hermes's own list,
    approvals.unattended_mode) or a single-query `hermes chat -q` run (approvals.single_query_mode). Its name, or None.
    Never raises."""
    try:
        from tools.approval import _UNATTENDED_APPROVAL_PLATFORMS as platforms
    except Exception:
        platforms = UNATTENDED_PLATFORMS
    platform = _session_env("HERMES_SESSION_PLATFORM").strip()
    if platform in platforms or platform in UNATTENDED_PLATFORMS:
        return platform
    if _truthy(_session_env("HERMES_SINGLE_QUERY_SESSION")):
        return "a single-query hermes chat -q run"
    return None


class _Finance:
    """The tools' handlers and the approval hook, bound to one Hermes plugin context."""

    def __init__(self, ctx: Any, tooldefs: Any):
        self.ctx = ctx
        self.tooldefs = tooldefs

    def db_path(self) -> Path:
        configured = _setting(self.ctx, DATA_DIR)
        if configured:
            return Path(configured).expanduser() / "finance.db"
        from hermes_finance import config
        return config.db_path()

    def handler(self, name: str):
        t = self.tooldefs

        def run(args: dict | None = None, **kwargs: Any) -> str:
            try:
                if name in t.WRITES:  # reached only after the pre_tool_call hook's approval
                    payload = t.apply(name, args, self.db_path(), t.today())
                elif name in t.ACTIONS:  # finance_sync: after the hook's approval when it asked for one
                    payload = t.run_action(name, args, self.db_path(), t.today())
                else:
                    payload = t.run_read(name, args, self.db_path(), t.today())
                return t.render(payload)
            except t.ToolError as e:
                return t.render({"error": str(e)})
            except Exception as e:  # a tool never raises into Hermes
                log.exception("%s failed", name)
                return t.render({"error": f"internal error ({type(e).__name__}); nothing was changed"})

        run.__name__ = name
        return run

    def approval(self, tool_name: str = "", args: dict | None = None, task_id: str | None = None,
                 **kwargs: Any) -> dict[str, str] | None:
        """pre_tool_call: exactly the four writes (and finance_sync's billed refresh) need the user's approval, shown the
        change in plain words. A call
        that can't be done (unknown transaction, a rule matching nothing, ...) is blocked with the reason instead.
        Never raises: Hermes skips a hook that crashes, and the write would then run unasked."""
        t = self.tooldefs
        if tool_name not in t.WRITES and tool_name not in getattr(t, "ACTIONS", ()):
            return None
        if _in_cron_session():
            return {"action": "block", "message": CRON_BLOCK.format(tool=tool_name)}
        nobody = _no_human_here()
        blocked = {"action": "block", "message": UNATTENDED_BLOCK.format(tool=tool_name, platform=nobody)}
        if nobody and tool_name in t.WRITES:
            return blocked
        try:
            if tool_name in t.WRITES:
                _, summary = t.describe(tool_name, args, self.db_path())
            else:   # finance_sync: only a billed refresh asks (and, where nobody can answer, is blocked)
                _, summary = t.approval_needed(tool_name, args, self.db_path())
                if summary is None:
                    return None
                if nobody:
                    return blocked
        except t.ToolError as e:
            return {"action": "block", "message": f"{tool_name}: {e} (nothing was changed)"}
        except Exception:  # still ask (the arguments as given), unless nobody is there to answer
            log.warning("%s: couldn't describe the change", tool_name, exc_info=True)
            if nobody:
                return blocked
            summary = f"{tool_name} with {json.dumps(args, default=str)[:500]}"
        return {"action": "approve", "message": summary}


def register(ctx: Any) -> None:
    tooldefs = _load_app(ctx)
    finance = _Finance(ctx, tooldefs)
    for tool in tooldefs.TOOLS.values():
        ctx.register_tool(name=tool.name, toolset=TOOLSET, handler=finance.handler(tool.name),
                          schema={"name": tool.name, "description": tool.description, "parameters": tool.schema()},
                          description=tool.description, emoji="💵")
    ctx.register_hook("pre_tool_call", finance.approval)
    ctx.register_skill("usage", PLUGIN_DIR / "skills" / "usage" / "SKILL.md",
                       description="How to answer money questions with the finance_* tools")
    register_cli = getattr(ctx, "register_cli_command", None)
    if callable(register_cli):   # `hermes finance ...`: its commands' modules load when Hermes builds its parser
        finance_cli = importlib.import_module(".finance_cli", __name__)
        command = finance_cli.FinanceCommand(ctx, __name__)
        register_cli(name="finance", help=finance_cli.HELP, setup_fn=command.setup, handler_fn=command.run,
                     description=finance_cli.HELP)
