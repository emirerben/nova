"""KRI-962: two look-alike clips on one named cue of a phone Voiceover edit, end to end.

Don Julio walk-in N1 (prod thread 99627FBC, job bdb0c798, 2026-10-26): "the DON JULIO sign
while I say 'Don Julio'" resolved to the window sign seen from the street AND to Atakan at the
brass plaque talking to the camera, both at confidence 0.9. ``_narrated_creator_clip_labels``
handed both clips the same ``creator_label``, the alignment agent opened the video on Atakan
(silent under the voice) and put the real sign on the unrelated last line "One question before
we eat.". The other five cues (queue, butcher, grill, flowers, plaques) landed right.

Drives ``_run_phone_narrated_job`` with that run's words, clip descriptions, analyzer facts,
resolved intents and the agent's own placements (fixture ``kri962_don_julio_n1_look_alike.json``;
the agent is mocked, so these tests pin the deterministic code around it).

Ways the fix could misfire (each has a test below):

FM1  It moves one of the five beats that were right (queue, butcher, grill, flowers, plaques).
FM2  It overrules the resolver: a talking-to-camera clip the resolver rated HIGHER for the cue
     is demoted anyway.
FM3  It moves a clip whose seat is the creator's: a clip-sound ask plays its own sound (an
     after-voice clip must stay last), or the whole order is locked by a filming guide.
FM4  Unknown analysis (no meta, a failed or degraded analysis) is read as "nobody talks", so a
     clip we know nothing about takes the cue.
FM5  It treats clips the creator wants together under one name (a group / caption ask) as
     alternatives and splits them.
FM6  Two equal look-alikes it cannot rank stay a silent guess when they end up apart; or the
     note fires when they sit side by side around the same words.
FM7  The note is English in a Turkish chat.
FM8  The demoted clip is dropped from the edit instead of being placed elsewhere.
FM9  The fix reads the creator's words (KRI-667): it may only read the resolver's confidence,
     the analyzer's speaks-to-camera / content type, the brief's clip-sound facts and pins.
     (Structural: ``app.pipeline.narrated_look_alikes`` takes no creator text; the KRI-667
     regex/membership ratchet covers it.)
FM10 A swap gives a clip a window longer than its footage: the phone must slow it, which it
     refuses without ``variableSpeed`` (the whole render would fail), and below half speed
     it looks broken. Such a swap is not made, and the note says so.
FM11 The tie-break raises and costs the approved video. It fails open: the agent's order and
     labels are used exactly as before.
"""

from __future__ import annotations

import copy
import json
import uuid
from pathlib import Path
from types import SimpleNamespace

import pytest

import app.tasks.generative_build as gb
from app.kria.brief import BriefRequirement, CreativeBrief
from app.kria.brief_binding import BriefBinding
from app.services.phone_sources import PHONE_SOURCES_FIELD
from tests.pipeline.test_phone_voiceover_montage_plan import _binding
from tests.tasks.test_phone_subtitled_narrated_dispatch import (
    _capture_pipeline_events,
    _mock_alignment_agent,
    _setup_narrated,
    _words,
)

FIX = json.loads(
    (Path(__file__).parents[1] / "fixtures" / "kri962_don_julio_n1_look_alike.json").read_text(
        "utf-8"
    )
)
MEDIA = [clip["media_id"] for clip in FIX["clips"]]
CLIP_OF = {media_id: f"c{i}" for i, media_id in enumerate(MEDIA)}
ATAKAN = "analysis-proxy-ios-6163F36C-EE16-4B4A-BC23-D2B67D7F1263.mp4"
SIGN = "analysis-proxy-ios-B39B8BA0-9AE0-4BC0-82D7-3B2881098E96.mp4"
SIGN_LABEL = "the DON JULIO sign"
PROD_ORDER = [p["media_id"] for p in FIX["agent_placements"]]
PROD_STARTS = [p["start_word"] for p in FIX["agent_placements"]]
PROD_STEPS = FIX["prod_steps"]


def _metas(**overrides: dict) -> list[SimpleNamespace]:
    """The render-time analyzer facts, per clip; ``overrides`` maps a media id to fields."""
    metas = []
    for i, clip in enumerate(FIX["clips"]):
        fields = {
            "clip_id": f"c{i}",
            "hook_score": 5.0,
            "hook_text": "",
            "transcript": "",
            "best_moments": [],
            "clip_summary": clip["clip_summary"],
            "activity": clip["activity"],
            "detected_subject": clip["detected_subject"],
            "setting": clip["setting"],
            "speaks_to_camera": clip["speaks_to_camera"],
            "clip_content_type": clip["content_type"],
            **overrides.get(clip["media_id"], {}),
        }
        metas.append(SimpleNamespace(**fields))
    return metas


