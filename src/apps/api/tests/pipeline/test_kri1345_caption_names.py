"""KRI-1345: captions spell the creator's names their way, for short names and names from the
job's own media (photo text, photo brand labels, file names).

Failure modes written before the code (see the KRI-1345 notes):

1. a common word turned into a brand ("villa" -> "Vela");
2. a different real name rewritten ("Aston Villa" -> "Aston Vela");
3. media labels polluted by the audio (a talking video's brand list holding the mishearing);
4. OCR noise / camera file names becoming names;
5. ambiguity (one heard word, two names);
6. a word the creator typed themselves respelled;
7. timings moved;
8. Turkish case endings lost;
9. no casing from whisper (only Gemini's hearing writes the name);
10. "don't" clauses read as names;
11. flag off not byte-identical.

The yorum3 case runs the real phone Talking caption steps on the real hearing of that take.
"""

from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from app.pipeline.caption_names import (
    apply_name_vocabulary,
    media_names,
    name_vocabulary,
    short_request_names,
)

_FIXTURE = json.loads(
    (Path(__file__).parent / "fixtures" / "kri1345_yorum3_hearing.json").read_text("utf-8")
)


def _cue(text: str, start: float = 0.0) -> dict:
    words = []
    t = start
    for token in text.split():
        words.append({"text": token, "start_s": round(t, 3), "end_s": round(t + 0.3, 3)})
        t += 0.35
    return {"text": text, "start_s": start, "end_s": round(t, 3), "words": words}


def _texts(cues: list[dict]) -> list[str]:
    return [c["text"] for c in cues]


def _timings(cues: list[dict]) -> list:
    return [
        (c["start_s"], c["end_s"], [(w["start_s"], w["end_s"]) for w in c.get("words") or []])
        for c in cues
    ]


def _word_texts(cues: list[dict]) -> list[str]:
    return [w["text"] for c in cues for w in c.get("words") or []]


# --- yorum3, end to end through the phone Talking caption steps ------------------------------


def _yorum3_caption_text(monkeypatch, *, enabled: bool) -> list[dict]:
    from app.config import settings
    from app.pipeline.captions import build_plain_cues
    from app.pipeline.transcribe import Word
    from app.tasks import generative_build

    monkeypatch.setattr(settings, "caption_name_vocabulary_enabled", enabled)
    # The correction model is a paid call; the yorum3 trace shows it never touched "Vila".
    monkeypatch.setattr(settings, "subtitled_caption_correction_enabled", False)
    words = [
        Word(text=w["text"], start_s=w["start_s"], end_s=w["end_s"], confidence=1.0)
        for w in _FIXTURE["whisper_words"]
    ]
    cues = build_plain_cues(words, attach_words=True)
    out, _receipt = generative_build._phone_talking_caption_text(
        cues,
        detected_lang="en",
        snapshot={},
        all_candidates={"creator_request": _FIXTURE["prompt"]},
        cut_plan=None,
        clip_metas=[SimpleNamespace(clip_id="talk", transcript=_FIXTURE["gemini_transcript"])],
    )
    return out


def test_yorum3_captions_spell_vela_like_the_prompt(monkeypatch):
    before = _yorum3_caption_text(monkeypatch, enabled=False)
    after = _yorum3_caption_text(monkeypatch, enabled=True)
    joined_before = " ".join(_texts(before))
    joined_after = " ".join(_texts(after))
    assert joined_before.count("Vila") == 2  # the incident, reproduced
    assert "Vila" not in joined_after
    assert joined_after.count("Vela") == 2
    # Nothing else changes: same words but the two names, same timings.
    diff = [(a, b) for a, b in zip(_word_texts(before), _word_texts(after), strict=True) if a != b]
    assert diff == [("Vila", "Vela"), ("Vila", "Vela")]
    assert _timings(before) == _timings(after)


def test_yorum3_vocabulary_holds_the_typed_short_names():
    assert short_request_names(_FIXTURE["prompt"]) == ("Vela", "Lumo")


# --- the narrow rule ------------------------------------------------------------------------


