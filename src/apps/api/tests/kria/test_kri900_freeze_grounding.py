"""KRI-900: a freeze holds the frame the creator named, never a guessed one.

Merope café M2 (KRI-775 fixture): "Freeze the frame for one second on Atakan's bite". The clip
record knows the bite's MOMENT (57.04-59.84 s of the take, "man takes first bite of dessert")
but not the instant of the bite. Before this fix the worker froze the moment's midpoint and the
receipt said "checked"; "freeze the last shot" matched any moment described with "last".

Each case replays the real gate (`planner._gate_unresolved_choices`, with the creator's answers
replayed from thread events), the real phone worker, the strict guided compiler, the phone
recipe compiler and the rollout validator, and reads the worker's own receipts. The bite case
also renders the held moment through the cloud path to an MP4 and compares the held picture
with the source frame at the second the creator picked.

Ways this can fail (written before the fix), each pinned below:
1. A position word still names a moment: "freeze the last shot" matches a "last light" moment.
2. Position parsing over-reaches: "his first bite" / "the last bite" lose their moment words.
3. "Last shot" holds the wrong cut (not the final one) or moves its frame.
4. The hold's seconds come out of the held cut's end, moving the frozen frame.
5. The midpoint guess survives anywhere (an unanswered question still holds the middle).
6. The receipt says "checked" / the exact frame for a frame nobody pinned down.
7. Question and render disagree: the answer pins a frame the worker does not hold.
8. The question is asked again after a hold-length correction or after the answer.
9. Model output alone pins a frame (a model-authored ``choice_answers`` row).
10. The question is asked where no freeze can render (feature unverified).
11. A picked frame outside the clip, or on a clip no longer in the edit, holds elsewhere.
12. Unanswered after two asks: the render blocks, or a frame is guessed.
13. "No freeze" still holds, or is reported as "Couldn't".
14. A brief with no freeze ask plans differently with the freeze path on.
15. Turkish position words are missed.
16. "Freeze the first frame / at the start" holds the first cut's LAST frame and says met.
17. "The last one" reads as a cut: "freeze the last one for 2 seconds" becomes a new freeze on
    the final shot instead of correcting the bite, or "make the last one 2 seconds" is lost.
18. The same cut asked twice ("the last shot", then "the last shot for 2 seconds") is two
    freezes, and the earlier one reports a false "couldn't tell which moment".
19. The second the receipt asks for ("freeze at 58.2 seconds") becomes the HOLD length.
20. A typed second is ignored, so the creator gets the same "tell me the second" receipt again.
21. The frame question is asked again after the creator typed the second.

Set ``KRI900_KEEP_DIR`` to keep the evidence: ``report.json`` (question, answer, plan row,
recipe hold, receipt per case), the cloud MP4 and ``held_vs_source.png``.
"""

from __future__ import annotations

import copy
import json
import os
import shutil
import subprocess
from pathlib import Path
from typing import Any

import pytest

from app.kria.brief import BriefRequirement, CreativeBrief
from app.kria.brief_checks import needs_creator_choice
from app.kria.freeze_ask import freeze_spec, live_freezes
from app.pipeline import unified_montage
from app.services.choice_questions import answered_brief
from tests.kria.test_kri775_merope_m2_replay import (
    RENDERS,
    TAKE,
    gate_media,
    gate_strategy,
    m2_brief_with_moment_ask,
    prod_flags,
    run_worker,
)
from tests.kria.test_kri828_freeze_frame_m2 import (
    BITE_END_S,
    BITE_START_S,
    _phone_recipe,
    _verify_freeze,
    gate_answering,
)

KEEP_DIR = os.environ.get("KRI900_KEEP_DIR")
BITE_MIDDLE_S = (BITE_START_S + BITE_END_S) / 2
_REPORT: dict[str, Any] = {}
_FRAME = 1 / 30 + 0.001


def _keep(case: str, payload: dict) -> None:
    if not KEEP_DIR:
        return
    directory = Path(KEEP_DIR)
    directory.mkdir(parents=True, exist_ok=True)
    _REPORT[case] = payload
    (directory / "report.json").write_text(
        json.dumps(_REPORT, indent=2, default=str, ensure_ascii=False) + "\n"
    )


