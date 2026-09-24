"""phone_reaction_grounding -- creator-authored reaction beats -> phone
Talking-lane cards + sound effects, grounded on the raw Whisper transcript
(KRI-178).

A creator can now author "reaction beats" (KRI-178 strategy schema,
`app.agents._schemas.reaction_beats`): "when I say X, show sticker/photo Y and
play sound Z" plus an optional "closing media" held until the end of the
clip. This module is the grounding step that turns those creator intents plus
the clip's raw Whisper words into ready-to-bind `SubtitledOverlayCard` /
`SubtitledSoundEffect` lanes -- the phrase-matching sibling of KRI-176's
`app.services.phone_overlay_grounding` (which matches a Visuals pool against
loose transcript *meaning* rather than an exact authored phrase).

It reuses two things straight from KRI-176 rather than duplicating them:

- `phone_overlay_grounding._load_ready_pool_assets` / `_label_for_asset` --
  same ready-pool seam, same creator-safe labeling.
- `phone_overlay_grounding.resolve_phone_card_geometry` -- the same
  face-aware, caption-safe geometry arbitration, so a reaction sticker and a
  KRI-176 auto-placed photo never fight the corner differently.

Every beat ends up in exactly one of the receipt's `placed`/`unplaced` lists
(one `unplaced` entry per beat even under `occurrence: "every"`; a `placed`
entry per successfully placed occurrence). The receipt is creator-safe: never
a `gcs_path`, a generation, or agent/prompt text -- only labels, timings, and
short plain-text reasons. Every stage fails open: this function itself never
raises for anything other than a genuine caller-level error (a broken DB
session) -- a single bad beat is reported unplaced with reason `"error"`
instead of aborting the whole job.
"""

from __future__ import annotations

import re
import unicodedata
from collections.abc import Callable
from contextlib import AbstractContextManager
from dataclasses import dataclass, field, replace
from typing import Any

import structlog
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models import SoundEffect
from app.pipeline.phone_subtitled_lanes import (
    CAPTION_BAND_TOP_FRAC,
    SubtitledOverlayCard,
    SubtitledSoundEffect,
    sfx_path_is_playable,
)
from app.pipeline.render_geometry import MediaFootprint
from app.services.phone_overlay_grounding import (
    _label_for_asset,
    _load_ready_pool_assets,
    resolve_phone_card_geometry,
)
from app.services.sfx_catalog import SfxEntry, resolve_described_effect

log = structlog.get_logger()

# `ReactionBeat.occurrence == "every"` cap -- mirrors
# `app.agents._schemas.reaction_beats.MAX_REACTION_BEATS`'s spirit of a small,
# creator-legible bound rather than an accidental unbounded loop over a noisy
# transcript match.
MAX_REACTION_OCCURRENCES = 8

_PHOTO_HOLD_S_DEFAULT = 3.0
_STICKER_HOLD_S_DEFAULT = 2.5
_MIN_CARD_WINDOW_S = 0.3
_MIN_TRUNCATED_WINDOW_S = 0.4
_CLOSING_DEFAULT_LOOKBACK_S = 3.0

# KRI-176 defaults (photo corner); a distinct lower-left spot for stickers so
# a beat's photo + sticker pair (e.g. a name photo + a rank badge) don't start
# life stacked on top of each other before arbitration even runs.
_PHOTO_SLOT = {"x_frac": 0.74, "y_frac": 0.22, "scale": 0.36}
_STICKER_SLOT = {"x_frac": 0.26, "y_frac": 0.24, "scale": 0.28}

_MAX_LABEL_LEN = 60
_MAX_REASON_LEN = 160

# --- phrase folding ----------------------------------------------------------

