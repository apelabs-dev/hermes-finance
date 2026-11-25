"""The bounded child process that reads a statement PDF: page/word caps, a wall clock and a memory ceiling.

The web routes re-parse a request's file (preview, ai_view and commit each do), and `_check_raw` bounds only the
file size. pdfminer's decompression and page walk are not bounded by that: the review measured a 0.9 KB double-Flate
file at 333 MB, a 15.8 KB corrupt-Flate file at 3.9 s / 1.0 GB and 12,000 pages at 10.2 s / 1.28 GB. So `words.read`
and `words.producer` run in a child process, as the Ask panel's read tools do (`chat._isolated`): the parent holds the
wall clock, and the child is alone in its own process group, so one kill takes it and anything it started down.

The memory ceiling is enforced with an RSS sampler on both POSIX systems, plus an address-space backstop on Linux:

- Both: the parent samples the child's RSS (`/proc/<pid>/status` on Linux, `ps` on macOS) every 50 ms and kills its
  process group once it passes the ceiling.
- Linux: the child also lowers `RLIMIT_AS` to a 1 GB backstop before it imports pdfplumber, so an allocation that the
  sampler misses fails with `MemoryError` inside the child (reported as a refusal, never a traceback). It is not the
  ceiling itself: a Python image's address space (glibc arenas, shared libraries) is already hundreds of MB.
- macOS: `RLIMIT_AS` cannot be lowered below the address space Python has already mapped (the dyld shared cache puts
  it in the hundreds of GB), so it raises `ValueError` there and the sampler is the whole ceiling.
- Windows: `resource` does not exist there, and neither does `os.killpg`; the read degrades to the wall clock and the
  page/word/character caps, with no memory ceiling and no child-side `signal.alarm` (the parent's clock still kills the
  child). `_warn_without_ceiling` logs that once, so a deployment there is not silently weaker.

At most `SLOTS` reads run at once: the review measured 6 simultaneous uploads as 6 live children. A child's peak is
not the 256 MB ceiling: on macOS the review measured 307-786 MB, because a fast inflate outruns one 50 ms `ps`
sample and there is no `RLIMIT_AS` backstop there (above).

Every limit hit raises `words.TooComplex`, which the importer turns into the product's `pdf_too_complex` refusal.
"""

from __future__ import annotations

import base64
import contextlib
import json
import logging
import math
import os
import signal
import subprocess
import sys
import threading
from dataclasses import dataclass
from pathlib import Path

from . import words

log = logging.getLogger(__name__)

SRC = str(Path(__file__).resolve().parent.parent.parent)   # the directory hermes_finance is imported from
MAX_OUT = 64 * 1024 * 1024                         # the child's JSON answer (the word cap bounds it well below this)
SAMPLE = 0.05                                      # seconds between two RSS samples
SLOTS = 2                                          # reads at once (a macOS child can peak 307-786 MB between samples)

_SLOTS = threading.BoundedSemaphore(SLOTS)


@dataclass(frozen=True)
class Limits:
    """What one PDF read may use. A real statement is a few dozen pages; measured here at 40 pages: 127 MB, 0.9 s."""
    pages: int = 50
    words: int = 40_000
    seconds: float = 10.0
    memory_mb: int = 256


LIMITS = Limits()

# What the child runs: set the hard limits first (before pdfplumber is imported), arm its own alarm (it ends even if
# the parent is gone; SIGALRM is not caught), then run guard._child. `resource` and `signal.alarm` are guarded for
# Windows, where neither exists: there the parent's clock is the only limit, plus the page and word caps.
CHILD = (
    "import sys\n"
    "try:\n"
    "    import resource\n"
    "except ImportError:\n"
    "    resource = None\n"
    "if resource is not None:\n"
    "    for _name, _value in ((getattr(resource, 'RLIMIT_CPU', None), int(sys.argv[3])),\n"
    "                          (getattr(resource, 'RLIMIT_AS', None), int(sys.argv[2]))):\n"
    "        if _name is not None:\n"
    "            try:\n"
    "                resource.setrlimit(_name, (_value, _value))\n"
    "            except (ValueError, OSError):\n"
    "                pass\n"
    "try:\n"
    "    import signal\n"
    "    signal.alarm(int(sys.argv[6]))\n"
    "except (ImportError, AttributeError):\n"
    "    pass\n"
    "sys.meta_path = [f for f in sys.meta_path if 'hermes_finance' not in str(getattr(f, 'MAPPING', ''))]\n"
    "sys.path.insert(0, sys.argv[1])\n"
    "from hermes_finance.pdfimport import guard\n"
    "guard._child(int(sys.argv[4]), int(sys.argv[5]))\n"
)


