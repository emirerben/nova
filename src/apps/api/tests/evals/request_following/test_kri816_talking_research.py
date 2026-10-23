"""KRI-816 T05: research on a ready phone Talking (subtitled, device) edit.

Journey (server chain, model calls replaced by recorded outputs, no network):
ready phone Talking variant + plan-item clip analysis -> the planner's clip
context -> ``build_editor_snapshot`` -> ``run_copilot_turn`` (research route)
-> ``compile_editor_ops`` -> phone editor Save -> pinned device recipe.

Failure modes written before the fix:
1. The Talking variant projects ``slots=[]`` -> "There is no footage to explain".
2. Slots exist but phone sources carry no seen/visible text -> "The pictured
   subject could not be confirmed" dead end.
3. One clip spans the whole video, so the "opening" rule rejects every window.
4. The explanation lands over spoken words / captions (cues are not text bars).
5. Research changes speech, captions, duration, framing, mix or timeline, on
   success or on failure (research-refusal-preservation fields).
6. The creator's saved caption correction ("Wow!") is dropped.
7. The projected slots leak into the applier and the Save writes a timeline.
8. No readable pause gives a generic refusal instead of a precise limit.
9. A stale source generation is trusted as "what is shown".
10. Narrated/voiceover/smart-caption montages (cues present, not Talking) lose their
    footage windows to the pause-only rule, or get the "spoken words" refusal.
11. A creator's own text bar inside the chosen pause is removed by the researched add.

Artifact: ``.dev/kri-816/talking-report.json`` (repo root) with the request,
projected slots, research windows, the staged operation and the recipe text.
"""

from __future__ import annotations

import asyncio
import copy
import json
import uuid
from pathlib import Path
from types import SimpleNamespace

import pytest

from app.agents.edit_copilot import EditCopilotAgent
from app.kria.device_render import make_device_request
from app.pipeline.phone_subtitled_plan import compile_phone_subtitled_plan
from app.routes import generative_jobs as gj
from app.routes._copilot import CopilotTurnBody, run_copilot_turn
from app.services.device_render import device_status, pin_device_request
from app.services.kria_editor_ops import (
    build_editor_snapshot,
    clip_seen_by_media_id,
    clip_visible_text_by_media_id,
    compile_editor_ops,
)
from app.services.phone_sources import PHONE_SOURCES_FIELD, PHONE_VISUALS_FIELD
from app.services.researched_text import (
    GroundedResearchAgent,
    ResearchCopyAgent,
    ResearchReviewAgent,
    ResearchReviewOutput,
    evidence_from_response,
)
from tests.pipeline.test_phone_subtitled_plan import _binding
from tests.routes.test_phone_subtitled_editor_commit import _enable_subtitled_editor

REQUEST = (
    "Research Troy and add one short, sourced explanation in the pause after the first "
    "spoken line. Keep the original speech, captions, duration and framing unchanged."
)
# The T05 job's cues, with the creator's saved "Wow." -> "Wow!" correction.
CUES = [
    {
        "text": "There's so many coming in and out.",
        "start_s": 0.82,
        "end_s": 3.28,
        "words": [
            {"text": "There's", "start_s": 0.82, "end_s": 1.54},
            {"text": "so", "start_s": 1.54, "end_s": 1.84},
            {"text": "many", "start_s": 1.84, "end_s": 2.16},
            {"text": "coming", "start_s": 2.16, "end_s": 2.46},
            {"text": "in", "start_s": 2.46, "end_s": 2.64},
            {"text": "and", "start_s": 2.64, "end_s": 2.88},
            {"text": "out.", "start_s": 2.88, "end_s": 3.28},
        ],
    },
    {
        "text": "Wow!",
        "start_s": 6.82,
        "end_s": 7.18,
        "words": [{"text": "Wow!", "start_s": 6.82, "end_s": 7.18}],
    },
]
SEEN = "Visitors walking in and out past the excavated stone walls of Troy"
FACT = "Troy is an archaeological site in northwest Turkey with nine main settlement layers."
PRESERVED = (
    "caption_cues",
    "user_timeline",
    "original_mix",
    "phone_overlay_receipt",
    "phone_lane_receipt",
    "duration_s",
)
REPORT = Path(__file__).resolve().parents[6] / ".dev" / "kri-816" / "talking-report.json"


