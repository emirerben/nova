from __future__ import annotations

import types
import uuid

import pytest

from app.agents._schemas.text_element import (
    append_ai_text_tombstones,
    merge_projected_text_elements_for_variant,
    text_element_source_identity,
    text_elements_for_variant,
)
from app.pipeline.generative_overlays import build_overlays_from_text_elements
from app.routes.generative_jobs import (
    _HEIF_PREVIEW_BACKFILL_ATTEMPTED,
    EditorCommitRequest,
    _lazy_backfill_media_overlay_previews,
    _variants_for_response,
    enqueue_editor_commit_render,
    prepare_editor_commit,
    validate_text_elements_payload,
)


def test_linear_intro_projects_one_grouped_bar_with_burn_style() -> None:
    variant = {
        "variant_id": "original_text",
        "text_mode": "agent_text",
        "intro_text": "This changed my life",
        "intro_layout": "linear",
        "intro_effect": "karaoke-line",
        "intro_text_color": "#F8FAFC",
        "intro_text_size_px": 72,
        "intro_start_s": 1.0,
        "intro_end_s": 6.0,
    }

    [intro] = text_elements_for_variant(variant)

    assert intro.text == "This changed my life"
    assert intro.start_s == pytest.approx(1.0)
    assert intro.end_s == pytest.approx(6.0)
    assert intro.reveal_s == pytest.approx(3.0)
    assert intro.effect == "karaoke-line"
    assert intro.color == "#F8FAFC"
    assert intro.highlight_color == "#FFD24A"
    assert intro.size_px == pytest.approx(72)
    assert text_element_source_identity(intro) == "generative_intro:intro:intro"
    assert intro.source_params["burn_dicts"]


def test_sequence_projects_each_rendered_block_with_exact_timing_and_style(monkeypatch) -> None:
    import app.pipeline.intro_cluster as ic

    def _two_blocks(text, *, base_size_px, reveal_window_s, **kwargs):
        first, *rest = text.split()
        return [
            {
                "text": first,
                "text_size_px": base_size_px,
                "font_family": "PlayfairDisplay-Bold",
                "position_x_frac": 0.4,
                "position_y_frac": 0.44,
                "start_offset_s": 0.0,
                "reveal_s": reveal_window_s,
            },
            {
                "text": " ".join(rest),
                "text_size_px": base_size_px - 8,
                "font_family": "Playfair Display Italic",
                "position_x_frac": 0.62,
                "position_y_frac": 0.56,
                "start_offset_s": 0.0,
                "reveal_s": reveal_window_s,
                "glow_color": "#7CFF8A",
                "glow_strength": 0.8,
            },
        ]

    monkeypatch.setattr(ic, "compute_cluster_blocks", _two_blocks)
    variant = {
        "variant_id": "original_text",
        "intro_mode": "sequence",
        "text_mode": "agent_text",
        "sequence_base_size_px": 66,
        "intro_text_color": "#E0F2FE",
        "scenes": [
            {"words": ["open", "here"], "start_s": 0.2, "end_s": 2.0, "fade_out": False},
            {"words": ["land", "there"], "start_s": 2.0, "end_s": 4.5, "fade_out": True},
        ],
    }

    elements = text_elements_for_variant(variant)

    assert [e.text for e in elements] == ["open", "here", "land", "there"]
    assert [e.start_s for e in elements] == [
        pytest.approx(0.2),
        pytest.approx(1.0),
        pytest.approx(2.0),
        pytest.approx(2.8),
    ]
    assert [e.end_s for e in elements] == [
        pytest.approx(2.0),
        pytest.approx(2.0),
        pytest.approx(4.5),
        pytest.approx(4.5),
    ]
    assert all(e.role == "generative_sequence" for e in elements)
    assert elements[0].font_family == "PlayfairDisplay-Bold"
    assert elements[0].size_px == pytest.approx(66)
    assert elements[1].font_family == "Playfair Display Italic"
    assert elements[1].size_px == pytest.approx(58)
    assert elements[1].x_frac == pytest.approx(0.62)
    assert elements[1].y_frac is not None
    assert elements[1].glow_color == "#7CFF8A"
    assert elements[1].glow_strength == pytest.approx(0.8)
    assert text_element_source_identity(elements[0]) == "generative_sequence:sequence_scene:0:0"
    assert text_element_source_identity(elements[1]) == "generative_sequence:sequence_scene:0:1"
    assert elements[2].fade_out_ms == 350
    assert elements[3].fade_out_ms == 350

    compiled = build_overlays_from_text_elements(elements, video_duration_s=4.5)
    compiled_glow = [overlay for overlay in compiled if overlay["text"] in {"here", "there"}]
    assert compiled_glow
    assert all(overlay["glow_color"] == "#7CFF8A" for overlay in compiled_glow)
    assert all(overlay["glow_strength"] == pytest.approx(0.8) for overlay in compiled_glow)


