from pathlib import Path

import pytest

from app.services import editor_limits


def test_editor_limits_are_loaded_from_shared_runtime_contract() -> None:
    assert editor_limits.EDITOR_MAX_TIMELINE_SLOTS == 120
    assert editor_limits.MOTION_MAX_INSTANCES == 12
    assert editor_limits.MOTION_MAX_ACTIVE_FRAMES == 360
    assert editor_limits.MOTION_MAX_CONCURRENT_COMPLEXITY == 8
    assert editor_limits.MOTION_MAX_COMPLEXITY_UNITS == 1440


def test_shallow_production_module_path_does_not_evaluate_source_fallback() -> None:
    source = Path("/app/app/services/editor_limits.py")
    assert editor_limits._source_tree_limits_path(source) is None


def test_source_tree_fallback_searches_parents(tmp_path: Path) -> None:
    limits = tmp_path / "packages" / "motion-runtime" / "motion-limits.json"
    limits.parent.mkdir(parents=True)
    limits.write_text("{}")
    source = tmp_path / "src" / "apps" / "api" / "app" / "services" / "editor_limits.py"
    assert editor_limits._source_tree_limits_path(source) == limits


def test_limit_loader_rejects_boolean_values(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    limits = tmp_path / "motion-limits.json"
    limits.write_text(
        '{"motion_fps":true,"timeline_max_slots":120,"motion_max_instances":12,'
        '"motion_max_instance_seconds":8,"motion_max_active_seconds":12,'
        '"motion_max_concurrent_complexity":8,'
        '"motion_max_complexity_multiplier":4}'
    )
    monkeypatch.setattr(editor_limits, "_limits_path", lambda: limits)
    editor_limits._limits.cache_clear()
    try:
        with pytest.raises(RuntimeError, match="limits are invalid"):
            editor_limits._limits()
    finally:
        editor_limits._limits.cache_clear()
