"""Cusco DJI T4 "Altının yolu": "use my voiceover" and "fill the rest" on a finished Voiceover.

Phone Voiceover job 91270ec0 (2026-11-17, build 2016, server 80c96b04c): a 63.3 s Turkish voiceover
file over 11 silent clips. After the render the review listed r1 "Use my recorded Turkish
voiceover." (``kind: audio``, ``facts.voiceover_language``) and r7 "Fill the rest with the other
clips." (``kind: select``, ``facts.all``) under "Have a look at these in the video". Both are
facts of the job's own record: the phone Voiceover writer runs only with a recorded voiceover file
(it narrates that file), and the steps it pinned (``assembly_plan.narrated_alignment``) name every
clip that plays. KRI-1515 (#2377) judges "use all these clips" on montages; the Voiceover branch of
the review never gave that check the attached clips or the clips the steps play.

``tests/fixtures/narrated_alignment/kri1517_cusco_t4_moments.json`` is the job's own record (brief
rows, steps with media renamed c0..c10, the variant without URLs, the review's receipts). The job
had 11 clips: the variant's own editor pool (``narrated_clip_assignments``) indexes clip_0..clip_10
and each step plays one of c0..c10. No media, no model call.

Ways this can fail, written before the code:

 V1 the replay is not the incident: with the switch off (NARRATED_CUE_RECEIPTS_ENABLED) r1 and r7
    are not the recorded "partial / unchecked", so a pass proves nothing (red control).
 V2 r1 is still "Have a look" although the narration is the recorded voiceover file.
 V3 r1 reads "Done" without proof: no accepted Voiceover record for this render (another
    generation, e.g. after an editor Save), or the record heard another language than the one the
    creator named for the file.
 V4 the voiceover check claims another audio row: a row with more than the file's language
    ("Use my voiceover, music under it"), or one a bed / cleanup reader owns.
 V5 wrong content passes: a clip the steps never play still reads "Done" for "fill the rest" (it
    must say "Left out 1 of your 11 clips"); an extra attached clip is not counted.
 V6 the voiceover file itself, or a clip the creator asked to leave out, counts as a left-out clip.
 V7 ids written in another form (paths whose names match no step) read as "Left out 11 of 11".
 V8 the montage / Talking "use all" answers change (KRI-1515 suites) or a draft is newly judged.
 V9 the Turkish chat gets the English sentence.
"""

from __future__ import annotations

import copy
import json
import uuid
from pathlib import Path
from types import SimpleNamespace

import pytest

from app.config import settings
from app.kria.brief import BriefRequirement, CreativeBrief
from app.kria.brief_binding import BriefBinding
from app.tasks import kria_runtime

CASE = json.loads(
    (
        Path(__file__).resolve().parents[1]
        / "fixtures"
        / "narrated_alignment"
        / "kri1517_cusco_t4_moments.json"
    ).read_text(encoding="utf-8")
)
RECORDED = {row["requirement_id"]: row for row in CASE["review_event_27"]}
POOL = sorted({row["clip_id"] for row in CASE["variant"]["narrated_clip_assignments"]})
CLIPS = [f"c{index}" for index in range(len(POOL))]
VOICEOVER = "plan-items/t4/voiceover/altinin-yolu.m4a"


def _clip_paths(names=CLIPS) -> list[str]:
    return [f"plan-items/t4/proxies/{name}" for name in names]


def _requirements(*extra: dict) -> list[BriefRequirement]:
    return [BriefRequirement.model_validate(row) for row in [*CASE["requirements"], *extra]]


