"""Split sections preserve simultaneous source occurrences through wire boundaries."""

import pytest
from pydantic import ValidationError

from app.kria.split_screen import SplitScreenSection, layout_split_timeline


def section(**changes):
    return SplitScreenSection.model_validate(
        {"id": "s", "duration_s": 4, "panel_count": 2, "panel_weights": [1, 1], **changes}
    )


def member(panel, offset=0, duration=4):
    return {
        "slot_id": f"p{panel}",
        "duration_s": duration,
        "split_section_id": "s",
        "split_panel_index": panel,
        "split_offset_s": offset,
    }


def test_group_advances_once_and_gaps_keep_section_duration():
    starts, sections, total = layout_split_timeline(
        [{"duration_s": 2}, member(0), member(1, 0.5, 2), {"duration_s": 1}], [section()]
    )
    assert starts == [0, 2, 2.5, 6]
    assert (sections[0].start, sections[0].end, total) == (2, 6, 7)


@pytest.mark.parametrize(
    "rows",
    [
        [member(0), {"duration_s": 1}, member(1)],
        [member(2)],
        [member(0, -1)],
        [member(0, 1)],
        [member(0), member(0)],
        [{"duration_s": 1, "split_section_id": "s"}],
        [member(0, float("nan"))],
    ],
)
def test_invalid_membership_is_refused(rows):
    with pytest.raises(ValueError):
        layout_split_timeline(rows, [section()])


def test_unknown_and_unpositioned_sections_are_refused():
    with pytest.raises(ValueError):
        layout_split_timeline([member(0)], [])
    with pytest.raises(ValueError):
        layout_split_timeline([{"duration_s": 1}], [section()])


@pytest.mark.parametrize(
    "changes",
    [
        {"panel_weights": [1]},
        {"panel_weights": [1, 0]},
        {"duration_s": float("inf")},
        {"background_color": "red"},
        {"panel_count": 4},
    ],
)
def test_invalid_layout_is_refused(changes):
    with pytest.raises(ValidationError):
        section(**changes)


def test_save_wire_keeps_membership_and_sections():
    from app.routes.generative_jobs import EditorCommitRequest

    payload = EditorCommitRequest.model_validate(
        {
            "timeline_slots": [{**member(0), "clip_index": 0, "in_s": 1}],
            "split_screen_sections": [section().model_dump()],
        }
    )
    wire = payload.model_dump(exclude_none=True)
    assert wire["timeline_slots"][0]["split_section_id"] == "s"
    assert wire["split_screen_sections"][0]["panel_weights"] == [1, 1]


def test_shared_projection_contract():
    import json
    from pathlib import Path

    cases = json.loads(
        (Path(__file__).parents[1] / "fixtures/split_screen_timeline.json").read_text()
    )["cases"]
    for case in cases:
        starts, _, total = layout_split_timeline(case["slots"], case["split_screen_sections"])
        assert total == case["expected_duration_s"], case["name"]
        for slot, start, expected in zip(
            case["slots"], starts, case["expected_windows"], strict=True
        ):
            assert {
                "slot_id": slot["slot_id"],
                "start_s": start,
                "end_s": start + slot["duration_s"],
            } == expected


def test_recipe_keeps_panel_source_edit_and_trailing_gap():
    import json
    from pathlib import Path

    from app.kria.recipes_v2 import EditRecipeV2

    raw = json.loads((Path(__file__).parents[1] / "fixtures/kria_edit_recipe_v2.json").read_text())
    raw["split_screen_sections"] = [{"id": "s", "start": 0, "end": 4, "panel_weights": [1, 1]}]
    clip = raw["tracks"][0]["clips"][0]
    clip.update(
        split_screen={"section_id": "s", "panel_index": 1},
        source_crop={"x": 0.1, "y": 0.1, "width": 0.8, "height": 0.8},
        volume=0.3,
    )
    recipe = EditRecipeV2.model_validate(raw)
    assert recipe.duration == 4
    assert "splitScreen" in recipe.required_capabilities
    wire = recipe.model_dump(mode="json")
    assert wire["tracks"][0]["clips"][0]["source_crop"] == clip["source_crop"]
    assert EditRecipeV2.model_validate(wire).tracks[0].clips[0].volume == 0.3


