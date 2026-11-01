"""KRI-1139: a Visuals-pool photo or video behind a clip (`backdrop.kind == "media"`).

The device draws it (`ClipBackdropMediaCompositionTests` renders pixels); here: the recipe contract
(`tests/fixtures/clip-tilt-parity/v1.json` is shared with Swift), the manifest declaration every
phone writer must make, the gates and the update-required answer for a build that cannot decode it.
"""

import pytest
from pydantic import ValidationError

from app.config import Settings, settings
from app.kria.clip_tilt_row import canonical_row_tilt
from app.kria.device_render import recipe_digest
from app.kria.recipes import ClipBackdrop, EditRecipeV1
from app.kria.recipes_v2 import EditRecipeV2
from app.pipeline.phone_authored_timeline import compile_phone_authored_timeline
from app.pipeline.phone_guided_plan import UnsupportedPhonePlan, compile_phone_guided_plan
from app.pipeline.phone_recipe_shared import clip_tilt_fields
from app.pipeline.phone_subtitled_plan import compile_phone_subtitled_plan
from app.pipeline.phone_voiceover_cut import replace_voiceover_cut
from app.routes.generative_jobs import TimelineSlotEdit
from app.services.authored_editor import resolve_authored_phone_slots
from app.services.client_protocol import (
    CLIP_BACKDROP_MEDIA_PROTOCOL,
    clear_request_context,
    set_client_protocol,
)
from app.services.phone_rollout import (
    PhoneCapabilityUnavailable,
    clip_backdrop_media_supported,
    recipe_has_clip_backdrop_media,
    validate_phone_pilot_recipe,
)
from app.services.phone_sources import PHONE_VISUALS_FIELD
from tests.kria.test_clip_tilt import POOL_VISUAL_ID, tilted_document
from tests.pipeline import test_authored_timeline as authored
from tests.pipeline import test_phone_voiceover_cut as voiceover_cut
from tests.pipeline import test_phone_voiceover_montage_plan as montage_plan
from tests.pipeline.test_phone_guided_plan import (
    PHOTO_ID,
    pool_video,
)
from tests.pipeline.test_phone_guided_plan import fixture as guided_fixture
from tests.pipeline.test_phone_subtitled_plan import _photo_visual

MEDIA = {"kind": "media", "asset_id": "photo-1"}
TILT = {"rotation_deg": 15.0, "backdrop": MEDIA}
VISUAL = _photo_visual()
WRITERS = [
    "guided",
    "authored",
    "voiceover_cut_narrated",
    "voiceover_cut_montage",
    "montage",
    "subtitled",
]


@pytest.fixture(autouse=True)
def _request_context():
    clear_request_context()
    yield
    clear_request_context()


def _clip(recipe):
    return next(c for track in recipe.tracks if track.kind == "video" for c in track.clips)


def _guided(tilt, visuals):
    plan, bindings = guided_fixture()
    if tilt is not None:
        plan.story_timeline[0].tilt = tilt
    return compile_phone_guided_plan(plan, bindings, visuals=visuals)


def _authored(tilt, visuals):
    job, variant, previous = authored.fixture()
    job.assembly_plan[PHONE_VISUALS_FIELD] = [v.model_dump(mode="json") for v in visuals]
    edit = {"tilt": tilt} if tilt is not None else {}
    variant["user_timeline"]["slots"] = resolve_authored_phone_slots(
        job,
        variant,
        [TimelineSlotEdit(slot_id="added", clip_index=1, in_s=2, duration_s=3, **edit)],
    )
    return compile_phone_authored_timeline(job, variant, previous)


def _voiceover_cut(archetype, tilt, visuals):
    recipe = voiceover_cut._recipe()
    if archetype == "voiceover":
        recipe = voiceover_cut._montage(recipe)
        slots = voiceover_cut._montage_slots(recipe)
    else:
        slots = voiceover_cut._narrated_slots(recipe)
    if tilt is not None:
        slots[0]["tilt"] = tilt
    return replace_voiceover_cut(
        recipe,
        archetype=archetype,
        slots=slots,
        bindings=voiceover_cut._BINDINGS,
        pool=voiceover_cut._POOL,
        visuals=visuals,
    )


def _montage(tilt, visuals):
    decision, bindings = montage_plan.fixture()
    first = f"step-0-{decision.assembly_steps[0].clip_id}"
    return montage_plan._compile(
        decision,
        bindings,
        narration=montage_plan._narration(),
        clip_tilts={first: tilt} if tilt is not None else None,
        visuals=visuals,
    )


def _subtitled(tilt, visuals):
    from tests.pipeline.test_authored_timeline import binding

    return compile_phone_subtitled_plan(
        (binding("old"),),
        caption_cues=[],
        clip_tilts={"clip-0": tilt} if tilt is not None else None,
        visuals=visuals,
    )


COMPILE = {
    "guided": _guided,
    "authored": _authored,
    "voiceover_cut_narrated": lambda tilt, visuals: _voiceover_cut("narrated", tilt, visuals),
    "voiceover_cut_montage": lambda tilt, visuals: _voiceover_cut("voiceover", tilt, visuals),
    "montage": _montage,
    "subtitled": _subtitled,
}