def _intents(**confidence: float) -> list[dict]:
    """The approved resolved intents; ``confidence`` re-rates the sign ask per media id."""
    intents = copy.deepcopy(FIX["resolved_clip_intents"])
    for intent in intents:
        if intent["attribute"] == SIGN_LABEL:
            for assignment in intent["assignments"]:
                if assignment["media_id"] in confidence:
                    assignment["confidence"] = confidence[assignment["media_id"]]
    return intents


def setup_n1(
    monkeypatch,
    *,
    intents=None,
    metas=None,
    order=PROD_ORDER,
    starts=PROD_STARTS,
    extra_candidates=None,
    requirements=None,
    reply_language=None,
    retimes=True,
):
    """The N1 job. ``retimes``: the phone can slow a short clip (``variableSpeed`` verified, as
    in prod on 2026-10-25); off, a swap may only give a clip a window its footage fills."""
    job, snapshot, _session, _ = _setup_narrated(
        monkeypatch,
        edit_format="narrated_planned",
        n_clips=len(MEDIA),
        spoken_words=_words(*[tuple(word) for word in FIX["words"]]),
        voiceover_s=PROD_STEPS[-1]["end_s"],
        real_phrases=True,
        alignment=True,
        extra_candidates={
            "creator_strategy": {
                "resolved_clip_intents": intents if intents is not None else _intents()
            },
            **(extra_candidates or {}),
        },
    )
    bindings = tuple(
        _binding(clip["media_id"], duration_s=clip["duration_s"]) for clip in FIX["clips"]
    )
    snapshot[PHONE_SOURCES_FIELD] = [b.model_dump(mode="json") for b in bindings]
    job.assembly_plan[PHONE_SOURCES_FIELD] = snapshot[PHONE_SOURCES_FIELD]
    if retimes:
        monkeypatch.setattr(
            gb.settings,
            "phone_render_verified_features",
            [*gb.settings.phone_render_verified_features, "variableSpeed"],
        )
    clip_metas = metas if metas is not None else _metas()
    monkeypatch.setattr(
        gb,
        "_ingest_clips",
        lambda *a, **k: {
            "clip_metas": clip_metas,
            "clip_id_to_gcs": {f"c{i}": b.proxy_path for i, b in enumerate(bindings)},
            "clip_id_to_local": {f"c{i}": f"/tmp/c{i}.mp4" for i in range(len(bindings))},
            "probe_map": {},
            "hero": clip_metas[0],
        },
        raising=False,
    )
    seen: list = []
    _mock_alignment_agent(monkeypatch, [CLIP_OF[m] for m in order], list(starts), seen=seen)
    if requirements:
        binding = BriefBinding.create(
            uuid.uuid4(),
            CreativeBrief(version=2, requirements=list(requirements)),
            media_snapshot={
                "clip_assignments": [
                    {
                        "media_id": clip["media_id"],
                        "kind": "video",
                        "gcs_path": f"user/analysis-proxy-{clip['media_id']}.mp4",
                        "duration_s": clip["duration_s"],
                    }
                    for clip in FIX["clips"]
                ]
            },
        )
        snapshot["creator_brief_binding"] = binding.model_dump(mode="json")
        job.assembly_plan["creator_brief_binding"] = snapshot["creator_brief_binding"]
    if reply_language:
        snapshot["creator_reply_language"] = reply_language
    return job, snapshot, seen


def run(job, snapshot):
    gb._run_phone_narrated_job(str(job.id), snapshot, job.all_candidates, ownership_epoch=3)
    assert job.status == "awaiting_device"
    [variant] = job.assembly_plan["variants"]
    return job.assembly_plan["narrated_alignment"]["steps"], variant


def labels_seen(seen) -> dict[str, str]:
    (agent_input,) = seen
    return {MEDIA[int(c.clip_id[1:])]: c.creator_label for c in agent_input.clips}


def _assert_five_beats_kept(steps):
    for got, prod in zip(steps[1:6], PROD_STEPS[1:6], strict=True):
        assert (got["step_id"], got["media_id"]) == (prod["step_id"], prod["media_id"])
        assert got["start_s"] == pytest.approx(prod["start_s"], abs=1e-3)
        assert got["end_s"] == pytest.approx(prod["end_s"], abs=1e-3)
        assert got["text"] == prod["text"]


