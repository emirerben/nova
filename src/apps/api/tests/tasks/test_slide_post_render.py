"""Unit tests for `_run_slide_post_job` in isolation.

Storage and FFmpeg are mocked (no GCS, no subprocess) — the real FFmpeg
pipeline is covered by tests/pipeline/test_slide_post_build.py. This test
pins the ORCHESTRATION contract: which PlanItemAsset rows get included/
dropped, what the persisted variant dict looks like, and that `video_path`
lands in the exact same write as `slide_post.bundle_gcs_path`.
"""

from __future__ import annotations

import uuid
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

import app.tasks.generative_build as gb
from app import storage as storage_module
from app.models import PlanItem
from app.pipeline.slide_post import build as slide_build_module
from app.schemas.slide_post import SlideEdits, SlidePostDraft, SlideRef, TextOverlay


def _asset(*, asset_id, gcs_path, kind, plan_item_id, user_id, media_status="ready", **extra):
    defaults = {
        "id": asset_id,
        "gcs_path": gcs_path,
        "kind": kind,
        "plan_item_id": plan_item_id,
        "user_id": user_id,
        "media_status": media_status,
        "content_fingerprint": f"fp-{asset_id}",
        "gcs_generation": None,
        "duration_s": None,
    }
    defaults.update(extra)
    return SimpleNamespace(**defaults)


class _FakeSession:
    def __init__(self, job, item, assets):
        self._job = job
        self._item = item
        self._assets = assets

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return False

    def get(self, model, _pk, **_kwargs):
        if model is gb.Job:
            return self._job
        if model is PlanItem:
            return self._item
        return None

    def execute(self, _stmt):
        assets = self._assets

        class _Result:
            def scalars(self_inner):
                return self_inner

            def all(self_inner):
                return assets

        return _Result()

    def commit(self):
        pass


def _patch_storage_and_ffmpeg(
    monkeypatch, *, existing_normalized: set[str] | None = None
) -> list[str]:
    """No-op storage + ffmpeg stand-ins. Returns the list of uploaded keys."""
    uploaded: list[str] = []
    existing = existing_normalized or set()

    def _object_exists(key):
        return key in existing

    def _download_to_file(_object_path, local_path):
        with open(local_path, "wb") as f:
            f.write(b"fake-bytes")

    def _download_generation_to_file(_object_path, local_path, *, generation):  # noqa: ARG001
        _download_to_file(_object_path, local_path)

    def _upload_local_file(_local_path, object_path, _content_type):
        uploaded.append(object_path)

    monkeypatch.setattr(storage_module, "object_exists", _object_exists)
    monkeypatch.setattr(storage_module, "download_to_file", _download_to_file)
    monkeypatch.setattr(storage_module, "download_generation_to_file", _download_generation_to_file)
    monkeypatch.setattr(storage_module, "upload_local_file", _upload_local_file)

    monkeypatch.setattr(slide_build_module, "normalize_image_slide", lambda *a, **k: None)
    monkeypatch.setattr(slide_build_module, "normalize_video_slide", lambda *a, **k: None)
    monkeypatch.setattr(slide_build_module, "probe_duration_s", lambda _path: 3.0)
    monkeypatch.setattr(slide_build_module, "probe_dimensions", lambda _path: (1080, 1920))
    monkeypatch.setattr(slide_build_module, "render_preview_segment", lambda *a, **k: None)
    monkeypatch.setattr(slide_build_module, "concat_preview_segments", lambda *a, **k: None)
    monkeypatch.setattr(slide_build_module, "extract_cover", lambda *a, **k: None)
    monkeypatch.setattr(
        slide_build_module,
        "build_bundle_zip",
        lambda out_zip_path, **k: open(out_zip_path, "wb").close(),
    )
    return uploaded


