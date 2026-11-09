"""KRI-720 (G3, N7): a named line cut from take 1 of several takes does not make a full-screen
photo vanish (the first take being cut used to hold every photo back), and the photo lands on its
word's place in the CUT video."""

from __future__ import annotations

import uuid

import pytest

import app.services.phone_overlay_grounding as ogm
import app.services.phone_reaction_grounding as rgm
import app.services.phone_visuals as phone_visuals_mod
from app.kria.brief import CreativeBrief
from app.kria.brief_binding import BriefBinding
from app.services.device_render import device_status
from app.tasks import generative_build as gb
from tests.tasks.test_kri841_talking_ending_clip import _POOL, _bind, _pool_rows
from tests.tasks.test_phone_multi_take_named_line import DUR, HEARD, OKAY, _snapshot
from tests.tasks.test_phone_subtitled_narrated_dispatch import _beats_features, _setup_talking_multi


def test_a_named_cut_in_take_one_keeps_the_full_screen_photo_and_shifts_it(monkeypatch):
    photo = str(uuid.uuid5(uuid.UUID("00000000-0000-4000-8000-000000000720"), "lamp"))
    _POOL[photo] = {"kind": "image", "dur": None, "name": "IMG_0720.JPG"}
    job, _snapshot_unused, bindings = _setup_talking_multi(
        monkeypatch, clips=[(cid, DUR, 8) for cid in HEARD]
    )
    monkeypatch.setattr(gb.settings, "talking_multi_take_cleanup_enabled", True)
    monkeypatch.setattr(gb.settings, "speech_cleanup_long_pauses_enabled", False)
    monkeypatch.setattr(gb.settings, "phone_subtitled_media_lanes_enabled", True)
    monkeypatch.setattr(gb.settings, "media_overlays_enabled", True)
    monkeypatch.setattr(gb.settings, "sound_effects_enabled", True)
    monkeypatch.setattr(gb.settings, "phone_subtitled_reaction_beats_enabled", True)
    monkeypatch.setattr(
        gb.settings, "phone_talking_multi_speaker_beats_enabled", True, raising=False
    )
    monkeypatch.setattr(
        gb.settings,
        "phone_render_verified_features",
        list(dict.fromkeys([*_beats_features(), "stillImages", "visualVideos"])),
    )
    monkeypatch.setattr(phone_visuals_mod, "bind_phone_visual_assets", _bind)
    monkeypatch.setattr(ogm, "_load_ready_pool_assets", lambda *a, **k: _pool_rows([photo]))
    monkeypatch.setattr(
        rgm, "_load_ready_pool_assets", lambda *a, **k: _pool_rows([photo]), raising=False
    )
    job.assembly_plan["speech_cleanup_contract"] = "required_v1"
    job.assembly_plan["_speech_cleanup_internal"] = {
        "preflight_snapshot": _snapshot([(c, bindings[c].proxy_path, HEARD[c]) for c in HEARD])
    }
    brief = CreativeBrief(version=1, requirements=[OKAY])
    job.assembly_plan["creator_brief_binding"] = BriefBinding.create(
        uuid.uuid4(), brief
    ).model_dump(mode="json")
    job.all_candidates["creator_strategy"] = {
        "edit_format": "subtitled",
        "audio_strategy": "original_audio",
        "reaction_beats": [
            {
                "beat_id": "lamp",
                "trigger": "light",
                "visual_id": f"asset-{photo}",
                "occurrence": "first",
                "visual_role": "photo",
                "display_mode": "fullscreen",
            }
        ],
    }

    gb._run_generative_job(str(job.id))

    assert job.status == "awaiting_device", job.error_detail
    recipe = device_status(job, "subtitled").request.recipe
    (row,) = job.assembly_plan["variants"][0]["named_cuts"]
    assert row["status"] == "applied" and row["take"]["index"] == 1
    removed = row["end_s"] - row["start_s"]
    track = next((t for t in recipe.tracks if t.id == "subtitled-fullscreen"), None)
    assert track is not None, "the photo vanished"
    (clip,) = track.clips
    # "light" is said 1.5 s into take C, which starts after A's kept seconds and all of B.
    assert float(clip.timeline_start) == pytest.approx(2 * DUR - removed + 1.5, abs=0.6)
