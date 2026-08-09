from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace

import pytest

from app.agents._schemas.music_labels import CURRENT_LABEL_VERSION
from app.agents._schemas.song_sections import CURRENT_SECTION_VERSION
from app.agents._schemas.sound_effect import SoundEffectPlacement
from app.pipeline.captions import generate_ass_from_cues, prepare_smart_caption_cues
from app.pipeline.reframe import _build_video_filter
from app.pipeline.render_geometry import (
    MediaFootprint,
    NormalizedBox,
    ProtectedRegion,
    arbitrate_media_overlays,
    sample_face_regions,
)
from app.pipeline.sound_effects import (
    MusicBedTreatment,
    apply_smart_audio_treatment,
    build_sound_effects_command,
)
from app.pipeline.text_overlay_skia import _typewriter_visible_text_at, measure_text_overlay_box
from app.smart_edit.compiler import compile_smart_plan
from app.smart_edit.planner import plan_smart_captions
from app.smart_edit.presets import load_preset
from app.tasks.generative_build import (
    _effective_smart_caption_policy,
    _load_smart_caption_assets_fail_open,
    _should_compose_subtitled_final,
    _smart_music_track_eligible,
    _text_element_burn_dicts,
)


def _cue(text: str, start_s: float, end_s: float) -> dict:
    tokens = text.split()
    step = (end_s - start_s) / len(tokens)
    return {
        "text": text,
        "start_s": start_s,
        "end_s": end_s,
        "words": [
            {
                "text": token,
                "start_s": start_s + index * step,
                "end_s": start_s + (index + 1) * step,
            }
            for index, token in enumerate(tokens)
        ],
    }


def test_v2_caption_policy_is_measured_two_line_and_reburn_stable(tmp_path) -> None:
    policy = load_preset("cigdem", "v2").caption.model_dump(mode="json")
    cues = [
        _cue(
            "Markalar aslında karakterlerle soyut fikirleri somut ve unutulmaz hale getiriyor",
            1.0,
            4.0,
        )
    ]

    prepared = prepare_smart_caption_cues(cues, policy)
    box = prepared[0]["smart_render_box"]
    assert 1 <= len(prepared[0]["smart_render_lines"]) <= 2
    assert box["right"] - box["left"] <= policy["width_frac"] + 0.001
    assert 0.0 <= box["top"] < box["bottom"] <= 1.0
    assert box["bottom"] == policy["y_frac"]

    initial = tmp_path / "initial.ass"
    reburn = tmp_path / "reburn.ass"
    generate_ass_from_cues(prepared, str(initial), smart_policy=policy, pop_in=True)
    generate_ass_from_cues(prepared, str(reburn), smart_policy=policy, pop_in=True)
    assert initial.read_bytes() == reburn.read_bytes()
    rendered = initial.read_text()
    assert f"{policy['font_family']},{policy['font_size_px']}" in rendered
    assert "\\N" in rendered


def test_smart_caption_policy_without_user_edits_keeps_pinned_preset() -> None:
    """Negative path: absent the *_user_edited flags, the reburn's ass_font and
    margin_v must NOT leak into the pinned Smart policy."""
    policy = load_preset("cigdem", "v2").caption.model_dump(mode="json")

    effective = _effective_smart_caption_policy(
        {"smart_caption_policy": policy},
        ass_font="TikTok Sans",
        margin_v=400,
    )

    assert effective is not None
    assert effective["font_family"] == policy["font_family"]
    assert effective["y_frac"] == policy["y_frac"]
    assert _effective_smart_caption_policy({}, ass_font="TikTok Sans", margin_v=None) is None


def test_smart_caption_policy_honors_explicit_creator_font_and_position() -> None:
    policy = load_preset("cigdem", "v2").caption.model_dump(mode="json")

    effective = _effective_smart_caption_policy(
        {
            "smart_caption_policy": policy,
            "caption_font_user_edited": True,
            "caption_position_user_edited": True,
        },
        ass_font="TikTok Sans",
        margin_v=round((1.0 - 0.66) * 1920),
    )

    assert effective is not None
    assert effective["font_family"] == "TikTok Sans"
    assert effective["y_frac"] == pytest.approx(0.66, abs=0.001)


def test_shared_geometry_moves_or_omits_decorative_media() -> None:
    protected = [NormalizedBox(0.0, 0.0, 1.0, 0.72)]
    overlays = [
        {
            "id": "decorative",
            "display_mode": "pip",
            "x_frac": 0.5,
            "y_frac": 0.5,
            "scale": 0.3,
        }
    ]
    resolved, receipts = arbitrate_media_overlays(overlays, protected_boxes=protected)
    if resolved:
        box = NormalizedBox(**resolved[0]["smart_layout_box"])
        assert box.iou(protected[0]) <= 0.02
    else:
        assert receipts[0]["decision"] == "omitted_no_safe_candidate"


def test_lower_band_rescues_card_blocked_out_of_the_top() -> None:
    # Talk-to-camera: face + chapter heading protect the whole top half. The
    # card must land in the lower band, not die as no_safe_candidate.
    protected = [NormalizedBox(0.0, 0.0, 1.0, 0.6)]
    overlays = [
        {
            "id": "player-photo",
            "display_mode": "pip",
            "position": "top",
            "scale": 0.3,
            "start_s": 5.0,
            "end_s": 8.0,
        }
    ]

    resolved, receipts = arbitrate_media_overlays(overlays, protected_boxes=protected)

    assert receipts[0]["decision"] in {"moved", "shrunk"}
    box = NormalizedBox(**resolved[0]["smart_layout_box"])
    assert box.top >= 0.55
    assert box.iou(protected[0]) <= 0.02


def test_face_protection_clamps_giant_detections_and_keeps_top_thin() -> None:
    from app.pipeline.render_geometry import _face_protection_box

    # Real krat.mp4 detection: Haar returned a "face" spanning 71% of the
    # frame width. The protection box must clamp to a plausible head and keep
    # the band above the hairline free for corner cards.
    raw = NormalizedBox(0.074, 0.177, 0.781, 0.575)

    box = _face_protection_box(raw)

    assert box.width <= 0.55 + 0.12 + 1e-9  # clamped width + side padding
    assert box.top >= raw.top - 0.02 - 1e-9  # thin top padding only
    assert box.bottom >= raw.bottom  # chin stays protected
    # A sane detection is padded but not clamped.
    sane = _face_protection_box(NormalizedBox(0.3, 0.2, 0.7, 0.55))
    assert abs(sane.left - 0.24) < 1e-9 and abs(sane.right - 0.76) < 1e-9


def test_same_asset_never_renders_twice_at_once() -> None:
    overlays = [
        {
            "id": card_id,
            "display_mode": "pip",
            "position": "custom",
            "x_frac": x,
            "y_frac": 0.8,
            "scale": 0.25,
            "start_s": 2.0,
            "end_s": 9.0,
            "src_gcs_path": "pool/spain-flag.png",
        }
        for card_id, x in (("hook-copy", 0.2), ("pair-copy", 0.8))
    ]

    resolved, receipts = arbitrate_media_overlays(overlays, protected_boxes=[])

    assert len(resolved) == 1
    assert receipts[1]["decision"] == "omitted_duplicate_asset"


