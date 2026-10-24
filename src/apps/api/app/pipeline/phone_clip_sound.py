"""One clip's own sound on a phone Voiceover edit (KRI-842).

Café N1 asked for two things the Voiceover edit could not do: the bite clip's own sound
"faintly under the voice" (every other clip silent under it), and, once the voice ends, the
last clip playing on with its own sound so Caro's "bye bye" is heard. The compiler
(`phone_narrated_plan.compile_phone_narrated_plan`) takes both as plain arguments; this
module decides them from the approved brief's clip-sound asks (`kria.narrated_clip_sound_ask`)
and records what was built so the receipts read the compiled result, never the global bed.

Everything here is PURE: no I/O, no settings, no model.

The tail
--------

The voice sets the video's length, so the last clip cannot be both silent under the voice and
audible after it. The last step is cut at its voiced part (rate 1) and a second, contiguous
clip of the same footage plays on from the voice's end, at full volume.

- With a line to hear ("bye bye"), grounded in that clip's own timed speech
  (`spoken_quotes.named_line_records`, narrowed to that clip): the voiced part ends in the pause
  ``LEAD_S`` before the line and the tail runs to ``TAIL_PAD_S`` after it, so the line never
  plays muted under the voice and again in the tail. Where the footage before the line is
  shorter than the voiced part, the voiced part starts at the clip's start and the tail starts
  right after it, which is only allowed when the line has not started yet.
- With no line: the rest of the clip after the voiced part, at most ``MAX_PLAIN_TAIL_S``; the
  voiced part moves earlier when it must, to leave at least ``MIN_TAIL_S``.
- A coarse analysis may hold the line inside a longer sentence ("... Caro. Bye bye." as one
  segment): the window is then narrowed with `spoken_quotes.estimate_in_segments` before it is
  used, so the draft and the render ground the same place.
- No room, a line said too early for the voiced part to fit before it, a line that is not in the
  clip, or a clip that is not the last one: no tail, and the receipt says why. A clip with
  footage left only by slowing its voiced part is never used.
- An ask never costs the creator their video: a plan the compile refuses is retried without the
  tail, then without the gains (`NarratedClipSoundPlan.degrade`), and a planner that fails
  refuses every ask (`refused_plan`); each such ask says `render_refused`.

The receipt (``variant["clip_sound"]``)
---------------------------------------

``{"version": 1, "under_voice": [row, ...], "after_voice": row | None, "not_built": [row, ...]}``.
Every row names its ``requirement_id`` and ``media_id`` and a ``status`` of ``"built"`` or
``"not_possible"`` (with a ``reason_code``). An ``after_voice`` row also carries the tail on the
timeline (``tail_start_s``, ``tail_end_s``), in the clip's own seconds (``source_start_s``,
``source_end_s``) and the line it must hold (``line``, ``line_start_s``, ``line_end_s``).
`reconcile_clip_sound` re-reads a row against the recipe actually pinned, so an editor Save that
changed the cut can never leave a "built" row behind.
"""

from __future__ import annotations

import copy
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field, replace
from typing import TYPE_CHECKING, Any

from app.kria.narrated_clip_sound_ask import AFTER_VOICE, UNDER_VOICE
from app.pipeline.phone_narrated_plan import MAX_VOICE_TAIL_S, NarratedVoiceTail
from app.pipeline.phone_recipe_shared import EXPORT_SAFETY_MARGIN_S, timeline_end_s
from app.services.length_policy import creator_duration_matches

if TYPE_CHECKING:
    from app.kria.narrated_clip_sound_ask import ClipSoundAsk
    from app.kria.recipes_v2 import EditRecipeV2
    from app.pipeline.phone_narrated_plan import NarratedPhoneStep
    from app.services.phone_sources import PhoneSourceBinding

RECEIPT_VERSION = 1

