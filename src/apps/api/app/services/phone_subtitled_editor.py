"""Bridge the generic native-editor sections onto a phone-rendered `subtitled`
(Talking to camera) variant's own lane vocabulary (KRI-182 step 1).

A phone `subtitled` variant carries sticker/photo cards (`SubtitledOverlayCard`),
catalog sound effects (`SubtitledSoundEffect` -> `ResolvedSoundEffect`), and an
optional muted ending clip -- compiled by
`app.pipeline.phone_subtitled_plan.compile_phone_subtitled_plan` and pinned via
`app.services.device_render.pin_device_request`. The native editor, however,
only understands the generic `sound_effects` (`SoundEffectPlacement`) and
`media_overlays` (`MediaOverlay`) editor-commit sections
(`app.agents._schemas.sound_effect`/`media_overlay`). This module is the
translation layer both directions:

  - reading: `project_phone_subtitled_editor_sections` turns the current lane
    state (persisted, or derived from the pinned device recipe) into
    `SoundEffectPlacement`/`MediaOverlay`-shaped dicts the editor already
    understands.
  - writing: `lanes_from_editor_sections` turns a committed
    `sound_effects`/`media_overlays` section (plus the previous lane state)
    back into a `PhoneSubtitledLanes` ready for
    `compile_phone_subtitled_plan`.

Both directions are PURE and read-only against already-pinned state: no GCS
probe is ever made except `lanes_from_editor_sections`'s one allowed exception
-- a brand new catalog sound-effect id, resolved the same way
`app.tasks.generative_build._resolve_phone_sound_effect` does. "Save never
downloads or hashes media" otherwise; a moved card, a deleted effect, or a
re-timed placement all reuse the identity already pinned in the previous
recipe.

`app.services.phone_editor.prepare_phone_editor_commit` is the only writer of
`app.assembly_plan["variants"][i][PHONE_SUBTITLED_EDITOR_LANES_FIELD]`; every
other consumer of this module treats it as read-only derived state.
"""

from __future__ import annotations

from typing import Any

from pydantic import ValidationError

from app.kria.device_render import DeviceRenderStatus
from app.kria.recipes_v2 import EditRecipeV2
from app.kria.render_assets import LibraryRenderAsset, VisualRenderAsset
from app.pipeline.phone_subtitled_lanes import (
    PhoneSubtitledLanes,
    ResolvedSoundEffect,
    SubtitledEndingClip,
    SubtitledOverlayCard,
    SubtitledSoundEffect,
    sfx_path_is_playable,
)
from app.pipeline.phone_subtitled_plan import SFX_DUCK_RECEIPT_FIELD
from app.services.device_render import DEVICE_RENDER_FIELD
from app.services.phone_sources import PHONE_VISUALS_FIELD, PhoneVisualBinding
from app.services.render_library import inspect_library_asset

# Per-VARIANT field. Preferred over deriving from the pinned device recipe
# once a phone-editor Save has run at least once (see
# `project_phone_subtitled_editor_sections`).
PHONE_SUBTITLED_EDITOR_LANES_FIELD = "_phone_subtitled_editor_lanes_v1"

# Sound-effect catalog prefix every resolved effect's GCS object must live
# under -- mirrors `app.tasks.generative_build._resolve_phone_sound_effect`'s
# own check.
_SFX_CATALOG_PREFIX = "sound-effects/{catalog_id}/"


def is_phone_subtitled_editor_variant(variant: dict) -> bool:
    """True for a phone-rendered `subtitled` (Talking to camera) variant."""
    return (
        isinstance(variant, dict)
        and variant.get("render_destination") == "device"
        and variant.get("resolved_archetype") == "subtitled"
    )


