"""Security-critical test suite for the sandboxed effect-language validator
(PR5 of the Nova AI effect-language train — see
app/pipeline/custom_effects.py). This module is the ONLY thing standing
between an agent- or user-authored effect spec and a real FFmpeg filter
string, so this file over-invests: every documented rejection class gets
its own test, plus a dedicated injection-attempt suite and pin tests on
the whitelist itself.
"""

from __future__ import annotations

import math

import pytest

from app.pipeline.custom_effects import (
    ALLOWED_FILTERS,
    EFFECT_COST_CEILING,
    FILTER_COST_WEIGHTS,
    FILTER_PARAM_SPECS,
    FILTERS_WITHOUT_TIMELINE_ENABLE,
    MAX_END_S,
    MAX_FILTERS,
    MAX_LABEL_CHARS,
    EffectValidationError,
    effect_spec_to_filter_chain,
    estimate_cost,
    validate_effect_spec,
)

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _base_raw(**overrides):
    raw = {
        "id": "effect-1",
        "label": "Vintage look",
        "filters": [{"name": "eq", "params": {"contrast": 1.2}}],
        "start_s": 1.0,
        "end_s": 3.5,
        "target": "full_frame",
    }
    raw.update(overrides)
    return raw


def _reason(raw: dict) -> str:
    with pytest.raises(EffectValidationError) as exc_info:
        validate_effect_spec(raw)
    return exc_info.value.reason


# ---------------------------------------------------------------------------
# Whitelist pin — changes to ALLOWED_FILTERS must be conscious (Emir sign-off)
# ---------------------------------------------------------------------------


def test_allowed_filters_pin():
    """Pin the exact ALLOWED_FILTERS contents so any change is deliberate,
    not an accidental typo/addition. If this test fails because you meant
    to add/remove a filter, update this set AND get sign-off per the module
    docstring."""
    assert ALLOWED_FILTERS == frozenset(
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
    assert len(ALLOWED_FILTERS) == 20


def test_every_allowed_filter_has_a_param_spec():
    """Property-style: FILTER_PARAM_SPECS must have an entry (possibly
    empty, e.g. hflip/vflip) for every ALLOWED_FILTERS member, and no
    extra entries for filters that aren't allowed."""
    assert set(FILTER_PARAM_SPECS.keys()) == ALLOWED_FILTERS


def test_every_allowed_filter_has_a_cost_weight():
    assert set(FILTER_COST_WEIGHTS.keys()) == ALLOWED_FILTERS


def test_filters_without_timeline_enable_is_subset_of_allowed():
    assert FILTERS_WITHOUT_TIMELINE_ENABLE <= ALLOWED_FILTERS


def test_no_enum_value_contains_forbidden_chars():
    """Defense in depth: even though ParamSpec.__post_init__ already
    asserts this at definition time, pin it as a standalone test so CI
    catches a future spec edit that reintroduces syntax characters."""
    forbidden = set(":;,'\"[]=\\/")
    for filter_name, params in FILTER_PARAM_SPECS.items():
        for param_name, spec in params.items():
            if spec.kind != "enum":
                continue
            for value in spec.enum:
                assert not (set(value) & forbidden), (
                    f"{filter_name}.{param_name} enum value {value!r} contains a forbidden char"
                )


# ---------------------------------------------------------------------------
# Happy path
# ---------------------------------------------------------------------------


def test_valid_spec_is_accepted():
    spec = validate_effect_spec(_base_raw())
    assert spec.id == "effect-1"
    assert spec.label == "Vintage look"
    assert spec.filters[0].name == "eq"
    assert spec.filters[0].params == {"contrast": 1.2}
    assert spec.start_s == 1.0
    assert spec.end_s == 3.5
    assert spec.target == "full_frame"


def test_valid_spec_with_empty_params_filter_is_accepted():
    spec = validate_effect_spec(_base_raw(filters=[{"name": "hflip", "params": {}}]))
    assert spec.filters[0].params == {}


def test_valid_spec_with_omitted_params_key_is_accepted():
    spec = validate_effect_spec(_base_raw(filters=[{"name": "vflip"}]))
    assert spec.filters[0].params == {}


def test_valid_spec_with_max_filters_is_accepted():
    spec = validate_effect_spec(_base_raw(filters=[{"name": "eq", "params": {}}] * MAX_FILTERS))
    assert len(spec.filters) == MAX_FILTERS


def test_valid_spec_at_max_end_s_is_accepted():
    spec = validate_effect_spec(_base_raw(start_s=MAX_END_S - 1.0, end_s=MAX_END_S))
    assert spec.end_s == MAX_END_S


def test_valid_spec_at_max_label_chars_is_accepted():
    label = "x" * MAX_LABEL_CHARS
    spec = validate_effect_spec(_base_raw(label=label))
    assert spec.label == label


def test_enum_param_string_value_is_preserved_not_coerced_to_number():
    spec = validate_effect_spec(
        _base_raw(
            filters=[{"name": "fade", "params": {"type": "in", "duration": 1.0, "alpha": "1"}}]
        )
    )
    assert spec.filters[0].params["alpha"] == "1"
    assert isinstance(spec.filters[0].params["alpha"], str)


# ---------------------------------------------------------------------------
# Unknown / disallowed filters — every file/URL/command-accepting filter
# named in the plan must be rejected.
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "filter_name",
    ["movie", "subtitles", "drawtext", "ass", "lut3d", "sendcmd", "zmq", "concat"],
)
def test_rejects_dangerous_filters_not_in_whitelist(filter_name):
    raw = _base_raw(filters=[{"name": filter_name, "params": {}}])
    assert _reason(raw) == "filter_not_allowed"


