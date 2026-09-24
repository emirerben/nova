"""Unit tests for `app.services.phone_overlay_grounding` (KRI-176).

`ground_phone_subtitled_overlays` is exercised directly (not via the phone
worker) with the matcher stage faked via the `_load_ready_pool_assets` seam
and either `heuristic_match` or `OverlayPlacementAgent.run` -- the real
`build_suggestions` / `arbitrate_media_overlays` / `sample_face_regions`
machinery is otherwise exercised for real, since it owns the actual
geometry/drop-reason behavior this module depends on.
"""

from __future__ import annotations

import json
import uuid
from types import SimpleNamespace
from unittest.mock import Mock

import app.services.phone_overlay_grounding as pg
from app.agents.overlay_placement import OverlayPlacementAgent, OverlayPlacementOutput, RawPlacement
from app.pipeline.phone_subtitled_lanes import CAPTION_BAND_TOP_FRAC
from app.pipeline.render_geometry import (
    MediaFootprint,
    NormalizedBox,
    ProtectedRegion,
    _box_for_overlay,
)


def _open_session():
    raise AssertionError("the DB seam should be monkeypatched in these tests")


def _asset(
    id_: str,
    *,
    kind: str = "image",
    generation: str | None = "7",
    filename: str | None = "photo.jpg",
    subject: str = "",
    aspect: float | None = 1.0,
) -> dict:
    return {
        "id": id_,
        "gcs_path": f"users/u1/plan/item1/pool/{id_}.jpg",
        "gcs_generation": generation,
        "kind": kind,
        "source_filename": filename,
        "duration_s": 5.0 if kind == "video" else None,
        "aspect": aspect,
        "user_context": "",
        "analysis": {"subject": subject} if subject else {},
    }


def _word(text: str, start_s: float, end_s: float) -> dict:
    return {"text": text, "start_s": start_s, "end_s": end_s, "confidence": 1.0}


def _placement(
    asset_id: str, *, start_s: float, end_s: float, tier: str = "confident", reason: str = ""
) -> RawPlacement:
    return RawPlacement(
        asset_id=asset_id,
        slot="top",
        start_s=start_s,
        end_s=end_s,
        confidence_tier=tier,
        reason=reason,
        transcript_anchor="",
    )


WORDS = [
    _word("So", 0.0, 0.3),
    _word("here", 0.3, 0.6),
    _word("is", 0.6, 0.8),
    _word("the", 0.8, 0.9),
    _word("screenshot", 3.0, 3.6),
    _word("I", 3.6, 3.7),
    _word("mentioned", 3.7, 4.2),
]


def _patch_assets(monkeypatch, assets: list[dict]) -> None:
    monkeypatch.setattr(pg, "_load_ready_pool_assets", lambda *a, **k: assets)


# --- matcher selection -------------------------------------------------------


def test_agent_path_places_card_with_default_geometry(monkeypatch):
    asset = _asset("a1", subject="screenshot")
    _patch_assets(monkeypatch, [asset])
    monkeypatch.setattr(pg.settings, "gemini_api_key", "fake-key", raising=False)

    def _fake_run(self, input, ctx=None):  # noqa: A002, ARG001
        return OverlayPlacementOutput(
            placements=[
                _placement("a1", start_s=3.0, end_s=6.0, reason="You mention the screenshot here.")
            ],
            wishlist=["Add a close-up of the settings page"],
        )

    monkeypatch.setattr(OverlayPlacementAgent, "run", _fake_run)

    result = pg.ground_phone_subtitled_overlays(
        _open_session,
        job_id=str(uuid.uuid4()),
        words=WORDS,
        duration_s=15.0,
        clip_path=None,
    )

    assert len(result.cards) == 1
    card = result.cards[0]
    assert card.media_id == "a1"
    assert card.gcs_path == asset["gcs_path"]
    assert card.generation == "7"
    assert card.start_s == 3.0
    assert card.x_frac == 0.74
    assert card.y_frac == 0.22
    assert card.scale == 0.36
    assert card.fade is True

    receipt = result.receipt
    assert receipt["version"] == 1
    assert receipt["matcher"] == "agent"
    assert receipt["face_sampling"] == "skipped"
    assert receipt["wishlist"] == ["Add a close-up of the settings page"]
    assert receipt["unplaced"] == []
    assert len(receipt["placed"]) == 1
    placed = receipt["placed"][0]
    assert placed["media_id"] == "a1"
    assert placed["label"] == "photo.jpg"
    assert "screenshot" in placed["reason"]


