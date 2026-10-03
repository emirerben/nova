import uuid
from types import SimpleNamespace

import pytest
from fastapi import HTTPException

from app.kria.device_render import make_device_request
from app.pipeline.phone_subtitled_lanes import PhoneSubtitledLanes
from app.pipeline.phone_subtitled_plan import (
    CUTAWAY_TRACK_ID,
    SFX_DUCK_RECEIPT_FIELD,
    SFX_SPEECH_DUCK_GAIN,
    PhoneCutaway,
    compile_phone_subtitled_plan,
    sfx_duck_receipt,
)
from app.routes import generative_jobs as gj
from app.services.device_render import device_status, pin_device_request
from app.services.phone_editor import prepare_phone_editor_commit
from app.services.phone_sources import PHONE_SOURCES_FIELD, PHONE_VISUALS_FIELD
from app.services.phone_subtitled_editor import (
    PHONE_SUBTITLED_EDITOR_LANES_FIELD,
    project_phone_subtitled_editor_sections,
)
from tests.pipeline.test_phone_subtitled_plan import (
    _CUES,
    PHOTO_PATH,
    _binding,
    _overlay_card,
    _photo_visual,
    _resolved_sfx,
)

_VERIFIED_FEATURES = [
    "basicComposition",
    "local1080Export",
    "positionedText",
    "animatedText",
    "audioMix",
    "soundEffects",
    "visualBlocks",
    "alphaOverlay",
    "stillImages",
    "visualVideos",
]


def _enable_subtitled_editor(monkeypatch, *, editor_flag: bool = True) -> None:
    # `phone_rendering_enabled` + `sound_effects_enabled`/`media_overlays_enabled`
    # are prerequisites this feature doesn't own -- always on here so a test
    # that only flips `editor_flag` off exercises THAT flag specifically,
    # not an unrelated one.
    monkeypatch.setattr(gj.settings, "phone_rendering_enabled", True)
    monkeypatch.setattr(gj.settings, "sound_effects_enabled", True)
    monkeypatch.setattr(gj.settings, "media_overlays_enabled", True)
    monkeypatch.setattr(gj.settings, "phone_subtitled_editor_lanes_enabled", editor_flag)
    monkeypatch.setattr(gj.settings, "phone_subtitled_media_lanes_enabled", editor_flag)
    monkeypatch.setattr(gj.settings, "phone_render_verified_features", list(_VERIFIED_FEATURES))


def phone_job(monkeypatch, *, enable=True, duck=False):
    """A phone-rendered `subtitled` variant with one pinned overlay card and
    one pinned sound effect, mirroring `tests.routes.test_phone_editor_commit
    .phone_job`'s pattern for the guided_story archetype. ``duck=True`` pins
    the recipe the runner compiles with the SFX speech duck on (the effect
    at 1.0 s lands on speech), plus its duck receipt."""
    _enable_subtitled_editor(monkeypatch, editor_flag=enable)
    monkeypatch.setattr(gj.settings, "phone_sfx_speech_duck_enabled", duck)
    bindings = (_binding(duration_s=10.0),)
    photo = _photo_visual()
    card = _overlay_card(id="card-1")
    sfx = _resolved_sfx()
    lanes = PhoneSubtitledLanes(overlays=[card], sound_effects=[sfx])
    recipe = compile_phone_subtitled_plan(
        bindings, caption_cues=_CUES, visuals=(photo,), lanes=lanes, duck_sfx_under_speech=duck
    )
    duck_receipt = sfx_duck_receipt(lanes, recipe)
    job = SimpleNamespace(
        id=uuid.uuid4(),
        user_id=uuid.uuid4(),
        status="awaiting_device",
        current_phase=None,
        assembly_plan={
            PHONE_SOURCES_FIELD: [b.model_dump(mode="json") for b in bindings],
            PHONE_VISUALS_FIELD: [photo.model_dump(mode="json")],
            "variants": [
                {
                    "variant_id": "subtitled",
                    "resolved_archetype": "subtitled",
                    "render_destination": "device",
                    "render_status": "awaiting_device",
                    "render_generation_id": "first",
                    "duration_s": recipe.duration,
                    "caption_cues": _CUES,
                    "voiceover_caption_style": "sentence",
                    **({SFX_DUCK_RECEIPT_FIELD: duck_receipt} if duck_receipt else {}),
                }
            ],
        },
    )
    pin_device_request(
        job,
        make_device_request(job_id=job.id, variant_id="subtitled", revision=1, recipe=recipe),
        base_generation="first",
    )
    return job


