"""Pause the creator's take for another of his clips, in a multi-clip Talking edit.

"After I say X, show the congress clip with its own sound": the picture switches to that clip
and its audio plays while the creator's take is PAUSED; then the take resumes at the second it
stopped. Nothing he said is lost and the video grows by the clip's length. This is not a
cutaway (the take keeps running under a cutaway) and not the ending clip (that follows the
whole talk).

The data shape is one number per insert: ``PhoneInsert.at_take_s``, a position on the TAKE
timeline (the first speaker's seconds with no insert counted). Three pure steps own the rest:

1. `place_inserts` turns the creator's asks (a phrase he says, or a second) into
   `PhoneInsert`s, or says why one cannot be placed. The point is always a boundary in his
   speech (the end of a caption, a pause between words, a word's end), never inside a word, and
   never in the hook window or the last second. No guess is made for a phrase that is not
   there.
2. `warp_for_inserts` moves everything expressed on the take timeline (captions, text rows,
   cards, effects, cutaways) onto the final timeline: whatever starts at or after an insert
   moves later by its length, and a window that spans the point is split around it, so
   nothing is drawn over the inserted clip and nothing is lost.
3. `compile_phone_subtitled_plan(inserts=...)` lays the clips out (the compiler's contract is
   unchanged: everything it is given is on the final timeline).

`inserts_from_recipe` is the inverse of the compile, so an editor Save keeps an insert as it
was. Facts only decide timing; the inserted clip's words are never drawn as text.
"""

from __future__ import annotations

import math
from collections.abc import Collection, Iterable, Mapping, Sequence
from dataclasses import dataclass
from typing import Any

from app.kria.recipes_v2 import EditRecipeV2
from app.pipeline.phone_subtitled_plan import (
    INSERT_CLIP_PREFIX,
    MAX_INSERT_CLIPS,
    PhoneCutaway,
    PhoneInsert,
)
from app.pipeline.phone_talking_cutaways import HOOK_FREE_S, TAIL_FREE_S
from app.services.phone_sources import PhoneSourceBinding

__all__ = [
    "HOOK_FREE_S",
    "PHONE_INSERT_CLIPS_FIELD",
    "MAX_INSERTS",
    "MAX_INSERT_S",
    "MIN_GAP_S",
    "TAIL_FREE_S",
    "InsertAsk",
    "InsertPlacement",
    "WarpedTimeline",
    "insert_blocked_windows",
    "insert_rows",
    "inserts_from_recipe",
    "place_inserts",
    "shift_time",
    "warp_for_inserts",
]

# The variant receipt of what the worker placed (`{"placed": [...], "dropped": [...]}`).
PHONE_INSERT_CLIPS_FIELD = "phone_insert_clips"
MAX_INSERTS = MAX_INSERT_CLIPS
# A longer clip is not an insert, it is a second video: refused rather than trimmed.
MAX_INSERT_S = 60.0
# Two inserts closer than this on the take would leave a few words between two long breaks.
MIN_GAP_S = 2.0
# How far past the spoken phrase the insert may wait for the end of a caption or a pause.
SNAP_SLACK_S = 2.5
# A gap between two words this long is a pause the insert can sit in.
PAUSE_S = 0.25
_EPS = 1e-6
# A cleanup seam this close to the point takes it: a speaker clip of a few frames beside the
# insert is not worth playing.
SEAM_SNAP_S = 0.1
# A part of a split window shorter than this is not worth drawing.
_MIN_PART_S = 0.3
_MIN_CUTAWAY_PART_S = 0.5
# Footage cutaways keep this far clear of an insert point (take timeline, each side).
_BLOCK_S = 0.05


@dataclass(frozen=True)
class InsertAsk:
    """What the creator asked for, as the approved strategy carries it."""

    media_id: str
    at_phrase: str | None = None
    at_s: float | None = None
    keep_audio: bool = True