# The pause the voiced part leaves before the line, and what the tail keeps after it. The lead
# is also what the footage duck needs to release (the cloud's side-chain release is 300 ms).
LEAD_S = 0.3
TAIL_PAD_S = 0.3
MIN_TAIL_S = 0.8
MAX_PLAIN_TAIL_S = 3.0
# Never end a window exactly on the device-measured end of the footage.
SOURCE_END_MARGIN_S = EXPORT_SAFETY_MARGIN_S
# A tail starts where the voice ends, give or take this much (two independently rounded times),
# and holds a line it ends within this much of.
_VOICE_END_TOLERANCE_S = 0.05
_COVER_TOLERANCE_S = 0.05
# A tail plays at normal volume; a rebuilt cut that left it at the quiet bed does not count.
_TAIL_MIN_GAIN = 0.5

BUILT = "built"
NOT_POSSIBLE = "not_possible"

# Why a clip-sound ask was not built. `brief_checks` words each one for the creator.
NOT_IN_EDIT = "not_in_edit"
NO_SOUND = "no_sound"
NOT_LAST_CLIP = "not_last_clip"
LINE_NOT_FOUND = "line_not_found"
LINE_AMBIGUOUS = "line_ambiguous"
LINE_TOO_LONG = "line_too_long"
LINE_TOO_EARLY = "line_too_early"
LINE_PAST_END = "line_past_end"
NO_ROOM = "no_room"
ONE_TAIL_ONLY = "one_tail_only"
LENGTH_TARGET = "length_target"
RENDER_REFUSED = "render_refused"
EDITED = "edited"


@dataclass(frozen=True)
class GroundedLine:
    """Where a spoken line the creator wants heard is said in one clip (source seconds)."""

    status: str  # "found" | "not_found" | "ambiguous" | "unknown" (its speech isn't analysed)
    quote: str | None = None
    start_s: float = 0.0
    end_s: float = 0.0


@dataclass(frozen=True)
class NarratedClipSoundPlan:
    """What the clip-sound asks change in the Voiceover compile."""

    steps: list[NarratedPhoneStep]
    under_voice_media_ids: frozenset[str]
    voice_tail: NarratedVoiceTail | None
    receipt: dict[str, Any]
    # The steps as they were before the tail moved the last one (what a refusal goes back to).
    original_steps: list[NarratedPhoneStep] = field(default_factory=list)

    def degrade(self) -> NarratedClipSoundPlan | None:
        """The plan without the next thing the compile may have refused, else None.

        An ask must never cost the creator their video (KRI-842): when the compile refuses a
        plan, the tail goes first (the clips under the voice keep their gains), then the gains
        too. Every ask that is no longer built says `render_refused`.
        """
        if self.voice_tail is not None:
            return replace(
                self,
                steps=list(self.original_steps),
                voice_tail=None,
                receipt=_refused(self.receipt, under=False),
            )
        if self.under_voice_media_ids:
            return replace(
                self,
                steps=list(self.original_steps),
                under_voice_media_ids=frozenset(),
                receipt=_refused(self.receipt, under=True),
            )
        return None


def _refused(receipt: Mapping[str, Any], *, under: bool) -> dict[str, Any]:
    """``receipt`` with the built tail (and, with ``under``, the built gains) refused."""
    out = copy.deepcopy(dict(receipt))
    rows = (
        [out.get("after_voice"), *(out.get("under_voice") or [])]
        if under
        else [out.get("after_voice")]
    )
    for row in rows:
        if isinstance(row, dict) and row.get("status") == BUILT:
            row.update(status=NOT_POSSIBLE, reason_code=RENDER_REFUSED)
    return out


def refused_plan(
    asks: Sequence[ClipSoundAsk], steps: list[NarratedPhoneStep]
) -> NarratedClipSoundPlan:
    """No change to the compile, every ask ``not_possible`` / ``render_refused``.

    For a planner that failed: the creator still gets the voiced edit, and says so plainly.
    """
    under_rows: list[dict[str, Any]] = []
    not_built: list[dict[str, Any]] = []
    after_row: dict[str, Any] | None = None
    for ask in asks:
        row = _row(ask, None, reason_code=RENDER_REFUSED)
        if ask.mode == UNDER_VOICE:
            under_rows.append(row)
        elif after_row is None:
            after_row = {**row, "line": ask.triggers[0] if ask.triggers else None}
        else:
            not_built.append(row)
    receipt = {
        "version": RECEIPT_VERSION,
        "under_voice": under_rows,
        "after_voice": after_row,
        "not_built": not_built,
    }
    return NarratedClipSoundPlan(list(steps), frozenset(), None, receipt, list(steps))


