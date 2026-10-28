"""KRI-1015 replay: "keep every language as it was spoken" is not a Turkish caption request.

The incident (Plaza T2 "maximo 2", job e46baf42, phone Talking render on the iPhone path): a
mixed English/Turkish take (the guide speaks English, the creator translates into Turkish in
the middle). The creator asked: "Keep every language as it was spoken: English captions for
the English, Turkish captions for my Turkish, nothing translated." The dispatcher parser read
that as "Turkish captions" (the last clause won) and stamped `caption_language_request: "tr"`.
`_run_phone_subtitled_job` then took the requested-language path: after speech cleanup (13
cuts) the clip was re-transcribed with whisper hinted "tr" and every English line came out as
a Turkish translation ("Peki ismin neydi?"), the variant was stamped `caption_language: "tr"`,
and the post-render review of the ask said only "Have a look".

Done when (KRI-1015): a creator who captions two languages each as itself gets the take as
spoken, English for the English, nothing translated, and a render that did translate says so.

Failure modes, written before the code (each is a test below):

 1. The dispatched request (and the bare kit PROMPT.txt) parse to "tr" although they ask for
    no single target language: the stamp is what turns the whole take into a translation.  [d]
 2. With the stamp gone, the cleanup branch must still not hint whisper with a language, call
    the translator, or stamp anything but the spoken language: the cues are the words the
    preflight heard, cut by the recorded 13 removals, the variant says `en` and carries NO
    `caption_translated_from`.                                                              [a]
 3. The fixed path passes for the wrong reason: the "English kept" check is grounded on an
    independent expectation (a local whisper pass forced to English over the same clip, not the
    preflight words the code under test reads), keeps only phrases that survive the recorded
    cuts, accepts the independent English, and rejects prod's Turkish cues.            [a, c, o]
 4. A render that did translate the English into Turkish (the prod path, replayed with the
    stamp forced) keeps its Turkish cues but records `caption_translated_from == "en"` and the
    review says so: partial, "I translated the English speech into Turkish captions."
    (Turkish: "İngilizce konuşmayı Türkçe altyazıya çevirdim.")                              [b]
 5. The review of the fixed render must not claim a translation.                              [a]
 6. A wrong-content render (prod's Turkish texts on the fixed render's cue timing) is rejected
    by the fulfillment check, so a green [a] cannot be a check that accepts anything.        [c]

Whisper (local whisper.cpp large-v3-turbo passes over the same 48 s clip, see
tests/fixtures/kri1015_mixed_caption_language/README.md) and the translator are stubbed; the
dispatcher's parser, the real phone Talking job, the real speech-cleanup plan builder, the cue
builder and the brief checks are the real ones. Nothing here touches the network.
"""

from __future__ import annotations

import json
import re
import unicodedata
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

import app.tasks.generative_build as gb
from app.kria.brief import BriefRequirement
from app.kria.brief_checks import build_receipts, plan_facts_from_phone_variant
from app.kria.reply_language import bind_reply_language, release_reply_language
from app.pipeline.caption_language import infer_language_from_text, parse_caption_language_request
from app.pipeline.speech_cleanup_analysis import (
    SpeechCleanupAnalysisInput,
    SpeechCleanupRemovalInput,
    analyze_speech_cleanup,
)
from app.pipeline.transcribe import Transcript, Word
from tests.tasks.test_phone_subtitled_narrated_dispatch import _setup_subtitled

FIXTURES = Path(__file__).resolve().parents[1] / "fixtures" / "kri1015_mixed_caption_language"


def _load(name: str) -> dict:
    return json.loads((FIXTURES / name).read_text(encoding="utf-8"))


INCIDENT = _load("incident.json")
DURATION_S = float(INCIDENT["original_duration_s"])  # 48.0: the whole kit clip
REMOVED = INCIDENT["removed"]  # the 13 cleanup removals prod applied, source time
PROD_CUES = INCIDENT["prod_variant"]["caption_cues"]  # prod's Turkish cues (output time)
R2 = INCIDENT["r2"]  # the brief requirement the creator's sentence became