def test_displaced_card_claims_the_nearest_free_corner() -> None:
    # Bottom-right card whose whole corner zone is blocked must claim the
    # nearest FREE true corner (bottom-left here) — never the center column.
    protected = [NormalizedBox(0.55, 0.72, 1.0, 0.95)]
    overlays = [
        {
            "id": "corner-card",
            "display_mode": "pip",
            "position": "custom",
            "x_frac": 0.81,
            "y_frac": 0.81,
            "scale": 0.25,
            "start_s": 0.0,
            "end_s": 4.0,
        }
    ]

    resolved, receipts = arbitrate_media_overlays(overlays, protected_boxes=protected)

    assert receipts[0]["decision"] in {"moved", "shrunk"}
    box = NormalizedBox(**resolved[0]["smart_layout_box"])
    x_center = (box.left + box.right) / 2
    assert x_center < 0.35  # nearest free corner column
    assert box.top > 0.6  # stayed in the bottom band


def test_displaced_corner_card_claims_free_corner_not_center() -> None:
    # France scenario: assigned lower-left, but the lower-left band is
    # blocked and lower-right is occupied. The card must take a free CORNER
    # (top-left here), never park in the center column under the captions.
    protected = [NormalizedBox(0.0, 0.3, 0.45, 1.0)]
    overlays = [
        {
            "id": "occupies-lower-right",
            "display_mode": "pip",
            "position": "custom",
            "x_frac": 0.81,
            "y_frac": 0.81,
            "scale": 0.25,
            "start_s": 0.0,
            "end_s": 6.0,
        },
        {
            "id": "displaced-lower-left",
            "display_mode": "pip",
            "position": "custom",
            "x_frac": 0.2,
            "y_frac": 0.81,
            "scale": 0.25,
            "start_s": 0.0,
            "end_s": 6.0,
        },
    ]

    resolved, receipts = arbitrate_media_overlays(overlays, protected_boxes=protected)

    assert len(resolved) == 2
    box = NormalizedBox(**resolved[1]["smart_layout_box"])
    x_center = (box.left + box.right) / 2
    assert x_center < 0.35 or x_center > 0.65  # a corner column, not center
    assert box.bottom < 0.3  # the free corner is up top


def test_shared_geometry_keeps_simultaneous_cards_from_stacking() -> None:
    overlays = [
        {
            "id": card_id,
            "display_mode": "pip",
            "position": "custom",
            "x_frac": 0.5,
            "y_frac": 0.5,
            "scale": 0.3,
            "start_s": 0.0,
            "end_s": 3.0,
        }
        for card_id in ("first", "second")
    ]

    resolved, _receipts = arbitrate_media_overlays(overlays, protected_boxes=[])

    assert len(resolved) == 2
    first = NormalizedBox(**resolved[0]["smart_layout_box"])
    second = NormalizedBox(**resolved[1]["smart_layout_box"])
    assert first.iou(second) <= 0.02


def test_shared_geometry_ignores_protected_regions_outside_card_interval() -> None:
    overlay = {
        "id": "early-card",
        "display_mode": "pip",
        "position": "custom",
        "x_frac": 0.5,
        "y_frac": 0.5,
        "scale": 0.3,
        "start_s": 0.0,
        "end_s": 2.0,
    }
    later_caption = ProtectedRegion(
        start_s=8.0,
        end_s=10.0,
        box=NormalizedBox(0.25, 0.35, 0.75, 0.65),
        kind="caption",
    )

    resolved, receipts = arbitrate_media_overlays(
        [overlay],
        protected_boxes=[later_caption],
    )

    assert resolved[0]["x_frac"] == pytest.approx(0.5)
    assert resolved[0]["y_frac"] == pytest.approx(0.5)
    assert receipts[0]["decision"] == "kept"


def test_shared_geometry_uses_real_aspect_and_alpha_footprint() -> None:
    overlay = {
        "id": "wide-transparent-card",
        "display_mode": "pip",
        "position": "custom",
        "x_frac": 0.5,
        "y_frac": 0.5,
        "scale": 0.4,
        "start_s": 0.0,
        "end_s": 2.0,
    }
    footprint = MediaFootprint(
        aspect_ratio=4.0,
        opaque_bounds=NormalizedBox(0.25, 0.0, 0.75, 1.0),
    )

    resolved, _receipts = arbitrate_media_overlays(
        [overlay],
        protected_boxes=[],
        footprints_by_id={"wide-transparent-card": footprint},
    )

    box = NormalizedBox(**resolved[0]["smart_layout_box"])
    assert box.width == pytest.approx(0.2)
    assert box.height == pytest.approx(0.4 * 1080 / 1920 / 4.0)


def test_media_overlay_persistence_keeps_failed_cards_drops_omitted() -> None:
    """Review D4 + delta finding: a transient download failure must not
    permanently delete a card (persist it for retry), but a card the
    arbitration DELIBERATELY omitted must NOT persist — the reburn path
    applies persisted cards without arbitration, so it would resurrect the
    exact occlusion the omission prevented."""
    from app.agents._schemas.media_overlay import MediaOverlay
    from app.tasks.generative_build import _merged_media_overlay_persistence

    requested = [
        MediaOverlay.model_validate(
            {
                "id": card_id,
                "kind": "video",
                "src_gcs_path": f"users/u/plan/p/overlays/{card_id}.mp4",
                "position": "custom",
                "x_frac": 0.5,
                "y_frac": 0.5,
                "scale": 0.3,
                "start_s": 0.0,
                "end_s": 3.0,
            }
        )
        for card_id in ("survivor", "failed-download", "omitted-collision")
    ]
    applied = [
        {
            "id": "survivor",
            "kind": "video",
            "src_gcs_path": "users/u/plan/p/overlays/survivor.mp4",
            "position": "custom",
            "x_frac": 0.2,
            "y_frac": 0.14,
            "scale": 0.3,
            "start_s": 0.0,
            "end_s": 3.0,
        }
    ]
    layout_receipts = [
        {"id": "survivor", "decision": "moved"},
        {"id": "omitted-collision", "decision": "omitted_no_safe_candidate"},
    ]

    merged = _merged_media_overlay_persistence(requested, applied, layout_receipts)

    assert [card["id"] for card in merged] == ["survivor", "failed-download"]
    # The survivor persists its arbitration-resolved geometry…
    assert merged[0]["x_frac"] == 0.2
    # …the download-failed card keeps its original compiled payload for retry…
    assert merged[1]["x_frac"] == 0.5
    # …and the deliberately-omitted card is gone from persistence entirely.
    assert all(card["id"] != "omitted-collision" for card in merged)


def test_smart_titles_force_text_caption_compositor_when_public_lane_is_off(monkeypatch) -> None:
    monkeypatch.setattr(
        "app.tasks.generative_build.settings.subtitled_text_lane_enabled",
        False,
    )

    assert _should_compose_subtitled_final(
        {
            "resolved_archetype": "subtitled",
            "text_elements_materialized_from": "smart_captions",
            "text_elements": [{"id": "chapter-1", "text": "1 - somutlastirma"}],
        }
    )


def test_skia_title_measurement_uses_renderer_geometry() -> None:
    box = measure_text_overlay_box(
        {
            "text": "1 - somutlaştırma",
            "font_family": "Montserrat",
            "text_size_px": 92,
            "position_x_frac": 0.5,
            "position_y_frac": 0.62,
            "max_width_frac": 0.72,
            "text_anchor": "center",
            "vertical_anchor": "center",
        }
    )
    assert 0.0 <= box["left"] < box["right"] <= 1.0
    assert 0.0 <= box["top"] < box["bottom"] <= 1.0