def _grounding():
    return SimpleNamespace(
        text=FACT,
        candidates=[
            SimpleNamespace(
                grounding_metadata=SimpleNamespace(
                    web_search_queries=["Troy archaeological site history"],
                    grounding_chunks=[
                        SimpleNamespace(
                            web=SimpleNamespace(
                                uri="https://whc.unesco.org/en/list/849", title="UNESCO"
                            )
                        )
                    ],
                    grounding_supports=[
                        SimpleNamespace(
                            segment=SimpleNamespace(text=FACT), grounding_chunk_indices=[0]
                        )
                    ],
                    search_entry_point=SimpleNamespace(rendered_content="<div>Search</div>"),
                )
            )
        ],
    )


def talking_job(monkeypatch, *, cues=None):
    """The T05 shape: one 10.08 s speaker clip, device `subtitled`, no timeline."""
    _enable_subtitled_editor(monkeypatch)
    monkeypatch.setattr(gj.settings, "phone_sfx_speech_duck_enabled", False)
    monkeypatch.setattr(gj.settings, "phone_subtitled_title_enabled", True)
    binding = _binding("analysis-proxy-30FC", duration_s=10.08)
    cues = copy.deepcopy(CUES if cues is None else cues)
    recipe = compile_phone_subtitled_plan((binding,), caption_cues=cues)
    variant = {
        "variant_id": "subtitled",
        "resolved_archetype": "subtitled",
        "render_destination": "device",
        "render_status": "ready",
        "render_generation_id": "first",
        "duration_s": recipe.duration,
        "caption_cues": cues,
        "voiceover_caption_style": "sentence",
        "phone_overlay_receipt": {"placed": [], "matcher": "none", "version": 1},
        "phone_lane_receipt": {"applied": [], "dropped": []},
    }
    job = SimpleNamespace(
        id=uuid.uuid4(),
        user_id=uuid.uuid4(),
        status="variants_ready",
        current_phase=None,
        all_candidates={"clip_paths": [binding.proxy_path]},
        assembly_plan={
            PHONE_SOURCES_FIELD: [binding.model_dump(mode="json")],
            PHONE_VISUALS_FIELD: [],
            "variants": [variant],
        },
    )
    pin_device_request(
        job,
        make_device_request(job_id=job.id, variant_id="subtitled", revision=1, recipe=recipe),
        base_generation="first",
    )
    assignments = [
        {
            "media_id": binding.media_id,
            "gcs_path": binding.proxy_path,
            "storage_generation": binding.generation,
            "kind": "video",
            "analysis": {"understanding": {"summary": SEEN, "subject": "Troy ruins"}},
        }
    ]
    return job, assignments


def _variant(job) -> dict:
    return job.assembly_plan["variants"][0]


def _snapshot(job, assignments) -> dict:
    variant = _variant(job)
    # The planner's `_copilot_clip_context` joins exactly these two readers.
    context = {
        "seen": clip_seen_by_media_id(job, variant, assignments),
        "visible_text": clip_visible_text_by_media_id(job, variant, assignments),
    }
    return build_editor_snapshot(job, variant, clip_context=context)


def _stub_models(monkeypatch, *, choose="after_first_line", calls=None):
    calls = calls if calls is not None else []

    def route(self, input, *, ctx=None):
        return self.parse(
            json.dumps(
                {
                    "research_request": {
                        "topic": "Troy archaeological site history",
                        "request_quote": "Research Troy",
                    }
                }
            ),
            input,
        )

    def search(self, input, *, ctx=None):
        calls.append(("search", input.topic))
        return evidence_from_response(_grounding())

    def write(self, input, *, ctx=None):
        windows = input.snapshot["research_windows"]
        calls.append(("copy", windows))
        if choose == "after_first_line":
            window = next(row for row in windows if row.get("after_spoken_line") == 1)
        else:
            window = choose(windows)
        return self.parse(
            json.dumps(
                {
                    "captions": [
                        {
                            "window_id": window["id"],
                            "text": "Nine settlement layers",
                            "context_label": "Troy",
                            "media_ids": [],
                            "evidence_ids": ["fact-0"],
                            "visual_quote": "excavated stone walls of Troy",
                            "scope": "context",
                            "subject_name": "",
                            "identity_quote": "",
                        }
                    ],
                    "limitations": "",
                    "unmet_requests": [],
                }
            ),
            input,
        )

    monkeypatch.setattr(EditCopilotAgent, "run", route)
    monkeypatch.setattr(GroundedResearchAgent, "run", search)
    monkeypatch.setattr(ResearchCopyAgent, "run", write)
    monkeypatch.setattr(
        ResearchReviewAgent,
        "run",
        lambda *a, **k: ResearchReviewOutput(approved=True, reason="Supported by UNESCO"),
    )
    return calls