def test_run_slide_post_job_persists_ordered_slides_and_bundle(monkeypatch) -> None:
    job_id = str(uuid.uuid4())
    item_id = uuid.uuid4()
    user_id = uuid.uuid4()
    good_image_id = uuid.uuid4()
    good_video_id = uuid.uuid4()

    job = SimpleNamespace(id=uuid.UUID(job_id), content_plan_item_id=item_id, user_id=user_id)
    draft = SlidePostDraft(
        platform_profile="instagram_carousel",
        slides=[
            SlideRef(id="s0", asset_id=good_image_id, kind="image", alt="a photo"),
            SlideRef(id="s1", asset_id=good_video_id, kind="video"),
        ],
        cover_index=0,
        caption="hello world",
    )
    item = SimpleNamespace(slide_post=draft.model_dump(mode="json"))
    assets = [
        _asset(
            asset_id=good_image_id,
            gcs_path=f"users/{user_id}/plan/{item_id}/pool/a.jpg",
            kind="image",
            plan_item_id=item_id,
            user_id=user_id,
        ),
        _asset(
            asset_id=good_video_id,
            gcs_path=f"users/{user_id}/plan/{item_id}/pool/b.mp4",
            kind="video",
            plan_item_id=item_id,
            user_id=user_id,
            duration_s=6.0,
        ),
    ]

    monkeypatch.setattr(gb, "_sync_session", lambda: _FakeSession(job, item, assets))
    uploaded = _patch_storage_and_ffmpeg(monkeypatch)
    captured: dict = {}
    monkeypatch.setattr(
        gb,
        "_upsert_variant_entry",
        lambda _job_id, result: captured.setdefault("result", result) or True,
    )
    monkeypatch.setattr(gb, "_finalize_job", lambda _job_id, _results: True)

    gb._run_slide_post_job(job_id, render_trace_id="trace-1")

    result = captured["result"]
    assert result["variant_id"] == "slides"
    assert result["resolved_archetype"] == "slides"
    assert result["render_status"] == "ready"
    assert [s["kind"] for s in result["slides"]] == ["image", "video"]
    assert [s["index"] for s in result["slides"]] == [0, 1]
    assert result["slide_post"]["platform_profile"] == "instagram_carousel"
    assert result["slide_post"]["caption"] == "hello world"
    assert result["slide_post"]["cover_index"] == 0
    # video_path (preview) and slide_post.bundle_gcs_path land in the SAME
    # upsert call — the reaper's ready-implies-complete invariant (risk #7).
    assert result["video_path"]
    assert result["slide_post"]["bundle_gcs_path"]
    assert result["poster_path"]
    assert any(key.endswith("bundle.zip") for key in uploaded)
    assert any(key.endswith("preview.mp4") for key in uploaded)
    assert any(key.endswith("cover.jpg") for key in uploaded)


def test_run_slide_post_job_rejects_incomplete_foreign_owned_draft_before_render(
    monkeypatch,
) -> None:
    """A render must never turn a partial ordered draft into a complete export."""
    job_id = str(uuid.uuid4())
    item_id = uuid.uuid4()
    user_id = uuid.uuid4()
    other_user_id = uuid.uuid4()
    good_id = uuid.uuid4()
    foreign_id = uuid.uuid4()

    job = SimpleNamespace(id=uuid.UUID(job_id), content_plan_item_id=item_id, user_id=user_id)
    draft = SlidePostDraft(
        platform_profile="tiktok_photo",
        slides=[
            SlideRef(id="s0", asset_id=good_id, kind="image"),
            SlideRef(id="s1", asset_id=foreign_id, kind="image"),
        ],
        cover_index=0,
    )
    item = SimpleNamespace(slide_post=draft.model_dump(mode="json"))
    assets = [
        _asset(
            asset_id=good_id,
            gcs_path=f"users/{user_id}/plan/{item_id}/pool/a.jpg",
            kind="image",
            plan_item_id=item_id,
            user_id=user_id,
        ),
        # Belongs to a different user — must never be resolved into this job.
        _asset(
            asset_id=foreign_id,
            gcs_path=f"users/{other_user_id}/plan/{item_id}/pool/b.jpg",
            kind="image",
            plan_item_id=item_id,
            user_id=other_user_id,
        ),
    ]

    monkeypatch.setattr(gb, "_sync_session", lambda: _FakeSession(job, item, assets))
    normalize = MagicMock(side_effect=AssertionError("incomplete draft must not render"))
    monkeypatch.setattr("app.pipeline.slide_post.build.normalize_image_slide", normalize)
    with pytest.raises(gb.SlidePostPolicyError) as exc_info:
        gb._run_slide_post_job(job_id, render_trace_id="trace-2")
    assert exc_info.value.reason == "incomplete_draft_assets"
    normalize.assert_not_called()