def test_legacy_saved_sequence_scene_suppresses_new_block_projections(monkeypatch) -> None:
    import app.pipeline.intro_cluster as ic

    monkeypatch.setattr(
        ic,
        "compute_cluster_blocks",
        lambda text, **kwargs: [
            {
                "text": "first",
                "text_size_px": 70,
                "font_family": "PlayfairDisplay-Bold",
                "position_x_frac": 0.4,
                "position_y_frac": 0.44,
            },
            {
                "text": "second",
                "text_size_px": 54,
                "font_family": "PlayfairDisplay-Regular",
                "position_x_frac": 0.6,
                "position_y_frac": 0.56,
            },
        ],
    )
    variant = {
        "intro_mode": "sequence",
        "text_mode": "agent_text",
        "scenes": [{"words": ["first", "second"], "start_s": 0.0, "end_s": 2.0}],
        "text_elements_user_edited": True,
        "text_elements": [
            {
                "id": "legacy-scene",
                "text": "My saved rewrite",
                "start_s": 0.0,
                "end_s": 2.0,
                "role": "generative_sequence",
                "source_params": {
                    "source": "sequence_scene",
                    "key": "0",
                    "identity": "sequence_scene:0",
                },
            }
        ],
    }

    merged = merge_projected_text_elements_for_variant(variant)

    assert merged is not None
    assert [element["text"] for element in merged] == ["My saved rewrite"]


def test_caption_cues_project_one_bar_per_cue() -> None:
    variant = {
        "variant_id": "narrated",
        "text_mode": "agent_text",
        "caption_text_color": "#FFFFFF",
        "caption_size_px": 54,
        "caption_stroke_width": 5,
        "caption_cues": [
            {"text": "first line", "start_s": 0.0, "end_s": 1.2},
            {"text": "second line", "start_s": 1.2, "end_s": 2.4},
        ],
    }

    elements = text_elements_for_variant(variant)

    assert [e.text for e in elements] == ["first line", "second line"]
    assert [e.start_s for e in elements] == [pytest.approx(0.0), pytest.approx(1.2)]
    assert [e.end_s for e in elements] == [pytest.approx(1.2), pytest.approx(2.4)]
    assert all(e.position == "bottom" for e in elements)
    assert elements[0].size_px == pytest.approx(54)
    assert elements[0].stroke_width == pytest.approx(5)
    assert text_element_source_identity(elements[0]) == "generative_sequence:caption_cue:0"


def test_merge_appends_unrepresented_ai_intro_to_legacy_user_elements() -> None:
    variant = {
        "text_mode": "agent_text",
        "intro_text": "AI burned intro",
        "text_elements_user_edited": True,
        "text_elements": [
            {
                "id": "user-1",
                "text": "User added label",
                "start_s": 4.0,
                "end_s": 5.0,
                "role": "generative_intro",
            }
        ],
    }

    merged = merge_projected_text_elements_for_variant(variant)

    assert merged is not None
    assert [e["text"] for e in merged] == ["User added label", "AI burned intro"]
    assert text_element_source_identity(merged[1]) == "generative_intro:intro:intro"


def test_merge_user_saved_identity_wins_over_projection() -> None:
    variant = {
        "text_mode": "agent_text",
        "intro_text": "Original AI intro",
        "text_elements_user_edited": True,
        "text_elements": [
            {
                "id": "edited-intro",
                "text": "User rewrite",
                "start_s": 0.0,
                "end_s": 3.0,
                "role": "generative_intro",
                "source_params": {
                    "source": "intro",
                    "key": "intro",
                    "identity": "intro:intro",
                },
            }
        ],
    }

    merged = merge_projected_text_elements_for_variant(variant)

    assert merged is not None
    assert len(merged) == 1
    assert merged[0]["id"] == "edited-intro"
    assert merged[0]["text"] == "User rewrite"


