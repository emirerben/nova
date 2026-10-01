"""KRI-219 PR-0: dispatch table + lane registry contracts (no network)."""

from __future__ import annotations

import pytest

from app.agents import edit_copilot, editor_ops_v2
from app.agents._runtime import ModelClient
from app.agents.edit_copilot import (
    EDIT_COPILOT_PROMPT_VERSION,
    EditCopilotAgent,
    EditCopilotInput,
    _family_allowed,
    _parse_op,
    _ParseState,
    editor_operation_contract,
)
from app.agents.editor_ops_v2 import OpSpec, is_v2_snapshot, merge_into_parser
from app.pipeline.prompt_loader import load_prompt
from app.services import kria_editor_ops as ops_mod
from app.services.editor_limits import MAX_EDITOR_OPS
from app.services.kria_editor_ops import KriaEditorOpError, compile_editor_ops
from tests.services.test_kria_editor_ops import _job, _variant


def test_prompt_version_pinned() -> None:
    assert EDIT_COPILOT_PROMPT_VERSION == "2026-10-01-v60"


def test_op_cap_is_a_single_shared_constant() -> None:
    from app.kria.registry import ApplyEditorOpsArguments

    assert MAX_EDITOR_OPS == 16
    assert ops_mod.MAX_EDITOR_OPS == MAX_EDITOR_OPS
    limit = ApplyEditorOpsArguments.model_fields["operations"].metadata
    assert any(getattr(m, "max_length", None) == MAX_EDITOR_OPS for m in limit)


def test_lane_specs_are_wellformed() -> None:
    specs = editor_ops_v2.lane_specs()
    new = [s.name for s in specs if s.coerce is not None]
    assert len(new) == len(set(new)), "two lanes declared the same new op"
    assert all(s.family for s in specs if s.coerce is not None)


def test_timeline_lane_registers_its_specs_and_fragment() -> None:
    names = {spec.name for spec in editor_ops_v2.lane_specs()}
    assert {"patch_slots", "set_total_duration"} <= names
    assert "patch_slots" in editor_ops_v2.prompt_fragments()


def test_lane_specs_only_extend_or_add_known_ops() -> None:
    names = [spec.name for spec in editor_ops_v2.lane_specs()]
    assert len(names) == len(set(names))


def _snapshot(**extra) -> dict:
    return {
        "allowed_op_families": ["text", "style", "timeline"],
        "text_bars": [{"text": "a"}],
        **extra,
    }


def test_prompt_is_byte_identical_without_marker_and_with_empty_fragments(monkeypatch) -> None:
    monkeypatch.setattr(editor_ops_v2, "prompt_fragments", lambda: "")
    plain = _snapshot()
    marked = _snapshot(editor_ops_version=2)

    def render(snapshot: dict) -> str:
        return EditCopilotAgent(ModelClient()).render_prompt(
            EditCopilotInput(utterance="make it pop", variant_snapshot=snapshot)
        )

    baseline = render(plain)
    expected = load_prompt(
        "edit_copilot",
        utterance=edit_copilot._clean_utterance("make it pop"),
        prior_turns=edit_copilot._format_prior_turns([]),
        original_request_block=edit_copilot._original_request_block(None),
        snapshot=edit_copilot._format_snapshot(plain),
        font_catalog=edit_copilot._font_catalog(),
        effect_catalog=edit_copilot._effect_catalog(),
        caption_font_catalog=edit_copilot._caption_font_catalog(),
        custom_effect_catalog=edit_copilot._custom_effect_catalog(),
        max_ops=edit_copilot._MAX_OPS,
    )
    assert baseline == expected
    assert render(marked) == baseline
    assert "editor_ops_version" not in baseline
    # Empty fragments: even a v2 snapshot appends nothing.
    assert edit_copilot._with_v2_fragments(baseline, marked) == baseline
    assert editor_operation_contract(plain) == editor_operation_contract(marked)


def test_fragments_append_only_under_marker(monkeypatch) -> None:
    monkeypatch.setattr(editor_ops_v2, "prompt_fragments", lambda: "LANE FRAGMENT")
    assert edit_copilot._with_v2_fragments("base", _snapshot()) == "base"
    assert (
        edit_copilot._with_v2_fragments("base", _snapshot(editor_ops_version=2))
        == "base\n\nLANE FRAGMENT"
    )