def test_common_word_is_never_a_brand():
    cues = [_cue("We stayed in a villa by the sea."), _cue("Villa prices went up again.", 3)]
    out, changed = apply_name_vocabulary(cues, ("Vela",))
    assert out == cues and changed == {}


def test_another_real_name_is_left_alone():
    cues = [_cue("We watched Aston Villa play last night."), _cue("Then Villa scored.", 3)]
    out, changed = apply_name_vocabulary(cues, ("Vela",))
    assert out == cues and changed == {}


def test_a_word_the_creator_typed_is_never_respelled():
    cues = [_cue("I compared Vela with Vila today.")]
    out, _ = apply_name_vocabulary(cues, ("Vela",), protected=("Vela", "Vila"))
    assert out == cues


def test_one_heard_word_two_names_is_left_alone():
    cues = [_cue("Yesterday Vila posted again.")]
    out, changed = apply_name_vocabulary(cues, ("Vela", "Vola"))
    assert out == cues and changed == {}


@pytest.mark.parametrize("heard", ["Bela", "Vilo", "Velas", "Vel"])
def test_short_names_only_take_one_vowel(heard):
    cues = [_cue(f"Yesterday {heard} posted again.")]
    out, _ = apply_name_vocabulary(cues, ("Vela",))
    assert out == cues


def test_turkish_ending_is_kept():
    cues = [_cue("Dün Vila'nın paylaşımını gördüm.")]
    out, changed = apply_name_vocabulary(cues, ("Vela",))
    assert _texts(out) == ["Dün Vela'nın paylaşımını gördüm."]
    assert _word_texts(out)[1] == "Vela'nın"
    assert _timings(out) == _timings(cues)
    assert changed == {"vila": "Vela"}


def test_gemini_hearing_is_enough_evidence_when_whisper_has_no_case():
    cues = [_cue("ten minutes after lumo launched, vila posted on instagram")]
    reference = "Ten minutes after Lumo launched, Vila posted on Instagram."
    out, _ = apply_name_vocabulary(cues, ("Vela",), references=(reference,))
    assert _texts(out) == ["ten minutes after lumo launched, Vela posted on instagram"]


def test_no_evidence_no_change():
    cues = [_cue("ten minutes after lumo launched, vila posted on instagram")]
    out, _ = apply_name_vocabulary(cues, ("Vela",))
    assert out == cues


def test_dont_clause_name_is_not_a_target():
    assert short_request_names("Make a video. Don't write Vela in the captions.") == ()
    assert short_request_names("Make a video about Vela. Don't write vela in lowercase.") == ()


# --- names from the job's own media ---------------------------------------------------------


def test_photo_text_and_file_names_feed_the_vocabulary():
    names = media_names(
        on_screen_texts=("KERVAN x LUMO PAMUKKALE SHOW", "Grand opening this Friday"),
        photo_brands=("Kervan",),
        filenames=(
            "2D5D6643-E924-4F6D-8E30-011D7404616B-IMG_7212.MOV",
            "IMG_0042.HEIC",
            "Screenshot 2026-10-30 at 13.05.00.png",
            "kervan_billboard.png",
        ),
    )
    assert "Kervan" in names and "Lumo" in names and "Pamukkale" in names
    assert not {"Img", "Mov", "Heic", "Screenshot", "Show", "Friday", "This"} & set(names)


def test_photo_name_respells_a_mishearing():
    cues = [_cue("Last week the Curvan show came to town."), _cue("Curvanın stand was huge.", 3)]
    out, _ = apply_name_vocabulary(cues, ("Kervan",))
    assert _texts(out) == ["Last week the Kervan show came to town.", "Kervan'ın stand was huge."]
    assert _timings(out) == _timings(cues)


def test_talking_video_brands_never_become_names():
    rows = [
        {
            "kind": "video",
            "source_filename": "IMG_1.MOV",
            "analysis": {"understanding": {"kind": "video", "brands": ["Vila"]}},
        },
    ]
    vocab = name_vocabulary("Talk about the launch.", rows)
    assert "Vila" not in vocab.names


