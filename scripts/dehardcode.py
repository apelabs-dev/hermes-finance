"""The de-hardcode check: no personal word (a name, a login, an address, a bank) in the shipped code, and the release
scan: nothing private (a word, a home path, a credential, an email, a real id) in any file a release ships.

    python3 scripts/dehardcode.py                 # scan every file under SCANNED_ROOTS (the working tree)
    python3 scripts/dehardcode.py PATH...         # scan these files (the working tree)
    python3 scripts/dehardcode.py --staged        # scan the staged blobs (`git show :path`) under SCANNED_ROOTS
    python3 scripts/dehardcode.py --release DIR   # the release scan of EVERY file under DIR (.git/ aside)
    python3 scripts/dehardcode.py --release-tracked   # the release scan of this checkout's tracked files that ship
    python3 scripts/dehardcode.py --release-staged    # the release scan of the staged blobs that ship (the hook)

Prints `path:line: token` (the release scan: `path:line: rule`, never the text it found: it runs in CI logs) per hit
and exits 1 when there is one, 0 when there is none (2 on bad usage). The one place the scanning lives:
tests/test_dehardcode_task18.py and tests/test_release_scan.py (the gates), scripts/hooks/pre-commit (the hook),
`scripts/check.py ci` (CI) and the public export all call it, so they can't drift. See docs/dehardcode.md.

Where the private words come from, in this order (none of them is ever written in a tracked file):
  1. the runner's own login, home-directory name and GECOS name words (4+ letters);
  2. HERMES_FINANCE_TEST_PRIVATE, a comma-separated env var (untracked: your shell, not the repo);
  3. the file named by HERMES_FINANCE_PRIVATE_FILE, else `.private-words` at the repo root (ignored by .gitignore):
     one word or phrase per line, `#` starts a comment.
A private word matches case-insensitively as a whole word (not inside a longer word or number). A caller may pass
extra `tokens`, which match case-insensitively anywhere (a VIN prefix inside a VIN).

Runs on the system python3 as well as the project's (a hook may run under either): Python 3.9+, stdlib only.
"""
from __future__ import annotations

import fnmatch
import os
import pwd
import re
import subprocess
import sys
from pathlib import Path
from typing import Iterable, Iterator, List, Optional, Sequence, Set, Tuple

ROOT = Path(__file__).resolve().parents[1]

# ---- The allowlist: what is scanned, and what is exempt (and why) ----
# Scanned: everything that ships and runs in someone else's install, the app (static/ is inside src/) and the Hermes
# plugin. scripts/, docs/ and every other shipped file: the release scan below (`--release`), over the export.
SCANNED_ROOTS = ("src", "hermes_plugin")
# The test fixtures and their generators (conftest, demo_data, gate_dbs, sync_scene, rules_scene, factory, fixtures/):
# synthetic look-alikes only, never a real value, so the gate scans them for the private words too (not the shapes:
# their synthetic VINs, tailnet hosts and tokens are deliberate; the release scan judges those, with its exemptions)
FIXTURE_ROOTS = ("tests",)
# Text files only: a binary (a font, a PNG, the vendored Chart.js's map) can't hold a word a person typed.
TEXT_SUFFIXES = {".py", ".js", ".css", ".html", ".svg", ".webmanifest", ".json", ".yaml", ".yml", ".md", ".txt",
                 ".toml", ".sql", ".sh", ".csv", ".ofx", ".qfx", ".cjs", ".cfg", ".ini", ""}
# Exempt paths (fnmatch patterns relative to the repo root), each with its reason. There are none today: nothing under
# SCANNED_ROOTS has a reason to hold a personal word. Add one only with its reason, e.g.
#   "src/hermes_finance/static/vendor/*": "third-party code, not ours to edit",
EXEMPT: dict = {}

PRIVATE_ENV = "HERMES_FINANCE_TEST_PRIVATE"
PRIVATE_FILE_ENV = "HERMES_FINANCE_PRIVATE_FILE"
DEFAULT_PRIVATE_FILE = ROOT / ".private-words"
MIN_OWN = 4   # the runner's own words shorter than this are too common to mean anything ("ab", "lee")
# A CI runner's or a container's account is a word any code has (`root`, "the runner", "a user"), not a person's
SERVICE_LOGINS = {"root", "runner", "user", "users", "admin", "build", "node", "ubuntu", "vagrant", "jenkins",
                  "circleci", "nobody", "github", "docker", "home"}


