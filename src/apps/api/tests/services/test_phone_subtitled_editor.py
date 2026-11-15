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
    _CUES,
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
    _video_overlay_card,
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


def test_lanes_from_editor_sections_accepts_a_fullscreen_card_and_honours_the_kill_switch(
    monkeypatch,
):
    """KRI-661: a card may be promoted to full screen -- unless the rollout switch is off."""
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
    lanes = lanes_from_editor_sections(
        previous=None, sound_effects=None, media_overlays=committed, visuals=(photo,)
    )
    assert [c.display_mode for c in lanes.overlays] == ["fullscreen"]

    from app.config import settings

    monkeypatch.setattr(settings, "phone_subtitled_fullscreen_overlays_enabled", False)
    with pytest.raises(ValueError, match="turned off"):
        lanes_from_editor_sections(
            previous=None, sound_effects=None, media_overlays=committed, visuals=(photo,)
        )
    # A card that was already full screen still round-trips with the switch off.
    kept = lanes_from_editor_sections(
        previous=lanes, sound_effects=None, media_overlays=committed, visuals=(photo,)
    )
    assert [c.display_mode for c in kept.overlays] == ["fullscreen"]


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


def _ducked_recipe(volume: float = 1.0):
    from app.pipeline.phone_subtitled_lanes import SubtitledSoundEffect

    sfx = _resolved_sfx(
        request=SubtitledSoundEffect(id="sfx-1", catalog_id="pop", at_s=1.0, volume=volume)
    )
    lanes = PhoneSubtitledLanes(sound_effects=[sfx])
    recipe = compile_phone_subtitled_plan(
        (_binding(duration_s=10.0),), caption_cues=_CUES, lanes=lanes, duck_sfx_under_speech=True
    )
    return lanes, recipe


def test_lanes_from_recipe_restores_the_pre_duck_volume_from_the_receipt():
    from app.pipeline.phone_subtitled_plan import sfx_duck_receipt

    lanes, recipe = _ducked_recipe(volume=0.8)
    receipt = sfx_duck_receipt(lanes, recipe)
    restored = lanes_from_recipe(recipe, visuals=(), duck_receipt=receipt)
    assert restored.sound_effects[0].request.volume == pytest.approx(0.8)
    # Without the receipt the ducked value is all the recipe knows.
    bare = lanes_from_recipe(recipe, visuals=())
    assert bare.sound_effects[0].request.volume == pytest.approx(0.28)


@pytest.mark.parametrize(
    "receipt",
    [
        {"gain": 0.35, "volumes": {"sfx-1": 0.5}},  # stale: 0.5 * 0.35 != 0.35
        {"gain": 0.35, "volumes": {"other": 1.0}},
        {"gain": True, "volumes": {"sfx-1": 1.0}},
        {"gain": 0.35, "volumes": {"sfx-1": "1.0"}},
        {"gain": 0.35, "volumes": ["sfx-1"]},
        "not-a-dict",
    ],
)
def test_lanes_from_recipe_ignores_a_stale_or_malformed_duck_receipt(receipt):
    _lanes, recipe = _ducked_recipe()
    derived = lanes_from_recipe(recipe, visuals=(), duck_receipt=receipt)
    assert derived.sound_effects[0].request.volume == pytest.approx(0.35)


# --- KRI-183: video PiP cards round-trip through the editor --------------------


def _video_card_recipe(*, source_start_s: float = 1.0):
    bindings = (_binding(duration_s=10.0),)
    video = _pool_video_visual(duration_s=6.0)
    card = _video_overlay_card(
        id="vid-1", start_s=2.0, end_s=4.0, x_frac=0.8, y_frac=0.2, source_start_s=source_start_s
    )
    recipe = compile_phone_subtitled_plan(
        bindings, caption_cues=[], visuals=(video,), lanes=PhoneSubtitledLanes(overlays=[card])
    )
    return recipe, video


def test_lanes_from_recipe_keeps_a_video_card_a_video_card():
    """Deriving lanes from a pinned recipe must not turn a video card into a
    photo card -- the recompile at Save would then reject the video visual,
    so every Save of that edit (even a sound-only one) would 422."""
    recipe, video = _video_card_recipe(source_start_s=1.0)

    derived = lanes_from_recipe(recipe, visuals=(video,))

    assert len(derived.overlays) == 1
    card = derived.overlays[0]
    assert card.kind == "video"
    assert card.media_id == VIDEO_ID
    assert card.source_start_s == pytest.approx(1.0)


