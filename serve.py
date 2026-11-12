#!/usr/bin/env python3
"""Serve the two rendered dashboards on http://localhost:8765/ (loopback only).

WHY THIS EXISTS
Chrome refuses file:// for these pages -- the Forge page loads forge-bundle.js and
three stylesheets as separate files, and file:// origin rules break that, so the
page renders unstyled with no components. They have to come off an HTTP origin.

WHY NOT `python3 -m http.server`
It would serve the whole warehouse directory, and that directory contains
foundry_analytics.db -- 289 MB of client conversation transcripts and verbatim
feedback comments. Loopback-only is not the same as private: any local process,
and any page in the browser, can fetch from 127.0.0.1. So this serves a fixed
allowlist of six files and 404s everything else, including the database.

WHY TWO SOCKETS
The favorited URLs are both http://localhost:8765 and http://127.0.0.1:8765. On
macOS `localhost` can resolve to ::1 first, and a socket bound to 127.0.0.1 does
not answer IPv6. Binding both loopback addresses makes either URL work without
depending on Chrome's fallback behaviour. Binding `::` would cover both with one
socket but would also expose this to the network, which is not wanted.
"""
import http.server
import os
import socketserver
import sys
import threading

ROOT = os.path.dirname(os.path.abspath(__file__))
PORT = int(os.environ.get("DASHBOARD_PORT", "8765"))

# Exactly what the two pages need. Anything else -- .db, .py, logs/, node_modules/
# -- is not served, so a path-traversal or a stray fetch cannot reach it.
ALLOWED = {
    "dashboard.html",
    "dashboard-forge.html",
    "forge-bundle.js",
    "forge.css",
    "forge-core.css",
    "forge-dark.css",
}

INDEX = """<!doctype html><meta charset=utf-8><title>Foundry analytics</title>
<style>body{font:16px/1.6 -apple-system,system-ui,sans-serif;margin:6rem auto;max-width:34rem}
a{display:block;padding:.9rem 1.1rem;margin:.5rem 0;border:1px solid #d5d9e0;border-radius:8px;
text-decoration:none;color:#15263d}a:hover{background:#f4f6f9}small{color:#667}</style>
<h1>Foundry analytics</h1>
<a href="/dashboard.html">dashboard.html<br><small>original</small></a>
<a href="/dashboard-forge.html">dashboard-forge.html<br><small>Tyler Forge rebuild</small></a>
"""


class Handler(http.server.SimpleHTTPRequestHandler):
    def __init__(self, *a, **kw):
        super().__init__(*a, directory=ROOT, **kw)

    def do_GET(self):
        if self.path in ("/", "/index.html"):
            body = INDEX.encode()
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return
        if not self._allowed():
            self.send_error(404, "Not served")
            return
        super().do_GET()

    def do_HEAD(self):
        if self.path in ("/", "/index.html"):
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.end_headers()
            return
        if not self._allowed():
            self.send_error(404, "Not served")
            return
        super().do_HEAD()

    def _allowed(self):
        # Compare the basename of the path with the query and fragment stripped,
        # so /forge.css?v=2 and /../foundry_analytics.db both resolve correctly.
        path = self.path.split("?", 1)[0].split("#", 1)[0]
        return os.path.basename(path) in ALLOWED and "/" == os.path.dirname(path)

    def end_headers(self):
        # A favorited URL plus a normal browser cache means a refreshed dashboard
        # can silently keep showing yesterday's numbers. no-cache still allows a
        # cheap 304 via Last-Modified, so the 2.6 MB page is not re-sent needlessly.
        self.send_header("Cache-Control", "no-cache, must-revalidate")
        super().end_headers()

    def log_message(self, fmt, *args):
        sys.stderr.write("%s %s\n" % (self.log_date_time_string(), fmt % args))


class V4(socketserver.ThreadingTCPServer):
    allow_reuse_address = True
    daemon_threads = True


class V6(V4):
    address_family = __import__("socket").AF_INET6


def main():
    servers = []
    for cls, addr in ((V4, ("127.0.0.1", PORT)), (V6, ("::1", PORT))):
        try:
            servers.append(cls(addr, Handler))
        except OSError as e:
            # One family failing is survivable; both failing is not.
            sys.stderr.write("could not bind %s:%d — %s\n" % (addr[0], PORT, e))
    if not servers:
        sys.stderr.write("FATAL: nothing bound on port %d\n" % PORT)
        return 1
    sys.stderr.write("serving %s (%d-file allowlist)\n" % (ROOT, len(ALLOWED)))
    for s in servers:
        host = s.server_address[0]
        # An IPv6 literal needs brackets or the address runs into the port and the
        # line is not a usable URL: http://::1:8765/ cannot be clicked or pasted.
        shown = "[%s]" % host if ":" in host else host
        sys.stderr.write("  bound  http://%s:%d/\n" % (shown, PORT))
        threading.Thread(target=s.serve_forever, daemon=True).start()
    # The index is the one link worth having in the log: it resolves over either
    # loopback family and links both dashboards, so it is the entry point to hand
    # someone (or to paste into a browser) without remembering either filename.
    sys.stderr.write("  index  http://localhost:%d/  ->  %s\n"
                     % (PORT, "  ".join(sorted(f for f in ALLOWED if f.endswith(".html")))))
    sys.stderr.flush()
    try:
        threading.Event().wait()
    except KeyboardInterrupt:
        return 0


if __name__ == "__main__":
    sys.exit(main() or 0)
