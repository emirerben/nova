"""KRI-724: "Show MEROPE the first time SHE says Merope" lands on Caro's "Merope", not Yasin's.

Merope café T1 (prod 042c405c9, 2026-10-21, thread 32A5B608; fixtures
``kri790_merope_t1_take.json`` + ``kri792_t1_phrase_cuts.json``). The take says "Merope" four
times: 38.94 s (Yasin, off camera), 39.98 s (Caro), 43.10 s and 47.06 s. The words carry no
speaker and there is no diarization (and no paid model in this lane), so before KRI-724 the
text went on Yasin's 38.94 s word and the receipt said "partial: the first time anyone says
it". Now the creator is asked which one before approval (the existing choice card), the answer
is frozen in the approval binding, and the phone Talking worker places the text on exactly
that occurrence on the cut timeline.

Replayed end to end with no model and no network: detector -> question -> the creator's tap
-> resolve_choices -> answered_brief -> BriefBinding -> the real phone Talking job
(`_run_phone_subtitled_job`) on the take's real words, with the creator's start/end lines and
both named cuts applied.

Failure modes, written before the code:

1. The sentence estimate puts Yasin's and Caro's "Merope" <1 s apart: a nearest-time match
   picks Yasin's. (Ordinal first; the placement is asserted at Caro's 39.98 s.)
2. The worker's words hold a different number of occurrences than the clip record: the
   nearest occurrence within a bounded drift, else ``pinned_occurrence_missing`` -- never the
   first match.
3. The picked word sits next to a cut ("how are you gonna" ends at 39.98 s, Caro's word starts
   there): the cut swallows it, or the pin's source time is not remapped onto the cut timeline.
4. A model-authored answer / fact picks the occurrence (spoof): ignored.
5. The media changes after the answer: the answer no longer counts, the question is asked again.
6. "You choose": no option is picked for the creator (it would pick a voice).
7. Exhausted after two asks: today's honest "partial" receipt, never "met".
8. More occurrences than options: only the first ones are offered and the question says so.
9. A waiver ("leave the text off") supersedes the ask instead of failing it later.
10. Flag off: no question, the strategy and brief pass through unchanged.
11. A voiceover edit: never asked (another slice).
12. A named cut said twice: the creator picks one, only that one is cut.
13. "Don't cut it" on a line named inside a pause-cleanup ask drops the whole ask, so the
    pauses stay in and nothing reports it. (Only the line leaves; the cleanup stays live.)
14. The render hears as many occurrences as the question offered, but not the same ones
    (the pick is cut out / misheard, another is heard only at render): the ordinal lands on
    an occurrence the creator never picked and the receipt says "met".
"""

from __future__ import annotations

import copy
import json
import os
import uuid
from pathlib import Path
from types import SimpleNamespace

import pytest

import app.pipeline.phone_subtitled_title as title_mod
from app.config import settings
from app.kria.brief import BriefRequirement, CreativeBrief
from app.kria.brief_binding import BriefBinding
from app.kria.brief_checks import (
    build_receipts,
    needs_creator_choice,
    plan_facts_from_phone_variant,
    plan_facts_from_strategy,
)
from app.kria.opening_cut_ask import OccurrencePin
from app.kria.spoken_text_ask import SpokenTextAsk, brief_spoken_texts
from app.pipeline.phone_spoken_text import find_pinned_word
from app.pipeline.render_geometry import NormalizedBox, ProtectedRegion
from app.pipeline.silence_cut import no_op_plan
from app.pipeline.speech_cleanup_analysis import SpeechCleanupAnalysisInput, analyze_speech_cleanup
from app.pipeline.speech_cut_state import remap_time
from app.pipeline.transcribe import Word
from app.services.choice_questions import (
    CONFLICT_SPEECH_OCCURRENCE,
    ChoiceCapability,
    answered_brief,
    build_choice_question,
    choice_question_text,
    collect_conflicts,
    delegated_choice,
    resolve_choices,
)
from app.services.clip_speech import SilenceDetectionResult
from app.services.creator_render_contract import (
    CONTRACT_FIELD,
    REQUIREMENT_VERSION_FIELD,
    build_render_contract,
)
from app.services.length_policy_state import (
    LENGTH_POLICIES_FIELD,
    LENGTH_POLICY_FIELD,
    stamp_length_policy,
)
from app.tasks import generative_build as gb
from tests._silent_pcm import silent_pcm
from tests.tasks.test_phone_subtitled_narrated_dispatch import _setup_subtitled