def _job(
    *,
    steps: list[dict] | None = None,
    clip_paths: list[str] | None = None,
    voiceover_path: str | None = VOICEOVER,
    requirements: list[BriefRequirement] | None = None,
    record_generation: str | None = None,
    strategy: dict | None = None,
):
    """The recorded job: its brief binding (with the approved media), the Voiceover record, the
    variant. The variant carries the record's generation, as a cloud-checked render does."""
    record = copy.deepcopy(CASE["narrated_alignment"])
    if steps is not None:
        record["steps"] = steps
    variant = copy.deepcopy(CASE["variant"])
    variant["render_generation_id"] = record["generation_id"]
    if record_generation is not None:
        record["generation_id"] = record_generation
    thread_id = uuid.uuid4()
    binding = BriefBinding.create(
        thread_id,
        CreativeBrief(version=CASE["brief_version"], requirements=requirements or _requirements()),
        media_snapshot={
            "clip_paths": _clip_paths() if clip_paths is None else clip_paths,
            "voiceover_path": voiceover_path,
        },
    )
    job = SimpleNamespace(
        id=uuid.uuid4(),
        assembly_plan={
            "creator_brief_binding": binding.model_dump(mode="json"),
            "narrated_alignment": record,
            "variants": [variant],
        },
        all_candidates={"creator_strategy": strategy or {}},
        content_plan_item_id=None,
    )
    thread = SimpleNamespace(id=thread_id, creator_id=uuid.uuid4())
    return job, thread, variant


def _review(**kwargs) -> dict[str, dict]:
    job, thread, variant = _job(**kwargs)
    _text, payload = kria_runtime._approved_generation_review(
        None, thread, job, variant, SimpleNamespace(result={}), "Ready."
    )
    return {row["requirement_id"]: row for row in payload}


def _verdict(row: dict) -> tuple:
    return row["status"], row["verification"], row["reason"]


def _steps_with(clip_for: dict[str, str]) -> list[dict]:
    """The recorded steps with some steps' clip swapped (``{step_id: clip}``)."""
    steps = copy.deepcopy(CASE["narrated_alignment"]["steps"])
    for step in steps:
        if step["step_id"] in clip_for:
            step["clip"] = step["media_id"] = clip_for[step["step_id"]]
    return steps


@pytest.fixture(autouse=True)
def _switch_on(monkeypatch) -> None:
    monkeypatch.setattr(settings, "narrated_cue_receipts_enabled", True)


def test_the_job_had_eleven_clips_and_every_step_plays_one_of_them() -> None:
    assert len(POOL) == 11
    assert {step["clip"] for step in CASE["narrated_alignment"]["steps"]} == set(CLIPS)


# -- V1: the replay is the incident -------------------------------------------------------------


def test_v1_with_the_switch_off_r1_and_r7_are_the_recorded_have_a_look(monkeypatch) -> None:
    monkeypatch.setattr(settings, "narrated_cue_receipts_enabled", False)

    rows = _review()

    for rid in ("r1", "r7"):
        assert RECORDED[rid]["verification"] == "unchecked"
        assert rows[rid]["status"] == "partial", rid
        assert rows[rid]["verification"] in (None, "unchecked"), rows[rid]


# -- V2 / V5: the incident, judged ---------------------------------------------------------------


def test_v2_v5_the_voiceover_and_fill_the_rest_are_done_on_the_t4_render() -> None:
    rows = _review()

    assert _verdict(rows["r1"]) == ("met", "checked", None)
    assert _verdict(rows["r7"]) == ("met", "checked", None)
    # The rows already judged keep their recorded answer.
    assert _verdict(rows["r2"]) == ("met", "checked", "The captions are in Turkish.")


def test_v5_a_clip_no_step_plays_is_left_out() -> None:
    # seg_3 played c3; it now plays c4 again, so c3 never plays.
    rows = _review(steps=_steps_with({"seg_3": "c4"}))

    assert _verdict(rows["r7"]) == ("partial", "checked", "Left out 1 of your 11 clips")
    assert _verdict(rows["r1"]) == ("met", "checked", None)


def test_v5_an_extra_attached_clip_the_steps_never_play_is_counted() -> None:
    rows = _review(clip_paths=_clip_paths([*CLIPS, "c11"]))

    assert _verdict(rows["r7"]) == ("partial", "checked", "Left out 1 of your 12 clips")


