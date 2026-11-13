"""Typed tool calling for the edit copilot (KRI-1393): one tool per editor operation.

``EDIT_COPILOT_OUTPUT_MODE=tools`` runs ``ToolsEditCopilotAgent``. Each operation open on the
edit (``_family_allowed``) is offered as a tool whose arguments schema is that operation's entry
in the typed wire registry (``_creator_wire.copilot_operation_schemas``), so the parser and the
tool schemas come from one table. A ``reply`` tool carries the turn's non-operation fields.

The rules prompt goes in the provider's system block (cached); the turn block is the user
message. Each operation's definition bullet moves out of the rules into its tool description,
so an operation closed on this edit costs no prompt at all.

The model's calls are turned back into the JSON answer the copilot parser already reads, so
partial apply, the honesty rules and the repair round are unchanged. A repair round sends the
first answer back as the model's turn, with one tool result per call: the rejected calls carry
``repair_feedback`` and every other call is acknowledged.
"""

from __future__ import annotations

import copy
import hashlib
import json
import re
from functools import lru_cache
from typing import Any

from pydantic import ValidationError

from app.agents._creator_wire import (
    copilot_field_schemas,
    copilot_operation_schemas,
    copilot_schema,
)
from app.agents._runtime import SchemaError
from app.agents._tool_calls import ToolAnswer, ToolExchange, ToolRequest, ToolResult, ToolSpec
from app.agents.edit_copilot import (
    _BULK_OPS,
    _OP_FIELDS,
    MAX_TOOL_ROUNDS,
    CopilotRepair,
    EditCopilotAgent,
    EditCopilotInput,
    EditCopilotOutput,
    RejectedOp,
    _family_allowed,
    repair_feedback,
    repair_item_lines,
)
from app.config import settings
from app.kria.reply_language import prompt_language_line, say
from app.pipeline.prompt_loader import load_prompt

TOOLS_VERSION = "copilot-tools-v1"
REPLY_TOOL = "reply"
# Carries a call's id through the parser to its rejection. The parser keeps only an op's own
# fields, so the key never reaches a parsed op.
CALL_KEY = "_tool_call"

_OP_BULLET = re.compile(r"^- ([a-z_]+): ")
_OUTPUT_JSON = re.compile(r"Return ONLY this JSON object:\n\n\{\n.*?\n\}\n", re.DOTALL)
_REPLY_NOW = (
    "Now call `reply` for this message: say what applied, list in unmet_requests what did not, "
    "and name in claimed_ops only operations that applied. Correct a NOT applied operation by "
    "calling its tool again first, in the same response."
)
_TOOLS_REPAIR_ANSWER = (
    "For each item, call ONE corrected operation tool, or, when it cannot be done on this "
    'edit, call `reply` with an `unmet_requests` entry `{"request", "reason", "repairs": '
    '"<id>"}` written for the creator. Call no other operation tools.'
)


@lru_cache(maxsize=2)
def _registry(direction_pace: bool) -> tuple[dict[str, dict], dict]:
    """The wire registry's operation schemas and the full answer schema (``direction_pace``
    keys the cache: ``set_edit_direction`` gains ``pace`` with that setting)."""
    del direction_pace
    return copilot_operation_schemas(), copilot_schema()


def _schemas() -> tuple[dict[str, dict], dict]:
    return _registry(settings.copilot_direction_follows_request_enabled)


def patch_fields() -> dict[str, frozenset[str]]:
    """The keys each op's ``patch`` may set, from the coercer that reads that patch. The wire
    types ``patch`` as the union of every op's keys; a tool offers only its own."""
    from app.agents.edit_copilot import (
        _CAPTION_META_FIELDS,
        _OVERLAY_PATCH_FIELDS,
        _STYLE_PATCH_FIELDS,
        _TEXT_APPEARANCE_FIELDS,
        MOTION_PATCH_FIELDS,
    )
    from app.agents.editor_ops_v2.text import text_patch_fields
    from app.agents.editor_ops_v2.timeline import _PATCH_KEYS as SLOT_PATCH_FIELDS

    text = text_patch_fields()
    return {
        "patch_text_style": _STYLE_PATCH_FIELDS,
        "patch_text_appearance": _TEXT_APPEARANCE_FIELDS,
        "patch_overlay": _OVERLAY_PATCH_FIELDS,
        "set_caption_meta": _CAPTION_META_FIELDS,
        "patch_motion_block": MOTION_PATCH_FIELDS,
        "add_text": text,
        "patch_text": text,
        "replace_text_sequence": text,
        "patch_slots": SLOT_PATCH_FIELDS,
    }


