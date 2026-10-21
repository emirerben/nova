"""KRI-585: a montage that keeps the camera sound follows what the clips say.

Prod thread 7CCBDE0A, job 136712e8 (cabin kit M1): eleven house-tour clips with the
creator's narration, river pinned first and Deniz pinned last, no song (so every clip's
own sound plays). The clip that says "welcome to our home tour" played 9th, and the
closing cut played half of "this is Deniz, from our founding team". The fixture below
keeps that shape: attachment order, the two pinned ends, the analysed speech timings.

Ways this can go wrong, written down before the code:

1. The creator's pinned first / last clips move (a welcome outranks "start with the river").
2. A creator rule that names the welcome clip ("end on the kitchen") is overridden.
3. The reorder runs when the order is not ours to change: a filming-order ask, a
   creator-reordered timeline, a grouping choice, a song that mutes the camera, or a job
   whose confirmed order is pinned by the render contract (``speech_order`` off).
4. A montage with no spoken roles changes at all (cuts, windows or the record).
5. The welcome clip lands at the front but its cut misses the welcome words.
6. Holding the welcome line breaks the creator's length, or holds it when it cannot fit.
7. The closing introduction cut still plays the introduction, overlaps it, or the quiet
   cut makes the video shorter or longer than asked.
8. A quiet window is forced when the gap is too short, or replaces the KRI-517 whole
   line the creator explicitly asked to hear.
9. The plan stops compiling through the guided and phone compilers.
10. With no length asked, the footage selector (KRI-496) plays each clip's strongest
    analysed moment, so the welcome line and the quiet ending are lost on that path.
11. A "narrative" order key (how the brief extractor files "start with the river, end
    with Deniz") reads as an order basis and turns the spoken order off.

An approved length plan (runtime v2) chooses the windows before the worker runs; that
path is ``tests/tasks/test_kri585_spoken_order_montage.py``.
"""

from __future__ import annotations

import dataclasses

import pytest

from app.kria.brief import BriefRequirement, CreativeBrief
from app.kria.media_sources import OriginalMediaDescriptor
from app.pipeline.guided_story import GuidedStoryExecutionPlan, compile_execution_plan
from app.pipeline.phone_guided_plan import compile_phone_guided_plan
from app.pipeline.unified_montage import (
    SPEECH_LEAD_S,
    SPEECH_TAIL_S,
    BriefView,
    UnifiedClip,
    brief_view,
    plan_unified_montage,
)
from app.services.phone_sources import PhoneSourceBinding
from tests.pipeline.test_unified_montage import clip

RIVER, WELCOME, DENIZ, BATHROOM = "c9", "c8", "c7", "c10"
WELCOME_LINE = (1.94, 3.4)
DENIZ_WORDS = ((0.0, 1.22), (3.3, 5.74))
QUIET = (1.22 + SPEECH_TAIL_S, 3.3 - SPEECH_LEAD_S)


def _speaks(
    index: int, *segments: tuple[str, float, float], duration: float = 5.0, people=None
) -> UnifiedClip:
    return dataclasses.replace(
        clip(index, duration=duration),
        analysis={
            "understanding": {
                "speech": {
                    "has_speech": True,
                    "transcript": " ".join(text for text, _s, _e in segments),
                    "segments": [{"text": t, "start_s": s, "end_s": e} for t, s, e in segments],
                },
                "people": {"count": people},
            }
        },
    )


def _m1(*, deniz_words=DENIZ_WORDS, welcome=True) -> list[UnifiedClip]:
    """The M1 job's eleven clips, in its attachment order (prod clip_paths)."""
    tour = [
        _speaks(0, ("Gördüğünüz üzere", 0.0, 2.06), duration=6.0),
        clip(1, duration=9.0),
        _speaks(2, ("Başka yerleri çekiyor.", 0.0, 0.82), duration=6.0),
        _speaks(3, ("Mutfağını görüyorsunuz.", 1.06, 1.78), duration=5.0),
        _speaks(4, ("Şimdi iskelemize doğru gidiyoruz", 0.0, 1.72), duration=10.0),
        _speaks(5, ("Dün akşam ben burada kaldım.", 2.6, 5.48), duration=5.5),
        _speaks(6, ("Ben üst tarafta kalıyorum bu arada.", 0.0, 1.66), duration=5.0),
        _speaks(
            7,
            ("Bu da Deniz,", *deniz_words[0]),
            ("kanalımızın kurucu ekibinden.", *deniz_words[1]),
            duration=6.0,
            people=1,
        ),
        _speaks(
            8,
            ("Evet arkadaşlar,", 0.0, 1.14),
            (
                "Tigre'de ev turumuza hoş geldiniz." if welcome else "Burası mutfak.",
                *WELCOME_LINE,
            ),
            duration=4.5,
        ),
        clip(9, duration=12.0),
        _speaks(10, ("Burada bir kırk ayak vardı.", 4.74, 6.24), duration=10.0),
    ]
    return tour