def project_phone_subtitled_editor_sections(
    assembly_plan: dict, variant: dict
) -> dict[str, list[dict]] | None:
    """The variant's current lane state as native-editor sections.

    PURE, no I/O. Returns
    ``{"sound_effects": [...], "media_overlays": [...]}`` -- prefers the
    persisted ``variant[PHONE_SUBTITLED_EDITOR_LANES_FIELD]`` (written by a
    prior phone-editor Save), else derives from the pinned device request
    (``assembly_plan[DEVICE_RENDER_FIELD][variant_id]``). Returns ``None``
    when neither is available (no pinned recipe yet). Does NOT check the
    rollout flag -- callers gate with
    `app.services.phone_rollout.phone_subtitled_editor_lanes_supported()`.
    """
    if not isinstance(assembly_plan, dict) or not isinstance(variant, dict):
        return None

    persisted = variant.get(PHONE_SUBTITLED_EDITOR_LANES_FIELD)
    if isinstance(persisted, dict) and isinstance(persisted.get("lanes"), dict):
        try:
            lanes = PhoneSubtitledLanes.model_validate(persisted["lanes"])
        except ValidationError:
            lanes = None
        if lanes is not None:
            labels = persisted.get("labels")
            paths = persisted.get("paths")
            return sections_from_lanes(
                lanes,
                labels=labels if isinstance(labels, dict) else {},
                paths=paths if isinstance(paths, dict) else None,
            )

    variant_id = variant.get("variant_id")
    if not isinstance(variant_id, str) or not variant_id:
        return None
    recipe = _pinned_recipe(assembly_plan, variant_id)
    if recipe is None:
        return None
    lanes = lanes_from_recipe(
        recipe,
        visuals=_visual_bindings(assembly_plan),
        duck_receipt=variant.get(SFX_DUCK_RECEIPT_FIELD),
    )
    return sections_from_lanes(lanes, labels={})


def lanes_from_editor_sections(
    *,
    previous: PhoneSubtitledLanes | None,
    sound_effects: list[dict] | None,
    media_overlays: list[dict] | None,
    visuals: tuple[PhoneVisualBinding, ...],
    inspect: Any = None,
) -> PhoneSubtitledLanes:
    """Rebuild the lanes to compile from a committed editor Save.

    ``inspect`` defaults to `app.services.render_library.inspect_library_asset`
    (resolved at CALL time, not bound as a default value, so tests can
    monkeypatch this module's ``inspect_library_asset`` attribute); pass an
    injectable fake to test a new-catalog-id resolve without a real GCS call.

    ``sound_effects``/``media_overlays`` are the committed section payloads
    exactly as the route already validated/catalog-resolved them
    (``SoundEffectPlacement``/``MediaOverlay`` shape) -- ``None`` means that
    section was NOT part of this commit, so its lane carries over unchanged
    from ``previous``. ``previous`` is the lane state before this Save (the
    persisted lanes, or a `lanes_from_recipe` derivation) -- ``None`` when
    there is none yet (first-ever editor Save on this variant).

    A sound effect whose ``sound_effect_id`` was already resolved in
    ``previous`` reuses that exact `LibraryRenderAsset` identity (no GCS
    probe). A newly referenced catalog id is resolved with ``inspect`` (a
    `LibraryRenderAsset`-returning GCS metadata probe, no download) after the
    same playable-format and prefix checks
    `app.tasks.generative_build._resolve_phone_sound_effect` applies.

    The ending clip is never editable in this step -- it always carries
    forward from ``previous`` verbatim.

    Raises ``ValueError`` (never silently drops content) when: a committed
    sound effect doesn't reference the catalog, its audio format/path is
    unplayable/invalid, or its trim window is empty; a committed overlay
    card's ``display_mode`` isn't the pip default (fullscreen isn't a phone
    Talking lane); a committed overlay card's ``src_gcs_path`` doesn't
    match any photo Kria pinned for this Talking edit (adding NEW photos from
    the phone editor isn't supported yet); or it binds a pinned VIDEO visual
    that isn't already a video card while the KRI-183 video-PiP gate is off.
    """
    return PhoneSubtitledLanes(
        overlays=_overlays_from_sections(media_overlays, previous=previous, visuals=visuals),
        sound_effects=_sound_effects_from_sections(
            sound_effects, previous=previous, inspect=inspect or inspect_library_asset
        ),
        ending_clip=previous.ending_clip if previous is not None else None,
    )


