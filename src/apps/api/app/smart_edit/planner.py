"""Deterministic semantic planner for the first Smart Captions render.

The planner deliberately reasons in transcript word ids and closed editorial
tokens.  It never emits pixels, font names, colours, storage paths, or arbitrary
FFmpeg expressions; those belong to :mod:`app.smart_edit.compiler`.

This first production planner is language-aware and fail-closed.  It recognizes
the editorial structure that matters most in talking-to-camera videos (hook,
context shifts, numbered list items, examples, payoffs, and CTAs) without making
the whole render depend on a second network model call.  Visual matching remains
the job of the existing Gemini overlay-placement agent, which sees the same
word-timed transcript after the base render.
"""

from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass
from typing import Any

from app.smart_edit.schemas import (
    MAX_SMART_WORDS,
    BaselineCaptionCue,
    BoundaryEffectLane,
    CaptionEmphasisLane,
    EventAnchor,
    SemanticRole,
    SfxLane,
    SmartEditEvent,
    SmartEditPlanDocument,
    SmartWord,
    TextLane,
    build_event_id,
)

PLANNER_VERSION = "heuristic-2026-07-17.1"

_TOKEN_RE = re.compile(r"\S+")
_ORDINAL_PREFIXES: tuple[tuple[int, tuple[str, ...]], ...] = (
    (1, ("birinci", "birincisi", "ilk olarak", "ilk basta", "1.", "1)")),
    (2, ("ikinci", "ikincisi", "2.", "2)")),
    (3, ("ucuncu", "ucuncusu", "3.", "3)")),
    (4, ("dorduncu", "dorduncusu", "4.", "4)")),
    (5, ("besinci", "besincisi", "5.", "5)")),
    (6, ("altinci", "altincisi", "6.", "6)")),
)
_CONTEXT_PREFIXES = (
    "peki",
    "simdi",
    "gelelim",
    "diger taraftan",
    "bir diger",
    "ama asil",
    "bunun yaninda",
    "sonra",
    "siradaki",
    "neden",
    "nasil",
)
_EXAMPLE_PREFIXES = ("ornegin", "mesela", "buna ornek", "ornek olarak")
_PAYOFF_PREFIXES = (
    "sonuc olarak",
    "kisacasi",
    "ozetle",
    "en onemlisi",
    "iste bu yuzden",
    "dolayisiyla",
)
_CTA_TERMS = (
    "takip et",
    "yorumlara",
    "yorum yaz",
    "kaydet",
    "paylas",
    "abone ol",
    "sen de",
)


@dataclass(frozen=True, slots=True)
class SmartPlanBuild:
    normalized_words: list[SmartWord]
    document: SmartEditPlanDocument
    planner_versions: dict[str, str]
    validation_receipt: dict[str, Any]


def _fold(value: str) -> str:
    value = value.casefold().translate(str.maketrans("çğıöşü", "cgiosu"))
    value = "".join(
        ch for ch in unicodedata.normalize("NFKD", value) if not unicodedata.combining(ch)
    )
    return " ".join(re.findall(r"[a-z0-9.)]+", value))


def _cue_tokens(cue: dict[str, Any]) -> list[str]:
    return [token for token in _TOKEN_RE.findall(str(cue.get("text") or "").strip()) if token]


def _word_windows(cue: dict[str, Any], tokens: list[str]) -> list[tuple[float, float]]:
    raw_words = cue.get("words")
    if isinstance(raw_words, list) and len(raw_words) == len(tokens):
        windows: list[tuple[float, float]] = []
        try:
            for raw in raw_words:
                if not isinstance(raw, dict):
                    raise ValueError
                start = max(0.0, float(raw.get("start_s", 0.0)))
                end = max(start + 0.01, float(raw.get("end_s", start + 0.01)))
                windows.append((start, end))
            return windows
        except (TypeError, ValueError):
            pass

    start = max(0.0, float(cue.get("start_s", 0.0) or 0.0))
    end = max(start + 0.01, float(cue.get("end_s", start + 0.01) or start + 0.01))
    step = (end - start) / max(1, len(tokens))
    return [(start + index * step, start + (index + 1) * step) for index in range(len(tokens))]


