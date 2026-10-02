from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

from app.cli import runtime_profile


def _install_fake_config(monkeypatch, *, features: list[object]) -> None:
    fake_settings = SimpleNamespace(
        safe_flag=True,
        disabled_flag=False,
        scalar_secret="do-not-export",
        kria_minimum_client_protocol=7,
        phone_render_verified_features=features,
    )
    fake_fields = {
        "safe_flag": SimpleNamespace(annotation=bool),
        "disabled_flag": SimpleNamespace(annotation=bool),
        "scalar_secret": SimpleNamespace(annotation=str),
        "kria_minimum_client_protocol": SimpleNamespace(annotation=int),
        "phone_render_verified_features": SimpleNamespace(annotation=list[str]),
    }
    monkeypatch.setitem(
        sys.modules,
        "app.config",
        SimpleNamespace(Settings=SimpleNamespace(model_fields=fake_fields), settings=fake_settings),
    )
    monkeypatch.setitem(
        sys.modules,
        "app.kria.recipes",
        SimpleNamespace(MediaCapability=__import__("typing").Literal["audioMix", "stillImages"]),
    )


def test_help_does_not_load_configuration(capsys, monkeypatch) -> None:
    monkeypatch.setattr(
        runtime_profile,
        "_profile_payload",
        lambda: (_ for _ in ()).throw(AssertionError),
    )

    with pytest.raises(SystemExit) as exc_info:
        runtime_profile.main(["--help"])

    assert exc_info.value.code == 0
    assert "safe, local runtime-settings profile" in capsys.readouterr().out


def test_module_help_succeeds_without_configuration_environment() -> None:
    api_dir = Path(__file__).resolve().parents[2]
    result = subprocess.run(
        [sys.executable, "-m", "app.cli.runtime_profile", "--help"],
        cwd=api_dir,
        env={"PATH": os.environ["PATH"]},
        text=True,
        capture_output=True,
        check=False,
    )

    assert result.returncode == 0
    assert "safe, local runtime-settings profile" in result.stdout


def test_profile_exports_only_safe_setting_types(monkeypatch, capsys) -> None:
    _install_fake_config(monkeypatch, features=["audioMix", "unknown", 4, "audioMix"])

    assert runtime_profile.main([]) == 0
    payload = json.loads(capsys.readouterr().out)

    assert payload["status"] == "ok"
    assert payload["provenance"] == "effective process settings; not a production observation"
    assert payload["captured_at_utc"].endswith("Z")
    assert payload["boolean_settings"] == {"disabled_flag": False, "safe_flag": True}
    assert payload["kria_minimum_client_protocol"] == 7
    assert type(payload["kria_minimum_client_protocol"]) is int
    assert payload["phone_render_verified_features"] == ["audioMix"]
    assert "scalar_secret" not in payload["boolean_settings"]


def test_configuration_failure_is_sanitized(monkeypatch, capsys) -> None:
    monkeypatch.setattr(
        runtime_profile,
        "_profile_payload",
        lambda: (_ for _ in ()).throw(ValueError("DATABASE_URL=postgres://secret")),
    )

    assert runtime_profile.main([]) == 1
    output = capsys.readouterr()
    payload = json.loads(output.out)

    assert payload["status"] == "configuration_unavailable"
    assert "secret" not in output.out.lower()
    assert "traceback" not in output.err.lower()


def test_unsafe_protocol_type_fails_closed(monkeypatch, capsys) -> None:
    _install_fake_config(monkeypatch, features=[])
    sys.modules["app.config"].settings.kria_minimum_client_protocol = "7"

    assert runtime_profile.main([]) == 1
    assert json.loads(capsys.readouterr().out)["status"] == "configuration_unavailable"