_FIXTURES = Path(__file__).resolve().parents[1] / "fixtures"
TAKE = json.loads((_FIXTURES / "kri790_merope_t1_take.json").read_text())
T1 = json.loads((_FIXTURES / "kri792_t1_phrase_cuts.json").read_text())
DURATION = float(TAKE["duration_s"])
WORDS = TAKE["words"]
MEROPE_S = [w["start_s"] for w in WORDS if w["text"].strip(",.").lower() == "merope"]
YASIN_S, CARO_S = 38.94, 39.98
THREAD = "32a5b608-6397-4608-889f-dff5ad9bec40"
MEDIA = "c0"
T1_FACE = NormalizedBox(0.30, 0.14, 0.72, 0.46)

# The approved strategy (job f88555bc), on the one clip the replay job plays.
STRATEGY = {
    **{k: v for k, v in T1["strategy"].items() if k != "rationale"},
    "selected_media_ids": [MEDIA],
}
CAP = ChoiceCapability(speech_occurrence=True)


def _t1_brief() -> CreativeBrief:
    """The bound T1 brief: r2 start/end lines, r3/r4 named cuts (the v65 `cut_lines` fact),
    r6 title, and r7 MEROPE live (prod superseded it via "simpler version")."""
    rows = []
    for row in T1["brief"]["requirements"]:
        row = copy.deepcopy(row)
        if row["id"] == "r3":
            row["facts"]["cut_lines"] = ["with bread, no,"]
        if row["id"] == "r4":
            row["facts"]["cut_lines"] = ["how are you gonna"]
        if row["id"] == "r7":
            row["status"] = "open"
        rows.append(row)
    return CreativeBrief.model_validate({**T1["brief"], "requirements": rows})


def _snapshot(media_id: str = MEDIA) -> dict:
    """The draft's media snapshot: the take's timed sentences as the binding stored them."""
    return {
        "clip_assignments": [
            {
                "media_id": media_id,
                "kind": "video",
                "duration_s": DURATION,
                "analysis": {
                    "duration_s": DURATION,
                    "understanding": {"speech": {"segments": copy.deepcopy(TAKE["segments"])}},
                },
            }
        ]
    }


def _ask(brief, snapshot, strategy=STRATEGY, cap=CAP):
    return [
        c
        for c in collect_conflicts(strategy, brief, snapshot, cap)
        if c.kind == CONFLICT_SPEECH_OCCURRENCE
    ]


def _answer(choice, option_key: str) -> list[tuple[str, dict]]:
    question = build_choice_question(choice.candidate())
    return [
        ("assistant", {"choice_question": question}),
        (
            "user",
            {
                "choice_selection": {
                    "question_id": question["question_id"],
                    "option_key": option_key,
                }
            },
        ),
    ]


def _occurrence_key(choice, ordinal: int) -> str:
    return next(o.key for o in choice.options if o.key.startswith(f"occ_{ordinal}_of_"))


# -- the real phone Talking job -----------------------------------------------------------


@pytest.fixture(autouse=True)
def _phone_draws_text(monkeypatch):
    monkeypatch.setattr(
        settings, "phone_render_verified_features", ["positionedText", "animatedText"]
    )


def _words() -> list[Word]:
    return [
        Word(text=w["text"], start_s=w["start_s"], end_s=w["end_s"], confidence=1.0) for w in WORDS
    ]


def _preflight_snapshot(storage_path: str) -> dict:
    fingerprint = "b" * 64
    analysis = analyze_speech_cleanup(
        SpeechCleanupAnalysisInput(
            source_fingerprint=fingerprint,
            local_media_path="preflight.wav",
            duration_s=DURATION,
            source_window_start_s=0.0,
            source_window_end_s=DURATION,
            mixed_gap_mode="off",
            over_budget_policy="clamp",
        ),
        transcribe_fn=lambda *_a, **_k: SimpleNamespace(
            words=_words(), language="en", low_confidence=False
        ),
        silence_detect_fn=lambda *_a, **_k: SilenceDetectionResult(spans=[], status="ok"),
        pcm_loader=silent_pcm,
    )
    return {
        "schema_version": 1,
        "analysis_id": str(uuid.uuid4()),
        "engine_version": "preflight-v1-2026-09-05",
        "detector_version": analysis.detector_version,
        "source": {
            "kind": "embedded_spine",
            "media_identity": MEDIA,
            "storage_path": storage_path,
            "generation": "1",
            "window_start_s": 0.0,
            "window_end_s": DURATION,
            "source_policy_fingerprint": fingerprint,
        },
        "analysis": analysis.to_payload(),
    }


