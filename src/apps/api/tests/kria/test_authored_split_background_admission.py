"""Authored split-origin speakers retain the existing background APIs under their gates."""

from copy import deepcopy

import pytest

from app.config import settings
from app.routes import generative_jobs as gj
from app.services.client_protocol import clear_request_context, set_client_protocol
from app.services.kria_editor_ops import build_editor_snapshot
from tests.routes.test_speaker_background_editor import PHOTO, _background, _recipe, background_job


@pytest.fixture
def speaker(monkeypatch):
    job = background_job(monkeypatch, background=_background())
    from app.services.phone_sources import PHONE_SOURCES_FIELD

    binding = job.assembly_plan[PHONE_SOURCES_FIELD][0]
    job.all_candidates = {"clip_paths": [binding["proxy_path"]]}
    job.status = "variants_ready"
    variant = job.assembly_plan["variants"][0]
    variant.update(editor_timeline_mode="authored", render_status="ready", duration_s=4)
    variant["user_timeline"] = {
        "slots": [
            {
                "slot_id": "speaker",
                "clip_index": 0,
                "in_s": 0,
                "duration_s": 4,
                "speaker_subject": True,
                "split_section_id": "s",
                "split_panel_index": 0,
                "split_offset_s": 0,
            }
        ],
        "split_screen_sections": [{"id": "s", "duration_s": 4, "panel_count": 2}],
    }
    variant["speaker_background"] = {
        "media_id": PHOTO.media_id,
        "kind": "image",
        "source_start_s": 0,
    }
    monkeypatch.setattr(
        settings,
        "phone_render_verified_features",
        [
            *settings.phone_render_verified_features,
            "splitScreen",
            "speakerBackgroundWindows",
            "speakerBackgroundColor",
            "backgroundFocus",
        ],
    )
    monkeypatch.setattr(settings, "background_focus_enabled", True)
    set_client_protocol(45)
    yield job, variant
    clear_request_context()


def test_authored_split_emits_existing_background_capabilities_and_ai_family(speaker):
    job, variant = speaker
    caps = gj._editor_capabilities(job, variant)
    assert caps["speaker_background"] is True
    assert caps["speaker_background_windows"] is True
    assert caps["speaker_background_color"] is True
    snapshot = build_editor_snapshot(job, variant)
    assert "background_windows" in snapshot["allowed_op_families"]
    assert snapshot["background"]["windows_available"] is True


@pytest.mark.parametrize(
    "change",
    ["old_client", "unverified", "cloud", "empty", "unrelated", "non_subject", "unresolved"],
)
def test_unqualified_authored_background_stays_closed(speaker, monkeypatch, change):
    job, variant = speaker
    if change == "old_client":
        set_client_protocol(42)
    elif change == "unverified":
        monkeypatch.setattr(
            settings,
            "phone_render_verified_features",
            [f for f in settings.phone_render_verified_features if f != "splitScreen"],
        )
    elif change == "cloud":
        variant["render_destination"] = "cloud"
    elif change == "empty":
        variant["editor_state"] = "empty"
    elif change == "unrelated":
        variant["user_timeline"]["split_screen_sections"] = []
    elif change == "non_subject":
        variant["user_timeline"]["slots"][0]["speaker_subject"] = False
    else:
        variant["user_timeline"]["slots"][0]["clip_index"] = 99
    assert not gj._speaker_background_capability(job, variant)


def test_existing_feature_gates_remain_independent(speaker, monkeypatch):
    job, variant = speaker
    monkeypatch.setattr(
        settings,
        "phone_render_verified_features",
        [f for f in settings.phone_render_verified_features if f != "speakerBackgroundWindows"],
    )
    assert gj._speaker_background_capability(job, variant)
    assert not gj._speaker_background_windows_capability(job, variant)
    monkeypatch.setattr(settings, "speaker_background_enabled", False)
    assert not gj._speaker_background_capability(job, variant)


@pytest.mark.parametrize(
    "document",
    [
        {"kind": "color", "color": "#FFFFFF"},
        {
            "media_id": PHOTO.media_id,
            "kind": "image",
            "speaker_frame": {"width_fraction": 0.4, "x_fraction": 0.75, "y_fraction": 0.75},
        },
        {
            "media_id": PHOTO.media_id,
            "kind": "image",
            "focus": {"x": 0.2, "y": 0.5, "zoom": 1.2},
            "windows": [{"media_id": PHOTO.media_id, "kind": "image", "start_s": 0, "end_s": 2}],
        },
    ],
)
def test_advertised_background_changes_validate_and_compile(speaker, document):
    from app.pipeline.phone_authored_timeline import compile_phone_authored_timeline

    job, variant = speaker
    changed, stored = gj._validate_speaker_background_section(
        job, variant, gj.EditorCommitRequest(speaker_background=document)
    )
    assert changed
    staged = deepcopy(variant)
    staged["speaker_background"] = stored
    recipe = compile_phone_authored_timeline(job, staged, _recipe(job))
    assert recipe.speaker_background is not None
    assert recipe.speaker_background.subject_clip_ids == ["speaker"]
    if document.get("focus"):
        assert recipe.speaker_background.focus.x == 0.2
    if document.get("windows"):
        assert len(recipe.speaker_background.windows) == 1


