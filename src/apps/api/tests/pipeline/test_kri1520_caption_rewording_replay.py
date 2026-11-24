"""KRI-1520: the caption proofreader must not reword what was said (two Turkish prod passes).

Cusco DJI T4 "Altının yolu" (job 91270ec0, parent KRI-1517): the speaker says "Şimdi konuyu
Osmanlı'ya bağlanıyor demiştim." and gpt-4o's proofreading turned it into "... bağlayacağımı
söylemiştim." Cusco DJI T1 "Good Take" (job d988bb99): "bitireceğim" became "getireceğim", a
different verb. The word guard let both through; the capital letters, "İnebahta" -> "İnebahtı"
and the comma fix in the same passes are what the step is for and must still land.

Real: ``correct_caption_cues`` (the legacy proofreader path the phone writers call), its word
alignment, the word guard and the trace event. Replayed: only the provider transport -- the
model returns the exact lines prod recorded (`tests/fixtures/kri1520_caption_rewording/`).

Failure modes this pins (written before the fix):
1. A long real-word rewrite slips through on letter overlap ("bağlanıyor" -> "bağlayacağımı").
2. A different verb with the same ending slips through ("demiştim" -> "söylemiştim").
3. A two-letter real-word swap that changes the first sound slips through ("bitireceğim" ->
   "getireceğim").
4. Tightening the guard drops the good fixes in the same pass (capitals, "İnebahtı", a comma).
5. A refused word moves its neighbours' timings or punctuation.
6. The trace stops saying what was kept and what was refused.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

import app.pipeline.caption_correct as cc
import app.services.pipeline_trace as pipeline_trace

FIXTURE = json.loads(
    (
        Path(__file__).resolve().parents[1]
        / "fixtures"
        / "kri1520_caption_rewording"
        / "incident.json"
    ).read_text(encoding="utf-8")
)
PASSES = FIXTURE["passes"]


def _cue(line: dict[str, Any]) -> dict[str, Any]:
    words = [{"text": text, "start_s": start, "end_s": end} for text, start, end in line["words"]]
    return {
        "text": " ".join(word["text"] for word in words),
        "start_s": words[0]["start_s"],
        "end_s": words[-1]["end_s"],
        "words": words,
    }


def _replay(monkeypatch, passage: dict[str, Any]) -> tuple[list[dict], list[dict], dict]:
    cues = [_cue(line) for line in passage["lines"]]
    recorded = [line["model_line"] for line in passage["lines"]]
    monkeypatch.setattr(
        cc, "_llm_correct_lines", lambda texts, language, *, model, **kwargs: list(recorded)
    )
    events: list[tuple] = []
    monkeypatch.setattr(pipeline_trace, "record_pipeline_event", lambda *a: events.append(a))
    out = cc.correct_caption_cues(cues, passage["language"])
    [(stage, name, payload)] = events
    assert (stage, name) == ("captions", "caption_correction")
    return cues, out, payload


@pytest.mark.parametrize("passage", PASSES, ids=lambda passage: passage["id"])
def test_kri1520_prod_pass_keeps_what_was_said_and_lands_the_real_fixes(
    monkeypatch, passage
) -> None:
    cues, out, _payload = _replay(monkeypatch, passage)

    assert [cue["text"] for cue in out] == [line["expected"] for line in passage["lines"]]
    for before, after in zip(cues, out):
        # Every word keeps its own time, whether its change landed or was refused.
        assert [(w["start_s"], w["end_s"]) for w in after["words"]] == [
            (w["start_s"], w["end_s"]) for w in before["words"]
        ]
        assert " ".join(w["text"] for w in after["words"]) == after["text"]


def test_kri1520_t4_caption_reads_what_the_voiceover_says(monkeypatch) -> None:
    """Against the independent transcripts, not whisper-1: the first caption is the voice."""
    [passage] = [p for p in PASSES if p["id"] == "t4_altinin_yolu"]
    _cues, out, _payload = _replay(monkeypatch, passage)
    first = passage["lines"][0]
    assert out[0]["text"] == first["spoken"]
    assert out[0]["text"] != first["prod_shown"]


@pytest.mark.parametrize(
    ("passage_id", "expected"),
    [
        # Prod: changed 4, accepted 5, reverted 1 word / 1 line. The two reworded words are
        # now refused, so their line stays as spoken and counts as a refused line.
        (
            "t4_altinin_yolu",
            {"changed": 3, "accepted_words": 3, "reverted_words": 3, "reverted_lines": 2},
        ),
        # Prod: changed 2, accepted 2, reverted 3 words / 2 lines; "getireceğim" now refused.
        (
            "t1_good_take",
            {"changed": 1, "accepted_words": 1, "reverted_words": 4, "reverted_lines": 3},
        ),
    ],
)
def test_kri1520_trace_says_what_was_kept_and_what_was_refused(
    monkeypatch, passage_id: str, expected: dict[str, int]
) -> None:
    [passage] = [p for p in PASSES if p["id"] == passage_id]
    _cues, _out, payload = _replay(monkeypatch, passage)
    assert {key: payload[key] for key in expected} == expected
    refused = {example["before"] for example in payload["reverted_examples"]}
    for line in passage["lines"]:
        if line["expected"] != line["model_line"]:
            assert _cue(line)["text"] in refused
