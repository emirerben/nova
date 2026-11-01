"""Show a clip full screen when the creator mentions it, in a multi-clip Talking edit.

`phone_talking_cutaways` decides roles from each clip's pace of speech and spreads the
footage over the talk mechanically. A creator who asks "show the talks full screen when I
mention them" gets neither: the talks have clear speech, so they play back to back before the
creator's own take, and the footage that is left is spread without regard to what he says.
This module is the creator-ask half, pure (no I/O, no settings, no model):

1. `demote_speakers_to_cutaways` turns speaker clips into footage when the creator asked for
   full-screen display: a clip the ask NAMES, or (for an ask about no clip in particular) a
   clip whose own analysis says it is not the creator speaking to camera, as long as another
   clip is. Anything the evidence does not back keeps its pace-based role, with the reason.
2. `schedule_mention_cutaways` places each footage clip where the creator talks about what
   the clip shows: the clip's own words and facts (its transcript, summary, brands, props)
   against the creator's word timeline. A clip that no mention matches, or that finds no room
   near its mention, is spread by the mechanical scheduler and the row says so.

Facts only decide timing. They never become on-screen text (the Gemini-metadata fence).
"""

from __future__ import annotations

import math
import re
from collections import Counter
from collections.abc import Iterable, Sequence
from dataclasses import dataclass, replace
from typing import Any

from app.pipeline.phone_talking_cutaways import (
    HOOK_FREE_S,
    MAX_WINDOW_S,
    MIN_SLOT_S,
    MIN_WINDOW_S,
    TAIL_FREE_S,
    FootageCutawayWindow,
    TalkingClipRoles,
    _free_intervals,
    schedule_footage_cutaways,
)

# Why a speaker clip was turned into footage / kept as a speaker.
DEMOTED_NAMED = "named_by_ask"
DEMOTED_NOT_TO_CAMERA = "not_the_creator_to_camera"
KEPT_ORDER_ASKED = "clip_order_asked"
KEPT_NO_CREATOR_TAKE = "no_creator_take_left"

# How a footage clip's window was chosen.
BASIS_MENTION = "mention"
BASIS_SPREAD = "spread"
SPREAD_NO_MENTION = "no_mention_found"
SPREAD_NO_ROOM = "no_room_at_mention"

# Matched words this close together are one mention.
MENTION_WINDOW_S = 6.0
# A cutaway that cannot start where the mention starts may start this much later and still
# count as "at the mention".
MENTION_SLACK_S = 3.0
# A mention needs at least this many distinct matched words ...
MIN_MENTION_KEYS = 2
# ... and their rarity-weighted total (a word the creator says once and only one clip is
# about counts 1.0; a word said three times counts a third) must reach this.
MIN_MENTION_SCORE = 1.0
# A clip whose own sound plays may run longer than a silent cutaway (it is a talk).
SOUND_WINDOW_S = 6.0

_STEM = 5
_PARTS = re.compile(r"[a-z]+|\d+")
_ID_PARTS = re.compile(r"[^0-9A-Za-z]+")


@dataclass(frozen=True)
class ClipRecord:
    """What the clip's own analysis says; ``speaks_to_camera`` is None when it is unknown
    (the analysis failed or was degraded: a default False is not evidence)."""

    clip_id: str
    media_id: str
    speaks_to_camera: bool | None


@dataclass(frozen=True)
class FootageClip:
    clip_id: str
    footage_s: float
    keys: frozenset[str]
    max_window_s: float = MAX_WINDOW_S


@dataclass(frozen=True)
class CutawayAnchor:
    """Where one footage clip went and why (creator-safe: ids, seconds, a reason code)."""

    clip_id: str
    basis: str
    start_s: float
    end_s: float
    at_s: float | None = None
    reason: str | None = None


def match_keys(text: object) -> list[str]:
    """The comparable words of ``text``: folded, split at letter/digit seams ("Euro2032" ->
    euro, 2032), words under four letters and zero runs dropped, long words cut to a stem so
    Turkish endings ("tahsis", "tahsis edilmesinin") still meet."""
    from app.services.spoken_quotes import fold_tokens  # noqa: PLC0415

    keys: list[str] = []
    for token in fold_tokens(text):
        for part in _PARTS.findall(token):
            if part.isdigit():
                if len(part) >= 2 and set(part) != {"0"}:
                    keys.append(part)
            elif len(part) >= 4:
                keys.append(part[:_STEM])
    return keys