def sections_from_lanes(
    lanes: PhoneSubtitledLanes,
    *,
    labels: dict[str, str],
    paths: dict[str, str] | None = None,
) -> dict[str, list[dict]]:
    """The inverse of `lanes_from_editor_sections`: lanes -> editor sections.

    ``labels`` maps a sound effect's ``catalog_id`` to its human-readable
    admin/catalog label and ``paths`` to its real catalog audio object
    (both persisted alongside the lanes by a phone-editor Save, so a
    re-projection can show a name and sign a playable preview instead of
    falling back to a bare catalog id / synthetic prefix path).
    """
    paths = paths or {}
    sound_effects: list[dict] = []
    for resolved in lanes.sound_effects:
        request = resolved.request
        catalog_id = resolved.asset.catalog_id
        item: dict[str, Any] = {
            "id": request.id,
            "sound_effect_id": catalog_id,
            # Recipes never carry storage paths (see app.kria.render_assets'
            # module docstring), so the exact original object name isn't
            # recoverable from a pinned recipe alone. This synthetic path
            # only needs to satisfy the `sound-effects/{catalog_id}/` prefix
            # contract `validate_sfx_gcs_path` checks -- a real signed
            # playback URL is a `_native_editor_assets` concern, and a probe
            # against a path that doesn't resolve to a real object just
            # drops that one preview row rather than breaking the projection.
            "src_gcs_path": paths.get(catalog_id)
            or _SFX_CATALOG_PREFIX.format(catalog_id=catalog_id) + catalog_id,
            "at_s": request.at_s,
            "gain": request.volume,
            "duration_s": resolved.duration_s,
            "label": labels.get(catalog_id, catalog_id),
            "source": "phone_lane",
        }
        if request.trim_start_s is not None:
            item["trim_start_s"] = request.trim_start_s
        if request.trim_end_s is not None:
            item["trim_end_s"] = request.trim_end_s
        sound_effects.append(item)

    media_overlays: list[dict] = []
    for card in lanes.overlays:
        # MediaOverlay's entrance/exit vocabulary is none/pop_in and
        # none/dissolve-out -- there is no "fade" token, and an unknown
        # entrance token fails `MediaOverlay` validation at Save. So a card's
        # `fade` is NOT expressed on the wire: it is carried across a Save by
        # id from the previous lanes (`_overlays_from_sections`), and both
        # tokens always project as "none" (pop-in is unqualified on the
        # phone anyway).
        entrance_token = "none"
        exit_token = "none"
        media_overlays.append(
            {
                "id": card.id,
                # KRI-183: a video card projects as `kind: "video"` with its
                # source start on `MediaOverlay.clip_trim_start_s`, so the
                # editor shows what actually renders and a Save round-trips it.
                "kind": card.kind,
                **({"clip_trim_start_s": card.source_start_s} if card.kind == "video" else {}),
                "src_gcs_path": card.gcs_path,
                "display_mode": "pip",
                "position": "custom",
                "x_frac": card.x_frac,
                "y_frac": card.y_frac,
                "scale": card.scale,
                "start_s": card.start_s,
                "end_s": card.end_s,
                "z": card.z,
                "entrance_token": entrance_token,
                "exit_token": exit_token,
                "source": "phone_lane",
            }
        )
    return {"sound_effects": sound_effects, "media_overlays": media_overlays}


