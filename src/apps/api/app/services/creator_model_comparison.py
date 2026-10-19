"""Reproducible, inert model comparison. Production routing is unchanged.

Offline preparation imports no provider SDK and performs no network or writes.
Live execution is an explicitly approved development-ledger operation in the CLI.
"""

from __future__ import annotations

import copy
import json
from dataclasses import asdict, dataclass, replace
from datetime import UTC, datetime
from pathlib import Path

from app.services.request_following_cases import digest

MODELS = ("gemini-3.1-pro-preview", "claude-sonnet-5-5", "gpt-6.1-sol")
AGENTS = ("main_creator", "edit_copilot")


@dataclass(frozen=True)
class Arm:
    id: str
    model: str
    effort: str
    output_mode: str = "json"
    legacy_retries: bool = False


def agent_class(name):
    if name == "main_creator":
        from app.agents.main_creator import MainCreatorAgent

        return MainCreatorAgent
    if name == "edit_copilot":
        from app.agents.edit_copilot import EditCopilotAgent

        return EditCopilotAgent
    raise ValueError("unsupported agent")


def schema_audit(schema: dict, model: str) -> dict:
    """Conservative feasibility check, never a claim of provider acceptance.

    Do not run SDK schema transformers: they may remove product constraints.
    Open dictionaries cannot constrain edit operations. OpenAI requires every
    property required; Anthropic caps optional/union parameters. Surface exact
    blockers instead of dropping fields or silently switching output modes.
    """
    blockers, unions, optional = [], 0, 0

    def walk(node, path="$"):
        nonlocal unions, optional
        if isinstance(node, list):
            for i, value in enumerate(node):
                walk(value, f"{path}/{i}")
        elif isinstance(node, dict):
            if node.get("type") == "object" or "properties" in node:
                props = node.get("properties", {})
                if node.get("additionalProperties") is not False:
                    blockers.append(f"{path}:open_object")
                missing = set(props) - set(node.get("required", []))
                optional += len(missing)
                if model == "gpt-6.1-sol" and missing:
                    blockers.append(f"{path}:optional_properties:{len(missing)}")
            if "anyOf" in node or "oneOf" in node:
                unions += 1
            if model == "gpt-6.1-sol" and "oneOf" in node:
                blockers.append(f"{path}:oneOf_requires_explicit_projection")
            for key, value in node.items():
                walk(value, f"{path}/{key}")

    walk(schema)
    if model == "claude-sonnet-5-5":
        definitions = schema.get("$defs", {})

        def references(node):
            if isinstance(node, dict):
                if "$ref" in node:
                    yield node["$ref"].removeprefix("#/$defs/")
                for child in node.values():
                    yield from references(child)
            elif isinstance(node, list):
                for child in node:
                    yield from references(child)

        visited = set()

        def acyclic(name, stack):
            if name in stack:
                return False
            if name in visited:
                return True
            if not all(
                acyclic(child, stack | {name}) for child in references(definitions.get(name))
            ):
                return False
            visited.add(name)
            return True

        if not all(acyclic(name, set()) for name in definitions):
            blockers.append("circular_reference_unsupported")
        if optional > 24:
            blockers.append(f"optional_parameters:{optional}>24")
        if unions > 16:
            blockers.append(f"union_parameters:{unions}>16")
    return {
        "compatible": not blockers,
        "blockers": blockers,
        "unions": unions,
        "optional_parameters": optional,
        "schema_sha256": digest(schema),
        "provider_acceptance": "unverified_offline",
    }


def build_agent(name, arm: Arm, client):
    if arm.model not in MODELS or arm.output_mode not in ("json", "native"):
        raise ValueError("unsupported model or output mode")
    if arm.output_mode == "native" and arm.model == "claude-sonnet-5-5":
        raise ValueError(
            "Sonnet rejected the full Main Creator/Copilot compiled grammar; "
            "select an explicit JSON profile until a capability-scoped schema is verified"
        )
    efforts = (
        ("low", "medium", "high")
        if arm.model.startswith("gemini")
        else ("low", "medium", "high", "xhigh", "max")
    )
    if arm.effort not in efforts:
        raise ValueError("unsupported effort")
    agent = agent_class(name)(client)
    schema = None
    if arm.output_mode == "native":
        from app.agents._creator_wire import (
            ANTHROPIC_PROJECTION_VERSION,
            INSTRUCTION,
            VERSION,
            WireContract,
            copilot_schema,
            main_creator_schema,
        )
        from app.agents._runtime import SchemaError

        contract = WireContract(
            main_creator_schema() if name == "main_creator" else copilot_schema()
        )
        schema = contract.provider_schema(arm.model)
        audit = schema_audit(schema, arm.model)
        if not audit["compatible"]:
            raise ValueError("native schema incompatible: " + ",".join(audit["blockers"]))
        base = type(agent)

        class NativeAgent(base):
            wire_contract = contract

            def render_prompt(self, input):
                return super().render_prompt(input) + INSTRUCTION

            def parse(self, raw_text, input):
                try:
                    decoded = contract.decode(json.loads(raw_text))
                except (ValueError, TypeError, KeyError) as exc:
                    raise SchemaError("invalid typed wire response") from exc
                return super().parse(json.dumps(decoded, ensure_ascii=False), input)

            def schema_clarification(self):
                return INSTRUCTION

            def refusal_clarification(self):
                return INSTRUCTION

        agent = NativeAgent(client)
        agent.spec = replace(agent.spec, prompt_version=agent.spec.prompt_version + "+" + VERSION)
        if arm.model.startswith("claude"):
            agent.spec = replace(
                agent.spec,
                prompt_version=agent.spec.prompt_version + "+" + ANTHROPIC_PROJECTION_VERSION,
            )
    if arm.legacy_retries and arm.model != "gemini-3.1-pro-preview":
        raise ValueError("legacy retries belong only to the current Gemini arm")
    if arm.legacy_retries and arm.model != agent.spec.model:
        raise ValueError("legacy arm differs from configured baseline")
    agent.spec = replace(
        agent.spec,
        model=arm.model,
        thinking_level=arm.effort,
        thinking_budget=None,
        sensitive_io=True,
        response_schema=copy.deepcopy(schema),
        fallback_models=agent.spec.fallback_models if arm.legacy_retries else (),
        max_attempts=agent.spec.max_attempts if arm.legacy_retries else 1,
    )
    return agent


