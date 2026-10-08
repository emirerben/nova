"""Portable captured model responses through the real planner/compiler/save path.

Database/thread history and object existence are synthetic. Model responses are
captured, not live on replay. Replan captures only prove routing: the capture's
MainCreator was a stub, so its answer is never treated as a successful outcome.
"""

from __future__ import annotations

import copy
import json
from pathlib import Path
from types import SimpleNamespace

import pytest
from fastapi import HTTPException

from app.agents._runtime import ModelClient
from app.agents._schemas.creator_agent import AskUser
from app.agents.brief_extractor import BriefExtractorAgent
from app.agents.edit_copilot import EditCopilotAgent
from app.kria import planner
from app.routes import generative_jobs as gj
from app.services.kria_editor_ops import compile_editor_ops
from tests._prod_profile import apply_prod_profile
from tests.evals.runners.snapshot_variant import build_synthetic_job
from tests.kria.test_creative_brief import _ASK, _wire_planner

ROWS = json.loads(
    (Path(__file__).resolve().parents[2] / "fixtures/prompt_coverage/system_chain.json").read_text()
)["cases"]


async def replay_chain(row, monkeypatch):
    apply_prod_profile(monkeypatch)
    variant = copy.deepcopy(row["variant"])
    job = build_synthetic_job(variant)
    for i, slot in enumerate(variant["ai_timeline"]["slots"]):
        slot["source_gcs_path"] = f"generative-jobs/{job.id}/sources/{i}.mp4"
    job.all_candidates["clip_paths"] = [
        s["source_gcs_path"] for s in variant["ai_timeline"]["slots"]
    ]
    actual_editor = planner._plan_editor_revision
    db, item, creator_id, _, replan_runs = _wire_planner(
        monkeypatch,
        output=SimpleNamespace(action=AskUser(**_ASK), brief_updates=[]),
        editor_plan=None,
        snapshot=row["snapshot"],
    )
    monkeypatch.setattr(planner, "_plan_editor_revision", actual_editor)
    calls = iter(row["calls"])
    consumed = []

    async def extract(inputs, *, creator_request, prior_brief=None, **_kwargs):
        call = next(calls)
        assert call["stage"] == "extract"
        consumed.append("extract")
        agent = BriefExtractorAgent(ModelClient())
        return agent.parse(
            call["raw_texts"][-1],
            agent.Input(
                creator_request=creator_request,
                user_message=inputs.agent_input.user_message,
                conversation=inputs.agent_input.conversation,
                current_brief=prior_brief,
            ),
        )

    async def editor(body, **_kwargs):
        call = next(calls)
        assert call["stage"] == "editor"
        consumed.append("editor")
        assert body.original_request and row["prompt"] in body.original_request
        agent = EditCopilotAgent(ModelClient())
        return agent.parse(
            call["raw_texts"][-1],
            agent.Input(
                utterance=body.message,
                variant_snapshot=body.snapshot,
                prior_turns=body.turns,
                original_request=body.original_request,
            ),
        )

    monkeypatch.setattr(planner, "_call_brief_extractor", extract)
    monkeypatch.setattr(planner, "run_copilot_turn", editor)
    monkeypatch.setattr(gj.storage, "object_exists", lambda *_a, **_kw: True)
    result = await planner.plan_live_turn(
        db, thread_id=item.id, item_id=item.id, creator_id=creator_id, user_message=row["prompt"]
    )
    assert next(calls, None) is None
    for intent in result.plan.intents:
        assert intent.tool_name == "draft.apply_editor_ops"
        compiled = compile_editor_ops(job, variant, intent.arguments["operations"])
        gj.prepare_editor_commit(job, variant["variant_id"], compiled.payload)
    return result, job.assembly_plan["variants"][0], consumed, replan_runs


