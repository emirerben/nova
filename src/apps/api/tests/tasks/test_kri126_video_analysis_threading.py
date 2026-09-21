"""KRI-126 regression tests: `_analyze_video` real best_moments/description.

Root cause: `ClipMeta.best_moments` is `list[dict]` (Moment.model_dump()), but
the old code read each field with `getattr(m, "start_s"/"end_s"/"energy"/
"description", default)` -- a bare getattr on a dict always misses and
silently returns the default, so EVERY real video analysis persisted an
all-zero `best_moments` list. The top-level `getattr(meta, "description", "")`
read was worse: `ClipMeta` has no `description` field at all, so it was
always "". Every one of 30 prod video analyses showed this exact shape
(description="", three all-zero best_moments, kind_hint="screenshot").

These tests pin the fix: `_moment_field` (dict-or-attribute accessor),
malformed/inverted/zero-length moment dropping, the rebuilt `description`,
and kind-scoped staleness for the ANALYSIS_VERSION 6 -> 7 bump.

Also covers the ANALYSIS_VERSION 7 -> 8 bump (KRI-127 shared clip
understanding record): the persisted `understanding` block, the new
top-level `transcript` key, and the byte-identical `on_screen_text`
back-compat shape.
"""

from __future__ import annotations

from types import SimpleNamespace

import app.tasks.autoplace as ap


def _patch_video_probe_and_upload(monkeypatch) -> None:
    from app.config import settings as _settings

    monkeypatch.setattr(_settings, "gemini_api_key", "gemini-key")
    # The real probe type, not a SimpleNamespace: a namespace fake only knows
    # the fields that existed when it was written, so the first new read in
    # _analyze_video (#1104: codec, pix_fmt) broke every test here on main
    # even though both PRs were green on their own.
    from app.pipeline.probe import VideoProbe

    monkeypatch.setattr(
        "app.pipeline.probe.probe_video",
        lambda _path: VideoProbe(
            duration_s=10.0,
            fps=30.0,
            width=720,
            height=1280,
            has_audio=True,
            codec="h264",
            aspect_ratio="9:16",
            file_size_bytes=1_000_000,
            pix_fmt="yuv420p",
        ),
    )
    file_ref = SimpleNamespace(uri="provider://file", mime_type="video/mp4")
    monkeypatch.setattr(
        "app.pipeline.agents.gemini_analyzer.gemini_upload_and_wait",
        lambda _path: file_ref,
    )


def _patch_analyze_clip(monkeypatch, meta) -> None:
    monkeypatch.setattr(
        "app.pipeline.agents.gemini_analyzer.analyze_clip",
        lambda *a, **k: meta,
    )


# ── best_moments / description threading (dict shape — the real one) ─────────


def test_dict_shaped_best_moments_persist_real_values(monkeypatch) -> None:
    """The REAL ClipMeta shape: best_moments is a list of dicts. Every field
    (start_s/end_s/energy/description) must survive, not silently zero out."""
    _patch_video_probe_and_upload(monkeypatch)
    meta = SimpleNamespace(
        failed=False,
        best_moments=[
            {"start_s": 1.25, "end_s": 4.5, "energy": 8.0, "description": "goal celebration"},
            {"start_s": 6.0, "end_s": 9.0, "energy": 6.5, "description": "crowd cheering"},
        ],
        detected_subject="football match",
        hook_text="He scores!",
        transcript="",
        brands=[],
    )
    _patch_analyze_clip(monkeypatch, meta)

    analysis, _aspect, _duration, _dims = ap._analyze_video("/tmp/asset.mp4")

    assert analysis is not None
    assert analysis["best_moments"] == [
        {"start_s": 1.25, "end_s": 4.5, "energy": 8.0, "description": "goal celebration"},
        {"start_s": 6.0, "end_s": 9.0, "energy": 6.5, "description": "crowd cheering"},
    ]
    assert analysis["description"] == "goal celebration crowd cheering"
    assert analysis["subject"] == "football match"