def _turn(snapshot):
    return asyncio.run(
        run_copilot_turn(
            CopilotTurnBody(message=REQUEST, snapshot=snapshot, client_contract_version=2),
            job_id=uuid.uuid4(),
        )
    )


def _preserved(job) -> str:
    variant = _variant(job)
    recipe = device_status(job, "subtitled").request.recipe
    return json.dumps(
        {
            "fields": {key: variant.get(key) for key in PRESERVED},
            "tracks": [track.model_dump(mode="json") for track in recipe.tracks],
            "captions": [
                layer.model_dump(mode="json")
                for layer in recipe.text_layers
                if layer.id.startswith("caption-")
            ],
            "duration": recipe.duration,
        },
        sort_keys=True,
    )


def test_t05_researched_explanation_lands_in_the_pause_and_keeps_everything_else(monkeypatch):
    job, assignments = talking_job(monkeypatch)
    calls = _stub_models(monkeypatch)
    before = _preserved(job)
    snapshot = _snapshot(job, assignments)

    # 1/2: the Talking clip is projected with what the analyzer saw.
    assert [slot["media_id"] for slot in snapshot["slots"]] == ["analysis-proxy-30FC"]
    assert snapshot["slots"][0]["seen"]["text"] == SEEN
    assert snapshot["slots"][0]["read_only"] is True

    result = _turn(snapshot)

    assert result.outcome == "proposed", result.reply
    [op] = result.ops
    assert op["op"] == "add_text"
    # 3/4: inside the 3.28 -> 6.82 s pause, padded, never over a spoken word.
    assert 3.28 < op["start_s"] and op["end_s"] < 6.82
    assert op["end_s"] - op["start_s"] >= 2
    assert op["text"] == "Troy: Nine settlement layers"
    research = op["research"]
    assert [source["url"] for source in research["sources"]] == [
        "https://whc.unesco.org/en/list/849"
    ]
    assert research["speech_binding"][0]["text"] == CUES[0]["text"]
    assert [kind for kind, _ in calls] == ["search", "copy"]

    # 7: the staged draft is text only: no timeline, no caption rewrite.
    compiled = compile_editor_ops(job, _variant(job), result.ops)
    assert compiled.payload.timeline_slots is None
    assert compiled.payload.caption_cues is None
    gj.prepare_editor_commit(
        job,
        "subtitled",
        gj.EditorCommitRequest(
            base_generation=gj.variant_render_baseline(_variant(job)),
            text_elements=compiled.payload.text_elements,
        ),
        user_id="owner",
        plan_item_id="item",
    )

    recipe = device_status(job, "subtitled").request.recipe
    [layer] = [layer for layer in recipe.text_layers if layer.id.startswith("text-")]
    assert " ".join(run.text for run in layer.runs) == "Troy: Nine settlement layers"
    assert (layer.start, layer.end) == (op["start_s"], op["end_s"])
    # 5/6: speech, captions (with the "Wow!" fix), duration, framing, mix intact.
    assert _preserved(job) == before
    assert _variant(job)["caption_cues"][1]["text"] == "Wow!"
    saved = next(
        row
        for row in _variant(job)["text_elements"]
        if (row.get("source_params") or {}).get("research")
    )
    assert saved["source_params"]["research"]["sources"] == research["sources"]

    REPORT.parent.mkdir(parents=True, exist_ok=True)
    REPORT.write_text(
        json.dumps(
            {
                "request": REQUEST,
                "slots": snapshot["slots"],
                "research_windows": calls[1][1],
                "staged_op": op,
                "recipe_text_layer": {
                    "text": " ".join(run.text for run in layer.runs),
                    "start": layer.start,
                    "end": layer.end,
                },
                "preserved_fields_identical": True,
            },
            indent=2,
            default=str,
        )
    )