def test_v2_typewriter_visual_and_keyboard_ticks_share_schedule() -> None:
    planned = plan_smart_captions(
        [
            _cue("Dört başlıkta anlatayım: 1.", 0.0, 2.0),
            _cue("Somutlaştırma markayı unutulmaz yapar.", 2.1, 4.5),
        ],
        preset_version="v2",
        language="tr",
        use_agent=False,
    )
    assert planned is not None
    compiled = compile_smart_plan(planned.document, planned.caption_cues)
    keyword = next(element for element in compiled.text_elements if not element["text"].isdigit())
    schedule = keyword["reveal_schedule_s"]
    ticks = [
        intent for intent in compiled.sfx_intents if intent["role"] == "keyword_typewriter_tick"
    ]
    assert 2 <= len(schedule) <= 12
    assert [tick["at_s"] for tick in ticks] == schedule
    assert all(right - left >= 0.08 for left, right in zip(schedule, schedule[1:]))
    burn = _text_element_burn_dicts(
        {"text_elements": compiled.text_elements, "duration_s": 6.0, "lyrics_baked": False}
    )
    rendered_keyword = next(overlay for overlay in burn if overlay.get("effect") == "typewriter")
    assert rendered_keyword["reveal_schedule_s"] == schedule
    assert schedule[-1] == pytest.approx(keyword["start_s"] + 0.68, abs=0.001)


def test_v3_generated_text_style_and_fast_schedule_are_preset_owned() -> None:
    preset = load_preset("cigdem", "v3")
    for role in ("context_title", "section_keyword"):
        style = preset.text_styles[role]
        assert style.color == "#FFF3A6"
        assert style.stroke_width == 0
        assert style.typewriter_reveal_duration_s == 0.25
    # The existing subtle shadow is compiler-owned and remains enabled.
    planned = plan_smart_captions(
        [
            _cue("Dört başlıkta anlatayım: 1.", 0.0, 2.0),
            _cue("Somutlaştırma markayı unutulmaz yapar.", 2.1, 4.5),
        ],
        preset_version="v3",
        language="tr",
        use_agent=False,
    )
    assert planned is not None
    compiled = compile_smart_plan(planned.document, planned.caption_cues)
    keyword = next(element for element in compiled.text_elements if not element["text"].isdigit())
    schedule = keyword["source_params"]["reveal_schedule_s"]
    ticks = [
        intent for intent in compiled.sfx_intents if intent["role"] == "keyword_typewriter_tick"
    ]
    assert keyword["color"] == "#FFF3A6"
    assert keyword["stroke_width"] == 0
    assert keyword["shadow_enabled"] is True
    assert keyword["reveal_s"] == pytest.approx(keyword["start_s"] + 0.25)
    assert schedule[-1] == pytest.approx(keyword["start_s"] + 0.25, abs=0.001)
    assert [tick["at_s"] for tick in ticks] == schedule


def test_renderer_matches_shared_absolute_typewriter_schedule_fixture() -> None:
    fixture_path = (
        Path(__file__).parents[5]
        / "tests"
        / "fixtures"
        / "overlay-animation"
        / "typewriter_schedule.json"
    )
    fixture = json.loads(fixture_path.read_text(encoding="utf-8"))

    for sample in fixture["samples"]:
        assert (
            _typewriter_visible_text_at(
                fixture["text"],
                t_local=sample["t_local"],
                raw_schedule=fixture["reveal_schedule_s"],
                start_s=fixture["start_s"],
            )
            == sample["visible_text"]
        )


def test_music_and_sfx_share_one_voice_safe_stream_copy_graph() -> None:
    effect = SoundEffectPlacement(
        id="tick",
        src_gcs_path="sound-effects/tick/audio.wav",
        at_s=1.0,
        gain=0.4,
    )
    bed = MusicBedTreatment(
        track_id="track",
        src_gcs_path="music/track/audio.mp3",
        section_start_s=4.0,
        section_end_s=24.0,
    )
    cmd = build_sound_effects_command(
        "/base.mp4",
        [effect],
        ["/tick.wav"],
        "/out.mp4",
        music_bed=bed,
        music_local_path="/music.mp3",
    )
    graph = " ".join(cmd)
    assert "sidechaincompress" in graph
    assert "loudnorm=I=-14:TP=-1.5" in graph
    assert cmd[cmd.index("-c:v") + 1] == "copy"


def test_music_eligibility_is_closed_by_freshness_not_license() -> None:
    """Eligibility is purely analysis freshness + curation (archived_at) — no
    license flag, no publish-state requirement. Archiving in /admin/music is
    the only remaining curation lever."""
    valid = SimpleNamespace(
        analysis_status="ready",
        published_at=None,
        archived_at=None,
        audio_gcs_path="music/track/audio.mp3",
        ai_labels={"labels": {}},
        label_version=CURRENT_LABEL_VERSION,
        best_sections=[{"rank": 1, "start_s": 2.0, "end_s": 20.0}],
        section_version=CURRENT_SECTION_VERSION,
        track_config={},
    )
    assert _smart_music_track_eligible(valid)
    for field, value in (
        ("analysis_status", "failed"),
        ("archived_at", datetime.now(UTC)),
        ("audio_gcs_path", None),
        ("ai_labels", None),
        ("label_version", "stale"),
        ("best_sections", None),
        ("section_version", "stale"),
    ):
        candidate = SimpleNamespace(**vars(valid))
        setattr(candidate, field, value)
        assert not _smart_music_track_eligible(candidate)


def test_music_eligibility_ignores_published_at_and_license_flag() -> None:
    """Pin: an unpublished, unlicensed, but ready+labeled+sectioned track is
    eligible — regression guard for the license/publish clauses removed from
    `_smart_music_track_eligible`."""
    track = SimpleNamespace(
        analysis_status="ready",
        published_at=None,
        archived_at=None,
        audio_gcs_path="music/track/audio.mp3",
        ai_labels={"labels": {}},
        label_version=CURRENT_LABEL_VERSION,
        best_sections=[{"rank": 1, "start_s": 2.0, "end_s": 20.0}],
        section_version=CURRENT_SECTION_VERSION,
        track_config={"smart_captions_licensed": False},
    )
    assert _smart_music_track_eligible(track)


def test_audio_treatment_retries_full_then_sfx_only(monkeypatch, tmp_path) -> None:
    effect = SoundEffectPlacement(
        id="tick",
        src_gcs_path="sound-effects/tick/audio.wav",
        at_s=1.0,
        gain=0.4,
    )
    calls: list[list[str]] = []

    def download(_gcs: str, local: str) -> None:
        tmp_path.joinpath(local.split("/")[-1]).write_bytes(b"audio")
        with open(local, "wb") as handle:
            handle.write(b"audio")

    def run(cmd, **_kwargs):
        calls.append(cmd)
        return SimpleNamespace(returncode=1 if len(calls) == 1 else 0, stderr=b"")

    monkeypatch.setattr("app.pipeline.sound_effects.storage.download_to_file", download)
    monkeypatch.setattr(
        "app.pipeline.sound_effects.storage.upload_public_read", lambda *a, **k: "u"
    )
    monkeypatch.setattr("app.pipeline.sound_effects._has_decodable_audio", lambda _path: True)
    monkeypatch.setattr("app.pipeline.sound_effects.subprocess.run", run)

    _, receipt = apply_smart_audio_treatment(
        "base.mp4",
        [effect],
        "out.mp4",
        music_bed={
            "track_id": "track",
            "src_gcs_path": "music/track/audio.mp3",
            "section_start_s": 2.0,
            "section_end_s": 20.0,
        },
    )
    assert receipt["attempted_tiers"] == ["full", "sfx_only"]
    assert receipt["final_tier"] == "sfx_only"
    assert all(cmd[cmd.index("-c:v") + 1] == "copy" for cmd in calls)