@pytest.fixture(autouse=True)
def _prod(monkeypatch: pytest.MonkeyPatch) -> None:
    prod_flags(monkeypatch)
    _verify_freeze(monkeypatch)


def _option(question: dict, label_start: str) -> str:
    return next(o["key"] for o in question["options"] if o["label"].startswith(label_start))


async def _run(
    monkeypatch: pytest.MonkeyPatch,
    *,
    brief: CreativeBrief | None = None,
    render: dict | None = None,
    pick=None,  # noqa: ANN001
):  # noqa: ANN202
    """Gate (answering with ``pick``) + worker; returns (asked, strategy, plan, outcome)."""
    render = render if render is not None else RENDERS[0]
    brief = brief if brief is not None else m2_brief_with_moment_ask(render)
    kwargs = {"pick": pick} if pick is not None else {}
    result, asked = await gate_answering(monkeypatch, render, brief=brief, **kwargs)
    strategy = gate_strategy(result)
    captured: dict = {}
    real = unified_montage.plan_unified_montage

    def spy(*args, **kw):  # noqa: ANN002, ANN003, ANN202
        captured["plan"] = real(*args, **kw)
        return captured["plan"]

    monkeypatch.setattr(unified_montage, "plan_unified_montage", spy)
    outcome = run_worker(
        monkeypatch,
        render,
        brief=answered_brief(brief, strategy),
        media=gate_media(result),
        strategy=strategy,
    )
    monkeypatch.setattr(unified_montage, "plan_unified_montage", real)
    return asked, strategy, captured.get("plan"), outcome, result


def _receipt(outcome, requirement_id: str) -> dict | None:  # noqa: ANN001
    return next((r for r in outcome.receipts if r["requirement_id"] == requirement_id), None)


def _freeze_questions(asked: list[dict]) -> list[dict]:
    return [a["question"] for a in asked if a["question"].get("kind") == "freeze_frame"]


# -- 5, 6, 7, 8, 10: the bite is asked about, and the picked frame is the held frame ------------


