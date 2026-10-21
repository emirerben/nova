"""KRI-791: "Show MEROPE in big letters the first time she says Merope" reaches the draft.

Café kit T1 (prod thread 32A5B608, plan item b5477ee9, jobs 0701fcc9 + f88555bc, 2026-10-21),
on main with the KRI-707 fix. The brief held the ask as r7 = ``text`` / ``global`` /
``literal: "MEROPE"`` with ``facts: {}``: no ``facts.triggers``, because the brief prompt only
asked for triggers on a photo, video, sticker or sound. `spoken_text_ask` needs a trigger, and
the wording check did not know "the first time she says" as a spoken cue, so the draft said
"That exact text isn't in this draft", the draft was held behind "a simpler version?", and
"simpler version" removed the ask. Two people say "Merope" in the take: the off-camera
creator first (segment 36.90-40.04 s), then Caro (40.04 s); speakers aren't diarized.

Ways this can go wrong (written before the code):

1. No trigger fact: the worker places nothing and the draft says "isn't in this draft".
2. "the first time ..." is not read as a spoken cue, so the ask never reaches the
   spoken-text branch of the receipt.
3. A third-person speaker ("she", "he", "they") is not read as a spoken cue either.
4. The fallback invents a trigger for a plain text ask ("Show 'SALE' in big letters", "show
   MEROPE at the start"), placing it on a random word and unpinning a text the render owes.
5. The fallback grabs too much ("Merope, placed so there is no text on her face") or a
   described word ("the café's name"), which the take never says verbatim.
6. A model-written trigger is overridden or merged with a guess from the wording.
7. Two voices say the word: the text lands on the first "Merope" (the creator's), and the
   receipt claims "met" for "the first time SHE says it" -- or the draft hides that it
   can't tell who is speaking.
8. The honest speaker note holds the draft ("a simpler version?") or blocks the render.
9. The render contract still pins "MEROPE" on a Talking edit, so the phone pin refuses the
   render when the take doesn't say the word -- also when no word could be read at all.
10. A spoken-cue ask with no readable word says "a Talking video can't show text at a word
    you say", which is false on the phone; it must say which part is missing.
11. A Turkish chat gets an English reason; a Turkish ask ("“Merope” dediğinde ...") gets no
    trigger.
12. The kill switch off changes the plain "can't".
13. The brief prompt still teaches triggers only for photos, videos, stickers and sounds.
14. (Review) Widening the spoken-cue recogniser every pop-in, whole-line and cut ask shares
    changes those asks: "play the line in full when she says it" stops being a whole-line
    ask, "a ding every time someone says cheers" becomes a held pop-in, a shot label "the
    shot when they get to the summit" loses its own question.
15. (Review) The guess runs past the word ("... she says Merope in big letters") and the text
    is never placed; or the shown text itself is read as the cue ("WHEN SHE SAYS HI").

Replayed from the real T1 brief, approved strategy and speech segments
(``~/Downloads/kria-ecuador-test/T1_caro_full_take_talking/runs/2026-10-21_live/``), no model
calls. The run never rendered, so no word timings past 30 s were stored: each speech
segment's words are spread evenly over the segment's own timing.
"""

from __future__ import annotations

import copy
import dataclasses
import uuid
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
    spoken_text_literals,
)
from app.kria.reply_language import bind_reply_language, release_reply_language
from app.kria.spoken_text_ask import requirement_spoken_text
from app.pipeline.render_geometry import NormalizedBox, ProtectedRegion
from app.pipeline.speech_cleanup_analysis import SpeechCleanupAnalysisInput, analyze_speech_cleanup
from app.pipeline.transcribe import Word
from app.services.clip_speech import SilenceDetectionResult
from app.services.creator_render_contract import (
    CONTRACT_FIELD,
    REQUIREMENT_VERSION_FIELD,
    build_render_contract,
)
from app.services.device_render import device_record, device_status
from app.services.length_policy_state import (
    LENGTH_POLICIES_FIELD,
    LENGTH_POLICY_FIELD,
    stamp_length_policy,
)
from app.tasks import generative_build as gb
from tests.tasks.test_phone_subtitled_narrated_dispatch import _setup_subtitled

DURATION = 68.37
GENERATION = "generation"  # `_setup_subtitled`'s `creator_generation_id`