def _sfx_payload(*, effect_id="sfx-1", catalog_id="pop", at_s=1.0, gain=1.0, **extra):
    return {
        "id": effect_id,
        "sound_effect_id": catalog_id,
        "src_gcs_path": f"sound-effects/{catalog_id}/{catalog_id}.m4a",
        "at_s": at_s,
        "gain": gain,
        "duration_s": 1.0,
        **extra,
    }


def _overlay_payload(*, card_id="card-1", src_gcs_path=PHOTO_PATH, x_frac=0.5, **extra):
    return {
        "id": card_id,
        "kind": "image",
        "src_gcs_path": src_gcs_path,
        "display_mode": "pip",
        "x_frac": x_frac,
        "y_frac": 0.4,
        "scale": 0.35,
        "start_s": 1.0,
        "end_s": 3.0,
        **extra,
    }


def save(job, **section_overrides):
    return gj.prepare_editor_commit(
        job,
        "subtitled",
        gj.EditorCommitRequest(base_generation="first", **section_overrides),
        user_id="owner",
        plan_item_id="item",
    )


# --- flag gating ----------------------------------------------------------------


def test_flag_off_still_422s_unsupported_phone_edit(monkeypatch):
    job = phone_job(monkeypatch, enable=False)
    with pytest.raises(HTTPException) as error:
        save(job, sound_effects=[_sfx_payload(at_s=4.0)])
    assert error.value.status_code == 422
    assert error.value.detail["code"] == "unsupported_phone_edit"


# --- happy path -------------------------------------------------------------------


def test_flag_on_save_pins_revision_two_with_no_cloud_task(monkeypatch):
    job = phone_job(monkeypatch)
    old = device_status(job, "subtitled").request
    prep = save(job, sound_effects=[_sfx_payload(at_s=4.0)])

    assert prep["render_destination"] == "device"
    assert prep["render_task_id"] is None
    new = device_status(job, "subtitled").request
    assert new.identity.recipe_revision == old.identity.recipe_revision + 1

    variant = job.assembly_plan["variants"][0]
    assert variant["render_status"] == "awaiting_device"
    assert PHONE_SUBTITLED_EDITOR_LANES_FIELD in variant


def test_media_overlay_move_round_trips_into_the_recipe_overlay_clip(monkeypatch):
    job = phone_job(monkeypatch)
    moved = _overlay_payload(x_frac=0.9)
    save(job, media_overlays=[moved])

    request = device_status(job, "subtitled").request
    overlay_track = next(t for t in request.recipe.tracks if t.id == "subtitled-overlays")
    assert len(overlay_track.clips) == 1
    assert overlay_track.clips[0].visual_placement.x_fraction == pytest.approx(0.9)

    variant = job.assembly_plan["variants"][0]
    assert variant["media_overlays"][0]["x_frac"] == pytest.approx(0.9)


def test_sound_delete_removes_the_sfx_clip(monkeypatch):
    job = phone_job(monkeypatch)
    save(job, sound_effects=[])

    request = device_status(job, "subtitled").request
    assert not any(t.id == "sfx" for t in request.recipe.tracks)
    variant = job.assembly_plan["variants"][0]
    assert variant["sound_effects"] is None


