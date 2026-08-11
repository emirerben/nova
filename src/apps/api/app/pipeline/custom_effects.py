"""Sandboxed effect-language validator (PR5 of the Nova AI effect-language train).

Validates agent- or user-authored FFmpeg filter chains against a hard
whitelist before they can ever reach a subprocess call. This module is
**inert by design**: nothing here executes FFmpeg, nothing wires into a
Celery task or a copilot op yet (that's a later PR). It only answers two
questions safely: "is this spec allowed?" and, if so, "what -vf fragment
does it compile to?".

Threat model: the input dict may originate from an LLM tool call (already a
semi-trusted but prompt-injectable surface) that a later PR will let flow,
indirectly, into a `subprocess` FFmpeg invocation. The design goal is that
**no value taken from `raw` ever reaches a filter-graph string unless it
has been type-checked, range/enum-checked, and character-scanned first** —
so this validator, not the filter string it emits, is the security
boundary. `effect_spec_to_filter_chain` therefore only ever reads back the
already-validated, strongly-typed fields on `EffectSpec` — never `raw`.

Modeled structurally on `app/pipeline/camera_effects.py::normalize_camera_effects`,
but reject-on-error instead of clamp-and-continue: a bad effect spec should
surface a specific, testable rejection reason to the caller, not silently
mutate into something else.
"""

from __future__ import annotations

import math
import re
from dataclasses import dataclass
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field
from pydantic import ValidationError as PydanticValidationError

# ---------------------------------------------------------------------------
# Filter whitelist — PENDING EMIR SIGN-OFF (2026-08-11, plan
# "our-product-is-disconnected-rustling-naur.md", PR5). This is the complete
# set of FFmpeg filters the validator will ever accept; nothing outside this
# set can reach the renderer through the effect language. Every entry is a
# pure per-pixel/per-frame video transform with no file, URL, network, or
# subprocess-command parameters. Deliberately excluded (and covered by
# tests): movie, subtitles, drawtext, ass, lut3d, sendcmd, zmq, concat, and
# any other filter accepting a path/URL/external-command string. Adding a
# filter here is a security decision as much as a product one — get sign-off
# before extending, and see FILTER_PARAM_SPECS below (every entry here MUST
# have a matching spec dict, enforced by test_custom_effects.py).
# ---------------------------------------------------------------------------
ALLOWED_FILTERS: frozenset[str] = frozenset(
    {
        "eq",
        "hue",
        "unsharp",
        "gblur",
        "boxblur",
        "curves",
        "colorbalance",
        "colorchannelmixer",
        "vignette",
        "noise",
        "tblend",
        "fade",
        "crop",
        "scale",
        "zoompan",
        "rotate",
        "hflip",
        "vflip",
        "setpts",
        "chromashift",
    }
)

# Filters that do NOT support FFmpeg's per-filter `enable=` timeline gating
# (no "T" in `ffmpeg -filters` flags for that filter, verified against
# ffmpeg 8.x). The serializer omits `enable=` for these; an execution-time
# caller that needs to bound one of these to a sub-window of the clip must
# wrap the whole chain (e.g. trim/concat) instead of relying on `enable=`.
FILTERS_WITHOUT_TIMELINE_ENABLE: frozenset[str] = frozenset({"crop", "scale", "setpts", "zoompan"})

MAX_FILTERS = 6
MAX_LABEL_CHARS = 80
MAX_ID_CHARS = 128
MAX_END_S = 600.0

# Characters that have syntactic meaning inside an FFmpeg filter-graph
# description (option separator, filter separator, pad-link brackets,
# key=value assignment, escape char, path separator). Any string param
# value containing one of these is rejected outright, rather than escaped —
# escaping filter-graph syntax correctly is error-prone, and none of our
# legitimate enum values ever need any of these characters.
_FORBIDDEN_STRING_CHARS = frozenset(":;,'\"[]=\\/")

_ID_PATTERN = re.compile(r"[A-Za-z0-9_-]+")