def test_run_slide_post_job_rejects_unready_draft_asset(monkeypatch) -> None:
    job_id = str(uuid.uuid4())
    item_id = uuid.uuid4()
    user_id = uuid.uuid4()
    only_id = uuid.uuid4()

    job = SimpleNamespace(id=uuid.UUID(job_id), content_plan_item_id=item_id, user_id=user_id)
    draft = SlidePostDraft(
        platform_profile="tiktok_photo",
        slides=[SlideRef(id="s0", asset_id=only_id, kind="image")],
        cover_index=0,
    )
    item = SimpleNamespace(slide_post=draft.model_dump(mode="json"))
    # media_status is "pending", not "ready" — the only slide is unusable.
    assets = [
        _asset(
            asset_id=only_id,
            gcs_path=f"users/{user_id}/plan/{item_id}/pool/a.jpg",
            kind="image",
            plan_item_id=item_id,
            user_id=user_id,
            media_status="pending",
        ),
    ]
    monkeypatch.setattr(gb, "_sync_session", lambda: _FakeSession(job, item, assets))

    with pytest.raises(gb.SlidePostPolicyError) as exc_info:
        gb._run_slide_post_job(job_id, render_trace_id="trace-3")
    assert exc_info.value.reason == "incomplete_draft_assets"


def test_run_slide_post_job_raises_on_empty_draft(monkeypatch) -> None:
    job_id = str(uuid.uuid4())
    item_id = uuid.uuid4()
    job = SimpleNamespace(id=uuid.UUID(job_id), content_plan_item_id=item_id, user_id=uuid.uuid4())
    item = SimpleNamespace(slide_post=None)
    monkeypatch.setattr(gb, "_sync_session", lambda: _FakeSession(job, item, []))

    with pytest.raises(gb.SlidePostPolicyError) as exc_info:
        gb._run_slide_post_job(job_id, render_trace_id="trace-4")
    assert exc_info.value.reason == "empty_draft"


@pytest.mark.parametrize(
    ("cached_suffix", "reused"),
    [
        ("_n2_wm1", True),
        # Cached before the watermark (KRI-472): must re-render, never ship
        # the unbranded derivative.
        ("_n2", False),
    ],
)
def test_run_slide_post_job_reuses_already_normalized_slide(
    monkeypatch, cached_suffix: str, reused: bool
) -> None:
    """A pure reorder/caption edit must not re-encode an unchanged slide —
    only the normalize step is skipped; the slide is still downloaded to
    build this render's preview segment."""
    job_id = str(uuid.uuid4())
    item_id = uuid.uuid4()
    user_id = uuid.uuid4()
    asset_id = uuid.uuid4()

    job = SimpleNamespace(id=uuid.UUID(job_id), content_plan_item_id=item_id, user_id=user_id)
    draft = SlidePostDraft(
        platform_profile="tiktok_photo",
        slides=[SlideRef(id="s0", asset_id=asset_id, kind="image")],
        cover_index=0,
    )
    item = SimpleNamespace(slide_post=draft.model_dump(mode="json"))
    fingerprint = f"fp-{asset_id}"
    assets = [
        _asset(
            asset_id=asset_id,
            gcs_path=f"users/{user_id}/plan/{item_id}/pool/a.jpg",
            kind="image",
            plan_item_id=item_id,
            user_id=user_id,
            content_fingerprint=fingerprint,
        ),
    ]
    monkeypatch.setattr(gb, "_sync_session", lambda: _FakeSession(job, item, assets))
    existing_key = (
        f"generative-jobs/{job_id}/slides/normalized/"
        f"{fingerprint}_1080x1920_noedits{cached_suffix}.jpg"
    )
    _patch_storage_and_ffmpeg(monkeypatch, existing_normalized={existing_key})

    normalize_calls: list[str] = []
    monkeypatch.setattr(
        slide_build_module,
        "normalize_image_slide",
        lambda *a, **k: normalize_calls.append("called"),
    )
    monkeypatch.setattr(gb, "_upsert_variant_entry", lambda _job_id, _result: True)
    monkeypatch.setattr(gb, "_finalize_job", lambda _job_id, _results: True)

    gb._run_slide_post_job(job_id, render_trace_id="trace-5")

    assert normalize_calls == ([] if reused else ["called"]), (
        "normalize must be skipped only when the current content-addressed key exists"
    )


