"""Editor ops v2: additive, lane-owned copilot operations (KRI-219).

Each lane module (``text``, ``timeline``, ``audio``) exports:

    SPECS: list[OpSpec]          # parser contract for its ops
    register_handlers() -> None  # calls ``kria_editor_ops.register_handler``

and owns a prompt fragment ``prompts/edit_copilot_ops/<lane>.txt``.

Wiring (all additive, no lane touches edit_copilot.py's tables):

* ``edit_copilot`` calls :func:`merge_into_parser` ONCE at the bottom of its
  module. It unions each spec into ``_VALID_OPS`` / ``_OP_REQUIRED`` /
  ``_OP_FIELDS`` and records the family aliases + coerce callables that
  ``_family_allowed`` / ``_coerce_payload`` consult.
* A v2 op is valid ONLY when ``snapshot["editor_ops_version"] == 2`` (set by the
  server-side ``build_editor_snapshot``; the web drawer never sets it).
* Prompt fragments are appended to the copilot prompt only under that marker.

Circular-import rule: lane modules MUST NOT import ``app.agents.edit_copilot``
or ``app.services.kria_editor_ops`` at module top level (both import this
package). Import helpers lazily inside functions.
"""

from __future__ import annotations

import importlib
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

EDITOR_OPS_VERSION = 2
LANES: tuple[str, ...] = ("text", "timeline", "audio")
_FRAGMENT_DIR = Path(__file__).resolve().parents[3] / "prompts" / "edit_copilot_ops"

# coerce(name, payload, snapshot, state) -> cleaned payload | None. Returning
# None drops the op; call ``state.invalid_value()`` / ``state.reject(...)``
# first (same contract as edit_copilot._coerce_payload branches).
CoerceFn = Callable[[str, dict, dict, Any], "dict | None"]


@dataclass(frozen=True)
class OpSpec:
    name: str
    required: frozenset[str] = frozenset()
    fields: frozenset[str] = frozenset()
    # Family aliases matched against snapshot["allowed_op_families"]
    # (see edit_copilot._family_allowed), e.g. {"text", "text_timeline"}.
    family: frozenset[str] = frozenset()
    # Required for a NEW op; must be None when extending an existing op
    # (extend = union `fields`/`required`; edit `_coerce_payload` additively).
    coerce: CoerceFn | None = None
    # Extra fragment file under prompts/edit_copilot_ops/ (beyond <lane>.txt).
    prompt_fragment: str | None = None


def lane_specs() -> list[OpSpec]:
    specs: list[OpSpec] = []
    for lane in LANES:
        module = importlib.import_module(f"{__name__}.{lane}")
        specs.extend(getattr(module, "SPECS", []))
    return specs


def register_all_handlers() -> None:
    """Idempotently let every lane register its compile handlers."""
    global _handlers_registered
    if _handlers_registered:
        return
    _handlers_registered = True
    for lane in LANES:
        importlib.import_module(f"{__name__}.{lane}").register_handlers()


_handlers_registered = False


@dataclass
class MergedRegistry:
    new_ops: frozenset[str] = frozenset()
    families: dict[str, frozenset[str]] = field(default_factory=dict)
    coerce: dict[str, CoerceFn] = field(default_factory=dict)


REGISTRY = MergedRegistry()


def merge_into_parser(
    valid_ops: set,
    op_required: dict[str, frozenset[str]],
    op_fields: dict[str, frozenset[str]],
) -> MergedRegistry:
    """Union lane specs into edit_copilot's parser tables (in place, idempotent)."""
    new_ops: set[str] = set()
    for spec in lane_specs():
        exists = spec.name in op_fields and spec.name not in REGISTRY.new_ops
        if exists:
            if spec.coerce is not None:
                raise ValueError(f"{spec.name}: coerce is only valid for new v2 ops")
            op_required[spec.name] = op_required[spec.name] | spec.required
            op_fields[spec.name] = op_fields[spec.name] | spec.fields | spec.required
            continue
        if spec.coerce is None:
            raise ValueError(f"{spec.name}: new v2 ops need a coerce callable")
        if spec.name in new_ops:
            raise ValueError(f"duplicate v2 op spec: {spec.name}")
        new_ops.add(spec.name)
        valid_ops.add(spec.name)
        op_required[spec.name] = spec.required
        op_fields[spec.name] = spec.fields | spec.required
        REGISTRY.families[spec.name] = spec.family
        REGISTRY.coerce[spec.name] = spec.coerce
    REGISTRY.new_ops = frozenset(new_ops) | REGISTRY.new_ops
    return REGISTRY


def is_v2_snapshot(snapshot: object) -> bool:
    return isinstance(snapshot, dict) and snapshot.get("editor_ops_version") == EDITOR_OPS_VERSION


def prompt_fragments() -> str:
    """Concatenated non-empty lane prompt fragments ('' when all are empty)."""
    names = [f"{lane}.txt" for lane in LANES]
    for spec in lane_specs():
        if spec.prompt_fragment and spec.prompt_fragment not in names:
            names.append(spec.prompt_fragment)
    parts: list[str] = []
    for name in names:
        try:
            text = (_FRAGMENT_DIR / name).read_text(encoding="utf-8").strip()
        except OSError:
            continue
        if text:
            parts.append(text)
    return "\n\n".join(parts)


__all__ = [
    "EDITOR_OPS_VERSION",
    "LANES",
    "REGISTRY",
    "CoerceFn",
    "MergedRegistry",
    "OpSpec",
    "is_v2_snapshot",
    "lane_specs",
    "merge_into_parser",
    "prompt_fragments",
    "register_all_handlers",
]