# The take's speech segments (brief binding `media_snapshot` ... `understanding.speech`).
T1_SEGMENTS = (
    ("Yes we will show.", 0.0, 1.46),
    ("Merhaba arkadaşlar, hello everyone.", 2.72, 4.44),
    ("We are with Karo today.", 4.86, 6.54),
    ("Hi, I'm Carolina, I'm from Ecuador.", 12.04, 13.58),
    ("This is the Oblea de la Casa.", 14.54, 16.88),
    ("It's a traditional Colombian dessert.", 17.2, 20.62),
    ("leche condensada and whipped cream.", 30.18, 34.46),
    ("That's it.", 35.74, 36.28),
    # The off-camera creator: the first "Merope".
    ("And where we are right now is Merope, how are you going to...", 36.9, 40.04),
    # Caro, on camera.
    ("Merope, it's a Colombian coffee and restaurant and Merope means god of honey.", 40.04, 45.48),
    ("Yeah, we definitely recommend coming to Merope, right?", 45.62, 47.84),
    ("Yeah, come back please, I'm gonna be here all week.", 47.84, 50.3),
    ("Thank you Karo, bye bye.", 65.92, 67.36),
)
# A plausible close-selfie face band (the run never sampled one).
T1_FACE = NormalizedBox(0.30, 0.14, 0.72, 0.46)


def _segment_words() -> list[tuple[str, float, float]]:
    out: list[tuple[str, float, float]] = []
    for text, start, end in T1_SEGMENTS:
        tokens = text.split()
        step = (end - start) / len(tokens)
        out.extend(
            (token, round(start + i * step, 3), round(start + (i + 1) * step, 3))
            for i, token in enumerate(tokens)
        )
    return out


T1_WORDS = _segment_words()
FIRST_MEROPE_S = next(start for text, start, _ in T1_WORDS if text.startswith("Merope"))
CARO_MEROPE_S = 40.04


def _req(rid: str, kind: str, description: str | None, **extra) -> BriefRequirement:
    return BriefRequirement(
        id=rid,
        kind=kind,
        scope=extra.pop("scope", "global"),
        description=description,
        source_turn_id="d02b5630-d3fd-492c-a749-0a7383cf91a1",
        **extra,
    )


# r7 exactly as prod stored it (job 0701fcc9 brief, before "simpler version" superseded it).
R7 = _req(
    "r7",
    "text",
    "Show MEROPE in big letters the first time she says Merope, placed so there is no text "
    "on her face or on the plate",
    literal="MEROPE",
)
# The rest of the T1 brief the worker reads. r2 (start/end lines read as an order ask) and
# r3/r4 (named cuts) belong to KRI-788/789/790/792 and are left out of this replay.
T1_REQUIREMENTS = [
    _req("r1", "timing", "About 35 seconds", facts={"duration_s": 35}),
    _req(
        "r5",
        "style",
        "Captions for both voices, ensuring correct spellings: Oblea de la Casa, dulce de "
        "leche, leche condensada, Merope",
        facts={"captions": "on"},
    ),
    _req(
        "r6",
        "text",
        "Title on screen, placed so there is no text on her face or on the plate",
        scope="title",
        literal="Colombian dessert, Ecuadorian hands",
    ),
    R7,
]
# The approved strategy (job 0701fcc9 `creator_strategy`), trimmed to what is read.
T1_STRATEGY = {
    "edit_format": "subtitled",
    "audio_strategy": "original_audio",
    "render_program": "native",
    "caption_style": "auto",
    "opening_title": "Colombian dessert, Ecuadorian hands",
    "pacing": "balanced",
    "direction": "native",
    "target_duration_s": 35,
    "selected_media_ids": ["c0"],
}

ANY_VOICE_PLAN = (
    "I'll show it the first time that word is said in your take; I can't tell who's "
    "speaking, so it may appear when someone else says it"
)
NO_WORD = "I couldn't tell which spoken word to wait for, so tell me the exact word to show it on"


@pytest.fixture(autouse=True)
def _phone_draws_text(monkeypatch):
    """The phone renders positioned, animated text (the Talking title's own features)."""
    monkeypatch.setattr(
        settings, "phone_render_verified_features", ["positionedText", "animatedText"]
    )


def _brief(*requirements: BriefRequirement) -> CreativeBrief:
    return CreativeBrief(version=1, requirements=list(requirements or T1_REQUIREMENTS))