class EffectValidationError(Exception):
    """Raised by `validate_effect_spec` for any rejected input.

    `reason` is a stable, machine-readable snake_case code callers/tests can
    branch on (e.g. "filter_not_allowed", "param_out_of_range"). `detail` is
    a human-readable elaboration only — it may echo back shapes/ranges but
    never gets embedded into any rendered filter string.
    """

    def __init__(self, reason: str, detail: str = "") -> None:
        self.reason = reason
        self.detail = detail
        super().__init__(f"{reason}: {detail}" if detail else reason)


@dataclass(frozen=True)
class ParamSpec:
    """One filter parameter's validation rule: a numeric range XOR a closed
    set of allowed strings — never both, never neither."""

    kind: Literal["numeric", "enum"]
    min: float | None = None
    max: float | None = None
    enum: frozenset[str] | None = None

    def __post_init__(self) -> None:
        if self.kind == "numeric":
            assert self.min is not None and self.max is not None and self.enum is None
            assert math.isfinite(self.min) and math.isfinite(self.max) and self.min < self.max
        else:
            assert self.enum is not None and self.min is None and self.max is None
            assert all(not (set(v) & _FORBIDDEN_STRING_CHARS) for v in self.enum)


def _num(lo: float, hi: float) -> ParamSpec:
    return ParamSpec(kind="numeric", min=lo, max=hi)


def _enum(*values: str) -> ParamSpec:
    return ParamSpec(kind="enum", enum=frozenset(values))


# Per-filter allowed parameter names, each range/enum-bounded. Ranges are
# deliberately tighter than FFmpeg's own documented extremes where the
# extremes are either meaningless in a 9:16 short-form render or would
# blow the "no complex graph topology" / "no expression strings" invariant
# (crop/scale/rotate/setpts all take plain numbers here, never FFmpeg
# expression syntax like `iw/2` or `PI/6`, precisely so a param value can
# never smuggle filter-graph syntax).
FILTER_PARAM_SPECS: dict[str, dict[str, ParamSpec]] = {
    "eq": {
        "brightness": _num(-1.0, 1.0),
        "contrast": _num(0.0, 3.0),
        "saturation": _num(0.0, 3.0),
        "gamma": _num(0.1, 3.0),
    },
    "hue": {
        "h": _num(-180.0, 180.0),
        "s": _num(-3.0, 3.0),
    },
    "unsharp": {
        "luma_msize_x": _num(3.0, 23.0),
        "luma_msize_y": _num(3.0, 23.0),
        "luma_amount": _num(-2.0, 5.0),
    },
    "gblur": {
        "sigma": _num(0.0, 50.0),
        "steps": _num(1.0, 6.0),
    },
    "boxblur": {
        "luma_radius": _num(0.0, 50.0),
        "luma_power": _num(0.0, 10.0),
    },
    "curves": {
        "preset": _enum(
            "color_negative",
            "cross_process",
            "darker",
            "increase_contrast",
            "lighter",
            "linear_contrast",
            "medium_contrast",
            "negative",
            "strong_contrast",
            "vintage",
        ),
    },
    "colorbalance": {
        "rs": _num(-1.0, 1.0),
        "gs": _num(-1.0, 1.0),
        "bs": _num(-1.0, 1.0),
        "rm": _num(-1.0, 1.0),
        "gm": _num(-1.0, 1.0),
        "bm": _num(-1.0, 1.0),
        "rh": _num(-1.0, 1.0),
        "gh": _num(-1.0, 1.0),
        "bh": _num(-1.0, 1.0),
    },
    "colorchannelmixer": {
        "rr": _num(-2.0, 2.0),
        "rg": _num(-2.0, 2.0),
        "rb": _num(-2.0, 2.0),
        "gr": _num(-2.0, 2.0),
        "gg": _num(-2.0, 2.0),
        "gb": _num(-2.0, 2.0),
        "br": _num(-2.0, 2.0),
        "bg": _num(-2.0, 2.0),
        "bb": _num(-2.0, 2.0),
    },
    "vignette": {
        "angle": _num(0.0, 1.5708),
        "x0": _num(0.0, 1.0),
        "y0": _num(0.0, 1.0),
        "mode": _enum("forward", "backward"),
    },
    "noise": {
        "alls": _num(0.0, 100.0),
        "allf": _enum("a", "p", "t", "u"),
    },
    "tblend": {
        "all_mode": _enum(
            "addition",
            "average",
            "darken",
            "lighten",
            "multiply",
            "overlay",
            "screen",
            "difference",
            "softlight",
            "hardlight",
        ),
    },
    "fade": {
        "type": _enum("in", "out"),
        "duration": _num(0.05, 10.0),
        "alpha": _enum("0", "1"),
    },
    "crop": {
        "w": _num(0.05, 1.0),
        "h": _num(0.05, 1.0),
        "x": _num(0.0, 0.95),
        "y": _num(0.0, 0.95),
    },
    "scale": {
        "width_frac": _num(0.1, 2.0),
        "height_frac": _num(0.1, 2.0),
    },
    "zoompan": {
        "zoom": _num(1.0, 4.0),
        "duration_frames": _num(1.0, 300.0),
    },
    "rotate": {
        "angle_deg": _num(-180.0, 180.0),
    },
    "hflip": {},
    "vflip": {},
    "setpts": {
        # Bounded speed multiplier only (never a raw PTS expression string).
        # Serialized as `setpts=(1/speed)*PTS`.
        "speed": _num(0.25, 4.0),
    },
    "chromashift": {
        "cbh": _num(-50.0, 50.0),
        "cbv": _num(-50.0, 50.0),
        "crh": _num(-50.0, 50.0),
        "crv": _num(-50.0, 50.0),
    },
}


