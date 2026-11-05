"""Green 4 / Green 5 (2026-11-04): the phone Talking captions misspell the brand the creator typed
and keep whisper's title-casing of ordinary words.

The creator wrote "Yorumla episode 9: Kervan x Lumo Pamukkale show." Both renders captioned
"curvan / Curvan celebrated its 10th year with", and the chip render (no case ask) showed
"a Sunrise Fashion Show at Pamukkale." Whisper heard "Curvan"; the creator-spellings step
(KRI-631/793) only takes quoted words or the brief's listed spellings, so the name the creator
typed never reached it. The capitals come from punctuation restore copying whisper's full-text
casing onto the timed words; Gemini's own record of the same take writes "a Sunrise fashion show".

This runs the real writer (`_run_phone_subtitled_job`) on the take's real words with the request
and brief the jobs pinned. No model calls (caption correction off, as in every writer test).

Ways the fix could fail (written before the fix):
1. The name the creator typed (not quoted, not listed) still never reaches the spelling step.
2. A name target respells an ordinary word that merely sounds close ("caravan" for "Kervan").
3. A respelled word loses its timing, or a word is added or dropped.
4. A request with no names (the "Suggest an edit." chip) changes words anyway.
5. The case step lowercases a real name ("Pamukkale", "Lumo") or a sentence start ("Okay").
6. The case step lowercases a capital that the independent record (Gemini) also writes.
7. The switch off is not byte-identical to before.

Review (Emir): "Do not change Curvan to Kervan" still captioned "Kervan": both names the creator
typed became targets, and "Kervan" sound-matched "Curvan". A caption word that reads as a word the
creator typed is already a creator spelling, so a typed-name target never respells it; no wording
is read. Ways the review fix could fail (written before the fix):
8. A typed name is still respelled into another typed name ("Curvan" -> "Kervan").
9. The rule leans on wording or position: the iPhone's "Don’t", a Turkish sentence, or the name
   starting a sentence ("Curvan'ı ...", never a target there) is missed.
10. A case-ended caption form of the typed name ("Curvan'ın", "Curvanın") is still respelled.
11. Over-reach: the Green 4 request (only "Kervan" typed) stops respelling "Curvan" (1).
12. Model-owned spellings change: a quoted ask or a word the brief lists no longer respells a
    typed word (an explicit "write "Kervan"" must still win).
13. A typed-name target respells a word that already reads as a model-owned spelling.
"""

from __future__ import annotations

import copy
import json
from pathlib import Path

import pytest

from app.config import settings
from app.pipeline.transcribe import Word
from app.tasks import generative_build as gb
from tests.tasks.test_generative_build import _Meta
from tests.tasks.test_phone_subtitled_narrated_dispatch import _setup_subtitled

FIXTURE = json.loads((Path(__file__).parent / "green45_caption_text.json").read_text())


def _words(rows: list[dict]) -> list[Word]:
    return [
        Word(text=w["text"], start_s=w["start_s"], end_s=w["end_s"], confidence=1.0) for w in rows
    ]


def _run(
    monkeypatch, *, request: str, binding: dict | None, rows: list[dict] | None = None
) -> dict:
    rows = rows or FIXTURE["take_words"]
    job, snapshot, _session, source = _setup_subtitled(
        monkeypatch, duration_s=FIXTURE["take_duration_s"], transcript_words=_words(rows)
    )
    meta = _Meta("c0", 5.0, transcript=FIXTURE["gemini_transcript"])
    monkeypatch.setattr(
        gb,
        "_ingest_clips",
        lambda *a, **k: {
            "clip_metas": [meta],
            "clip_id_to_gcs": {"c0": source.proxy_path},
            "clip_id_to_local": {"c0": "/tmp/c0.mp4"},
            "probe_map": {},
            "hero": meta,
        },
        raising=False,
    )
    job.all_candidates.update(
        {
            "creator_request": request,
            "creator_strategy": {
                "edit_format": "subtitled",
                "audio_strategy": "original_audio",
                "caption_style": "clean",
            },
        }
    )
    if binding is not None:
        snapshot["creator_brief_binding"] = binding
        job.assembly_plan["creator_brief_binding"] = binding
    gb._run_phone_subtitled_job(str(job.id), snapshot, job.all_candidates, ownership_epoch=3)
    assert job.status == "awaiting_device", (job.status, job.error_detail)
    return job.assembly_plan["variants"][0]


