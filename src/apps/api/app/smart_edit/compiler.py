"""Compile a validated Smart event plan into Nova renderer lanes."""

from __future__ import annotations

import hashlib
import re
import unicodedata
from dataclasses import dataclass
from typing import Any

from app.agents._schemas.sound_effect import SoundEffectPlacement
from app.agents._schemas.text_element import TextElement
from app.smart_edit.presets import TextStylePolicy, load_preset
from app.smart_edit.schemas import (
    SMART_EDIT_SCHEMA_VERSION_V2,
    AudioTreatmentLane,
    BoundaryEffectLane,
    CameraLane,
    CaptionEmphasisLane,
    SfxLane,
    SmartEditPlanDocument,
    TextLane,
    VisualLane,
)

COMPILER_VERSION = "nova-smart-lanes-2026-07-18.2"
COMPILER_VERSION_V2 = "nova-smart-lanes-2026-07-20.1"
_STYLE_BY_TOKEN = {
    "hook_lime": "hook",
    "context_lime": "context",
    "list_keyword": "list_item",
    "example_soft": "example",
    "payoff_lime": "payoff",
    "cta_lime": "cta",
}
_STYLE_PRIORITY = {
    "hook": 1,
    "example": 2,
    "context": 3,
    "payoff": 4,
    "cta": 5,
    "list_item": 6,
}
_KEYWORD_STOP = {
    "bir",
    "iki",
    "uc",
    "dort",
    "bes",
    "ilk",
    "olarak",
    "birinci",
    "birincisi",
    "ikinci",
    "ikincisi",
    "ucuncu",
    "ucuncusu",
    "dorduncu",
    "dorduncusu",
    "ve",
    "de",
    "da",
    "bu",
    "su",
    # English list markers + function words (plan 012 P1-3): keeps the section
    # heading from picking a marker ("number") instead of the topic/name. This
    # set is compiler-local (planner._KEYWORD_STOP is a separate constant), so
    # asset matching stays byte-identical.
    "number",
    "no",
    "one",
    "two",
    "three",
    "four",
    "five",
    "six",
    "seven",
    "eight",
    "nine",
    "ten",
    "first",
    "second",
    "third",
    "fourth",
    "fifth",
    "next",
    "last",
    "the",
    "a",
    "an",
    "and",
    "of",
    "to",
    "is",
    "are",
    "my",
    "this",
    "that",
}
_ROLE_OFFSETS_MS = {
    "chapter_number_pop": -50,
    "keyword_typewriter_tick": 250,
    "transition_whip": 200,
    "visual_enter_soft": 0,
    "visual_enter_accent": 0,
    "badge_enter": 0,
    "cta_click": 0,
}


@dataclass(frozen=True, slots=True)
class CompiledSmartEdit:
    caption_cues: list[dict[str, Any]]
    text_elements: list[dict[str, Any]]
    media_overlays: list[dict[str, Any]]
    sfx_intents: list[dict[str, Any]]
    boundary_effects: list[dict[str, Any]]
    camera_intents: list[dict[str, Any]]
    audio_treatment_intents: list[dict[str, Any]]
    compiled_patch: dict[str, Any]
    validation_receipt: dict[str, Any]


def _fold(value: str) -> str:
    value = value.casefold().translate(str.maketrans("çğıöşü", "cgiosu"))
    value = "".join(
        ch for ch in unicodedata.normalize("NFKD", value) if not unicodedata.combining(ch)
    )
    return " ".join(re.findall(r"[a-z0-9]+", value))


def _stable_id(*parts: object, length: int = 32) -> str:
    material = "|".join(str(part) for part in parts)
    return hashlib.sha256(material.encode("utf-8")).hexdigest()[:length]


def _word_maps(
    document: SmartEditPlanDocument,
    cues: list[dict[str, Any]],
) -> tuple[dict[str, int], dict[str, str], dict[str, dict[str, Any]]]:
    cue_index_by_word: dict[str, int] = {}
    display_by_word: dict[str, str] = {}
    raw_by_word: dict[str, dict[str, Any]] = {}
    for index, baseline in enumerate(document.baseline_captions):
        if index >= len(cues):
            break
        raw_words = cues[index].get("words")
        for word_index, word_id in enumerate(baseline.word_ids):
            cue_index_by_word[word_id] = index
            if isinstance(raw_words, list) and word_index < len(raw_words):
                raw = raw_words[word_index]
                if isinstance(raw, dict):
                    display_by_word[word_id] = str(raw.get("text") or "")
                    raw_by_word[word_id] = dict(raw)
                    continue
            tokens = str(baseline.display_text).split()
            display_by_word[word_id] = tokens[word_index] if word_index < len(tokens) else ""
    return cue_index_by_word, display_by_word, raw_by_word