class FilterNode(BaseModel):
    """One linear-chain step. `params` values are validated typed floats or
    validated enum strings ONLY by the time this model is constructed."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    name: str
    params: dict[str, float | str] = Field(default_factory=dict)


class EffectSpec(BaseModel):
    """A validated custom-effect spec. v1 is intentionally a single LINEAR
    filter chain (no branching/merging filter-graph topology) applied to the
    full frame for a bounded time window — simpler to validate, simpler to
    serialize, simpler to reason about safety-wise. Clip/text_bar scoping
    (`target` beyond "full_frame") is deferred to a later PR."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    id: str
    label: str = Field(max_length=MAX_LABEL_CHARS)
    filters: list[FilterNode] = Field(min_length=1, max_length=MAX_FILTERS)
    start_s: float = Field(ge=0.0)
    end_s: float = Field(gt=0.0, le=MAX_END_S)
    target: Literal["full_frame"]


def _coerce_finite_float(
    value: Any, *, context: str, bounds: tuple[float, float] | None = None
) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise EffectValidationError(
            "param_wrong_type", f"{context} must be a number, got {type(value).__name__}"
        )
    parsed = float(value)
    if not math.isfinite(parsed):
        raise EffectValidationError("param_nan_or_inf", f"{context} must be finite (no NaN/inf)")
    if bounds is not None:
        lo, hi = bounds
        if not (lo <= parsed <= hi):
            raise EffectValidationError(
                "param_out_of_range", f"{context}={parsed} out of range [{lo}, {hi}]"
            )
    return parsed


def _validate_param_value(
    filter_name: str, param_name: str, value: Any, spec: ParamSpec
) -> float | str:
    if spec.kind == "numeric":
        return _coerce_finite_float(
            value, context=f"{filter_name}.{param_name}", bounds=(spec.min, spec.max)
        )

    if not isinstance(value, str):
        raise EffectValidationError(
            "param_wrong_type",
            f"{filter_name}.{param_name} must be a string, got {type(value).__name__}",
        )
    # Character-scan BEFORE the enum-membership check: this is the defense
    # against filter-graph syntax injection, and it must fire independently
    # of whatever the enum happens to contain (defense in depth — the enum
    # values themselves are asserted clean at spec-definition time above).
    if any(ch in _FORBIDDEN_STRING_CHARS for ch in value):
        raise EffectValidationError(
            "forbidden_chars_in_string",
            f"{filter_name}.{param_name} value contains disallowed characters",
        )
    if value not in (spec.enum or frozenset()):
        raise EffectValidationError(
            "enum_value_not_allowed",
            f"{filter_name}.{param_name}={value!r} is not an allowed value",
        )
    return value


