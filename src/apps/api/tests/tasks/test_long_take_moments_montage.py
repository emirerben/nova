"""KRI-596 + KRI-597: "six very short moments from two long takes, end on the line I say".

Incident (prod thread 6605cf61, 2026-10-11, cabin kit M2): two long handheld takes
(70 s indoors, 67 s outdoors) plus two dark clips. "Pick the best six moments from the
long takes, each very short, and skip the dark ones. Keep the original sound, and end on
the moment I say '...'. Make it 18 seconds." The phone rendered two cuts (7.75 s + 10.25 s)
and ended on the indoor take; the quoted line, spoken in the outdoor take, was not in the
video. Both receipts said "met".

Ways this can fail, each pinned below:

KRI-597 (the quoted line)
1. The resolver seats the quote on a clip whose transcript does not say it (it saw only
   the first 200 transcript characters and invented the evidence).
2. The creator quotes the line in a different word order than the transcript has it.
3. Two clips say the line: nothing is guessed.
4. The clip's timed segments miss the line (whisper dropped it; a hallucinated "thanks
   for watching" sits after it): a word-timed pass must find it, or the plan says so.
5. The line is found but the closing cut does not hold it, or is not the last cut.
6. The total length drifts off the creator's 18 s.
7. A receipt says "met" when the line is not in the finished video.

KRI-596 (several moments from one take)
8. "six moments" / "altı an" / "6 shots" is not read; "keep it short" (the whole video)
   is misread as short moments.
9. A cut runs longer than "very short" (3 s).
10. Two windows of one take overlap or leave the source.
11. One take's windows are scattered (the contract's clip order breaks) or out of
    source order; the quoted line is not the last of its take.
12. A selected take is left out.
13. An edit that asks for none of this changes at all.
14. The approved envelope with repeated takes is refused by the worker, the snapshot or
    the phone verifier.
15. The moment receipt stays "can't verify".
"""

from __future__ import annotations

import dataclasses
import uuid

import pytest

from app.config import settings
from app.kria.brief import BriefRequirement, BriefUpdate, CreativeBrief, apply_updates
from app.kria.brief_binding import BriefBinding
from app.kria.brief_checks import (
    check_requirement,
    plan_facts_from_rendered_montage,
)
from app.kria.planner import _enrich_best_parts_snapshot, _moment_count_notice
from app.kria.style_asks import MomentAsk, moment_ask
from app.pipeline.best_parts import select_best_parts
from app.pipeline.guided_story import compile_execution_plan
from app.pipeline.unified_montage import brief_view, plan_unified_montage
from app.schemas.clip_intents import ResolvedClipIntent
from app.services.clip_intent_resolution import IntentClip
from app.services.creator_render_contract import build_render_contract
from app.services.length_planning import read_length_plan
from app.services.spoken_quotes import (
    closing_quote_notice,
    ground_closing_quote,
    regound_quoted_intents,
)
from tests.pipeline.test_unified_montage import clip
from tests.tasks.test_selected_clip_dispatch import _verify

DARK_A, DARK_B, INDOOR, OUTDOOR = "c0", "c1", "c2", "c3"
DURATIONS = {DARK_A: 5.0, DARK_B: 7.0, INDOOR: 70.0, OUTDOOR: 67.0}
QUOTE = "Huzuru bulduk burada"
# The transcript says it in another order, with a filler word.
SPOKEN = "Burada huzuru bulduk biz ya."

INDOOR_MOMENTS = [
    (0.0, 2.67, 6.0),
    (2.67, 6.57, 5.0),
    (12.6, 17.0, 6.5),
    (26.5, 35.0, 7.0),
    (40.0, 48.0, 6.0),
]
OUTDOOR_MOMENTS = [
    (2.5, 7.0, 5.0),
    (9.5, 15.0, 5.0),
    (22.0, 27.0, 6.0),
    (39.5, 47.0, 7.0),
    (56.0, 62.0, 8.0),
]


def _moments(rows):
    return [{"start_s": s, "end_s": e, "energy": n, "description": "moment"} for s, e, n in rows]


