"""Talking edit: clips the creator asks to show "when I mention them" cover him full screen at
the moment he mentions them, instead of playing back to back before his own take.

Replays prod thread 6fa36e51 / job 8cb3fc21 (2026-11-01): the creator's 58 s take, three
podium talks (clear speech, `speaks_to_camera: false`) and a drone shot of the stadium. The
brief is the thread's own (`kri1151_thread_6fa36e51.json`: r1 "Show the talks full screen when
mentioned", r2 "Use the audio of the talk clips", r3/r4 the stadium); the clips' words and
analyses are the ones the worker measured (`talking_show_when_mentioned_6fa36e51.json`). Only
transport (download, probe, Whisper) is replaced; the classifier, scheduler, compiler and
recipe validation are the real ones, driven through `_run_generative_job`.

Before the fix the three talks were classified speakers by pace of speech alone and played back
to back ahead of the creator, which also switched overlay grounding and beats off
(`lanes_skipped: multiple_speakers`).
"""

from __future__ import annotations

import json
import uuid
from pathlib import Path
from types import SimpleNamespace

import pytest

from app.kria.brief import BriefRequirement, CreativeBrief
from app.kria.brief_binding import BriefBinding
from app.pipeline.transcribe import Transcript, Word
from app.services.device_render import device_status
from app.services.phone_sources import PHONE_SOURCES_FIELD
from app.tasks import generative_build as gb
from tests.pipeline.test_phone_voiceover_montage_plan import _binding
from tests.tasks.test_phone_subtitled_narrated_dispatch import _job_and_session

_FIXTURES = Path(__file__).resolve().parents[1] / "fixtures"
THREAD = json.loads((_FIXTURES / "kri1151_thread_6fa36e51.json").read_text(encoding="utf-8"))
CLIPS = json.loads(
    (_FIXTURES / "talking_show_when_mentioned_6fa36e51.json").read_text(encoding="utf-8")
)["clips"]
BY_ROLE = {c["role"]: c for c in CLIPS}
CREATOR = BY_ROLE["creator"]["media_id"]
STADIUM = BY_ROLE["stadium"]["media_id"]
# The talks, named by what each says: the clip about the 2032 final and 75,000 seats, and the
# one offering to allocate the stadium to the club.
FINAL_TALK = next(c["media_id"] for c in CLIPS if c["clip_id"] == "files/5y0pv0icb952")
ALLOCATE_TALK = next(c["media_id"] for c in CLIPS if c["clip_id"] == "files/smba6yqy3adx")
CONGRESS_TALK = next(c["media_id"] for c in CLIPS if c["clip_id"] == "files/3d0mpvir0ez8")
TALKS = {FINAL_TALK, ALLOCATE_TALK, CONGRESS_TALK}

_FEATURES = [
    "basicComposition",
    "local1080Export",
    "positionedText",
    "animatedText",
    "narrationAudio",
    "audioMix",
    "visualBlocks",
    "visualVideos",
    "alphaOverlay",
]


def _brief(*, drop: tuple[str, ...] = (), extra: tuple[BriefRequirement, ...] = ()):
    reqs = [BriefRequirement(**r) for r in THREAD["brief"]["requirements"] if r["id"] not in drop]
    return CreativeBrief(version=2, requirements=[*reqs, *extra])