def test_a_sound_only_save_recompiles_an_edit_carrying_a_video_card():
    recipe, video = _video_card_recipe()
    previous = lanes_from_recipe(recipe, visuals=(video,))

    lanes = lanes_from_editor_sections(
        previous=previous, sound_effects=None, media_overlays=None, visuals=(video,)
    )
    recompiled = compile_phone_subtitled_plan(
        (_binding(duration_s=10.0),), caption_cues=[], visuals=(video,), lanes=lanes
    )

    assert "visualVideos" in recompiled.required_capabilities


# --- KRI-1478: a strip card in the editor --------------------------------------
#
# The first render draws a wide card as a full-width strip cut to its text column
# (`source_crop` on the card clip). The phone editor draws a card from the lanes the server
# derives from that recipe: x / y / scale, the whole picture, no crop. Written BEFORE the fix.
# Ways this goes wrong:
# - the derived card keeps the strip's 0.94 width without its crop, so the editor shows (and a
#   Save bakes in) the whole page 0.94 wide and 0.30 tall, over her forehead;
# - the derived card keeps "no fade", so a Save is neither the strip nor today's card;
# - a Save re-emits a crop or a strip for a card the creator may have moved;
# - a card with no crop comes back changed (every existing edit).

_STRIP_COLUMN = {
    "left": 0.197,
    "right": 0.613,
    "line_pitch": 37 / 1080,
    "source_width": 1920,
    "source_height": 1080,
}
# Run 3: the whole 16:9 page fits 0.553 wide in the 0.175 of headroom above her face.
_TODAY_WIDTH = 0.175 * (16 / 9) / (1080 / 1920)
_TODAY_CARD = {
    "x_frac": 0.5,
    "y_frac": 0.1075,
    "scale": _TODAY_WIDTH,
    "fade": True,
    "caption_clear": True,
}
_STRIP = {"strip": {"scale": 0.94, "height": 0.175}}


def _card_recipe(kind: str, **card):
    if kind == "video":
        visual = _pool_video_visual(text_column=_STRIP_COLUMN, orientation_degrees=0)
        lane_card = _video_overlay_card(**card)
    else:
        visual = _photo_visual(text_column=_STRIP_COLUMN)
        lane_card = _overlay_card(**card)
    recipe = compile_phone_subtitled_plan(
        (_binding(duration_s=10.0),),
        caption_cues=[],
        visuals=(visual,),
        lanes=PhoneSubtitledLanes(overlays=[lane_card]),
    )
    return recipe, visual


def _card_clip(recipe):
    return next(t for t in recipe.tracks if t.id == "subtitled-overlays").clips[0]


def _saved(recipe, visual):
    """The recipe an untouched editor Save recompiles from ``recipe``."""
    previous = lanes_from_recipe(recipe, visuals=(visual,))
    sections = sections_from_lanes(previous, labels={})["media_overlays"]
    lanes = lanes_from_editor_sections(
        previous=previous, sound_effects=None, media_overlays=sections, visuals=(visual,)
    )
    saved = compile_phone_subtitled_plan(
        (_binding(duration_s=10.0),), caption_cues=[], visuals=(visual,), lanes=lanes
    )
    return saved, lanes, sections


def _wire(recipe, *, round_width: bool = False) -> dict:
    import json

    document = json.loads(recipe.model_dump_json())
    document["required_capabilities"] = sorted(document["required_capabilities"])
    if round_width:  # the crop is stored to six decimals, so the width comes back to ~1e-6
        for track in document["tracks"]:
            for clip in track["clips"]:
                placement = clip.get("visual_placement") or {}
                if placement.get("width_fraction") is not None:
                    placement["width_fraction"] = round(placement["width_fraction"], 5)
    return document