def test_run_slide_post_job_edits_bust_the_normalized_cache_key(monkeypatch) -> None:
    """CRITICAL regression case (plans/024 follow-up eng-review): a slide's
    cache key must include its edits. Without this, editing a slide's text/
    look-preset and re-rendering would silently reuse the OLD normalized
    derivative from before the edit — the edit would never appear in the
    export. This asserts the render actually happens (cache MISS) when the
    only thing that changed is the edits, with the unedited key present."""
    job_id = str(uuid.uuid4())
    item_id = uuid.uuid4()
    user_id = uuid.uuid4()
    asset_id = uuid.uuid4()

    job = SimpleNamespace(id=uuid.UUID(job_id), content_plan_item_id=item_id, user_id=user_id)
    edits = SlideEdits(text=TextOverlay(content="sold out", position="bottom"))
    draft = SlidePostDraft(
        platform_profile="tiktok_photo",
        slides=[SlideRef(id="s0", asset_id=asset_id, kind="image", edits=edits)],
        cover_index=0,
    )
    item = SimpleNamespace(slide_post=draft.model_dump(mode="json"))
    fingerprint = f"fp-{asset_id}"
    assets = [
        _asset(
            asset_id=asset_id,
            gcs_path=f"users/{user_id}/plan/{item_id}/pool/a.jpg",
            kind="image",
            plan_item_id=item_id,
            user_id=user_id,
            content_fingerprint=fingerprint,
        ),
    ]
    monkeypatch.setattr(gb, "_sync_session", lambda: _FakeSession(job, item, assets))
    # Only the UNEDITED key exists in storage — simulating "this asset was
    # rendered once before, with no edits, and the user just added one."
    stale_unedited_key = (
        f"generative-jobs/{job_id}/slides/normalized/{fingerprint}_1080x1920_noedits_n2.jpg"
    )
    _patch_storage_and_ffmpeg(monkeypatch, existing_normalized={stale_unedited_key})

    normalize_calls: list[object] = []
    monkeypatch.setattr(
        slide_build_module,
        "normalize_image_slide",
        lambda *a, **k: normalize_calls.append(k.get("edits")),
    )
    monkeypatch.setattr(gb, "_upsert_variant_entry", lambda _job_id, _result: True)
    monkeypatch.setattr(gb, "_finalize_job", lambda _job_id, _results: True)

    gb._run_slide_post_job(job_id, render_trace_id="trace-6")

    assert len(normalize_calls) == 1, (
        "a changed edits digest must miss the stale unedited cache key and re-render"
    )
    assert normalize_calls[0] == edits, "the render must receive this slide's actual edits"


