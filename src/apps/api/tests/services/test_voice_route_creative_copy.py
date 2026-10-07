"""KRI-479 x KRI-506: the voice route never renders wording the creator did not approve.

Two layers, both independent of the route:

* the #1466 gate (`creative_copy_problem`) judges the STRATEGY at draft commitment and at render
  approval, so a continuous-voice plan with an unapproved candidate title is stopped exactly like
  any other plan;
* the voice runner takes its title words ONLY from the pinned contract's approved `exact_texts`
  (derived from `strategy.opening_title`), never from a diverging strategy value, the request
  text or a brief literal, so a candidate that slipped past cannot reach the screen.
"""

from __future__ import annotations

from app.services.creative_copy_decisions import media_digest, wording_question
from app.services.creative_copy_gate import creative_copy_problem
from tests.services.test_phone_voice_behind_footage_job import (
    SECRET,
    _recipe,
    _run,
    _strategy,
    _world,
)

MEDIA = {"clip_paths": ["owner/talk.mp4"], "clip_assignments": []}
CANDIDATE = "A day worth remembering"


def _wording_events(*, approved: bool):
    question = wording_question(
        target="opening_title", candidate=CANDIDATE, dependency_digest=media_digest(MEDIA)
    )
    events = [("assistant", {"choice_question": question})]
    if approved:
        events.append(
            (
                "user",
                {
                    "choice_selection": {
                        "question_id": question["question_id"],
                        "option_key": "approve",
                    }
                },
            )
        )
    return events


def _voice_strategy(title):
    return {
        "voice_mode": "continuous",
        "audio_strategy": "original_audio",
        "montage_audio": {"preserve_source_audio": True, "source_media_ids": ["talk"]},
        "ordering_choice": "chronological",
        "opening_title": title,
    }


def test_an_unapproved_candidate_title_blocks_a_continuous_voice_plan():
    assert creative_copy_problem(
        _wording_events(approved=False), MEDIA, strategy=_voice_strategy(CANDIDATE)
    )
    assert creative_copy_problem(
        _wording_events(approved=False), MEDIA, strategy=_voice_strategy(None)
    )


def test_only_the_approved_words_pass_for_a_continuous_voice_plan():
    events = _wording_events(approved=True)
    assert creative_copy_problem(events, MEDIA, strategy=_voice_strategy(CANDIDATE)) is None
    assert creative_copy_problem(events, MEDIA, strategy=_voice_strategy("Model rewrite"))
    assert creative_copy_problem(events, MEDIA, strategy=_voice_strategy(None))


def _texts(world):
    recipe = _recipe(world)
    return [" ".join(run.text for run in layer.runs).split() for layer in recipe.text_layers]


def test_the_runner_renders_the_contracts_approved_words_not_a_diverging_strategy_value(
    monkeypatch,
):
    world = _world(monkeypatch, strategy=_strategy(opening_title="Approved words here"))
    assert world.contract.exact_texts[0].text == "Approved words here"
    # A candidate that never got approved but sits in the persisted strategy / request:
    world.candidates["creator_strategy"] = {
        **world.candidates["creator_strategy"],
        "opening_title": CANDIDATE,
    }
    world.candidates["creator_request"] = f"title it {CANDIDATE} {SECRET}"
    assert _run(world) is True
    assert _texts(world) == [["Approved", "words", "here"]]


def test_a_title_that_is_not_in_the_contract_is_never_rendered(monkeypatch):
    plain = _strategy()
    plain.pop("opening_title", None)
    plain.pop("opening_title_duration_s", None)
    world = _world(monkeypatch, strategy=plain)
    assert world.contract.exact_texts == ()
    world.candidates["creator_strategy"] = {**plain, "opening_title": CANDIDATE}
    world.candidates["creator_request"] = f"title it {CANDIDATE}"
    assert _run(world) is True
    assert _recipe(world).text_layers == []