@dataclass(frozen=True)
class InsertPlacement:
    """One ask and what became of it: a `PhoneInsert`, or a ``reason`` it was not used."""

    ask: InsertAsk
    insert: PhoneInsert | None
    reason: str | None = None
    basis: str | None = None  # "phrase" | "second"
    snap: str | None = None  # "cue_end" | "pause" | "word_end" | "exact" | "seam"


@dataclass(frozen=True)
class WarpedTimeline:
    cues: list[dict]
    text_rows: list[dict]
    lanes: Any
    cutaways: tuple[PhoneCutaway, ...]


def _field(word: Any, name: str) -> float:
    value = word.get(name) if isinstance(word, Mapping) else getattr(word, name, None)
    return float(value) if value is not None else 0.0


def _text(word: Any) -> str:
    value = word.get("text") if isinstance(word, Mapping) else getattr(word, "text", "")
    return str(value or "")


def _take_words(words: Sequence[Any], take_s: float) -> list[dict]:
    """The creator's words on the take timeline, in order (an extra speaker's come after it)."""
    rows = [
        {"text": _text(w), "start_s": _field(w, "start_s"), "end_s": _field(w, "end_s")}
        for w in words
        if _field(w, "start_s") < take_s - _EPS
    ]
    return sorted(rows, key=lambda w: w["start_s"])


def _boundaries(
    words: Sequence[Mapping[str, Any]], cues: Iterable[Mapping[str, Any]]
) -> list[tuple[float, str]]:
    """Moments in his speech where a pause for another clip reads naturally."""
    found: dict[float, str] = {}
    for index, word in enumerate(words):
        end = float(word["end_s"])
        following = words[index + 1]["start_s"] if index + 1 < len(words) else math.inf
        if following - end >= PAUSE_S:
            found[round(end, 3)] = "pause"
    for cue in cues:
        end = cue.get("end_s")
        if end is not None:
            found[round(float(end), 3)] = "cue_end"
    return sorted((t, kind) for t, kind in found.items())


def _snap(
    point: float, words: Sequence[Mapping[str, Any]], cues: Sequence[Mapping[str, Any]]
) -> tuple[float, str]:
    """``point`` moved to a boundary in his speech: out of a word to its end, then forward to
    the end of the caption or the pause that follows within `SNAP_SLACK_S`."""
    snap = "exact"
    for word in words:
        if float(word["start_s"]) + _EPS < point < float(word["end_s"]) - _EPS:
            point, snap = float(word["end_s"]), "word_end"
            break
    for at, kind in _boundaries(words, cues):
        if point - _EPS <= at <= point + SNAP_SLACK_S:
            return at, kind
    return point, snap


def _phrase_end(phrase: str, words: Sequence[Mapping[str, Any]], take_s: float) -> float | None:
    from app.services.speech_segments import ground_excerpt  # noqa: PLC0415

    grounded = ground_excerpt(list(words), phrase, duration_s=take_s)
    return None if grounded is None else float(grounded.word_end_s)