def test_audio_treatment_timeout_falls_through_to_untouched_speech(monkeypatch, tmp_path) -> None:
    import subprocess

    monkeypatch.setattr(
        "app.pipeline.sound_effects.storage.download_to_file",
        lambda _gcs, local: open(local, "wb").close(),
    )
    monkeypatch.setattr(
        "app.pipeline.sound_effects.storage.upload_public_read", lambda *a, **k: "u"
    )
    monkeypatch.setattr("app.pipeline.sound_effects._has_decodable_audio", lambda _path: True)
    monkeypatch.setattr(
        "app.pipeline.sound_effects.subprocess.run",
        lambda cmd, **_kwargs: (_ for _ in ()).throw(subprocess.TimeoutExpired(cmd, 600)),
    )

    _, receipt = apply_smart_audio_treatment(
        "base.mp4",
        [
            SoundEffectPlacement(
                id="tick",
                src_gcs_path="sound-effects/tick/audio.wav",
                at_s=1.0,
                gain=0.4,
            )
        ],
        "out.mp4",
    )

    assert receipt["final_tier"] == "untouched_speech"
    assert receipt["component_failures"][-1]["reason"] == "ffmpeg_timeout"


def test_semantic_crop_is_inside_existing_reframe_filter_only() -> None:
    baseline = _build_video_filter("9:16", None)
    assert baseline == _build_video_filter("9:16", None, semantic_crop_pulses=[])
    treated = _build_video_filter(
        "9:16",
        None,
        semantic_crop_pulses=[
            {
                "token": "semantic_crop_pulse",
                "start_s": 3.0,
                "end_s": 4.2,
                "intensity": 0.04,
                "easing": "sine_pulse",
            }
        ],
    )
    joined = ",".join(treated)
    assert "0.0400*between(t,3.000,4.200)" in joined
    assert "pow(sin(PI*(t-3.000)/1.200),2)" in joined
    assert "1+0.08" not in joined
    assert len(treated) == len(baseline) + 2


def test_face_detection_failure_has_bounded_empty_fallback() -> None:
    regions, receipt = sample_face_regions("/does/not/exist.mp4", [0.0, 1.0], timeout_s=0.01)
    assert regions == []
    assert receipt["attempted"] <= 2
    assert receipt["elapsed_ms"] < 5000
    # A worker failure (as opposed to a genuine zero-face clip) must be
    # observable: either the budget killed it or the receipt names the error.
    assert receipt["timed_out"] or "worker_error" in receipt


def test_face_sampler_success_payload_becomes_padded_timed_regions(monkeypatch) -> None:
    import json as json_lib
    import subprocess  # noqa: F401 — patched below
    from types import SimpleNamespace

    from app.pipeline import render_geometry as rg

    payload = {
        "attempted": 1,
        "samples": [{"at_s": 2.0, "box": {"left": 0.4, "top": 0.2, "right": 0.6, "bottom": 0.5}}],
    }
    monkeypatch.setattr(
        rg.subprocess,
        "run",
        lambda *a, **k: SimpleNamespace(returncode=0, stdout=json_lib.dumps(payload), stderr=""),
    )

    regions, receipt = rg.sample_face_regions("/v.mp4", [2.0])

    assert receipt["detected"] == 1
    assert "worker_error" not in receipt
    assert regions[0].start_s == pytest.approx(1.5)
    assert regions[0].end_s == pytest.approx(2.5)
    assert regions[0].kind == "face"
    # Asymmetric padding: generous sides/chin, thin above the hairline so
    # corner cards can sit beside the hair.
    assert regions[0].box.left == pytest.approx(0.4 - 0.06)
    assert regions[0].box.top == pytest.approx(0.2 - 0.02)
    assert regions[0].box.bottom == pytest.approx(0.5 + 0.08)


def test_face_detection_timeout_is_enforced_by_killable_process(monkeypatch) -> None:
    import subprocess

    def timeout(command, **_kwargs):
        raise subprocess.TimeoutExpired(command, 0.01)

    monkeypatch.setattr("app.pipeline.render_geometry.subprocess.run", timeout)

    regions, receipt = sample_face_regions("/blocked-decoder.mp4", [0.0, 1.0], timeout_s=0.01)

    assert regions == []
    assert receipt["timed_out"] is True
    assert receipt["attempted"] == 2
    assert receipt["elapsed_ms"] < 500


def test_asset_context_failure_returns_empty_pool_and_receipt(monkeypatch) -> None:
    monkeypatch.setattr(
        "app.tasks.generative_build._load_smart_caption_assets",
        lambda _job_id: (_ for _ in ()).throw(RuntimeError("database unavailable")),
    )

    assets, receipt = _load_smart_caption_assets_fail_open("00000000-0000-0000-0000-000000000000")

    assert assets == []
    assert receipt == {"status": "failed_open", "error_class": "RuntimeError"}


# ── Feature C: face-aware caption placement (plan 011) ────────────────────────


def test_sampler_receipt_is_byte_identical_without_count_decoded(monkeypatch) -> None:
    """Flag-off pin: the default sampler call must NOT gain a ``decoded`` key, so
    every existing geometry_prepare receipt stays byte-identical."""
    import json as json_lib

    from app.pipeline import render_geometry as rg

    payload = {"attempted": 2, "decoded": 2, "samples": []}
    monkeypatch.setattr(
        rg.subprocess,
        "run",
        lambda *a, **k: SimpleNamespace(returncode=0, stdout=json_lib.dumps(payload), stderr=""),
    )

    _, default_receipt = rg.sample_face_regions("/v.mp4", [1.0, 2.0])
    assert "decoded" not in default_receipt

    _, counted_receipt = rg.sample_face_regions("/v.mp4", [1.0, 2.0], count_decoded=True)
    assert counted_receipt["decoded"] == 2


