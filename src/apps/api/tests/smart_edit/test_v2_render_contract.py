from __future__ import annotations

from datetime import UTC, datetime
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
from app.pipeline.text_overlay_skia import measure_text_overlay_box
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


def test_displaced_card_stays_near_its_assigned_corner() -> None:
    # Bottom-right card whose exact spot is blocked must slide within the
    # right side, not teleport to the left — the four-corner composition
    # depends on it.
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
    assert (box.left + box.right) / 2 > 0.5  # still on the right half


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


def test_music_eligibility_is_closed_and_requires_explicit_license() -> None:
    valid = SimpleNamespace(
        analysis_status="ready",
        published_at=datetime.now(UTC),
        archived_at=None,
        audio_gcs_path="music/track/audio.mp3",
        ai_labels={"labels": {}},
        label_version=CURRENT_LABEL_VERSION,
        best_sections=[{"rank": 1, "start_s": 2.0, "end_s": 20.0}],
        section_version=CURRENT_SECTION_VERSION,
        track_config={"smart_captions_licensed": True},
    )
    assert _smart_music_track_eligible(valid)
    for field, value in (
        ("analysis_status", "failed"),
        ("published_at", None),
        ("archived_at", datetime.now(UTC)),
        ("audio_gcs_path", None),
        ("label_version", "stale"),
        ("section_version", "stale"),
        ("track_config", {"smart_captions_licensed": False}),
    ):
        candidate = SimpleNamespace(**vars(valid))
        setattr(candidate, field, value)
        assert not _smart_music_track_eligible(candidate)


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
                "end_s": 3.8,
            }
        ],
    )
    joined = ",".join(treated)
    assert "between(t,3.000,3.800)" in joined
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
    assert regions[0].box.left == pytest.approx(0.4 - 0.08)
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