def place_inserts(
    asks: Sequence[InsertAsk],
    *,
    bindings: Mapping[str, PhoneSourceBinding],
    words: Sequence[Any],
    cues: Sequence[Mapping[str, Any]],
    take_s: float,
    in_use: Collection[str] = (),
    seams: Sequence[float] = (),
) -> list[InsertPlacement]:
    """One `InsertPlacement` per ask, in ask order.

    ``words``/``cues`` are the creator's speech on the take timeline (speech cleanup already
    applied, so a phrase inside a cut-out region is not found); ``in_use`` is every clip the
    edit already plays (speakers, cutaways, the ending); ``seams`` are the take-timeline seconds
    where speech cleanup joined two kept segments: a point within `SEAM_SNAP_S` of one moves
    onto it, unrounded, so no sliver of speech is left beside the insert.
    """
    speech = _take_words(words, take_s)
    placed: list[PhoneInsert] = []
    used: set[str] = set()
    out: list[InsertPlacement] = []

    def refuse(ask: InsertAsk, reason: str, **kw: Any) -> InsertPlacement:
        return InsertPlacement(ask, None, reason, **kw)

    for ask in asks:
        binding = bindings.get(ask.media_id)
        if ask.media_id in used:
            out.append(refuse(ask, "duplicate_clip"))
            continue
        if ask.media_id in in_use:
            out.append(refuse(ask, "clip_in_use"))
            continue
        if len(placed) >= MAX_INSERTS:
            out.append(refuse(ask, "too_many"))
            continue
        if binding is None:
            out.append(refuse(ask, "clip_not_found"))
            continue
        original = binding.original
        if original.width is None or original.height is None:
            out.append(refuse(ask, "not_a_video"))
            continue
        if float(original.duration_s) > MAX_INSERT_S:
            out.append(refuse(ask, "too_long"))
            continue
        if ask.at_phrase:
            basis = "phrase"
            point = _phrase_end(ask.at_phrase, speech, take_s)
            if point is None:
                out.append(refuse(ask, "phrase_not_found", basis=basis))
                continue
        elif ask.at_s is not None:
            basis, point = "second", float(ask.at_s)
        else:
            out.append(refuse(ask, "no_position"))
            continue
        if point < HOOK_FREE_S - _EPS:
            out.append(refuse(ask, "in_hook_window", basis=basis))
            continue
        at, snap = _snap(point, speech, cues)
        seam = min(seams, key=lambda s: abs(s - at), default=None)
        if seam is not None and abs(seam - at) <= SEAM_SNAP_S:
            at, snap = float(seam), "seam"
        if at > take_s - TAIL_FREE_S + _EPS:
            out.append(refuse(ask, "in_tail_window", basis=basis, snap=snap))
            continue
        if snap != "seam":
            at = round(at, 3)
        if any(abs(at - other.at_take_s) < MIN_GAP_S for other in placed):
            out.append(refuse(ask, "too_close_to_previous", basis=basis, snap=snap))
            continue
        insert = PhoneInsert(binding=binding, at_take_s=at, keep_audio=ask.keep_audio)
        placed.append(insert)
        used.add(ask.media_id)
        out.append(InsertPlacement(ask, insert, None, basis, snap))
    return out


def insert_rows(inserts: Sequence[PhoneInsert]) -> list[dict[str, Any]]:
    """Creator-safe rows (ids and seconds) of inserts on the FINAL timeline, in play order."""
    rows: list[dict[str, Any]] = []
    shift = 0.0
    for insert in sorted(inserts, key=lambda i: i.at_take_s):
        length = float(insert.binding.original.duration_s)
        start = insert.at_take_s + shift
        rows.append(
            {
                "media_id": insert.binding.media_id,
                "start_s": round(start, 3),
                "end_s": round(start + length, 3),
                "at_take_s": round(insert.at_take_s, 3),
                "keep_audio": insert.keep_audio,
            }
        )
        shift += length
    return rows


def insert_blocked_windows(inserts: Sequence[PhoneInsert]) -> list[tuple[float, float]]:
    """Take-timeline windows no footage cutaway may cover (it would straddle the pause)."""
    return [(i.at_take_s - _BLOCK_S, i.at_take_s + _BLOCK_S) for i in inserts]


def _steps(inserts: Sequence[PhoneInsert]) -> list[tuple[float, float]]:
    """``(point, length)`` per insert, the point already counting the earlier inserts."""
    steps: list[tuple[float, float]] = []
    shift = 0.0
    for insert in sorted(inserts, key=lambda i: i.at_take_s):
        length = float(insert.binding.original.duration_s)
        steps.append((insert.at_take_s + shift, length))
        shift += length
    return steps


def shift_time(t: float, inserts: Sequence[PhoneInsert]) -> float:
    """Take-timeline instant ``t`` on the final timeline: later by every insert at or before it."""
    return t + sum(i.binding.original.duration_s for i in inserts if i.at_take_s <= t + _EPS)


