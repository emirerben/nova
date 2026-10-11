#!/usr/bin/env python3
"""Resolve the duration-contract mode from Fly's managed Machine configs."""

from __future__ import annotations

import argparse
import json
from collections.abc import Mapping, Sequence
from typing import Any, Literal


Mode = Literal["log_only", "enforce"]
ENVIRONMENT_KEY = "CREATOR_DURATION_CHECK_MODE"
VALID_MODES = {"log_only", "enforce"}


class ModeResolutionError(ValueError):
    """The running fleet cannot safely provide a rollout mode."""


def _machine_config(machine: Mapping[str, Any]) -> Mapping[str, Any]:
    config = machine.get("config")
    if isinstance(config, Mapping):
        return config
    incomplete = machine.get("incomplete_config")
    return incomplete if isinstance(incomplete, Mapping) else {}


def resolve_mode(machines: Sequence[object], *, requested_mode: str | None) -> Mode:
    """Return a manual choice or the one consistent managed-fleet setting.

    A push inherits the observed setting. An all-absent setting is the initial
    rollout state, ``log_only``. A partially absent, malformed, or mixed fleet
    is unsafe because it cannot establish a single authoritative mode.
    """
    if requested_mode is not None:
        if requested_mode not in VALID_MODES:
            raise ModeResolutionError("manual creator duration mode is invalid")
        return requested_mode  # type: ignore[return-value]

    observed: list[str | None] = []
    for machine in machines:
        if not isinstance(machine, Mapping):
            raise ModeResolutionError("Fly Machine list contains a malformed entry")
        config = _machine_config(machine)
        metadata = config.get("metadata")
        if not isinstance(metadata, Mapping) or not metadata.get("fly_process_group"):
            continue
        environment = config.get("env")
        if environment is None:
            observed.append(None)
        elif isinstance(environment, Mapping):
            if ENVIRONMENT_KEY not in environment:
                observed.append(None)
            else:
                value = environment[ENVIRONMENT_KEY]
                observed.append(value if isinstance(value, str) else "<malformed>")
        else:
            observed.append("<malformed>")
    if not observed or all(value is None for value in observed):
        return "log_only"
    if any(value is None or value not in VALID_MODES for value in observed):
        raise ModeResolutionError("managed Fly Machines have absent or malformed duration modes")
    if len(set(observed)) != 1:
        raise ModeResolutionError("managed Fly Machines disagree on duration mode")
    return observed[0]  # type: ignore[return-value]


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--machines-json", required=True)
    parser.add_argument("--requested-mode", default=None)
    args = parser.parse_args()
    try:
        raw = json.loads(args.machines_json)
    except json.JSONDecodeError as exc:
        raise SystemExit(f"KRI-499 duration mode: Fly returned invalid JSON: {exc}") from exc
    if not isinstance(raw, list):
        raise SystemExit("KRI-499 duration mode: Fly Machine list must be an array")
    try:
        print(resolve_mode(raw, requested_mode=args.requested_mode or None))
    except ModeResolutionError as exc:
        raise SystemExit(f"KRI-499 duration mode: {exc}") from exc


if __name__ == "__main__":
    main()