def exempt(rel: str) -> Optional[str]:
    """The reason `rel` (a repo-relative POSIX path) is exempt, or None."""
    for pat, why in EXEMPT.items():
        if fnmatch.fnmatch(rel, pat):
            return why
    return None


def scanned(rel: str, roots: Sequence[str] = SCANNED_ROOTS) -> bool:
    """Whether the check covers `rel` (a repo-relative POSIX path): under a scanned root, a text file, not exempt."""
    p = Path(rel)
    return (bool(p.parts) and p.parts[0] in roots and "__pycache__" not in p.parts
            and p.suffix in TEXT_SUFFIXES and exempt(rel) is None)


def own_words() -> Set[str]:
    """The runner's login, home-directory name and GECOS name words of MIN_OWN+ letters, lowercased."""
    try:
        user = pwd.getpwuid(os.getuid())
    except KeyError:
        return set()
    own = {user.pw_name, Path(user.pw_dir).name, *re.findall(r"[a-z0-9]+", (user.pw_gecos or "").lower())}
    return {w.lower() for w in own if len(w) >= MIN_OWN} - SERVICE_LOGINS


def file_words(path: Optional[Path]) -> Set[str]:
    if path is None or not path.is_file():
        return set()
    out = set()
    for line in path.read_text(errors="replace").splitlines():
        w = line.split("#", 1)[0].strip().lower()
        if w:
            out.add(w)
    return out


def private_words(env: Optional[dict] = None, include_own: bool = True) -> Set[str]:
    """Every private word, from the three sources in the module docstring (lowercased, empty ones dropped)."""
    env = os.environ if env is None else env
    words = own_words() if include_own else set()
    words |= {w.strip().lower() for w in env.get(PRIVATE_ENV, "").split(",")}
    named = env.get(PRIVATE_FILE_ENV)
    words |= file_words(Path(named) if named else DEFAULT_PRIVATE_FILE)
    return words - {""}


class Matcher:
    """Finds private words (whole words) and tokens (anywhere), case-insensitively."""

    def __init__(self, words: Iterable[str] = (), tokens: Iterable[str] = ()):
        parts = [rf"(?<![a-z0-9]){re.escape(w)}(?![a-z0-9])" for w in sorted(set(words), key=len, reverse=True) if w]
        parts += [re.escape(t) for t in sorted(set(tokens), key=len, reverse=True) if t]
        self.rx = re.compile("|".join(parts), re.I) if parts else None

    def scan(self, rel: str, text: str) -> List[Tuple[str, int, str]]:
        """[(path, line number, token as found)] in `text`."""
        if self.rx is None:
            return []
        return [(rel, n, m.group(0)) for n, line in enumerate(text.splitlines(), 1) for m in self.rx.finditer(line)]


def tree_files(root: Path = ROOT, roots: Sequence[str] = SCANNED_ROOTS) -> Iterator[str]:
    """Every scanned file of the working tree under `roots`, repo-relative."""
    for base in roots:
        for p in sorted((root / base).rglob("*")):
            rel = p.relative_to(root).as_posix()
            if p.is_file() and scanned(rel, roots):
                yield rel


def _git(root: Path, *args: str) -> bytes:
    return subprocess.run(["git", *args], cwd=root, check=True, stdout=subprocess.PIPE).stdout


def staged_files(root: Path) -> List[str]:
    """The staged (added, copied, modified, renamed) paths under the scanned roots, repo-relative."""
    out = _git(root, "diff", "--cached", "--name-only", "--diff-filter=ACMR", "-z", "--", *SCANNED_ROOTS)
    return [p for p in out.decode("utf-8", "replace").split("\0") if p and scanned(p)]


def staged_text(root: Path, rel: str) -> str:
    """The index's content of `rel` (what the commit will hold), not the working file's."""
    return _git(root, "show", f":{rel}").decode("utf-8", "replace")


def check(paths: Sequence[str], read, matcher: Matcher) -> List[Tuple[str, int, str]]:
    hits = []
    for rel in paths:
        hits += matcher.scan(rel, read(rel))
    return hits


