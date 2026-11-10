"""Colour moments on a phone edit (KRI-1350): one op that sets the whole lane.

``set_color_moments`` replaces the stretches of the finished video drawn black-and-white /
warm / faded (each with a cut, white flash, warm light-leak or dip-to-black edge) and/or the
end fade to black. A moment may name a library sound that lands on one of its edges; that
sound is an ordinary sound-effect row starting exactly on the edge (the flash's peak frame),
so the phone mixes it like any other, and it is replaced together with the moments.

Only in ``allowed_op_families`` while `color_moments_supported` holds (kill switch, verified
token, app build) on a device edit that recompiles lane-free sections. Kria adds moments only
when the creator asks (the prompt fragment says so; failure mode 6).
"""

from __future__ import annotations

import math
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, ValidationError

from app.agents.editor_ops_v2 import OpSpec
from app.kria.portable_color_moments import (
    DEFAULT_EDGE_S,
    END_FADE_RANGE_S,
    MAX_COLOR_MOMENTS,
    SOUND_ID_PREFIX,
    ColorMoment,
    ColorMomentEdgeStyle,
    ColorMomentTone,
    validate_color_moments,
)

FAMILY = "color_moments"
CLOSED_NOTE = (
    "Black-and-white moments, flashes and the end fade are not available here yet.",
    "Siyah beyaz anlar, flaşlar ve sondaki kararma burada henüz yok.",
)
REMOVAL_ONLY_NOTE = (
    "I can only take the colour moments or the end fade off this video right now, "
    "not add or change them."
)
UNASKED_NOTE = "I only change the colours when you ask for it, so I left them as they are."
# Failure mode 6: a colour change is added only when the creator's own words ask for one. The
# model is told the same; this is the server-side check that a vague "make it cinematic" can
# never come back as black-and-white. Lower-cased substrings, English and Turkish.
ASK_WORDS = (
    "black and white",
    "black & white",
    "b&w",
    "b/w",
    "monochrome",
    "mono",
    "grayscale",
    "greyscale",
    "grey",
    "gray",
    "colour",
    "color",
    "tone",
    "flash",
    "light leak",
    "fade",
    "dip to black",
    "warm",
    "faded",
    "sepia",
    "old film",
    "siyah",
    "beyaz",
    "renk",
    "flaş",
    "flas",
    "karar",
    "sıcak",
    "soluk",
    "solgun",
)
# Sound rows this lane owns carry `SOUND_ID_PREFIX` (portable_color_moments), so a later
# `set_color_moments` replaces them with the moments and never touches a sound the creator
# placed by hand.


class _MomentCommand(BaseModel):
    model_config = ConfigDict(extra="forbid", allow_inf_nan=False)
    start_s: float = Field(ge=0, le=1800)
    end_s: float = Field(gt=0, le=1800)
    tone: ColorMomentTone = "mono"
    enter: ColorMomentEdgeStyle = "cut"
    exit: ColorMomentEdgeStyle = "cut"
    sound: str | None = Field(default=None, min_length=1, max_length=200)
    sound_at: str = Field(default="enter", pattern="^(enter|exit)$")


class _SetCommand(BaseModel):
    model_config = ConfigDict(extra="forbid", allow_inf_nan=False)
    moments: list[_MomentCommand] | None = Field(default=None, max_length=MAX_COLOR_MOMENTS)
    end_fade_s: float | None = Field(default=None, ge=0, le=END_FADE_RANGE_S[1])


def _note(state: Any, text: str) -> None:
    notes = getattr(state, "reply_notes", None)
    if isinstance(notes, list) and text not in notes:
        notes.append(text)


