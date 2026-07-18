from __future__ import annotations

import types
import uuid

import pytest

import app.tasks.generative_build as gb
from app.agents.lyrics import LyricsExtractionAgent
from app.models import MusicTrack
from tests.tasks.conftest import FakeJob


class _Meta:
    def __init__(self, clip_id: str, hook_score: float = 5.0):
        self.clip_id = clip_id
        self.hook_score = hook_score
        self.best_moments = []
        self.transcript = ""
        self.detected_subject = ""
        self.hook_text = ""


def _lyrics_cached() -> dict:
    return {
        "prompt_version": LyricsExtractionAgent.spec.prompt_version,
        "source": "lrclib_synced+whisper",
        "language": "en",
        "lines": [
            {
                "text": "Hello bright world",
                "start_s": 0.0,
                "end_s": 2.0,
                "words": [
                    {"text": "Hello", "start_s": 0.0, "end_s": 0.6},
                    {"text": "bright", "start_s": 0.6, "end_s": 1.2},
                    {"text": "world", "start_s": 1.2, "end_s": 2.0},
                ],
            }
        ],
    }


def _track(track_id: str | uuid.UUID | None = None) -> types.SimpleNamespace:
    return types.SimpleNamespace(
        id=track_id or uuid.uuid4(),
        title="Song A",
        audio_gcs_path="music/t1/audio.m4a",
        beat_timestamps_s=[0.5, 1.0, 1.5, 2.0],
        duration_s=60.0,
        track_config={"best_start_s": 0.0, "best_end_s": 6.0},
        ai_labels={"labels": {}},
        analysis_status="ready",
        lyrics_cached=_lyrics_cached(),
    )


def _patch_render_helpers(monkeypatch: pytest.MonkeyPatch, mix_calls: list) -> None:
    import app.pipeline.agents.gemini_analyzer as ga
    import app.pipeline.template_matcher as tm
    import app.storage as storage
    import app.tasks.template_orchestrate as to

    monkeypatch.setattr(
        ga,
        "build_recipe",
        lambda d: types.SimpleNamespace(
            beat_timestamps_s=d.get("beat_timestamps_s", []), color_grade="none"
        ),
        raising=False,
    )
    monkeypatch.setattr(tm, "consolidate_slots", lambda recipe, metas: recipe, raising=False)
    monkeypatch.setattr(
        tm,
        "match",
        lambda recipe, metas, **kw: types.SimpleNamespace(steps=[]),
        raising=False,
    )
    monkeypatch.setattr(to, "_enrich_slots_with_energy", lambda slots, beats: slots, raising=False)
    monkeypatch.setattr(
        to,
        "_assemble_clips",
        lambda steps, c2l, probe, out_path, tmpdir, **kw: open(out_path, "wb").write(b"base"),
        raising=False,
    )
    monkeypatch.setattr(
        to,
        "_mix_template_audio",
        lambda *a, **k: mix_calls.append(a) or open(a[2], "wb").write(b"mixed"),
        raising=False,
    )
    monkeypatch.setattr(
        storage,
        "upload_public_read",
        lambda local, gcs: f"https://signed/{gcs}",
        raising=False,
    )


def _render_lyrics_variant(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path,
    *,
    lyrics_enabled: bool | None = None,
) -> dict:
    mix_calls: list = []
    _patch_render_helpers(monkeypatch, mix_calls)
    vdir = tmp_path / "v1"
    vdir.mkdir()
    return gb._render_generative_variant(
        job_id="j",
        rank=1,
        spec={"variant_id": "song_lyrics", "rank": 1, "text_mode": "lyrics", "track": _track()},
        clip_metas=[_Meta("c1")],
        clip_id_to_local={"c1": "/x.mp4"},
        clip_id_to_gcs={"c1": "users/u/plan/i/x.mp4"},
        probe_map={},
        available_footage_s=6.0,
        agent_text=None,
        agent_form={},
        variant_dir=str(vdir),
        lyrics_enabled=lyrics_enabled,
    )