# ---- The release scan: every file a release ships, text line by line, a binary as its raw bytes ----
# Rules (the name is what a hit prints; a hit never echoes a credential's value):
#   private-word   a private word (the sources above), whole word, case-insensitive
#   personal-path  an absolute path into someone's home: /home/<x>, /Volumes/<x>, macOS's Users/<x>, C:\Users\<x>
#   token:<kind>   a real credential's shape (Plaid production/development, Alpaca, OpenAI/Anthropic, GitHub, Slack,
#                  AWS, a private key) or a long high-entropy value assigned to a *_SECRET/*_KEY/*_TOKEN/password
#   email          an email address outside the reserved/placeholder domains (PLACEHOLDER_DOMAINS)
#   long-id        a 17-20 digit number (a Discord snowflake, an account id) that isn't obviously made up
#   hex-id         a standalone 12-hex id (a cron job id) that isn't obviously made up
# The exact public strings masked before matching, each with its reason. Nothing else is allowed by text. None today.
# The repository's own URL is the one runtime mask (own_repo_slugs): the release tooling writes it into CHANGELOG.md
# (compare and commit links), and once the repo is reachable at that URL the owner's login is public there anyway.
# The mask is the exact `github.com/<owner>/<repo>` string, read at runtime and never written in a tracked file, so
# the login alone stays a private word everywhere else.
ALLOWED_PUBLIC: dict = {}
# Where the repository's own URL comes from (each optional): CI's GITHUB_REPOSITORY (`owner/repo`), the development
# checkout's HERMES_FINANCE_PUBLIC_URL (as scripts/sync_release.py uses), and the scanned checkout's git remotes.
REPO_SLUG_ENV = ("GITHUB_REPOSITORY", "HERMES_FINANCE_PUBLIC_URL")
_GITHUB_URL = re.compile(r"(?:^|[/@:])github\.com[/:]([A-Za-z0-9-]+)/([A-Za-z0-9._-]+?)(?:\.git)?/?$")


def _slug_of(value: str) -> Optional[str]:
    """`github.com/owner/repo` from an `owner/repo` pair or a GitHub https/ssh URL, else None."""
    value = value.strip()
    m = _GITHUB_URL.search(value)
    if m:
        return f"github.com/{m.group(1)}/{m.group(2)}"
    m = re.fullmatch(r"([A-Za-z0-9-]+)/([A-Za-z0-9._-]+)", value)
    return f"github.com/{m.group(1)}/{m.group(2)}" if m else None


def own_repo_slugs(base: Optional[Path] = None, env: Optional[dict] = None) -> Set[str]:
    """The repository's own `github.com/<owner>/<repo>` strings, from the environment and `base`'s git remotes."""
    env = os.environ if env is None else env
    found = [env.get(k, "") for k in REPO_SLUG_ENV]
    if base is not None and (base / ".git").exists():
        try:   # every remote of the scanned checkout: a clone's origin, a development checkout's `public` remote
            out = _git(base, "config", "--get-regexp", r"^remote\..*\.url$").decode("utf-8", "replace")
            found += [line.split(" ", 1)[1] for line in out.splitlines() if " " in line]
        except Exception:
            pass
    return {s for s in (_slug_of(v) for v in found if v) if s}
# Placeholder user names an example path may use (/home/you/..., /home/<user>/...): never a real account
PLACEHOLDER_USERS = {"you", "<you>", "me", "<me>", "user", "<user>", "username", "<username>", "$user", "${user}",
                     "name", "<name>", "owner", "jane", "someone", "example", "...", "…", "*", "runner"}
# Reserved (RFC 2606/6761) and placeholder email domains
PLACEHOLDER_DOMAINS = re.compile(r"(?:^|\.)(?:example\.(?:com|org|net)|example|test|invalid|localhost|"
                                 r"users\.noreply\.github\.com)$", re.I)