# What the parser keeps of a clarification context (`_sanitize_clarification_context`).
_CLARIFICATION_KEYS = ("selector", "scope", "media_kind", "quantifier", "referent")
_BULK_SELECTOR_KEYS = ("scope", "media_kind", "quantifier")
_JSON_REFS = ("#/$defs/JSONValue", "#/$defs/NonNullJSONValue")


def _kind(node: Any) -> str:
    if not isinstance(node, dict) or node.get("$ref") in _JSON_REFS:
        return "any JSON"
    if "const" in node:
        return json.dumps(node["const"])
    if "anyOf" in node:
        return " or ".join(_kind(branch) for branch in node["anyOf"])
    if "enum" in node:
        return "one of " + "|".join(map(str, node["enum"]))
    if node.get("type") == "array":
        return "list of " + _kind(node.get("items"))
    if node.get("type") == "object" and "properties" in node:
        return "object with " + ", ".join(node["properties"])
    return str(node.get("type", "any JSON"))


def _tool_schema(node: Any, depth: int = 0) -> Any:
    """The wire schema as a tool argument schema: typed at the top level, nested objects
    described by their keys and types.

    Gemini refuses a request whose tool schemas pass a size limit (a phone edit's ~45 fully
    typed op tools were refused; every tool alone was accepted) and any recursive reference,
    so the nesting below an op's own fields, and the recursive JSONValue, become text. The
    tools are non-strict on every provider, and the parser validates every argument.
    """
    if isinstance(node, list):
        return [_tool_schema(value, depth) for value in node]
    if not isinstance(node, dict):
        return node
    if node.get("$ref") in _JSON_REFS:
        return {"description": "Any JSON value; its shape is in the rules."}
    if depth >= 1 and node.get("type") == "object" and "properties" in node:
        required = set(node.get("required", []))
        fields = ", ".join(
            f"{key} ({_kind(value)}{', required' if key in required else ''})"
            for key, value in node["properties"].items()
        )
        return {"type": "object", "description": f"Fields: {fields}."}
    result: dict[str, Any] = {}
    for key, value in node.items():
        if key == "properties":
            result[key] = {k: _tool_schema(v, depth + 1) for k, v in value.items()}
        elif key in ("items", "anyOf"):
            result[key] = _tool_schema(value, depth)
        else:
            result[key] = value
    return result


def _description(name: str, prose: str | None) -> str:
    if not prose:
        return f"Editor operation {name}. Its rules are in the system prompt."
    # The examples were written as whole ops; as tool arguments they have no "op" key.
    prose = prose.replace(f'{{"op":"{name}",', "{").replace(f'{{"op":"{name}"}}', "{}")
    return f"Editor operation {name}. Example arguments and rules: {prose}"


def operation_tool(name: str, prose: str | None = None) -> ToolSpec:
    schemas, _ = _schemas()
    branch = schemas[name]
    properties = {k: v for k, v in branch["properties"].items() if k != "op"}
    keys = patch_fields().get(name)
    if keys is not None and "patch" in properties:
        properties["patch"] = {
            **properties["patch"],
            "properties": copilot_field_schemas(sorted(keys)),
        }
    parameters = {
        "type": "object",
        "properties": properties,
        "required": [field for field in branch.get("required", []) if field != "op"],
        "additionalProperties": False,
    }
    return ToolSpec(
        name=name,
        description=_description(name, prose),
        parameters=_tool_schema(parameters),
    )


def reply_tool(open_ops: set[str]) -> ToolSpec:
    _, full = _schemas()
    properties = copy.deepcopy({k: v for k, v in full["properties"].items() if k != "ops"})
    unmet = properties["unmet_requests"]["items"]
    unmet["properties"]["repairs"] = {
        "type": "string",
        "description": "The repair id (r1, r2, ...) of a NOT applied operation this entry answers.",
    }
    pending = [
        branch
        for branch in properties["pending_actions"]["items"]["anyOf"]
        if branch["properties"]["op"]["const"] in _BULK_OPS & open_ops
    ]
    if pending:
        properties["pending_actions"]["items"] = {"anyOf": pending}
    else:
        del properties["pending_actions"]
    context = properties["clarification_context"]["anyOf"][0]
    context["properties"] = {k: context["properties"][k] for k in _CLARIFICATION_KEYS}
    selector = context["properties"]["selector"]
    selector["properties"] = {k: selector["properties"][k] for k in _BULK_SELECTOR_KEYS}
    parameters = {
        "type": "object",
        "properties": properties,
        "required": ["intent", "reply"],
        "additionalProperties": False,
    }
    return ToolSpec(
        name=REPLY_TOOL,
        description=(
            "Finish the turn. Call exactly once, after every operation call of this answer, "
            "with the user-facing reply and the turn's fields (see Output in the system prompt)."
        ),
        parameters=_tool_schema(parameters),
    )