def _run_job(monkeypatch, binding: BriefBinding, brief: CreativeBrief) -> dict:
    """The approved T1 take through the real phone Talking worker (cleanup off, the checked
    preflight words on the job), with ``binding`` pinned."""

    def faces(path, anchors, **kwargs):
        return [ProtectedRegion(0.0, DURATION, T1_FACE, "face")], {
            "attempted": len(anchors),
            "decoded": len(anchors),
        }

    monkeypatch.setattr(title_mod, "sample_face_regions", faces)
    job, snapshot, _session, phone = _setup_subtitled(
        monkeypatch, duration_s=DURATION, transcript_words=_words()
    )
    job.all_candidates.update(
        {"creator_strategy": copy.deepcopy(STRATEGY), REQUIREMENT_VERSION_FIELD: 1}
    )
    contract = build_render_contract(
        STRATEGY,
        generation_id="generation",
        brief=brief,
        media_snapshot=_snapshot(),
        clip_order=[MEDIA],
    )
    assert contract is not None
    snapshot.update(
        {
            "speech_cleanup_contract": "off_v1",
            "creator_brief_binding": binding.model_dump(mode="json"),
            "_speech_cleanup_internal": {
                "preflight_snapshot": _preflight_snapshot(phone.proxy_path)
            },
            CONTRACT_FIELD: contract.model_dump(mode="json"),
        }
    )
    snapshot.update(stamp_length_policy({}, STRATEGY, contract, mode="log_only"))
    for key in (
        "speech_cleanup_contract",
        "creator_brief_binding",
        "_speech_cleanup_internal",
        CONTRACT_FIELD,
        LENGTH_POLICY_FIELD,
        LENGTH_POLICIES_FIELD,
    ):
        job.assembly_plan[key] = copy.deepcopy(snapshot[key])
    gb._run_phone_subtitled_job(str(job.id), snapshot, job.all_candidates, ownership_epoch=3)
    assert job.status == "awaiting_device", (job.status, job.error_detail)
    return job.assembly_plan["variants"][0]


def _approve(brief: CreativeBrief, events) -> tuple[CreativeBrief, BriefBinding, object]:
    """What approval freezes: the answered brief and the server-owned answers."""
    resolution = resolve_choices(STRATEGY, brief, _snapshot(), events, CAP)
    final = answered_brief(brief, resolution.strategy)
    binding = BriefBinding.create(
        THREAD,
        final,
        media_snapshot=_snapshot(),
        choice_answers=list(resolution.strategy.get("choice_answers") or []),
    )
    return final, binding, resolution


# -- the incident, end to end -------------------------------------------------------------