def test_face_placement_persists_chosen_y_and_remeasures(monkeypatch, tmp_path) -> None:
    """Flag-on orchestration: the chosen y is persisted onto the policy +
    caption_margin_v (WITHOUT the user-edited flag), every cue is re-measured so
    its box bottom follows the chosen y, the candidate ladder leads with the
    preset, and the sampler is asked for the decodable count on the anchor union."""
    from types import SimpleNamespace as NS

    from app.pipeline import probe as probe_mod
    from app.pipeline import render_geometry as rg
    from app.pipeline.captions import y_frac_to_margin_v
    from app.tasks import generative_build as gb

    policy = load_preset("cigdem", "v2").caption.model_dump(mode="json")
    preset_y = policy["y_frac"]
    chosen = 0.55

    captured: dict = {}

    def fake_sample(base_path, anchors, **kwargs):
        captured["anchors"] = list(anchors)
        captured["kwargs"] = kwargs
        return [], {"attempted": len(anchors), "decoded": len(anchors), "timed_out": False}

    def fake_choose(face_regions, face_receipt, probe, titles, candidates):
        captured["candidates"] = candidates
        captured["probe"] = probe
        return chosen, {"status": "moved", "reason": None, "chosen_y_frac": chosen}

    monkeypatch.setattr(rg, "sample_face_regions", fake_sample)
    monkeypatch.setattr(rg, "choose_caption_y_frac", fake_choose)
    monkeypatch.setattr(probe_mod, "probe_video", lambda _p: NS(duration_s=12.0))
    monkeypatch.setattr("app.services.pipeline_trace.record_pipeline_event", lambda *a, **k: None)

    cues = prepare_smart_caption_cues([_cue("hello world this is a caption", 0.0, 2.0)], policy)
    base = {
        "smart_caption_policy": dict(policy),
        "smart_validation_receipts": {},
        "text_elements": [],
    }
    smart_compiled = NS(camera_intents=[{"at_s": 3.0}], media_overlays=[])

    faces, receipt, remeasured = gb._apply_face_aware_caption_placement(
        base=base,
        cues=cues,
        base_path=str(tmp_path / "base.mp4"),
        smart_compiled=smart_compiled,
        job_id="job",
        variant_id="v1",
    )

    assert base["smart_caption_policy"]["y_frac"] == chosen
    assert base["caption_margin_v"] == y_frac_to_margin_v(chosen)
    assert "caption_position_user_edited" not in base  # first-render, not a user edit
    assert base["smart_validation_receipts"]["caption_placement"]["status"] == "moved"
    # TEST-1: every cue is re-measured at the chosen y so protected boxes match.
    assert remeasured[0]["smart_render_box"]["bottom"] == pytest.approx(chosen, abs=1e-6)
    # Candidate #0 is ALWAYS the preset; the sampler gets the decodable count.
    assert captured["candidates"][0] == preset_y
    assert captured["kwargs"]["count_decoded"] is True
    # Anchor UNION includes the camera-intent time and the evenly-spaced anchors.
    assert 3.0 in captured["anchors"]
    assert len(captured["anchors"]) >= 8


def test_face_placement_anchor_union_dedupes_and_scales_timeout(monkeypatch, tmp_path) -> None:
    from types import SimpleNamespace as NS

    from app.pipeline import probe as probe_mod
    from app.pipeline import render_geometry as rg
    from app.tasks import generative_build as gb

    policy = load_preset("cigdem", "v2").caption.model_dump(mode="json")
    captured: dict = {}

    def fake_sample(base_path, anchors, **kwargs):
        captured["anchors"] = list(anchors)
        captured["kwargs"] = kwargs
        return [], {"attempted": len(anchors), "decoded": len(anchors), "timed_out": False}

    monkeypatch.setattr(rg, "sample_face_regions", fake_sample)
    monkeypatch.setattr(
        rg, "choose_caption_y_frac", lambda *a: (policy["y_frac"], {"status": "well_framed"})
    )
    monkeypatch.setattr(probe_mod, "probe_video", lambda _p: NS(duration_s=8.0))
    monkeypatch.setattr("app.services.pipeline_trace.record_pipeline_event", lambda *a, **k: None)

    cues = prepare_smart_caption_cues([_cue("hi there", 0.0, 1.0)], policy)
    base = {
        "smart_caption_policy": dict(policy),
        "smart_validation_receipts": {},
        "text_elements": [],
    }
    # A media start at 0.5 collides with the evenly-spaced 0.5 anchor (8.0*0.5/8)
    # and must be deduped within the ±0.25s window.
    smart_compiled = NS(camera_intents=[], media_overlays=[{"start_s": 0.5}])

    gb._apply_face_aware_caption_placement(
        base=base,
        cues=cues,
        base_path=str(tmp_path / "b.mp4"),
        smart_compiled=smart_compiled,
        job_id="j",
        variant_id="v",
    )

    anchors = captured["anchors"]
    assert anchors == sorted(anchors)
    # No two anchors sit within 0.25s of each other (dedupe held).
    assert all(b - a >= 0.25 for a, b in zip(anchors, anchors[1:]))
    # timeout_s scales with the anchor count on top of a cold-start base term.
    assert captured["kwargs"]["timeout_s"] == pytest.approx(
        gb._FACE_PLACEMENT_TIMEOUT_BASE_S + gb._FACE_PLACEMENT_TIMEOUT_PER_ANCHOR_S * len(anchors)
    )
    # The base term must cover interpreter boot + `import cv2` (~1.7s in the prod
    # image) before the first frame decodes — the original 1.0s did not, and the
    # kills that followed sent captions back onto the speaker's face.
    assert gb._FACE_PLACEMENT_TIMEOUT_BASE_S >= 2.0
    assert captured["kwargs"]["max_samples"] >= len(anchors)


def test_shared_margin_helpers_match_the_legacy_inline_formula() -> None:
    """QUAL-1: the migrated helpers are byte-identical to the six old inline copies."""
    from app.pipeline.captions import (
        CAPTION_Y_FRAC_MAX,
        CAPTION_Y_FRAC_MIN,
        clamp_caption_y_frac,
        margin_v_to_y_frac,
        y_frac_to_margin_v,
    )

    for y in (0.30, 0.55, 0.66, 0.705, 0.86, 0.90):
        assert y_frac_to_margin_v(y) == round((1.0 - y) * 1920)
    for mv in (192, 400, 653, 1344):
        assert margin_v_to_y_frac(mv) == max(0.3, min(0.9, 1.0 - mv / 1920.0))
    for y in (0.1, 0.30, 0.5, 0.90, 1.2):
        assert clamp_caption_y_frac(y) == max(0.3, min(0.9, float(y)))
    # The clamp bounds used by _resolve_caption_margin_v round-trip through them.
    assert y_frac_to_margin_v(CAPTION_Y_FRAC_MAX) == round((1.0 - 0.90) * 1920)
    assert y_frac_to_margin_v(CAPTION_Y_FRAC_MIN) == round((1.0 - 0.30) * 1920)


def test_chosen_y_drives_the_burned_ass_margin_v(tmp_path) -> None:
    """After face placement mutates the policy y, the burned ASS MarginV follows
    it (the burn geometry tracks the persisted policy automatically)."""
    from app.pipeline.captions import y_frac_to_margin_v

    policy = load_preset("cigdem", "v2").caption.model_dump(mode="json")
    policy["y_frac"] = 0.55  # a face-chosen y ≠ the preset
    cues = prepare_smart_caption_cues([_cue("hello world this is a caption", 1.0, 3.0)], policy)

    ass_path = tmp_path / "captions.ass"
    generate_ass_from_cues(cues, str(ass_path), smart_policy=policy, pop_in=True)

    style = next(
        line for line in ass_path.read_text().splitlines() if line.startswith("Style: Default,")
    )
    # Format: ...,Outline,Shadow,Alignment,MarginL,MarginR,MarginV,Encoding
    margin_v = int(style.split(",")[-2])
    assert margin_v == y_frac_to_margin_v(0.55)


# ── Feature C kill switch: the REAL gate inside _render_subtitled_variant ─────


