"""Pinned creator requirements and a narrow verifier for portable phone recipes.

Approval pins objective requirements; the verifier reads the compiled timeline,
text and audio tracks. Unsupported evidence is an explicit refusal. This does
not claim native pixel/audio playback verification or cover every strategy field.
"""

from __future__ import annotations

import hashlib
import json
import unicodedata
from collections.abc import Mapping, Sequence
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, ValidationError

from app.agents._schemas.creator_agent import CreativeStrategy
from app.kria.brief import CreativeBrief
from app.kria.recipes_v2 import EditRecipeV2
from app.kria.render_assets import OriginalRenderAsset, VoiceoverRenderAsset
from app.pipeline.phone_guided_plan import UnsupportedPhonePlan

CONTRACT_FIELD = "creator_render_requirements"
REQUIREMENT_VERSION_FIELD = "creator_render_requirements_version"

# Every CreativeStrategy field has an explicit ownership note.  This is not a
# capability claim: only the small core projected by build_render_contract is
# verified here; the rest stays under its established planner/capability policy.
FIELD_ACCOUNTING: dict[str, str] = {
    "direction": "deferred capability policy",
    "edit_format": "deferred capability policy",
    "archetype": "deferred capability policy",
    "hero_media_id": "deferred capability policy",
    "audio_strategy": "audio requirement",
    "execution_contract": "deferred capability policy",
    "media_scope": "deferred capability policy",
    "story_structure": "deferred capability policy",
    "caption_style": "deferred capability policy",
    "intro_hook": "advisory",
    "opening_title": "exact text",
    "opening_title_duration_s": "exact text timing",
    "shot_labels": "exact text",
    "closing_title": "exact text",
    "font_family": "deferred capability policy",
    "text_color": "deferred capability policy",
    "clip_intents": "deferred capability policy",
    "resolved_clip_intents": "deferred capability policy",
    "ordering_choice": "order requirement",
    "reaction_beats": "deferred capability policy",
    "closing_media": "deferred capability policy",
    "song_sync": "deferred capability policy",
    "resolved_song_takes": "deferred capability policy",
    "image_layout": "deferred capability policy",
    "pacing": "deferred capability policy",
    "target_duration_s": "duration requirement",
    "render_program": "deferred capability policy",
    "selected_media_ids": "deferred capability policy",
    "optional_treatments": "deferred capability policy",
    "overlay_display": "deferred capability policy",
    "licensed_sfx": "deferred capability policy",
    "mixed_media_timing": "deferred capability policy",
    "montage_audio": "source-audio requirement",
    "video_reuse_policy": "deferred capability policy",
    "montage_cadence": "deferred capability policy",
    "rationale": "advisory",
    "target_duration_requested": "server-owned duration provenance",
}


class CreatorRenderContractError(UnsupportedPhonePlan):
    """A confirmed creator requirement is absent from a device recipe."""


class TextRequirement(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, allow_inf_nan=False)
    role: Literal["opening", "closing", "any", "clip"]
    text: str = Field(min_length=1, max_length=2000)
    media_id: str | None = None
    shot_index: int | None = Field(default=None, ge=0)
    duration_s: float | None = Field(default=None, gt=0)


class CreatorRenderContract(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, allow_inf_nan=False)
    version: Literal[1] = 1
    generation_id: str = Field(min_length=1)
    strategy_digest: str | None = None
    brief_digest: str | None = None
    duration_s: float | None = Field(default=None, gt=0)
    audio_source_ids: tuple[str, ...] = ()
    original_audio: Literal["forbid", "require"] | None = None
    require_voiceover: bool = False
    exact_texts: tuple[TextRequirement, ...] = ()
    order_ids: tuple[str, ...] = ()
    order_required: bool = False
    order_basis: str | None = None
    unresolved: tuple[str, ...] = ()
    digest: str = ""

    def rebind(self, **changes: object) -> CreatorRenderContract:
        """Return an explicitly changed contract with a fresh integrity digest."""
        data = self.model_dump(mode="json") | changes
        data.pop("digest", None)
        validated = CreatorRenderContract(**data)
        return validated.model_copy(update={"digest": _digest(validated.model_dump(mode="json"))})


def _normal(value: object) -> str:
    # Line wrapping is layout, not a change to the approved words.
    return " ".join(unicodedata.normalize("NFC", str(value)).split())


def _json_value(value: object) -> object:
    if isinstance(value, BaseModel):
        return value.model_dump(mode="json")
    if isinstance(value, tuple | list):
        return [_json_value(item) for item in value]
    if isinstance(value, dict):
        return {key: _json_value(item) for key, item in value.items()}
    return value


