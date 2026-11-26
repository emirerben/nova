"""The chat copilot dims clips through ``patch_slots.dim``: the model's ops run through the
real parser (``EditCopilotAgent.parse``) and the real compiler (``compile_editor_ops``), and the
rows the compiler hands to the Save model are read back.

Failure modes, one test each:
- "dim all clips 40%" dims one clip, or stores 40 instead of 0.4;
- "remove the dim from this clip" removes it from every clip, or omits the key so the Save keeps
  the stored dim;
- the receipt names the wrong clips or the wrong strength;
- a percent, a negative or a word reaches the draft instead of a plain refusal;
- an ask that changes nothing reports success;
- a closed gate changes the snapshot, the prompt or the tools-mode tool schema, or lets the model
  claim a dim it cannot make.
"""

from __future__ import annotations

import copy
import json
from typing import Any

import pytest

from app.agents import editor_ops_v2 as v2
from app.agents._runtime import ModelClient
from app.agents.copilot_tools import copilot_tools
from app.agents.edit_copilot import (
    EditCopilotAgent,
    EditCopilotInput,
    _format_editor_focus,
    _format_snapshot,
)
from app.services import kria_editor_ops
from app.services.kria_editor_ops import (
    KriaEditorOpError,
    build_editor_snapshot,
    compile_editor_ops,
)
from app.services.kria_editor_ops_diff import describe_diff
from tests.kria.test_kri558_editor_receipts import _req
from tests.services.test_kria_editor_ops import _job, _variant

OPEN = {
    "text_elements": True,
    "timeline": True,
    "clips": {"dim": {"editable": True, "reason": None}},
}
CLOSED = {"text_elements": True, "timeline": True, "clips": {}}


def _caps(monkeypatch: pytest.MonkeyPatch, caps: dict[str, Any]) -> None:
    monkeypatch.setattr("app.services.kria_editor_ops._editor_capabilities", lambda *_: caps)


def _three_clips(*dims: float | None) -> dict:
    """a.mp4 (video), b.jpg (photo), c.png (photo), each with the given dim (absent for None)."""
    variant = _variant()
    slots = variant["ai_timeline"]["slots"]
    slots.append(
        {**slots[1], "slot_id": "slot-3", "clip_index": 2, "source_gcs_path": "users/u/c.png"}
    )
    for slot, dim in zip(slots, dims, strict=False):
        if dim is not None:
            slot["dim"] = dim
    return variant


def _snapshot(monkeypatch, variant: dict, caps: dict = OPEN) -> dict:
    _caps(monkeypatch, caps)
    return build_editor_snapshot(_job(variant), variant)


def _dim(selector: dict, dim: Any) -> dict:
    return {"op": "patch_slots", "selector": selector, "patch": {"dim": dim}}


def _parse(snapshot: dict, *ops: dict, reply: str = "Done.", utterance: str = "dim"):
    raw = json.dumps({"intent": "edit", "ops": list(ops), "confidence": 0.95, "reply": reply})
    return EditCopilotAgent(ModelClient()).parse(
        raw, EditCopilotInput(utterance=utterance, variant_snapshot=snapshot)
    )


def _compile(monkeypatch, variant: dict, *ops: dict):
    """(draft, rows): the draft and the slot rows the Save model is built from."""
    seen: dict[str, list[dict]] = {}
    real = kria_editor_ops._timeline_models

    def spy(rows):
        seen.setdefault("rows", copy.deepcopy(rows))
        return real(rows)

    monkeypatch.setattr(kria_editor_ops, "_timeline_models", spy)
    draft = compile_editor_ops(_job(variant), variant, list(ops))
    return draft, seen["rows"]


def _said(draft) -> list[str]:
    return describe_diff(draft.diff.entries, live_text_count=draft.diff.live_text_count)


def test_dim_all_clips_forty_percent_sets_every_clip(monkeypatch):
    variant = _three_clips()
    snapshot = _snapshot(monkeypatch, variant)
    parsed = _parse(snapshot, _dim({"all": True}, 0.4), utterance="dim all clips 40%")
    assert parsed.ops == [_dim({"all": True}, 0.4)]

    draft, rows = _compile(monkeypatch, variant, *parsed.ops)
    assert [row["dim"] for row in rows] == [0.4, 0.4, 0.4]
    assert _said(draft) == ["Dimmed 3 clips 40%"]


def test_remove_the_dim_from_this_clip_clears_only_that_clip(monkeypatch):
    variant = _three_clips(0.4, 0.4, 0.4)
    snapshot = _snapshot(monkeypatch, variant)
    assert [slot.get("dim") for slot in snapshot["slots"]] == [0.4, 0.4, 0.4]
    snapshot["editor_focus"] = {"selected": {"kind": "clip", "id": "slot-2", "index": 1}}
    assert "selected=" in _format_editor_focus(snapshot)
    assert " dim=0.4" in _format_snapshot(snapshot)
    parsed = _parse(
        snapshot, _dim({"slot_indexes": [1]}, None), utterance="remove the dim from this clip"
    )

    draft, rows = _compile(monkeypatch, variant, *parsed.ops)
    assert [row.get("dim", "absent") for row in rows] == [0.4, None, 0.4]
    assert _said(draft) == ["Removed the dim from clip 2"]