def test_heuristic_fallback_used_when_no_gemini_key(monkeypatch):
    asset = _asset("a1", subject="screenshot")
    _patch_assets(monkeypatch, [asset])
    monkeypatch.setattr(pg.settings, "gemini_api_key", None, raising=False)

    monkeypatch.setattr(
        pg,
        "heuristic_match",
        lambda words, assets, *, duration_s: [
            _placement("a1", start_s=3.0, end_s=5.0, tier="likely")
        ],
    )

    result = pg.ground_phone_subtitled_overlays(
        _open_session, job_id=str(uuid.uuid4()), words=WORDS, duration_s=15.0, clip_path=None
    )

    assert result.receipt["matcher"] == "heuristic"
    assert len(result.cards) == 1
    assert result.cards[0].media_id == "a1"


# --- pool-asset classification ----------------------------------------------


def test_video_visual_reported_unsupported(monkeypatch):
    video = _asset("v1", kind="video", filename="clip.mp4")
    _patch_assets(monkeypatch, [video])
    monkeypatch.setattr(pg.settings, "gemini_api_key", None, raising=False)
    monkeypatch.setattr(pg, "heuristic_match", lambda *a, **k: [])

    result = pg.ground_phone_subtitled_overlays(
        _open_session, job_id=str(uuid.uuid4()), words=WORDS, duration_s=15.0, clip_path=None
    )

    assert result.cards == []
    assert result.receipt["unplaced"] == [
        {"media_id": "v1", "label": "clip.mp4", "reason": "video_not_supported"}
    ]


def test_unmatched_asset_reported_no_spoken_match(monkeypatch):
    matched = _asset("a1", subject="screenshot")
    unmatched = _asset("a2", subject="unrelated widget")
    _patch_assets(monkeypatch, [matched, unmatched])
    monkeypatch.setattr(pg.settings, "gemini_api_key", None, raising=False)
    monkeypatch.setattr(
        pg,
        "heuristic_match",
        lambda *a, **k: [_placement("a1", start_s=3.0, end_s=5.0, tier="likely")],
    )

    result = pg.ground_phone_subtitled_overlays(
        _open_session, job_id=str(uuid.uuid4()), words=WORDS, duration_s=15.0, clip_path=None
    )

    placed_ids = {c.media_id for c in result.cards}
    assert placed_ids == {"a1"}
    unplaced_by_id = {u["media_id"]: u for u in result.receipt["unplaced"]}
    assert unplaced_by_id["a2"]["reason"] == "no_spoken_match"


def test_hook_window_drop_reason_surfaces(monkeypatch):
    """A `likely`-tier placement inside the 2.5s hook window is dropped by
    the real `build_suggestions` -- the trace reason must reach the receipt
    verbatim rather than falling back to a generic reason."""
    asset = _asset("a1", subject="logo")
    _patch_assets(monkeypatch, [asset])
    monkeypatch.setattr(pg.settings, "gemini_api_key", None, raising=False)
    monkeypatch.setattr(
        pg,
        "heuristic_match",
        lambda *a, **k: [_placement("a1", start_s=0.3, end_s=2.0, tier="likely")],
    )

    result = pg.ground_phone_subtitled_overlays(
        _open_session, job_id=str(uuid.uuid4()), words=WORDS, duration_s=15.0, clip_path=None
    )

    assert result.cards == []
    assert result.receipt["unplaced"] == [
        {"media_id": "a1", "label": "photo.jpg", "reason": "hook_window"}
    ]


# --- geometry / face awareness -----------------------------------------------


def test_caption_band_protection_keeps_card_clear(monkeypatch):
    asset = _asset("a1", subject="screenshot")
    _patch_assets(monkeypatch, [asset])
    monkeypatch.setattr(pg.settings, "gemini_api_key", None, raising=False)
    monkeypatch.setattr(
        pg, "heuristic_match", lambda *a, **k: [_placement("a1", start_s=3.0, end_s=5.0)]
    )

    result = pg.ground_phone_subtitled_overlays(
        _open_session, job_id=str(uuid.uuid4()), words=WORDS, duration_s=15.0, clip_path=None
    )

    card = result.cards[0]
    assert card.y_frac <= CAPTION_BAND_TOP_FRAC
    box = _box_for_overlay(
        {"position": "custom", "x_frac": card.x_frac, "y_frac": card.y_frac, "scale": card.scale},
        footprint=MediaFootprint(aspect_ratio=1.0),
    )
    assert box.bottom <= 0.60 + 1e-6