def _patch_subtitled_render(monkeypatch, tmp_path, *, media_overlays):
    """Stub every ffmpeg/network dependency of `_render_subtitled_variant` and the
    smart compile, so the Feature C gate itself is what the test exercises."""
    import types

    import app.pipeline.caption_correct as cc
    import app.pipeline.captions as captions_mod
    import app.pipeline.narrated_assembler as na
    import app.pipeline.probe as probe_mod
    import app.pipeline.reframe as reframe_mod
    import app.pipeline.transcribe as transcribe_mod
    import app.services.pipeline_trace as pt
    import app.storage as storage
    from app.pipeline import render_geometry as rg
    from app.tasks import generative_build as gb

    captured: dict = {}

    monkeypatch.setattr(
        probe_mod,
        "probe_video",
        lambda p: types.SimpleNamespace(duration_s=8.0, aspect_ratio="9:16", has_audio=True),
        raising=False,
    )

    def _fake_reframe(input_path, start_s, end_s, aspect, ass, output_path, **kw):
        with open(output_path, "wb") as handle:
            handle.write(b"\x00" * 16)

    monkeypatch.setattr(reframe_mod, "reframe_and_export", _fake_reframe, raising=False)
    monkeypatch.setattr(
        reframe_mod,
        "resolve_output_fit",
        lambda probe, landscape_fit="fill", **kw: "crop",
        raising=False,
    )
    monkeypatch.setattr(
        transcribe_mod,
        "transcribe_whisper_cached",
        lambda path, language=None, **kw: types.SimpleNamespace(
            words=[], language="en", low_confidence=False, full_text=""
        ),
        raising=False,
    )
    monkeypatch.setattr(
        captions_mod,
        "build_plain_cues",
        lambda words, offset_s=0.0, *, attach_words=False: [
            {"text": "hello world caption", "start_s": 0.0, "end_s": 2.0}
        ],
        raising=False,
    )
    monkeypatch.setattr(
        captions_mod, "resplit_cues_into_sentences", lambda cues: cues, raising=False
    )

    def _write_ass(cues, ass_path, **kw):
        with open(ass_path, "w") as handle:
            handle.write("ass")

    monkeypatch.setattr(captions_mod, "generate_ass_from_cues", _write_ass, raising=False)
    monkeypatch.setattr(captions_mod, "generate_word_pop_ass", _write_ass, raising=False)
    monkeypatch.setattr(cc, "correct_caption_cues", lambda cues, lang, **kw: cues, raising=False)
    monkeypatch.setattr(na, "resolve_caption_font", lambda f: "TikTok Sans", raising=False)
    monkeypatch.setattr(
        na,
        "burn_captions_on_video",
        lambda base, ass, fonts, out: open(out, "wb").write(b"\x01" * 8),
        raising=False,
    )
    monkeypatch.setattr(
        storage, "upload_public_read", lambda local, gcs: f"https://signed/{gcs}", raising=False
    )
    monkeypatch.setattr(pt, "record_pipeline_event", lambda *a, **k: None, raising=False)
    monkeypatch.setattr(
        gb, "_load_smart_caption_assets_fail_open", lambda job_id: ([], None), raising=False
    )

    policy = load_preset("cigdem", "v2").caption.model_dump(mode="json")
    compiled = types.SimpleNamespace(
        caption_cues=prepare_smart_caption_cues(
            [_cue("hello world this is a caption", 0.0, 2.0)], policy
        ),
        camera_intents=[{"at_s": 3.0}],
        media_overlays=media_overlays,
        boundary_effects=[],
        text_elements=[],
        sfx_intents=[],
        audio_treatment_intents=[],
        compiled_patch={"compiler_version": "test"},
        validation_receipt={"styled_caption_count": 1},
    )
    monkeypatch.setattr(
        gb,
        "_compile_smart_caption_render_plan",
        lambda **kw: (
            compiled,
            {
                "smart_captions_applied": True,
                "smart_edit_document": {},
                "smart_compiled_patch": compiled.compiled_patch,
                "smart_planner_versions": {},
                "smart_validation_receipts": {"planner": {}, "compiler": {}},
                "media_overlays": media_overlays or None,
                "boundary_effects": None,
                "text_elements": [],
                "text_elements_user_edited": True,
                "text_elements_materialized_from": "smart_captions",
            },
        ),
        raising=False,
    )

    def _fake_sample(base_path, anchors, **kwargs):
        captured["anchors"] = list(anchors)
        captured["kwargs"] = kwargs
        return [], {"attempted": len(anchors), "detected": 0, "timed_out": False}

    monkeypatch.setattr(rg, "sample_face_regions", _fake_sample, raising=False)
    return captured, policy


def _run_subtitled(tmp_path, subdir):
    from app.tasks import generative_build as gb

    vdir = tmp_path / subdir
    vdir.mkdir(exist_ok=True)
    (tmp_path / "clip.mp4").write_bytes(b"\x00" * 8)
    return gb._render_subtitled_variant(
        job_id="00000000-0000-0000-0000-000000000000",
        rank=1,
        spec={"variant_id": "subtitled", "archetype": "subtitled", "caption_style": "sentence"},
        clip_id_to_local={"c1": str(tmp_path / "clip.mp4")},
        variant_dir=str(vdir),
        language="en",
        smart_captions={
            "preset_id": "cigdem",
            "preset_version": "v2",
            "sound_design": "off",
        },
    )


def test_face_placement_flag_off_keeps_anchor_list_and_receipts_byte_identical(
    monkeypatch, tmp_path
) -> None:
    """Kill switch: with the flag OFF the sampler must receive the LEGACY anchor
    list (camera + media starts only — no evenly-spaced union), no count_decoded,
    and the variant must carry neither a caption_placement receipt nor a moved y."""
    from app.config import settings

    captured, policy = _patch_subtitled_render(
        monkeypatch, tmp_path, media_overlays=[{"start_s": 5.0, "z": 1}]
    )
    monkeypatch.setattr(settings, "smart_caption_face_placement_enabled", False, raising=False)

    result = _run_subtitled(tmp_path, "variant_off")

    assert result["ok"] is True
    # Legacy anchors: camera intent (3.0) + media start (5.0). Nothing else.
    assert captured["anchors"] == [3.0, 5.0]
    assert "count_decoded" not in captured["kwargs"]
    assert captured["kwargs"] == {}  # legacy call passes NO keyword arguments
    assert "caption_placement" not in result["smart_validation_receipts"]
    assert "source" not in result["smart_validation_receipts"]["geometry_prepare"]
    assert result["smart_caption_policy"]["y_frac"] == policy["y_frac"]


def test_face_placement_flag_on_uses_the_capped_anchor_union(monkeypatch, tmp_path) -> None:
    """Flag ON: the sampler gets the deduped UNION (evenly-spaced ∪ camera ∪ media)
    with count_decoded, and the placement receipt is recorded."""
    from app.config import settings

    captured, _policy = _patch_subtitled_render(
        monkeypatch, tmp_path, media_overlays=[{"start_s": 5.0, "z": 1}]
    )
    monkeypatch.setattr(settings, "smart_caption_face_placement_enabled", True, raising=False)

    result = _run_subtitled(tmp_path, "variant_on")

    assert result["ok"] is True
    anchors = captured["anchors"]
    assert captured["kwargs"]["count_decoded"] is True
    # Union keeps the intent anchors AND adds evenly-spaced coverage.
    from app.tasks import generative_build as gb

    assert 3.0 in anchors and 5.0 in anchors
    assert len(anchors) > 2
    assert len(anchors) <= gb._FACE_PLACEMENT_MAX_ANCHORS  # hard cap
    assert result["smart_validation_receipts"]["caption_placement"]["status"] is not None
    assert result["smart_validation_receipts"]["geometry_prepare"]["source"] == "face_placement"
    # Persistence: the returned dict IS the variant record written to
    # Job.assembly_plan, so the chosen y and its UI mirror must both be on it and
    # must agree — and must NOT masquerade as a creator pin.
    from app.pipeline.captions import y_frac_to_margin_v

    chosen = result["smart_caption_policy"]["y_frac"]
    assert result["caption_margin_v"] == y_frac_to_margin_v(chosen)
    assert result.get("caption_position_user_edited") is not True


