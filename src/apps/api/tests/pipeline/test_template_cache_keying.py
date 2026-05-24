"""Tests for the content-hashed Layer-2 cache key derivation.

The cache key for Layer-2 (`text_overlay_version` = "v2-…") is derived from
the content of the prompt files, the schema modules, the agents'
`prompt_version` strings, and a settings-flag dict. Each input must
independently invalidate the key, and identical inputs must produce
identical keys regardless of insertion order.

Replaces the prior pattern where bumping the v2 cache namespace required
remembering to edit a hardcoded ``TEXT_OVERLAY_VERSION_V2 = "v2-2026-..."``
string. The hashing helper is the single source of truth now.
"""

from __future__ import annotations

from pathlib import Path

from app.pipeline.template_cache import (
    TEXT_OVERLAY_VERSION_V1,
    compute_text_overlay_version,
)


def _write(path: Path, content: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(content)


def _make_fixture(tmp_path: Path) -> dict:
    """Build a small on-disk fixture of fake prompts + schemas + versions.

    Returns the kwargs to pass to ``compute_text_overlay_version`` so each
    test can flip exactly one input.
    """
    prompt_a = tmp_path / "prompts" / "stage_e.txt"
    prompt_b = tmp_path / "prompts" / "stage_g.txt"
    schema_a = tmp_path / "schemas" / "text_alignment.py"
    schema_b = tmp_path / "schemas" / "text_classification.py"
    _write(prompt_a, b"align prompt v1")
    _write(prompt_b, b"classify prompt v1")
    _write(schema_a, b"# alignment schema")
    _write(schema_b, b"# classification schema")
    return {
        "force_layer2": True,
        "settings_flag": False,
        "prompt_paths": (prompt_a, prompt_b),
        "schema_paths": (schema_a, schema_b),
        "prompt_versions": ("2026-05-19", "2026-05-18.1"),
        "settings_dict": {"text_overlay_v2_enabled": False},
    }


def test_identical_inputs_produce_identical_key(tmp_path):
    kw = _make_fixture(tmp_path)
    a = compute_text_overlay_version(**kw)
    b = compute_text_overlay_version(**kw)
    assert a == b
    assert a.startswith("v2-")


def test_prompt_change_invalidates_key(tmp_path):
    kw = _make_fixture(tmp_path)
    before = compute_text_overlay_version(**kw)
    # Mutate one prompt file's content; everything else stays identical.
    kw["prompt_paths"][0].write_bytes(b"align prompt v2 NEW")
    after = compute_text_overlay_version(**kw)
    assert before != after, "prompt-file content change must invalidate the Layer-2 cache key"


def test_schema_change_invalidates_key(tmp_path):
    kw = _make_fixture(tmp_path)
    before = compute_text_overlay_version(**kw)
    kw["schema_paths"][1].write_bytes(b"# classification schema with new field")
    after = compute_text_overlay_version(**kw)
    assert before != after, "schema-module content change must invalidate the Layer-2 cache key"


def test_prompt_version_change_invalidates_key(tmp_path):
    kw = _make_fixture(tmp_path)
    before = compute_text_overlay_version(**kw)
    kw["prompt_versions"] = ("2026-05-19", "2026-05-23")  # bumped one
    after = compute_text_overlay_version(**kw)
    assert before != after, "AgentSpec.prompt_version bump must invalidate the Layer-2 cache key"


def test_settings_flag_change_invalidates_key(tmp_path):
    kw = _make_fixture(tmp_path)
    before = compute_text_overlay_version(**kw)
    kw["settings_dict"] = {"text_overlay_v2_enabled": True}
    after = compute_text_overlay_version(**kw)
    assert before != after, "settings-dict value change must invalidate the Layer-2 cache key"


def test_settings_key_ordering_stable(tmp_path):
    """Passing the settings dict with different insertion orders must not
    change the key — JSON encoding is sort_keys=True."""
    kw = _make_fixture(tmp_path)
    kw["settings_dict"] = {"a": 1, "b": 2, "c": 3}
    forward = compute_text_overlay_version(**kw)
    kw["settings_dict"] = {"c": 3, "a": 1, "b": 2}
    reordered = compute_text_overlay_version(**kw)
    kw["settings_dict"] = {"b": 2, "c": 3, "a": 1}
    yet_another = compute_text_overlay_version(**kw)
    assert forward == reordered == yet_another, (
        "settings-dict key order must not affect the Layer-2 cache key"
    )


def test_returns_v1_when_layer2_disabled(tmp_path):
    """When neither force_layer2 nor settings_flag is true, the function
    short-circuits to ``"v1"`` — the v1 path is namespaced via
    TEMPLATE_PROMPT_VERSION + CACHE_SCHEMA_VERSION and does not need this
    hash."""
    kw = _make_fixture(tmp_path)
    kw["force_layer2"] = False
    kw["settings_flag"] = False
    assert compute_text_overlay_version(**kw) == TEXT_OVERLAY_VERSION_V1


def test_prompt_version_ordering_stable(tmp_path):
    """Passing prompt_versions in a different tuple order must produce the
    same key — versions are sorted before hashing."""
    kw = _make_fixture(tmp_path)
    kw["prompt_versions"] = ("2026-05-19", "2026-05-18.1", "2026-05-17")
    forward = compute_text_overlay_version(**kw)
    kw["prompt_versions"] = ("2026-05-17", "2026-05-19", "2026-05-18.1")
    reordered = compute_text_overlay_version(**kw)
    assert forward == reordered


def test_default_inputs_resolve_against_real_files(tmp_path):
    """Call with no overrides — exercises the production code path that
    pulls prompt + schema paths and AgentSpec.prompt_versions from the
    repo. Pinned to ``v2-`` prefix and a 16-char hex suffix."""
    v = compute_text_overlay_version(force_layer2=True, settings_flag=False)
    assert v.startswith("v2-")
    suffix = v[len("v2-") :]
    assert len(suffix) == 16
    int(suffix, 16)  # raises if non-hex