# Release-scan exemptions: (path pattern (fnmatch, relative to the scanned dir), rule, regex the hit's text must match
# (None: any), reason). A hit is exempt only when all three match. Each is a committed, reasoned decision.
RELEASE_EXEMPT: List[Tuple[str, str, Optional[str], str]] = [
    ("tests/*", "token:plaid", r"-(?:aaaaaaaa|bbbbbbbb|cccccccc|deadbeef|11111111)-(?:1111-)?2222-3333-4444",
     "synthetic Plaid tokens of the tests (repeated-digit groups): never issued, they only exercise the token handling"),
    ("tests/*", "token:openai", r"^sk-or-(?:test|fake)-",
     "synthetic OpenRouter keys of the tests ('sk-or-test-', 'sk-or-fake-'): placeholders the AI code is driven with"),
    ("tests/test_doctor.py", "token:assigned", r"SENTINEL-",
     "sentinel values the doctor test plants to prove `finance doctor` never prints a credential"),
    ("tests/fixtures/import/card.qfx", "long-id", r"^2026\d{4}2469216000\d$",
     "the card fixture's synthetic OFX FITIDs: the posting date, then a made-up counter"),
    ("tests/test_importfile.py", "long-id", r"^2026\d{4}2469216000\d$",
     "the importer test's expectation of the card fixture's synthetic FITIDs (see card.qfx)"),
    ("tests/test_plugin_crons.py", "hex-id", r"^0f1e2d" r"3c4b5a$",   # (split: this file holds no 12-hex id itself)
     "a made-up job id the crons test plants to prove its own leak check catches a 12-hex id"),
    ("tests/test_valuation.py", "hex-id", r"^82633a" r"004352$",
     "the tail of the textbook sample VIN 1HGCM82633A004352, spaced out to test VIN clean-up: not an id"),
]
# What ships: in a development checkout, scripts/public_allowlist.txt (the public export's rules) decides, so
# `--release-tracked` / `--release-staged` scan exactly what the export would hold. Where that file isn't (a released
# tree: the export leaves it out), every tracked file ships and is scanned.
ALLOWLIST = "scripts/public_allowlist.txt"

_PATH_RX = re.compile(r"(?<![\w.~-])/(?:Users|home|Volumes)/([^/\s\"'`<>:;,()\[\]{}|]+)"
                      r"|(?<![\w])[A-Za-z]:\\{1,2}Users\\{1,2}([^\\\s\"'`<>:;,]+)")
_TOKEN_RX = [
    ("token:plaid", re.compile(r"\b(?:access|public|link)-(?:production|development)-[0-9a-f]{8}-[0-9a-f-]{4,}", re.I)),
    ("token:alpaca", re.compile(r"\b[PA]K[A-Z0-9]{18}\b")),
    ("token:anthropic", re.compile(r"\bsk-ant-[A-Za-z0-9_-]{20,}")),
    ("token:openai", re.compile(r"\bsk-(?!ant-)(?:proj-)?[A-Za-z0-9_-]{20,}")),
    ("token:github", re.compile(r"\b(?:gh[pousr]_[A-Za-z0-9]{30,}|github_pat_[A-Za-z0-9_]{30,})")),
    ("token:slack", re.compile(r"\bxox[abp]-[A-Za-z0-9-]{10,}")),
    ("token:aws", re.compile(r"\bAKIA[0-9A-Z]{16}\b")),
    ("token:private-key", re.compile(r"-----BEGIN (?:[A-Z]+ )*PRIVATE KEY-----")),
]
# NAME_SECRET = "value" / name_key: 'value' / password="value": the value is judged by its entropy (assigned_secret())
_ASSIGN_RX = re.compile(r"(?i)\b([\w.-]*(?:secret|_key|token|password|passwd)\w*)[\"']?\s*[:=]\s*[\"']([^\"'\s]{16,})[\"']")
_EMAIL_RX = re.compile(r"[A-Za-z0-9._%+-]+@([A-Za-z0-9-]+(?:\.[A-Za-z0-9-]+)*\.[A-Za-z]{2,})")
_LONG_ID_RX = re.compile(r"(?<![\w.])\d{17,20}(?![\w.])")
_HEX_ID_RX = re.compile(r"(?<![0-9A-Za-z#_-])[0-9a-f]{12}(?![0-9A-Za-z_-])")


def _entropy(s: str) -> float:
    import math
    n = len(s)
    return -sum(c / n * math.log2(c / n) for c in (s.count(ch) for ch in set(s)))