@pytest.mark.parametrize(
    "operation",
    [
        {
            "op": "set_speaker_frame",
            "selector": {"slot_indexes": [0]},
            "preset": "small_bottom_right",
        },
        {
            "op": "set_speaker_frame",
            "selector": {"slot_indexes": [0]},
            "frame": {"width_fraction": 0.3, "x_fraction": 0.2, "y_fraction": 0.8},
        },
        {"op": "clear_speaker_frame", "selector": {"slot_indexes": [0]}},
    ],
)
def test_targeted_operations_reachable_from_real_authored_snapshot(speaker, operation):
    from app.agents.edit_copilot import _ParseState, parse_editor_operation
    from app.services.kria_editor_ops import compile_editor_ops

    job, variant = speaker
    snapshot = build_editor_snapshot(job, variant)
    snapshot["editor_ops_version"] = 2
    parsed = parse_editor_operation(operation, snapshot, _ParseState(0.99))
    assert parsed is not None
    payload = compile_editor_ops(job, variant, [parsed]).payload
    assert payload.timeline_slots[0].speaker_frame_override is not None
    assert "speaker_background" not in payload.model_fields_set


def test_global_background_removal_recompiles_without_fallback(speaker):
    from app.services.authored_editor import prepare_authored_editor_commit
    from app.services.device_render import device_status

    job, variant = speaker
    before = deepcopy(variant["user_timeline"]["slots"])
    prepare_authored_editor_commit(
        job,
        "subtitled",
        gj.EditorCommitRequest(base_generation="first", speaker_background=None),
        validation_arguments={},
    )
    current = job.assembly_plan["variants"][0]
    assert current["speaker_background"] is None
    assert device_status(job, "subtitled").request.recipe.speaker_background is None
    assert current["user_timeline"]["slots"][0]["in_s"] == before[0]["in_s"]


def test_initial_talking_split_without_background_keeps_speaker_eligibility(monkeypatch):
    from app.pipeline.phone_initial_split_screen import compile_initial_split_screen
    from app.services.device_render import device_status
    from tests.routes.test_authored_playback_speed import _authored_phone_job

    job, variant = _authored_phone_job(monkeypatch)
    # pin_device_request copies assembly_plan; the helper returns its pre-pin row.
    variant = job.assembly_plan["variants"][0]
    variant["resolved_archetype"] = "subtitled"
    monkeypatch.setattr(
        settings,
        "phone_render_verified_features",
        [*settings.phone_render_verified_features, "splitScreen"],
    )
    job.all_candidates["creator_strategy"] = {
        "split_screen": {"panels": [{"media_id": "old"}, {"media_id": "new"}], "duration_s": 3}
    }
    compile_initial_split_screen(job, device_status(job, "original_text").request)
    rows = job.assembly_plan["variants"][0]["user_timeline"]["slots"]
    assert rows[0]["speaker_subject"] is True
    assert rows[1].get("speaker_subject") is not True


@pytest.mark.parametrize(
    "policy",
    [
        {"mode": "inherit"},
        {"mode": "custom", "frame": {"width_fraction": 0.4, "x_fraction": 0.5, "y_fraction": 0.5}},
    ],
)
def test_unrelated_authored_policy_cannot_establish_split_origin(speaker, policy):
    job, variant = speaker
    variant["user_timeline"]["split_screen_sections"] = []
    row = variant["user_timeline"]["slots"][0]
    for key in ("split_section_id", "split_panel_index", "split_offset_s"):
        row.pop(key)
    row["speaker_frame_override"] = policy
    row["speaker_subject"] = True
    assert "editor_split_screen_origin" not in variant
    assert not gj._speaker_background_capability(job, variant)
    snapshot = build_editor_snapshot(job, variant)
    assert "background" not in snapshot["allowed_op_families"]
    assert "background_windows" not in snapshot["allowed_op_families"]


def test_current_split_origin_requires_valid_grouped_layout(speaker):
    job, variant = speaker
    variant["user_timeline"]["slots"][0]["split_panel_index"] = 2
    assert not gj._speaker_background_capability(job, variant)
