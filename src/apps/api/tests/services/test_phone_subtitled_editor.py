import pytest

from app.agents._schemas.media_overlay import MediaOverlay
from app.agents._schemas.sound_effect import SoundEffectPlacement
from app.pipeline.phone_subtitled_lanes import PhoneSubtitledLanes, SubtitledEndingClip
from app.pipeline.phone_subtitled_plan import compile_phone_subtitled_plan
from app.services.device_render import DEVICE_RENDER_FIELD, pin_device_request
from app.services.phone_sources import PHONE_VISUALS_FIELD
from app.services.phone_subtitled_editor import (
    PHONE_SUBTITLED_EDITOR_LANES_FIELD,
    is_phone_subtitled_editor_variant,
    lanes_from_editor_sections,
    lanes_from_recipe,
    project_phone_subtitled_editor_sections,
    sections_from_lanes,
)
from tests.pipeline.test_phone_subtitled_plan import (
    PHOTO_ID,
    PHOTO_PATH,
    VIDEO_ID,
    VIDEO_PATH,
    _binding,
    _overlay_card,
    _photo_visual,
    _pool_video_visual,
    _resolved_sfx,
    _sfx_asset,
)


def _pinned_assembly_plan(recipe, *, variant_id="subtitled", visuals=()):
    import uuid
    from types import SimpleNamespace

    from app.kria.device_render import make_device_request

    job = SimpleNamespace(id=uuid.uuid4(), assembly_plan={})
    request = make_device_request(job_id=job.id, variant_id=variant_id, revision=1, recipe=recipe)
    pin_device_request(job, request, base_generation="first")
    assembly = job.assembly_plan
    if visuals:
        assembly[PHONE_VISUALS_FIELD] = [v.model_dump(mode="json") for v in visuals]
    return assembly


# --- is_phone_subtitled_editor_variant ----------------------------------------


def test_is_phone_subtitled_editor_variant_true_for_device_subtitled():
    assert is_phone_subtitled_editor_variant(
        {"render_destination": "device", "resolved_archetype": "subtitled"}
    )


@pytest.mark.parametrize(
    "variant",
    [
        {"render_destination": "device", "resolved_archetype": "guided_story"},
        {"render_destination": "cloud", "resolved_archetype": "subtitled"},
        {"resolved_archetype": "subtitled"},
        {},
        None,
    ],
)
def test_is_phone_subtitled_editor_variant_false_otherwise(variant):
    assert is_phone_subtitled_editor_variant(variant) is False


# --- lanes_from_recipe / projection round-trip --------------------------------


def test_lanes_from_recipe_round_trips_overlay_sfx_and_ending_clip():
    bindings = (_binding(duration_s=10.0),)
    photo = _photo_visual()
    video = _pool_video_visual()
    card = _overlay_card(id="card-1", start_s=1.0, end_s=3.0, x_frac=0.6, y_frac=0.3, scale=0.4)
    sfx = _resolved_sfx()
    ending = SubtitledEndingClip(media_id=VIDEO_ID, gcs_path=VIDEO_PATH, generation="88")
    lanes = PhoneSubtitledLanes(overlays=[card], sound_effects=[sfx], ending_clip=ending)
    recipe = compile_phone_subtitled_plan(
        bindings, caption_cues=[], visuals=(photo, video), lanes=lanes
    )

    derived = lanes_from_recipe(recipe, visuals=(photo, video))

    assert len(derived.overlays) == 1
    assert derived.overlays[0].media_id == PHOTO_ID
    assert derived.overlays[0].gcs_path == PHOTO_PATH
    assert derived.overlays[0].x_frac == pytest.approx(0.6)
    assert derived.overlays[0].y_frac == pytest.approx(0.3)

    assert len(derived.sound_effects) == 1
    assert derived.sound_effects[0].asset.catalog_id == "pop"
    assert derived.sound_effects[0].request.at_s == pytest.approx(1.0)

    assert derived.ending_clip is not None
    assert derived.ending_clip.media_id == VIDEO_ID
    assert derived.ending_clip.gcs_path == VIDEO_PATH