def tools_rules(rules_prompt: str) -> tuple[str, dict[str, str]]:
    """The rules prompt for the tool transport, and each operation's definition bullet.

    A bullet ``- <op>: ...`` and its indented continuation lines leave the rules and become
    that operation's tool description. The JSON output block becomes the tool-call contract.
    """
    kept: list[str] = []
    prose: dict[str, list[str]] = {}
    current: str | None = None
    for line in rules_prompt.split("\n"):
        match = _OP_BULLET.match(line)
        if match and match.group(1) in _OP_FIELDS:
            current = match.group(1)
            prose.setdefault(current, []).append(line[match.end() :].strip())
        elif current is not None and line.startswith("  "):
            prose[current].append(line.strip())
        else:
            current = None
            kept.append(line)
    contract = load_prompt("edit_copilot_tools").strip() + "\n"
    system, replaced = _OUTPUT_JSON.subn(lambda _: contract, "\n".join(kept), count=1)
    if replaced != 1:
        raise RuntimeError("edit_copilot prompt has no JSON output block to replace")
    return system, {name: " ".join(lines) for name, lines in prose.items()}


def copilot_tools(snapshot: dict, prose: dict[str, str]) -> list[ToolSpec]:
    """A tool per operation open on this edit, then ``reply``."""
    schemas, _ = _schemas()
    open_ops = [name for name in sorted(schemas) if _family_allowed(name, snapshot)]
    return [
        *(operation_tool(name, prose.get(name)) for name in open_ops),
        reply_tool(set(open_ops)),
    ]


def _reply_arguments(answer: ToolAnswer) -> dict[str, Any] | None:
    replies = [
        call.arguments
        for call in answer.calls
        if call.name == REPLY_TOOL and call.arguments is not None
    ]
    return replies[-1] if replies else None


def _operations(answer: ToolAnswer) -> list[dict[str, Any]]:
    """The operation calls in the order the model made them, as the parser's raw ops."""
    return [
        {**(call.arguments or {}), "op": call.name, CALL_KEY: call.id}
        for call in answer.calls
        if call.name != REPLY_TOOL
    ]


def _without_call_key(item: RejectedOp) -> RejectedOp:
    if not isinstance(item.raw, dict) or CALL_KEY not in item.raw:
        return item
    return item.model_copy(update={"raw": {k: v for k, v in item.raw.items() if k != CALL_KEY}})


def tool_results(
    answer: ToolAnswer, not_applied: list[RejectedOp], repair: CopilotRepair
) -> list[ToolResult]:
    """One result per call of ``answer``. A call that did not apply says why (with its repair
    id when a round may fix it); the others are accepted. ``reply``, or else the first refused
    call, or else the last call, also carries the repair block and what to do next."""
    ids = {id(item): index for index, item in enumerate(repair.rejected, start=1)}
    by_call: dict[str, RejectedOp] = {}
    for item in [*repair.rejected, *not_applied]:
        call_id = item.raw.get(CALL_KEY) if isinstance(item.raw, dict) else None
        if isinstance(call_id, str):
            by_call.setdefault(call_id, item)
    results: list[ToolResult] = []
    for call in answer.calls:
        item = by_call.get(call.id)
        if call.name == REPLY_TOOL:
            content = "Received."
        elif item is None:
            content = "Accepted."
        else:
            index = ids.get(id(item))
            why = repair_item_lines(index or 0, _without_call_key(item))[1].strip()
            content = f"NOT applied, repair id r{index}. {why}" if index else f"NOT applied. {why}"
        results.append(
            ToolResult(call_id=call.id, name=call.name, content=content, is_error=item is not None)
        )
    tail = []
    if repair.rejected:
        tail.append(
            repair_feedback(
                [_without_call_key(item) for item in repair.rejected], answer=_TOOLS_REPAIR_ANSWER
            )
        )
    if repair.awaiting_reply:
        tail.append(_REPLY_NOW)
    if results and tail:
        carrier = next(
            (i for i, call in enumerate(answer.calls) if call.name == REPLY_TOOL),
            next((i for i, result in enumerate(results) if result.is_error), len(results) - 1),
        )
        target = results[carrier]
        results[carrier] = target.model_copy(
            update={"content": "\n\n".join([target.content, *tail])}
        )
    return results


