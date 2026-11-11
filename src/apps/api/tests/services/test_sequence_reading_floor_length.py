"""The approved length plan never undercuts the text it must show (KRI-1365).

A creator approved a 42-word English sentence and a 26-word Turkish one, word by word, for a
fast montage. The fast pace cut the plan to a few seconds; a piece cannot stay on screen for less
than 0.25 s, so the sentence could not be read inside the video it was planned into.

Failure modes, written before the fix:
 1. fast pace, no length stated: the plan is at least the longest sentence's minimum read time;
 2. the creator's own length is honoured, never stretched for the text (the text pass reports it);
 3. footage too short for the floor: no new conflict, the plan is what the footage holds;
 4. no word-by-word ask: the plan is exactly what it was;
 5. phrase and build-up asks read in the composer's pieces: they set no word floor.
Boundary: the planner's own `_enrich_best_parts_snapshot`, the plan the approval card pins.
"""

from __future__ import annotations

import pytest

from app.kria.brief import BriefRequirement, CreativeBrief
from app.kria.planner import _enrich_best_parts_snapshot

WORDS_42 = " ".join(f"w{i}" for i in range(42))
STRATEGY = {
    "edit_format": "montage",
    "audio_strategy": "original_audio",
    "pacing": "fast",
    "pacing_requested": True,
}


def _media(count: int = 6, duration_s: float = 4.0) -> dict:
    return {
        "clip_assignments": [
            {
                "media_id": f"c{i}",
                "gcs_path": f"c{i}.mp4",
                "generation": "1",
                "duration_s": duration_s,
                "kind": "video",
            }
            for i in range(count)
        ]
    }


def _brief(*, unit: str = "word", mode: str = "agent_choice", length_s=None) -> CreativeBrief:
    reqs = [
        BriefRequirement(
            id="r1",
            kind="text_sequence",
            scope="title",
            literal=WORDS_42,
            facts={"unit": unit, "mode": mode, "position": "bottom"},
        )
    ]
    if length_s is not None:
        reqs.append(
            BriefRequirement(id="r2", kind="timing", scope="global", facts={"duration_s": length_s})
        )
    return CreativeBrief(version=1, requirements=reqs)


def _planned(brief, *, strategy: dict = STRATEGY, media=None) -> tuple[float, list]:
    plan = _enrich_best_parts_snapshot(strategy, media or _media(), brief)["length_plan"]
    total = sum(w["source_end_s"] - w["source_start_s"] for w in plan["windows"])
    return round(total, 3), plan["conflicts"]


def test_a_fast_plan_is_at_least_the_longest_sentences_minimum_read_time() -> None:
    plain, _ = _planned(None)
    planned, conflicts = _planned(_brief())

    assert plain < 42 * 0.25  # the premise: the fast plan alone cannot show the sentence
    assert planned >= 42 * 0.25 - 1e-6
    assert conflicts == []


def test_a_length_the_creator_stated_is_never_stretched_for_the_text() -> None:
    planned, _ = _planned(_brief(length_s=6))

    assert planned == pytest.approx(6.0, abs=0.1)


def test_footage_shorter_than_the_floor_adds_no_conflict() -> None:
    planned, conflicts = _planned(_brief(), media=_media(count=2, duration_s=4.0))

    assert planned == pytest.approx(8.0, abs=0.1)
    assert conflicts == []


def test_no_word_sequence_plans_exactly_as_before() -> None:
    nothing = CreativeBrief(
        version=1,
        requirements=[
            BriefRequirement(id="r1", kind="text", scope="title", literal="Hello there friend")
        ],
    )

    assert _planned(nothing) == _planned(None)


@pytest.mark.parametrize("facts", [{"unit": "phrase"}, {"unit": "word", "mode": "cumulative"}])
def test_phrase_and_build_up_asks_set_no_word_floor(facts) -> None:
    brief = _brief(unit=facts["unit"], mode=facts.get("mode", "agent_choice"))

    assert _planned(brief) == _planned(None)
