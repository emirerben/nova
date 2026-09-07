from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path

import pytest

SCRIPT = Path(__file__).parents[2] / "scripts" / "replay_creator.py"
SPEC = importlib.util.spec_from_file_location("replay_creator", SCRIPT)
assert SPEC and SPEC.loader
replay_creator = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = replay_creator
SPEC.loader.exec_module(replay_creator)


def _manifest(tmp_path: Path, *, count: int = 40) -> tuple[Path, Path]:
    sources = []
    for index in range(count):
        kind = "audio" if index == count - 1 else ("image" if index >= count - 20 else "video")
        suffix = {"video": ".mp4", "image": ".jpg", "audio": ".mp3"}[kind]
        path = tmp_path / f"source-{index}{suffix}"
        path.write_bytes(f"source-{index}".encode())
        sources.append(
            {
                "id": f"source-{index}{suffix}" if kind == "video" else f"source-{index}",
                "kind": kind,
                "local_path": str(path),
                "size": path.stat().st_size,
                "sha256": replay_creator.sha256_bytes(path.read_bytes()),
                "generation": str(index),
            }
        )
    manifest = tmp_path / "source-manifest.json"
    manifest.write_text(json.dumps({"sources": sources, "creator_request": "Use this footage."}))
    return manifest, tmp_path


def test_local_database_boundary_rejects_production_destination() -> None:
    with pytest.raises(replay_creator.ReplaySafetyError, match="non-local replay database"):
        replay_creator.validate_local_boundary(
            database_url="postgresql://postgres@nova-video.fly.dev:5432/nova",
            storage_provider="local",
            e2e_fixtures=True,
            destination_root=Path("/private/tmp/replay-run"),
            source_root=Path("/private/tmp/source-input"),
        )


@pytest.mark.parametrize("redis_url", ["redis://localhost:6379", "redis://localhost:6379/0"])
def test_replay_redis_is_pinned_to_local_database_14(redis_url: str) -> None:
    with pytest.raises(replay_creator.ReplaySafetyError, match="database 14"):
        replay_creator._redis_target(redis_url)


@pytest.mark.parametrize(
    ("provider", "fixtures", "message"),
    [("gcs", True, "STORAGE_PROVIDER"), ("local", False, "E2E_FIXTURES")],
)
def test_local_storage_boundary_requires_fixture_mode(
    provider: str, fixtures: bool, message: str
) -> None:
    with pytest.raises(replay_creator.ReplaySafetyError, match=message):
        replay_creator.validate_local_boundary(
            database_url="postgresql://postgres@localhost:5432/nova_creator_fidelity_test",
            storage_provider=provider,
            e2e_fixtures=fixtures,
            destination_root=Path("/private/tmp/replay-run"),
            source_root=Path("/private/tmp/source-input"),
        )


def test_destination_cannot_overlap_source_directory() -> None:
    with pytest.raises(replay_creator.ReplaySafetyError, match="separate from the source"):
        replay_creator.validate_local_boundary(
            database_url="postgresql://postgres@localhost:5432/nova_creator_fidelity_test",
            storage_provider="local",
            e2e_fixtures=True,
            destination_root=Path("/private/tmp/source-input/replay"),
            source_root=Path("/private/tmp/source-input"),
        )


def test_load_inputs_verifies_pinned_hashes_and_maps_cached_selected_clips(tmp_path: Path) -> None:
    manifest_path, source_root = _manifest(tmp_path)
    job_debug_path = tmp_path / "job.json"
    job_debug_path.write_text(
        json.dumps(
            {
                "job": {
                    "all_candidates": {
                        "creator_strategy": {
                            "selected_media_ids": ["source-0.mp4", "source-1.mp4"]
                        },
                        "clip_metadata_cache": {
                            "version": 1,
                            "clip_metas": [
                                {"detected_subject": "player one", "transcript": "score"},
                                {"detected_subject": "player two", "transcript": "goal"},
                            ],
                        },
                    }
                },
                "agent_runs": [
                    {
                        "agent_name": "nova.compose.narrated_storyboard",
                        "input_json": {
                            "words": [
                                {"word": "exact", "start_s": 0.0, "end_s": 0.4},
                                {"word": "words", "start_s": 0.4, "end_s": 0.8},
                            ]
                        },
                    }
                ],
            }
        )
    )
    item_debug_path = tmp_path / "item.json"
    item_debug_path.write_text(json.dumps({"pool_assets": []}))

    loaded = replay_creator.load_inputs(
        manifest_path=manifest_path,
        job_debug_path=job_debug_path,
        item_debug_path=item_debug_path,
    )

    assert len(loaded.sources) == 40
    assert loaded.source_identity_hash == replay_creator.sha256_json(
        [
            {
                key: source[key]
                for key in ("id", "kind", "size", "sha256", "generation")
                if key in source
            }
            for source in loaded.sources
        ]
    )
    assert loaded.clip_analysis_cache["source-0.mp4"]["subject"] == "player one"
    assert loaded.clip_analysis_cache["source-1.mp4"]["subject"] == "player two"
    assert loaded.transcript_cache["available"] is True
    assert loaded.transcript_cache["value"]["words"][0]["word"] == "exact"
    assert source_root.joinpath("source-0.mp4").read_bytes() == b"source-0"


