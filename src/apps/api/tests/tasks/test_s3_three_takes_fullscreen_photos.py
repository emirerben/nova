"""KRI-1327 (Çiğdem S3, job 7416c94b): three takes of one creator + "show the Nar, Mira, donut
and café pictures full screen when I mention them" rendered NO photo (``lanes_skipped:
multiple_speakers``) while the draft said "Done" four times.

Failure modes (written before the code):

B1  Several talking takes switch the whole lane machinery off, so a full-screen photo pinned to
    a word never reaches the recipe, although it covers the frame and needs no one speaker's
    frames.
B2  The words of the 2nd/3rd take are matched in their own clip time, so a photo named in take
    C lands inside take A (or past the end).
B3  Corner cards, the held closing shot and the generic grounded pass come back on (they DO
    need one speaker's frames/timeline) -- they stay off and the receipt names them.
B4  With the kill switch off the render is byte-identical to today (lanes skipped, no photos).
B5  The decoy Visual nobody pinned is shown.
"""

from __future__ import annotations

import uuid

import pytest

import app.services.phone_overlay_grounding as ogm
import app.services.phone_reaction_grounding as rgm
import app.services.phone_visuals as phone_visuals_mod
from app.pipeline.transcribe import Transcript, Word
from app.services.device_render import device_status
from app.tasks import generative_build as gb
from tests.tasks.test_kri841_talking_ending_clip import _POOL, _bind, _pool_rows
from tests.tasks.test_phone_subtitled_narrated_dispatch import (
    _beats_features,
    _setup_talking_multi,
)

# Take-local word timings as the S3 render heard them (prod caption cues, job 7416c94b):
# take A 20.1 s (home), take B 6.967 s (home), take C 7.134 s (car).
TAKE_A = [
    ("Like", 11.2, 11.4),
    ("NARS", 11.5, 11.9),
    ("lip", 11.9, 12.1),
    ("tint", 12.1, 12.4),
    ("Mira's", 13.6, 14.0),
    ("perfume", 14.0, 14.4),
    ("pancakes.", 19.3, 19.9),
]
TAKE_B = [
    ("And", 0.0, 0.2),
    ("food", 0.2, 0.5),
    ("whole", 5.4, 5.6),
    ("new", 5.6, 5.8),
    ("audience.", 5.8, 6.4),
]
TAKE_C = [("luxury", 1.6, 2.0), ("brands", 2.0, 2.3), ("opening", 2.3, 2.6), ("cafes.", 2.6, 3.0)]
DURATIONS = {"takeA": 20.1, "takeB": 6.967, "takeC": 7.134}

_NS = uuid.UUID("00000000-0000-4000-8000-000000001327")
NAR, MIRA, DONUT, CAFE, CALENDAR = (
    str(uuid.uuid5(_NS, n)) for n in ("nar", "mira", "donut", "cafe", "calendar")
)


def _padded(words, count, duration):
    """Enough other words for the classifier to call every take a speaker."""
    filler = [
        (f"w{i}", duration * i / (count + 1), duration * i / (count + 1) + 0.08)
        for i in range(count)
    ]
    taken = [(s, e) for _t, s, e in words]
    filler = [f for f in filler if not any(s - 0.1 <= f[1] <= e + 0.1 for s, e in taken)]
    return sorted(words + filler, key=lambda w: w[1])


def _beat(beat_id, trigger, media_id):
    return {
        "beat_id": beat_id,
        "trigger": trigger,
        "visual_id": f"asset-{media_id}",
        "occurrence": "first",
        "visual_role": "photo",
        "display_mode": "fullscreen",
    }