def test_face_placement_anchor_union_is_capped_under_a_saturated_plan(
    monkeypatch, tmp_path
) -> None:
    """A plan near MAX_SMART_EDIT_EVENTS must not hand the sampler an unbounded
    frame-seek count / timeout budget."""
    from app.config import settings
    from app.tasks import generative_build as gb

    captured, _policy = _patch_subtitled_render(
        monkeypatch,
        tmp_path,
        media_overlays=[{"start_s": float(i) * 0.5, "z": 1} for i in range(120)],
    )
    monkeypatch.setattr(settings, "smart_caption_face_placement_enabled", True, raising=False)

    _run_subtitled(tmp_path, "variant_saturated")

    assert len(captured["anchors"]) <= gb._FACE_PLACEMENT_MAX_ANCHORS
    assert captured["kwargs"]["max_samples"] <= gb._FACE_PLACEMENT_MAX_ANCHORS
    # Timeout stays bounded well under the render task's soft ceiling.
    assert captured["kwargs"]["timeout_s"] <= 1.0 + 0.35 * gb._FACE_PLACEMENT_MAX_ANCHORS


def test_front_loaded_intent_anchors_cannot_starve_whole_video_coverage(
    monkeypatch, tmp_path
) -> None:
    """Adversarial regression: a plan whose camera/media events all cluster in the
    first seconds must NOT reduce this permanent placement decision to a sample of
    the video's opening — the evenly-spaced lane keeps its own budget."""
    from app.config import settings
    from app.tasks import generative_build as gb

    captured, _policy = _patch_subtitled_render(
        monkeypatch,
        tmp_path,
        # 40 events clustered in the first ~2.4s of an 8s video.
        media_overlays=[{"start_s": i * 0.06, "z": 1} for i in range(40)],
    )
    monkeypatch.setattr(settings, "smart_caption_face_placement_enabled", True, raising=False)

    _run_subtitled(tmp_path, "variant_frontloaded")

    anchors = captured["anchors"]
    assert len(anchors) <= gb._FACE_PLACEMENT_MAX_ANCHORS
    # The evenly-spaced lane survives: anchors reach the LAST eighth of the video.
    assert max(anchors) > 8.0 * 0.75
    # And the intent lane never exceeds the sampler's historical parity budget.
    assert sum(1 for a in anchors if a < 2.5) <= gb._FACE_PLACEMENT_MAX_INTENT_ANCHORS


def test_protected_regions_include_authored_title_boxes() -> None:
    """The consolidated caption+title builder must actually measure titles — the
    title half of the three-copies-into-one refactor needs real content to prove
    it, since the Feature C tests all run with an empty text lane."""
    from app.tasks.generative_build import _smart_caption_protected_regions

    policy = load_preset("cigdem", "v2").caption.model_dump(mode="json")
    cues = prepare_smart_caption_cues([_cue("hello world caption", 0.0, 2.0)], policy)
    base = {
        "text_elements": [
            {
                "id": "chapter-1",
                "text": "1 - somutlastirma",
                "start_s": 0.5,
                "end_s": 3.5,
            }
        ],
        "duration_s": 10.0,
    }

    caption_regions, title_regions = _smart_caption_protected_regions(base, cues)

    assert len(caption_regions) == 1
    assert caption_regions[0].kind == "caption"
    assert caption_regions[0].box.bottom == pytest.approx(policy["y_frac"])
    # The title lane produced a real measured box, not a degenerate point.
    assert len(title_regions) == 1
    assert title_regions[0].kind == "title"
    box = title_regions[0].box
    assert 0.0 <= box.left < box.right <= 1.0
    assert 0.0 <= box.top < box.bottom <= 1.0


def test_face_placement_failure_falls_open_to_preset_geometry(monkeypatch, tmp_path) -> None:
    """The feature's core safety contract: ANY error inside the placement pass
    leaves the preset geometry, receipts, and legacy sampling path intact."""
    from app.config import settings
    from app.tasks import generative_build as gb

    captured, policy = _patch_subtitled_render(
        monkeypatch, tmp_path, media_overlays=[{"start_s": 5.0, "z": 1}]
    )
    monkeypatch.setattr(settings, "smart_caption_face_placement_enabled", True, raising=False)

    def _boom(**_kwargs):
        raise RuntimeError("ffprobe exploded")

    monkeypatch.setattr(gb, "_apply_face_aware_caption_placement", _boom, raising=False)

    result = _run_subtitled(tmp_path, "variant_failopen")

    # The variant still ships…
    assert result["ok"] is True
    # …at the PRESET position, with no half-applied placement state.
    assert result["smart_caption_policy"]["y_frac"] == policy["y_frac"]
    assert "caption_placement" not in result["smart_validation_receipts"]
    # …and geometry falls through to the legacy sampling lane (camera + media
    # anchors only, no count_decoded), exactly as if the flag were off.
    assert captured["anchors"] == [3.0, 5.0]
    assert captured["kwargs"] == {}
    assert "source" not in result["smart_validation_receipts"]["geometry_prepare"]


# ── partial-sample recovery on a timeout (T-CAP011-2, prod job b2a815e8) ──────
#
# The worker streams one line per anchor. A kill used to discard all of them, so
# a run that had already found the speaker reported detected:0 and the caption
# went back to the preset band — onto the face. 6265ms against a 6250ms budget.


def _anchor_line(at_s: float, decoded: bool = True, box: dict | None = None) -> str:
    import json as json_lib

    return json_lib.dumps({"anchor": {"at_s": at_s, "decoded": decoded, "box": box}})


def _face_box(top: float = 0.02, bottom: float = 0.68) -> dict:
    return {"left": 0.10, "top": top, "right": 0.92, "bottom": bottom}


def _timeout_with_stdout(stdout: str):
    import subprocess

    def _run(command, **_kwargs):
        raise subprocess.TimeoutExpired(command, 0.01, output=stdout)

    return _run


def test_sampler_timeout_keeps_partial_samples(monkeypatch) -> None:
    from app.pipeline import render_geometry as rg

    streamed = "\n".join(_anchor_line(float(i), True, _face_box()) for i in range(5)) + "\n"
    monkeypatch.setattr(rg.subprocess, "run", _timeout_with_stdout(streamed))

    regions, receipt = rg.sample_face_regions(
        "/slow.mp4", [float(i) for i in range(15)], max_samples=15, count_decoded=True
    )

    assert receipt["timed_out"] is True
    assert receipt["partial"] is True
    assert receipt["attempted"] == 5
    assert receipt["decoded"] == 5
    assert receipt["detected"] == 5
    assert len(regions) == 5
    # Recovered regions go through the SAME padding/clamping and time window as
    # the happy path — a partial result must not be a second, looser geometry.
    from app.pipeline.render_geometry import _face_protection_box

    assert regions[0].kind == "face"
    assert regions[0].start_s == pytest.approx(-0.5 + 0.5)  # anchor 0.0, clamped at 0
    assert regions[0].end_s == pytest.approx(0.5)
    assert regions[0].box == _face_protection_box(NormalizedBox(**_face_box()))


