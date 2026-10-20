"""KRI-760: a quoted line the creator wants "baştan sona duyulsun" is cut around, not refused.

Incident (prod thread A9100235, Merope café M1, 2026-10-19): the brief filed
``"Hi, I'm Karolina, I'm from Ecuador" dediği cümle baştan sona duyulsun`` as an ``audio``
requirement with ``facts={"triggers": [line], "keep_whole_take": True}``. The pop-in checker
took it, answered "photo and sound pop-ins aren't available", and the creator's "simpler
version" then dropped the ask, so the draft had no intent for the line at all.

This replays the ask end to end on made-up café clips (the incident's own clip data was not
saved; the transcript shapes are the prod ones): brief -> length plan -> unified montage ->
finished timeline -> render-ready receipt.

Ways this can fail, each pinned below (written before the code):
1. The receipt calls the ask a pop-in / "not possible" (the incident).
2. A quoted line with commas ("Hi, I'm Karolina, I'm from Ecuador") is split at the commas, no
   piece matches the clip's words, and the clip keeps its default cut (the line is clipped).
3. The line is in no clip's words: the receipt must say so, never "met".
4. The cut is shorter than the line: never "met".
5. Two clips say the line equally: nothing moves, nothing is claimed.
6. The draft (nothing rendered) is judged as a failure.
7. A montage with no whole-line ask keeps its windows byte for byte.
"""

from __future__ import annotations

import json

import pytest

from app.config import settings
from app.kria.brief import BriefRequirement, CreativeBrief
from app.kria.brief_checks import (
    PlanFacts,
    check_requirement,
    is_judged,
    plan_facts_from_rendered_montage,
)
from app.kria.reply_language import reply_language_for
from tests.tasks.test_named_line_windows_montage import _finished, _holds, _plan, _render, _window

LINE = "Hi, I'm Karolina, I'm from Ecuador"
KAROLINA = "m1_06"
# Where the line is in the Karolina clip (the incident's was 0.2-2.2 s of an 8 s clip).
INCIDENT_SPAN = (0.2, 2.2)
# A line deeper in the clip, where a default cut from second 0 can't hold it.
DEEP_SPAN = (5.0, 7.2)


def _clip(media_id: str, duration: float, segments: list[tuple[float, float, str]]) -> dict:
    text = " ".join(s[2] for s in segments)
    speech = (
        {
            "has_speech": True,
            "language": "en",
            "transcript": text,
            "segments": [{"text": t, "start_s": a, "end_s": b} for a, b, t in segments],
        }
        if segments
        else {"has_speech": False, "transcript": ""}
    )
    return {
        "media_id": media_id,
        "duration_s": duration,
        "analysis": {
            "transcript": text,
            "best_moments": [
                {"start_s": 0.0, "end_s": min(2.2, duration), "energy": 3.0, "description": "x"}
            ],
            "understanding": {"speech": speech},
        },
    }


def _clips(span: tuple[float, float] | None = INCIDENT_SPAN, *, says: str = LINE) -> list[dict]:
    karolina = [(span[0], span[1], says + ".")] if span is not None else []
    return [
        _clip("m1_01", 9.0, []),
        _clip("m1_02", 12.0, [(1.0, 3.0, "Mmm, tadı çok güzel.")]),
        _clip("m1_03", 8.0, []),
        _clip("m1_04", 10.0, []),
        _clip("m1_05", 7.0, []),
        _clip(KAROLINA, 10.0, karolina),
        _clip("m1_07", 9.0, []),
        _clip("m1_08", 11.0, []),
    ]


def _r5(**over) -> BriefRequirement:
    """r5 exactly as prod filed it."""
    return BriefRequirement(
        id="r5",
        kind="audio",
        scope="global",
        description=f'"{LINE}" dediği cümle baştan sona duyulsun',
        facts={"triggers": [LINE], "keep_whole_take": True},
        **over,
    )


def _brief(*extra: BriefRequirement) -> CreativeBrief:
    return CreativeBrief(
        version=3,
        requirements=[
            BriefRequirement(id="r1", kind="text", scope="title", literal="Kolombiya tatlısı"),
            BriefRequirement(
                id="r2",
                kind="timing",
                scope="global",
                description="20 saniye",
                facts={"duration_s": 20},
            ),
            *(extra or (_r5(),)),
        ],
    )


def _strategy() -> dict:
    return {
        "edit_format": "montage",
        "render_program": "guided",
        "direction": "guided_story",
        "pacing": "fast",
        "media_scope": "all",
        "selected_media_ids": [],
        "audio_strategy": "original_audio",
        "montage_audio": {"preserve_source_audio": True, "source_media_ids": []},
        "opening_title": "Kolombiya tatlısı",
        "target_duration_s": 20.0,
        "creator_duration_s": 20.0,
        "target_duration_requested": True,
        "video_reuse_policy": "once",
        "resolved_clip_intents": [],
    }