def _render(monkeypatch, *, enabled: bool, beats=None):
    job, _snapshot, _bindings = _setup_talking_multi(
        monkeypatch, clips=[("takeA", 20.1, 40), ("takeB", 6.967, 14), ("takeC", 7.134, 14)]
    )
    names = {
        NAR: "IMG_7138.JPG",
        MIRA: "IMG_7140.JPG",
        DONUT: "IMG_7146.JPG",
        CAFE: "IMG_7150.JPG",
        CALENDAR: "IMG_7143.PNG",
    }
    for media_id, name in names.items():
        _POOL[media_id] = {"kind": "image", "dur": None, "name": name}
    pool = list(names)
    monkeypatch.setattr(gb.settings, "phone_subtitled_media_lanes_enabled", True)
    monkeypatch.setattr(gb.settings, "media_overlays_enabled", True)
    monkeypatch.setattr(gb.settings, "sound_effects_enabled", True)
    monkeypatch.setattr(gb.settings, "phone_subtitled_reaction_beats_enabled", True)
    monkeypatch.setattr(
        gb.settings, "phone_talking_multi_speaker_beats_enabled", enabled, raising=False
    )
    monkeypatch.setattr(
        gb.settings,
        "phone_render_verified_features",
        list(dict.fromkeys([*_beats_features(), "stillImages", "visualVideos"])),
    )
    monkeypatch.setattr(phone_visuals_mod, "bind_phone_visual_assets", _bind)
    monkeypatch.setattr(ogm, "_load_ready_pool_assets", lambda *a, **k: _pool_rows(pool))
    monkeypatch.setattr(
        rgm, "_load_ready_pool_assets", lambda *a, **k: _pool_rows(pool), raising=False
    )
    import app.pipeline.transcribe as transcribe_mod

    heard = {
        "takeA": _padded(TAKE_A, 40, DURATIONS["takeA"]),
        "takeB": _padded(TAKE_B, 14, DURATIONS["takeB"]),
        "takeC": _padded(TAKE_C, 14, DURATIONS["takeC"]),
    }

    def _transcribe(path, *a, **k):
        clip_id = path.rsplit("/", 1)[-1].split(".")[0]
        return Transcript(
            words=[Word(text=t, start_s=s, end_s=e, confidence=1.0) for t, s, e in heard[clip_id]],
            language="en",
        )

    monkeypatch.setattr(transcribe_mod, "transcribe_whisper_cached", _transcribe, raising=False)
    # The S3 brief's own triggers (r3-r6), as the planner wrote them.
    job.all_candidates["creator_strategy"] = {
        "edit_format": "subtitled",
        "audio_strategy": "original_audio",
        "reaction_beats": beats
        if beats is not None
        else [
            _beat("nar", "Nar", NAR),
            _beat("mira", "Mira", MIRA),
            _beat("donut", "audience", DONUT),
            _beat("cafe", "café", CAFE),
        ],
    }
    gb._run_generative_job(str(job.id))
    assert job.status == "awaiting_device", job.error_detail
    recipe = device_status(job, "subtitled").request.recipe
    return recipe, job.assembly_plan["variants"][0]


def _fullscreen(recipe) -> dict[str, float]:
    track = next((t for t in recipe.tracks if t.id == "subtitled-fullscreen"), None)
    if track is None:
        return {}
    return {clip.source_asset_id: float(clip.timeline_start) for clip in track.clips}


def test_three_takes_of_one_creator_show_each_photo_full_screen_on_its_word(monkeypatch):
    recipe, variant = _render(monkeypatch, enabled=True)

    shown = _fullscreen(recipe)
    starts = {
        key.split("-", 1)[-1] if key.startswith("asset-") else key: at for key, at in shown.items()
    }

    def at(media_id):
        hits = [t for k, t in starts.items() if media_id in k]
        assert hits, f"{media_id} not shown full screen; shown={shown}"
        return hits[0]

    # Take A plays first (0-20.1 s): "NARS" 11.5 s, "Mira's" 13.6 s.
    assert at(NAR) == pytest.approx(11.5, abs=0.6)
    assert at(MIRA) == pytest.approx(13.6, abs=0.6)
    # Take B starts at 20.1 s: "audience" 5.8 s into it (B2).
    assert at(DONUT) == pytest.approx(20.1 + 5.8, abs=0.6)
    # Take C starts at 27.067 s: "cafes" 2.6 s into it (B2).
    assert at(CAFE) == pytest.approx(27.067 + 2.6, abs=0.6)
    assert not any(CALENDAR in key for key in shown), "the decoy is never shown (B5)"
    assert variant.get("phone_beat_receipt"), "the beat receipt is kept for the review"
    multi = variant[gb.PHONE_TALKING_MULTI_CLIP_FIELD]
    assert multi["lanes_skipped"] == "multiple_speakers", "cards / generic pass stay off (B3)"


def test_a_corner_card_still_stays_off_and_is_named(monkeypatch):
    corner = {**_beat("mira", "Mira", MIRA), "display_mode": "pip"}
    recipe, variant = _render(monkeypatch, enabled=True, beats=[_beat("nar", "Nar", NAR), corner])

    assert any(NAR in key for key in _fullscreen(recipe))
    assert not any(MIRA in clip.source_asset_id for t in recipe.tracks for clip in t.clips)
    unplaced = variant["phone_beat_receipt"]["unplaced"]
    assert any(
        row.get("beat_id") == "mira" and row.get("reason") == "multiple_speakers"
        for row in unplaced
    ), unplaced


def test_kill_switch_off_is_todays_render(monkeypatch):
    recipe, variant = _render(monkeypatch, enabled=False)

    assert "subtitled-fullscreen" not in {t.id for t in recipe.tracks}
    assert "phone_beat_receipt" not in variant
    assert variant[gb.PHONE_TALKING_MULTI_CLIP_FIELD]["lanes_skipped"] == "multiple_speakers"