def _analysis(media_id: str) -> dict:
    if media_id == INDOOR:
        return {
            "transcript": "Ev turumuza hoş geldiniz. Şu an mutfağı görüyorsunuz. "
            "Dün akşam ben burada kaldım.",
            "best_moments": _moments(INDOOR_MOMENTS),
            "understanding": {
                "speech": {
                    "has_speech": True,
                    "language": "tr",
                    "transcript": "Ev turumuza hoş geldiniz.",
                    "segments": [
                        {"text": "Ev turumuza hoş geldiniz.", "start_s": 1.9, "end_s": 3.5},
                        {"text": "Şu an mutfağı görüyorsunuz.", "start_s": 7.3, "end_s": 8.6},
                        {"text": "Dün akşam ben burada kaldım.", "start_s": 30.3, "end_s": 33.4},
                    ],
                }
            },
        }
    if media_id == OUTDOOR:
        return {
            # The untimed transcript has the line; the timed segments lost it.
            "transcript": "Şurada mangal var. Hamaklarımız, orman. Şimdi iskeleye "
            f"gidiyoruz. Ayaklarımda sadece çorap var. {SPOKEN}",
            "best_moments": _moments(OUTDOOR_MOMENTS),
            "understanding": {
                "speech": {
                    "has_speech": True,
                    "language": "tr",
                    "transcript": "Şurada mangal var. Hamaklarımız, orman.",
                    "segments": [
                        {"text": "Şurada mangal var.", "start_s": 1.9, "end_s": 8.0},
                        {"text": "Hamaklarımız, orman.", "start_s": 18.1, "end_s": 28.4},
                        {"text": "Şimdi iskeleye gidiyoruz.", "start_s": 36.7, "end_s": 38.8},
                        {"text": "Ayakta sadece çorap var.", "start_s": 40.0, "end_s": 42.6},
                        # whisper's outro hallucination on trailing wind
                        {
                            "text": "İzlediğiniz için teşekkür ederim.",
                            "start_s": 65.6,
                            "end_s": 67.0,
                        },
                    ],
                }
            },
        }
    return {
        "transcript": "",
        "best_moments": [],
        "understanding": {"speech": {"has_speech": False}},
    }


def _unified_clips():
    return [
        dataclasses.replace(
            clip(i, duration=DURATIONS[f"c{i}"]),
            analysis=_analysis(f"c{i}"),
            width=1080,
            height=1920,
        )
        for i in range(4)
    ]


def _snapshot() -> dict:
    return {
        "clip_assignments": [
            {
                "media_id": c.media_id,
                "kind": "video",
                "gcs_path": c.proxy_path,
                "storage_generation": c.generation,
                "duration_s": c.duration_s,
                "analysis": _analysis(c.media_id),
            }
            for c in _unified_clips()
        ]
    }


def _wrong_seat() -> ResolvedClipIntent:
    """The incident's resolver answer: the indoor take, with evidence it never had."""
    return ResolvedClipIntent.model_validate(
        {
            "op": "order",
            "status": "resolved",
            "intent_id": "order_last_moment",
            "attribute": f"the moment I say '{QUOTE}'",
            "position": "last",
            "assignments": [
                {"media_id": INDOOR, "confidence": 1.0, "evidence": f"transcript: '...{QUOTE}'"}
            ],
        }
    )


def _strategy(intents: list[ResolvedClipIntent], **extra) -> dict:
    return {
        "edit_format": "montage",
        "render_program": "guided",
        "direction": "guided_story",
        "pacing": "fast",
        "media_scope": "selected",
        "selected_media_ids": [INDOOR, OUTDOOR],
        "audio_strategy": "original_audio",
        "montage_audio": {"preserve_source_audio": True, "source_media_ids": []},
        "opening_title": "Our cabin in Tigre",
        "target_duration_s": 18.0,
        "creator_duration_s": 18.0,
        "target_duration_requested": True,
        "video_reuse_policy": "once",
        "resolved_clip_intents": [i.model_dump(mode="json") for i in intents],
        **extra,
    }


R3 = BriefRequirement(
    id="r3",
    kind="select",
    scope="global",
    description="Pick the best six moments from the long takes, each very short, "
    "and skip the dark ones",
)
R5 = BriefRequirement(
    id="r5",
    kind="order",
    scope="global",
    description=f"End on the moment I say '{QUOTE}'",
    facts={"last_clip": f"the moment I say '{QUOTE}'"},
)


def _brief(*extra: BriefRequirement) -> CreativeBrief:
    base = [
        BriefRequirement(
            id="r1",
            kind="timing",
            scope="global",
            description="Make it 18 seconds",
            facts={"duration_s": 18},
        ),
        BriefRequirement(
            id="r2", kind="text", scope="title", literal="Our cabin in Tigre", description=None
        ),
    ]
    return CreativeBrief(version=1, requirements=[*base, *extra])


