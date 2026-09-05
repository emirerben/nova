from __future__ import annotations

import copy

from app.services.public_assembly_plan import (
    project_admin_debug_candidates,
    project_public_assembly_plan,
    project_public_assembly_plan_with_metadata,
)


def test_projection_recursively_strips_private_cleanup_and_identity_state() -> None:
    stored = {
        "title": "User-authored title",
        "_speech_cleanup_internal": {
            "render_generation_cleanup_pending": [{"private": True}],
        },
        "variants": [
            {
                "variant_id": "variant-1",
                "candidate_snapshot": {
                    "clip_paths": ["source.mp4"],
                    "clip_source_instance_ids": ["00000000-0000-4000-8000-000000000001"],
                    "clip_metadata_identity_index_v2": {"records": [{"secret": True}]},
                    "clip_metadata_identity_index_v99": {"future": "also private"},
                    "source_tag": "0123456789abcdef",
                    "user_note": "keep me",
                },
            }
        ],
    }
    snapshot = copy.deepcopy(stored)

    projected = project_public_assembly_plan(stored)

    assert projected == {
        "title": "User-authored title",
        "variants": [
            {
                "variant_id": "variant-1",
                "candidate_snapshot": {
                    "clip_paths": ["source.mp4"],
                    "user_note": "keep me",
                },
            }
        ],
    }
    assert stored == snapshot


def test_projection_preserves_none_scalars_and_unrelated_internal_looking_fields() -> None:
    assert project_public_assembly_plan(None) is None
    assert project_public_assembly_plan("legacy") == "legacy"
    assert project_public_assembly_plan(
        {"metadata_identity": "user value", "clip_metadata": {"identity": "keep"}}
    ) == {"metadata_identity": "user value", "clip_metadata": {"identity": "keep"}}


def test_admin_candidate_projection_exposes_only_top_level_source_vector() -> None:
    candidates = {
        "clip_paths": ["durable/a.mp4"],
        "clip_source_instance_ids": ["00000000-0000-4000-8000-000000000001"],
        "clip_metadata_identity_index_v2": {"records": [{"private": True}]},
        "nested": {
            "clip_source_instance_ids": ["must-not-leak"],
            "_speech_cleanup_internal": {"locks": True},
        },
    }

    projected = project_admin_debug_candidates(candidates)

    assert projected == {
        "clip_paths": ["durable/a.mp4"],
        "clip_source_instance_ids": ["00000000-0000-4000-8000-000000000001"],
        "nested": {},
    }
    assert "clip_source_instance_ids" not in project_public_assembly_plan(candidates)


def test_projection_serves_last_good_media_while_required_speech_compose_is_locked() -> None:
    stored = {
        "speech_cleanup_contract": "required_v1",
        "speech_cut_control": {
            "variant_id": "subtitled",
            "operation_id": "operation-a",
            "render_generation_id": "generation-new",
        },
        "speech_cut_previous_variant": {
            "variant_id": "subtitled",
            "render_status": "ready",
            "render_generation_id": "generation-old",
            "ok": True,
            "video_path": "generative-jobs/job/last-good.mp4",
            "output_url": "https://storage/last-good",
        },
        "speech_cut_previous_variants": [],
        "_speech_cleanup_internal": {
            "required_speech_generation_locks": {"subtitled": "generation-new"},
            "staged_render_results": {
                "subtitled:generation-new": {
                    "variant_id": "subtitled",
                    "render_generation_id": "generation-new",
                    "video_path": "generative-jobs/job/render-generations/generation-new/core.mp4",
                }
            },
        },
        "variants": [
            {
                "variant_id": "subtitled",
                "render_status": "rendering",
                "render_generation_id": "generation-new",
                "ok": True,
                "video_path": "generative-jobs/job/render-generations/generation-new/composed.mp4",
                "output_url": "https://storage/provisional",
                "speech_cut_in_flight": {"operation_id": "operation-a"},
            }
        ],
    }

    projected = project_public_assembly_plan(stored)

    assert projected["variants"] == [
        {
            "variant_id": "subtitled",
            "render_status": "rendering",
            "render_generation_id": "generation-new",
            "ok": False,
            "video_path": "generative-jobs/job/last-good.mp4",
            "output_url": "https://storage/last-good",
            "speech_cut_in_flight": {"operation_id": "operation-a"},
        }
    ]
    assert "speech_cut_control" not in projected
    assert "speech_cut_previous_variant" not in projected
    assert "speech_cut_previous_variants" not in projected
    assert "_speech_cleanup_internal" not in projected

    projection = project_public_assembly_plan_with_metadata(stored)
    assert projection.value == projected
    assert projection.masked_last_good_variant_ids == frozenset({"subtitled"})
    assert "masked_last_good_variant_ids" not in str(projection.value)