def test_darken_this_clip_dims_the_selected_clip_only(monkeypatch):
    variant = _three_clips()
    snapshot = _snapshot(monkeypatch, variant)
    parsed = _parse(snapshot, _dim({"slot_indexes": [2]}, 0.3), utterance="darken this clip")

    draft, rows = _compile(monkeypatch, variant, *parsed.ops)
    assert [row.get("dim") for row in rows] == [None, None, 0.3]
    assert _said(draft) == ["Dimmed clip 3 30%"]


def test_a_dim_of_zero_is_a_removal_and_one_is_black(monkeypatch):
    variant = _three_clips(0.5)
    snapshot = _snapshot(monkeypatch, variant)
    zero = _parse(snapshot, _dim({"slot_indexes": [0]}, 0))
    assert zero.ops == [_dim({"slot_indexes": [0]}, None)]
    black = _parse(snapshot, _dim({"slot_indexes": [0]}, 1))
    _, rows = _compile(monkeypatch, variant, *black.ops)
    assert rows[0]["dim"] == 1.0


@pytest.mark.parametrize("bad", [40, -0.2, 1.5, "40%", True, {"strength": 0.4}])
def test_an_unusable_strength_is_refused_in_plain_words(monkeypatch, bad):
    snapshot = _snapshot(monkeypatch, _three_clips())
    parsed = _parse(snapshot, _dim({"all": True}, bad), reply="Dimmed every clip.")
    assert parsed.ops == []
    assert "Dimmed" not in parsed.reply
    assert "0.4 is 40% darker" in parsed.reply


def test_an_ask_that_changes_nothing_says_so(monkeypatch):
    variant = _three_clips(0.4, 0.4, 0.4)
    _caps(monkeypatch, OPEN)
    with pytest.raises(KriaEditorOpError, match="already dimmed that much"):
        compile_editor_ops(_job(variant), variant, [_dim({"all": True}, 0.4)])
    plain = _three_clips()
    with pytest.raises(KriaEditorOpError, match="no dim to remove"):
        compile_editor_ops(_job(plain), plain, [_dim({"all": True}, None)])


def test_a_closed_gate_leaves_the_snapshot_and_the_prompt_as_they_were(monkeypatch):
    variant = _three_clips(0.4)
    snapshot = _snapshot(monkeypatch, variant, CLOSED)
    assert "dim" not in snapshot["timeline_patch_capabilities"]
    assert all("dim" not in slot for slot in snapshot["slots"])
    assert " dim=" not in _format_snapshot(snapshot)
    assert "CLIP DIM" not in v2.family_fragments(snapshot)

    opened = _snapshot(monkeypatch, variant, OPEN)
    assert "CLIP DIM" in v2.family_fragments(opened)
    assert v2.family_fragments(opened).startswith(v2.family_fragments(snapshot))

    closed_tools = [tool.model_dump() for tool in copilot_tools(snapshot, {})]
    opened_tools = [tool.model_dump() for tool in copilot_tools(opened, {})]
    assert '"dim"' not in json.dumps(closed_tools)
    patch_slots = next(tool for tool in opened_tools if tool["name"] == "patch_slots")
    offered = patch_slots["parameters"]["properties"]["patch"]["properties"].pop("dim")
    assert offered == {"anyOf": [{"type": "number"}, {"type": "null"}]}
    assert json.dumps(opened_tools, sort_keys=True) == json.dumps(closed_tools, sort_keys=True)


def test_a_closed_gate_refuses_the_ask_instead_of_claiming_it(monkeypatch):
    snapshot = _snapshot(monkeypatch, _three_clips(), CLOSED)
    parsed = _parse(snapshot, _dim({"all": True}, 0.4), reply="Dimmed every clip 40%.")
    assert parsed.ops == []
    assert parsed.outcome == "unsupported"
    assert "Dimmed" not in parsed.reply
    assert "can't dim" in parsed.reply


def test_a_closed_gate_also_stops_an_op_that_skips_the_parser(monkeypatch):
    variant = _three_clips()
    _caps(monkeypatch, CLOSED)
    with pytest.raises(KriaEditorOpError, match="isn't available"):
        compile_editor_ops(_job(variant), variant, [_dim({"all": True}, 0.4)])


def test_the_chat_receipt_names_the_dim_it_made(monkeypatch):
    from dataclasses import replace

    from app.kria.brief_checks import build_receipts, plan_facts_from_editor_payload
    from app.kria.editor_receipts import bind_editor_receipts, reply_from_editor_receipts

    variant = _three_clips()
    _caps(monkeypatch, OPEN)
    compiled = compile_editor_ops(_job(variant), variant, [_dim({"all": True}, 0.4)])
    payload = compiled.payload.model_dump(mode="json", exclude_none=True)
    facts = plan_facts_from_editor_payload(payload, compiled.text_diff or None, compiled.changes)
    facts = replace(facts, anaphora_rows=compiled.diff.changed_text_ids())
    reqs = [_req("style", "dim all clips 40%")]
    receipts = build_receipts(reqs, facts, include_unchecked=True)
    receipts, unbound = bind_editor_receipts(reqs, receipts, compiled.diff, facts.text_styles or ())
    assert receipts[0].reason == "Dimmed 3 clips 40%"
    assert reply_from_editor_receipts(reqs, receipts, unbound=[]) == (
        "Have a look: dim all clips 40% (I changed: Dimmed 3 clips 40%)."
    )