def _words(name: str) -> list[Word]:
    return [
        Word(text=w["text"], start_s=w["start"], end_s=w["end"], confidence=1.0)
        for w in _load(name)["words"]
    ]


def _transcript(words: list[Word], language: str) -> Transcript:
    return Transcript(words=words, language=language, full_text=" ".join(w.text for w in words))


# What prod's preflight heard (original clip time): English for 0-26 s, whisper-1 on a take it
# could not hold in one language. The language is authored "en" (the merged pass's language).
SPOKEN_WORDS = _words("spoken_words.json")
SPOKEN = _transcript(SPOKEN_WORDS, "en")
# The independent passes over the same clip (local whisper large-v3-turbo, see README):
TRUTH_EN_WORDS = _words("truth_words_en.json")  # forced English: the expectation source
# Forced Turkish: it translated the English into Turkish. This is the tr-hinted pass prod ran.
TRUTH_TR = _transcript(_words("truth_words_tr.json"), "tr")

# English the creator spoke in 0-26 s that is in BOTH the preflight words and the independent
# English pass, and is not touched by a recorded cut.
EXPECTED_ENGLISH = ("translate it to Turkish", "what was your name", "gained independence")
# Three of prod's Turkish renderings of exactly those English lines.
PROD_TURKISH_RENDERINGS = (
    "Tamam, Türkçe çevireceğim",
    "Peki ismin neydi",
    "İndependansı kazandın",
)


def _norm(text: str) -> str:
    """Letters and digits only, lower case, accents folded (so "İndependansı" is comparable)."""
    folded = unicodedata.normalize("NFKD", text)
    letters = "".join(ch for ch in folded if not unicodedata.combining(ch))
    return " ".join(re.sub(r"[^0-9a-zıöüçğş]+", " ", letters.casefold()).split())


def english_kept_and_nothing_translated(cues: list[dict]) -> bool:
    """The KRI-1015 fulfillment check on a finished render's caption cues.

    True when every English phrase the creator spoke (see EXPECTED_ENGLISH) is on screen as
    spoken and none of prod's Turkish renderings of them is. A bare "language == en" label is
    never the evidence: the text is read."""
    on_screen = _norm(" ".join(str(cue["text"]) for cue in cues))
    return all(_norm(p) in on_screen for p in EXPECTED_ENGLISH) and not any(
        _norm(t) in on_screen for t in PROD_TURKISH_RENDERINGS
    )


# ------------------------------------------------------------------------------ the harness


@pytest.fixture
def translator(monkeypatch) -> Mock:
    """The provider call, stubbed. Nothing in this replay may need it: any call is recorded
    and fails open into a RuntimeError, so a test asserts on the calls, not on a side effect."""
    mock = Mock(side_effect=RuntimeError("no translator call is expected in this replay"))
    monkeypatch.setattr("app.pipeline.caption_translate._llm_translate_lines", mock)
    return mock


@pytest.fixture
def events(monkeypatch) -> Mock:
    from app.services import pipeline_trace

    recorded = Mock()
    monkeypatch.setattr(pipeline_trace, "record_pipeline_event", recorded)
    return recorded


def _event(events: Mock, name: str) -> list[dict]:
    return [call.args[2] for call in events.call_args_list if call.args[1] == name]


def _whisper_by_hint(monkeypatch, by_hint: dict) -> Mock:
    """whisper per language hint; every call is recorded. A hint nobody stubbed hears nothing
    (an empty transcript), so a stray call shows up in the assertions, not as a KeyError."""
    mock = Mock(
        side_effect=lambda _path, language=None, **_kw: by_hint.get(
            language, Transcript(words=[], language=language or "", full_text="")
        )
    )
    monkeypatch.setattr("app.pipeline.transcribe.transcribe_whisper_cached", mock)
    return mock


def _hints(whisper: Mock) -> list[str | None]:
    return [call.kwargs.get("language") for call in whisper.call_args_list]