# --- recipe contract --------------------------------------------------------------------


def test_a_media_backdrop_names_the_pool_visual_the_manifest_declares():
    recipe = EditRecipeV2.model_validate(
        tilted_document(
            rotation=30,
            backdrop={"kind": "media", "asset_id": POOL_VISUAL_ID},
            pool_media_kind="image",
        )
    )
    assert {"clipTilt", "clipBackdropMedia", "stillImages"} <= recipe.required_capabilities
    clip = recipe.model_dump(mode="json")["tracks"][0]["clips"][0]
    assert clip["backdrop"] == {"kind": "media", "asset_id": POOL_VISUAL_ID}
    assert EditRecipeV2.model_validate_json(recipe.model_dump_json()) == recipe
    assert recipe_has_clip_backdrop_media(recipe)


def test_a_media_backdrop_refuses_an_asset_the_manifest_does_not_declare():
    for pool_kind in (None, "image"):
        document = tilted_document(
            rotation=30,
            backdrop={"kind": "media", "asset_id": "somebody-elses"},
            pool_media_kind=pool_kind,
        )
        with pytest.raises(ValidationError):
            EditRecipeV2.model_validate(document)


def test_a_media_backdrop_needs_a_v2_recipe():
    document = tilted_document(
        rotation=30, backdrop={"kind": "media", "asset_id": POOL_VISUAL_ID}, pool_media_kind="image"
    )
    document["schema_version"] = 1
    for key in ("asset_manifest",):
        document.pop(key)
    with pytest.raises(ValidationError):
        EditRecipeV1.model_validate(document)


def test_blank_colour_and_blur_recipes_keep_their_bytes_and_capabilities():
    for backdrop in ({"kind": "blank"}, {"kind": "blur"}, {"kind": "color", "color": "#112233"}):
        recipe = EditRecipeV2.model_validate(tilted_document(rotation=30, backdrop=backdrop))
        assert "clipBackdropMedia" not in recipe.required_capabilities
        assert not recipe_has_clip_backdrop_media(recipe)
        assert set(recipe.model_dump(mode="json")["tracks"][0]["clips"][0]["backdrop"]) <= {
            "kind",
            "color",
        }
    assert ClipBackdrop(kind="color", color="#112233").model_dump(mode="json") == {
        "kind": "color",
        "color": "#112233",
    }


def test_the_media_backdrop_changes_the_recipe_digest_with_its_asset():
    first = EditRecipeV2.model_validate(
        tilted_document(
            rotation=30,
            backdrop={"kind": "media", "asset_id": POOL_VISUAL_ID},
            pool_media_kind="image",
        )
    )
    video = EditRecipeV2.model_validate(
        tilted_document(
            rotation=30,
            backdrop={"kind": "media", "asset_id": POOL_VISUAL_ID},
            pool_media_kind="video",
        )
    )
    assert recipe_digest(first) != recipe_digest(video)
    assert "visualVideos" in video.required_capabilities


# --- every phone writer ----------------------------------------------------------------


@pytest.mark.parametrize("writer", WRITERS)
def test_every_phone_writer_declares_the_pool_photo_and_keeps_the_backdrop(writer):
    recipe = COMPILE[writer](TILT, (VISUAL,))
    clip = _clip(recipe)
    assert clip.backdrop == ClipBackdrop(kind="media", asset_id="photo-1")
    assert clip.transform.rotation_degrees == 15.0
    declared = {a.id: a for a in recipe.asset_manifest.assets}[f"visual-{VISUAL.media_id}"]
    assert declared.visual_id == "photo-1" and declared.generation == VISUAL.generation
    assert f"visual-{VISUAL.media_id}" in {asset.id for asset in recipe.assets}
    assert {"clipTilt", "clipBackdropMedia", "stillImages"} <= recipe.required_capabilities


@pytest.mark.parametrize("writer", WRITERS)
def test_every_phone_writer_fails_closed_on_a_backdrop_the_job_never_pinned(writer):
    with pytest.raises((UnsupportedPhonePlan, ValueError)) as refused:
        COMPILE[writer](TILT, ())
    assert "pinned" in str(refused.value) or "clipBackdropMedia" in str(
        getattr(refused.value, "capability", "")
    )


@pytest.mark.parametrize("writer", WRITERS)
def test_a_blur_tilt_compiles_exactly_as_before_even_with_visuals_pinned(writer):
    blur = {"rotation_deg": 15.0, "backdrop": {"kind": "blur"}}
    with_visuals = COMPILE[writer](blur, (VISUAL,))
    assert recipe_digest(with_visuals) == recipe_digest(COMPILE[writer](blur, ()))
    assert "clipBackdropMedia" not in with_visuals.required_capabilities