def test_face_collision_moves_card(monkeypatch):
    asset = _asset("a1", subject="screenshot")
    _patch_assets(monkeypatch, [asset])
    monkeypatch.setattr(pg.settings, "gemini_api_key", None, raising=False)
    monkeypatch.setattr(
        pg, "heuristic_match", lambda *a, **k: [_placement("a1", start_s=3.0, end_s=5.0)]
    )

    def _fake_sample_face_regions(
        video_path, anchor_times_s, *, max_samples=12, timeout_s=2.0, count_decoded=False
    ):
        # Covers the default upper-right corner (and its immediate neighbor
        # candidate) so arbitration must move the card elsewhere.
        region = ProtectedRegion(0.0, float("inf"), NormalizedBox(0.45, 0.0, 1.0, 0.5), kind="face")
        return [region], {"attempted": len(anchor_times_s), "detected": 1}

    monkeypatch.setattr(pg, "sample_face_regions", _fake_sample_face_regions)

    result = pg.ground_phone_subtitled_overlays(
        _open_session,
        job_id=str(uuid.uuid4()),
        words=WORDS,
        duration_s=15.0,
        clip_path="/tmp/clip.mp4",
    )

    assert len(result.cards) == 1
    card = result.cards[0]
    assert (card.x_frac, card.y_frac) != (pg._DEFAULT_CARD_X_FRAC, pg._DEFAULT_CARD_Y_FRAC)
    assert card.x_frac < 0.45  # moved clear of the blocked right half
    assert result.receipt["face_sampling"] == "ok"


def test_face_sampling_failure_is_fail_open(monkeypatch):
    asset = _asset("a1", subject="screenshot")
    _patch_assets(monkeypatch, [asset])
    monkeypatch.setattr(pg.settings, "gemini_api_key", None, raising=False)
    monkeypatch.setattr(
        pg, "heuristic_match", lambda *a, **k: [_placement("a1", start_s=3.0, end_s=5.0)]
    )

    def _broken_sample_face_regions(*a, **k):
        raise RuntimeError("sampler subprocess crashed")

    monkeypatch.setattr(pg, "sample_face_regions", _broken_sample_face_regions)

    result = pg.ground_phone_subtitled_overlays(
        _open_session,
        job_id=str(uuid.uuid4()),
        words=WORDS,
        duration_s=15.0,
        clip_path="/tmp/clip.mp4",
    )

    assert result.receipt["face_sampling"] == "failed"
    assert len(result.cards) == 1
    # No face regions were applied -- the card keeps the untouched default spot.
    assert result.cards[0].x_frac == pg._DEFAULT_CARD_X_FRAC
    assert result.cards[0].y_frac == pg._DEFAULT_CARD_Y_FRAC


# --- receipt safety / coverage invariants ------------------------------------


def test_receipt_never_contains_gcs_path_or_generation(monkeypatch):
    asset = _asset("a1", subject="screenshot", generation="super-secret-generation-42")
    _patch_assets(monkeypatch, [asset])
    monkeypatch.setattr(pg.settings, "gemini_api_key", None, raising=False)
    monkeypatch.setattr(
        pg, "heuristic_match", lambda *a, **k: [_placement("a1", start_s=3.0, end_s=5.0)]
    )

    result = pg.ground_phone_subtitled_overlays(
        _open_session, job_id=str(uuid.uuid4()), words=WORDS, duration_s=15.0, clip_path=None
    )

    dumped = json.dumps(result.receipt)
    assert "gcs_path" not in dumped
    assert "generation" not in dumped
    assert asset["gcs_path"] not in dumped
    assert "super-secret-generation-42" not in dumped


def test_every_candidate_appears_exactly_once(monkeypatch):
    placed_asset = _asset("a1", subject="screenshot")
    dropped_asset = _asset("a2", subject="logo")  # will be hook-window dropped
    unmatched_asset = _asset("a3", subject="unrelated")
    video_asset = _asset("v1", kind="video", filename="clip.mp4")
    used_asset = _asset("used", subject="already applied")
    _patch_assets(
        monkeypatch, [placed_asset, dropped_asset, unmatched_asset, video_asset, used_asset]
    )
    monkeypatch.setattr(pg.settings, "gemini_api_key", None, raising=False)
    monkeypatch.setattr(
        pg,
        "heuristic_match",
        lambda *a, **k: [
            _placement("a1", start_s=3.0, end_s=5.0),
            _placement("a2", start_s=0.3, end_s=2.0, tier="likely"),
        ],
    )

    result = pg.ground_phone_subtitled_overlays(
        _open_session,
        job_id=str(uuid.uuid4()),
        words=WORDS,
        duration_s=15.0,
        clip_path=None,
        used_media_ids=frozenset({"used"}),
    )

    seen = [p["media_id"] for p in result.receipt["placed"]] + [
        u["media_id"] for u in result.receipt["unplaced"]
    ]
    assert sorted(seen) == ["a1", "a2", "a3", "v1"]
    assert len(seen) == len(set(seen))
    assert "used" not in seen