def test_load_inputs_rejects_changed_source_bytes(tmp_path: Path) -> None:
    manifest_path, _ = _manifest(tmp_path)
    source = tmp_path / "source-0.mp4"
    source.write_bytes(b"changed")
    with pytest.raises(replay_creator.ReplaySafetyError, match="SHA-256 mismatch"):
        replay_creator.load_inputs(
            manifest_path=manifest_path,
            job_debug_path=tmp_path / "missing-job.json",
            item_debug_path=tmp_path / "missing-item.json",
        )


def test_render_is_explicit_and_requires_fidelity(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    parser = replay_creator.build_parser()
    args = parser.parse_args([])
    assert args.render is False
    assert args.enable_fidelity is False

    monkeypatch.setenv(
        "DATABASE_URL", "postgresql://postgres@localhost:5432/nova_creator_fidelity_test"
    )
    with pytest.raises(replay_creator.ReplaySafetyError, match="requires --enable-fidelity"):
        replay_creator._configure_environment(
            parser.parse_args(["--render"]),
            output_dir=Path("/private/tmp/nova-creator-fidelity-test-run"),
        )


def test_output_receipt_hashes_only_local_worker_objects(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = tmp_path / "storage"
    root.mkdir()
    monkeypatch.setattr(replay_creator, "Path", Path)

    class FakeStorage:
        def object_metadata(self, path: str):
            return type("Metadata", (), {"generation": "7", "size": 5})()

        def local_object_path(self, path: str) -> Path:
            return root / path

    output = root / "generative-jobs" / "run" / "video.mp4"
    output.parent.mkdir(parents=True)
    output.write_bytes(b"ready")
    snapshot = {
        "job": {
            "assembly_plan": {
                "variants": [
                    {"variant_id": "narrated", "output_url": "generative-jobs/run/video.mp4"}
                ]
            }
        }
    }
    receipts = replay_creator._output_receipts(snapshot, FakeStorage(), {"users/run/source.mp4"})
    assert receipts[0]["sha256"] == replay_creator.sha256_bytes(b"ready")
    assert receipts[0]["generation"] == "7"


def test_planning_and_confirmation_share_event_loop(monkeypatch, tmp_path):
    import asyncio
    from types import SimpleNamespace

    monkeypatch.setattr(replay_creator, "_configure_environment", lambda *a, **k: {})
    monkeypatch.setattr(
        replay_creator,
        "load_inputs",
        lambda **k: SimpleNamespace(
            manifest_hash="manifest",
            source_identity_hash="sources",
            sources=[],
            seed_description="Use all footage",
            clip_analysis_cache={},
            transcript_cache={},
        ),
    )
    monkeypatch.setattr(replay_creator, "seed_replay_database", lambda **k: {})
    monkeypatch.setattr(replay_creator, "analyze_replay_media", lambda **k: {})
    planning_loop = None

    async def plan(**kwargs):
        nonlocal planning_loop
        planning_loop = asyncio.get_running_loop()
        return {"id": "00000000-0000-0000-0000-000000000001", "pending_plan": {}}

    async def confirm(**kwargs):
        assert asyncio.get_running_loop() is planning_loop
        assert not planning_loop.is_closed()
        return kwargs["plan"]

    monkeypatch.setattr(replay_creator, "run_creator_planning", plan)
    monkeypatch.setattr(replay_creator, "confirm_creator_plan", confirm)
    monkeypatch.setattr(replay_creator, "wait_for_worker", lambda **k: {"job": {"status": "done"}})
    assert (
        replay_creator.main(["--render", "--enable-fidelity", "--output-dir", str(tmp_path)]) == 0
    )
    assert planning_loop.is_closed()
