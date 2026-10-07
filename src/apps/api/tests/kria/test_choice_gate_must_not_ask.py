"""KRI-476 (PR-C) must-not-ask gate: a clear request is never turned into a question.

Every recorded creator thread the repo owns (the 34 `request_following` threads, the
`kria_turns` brief fixtures, the 240-row `kria_format_matrix`) is run through the real
clarification gate (`planner._gate_unresolved_choices`) with the strictest plausible
reading of what the creator asked: every requirement of the thread in force at once, on
the thread's own footage, as a montage. A clear request must produce ZERO questions.

The inputs are derived, not recorded: the fixtures hold plans and footage facts, not a
live strategy/brief/snapshot. The derivation is deliberately the most question-prone one
(montage shape, every duration explicit, every chronological ask held to the footage's
own capture facts), so a pass here is a real bound, and a footage set that genuinely
cannot satisfy its ask is listed in ``LEGITIMATE_ASKS`` with the reason instead of being
quietly excused.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from app.kria.brief import BriefRequirement, CreativeBrief
from app.services.choice_questions import collect_conflicts
from tests.evals.request_following.runner import discover_fixture_paths, load_fixture, load_footage
from tests.kria.test_choice_conflict_gate import _gate, _planned

FIXTURES = Path(__file__).resolve().parents[1] / "fixtures"

# fixture id -> why asking is CORRECT (the footage genuinely cannot satisfy the ask).
LEGITIMATE_ASKS: dict[str, str] = {}

THREADS = [pytest.param(path, id=path.stem) for path in discover_fixture_paths()]


def _rows(footage) -> list[dict]:  # noqa: ANN001
    rows = []
    for clip in footage.clips:
        row: dict = {"media_id": clip.clip_id, "kind": "video", "duration_s": clip.duration_s}
        stamp = clip.fact("capture_time")
        if stamp is not None:
            row["capture"] = {"capture_time": stamp.value}
        rows.append(row)
    return rows


def _derive(fixture, footage) -> tuple[dict, CreativeBrief, list[dict]]:  # noqa: ANN001
    """The strategy kwargs, brief and snapshot rows a thread's requirements imply."""
    rows = _rows(footage)
    requirements: list[BriefRequirement] = []
    strategy: dict = {}
    clip_ids = [row["media_id"] for row in rows]
    for index, req in enumerate(fixture.requirements, start=1):
        rid = f"r{index}"
        if req.kind == "timing" and req.checker == "duration_within":
            seconds = float(req.params["target_s"])
            strategy["seconds"] = int(seconds) if seconds.is_integer() else seconds
            requirements.append(
                BriefRequirement(
                    id=rid,
                    kind="timing",
                    scope="global",
                    description=f"{seconds:g} seconds",
                    facts={"duration_s": seconds},
                )
            )
        elif req.kind == "order":
            key = str(req.params.get("key") or "explicit")
            requirements.append(
                BriefRequirement(
                    id=rid,
                    kind="order",
                    scope="global",
                    description=req.source or "an order",
                    facts={"key": {"route_rank": "route"}.get(key, key)},
                )
            )
        elif req.checker == "select_exclude":
            gone = set(req.params.get("clip_ids") or [])
            strategy |= {
                "media_scope": "selected",
                "selected_media_ids": [c for c in clip_ids if c not in gone],
            }
        elif req.checker == "select_include" and req.params.get("clip_ids"):
            strategy |= {
                "media_scope": "selected",
                "selected_media_ids": list(req.params["clip_ids"]),
            }
    return strategy, CreativeBrief(version=1, requirements=requirements), rows


def test_the_thread_corpus_is_the_34_the_plan_names() -> None:
    assert len(discover_fixture_paths()) == 34


@pytest.mark.asyncio
@pytest.mark.parametrize("path", THREADS)
async def test_a_clear_thread_asks_zero_questions(monkeypatch, path) -> None:
    fixture = load_fixture(path)
    strategy, brief, rows = _derive(fixture, load_footage(fixture.footage))
    result = await _gate(monkeypatch, _planned(**strategy), rows=rows, brief=brief)
    asked = result.plan.turn_value == "question"
    if fixture.fixture_id in LEGITIMATE_ASKS:
        assert asked, "stale LEGITIMATE_ASKS entry: this thread no longer asks"
    else:
        assert not asked, f"unnecessary question on a clear request: {result.plan.response!r}"
        assert result.plan.mode == "act"


def test_kria_turn_brief_fixtures_ask_zero_questions() -> None:
    """The recorded brief updates, applied in order, on dated clips of the fixture's size."""
    seen = 0
    for path in sorted((FIXTURES / "kria_turns").glob("*.json")):
        data = json.loads(path.read_text(encoding="utf-8"))
        if "turns" not in data or "clip_ids" not in data:
            continue
        rows = [
            {
                "media_id": clip_id,
                "kind": "video",
                "duration_s": 4.0,
                "capture": {"capture_time": f"2026-09-20T09:{i:02d}:00Z"},
            }
            for i, clip_id in enumerate(data["clip_ids"])
        ]
        requirements: list[BriefRequirement] = []
        seconds = None
        for turn in data["turns"]:
            for update in turn.get("brief_updates") or []:
                requirements.append(
                    BriefRequirement(
                        id=f"r{len(requirements) + 1}",
                        kind=update["kind"],
                        scope=update.get("scope") or "global",
                        literal=update.get("literal"),
                        description=update.get("description"),
                        facts=update.get("facts") or {},
                    )
                )
                if update["kind"] == "timing":
                    seconds = (update.get("facts") or {}).get("duration_s")
        strategy = {"edit_format": "montage"}
        if seconds:
            strategy |= {"target_duration_s": seconds, "target_duration_requested": True}
        brief = CreativeBrief(version=1, requirements=requirements)
        assert collect_conflicts(strategy, brief, {"clip_assignments": rows}) == [], path.name
        seen += 1
    assert seen >= 1


def test_every_format_matrix_row_asks_zero_questions() -> None:
    """The 240 utterance rows carry no media: nothing to conflict, whatever the format."""
    rows = [
        json.loads(line)
        for line in (FIXTURES / "kria_format_matrix.jsonl").read_text("utf-8").splitlines()
        if line.strip()
    ]
    assert len(rows) == 240
    for row in rows:
        format_token = str(row["format_token"])
        strategy = {"edit_format": format_token if format_token != "day_vlog" else "montage"}
        assert collect_conflicts(strategy, None, {"clip_assignments": []}) == [], row["case_id"]


@pytest.mark.asyncio
async def test_control_the_same_derivation_does_ask_when_the_footage_cannot_satisfy_the_ask(
    monkeypatch,
) -> None:
    """Without this the zero-question runs could pass because the gate never fires."""
    fixture = next(load_fixture(p) for p in discover_fixture_paths() if p.stem == "east_run")
    footage = load_footage(fixture.footage)
    assert all(clip.fact("capture_time") is None for clip in footage.clips)
    brief = CreativeBrief(
        version=1,
        requirements=[
            BriefRequirement(
                id="r1",
                kind="order",
                scope="global",
                description="in the order I filmed them",
                facts={"key": "capture_time"},
            )
        ],
    )
    result = await _gate(monkeypatch, _planned(), rows=_rows(footage), brief=brief)
    assert result.plan.turn_value == "question"
    assert result.plan.choice_question["kind"] == "order_basis"