def test_used_media_ids_are_skipped(monkeypatch):
    kept = _asset("a1", subject="screenshot")
    skipped = _asset("a2", subject="already added by an admin")
    _patch_assets(monkeypatch, [kept, skipped])
    monkeypatch.setattr(pg.settings, "gemini_api_key", None, raising=False)
    monkeypatch.setattr(
        pg, "heuristic_match", lambda *a, **k: [_placement("a1", start_s=3.0, end_s=5.0)]
    )

    result = pg.ground_phone_subtitled_overlays(
        _open_session,
        job_id=str(uuid.uuid4()),
        words=WORDS,
        duration_s=15.0,
        clip_path=None,
        used_media_ids=frozenset({"a2"}),
    )

    all_ids = {p["media_id"] for p in result.receipt["placed"]} | {
        u["media_id"] for u in result.receipt["unplaced"]
    }
    assert "a2" not in all_ids


# --- empty / early-return branches -------------------------------------------


def test_no_image_assets_returns_none_matcher(monkeypatch):
    _patch_assets(monkeypatch, [])

    result = pg.ground_phone_subtitled_overlays(
        _open_session, job_id=str(uuid.uuid4()), words=WORDS, duration_s=15.0, clip_path=None
    )

    assert result.cards == []
    assert result.receipt == {
        "version": 1,
        "matcher": "none",
        "face_sampling": "skipped",
        "placed": [],
        "unplaced": [],
        "wishlist": [],
    }


def test_no_words_reports_every_image_as_no_spoken_match(monkeypatch):
    asset = _asset("a1", subject="screenshot")
    _patch_assets(monkeypatch, [asset])

    result = pg.ground_phone_subtitled_overlays(
        _open_session, job_id=str(uuid.uuid4()), words=[], duration_s=15.0, clip_path=None
    )

    assert result.cards == []
    assert result.receipt["matcher"] == "none"
    assert result.receipt["unplaced"] == [
        {"media_id": "a1", "label": "photo.jpg", "reason": "no_spoken_match"}
    ]


# --- _load_ready_pool_assets --------------------------------------------------


def test_load_ready_pool_assets_queries_ready_rows(monkeypatch):
    item_id = uuid.uuid4()
    job = SimpleNamespace(id=uuid.uuid4(), content_plan_item_id=item_id)
    row = SimpleNamespace(
        id=uuid.uuid4(),
        gcs_path="users/u/plan/i/pool/x.jpg",
        gcs_generation="3",
        kind="image",
        source_filename="x.jpg",
        duration_s=None,
        aspect=1.5,
        user_context="ctx",
        analysis={"subject": "s"},
    )
    session = Mock()
    session.get = Mock(return_value=job)
    scalars_result = Mock()
    scalars_result.all = Mock(return_value=[row])
    execute_result = Mock()
    execute_result.scalars = Mock(return_value=scalars_result)
    session.execute = Mock(return_value=execute_result)

    def _sessions():
        from contextlib import contextmanager

        @contextmanager
        def _cm():
            yield session

        return _cm()

    rows = pg._load_ready_pool_assets(_sessions, job_id=str(job.id))
    assert rows == [
        {
            "id": str(row.id),
            "gcs_path": row.gcs_path,
            "gcs_generation": "3",
            "kind": "image",
            "source_filename": "x.jpg",
            "duration_s": None,
            "aspect": 1.5,
            "user_context": "ctx",
            "analysis": {"subject": "s"},
        }
    ]


def test_load_ready_pool_assets_missing_job_returns_empty(monkeypatch):
    session = Mock()
    session.get = Mock(return_value=None)

    def _sessions():
        from contextlib import contextmanager

        @contextmanager
        def _cm():
            yield session

        return _cm()

    assert pg._load_ready_pool_assets(_sessions, job_id=str(uuid.uuid4())) == []