def _normalize_captions(
    cues: list[dict[str, Any]], *, language: str
) -> tuple[list[SmartWord], list[BaselineCaptionCue], list[tuple[int, list[str]]]]:
    words: list[SmartWord] = []
    baseline: list[BaselineCaptionCue] = []
    cue_word_ids: list[tuple[int, list[str]]] = []
    next_word = 1

    for cue_index, cue in enumerate(cues):
        tokens = _cue_tokens(cue)
        if not tokens:
            continue
        if next_word - 1 + len(tokens) > MAX_SMART_WORDS:
            break
        windows = _word_windows(cue, tokens)
        ids: list[str] = []
        timing_quality = (
            "aligned"
            if isinstance(cue.get("words"), list) and len(cue["words"]) == len(tokens)
            else "segment_estimate"
        )
        for token, (start_s, end_s) in zip(tokens, windows):
            word_id = f"w{next_word:06d}"
            next_word += 1
            normalized = _fold(token) or token.casefold()
            words.append(
                SmartWord(
                    word_id=word_id,
                    spoken_text=token,
                    display_text=token,
                    normalized_text=normalized,
                    start_ms=round(start_s * 1000),
                    end_ms=max(round(end_s * 1000), round(start_s * 1000) + 1),
                    timing_quality=timing_quality,
                    display_alignment=[word_id],
                    language=language[:16] or None,
                )
            )
            ids.append(word_id)
        baseline.append(
            BaselineCaptionCue(
                cue_id=f"smart-cue-{cue_index + 1:03d}",
                word_ids=ids,
                display_text=str(cue.get("text") or "").strip(),
            )
        )
        cue_word_ids.append((cue_index, ids))
    return words, baseline, cue_word_ids


def _ordinal_for(text: str) -> int | None:
    head = " ".join(text.split()[:5])
    for ordinal, prefixes in _ORDINAL_PREFIXES:
        if any(head.startswith(prefix) for prefix in prefixes):
            return ordinal
    return None


def _starts_with(text: str, prefixes: tuple[str, ...]) -> bool:
    return any(text.startswith(prefix) for prefix in prefixes)


def _classify(
    text: str, *, is_first: bool, pause_s: float
) -> tuple[SemanticRole, int | None] | None:
    if is_first:
        return "hook", None
    ordinal = _ordinal_for(text)
    if ordinal is not None:
        return "list_item", ordinal
    if _starts_with(text, _CTA_TERMS) or any(term in text for term in _CTA_TERMS):
        return "cta", None
    if _starts_with(text, _PAYOFF_PREFIXES):
        return "payoff", None
    if _starts_with(text, _EXAMPLE_PREFIXES):
        return "example", None
    if _starts_with(text, _CONTEXT_PREFIXES):
        return "context_shift", None
    # A real pause is a useful deterministic fallback for an un-signposted topic
    # boundary. Keep the threshold high so normal breath gaps do not animate.
    if pause_s >= 1.15:
        return "context_shift", None
    return None