def _words() -> list[Word]:
    return [Word(text=t, start_s=s, end_s=e, confidence=1.0) for t, s, e in T1_WORDS]


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
    )
    return {
        "schema_version": 1,
        "analysis_id": str(uuid.uuid4()),
        "engine_version": "preflight-v1-2026-09-05",
        "detector_version": analysis.detector_version,
        "source": {
            "kind": "embedded_spine",
            "media_identity": "c0",
            "storage_path": storage_path,
            "generation": "1",
            "window_start_s": 0.0,
            "window_end_s": DURATION,
            "source_policy_fingerprint": fingerprint,
        },
        "analysis": analysis.to_payload(),
    }


def _faces(monkeypatch) -> None:
    def sample(path, anchors, **kwargs):
        return [ProtectedRegion(0.0, DURATION, T1_FACE, "face")], {
            "attempted": len(anchors),
            "decoded": len(anchors),
        }

    monkeypatch.setattr(title_mod, "sample_face_regions", sample)


def _t1_job(monkeypatch, *, brief: CreativeBrief | None = None):
    """The T1 take approved on main: cleanup off, the checked preflight words on the job, the
    hook title, the brief pinned and its contract stamped (length logged, not enforced)."""
    job, snapshot, _session, binding = _setup_subtitled(
        monkeypatch, duration_s=DURATION, transcript_words=_words()
    )
    brief = brief or _brief()
    job.all_candidates.update(
        {"creator_strategy": copy.deepcopy(T1_STRATEGY), REQUIREMENT_VERSION_FIELD: 1}
    )
    contract = build_render_contract(T1_STRATEGY, generation_id=GENERATION, brief=brief)
    assert contract is not None
    snapshot.update(
        {
            "speech_cleanup_contract": "off_v1",
            "creator_brief_binding": BriefBinding.create(uuid.uuid4(), brief).model_dump(
                mode="json"
            ),
            "_speech_cleanup_internal": {
                "preflight_snapshot": _preflight_snapshot(binding.proxy_path)
            },
            CONTRACT_FIELD: contract.model_dump(mode="json"),
        }
    )
    snapshot.update(stamp_length_policy({}, T1_STRATEGY, contract, mode="log_only"))
    for key in (
        "speech_cleanup_contract",
        "creator_brief_binding",
        "_speech_cleanup_internal",
        CONTRACT_FIELD,
        LENGTH_POLICY_FIELD,
        LENGTH_POLICIES_FIELD,
    ):
        job.assembly_plan[key] = copy.deepcopy(snapshot[key])
    return job, snapshot


def _run(job, snapshot) -> dict:
    gb._run_phone_subtitled_job(str(job.id), snapshot, job.all_candidates, ownership_epoch=3)
    assert job.status == "awaiting_device", (job.status, job.error_detail)
    return job.assembly_plan["variants"][0]


def _draft_receipt(req: BriefRequirement, *, phone_talking: bool = True):
    facts = dataclasses.replace(plan_facts_from_strategy(T1_STRATEGY), phone_talking=phone_talking)
    [receipt] = build_receipts([req], facts, include_unchecked=True)
    return receipt


# -- the incident, end to end ----------------------------------------------------------


def test_t1_merope_lands_on_the_first_spoken_merope_with_an_honest_speaker_receipt(monkeypatch):
    _faces(monkeypatch)
    job, snapshot = _t1_job(monkeypatch)

    variant = _run(job, snapshot)

    cut = variant.get("opening_cut") or {}
    offset = float(cut.get("start_s") or 0.0) if cut.get("status") == "applied" else 0.0
    # 1-3, 5: the trigger is "Merope" (not "Merope, placed so ..."), heard where it is first
    # said -- the off-camera voice, before Caro's at 40.04 s (7: no speaker filter exists).
    [row] = [r for r in variant["text_elements"] if r["text"] == "MEROPE"]
    assert row["start_s"] == pytest.approx(FIRST_MEROPE_S - offset, abs=0.01)
    assert FIRST_MEROPE_S < CARO_MEROPE_S
    [placement] = variant["spoken_text_placement"]["rows"]
    assert (placement["requirement_id"], placement["status"]) == ("r7", "placed")
    assert placement["trigger"] == "Merope"
    assert placement["speaker"] == "she"
    assert placement["face_collision"] is False
    # The phone draws it at the word.
    recipe = device_status(job, "subtitled").request.recipe
    [layer] = [
        layer
        for layer in recipe.text_layers
        if layer.id.startswith("text-") and "".join(run.text for run in layer.runs) == "MEROPE"
    ]
    assert layer.start == pytest.approx(row["start_s"], abs=0.01)
    # 9: the contract pins only the title; the pin accepted the recipe.
    assert device_record(job, "subtitled").get("contract_digest")
    assert "MEROPE" not in [t["text"] for t in job.assembly_plan[CONTRACT_FIELD]["exact_texts"]]
    # 7 + 8: the finished video says it showed the first "Merope" anyone said, never "met"
    # for "the first time SHE says it", and that note never asks the creator anything.
    [receipt] = build_receipts([R7], plan_facts_from_phone_variant(variant), include_unchecked=True)
    assert receipt.status == "partial"
    assert "can't tell who's speaking" in (receipt.reason or "")
    assert not needs_creator_choice(receipt)