def test_t05_explanation_over_spoken_words_is_refused_and_the_edit_is_unchanged(monkeypatch):
    job, assignments = talking_job(monkeypatch)
    snapshot = _snapshot(job, assignments)
    before = (_preserved(job), json.dumps(snapshot, sort_keys=True))

    from app.services.researched_text import research_windows

    assert all(row["end_s"] <= 0.82 or row["start_s"] >= 3.28 for row in research_windows(snapshot))
    caption = {
        "text": "Troy: Nine settlement layers",
        "context_label": "Troy",
        "media_ids": ["analysis-proxy-30FC"],
        "evidence_ids": ["fact-0"],
        "visual_quote": "excavated stone walls of Troy",
        "scope": "context",
    }
    from app.services.researched_text import ResearchUnavailable, research_operations

    with pytest.raises(ResearchUnavailable, match="spoken words"):
        research_operations([caption], evidence_from_response(_grounding()), snapshot, REQUEST)
    assert (_preserved(job), json.dumps(snapshot, sort_keys=True)) == before


def test_t05_no_readable_pause_gives_a_precise_limit_and_keeps_the_edit(monkeypatch):
    dense = [
        {"text": "First line", "start_s": 0.5, "end_s": 4.0},
        {"text": "Second line", "start_s": 4.6, "end_s": 8.4},
        {"text": "Third line", "start_s": 8.9, "end_s": 10.0},
    ]
    job, assignments = talking_job(monkeypatch, cues=dense)
    _stub_models(monkeypatch)
    before = _preserved(job)

    result = _turn(_snapshot(job, assignments))

    assert result.ops == []
    assert "no pause of at least 2 seconds between spoken lines" in result.reply
    assert _preserved(job) == before


def test_t05_stale_source_generation_is_not_trusted_as_what_is_shown(monkeypatch):
    job, assignments = talking_job(monkeypatch)
    assignments[0]["storage_generation"] = "2"
    _stub_models(monkeypatch)

    snapshot = _snapshot(job, assignments)

    assert "seen" not in snapshot["slots"][0]
    result = _turn(snapshot)
    assert result.ops == []
    assert "pictured subject could not be confirmed" in result.reply


def test_cues_on_a_non_talking_edit_keep_footage_windows_and_allow_overlap():
    from app.services.researched_text import research_windows

    snapshot = {
        "slots": [
            {"media_id": "m0", "output_start_s": 0.0, "output_end_s": 4.0, "seen": {"text": "a"}},
            {"media_id": "m1", "output_start_s": 4.0, "output_end_s": 9.0, "seen": {"text": "b"}},
        ],
        "text_bars": [],
        "captions": {
            "cues": [
                {"text": "Narration one", "start_s": 0.2, "end_s": 4.5},
                {"text": "Narration two", "start_s": 4.7, "end_s": 8.9},
            ]
        },
        "total_duration_s": 9.0,
    }

    windows = research_windows(snapshot)

    assert [(row["start_s"], row["end_s"]) for row in windows] == [(0.0, 4.0), (4.0, 9.0)]
    assert all("after_spoken_line" not in row for row in windows)


def test_t05_creator_text_inside_the_pause_is_protected_not_replaced(monkeypatch):
    job, assignments = talking_job(monkeypatch)
    snapshot = _snapshot(job, assignments)
    from app.services.researched_text import (
        ResearchUnavailable,
        research_operations,
        research_windows,
    )

    window = next(row for row in research_windows(snapshot) if row["after_spoken_line"] == 1)
    mid = (window["start_s"] + window["end_s"]) / 2
    snapshot["text_bars"] = [
        *snapshot.get("text_bars", []),
        {"id": "creator-day-1", "text": "Day 1", "start_s": mid - 0.2, "end_s": mid + 0.2},
    ]
    caption = {
        "text": "Troy: Nine settlement layers",
        "context_label": "Troy",
        "media_ids": window["media_ids"],
        "evidence_ids": ["fact-0"],
        "visual_quote": "excavated stone walls of Troy",
        "scope": "context",
        "window_id": window["id"],
    }

    with pytest.raises(ResearchUnavailable, match="protected text"):
        research_operations([caption], evidence_from_response(_grounding()), snapshot, REQUEST)
