"""The runner replays turns through the real paths."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from app.kria.replay import load_fixture as load_kria_fixture

from .models import FinalPlan, PlanClip, PlanText, RFFixture, Turn
from .runner import (
    FIXTURE_ROOT,
    _plan_with_texts,
    _plan_with_timeline_slots,
    evaluate_rollout_gate,
    load_fixture,
    replay_turns,
    run_thread,
)

EAST_RUN = FIXTURE_ROOT / "threads" / "east_run.json"
KRIA_TURNS = Path(__file__).resolve().parents[2] / "fixtures" / "kria_turns"


def _east_run() -> RFFixture:
    return load_fixture(EAST_RUN)


def test_v1_turn_runs_the_recorded_model_text_through_the_real_copilot_path():
    turns = {t.turn_id: t for t in replay_turns(_east_run())}

    brief = turns["t1-brief"]
    assert brief.reply == "Edit text. Everything else is unchanged."
    assert "copilot outcome=proposed" in brief.notes
    labels = [t.text for t in brief.plan_after.texts if t.role == "label"]
    assert labels == [
        "Arnavutköy waterfront & Bosphorus views",
        "Dolmabahçe Palace & Historic Clock Tower",
        "Eminönü finish line & harbor skyline",
    ]
    assert brief.plan_after.title().text == "20k run from arnavutköy to eminönü"
    # Ops only touch text; the clips are exactly the ones already on the timeline.
    assert brief.plan_after.clips == turns["t0-suggest"].plan_after.clips


def test_unsupported_turns_leave_the_edit_untouched_and_use_the_honest_outcome_reply():
    turns = {t.turn_id: t for t in replay_turns(_east_run())}
    before = turns["t1-brief"].plan_after
    for turn_id in ("t2-recreate", "t3-keep15"):
        assert turns[turn_id].plan_after == before
        assert "copilot outcome=unsupported" in turns[turn_id].notes
    assert turns["t3-keep15"].reply == "I can't change that on this edit yet."


def test_style_turn_moves_the_font_through_compile_editor_ops():
    turns = {t.turn_id: t for t in replay_turns(_east_run())}
    assert turns["t5-brief-again"].plan_after.title().font_family == "Fraunces"
    assert turns["t6-no-fraunces"].plan_after.title().font_family == "Inter"
    assert turns["t6-no-fraunces"].reply == "Patch text style. Everything else is unchanged."


def test_recorded_turn_plan_comes_straight_from_the_fixture():
    fixture = _east_run()
    by_id = {t.turn_id: t for t in replay_turns(fixture)}
    replan = next(t for t in fixture.turns if t.turn_id == "t4-replan")
    assert by_id["t4-replan"].plan_after == replan.recorded.plan_after
    assert len(by_id["t4-replan"].plan_after.clips) == 11


def test_compile_rejection_keeps_the_edit_and_uses_the_generic_reply():
    """`compile_editor_ops` refusing an op must not change the plan or invent a reply."""
    fixture = _east_run()
    turn = next(t for t in fixture.turns if t.turn_id == "t6-no-fraunces")
    bad = json.loads(turn.copilot["raw_text"])
    # Removing the same bar twice: the model proposes it, the real compiler refuses it.
    bad["ops"] = [{"op": "remove_text", "bar_index": 0}] * 2
    turn.copilot = {**turn.copilot, "raw_text": json.dumps(bad)}
    results = replay_turns(fixture)
    result = next(t for t in results if t.turn_id == "t6-no-fraunces")
    prior = next(t for t in results if t.turn_id == "t5-brief-again")
    assert any(n.startswith("compile rejected") for n in result.notes)
    assert result.plan_after == prior.plan_after
    assert result.reply == "I couldn't safely apply that change. Nothing was changed."


def test_v2_turns_replay_as_one_thread_and_reply_from_the_kria_trace():
    matcha = load_kria_fixture(KRIA_TURNS / "nermin-matcha-update.json")
    travel = load_kria_fixture(KRIA_TURNS / "nermin-travel-diary.json")
    plan = FinalPlan(clips=[PlanClip(clip_id="a", start_s=0, end_s=2)])
    fixture = RFFixture(
        fixture_id="v2-thread",
        provenance="authored",
        footage="east_run",
        turns=[
            Turn(
                turn_id=f"v2-{i}",
                user_message=f.user_message,
                engine="v2_kria",
                kria=f.model_dump(mode="json"),
                recorded={"plan_after": plan.model_dump(), "reply": "ignored"},
            )
            for i, f in enumerate((matcha, travel))
        ],
        requirements=[],
        reference={"plan_after": plan.model_dump()},
    )
    results = replay_turns(fixture)
    assert results[0].reply == matcha.expected_message
    assert results[1].reply and results[1].reply != "ignored"
    assert results[1].plan_after == plan


def test_a_turn_without_a_recording_carries_the_edit_forward_and_marks_the_thread():
    fixture = _east_run()
    fixture.turns[4].recorded = None
    results = replay_turns(fixture)
    assert results[4].unrecorded and results[4].reply is None
    assert results[4].plan_after == results[3].plan_after
    thread = run_thread(fixture)
    assert thread.unrecorded and thread.scores == []


def test_live_mode_uses_the_default_model_client_instead_of_the_recording(monkeypatch):
    """Live is opt-in; verify the seam without a network by swapping `default_client`."""
    from tests.evals.runners.eval_runner import CassetteModelClient

    seen: list[str] = []
    fixture = _east_run()
    turn = next(t for t in fixture.turns if t.turn_id == "t3-keep15")
    recorded = turn.copilot["raw_text"]
    turn.copilot = {**turn.copilot, "raw_text": "THIS RECORDING MUST NOT BE USED"}

    def fake_default_client():
        seen.append("default_client")
        return CassetteModelClient(recorded)

    monkeypatch.setattr("app.agents._model_client.default_client", fake_default_client)
    live = [t for t in replay_turns(fixture, mode="live") if t.turn_id == "t3-keep15"][0]
    v1_turns = [t for t in fixture.turns if t.engine == "v1_copilot"]
    assert len(seen) == len(v1_turns)  # one live client per copilot turn, none from recordings
    assert live.reply == "I can't change that on this edit yet."
    with pytest.raises(Exception):  # noqa: B017 - the poisoned recording is unparseable
        replay_turns(fixture, mode="replay")


def test_scored_plan_text_roles_survive_an_editor_round_trip():
    fixture = _east_run()
    turn = next(t for t in replay_turns(fixture) if t.turn_id == "t1-brief")
    assert [t.role for t in turn.plan_after.texts] == ["title", "label", "label", "label"]
    assert all(isinstance(t, PlanText) for t in turn.plan_after.texts)


def test_projection_retains_compiled_geometry_and_source_ranges():
    plan = FinalPlan(texts=[PlanText(id="title", role="title", text="old", start_s=0, end_s=2)])
    projected = _plan_with_texts(
        plan,
        [
            {
                "id": "title",
                "text": "new",
                "start_s": 0,
                "end_s": 5,
                "position": "custom",
                "alignment": "left",
                "x_frac": 0.2,
                "y_frac": 0.15,
                "source_start_s": 1.5,
                "source_end_s": 6.5,
            }
        ],
    )
    text = projected.texts[0]
    assert (text.position, text.alignment, text.x_frac, text.y_frac) == (
        "custom",
        "left",
        0.2,
        0.15,
    )
    assert (text.source_start_s, text.source_end_s) == (1.5, 6.5)

    clips = _plan_with_timeline_slots(
        FinalPlan(),
        [
            {
                "media_id": "source-a",
                "in_s": 2.0,
                "duration_s": 3.0,
                "output_start_s": 0.0,
                "output_end_s": 3.0,
            }
        ],
    )
    assert clips.clips[0].clip_id == "source-a"
    assert (clips.clips[0].source_start_s, clips.clips[0].source_end_s) == (2.0, None)


def test_projection_preserves_generative_intro_as_title_identity():
    plan = FinalPlan()
    projected = _plan_with_texts(
        plan,
        [{"id": "title", "role": "generative_intro", "text": "Run", "start_s": 0, "end_s": 4}],
    )
    assert projected.texts[0].role == "title"


def test_v2_cassette_compiles_the_actual_plan_instead_of_using_recorded_plan_after():
    fixture = load_fixture(FIXTURE_ROOT / "threads" / "v2_food_title_cassette.json")
    turn = replay_turns(fixture)[0]
    assert turn.plan_after.title().text == "Kadıköy'de Pazar"
    assert turn.plan_after != fixture.turns[0].recorded.plan_after
    assert turn.execution_proof is not None
    assert turn.execution_proof.execution_path == "compile_only"
    assert turn.execution_proof.model_call_observed
    assert turn.execution_proof.prompt_hash and turn.execution_proof.compiler_hash
    assert any(
        "unexecuted: stage/save/DB dispatch/device apply/render/export" == note
        for note in turn.notes
    )


def test_v2_cassette_scores_compiled_output_but_never_claims_rollout_or_render_proof():
    fixture = load_fixture(FIXTURE_ROOT / "threads" / "v2_food_title_cassette.json")
    result = run_thread(fixture)
    assert [score.status for score in result.scores] == ["met"]
    assert result.evidence and result.evidence.execution_path == "compile_only"
    assert result.evidence.replay_only
    assert not result.rollout_eligible
    assert evaluate_rollout_gate([result]).status == "incomplete"


def test_v2_strategy_cassette_compiles_an_inert_plan_without_fabricating_a_timeline():
    source = json.loads(
        (
            Path(__file__).resolve().parents[2]
            / "fixtures"
            / "agent_evals"
            / "main_creator"
            / "kri129_caption_food_and_weather.json"
        ).read_text(encoding="utf-8")
    )
    fixture = RFFixture(
        fixture_id="v2-strategy-cassette",
        provenance="authored",
        footage="food_day",
        turns=[
            Turn(
                turn_id="strategy",
                user_message=source["input"]["user_message"],
                engine="v2_kria",
                kria={
                    "strategy_cassette": {
                        "agent_input": source["input"],
                        "model_output": source["raw_text"],
                    }
                },
                expected_outcome={"plan_after": FinalPlan().model_dump(), "reply": None},
            )
        ],
        requirements=[],
        reference={"plan_after": FinalPlan().model_dump()},
    )
    result = replay_turns(fixture)[0]
    assert result.plan_after == FinalPlan()
    assert result.execution_proof and result.execution_proof.execution_path == "compile_only"
    assert any("inert CreatorEditPlan" in note for note in result.notes)
    assert any(
        "unexecuted: draft persistence/DB dispatch/render/device export" == note
        for note in result.notes
    )


def test_v2_six_captions_then_one_correction_changes_only_the_target_label():
    fixture = load_fixture(FIXTURE_ROOT / "threads" / "v2_six_captions_one_correction.json")
    result = run_thread(fixture)
    captions, correction = result.turns[-2:]
    assert [text.text for text in captions.plan_after.texts] == [
        "Simit and tea",
        "Turkish coffee",
        "Spice bazaar",
        "Lahmacun",
        "Baklava",
        "Ferry snack",
    ]
    assert correction.plan_after.clips == captions.plan_after.clips
    changed = [
        after.id
        for before, after in zip(
            captions.plan_after.texts, correction.plan_after.texts, strict=True
        )
        if before != after
    ]
    assert changed == ["c3"]
    assert correction.plan_after.texts[2].text == "Spice market"
    assert [score.status for score in result.scores] == ["met", "met"]


def test_v2_unsupported_talking_and_slides_keep_unsaved_editor_state_and_reply_honestly():
    fixture = load_fixture(FIXTURE_ROOT / "threads" / "v2_talking_slides_unsupported.json")
    result = run_thread(fixture)
    seed, talking, slides = result.turns
    assert talking.plan_after == seed.plan_after == slides.plan_after
    assert "not available" in talking.reply.lower()
    assert "not supported" in slides.reply.lower()
    assert [score.status for score in result.scores] == ["met", "met"]


def test_v2_later_cassette_cannot_repair_an_earlier_compile_result():
    fixture = load_fixture(FIXTURE_ROOT / "threads" / "v2_six_captions_one_correction.json")
    first = fixture.turns[1].kria["cassette"]["model_output"]["ops"]
    first[0]["text"] = "Unexpected first caption"
    results = replay_turns(fixture)
    assert results[-1].plan_after.texts[0].text == "Unexpected first caption"
    assert results[-1].plan_after.texts[2].text == "Spice market"
    assert results[-2].plan_after.texts[2].text == "Spice bazaar"
