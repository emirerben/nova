"""Print a safe snapshot of the effective process rollout settings.

This command reports configuration loaded by this process.  It does not query
any service and must never be read as an observation of production state.
"""

from __future__ import annotations

import argparse
import json
from datetime import UTC, datetime
from typing import Any, get_args


def _parser() -> argparse.ArgumentParser:
    return argparse.ArgumentParser(
        description="Print a safe, local runtime-settings profile as JSON."
    )


def _captured_at_utc() -> str:
    return datetime.now(UTC).isoformat().replace("+00:00", "Z")


def _failure_payload() -> dict[str, Any]:
    return {
        "captured_at_utc": _captured_at_utc(),
        "provenance": "effective process settings; not a production observation",
        "status": "configuration_unavailable",
    }


def _profile_payload() -> dict[str, Any]:
    # Imports deliberately live here: --help must work even when the local
    # environment cannot instantiate Settings.
    from app.config import Settings, settings
    from app.kria.recipes import MediaCapability

    boolean_settings = {
        name: value
        for name, field in Settings.model_fields.items()
        if field.annotation is bool and type(value := getattr(settings, name)) is bool
    }
    safe_capabilities = frozenset(get_args(MediaCapability))
    verified_features = sorted(
        {
            feature
            for feature in settings.phone_render_verified_features
            if type(feature) is str and feature in safe_capabilities
        }
    )
    minimum_protocol = settings.kria_minimum_client_protocol
    if type(minimum_protocol) is not int:
        raise ValueError("minimum client protocol has an unsafe type")
    return {
        "captured_at_utc": _captured_at_utc(),
        "provenance": "effective process settings; not a production observation",
        "status": "ok",
        "boolean_settings": boolean_settings,
        "kria_minimum_client_protocol": minimum_protocol,
        "phone_render_verified_features": verified_features,
    }


def main(argv: list[str] | None = None) -> int:
    _parser().parse_args(argv)
    try:
        payload = _profile_payload()
    except Exception:  # Configuration errors must not reveal env values or traces.
        print(json.dumps(_failure_payload(), sort_keys=True))
        return 1
    print(json.dumps(payload, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
