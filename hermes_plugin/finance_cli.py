"""`hermes finance ...`: the plugin's command group, registered with ctx.register_cli_command.

Each entry of COMMANDS is one subcommand: its name, its help, and the plugin module (relative to this package) whose
`setup(parser, ctx)` adds the subcommand's arguments and sets `finance_handler` on each leaf parser. Adding a command
(e.g. a completion bridge) is a new module plus one line here.

Hermes builds this parser on every `hermes` invocation whose first word isn't one of its own commands (a chat prompt
included), so setup only builds argparse objects: it imports each command's module, which must keep its own heavy
imports inside the handlers, and never raises."""

from __future__ import annotations

import argparse
import importlib
import logging
from typing import Any

log = logging.getLogger(__name__)

COMMANDS: tuple[tuple[str, str, str], ...] = (
    ("crons", "the daily and weekly finance report jobs: status, plan, apply", ".crons.command"),
    ("completion", "the finance app's Ask bridge: one structured completion (started by the app)", ".completion"),
)
HELP = ("hermes-finance: the plugin's own commands (crons: the daily and weekly report jobs; completion: the Ask "
        "panel's bridge to Hermes's model)")


class FinanceCommand:
    def __init__(self, ctx: Any, package: str):
        self.ctx = ctx
        self.package = package
        self.parser: argparse.ArgumentParser | None = None

    def setup(self, parser: argparse.ArgumentParser) -> None:
        self.parser = parser
        sub = parser.add_subparsers(dest="finance_command", metavar="{" + ",".join(c[0] for c in COMMANDS) + "}")
        for name, help_, module in COMMANDS:
            try:
                mod = importlib.import_module(module, self.package)
                p = sub.add_parser(name, help=help_, description=help_)
                mod.setup(p, self.ctx)
            except Exception:   # one broken command must not take `hermes` (or the plugin's other commands) down
                log.exception("hermes finance %s: can't set up the command", name)

    def run(self, args: argparse.Namespace) -> int:
        handler = getattr(args, "finance_handler", None)
        if handler is None:
            if self.parser is not None:
                self.parser.print_help()
            return 0
        return handler(args)
