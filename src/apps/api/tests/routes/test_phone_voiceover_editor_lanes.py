"""KRI-281: phone-rendered Voiceover videos (`narrated` and montage `voiceover`)
show their real clips in the native editor and open the sound-effect / Visuals
lanes -- capabilities and Save together.

Covers: the read-time timeline projection (existing videos), the persisted
write-time rows, the capability clamp (rollout on / off / old app build), and
the Save recompile for both archetypes.
"""

from __future__ import annotations

import uuid
from types import SimpleNamespace

import pytest
from fastapi import HTTPException

import app.routes.generative_jobs as gj
from app.kria.device_render import make_device_request
from app.kria.recipes_v2 import EditRecipeV2
from app.pipeline.phone_narrated_plan import compile_phone_narrated_plan
from app.services.client_protocol import clear_request_context, set_client_protocol
from app.services.device_render import device_status, pin_device_request
from app.services.phone_sources import PHONE_SOURCES_FIELD, PHONE_VISUALS_FIELD
from app.services.phone_voiceover_timeline import (
    narrated_timings_and_assignments,
    project_phone_voiceover_timeline,
    source_pool_paths,
)
from tests.pipeline.test_phone_narrated_plan import _binding, _narration, _step
from tests.pipeline.test_phone_subtitled_plan import PHOTO_PATH, _photo_visual
from tests.routes.test_editor_commit import _arm_every_editor_lane, _job

_VERIFIED = [
    "basicComposition",
    "local1080Export",
    "narrationAudio",
    "audioMix",
    "audioDucking",
    "positionedText",
    "animatedText",
    "variableSpeed",
    "soundEffects",
    "visualBlocks",
    "alphaOverlay",
    "stillImages",
    "visualVideos",
]
_CUES = [{"text": "First we pack", "start_s": 0.0, "end_s": 2.0}]
_MIN_PROTOCOL = 3


@pytest.fixture(autouse=True)
def _reset_protocol():
    clear_request_context()
    yield
    clear_request_context()


def _enable(monkeypatch, *, lanes: bool = True, protocol: int | None = _MIN_PROTOCOL) -> None:
    monkeypatch.setattr(gj.settings, "phone_rendering_enabled", True)
    monkeypatch.setattr(gj.settings, "phone_render_user_ids", [])
    monkeypatch.setattr(gj.settings, "phone_render_verified_features", list(_VERIFIED))
    monkeypatch.setattr(gj.settings, "phone_narrated_caption_edits_enabled", True)
    monkeypatch.setattr(gj.settings, "sound_effects_enabled", True)
    monkeypatch.setattr(gj.settings, "media_overlays_enabled", True)
    monkeypatch.setattr(gj.settings, "phone_voiceover_editor_lanes_enabled", lanes)
    set_client_protocol(protocol)


def _bindings():
    return (_binding("c0"), _binding("c1", duration_s=2.0), _binding("c2"))


def _narrated_recipe() -> EditRecipeV2:
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
        caption_cues=_CUES,
    )


def _montage_recipe() -> EditRecipeV2:
    recipe = _narrated_recipe()
    fields = {name: getattr(recipe, name) for name in type(recipe).model_fields}
    fields["tracks"] = [
        track.model_copy(update={"id": "montage"}) if track.id == "narrated" else track
        for track in recipe.tracks
    ]
    return EditRecipeV2(**fields)


# The job's source pool is deliberately NOT in binding order: the matcher reorders
# clips, and a slot must index the pool the timeline lists.
_POOL = [f"user/analysis-proxy-{m}.mp4" for m in ("c2", "c0", "c1")]


