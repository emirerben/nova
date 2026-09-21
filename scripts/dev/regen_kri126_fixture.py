#!/usr/bin/env python3
"""Regenerate the shared clip-understanding records in the KRI-126 fixture (KRI-127).

Read-production / write-local tool. It issues ONE GET to the production admin
API (plan-item debug), downloads the 30 source clips read-only, runs the CURRENT
video analyzer on each (about 30 Gemini calls, well under $1) and writes only the
privacy-safe ``understanding`` block into
``src/apps/api/tests/fixtures/kri126_thirty_clip_guided_story.json``.

Privacy: spoken transcripts are never written (word-count placeholder; the
``has_speech`` / ``to_camera`` flags are kept) and brand strings are scrubbed.
No GCS path, user id or media id leaves this machine. Review the diff before
committing.

Usage (from the repo root, with the API venv):
    set -a && source .env && set +a
    src/apps/api/.venv/bin/python scripts/dev/regen_kri126_fixture.py --allow-prod-read
"""

from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
import tempfile
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
API_ROOT = REPO_ROOT / "src" / "apps" / "api"
if str(API_ROOT) not in sys.path:
    sys.path.insert(0, str(API_ROOT))

ITEM_ID = "5e7a1c97-0c04-4fb5-99c4-306072c2a79d"
FIXTURE = API_ROOT / "tests" / "fixtures" / "kri126_thirty_clip_guided_story.json"
DURATION_TOLERANCE_S = 0.05


def _prod_clip_rows() -> list[dict]:
    out = subprocess.run(
        [
            sys.executable,
            str(REPO_ROOT / "scripts" / "admin.py"),
            "--prod",
            "GET",
            f"/admin/plan-items/{ITEM_ID}/debug",
        ],
        check=True,
        capture_output=True,
        text=True,
    ).stdout
    return list(json.loads(out)["clip_assignments"])


def _scrub(value: object, brands: list[str]) -> object:
    if isinstance(value, str):
        for brand in brands:
            value = re.sub(re.escape(brand), "[brand]", value, flags=re.IGNORECASE)
        return value
    if isinstance(value, list):
        return [_scrub(v, brands) for v in value]
    if isinstance(value, dict):
        return {k: _scrub(v, brands) for k, v in value.items()}
    return value


def _redact(understanding: dict, brands: list[str]) -> dict:
    """Drop verbatim speech and brand strings (logos/jersey names can identify people).

    ``brands`` is the union across ALL clips: the analyzer may name a logo in one
    clip's summary while only listing it under ``brands`` for another clip.
    """
    speech = dict(understanding.get("speech") or {})
    words = len(str(speech.get("transcript") or "").split())
    speech["transcript"] = f"(redacted spoken transcript, {words} words)" if words else ""
    scrubbed = _scrub({**understanding, "speech": speech}, brands)
    return {**scrubbed, "brands": []}


def _analyze(index: int, row: dict, workdir: Path) -> dict:
    from app import storage  # noqa: PLC0415
    from app.schemas.clip_understanding import UNDERSTANDING_KEY  # noqa: PLC0415
    from app.tasks.autoplace import _analyze_video  # noqa: PLC0415

    local = workdir / f"clip-{index + 1:02d}.mp4"
    storage.download_to_file(row["gcs_path"], str(local))
    analysis, _aspect, _duration, _dims = _analyze_video(
        str(local), job_scope=f"kri127-fixture-{index + 1:02d}"
    )
    block = (analysis or {}).get(UNDERSTANDING_KEY)
    if not isinstance(block, dict) or not block:
        raise RuntimeError(f"clip-{index + 1:02d}: analyzer returned no understanding block")
    return block


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--allow-prod-read", action="store_true", required=True)
    parser.add_argument("--workers", type=int, default=4)
    args = parser.parse_args()

    from app.schemas.clip_understanding import UNDERSTANDING_KEY  # noqa: PLC0415

    fixture = json.loads(FIXTURE.read_text())
    media = fixture["media"]
    rows = _prod_clip_rows()
    if len(rows) != len(media):
        raise SystemExit(f"prod has {len(rows)} clips, fixture has {len(media)}")
    for i, (row, entry) in enumerate(zip(rows, media, strict=True)):
        drift = abs(float(row.get("duration_s") or 0) - float(entry["duration_s"]))
        if drift > DURATION_TOLERANCE_S:
            raise SystemExit(f"clip-{i + 1:02d}: duration mismatch, refusing to guess the mapping")

    with tempfile.TemporaryDirectory(prefix="kri127-fixture-") as tmp:
        workdir = Path(tmp)
        with ThreadPoolExecutor(max_workers=max(1, args.workers)) as pool:
            jobs = [pool.submit(_analyze, i, row, workdir) for i, row in enumerate(rows)]
            blocks = [job.result() for job in jobs]

    # Longest first so a full name is scrubbed before any shorter name inside it.
    brands = sorted(
        {b for block in blocks for b in block.get("brands") or [] if b}, key=len, reverse=True
    )
    for entry, block in zip(media, blocks, strict=True):
        entry["analysis"][UNDERSTANDING_KEY] = _redact(block, brands)
    FIXTURE.write_text(json.dumps(fixture, indent=2, ensure_ascii=False) + "\n")
    print(f"wrote {len(blocks)} understanding records to {FIXTURE.relative_to(REPO_ROOT)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
