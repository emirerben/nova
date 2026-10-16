"""KRI-719: multi-clip Talking to camera on the phone.

A declared Talking-to-camera (`subtitled`) item may carry several clips. Two
pure decisions live here, with no I/O so the worker and tests share them:

1. `classify_talking_clips` -- which clips are SPEAKERS (clear speech: they
   play back to back on the main track, each with its own audio and captions)
   and which are FOOTAGE (shown on top as muted full-frame cutaways). Decided
   from each clip's own Whisper transcript: a clip is a speaker when it holds
   at least `MIN_SPEAKER_WORDS` words at `MIN_SPEAKER_WORDS_PER_S` or more. A
   clip whose audio is silent/inaudible is never a speaker. When no clip
   qualifies, the clip with the most words speaks (the worker's silent-take
   check then refuses a truly silent one) so an edit always has a voice.

2. `schedule_footage_cutaways` -- when each footage clip covers the speakers.
   The same rules as the KRI-297 full-screen Visuals sequence
   (`phone_overlay_grounding._ground_fullscreen_sequence`): the first 2.5 s
   (the hook and title) and the last 1 s stay on the speaker, each cutaway is
   1.5-4 s and never longer than its own footage, starts snap to a spoken word
   when one is near, upload order is kept, and two cutaways never overlap.
   ``blocked`` windows (full-screen Visuals already placed) are left free, so
   footage and Visuals share one sequence. A clip that does not fit is
   reported, never dropped silently.
"""

from __future__ import annotations

import math
from collections.abc import Iterable, Sequence
from dataclasses import dataclass

from app.services.overlay_autoplace import _snap_to_word_start

# A speaker clip: enough words, said at a talking pace. Ambient b-roll that
# Whisper hears a stray "Thank you." in stays footage.
MIN_SPEAKER_WORDS = 6
MIN_SPEAKER_WORDS_PER_S = 0.8

# Mirrors `phone_overlay_grounding._FS_*` (KRI-297) so footage cutaways and
# full-screen Visuals feel the same.
HOOK_FREE_S = 2.5
TAIL_FREE_S = 1.0
MIN_SLOT_S = 1.5
MAX_WINDOW_S = 4.0
# Mirrors `phone_subtitled_plan._MIN_CUTAWAY_S`: shorter reads as a flicker.
MIN_WINDOW_S = 0.5

UNPLACED_NO_ROOM = "no_room_in_timeline"
UNPLACED_TOO_SHORT = "clip_too_short"


@dataclass(frozen=True)
class TalkingClipEvidence:
    """What the worker measured for one clip, in upload order."""

    clip_id: str
    duration_s: float
    word_count: int
    audible: bool


@dataclass(frozen=True)
class TalkingClipRoles:
    speakers: tuple[str, ...]
    footage: tuple[str, ...]
    # One creator-safe row per clip: id, role, word count, words per second.
    receipt: tuple[dict, ...]


@dataclass(frozen=True)
class FootageCutawayWindow:
    clip_id: str
    start_s: float
    end_s: float


def _words_per_s(evidence: TalkingClipEvidence) -> float:
    if evidence.duration_s <= 0:
        return 0.0
    return evidence.word_count / evidence.duration_s


def _is_speaker(evidence: TalkingClipEvidence) -> bool:
    return (
        evidence.audible
        and evidence.word_count >= MIN_SPEAKER_WORDS
        and _words_per_s(evidence) >= MIN_SPEAKER_WORDS_PER_S
    )


def classify_talking_clips(clips: Sequence[TalkingClipEvidence]) -> TalkingClipRoles:
    """Split ``clips`` (upload order) into speakers and footage, keeping order.

    Raises ``ValueError`` for an empty list (the caller always has a clip).
    """
    if not clips:
        raise ValueError("a Talking edit needs at least one clip")
    speaker_ids = [c.clip_id for c in clips if _is_speaker(c)]
    fallback = False
    if not speaker_ids:
        # No clear speaker: the wordiest clip speaks (first one on a tie).
        best = max(clips, key=lambda c: (c.word_count, -clips.index(c)))
        speaker_ids = [best.clip_id]
        fallback = True
    speakers = tuple(speaker_ids)
    footage = tuple(c.clip_id for c in clips if c.clip_id not in speakers)
    receipt = tuple(
        {
            "clip_id": c.clip_id,
            "role": "speaker" if c.clip_id in speakers else "footage",
            "word_count": c.word_count,
            "words_per_s": round(_words_per_s(c), 2),
            "audible": c.audible,
            **({"fallback": True} if fallback and c.clip_id in speakers else {}),
        }
        for c in clips
    )
    return TalkingClipRoles(speakers=speakers, footage=footage, receipt=receipt)


