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

Second review (Emir, on b101777): two inputs still respell "Curvan" into "Kervan". (a) "Do not
change curvan to Kervan." types the old name in lowercase, and only capitalised words counted as
typed. (b) "Do not use Kervan in the captions." excludes the name, yet it still became a target.
Ways this fix could fail (written before the fix):
14. A word the creator typed in lowercase is not a creator spelling, so a name target respells it.
15. A name the creator typed only in a "don't" clause still becomes a target.
16. A name excluded in one sentence is still taken from another sentence that types it.
17. The cue text and the timed words disagree (one path respelled, the other not).
18. The exclusion reads new wording: it must be the clause rule the quoted asks already use
    (KRI-631), so "Do not write "Kervan"" drops the quoted ask and the typed name alike.
19. Over-reach: Green 4 (no "don't" clause names "Kervan") stops respelling "Curvan" (1, 11).

Third review (Emir, on 3704d6330): "Kervan x Lumo Pamukkale show. Do not use kervan in the
captions." still respells "Curvan": only capitalised words in a "don't" clause were excluded, so
the lowercase "kervan" missed and the earlier "Kervan" stayed a target. Ways it could fail:
20. A name excluded in another case ("kervan", "KERVAN", "kervan'ı") stays a target.
21. Over-reach: an ordinary lowercase word in a "don't" clause drops an unrelated name.
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
        creator_typed_words,
    )

    # No "don't" clause here: a name typed in one is no target at all (15), see below.
    request = "Yorumla episode 9: Kervan x Lumo Pamukkale show. Curvan is the old name."
    names, typed = creator_request_names(request), creator_typed_words(request)
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


@pytest.mark.parametrize(
    "added",
    [
        "Do not change curvan to Kervan.",  # 14: the old name typed in lowercase
        "curvan is the old name, Kervan the new one.",  # 14: lowercase, no "don't" clause
        "Do not use Kervan in the captions.",  # 15, 16: Green 4 still types "Kervan" earlier
        "Don’t use Kervan in the captions.",  # 15: the iPhone's apostrophe
        "Kervan altyazıda olmasın.",  # 15: Turkish, the clause rule's own wording
        'Do not write "Kervan" in the captions.',  # 18: one rule for a quoted ask and a name
    ],
)
def test_a_lowercase_or_excluded_name_keeps_the_heard_word(monkeypatch, added):
    """Second review: the cue text and every timed word keep the heard "Curvan" (14-18)."""
    request = FIXTURE["green4_request"].replace("No music.", f"No music. {added}")
    variant = _run(monkeypatch, request=request, binding=FIXTURE["green4_brief_binding"])
    assert "curvan celebrated its 10th year" in _text(variant).casefold(), _text(variant)
    # 17: the timed words are what the phone draws; they keep it too.
    shown = [w["text"] for w in _shown(variant)]
    assert "Curvan" in shown and "Kervan" not in shown, shown


@pytest.mark.parametrize(
    "request_text",
    [
        "Do not change curvan to Kervan.",  # the review's input (a)
        "Do not use Kervan in the captions.",  # the review's input (b)
        "Yorumla episode 9: Kervan x Lumo Pamukkale show. Do not use Kervan in the captions.",
        # Third review (20): mentioned as "Kervan", excluded in lowercase.
        "Yorumla episode 9: Kervan x Lumo Pamukkale show. Do not use kervan in the captions.",
        "Yorumla episode 9: Kervan x Lumo Pamukkale show. Do not use KERVAN in the captions.",
        "Yorumla episode 9: Kervan x Lumo Pamukkale show. kervan'ı altyazıda yazma.",
    ],
)
def test_the_reviews_inputs_change_neither_cue_text_nor_timed_words(request_text):
    """14-17 on the writer's own calls, targets=() as in the review."""
    from app.pipeline.caption_spelling import (
        apply_spelling_to_cues,
        creator_request_names,
        creator_typed_words,
    )

    words = ["Curvan", "celebrated", "Curvan'ın", "Curvanın"]
    cue = {
        "text": " ".join(words),
        "words": [{"text": w, "start_s": float(i), "end_s": i + 0.5} for i, w in enumerate(words)],
    }
    [out] = apply_spelling_to_cues(
        [cue],
        (),
        request_names=creator_request_names(request_text),
        typed=creator_typed_words(request_text),
    )
    assert out["text"] == cue["text"]
    assert out["words"] == cue["words"]


def test_an_excluded_name_is_never_a_request_name():
    """15, 16, 18, 19 on the name list itself."""
    from app.pipeline.caption_spelling import creator_request_names, extract_spelling_asks

    green4 = "Yorumla episode 9: Kervan x Lumo Pamukkale show. No music."
    assert creator_request_names(green4) == ("Kervan", "Pamukkale")  # 19
    assert creator_request_names(f"{green4} Do not use Kervan in the captions.") == ("Pamukkale",)
    quoted = f'{green4} Do not write "Kervan" in the captions.'
    assert extract_spelling_asks(quoted) == ()  # KRI-631, unchanged
    assert creator_request_names(quoted) == ("Pamukkale",)  # 18: the same clause rule
    # 20: the exclusion matches the name in any case.
    lower = f"{green4} Do not use kervan in the captions."
    assert creator_request_names(lower) == ("Pamukkale",)
    # 21: an ordinary word in a "don't" clause drops no name.
    assert creator_request_names(f"{green4} Do not use music.") == ("Kervan", "Pamukkale")


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


def test_a_lowercase_exclusion_keeps_the_heard_word_in_the_writer(monkeypatch):
    """Third review (20) through the real writer: cue text and every timed word."""
    request = FIXTURE["green4_request"].replace(
        "No music.", "No music. Do not use kervan in the captions."
    )
    variant = _run(monkeypatch, request=request, binding=FIXTURE["green4_brief_binding"])
    assert "curvan celebrated its 10th year" in _text(variant).casefold(), _text(variant)
    shown = [w["text"] for w in _shown(variant)]
    assert "Curvan" in shown and "Kervan" not in shown, shown