def _text_element(
    *,
    element_id: str,
    text: str,
    start_s: float,
    end_s: float,
    style: TextStylePolicy,
    event_id: str,
    role: str,
    z: int,
) -> TextElement:
    return TextElement(
        id=element_id,
        text=text,
        start_s=start_s,
        end_s=max(start_s + 0.2, end_s),
        role="generative_sequence",
        position="custom",
        x_frac=style.x_frac,
        y_frac=style.y_frac,
        font_family=style.font_family,
        size_px=style.size_px,
        color=style.color,
        highlight_color=style.highlight_color,
        stroke_width=style.stroke_width,
        shadow_enabled=True,
        alignment=style.alignment,
        effect=style.effect,
        reveal_s=(min(end_s, start_s + 0.68) if style.effect == "typewriter" else None),
        max_width_frac=style.max_width_frac,
        line_spacing=1.02,
        z=z,
        source_params={
            "source": "smart_captions",
            "event_id": event_id,
            "role": role,
        },
    )


def _section_keyword(word_ids: list[str], display_by_word: dict[str, str]) -> str:
    for word_id in word_ids:
        value = display_by_word.get(word_id, "").strip(".,!?;:()[]{}\"'“”")
        if value and _fold(value) not in _KEYWORD_STOP and not value.isdigit():
            return value
    return ""