def test_media_name_that_sounds_like_a_typed_name_is_dropped():
    rows = [
        {
            "kind": "image",
            "source_filename": "post.png",
            "analysis": {"source": "image_metadata", "on_screen_text": "VILA new drop"},
        }
    ]
    vocab = name_vocabulary("Episode 8: Vela's reply to Lumo.", rows)
    assert vocab.short_names == ("Vela", "Lumo")
    assert "Vila" not in vocab.names


def test_a_name_after_it_with_a_turkish_ending_does_not_block():
    cues = [_cue("Lumo telefonunu çıkardı, on dakika sonra Vila Instagram'da paylaştı.")]
    out, _ = apply_name_vocabulary(cues, ("Vela", "Lumo"))
    assert _texts(out) == ["Lumo telefonunu çıkardı, on dakika sonra Vela Instagram'da paylaştı."]
    blocked = [_cue("We drove past Villa Park yesterday.")]
    assert apply_name_vocabulary(blocked, ("Vela",))[0] == blocked


# --- phone Narrated, Turkish ----------------------------------------------------------------


def test_narrated_turkish_captions_take_the_typed_names(monkeypatch):
    from app.config import settings
    from app.tasks import generative_build

    monkeypatch.setattr(settings, "caption_name_vocabulary_enabled", True)
    prompt = (
        "Yorumla bölüm 9: Vela'nın Lumo'ya cevabı ve Kervan reklamı. Seslendirmemi kullan, "
        "altyazılar küçük olsun."
    )
    cues = [
        _cue("Lumo telefonunu çıkardıktan on dakika sonra Vila Instagram'da paylaştı."),
        _cue("Sonra Curvan'ın reklamı geldi, villa tatili gibi bir şey.", 4),
    ]
    out = generative_build._narrated_caption_names(
        cues, snapshot={}, all_candidates={"creator_request": prompt}, job_id=None
    )
    assert _texts(out) == [
        "Lumo telefonunu çıkardıktan on dakika sonra Vela Instagram'da paylaştı.",
        "Sonra Kervan'ın reklamı geldi, villa tatili gibi bir şey.",
    ]
    assert _timings(out) == _timings(cues)


def test_same_sound_other_letters_is_respelled_after_a_turkish_adjective():
    # Ecuador N1 (whisper.cpp): "Caro" typed, "Karo" heard; "Ekvadorlu" (Ecuadorian) is an
    # adjective, not part of her name.
    cues = [
        _cue("Bizi Ekvadorlu Karo karşıladı."),
        _cue("Kahvesi de Kolombiya'dan. Karo bütün hafta oradaydı.", 3),
    ]
    out, changed = apply_name_vocabulary(cues, ("Caro",))
    assert _texts(out) == [
        "Bizi Ekvadorlu Caro karşıladı.",
        "Kahvesi de Kolombiya'dan. Caro bütün hafta oradaydı.",
    ]
    assert changed == {"karo": "Caro"}


# --- review (Emir, on 93e22977e): a two-word name that opens a sentence ----------------------


@pytest.mark.parametrize(
    "text",
    [
        "Mila Kunis starred. Later Mila returned.",  # the name opens the sentence
        "Aston Villa scored first. Later Villa won.",  # the other word opens it
        "Later Mila returned. Mila Kunis starred.",  # the compound comes second
    ],
)
def test_a_two_word_name_at_a_sentence_start_blocks_the_word_everywhere(text):
    cues = [_cue(text)]
    names = ("Mela",) if "Mila" in text else ("Vela",)
    out, changed = apply_name_vocabulary(cues, names)
    assert out == cues and changed == {}


def test_a_plain_sentence_opener_does_not_block_the_name():
    # "Then" opens the sentence and is an ordinary word, not part of her name.
    cues = [_cue("Then Vila posted again. Yesterday Vila replied.")]
    out, _ = apply_name_vocabulary(cues, ("Vela",))
    assert _texts(out) == ["Then Vela posted again. Yesterday Vela replied."]
