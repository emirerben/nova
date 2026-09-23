import copy
import uuid
from types import SimpleNamespace
from unittest.mock import patch

import pytest
from fastapi import HTTPException

from app.agents._schemas.text_element import TextElement
from app.kria.device_render import make_device_request
from app.pipeline.phone_guided_plan import compile_phone_guided_plan
from app.routes import generative_jobs as gj
from app.services.device_render import device_status, pin_device_request
from app.services.phone_editor import prepare_phone_editor_commit
from app.services.phone_sources import PHONE_SOURCES_FIELD, PHONE_VISUALS_FIELD
from tests.pipeline.test_phone_guided_plan import fixture
from tests.services import test_phone_visuals as photos


def phone_job(monkeypatch, *, photo=False, narration=None, bed=None):
    """``photo`` appends a Visuals-pool still whose receipt the worker pinned;
    ``narration`` + ``bed`` pin a recorded voiceover the way the worker did."""
    monkeypatch.setattr(gj.settings, "phone_rendering_enabled", True)
    monkeypatch.setattr(
        gj.settings,
        "phone_render_verified_features",
        ["basicComposition", "local1080Export", "positionedText", "animatedText", "audioMix"]
        + (["stillImages"] if photo else [])
        + (["narrationAudio"] if narration is not None else []),
    )
    monkeypatch.setattr(gj.settings, "guided_story_editor_v2_enabled", False)
    plan, bindings = photos.photo_plan() if photo else fixture()
    if narration is not None:
        plan.narration = narration
    visuals = (photos.photo_visual(),) if photo else ()
    plan.text_elements = [
        TextElement(
            id="title",
            text="Before",
            start_s=0,
            end_s=2,
            effect="none",
            font_family="Inter-Bold",
            size_px=64,
        )
    ]
    raw = plan.model_dump(mode="json")
    job = SimpleNamespace(
        id=uuid.uuid4(),
        user_id=uuid.uuid4(),
        status="awaiting_device",
        current_phase=None,
        assembly_plan={
            "guided_story_execution_plan": raw,
            PHONE_SOURCES_FIELD: [b.model_dump(mode="json") for b in bindings],
            **(
                {PHONE_VISUALS_FIELD: [v.model_dump(mode="json") for v in visuals]} if photo else {}
            ),
            "variants": [
                {
                    "variant_id": "guided_story",
                    "resolved_archetype": "guided_story",
                    "render_destination": "device",
                    "render_status": "awaiting_device",
                    "render_generation_id": "first",
                    "duration_s": raw["resolved_duration_s"],
                    "text_mode": "agent_text",
                    "intro_mode": "linear",
                    "intro_layout": "linear",
                    "text_elements": copy.deepcopy(raw["text_elements"]),
                }
            ],
        },
    )
    pin_device_request(
        job,
        make_device_request(
            job_id=job.id,
            variant_id="guided_story",
            revision=1,
            recipe=compile_phone_guided_plan(plan, bindings, visuals=visuals, narration=bed),
        ),
        base_generation="first",
    )
    return job


def save(
    job,
    *,
    generation="first",
    effect="fade-in",
    motion=None,
    guided_revision_number=None,
    **element_overrides,
):
    element = {
        **job.assembly_plan["variants"][0]["text_elements"][0],
        "text": "After",
        "effect": effect,
        "motion": motion,
        **element_overrides,
    }
    return gj.prepare_editor_commit(
        job,
        "guided_story",
        gj.EditorCommitRequest(
            base_generation=generation,
            text_elements=[element],
            guided_revision_number=guided_revision_number,
        ),
    )


