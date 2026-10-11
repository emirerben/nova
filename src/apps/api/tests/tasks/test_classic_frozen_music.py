from __future__ import annotations

from contextlib import contextmanager
from types import SimpleNamespace

import pytest

import app.tasks.generative_build as gb
from app.services.creator_render_contract import CreatorRenderContractError


def _snapshot(licensed_music: dict | None) -> dict:
    from app.kria.brief_binding import BriefBinding
    from app.services.length_planning import build_length_plan

    media = {}
    if licensed_music is not None:
        media = {"clip_assignments": [{"media_id": "a", "duration_s": 40}]}
        media["length_plan"] = build_length_plan(
            {"selected_music_section": licensed_music},
            media,
        )
    binding = BriefBinding.create("thread", None, media_snapshot=media)
    return {"creator_brief_binding": binding.model_dump(mode="json")}


@contextmanager
def _catalog(track):
    yield SimpleNamespace(get=lambda _model, _track_id: track)


def _frozen_music() -> dict:
    return {
        "status": "selected",
        "track_id": "00000000-0000-0000-0000-000000000111",
        "title": "Approved song",
        "artist": "Approved artist",
        "start_s": 12.25,
        "end_s": 31.75,
        "catalog_duration_s": 60.0,
        "beat_timestamps_s": [0.0, 12.25, 20.0, 31.75],
    }


def test_selected_classic_music_clones_catalog_track_and_preserves_approved_window(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    catalog_track = SimpleNamespace(
        id="00000000-0000-0000-0000-000000000111",
        title="Live title",
        artist="Live artist",
        duration_s=90.0,
        beat_timestamps_s=[1.0],
        track_config={"best_start_s": 1.0, "best_end_s": 45.0},
        audio_gcs_path="music/00000000-0000-0000-0000-000000000111/audio.mp3",
    )
    monkeypatch.setattr(gb, "_sync_session", lambda: _catalog(catalog_track))

    status, track = gb._approved_licensed_music_track(_snapshot(_frozen_music()), job_id="j1")

    assert status == "selected"
    assert track is not catalog_track
    assert track.track_config["best_start_s"] == pytest.approx(12.25)
    assert track.track_config["best_end_s"] == pytest.approx(31.75)
    assert catalog_track.track_config == {"best_start_s": 1.0, "best_end_s": 45.0}
    assert catalog_track.beat_timestamps_s == [1.0]

    # Live rank-one sections and footage capacity cannot move an approved range.
    window = gb._effective_music_window(
        track, requested_start_s=0.0, requested_duration_s=2.0, fallback_footage_s=3.0
    )
    assert window["validated"] is True
    assert window["start_s"] == pytest.approx(12.25)
    assert window["end_s"] == pytest.approx(31.75)
    assert window["duration_s"] == pytest.approx(19.5)


def test_fallback_footage_suppresses_classic_music_resolution(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def unexpected_catalog_access():
        raise AssertionError("fallback footage must not read or match catalog music")

    monkeypatch.setattr(gb, "_sync_session", unexpected_catalog_access)

    assert gb._approved_licensed_music_track(
        _snapshot({"status": "fallback_footage"}), job_id="j1"
    ) == ("fallback_footage", None)


def test_legacy_snapshot_keeps_matcher_path() -> None:
    assert gb._approved_licensed_music_track(_snapshot(None), job_id="j1") == ("legacy", None)


def test_missing_approved_track_is_typed_creator_decline(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(gb, "_sync_session", lambda: _catalog(None))

    with pytest.raises(CreatorRenderContractError) as exc_info:
        gb._approved_licensed_music_track(_snapshot(_frozen_music()), job_id="j1")

    assert exc_info.value.decline_reason == "evidence_missing"
    assert exc_info.value.field_path == "licensed_music.track_id"


def test_approved_best_parts_decisions_accepts_the_real_source_identity_shape() -> None:
    from app.services.length_planning import LENGTH_PLAN_VERSION

    path = "users/u/source.mp4"
    decisions, receipt = gb._approved_best_parts_decisions(
        {
            "version": LENGTH_PLAN_VERSION,
            "sources": [
                {
                    "media_id": "media-1",
                    "gcs_path": path,
                    "storage_generation": "42",
                    "duration_s": 8.0,
                }
            ],
            "windows": [
                {
                    "media_id": "media-1",
                    "source_start_s": 1.0,
                    "source_end_s": 4.0,
                    "score": 0.9,
                    "reason": "approved",
                }
            ],
            "conflicts": [],
            "window_eligible": True,
        },
        clip_id_to_gcs={"clip-1": path},
        clip_id_to_local={"clip-1": "/tmp/source.mp4"},
        probe_map={"/tmp/source.mp4": SimpleNamespace(duration_s=8.0)},
        runtime_generations={path: "42"},
    )

    assert [(row.clip_id, row.in_s, row.out_s) for row in decisions] == [("clip-1", 1.0, 4.0)]
    assert receipt == [
        {
            "media_id": "media-1",
            "source_start_s": 1.0,
            "source_end_s": 4.0,
            "score": 0.9,
            "reason": "approved",
        }
    ]