def _pinned(first: str | None = RIVER, last: str | None = DENIZ, **extra) -> dict:
    intents = []
    if first:
        intents.append(_order("first", first))
    if last:
        intents.append(_order("last", last))
    return {"target_duration_s": 20, "resolved_clip_intents": intents, **extra}


def _order(position: str, media_id: str) -> dict:
    return {
        "op": "order",
        "status": "resolved",
        "intent_id": f"{position}_{media_id}",
        "attribute": media_id,
        "position": position,
        "assignments": [{"media_id": media_id, "confidence": 0.95}],
    }


def _plan(strategy=None, clips=None, *, view=None, speech_order=True, **kwargs):
    return plan_unified_montage(
        clips if clips is not None else _m1(),
        view or BriefView(target_duration_s=20),
        strategy=_pinned() if strategy is None else strategy,
        clip_intents_enabled=True,
        speech_order=speech_order,
        **kwargs,
    )


def _cut(plan, media_id):
    return next(cut for cut in plan.snapshot.fast_cuts if cut.media_id == media_id)


def _windows(plan):
    return [
        (c.media_id, c.source_start_s, c.source_end_s, c.output_duration_s)
        for c in plan.snapshot.fast_cuts
    ]


def test_m1_the_welcome_follows_the_pinned_river_and_deniz_still_closes() -> None:
    plan = _plan()

    assert plan.clip_ids[0] == RIVER
    assert plan.clip_ids[1] == WELCOME
    assert plan.clip_ids[-1] == DENIZ
    # Everything else keeps the attachment order it had.
    assert plan.clip_ids[2:-1] == ["c0", "c1", "c2", "c3", "c4", "c5", "c6", BATHROOM]
    assert plan.duration_s == pytest.approx(20, abs=0.05)
    assert plan.record()["spoken_roles"] == {WELCOME: "opening", DENIZ: "introduction"}


def test_m1_the_welcome_cut_carries_the_welcome_words() -> None:
    cut = _cut(_plan(), WELCOME)
    assert cut.source_start_s == pytest.approx(WELCOME_LINE[0] - SPEECH_LEAD_S)
    assert cut.source_end_s >= WELCOME_LINE[1] + SPEECH_TAIL_S - 1 / 30
    assert cut.source_end_s <= 4.5


def test_m1_the_closing_cut_ends_on_deniz_without_his_introduction() -> None:
    plan = _plan()
    cut = _cut(plan, DENIZ)
    assert cut.source_start_s >= QUIET[0] - 1e-6
    assert cut.source_end_s <= QUIET[1] + 1e-3
    assert cut.output_duration_s >= 1.2 - 1e-6
    assert plan.record()["closing_quiet"] == {
        "media_id": DENIZ,
        "source_start_s": cut.source_start_s,
        "source_end_s": cut.source_end_s,
        "reason": "introduction",
    }
    others = [c for c in plan.snapshot.fast_cuts if c.media_id != DENIZ]
    assert all(c.output_duration_s >= 1.2 - 1e-6 for c in others)


def test_without_a_pinned_first_clip_the_welcome_opens_the_montage() -> None:
    plan = _plan(_pinned(first=None))
    assert plan.clip_ids[0] == WELCOME
    assert plan.clip_ids[-1] == DENIZ


def test_a_sign_off_closes_right_before_the_pinned_last_clip() -> None:
    clips = _m1()
    clips[2] = _speaks(2, ("Görüşmek üzere, hoşça kalın!", 0.0, 1.8), duration=6.0)
    plan = _plan(clips=clips)
    assert plan.clip_ids[-2:] == ["c2", DENIZ]
    assert _plan(_pinned(last=None), clips=clips).clip_ids[-1] == "c2"


def test_the_creators_own_rule_for_the_welcome_clip_wins() -> None:
    plan = _plan(_pinned(last=WELCOME))
    assert plan.clip_ids[0] == RIVER
    assert plan.clip_ids[-1] == WELCOME
    assert "opening_speech" not in plan.record()


@pytest.mark.parametrize(
    "case",
    [
        "flag_off",
        "capture_order",
        "upload_order",
        "creator_order",
        "group_first",
        "chronological",
    ],
)
def test_the_order_is_left_alone_when_it_is_not_ours_to_change(case: str) -> None:
    kwargs: dict = {}
    view = BriefView(target_duration_s=20)
    strategy = _pinned()
    if case == "flag_off":
        kwargs["speech_order"] = False
    elif case == "capture_order":
        view = BriefView(target_duration_s=20, order_by_capture=True)
    elif case == "upload_order":
        view = brief_view(
            CreativeBrief(
                requirements=[
                    BriefRequirement(
                        id="r1",
                        kind="order",
                        scope="global",
                        description="keep the order I added them",
                        facts={"key": "attachment"},
                    ),
                    BriefRequirement(
                        id="r2", kind="timing", scope="global", facts={"duration_s": 20}
                    ),
                ]
            )
        )
        assert view.order_basis_stated
    elif case == "creator_order":
        kwargs["creator_order"] = list(range(11))
    else:
        strategy = _pinned(ordering_choice=case)
    plan = _plan(strategy, view=view, **kwargs)
    baseline = _plan(strategy, view=view, **{**kwargs, "speech_order": False})

    assert _windows(plan) == _windows(baseline)
    assert plan.record() == baseline.record()
    assert "spoken_roles" not in plan.record()


