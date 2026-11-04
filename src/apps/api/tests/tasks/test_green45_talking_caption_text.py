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
"""

from __future__ import annotations

import copy
import json
from pathlib import Path

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
