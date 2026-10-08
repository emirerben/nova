"""KRI-533: the phone Voiceover worker records what it pinned and judges the brief from it.

Drives `_run_phone_narrated_job` through `_setup_narrated` with a mocked alignment agent and a
Cappadocia-shaped bound brief: a last-clip anchor, a clip-group timing ask and an English
caption ask. The render-ready review then reads the same record.
"""

from __future__ import annotations

import uuid
from types import SimpleNamespace

import pytest

import app.tasks.generative_build as gb
from app.kria.brief import BriefRequirement, CreativeBrief
from app.kria.brief_binding import BriefBinding
from app.pipeline.phone_guided_plan import UnsupportedPhonePlan
from app.tasks import kria_runtime
from tests.tasks.test_narrated_caption_language import (
    _HINTED_EN,
    _SPOKEN_TR,
    _whisper_by_hint,
)
from tests.tasks.test_phone_subtitled_narrated_dispatch import (
    _mock_alignment_agent,
    _setup_narrated,
)

BRIEF = CreativeBrief(
    version=4,
    requirements=[
        BriefRequirement(
            id="r1",
            kind="style",
            scope="global",
            description="Provide English subtitles translated from the Turkish voiceover",
        ),
        BriefRequirement(
            id="r2",
            kind="timing",
            scope="global",
            description="Show the balloons while talking about the balloons",
        ),
        BriefRequirement(
            id="r3",
            kind="order",
            scope="global",
            description="End on the sunset valley",
            facts={"last_clip": "the sunset valley"},
        ),
        BriefRequirement(id="r4", kind="select", scope="global", description="Skip the quad bike"),
    ],
)


def _intents():
    def intent(op, label, clips, **extra):
        return {
            "op": op,
            "status": "resolved",
            "attribute": label,
            "assignments": [{"media_id": f"analysis-proxy-{c}.mp4"} for c in clips],
            **extra,
        }

    return [
        intent("order", "the balloons", ["c0", "c1"]),
        intent("order", "the sunset valley", ["c2"], position="last"),
    ]


def _setup(monkeypatch, *, order, bound=True):
    job, snapshot, _session, _bindings = _setup_narrated(
        monkeypatch,
        edit_format="narrated_planned",
        alignment=True,
        extra_candidates={
            "caption_language_request": "en",
            "creator_strategy": {"resolved_clip_intents": _intents()},
        },
    )
    _whisper_by_hint(monkeypatch, {None: _SPOKEN_TR, "en": _HINTED_EN})
    # Word ids are positional: the three clips start at words 0, 2 and 4.
    _mock_alignment_agent(monkeypatch, order, [0, 2, 4])
    binding = BriefBinding.create(uuid.uuid4(), BRIEF)
    if bound:
        snapshot["creator_brief_binding"] = binding.model_dump(mode="json")
        job.assembly_plan["creator_brief_binding"] = snapshot["creator_brief_binding"]
    return job, snapshot, binding


def test_the_worker_records_the_steps_and_judges_the_brief(monkeypatch):
    job, snapshot, _binding = _setup(monkeypatch, order=["c0", "c1", "c2"])

    gb._run_phone_narrated_job(str(job.id), snapshot, job.all_candidates, ownership_epoch=3)

    assert job.status == "awaiting_device"
    record = job.assembly_plan["narrated_alignment"]
    assert record["generation_id"] == "generation"
    assert record["brief_version"] == 4
    assert record["ordering_basis"] == "spoken_word_alignment"
    assert record["caption_language"] == "en"
    assert record["spoken_language"] == "tr"
    assert record["caption_language_request"] == "en"
    assert [
        (s["media_id"], s["clip"], s["labels"], s["start_s"], s["end_s"], s["text"])
        for s in record["steps"]
    ] == [
        ("c0", "analysis-proxy-c0.mp4", ["the balloons"], 0.0, 4.0, "Sabah balonlar."),
        ("c1", "analysis-proxy-c1.mp4", ["the balloons"], 4.0, 8.0, "Sonra yürü."),
        ("c2", "analysis-proxy-c2.mp4", ["the sunset valley"], 8.0, 12.0, "Gün batımı."),
    ]
    assert record["steps"][2]["placed"] == [{"spot": "last", "name": "the sunset valley"}]
    receipts = {r["requirement_id"]: r for r in record["requirement_receipts"]}
    # Only what the record could judge is stored; the skipped-clip ask is not.
    assert set(receipts) == {"r1", "r2", "r3"}
    assert all(r["status"] == "met" and r["verification"] == "checked" for r in receipts.values())
    assert all(
        r["brief_version"] == 4 and r["generation_id"] == "generation" for r in receipts.values()
    )
    assert receipts["r2"]["reason"] == (
        'The balloons clips play from 0.0 s to 8.0 s, under: "Sabah balonlar. Sonra yürü."'
    )
    assert "request_recovery" not in job.assembly_plan


