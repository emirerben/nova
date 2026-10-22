"""KRI-790: a quoted start AND end line inside one phone Talking take (Merope café T1).

"Start at "Hi, I'm Karolina, I'm from Ecuador" and end right after "I'm gonna be here all
week"" in a 68.37 s take: every draft said "A Talking edit keeps your whole take ... your take
is 68.4s", no draft or approval card named either line, and the worker had no end cut at all.

End to end through the real pieces, on the take's real timings
(``tests/fixtures/kri790_merope_t1_take.json``): the brief rows prod stored -> the ask
(`take_window_ask`) -> the lines grounded in the take's words -> the cleanup `CutPlan` (on)
or none (off) -> `compile_phone_subtitled_plan` + the caption remap -> the receipts the draft
and the finished video are judged by, through the worker's own `_phone_opening_cut`.

How it can fail (written before the code):

1. The lines live in facts the cut never reads: prod filed them as `facts.start` /
   `facts.end` on an `order` requirement (KRI-789 files them as `timing` +
   `facts.start_line` / `facts.end_line`), and after the order answer the description no
   longer quotes them. Every shape must give the same ask; a route's place names or a text
   row must never become a line.
2. The start line is misheard: the transcript says "Hi, I'm Carolina from Ecuador" (C, no
   second "I'm"), 5 of 8 tokens, so the token match misses it and the video opens at 0:00.
3. A loose match lands on another sentence (Yasin's "we are with Caro today"), or a two-word
   quote matches a different two words.
4. The end line is looked for before the start line (a phrase said twice), or is said only
   before it: the window would be empty or backwards.
5. The cut eats the first word ("Hi"), keeps the word before it ("from?"), clips "week", or
   keeps the next speaker's "Nice".
6. With cleanup on, the window drops cleanup's own cuts inside it, or keep + removed no
   longer partition the take; with cleanup off, nothing is cut.
7. Captions still show Yasin's intro or Atakan's goodbye, or the compiled video runs past
   the end line.
8. The draft's length line still measures the whole take (68.4 s), or states a number for a
   line it could not place.
9. The draft / approval card names neither line; after render a both-lines ask reads "Done"
   when only one side was cut, or an older render with no end receipt reads "Done".
10. A Turkish chat gets English, or the new plan text triggers the "simpler version?" question.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from app.kria.brief import BriefRequirement, CreativeBrief
from app.kria.brief_binding import BriefBinding
from app.kria.brief_checks import (
    PlanFacts,
    build_receipts,
    is_format_limit,
    is_judged,
    plan_facts_from_phone_variant,
)
from app.kria.media_sources import OriginalMediaDescriptor
from app.kria.opening_cut_ask import estimate_take_window, take_window_ask
from app.kria.reply_language import reply_language_for
from app.pipeline.phone_opening_cut import END_CUT_REASON, OPENING_CUT_REASON, apply_take_window
from app.pipeline.phone_subtitled_plan import compile_phone_subtitled_plan
from app.pipeline.silence_cut import build_cut_plan, no_op_plan, remap_words
from app.services.phone_sources import PhoneSourceBinding
from app.services.speech_segments import ground_line
from app.tasks import generative_build as gb

FIXTURE = json.loads(
    (Path(__file__).resolve().parents[1] / "fixtures" / "kri790_merope_t1_take.json").read_text()
)
TAKE_S = float(FIXTURE["duration_s"])
WORDS = FIXTURE["words"]
SEGMENTS = FIXTURE["segments"]
START = "Hi, I'm Karolina, I'm from Ecuador"
END = "I'm gonna be here all week"
# Where the lines are said in this take (kit README: 0:12.0 and 0:50.4).
HI_S = next(w["start_s"] for w in WORDS if w["text"] == "Hi,")
WEEK_END_S = next(w["end_s"] for w in WORDS if w["text"] == "week.")


def _reqs(version: str) -> list[BriefRequirement]:
    return [BriefRequirement.model_validate(row) for row in FIXTURE[version].values()]


def _kri789_shape() -> list[BriefRequirement]:
    r1 = BriefRequirement.model_validate(FIXTURE["brief_v1"]["r1"])
    r2 = BriefRequirement(
        id="r2",
        kind="timing",
        scope="global",
        description=FIXTURE["brief_v1"]["r2"]["description"],
        facts={"start_line": START, "end_line": END},
    )
    return [r1, r2]


def _snapshot() -> dict:
    return {
        "clip_assignments": [
            {
                "kind": "video",
                "duration_s": TAKE_S,
                "analysis": {
                    "duration_s": TAKE_S,
                    "understanding": {"speech": {"segments": SEGMENTS}},
                },
            }
        ]
    }


def _binding() -> PhoneSourceBinding:
    return PhoneSourceBinding(
        media_id="clip-0",
        proxy_path="user/analysis-proxy-clip-0.mp4",
        generation="1",
        original=OriginalMediaDescriptor(
            sha256="a" * 64,
            byte_count=1000,
            duration_s=TAKE_S,
            width=1080,
            height=1920,
            orientation_degrees=0,
            has_audio=True,
        ),
    )


def _cleanup_plan():
    # What a real cleanup of this take cuts: every pause of 0.6 s or more between words.
    gaps = [
        (a["end_s"], b["start_s"])
        for a, b in zip(WORDS, WORDS[1:], strict=False)
        if b["start_s"] - a["end_s"] >= 0.6
    ]
    return build_cut_plan(WORDS, gaps, TAKE_S)


# -- 1. one ask, whatever shape the brief stored ----------------------------------------------


@pytest.mark.parametrize("shape", ["brief_v1", "brief_v2", "kri789"])
def test_every_stored_shape_asks_for_both_lines(shape: str) -> None:
    reqs = _kri789_shape() if shape == "kri789" else _reqs(shape)
    ask = take_window_ask(reqs)
    assert ask is not None
    assert (ask.start_quote, ask.end_quote) == (START, END)
    assert ask.start_ids == ask.end_ids == ("r2",)


def test_place_names_and_text_rows_are_never_lines() -> None:
    route = BriefRequirement(
        id="r1",
        kind="order",
        scope="global",
        description="I ran from Arnavutköy to Eminönü",
        facts={"key": "capture_time", "start": "Arnavutköy", "end": "Eminönü", "distance_km": 20},
    )
    text = BriefRequirement(
        id="r2",
        kind="text",
        scope="title",
        literal="20K",
        facts={"start": "Hi, I'm Karolina", "end": "here all week"},
    )
    assert take_window_ask([route, text]) is None


# -- 2/3. the lines in the take ---------------------------------------------------------------


def test_the_misheard_start_line_is_found_where_she_says_it() -> None:
    grounded = ground_line(WORDS, START, duration_s=TAKE_S)
    assert grounded is not None and not grounded.exact
    assert grounded.word_start_s == HI_S


def test_a_loose_match_never_lands_on_another_line() -> None:
    # Yasin's intro mentions Caro; the line is not there.
    intro = [w for w in WORDS if w["end_s"] <= HI_S]
    assert ground_line(intro, START, duration_s=TAKE_S) is None
    assert ground_line(WORDS, "Hi, I'm Valentina, I'm from Peru", duration_s=TAKE_S) is None
    # Two words stay exact: "here all" is said, "hear all" is not.
    assert ground_line(WORDS, "hear all", duration_s=TAKE_S) is None


# -- 4. the end line follows the start line ---------------------------------------------------


def _tiny(*spoken: tuple[str, float, float]) -> list[dict]:
    return [{"text": t, "start_s": s, "end_s": e} for t, s, e in spoken]


def test_the_end_line_is_the_first_one_after_the_start_line() -> None:
    words = _tiny(
        ("see", 0.5, 0.8), ("you", 0.8, 1.0), ("all", 1.0, 1.3), ("week", 1.3, 1.7),
        ("hello", 3.0, 3.4), ("my", 3.4, 3.6), ("friends", 3.6, 4.2), ("today", 4.2, 4.8),
        ("we", 5.0, 5.2), ("cook", 5.2, 5.6), ("see", 6.0, 6.3), ("you", 6.3, 6.5),
        ("all", 6.5, 6.8), ("week", 6.8, 7.2), ("bye", 8.0, 8.4),
    )  # fmt: skip
    plan, start, end = apply_take_window(
        no_op_plan(10.0),
        words,
        start_quote="hello my friends today",
        end_quote="see you all week",
        duration_s=10.0,
    )
    assert start["status"] == end["status"] == "applied"
    assert plan.keep_segments == [(start["start_s"], end["end_s"])]
    assert 2.8 < start["start_s"] < 3.0 and 7.2 <= end["end_s"] <= 7.45

    _plan, _start, only_before = apply_take_window(
        no_op_plan(10.0),
        words[:10],
        start_quote="hello my friends today",
        end_quote="see you all week",
        duration_s=10.0,
    )
    assert (only_before["status"], only_before["reason"]) == ("not_applied", "before_start")


# -- 5/6/7. the cut, with cleanup off and on, through the phone compiler -----------------------


def _kept_words(plan) -> list[str]:  # noqa: ANN001
    return [w["text"] for w in remap_words(WORDS, plan)]


@pytest.mark.parametrize("cleanup", ["off", "on"])
def test_the_video_starts_on_hi_and_ends_after_here_all_week(cleanup: str) -> None:
    base = no_op_plan(TAKE_S) if cleanup == "off" else _cleanup_plan()
    ask = take_window_ask(_reqs("brief_v2"))
    plan, start, end = apply_take_window(
        base,
        WORDS,
        start_quote=ask.start_quote,
        end_quote=ask.end_quote,
        duration_s=TAKE_S,
        start_ids=ask.start_ids,
        end_ids=ask.end_ids,
    )
    assert start["status"] == end["status"] == "applied"
    # Between "from?" (12.18) and "Hi," (12.35); between "week." and Yasin's "Nice,".
    assert 12.18 < plan.keep_segments[0][0] <= HI_S
    assert plan.keep_segments[-1][1] == pytest.approx(WEEK_END_S)
    assert plan.removed[0].reason == OPENING_CUT_REASON
    assert plan.removed[-1].reason == END_CUT_REASON
    kept = sum(e - s for s, e in plan.keep_segments)
    assert kept + plan.time_saved_s == pytest.approx(TAKE_S, abs=0.01)
    if cleanup == "on":
        # Cleanup's own cuts inside the window stay: shorter than the 38.1 s window.
        assert any(r.reason not in {OPENING_CUT_REASON, END_CUT_REASON} for r in plan.removed)
        assert kept < WEEK_END_S - plan.keep_segments[0][0] - 1.0
    else:
        assert plan.keep_segments == [(plan.keep_segments[0][0], WEEK_END_S)]

    said = _kept_words(plan)
    assert said[:4] == ["Hi,", "I'm", "Carolina", "from"]
    assert said[-3:] == ["here", "all", "week."]
    assert "Merhaba" not in said and "bite." not in said and "Nice," not in said

    recipe = compile_phone_subtitled_plan((_binding(),), caption_cues=[], cut_plan=plan)
    video = next(t for t in recipe.tracks if t.kind == "video")
    assert video.clips[0].source_start == pytest.approx(plan.keep_segments[0][0])
    assert recipe.duration == pytest.approx(kept, abs=0.01)


def test_the_worker_cuts_both_lines_from_the_approved_brief() -> None:
    """Through `_phone_opening_cut` (the seam the KRI-787 replay calls), cleanup off with the
    checked preflight words, and on with the cleanup plan."""
    brief = CreativeBrief(version=2, requirements=_reqs("brief_v2"))
    binding = BriefBinding.create("32a5b608-6397-4608-889f-dff5ad9bec40", brief)
    job = {"creator_brief_binding": binding.model_dump(mode="json")}
    for cut_plan, words, off_words in ((None, (), WORDS), (_cleanup_plan(), WORDS, None)):
        plan, receipts = gb._phone_opening_cut(
            job, cut_plan, words, duration_s=TAKE_S, off_words=off_words
        )
        assert plan is not None
        assert set(receipts) == {"opening_cut", "end_cut"}
        assert {r["status"] for r in receipts.values()} == {"applied"}
        assert receipts["end_cut"]["quote"] == END
        assert plan.keep_segments[-1][1] == pytest.approx(WEEK_END_S)

    # Cleanup declined and no words to ground the lines in: both sides say so.
    plan, receipts = gb._phone_opening_cut(job, None, (), duration_s=TAKE_S, off_words=None)
    assert plan is None
    assert {r["reason"] for r in receipts.values()} == {"cleanup_off"}


# -- 8/9/10. what the draft, the approval card and the finished video say ---------------------


def _draft_receipts(reqs: list[BriefRequirement], language: str | None = None) -> dict:
    estimate = estimate_take_window(reqs, _snapshot(), strategy={"media_scope": "selected"})
    facts = PlanFacts(
        edit_format="subtitled", phone_talking=True, take_duration_s=TAKE_S, take_window=estimate
    )
    with reply_language_for(language):
        receipts = build_receipts(reqs, facts, include_unchecked=True, strict_order=True)
    return {r.requirement_id: r for r in receipts}


@pytest.mark.parametrize("shape", ["brief_v1", "brief_v2", "kri789"])
def test_the_draft_measures_the_window_and_names_both_lines(shape: str) -> None:
    reqs = _kri789_shape() if shape == "kri789" else _reqs(shape)
    receipts = _draft_receipts(reqs)
    length = receipts["r1"].reason or ""
    assert "whole take" not in length and "68.4" not in length
    assert "about 38.6s and you asked for 35s" in length
    lines = receipts["r2"].reason or ""
    assert f"“{START}” (0:12)" in lines
    assert f"“{END}” (0:50)" in lines
    for req_id in ("r1", "r2"):
        receipt = receipts[req_id]
        assert is_judged(next(r for r in reqs if r.id == req_id), receipt)
        assert is_format_limit(receipt.reason)  # a limit of the format, never "simpler version?"


def test_a_turkish_chat_reads_the_window_in_turkish() -> None:
    receipts = _draft_receipts(_reqs("brief_v2"), language="tr")
    assert "o kısım yaklaşık 38.6 saniye, sen 35 saniye istedin" in (receipts["r1"].reason or "")
    assert "cümlesinin hemen sonrasına kadar" in (receipts["r2"].reason or "")
    assert all(is_format_limit(r.reason) for r in receipts.values())


def test_a_line_the_draft_cannot_place_gives_only_the_take_length() -> None:
    reqs = _reqs("brief_v2")
    reqs[1] = reqs[1].model_copy(update={"facts": {**reqs[1].facts, "end": "nunca dije esto aqui"}})
    receipts = _draft_receipts(reqs)
    length = receipts["r1"].reason or ""
    assert "only the part of your take" in length and "your take is 68.4s" in length
    assert "whole take" not in length and "38.6" not in length
    assert "(0:12)" in (receipts["r2"].reason or "")
    assert "“nunca dije esto aqui”, if" in (receipts["r2"].reason or "")


def _rendered(**receipts: dict) -> PlanFacts:
    return plan_facts_from_phone_variant(
        {
            "variant_id": "subtitled",
            "resolved_archetype": "subtitled",
            "render_destination": "device",
            "caption_cues": [],
            "text_elements": [],
            "duration_s": 36.0,
            **receipts,
        }
    )


def test_the_finished_video_is_done_only_when_both_lines_were_cut() -> None:
    reqs = _reqs("brief_v2")
    _plan, start, end = apply_take_window(
        no_op_plan(TAKE_S), WORDS, start_quote=START, end_quote=END, duration_s=TAKE_S
    )
    done = build_receipts(reqs, _rendered(opening_cut=start, end_cut=end), include_unchecked=True)
    assert {r.requirement_id: r.status for r in done} == {"r1": "met", "r2": "met"}

    missed = {**end, "status": "not_applied", "reason": "not_found"}
    [_r1, half] = build_receipts(
        reqs, _rendered(opening_cut=start, end_cut=missed), include_unchecked=True
    )
    assert half.status == "partial"
    assert f"couldn't find “{END}”" in (half.reason or "")

    # A render that recorded only the start (older code) cannot certify the end.
    [_r1, old] = build_receipts(reqs, _rendered(opening_cut=start), include_unchecked=True)
    assert (old.status, old.verification) == ("partial", "unchecked")