def _has_memory_sampler() -> bool:
    """Whether this platform can sample the child's RSS (`/proc` on Linux, `ps` on macOS and the BSDs).

    Windows has neither, `resource` and `os.killpg` do not exist there either, so a read there has no memory ceiling
    (the wall clock and the page/word/character caps still apply). The old code documented that in a docstring only.
    """
    return os.name == "posix"


_warned_no_ceiling = False


def _warn_without_ceiling() -> None:
    """One log line, once, when the read has no memory ceiling on this platform."""
    global _warned_no_ceiling
    if _has_memory_sampler() or _warned_no_ceiling:
        return
    _warned_no_ceiling = True
    log.warning("pdf read: no memory ceiling on this platform (no RSS sampler); the wall clock and the page, word "
                "and character caps still apply")


def read(raw: bytes, password: str | None = None, *, limits: Limits | None = None):
    """(pages, sizes, producer) for one PDF, or words.TooComplex when a limit is hit; any other failure is
    words.WordReadError (never a raw pdfminer exception on the request thread).

    At most SLOTS reads run at once (a child peaks above the 256 MB ceiling — 307-786 MB measured on macOS — so N
    uploads were N x that); a read that has to wait for a slot waits before its clock starts.
    """
    _warn_without_ceiling()
    limits = LIMITS if limits is None else limits
    with _SLOTS:
        return _read_now(raw, password, limits)


def _read_now(raw: bytes, password: str | None, limits: Limits):
    """One read: build the request, run the child under the clock and the sampler, answer or raise.

    The reply is read in 64 KiB chunks while a writer thread feeds the request: the parent never holds more than
    `MAX_OUT` plus one chunk of the child's answer, and a reply that passes `MAX_OUT` is killed at its process group
    as it is read, not after `communicate()` has buffered all of it. The writer is a thread because the request is up
    to ~13 MB (a 10 MB file, base64): a single-threaded chunked read would deadlock once the child's stdin pipe filled.
    """
    body = json.dumps({"raw": base64.b64encode(raw).decode("ascii"), "password": password}).encode()
    # RLIMIT_AS is a backstop for the RSS sampler, not the ceiling itself: a Python image's address space (arenas,
    # shared libraries) is already hundreds of MB, so a 256 MB hard AS limit would break every read on Linux.
    addrspace = max(1024, int(limits.memory_mb)) * 1024 * 1024
    cpu = max(1, int(math.ceil(limits.seconds)) + 1)
    argv = [sys.executable, "-I", "-c", CHILD, SRC, str(addrspace), str(cpu), str(max(1, int(limits.pages))),
            str(max(1, int(limits.words))), str(cpu)]
    kwargs = {"start_new_session": True} if _has_memory_sampler() else {}
    try:
        proc = subprocess.Popen(argv, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, **kwargs)
    except OSError as exc:
        words.log_failure(type(exc).__name__)
        raise words.WordReadError() from None
    over = {"memory": False}
    stop = threading.Event()
    watch = None
    if _has_memory_sampler():
        watch = threading.Thread(target=_sample_memory, args=(proc, limits.memory_mb, stop, over), daemon=True)
        watch.start()
    reply = bytearray()
    reply_over = threading.Event()

    def feed() -> None:
        """Write the whole request, then close stdin: the child reads it to EOF before it answers."""
        try:
            proc.stdin.write(body)
            proc.stdin.flush()
            proc.stdin.close()
        except (OSError, ValueError):
            pass        # the child was killed (or closed its end) first: its reply or the kill is the answer

    def drain() -> None:
        """Read the reply in 64 KiB chunks; kill the group once it passes MAX_OUT, before buffering any more.

        `read1`, not `read`: `read(n)` waits for n bytes, so a child that passes the cap and then goes quiet would
        keep the parent waiting for a full chunk (to the clock) instead of being killed at the cap.
        """
        try:
            while True:
                chunk = proc.stdout.read1(65536)
                if not chunk:
                    return
                reply.extend(chunk)
                if len(reply) > MAX_OUT:
                    reply_over.set()
                    _kill(proc)
                    return
        except (OSError, ValueError):
            return

    writer = threading.Thread(target=feed, daemon=True)
    reader = threading.Thread(target=drain, daemon=True)
    writer.start()
    reader.start()
    timed_out = False
    try:
        try:
            proc.wait(timeout=max(0.1, limits.seconds))
        except subprocess.TimeoutExpired:
            timed_out = True
            _kill(proc)
    finally:
        stop.set()
        if watch is not None:
            watch.join(2)
        if proc.poll() is None:
            _kill(proc)
        reader.join(5)      # it ends at EOF, or right after the kill above
        writer.join(5)      # and it ends when the child's stdin closes, or with the child
        with contextlib.suppress(Exception):
            proc.wait()
        # Only a stream whose thread has finished: closing one that is still blocked in a read/write would wait on
        # the buffer lock that thread holds, hanging the request thread (and its slot) for good. Nothing escapes the
        # process group, so a thread still here after 5 s is a stuck pipe, not a live child.
        for thread, stream in ((reader, proc.stdout), (writer, proc.stdin)):
            if not thread.is_alive():
                with contextlib.suppress(Exception):
                    stream.close()
    if timed_out or over["memory"] or reply_over.is_set():
        raise words.TooComplex() from None
    if len(reply) > MAX_OUT:          # (a reply that finished after the child exited without the chunk check firing)
        raise words.TooComplex() from None
    return _answer(bytes(reply))


