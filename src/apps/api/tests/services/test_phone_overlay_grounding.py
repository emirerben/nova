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
    duration_s: float | None = None,
) -> dict:
    return {
        "id": id_,
        "gcs_path": f"users/u1/plan/item1/pool/{id_}.jpg",
        "gcs_generation": generation,
        "kind": kind,
        "source_filename": filename,
        "duration_s": duration_s if duration_s is not None else (5.0 if kind == "video" else None),
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
    # KRI-183: clip_path=None means face sampling never ran even though there
    # is a card to place (face_sampling == "skipped") -- the conservative
    # fallback face box is protected instead of trusting the default corner,
    # so the card is shrunk into the opposite upper corner rather than
    # sitting at the untouched default spot.
    assert card.x_frac == 0.8
    assert card.y_frac == 0.14
    assert card.scale == 0.198
    assert card.fade is True
    assert card.kind == "image"

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
        {"media_id": "v1", "label": "clip.mp4", "reason": "video_not_supported", "kind": "video"}
    ]


def test_video_supported_matched_video_becomes_card_capped_by_freeze_rule(monkeypatch):
    """KRI-183: `video_supported=True` widens the candidate pool to videos --
    a matched video joins the pipeline like an image, and its card's window
    is capped by `build_suggestions`' own freeze-allowance rule (footage
    duration + 1.0s), not just the generic pacing cap."""
    video = _asset("v1", kind="video", filename="clip.mp4", duration_s=2.0)
    _patch_assets(monkeypatch, [video])
    monkeypatch.setattr(pg.settings, "gemini_api_key", None, raising=False)
    monkeypatch.setattr(
        pg,
        "heuristic_match",
        lambda *a, **k: [_placement("v1", start_s=3.0, end_s=8.0, tier="confident")],
    )

    result = pg.ground_phone_subtitled_overlays(
        _open_session,
        job_id=str(uuid.uuid4()),
        words=WORDS,
        duration_s=15.0,
        clip_path=None,
        video_supported=True,
    )

    assert len(result.cards) == 1
    card = result.cards[0]
    assert card.media_id == "v1"
    assert card.kind == "video"
    assert card.source_start_s == 0.0
    # Freeze allowance: window <= asset duration (2.0s) + 1.0s, tighter here
    # than the generic 4s pacing cap for this duration_s.
    assert card.end_s - card.start_s == 3.0

    assert result.receipt["unplaced"] == []
    placed = result.receipt["placed"][0]
    assert placed["media_id"] == "v1"
    assert placed["kind"] == "video"


def test_video_supported_unmatched_video_reported_no_spoken_match(monkeypatch):
    video = _asset("v1", kind="video", filename="clip.mp4")
    _patch_assets(monkeypatch, [video])
    monkeypatch.setattr(pg.settings, "gemini_api_key", None, raising=False)
    monkeypatch.setattr(pg, "heuristic_match", lambda *a, **k: [])

    result = pg.ground_phone_subtitled_overlays(
        _open_session,
        job_id=str(uuid.uuid4()),
        words=WORDS,
        duration_s=15.0,
        clip_path=None,
        video_supported=True,
    )

    assert result.cards == []
    assert result.receipt["unplaced"] == [
        {"media_id": "v1", "label": "clip.mp4", "reason": "no_spoken_match", "kind": "video"}
    ]


def test_video_supported_missing_generation_still_defensive(monkeypatch):
    video = _asset("v1", kind="video", filename="clip.mp4", generation=None)
    _patch_assets(monkeypatch, [video])
    monkeypatch.setattr(pg.settings, "gemini_api_key", None, raising=False)
    monkeypatch.setattr(pg, "heuristic_match", lambda *a, **k: [])

    result = pg.ground_phone_subtitled_overlays(
        _open_session,
        job_id=str(uuid.uuid4()),
        words=WORDS,
        duration_s=15.0,
        clip_path=None,
        video_supported=True,
    )

    assert result.cards == []
    assert result.receipt["unplaced"] == [
        {"media_id": "v1", "label": "clip.mp4", "reason": "missing_generation", "kind": "video"}
    ]