def test_the_t1_draft_promises_merope_honestly_and_never_holds_for_a_simpler_version():
    receipt = _draft_receipt(R7)

    # 1, 7, 8: a plan the render keeps, with the speaker caveat, never an omission.
    assert receipt.verification == "checked"
    assert (receipt.status, receipt.reason) == ("partial", ANY_VOICE_PLAN)
    assert not needs_creator_choice(receipt)


def test_the_t1_draft_reason_is_turkish_in_a_turkish_chat():
    token = bind_reply_language("tr")
    try:
        receipt = _draft_receipt(R7)
    finally:
        release_reply_language(token)
    # 11
    assert receipt.reason and "speaking" not in receipt.reason and "kelime" in receipt.reason
    assert not needs_creator_choice(receipt)


# -- what counts as a text on a spoken word --------------------------------------------


@pytest.mark.parametrize(
    ("description", "literal", "triggers", "speaker"),
    [
        # 2, 3, 5: the T1 wording; the trigger stops at the comma.
        (R7.description, "MEROPE", ("Merope",), "she"),
        ("Show MEROPE when he says Merope", "MEROPE", ("Merope",), "he"),
        ("Show “MEROPE” the first time they say “Merope”", "MEROPE", ("Merope",), "they"),
        # The creator's own voice, or anyone's: no speaker caveat.
        ("Show ATAKAN the first time I say Atakan", "ATAKAN", ("Atakan",), None),
        ("Show MEROPE the first time anyone says Merope", "MEROPE", ("Merope",), None),
        # 11: the Turkish shape (the word, quoted, before the verb).
        ("“Merope” dediğinde ekrana büyük “MEROPE” yazısı gelsin", "MEROPE", ("Merope",), None),
        ("“yirmi dakika” dediğimde ekrana “20 dk” yazısı", "20 dk", ("yirmi dakika",), None),
        # 15: cut to the shown text when the guess starts with it.
        (
            "Show MEROPE the first time she says Merope in big letters",
            "MEROPE",
            ("Merope",),
            "she",
        ),
        ("When she says Merope show MEROPE on screen", "MEROPE", ("Merope",), "she"),
        # A voiceover is the creator's own voice, whoever's "her".
        ("Show MEROPE when her voiceover says Merope", "MEROPE", ("Merope",), None),
    ],
)
def test_the_spoken_word_is_read_off_the_ask_when_its_facts_leave_it_out(
    description, literal, triggers, speaker
):
    req = _req("r9", "text", description, literal=literal)

    ask = requirement_spoken_text(req)

    assert ask is not None
    assert (ask.triggers, ask.speaker) == (triggers, speaker)


@pytest.mark.parametrize(
    "description",
    [
        # 4: plain text asks name no spoken moment.
        "Show SALE in big letters",
        "Show MEROPE in big letters at the start",
        "Big MEROPE text, no text on her face",
        # 5: a described word is not a word to listen for.
        "Show MEROPE the first time she says the café's name",
    ],
)
def test_no_word_is_guessed_for_a_plain_or_described_text(description):
    assert requirement_spoken_text(_req("r9", "text", description, literal="MEROPE")) is None


def test_the_shown_text_is_never_read_as_the_cue():
    # 15
    req = _req("r9", "text", "Big text on screen", literal="WHEN SHE SAYS HI")
    assert requirement_spoken_text(req) is None


