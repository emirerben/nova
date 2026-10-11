"""Pure parsing tests for the deploy-time creator duration mode resolver."""

from __future__ import annotations

import importlib.util
from pathlib import Path

import pytest


SCRIPT = Path(__file__).with_name("resolve-creator-duration-mode.py")
SPEC = importlib.util.spec_from_file_location("duration_mode_resolver", SCRIPT)
assert SPEC and SPEC.loader
MODULE = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(MODULE)


def _machine(mode: str | None) -> dict[str, object]:
    env = {} if mode is None else {"CREATOR_DURATION_CHECK_MODE": mode}
    return {"config": {"metadata": {"fly_process_group": "api"}, "env": env}}


def test_push_inherits_consistent_managed_mode() -> None:
    assert MODULE.resolve_mode([_machine("enforce"), _machine("enforce")], requested_mode=None) == "enforce"


def test_push_uses_log_only_when_setting_is_absent_everywhere() -> None:
    assert MODULE.resolve_mode([_machine(None), _machine(None)], requested_mode=None) == "log_only"


@pytest.mark.parametrize("modes", [("enforce", "log_only"), ("enforce", None), ("unexpected", "unexpected")])
def test_push_rejects_mixed_or_malformed_managed_modes(modes: tuple[str | None, str | None]) -> None:
    with pytest.raises(MODULE.ModeResolutionError):
        MODULE.resolve_mode([_machine(modes[0]), _machine(modes[1])], requested_mode=None)


def test_manual_log_only_explicitly_overrides_running_mode() -> None:
    assert MODULE.resolve_mode([_machine("enforce")], requested_mode="log_only") == "log_only"