def _resolved(brief: CreativeBrief, snapshot: dict) -> CreativeBrief:
    binding = BriefBinding.create(
        uuid.uuid5(uuid.NAMESPACE_URL, "kri-596"),
        brief,
        latest_message="six moments, end on my line",
        media_snapshot=snapshot,
    )
    return binding.resolve()


def _words(*, with_line: bool = True) -> list[dict]:
    """The word-timed pass on the outdoor proxy: the line at 43.1-45.4 s."""
    rows = [("Ayakta", 40.1), ("sadece", 40.9), ("çorap", 41.6), ("var.", 42.2)]
    if with_line:
        rows += [("Burada", 43.1), ("huzuru", 43.6), ("bulduk", 44.2), ("biz", 44.8), ("ya.", 45.1)]
    return [{"text": t, "start_s": s, "end_s": round(s + 0.3, 2)} for t, s in rows]


def _transcriber(words: list[dict], calls: list | None = None):
    def run(gcs_path: str, *, language: str | None, prompt: str | None):
        if calls is not None:
            calls.append((gcs_path, language, prompt))
        return words, language or ""

    return run


@pytest.fixture(autouse=True)
def _clip_intents_on(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(settings, "clip_intents_enabled", True)


async def _plan(*, words: list[dict], brief: CreativeBrief | None = None, intents=None):
    """The planner's path: re-seat by transcript, ground the line, freeze the length plan."""
    intent_clips = [
        IntentClip(c.media_id, "video", _analysis(c.media_id)) for c in _unified_clips()
    ]
    seated, moved = regound_quoted_intents(intents or [_wrong_seat()], intent_clips)
    strategy = _strategy(seated)
    snapshot = _snapshot()
    quote = await ground_closing_quote(strategy, snapshot, transcribe=_transcriber(words))
    brief = brief or _brief(R3, R5)
    enriched = _enrich_best_parts_snapshot(strategy, snapshot, brief, closing_quote=quote)
    return strategy, enriched, quote, moved, brief


def _render(strategy: dict, snapshot: dict, brief: CreativeBrief):
    resolved = _resolved(brief, snapshot)
    clips = [c for c in _unified_clips() if c.media_id in (INDOOR, OUTDOOR)]
    plan = plan_unified_montage(
        clips,
        brief_view(resolved),
        strategy=strategy,
        clip_intents_enabled=True,
        creator_duration_proven=True,
        approved_length_plan=snapshot["length_plan"],
    )
    contract = build_render_contract(
        strategy, generation_id="gen", brief=resolved, media_snapshot=snapshot
    )
    return plan, contract, clips


def _finished(plan) -> dict:
    execution = compile_execution_plan(plan.guided_edit(), track=None)
    return {"story_timeline": execution["story_timeline"], "source_audio_preserved": True}


# -- the incident, end to end ----------------------------------------------------------


async def test_incident_six_short_moments_end_on_the_quoted_line_and_verify() -> None:
    strategy, snapshot, quote, moved, brief = await _plan(words=_words())

    # 1 + 2: the quote moves to the take that says it, despite the word order.
    assert moved == 1
    seat = strategy["resolved_clip_intents"][0]["assignments"]
    assert [a["media_id"] for a in seat] == [OUTDOOR]
    # 4: the timed segments lost the line; the word-timed pass found it.
    assert quote["status"] == "found" and quote["media_id"] == OUTDOOR
    assert quote["start_s"] <= 43.1 and quote["end_s"] >= 45.4
    assert quote["end_s"] - quote["start_s"] < 4.0

    windows = snapshot["length_plan"]["windows"]
    assert not snapshot["length_plan"]["conflicts"]
    # 8 + 9: six moments, none longer than 3 s; 6: still 18 s.
    assert len(windows) == 6
    assert all(w["source_end_s"] - w["source_start_s"] <= 3.0 + 1e-6 for w in windows)
    assert sum(w["source_end_s"] - w["source_start_s"] for w in windows) == pytest.approx(
        18.0, abs=0.05
    )
    # 12: both takes give moments.
    assert {w["media_id"] for w in windows} == {INDOOR, OUTDOOR}

    plan, contract, clips = _render(strategy, snapshot, brief)
    cuts = plan.snapshot.fast_cuts
    assert len(cuts) == 6
    assert plan.snapshot.video_reuse_policy == "distinct_windows"
    # 11: each take's moments play together, in source order, the line last.
    ids = [c.media_id for c in cuts]
    assert ids == sorted(ids, key=[INDOOR, OUTDOOR].index)
    for media_id in (INDOOR, OUTDOOR):
        own = [(c.source_start_s, c.source_end_s) for c in cuts if c.media_id == media_id]
        body = own[:-1] if media_id == OUTDOOR else own
        assert body == sorted(body)
        # 10: never overlapping, never off the source.
        spans = sorted(own)
        assert all(b[0] >= a[1] - 1e-6 for a, b in zip(spans, spans[1:], strict=False))
        assert all(0 <= s < e <= DURATIONS[media_id] + 1e-6 for s, e in own)
    # 5: the last cut holds the whole line.
    last = cuts[-1]
    assert last.media_id == OUTDOOR
    assert last.source_start_s <= 43.1 and last.source_end_s >= 45.4
    assert plan.duration_s == pytest.approx(18.0, abs=0.05)

    # 14: the phone recipe compiles and the contract verifier accepts it.
    assert _verify(contract, plan, clips)

    # 7 + 15: the finished video's receipts are honest.
    facts = plan_facts_from_rendered_montage(_finished(plan), plan.record())
    assert check_requirement(R3, facts).status == "met"
    ending = check_requirement(R5, facts)
    assert ending.status == "met", ending.reason
    assert facts.closing_speech is not None and QUOTE in facts.closing_speech.quote


async def test_the_whisper_pass_gets_the_clip_language_and_the_quote_as_its_prompt() -> None:
    calls: list = []
    strategy = _strategy(
        regound_quoted_intents(
            [_wrong_seat()],
            [IntentClip(c.media_id, "video", _analysis(c.media_id)) for c in _unified_clips()],
        )[0]
    )
    await ground_closing_quote(strategy, _snapshot(), transcribe=_transcriber(_words(), calls))
    assert calls == [(f"users/u/analysis-proxy-{OUTDOOR}.mp4", "tr", QUOTE)]


async def test_a_line_nobody_can_find_is_never_reported_as_the_ending() -> None:
    # 4 + 7: the word-timed pass misses it too.
    strategy, snapshot, quote, _moved, brief = await _plan(words=_words(with_line=False))
    assert quote["status"] == "not_found"
    notice = closing_quote_notice(quote)
    assert notice and QUOTE in notice
    # The moments are still planned; no window pretends to be the line.
    assert len(snapshot["length_plan"]["windows"]) == 6

    plan, contract, clips = _render(strategy, snapshot, brief)
    assert _verify(contract, plan, clips)
    facts = plan_facts_from_rendered_montage(_finished(plan), plan.record())
    ending = check_requirement(R5, facts)
    assert ending.status != "met"
    assert QUOTE in (ending.reason or "")


async def test_a_quote_two_takes_say_is_not_guessed() -> None:
    # 3: both takes say it; the resolver's own seat stands.
    both = {**_analysis(INDOOR)}
    both["transcript"] = f"{both['transcript']} {SPOKEN}"
    clips = [
        IntentClip(INDOOR, "video", both),
        IntentClip(OUTDOOR, "video", _analysis(OUTDOOR)),
    ]
    seated, moved = regound_quoted_intents([_wrong_seat()], clips)
    assert moved == 0
    assert seated[0].assignments[0].media_id == INDOOR


async def test_a_right_seat_and_an_unquoted_ask_are_left_alone() -> None:
    clips = [IntentClip(c.media_id, "video", _analysis(c.media_id)) for c in _unified_clips()]
    right = _wrong_seat().model_copy(
        update={
            "assignments": [_wrong_seat().assignments[0].model_copy(update={"media_id": OUTDOOR})]
        }
    )
    unquoted = _wrong_seat().model_copy(update={"attribute": "the river clip"})
    # A quoted sign or caption is not something said, even when a transcript has the words.
    sign = _wrong_seat().model_copy(update={"attribute": f"the sign that reads '{QUOTE}'"})
    seated, moved = regound_quoted_intents([right, unquoted, sign], clips)
    assert moved == 0
    assert seated == [right, unquoted, sign]


async def test_an_edit_that_asks_for_neither_keeps_its_one_window_per_take() -> None:
    # 13: no moment ask, no quote: the envelope is exactly the old selector's.
    unquoted = _wrong_seat().model_copy(update={"attribute": "the river clip"})
    strategy, snapshot, quote, _moved, _brief_ = await _plan(
        words=_words(), brief=_brief(), intents=[unquoted]
    )
    assert quote is None
    sources = [
        {"media_id": c.media_id, "duration_s": c.duration_s, "analysis": _analysis(c.media_id)}
        for c in _unified_clips()
        if c.media_id in (INDOOR, OUTDOOR)
    ]
    expected = select_best_parts(sources, target_duration_s=18.0, maximum_duration_s={})
    assert snapshot["length_plan"]["windows"] == expected.receipt()


async def test_moments_that_do_not_fit_are_said_before_approval() -> None:
    # 8 + 15: two 4 s clips cannot hold six separate moments; the reply says so.
    short_clips = {
        "clip_assignments": [
            {**row, "duration_s": 4.0, "analysis": {"best_moments": []}}
            for row in _snapshot()["clip_assignments"]
            if row["media_id"] in (INDOOR, OUTDOOR)
        ]
    }
    unquoted = _wrong_seat().model_copy(update={"attribute": "the river clip"})
    strategy = _strategy([unquoted])
    enriched = _enrich_best_parts_snapshot(strategy, short_clips, _brief(R3))
    windows = enriched["length_plan"]["windows"]
    assert len(windows) == 2
    notice = _moment_count_notice(strategy, _brief(R3), enriched["length_plan"])
    assert notice == "I could fit 2 moments, not 6."
    plan_ok = await _plan(words=_words())
    assert _moment_count_notice(plan_ok[0], plan_ok[4], plan_ok[1]["length_plan"]) is None


def test_a_taken_back_moment_ask_stops_slicing_and_counting() -> None:
    # 8 + 15, after "actually, forget the six moments" / "make it three instead".
    # A removed ask stays in the brief as superseded; only live asks may shape the cut.
    short_clips = {
        "clip_assignments": [
            {**row, "duration_s": 4.0, "analysis": {"best_moments": []}}
            for row in _snapshot()["clip_assignments"]
            if row["media_id"] in (INDOOR, OUTDOOR)
        ]
    }
    strategy = _strategy([_wrong_seat().model_copy(update={"attribute": "the river clip"})])
    asked = _brief(R3)
    remove = BriefUpdate(operation="remove", target_requirement_id="r3", expected_version=1)
    retracted = apply_updates(asked, [remove], source_turn_id="t2")
    three = BriefUpdate(
        kind="select", scope="global", description="Pick the best 3 moments from the long takes"
    )
    replaced = apply_updates(asked, [remove, three], source_turn_id="t2")
    assert [req.id for req in retracted.live()] == ["r1", "r2"]

    only_three = _brief(replaced.live()[-1])
    for clips in (_snapshot(), short_clips):
        after_retraction = _enrich_best_parts_snapshot(strategy, clips, retracted)
        assert after_retraction == _enrich_best_parts_snapshot(strategy, clips, _brief())
        after_replacement = _enrich_best_parts_snapshot(strategy, clips, replaced)
        assert after_replacement == _enrich_best_parts_snapshot(strategy, clips, only_three)
    assert (
        len(_enrich_best_parts_snapshot(strategy, _snapshot(), asked)["length_plan"]["windows"])
        == 6
    )
    assert len(after_replacement["length_plan"]["windows"]) == 2
    assert _moment_count_notice(strategy, retracted, after_retraction["length_plan"]) is None
    notice = _moment_count_notice(strategy, replaced, after_replacement["length_plan"])
    assert notice == "I could fit 2 moments, not 3."


def test_a_count_of_the_creators_clips_never_becomes_a_moment_count() -> None:
    # 1 + 4 + 6: clip counts pick sources; they never cut moments or shadow a moment ask.
    strategy = _strategy([_wrong_seat().model_copy(update={"attribute": "the river clip"})])

    def ask(rid: str, text: str) -> BriefRequirement:
        return BriefRequirement(id=rid, kind="select", scope="global", description=text)

    def plan(brief: CreativeBrief) -> dict:
        return _enrich_best_parts_snapshot(strategy, _snapshot(), brief)

    never_asked = plan(_brief())
    for text in (
        "pick best 4 of my 10 clips",
        "use 5 clips",
        "I filmed 10 clips",
        "skip 2 clips",
        "use the 4 clips",
    ):
        planned = plan(_brief(ask("r3", text)))
        assert planned == never_asked, text
        assert (
            _moment_count_notice(strategy, _brief(ask("r3", text)), planned["length_plan"]) is None
        )

    six_short = plan(_brief(ask("r4", "best 6 moments, each very short")))
    assert len(six_short["length_plan"]["windows"]) == 6
    assert (
        plan(_brief(ask("r3", "use the 4 clips"), ask("r4", "best 6 moments, each very short")))
        == six_short
    )
    assert plan(_brief(ask("r3", "best 6 moments"), ask("r4", "each very short"))) == six_short
    eight_cuts = plan(_brief(ask("r3", "8 quick cuts")))
    assert len(eight_cuts["length_plan"]["windows"]) == 8


def test_the_worker_refuses_overlapping_windows_of_one_take() -> None:
    # 10 + 14: repeats are fine, overlaps are not.
    clips = [c for c in _unified_clips() if c.media_id in (INDOOR, OUTDOOR)]
    envelope = {
        "version": 1,
        "window_eligible": True,
        "conflicts": [],
        "sources": [
            {
                "media_id": c.media_id,
                "storage_generation": c.generation,
                "gcs_path": c.proxy_path,
                "kind": "video",
                "duration_s": c.duration_s,
            }
            for c in clips
        ],
        "windows": [
            {"media_id": INDOOR, "source_start_s": 1.0, "source_end_s": 4.0, "score": 1},
            {"media_id": INDOOR, "source_start_s": 10.0, "source_end_s": 13.0, "score": 1},
            {"media_id": OUTDOOR, "source_start_s": 5.0, "source_end_s": 8.0, "score": 1},
        ],
    }
    assert len(read_length_plan(envelope, clips) or ()) == 3
    envelope["windows"][1] = {
        "media_id": INDOOR,
        "source_start_s": 3.0,
        "source_end_s": 6.0,
        "score": 1,
    }
    with pytest.raises(ValueError, match="overlap"):
        read_length_plan(envelope, clips)


@pytest.mark.parametrize(
    ("text", "ask"),
    [
        (
            "Pick the best six moments from the long takes, each very short",
            MomentAsk(count=6, short=True),
        ),
        ("use 8 quick cuts from these videos", MomentAsk(count=8, short=True)),
        ("give me the best 5 shots", MomentAsk(count=5, short=False)),
        ("uzun videolardan en iyi altı anı seç, her biri çok kısa", MomentAsk(count=6, short=True)),
        ("short moments from my walk", MomentAsk(count=None, short=True)),
        # the creator's footage is counted, not the moments (review of #1554)
        ("pick best 4 of my 10 clips", None),
        ("pick the best four of the ten clips", None),
        ("pick 4 out of 10 clips", None),
        ("pick 4 from 10 clips", None),
        ("use all 5 clips", None),
        ("best 4 of my 10 short clips", None),
        ("tüm 5 klibi kullan, bu 10 klip içinden en iyi 4 anı seç", MomentAsk(count=4)),
        ("from my 3 clips pick 6 moments", MomentAsk(count=6)),
        ("6 moments from my 2 long takes", MomentAsk(count=6)),
        ("a video of 6 moments", MomentAsk(count=6)),
        ("the 6 best moments", MomentAsk(count=6)),
        # a bare count of clips picks sources; filming or removal verbs count footage
        ("use 5 clips", None),
        ("I filmed 10 clips", None),
        ("skip 2 clips", None),
        ("use the 4 clips", None),
        ("I filmed 10 short clips", None),
        ("I shot 10 scenes", None),
        ("skip 2 moments", None),
        ("leave out the first 3 cuts", None),
        ("10 klip çektim", None),
        ("10 sahne çektim", None),
        ("2 anı atla", None),
        ("6 moments", MomentAsk(count=6)),
        ("8 quick cuts", MomentAsk(count=8, short=True)),
        # the whole video, not each moment
        ("keep it short, 18 seconds", None),
        ("make it 18 seconds", None),
        ("use 2 songs", None),
    ],
)
def test_moment_asks_are_read_in_the_creators_words(text: str, ask: MomentAsk | None) -> None:
    # 8
    req = BriefRequirement(id="r", kind="select", scope="global", description=text)
    assert moment_ask(req) == ask