def test_sampler_timeout_counts_undecodable_anchors_without_a_face_region(monkeypatch) -> None:
    """A silence-cut base seeks past EOF on late anchors: attempted counts them,
    decoded does not, and neither invents a face box."""
    from app.pipeline import render_geometry as rg

    streamed = "\n".join(
        [
            _anchor_line(0.0, True, _face_box()),
            _anchor_line(1.0, True, None),  # decoded, no face found
            _anchor_line(2.0, False, None),  # seek past EOF
        ]
    )
    monkeypatch.setattr(rg.subprocess, "run", _timeout_with_stdout(streamed))

    regions, receipt = rg.sample_face_regions("/cut.mp4", [0.0, 1.0, 2.0], count_decoded=True)

    assert receipt["attempted"] == 3
    assert receipt["decoded"] == 2
    assert len(regions) == 1


def test_sampler_timeout_with_no_output_keeps_todays_empty_receipt(monkeypatch) -> None:
    """Byte-identity pin: a kill before the first flush must produce exactly the
    pre-feature timeout receipt — no `partial`, no `decoded`."""
    from app.pipeline import render_geometry as rg

    monkeypatch.setattr(rg.subprocess, "run", _timeout_with_stdout(""))

    regions, receipt = rg.sample_face_regions("/slow.mp4", [0.0, 1.0], count_decoded=True)

    assert regions == []
    assert receipt["attempted"] == 2
    assert receipt["detected"] == 0
    assert receipt["timed_out"] is True
    assert "partial" not in receipt
    assert "decoded" not in receipt


def test_sampler_timeout_ignores_a_truncated_trailing_line(monkeypatch) -> None:
    """The kill can cut a line mid-write; a half-written JSON object must be
    skipped, never raised, and never counted."""
    from app.pipeline import render_geometry as rg

    streamed = _anchor_line(0.0, True, _face_box()) + '\n{"anchor": {"at_s": 1.0, "dec'
    monkeypatch.setattr(rg.subprocess, "run", _timeout_with_stdout(streamed))

    regions, receipt = rg.sample_face_regions("/slow.mp4", [0.0, 1.0], count_decoded=True)

    assert receipt["attempted"] == 1
    assert len(regions) == 1


def test_happy_path_ignores_the_anchor_stream_and_reads_the_summary(monkeypatch) -> None:
    """The per-anchor lines precede the summary; the successful path must still
    read the summary object alone so existing receipts stay byte-identical."""
    import json as json_lib

    from app.pipeline import render_geometry as rg

    summary = {
        "attempted": 2,
        "decoded": 2,
        "samples": [{"at_s": 1.0, "box": _face_box()}],
    }
    stdout = (
        _anchor_line(1.0, True, _face_box())
        + "\n"
        + _anchor_line(2.0, True, None)
        + "\n"
        + json_lib.dumps(summary)
        + "\n"
    )
    monkeypatch.setattr(
        rg.subprocess,
        "run",
        lambda *a, **k: SimpleNamespace(returncode=0, stdout=stdout, stderr=""),
    )

    regions, receipt = rg.sample_face_regions("/v.mp4", [1.0, 2.0], count_decoded=True)

    assert receipt["timed_out"] is False
    assert "partial" not in receipt
    assert receipt["attempted"] == 2
    assert receipt["decoded"] == 2
    assert len(regions) == 1  # from the summary's samples, not the stream


def test_partial_timeout_still_moves_the_caption_off_a_close_up_face() -> None:
    """END-TO-END regression for prod job b2a815e8.

    15 anchors, the sampler is killed at the budget, but the worker had already
    streamed the speaker's face. Before partial recovery this reported detected:0
    and the caption stayed at the preset 0.705 — 68% covered by that face band.
    """
    from app.pipeline.captions import clamp_caption_y_frac  # noqa: F401 — contract doc
    from app.pipeline.render_geometry import (
        NormalizedBox,
        ProtectedRegion,
        choose_caption_y_frac,
    )

    close_up = NormalizedBox(0.10, 0.02, 0.92, 0.68)
    recovered = [ProtectedRegion(float(i), float(i) + 1, close_up, kind="face") for i in range(10)]
    # What sample_face_regions now returns for that kill.
    receipt = {"attempted": 10, "decoded": 10, "detected": 10, "timed_out": True, "partial": True}
    probe = NormalizedBox(0.06, 0.626, 0.94, 0.705)  # measured 2-line cue at the preset

    chosen, placement = choose_caption_y_frac(
        recovered, receipt, [probe], [], (0.705, 0.62, 0.78, 0.55, 0.86)
    )

    assert chosen == pytest.approx(0.78)
    assert placement["status"] == "moved"
    assert placement["coverage"] <= 0.05
    # The old behaviour, pinned so a regression is loud: the preset it used to
    # return is two thirds covered by this face band.
    assert probe.coverage_by(close_up) > 0.6


def test_user_pinned_caption_position_skips_face_placement(monkeypatch, tmp_path) -> None:
    """Documented precedence: caption_position_user_edited > face-chosen > preset.

    The gate never checked the flag, so a full re-render silently overrode a
    position the creator had set by hand — and with the safe fallback in place a
    sampler failure could have moved a pinned caption too.
    """
    from app.config import settings
    from app.pipeline.captions import margin_v_to_y_frac, y_frac_to_margin_v
    from app.tasks import generative_build as gb

    _patch_subtitled_render(monkeypatch, tmp_path, media_overlays=[{"start_s": 5.0, "z": 1}])
    monkeypatch.setattr(settings, "smart_caption_face_placement_enabled", True, raising=False)

    # A spy that MOVES the caption, not one that raises: the call site wraps this
    # helper in a fail-open `except Exception`, so a raising spy is swallowed and
    # the test would pass against the unfixed gate.
    calls: list[int] = []

    def _spy(*, base, cues, **_kwargs):
        calls.append(1)
        base["smart_caption_policy"]["y_frac"] = 0.86
        base["caption_margin_v"] = y_frac_to_margin_v(0.86)
        base.setdefault("smart_validation_receipts", {})["caption_placement"] = {"status": "moved"}
        return [], {}, cues

    monkeypatch.setattr(gb, "_apply_face_aware_caption_placement", _spy, raising=False)

    pinned_margin_v = y_frac_to_margin_v(0.66)  # the editor's "Middle" preset
    vdir = tmp_path / "variant_pinned"
    vdir.mkdir(exist_ok=True)
    (tmp_path / "clip.mp4").write_bytes(b"\x00" * 8)

    result = gb._render_subtitled_variant(
        job_id="00000000-0000-0000-0000-000000000000",
        rank=1,
        spec={
            "variant_id": "subtitled",
            "archetype": "subtitled",
            "caption_style": "sentence",
            "caption_margin_v": pinned_margin_v,
            "caption_position_user_edited": True,
        },
        clip_id_to_local={"c1": str(tmp_path / "clip.mp4")},
        variant_dir=str(vdir),
        language="en",
        smart_captions={"preset_id": "cigdem", "preset_version": "v2", "sound_design": "off"},
    )

    assert result["ok"] is True
    assert calls == []  # the gate, not the fail-open, is what stopped it
    assert "caption_placement" not in result["smart_validation_receipts"]
    assert result["caption_margin_v"] == pinned_margin_v
    assert result["smart_caption_policy"]["y_frac"] == pytest.approx(
        margin_v_to_y_frac(pinned_margin_v)
    )