def test_dict_shaped_moments_without_descriptions_fall_back_to_hook_text(
    monkeypatch,
) -> None:
    """No moment carries a description -> fall back to hook_text rather than
    persisting empty, but never invent a Gemini field that doesn't exist."""
    _patch_video_probe_and_upload(monkeypatch)
    meta = SimpleNamespace(
        failed=False,
        best_moments=[{"start_s": 0.0, "end_s": 3.0, "energy": 5.0, "description": ""}],
        detected_subject="park",
        hook_text="A quiet afternoon in the park",
        transcript="",
        brands=[],
    )
    _patch_analyze_clip(monkeypatch, meta)

    analysis, _a, _d, _dims = ap._analyze_video("/tmp/asset.mp4")

    assert analysis is not None
    assert analysis["description"] == "A quiet afternoon in the park"


def test_analysis_never_carries_kind_hint_for_video(monkeypatch) -> None:
    """The image-only vocabulary ("screenshot"/"photo"/...) has no video value
    and nothing downstream reads kind_hint off a video analysis -- the old
    hardcoded "screenshot" was actively misleading in the job-debug view."""
    _patch_video_probe_and_upload(monkeypatch)
    meta = SimpleNamespace(
        failed=False,
        best_moments=[],
        detected_subject="test pattern",
        hook_text="",
        transcript="",
        brands=[],
    )
    _patch_analyze_clip(monkeypatch, meta)

    analysis, _a, _d, _dims = ap._analyze_video("/tmp/asset.mp4")

    assert analysis is not None
    assert "kind_hint" not in analysis


# ── object-style (attribute) moments still work ───────────────────────────────


def test_object_shaped_best_moments_still_work(monkeypatch) -> None:
    """A duck-typed stand-in (SimpleNamespace moments, not dicts) must still
    thread real values through `_moment_field`'s attribute branch."""
    _patch_video_probe_and_upload(monkeypatch)
    meta = SimpleNamespace(
        failed=False,
        best_moments=[
            SimpleNamespace(start_s=2.0, end_s=5.0, energy=7.0, description="a wave"),
        ],
        detected_subject="beach",
        hook_text="",
        transcript="",
        brands=[],
    )
    _patch_analyze_clip(monkeypatch, meta)

    analysis, _a, _d, _dims = ap._analyze_video("/tmp/asset.mp4")

    assert analysis is not None
    assert analysis["best_moments"] == [
        {"start_s": 2.0, "end_s": 5.0, "energy": 7.0, "description": "a wave"}
    ]
    assert analysis["description"] == "a wave"


# ── malformed moments are dropped, not persisted ──────────────────────────────


def test_inverted_zero_length_and_malformed_moments_are_dropped(monkeypatch) -> None:
    _patch_video_probe_and_upload(monkeypatch)
    meta = SimpleNamespace(
        failed=False,
        best_moments=[
            # Well-formed -- kept.
            {"start_s": 1.0, "end_s": 3.0, "energy": 5.0, "description": "kept"},
            # Zero-length -- dropped.
            {"start_s": 4.0, "end_s": 4.0, "energy": 5.0, "description": "zero-length"},
            # Inverted -- dropped.
            {"start_s": 8.0, "end_s": 2.0, "energy": 5.0, "description": "inverted"},
            # Non-numeric -- dropped.
            {"start_s": "not-a-number", "end_s": 9.0, "energy": 5.0, "description": "bad"},
        ],
        detected_subject="mixed",
        hook_text="",
        transcript="",
        brands=[],
    )
    _patch_analyze_clip(monkeypatch, meta)

    analysis, _a, _d, _dims = ap._analyze_video("/tmp/asset.mp4")

    assert analysis is not None
    assert analysis["best_moments"] == [
        {"start_s": 1.0, "end_s": 3.0, "energy": 5.0, "description": "kept"}
    ]
    assert analysis["description"] == "kept"


# ── kind-scoped staleness (ANALYSIS_VERSION 6 -> 7, KRI-126 is video-only) ────


def test_stored_video_analysis_at_v6_is_stale() -> None:
    assert ap.analysis_is_stale({"source": "clip_metadata", "analysis_version": 6}, kind="video")


