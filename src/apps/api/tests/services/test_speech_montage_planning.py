"""KRI-282: planning + grounding of a spoken-excerpt montage."""

from __future__ import annotations

import json

import pytest

from app.agents._runtime import SchemaError
from app.agents.speech_excerpt_planner import (
    SpeechExcerptPlannerAgent,
    SpeechExcerptPlannerInput,
)
from app.schemas.speech_montage import SpeechMontagePlan
from app.services.speech_montage_planning import (
    NO_SPEECH_QUESTION,
    SpeechCandidate,
    mentions_speech,
    plan_speech_montage,
)

SPEECH = (
    "So the thing I learned in Lisbon was, never rush a good espresso. "
    "Honestly it changed my whole morning routine! "
    "The tram up the hill is older than my grandmother. "
    "Anyway, we left the next day."
)


def _words(text: str = SPEECH) -> list[dict]:
    words, t = [], 0.0
    for token in text.split():
        words.append({"text": token, "start_s": round(t, 3), "end_s": round(t + 0.3, 3)})
        t += 0.4
    return words


def _speaker(media_id: str = "talk", *, to_camera: bool = True, text: str = SPEECH):
    return SpeechCandidate(
        media_id=media_id,
        duration_s=60.0,
        summary="creator talking to camera",
        has_speech=True,
        to_camera=to_camera,
    ), _words(text)


def _others(n: int) -> list[SpeechCandidate]:
    return [
        SpeechCandidate(media_id=f"b{i}", duration_s=7.0, summary=f"scene {i}") for i in range(n)
    ]


class _Stub:
    """A planner stub that records what the planner was shown."""

    def __init__(self, plan: SpeechMontagePlan | Exception) -> None:
        self.plan = plan
        self.seen: list[SpeechExcerptPlannerInput] = []

    def __call__(self, planner_input: SpeechExcerptPlannerInput) -> SpeechMontagePlan:
        self.seen.append(planner_input)
        if isinstance(self.plan, Exception):
            raise self.plan
        return self.plan


def _plan(*sections: dict, question: str | None = None) -> SpeechMontagePlan:
    return SpeechMontagePlan.model_validate(
        {"wants_speech_excerpts": True, "sections": list(sections), "question": question}
    )


def _run(candidates, plan, words_by_id=None, request="Play my lines over the clips", **kw):
    words_by_id = words_by_id or {}
    stub = plan if isinstance(plan, _Stub) else _Stub(plan)
    result = plan_speech_montage(
        creator_request=request,
        candidates=candidates,
        run_planner=stub,
        load_words=lambda c: (words_by_id.get(c.media_id, []), "en"),
        **kw,
    )
    return result, stub


def test_fifty_assets_and_a_long_prompt_yield_multiple_grounded_excerpts() -> None:
    speaker, words = _speaker()
    candidates = [speaker, *_others(49)]
    assert len(candidates) == 50
    long_prompt = (
        "Make a fast, energetic montage of my Lisbon trip using ALL of these clips. "
        + "Label each place with the city name, keep the cuts under a second. " * 40
        + "Play the line about the espresso over the clips, then cut to me saying the part "
        "about my morning routine, go back to fast cuts, and end on the tram line."
    )
    assert len(long_prompt) > 2500
    result, stub = _run(
        candidates,
        _plan(
            {"kind": "montage", "duration_s": 4.0, "cut_s": 0.8},
            {
                "kind": "speech",
                "clip_ref": "c1",
                "quote": "never rush a good espresso",
                "visual": "cutaways",
            },
            {
                "kind": "speech",
                "clip_ref": "c1",
                "quote": "it changed my whole morning routine",
                "visual": "speaker",
            },
            {"kind": "montage", "duration_s": 5.0},
            {
                "kind": "speech",
                "clip_ref": "c1",
                "quote": "The tram up the hill ... my grandmother",
                "visual": "cutaways",
            },
        ),
        {"talk": words},
        request=long_prompt,
        target_duration_s=40,
    )
    assert result.status == "ready"
    assert [s.kind for s in result.sections] == ["montage", "speech", "speech", "montage", "speech"]
    assert [s.visual for s in result.sections if s.kind == "speech"] == [
        "cutaways",
        "speaker",
        "cutaways",
    ]
    speech = [s for s in result.sections if s.kind == "speech"]
    # Excerpts keep source order and never overlap.
    assert all(a.source_end_s <= b.source_start_s for a, b in zip(speech, speech[1:]))
    assert all(s.media_id == "talk" and s.source_end_s - s.source_start_s >= 0.8 for s in speech)
    seen = stub.seen[0]
    assert len(seen.other_clips) == 49 and [c.ref for c in seen.speech_clips] == ["c1"]
    assert seen.speech_clips[0].segments and seen.target_duration_s == 40


