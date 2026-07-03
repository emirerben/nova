"""Light single-pass footage summary for the "Get a transcript" flow.

The narrated render deliberately SKIPS clip analysis (generative_build.py:706:
`skip_analysis` — "never reads clip metadata, so analysis is wasted work there"),
so this is a transcript-flow-only, minimal grounding pass — NOT the full per-clip
`ClipMetadata` on every clip. It analyzes at most `_MAX_CLIPS` clips and composes a
one-paragraph "what the footage shows" the script writer can lean on.

Best-effort by contract: no GEMINI_API_KEY (the local dev machine), no clips, or ANY
failure → returns None. The caller then writes the script from the brief alone, so
the flow always completes at localhost without keys.
"""

from __future__ import annotations

import os
import tempfile

import structlog

from app.config import settings

log = structlog.get_logger()

# Keep it light — a couple of clips is enough grounding for a voiceover script,
# and analysing every clip would re-introduce the per-clip Gemini pass the narrated
# render was built to skip.
_MAX_CLIPS = 2


def summarize_footage(clip_gcs_paths: list[str], job_id: str | None = None) -> str | None:
    """Return a one-paragraph footage summary, or None when unavailable.

    None whenever Gemini is not configured, there are no clips, or anything fails —
    the caller treats None as "write from the brief only".
    """
    if not settings.gemini_api_key:
        return None
    paths = [p for p in (clip_gcs_paths or []) if isinstance(p, str) and p.strip()]
    if not paths:
        return None

    try:
        from app.pipeline.agents.gemini_analyzer import (  # noqa: PLC0415
            analyze_clip,
            gemini_upload_and_wait,
        )
        from app.storage import download_to_file  # noqa: PLC0415
    except Exception as exc:  # noqa: BLE001
        log.warning("footage_summary.import_failed", error=str(exc)[:200])
        return None

    fragments: list[str] = []
    for idx, gcs_path in enumerate(paths[:_MAX_CLIPS]):
        try:
            with tempfile.TemporaryDirectory() as tmpdir:
                local_path = os.path.join(tmpdir, f"clip{idx}.mp4")
                download_to_file(gcs_path, local_path)
                file_ref = gemini_upload_and_wait(local_path)
                meta = analyze_clip(file_ref, job_id=job_id)
            if getattr(meta, "failed", False) or getattr(meta, "analysis_degraded", False):
                continue
            subject = (
                getattr(meta, "detected_subject", None) or getattr(meta, "subject", "") or ""
            ).strip()
            desc = (getattr(meta, "description", "") or "").strip()
            bit = subject
            if desc and desc.lower() != subject.lower():
                bit = f"{subject} — {desc}" if subject else desc
            if bit:
                fragments.append(bit)
        except Exception as exc:  # noqa: BLE001
            log.warning(
                "footage_summary.clip_failed", gcs_path=gcs_path[:120], error=str(exc)[:200]
            )
            continue

    if not fragments:
        return None
    return " ".join(f"Clip {i + 1}: {frag}." for i, frag in enumerate(fragments))