def test_n1_opens_on_the_sign_when_the_voice_says_don_julio(monkeypatch):
    """The prod placements (Atakan first, the sign last) open on the sign now (FM1, FM8)."""
    job, snapshot, _seen = setup_n1(monkeypatch)
    events = _capture_pipeline_events(monkeypatch)

    steps, variant = run(job, snapshot)

    assert steps[0]["media_id"] == SIGN
    assert steps[0]["text"].startswith("This is Don Julio")
    assert steps[0]["labels"] == [SIGN_LABEL]
    # FM8: Atakan stays in the edit, in the seat the agent had given the sign.
    assert steps[6]["media_id"] == ATAKAN
    assert steps[6]["start_s"] == pytest.approx(PROD_STEPS[6]["start_s"], abs=1e-3)
    # FM1: the five beats that were right keep their clip, window and words.
    _assert_five_beats_kept(steps)
    assert len({step["media_id"] for step in steps}) == len(MEDIA)
    # A ranked tie is not a guess: no note for it.
    assert not variant.get("phone_repair_notes")
    (event,) = [e for e in events if e[1] == "narrated_look_alikes"]
    assert event[2]["sets"] == [{"labels": [SIGN_LABEL], "clip_ids": ["c0", "c6"]}]
    assert event[2]["demoted"] == ["c0"]
    assert event[2]["swaps"] == [
        {"labels": [SIGN_LABEL], "seat": 0, "lead": "c6", "moved": "c0", "moved_to_seat": 6}
    ]
    assert event[2]["blocked"] == [] and event[2]["ties"] == []


def test_a_swap_the_phone_could_not_play_is_said_not_made(monkeypatch):
    """FM10: the 4.0 s sign clip needs a slight slow-down for the 4.15 s opening window; where
    the phone does not retime, the swap is not made (the render still succeeds) and the note
    says why the sign is not first."""
    job, snapshot, _seen = setup_n1(monkeypatch, retimes=False)

    steps, variant = run(job, snapshot)

    assert [s["media_id"] for s in steps] == [s["media_id"] for s in PROD_STEPS]
    assert variant["phone_repair_notes"] == [
        'Clip 7 looks most like "the DON JULIO sign", but it is too short to fill clip 1\'s '
        "place, so clip 1 comes first."
    ]


def test_n1_names_the_sign_on_one_clip_in_the_alignment_input(monkeypatch):
    """The agent is told which clip the cue is: only the sign carries its label (FM1)."""
    job, snapshot, seen = setup_n1(monkeypatch)

    run(job, snapshot)

    labels = labels_seen(seen)
    assert labels[SIGN] == SIGN_LABEL
    assert labels[ATAKAN] == ""
    # Every other clip is shown exactly what prod showed it.
    for clip in FIX["clips"]:
        if clip["media_id"] not in (SIGN, ATAKAN):
            assert labels[clip["media_id"]] == clip["alignment_creator_label"]


def test_a_talking_head_the_resolver_rated_higher_keeps_the_cue(monkeypatch):
    """FM2: the resolver's confidence outranks the talking-to-camera rule."""
    job, snapshot, seen = setup_n1(monkeypatch, intents=_intents(**{SIGN: 0.8}))
    events = _capture_pipeline_events(monkeypatch)

    steps, variant = run(job, snapshot)

    assert labels_seen(seen)[ATAKAN] == SIGN_LABEL
    assert [s["media_id"] for s in steps] == [s["media_id"] for s in PROD_STEPS]
    assert not variant.get("phone_repair_notes")
    (event,) = [e for e in events if e[1] == "narrated_look_alikes"]
    assert event[2]["demoted"] == [] and event[2]["swaps"] == []


@pytest.mark.parametrize("mode", ["under_voice", "after_voice"])
def test_a_talking_head_with_its_own_sound_is_never_moved(monkeypatch, mode):
    """FM3: a clip-sound ask plays Atakan's own sound, so his seat is not the tie-break's."""
    facts = {"clip_sound": mode}
    if mode == "after_voice":
        facts["triggers"] = ["Super excited"]
    requirement = BriefRequirement(
        id="r5",
        kind="audio",
        scope=f"clip:{ATAKAN}",
        description="play Atakan's 'Super excited!' with its own sound",
        facts=facts,
    )
    job, snapshot, seen = setup_n1(monkeypatch, requirements=[requirement])

    steps, _variant = run(job, snapshot)

    assert labels_seen(seen)[ATAKAN] == SIGN_LABEL
    assert [s["media_id"] for s in steps] == [s["media_id"] for s in PROD_STEPS]


def test_a_filming_guide_order_is_never_reseated(monkeypatch):
    """FM3: a locked order keeps the listed clips where they are."""
    job, snapshot, seen = setup_n1(
        monkeypatch,
        order=MEDIA,
        starts=PROD_STARTS,
        extra_candidates={"narrative_shot_count": len(MEDIA)},
    )

    steps, _variant = run(job, snapshot)

    assert seen[0].order_locked is True
    assert labels_seen(seen)[ATAKAN] == SIGN_LABEL
    assert [s["media_id"] for s in steps] == MEDIA