_NUMBER_WORDS_EN: dict[str, str] = {
    "zero": "0",
    "one": "1",
    "two": "2",
    "three": "3",
    "four": "4",
    "five": "5",
    "six": "6",
    "seven": "7",
    "eight": "8",
    "nine": "9",
    "ten": "10",
    "eleven": "11",
    "twelve": "12",
    "thirteen": "13",
    "fourteen": "14",
    "fifteen": "15",
    "sixteen": "16",
    "seventeen": "17",
    "eighteen": "18",
    "nineteen": "19",
    "twenty": "20",
}
# Turkish 0-10 + 20 as they're written as ONE word -- 11-19 are two-word
# compounds ("on bir" = "ten one" = 11) and are deliberately out of scope; a
# creator's trigger for those already matches token-by-token ("on" then "bir"
# adjacent), just not as a fused "11".
_NUMBER_WORDS_TR: dict[str, str] = {
    "sifir": "0",
    "bir": "1",
    "iki": "2",
    "uc": "3",
    "dort": "4",
    "bes": "5",
    "alti": "6",
    "yedi": "7",
    "sekiz": "8",
    "dokuz": "9",
    "on": "10",
    "yirmi": "20",
}
# Dropped ONLY when immediately followed by a digit token ("number 3" -> "3");
# left alone everywhere else so English "no" (a reaction trigger in its own
# right) is untouched.
_NUMBER_FILLERS = frozenset({"number", "no", "numara", "rank"})
_POSSESSIVE_RE = re.compile(r"['’]s\b", re.IGNORECASE)
_NON_ALNUM_RE = re.compile(r"[^a-z0-9]+")
# Turkish dotted capital I / dotless small i -- Python's locale-independent
# str.casefold() does not apply Turkish dotting rules, so these must be
# folded to plain "i" explicitly, before NFKD/casefold ever run.
_TR_I_MAP = {"İ": "i", "ı": "i"}


def _raw_fold(text: object) -> list[str]:
    """`fold_tokens` up through number-word mapping, WITHOUT the filler-drop
    pass -- split out so `_token_stream` can apply that pass across word
    boundaries instead of within a single transcript word (see below)."""

    raw = str(text or "")
    if not raw:
        return []
    for src, dst in _TR_I_MAP.items():
        raw = raw.replace(src, dst)
    raw = _POSSESSIVE_RE.sub("", raw)
    raw = unicodedata.normalize("NFKD", raw)
    raw = "".join(ch for ch in raw if not unicodedata.combining(ch))
    raw = raw.casefold()
    raw = _NON_ALNUM_RE.sub(" ", raw)
    words = [w for w in raw.split() if w]
    return [_NUMBER_WORDS_EN.get(w, _NUMBER_WORDS_TR.get(w, w)) for w in words]


def _drop_number_fillers(tokens: list[str]) -> list[str]:
    kept: list[str] = []
    for index, token in enumerate(tokens):
        next_token = tokens[index + 1] if index + 1 < len(tokens) else None
        if token in _NUMBER_FILLERS and next_token is not None and next_token.isdigit():
            continue
        kept.append(token)
    return kept


def fold_tokens(text: object) -> list[str]:
    """Normalize a phrase into a list of matchable word tokens.

    Unicode NFKD + combining-mark strip + casefold makes accented Latin
    ("Leão", "Vlahović") match their plain-ASCII spelling; non-alphanumerics
    become token breaks; a trailing possessive ("'s"/"'s") is dropped; number
    words 0-20 (English) and the single-word Turkish numbers fold to their
    digit string so "number three"/"3"/"#3"/"no. 3"/"numara 3" all fold to
    ``["3"]`` -- the filler word ("number"/"no"/"numara"/"rank") is dropped
    only when it is immediately followed by a digit token, so a bare "no" (a
    reaction trigger) is never touched.

    A creator's trigger is always folded as one call over the whole phrase,
    so "number three" sees its own "number" immediately followed by "3" in
    this same call. The raw transcript is folded word-by-word instead (see
    `_token_stream`), which needs the SAME filler-drop rule applied across
    word boundaries -- `_raw_fold`/`_drop_number_fillers` are split out for
    that reason.
    """

    return _drop_number_fillers(_raw_fold(text))


# --- transcript token stream + trigger matching ------------------------------


@dataclass(frozen=True)
class _Token:
    text: str
    start_s: float
    end_s: float


