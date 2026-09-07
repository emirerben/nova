"""Single server-owned registry for Kria runtime-v2 tools."""

from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass
from typing import Any

from pydantic import BaseModel, ConfigDict, Field

from app.agents._schemas.creator_agent import CreativeStrategy
from app.kria.contracts import KriaToolDefinition


class EmptyArguments(BaseModel):
    model_config = ConfigDict(extra="forbid")


class InspectProjectResult(BaseModel):
    model_config = ConfigDict(extra="forbid")

    editorial_decision: str
    available_media: list[str]
    edit_format: str
    next_action: str


class ApplyStrategyArguments(BaseModel):
    model_config = ConfigDict(extra="forbid")

    strategy: CreativeStrategy
    summary: str = Field(min_length=1, max_length=1000)


class ApplyStrategyResult(BaseModel):
    model_config = ConfigDict(extra="forbid")

    draft_id: str
    draft_revision: int = Field(ge=0)
    snapshot_hash: str
    changes: list[str]


class ApplyEditorOpsArguments(BaseModel):
    """Portable EditCopilot operations; target and generation pins are server-owned."""

    model_config = ConfigDict(extra="forbid")

    operations: list[dict[str, Any]] = Field(min_length=1, max_length=8)
    summary: str = Field(min_length=1, max_length=1000)


class RequestRenderArguments(BaseModel):
    """The model may request consent but never authors target pins."""

    model_config = ConfigDict(extra="forbid")


class RequestRenderResult(BaseModel):
    model_config = ConfigDict(extra="forbid")

    approval_id: str
    consequence: str


ToolHandler = Callable[[BaseModel, Mapping[str, Any]], BaseModel]


@dataclass(frozen=True)
class RegisteredKriaTool:
    definition: KriaToolDefinition
    arguments_model: type[BaseModel]
    result_model: type[BaseModel]
    handler: ToolHandler


class KriaToolRegistry:
    """Typed lookup and manifest generation for deterministic executors."""

    def __init__(self) -> None:
        self._tools: dict[tuple[str, int], RegisteredKriaTool] = {}

    def register(
        self,
        *,
        name: str,
        version: int,
        description: str,
        arguments_model: type[BaseModel],
        result_model: type[BaseModel],
        capability: str,
        risk: str,
        execution_mode: str,
        retry: str,
        failure_class: str,
        handler: ToolHandler,
    ) -> None:
        key = (name, version)
        if key in self._tools:
            raise ValueError(f"duplicate Kria tool registration: {name}@{version}")
        definition = KriaToolDefinition(
            name=name,
            version=version,
            description=description,
            argument_schema=arguments_model.model_json_schema(),
            result_schema=result_model.model_json_schema(),
            capability=capability,
            risk=risk,
            execution_mode=execution_mode,
            retry=retry,
            failure_class=failure_class,
        )
        self._tools[key] = RegisteredKriaTool(
            definition=definition,
            arguments_model=arguments_model,
            result_model=result_model,
            handler=handler,
        )

    def get(self, name: str, version: int) -> RegisteredKriaTool:
        try:
            return self._tools[(name, version)]
        except KeyError as exc:
            raise KeyError(f"unknown Kria tool: {name}@{version}") from exc

    def manifest(self) -> list[dict[str, Any]]:
        return [tool.definition.model_dump(mode="json") for _, tool in sorted(self._tools.items())]


def _inspect_project(_args: BaseModel, snapshot: Mapping[str, Any]) -> BaseModel:
    media = [str(value) for value in snapshot.get("media_labels", [])]
    edit_format = str(snapshot.get("edit_format") or "montage")
    if not media:
        decision = "This project needs footage before I can make an editorial decision."
        next_action = "attach_media"
    else:
        strongest = str(snapshot.get("strongest_moment") or media[0])
        decision = str(
            snapshot.get("editorial_decision")
            or f"Open with {strongest}; it gives the story an immediate visual point of view."
        )
        next_action = "prepare_draft"
    return InspectProjectResult(
        editorial_decision=decision,
        available_media=media,
        edit_format=edit_format,
        next_action=next_action,
    )


def _request_render_requires_policy(_args: BaseModel, _snapshot: Mapping[str, Any]) -> BaseModel:
    raise RuntimeError("render.request must be consumed by approval policy")


def _apply_strategy_requires_runtime(_args: BaseModel, _snapshot: Mapping[str, Any]) -> BaseModel:
    raise RuntimeError("draft.apply_strategy must be consumed by the durable runtime")


def _apply_editor_ops_requires_runtime(_args: BaseModel, _snapshot: Mapping[str, Any]) -> BaseModel:
    raise RuntimeError("draft.apply_editor_ops must be consumed by the durable runtime")


KRIA_TOOLS = KriaToolRegistry()
KRIA_TOOLS.register(
    name="project.inspect",
    version=1,
    description="Inspect the trusted project snapshot and return an editorial next step.",
    arguments_model=EmptyArguments,
    result_model=InspectProjectResult,
    capability="project_inspection",
    risk="read",
    execution_mode="sync",
    retry="idempotent",
    failure_class="project_inspection_failed",
    handler=_inspect_project,
)
KRIA_TOOLS.register(
    name="draft.apply_strategy",
    version=1,
    description="Apply a reversible editorial strategy to the authoritative server draft.",
    arguments_model=ApplyStrategyArguments,
    result_model=ApplyStrategyResult,
    capability="editorial_strategy",
    risk="reversible_draft",
    execution_mode="sync",
    retry="idempotent",
    failure_class="draft_apply_failed",
    handler=_apply_strategy_requires_runtime,
)
KRIA_TOOLS.register(
    name="draft.apply_editor_ops",
    version=1,
    description="Apply a validated reversible operation bundle to the authoritative editor draft.",
    arguments_model=ApplyEditorOpsArguments,
    result_model=ApplyStrategyResult,
    capability="editor_operations",
    risk="reversible_draft",
    execution_mode="sync",
    retry="idempotent",
    failure_class="editor_draft_apply_failed",
    handler=_apply_editor_ops_requires_runtime,
)
KRIA_TOOLS.register(
    name="render.request",
    version=1,
    description="Prepare exact approval for one initial render or rerender.",
    arguments_model=RequestRenderArguments,
    result_model=RequestRenderResult,
    capability="render_request",
    risk="approval_required",
    execution_mode="async",
    retry="never",
    failure_class="render_approval_failed",
    handler=_request_render_requires_policy,
)