def _preflight_snapshot(*, storage_path: str) -> dict:
    """The raw ``preflight_snapshot`` the accepted "clean" choice persisted, built by the REAL
    cleanup analysis: the recorded spoken words, and exactly the 13 removals prod applied
    (fed as pre-approved removals, no silence detection run)."""
    from app.services.clip_speech import SilenceDetectionResult

    fingerprint = "b" * 64
    analysis = analyze_speech_cleanup(
        SpeechCleanupAnalysisInput(
            source_fingerprint=fingerprint,
            local_media_path="preflight.wav",
            duration_s=DURATION_S,
            source_window_start_s=0.0,
            source_window_end_s=DURATION_S,
            mixed_gap_mode="off",
            over_budget_policy="clamp",
            forced_removals=tuple(
                SpeechCleanupRemovalInput(
                    start_s=row["start_s"], end_s=row["end_s"], reason=row["reason"]
                )
                for row in REMOVED
            ),
        ),
        transcribe_fn=lambda *_a, **_k: SimpleNamespace(
            words=SPOKEN_WORDS, language=SPOKEN.language, low_confidence=False
        ),
        silence_detect_fn=lambda *_a, **_k: SilenceDetectionResult(spans=[], status="ok"),
    )
    return {
        "schema_version": 1,
        "analysis_id": "b06acb2b-c569-46ac-9b7e-2f5318a3fba2",
        "engine_version": "preflight-v1-2026-09-05",
        "detector_version": analysis.detector_version,
        "source": {
            "kind": "embedded_spine",
            "media_identity": "c0",
            "storage_path": storage_path,
            "generation": "1",
            "window_start_s": 0.0,
            "window_end_s": DURATION_S,
            "source_policy_fingerprint": fingerprint,
        },
        "analysis": analysis.to_payload(),
    }


def _render(monkeypatch, *, requested: str | None, by_hint: dict):
    """Run the real phone Talking job through the speech-cleanup branch on the recorded take.

    ``requested`` is the ``caption_language_request`` the dispatcher stamped (None: nothing
    was stamped, the key is absent like on every job without an ask). Returns
    (job, variant, whisper)."""
    job, snapshot, _session, binding = _setup_subtitled(monkeypatch, duration_s=DURATION_S)
    job.all_candidates["language"] = "en"
    job.all_candidates["creator_request"] = INCIDENT["dispatched_request"]
    if requested is not None:
        job.all_candidates["caption_language_request"] = requested
    snapshot["speech_cleanup_contract"] = "required_v1"
    snapshot["_speech_cleanup_internal"] = {
        "preflight_snapshot": _preflight_snapshot(storage_path=binding.proxy_path)
    }
    whisper = _whisper_by_hint(monkeypatch, by_hint)
    gb._run_phone_subtitled_job(str(job.id), snapshot, job.all_candidates, ownership_epoch=3)
    [variant] = job.assembly_plan["variants"]
    return job, variant, whisper


def _cues(variant: dict) -> list[dict]:
    return [
        {"text": cue["text"], "start": cue["start_s"], "end": cue["end_s"]}
        for cue in variant["caption_cues"]
    ]


def _r2_receipt(variant: dict):
    """The render-ready receipt of the creator's sentence against the finished variant."""
    req = BriefRequirement(
        id="r2",
        kind="style",
        scope="global",
        description=R2["description"],
        facts=dict(R2["facts"]),
    )
    [receipt] = build_receipts(
        [req], plan_facts_from_phone_variant(variant), include_unchecked=True
    )
    return receipt


# ------------------------------------------------------------ [o] the oracle is independent