def test_lyrics_render_persists_base_snapshot_and_flags(monkeypatch, tmp_path) -> None:
    res = _render_lyrics_variant(monkeypatch, tmp_path)

    assert res["ok"] is True
    assert res["base_video_path"] == "generative-jobs/j/base_1_song_lyrics.mp4"
    assert res["lyrics_enabled"] is True
    assert res["lyrics_available"] is True
    assert res["lyric_line_overrides"] is None
    assert res["lyric_overlay_snapshot"]
    assert res["lyric_overlay_snapshot"][0]["line_key"] == "L0"


def test_lyrics_disabled_skips_injection_but_keeps_base_for_user_text(
    monkeypatch,
    tmp_path,
) -> None:
    monkeypatch.setattr(
        gb,
        "_inject_lyrics",
        lambda *a, **k: (_ for _ in ()).throw(AssertionError("_inject_lyrics called")),
    )

    res = _render_lyrics_variant(monkeypatch, tmp_path, lyrics_enabled=False)

    assert res["ok"] is True
    assert res["lyrics_enabled"] is False
    assert res["lyric_overlay_snapshot"] is None
    assert res["base_video_path"] == "generative-jobs/j/base_1_song_lyrics.mp4"


def test_reburn_lyrics_variant_only_burns_text_elements(monkeypatch, tmp_path) -> None:
    base_file = tmp_path / "base.mp4"
    base_file.write_bytes(b"base")
    burned: list[list[dict]] = []

    monkeypatch.setattr(
        "app.storage.download_to_file",
        lambda gcs, local: open(local, "wb").write(base_file.read_bytes()),
    )
    monkeypatch.setattr(
        "app.storage.upload_public_read",
        lambda local, gcs: f"https://signed/{gcs}",
    )
    monkeypatch.setattr(
        "app.pipeline.generative_overlays.build_persistent_intro_overlays",
        lambda *a, **k: (_ for _ in ()).throw(AssertionError("intro synthesized")),
    )

    def _burn(input_path, overlays, output_path, tmpdir, *, matte=None):
        burned.append(overlays)
        with open(output_path, "wb") as f:
            f.write(b"final")

    monkeypatch.setattr("app.pipeline.text_overlay_skia.burn_text_overlays_skia", _burn)
    existing = {
        "rank": 1,
        "text_mode": "lyrics",
        "base_video_path": "generative-jobs/j/base_1_song_lyrics.mp4",
        "video_path": "generative-jobs/j/variant_1_song_lyrics.mp4",
        "text_elements_user_edited": True,
        "duration_s": 3.0,
        "text_elements": [
            {
                "id": "user",
                "text": "USER",
                "role": "generative_intro",
                "start_s": 0.0,
                "end_s": 1.0,
                "position": "middle",
            },
            {
                "id": "lyric",
                "text": "LYRIC",
                "role": "lyric_line",
                "start_s": 0.0,
                "end_s": 1.0,
                "position": "middle",
            },
        ],
    }

    res = gb._reburn_text_on_base(
        job_id="j",
        variant_id="song_lyrics",
        existing=existing,
        agent_text=types.SimpleNamespace(text="DO NOT BURN", highlight_word=None),
        agent_form={"effect": "karaoke-line"},
        text_mode="lyrics",
        resolved_style_set_id="default",
        size_override_px=None,
        settings=types.SimpleNamespace(),
    )

    assert res["ok"] is True
    assert len(burned) == 1
    assert [overlay["text"] for overlay in burned[0]] == ["USER"]


def _patch_regen_session(monkeypatch: pytest.MonkeyPatch, job: FakeJob, track=None) -> None:
    class _Sess:
        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def get(self, model, pk, **kw):
            if model is MusicTrack and track is not None:
                return track
            return job

        def commit(self):
            pass

    monkeypatch.setattr(gb, "_sync_session", lambda: _Sess())