def _coerce(name: str, payload: dict, snapshot: dict, state: Any) -> dict | None:
    lane = snapshot.get("color_moments")
    if FAMILY not in (snapshot.get("allowed_op_families") or []) or not isinstance(lane, dict):
        state.reject(op=name, reason="capability_unavailable", detail=CLOSED_NOTE[0])
        _note(state, CLOSED_NOTE[0])
        return None
    utterance = str(getattr(state, "utterance", "") or "").casefold()
    if utterance and not any(word in utterance for word in ASK_WORDS):
        state.reject(op=name, reason="not_requested", detail=UNASKED_NOTE)
        _note(state, UNASKED_NOTE)
        return None
    try:
        command = _SetCommand.model_validate(payload)
        if command.moments is None and command.end_fade_s is None:
            raise ValueError("nothing to set")
        if lane.get("removal_only") and (command.moments or command.end_fade_s):
            # Gate closed but the edit still carries moments: removing is the only change
            # (`moments: []`, `end_fade_s: 0`); anything new stays refused (KRI-1350 review).
            state.reject(op=name, reason="capability_unavailable", detail=REMOVAL_ONLY_NOTE)
            _note(state, REMOVAL_ONLY_NOTE)
            return None
        duration = float(lane.get("duration_s") or 0)
        clean: dict[str, Any] = {}
        if command.moments is not None:
            moments = sorted(command.moments, key=lambda m: m.start_s)
            models = [
                ColorMoment(
                    id=f"cm{index + 1}",
                    start=round(m.start_s, 3),
                    end=round(min(m.end_s, duration) if duration else m.end_s, 3),
                    tone=m.tone,
                    enter={"style": m.enter, "duration": DEFAULT_EDGE_S},
                    exit={"style": m.exit, "duration": DEFAULT_EDGE_S},
                )
                for index, m in enumerate(moments)
            ]
            validate_color_moments(models, duration or math.inf)
            catalog = {
                row.get("id")
                for row in ((snapshot.get("sfx") or {}).get("catalog") or [])
                if isinstance(row, dict)
            }
            sfx_open = "sfx" in (snapshot.get("allowed_op_families") or [])
            rows = []
            for model, m in zip(models, moments):
                row = model.model_dump(mode="json")
                if m.sound is not None:
                    if not sfx_open:
                        _note(
                            state,
                            "I can't add sound effects to this edit, so the moment is silent.",
                        )
                    elif m.sound not in catalog:
                        raise ValueError("unknown sound effect")
                    else:
                        row["sound"] = {"effect_id": m.sound, "at": m.sound_at}
                rows.append(row)
            clean["moments"] = rows
        if command.end_fade_s is not None:
            if command.end_fade_s and not (
                END_FADE_RANGE_S[0] <= command.end_fade_s <= (duration or END_FADE_RANGE_S[1])
            ):
                raise ValueError("end fade out of range")
            clean["end_fade_s"] = round(command.end_fade_s, 3)
        return clean
    except (ValidationError, ValueError, TypeError):
        state.invalid_value(
            "Colour moments need a start before the end, at least half a second each, at most "
            f"{MAX_COLOR_MOMENTS}, not overlapping and inside the video."
        )
        return None


SPECS = [
    OpSpec(
        name="set_color_moments",
        required=frozenset(),
        fields=frozenset({"moments", "end_fade_s"}),
        family=frozenset({FAMILY}),
        coerce=_coerce,
        closed_note=CLOSED_NOTE,
    )
]


def current_lane(variant: dict[str, Any]) -> dict[str, Any]:
    stored = variant.get("color_moments")
    if not isinstance(stored, dict):
        return {"moments": [], "end_fade": None}
    return {
        "moments": [dict(m) for m in stored.get("moments") or [] if isinstance(m, dict)],
        "end_fade": dict(stored["end_fade"]) if isinstance(stored.get("end_fade"), dict) else None,
    }


def _op_set_color_moments(state: Any, op: dict[str, Any]) -> None:
    from app.services.kria_editor_ops import KriaEditorOpError  # noqa: PLC0415

    working = (
        state.color_moments
        if "color_moments" in state.changed and state.color_moments is not None
        else current_lane(state.variant)
    )
    document = {"moments": list(working.get("moments") or []), "end_fade": working.get("end_fade")}
    sounds: list[dict[str, Any]] | None = None
    if "moments" in op:
        sounds = []
        document["moments"] = []
        for row in op["moments"]:
            sound = row.get("sound")
            moment = {key: value for key, value in row.items() if key != "sound"}
            document["moments"].append(moment)
            if sound:
                at = moment["start"] if sound.get("at") == "enter" else moment["end"]
                sounds.append(
                    {
                        "id": f"{SOUND_ID_PREFIX}{moment['id']}-{sound.get('at', 'enter')}",
                        "sound_effect_id": sound["effect_id"],
                        "src_gcs_path": "",
                        "at_s": round(float(at), 3),
                        "gain": 1.0,
                        "trim_start_s": None,
                        "trim_end_s": None,
                        "label": None,
                        "source": "user",
                    }
                )
    if "end_fade_s" in op:
        document["end_fade"] = {"duration": op["end_fade_s"]} if op["end_fade_s"] else None
    if len(document["moments"]) > MAX_COLOR_MOMENTS:
        raise KriaEditorOpError(f"At most {MAX_COLOR_MOMENTS} colour moments")
    state.color_moments = document
    state.changed.add("color_moments")
    if sounds is not None:
        kept = [
            row
            for row in state.sound_effects
            if not str(row.get("id") or "").startswith(SOUND_ID_PREFIX)
        ]
        if sounds or len(kept) != len(state.sound_effects):
            state.sound_effects[:] = kept + sounds
            state.changed.add("sound_effects")
    tones = {"mono": "black-and-white", "warm": "warm", "faded": "faded"}
    parts = []
    if "moments" in op:
        parts.append(
            ", ".join(
                f"{tones[m['tone']]} {m['start']:g}-{m['end']:g}s" for m in document["moments"]
            )
            or "no colour moments"
        )
    if "end_fade_s" in op:
        parts.append(f"fade to black {op['end_fade_s']:g}s" if op["end_fade_s"] else "no end fade")
    state.summary = "Colour: " + "; ".join(parts)


def register_handlers() -> None:
    from app.services import kria_editor_ops  # noqa: PLC0415

    kria_editor_ops.register_handler("set_color_moments", _op_set_color_moments)
