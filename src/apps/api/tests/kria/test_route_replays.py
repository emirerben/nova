"""Every render route: does each planner-chosen field and each stated ask reach the render, or
the approval card before approval? (KRI-1097 wave 2)

Thin wrapper over the route scorecard (`scripts/kria/score_route.py`,
`make kria-replay FIXTURE=<route>`), one recorded or reconstructed draft per route under
`tests/fixtures/kria_replay/<route>/`. The card is assembled from the pure functions the draft
turn runs; no model, network or database.

Disclosure checks (kind ``route`` / ``disclosed_ok``) must pass. Render checks (kind
``honored``) are the lanes' targets and are red until a lane lands its route replay. Every red
check is listed in `tests/fixtures/kria_replay/lane_targets.json` with its lane; a red check
missing from the list fails, and so does a listed one that turned green (the lane removes it)
or one the fixture stopped grading (``must_grade``: restore it in the fixture's checks).
"""

from __future__ import annotations

import importlib.util
import json
import re
import socket
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[5]
FIXTURES = Path(__file__).resolve().parents[1] / "fixtures" / "kria_replay"
ROUTES = (
    "voiceover_montage",
    "talking_head",
    "narrated",
    "lipsync_song",
    "speech_excerpt",
    "slide_post",
)
_UUID = re.compile(r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}", re.I)


