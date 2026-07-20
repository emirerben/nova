from __future__ import annotations

from datetime import UTC, datetime
from types import SimpleNamespace

from app.agents._schemas.music_labels import CURRENT_LABEL_VERSION
from app.agents._schemas.song_sections import CURRENT_SECTION_VERSION
from app.agents._schemas.sound_effect import SoundEffectPlacement
from app.pipeline.captions import generate_ass_from_cues, prepare_smart_caption_cues
from app.pipeline.reframe import _build_video_filter
from app.pipeline.render_geometry import (
    NormalizedBox,
    arbitrate_media_overlays,
    sample_face_boxes,
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
from app.tasks.generative_build import _smart_music_track_eligible, _text_element_burn_dicts


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

    initial = tmp_path / "initial.ass"
    reburn = tmp_path / "reburn.ass"
    generate_ass_from_cues(prepared, str(initial), smart_policy=policy, pop_in=True)
    generate_ass_from_cues(prepared, str(reburn), smart_policy=policy, pop_in=True)
    assert initial.read_bytes() == reburn.read_bytes()
    rendered = initial.read_text()
    assert f"{policy['font_family']},{policy['font_size_px']}" in rendered
    assert "\\N" in rendered


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
    boxes, receipt = sample_face_boxes("/does/not/exist.mp4", [0.0, 1.0], timeout_s=0.01)
    assert boxes == []
    assert receipt["attempted"] <= 2
    assert receipt["elapsed_ms"] < 1000