def _coerce_float(value: Any, default: float = 0.0) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def _token_stream(words: list[dict]) -> list[_Token]:
    """Fold every transcript word into tokens carrying that word's timing.

    The filler-drop rule ("number" + a following digit token) is applied
    AFTER flattening every word's tokens into one sequence, not per word --
    "number" and "three" are two separate transcript words, so the drop only
    fires with cross-word adjacency in view (`fold_tokens` gets the whole
    trigger phrase in one call and doesn't need this two-step split).
    """

    raw_tokens: list[str] = []
    timings: list[tuple[float, float]] = []
    for word in words or []:
        if not isinstance(word, dict):
            continue
        start_s = _coerce_float(word.get("start_s"), 0.0)
        end_s = _coerce_float(word.get("end_s"), start_s)
        for token in _raw_fold(word.get("text")):
            raw_tokens.append(token)
            timings.append((start_s, end_s))
    stream: list[_Token] = []
    for index, token in enumerate(raw_tokens):
        next_token = raw_tokens[index + 1] if index + 1 < len(raw_tokens) else None
        if token in _NUMBER_FILLERS and next_token is not None and next_token.isdigit():
            continue
        start_s, end_s = timings[index]
        stream.append(_Token(token, start_s, end_s))
    return stream


def _find_all(stream_tokens: list[str], target: list[str]) -> list[int]:
    if not target:
        return []
    n, m = len(stream_tokens), len(target)
    if m > n:
        return []
    return [i for i in range(n - m + 1) if stream_tokens[i : i + m] == target]


@dataclass(frozen=True)
class _MatchResult:
    windows: list[tuple[float, float]] = field(default_factory=list)
    reason: str | None = None


def _match_trigger(
    stream: list[_Token], trigger: str, after: str | None, occurrence: str
) -> _MatchResult:
    """Find qualifying spoken occurrences of ``trigger`` in ``stream``.

    ``after`` (when given) restricts occurrences to those starting strictly
    after the FIRST spoken match of ``after`` ends; if ``after`` itself was
    never heard, or every ``trigger`` match falls before/at it, the whole
    lookup fails with reason ``"after_not_heard"`` rather than silently
    falling back to "any occurrence".
    """

    trigger_tokens = fold_tokens(trigger)
    if not trigger_tokens:
        return _MatchResult(reason="never_heard")
    stream_tokens = [t.text for t in stream]
    starts = _find_all(stream_tokens, trigger_tokens)
    if not starts:
        return _MatchResult(reason="never_heard")

    after_end_s: float | None = None
    if after:
        after_tokens = fold_tokens(after)
        after_starts = _find_all(stream_tokens, after_tokens) if after_tokens else []
        if not after_starts:
            return _MatchResult(reason="after_not_heard")
        after_end_s = stream[after_starts[0] + len(after_tokens) - 1].end_s

    windows: list[tuple[float, float]] = []
    for start_index in starts:
        match_start_s = stream[start_index].start_s
        match_end_s = stream[start_index + len(trigger_tokens) - 1].end_s
        if after_end_s is not None and not (match_start_s > after_end_s):
            continue
        windows.append((match_start_s, match_end_s))
    if not windows:
        return _MatchResult(reason="after_not_heard")
    if occurrence == "every":
        return _MatchResult(windows=windows[:MAX_REACTION_OCCURRENCES])
    return _MatchResult(windows=windows[:1])


# --- pool asset / sfx resolution ---------------------------------------------


def _index_pool_assets(assets: list[dict]) -> dict[str, dict]:
    return {str(a["id"]).casefold(): a for a in assets}


# The creator manifest exposes a Visuals-pool image to the model as
# `media_id = f"asset-{PlanItemAsset.id}"` (`creator_sessions.py` ~line 397),
# so a strategy's `visual_id`/`badge_visual_id` normally arrives prefixed --
# but `_load_ready_pool_assets` (and `_index_pool_assets` above) key rows by
# the raw `PlanItemAsset.id`, with no prefix.
_ASSET_ID_PREFIX = "asset-"


def _visual_id_candidates(visual_id: str) -> list[str]:
    """The manifest-prefixed id normalized to a bare pool-asset id, tried
    FIRST, with the id exactly as supplied kept as a fallback -- a strategy
    or a legacy caller could echo back an id that was never prefixed."""

    candidates = [visual_id]
    if visual_id.casefold().startswith(_ASSET_ID_PREFIX):
        candidates.insert(0, visual_id[len(_ASSET_ID_PREFIX) :])
    return candidates


