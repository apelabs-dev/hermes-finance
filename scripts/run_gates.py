"""Run both final-review gates on the synthetic DBs (tests/gate_dbs.py: full, nocaps, empty, history-empty, onepoint, features,
plus the sign-in DBs signin and signin-setup), each served by
the dashboard's App on a throwaway loopback port (tests/cdp.Server; it never reaches Plaid), all at once (one
server and one headless Chrome each). Nothing real is read: the DBs are built in a temp dir, and HERMES_ENV_FILE points
at a temp file that switches Carfax, Redfin and the photos on (so the pages show them on) while valuation.transport
refuses every call, valuation.browser has no Chrome (HERMES_CHROME_DEVTOOLS=off as well) and photos.schedule starts
nothing: no gate can reach NHTSA, Carfax, Redfin, Wikimedia Commons or OpenStreetMap, the real Chrome, or read
~/.hermes/.env.

    uv run python scripts/run_gates.py [numbers|layout|both|controls] [full,nocaps,empty,history-empty,onepoint,features,signin,signin-setup]
                                       [--out shots/011]

`controls` is the controls gate (scripts/audit_controls.py, deadctl: every control does something when clicked), on
the DBs given (tests/test_gates.py runs it on audit_controls.KINDS).

Prints each gate's full output, DB by DB; exits 1 if any check FAILs.
"""
from __future__ import annotations

import argparse
import contextlib
import os
import subprocess
import sys
import tempfile
import threading
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tests"))
sys.path.insert(0, str(ROOT / "scripts"))
import audit_controls  # noqa: E402
import audit_layout  # noqa: E402
import audit_numbers  # noqa: E402
import gate_dbs  # noqa: E402
import hermes_finance  # noqa: E402
from hermes_finance import photos, valuation  # noqa: E402

SOURCES_ON = "CARFAX_ENABLED=1\nREDFIN_ENABLED=1\nPHOTOS_ENABLED=1\n"


def _no_provider(url, headers, timeout):
    raise AssertionError("the gates never call a valuation provider")


class _NoChrome:
    def available(self):
        return "Chrome not running (the gates have no browser)"

    def tab(self, url):
        raise AssertionError("the gates never open a Chrome tab")


@contextlib.contextmanager
def offline_valuation(tmp: Path):
    """The value sources shown as on, and no outbound valuation / photo call, for the length of the run."""
    env = tmp / "gates-hermes.env"
    env.write_text(SOURCES_ON)
    old_secrets = os.environ.get("FINANCE_SECRETS_FILE")
    os.environ["FINANCE_SECRETS_FILE"] = str(tmp / "gates-secrets.env")  # never the real one (TASK-18)
    old_env, old_transport, old_browser = os.environ.get("HERMES_ENV_FILE"), valuation.transport, valuation.browser
    old_schedule = photos.schedule
    old_cdp = os.environ.get("HERMES_CHROME_DEVTOOLS")
    old_alpaca = {k: os.environ.get(k) for k in ("HERMES_ALPACA", "HERMES_ALPACA_KEY_FILE")}
    os.environ["HERMES_ENV_FILE"] = str(env)
    os.environ["HERMES_CHROME_DEVTOOLS"] = "off"
    os.environ["HERMES_ALPACA"] = "off"                                       # TASK-19: no Alpaca call, no key file read
    os.environ["HERMES_ALPACA_KEY_FILE"] = str(tmp / "gates-no-alpaca.env")
    _offline()
    try:
        yield
    finally:
        valuation.transport, valuation.browser, photos.schedule = old_transport, old_browser, old_schedule
        if old_cdp is None:
            os.environ.pop("HERMES_CHROME_DEVTOOLS", None)
        else:
            os.environ["HERMES_CHROME_DEVTOOLS"] = old_cdp
        if old_env is None:
            os.environ.pop("HERMES_ENV_FILE", None)
        else:
            os.environ["HERMES_ENV_FILE"] = old_env
        if old_secrets is None:
            os.environ.pop("FINANCE_SECRETS_FILE", None)
        else:
            os.environ["FINANCE_SECRETS_FILE"] = old_secrets
        for k, v in old_alpaca.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v


