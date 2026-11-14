"""KRI-1393: the copilot's tool definitions and its parser are one contract.

Fails when an operation the parser accepts has no tool, when a tool names an operation the
parser does not know, when a tool's fields or required fields differ from the parser's, when
no edit could ever offer an operation as a tool, or when the rules prompt stops matching what
the tool transport rewrites.
"""

from __future__ import annotations

import pytest

from app.agents.copilot_tools import (
    REPLY_TOOL,
    copilot_tools,
    operation_tool,
    patch_fields,
    tools_rules,
)
from app.agents.edit_copilot import (
    _OP_FIELDS,
    _OP_REQUIRED,
    _VALID_OPS,
    SLIDE_POST_SURFACE,
    EditCopilotAgent,
    EditCopilotInput,
    _op_fields,
)
from app.agents.editor_ops_v2.split_screen import _MODELS as SPLIT_SCREEN_MODELS

_OPEN_VIDEO = {
    "editor_ops_version": 2,
    "allowed_op_families": ["all", "text", "visual_media"],
    "label_facts": True,
}
_OPEN_SLIDES = {**_OPEN_VIDEO, "surface": SLIDE_POST_SURFACE}


def test_every_parser_op_has_exactly_one_tool_and_every_tool_is_a_parser_op():
    tools = {name: operation_tool(name) for name in _OP_FIELDS}
    assert set(tools) == set(_VALID_OPS)
    assert REPLY_TOOL not in _VALID_OPS


@pytest.mark.parametrize("name", sorted(_OP_FIELDS))
def test_tool_fields_and_required_fields_are_the_parsers(name):
    parameters = operation_tool(name).parameters
    assert set(parameters["properties"]) == set(_op_fields(name)) - {"integrity"}
    required = (
        set(SPLIT_SCREEN_MODELS[name].model_json_schema().get("required", []))
        if name in SPLIT_SCREEN_MODELS
        else set(_OP_REQUIRED[name])
    )
    assert set(parameters["required"]) == required
    assert parameters["additionalProperties"] is False
    assert "$ref" not in str(parameters)


def test_some_edit_offers_every_op_as_a_tool(monkeypatch):
    from app.config import settings

    monkeypatch.setattr(settings, "custom_effects_enabled", True)
    offered = {
        tool.name
        for snapshot in (_OPEN_VIDEO, _OPEN_SLIDES)
        for tool in copilot_tools(snapshot, {})
    }
    assert offered - {REPLY_TOOL} == set(_VALID_OPS)


def test_a_closed_family_is_not_offered():
    tools = {tool.name for tool in copilot_tools({"allowed_op_families": ["text"]}, {})}
    assert "edit_text" in tools
    assert not tools & {"set_clip_duration", "swap_music", "add_sfx", "set_caption_meta"}


def test_tools_rules_moves_each_op_definition_into_its_tool_and_keeps_the_rest():
    rules = EditCopilotAgent(None).rules_prompt(EditCopilotInput())
    system, prose = tools_rules(rules)
    assert set(prose) <= set(_OP_FIELDS)
    assert {"edit_text", "patch_text_appearance", "set_clip_duration"} <= set(prose)
    assert '{"op":"edit_text","bar_index":0,"text":"new hook"}' not in system
    assert "Return ONLY this JSON object" not in system
    assert "## Redirects for unavailable capabilities" in system
    assert "`unmet_requests` itemizes EVERY part" in system
    assert operation_tool("edit_text", prose["edit_text"]).description.endswith(
        '{"bar_index":0,"text":"new hook"}'
    )


def test_every_op_with_a_patch_offers_exactly_its_coercers_keys():
    with_patch = {name for name in _OP_FIELDS if "patch" in _op_fields(name)}
    assert set(patch_fields()) == with_patch
    for name, keys in patch_fields().items():
        assert set(operation_tool(name).parameters["properties"]["patch"]["properties"]) == keys