def test_rejects_unknown_gibberish_filter_name():
    raw = _base_raw(filters=[{"name": "not_a_real_filter_xyz", "params": {}}])
    assert _reason(raw) == "filter_not_allowed"


def test_rejects_non_string_filter_name():
    raw = _base_raw(filters=[{"name": 123, "params": {}}])
    assert _reason(raw) == "invalid_filter_node"


def test_rejects_filter_node_missing_name():
    raw = _base_raw(filters=[{"params": {}}])
    assert _reason(raw) == "invalid_filter_node"


def test_rejects_non_dict_filter_node():
    raw = _base_raw(filters=["eq"])
    assert _reason(raw) == "invalid_filter_node"


# ---------------------------------------------------------------------------
# Unknown params
# ---------------------------------------------------------------------------


def test_rejects_unknown_param_name():
    raw = _base_raw(filters=[{"name": "eq", "params": {"not_a_real_param": 1.0}}])
    assert _reason(raw) == "unknown_param"


def test_rejects_param_belonging_to_a_different_filter():
    # "preset" is valid on curves, not eq.
    raw = _base_raw(filters=[{"name": "eq", "params": {"preset": "vintage"}}])
    assert _reason(raw) == "unknown_param"


def test_rejects_non_dict_params():
    raw = _base_raw(filters=[{"name": "eq", "params": "contrast=1"}])
    assert _reason(raw) == "invalid_filter_node"


# ---------------------------------------------------------------------------
# Out-of-bounds numerics
# ---------------------------------------------------------------------------


def test_rejects_numeric_above_max():
    raw = _base_raw(filters=[{"name": "eq", "params": {"contrast": 999.0}}])
    assert _reason(raw) == "param_out_of_range"


def test_rejects_numeric_below_min():
    raw = _base_raw(filters=[{"name": "eq", "params": {"brightness": -5.0}}])
    assert _reason(raw) == "param_out_of_range"


def test_accepts_numeric_at_exact_bounds():
    spec = validate_effect_spec(_base_raw(filters=[{"name": "eq", "params": {"brightness": -1.0}}]))
    assert spec.filters[0].params["brightness"] == -1.0
    spec2 = validate_effect_spec(_base_raw(filters=[{"name": "eq", "params": {"brightness": 1.0}}]))
    assert spec2.filters[0].params["brightness"] == 1.0


def test_rejects_non_numeric_param_for_numeric_spec():
    raw = _base_raw(filters=[{"name": "eq", "params": {"contrast": "high"}}])
    assert _reason(raw) == "param_wrong_type"