def _with_exchange(
    output: EditCopilotOutput,
    repair: CopilotRepair,
    answer: ToolAnswer,
    input: EditCopilotInput,  # noqa: A002
) -> EditCopilotOutput:
    """``output`` whose next round carries this answer and its tool results."""
    prior = input.repair.exchanges if input.repair is not None else []
    exchange = ToolExchange(answer=answer, results=tool_results(answer, output.not_applied, repair))
    return output.model_copy(
        update={"repair": repair.model_copy(update={"exchanges": [*prior, exchange]})}
    )


class ToolsEditCopilotAgent(EditCopilotAgent):
    """The edit copilot answering with typed tool calls instead of one JSON object."""

    response_json = False

    def required_fields(self) -> list[str]:
        return []

    def render_prompt(self, input: EditCopilotInput) -> str:  # noqa: A002
        turn = f"{self.turn_prompt(input).rstrip(chr(10))}\n"
        language_line = prompt_language_line(input.reply_language)
        return f"{turn.rstrip(chr(10))}\n\n{language_line}\n" if language_line else turn

    def tool_request(self, input: EditCopilotInput) -> ToolRequest:  # noqa: A002
        system, prose = tools_rules(self.rules_prompt(input))
        tools = copilot_tools(input.variant_snapshot, prose)
        digest = hashlib.sha256(
            json.dumps([system, [tool.model_dump() for tool in tools]], sort_keys=True).encode()
        ).hexdigest()[:24]
        return ToolRequest(
            system=system,
            tools=tools,
            history=list(input.repair.exchanges) if input.repair is not None else [],
            cache_key=f"{self.spec.name}:{digest}",
        )

    def parse(self, raw_text: str, input: EditCopilotInput) -> EditCopilotOutput:  # noqa: A002
        try:
            answer = ToolAnswer.decode(raw_text)
        except (ValueError, ValidationError) as exc:
            raise SchemaError("edit_copilot: the answer is not a tool-call answer") from exc
        repair = input.repair
        reply = _reply_arguments(answer)
        ops = _operations(answer)
        if reply is None and (repair is None or repair.awaiting_reply):
            # The turn's intent, confidence and reply are the model's to state, never invented
            # here. Operations without them (a model that calls one tool per response) get a
            # round that reports what applied and asks for the rest, inside the repair rounds'
            # cap. Nothing at all (prose) is a schema error the runtime retries.
            next_round = repair.repair_round + 1 if repair is not None else 1
            if not ops or next_round > MAX_TOOL_ROUNDS:
                raise SchemaError(
                    "edit_copilot: no reply tool call"
                    + (" (answered in text)" if answer.text.strip() else "")
                )
            return self._awaiting_reply(answer, ops, input, next_round)
        data = {**(reply or {}), "ops": ops}
        output = super().parse(json.dumps(data, ensure_ascii=False), input)
        if output.repair is None:
            return output
        return _with_exchange(output, output.repair, answer, input)

    def _awaiting_reply(
        self,
        answer: ToolAnswer,
        ops: list[dict[str, Any]],
        input: EditCopilotInput,  # noqa: A002
        repair_round: int,
    ) -> EditCopilotOutput:
        parsed = super().parse(
            json.dumps({"intent": "edit", "confidence": 1.0, "reply": "", "ops": ops}), input
        )
        if parsed.needs_clarification:
            return parsed  # the server's own question (bulk capacity) is already the answer
        first = parsed.repair
        repair = CopilotRepair(
            repair_round=repair_round,
            base={},
            awaiting_reply=True,
            kept_ops=first.kept_ops if first is not None else parsed.ops,
            rejected=first.rejected if first is not None else [],
            unrepairable=first.unrepairable if first is not None else parsed.not_applied,
            notes=first.notes
            if first is not None
            else ([parsed.reply_notes] if parsed.reply_notes else []),
        )
        output = EditCopilotOutput(
            intent="unknown",
            ops=[],
            confidence=0.0,
            reply=say(
                en="I couldn't finish that edit. Nothing changed.",
                tr="Bu düzenlemeyi bitiremedim. Hiçbir şey değişmedi.",
            ),
            outcome="failed",
            repair=repair,
            not_applied=parsed.not_applied,
        )
        return _with_exchange(output, repair, answer, input)

    def schema_clarification(self) -> str:
        return (
            "\n\nIMPORTANT: answer only with tool calls, all in one response: one operation "
            "tool call per edit, then `reply` exactly once. No plain-text answer."
        )

    def refusal_clarification(self) -> str:
        return self.schema_clarification()
