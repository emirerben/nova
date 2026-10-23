"""Start a narration-aligned original clip afresh after its full-screen photo.

The photo takes over the existing picture interval; it never inserts time into
the narration. Resolve only against the final compiled lanes, after any failed
lane was removed. A missing/ambiguous placement is recovery, not a PiP fallback.
"""

from __future__ import annotations

import copy
from collections.abc import Mapping, Sequence
from typing import NoReturn

from app.kria.recipes_v2 import EditRecipeV2
from app.kria.render_assets import VisualRenderAsset
from app.pipeline.phone_guided_plan import UnsupportedPhonePlan
from app.services.phone_sources import PhoneSourceBinding


class PhotoSequenceUnavailable(UnsupportedPhonePlan):
    def __init__(self, trigger: str, reason: str):
        self.trigger = trigger
        next_step = {
            "photo_not_placed": "Please select the photo again or choose a different spoken cue.",
            "wrong_photo": "Please select the photo you want to show at this cue.",
            "following_clip_not_unique": "Please choose one original video to follow this photo.",
            "conflicting_sequences": "Please choose a different video for each photo sequence.",
        }.get(reason, "Please choose a shorter photo hold or a different spoken cue.")
        super().__init__(
            f'I could not place the full-screen photo followed by its video at "{trigger}". '
            + next_step,
            reason=reason,
        )


def compile_narrated_photo_sequences(
    recipe: EditRecipeV2,
    *,
    beats: Sequence[Mapping],
    bindings: Sequence[PhoneSourceBinding],
) -> tuple[EditRecipeV2, dict[str, dict]]:
    """Preserve every cut outside the photo and restart the following clip.

    Only an already-aligned, uniquely selected original can follow the photo.
    Splitting its hidden prefix preserves source speed, previous pictures, and
    every other lane. The visible suffix starts at the original in-point, so
    its opening frames are not consumed behind the photo. This uses existing
    recipe fields and survives the editor's lane replacement/save operation.
    """
    requests = [beat for beat in beats if beat.get("following_clip_id")]
    if not requests:
        return recipe, {}
    main = next((track for track in recipe.tracks if track.id == "narrated"), None)
    fullscreen = next(
        (track for track in recipe.tracks if track.id == "subtitled-fullscreen"), None
    )
    asset_ids = {binding.media_id: binding.render_asset().id for binding in bindings}
    replacements: dict[str, list] = {}
    evidence: dict[str, dict] = {}
    for beat in requests:
        trigger = str(beat.get("trigger") or "")

        def unavailable(reason: str) -> NoReturn:
            raise PhotoSequenceUnavailable(trigger, reason)

        if beat.get("display_mode") != "fullscreen" or beat.get("occurrence", "first") != "first":
            unavailable("invalid_sequence")
        if main is None or fullscreen is None:
            unavailable("photo_not_placed")
        beat_id = str(beat.get("beat_id") or "")
        if beat_id in evidence:
            unavailable("conflicting_sequences")
        photo_id = f"subtitled-overlay-beat-{beat_id}-1"
        photo = next((clip for clip in fullscreen.clips if clip.id == photo_id), None)
        if photo is None or photo.visual_placement is None:
            unavailable("photo_not_placed")
        photo_asset = next(
            (asset for asset in recipe.asset_manifest.assets if asset.id == photo.source_asset_id),
            None,
        )
        # Creator manifests prefix pool references with `asset-`; grounding
        # binds the raw pool id. Accept the same two identities as grounding,
        # while still rejecting a different photo at the identical timestamp.
        requested_visual = str(beat.get("visual_id") or "")
        visual_ids = {requested_visual}
        if requested_visual.casefold().startswith("asset-"):
            visual_ids.add(requested_visual[len("asset-") :])
        if (
            not isinstance(photo_asset, VisualRenderAsset)
            or photo_asset.media_kind != "image"
            or photo_asset.visual_id not in visual_ids
        ):
            unavailable("wrong_photo")
        placement = photo.visual_placement
        if placement.width_fraction is not None:
            unavailable("photo_not_fullscreen")
        photo_start, photo_end = placement.window_start, placement.window_end
        hold = float(beat.get("hold_s") or 1.5)
        if abs(photo_end - photo_start - hold) > 0.04:
            unavailable("photo_hold_shortened")
        if placement.fade_in or placement.fade_out:
            unavailable("photo_not_opaque")
        target = str(beat["following_clip_id"])
        target_asset = asset_ids.get(target)
        candidates = [clip for clip in main.clips if clip.source_asset_id == target_asset]
        if len(candidates) != 1:
            unavailable("following_clip_not_unique")
        clip = candidates[0]
        end = clip.timeline_start + clip.source_duration / clip.rate
        # Do not move another scene, show the video before its photo, or eat
        # the next shot/the pinned ending to make a sequence fit.
        if photo_start > clip.timeline_start + 0.04:
            unavailable("following_clip_already_started")
        if photo_end < clip.timeline_start or end - photo_end < 0.3:
            unavailable("no_room_for_following_clip")
        # Later intentional cutaways are allowed; report only the uninterrupted
        # visible suffix. Independent sequences may target different originals.
        visible_end = end
        for other in fullscreen.clips:
            if other.id == photo.id or other.visual_placement is None:
                continue
            other_window = other.visual_placement
            if other_window.window_end > photo_end and other_window.window_start < end:
                visible_end = min(visible_end, max(photo_end, other_window.window_start))
        if visible_end - photo_end < 0.3:
            unavailable("following_clip_covered")
        if clip.id in replacements:
            unavailable("conflicting_sequences")
        visible = clip.model_copy(
            update={
                "id": f"{clip.id[:125]}-after-photo-{len(evidence)}",
                "timeline_start": photo_end,
                "source_duration": (end - photo_end) * clip.rate,
            }
        )
        split = []
        if photo_end > clip.timeline_start:
            split.append(
                clip.model_copy(
                    update={"source_duration": (photo_end - clip.timeline_start) * clip.rate}
                )
            )
        split.append(visible)
        replacements[clip.id] = split
        evidence[beat_id] = {
            "sequence_photo_id": photo_asset.visual_id,
            "following_clip_id": target,
            "following_at_s": round(photo_end, 3),
            "following_end_s": round(visible_end, 3),
            "following_source_start_s": round(visible.source_start, 3),
            "sequence_realized": True,
        }
    tracks = [
        track.model_copy(
            update={
                "clips": [
                    part for clip in track.clips for part in replacements.get(clip.id, [clip])
                ]
            }
        )
        if track.id == "narrated"
        else track
        for track in recipe.tracks
    ]
    result = recipe.model_copy(update={"tracks": tracks})
    return EditRecipeV2.model_validate(result.model_dump(mode="json")), evidence