def test_rejects_bool_as_numeric_param():
    # bool is an int subclass in Python; must not sneak through as 0/1.
    raw = _base_raw(filters=[{"name": "eq", "params": {"contrast": True}}])
    assert _reason(raw) == "param_wrong_type"


def test_rejects_none_as_numeric_param():
    raw = _base_raw(filters=[{"name": "eq", "params": {"contrast": None}}])
    assert _reason(raw) == "param_wrong_type"


def test_rejects_list_as_numeric_param():
    raw = _base_raw(filters=[{"name": "eq", "params": {"contrast": [1.0]}}])
    assert _reason(raw) == "param_wrong_type"


# ---------------------------------------------------------------------------
# NaN / inf
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("bad_value", [math.nan, math.inf, -math.inf])
def test_rejects_nan_and_inf_params(bad_value):
    raw = _base_raw(filters=[{"name": "eq", "params": {"contrast": bad_value}}])
    assert _reason(raw) == "param_nan_or_inf"


@pytest.mark.parametrize("bad_value", [math.nan, math.inf, -math.inf])
def test_rejects_nan_and_inf_start_end(bad_value):
    assert _reason(_base_raw(start_s=bad_value)) in ("param_nan_or_inf", "param_wrong_type")
    assert _reason(_base_raw(end_s=bad_value)) in ("param_nan_or_inf", "param_wrong_type")


# ---------------------------------------------------------------------------
# String params: not-in-enum vs forbidden-char injection (two DISTINCT
# rejection classes, per the plan).
# ---------------------------------------------------------------------------


def test_rejects_enum_string_not_a_member():
    raw = _base_raw(filters=[{"name": "curves", "params": {"preset": "not_a_real_preset"}}])
    assert _reason(raw) == "enum_value_not_allowed"


def test_rejects_non_string_value_for_enum_param():
    raw = _base_raw(filters=[{"name": "curves", "params": {"preset": 1}}])
    assert _reason(raw) == "param_wrong_type"


@pytest.mark.parametrize(
    "injected",
    [
        "vintage:evil",
        "vintage;evil",
        "vintage,evil",
        "vintage'evil",
        'vintage"evil',
        "vintage[evil]",
        "vintage=evil",
        "vintage\\evil",
        "vintage/evil",
        "1:enable='between(t,0,1)'",
    ],
)
def test_rejects_enum_string_containing_forbidden_chars(injected):
    raw = _base_raw(filters=[{"name": "curves", "params": {"preset": injected}}])
    assert _reason(raw) == "forbidden_chars_in_string"


def test_rejects_unicode_lookalike_colon_as_not_a_member():
    # Fullwidth colon (U+FF1A) is not in the ASCII forbidden-char set, so it
    # falls through to the enum-membership check instead — still rejected,
    # just via a different (still safe) reason code.
    raw = _base_raw(filters=[{"name": "curves", "params": {"preset": "vintage："}}])
    assert _reason(raw) == "enum_value_not_allowed"


# ---------------------------------------------------------------------------
# Injection-attempt suite (explicit, per the plan's ask)
# ---------------------------------------------------------------------------


def test_injection_movie_file_path_is_rejected():
    raw = _base_raw(filters=[{"name": "movie", "params": {"filename": "/tmp/x"}}])
    assert _reason(raw) == "filter_not_allowed"


def test_injection_subtitles_empty_value_is_rejected():
    raw = _base_raw(filters=[{"name": "subtitles", "params": {"filename": ""}}])
    assert _reason(raw) == "filter_not_allowed"


def test_injection_param_value_smuggling_extra_filter_syntax_is_rejected():
    raw = _base_raw(
        filters=[{"name": "curves", "params": {"preset": "1:enable='between(t,0,999)'"}}]
    )
    assert _reason(raw) == "forbidden_chars_in_string"


def test_injection_param_name_containing_equals_is_rejected():
    raw = _base_raw(filters=[{"name": "eq", "params": {"contrast=1;evil": 1.0}}])
    assert _reason(raw) == "unknown_param"