def test_request_that_does_not_use_speech_is_left_to_the_ordinary_montage() -> None:
    speaker, words = _speaker()
    result, _ = _run(
        [speaker, *_others(3)],
        SpeechMontagePlan(wants_speech_excerpts=False),
        {"talk": words},
        request="Just cut them fast and add city names",
    )
    assert result.status == "not_requested" and not result.sections


def test_no_speech_clip_and_no_hint_never_calls_the_planner() -> None:
    stub = _Stub(RuntimeError("must not be called"))
    result, _ = _run(_others(5), stub, request="fast montage, 1s per clip")
    assert result.status == "not_requested" and not stub.seen


def test_missing_speech_asks_a_specific_question() -> None:
    result, stub = _run(
        _others(5),
        _plan({"kind": "speech", "clip_ref": "c1", "quote": "anything"}),
        request="Play what I say about the trip over the clips",
    )
    assert result.status == "needs_creator" and result.question == NO_SPEECH_QUESTION
    assert stub.seen and not stub.seen[0].speech_clips


def test_a_clip_with_too_little_speech_is_not_a_speech_source() -> None:
    speaker, _ = _speaker()
    result, stub = _run(
        [speaker, *_others(2)],
        SpeechMontagePlan(wants_speech_excerpts=True, question="Which line?"),
        {"talk": _words("hmm yeah ok")},
    )
    assert not stub.seen[0].speech_clips
    assert result.status == "needs_creator"


def test_ambiguous_speaker_surfaces_the_planners_question_and_shows_both_clips() -> None:
    a, words_a = _speaker("me")
    b, words_b = _speaker("friend", text="This tram is older than my grandmother, honestly it is.")
    result, stub = _run(
        [a, b, *_others(3)],
        SpeechMontagePlan(
            wants_speech_excerpts=True,
            question="Two clips have someone talking: you and your friend. Whose words?",
        ),
        {"me": words_a, "friend": words_b},
    )
    assert [c.ref for c in stub.seen[0].speech_clips] == ["c1", "c2"]
    assert result.status == "needs_creator" and "Whose words" in (result.question or "")


def test_ungroundable_quote_names_the_quote_instead_of_a_generic_error() -> None:
    speaker, words = _speaker()
    result, _ = _run(
        [speaker, *_others(2)],
        _plan(
            {"kind": "speech", "clip_ref": "c1", "quote": "I quit my job to become an astronaut"}
        ),
        {"talk": words},
    )
    assert result.status == "needs_creator"
    assert "I quit my job to become an astronaut" in (result.question or "")
    assert "restate which clips" not in (result.question or "")
    assert result.dropped[0]["reason"] == "not_said"


def test_one_ungroundable_quote_is_dropped_and_reported_while_the_rest_render() -> None:
    speaker, words = _speaker()
    result, _ = _run(
        [speaker, *_others(2)],
        _plan(
            {"kind": "speech", "clip_ref": "c1", "quote": "never rush a good espresso"},
            {"kind": "speech", "clip_ref": "c1", "quote": "quantum physics of tennis"},
        ),
        {"talk": words},
    )
    assert result.status == "ready"
    assert len([s for s in result.sections if s.kind == "speech"]) == 1
    assert result.dropped == [{"quote": "quantum physics of tennis", "reason": "not_said"}]
    assert any("quantum physics" in note for note in result.adjustments)


def test_speech_over_broll_without_other_footage_shows_the_speaker_instead() -> None:
    speaker, words = _speaker()
    result, _ = _run(
        [speaker],
        _plan(
            {
                "kind": "speech",
                "clip_ref": "c1",
                "quote": "never rush a good espresso",
                "visual": "cutaways",
            },
            {"kind": "montage", "duration_s": 3.0},
        ),
        {"talk": words},
    )
    assert result.status == "ready"
    assert [s.kind for s in result.sections] == ["speech"]
    assert result.sections[0].visual == "speaker" and result.adjustments