def _hash(value: object) -> str:
    return hashlib.sha256(
        json.dumps(
            _json_value(value), sort_keys=True, separators=(",", ":"), ensure_ascii=False
        ).encode()
    ).hexdigest()


def _digest(data: Mapping[str, Any]) -> str:
    return _hash({key: value for key, value in data.items() if key != "digest"})


def _strategy(raw: Mapping[str, Any] | None) -> CreativeStrategy | None:
    if raw is None:
        return None
    try:
        return CreativeStrategy.model_validate(raw)
    except ValidationError as exc:
        raise CreatorRenderContractError(
            "I couldn't verify this edit's confirmed requirements."
        ) from exc


def build_render_contract(
    strategy: Mapping[str, Any] | None,
    *,
    generation_id: str,
    brief: CreativeBrief | None = None,
    media_snapshot: Mapping[str, Any] | None = None,
    clip_order: Sequence[str] = (),
    has_voiceover: bool = False,
) -> CreatorRenderContract | None:
    """Pin only facts that a portable recipe can objectively demonstrate."""
    if strategy is None and brief is None:
        return None
    typed = _strategy(strategy)
    raw = dict(strategy or {})
    texts: list[TextRequirement] = []
    if typed is not None:
        if typed.opening_title:
            texts.append(
                TextRequirement(
                    role="opening",
                    text=typed.opening_title,
                    duration_s=typed.opening_title_duration_s,
                )
            )
        if typed.closing_title:
            texts.append(TextRequirement(role="closing", text=typed.closing_title))
        for index, text in enumerate(typed.shot_labels or ()):
            texts.append(TextRequirement(role="clip", text=text, shot_index=index))
    durations: list[float] = []
    if raw.get("target_duration_requested") is True and "target_duration_s" in raw:
        durations.append(float(raw["target_duration_s"]))
    order_required = bool(typed and typed.ordering_choice == "chronological")
    order_ids = tuple(str(item) for item in clip_order if str(item).strip())
    order_basis = "confirmed" if order_ids else None
    unresolved: list[str] = []
    if brief:
        for requirement in brief.live():
            if requirement.kind == "timing" and requirement.facts.get("duration_s") is not None:
                durations.append(float(requirement.facts["duration_s"]))
            if requirement.kind == "text" and requirement.literal:
                shot_index = None
                if requirement.is_shot_text:
                    labels = list(typed.shot_labels or ()) if typed else []
                    matching = [
                        index
                        for index, text in enumerate(labels)
                        if _normal(text) == _normal(requirement.literal)
                    ]
                    if len(matching) == 1:
                        shot_index = matching[0]
                    else:
                        unresolved.append(
                            "I need an explicit shot assignment for the confirmed text."
                        )
                role: Literal["opening", "closing", "any", "clip"] = (
                    "clip"
                    if requirement.scope.startswith("clip:") or requirement.scope == "per_clip"
                    else "any"
                )
                texts.append(
                    TextRequirement(
                        role=role,
                        text=requirement.literal,
                        media_id=requirement.scope[5:]
                        if requirement.scope.startswith("clip:")
                        else None,
                        shot_index=shot_index,
                    )
                )
            if requirement.kind == "order":
                order_required = True
                if requirement.facts.get("key") not in {"capture_time", "chronological"}:
                    unresolved.append("I can't verify this ordering rule from the approved media.")
    if order_required:
        from app.services.clip_facts import capture_from_assignment

        rows = (media_snapshot or {}).get("clip_assignments") or []
        selected = set(typed.selected_media_ids or ()) if typed else set()
        if selected:
            rows = [
                row for row in rows if isinstance(row, Mapping) and row.get("media_id") in selected
            ]
        dates = {}
        for row in rows:
            if not isinstance(row, Mapping) or not row.get("media_id"):
                continue
            capture = capture_from_assignment(row)
            if capture and capture.capture_time:
                dates[str(row["media_id"])] = capture.capture_time
        ids = [
            str(row["media_id"]) for row in rows if isinstance(row, Mapping) and row.get("media_id")
        ]
        if (
            not ids
            or len(ids) != len(rows)
            or len(set(ids)) != len(ids)
            or set(ids) != set(dates)
            or (selected and set(ids) != selected)
        ):
            order_ids = ()
            unresolved.append(
                "I need capture times for every selected clip to verify chronological order."
            )
        else:
            order_ids = tuple(sorted(ids, key=lambda media_id: dates[media_id]))
            order_basis = "capture_time"
    if durations and any(abs(value - durations[0]) > 0.001 for value in durations[1:]):
        raise CreatorRenderContractError(
            "Your confirmed edit lengths conflict, so I can't render it safely."
        )
    source_ids: tuple[str, ...] = ()
    original_audio: Literal["forbid", "require"] | None = None
    if typed:
        has_voiceover = typed.audio_strategy == "voiceover"
    if typed and typed.audio_strategy == "original_audio":
        original_audio = "require"
    if typed and typed.montage_audio:
        ids = getattr(typed.montage_audio, "source_media_ids", None) or []
        source_ids = (
            tuple(str(value) for value in ids) if typed.montage_audio.preserve_source_audio else ()
        )
        original_audio = "require" if typed.montage_audio.preserve_source_audio else "forbid"
    data: dict[str, Any] = dict(
        version=1,
        generation_id=generation_id,
        strategy_digest=_hash(raw) if strategy is not None else None,
        brief_digest=_hash(brief.model_dump(mode="json")) if brief else None,
        duration_s=durations[0] if durations else None,
        audio_source_ids=source_ids,
        original_audio=original_audio,
        require_voiceover=bool(has_voiceover),
        exact_texts=tuple(texts),
        order_ids=order_ids,
        order_required=order_required,
        order_basis=order_basis,
        unresolved=tuple(unresolved),
    )
    return CreatorRenderContract(**data).rebind()


