"""KRI-1001: chat "turn on captions" reaches the lyrics a lip-sync/song montage keeps hidden.

Failure modes written first:
  (a) the chat snapshot offers no caption family because the lyrics are `text_elements` rows,
      not `caption_cues`, so the model never sees a caption control;
  (b) the snapshot reads `enabled` from the variant (absent => true) instead of the guided
      revision (`false`), so the model thinks the lyrics are already on and does nothing;
  (c) the chat patch lands somewhere the editor's Show captions toggle does not read, so the
      toggle and the chat disagree;
  (d) the chat payload carries only `enabled`, and Save/the phone replace caption_meta wholesale,
      so the planned lyric style and spot are lost; or Undo restores "on" for hidden lyrics;
  (e) a video with no lyric rows starts offering captions it cannot show.
"""

# ruff: noqa: F811
from __future__ import annotations

import copy
import json
import re

import pytest

from app.tasks import generative_build as gb
from tests.tasks.test_user_song_montage_job import (  # noqa: F401
    _drawn_lyrics,
    _editor_read,
    _lyrics_analysis,
    composer,
    confident,
    harness,
)

_UNASKED = "Full creator request (chronological):\nMake a fast montage"


def _chat(job, variant, utterance: str, ops: list[dict]):
    from app.agents._runtime import ModelClient
    from app.agents.edit_copilot import EditCopilotAgent, EditCopilotInput
    from app.services.kria_editor_ops import build_editor_snapshot

    snapshot = build_editor_snapshot(job, variant)
    raw = json.dumps(
        {
            "intent": "edit",
            "ops": ops,
            "confidence": 0.9,
            "reply": "Done.",
            "suggestions": [],
            "needs_clarification": False,
        }
    )
    return snapshot, EditCopilotAgent(ModelClient()).parse(
        raw, EditCopilotInput(utterance=utterance, prior_turns=[], variant_snapshot=snapshot)
    )


@pytest.mark.parametrize("sync", ["lipsync", "background"])
def test_chat_turn_on_captions_reveals_hidden_song_lyrics_and_the_editor_toggle_agrees(
    harness,
    composer,
    monkeypatch,
    sync,
):
    from app.routes import generative_jobs as gj
    from app.services.kria_editor_ops import compile_editor_ops

    monkeypatch.setattr(gj.settings, "guided_story_editor_v2_enabled", True)
    monkeypatch.setattr(gj, "signed_get_url", lambda path, ttl: f"https://signed.test/{path}")
    job, _snapshot, _bindings = harness(
        sync=sync,
        rows=[confident("clip-a", 10), confident("clip-b", 28), confident("clip-c", 46)],
        song_analysis=_lyrics_analysis(),
        creator_request=_UNASKED,
    )
    gb._run_generative_job(str(job.id))
    assert _drawn_lyrics(job) == []
    variant = job.assembly_plan["variants"][0]  # what the chat planner reads
    assert not variant.get("caption_cues")

    snapshot, output = _chat(
        job,
        variant,
        "turn on captions",
        [{"op": "set_caption_meta", "patch": {"enabled": True}}],
    )
    assert "caption" in snapshot["allowed_op_families"]
    assert snapshot["captions"]["meta"]["enabled"] is False
    assert output.outcome == "proposed", output
    assert [op["op"] for op in output.ops] == ["set_caption_meta"]

    compiled = compile_editor_ops(job, variant, output.ops)
    assert compiled.payload.caption_meta.enabled is True
    # A Save replaces caption_meta wholesale: the planned lyric look must ride along ...
    assert compiled.payload.caption_meta.style == "sentence"
    assert compiled.payload.caption_meta.y_frac == 0.7
    # ... without the receipt claiming the creator asked for it.
    assert len(compiled.changes) == 1
    # Undo restores hidden AND the look Save would otherwise replace.
    assert compiled.before["caption_meta"] == {"enabled": False, "style": "sentence", "y_frac": 0.7}
    assert [(e.lane, e.field) for e in compiled.diff.entries] == [("caption_meta", "enabled")]
    # The phone applies the draft; the cloud Save path takes the same payload.
    gj.require_guided_story_editor_commit(job, "guided_story", compiled.payload)
    gj.prepare_editor_commit(job, "guided_story", compiled.payload)

    shown, revision, _timeline = _editor_read(job)
    assert shown["caption_meta"]["enabled"] is True  # the editor's Show captions toggle
    assert revision["caption_meta"]["enabled"] is True
    assert revision["caption_meta"]["style"] == "sentence"
    assert _drawn_lyrics(job)
    assert all(re.search(r"\bla\d+\b", text) for text in _drawn_lyrics(job))


def test_a_staged_chat_toggle_shows_in_the_next_turns_snapshot(harness, composer, monkeypatch):
    from app.routes import generative_jobs as gj
    from app.services.kria_editor_ops import (
        build_editor_snapshot,
        compile_editor_ops,
        editor_payload_dump,
        project_editor_draft,
    )

    monkeypatch.setattr(gj.settings, "guided_story_editor_v2_enabled", True)
    job, _snapshot, _bindings = harness(
        sync="lipsync", song_analysis=_lyrics_analysis(), creator_request=_UNASKED
    )
    gb._run_generative_job(str(job.id))
    variant = job.assembly_plan["variants"][0]
    compiled = compile_editor_ops(
        job, variant, [{"op": "set_caption_meta", "patch": {"enabled": True}}]
    )
    staged = project_editor_draft(variant, editor_payload_dump(compiled.payload), job)
    assert build_editor_snapshot(job, staged)["captions"]["meta"]["enabled"] is True


