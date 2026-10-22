#!/usr/bin/env python3
"""Build an offline native-export fixture for three spoken voiceover rules.

Synthetic word times and tone audio are deliberate: this verifies native recipe
fidelity, not live transcription or the original user's full night-boat edit.
Run with the API venv, then DeviceMontageRenderE2ETests/testSpokenVoiceoverRules.
"""

from __future__ import annotations

import importlib.util
import json
import sys
import uuid
from pathlib import Path

spec = importlib.util.spec_from_file_location(
    "montage_fixture", Path(__file__).with_name("phone-montage-render-e2e.py")
)
fixture = importlib.util.module_from_spec(spec)
spec.loader.exec_module(fixture)

from app.kria.spoken_text_ask import SpokenTextAsk  # noqa: E402
from app.pipeline.phone_narrated_plan import (  # noqa: E402
    NarratedPhoneStep,
    compile_phone_narrated_plan,
    replace_narrated_title,
)
from app.pipeline.phone_narrated_spoken_text import compile_narrated_spoken_text  # noqa: E402
from app.pipeline.phone_voiceover_montage_plan import PhoneNarrationBed  # noqa: E402
from app.kria.render_assets import RenderFingerprint  # noqa: E402


def main():
    out = Path(sys.argv[1])
    out.mkdir(parents=True, exist_ok=True)
    clip, voice = out / "source.mp4", out / "voice.m4a"
    fixture._color_clip(clip, "black", 260, duration=14)
    fixture._tone(voice, 1046, duration=14)
    sha, size = fixture._fingerprint(voice)
    narration = PhoneNarrationBed(
        plan_item_id="spoken-rules",
        generation="1",
        fingerprint=RenderFingerprint(sha256=sha, byte_count=size),
        duration_s=14,
    )
    cues = [{"text": "A separate subtitle line", "start_s": 1, "end_s": 11}]
    recipe = compile_phone_narrated_plan(
        [NarratedPhoneStep(step_id="s", media_id="source", start_s=0, end_s=14)],
        (fixture._binding(clip, "source", duration_s=14),),
        narration,
        voiceover_duration_s=14,
        mix=1,
        caption_cues=cues,
    )
    copies = [
        "1 · leave before 11",
        "2 · you're all amigos",
        "3 · 20 minutes of darkness",
    ]
    asks = [
        SpokenTextAsk(f"r{i}", text, (f"rule {i}",)) for i, text in enumerate(copies, 1)
    ]
    words = [
        w
        for i, start in enumerate((2, 5, 8), 1)
        for w in (
            {"text": "rule", "start_s": start, "end_s": start + 0.2},
            {"text": str(i), "start_s": start + 0.2, "end_s": start + 0.4},
        )
    ]
    rows, receipt = compile_narrated_spoken_text(
        asks,
        words=words,
        cues=cues,
        caption_style="sentence",
        timeline_duration_s=14,
        canvas=recipe.canvas,
    )
    assert [r["text"] for r in rows] == copies, receipt
    assert [r["start_s"] for r in rows] == [2, 5, 8], receipt
    recipe = replace_narrated_title(recipe, rows)
    # A wrong-time control must fail the independent expected cue-time check.
    wrong = [dict(r) for r in rows]
    wrong[2]["start_s"] = 0
    assert [r["start_s"] for r in wrong] != [2, 5, 8]
    case = "spoken_voiceover_rules"
    (out / "status.json").write_text(
        json.dumps(fixture._status(uuid.uuid4(), case, recipe), indent=2)
    )
    samples = []
    for index, layer in enumerate(recipe.text_layers[:3]):
        region = fixture._caption_region(
            layer, canvas_width=1080, canvas_height=1920, pad=8
        )
        samples.extend(
            [
                {
                    "name": f"rule{index + 1}_on",
                    "t": [2.8, 5.8, 8.8][index],
                    "region": region,
                    "expect_text": True,
                },
                {
                    "name": f"rule{index + 1}_off",
                    "t": 12.8,
                    "region": region,
                    "expect_text": False,
                },
            ]
        )
    caps = sorted(recipe.required_capabilities)
    meta = {
        "verified_features": caps,
        "cases": {
            case: {
                "status_file": "status.json",
                "duration_s": 14,
                "required_capabilities": caps,
                "drop_capability": "positionedText",
                "clips": [{"media_id": "source", "file": "source.mp4"}],
                "voiceover_asset_id": "voiceover-spoken-rules",
                "voiceover_file": "voice.m4a",
                "expects_source_audio": False,
                "expects_music_audio": False,
                "expects_narration_audio": True,
                "samples": [
                    {"name": f"rule{i}", "t": t, "x": 10, "y": 10, "rgb": [0, 0, 0]}
                    for i, t in enumerate((2.8, 5.8, 8.8), 1)
                ],
                "caption_samples": samples,
            }
        },
    }
    (out / "e2e.json").write_text(json.dumps(meta, indent=2))
    (out / "spoken-text-receipt.json").write_text(json.dumps(receipt, indent=2))
    print(out)


if __name__ == "__main__":
    main()
