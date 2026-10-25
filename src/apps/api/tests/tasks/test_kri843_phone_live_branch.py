"""KRI-843 follow-up: the phone Talking writer's LIVE branch transcribes a mixed take per stretch.

PR #1782 taught the preflight engine to transcribe a mixed-language take (Don Julio T1: an
English question, Lucas's Spanish, Turkish table chatter) stretch by stretch. The phone Talking
writer only saw those words through the preflight snapshot: with cleanup declined and no quoted
start/end line (`_run_phone_subtitled_job`'s ``else:`` branch) it transcribed live with one
auto-language pass plus the EN/TR cross-check, so the same take got single-language captions.
And `speech_cleanup_preflight_mode` defaults to "off", so for most creators that live branch IS
the path.

Replayed on the real take's whisper.cpp passes (`tests/fixtures/kri843_don_julio_t1_passes.json`,
free, offline) through the real `_run_phone_subtitled_job`; only transcription is stubbed.
Failure modes L1-L13: `docs/reviews/kri-843-live-branch-failure-modes.md`.
"""

from __future__ import annotations

import json
import re
from pathlib import Path
from unittest.mock import Mock

import pytest

from app.config import settings
from app.pipeline.caption_language import crosscheck_detected_language
from app.pipeline.caption_translate import CaptionLanguageResult
from app.pipeline.transcribe import TranscribeError, Transcript, Word
from app.services.mixed_language_transcript import MAX_EXTRA_PASSES, reference_languages
from app.tasks import generative_build as gb
from tests.tasks.test_generative_build import _Meta
from tests.tasks.test_phone_subtitled_narrated_dispatch import _setup_subtitled

FIXTURES = Path(__file__).resolve().parents[1] / "fixtures"
T1 = json.loads((FIXTURES / "kri843_don_julio_t1_passes.json").read_text())
M1 = json.loads((FIXTURES / "kri808_don_julio_clip_speech.json").read_text())["clips"]
DURATION_S = float(T1["duration_s"])
R1 = T1["references"]["R1"]["text"]

# Hand ground truth (the fixture's `speech_truth`): Yasin's English, Lucas's Spanish, then the
# Turkish chatter nothing but the first pass hears.
YASIN_S = (1.0, 3.9)
LUCAS_S = (3.9, 11.5)
CHATTER_FROM_S = 16.5
MURMUR = {"videoya", "bak"}
TRANSLATION = {"evet", "adın", "adin", "lukas"}

_TURKISH_LETTERS = re.compile("[ğşıİ]")
_TURKISH_WORDS = frozenset(
    "evet adın adin isminiz ne bak videoya videomu yandık yandik hiçbir hicbir şey sey "
    "bilmiyordum canım canim tepara ispanyol sağolun sagolun güzel guzel bilir".split()
)


def _token(text: str) -> str:
    return text.strip(" .,!?¡¿…\"'").lower()


def _turkish(tokens: list[str]) -> list[str]:
    return [t for t in tokens if _TURKISH_LETTERS.search(t) or t in _TURKISH_WORDS]


def _transcript(language: str, rows: list[dict]) -> Transcript:
    """The shape `transcribe_whisper_cached` returns; whisper.cpp's "-" dialogue dashes dropped."""
    words = []
    for row in rows:
        text = str(row["text"]).strip().lstrip("-").strip()
        if text and re.search(r"\w", text):
            words.append(
                Word(text=text, start_s=row["start_s"], end_s=row["end_s"], confidence=1.0)
            )
    return Transcript(words=words, full_text=" ".join(w.text for w in words), language=language)


def _t1_passes() -> dict[str, Transcript]:
    return {name: _transcript(p["language"], p["words"]) for name, p in T1["passes"].items()}


def _whisper(monkeypatch, passes: dict[str, Transcript], *, first: str, errors=None) -> Mock:
    """`transcribe_whisper_cached`: ``language=None`` is the auto pass, ``"xx"`` a forced one."""
    errors = errors or {}

    def call(_path, *, language=None, verbatim_prompt=None, **_kwargs):
        if language in errors:
            raise errors[language]
        key = first if language is None else language
        return passes.get(key) or Transcript(words=[], full_text="", language=str(language))

    mock = Mock(side_effect=call)
    monkeypatch.setattr("app.pipeline.transcribe.transcribe_whisper_cached", mock)
    return mock


