"""KRI-479: the clarification gate for a continuous voice (`voice_mode == "continuous"`).

Failure modes written against first:

* it asks when the request is clear (voice as long as the length, or longer: that is a
  disclosed trim, never a question);
* it silently renders a shorter / different video instead of asking when the voice is
  shorter than the asked length;
* a "silent tail" option is offered that the composer cannot execute, or an answer is lost
  (retry / re-sent prompt asks again);
* several candidate voices: the gate picks one for the creator, or asks for a plain
  excerpts montage that never needed the question;
* the voice clip is counted as one of the clips that must fit the length;
* the contract still rejects the plan the creator chose.
"""

from __future__ import annotations

import pytest

from app.services.choice_questions import (
    CONFLICT_VOICE_VS_DURATION,
    CONFLICT_WHICH_VOICE,
    collect_conflicts,
)
from app.services.creator_render_contract import (
    build_render_contract,
    commitments_from_strategy,
)
from tests.kria.test_choice_conflict_gate import (
    _asked,
    _brief,
    _gate,
    _order,
    _picked,
    _planned,
    _strategy,
    _timing,
)

VOICE = "talk"


def _voice_rows(*, voice_s: float, pictures: int = 6, speech: dict[str, str] | None = None):
    rows = [
        {
            "media_id": VOICE,
            "kind": "video",
            "duration_s": voice_s,
            "capture": {"capture_time": "2026-06-01T09:59:00Z"},
            "analysis": {
                "understanding": {
                    "speech": {"has_speech": True, "to_camera": True, "transcript": "hello there"}
                }
            },
        }
    ]
    for i in range(pictures):
        rows.append(
            {
                "media_id": f"c{i:02d}",
                "kind": "video",
                "duration_s": 12.0,
                "capture": {"capture_time": f"2026-06-01T{9 + i // 60:02d}:{i % 60:02d}:00Z"},
            }
        )
    for media_id, quote in (speech or {}).items():
        rows.append(
            {
                "media_id": media_id,
                "kind": "video",
                "duration_s": 30.0,
                "capture": {"capture_time": "2026-06-01T09:58:00Z"},
                "analysis": {
                    "understanding": {
                        "speech": {"has_speech": True, "to_camera": True, "transcript": quote}
                    }
                },
            }
        )
    return rows


def _voice_plan(seconds: int | None, **extra):
    return _planned(
        seconds=seconds,
        audio_strategy="original_audio",
        voice_mode="continuous",
        ordering_choice="chronological",
        montage_audio={"preserve_source_audio": True, "source_media_ids": [VOICE]},
        **extra,
    )


# --- must-not-ask controls ----------------------------------------------------------------


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("voice_s", "asked_s"),
    [(20.4, 20), (30.0, 20), (147.7, 30), (45.0, 30)],
)
async def test_a_voice_as_long_as_the_asked_length_or_longer_asks_nothing(
    monkeypatch, voice_s, asked_s
) -> None:
    result = await _gate(
        monkeypatch,
        _voice_plan(asked_s),
        rows=_voice_rows(voice_s=voice_s),
        brief=_brief(_timing(asked_s), _order()),
    )
    assert result.plan.mode == "act", result.plan.response


@pytest.mark.asyncio
async def test_an_implicit_length_with_a_short_voice_asks_nothing(monkeypatch) -> None:
    result = await _gate(
        monkeypatch,
        _voice_plan(None),
        rows=_voice_rows(voice_s=40.0),
        brief=_brief(_order()),
    )
    assert result.plan.mode == "act", result.plan.response


@pytest.mark.asyncio
async def test_a_plain_speech_montage_with_several_ids_is_not_questioned(monkeypatch) -> None:
    result = await _gate(
        monkeypatch,
        _planned(
            seconds=30,
            audio_strategy="original_audio",
            montage_audio={"preserve_source_audio": True, "source_media_ids": [VOICE, "c00"]},
        ),
        rows=_voice_rows(voice_s=60.0),
        brief=_brief(_timing(30)),
    )
    assert result.plan.mode == "act", result.plan.response


# --- voice_vs_duration ---------------------------------------------------------------------


