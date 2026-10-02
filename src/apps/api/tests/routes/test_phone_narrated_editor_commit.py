"""KRI-280: a phone-rendered Narrated variant's caption Save recompiles its
device recipe, the same caption editing phone Talking has (KRI-216).

Goes through the real `generative_jobs.prepare_editor_commit`, so the generic
caption validators, the device routing and `prepare_phone_editor_commit`'s
narrated branch (`phone_narrated_plan.replace_narrated_captions`) are all
exercised together.
"""

import uuid
from types import SimpleNamespace

import pytest
from fastapi import HTTPException

from app.kria.device_render import make_device_request, recipe_digest
from app.pipeline.phone_narrated_plan import compile_phone_narrated_plan
from app.routes import generative_jobs as gj
from app.services.device_render import device_record, device_status, pin_device_request
from app.services.phone_sources import PHONE_SOURCES_FIELD
from tests.pipeline.test_phone_narrated_plan import _binding, _narration, _step

_VERIFIED_FEATURES = [
    "basicComposition",
    "local1080Export",
    "narrationAudio",
    "audioMix",
    "audioDucking",
    "positionedText",
    "animatedText",
    "variableSpeed",
]

_CUES = [
    {"text": "First we pack", "start_s": 0.0, "end_s": 2.0},
    {"text": "then we drive", "start_s": 4.5, "end_s": 6.0},
]

_WORD_CUES = [
    {
        "text": "First we pack",
        "start_s": 0.0,
        "end_s": 2.0,
        "words": [
            {"text": "First", "start_s": 0.0, "end_s": 0.6},
            {"text": "we", "start_s": 0.7, "end_s": 1.0},
            {"text": "pack", "start_s": 1.1, "end_s": 2.0},
        ],
    },
]


def _bindings():
    # c1 holds only 2 s of footage for a 4 s step, so the job slowed it down
    # (rate < 1): a caption Save must leave that decision exactly as pinned.
    return (_binding("c0"), _binding("c1", duration_s=2.0), _binding("c2"))


def _pinned_recipe(*, cues=_CUES, style="sentence"):
    # The worker's real knobs: a ducked footage bed and loudness target, both
    # of which a Save must carry over untouched.
    return compile_phone_narrated_plan(
        [
            _step("s0", "c0", start_s=0.0, end_s=4.0, source_start_s=1.5),
            _step("s1", "c1", start_s=4.0, end_s=8.0),
            _step("s2", "c2", start_s=8.0, end_s=12.0),
        ],
        _bindings(),
        _narration(duration_s=12.0),
        voiceover_duration_s=12.0,
        mix=0.7,
        caption_cues=cues,
        caption_style=style,
        target_lufs=-14.0,
        duck_footage_bed=True,
    )


def phone_job(monkeypatch, *, cues=_CUES, style="sentence", narration_binding=None):
    """A device `narrated` variant exactly as `_run_phone_narrated_job` leaves it:
    caption cues + style on the variant, no base video, no guided plan."""
    monkeypatch.setattr(gj.settings, "phone_rendering_enabled", True)
    monkeypatch.setattr(gj.settings, "phone_render_user_ids", [])
    monkeypatch.setattr(gj.settings, "phone_render_verified_features", list(_VERIFIED_FEATURES))
    monkeypatch.setattr(gj.settings, "phone_narrated_caption_edits_enabled", True)
    recipe = _pinned_recipe(cues=cues, style=style)
    job = SimpleNamespace(
        id=uuid.uuid4(),
        user_id=uuid.uuid4(),
        status="awaiting_device",
        current_phase=None,
        assembly_plan={
            PHONE_SOURCES_FIELD: [b.model_dump(mode="json") for b in _bindings()],
            "variants": [
                {
                    "variant_id": "narrated",
                    "rank": 1,
                    "resolved_archetype": "narrated",
                    "render_destination": "device",
                    "render_status": "awaiting_device",
                    "render_generation_id": "first",
                    "duration_s": recipe.duration,
                    "caption_cues": cues,
                    "voiceover_caption_style": style,
                    "caption_language": "en",
                    "voiceover_bed_level": 0.3,
                    "ok": False,
                }
            ],
        },
    )
    pin_device_request(
        job,
        make_device_request(job_id=job.id, variant_id="narrated", revision=1, recipe=recipe),
        base_generation="first",
        narration_binding=narration_binding,
    )
    return job


def save(job, **sections):
    return gj.prepare_editor_commit(
        job,
        "narrated",
        gj.EditorCommitRequest(base_generation="first", **sections),
        user_id="owner",
        plan_item_id="item",
    )


def _caption_texts(recipe):
    return [" ".join(run.text for run in layer.runs).strip() for layer in recipe.text_layers]


def _fonts(recipe):
    return sorted(a.id for a in recipe.asset_manifest.assets if a.id.startswith("font-"))


def _assert_only_captions_moved(old, new):
    assert new.tracks == old.tracks
    assert new.audio == old.audio
    assert new.canvas == old.canvas
    non_font = [a for a in old.asset_manifest.assets if not a.id.startswith("font-")]
    assert [a for a in new.asset_manifest.assets if not a.id.startswith("font-")] == non_font


