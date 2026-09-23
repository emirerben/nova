#!/usr/bin/env python3
"""Build, audit and publish Kria's creator sound-effect library (KRI-173).

Run from ``src/apps/api``:

    python scripts/seed_sfx_library.py build                 # render + master + QA
    python scripts/seed_sfx_library.py upload                # local API
    python scripts/seed_sfx_library.py upload --prod         # Fly prod (asks y/N)
    python scripts/seed_sfx_library.py upload --prod --dry-run
    python scripts/seed_sfx_library.py retract --prod       # rollback: unpublish

``build`` renders every ``catalog.LIBRARY`` entry into ``--out`` (default
``.sfx-library/``, git-ignored) as an iPhone-safe .m4a, re-decodes each file to
check it against the contract in ``master.py``, and writes ``manifest.json``
plus a local ``index.html`` for listening. Any QA problem fails the build.

``upload`` goes through the admin API (upload-init-file → signed PUT →
upload-confirm → PATCH), the same path as the admin page. Tokens come from the
repo-root ``.env`` exactly like ``scripts/admin.py``. It is idempotent: an
effect is matched by ``source_filename`` (``<slug>.m4a``); a match with the
same SHA-256 only gets its metadata refreshed, a match with different audio is
reported and left alone unless ``--replace-audio`` archives it and uploads the
new file.
"""

from __future__ import annotations

import argparse
import hashlib
import html
import importlib.util
import json
import os
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

from scripts.sfx_library import catalog, master

API_DIR = Path(__file__).resolve().parents[1]
REPO_ROOT = API_DIR.parents[2]
DEFAULT_OUT = API_DIR / ".sfx-library"
LOCAL_URL = "http://localhost:8000"
PROD_URL = "https://nova-video.fly.dev"


# ── build ────────────────────────────────────────────────────────────────────


def build(out: Path, only: set[str] | None = None) -> list[dict]:
    out.mkdir(parents=True, exist_ok=True)
    names = [effect.name.casefold() for effect in catalog.LIBRARY]
    if len(set(names)) != len(names):
        raise SystemExit("effect names must be unique (the creator agent resolves by name)")
    entries, failures = [], []
    for rank, effect in enumerate(catalog.LIBRARY):
        if only and effect.slug not in only:
            continue
        mastered, report = master.master(
            catalog.render_raw(effect), effect.kind, tail_fade_ms=effect.tail_fade_ms
        )
        path = out / effect.filename
        qa = master.deliver(mastered, effect.kind, path)
        entry = {
            "slug": effect.slug,
            "rank": rank,
            "name": effect.name,
            "category": effect.category,
            "kind": effect.kind,
            "file": effect.filename,
            "search_terms": effect.search_terms,
            "contains_voice": effect.contains_voice,
            "license": effect.license,
            "provenance": effect.provenance,
            "sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
            "gain_db": report.gain_db,
            "peak_limited": report.peak_limited,
            "qa": qa,
        }
        entries.append(entry)
        status = "FAIL " + "; ".join(qa["problems"]) if qa["problems"] else "ok"
        print(
            f"{effect.slug:26s} {effect.kind:8s} {qa['duration_s']:5.2f}s "
            f"{qa['loudness_lufs']:6.1f} LUFS {qa['true_peak_dbtp']:5.1f} dBTP "
            f"onset {qa['leading_silence_ms']:4.1f}ms  {status}"
        )
        if qa["problems"]:
            failures.append(effect.slug)
    (out / "manifest.json").write_text(json.dumps(entries, indent=1) + "\n")
    (out / "index.html").write_text(_listening_page(entries))
    print(f"\n{len(entries)} effects → {out}")
    if failures:
        raise SystemExit(f"QA failed for: {', '.join(failures)}")
    return entries


def _listening_page(entries: list[dict]) -> str:
    rows = "\n".join(
        f"<tr><td>{html.escape(e['category'])}</td><td>{html.escape(e['name'])}</td>"
        f"<td><audio controls preload=none src='{html.escape(e['file'])}'></audio></td>"
        f"<td>{e['qa']['duration_s']}s</td><td>{html.escape(e['license'])}</td></tr>"
        for e in entries
    )
    return (
        "<!doctype html><meta charset=utf-8><title>Kria SFX library</title>"
        "<table><tr><th>Category<th>Name<th>Preview<th>Length<th>License</tr>"
        f"{rows}</table>"
    )


# ── upload ───────────────────────────────────────────────────────────────────


def _load_admin_env() -> dict[str, str]:
    spec = importlib.util.spec_from_file_location(
        "nova_admin_cli", REPO_ROOT / "scripts" / "admin.py"
    )
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return {**module.load_env(REPO_ROOT / ".env"), **os.environ}