def test_validate_payload_adds_tombstone_for_deleted_projected_intro() -> None:
    variant = {
        "text_mode": "agent_text",
        "intro_text": "AI burned intro",
        "text_elements_user_edited": True,
    }
    payload = [
        {
            "id": "user-1",
            "text": "User label remains",
            "start_s": 4.0,
            "end_s": 5.0,
            "role": "generative_intro",
        }
    ]

    validated, _ = validate_text_elements_payload(
        variant,
        payload,
        require_base=False,
        strict_drop=True,
    )

    assert [e["text"] for e in validated if not e.get("removed")] == ["User label remains"]
    tombstones = [e for e in validated if e.get("removed")]
    assert len(tombstones) == 1
    assert text_element_source_identity(tombstones[0]) == "generative_intro:intro:intro"
    reloaded = merge_projected_text_elements_for_variant(
        {**variant, "text_elements": validated, "text_elements_user_edited": True}
    )
    assert reloaded is not None
    assert [e["text"] for e in reloaded] == ["User label remains"]


def test_append_tombstones_empty_payload_deletes_all_projected_ai_text() -> None:
    variant = {"text_mode": "agent_text", "intro_text": "AI burned intro"}

    validated = append_ai_text_tombstones(variant, [])

    assert len(validated) == 1
    assert validated[0]["removed"] is True
    assert text_element_source_identity(validated[0]) == "generative_intro:intro:intro"


def test_variants_for_response_merges_ai_intro_and_signs_media_preview(monkeypatch) -> None:
    monkeypatch.setattr(
        "app.routes.generative_jobs.signed_get_url",
        lambda path, ttl=None: f"https://signed/{path}",
    )
    job_id = uuid.uuid4()
    job = types.SimpleNamespace(
        id=job_id,
        mode="content_plan",
        all_candidates={"clip_paths": [f"generative-jobs/{job_id}/sources/clip.mp4"]},
        assembly_plan={
            "variants": [
                {
                    "variant_id": "original_text",
                    "text_mode": "agent_text",
                    "render_status": "ready",
                    "video_path": f"generative-jobs/{job_id}/out.mp4",
                    "intro_text": "AI burned intro",
                    "text_elements_user_edited": True,
                    "text_elements": [
                        {
                            "id": "user-1",
                            "text": "User bar",
                            "start_s": 4.0,
                            "end_s": 5.0,
                            "role": "generative_intro",
                        }
                    ],
                    "media_overlays": [
                        {
                            "id": "ov-1",
                            "kind": "image",
                            "src_gcs_path": "users/u1/plan/p1/overlays/card.heic",
                            "preview_gcs_path": "users/u1/plan/p1/overlays/card.heic.preview.jpg",
                            "start_s": 0,
                            "end_s": 3,
                        }
                    ],
                }
            ]
        },
    )

    [variant] = _variants_for_response(job)

    assert [e["text"] for e in variant["text_elements"]] == ["User bar", "AI burned intro"]
    assert "base_video_url" not in variant
    assert variant["editor_capabilities"]["text_elements"] is True
    assert variant["media_overlays"][0]["preview_url"] == (
        "https://signed/users/u1/plan/p1/overlays/card.heic.preview.jpg"
    )


def test_variants_for_response_lazy_backfills_legacy_heic_preview(monkeypatch) -> None:
    calls: list[str] = []
    monkeypatch.setattr(
        "app.routes.generative_jobs.convert_heif_overlay_preview",
        lambda path: (
            calls.append(path) or (f"{path}.preview.jpg", f"https://signed/{path}.preview.jpg")
        ),
    )
    monkeypatch.setattr(
        "app.routes.generative_jobs.signed_get_url",
        lambda path, ttl=None: f"https://signed/{path}",
    )
    _HEIF_PREVIEW_BACKFILL_ATTEMPTED.clear()
    job = types.SimpleNamespace(
        id=uuid.uuid4(),
        mode="content_plan",
        all_candidates={"clip_paths": []},
        assembly_plan={
            "variants": [
                {
                    "variant_id": "original_text",
                    "render_status": "ready",
                    "media_overlays": [
                        {
                            "id": "ov-1",
                            "kind": "image",
                            "src_gcs_path": "users/u1/plan/p1/overlays/card.HEIC",
                            "preview_gcs_path": " ",
                            "start_s": 0,
                            "end_s": 3,
                        }
                    ],
                }
            ]
        },
    )

    [variant] = _variants_for_response(job)

    assert calls == ["users/u1/plan/p1/overlays/card.HEIC"]
    assert getattr(job, "_media_overlay_preview_backfilled") is True
    stored = job.assembly_plan["variants"][0]["media_overlays"][0]
    assert stored["preview_gcs_path"] == "users/u1/plan/p1/overlays/card.HEIC.preview.jpg"
    assert variant["media_overlays"][0]["preview_url"] == (
        "https://signed/users/u1/plan/p1/overlays/card.HEIC.preview.jpg"
    )


