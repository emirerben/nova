"""Occurrence framing must not mutate peer frames, source clocks or the global background."""

from copy import deepcopy
from types import SimpleNamespace

import pytest
from pydantic import ValidationError

from app.agents.editor_ops_v2.background import _op_clear_speaker_frame, _op_set_speaker_frame
from app.kria.portable_speaker_background import SpeakerFrameOverride
from app.routes.generative_jobs import TimelineSlotEdit

FRAME = {"width_fraction": 0.4, "x_fraction": 0.25, "y_fraction": 0.75}


@pytest.mark.parametrize(
    "value", [{"mode": "full"}, {"mode": "inherit"}, {"mode": "custom", "frame": FRAME}]
)
def test_override_survives_save_wire(value):
    slot = TimelineSlotEdit(clip_index=0, in_s=1, duration_s=2, speaker_frame_override=value)
    wire = slot.model_dump(mode="json", exclude_none=True)
    assert wire["speaker_frame_override"] == value
    assert (
        TimelineSlotEdit.model_validate(wire).speaker_frame_override.model_dump(exclude_none=True)
        == value
    )


@pytest.mark.parametrize(
    "value",
    [
        {"mode": "custom"},
        {"mode": "full", "frame": FRAME},
        {"mode": "inherit", "frame": FRAME},
        {"mode": "custom", "frame": {**FRAME, "width_fraction": 0.1}},
        {"mode": "custom", "frame": {**FRAME, "x_fraction": float("nan")}},
    ],
)
def test_invalid_override_refused(value):
    with pytest.raises(ValidationError):
        SpeakerFrameOverride.model_validate(value)


def state(monkeypatch):
    from app.services import kria_editor_ops as ops

    monkeypatch.setattr(ops, "split_screen_available", lambda *args: True)
    rows = [
        {
            "slot_id": str(i),
            "media_id": f"m{i}",
            "clip_index": i,
            "in_s": 1,
            "duration_s": 2,
            "source_end_s": 5,
            "speaker_subject": True,
            "media_kind": "video",
            "split_section_id": "s",
            "split_panel_index": i,
            "split_offset_s": 0,
        }
        for i in range(2)
    ]
    return SimpleNamespace(
        slots=rows,
        speaker_background={"color": "#000000", "speaker_frame": FRAME},
        changed=set(),
        job=None,
        variant={"speaker_background": {"color": "#000000", "speaker_frame": FRAME}},
        summary="",
    )


def test_targeted_custom_full_and_inherit_preserve_peer_global_and_source(monkeypatch):
    draft = state(monkeypatch)
    before = deepcopy(draft.slots)
    background = deepcopy(draft.speaker_background)
    selector = {"all": True}
    _op_set_speaker_frame(draft, {"selector": selector, "frame": FRAME})
    assert all(
        row["speaker_frame_override"] == {"mode": "custom", "frame": FRAME} for row in draft.slots
    )
    _op_clear_speaker_frame(draft, {"selector": {"media_kind": "video"}})
    assert all(row["speaker_frame_override"] == {"mode": "full"} for row in draft.slots)
    _op_set_speaker_frame(draft, {"selector": selector, "preset": "inherit"})
    assert all(row["speaker_frame_override"] == {"mode": "inherit"} for row in draft.slots)
    assert [
        {k: v for k, v in row.items() if k != "speaker_frame_override"} for row in draft.slots
    ] == before
    assert draft.speaker_background == background
    assert draft.changed == {"timeline"}


def test_invalid_target_is_atomic(monkeypatch):
    draft = state(monkeypatch)
    draft.slots[1]["speaker_subject"] = False
    before = deepcopy(draft.slots)
    with pytest.raises(ValueError):
        _op_set_speaker_frame(draft, {"selector": {"all": True}, "frame": FRAME})
    assert draft.slots == before


def test_targeted_frame_requires_split_gate(monkeypatch):
    from app.services import kria_editor_ops as ops

    draft = state(monkeypatch)
    monkeypatch.setattr(ops, "split_screen_available", lambda *args: False)
    with pytest.raises(ValueError):
        _op_clear_speaker_frame(draft, {"selector": {"all": True}})
    assert not draft.changed