def _enable_guided_v2(job, monkeypatch):
    """Turn the fixture into a real revision-token-bearing guided-v2 job."""
    from app.pipeline.guided_story import compile_execution_plan
    from app.schemas.edit_proposal import (
        EditProposalSnapshot,
        FastMontageCut,
        MediaRef,
        StoryBeat,
        canonical_media_digest,
    )

    monkeypatch.setattr(gj.settings, "guided_story_editor_v2_enabled", True)
    binding = job.assembly_plan[PHONE_SOURCES_FIELD][0]
    media = [
        MediaRef(
            lane="clip",
            media_id="source",
            gcs_path=binding["proxy_path"],
            generation="123",
            kind="video",
            duration_s=10,
        )
    ]
    snapshot = EditProposalSnapshot(
        direction="fast_montage",
        duration_s=3,
        title="A short scene",
        media=media,
        fast_cuts=[
            FastMontageCut(
                cut_id=f"cut-{index}",
                media_id="source",
                source_start_s=2 + index,
                source_end_s=3 + index,
                output_duration_s=1,
                role="hook",
            )
            for index in range(3)
        ],
        story_beats=[StoryBeat(beat_id="story", topic="Scene", media_ids=["source"], duration_s=3)],
    )
    guided = {
        "proposal_version": 1,
        "media_digest": canonical_media_digest(media),
        "approved_proposal": snapshot.model_dump(mode="json"),
        "media_identities": [
            {
                key: getattr(media[0], key)
                for key in ("lane", "media_id", "gcs_path", "generation", "kind")
            }
        ],
    }
    plan = compile_execution_plan(guided, track=None)
    job.assembly_plan.update(guided_edit=guided, guided_story_execution_plan=plan)
    variant = job.assembly_plan["variants"][0]
    variant["text_elements"] = plan["text_elements"]
    revision = gj._guided_v2_revision(job, variant)
    assert revision is not None
    return revision


def test_phone_save_atomically_pins_revision_without_cloud_dispatch(monkeypatch):
    job = phone_job(monkeypatch)
    old = device_status(job, "guided_story").request
    prep = save(job)
    new = device_status(job, "guided_story").request
    assert new.identity.recipe_revision == old.identity.recipe_revision + 1
    assert new.identity.recipe_digest != old.identity.recipe_digest
    assert new.recipe.text_layers[0].effect == "fade-in"
    assert "After" in new.model_dump_json()
    assert job.assembly_plan["variants"][0]["render_status"] == "awaiting_device"
    assert prep["render_destination"] == "device"
    with patch("app.tasks.generative_build.regenerate_generative_variant.apply_async") as cloud:
        gj.enqueue_editor_commit_render(str(job.id), "guided_story", prep)
    cloud.assert_not_called()
    before = copy.deepcopy(job.assembly_plan)
    with pytest.raises(HTTPException) as stale:
        save(job)
    assert stale.value.status_code == 409
    assert job.assembly_plan == before
    save(job, generation=prep["generation"])
    assert device_status(job, "guided_story").request.identity.recipe_revision == 3


def test_phone_save_recompiles_with_the_cleaned_narration_bed(monkeypatch):
    """A "Clean up speech" story keeps playing its derivative after a native Save.

    Save never re-downloads the voiceover: it reuses the bed pinned in the
    previous immutable recipe, which for a cleaned story is the WAV derivative
    at its cleaned duration -- the raw recording must not come back.
    """
    from tests.pipeline.test_phone_guided_plan import narration_bed
    from tests.tasks.test_phone_guided_dispatch import DERIVATIVE_GENERATION, cleaned_narration

    narration = cleaned_narration()
    bed = narration_bed(generation=DERIVATIVE_GENERATION, duration_s=narration.duration_s)
    job = phone_job(monkeypatch, narration=narration, bed=bed)
    before = device_status(job, "guided_story").request

    save(job)

    after = device_status(job, "guided_story").request
    assert after.identity.recipe_revision == before.identity.recipe_revision + 1
    assert "After" in after.model_dump_json()
    voices = [a for a in after.recipe.asset_manifest.assets if a.kind == "voiceover"]
    assert voices == [a for a in before.recipe.asset_manifest.assets if a.kind == "voiceover"]
    assert voices[0].generation == DERIVATIVE_GENERATION
    [track] = [t for t in after.recipe.tracks if t.kind == "audio"]
    [clip] = track.clips
    assert (clip.source_asset_id, clip.source_start) == (voices[0].id, 0)
    assert clip.source_duration == pytest.approx(narration.duration_s)
    assert after.recipe.audio.narration_asset_id == voices[0].id


@pytest.mark.parametrize("failure", ["capability", "cohort", "rollback", "binding"])
def test_failed_native_compile_leaves_entire_baseline_untouched(monkeypatch, failure):
    job = phone_job(monkeypatch)
    if failure == "capability":
        monkeypatch.setattr(
            gj.settings,
            "phone_render_verified_features",
            ["basicComposition", "local1080Export", "positionedText", "audioMix"],
        )
    elif failure == "cohort":
        monkeypatch.setattr(gj.settings, "phone_render_user_ids", [uuid.uuid4()])
    elif failure == "rollback":
        monkeypatch.setattr(gj.settings, "phone_rendering_enabled", False)
    elif failure == "binding":
        job.assembly_plan[PHONE_SOURCES_FIELD][0]["generation"] = "changed"
    before = copy.deepcopy(vars(job))
    with pytest.raises(HTTPException) as error:
        save(job, effect="fade-in")
    assert error.value.status_code == 422
    assert vars(job) == before