def test_agent_path_passes_video_kind_to_placement_asset(monkeypatch):
    """The agent path must tell `PlacementAsset` a Visual is a video -- the
    same field `app.tasks.autoplace.match_overlay_suggestions` has always
    sent for the cloud placement agent."""
    video = _asset("v1", kind="video", filename="clip.mp4", subject="dog running")
    _patch_assets(monkeypatch, [video])
    monkeypatch.setattr(pg.settings, "gemini_api_key", "fake-key", raising=False)

    captured: dict = {}

    def _fake_run(self, input, ctx=None):  # noqa: A002, ARG001
        captured["assets"] = list(input.assets)
        return OverlayPlacementOutput(placements=[], wishlist=[])

    monkeypatch.setattr(OverlayPlacementAgent, "run", _fake_run)

    pg.ground_phone_subtitled_overlays(
        _open_session,
        job_id=str(uuid.uuid4()),
        words=WORDS,
        duration_s=15.0,
        clip_path=None,
        video_supported=True,
    )

    assert len(captured["assets"]) == 1
    assert captured["assets"][0].asset_id == "v1"
    assert captured["assets"][0].kind == "video"


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
        {"media_id": "a1", "label": "photo.jpg", "reason": "hook_window", "kind": "image"}
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


def test_face_sampling_failure_is_fail_safe_not_fail_open(monkeypatch):
    """KRI-183: a broken sampler must not fall back to the risky default
    corner ("never assume there's no face") -- the conservative
    `_FALLBACK_FACE_BOX` is protected instead, and the card still gets
    placed (shrunk/moved), never omitted."""
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
    card = result.cards[0]
    box = _box_for_overlay(
        {"position": "custom", "x_frac": card.x_frac, "y_frac": card.y_frac, "scale": card.scale},
        footprint=MediaFootprint(aspect_ratio=1.0),
    )
    assert box.iou(pg._FALLBACK_FACE_BOX) <= pg._MAX_ARBITRATION_IOU
    # Moved/shrunk clear of the fallback face box, not sitting on the
    # untouched default spot (which does collide with it).
    assert (card.x_frac, card.y_frac) != (pg._DEFAULT_CARD_X_FRAC, pg._DEFAULT_CARD_Y_FRAC)


def test_face_sampling_ok_with_zero_faces_keeps_default_geometry(monkeypatch):
    """The "ok" path (OpenCV ran and found nothing) is untouched by KRI-183
    -- a confirmed-clear frame keeps the exact default corner, unlike the
    "failed"/"skipped" fallback above."""
    asset = _asset("a1", subject="screenshot")
    _patch_assets(monkeypatch, [asset])
    monkeypatch.setattr(pg.settings, "gemini_api_key", None, raising=False)
    monkeypatch.setattr(
        pg, "heuristic_match", lambda *a, **k: [_placement("a1", start_s=3.0, end_s=5.0)]
    )

    def _fake_sample_face_regions(*a, **k):
        return [], {"attempted": 3, "detected": 0}

    monkeypatch.setattr(pg, "sample_face_regions", _fake_sample_face_regions)

    result = pg.ground_phone_subtitled_overlays(
        _open_session,
        job_id=str(uuid.uuid4()),
        words=WORDS,
        duration_s=15.0,
        clip_path="/tmp/clip.mp4",
    )

    assert result.receipt["face_sampling"] == "ok"
    assert len(result.cards) == 1
    assert result.cards[0].x_frac == pg._DEFAULT_CARD_X_FRAC
    assert result.cards[0].y_frac == pg._DEFAULT_CARD_Y_FRAC


def test_two_time_overlapping_cards_get_distinct_geometry(monkeypatch):
    """Arbitration must not stack two cards whose windows overlap in time --
    `arbitrate_media_overlays`' own `occupied` collision list (not a
    KRI-183 change, just pinned here since both KRI-176 grounding cards
    always start from the SAME default spot)."""
    a1 = _asset("a1", subject="one")
    a2 = _asset("a2", subject="two")
    _patch_assets(monkeypatch, [a1, a2])
    monkeypatch.setattr(pg.settings, "gemini_api_key", None, raising=False)
    monkeypatch.setattr(
        pg,
        "heuristic_match",
        lambda *a, **k: [
            _placement("a1", start_s=0.9, end_s=2.9, tier="confident"),
            _placement("a2", start_s=1.0, end_s=3.0, tier="confident"),
        ],
    )

    result = pg.ground_phone_subtitled_overlays(
        _open_session, job_id=str(uuid.uuid4()), words=WORDS, duration_s=15.0, clip_path=None
    )

    assert len(result.cards) == 2
    # Confirm the windows genuinely overlap in time (hook-burst staggering
    # keeps them concurrent rather than sequential) -- otherwise distinct
    # geometry would be trivially true.
    first, second = result.cards
    assert first.start_s < second.end_s and second.start_s < first.end_s
    positions = {(c.x_frac, c.y_frac, c.scale) for c in result.cards}
    assert len(positions) == 2


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
        {"media_id": "a1", "label": "photo.jpg", "reason": "no_spoken_match", "kind": "image"}
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