def test_old_app_cannot_erase_sections(monkeypatch):
    from fastapi import HTTPException

    from app.routes.generative_jobs import EditorCommitRequest
    from app.services import split_screen_editor as editor

    monkeypatch.setattr(editor, "split_screen_client_supported", lambda: False)
    with pytest.raises(HTTPException) as error:
        editor.require_split_screen_commit(
            {"user_timeline": {"split_screen_sections": [section().model_dump()]}},
            EditorCommitRequest(text_elements=[]),
        )
    assert error.value.status_code == 426


def test_authored_save_reopens_with_independent_panel_source_clocks(monkeypatch):
    from app.config import settings
    from app.routes import generative_jobs as gj
    from app.services.device_render import device_status
    from tests.routes.test_authored_playback_speed import _authored_phone_job

    job, variant = _authored_phone_job(monkeypatch)
    monkeypatch.setattr(
        settings,
        "phone_render_verified_features",
        [*settings.phone_render_verified_features, "splitScreen", "sourceCrop"],
    )
    payload = gj.EditorCommitRequest(
        base_generation="first",
        editor_state_version=2,
        timeline_slots=[
            gj.TimelineSlotEdit(
                slot_id="11111111-1111-4111-8111-111111111111",
                clip_index=1,
                in_s=2,
                duration_s=3,
                source_end_s=8,
                playback_rate=2,
                split_section_id="s",
                split_panel_index=0,
                split_offset_s=0,
                source_crop={"x": 0.1, "y": 0.1, "width": 0.8, "height": 0.8},
            ),
            gj.TimelineSlotEdit(
                clip_index=1,
                in_s=7,
                duration_s=2,
                split_section_id="s",
                split_panel_index=1,
                split_offset_s=1,
                muted=True,
            ),
        ],
        split_screen_sections=[section()],
    )
    gj.prepare_editor_commit(job, "original_text", payload)
    saved = job.assembly_plan["variants"][0]
    assert saved["user_timeline"]["split_screen_sections"][0]["id"] == "s"
    recipe = device_status(job, "original_text").request.recipe
    panels = [clip for track in recipe.tracks for clip in track.clips if clip.split_screen]
    assert len(panels) == 2
    assert recipe.duration == 4
    assert [
        (clip.source_start, clip.timeline_start, clip.rate, clip.volume) for clip in panels
    ] == [(2, 0, 2, 1), (7, 1, 1, 0)]
    reopened = gj.dispatch_get_timeline(
        job, "original_text", sign_url=lambda *_: "https://example.test/media"
    )
    assert reopened["split_screen_sections"][0]["id"] == "s"
    assert reopened["total_duration_s"] == 4
    assert [row["output_start_s"] for row in reopened["slots"]] == [0, 1]


def test_panel_transition_uses_its_own_predecessor_and_serial_neighbors_overlap():
    first = {**member(0, 0, 2), "transition_after": "crossfade", "transition_duration_s": 0.2}
    second = member(0, 1.8, 2)
    starts, _, total = layout_split_timeline(
        [
            {"duration_s": 1, "transition_after": "crossfade", "transition_duration_s": 0.2},
            {"duration_s": 1},
            first,
            member(1),
            second,
        ],
        [section()],
    )
    assert starts == pytest.approx([0, 0.8, 1.8, 1.8, 3.6])
    assert total == pytest.approx(5.8)


def test_saved_draft_public_projection_keeps_atomic_timeline(monkeypatch):
    from types import SimpleNamespace

    from app.services.editor_empty_drafts import (
        public_saved_editor_variant,
        stage_saved_draft_baseline,
    )
    from app.services.kria_editor_ops import build_editor_snapshot
    from tests.routes.test_authored_playback_speed import _authored_phone_job

    job, variant = _authored_phone_job(monkeypatch)
    variant["editor_state_version"] = 2
    row = {**member(0), "clip_index": 1, "in_s": 2}
    sections = {"timeline_slots": [row], "split_screen_sections": [section().model_dump()]}
    snapshot = {"editor_payload": {"sections": sections}}
    draft = SimpleNamespace(snapshot=snapshot, model_dump=lambda **_: snapshot)
    job._saved_editor_drafts = {"original_text": draft}
    public = public_saved_editor_variant(job, variant)
    assert public["user_timeline"]["split_screen_sections"] == sections["split_screen_sections"]
    assert (
        build_editor_snapshot(job, public)["split_screen_sections"]
        == sections["split_screen_sections"]
    )
    # stage_saved_draft_baseline loads the same attached revision via the ordinary accessor.
    import app.services.editor_empty_drafts as drafts

    monkeypatch.setattr(drafts, "saved_editor_draft", lambda *_: draft)
    staged = stage_saved_draft_baseline(
        job, "original_text", SimpleNamespace(editor_state_version=2, base_generation="first")
    )
    assert (
        staged.assembly_plan["variants"][0]["user_timeline"]["split_screen_sections"]
        == sections["split_screen_sections"]
    )