@pytest.mark.parametrize("effect", ["typewriter", "stream-in", "smooth-type", "staggered-slice"])
def test_reveal_save_stays_on_device(monkeypatch, effect):
    job = phone_job(monkeypatch)
    monkeypatch.setattr(gj.settings, "text_motion_v2_enabled", True)
    prep = save(job, effect=effect, motion={"version": 2} if effect == "smooth-type" else None)
    request = device_status(job, "guided_story").request
    layer = request.recipe.text_layers[0]
    assert layer.effect == effect
    content = layer.staggered or layer.smooth_reveal or layer.discrete_reveal
    assert content.text == "After"
    assert request.identity.recipe_revision == 2
    with patch("app.tasks.generative_build.regenerate_generative_variant.apply_async") as cloud:
        gj.enqueue_editor_commit_render(str(job.id), "guided_story", prep)
    cloud.assert_not_called()


@pytest.mark.parametrize("guided_v2", [False, True], ids=["legacy", "guided-v2"])
def test_phone_save_persists_highlight_preset_and_bumps_generation(monkeypatch, guided_v2):
    job = phone_job(monkeypatch)
    monkeypatch.setattr(
        gj.settings,
        "phone_render_verified_features",
        [*gj.settings.phone_render_verified_features, "authoredText"],
    )
    revision = _enable_guided_v2(job, monkeypatch) if guided_v2 else None

    prep = save(
        job,
        guided_revision_number=revision["revision_number"] if revision else None,
        editor_preset="Highlight",
        background_color="#FFF0A6",
        font_family="Inter",
        color="#30352C",
    )
    request = device_status(job, "guided_story").request
    layer = request.recipe.text_layers[0]
    assert prep["generation"] != "first"
    assert request.identity.recipe_revision == 2
    assert layer.runs[0].fill.red == pytest.approx(0x30 / 255)
    assert layer.runs[0].fill.green == pytest.approx(0x35 / 255)
    assert layer.runs[0].fill.blue == pytest.approx(0x2C / 255)
    assert layer.background is not None
    assert layer.background.color.red == pytest.approx(1.0)
    assert layer.background.color.green == pytest.approx(0xF0 / 255)
    assert layer.background.color.blue == pytest.approx(0xA6 / 255)


@pytest.mark.parametrize(
    ("phase", "value"),
    [("entrance", value) for value in ("none", "fade", "pop", "slide", "typewriter")]
    + [("exit", value) for value in ("none", "fade", "pop", "slide", "typewriter")]
    + [("loop", value) for value in ("none", "pulse", "bounce", "float")],
)
@pytest.mark.parametrize("guided_v2", [False, True], ids=["legacy", "guided-v2"])
def test_phone_save_persists_every_native_text_phase(monkeypatch, phase, value, guided_v2):
    job = phone_job(monkeypatch)
    monkeypatch.setattr(
        gj.settings,
        "phone_render_verified_features",
        [*gj.settings.phone_render_verified_features, "authoredText"],
    )
    revision = _enable_guided_v2(job, monkeypatch) if guided_v2 else None
    phases = {"entrance": "none", "exit": "none", "loop": "none", "speed": 1}
    phases[phase] = value
    prep = save(
        job,
        guided_revision_number=revision["revision_number"] if revision else None,
        animation_phases=phases,
    )
    request = device_status(job, "guided_story").request
    assert prep["generation"] != "first"
    assert request.identity.recipe_revision == 2
    assert getattr(request.recipe.text_layers[0].animation_phases, phase) == value


@pytest.mark.parametrize("speed", [0.25, 3.0])
@pytest.mark.parametrize("guided_v2", [False, True], ids=["legacy", "guided-v2"])
def test_phone_save_persists_native_text_animation_speed(monkeypatch, speed, guided_v2):
    job = phone_job(monkeypatch)
    monkeypatch.setattr(
        gj.settings,
        "phone_render_verified_features",
        [*gj.settings.phone_render_verified_features, "authoredText"],
    )
    revision = _enable_guided_v2(job, monkeypatch) if guided_v2 else None
    prep = save(
        job,
        guided_revision_number=revision["revision_number"] if revision else None,
        animation_phases={"entrance": "none", "exit": "none", "loop": "none", "speed": speed},
    )
    request = device_status(job, "guided_story").request
    assert prep["generation"] != "first"
    assert request.identity.recipe_revision == 2
    assert request.recipe.text_layers[0].animation_phases.speed == speed