def _sized_job(
    monkeypatch,
    *,
    profile: str,
    aspect: str | None,
    fit: str | None,
    drawing: dict | None = None,
    tilt: dict | None = None,
):
    """Run one sized slide through `_run_slide_post_job` with storage/ffmpeg stubbed.
    Returns (variant result, normalize canvases, preview canvases, normalized keys)."""
    job_id = str(uuid.uuid4())
    item_id = uuid.uuid4()
    user_id = uuid.uuid4()
    asset_id = uuid.uuid4()
    job = SimpleNamespace(id=uuid.UUID(job_id), content_plan_item_id=item_id, user_id=user_id)
    draft = SlidePostDraft(
        platform_profile=profile,
        slides=[
            SlideRef(
                id="s0",
                asset_id=asset_id,
                kind="image",
                edits=SlideEdits(fit=fit, drawing=drawing, tilt=tilt)
                if (fit or drawing or tilt)
                else None,
            ),
            SlideRef(id="s1", asset_id=asset_id, kind="image"),
        ],
        aspect=aspect,
    )
    item = SimpleNamespace(slide_post=draft.model_dump(mode="json"))
    assets = [
        _asset(
            asset_id=asset_id,
            gcs_path=f"users/{user_id}/plan/{item_id}/pool/a.jpg",
            kind="image",
            plan_item_id=item_id,
            user_id=user_id,
        )
    ]
    monkeypatch.setattr(gb, "_sync_session", lambda: _FakeSession(job, item, assets))
    uploaded = _patch_storage_and_ffmpeg(monkeypatch)
    normalize_canvases: list[tuple[int, int]] = []
    preview_canvases: list[tuple[int, int]] = []
    monkeypatch.setattr(
        slide_build_module,
        "normalize_image_slide",
        lambda *a, **k: normalize_canvases.append(k["canvas"]),
    )
    monkeypatch.setattr(
        slide_build_module,
        "render_preview_segment",
        lambda *a, **k: preview_canvases.append(k["canvas"]),
    )
    captured: dict = {}
    monkeypatch.setattr(
        gb, "_upsert_variant_entry", lambda _j, result: captured.setdefault("r", result) or True
    )
    monkeypatch.setattr(gb, "_finalize_job", lambda _j, _r: True)
    gb._run_slide_post_job(job_id, render_trace_id="trace-sized")
    keys = [k for k in uploaded if "/normalized/" in k]
    return captured["r"], normalize_canvases, preview_canvases, keys


def test_sized_post_renders_every_slide_and_the_preview_at_the_chosen_canvas(monkeypatch) -> None:
    result, normalize, preview, keys = _sized_job(
        monkeypatch, profile="tiktok_photo", aspect="1:1", fit="blur"
    )
    assert normalize == [(1080, 1080), (1080, 1080)]
    assert set(preview) == {(1080, 1080)}
    assert all("_1080x1080_" in k for k in keys)
    assert result["slide_post"]["aspect"] == "1:1"


def test_a_post_that_never_chose_a_size_keeps_the_profile_canvas_and_keys(monkeypatch) -> None:
    result, normalize, preview, keys = _sized_job(
        monkeypatch, profile="instagram_carousel", aspect=None, fit=None
    )
    assert set(normalize) == {(1080, 1350)} and set(preview) == {(1080, 1350)}
    assert all("_1080x1350_noedits_n2_wm1" in k for k in keys)
    assert result["slide_post"]["aspect"] is None


def test_fit_busts_the_normalized_cache_key(monkeypatch) -> None:
    _, _, _, fit_keys = _sized_job(monkeypatch, profile="tiktok_photo", aspect=None, fit="blur")
    _, _, _, fill_keys = _sized_job(monkeypatch, profile="tiktok_photo", aspect=None, fit=None)
    # Slide 0 is the one with Fit. Same asset and canvas, but it must not share a key with
    # the unfitted render, or a Fit toggle would re-export the OLD cover-cropped pixels.
    assert "noedits" not in fit_keys[0] and "noedits" in fit_keys[1]
    assert "noedits" in fill_keys[0]


def test_variant_is_stamped_with_the_sizes_renderer_but_dispatch_stays_at_2(monkeypatch) -> None:
    from app.agents._schemas.edit_format import (  # noqa: PLC0415
        SLIDES_RENDERER_VERSION,
        SLIDES_SIZES_RENDERER_VERSION,
    )

    result, *_ = _sized_job(monkeypatch, profile="tiktok_photo", aspect="4:5", fit=None)
    assert result["slide_post"]["renderer_version"] == SLIDES_SIZES_RENDERER_VERSION == 3
    # The job-dispatch stamp is what a not-yet-updated worker checks at entry.
    assert result["slides_renderer_version"] == SLIDES_RENDERER_VERSION == 2


_DRAWING = {
    "canvas_aspect": 0.5625,
    "strokes": [
        {
            "color": "#FF3B30",
            "width_frac": 0.02,
            "points": [[0.2, 0.3], [0.8, 0.6]],
        }
    ],
}