def test_initial_creation_uses_the_same_editable_section_contract(monkeypatch):
    from app.config import settings
    from app.pipeline.phone_initial_split_screen import compile_initial_split_screen
    from app.services.device_render import device_status
    from tests.routes.test_authored_playback_speed import _authored_phone_job

    job, variant = _authored_phone_job(monkeypatch)
    monkeypatch.setattr(
        settings,
        "phone_render_verified_features",
        [*settings.phone_render_verified_features, "splitScreen"],
    )
    job.all_candidates["creator_strategy"] = {
        "split_screen": {
            "panels": [
                {"media_id": "old", "source_start_s": 1},
                {"media_id": "new", "source_start_s": 5},
            ],
            "duration_s": 3,
        }
    }
    result = compile_initial_split_screen(job, device_status(job, "original_text").request)
    assert result.recipe.duration == 3
    assert len(result.recipe.split_screen_sections) == 1
    clips = [clip for track in result.recipe.tracks for clip in track.clips if clip.split_screen]
    assert [clip.source_start for clip in clips] == [1, 5]
    saved = job.assembly_plan["variants"][0]["user_timeline"]
    assert len(saved["slots"]) == 2
    assert saved["split_screen_sections"][0]["panel_count"] == 2


def test_panel_freeze_keeps_held_frame_and_audio_source_window():
    from app.pipeline.phone_authored_timeline import compile_phone_authored_timeline
    from app.routes.generative_jobs import TimelineSlotEdit
    from app.services.authored_editor import resolve_authored_phone_slots
    from tests.pipeline.test_authored_timeline import fixture

    job, variant, previous = fixture()
    variant["user_timeline"]["split_screen_sections"] = [section().model_dump()]
    slot = TimelineSlotEdit(
        slot_id="added",
        clip_index=1,
        in_s=2,
        duration_s=4,
        freeze_frame_s=3,
        freeze_hold_s=3,
        split_section_id="s",
        split_panel_index=0,
        split_offset_s=0,
    )
    variant["user_timeline"]["slots"] = resolve_authored_phone_slots(job, variant, [slot])
    clip = compile_phone_authored_timeline(job, variant, previous).tracks[0].clips[0]
    assert (clip.source_start, clip.source_duration, clip.hold_duration) == (2, 1, 3)


@pytest.mark.parametrize("processed", [False, True])
def test_initial_bridge_preserves_volume_position_and_source_transform(monkeypatch, processed):
    from app.config import settings
    from app.kria.device_render import make_device_request
    from app.kria.recipes import MediaTransform
    from app.pipeline.phone_initial_split_screen import compile_initial_split_screen
    from app.services.device_render import device_status
    from tests.routes.test_authored_playback_speed import _authored_phone_job

    job, _ = _authored_phone_job(monkeypatch)
    monkeypatch.setattr(
        settings,
        "phone_render_verified_features",
        [*settings.phone_render_verified_features, "splitScreen"],
    )
    request = device_status(job, "original_text").request
    recipe = request.recipe.model_copy(deep=True)
    clip = next(track for track in recipe.tracks if track.kind == "video").clips[0]
    clip.volume = 0.4
    clip.transform = MediaTransform(scale=0.8, position_x=12, position_y=-9)
    clip.audio_fade_in = 0.2
    if processed:
        from app.pipeline.phone_processed_audio import (
            PHONE_PROCESSED_AUDIO_FIELD,
            ProcessedAudioReceipt,
            replace_audio_with_processed_segments,
        )

        receipt = ProcessedAudioReceipt(
            job_id=str(job.id),
            variant_id="original_text",
            render_generation="first",
            source_media_id="old",
            source_generation="1",
            path="test/audio.m4a",
            generation="1",
            fingerprint={"sha256": "c" * 64, "byte_count": 100},
            duration_s=12,
        )
        job.assembly_plan[PHONE_PROCESSED_AUDIO_FIELD] = {
            "original_text": {"old": receipt.model_dump(mode="json")}
        }
        recipe = replace_audio_with_processed_segments(recipe, receipts_by_source={"old": receipt})
        monkeypatch.setattr(
            settings,
            "phone_render_verified_features",
            [*settings.phone_render_verified_features, "processedAudio"],
        )
    job.all_candidates["creator_strategy"] = {
        "split_screen": {"panels": [{"media_id": "old"}, {"media_id": "new"}], "duration_s": 3}
    }
    request = make_device_request(
        job_id=job.id, variant_id="original_text", revision=1, recipe=recipe
    )
    result = compile_initial_split_screen(job, request)
    top = next(
        clip
        for track in result.recipe.tracks
        for clip in track.clips
        if clip.split_screen and clip.split_screen.panel_index == 0
    )
    assert top.volume == (0 if processed else 0.4)
    if processed:
        derivative = next(
            t for t in result.recipe.tracks if t.id == "processed-source-audio"
        ).clips[0]
        assert derivative.volume == 0.4
        assert derivative.audio_fade_in == 0.2
    assert top.audio_fade_in == 0.2
    assert top.transform == clip.transform