def match_media_id(ref: str, media_ids: Iterable[str]) -> str | None:
    """The clip ``ref`` (a brief scope id) names among ``media_ids``, else None.

    The brief writes a clip by the phone's file name ("134CD...B33B.mp4") where the edit
    carries the analysis-proxy name ("analysis-proxy-ios-134CD...B33B.mp4"), so an id that
    ends with the whole ref names it, as does a 6+ letter id part that exactly one clip has.
    """
    known = list(dict.fromkeys(media_ids))
    token = ref.strip().casefold()
    if not token:
        return None
    for media_id in known:
        if media_id.casefold() == token:
            return media_id
    tails = [m for m in known if len(token) >= 12 and m.casefold().endswith(token)]
    if len(tails) == 1:
        return tails[0]
    if len(token) < 6 or not token.isalnum():
        return None
    hits = [m for m in known if token in {p.casefold() for p in _ID_PARTS.split(m)}]
    return hits[0] if len(hits) == 1 else None


def clip_keys(texts: Iterable[object]) -> frozenset[str]:
    return frozenset(key for text in texts for key in match_keys(text))


def demote_speakers_to_cutaways(
    roles: TalkingClipRoles,
    records: Sequence[ClipRecord],
    *,
    named_media_ids: Iterable[str],
    global_ask: bool,
    protect_media_ids: Iterable[str] = (),
) -> tuple[TalkingClipRoles, list[dict[str, Any]], str | None]:
    """``roles`` with the speaker clips the creator's full-screen ask covers turned into
    footage. Returns ``(roles, demoted_rows, kept_reason)``; ``kept_reason`` says why a
    covered clip could not move (the creator's take must stay).

    A clip is covered when the ask names it (``named_media_ids``, canonical ids of this
    edit), or when the ask is about no clip in particular (``global_ask``) and the clip's
    analysis says it is NOT the creator speaking to camera. Unknown analysis never covers a
    clip. Clips in ``protect_media_ids`` (the creator's ending clip) are left to their own
    rule. The result must leave a speaker; a global ask also needs one the analysis says
    speaks to camera, else nothing moves.
    """
    by_id = {r.clip_id: r for r in records}
    named = set(named_media_ids)
    protected = set(protect_media_ids)
    reasons: dict[str, str] = {}
    for clip_id in roles.speakers:
        record = by_id.get(clip_id)
        if record is None or record.media_id in protected:
            continue
        if record.media_id in named:
            reasons[clip_id] = DEMOTED_NAMED
        elif global_ask and record.speaks_to_camera is False:
            reasons[clip_id] = DEMOTED_NOT_TO_CAMERA
    if not reasons:
        return roles, [], None
    remaining = [c for c in roles.speakers if c not in reasons]
    if not remaining:
        return roles, [], KEPT_NO_CREATOR_TAKE
    if all(r == DEMOTED_NOT_TO_CAMERA for r in reasons.values()) and not any(
        by_id.get(c) is not None and by_id[c].speaks_to_camera is True for c in remaining
    ):
        return roles, [], KEPT_NO_CREATOR_TAKE
    order = [row["clip_id"] for row in roles.receipt]
    footage_ids = set(roles.footage) | set(reasons)
    footage = tuple(c for c in order if c in footage_ids)
    receipt = tuple(
        {**row, "role": "footage", "was": "speaker", "reason": reasons[row["clip_id"]]}
        if row["clip_id"] in reasons
        else row
        for row in roles.receipt
    )
    rows = [
        {"clip_id": c, "media_id": by_id[c].media_id, "reason": reasons[c]}
        for c in order
        if c in reasons
    ]
    return (
        replace(roles, speakers=tuple(remaining), footage=footage, receipt=receipt),
        rows,
        None,
    )


@dataclass(frozen=True)
class _Token:
    key: str
    start_s: float


def _timeline_tokens(words: Iterable[Any]) -> list[_Token]:
    tokens: list[_Token] = []
    for word in words:
        start = float(getattr(word, "start_s", 0.0))
        tokens.extend(_Token(key, start) for key in match_keys(getattr(word, "text", "")))
    return sorted(tokens, key=lambda t: t.start_s)


def _best_mention(
    clip: FootageClip, tokens: Sequence[_Token], weight: dict[str, float]
) -> tuple[float, float] | None:
    """``(score, first_matched_word_start_s)`` of the best mention window of ``clip``, else
    None when no window has MIN_MENTION_KEYS distinct matched words reaching MIN_MENTION_SCORE.
    Earlier windows win ties."""
    best: tuple[float, float] | None = None
    for begin, token in enumerate(tokens):
        if token.key not in clip.keys:
            continue
        seen: dict[str, float] = {}
        for other in tokens[begin:]:
            if other.start_s - token.start_s > MENTION_WINDOW_S:
                break
            if other.key in clip.keys:
                seen.setdefault(other.key, other.start_s)
        score = sum(weight[key] for key in seen)
        if len(seen) >= MIN_MENTION_KEYS and score >= MIN_MENTION_SCORE:
            if best is None or score > best[0] + 1e-9:
                best = (score, token.start_s)
    return best


