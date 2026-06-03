"""One-shot backfill: re-extract published tracks whose lyrics_cached blob
predates the current `LyricsExtractionAgent.prompt_version`.

Why this exists
---------------
The lyrics alignment pipeline caches its full output (per-line + per-word
timings) on `MusicTrack.lyrics_cached`. When the alignment algorithm
changes (e.g., the 2026-05-28 multi-line median re-anchor for Overnight
/ The Bay), we bump `LyricsExtractionAgent.spec.prompt_version`. Cached
blobs from the prior version will only be regenerated when a user
submits a job against that track — until then, the admin Test tab and
the music gallery surface stale alignment data.

This script accelerates that invalidation. It iterates published
non-archived tracks where `lyrics_cached.prompt_version` doesn't match
the current code's `LyricsExtractionAgent.spec.prompt_version` and POSTs
`/admin/music-tracks/{id}/extract-lyrics` for each. Sleeps between calls to
avoid stampeding the worker.

Usage
-----
    # Dry-run (default) — print planned actions, do not POST
    python3 scripts/backfill_lyrics.py

    # Actually fire the lyric extraction calls (against prod):
    python3 scripts/backfill_lyrics.py --prod --execute

    # Override the inter-call sleep (default 2.0s):
    python3 scripts/backfill_lyrics.py --prod --execute --sleep 1.0

Auth
----
Reads ADMIN_API_KEY (local) or ADMIN_PROD_API_KEY (prod) from .env.
Same mechanism as scripts/admin.py — no secrets on the command line.

Stdlib-only (no requests/httpx). Runs with any Python 3 outside the
API venv. Exits 0 on success, 1 on any per-track failure.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
LOCAL_BASE = "http://localhost:8000"
PROD_BASE = "https://nova-video.fly.dev"


def load_env(env_path: Path) -> dict[str, str]:
    """Tiny .env parser — same shape as scripts/admin.py."""
    if not env_path.exists():
        return {}
    out: dict[str, str] = {}
    for raw_line in env_path.read_text().splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        key = key.strip()
        value = value.strip().strip('"').strip("'")
        out[key] = value
    return out


def http_request(
    *,
    method: str,
    url: str,
    token: str,
    body: dict | None = None,
    timeout_s: float = 30.0,
) -> tuple[int, dict | str]:
    """Stdlib HTTP request returning (status, parsed_body_or_text)."""
    data = json.dumps(body).encode("utf-8") if body is not None else None
    req = urllib.request.Request(url, data=data, method=method)
    req.add_header("X-Admin-Token", token)
    if data is not None:
        req.add_header("Content-Type", "application/json")
    try:
        with urllib.request.urlopen(req, timeout=timeout_s) as resp:
            raw = resp.read().decode("utf-8")
            status = resp.status
    except urllib.error.HTTPError as exc:
        raw = exc.read().decode("utf-8") if exc.fp else ""
        status = exc.code
    try:
        return status, json.loads(raw) if raw else {}
    except json.JSONDecodeError:
        return status, raw


def get_current_prompt_version() -> str | None:
    """Read `LyricsExtractionAgent.spec.prompt_version` from app code.

    Imports the agent if the app package is importable in the current env;
    otherwise greps the source file directly so the script still works
    from outside the API venv.
    """
    try:
        sys.path.insert(0, str(REPO_ROOT / "src" / "apps" / "api"))
        from app.agents.lyrics import LyricsExtractionAgent  # noqa: PLC0415

        return LyricsExtractionAgent.spec.prompt_version
    except (ImportError, AttributeError):
        # Fallback: grep the source. Brittle but works from any Python env.
        source = REPO_ROOT / "src" / "apps" / "api" / "app" / "agents" / "lyrics.py"
        if not source.exists():
            return None
        for line in source.read_text().splitlines():
            stripped = line.strip()
            if stripped.startswith("prompt_version="):
                value = stripped.split("=", 1)[1].strip().rstrip(",")
                return value.strip('"').strip("'")
        return None


def _extract_track_rows(body: dict | list) -> tuple[list[dict], int | None]:
    """Normalize current and legacy admin list response shapes."""
    if isinstance(body, dict) and isinstance(body.get("tracks"), list):
        total = body.get("total")
        return list(body["tracks"]), total if isinstance(total, int) else None
    if isinstance(body, list):
        return list(body), len(body)
    raise ValueError(f"unexpected /admin/music-tracks response shape: {type(body).__name__}")


def list_music_track_rows(base: str, token: str, *, limit: int = 100) -> list[dict]:
    """Fetch every admin music-track list row, following offset pagination."""
    rows: list[dict] = []
    offset = 0
    while True:
        status_code, body = http_request(
            method="GET",
            url=f"{base}/admin/music-tracks?limit={limit}&offset={offset}",
            token=token,
        )
        if status_code != 200 or not isinstance(body, dict | list):
            raise RuntimeError(f"GET /admin/music-tracks → {status_code}: {body!r}")
        page_rows, total = _extract_track_rows(body)
        rows.extend(page_rows)
        if not page_rows:
            break
        if total is not None and len(rows) >= total:
            break
        if len(page_rows) < limit:
            break
        offset += len(page_rows)
    return rows


def find_stale_tracks(base: str, token: str, target: str) -> tuple[list[tuple[str, str, str | None]], int]:
    """Return published ready tracks whose detail lyrics cache is stale."""
    list_rows = list_music_track_rows(base, token)
    stale: list[tuple[str, str, str | None]] = []
    for row in list_rows:
        if row.get("analysis_status") != "ready":
            continue
        if row.get("archived_at"):
            continue
        if not row.get("published_at"):
            # Stale lyrics on unpublished tracks don't affect users.
            continue

        track_id = row.get("id")
        if not isinstance(track_id, str) or not track_id:
            continue
        status_code, detail = http_request(
            method="GET",
            url=f"{base}/admin/music-tracks/{track_id}",
            token=token,
        )
        if status_code != 200 or not isinstance(detail, dict):
            raise RuntimeError(
                f"GET /admin/music-tracks/{track_id} → {status_code}: {detail!r}"
            )

        blob = detail.get("lyrics_cached") or {}
        blob_version = blob.get("prompt_version") if isinstance(blob, dict) else None
        if blob_version != target:
            title = detail.get("title") or row.get("title") or "<no title>"
            stale.append((track_id, title, blob_version))

    return stale, len(list_rows)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--prod",
        action="store_true",
        help="Hit prod API (default: local localhost:8000).",
    )
    parser.add_argument(
        "--execute",
        action="store_true",
        help="Actually POST lyric extraction (default: dry-run, print only).",
    )
    parser.add_argument(
        "--sleep",
        type=float,
        default=2.0,
        help="Seconds to sleep between extraction calls (default: 2.0).",
    )
    parser.add_argument(
        "--target-version",
        type=str,
        default=None,
        help="Override target prompt_version (default: read from code).",
    )
    args = parser.parse_args()

    env = {**load_env(REPO_ROOT / ".env"), **os.environ}
    token_key = "ADMIN_PROD_API_KEY" if args.prod else "ADMIN_API_KEY"
    token = env.get(token_key)
    if not token:
        print(
            f"error: {token_key} is not set. Set it in .env "
            f"({'fly secrets list -a nova-video' if args.prod else 'local API config'}).",
            file=sys.stderr,
        )
        return 1

    base = PROD_BASE if args.prod else LOCAL_BASE
    target = args.target_version or get_current_prompt_version()
    if not target:
        print(
            "error: could not determine target prompt_version. "
            "Pass --target-version=<value> explicitly.",
            file=sys.stderr,
        )
        return 1

    print(f"Target prompt_version: {target}")
    print(f"Base URL: {base}")
    print(f"Mode: {'EXECUTE' if args.execute else 'DRY-RUN'}")
    print()

    try:
        stale, total_rows = find_stale_tracks(base, token, target)
    except RuntimeError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1

    print(f"Found {len(stale)} stale published tracks out of {total_rows} total.")
    if not stale:
        return 0

    # The /extract-lyrics endpoint reuses existing beat/section analysis and
    # refreshes only the LRCLIB + Whisper cache. Sleep math is just inter-call
    # pacing; the worker jobs themselves run asynchronously.
    if args.execute:
        est_inter_call_s = args.sleep * len(stale)
        print()
        print(
            f"WARNING: --execute will trigger lyric extraction on {len(stale)} tracks "
            f"(LRCLIB + Whisper, no beat/section reanalysis)."
        )
        print(
            f"  Estimated inter-call delay: {est_inter_call_s:.0f}s "
            f"(worker extraction runs asynchronously)."
        )
        print()

    failed: list[str] = []
    for i, (track_id, title, blob_version) in enumerate(stale, 1):
        prefix = f"[{i}/{len(stale)}]"
        action = "POST" if args.execute else "DRY-RUN"
        print(f"{prefix} {action} extract-lyrics {track_id} '{title}' (was: {blob_version!r})")
        if not args.execute:
            continue

        status_code, body = http_request(
            method="POST",
            url=f"{base}/admin/music-tracks/{track_id}/extract-lyrics",
            token=token,
        )
        if status_code != 200:
            print(f"  FAILED: {status_code}: {body!r}", file=sys.stderr)
            failed.append(track_id)
        else:
            print(f"  ok: queued (analysis_status={body.get('analysis_status')})")

        if i < len(stale):
            time.sleep(args.sleep)

    print()
    if failed:
        print(f"DONE with {len(failed)} failures: {failed}", file=sys.stderr)
        return 1
    print(f"DONE: {len(stale)} tracks queued for lyric extraction.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
