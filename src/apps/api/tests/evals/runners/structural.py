"""Deterministic structural assertions for the Big-3 agents.

Each `check_*` function takes a parsed Output instance plus its Input and returns
a list of human-readable failure strings. Empty list means the output passes the
structural floor. These run in BOTH replay and live mode and need no network.

Constants are imported from the agent modules themselves so the eval can never
drift out of sync with the runtime's own validation rules.
"""

from __future__ import annotations

import re
from typing import Any

from app.agents.audio_template import AudioTemplateOutput
from app.agents.clip_metadata import (
    _BALL_BLACKLIST,
    _BALL_WHITELIST,
    ClipMetadataInput,
    ClipMetadataOutput,
)
from app.agents.creative_direction import CreativeDirectionOutput
from app.agents.platform_copy import PlatformCopyOutput
from app.agents.template_recipe import (
    _VALID_COLOR_HINTS,
    _VALID_INTERSTITIAL_TYPES,
    _VALID_OVERLAY_ROLES,
    _VALID_TRANSITION_TYPES,
    TemplateRecipeOutput,
)
from app.agents.transcript import TranscriptOutput
from app.pipeline.agents.copy_writer import (
    INSTAGRAM_CAPTION_MAX,
    TIKTOK_CAPTION_MAX,
    YOUTUBE_DESCRIPTION_MAX,
    YOUTUBE_TITLE_MAX,
)

# ── template_recipe ──────────────────────────────────────────────────────────


def check_template_recipe(output: TemplateRecipeOutput) -> list[str]:
    failures: list[str] = []

    if output.shot_count != len(output.slots):
        failures.append(f"shot_count={output.shot_count} != len(slots)={len(output.slots)}")

    if output.total_duration_s > 0:
        slot_sum = sum(float(s.get("target_duration_s", 0.0) or 0.0) for s in output.slots)
        delta = abs(slot_sum - output.total_duration_s)
        if delta > 5.0:
            failures.append(
                f"slot durations sum to {slot_sum:.1f}s but total_duration_s="
                f"{output.total_duration_s:.1f}s (delta {delta:.1f}s > 5.0s tolerance)"
            )

    if output.hook_duration_s <= 0:
        failures.append(f"hook_duration_s={output.hook_duration_s} must be > 0")
    elif output.total_duration_s > 0 and output.hook_duration_s > output.total_duration_s:
        failures.append(
            f"hook_duration_s={output.hook_duration_s} exceeds "
            f"total_duration_s={output.total_duration_s}"
        )

    for i, slot in enumerate(output.slots, start=1):
        slot_dur = float(slot.get("target_duration_s", 0.0) or 0.0)

        energy = slot.get("energy")
        if energy is not None:
            try:
                e = float(energy)
                if e < 0 or e > 10:
                    failures.append(f"slot {i}: energy={e} outside [0, 10]")
            except (TypeError, ValueError):
                failures.append(f"slot {i}: energy={energy!r} not numeric")

        transition_in = slot.get("transition_in")
        if transition_in is not None and transition_in not in _VALID_TRANSITION_TYPES:
            valid = sorted(_VALID_TRANSITION_TYPES)
            failures.append(f"slot {i}: transition_in={transition_in!r} not in {valid}")

        color_hint = slot.get("color_hint")
        if color_hint is not None and color_hint not in _VALID_COLOR_HINTS:
            failures.append(
                f"slot {i}: color_hint={color_hint!r} not in {sorted(_VALID_COLOR_HINTS)}"
            )

        speed = slot.get("speed_factor")
        if speed is not None:
            try:
                sf = float(speed)
                if sf < 0.25 or sf > 4.0:
                    failures.append(f"slot {i}: speed_factor={sf} outside [0.25, 4.0]")
            except (TypeError, ValueError):
                failures.append(f"slot {i}: speed_factor={speed!r} not numeric")

        for j, ov in enumerate(slot.get("text_overlays", []) or []):
            if not isinstance(ov, dict):
                failures.append(f"slot {i} overlay {j}: not a dict")
                continue
            role = ov.get("role")
            if role not in _VALID_OVERLAY_ROLES:
                failures.append(
                    f"slot {i} overlay {j}: role={role!r} not in {sorted(_VALID_OVERLAY_ROLES)}"
                )
            try:
                start = float(ov.get("start_s", 0.0) or 0.0)
                end = float(ov.get("end_s", 0.0) or 0.0)
            except (TypeError, ValueError):
                failures.append(f"slot {i} overlay {j}: non-numeric start/end")
                continue
            if start >= end:
                failures.append(f"slot {i} overlay {j}: start_s={start} >= end_s={end}")
            if slot_dur > 0 and start >= slot_dur:
                failures.append(
                    f"slot {i} overlay {j}: start_s={start} outside slot duration {slot_dur}"
                )

    for k, inter in enumerate(output.interstitials):
        itype = inter.get("type")
        if itype not in _VALID_INTERSTITIAL_TYPES:
            failures.append(
                f"interstitial {k}: type={itype!r} not in {sorted(_VALID_INTERSTITIAL_TYPES)}"
            )
        try:
            after_slot = int(inter.get("after_slot", 0))
        except (TypeError, ValueError):
            failures.append(f"interstitial {k}: after_slot not integer")
            continue
        if after_slot < 1 or after_slot > output.shot_count:
            failures.append(
                f"interstitial {k}: after_slot={after_slot} outside [1, {output.shot_count}]"
            )
        try:
            animate_s = float(inter.get("animate_s", 0.0))
            if animate_s < 0 or animate_s > 2:
                failures.append(f"interstitial {k}: animate_s={animate_s} outside [0, 2]")
        except (TypeError, ValueError):
            failures.append(f"interstitial {k}: animate_s not numeric")
        try:
            hold_s = float(inter.get("hold_s", 0.0))
            if hold_s < 0.1 or hold_s > 3.0:
                failures.append(f"interstitial {k}: hold_s={hold_s} outside [0.1, 3.0]")
        except (TypeError, ValueError):
            failures.append(f"interstitial {k}: hold_s not numeric")

    return failures