@pytest.mark.parametrize("mode", ["full", "inherit", "custom"])
def test_recipe_override_requires_gate_and_exact_subject(mode):
    import json
    from pathlib import Path

    from app.kria.recipes_v2 import EditRecipeV2

    raw = json.loads((Path(__file__).parents[1] / "fixtures/kria_edit_recipe_v2.json").read_text())
    override = {"mode": mode, **({"frame": FRAME} if mode == "custom" else {})}
    raw["speaker_background"] = {
        "subject_track_id": "video",
        "media_kind": "color",
        "color": "#000000",
        "speaker_frame": FRAME,
    }
    raw["tracks"][0]["clips"][0]["speaker_frame_override"] = override
    recipe = EditRecipeV2.model_validate(raw)
    assert "splitScreen" in recipe.required_capabilities
    assert recipe.model_dump()["tracks"][0]["clips"][0]["speaker_frame_override"]["mode"] == mode
    if mode == "inherit":
        raw.pop("speaker_background")
        assert "splitScreen" in EditRecipeV2.model_validate(raw).required_capabilities
        return
    raw["speaker_background"]["subject_clip_ids"] = []
    with pytest.raises(ValueError, match="speaker frame override"):
        EditRecipeV2.model_validate(raw)
    raw.pop("speaker_background")
    with pytest.raises(ValueError, match="speaker frame override"):
        EditRecipeV2.model_validate(raw)


def test_authored_resolver_preserves_omitted_override_and_refuses_old_client(monkeypatch):
    from fastapi import HTTPException

    from app.services.authored_editor import resolve_authored_phone_slots
    from tests.pipeline.test_authored_timeline import fixture

    job, variant, _ = fixture()
    original = variant["user_timeline"]["slots"][0]
    original["speaker_frame_override"] = {"mode": "full"}
    monkeypatch.setattr("app.services.phone_rollout.split_screen_supported", lambda **kw: True)
    monkeypatch.setattr(
        "app.services.phone_rollout.split_screen_client_supported", lambda **kw: True
    )
    saved = resolve_authored_phone_slots(
        job,
        variant,
        [
            TimelineSlotEdit.model_validate(
                {k: v for k, v in original.items() if k != "speaker_frame_override"}
            )
        ],
    )
    assert saved[0]["speaker_frame_override"] == {"mode": "full"}
    monkeypatch.setattr(
        "app.services.phone_rollout.split_screen_client_supported", lambda **kw: False
    )
    with pytest.raises(HTTPException) as error:
        resolve_authored_phone_slots(job, variant, [TimelineSlotEdit.model_validate(original)])
    assert error.value.status_code == 426


def test_compile_retains_occurrence_full_without_changing_global_frame():
    from app.kria.portable_speaker_background import SpeakerBackgroundLayer
    from app.pipeline.phone_authored_timeline import compile_phone_authored_timeline
    from tests.pipeline.test_authored_timeline import fixture

    job, variant, previous = fixture()
    row = variant["user_timeline"]["slots"][0]
    row.update(speaker_subject=True, speaker_frame_override={"mode": "full"})
    previous.speaker_background = SpeakerBackgroundLayer(
        subject_track_id=previous.tracks[0].id,
        media_kind="color",
        color="#000000",
        speaker_frame=FRAME,
    )
    compiled = compile_phone_authored_timeline(job, variant, previous)
    assert compiled.tracks[0].clips[0].speaker_frame_override.mode == "full"
    assert compiled.speaker_background.speaker_frame.model_dump() == FRAME
    assert "splitScreen" in compiled.required_capabilities


def test_selected_frame_preserves_peer_and_survives_ai_manual_ai_projection(monkeypatch):
    from app.services import kria_editor_ops as ops

    draft = state(monkeypatch)
    peer = deepcopy(draft.slots[1])
    _op_set_speaker_frame(draft, {"selector": {"clip_ids": ["m0"]}, "frame": FRAME})
    assert draft.slots[1] == peer
    payload = {
        "timeline_slots": [
            row.model_dump(mode="json", exclude_none=True)
            for row in ops._timeline_models(draft.slots)
        ]
    }
    variant = {"user_timeline": {"slots": deepcopy(draft.slots)}}
    projected = ops.project_editor_draft(variant, payload)
    assert projected["user_timeline"]["slots"][0]["speaker_frame_override"] == {
        "mode": "custom",
        "frame": FRAME,
    }
    manual = TimelineSlotEdit.model_validate(payload["timeline_slots"][0])
    assert manual.speaker_frame_override.frame.model_dump() == FRAME
    draft.slots = projected["user_timeline"]["slots"]
    _op_clear_speaker_frame(draft, {"selector": {"clip_ids": ["m0"]}})
    assert draft.slots[0]["speaker_frame_override"] == {"mode": "full"}
    assert draft.slots[1].get("speaker_frame_override") is None


def test_targeted_parser_rejects_ambiguous_geometry_and_preserves_selector():
    from app.agents.edit_copilot import _ParseState
    from app.agents.editor_ops_v2.background import _coerce_set_speaker_frame

    snapshot = {"split_screen_available": True, "slots": [{"slot_id": "a"}]}
    # all selector is independent of snapshot indexing.
    valid = {"selector": {"all": True}, "frame": FRAME}
    result = _coerce_set_speaker_frame("set_speaker_frame", valid, snapshot, _ParseState(0.99))
    assert result == {"op": "set_speaker_frame", **valid}
    assert (
        _coerce_set_speaker_frame(
            "set_speaker_frame",
            {**valid, "preset": "small_bottom_right"},
            snapshot,
            _ParseState(0.99),
        )
        is None
    )
    assert (
        _coerce_set_speaker_frame(
            "set_speaker_frame", {"frame": FRAME}, snapshot, _ParseState(0.99)
        )
        is None
    )