def _run(
    monkeypatch,
    *,
    mention: bool,
    sound: bool = False,
    brief: CreativeBrief | None = None,
):
    bindings = {c["media_id"]: _binding(c["media_id"], duration_s=c["duration_s"]) for c in CLIPS}
    snapshot = {
        PHONE_SOURCES_FIELD: [b.model_dump(mode="json") for b in bindings.values()],
        "creator_generation_id": "generation",
        "creator_brief_binding": BriefBinding.create(
            uuid.uuid4(), brief if brief is not None else _brief()
        ).model_dump(mode="json"),
    }
    all_candidates = {
        "clip_paths": [b.proxy_path for b in bindings.values()],
        "edit_format": "subtitled",
        "language": "tr",
    }
    job, _session = _job_and_session(
        monkeypatch, assembly_plan=snapshot, all_candidates=all_candidates
    )
    monkeypatch.setattr(gb.settings, "phone_render_verified_features", _FEATURES)
    monkeypatch.setattr(gb.settings, "talking_multi_clip_enabled", True)
    monkeypatch.setattr(gb.settings, "phone_talking_mention_cutaways_enabled", mention)
    monkeypatch.setattr(gb.settings, "phone_subtitled_media_lanes_enabled", True)
    monkeypatch.setattr(gb.settings, "phone_clip_sound_windows_enabled", sound)
    monkeypatch.setattr(gb.settings, "phone_talking_cutaway_sound_enabled", sound)
    # The worker saw the clips in the order their analyses finished, not the upload order.
    finished = [CLIPS[3], CLIPS[4], CLIPS[1], CLIPS[2], CLIPS[0]]
    by_id = {c["media_id"]: c for c in CLIPS}
    path_of = {c["clip_id"]: f"/tmp/{c['clip_id'].split('/')[1]}.mp4" for c in CLIPS}
    clip_of_path = {path: cid for cid, path in path_of.items()}
    media_of_clip = {c["clip_id"]: c["media_id"] for c in CLIPS}

    def _meta(c):
        m = c["meta"]
        return SimpleNamespace(
            clip_id=c["clip_id"],
            hook_score=m["hook_score"],
            best_moments=[],
            transcript=m["transcript"],
            detected_subject=m["detected_subject"],
            hook_text=m["hook_text"],
            speaks_to_camera=m["speaks_to_camera"],
            failed=m["failed"],
            analysis_degraded=m["analysis_degraded"],
            clip_summary=m["clip_summary"],
            clip_brands=m["clip_brands"],
            clip_props=m["clip_props"],
            clip_content_type=m["clip_content_type"],
            activity=m["activity"],
        )

    monkeypatch.setattr(
        gb,
        "_ingest_clips",
        lambda *a, **k: {
            "clip_metas": [_meta(c) for c in finished],
            "clip_id_to_gcs": {c["clip_id"]: bindings[c["media_id"]].proxy_path for c in CLIPS},
            "clip_id_to_local": path_of,
            "probe_map": {
                path_of[c["clip_id"]]: SimpleNamespace(duration_s=c["duration_s"]) for c in CLIPS
            },
            "hero": _meta(CLIPS[0]),
        },
        raising=False,
    )

    import app.pipeline.probe as probe_mod
    import app.pipeline.transcribe as transcribe_mod

    monkeypatch.setattr(
        probe_mod,
        "probe_video",
        lambda path: SimpleNamespace(
            duration_s=by_id[media_of_clip[clip_of_path[path]]]["duration_s"]
        ),
        raising=False,
    )

    def _transcribe(path, *a, **k):
        clip = by_id[media_of_clip[clip_of_path[path]]]
        return Transcript(
            words=[Word(text=t, start_s=s, end_s=e, confidence=1.0) for t, s, e in clip["words"]],
            language="tr",
        )

    monkeypatch.setattr(transcribe_mod, "transcribe_whisper_cached", _transcribe, raising=False)
    gb._run_generative_job(str(job.id))
    assert job.status == "awaiting_device", job.error_detail
    return job, device_status(job, "subtitled").request.recipe, job.assembly_plan["variants"][0]


def _tracks(recipe):
    return {track.id: track for track in recipe.tracks}


def _cutaway_windows(recipe):
    return {
        c.source_asset_id: (c.timeline_start, c.timeline_start + c.source_duration)
        for c in _tracks(recipe)["talking-head-cutaways"].clips
    }


def test_the_threads_clips_replay_as_the_prod_job_saw_them():
    assert [c["role"] for c in CLIPS] == ["creator", "talk", "talk", "talk", "stadium"]
    assert BY_ROLE["creator"]["meta"]["speaks_to_camera"] is True
    assert not any(c["meta"]["speaks_to_camera"] for c in CLIPS if c["role"] != "creator")
    assert not any(c["meta"]["failed"] or c["meta"]["analysis_degraded"] for c in CLIPS)


def test_flag_off_the_talks_still_play_back_to_back_ahead_of_the_creator(monkeypatch):
    _job, recipe, variant = _run(monkeypatch, mention=False)
    receipt = variant[gb.PHONE_TALKING_MULTI_CLIP_FIELD]
    assert len(receipt["speaker_media_ids"]) == 4
    assert receipt["lanes_skipped"] == "multiple_speakers"
    assert "mention_cutaways" not in receipt
    assert len(_tracks(recipe)["subtitled"].clips) == 4


def test_flag_on_the_creators_take_is_the_only_speaker_and_lanes_stay_on(monkeypatch):
    _job, recipe, variant = _run(monkeypatch, mention=True)
    receipt = variant[gb.PHONE_TALKING_MULTI_CLIP_FIELD]
    assert receipt["speaker_media_ids"] == [CREATOR]
    assert "lanes_skipped" not in receipt
    main = _tracks(recipe)["subtitled"].clips
    assert [c.source_asset_id for c in main] == [CREATOR]
    assert recipe.duration == pytest.approx(58.5)
    assert {row["media_id"] for row in receipt["cutaways"]} == {*TALKS, STADIUM}
    assert receipt["unplaced"] == []