def test_injection_param_name_containing_colon_is_rejected():
    raw = _base_raw(filters=[{"name": "eq", "params": {"contrast:evil": 1.0}}])
    assert _reason(raw) == "unknown_param"


def test_injection_unicode_lookalikes_do_not_bypass_validation():
    # Fullwidth "＝" (U+FF1D) lookalike for "=" in a param name — still just
    # an unrecognized param name, never reaches the allowlist.
    raw = _base_raw(filters=[{"name": "eq", "params": {"contrast＝1": 1.0}}])
    assert _reason(raw) == "unknown_param"


def test_injection_extra_top_level_keys_are_rejected_by_extra_forbid():
    # Not a validate_effect_spec check directly, but the final EffectSpec
    # model must not silently accept smuggled top-level fields either.
    from app.pipeline.custom_effects import EffectSpec

    with pytest.raises(Exception):
        EffectSpec(
            id="x",
            label="L",
            filters=[{"name": "eq", "params": {}}],
            start_s=0.0,
            end_s=1.0,
            target="full_frame",
            sneaky_field="/etc/passwd",
        )


# ---------------------------------------------------------------------------
# Chain length
# ---------------------------------------------------------------------------


def test_rejects_chain_longer_than_max_filters():
    raw = _base_raw(filters=[{"name": "eq", "params": {}}] * (MAX_FILTERS + 1))
    assert _reason(raw) == "chain_too_long"


def test_rejects_empty_filter_chain():
    raw = _base_raw(filters=[])
    assert _reason(raw) == "invalid_filters"


def test_rejects_non_list_filters():
    raw = _base_raw(filters={"name": "eq"})
    assert _reason(raw) == "invalid_filters"


# ---------------------------------------------------------------------------
# Label
# ---------------------------------------------------------------------------


def test_rejects_label_over_max_chars():
    raw = _base_raw(label="x" * (MAX_LABEL_CHARS + 1))
    assert _reason(raw) == "invalid_label"


def test_rejects_empty_label():
    raw = _base_raw(label="")
    assert _reason(raw) == "invalid_label"


def test_rejects_whitespace_only_label():
    raw = _base_raw(label="   ")
    assert _reason(raw) == "invalid_label"


@pytest.mark.parametrize("bad_char", ["\x00", "\x07", "\x1b", "\x7f"])
def test_rejects_label_with_control_chars(bad_char):
    raw = _base_raw(label=f"hello{bad_char}world")
    assert _reason(raw) == "invalid_label"


def test_rejects_non_string_label():
    raw = _base_raw(label=42)
    assert _reason(raw) == "invalid_label"


# ---------------------------------------------------------------------------
# id
# ---------------------------------------------------------------------------


def test_rejects_empty_id():
    raw = _base_raw(id="")
    assert _reason(raw) == "invalid_id"


def test_rejects_id_with_path_separators():
    raw = _base_raw(id="../../etc/passwd")
    assert _reason(raw) == "invalid_id"


def test_rejects_id_over_max_chars():
    raw = _base_raw(id="x" * 129)
    assert _reason(raw) == "invalid_id"


# ---------------------------------------------------------------------------
# Time range
# ---------------------------------------------------------------------------


def test_rejects_negative_start_s():
    raw = _base_raw(start_s=-1.0, end_s=1.0)
    assert _reason(raw) == "invalid_start_s"


def test_rejects_end_before_start():
    raw = _base_raw(start_s=5.0, end_s=2.0)
    assert _reason(raw) == "invalid_time_range"


def test_rejects_end_equal_to_start():
    raw = _base_raw(start_s=2.0, end_s=2.0)
    assert _reason(raw) == "invalid_time_range"


def test_rejects_end_s_beyond_ceiling():
    raw = _base_raw(start_s=0.0, end_s=MAX_END_S + 0.01)
    assert _reason(raw) == "time_range_exceeds_ceiling"


def test_rejects_non_numeric_start_s():
    raw = _base_raw(start_s="soon")
    assert _reason(raw) == "param_wrong_type"


# ---------------------------------------------------------------------------
# target
# ---------------------------------------------------------------------------


def test_rejects_target_other_than_full_frame():
    raw = _base_raw(target="clip")
    assert _reason(raw) == "invalid_target"


