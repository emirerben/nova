from types import SimpleNamespace

import pytest

from app.schemas.edit_proposal import MediaRef
from app.services import creator_clip_analysis as service
from app.tasks.autoplace import ANALYSIS_VERSION


def _analysis(*, source: str = "clip_metadata", subject: str = "coast", version: int | None = None):
    return {
        "source": source,
        "subject": subject,
        "analysis_version": ANALYSIS_VERSION if version is None else version,
    }


def test_clip_analysis_ready_requires_semantic_non_probe_analysis() -> None:
    base = {"generation": "42"}
    assert service.clip_analysis_ready({**base, "analysis": _analysis()})
    assert not service.clip_analysis_ready({**base, "analysis": _analysis(source="probe_only")})
    assert not service.clip_analysis_ready({**base, "analysis": _analysis(source="stub")})
    assert not service.clip_analysis_ready({**base, "analysis": _analysis(subject="")})
    assert not service.clip_analysis_ready({**base, "analysis": {}})
    assert not service.clip_analysis_ready({"analysis": _analysis()})


def test_clip_analysis_ready_uses_image_freshness_floor() -> None:
    assert service.clip_analysis_ready(
        {"generation": "42", "analysis": _analysis(version=6)}, kind="image"
    )
    assert not service.clip_analysis_ready(
        {"generation": "42", "analysis": _analysis(version=5)}, kind="image"
    )


def test_assignment_downloads_registered_generation_and_passes_context(monkeypatch) -> None:
    calls: list[tuple] = []
    context = object()

    monkeypatch.setattr(
        "app.storage.object_metadata",
        lambda _path: SimpleNamespace(content_type="video/mp4", generation="42"),
    )
    monkeypatch.setattr(
        "app.storage.download_generation_to_file",
        lambda path, local, *, generation: calls.append((path, local, generation)),
    )

    def analyze(local_path, *, run_context):
        calls.append(("analyze", local_path, run_context))
        return _analysis(), 1.5, 4.0, (720, 1280)

    monkeypatch.setattr("app.tasks.autoplace.analyze_pool_video", analyze)

    entry, ref = service.analyze_clip_assignment(
        {
            "media_id": "clip-1",
            "gcs_path": "users/u/clip.mp4",
            "storage_generation": "42",
        },
        {},
        run_context=context,
    )

    assert calls[0][2] == "42"
    assert calls[1][2] is context
    assert entry["generation"] == "42"
    assert ref.generation == "42"


def test_assignment_rejects_replaced_registered_source(monkeypatch) -> None:
    monkeypatch.setattr(
        "app.storage.object_metadata",
        lambda _path: SimpleNamespace(content_type="video/mp4", generation="new"),
    )

    with pytest.raises(ValueError, match="generation changed"):
        service.analyze_clip_assignment(
            {
                "media_id": "clip-1",
                "gcs_path": "users/u/clip.mp4",
                "storage_generation": "old",
            },
            {},
        )


def test_pool_reuse_requires_exact_current_generation(monkeypatch) -> None:
    monkeypatch.setattr(
        "app.storage.object_metadata",
        lambda _path: SimpleNamespace(content_type="video/mp4", generation="42"),
    )
    pooled = MediaRef(
        lane="asset",
        media_id="asset-1",
        gcs_path="users/u/clip.mp4",
        generation="41",
        kind="video",
        duration_s=4,
        aspect=1.5,
        analysis=_analysis(),
    )
    monkeypatch.setattr("app.storage.download_generation_to_file", lambda *_a, **_kw: None)
    monkeypatch.setattr(
        "app.tasks.autoplace.analyze_pool_video",
        lambda _local: (_analysis(subject="fresh"), 1.5, 4.0, (720, 1280)),
    )

    _entry, ref = service.analyze_clip_assignment(
        {"media_id": "clip-1", "gcs_path": pooled.gcs_path},
        {pooled.gcs_path: pooled},
    )

    assert ref.media_id == "clip-1"
    assert ref.generation == "42"
    assert ref.analysis["subject"] == "fresh"


def test_require_semantic_forces_probe_only_cache_miss(monkeypatch) -> None:
    calls: list[str] = []
    monkeypatch.setattr(
        "app.storage.object_metadata",
        lambda _path: SimpleNamespace(content_type="video/mp4", generation="42"),
    )
    monkeypatch.setattr("app.storage.download_generation_to_file", lambda *_a, **_kw: None)

    def analyze(local_path):
        calls.append(local_path)
        return None, 1.5, 4.0, (720, 1280)

    monkeypatch.setattr("app.tasks.autoplace.analyze_pool_video", analyze)
    raw = {
        "media_id": "clip-1",
        "gcs_path": "users/u/clip.mp4",
        "generation": "42",
        "analysis": {"source": "probe_only", "analysis_version": ANALYSIS_VERSION},
    }

    service.analyze_clip_assignment(raw, {}, require_semantic=True)
    assert len(calls) == 1
