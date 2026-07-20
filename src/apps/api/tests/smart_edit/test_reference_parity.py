from __future__ import annotations

import array
import copy
import math
import shutil
import subprocess

import pytest
from PIL import Image, ImageDraw

import app.pipeline.caption_correct as correction
from app.agents._schemas.sound_effect import SoundEffectPlacement
from app.pipeline.render_geometry import NormalizedBox, arbitrate_media_overlays, opaque_alpha_box
from app.pipeline.sound_effects import MusicBedTreatment, build_sound_effects_command
from app.smart_edit.compiler import compile_smart_plan
from app.smart_edit.planner import plan_smart_captions
from app.smart_edit.presets import load_preset
from app.tasks.generative_build import _smart_caption_trusted_aliases, _smart_shadow_comparison


def _working_ffmpeg() -> bool:
    binary = shutil.which("ffmpeg")
    if binary is None:
        return False
    return (
        subprocess.run(
            [binary, "-version"],
            capture_output=True,
            timeout=10,
            check=False,
        ).returncode
        == 0
    )


def _run_ffmpeg(command: list[str]) -> subprocess.CompletedProcess[bytes]:
    result = subprocess.run(command, capture_output=True, timeout=120, check=False)
    assert result.returncode == 0, result.stderr.decode(errors="replace")[-800:]
    return result


def _cue(text: str, start_s: float, end_s: float) -> dict:
    tokens = text.split()
    step = (end_s - start_s) / len(tokens)
    return {
        "text": text,
        "start_s": start_s,
        "end_s": end_s,
        "words": [
            {
                "word_id": f"asr-{start_s}-{index}",
                "text": token,
                "start_s": round(start_s + step * index, 3),
                "end_s": round(start_s + step * (index + 1), 3),
            }
            for index, token in enumerate(tokens)
        ],
    }


def _assets() -> list[dict]:
    return [
        {
            "id": "selocan",
            "kind": "image",
            "gcs_path": "users/u/plan/i/overlays/selocan.png",
            "source_filename": "selocanlar.png",
            "analysis": {"subject": "Selocan family"},
        },
        {
            "id": "robots",
            "kind": "image",
            "gcs_path": "users/u/plan/i/overlays/robots.png",
            "source_filename": "celik_celiknaz.png",
            "analysis": {"subject": "Robot wedding couple"},
        },
        {
            "id": "pinar",
            "kind": "image",
            "gcs_path": "users/u/plan/i/overlays/pinar.png",
            "source_filename": "pinar_beyaz.png",
            "analysis": {"subject": "White sheep mascot"},
        },
        {
            "id": "selpak",
            "kind": "image",
            "gcs_path": "users/u/plan/i/overlays/selpak.png",
            "source_filename": "selpak_fil.png",
            "analysis": {"subject": "Selpak elephant character"},
        },
        {
            "id": "cocacola",
            "kind": "image",
            "gcs_path": "users/u/plan/i/overlays/cocacola.png",
            "source_filename": "coca_cola_ayiciklari.png",
            "analysis": {"subject": "Coca-Cola polar bears"},
        },
        {
            "id": "demo",
            "kind": "video",
            "gcs_path": "users/u/plan/i/overlays/demo.mp4",
            "source_filename": "demo_video.mp4",
            "analysis": {"subject": "Demo video"},
        },
    ]