def assigned_secret(value: str) -> bool:
    """A value assigned to a credential-named variable that looks real: long, letters and digits mixed, high entropy
    (a placeholder like "test-secret", "xxxxxxxx", "${PLAID_SECRET}" or "changeme-please-now" is not)."""
    return (len(value) >= 20 and bool(re.search(r"\d", value)) and bool(re.search(r"[A-Za-z]", value))
            and not re.search(r"[${}<>]", value) and _entropy(value) >= 4.0)


def synthetic_number(s: str) -> bool:
    """A 17-20 digit number that is obviously made up: few distinct digits (1000000000000000000, 1212121212121212121),
    or a run up or down (12345678901234567890, 98765432109876543210)."""
    up, down = "1234567890" * 3, "9876543210" * 3
    return len(set(s)) <= 3 or s in up or s in down


def synthetic_hex(s: str) -> bool:
    """A 12-hex string that is obviously made up or isn't an id: few distinct characters (000000000000, abcabcabcabc),
    a run (0123456789ab, abcdef012345), or no digit / no letter at all (a word, a plain number)."""
    runs = "0123456789abcdef" * 2
    return (len(set(s)) <= 4 or s in runs or s in runs[::-1] or not re.search(r"[0-9]", s)
            or not re.search(r"[a-f]", s))


def _mask_public(text: str, masks: Iterable[str] = ()) -> str:
    for s in [*ALLOWED_PUBLIC, *masks]:
        text = re.sub(re.escape(s), lambda m: "_" * len(m.group(0)), text, flags=re.I)
    return text


def release_hits_in_text(text: str, words: Iterable[str], masks: Iterable[str] = ()) -> List[Tuple[int, str, str]]:
    """[(line, rule, matched text)] in `text` (a whole file: a binary decoded as latin-1, so each byte is one char).
    `masks`: the repository's own URL strings (own_repo_slugs), masked like ALLOWED_PUBLIC."""
    text = _mask_public(text, masks)
    found: List[Tuple[int, str, str]] = []

    def add(pos: int, rule: str, s: str) -> None:
        found.append((text.count("\n", 0, pos) + 1, rule, s))

    m = Matcher(words)
    if m.rx is not None:
        for x in m.rx.finditer(text):
            add(x.start(), "private-word", x.group(0))
    for x in _PATH_RX.finditer(text):
        who = (x.group(1) or x.group(2) or "").lower()
        if who not in PLACEHOLDER_USERS and not who.startswith(("<", "$", "{")):
            add(x.start(), "personal-path", x.group(0))
    for rule, rx in _TOKEN_RX:
        for x in rx.finditer(text):
            add(x.start(), rule, x.group(0))
    for x in _ASSIGN_RX.finditer(text):
        if assigned_secret(x.group(2)):
            add(x.start(), "token:assigned", x.group(0))
    for x in _EMAIL_RX.finditer(text):
        if not PLACEHOLDER_DOMAINS.search(x.group(1)):
            add(x.start(), "email", x.group(0))
    for x in _LONG_ID_RX.finditer(text):
        if not synthetic_number(x.group(0)):
            add(x.start(), "long-id", x.group(0))
    for x in _HEX_ID_RX.finditer(text):
        if not synthetic_hex(x.group(0)):
            add(x.start(), "hex-id", x.group(0))
    return sorted(found)


def is_binary(data: bytes) -> bool:
    if b"\0" in data[:8192]:
        return True
    try:
        data.decode("utf-8")
    except UnicodeDecodeError:
        return True
    return False


def release_exempt(rel: str, rule: str, s: str) -> Optional[str]:
    for pat, r, rx, why in RELEASE_EXEMPT:
        if fnmatch.fnmatch(rel, pat) and r == rule and (rx is None or re.search(rx, s)):
            return why
    return None


def release_scan_bytes(rel: str, data: bytes, words: Iterable[str],
                       masks: Iterable[str] = ()) -> List[Tuple[str, int, str, str]]:
    """[(path, line, rule, matched text)] of one file's content, exemptions applied."""
    text = data.decode("latin-1") if is_binary(data) else data.decode("utf-8")
    return [(rel, n, rule, s) for n, rule, s in release_hits_in_text(text, words, masks)
            if release_exempt(rel, rule, s) is None]