def test_t1_merope_is_asked_before_approval_and_lands_on_caros_merope(monkeypatch):
    brief = _t1_brief()
    assert MEROPE_S == [YASIN_S, CARO_S, 43.10, 47.06]

    # Before approval: the one question, built from the take's timed sentences.
    [choice] = _ask(brief, _snapshot())
    assert choice.requirement_ids == ("r7",)
    labels = [o.label for o in choice.options]
    assert [label.split(" ")[0] for label in labels[:4]] == [
        "0:38",
        "0:40",
        "0:44",
        "0:47",
    ]  # sentence estimates
    assert labels[1].startswith("0:40 “") and "Colombian" in labels[1]
    assert labels[-1] == "Leave the text off"
    assert not any(o.recommended for o in choice.options)  # 6: nothing is chosen for them
    text = choice_question_text(choice.candidate())
    assert "said 4 times" in text and "can't tell who is speaking" in text
    assert text.splitlines()[0].endswith("Which one should it appear on?")

    # The creator taps Caro's (the second); approval freezes the answer.
    key = _occurrence_key(choice, 2)
    final, binding, resolution = _approve(brief, _answer(choice, key))
    assert resolution.question is None
    [answer] = binding.choice_answers
    assert (answer["kind"], answer["option"], answer["value"]) == (
        CONFLICT_SPEECH_OCCURRENCE,
        key,
        "Merope",
    )
    assert "I'm using the one at 0:40 in your take." in resolution.notices

    # The draft says the picked time, never "it may appear when someone else says it", and
    # never holds the draft for a simpler version.
    facts = plan_facts_from_strategy(resolution.strategy, clip_ids=[MEDIA])
    import dataclasses

    facts = dataclasses.replace(facts, phone_talking=True)
    [draft] = build_receipts(
        [r for r in final.live() if r.id == "r7"], facts, include_unchecked=True
    )
    assert "you picked, at 0:40 in your take" in (draft.reason or ""), draft
    assert "can't tell who's speaking" not in (draft.reason or "")
    assert not needs_creator_choice(draft)

    # After approval: the real worker, the creator's start/end lines and both cuts applied.
    variant = _run_job(monkeypatch, binding, final)
    assert variant["opening_cut"]["status"] == "applied"
    assert variant["end_cut"]["status"] == "applied"
    named = {row["quote"]: row for row in variant["named_cuts"]}
    assert named["how are you gonna"]["status"] == "applied"
    assert named["with bread, no,"]["status"] == "applied"

    # 1 + 3: Caro's 39.98 s word, remapped onto the cut timeline; never Yasin's 38.94 s.
    plan, _receipts = gb._phone_opening_cut(
        {"creator_brief_binding": binding.model_dump(mode="json")},
        None,
        None,
        duration_s=DURATION,
        off_words=WORDS,
    )
    caro_on_timeline = remap_time(CARO_S, plan.removed)
    yasin_on_timeline = remap_time(YASIN_S, plan.removed)
    assert caro_on_timeline is not None and yasin_on_timeline is not None
    [row] = [r for r in variant["text_elements"] if r["text"] == "MEROPE"]
    assert row["start_s"] == pytest.approx(caro_on_timeline, abs=0.01)
    assert abs(row["start_s"] - yasin_on_timeline) > 0.3
    [placement] = variant["spoken_text_placement"]["rows"]
    assert placement["status"] == "placed" and placement["pinned"] is True
    assert placement["pin"]["ordinal"] == 2 and placement["pin"]["of"] == 4

    # The finished video's receipt: met (the creator said whose word it is).
    [receipt] = build_receipts(
        [r for r in final.live() if r.id == "r7"],
        plan_facts_from_phone_variant(variant),
        include_unchecked=True,
    )
    assert receipt.status == "met", receipt

    target = os.environ.get("KRI724_REPORT")
    if target:
        Path(target).parent.mkdir(parents=True, exist_ok=True)
        Path(target).write_text(
            json.dumps(
                {
                    "ticket": "KRI-724",
                    "request": TAKE["prompt"],
                    "question": choice_question_text(choice.candidate()),
                    "answer": answer,
                    "binding_digest": binding.digest,
                    "merope_words_source_s": MEROPE_S,
                    "placed": row,
                    "placement": placement,
                    "named_cuts": variant["named_cuts"],
                    "opening_cut": variant["opening_cut"],
                    "end_cut": variant["end_cut"],
                    "receipt": receipt.model_dump(mode="json"),
                },
                indent=2,
            )
        )


# -- negative controls --------------------------------------------------------------------


def test_flag_off_asks_nothing_and_passes_the_plan_through():
    brief = _t1_brief()
    off = ChoiceCapability(speech_occurrence=False)
    assert _ask(brief, _snapshot(), cap=off) == []
    resolution = resolve_choices(STRATEGY, brief, _snapshot(), [], off)
    assert resolution.question is None or resolution.question.kind != CONFLICT_SPEECH_OCCURRENCE
    assert "choice_answers" not in resolution.strategy
    assert answered_brief(brief, resolution.strategy) is brief


def test_a_model_written_answer_never_picks_the_occurrence():
    brief = _t1_brief()
    [choice] = _ask(brief, _snapshot())
    spoof = {
        **STRATEGY,
        "choice_answers": [
            {
                "conflict": choice.conflict_id,
                "kind": CONFLICT_SPEECH_OCCURRENCE,
                "option": _occurrence_key(choice, 2),
                "input_digest": choice.input_digest,
                "requirement_ids": ["r7"],
                "value": "Merope",
            }
        ],
    }
    resolution = resolve_choices(spoof, brief, _snapshot(), [], CAP)
    # Dropped (answers are server-owned) and the creator is asked.
    assert resolution.question is not None
    assert resolution.question.kind == CONFLICT_SPEECH_OCCURRENCE
    assert not resolution.answers
    # A pin written into the brief's facts is not an answer either.
    spoofed = brief.model_copy(
        update={
            "requirements": [
                r.model_copy(update={"facts": {**r.facts, "spoken_pin": {"ordinal": 2, "of": 4}}})
                if r.id == "r7"
                else r
                for r in brief.requirements
            ]
        }
    )
    [ask] = [a for a in brief_spoken_texts(spoofed, []) if a.requirement_id == "r7"]
    assert ask.pin is None