# ── clip_metadata ────────────────────────────────────────────────────────────


_FOOTBALL_HINT_KEYWORDS = ("ball", "top", "futbol", "football", "soccer")
_VAGUE_DESCRIPTION_PATTERNS = (
    re.compile(r"^\s*player on (the )?(field|pitch)\s*\.?\s*$", re.IGNORECASE),
    re.compile(r"^\s*wide shot\s*\.?\s*$", re.IGNORECASE),
    re.compile(r"^\s*empty (pitch|field)\s*\.?\s*$", re.IGNORECASE),
    re.compile(r"^\s*scene\s*\.?\s*$", re.IGNORECASE),
    re.compile(r"^\s*moment\s*\.?\s*$", re.IGNORECASE),
)


def check_clip_metadata(
    output: ClipMetadataOutput,
    input: ClipMetadataInput,  # noqa: A002
) -> list[str]:
    failures: list[str] = []

    if output.hook_score < 0 or output.hook_score > 10:
        failures.append(f"hook_score={output.hook_score} outside [0, 10]")

    n = len(output.best_moments)
    if n < 2 or n > 5:
        failures.append(f"best_moments has {n} entries; prompt requires 2-5")

    for i, m in enumerate(output.best_moments):
        if m.start_s >= m.end_s:
            failures.append(f"moment {i}: start_s={m.start_s} >= end_s={m.end_s}")
        if m.energy < 0 or m.energy > 10:
            failures.append(f"moment {i}: energy={m.energy} outside [0, 10]")
        desc = (m.description or "").strip()
        if not desc:
            failures.append(f"moment {i}: description is empty")
            continue
        if len(desc.split()) < 3:
            failures.append(f"moment {i}: description {desc!r} has < 3 words (likely vague)")
        for pat in _VAGUE_DESCRIPTION_PATTERNS:
            if pat.match(desc):
                failures.append(f"moment {i}: description {desc!r} matches vague-label blocklist")
                break

    hint_lower = (input.filter_hint or "").lower()
    if any(k in hint_lower for k in _FOOTBALL_HINT_KEYWORDS):
        for i, m in enumerate(output.best_moments):
            d = (m.description or "").lower()
            if any(b in d for b in _BALL_BLACKLIST):
                failures.append(
                    f"moment {i}: football-mode kept blacklisted description {m.description!r}"
                )
                continue
            if not any(w in d for w in _BALL_WHITELIST):
                failures.append(
                    f"moment {i}: football-mode kept moment without "
                    f"whitelist verb: {m.description!r}"
                )

    if input.segment is not None:
        seg_dur = input.segment.end_s - input.segment.start_s
        if seg_dur >= 30.0 and output.best_moments:
            longest = max(m.end_s - m.start_s for m in output.best_moments)
            if longest < 13.0:
                failures.append(
                    f"segment {seg_dur:.1f}s long but longest moment is {longest:.1f}s "
                    "(prompt asks for at least one ≥ 13s on long clips)"
                )

    return failures


