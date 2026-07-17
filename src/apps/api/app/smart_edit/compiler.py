"""Compile a renderer-independent Smart plan into Nova's proven edit lanes."""

from __future__ import annotations

import re
import uuid
from dataclasses import dataclass
from typing import Any

from app.agents._schemas.sound_effect import SoundEffectPlacement
from app.agents._schemas.text_element import TextElement
from app.smart_edit.schemas import (
    BoundaryEffectLane,
    CaptionEmphasisLane,
    SfxLane,
    SmartEditPlanDocument,
    TextLane,
)

COMPILER_VERSION = "nova-lanes-2026-07-17.1"
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


@dataclass(frozen=True, slots=True)
class CompiledSmartEdit:
    caption_cues: list[dict[str, Any]]
    text_elements: list[dict[str, Any]]
    sfx_intents: list[dict[str, Any]]
    boundary_effects: list[dict[str, Any]]
    compiled_patch: dict[str, Any]
    validation_receipt: dict[str, Any]


def _title_text(text: str, *, max_words: int = 7, max_chars: int = 54) -> str:
    words = text.split()[:max_words]
    value = " ".join(words).strip()
    if len(value) <= max_chars:
        return value
    return value[: max_chars - 1].rstrip() + "…"


def _number_element(event, number: int) -> TextElement:  # noqa: ANN001
    start_s = event.active_start_ms / 1000
    end_s = min(event.active_end_ms / 1000 + 0.35, start_s + 1.8)
    return TextElement(
        id=f"smart-{event.event_id}-number",
        text=str(number),
        start_s=start_s,
        end_s=max(start_s + 0.5, end_s),
        role="generative_sequence",
        position="custom",
        x_frac=0.5,
        y_frac=0.075,
        font_family="Inter-Bold",
        size_px=126,
        color="#FFFFFF",
        highlight_color="#84CC16",
        stroke_width=8,
        shadow_enabled=True,
        alignment="center",
        effect="pop-in",
        reveal_s=min(start_s + 0.22, max(start_s + 0.21, end_s)),
        z=24,
        source_params={
            "source": "smart_captions",
            "event_id": event.event_id,
            "role": event.role,
        },
    )


def _context_element(event, text: str) -> TextElement:  # noqa: ANN001
    start_s = event.active_start_ms / 1000
    end_s = min(max(event.active_end_ms / 1000, start_s + 1.2), start_s + 2.6)
    return TextElement(
        id=f"smart-{event.event_id}-title",
        text=_title_text(text),
        start_s=start_s,
        end_s=end_s,
        role="generative_sequence",
        position="custom",
        x_frac=0.5,
        y_frac=0.145,
        font_family="Inter-Bold",
        size_px=62,
        color="#84CC16",
        highlight_color="#FFFFFF",
        stroke_width=7,
        shadow_enabled=True,
        alignment="center",
        effect="typewriter",
        reveal_s=min(start_s + 0.65, end_s),
        max_width_frac=0.84,
        line_spacing=1.04,
        z=22,
        source_params={
            "source": "smart_captions",
            "event_id": event.event_id,
            "role": event.role,
        },
    )