def test_asks_outside_on_screen_text_read_exactly_as_before():
    """14: the pop-in, whole-line and shot-label readers never see the new cue forms."""
    from app.kria.brief_checks import _wants_beats, whole_line_quotes

    whole_line = _req(
        "r3",
        "audio",
        "Let the line “Hi, I'm Karolina” play in full when she says it",
        facts={"keep_whole_take": True, "triggers": ["Hi, I'm Karolina"]},
    )
    assert whole_line_quotes(whole_line) == ["Hi, I'm Karolina"]
    ding = _req("r4", "audio", "Add a ding sound every time someone says cheers")
    assert _wants_beats(ding) is False
    label = _req(
        "r8",
        "text",
        "the shot the first time she says hi",
        scope="per_clip",
        literal="HELLO",
    )
    summit = _req("r9", "text", "Show SUMMIT when they get to the summit", literal="SUMMIT")
    assert spoken_text_literals(_brief(label, summit)) == ()
    assert requirement_spoken_text(summit) is None


def test_a_model_written_trigger_and_speaker_win_over_the_wording():
    # 6
    req = R7.model_copy(update={"facts": {"triggers": ["Merope café"], "speaker": "Caro"}})

    ask = requirement_spoken_text(req)

    assert ask is not None
    assert (ask.triggers, ask.speaker) == (("Merope café",), "Caro")


def test_a_first_person_speaker_fact_is_no_caveat():
    req = R7.model_copy(update={"facts": {"triggers": ["Merope"], "speaker": "I"}})

    ask = requirement_spoken_text(req)

    assert ask is not None and ask.speaker is None
    assert _draft_receipt(req).reason == (
        "I'll show it the moment you say that word in your take; if I don't hear the word, it "
        "won't appear"
    )


def test_a_title_is_never_a_spoken_word_ask():
    title = T1_REQUIREMENTS[2]
    assert requirement_spoken_text(title) is None
    assert requirement_spoken_text(title.model_copy(update={"facts": {"triggers": ["x"]}})) is None


def test_a_voiceover_edit_still_keeps_the_ask_out_of_the_clip_picker():
    """KRI-692 reads the same cue: the T1 wording is a text on a spoken word there too."""
    assert spoken_text_literals(_brief(R7)) == ("MEROPE",)


# -- when it still can't be placed, the receipt says why -------------------------------


def test_a_spoken_cue_with_no_readable_word_says_which_part_is_missing_and_is_not_pinned():
    vague = _req(
        "r7", "text", "Show MEROPE the first time she says the café's name", literal="MEROPE"
    )

    receipt = _draft_receipt(vague)

    # 10: honest, not "can't show text at a word you say" (the phone can), and not a hold.
    assert (receipt.status, receipt.reason) == ("not_possible", NO_WORD)
    assert not needs_creator_choice(receipt)
    # 9: the render never refuses over a text the draft already said it can't place.
    contract = build_render_contract(
        T1_STRATEGY, generation_id=GENERATION, brief=_brief(*T1_REQUIREMENTS[:3], vague)
    )
    assert contract is not None
    assert "MEROPE" not in [t.text for t in contract.exact_texts]


def test_a_plain_text_ask_keeps_its_answer():
    plain = _req("r7", "text", "Show SALE in big letters", literal="SALE")

    receipt = _draft_receipt(plain)

    assert (receipt.status, receipt.reason) == ("partial", "That exact text isn't in this draft.")


def test_kill_switch_off_keeps_the_plain_cant(monkeypatch):
    # 12
    monkeypatch.setattr(settings, "phone_subtitled_spoken_text_enabled", False)
    assert (_draft_receipt(R7).status, _draft_receipt(R7).reason) == (
        "not_possible",
        "A Talking video can't show text at a word you say yet",
    )


def test_a_cloud_talking_edit_keeps_the_plain_cant():
    assert _draft_receipt(R7, phone_talking=False).reason == (
        "A Talking video can't show text at a word you say yet"
    )


# -- the brief prompt ------------------------------------------------------------------


def test_the_brief_prompt_asks_for_triggers_on_text_timed_to_a_spoken_word():
    # 13: the model is told to record the word (and who says it) for on-screen text too.
    from app.agents.main_creator import _BRIEF_PROMPT_SECTION

    section = " ".join(_BRIEF_PROMPT_SECTION.split())
    assert "the first time she says Merope" in section
    assert '{"triggers": ["Merope"], "speaker": "she"}' in section
