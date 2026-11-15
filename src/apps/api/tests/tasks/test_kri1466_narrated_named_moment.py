"""KRI-1466: "show the TOR on the big screen on "Tor"" replayed through the phone Voiceover job.

Kria TikTok video 1 (job c8fcb7c0, thread 78787267, 2026-11-14, build 1996 / protocol 51).
The goal clip came on screen on the word (9.76 s) from its first frame and, being the last
clip, was slowed to half speed over the remaining 15.4 s of voice. The creator's review of the
source and of the rendered file: the big screen reads TOR at about 3.2-4.7 s of the clip, and
in the video it showed at about 16.0-17.6 s, 7 s after the word. The review then said "When
you say Tor, clip 4 of 4 is on screen (9.8-25.2 s)." and nothing else.

``tests/fixtures/narrated_alignment/kri1466_tor_moment.json`` says what is recorded (clip
lengths, this render's analysed moments, the steps, the creator's observation) and what is
reconstructed (word times, the alignment agent's answer).

The expectation is the creator's own observation of the source (3.2-4.7 s), never the analysis
the fix reads (its moment is 2.76-4.5 s): `_shown_at` asks the compiled recipe when that
stretch of the goal clip is on screen.

Ways this can fail, written before the code (`tests/pipeline/test_narrated_footage_fit.py`
pins the fit itself, P1-P13):

 J1 the recipe still opens the goal clip on its first frame, or slows it (the incident).
 J2 the replay is not the incident: with the fix off the recipe is not the recorded one (four
    clips, the goal clip from 0.0 over 9.76-25.18 s, TOR at about 16 s), so a pass proves
    nothing.
 J3 wrong footage passes: the TOR moment is taken from a clip that does not show it (a moment
    another clip's analysis recorded), or a clip whose analysis records no such moment is cut
    somewhere anyway.
 J4 the record lies: a filler row carries the creator's label or a first / last seat, so the
    order and first / last receipts read it as the shot the creator named; or the goal row
    does not say where the moment plays.
 J5 the cut the editor shows (`narrated_timings` / `narrated_clip_assignments`) and the
    recipe disagree, or the steps stop tiling the voiceover and the compile refuses.
 J6 an edit that follows a filming guide, or a clip the creator asked to end on, gets
    fillers; a clip that should play on after the voice (KRI-842) is no longer last.
 J7 the kill switch does not restore the old plan.
 J8 the review still reads as fine when the named moment plays late, or blames a clip whose
    moment is on screen on the word.
"""

from __future__ import annotations

import json
import uuid
from pathlib import Path
from types import SimpleNamespace

import pytest

from app.kria.brief import BriefRequirement, CreativeBrief
from app.kria.brief_binding import BriefBinding
from app.pipeline.phone_narrated_plan import NarratedPhoneStep
from app.services.device_render import device_status
from app.services.phone_sources import PHONE_SOURCES_FIELD
from app.tasks import generative_build as gb
from app.tasks import kria_runtime
from tests.pipeline.test_phone_voiceover_montage_plan import _binding
from tests.tasks import test_phone_subtitled_narrated_dispatch as nd

CASE = json.loads(
    (
        Path(__file__).parent.parent / "fixtures/narrated_alignment/kri1466_tor_moment.json"
    ).read_text(encoding="utf-8")
)
CLIPS = CASE["clips"]
GOAL = CASE["observed"]["clip_id"]
WORD_AT = CASE["observed"]["word_start_s"]
TOR_SEEN = (CASE["observed"]["tor_source_start_s"], CASE["observed"]["tor_source_end_s"])
VOICE_END = CASE["voiceover_s"]


def _metas(moments_by_clip: dict[str, list[dict]] | None = None) -> list[SimpleNamespace]:
    return [
        SimpleNamespace(
            clip_id=clip["clip_id"],
            hook_score=5.0,
            hook_text="",
            transcript="",
            clip_summary=clip["summary"],
            activity=clip["activity"],
            detected_subject=clip["subject"],
            setting=clip["setting"],
            best_moments=(moments_by_clip or {}).get(clip["clip_id"], clip["best_moments"]),
        )
        for clip in CLIPS
    ]