def test_same_quote_twice_resolves_to_successive_utterances() -> None:
    speaker, _ = _speaker()
    text = "buy it now because it is great and honestly you should buy it now today"
    result, _ = _run(
        [speaker, *_others(2)],
        _plan(
            {"kind": "speech", "clip_ref": "c1", "quote": "buy it now"},
            {"kind": "speech", "clip_ref": "c1", "quote": "buy it now today"},
        ),
        {"talk": _words(text)},
    )
    speech = [s for s in result.sections if s.kind == "speech"]
    assert len(speech) == 2 and speech[0].source_end_s <= speech[1].source_start_s


def test_unsupported_request_is_asked_not_guessed() -> None:
    speaker, words = _speaker()
    result, _ = _run(
        [speaker, *_others(2)],
        SpeechMontagePlan(
            wants_speech_excerpts=True,
            question=(
                "I can only play your own words, not translate them into French. "
                "Keep the originals?"
            ),
        ),
        {"talk": words},
        request="Translate what I say into French and play it over the clips",
    )
    assert result.status == "needs_creator" and "translate" in (result.question or "")


def test_planner_failure_is_a_question_when_speech_was_asked_and_silent_otherwise() -> None:
    speaker, words = _speaker()
    asked, _ = _run(
        [speaker, *_others(2)],
        _Stub(RuntimeError("boom")),
        {"talk": words},
        request="Play my best line over the clips",
    )
    assert asked.status == "needs_creator" and asked.question
    quiet, _ = _run(
        [speaker, *_others(2)], _Stub(RuntimeError("boom")), {"talk": words}, request="fast cuts"
    )
    assert quiet.status == "not_requested"


def test_a_clip_that_fails_to_transcribe_is_skipped_not_fatal() -> None:
    speaker, _ = _speaker()

    def boom(_c):
        raise OSError("download failed")

    stub = _Stub(SpeechMontagePlan(wants_speech_excerpts=True, question="Which line?"))
    result = plan_speech_montage(
        creator_request="play my lines",
        candidates=[speaker, *_others(2)],
        run_planner=stub,
        load_words=boom,
    )
    assert not stub.seen[0].speech_clips and result.status == "needs_creator"


def test_mentions_speech_hint_is_multilingual() -> None:
    assert mentions_speech("play what I say") and mentions_speech("konuşmamı kullan")
    assert not mentions_speech("fast cuts with city names")


# --- the agent's parse() is the hallucination fence -------------------------


def _agent_input() -> SpeechExcerptPlannerInput:
    return SpeechExcerptPlannerInput.model_validate(
        {
            "creator_request": "play my lines",
            "speech_clips": [{"ref": "c1", "duration_s": 10, "segments": []}],
            "other_clips": [{"ref": "v1", "duration_s": 5}],
        }
    )


def _parse(payload: dict):
    agent = SpeechExcerptPlannerAgent.__new__(SpeechExcerptPlannerAgent)
    return agent.parse(json.dumps(payload), _agent_input())


def test_parse_accepts_a_valid_plan_and_dedupes_repeated_excerpts() -> None:
    plan = _parse(
        {
            "wants_speech_excerpts": True,
            "sections": [
                {"kind": "speech", "clip_ref": "c1", "quote": "a b c", "visual": "cutaways"},
                {"kind": "speech", "clip_ref": "c1", "quote": "A B C"},
                {"kind": "montage", "duration_s": 99, "cut_s": 0.1},
            ],
        }
    )
    assert len(plan.speech_sections()) == 1
    montage = plan.sections[-1]
    assert montage.duration_s == 20.0 and montage.cut_s == 0.4  # clamped, not rejected


@pytest.mark.parametrize(
    "payload",
    [
        {
            "wants_speech_excerpts": True,
            "sections": [{"kind": "speech", "clip_ref": "v1", "quote": "x"}],
        },
        {"wants_speech_excerpts": True, "sections": [{"kind": "montage", "duration_s": 3}]},
        {"wants_speech_excerpts": True, "sections": [{"kind": "dance"}]},
        {
            "wants_speech_excerpts": True,
            "question": "Which?",
            "sections": [{"kind": "speech", "clip_ref": "c1", "quote": "x"}],
        },
    ],
)
def test_parse_rejects_hallucinated_refs_and_inconsistent_plans(payload: dict) -> None:
    with pytest.raises(SchemaError):
        _parse(payload)


def test_parse_not_wanted_and_question_shapes() -> None:
    assert _parse({"wants_speech_excerpts": False, "sections": []}).wants_speech_excerpts is False
    ask = _parse({"wants_speech_excerpts": True, "sections": [], "question": "Whose words?"})
    assert ask.question == "Whose words?" and not ask.sections