def voiceover_job(*, archetype: str = "narrated", persisted: dict | None = None):
    recipe = _narrated_recipe() if archetype == "narrated" else _montage_recipe()
    variant_id = "narrated" if archetype == "narrated" else "voiceover_only"
    photo = _photo_visual()
    job = SimpleNamespace(
        id=uuid.uuid4(),
        user_id=uuid.uuid4(),
        status="awaiting_device",
        current_phase=None,
        content_plan_item_id=None,
        mode="content_plan",
        all_candidates={"clip_paths": list(_POOL)},
        assembly_plan={
            PHONE_SOURCES_FIELD: [b.model_dump(mode="json") for b in _bindings()],
            PHONE_VISUALS_FIELD: [photo.model_dump(mode="json")],
            "variants": [
                {
                    "variant_id": variant_id,
                    "rank": 1,
                    "resolved_archetype": archetype,
                    "render_destination": "device",
                    "render_status": "awaiting_device",
                    "render_generation_id": "first",
                    "duration_s": recipe.duration,
                    "caption_cues": _CUES if archetype == "narrated" else None,
                    "voiceover_caption_style": "sentence",
                    **(persisted or {}),
                }
            ],
        },
    )
    pin_device_request(
        job,
        make_device_request(job_id=job.id, variant_id=variant_id, revision=1, recipe=recipe),
        base_generation="first",
    )
    return job, variant_id


# --- projection ---------------------------------------------------------------------


def test_narrated_projection_maps_clips_to_the_source_pool_not_binding_order():
    job, vid = voiceover_job()
    variant = job.assembly_plan["variants"][0]

    projected = project_phone_voiceover_timeline(job.assembly_plan, job.all_candidates, variant)

    assert [a["step_id"] for a in projected["narrated_clip_assignments"]] == ["s0", "s1", "s2"]
    # c0 -> pool[1], c1 -> pool[2], c2 -> pool[0].
    assert [a["clip_id"] for a in projected["narrated_clip_assignments"]] == [
        "clip_1",
        "clip_2",
        "clip_0",
    ]
    assert projected["narrated_clip_assignments"][0]["participant_key"] == "clip:clip_1"
    assert projected["narrated_clip_assignments"][0]["source_start_s"] == pytest.approx(1.5)
    assert [(t["start_s"], t["end_s"]) for t in projected["narrated_timings"]] == [
        (0.0, 4.0),
        (4.0, 8.0),
        (8.0, 12.0),
    ]
    assert [s["clip_index"] for s in projected["slots"]] == [1, 2, 0]
    assert projected["pool"] == _POOL


def test_voiceover_montage_projection_is_a_read_only_ai_timeline():
    job, _ = voiceover_job(archetype="voiceover")
    variant = job.assembly_plan["variants"][0]

    projected = project_phone_voiceover_timeline(job.assembly_plan, job.all_candidates, variant)

    slots = projected["ai_timeline"]["slots"]
    assert [(s["clip_index"], s["in_s"], s["duration_s"]) for s in slots] == [
        (1, 1.5, 4.0),
        (2, 0.0, 4.0),
        (0, 0.0, 4.0),
    ]
    assert all(s["removed"] is False for s in slots)
    assert projected["ai_timeline"]["beat_grid"] == []


def test_pool_falls_back_to_binding_order_when_clip_paths_do_not_cover_the_bindings():
    job, _ = voiceover_job()
    assert source_pool_paths(job.assembly_plan, {"clip_paths": ["only-one.mp4"]}) == [
        b.proxy_path for b in _bindings()
    ]
    assert source_pool_paths(job.assembly_plan, None) == [b.proxy_path for b in _bindings()]


def test_unpinned_or_other_archetypes_project_nothing():
    job, _ = voiceover_job()
    variant = job.assembly_plan["variants"][0]
    assert (
        project_phone_voiceover_timeline(
            job.assembly_plan, job.all_candidates, {**variant, "resolved_archetype": "subtitled"}
        )
        is None
    )
    assert (
        project_phone_voiceover_timeline(
            {PHONE_SOURCES_FIELD: job.assembly_plan[PHONE_SOURCES_FIELD]},
            job.all_candidates,
            variant,
        )
        is None
    )


