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

from app.agents._schemas.music_labels import CURRENT_LABEL_VERSION
from app.agents.audio_template import AudioTemplateOutput
from app.agents.clip_metadata import (
    _BALL_BLACKLIST,
    _BALL_WHITELIST,
    ClipMetadataInput,
    ClipMetadataOutput,
)
from app.agents.clip_router import ClipRouterInput, ClipRouterOutput
from app.agents.creative_direction import CreativeDirectionOutput
from app.agents.music_matcher import MusicMatcherInput, MusicMatcherOutput
from app.agents.platform_copy import PlatformCopyOutput
from app.agents.shot_ranker import ShotRankerInput, ShotRankerOutput
from app.agents.song_classifier import SongClassifierOutput
from app.agents.template_recipe import (
    _VALID_COLOR_HINTS,
    _VALID_INTERSTITIAL_TYPES,
    _VALID_OVERLAY_ROLES,
    _VALID_TRANSITION_TYPES,
    TemplateRecipeOutput,
)
from app.agents.text_designer import (
    _VALID_EFFECTS as _TEXT_DESIGNER_VALID_EFFECTS,
)
from app.agents.text_designer import (
    _VALID_FONT_STYLES,
    _VALID_TEXT_SIZES,
    TextDesignerInput,
    TextDesignerOutput,
)
from app.agents.transcript import TranscriptOutput
from app.agents.transition_picker import (
    _VALID_TRANSITIONS as _PICKER_VALID_TRANSITIONS,
)
from app.agents.transition_picker import (
    TransitionPickerInput,
    TransitionPickerOutput,
)
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

            bbox = ov.get("text_bbox")
            if bbox is not None:
                if not isinstance(bbox, dict):
                    failures.append(
                        f"slot {i} overlay {j}: text_bbox is not a dict (got {type(bbox).__name__})"
                    )
                else:
                    try:
                        bx = float(bbox.get("x_norm"))
                        by = float(bbox.get("y_norm"))
                        bw = float(bbox.get("w_norm"))
                        bh = float(bbox.get("h_norm"))
                        bt = float(bbox.get("sample_frame_t"))
                    except (TypeError, ValueError):
                        failures.append(f"slot {i} overlay {j}: text_bbox has non-numeric field")
                    else:
                        if not (0.0 <= bx <= 1.0 and 0.0 <= by <= 1.0):
                            failures.append(
                                f"slot {i} overlay {j}: text_bbox center ({bx},{by}) outside [0,1]"
                            )
                        if not (0.0 < bw <= 1.0 and 0.0 < bh <= 1.0):
                            failures.append(
                                f"slot {i} overlay {j}: text_bbox dims ({bw},{bh}) outside (0,1]"
                            )
                        if (bx - bw / 2.0) < 0.0 or (bx + bw / 2.0) > 1.0:
                            failures.append(
                                f"slot {i} overlay {j}: text_bbox horizontal extent "
                                f"out of frame (x={bx}, w={bw})"
                            )
                        if (by - bh / 2.0) < 0.0 or (by + bh / 2.0) > 1.0:
                            failures.append(
                                f"slot {i} overlay {j}: text_bbox vertical extent "
                                f"out of frame (y={by}, h={bh})"
                            )
                        if bt < start or bt > end:
                            failures.append(
                                f"slot {i} overlay {j}: text_bbox sample_frame_t={bt} "
                                f"outside overlay window [{start},{end}]"
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


# ── clip_router ──────────────────────────────────────────────────────────────


# Lines shorter than this in a rationale almost always indicate boilerplate
# ("good fit", "best clip"). The 10-char floor catches the trivial cases
# without flagging legitimate terse rationales like "hook_score 9 wins".
_RATIONALE_MIN_CHARS = 10
_BOILERPLATE_RATIONALES = {
    "best clip",
    "best fit",
    "good fit",
    "good match",
    "matches well",
    "best choice",
    "looks good",
    "fits well",
}


def check_clip_router(output: ClipRouterOutput, input: ClipRouterInput) -> list[str]:  # noqa: A002
    """Structural floor for slot assignment.

    `parse()` already enforces that every slot has exactly one assignment and
    that every referenced `candidate_id` is in the input. We check the two
    things parse can't catch:

      - **Duplicate candidate usage.** Same candidate assigned to multiple
        slots — silently allowed by the parser today, but it defeats the
        variety constraint. Catch here so the eval fails loudly.
      - **Boilerplate rationales.** "best fit" / "good match" / empty is a
        signal that the model is autopiloting through the assignment without
        reasoning about it. Forces the rubric's `rationale_quality` dimension
        to have a structural floor to stand on.
    """
    failures: list[str] = []

    valid_slots = {s.position for s in input.slots}
    valid_ids = {c.id for c in input.candidates}

    assigned_slots = {a.slot_position for a in output.assignments}
    if assigned_slots != valid_slots:
        missing = sorted(valid_slots - assigned_slots)
        extra = sorted(assigned_slots - valid_slots)
        if missing:
            failures.append(f"missing assignments for slots {missing}")
        if extra:
            failures.append(f"assignments reference unknown slots {extra}")

    used_ids: list[str] = []
    for a in output.assignments:
        if a.candidate_id not in valid_ids:
            failures.append(
                f"slot {a.slot_position}: candidate_id {a.candidate_id!r} not in candidate set"
            )
            continue
        used_ids.append(a.candidate_id)

    duplicates = {cid for cid in used_ids if used_ids.count(cid) > 1}
    if duplicates:
        failures.append(
            f"candidate(s) {sorted(duplicates)} assigned to multiple slots "
            "(variety constraint violated)"
        )

    for a in output.assignments:
        r = (a.rationale or "").strip().lower()
        if not r:
            failures.append(f"slot {a.slot_position}: rationale is empty")
            continue
        if len(r) < _RATIONALE_MIN_CHARS:
            failures.append(
                f"slot {a.slot_position}: rationale {r!r} is too short "
                f"(<{_RATIONALE_MIN_CHARS} chars — likely boilerplate)"
            )
            continue
        if r in _BOILERPLATE_RATIONALES:
            failures.append(
                f"slot {a.slot_position}: rationale {r!r} is boilerplate (needs concrete reason)"
            )

    return failures


# ── shot_ranker ──────────────────────────────────────────────────────────────


def check_shot_ranker(output: ShotRankerOutput, input: ShotRankerInput) -> list[str]:  # noqa: A002
    """Structural floor for top-K moment ranking.

    `parse()` re-numbers ranks 1..N and drops hallucinated IDs. We add:

      - **No duplicate ranks** — parse re-numbers post-hoc, but the model
        emitting duplicates is a signal the prompt isn't anchoring rank
        semantics. Catch the raw output's intent before it gets normalized.
        (parse() sorts and renumbers — by the time we see `output.ranked`
        ranks ARE 1..N, but we can still check for missing IDs and short
        rationales.)
      - **No duplicate IDs.** Same moment ranked twice — silently passable
        through parse, but breaks the "top-K distinct moments" contract.
      - **Ranks dense from 1.** No gaps. After parse() this should always
        hold; the assertion canaries any future parse() change.
      - **Boilerplate rationales** — same logic as clip_router.
      - **target_count adherence** — the agent SHOULD return exactly
        target_count entries (or fewer if it judged the candidate pool weak).
        Returning MORE than target_count is a contract violation.
    """
    failures: list[str] = []

    valid_ids = {c.id for c in input.candidates}

    if len(output.ranked) > input.target_count:
        failures.append(
            f"ranked has {len(output.ranked)} entries > target_count={input.target_count}"
        )

    seen_ids: list[str] = []
    for m in output.ranked:
        if m.id not in valid_ids:
            failures.append(f"rank {m.rank}: id {m.id!r} not in candidate set")
            continue
        seen_ids.append(m.id)

    duplicates = {mid for mid in seen_ids if seen_ids.count(mid) > 1}
    if duplicates:
        failures.append(f"id(s) {sorted(duplicates)} ranked more than once")

    ranks = [m.rank for m in output.ranked]
    if ranks and sorted(ranks) != list(range(1, len(ranks) + 1)):
        failures.append(f"ranks not dense from 1: got {sorted(ranks)}")

    for m in output.ranked:
        r = (m.rationale or "").strip().lower()
        if not r:
            failures.append(f"rank {m.rank}: rationale is empty")
            continue
        if len(r) < _RATIONALE_MIN_CHARS:
            failures.append(
                f"rank {m.rank}: rationale {r!r} is too short "
                f"(<{_RATIONALE_MIN_CHARS} chars — likely boilerplate)"
            )
            continue
        if r in _BOILERPLATE_RATIONALES:
            failures.append(
                f"rank {m.rank}: rationale {r!r} is boilerplate (needs concrete reason)"
            )

    return failures


# ── text_designer ────────────────────────────────────────────────────────────


# Text-size ordering used to assert hierarchy by placeholder kind. A `subject`
# placeholder must never be 'small' or 'medium' (it's the visual anchor of the
# slot — that's the agent's stated job). A `prefix` must never be 'xxlarge'
# (prefix is the quiet lead-in to the subject — outranking the subject in size
# inverts the read).
_TEXT_SIZE_RANK = {size: i for i, size in enumerate(_VALID_TEXT_SIZES)}


def check_text_designer(
    output: TextDesignerOutput,
    input: TextDesignerInput,  # noqa: A002
) -> list[str]:
    """Structural floor for per-slot typographic decisions.

    `parse()` coerces invalid enum values to defaults, which means a structural
    floor can't rely on "invalid value → fail". Instead we catch *intent-level*
    drift that coercion hides:

      - **Hierarchy inversion.** subject placeholder coming back at 'small' /
        'medium', or prefix coming back at 'xxlarge'. Coercion can't repair
        this — the model chose the wrong size band on purpose.
      - **accel_at_s + effect mismatch.** accel_at_s is only meaningful when
        effect == 'font-cycle' (renderer ignores it otherwise). Setting it
        with another effect signals confused output.
      - **start_s in the legal envelope.** A negative start_s would be coerced
        to 0.0 by parse(); but a start_s past 10s on a 3s slot is silently
        accepted. We can't see slot_duration from the agent's perspective, but
        we can flag values that clearly imply a misread of the slot timing.
      - **text_color shape.** parse() already coerces to '#FFFFFF' on a bad
        hex; we re-assert so future parse() changes don't silently break the
        contract.
    """
    failures: list[str] = []

    if output.text_size not in _VALID_TEXT_SIZES:
        failures.append(
            f"text_size={output.text_size!r} not in {list(_VALID_TEXT_SIZES)} "
            "(parse() should have coerced — canary for parser regression)"
        )

    if output.font_style not in _VALID_FONT_STYLES:
        failures.append(f"font_style={output.font_style!r} not in {list(_VALID_FONT_STYLES)}")

    if output.effect not in _TEXT_DESIGNER_VALID_EFFECTS:
        failures.append(f"effect={output.effect!r} not in {list(_TEXT_DESIGNER_VALID_EFFECTS)}")

    color = output.text_color or ""
    if not (color.startswith("#") and len(color) in (4, 7)):
        failures.append(f"text_color={color!r} not a valid hex code (#RGB or #RRGGBB)")

    # Hierarchy by placeholder_kind.
    if input.placeholder_kind == "subject":
        rank = _TEXT_SIZE_RANK.get(output.text_size, -1)
        if rank >= 0 and rank < _TEXT_SIZE_RANK["large"]:
            failures.append(
                f"subject placeholder got text_size={output.text_size!r} "
                "(must be at least 'large' — subject is the visual anchor of the slot)"
            )
    elif input.placeholder_kind == "prefix":
        rank = _TEXT_SIZE_RANK.get(output.text_size, -1)
        if rank >= _TEXT_SIZE_RANK["large"]:
            failures.append(
                f"prefix placeholder got text_size={output.text_size!r} "
                "(must be smaller than 'large' — prefix is the quiet lead-in to the subject)"
            )

    # accel_at_s is meaningful only with effect='font-cycle'.
    if output.accel_at_s is not None and output.effect != "font-cycle":
        failures.append(
            f"accel_at_s={output.accel_at_s} set with effect={output.effect!r} "
            "(renderer only honors accel_at_s for font-cycle; signals confused output)"
        )
    # Inverse canary: font-cycle on a hook subject slot SHOULD have accel_at_s.
    # We only fire this when the agent's own calibration pattern explicitly
    # documents it (subject + slot 1 + font-cycle).
    if (
        input.placeholder_kind == "subject"
        and input.slot_position == 1
        and output.effect == "font-cycle"
        and output.accel_at_s is None
    ):
        failures.append(
            "subject on slot 1 with font-cycle effect has accel_at_s=None "
            "(calibration pattern requires a beat-aligned lock-in time)"
        )

    if output.start_s < 0:
        failures.append(f"start_s={output.start_s} is negative")
    # Hard upper sanity bound — slots in prod rarely exceed 15s; start_s past
    # that strongly implies the agent confused absolute clip time with slot-
    # relative time. Not a perfect check, but a useful canary.
    if output.start_s > 15.0:
        failures.append(
            f"start_s={output.start_s} > 15.0 — likely confused with absolute "
            "clip time; start_s is relative to slot start"
        )

    return failures


# ── transition_picker ────────────────────────────────────────────────────────

# Canonical duration ranges per transition. These mirror the duration_envelope
# table in the agent's prompt. Used to flag picks whose duration is clearly
# outside the band for the chosen transition (a hard-cut with duration=0.8 is
# a contract violation; a dissolve at 0.1s reads as a glitch).
_TRANSITION_DURATION_RANGES: dict[str, tuple[float, float]] = {
    "hard-cut": (0.0, 0.0),
    "match-cut": (0.0, 0.0),  # renders identically to hard-cut; instant cut
    "speed-ramp": (0.0, 0.0),  # cut is instant; mechanic is on dest slot's speed_factor
    "none": (0.0, 0.0),
    "whip-pan": (0.20, 0.40),
    "zoom-in": (0.30, 0.50),
    "dissolve": (0.40, 0.80),
    "curtain-close": (0.60, 1.00),
}
# Tolerance band around the canonical envelope — slight drift (e.g. dissolve at
# 0.35s or whip-pan at 0.45s) is fine; the structural check only fires on clear
# violations (hard-cut at 0.5s, dissolve at 0.1s).
_DURATION_TOLERANCE_S = 0.15


def check_transition_picker(
    output: TransitionPickerOutput,
    input: TransitionPickerInput,  # noqa: A002, ARG001
) -> list[str]:
    """Structural floor for the per-pair transition pick.

    `parse()` rejects unknown transition values with a SchemaError (good), but
    clamps duration_s to [0.0, 2.0] silently. The interesting failure modes are
    semantic, not parse-time:

      - **Duration outside the canonical envelope** for the picked transition.
        A hard-cut with duration=0.8 means the agent missed the "instant"
        contract; a dissolve at 0.1s reads as a glitch. Both pass parse but
        signal drift.
      - **Empty / too-short rationale.** Like clip_router / shot_ranker, the
        rubric's `default_fidelity` and `pacing_style_modulation` dimensions
        need an auditable reason to score against.
      - **Sanity bound on whip-pan with static cameras.** The agent's own
        prompt explicitly forbids whip-pan between two static shots ("reads
        as a glitch, not a transition"). If both clips report
        camera_movement='static' and the pick is whip-pan, flag it.
    """
    failures: list[str] = []

    if output.transition not in _PICKER_VALID_TRANSITIONS:
        failures.append(
            f"transition={output.transition!r} not in {list(_PICKER_VALID_TRANSITIONS)}"
        )
        return failures  # downstream range check is meaningless without a valid type

    lo, hi = _TRANSITION_DURATION_RANGES[output.transition]
    if not (lo - _DURATION_TOLERANCE_S <= output.duration_s <= hi + _DURATION_TOLERANCE_S):
        failures.append(
            f"duration_s={output.duration_s} outside canonical envelope "
            f"[{lo}, {hi}] (±{_DURATION_TOLERANCE_S} tolerance) for transition "
            f"{output.transition!r}"
        )

    rationale = (output.rationale or "").strip().lower()
    if not rationale:
        failures.append("rationale is empty")
    elif len(rationale) < _RATIONALE_MIN_CHARS:
        failures.append(
            f"rationale {rationale!r} is too short "
            f"(<{_RATIONALE_MIN_CHARS} chars — likely boilerplate)"
        )
    elif rationale in _BOILERPLATE_RATIONALES:
        failures.append(f"rationale {rationale!r} is boilerplate (needs concrete reason)")

    # Camera-state sanity: whip-pan between two static shots is forbidden by
    # the prompt itself.
    if (
        output.transition == "whip-pan"
        and input.outgoing.camera_movement == "static"
        and input.incoming.camera_movement == "static"
    ):
        failures.append(
            "whip-pan picked for static→static pair (prompt forbids — reads as a glitch)"
        )

    return failures


# ── Dispatch ─────────────────────────────────────────────────────────────────


def check_song_classifier(output: SongClassifierOutput) -> list[str]:
    """Structural floor for nova.audio.song_classifier.

    Pydantic already enforces the categorical enums and the vibe_tags length
    bounds. This layer asserts the cross-field invariants that Pydantic can't
    express on its own.
    """
    failures: list[str] = []
    labels = output.labels

    if labels.label_version != CURRENT_LABEL_VERSION:
        failures.append(
            f"label_version={labels.label_version!r} != CURRENT_LABEL_VERSION "
            f"({CURRENT_LABEL_VERSION!r}) — Phase 1 forces equality in parse()"
        )

    # vibe_tags: dedup, lowercase, non-empty after normalization. Pydantic
    # bounds the length 1-8 but does not enforce shape.
    seen: set[str] = set()
    for i, tag in enumerate(labels.vibe_tags):
        if not isinstance(tag, str) or not tag.strip():
            failures.append(f"vibe_tags[{i}]: empty or non-string")
            continue
        if tag != tag.lower():
            failures.append(f"vibe_tags[{i}]={tag!r}: not lowercase (parse() normalizes)")
        if tag in seen:
            failures.append(f"vibe_tags[{i}]={tag!r}: duplicate (parse() dedupes)")
        seen.add(tag)

    if not labels.mood.strip():
        failures.append("mood: empty after strip")
    if not labels.ideal_content_profile.strip():
        failures.append("ideal_content_profile: empty after strip")
    if not output.rationale.strip():
        failures.append("rationale: empty after strip")

    return failures


def check_music_matcher(output: MusicMatcherOutput, input: MusicMatcherInput) -> list[str]:  # noqa: A002
    """Structural floor for nova.audio.music_matcher.

    Pydantic enforces score bounds [0, 10] and per-entry required fields. This
    layer asserts the cross-field invariants ``parse()`` is supposed to uphold:
    every ``track_id`` resolves against ``available_tracks``, no duplicates,
    rationale is non-empty after strip, scores are monotonically non-increasing
    (matcher contract: ranked highest to lowest).
    """
    failures: list[str] = []
    valid_ids = {t.track_id for t in input.available_tracks}

    seen: set[str] = set()
    last_score: float | None = None
    for i, entry in enumerate(output.ranked):
        if entry.track_id not in valid_ids:
            failures.append(
                f"ranked[{i}].track_id={entry.track_id!r}: not in available_tracks "
                "(parse() should have dropped this)"
            )
        if entry.track_id in seen:
            failures.append(f"ranked[{i}].track_id={entry.track_id!r}: duplicate")
        seen.add(entry.track_id)

        if not entry.rationale.strip():
            failures.append(f"ranked[{i}]: rationale empty after strip")

        if entry.score < 0.0 or entry.score > 10.0:
            failures.append(f"ranked[{i}]: score={entry.score} outside [0, 10]")

        if last_score is not None and entry.score - last_score > 0.01:
            failures.append(
                f"ranked[{i}]: score={entry.score:.2f} > ranked[{i - 1}].score="
                f"{last_score:.2f} — ranking should be non-increasing"
            )
        last_score = entry.score

    return failures


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
    if agent_name == "nova.audio.song_classifier":
        return check_song_classifier(output)
    if agent_name == "nova.audio.music_matcher":
        return check_music_matcher(output, input)
    if agent_name == "nova.video.clip_router":
        return check_clip_router(output, input)
    if agent_name == "nova.video.shot_ranker":
        return check_shot_ranker(output, input)
    if agent_name == "nova.layout.text_designer":
        return check_text_designer(output, input)
    if agent_name == "nova.layout.transition_picker":
        return check_transition_picker(output, input)
    raise ValueError(f"no structural checks registered for agent {agent_name!r}")