def test_new_media_reopens_the_question():
    brief = _t1_brief()
    [choice] = _ask(brief, _snapshot())
    events = _answer(choice, _occurrence_key(choice, 2))
    resolution = resolve_choices(STRATEGY, brief, _snapshot("c1"), events, CAP)
    assert resolution.question is not None
    assert resolution.question.input_digest != choice.input_digest
    assert not resolution.answers


def test_you_choose_never_picks_a_voice_and_two_asks_stay_honest():
    brief = _t1_brief()
    [choice] = _ask(brief, _snapshot())
    question = build_choice_question(choice.candidate())
    assert delegated_choice(question, "you choose") is None
    # Asked twice, never answered: the plan goes through with the KRI-791 caveat, not "met".
    events = [("assistant", {"choice_question": question})] * 2
    resolution = resolve_choices(STRATEGY, brief, _snapshot(), events, CAP)
    assert resolution.question is None
    assert [c.kind for c in resolution.exhausted] == [CONFLICT_SPEECH_OCCURRENCE]
    [ask] = brief_spoken_texts(brief, resolution.strategy.get("choice_answers"))
    assert ask.pin is None and ask.speaker == "she"


def test_leave_the_text_off_is_an_agreed_waiver():
    brief = _t1_brief()
    [choice] = _ask(brief, _snapshot())
    final, binding, resolution = _approve(brief, _answer(choice, "waive"))
    assert {r.id: r.status for r in final.requirements}["r7"] == "superseded"
    assert brief_spoken_texts(final, binding.choice_answers) == ()
    assert "I'm skipping that one, as you chose." in resolution.notices


def test_a_voiceover_edit_is_not_asked():
    voiceover = {**STRATEGY, "audio_strategy": "voiceover"}
    assert _ask(_t1_brief(), _snapshot(), strategy=voiceover) == []


def test_the_creators_own_word_is_not_asked():
    brief = CreativeBrief(
        version=1,
        requirements=[
            BriefRequirement(
                id="r9",
                kind="text",
                scope="global",
                literal="MEROPE",
                description="Show MEROPE the first time I say Merope",
            )
        ],
    )
    assert _ask(brief, _snapshot()) == []


# -- 2: the worker's words count differently ---------------------------------------------

_PIN = OccurrencePin(ordinal=2, of=4, approx_s=40.04)  # the sentence estimate of Caro's word
_ASK = SpokenTextAsk("r7", "MEROPE", ("Merope",), speaker="she", pin=_PIN)


def _without(start_s: float) -> list[dict]:
    return [w for w in WORDS if w["start_s"] != start_s]


def test_a_missing_later_merope_still_finds_caros_by_time():
    (hit, missed) = find_pinned_word(_without(43.10), _ASK)
    assert missed is None and hit[1] == pytest.approx(CARO_S)


def test_a_missing_picked_merope_is_never_swapped_for_yasins():
    (hit, missed) = find_pinned_word(_without(CARO_S), _ASK)
    assert hit is None and missed == "pinned_occurrence_missing"


def test_a_picked_moment_cut_out_of_the_take_says_so():
    words = _without(CARO_S)

    def cut_out(source_s: float):
        return None if 39.5 <= source_s <= 40.6 else source_s

    (hit, missed) = find_pinned_word(words, _ASK, to_timeline=cut_out)
    assert hit is None and missed == "pinned_occurrence_cut"


# -- 12: a named cut said twice ----------------------------------------------------------