# ── creative_direction ───────────────────────────────────────────────────────


_TOPIC_KEYWORDS: dict[str, tuple[str, ...]] = {
    "pacing": ("pace", "pacing", "rhythm", "tempo", "speed"),
    "transition": ("transition", "cut", "wipe", "curtain", "barn-door", "iris", "dissolve"),
    "color": ("color", "colour", "grade", "warm", "cool", "saturation", "tone"),
    "speed_ramp": (
        "ramp",
        "slow-mo",
        "slowmo",
        "slow motion",
        "speed-up",
        "speed up",
        "fast forward",
    ),
    "audio_sync": ("beat", "music", "sound", "audio", "sync", "drop"),
    "on_camera": (
        "on-camera",
        "on camera",
        "talking head",
        "voiceover",
        "voice-over",
        "narration",
        "host",
        "creator",
    ),
    "letterbox": ("letterbox", "bars", "aspect", "framing"),
    "niche": ("niche", "topic", "genre", "subject", "category"),
}


def check_creative_direction(output: CreativeDirectionOutput) -> list[str]:
    failures: list[str] = []
    text = (output.text or "").strip()

    if not text:
        failures.append("creative_direction text is empty")
        return failures

    word_count = len(text.split())
    if word_count < 50:
        failures.append(f"word count {word_count} below floor of 50 (likely under-described)")
    if word_count > 400:
        failures.append(f"word count {word_count} above ceiling of 400 (prompt caps at 400 tokens)")

    text_lower = text.lower()
    matched_topics: list[str] = []
    for topic, keywords in _TOPIC_KEYWORDS.items():
        if any(k in text_lower for k in keywords):
            matched_topics.append(topic)
    if len(matched_topics) < 4:
        failures.append(
            f"only {len(matched_topics)} topics mentioned ({matched_topics}); "
            f"prompt requires coverage of ≥4 of {list(_TOPIC_KEYWORDS.keys())}"
        )

    return failures


# ── transcript ───────────────────────────────────────────────────────────────


_OVERLAP_EPSILON_S = 0.01
_MIN_NON_LOW_CONFIDENCE = 0.6


