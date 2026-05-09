"""Agent layer — uniform interface for LLM-backed creative-decision agents.

Public surface:
  Agent          — base class, subclass with Input/Output Pydantic schemas
  AgentSpec      — metadata + retry/cost config
  RunContext     — per-call binding (job_id, segment_idx, ...)
  ModelClient    — abstract; concrete impls live in `_model_client`
  AgentError     — base error class
  TransientError — 5xx / 429 / timeout, retry-able
  RefusalError   — safety refusal or missing required fields
  SchemaError    — output failed Pydantic / JSON validation
  TerminalError  — exhausted retries / fallbacks
  run_with_shadow — run primary + shadow side-by-side, log divergence

Concrete agents live in sibling modules: clip_metadata.py, template_recipe.py, ...
The registry (mapping agent name → class) is in `_registry.py`.
"""

from app.agents._registry import AGENTS, get_agent
from app.agents._runtime import (
    Agent,
    AgentError,
    AgentSpec,
    ModelClient,
    ModelInvocation,
    RefusalError,
    RunContext,
    SchemaError,
    TerminalError,
    TransientError,
    run_with_shadow,
)

__all__ = [
    "AGENTS",
    "Agent",
    "AgentError",
    "AgentSpec",
    "ModelClient",
    "ModelInvocation",
    "RefusalError",
    "RunContext",
    "SchemaError",
    "TerminalError",
    "TransientError",
    "get_agent",
    "run_with_shadow",
]