def test_snapshot_and_occurrence_copy_keep_explicit_full():
    from app.services.kria_editor_ops import _safe_slot, copy_source_occurrence

    row = {
        "slot_id": "a",
        "clip_index": 0,
        "in_s": 1,
        "duration_s": 2,
        "speaker_frame_override": {"mode": "full"},
    }
    assert _safe_slot(row, 0)["speaker_frame_override"] == {"mode": "full"}
    copied = copy_source_occurrence(row)
    copied["speaker_frame_override"]["mode"] = "inherit"
    assert row["speaker_frame_override"] == {"mode": "full"}


def test_remove_background_resets_all_policies_atomically(monkeypatch):
    from app.agents.editor_ops_v2.background import _op_remove_background

    draft = state(monkeypatch)
    _op_set_speaker_frame(draft, {"selector": {"all": True}, "frame": FRAME})
    before = deepcopy(draft.slots)
    _op_remove_background(draft, {})
    assert draft.speaker_background is None
    assert draft.changed == {"timeline", "speaker_background"}
    for old, row in zip(before, draft.slots, strict=True):
        assert row == {**old, "speaker_frame_override": {"mode": "inherit"}}


def test_inherit_can_reset_former_subject_without_background(monkeypatch):
    draft = state(monkeypatch)
    draft.variant = {"speaker_background": None}
    draft.speaker_background = None
    draft.slots[0].update(
        speaker_subject=False, media_kind="image", speaker_frame_override={"mode": "full"}
    )
    _op_set_speaker_frame(draft, {"selector": {"clip_ids": ["m0"]}, "preset": "inherit"})
    assert draft.slots[0]["speaker_frame_override"] == {"mode": "inherit"}
    assert draft.slots[0]["speaker_subject"] is False


def test_background_only_save_resets_persisted_occurrences():
    from app.routes.generative_jobs import EditorCommitRequest
    from app.services.authored_editor import reset_removed_background_frames

    rows = [
        {
            "slot_id": "a",
            "clip_index": 0,
            "in_s": 1,
            "duration_s": 2,
            "speaker_frame_override": {"mode": "full"},
        }
    ]
    request = EditorCommitRequest(speaker_background=None)
    result = reset_removed_background_frames(request, rows)
    assert result.timeline_slots[0].speaker_frame_override.mode == "inherit"
    assert result.timeline_slots[0].in_s == 1
    assert result.timeline_slots[0].duration_s == 2
    assert "speaker_background" in result.model_fields_set
    assert "timeline_slots" in result.model_fields_set
    untouched = EditorCommitRequest()
    assert reset_removed_background_frames(untouched, rows) is untouched


@pytest.mark.parametrize(
    "changes",
    [
        {"text_elements": []},
        {
            "timeline_slots": [
                {
                    "slot_id": "a",
                    "clip_index": 0,
                    "in_s": 0,
                    "duration_s": 2,
                    "speaker_frame_override": None,
                }
            ]
        },
        {"timeline_slots": [{"slot_id": "b", "clip_index": 0, "in_s": 0, "duration_s": 2}]},
    ],
)
def test_old_client_cannot_remove_saved_unstacked_policy(monkeypatch, changes):
    from fastapi import HTTPException

    from app.routes.generative_jobs import EditorCommitRequest
    from app.services import split_screen_editor

    variant = {
        "render_destination": "device",
        "user_timeline": {"slots": [{"slot_id": "a", "speaker_frame_override": {"mode": "full"}}]},
    }
    before = deepcopy(variant)
    monkeypatch.setattr(split_screen_editor, "split_screen_client_supported", lambda: False)
    with pytest.raises(HTTPException) as error:
        split_screen_editor.require_split_screen_commit(
            variant, EditorCommitRequest.model_validate(changes)
        )
    assert error.value.status_code == 426
    assert variant == before