def read_render_contract(assembly: Mapping[str, Any]) -> CreatorRenderContract | None:
    if CONTRACT_FIELD not in assembly:
        return None
    raw = assembly[CONTRACT_FIELD]
    try:
        contract = CreatorRenderContract.model_validate(raw)
    except ValidationError as exc:
        raise CreatorRenderContractError(
            "I couldn't read this edit's confirmed requirements."
        ) from exc
    if contract.digest != _digest(contract.model_dump(mode="json")):
        raise CreatorRenderContractError(
            "This edit's confirmed requirements changed; please try again."
        )
    return contract


def verify_phone_recipe(
    contract: CreatorRenderContract,
    recipe: EditRecipeV2,
    *,
    source_audio: Mapping[str, bool] | None = None,
) -> list[dict[str, Any]]:
    if contract.unresolved:
        raise CreatorRenderContractError(contract.unresolved[0])
    manifest = {asset.id: asset for asset in recipe.asset_manifest.assets}
    receipts: list[dict[str, Any]] = []
    if (
        contract.duration_s is not None
        and abs(recipe.duration - contract.duration_s) / contract.duration_s > 0.1
    ):
        raise CreatorRenderContractError("This edit couldn't keep the confirmed length.")

    def audible(track, clip) -> bool:
        if clip.volume <= 0 or (track.kind == "video" and recipe.audio.original_volume <= 0):
            return False
        # Mute windows use timeline time. Covering only part of a clip cannot
        # prove silence; adjacent windows can together cover the whole clip.
        start = clip.timeline_start
        end = start + clip.source_duration / clip.rate
        for window in sorted(recipe.audio.mute_windows, key=lambda item: item.start):
            if clip.id not in window.clip_ids or window.end <= start:
                continue
            if window.start > start:
                return True
            start = max(start, window.end)
            if start >= end:
                return False
        return start < end

    if contract.require_voiceover:
        voice_is_audible = any(
            track.kind == "audio"
            and audible(track, clip)
            and isinstance(manifest.get(clip.source_asset_id), VoiceoverRenderAsset)
            for track in recipe.tracks
            for clip in track.clips
        )
        if not voice_is_audible:
            raise CreatorRenderContractError(
                "This edit needs your recorded voice before it can render."
            )
    original_ids = {
        asset.media_id
        for asset in manifest.values()
        if isinstance(asset, OriginalRenderAsset)
        and any(
            (
                track.kind in {"video", "audio"}
                or (
                    track.kind == "overlay"
                    and clip.visual_placement is None
                    and clip.overlay_preserve_alpha is None
                )
            )
            and clip.source_asset_id == asset.id
            and audible(track, clip)
            and (source_audio is None or source_audio.get(asset.media_id) is True)
            for track in recipe.tracks
            for clip in track.clips
        )
    }
    if contract.original_audio == "forbid" and original_ids:
        raise CreatorRenderContractError("This edit can't use the camera audio you turned off.")
    if (contract.original_audio == "require" or contract.audio_source_ids) and source_audio is None:
        raise CreatorRenderContractError("I couldn't verify audio in the approved source files.")
    if contract.original_audio == "require" and not original_ids:
        raise CreatorRenderContractError("This edit needs audible camera audio.")
    if contract.audio_source_ids and original_ids != set(contract.audio_source_ids):
        raise CreatorRenderContractError(
            "This edit couldn't keep the confirmed camera-audio sources."
        )

    picture = sorted(
        [clip for track in recipe.tracks if track.kind == "video" for clip in track.clips],
        key=lambda clip: clip.timeline_start,
    )
    frame = 1 / recipe.frame_rate

    def _run_can_be_visible(run) -> bool:  # noqa: ANN001
        """Reject only layers the portable paint contract proves invisible."""

        if run.fill.alpha > 0 or (run.stroke_width > 0 and run.stroke.alpha > 0):
            return True
        if run.gradient is not None and any(stop.color.alpha > 0 for stop in run.gradient.stops):
            return True
        return any(blur.color.alpha > 0 for blur in run.blur_layers)

    def _layer_text(layer) -> str:  # noqa: ANN001
        runs = [unicodedata.normalize("NFC", run.text) for run in layer.runs]
        # Karaoke compilation emits one word per run on a shared baseline, so
        # its visual word boundaries are spaces. Ordinary runs on one baseline
        # may instead be styled spans of a single word and must stay adjacent.
        if layer.effect == "karaoke-line":
            return " ".join(runs)
        lines: list[list[str]] = []
        baseline: float | None = None
        for run, text in zip(layer.runs, runs, strict=True):
            if baseline is None or abs(run.baseline_y - baseline) > 0.001:
                lines.append([text])
                baseline = run.baseline_y
            else:
                lines[-1].append(text)
        return "\n".join("".join(line) for line in lines)

    def text_layers() -> list[tuple[str, float, float]]:
        return [
            (_normal(_layer_text(layer)), layer.start, layer.end)
            for layer in recipe.text_layers
            if layer.runs
            and all(
                _run_can_be_visible(run)
                or (
                    layer.karaoke is not None
                    and layer.karaoke.highlight.alpha > 0
                    and layer.karaoke.starts[index] < layer.end - layer.start
                )
                for index, run in enumerate(layer.runs)
                if run.text.strip()
            )
        ]

    rendered = text_layers()
    for requirement in contract.exact_texts:
        matches = [row for row in rendered if row[0] == _normal(requirement.text)]
        if not matches:
            raise CreatorRenderContractError("This edit is missing confirmed on-screen text.")
        if requirement.duration_s is not None:
            matches = [row for row in matches if row[2] - row[1] + frame >= requirement.duration_s]
            if not matches:
                raise CreatorRenderContractError(
                    "This edit couldn't keep confirmed text on screen long enough."
                )
        if requirement.role == "opening":
            matches = [row for row in matches if row[1] <= frame]
        elif requirement.role == "closing":
            matches = [row for row in matches if row[2] >= recipe.duration - frame]
        elif requirement.role == "clip":
            targets = picture
            if requirement.shot_index is not None:
                targets = picture[requirement.shot_index : requirement.shot_index + 1]
            elif requirement.media_id:
                targets = [
                    clip
                    for clip in picture
                    if isinstance(manifest.get(clip.source_asset_id), OriginalRenderAsset)
                    and manifest[clip.source_asset_id].media_id == requirement.media_id
                ]
            if not targets or any(
                not any(
                    start >= clip.timeline_start - frame
                    and end
                    <= clip.timeline_start
                    + clip.source_duration / clip.rate
                    + (clip.hold_duration or 0)
                    + frame
                    for _text, start, end in matches
                )
                for clip in targets
            ):
                matches = []
        if not matches:
            raise CreatorRenderContractError("This edit put confirmed text in the wrong place.")
    if contract.order_required:
        actual: list[str] = []
        for clip in picture:
            asset = manifest.get(clip.source_asset_id)
            if not isinstance(asset, OriginalRenderAsset):
                raise CreatorRenderContractError(
                    "This edit has an unverified picture source in the confirmed order."
                )
            if not actual or actual[-1] != asset.media_id:
                actual.append(asset.media_id)
        if not contract.order_ids or tuple(actual) != contract.order_ids:
            raise CreatorRenderContractError("This edit couldn't keep the confirmed clip order.")
    receipts.append({"duration_s": recipe.duration, "verified": True})
    return receipts