def release_dir_files(base: Path) -> List[str]:
    """Every file under `base`, relative POSIX paths, `.git/` aside (the scanned tree, not its repository)."""
    out = []
    for dirpath, dirnames, filenames in os.walk(base):
        dirnames[:] = sorted(d for d in dirnames if not (d == ".git" and Path(dirpath) == base))
        for f in sorted(filenames):
            p = Path(dirpath) / f
            if p.is_file() and not p.is_symlink():
                out.append(p.relative_to(base).as_posix())
    return out


def shipped_filter(root: Path):
    """A predicate: whether a tracked path of `root` ships (the export's allowlist decides; see ALLOWLIST). A path the
    allowlist doesn't decide is scanned: the export itself refuses it until someone decides."""
    if not (root / ALLOWLIST).is_file() or not (root / "scripts" / "export_public.py").is_file():
        return lambda rel: True
    import importlib.util
    spec = importlib.util.spec_from_file_location("_export_public", root / "scripts" / "export_public.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    rules = mod.load_rules(root / ALLOWLIST)

    def ships(rel: str) -> bool:
        r = mod.decide(rel, rules)
        return r is None or r.action == "include"
    return ships


def tracked_files(root: Path) -> List[str]:
    out = _git(root, "ls-files", "-z")
    return [p for p in out.decode("utf-8", "replace").split("\0") if p and (root / p).is_file()]


def release_scan(base: Path, files: Sequence[str], words: Iterable[str]) -> List[Tuple[str, int, str, str]]:
    words = set(words)
    masks = own_repo_slugs(base)
    hits = []
    for rel in files:
        hits += release_scan_bytes(rel, (base / rel).read_bytes(), words, masks)
    return hits


def format_release_hit(rel: str, n: int, rule: str, s: str) -> str:
    """`path:line: rule`: never the text found (a private word, a credential, an email, an id, a path), since the scan
    runs in CI, whose log is public."""
    return f"{rel}:{n}: {rule}"


def main(argv: Optional[Sequence[str]] = None) -> int:
    args = list(sys.argv[1:] if argv is None else argv)
    if args[:1] in (["--release"], ["--release-tracked"], ["--release-staged"]):
        if len(args) != (2 if args[0] == "--release" else 1):
            print(__doc__.split("\n\n")[1], file=sys.stderr)
            return 2
        if args[0] == "--release":
            base = Path(args[1]).resolve()
            if not base.is_dir():
                print(f"dehardcode: {args[1]} is not a directory", file=sys.stderr)
                return 2
            files = release_dir_files(base)
            hits = release_scan(base, files, private_words())
        else:
            base = Path(_git(Path.cwd(), "rev-parse", "--show-toplevel").decode().strip())
            ships = shipped_filter(base)
            if args[0] == "--release-tracked":
                files = [f for f in tracked_files(base) if ships(f)]
                hits = release_scan(base, files, private_words())
            else:
                out = _git(base, "diff", "--cached", "--name-only", "--diff-filter=ACMR", "-z")
                files = [f for f in out.decode("utf-8", "replace").split("\0") if f and ships(f)]
                words = private_words()
                masks = own_repo_slugs(base)
                hits = []
                for rel in files:
                    hits += release_scan_bytes(rel, _git(base, "show", f":{rel}"), words, masks)
        for h in hits:
            print(format_release_hit(*h))
        print(f"release scan: {len(files)} files, {len(hits)} hit(s)", file=sys.stderr)
        return 1 if hits else 0
    staged = "--staged" in args
    paths = [a for a in args if a != "--staged"]
    if any(a.startswith("-") for a in paths) or (staged and paths):
        print(__doc__.split("\n\n")[1], file=sys.stderr)
        return 2
    root = Path(_git(Path.cwd(), "rev-parse", "--show-toplevel").decode().strip()) if staged else ROOT
    matcher = Matcher(private_words())
    if staged:
        files = staged_files(root)
        hits = check(files, lambda rel: staged_text(root, rel), matcher)
    else:
        files = [Path(p).resolve().relative_to(root).as_posix() if Path(p).is_absolute() else p for p in paths] or list(tree_files(root))
        files = [f for f in files if scanned(f)]
        hits = check(files, lambda rel: (root / rel).read_text(errors="replace"), matcher)
    for rel, n, tok in hits:
        print(f"{rel}:{n}: {tok}")
    return 1 if hits else 0


if __name__ == "__main__":
    sys.exit(main())
