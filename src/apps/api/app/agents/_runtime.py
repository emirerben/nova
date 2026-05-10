"""Agent runtime: uniform interface for LLM-backed creative-decision agents.

Responsibilities (handled once here, never per-agent):
  - input/output Pydantic validation
  - retry with backoff on TransientError (configurable schedule)
  - 1 retry on RefusalError with clarification suffix
  - 1 retry on SchemaError with stricter prompt suffix
  - fallback model chain on exhausted primary
  - one canonical `agent_run` structlog event per call

Each Agent subclass defines:
  spec: ClassVar[AgentSpec]            — metadata + retry/cost config
  Input: ClassVar[type[BaseModel]]     — input schema
  Output: ClassVar[type[BaseModel]]    — output schema
  render_prompt(input) -> str          — prompt rendering
  parse(raw_text, input) -> Output     — model response → validated output

Optional overrides:
  media_uri(input) -> str | None       — Gemini File API ref for video/audio agents
  media_mime(input) -> str             — defaults to "video/mp4"
  required_fields() -> list[str]       — JSON keys that must be non-empty
  refusal_clarification() -> str       — suffix appended on refusal retry
  schema_clarification() -> str        — suffix appended on schema-error retry
  compute(input) -> Output             — bypass LLM (for spec.model == "rule_based")
  response_json: bool = True           — set False for freeform-text agents
  max_output_tokens: int | None = None — cap on response size
"""

from __future__ import annotations

import json
import time
from abc import ABC, abstractmethod
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any, ClassVar, Generic, TypeVar

import structlog
from pydantic import BaseModel, ValidationError

log = structlog.get_logger()


# ── Errors ────────────────────────────────────────────────────────────────────


class AgentError(Exception):
    """Base for all agent-runtime errors."""


class TransientError(AgentError):
    """5xx, 429, or timeout. Retry-able."""


class RefusalError(AgentError):
    """Safety refusal, invalid JSON, or missing required fields."""


class SchemaError(AgentError):
    """Output failed Pydantic / value validation after parse."""


class TerminalError(AgentError):
    """Exhausted retries and fallbacks. Caller decides graceful degradation."""


# ── Spec + context ────────────────────────────────────────────────────────────


@dataclass(frozen=True, slots=True)
class AgentSpec:
    """Per-agent metadata. Immutable; bump `prompt_version` when prompt changes."""

    name: str
    prompt_id: str
    prompt_version: str
    model: str
    fallback_models: tuple[str, ...] = ()
    max_attempts: int = 5
    backoff_s: tuple[float, ...] = (3.0, 9.0, 27.0, 60.0)
    timeout_s: float = 30.0
    cost_per_1k_input_usd: float = 0.0
    cost_per_1k_output_usd: float = 0.0
    # When True (default): on SchemaError/RefusalError, retry once with a
    # clarification suffix. When False: raise TerminalError immediately so
    # the caller can fall through to a cheaper backup path. Used for agents
    # like ClipMetadataAgent where a Whisper fallback is faster than burning
    # a second 100-second Gemini call that's likely to fail the same way.
    enable_clarification_retries: bool = True


@dataclass(slots=True)
class RunContext:
    """Per-call binding. Threaded into structlog events for cross-agent correlation."""

    job_id: str | None = None
    request_id: str | None = None
    segment_idx: int | None = None
    extra: dict[str, Any] = field(default_factory=dict)


@dataclass(slots=True)
class ModelInvocation:
    """Result of a single model call."""

    raw_text: str
    tokens_in: int = 0
    tokens_out: int = 0
    raw_response: Any = None  # SDK-specific; used for finish_reason inspection


@dataclass(slots=True)
class _RunStats:
    """Accumulated per-call stats threaded through the retry loop."""

    attempts: int = 0
    refusal_retries: int = 0
    schema_retries: int = 0
    tokens_in: int = 0
    tokens_out: int = 0


# ── Model client interface ────────────────────────────────────────────────────


class ModelClient:
    """Abstract model client. Concrete implementations in `_model_client.py`.

    Implementations MUST raise `TransientError` for 5xx/429/timeout, and
    `TerminalError` for permanent client errors (4xx other than 429).
    """

    def invoke(
        self,
        *,
        model: str,
        prompt: str,
        media_uri: str | None = None,
        media_mime: str | None = None,
        response_json: bool = True,
        max_output_tokens: int | None = None,
        timeout_s: float = 30.0,
    ) -> ModelInvocation:
        raise NotImplementedError


