"""Write docs/rules-api.md: real responses of the rules routes on tests/rules_scene.py's synthetic DB (the Undo receipt,
random each time, is shown as a placeholder)."""
import atexit
import json
import os
import shutil
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path[:0] = [str(ROOT / "tests"), str(ROOT / "src")]
tmp = Path(tempfile.mkdtemp())
atexit.register(shutil.rmtree, tmp, True)
os.environ["FINANCE_DATA_DIR"] = str(tmp / "data")
os.environ["HERMES_ENV_FILE"] = str(tmp / "none.env")
os.environ["FINANCE_SECRETS_FILE"] = str(tmp / "none-secrets.env")
os.environ["HERMES_PLAID"] = "off"

from hermes_finance import config, db  # noqa: E402
from hermes_finance.web import App  # noqa: E402
import rules_scene  # noqa: E402

conn = db.connect()
rules_scene.build(conn)
app = App(config.db_path(), lambda: None, "127.0.0.1", 8765)
H = {"Host": "127.0.0.1:8765"}
J = {**H, "Content-Type": "application/json", "Origin": "http://127.0.0.1:8765"}


def call(method, path, body=None):
    s, _, out, _ = app.respond(method, path, J if body is not None else H, json.dumps(body).encode() if body is not None else b"")
    return s, json.loads(out)


out = []
s, got = call("GET", "/api/rules")
out.append(("GET /api/rules", None, s, got))
ai = next(r for r in got["rules"] if r["source"] == "ai" and r["type"] == "exact")
s2, upd = call("POST", "/api/rules/update", {"id": ai["id"], "category": "Other Health"})
out.append(("POST /api/rules/update", {"id": ai["id"], "category": "Other Health"}, s2, upd))
s3, dele = call("POST", "/api/rules/delete", {"id": ai["id"]})
RECEIPT = "<receipt: 32 random characters, returned once>"
shown_undo = {"undo": {"receipt": RECEIPT}}
out.append(("POST /api/rules/delete", {"id": ai["id"]}, s3, {**dele, **shown_undo}))
s5, back = call("POST", "/api/rules/restore", {"undo": dele["undo"]})
out.append(("POST /api/rules/restore", shown_undo, s5, back))
s6, again = call("POST", "/api/rules/restore", {"undo": dele["undo"]})
s4, err = call("POST", "/api/rules/update", {"id": got["rules"][-1]["id"], "category": "Other Health", "pattern": "x"})
lines = ["# Rules manager API", "",
         "Real responses from `scripts/rules_api_doc.py` on the synthetic DB in `tests/rules_scene.py` (5 rules:",
         "3 AI, 2 yours; 2 suggestions). All POSTs need `x-csrf-token` with the built-in login; errors are",
         "`{\"error\": str}` with 400 (bad input), 404 (unknown rule / suggestion / Undo receipt) or 409 (that Undo",
         "was used already, or a rule with its pattern exists again).", "",
         "Routes: `GET /api/rules`; `POST /api/rules/update {id, category, pattern?}` (pattern only for user rules, else 400);",
         "`POST /api/rules/keep {id}`; `POST /api/rules/delete {id}` -> `{deleted, rule, filed, undo}`;",
         "`POST /api/rules/restore {undo}` -> `{rule, relinked}`; `POST /api/rules/suggestions/accept {key}` -> `{rule, refiled}`;",
         "`POST /api/rules/suggestions/dismiss {key}`. Each write is one transaction: it all happens or none of it does.", "",
         "Undo: `undo` is `{\"receipt\": str}`, the handle of the server's own record of the delete (table rule_deletions; only",
         "the receipt's SHA-256 is stored). Restore reads nothing else: any other field, or any other shape, is a 400; an unknown",
         "receipt is a 404; a receipt restores once, and a second try (or a rule with the same pattern saved since) is a 409. The",
         "rule comes back as it was, and only the rows it filed that no one changed since are linked to it again (`relinked`).",
         "The receipt survives a restart. The rows a deleted rule filed keep their category; a deleted user rule's rows stay",
         "the user's (the AI never changes them), a deleted AI rule's rows stay open to the AI.", "",
         "Accepting a suggestion saves that merchant's rule and re-files only the rows it matches (`refiled` counts those).",
         "Editing an AI rule's category makes it yours (source becomes \"user\"). `matches` counts live transactions the rule",
         "matches now (user-filed ones included).", ""]
for i, (title, body, status, resp) in enumerate(out, 1):
    lines += [f"## {i}. {title}" + (f" `{json.dumps(body)}`" if body else ""), "", f"HTTP {status}", "", "```json",
              json.dumps(resp, indent=2), "```", ""]
lines += ["## Error example: pattern edit on an AI rule", "", f"HTTP {s4}", "", "```json", json.dumps(err, indent=2), "```", ""]
lines += ["## Error example: the same Undo again", "", f"HTTP {s6}", "", "```json", json.dumps(again, indent=2), "```", ""]
(ROOT / "docs" / "rules-api.md").write_text("\n".join(lines))
print(s, s2, s3, s4, s5, s6, got["counts"], len(got["suggestions"]))