def test_lanes_from_recipe_drops_a_clip_whose_visual_is_unpinned():
    bindings = (_binding(duration_s=10.0),)
    photo = _photo_visual()
    card = _overlay_card()
    recipe = compile_phone_subtitled_plan(
        bindings,
        caption_cues=[],
        visuals=(photo,),
        lanes=PhoneSubtitledLanes(overlays=[card]),
    )
    # No visuals passed this time -- the overlay clip's visual can't be matched.
    derived = lanes_from_recipe(recipe, visuals=())
    assert derived.overlays == []


# --- project_phone_subtitled_editor_sections ----------------------------------


def test_project_sections_returns_none_when_unpinned():
    assert project_phone_subtitled_editor_sections({}, {"variant_id": "subtitled"}) is None
    assert (
        project_phone_subtitled_editor_sections({DEVICE_RENDER_FIELD: {}}, {"variant_id": "x"})
        is None
    )


def test_project_sections_derives_from_the_pinned_recipe_when_nothing_persisted():
    bindings = (_binding(duration_s=10.0),)
    photo = _photo_visual()
    card = _overlay_card()
    sfx = _resolved_sfx()
    recipe = compile_phone_subtitled_plan(
        bindings,
        caption_cues=[],
        visuals=(photo,),
        lanes=PhoneSubtitledLanes(overlays=[card], sound_effects=[sfx]),
    )
    assembly = _pinned_assembly_plan(recipe, visuals=(photo,))
    variant = {"variant_id": "subtitled", "render_destination": "device"}

    sections = project_phone_subtitled_editor_sections(assembly, variant)

    assert sections is not None
    assert len(sections["sound_effects"]) == 1
    assert sections["sound_effects"][0]["sound_effect_id"] == "pop"
    assert len(sections["media_overlays"]) == 1
    assert sections["media_overlays"][0]["src_gcs_path"] == PHOTO_PATH


def test_project_sections_prefers_the_persisted_lanes_field_over_the_recipe():
    bindings = (_binding(duration_s=10.0),)
    photo = _photo_visual()
    # Pinned recipe carries one card...
    recipe = compile_phone_subtitled_plan(
        bindings,
        caption_cues=[],
        visuals=(photo,),
        lanes=PhoneSubtitledLanes(overlays=[_overlay_card(id="stale-card")]),
    )
    assembly = _pinned_assembly_plan(recipe, visuals=(photo,))
    # ...but the persisted editor-lanes field is newer/different and must win.
    fresh_lanes = PhoneSubtitledLanes(sound_effects=[_resolved_sfx()])
    variant = {
        "variant_id": "subtitled",
        "render_destination": "device",
        PHONE_SUBTITLED_EDITOR_LANES_FIELD: {
            "version": 1,
            "caption_style": "sentence",
            "lanes": fresh_lanes.model_dump(mode="json"),
            "labels": {"pop": "Pop!"},
        },
    }

    sections = project_phone_subtitled_editor_sections(assembly, variant)

    assert sections["media_overlays"] == []
    assert len(sections["sound_effects"]) == 1
    assert sections["sound_effects"][0]["label"] == "Pop!"


def test_projected_sections_validate_against_the_generic_schemas():
    """The projected dicts must round-trip through the exact models the
    native editor and the generic editor-commit validators already use."""
    card = _overlay_card()
    sfx = _resolved_sfx()
    lanes = PhoneSubtitledLanes(overlays=[card], sound_effects=[sfx])
    sections = sections_from_lanes(lanes, labels={"pop": "Pop"})

    for item in sections["media_overlays"]:
        MediaOverlay.model_validate(item)
    for item in sections["sound_effects"]:
        SoundEffectPlacement.model_validate(item)


# --- lanes_from_editor_sections ------------------------------------------------


def _fake_inspect(catalog_id: str, *, generation: str = "9"):
    def inspect(path, *, asset_id, catalog, catalog_id=catalog_id):
        return _sfx_asset(catalog_id, generation=generation)

    return inspect


def test_lanes_from_editor_sections_moves_an_overlay_card():
    photo = _photo_visual()
    card = _overlay_card(id="card-1", x_frac=0.2)
    previous = PhoneSubtitledLanes(overlays=[card])
    sections = sections_from_lanes(previous, labels={})
    moved = [{**sections["media_overlays"][0], "x_frac": 0.9}]

    new_lanes = lanes_from_editor_sections(
        previous=previous,
        sound_effects=None,
        media_overlays=moved,
        visuals=(photo,),
    )

    assert len(new_lanes.overlays) == 1
    assert new_lanes.overlays[0].x_frac == pytest.approx(0.9)
    assert new_lanes.overlays[0].media_id == PHOTO_ID


