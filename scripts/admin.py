#!/usr/bin/env python3
"""Admin API CLI — call /admin/* endpoints without ever typing the raw token.

Reads ADMIN_API_KEY (local) or ADMIN_PROD_API_KEY (prod) from .env at the
repo root and injects it as X-Admin-Token. Defaults to http://localhost:8000;
--prod switches to https://nova-video.fly.dev and prompts before mutating.

Examples
--------
    python scripts/admin.py GET  /admin/templates
    python scripts/admin.py GET  templates/abc123/debug          # /admin/ auto-prefixed
    python scripts/admin.py POST /admin/templates/abc/reanalyze-agentic --json '{"use_layer2": true}'
    python scripts/admin.py --prod GET /admin/templates
    python scripts/admin.py --prod POST /admin/templates/abc/publish   # prompts y/N

Exits 0 on 2xx, 1 otherwise — composes in shell pipelines.
"""
from __future__ import annotations

import argparse
import json
import os
import re
import sys
import urllib.error
import urllib.request
from pathlib import Path

LOCAL_URL = "http://localhost:8000"
PROD_URL = "https://nova-video.fly.dev"
MUTATING_METHODS = {"POST", "PUT", "PATCH", "DELETE"}
REPO_ROOT = Path(__file__).resolve().parent.parent


def load_env(path: Path) -> dict[str, str]:
    """Tiny .env reader — KEY=VALUE lines, optional quotes, # comments. No deps.

    dotenv-style inline comments, matching how pydantic-settings reads the same
    file for the local API: an unquoted value is cut at the first
    whitespace-preceded `#`; a quoted value ends at its closing quote, so `#`
    inside quotes is literal and a comment may follow the closing quote.
    Malformed quoting (unterminated or mismatched) is handled best-effort,
    not dotenv-parity.
    """
    if not path.exists():
        return {}
    out: dict[str, str] = {}
    for raw in path.read_text().splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, val = line.partition("=")
        key = key.strip()
        val = val.strip()
        quote = val[:1]
        end = val.find(quote, 1) if quote in {'"', "'"} else -1
        if end != -1 and (
            end == len(val) - 1 or val[end + 1].isspace() or val[end + 1] == "#"
        ):
            val = val[1:end]
        else:
            val = re.split(r"\s#", val, maxsplit=1)[0].rstrip()
        out[key] = val
    return out


def resolve_path(raw: str) -> str:
    """Allow `templates/123` as shorthand for `/admin/templates/123`."""
    if not raw.startswith("/"):
        raw = "/" + raw
    if not raw.startswith("/admin/") and raw != "/admin":
        raw = "/admin" + raw
    return raw


def confirm(prompt: str) -> bool:
    try:
        ans = input(prompt).strip().lower()
    except (EOFError, KeyboardInterrupt):
        return False
    return ans in {"y", "yes"}


def main() -> int:
    p = argparse.ArgumentParser(
        prog="admin.py",
        description="Call Kria /admin/* endpoints with the right token + host.",
    )
    p.add_argument("method", help="HTTP method (GET, POST, PATCH, PUT, DELETE)")
    p.add_argument("path", help="Endpoint path, e.g. /admin/templates or templates/123")
    p.add_argument("--prod", action="store_true", help="Hit Fly prod instead of localhost")
    p.add_argument("--json", dest="body", help="Request body as a JSON string")
    p.add_argument(
        "--data-file",
        help="Path to a JSON file to send as the request body",
    )
    p.add_argument(
        "--query",
        action="append",
        default=[],
        help="Extra ?key=value query param (repeatable)",
    )
    p.add_argument("--yes", action="store_true", help="Skip the prod-mutation confirm prompt")
    p.add_argument("--dry-run", action="store_true", help="Print what would be sent; no network")
    p.add_argument("-v", "--verbose", action="store_true", help="Show request/response headers")
    p.add_argument(
        "--timeout",
        type=float,
        default=30.0,
        help="Request timeout in seconds (default 30)",
    )
    args = p.parse_args()

    method = args.method.upper()

    env = {**load_env(REPO_ROOT / ".env"), **os.environ}
    token_key = "ADMIN_PROD_API_KEY" if args.prod else "ADMIN_API_KEY"
    token = env.get(token_key, "").strip()
    if not token:
        hint = (
            f"Set {token_key} in {REPO_ROOT / '.env'} "
            f"({'fly secrets list -a nova-video' if args.prod else 'matches your local API'})."
        )
        print(f"error: {token_key} is not set. {hint}", file=sys.stderr)
        return 2

    base = PROD_URL if args.prod else LOCAL_URL
    path = resolve_path(args.path)
    if args.query:
        sep = "&" if "?" in path else "?"
        path = path + sep + "&".join(args.query)
    url = base + path

    body_bytes: bytes | None = None
    body_repr: str | None = None
    if args.body and args.data_file:
        print("error: pass either --json or --data-file, not both", file=sys.stderr)
        return 2
    if args.body:
        try:
            json.loads(args.body)
        except json.JSONDecodeError as exc:
            print(f"error: --json is not valid JSON: {exc}", file=sys.stderr)
            return 2
        body_bytes = args.body.encode("utf-8")
        body_repr = args.body
    elif args.data_file:
        body_bytes = Path(args.data_file).read_bytes()
        body_repr = body_bytes.decode("utf-8", errors="replace")

    if args.prod and method in MUTATING_METHODS and not args.yes and not args.dry_run:
        print(f"About to {method} {url} on PROD.")
        if body_repr:
            print(f"   body: {body_repr}")
        if not confirm("Proceed? [y/N]: "):
            print("aborted.", file=sys.stderr)
            return 130

    headers = {
        "X-Admin-Token": token,
        "Accept": "application/json",
    }
    if body_bytes is not None:
        headers["Content-Type"] = "application/json"

    if args.verbose or args.dry_run:
        print(f"→ {method} {url}", file=sys.stderr)
        for k, v in headers.items():
            shown = "***" if k == "X-Admin-Token" else v
            print(f"  {k}: {shown}", file=sys.stderr)
        if body_repr:
            print(f"  body: {body_repr}", file=sys.stderr)
    if args.dry_run:
        return 0

    req = urllib.request.Request(url, data=body_bytes, headers=headers, method=method)
    try:
        with urllib.request.urlopen(req, timeout=args.timeout) as resp:
            status = resp.status
            raw = resp.read()
            resp_headers = dict(resp.headers.items())
    except urllib.error.HTTPError as exc:
        status = exc.code
        raw = exc.read()
        resp_headers = dict(exc.headers.items()) if exc.headers else {}
    except urllib.error.URLError as exc:
        print(f"error: connection failed — {exc.reason}", file=sys.stderr)
        return 1

    if args.verbose:
        print(f"← {status}", file=sys.stderr)
        for k, v in resp_headers.items():
            print(f"  {k}: {v}", file=sys.stderr)

    text = raw.decode("utf-8", errors="replace")
    try:
        parsed = json.loads(text)
        print(json.dumps(parsed, indent=2, ensure_ascii=False))
    except json.JSONDecodeError:
        if text:
            print(text)

    return 0 if 200 <= status < 300 else 1


if __name__ == "__main__":
    sys.exit(main())