def test_added_catalog_effect_lands_on_the_sfx_track(monkeypatch):
    job = phone_job(monkeypatch)
    new_effect = _sfx_payload(effect_id="sfx-new", catalog_id="ding", at_s=6.0)

    from app.services import phone_subtitled_editor as pse

    def fake_inspect(path, *, asset_id, catalog, catalog_id):
        from tests.pipeline.test_phone_subtitled_plan import _sfx_asset

        return _sfx_asset(catalog_id, generation="42")

    monkeypatch.setattr(pse, "inspect_library_asset", fake_inspect)

    save(job, sound_effects=[_sfx_payload(at_s=1.0), new_effect])

    request = device_status(job, "subtitled").request
    sfx_track = next(t for t in request.recipe.tracks if t.id == "sfx")
    assert len(sfx_track.clips) == 2
    assert {c.id for c in sfx_track.clips} == {"sfx-sfx-1", "sfx-sfx-new"}


def test_untouched_section_carries_forward_when_only_the_other_is_committed(monkeypatch):
    job = phone_job(monkeypatch)
    save(job, sound_effects=[_sfx_payload(at_s=4.0)])

    request = device_status(job, "subtitled").request
    overlay_track = next(t for t in request.recipe.tracks if t.id == "subtitled-overlays")
    assert len(overlay_track.clips) == 1  # the pinned card carried forward untouched


# --- named-reason 422s -------------------------------------------------------------


def test_text_elements_section_is_a_named_422_reason(monkeypatch):
    """Mirrors `test_phone_editor_commit.test_unsupported_phone_edit_names_
    its_reason` -- a hand-built `prep` dict marking `text_elements` active
    bypasses `_prepare_editor_commit`'s own (differently-shaped) generic
    caption-archetype rejection, isolating just this branch's own
    unsupported-section check and its exact reason text."""
    job = phone_job(monkeypatch)
    with pytest.raises(HTTPException) as error:
        prepare_phone_editor_commit(
            job,
            "subtitled",
            prepare=lambda staged: {
                "has_render_section": True,
                "sections": {"text_elements": True},
                "sfx_override": None,
                "media_overlays_override": None,
                "caption_cues_override": None,
                "generation": "second",
            },
        )
    assert error.value.status_code == 422
    assert error.value.detail["code"] == "unsupported_phone_edit"
    assert "text_elements" in error.value.detail["reason"]


def test_unpinned_overlay_path_is_a_named_422_reason(monkeypatch):
    job = phone_job(monkeypatch)
    bad = _overlay_payload(card_id="card-bad", src_gcs_path="users/owner/plan/item/pool/nope.jpg")
    with pytest.raises(HTTPException) as error:
        save(job, media_overlays=[bad])
    assert error.value.status_code == 422
    assert error.value.detail["code"] == "unsupported_phone_edit"
    assert "isn't a photo Kria pinned" in error.value.detail["reason"]


def test_failed_compile_leaves_the_baseline_untouched(monkeypatch):
    job = phone_job(monkeypatch)
    baseline = device_status(job, "subtitled").request
    baseline_variant = dict(job.assembly_plan["variants"][0])

    with pytest.raises(HTTPException):
        save(
            job,
            media_overlays=[
                _overlay_payload(
                    card_id="card-bad", src_gcs_path="users/owner/plan/item/pool/nope.jpg"
                )
            ],
        )

    assert device_status(job, "subtitled").request == baseline
    assert job.assembly_plan["variants"][0] == baseline_variant


def test_unsupported_phone_edit_names_its_reason_via_synthetic_prep(monkeypatch):
    """Mirrors `test_phone_editor_commit.test_unsupported_phone_edit_names_
    its_reason` -- construct a `prep` dict by hand rather than routing
    through the full `_prepare_editor_commit`."""
    job = phone_job(monkeypatch, enable=False)
    with pytest.raises(HTTPException) as error:
        prepare_phone_editor_commit(
            job,
            "subtitled",
            prepare=lambda staged: {
                "has_render_section": True,
                "sections": {"sound_effects": True},
                "sfx_override": [_sfx_payload()],
                "media_overlays_override": None,
                "caption_cues_override": None,
                "generation": "second",
            },
        )
    assert error.value.status_code == 422
    assert error.value.detail["code"] == "unsupported_phone_edit"
    assert error.value.detail["reason"] == "ValueError: phone Talking edits aren't editable yet"