def lanes_from_recipe(
    recipe: EditRecipeV2,
    *,
    visuals: tuple[PhoneVisualBinding, ...],
    duck_receipt: object = None,
) -> PhoneSubtitledLanes:
    """Reconstruct `PhoneSubtitledLanes` from an already-compiled recipe.

    Fallback source of truth used before a variant's first phone-editor Save
    (no `PHONE_SUBTITLED_EDITOR_LANES_FIELD` persisted yet). Lossy on
    anything the V2 recipe schema itself doesn't retain byte-for-byte (an
    overlay card's original ``z`` collapses to its compiled
    ``visual_placement.order``; a sound effect's original trim window
    collapses to its clamped ``source_start``/``source_duration``) -- exact
    once a Save persists the lanes field going forward.

    A clip whose visual can't be matched against ``visuals`` (job-level
    pinned `PhoneVisualBinding` rows) is dropped rather than guessed.

    ``duck_receipt`` is the variant's `SFX_DUCK_RECEIPT_FIELD`: a sound
    effect the speech duck lowered gets its requested (pre-duck) volume back,
    so the editor shows the creator's volume and a Save re-ducks it once
    instead of twice. An entry is honoured only while the pinned clip still
    carries exactly the ducked value it describes.
    """
    manifest = {asset.id: asset for asset in recipe.asset_manifest.assets}
    media_assets = {asset.id: asset for asset in recipe.assets}

    def visual_for(source_asset_id: str) -> PhoneVisualBinding | None:
        render_asset = manifest.get(source_asset_id)
        if not isinstance(render_asset, VisualRenderAsset):
            return None
        for visual in visuals:
            if (
                visual.media_id == render_asset.visual_id
                and visual.generation == render_asset.generation
            ):
                return visual
        return None

    ending_clip: SubtitledEndingClip | None = None
    overlays: list[SubtitledOverlayCard] = []
    sound_effects: list[ResolvedSoundEffect] = []

    for track in recipe.tracks:
        if track.id == "subtitled":
            for clip in track.clips:
                if clip.id != "clip-ending":
                    continue
                visual = visual_for(clip.source_asset_id)
                if visual is None:
                    continue
                ending_clip = SubtitledEndingClip(
                    media_id=visual.media_id,
                    gcs_path=visual.gcs_path,
                    generation=visual.generation,
                    trim_start_s=clip.source_start,
                    max_duration_s=clip.source_duration,
                )
        elif track.id == "subtitled-overlays":
            for clip in track.clips:
                placement = clip.visual_placement
                if placement is None:
                    continue
                visual = visual_for(clip.source_asset_id)
                if visual is None:
                    continue
                overlays.append(
                    SubtitledOverlayCard(
                        id=clip.id.removeprefix("subtitled-overlay-"),
                        media_id=visual.media_id,
                        gcs_path=visual.gcs_path,
                        generation=visual.generation,
                        start_s=placement.window_start,
                        end_s=placement.window_end,
                        x_frac=placement.x_fraction,
                        y_frac=placement.y_fraction,
                        scale=placement.width_fraction,
                        fade=bool(placement.fade_in or placement.fade_out),
                        z=max(placement.order - 1, 0),
                        # KRI-183: a video card compiled from a video visual
                        # keeps its kind + source start; deriving it as a
                        # photo card would make every later Save fail the
                        # compiler's kind check.
                        kind="video" if visual.kind == "video" else "image",
                        source_start_s=clip.source_start if visual.kind == "video" else 0.0,
                    )
                )
        elif track.id == "sfx":
            for clip in track.clips:
                asset = manifest.get(clip.source_asset_id)
                if not isinstance(asset, LibraryRenderAsset) or asset.catalog != "sound_effect":
                    continue
                media_asset = media_assets.get(asset.id)
                duration_s = (
                    media_asset.duration
                    if media_asset is not None and media_asset.duration
                    else clip.source_duration
                )
                request_id = clip.id.removeprefix("sfx-")
                sound_effects.append(
                    ResolvedSoundEffect(
                        request=SubtitledSoundEffect(
                            id=request_id,
                            catalog_id=asset.catalog_id,
                            at_s=clip.timeline_start,
                            volume=_unducked_volume(request_id, clip.volume, duck_receipt),
                        ),
                        asset=asset,
                        duration_s=duration_s,
                    )
                )

    return PhoneSubtitledLanes(
        overlays=overlays, sound_effects=sound_effects, ending_clip=ending_clip
    )