def test_a_video_without_lyric_rows_is_not_offered_captions(harness, composer, monkeypatch):
    from app.routes import generative_jobs as gj
    from app.services.kria_editor_ops import build_editor_snapshot

    monkeypatch.setattr(gj.settings, "guided_story_editor_v2_enabled", True)
    job, _snapshot, _bindings = harness(
        sync="lipsync", song_analysis=_lyrics_analysis(), creator_request=_UNASKED
    )
    gb._run_generative_job(str(job.id))
    variant = copy.deepcopy(job.assembly_plan["variants"][0])
    variant["text_elements"] = [
        row for row in variant["text_elements"] if not row["id"].startswith("song-caption-")
    ]
    snapshot = build_editor_snapshot(job, variant)
    assert "caption" not in snapshot["allowed_op_families"]
    assert "captions" not in snapshot


def _hidden_lyrics_item(harness, monkeypatch, sync="lipsync"):
    from app.routes import generative_jobs as gj

    monkeypatch.setattr(gj.settings, "guided_story_editor_v2_enabled", True)
    job, _snapshot, _bindings = harness(
        sync=sync, song_analysis=_lyrics_analysis(), creator_request=_UNASKED
    )
    gb._run_generative_job(str(job.id))
    return job, job.assembly_plan["variants"][0]


def test_a_captions_scoped_turn_keeps_the_planned_lyric_look(harness, composer, monkeypatch):
    """The Kria runtime strips every commit lane outside a flagged scope (KRI-441 layer 3);
    the lyric look riding along with the toggle is not a change and must not be stripped."""
    from app.kria import plan_review
    from app.services.kria_editor_ops import compile_editor_ops

    job, variant = _hidden_lyrics_item(harness, monkeypatch)
    compiled = compile_editor_ops(
        job, variant, [{"op": "set_caption_meta", "patch": {"enabled": True}}]
    )
    repaired, report = plan_review.repair_compiled(compiled, ["captions"])
    meta = repaired.payload.caption_meta
    assert (meta.enabled, meta.style, meta.y_frac) == (True, "sentence", 0.7)
    assert not report
    assert plan_review.repair_note(report, []) == ""
    data = repaired.payload.model_dump(mode="json", exclude_none=True)
    assert plan_review.changed_sections(data, repaired.before, ["captions"]) == ["captions"]


def test_a_look_only_scope_does_not_turn_the_lyrics_on(harness, composer, monkeypatch):
    from app.kria import plan_review
    from app.services.kria_editor_ops import compile_editor_ops

    job, variant = _hidden_lyrics_item(harness, monkeypatch)
    compiled = compile_editor_ops(
        job, variant, [{"op": "set_caption_meta", "patch": {"enabled": True}}]
    )
    repaired, report = plan_review.repair_compiled(compiled, ["look"])
    assert report.reverted_sections == ["captions"]
    assert repaired.payload.caption_meta is None


def test_undoing_the_toggle_restores_hidden_with_the_planned_lyric_look(
    harness, composer, monkeypatch
):
    """Undo commits `caption_meta` wholesale too, so it must put the look back with `enabled`."""
    from app.kria import plan_review
    from app.routes import generative_jobs as gj
    from app.routes.generative_jobs import EditorCommitCaptionMeta, variant_render_baseline
    from app.services.kria_editor_ops import compile_editor_ops, editor_payload_dump

    job, variant = _hidden_lyrics_item(harness, monkeypatch)
    compiled = compile_editor_ops(
        job, variant, [{"op": "set_caption_meta", "patch": {"enabled": True}}]
    )
    compiled, _report = plan_review.repair_compiled(compiled, ["captions"])
    data = editor_payload_dump(compiled.payload)
    record = plan_review.build_restore_record(
        data, compiled.before, ["captions"], turn_id="t1", base_job_id=str(job.id)
    )
    lanes, _toggle = plan_review.restore_section(record, "captions", data)
    assert lanes["caption_meta"] == {"enabled": False, "style": "sentence", "y_frac": 0.7}

    gj.prepare_editor_commit(job, "guided_story", compiled.payload)
    assert _drawn_lyrics(job)
    undo = compiled.payload.model_copy(
        update={
            "caption_meta": EditorCommitCaptionMeta(**lanes["caption_meta"]),
            "guided_revision_number": compiled.payload.guided_revision_number + 1,
            "base_generation": variant_render_baseline(job.assembly_plan["variants"][0]),
        }
    )
    gj.prepare_editor_commit(job, "guided_story", undo)
    _shown, revision, _timeline = _editor_read(job)
    assert revision["caption_meta"]["enabled"] is False
    assert (revision["caption_meta"]["style"], revision["caption_meta"]["y_frac"]) == (
        "sentence",
        0.7,
    )
    assert _drawn_lyrics(job) == []


def test_without_a_guided_revision_chat_is_not_told_the_lyrics_are_on(
    harness, composer, monkeypatch
):
    """The revision is where hidden lyrics are stored; chat must not read `enabled=True` by
    default when it cannot see that revision."""
    from app.services import kria_editor_ops
    from app.services.kria_editor_ops import build_editor_snapshot

    job, variant = _hidden_lyrics_item(harness, monkeypatch)
    assert build_editor_snapshot(job, variant)["captions"]["meta"]["enabled"] is False
    monkeypatch.setattr(kria_editor_ops, "_guided_v2_revision", lambda *_args: None)
    snapshot = build_editor_snapshot(job, variant)
    assert "caption" not in snapshot["allowed_op_families"]
    assert "captions" not in snapshot
