"""Which footage cutaways of a Talking edit play their own sound, and did the recipe (KRI-1151).

"Show the talks full screen and use their audio" on a Talking edit: the speaker's clip stays the
spine, the footage clips cover it full screen (`phone_talking_cutaways`), and a cutaway the
creator turned the sound on for plays its own file while the speaker is muted under it
(`phone_subtitled_plan.PhoneCutaway.sound`). This module is the creator-ask half, PURE (no I/O,
no settings, no model): `plan_cutaway_sound` reads the brief's on/off asks (the same typed facts
as the montage lane, `app.kria.clip_sound_toggle_ask`) and decides which cutaway clips play;
`judge_cutaway_sound` reads the COMPILED recipe and says what it really plays.

Resolution follows the montage lane's order (`app.pipeline.clip_sound_toggle`): whole-video asks
paint first in brief order, then asks naming a clip; the last paint on a clip decides it. A
cutaway plays nothing unless an "on" ask paints it. What a cutaway can express is a whole
clip, so an "on" ask for seconds, a spoken line, "the first/last clip" or with exceptions is
not built and says so (`cutaway_only`). A "sound off" ask for the speaker or the whole video is
not this module's: it builds nothing and records no row.

The record (`variant["clip_sound_toggles"]`) keeps the montage lane's row shape so
`brief_checks._check_sound_toggle` words it unchanged.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from typing import Any

from app.kria.clip_sound_toggle_ask import SOUND_OFF, SOUND_ON, SoundToggleAsk

RECEIPT_VERSION = 1

BUILT = "built"
MET = "met"
PARTIAL = "partial"
NOT_POSSIBLE = "not_possible"

CONTRACT_CONFLICT = "contract_conflict"
CUTAWAY_ONLY = "cutaway_only"
NOT_IN_EDIT = "not_in_edit"
NOT_BUILT = "not_built"
NO_SOUND = "no_sound"
OVERRIDDEN = "overridden"

_EPS = 0.06  # the engine's 25 ms ramp at a mute edge plus rounding

Interval = tuple[float, float]


@dataclass(frozen=True)
class CutawaySoundPlan:
    """The cutaway clips that play their own sound, and one row per ask this lane owns."""

    on_media_ids: frozenset[str]
    rows: tuple[dict[str, Any], ...]


def _expressible(ask: SoundToggleAsk) -> bool:
    target = ask.target
    return not (
        ask.exceptions
        or target.clip_position is not None
        or target.has_window
        or target.lines
        or target.kept_line is not None
    )


def _media_match(ref: str, media_ids: Sequence[str]) -> str | None:
    from app.pipeline.clip_sound_toggle import _media_match as match  # noqa: PLC0415

    return match(ref, media_ids)


def plan_cutaway_sound(
    asks: Iterable[SoundToggleAsk],
    *,
    cutaway_media_ids: Sequence[str],
    speaker_media_ids: Sequence[str],
) -> CutawaySoundPlan:
    ordered = list(asks)
    order = [a for a in ordered if not a.targeted] + [a for a in ordered if a.targeted]
    cutaways = list(dict.fromkeys(cutaway_media_ids))
    speakers = list(dict.fromkeys(speaker_media_ids))
    state: dict[str, str] = {}
    rows: dict[str, dict[str, Any]] = {}
    for ask in order:
        row: dict[str, Any] = {
            "requirement_id": ask.requirement_id,
            "state": ask.state,
            "status": BUILT,
            "reason_code": None,
            "media_ids": [],
            "named": ask.target.media_ref is not None,
        }
        target = ask.target
        if target.media_ref is None:
            if ask.state == SOUND_OFF:
                continue
            if not _expressible(ask):
                rows[ask.requirement_id] = {
                    **row,
                    "status": NOT_POSSIBLE,
                    "reason_code": CUTAWAY_ONLY,
                }
                continue
            hit = list(cutaways)
        else:
            named = _media_match(target.media_ref, [*cutaways, *speakers])
            if named is None:
                if ask.state == SOUND_ON or _expressible(ask):
                    rows[ask.requirement_id] = {
                        **row,
                        "status": NOT_POSSIBLE,
                        "reason_code": NOT_IN_EDIT,
                    }
                continue
            if named in speakers:
                if ask.state == SOUND_ON and _expressible(ask):
                    rows[ask.requirement_id] = row
                continue
            if not _expressible(ask):
                if ask.state == SOUND_ON:
                    rows[ask.requirement_id] = {
                        **row,
                        "status": NOT_POSSIBLE,
                        "reason_code": CUTAWAY_ONLY,
                    }
                continue
            hit = [named]
        for media_id in hit:
            state[media_id] = ask.state
        rows[ask.requirement_id] = {**row, "media_ids": hit}
    owned: dict[str, str] = {}
    for ask in order:
        row = rows.get(ask.requirement_id)
        if row is None or row["status"] != BUILT:
            continue
        for media_id in row["media_ids"]:
            owned[media_id] = ask.requirement_id
    for rid, row in rows.items():
        claimed = row["media_ids"]
        row["media_ids"] = [m for m in claimed if owned.get(m) == rid]
        if claimed and not row["media_ids"]:
            row.update(status=NOT_POSSIBLE, reason_code=OVERRIDDEN)
    on = frozenset(m for m, s in state.items() if s == SOUND_ON)
    return CutawaySoundPlan(
        on_media_ids=on,
        rows=tuple(rows[a.requirement_id] for a in ordered if a.requirement_id in rows),
    )


def sound_breaks_contract(
    contract: Any, speaker_bindings: Iterable[Any], cutaways: Iterable[Any]
) -> bool:
    """True when the cutaways' sound would change which camera sources the approved contract
    lets play (`original_audio` forbid, or an `audio_source_ids` list that must stay exact)."""
    if contract is None:
        return False
    audible = {b.media_id for b in speaker_bindings if b.original.has_audio is not False}
    audible |= {
        c.binding.media_id
        for c in cutaways
        if c.sound and c.binding.original.has_audio is not False
    }
    if contract.original_audio == "forbid" and audible:
        return True
    return bool(contract.audio_source_ids) and audible != set(contract.audio_source_ids)


def without_sound(plan: CutawaySoundPlan, reason_code: str) -> CutawaySoundPlan:
    """``plan`` with no cutaway playing; its built "on" asks become ``reason_code``."""
    rows = tuple(
        {**row, "status": NOT_POSSIBLE, "reason_code": reason_code}
        if row["state"] == SOUND_ON and row["status"] == BUILT
        else row
        for row in plan.rows
    )
    return CutawaySoundPlan(on_media_ids=frozenset(), rows=rows)


def _merge(spans: Iterable[Interval]) -> list[Interval]:
    out: list[Interval] = []
    for start, end in sorted(spans):
        if out and start <= out[-1][1] + 1e-6:
            out[-1] = (out[-1][0], max(out[-1][1], end))
        else:
            out.append((start, end))
    return out


def _covered(window: Interval, spans: Iterable[Interval]) -> bool:
    return any(s <= window[0] + _EPS and e >= window[1] - _EPS for s, e in _merge(spans))


def _recipe_tracks(recipe: Any) -> tuple[Any, Any]:
    from app.pipeline.phone_subtitled_plan import (  # noqa: PLC0415
        CUTAWAY_AUDIO_TRACK_ID,
        CUTAWAY_TRACK_ID,
    )

    picture = next((t for t in recipe.tracks if t.id == CUTAWAY_TRACK_ID), None)
    sound = next((t for t in recipe.tracks if t.id == CUTAWAY_AUDIO_TRACK_ID), None)
    return picture, sound


def _sound_plays(window: Interval, media_id: str, recipe: Any, sound: Any) -> tuple[bool, bool]:
    """``(the cutaway's own file plays across window, the speaker is silent across it)``."""
    plays = [
        (
            float(c.timeline_start),
            float(c.timeline_start) + float(c.source_duration) / float(c.rate),
        )
        for c in (sound.clips if sound is not None else [])
        if c.source_asset_id == media_id and c.volume > 0
    ]
    muted = [(float(w.start), float(w.end)) for w in recipe.audio.mute_windows]
    return _covered(window, plays), _covered(window, muted)


def judge_cutaway_sound(
    plan: CutawaySoundPlan, recipe: Any, *, silent_media_ids: Iterable[str] = ()
) -> dict[str, Any]:
    """Each row of ``plan`` judged against what ``recipe`` really plays (not what was planned)."""
    picture, sound = _recipe_tracks(recipe)
    silent = set(silent_media_ids)
    windows: dict[str, list[Interval]] = {}
    for clip in picture.clips if picture is not None else []:
        start = float(clip.timeline_start)
        windows.setdefault(clip.source_asset_id, []).append(
            (start, start + float(clip.source_duration) / float(clip.rate))
        )
    rows = []
    for row in plan.rows:
        out = {
            "requirement_id": row["requirement_id"],
            "state": row["state"],
            "status": row["status"],
            "reason_code": row["reason_code"],
            "on_windows": [],
            "off_windows": [],
            "line": None,
            "mismatch_s": 0.0,
        }
        if row["status"] == BUILT:
            placed = [m for m in row["media_ids"] if m in windows]
            if row["named"] and row["media_ids"] and not placed:
                out.update(status=NOT_POSSIBLE, reason_code=NOT_IN_EDIT)
            else:
                ok: list[Interval] = []
                half: list[Interval] = []
                bad: list[Interval] = []
                for media_id in placed:
                    for window in windows[media_id]:
                        plays, muted = _sound_plays(window, media_id, recipe, sound)
                        if row["state"] == SOUND_ON:
                            bucket = ok if plays and muted else half if plays else bad
                        else:
                            bucket = bad if plays else ok
                        bucket.append(window)
                key = "on_windows" if row["state"] == SOUND_ON else "off_windows"
                out[key] = [[round(a, 2), round(b, 2)] for a, b in _merge(ok)]
                out["mismatch_s"] = round(sum(e - s for s, e in [*half, *bad]), 3)
                if not (bad or half):
                    out.update(status=MET, reason_code=None)
                else:
                    code = NO_SOUND if silent.intersection(placed) else NOT_BUILT
                    out.update(status=PARTIAL if ok or half else NOT_POSSIBLE, reason_code=code)
        elif row["status"] == NOT_POSSIBLE:
            out["status"] = NOT_POSSIBLE
        rows.append(out)
    return {"version": RECEIPT_VERSION, "rows": rows, "windows": True}


def rendered_cutaway_receipt(
    asks: Iterable[SoundToggleAsk], recipe: Any, *, silent_media_ids: Iterable[str] = ()
) -> dict[str, Any] | None:
    """The asks judged against a pinned Talking recipe that has footage cutaways, or None.

    The cutaway and speaker clips are read off the recipe itself (an editor Save included),
    never from the worker's plan, so the receipt follows what the phone renders now.
    """
    picture, _sound = _recipe_tracks(recipe)
    if picture is None:
        return None
    speakers = [c.source_asset_id for t in recipe.tracks if t.kind == "video" for c in t.clips]
    plan = plan_cutaway_sound(
        asks,
        cutaway_media_ids=[c.source_asset_id for c in picture.clips],
        speaker_media_ids=speakers,
    )
    return judge_cutaway_sound(plan, recipe, silent_media_ids=silent_media_ids)


def plan_cutaway_sound_for_snapshot(
    snapshot: Mapping[str, Any],
    *,
    cutaway_media_ids: Sequence[str],
    speaker_media_ids: Sequence[str],
) -> CutawaySoundPlan:
    """`plan_cutaway_sound` over the approved brief pinned on ``snapshot`` (empty without one)."""
    from app.kria.clip_sound_toggle_ask import brief_sound_toggle_asks  # noqa: PLC0415
    from app.services.phone_clip_sound_toggle import _binding_brief  # noqa: PLC0415

    brief, _media_snapshot = _binding_brief(snapshot)
    return plan_cutaway_sound(
        brief_sound_toggle_asks(brief),
        cutaway_media_ids=cutaway_media_ids,
        speaker_media_ids=speaker_media_ids,
    )


__all__ = [
    "CUTAWAY_ONLY",
    "CutawaySoundPlan",
    "judge_cutaway_sound",
    "plan_cutaway_sound",
    "sound_breaks_contract",
    "without_sound",
    "rendered_cutaway_receipt",
    "plan_cutaway_sound_for_snapshot",
]