def test_drawing_and_single_subject_background_survive_split_compile():
    from app.kria.portable_speaker_background import SpeakerBackgroundLayer
    from app.pipeline.phone_authored_timeline import compile_phone_authored_timeline
    from app.pipeline.phone_recipe_shared import with_drawings
    from tests.pipeline.test_authored_timeline import fixture

    job, variant, previous = fixture()
    previous = with_drawings(
        previous,
        [
            {
                "id": "ink",
                "start_s": 0,
                "end_s": 3,
                "canvas_aspect": 0.5625,
                "animation": {"entrance": "draw", "draw_duration_s": 1, "exit": "none"},
                "strokes": [
                    {"color": "#FF0000", "width_frac": 0.01, "points": [[0.1, 0.1], [0.4, 0.4]]}
                ],
            }
        ],
    )
    previous.speaker_background = SpeakerBackgroundLayer(
        media_kind="color", color="#112233", subject_track_id=previous.tracks[0].id
    )
    variant["user_timeline"] = {
        "slots": [{**member(0), "clip_index": 0, "in_s": 0}],
        "split_screen_sections": [section().model_dump()],
    }
    recipe = compile_phone_authored_timeline(job, variant, previous)
    assert recipe.drawing_layers == previous.drawing_layers
    assert recipe.speaker_background.color == "#112233"
    assert recipe.speaker_background.subject_clip_ids == [recipe.tracks[0].clips[0].id]
    variant.update(drawings=[], speaker_background=None)
    cleared = compile_phone_authored_timeline(job, variant, previous)
    assert cleared.drawing_layers == []
    assert cleared.speaker_background is None


@pytest.mark.parametrize(
    "protocol,verified,device,expected",
    [
        (43, True, True, True),
        (42, True, True, False),
        (43, False, True, False),
        (43, True, False, False),
    ],
)
def test_editor_split_capability_is_explicit_and_qualified(
    monkeypatch, protocol, verified, device, expected
):
    from app.config import settings
    from app.routes import generative_jobs as gj
    from app.services.client_protocol import clear_request_context, set_client_protocol
    from tests.routes.test_authored_playback_speed import _authored_phone_job

    job, variant = _authored_phone_job(monkeypatch)
    if verified:
        monkeypatch.setattr(
            settings,
            "phone_render_verified_features",
            [*settings.phone_render_verified_features, "splitScreen"],
        )
    if not device:
        variant["render_destination"] = "cloud"
    set_client_protocol(protocol)
    try:
        assert (gj._editor_capabilities(job, variant).get("split_screen") is True) == expected
    finally:
        clear_request_context()


def test_source_label_clock_moves_before_freeze_and_preserves_hold_offset():
    from app.services.guided_timeline import make_time_projector

    old = {
        "segment_id": "a",
        "source_start_s": 2,
        "source_end_s": 4,
        "output_start_s": 0,
        "output_end_s": 4,
        "duration_s": 4,
        "freeze_hold_s": 2,
    }
    new = {**old, "output_end_s": 3, "duration_s": 3}
    project = make_time_projector([old], [new])
    assert project(1) == (0.5, "a")
    assert project(3) == (2, "a")
    assert project(4) == (3, "a")


@pytest.mark.parametrize("clear_sections", [False, True])
def test_new_ordinary_uuid_occurrence_survives_after_unstack(clear_sections):
    from app.routes.generative_jobs import TimelineSlotEdit
    from app.services.authored_editor import resolve_authored_phone_slots
    from tests.pipeline.test_authored_timeline import fixture

    job, variant, _ = fixture()
    if clear_sections:
        variant["user_timeline"]["split_screen_sections"] = []
    key = "22222222-2222-4222-8222-222222222222"
    rows = resolve_authored_phone_slots(
        job,
        variant,
        [
            TimelineSlotEdit(
                slot_id=key,
                parent_segment_id="added",
                clip_index=1,
                in_s=3,
                duration_s=1,
            )
        ],
    )
    assert rows[0]["slot_id"] == key
    assert rows[0]["in_s"] == 3