@pytest.mark.asyncio
async def test_a_voice_shorter_than_the_asked_length_asks_with_two_executable_options(
    monkeypatch,
) -> None:
    result = await _gate(
        monkeypatch,
        _voice_plan(30),
        rows=_voice_rows(voice_s=20.0),
        brief=_brief(_timing(30), _order()),
    )
    question = result.plan.choice_question
    assert question["kind"] == CONFLICT_VOICE_VS_DURATION
    labels = [o["label"] for o in question["options"]]
    assert [o["key"] for o in question["options"]] == ["match_voice", "silent_tail"]
    assert "20" in labels[0] and "voice ends" in labels[0]
    assert "30" in labels[1] and "without voice" in labels[1]
    assert question["options"][0]["recommended"] is True
    assert "20" in result.plan.response and "30" in result.plan.response


@pytest.mark.asyncio
async def test_match_voice_rewrites_the_length_and_the_contract_pins_it(monkeypatch) -> None:
    brief, rows = _brief(_timing(30), _order()), _voice_rows(voice_s=20.0)
    first = await _gate(monkeypatch, _voice_plan(30), rows=rows, brief=brief)
    result = await _gate(
        monkeypatch,
        _voice_plan(30),
        rows=rows,
        brief=brief,
        events=(_asked(first), _picked(first.plan.choice_question, "match_voice")),
    )
    strategy = _strategy(result)
    assert strategy["target_duration_s"] == 20 and strategy["target_duration_requested"] is True
    assert strategy["choice_answers"][0]["option"] == "match_voice"
    contract = build_render_contract(
        strategy,
        generation_id="g",
        brief=brief,
        media_snapshot={"clip_assignments": rows},
        composition=commitments_from_strategy(strategy),
    )
    assert contract is not None and contract.duration_s == 20 and contract.unresolved == ()
    assert contract.order_ids == tuple(f"c{i:02d}" for i in range(6))  # voice clip left out


@pytest.mark.asyncio
async def test_silent_tail_keeps_the_length_and_commits_the_voice_span(monkeypatch) -> None:
    brief, rows = _brief(_timing(30), _order()), _voice_rows(voice_s=20.0)
    first = await _gate(monkeypatch, _voice_plan(30), rows=rows, brief=brief)
    result = await _gate(
        monkeypatch,
        _voice_plan(30),
        rows=rows,
        brief=brief,
        events=(_asked(first), _picked(first.plan.choice_question, "silent_tail")),
    )
    strategy = _strategy(result)
    assert strategy["target_duration_s"] == 30
    commitments = commitments_from_strategy(strategy, voice_duration_s=20.0)
    assert commitments is not None and commitments.voice_span_s == pytest.approx(19.95)
    contract = build_render_contract(
        strategy,
        generation_id="g",
        brief=brief,
        media_snapshot={"clip_assignments": rows},
        composition=commitments,
    )
    assert contract is not None and contract.duration_s == 30


@pytest.mark.asyncio
async def test_the_answer_survives_a_resent_prompt_and_a_new_voice_reopens_it(monkeypatch) -> None:
    brief, rows = _brief(_timing(30), _order()), _voice_rows(voice_s=20.0)
    first = await _gate(monkeypatch, _voice_plan(30), rows=rows, brief=brief)
    history = (_asked(first), _picked(first.plan.choice_question, "match_voice"))
    again = await _gate(monkeypatch, _voice_plan(30), rows=rows, brief=brief, events=history)
    assert again.plan.mode == "act"  # no second ask for the same inputs
    swapped = await _gate(
        monkeypatch, _voice_plan(30), rows=_voice_rows(voice_s=14.0), brief=brief, events=history
    )
    assert swapped.plan.choice_question["kind"] == CONFLICT_VOICE_VS_DURATION  # new digest


@pytest.mark.asyncio
async def test_a_long_voice_with_no_stated_length_asks_how_long(monkeypatch) -> None:
    result = await _gate(
        monkeypatch,
        _voice_plan(24),
        rows=_voice_rows(voice_s=147.7),
        brief=_brief(_order()),
    )
    question = result.plan.choice_question
    assert question["kind"] == CONFLICT_VOICE_VS_DURATION
    assert [o["key"] for o in question["options"]] == ["length_30", "length_60"]
    picked = await _gate(
        monkeypatch,
        _voice_plan(24),
        rows=_voice_rows(voice_s=147.7),
        brief=_brief(_order()),
        events=(_asked(result), _picked(question, "length_60")),
    )
    strategy = _strategy(picked)
    assert strategy["target_duration_s"] == 60 and strategy["target_duration_requested"] is True