def test_projection_prefers_exact_last_good_vector_when_private_owners_disagree() -> None:
    last_good = {
        "variant_id": "subtitled",
        "render_status": "ready",
        "render_generation_id": "generation-old",
        "ok": True,
        "video_path": "generative-jobs/job/last-good.mp4",
        "output_url": "https://storage/last-good",
    }
    stored = {
        "speech_cleanup_contract": "required_v1",
        "speech_cut_control": {
            "variant_id": "subtitled",
            "operation_id": "operation-a",
            "render_generation_id": "generation-control",
        },
        # Creator bundles use this singular field for the private working lane.
        "speech_cut_previous_variant": {
            **last_good,
            "render_status": "rendering",
            "render_generation_id": "generation-control",
            "video_path": ("generative-jobs/job/render-generations/generation-control/working.mp4"),
            "output_url": "https://private.example/working",
        },
        "speech_cut_previous_variants": [copy.deepcopy(last_good)],
        "_speech_cleanup_internal": {
            # Corrupt/mismatched lock metadata must not turn the current row public.
            "required_speech_generation_locks": {"subtitled": "generation-lock"},
        },
        "variants": [
            {
                "variant_id": "subtitled",
                "render_status": "rendering",
                "render_generation_id": "generation-control",
                "ok": True,
                "video_path": (
                    "generative-jobs/job/render-generations/generation-control/provisional.mp4"
                ),
                "output_url": "https://private.example/provisional",
            }
        ],
    }

    projection = project_public_assembly_plan_with_metadata(stored)

    assert projection.value["variants"] == [
        {
            **last_good,
            "render_status": "rendering",
            "render_generation_id": "generation-control",
            "ok": False,
        }
    ]
    assert projection.masked_last_good_variant_ids == frozenset({"subtitled"})
    assert "private.example" not in str(projection.value)
    assert "provisional.mp4" not in str(projection.value)


def test_projection_strips_every_media_reference_when_last_good_is_unprovable() -> None:
    malformed_states = [
        {
            "speech_cut_control": {
                "variant_id": "subtitled",
                "render_generation_id": "generation-new",
            },
        },
        {
            "speech_cut_control": "malformed",
            "_speech_cleanup_internal": {
                "required_speech_generation_locks": {"subtitled": "generation-new"},
            },
        },
        {
            "speech_cut_control": {
                "variant_id": "subtitled",
                "render_generation_id": "generation-new",
            },
            "_speech_cleanup_internal": "malformed",
        },
        {
            "speech_cut_control": {
                "variant_id": "subtitled",
                "render_generation_id": "generation-new",
            },
            "_speech_cleanup_internal": {
                "required_speech_generation_locks": {"subtitled": {"bad": True}},
            },
        },
    ]
    provisional = {
        "variant_id": "subtitled",
        "render_status": "rendering",
        "render_generation_id": "generation-new",
        "ok": True,
        "video_path": ("generative-jobs/job/render-generations/generation-new/PROVISIONAL.mp4"),
        "output_url": "https://private.example/PROVISIONAL",
        "poster_path": "generative-jobs/job/PROVISIONAL.jpg",
        "base_video_path": "generative-jobs/job/PROVISIONAL-base.mp4",
        "source_audio_options": [
            {
                "audio_path": "generative-jobs/job/PROVISIONAL.m4a",
                "audio_url": "https://private.example/PROVISIONAL-audio",
            }
        ],
        "media_overlays": [
            {
                "asset_id": "private-asset",
                "src_gcs_path": "generative-jobs/job/PROVISIONAL-overlay.png",
            }
        ],
        "nested": {
            "source_references": ["generative-jobs/job/PROVISIONAL-source.mov"],
            "source_tag": "0123456789abcdef",
            "opaque_locator": "https://private.example/PROVISIONAL-opaque",
            "untyped_values": ["PROVISIONAL.mp4", "keep this label"],
        },
    }

    for malformed in malformed_states:
        stored = {
            "speech_cleanup_contract": "required_v1",
            "variants": [copy.deepcopy(provisional)],
            **copy.deepcopy(malformed),
        }
        snapshot = copy.deepcopy(stored)

        projection = project_public_assembly_plan_with_metadata(stored)
        [visible] = projection.value["variants"]

        assert visible["variant_id"] == "subtitled"
        assert visible["render_status"] == "rendering"
        assert visible["ok"] is False
        assert "provisional" not in str(visible).lower()
        assert "private-asset" not in str(visible)
        assert "0123456789abcdef" not in str(visible)
        assert visible["nested"]["untyped_values"] == ["keep this label"]
        assert "render_generation_id" not in visible
        assert projection.masked_last_good_variant_ids == frozenset()
        assert stored == snapshot