# --- KRI-297: full-screen sequence ------------------------------------------


def _fs_ground(monkeypatch, assets, *, words, duration_s, video_supported=True):
    _patch_assets(monkeypatch, assets)
    # The sequence path must never consult the placement agent/heuristic.
    monkeypatch.setattr(
        pg, "_match_placements", Mock(side_effect=AssertionError("matcher must not run"))
    )
    return pg.ground_phone_subtitled_overlays(
        _open_session,
        job_id=str(uuid.uuid4()),
        words=words,
        duration_s=duration_s,
        clip_path=None,
        video_supported=video_supported,
        layout="fullscreen",
    )


def _talk_words(duration_s: float) -> list[dict]:
    # A word every 0.7s across the talk.
    out, t = [], 0.0
    while t < duration_s - 0.5:
        out.append(_word(f"w{len(out)}", round(t, 3), round(t + 0.4, 3)))
        t += 0.7
    return out


def test_fullscreen_places_all_22_visuals_non_overlapping_with_hook_free(monkeypatch):
    assets = [_asset(f"a{i}") for i in range(22)]
    grounded = _fs_ground(monkeypatch, assets, words=_talk_words(147.0), duration_s=147.0)
    assert len(grounded.cards) == 22
    assert grounded.receipt["layout"] == "fullscreen"
    assert len(grounded.receipt["placed"]) == 22 and grounded.receipt["unplaced"] == []
    assert all(card.display_mode == "fullscreen" for card in grounded.cards)
    assert [c.media_id for c in grounded.cards] == [f"a{i}" for i in range(22)]
    cards = sorted(grounded.cards, key=lambda c: c.start_s)
    assert cards[0].start_s >= 2.5 - 1e-6  # hook window stays the speaker
    assert cards[-1].end_s <= 146.0 + 1e-6  # last second free
    for earlier, later in zip(cards, cards[1:]):
        assert earlier.end_s <= later.start_s + 1e-6
    for card in cards:
        assert 1.5 - 1e-6 <= card.end_s - card.start_s <= 4.0 + 1e-6


def test_fullscreen_snaps_to_word_starts_else_uses_slot_time(monkeypatch):
    assets = [_asset(f"a{i}") for i in range(3)]
    words = _talk_words(30.0)
    snapped = _fs_ground(monkeypatch, assets, words=words, duration_s=30.0)
    starts = {w["start_s"] for w in words}
    assert all(any(abs(c.start_s - s) < 1e-3 for s in starts) for c in snapped.cards)
    plain = _fs_ground(monkeypatch, assets, words=[], duration_s=30.0)
    assert len(plain.cards) == 3  # no transcript: plain time placement
    assert all("evenly spaced" in p["reason"] for p in plain.receipt["placed"])


def test_fullscreen_too_many_visuals_drops_the_tail_with_an_honest_reason(monkeypatch):
    assets = [_asset(f"a{i}") for i in range(10)]
    # span = 8 - 2.5 - 1.0 = 4.5s -> 3 windows of 1.5s fit.
    grounded = _fs_ground(monkeypatch, assets, words=[], duration_s=8.0)
    assert [c.media_id for c in grounded.cards] == ["a0", "a1", "a2"]
    reasons = {u["media_id"]: u["reason"] for u in grounded.receipt["unplaced"]}
    assert set(reasons) == {f"a{i}" for i in range(3, 10)}
    assert set(reasons.values()) == {"no_room_in_timeline"}


