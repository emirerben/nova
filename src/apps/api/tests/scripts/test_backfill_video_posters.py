from __future__ import annotations

import uuid
from datetime import UTC, datetime
from types import SimpleNamespace

from scripts.backfill_video_posters import (
    _candidates_for_job,
    _generate_poster,
    _parse_cursor,
)


def _job() -> SimpleNamespace:
    return SimpleNamespace(
        id=uuid.uuid4(),
        user_id=uuid.uuid4(),
        status="variants_ready",
        created_at=datetime(2026, 8, 25, tzinfo=UTC),
        assembly_plan={
            "variants": [
                {
                    "variant_id": "song_text",
                    "render_status": "ready",
                    "video_path": "generative-jobs/PLACEHOLDER/video.mp4",
                }
            ]
        },
    )


def test_cursor_round_trip_is_keyset_safe() -> None:
    created_at = datetime(2026, 8, 25, 12, 30, tzinfo=UTC)
    job_id = uuid.uuid4()
    assert _parse_cursor(f"{created_at.isoformat()}|{job_id}") == (created_at, job_id)


def test_candidate_discovery_skips_unrequested_preview_bases() -> None:
    job = _job()
    job.assembly_plan["variants"][0]["video_path"] = f"generative-jobs/{job.id}/video.mp4"
    clips = [
        SimpleNamespace(
            id=uuid.uuid4(),
            job_id=job.id,
            rank=1,
            created_at=job.created_at,
            render_status="ready",
            video_path=f"auto-music-jobs/{job.id}/variant.mp4",
            thumbnail_path=None,
        )
    ]

    candidates = list(_candidates_for_job(job, clips, include_preview_bases=False))

    assert [candidate.kind for candidate in candidates] == ["variant", "job_clip"]
    assert candidates[0].source_key == f"generative-jobs/{job.id}/video.mp4"


def test_generate_poster_uses_deterministic_sibling(monkeypatch) -> None:
    calls: dict[str, object] = {}
    monkeypatch.setattr(
        "scripts.backfill_video_posters.download_to_file",
        lambda source, local: calls.update(source=source, local=local),
    )
    monkeypatch.setattr(
        "scripts.backfill_video_posters.extract_poster_bytes",
        lambda local: b"jpeg",
    )
    monkeypatch.setattr(
        "scripts.backfill_video_posters.upload_bytes_public_read",
        lambda data, path, content_type: calls.update(
            data=data, path=path, content_type=content_type
        ),
    )

    poster, outcome = _generate_poster(f"jobs/{uuid.uuid4()}/output.mp4")

    assert outcome == "generated"
    assert poster and poster.endswith("output.mp4.poster.jpg")
    assert calls["data"] == b"jpeg"
    assert calls["content_type"] == "image/jpeg"