def test_rejects_missing_target():
    raw = _base_raw()
    del raw["target"]
    assert _reason(raw) == "invalid_target"


# ---------------------------------------------------------------------------
# Serialization — byte-exact
# ---------------------------------------------------------------------------


def test_serialization_is_byte_exact_for_a_known_spec():
    raw = {
        "id": "vintage-1",
        "label": "Vintage look",
        "filters": [
            {"name": "eq", "params": {"contrast": 1.2, "saturation": 0.8}},
            {"name": "curves", "params": {"preset": "vintage"}},
            {"name": "vignette", "params": {}},
        ],
        "start_s": 1.0,
        "end_s": 3.5,
        "target": "full_frame",
    }
    spec = validate_effect_spec(raw)
    chain = effect_spec_to_filter_chain(spec)
    assert chain == (
        "eq=contrast=1.2:saturation=0.8:enable='between(t,1,3.5)',"
        "curves=preset=vintage:enable='between(t,1,3.5)',"
        "vignette=enable='between(t,1,3.5)'"
    )


def test_serialization_omits_enable_for_filters_without_timeline_support():
    raw = _base_raw(filters=[{"name": "crop", "params": {"w": 0.9, "h": 0.9, "x": 0.0, "y": 0.0}}])
    spec = validate_effect_spec(raw)
    chain = effect_spec_to_filter_chain(spec)
    assert "enable=" not in chain
    assert chain == "crop=h=0.9:w=0.9:x=0:y=0"


def test_serialization_never_leaks_forbidden_chars_for_any_valid_spec():
    # Sanity net: for every allowed filter, a minimal valid instance must
    # serialize into a string that only uses ffmpeg's own separators in the
    # positions this module puts them (no stray quotes/brackets from param
    # values, since those are pre-validated).
    for filter_name, params in FILTER_PARAM_SPECS.items():
        filled = {}
        for param_name, spec in params.items():
            filled[param_name] = spec.min if spec.kind == "numeric" else next(iter(spec.enum))
        raw = _base_raw(filters=[{"name": filter_name, "params": filled}])
        spec_obj = validate_effect_spec(raw)
        chain = effect_spec_to_filter_chain(spec_obj)
        assert isinstance(chain, str) and chain.startswith(filter_name)


def test_serialization_uses_fixed_point_never_scientific_notation():
    raw = _base_raw(
        filters=[{"name": "chromashift", "params": {"cbh": 0.0001}}], start_s=0.0, end_s=1.0
    )
    spec = validate_effect_spec(raw)
    chain = effect_spec_to_filter_chain(spec)
    assert "e-" not in chain.lower()
    assert "e+" not in chain.lower()


# ---------------------------------------------------------------------------
# estimate_cost
# ---------------------------------------------------------------------------


def test_estimate_cost_scales_with_window_length():
    spec = validate_effect_spec(_base_raw(start_s=0.0, end_s=2.0))
    short = estimate_cost(spec, duration_s=10.0)
    spec2 = validate_effect_spec(_base_raw(start_s=0.0, end_s=4.0))
    longer = estimate_cost(spec2, duration_s=10.0)
    assert longer > short


def test_estimate_cost_clamps_to_clip_duration():
    spec = validate_effect_spec(_base_raw(start_s=0.0, end_s=100.0))
    clamped = estimate_cost(spec, duration_s=5.0)
    unclamped = estimate_cost(spec, duration_s=100.0)
    assert clamped < unclamped


def test_estimate_cost_is_nonnegative_for_zero_duration():
    spec = validate_effect_spec(_base_raw(start_s=0.0, end_s=1.0))
    assert estimate_cost(spec, duration_s=0.0) >= 0.0


def test_estimate_cost_ceiling_is_a_positive_finite_constant():
    assert EFFECT_COST_CEILING > 0
    assert math.isfinite(EFFECT_COST_CEILING)


# ---------------------------------------------------------------------------
# Config flag
# ---------------------------------------------------------------------------


def test_custom_effects_enabled_defaults_to_false():
    from app.config import Settings

    assert Settings.model_fields["custom_effects_enabled"].default is False
