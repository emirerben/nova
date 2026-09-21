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
"""

from __future__ import annotations

from types import SimpleNamespace

import app.tasks.autoplace as ap


def _patch_video_probe_and_upload(monkeypatch) -> None:
    from app.config import settings as _settings

    monkeypatch.setattr(_settings, "gemini_api_key", "gemini-key")
    monkeypatch.setattr(
        "app.pipeline.probe.probe_video",
        lambda _path: SimpleNamespace(duration_s=10.0, width=720, height=1280),
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


def test_stored_video_analysis_at_v7_is_fresh() -> None:
    assert (
        ap.analysis_is_stale({"source": "clip_metadata", "analysis_version": 7}, kind="video")
        is False
    )


def test_analysis_version_is_bumped_to_7() -> None:
    assert ap.ANALYSIS_VERSION == 7