def check_transcript(output: TranscriptOutput, input: Any) -> list[str]:  # noqa: A002
    """Structural floor for a Gemini transcript.

    Ground truth comes from the recorded `output` itself plus the agent's input
    (file_uri only — no audio duration is provided to the agent at runtime, so
    we don't try to bound by it here).
    """
    failures: list[str] = []

    if not output.words:
        failures.append("words list is empty")
        return failures

    last_end = -1.0
    confidences: list[float] = []
    for i, w in enumerate(output.words):
        if not (w.text or "").strip():
            failures.append(f"word {i}: text is empty")
        if w.start_s >= w.end_s:
            failures.append(f"word {i}: start_s={w.start_s} >= end_s={w.end_s}")
        if w.start_s + _OVERLAP_EPSILON_S < last_end:
            failures.append(f"word {i}: start_s={w.start_s} overlaps prior end_s={last_end}")
        last_end = max(last_end, w.end_s)
        confidences.append(float(w.confidence))

    if not output.full_text.strip():
        failures.append("full_text is empty but words list is non-empty")
    else:
        joined = " ".join((w.text or "").strip() for w in output.words).strip()
        if joined:
            normalized_joined = re.sub(r"\s+", " ", joined.lower())
            normalized_full = re.sub(r"\s+", " ", output.full_text.strip().lower())
            normalized_full = re.sub(r"[^\w\s]", "", normalized_full)
            normalized_joined = re.sub(r"[^\w\s]", "", normalized_joined)
            if (
                normalized_full
                and normalized_joined not in normalized_full
                and normalized_full not in normalized_joined
            ):
                failures.append(
                    "full_text does not align with concatenated words "
                    "(words and full_text describe different content)"
                )

    if not output.low_confidence and confidences:
        avg_conf = sum(confidences) / len(confidences)
        if avg_conf < _MIN_NON_LOW_CONFIDENCE:
            failures.append(
                f"low_confidence=False but avg word confidence "
                f"{avg_conf:.2f} < {_MIN_NON_LOW_CONFIDENCE}"
            )

    return failures


# ── platform_copy ────────────────────────────────────────────────────────────


_PLACEHOLDER_PATTERNS = (
    re.compile(r"\{[^}]{1,40}\}"),
    re.compile(r"\[insert\b", re.IGNORECASE),
    re.compile(r"\[your\b", re.IGNORECASE),
    re.compile(r"<insert\b", re.IGNORECASE),
    re.compile(r"\bTODO\b"),
    re.compile(r"\bplaceholder\b", re.IGNORECASE),
)


def check_platform_copy(output: PlatformCopyOutput) -> list[str]:
    """Structural floor for TikTok/IG/YT copy.

    Field truncation is enforced at the Pydantic layer (see copy_writer.py
    field_validators). We don't re-test that — we test the things truncation
    can't catch: empties, placeholder leakage, copy-paste duplication.
    """
    failures: list[str] = []
    pc = output.value

    def _check_text(label: str, text: str, *, max_len: int | None = None) -> None:
        s = (text or "").strip()
        if not s:
            failures.append(f"{label}: empty")
            return
        if max_len is not None and len(s) > max_len:
            failures.append(f"{label}: length {len(s)} > {max_len}")
        for pat in _PLACEHOLDER_PATTERNS:
            if pat.search(s):
                failures.append(f"{label}: contains placeholder-shaped token: {s[:80]!r}")
                break

    _check_text("tiktok.hook", pc.tiktok.hook, max_len=150)
    _check_text("tiktok.caption", pc.tiktok.caption, max_len=TIKTOK_CAPTION_MAX)
    if not pc.tiktok.hashtags:
        failures.append("tiktok.hashtags: empty (need ≥ 1)")

    _check_text("instagram.hook", pc.instagram.hook, max_len=150)
    _check_text("instagram.caption", pc.instagram.caption, max_len=INSTAGRAM_CAPTION_MAX)
    if len(pc.instagram.hashtags) < 3:
        failures.append(
            f"instagram.hashtags has {len(pc.instagram.hashtags)} entries, expected ≥ 3"
        )

    _check_text("youtube.title", pc.youtube.title, max_len=YOUTUBE_TITLE_MAX)
    _check_text("youtube.description", pc.youtube.description, max_len=YOUTUBE_DESCRIPTION_MAX)
    if len(pc.youtube.tags) < 3:
        failures.append(f"youtube.tags has {len(pc.youtube.tags)} entries, expected ≥ 3")

    hooks = [pc.tiktok.hook.strip(), pc.instagram.hook.strip(), pc.youtube.title.strip()]
    non_empty_hooks = [h for h in hooks if h]
    if len(non_empty_hooks) >= 2 and len(set(non_empty_hooks)) < len(non_empty_hooks):
        failures.append(
            "at least two of (tiktok.hook, instagram.hook, youtube.title) are identical"
        )

    return failures


