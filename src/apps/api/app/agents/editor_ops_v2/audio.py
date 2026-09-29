"""Editor ops v2, audio lane (KRI-219 Lane C).

* ``set_mix`` is EXTENDED (not replaced): ``music_level`` keeps its exact
  behaviour, ``original_level`` (device-rendered variants only) and
  ``music_gain_db`` (the smart background bed) are added.
* ``add_sfx`` gets a real compile handler (the parser branch already existed
  and fails closed while ``snapshot.sfx.catalog`` is empty).

Do not import app.agents.edit_copilot / app.services.kria_editor_ops at module
top level (circular); import lazily inside functions.
"""

from __future__ import annotations

import math
import uuid
from typing import Any

from app.agents.editor_ops_v2 import OpSpec

# Extends the existing set_mix op: fields are unioned into edit_copilot's
# tables; the "at least one of" rule lives in `validate_set_mix` because a
# frozenset cannot express it (edit_copilot makes set_mix's required set empty).
SPECS: list[OpSpec] = [
    OpSpec(
        name="set_mix",
        fields=frozenset({"music_level", "original_level", "music_gain_db"}),
    ),
]

MIX_FIELDS = ("music_level", "original_level", "music_gain_db")
MIN_GAIN_DB = -40.0
MAX_GAIN_DB = 0.0
SFX_GAIN_RANGE = (0.0, 2.0)


def _num(value: object) -> float | None:
    if isinstance(value, bool):
        return None
    try:
        number = float(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return None
    return number if math.isfinite(number) else None


def has_mix_field(payload: dict) -> bool:
    return any(key in payload for key in MIX_FIELDS)


def validate_set_mix(out: dict, snapshot: dict, state: Any) -> dict | None:
    """Validate/clamp the v2 set_mix fields in place. None drops the op.

    ``music_level`` is left to the legacy branch (byte-identical behaviour).
    """
    mix = snapshot.get("mix") if isinstance(snapshot, dict) else None
    mix = mix if isinstance(mix, dict) else {}
    if "original_level" in out:
        level = _num(out["original_level"])
        if level is None:
            state.invalid_value()
            return None
        if snapshot.get("render_destination") != "device":
            # The server stores mix.original_level but its renderer ignores it
            # ("not yet honored", generative_jobs.EditorCommitMix); only the
            # phone compiler applies it. Say so instead of a silent no-op.
            state.reject(
                op="set_mix",
                reason="capability_unavailable",
                detail="I can only change original audio on phone-rendered edits",
            )
            return None
        out["original_level"] = max(0.0, min(1.0, level))
    if "music_gain_db" in out:
        gain = _num(out["music_gain_db"])
        if gain is None:
            state.invalid_value()
            return None
        bed = mix.get("background_music")
        if not isinstance(bed, dict) or not bed.get("track_id"):
            state.reject(
                op="set_mix",
                reason="capability_unavailable",
                detail="this edit has no adjustable background music bed",
            )
            return None
        out["music_gain_db"] = max(MIN_GAIN_DB, min(MAX_GAIN_DB, gain))
    return out


def _op_set_mix(state: Any, op: dict[str, Any]) -> None:
    from app.services.kria_editor_ops import KriaEditorOpError  # noqa: PLC0415

    if not has_mix_field(op):
        raise KriaEditorOpError("Mix change needs a level")
    if op.get("music_level") is not None:
        state.mix_level = float(op["music_level"])
    if op.get("original_level") is not None:
        if state.variant.get("render_destination") != "device":
            raise KriaEditorOpError("Original audio can only be changed on phone-rendered edits")
        state.original_level = max(0.0, min(1.0, float(op["original_level"])))
    if op.get("music_gain_db") is not None:
        treatment = state.variant.get("smart_music_treatment")
        track_id = treatment.get("track_id") if isinstance(treatment, dict) else None
        if not track_id:
            raise KriaEditorOpError("There is no background music bed to adjust")
        state.music_gain_db = max(MIN_GAIN_DB, min(MAX_GAIN_DB, float(op["music_gain_db"])))
        state.background_track_id = str(track_id)
    state.changed.add("mix")


def _op_add_sfx(state: Any, op: dict[str, Any]) -> None:
    from app.routes.generative_jobs import visual_block_variant_duration  # noqa: PLC0415
    from app.services.kria_editor_ops import KriaEditorOpError  # noqa: PLC0415

    effect_id = op.get("effect_id")
    if not isinstance(effect_id, str) or not effect_id.strip():
        raise KriaEditorOpError("Sound effect needs a catalog id")
    at_s = _num(op.get("at_s"))
    if at_s is None or at_s < 0:
        raise KriaEditorOpError("Sound effect time is invalid")
    total_s = visual_block_variant_duration(state.variant)
    if total_s and total_s > 0 and at_s > total_s:
        raise KriaEditorOpError("Sound effect time is past the end of the video")
    gain = _num(op.get("gain", 1.0))
    if gain is None or not SFX_GAIN_RANGE[0] <= gain <= SFX_GAIN_RANGE[1]:
        raise KriaEditorOpError("Sound effect volume is out of range")
    # Same wire shape the iOS editor sends (NativeEditorSession.addSoundEffect):
    # the commit route resolves src_gcs_path/label/duration from the catalog id.
    state.sound_effects.append(
        {
            "id": uuid.uuid4().hex,
            "sound_effect_id": effect_id.strip(),
            "src_gcs_path": "",
            "at_s": round(at_s, 3),
            "gain": round(gain, 3),
            "trim_start_s": None,
            "trim_end_s": None,
            "label": None,
            "source": "user",
        }
    )
    state.changed.add("sound_effects")


def register_handlers() -> None:
    from app.services import kria_editor_ops  # noqa: PLC0415

    kria_editor_ops.register_handler("set_mix", _op_set_mix, replace=True)
    kria_editor_ops.register_handler("add_sfx", _op_add_sfx)