def test_o_the_expected_english_is_in_the_independent_pass_and_survives_the_recorded_cuts():
    """The phrases [a] looks for come from the forced-English pass over the audio, not from the
    words the code under test reads, and each is still spoken after the recorded cuts: a
    green [a] cannot mean "the check only asks for what the code was always going to print"."""
    independent = [_norm(w.text) for w in TRUTH_EN_WORDS]
    spoken = [_norm(w.text) for w in SPOKEN_WORDS]
    for phrase in EXPECTED_ENGLISH:
        tokens = _norm(phrase).split()
        assert _find(independent, tokens) is not None, f"{phrase!r} not in the independent pass"
        start = _find(spoken, tokens)
        assert start is not None, f"{phrase!r} not in the recorded spoken words"
        for word in SPOKEN_WORDS[start : start + len(tokens)]:
            cut = [r for r in REMOVED if r["start_s"] < word.end_s and word.start_s < r["end_s"]]
            assert not cut, f"{word.text!r} is inside the recorded removal {cut}"
    # and the Turkish renderings are really prod's, not an invented foil
    prod_text = _norm(" ".join(cue["text"] for cue in PROD_CUES))
    assert all(_norm(t) in prod_text for t in PROD_TURKISH_RENDERINGS)
    assert INCIDENT["prod_variant"]["caption_language"] == "tr"


def _find(haystack: list[str], needle: list[str]) -> int | None:
    for start in range(len(haystack) - len(needle) + 1):
        if haystack[start : start + len(needle)] == needle:
            return start
    return None


def test_o_the_fulfillment_check_accepts_independent_english_and_rejects_prod():
    independent_english = [{"text": " ".join(w.text for w in TRUTH_EN_WORDS)}]
    assert english_kept_and_nothing_translated(independent_english) is True
    prod = [{"text": cue["text"]} for cue in PROD_CUES]
    assert english_kept_and_nothing_translated(prod) is False


def test_o_the_recorded_cuts_replay_exactly_through_the_real_cleanup_analysis():
    """The harness applies the 13 removals prod applied (8.765 s), nothing else."""
    snapshot = _preflight_snapshot(storage_path="phone-proxies/c0.mp4")
    plan = snapshot["analysis"]["cut_plan"]
    # pytest 9.2 rejects approx() on nested pairs, so compare flattened bounds.
    assert [b for r in plan["removed"] for b in (r["start_s"], r["end_s"])] == pytest.approx(
        [b for r in REMOVED for b in (r["start_s"], r["end_s"])]
    )
    assert plan["time_saved_s"] == pytest.approx(8.765, abs=1e-3)
    assert snapshot["analysis"]["language"] == "en"


# ------------------------------------------------------------------- [d] the parser half


def test_d_the_recorded_dispatched_request_asks_for_no_single_caption_language():
    assert parse_caption_language_request(INCIDENT["dispatched_request"]) is None


def test_d_the_bare_kit_prompt_asks_for_no_single_caption_language():
    prompt = INCIDENT["prompt_txt"]
    assert "English captions for the English, Turkish captions for my Turkish" in prompt
    assert parse_caption_language_request(prompt) is None


def test_d_prod_stamped_tr_from_exactly_that_text():
    """The anchor of the incident: the job row held `caption_language_request: "tr"`."""
    assert INCIDENT["stamped_in_prod"] == "tr"


# ----------------------------------------------------------- [a] the fixed path, replayed


def test_a_keep_every_language_as_spoken_captions_the_english_as_english(
    monkeypatch, translator, events
):
    # what the dispatcher does with the creator's words (services/generative_jobs.py)
    requested = parse_caption_language_request(INCIDENT["dispatched_request"])
    assert requested is None

    _job, variant, whisper = _render(
        monkeypatch,
        requested=requested,
        by_hint={None: SPOKEN, "tr": TRUTH_TR},
    )

    # the cleanup was applied exactly as recorded: 13 cuts, 8.765 s, cues on the cut timeline
    assert variant["silence_cut_outcome"] == "applied"
    assert len(variant["silence_cut"]["removed"]) == len(REMOVED) == 13
    assert all(cue["end_s"] <= DURATION_S - 8.765 + 1e-6 for cue in variant["caption_cues"])
    # nothing was hinted, nothing was translated, nothing was "requested"
    assert [h for h in _hints(whisper) if h is not None] == []
    translator.assert_not_called()
    assert _event(events, "caption_language_requested") == []
    assert _event(events, "caption_translation") == []
    assert _event(events, "caption_language_request_unmet") == []
    # the captions are what he said, in English, and not prod's Turkish
    cues = _cues(variant)
    joined = " ".join(cue["text"] for cue in cues)
    assert english_kept_and_nothing_translated(cues), joined
    assert "Okay, I'll translate it to Turkish" in joined  # the opening line, verbatim
    # the stamp is the language spoken, and no translation is recorded
    assert variant["caption_language"] == "en"
    assert "caption_translated_from" not in variant
    # the review cannot claim a translation that did not happen
    receipt = _r2_receipt(variant)
    assert "translated" not in receipt.reason.lower()