def _setup(monkeypatch, *, fit: bool = True, intents=None, metas=None, extra=None):
    """The recorded job: four clips of their real lengths, the recorded steps, one voice."""
    job, snapshot, _session, _bindings = nd._setup_narrated(
        monkeypatch,
        n_clips=len(CLIPS),
        spoken_words=nd._words(*[(t, s, e) for t, s, e in CASE["words"]]),
        voiceover_s=VOICE_END,
        metas=metas or _metas(),
        extra_candidates={
            "creator_request": CASE["creator_request"],
            "creator_strategy": {
                "resolved_clip_intents": (
                    CASE["resolved_clip_intents"] if intents is None else intents
                )
            },
            "language": "de",
            **(extra or {}),
        },
        real_phrases=True,
        alignment=True,
    )
    # Each clip's own measured length (the harness gives every clip the same one).
    sources = [
        _binding(clip["clip_id"], duration_s=clip["duration_s"]).model_dump(mode="json")
        for clip in CLIPS
    ]
    snapshot[PHONE_SOURCES_FIELD] = sources
    job.assembly_plan[PHONE_SOURCES_FIELD] = [dict(source) for source in sources]
    monkeypatch.setattr(gb.settings, "narrated_footage_fit_enabled", fit)
    # A slowed clip needs the phone's variable-speed renderer (verified in production).
    monkeypatch.setattr(
        gb.settings,
        "phone_render_verified_features",
        [*gb.settings.phone_render_verified_features, "variableSpeed"],
    )
    nd._mock_alignment_agent(
        monkeypatch, CASE["alignment"]["order"], CASE["alignment"]["start_words"]
    )
    return job, snapshot


def _run(job, snapshot):
    gb._run_phone_narrated_job(str(job.id), snapshot, job.all_candidates, ownership_epoch=3)
    assert job.status == "awaiting_device"
    recipe = device_status(job, "narrated").request.recipe
    video = next(track for track in recipe.tracks if track.kind == "video")
    return recipe, list(video.clips)


def _shown_at(clips, media: str, source_s: float) -> float | None:
    """The first second of the video at which ``source_s`` of ``media``'s footage is on screen."""
    for clip in clips:
        if media not in clip.source_asset_id:
            continue
        if clip.source_start <= source_s < clip.source_start + clip.source_duration:
            return clip.timeline_start + (source_s - clip.source_start) / clip.rate
    return None


def _rows(job) -> list[dict]:
    return job.assembly_plan["narrated_alignment"]["steps"]


# -- J2: the replay is the incident ---------------------------------------------------------


def test_j2_j7_with_the_fix_off_the_recipe_is_the_recorded_one(monkeypatch) -> None:
    job, snapshot = _setup(monkeypatch, fit=False)

    _recipe, clips = _run(job, snapshot)

    # The recorded plan block: four clips, the goal clip last, from 0.0, over 15.4 s.
    assert [(round(c.timeline_start, 2)) for c in clips] == [0.0, 6.76, 8.26, 9.76]
    goal = clips[-1]
    assert GOAL in goal.source_asset_id
    assert goal.source_start == 0.0
    assert goal.source_duration == pytest.approx(7.653)  # the recorded source_end_s
    assert goal.rate == pytest.approx(7.653 / (VOICE_END - 9.76), abs=1e-3)  # 0.496
    # The creator saw TOR at about 16.0-17.6 s of the video.
    shown = _shown_at(clips, GOAL, TOR_SEEN[0])
    assert shown == pytest.approx(CASE["observed"]["incident_shown_at_s"], abs=0.3)
    assert shown - WORD_AT > 6
    # J7: the record is the recorded one too, key for key.
    recorded_ids = {clip["recorded_media_id"]: clip["clip_id"] for clip in CLIPS}
    assert _rows(job) == [
        {
            **row,
            "media_id": recorded_ids[row["media_id"]],
            "clip": f"analysis-proxy-{recorded_ids[row['media_id']]}.mp4",
        }
        for row in CASE["steps"]
    ]