@pytest.mark.parametrize(
    "edit", [{"volume": 0.35, "window_start_s": 2, "window_end_s": 7}, {"removed": True}]
)
def test_authored_split_creator_song_controls_keep_receipt(monkeypatch, edit):
    from app.config import settings
    from app.pipeline.phone_initial_split_screen import compile_initial_split_screen
    from app.routes import generative_jobs as gj
    from app.services.device_render import device_status, pin_device_request
    from app.services.user_song_projection import user_song_for_variant
    from tests._prod_profile import PROD_VERIFIED_FEATURES
    from tests.routes.test_phone_song_editor_commit import background_job

    monkeypatch.setattr(settings, "phone_rendering_enabled", True)
    monkeypatch.setattr(settings, "guided_story_editor_v2_enabled", True)
    monkeypatch.setattr(
        settings, "phone_render_verified_features", [*PROD_VERIFIED_FEATURES, "splitScreen"]
    )
    job, _ = background_job()
    job.all_candidates["creator_strategy"] = {
        "split_screen": {
            "panels": [{"media_id": "c1"}, {"media_id": "c2"}],
            "duration_s": 4,
        }
    }
    request = compile_initial_split_screen(job, device_status(job, "guided_story").request)
    from app.kria.device_render import make_device_request

    request = make_device_request(
        job_id=job.id, variant_id="guided_story", revision=2, recipe=request.recipe
    )
    pin_device_request(job, request, base_generation="first")
    variant = job.assembly_plan["variants"][0]
    gj.prepare_editor_commit(
        job,
        "guided_story",
        gj.EditorCommitRequest(
            base_generation="first",
            editor_state_version=2,
            user_song=gj.EditorCommitUserSong(**edit),
        ),
    )
    variant = job.assembly_plan["variants"][0]
    song = user_song_for_variant(job, variant, song_filename=None)
    recipe = device_status(job, "guided_story").request.recipe
    song_assets = {a.id for a in recipe.asset_manifest.assets if a.kind == "song"}
    song_clips = [c for t in recipe.tracks for c in t.clips if c.source_asset_id in song_assets]
    if edit.get("removed"):
        assert song is None
        assert song_clips == []
        assert recipe.audio.original_volume > 0
    else:
        assert song["volume"] == 0.35
        assert song["window_start_s"] == 2
        assert song_clips[0].volume == 0.35
        assert song_clips[0].source_start == 2
        assert song_clips[0].source_duration == 4


def test_source_segment_uses_only_moving_window_before_freeze():
    from app.services.authored_text_fit import _segments

    row = {"slot_id": "a", "in_s": 2, "duration_s": 4, "freeze_hold_s": 2, "freeze_frame_s": 4}
    assert _segments([row])[0]["source_end_s"] == 4
    panel = {
        **row,
        "freeze_hold_s": 0,
        "duration_s": 1,
        "playback_rate": 2,
        "source_end_s": 9,
        "split_section_id": "s",
    }
    assert _segments([panel])[0]["source_end_s"] == 4


def test_speech_canvas_basis_uses_section_clock_once():
    from app.pipeline.speech_cut_state import (
        canvas_speech_slots,
        project_speech_basis,
        sync_speech_basis,
    )

    rows = [
        {**member(0, duration=2), "clip_index": 0, "in_s": 0},
        {**member(1), "clip_index": 1, "in_s": 0},
    ]
    layout = [section().model_dump()]
    canvas = canvas_speech_slots(rows, layout)
    records = [
        {"id": "ink", "start_s": 1, "end_s": 1.5},
        {"id": "overlay", "start_s": 3, "end_s": 3.5},
    ]
    entries = sync_speech_basis(None, records, rows, captions=False, canvas_slots=canvas)
    trimmed = [{**r, "duration_s": 2} for r in rows]
    assert (
        project_speech_basis(
            entries, trimmed, captions=False, canvas_slots=canvas_speech_slots(trimmed, layout)
        )
        == records
    )
    assert project_speech_basis(entries, rows, captions=False, canvas_slots=canvas) == records
    shorter = [section(duration_s=3).model_dump()]
    assert (
        project_speech_basis(
            entries, trimmed, captions=False, canvas_slots=canvas_speech_slots(trimmed, shorter)
        )
        == records[:1]
    )