def _offline() -> None:
    """In this process: valuation.transport refuses, valuation.browser has no Chrome, photos.schedule starts nothing."""
    valuation.transport = _no_provider
    valuation.browser = _NoChrome()
    photos.schedule = lambda db_path, account_id: None


def _no_plaid():
    raise AssertionError("the gates must never reach Plaid")


class ServerProcess:
    """TEST-SPEED: tests/cdp.Server's App (the same App, loopback, a free port, no Plaid), in a process of its own. In a
    thread of this process, every DB's server and every Chrome's driver shared one GIL: the Python process was the
    bottleneck at ~230 ms of CPU a state. Here each DB is served on its own core. The child (`run_gates.py --serve`)
    inherits offline_valuation()'s environment and sets up its patches itself; it lets the browser keep /static/ files
    for the run (the app itself answers no-store: a gate's hundreds of page loads fetched the same 27 files each time).
    It exits when this process goes away however that happens (its stdin closes)."""

    def __init__(self, db_path: Path, cache_static: bool = True):
        env = {**os.environ, "HERMES_PLAID": "off",
               "PYTHONPATH": os.pathsep.join([str(Path(hermes_finance.__file__).resolve().parents[1]),
                                              *filter(None, [os.environ.get("PYTHONPATH")])])}
        log = os.environ.get("HF_GATE_SERVER_LOG")   # scripts/check.py: the servers' own lines in its log, not on screen
        err = open(log, "a") if log else None
        self.proc = subprocess.Popen([sys.executable, str(Path(__file__).resolve()), "--serve", str(db_path),
                                      "cache" if cache_static else "no-cache"],
                                     stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=err, env=env, text=True)
        if err:
            err.close()
        line = self.proc.stdout.readline().strip()
        if not line.isdigit():
            self.close()
            raise RuntimeError(f"the gate server for {db_path} did not start ({line!r})")
        self.port = int(line)
        self.url = f"http://127.0.0.1:{self.port}"

    def close(self) -> None:
        if self.proc.poll() is None:
            self.proc.stdin.close()   # the child's cue to exit
            try:
                self.proc.wait(5)
            except subprocess.TimeoutExpired:
                self.proc.kill()
                self.proc.wait(5)
        self.proc.stdout.close()


def serve(db_path: str, cache: str) -> None:
    """The child of a ServerProcess: serve `db_path`, print the port, run until stdin closes."""
    from hermes_finance import web
    _offline()
    app = web.App(Path(db_path), _no_plaid, "127.0.0.1", 0)
    base = web.make_handler(app)

    class Handler(base):
        def send_response(self, code, message=None):
            self._status = code
            super().send_response(code, message)

        def send_header(self, keyword, value):
            if (cache == "cache" and keyword == "Cache-Control" and getattr(self, "_status", 0) == 200
                    and self.path.startswith("/static/")):
                value = "max-age=86400"
            super().send_header(keyword, value)

    srv = web._Server(("127.0.0.1", 0), Handler)
    app.port = srv.server_address[1]
    threading.Thread(target=lambda: (sys.stdin.read(), os._exit(0)), daemon=True).start()
    print(app.port, flush=True)
    os.dup2(2, 1)   # anything printed later goes where stderr goes, not into the pipe nobody reads any more
    srv.serve_forever()


# FIX-CDP-LEAK: a real cap now (before, 8 DBs each got `workers` Chromes and nothing bounded the total).
# TEST-SPEED: this many headless Chromes for the whole run, whatever the number of DBs: the layout gate is one queue of
# every DB's states (audit_layout.audit_many), so all of them stay busy to the end. Default 4, this Mac mini's budget
# (tests/cdp.py holds every Chrome to HF_CHROMES machine-wide as well); GATES_CHROMES lowers it.
CHROMES = max(1, min(4, int(os.environ.get("GATES_CHROMES") or 4)))   # (at most 4 whatever it says)