# -- J1 / J5: the goal clip opens on TOR, at normal speed -----------------------------------


def test_j1_j5_the_tor_screen_is_on_screen_when_the_creator_says_tor(monkeypatch) -> None:
    job, snapshot = _setup(monkeypatch)

    recipe, clips = _run(job, snapshot)

    # J1: the stretch the creator saw TOR in is on screen within a second of the word ...
    shown = _shown_at(clips, GOAL, TOR_SEEN[0])
    assert shown is not None and 0 <= shown - WORD_AT <= 1.0, shown
    # ... the goal clip still comes on screen on the word ...
    goal = next(c for c in clips if GOAL in c.source_asset_id and c.timeline_start >= 9.0)
    assert goal.timeline_start == pytest.approx(WORD_AT)
    # ... and nothing in the video is slowed.
    assert [c.rate for c in clips] == [1.0] * len(clips)
    # The three clips before it are as recorded.
    assert [round(c.timeline_start, 2) for c in clips[:4]] == [0.0, 6.76, 8.26, 9.76]
    assert [c.source_start for c in clips[:3]] == [0.0, 0.0, 0.0]
    # J5: the clips tile the whole voiceover, and no clip follows itself.
    cursor = 0.0
    for clip in clips:
        assert clip.timeline_start == pytest.approx(cursor, abs=1e-3)
        cursor += clip.source_duration / clip.rate
    assert cursor == pytest.approx(VOICE_END, abs=1e-3)
    assert recipe.duration == pytest.approx(VOICE_END, abs=1e-3)
    for before, after in zip(clips, clips[1:]):
        assert before.source_asset_id != after.source_asset_id
    # The time the goal clip no longer fills shows footage nobody had seen.
    after_goal = clips[4:]
    assert len(after_goal) >= 2
    assert all(GOAL not in c.source_asset_id for c in after_goal)
    # J5: the cut the editor shows is this recipe's.
    variant = job.assembly_plan["variants"][0]
    assert [(t["start_s"], t["end_s"]) for t in variant["narrated_timings"]] == [
        (round(c.timeline_start, 3), round(c.timeline_start + c.source_duration / c.rate, 3))
        for c in clips
    ]
    assert len(variant["narrated_clip_assignments"]) == len(clips)


# -- J3: wrong footage ----------------------------------------------------------------------


def test_j3_a_moment_another_clip_recorded_never_moves_the_goal_clip(monkeypatch) -> None:
    """The wrong-content control: same clips, lengths, order and count, but the goal clip's
    analysis records no TOR moment (it sits on the crowd clip's record instead). Nothing may
    be cut to it: the goal clip starts at its first frame, and the creator's TOR stretch is
    not on screen on the word, so the check that passes above must fail here."""
    by_id = {clip["clip_id"]: clip["best_moments"] for clip in CLIPS}
    swapped = {**by_id, GOAL: by_id["c3"], "c3": by_id[GOAL]}
    job, snapshot = _setup(monkeypatch, metas=_metas(swapped))

    _recipe, clips = _run(job, snapshot)

    goal = next(c for c in clips if GOAL in c.source_asset_id and c.timeline_start >= 9.0)
    assert goal.source_start == 0.0
    shown = _shown_at(clips, GOAL, TOR_SEEN[0])
    assert shown is None or shown - WORD_AT > 1.0
    # The crowd clip has no creator words, so its (swapped) TOR moment moves nothing either.
    crowd = next(c for c in clips if "c3" in c.source_asset_id)
    assert crowd.source_start == 0.0
    assert all("moment" not in row for row in _rows(job))