def _event_lanes(
    *, role: SemanticRole, ordinal: int | None, word_ids: list[str], event_id: str
) -> list:
    lanes: list = [
        CaptionEmphasisLane(
            kind="caption_emphasis",
            token={
                "hook": "hook_lime",
                "context_shift": "context_lime",
                "list_item": "list_keyword",
                "example": "example_soft",
                "payoff": "payoff_lime",
                "cta": "cta_lime",
            }[role],
            baseline_caption_word_ids=word_ids,
        )
    ]
    if role == "list_item" and ordinal is not None:
        lanes.append(
            TextLane(
                kind="text",
                token=f"list_number_{ordinal}",
                transcript_word_ids=[word_ids[0]],
                transform="list_number_from_sequence",
            )
        )
        lanes.append(
            SfxLane(
                kind="sfx",
                asset_id="list.pop.clean",
                sync_to_event_id=event_id,
                offset_ms=0,
                gain_token="foreground_soft",
            )
        )
    elif role == "context_shift":
        lanes.append(
            TextLane(
                kind="text",
                token="context_title",
                transcript_word_ids=word_ids[: min(7, len(word_ids))],
                transform="verbatim",
            )
        )
        lanes.append(BoundaryEffectLane(kind="boundary_effect", effect_token="soft_whip"))
    elif role == "cta":
        lanes.append(
            SfxLane(
                kind="sfx",
                asset_id="cta.click.clean",
                sync_to_event_id=event_id,
                offset_ms=0,
                gain_token="foreground_soft",
            )
        )
    return lanes


def plan_smart_captions(
    cues: list[dict[str, Any]],
    *,
    preset_version: str,
    language: str,
) -> SmartPlanBuild | None:
    """Build a closed-token Smart plan from corrected, word-timed caption cues."""

    normalized_words, baseline, cue_word_ids = _normalize_captions(cues, language=language)
    if not normalized_words or not baseline:
        return None

    by_id = {word.word_id: word for word in normalized_words}
    events: list[SmartEditEvent] = []
    previous_end_s = 0.0
    last_context_start_s = -999.0
    for logical_index, (cue_index, word_ids) in enumerate(cue_word_ids):
        cue = cues[cue_index]
        folded = _fold(str(cue.get("text") or ""))
        start_s = by_id[word_ids[0]].start_ms / 1000
        end_s = by_id[word_ids[-1]].end_ms / 1000
        classified = _classify(
            folded,
            is_first=logical_index == 0,
            pause_s=max(0.0, start_s - previous_end_s),
        )
        previous_end_s = end_s
        if classified is None:
            continue
        role, ordinal = classified
        # Pause-derived context shifts can cluster around stop-start speech. One
        # transition every six seconds is the cinematic ceiling for this lane.
        if role == "context_shift" and start_s - last_context_start_s < 6.0:
            continue
        if role == "context_shift":
            last_context_start_s = start_s
        event_id = build_event_id(
            preset_version=preset_version,
            role=role,
            start_word_id=word_ids[0],
            end_word_id=word_ids[-1],
            collision_ordinal=0,
        )
        active_end_ms = max(round(end_s * 1000), round((start_s + 0.35) * 1000))
        events.append(
            SmartEditEvent(
                event_id=event_id,
                role=role,
                start_word_id=word_ids[0],
                end_word_id=word_ids[-1],
                anchor=EventAnchor(word_id=word_ids[0], offset_ms=0),
                active_start_ms=round(start_s * 1000),
                active_end_ms=active_end_ms,
                confidence_tier=(
                    "high"
                    if role in {"hook", "list_item", "cta"}
                    or _starts_with(folded, _CONTEXT_PREFIXES)
                    else "medium"
                ),
                spatial_owner=("smart_title" if role in {"context_shift", "list_item"} else None),
                enabled=True,
                lanes=_event_lanes(
                    role=role,
                    ordinal=ordinal,
                    word_ids=word_ids,
                    event_id=event_id,
                ),
                provenance=[PLANNER_VERSION, f"preset:{preset_version}"],
            )
        )

    document = SmartEditPlanDocument(baseline_captions=baseline, events=events)
    return SmartPlanBuild(
        normalized_words=normalized_words,
        document=document,
        planner_versions={"semantic_planner": PLANNER_VERSION},
        validation_receipt={
            "valid": True,
            "normalized_word_count": len(normalized_words),
            "baseline_caption_count": len(baseline),
            "event_count": len(events),
            "roles": {
                role: sum(event.role == role for event in events)
                for role in (
                    "hook",
                    "context_shift",
                    "list_item",
                    "example",
                    "payoff",
                    "cta",
                )
            },
        },
    )