def test_sanitized_reference_reaches_semantic_visual_title_audio_camera_parity(monkeypatch) -> None:
    cues = [
        _cue(
            "Selocanlar Çeliknaz Pınar Beyaz Selpak Fil Koka Kola Ayıcıkları",
            0.0,
            4.5,
        ),
        _cue("Bunları hatırlıyor musunuz?", 4.6, 6.0),
        _cue("Dört başlıkta anlatayım: 1.", 7.0, 9.0),
        _cue("Somutlaştırma markayı akılda tutar.", 9.1, 11.0),
        _cue("Bu bölümün son sözü İki", 15.0, 17.0),
        _cue("Pester Power tüketiciyi etkiler.", 17.1, 19.0),
        _cue("Üç sosyal kanıt sağlar.", 23.0, 25.0),
        _cue("Mesela demo videosu her şeyi gösterir.", 27.0, 30.0),
        _cue("Dört nostalji yaratır.", 33.0, 35.0),
        _cue("Sonuç olarak karakterler unutulmaz olur.", 38.0, 40.0),
    ]
    monkeypatch.setattr(
        correction,
        "_llm_propose_substitutions",
        lambda *args, **kwargs: [
            {
                "line_index": 0,
                "start_word_index": 6,
                "end_word_index": 7,
                "original_span": "Koka Kola",
                "target_alias": "Coca Cola",
            }
        ],
    )
    aliases = correction.build_trusted_caption_hints(
        visual_aliases=load_preset("cigdem", "v2").visual_aliases,
        asset_names=[asset["source_filename"] for asset in _assets()],
    )
    corrected = correction.correct_caption_cues(
        cues,
        "tr",
        trusted_aliases=aliases,
    )
    assert corrected[0]["text"].endswith("Coca Cola Ayıcıkları")
    assert corrected[0]["words"][6]["start_s"] == cues[0]["words"][6]["start_s"]

    planned = plan_smart_captions(
        corrected,
        preset_version="v2",
        language="tr",
        assets=_assets(),
        use_agent=False,
    )
    assert planned is not None
    compiled = compile_smart_plan(
        planned.document,
        planned.caption_cues,
        assets_by_id={asset["id"]: asset for asset in _assets()},
    )

    hook = [
        overlay
        for overlay in compiled.media_overlays
        if overlay.get("composition_group_id") == "hook_accumulation"
    ]
    assert {overlay["src_gcs_path"] for overlay in hook} == {
        asset["gcs_path"] for asset in _assets()[:5]
    }
    assert len({overlay["end_s"] for overlay in hook}) == 1
    assert compiled.compiled_patch["hook_caption_suppressed"] is False
    assert compiled.compiled_patch["hook_caption_suppression_eligible"] is True
    assert any(cue.get("smart_style") == "hook" for cue in compiled.caption_cues)
    assert planned.validation_receipt["detected_chapters"] == [1, 2, 3, 4]
    assert [element["text"] for element in compiled.text_elements if element["text"].isdigit()] == [
        "1",
        "2",
        "3",
        "4",
    ]
    caption_text = " ".join(cue["text"] for cue in compiled.caption_cues)
    assert "Somutlaştırma" not in caption_text
    assert any(overlay["display_mode"] == "fullscreen" for overlay in compiled.media_overlays)
    assert compiled.audio_treatment_intents
    assert compiled.camera_intents
    assert any(intent["role"] == "keyword_typewriter_tick" for intent in compiled.sfx_intents)

    coca_overlay = next(overlay for overlay in hook if "cocacola" in overlay["src_gcs_path"])
    spoken_start = corrected[0]["words"][6]["start_s"]
    assert abs(coca_overlay["start_s"] - spoken_start) <= 0.2


def test_synthetic_transparent_pool_obeys_safe_geometry(tmp_path) -> None:
    source = tmp_path / "mascot.png"
    image = Image.new("RGBA", (400, 400), (0, 0, 0, 0))
    ImageDraw.Draw(image).ellipse((100, 80, 300, 330), fill=(255, 255, 255, 255))
    image.save(source)
    alpha = opaque_alpha_box(str(source))
    assert alpha == NormalizedBox(0.25, 0.2, 0.7525, 0.8275)

    protected = [NormalizedBox(0.15, 0.58, 0.85, 0.78)]
    resolved, receipts = arbitrate_media_overlays(
        [
            {
                "id": "mascot",
                "kind": "image",
                "src_gcs_path": "users/u/plan/i/overlays/mascot.png",
                "display_mode": "pip",
                "position": "custom",
                "x_frac": 0.5,
                "y_frac": 0.68,
                "scale": 0.35,
            }
        ],
        protected_boxes=protected,
    )
    assert resolved
    assert receipts[0]["max_protected_iou"] <= 0.02


def test_reference_audio_graph_has_reveal_ticks_and_persistent_voice_safe_bed() -> None:
    ticks = [
        SoundEffectPlacement(
            id=f"tick-{index}",
            src_gcs_path="sound-effects/tick/audio.wav",
            at_s=2.0 + index * 0.1,
            gain=0.16,
        )
        for index in range(4)
    ]
    command = build_sound_effects_command(
        "/speech.mp4",
        ticks,
        ["/tick.wav"] * len(ticks),
        "/out.mp4",
        music_bed=MusicBedTreatment(
            track_id="licensed",
            src_gcs_path="music/licensed/audio.mp3",
            section_start_s=5.0,
            section_end_s=35.0,
        ),
        music_local_path="/music.mp3",
    )
    graph = " ".join(command)
    assert all(f"adelay={2000 + index * 100}|" in graph for index in range(4))
    assert "sidechaincompress" in graph and "aloop=loop=-1" in graph
    assert command[command.index("-c:v") + 1] == "copy"