def test_stored_image_analysis_at_v6_stays_fresh() -> None:
    assert (
        ap.analysis_is_stale({"source": "image_metadata", "analysis_version": 6}, kind="image")
        is False
    )


# ── kind-scoped staleness (ANALYSIS_VERSION 7 -> 8, KRI-127 is video-only) ────


def test_stored_video_analysis_at_v7_is_stale() -> None:
    """v7 -> v8 (KRI-127) is video-only, so a v7 real video analysis is now
    stale — it predates the shared `understanding` block."""
    assert ap.analysis_is_stale({"source": "clip_metadata", "analysis_version": 7}, kind="video")


def test_stored_image_analysis_at_v7_stays_fresh() -> None:
    """v8 is video-only; the image floor stays at v6 (unaffected)."""
    assert (
        ap.analysis_is_stale({"source": "image_metadata", "analysis_version": 7}, kind="image")
        is False
    )


def test_stored_video_analysis_at_v8_is_fresh() -> None:
    assert (
        ap.analysis_is_stale({"source": "clip_metadata", "analysis_version": 8}, kind="video")
        is False
    )


def test_analysis_version_is_bumped_to_8() -> None:
    assert ap.ANALYSIS_VERSION == 8


# ── KRI-127: shared `understanding` block + top-level `transcript` key ────────


def test_analyze_video_persists_understanding_block(monkeypatch) -> None:
    """`_analyze_video` must persist a valid `understanding` block built by
    `understanding_payload`, and add a correctly-named top-level `transcript`
    key WITHOUT touching the legacy `on_screen_text` back-compat shape."""
    from app.schemas.clip_understanding import UNDERSTANDING_KEY, ClipUnderstanding

    _patch_video_probe_and_upload(monkeypatch)
    meta = SimpleNamespace(
        failed=False,
        best_moments=[
            {"start_s": 1.0, "end_s": 4.0, "energy": 7.0, "description": "spike at the net"},
        ],
        detected_subject="people playing volleyball",
        hook_text="watch this rally",
        transcript="okay so this is the final point " * 20,  # long enough to test capping
        clip_brands=["Mikasa"],
        clip_summary="Friends play a volleyball match on a sand court.",
        setting="outdoor sand volleyball court in a city park",
        activity="playing volleyball",
        people_count=6,
        speaks_to_camera=True,
        people_note="one man faces the camera and narrates",
        clip_content_type="action",
        clip_audio_type="dialogue",
    )
    _patch_analyze_clip(monkeypatch, meta)

    analysis, _a, _d, _dims = ap._analyze_video("/tmp/asset.mp4")

    assert analysis is not None
    assert UNDERSTANDING_KEY in analysis
    record = ClipUnderstanding.model_validate(analysis[UNDERSTANDING_KEY])
    assert record.activity == "playing volleyball"
    assert record.setting == "outdoor sand volleyball court in a city park"
    assert record.people.count == 6
    assert record.people.speaks_to_camera is True
    assert record.speech.has_speech is True
    assert record.brands == ["Mikasa"]
    assert record.notable_moments[0].description == "spike at the net"


def test_analyze_video_adds_transcript_key_keeps_on_screen_text_byte_identical(
    monkeypatch,
) -> None:
    """New top-level `transcript` key carries the spoken transcript capped at
    1200 chars; the pre-existing `on_screen_text` key is left exactly as it
    was pre-KRI-127 (still the spoken transcript, still capped at 400)."""
    _patch_video_probe_and_upload(monkeypatch)
    long_transcript = "word " * 400  # 2000 chars, well past both caps
    meta = SimpleNamespace(
        failed=False,
        best_moments=[],
        detected_subject="",
        hook_text="",
        transcript=long_transcript,
        brands=[],
    )
    _patch_analyze_clip(monkeypatch, meta)

    analysis, _a, _d, _dims = ap._analyze_video("/tmp/asset.mp4")

    assert analysis is not None
    assert analysis["on_screen_text"] == long_transcript[:400]
    assert analysis["transcript"] == long_transcript[:1200]
    assert len(analysis["transcript"]) > len(analysis["on_screen_text"])