def _shown(variant: dict) -> list[dict]:
    return [word for cue in variant["caption_cues"] for word in cue.get("words") or []]


def _text(variant: dict) -> str:
    return " ".join(cue["text"] for cue in variant["caption_cues"])


def test_green4_captions_spell_the_brand_the_creator_typed(monkeypatch):
    variant = _run(
        monkeypatch, request=FIXTURE["green4_request"], binding=FIXTURE["green4_brief_binding"]
    )
    text = _text(variant)
    assert "curvan" not in text.casefold(), text
    assert "kervan celebrated its 10th year" in text.casefold(), text
    # 3: every word keeps its own timing; only the misheard name changed.
    shown = _shown(variant)
    heard = FIXTURE["take_words"]
    assert [(w["start_s"], w["end_s"]) for w in shown] == [
        (w["start_s"], w["end_s"]) for w in heard
    ]
    changed = {
        (h["text"].casefold(), s["text"].casefold())
        for h, s in zip(heard, shown, strict=True)
        if h["text"].casefold() != s["text"].casefold()
    }
    assert changed == {("curvan", "kervan")}, changed


def test_an_ordinary_word_that_sounds_close_is_left_alone(monkeypatch):
    """2: negative control, "caravan" is a word, not a mishearing of the brand."""
    rows = copy.deepcopy(FIXTURE["take_words"])
    index = next(i for i, w in enumerate(rows) if w["text"] == "partner.")
    rows[index]["text"] = "caravan."
    variant = _run(
        monkeypatch,
        request=FIXTURE["green4_request"],
        binding=FIXTURE["green4_brief_binding"],
        rows=rows,
    )
    assert "caravan." in [w["text"].casefold() for w in _shown(variant)], _text(variant)


def test_a_request_without_names_changes_no_word(monkeypatch):
    """4: the chip job's request names nothing, so the heard words stay as heard."""
    variant = _run(monkeypatch, request=FIXTURE["green5_chip_request"], binding=None)
    assert [w["text"] for w in _shown(variant)][:5] == [
        w["text"] for w in FIXTURE["take_words"][:5]
    ]


def test_green5_captions_drop_title_case_the_take_record_does_not_have(monkeypatch):
    variant = _run(monkeypatch, request=FIXTURE["green5_chip_request"], binding=None)
    text = _text(variant)
    assert "fashion show at" in text, text  # whisper: "Fashion Show"; Gemini: "fashion show"
    # 5, 6: real names, sentence starts and capitals both hearings agree on stay.
    assert "Pamukkale" in text and "Lumo" in text and text.startswith("Okay"), text
    assert "Sunrise" in text, text


@pytest.mark.parametrize(
    ("added", "heard_as"),
    [
        ("", "kervan celebrated"),  # 11: Green 4, only "Kervan" typed
        ("Do not change Curvan to Kervan.", "curvan celebrated"),  # the review's own sentence
        ("Don’t change Curvan to Kervan.", "curvan celebrated"),  # the iPhone's apostrophe
        ("Curvan'ı Kervan olarak değiştirme.", "curvan celebrated"),  # a sentence start
        ("Lütfen Curvan'ı Kervan yapma.", "curvan celebrated"),
    ],
)
def test_a_name_the_creator_typed_is_never_respelled_into_another(monkeypatch, added, heard_as):
    """8, 9, 11: the request types "Kervan" in its first sentence either way."""
    request = FIXTURE["green4_request"].replace("No music.", f"No music. {added}".rstrip())
    variant = _run(monkeypatch, request=request, binding=FIXTURE["green4_brief_binding"])
    text = _text(variant).casefold()
    assert f"{heard_as} its 10th year" in text, text