def test_a_guided_video_backdrop_declares_the_video_and_its_duration():
    video = pool_video()
    plan, bindings = guided_fixture()
    plan.story_timeline[0].tilt = {
        "rotation_deg": 5,
        "backdrop": {"kind": "media", "asset_id": video.media_id},
    }
    recipe = compile_phone_guided_plan(plan, bindings, visuals=(video,))
    asset = next(a for a in recipe.assets if a.id == f"visual-{video.media_id}")
    assert asset.duration == 8
    assert {a.id: a for a in recipe.asset_manifest.assets}[asset.id].media_kind == "video"
    assert "visualVideos" in recipe.required_capabilities


def test_a_look_on_the_clip_refuses_the_backdrop_like_any_tilt():
    plan, bindings = guided_fixture()
    plan.story_timeline[0].tilt = {
        "rotation_deg": 0,
        "backdrop": {"kind": "media", "asset_id": PHOTO_ID},
    }
    plan.story_timeline[0].look_preset = "golden_hour"
    with pytest.raises(UnsupportedPhonePlan):
        compile_phone_guided_plan(plan, bindings, visuals=(_photo_visual(media_id=PHOTO_ID),))


def test_a_later_cut_drops_the_declaration_when_the_backdrop_goes():
    media = _voiceover_cut("voiceover", TILT, (VISUAL,))
    slots = voiceover_cut._montage_slots(media)
    for slot in slots:
        slot["tilt"] = None
    again = replace_voiceover_cut(
        media,
        archetype="voiceover",
        slots=slots,
        bindings=voiceover_cut._BINDINGS,
        pool=voiceover_cut._POOL,
        visuals=(VISUAL,),
    )
    assert f"visual-{VISUAL.media_id}" not in {a.id for a in again.assets}
    assert f"visual-{VISUAL.media_id}" not in {a.id for a in again.asset_manifest.assets}
    assert "clipBackdropMedia" not in again.required_capabilities
    assert recipe_digest(again) == recipe_digest(_voiceover_cut("voiceover", None, ()))


def test_the_row_value_is_the_recipe_value():
    assert canonical_row_tilt(TILT) == TILT
    patch, backdrop = clip_tilt_fields({"tilt": TILT})
    assert patch == {"rotation_degrees": 15.0}
    assert backdrop.model_dump(mode="json") == MEDIA


# --- gates -----------------------------------------------------------------------------


def test_the_gate_extends_the_tilt_gate_and_needs_its_own_token_and_build(monkeypatch):
    def gate(*, enabled=True, tilt=True, media=True, protocol=29, require_client=True):
        monkeypatch.setattr(settings, "media_tilt_enabled", enabled)
        monkeypatch.setattr(
            settings,
            "phone_render_verified_features",
            (["clipTilt"] if tilt else []) + (["clipBackdropMedia"] if media else []),
        )
        monkeypatch.setattr(
            "app.services.client_protocol.current_client_protocol", lambda: protocol
        )
        return clip_backdrop_media_supported(require_client=require_client)

    assert gate()
    assert not gate(enabled=False)
    assert not gate(tilt=False)
    assert not gate(media=False)
    assert not gate(protocol=28)
    assert not gate(protocol=None)
    assert gate(protocol=None, require_client=False)


def test_the_flag_and_protocol_defaults():
    fields = Settings.model_fields
    assert (
        fields["media_backdrop_min_client_protocol"].default == CLIP_BACKDROP_MEDIA_PROTOCOL == 29
    )
    assert fields["media_tilt_enabled"].default is False


def test_pilot_validation_refuses_an_unverified_media_backdrop(monkeypatch):
    recipe = EditRecipeV2.model_validate(
        tilted_document(
            rotation=30,
            backdrop={"kind": "media", "asset_id": POOL_VISUAL_ID},
            pool_media_kind="image",
        )
    )
    verified = [*recipe.required_capabilities]
    monkeypatch.setattr(settings, "phone_render_verified_features", verified)
    validate_phone_pilot_recipe(recipe)
    monkeypatch.setattr(
        settings,
        "phone_render_verified_features",
        [c for c in verified if c != "clipBackdropMedia"],
    )
    with pytest.raises(PhoneCapabilityUnavailable) as refused:
        validate_phone_pilot_recipe(recipe)
    assert refused.value.capability == "clipBackdropMedia"


def test_an_old_build_is_told_to_update_instead_of_decoding_a_media_backdrop():
    from types import SimpleNamespace

    from app.routes import device_render

    def detail(document, protocol):
        set_client_protocol(protocol)
        recipe = EditRecipeV2.model_validate(document)
        status = SimpleNamespace(request=SimpleNamespace(recipe=recipe))
        return device_render._client_update_detail(SimpleNamespace(assembly_plan={}), {}, status)

    media = tilted_document(
        rotation=30, backdrop={"kind": "media", "asset_id": POOL_VISUAL_ID}, pool_media_kind="image"
    )
    blur = tilted_document(rotation=30, backdrop={"kind": "blur"})
    assert detail(media, 29) is None
    assert detail(blur, 28) is None
    assert detail(media, 28) == {
        "code": "client_update_required",
        "feature": "clip_backdrop_media",
        "minimum_client_protocol": 29,
    }
    assert detail(media, 27)["feature"] == "clip_tilt"