@pytest.mark.parametrize(
    "unknown",
    [{"failed": True}, {"analysis_degraded": True}],
    ids=["failed", "degraded"],
)
def test_unknown_analysis_never_takes_the_cue(monkeypatch, unknown):
    """FM4: a sign clip whose analysis failed is not "nobody talks"; nothing is reseated."""
    job, snapshot, seen = setup_n1(monkeypatch, metas=_metas(**{SIGN: unknown}))

    steps, variant = run(job, snapshot)

    assert labels_seen(seen)[ATAKAN] == SIGN_LABEL
    assert steps[0]["media_id"] == ATAKAN
    # Nothing could rank them and they ended up apart: say so (FM6).
    assert variant["phone_repair_notes"] == [
        'Clips 1 and 7 both look like "the DON JULIO sign", and I couldn\'t tell which one '
        "you meant, so clip 1 comes first."
    ]


@pytest.mark.parametrize("op", ["group", "caption"])
def test_clips_named_together_are_not_look_alikes(monkeypatch, op):
    """FM5: a group / caption ask names clips that belong together, never alternatives."""
    intents = _intents()
    for intent in intents:
        if intent["attribute"] == SIGN_LABEL:
            intent["op"] = op
    job, snapshot, seen = setup_n1(monkeypatch, intents=intents)
    events = _capture_pipeline_events(monkeypatch)

    steps, variant = run(job, snapshot)

    assert labels_seen(seen)[ATAKAN] == SIGN_LABEL
    assert [s["media_id"] for s in steps] == [s["media_id"] for s in PROD_STEPS]
    assert not variant.get("phone_repair_notes")
    assert not [e for e in events if e[1] == "narrated_look_alikes"]


def test_two_equal_look_alikes_apart_on_screen_are_named_in_the_render_notes(monkeypatch):
    """FM6: nobody talks in either clip and the resolver tied them: a note, not a silent guess."""
    job, snapshot, seen = setup_n1(
        monkeypatch,
        metas=_metas(**{ATAKAN: {"speaks_to_camera": False, "clip_content_type": "broll"}}),
    )

    steps, variant = run(job, snapshot)

    labels = labels_seen(seen)
    assert labels[ATAKAN] == labels[SIGN] == SIGN_LABEL
    assert [s["media_id"] for s in steps] == [s["media_id"] for s in PROD_STEPS]
    assert variant["phone_repair_notes"] == [
        'Clips 1 and 7 both look like "the DON JULIO sign", and I couldn\'t tell which one '
        "you meant, so clip 1 comes first."
    ]


def test_two_equal_look_alikes_side_by_side_need_no_note(monkeypatch):
    """FM6: both clips play around the same words, so there is nothing to tell."""
    order = [ATAKAN, SIGN, *[m for m in PROD_ORDER[1:6]]]
    starts = [0, 5, *PROD_STARTS[1:6]]
    job, snapshot, _seen = setup_n1(
        monkeypatch,
        metas=_metas(**{ATAKAN: {"speaks_to_camera": False, "clip_content_type": "broll"}}),
        order=order,
        starts=starts,
    )

    steps, variant = run(job, snapshot)

    assert [s["media_id"] for s in steps] == order
    assert not variant.get("phone_repair_notes")


@pytest.mark.parametrize("stage", ["look_alike_sets", "reseat"])
def test_a_tie_break_error_never_costs_the_video(monkeypatch, stage):
    """FM11."""
    import app.pipeline.narrated_look_alikes as look_alikes_mod

    def _boom(*_a, **_k):
        raise RuntimeError("unexpected")

    monkeypatch.setattr(look_alikes_mod, stage, _boom)
    job, snapshot, seen = setup_n1(monkeypatch)

    steps, variant = run(job, snapshot)

    assert [s["media_id"] for s in steps] == [s["media_id"] for s in PROD_STEPS]
    if stage == "look_alike_sets":
        assert labels_seen(seen)[ATAKAN] == SIGN_LABEL
    assert not variant.get("phone_repair_notes")


def test_the_tie_note_is_turkish_in_a_turkish_chat(monkeypatch):
    """FM7."""
    job, snapshot, _seen = setup_n1(
        monkeypatch,
        metas=_metas(**{ATAKAN: {"speaks_to_camera": False, "clip_content_type": "broll"}}),
        reply_language="tr",
    )

    _steps, variant = run(job, snapshot)

    assert variant["phone_repair_notes"] == [
        '1. ve 7. klibin ikisi de "the DON JULIO sign" gibi görünüyor; hangisini '
        "kastettiğini anlayamadım, bu yüzden önce 1. klip geliyor."
    ]