def _resolve_visual(
    visual_id: str | None, assets_by_id: dict[str, dict]
) -> tuple[dict | None, str | None]:
    if not visual_id:
        return None, None
    asset = None
    for candidate in _visual_id_candidates(str(visual_id)):
        asset = assets_by_id.get(candidate.casefold())
        if asset is not None:
            break
    if asset is None:
        return None, "visual_not_in_pool"
    if asset.get("kind") == "video":
        return None, "visual_is_video"
    if asset.get("kind") != "image" or not asset.get("gcs_generation"):
        return None, "visual_not_in_pool"
    return asset, None


def _load_sfx_entries(
    open_session: Callable[[], AbstractContextManager[Session]],
) -> list[SfxEntry]:
    """Ready, published, non-archived, phone-playable catalog effects.

    A dedicated seam (mirrors `phone_overlay_grounding._load_ready_pool_assets`)
    so tests can supply a fixed catalog without a real database.
    """

    with open_session() as db:
        rows = (
            db.execute(
                select(SoundEffect).where(
                    SoundEffect.status == "ready",
                    SoundEffect.published_at.isnot(None),
                    SoundEffect.archived_at.is_(None),
                    SoundEffect.audio_gcs_path.isnot(None),
                )
            )
            .scalars()
            .all()
        )
    return [
        SfxEntry.from_row(row)
        for row in rows
        if row.audio_gcs_path and sfx_path_is_playable(row.audio_gcs_path)
    ]


def _resolve_sound(
    sound: str | None, entries: list[SfxEntry], by_id: dict[str, SfxEntry]
) -> SfxEntry | None:
    if not sound:
        return None
    exact = by_id.get(str(sound))
    if exact is not None:
        return exact
    return resolve_described_effect(entries, sound)


# --- card candidates ----------------------------------------------------------


@dataclass
class _CardCandidate:
    id: str
    beat_id: str
    n: int
    trigger: str
    slot: str  # "photo" | "sticker"
    start_s: float
    end_s: float
    asset: dict


@dataclass
class _Occurrence:
    beat_id: str
    n: int
    trigger: str
    match_start_s: float
    match_end_s: float
    card_id: str | None = None
    sound: SubtitledSoundEffect | None = None
    sound_label: str | None = None
    failed: bool = False
    reason: str | None = None


def _slot_defaults(slot: str) -> dict[str, float]:
    return _PHOTO_SLOT if slot == "photo" else _STICKER_SLOT


def _truncate_same_slot_overlaps(
    cards: list[_CardCandidate],
) -> tuple[list[_CardCandidate], dict[str, str]]:
    """Same-slot cards overlapping in time: truncate the earlier at the
    later's start; drop the earlier if that leaves < 0.4s (reason
    "overlap")."""

    dropped: dict[str, str] = {}
    by_slot: dict[str, list[_CardCandidate]] = {}
    for card in cards:
        by_slot.setdefault(card.slot, []).append(card)

    survivors: list[_CardCandidate] = []
    for slot_cards in by_slot.values():
        ordered = sorted(slot_cards, key=lambda c: (c.start_s, c.n))
        i = 0
        while i < len(ordered):
            current = ordered[i]
            if i + 1 < len(ordered):
                nxt = ordered[i + 1]
                if current.end_s > nxt.start_s:
                    new_end = nxt.start_s
                    if new_end - current.start_s < _MIN_TRUNCATED_WINDOW_S:
                        dropped[current.id] = "overlap"
                        i += 1
                        continue
                    current = replace(current, end_s=new_end)
            survivors.append(current)
            i += 1
    return survivors, dropped


@dataclass(frozen=True)
class GroundedReactionBeats:
    """The grounding result: ready-to-bind cards/sounds + a creator-safe
    receipt."""

    cards: list[SubtitledOverlayCard] = field(default_factory=list)
    sound_effects: list[SubtitledSoundEffect] = field(default_factory=list)
    receipt: dict[str, Any] = field(default_factory=dict)