def test_a_line_said_twice_is_cut_only_where_the_creator_picked():
    brief = CreativeBrief(
        version=1,
        requirements=[
            BriefRequirement(
                id="r8",
                kind="audio",
                scope="global",
                description="Cut where she says 'thank you Karo'",
                facts={"speech_cleanup": True, "cut_lines": ["thank you Karo"]},
            )
        ],
    )
    [choice] = _ask(brief, _snapshot())
    keys = [o.key for o in choice.options]
    assert keys[-2:] == ["occ_all_of_2", "waive"] and len(keys) == 4
    final, binding, _resolution = _approve(brief, _answer(choice, _occurrence_key(choice, 2)))
    _plan, receipts = gb._phone_opening_cut(
        {"creator_brief_binding": binding.model_dump(mode="json")},
        no_op_plan(DURATION),
        WORDS,
        duration_s=DURATION,
    )
    [cut] = receipts["named_cuts"]
    assert cut["status"] == "applied" and cut["pin"]["ordinal"] == 2
    # Only the second "thank you Karo" (65.9 s), never the first (64.6 s).
    assert cut["start_s"] > 65.0
    [receipt] = build_receipts(
        final.live(),
        plan_facts_from_phone_variant(
            {
                "resolved_archetype": "subtitled",
                "render_destination": "device",
                "named_cuts": receipts["named_cuts"],
            }
        ),
        include_unchecked=True,
    )
    assert receipt.status == "met", receipt

    # "Every time" cuts both.
    _final, every, _ = _approve(brief, _answer(choice, "occ_all_of_2"))
    _plan, receipts = gb._phone_opening_cut(
        {"creator_brief_binding": every.model_dump(mode="json")},
        no_op_plan(DURATION),
        WORDS,
        duration_s=DURATION,
    )
    [cut] = receipts["named_cuts"]
    assert cut["status"] == "applied" and len(cut["spans"]) == 2


# -- 13: a waived line inside a cleanup ask keeps the cleanup -----------------------------


def test_dont_cut_it_keeps_the_pause_cleanup_of_the_same_ask():
    from app.kria.opening_cut_ask import brief_named_cuts
    from app.services.choice_questions import _asks_speech_cleanup

    brief = CreativeBrief(
        version=1,
        requirements=[
            BriefRequirement(
                id="r1",
                kind="timing",
                scope="global",
                description="Cut the long pauses and the part where I say 'thank you Karo'",
                facts={"cut_lines": ["thank you Karo"]},
            )
        ],
    )
    [choice] = _ask(brief, _snapshot())
    final, binding, _resolution = _approve(brief, _answer(choice, "waive"))
    [r1] = final.requirements
    assert r1.status != "superseded" and r1.facts["cut_lines"] == []
    assert _asks_speech_cleanup(final)
    assert brief_named_cuts(final, binding.choice_answers) == ()


def test_dont_cut_it_on_a_line_only_ask_still_supersedes_it():
    brief = CreativeBrief(
        version=1,
        requirements=[
            BriefRequirement(
                id="r8",
                kind="audio",
                scope="global",
                description="Cut where she says 'thank you Karo'",
                facts={"speech_cleanup": True, "cut_lines": ["thank you Karo"]},
            )
        ],
    )
    [choice] = _ask(brief, _snapshot())
    final, _binding, _resolution = _approve(brief, _answer(choice, "waive"))
    assert final.requirements[0].status == "superseded"


# -- 14: an equal count is not the same set of occurrences --------------------------------


def _merope(start_s: float) -> dict:
    return {"text": "Merope", "start_s": start_s, "end_s": start_s + 0.45}


def _swapped() -> list[dict]:
    """Caro's word misheard, one more "Merope" heard at 52 s: still four occurrences."""
    return sorted([*_without(CARO_S), _merope(52.0)], key=lambda w: w["start_s"])


def test_an_equal_count_of_other_meropes_is_never_placed_as_caros():
    (hit, missed) = find_pinned_word(_swapped(), _ASK)
    assert hit is None and missed == "pinned_occurrence_missing"


def test_a_cut_out_pick_never_falls_back_to_the_ordinal():
    def cut_out(source_s: float):
        return None if 39.5 <= source_s <= 40.6 else source_s

    (hit, missed) = find_pinned_word(_swapped(), _ASK, to_timeline=cut_out)
    assert hit is None and missed == "pinned_occurrence_cut"


def test_the_ordinal_still_beats_a_sentence_estimate_nearer_yasin():
    # Control for 14: same set, estimate drifted toward Yasin's word -> still Caro's.
    pin = OccurrencePin(ordinal=2, of=4, approx_s=39.1)
    ask = SpokenTextAsk("r7", "MEROPE", ("Merope",), speaker="she", pin=pin)
    (hit, missed) = find_pinned_word(WORDS, ask)
    assert missed is None and hit[1] == pytest.approx(CARO_S)