def _unducked_volume(request_id: str, volume: float, duck_receipt: object) -> float:
    if not isinstance(duck_receipt, dict):
        return volume
    gain, volumes = duck_receipt.get("gain"), duck_receipt.get("volumes")
    if not isinstance(volumes, dict) or not _is_number(gain):
        return volume
    requested = volumes.get(request_id)
    if not _is_number(requested):
        return volume
    if abs(round(float(requested) * float(gain), 4) - volume) > 1e-6:
        return volume
    return float(requested)


def _is_number(value: object) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool)


def _pinned_recipe(assembly_plan: dict, variant_id: str) -> EditRecipeV2 | None:
    records = assembly_plan.get(DEVICE_RENDER_FIELD)
    if not isinstance(records, dict):
        return None
    record = records.get(variant_id)
    if not isinstance(record, dict):
        return None
    raw_status = record.get("status")
    if not isinstance(raw_status, dict):
        return None
    try:
        status = DeviceRenderStatus.model_validate(raw_status)
    except ValidationError:
        return None
    recipe = status.request.recipe
    return recipe if isinstance(recipe, EditRecipeV2) else None


def _visual_bindings(assembly_plan: dict) -> tuple[PhoneVisualBinding, ...]:
    rows = assembly_plan.get(PHONE_VISUALS_FIELD)
    if not isinstance(rows, list):
        return ()
    bindings: list[PhoneVisualBinding] = []
    for row in rows:
        if not isinstance(row, dict):
            continue
        try:
            bindings.append(PhoneVisualBinding.model_validate(row))
        except ValidationError:
            continue
    return tuple(bindings)


def _overlays_from_sections(
    media_overlays: list[dict] | None,
    *,
    previous: PhoneSubtitledLanes | None,
    visuals: tuple[PhoneVisualBinding, ...],
) -> list[SubtitledOverlayCard]:
    if media_overlays is None:
        return list(previous.overlays) if previous is not None else []
    previous_by_id = {card.id: card for card in (previous.overlays if previous is not None else [])}
    cards: list[SubtitledOverlayCard] = []
    for item in media_overlays:
        if not isinstance(item, dict):
            raise ValueError("media overlay card must be an object")
        display_mode = item.get("display_mode", "pip")
        if display_mode != "pip":
            raise ValueError(
                "phone Talking edits only support picture-in-picture overlay cards -- "
                "fullscreen overlays aren't supported yet"
            )
        path = item.get("src_gcs_path")
        binding = next((visual for visual in visuals if visual.gcs_path == path), None)
        if binding is None:
            raise ValueError(
                "that photo isn't a photo Kria pinned for this Talking edit -- adding new "
                "photos to a phone Talking edit isn't supported yet"
            )
        card_id = item.get("id")
        if not isinstance(card_id, str) or not card_id:
            raise ValueError("media overlay card is missing its id")
        # `MediaOverlay` has no fade token (see `sections_from_lanes`), so a
        # card keeps the fade the worker/previous Save gave it; a card the
        # creator added in the editor is static.
        previous_card = previous_by_id.get(card_id)
        fade = previous_card.fade if previous_card is not None else False
        # KRI-183: the pinned visual's kind is the truth, not the payload's.
        # A video card is allowed when the video-PiP gate holds, or when this
        # same card already rendered as a video card (so a later flag
        # rollback stops NEW video cards without breaking Saves on edits
        # that already carry one). The recompile's device validation still
        # fails closed if `visualVideos` is no longer verified.
        source_start_s = 0.0
        if binding.kind == "video":
            already_video = (
                previous_card is not None
                and previous_card.kind == "video"
                and previous_card.media_id == binding.media_id
            )
            from app.services.phone_rollout import (  # noqa: PLC0415
                phone_subtitled_video_overlays_supported,
            )

            if not (already_video or phone_subtitled_video_overlays_supported()):
                raise ValueError(
                    "video cards aren't supported on phone Talking edits yet -- "
                    "use a photo for this card"
                )
            raw_start = item.get("clip_trim_start_s")
            if isinstance(raw_start, (int, float)) and not isinstance(raw_start, bool):
                source_start_s = max(float(raw_start), 0.0)
            elif previous_card is not None and previous_card.kind == "video":
                source_start_s = previous_card.source_start_s
        elif binding.kind != "image":
            raise ValueError("overlay cards require a photo or video visual")
        cards.append(
            SubtitledOverlayCard(
                id=card_id,
                media_id=binding.media_id,
                gcs_path=binding.gcs_path,
                generation=binding.generation,
                start_s=item.get("start_s", 0.0),
                end_s=item.get("end_s", 3.0),
                x_frac=item.get("x_frac", 0.5),
                y_frac=item.get("y_frac", 0.4),
                scale=item.get("scale", 0.35),
                fade=fade,
                z=max(int(item.get("z") or 0), 0),
                kind="video" if binding.kind == "video" else "image",
                source_start_s=source_start_s,
            )
        )
    return cards


