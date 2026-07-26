from __future__ import annotations

from pathlib import Path

import pytest

from app.pipeline.motion_scene import (
    MOTION_MAX_ACTIVE_FRAMES,
    MOTION_RUNTIME_HASH,
    _render_sequence,
    validate_motion_instances,
)


def _scene(**overrides) -> dict:
    value = {
        "id": "route-1",
        "preset_id": "route_trace",
        "preset_version": 1,
        "start_frame": 0,
        "end_frame_exclusive": 60,
        "palette": {"primary": "#8b5cf6", "accent": "#d9ff43"},
        "intensity": 0.8,
    }
    value.update(overrides)
    return value


def test_motion_contract_accepts_bounded_preset_and_normalizes_colors() -> None:
    validated = validate_motion_instances([_scene()], duration_frames=90)
    assert validated == [
        {
            **_scene(),
            "palette": {"primary": "#8B5CF6", "accent": "#D9FF43"},
        }
    ]
    assert MOTION_RUNTIME_HASH.startswith("motion-v1:ck0.40.0:")


@pytest.mark.parametrize(
    ("patch", "message"),
    [
        ({"end_frame_exclusive": 0}, "minimum of 1"),
        ({"end_frame_exclusive": 60, "start_frame": 60}, "greater than start_frame"),
        ({"preset_id": "raw_svg"}, "'route_trace' was expected"),
        ({"intensity": 1.1}, "maximum of 1"),
        ({"palette": {"primary": "red", "accent": "#FFFFFF"}}, "does not match"),
    ],
)
def test_motion_contract_rejects_unbounded_or_executable_input(
    patch: dict,
    message: str,
) -> None:
    with pytest.raises(ValueError, match=message):
        validate_motion_instances([_scene(**patch)], duration_frames=120)


def test_motion_contract_uses_exclusive_end_and_bounded_span() -> None:
    with pytest.raises(ValueError, match="exceeds the video duration"):
        validate_motion_instances([_scene(end_frame_exclusive=61)], duration_frames=60)
    with pytest.raises(ValueError, match=f"maximum is {MOTION_MAX_ACTIVE_FRAMES}"):
        validate_motion_instances(
            [
                _scene(id="first", start_frame=0, end_frame_exclusive=30),
                _scene(
                    id="last",
                    start_frame=MOTION_MAX_ACTIVE_FRAMES,
                    end_frame_exclusive=MOTION_MAX_ACTIVE_FRAMES + 1,
                ),
            ]
        )


def test_motion_contract_rejects_duplicate_ids() -> None:
    with pytest.raises(ValueError, match="id must be unique"):
        validate_motion_instances([_scene(), _scene()])


def test_python_and_typescript_runtime_hashes_are_locked_together() -> None:
    contract = (
        Path(__file__).resolve().parents[4] / "packages" / "motion-runtime" / "src" / "contract.ts"
    ).read_text()
    assert f'"{MOTION_RUNTIME_HASH}"' in contract


def test_deno_renderer_discovers_cache_without_broad_read_permission(
    monkeypatch,
    tmp_path,
) -> None:
    calls: list[list[str]] = []

    def fake_run(cmd: list[str], *, label: str, env=None):
        del label, env
        calls.append(cmd)
        if cmd[1:3] == ["info", "--json"]:
            return type("Result", (), {"stdout": b'{"denoDir":"/tmp/deno-cache"}'})()
        return type(
            "Result",
            (),
            {"stdout": f'{{"runtime_hash":"{MOTION_RUNTIME_HASH}"}}\n'.encode()},
        )()

    monkeypatch.delenv("DENO_DIR", raising=False)
    monkeypatch.setattr("app.pipeline.motion_scene.shutil.which", lambda name: f"/usr/bin/{name}")
    monkeypatch.setattr("app.pipeline.motion_scene._run", fake_run)

    _render_sequence([_scene()], width=1080, height=1920, tmpdir=str(tmp_path))

    render_cmd = calls[1]
    assert "--node-modules-dir=none" in render_cmd
    read_flag = next(part for part in render_cmd if part.startswith("--allow-read"))
    assert read_flag.startswith("--allow-read=")
    assert str(tmp_path) in read_flag
    assert "/tmp/deno-cache" in read_flag