def _rows(media_snapshot: Mapping[str, Any] | None) -> list[Mapping[str, Any]]:
    rows = media_snapshot.get("clip_assignments") if isinstance(media_snapshot, Mapping) else None
    return [row for row in rows or [] if isinstance(row, Mapping)]


def snapshot_row(media_snapshot: Mapping[str, Any] | None, media_id: str) -> Mapping | None:
    return next((row for row in _rows(media_snapshot) if row.get("media_id") == media_id), None)


def _is_video(row: Mapping[str, Any]) -> bool:
    return row.get("kind", "video") == "video"


def video_media_ids(media_snapshot: Mapping[str, Any] | None) -> list[str]:
    """The approved video clips' media ids, in the snapshot's (attachment) order."""
    return [
        str(row["media_id"])
        for row in _rows(media_snapshot)
        if isinstance(row.get("media_id"), str) and _is_video(row)
    ]


def media_aliases(media_snapshot: Mapping[str, Any] | None, media_id: str) -> set[str]:
    """The ids a clip goes by: its media id and its upload's file name (an order intent may
    name the clip by either, see `generative_build._narrated_alignment_record`)."""
    row = snapshot_row(media_snapshot, media_id)
    path = row.get("gcs_path") if row is not None else None
    return {media_id, *([path.rsplit("/", 1)[-1]] if isinstance(path, str) and path else [])}


def resolve_ask_media(
    ask: ClipSoundAsk, edit_media_ids: Sequence[str], media_snapshot: Mapping[str, Any] | None
) -> tuple[str | None, str | None]:
    """``(media id, None)`` when the ask names one of the edit's clips, else ``(None, why)``.

    A clip the creator added that is not footage in this edit (a photo) is ``NO_SOUND``: it
    has none to play. Anything else the scope does not name is ``NOT_IN_EDIT``.
    """
    media = ask.media_id(edit_media_ids)
    if media is not None:
        return media, None
    known = [str(row["media_id"]) for row in _rows(media_snapshot) if row.get("media_id")]
    other = ask.media_id(known) if known else None
    row = snapshot_row(media_snapshot, other) if other is not None else None
    return None, NO_SOUND if row is not None and not _is_video(row) else NOT_IN_EDIT


def ground_trigger_line(
    triggers: Sequence[str],
    media_id: str,
    media_snapshot: Mapping[str, Any] | None,
    *,
    duration_s: float | None = None,
) -> GroundedLine:
    """The first of ``triggers`` that the clip ``media_id`` says, with its source seconds.

    Narrowed to that one clip, so a line another clip also says is not "ambiguous" here: the
    ask names the clip. ``named_line_records`` does the matching (word for word first, so a
    one-word or two-word line is found).
    """
    from app.services.spoken_quotes import (  # noqa: PLC0415
        _row_analysis,
        _segments,
        estimate_in_segments,
        fold_tokens,
        named_line_records,
    )

    row = snapshot_row(media_snapshot, media_id)
    if row is None or not row.get("analysis") or not isinstance(row.get("analysis"), Mapping):
        return GroundedLine("unknown")
    if not isinstance(row.get("duration_s"), (int, float)) and duration_s:
        row = {**row, "duration_s": float(duration_s)}
    narrowed = {"clip_assignments": [row]}
    statuses: list[str] = []
    for quote in triggers:
        record = named_line_records([quote], narrowed)
        found = record[0] if record else {}
        if found.get("status") == "found" and found.get("media_id") == media_id:
            start, end = float(found["start_s"]), float(found["end_s"])
            # A coarse analysis holds a short line inside a longer sentence ("... Caro. Bye bye!"
            # as one segment): the window is the whole sentence. Narrow it with the same
            # estimate the long-line case uses, so the voiced part is not pushed back to the
            # sentence's start, and the draft and the render agree on the line's place.
            segments = _segments(_row_analysis(row))
            said = sum(
                len(fold_tokens(str(seg.get("text") or "")))
                for seg in segments
                if isinstance(seg.get("start_s"), (int, float))
                and isinstance(seg.get("end_s"), (int, float))
                and float(seg["start_s"]) < end - 1e-6
                and float(seg["end_s"]) > start + 1e-6
            )
            if said > len(fold_tokens(quote)):
                estimate = estimate_in_segments(quote, segments, float(row["duration_s"]))
                if estimate is not None and estimate[1] - estimate[0] < end - start - 1e-6:
                    start, end = estimate
            return GroundedLine("found", str(quote), start, end)
        statuses.append(str(found.get("status") or "not_found"))
    return GroundedLine("ambiguous" if "ambiguous" in statuses else "not_found")