def _sound_effects_from_sections(
    sound_effects: list[dict] | None,
    *,
    previous: PhoneSubtitledLanes | None,
    inspect: Any,
) -> list[ResolvedSoundEffect]:
    if sound_effects is None:
        return list(previous.sound_effects) if previous is not None else []
    previous_by_catalog_id = {
        resolved.asset.catalog_id: resolved
        for resolved in (previous.sound_effects if previous is not None else [])
    }
    resolved_list: list[ResolvedSoundEffect] = []
    for item in sound_effects:
        if not isinstance(item, dict):
            raise ValueError("sound effect placement must be an object")
        catalog_id = item.get("sound_effect_id")
        if not isinstance(catalog_id, str) or not catalog_id:
            raise ValueError(
                "phone Talking sound effects must reference the catalog -- custom uploads "
                "aren't supported yet"
            )
        request_id = item.get("id")
        if not isinstance(request_id, str) or not request_id:
            raise ValueError("sound effect placement is missing its id")
        request = SubtitledSoundEffect(
            id=request_id,
            catalog_id=catalog_id,
            at_s=item.get("at_s", 0.0),
            volume=item.get("gain", 1.0),
            trim_start_s=item.get("trim_start_s"),
            trim_end_s=item.get("trim_end_s"),
        )
        reused = previous_by_catalog_id.get(catalog_id)
        if reused is not None:
            resolved_list.append(
                ResolvedSoundEffect(
                    request=request, asset=reused.asset, duration_s=reused.duration_s
                )
            )
            continue
        resolved_list.append(_resolve_new_sound_effect(request, item, inspect=inspect))
    return resolved_list


def _resolve_new_sound_effect(
    request: SubtitledSoundEffect, item: dict, *, inspect: Any
) -> ResolvedSoundEffect:
    path = item.get("src_gcs_path")
    if not isinstance(path, str) or not path:
        raise ValueError("sound effect is missing its catalog audio path")
    if not sfx_path_is_playable(path):
        raise ValueError("sound effect format cannot play on the iPhone (needs m4a/wav/mp3/aac)")
    prefix = _SFX_CATALOG_PREFIX.format(catalog_id=request.catalog_id)
    if not path.startswith(prefix):
        raise ValueError("invalid render catalog path")
    duration_s = item.get("duration_s")
    if not isinstance(duration_s, (int, float)) or isinstance(duration_s, bool) or duration_s <= 0:
        raise ValueError("sound effect has no usable duration for phone rendering")
    asset = inspect(
        path,
        asset_id=f"sfx-{request.catalog_id}",
        catalog="sound_effect",
        catalog_id=request.catalog_id,
    )
    return ResolvedSoundEffect(request=request, asset=asset, duration_s=float(duration_s))