def ground_phone_reaction_beats(
    open_session: Callable[[], AbstractContextManager[Session]],
    *,
    job_id: str,
    beats: list[dict],
    closing: dict | None,
    words: list[dict],
    duration_s: float,
    clip_path: str | None,
) -> GroundedReactionBeats:
    """Ground creator-authored reaction beats + closing media against the
    clip's raw Whisper words into phone Talking-lane cards/sounds (KRI-178).

    Fails open at every stage: a broken face sampler leaves no face regions
    protected (`face_sampling == "failed"`), a bad DB row is skipped, and any
    unexpected error inside a single beat's processing reports that ONE beat
    unplaced with reason `"error"` rather than raising. A genuinely broken
    caller (e.g. `open_session` itself failing) still propagates -- the
    runner decides how job-level failures are handled.
    """

    duration_s = max(0.0, _coerce_float(duration_s, 0.0))
    stream = _token_stream(words)

    pool_assets = _load_ready_pool_assets(open_session, job_id=job_id) if (beats or closing) else []
    assets_by_id = _index_pool_assets(pool_assets)

    wants_sound = any(isinstance(b, dict) and b.get("sound") for b in beats or [])
    sfx_entries = _load_sfx_entries(open_session) if wants_sound else []
    sfx_by_id = {e.id: e for e in sfx_entries}

    occurrences: dict[tuple[str, int], _Occurrence] = {}
    beat_order: list[str] = []
    card_candidates: list[_CardCandidate] = []
    card_id_seen: set[str] = set()

    def _record_beat_failure(beat_id: str, trigger: str, reason: str) -> None:
        key = (beat_id, 0)
        if key not in occurrences:
            occurrences[key] = _Occurrence(
                beat_id=beat_id,
                n=0,
                trigger=trigger,
                match_start_s=0.0,
                match_end_s=0.0,
                failed=True,
                reason=reason[:_MAX_REASON_LEN],
            )

    for beat_index, raw_beat in enumerate(beats or []):
        beat_id = f"beat-{beat_index}"
        trigger = ""
        try:
            if not isinstance(raw_beat, dict):
                raise ValueError("beat is not an object")
            beat_id = str(raw_beat.get("beat_id") or beat_id)
            beat_order.append(beat_id)
            trigger = str(raw_beat.get("trigger") or "")
            after = raw_beat.get("after")
            occurrence_mode = raw_beat.get("occurrence") or "first"
            visual_id = raw_beat.get("visual_id")
            visual_role = raw_beat.get("visual_role") or "sticker"
            slot = "photo" if visual_role == "photo" else "sticker"
            sound_request = raw_beat.get("sound")
            hold_s = raw_beat.get("hold_s")

            if not visual_id and not sound_request:
                _record_beat_failure(beat_id, trigger, "error: beat has neither visual nor sound")
                continue

            asset: dict | None = None
            if visual_id:
                asset, visual_reason = _resolve_visual(visual_id, assets_by_id)
                if visual_reason:
                    _record_beat_failure(beat_id, trigger, visual_reason)
                    continue

            match = _match_trigger(stream, trigger, after, str(occurrence_mode))
            if match.reason:
                _record_beat_failure(beat_id, trigger, match.reason)
                continue

            hold = _coerce_float(hold_s, 0.0) if hold_s is not None else None
            if hold is None:
                hold = _PHOTO_HOLD_S_DEFAULT if slot == "photo" else _STICKER_HOLD_S_DEFAULT

            any_success = False
            for occurrence_index, (match_start_s, match_end_s) in enumerate(match.windows, start=1):
                occ = _Occurrence(
                    beat_id=beat_id,
                    n=occurrence_index,
                    trigger=trigger,
                    match_start_s=match_start_s,
                    match_end_s=match_end_s,
                )
                occurrences[(beat_id, occurrence_index)] = occ

                if asset is not None:
                    win_start = max(0.0, match_start_s)
                    win_end = min(duration_s, win_start + hold)
                    if win_end - win_start >= _MIN_CARD_WINDOW_S:
                        card_id = f"beat-{beat_id}-{occurrence_index}"
                        # A defensive de-dupe: a malformed `beat_id` colliding
                        # with an earlier beat's id must not silently merge
                        # two unrelated cards under one id.
                        suffix = 1
                        while card_id in card_id_seen:
                            suffix += 1
                            card_id = f"beat-{beat_id}-{occurrence_index}-{suffix}"
                        card_id_seen.add(card_id)
                        card_candidates.append(
                            _CardCandidate(
                                id=card_id,
                                beat_id=beat_id,
                                n=occurrence_index,
                                trigger=trigger,
                                slot=slot,
                                start_s=win_start,
                                end_s=win_end,
                                asset=asset,
                            )
                        )
                        occ.card_id = card_id
                        any_success = True

                if sound_request:
                    entry = _resolve_sound(str(sound_request), sfx_entries, sfx_by_id)
                    if entry is not None:
                        occ.sound = SubtitledSoundEffect(
                            id=f"beat-{beat_id}-{occurrence_index}-sfx",
                            catalog_id=entry.id,
                            at_s=max(0.0, match_start_s),
                        )
                        occ.sound_label = entry.name[:_MAX_LABEL_LEN]
                        any_success = True
                    elif occ.card_id is None:
                        occ.failed = True
                        occ.reason = "sound_not_found"

                if occ.card_id is None and occ.sound is None and not occ.failed:
                    # Neither a usable card window nor a resolved sound came
                    # out of this occurrence -- the window collapsed under
                    # `_MIN_CARD_WINDOW_S` after clamping to duration_s (a
                    # trigger spoken right near the end of the clip) and no
                    # sound was requested. Distinct from "overlap", which is
                    # reserved for a window shortened by ANOTHER card.
                    occ.failed = True
                    occ.reason = "too_short"

            if not any_success and (beat_id, 1) not in occurrences:
                # `occurrence: "first"` with zero windows never entered the
                # loop above (shouldn't happen -- match.windows is non-empty
                # here -- but stay defensive).
                _record_beat_failure(beat_id, trigger, "never_heard")
        except Exception as exc:  # noqa: BLE001 - one bad beat must not sink the job
            log.warning(
                "phone_reaction_grounding.beat_failed",
                job_id=job_id,
                beat_id=beat_id,
                error=str(exc)[:200],
            )
            _record_beat_failure(beat_id, trigger, f"error: {exc}"[:_MAX_REASON_LEN])

    # --- same-slot overlap truncation (beats only, before closing) ----------
    card_candidates, overlap_dropped = _truncate_same_slot_overlaps(card_candidates)
    cards_by_id = {c.id: c for c in card_candidates}
    for card_id, reason in overlap_dropped.items():
        occ = next((o for o in occurrences.values() if o.card_id == card_id), None)
        if occ is not None:
            occ.card_id = None
            if occ.sound is None:
                occ.failed = True
                occ.reason = reason

    # --- closing media --------------------------------------------------------
    # `badge` tracks the closing badge SEPARATELY from the photo's `status` --
    # a badge can fail (missing from the pool, a video, no safe spot) while
    # the photo still stands, and vice versa (the merge-target-dropped check
    # below can flip the photo to "unplaced" after the badge already placed).
    closing_receipt: dict[str, Any] = {"status": "none", "badge": "none"}
    merged_card_id: str | None = None
    badge_status = "none"
    badge_reason: str | None = None
    if closing:
        try:
            closing_visual_id = closing.get("visual_id")
            badge_visual_id = closing.get("badge_visual_id")
            from_trigger = closing.get("from_trigger")

            from_s = max(0.0, duration_s - _CLOSING_DEFAULT_LOOKBACK_S)
            if from_trigger:
                from_tokens = fold_tokens(str(from_trigger))
                stream_tokens = [t.text for t in stream]
                starts = _find_all(stream_tokens, from_tokens)
                if starts:
                    last_start = starts[-1]
                    from_s = stream[last_start + len(from_tokens) - 1].end_s

            start_s = max(0.0, min(from_s, duration_s))
            end_s = duration_s

            closing_asset, closing_reason = _resolve_visual(closing_visual_id, assets_by_id)
            if closing_asset is None:
                closing_receipt = {
                    "status": "unplaced",
                    "reason": closing_reason or "visual_not_in_pool",
                    "from_s": round(start_s, 3),
                    "badge": "none",
                }
            else:
                closing_label = _label_for_asset(closing_asset)
                closing_asset_id = str(closing_asset["id"]).casefold()

                # Merge case: a surviving beat card in the photo slot for the
                # SAME asset overlapping the closing window is extended to
                # duration_s in place, instead of a duplicate closing-photo
                # card being created.
                candidate_merge = None
                for card in card_candidates:
                    if (
                        card.slot == "photo"
                        and str(card.asset["id"]).casefold() == closing_asset_id
                        and card.end_s > start_s
                    ):
                        if candidate_merge is None or card.start_s > candidate_merge.start_s:
                            candidate_merge = card
                if candidate_merge is not None:
                    cards_by_id[candidate_merge.id] = replace(candidate_merge, end_s=end_s)
                    merged_card_id = candidate_merge.id
                else:
                    photo_card_id = "closing-photo"
                    cards_by_id[photo_card_id] = _CardCandidate(
                        id=photo_card_id,
                        beat_id="__closing__",
                        n=1,
                        trigger="",
                        slot="photo",
                        start_s=start_s,
                        end_s=end_s,
                        asset=closing_asset,
                    )

                closing_receipt = {
                    "status": "placed",
                    "from_s": round(start_s, 3),
                    "visual_label": closing_label,
                }

                if badge_visual_id:
                    badge_asset, badge_visual_reason = _resolve_visual(
                        badge_visual_id, assets_by_id
                    )
                    if badge_asset is None:
                        badge_status = "unplaced"
                        badge_reason = badge_visual_reason or "visual_not_in_pool"
                    else:
                        cards_by_id["closing-badge"] = _CardCandidate(
                            id="closing-badge",
                            beat_id="__closing__",
                            n=1,
                            trigger="",
                            slot="sticker",
                            start_s=start_s,
                            end_s=end_s,
                            asset=badge_asset,
                        )
                        # Tentative -- geometry arbitration (below) can still
                        # omit the badge card (no_safe_spot/duplicate), which
                        # flips this back to "unplaced".
                        badge_status = "placed"

                # Closing wins: any OTHER surviving card overlapping the
                # closing window gets truncated at its start; drop it if that
                # leaves too little to show.
                for card_id, card in list(cards_by_id.items()):
                    if card_id in ("closing-photo", "closing-badge", merged_card_id):
                        continue
                    if card.end_s > start_s and card.start_s < end_s:
                        new_end = min(card.end_s, start_s)
                        if new_end - card.start_s < _MIN_CARD_WINDOW_S:
                            del cards_by_id[card_id]
                            occ = next(
                                (o for o in occurrences.values() if o.card_id == card_id), None
                            )
                            if occ is not None:
                                occ.card_id = None
                                if occ.sound is None:
                                    occ.failed = True
                                    occ.reason = "overlap"
                        else:
                            cards_by_id[card_id] = replace(card, end_s=new_end)
        except Exception as exc:  # noqa: BLE001 - closing failure must not sink beats
            log.warning(
                "phone_reaction_grounding.closing_failed", job_id=job_id, error=str(exc)[:200]
            )
            closing_receipt = {
                "status": "unplaced",
                "reason": f"error: {exc}"[:_MAX_REASON_LEN],
                "badge": "none",
            }
            badge_status = "none"
            badge_reason = None

    # --- geometry arbitration --------------------------------------------------
    overlays_for_arbitration: list[dict[str, Any]] = []
    footprints_by_id: dict[str, MediaFootprint] = {}
    for card_id, card in cards_by_id.items():
        defaults = _slot_defaults(card.slot)
        aspect = float(card.asset["aspect"]) if card.asset.get("aspect") else 1.0
        footprints_by_id[card_id] = MediaFootprint(aspect_ratio=aspect)
        overlays_for_arbitration.append(
            {
                "id": card_id,
                "asset_id": card.asset["id"],
                "src_gcs_path": card.asset.get("gcs_path"),
                "position": "custom",
                "x_frac": defaults["x_frac"],
                "y_frac": defaults["y_frac"],
                "scale": defaults["scale"],
                "start_s": card.start_s,
                "end_s": card.end_s,
            }
        )

    resolved_by_id, geometry_reason_by_id, face_sampling = resolve_phone_card_geometry(
        overlays_for_arbitration,
        clip_path=clip_path,
        job_id=job_id,
        footprints_by_id=footprints_by_id,
    )

    cards: list[SubtitledOverlayCard] = []
    for card_id, card in cards_by_id.items():
        resolved = resolved_by_id.get(card_id)
        if resolved is None:
            reason = geometry_reason_by_id.get(card_id, "no_safe_spot")
            if card_id == "closing-photo":
                closing_receipt = {
                    **{k: v for k, v in closing_receipt.items() if k != "status"},
                    "status": "unplaced",
                    "reason": reason,
                }
            elif card_id == "closing-badge":
                # Badge omission is not a PHOTO failure -- the photo still
                # stands -- but is tracked as its own badge/badge_reason pair.
                # Geometry's own "duplicate" decision maps onto the closing
                # reason vocabulary as "overlap" (the same asset collided
                # with another placement's time window).
                badge_status = "unplaced"
                badge_reason = "no_safe_spot" if reason == "no_safe_spot" else "overlap"
            else:
                occ = next((o for o in occurrences.values() if o.card_id == card_id), None)
                if occ is not None:
                    occ.card_id = None
                    if occ.sound is None:
                        occ.failed = True
                        occ.reason = reason
            continue
        y_frac = min(float(resolved["y_frac"]), CAPTION_BAND_TOP_FRAC)
        cards.append(
            SubtitledOverlayCard(
                id=card_id,
                media_id=str(card.asset["id"]),
                gcs_path=card.asset["gcs_path"],
                generation=str(card.asset.get("gcs_generation")),
                start_s=float(resolved["start_s"]),
                end_s=float(resolved["end_s"]),
                x_frac=float(resolved["x_frac"]),
                y_frac=y_frac,
                scale=float(resolved["scale"]),
                fade=True,
                z=1 if card.slot == "sticker" else 0,
            )
        )

    if merged_card_id is not None and merged_card_id not in {c.id for c in cards}:
        # The card the closing photo merged into didn't survive geometry
        # arbitration -- the closing photo has nothing left to show either.
        closing_receipt = {
            **{k: v for k, v in closing_receipt.items() if k != "status"},
            "status": "unplaced",
            "reason": geometry_reason_by_id.get(merged_card_id, "no_safe_spot"),
        }

    # `badge`/`badge_reason` are assembled last so no intermediate dict
    # rebuild above (photo status flips) can drop them.
    closing_receipt["badge"] = badge_status
    if badge_status == "unplaced" and badge_reason:
        closing_receipt["badge_reason"] = badge_reason
    elif "badge_reason" in closing_receipt:
        del closing_receipt["badge_reason"]

    # --- receipt assembly -------------------------------------------------------
    placed: list[dict[str, Any]] = []
    unplaced: list[dict[str, Any]] = []
    seen_unplaced_beats: set[str] = set()

    ordered_keys = sorted(
        occurrences.keys(),
        key=lambda k: (beat_order.index(k[0]) if k[0] in beat_order else 0, k[1]),
    )
    for beat_id, n in ordered_keys:
        occ = occurrences[(beat_id, n)]
        card = cards_by_id.get(occ.card_id) if occ.card_id else None
        card_alive = occ.card_id is not None and any(c.id == occ.card_id for c in cards)
        if card_alive or occ.sound is not None:
            entry: dict[str, Any] = {"beat_id": beat_id, "trigger": occ.trigger}
            if card_alive and card is not None:
                entry["at_s"] = round(card.start_s, 3)
                entry["end_s"] = round(card.end_s, 3)
                entry["visual_label"] = _label_for_asset(card.asset)
            else:
                entry["at_s"] = round(occ.match_start_s, 3)
                entry["end_s"] = round(occ.match_end_s, 3)
            if occ.sound is not None:
                entry["sound_label"] = occ.sound_label
            placed.append(entry)
        elif occ.reason and beat_id not in seen_unplaced_beats:
            unplaced.append(
                {
                    "beat_id": beat_id,
                    "trigger": occ.trigger,
                    "reason": (occ.reason or "error")[:_MAX_REASON_LEN],
                }
            )
            seen_unplaced_beats.add(beat_id)

    # A beat with `occurrence: "every"` may have SOME occurrences placed and
    # others individually dropped (overlap/geometry) -- that is not a beat
    # failure as long as at least one occurrence made it; only surface an
    # unplaced entry for a beat with literally zero placed occurrences.
    placed_beat_ids = {p["beat_id"] for p in placed}
    unplaced = [u for u in unplaced if u["beat_id"] not in placed_beat_ids]

    sound_effects = [occ.sound for occ in occurrences.values() if occ.sound is not None]

    receipt: dict[str, Any] = {
        "version": 1,
        "matcher": "phrase",
        "face_sampling": face_sampling,
        "placed": placed,
        "unplaced": unplaced,
        "closing": closing_receipt,
    }
    return GroundedReactionBeats(cards=cards, sound_effects=sound_effects, receipt=receipt)