def test_j3_words_that_fit_the_whole_clip_locate_nothing(monkeypatch) -> None:
    """ "The train" is the train clip from start to end (all three analysed moments say
    "train"), and nothing in the light-show clip's record says "light show": both start
    where they always did."""
    job, snapshot = _setup(monkeypatch)

    _recipe, clips = _run(job, snapshot)

    assert clips[0].source_start == 0.0 and clips[2].source_start == 0.0
    assert [row["step_id"] for row in _rows(job) if "moment" in row] == ["seg_3"]


def test_j3_the_chats_own_record_of_the_same_moment_is_not_a_second_vote(monkeypatch) -> None:
    """Review finding: the chat's stored clip record often describes the same moment a few
    frames apart ("TOR on the scoreboard", 3.0-4.6 s). Read together with this render's
    analysis the two tie, and a tie locates nothing, so the render's own analysis is read
    alone; the stored record only serves a clip the render has no moments for."""
    job, snapshot = _setup(monkeypatch)
    steps = [
        NarratedPhoneStep(step_id=f"seg_{i}", media_id=media, start_s=start, end_s=end)
        for i, (media, start, end) in enumerate(
            [("c0", 0.0, 6.76), ("c3", 6.76, 8.26), ("c1", 8.26, 9.76), ("c2", 9.76, VOICE_END)]
        )
    ]
    stored = {"best_moments": [{"start_s": 3.0, "end_s": 4.6, "description": "TOR on a display"}]}

    def fit(metas):
        return gb._narrated_footage_fit(
            steps,
            step_clip_ids=["c0", "c3", "c1", "c2"],
            bindings=[_binding(c["clip_id"], duration_s=c["duration_s"]) for c in CLIPS],
            clip_metas=metas,
            strategy=job.all_candidates["creator_strategy"],
            media_snapshot={"clip_assignments": [{"media_id": "c2", "analysis": stored}]},
            all_candidates=job.all_candidates,
            clip_id_to_gcs={c["clip_id"]: f"user/analysis-proxy-{c['clip_id']}.mp4" for c in CLIPS},
            media_id_for_clip=lambda clip_id: clip_id,
            brief=None,
            job_id=str(job.id),
        )

    both = fit(_metas())
    assert (both.moments["seg_3"].start_s, both.moments["seg_3"].end_s) == (2.76, 4.5)
    # A render with no moments for the goal clip falls back to the stored record.
    only_stored = fit(_metas({GOAL: []}))
    assert (only_stored.moments["seg_3"].start_s, only_stored.moments["seg_3"].end_s) == (3.0, 4.6)


# -- J4: the record --------------------------------------------------------------------------


def test_j4_the_record_marks_fillers_and_says_where_the_moment_plays(monkeypatch) -> None:
    job, snapshot = _setup(monkeypatch)

    _run(job, snapshot)

    rows = _rows(job)
    named = [row for row in rows if not row.get("fill")]
    fillers = [row for row in rows if row.get("fill")]
    # The four named shots are the recorded ones, in the recorded order, with their labels.
    assert [(r["step_id"], r["labels"]) for r in named] == [
        (r["step_id"], r["labels"]) for r in CASE["steps"]
    ]
    assert [r["media_id"] for r in named] == ["c0", "c3", "c1", "c2"]
    assert [r["start_s"] for r in named] == [0.0, 6.76, 8.26, 9.76]
    # A filler is nobody's named shot.
    assert fillers
    assert all(row["labels"] == [] and row["placed"] == [] for row in fillers)
    assert all(row["step_id"].startswith("seg_3_fill") for row in fillers)
    # The goal row says where the clip starts and when the named moment plays.
    goal = named[-1]
    assert goal["source_start_s"] == pytest.approx(2.76)
    assert goal["moment"] == {
        "words": "the TOR on the big screen",
        "source_start_s": 2.76,
        "source_end_s": 4.5,
        "at_s": pytest.approx(WORD_AT),
    }
    # The rows tile the voiceover, and every spoken word is under exactly one of them.
    assert rows[0]["start_s"] == 0.0 and rows[-1]["end_s"] == pytest.approx(VOICE_END)
    for before, after in zip(rows, rows[1:]):
        assert before["end_s"] == after["start_s"]
    spoken = " ".join(row["text"] for row in rows if row["text"])
    assert spoken == " ".join(word for word, _s, _e in CASE["words"])