def _split_cue(cue: dict, point: float, length: float) -> list[dict]:
    """A caption that spans the insert point: each of its words plays once, on its own side.

    A cue holds its last words on screen until the next block starts (`build_plain_cues`), so
    one whose words are all on one side can still span the point: only the side that has its
    words keeps it, never a copy on both.
    """
    words = [w for w in cue.get("words") or [] if isinstance(w, dict)]
    if not words:
        # No word timings to split by: it plays where most of its time is, once.
        start, end = float(cue["start_s"]), float(cue["end_s"])
        if point - start >= end - point:
            return [{**cue, "end_s": point}]
        return [_shift_cue(cue, length, start=point)]
    first = [w for w in words if (float(w["start_s"]) + float(w["end_s"])) / 2 < point]
    second = words[len(first) :]
    if not second:
        return [_head_cue(cue, first, point)]
    tail = _tail_cue(cue, second, point, length)
    return [tail] if not first else [_head_cue(cue, first, point), tail]


def _head_cue(cue: dict, words: list[dict], point: float) -> dict:
    kept = [{**w, "end_s": min(float(w["end_s"]), point)} for w in words]
    whole = len(kept) == len(cue.get("words") or [])
    return {
        **cue,
        "text": cue.get("text", "") if whole else " ".join(_text(w) for w in kept),
        "end_s": point if whole else float(kept[-1]["end_s"]),
        "words": kept,
    }


def _tail_cue(cue: dict, words: list[dict], point: float, length: float) -> dict:
    kept = [
        {
            **w,
            "start_s": max(float(w["start_s"]), point) + length,
            "end_s": float(w["end_s"]) + length,
        }
        for w in words
    ]
    whole = len(kept) == len(cue.get("words") or [])
    return {
        **cue,
        "text": cue.get("text", "") if whole else " ".join(_text(w) for w in kept),
        "start_s": float(kept[0]["start_s"]),
        "end_s": float(cue["end_s"]) + length,
        "words": kept,
    }


def _shift_cue(cue: dict, length: float, *, start: float | None = None) -> dict:
    moved = {
        **cue,
        "start_s": (float(cue["start_s"]) if start is None else start) + length,
        "end_s": float(cue["end_s"]) + length,
    }
    if cue.get("words"):
        moved["words"] = [
            {**w, "start_s": float(w["start_s"]) + length, "end_s": float(w["end_s"]) + length}
            for w in cue["words"]
        ]
    return moved


def _warp_cues(cues: Sequence[dict], point: float, length: float) -> list[dict]:
    out: list[dict] = []
    for cue in cues:
        start, end = float(cue["start_s"]), float(cue["end_s"])
        if end <= point + _EPS:
            out.append(cue)
        elif start >= point - _EPS:
            out.append(_shift_cue(cue, length))
        else:
            out.extend(_split_cue(cue, point, length))
    return out


def _warp_rows(rows: Sequence[dict], point: float, length: float) -> list[dict]:
    out: list[dict] = []
    for row in rows:
        start, end = float(row["start_s"]), float(row["end_s"])
        if end <= point + _EPS:
            out.append(row)
        elif start >= point - _EPS:
            out.append({**row, "start_s": start + length, "end_s": end + length})
        else:
            out.append({**row, "end_s": point})
            later = {**row, "start_s": point + length, "end_s": end + length}
            if "id" in later:
                later["id"] = f"{row['id']}-b"
            out.append(later)
    return out


def _warp_cards(cards: Sequence[Any], point: float, length: float) -> list[Any]:
    out: list[Any] = []
    for card in cards:
        if card.end_s <= point + _EPS:
            out.append(card)
        elif card.start_s >= point - _EPS:
            out.append(
                card.model_copy(
                    update={"start_s": card.start_s + length, "end_s": card.end_s + length}
                )
            )
        else:
            if point - card.start_s >= _MIN_PART_S:
                out.append(card.model_copy(update={"end_s": point}))
            if card.end_s - point >= _MIN_PART_S:
                resumed = {
                    "id": f"{card.id}-b",
                    "start_s": point + length,
                    "end_s": card.end_s + length,
                }
                if card.kind == "video":
                    resumed["source_start_s"] = card.source_start_s + (point - card.start_s)
                out.append(card.model_copy(update=resumed))
    return out