def _scorecard():
    spec = importlib.util.spec_from_file_location(
        "score_route", REPO / "scripts" / "kria" / "score_route.py"
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture(scope="module")
def reports():
    card = _scorecard()
    patcher = pytest.MonkeyPatch()

    def blocked(*_a, **_k):
        raise card._BASE.OfflineViolation("network access is blocked in the route replays")

    patcher.setattr(socket.socket, "connect", blocked)
    patcher.setattr(socket, "create_connection", blocked)
    try:
        yield {route: card.run_offline(FIXTURES / route) for route in ROUTES}
    finally:
        patcher.undo()


@pytest.mark.parametrize("route", ROUTES)
def test_negative_controls_tell_ignoring_from_honouring(reports, route) -> None:
    bad = [c for c in reports[route]["negative_controls"] if c["ok"] is not True]
    assert not bad, bad


@pytest.mark.parametrize("route", ROUTES)
def test_no_check_is_inconclusive(reports, route) -> None:
    broken = [
        (c["id"], c["why_it_fails"])
        for c in reports[route]["checks"]
        if c["verdict"] == "INCONCLUSIVE"
    ]
    assert not broken, broken


@pytest.mark.parametrize("route", ROUTES)
def test_fixture_is_scrubbed(route) -> None:
    for path in sorted((FIXTURES / route).glob("*.json")):
        text = path.read_text(encoding="utf-8")
        for needle in ("users/", "gs://", "X-Goog", "Signature=", "@gmail", "creation-threads"):
            assert needle not in text, (path.name, needle)
        for token in text.split('"'):
            assert not token.startswith("http") or "scrubbed.invalid" in token, (
                path.name,
                token[:80],
            )
        stray = {u for u in _UUID.findall(text) if u != "00000000-0000-4000-8000-000000000000"}
        assert not stray, (path.name, sorted(stray)[:3])


@pytest.mark.parametrize("route", ROUTES)
def test_disclosure_checks_pass(reports, route) -> None:
    report = reports[route]
    red = [
        (c["id"], c["why_it_fails"])
        for c in report["checks"]
        if c["kind"] in {"route", "disclosed_ok"}
        and not c["pass"]
        and str(c["id"]) not in report["lane_targets"]
    ]
    assert not red, red


@pytest.mark.parametrize("route", ROUTES)
def test_lane_targets_are_current(reports, route, record_property) -> None:
    report = reports[route]
    record_property("expected_red", ",".join(report["expected_red"]))
    assert not report["unexpected_red"], (
        "red checks not listed in lane_targets.json",
        report["unexpected_red"],
    )
    assert not report["missing_targets"], (
        "render targets the fixture no longer grades: restore them in its checks.honored "
        "(lane_targets.json must_grade lists every one, red or green)",
        report["missing_targets"],
    )
    assert not report["unexpected_green"], (
        "lane targets that now pass under the replay controls: remove them from "
        "lane_targets.json targets (keep them in must_grade)",
        report["unexpected_green"],
    )
    assert not report["exclusions"]["unexplained"], (
        "fields or asks the fixture's lists leave out with no reason in checks.not_graded",
        report["exclusions"]["unexplained"],
    )


@pytest.mark.parametrize("route", ROUTES)
def test_consumer_gate_is_report_only(reports, route) -> None:
    gate = next(c for c in reports[route]["checks"] if c["id"] == 10)
    assert gate["report_only"] is True
    assert gate["verdict"].startswith("REPORT"), gate["why_it_fails"]
    assert isinstance(gate["evidence"]["undisclosed"], list)
    json.dumps(gate["evidence"])


# The route each draft shape renders through, read off the worker's dispatch
# (`generative_build._run_generative_job_impl`, `_run_phone_subtitled_job`,
# `creator_render_contract.job_requires_camera_speech`), never off `resolve_route_id`. None: the
# worker refuses the shape, or it is a writer with no table (`voice_behind_footage`).
_KEPT = {"preserve_source_audio": True, "source_media_ids": ["c0", "c1"]}
_KEPT_ONE = {"preserve_source_audio": True, "source_media_ids": ["c0"]}
DISPATCH_MATRIX = (
    (
        "montage, music",
        {"edit_format": "montage", "audio_strategy": "licensed_music"},
        4,
        {},
        "unified_montage",
    ),
    (
        "montage, no audio strategy (schema default)",
        {"edit_format": "montage"},
        4,
        {},
        "unified_montage",
    ),
    (
        "day vlog, music",
        {"edit_format": "day_vlog", "audio_strategy": "licensed_music"},
        4,
        {},
        "unified_montage",
    ),
    (
        "creator song in the background",
        {"edit_format": "montage", "audio_strategy": "user_song", "song_sync": "background"},
        4,
        {},
        "unified_montage",
    ),
    (
        "creator song, lip sync",
        {"edit_format": "montage", "audio_strategy": "user_song", "song_sync": "lipsync"},
        4,
        {},
        "lipsync_song",
    ),
    (
        "kept camera sound, excerpts",
        {
            "edit_format": "montage",
            "audio_strategy": "licensed_music",
            "montage_audio": _KEPT,
            "voice_mode": "excerpts",
        },
        4,
        {},
        "speech_excerpt",
    ),
    (
        "kept camera sound per clip, stamped job",
        {
            "edit_format": "montage",
            "audio_strategy": "licensed_music",
            "montage_audio": _KEPT,
            "voice_mode": "per_clip",
        },
        4,
        {},
        "unified_montage",
    ),
    (
        "kept camera sound per clip, plan authority off (unstamped: voice_mode ignored)",
        {
            "edit_format": "montage",
            "audio_strategy": "licensed_music",
            "montage_audio": _KEPT,
            "voice_mode": "per_clip",
        },
        4,
        {"kria_plan_authority_enabled": False},
        "speech_excerpt",
    ),
    (
        "one clip's voice straight through, stamped job (voice_behind_footage)",
        {
            "edit_format": "montage",
            "audio_strategy": "original_audio",
            "montage_audio": _KEPT_ONE,
            "voice_mode": "continuous",
        },
        4,
        {},
        None,
    ),
    (
        "one clip's voice straight through, plan authority off",
        {
            "edit_format": "montage",
            "audio_strategy": "original_audio",
            "montage_audio": _KEPT_ONE,
            "voice_mode": "continuous",
        },
        4,
        {"kria_plan_authority_enabled": False},
        "speech_excerpt",
    ),
    (
        "kept camera sound, excerpts, spoken-excerpt writer off (it asks instead)",
        {
            "edit_format": "montage",
            "audio_strategy": "licensed_music",
            "montage_audio": _KEPT,
            "voice_mode": "excerpts",
        },
        4,
        {"speech_excerpt_montage_enabled": False},
        None,
    ),
    (
        "creator song, lip sync, creator songs off",
        {"edit_format": "montage", "audio_strategy": "user_song", "song_sync": "lipsync"},
        4,
        {"user_song_montage_enabled": False},
        None,
    ),
    (
        "creator song in the background, creator songs off",
        {"edit_format": "montage", "audio_strategy": "user_song", "song_sync": "background"},
        4,
        {"user_song_montage_enabled": False},
        None,
    ),
    (
        "montage with a recorded voiceover",
        {"edit_format": "montage", "audio_strategy": "voiceover"},
        4,
        {},
        "voiceover_montage",
    ),
    (
        "montage with a recorded voiceover and kept camera sound (two soundtracks refused)",
        {"edit_format": "montage", "audio_strategy": "voiceover", "montage_audio": _KEPT},
        4,
        {},
        None,
    ),
    (
        "narrated with a recorded voiceover and kept camera sound, camera sound supported",
        {"edit_format": "narrated", "audio_strategy": "voiceover", "montage_audio": _KEPT},
        6,
        {"phone_voiceover_source_audio_enabled": True},
        "narrated",
    ),
    (
        "narrated with a recorded voiceover and kept camera sound, camera sound off",
        {"edit_format": "narrated", "audio_strategy": "voiceover", "montage_audio": _KEPT},
        6,
        {"phone_voiceover_source_audio_enabled": False},
        None,
    ),
    (
        "subtitled, one clip",
        {"edit_format": "subtitled", "audio_strategy": "original_audio"},
        1,
        {},
        "talking_head",
    ),
    (
        "subtitled, three clips, multi-clip Talking off",
        {"edit_format": "subtitled", "audio_strategy": "original_audio"},
        3,
        {"talking_multi_clip_enabled": False},
        None,
    ),
    (
        "narrated with a recorded voiceover",
        {"edit_format": "narrated", "audio_strategy": "voiceover"},
        6,
        {},
        "narrated",
    ),
    (
        "narrated self-narration, one clip",
        {"edit_format": "narrated", "audio_strategy": "original_audio"},
        1,
        {},
        "talking_head",
    ),
    (
        "narrated self-narration, three clips, phone Talking head on",
        {"edit_format": "narrated", "audio_strategy": "original_audio"},
        3,
        {"phone_talking_head_rendering_enabled": True},
        "talking_head",
    ),
    (
        "narrated self-narration, three clips, phone Talking head off",
        {"edit_format": "narrated", "audio_strategy": "original_audio"},
        3,
        {"phone_talking_head_rendering_enabled": False},
        None,
    ),
    (
        "talking_head format (no phone writer)",
        {"edit_format": "talking_head", "audio_strategy": "original_audio"},
        2,
        {},
        None,
    ),
    ("slides", {"edit_format": "slides", "audio_strategy": "licensed_music"}, 5, {}, "slide_post"),
)


@pytest.mark.parametrize(
    ("strategy", "clips", "flags", "dispatched"),
    [case[1:] for case in DISPATCH_MATRIX],
    ids=[case[0] for case in DISPATCH_MATRIX],
)
def test_the_card_reads_the_route_the_worker_dispatches(strategy, clips, flags, dispatched) -> None:
    from unittest.mock import patch

    from app.config import settings
    from app.kria.route_capabilities import resolve_route_id

    card = _scorecard()
    paths = [f"users/synthetic/threads/synthetic/analysis-proxy-ios-c{i}.mp4" for i in range(clips)]
    with (
        card._prod_flags(),
        patch.multiple(settings, **flags) if flags else card.contextlib.nullcontext(),
    ):
        route = resolve_route_id(
            creator_id=card.CREATOR_ID,
            edit_format=strategy["edit_format"],
            strategy=strategy,
            clip_paths=paths,
        )
    assert (None if route is None else str(route)) == dispatched
