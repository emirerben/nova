"""KRI-498 duration ownership coverage against the public strategy vocabulary."""

import json
from pathlib import Path

import pytest

from app.agents._schemas.edit_format import EDIT_FORMATS
from app.kria.brief import BriefRequirement, CreativeBrief
from app.services.choice_questions import CONFLICT_LENGTH_VS_NATURAL, collect_conflicts
from app.services.creator_render_contract import build_render_contract
from app.services.length_policy import (
    KNOWN_AUDIO_SOURCES,
    NATURAL_OWNER_DECLARATION,
    length_owner,
    natural_length_owner,
)


@pytest.mark.parametrize(
    ("edit_format", "audio_strategy", "expected_owner"),
    tuple(
        (edit_format, audio_strategy, owner)
        for edit_format, sources in NATURAL_OWNER_DECLARATION.items()
        for audio_strategy, owner in sources.items()
    ),
)
def test_every_declared_edit_audio_pair_has_its_declared_natural_owner(
    edit_format: str, audio_strategy: str, expected_owner: str
) -> None:
    assert (
        natural_length_owner({"edit_format": edit_format, "audio_strategy": audio_strategy})
        == expected_owner
    )


def test_every_public_format_and_audio_source_has_an_explicit_declaration() -> None:
    assert set(EDIT_FORMATS) == set(NATURAL_OWNER_DECLARATION)
    assert all(
        set(sources) == set(KNOWN_AUDIO_SOURCES) for sources in NATURAL_OWNER_DECLARATION.values()
    )


def test_unknown_edit_or_audio_owner_fails_closed() -> None:
    with pytest.raises(ValueError, match="edit format"):
        natural_length_owner({"edit_format": "future_format", "audio_strategy": "licensed_music"})
    with pytest.raises(ValueError, match="audio source"):
        natural_length_owner({"edit_format": "montage", "audio_strategy": "future_audio"})


@pytest.mark.parametrize(
    ("strategy", "expected"),
    (
        ({"target_duration_s": 24}, "footage"),  # default 24
        ({"target_duration_s": 24, "target_duration_requested": True}, "footage"),
        ({"target_duration_s": 62, "target_duration_requested": True}, "footage"),  # model-derived
        ({"target_duration_s": 24, "creator_duration_s": 24}, "creator"),  # explicit 24
    ),
)
def test_provenance_not_the_model_target_selects_creator_owner(strategy, expected) -> None:
    assert (
        length_owner(strategy, creator_duration_proven="creator_duration_s" in strategy) == expected
    )
    contract = build_render_contract(strategy, generation_id="g")
    assert contract is not None
    assert contract.duration_s == (24 if expected == "creator" else None)


@pytest.mark.parametrize(
    "strategy",
    (
        {"audio_strategy": "user_song"},
        {"audio_strategy": "voiceover"},
        {"audio_strategy": "original_audio", "edit_format": "subtitled"},
    ),
    ids=("song", "voiceover", "take"),
)
def test_explicit_creator_length_overrides_natural_source_owner(strategy) -> None:
    explicit = {**strategy, "target_duration_s": 30, "creator_duration_s": 30}
    assert natural_length_owner(explicit) in {"song", "voiceover", "take"}
    assert length_owner(explicit, creator_duration_proven=True) == "creator"
    contract = build_render_contract(explicit, generation_id="g")
    assert contract is not None and contract.duration_s == 30


@pytest.mark.parametrize(
    ("requested", "source", "within_tolerance"),
    ((22, 20, True), (20, 22, True), (24, 23, True), (20, 23, False)),
)
def test_named_length_capacity_tolerance_is_ten_percent(
    requested: int, source: int, within_tolerance: bool
) -> None:
    brief = CreativeBrief(
        requirements=[
            BriefRequirement(
                id="length",
                kind="timing",
                scope="global",
                description=f"Keep {requested} seconds",
                facts={"duration_s": requested},
            )
        ]
    )
    conflicts = collect_conflicts(
        {"audio_strategy": "user_song"}, brief, {"song_duration_s": source}
    )
    asked = any(conflict.kind == CONFLICT_LENGTH_VS_NATURAL for conflict in conflicts)
    assert asked is not within_tolerance


@pytest.mark.parametrize(
    "strategy",
    (
        {"selected_media_ids": ["a", "b"]},
        {"ordering_choice": "chronological", "selected_media_ids": ["a", "b"]},
        {"media_scope": "all", "pinned_texts": [{"text": "Always", "corner": "top"}]},
    ),
    ids=("included", "ordered", "use-all-with-pin"),
)
def test_clip_scope_order_and_pins_do_not_change_footage_ownership(strategy) -> None:
    assert natural_length_owner(strategy) == "footage"


@pytest.mark.parametrize(
    ("incident", "strategy", "owner"),
    (
        ("887b683a", {"audio_strategy": "user_song"}, "song"),
        ("30f1c7d2", {"audio_strategy": "user_song", "target_duration_s": 71}, "song"),
        ("e1c5f89e", {"edit_format": "subtitled", "audio_strategy": "original_audio"}, "take"),
    ),
)
def test_incident_shapes_keep_their_natural_owner(incident, strategy, owner) -> None:
    assert natural_length_owner(strategy) == owner, incident


def test_captured_incident_shapes_are_minimized_and_provenanced() -> None:
    fixture = Path(__file__).parents[5] / "scripts/ci/fixtures/kri492-incident-shapes.json"
    payload = json.loads(fixture.read_text())
    captures = payload["captures"]
    assert payload["capture_count"] == 3
    assert {capture["shape"]["incident_id"] for capture in captures} == {
        "887b683a",
        "30f1c7d2",
        "e1c5f89e",
    }
    for capture in captures:
        provenance = capture["source_provenance"]
        assert provenance["captured_at"]
        assert len(provenance["source_sha256"]) == 64
        assert provenance["projection"] == "kri492_incident_shape_v1"
        assert provenance["text_qualification"] == "no_creator_payload"
        encoded = json.dumps(capture, sort_keys=True)
        assert all(
            secret not in encoded for secret in ("creator_request", "clip_paths", "output_url")
        )


@pytest.mark.parametrize(
    ("incident", "expected_owner", "expected_duration"),
    (("887b683a", "song", 62.0), ("30f1c7d2", "song", 71.0), ("e1c5f89e", "take", 40.0)),
)
def test_captured_incidents_preserve_duration_owner_evidence(
    incident: str, expected_owner: str, expected_duration: float
) -> None:
    fixture = Path(__file__).parents[5] / "scripts/ci/fixtures/kri492-incident-shapes.json"
    captures = json.loads(fixture.read_text())["captures"]
    shape = next(
        capture["shape"] for capture in captures if capture["shape"]["incident_id"] == incident
    )
    strategy = {
        "edit_format": shape["edit_format"],
        "audio_strategy": shape["audio_strategy"],
        "song_sync": shape["song_sync"],
        "target_duration_s": shape["target_duration_s"],
        "target_duration_requested": shape["target_duration_requested"],
    }
    assert natural_length_owner(strategy) == expected_owner
    assert shape["target_duration_s"] == expected_duration
    assert shape["target_duration_requested"] is True
