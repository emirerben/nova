"""KRI-793 at render: the phone Talking writer spells the brief's listed words its way.

Café kit T1 (prod thread 32A5B608, 2026-10-21). The approved brief asks "Captions for both
voices, ensuring correct spellings: Oblea de la Casa, dulce de leche, leche condensada,
Merope". Two hearings of the same take got two of those words wrong: the transcript agent
wrote "Ole de la Casa", the clip analysis "Meropa". On main the caption spelling step read
only quoted single words from the raw request (KRI-631), so both would have shipped.

This runs the real writer (`_run_phone_subtitled_job`) on the take's real words with the
real brief pinned on the job, then judges the finished variant the way the render-ready
review does. Failure modes 12-18 of `tests/kria/test_kri793_caption_spellings.py`; no model
calls (caption correction is a local fake).
"""

from __future__ import annotations

import json
import uuid
from pathlib import Path

import app.pipeline.caption_correct as caption_correct_mod
from app.config import settings
from app.kria.brief import CreativeBrief
from app.kria.brief_binding import BriefBinding
from app.kria.brief_checks import build_receipts, plan_facts_from_phone_variant
from app.pipeline.transcribe import Word
from app.tasks import generative_build as gb
from tests.tasks.test_phone_subtitled_narrated_dispatch import _setup_subtitled

FIXTURE = json.loads(
    (
        Path(__file__).resolve().parents[1]
        / "fixtures"
        / "kri793_t1_caro"
        / "t1_brief_and_speech.json"
    ).read_text(encoding="utf-8")
)
T1_BRIEF = CreativeBrief.model_validate(FIXTURE["brief"])
R5 = next(req for req in T1_BRIEF.requirements if req.id == "r5")
DURATION = FIXTURE["take_duration_s"]


def _take_words() -> list[Word]:
    """The take as one hearing: the transcript agent's real timed words up to 29.35 s
    ("Ole de la Casa"), then the analysis segments' words, spread evenly over each segment,
    with the first "Merope" as the analysis transcript heard it ("Meropa")."""
    words = [
        Word(text=w["text"], start_s=w["start_s"], end_s=w["end_s"], confidence=1.0)
        for w in FIXTURE["asr_words"]
    ]
    last = words[-1].end_s
    for segment in FIXTURE["segments"]:
        if segment["start_s"] < last:
            continue
        text = segment["text"]
        if segment["start_s"] == 36.9:  # "...is Merope, how are you going to..."
            assert "Meropa" in FIXTURE["full_transcript"]
            text = text.replace("Merope", "Meropa")
        tokens = text.split()
        step = (segment["end_s"] - segment["start_s"]) / len(tokens)
        words.extend(
            Word(
                text=token,
                start_s=round(segment["start_s"] + i * step, 3),
                end_s=round(segment["start_s"] + (i + 1) * step, 3),
                confidence=1.0,
            )
            for i, token in enumerate(tokens)
        )
    return words


def _mangling_correction(monkeypatch) -> list[list[str]]:
    """Caption correction that writes "Merope" the other way: proves the creator's spelling
    is applied after it (failure mode 15), and that no model is called."""
    seen: list[list[str]] = []

    def correct(texts, _language, *, model):
        seen.append(list(texts))
        return [text.replace("Merope", "Meropa") for text in texts]

    monkeypatch.setattr(caption_correct_mod, "_llm_correct_lines", correct)
    monkeypatch.setattr(settings, "subtitled_caption_correction_enabled", True)
    return seen


def _t1_job(monkeypatch, *, binding: dict | None):
    job, snapshot, _session, _source = _setup_subtitled(
        monkeypatch, duration_s=DURATION, transcript_words=_take_words()
    )
    job.all_candidates.update(
        {
            "creator_request": FIXTURE["creator_request"],
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
    return job, snapshot


def _run(job, snapshot) -> dict:
    gb._run_phone_subtitled_job(str(job.id), snapshot, job.all_candidates, ownership_epoch=3)
    assert job.status == "awaiting_device", (job.status, job.error_detail)
    return job.assembly_plan["variants"][0]


def _caption_text(variant: dict) -> str:
    return " ".join(cue["text"] for cue in variant["caption_cues"])


def _cue_words(variant: dict) -> list[dict]:
    return [word for cue in variant["caption_cues"] for word in cue.get("words") or []]


def test_t1_captions_spell_the_listed_words_and_the_review_says_done(monkeypatch):
    binding = BriefBinding.create(uuid.uuid4(), T1_BRIEF).model_dump(mode="json")
    job, snapshot = _t1_job(monkeypatch, binding=binding)
    corrected = _mangling_correction(monkeypatch)  # after the harness turns it off

    variant = _run(job, snapshot)

    assert corrected, "caption correction ran before the spelling step"
    text = _caption_text(variant)
    # 12, 13: the several-word name and the one-word name both arrive and are applied.
    assert "Ole de la Casa" not in text and "Oblea de la Casa" in text
    assert "Meropa" not in text and text.count("Merope") == 4
    assert "dulce de leche" in text and "leche condensada" in text
    # 14: every word keeps its own timing; nothing is added or dropped.
    spoken = _take_words()
    shown = _cue_words(variant)
    assert [(w["start_s"], w["end_s"]) for w in shown] == [(w.start_s, w.end_s) for w in spoken]
    changed = {
        (heard.text, w["text"])
        for heard, w in zip(spoken, shown, strict=True)
        if heard.text != w["text"]
    }
    assert changed == {("Ole", "Oblea"), ("Meropa,", "Merope,")}
    # 17: the render-ready review reads the finished captions and finds every word right.
    [receipt] = build_receipts([R5], plan_facts_from_phone_variant(variant), include_unchecked=True)
    assert (receipt.status, receipt.verification) == ("met", "checked")
    assert receipt.reason == (
        "Oblea de la Casa, dulce de leche, leche condensada and Merope are spelled as you "
        "wrote them."
    )


def test_an_unreadable_binding_renders_with_the_quoted_asks_only(monkeypatch):
    """16: a binding that fails its own digest check adds no words, never a failed job."""
    binding = BriefBinding.create(uuid.uuid4(), T1_BRIEF).model_dump(mode="json")
    binding["digest"] = "0" * 64
    job, snapshot = _t1_job(monkeypatch, binding=binding)
    _mangling_correction(monkeypatch)

    variant = _run(job, snapshot)

    text = _caption_text(variant)
    # As on main: nothing listed reaches the captions, and correction's spelling stays.
    assert "Ole de la Casa" in text and "Meropa" in text