def test_write_time_helper_matches_the_read_time_projection():
    job, _ = voiceover_job()
    variant = job.assembly_plan["variants"][0]
    recipe = device_status(job, "narrated").request.recipe
    from app.services.phone_sources import PhoneSourceBinding

    bindings = [
        PhoneSourceBinding.model_validate(r) for r in job.assembly_plan[PHONE_SOURCES_FIELD]
    ]
    timings, assignments, _slots = narrated_timings_and_assignments(recipe, bindings, _POOL)
    read = project_phone_voiceover_timeline(job.assembly_plan, job.all_candidates, variant)
    assert (timings, assignments) == (
        read["narrated_timings"],
        read["narrated_clip_assignments"],
    )


def test_variants_response_backfills_an_existing_narrated_video(monkeypatch):
    job, _ = voiceover_job()
    variant = job.assembly_plan["variants"][0]
    assert "narrated_timings" not in variant

    patched = gj._augment_variant_with_phone_voiceover_timeline(job, dict(variant))

    assert len(patched["narrated_timings"]) == 3
    assert len(patched["narrated_clip_assignments"]) == 3
    # A persisted cut always wins over a fresh derivation.
    keep = {**variant, "narrated_timings": [{"step_id": "x"}], "narrated_clip_assignments": [1]}
    assert gj._augment_variant_with_phone_voiceover_timeline(job, keep) == keep


def test_variants_response_backfills_a_voiceover_montage_ai_timeline():
    job, _ = voiceover_job(archetype="voiceover")
    patched = gj._augment_variant_with_phone_voiceover_timeline(
        job, dict(job.assembly_plan["variants"][0])
    )
    assert len(patched["ai_timeline"]["slots"]) == 3


@pytest.mark.parametrize("archetype", ["narrated", "voiceover"])
def test_timeline_endpoint_lists_slots_and_marks_pool_clips_used(archetype):
    job, vid = voiceover_job(archetype=archetype)

    timeline = gj.dispatch_get_timeline(job, vid, sign_url=lambda path, ttl: f"https://s/{path}")

    # Cut edits stay locked to the voiceover.
    assert timeline["editable"] is False
    assert timeline["reason"] in {"locked_to_voiceover", "voiceover_bed_fit"}
    assert [s["clip_index"] for s in timeline["slots"]] == [1, 2, 0]
    assert [c["clip_index"] for c in timeline["clips"]] == [0, 1, 2]
    assert all(c["used"] for c in timeline["clips"])
    assert timeline["total_duration_s"] == pytest.approx(12.0)
    # Pool entry 0 is c2: its native source is that binding.
    assert timeline["clips"][0]["native_source"]["media_id"] == "c2"
    assert timeline["clips"][1]["native_source"]["media_id"] == "c0"


# --- capability clamp ---------------------------------------------------------------


def _caps(monkeypatch, archetype="narrated"):
    _arm_every_editor_lane(monkeypatch)
    job = _job(resolved_archetype=archetype)
    variant = {**job.assembly_plan["variants"][0], "render_destination": "device"}
    return gj._editor_capabilities(job, variant)


@pytest.mark.parametrize("archetype", ["narrated", "voiceover"])
def test_lanes_open_when_rollout_and_build_gate_pass(monkeypatch, archetype):
    caps_off = _caps(monkeypatch, archetype)  # pre-rollout baseline
    _enable(monkeypatch)
    caps = _caps(monkeypatch, archetype)
    _enable(monkeypatch)  # _arm resets some flags; restore ours

    assert caps_off["sfx"] is False and caps_off["overlays"] is False
    assert caps["sfx"] is True and caps["overlays"] is True
    # Nothing else is opened by this rollout.
    for lane in ("visual_blocks", "motion_scenes", "camera_effects"):
        assert caps[lane] is False