def _validate_filter_node(raw_node: Any, index: int) -> FilterNode:
    if not isinstance(raw_node, dict):
        raise EffectValidationError("invalid_filter_node", f"filters[{index}] must be an object")

    name = raw_node.get("name")
    if not isinstance(name, str):
        raise EffectValidationError(
            "invalid_filter_node", f"filters[{index}].name must be a string"
        )
    if name not in ALLOWED_FILTERS:
        raise EffectValidationError(
            "filter_not_allowed", f"filter {name!r} is not in ALLOWED_FILTERS"
        )

    param_specs = FILTER_PARAM_SPECS[name]
    raw_params = raw_node.get("params") if raw_node.get("params") is not None else {}
    if not isinstance(raw_params, dict):
        raise EffectValidationError(
            "invalid_filter_node", f"filters[{index}].params must be an object"
        )

    validated_params: dict[str, float | str] = {}
    for param_name, param_value in raw_params.items():
        if not isinstance(param_name, str) or param_name not in param_specs:
            raise EffectValidationError(
                "unknown_param", f"{name}.{param_name!r} is not an allowed parameter"
            )
        validated_params[param_name] = _validate_param_value(
            name, param_name, param_value, param_specs[param_name]
        )

    return FilterNode(name=name, params=validated_params)


def validate_effect_spec(raw: dict[str, Any]) -> EffectSpec:
    """Validate an untrusted raw dict into a strongly-typed `EffectSpec`.

    Raises `EffectValidationError` (never a bare pydantic ValidationError)
    for every rejection class — unknown filters, filters outside
    ALLOWED_FILTERS, unknown params, out-of-range/non-finite numerics,
    string params outside their enum (or containing filter-graph syntax
    characters), oversized chains, and malformed label/id/time-range/target
    fields.
    """
    if not isinstance(raw, dict):
        raise EffectValidationError("invalid_spec", f"expected an object, got {type(raw).__name__}")

    effect_id = raw.get("id")
    if not isinstance(effect_id, str) or not effect_id.strip():
        raise EffectValidationError("invalid_id", "id must be a non-empty string")
    effect_id = effect_id.strip()
    if len(effect_id) > MAX_ID_CHARS or _ID_PATTERN.fullmatch(effect_id) is None:
        raise EffectValidationError(
            "invalid_id", f"id must be <= {MAX_ID_CHARS} chars of [A-Za-z0-9_-]"
        )

    label = raw.get("label")
    if not isinstance(label, str) or not label.strip():
        raise EffectValidationError("invalid_label", "label must be a non-empty string")
    if len(label) > MAX_LABEL_CHARS:
        raise EffectValidationError("invalid_label", f"label exceeds {MAX_LABEL_CHARS} chars")
    if any(ord(ch) < 0x20 or ord(ch) == 0x7F for ch in label):
        raise EffectValidationError("invalid_label", "label contains control characters")

    target = raw.get("target")
    if target != "full_frame":
        raise EffectValidationError("invalid_target", f"unsupported target {target!r}")

    start_s = _coerce_finite_float(raw.get("start_s"), context="start_s")
    end_s = _coerce_finite_float(raw.get("end_s"), context="end_s")
    if start_s < 0.0:
        raise EffectValidationError("invalid_start_s", "start_s must be >= 0")
    if end_s <= start_s:
        raise EffectValidationError("invalid_time_range", "end_s must be > start_s")
    if end_s > MAX_END_S:
        raise EffectValidationError("time_range_exceeds_ceiling", f"end_s must be <= {MAX_END_S}")

    raw_filters = raw.get("filters")
    if not isinstance(raw_filters, list) or not raw_filters:
        raise EffectValidationError("invalid_filters", "filters must be a non-empty list")
    if len(raw_filters) > MAX_FILTERS:
        raise EffectValidationError("chain_too_long", f"filters exceeds MAX_FILTERS={MAX_FILTERS}")

    validated_filters = [
        _validate_filter_node(node, index) for index, node in enumerate(raw_filters)
    ]

    try:
        return EffectSpec(
            id=effect_id,
            label=label,
            filters=validated_filters,
            start_s=start_s,
            end_s=end_s,
            target=target,
        )
    except PydanticValidationError as exc:  # pragma: no cover - belt-and-suspenders
        raise EffectValidationError("schema_error", str(exc)) from exc