@pytest.mark.parametrize("kind", ["image", "video"])
def test_a_strip_card_opens_in_the_editor_as_todays_whole_picture_card(kind):
    strip_recipe, visual = _card_recipe(kind, **_TODAY_CARD, **_STRIP)
    today_recipe, _ = _card_recipe(kind, **_TODAY_CARD)
    clip = _card_clip(strip_recipe)
    assert clip.source_crop is not None and clip.visual_placement.width_fraction == 0.94

    (derived,) = lanes_from_recipe(strip_recipe, visuals=(visual,)).overlays
    (today,) = lanes_from_recipe(today_recipe, visuals=(visual,)).overlays

    assert _TODAY_WIDTH == pytest.approx(0.553, abs=1e-4)
    assert derived.scale == pytest.approx(_TODAY_WIDTH, abs=1e-5), "the whole page, not 0.94"
    assert derived.strip is None
    assert derived.model_dump() | {"scale": 0.0} == today.model_dump() | {"scale": 0.0}
    (section,) = sections_from_lanes(PhoneSubtitledLanes(overlays=[derived]), labels={})[
        "media_overlays"
    ]
    assert section["scale"] == pytest.approx(_TODAY_WIDTH, abs=1e-5)
    assert (section["x_frac"], section["y_frac"]) == (0.5, pytest.approx(0.1075))
    assert "source_crop" not in section and "strip" not in section
    MediaOverlay.model_validate(section)


@pytest.mark.parametrize("kind", ["image", "video"])
def test_a_save_from_a_strip_card_is_the_flag_off_card(kind):
    from app.kria.recipes_v2 import EditRecipeV2

    strip_recipe, visual = _card_recipe(kind, **_TODAY_CARD, **_STRIP)
    today_recipe, _ = _card_recipe(kind, **_TODAY_CARD)

    saved, lanes, _sections = _saved(strip_recipe, visual)

    assert lanes.overlays[0].strip is None
    clip = _card_clip(saved)
    assert clip.source_crop is None and "sourceCrop" not in saved.required_capabilities
    assert clip.visual_placement.fade_in is True and clip.visual_placement.fade_out is True
    assert EditRecipeV2.model_validate_json(saved.model_dump_json()) == saved
    # Exactly today's (flag-off) recipe, to the sixth decimal of the card's width.
    assert _wire(saved, round_width=True) == _wire(today_recipe, round_width=True)


@pytest.mark.parametrize("kind", ["image", "video"])
@pytest.mark.parametrize("fade", [True, False])
def test_a_card_without_a_crop_is_untouched_by_the_strip_path(kind, fade):
    recipe, visual = _card_recipe(kind, **_TODAY_CARD | {"fade": fade})
    placement = _card_clip(recipe).visual_placement

    (derived,) = lanes_from_recipe(recipe, visuals=(visual,)).overlays

    assert derived.scale == placement.width_fraction == _TODAY_WIDTH  # exact, not approximately
    assert (derived.x_frac, derived.y_frac, derived.fade) == (0.5, 0.1075, fade)
    saved, _lanes, _sections = _saved(recipe, visual)
    assert _wire(saved) == _wire(recipe), "an untouched Save recompiles the very same recipe"


def test_projected_video_card_reports_kind_video_and_validates():
    recipe, video = _video_card_recipe(source_start_s=1.5)
    sections = sections_from_lanes(lanes_from_recipe(recipe, visuals=(video,)), labels={})

    item = sections["media_overlays"][0]
    assert item["kind"] == "video"
    assert item["clip_trim_start_s"] == pytest.approx(1.5)
    MediaOverlay.model_validate(item)


def test_projected_photo_card_carries_no_clip_trim_field():
    sections = sections_from_lanes(PhoneSubtitledLanes(overlays=[_overlay_card()]), labels={})
    assert "clip_trim_start_s" not in sections["media_overlays"][0]
    assert sections["media_overlays"][0]["kind"] == "image"


def test_moving_an_existing_video_card_keeps_kind_and_source_start(monkeypatch):
    """Flag off after the render: moving a card that ALREADY rendered as a
    video card still Saves, keeping its kind and source start."""
    from app.config import settings

    monkeypatch.setattr(settings, "phone_subtitled_video_overlays_enabled", False)
    recipe, video = _video_card_recipe(source_start_s=1.0)
    previous = lanes_from_recipe(recipe, visuals=(video,))
    item = sections_from_lanes(previous, labels={})["media_overlays"][0]

    lanes = lanes_from_editor_sections(
        previous=previous,
        sound_effects=None,
        media_overlays=[{**item, "x_frac": 0.25}],
        visuals=(video,),
    )

    assert lanes.overlays[0].kind == "video"
    assert lanes.overlays[0].x_frac == pytest.approx(0.25)
    assert lanes.overlays[0].source_start_s == pytest.approx(1.0)