def assert_outcome(case, before, saved):
    titles = {b["id"]: b for b in saved["text_elements"] if b["id"] in {"title-main", "title-sub"}}
    if case in {"word_sequence", "chunk_sequence"}:
        children = [
            b
            for b in saved["text_elements"]
            if (b.get("source_params") or {}).get("sequence_source_id") == "title-main"
        ]
        expected = (
            ["An", "afternoon", "in", "Kadıköy"]
            if case == "word_sequence"
            else ["An afternoon", "in", "Kadıköy"]
        )
        assert [b["text"] for b in children] == expected
        assert children[0]["start_s"] == 0 and children[-1]["end_s"] == 2
        assert all(left["end_s"] <= right["start_s"] for left, right in zip(children, children[1:]))
        assert all(
            b["animation_phases"]["entrance"] == "fade" and b["animation_phases"]["exit"] == "fade"
            for b in children
        )
        for child in children:
            for key in (
                "position",
                "alignment",
                "x_frac",
                "y_frac",
                "font_family",
                "size_px",
                "color",
            ):
                assert child[key] == before["text_elements"][0][key]
        for prior in before["text_elements"][1:]:
            current = next(b for b in saved["text_elements"] if b["id"] == prior["id"])
            assert {k: current[k] for k in prior} == prior
    else:
        assert len(titles) == 2
    before_titles = {b["id"]: b for b in before["text_elements"] if b["id"] in titles}
    slots = (saved.get("user_timeline") or saved["ai_timeline"])["slots"]
    before_slots = before["ai_timeline"]["slots"]
    # Every tested ask preserves copy, the unrelated location label, and audio
    # unless the requested audio change says otherwise.
    assert {k: v["text"] for k, v in titles.items()} == {
        k: v["text"] for k, v in before_titles.items()
    }
    location = next(b for b in saved["text_elements"] if b["id"] == "location")
    assert {key: location[key] for key in before["text_elements"][2]} == before["text_elements"][2]
    if case == "audio_title":
        assert titles["title-main"]["color"] == "#FFFF00"
        assert saved["mix"] == pytest.approx(0.2)
    else:
        assert saved["mix"] == before["mix"]
    if case == "order_style":
        assert [s["clip_index"] for s in slots] == [1, 0]
        assert all(b["size_px"] < before_titles[k]["size_px"] for k, b in titles.items())
    elif case == "duration_title":
        assert [s["duration_s"] for s in slots] == [4, 4]
        assert all(b["start_s"] == 0 and b["end_s"] == 8 for b in titles.values())
    elif case == "preserve_cut":
        assert all(
            b["alignment"] == "left" and b["x_frac"] < 0.2 and b["y_frac"] > 0.7
            for b in titles.values()
        )
        assert all(
            (b["start_s"], b["end_s"]) == (before_titles[k]["start_s"], before_titles[k]["end_s"])
            for k, b in titles.items()
        )
    elif case == "caption_edit":
        assert [c["text"] for c in saved["caption_cues"]] == ["Meet me by the sea", "Stay a while"]
        assert saved["caption_margin_v"] > 1920 * 0.6
    elif case == "source_restore":
        assert [s["in_s"] for s in slots] == [0, 0]
        assert [s["duration_s"] for s in slots] == [20, 15]
        # Named yellow permits hue variation; it must still be recognizably yellow.
        assert all(
            int(b["color"][1:3], 16) >= 220
            and int(b["color"][3:5], 16) >= 180
            and int(b["color"][5:7], 16) <= 80
            for b in titles.values()
        )
    if case not in {"duration_title", "source_restore"}:
        assert sorted((s["clip_index"], s["in_s"], s["duration_s"]) for s in slots) == sorted(
            (s["clip_index"], s["in_s"], s["duration_s"]) for s in before_slots
        )
    if case != "order_style":
        assert [s["clip_index"] for s in slots] == [0, 1]


@pytest.mark.asyncio
@pytest.mark.parametrize("row", ROWS, ids=lambda r: r["run"] + "-" + r["case"])
async def test_live_capture_replays_through_planner_and_save(row, monkeypatch):
    if row.get("fixture_limitation"):
        with pytest.raises(HTTPException) as exc:
            await replay_chain(row, monkeypatch)
        assert exc.value.status_code == 422
        return
    result, saved, consumed, replan_runs = await replay_chain(row, monkeypatch)
    if row["run"] == "c" and row["case"] in {"caption_audio", "unsupported_partial"}:
        assert result.brief_route == "replan" and consumed == ["extract"] and replan_runs
        assert not result.plan.intents  # Stubbed answer is not live success evidence.
    elif row["case"] == "already_satisfied":
        assert not result.plan.intents and "already" in result.plan.response.lower()
        assert_outcome(row["case"], row["variant"], saved)
    elif row["case"] == "speed_limit":
        assert result.brief_route == "editor_ops" and consumed == ["extract", "editor"]
        assert not result.plan.intents and "not supported" in result.plan.response.lower()
    else:
        assert result.brief_route == "editor_ops" and consumed == ["extract", "editor"]
        assert result.plan.intents and not replan_runs
        assert_outcome(row["case"], row["variant"], saved)

        if row["case"] == "unsupported_partial":
            assert all(b["color"] == "#FFFF00" for b in saved["text_elements"][:2])
            summary = result.plan.intents[0].arguments["summary"].lower()
            assert "not supported" in summary and "voice" in summary


def test_incomplete_title_selection_fails_independent_outcome():
    row = next(r for r in ROWS if r["case"] == "order_style")
    changed = copy.deepcopy(row["variant"])
    changed["ai_timeline"]["slots"].reverse()
    changed["text_elements"][0]["size_px"] *= 0.8
    with pytest.raises(AssertionError):
        assert_outcome("order_style", row["variant"], changed)