def _fmt_num(value: float) -> str:
    """Deterministic, fixed-point (never scientific-notation) formatting."""
    text = f"{float(value):.6f}".rstrip("0").rstrip(".")
    return text if text not in ("", "-") else "0"


def _serialize_filter_node(node: FilterNode, start_s: float, end_s: float) -> str:
    opt_parts: list[str] = []
    for param_name in sorted(node.params.keys()):
        value = node.params[param_name]
        formatted = _fmt_num(value) if isinstance(value, (int, float)) else str(value)
        opt_parts.append(f"{param_name}={formatted}")
    if node.name not in FILTERS_WITHOUT_TIMELINE_ENABLE:
        opt_parts.append(f"enable='between(t,{_fmt_num(start_s)},{_fmt_num(end_s)})'")
    options = ":".join(opt_parts)
    return f"{node.name}={options}" if options else node.name


def effect_spec_to_filter_chain(spec: EffectSpec) -> str:
    """Serialize a validated `EffectSpec` into an FFmpeg `-vf` fragment.

    Only ever reads back typed values already validated onto `spec` —
    never string-interpolates anything from an original raw request. Each
    filter that supports FFmpeg's `enable=` timeline gating (i.e. not in
    FILTERS_WITHOUT_TIMELINE_ENABLE) gets `enable='between(t,start,end)'`
    so it activates only inside the effect's own window; the remaining
    filters run for however long the caller's chain/wrapper leaves them
    active, per that constant's docstring.
    """
    return ",".join(_serialize_filter_node(node, spec.start_s, spec.end_s) for node in spec.filters)


# Cheap, deliberately crude per-filter complexity weights for estimate_cost.
# Every ALLOWED_FILTERS entry has a weight (pinned by
# test_custom_effects.py); unknown filters can't reach here (they're
# rejected by validate_effect_spec first) but default to 1.0 for safety.
FILTER_COST_WEIGHTS: dict[str, float] = {
    "eq": 1.0,
    "hue": 1.0,
    "unsharp": 2.5,
    "gblur": 3.0,
    "boxblur": 2.0,
    "curves": 1.5,
    "colorbalance": 1.0,
    "colorchannelmixer": 1.0,
    "vignette": 1.5,
    "noise": 2.0,
    "tblend": 2.5,
    "fade": 0.5,
    "crop": 0.5,
    "scale": 1.5,
    "zoompan": 3.5,
    "rotate": 2.0,
    "hflip": 0.2,
    "vflip": 0.2,
    "setpts": 0.5,
    "chromashift": 1.0,
}

# Reliability guard, NOT a usage/billing limit — usage-based per-user limits
# are explicitly deferred (see the plan's risk #6). A future execution-path
# caller should treat estimate_cost(...) above this ceiling as "reject or
# flag for review before dispatch", not silently render it anyway.
EFFECT_COST_CEILING = 250.0


def estimate_cost(spec: EffectSpec, duration_s: float) -> float:
    """Cheap proxy for how expensive `spec` is to burn in: (sum of
    per-filter weights) x (effect window length, clamped to the clip's
    actual duration if known). Deliberately crude — exists to catch
    egregious cases (a MAX_FILTERS chain stretched across a long window)
    cheaply, not to model FFmpeg's real per-frame cost."""
    clip_duration = max(0.0, duration_s)
    window_end = min(spec.end_s, clip_duration) if clip_duration > 0 else spec.end_s
    effective_s = max(0.0, window_end - spec.start_s)
    weight_sum = sum(FILTER_COST_WEIGHTS.get(node.name, 1.0) for node in spec.filters)
    return round(weight_sum * effective_s, 4)