def _events(monkeypatch) -> Mock:
    from app.services import pipeline_trace

    events = Mock()
    monkeypatch.setattr(pipeline_trace, "record_pipeline_event", events)
    return events


def _payloads(events: Mock, name: str) -> list[dict]:
    return [c.args[2] for c in events.call_args_list if c.args[1] == name]


def _calls(whisper: Mock) -> list[str | None]:
    return [c.kwargs.get("language") for c in whisper.call_args_list]


def _render(
    monkeypatch,
    *,
    reference: str,
    passes: dict[str, Transcript],
    first: str = "tr",
    duration_s: float = DURATION_S,
    errors=None,
    request: str | None = None,
):
    """The real phone Talking job on one clip Gemini heard ``reference`` in, cleanup declined."""
    job, snapshot, _session, binding = _setup_subtitled(monkeypatch, duration_s=duration_s)
    meta = _Meta("c0", 5.0, transcript=reference)
    monkeypatch.setattr(
        gb,
        "_ingest_clips",
        lambda *a, **k: {
            "clip_metas": [meta],
            "clip_id_to_gcs": {"c0": binding.proxy_path},
            "clip_id_to_local": {"c0": "/tmp/c0.mp4"},
            "probe_map": {},
            "hero": meta,
        },
        raising=False,
    )
    if request is not None:
        job.all_candidates["caption_language_request"] = request
    # L11: no preflight evidence on the job at all (the default: preflight mode "off").
    assert settings.speech_cleanup_preflight_mode == "off"
    assert (
        gb._phone_opening_cut_snapshot(
            snapshot, storage_path=binding.proxy_path, duration_s=duration_s
        )
        is None
    )
    whisper = _whisper(monkeypatch, passes, first=first, errors=errors)
    events = _events(monkeypatch)

    gb._run_phone_subtitled_job(str(job.id), snapshot, job.all_candidates, ownership_epoch=3)

    [variant] = job.assembly_plan["variants"]
    return job, variant, whisper, events


def _cue_words(variant: dict) -> list[dict]:
    return [word for cue in variant["caption_cues"] for word in cue["words"]]


def _said_between(variant: dict, low_s: float, high_s: float) -> list[str]:
    return [_token(w["text"]) for w in _cue_words(variant) if low_s <= w["start_s"] < high_s]


def _timed(words) -> list[tuple[str, float, float]]:
    return [(w.text, w.start_s, w.end_s) for w in words]


# -- L3 / L4 / L10 / L11 / L13: the Don Julio take on the live branch ---------------------------


def test_l3_l4_l11_t1_live_captions_follow_each_stretch(monkeypatch) -> None:
    """Cleanup declined, no quoted line, preflight off: the captions still read the English
    question, Lucas's Spanish and the first pass's Turkish chatter, each where it is said."""
    _job, variant, whisper, events = _render(monkeypatch, reference=R1, passes=_t1_passes())

    calls = _calls(whisper)
    assert calls[0] is None and set(calls[1:]) == {"en", "es"} and len(calls) == 3
    assert len({c.kwargs.get("verbatim_prompt") for c in whisper.call_args_list}) == 1

    words = _cue_words(variant)
    yasin, lucas = _said_between(variant, *YASIN_S), _said_between(variant, *LUCAS_S)
    assert "what's your name" in " ".join(yasin), variant["caption_cues"]
    assert _turkish(yasin) == []
    assert any(t.startswith("salsa") for t in lucas) and any(
        t.startswith("tradicional") for t in lucas
    )
    assert _turkish(lucas) == []
    # L3: nothing of the Turkish translation survives (the chatter from 16.5 s really says
    # "evet"/"ne", so it is judged up to there); the only Turkish before the chatter is the
    # murmur the first pass keeps.
    before_chatter = [_token(w["text"]) for w in words if w["start_s"] < CHATTER_FROM_S]
    assert not TRANSLATION & set(before_chatter), before_chatter
    assert set(_turkish(before_chatter)) <= MURMUR, before_chatter
    # L4: the chatter past what any pass confirms is the FIRST pass, word for word.
    first_pass = _t1_passes()["tr"]
    assert [
        (w["text"], w["start_s"], w["end_s"]) for w in words if w["start_s"] >= CHATTER_FROM_S
    ] == [(w.text, w.start_s, w.end_s) for w in first_pass.words if w.start_s >= CHATTER_FROM_S]
    assert _turkish([_token(w["text"]) for w in words if w["start_s"] >= CHATTER_FROM_S])
    # L13: valid cue words for the device recipe.
    starts = [w["start_s"] for w in words]
    assert starts == sorted(starts)
    assert all(0.0 <= w["start_s"] <= w["end_s"] <= DURATION_S + 1e-6 for w in words)
    assert variant["caption_language"] == "en"  # the pass with the most confirmed words

    # L10: the same receipt the preflight engine writes, on the captions event.
    [receipt] = _payloads(events, "caption_language_crosscheck")
    assert receipt["variant_id"] == "subtitled"
    assert receipt["mixed"] is True and receipt["applied"] is True
    assert receipt["whisper_language"] == "tr"
    assert set(receipt["reference_languages"]) == {"en", "es", "tr"}
    assert (set(receipt["tried"]), set(receipt["heard"]), receipt["failed"]) == (
        {"en", "es"},
        {"en", "es"},
        [],
    )
    assert [(s["language"], s["confirmed"]) for s in receipt["segments"][:2]] == [
        ("en", True),
        ("es", True),
    ]


