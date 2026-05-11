"""Export agent eval fixtures from the local/prod database.

Walks DB rows and writes one JSON fixture per record under
`tests/fixtures/agent_evals/<agent>/prod_snapshots/` for:

  - template_recipe       ← VideoTemplate.recipe_cached
  - creative_direction    ← VideoTemplate.recipe_cached.creative_direction
  - transcript            ← Job.transcript
  - platform_copy         ← JobClip.platform_copy
  - audio_template        ← MusicTrack.recipe_cached

Each fixture is a self-contained replay payload:
    {
      "agent": "nova.compose.template_recipe",
      "prompt_version": "exported",
      "input": {...},
      "raw_text": "<json-serialized recipe>",
      "output": {...},
      "meta": {"template_id": "...", "template_name": "...", "exported_at": "..."}
    }

Each fixture is run through the agent + structural checks before being written.
Fixtures that fail structural validation are SKIPPED and logged — they represent
real quality issues in the source row (e.g. creative_direction strings shorter
than 50 words from templates analyzed before two-pass mode shipped). Re-run the
agent on those templates to repopulate `recipe_cached`, then re-export.

Pass `--include-failing` to write them anyway (useful for testing the eval).

Note on clip_metadata: best_moments are not persisted in JobClip / Job.all_candidates
(only the chosen window is). To produce clip_metadata fixtures, run the agent
live on a sample clip via `--eval-mode=live` and capture the recording, OR add
hand-crafted fixtures under `tests/fixtures/agent_evals/clip_metadata/golden/`.

Run:  cd src/apps/api && .venv/bin/python scripts/export_eval_fixtures.py
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
from datetime import UTC, datetime
from pathlib import Path

# Bootstrap imports when run directly
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from sqlalchemy import select  # noqa: E402

from app.database import AsyncSessionLocal  # noqa: E402
from app.models import Job, JobClip, MusicTrack, VideoTemplate  # noqa: E402

FIXTURES_ROOT = Path(__file__).parent.parent / "tests" / "fixtures" / "agent_evals"


def _slug(name: str) -> str:
    return "".join(c if c.isalnum() else "_" for c in name.lower()).strip("_") or "unnamed"


def _build_template_recipe_fixture(tpl: VideoTemplate) -> dict | None:
    recipe = tpl.recipe_cached
    if not isinstance(recipe, dict) or not recipe.get("slots"):
        return None
    raw_text = json.dumps(recipe)
    return {
        "agent": "nova.compose.template_recipe",
        "prompt_version": "exported",
        "input": {
            "file_uri": tpl.gcs_path or f"templates/{tpl.id}/reference.mp4",
            "file_mime": "video/mp4",
            "analysis_mode": "single",
            "creative_direction": "",
            "black_segments": [],
        },
        "raw_text": raw_text,
        "output": recipe,
        "meta": {
            "source": "VideoTemplate.recipe_cached",
            "template_id": tpl.id,
            "template_name": tpl.name,
            "exported_at": datetime.now(UTC).isoformat(),
        },
    }


def _build_transcript_fixture(job: Job) -> dict | None:
    transcript = job.transcript
    if not isinstance(transcript, dict) or not transcript.get("words"):
        return None
    raw_text = json.dumps(transcript)
    return {
        "agent": "nova.audio.transcript",
        "prompt_version": "exported",
        "input": {
            "file_uri": job.raw_storage_path or f"jobs/{job.id}/source.mp4",
            "file_mime": "video/mp4",
        },
        "raw_text": raw_text,
        "output": transcript,
        "meta": {
            "source": "Job.transcript",
            "job_id": str(job.id),
            "exported_at": datetime.now(UTC).isoformat(),
        },
    }


def _build_platform_copy_fixture(clip: JobClip) -> dict | None:
    """Build a platform_copy fixture from a JobClip row.

    Limitation: `transcript_excerpt` and `template_tone` are not persisted on
    JobClip — they are derived at call time from Job.transcript + the parent
    template. We export empty strings here. This is fine for replay-mode evals
    (the cassette ignores prompt content), but live-mode reruns will produce
    different output than the recorded raw_text. Shadow-mode comparisons on
    platform_copy fixtures should be interpreted with that caveat. To get a
    fully faithful live rerun, hand-author a fixture under `golden/` with
    realistic context.
    """
    pc = clip.platform_copy
    if not isinstance(pc, dict):
        return None
    if not all(k in pc for k in ("tiktok", "instagram", "youtube")):
        return None
    raw_text = json.dumps(pc)
    return {
        "agent": "nova.compose.platform_copy",
        "prompt_version": "exported",
        "input": {
            "hook_text": clip.hook_text or "",
            "transcript_excerpt": "",
            "has_transcript": True,
            "template_tone": "",
        },
        "raw_text": raw_text,
        "output": {"value": pc},
        "meta": {
            "source": "JobClip.platform_copy",
            "clip_id": str(clip.id),
            "job_id": str(clip.job_id),
            "rank": clip.rank,
            "exported_at": datetime.now(UTC).isoformat(),
        },
    }


def _build_audio_template_fixture(track: MusicTrack) -> dict | None:
    recipe = track.recipe_cached
    if not isinstance(recipe, dict) or not recipe.get("slots"):
        return None
    raw_text = json.dumps(recipe)
    cfg = track.track_config or {}
    return {
        "agent": "nova.audio.template_recipe",
        "prompt_version": "exported",
        "input": {
            "file_uri": track.audio_gcs_path or f"music/{track.id}.mp3",
            "file_mime": "audio/mp4",
            "beat_timestamps_s": list(track.beat_timestamps_s or []),
            "best_start_s": float(cfg.get("best_start_s", 0.0) or 0.0),
            "best_end_s": float(cfg.get("best_end_s", track.duration_s or 0.0) or 0.0),
            "duration_s": float(track.duration_s or 0.0),
        },
        "raw_text": raw_text,
        "output": recipe,
        "meta": {
            "source": "MusicTrack.recipe_cached",
            "track_id": track.id,
            "track_title": track.title,
            "exported_at": datetime.now(UTC).isoformat(),
        },
    }


def _build_creative_direction_fixture(tpl: VideoTemplate) -> dict | None:
    recipe = tpl.recipe_cached
    if not isinstance(recipe, dict):
        return None
    text = (recipe.get("creative_direction") or "").strip()
    if not text:
        return None
    return {
        "agent": "nova.compose.creative_direction",
        "prompt_version": "exported",
        "input": {
            "file_uri": tpl.gcs_path or f"templates/{tpl.id}/reference.mp4",
            "file_mime": "video/mp4",
        },
        "raw_text": text,
        "output": {"text": text},
        "meta": {
            "source": "VideoTemplate.recipe_cached.creative_direction",
            "template_id": tpl.id,
            "template_name": tpl.name,
            "exported_at": datetime.now(UTC).isoformat(),
        },
    }


def _write(path: Path, payload: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, indent=2, default=str))


def _validate_payload(payload: dict) -> list[str]:
    """Run the same structural checks the eval suite would. Empty list = pass."""
    # Local imports so this script can run without the eval test dependencies
    # in environments where tests/ isn't on the path.
    from tests.evals.runners.eval_runner import (  # noqa: PLC0415
        CassetteModelClient,
        Fixture,
        run_eval,
    )

    fixture = Fixture(
        path=Path("/dev/null"),
        agent=payload["agent"],
        prompt_version=payload.get("prompt_version", ""),
        input=payload["input"],
        raw_text=payload["raw_text"],
        output=payload.get("output", {}),
        meta=payload.get("meta", {}),
    )
    result = run_eval(fixture, model_client=CassetteModelClient(payload["raw_text"]))
    if result.error:
        return [result.error]
    return result.structural_failures


async def export_all(
    *, only: str | None = None, dry_run: bool = False, include_failing: bool = False
) -> dict[str, int]:
    counts = {
        "template_recipe": 0,
        "creative_direction": 0,
        "transcript": 0,
        "platform_copy": 0,
        "audio_template": 0,
        "skipped": 0,
        "rejected": 0,
    }

    def _emit(agent_dir: str, slug: str, payload: dict) -> None:
        failures = [] if include_failing else _validate_payload(payload)
        if failures:
            print(f"[reject] {agent_dir}/{slug}: {failures[0]}")
            counts["rejected"] += 1
            return
        target = FIXTURES_ROOT / agent_dir / "prod_snapshots" / f"{slug}.json"
        if dry_run:
            print(f"[dry-run] would write {target}")
        else:
            _write(target, payload)
        counts[agent_dir] += 1

    async with AsyncSessionLocal() as session:
        templates = (
            (
                await session.execute(
                    select(VideoTemplate).where(VideoTemplate.analysis_status == "ready")
                )
            )
            .scalars()
            .all()
        )

        jobs: list[Job] = []
        clips: list[JobClip] = []
        tracks: list[MusicTrack] = []
        if only in (None, "transcript"):
            jobs = (
                (await session.execute(select(Job).where(Job.transcript.isnot(None)).limit(50)))
                .scalars()
                .all()
            )
        if only in (None, "platform_copy"):
            clips = (
                (
                    await session.execute(
                        select(JobClip).where(JobClip.platform_copy.isnot(None)).limit(50)
                    )
                )
                .scalars()
                .all()
            )
        if only in (None, "audio_template"):
            tracks = (
                (
                    await session.execute(
                        select(MusicTrack).where(
                            MusicTrack.analysis_status == "ready",
                            MusicTrack.recipe_cached.isnot(None),
                        )
                    )
                )
                .scalars()
                .all()
            )

    for tpl in templates:
        slug = _slug(tpl.name)

        if only in (None, "template_recipe"):
            payload = _build_template_recipe_fixture(tpl)
            if payload is None:
                counts["skipped"] += 1
            else:
                _emit("template_recipe", slug, payload)

        if only in (None, "creative_direction"):
            payload = _build_creative_direction_fixture(tpl)
            if payload is not None:
                _emit("creative_direction", slug, payload)

    for job in jobs:
        payload = _build_transcript_fixture(job)
        if payload is None:
            counts["skipped"] += 1
            continue
        _emit("transcript", f"job_{job.id}", payload)

    for clip in clips:
        payload = _build_platform_copy_fixture(clip)
        if payload is None:
            counts["skipped"] += 1
            continue
        _emit("platform_copy", f"clip_{clip.id}", payload)

    for track in tracks:
        payload = _build_audio_template_fixture(track)
        if payload is None:
            counts["skipped"] += 1
            continue
        _emit("audio_template", _slug(track.title or track.id), payload)

    return counts


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--only",
        choices=[
            "template_recipe",
            "creative_direction",
            "transcript",
            "platform_copy",
            "audio_template",
        ],
        default=None,
        help="Export only one agent's fixtures (default: all).",
    )
    parser.add_argument("--dry-run", action="store_true", help="Print targets without writing.")
    parser.add_argument(
        "--include-failing",
        action="store_true",
        help="Write fixtures even if they fail structural validation (debug only).",
    )
    args = parser.parse_args()

    db_url = os.environ.get("DATABASE_URL", "<not set>")
    print(f"Reading from DATABASE_URL host: {db_url.split('@')[-1].split('/')[0]}")

    counts = asyncio.run(
        export_all(
            only=args.only,
            dry_run=args.dry_run,
            include_failing=args.include_failing,
        )
    )
    print(
        f"\nExported: {counts['template_recipe']} template_recipe, "
        f"{counts['creative_direction']} creative_direction, "
        f"{counts['transcript']} transcript, "
        f"{counts['platform_copy']} platform_copy, "
        f"{counts['audio_template']} audio_template "
        f"(skipped {counts['skipped']} with missing data, "
        f"rejected {counts['rejected']} that failed structural validation)"
    )
    print(f"Output: {FIXTURES_ROOT}")


if __name__ == "__main__":
    main()