def test_fullscreen_video_window_is_capped_to_its_footage_and_gated(monkeypatch):
    assets = [_asset("v1", kind="video", duration_s=2.0), _asset("p1")]
    grounded = _fs_ground(monkeypatch, assets, words=[], duration_s=60.0)
    video = next(c for c in grounded.cards if c.media_id == "v1")
    assert video.kind == "video" and video.end_s - video.start_s == 2.0
    off = _fs_ground(monkeypatch, assets, words=[], duration_s=60.0, video_supported=False)
    assert [c.media_id for c in off.cards] == ["p1"]
    assert off.receipt["unplaced"][0]["reason"] == "video_not_supported"


def test_pip_receipt_has_no_layout_key(monkeypatch):
    _patch_assets(monkeypatch, [_asset("a1")])
    grounded = pg.ground_phone_subtitled_overlays(
        _open_session, job_id=str(uuid.uuid4()), words=[], duration_s=10.0, clip_path=None
    )
    assert "layout" not in grounded.receipt


def test_a_face_filled_speaker_is_avoided_where_the_crop_draws_it(monkeypatch):
    """KRI-547: on a face-filled crop of a sideways clip, the speaker in the
    source's left third fills the middle of the canvas. The sampler is asked
    for RAW boxes and their eyes-nose-mouth core is mapped through the crop, so
    the card never touches the core (the unmapped source box would have left
    the default corner "clear" right on top of the face)."""
    from app.pipeline.phone_speaker_framing import face_core_mapper  # noqa: PLC0415
    from app.pipeline.phone_subtitled_plan import _STORY_CANVAS  # noqa: PLC0415

    asset = _asset("a1", subject="screenshot")
    _patch_assets(monkeypatch, [asset])
    monkeypatch.setattr(pg.settings, "gemini_api_key", None, raising=False)
    monkeypatch.setattr(
        pg, "heuristic_match", lambda *a, **k: [_placement("a1", start_s=3.0, end_s=5.0)]
    )
    raw_face = NormalizedBox(0.2, 0.15, 0.45, 0.6)
    calls: list[dict] = []

    def _fake_sample_face_regions(video_path, anchor_times_s, **kwargs):
        calls.append(kwargs)
        return [ProtectedRegion(0.0, float("inf"), raw_face, kind="face")], {
            "attempted": len(anchor_times_s),
            "detected": 1,
        }

    monkeypatch.setattr(pg, "sample_face_regions", _fake_sample_face_regions)
    mapper = face_core_mapper(
        display_width=1920, display_height=1080, canvas=_STORY_CANVAS, position_x=597.33
    )
    core_on_canvas = mapper(raw_face)

    unmapped = pg.ground_phone_subtitled_overlays(
        _open_session, job_id=str(uuid.uuid4()), words=WORDS, duration_s=15.0, clip_path="/c.mp4"
    )
    framed = pg.ground_phone_subtitled_overlays(
        _open_session,
        job_id=str(uuid.uuid4()),
        words=WORDS,
        duration_s=15.0,
        clip_path="/c.mp4",
        face_box_to_canvas=mapper,
    )

    assert "raw_boxes" not in calls[0]
    assert calls[1]["raw_boxes"] is True
    [corner] = unmapped.cards
    assert (corner.x_frac, corner.y_frac) == (pg._DEFAULT_CARD_X_FRAC, pg._DEFAULT_CARD_Y_FRAC)
    default_box = _box_for_overlay(
        {"position": "custom", "x_frac": corner.x_frac, "y_frac": corner.y_frac, "scale": 0.36},
        footprint=MediaFootprint(aspect_ratio=1.0),
    )
    assert default_box.intersection_area(core_on_canvas) > 0  # the corner IS on the face
    [card] = framed.cards
    box = _box_for_overlay(
        {"position": "custom", "x_frac": card.x_frac, "y_frac": card.y_frac, "scale": card.scale},
        footprint=MediaFootprint(aspect_ratio=1.0),
    )
    assert box.intersection_area(core_on_canvas) == 0