def test_merge_gates_v2_ops_on_marker_and_extends_existing(monkeypatch) -> None:
    valid = set(edit_copilot._VALID_OPS)
    required = dict(edit_copilot._OP_REQUIRED)
    fields = dict(edit_copilot._OP_FIELDS)
    registry = editor_ops_v2.MergedRegistry()
    monkeypatch.setattr(editor_ops_v2, "REGISTRY", registry)
    monkeypatch.setattr(edit_copilot, "_v2", editor_ops_v2)
    monkeypatch.setattr(edit_copilot, "_VALID_OPS", valid)
    monkeypatch.setattr(edit_copilot, "_OP_REQUIRED", required)
    monkeypatch.setattr(edit_copilot, "_OP_FIELDS", fields)

    def coerce(name, payload, snapshot, state):  # noqa: ANN001, ANN202
        return {"selector": payload["selector"]}

    specs = [
        OpSpec(
            name="zz_new_op",
            required=frozenset({"selector"}),
            fields=frozenset({"selector"}),
            family=frozenset({"text"}),
            coerce=coerce,
        ),
        OpSpec(name="set_mix", fields=frozenset({"original_level"})),
    ]
    monkeypatch.setattr(editor_ops_v2, "lane_specs", lambda: specs)
    merge_into_parser(valid, required, fields)

    assert "zz_new_op" in valid and required["zz_new_op"] == {"selector"}
    assert fields["set_mix"] >= {"music_level", "original_level"}
    raw = {"op": "zz_new_op", "selector": {"group": "labels"}}
    assert not _family_allowed("zz_new_op", _snapshot())
    assert _family_allowed("zz_new_op", _snapshot(editor_ops_version=2))
    assert _parse_op(raw, _snapshot(), _ParseState(0.9)) is None
    parsed = _parse_op(raw, _snapshot(editor_ops_version=2), _ParseState(0.9))
    assert parsed == {"op": "zz_new_op", "selector": {"group": "labels"}}
    assert is_v2_snapshot({"editor_ops_version": 2})
    assert not is_v2_snapshot({"editor_ops_version": True + 0})


def test_new_op_without_coerce_is_rejected() -> None:
    with pytest.raises(ValueError):
        import app.agents.editor_ops_v2 as v2

        orig = v2.lane_specs
        v2.lane_specs = lambda: [OpSpec(name="brand_new_op")]
        try:
            merge_into_parser(set(), {}, {})
        finally:
            v2.lane_specs = orig


def test_register_handler_dispatch_duplicate_and_unregistered(monkeypatch) -> None:
    table = dict(ops_mod._OP_HANDLERS)
    monkeypatch.setattr(ops_mod, "_OP_HANDLERS", table)
    variant = _variant()
    job = _job(variant)

    with pytest.raises(KriaEditorOpError, match="not portable to Kria yet"):
        compile_editor_ops(job, variant, [{"op": "zz_test_op"}])

    def handler(state, op):  # noqa: ANN001, ANN202
        state.text[0]["text"] = "handled"
        state.changed.add("text")

    ops_mod.register_handler("zz_test_op", handler)
    compiled = compile_editor_ops(job, variant, [{"op": "zz_test_op"}])
    assert compiled.payload.text_elements[0]["text"] == "handled"
    with pytest.raises(ValueError):
        ops_mod.register_handler("zz_test_op", handler)
    ops_mod.register_handler("zz_test_op", handler, replace=True)


def test_built_in_ops_all_have_handlers() -> None:
    portable = {
        "remove_visual_media", "edit_text", "patch_text_style", "set_text_timing", "add_text",
        "remove_text", "label_each_clip", "patch_text_appearance", "set_clip_duration",
        "set_clip_in", "trim_clip_start", "set_look_preset", "trim_output_start",
        "reorder_clip", "remove_clip", "split_clip", "set_transition", "add_unused_sources",
        "set_media_duration", "stack_images", "edit_caption", "replace_caption_text",
        "set_caption_timing", "set_caption_emphasis", "set_caption_meta", "set_mix",
        "remove_music", "swap_music", "set_title", "add_camera_effect", "patch_camera_effect",
        "remove_camera_effect", "patch_sfx", "remove_sfx",
    }  # fmt: skip
    assert portable <= set(ops_mod._OP_HANDLERS)


def test_build_editor_snapshot_marks_server_snapshots(monkeypatch) -> None:
    variant = _variant()
    monkeypatch.setattr(
        "app.services.kria_editor_ops._editor_capabilities",
        lambda _job, _variant: {"text_elements": True},
    )
    assert ops_mod.build_editor_snapshot(_job(variant), variant)["editor_ops_version"] == 2


def test_lane_modules_import_standalone() -> None:
    import subprocess
    import sys

    for lane in editor_ops_v2.LANES:
        subprocess.run(
            [sys.executable, "-c", f"import app.agents.editor_ops_v2.{lane}"],
            check=True,
        )