# -- J6: asks the fit must leave alone -------------------------------------------------------


def test_j6_a_clip_the_creator_ends_on_keeps_the_rest_of_the_voice(monkeypatch) -> None:
    intents = [
        *CASE["resolved_clip_intents"],
        {
            "op": "order",
            "status": "resolved",
            "position": "last",
            "attribute": "the goal",
            "assignments": [{"media_id": "analysis-proxy-c2.mp4", "confidence": 1.0}],
        },
    ]
    job, snapshot = _setup(monkeypatch, intents=intents)

    _recipe, clips = _run(job, snapshot)

    assert len(clips) == 4 and GOAL in clips[-1].source_asset_id
    assert clips[-1].timeline_start + clips[-1].source_duration / clips[-1].rate == pytest.approx(
        VOICE_END, abs=1e-3
    )
    assert not [row for row in _rows(job) if row.get("fill")]


def test_j6_an_edit_that_follows_a_filming_guide_is_left_alone(monkeypatch) -> None:
    guide = [{"shot_id": f"s{i}", "what": clip["summary"]} for i, clip in enumerate(CLIPS)]
    job, snapshot = _setup(monkeypatch, extra={"filming_guide": guide[:1]})
    monkeypatch.setattr(gb, "_resolve_narrative_order", lambda *a, **k: ["c0", "c3", "c1", "c2"])

    _recipe, clips = _run(job, snapshot)

    assert len(clips) == 4
    assert [c.source_start for c in clips] == [0.0] * 4
    assert all("fill" not in row and "moment" not in row for row in _rows(job))


# -- J8: the review ---------------------------------------------------------------------------

TOR_ASK = BriefRequirement(
    id="r7",
    kind="style",
    scope="global",
    description='Show the TOR on the big screen when I say "Tor".',
    facts={"triggers": ["Tor"]},
)
TRAIN_ASK = BriefRequirement(
    id="r4",
    kind="style",
    scope="global",
    description='Show the train when I say "Zug".',
    facts={"triggers": ["Zug"]},
)


def _bind(monkeypatch, job, snapshot, *requirements: BriefRequirement) -> BriefBinding:
    """The approved brief, on a phone that can show pop-ins (so a spoken cue is judged)."""
    import app.services.phone_reaction_grounding as grounding  # noqa: PLC0415
    import app.services.phone_visuals as phone_visuals  # noqa: PLC0415

    nd._enable_beats(monkeypatch)
    monkeypatch.setattr(gb.settings, "phone_voiceover_editor_lanes_enabled", True)
    monkeypatch.setattr(gb.settings, "phone_voiceover_reaction_beats_enabled", True)
    monkeypatch.setattr(gb.settings, "phone_subtitled_fullscreen_overlays_enabled", True)
    monkeypatch.setattr(
        grounding,
        "_load_ready_pool_assets",
        lambda *a, **k: [
            nd._pool_asset("stadium-exterior", "IMG_0990.HEIC"),
            nd._pool_asset("kria-card", "kria_logo.PNG"),
        ],
    )
    monkeypatch.setattr(grounding, "_load_sfx_entries", lambda *a, **k: [])
    monkeypatch.setattr(phone_visuals, "bind_phone_visual_assets", nd._make_fake_bind([]))
    monkeypatch.setattr(
        gb.settings,
        "phone_render_verified_features",
        [*gb.settings.phone_render_verified_features, "variableSpeed"],
    )
    binding = BriefBinding.create(
        uuid.uuid4(), CreativeBrief(version=3, requirements=list(requirements))
    )
    snapshot["creator_brief_binding"] = binding.model_dump(mode="json")
    job.assembly_plan["creator_brief_binding"] = snapshot["creator_brief_binding"]
    job.all_candidates["creator_strategy"] = {
        **job.all_candidates["creator_strategy"],
        "edit_format": "narrated_planned",
        "audio_strategy": "voiceover",
        # The recorded plan's two pop-ins (the stadium photo on "Stadion", the Kria card on
        # "Die App"): with a pop-in lane, a spoken cue with none is judged as footage.
        "reaction_beats": [
            {
                "beat_id": beat_id,
                "trigger": trigger,
                "visual_id": f"asset-{beat_id}",
                "visual_role": "photo",
                "display_mode": "fullscreen",
            }
            for beat_id, trigger in (("stadium-exterior", "Stadion"), ("kria-card", "Die App"))
        ],
    }
    return binding