# KRI-547 follow-up: the Kadıköy take (T3). Raw boxes the OpenCV sampler found on the
# 568x320 analysis proxy at exactly the four anchors r4's card window samples
# ("'İlk durak' dediğinde kahve demleme videosunu köşede küçük göster", 7.72-12.753 s),
# and the face-fill shift the worker chose for the whole take.
_T3_FACES = {
    8.349: (0.2, 0.1778, 0.4625, 0.6444),
    9.607: (0.2229, 0.2074, 0.475, 0.6556),
    10.866: (0.1917, 0.2037, 0.4542, 0.6704),
    12.124: (0.2062, 0.1407, 0.4646, 0.6),
}
_T3_SHIFT = 508.44
_T3_BREW_ASPECT = 1080 / 1920  # kahve_demleme.mp4 is a vertical clip


def _t3_card_geometry(monkeypatch, faces: dict[float, tuple]):
    from app.pipeline.phone_speaker_framing import face_core_mapper  # noqa: PLC0415
    from app.pipeline.phone_subtitled_plan import _STORY_CANVAS  # noqa: PLC0415
    from app.services.phone_reaction_grounding import _PHOTO_SLOT  # noqa: PLC0415

    calls: list = []

    def sample(video_path, anchor_times_s, **kwargs):
        calls.append((list(anchor_times_s), kwargs))
        regions = [
            ProtectedRegion(max(0.0, at - 0.5), at + 0.5, NormalizedBox(*faces[at]), "face")
            for at in anchor_times_s
            if at in faces
        ]
        return regions, {"attempted": len(anchor_times_s), "detected": len(regions)}

    monkeypatch.setattr(pg, "sample_face_regions", sample)
    mapper = face_core_mapper(
        display_width=1920, display_height=1080, canvas=_STORY_CANVAS, position_x=_T3_SHIFT
    )
    overlay = {
        "id": "ilk-durak-video",
        "asset_id": "brew",
        "position": "custom",
        **_PHOTO_SLOT,
        "start_s": 7.72,
        "end_s": 12.753,
    }
    footprints = {"ilk-durak-video": MediaFootprint(aspect_ratio=_T3_BREW_ASPECT)}
    resolved, reasons, sampling = pg.resolve_phone_card_geometry(
        [overlay],
        clip_path="/tmp/kadikoy.mp4",
        job_id="t3",
        footprints_by_id=footprints,
        face_box_to_canvas=mapper,
    )
    return resolved, reasons, sampling, calls


def test_the_kadikoy_brewing_video_fits_the_corner_off_the_face_core(monkeypatch):
    from app.pipeline.phone_speaker_framing import face_core_box  # noqa: PLC0415
    from app.pipeline.phone_subtitled_plan import _STORY_CANVAS  # noqa: PLC0415
    from app.pipeline.phone_subtitled_title import source_box_to_canvas  # noqa: PLC0415

    resolved, reasons, sampling, calls = _t3_card_geometry(monkeypatch, _T3_FACES)

    [(anchors, kwargs)] = calls
    assert anchors == list(_T3_FACES)  # the fixture is every frame the window samples
    assert kwargs["raw_boxes"] is True
    assert (reasons, sampling) == ({}, "ok")
    card = resolved["ilk-durak-video"]
    # The top-right corner, small, flush against the frame edges.
    assert card["x_frac"] > 0.8 and card["y_frac"] < 0.2
    assert abs(card["scale"] - 0.36 * 0.55) < 1e-9
    box = _box_for_overlay(card, footprint=MediaFootprint(aspect_ratio=_T3_BREW_ASPECT))
    assert box.left >= 0 and box.right <= 1 and box.top >= 0
    for at, raw in _T3_FACES.items():
        core = source_box_to_canvas(
            face_core_box(NormalizedBox(*raw)),
            display_width=1920,
            display_height=1080,
            canvas=_STORY_CANVAS,
            position_x=_T3_SHIFT,
        )
        assert box.intersection_area(core) == 0, at


def test_a_face_core_that_fills_the_frame_still_leaves_no_safe_spot(monkeypatch):
    # The head fills the crop edge to edge and top to bottom: no corner is clear of
    # the eyes, nose and mouth, so the card is dropped rather than put on the face.
    huge = dict.fromkeys(_T3_FACES, (0.17, 0.0, 0.53, 0.75))
    resolved, reasons, _sampling, _calls = _t3_card_geometry(monkeypatch, huge)
    assert resolved == {}
    assert reasons == {"ilk-durak-video": "no_safe_spot"}