def test_run_regenerate_variant_sticky_inherits_lyrics_fields(monkeypatch) -> None:
    job_id = str(uuid.uuid4())
    existing = {
        "variant_id": "song_lyrics",
        "rank": 1,
        "text_mode": "lyrics",
        "music_track_id": None,
        "lyrics_enabled": False,
        "lyric_line_overrides": {"L0": {"text": "Edited"}},
    }
    job = FakeJob(
        job_id=job_id,
        assembly_plan={"variants": [existing]},
        all_candidates={"clip_paths": ["users/u/clip.mp4"]},
    )
    _patch_regen_session(monkeypatch, job)
    monkeypatch.setattr(gb, "_update_variant_entry", lambda *a, **k: True)
    monkeypatch.setattr(
        gb,
        "_ingest_clips",
        lambda *a, **k: {
            "clip_metas": [_Meta("c1")],
            "clip_id_to_local": {"c1": "/x.mp4"},
            "clip_id_to_gcs": {"c1": "users/u/clip.mp4"},
            "probe_map": {},
            "hero": _Meta("c1"),
        },
    )
    monkeypatch.setattr(gb, "_available_footage_s", lambda probe: 3.0)
    monkeypatch.setattr(gb, "_reapply_user_media_layers", lambda *a, **k: False)
    seen: dict = {}

    def _render(**kwargs):
        seen.update(kwargs)
        return {"ok": True, "base_video_path": "base", "video_path": "video"}

    monkeypatch.setattr(gb, "_render_generative_variant", _render)

    gb._run_regenerate_variant(
        job_id,
        "song_lyrics",
        new_track_id=None,
        override_text=None,
        remove_text=False,
    )

    assert seen["lyrics_enabled"] is False
    assert seen["lyric_line_overrides"] == {"L0": {"text": "Edited"}}


def test_run_regenerate_variant_swap_song_wipes_lyric_overrides(monkeypatch) -> None:
    job_id = str(uuid.uuid4())
    new_track_id = str(uuid.uuid4())
    existing = {
        "variant_id": "song_lyrics",
        "rank": 1,
        "text_mode": "lyrics",
        "music_track_id": None,
        "lyrics_enabled": True,
        "lyric_line_overrides": {"L0": {"text": "Edited"}},
    }
    job = FakeJob(
        job_id=job_id,
        assembly_plan={"variants": [existing]},
        all_candidates={"clip_paths": ["users/u/clip.mp4"]},
    )
    _patch_regen_session(monkeypatch, job, track=_track(uuid.UUID(new_track_id)))
    monkeypatch.setattr(gb, "_update_variant_entry", lambda *a, **k: True)
    monkeypatch.setattr(gb, "_clear_user_timeline", lambda *a, **k: None)
    monkeypatch.setattr(
        gb,
        "_ingest_clips",
        lambda *a, **k: {
            "clip_metas": [_Meta("c1")],
            "clip_id_to_local": {"c1": "/x.mp4"},
            "clip_id_to_gcs": {"c1": "users/u/clip.mp4"},
            "probe_map": {},
            "hero": _Meta("c1"),
        },
    )
    monkeypatch.setattr(gb, "_available_footage_s", lambda probe: 3.0)
    monkeypatch.setattr(gb, "_reapply_user_media_layers", lambda *a, **k: False)
    seen: dict = {}
    monkeypatch.setattr(
        gb,
        "_render_generative_variant",
        lambda **kwargs: (
            seen.update(kwargs) or {"ok": True, "base_video_path": "base", "video_path": "video"}
        ),
    )

    gb._run_regenerate_variant(
        job_id,
        "song_lyrics",
        new_track_id=new_track_id,
        override_text=None,
        remove_text=False,
    )

    assert seen["lyrics_enabled"] is True
    assert seen["lyric_line_overrides"] is None
