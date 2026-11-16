"""An agent run that ends ok but drops part of its answer reports it on the agent_run row.

KRI-1429. Failure modes, written before the code:

1. the editorial plan Main Creator cannot run is dropped at parse and nothing records it
2. the music matcher skips ids the library does not hold and the run reads as clean
3. a clean answer is reported as a fallback (false positive), or the dump of a clean answer
   changes (it must stay byte-identical: no `fallbacks` key)
4. the field leaks into the schema the model is shown
5. the fallback is counted on a failed run (it belongs to answers that stood)

Each test runs the real agent through `Agent.run` and reads what the runtime persists.
"""

from __future__ import annotations

import json
import uuid
from pathlib import Path

import pytest

from app.agents import _persistence
from app.agents._runtime import RunContext
from app.agents.main_creator import MainCreatorAgent, MainCreatorOutput
from app.agents.music_matcher import MusicMatcherAgent, MusicMatcherOutput
from tests.evals.runners.eval_runner import CassetteModelClient

FIXTURES = Path(__file__).resolve().parents[1] / "fixtures" / "agent_evals"


def _fixture(agent_dir: str, name: str) -> dict:
    found = next((FIXTURES / agent_dir).rglob(f"{name}.json"))
    return json.loads(found.read_text())


@pytest.fixture
def persisted(monkeypatch) -> list[dict]:
    rows: list[dict] = []
    monkeypatch.setattr(_persistence, "persist_agent_run", lambda **row: rows.append(row))
    return rows


def _run(agent_cls, fixture: dict, raw_text: str | None = None):
    agent = agent_cls(CassetteModelClient(raw_text or fixture["raw_text"]))
    ctx = RunContext(job_id=str(uuid.uuid4()), request_id="behavioural-fallback-test")
    return agent.run(fixture["input"], ctx=ctx)


def test_a_dropped_editorial_plan_is_recorded_on_the_run(persisted) -> None:
    fixture = _fixture("main_creator", "kri806_don_julio_kept_lines")

    output = _run(MainCreatorAgent, fixture)

    assert [(f.kind, f.detail) for f in output.fallbacks] == [
        ("editorial_plan_dropped", "montage_audio")
    ]
    assert persisted[-1]["outcome"] == "ok"
    assert persisted[-1]["fallback_kind"] == "editorial_plan_dropped"
    # Stored and wire forms of the answer do not change: the field is never dumped.
    assert "fallbacks" not in output.model_dump(mode="json")


def test_a_clean_main_creator_answer_reports_nothing_and_dumps_as_before(persisted) -> None:
    fixture = _fixture("main_creator", "kri598_tigre_use_all_clips")

    output = _run(MainCreatorAgent, fixture)

    assert output.fallbacks == []
    assert persisted[-1]["fallback_kind"] is None
    assert "fallbacks" not in output.model_dump()


def test_matcher_entries_the_parse_refuses_are_recorded_while_the_answer_stands(persisted) -> None:
    fixture = _fixture("music_matcher", "acoustic_morning_coffee")
    answer = json.loads(fixture["raw_text"])
    answer["ranked"].append(
        {
            "track_id": "not-in-the-library",
            "score": 9,
            "rationale": "invented",
            "predicted_strengths": [],
        }
    )

    output = _run(MusicMatcherAgent, fixture, json.dumps(answer))

    assert output.ranked
    assert [f.kind for f in output.fallbacks] == ["matcher_entries_dropped"]
    assert output.fallbacks[0].detail == "hallucinated=1,invalid=0"
    assert persisted[-1]["fallback_kind"] == "matcher_entries_dropped"


def test_a_clean_matcher_answer_reports_nothing(persisted) -> None:
    fixture = _fixture("music_matcher", "acoustic_morning_coffee")

    output = _run(MusicMatcherAgent, fixture)

    assert output.fallbacks == []
    assert persisted[-1]["fallback_kind"] is None
    assert "fallbacks" not in output.model_dump()


def test_the_field_is_not_part_of_the_schema_the_model_is_shown() -> None:
    for output in (MainCreatorOutput, MusicMatcherOutput):
        assert "fallbacks" not in json.dumps(output.model_json_schema())