def tail_window(
    *,
    clip_s: float,
    voiced_s: float,
    voiced_start_s: float,
    line: tuple[float, float] | None,
) -> tuple[float, float, float] | str:
    """``(voiced source start, tail source start, tail length)`` or a reason code.

    ``clip_s`` is the clip's footage length, ``voiced_s`` how long the last step plays under
    the voice (rate 1) and ``voiced_start_s`` where it starts today. See the module docstring.
    """
    limit = clip_s - SOURCE_END_MARGIN_S
    if line is not None:
        start, end = line
        tail_end = min(limit, end + TAIL_PAD_S)
        if tail_end < end - _COVER_TOLERANCE_S:
            return LINE_PAST_END  # the footage ends before the line does
        voiced_end = max(0.0, min(start - LEAD_S, tail_end - MIN_TAIL_S))
        voiced_start = voiced_end - voiced_s
        if voiced_start < 0:
            # Not enough footage before the line for the whole voiced part: it starts at the
            # clip's start and the tail follows, which only works while the line has not begun.
            voiced_start, voiced_end = 0.0, voiced_s
            if voiced_end > start:
                return LINE_TOO_EARLY  # the voice would still be playing when the line starts
            tail_end = min(limit, max(tail_end, voiced_end + MIN_TAIL_S))
        length = tail_end - voiced_end
        if length < MIN_TAIL_S - 1e-9:
            return NO_ROOM
        if length > MAX_VOICE_TAIL_S + 1e-9:
            return LINE_TOO_LONG
        return round(voiced_start, 3), round(voiced_end, 3), round(length, 3)
    voiced_start = max(0.0, voiced_start_s)
    if limit - (voiced_start + voiced_s) < MIN_TAIL_S:
        # Shift the voiced part earlier to leave the minimum.
        voiced_start = limit - MIN_TAIL_S - voiced_s
        if voiced_start < 0:
            return NO_ROOM
    voiced_end = voiced_start + voiced_s
    length = min(MAX_PLAIN_TAIL_S, limit - voiced_end)
    return round(voiced_start, 3), round(voiced_end, 3), round(length, 3)


def _row(ask: ClipSoundAsk, media_id: str | None, **fields: Any) -> dict[str, Any]:
    return {
        "requirement_id": ask.requirement_id,
        "media_id": media_id,
        "status": NOT_POSSIBLE,
        "reason_code": None,
        **fields,
    }


def _has_sound(binding: PhoneSourceBinding | None) -> bool:
    return binding is not None and binding.original.has_audio is not False