# -- L1 / L2 / L6: controls -----------------------------------------------------------------------


def test_l1_a_single_language_take_keeps_todays_live_path(monkeypatch) -> None:
    """M1_05 (Yasin's question, 4 s): prod whisper-1 said Turkish, Gemini heard English. Today's
    EN/TR re-run, calls and event payload must not change by a byte."""
    clip = M1["M1_05"]
    passes = {
        "first": _transcript(
            clip["prod_whisper_first_pass"]["language"], clip["prod_whisper_first_pass"]["words"]
        ),
        "en": _transcript("en", clip["local_whisper_medium"]["en"]["words"]),
    }

    _job, variant, whisper, events = _render(
        monkeypatch,
        reference=clip["prod_record"]["transcript"],
        passes=passes,
        first="first",
        duration_s=4.0,
    )

    assert _calls(whisper) == [None, "en"]
    assert " ".join(c["text"] for c in variant["caption_cues"]) == "What's your name? Lucas."
    assert variant["caption_language"] == "en"
    assert _payloads(events, "caption_language_crosscheck") == [
        {
            "variant_id": "subtitled",
            "whisper_language": "tr",
            "reference_language": "en",
            "applied": True,
        }
    ]


def test_l2_kill_switch_off_is_todays_live_path(monkeypatch) -> None:
    monkeypatch.setattr(settings, "speech_cleanup_mixed_language_enabled", False)
    passes = _t1_passes()
    # Gemini's R1 reads neither English nor Turkish to the EN/TR check: one pass, no event.
    assert crosscheck_detected_language("tr", reference_text=R1) is None

    _job, variant, whisper, events = _render(monkeypatch, reference=R1, passes=passes)

    assert _calls(whisper) == [None]
    assert [(w["text"], w["start_s"], w["end_s"]) for w in _cue_words(variant)] == _timed(
        passes["tr"].words
    )
    assert variant["caption_language"] == "tr"
    assert _payloads(events, "caption_language_crosscheck") == []


def test_l6_names_in_an_english_reference_are_not_a_second_language(monkeypatch) -> None:
    reference = (
        "Hey Lucas. We are at Don Julio, the salsa criolla and the chimichurri are on the "
        "table. Cheers to everyone, thank you."
    )
    assert reference_languages(reference) == ["en"]
    english = Transcript(
        words=[Word(text="Hey", start_s=0.0, end_s=0.3, confidence=1.0)],
        full_text="Hey",
        language="en",
    )

    _job, variant, whisper, events = _render(
        monkeypatch, reference=reference, passes={"en": english}, first="en", duration_s=4.0
    )

    assert _calls(whisper) == [None]
    assert _payloads(events, "caption_language_crosscheck") == []
    assert variant["caption_language"] == "en"


# -- L5: the creator's requested caption language still wins -----------------------------------