def test_lanes_from_editor_sections_deletes_a_sound_effect():
    previous = PhoneSubtitledLanes(sound_effects=[_resolved_sfx()])

    new_lanes = lanes_from_editor_sections(
        previous=previous,
        sound_effects=[],
        media_overlays=None,
        visuals=(),
    )

    assert new_lanes.sound_effects == []


def test_lanes_from_editor_sections_adds_a_new_catalog_effect_via_inspect():
    previous = PhoneSubtitledLanes()
    committed = [
        {
            "id": "sfx-new",
            "sound_effect_id": "ding",
            "src_gcs_path": "sound-effects/ding/ding.m4a",
            "at_s": 2.0,
            "gain": 0.8,
            "duration_s": 1.5,
        }
    ]

    new_lanes = lanes_from_editor_sections(
        previous=previous,
        sound_effects=committed,
        media_overlays=None,
        visuals=(),
        inspect=_fake_inspect("ding"),
    )

    assert len(new_lanes.sound_effects) == 1
    resolved = new_lanes.sound_effects[0]
    assert resolved.asset.catalog_id == "ding"
    assert resolved.request.at_s == pytest.approx(2.0)
    assert resolved.request.volume == pytest.approx(0.8)
    assert resolved.duration_s == pytest.approx(1.5)


def test_lanes_from_editor_sections_carries_trim_through():
    previous = PhoneSubtitledLanes()
    committed = [
        {
            "id": "sfx-new",
            "sound_effect_id": "ding",
            "src_gcs_path": "sound-effects/ding/ding.m4a",
            "at_s": 2.0,
            "gain": 1.0,
            "duration_s": 3.0,
            "trim_start_s": 0.5,
            "trim_end_s": 2.0,
        }
    ]

    new_lanes = lanes_from_editor_sections(
        previous=previous,
        sound_effects=committed,
        media_overlays=None,
        visuals=(),
        inspect=_fake_inspect("ding"),
    )

    request = new_lanes.sound_effects[0].request
    assert request.trim_start_s == pytest.approx(0.5)
    assert request.trim_end_s == pytest.approx(2.0)


def test_lanes_from_editor_sections_reuses_previous_asset_for_a_pinned_catalog_id():
    """A catalog id already resolved in `previous` must not trigger a GCS
    probe -- `inspect` must never be called for it."""
    resolved = _resolved_sfx()
    previous = PhoneSubtitledLanes(sound_effects=[resolved])
    committed = [
        {
            "id": "sfx-moved",
            "sound_effect_id": "pop",
            "src_gcs_path": "sound-effects/pop/pop.m4a",
            "at_s": 5.0,
            "gain": 1.0,
            "duration_s": 1.0,
        }
    ]

    def _explode(*args, **kwargs):
        raise AssertionError("inspect should not be called for an already-pinned catalog id")

    new_lanes = lanes_from_editor_sections(
        previous=previous,
        sound_effects=committed,
        media_overlays=None,
        visuals=(),
        inspect=_explode,
    )

    assert new_lanes.sound_effects[0].asset == resolved.asset
    assert new_lanes.sound_effects[0].request.at_s == pytest.approx(5.0)


def test_lanes_from_editor_sections_rejects_an_unpinned_overlay_path():
    committed = [
        {
            "id": "card-new",
            "kind": "image",
            "src_gcs_path": "users/owner/plan/item/pool/not-pinned.jpg",
            "display_mode": "pip",
            "start_s": 0.0,
            "end_s": 2.0,
        }
    ]
    with pytest.raises(ValueError, match="isn't a photo Kria pinned"):
        lanes_from_editor_sections(
            previous=None,
            sound_effects=None,
            media_overlays=committed,
            visuals=(_photo_visual(),),
        )