@pytest.mark.parametrize("change", ["weights", "panel_trim", "section_trim"])
def test_real_save_keeps_canvas_lanes_independent_of_panel_coverage(monkeypatch, change):
    from app.config import settings
    from app.routes import generative_jobs as gj
    from tests.routes.test_authored_playback_speed import _authored_phone_job

    job, variant = _authored_phone_job(monkeypatch)
    monkeypatch.setattr(
        settings,
        "phone_render_verified_features",
        [*settings.phone_render_verified_features, "splitScreen", "drawings"],
    )
    rows = [
        {
            **member(0, duration=2),
            "slot_id": "native-phone-talking-source-0",
            "clip_index": 0,
            "in_s": 0,
        },
        {**member(1), "slot_id": "native-phone-talking-source-1", "clip_index": 1, "in_s": 0},
    ]
    variant["user_timeline"] = {"slots": rows, "split_screen_sections": [section().model_dump()]}
    variant["text_elements"] = [
        {"id": "canvas-label", "text": "Canvas gap", "start_s": 3, "end_s": 3.5}
    ]
    variant["drawings"] = [
        {
            "id": "ink",
            "start_s": 1,
            "end_s": 1.5,
            "strokes": [{"color": "#FFFFFF", "width": 0.01, "points": [[0.2, 0.2], [0.4, 0.4]]}],
        }
    ]
    if change in ("panel_trim", "section_trim"):
        rows = [{**r, "duration_s": 2} for r in rows]
    layout = section(duration_s=3) if change == "section_trim" else section(panel_weights=[2, 1])
    gj.prepare_editor_commit(
        job,
        "original_text",
        gj.EditorCommitRequest(
            base_generation="first",
            editor_state_version=2,
            timeline_slots=[gj.TimelineSlotEdit.model_validate(r) for r in rows],
            split_screen_sections=[layout],
        ),
    )
    saved = job.assembly_plan["variants"][0]
    assert [(d["id"], d["start_s"], d["end_s"]) for d in saved["drawings"]] == [("ink", 1, 1.5)]
    if change != "section_trim":
        assert [(t["start_s"], t["end_s"]) for t in saved["text_elements"]] == [(3, 3.5)]
    else:
        assert saved["text_elements"] == []


@pytest.mark.parametrize("nested", [False, True])
def test_speech_basis_explicit_panel_owner_does_not_duplicate(nested):
    from app.pipeline.speech_cut_state import (
        canvas_speech_slots,
        project_speech_basis,
        sync_speech_basis,
    )

    rows = [
        {**member(0), "clip_index": 0, "in_s": 0, "_speech_output_start_s": 0},
        {**member(1), "clip_index": 1, "in_s": 0, "_speech_output_start_s": 0},
    ]
    owner = {"source_params": {"segment_id": "p1"}} if nested else {"segment_id": "p1"}
    record = {"id": "panel-label", "start_s": 1, "end_s": 1.5, **owner}
    canvas = canvas_speech_slots(rows, [section().model_dump()])
    entries = sync_speech_basis(None, [record], rows, captions=False, canvas_slots=canvas)
    assert [r["slot_id"] for r in entries[0]["slots"]] == ["p1"]
    assert project_speech_basis(entries, rows, captions=False, canvas_slots=canvas) == [record]


def test_canvas_basis_crossing_section_boundary_stays_one_object_and_ripples():
    from app.pipeline.speech_cut_state import canvas_speech_slots, project_speech_basis, sync_speech_basis
    rows = [{**member(0, duration=2), 'clip_index': 0, 'in_s': 0}, {**member(1, duration=2), 'clip_index': 1, 'in_s': 0}, {'slot_id': 'after', 'clip_index': 0, 'in_s': 4, 'duration_s': 2}]
    record = {'id': 'ink', 'start_s': 3, 'end_s': 5}
    old = canvas_speech_slots(rows, [section().model_dump()])
    entries = sync_speech_basis(None, [record], rows, captions=False, canvas_slots=old)
    assert project_speech_basis(entries, rows, captions=False, canvas_slots=old) == [record]
    new = canvas_speech_slots(rows, [section(duration_s=3).model_dump()])
    assert project_speech_basis(entries, rows, captions=False, canvas_slots=new) == [{**record, 'start_s': 3, 'end_s': 4}]