class AdminClient:
    def __init__(self, base: str, token: str, dry_run: bool) -> None:
        self.base, self.token, self.dry_run = base, token, dry_run

    def request(self, method: str, path: str, body: dict | None = None) -> dict:
        if self.dry_run and method != "GET":
            print(f"  [dry-run] {method} {path} {json.dumps(body) if body else ''}")
            return {}
        data = json.dumps(body).encode() if body is not None else None
        req = urllib.request.Request(
            f"{self.base}/admin/sound-effects{path}",
            data=data,
            method=method,
            headers={"X-Admin-Token": self.token, "Content-Type": "application/json"},
        )
        for attempt in range(3):
            try:
                with urllib.request.urlopen(req, timeout=120) as resp:
                    raw = resp.read()
                    return json.loads(raw) if raw else {}
            except urllib.error.HTTPError as exc:
                detail = exc.read().decode("utf-8", "replace")[:400]
                if exc.code < 500 or attempt == 2:
                    raise RuntimeError(f"{method} {path} → {exc.code}: {detail}") from exc
            except urllib.error.URLError:
                if attempt == 2:
                    raise
            time.sleep(2 * (attempt + 1))
        raise AssertionError("unreachable")

    def put_file(self, url: str, path: Path, content_type: str) -> None:
        if self.dry_run:
            print(f"  [dry-run] PUT {path.name} ({path.stat().st_size} bytes)")
            return
        req = urllib.request.Request(
            url, data=path.read_bytes(), method="PUT", headers={"Content-Type": content_type}
        )
        with urllib.request.urlopen(req, timeout=120) as resp:
            if resp.status >= 300:
                raise RuntimeError(f"signed PUT failed: {resp.status}")


def _metadata(entry: dict) -> dict:
    return {
        "name": entry["name"],
        "publish": True,
        "archive": False,
        "contains_voice": entry["contains_voice"],
        "vocal_probability": 1.0 if entry["contains_voice"] else 0.0,
        "provenance": entry["provenance"],
        "license": entry["license"],
        "quality_tier": "library",
        "manual_audit_status": "approved",
        "category": entry["category"],
        "search_terms": entry["search_terms"],
        "catalog_rank": entry["rank"],
    }


def _library_tag(entry: dict) -> str:
    # Trailing space: "applause " must not match "applause-long ".
    return f"{catalog.LIBRARY_VERSION}:{entry['slug']} "


def _plan(entries: list[dict], existing: list[dict], *, replace_audio: bool) -> tuple[list, list]:
    """Pair each manifest entry with the library row it owns, if any.

    Only rows this script created are ever touched: rows tagged with the
    entry's library provenance, or an untagged row with byte-identical audio
    (a run interrupted between confirm and PATCH). A foreign row that merely
    shares ``<slug>.m4a`` (an admin page upload) is reported, never modified.
    Rows stuck pending/failed have no audio and are never matched.
    """
    ready = sorted(
        (r for r in existing if not r.get("archived_at") and r.get("status") == "ready"),
        key=lambda r: r["created_at"],
    )
    plan, notes = [], []
    for entry in entries:
        same_file = [r for r in ready if r.get("source_filename") == entry["file"]]
        tagged = [
            r for r in same_file if str(r.get("provenance") or "").startswith(_library_tag(entry))
        ]
        resumable = [
            r for r in same_file if not r.get("provenance") and r.get("sha256") == entry["sha256"]
        ]
        row = (tagged or resumable or [None])[-1]
        if len(tagged) > 1:
            notes.append(f"  ! {entry['slug']}: {len(tagged)} library rows; clean up duplicates")
        for other in same_file:
            if other is not row and other not in tagged:
                notes.append(f"  ? {entry['slug']}: row {other['id']} is not ours; left untouched")
        for other in ready:
            if (
                other is not row
                and other not in same_file
                and (str(other.get("name") or "").casefold() == entry["name"].casefold())
            ):
                notes.append(
                    f"  ? {entry['slug']}: row {other['id']} is also named {entry['name']!r};"
                    " exact-name requests can't resolve until one is renamed"
                )
        if row is None:
            plan.append(("create", entry, None))
        elif row.get("sha256") == entry["sha256"]:
            plan.append(("update", entry, row))
        else:
            plan.append(("replace" if replace_audio else "stale", entry, row))
    return plan, notes