def test_lanes_from_editor_sections_rejects_fullscreen_display_mode():
    photo = _photo_visual()
    committed = [
        {
            "id": "card-full",
            "kind": "image",
            "src_gcs_path": PHOTO_PATH,
            "display_mode": "fullscreen",
            "start_s": 0.0,
            "end_s": 2.0,
        }
    ]
    with pytest.raises(ValueError, match="picture-in-picture"):
        lanes_from_editor_sections(
            previous=None,
            sound_effects=None,
            media_overlays=committed,
            visuals=(photo,),
        )


def test_lanes_from_editor_sections_keeps_the_ending_clip_unchanged():
    ending = SubtitledEndingClip(media_id=VIDEO_ID, gcs_path=VIDEO_PATH, generation="88")
    previous = PhoneSubtitledLanes(ending_clip=ending)

    new_lanes = lanes_from_editor_sections(
        previous=previous,
        sound_effects=[],
        media_overlays=[],
        visuals=(),
    )

    assert new_lanes.ending_clip == ending


def test_lanes_from_editor_sections_none_sections_keep_previous_lanes_untouched():
    previous = PhoneSubtitledLanes(overlays=[_overlay_card()], sound_effects=[_resolved_sfx()])

    new_lanes = lanes_from_editor_sections(
        previous=previous,
        sound_effects=None,
        media_overlays=None,
        visuals=(),
    )

    assert new_lanes.overlays == previous.overlays
    assert new_lanes.sound_effects == previous.sound_effects


def test_lanes_from_editor_sections_rejects_a_non_catalog_sound_effect():
    committed = [
        {
            "id": "sfx-uploaded",
            "src_gcs_path": "users/owner/uploads/clip.m4a",
            "at_s": 0.0,
            "gain": 1.0,
        }
    ]
    with pytest.raises(ValueError, match="catalog"):
        lanes_from_editor_sections(
            previous=None,
            sound_effects=committed,
            media_overlays=None,
            visuals=(),
        )


# --- parent fixes: fade carry-over + real catalog paths ------------------------


def test_fade_is_carried_across_a_save_by_card_id_and_new_cards_are_static():
    """`MediaOverlay` has no fade token, so a fading card must keep its fade
    through a move (same id) while a card the creator adds is static."""
    photo = _photo_visual()
    fading = _overlay_card(id="fading", fade=True, x_frac=0.2)
    previous = PhoneSubtitledLanes(overlays=[fading])
    sections = sections_from_lanes(previous, labels={})
    projected = sections["media_overlays"][0]
    # The wire never claims a token the generic schema would reject.
    assert projected["entrance_token"] == "none"
    assert projected["exit_token"] == "none"
    MediaOverlay.model_validate(projected)

    moved = {**projected, "x_frac": 0.8}
    added = {**projected, "id": "added", "x_frac": 0.5}
    new_lanes = lanes_from_editor_sections(
        previous=previous,
        sound_effects=None,
        media_overlays=[moved, added],
        visuals=(photo,),
    )

    by_id = {card.id: card for card in new_lanes.overlays}
    assert by_id["fading"].fade is True
    assert by_id["fading"].x_frac == pytest.approx(0.8)
    assert by_id["added"].fade is False


def test_project_sections_uses_persisted_catalog_paths_for_sound_effects():
    lanes = PhoneSubtitledLanes(sound_effects=[_resolved_sfx()])
    catalog_id = lanes.sound_effects[0].asset.catalog_id
    real_path = f"sound-effects/{catalog_id}/audio.m4a"
    variant = {
        "variant_id": "subtitled",
        "render_destination": "device",
        PHONE_SUBTITLED_EDITOR_LANES_FIELD: {
            "version": 1,
            "caption_style": "sentence",
            "lanes": lanes.model_dump(mode="json"),
            "labels": {},
            "paths": {catalog_id: real_path},
        },
    }

    sections = project_phone_subtitled_editor_sections({}, variant)

    assert sections["sound_effects"][0]["src_gcs_path"] == real_path
    # Without a persisted path the projection falls back to the prefix-valid
    # placeholder (`_native_editor_assets` swaps in the catalog row's path).
    del variant[PHONE_SUBTITLED_EDITOR_LANES_FIELD]["paths"]
    fallback = project_phone_subtitled_editor_sections({}, variant)
    assert fallback["sound_effects"][0]["src_gcs_path"].startswith(f"sound-effects/{catalog_id}/")