def run(kinds, which: str, out: Path, select=None, self_test=True) -> list[dict]:
    """Build and serve every DB in `kinds`, run the gate(s) on all of them; one result per DB, in `kinds` order:
    {kind, lines, numbers, numbers_fail, layout, layout_fail, controls, controls_fail}. The sign-in DBs (TASK-18: gate_dbs.AUTH_KINDS) only get
    the layout gate: everything else there answers "sign in first". select / self_test: audit_layout.audit_many's (only
    scripts/check.py fast narrows them; the gates themselves run everything)."""
    with tempfile.TemporaryDirectory(prefix="gates-") as tmp, offline_valuation(Path(tmp)):
        res = {k: {"kind": k, "lines": [], "numbers_fail": [], "layout_fail": 0, "numbers": None, "layout": None,
                   "controls": None, "controls_fail": 0} for k in kinds}
        servers: dict[str, ServerProcess] = {}
        try:
            for k in kinds:
                (Path(tmp) / k).mkdir(parents=True, exist_ok=True)
                servers[k] = ServerProcess(gate_dbs.build(k, Path(tmp) / k / "finance.db"))
            num = [k for k in kinds if k not in gate_dbs.AUTH_KINDS] if which in ("numbers", "both") else []
            if num:   # one Chrome per DB, CHROMES at once
                def numbers(k: str) -> None:
                    log = res[k]["lines"].append
                    log(f"######## Gate 1: numbers audit, {k} DB ########")
                    a = audit_numbers.audit(servers[k].url, Path(tmp) / k / "finance.db", log=log)
                    res[k]["numbers"], res[k]["numbers_fail"] = a.results, a.fails()
                    log(f"\n[{k}] numbers: {len(a.results)} checks, {len(a.results) - len(a.fails())} PASS, {len(a.fails())} FAIL")
                with ThreadPoolExecutor(max(1, min(len(num), CHROMES))) as pool:
                    list(pool.map(numbers, num))
            if which in ("layout", "both"):
                for k in kinds:
                    res[k]["lines"].append(f"\n######## Gate 2: layout audit, {k} DB ########")
                reports = audit_layout.audit_many({k: servers[k].url for k in kinds}, out,
                                                  logs={k: res[k]["lines"].append for k in kinds}, workers=CHROMES,
                                                  select=select, self_test=self_test)
                for k, rep in reports.items():
                    res[k]["layout"], res[k]["layout_fail"] = rep, rep["fail"]
            if which == "controls":   # (the sign-in DBs have no control past the sign-in form: not given here)
                for k in kinds:
                    res[k]["lines"].append(f"\n######## Gate 3: controls (deadctl), {k} DB ########")
                reports = audit_controls.audit_many({k: servers[k].url for k in kinds}, out,
                                                    logs={k: res[k]["lines"].append for k in kinds}, workers=CHROMES)
                for k, rep in reports.items():
                    res[k]["controls"], res[k]["controls_fail"] = rep, rep["fail"]
        finally:
            for srv in servers.values():
                srv.close()
        return [res[k] for k in kinds]


def main(argv: list[str]) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("which", nargs="?", default="both", choices=("numbers", "layout", "both", "controls"))
    ap.add_argument("kinds", nargs="?", default=",".join((*gate_dbs.KINDS, *gate_dbs.AUTH_KINDS)))
    ap.add_argument("--out", type=Path, default=ROOT / "shots" / "011")
    args = ap.parse_args(argv)
    results = run(args.kinds.split(","), args.which, args.out)
    for r in results:
        print("\n".join(r["lines"]), flush=True)
    print("\n######## summary ########")
    for r in results:
        num, lay, ctl = r["numbers"], r["layout"], r["controls"]
        print(f"{r['kind']}: numbers {'-' if num is None else '%d checks, %d FAIL' % (len(num), len(r['numbers_fail']))}; "
              f"layout {'-' if lay is None else '%d checks, %d FAIL' % (lay['pass'] + lay['fail'], r['layout_fail'])}; "
              f"controls {'-' if ctl is None else '%d clicked, %d page(s) FAIL' % (ctl['probed'], r['controls_fail'])}")
    return 1 if any(r["numbers_fail"] or r["layout_fail"] or r["controls_fail"] for r in results) else 0


if __name__ == "__main__":
    if sys.argv[1:2] == ["--serve"]:
        serve(*sys.argv[2:4])
    sys.exit(main(sys.argv[1:]))