def _warp_cutaways(
    cutaways: Sequence[PhoneCutaway], point: float, length: float
) -> list[PhoneCutaway]:
    out: list[PhoneCutaway] = []
    for cutaway in cutaways:
        if cutaway.end_s <= point + _EPS:
            out.append(cutaway)
        elif cutaway.start_s >= point - _EPS:
            out.append(
                cutaway.model_copy(
                    update={"start_s": cutaway.start_s + length, "end_s": cutaway.end_s + length}
                )
            )
        else:
            if point - cutaway.start_s >= _MIN_CUTAWAY_PART_S:
                out.append(cutaway.model_copy(update={"end_s": point}))
            if cutaway.end_s - point >= _MIN_CUTAWAY_PART_S:
                out.append(
                    cutaway.model_copy(
                        update={
                            "start_s": point + length,
                            "end_s": cutaway.end_s + length,
                            "source_start_s": cutaway.source_start_s + (point - cutaway.start_s),
                        }
                    )
                )
    return out


def warp_for_inserts(
    inserts: Sequence[PhoneInsert],
    *,
    cues: Sequence[dict],
    text_rows: Sequence[dict] = (),
    lanes: Any = None,
    cutaways: Sequence[PhoneCutaway] = (),
) -> WarpedTimeline:
    """Move a set expressed on the take timeline onto the final timeline (see module doc).

    Inputs are never mutated. With no inserts the very same objects come back.
    """
    if not inserts:
        return WarpedTimeline(list(cues or []), list(text_rows or []), lanes, tuple(cutaways or ()))
    cue_list, rows, cut_list = list(cues or []), list(text_rows or []), list(cutaways or ())
    for point, length in _steps(inserts):
        cue_list = _warp_cues(cue_list, point, length)
        rows = _warp_rows(rows, point, length)
        cut_list = _warp_cutaways(cut_list, point, length)
        if lanes is not None:
            effects = [
                effect.model_copy(
                    update={
                        "request": effect.request.model_copy(
                            update={"at_s": effect.request.at_s + length}
                        )
                    }
                )
                if effect.request.at_s >= point - _EPS
                else effect
                for effect in lanes.sound_effects
            ]
            lanes = lanes.model_copy(
                update={
                    "overlays": _warp_cards(lanes.overlays, point, length),
                    "sound_effects": effects,
                }
            )
    return WarpedTimeline(cue_list, rows, lanes, tuple(cut_list))


def inserts_from_recipe(
    recipe: EditRecipeV2, bindings: tuple[PhoneSourceBinding, ...]
) -> tuple[PhoneInsert, ...]:
    """The inverse of ``compile_phone_subtitled_plan(inserts=...)``: the inserts a pinned recipe
    plays on its main track, so an editor Save recompiles them unchanged. ``()`` for a recipe
    with none. Raises ``ValueError`` when an inserted clip no longer has a phone source."""
    track = next((t for t in recipe.tracks if t.id == "subtitled"), None)
    if track is None:
        return ()
    by_asset = {b.render_asset().id: b for b in bindings}
    found: list[PhoneInsert] = []
    earlier = 0.0
    for clip in sorted(track.clips, key=lambda c: c.timeline_start):
        if not clip.id.startswith(INSERT_CLIP_PREFIX):
            continue
        binding = by_asset.get(clip.source_asset_id)
        if binding is None:
            raise ValueError("Talking insert has no phone source binding")
        found.append(
            PhoneInsert(
                binding=binding,
                at_take_s=round(clip.timeline_start - earlier, 6),
                keep_audio=clip.volume != 0,
            )
        )
        earlier += clip.source_duration
    return tuple(found)
