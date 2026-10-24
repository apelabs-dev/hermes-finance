#!/usr/bin/env python3
"""Serve a built demo on this computer (standard library only).

    python3 scripts/serve_demo.py [dist] [--port 8000] [--bind 127.0.0.1]

`cd dist && python3 -m http.server 8000` works too. This adds what Cloudflare Pages does with dist/_headers (the
Content-Security-Policy and the other headers, image/png for the logo files that have no extension), and a listen
backlog of 64: a page load asks for ~30 files at once, and http.server's default of 5 reset some of them now and then
(the app's own server raised it for the same reason: web._Server).
"""

from __future__ import annotations

import argparse
import fnmatch
import functools
import sys
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def read_headers(path: Path) -> list[tuple[str, list[tuple[str, str]]]]:
    """Cloudflare Pages' _headers: a path pattern on its own line, then its "Name: value" lines, indented."""
    rules: list[tuple[str, list[tuple[str, str]]]] = []
    if not path.is_file():
        return rules
    for line in path.read_text().splitlines():
        if not line.strip() or line.lstrip().startswith("#"):
            continue
        if not line[0].isspace():
            rules.append((line.strip(), []))
        elif rules and ":" in line:
            name, _, value = line.strip().partition(":")
            rules[-1][1].append((name.strip(), value.strip()))
    return rules


class Handler(SimpleHTTPRequestHandler):
    rules: list = []

    def end_headers(self):
        url = self.path.split("?", 1)[0]
        for pattern, headers in self.rules:
            if fnmatch.fnmatchcase(url, pattern):
                for name, value in headers:
                    if name.lower() != "content-type":
                        self.send_header(name, value)
        super().end_headers()

    def guess_type(self, path):
        url = self.path.split("?", 1)[0]
        for pattern, headers in self.rules:
            if fnmatch.fnmatchcase(url, pattern):
                for name, value in headers:
                    if name.lower() == "content-type":
                        return value
        return super().guess_type(path)

    def log_message(self, fmt, *args):   # quiet: one line per error only
        if args and str(args[1] if len(args) > 1 else "").startswith(("4", "5")):
            sys.stderr.write(f"serve_demo: {self.address_string()} {fmt % args}\n")


class Server(ThreadingHTTPServer):
    request_queue_size = 64
    daemon_threads = True


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("dist", nargs="?", type=Path, default=ROOT / "dist" / "demo")
    ap.add_argument("--port", type=int, default=8000)
    ap.add_argument("--bind", default="127.0.0.1")
    args = ap.parse_args(argv)
    dist = args.dist.resolve()
    if not (dist / "index.html").is_file():
        print(f"serve_demo: {dist} has no index.html: build it first (scripts/build_demo.py)")
        return 2
    Handler.rules = read_headers(dist / "_headers")
    srv = Server((args.bind, args.port), functools.partial(Handler, directory=str(dist)))
    print(f"serve_demo: http://{args.bind}:{srv.server_address[1]}/ (Ctrl-C stops it)", flush=True)
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        srv.server_close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