def _review(job, binding) -> dict[str, dict]:
    [variant] = job.assembly_plan["variants"]
    thread = SimpleNamespace(id=uuid.UUID(binding.thread_id), creator_id=uuid.uuid4())
    _text, payload = kria_runtime._approved_generation_review(
        None, thread, job, variant, SimpleNamespace(result={}), "Ready."
    )
    return {row["requirement_id"]: row for row in payload}


def test_j8_with_the_fix_off_the_review_is_the_recorded_neutral_line(monkeypatch) -> None:
    job, snapshot = _setup(monkeypatch, fit=False)
    binding = _bind(monkeypatch, job, snapshot, TRAIN_ASK, TOR_ASK)
    _run(job, snapshot)

    rows = _review(job, binding)

    # The 21:54 review, word for word: fine-sounding, while TOR played 6 s after the word.
    assert rows["r7"]["reason"] == "When you say Tor, clip 4 of 4 is on screen (9.8–25.2 s)."
    assert rows["r4"]["reason"] == "When you say Zug, clip 1 of 4 is on screen (0.0–6.8 s)."
    assert rows["r7"]["status"] == "partial"


def test_j8_a_moment_on_the_word_is_not_blamed(monkeypatch) -> None:
    job, snapshot = _setup(monkeypatch)
    binding = _bind(monkeypatch, job, snapshot, TRAIN_ASK, TOR_ASK)
    _run(job, snapshot)

    rows = _review(job, binding)

    assert rows["r7"]["reason"] == "When you say Tor, clip 4 of 8 is on screen (9.8–14.7 s)."
    assert rows["r4"]["reason"] == "When you say Zug, clip 1 of 8 is on screen (0.0–6.8 s)."


def test_j8_a_moment_that_plays_late_is_said_so(monkeypatch) -> None:
    """The creator ends on the goal clip, so nothing may follow it: it is slowed over the
    rest of the voice as before, TOR plays late, and now the review says when."""
    job, snapshot = _setup(monkeypatch)
    ends_on_it = BriefRequirement(
        id="r13",
        kind="order",
        scope="global",
        description="End on the goal.",
        facts={"last_clip": "the TOR on the big screen"},
    )
    binding = _bind(monkeypatch, job, snapshot, TRAIN_ASK, TOR_ASK, ends_on_it)
    _recipe, clips = _run(job, snapshot)

    rows = _review(job, binding)

    assert len(clips) == 4 and clips[-1].source_start == 0.0
    shown = _shown_at(clips, GOAL, 2.76)
    assert rows["r7"]["reason"] == (
        "When you say Tor, clip 4 of 4 is on screen (9.8–25.2 s), "
        f"but the part you named comes at {shown:.1f} s."
    )
    assert rows["r7"]["status"] == "partial"
    # Another ask's cue on a clip with no located moment keeps its plain line.
    assert rows["r4"]["reason"] == "When you say Zug, clip 1 of 4 is on screen (0.0–6.8 s)."
    # The clip the creator ends on is still the last one.
    assert rows["r13"]["status"] == "met", rows["r13"]