# --- happy path --------------------------------------------------------------------


def test_caption_text_edit_pins_revision_two_on_device(monkeypatch):
    job = phone_job(monkeypatch)
    old = device_status(job, "narrated").request
    edited = [dict(_CUES[0], text="First we pack light"), _CUES[1]]

    prep = save(job, caption_cues=edited)

    assert prep["render_destination"] == "device"
    assert prep["render_task_id"] is None
    new = device_status(job, "narrated").request
    assert new.identity.recipe_revision == old.identity.recipe_revision + 1
    assert _caption_texts(new.recipe) == ["First we pack light", "then we drive"]
    # Only the caption lane moved: same clips (incl. the slowed one), same
    # narration bed, same ducked mix and loudness target.
    _assert_only_captions_moved(old.recipe, new.recipe)

    variant = job.assembly_plan["variants"][0]
    assert variant["caption_cues"][0]["text"] == "First we pack light"
    assert variant["render_status"] == "awaiting_device"
    assert variant["render_destination"] == "device"
    assert variant["duration_s"] == pytest.approx(12.0)
    assert job.status == "awaiting_device"


def test_unchanged_cues_save_pins_the_same_program(monkeypatch):
    job = phone_job(monkeypatch)
    old = device_status(job, "narrated").request

    save(job, caption_cues=_CUES)

    new = device_status(job, "narrated").request
    assert new.identity.recipe_revision == 2
    # The device identity digest: `required_capabilities` is a set, so its raw
    # JSON list order depends on the hash seed; the digest sorts it.
    assert recipe_digest(new.recipe) == recipe_digest(old.recipe)


def test_caption_timing_edit_moves_the_layer(monkeypatch):
    job = phone_job(monkeypatch)
    moved = [_CUES[0], dict(_CUES[1], start_s=7.0, end_s=9.5)]

    save(job, caption_cues=moved)

    layers = device_status(job, "narrated").request.recipe.text_layers
    assert (layers[1].start, layers[1].end) == pytest.approx((7.0, 9.5))


def test_deleting_every_cue_drops_the_caption_layers_and_fonts(monkeypatch):
    job = phone_job(monkeypatch)
    save(job, caption_cues=[])

    recipe = device_status(job, "narrated").request.recipe
    assert recipe.text_layers == []
    assert "positionedText" not in recipe.required_capabilities
    assert _fonts(recipe) == []
    assert job.assembly_plan["variants"][0]["caption_cues"] == []


def test_word_style_keeps_karaoke_captions(monkeypatch):
    job = phone_job(monkeypatch, cues=_WORD_CUES, style="word")
    save(job, caption_cues=[dict(_WORD_CUES[0])])

    recipe = device_status(job, "narrated").request.recipe
    assert [layer.effect for layer in recipe.text_layers] == ["karaoke-line"]


# --- caption_meta (Style + Settings) ------------------------------------------------


def test_caption_meta_save_applies_every_field_to_the_compiled_recipe(monkeypatch):
    job = phone_job(monkeypatch, cues=_WORD_CUES)
    old = device_status(job, "narrated").request

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
    # caption_meta alone never rewrites the cues.
    assert variant["caption_cues"] == _WORD_CUES

    new = device_status(job, "narrated").request
    assert new.identity.recipe_revision == 2
    layer = new.recipe.text_layers[0]
    assert layer.effect == "karaoke-line"
    assert "animatedText" in new.recipe.required_capabilities
    run = layer.runs[0]
    assert run.font_asset_id == "font-Montserrat-Bold.ttf"
    assert run.font_size == pytest.approx(96)
    assert run.fill.red == pytest.approx(0x11 / 255)
    assert run.fill.green == pytest.approx(0x22 / 255)
    assert run.fill.blue == pytest.approx(0x33 / 255)
    assert run.stroke_width == pytest.approx(14)  # outline_px(7) * 2
    assert run.blur_layers == []  # shadow_enabled False
    assert layer.anchor_x == pytest.approx(80.0)  # alignment=left safe margin
    # The old caption font is no longer shipped to the phone.
    assert _fonts(new.recipe) == ["font-Montserrat-Bold.ttf"]
    _assert_only_captions_moved(old.recipe, new.recipe)


def test_a_variable_caption_font_does_not_stay_required_after_switching_back(monkeypatch):
    """`authoredText` comes from variable-font runs; once the creator switches
    back to the default font it must stop being required, or every later
    Save depends on a device feature the captions no longer use."""
    job = phone_job(monkeypatch)
    monkeypatch.setattr(
        gj.settings, "phone_render_verified_features", [*_VERIFIED_FEATURES, "authoredText"]
    )
    pinned = device_status(job, "narrated").request.recipe

    save(job, caption_meta=gj.EditorCommitCaptionMeta(font="Fraunces", font_set=True))
    assert "authoredText" in device_status(job, "narrated").request.recipe.required_capabilities

    job.assembly_plan["variants"][0]["render_generation_id"] = "first"
    save(job, caption_meta=gj.EditorCommitCaptionMeta(font=None, font_set=True))
    recipe = device_status(job, "narrated").request.recipe
    assert "authoredText" not in recipe.required_capabilities
    assert recipe_digest(recipe) == recipe_digest(pinned)