@pytest.mark.parametrize(
    "element", [{"background_color": "#FFF0A6"}, {"animation_phases": {"entrance": "fade"}}]
)
def test_phone_authored_text_gate_off_leaves_save_unchanged(monkeypatch, element):
    job = phone_job(monkeypatch)
    before = copy.deepcopy(vars(job))
    with pytest.raises(HTTPException) as error:
        save(job, **element)
    assert error.value.status_code == 422
    assert "Authored text" in str(error.value.__cause__)
    assert vars(job) == before


def test_phone_guided_revision_compiles_trimmed_source_window(monkeypatch):
    from app.pipeline.guided_story import compile_execution_plan
    from app.schemas.edit_proposal import (
        EditProposalSnapshot,
        FastMontageCut,
        MediaRef,
        StoryBeat,
        canonical_media_digest,
    )

    job = phone_job(monkeypatch)
    monkeypatch.setattr(gj.settings, "guided_story_editor_v2_enabled", True)
    binding = job.assembly_plan[PHONE_SOURCES_FIELD][0]
    media = [
        MediaRef(
            lane="clip",
            media_id="source",
            gcs_path=binding["proxy_path"],
            generation="123",
            kind="video",
            duration_s=10,
        )
    ]
    snapshot = EditProposalSnapshot(
        direction="fast_montage",
        duration_s=3,
        title="A short scene",
        media=media,
        fast_cuts=[
            FastMontageCut(
                cut_id=f"cut-{index}",
                media_id="source",
                source_start_s=2 + index,
                source_end_s=3 + index,
                output_duration_s=1,
                role="hook",
            )
            for index in range(3)
        ],
        story_beats=[StoryBeat(beat_id="story", topic="Scene", media_ids=["source"], duration_s=3)],
    )
    guided = {
        "proposal_version": 1,
        "media_digest": canonical_media_digest(media),
        "approved_proposal": snapshot.model_dump(mode="json"),
        "media_identities": [
            {
                key: getattr(media[0], key)
                for key in ("lane", "media_id", "gcs_path", "generation", "kind")
            }
        ],
    }
    plan = compile_execution_plan(guided, track=None)
    job.assembly_plan.update(guided_edit=guided, guided_story_execution_plan=plan)
    variant = job.assembly_plan["variants"][0]
    variant["text_elements"] = plan["text_elements"]
    revision = gj._guided_v2_revision(job, variant)
    assert revision is not None
    segment = revision["segments"][0]
    prep = gj.prepare_editor_commit(
        job,
        "guided_story",
        gj.EditorCommitRequest(
            base_generation="first",
            guided_revision_number=revision["revision_number"],
            timeline_slots=[
                gj.TimelineSlotEdit(
                    slot_id=segment["segment_id"], clip_index=0, in_s=2.2, duration_s=0.8
                )
            ],
        ),
    )
    request = device_status(job, "guided_story").request
    clip = request.recipe.tracks[0].clips[0]
    assert (clip.source_start, clip.source_duration) == pytest.approx((2.2, 0.8))
    assert request.recipe.duration == pytest.approx(0.8)
    assert prep["revision_number"] == 2
    assert request.identity.recipe_revision == 2
    assert job.assembly_plan["variants"][0]["duration_s"] == pytest.approx(0.8)


@pytest.mark.parametrize("endpoint", ["timeline", "orientation", "text"])
async def test_legacy_phone_mutations_fail_before_state_or_queue_changes(monkeypatch, endpoint):
    from unittest.mock import AsyncMock, Mock

    job = phone_job(monkeypatch)
    monkeypatch.setattr(gj.settings, "guided_story_editor_v2_enabled", True)
    monkeypatch.setattr(gj, "_LANDSCAPE_OUTPUT_ENABLED", True)
    before = copy.deepcopy(vars(job))
    db = AsyncMock()
    db.execute.return_value = SimpleNamespace(scalar_one_or_none=lambda: job)
    cloud = Mock()
    monkeypatch.setattr("app.tasks.generative_build.regenerate_generative_variant.delay", cloud)
    with pytest.raises(HTTPException) as error:
        if endpoint == "timeline":
            await gj.dispatch_edit_timeline(job, "guided_story", SimpleNamespace(), db=db)
        elif endpoint == "orientation":
            await gj.dispatch_set_orientation(db, job, "guided_story", orientation="landscape")
        else:
            gj.dispatch_set_text_elements(job, "guided_story", elements=[], render=False)
    assert error.value.status_code == 422
    assert error.value.detail == {"code": "phone_editor_required"}
    assert vars(job) == before
    db.commit.assert_not_awaited()
    cloud.assert_not_called()