# --- which_voice -----------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_two_named_voices_ask_which_and_the_answer_names_exactly_one(monkeypatch) -> None:
    rows = _voice_rows(voice_s=40.0, speech={"talk2": "I think that the second take is better"})
    plan = _planned(
        seconds=30,
        audio_strategy="original_audio",
        voice_mode="continuous",
        montage_audio={"preserve_source_audio": True, "source_media_ids": [VOICE, "talk2"]},
    )
    first = await _gate(monkeypatch, plan, rows=rows, brief=_brief(_timing(30)))
    question = first.plan.choice_question
    assert question["kind"] == CONFLICT_WHICH_VOICE
    assert len(question["options"]) == 2
    assert "second take" in " ".join(o["label"] for o in question["options"])
    key = next(o["key"] for o in question["options"] if "second take" in o["label"])
    result = await _gate(
        monkeypatch,
        plan,
        rows=rows,
        brief=_brief(_timing(30)),
        events=(_asked(first), _picked(question, key)),
    )
    audio = _strategy(result)["montage_audio"]
    assert audio["source_media_ids"] == ["talk2"] and audio["preserve_source_audio"] is True


@pytest.mark.asyncio
async def test_no_named_voice_with_several_speakers_asks_but_with_one_it_does_not(
    monkeypatch,
) -> None:
    plan = _planned(
        seconds=30,
        audio_strategy="original_audio",
        voice_mode="continuous",
        montage_audio={"preserve_source_audio": True},
    )
    two = _voice_rows(voice_s=40.0, speech={"talk2": "another take"})
    asked = await _gate(monkeypatch, plan, rows=two, brief=_brief(_timing(30)))
    assert asked.plan.choice_question["kind"] == CONFLICT_WHICH_VOICE
    one = _voice_rows(voice_s=40.0)
    quiet = await _gate(monkeypatch, plan, rows=one, brief=_brief(_timing(30)))
    assert quiet.plan.mode == "act"  # one speaker is not a question; nothing is guessed either


# --- the voice clip is not one of the clips that must fit -----------------------------------


def test_the_voice_clip_is_not_counted_against_the_readable_floor() -> None:
    from app.agents._schemas.creator_agent import CreativeStrategy

    strategy = CreativeStrategy(
        edit_format="montage",
        audio_strategy="original_audio",
        voice_mode="continuous",
        montage_audio={"preserve_source_audio": True, "source_media_ids": [VOICE]},
        target_duration_s=24,
        target_duration_requested=True,
    )
    snapshot = {"clip_assignments": _voice_rows(voice_s=60.0, pictures=30)}
    # 30 pictures x 0.8 s = 24 s fits exactly; counting the voice clip (31) would not.
    assert collect_conflicts(strategy, _brief(_timing(24)), snapshot) == []
    tight = strategy.model_copy(update={"target_duration_s": 20})
    kinds = [c.kind for c in collect_conflicts(tight, _brief(_timing(20)), snapshot)]
    assert kinds == ["duration_vs_count"]


@pytest.mark.asyncio
async def test_how_long_never_offers_a_length_the_clips_cannot_be_seen_in(monkeypatch) -> None:
    """41 clips need 32.8 s at the readable floor: 30 s would be a trap, so only 60 s is offered."""
    result = await _gate(
        monkeypatch,
        _voice_plan(24),
        rows=_voice_rows(voice_s=147.7, pictures=41),
        brief=_brief(_order()),
    )
    question = result.plan.choice_question
    assert [o["key"] for o in question["options"]] == ["length_60"]
    assert "32.8" in result.plan.response  # why only one length is on offer
    # Not even a minute holds 80 clips: no question here; the composer declines, typed.
    crowded = await _gate(
        monkeypatch,
        _voice_plan(24),
        rows=_voice_rows(voice_s=147.7, pictures=80),
        brief=_brief(_order()),
    )
    assert crowded.plan.mode == "act"