# -------------------------------------------- [b] negative control: the prod path, replayed


def test_b_a_forced_tr_stamp_reproduces_prod_and_the_review_says_it_translated(
    monkeypatch, translator, events
):
    """Stamp `tr` as prod did: the tr-hinted pass is the Turkish truth pass (it translated
    the English), the cues are Turkish and the label is `tr` -- the prod shape. The fix is not
    "never translate", it is "say so": the variant records where the translation came from
    and the review reports it."""
    _job, variant, whisper = _render(
        monkeypatch,
        requested=INCIDENT["stamped_in_prod"],
        by_hint={None: SPOKEN, "tr": TRUTH_TR},
    )

    # the requested-language pass ran with the Turkish hint, like prod
    assert "tr" in _hints(whisper)
    cues = _cues(variant)
    joined = " ".join(cue["text"] for cue in cues)
    assert infer_language_from_text(joined) == "tr", joined
    assert "Tamam" in joined
    assert "Okay, I'll translate" not in joined and "what was your name" not in joined
    assert english_kept_and_nothing_translated(cues) is False
    assert variant["caption_language"] == "tr"
    # ...and the render now records that the English speech was translated into these captions
    assert variant.get("caption_translated_from") == "en"

    receipt = _r2_receipt(variant)
    assert receipt.status == "partial"
    assert "translated the English speech into Turkish captions" in receipt.reason
    token = bind_reply_language("tr")
    try:
        in_turkish = _r2_receipt(variant)
    finally:
        release_reply_language(token)
    assert in_turkish.status == "partial"
    assert "İngilizce konuşmayı Türkçe altyazıya çevirdim." in in_turkish.reason


# ---------------------------------------------------- [c] wrong-content control (the oracle)


def test_c_prod_turkish_texts_on_the_fixed_renders_timing_are_rejected(monkeypatch, translator):
    """The fulfillment check from [a] must be able to fail: the fixed render's cue count and
    timing, but prod's Turkish texts."""
    _job, variant, _whisper = _render(
        monkeypatch, requested=None, by_hint={None: SPOKEN, "tr": TRUTH_TR}
    )
    fixed = _cues(variant)
    assert english_kept_and_nothing_translated(fixed), "[a] precondition: the fixed cues pass"

    wrong = [{**cue, "text": PROD_CUES[i % len(PROD_CUES)]["text"]} for i, cue in enumerate(fixed)]
    assert len(wrong) == len(fixed)
    assert [(c["start"], c["end"]) for c in wrong] == [(c["start"], c["end"]) for c in fixed]
    assert english_kept_and_nothing_translated(wrong) is False


def test_c_one_translated_line_among_the_english_is_rejected(monkeypatch, translator):
    _job, variant, _whisper = _render(
        monkeypatch, requested=None, by_hint={None: SPOKEN, "tr": TRUTH_TR}
    )
    fixed = _cues(variant)
    one_translated = [dict(cue) for cue in fixed]
    one_translated.append({"text": "Peki ismin neydi?", "start": 0.0, "end": 1.0})
    assert english_kept_and_nothing_translated(fixed) is True
    assert english_kept_and_nothing_translated(one_translated) is False


def test_c_english_that_lost_a_spoken_line_is_rejected(monkeypatch, translator):
    """Not translated, but a line the creator said is gone: still not "kept as spoken"."""
    _job, variant, _whisper = _render(
        monkeypatch, requested=None, by_hint={None: SPOKEN, "tr": TRUTH_TR}
    )
    fixed = _cues(variant)
    lost = [cue for cue in fixed if "independence" not in cue["text"].lower()]
    assert len(lost) < len(fixed)
    assert english_kept_and_nothing_translated(lost) is False
