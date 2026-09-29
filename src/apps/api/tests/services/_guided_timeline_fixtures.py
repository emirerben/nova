"""Shared guided-story fixtures for the KRI-219 Lane B tests (no DB, no network)."""

from __future__ import annotations

import types
import uuid
from typing import Any

from app.schemas.guided_edit_revision import normalize_guided_editor_revision

SEGMENT_S = 2.0


def guided_revision(
    job_id: str,
    *,
    count: int = 4,
    duration_s: float = SEGMENT_S,
    source_duration_s: float = 10.0,
    transitions: dict[int, tuple[str, float]] | None = None,
    extra_segment: dict[int, dict[str, Any]] | None = None,
    text_elements: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    prefix = f"generative-jobs/{job_id}/sources/"
    segments = []
    for index in range(count):
        row: dict[str, Any] = {
            "segment_id": f"s{index + 1}",
            "media_id": f"m{index}",
            "source_start_s": 0.0,
            "source_end_s": duration_s,
            "duration_s": duration_s,
        }
        if transitions and index in transitions:
            row["transition_after"], row["transition_duration_s"] = transitions[index]
        row.update((extra_segment or {}).get(index, {}))
        segments.append(row)
    return normalize_guided_editor_revision(
        {
            "approval_proposal_version": 1,
            "approval_media_digest": "a" * 64,
            "revision_number": 1,
            "base_generation": "gen-1",
            "sources": [
                {
                    "media_id": f"m{index}",
                    "gcs_path": f"{prefix}clip_{index}.mp4",
                    "generation": "1",
                    "kind": "video",
                    "duration_s": source_duration_s,
                }
                for index in range(count)
            ],
            "segments": segments,
            **({"text_elements": text_elements} if text_elements else {}),
        }
    )


def label_bar(media_id: str, segment_id: str, start_s: float, end_s: float, text: str) -> dict:
    return {
        "id": f"clip-label-media-{media_id}",
        "text": text,
        "start_s": start_s,
        "end_s": end_s,
        "segment_id": segment_id,
        "role": "generative_intro",
        "position": "custom",
        "x_frac": 0.5,
        "y_frac": 0.78,
        "size_px": 58,
        "font_family": "DM Sans",
        "color": "#FFF8F0",
        "effect": "static",
        "alignment": "center",
    }


def guided_bars(revision: dict[str, Any]) -> list[dict]:
    """Title + one label per segment + a caption bar + a whole-video bar + a tail bar."""
    total = max(float(s["output_end_s"]) for s in revision["segments"])
    bars = [
        {
            "id": "guided-title",
            "text": "Weekend in Lisbon",
            "start_s": 0.0,
            "end_s": total,
            "role": "generative_intro",
            "position": "custom",
            "y_frac": 0.16,
            "font_family": "DM Sans",
            "size_px": 76,
            "color": "#FFF8F0",
            "effect": "static",
            "alignment": "center",
        }
    ]
    for segment in revision["segments"]:
        bars.append(
            label_bar(
                segment["media_id"],
                segment["segment_id"],
                segment["output_start_s"],
                segment["output_end_s"],
                f"Label {segment['media_id']}",
            )
        )
    bars.append(
        {
            "id": "caption-1",
            "text": "spoken line",
            "start_s": 0.4,
            "end_s": 1.6,
            "role": "generative_intro",
            "position": "bottom",
            "source_params": {"source": "caption_cue"},
        }
    )
    return bars


def guided_job(revision: dict[str, Any], bars: list[dict], job_id: str | None = None):
    jid = job_id or str(uuid.uuid4())
    variant = {
        "variant_id": "song_text",
        "resolved_archetype": "guided_story",
        "render_status": "ready",
        "render_generation_id": "gen-1",
        "render_finished_at": "2026-07-01T00:00:00Z",
        "text_elements": bars,
        "guided_edit_revision": revision,
    }
    job = types.SimpleNamespace(
        id=uuid.UUID(jid),
        assembly_plan={"variants": [variant], "guided_edit": {}, "guided_story_execution_plan": {}},
        all_candidates={"clip_paths": []},
        status="variants_ready",
        mode="content_plan",
    )
    return job, variant


def arm_guided(monkeypatch, revision: dict[str, Any], *, caps: dict | None = None) -> None:
    """Make the fake job read as a guided-native variant with an editable timeline."""
    import app.routes.generative_jobs as gj
    import app.services.kria_editor_ops as ops
    from app.config import settings

    monkeypatch.setattr(settings, "edit_transitions_enabled", True, raising=False)
    monkeypatch.setattr(settings, "guided_story_editor_v2_enabled", True, raising=False)
    monkeypatch.setattr(gj, "_guided_v2_revision", lambda *_a: revision)
    monkeypatch.setattr(ops, "_guided_v2_revision", lambda *_a: revision)
    capabilities = caps or {
        "text_elements": True,
        "timeline": True,
        "clips": {"transitions": {"editable": True}},
    }
    monkeypatch.setattr(ops, "_editor_capabilities", lambda _j, _v: capabilities)