def _typewriter_schedule(
    text: str,
    *,
    start_s: float,
    reveal_end_s: float,
    min_spacing_ms: int,
    max_ticks: int = 12,
) -> list[float]:
    glyph_count = sum(not char.isspace() for char in text)
    if glyph_count <= 0:
        return []
    duration = max(0.0, reveal_end_s - start_s)
    if duration <= 0:
        return [round(start_s, 3)]
    desired = min(max_ticks, max(2, (glyph_count + 1) // 2))
    min_spacing_s = min_spacing_ms / 1000
    capacity = max(1, int(duration / max(min_spacing_s, 0.001)) + 1)
    count = min(desired, capacity)
    if count <= 1:
        return [round(start_s, 3)]
    return [round(start_s + duration * index / (count - 1), 3) for index in range(count)]


def _suppress_claimed_words(
    cues: list[dict[str, Any]],
    document: SmartEditPlanDocument,
    claimed: set[str],
) -> list[dict[str, Any]]:
    if not claimed:
        return cues
    result: list[dict[str, Any]] = []
    for index, baseline in enumerate(document.baseline_captions):
        if index >= len(cues):
            break
        cue = dict(cues[index])
        raw_words = cue.get("words")
        if not isinstance(raw_words, list) or len(raw_words) != len(baseline.word_ids):
            if set(baseline.word_ids) <= claimed:
                continue
            result.append(cue)
            continue
        kept = [
            dict(raw)
            for word_id, raw in zip(baseline.word_ids, raw_words)
            if word_id not in claimed and isinstance(raw, dict)
        ]
        if not kept:
            continue
        cue["words"] = kept
        cue["text"] = " ".join(str(word.get("text") or "").strip() for word in kept).strip()
        cue["start_s"] = float(kept[0].get("start_s", cue.get("start_s", 0.0)))
        cue["end_s"] = float(kept[-1].get("end_s", cue.get("end_s", cue["start_s"])))
        if cue["text"]:
            result.append(cue)
    # Defensive passthrough for any extra cue not represented by the document.
    result.extend(dict(cue) for cue in cues[len(document.baseline_captions) :])
    return result


def compile_smart_plan(
    document: SmartEditPlanDocument,
    cues: list[dict[str, Any]],
    *,
    assets_by_id: dict[str, dict[str, Any]] | None = None,
) -> CompiledSmartEdit:
    """Resolve Smart tokens to caption, text, visual, boundary, and SFX lanes."""

    preset = load_preset(document.preset_id, document.preset_version)
    is_v2 = document.schema_version == SMART_EDIT_SCHEMA_VERSION_V2
    compiled_cues = [dict(cue) for cue in cues]
    cue_index_by_word, display_by_word, _ = _word_maps(document, compiled_cues)
    assets_by_id = assets_by_id or {}
    text_elements: list[dict[str, Any]] = []
    media_overlays: list[dict[str, Any]] = []
    sfx_intents: list[dict[str, Any]] = []
    boundary_effects: list[dict[str, Any]] = []
    camera_intents: list[dict[str, Any]] = []
    audio_treatment_intents: list[dict[str, Any]] = []
    claimed_word_ids: set[str] = set()
    omissions: list[dict[str, str]] = []
    event_receipts: list[dict[str, Any]] = []
    reveal_schedules: dict[str, list[float]] = {}

    for event in document.events:
        if not event.enabled:
            continue
        cue_index = cue_index_by_word.get(event.anchor.word_id)
        if cue_index is None or cue_index >= len(compiled_cues):
            omissions.append({"event_id": event.event_id, "reason": "anchor_not_in_captions"})
            continue
        cue = compiled_cues[cue_index]
        before = (
            len(text_elements),
            len(media_overlays),
            len(sfx_intents),
            len(boundary_effects),
        )
        camera_before = len(camera_intents)
        audio_before = len(audio_treatment_intents)
        styled_before = bool(cue.get("smart_style"))
        for lane in event.lanes:
            if isinstance(lane, CaptionEmphasisLane):
                style = _STYLE_BY_TOKEN.get(lane.token)
                target_indexes = (
                    {
                        cue_index_by_word[word_id]
                        for word_id in lane.baseline_caption_word_ids
                        if word_id in cue_index_by_word
                    }
                    if is_v2
                    else {cue_index}
                )
                for target_index in target_indexes:
                    target_cue = compiled_cues[target_index]
                    if style and _STYLE_PRIORITY[style] >= _STYLE_PRIORITY.get(
                        str(target_cue.get("smart_style") or ""), 0
                    ):
                        target_cue["smart_style"] = style
            elif isinstance(lane, TextLane):
                start_s = event.active_start_ms / 1000
                from app.config import settings as _settings  # noqa: PLC0415

                if (
                    lane.token == "section_heading"
                    and lane.sequence_number
                    and _settings.smart_caption_section_heading_enabled
                ):
                    number_style = preset.text_styles["section_number"]
                    keyword_style = preset.text_styles["section_keyword"]
                    number_end = start_s + number_style.duration_s
                    text_elements.append(
                        _text_element(
                            element_id=f"smart-{event.event_id}-number",
                            text=str(lane.sequence_number),
                            start_s=start_s,
                            end_s=number_end,
                            style=number_style,
                            event_id=event.event_id,
                            role=event.role,
                            z=80,
                        ).model_dump(exclude_none=True)
                    )
                    keyword = _section_keyword(lane.transcript_word_ids, display_by_word)
                    if keyword:
                        keyword_start = start_s + 0.30
                        keyword_render_style = (
                            keyword_style.model_copy(update={"effect": "typewriter"})
                            if is_v2
                            else keyword_style
                        )
                        keyword_element = _text_element(
                            element_id=f"smart-{event.event_id}-keyword",
                            text=keyword,
                            start_s=keyword_start,
                            end_s=keyword_start + keyword_render_style.duration_s,
                            style=keyword_render_style,
                            event_id=event.event_id,
                            role=event.role,
                            z=81,
                        ).model_dump(exclude_none=True)
                        text_elements.append(keyword_element)
                        if is_v2:
                            schedule = _typewriter_schedule(
                                keyword,
                                start_s=keyword_start,
                                reveal_end_s=float(
                                    keyword_element.get("reveal_s")
                                    or keyword_start + keyword_render_style.duration_s
                                ),
                                min_spacing_ms=preset.sfx_roles[
                                    "keyword_typewriter_tick"
                                ].min_spacing_ms,
                            )
                            reveal_schedules[event.event_id] = schedule
                            keyword_element["reveal_schedule_s"] = schedule
                            keyword_element["source_params"]["reveal_schedule_s"] = schedule
                elif lane.token == "context_title":
                    title = " ".join(
                        display_by_word.get(word_id, "") for word_id in lane.transcript_word_ids
                    ).strip()
                    if title:
                        style = preset.text_styles["context_title"]
                        render_style = (
                            style.model_copy(update={"effect": "typewriter"}) if is_v2 else style
                        )
                        title_element = _text_element(
                            element_id=f"smart-{event.event_id}-title",
                            text=title,
                            start_s=start_s,
                            end_s=min(
                                event.active_end_ms / 1000,
                                start_s + render_style.duration_s,
                            ),
                            style=render_style,
                            event_id=event.event_id,
                            role=event.role,
                            z=72,
                        ).model_dump(exclude_none=True)
                        text_elements.append(title_element)
                        if is_v2:
                            schedule = _typewriter_schedule(
                                title,
                                start_s=start_s,
                                reveal_end_s=float(
                                    title_element.get("reveal_s")
                                    or start_s + render_style.duration_s
                                ),
                                min_spacing_ms=preset.sfx_roles[
                                    "keyword_typewriter_tick"
                                ].min_spacing_ms,
                            )
                            reveal_schedules[event.event_id] = schedule
                            title_element["reveal_schedule_s"] = schedule
                            title_element["source_params"]["reveal_schedule_s"] = schedule
                if lane.caption_visibility == "suppress_claimed_span":
                    claimed_word_ids.update(lane.claimed_word_ids)
            elif isinstance(lane, VisualLane):
                asset = assets_by_id.get(lane.asset_id)
                zone = preset.visual_zones.get(lane.zone)
                if asset is None or zone is None:
                    omissions.append(
                        {
                            "event_id": event.event_id,
                            "reason": "visual_asset_or_zone_missing",
                        }
                    )
                    continue
                gcs_path = str(asset.get("gcs_path") or asset.get("src_gcs_path") or "")
                if not gcs_path.startswith("users/"):
                    omissions.append(
                        {"event_id": event.event_id, "reason": "visual_asset_path_rejected"}
                    )
                    continue
                media_overlays.append(
                    {
                        "id": _stable_id("smart-visual", event.event_id, lane.asset_id),
                        "kind": "video" if asset.get("kind") == "video" else "image",
                        "src_gcs_path": gcs_path,
                        "preview_gcs_path": asset.get("preview_gcs_path"),
                        "display_mode": zone.display_mode,
                        "position": "custom",
                        "x_frac": zone.x_frac,
                        "y_frac": zone.y_frac,
                        "scale": zone.scale,
                        "start_s": event.active_start_ms / 1000,
                        "end_s": event.active_end_ms / 1000,
                        "clip_duration_s": asset.get("duration_s"),
                        "z": zone.z + lane.group_order,
                        "source": "smart_captions",
                        "event_id": event.event_id,
                        "composition_group_id": lane.composition_group_id,
                        "entrance_token": lane.entrance_token,
                    }
                )
            elif isinstance(lane, SfxLane):
                for role in lane.role_tokens:
                    if role not in preset.sfx_roles:
                        omissions.append({"event_id": event.event_id, "reason": "sfx_role_unknown"})
                        continue
                    if is_v2 and role == "keyword_typewriter_tick":
                        for reveal_index, at_s in enumerate(
                            reveal_schedules.get(event.event_id, [])
                        ):
                            sfx_intents.append(
                                {
                                    "event_id": event.event_id,
                                    "role": role,
                                    "at_s": at_s,
                                    "reveal_index": reveal_index,
                                }
                            )
                        continue
                    at_ms = event.active_start_ms + lane.offset_ms + _ROLE_OFFSETS_MS.get(role, 0)
                    sfx_intents.append(
                        {
                            "event_id": event.event_id,
                            "role": role,
                            "at_s": max(0.0, at_ms / 1000),
                        }
                    )
            elif isinstance(lane, BoundaryEffectLane):
                policy = preset.boundary_effects.get(lane.effect_token)
                if policy is None:
                    omissions.append(
                        {"event_id": event.event_id, "reason": "boundary_token_unknown"}
                    )
                    continue
                boundary_effects.append(
                    {
                        "event_id": event.event_id,
                        "effect": policy.effect,
                        "at_s": event.active_start_ms / 1000,
                        "duration_s": policy.duration_ms / 1000,
                        "blur_sigma": policy.blur_sigma,
                        "intensity": policy.intensity,
                    }
                )
            elif isinstance(lane, CameraLane):
                camera_intents.append(
                    {
                        "event_id": event.event_id,
                        "role": event.role,
                        "token": lane.token,
                        "intensity_token": lane.intensity_token,
                        "at_s": event.active_start_ms / 1000,
                        "start_s": event.active_start_ms / 1000,
                        "end_s": min(
                            event.active_end_ms / 1000,
                            event.active_start_ms / 1000 + 0.8,
                        ),
                    }
                )
            elif isinstance(lane, AudioTreatmentLane):
                policy = preset.audio_treatment
                if policy is None:
                    omissions.append(
                        {"event_id": event.event_id, "reason": "audio_treatment_policy_missing"}
                    )
                    continue
                audio_treatment_intents.append(
                    {
                        "event_id": event.event_id,
                        "token": lane.token,
                        "selection_token": lane.selection_token,
                        "gain_token": lane.gain_token,
                        "music_match_min_score": policy.music_match_min_score,
                        "bed_gain_db": policy.bed_gain_db,
                        "speech_duck_db": policy.speech_duck_db,
                        "final_lufs": policy.final_lufs,
                    }
                )
        event_receipt = {
            "event_id": event.event_id,
            "role": event.role,
            "caption_style_applied": (not styled_before and bool(cue.get("smart_style"))),
            "text_elements": len(text_elements) - before[0],
            "visuals": len(media_overlays) - before[1],
            "sfx_intents": len(sfx_intents) - before[2],
            "boundary_effects": len(boundary_effects) - before[3],
        }
        if is_v2:
            event_receipt["camera_intents"] = len(camera_intents) - camera_before
            event_receipt["audio_treatment_intents"] = len(audio_treatment_intents) - audio_before
        event_receipts.append(event_receipt)

    hook_caption_suppressed = False
    hook_caption_suppression_eligible = False
    if is_v2:
        hook_scene = preset.scene_layouts.get("hook_accumulation")
        resolved_hook_visuals = {
            str(overlay["id"])
            for overlay in media_overlays
            if overlay.get("composition_group_id") == "hook_accumulation"
        }
        if (
            hook_scene
            and hook_scene.caption_visibility == "suppress_if_resolved"
            and len(resolved_hook_visuals) >= preset.density.hook_caption_suppress_min_visuals
        ):
            # Asset resolution here is planning-time evidence only. Downloads,
            # normalization, collision arbitration, and FFmpeg can still fail
            # later. Suppressing speech here could therefore ship a hook with
            # neither captions nor visuals. Keep the transcript visible until
            # both lanes share a transactional compositor that can suppress
            # from an applied-media manifest.
            hook_caption_suppression_eligible = True

    compiled_cues = _suppress_claimed_words(compiled_cues, document, claimed_word_ids)
    if is_v2:
        from app.pipeline.captions import prepare_smart_caption_cues  # noqa: PLC0415

        compiled_cues = prepare_smart_caption_cues(
            compiled_cues,
            preset.caption.model_dump(mode="json"),
        )
    text_elements.sort(key=lambda item: (float(item["start_s"]), str(item["id"])))
    media_overlays.sort(key=lambda item: (float(item["start_s"]), int(item["z"])))
    sfx_intents.sort(key=lambda item: (float(item["at_s"]), str(item["role"])))
    boundary_effects.sort(key=lambda item: (float(item["at_s"]), str(item["event_id"])))
    camera_intents.sort(key=lambda item: (float(item["at_s"]), str(item["event_id"])))
    patch = {
        "caption_cues": compiled_cues,
        "caption_policy": preset.caption.model_dump(mode="json"),
        "text_elements": text_elements,
        "media_overlays": media_overlays,
        "sfx_intents": sfx_intents,
        "boundary_effects": boundary_effects,
        "compiler_version": COMPILER_VERSION_V2 if is_v2 else COMPILER_VERSION,
        "omissions": omissions,
    }
    if is_v2:
        patch["camera_intents"] = camera_intents
        patch["audio_treatment_intents"] = audio_treatment_intents
        patch["hook_caption_suppressed"] = hook_caption_suppressed
        patch["hook_caption_suppression_eligible"] = hook_caption_suppression_eligible
        patch["hook_caption_suppression_status"] = (
            "deferred_until_transactional_compositor"
            if hook_caption_suppression_eligible
            else "not_eligible"
        )
        patch["reveal_schedules"] = reveal_schedules
    validation_receipt = {
        "valid": True,
        "caption_count": len(compiled_cues),
        "styled_caption_count": sum(bool(cue.get("smart_style")) for cue in compiled_cues),
        "text_element_count": len(text_elements),
        "media_overlay_count": len(media_overlays),
        "sfx_intent_count": len(sfx_intents),
        "boundary_effect_count": len(boundary_effects),
        "event_receipts": event_receipts,
        "omissions": omissions,
    }
    if is_v2:
        validation_receipt.update(
            {
                "camera_intent_count": len(camera_intents),
                "audio_treatment_intent_count": len(audio_treatment_intents),
                "hook_caption_suppressed": hook_caption_suppressed,
                "hook_caption_suppression_eligible": hook_caption_suppression_eligible,
            }
        )
    return CompiledSmartEdit(
        caption_cues=compiled_cues,
        text_elements=text_elements,
        media_overlays=media_overlays,
        sfx_intents=sfx_intents,
        boundary_effects=boundary_effects,
        camera_intents=camera_intents,
        audio_treatment_intents=audio_treatment_intents,
        compiled_patch=patch,
        validation_receipt=validation_receipt,
    )


def _clean_sfx_rows(glossary: list[dict[str, Any]]) -> list[dict[str, Any]]:
    rejected_name_tokens = {
        "female",
        "male",
        "girl",
        "boy",
        "woman",
        "man voice",
        "kadın",
        "kadin",
        "erkek",
        "konuşma",
        "konusma",
        "voice",
        "vocal",
        "speech",
        "narration",
        "says",
    }
    result: list[dict[str, Any]] = []
    for effect in glossary:
        name = str(effect.get("name") or "").casefold()
        if any(token in name for token in rejected_name_tokens):
            continue
        if effect.get("contains_voice") is True:
            continue
        result.append(effect)
    return result


def _pick_sfx(
    role: str,
    policy: Any,
    glossary: list[dict[str, Any]],
) -> dict[str, Any] | None:
    by_id = {str(effect.get("id")): effect for effect in glossary}
    for asset_id in policy.asset_ids:
        effect = by_id.get(asset_id)
        if effect and effect.get("audio_gcs_path"):
            return effect
    for effect in glossary:
        tags = {str(value) for value in (effect.get("role_tags") or [])}
        vocal_probability = effect.get("vocal_probability")
        if vocal_probability is not None:
            try:
                if float(vocal_probability) > policy.max_vocal_probability:
                    continue
            except (TypeError, ValueError):
                continue
        audit = effect.get("manual_audit_status")
        if audit not in (None, "approved"):
            continue
        if tags.intersection(policy.role_tags) and effect.get("audio_gcs_path"):
            return effect
    # Compatibility bridge for existing curated rows. It is role-specific and
    # still subject to voice-name rejection; list order is never a fallback.
    for token in policy.name_fallback_tokens:
        matches = [
            effect
            for effect in glossary
            if token.casefold() in str(effect.get("name") or "").casefold()
            and effect.get("audio_gcs_path")
        ]
        if matches:
            return sorted(matches, key=lambda effect: str(effect.get("id")))[0]
    return None


def resolve_sfx_placements(
    intents: list[dict[str, Any]],
    glossary: list[dict[str, Any]],
    *,
    preset_id: str = "cigdem",
    preset_version: str = "v1",
) -> list[dict[str, Any]]:
    """Resolve role intents to audited library assets with role-specific spacing."""

    preset = load_preset(preset_id, preset_version)
    glossary = _clean_sfx_rows(glossary)
    placements: list[dict[str, Any]] = []
    last_by_role: dict[str, float] = {}
    for intent in sorted(intents, key=lambda item: float(item.get("at_s", 0.0))):
        role = str(intent.get("role") or "")
        policy = preset.sfx_roles.get(role)
        if policy is None:
            continue
        at_s = max(0.0, float(intent.get("at_s", 0.0)))
        if (at_s - last_by_role.get(role, -999.0)) * 1000 < policy.min_spacing_ms:
            continue
        effect = _pick_sfx(role, policy, glossary)
        if effect is None:
            continue
        effect_id = str(effect.get("id"))
        placement = SoundEffectPlacement(
            id=_stable_id(intent.get("event_id"), role, effect_id, f"{at_s:.3f}"),
            at_s=at_s,
            gain=policy.gain,
            sound_effect_id=effect_id,
            src_gcs_path=str(effect.get("audio_gcs_path")),
            duration_s=effect.get("duration_s"),
            label=str(effect.get("name") or role)[:40],
        )
        payload = placement.model_dump(exclude_none=True)
        payload["smart_role"] = role
        payload["smart_event_id"] = str(intent.get("event_id") or "")
        placements.append(payload)
        last_by_role[role] = at_s
        if len(placements) >= 48:
            break
    return placements