def _answer(out: bytes):
    """The child's JSON answer as (pages, sizes, producer), or the words exception it reported."""
    try:
        answer = json.loads(out)
    except (ValueError, TypeError):
        raise words.WordReadError() from None
    if not isinstance(answer, dict):
        raise words.WordReadError() from None
    if answer.get("refused") == "password_required":
        raise words.PasswordRequired() from None
    if answer.get("refused") == "password_wrong":
        raise words.PasswordWrong() from None
    if answer.get("refused") == "pdf_too_complex":
        raise words.TooComplex() from None
    if answer.get("refused") == "pdf_unreadable" or "crashed" in answer:
        name = answer.get("type") or answer.get("crashed") or "WordReadError"
        if answer.get("crashed") == "MemoryError":
            # MemoryError escaped the child's own handling: the ceiling was hit, not a malformed file.
            raise words.TooComplex() from None
        words.log_failure(str(name)[:80])   # (the child's own log went to its discarded stderr)
        raise words.WordReadError() from None
    pages, sizes, producer = answer.get("pages"), answer.get("sizes"), answer.get("producer")
    if not isinstance(pages, list) or not isinstance(sizes, list) or not isinstance(producer, str):
        raise words.WordReadError() from None
    return pages, [(float(w), float(h)) for w, h in sizes], producer


def _kill(proc: subprocess.Popen) -> None:
    if os.name == "posix":
        with contextlib.suppress(ProcessLookupError, PermissionError):
            os.killpg(proc.pid, signal.SIGKILL)
    else:
        with contextlib.suppress(OSError):
            proc.kill()


def _sample_memory(proc: subprocess.Popen, limit_mb: int, stop: threading.Event, over: dict) -> None:
    """Kill the child's group once its RSS passes the ceiling (the macOS route; a Linux backstop). `poll()` first, so
    the pid can't have been reaped and reused between a sample and the kill."""
    while not stop.wait(SAMPLE):
        if proc.poll() is not None:
            return
        kb = _rss_kb(proc.pid)
        if kb is not None and kb > limit_mb * 1024 and proc.poll() is None:
            over["memory"] = True
            _kill(proc)
            return


def _rss_kb(pid: int) -> int | None:
    try:
        with open(f"/proc/{pid}/status", encoding="ascii", errors="ignore") as f:
            for line in f:
                if line.startswith("VmRSS:"):
                    return int(line.split()[1])
    except (OSError, ValueError, IndexError):
        pass
    try:   # macOS and the BSDs: ps reports kilobytes for its own user's processes
        out = subprocess.run(["ps", "-o", "rss=", "-p", str(pid)], capture_output=True, text=True, timeout=1)
        return int(out.stdout.strip()) if out.stdout.strip() else None
    except (OSError, ValueError, subprocess.SubprocessError):
        return None


def _child(max_pages: int, max_words: int) -> None:
    """The child side: read the request on stdin, answer with the word pages or the refusal code, JSON both."""
    req = json.loads(sys.stdin.buffer.read())
    raw = base64.b64decode(req.get("raw") or "")
    try:
        pages, sizes = words.read(raw, req.get("password"), max_pages=max_pages, max_words=max_words)
        producer = words.producer(raw, req.get("password"))
    except words.TooComplex:
        answer = {"refused": "pdf_too_complex"}
    except words.PasswordRequired:
        answer = {"refused": "password_required"}
    except words.PasswordWrong:
        answer = {"refused": "password_wrong"}
    except words.WordReadError as exc:
        answer = {"refused": "pdf_unreadable", "type": getattr(exc, "cause", None) or type(exc).__name__}
    except MemoryError:
        answer = {"refused": "pdf_too_complex"}
    except Exception as exc:  # noqa: BLE001 - the type only: a message can quote the PDF's bytes
        answer = {"crashed": type(exc).__name__}
    else:
        try:
            text = json.dumps({"pages": pages, "sizes": sizes, "producer": producer})
        except MemoryError:
            answer, text = {"refused": "pdf_too_complex"}, None
        except (TypeError, ValueError):
            answer, text = {"refused": "pdf_unreadable"}, None
        if text is not None:
            sys.stdout.write(text)
            sys.stdout.flush()
            return
    sys.stdout.write(json.dumps(answer))
    sys.stdout.flush()