# --- SFX speech duck (KRI-181 follow-up) -------------------------------------
# `_CUES` speaks over [0.0, 3.0]; the pinned effect at 1.0 s lands on speech.


def _sfx_volumes(job) -> dict[str, float]:
    recipe = device_status(job, "subtitled").request.recipe
    return {c.id: c.volume for t in recipe.tracks if t.id == "sfx" for c in t.clips}


def test_ducked_pin_projects_the_creators_volume_not_the_ducked_one(monkeypatch):
    job = phone_job(monkeypatch, duck=True)
    assert _sfx_volumes(job) == {"sfx-sfx-1": pytest.approx(SFX_SPEECH_DUCK_GAIN)}
    sections = project_phone_subtitled_editor_sections(
        job.assembly_plan, job.assembly_plan["variants"][0]
    )
    assert sections["sound_effects"][0]["gain"] == pytest.approx(1.0)


# --- captions (KRI-216) -------------------------------------------------------


def test_caption_fields_422_when_rollout_flag_is_off(monkeypatch):
    job = phone_job(monkeypatch, enable=False)
    with pytest.raises(HTTPException) as error:
        save(job, caption_cues=[{"text": "x", "start_s": 0.0, "end_s": 1.0}])
    assert error.value.status_code == 422

    job2 = phone_job(monkeypatch, enable=False)
    with pytest.raises(HTTPException) as error2:
        save(job2, caption_meta=gj.EditorCommitCaptionMeta(enabled=False))
    assert error2.value.status_code == 422


def test_caption_cues_save_updates_text_and_bumps_revision(monkeypatch):
    job = phone_job(monkeypatch)
    old = device_status(job, "subtitled").request
    new_cues = [
        {"text": "Updated caption text", "start_s": 0.0, "end_s": 1.5},
        {"text": "welcome back", "start_s": 1.5, "end_s": 3.0},
    ]

    prep = save(job, caption_cues=new_cues)

    assert prep["render_destination"] == "device"
    new = device_status(job, "subtitled").request
    assert new.identity.recipe_revision == old.identity.recipe_revision + 1

    variant = job.assembly_plan["variants"][0]
    assert variant["render_status"] == "awaiting_device"
    assert variant["caption_cues"] == new_cues

    layer_texts = [run.text for layer in new.recipe.text_layers for run in layer.runs]
    assert "Updated caption text" in layer_texts


def test_caption_meta_save_applies_every_field_to_the_compiled_recipe(monkeypatch):
    job = phone_job(monkeypatch)

    prep = save(
        job,
        caption_meta=gj.EditorCommitCaptionMeta(
            style="word",
            font="Montserrat Bold",
            font_set=True,
            y_frac=0.5,
            size_px=96,
            color="#112233",
            highlight_color="#A3E635",
            stroke_width=7,
            shadow_enabled=False,
            appearance=gj.EditorCaptionAppearance(alignment="left"),
        ),
    )
    assert prep["render_destination"] == "device"

    variant = job.assembly_plan["variants"][0]
    assert variant["voiceover_caption_font"] == "Montserrat Bold"
    assert variant["caption_size_px"] == 96
    assert variant["caption_text_color"] == "#112233"
    assert variant["caption_highlight_color"] == "#A3E635"
    assert variant["caption_stroke_width"] == 7
    assert variant["caption_shadow_enabled"] is False
    assert variant["voiceover_caption_style"] == "word"
    assert variant["caption_editor_style"]["alignment"] == "left"

    request = device_status(job, "subtitled").request
    layers = request.recipe.text_layers
    assert layers, "expected caption layers to compile"
    run = layers[0].runs[0]
    assert run.font_asset_id == "font-Montserrat-Bold.ttf"
    assert run.font_size == pytest.approx(96)
    assert run.fill.red == pytest.approx(0x11 / 255)
    assert run.fill.green == pytest.approx(0x22 / 255)
    assert run.fill.blue == pytest.approx(0x33 / 255)
    assert run.stroke_width == pytest.approx(14)  # outline_px(7) * 2
    assert run.blur_layers == []  # shadow_enabled False, no competing override
    assert layers[0].anchor_x == pytest.approx(80.0)  # alignment=left safe margin