def test_projection_redacts_every_variant_when_active_target_is_malformed() -> None:
    stored = {
        "speech_cleanup_contract": "required_v1",
        "speech_cut_control": {"render_generation_id": "generation-new"},
        "variants": [
            {
                "variant_id": "first",
                "render_status": "ready",
                "video_path": "generative-jobs/job/first.mp4",
                "output_url": "https://private.example/first",
            },
            {
                "variant_id": "second",
                "render_status": "ready",
                "video_path": "generative-jobs/job/second.mp4",
                "output_url": "https://private.example/second",
            },
        ],
    }

    projected = project_public_assembly_plan(stored)

    assert [row["variant_id"] for row in projected["variants"]] == ["first", "second"]
    assert all(row["render_status"] == "rendering" for row in projected["variants"])
    assert all(row["ok"] is False for row in projected["variants"])
    assert "private.example" not in str(projected)
    assert ".mp4" not in str(projected)


def test_projection_fails_closed_when_required_lock_survives_a_missing_contract() -> None:
    stored = {
        "_speech_cleanup_internal": {
            "required_speech_generation_locks": {"subtitled": "generation-new"},
        },
        "variants": [
            {
                "variant_id": "subtitled",
                "render_status": "rendering",
                "render_generation_id": "generation-new",
                "video_path": (
                    "generative-jobs/job/render-generations/generation-new/provisional.mp4"
                ),
                "output_url": "https://private.example/provisional",
            }
        ],
    }

    projected = project_public_assembly_plan(stored)

    assert projected == {
        "variants": [
            {
                "variant_id": "subtitled",
                "render_status": "rendering",
                "ok": False,
            }
        ]
    }


def test_projection_fails_closed_when_control_survives_a_missing_contract() -> None:
    stored = {
        "speech_cut_control": {
            "variant_id": "subtitled",
            "operation_id": "operation-a",
            "render_generation_id": "generation-new",
        },
        "variants": [
            {
                "variant_id": "subtitled",
                "render_status": "ready",
                "render_generation_id": "generation-new",
                "video_path": (
                    "generative-jobs/job/render-generations/generation-new/provisional.mp4"
                ),
                "output_url": "https://private.example/provisional",
            }
        ],
    }

    projection = project_public_assembly_plan_with_metadata(stored)

    assert projection.value == {
        "variants": [
            {
                "variant_id": "subtitled",
                "render_status": "rendering",
                "ok": False,
            }
        ]
    }
    assert projection.masked_last_good_variant_ids == frozenset()
    assert projection.media_unavailable_variant_ids == frozenset({"subtitled"})


def test_projection_fails_closed_when_required_contract_survives_without_owners() -> None:
    provisional_path = "generative-jobs/job/render-generations/generation-new/provisional.mp4"
    stored = {
        "speech_cleanup_contract": "required_v1",
        "output_path": provisional_path,
        "variants": [
            {
                "variant_id": "subtitled",
                "render_status": "rendering",
                "render_generation_id": "generation-new",
                "video_path": provisional_path,
                "output_url": "https://private.example/provisional",
            }
        ],
    }

    projection = project_public_assembly_plan_with_metadata(stored)

    assert projection.value == {
        "speech_cleanup_contract": "required_v1",
        "variants": [
            {
                "variant_id": "subtitled",
                "render_status": "rendering",
                "ok": False,
            }
        ],
    }
    assert projection.active_speech_projection is True
    assert projection.media_unavailable_variant_ids == frozenset({"subtitled"})


def test_projection_keeps_completed_required_contract_public() -> None:
    stored = {
        "speech_cleanup_contract": "required_v1",
        "variants": [
            {
                "variant_id": "subtitled",
                "render_status": "ready",
                "render_generation_id": "generation-published",
                "video_path": (
                    "generative-jobs/job/render-generations/generation-published/core.mp4"
                ),
            }
        ],
    }

    projection = project_public_assembly_plan_with_metadata(stored)

    assert projection.value == stored
    assert projection.active_speech_projection is False


def test_legacy_control_only_projection_keeps_an_exact_last_good_vector() -> None:
    last_good = {
        "variant_id": "subtitled",
        "render_status": "ready",
        "render_generation_id": "generation-old",
        "video_path": "generative-jobs/job/last-good.mp4",
        "output_url": "https://storage.example/last-good",
    }
    stored = {
        "speech_cut_control": {
            "variant_id": "subtitled",
            "operation_id": "legacy-operation",
            "render_generation_id": "generation-new",
        },
        "speech_cut_previous_variants": [copy.deepcopy(last_good)],
        "variants": [
            {
                "variant_id": "subtitled",
                "render_status": "rendering",
                "render_generation_id": "generation-new",
                "video_path": (
                    "generative-jobs/job/render-generations/generation-new/provisional.mp4"
                ),
            }
        ],
    }

    projection = project_public_assembly_plan_with_metadata(stored)

    assert projection.value["variants"] == [
        {
            **last_good,
            "render_status": "rendering",
            "render_generation_id": "generation-new",
            "ok": False,
        }
    ]
    assert projection.masked_last_good_variant_ids == frozenset({"subtitled"})
    assert projection.media_unavailable_variant_ids == frozenset()
    assert projection.active_speech_projection is True