def refresh_narrated_sequence_receipt(recipe: EditRecipeV2, receipt: object) -> object:
    """A later editor save cannot keep a success receipt for a changed sequence.

    Read the final recipe without restarting any source again. Caption/title
    saves preserve valid evidence; removing/moving/replacing either picture
    makes the original sequence unverified instead of claiming stale success.
    """
    if not isinstance(receipt, dict) or not any(
        isinstance(row, dict) and row.get("following_clip_id")
        for row in receipt.get("placed") or []
    ):
        return receipt
    result = copy.deepcopy(receipt)
    photos = next(
        (track.clips for track in recipe.tracks if track.id == "subtitled-fullscreen"), []
    )
    main = next((track.clips for track in recipe.tracks if track.id == "narrated"), [])
    assets = {asset.id: asset for asset in recipe.asset_manifest.assets}
    for row in result.get("placed") or []:
        if not row.get("following_clip_id"):
            continue
        row["sequence_realized"] = False
        photo = next((c for c in photos if c.id == f"subtitled-overlay-{row.get('card_id')}"), None)
        if photo is None or photo.visual_placement is None:
            continue
        window = photo.visual_placement
        asset = assets.get(photo.source_asset_id)
        if (
            not isinstance(asset, VisualRenderAsset)
            or asset.visual_id != row.get("sequence_photo_id")
            or asset.media_kind != "image"
            or window.width_fraction is not None
            or window.fade_in
            or window.fade_out
            or round(window.window_start, 3) != row.get("at_s")
            or round(window.window_end, 3) != row.get("end_s")
        ):
            continue
        following = next(
            (
                c
                for c in main
                if (
                    c.source_asset_id == row["following_clip_id"]
                    and abs(c.timeline_start - window.window_end) < 0.001
                    and round(c.source_start, 3) == row.get("following_source_start_s")
                )
            ),
            None,
        )
        if following is None:
            continue
        end = following.timeline_start + following.source_duration / following.rate
        end = min(
            [
                end,
                *(
                    max(window.window_end, c.visual_placement.window_start)
                    for c in photos
                    if c.id != photo.id
                    and c.visual_placement is not None
                    and c.visual_placement.window_end > window.window_end
                ),
            ]
        )
        if end - window.window_end >= 0.3:
            row["sequence_realized"] = True
            row["following_end_s"] = round(end, 3)
    return result