def test_l5_requested_caption_language_still_overrides_the_merged_pass(monkeypatch) -> None:
    """ "Turkish captions" on the T1 take: the SPOKEN pass is still merged per stretch (beats,
    grounding and spoken-text asks read it), the Turkish-hinted caption pass still runs on
    the original clip, and KRI-838's text check still judges its words."""
    passes = _t1_passes()
    hinted = passes["tr"]
    judged: list[list] = []

    def ensure(words, *, requested, fallback_language, **_kwargs):
        judged.append([(w.text, w.start_s, w.end_s) for w in words])
        assert requested == "tr"
        assert fallback_language == "en"  # the merged spoken pass's label, never the request
        return CaptionLanguageResult(list(words), "tr", "heard", detected="tr")

    monkeypatch.setattr(gb, "_ensure_requested_caption_language", ensure)

    _job, variant, whisper, events = _render(monkeypatch, reference=R1, passes=passes, request="tr")

    calls = _calls(whisper)
    assert calls[0] is None and set(calls[1:3]) == {"en", "es"} and calls[3:] == ["tr"]
    assert judged == [_timed(hinted.words)]
    assert [(w["text"], w["start_s"], w["end_s"]) for w in _cue_words(variant)] == _timed(
        hinted.words
    )
    assert variant["caption_language"] == "tr"
    assert _payloads(events, "caption_language_requested") == [
        {"variant_id": "subtitled", "requested": "tr", "spoken": "en"}
    ]
    [receipt] = _payloads(events, "caption_language_crosscheck")
    assert receipt["mixed"] is True


# -- L8 / L9: cost cap and a failed extra pass -------------------------------------------------


def test_l8_extra_passes_are_capped_on_the_live_branch(monkeypatch) -> None:
    reference = (
        "What's your name? Thank you very much for this. Evet, bu çok güzel bir şey. "
        "Acá tenemos salsas tradicionales, muy bueno. Das ist nicht so gut, aber wir haben es. "
        "Nous sommes ici avec vous, merci beaucoup."
    )
    assert len(reference_languages(reference)) == 5

    _job, _variant, whisper, _events = _render(
        monkeypatch, reference=reference, passes=_t1_passes()
    )

    calls = _calls(whisper)
    assert calls[0] is None
    extras = calls[1:]
    assert len(extras) == MAX_EXTRA_PASSES
    assert len(set(extras)) == len(extras) and "tr" not in extras


def test_l8_a_first_pass_with_no_words_pays_no_extra_pass(monkeypatch) -> None:
    silent = {"tr": Transcript(words=[], full_text="", language="tr")}

    _job, _variant, whisper, events = _render(monkeypatch, reference=R1, passes=silent)

    assert _calls(whisper) == [None]
    assert _payloads(events, "caption_language_crosscheck") == []


def test_l9_a_failed_extra_pass_is_skipped_on_the_live_branch(monkeypatch) -> None:
    """whisper-1 fails the Spanish pass: the render still ships, the English question is
    taken from the English pass, Lucas stays as the first pass heard him, the receipt says."""
    passes = _t1_passes()

    _job, variant, whisper, events = _render(
        monkeypatch, reference=R1, passes=passes, errors={"es": TranscribeError("boom")}
    )

    calls = _calls(whisper)
    assert calls[0] is None and set(calls[1:]) == {"en", "es"}
    assert "what's your name" in " ".join(_said_between(variant, *YASIN_S))
    first_pass_lucas = [
        _token(w.text) for w in passes["tr"].words if LUCAS_S[0] <= w.start_s < LUCAS_S[1]
    ]
    assert _said_between(variant, *LUCAS_S) == first_pass_lucas
    [receipt] = _payloads(events, "caption_language_crosscheck")
    assert receipt["mixed"] is True and receipt["applied"] is True
    assert (receipt["heard"], receipt["failed"]) == (["en"], ["es"])


@pytest.mark.parametrize("error", [KeyError("k"), TypeError("t")])
def test_l9_a_programming_error_in_an_extra_pass_escapes(monkeypatch, error) -> None:
    from app.pipeline.speech_cleanup_analysis import merge_mixed_language_passes

    passes = _t1_passes()

    def transcribe(_path, *, language, verbatim_prompt):
        raise error

    with pytest.raises(type(error)):
        merge_mixed_language_passes(
            passes["tr"],
            media_path="take.wav",
            reference_text=R1,
            languages=["en", "tr", "es"],
            duration_s=DURATION_S,
            covers_s=None,
            transcribe=transcribe,
            verbatim_prompt=None,
            receipt={"whisper_language": "tr", "reference_language": "", "applied": False},
        )