def test_the_render_ready_review_lists_the_record_receipts(monkeypatch):
    job, snapshot, binding = _setup(monkeypatch, order=["c0", "c1", "c2"])
    gb._run_phone_narrated_job(str(job.id), snapshot, job.all_candidates, ownership_epoch=3)
    [variant] = job.assembly_plan["variants"]
    thread = SimpleNamespace(id=uuid.UUID(binding.thread_id), creator_id=uuid.uuid4())
    execution = SimpleNamespace(result={})

    text, payload = kria_runtime._approved_generation_review(
        None, thread, job, variant, execution, "Ready."
    )

    assert "Done: End on the sunset valley" in text
    assert "Done: Show the balloons while talking about the balloons (The balloons clips" in text
    assert "Done: Provide English subtitles translated from the Turkish voiceover" in text
    assert "Couldn't verify: Skip the quad bike" in text
    assert text.count("Couldn't verify") == 1
    by_id = {row["requirement_id"]: row for row in payload}
    assert by_id["r3"]["verification"] == "checked" and by_id["r4"]["verification"] == "unchecked"


def test_a_last_clip_that_misses_asks_before_pinning_a_recipe(monkeypatch):
    # The agent seats the sunset clip first: "End on the sunset valley" cannot be met.
    job, snapshot, binding = _setup(monkeypatch, order=["c2", "c0", "c1"])

    with pytest.raises(UnsupportedPhonePlan, match="The sunset valley is clip 1 of 3"):
        gb._run_phone_narrated_job(str(job.id), snapshot, job.all_candidates, ownership_epoch=3)

    assert "_device_render_v1" not in job.assembly_plan
    assert "narrated_alignment" not in job.assembly_plan
    recovery = job.assembly_plan["request_recovery"]
    assert "Should I try again or simplify this request?" in recovery["message"]
    assert recovery["binding_digest"] == binding.digest
    [receipt] = [r for r in recovery["requirement_receipts"] if r["requirement_id"] == "r3"]
    assert receipt["status"] == "not_possible" and receipt["verification"] == "checked"


def test_an_unbound_job_records_the_steps_but_no_receipts_and_never_blocks(monkeypatch):
    job, snapshot, _binding = _setup(monkeypatch, order=["c2", "c0", "c1"], bound=False)

    gb._run_phone_narrated_job(str(job.id), snapshot, job.all_candidates, ownership_epoch=3)

    assert job.status == "awaiting_device"
    record = job.assembly_plan["narrated_alignment"]
    assert record["brief_version"] is None and "requirement_receipts" not in record
    assert [s["media_id"] for s in record["steps"]] == ["c2", "c0", "c1"]


def test_the_legacy_split_records_its_ordering_basis_and_reports_no_timing(monkeypatch):
    job, snapshot, _binding = _setup(monkeypatch, order=["c0", "c1", "c2"])
    monkeypatch.setattr(gb.settings, "narrated_clip_alignment_enabled", False)
    # Drop the bound brief: with it a multi-clip job refuses the bucket fallback (KRI-459).
    snapshot.pop("creator_brief_binding")
    job.assembly_plan.pop("creator_brief_binding")

    gb._run_phone_narrated_job(str(job.id), snapshot, job.all_candidates, ownership_epoch=3)

    assert job.assembly_plan["narrated_alignment"]["ordering_basis"] == "attachment"


def test_the_record_helper_matches_labels_by_basename_or_source_id():
    steps = [
        SimpleNamespace(step_id="s1", media_id="m-a", start_s=0.0, end_s=2.0),
        SimpleNamespace(step_id="s2", media_id="m-b", start_s=2.0, end_s=4.0),
    ]
    words = [
        SimpleNamespace(text="one", start_s=0.1, end_s=0.4),
        SimpleNamespace(text="two", start_s=2.1, end_s=2.4),
    ]
    strategy = {
        "resolved_clip_intents": [
            {
                "op": "order",
                "status": "resolved",
                "attribute": "  the  start ",
                "position": "first",
                "assignments": [{"media_id": "analysis-proxy-a.mp4"}],
            },
            {
                "op": "include",
                "status": "resolved",
                "attribute": "b",
                "assignments": [{"media_id": "m-b"}],
            },
            {"op": "order", "status": "needs_creator", "attribute": "skipped", "assignments": []},
        ]
    }
    record = gb._narrated_alignment_record(
        generation="g",
        brief_version=1,
        ordering_basis="attachment",
        steps=steps,
        proxy_path_by_media_id={"m-a": "u/analysis-proxy-a.mp4", "m-b": "u/analysis-proxy-b.mp4"},
        strategy=strategy,
        words=words,
        caption_language=None,
        spoken_language=None,
        caption_language_request=None,
    )
    assert [(s["labels"], s["placed"], s["text"]) for s in record["steps"]] == [
        (["the start"], [{"spot": "first", "name": "the start"}], "one"),
        (["b"], [], "two"),
    ]