def upload(
    out: Path,
    *,
    prod: bool,
    dry_run: bool,
    yes: bool,
    replace_audio: bool,
    api_url: str | None = None,
) -> None:
    manifest_path = out / "manifest.json"
    if not manifest_path.exists():
        raise SystemExit(f"no {manifest_path}; run `build` first")
    entries = json.loads(manifest_path.read_text())
    failed = [
        e.get("slug") for e in entries if (e.get("qa") or {"problems": ["no qa"]})["problems"]
    ]
    if failed:
        raise SystemExit(f"{manifest_path} has QA failures ({', '.join(failed)}); fix and rebuild")
    client = _client(prod=prod, dry_run=dry_run, api_url=api_url)

    plan, notes = _plan(
        entries, client.request("GET", "").get("effects", []), replace_audio=replace_audio
    )
    counts = {
        action: sum(1 for a, *_ in plan if a == action)
        for action in ("create", "update", "stale", "replace")
    }
    print(f"target {client.base}: {counts}")
    for action, entry, _row in plan:
        if action == "stale":
            print(f"  ! {entry['slug']}: live audio differs; metadata only (--replace-audio swaps)")
    for note in notes:
        print(note)
    if prod and not dry_run and not yes:
        if input("Publish to PROD? [y/N] ").strip().lower() not in {"y", "yes"}:
            raise SystemExit("aborted")

    for action, entry, row in plan:
        path = out / entry["file"]
        if action in {"update", "stale"}:
            client.request("PATCH", f"/{row['id']}", _metadata(entry))
            print(f"  = {entry['slug']} ({row['id']})")
            continue
        init = client.request(
            "POST",
            "/upload-init-file",
            {
                "filename": entry["file"],
                "name": entry["name"],
                "ext": ".m4a",
                "byte_count": path.stat().st_size,
            },
        )
        if dry_run:
            print(f"  + {entry['slug']} (dry run)")
            continue
        client.put_file(init["upload_url"], path, init["content_type"])
        confirmed = client.request("POST", f"/{init['effect_id']}/upload-confirm")
        if confirmed.get("status") != "ready":
            raise RuntimeError(f"{entry['slug']} did not confirm: {confirmed}")
        client.request("PATCH", f"/{init['effect_id']}", _metadata(entry))
        if action == "replace":
            # Only after the new row is live, so a failure never leaves a gap.
            client.request("PATCH", f"/{row['id']}", {"archive": True, "publish": False})
        print(f"  + {entry['slug']} → {init['effect_id']}")


def _client(*, prod: bool, dry_run: bool, api_url: str | None) -> AdminClient:
    token_key = "ADMIN_PROD_API_KEY" if prod else "ADMIN_API_KEY"
    token = _load_admin_env().get(token_key, "").strip()
    if not token:
        raise SystemExit(f"{token_key} is not set in {REPO_ROOT / '.env'}")
    return AdminClient(api_url or (PROD_URL if prod else LOCAL_URL), token, dry_run)


def retract(*, prod: bool, dry_run: bool, yes: bool, api_url: str | None = None) -> None:
    """Rollback: unpublish every library effect (rows and audio stay intact)."""
    client = _client(prod=prod, dry_run=dry_run, api_url=api_url)
    rows = [
        r
        for r in client.request("GET", "").get("effects", [])
        if r.get("published_at")
        and str(r.get("provenance") or "").startswith(catalog.LIBRARY_VERSION)
    ]
    print(f"target {client.base}: unpublish {len(rows)} library effects")
    if prod and not dry_run and not yes:
        if input("Unpublish from PROD? [y/N] ").strip().lower() not in {"y", "yes"}:
            raise SystemExit("aborted")
    for row in rows:
        client.request("PATCH", f"/{row['id']}", {"publish": False})
        print(f"  - {row['name']} ({row['id']})")


def main() -> None:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    sub = parser.add_subparsers(dest="command", required=True)
    build_cmd = sub.add_parser("build")
    build_cmd.add_argument("--out", type=Path, default=DEFAULT_OUT)
    build_cmd.add_argument("--only", help="comma-separated slugs")
    remote = argparse.ArgumentParser(add_help=False)
    remote.add_argument("--prod", action="store_true")
    remote.add_argument("--dry-run", action="store_true")
    remote.add_argument("--yes", action="store_true", help="skip the prod confirmation")
    remote.add_argument("--api-url", help=f"override the API base (default {LOCAL_URL})")
    upload_cmd = sub.add_parser("upload", parents=[remote])
    upload_cmd.add_argument("--out", type=Path, default=DEFAULT_OUT)
    upload_cmd.add_argument("--replace-audio", action="store_true")
    sub.add_parser("retract", parents=[remote], help="unpublish the whole library (rollback)")
    args = parser.parse_args()
    if args.command == "build":
        build(args.out, set(args.only.split(",")) if args.only else None)
    elif args.command == "retract":
        retract(prod=args.prod, dry_run=args.dry_run, yes=args.yes, api_url=args.api_url)
    else:
        upload(
            args.out,
            prod=args.prod,
            dry_run=args.dry_run,
            yes=args.yes,
            replace_audio=args.replace_audio,
            api_url=args.api_url,
        )


if __name__ == "__main__":
    sys.exit(main())