def plan_narrated_clip_sound(
    asks: Sequence[ClipSoundAsk],
    *,
    steps: list[NarratedPhoneStep],
    bindings: Sequence[PhoneSourceBinding],
    media_snapshot: Mapping[str, Any] | None,
    target_duration_s: float | None = None,
) -> NarratedClipSoundPlan:
    """The compile changes the clip-sound ``asks`` make, and the receipt of what was built.

    ``steps`` already tile the voiceover (the last one ends where the voice does). Media are
    matched by id, never by position: a clip used in two steps is quiet-under-voice on both.
    ``target_duration_s`` is a length the creator proved (the render contract's): a tail that
    would take a video that meets it past its ten percent is not built, so the confirmed
    length never fails the whole render for a bonus sound.
    """
    by_media = {binding.media_id: binding for binding in bindings}
    step_media = list(dict.fromkeys(step.media_id for step in steps))
    under_rows: list[dict[str, Any]] = []
    not_built: list[dict[str, Any]] = []
    after_row: dict[str, Any] | None = None
    under_ids: set[str] = set()
    tail: NarratedVoiceTail | None = None
    out_steps = list(steps)

    for ask in asks:
        media, why = resolve_ask_media(ask, step_media, media_snapshot)
        if ask.mode == UNDER_VOICE:
            row = _row(ask, media, reason_code=why)
            if media is not None and not _has_sound(by_media.get(media)):
                row["reason_code"] = NO_SOUND
            elif media is not None:
                row.update(status=BUILT, reason_code=None, gain=None)
                under_ids.add(media)
            under_rows.append(row)
            continue
        if ask.mode != AFTER_VOICE:
            continue
        if after_row is not None:
            not_built.append(_row(ask, media, reason_code=ONE_TAIL_ONLY))
            continue
        after_row = _row(
            ask, media, reason_code=why, line=ask.triggers[0] if ask.triggers else None
        )
        if media is None:
            continue
        if media != steps[-1].media_id:
            after_row["reason_code"] = NOT_LAST_CLIP
            continue
        binding = by_media[media]
        if not _has_sound(binding):
            after_row["reason_code"] = NO_SOUND
            continue
        line: tuple[float, float] | None = None
        if ask.triggers:
            grounded = ground_trigger_line(
                ask.triggers,
                media,
                media_snapshot,
                duration_s=float(binding.original.duration_s),
            )
            if grounded.status != "found":
                after_row["reason_code"] = (
                    LINE_AMBIGUOUS if grounded.status == "ambiguous" else LINE_NOT_FOUND
                )
                continue
            line = (grounded.start_s, grounded.end_s)
            after_row.update(line=grounded.quote, line_start_s=line[0], line_end_s=line[1])
        last = steps[-1]
        voiced_s = float(last.end_s) - float(last.start_s)
        window = tail_window(
            clip_s=float(binding.original.duration_s),
            voiced_s=voiced_s,
            voiced_start_s=float(last.source_start_s),
            line=line,
        )
        if isinstance(window, str):
            after_row["reason_code"] = window
            continue
        voiced_start, tail_start, length = window
        voice_s = float(last.end_s)
        if (
            target_duration_s
            and creator_duration_matches(voice_s, target_duration_s)
            and not creator_duration_matches(voice_s + length, target_duration_s)
        ):
            after_row["reason_code"] = LENGTH_TARGET
            continue
        out_steps[-1] = last.model_copy(update={"source_start_s": voiced_start})
        tail = NarratedVoiceTail(media_id=media, source_start_s=tail_start, duration_s=length)
        after_row.update(
            status=BUILT,
            reason_code=None,
            source_start_s=tail_start,
            source_end_s=round(tail_start + length, 3),
            tail_start_s=round(float(last.end_s), 3),
            tail_end_s=round(float(last.end_s) + length, 3),
        )

    receipt: dict[str, Any] = {
        "version": RECEIPT_VERSION,
        "under_voice": under_rows,
        "after_voice": after_row,
        "not_built": not_built,
    }
    return NarratedClipSoundPlan(out_steps, frozenset(under_ids), tail, receipt, list(steps))


def _voice_end_s(recipe: EditRecipeV2) -> float | None:
    manifest = {asset.id: asset for asset in recipe.asset_manifest.assets}
    ends = [
        clip.timeline_start + clip.source_duration / clip.rate
        for track in recipe.tracks
        if track.kind == "audio"
        for clip in track.clips
        if getattr(manifest.get(clip.source_asset_id), "kind", None) == "voiceover"
    ]
    return max(ends, default=None)