def test_a_named_cut_with_an_equal_count_of_other_takes_cuts_nothing():
    from app.pipeline.phone_opening_cut import apply_named_line_cuts

    words = [
        Word(text=t, start_s=s, end_s=s + 0.3, confidence=1.0)
        for t, s in [("thank", 2.0), ("you", 2.3), ("Karo", 2.6), ("ok", 6.0)]
        + [("thank", 20.0), ("you", 20.3), ("Karo", 20.6), ("bye", 25.0)]
    ]
    # The creator picked the second of two, at 12 s; the render hears two others (2 s, 20 s).
    pin = OccurrencePin(ordinal=2, of=2, approx_s=12.0)
    plan, receipts = apply_named_line_cuts(
        no_op_plan(30.0), words, [("thank you Karo", ("r8",), None, pin)], duration_s=30.0
    )
    [receipt] = receipts
    assert receipt["status"] != "applied" and receipt["reason"] == "pinned_occurrence_missing"
    assert plan.keep_segments == no_op_plan(30.0).keep_segments


# -- 15: pause cleanup on: the sentence estimate lands in a removed pause -----------------
# Failure modes: (a) the estimate sits in a pause cleanup removed while the word itself plays
# -> must still be placed; (b) a Gemini segment start in leading silence, 2 of 2 -> the
# ordinal word right where the cut closes; (c) the picked word itself cut out and the render
# hears the same count -> never another occurrence; (d) a word right after the cut that the
# ordinal does not name -> never placed as the pick.


def _cleaned(words: list[tuple[str, float]], removed: list[tuple[float, float]]):
    from app.pipeline.silence_cut import Removal

    removals = [Removal(start_s=a, end_s=b, reason="pause") for a, b in removed]
    timeline = []
    for text, start in words:
        mapped = remap_time(start, removals)
        if mapped is not None:
            timeline.append({"text": text, "start_s": mapped, "end_s": mapped + 0.4})
    return timeline, (lambda t: remap_time(t, removals))


def _pinned(ordinal: int, of: int, approx_s: float) -> SpokenTextAsk:
    pin = OccurrencePin(ordinal=ordinal, of=of, approx_s=approx_s)
    return SpokenTextAsk("r9", "MEROPE", ("Merope",), pin=pin)


def test_an_estimate_in_a_removed_pause_still_places_the_kept_word():
    # "Hi Merope is here, see you" 10-14 s; real "Merope" at 12.0 after a 10.3-12.0 pause.
    timeline, to_timeline = _cleaned(
        [("Hi", 10.0), ("Merope", 12.0), ("is", 12.5), ("here", 12.8)], [(10.3, 12.0)]
    )
    (hit, missed) = find_pinned_word(timeline, _pinned(1, 1, 10.46), to_timeline=to_timeline)
    assert missed is None and hit[1] == pytest.approx(10.3)


def test_a_segment_start_in_leading_silence_keeps_the_ordinal_pick():
    timeline, to_timeline = _cleaned(
        [("Merope", 1.0), ("Merope", 11.2), ("is", 11.7), ("great", 12.0)], [(1.7, 11.0)]
    )
    (hit, missed) = find_pinned_word(timeline, _pinned(2, 2, 10.0), to_timeline=to_timeline)
    assert missed is None and hit[1] == pytest.approx(1.9)


def test_a_cut_out_pick_with_cleanup_on_is_never_swapped():
    # The picked word (11.2) itself is cut; the render still hears two (another at 30 s).
    timeline, to_timeline = _cleaned(
        [("Merope", 1.0), ("Merope", 11.2), ("Merope", 30.0)], [(9.0, 12.0)]
    )
    (hit, missed) = find_pinned_word(timeline, _pinned(2, 2, 10.0), to_timeline=to_timeline)
    assert hit is None and missed == "pinned_occurrence_cut"


def test_a_word_where_the_cut_closes_that_the_ordinal_does_not_name_is_never_placed():
    timeline, to_timeline = _cleaned(
        [("Merope", 1.0), ("Merope", 12.1), ("Merope", 30.0)], [(9.0, 12.0)]
    )
    (hit, missed) = find_pinned_word(timeline, _pinned(1, 3, 10.0), to_timeline=to_timeline)
    assert hit is None and missed == "pinned_occurrence_cut"