def test_flag_on_each_talk_covers_the_creator_where_he_talks_about_it(monkeypatch):
    _job, recipe, variant = _run(monkeypatch, mention=True)
    windows = _cutaway_windows(recipe)
    # "statın kulübe tahsis edilmesinin teklif edildiğini" ~ 10.6-14 s: the clip offering to
    # allocate the stadium to the club.
    assert 10.0 <= windows[ALLOCATE_TALK][0] <= 12.5
    # "Bu 75.000 kişilik stat Euro2032 finali için" ~ 14.9-18.8 s: the 2032 final talk.
    assert 14.5 <= windows[FINAL_TALK][0] <= 17.0
    # No window starts before the hook is over, ends in the last second, or overlaps another.
    spans = sorted(windows.values())
    assert all(start >= 2.5 - 1e-6 and end <= 57.5 + 1e-6 for start, end in spans)
    assert all(a_end <= b_start + 1e-6 for (_, a_end), (b_start, _) in zip(spans, spans[1:]))
    assert all(end - start <= 4.0 + 1e-6 for start, end in spans)

    rows = {
        row["media_id"]: row
        for row in variant[gb.PHONE_TALKING_MULTI_CLIP_FIELD]["mention_cutaways"]["placements"]
    }
    assert rows[ALLOCATE_TALK]["basis"] == "mention"
    assert rows[FINAL_TALK]["basis"] == "mention"
    # Nothing the creator says is about the congress talk or the drone shot: spread, and said.
    assert rows[CONGRESS_TALK]["basis"] == "spread"
    assert rows[CONGRESS_TALK]["reason"] == "no_mention_found"
    assert rows[STADIUM]["basis"] == "spread"
    # The receipt's windows are the ones the device renders.
    for media_id, (start, end) in windows.items():
        assert (rows[media_id]["start_s"], rows[media_id]["end_s"]) == (
            pytest.approx(start, abs=1e-6),
            pytest.approx(end, abs=1e-6),
        )


def test_flag_on_cutaways_stay_silent_and_the_receipt_says_the_sound_was_not_applied(
    monkeypatch,
):
    _job, recipe, variant = _run(monkeypatch, mention=True, sound=False)
    assert _tracks(recipe).get("talking-head-cutaway-audio") is None
    assert recipe.audio.mute_windows == []
    sound = variant[gb.PHONE_TALKING_MULTI_CLIP_FIELD]["mention_cutaways"]["sound"]
    assert sound == {"asked": True, "applied": False, "reason": "sound_not_enabled"}


def test_flag_on_with_sound_the_talks_play_their_own_audio_and_the_stadium_keeps_his(
    monkeypatch,
):
    _job, recipe, variant = _run(monkeypatch, mention=True, sound=True)
    audio = _tracks(recipe)["talking-head-cutaway-audio"]
    assert {c.source_asset_id for c in audio.clips} == TALKS
    windows = _cutaway_windows(recipe)
    muted = {(round(w.start, 2), round(w.end, 2)) for w in recipe.audio.mute_windows}
    assert {(round(windows[m][0], 2), round(windows[m][1], 2)) for m in TALKS} == muted
    # A talk with its own sound may run longer than a silent cutaway, never over another.
    assert max(end - start for start, end in (windows[m] for m in TALKS)) <= 6.0 + 1e-6
    assert variant[gb.PHONE_TALKING_MULTI_CLIP_FIELD]["mention_cutaways"]["sound"] == {
        "asked": True,
        "applied": True,
    }


def test_flag_on_a_clip_order_ask_keeps_the_talks_back_to_back(monkeypatch):
    order = BriefRequirement(
        id="r9",
        kind="order",
        scope="global",
        status="open",
        description="Play the talks one after another, then me",
        facts={},
        source_turn_id="t1",
    )
    _job, _recipe, variant = _run(monkeypatch, mention=True, brief=_brief(extra=(order,)))
    receipt = variant[gb.PHONE_TALKING_MULTI_CLIP_FIELD]
    assert len(receipt["speaker_media_ids"]) == 4
    assert receipt["mention_cutaways"]["kept"] == "clip_order_asked"
    assert receipt["mention_cutaways"]["demoted"] == []


def test_flag_on_without_a_full_screen_ask_nothing_changes(monkeypatch):
    _job, recipe, variant = _run(monkeypatch, mention=True, brief=_brief(drop=("r1", "r3")))
    receipt = variant[gb.PHONE_TALKING_MULTI_CLIP_FIELD]
    assert len(receipt["speaker_media_ids"]) == 4
    assert "mention_cutaways" not in receipt
    assert len(_tracks(recipe)["subtitled"].clips) == 4