def test_a_drawn_post_is_stamped_by_the_drawing_renderer_and_dispatch_stays_at_2(
    monkeypatch,
) -> None:
    from app.agents._schemas.edit_format import (  # noqa: PLC0415
        SLIDES_DRAWING_RENDERER_VERSION,
        SLIDES_RENDERER_VERSION,
    )

    result, *_ = _sized_job(
        monkeypatch, profile="tiktok_photo", aspect=None, fit=None, drawing=_DRAWING
    )
    assert result["slide_post"]["renderer_version"] == SLIDES_DRAWING_RENDERER_VERSION == 4
    assert result["slides_renderer_version"] == SLIDES_RENDERER_VERSION == 2


def test_a_drawing_busts_the_normalized_key_of_that_slide_only(monkeypatch) -> None:
    _, _, _, drawn = _sized_job(
        monkeypatch, profile="tiktok_photo", aspect=None, fit=None, drawing=_DRAWING
    )
    _, _, _, plain = _sized_job(monkeypatch, profile="tiktok_photo", aspect=None, fit=None)
    version = slide_build_module.SLIDE_DRAWING_PAINTER_VERSION

    def name(key):  # the job id and the asset fingerprint are random per run
        return key.split("/slides/normalized/")[1].split("_", 1)[1]

    drawn, plain = [name(k) for k in drawn], [name(k) for k in plain]
    assert "noedits" not in drawn[0] and f"_wm1_dr{version}." in drawn[0]
    # The undrawn slide beside it, and every key of an undrawn post, are unchanged.
    assert drawn[1] == plain[1] and "_dr" not in drawn[1]
    assert plain[0] == plain[1] and "_dr" not in plain[0]


_TILT = {"rotation_deg": 15, "backdrop": {"kind": "blur"}}


def test_a_tilted_post_is_stamped_by_the_tilt_renderer_and_dispatch_stays_at_2(
    monkeypatch,
) -> None:
    from app.agents._schemas.edit_format import (  # noqa: PLC0415
        SLIDES_RENDERER_VERSION,
        SLIDES_TILT_RENDERER_VERSION,
    )

    result, *_ = _sized_job(monkeypatch, profile="tiktok_photo", aspect=None, fit=None, tilt=_TILT)
    assert result["slide_post"]["renderer_version"] == SLIDES_TILT_RENDERER_VERSION == 6
    assert result["slides_renderer_version"] == SLIDES_RENDERER_VERSION == 2
    plain, *_ = _sized_job(monkeypatch, profile="tiktok_photo", aspect=None, fit=None)
    assert plain["slide_post"]["renderer_version"] == 3  # an untilted post is stamped as before


def test_a_tilt_busts_the_normalized_key_of_that_slide_only(monkeypatch) -> None:
    _, _, _, tilted = _sized_job(
        monkeypatch, profile="tiktok_photo", aspect=None, fit=None, tilt=_TILT
    )
    _, _, _, plain = _sized_job(monkeypatch, profile="tiktok_photo", aspect=None, fit=None)
    version = slide_build_module.SLIDE_TILT_COMPOSITOR_VERSION

    def name(key):  # the job id and the asset fingerprint are random per run
        return key.split("/slides/normalized/")[1].split("_", 1)[1]

    tilted, plain = [name(k) for k in tilted], [name(k) for k in plain]
    assert "noedits" not in tilted[0] and f"_wm1_tl{version}." in tilted[0]
    # The untilted slide beside it, and every key of an untilted post, are unchanged.
    assert tilted[1] == plain[1] and "_tl" not in tilted[1]
    assert plain[0] == plain[1] and "_tl" not in plain[0]


# ── Slide stickers (KRI-647): pool image that is never a slide ───────────────────────────


