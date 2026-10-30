"""Chat sets caption animation (KRI-1077): the creator's ask reaches every caption line.

Drives the chat path on a phone narrated job: the worker's editor snapshot for the app build
that sent the message, the edit copilot parsing a recorded model answer, the op compiler, the
real editor Save, and the device recipe the iPhone draws. Failure modes written first:

  (a) the worker never sees the sending build, so the snapshot never offers caption animation
      and every caption ask is refused;
  (b) the snapshot offers it to a build below the caption-animation protocol, or with the kill
      switch off, so the approved Save 426s or an older build drops the phases;
  (c) a refused ask still claims success in the reply;
  (d) a line with its own phases keeps them, so "all captions" leaves that line as it was;
  (e) a partial ask ("slower") wipes the entrance already set;
  (f) the receipt does not say which animation the captions got.
"""

from __future__ import annotations

import copy
import json

import pytest

from app.agents.edit_copilot import EditCopilotAgent
from app.routes import generative_jobs as gj
from app.services.client_protocol import (
    bind_sending_build,
    clear_request_context,
    unbind_sending_build,
)
from app.services.device_render import device_status
from app.services.kria_editor_ops import build_editor_snapshot, compile_editor_ops
from app.services.kria_editor_ops_diff import describe_diff
from tests.evals.runners.eval_runner import CassetteModelClient

WORD_RELAY = {"entrance": "wordRelay"}
SLIDE_PULSE = {"entrance": "slide", "exit": "none", "loop": "pulse", "speed": 1.5}


@pytest.fixture(autouse=True)
def _worker_context():
    clear_request_context()
    yield
    clear_request_context()


def _job(monkeypatch, *, line_phases: dict | None = None):
    from tests.routes.test_phone_narrated_editor_commit import _CUES, _VERIFIED_FEATURES, phone_job

    cues = [
        dict(_CUES[0]),
        {**_CUES[1], **({"animation_phases": line_phases} if line_phases else {})},
    ]
    job = phone_job(monkeypatch, cues=cues)
    job.all_candidates = {}
    monkeypatch.setattr(
        gj.settings, "phone_render_verified_features", [*_VERIFIED_FEATURES, "authoredText"]
    )
    return job


def _chat(job, *, sent_from: int | None, phases: dict, utterance: str = "animate the captions"):
    """One chat turn in the worker: snapshot, recorded model answer, parse, compile."""
    variant = copy.deepcopy(job.assembly_plan["variants"][0])
    token = bind_sending_build(sent_from)
    try:
        snapshot = build_editor_snapshot(job, variant)
        answer = {
            "intent": "edit",
            "ops": [{"op": "set_caption_meta", "patch": {"animation_phases": phases}}],
            "confidence": 0.9,
            "reply": "Every caption line now animates.",
            "suggestions": [],
            "needs_clarification": False,
        }
        output = EditCopilotAgent(CassetteModelClient(json.dumps(answer))).run(
            {"utterance": utterance, "variant_snapshot": snapshot}
        )
        draft = compile_editor_ops(job, variant, output.ops) if output.ops else None
    finally:
        unbind_sending_build(token)
    return snapshot, output, draft


def _save(job, draft) -> None:
    gj.prepare_editor_commit(job, "narrated", draft.payload, user_id="owner", plan_item_id="item")


def _caption_phases(job) -> list[dict | None]:
    recipe = device_status(job, "narrated").request.recipe
    return [
        None if layer.animation_phases is None else layer.animation_phases.model_dump()
        for layer in recipe.text_layers
        if layer.id.startswith("caption-")
    ]


def test_all_captions_get_the_animation_including_a_line_that_had_its_own(monkeypatch):
    job = _job(monkeypatch, line_phases=SLIDE_PULSE)
    snapshot, output, draft = _chat(job, sent_from=24, phases=WORD_RELAY)

    assert snapshot["editor_capabilities"] == {"caption_animation": True}
    assert output.outcome == "proposed"
    assert describe_diff(draft.diff.entries) == ["Captions now enter with Word Relay"]
    _save(job, draft)

    relay = {"entrance": "wordRelay", "exit": "none", "loop": "none", "speed": 1.0}
    assert job.assembly_plan["variants"][0]["caption_animation_phases"] == relay
    assert _caption_phases(job) == [relay, relay]


def test_a_partial_ask_keeps_the_animation_already_set(monkeypatch):
    job = _job(monkeypatch)
    _save(job, _chat(job, sent_from=24, phases=WORD_RELAY)[2])

    _snapshot, _output, draft = _chat(job, sent_from=24, phases={"speed": 0.5})
    assert describe_diff(draft.diff.entries) == ["Changed the caption animation"]
    _save(job, draft)

    slow_relay = {"entrance": "wordRelay", "exit": "none", "loop": "none", "speed": 0.5}
    assert _caption_phases(job) == [slow_relay, slow_relay]


@pytest.mark.parametrize(
    ("sent_from", "kill_switch_on"),
    [(23, True), (None, True), (24, False)],
    ids=["older-build", "no-build", "kill-switch-off"],
)
def test_without_the_capability_the_ask_is_refused_honestly(monkeypatch, sent_from, kill_switch_on):
    job = _job(monkeypatch)
    monkeypatch.setattr(gj.settings, "caption_animation_enabled", kill_switch_on)
    snapshot, output, draft = _chat(job, sent_from=sent_from, phases=WORD_RELAY)

    assert "editor_capabilities" not in snapshot
    assert draft is None
    assert output.outcome == "unsupported"
    assert output.reply == "Captions on this video can't animate yet."
    assert [r["reason"] for r in output.rejection_reasons] == ["capability_unavailable"]