def _free_intervals(
    lo: float, hi: float, blocked: Iterable[tuple[float, float]]
) -> list[tuple[float, float]]:
    """``[lo, hi]`` minus every ``blocked`` window, in time order."""
    free = [(lo, hi)] if hi > lo else []
    for b_start, b_end in sorted((float(a), float(b)) for a, b in blocked):
        if b_end <= b_start:
            continue
        next_free: list[tuple[float, float]] = []
        for f_start, f_end in free:
            if b_end <= f_start or b_start >= f_end:
                next_free.append((f_start, f_end))
                continue
            if b_start > f_start:
                next_free.append((f_start, b_start))
            if b_end < f_end:
                next_free.append((b_end, f_end))
        free = next_free
    return [(a, b) for a, b in free if b - a > 1e-6]


def schedule_footage_cutaways(
    clips: Sequence[tuple[str, float]],
    *,
    duration_s: float,
    word_starts: Sequence[float] = (),
    blocked: Iterable[tuple[float, float]] = (),
) -> tuple[list[FootageCutawayWindow], list[dict]]:
    """Place each footage clip ``(clip_id, footage_duration_s)`` once over a
    ``duration_s`` speaker timeline. Returns ``(windows, unplaced)``; every
    clip is in exactly one of them (``unplaced`` rows carry ``clip_id`` and a
    ``reason``)."""
    unplaced: list[dict] = []
    usable: list[tuple[str, float]] = []
    for clip_id, footage_s in clips:
        if float(footage_s) < MIN_WINDOW_S:
            unplaced.append({"clip_id": clip_id, "reason": UNPLACED_TOO_SHORT})
        else:
            usable.append((clip_id, float(footage_s)))

    duration = max(float(duration_s), 0.0)
    lo = min(HOOK_FREE_S, duration)
    hi = max(duration - TAIL_FREE_S, lo)
    intervals = _free_intervals(lo, hi, blocked)
    capacities = [int((end - start) // MIN_SLOT_S + 1e-9) for start, end in intervals]
    total_capacity = sum(capacities)
    fitted = usable[:total_capacity]
    for clip_id, _ in usable[total_capacity:]:
        unplaced.append({"clip_id": clip_id, "reason": UNPLACED_NO_ROOM})
    if not fitted:
        return [], unplaced

    # Share the clips out over the free intervals in proportion to their room
    # (largest remainder), keeping upload order across the timeline.
    shares = [len(fitted) * cap / total_capacity for cap in capacities]
    counts = [min(int(share), cap) for share, cap in zip(shares, capacities, strict=True)]
    remaining = len(fitted) - sum(counts)
    order = sorted(range(len(intervals)), key=lambda i: (counts[i] - shares[i], i))
    while remaining > 0:
        for i in order:
            if remaining == 0:
                break
            if counts[i] < capacities[i]:
                counts[i] += 1
                remaining -= 1

    words = [{"start_s": float(ws)} for ws in word_starts]
    windows: list[FootageCutawayWindow] = []
    cursor = 0
    for (start, end), count in zip(intervals, counts, strict=True):
        if count <= 0:
            continue
        slot_len = (end - start) / count
        for slot in range(count):
            clip_id, footage_s = fitted[cursor]
            cursor += 1
            slot_start = start + slot * slot_len
            slot_end = slot_start + slot_len
            window = min(MAX_WINDOW_S, footage_s, slot_len)
            nominal = slot_start + (slot_len - window) / 2
            snapped = _snap_to_word_start(nominal, words) if words else nominal
            snapped = min(max(snapped, slot_start), slot_end - window)
            # Millisecond grid, rounded inward so a window never leaves its own
            # slot (the compiler rejects even a 1 ms overlap).
            windows.append(
                FootageCutawayWindow(
                    clip_id=clip_id,
                    start_s=math.ceil(snapped * 1000 - 1e-6) / 1000,
                    end_s=math.floor((snapped + window) * 1000 + 1e-6) / 1000,
                )
            )
    return windows, unplaced