async def test_the_bite_is_asked_about_and_the_picked_frame_is_held(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    picked: dict = {}

    def late(question: dict) -> str:
        picked["key"] = _option(question, "Late")
        return picked["key"]

    asked, strategy, plan, outcome, _result = await _run(monkeypatch, pick=late)
    questions = _freeze_questions(asked)
    # One question, before approval, with concrete frames inside the bite and a way out.
    assert len(questions) == 1, asked
    question = questions[0]
    assert question["conflict"] == "freeze_frame:r3"
    labels = [o["label"] for o in question["options"]]
    assert labels[-1] == "No freeze"
    assert [label.split(" (")[0] for label in labels[:-1]] == [
        "Early in it",
        "Middle",
        "Late in it",
    ]
    assert "57.0–59.8s" in asked[0]["text"]
    assert "won't guess" in asked[0]["text"]
    late_s = float(picked["key"].split("_")[1]) / 1000
    assert BITE_START_S < late_s < BITE_END_S
    assert late_s == pytest.approx(BITE_START_S + 0.75 * (BITE_END_S - BITE_START_S), abs=_FRAME)

    # The answer is server-owned on the approved strategy.
    answers = [a for a in strategy["choice_answers"] if a["kind"] == "freeze_frame"]
    assert answers == [
        {
            "conflict": "freeze_frame:r3",
            "kind": "freeze_frame",
            "option": picked["key"],
            "input_digest": question["input_digest"],
            "requirement_ids": ["r3"],
            "source": "creator",
        }
    ]

    assert outcome.refusal is None, outcome.refusal
    execution, recipe = _phone_recipe(monkeypatch, plan)
    held = [m for m in execution.story_timeline if m.freeze_hold_s]
    assert len(held) == 1
    bite = held[0]
    # 7. the held frame is exactly the frame the creator picked, never the moment's middle
    assert bite.media_id == TAKE
    assert bite.source_end_s == pytest.approx(late_s, abs=0.002)
    assert abs(bite.source_end_s - (BITE_START_S + BITE_END_S) / 2) > 0.5
    clip = next(c for t in recipe.tracks for c in t.clips if c.id == bite.moment_id)
    assert clip.rate == 1
    assert clip.hold_duration == pytest.approx(1.0, abs=_FRAME)
    assert clip.source_start + clip.source_duration == pytest.approx(late_s, abs=0.002)
    row = next(r for r in plan.freeze_frames if r["requirement_id"] == "r3")
    assert row["grounding"] == "creator_frame"

    # 6. the receipt is "checked" because the frame is exactly the one asked for
    receipt = _receipt(outcome, "r3")
    assert receipt["status"] == "met", receipt
    assert receipt["verification"] == "checked"
    assert "second you picked" in receipt["reason"]
    assert _receipt(outcome, "r7")["status"] == "met"

    cloud = _render_held_moment(tmp_path, bite.source_start_s, bite.source_end_s, 1.0)
    # The held picture is the last frame the renderer played, i.e. the window's end, which
    # is the picked second (the window is pinned above) ...
    assert cloud["held_luma"] == pytest.approx(cloud["played_last_luma"], abs=0.75)
    # ... the ramp is still moving into it (a true hold at 1x, not a stall) ...
    assert cloud["ramp_per_frame"] > 0.75
    # ... and, read on the same ramp, it is far from where the moment's middle (the old
    # guess) would have frozen: 0.7 s = 21 frames away.
    frames_from_middle = (cloud["held_luma"] - cloud["middle_luma"]) / cloud["ramp_per_frame"]
    assert frames_from_middle == pytest.approx((late_s - BITE_MIDDLE_S) * 30, abs=2)
    _keep(
        "bite_picked_late",
        {
            "question": question,
            "question_text": asked[0]["text"],
            "answer": answers[0],
            "plan_row": row,
            "recipe_clip": clip.model_dump(mode="json"),
            "receipt": receipt,
            "cloud": cloud,
        },
    )


def _ffmpeg(*args: str) -> None:
    subprocess.run(
        ["ffmpeg", "-v", "error", "-y", *args], check=True, capture_output=True, timeout=120
    )


def _render_held_moment(tmp_path: Path, start_s: float, end_s: float, hold_s: float) -> dict:
    """The cloud renderer's held moment over a synthetic take whose luma encodes time.

    Only 55-61 s of the 68.37 s take matter, so the synthetic source starts at take second
    55 and brightens 40 luma per second. Luma is read on the renderer's own output (its
    moving part and its held part), so colour-range conversion cancels out.
    """
    from app.pipeline import guided_story  # noqa: PLC0415
    from tests.kria.test_kri828_freeze_frame_m2 import _all_lumas  # noqa: PLC0415

    if shutil.which("ffmpeg") is None:
        pytest.skip("ffmpeg required")
    base = 55.0
    source = tmp_path / "take.mp4"
    _ffmpeg(
        "-f",
        "lavfi",
        "-i",
        "color=black:size=180x320:rate=30,geq=lum='min(255,20+T*40)':cb=128:cr=128",
        "-t",
        "6",
        "-c:v",
        "libx264",
        "-pix_fmt",
        "yuv420p",
        str(source),
    )
    total = round(end_s - start_s + hold_s, 3)
    plan = {
        "output_orientation": "portrait",
        "resolved_duration_s": total,
        "story_timeline": [
            {
                "moment_id": "held",
                "beat_id": "held",
                "media_id": "take",
                "generation": "1",
                "kind": "video",
                "layout": "fullscreen",
                "source_start_s": round(start_s - base, 3),
                "source_end_s": round(end_s - base, 3),
                "freeze_hold_s": hold_s,
                "output_start_s": 0.0,
                "output_end_s": total,
                "duration_s": total,
            }
        ],
    }
    outputs, receipts = guided_story._render_moments(plan, {"take": str(source)}, str(tmp_path))
    held = Path(outputs[0])
    played = _all_lumas(tmp_path / "moment_00_moving.mp4")
    frames = _all_lumas(held)
    ramp = (played[-1] - played[0]) / max(1, len(played) - 1)
    # Where the moment's middle sits on the played ramp (frames before its last frame).
    middle_luma = played[-1] - ramp * (end_s - BITE_MIDDLE_S) * 30
    if KEEP_DIR:
        directory = Path(KEEP_DIR)
        directory.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(held, directory / "cloud_held_moment.mp4")
        # Left: a held frame of the render. Right: the source frame at the picked second.
        _ffmpeg(
            "-ss",
            f"{receipts[0]['output_duration_s'] - hold_s / 2:.3f}",
            "-i",
            str(held),
            "-ss",
            f"{end_s - base - 1 / 30:.3f}",
            "-i",
            str(source),
            "-filter_complex",
            "[0:v]scale=180:320,trim=end_frame=1[a];[1:v]trim=end_frame=1[b];[a][b]hstack",
            "-frames:v",
            "1",
            str(directory / "held_vs_source.png"),
        )
    return {
        "held_luma": frames[-1],
        "held_lumas": frames[-int(hold_s * 30) :],
        "played_last_luma": played[-1],
        "ramp_per_frame": ramp,
        "middle_luma": middle_luma,
        "moment_receipt": receipts[0],
        "picked_source_s": end_s,
    }


# -- 6: "you choose" holds the middle, and the receipt says the frame was left to Kria ----------


async def test_a_delegated_frame_is_held_but_never_called_checked(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from tests.kria.test_kri828_freeze_frame_m2 import pick_recommended  # noqa: PLC0415

    _delegating(monkeypatch)
    asked, strategy, plan, outcome, _result = await _run(monkeypatch, pick=pick_recommended)
    answer = next(a for a in strategy["choice_answers"] if a["kind"] == "freeze_frame")
    assert answer["source"] == "creator_delegated"
    row = next(r for r in plan.freeze_frames if r["requirement_id"] == "r3")
    assert row["status"] == "held"
    assert row["grounding"] == "delegated_frame"
    receipt = _receipt(outcome, "r3")
    assert receipt["status"] == "met"
    # Never said as the creator's own frame: the reason says who chose it.
    assert "you left the exact frame to me" in receipt["reason"]
    assert "you picked" not in receipt["reason"]
    _keep("delegated", {"answer": answer, "plan_row": row, "receipt": receipt})


def _delegating(monkeypatch: pytest.MonkeyPatch) -> None:
    """Every answer replays as a delegation ("you choose"), as the app records one."""
    from app.services import choice_questions  # noqa: PLC0415

    real = choice_questions.fold_scoped_answers

    def fold(events):  # noqa: ANN001, ANN202
        return {
            key: choice_questions.ScopedAnswer(
                answer.option_key, answer.input_digest, True, answer.index
            )
            for key, answer in real(events).items()
        }

    monkeypatch.setattr(choice_questions, "fold_scoped_answers", fold)


# -- 5, 12: unanswered twice, the draft goes through with no hold and a specific ask ---------------


async def test_an_unanswered_question_renders_no_guess(monkeypatch: pytest.MonkeyPatch) -> None:
    asked, strategy, plan, outcome, _result = await _run(monkeypatch, pick=lambda _q: None)
    assert len(_freeze_questions(asked)) == 2  # the question and one re-ask, never more
    assert not [a for a in strategy.get("choice_answers") or [] if a["kind"] == "freeze_frame"]
    assert outcome.refusal is None, outcome.refusal
    execution, recipe = _phone_recipe(monkeypatch, plan)
    assert not [m for m in execution.story_timeline if m.freeze_hold_s]
    assert "freezeFrame" not in recipe.required_capabilities
    row = next(r for r in plan.freeze_frames if r["requirement_id"] == "r3")
    assert row["status"] == "needs_frame"
    assert row["moment_source_s"] == [BITE_START_S, BITE_END_S]
    receipt = _receipt(outcome, "r3")
    assert receipt["status"] == "not_possible", receipt
    # An honest limit with a specific ask, never the "simpler version?" question.
    assert not needs_creator_choice(receipt)
    assert receipt["reason"].startswith("I found that moment (57.0–59.8s into the clip)")
    assert "Tell me the second to freeze on" in receipt["reason"]
    _keep("unanswered", {"plan_row": row, "receipt": receipt})


# -- 13: "No freeze" withdraws the ask ---------------------------------------------------------


async def test_no_freeze_holds_nothing_and_says_so(monkeypatch: pytest.MonkeyPatch) -> None:
    asked, strategy, plan, outcome, result = await _run(
        monkeypatch, pick=lambda q: "no_freeze" if q.get("kind") == "freeze_frame" else None
    )
    assert len(_freeze_questions(asked)) == 1
    assert outcome.refusal is None, outcome.refusal
    execution, _recipe = _phone_recipe(monkeypatch, plan)
    assert not [m for m in execution.story_timeline if m.freeze_hold_s]
    summary = result.plan.intents[0].arguments["summary"]
    assert "leaving the freeze out" in summary
    # The withdrawn ask is never judged as a "Couldn't".
    assert _receipt(outcome, "r3") is None
    assert all("freeze" not in str(r.get("reason")).lower() for r in outcome.receipts)
    _keep("no_freeze", {"summary": summary, "receipts": outcome.receipts})


# -- 8: a hold-length correction after the answer is never asked again ---------------------------


async def test_a_length_correction_keeps_the_picked_frame(monkeypatch: pytest.MonkeyPatch) -> None:
    from tests.kria.test_kri899_freeze_length_changes import _with  # noqa: PLC0415

    answered = await _run(monkeypatch, pick=lambda q: _option(q, "Early"))
    early_key = next(
        a["option"] for a in answered[1]["choice_answers"] if a["kind"] == "freeze_frame"
    )
    brief = _with(("Make the freeze 2 seconds instead", {"freeze_frame": True}))
    picks: list[str] = []

    def early_again(question: dict) -> str:
        picks.append(question["conflict"])
        return _option(question, "Early")

    asked, strategy, plan, outcome, _result = await _run(monkeypatch, brief=brief, pick=early_again)
    questions = _freeze_questions(asked)
    # Same conflict, same digest as before the correction: one answer covers both turns.
    assert [q["input_digest"] for q in questions] == [
        q["input_digest"] for q in _freeze_questions(answered[0])
    ]
    answer = next(a for a in strategy["choice_answers"] if a["kind"] == "freeze_frame")
    assert answer["option"] == early_key
    assert set(answer["requirement_ids"]) >= {"r3"}
    execution, _recipe = _phone_recipe(monkeypatch, plan)
    held = [m for m in execution.story_timeline if m.freeze_hold_s]
    assert len(held) == 1
    assert held[0].source_end_s == pytest.approx(int(early_key.split("_")[1]) / 1000, abs=0.002)
    assert held[0].freeze_hold_s == pytest.approx(2.0, abs=0.001)


# -- 1, 3, 4: "the last shot" is the final cut, never a moment described with "last" -------------


def _with_decoy_and_last_shot(description: str) -> tuple[dict, CreativeBrief]:
    render = copy.deepcopy(RENDERS[0])
    for snapshot in (render["media_snapshot"],):
        analysis = snapshot["clip_assignments"][0]["analysis"]
        analysis["best_moments"] = [
            *analysis["best_moments"],
            {
                "start_s": 30.0,
                "end_s": 32.0,
                "energy": 1.0,
                "description": "last light on the wall",
            },
        ]
    brief = m2_brief_with_moment_ask(render)
    requirements = [
        req.model_copy(update={"description": description, "facts": {"freeze_frame": True}})
        if req.id == "r3"
        else req
        for req in brief.requirements
    ]
    return render, brief.model_copy(update={"requirements": requirements})


async def test_the_last_shot_holds_the_final_cuts_own_last_frame(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    render, brief = _with_decoy_and_last_shot("Freeze the last shot for 2 seconds")
    _verify_freeze(monkeypatch, on=False)
    _asked0, _s0, baseline_plan, _o0, _r0 = await _run(monkeypatch, brief=brief, render=render)
    baseline, _recipe0 = _phone_recipe(monkeypatch, baseline_plan)
    _verify_freeze(monkeypatch)
    asked, _strategy, plan, outcome, _result = await _run(monkeypatch, brief=brief, render=render)
    assert not _freeze_questions(asked)  # a named cut is exact: nothing to ask
    assert outcome.refusal is None, outcome.refusal
    execution, recipe = _phone_recipe(monkeypatch, plan)
    held = [m for m in execution.story_timeline if m.freeze_hold_s]
    assert len(held) == 1
    last = execution.story_timeline[-1]
    assert held[0].moment_id == last.moment_id
    assert last.kind == "video"
    # 4. its own last frame, unmoved by the hold's reservation
    assert last.source_end_s == pytest.approx(baseline.story_timeline[-1].source_end_s, abs=0.002)
    assert not (30.0 <= last.source_end_s <= 32.0)  # 1. never the "last light" decoy
    assert last.freeze_hold_s == pytest.approx(2.0, abs=_FRAME)
    clip = next(c for t in recipe.tracks for c in t.clips if c.id == last.moment_id)
    assert clip.hold_duration == pytest.approx(2.0, abs=_FRAME)
    row = next(r for r in plan.freeze_frames if r["requirement_id"] == "r3")
    assert (row["grounding"], row["position"]) == ("cut_position", "last")
    receipt = _receipt(outcome, "r3")
    assert receipt["status"] == "met", receipt
    assert receipt["verification"] == "checked"
    assert "last frame of the last shot" in receipt["reason"]
    assert _receipt(outcome, "r7")["status"] == "met", _receipt(outcome, "r7")  # about 14 s
    _keep("last_shot", {"plan_row": row, "receipt": receipt, "recipe_duration": recipe.duration})


# -- 1, 2, 15: position words are positions, moment words stay moment words ---------------------


def _req(description: str, rid: str = "r3") -> BriefRequirement:
    return BriefRequirement(
        id=rid,
        kind="timing",
        scope="global",
        description=description,
        facts={"freeze_frame": True},
    )


@pytest.mark.parametrize(
    ("description", "position", "words"),
    [
        ("Freeze the last shot", "last", ()),
        ("Freeze the final frame for 2 seconds", "last", ()),
        ("freeze at the end", "last", ()),
        ("Freeze the first shot", "first", ()),
        ("freeze 2 seconds into the video", None, ()),
        ("Son sahneyi dondur", "last", ()),
        ("Videonun sonunda kareyi dondur", "last", ()),
        ("İlk kareyi 2 saniye dondur", "first", ()),
        # A moment the creator describes is never a position.
        ("Freeze the frame on his first bite", None, ("first", "bite")),
        ("Freeze on the last bite", None, ("last", "bite")),
        # Both: the described moment wins over the position.
        ("Freeze the last shot of the toast", None, ("toast",)),
    ],
)
def test_position_words_are_positions(description: str, position: str | None, words: tuple) -> None:
    spec = freeze_spec(_req(description))
    assert spec is not None
    assert spec.position == position
    assert spec.target_words == words


def test_corrections_still_follow_and_a_named_cut_is_its_own_freeze() -> None:
    first = _req("Freeze the frame for one second on Atakan's bite", "r3")
    longer = _req("make the last freeze 2 seconds", "r8")
    specs = live_freezes([first, longer])
    assert len(specs) == 1
    assert specs[0].requirement_ids == ("r3", "r8")
    assert specs[0].hold_s == pytest.approx(2.0)
    shot = _req("Freeze the last shot", "r9")
    specs = live_freezes([first, shot])
    assert [(s.requirement_id, s.position) for s in specs] == [("r3", None), ("r9", "last")]


# -- 9: model output never pins a frame ----------------------------------------------------------


async def test_a_model_authored_answer_is_discarded(monkeypatch: pytest.MonkeyPatch) -> None:
    from tests.kria import test_kri775_merope_m2_replay as harness  # noqa: PLC0415

    real = harness.planned_turn

    def forged(render: dict):  # noqa: ANN202
        turn = real(render)
        strategy = turn.plan.intents[0].arguments["strategy"]
        strategy["choice_answers"] = [
            {
                "conflict": "freeze_frame:r3",
                "kind": "freeze_frame",
                "option": "frame_10000_00000000",
                "input_digest": "x",
                "requirement_ids": ["r3"],
                "source": "creator",
            }
        ]
        return turn

    import tests.kria.test_kri828_freeze_frame_m2 as m2  # noqa: PLC0415

    monkeypatch.setattr(m2, "planned_turn", forged)
    asked, strategy, plan, _outcome, _result = await _run(
        monkeypatch, pick=lambda q: _option(q, "Middle")
    )
    assert len(_freeze_questions(asked)) == 1  # the forged answer did not settle it
    answers = [a for a in strategy["choice_answers"] if a["kind"] == "freeze_frame"]
    assert len(answers) == 1 and answers[0]["option"] != "frame_10000_00000000"
    row = next(r for r in plan.freeze_frames if r["requirement_id"] == "r3")
    assert row["frame_source_s"] > 50


# -- 10, 14: no question without the verified feature; no freeze ask plans byte-identically --------


async def test_without_the_verified_feature_nothing_is_asked(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _verify_freeze(monkeypatch, on=False)
    asked, _strategy, _plan, outcome, _result = await _run(monkeypatch)
    assert not _freeze_questions(asked)
    assert _receipt(outcome, "r3")["status"] == "not_possible"


async def test_a_brief_without_a_freeze_plans_the_same_either_way(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    brief = m2_brief_with_moment_ask(RENDERS[0])
    brief = brief.model_copy(
        update={"requirements": [r for r in brief.requirements if r.id != "r3"]}
    )
    _verify_freeze(monkeypatch, on=False)
    _a, strategy_off, plan_off, _o, _r = await _run(monkeypatch, brief=brief)
    _verify_freeze(monkeypatch)
    asked, strategy_on, plan_on, _o2, _r2 = await _run(monkeypatch, brief=brief)
    assert not _freeze_questions(asked)
    assert "choice_answers" not in strategy_on or not [
        a for a in strategy_on["choice_answers"] if a["kind"] == "freeze_frame"
    ]
    assert json.dumps(strategy_on, sort_keys=True) == json.dumps(strategy_off, sort_keys=True)
    assert plan_on.freeze_frames == [] == plan_off.freeze_frames
    on = json.dumps(plan_on.guided_edit(), sort_keys=True, default=str)
    off = json.dumps(plan_off.guided_edit(), sort_keys=True, default=str)
    assert on == off


# -- 7: two moments fit the words equally: the creator picks the moment, never a guess ----------


async def test_tied_moments_ask_which_moment_and_hold_the_picked_one(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    render = copy.deepcopy(RENDERS[0])
    analysis = render["media_snapshot"]["clip_assignments"][0]["analysis"]
    analysis["best_moments"] = [
        *analysis["best_moments"],
        {"start_s": 24.0, "end_s": 26.0, "energy": 1.0, "description": "a bite of bread"},
    ]
    brief = m2_brief_with_moment_ask(render)

    def dessert(question: dict) -> str:
        return next(o["key"] for o in question["options"] if "dessert" in o["label"])

    asked, strategy, plan, outcome, _result = await _run(
        monkeypatch, brief=brief, render=render, pick=dessert
    )
    question = _freeze_questions(asked)[0]
    labels = [o["label"] for o in question["options"]]
    # In the clip record's own order.
    assert labels == [
        "“man takes first bite of dessert” (57.0–59.8s)",
        "“a bite of bread” (24.0–26.0s)",
        "No freeze",
    ]
    assert not any(o.get("recommended") for o in question["options"])  # no guess offered
    assert outcome.refusal is None, outcome.refusal
    row = next(r for r in plan.freeze_frames if r["requirement_id"] == "r3")
    assert row["grounding"] == "creator_moment"
    assert row["frame_source_s"] == pytest.approx(BITE_MIDDLE_S, abs=_FRAME)
    receipt = _receipt(outcome, "r3")
    assert receipt["status"] == "met"
    assert "the moment you picked" in receipt["reason"]
    _keep("tied_moments", {"question": question, "plan_row": row, "receipt": receipt})


# -- 16: the opening frame is never faked with the end of the first cut --------------------------


@pytest.mark.parametrize("description", ["İlk kareyi 2 saniye dondur", "Freeze at the start"])
async def test_the_first_frame_is_asked_for_never_faked(
    monkeypatch: pytest.MonkeyPatch, description: str
) -> None:
    render, brief = _with_decoy_and_last_shot(description)
    asked, _strategy, plan, outcome, _result = await _run(monkeypatch, brief=brief, render=render)
    assert not _freeze_questions(asked)
    assert outcome.refusal is None, outcome.refusal
    execution, recipe = _phone_recipe(monkeypatch, plan)
    assert not [m for m in execution.story_timeline if m.freeze_hold_s]
    assert "freezeFrame" not in recipe.required_capabilities
    row = next(r for r in plan.freeze_frames if r["requirement_id"] == "r3")
    assert (row["status"], row["position"]) == ("needs_frame", "first")
    assert "frame_source_s" not in row
    receipt = _receipt(outcome, "r3")
    assert receipt["status"] == "not_possible", receipt
    assert "can't hold the opening frame" in receipt["reason"] or "açılış" in receipt["reason"]
    _keep(f"first_frame:{description}", {"plan_row": row, "receipt": receipt})


# -- 17, 18: "the last one" is the latest freeze; the same cut twice is one freeze ---------------


@pytest.mark.parametrize(
    "later", ["freeze the last one for 2 seconds", "make the last one 2 seconds"]
)
def test_the_last_one_corrects_the_latest_freeze(later: str) -> None:
    bite = _req("Freeze the frame for one second on Atakan's bite", "r3")
    specs = live_freezes([bite, _req(later, "r8")])
    assert [(s.requirement_ids, s.target_words, s.position, s.hold_s) for s in specs] == [
        (("r3", "r8"), ("atakan", "bite"), None, 2.0)
    ]


def test_the_same_cut_asked_twice_is_one_freeze() -> None:
    specs = live_freezes(
        [_req("Freeze the last shot", "r1"), _req("Freeze the last shot for 2 seconds", "r2")]
    )
    assert [(s.requirement_ids, s.position, s.hold_s) for s in specs] == [
        (("r1", "r2"), "last", 2.0)
    ]


# -- 19, 20, 21: the second the receipt asks for is the frame, never the hold --------------------


@pytest.mark.parametrize(
    ("reply", "frame_s"),
    [
        ("freeze at 58.2 seconds", 58.2),
        ("freeze it at 58.5 seconds into the clip", 58.5),
        ("Klibin 58.2. saniyesinde dondur", 58.2),
    ],
)
async def test_a_typed_second_is_the_held_frame(
    monkeypatch: pytest.MonkeyPatch, reply: str, frame_s: float
) -> None:
    from tests.kria.test_kri899_freeze_length_changes import _with  # noqa: PLC0415

    brief = _with((reply, {"freeze_frame": True}))
    asked, strategy, plan, outcome, _result = await _run(
        monkeypatch, brief=brief, pick=lambda _q: None
    )
    assert not _freeze_questions(asked)  # 21. the creator already named the frame
    assert not [a for a in strategy.get("choice_answers") or [] if a["kind"] == "freeze_frame"]
    assert outcome.refusal is None, outcome.refusal
    execution, recipe = _phone_recipe(monkeypatch, plan)
    held = [m for m in execution.story_timeline if m.freeze_hold_s]
    assert len(held) == 1
    assert held[0].media_id == TAKE
    assert held[0].source_end_s == pytest.approx(frame_s, abs=0.002)  # 20.
    assert held[0].freeze_hold_s == pytest.approx(1.0, abs=_FRAME)  # 19. the bite's 1 s stands
    clip = next(c for t in recipe.tracks for c in t.clips if c.id == held[0].moment_id)
    assert clip.hold_duration == pytest.approx(1.0, abs=_FRAME)
    row = next(r for r in plan.freeze_frames if "r3" in r["requirement_ids"])
    assert row["grounding"] == "creator_frame"
    assert row["hold_s"] == pytest.approx(1.0)
    receipt = _receipt(outcome, "r3")
    assert receipt["status"] == "met", receipt
    assert receipt["verification"] == "checked"
    assert f"({frame_s:.1f}s into the clip)" in receipt["reason"] or "saniyesi" in receipt["reason"]
    _keep(f"typed_second:{reply}", {"plan_row": row, "receipt": receipt})


async def test_a_second_into_the_video_holds_the_cut_playing_then(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from tests.kria.test_kri899_freeze_length_changes import _with  # noqa: PLC0415

    brief = _with(("freeze 2 seconds into the video", {"freeze_frame": True}))
    _asked, _strategy, plan, outcome, _result = await _run(
        monkeypatch, brief=brief, pick=lambda _q: None
    )
    assert outcome.refusal is None, outcome.refusal
    row = next(r for r in plan.freeze_frames if "r3" in r["requirement_ids"])
    assert (row["status"], row["frame_scope"], row["frame_requested_s"]) == ("held", "edit", 2.0)
    assert row["hold_s"] == pytest.approx(1.0)  # 19. "2 seconds into" is never the hold
    execution, _recipe = _phone_recipe(monkeypatch, plan)
    held = [m for m in execution.story_timeline if m.freeze_hold_s]
    assert len(held) == 1
    assert held[0].source_end_s == pytest.approx(row["frame_source_s"], abs=0.002)