def compile_smart_plan(
    document: SmartEditPlanDocument,
    cues: list[dict[str, Any]],
) -> CompiledSmartEdit:
    """Resolve closed Smart tokens to caption, text, transition and SFX lanes."""

    compiled_cues = [dict(cue) for cue in cues]
    cue_index_by_word: dict[str, int] = {}
    cue_text_by_word: dict[str, str] = {}
    for index, baseline in enumerate(document.baseline_captions):
        if index >= len(compiled_cues):
            break
        for word_id in baseline.word_ids:
            cue_index_by_word[word_id] = index
            cue_text_by_word[word_id] = baseline.display_text

    text_elements: list[dict[str, Any]] = []
    sfx_intents: list[dict[str, Any]] = []
    boundary_effects: list[dict[str, Any]] = []
    for event in document.events:
        if not event.enabled:
            continue
        cue_index = cue_index_by_word.get(event.anchor.word_id)
        if cue_index is None or cue_index >= len(compiled_cues):
            continue
        cue = compiled_cues[cue_index]
        for lane in event.lanes:
            if isinstance(lane, CaptionEmphasisLane):
                style = _STYLE_BY_TOKEN.get(lane.token)
                if style and _STYLE_PRIORITY[style] >= _STYLE_PRIORITY.get(
                    str(cue.get("smart_style") or ""), 0
                ):
                    cue["smart_style"] = style
            elif isinstance(lane, TextLane):
                match = re.fullmatch(r"list_number_(\d+)", lane.token)
                if match:
                    text_elements.append(
                        _number_element(event, int(match.group(1))).model_dump(exclude_none=True)
                    )
                elif lane.token == "context_title":
                    title = cue_text_by_word.get(event.anchor.word_id, str(cue.get("text") or ""))
                    if title.strip():
                        text_elements.append(
                            _context_element(event, title).model_dump(exclude_none=True)
                        )
            elif isinstance(lane, SfxLane):
                sfx_intents.append(
                    {
                        "event_id": event.event_id,
                        "asset_id": lane.asset_id,
                        "intent": "click" if event.role == "cta" else "pop_in",
                        "at_s": max(0.0, event.active_start_ms / 1000 + lane.offset_ms / 1000),
                        "gain": 0.68 if lane.gain_token == "foreground_soft" else 1.0,
                    }
                )
            elif isinstance(lane, BoundaryEffectLane):
                boundary_effects.append(
                    {
                        "event_id": event.event_id,
                        "effect": lane.effect_token,
                        "at_s": event.active_start_ms / 1000,
                    }
                )

    # Stable time order keeps JSON diffs and renderer behaviour deterministic.
    text_elements.sort(key=lambda item: (float(item["start_s"]), str(item["id"])))
    sfx_intents.sort(key=lambda item: (float(item["at_s"]), str(item["event_id"])))
    boundary_effects.sort(key=lambda item: (float(item["at_s"]), str(item["event_id"])))

    patch = {
        "caption_cues": compiled_cues,
        "text_elements": text_elements,
        "sfx_intents": sfx_intents,
        "boundary_effects": boundary_effects,
        "compiler_version": COMPILER_VERSION,
    }
    return CompiledSmartEdit(
        caption_cues=compiled_cues,
        text_elements=text_elements,
        sfx_intents=sfx_intents,
        boundary_effects=boundary_effects,
        compiled_patch=patch,
        validation_receipt={
            "valid": True,
            "caption_count": len(compiled_cues),
            "styled_caption_count": sum(bool(cue.get("smart_style")) for cue in compiled_cues),
            "text_element_count": len(text_elements),
            "sfx_intent_count": len(sfx_intents),
            "boundary_effect_count": len(boundary_effects),
        },
    )


def resolve_sfx_placements(
    intents: list[dict[str, Any]], glossary: list[dict[str, Any]]
) -> list[dict[str, Any]]:
    """Resolve closed SFX intents to published, clean glossary assets.

    This intentionally disables the legacy "first usable effect" fallback: a
    missing clean pop/click yields silence, never an unrelated voice or meme.
    """

    from app.services.overlay_autoplace import map_sfx_intent  # noqa: PLC0415

    # Names are the only trust signal available in the current glossary. Keep
    # speech/vocal assets out even when their label also contains a preferred
    # token (for example "female pop voice"). Smart Captions should choose
    # silence over reintroducing a voice clip behind the creator.
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
    clean_glossary = [
        effect
        for effect in glossary
        if not any(
            token in str(effect.get("name") or "").casefold()
            for token in rejected_name_tokens
        )
    ]

    placements: list[dict[str, Any]] = []
    last_at_s = -999.0
    for intent in sorted(intents, key=lambda item: float(item.get("at_s", 0.0))):
        at_s = max(0.0, float(intent.get("at_s", 0.0)))
        if at_s - last_at_s < 0.7:
            continue
        resolved = map_sfx_intent(
            str(intent.get("intent") or "pop_in"),
            clean_glossary,
            allow_fallback=False,
        )
        if resolved is None:
            continue
        placement = SoundEffectPlacement(
            id=uuid.uuid4().hex,
            at_s=at_s,
            gain=max(0.0, min(1.0, float(intent.get("gain", 0.68)))),
            **resolved,
        )
        placements.append(placement.model_dump(exclude_none=True))
        last_at_s = at_s
        if len(placements) >= 8:
            break
    return placements
