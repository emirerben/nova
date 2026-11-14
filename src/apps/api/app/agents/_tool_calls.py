"""Provider-neutral tool calling (KRI-1393): what an agent offers, what the model called.

A provider client given a ``ToolRequest`` sends ``system`` as its system/instructions block,
the agent's prompt as the user turn, every earlier ``ToolExchange`` as the model's own turn
followed by the results, and ``tools`` as function declarations. It returns the model's calls
as a ``ToolAnswer`` serialized into ``ModelInvocation.raw_text``, so a recorded answer replays
through the agent's parser like any other raw text.

``continuation`` is the provider's own record of its turn (OpenAI reasoning items, Anthropic
thinking blocks, Gemini thought signatures). A later round must send it back unchanged; it is
opaque here, never parsed, and dropped from recordings.
"""

from __future__ import annotations

import json
from typing import Any

from pydantic import BaseModel, ConfigDict, Field


class ToolSpec(BaseModel):
    model_config = ConfigDict(frozen=True)

    name: str
    description: str
    # A JSON schema for the arguments object.
    parameters: dict[str, Any]


class ToolCall(BaseModel):
    model_config = ConfigDict(frozen=True)

    id: str
    name: str
    # None: the provider's arguments were not a JSON object (a malformed call).
    arguments: dict[str, Any] | None


class ToolAnswer(BaseModel):
    model_config = ConfigDict(frozen=True)

    provider: str
    calls: list[ToolCall] = Field(default_factory=list)
    # Visible text the model wrote beside (or instead of) its calls.
    text: str = ""
    continuation: list[dict[str, Any]] = Field(default_factory=list)

    def envelope(self) -> str:
        return json.dumps(
            {
                "tool_calls": [call.model_dump() for call in self.calls],
                "text": self.text,
                "provider": self.provider,
                "continuation": self.continuation,
            },
            ensure_ascii=False,
        )

    def recorded(self) -> ToolAnswer:
        """The answer without the provider's opaque continuation (what fixtures keep)."""
        return self.model_copy(update={"continuation": []})

    @classmethod
    def decode(cls, raw_text: str) -> ToolAnswer:
        data = json.loads(raw_text)
        if not isinstance(data, dict) or not isinstance(data.get("tool_calls"), list):
            raise ValueError("not a tool-call answer")
        return cls(
            provider=str(data.get("provider") or ""),
            calls=[ToolCall.model_validate(call) for call in data["tool_calls"]],
            text=str(data.get("text") or ""),
            continuation=list(data.get("continuation") or []),
        )


def parse_arguments(raw: object) -> dict[str, Any] | None:
    """A provider's arguments as a JSON object, or None when they are not one."""
    if isinstance(raw, dict):
        return raw
    if isinstance(raw, str):
        try:
            value = json.loads(raw)
        except ValueError:
            return None
        return value if isinstance(value, dict) else None
    return None


class ToolResult(BaseModel):
    model_config = ConfigDict(frozen=True)

    call_id: str
    name: str
    content: str
    is_error: bool = False


class ToolExchange(BaseModel):
    """One model answer in this turn and what the server said back about each call."""

    model_config = ConfigDict(frozen=True)

    answer: ToolAnswer
    results: list[ToolResult]


class ToolRequest(BaseModel):
    model_config = ConfigDict(frozen=True)

    system: str
    tools: list[ToolSpec]
    history: list[ToolExchange] = Field(default_factory=list)
    # Routes repeated requests that share system + tools to the same provider cache (OpenAI).
    cache_key: str = ""

    def accounting_text(self) -> str:
        """Everything this request sends besides the prompt, for cost estimates and call keys."""
        return json.dumps(
            {
                "system": self.system,
                "tools": [tool.model_dump() for tool in self.tools],
                "history": [exchange.model_dump() for exchange in self.history],
            },
            ensure_ascii=False,
            sort_keys=True,
        )


def described_nesting(schema: dict[str, Any]) -> dict[str, Any]:
    """``schema`` typed at its top level, with every nested object written as its keys and types.

    The shape the OpenAI and Anthropic copilot tools were measured with (KRI-797): about half
    the tool tokens of full typing, and their tool arguments are not decoded against the schema.
    Gemini constrains arguments to the declared properties (an object declared without them
    comes back empty), so it gets the typed schema.
    """

    def kind(node: Any) -> str:
        if not isinstance(node, dict):
            return "any JSON"
        if "const" in node:
            return json.dumps(node["const"])
        if "anyOf" in node:
            return " or ".join(kind(branch) for branch in node["anyOf"])
        if "enum" in node:
            return "one of " + "|".join(map(str, node["enum"]))
        if node.get("type") == "array":
            return "list of " + kind(node.get("items"))
        if node.get("type") == "object" and "properties" in node:
            return "object with " + ", ".join(node["properties"])
        return str(node.get("type", "any JSON"))

    def visit(node: Any, depth: int) -> Any:
        if isinstance(node, list):
            return [visit(value, depth) for value in node]
        if not isinstance(node, dict):
            return node
        if depth >= 1 and node.get("type") == "object" and "properties" in node:
            required = set(node.get("required", []))
            fields = ", ".join(
                f"{key} ({kind(value)}{', required' if key in required else ''})"
                for key, value in node["properties"].items()
            )
            return {"type": "object", "description": f"Fields: {fields}."}
        return {
            key: {k: visit(v, depth + 1) for k, v in value.items()}
            if key == "properties"
            else visit(value, depth)
            if key in ("items", "anyOf")
            else value
            for key, value in node.items()
        }

    return visit(schema, 0)
