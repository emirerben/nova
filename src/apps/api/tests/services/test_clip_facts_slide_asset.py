"""slide_asset_facts + SlidePostState capture projection (KRI-300)."""

from __future__ import annotations

from types import SimpleNamespace

from app.routes.plan_items import SlidePostStateAsset, _stored_asset_capture
from app.services.clip_facts import slide_asset_facts

CAPTURE = {
    "capture_time": "2025-06-01T10:30:00Z",
    "coarse_location": {"lat": 41.01, "lon": 28.98},
    "place": {"locality": "Istanbul", "country": "Türkiye"},
}


def _asset(capture=None, analysis=None):
    return SimpleNamespace(capture=capture, analysis=analysis)


def _pairs(facts):
    return [(f.kind, f.value, f.provenance) for f in facts]


def test_capture_becomes_time_and_place_facts() -> None:
    assert _pairs(slide_asset_facts(_asset(CAPTURE))) == [
        ("capture_time", "2025-06-01T10:30:00Z", "exif"),
        ("place", "Istanbul, Türkiye", "geocode"),
    ]


def test_no_capture_no_facts() -> None:
    assert slide_asset_facts(_asset()) == []
    assert slide_asset_facts(_asset(capture={})) == []


def test_malformed_stored_capture_is_no_capture() -> None:
    assert slide_asset_facts(_asset(capture={"capture_time": "garbage", "extra": 1})) == []
    assert slide_asset_facts(_asset(capture="nope")) == []


def test_country_only_place_is_not_a_place_fact() -> None:
    facts = slide_asset_facts(_asset({"place": {"country": "Türkiye"}}))
    assert facts == []


def test_understanding_facts_merge_without_duplicating_capture() -> None:
    analysis = {
        "clip_facts": [
            {"kind": "place", "value": "Elsewhere", "provenance": "geocode"},
            {"kind": "landmark", "value": "Galata Tower", "provenance": "inferred"},
        ],
    }
    facts = slide_asset_facts(_asset(CAPTURE, analysis))
    assert [f.kind for f in facts] == ["capture_time", "place", "landmark"]
    assert facts[1].value == "Istanbul, Türkiye"


def test_state_projection_returns_none_for_missing_or_bad_capture() -> None:
    assert _stored_asset_capture(_asset()) is None
    assert _stored_asset_capture(_asset({"capture_time": "garbage"})) is None
    assert _stored_asset_capture(SimpleNamespace(capture=CAPTURE)) is not None


def test_state_asset_capture_json_shape() -> None:
    asset = SlidePostStateAsset(
        id="a",
        kind="image",
        status="ready",
        media_status="ready",
        capture=_stored_asset_capture(_asset(CAPTURE)),
    )
    assert asset.model_dump(mode="json", exclude_none=True)["capture"] == CAPTURE