def test_text_only_phone_save_keeps_approved_timing_under_guided_v2(monkeypatch):
    # 2026-09-19 (job d9a965b0): with guided editor v2 on, every phone text
    # edit re-projected the timeline through compile_guided_runtime_plan, which
    # re-clocks moments/transitions onto 1/30 s frames; the phone compiler then
    # rejected the recipe ("phone transition offset must match cloud
    # milliseconds") and the creator saw a bare unsupported_phone_edit.
    from app.services import phone_editor

    job = phone_job(monkeypatch)
    monkeypatch.setattr(gj.settings, "guided_story_editor_v2_enabled", True)
    old = device_status(job, "guided_story").request

    def _boom(*_args):
        raise AssertionError("text-only saves must not re-clock the approved timeline")

    monkeypatch.setattr(phone_editor, "compile_guided_runtime_plan", _boom)

    def prepare(staged):
        variant = staged.assembly_plan["variants"][0]
        variant["text_elements"] = []  # the creator removed every text bar
        return {
            "has_render_section": True,
            "guided_revision": {"revision_number": 2},
            "sections": {"text_elements": True, "timeline": False},
            "generation": "second",
        }

    prep = prepare_phone_editor_commit(job, "guided_story", prepare=prepare)

    new = device_status(job, "guided_story").request
    assert prep["render_destination"] == "device"
    assert new.identity.recipe_revision == old.identity.recipe_revision + 1
    assert new.recipe.text_layers == []
    assert new.recipe.duration == old.recipe.duration
    assert job.assembly_plan["variants"][0]["render_status"] == "awaiting_device"


def test_unsupported_phone_edit_names_its_reason(monkeypatch):
    from app.services import phone_editor

    job = phone_job(monkeypatch)
    job.assembly_plan["guided_edit"] = {}
    monkeypatch.setattr(gj.settings, "guided_story_editor_v2_enabled", True)
    monkeypatch.setattr(
        phone_editor,
        "compile_guided_runtime_plan",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(ValueError("re-clocked timeline")),
    )
    with pytest.raises(HTTPException) as error:
        prepare_phone_editor_commit(
            job,
            "guided_story",
            prepare=lambda staged: {
                "has_render_section": True,
                "guided_revision": {"revision_number": 2},
                "sections": {"timeline": True},
                "generation": "second",
            },
        )
    assert error.value.status_code == 422
    assert error.value.detail["code"] == "unsupported_phone_edit"
    assert error.value.detail["reason"] == "ValueError: re-clocked timeline"


def visual_assets(request):
    return [asset for asset in request.recipe.asset_manifest.assets if asset.kind == "visual"]


def test_phone_resave_keeps_the_pinned_still(monkeypatch):
    job = phone_job(monkeypatch, photo=True)
    old = device_status(job, "guided_story").request
    save(job)
    new = device_status(job, "guided_story").request
    assert new.identity.recipe_revision == 2
    assert "After" in new.model_dump_json()
    assert visual_assets(new) == visual_assets(old) == [photos.photo_visual().render_asset()]
    still = new.recipe.tracks[0].clips[-1]
    assert (still.source_asset_id, still.source_start, still.timeline_start) == (
        f"visual-{photos.PHOTO_ID}",
        0,
        3,
    )
    assert job.assembly_plan[PHONE_VISUALS_FIELD] == [photos.photo_visual().model_dump(mode="json")]


@pytest.mark.parametrize("receipt", ["missing", "stale"])
def test_phone_resave_without_the_photo_receipt_fails_closed(monkeypatch, receipt):
    job = phone_job(monkeypatch, photo=True)
    if receipt == "missing":
        del job.assembly_plan[PHONE_VISUALS_FIELD]
    else:
        job.assembly_plan[PHONE_VISUALS_FIELD][0]["generation"] = "78"
    before = copy.deepcopy(vars(job))
    with pytest.raises(HTTPException) as error:
        save(job)
    assert error.value.status_code == 422
    assert error.value.detail["code"] == "unsupported_phone_edit"
    cause = "unsupported phone photo" if receipt == "missing" else "does not match its pinned"
    assert cause in str(error.value.__cause__)
    assert cause in error.value.detail["reason"]
    assert vars(job) == before