# ── Agent base class ──────────────────────────────────────────────────────────


InputT = TypeVar("InputT", bound=BaseModel)
OutputT = TypeVar("OutputT", bound=BaseModel)


class Agent(ABC, Generic[InputT, OutputT]):
    """Base class for all agents. See module docstring for required overrides."""

    spec: ClassVar[AgentSpec]
    Input: ClassVar[type[BaseModel]]
    Output: ClassVar[type[BaseModel]]

    response_json: ClassVar[bool] = True
    max_output_tokens: ClassVar[int | None] = None

    def __init__(self, model_client: ModelClient) -> None:
        self.client = model_client

    # ── Required ──────────────────────────────────────────────────

    @abstractmethod
    def render_prompt(self, input: InputT) -> str:  # noqa: A002
        """Build the prompt string for this input."""

    @abstractmethod
    def parse(self, raw_text: str, input: InputT) -> OutputT:  # noqa: A002
        """Parse + validate model output. Raise SchemaError / ValidationError on failure."""

    # ── Optional overrides ────────────────────────────────────────

    def media_uri(self, input: InputT) -> str | None:  # noqa: A002, ARG002
        """Return a Gemini File API URI for video/audio agents, or None."""
        return None

    def media_mime(self, input: InputT) -> str:  # noqa: A002, ARG002
        return "video/mp4"

    def required_fields(self) -> list[str]:
        """Top-level JSON keys that must be present + non-empty for refusal detection."""
        return []

    def refusal_clarification(self) -> str:
        return (
            "\n\nIMPORTANT: respond with the requested data only. "
            "Do not refuse, decline, or add disclaimers."
        )

    def schema_clarification(self) -> str:
        return (
            "\n\nIMPORTANT: return ONLY valid JSON matching the schema above. "
            "No markdown fences, no prose, no comments."
        )

    def compute(self, input: InputT) -> OutputT:  # noqa: A002, ARG002
        """Override for `spec.model == 'rule_based'` agents. LLM path skipped entirely."""
        raise NotImplementedError(
            f"{type(self).__name__}.compute() must be overridden when spec.model='rule_based'"
        )

    # ── Public entry point ────────────────────────────────────────

    def run(self, input: InputT | dict, *, ctx: RunContext | None = None) -> OutputT:  # noqa: A002
        ctx = ctx or RunContext()
        validated_input = self._validate_input(input)
        start = time.monotonic()

        # Rule-based bypass: no model client, no retries, no fallbacks
        if self.spec.model == "rule_based":
            try:
                output = self.compute(validated_input)
            except Exception as exc:
                self._log_outcome(
                    outcome="terminal_rule_based",
                    model="rule_based",
                    stats=_RunStats(),
                    fallback_used=False,
                    latency_ms=int((time.monotonic() - start) * 1000),
                    ctx=ctx,
                    error=str(exc),
                )
                raise TerminalError(f"{self.spec.name}: rule_based compute failed — {exc}") from exc
            self._log_outcome(
                outcome="ok",
                model="rule_based",
                stats=_RunStats(),
                fallback_used=False,
                latency_ms=int((time.monotonic() - start) * 1000),
                ctx=ctx,
            )
            return output

        # LLM path with fallback chain
        models_to_try = (self.spec.model, *self.spec.fallback_models)
        stats = _RunStats()
        chosen_model = self.spec.model
        last_exc: BaseException | None = None

        for model_idx, model in enumerate(models_to_try):
            chosen_model = model
            fallback_used = model_idx > 0
            try:
                output = self._run_on_model(model, validated_input, ctx, stats)
                self._log_outcome(
                    outcome="ok_fallback" if fallback_used else "ok",
                    model=model,
                    stats=stats,
                    fallback_used=fallback_used,
                    latency_ms=int((time.monotonic() - start) * 1000),
                    ctx=ctx,
                )
                return output
            except RefusalError as exc:
                # Refusing model probably won't yield to a different one — terminate.
                self._log_outcome(
                    outcome="terminal_refusal",
                    model=model,
                    stats=stats,
                    fallback_used=fallback_used,
                    latency_ms=int((time.monotonic() - start) * 1000),
                    ctx=ctx,
                    error=str(exc),
                )
                raise TerminalError(f"{self.spec.name}: refusal — {exc}") from exc
            except SchemaError as exc:
                # Schema retries already exhausted — same model can't fix it; fallback won't either.
                self._log_outcome(
                    outcome="terminal_schema",
                    model=model,
                    stats=stats,
                    fallback_used=fallback_used,
                    latency_ms=int((time.monotonic() - start) * 1000),
                    ctx=ctx,
                    error=str(exc),
                )
                raise TerminalError(f"{self.spec.name}: schema — {exc}") from exc
            except TransientError as exc:
                last_exc = exc
                # Try the next model in the fallback chain.
                continue

        # All models exhausted on TransientError.
        self._log_outcome(
            outcome="terminal_transient",
            model=chosen_model,
            stats=stats,
            fallback_used=len(models_to_try) > 1,
            latency_ms=int((time.monotonic() - start) * 1000),
            ctx=ctx,
            error=str(last_exc) if last_exc else "exhausted",
        )
        raise TerminalError(
            f"{self.spec.name}: exhausted {len(models_to_try)} model(s) "
            f"after {stats.attempts} attempt(s)"
        ) from last_exc

    # ── Per-model retry loop ──────────────────────────────────────

    def _run_on_model(
        self,
        model: str,
        input: InputT,  # noqa: A002
        ctx: RunContext,
        stats: _RunStats,
    ) -> OutputT:
        prompt = self.render_prompt(input)
        media = self.media_uri(input)
        mime = self.media_mime(input) if media else None

        last_transient: BaseException | None = None

        for attempt in range(self.spec.max_attempts):
            stats.attempts += 1
            try:
                inv = self.client.invoke(
                    model=model,
                    prompt=prompt,
                    media_uri=media,
                    media_mime=mime,
                    response_json=self.response_json,
                    max_output_tokens=self.max_output_tokens,
                    timeout_s=self.spec.timeout_s,
                )
            except TransientError as exc:
                last_transient = exc
                if attempt >= self.spec.max_attempts - 1:
                    raise
                backoff = self.spec.backoff_s[min(attempt, len(self.spec.backoff_s) - 1)]
                log.warning(
                    "agent_transient_retry",
                    agent=self.spec.name,
                    model=model,
                    attempt=attempt + 1,
                    of=self.spec.max_attempts,
                    backoff_s=backoff,
                    error=str(exc),
                    job_id=ctx.job_id,
                )
                time.sleep(backoff)
                continue
            except TerminalError:
                raise
            except Exception as exc:  # SDK leaks something we didn't classify
                raise TerminalError(f"{self.spec.name}: unclassified — {exc}") from exc

            stats.tokens_in += inv.tokens_in
            stats.tokens_out += inv.tokens_out

            # Refusal check (safety + required fields)
            try:
                self._check_refusal(inv)
            except RefusalError:
                # Skip clarification retry for agents that have a faster
                # backup path (e.g. ClipMetadataAgent → Whisper fallback).
                # A second 100-second Gemini call rarely succeeds when the
                # first one refused.
                if stats.refusal_retries >= 1 or not self.spec.enable_clarification_retries:
                    raise
                stats.refusal_retries += 1
                prompt = self.render_prompt(input) + self.refusal_clarification()
                continue

            # Parse + validate
            try:
                return self.parse(inv.raw_text, input)
            except (SchemaError, ValidationError, ValueError, KeyError, TypeError) as exc:
                # Same logic as refusal: skip the schema-clarification retry
                # for agents whose caller can fall through cheaper than a
                # second Gemini call.
                if stats.schema_retries >= 1 or not self.spec.enable_clarification_retries:
                    if isinstance(exc, SchemaError):
                        raise
                    raise SchemaError(f"{self.spec.name}: parse failed — {exc}") from exc
                stats.schema_retries += 1
                prompt = self.render_prompt(input) + self.schema_clarification()
                continue

        # Exhausted attempts on transient errors
        if last_transient is not None:
            raise TransientError(str(last_transient)) from last_transient
        raise TerminalError(f"{self.spec.name}: max_attempts exhausted on {model}")

    # ── Helpers ───────────────────────────────────────────────────

    def _validate_input(self, input: InputT | dict) -> InputT:  # noqa: A002
        if isinstance(input, self.Input):
            return input  # type: ignore[return-value]
        return self.Input.model_validate(input)  # type: ignore[return-value]

    def _check_refusal(self, inv: ModelInvocation) -> None:
        # Provider-specific finish_reason check (Gemini-flavored, but tolerant)
        raw = inv.raw_response
        if raw is not None:
            candidates = getattr(raw, "candidates", None)
            if candidates:
                fin = getattr(candidates[0], "finish_reason", None)
                if fin is not None:
                    fin_name = getattr(fin, "name", None) or str(fin)
                    if fin_name == "SAFETY":
                        raise RefusalError("Content policy refusal")

        required = self.required_fields()
        if not required:
            return
        try:
            data = json.loads(inv.raw_text)
        except (ValueError, TypeError) as exc:
            raise RefusalError(f"Invalid JSON: {exc}") from exc
        if not isinstance(data, dict):
            raise RefusalError("Response is not a JSON object")
        for field_name in required:
            val = data.get(field_name)
            if val is None or val == "" or val == []:
                raise RefusalError(f"Missing required field: {field_name}")

    def _log_outcome(
        self,
        *,
        outcome: str,
        model: str,
        stats: _RunStats,
        fallback_used: bool,
        latency_ms: int,
        ctx: RunContext,
        error: str | None = None,
    ) -> None:
        cost_usd = (
            (stats.tokens_in / 1000.0) * self.spec.cost_per_1k_input_usd
            + (stats.tokens_out / 1000.0) * self.spec.cost_per_1k_output_usd
        )
        payload = {
            "agent": self.spec.name,
            "prompt_version": self.spec.prompt_version,
            "model": model,
            "outcome": outcome,
            "attempts": stats.attempts,
            "fallback_used": fallback_used,
            "refusal_count": stats.refusal_retries,
            "schema_retry_count": stats.schema_retries,
            "tokens_in": stats.tokens_in,
            "tokens_out": stats.tokens_out,
            "cost_usd": round(cost_usd, 6),
            "latency_ms": latency_ms,
            "job_id": ctx.job_id,
            "segment_idx": ctx.segment_idx,
            "request_id": ctx.request_id,
        }
        if error is not None:
            payload["error"] = error
        log.info("agent_run", **payload)