def test_a_case_ended_typed_name_keeps_its_spelling(monkeypatch):
    """10: the caption hears the typed name with a case ending."""
    rows = copy.deepcopy(FIXTURE["take_words"])
    index = next(i for i, w in enumerate(rows) if w["text"] == "Curvan")
    rows[index]["text"] = "Curvan'ın"
    request = FIXTURE["green4_request"].replace(
        "No music.", "No music. Do not change Curvan to Kervan."
    )
    variant = _run(monkeypatch, request=request, binding=FIXTURE["green4_brief_binding"], rows=rows)
    assert "curvan'ın celebrated" in _text(variant).casefold(), _text(variant)


def test_a_quoted_spelling_still_respells_a_typed_name(monkeypatch):
    """12: the creator's own quoted ask is model-owned wording and keeps its say."""
    request = FIXTURE["green4_request"].replace(
        "No music.", 'No music. Write "Kervan" in the captions, not Curvan.'
    )
    variant = _run(monkeypatch, request=request, binding=FIXTURE["green4_brief_binding"])
    assert "kervan celebrated its 10th year" in _text(variant).casefold(), _text(variant)


def test_typed_names_never_respell_a_creator_spelling():
    """8, 10, 12, 13 in isolation, on the writer's own calls."""
    from app.pipeline.caption_spelling import (
        apply_spelling_to_cues,
        creator_request_names,
        creator_typed_names,
    )

    request = "Yorumla episode 9: Kervan x Lumo Pamukkale show. Do not change Curvan to Kervan."
    names, typed = creator_request_names(request), creator_typed_names(request)
    words = ["Curvan'ın", "Curvanın", "Curvan,", "Kervon", "too."]
    cues = [
        {
            "text": " ".join(words),
            "words": [
                {"text": w, "start_s": float(i), "end_s": i + 0.5} for i, w in enumerate(words)
            ],
        }
    ]

    def shown(targets, *, request_names=names):
        [cue] = apply_spelling_to_cues(cues, targets, request_names=request_names, typed=typed)
        assert cue["text"] == " ".join(w["text"] for w in cue["words"])
        return cue["text"]

    # 8, 10: every hearing of the typed "Curvan" stays; "Kervon" (never typed) is respelled.
    assert shown(()) == "Curvan'ın Curvanın Curvan, Kervan too."
    # 12: a model-owned spelling (a quoted ask, a word the brief lists) respells as before.
    assert shown(("Kervan",)) == "Kervan'ın Kervan'ın Kervan, Kervan too."
    assert shown(("Kervan",), request_names=()) == shown(("Kervan",))
    # 13: the typed-name target "Meropa" never turns the model-owned "Merope" back (unguarded:
    # "Meropa Merope"); the model-owned target still respells the typed "Meropa" (12).
    [cue] = apply_spelling_to_cues(
        [{"text": "Merope Meropa"}], ("Merope",), request_names=("Meropa",), typed=("Meropa",)
    )
    assert cue["text"] == "Merope Merope"


def test_names_switch_off_keeps_the_heard_name(monkeypatch):
    """7: off is the words as before."""
    monkeypatch.setattr(settings, "caption_request_names_enabled", False)
    variant = _run(
        monkeypatch, request=FIXTURE["green4_request"], binding=FIXTURE["green4_brief_binding"]
    )
    assert "curvan celebrated" in _text(variant).casefold()


def test_case_switch_off_keeps_whispers_capitals(monkeypatch):
    """7: off is the words as before."""
    monkeypatch.setattr(settings, "caption_case_reference_enabled", False)
    variant = _run(monkeypatch, request=FIXTURE["green5_chip_request"], binding=None)
    assert "Sunrise Fashion Show" in _text(variant)