@pytest.mark.parametrize("bound", [False, True])
def test_phone_editor_cannot_place_an_unbound_approved_photo(monkeypatch, bound):
    """An approved-but-unused photo has no worker receipt, and the editor never
    hashes, so placing it fails closed. The bound control proves the 422 comes
    from the missing receipt, not from placing a photo at all."""
    from app.pipeline.guided_story import compile_execution_plan
    from app.schemas.edit_proposal import (
        EditProposalSnapshot,
        FastMontageCut,
        MediaRef,
        StoryBeat,
        canonical_media_digest,
    )

    job = phone_job(monkeypatch)
    monkeypatch.setattr(
        gj.settings,
        "phone_render_verified_features",
        [*gj.settings.phone_render_verified_features, "stillImages"],
    )
    monkeypatch.setattr(gj.settings, "guided_story_editor_v2_enabled", True)
    binding = job.assembly_plan[PHONE_SOURCES_FIELD][0]
    media = [
        MediaRef(
            lane="clip",
            media_id="source",
            gcs_path=binding["proxy_path"],
            generation="123",
            kind="video",
            duration_s=10,
        ),
        MediaRef(
            lane="asset",
            media_id=photos.PHOTO_ID,
            gcs_path=photos.PHOTO_PATH,
            generation=photos.PHOTO_GENERATION,
            kind="image",
        ),
    ]
    snapshot = EditProposalSnapshot(
        direction="fast_montage",
        duration_s=3,
        title="A short scene",
        media=media,
        fast_cuts=[
            FastMontageCut(
                cut_id=f"cut-{index}",
                media_id="source",
                source_start_s=2 + index,
                source_end_s=3 + index,
                output_duration_s=1,
                role="hook",
            )
            for index in range(3)
        ],
        story_beats=[StoryBeat(beat_id="story", topic="Scene", media_ids=["source"], duration_s=3)],
    )
    guided = {
        "proposal_version": 1,
        "media_digest": canonical_media_digest(media),
        "approved_proposal": snapshot.model_dump(mode="json"),
        "media_identities": [
            {
                key: getattr(ref, key)
                for key in ("lane", "media_id", "gcs_path", "generation", "kind")
            }
            for ref in media
        ],
    }
    plan = compile_execution_plan(guided, track=None)
    assert photos.PHOTO_ID not in plan["selected_media_ids"]
    job.assembly_plan.update(guided_edit=guided, guided_story_execution_plan=plan)
    if bound:
        job.assembly_plan[PHONE_VISUALS_FIELD] = [photos.photo_visual().model_dump(mode="json")]
    variant = job.assembly_plan["variants"][0]
    variant["text_elements"] = plan["text_elements"]
    revision = gj._guided_v2_revision(job, variant)
    assert revision is not None
    photo_index = next(
        index
        for index, source in enumerate(revision["sources"])
        if source["media_id"] == photos.PHOTO_ID
    )
    commit = gj.EditorCommitRequest(
        base_generation="first",
        guided_revision_number=revision["revision_number"],
        timeline_slots=[
            gj.TimelineSlotEdit(
                slot_id=revision["segments"][0]["segment_id"], clip_index=0, in_s=2, duration_s=1
            ),
            gj.TimelineSlotEdit(slot_id=None, clip_index=photo_index, in_s=0, duration_s=1),
        ],
    )
    if not bound:
        before = copy.deepcopy(vars(job))
        with pytest.raises(HTTPException) as error:
            gj.prepare_editor_commit(job, "guided_story", commit)
        assert error.value.status_code == 422
        assert error.value.detail["code"] == "unsupported_phone_edit"
        assert "unsupported phone photo" in str(error.value.__cause__)
        assert vars(job) == before
        return
    gj.prepare_editor_commit(job, "guided_story", commit)
    request = device_status(job, "guided_story").request
    assert visual_assets(request) == [photos.photo_visual().render_asset()]
    assert request.recipe.tracks[0].clips[-1].source_asset_id == f"visual-{photos.PHOTO_ID}"