def test_a_new_video_card_is_rejected_while_the_video_gate_is_off(monkeypatch):
    from app.config import settings

    monkeypatch.setattr(settings, "phone_subtitled_video_overlays_enabled", False)
    video = _pool_video_visual()
    new_item = {
        "id": "new-vid",
        "kind": "video",
        "src_gcs_path": VIDEO_PATH,
        "display_mode": "pip",
        "start_s": 1.0,
        "end_s": 3.0,
    }

    with pytest.raises(ValueError, match="video cards aren't supported"):
        lanes_from_editor_sections(
            previous=PhoneSubtitledLanes(),
            sound_effects=None,
            media_overlays=[new_item],
            visuals=(video,),
        )


def test_a_new_video_card_is_accepted_while_the_video_gate_holds(monkeypatch):
    from app.config import settings
    from app.services.phone_rollout import (
        PHONE_SUBTITLED_OVERLAY_FEATURES,
        PHONE_SUBTITLED_VIDEO_OVERLAY_FEATURES,
    )

    monkeypatch.setattr(settings, "phone_subtitled_media_lanes_enabled", True)
    monkeypatch.setattr(settings, "media_overlays_enabled", True)
    monkeypatch.setattr(settings, "phone_subtitled_video_overlays_enabled", True)
    monkeypatch.setattr(
        settings,
        "phone_render_verified_features",
        list(PHONE_SUBTITLED_OVERLAY_FEATURES + PHONE_SUBTITLED_VIDEO_OVERLAY_FEATURES),
    )
    video = _pool_video_visual()
    new_item = {
        "id": "new-vid",
        "kind": "image",  # the pinned visual's kind wins over the payload's
        "src_gcs_path": VIDEO_PATH,
        "display_mode": "pip",
        "start_s": 1.0,
        "end_s": 3.0,
        "clip_trim_start_s": 0.5,
    }

    lanes = lanes_from_editor_sections(
        previous=PhoneSubtitledLanes(),
        sound_effects=None,
        media_overlays=[new_item],
        visuals=(video,),
    )

    assert lanes.overlays[0].kind == "video"
    assert lanes.overlays[0].source_start_s == pytest.approx(0.5)


def test_landscape_fit_from_recipe_round_trips_through_a_pinned_recompile():
    from app.pipeline.phone_subtitled_plan import landscape_fit_from_recipe

    wide = (_binding(width=1920, height=1080),)
    for fit in ("fit", "fill"):
        recipe = compile_phone_subtitled_plan(wide, caption_cues=_CUES, landscape_fit=fit)
        recompiled = compile_phone_subtitled_plan(
            wide, caption_cues=_CUES, landscape_fit=landscape_fit_from_recipe(recipe)
        )
        assert recompiled.model_dump_json() == recipe.model_dump_json()


def test_fullscreen_cards_round_trip_through_sections_and_recipe():
    """KRI-297: a recipe with Kria's full-screen cards must Save without
    raising or silently turning into pip."""
    from app.pipeline.phone_subtitled_lanes import SubtitledOverlayCard
    from app.pipeline.phone_subtitled_plan import compile_phone_subtitled_plan
    from app.services.phone_subtitled_editor import lanes_from_recipe, sections_from_lanes

    photo = _photo_visual()
    card = SubtitledOverlayCard(
        id="fs-0",
        media_id=photo.media_id,
        gcs_path=photo.gcs_path,
        generation=photo.generation,
        start_s=3.0,
        end_s=6.0,
        display_mode="fullscreen",
    )
    lanes = PhoneSubtitledLanes(overlays=[card])
    sections = sections_from_lanes(lanes, labels={})
    assert sections["media_overlays"][0]["display_mode"] == "fullscreen"

    again = lanes_from_editor_sections(
        previous=lanes,
        sound_effects=None,
        media_overlays=sections["media_overlays"],
        visuals=(photo,),
    )
    assert again.overlays[0].display_mode == "fullscreen"
    assert again.overlays[0].start_s == pytest.approx(3.0)

    # Pinned-recipe derivation (before any editor Save persisted lanes).
    from tests.pipeline.test_phone_subtitled_plan import _binding

    recipe = compile_phone_subtitled_plan(
        (_binding(duration_s=10.0),), caption_cues=[], visuals=(photo,), lanes=lanes
    )
    derived = lanes_from_recipe(recipe, visuals=(photo,))
    assert [c.display_mode for c in derived.overlays] == ["fullscreen"]
    assert derived.overlays[0].id == "fs-0"