@pytest.mark.parametrize(
    "mutate",
    [
        pytest.param(
            lambda m: m.setattr(gj.settings, "phone_voiceover_editor_lanes_enabled", False),
            id="flag-off",
        ),
        pytest.param(lambda m: set_client_protocol(_MIN_PROTOCOL - 1), id="old-build"),
        pytest.param(lambda m: set_client_protocol(None), id="no-header"),
        pytest.param(
            lambda m: m.setattr(gj.settings, "sound_effects_enabled", False), id="sfx-killed"
        ),
        pytest.param(
            lambda m: m.setattr(gj.settings, "media_overlays_enabled", False), id="overlay-killed"
        ),
        pytest.param(
            lambda m: m.setattr(
                gj.settings, "phone_render_verified_features", ["basicComposition"]
            ),
            id="parity-gate",
        ),
    ],
)
def test_lanes_stay_closed_unless_every_gate_passes(monkeypatch, mutate):
    _enable(monkeypatch)
    _arm_every_editor_lane(monkeypatch)
    mutate(monkeypatch)
    job = _job(resolved_archetype="narrated")
    variant = {**job.assembly_plan["variants"][0], "render_destination": "device"}
    caps = gj._editor_capabilities(job, variant)
    assert caps["sfx"] is False
    assert caps["overlays"] is False
    assert caps["sfx_reason"] == "phone_edit_unsupported" or caps["sfx_reason"] is not None


def test_cloud_narrated_is_untouched_by_the_voiceover_rollout(monkeypatch):
    _enable(monkeypatch)
    _arm_every_editor_lane(monkeypatch)
    job = _job(resolved_archetype="narrated")
    variant = job.assembly_plan["variants"][0]
    assert gj._phone_voiceover_editor_lanes_available(variant) is False


# --- Save ---------------------------------------------------------------------------


def _sfx_payload(effect_id="sfx-1", catalog_id="pop", at_s=1.0):
    return {
        "id": effect_id,
        "sound_effect_id": catalog_id,
        "src_gcs_path": f"sound-effects/{catalog_id}/{catalog_id}.m4a",
        "at_s": at_s,
        "gain": 1.0,
        "duration_s": 1.0,
    }


def _overlay_payload(card_id="card-1"):
    return {
        "id": card_id,
        "kind": "image",
        "src_gcs_path": PHOTO_PATH,
        "display_mode": "pip",
        "x_frac": 0.5,
        "y_frac": 0.4,
        "scale": 0.35,
        "start_s": 1.0,
        "end_s": 3.0,
    }


def _fake_inspect(monkeypatch):
    from app.services import phone_subtitled_editor as pse
    from tests.pipeline.test_phone_subtitled_plan import _sfx_asset

    monkeypatch.setattr(
        pse,
        "inspect_library_asset",
        lambda path, *, asset_id, catalog, catalog_id: _sfx_asset(catalog_id, generation="42"),
    )


def save(job, variant_id, **sections):
    return gj.prepare_editor_commit(
        job,
        variant_id,
        gj.EditorCommitRequest(base_generation="first", **sections),
        user_id="owner",
        plan_item_id="item",
    )


def test_narrated_save_adds_sfx_and_overlay_lanes_and_keeps_the_cut(monkeypatch):
    _enable(monkeypatch)
    _fake_inspect(monkeypatch)
    job, vid = voiceover_job()
    old = device_status(job, vid).request.recipe

    prep = save(job, vid, sound_effects=[_sfx_payload()], media_overlays=[_overlay_payload()])

    assert prep["render_destination"] == "device"
    new_status = device_status(job, vid).request
    recipe = new_status.recipe
    assert new_status.identity.recipe_revision == 2
    assert next(t for t in recipe.tracks if t.id == "narrated") == next(
        t for t in old.tracks if t.id == "narrated"
    )
    assert next(t for t in recipe.tracks if t.id == "narration") == next(
        t for t in old.tracks if t.id == "narration"
    )
    assert recipe.text_layers == old.text_layers
    assert recipe.audio == old.audio
    assert [c.id for c in next(t for t in recipe.tracks if t.id == "sfx").clips] == ["sfx-sfx-1"]
    assert len(next(t for t in recipe.tracks if t.id == "subtitled-overlays").clips) == 1
    assert {"soundEffects", "visualBlocks", "alphaOverlay"} <= set(recipe.required_capabilities)
    variant = job.assembly_plan["variants"][0]
    assert variant["sound_effects"][0]["sound_effect_id"] == "pop"
    assert variant["media_overlays"][0]["id"] == "card-1"