# ── audio_template ───────────────────────────────────────────────────────────


_AUDIO_TEMPLATE_REQUIRED_FIELDS = (
    "copy_tone",
    "caption_style",
    "creative_direction",
    "transition_style",
    "pacing_style",
)


def check_audio_template(output: AudioTemplateOutput) -> list[str]:
    """Structural floor for a music-track audio recipe.

    Mirrors `check_template_recipe` for the slot-arithmetic invariants and adds
    audio-specific assertions (beat list monotonic, style metadata non-empty).
    """
    failures: list[str] = []

    if output.shot_count != len(output.slots):
        failures.append(f"shot_count={output.shot_count} != len(slots)={len(output.slots)}")

    if output.total_duration_s > 0:
        slot_sum = sum(float(s.get("target_duration_s", 0.0) or 0.0) for s in output.slots)
        delta = abs(slot_sum - output.total_duration_s)
        if delta > 5.0:
            failures.append(
                f"slot durations sum to {slot_sum:.1f}s but total_duration_s="
                f"{output.total_duration_s:.1f}s (delta {delta:.1f}s > 5.0s tolerance)"
            )

    if output.hook_duration_s < 0:
        failures.append(f"hook_duration_s={output.hook_duration_s} must be >= 0")
    elif output.total_duration_s > 0 and output.hook_duration_s > output.total_duration_s:
        failures.append(
            f"hook_duration_s={output.hook_duration_s} exceeds "
            f"total_duration_s={output.total_duration_s}"
        )

    beats = output.beat_timestamps_s
    if beats:
        upper = output.total_duration_s + 0.5 if output.total_duration_s > 0 else None
        prev = -1.0
        for i, b in enumerate(beats):
            if b < 0:
                failures.append(f"beat {i}: timestamp {b} is negative")
            if upper is not None and b > upper:
                failures.append(f"beat {i}: timestamp {b} exceeds total_duration_s + 0.5")
            if b < prev:
                failures.append(f"beat {i}: timestamp {b} not sorted (prev={prev})")
                break
            prev = b

    for k, inter in enumerate(output.interstitials):
        try:
            after_slot = int(inter.get("after_slot", 0))
        except (TypeError, ValueError):
            failures.append(f"interstitial {k}: after_slot not integer")
            continue
        if after_slot < 1 or after_slot > output.shot_count:
            failures.append(
                f"interstitial {k}: after_slot={after_slot} outside [1, {output.shot_count}]"
            )

    for field_name in _AUDIO_TEMPLATE_REQUIRED_FIELDS:
        value = getattr(output, field_name, "")
        if not (value or "").strip():
            failures.append(f"{field_name}: empty (recipe quality bug — Gemini returned blank)")

    if output.color_grade not in _VALID_COLOR_HINTS:
        failures.append(f"color_grade={output.color_grade!r} not in {sorted(_VALID_COLOR_HINTS)}")

    return failures


# ── Dispatch ─────────────────────────────────────────────────────────────────


def run_structural(agent_name: str, output: Any, input: Any) -> list[str]:  # noqa: A002
    """Dispatch by agent name. Used by eval_runner."""
    if agent_name == "nova.compose.template_recipe":
        return check_template_recipe(output)
    if agent_name == "nova.video.clip_metadata":
        return check_clip_metadata(output, input)
    if agent_name == "nova.compose.creative_direction":
        return check_creative_direction(output)
    if agent_name == "nova.audio.transcript":
        return check_transcript(output, input)
    if agent_name == "nova.compose.platform_copy":
        return check_platform_copy(output)
    if agent_name == "nova.audio.template_recipe":
        return check_audio_template(output)
    raise ValueError(f"no structural checks registered for agent {agent_name!r}")