def test_lazy_backfill_failure_does_not_retry_same_overlay(monkeypatch) -> None:
    calls: list[str] = []

    def _fail(path: str) -> tuple[None, None]:
        calls.append(path)
        return None, None

    monkeypatch.setattr("app.routes.generative_jobs.convert_heif_overlay_preview", _fail)
    _HEIF_PREVIEW_BACKFILL_ATTEMPTED.clear()
    job = types.SimpleNamespace(
        id=uuid.uuid4(),
        assembly_plan={
            "variants": [
                {
                    "variant_id": "original_text",
                    "media_overlays": [
                        {
                            "id": "ov-1",
                            "kind": "image",
                            "src_gcs_path": "users/u1/plan/p1/overlays/card.heif",
                            "preview_gcs_path": None,
                            "start_s": 0,
                            "end_s": 3,
                        }
                    ],
                }
            ]
        },
    )

    assert _lazy_backfill_media_overlay_previews(job) is False
    assert _lazy_backfill_media_overlay_previews(job) is False

    assert calls == ["users/u1/plan/p1/overlays/card.heif"]
    assert job.assembly_plan["variants"][0]["media_overlays"][0]["preview_gcs_path"] is None


def test_editor_commit_text_without_base_routes_to_full_render(monkeypatch) -> None:
    job_id = uuid.uuid4()
    prefix = f"generative-jobs/{job_id}/sources/"
    job = types.SimpleNamespace(
        id=job_id,
        mode="content_plan",
        status="variants_ready",
        all_candidates={"clip_paths": [f"{prefix}clip.mp4"]},
        assembly_plan={
            "variants": [
                {
                    "variant_id": "original_text",
                    "text_mode": "agent_text",
                    "render_status": "ready",
                    "render_finished_at": "2026-07-01T00:00:00Z",
                    "intro_text": "AI burned intro",
                    "ai_timeline": {
                        "beat_grid": [0.0, 1.0],
                        "slots": [
                            {
                                "slot_id": "s1",
                                "clip_index": 0,
                                "source_gcs_path": f"{prefix}clip.mp4",
                                "source_duration_s": 10.0,
                                "in_s": 0.0,
                                "duration_s": 1.0,
                                "duration_beats": 1,
                                "order": 0,
                            }
                        ],
                    },
                }
            ]
        },
    )
    payload = EditorCommitRequest(
        base_generation="2026-07-01T00:00:00Z",
        text_elements=[
            {
                "id": "edited-intro",
                "text": "Edited intro",
                "start_s": 0.0,
                "end_s": 3.0,
                "role": "generative_intro",
                "source_params": {
                    "source": "intro",
                    "key": "intro",
                    "identity": "intro:intro",
                },
            }
        ],
    )

    prep = prepare_editor_commit(job, "original_text", payload)

    assert prep["text_requires_full_render"] is True
    calls: list[dict] = []
    monkeypatch.setattr(
        "app.tasks.generative_build.regenerate_generative_variant",
        types.SimpleNamespace(apply_async=lambda **kwargs: calls.append(kwargs)),
        raising=False,
    )
    enqueue_editor_commit_render(str(job.id), "original_text", prep)
    assert len(calls) == 1
    assert "queue" not in calls[0]
    # text_requires_full_render commits pin the regen's fast-reburn check off
    # (force_full_render) so the full re-assembly actually happens.
    assert calls[0]["kwargs"] == {
        "render_gen_id": prep["generation"],
        "force_full_render": True,
    }