@pytest.mark.asyncio
async def test_legacy_edit_cannot_remove_saved_unstacked_policy(monkeypatch):
    from fastapi import HTTPException

    from app.routes import generative_jobs as gj
    from app.services import split_screen_editor

    variant = {
        "variant_id": "v",
        "render_destination": "device",
        "user_timeline": {
            "slots": [{"slot_id": "a", "speaker_frame_override": {"mode": "inherit"}}]
        },
    }
    job = SimpleNamespace(assembly_plan={"variants": [variant]})
    before = deepcopy(job.assembly_plan)
    monkeypatch.setattr(gj, "_assert_variant_generation_editable_or_409", lambda *a: None)
    monkeypatch.setattr(split_screen_editor, "split_screen_client_supported", lambda: False)
    payload = gj.TimelineEditRequest(slots=[TimelineSlotEdit(clip_index=0, in_s=0, duration_s=2)])
    with pytest.raises(HTTPException) as error:
        await gj.dispatch_edit_timeline(job, "v", payload, db=None)
    assert error.value.status_code == 426
    assert job.assembly_plan == before


def _replacement_fixture(photo=False):
    from app.kria.portable_speaker_background import SpeakerBackgroundLayer
    from tests.pipeline.test_authored_timeline import fixture
    from tests.pipeline.test_phone_subtitled_plan import _photo_visual
    from tests.routes.test_phone_subtitled_editor_media import _admit

    job, variant, previous = fixture()
    previous.speaker_background = SpeakerBackgroundLayer(
        subject_track_id=previous.tracks[0].id,
        subject_clip_ids=[previous.tracks[0].clips[0].id],
        media_kind="color",
        color="#000000",
        speaker_frame=FRAME,
    )
    row = variant["user_timeline"]["slots"][0]
    row.update(
        clip_index=0,
        in_s=0,
        speaker_subject=True,
        speaker_frame_override={"mode": "full"},
        source_audio_volume=0.4,
    )
    if photo:
        _admit(variant, _photo_visual())
    return job, variant, previous


@pytest.mark.parametrize("mode", ["full", "custom"])
def test_original_video_replacement_keeps_explicit_subject_and_frame(monkeypatch, mode):
    from app.pipeline.phone_authored_timeline import compile_phone_authored_timeline
    from app.services.authored_editor import resolve_authored_phone_slots

    job, variant, previous = _replacement_fixture()
    monkeypatch.setattr(
        "app.services.phone_rollout.split_screen_client_supported", lambda **kw: True
    )
    monkeypatch.setattr("app.services.phone_rollout.split_screen_supported", lambda **kw: True)
    override = {"mode": mode, **({"frame": FRAME} if mode == "custom" else {})}
    row = {
        **variant["user_timeline"]["slots"][0],
        "clip_index": 1,
        "in_s": 2,
        "speaker_frame_override": override,
    }
    before = deepcopy(variant)
    resolved = resolve_authored_phone_slots(job, variant, [TimelineSlotEdit.model_validate(row)])
    assert variant == before
    assert resolved[0]["speaker_subject"] is True
    assert resolved[0]["speaker_frame_override"] == override
    assert resolved[0]["media_id"] == "new"
    assert resolved[0]["source_audio_volume"] == 0.4
    variant["user_timeline"]["slots"] = resolved
    recipe = compile_phone_authored_timeline(job, variant, previous)
    clip = recipe.tracks[0].clips[0]
    assert clip.source_asset_id == "new"
    assert clip.speaker_frame_override.mode == mode
    assert recipe.speaker_background.subject_clip_ids == [clip.id]


def test_photo_replacement_refuses_active_frame_then_accepts_inherit(monkeypatch):
    from app.pipeline.phone_authored_timeline import compile_phone_authored_timeline
    from app.services.authored_editor import resolve_authored_phone_slots

    job, variant, previous = _replacement_fixture(photo=True)
    monkeypatch.setattr(
        "app.services.phone_rollout.split_screen_client_supported", lambda **kw: True
    )
    monkeypatch.setattr("app.services.phone_rollout.split_screen_supported", lambda **kw: True)
    row = {
        **variant["user_timeline"]["slots"][0],
        "clip_index": 1,
        "in_s": 0,
        "speaker_subject": False,
    }
    variant["user_timeline"]["slots"] = resolve_authored_phone_slots(
        job, variant, [TimelineSlotEdit.model_validate(row)]
    )
    before = deepcopy(variant)
    with pytest.raises(ValueError, match="reset.*inherit"):
        compile_phone_authored_timeline(job, variant, previous)
    assert variant == before
    variant["user_timeline"]["slots"][0]["speaker_frame_override"] = {"mode": "inherit"}
    recipe = compile_phone_authored_timeline(job, variant, previous)
    assert recipe.tracks[0].clips[0].speaker_frame_override.mode == "inherit"
    from tests.pipeline.test_phone_subtitled_plan import _photo_visual

    clip = recipe.tracks[0].clips[0]
    assert clip.still_layout is None  # The recipe encodes only the supporting-card exception.
    assert clip.source_asset_id == _photo_visual().render_asset().id
    asset = next(
        asset for asset in recipe.asset_manifest.assets if asset.id == clip.source_asset_id
    )
    assert asset.kind == "visual"
    assert asset.media_kind == "image"