def test_caption_meta_enabled_false_compiles_no_caption_layers(monkeypatch):
    job = phone_job(monkeypatch)

    save(job, caption_meta=gj.EditorCommitCaptionMeta(enabled=False))

    request = device_status(job, "subtitled").request
    assert request.recipe.text_layers == []
    variant = job.assembly_plan["variants"][0]
    assert variant["captions_enabled"] is False


def _cut_phone_job(monkeypatch, *, enable=True):
    """A phone `subtitled` variant already pinned with a speech-cleanup cut
    (two kept segments, [0, 4) and [5, 10) of a 10s clip) -- mirrors
    `phone_job`'s pattern but exercises `compile_phone_subtitled_plan`'s
    `cut_plan` param so the pinned recipe's main track carries TWO
    `TimelineClip`s instead of one."""
    from app.pipeline.silence_cut import CutPlan, Removal

    _enable_subtitled_editor(monkeypatch, editor_flag=enable)
    monkeypatch.setattr(gj.settings, "phone_sfx_speech_duck_enabled", False)
    bindings = (_binding(duration_s=10.0),)
    cut_plan = CutPlan(
        keep_segments=[(0.0, 4.0), (5.0, 10.0)],
        removed=[Removal(start_s=4.0, end_s=5.0, reason="test")],
        time_saved_s=1.0,
    )
    cues = [{"text": "Hello everyone", "start_s": 0.0, "end_s": 1.5}]
    recipe = compile_phone_subtitled_plan(bindings, caption_cues=cues, cut_plan=cut_plan)
    job = SimpleNamespace(
        id=uuid.uuid4(),
        user_id=uuid.uuid4(),
        status="awaiting_device",
        current_phase=None,
        assembly_plan={
            PHONE_SOURCES_FIELD: [b.model_dump(mode="json") for b in bindings],
            PHONE_VISUALS_FIELD: [],
            "variants": [
                {
                    "variant_id": "subtitled",
                    "resolved_archetype": "subtitled",
                    "render_destination": "device",
                    "render_status": "awaiting_device",
                    "render_generation_id": "first",
                    "duration_s": recipe.duration,
                    "caption_cues": cues,
                    "voiceover_caption_style": "sentence",
                }
            ],
        },
    )
    pin_device_request(
        job,
        make_device_request(job_id=job.id, variant_id="subtitled", revision=1, recipe=recipe),
        base_generation="first",
    )
    return job


def _letterboxed_phone_job(monkeypatch, *, cut: bool):
    """A pinned landscape (1920x1080) `subtitled` variant compiled with
    ``landscape_fit="fit"`` (KRI-283), optionally with a speech-cleanup cut."""
    from app.pipeline.silence_cut import CutPlan, Removal

    _enable_subtitled_editor(monkeypatch)
    monkeypatch.setattr(gj.settings, "phone_sfx_speech_duck_enabled", False)
    bindings = (_binding(duration_s=10.0, width=1920, height=1080),)
    cut_plan = (
        CutPlan(
            keep_segments=[(0.0, 4.0), (5.0, 10.0)],
            removed=[Removal(start_s=4.0, end_s=5.0, reason="test")],
            time_saved_s=1.0,
        )
        if cut
        else None
    )
    cues = [{"text": "Hello everyone", "start_s": 0.0, "end_s": 1.5}]
    recipe = compile_phone_subtitled_plan(
        bindings, caption_cues=cues, cut_plan=cut_plan, landscape_fit="fit"
    )
    job = SimpleNamespace(
        id=uuid.uuid4(),
        user_id=uuid.uuid4(),
        status="awaiting_device",
        current_phase=None,
        assembly_plan={
            PHONE_SOURCES_FIELD: [b.model_dump(mode="json") for b in bindings],
            PHONE_VISUALS_FIELD: [],
            "variants": [
                {
                    "variant_id": "subtitled",
                    "resolved_archetype": "subtitled",
                    "render_destination": "device",
                    "render_status": "awaiting_device",
                    "render_generation_id": "first",
                    "duration_s": recipe.duration,
                    "caption_cues": cues,
                    "voiceover_caption_style": "sentence",
                }
            ],
        },
    )
    pin_device_request(
        job,
        make_device_request(job_id=job.id, variant_id="subtitled", revision=1, recipe=recipe),
        base_generation="first",
    )
    return job