def configure_creator_agent(agent, name):
    """Server-only opt-in. JSON defaults return the existing instance unchanged."""
    from app.config import settings

    mode = getattr(settings, name + "_output_mode")
    if mode == "json":
        return agent
    return build_agent(
        name,
        Arm(
            "configured",
            agent.spec.model,
            agent.spec.thinking_level or "medium",
            output_mode="native",
        ),
        agent.client,
    )


def runtime_digest():
    root = Path(__file__).resolve().parents[1]
    files = [
        "agents/_runtime.py",
        "agents/_model_client.py",
        "agents/_text_providers.py",
        "agents/_creator_wire.py",
        "agents/main_creator.py",
        "agents/edit_copilot.py",
        "services/ai_cost_control.py",
        "services/creator_model_comparison.py",
    ]
    return digest({name: (root / name).read_text() for name in files})


def prepare_case(case, arm, *, allow_holdout=False):
    if case.get("split") == "holdout" and not allow_holdout:
        raise ValueError("holdout is sealed from development")
    if case.get("source_kind") not in (
        "exact_invocation",
        "reconstructed_real",
        "recorded_replay",
        "synthetic_control",
    ):
        raise ValueError("source provenance required")
    agent = build_agent(case["agent"], arm, None)
    validated = agent.Input.model_validate(case["input"])
    prompt = agent.render_prompt(validated)
    from app.services.ai_cost_control import PRICE_VERSION, estimate_call_cost_usd

    models = (agent.spec.model, *agent.spec.fallback_models)
    estimate = sum(
        estimate_call_cost_usd(
            model=model,
            prompt=prompt
            + (json.dumps(agent.spec.response_schema) if agent.spec.response_schema else ""),
            max_output_tokens=agent.max_output_tokens,
            media_mime=None,
        )
        * agent.spec.max_attempts
        for model in models
    )
    return {
        "case": case["id"],
        "agent": case["agent"],
        "arm": arm.id,
        "source_kind": case["source_kind"],
        "split": case.get("split"),
        "input_sha256": digest(validated.model_dump(mode="json")),
        "prompt_sha256": digest(prompt),
        "prompt_version": agent.spec.prompt_version,
        "spec_sha256": digest(asdict(agent.spec)),
        "runtime_sha256": runtime_digest(),
        "price_version": PRICE_VERSION,
        "output_mode": (
            "prompt_json"
            if arm.model.startswith("claude")
            else "json_mime"
            if arm.model.startswith("gemini")
            else "json_object"
        )
        if arm.output_mode == "json"
        else "native",
        "max_attempts": agent.spec.max_attempts,
        "fallback_models": list(agent.spec.fallback_models),
        "estimated_max_usd": round(estimate, 8),
    }


def validate_packet(packet):
    if packet.get("schema_version") != 1:
        raise ValueError("unsupported packet version")
    if digest({k: v for k, v in packet.items() if k != "sha256"}) != packet.get("sha256"):
        raise ValueError("packet hash mismatch")
    if not 1 <= packet.get("repetitions", 0) <= 3:
        raise ValueError("repetitions must be 1..3")
    ids = [c["id"] for c in packet["cases"]]
    arms = [a["id"] for a in packet["arms"]]
    if len(ids) != len(set(ids)) or len(arms) != len(set(arms)):
        raise ValueError("duplicate case or arm identity")
    for case in packet["cases"]:
        if case.get("split") not in ("development", "holdout", "control"):
            raise ValueError("split required")
        if case.get("source_kind") in ("exact_invocation", "reconstructed_real"):
            expires = datetime.fromisoformat(case["expires_at"])
            if expires <= datetime.now(UTC):
                raise ValueError("case expired")
            if digest(case["input"]) != case["input_sha256"]:
                raise ValueError("input hash mismatch")


def prepare_packet(packet, *, holdout=False):
    validate_packet(packet)
    arms = [Arm(**a) for a in packet["arms"]]
    rows = [
        prepare_case(case, arm, allow_holdout=holdout) for case in packet["cases"] for arm in arms
    ]
    if "bindings" in packet and packet["bindings"] != rows:
        raise ValueError("frozen prompt/spec/price drift; freeze a new packet")
    return {
        "mode": "offline_preflight",
        "packet_sha256": packet["sha256"],
        "calls": len(rows) * packet["repetitions"],
        "max_provider_submissions": sum(
            r["max_attempts"] * (1 + len(r["fallback_models"])) for r in rows
        )
        * packet["repetitions"],
        "rows": rows,
        "estimated_max_usd": round(
            sum(r["estimated_max_usd"] for r in rows) * packet["repetitions"], 6
        ),
        "quality_verdict": "unassessed",
        "actual_export": "unassessed",
        "live_eligibility": "not_checked_offline",
    }