def test_a_chat_style_text_edit_shows_the_new_words_on_word_captions(monkeypatch):
    """The chat edit path changes `text` but keeps the old `words`; the phone
    must burn the edited words, not the pre-edit ones."""
    job = phone_job(monkeypatch, cues=_WORD_CUES, style="word")
    stale = [dict(_WORD_CUES[0], text="Packed and ready")]

    save(job, caption_cues=stale)

    layer = device_status(job, "narrated").request.recipe.text_layers[0]
    assert layer.effect == "karaoke-line"
    assert [run.text for run in layer.runs] == ["Packed", "and", "ready"]


def test_caption_position_moves_the_layer(monkeypatch):
    job = phone_job(monkeypatch)
    before = device_status(job, "narrated").request.recipe.text_layers[0].anchor_y

    save(job, caption_meta=gj.EditorCommitCaptionMeta(y_frac=0.5))

    after = device_status(job, "narrated").request.recipe.text_layers[0].anchor_y
    assert after == pytest.approx(0.5 * 1920)
    assert after != pytest.approx(before)


def test_caption_meta_enabled_false_compiles_no_caption_layers(monkeypatch):
    job = phone_job(monkeypatch)

    save(job, caption_meta=gj.EditorCommitCaptionMeta(enabled=False))

    recipe = device_status(job, "narrated").request.recipe
    assert recipe.text_layers == []
    variant = job.assembly_plan["variants"][0]
    assert variant["captions_enabled"] is False
    # Turning captions back on brings every line back.
    job.assembly_plan["variants"][0]["render_generation_id"] = "first"
    save(job, caption_meta=gj.EditorCommitCaptionMeta(enabled=True))
    assert _caption_texts(device_status(job, "narrated").request.recipe) == [
        "First we pack",
        "then we drive",
    ]


def test_a_variable_font_without_verified_authored_text_is_refused(monkeypatch):
    job = phone_job(monkeypatch)
    before = device_status(job, "narrated").request

    with pytest.raises(HTTPException) as error:
        save(job, caption_meta=gj.EditorCommitCaptionMeta(font="Fraunces", font_set=True))

    assert error.value.status_code == 422
    assert error.value.detail["code"] == "unsupported_phone_edit"
    assert device_status(job, "narrated").request == before


# --- carried-over state -------------------------------------------------------------


def test_cleaned_narration_binding_survives_a_caption_save(monkeypatch):
    """KRI-277 pins the cleaned voiceover's delivery binding on the first
    request; a caption Save keeps the same voiceover asset, so the next
    revision must keep delivering the cleaned narration."""
    voice = next(
        asset for asset in _pinned_recipe().asset_manifest.assets if asset.kind == "voiceover"
    )
    binding = {
        "asset_id": voice.id,
        "plan_item_id": voice.plan_item_id,
        "sha256": voice.fingerprint.sha256,
        "byte_count": voice.fingerprint.byte_count,
        "narration": {"generation": voice.generation, "gcs_path": "cleaned/voice.m4a"},
    }
    job = phone_job(monkeypatch, narration_binding=binding)

    save(job, caption_cues=[dict(_CUES[0], text="Packed")])

    assert device_record(job, "narrated")["narration_binding"] == binding


# --- refusals -----------------------------------------------------------------------


def test_a_non_caption_section_is_an_unsupported_phone_edit(monkeypatch):
    job = phone_job(monkeypatch)
    before = device_status(job, "narrated").request
    plan_before = job.assembly_plan

    with pytest.raises(HTTPException) as error:
        save(job, caption_cues=_CUES, text_elements=[])

    assert error.value.status_code == 422
    assert error.value.detail["code"] == "unsupported_phone_edit"
    assert "text_elements" in error.value.detail["reason"]
    # Nothing staged leaks onto the job.
    assert device_status(job, "narrated").request == before
    assert job.assembly_plan is plan_before


@pytest.mark.parametrize(
    "sections",
    [
        {"caption_cues": _CUES},
        {"caption_meta": gj.EditorCommitCaptionMeta(enabled=False)},
    ],
)
def test_rollout_off_refuses_caption_saves(monkeypatch, sections):
    job = phone_job(monkeypatch)
    monkeypatch.setattr(gj.settings, "phone_narrated_caption_edits_enabled", False)

    with pytest.raises(HTTPException) as error:
        save(job, **sections)

    assert error.value.status_code == 422


def test_cloud_narrated_without_a_base_still_rejects_caption_edits(monkeypatch):
    job = phone_job(monkeypatch)
    job.assembly_plan["variants"][0]["render_destination"] = "cloud"
    with pytest.raises(HTTPException) as error:
        save(job, caption_cues=_CUES)
    assert error.value.status_code == 422