def _main_track_scales(request) -> list[float]:
    track = next(t for t in request.recipe.tracks if t.id == "subtitled")
    return [clip.transform.scale for clip in track.clips]


@pytest.mark.parametrize("cut", [False, True])
def test_landscape_letterbox_is_preserved_across_a_save(monkeypatch, cut):
    job = _letterboxed_phone_job(monkeypatch, cut=cut)
    baseline = _main_track_scales(device_status(job, "subtitled").request)
    assert baseline and all(scale == 0.31640625 for scale in baseline)

    save(job, sound_effects=[])

    after = _main_track_scales(device_status(job, "subtitled").request)
    assert after == baseline


def _main_track_segments(request) -> list[tuple[float, float]]:
    track = next(t for t in request.recipe.tracks if t.id == "subtitled")
    return sorted(
        (clip.source_start, clip.source_start + clip.source_duration) for clip in track.clips
    )


def test_pinned_speech_cleanup_cut_is_preserved_across_an_unrelated_save(monkeypatch):
    job = _cut_phone_job(monkeypatch)
    baseline = _main_track_segments(device_status(job, "subtitled").request)
    assert baseline == [(0.0, 4.0), (5.0, 10.0)]  # sanity: the fixture is actually cut

    # An empty sfx section is a real "unrelated" commit (no lane content to
    # resolve, unlike adding a brand new catalog effect) that still runs the
    # full `_compile_subtitled_editor_commit` recompile path.
    save(job, sound_effects=[])

    segments = _main_track_segments(device_status(job, "subtitled").request)
    assert segments == baseline


def test_pinned_speech_cleanup_cut_is_preserved_across_a_caption_cues_save(monkeypatch):
    job = _cut_phone_job(monkeypatch)
    baseline = _main_track_segments(device_status(job, "subtitled").request)

    save(job, caption_cues=[{"text": "Still cut", "start_s": 0.0, "end_s": 1.0}])

    segments = _main_track_segments(device_status(job, "subtitled").request)
    assert segments == baseline


def test_uncut_variant_stays_single_full_duration_clip_after_a_save(monkeypatch):
    """The `keep_segments` reconstruction must be a no-op for a variant that
    was never cut -- `phone_job`'s fixture recipe is a single full-duration
    clip; a Save must not turn it into anything else."""
    job = phone_job(monkeypatch)
    baseline = _main_track_segments(device_status(job, "subtitled").request)
    assert len(baseline) == 1

    save(job, sound_effects=[_sfx_payload(at_s=4.0)])

    segments = _main_track_segments(device_status(job, "subtitled").request)
    assert segments == baseline


def test_first_save_does_not_duck_a_carried_forward_effect_twice(monkeypatch):
    job = phone_job(monkeypatch, duck=True)
    # Only overlays are committed: the sound lane is derived from the ducked pin.
    save(job, media_overlays=[_overlay_payload(x_frac=0.9)])
    assert _sfx_volumes(job) == {"sfx-sfx-1": pytest.approx(SFX_SPEECH_DUCK_GAIN)}
    variant = job.assembly_plan["variants"][0]
    assert variant[SFX_DUCK_RECEIPT_FIELD]["volumes"] == {"sfx-1": 1.0}