def _fit_at(
    start_s: float, length_s: float, free: Sequence[tuple[float, float]]
) -> tuple[float, float] | None:
    """The part of ``[start, start + length]`` that fits the first free interval ending after
    ``start`` by at least MIN_SLOT_S (or the whole of a shorter footage), moved no later than
    MENTION_SLACK_S; None when nothing fits."""
    minimum = min(MIN_SLOT_S, length_s)
    for f_start, f_end in free:
        if f_end <= start_s + minimum - 1e-9:
            continue
        begin = max(start_s, f_start)
        if begin - start_s > MENTION_SLACK_S:
            return None
        end = min(begin + length_s, f_end)
        if end - begin >= minimum - 1e-9:
            return begin, end
    return None


def schedule_mention_cutaways(
    clips: Sequence[FootageClip],
    *,
    words: Sequence[Any],
    duration_s: float,
    blocked: Iterable[tuple[float, float]] = (),
    word_starts: Sequence[float] = (),
) -> tuple[list[FootageCutawayWindow], list[dict], list[CutawayAnchor]]:
    """Place each footage clip where the creator mentions what it shows, else spread it.

    Returns ``(windows, unplaced, anchors)``: ``windows`` and ``unplaced`` have the shape
    `schedule_footage_cutaways` returns; ``anchors`` has one row per placed clip saying
    whether its window sits at a mention or was spread, and why.
    """
    tokens = _timeline_tokens(words)
    creator_count = Counter(t.key for t in tokens)
    clip_count = Counter(key for clip in clips for key in clip.keys)
    weight = {
        key: 1.0 / (creator_count[key] * clip_count[key])
        for key in creator_count
        if clip_count[key] > 0
    }

    duration = max(float(duration_s), 0.0)
    lo = min(HOOK_FREE_S, duration)
    hi = max(duration - TAIL_FREE_S, lo)
    taken: list[tuple[float, float]] = [(float(a), float(b)) for a, b in blocked]

    scored: list[tuple[float, float, FootageClip]] = []
    spread: dict[str, str] = {}
    for clip in clips:
        if float(clip.footage_s) < MIN_WINDOW_S:
            continue
        best = _best_mention(clip, tokens, weight)
        if best is None:
            spread[clip.clip_id] = SPREAD_NO_MENTION
        else:
            scored.append((best[0], best[1], clip))

    mentions = sorted(at_s for _score, at_s, _clip in scored)
    placed: list[FootageCutawayWindow] = []
    at_mention: dict[str, float] = {}
    for _score, at_s, clip in sorted(scored, key=lambda row: (-row[0], row[1])):
        length = min(clip.max_window_s, float(clip.footage_s))
        # A window stops where the next clip's mention starts, when that leaves it a slot.
        later = next((m for m in mentions if m > at_s + 1e-9), None)
        if later is not None and later - max(at_s, lo) >= MIN_SLOT_S:
            length = min(length, later - max(at_s, lo))
        free = _free_intervals(lo, hi, taken)
        fit = _fit_at(max(at_s, lo), length, free)
        if fit is None:
            spread[clip.clip_id] = SPREAD_NO_ROOM
            continue
        begin = math.ceil(fit[0] * 1000 - 1e-6) / 1000
        end = math.floor(fit[1] * 1000 + 1e-6) / 1000
        if end - begin < min(MIN_WINDOW_S, length):
            spread[clip.clip_id] = SPREAD_NO_ROOM
            continue
        placed.append(FootageCutawayWindow(clip.clip_id, begin, end))
        taken.append((begin, end))
        at_mention[clip.clip_id] = at_s

    rest = [(c.clip_id, float(c.footage_s)) for c in clips if c.clip_id not in at_mention]
    spread_windows, unplaced = schedule_footage_cutaways(
        rest, duration_s=duration, word_starts=word_starts, blocked=taken
    )
    anchors = [
        CutawayAnchor(
            w.clip_id, BASIS_MENTION, w.start_s, w.end_s, at_s=round(at_mention[w.clip_id], 2)
        )
        for w in placed
    ] + [
        CutawayAnchor(w.clip_id, BASIS_SPREAD, w.start_s, w.end_s, reason=spread.get(w.clip_id))
        for w in spread_windows
    ]
    windows = sorted([*placed, *spread_windows], key=lambda w: w.start_s)
    anchors.sort(key=lambda a: a.start_s)
    return windows, unplaced, anchors
