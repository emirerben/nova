"""KRI-517: "at the very end use the sentence Elif says to the camera, with her own voice".

Prod thread 0b1f9556 (stress kit M3 Berlin): the creator's ``last`` order intent seated
Elif's clip at the end and the camera audio was kept, but the unified montage gave the
closing cut the same ~2.3 s window as every other cut (source 2.70-4.97 s of a line that
runs 0.00-5.44 s), so the video ended mid-sentence. The closing cut now holds the whole
analysed line; every other montage keeps its cuts.
"""

from __future__ import annotations

import dataclasses

import pytest

from app.kria.media_sources import OriginalMediaDescriptor
from app.pipeline.guided_story import GuidedStoryExecutionPlan, compile_execution_plan
from app.pipeline.phone_guided_plan import compile_phone_guided_plan
from app.pipeline.unified_montage import (
    SPEECH_LEAD_S,
    SPEECH_TAIL_S,
    BriefView,
    plan_unified_montage,
)
from app.services.phone_sources import PhoneSourceBinding
from tests.pipeline.test_unified_montage import clip

ELIF = "c5"
LINE = (0.0, 5.44)  # the analysed line, as in prod
KEPT = {"montage_audio": {"preserve_source_audio": True, "source_media_ids": []}}


def _speaking(index: int, *, segments=((0.0, 2.9), (3.2, 5.44)), duration: float = 7.2):
    return dataclasses.replace(
        clip(index, duration=duration),
        analysis={
            "understanding": {
                "speech": {
                    "has_speech": True,
                    "to_camera": True,
                    "segments": [{"text": "…", "start_s": s, "end_s": e} for s, e in segments],
                }
            }
        },
    )


def _clips(**speaking):
    return [clip(i) for i in range(5)] + [_speaking(5, **speaking)]


def _ends_on_elif(**extra) -> dict:
    return {
        "target_duration_s": 25,
        "resolved_clip_intents": [
            {
                "op": "order",
                "status": "resolved",
                "intent_id": "order_elif_voice_end",
                "attribute": "Elif's original voice clip",
                "position": "last",
                "assignments": [{"media_id": ELIF, "confidence": 0.9}],
            }
        ],
        **KEPT,
        **extra,
    }


def _plan(strategy: dict, clips=None, *, enabled: bool = True, target: float | None = 25):
    return plan_unified_montage(
        clips or _clips(),
        BriefView(target_duration_s=target),
        strategy=strategy,
        clip_intents_enabled=enabled,
    )


def _closing(plan):
    return plan.snapshot.fast_cuts[-1]


def test_the_closing_cut_holds_the_whole_spoken_line_and_the_length_is_kept() -> None:
    plan = _plan(_ends_on_elif())

    cut = _closing(plan)
    assert cut.media_id == ELIF
    assert cut.source_start_s == 0.0  # the line starts the cut (no lead before 0)
    assert cut.source_end_s >= LINE[1] + SPEECH_TAIL_S - 1 / 30
    assert cut.source_end_s <= 7.2
    assert plan.duration_s == pytest.approx(25, abs=0.05)
    assert plan.record()["closing_speech"] == {
        "media_id": ELIF,
        "source_start_s": cut.source_start_s,
        "source_end_s": cut.source_end_s,
    }


def test_a_line_that_starts_later_keeps_a_breath_before_it() -> None:
    plan = _plan(_ends_on_elif(), _clips(segments=((1.5, 4.0),)))
    cut = _closing(plan)
    assert cut.source_start_s == pytest.approx(1.5 - SPEECH_LEAD_S)
    assert cut.source_end_s >= 4.0 + SPEECH_TAIL_S - 1 / 30


def test_the_other_cuts_share_the_rest_and_none_is_cut_below_the_floor() -> None:
    plan = _plan(_ends_on_elif())
    others = plan.snapshot.fast_cuts[:-1]
    assert sum(c.output_duration_s for c in others) == pytest.approx(
        plan.duration_s - _closing(plan).output_duration_s, abs=0.01
    )
    assert all(c.output_duration_s >= 1.2 - 1e-6 for c in others)


@pytest.mark.parametrize(
    "case",
    ["not_placed_last", "music_only", "no_speech", "intents_off", "line_does_not_fit"],
)
def test_every_other_montage_keeps_its_ordinary_closing_cut(case: str) -> None:
    strategy = _ends_on_elif()
    clips = _clips()
    enabled = True
    target: float | None = 25
    if case == "not_placed_last":
        # Elif is attached last anyway: ending on a talking clip is not an ask.
        strategy["resolved_clip_intents"] = []
    elif case == "music_only":
        strategy.pop("montage_audio")
    elif case == "no_speech":
        clips = [*clips[:-1], clip(5, duration=7.2)]
    elif case == "intents_off":
        enabled = False
    else:
        # 6 cuts at the 1.2 s minimum leave 6 - 6 * 1.2 < 5.74 s for the line.
        target = 6.5
    plan = _plan(strategy, clips, enabled=enabled, target=target)
    baseline = _plan({}, [*clips[:-1], clip(5, duration=7.2)], enabled=enabled, target=target)

    assert "closing_speech" not in plan.record()
    assert _closing(plan).media_id == ELIF
    assert [(c.source_start_s, c.source_end_s) for c in plan.snapshot.fast_cuts] == [
        (c.source_start_s, c.source_end_s) for c in baseline.snapshot.fast_cuts
    ]


def test_the_held_line_survives_the_guided_and_phone_compilers() -> None:
    clips = _clips()
    plan = _plan(_ends_on_elif(), clips)
    bindings = tuple(
        PhoneSourceBinding(
            media_id=c.media_id,
            proxy_path=c.proxy_path,
            generation=c.generation,
            original=OriginalMediaDescriptor(
                sha256="a" * 64,
                byte_count=1000,
                duration_s=c.duration_s,
                width=1080,
                height=1920,
                has_audio=True,
            ),
        )
        for c in clips
    )
    execution = compile_execution_plan(plan.guided_edit(), track=None)
    recipe = compile_phone_guided_plan(GuidedStoryExecutionPlan.model_validate(execution), bindings)
    picture = sorted(
        (c for track in recipe.tracks if track.kind == "video" for c in track.clips),
        key=lambda c: c.timeline_start,
    )
    last = picture[-1]
    assert last.source_start <= LINE[0] + 1e-6
    assert last.source_start + last.source_duration >= LINE[1]