def test_effect_moved_into_a_pause_plays_at_full_volume_again(monkeypatch):
    job = phone_job(monkeypatch, duck=True)
    save(job, sound_effects=[_sfx_payload(at_s=5.0, gain=1.0)])
    assert _sfx_volumes(job) == {"sfx-sfx-1": pytest.approx(1.0)}
    assert SFX_DUCK_RECEIPT_FIELD not in job.assembly_plan["variants"][0]


def test_editor_save_ducks_an_effect_moved_onto_speech(monkeypatch):
    job = phone_job(monkeypatch, duck=True)
    save(job, sound_effects=[_sfx_payload(at_s=2.0, gain=0.8)])
    assert _sfx_volumes(job) == {"sfx-sfx-1": pytest.approx(0.8 * SFX_SPEECH_DUCK_GAIN)}
    receipt = job.assembly_plan["variants"][0][SFX_DUCK_RECEIPT_FIELD]
    assert receipt["volumes"] == {"sfx-1": 0.8}


def test_duck_off_editor_save_writes_no_receipt(monkeypatch):
    job = phone_job(monkeypatch, duck=False)
    save(job, sound_effects=[_sfx_payload(at_s=1.0, gain=1.0)])
    assert _sfx_volumes(job) == {"sfx-sfx-1": pytest.approx(1.0)}
    assert SFX_DUCK_RECEIPT_FIELD not in job.assembly_plan["variants"][0]


# --- KRI-136: multi-clip Talking head keeps its speaker + cutaways ------------


def talking_head_job(monkeypatch):
    """A phone Talking variant pinned as the worker pins a multi-clip Talking
    head: every clip is a phone source, only the speaker plays on the main
    track, the others are cutaways."""
    _enable_subtitled_editor(monkeypatch)
    monkeypatch.setattr(gj.settings, "phone_sfx_speech_duck_enabled", False)
    speaker = _binding("speaker", duration_s=20.0)
    cutaways = (
        PhoneCutaway(binding=_binding("broll-a", duration_s=6.0), start_s=1.5, end_s=4.5),
        PhoneCutaway(binding=_binding("broll-b", duration_s=2.0), start_s=10.0, end_s=12.0),
    )
    recipe = compile_phone_subtitled_plan((speaker,), caption_cues=_CUES, cutaways=cutaways)
    bindings = (cutaways[0].binding, speaker, cutaways[1].binding)
    job = SimpleNamespace(
        id=uuid.uuid4(),
        user_id=uuid.uuid4(),
        status="awaiting_device",
        current_phase=None,
        assembly_plan={
            PHONE_SOURCES_FIELD: [b.model_dump(mode="json") for b in bindings],
            "variants": [
                {
                    "variant_id": "subtitled",
                    "resolved_archetype": "subtitled",
                    "render_destination": "device",
                    "render_status": "awaiting_device",
                    "render_generation_id": "first",
                    "duration_s": recipe.duration,
                    "caption_cues": _CUES,
                    "voiceover_caption_style": "sentence",
                }
            ],
        },
    )
    pin_device_request(
        job,
        make_device_request(job_id=job.id, variant_id="subtitled", revision=1, recipe=recipe),
        base_generation="first",
    )
    return job, recipe


def test_talking_head_caption_save_keeps_speaker_and_cutaways(monkeypatch):
    job, old_recipe = talking_head_job(monkeypatch)
    save(job, caption_cues=[{"text": "Updated caption", "start_s": 0.0, "end_s": 1.5}])

    new = device_status(job, "subtitled").request
    assert new.identity.recipe_revision == 2
    tracks = {track.id: track for track in new.recipe.tracks}
    assert [c.source_asset_id for c in tracks["subtitled"].clips] == ["speaker"]
    old_cutaways = next(t for t in old_recipe.tracks if t.id == CUTAWAY_TRACK_ID)
    assert tracks[CUTAWAY_TRACK_ID].model_dump() == old_cutaways.model_dump()
    assert new.recipe.duration == pytest.approx(20.0)