def test_narrated_lane_save_then_delete_returns_to_a_laneless_recipe(monkeypatch):
    _enable(monkeypatch)
    _fake_inspect(monkeypatch)
    job, vid = voiceover_job()
    save(job, vid, sound_effects=[_sfx_payload()], media_overlays=[_overlay_payload()])
    save_again = gj.EditorCommitRequest(
        base_generation=job.assembly_plan["variants"][0]["render_generation_id"],
        sound_effects=[],
        media_overlays=[],
    )
    gj.prepare_editor_commit(job, vid, save_again, user_id="owner", plan_item_id="item")

    recipe = device_status(job, vid).request.recipe
    assert not any(t.id in {"sfx", "subtitled-overlays"} for t in recipe.tracks)
    assert not {"soundEffects", "visualBlocks", "alphaOverlay"} & set(recipe.required_capabilities)
    original = _narrated_recipe()
    assert [a.id for a in recipe.assets] == [a.id for a in original.assets]


def test_caption_save_carries_pinned_lanes_forward(monkeypatch):
    _enable(monkeypatch)
    _fake_inspect(monkeypatch)
    job, vid = voiceover_job()
    save(job, vid, sound_effects=[_sfx_payload()])
    gj.prepare_editor_commit(
        job,
        vid,
        gj.EditorCommitRequest(
            base_generation=job.assembly_plan["variants"][0]["render_generation_id"],
            caption_cues=[{"text": "Packed", "start_s": 0.0, "end_s": 2.0}],
        ),
        user_id="owner",
        plan_item_id="item",
    )
    recipe = device_status(job, vid).request.recipe
    assert any(t.id == "sfx" for t in recipe.tracks)


def test_voiceover_montage_save_recompiles_lanes(monkeypatch):
    _enable(monkeypatch)
    _fake_inspect(monkeypatch)
    job, vid = voiceover_job(archetype="voiceover")
    old = device_status(job, vid).request.recipe

    save(job, vid, sound_effects=[_sfx_payload()], media_overlays=[_overlay_payload()])

    recipe = device_status(job, vid).request.recipe
    assert next(t for t in recipe.tracks if t.id == "montage") == next(
        t for t in old.tracks if t.id == "montage"
    )
    assert any(t.id == "sfx" for t in recipe.tracks)
    assert any(t.id == "subtitled-overlays" for t in recipe.tracks)


@pytest.mark.parametrize("archetype", ["narrated", "voiceover"])
def test_save_422s_lane_sections_when_the_rollout_is_off(monkeypatch, archetype):
    _enable(monkeypatch, lanes=False)
    job, vid = voiceover_job(archetype=archetype)
    before = device_status(job, vid).request

    with pytest.raises(HTTPException) as error:
        save(job, vid, sound_effects=[_sfx_payload()])

    assert error.value.status_code == 422
    assert error.value.detail["code"] == "unsupported_phone_edit"
    assert device_status(job, vid).request == before


def test_save_does_not_need_the_request_header(monkeypatch):
    """Capabilities consult the app build; Save (also reached by worker-driven chat
    edits with no request) only needs the server-side gates."""
    _enable(monkeypatch, protocol=None)
    _fake_inspect(monkeypatch)
    job, vid = voiceover_job()
    save(job, vid, sound_effects=[_sfx_payload()])
    assert any(t.id == "sfx" for t in device_status(job, vid).request.recipe.tracks)