def reconcile_clip_sound(
    recipe: EditRecipeV2, receipt: Mapping[str, Any], *, video_track_id: str = "narrated"
) -> dict[str, Any]:
    """``receipt`` re-read against the recipe actually pinned.

    A "built" row stays built only while the recipe still plays what it promises: a clip of
    that media that reaches the mix under the voice with every other clip silent
    (``under_voice``), or one that starts where the voice ends, plays at normal volume and still
    holds the whole line (``after_voice``). Anything else becomes ``not_possible`` / ``edited``
    (the creator changed the cut, or a Save rebuilt the clips), and the recorded gain and tail
    follow the clips. Rows that were never built stay as they are.
    """
    out = copy.deepcopy(dict(receipt))
    video = next((track for track in recipe.tracks if track.id == video_track_id), None)
    clips = sorted(video.clips, key=lambda clip: clip.timeline_start) if video else []
    voice_end = _voice_end_s(recipe)
    original = recipe.audio.original_volume

    def gain(clip: Any) -> float:
        return clip.volume * original

    def fail(row: dict[str, Any]) -> None:
        row.update(status=NOT_POSSIBLE, reason_code=EDITED)

    under = [
        clip
        for clip in clips
        if voice_end is None or clip.timeline_start < voice_end - _VOICE_END_TOLERANCE_S
    ]
    asked = {r.get("media_id") for r in out.get("under_voice") or [] if r.get("status") == BUILT}
    # "Only this clip's sound" holds only while every other clip is silent under the voice.
    others_silent = all(gain(clip) <= 0 for clip in under if clip.source_asset_id not in asked)
    for row in out.get("under_voice") or []:
        if row.get("status") != BUILT:
            continue
        audible = [
            gain(clip)
            for clip in under
            if clip.source_asset_id == row.get("media_id") and gain(clip) > 0
        ]
        if audible and others_silent:
            row["gain"] = round(max(audible), 4)
        else:
            fail(row)
    row = out.get("after_voice")
    if isinstance(row, dict) and row.get("status") == BUILT:
        line_start, line_end = row.get("line_start_s"), row.get("line_end_s")
        found = None
        for clip in clips:
            if (
                clip.source_asset_id != row.get("media_id")
                or voice_end is None
                or clip.timeline_start < voice_end - _VOICE_END_TOLERANCE_S
                or gain(clip) < _TAIL_MIN_GAIN
            ):
                continue
            holds_line = line_start is None or (
                clip.source_start <= line_start + _VOICE_END_TOLERANCE_S
                and clip.source_start + clip.source_duration >= line_end - _VOICE_END_TOLERANCE_S
            )
            if holds_line:
                found = clip
                break
        if found is None:
            fail(row)
        else:
            row.update(
                source_start_s=round(found.source_start, 3),
                source_end_s=round(found.source_start + found.source_duration, 3),
                tail_start_s=round(found.timeline_start, 3),
                tail_end_s=round(timeline_end_s([found]), 3),
            )
    return out


__all__ = [
    "BUILT",
    "EDITED",
    "LINE_AMBIGUOUS",
    "LINE_NOT_FOUND",
    "LENGTH_TARGET",
    "LINE_PAST_END",
    "LINE_TOO_EARLY",
    "LINE_TOO_LONG",
    "RENDER_REFUSED",
    "NOT_IN_EDIT",
    "NOT_LAST_CLIP",
    "NOT_POSSIBLE",
    "NO_ROOM",
    "NO_SOUND",
    "ONE_TAIL_ONLY",
    "GroundedLine",
    "NarratedClipSoundPlan",
    "ground_trigger_line",
    "media_aliases",
    "plan_narrated_clip_sound",
    "refused_plan",
    "reconcile_clip_sound",
    "resolve_ask_media",
    "snapshot_row",
    "tail_window",
    "video_media_ids",
]