# ── Shadow-mode helper ────────────────────────────────────────────────────────


def run_with_shadow(
    primary: Agent[InputT, OutputT],
    shadow: Callable[[InputT], OutputT] | Agent[InputT, OutputT],
    input: InputT,  # noqa: A002
    *,
    ctx: RunContext | None = None,
    diff: Callable[[OutputT, OutputT], str | None] | None = None,
) -> OutputT:
    """Run primary; run shadow side-by-side; log divergence; return primary output.

    Shadow failure is non-fatal — primary always wins. Used for safe migrations
    (e.g., swap a static dict for an LLM agent without trusting it yet).

    `diff` is an optional comparator: returns None if outputs match, or a short
    string summarizing the divergence. Default does shallow Pydantic equality.
    """
    ctx = ctx or RunContext()
    primary_out = primary.run(input, ctx=ctx)

    shadow_out: OutputT | None = None
    shadow_error: str | None = None
    try:
        if isinstance(shadow, Agent):
            shadow_out = shadow.run(input, ctx=ctx)
        else:
            shadow_out = shadow(input)
    except Exception as exc:  # noqa: BLE001 — shadow failures must never break the call
        shadow_error = str(exc)

    if shadow_out is not None:
        divergence: str | None
        try:
            divergence = (
                diff(primary_out, shadow_out)
                if diff
                else _default_diff(primary_out, shadow_out)
            )
        except Exception as exc:  # noqa: BLE001
            divergence = f"diff_failed: {exc}"
        log.info(
            "agent_shadow",
            primary_agent=primary.spec.name,
            shadow=getattr(shadow, "spec", type(shadow).__name__).__str__()
            if hasattr(shadow, "spec")
            else getattr(shadow, "__name__", "callable"),
            divergence=divergence,
            job_id=ctx.job_id,
            segment_idx=ctx.segment_idx,
        )
    else:
        log.warning(
            "agent_shadow_failed",
            primary_agent=primary.spec.name,
            error=shadow_error,
            job_id=ctx.job_id,
        )

    return primary_out


def _default_diff(a: BaseModel, b: BaseModel) -> str | None:
    """Shallow equality: None if equal, else `field=primary_val|shadow_val`."""
    if a == b:
        return None
    a_dump = a.model_dump() if isinstance(a, BaseModel) else a
    b_dump = b.model_dump() if isinstance(b, BaseModel) else b
    if not isinstance(a_dump, dict) or not isinstance(b_dump, dict):
        return f"primary={a_dump!r} shadow={b_dump!r}"
    diffs: list[str] = []
    for key in set(a_dump) | set(b_dump):
        av = a_dump.get(key, "<missing>")
        bv = b_dump.get(key, "<missing>")
        if av != bv:
            diffs.append(f"{key}={av!r}|{bv!r}")
    return "; ".join(diffs) if diffs else None
