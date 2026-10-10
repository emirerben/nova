"""Structured per-section payloads for the live plan feed (KRI-439 / KRI-447 / KRI-448).

Pure functions from the facts a render path already holds (a pinned guided plan, a phone
recipe, a rendered variant) to the contract shapes in `plan_contract.py`. Nothing here
raises: a payload that cannot be built or validated is `None`, and the block keeps its
`summary` (old clients and the Swift decode-with-fallback render from that).

Doc: docs/pipelines/live-plan-blocks.md. Never log or return LLM text as `look` chips.
"""

from __future__ import annotations

import json
import statistics
from collections.abc import Callable
from typing import Any

import structlog
from pydantic import ValidationError

from app.kria.plan_contract import (
    MAX_CAPTION_LINES,
    MAX_CLIPS,
    MAX_OVERLAYS,
    MAX_SFX,
    PAYLOAD_MAX_BYTES,
    PAYLOAD_MODELS,
    TRANSITION_DISPLAY,
)

log = structlog.get_logger()

# Recipe-side transition kinds the frozen TRANSITION_DISPLAY does not list. Both fade
# variants are a "fade" to the creator; wipes stay unmapped (null, never a guess).
_RECIPE_TRANSITIONS: dict[str, str] = {"fade_black": "fade", "fade_white": "fade"}

_CLIP_TEXT_PREFIXES = ("clip-label-", "montage-text-")
_MOTION_FPS = 30.0

TrackMeta = Callable[[str], dict[str, Any] | None]


# ── shared helpers ───────────────────────────────────────────────────────────


def display_transition(value: Any) -> str | None:
    """Editor / recipe / template wire value -> cut|dissolve|whip|fade, else None."""
    if not isinstance(value, str):
        return None
    key = value.strip().lower()
    return TRANSITION_DISPLAY.get(key) or _RECIPE_TRANSITIONS.get(key)


def _f(value: Any) -> float | None:
    if isinstance(value, bool) or not isinstance(value, int | float):
        return None
    number = float(value)
    return number if number == number and abs(number) != float("inf") else None


def _r(value: float | None, places: int = 3) -> float | None:
    return None if value is None else round(value, places)


def _s(value: Any, limit: int) -> str | None:
    text = " ".join(str(value or "").split())
    return text[:limit] or None


def _rows(value: Any) -> list[dict[str, Any]]:
    return [row for row in value if isinstance(row, dict)] if isinstance(value, list) else []


def humanize(value: Any) -> str:
    return str(value or "").replace("_", " ").strip().capitalize()


def bpm_from_beats(beats: Any) -> float | None:
    """`round(60 / median(diff(beats)))`; None when there are fewer than two beats."""
    try:
        points = sorted(float(b) for b in beats or [])
        gaps = [b - a for a, b in zip(points, points[1:], strict=False) if b - a > 0.05]
        if not gaps:
            return None
        bpm = round(60.0 / statistics.median(gaps))
        return float(bpm) if 0 < bpm <= 300 else None
    except (TypeError, ValueError, statistics.StatisticsError):
        return None


def lookup_track_meta(track_id: str) -> dict[str, Any] | None:
    """Best-effort `MusicTrack` facts (title, artist, bpm, art). Never raises."""
    try:
        from app.database import sync_session  # noqa: PLC0415
        from app.models import MusicTrack  # noqa: PLC0415

        with sync_session() as db:
            track = db.get(MusicTrack, str(track_id))
            if track is None:
                return None
            return {
                "title": track.title,
                "artist": track.artist or None,
                "bpm": bpm_from_beats(track.beat_timestamps_s),
                "art_url": track.thumbnail_url,
            }
    except Exception as exc:  # noqa: BLE001 - metadata is decoration, never a failure
        log.info("plan_payload_track_lookup_failed", error=str(exc)[:160])
        return None