@pytest.fixture(autouse=True)
def _clip_intents_on(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(settings, "clip_intents_enabled", True)


async def _render_receipt(span, *, clips=None, says: str = LINE):
    clips = clips or _clips(span, says=says)
    brief, strategy = _brief(), _strategy()
    snapshot, _quote = await _plan(strategy, brief, clips=clips)
    plan, _contract, _unified = _render(strategy, snapshot, brief, clips)
    facts = plan_facts_from_rendered_montage(_finished(plan), plan.record())
    return snapshot, plan, facts, check_requirement(_r5(), facts)


@pytest.mark.parametrize("span", [INCIDENT_SPAN, DEEP_SPAN], ids=["incident", "deep-in-clip"])
async def test_the_montage_cuts_the_karolina_clip_around_her_line_and_the_review_says_met(
    span,
) -> None:
    snapshot, plan, facts, receipt = await _render_receipt(span)

    # 2: the quoted line (commas and all) seats the clip's window on the line.
    assert _holds(*_window(snapshot, KAROLINA), span)
    cut = next(c for c in plan.snapshot.fast_cuts if c.media_id == KAROLINA)
    assert _holds(cut.source_start_s, cut.source_end_s, span)

    # 1: judged, and met - never a pop-in, never "not possible".
    assert receipt.status == "met", receipt.reason
    assert LINE in (receipt.reason or "")
    assert is_judged(_r5(), receipt)
    assert plan.duration_s == pytest.approx(20.0, abs=0.1)


async def test_a_line_no_clip_says_is_reported_unmet_and_names_the_line() -> None:
    # 3: the transcript has other words only.
    _snap, _plan_, _facts, receipt = await _render_receipt(
        DEEP_SPAN, says="Merhaba arkadaşlar nasılsınız"
    )
    assert receipt.status == "partial"
    assert LINE in (receipt.reason or "")
    assert "pop-in" not in (receipt.reason or "").lower()


async def test_a_cut_shorter_than_the_line_is_never_met() -> None:
    # 4: a 9 s monologue; the planner keeps ~6 s of it when lengths are shared.
    long_line = (0.5, 9.5)
    snapshot, plan, facts, receipt = await _render_receipt(long_line)
    assert not _holds(*_window(snapshot, KAROLINA), long_line)
    assert [line.status for line in facts.named_lines or ()] == ["not_held"]
    assert receipt.status == "partial"
    assert "doesn't hold the whole line" in (receipt.reason or "")
    assert LINE in (receipt.reason or "")


async def test_the_finished_timeline_has_the_last_word_over_the_plan_record() -> None:
    # 4: the plan record says held; the finished cut is shorter than the line.
    _snap, plan, _facts, _receipt = await _render_receipt(INCIDENT_SPAN)
    finished = _finished(plan)
    for row in finished["story_timeline"]:
        if row.get("media_id") == KAROLINA:
            row["source_end_s"] = INCIDENT_SPAN[1] - 0.5
    facts = plan_facts_from_rendered_montage(finished, plan.record())
    assert [line.status for line in facts.named_lines or ()] == ["not_held"]
    assert check_requirement(_r5(), facts).status == "partial"


async def test_the_clips_own_sound_off_is_not_met() -> None:
    _snap, plan, _facts, _receipt = await _render_receipt(INCIDENT_SPAN)
    finished = {**_finished(plan), "source_audio_preserved": False}
    facts = plan_facts_from_rendered_montage(finished, plan.record())
    receipt = check_requirement(_r5(), facts)
    assert receipt.status == "partial"
    assert "own sound is off" in (receipt.reason or "")


async def test_the_receipt_speaks_the_chat_language() -> None:
    _snap, _plan_, facts, _receipt = await _render_receipt(INCIDENT_SPAN)
    with reply_language_for("tr"):
        receipt = check_requirement(_r5(), facts)
    assert receipt.status == "met"
    assert "baştan sona" in (receipt.reason or "")


async def test_a_line_two_clips_say_equally_moves_nothing_and_claims_nothing() -> None:
    # 5: both clips say it once.
    clips = _clips(INCIDENT_SPAN)
    twin = json.loads(json.dumps(next(c for c in clips if c["media_id"] == KAROLINA)))
    twin["media_id"] = "m1_09"
    clips.append(twin)
    snapshot, _plan_, _facts, receipt = await _render_receipt(INCIDENT_SPAN, clips=clips)
    assert snapshot["length_plan"]["named_lines"][0]["status"] == "ambiguous"
    assert receipt.status == "partial"
    assert "More than one clip says" in (receipt.reason or "")
    assert "pop-in" not in (receipt.reason or "").lower()


def test_the_draft_is_neutral_not_a_failure() -> None:
    # 6: nothing rendered yet.
    receipt = check_requirement(
        _r5(), PlanFacts(reaction_beats_available=False, reaction_beats=(), edit_format="montage")
    )
    assert receipt.status != "not_possible"
    assert not is_judged(_r5(), receipt)


async def test_an_edit_without_the_ask_keeps_its_windows() -> None:
    # 7: the same clips and a brief with no whole-line ask: no named line, and the line's
    # clip keeps its default window (this ask is what moves it).
    plain = _brief(
        BriefRequirement(id="r5", kind="audio", scope="global", description="Sakin bir müzik")
    )
    snapshot, _quote = await _plan(_strategy(), plain, clips=_clips(DEEP_SPAN))
    assert "named_lines" not in snapshot["length_plan"]
    assert not _holds(*_window(snapshot, KAROLINA), DEEP_SPAN)
    asked, _quote = await _plan(_strategy(), _brief(), clips=_clips(DEEP_SPAN))
    assert _holds(*_window(asked, KAROLINA), DEEP_SPAN)