@pytest.mark.skipif(not _working_ffmpeg(), reason="working ffmpeg runtime is required")
def test_reference_audio_policy_executes_and_keeps_timed_tick_audible(tmp_path) -> None:
    cues = [
        _cue("Bunları hatırlıyor musunuz?", 0.0, 0.7),
        _cue("Dört başlıkta anlatayım: 1.", 0.7, 1.3),
        _cue("Somutlaştırma markayı akılda tutar.", 1.3, 2.0),
    ]
    planned = plan_smart_captions(
        cues,
        preset_version="v2",
        language="tr",
        use_agent=False,
    )
    assert planned is not None
    compiled = compile_smart_plan(planned.document, planned.caption_cues)
    intent = compiled.audio_treatment_intents[0]

    base = tmp_path / "base.mp4"
    music = tmp_path / "music.wav"
    tick = tmp_path / "tick.wav"
    output = tmp_path / "output.mp4"
    _run_ffmpeg(
        [
            "ffmpeg",
            "-y",
            "-f",
            "lavfi",
            "-i",
            "color=c=blue:s=320x568:r=24:d=2",
            "-f",
            "lavfi",
            "-i",
            "sine=frequency=440:sample_rate=48000:duration=2",
            "-filter:a",
            "volume=0.03",
            "-shortest",
            "-c:v",
            "libx264",
            "-preset",
            "ultrafast",
            "-pix_fmt",
            "yuv420p",
            "-c:a",
            "aac",
            str(base),
        ]
    )
    _run_ffmpeg(
        [
            "ffmpeg",
            "-y",
            "-f",
            "lavfi",
            "-i",
            "sine=frequency=220:sample_rate=48000:duration=2",
            "-filter:a",
            "volume=0.02",
            str(music),
        ]
    )
    _run_ffmpeg(
        [
            "ffmpeg",
            "-y",
            "-f",
            "lavfi",
            "-i",
            "sine=frequency=1400:sample_rate=48000:duration=0.12",
            str(tick),
        ]
    )
    placement = SoundEffectPlacement(
        id="keyboard-tick",
        src_gcs_path="sound-effects/keyboard/audio.wav",
        at_s=1.0,
        gain=1.0,
    )
    command = build_sound_effects_command(
        str(base),
        [placement],
        [str(tick)],
        str(output),
        music_bed=MusicBedTreatment(
            track_id="licensed",
            src_gcs_path="music/licensed/audio.wav",
            section_start_s=0.0,
            section_end_s=2.0,
            gain_db=float(intent["bed_gain_db"]),
            speech_duck_db=float(intent["speech_duck_db"]),
            final_lufs=float(intent["final_lufs"]),
        ),
        music_local_path=str(music),
    )
    _run_ffmpeg(command)

    decoded = _run_ffmpeg(
        [
            "ffmpeg",
            "-v",
            "error",
            "-i",
            str(output),
            "-map",
            "0:a:0",
            "-ac",
            "1",
            "-ar",
            "48000",
            "-f",
            "s16le",
            "pipe:1",
        ]
    ).stdout
    samples = array.array("h")
    samples.frombytes(decoded)

    def rms(start_s: float, end_s: float) -> float:
        window = samples[round(start_s * 48000) : round(end_s * 48000)]
        return math.sqrt(sum(sample * sample for sample in window) / len(window))

    assert output.stat().st_size > 0
    assert rms(1.0, 1.1) > rms(0.3, 0.5) * 1.5


def test_shadow_v2_receipt_never_mutates_or_materializes_primary_v1() -> None:
    cues = [_cue("Bunları hatırlıyor musunuz?", 0.0, 2.0)]
    primary = plan_smart_captions(cues, preset_version="v1", language="tr", use_agent=False)
    shadow = plan_smart_captions(cues, preset_version="v2", language="tr", use_agent=False)
    assert primary is not None and shadow is not None
    primary_compiled = compile_smart_plan(primary.document, primary.caption_cues)
    shadow_compiled = compile_smart_plan(shadow.document, shadow.caption_cues)
    primary_state = {
        "smart_edit_document": primary.document.model_dump(mode="json"),
        "smart_compiled_patch": primary_compiled.compiled_patch,
        "output_url": "primary-v1-only",
    }
    before = copy.deepcopy(primary_state)
    receipt = _smart_shadow_comparison(
        primary_state=primary_state,
        shadow_state={
            "smart_edit_document": shadow.document.model_dump(mode="json"),
            "smart_compiled_patch": shadow_compiled.compiled_patch,
        },
        shadow_compiled=shadow_compiled,
    )
    assert primary_state == before
    assert primary_state["output_url"] == "primary-v1-only"
    assert receipt["shadow_preset_version"] == "v2"
    assert receipt["materialized"] is False


def test_shadow_assignment_cannot_change_primary_correction_vocabulary() -> None:
    primary = {
        "preset_id": "cigdem",
        "preset_version": "v2",
    }
    with_shadow = {
        **primary,
        "shadow_preset_id": "cigdem",
        "shadow_preset_version": "v1",
    }
    assert _smart_caption_trusted_aliases(primary, _assets()) == _smart_caption_trusted_aliases(
        with_shadow,
        _assets(),
    )