def finalize_payload(section: str, raw: dict[str, Any] | None) -> dict[str, Any] | None:
    """Validate against the contract model, cap to `PAYLOAD_MAX_BYTES`, dump JSON-safe.

    Lists are truncated to the contract caps first. Over the byte cap the lists are halved
    until it fits (captions set `truncated`); still too big -> None. Never raises.
    """
    model = PAYLOAD_MODELS.get(section)
    if model is None or not isinstance(raw, dict):
        return None
    try:
        data = dict(raw)
        for key, cap in (
            ("clips", MAX_CLIPS),
            ("lines", MAX_CAPTION_LINES),
            ("items", MAX_SFX if section == "sfx" else MAX_OVERLAYS),
        ):
            if isinstance(data.get(key), list) and len(data[key]) > cap:
                data[key] = data[key][:cap]
                if section == "captions":
                    data["truncated"] = True
        for _ in range(8):
            dumped = model.model_validate(data).model_dump(mode="json", exclude_none=True)
            if len(json.dumps(dumped, separators=(",", ":")).encode("utf-8")) <= PAYLOAD_MAX_BYTES:
                return dumped
            lists = [k for k in ("clips", "lines", "items") if isinstance(data.get(k), list)]
            if not lists:
                return None
            for key in lists:
                data[key] = data[key][: max(1, len(data[key]) // 2)]
            if section == "captions":
                data["truncated"] = True
        return None
    except (ValidationError, TypeError, ValueError) as exc:
        log.info("plan_payload_invalid", section=section, error=str(exc)[:200])
        return None


def payload_equal(a: dict[str, Any] | None, b: dict[str, Any] | None) -> bool:
    def canon(value: dict[str, Any]) -> str:
        return json.dumps(value, sort_keys=True, separators=(",", ":"), default=str)

    return a is not None and b is not None and canon(a) == canon(b)


# ── per-section raw builders ─────────────────────────────────────────────────


def _caption_lines(
    cues: Any = None, bars: Any = None, *, start_index: int = 0
) -> tuple[list[dict[str, Any]], int]:
    lines: list[dict[str, Any]] = []
    for index, cue in enumerate(_rows(cues)):
        text, start, end = _s(cue.get("text"), 200), _f(cue.get("start_s")), _f(cue.get("end_s"))
        if text is None or start is None or end is None:
            continue
        cue_id = cue.get("id")
        lines.append(
            {
                "id": str(cue_id) if cue_id else f"cue-{index + start_index}",
                "kind": "cue",
                "text": text,
                "start_s": _r(max(0.0, start)),
                "end_s": _r(max(0.0, end)),
            }
        )
    for bar in _rows(bars):
        text, start, end = _s(bar.get("text"), 200), _f(bar.get("start_s")), _f(bar.get("end_s"))
        if text is None or start is None or end is None or not bar.get("id"):
            continue
        lines.append(
            {
                "id": str(bar["id"]),
                "kind": "bar",
                "text": text,
                "start_s": _r(max(0.0, start)),
                "end_s": _r(max(0.0, end)),
            }
        )
    lines.sort(key=lambda line: (line["start_s"], line["id"]))
    return lines, len(lines)


def captions_raw(lines: list[dict[str, Any]], count: int | None = None) -> dict[str, Any] | None:
    if not lines:
        return None
    return {
        "count": int(count if count is not None else len(lines)),
        "lines": lines,
        "truncated": False,
    }


def title_raw(text: Any, bar_id: Any = None, highlight_word: Any = None) -> dict[str, Any] | None:
    clean = _s(text, 300)
    if clean is None:
        return None
    return {
        "text": clean,
        "highlight_word": _s(highlight_word, 60),
        "bar_id": str(bar_id) if bar_id else None,
    }


def post_caption_raw(text: Any, hashtags: Any) -> dict[str, Any] | None:
    clean = " ".join(str(text or "").split())[:2200]
    if not clean:
        return None
    tags: list[str] = []
    for tag in hashtags or []:
        value = str(tag or "").strip().lstrip("#").strip()
        if value and value not in tags:
            tags.append(value[:60])
    return {"text": clean, "hashtags": tags[:15], "platform": "tiktok"}


def look_raw(
    *,
    style_id: Any = None,
    font: Any = None,
    look_presets: list[str] | None = None,
    extra: list[str] | None = None,
) -> dict[str, Any] | None:
    """Chips are derived from ids and enums only, never from model-written text."""
    chips: list[str] = []

    def add(chip: str | None) -> None:
        if chip and chip not in chips and len(chips) < 6:
            chips.append(chip[:24])

    if style_id:
        add(humanize(style_id))
    if font:
        family = str(font).split("-")[0].strip()
        add(f"{humanize(family)} titles" if family else None)
    for preset in look_presets or []:
        if preset and preset != "none":
            add(humanize(preset))
    for chip in extra or []:
        add(chip)
    if not chips:
        return None
    return {
        "chips": chips,
        "style_id": _s(style_id, 80),
        "look_preset": next((p for p in look_presets or [] if p and p != "none"), None),
    }


def _overlay_items(
    media: Any = None, visual: Any = None, motion: Any = None
) -> list[dict[str, Any]]:
    items: list[dict[str, Any]] = []
    for row in _rows(media):
        start, end = _f(row.get("start_s")), _f(row.get("end_s"))
        if start is None or end is None or not row.get("id"):
            continue
        mode = row.get("display_mode")
        items.append(
            {
                "id": str(row["id"]),
                "kind": "video" if row.get("kind") == "video" else "image",
                "label": _s(row.get("label"), 60),
                "start_s": _r(max(0.0, start)),
                "end_s": _r(max(0.0, end)),
                "display_mode": mode if mode in ("pip", "fullscreen") else None,
            }
        )
    for row in _rows(visual):
        start, end = _f(row.get("start_s")), _f(row.get("end_s"))
        if start is None or end is None or not row.get("id"):
            continue
        items.append(
            {
                "id": str(row["id"]),
                "kind": "text_card" if row.get("kind") == "text_card" else "visual",
                "label": _s(row.get("label"), 60),
                "start_s": _r(max(0.0, start)),
                "end_s": _r(max(0.0, end)),
            }
        )
    for row in _rows(motion):
        start, end = _f(row.get("start_frame")), _f(row.get("end_frame_exclusive"))
        if start is None or end is None or not row.get("id"):
            continue
        items.append(
            {
                "id": str(row["id"]),
                "kind": "motion",
                "label": _s(row.get("label"), 60),
                "start_s": _r(max(0.0, start / _MOTION_FPS)),
                "end_s": _r(max(0.0, end / _MOTION_FPS)),
            }
        )
    items.sort(key=lambda item: (item["start_s"], item["id"]))
    return items


def _sfx_items(effects: Any) -> list[dict[str, Any]]:
    items: list[dict[str, Any]] = []
    for row in _rows(effects):
        at = _f(row.get("at_s"))
        if at is None or not row.get("id"):
            continue
        gain = _f(row.get("gain"))
        items.append(
            {
                "id": str(row["id"]),
                "label": _s(row.get("label"), 60),
                "at_s": _r(max(0.0, at)),
                "gain": None if gain is None else _r(min(2.0, max(0.0, gain))),
            }
        )
    items.sort(key=lambda item: (item["at_s"], item["id"]))
    return items


def sfx_raw(effects: Any) -> dict[str, Any] | None:
    items = _sfx_items(effects)
    return {"count": len(items), "items": items} if items else None


def overlays_raw(media: Any = None, visual: Any = None, motion: Any = None) -> dict | None:
    items = _overlay_items(media, visual, motion)
    return {"count": len(items), "items": items} if items else None


def _mix(
    music_level: float | None = None,
    original_level: float | None = None,
    music_gain_db: float | None = None,
) -> dict[str, float] | None:
    mix: dict[str, float] = {}
    if music_level is not None:
        mix["music_level"] = min(1.0, max(0.0, music_level))
    if original_level is not None:
        mix["original_level"] = min(1.0, max(0.0, original_level))
    if music_gain_db is not None:
        mix["music_gain_db"] = min(0.0, max(-40.0, music_gain_db))
    return mix or None


# ── guided plan adapter ──────────────────────────────────────────────────────


def _label_for_window(texts: list[dict[str, Any]], start: float, end: float) -> str | None:
    best: tuple[float, str] | None = None
    for row in texts:
        row_start, row_end = _f(row.get("start_s")), _f(row.get("end_s"))
        if row_start is None or row_end is None:
            continue
        overlap = min(end, row_end) - max(start, row_start)
        if overlap > 0 and (best is None or overlap > best[0]):
            best = (overlap, str(row.get("text") or ""))
    return _s(best[1], 60) if best else None


def guided_clips_raw(plan: dict[str, Any]) -> dict[str, Any] | None:
    timeline = _rows(plan.get("story_timeline"))
    if not timeline:
        return None
    policy = (
        plan.get("transition_policy") if isinstance(plan.get("transition_policy"), dict) else {}
    )
    labels = [
        row
        for row in _rows(plan.get("text_elements"))
        if str(row.get("id") or "").startswith(_CLIP_TEXT_PREFIXES) and row.get("text")
    ]
    clips: list[dict[str, Any]] = []
    for index, moment in enumerate(timeline):
        start, end = _f(moment.get("output_start_s")), _f(moment.get("output_end_s"))
        if start is None or end is None:
            duration = _f(moment.get("duration_s")) or 0.0
            start = clips[-1]["end_s"] if clips else 0.0
            end = start + duration
        transition: str | None = None
        duration_s: float | None = None
        if index < len(timeline) - 1:
            raw = moment.get("transition_after") or policy.get("type")
            transition = display_transition(raw)
            if transition is not None and transition != "cut":
                duration_s = _f(moment.get("transition_duration_s"))
                if duration_s is None:
                    duration_s = _f(policy.get("duration_s"))
                duration_s = None if duration_s is None else min(1.0, max(0.0, duration_s))
        clips.append(
            {
                "index": index,
                "media_id": _s(moment.get("media_id"), 160),
                "kind": "image" if moment.get("kind") == "image" else "video",
                "role": _s(moment.get("topic"), 40),
                "label": _label_for_window(labels, start, end),
                "start_s": _r(max(0.0, start)),
                "end_s": _r(max(0.0, end)),
                "source_start_s": _r(_f(moment.get("source_start_s"))),
                "source_end_s": _r(_f(moment.get("source_end_s"))),
                "transition": transition,
                "transition_duration_s": _r(duration_s),
            }
        )
    total = _f(plan.get("resolved_duration_s")) or (clips[-1]["end_s"] if clips else 0.0)
    return {"total_duration_s": _r(total), "clips": clips}


def guided_music_raw(plan: dict[str, Any], track_meta: TrackMeta | None) -> dict[str, Any] | None:
    lookup = track_meta or lookup_track_meta
    original = _f(plan.get("editor_original_level"))
    song = plan.get("user_song")
    music = plan.get("music")
    if isinstance(music, dict) and music.get("title"):
        meta = lookup(str(music.get("track_id"))) if music.get("track_id") else None
        meta = meta or {}
        level = _f(music.get("level"))
        return {
            "source": "catalog",
            "track_id": _s(music.get("track_id"), 160),
            "title": _s(music.get("title"), 120),
            "artist": _s(meta.get("artist") or music.get("artist"), 120),
            "bpm": meta.get("bpm"),
            "start_s": _r(_f(music.get("start_s"))),
            "mix": _mix(
                level if level is not None else 1.0,
                original if original is not None else _f(plan.get("editor_audio_level")),
            ),
        }
    if isinstance(song, dict):
        volume = _f(song.get("volume"))
        return {
            "source": "user_song",
            "mode": "lipsync" if song.get("mode") == "lipsync" else "background",
            "start_s": _r(_f(song.get("window_start_s"))),
            "mix": _mix(
                volume if volume is not None else 1.0, original if original is not None else 0.0
            ),
        }
    if plan.get("narration"):
        return {"source": "voiceover", "mix": _mix(None, original)}
    return None


def guided_look_raw(plan: dict[str, Any]) -> dict[str, Any] | None:
    typography = plan.get("typography") if isinstance(plan.get("typography"), dict) else {}
    presets: list[str] = []
    for moment in _rows(plan.get("story_timeline")):
        preset = moment.get("look_preset")
        if isinstance(preset, str) and preset != "none" and preset not in presets:
            presets.append(preset)
    return look_raw(
        style_id=typography.get("style_id"),
        font=typography.get("font"),
        look_presets=presets[:2],
    )


def guided_title_raw(plan: dict[str, Any]) -> dict[str, Any] | None:
    title = next(
        (
            row
            for row in _rows(plan.get("text_elements"))
            if row.get("text")
            and not str(row.get("id") or "").startswith(_CLIP_TEXT_PREFIXES)
            and not row.get("caption_cue")
        ),
        None,
    )
    return title_raw(title.get("text"), title.get("id")) if title else None


def guided_captions_raw(plan: dict[str, Any]) -> dict[str, Any] | None:
    bars = [
        *_rows(plan.get("narration_label_text_elements")),
        *_rows(plan.get("context_label_text_elements")),
        *[row for row in _rows(plan.get("text_elements")) if row.get("caption_cue")],
    ]
    lines, count = _caption_lines(plan.get("caption_cues"), bars)
    return captions_raw(lines, count)


def guided_section_raw(
    plan: dict[str, Any], section: str, track_meta: TrackMeta | None = None
) -> dict[str, Any] | None:
    """One section's raw payload from a pinned guided plan (None = nothing to show)."""
    if section == "title":
        return guided_title_raw(plan)
    if section == "clips":
        return guided_clips_raw(plan)
    if section == "captions":
        return guided_captions_raw(plan)
    if section == "music":
        return guided_music_raw(plan, track_meta)
    if section == "sfx":
        return sfx_raw(plan.get("editor_sound_effects"))
    if section == "overlays":
        return overlays_raw(
            plan.get("editor_media_overlays"),
            plan.get("editor_visual_blocks"),
            plan.get("editor_motion_scenes"),
        )
    if section == "look":
        return guided_look_raw(plan)
    if section == "post_caption":
        caption = plan.get("post_caption")
        if isinstance(caption, dict):
            return post_caption_raw(caption.get("text"), caption.get("hashtags"))
    return None


# ── phone recipe adapter ─────────────────────────────────────────────────────


def _recipe_track_clips(recipe: Any, *, kind: str | None = None) -> list[Any]:
    clips: list[Any] = []
    for track in getattr(recipe, "tracks", None) or []:
        if kind is not None and getattr(track, "kind", None) != kind:
            continue
        clips.extend(getattr(track, "clips", None) or [])
    return clips


def recipe_clips_raw(recipe: Any, *, clip_count: int | None = None) -> dict[str, Any] | None:
    """Clips on the video track in output order. `transition` leaves a clip into the next:
    a recipe stores the INCOMING transition on each clip, so shift it back by one."""
    video = [
        clip
        for clip in _recipe_track_clips(recipe, kind="video")
        if _f(getattr(clip, "timeline_start", None)) is not None
    ]
    if not video:
        return None
    video.sort(key=lambda clip: float(clip.timeline_start))
    rows: list[dict[str, Any]] = []
    for index, clip in enumerate(video):
        rate = _f(getattr(clip, "rate", None)) or 1.0
        hold = _f(getattr(clip, "hold_duration", None))
        source_duration = _f(getattr(clip, "source_duration", None)) or 0.0
        length = hold if hold else source_duration / rate if rate > 0 else source_duration
        start = float(clip.timeline_start)
        following = video[index + 1] if index + 1 < len(video) else None
        incoming = getattr(following, "transition", None) if following is not None else None
        transition = display_transition(getattr(incoming, "kind", None)) if incoming else None
        if following is not None and incoming is None:
            transition = "cut"
        duration = _f(getattr(incoming, "duration", None)) if incoming else None
        source_start = _f(getattr(clip, "source_start", None))
        rows.append(
            {
                "index": index,
                "media_id": _s(getattr(clip, "source_asset_id", None), 160),
                "kind": "image" if hold else "video",
                "start_s": _r(max(0.0, start)),
                "end_s": _r(max(0.0, start + length)),
                "source_start_s": _r(source_start),
                "source_end_s": _r(
                    None if source_start is None else source_start + source_duration
                ),
                "transition": transition,
                "transition_duration_s": _r(
                    None if duration is None or transition in (None, "cut") else min(1.0, duration)
                ),
            }
        )
    duration_total = _f(getattr(recipe, "duration", None)) or rows[-1]["end_s"]
    return {"total_duration_s": _r(duration_total), "clips": rows}


def recipe_sfx_overlays(recipe: Any) -> tuple[dict | None, dict | None]:
    cutaways = "talking-head-cutaways"
    sfx_items = [
        {
            "id": str(getattr(clip, "id", "") or f"sfx-{i}"),
            "at_s": float(getattr(clip, "timeline_start", 0.0) or 0.0),
            "gain": getattr(clip, "volume", None),
        }
        for i, clip in enumerate(
            clip
            for track in getattr(recipe, "tracks", None) or []
            if getattr(track, "id", None) == "sfx"
            for clip in getattr(track, "clips", None) or []
        )
    ]
    overlay_rows: list[dict[str, Any]] = []
    for track in getattr(recipe, "tracks", None) or []:
        if getattr(track, "kind", None) != "overlay" or getattr(track, "id", None) == cutaways:
            continue
        for i, clip in enumerate(getattr(track, "clips", None) or []):
            start = float(getattr(clip, "timeline_start", 0.0) or 0.0)
            rate = _f(getattr(clip, "rate", None)) or 1.0
            hold = _f(getattr(clip, "hold_duration", None))
            length = hold if hold else float(getattr(clip, "source_duration", 0.0) or 0.0) / rate
            overlay_rows.append(
                {
                    "id": str(getattr(clip, "id", "") or f"{track.id}-{i}"),
                    "kind": "image" if hold else "video",
                    "start_s": start,
                    "end_s": start + length,
                    "display_mode": "pip",
                }
            )
    return sfx_raw(sfx_items), overlays_raw(overlay_rows)


# ── rendered variant adapter (cloud, non-guided) ─────────────────────────────


def variant_clips_raw(variant: dict[str, Any]) -> dict[str, Any] | None:
    timeline = variant.get("user_timeline") or variant.get("ai_timeline")
    slots = _rows(timeline.get("slots")) if isinstance(timeline, dict) else []
    if not slots:
        return None
    slots = sorted(slots, key=lambda slot: int(slot.get("order") or 0))
    clips: list[dict[str, Any]] = []
    cursor = 0.0
    for index, slot in enumerate(slots):
        duration = _f(slot.get("duration_s"))
        if duration is None or duration <= 0:
            continue
        start_in = _f(slot.get("in_s"))
        transition = None
        if index < len(slots) - 1:
            transition = display_transition(
                slot.get("transition_after") or slots[index + 1].get("transition_in")
            )
        clips.append(
            {
                "index": len(clips),
                "kind": "video",
                "role": _s(slot.get("slot_type"), 40),
                "start_s": _r(cursor),
                "end_s": _r(cursor + duration),
                "source_start_s": _r(start_in),
                "source_end_s": _r(None if start_in is None else start_in + duration),
                "transition": transition,
            }
        )
        cursor += duration
    if not clips:
        return None
    return {"total_duration_s": _r(_f(variant.get("duration_s")) or cursor), "clips": clips}


def variant_raws(
    variant: dict[str, Any], track_meta: TrackMeta | None = None
) -> dict[str, dict[str, Any] | None]:
    """Payloads for a rendered cloud variant (the non-guided generative path)."""
    texts = _rows(variant.get("text_elements"))
    title = next(
        (
            row
            for row in texts
            if row.get("text")
            and not row.get("caption_cue")
            and not str(row.get("id") or "").startswith(_CLIP_TEXT_PREFIXES)
        ),
        None,
    )
    bars = [row for row in texts if row.get("caption_cue")]
    lines, count = _caption_lines(variant.get("caption_cues"), bars)
    music: dict[str, Any] | None = None
    track_id = variant.get("music_track_id")
    if track_id:
        meta = (track_meta or lookup_track_meta)(str(track_id)) or {}
        mix = variant.get("audio_mix") if isinstance(variant.get("audio_mix"), dict) else {}
        window = (
            variant.get("music_window") if isinstance(variant.get("music_window"), dict) else {}
        )
        music = {
            "source": "catalog",
            "track_id": str(track_id),
            "title": _s(meta.get("title"), 120),
            "artist": _s(meta.get("artist"), 120),
            "bpm": meta.get("bpm"),
            "start_s": _r(_f(window.get("start_s") if window else variant.get("music_start_s"))),
            "mix": _mix(
                _f(mix.get("music_level")),
                _f(mix.get("original_level")),
                _f(mix.get("music_gain_db")),
            ),
        }
    return {
        "title": title_raw(title.get("text"), title.get("id")) if title else None,
        "clips": variant_clips_raw(variant),
        "captions": captions_raw(lines, count),
        "music": music,
        "sfx": sfx_raw(variant.get("sound_effects")),
        "overlays": overlays_raw(
            variant.get("media_overlays"),
            variant.get("visual_blocks"),
            variant.get("motion_scenes"),
        ),
        "look": look_raw(
            style_id=variant.get("style_set_id"),
            extra=[humanize(variant["caption_style"])]
            if variant.get("caption_style") not in (None, "", "none")
            else None,
        ),
    }


def finalized(raws: dict[str, dict[str, Any] | None]) -> dict[str, dict[str, Any] | None]:
    return {section: finalize_payload(section, raw) for section, raw in raws.items()}


# Public names for the builders `plan_blocks` composes with.
caption_lines = _caption_lines
to_float = _f
mix_levels = _mix
