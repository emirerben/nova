"""Export clip_metadata eval fixtures from the Redis clip-analysis cache.

The clip_metadata agent's output (best_moments) is not persisted in Postgres —
only the chosen window per JobClip is. But every successful analyze_clip call
is content-addressably cached in Redis with key prefix `clip_analysis:v1:s1:*`,
so we can reconstruct historical fixtures from there.

Output goes to tests/fixtures/agent_evals/clip_metadata/prod_snapshots/.

Run from inside a Fly machine (REDIS_URL is already injected):
    fly ssh console -a nova-video \
        -C "cd /app && python scripts/export_clip_metadata_fixtures.py --limit 6"

Or locally with REDIS_URL set:
    REDIS_URL=... python scripts/export_clip_metadata_fixtures.py --limit 6
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from datetime import UTC, datetime
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import redis as redis_lib  # noqa: E402

FIXTURES_ROOT = (
    Path(__file__).parent.parent / "tests" / "fixtures" / "agent_evals" / "clip_metadata"
)
SUBDIR = "prod_snapshots"
KEY_PATTERN = "clip_analysis:*"


def _redis() -> redis_lib.Redis:
    url = os.environ.get("REDIS_URL")
    if not url:
        sys.exit("REDIS_URL not set — run inside Fly or export it locally.")
    return redis_lib.from_url(url, socket_connect_timeout=5, socket_timeout=5)


def _meta_to_fixture(clip_hash: str, meta: dict) -> dict | None:
    """Reconstruct a fixture payload from a cached ClipMeta dict.

    Diversity gate: skip entries with no transcript AND no moments (likely a
    degraded-fallback that snuck past the analysis_degraded filter, or a
    very short clip).
    """
    moments = meta.get("best_moments") or []
    transcript = (meta.get("transcript") or "").strip()
    hook_text = (meta.get("hook_text") or "").strip()
    if not moments and not transcript:
        return None
    if not hook_text:
        return None

    output = {
        "clip_id": meta.get("clip_id") or clip_hash[:12],
        "transcript": transcript,
        "hook_text": hook_text,
        "hook_score": float(meta.get("hook_score", 5.0) or 5.0),
        "best_moments": moments,
        "detected_subject": meta.get("detected_subject", "") or "",
    }
    raw_text = json.dumps(output)
    return {
        "agent": "nova.video.clip_metadata",
        "prompt_version": "exported",
        "input": {
            "file_uri": f"clips/{clip_hash[:12]}.mp4",
            "file_mime": "video/mp4",
        },
        "raw_text": raw_text,
        "output": output,
        "meta": {
            "source": "Redis clip_analysis cache",
            "clip_hash": clip_hash,
            "moment_count": len(moments),
            "transcript_len": len(transcript),
            "exported_at": datetime.now(UTC).isoformat(),
        },
    }


def _slug_from_hash(clip_hash: str, output: dict) -> str:
    subject = (output.get("detected_subject") or "").strip().lower()
    if subject:
        subject = "".join(c if c.isalnum() else "_" for c in subject)[:32].strip("_")
    suffix = clip_hash[:8]
    return f"{subject}_{suffix}" if subject else f"clip_{suffix}"


def _bucket(meta: dict) -> str:
    """Diversity bucket for spread across short/long, with-speech/no-speech."""
    moments = meta.get("best_moments") or []
    transcript_len = len((meta.get("transcript") or "").strip())
    if not transcript_len:
        return "no_speech"
    if len(moments) >= 4:
        return "high_moment"
    if transcript_len > 200:
        return "long_speech"
    return "short_speech"


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--limit", type=int, default=6, help="Max fixtures to export.")
    parser.add_argument(
        "--per-bucket",
        type=int,
        default=2,
        help="Max fixtures per diversity bucket (no_speech / short / long / high_moment).",
    )
    parser.add_argument("--dry-run", action="store_true", help="Print without writing.")
    parser.add_argument(
        "--stdout-json",
        action="store_true",
        help="Emit fixtures as a JSON array on stdout (for piping out of fly ssh).",
    )
    parser.add_argument(
        "--out-dir",
        default=None,
        help=(
            "Override output directory "
            "(default: tests/fixtures/agent_evals/clip_metadata/prod_snapshots)."
        ),
    )
    args = parser.parse_args()

    r = _redis()
    print(f"Scanning Redis for {KEY_PATTERN} ...", file=sys.stderr)
    keys: list[bytes] = []
    for k in r.scan_iter(match=KEY_PATTERN, count=500):
        keys.append(k)
    print(f"Found {len(keys)} cached clip entries.", file=sys.stderr)

    if not keys:
        sys.exit("No cached entries — cache may have been flushed or app hasn't run recently.")

    buckets: dict[str, list[tuple[str, dict]]] = {
        "no_speech": [],
        "short_speech": [],
        "long_speech": [],
        "high_moment": [],
    }

    for raw_key in keys:
        try:
            raw_val = r.get(raw_key)
            if raw_val is None:
                continue
            meta = json.loads(raw_val)
        except Exception as exc:
            print(f"[skip] decode failed for {raw_key!r}: {exc}", file=sys.stderr)
            continue

        # Key shape: clip_analysis:v1:s1:{filter_hint_hash}:{clip_hash}
        key_str = raw_key.decode("utf-8") if isinstance(raw_key, bytes) else str(raw_key)
        parts = key_str.split(":")
        if len(parts) < 5:
            continue
        clip_hash = parts[-1]

        bucket = _bucket(meta)
        buckets[bucket].append((clip_hash, meta))

    print("Bucket counts:", {k: len(v) for k, v in buckets.items()}, file=sys.stderr)

    selected: list[tuple[str, dict]] = []
    for bucket_name, entries in buckets.items():
        take = min(args.per_bucket, len(entries), args.limit - len(selected))
        if take <= 0:
            continue
        selected.extend(entries[:take])
        if len(selected) >= args.limit:
            break

    if not selected:
        sys.exit("No usable fixtures (all entries lacked transcript+moments).")

    print(f"\nExporting {len(selected)} fixtures:", file=sys.stderr)
    fixtures: list[tuple[str, dict]] = []
    for clip_hash, meta in selected:
        fixture = _meta_to_fixture(clip_hash, meta)
        if fixture is None:
            continue
        slug = _slug_from_hash(clip_hash, fixture["output"])
        fixtures.append((slug, fixture))

    if args.stdout_json:
        json.dump(
            [{"slug": slug, "fixture": fix} for slug, fix in fixtures],
            sys.stdout,
        )
        return

    target_dir = Path(args.out_dir) if args.out_dir else (FIXTURES_ROOT / SUBDIR)
    if not args.dry_run:
        target_dir.mkdir(parents=True, exist_ok=True)

    for slug, fixture in fixtures:
        path = target_dir / f"{slug}.json"
        if args.dry_run:
            n_moments = len(fixture["output"]["best_moments"])
            print(f"[dry-run] would write {path} (moments={n_moments})")
        else:
            path.write_text(json.dumps(fixture, indent=2))
            print(f"[wrote] {path}")

    print(f"\nDone. {len(fixtures)} fixtures written to {target_dir}")


if __name__ == "__main__":
    main()