def test_a_song_montage_mutes_the_camera_so_the_order_is_untouched() -> None:
    song = {
        "song_duration_s": 60.0,
        "song_plan_item_id": "item-1",
        "song_generation": 1,
        "song_beats": [i * 0.5 for i in range(120)],
    }
    plan = _plan(**song)
    baseline = _plan(speech_order=False, **song)
    assert plan.clip_ids == baseline.clip_ids
    assert _windows(plan) == _windows(baseline)


def test_a_montage_with_no_spoken_roles_is_unchanged() -> None:
    clips = _m1(welcome=False)
    clips[7] = _speaks(7, ("kanalımızın kurucu ekibinden.", 3.3, 5.74), duration=6.0, people=1)
    plan = _plan(clips=clips)
    baseline = _plan(clips=clips, speech_order=False)
    assert _windows(plan) == _windows(baseline)
    assert plan.record() == baseline.record()


def test_a_welcome_line_that_cannot_fit_the_length_keeps_an_ordinary_cut() -> None:
    plan = _plan(_pinned(target_duration_s=13.3), view=BriefView(target_duration_s=13.3))
    assert plan.clip_ids[1] == WELCOME
    assert "opening_speech" not in plan.record()
    assert plan.duration_s == pytest.approx(13.3, abs=0.05)


def test_a_gap_too_short_to_hold_a_cut_keeps_the_ordinary_closing_cut() -> None:
    clips = _m1(deniz_words=((0.0, 1.22), (2.0, 5.74)))
    plan = _plan(clips=clips)
    baseline = _plan(clips=clips, speech_order=False)
    assert "closing_quiet" not in plan.record()
    assert _cut(plan, DENIZ).source_start_s == _cut(baseline, DENIZ).source_start_s


def test_an_explicit_ask_to_hear_the_closing_line_still_holds_it_whole() -> None:
    kept = {"montage_audio": {"preserve_source_audio": True, "source_media_ids": []}}
    plan = _plan(_pinned(**kept))
    cut = _cut(plan, DENIZ)
    assert cut.source_start_s == 0.0
    assert cut.source_end_s >= DENIZ_WORDS[1][1]
    assert "closing_quiet" not in plan.record()
    assert plan.record()["closing_speech"]["media_id"] == DENIZ


def test_an_introduction_that_is_not_pinned_last_is_left_alone() -> None:
    plan = _plan(_pinned(last=None))
    baseline = _plan(_pinned(last=None), speech_order=False)
    assert "closing_quiet" not in plan.record()
    assert _cut(plan, DENIZ).source_start_s == _cut(baseline, DENIZ).source_start_s


def test_the_spoken_order_survives_the_guided_and_phone_compilers() -> None:
    clips = _m1()
    plan = _plan(clips=clips)
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
    assert len(picture) == len(clips)
    assert recipe.audio.original_volume > 0  # the narration is what the viewer hears
    last = picture[-1]
    assert last.source_start >= QUIET[0] - 1e-3
    assert last.source_start + last.source_duration <= QUIET[1] + 1e-3


def test_with_no_length_asked_the_selector_keeps_the_welcome_and_the_quiet_ending() -> None:
    strategy = _pinned()
    del strategy["target_duration_s"]
    plan = _plan(strategy, view=BriefView())
    assert plan.record()["best_parts"]  # the footage selector chose the windows
    assert plan.clip_ids[:2] == [RIVER, WELCOME] and plan.clip_ids[-1] == DENIZ
    # Selected windows are whole frames: within a frame of the padded line.
    welcome = _cut(plan, WELCOME)
    assert welcome.source_start_s <= WELCOME_LINE[0] - SPEECH_LEAD_S + 1 / 30
    assert welcome.source_end_s >= WELCOME_LINE[1] + SPEECH_TAIL_S - 1 / 30
    deniz = _cut(plan, DENIZ)
    assert deniz.source_start_s >= QUIET[0] - 1e-6
    assert deniz.source_end_s <= QUIET[1] + 1e-3
    assert plan.record()["closing_quiet"]["media_id"] == DENIZ
    assert plan.record()["opening_speech"]["media_id"] == WELCOME


def test_a_narrative_order_key_is_not_an_order_basis() -> None:
    view = brief_view(
        CreativeBrief(
            requirements=[
                BriefRequirement(
                    id="r2",
                    kind="order",
                    scope="global",
                    description="start with the river, end with Deniz at his laptop",
                    facts={
                        "key": "narrative",
                        "first_clip": "the river",
                        "last_clip": "Deniz at his laptop",
                    },
                ),
                BriefRequirement(id="r9", kind="timing", scope="global", facts={"duration_s": 20}),
            ]
        )
    )
    assert not view.order_basis_stated and not view.order_by_capture
    plan = _plan(view=view)
    assert plan.clip_ids[:2] == [RIVER, WELCOME] and plan.clip_ids[-1] == DENIZ
