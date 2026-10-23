"""KRI-813 journey: the Troya museum narrated edit, as the iPhone editor receives it.

Troya stress run C7 (job b9e4c530, generations aafe53ea and ee703e65) exported
on the phone, but the editor's source preview failed every time with "This
edit's timeline couldn't be read". The server's narrated planner
(`_fit_step_window`) slows short footage to fill each voiceover step and has
no rate floor, so the museum's 2.4 s sculpture clip under a 14.22 s step plays
at about 0.165x. The export engine accepts that rate; the editor's preview
compiler refused anything under 0.25x.

This module pins the server half of the shared contract in
``src/apps/ios/Tests/Fixtures/KRI813MuseumNarrated.json``: the C7 step timings
and per-step source durations (durations only: no URLs, media ids or user
data), and the per-clip ``(source_start, source_duration, rate)`` the phone
narrated compiler produces from them. `KRI813NarratedSlowPreviewE2ETests`
builds the same edit in the native editor (real `prepareSourcePreview` over
generated sources of these exact durations) and requires its preview recipe
to match these clips exactly.

Regenerate after an intended server change, then re-run the native test:
    KRIA_REGENERATE_FIXTURES=1 pytest tests/pipeline/test_kri813_narrated_preview_fixture.py
"""

from __future__ import annotations

import json
import os
from pathlib import Path

from app.kria.media_sources import OriginalMediaDescriptor
from app.kria.render_assets import RenderFingerprint
from app.pipeline.phone_narrated_plan import NarratedPhoneStep, compile_phone_narrated_plan
from app.pipeline.phone_recipe_shared import PhoneNarrationBed
from app.services.phone_sources import PhoneSourceBinding

FIXTURE = Path(__file__).resolve().parents[3] / "ios/Tests/Fixtures/KRI813MuseumNarrated.json"
GENERATOR = "src/apps/api/tests/pipeline/test_kri813_narrated_preview_fixture.py"

VOICEOVER_S = 54.0
# (step id, start s, end s, device-original duration s of the clip assigned to
# it). Read from c7-job-ready.json: narrated_timings, narrated_alignment.steps
# media ids, and the media snapshot's upload_contract.proxy.original.duration_s.
STEPS = [
    ("seg_0", 0.0, 5.82, 3.36),
    ("seg_1", 5.82, 16.66, 2.88),
    ("seg_2", 16.66, 30.88, 2.4),  # the sculpture: ~0.165x
    ("seg_3", 30.88, 37.9, 2.4),
    ("seg_4", 37.9, 41.26, 26.4),
    ("seg_5", 41.26, 43.62, 8.16),
    ("seg_6", 43.62, 46.14, 9.6),
    ("seg_7", 46.14, 48.0, 10.08),
    ("seg_8", 48.0, 49.5, 2.88),
    ("seg_9", 49.5, 51.0, 1.92),
    ("seg_10", 51.0, 52.5, 3.36),
    ("seg_11", 52.5, 54.0, 4.32),
]
# The creator's Save in generation ee703e65: pan only the sculpture slot.
CROP_STEP = "seg_2"
CROP = {"x": 0.6146287096, "y": 0.0, "width": 0.31640625, "height": 1.0}


def _binding(index: int, duration_s: float) -> PhoneSourceBinding:
    return PhoneSourceBinding(
        media_id=f"kri813-clip-{index}",
        proxy_path=f"users/owner/creation/analysis-proxy-kri813-clip-{index}.mp4",
        generation="813",
        original=OriginalMediaDescriptor(
            sha256=f"{index:x}" * 64,
            byte_count=813,
            duration_s=duration_s,
            width=1080,
            height=1920,
            has_audio=True,
        ),
    )


def build_fixture() -> dict:
    bindings = tuple(_binding(index, duration) for index, (*_, duration) in enumerate(STEPS))
    steps = [
        NarratedPhoneStep(
            step_id=step_id,
            media_id=bindings[index].media_id,
            start_s=start,
            end_s=end,
        )
        for index, (step_id, start, end, _) in enumerate(STEPS)
    ]
    narration = PhoneNarrationBed(
        plan_item_id="kri813-item",
        generation="813",
        fingerprint=RenderFingerprint(sha256="d" * 64, byte_count=813),
        duration_s=VOICEOVER_S,
    )
    recipe = compile_phone_narrated_plan(
        steps, bindings, narration, voiceover_duration_s=VOICEOVER_S
    )
    [video] = [track for track in recipe.tracks if track.kind == "video"]
    assert len(video.clips) == len(STEPS)
    rows = []
    for index, ((step_id, start, end, duration), clip) in enumerate(
        zip(STEPS, video.clips, strict=True)
    ):
        assert abs(clip.timeline_start - start) < 1e-6, (step_id, clip.timeline_start)
        assert abs(clip.source_duration / clip.rate - (end - start)) < 1e-6, step_id
        rows.append(
            {
                "step_id": step_id,
                "clip_index": index,
                "start_s": start,
                "end_s": end,
                "source_duration_s": duration,
                "expected": {
                    "source_start": round(clip.source_start, 9),
                    "source_duration": round(clip.source_duration, 9),
                    "rate": round(clip.rate, 9),
                },
            }
        )
    slow = [row["step_id"] for row in rows if row["expected"]["rate"] < 0.25]
    # The C7 failure: the sculpture slot is below the editor's old 0.25 floor.
    assert slow == ["seg_2"], slow
    return {
        "contract": (
            "KRI-813: the iPhone editor's source preview of a phone narrated edit plays "
            "every step at exactly the server compiler's (source_start, source_duration, "
            "rate), including slow-to-fill rates under 0.25x"
        ),
        "generator": GENERATOR,
        "evidence": {
            "job": "b9e4c530-fb74-42ce-abd0-c38665693807",
            "generations": [
                "aafe53ea-f84e-409f-9095-2dba96c6cc3c",
                "ee703e65-ca25-4b32-8171-ebac4482be4c",
            ],
        },
        "voiceover_s": VOICEOVER_S,
        "crop": {"step_id": CROP_STEP, "source_crop": CROP},
        "steps": rows,
    }


def test_checked_in_device_fixture_matches_the_server_compiler():
    built = build_fixture()
    if os.environ.get("KRIA_REGENERATE_FIXTURES") == "1":
        FIXTURE.write_text(json.dumps(built, indent=2) + "\n")
    assert FIXTURE.exists(), f"missing {FIXTURE}; regenerate with KRIA_REGENERATE_FIXTURES=1"
    assert json.loads(FIXTURE.read_text()) == built, (
        "the server's narrated recipe changed; regenerate the iOS fixture with "
        "KRIA_REGENERATE_FIXTURES=1 and re-run KRI813NarratedSlowPreviewE2ETests"
    )
