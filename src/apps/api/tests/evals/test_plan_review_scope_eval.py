"""KRI-441 replay eval: a prompt scoped to some plan sections leaves every other lane alone.

Replay mode, no network. Two goldens share ONE recorded (deliberately leaky) model answer for
"make the captions shorter": it shortens two captions but also lowers the music, shortens a
clip and retitles the hook. The scoped twin carries ``scope=["captions"]`` and the families
the server would send; the unscoped twin is the same ask with no scope.

What it proves, layer by layer (``app/kria/plan_review.py``):

1. layer 1 (snapshot families): the parser itself drops the music and clip ops;
2. layer 2 (op filter): the title edit rides the shared ``text`` family, so the op filter
   drops it and names the section it left alone;
3. layer 3 (post-compile repair): even if the title edit is compiled, the repair reverts it.

Without a scope the same recording changes the music, the timeline and the title too.
"""

from __future__ import annotations

import json

from app.agents.edit_copilot import EDIT_COPILOT_PROMPT_VERSION, EditCopilotAgent
from app.kria import plan_review
from app.routes.generative_jobs import EditorCommitRequest
from app.services.kria_editor_ops import compile_editor_ops

from .runners.eval_runner import discover_fixtures, load_fixture, run_eval
from .runners.snapshot_variant import build_synthetic_variant_and_job

FIXTURES = {path.stem: path for path in discover_fixtures("edit_copilot")}


def _golden(name: str):
    return load_fixture(FIXTURES[name])


def _ops(name: str) -> list[dict]:
    result = run_eval(_golden(name))
    assert result.passed, f"{name}: {result.structural_failures} {result.error}"
    assert result.output is not None
    return list(result.output["ops"])


def _lanes(ops: list[dict], snapshot: dict) -> set[str]:
    job, variant = build_synthetic_variant_and_job(snapshot, ops)
    compiled = compile_editor_ops(job, variant, ops)
    assert isinstance(compiled.payload, EditorCommitRequest)
    data = compiled.payload.model_dump(mode="json", exclude_none=True)
    return set(plan_review.lanes_in(data))


def test_scoped_goldens_are_pinned_to_the_current_prompt_version() -> None:
    for name in ("plan_review_scope_captions_layer1", "plan_review_unscoped_same_prompt"):
        assert _golden(name).prompt_version == EDIT_COPILOT_PROMPT_VERSION


def test_scoped_prompt_says_what_to_change_and_unscoped_prompt_is_unchanged() -> None:
    scoped = _golden("plan_review_scope_captions_layer1")
    unscoped = _golden("plan_review_unscoped_same_prompt")
    agent = EditCopilotAgent(None)  # type: ignore[arg-type]
    scoped_prompt = agent.render_prompt(EditCopilotAgent.Input.model_validate(scoped.input))
    unscoped_prompt = agent.render_prompt(EditCopilotAgent.Input.model_validate(unscoped.input))
    assert "Change only: captions. Do not change anything else." in scoped_prompt
    assert "Change only:" not in unscoped_prompt


def test_unscoped_prompt_changes_more_than_captions() -> None:
    snapshot = _golden("plan_review_unscoped_same_prompt").input["variant_snapshot"]
    ops = _ops("plan_review_unscoped_same_prompt")
    assert {op["op"] for op in ops} == {"edit_caption", "set_mix", "set_clip_duration", "edit_text"}
    assert _lanes(ops, snapshot) == {"caption_cues", "mix", "timeline_slots", "text_elements"}


def test_flagged_only_prompt_leaves_every_other_section_untouched() -> None:
    fixture = _golden("plan_review_scope_captions_layer1")
    snapshot = fixture.input["variant_snapshot"]
    assert snapshot["scope"] == ["captions"]

    # Layer 1: music and clip families are not offered, so those ops never survive parsing.
    parsed = _ops("plan_review_scope_captions_layer1")
    assert {op["op"] for op in parsed} == {"edit_caption", "edit_text"}

    # Layer 2: the title edit rides the shared `text` family; the op filter drops it.
    kept, dropped = plan_review.filter_ops_to_scope(parsed, snapshot, ["captions"])
    assert [op["op"] for op in kept] == ["edit_caption", "edit_caption"]
    assert dropped == [{"op": "edit_text", "section": "title"}]
    assert "title" in plan_review.left_alone_note(dropped, ["captions"]).lower()
    assert _lanes(kept, snapshot) == {"caption_cues"}

    # Layer 3: compile the leak anyway; the repair reverts the title bar, not the captions.
    job, variant = build_synthetic_variant_and_job(snapshot, parsed)
    compiled = compile_editor_ops(job, variant, parsed)
    repaired, report = plan_review.repair_compiled(compiled, ["captions"])
    data = repaired.payload.model_dump(mode="json", exclude_none=True)
    assert plan_review.lanes_in(data) == ["caption_cues"]
    assert report.reverted_bars == ["hook"]


def test_leaky_recording_is_fully_repaired_even_with_every_family_open() -> None:
    """The unscoped recording (music + clip + title leak) repaired to a captions scope."""
    snapshot = _golden("plan_review_unscoped_same_prompt").input["variant_snapshot"]
    ops = _ops("plan_review_unscoped_same_prompt")
    job, variant = build_synthetic_variant_and_job(snapshot, ops)
    compiled = compile_editor_ops(job, variant, ops)
    repaired, report = plan_review.repair_compiled(compiled, ["captions"])
    data = repaired.payload.model_dump(mode="json", exclude_none=True)
    assert plan_review.lanes_in(data) == ["caption_cues"]
    assert {"mix", "timeline_slots"} <= set(report.reverted_fields)
    assert json.dumps(data["caption_cues"])  # the flagged lane still carries the edit
