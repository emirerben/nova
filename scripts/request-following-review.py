#!/usr/bin/env python3
"""Open the local request-following review app; no frontend build required.

    python3 scripts/request-following-review.py --prod
    python3 scripts/request-following-review.py --api-url http://127.0.0.1:8000

Reads a private capture store or proxies the review API. Upstream credentials stay
in this local process. No collection, prompt mutation, or review is automatic.
"""

from __future__ import annotations

import argparse
import hmac
import json
import os
import re
import secrets
import sys
import urllib.error
import urllib.parse
import urllib.request
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from admin import PROD_URL, load_env

ROOT = Path(__file__).resolve().parents[1]
ASSETS = ROOT / "tools/request-following-review"
API_PATH = re.compile(r"/api/cases(?:/[a-f0-9-]{36}(?:/(?:review|export))?)?$")
MAX_BODY = 256_000


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def serve(*, base=None, token=None, store=None, port=4455, open_browser=True):
    session = secrets.token_urlsafe(32)
    upstream = urllib.request.build_opener(NoRedirect())

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *_args):
            pass  # Creator request content and query strings never enter logs.

        def reply(self, status, value, content_type="application/json"):
            body = value if isinstance(value, bytes) else json.dumps(value).encode()
            self.send_response(status)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.send_header("X-Content-Type-Options", "nosniff")
            self.send_header("Referrer-Policy", "no-referrer")
            self.send_header(
                "Content-Security-Policy",
                "default-src 'self'; script-src 'self'; style-src 'self'; img-src 'self' data:; object-src 'none'; frame-ancestors 'none'; base-uri 'none'",
            )
            self.end_headers()
            self.wfile.write(body)

        def allowed_host(self):
            expected = f"127.0.0.1:{self.server.server_port}"
            return self.headers.get("Host") == expected

        def do_GET(self):
            self.handle_request()

        def do_POST(self):
            self.handle_request()

        def handle_request(self):
            if not self.allowed_host():
                self.reply(
                    403, {"detail": "Open the app using its printed 127.0.0.1 address."}
                )
                return
            parsed = urllib.parse.urlsplit(self.path)
            if self.command == "GET" and parsed.path == "/config":
                self.reply(
                    200,
                    {
                        "session": session,
                        "source": "Private captures"
                        if store
                        else "Production"
                        if base == PROD_URL
                        else "Local API",
                    },
                )
                return
            files = {
                "/": ("index.html", "text/html; charset=utf-8"),
                "/app.js": ("app.js", "text/javascript; charset=utf-8"),
                "/style.css": ("style.css", "text/css; charset=utf-8"),
            }
            if self.command == "GET" and parsed.path in files:
                filename, kind = files[parsed.path]
                self.reply(200, (ASSETS / filename).read_bytes(), kind)
                return
            if not API_PATH.fullmatch(parsed.path):
                self.reply(404, {"detail": "Not found"})
                return
            if not hmac.compare_digest(
                self.headers.get("X-Review-Session", ""), session
            ):
                self.reply(403, {"detail": "Refresh the local app to reconnect."})
                return
            if self.command == "POST":
                if (
                    not parsed.path.endswith("/review")
                    or self.headers.get("Origin")
                    != f"http://127.0.0.1:{self.server.server_port}"
                ):
                    self.reply(
                        403, {"detail": "Review writes must come from this local app."}
                    )
                    return
                try:
                    length = int(self.headers.get("Content-Length", "0"))
                    if not 0 < length <= MAX_BODY:
                        raise ValueError()
                    data = self.rfile.read(length)
                    json.loads(data)
                except (ValueError, json.JSONDecodeError):
                    self.reply(400, {"detail": "Invalid review body"})
                    return
            else:
                data = None
            query = urllib.parse.parse_qs(parsed.query)
            if set(query) - {"cursor", "limit", "review_state"}:
                self.reply(400, {"detail": "Unsupported filter"})
                return
            if store is not None:
                from app.services.request_following_private_store import StoreError

                try:
                    result = store.handle(
                        self.command,
                        parsed.path,
                        query,
                        json.loads(data) if data else None,
                    )
                    self.reply(200, result)
                except StoreError as error:
                    self.reply(error.status, {"detail": error.detail})
                except (ValueError, KeyError, TypeError, OSError):
                    self.reply(
                        422,
                        {
                            "detail": "Private capture unavailable or invalid. Reimport verified evidence."
                        },
                    )
                return
            path = parsed.path.replace("/api/cases", "/admin/request-following", 1)
            if parsed.query:
                path += "?" + urllib.parse.urlencode(query, doseq=True)
            request = urllib.request.Request(
                base + path,
                data=data,
                method=self.command,
                headers={"X-Admin-Token": token, "Content-Type": "application/json"},
            )
            try:
                with upstream.open(request, timeout=60) as response:
                    self.reply(response.status, response.read())
            except urllib.error.HTTPError as error:
                if error.code == 404 and parsed.path == "/api/cases":
                    self.reply(
                        503,
                        {
                            "detail": "The review API is not installed on this server yet. Deploy the learning-loop update, then refresh."
                        },
                    )
                else:
                    self.reply(error.code, error.read())
            except (urllib.error.URLError, TimeoutError, OSError):
                self.reply(
                    502,
                    {
                        "detail": "Cannot reach the review API. Check that it is running, then retry."
                    },
                )

    server = ThreadingHTTPServer(("127.0.0.1", port), Handler)
    url = f"http://127.0.0.1:{server.server_port}"
    print(f"Review app: {url}", flush=True)
    print(
        f"Source: {'Private captures' if store else 'Production' if base == PROD_URL else base}. Ctrl+C stops the app.",
        flush=True,
    )
    if open_browser:
        webbrowser.open(url)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    source = parser.add_mutually_exclusive_group()
    source.add_argument("--prod", action="store_true")
    source.add_argument(
        "--store", type=Path, help="Review receipted private CLI captures offline"
    )
    source.add_argument("--api-url", default="http://127.0.0.1:8000")
    parser.add_argument("--port", type=int, default=4455)
    parser.add_argument("--no-open", action="store_true")
    args = parser.parse_args()
    if args.store is not None:
        sys.path.insert(0, str(ROOT / "src/apps/api"))
        from app.services.request_following_private_store import PrivateReviewStore

        private_path = args.store.resolve()
        if private_path == ROOT or ROOT in private_path.parents:
            parser.error("Keep private captures outside the repository")
        try:
            store = PrivateReviewStore(private_path)
        except ValueError as error:
            parser.error(str(error))
        serve(store=store, port=args.port, open_browser=not args.no_open)
        return
    base = PROD_URL if args.prod else args.api_url.rstrip("/")
    parsed = urllib.parse.urlsplit(base)
    if not args.prod and (
        parsed.scheme != "http"
        or parsed.hostname not in {"127.0.0.1", "localhost"}
        or parsed.username
        or parsed.password
        or parsed.path
        or parsed.query
        or parsed.fragment
    ):
        parser.error(
            "--api-url must be a loopback HTTP API; use --prod for Nova production"
        )
    values = {**load_env(ROOT / ".env"), **os.environ}
    key = "ADMIN_PROD_API_KEY" if args.prod else "ADMIN_API_KEY"
    token = values.get(key, "").strip()
    if not token:
        parser.error(f"Set {key} in the repository .env before starting the review app")
    serve(base=base, token=token, port=args.port, open_browser=not args.no_open)


if __name__ == "__main__":
    main()