def test_v9_turkish_chat_gets_the_turkish_sentence() -> None:
    from app.kria.reply_language import _CURRENT  # noqa: PLC0415

    token = _CURRENT.set("tr")
    try:
        rows = _review(steps=_steps_with({"seg_3": "c4"}))
    finally:
        _CURRENT.reset(token)
    assert rows["r7"]["reason"] == "11 klibinden 1 tanesi dışarıda kaldı"


# -- V6: what is not a left-out clip -------------------------------------------------------------


def test_v6_the_voiceover_file_listed_with_the_clips_is_not_a_left_out_clip() -> None:
    rows = _review(clip_paths=[*_clip_paths(), VOICEOVER])

    assert _verdict(rows["r7"]) == ("met", "checked", None)


@pytest.mark.parametrize("key", ["c3", "plan-items/t4/proxies/c3"])
def test_v6_a_clip_the_creator_left_out_is_not_counted(key: str) -> None:
    strategy = {
        "resolved_clip_intents": [
            {
                "op": "exclude",
                "status": "resolved",
                "attribute": "the blurry street",
                "assignments": [{"media_id": key}],
            }
        ]
    }

    rows = _review(steps=_steps_with({"seg_3": "c4"}), strategy=strategy)

    assert _verdict(rows["r7"]) == ("met", "checked", None)


# -- V7: ids that match nothing claim nothing ----------------------------------------------------


def test_v7_clip_names_matching_no_step_keep_the_old_answer() -> None:
    rows = _review(clip_paths=[f"plan-items/t4/proxies/IMG_{i:04}.MOV" for i in range(11)])

    assert rows["r7"]["status"] == "partial"
    assert rows["r7"]["verification"] in (None, "unchecked")
    assert "Left out" not in (rows["r7"]["reason"] or "")


def test_v7_no_approved_clip_list_keeps_the_old_answer() -> None:
    rows = _review(clip_paths=[])

    assert rows["r7"]["verification"] in (None, "unchecked")


# -- V3: no proof, no "Done" ---------------------------------------------------------------------


def test_v3_a_record_from_another_generation_judges_neither_row() -> None:
    rows = _review(record_generation="an-editor-save-generation")

    for rid in ("r1", "r7"):
        assert rows[rid]["status"] == "partial", rid
        assert rows[rid]["verification"] in (None, "unchecked"), rows[rid]


def test_v3_a_voiceover_heard_in_another_language_keeps_the_old_answer() -> None:
    german = {**CASE["requirements"][0], "facts": {"voiceover_language": "de"}}
    rows = _review(
        requirements=[
            BriefRequirement.model_validate(row) for row in [german, *CASE["requirements"][1:]]
        ]
    )

    assert rows["r1"]["status"] == "partial"
    assert rows["r1"]["verification"] in (None, "unchecked")


# -- V4: the voiceover check owns only the plain ask ---------------------------------------------


@pytest.mark.parametrize(
    ("description", "facts"),
    [
        (
            "Use my recorded Turkish voiceover with soft music under it.",
            {"voiceover_language": "tr", "audio_intent": "music"},
        ),
        ("Use my Turkish voiceover and cut out the long pauses.", {"voiceover_language": "tr"}),
        (
            "Use my Turkish voiceover with the clip sound quiet under it.",
            {"voiceover_language": "tr"},
        ),
        ("Keep the original sound.", {}),
    ],
)
def test_v4_other_audio_rows_are_not_called_the_voiceover(
    monkeypatch, description: str, facts: dict
) -> None:
    row = {
        "id": "r9",
        "kind": "audio",
        "facts": facts,
        "scope": "global",
        "status": "open",
        "literal": None,
        "description": description,
    }

    judged = _review(requirements=_requirements(row))["r9"]
    monkeypatch.setattr(settings, "narrated_cue_receipts_enabled", False)
    before = _review(requirements=_requirements(row))["r9"]

    # Whatever another reader says about the row (the cleanup check judges the pauses), this
    # change says nothing new about it.
    assert _verdict(judged) == _verdict(before)
