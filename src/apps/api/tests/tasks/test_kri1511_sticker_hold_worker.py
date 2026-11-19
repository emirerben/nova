"""KRI-1511 through the phone Talking worker: a held sticker keeps its time on screen when the
accepted Clean up speech plan cuts a pause inside its window.

The pipeline replay of the Cusco T3 job lives in
`tests/pipeline/test_kri1511_cut_keeps_sticker_hold.py`; this pins the wiring the replay cannot
see: `_run_phone_subtitled_job` hands the grounding's `held_card_ids` to `remap_lanes_for_cut`
(and only while `CUT_KEEPS_STICKER_HOLD_ENABLED` is on), and the device recipe the phone
renders carries the held window.

Ways the wiring can go wrong: W1 the held ids never reach the remap (the sticker still shrinks);
W2 the switch off is not today's window; W3 a card the grounding did not hold is extended.

The cut is the proven `required_v1` fixture of `test_phone_subtitled_narrated_dispatch` (6.5 s
take, removals 0.88-1.42 and 2.5-4.4, cut take 4.06 s). The sticker is on the take at 1.5-4.0 s
(2.5 s): the plain remap ends it at the 2.5 s removal edge (0.96-1.96 s, 1.0 s on screen); held,
it runs 0.96-3.46 s.
"""

from __future__ import annotations

from unittest.mock import Mock

import pytest

import app.services.phone_overlay_grounding as phone_overlay_grounding_mod
import app.services.phone_reaction_grounding as phone_reaction_grounding_mod
import app.services.phone_visuals as phone_visuals_mod
from app.config import settings
from app.pipeline.phone_subtitled_lanes import SubtitledOverlayCard
from app.services.device_render import device_status
from app.services.phone_reaction_grounding import GroundedReactionBeats
from app.tasks import generative_build as gb
from tests.tasks.test_phone_subtitled_narrated_dispatch import (
    DURATION,
    _basic_beat_receipt,
    _beat_dict,
    _enable_beats,
    _fake_resolve_sfx,
    _grounding_mock,
    _make_fake_bind,
    _raw_preflight_snapshot,
    _setup_subtitled,
)


def _sticker(card_id: str, start_s: float, end_s: float) -> SubtitledOverlayCard:
    return SubtitledOverlayCard(
        id=card_id,
        media_id=card_id.removeprefix("beat-").removesuffix("-1"),
        gcs_path=f"users/u1/plan/item1/pool/{card_id}.png",
        generation="1",
        start_s=start_s,
        end_s=end_s,
        x_frac=0.26,
        y_frac=0.24,
        scale=0.28,
        fade=True,
        z=1,
    )


def _run(monkeypatch, *, held: frozenset[str]) -> dict[str, tuple[float, float]]:  # noqa: ANN001
    job, snapshot, _session, binding = _setup_subtitled(monkeypatch, duration_s=DURATION)
    snapshot["speech_cleanup_contract"] = "required_v1"
    snapshot["_speech_cleanup_internal"] = {
        "preflight_snapshot": _raw_preflight_snapshot(storage_path=binding.proxy_path)
    }
    _enable_beats(monkeypatch)
    monkeypatch.setattr(phone_visuals_mod, "bind_phone_visual_assets", _make_fake_bind([]))
    monkeypatch.setattr(gb, "_resolve_phone_sound_effect", _fake_resolve_sfx)
    monkeypatch.setattr("app.services.pipeline_trace.record_pipeline_event", Mock())
    cards = [_sticker("beat-puma-1", 1.5, 4.0), _sticker("beat-badge-1", 1.5, 4.0)]
    receipt = _basic_beat_receipt(
        placed=[
            {"beat_id": "puma", "trigger": "puma", "at_s": 1.5, "end_s": 4.0},
            {"beat_id": "badge", "trigger": "puma", "at_s": 1.5, "end_s": 4.0},
        ]
    )
    monkeypatch.setattr(
        phone_reaction_grounding_mod,
        "ground_phone_reaction_beats",
        Mock(
            return_value=GroundedReactionBeats(
                cards=cards, sound_effects=[], receipt=receipt, held_card_ids=held
            )
        ),
    )
    monkeypatch.setattr(
        phone_overlay_grounding_mod, "ground_phone_subtitled_overlays", _grounding_mock([])
    )
    job.all_candidates["creator_strategy"] = {
        "reaction_beats": [
            _beat_dict("puma", "puma", visual_id="puma"),
            _beat_dict("badge", "puma", visual_id="badge"),
        ]
    }

    gb._run_phone_subtitled_job(str(job.id), snapshot, job.all_candidates, ownership_epoch=3)

    assert job.status == "awaiting_device"
    assert job.assembly_plan["variants"][0]["silence_cut_outcome"] == "applied"
    recipe = device_status(job, "subtitled").request.recipe
    assert recipe.duration == pytest.approx(DURATION - (0.54 + 1.9), abs=1e-3)
    return {
        clip.id.removeprefix("subtitled-overlay-"): (
            round(clip.timeline_start, 2),
            round(clip.timeline_start + clip.source_duration / clip.rate, 2),
        )
        for track in recipe.tracks
        for clip in track.clips
        if clip.id.startswith("subtitled-overlay-beat-")
    }


def test_w1_w3_the_held_sticker_keeps_its_hold_and_the_other_card_does_not(monkeypatch):
    spans = _run(monkeypatch, held=frozenset({"beat-puma-1"}))
    assert spans == {"beat-puma-1": (0.96, 3.46), "beat-badge-1": (0.96, 1.96)}


def test_w2_the_switch_off_keeps_todays_shorter_window(monkeypatch):
    monkeypatch.setattr(settings, "cut_keeps_sticker_hold_enabled", False)
    spans = _run(monkeypatch, held=frozenset({"beat-puma-1"}))
    assert spans == {"beat-puma-1": (0.96, 1.96), "beat-badge-1": (0.96, 1.96)}
