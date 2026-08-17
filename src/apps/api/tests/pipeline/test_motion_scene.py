from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest

from app.pipeline.motion_scene import (
    MOTION_MAX_ACTIVE_FRAMES,
    MOTION_MAX_COMPLEXITY_UNITS,
    MOTION_RUNTIME_HASH,
    MotionSceneError,
    _normalize_motion_asset,
    _render_sequence,
    _runtime_candidates,
    _runtime_root,
    apply_motion_scenes,
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


def _evolving_scene(**overrides) -> dict:
    value = {
        "id": "evolving-1",
        "preset_id": "evolving_type",
        "preset_version": 2,
        "start_frame": 0,
        "end_frame_exclusive": 159,
        "palette": {"primary": "#000000", "accent": "#ffffff"},
        "intensity": 0.72,
        "params": {
            "headline": "EVOLVE THE IDEA",
            "subtitle": "Shape, split, and settle into focus",
            "icon_count": 4,
            "icon_style": "organic",
            "text_stagger_ms": 45,
            "icon_stagger_ms": 70,
            "morph_amplitude": 0.65,
            "density": "medium",
            "layout": "compact",
            "order": "forward",
            "typography_scale": 1,
            "backdrop_opacity": 0.7,
            "split_icons": True,
        },
        "motion": {
            "version": 2,
            "speed": 1,
            "easing": "ease-in-out-cubic",
            "hold_frames": 30,
        },
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
    assert MOTION_RUNTIME_HASH.startswith("motion-v4:ck0.40.0:")


def test_motion_contract_accepts_evolving_type_v2_with_reference_defaults() -> None:
    validated = validate_motion_instances([_evolving_scene()], duration_frames=159)

    assert validated[0]["end_frame_exclusive"] == 159
    assert validated[0]["params"]["icon_count"] == 4
    assert validated[0]["params"]["text_stagger_ms"] == 45
    assert validated[0]["params"]["icon_stagger_ms"] == 70
    assert validated[0]["params"]["morph_amplitude"] == 0.65
    assert validated[0]["palette"] == {"primary": "#000000", "accent": "#FFFFFF"}


@pytest.mark.parametrize(
    ("field", "value", "path"),
    [
        ("speed", 1.013, "motion.speed"),
        ("morph_amplitude", 0.673, "params.morph_amplitude"),
    ],
)
def test_motion_contract_matches_ts_decimal_step_validation(
    field: str,
    value: float,
    path: str,
) -> None:
    scene = _evolving_scene()
    target = scene["motion"] if field == "speed" else scene["params"]
    target[field] = value

    with pytest.raises(ValueError, match=rf"{path}: .* does not align to step"):
        validate_motion_instances([scene], duration_frames=159)


def test_motion_contract_fails_closed_for_unknown_v2_controls() -> None:
    scene = _evolving_scene()
    scene["motion"] = {**scene["motion"], "shader_source": "void main() {}"}

    with pytest.raises(ValueError, match="not valid under any of the given schemas"):
        validate_motion_instances([scene], duration_frames=159)


@pytest.mark.parametrize(
    ("patch", "message"),
    [
        ({"end_frame_exclusive": 0}, "minimum of 1"),
        ({"end_frame_exclusive": 60, "start_frame": 60}, "greater than start_frame"),
        ({"preset_id": "raw_svg"}, "not valid under any of the given schemas"),
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


def test_motion_contract_uses_exclusive_end_and_bounded_active_union() -> None:
    with pytest.raises(ValueError, match="exceeds the video duration"):
        validate_motion_instances([_scene(end_frame_exclusive=61)], duration_frames=60)
    separated = [
        _scene(id="first", start_frame=0, end_frame_exclusive=30),
        _scene(id="last", start_frame=300, end_frame_exclusive=330),
    ]
    assert len(validate_motion_instances(separated)) == 2
    with pytest.raises(ValueError, match=f"maximum is {MOTION_MAX_ACTIVE_FRAMES}"):
        validate_motion_instances(
            [
                _scene(id="first", start_frame=0, end_frame_exclusive=MOTION_MAX_ACTIVE_FRAMES),
                _scene(id="last", start_frame=300, end_frame_exclusive=301),
            ]
        )


def test_motion_contract_weighted_complexity_accepts_boundary_and_rejects_overlap() -> None:
    evolving = _evolving_scene(end_frame_exclusive=MOTION_MAX_ACTIVE_FRAMES)
    validated = validate_motion_instances([evolving])
    assert len(validated) == 1
    assert MOTION_MAX_COMPLEXITY_UNITS == MOTION_MAX_ACTIVE_FRAMES * 4

    overlapping_legacy = _scene(
        id="legacy-overlap",
        start_frame=0,
        end_frame_exclusive=MOTION_MAX_ACTIVE_FRAMES,
    )
    with pytest.raises(ValueError, match="weighted complexity units"):
        validate_motion_instances([evolving, overlapping_legacy])


def test_motion_contract_back_to_back_weighted_scenes_do_not_overlap() -> None:
    first = _evolving_scene(end_frame_exclusive=120)
    second = _evolving_scene(
        id="evolving-2",
        start_frame=120,
        end_frame_exclusive=240,
    )
    assert len(validate_motion_instances([first, second])) == 2


def test_motion_contract_accepts_creator_text_and_media_params() -> None:
    text = _scene(
        preset_id="kinetic_word",
        end_frame_exclusive=75,
        params={"text": "MAKE IT WILD"},
    )
    media = _scene(
        id="cards",
        preset_id="card_stack",
        end_frame_exclusive=120,
        params={
            "assets": [
                {"asset_id": "a", "gcs_path": "users/u/plan/pool/a.png"},
                {"asset_id": "b", "gcs_path": "users/u/plan/pool/b.png"},
            ]
        },
    )
    assert validate_motion_instances([text]) == [
        text | {"palette": {"primary": "#8B5CF6", "accent": "#D9FF43"}}
    ]
    assert validate_motion_instances([media])


def test_motion_contract_rejects_duplicate_ids() -> None:
    with pytest.raises(ValueError, match="id must be unique"):
        validate_motion_instances([_scene(), _scene()])


def test_python_and_typescript_runtime_hashes_are_locked_together() -> None:
    contract = (
        Path(__file__).resolve().parents[4] / "packages" / "motion-runtime" / "src" / "contract.ts"
    ).read_text()
    assert f'"{MOTION_RUNTIME_HASH}"' in contract


def test_runtime_candidates_support_shallow_production_module_path() -> None:
    candidates = _runtime_candidates(Path("/app/app/pipeline/motion_scene.py"))

    assert candidates[0] == Path("/app/motion-runtime")
    assert Path("/app/packages/motion-runtime") in candidates


def test_runtime_root_discovers_local_package_from_any_source_depth(
    monkeypatch,
    tmp_path,
) -> None:
    module_file = tmp_path / "nested" / "app" / "pipeline" / "motion_scene.py"
    runtime = tmp_path / "packages" / "motion-runtime"
    runtime.mkdir(parents=True)
    (runtime / "motion-scene.schema.json").write_text("{}")
    monkeypatch.setattr(
        "app.pipeline.motion_scene._runtime_candidates",
        lambda _module_file: _runtime_candidates(module_file),
    )

    assert _runtime_root() == runtime


def test_runtime_root_fails_explicitly_when_package_is_missing(
    monkeypatch,
    tmp_path,
) -> None:
    monkeypatch.setattr(
        "app.pipeline.motion_scene._runtime_candidates",
        lambda _module_file: (tmp_path / "missing-runtime",),
    )

    with pytest.raises(MotionSceneError, match="motion runtime package is missing"):
        _runtime_root()


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
            {
                "stdout": (
                    f'{{"runtime_hash":"{MOTION_RUNTIME_HASH}",'
                    '"segments":[{"start_frame":0,"end_frame_exclusive":60}],'
                    '"frame_count":60}\n'
                ).encode()
            },
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


def test_deno_renderer_materializes_exact_media_generations(monkeypatch, tmp_path) -> None:
    downloads: list[tuple[str, str]] = []
    refs = [
        {"asset_id": "image-1", "gcs_path": "users/u/plan/i/pool/one.png"},
        {"asset_id": "image-2", "gcs_path": "users/u/plan/i/pool/two.png"},
    ]
    media_scene = {
        **_scene(preset_id="card_stack"),
        "params": {"assets": refs},
    }

    def download(path, local_path, *, generation):
        downloads.append((path, generation))
        Path(local_path).write_bytes(b"encoded-image")

    def fake_run(cmd: list[str], *, label: str, env=None):
        del cmd, label, env
        return SimpleNamespace(
            stdout=(
                f'{{"runtime_hash":"{MOTION_RUNTIME_HASH}",'
                '"segments":[{"start_frame":0,"end_frame_exclusive":60}],'
                '"frame_count":60}\n'
            ).encode()
        )

    monkeypatch.setenv("DENO_DIR", "/tmp/deno-cache")
    monkeypatch.setattr("app.pipeline.motion_scene.shutil.which", lambda name: f"/usr/bin/{name}")
    monkeypatch.setattr(
        "app.pipeline.motion_scene.storage.download_generation_to_file",
        download,
    )
    monkeypatch.setattr(
        "app.pipeline.motion_scene._normalize_motion_asset",
        lambda path, **_kwargs: path,
    )
    monkeypatch.setattr("app.pipeline.motion_scene._run", fake_run)

    _render_sequence(
        [media_scene],
        width=1080,
        height=1920,
        tmpdir=str(tmp_path),
        asset_generations={"image-1": "11", "image-2": "22"},
    )

    assert downloads == [
        ("users/u/plan/i/pool/one.png", "11"),
        ("users/u/plan/i/pool/two.png", "22"),
    ]


def test_deno_renderer_accepts_prepared_benchmark_assets(monkeypatch, tmp_path) -> None:
    prepared_root = tmp_path / "prepared"
    prepared_root.mkdir()
    prepared = prepared_root / "image.png"
    prepared.write_bytes(b"normalized-png")
    scene = {
        **_scene(preset_id="film_strip", preset_version=2, end_frame_exclusive=36),
        "params": {
            "assets": [
                {
                    "asset_id": "image-1",
                    "gcs_path": "users/u/plan/i/pool/one.png",
                }
            ]
        },
        "motion": {
            "version": 2,
            "speed": 4,
            "easing": "ease-in-out-cubic",
            "hold_frames": 0,
        },
    }
    calls: list[list[str]] = []

    def fake_run(cmd: list[str], *, label: str, env=None):
        del label, env
        calls.append(cmd)
        return SimpleNamespace(
            stdout=(
                f'{{"runtime_hash":"{MOTION_RUNTIME_HASH}",'
                '"segments":[{"start_frame":0,"end_frame_exclusive":36}],'
                '"frame_count":36}\n'
            ).encode()
        )

    monkeypatch.setenv("DENO_DIR", "/tmp/deno-cache")
    monkeypatch.setattr("app.pipeline.motion_scene.shutil.which", lambda name: f"/usr/bin/{name}")
    monkeypatch.setattr(
        "app.pipeline.motion_scene.storage.download_to_file",
        lambda *_args, **_kwargs: pytest.fail("prepared assets must not hit storage"),
    )
    monkeypatch.setattr(
        "app.pipeline.motion_scene._normalize_motion_asset",
        lambda *_args, **_kwargs: pytest.fail("prepared assets are already normalized"),
    )
    monkeypatch.setattr("app.pipeline.motion_scene._run", fake_run)
    render_root = tmp_path / "render"
    render_root.mkdir()

    _render_sequence(
        [scene],
        width=1080,
        height=1920,
        tmpdir=str(render_root),
        prepared_asset_paths={"image-1": str(prepared)},
    )

    render_cmd = calls[-1]
    read_flag = next(part for part in render_cmd if part.startswith("--allow-read"))
    assert str(prepared_root) in read_flag


def test_motion_image_resource_is_probed_and_normalized_before_canvaskit(
    monkeypatch, tmp_path
) -> None:
    source = tmp_path / "source.jpg"
    source.write_bytes(b"encoded-image")

    def fake_run(cmd: list[str], *, label: str, env=None):
        del env
        if label == "Creator Block image probe":
            return SimpleNamespace(
                stdout=b'{"streams":[{"codec_type":"video","width":4000,"height":3000}]}'
            )
        assert label == "Creator Block image normalization"
        Path(cmd[-1]).write_bytes(b"normalized-png")
        return SimpleNamespace(stdout=b"")

    monkeypatch.setattr("app.pipeline.motion_scene._run", fake_run)
    normalized = _normalize_motion_asset(str(source), index=0, tmpdir=str(tmp_path))
    assert Path(normalized).read_bytes() == b"normalized-png"


def test_motion_image_resource_rejects_decompression_bomb_dimensions(monkeypatch, tmp_path) -> None:
    source = tmp_path / "source.jpg"
    source.write_bytes(b"tiny-encoded-image")
    monkeypatch.setattr(
        "app.pipeline.motion_scene._run",
        lambda *_args, **_kwargs: SimpleNamespace(
            stdout=b'{"streams":[{"codec_type":"video","width":20000,"height":20000}]}'
        ),
    )
    with pytest.raises(MotionSceneError, match="dimensions are invalid"):
        _normalize_motion_asset(str(source), index=0, tmpdir=str(tmp_path))


def test_motion_image_resource_rejects_video_disguised_as_image(monkeypatch, tmp_path) -> None:
    source = tmp_path / "spoofed.jpg"
    source.write_bytes(b"mp4-bytes-with-image-extension")
    monkeypatch.setattr(
        "app.pipeline.motion_scene._run",
        lambda *_args, **_kwargs: SimpleNamespace(
            stdout=(
                b'{"streams":[{"codec_type":"video","width":1080,"height":1920,'
                b'"nb_frames":"90","duration":"3.0"}],"format":{"duration":"3.0"}}'
            )
        ),
    )
    with pytest.raises(MotionSceneError, match="not a still image"):
        _normalize_motion_asset(str(source), index=0, tmpdir=str(tmp_path))


def test_sparse_segments_composite_at_exact_offsets_with_final_encoder_policy(
    monkeypatch,
) -> None:
    commands: list[list[str]] = []
    uploaded: list[tuple[str, str]] = []
    generation_downloads: list[tuple[str, str]] = []
    monkeypatch.setattr("app.pipeline.motion_scene.storage.download_to_file", lambda *_: None)
    monkeypatch.setattr(
        "app.pipeline.motion_scene.storage.download_generation_to_file",
        lambda path, _local, *, generation: generation_downloads.append((path, generation)),
    )
    monkeypatch.setattr(
        "app.pipeline.motion_scene.storage.upload_public_read",
        lambda *args: uploaded.append(args),
    )
    monkeypatch.setattr("app.pipeline.motion_scene._probe_dimensions", lambda _path: (1920, 1080))
    monkeypatch.setattr(
        "app.pipeline.motion_scene._render_sequence",
        lambda *_args, **kwargs: (
            str(Path(kwargs["tmpdir"]) / "frames"),
            [
                {"start_frame": 0, "end_frame_exclusive": 30},
                {"start_frame": 300, "end_frame_exclusive": 330},
            ],
            60,
        ),
    )
    monkeypatch.setattr(
        "app.pipeline.motion_scene._run",
        lambda cmd, **_kwargs: commands.append(cmd),
    )

    apply_motion_scenes(
        base_gcs_path="generative-jobs/job/base.mp4",
        instances=[
            _scene(id="first", start_frame=0, end_frame_exclusive=30),
            _scene(id="last", start_frame=300, end_frame_exclusive=330),
        ],
        output_gcs_path="generative-jobs/job/motion.mp4",
        job_id="job",
        source_generation="source-generation-7",
    )

    command = commands[0]
    filters = command[command.index("-filter_complex") + 1]
    assert "setpts=PTS+0.000000000/TB" in filters
    assert "setpts=PTS+10.000000000/TB" in filters
    assert "repeatlast=0" in filters
    assert command[command.index("-preset") + 1] == "fast"
    assert uploaded and uploaded[0][1] == "generative-jobs/job/motion.mp4"
    assert generation_downloads == [("generative-jobs/job/base.mp4", "source-generation-7")]