def test_montage_save_refuses_sections_other_than_the_two_lanes(monkeypatch):
    _enable(monkeypatch)
    job, vid = voiceover_job(archetype="voiceover")
    from app.services.phone_editor import prepare_phone_editor_commit

    with pytest.raises(HTTPException) as error:
        prepare_phone_editor_commit(
            job,
            vid,
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
    assert "text_elements" in error.value.detail["reason"]


def test_middleware_exposes_the_declared_client_protocol_to_handlers():
    from fastapi.testclient import TestClient

    from app.main import app
    from app.services.client_protocol import current_client_protocol

    @app.get("/__kri281_protocol_probe")
    async def probe():
        return {"protocol": current_client_protocol()}

    client = TestClient(app)
    assert client.get("/__kri281_protocol_probe").json() == {"protocol": None}
    declared = client.get("/__kri281_protocol_probe", headers={"X-Kria-Client-Protocol": "3"})
    assert declared.json() == {"protocol": 3}
    junk = client.get("/__kri281_protocol_probe", headers={"X-Kria-Client-Protocol": "abc"})
    assert junk.json() == {"protocol": None}


# --- worker / sync callers (no HTTP request) ----------------------------------------


def test_worker_context_judges_lanes_on_server_gates_only(monkeypatch):
    _enable(monkeypatch)
    _arm_every_editor_lane(monkeypatch)
    clear_request_context()  # Celery / sync runtime: no request, so no build to qualify
    job = _job(resolved_archetype="narrated")
    variant = {**job.assembly_plan["variants"][0], "render_destination": "device"}
    caps = gj._editor_capabilities(job, variant)
    assert caps["sfx"] is True and caps["overlays"] is True
    # ...but the server-side gates still apply.
    monkeypatch.setattr(gj.settings, "phone_voiceover_editor_lanes_enabled", False)
    assert gj._editor_capabilities(job, variant)["sfx"] is False


def test_sfx_paths_resolve_for_voiceover_variants_in_worker_and_request(monkeypatch):
    _enable(monkeypatch)
    _fake_inspect(monkeypatch)
    job, vid = voiceover_job()
    save(job, vid, sound_effects=[_sfx_payload()])
    variant = dict(job.assembly_plan["variants"][0])
    # Persisted rows carry a real path from the Save; a placeholder one needs a catalog read.
    variant["sound_effects"] = [
        {
            **variant["sound_effects"][0],
            "src_gcs_path": "sound-effects/pop/pop",
            "source": "phone_lane",
        }
    ]
    assert gj._phone_subtitled_sfx_ids_missing_paths(job, variant) == {"pop"}
    clear_request_context()
    assert gj._phone_subtitled_sfx_ids_missing_paths(job, variant) == {"pop"}
    # An old build in a request still gets nothing.
    set_client_protocol(_MIN_PROTOCOL - 1)
    assert gj._phone_subtitled_sfx_ids_missing_paths(job, variant) == set()


# --- montage crossfade overlaps -------------------------------------------------------


def test_montage_crossfade_overlaps_are_not_double_counted(monkeypatch):
    from app.kria.recipes import Transition

    recipe = _montage_recipe()
    fields = {name: getattr(recipe, name) for name in type(recipe).model_fields}
    tracks = []
    for track in recipe.tracks:
        if track.id == "montage":
            clips = []
            shift = 0.0
            for i, clip in enumerate(track.clips):
                if i:
                    shift += 0.5  # each later clip starts 0.5 s into the previous one
                clips.append(
                    clip.model_copy(
                        update={
                            "timeline_start": clip.timeline_start - shift,
                            "transition": Transition(kind="crossfade", duration=0.5) if i else None,
                        }
                    )
                )
            track = track.model_copy(update={"clips": clips})
        tracks.append(track)
    fields["tracks"] = tracks
    overlapped = EditRecipeV2(**fields)

    job, vid = voiceover_job(archetype="voiceover")
    pin_device_request(
        job,
        make_device_request(job_id=job.id, variant_id=vid, revision=2, recipe=overlapped),
        base_generation="first",
    )
    timeline = gj.dispatch_get_timeline(job, vid, sign_url=lambda path, ttl: f"https://s/{path}")

    assert timeline["total_duration_s"] == pytest.approx(overlapped.duration)
    slots = timeline["slots"]
    assert [s["transition_after"] for s in slots] == ["crossfade", "crossfade", "cut"]
    assert slots[0]["transition_duration_s"] == pytest.approx(0.5)