def _sticker_job(monkeypatch, *, sticker_fp="fp-sticker", sticker_owner="me", sticker_kind="image"):
    """Run a two-slide post whose slide 0 carries one sticker through `_run_slide_post_job`.

    Returns (variant result, normalize call kwargs, normalized keys)."""
    job_id = str(uuid.uuid4())
    item_id = uuid.uuid4()
    user_id = uuid.uuid4()
    slide_asset, sticker_asset = uuid.uuid4(), uuid.uuid4()
    job = SimpleNamespace(id=uuid.UUID(job_id), content_plan_item_id=item_id, user_id=user_id)
    sticker = {
        "id": "st1",
        "asset_id": str(sticker_asset),
        "x_frac": 0.5,
        "y_frac": 0.5,
        "width_frac": 0.3,
        "rotation_deg": 15,
    }
    draft = SlidePostDraft(
        platform_profile="tiktok_photo",
        slides=[
            SlideRef(
                id="s0",
                asset_id=slide_asset,
                kind="image",
                edits=SlideEdits(stickers=[sticker]),
            ),
        ],
    )
    item = SimpleNamespace(slide_post=draft.model_dump(mode="json"))
    prefix = f"users/{user_id}/plan/{item_id}/pool"
    assets = [
        _asset(
            asset_id=slide_asset,
            gcs_path=f"{prefix}/photo.jpg",
            kind="image",
            plan_item_id=item_id,
            user_id=user_id,
        ),
        _asset(
            asset_id=sticker_asset,
            gcs_path=f"{prefix}/strava.png",
            kind=sticker_kind,
            plan_item_id=item_id,
            user_id=user_id if sticker_owner == "me" else uuid.uuid4(),
            content_fingerprint=sticker_fp,
        ),
    ]
    monkeypatch.setattr(gb, "_sync_session", lambda: _FakeSession(job, item, assets))
    uploaded = _patch_storage_and_ffmpeg(monkeypatch)
    calls: list[dict] = []
    downloaded: list[str] = []
    real_download = storage_module.download_to_file

    def _download(path, local):
        downloaded.append(path)
        real_download(path, local)

    monkeypatch.setattr(storage_module, "download_to_file", _download)
    monkeypatch.setattr(
        slide_build_module,
        "normalize_image_slide",
        lambda *a, **k: calls.append({**k, "downloaded": list(downloaded)}),
    )
    captured: dict = {}
    monkeypatch.setattr(
        gb, "_upsert_variant_entry", lambda _j, result: captured.setdefault("r", result) or True
    )
    monkeypatch.setattr(gb, "_finalize_job", lambda _j, _r: True)
    gb._run_slide_post_job(job_id, render_trace_id="trace-stickers")
    keys = [k for k in uploaded if "/normalized/" in k]
    return captured["r"], calls, keys, f"{prefix}/strava.png"


def test_a_sticker_is_downloaded_rendered_and_never_becomes_a_slide(monkeypatch) -> None:
    from app.agents._schemas.edit_format import (  # noqa: PLC0415
        SLIDES_RENDERER_VERSION,
        SLIDES_STICKER_RENDERER_VERSION,
    )

    result, calls, keys, sticker_path = _sticker_job(monkeypatch)
    assert len(result["slides"]) == 1  # the pool's sticker image is not a slide
    assert result["slide_post"]["renderer_version"] == SLIDES_STICKER_RENDERER_VERSION == 5
    assert result["slides_renderer_version"] == SLIDES_RENDERER_VERSION == 2
    assert keys[0].endswith(f"_wm1_st{slide_build_module.SLIDE_STICKER_COMPOSITOR_VERSION}.jpg")
    assert sticker_path in calls[0]["downloaded"]
    assert set(calls[0]["sticker_sources"]) == {"st1"}


def test_a_stale_foreign_or_non_image_sticker_is_dropped_not_fatal(monkeypatch) -> None:
    for kwargs in ({"sticker_owner": "other"}, {"sticker_kind": "video"}):
        result, calls, _keys, sticker_path = _sticker_job(monkeypatch, **kwargs)
        assert result["render_status"] == "ready"
        assert calls[0]["sticker_sources"] == {}
        assert sticker_path not in calls[0]["downloaded"]  # never fetched


def test_replacing_the_sticker_image_busts_the_key(monkeypatch) -> None:
    def name(key):
        return key.split("/slides/normalized/")[1].split("_", 1)[1]

    a = _sticker_job(monkeypatch, sticker_fp="fp-a")[2]
    b = _sticker_job(monkeypatch, sticker_fp="fp-b")[2]
    assert name(a[0]) != name(b[0])
