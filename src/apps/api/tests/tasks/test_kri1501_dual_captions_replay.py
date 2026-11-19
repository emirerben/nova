"""KRI-1501 replay: two-language captions through the real phone Talking job.

A creator asks for "Turkish captions with English under it". The spoken line keeps the words
she says (with its word highlight) and a smaller translated line shows under it for the same
stretch of speech (docs/pipelines/dual-captions.md).

Failure modes, written before the code (each is a test below; the compile and editor ones
live in tests/pipeline/test_kri1501_translated_caption_layers.py and
tests/routes/test_kri1501_dual_captions_editor.py):

 1. Gate off (switch off, device not verified, or an app build below the protocol): the
    spoken captions, the persisted cues and every compiled text layer are byte-identical to a
    job without the ask; the variant says honestly the translated line is not drawn
    (``caption_translation.outcome == "unmet"``) and no model is called.          [gate_off]
 2. The ask routes through the KRI-838 replace-words translation: the SPOKEN line comes out in
    English. The spoken cue texts must equal the no-ask run's, word for word.   [delivered]
 3. A translation is attached to a sentence chunk instead of the final cue: a cue's line no
    longer matches its own text/time. One line per FINAL cue, in cue order.     [delivered]
 4. A line the model answered in the wrong language (the Turkish handed back) is drawn: it
    must be dropped (no ``translation`` on that cue, no layer), ``outcome`` "partial", and its
    cue id listed in ``dropped_cue_ids``.                                       [wrong_line]
 5. The whole answer is another language: no line at all, ``outcome`` "unmet".  [wrong_answer]
 6. The provider is down: the job still renders, spoken captions unchanged, "unmet".  [down]
 7. "English captions with English under it" on English speech (nothing to translate into):
    "unmet" with a reason, no model call.                                       [same_lang]
 8. The persisted variant does not carry what the editor needs: ``caption_translation`` with
    language, source, outcome, and every cue carrying an id.                   [delivered]
 9. The recipe draws no translated layer, or one per piece of a split cue: exactly one
    ``caption-tr-<i>`` layer per translated cue, spanning that cue's time.      [delivered]

Whisper and the translator are stubbed (authored answers, never a model output); the
dispatcher's parser, the real phone Talking job, the cue builder, the caption compiler and the
recipe are the real ones. Turkish speech: ``tests/fixtures/kri632_t1_talking_words.json``.
"""

from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import Mock

import pytest

from app.pipeline.transcribe import Word
from app.services.client_protocol import DUAL_CAPTIONS_PROTOCOL
from app.services.device_render import device_status
from app.tasks import generative_build as gb
from tests.tasks.test_phone_subtitled_narrated_dispatch import _setup_subtitled

FIXTURES = Path(__file__).resolve().parents[1] / "fixtures"
TR_WORDS = [
    Word(text=w["text"], start_s=w["start_s"], end_s=w["end_s"], confidence=0.95)
    for w in json.loads((FIXTURES / "kri632_t1_talking_words.json").read_text(encoding="utf-8"))
]
DURATION = 38.0
ASK = "Turkish captions with English under it"

# Authored English subtitle lines the stub hands back, one per caption line in order (cycled).
ENGLISH = [
    "Yes, boss. Have you started getting ready?",
    "Come on, we are leaving soon.",
    "Do you want to say something to our subscribers?",
    "Stay tuned, and do not forget the like button.",
    "Honestly, I think that is really cringe.",
    "Do not press it if you do not want to, brother.",
    "If I say subscribe and like in every video, we should not do this at all.",
    "Do you get it? Whoever wants to subscribes, whoever does not, does not.",
    "We are trying to build a community with these people.",
    "Let us have a thousand regular viewers who love us.",
    "Let them chase the truth. That is enough for us.",
    "Is it not enough? Yes, it is.",
    "We could even call the community Loy Tree.",
    "We could. Have a good evening, everyone.",
]


def _english(lines, **_kwargs):
    return [ENGLISH[index % len(ENGLISH)] for index in range(len(lines))]


@pytest.fixture
def events(monkeypatch) -> Mock:
    from app.services import pipeline_trace

    recorded = Mock()
    monkeypatch.setattr(pipeline_trace, "record_pipeline_event", recorded)
    return recorded


def _gate(monkeypatch, *, on: bool = True, verified: bool = True) -> None:
    monkeypatch.setattr(gb.settings, "dual_captions_enabled", on)
    features = [f for f in gb.settings.phone_render_verified_features if f != "dualCaptions"]
    monkeypatch.setattr(
        gb.settings,
        "phone_render_verified_features",
        [*features, *(["dualCaptions"] if verified else [])],
    )


def _render(
    monkeypatch,
    *,
    prompt: str | None = ASK,
    protocol: int = DUAL_CAPTIONS_PROTOCOL,
    words=TR_WORDS,
    on: bool = True,
    verified: bool = True,
):
    """The real phone Talking job for ``prompt``; returns (variant, recipe)."""
    job, snapshot, _session, _binding = _setup_subtitled(
        monkeypatch, transcript_words=list(words), duration_s=DURATION
    )
    import app.pipeline.transcribe as transcribe_mod
    from app.pipeline.transcribe import Transcript

    # whisper's auto pass on this take reports Turkish.
    monkeypatch.setattr(
        transcribe_mod,
        "transcribe_whisper_cached",
        lambda *a, **k: Transcript(words=list(words), language="tr"),
        raising=False,
    )
    _gate(monkeypatch, on=on, verified=verified)
    job.all_candidates["language"] = "tr"
    job.all_candidates["creator_client_protocol"] = protocol
    if prompt is not None:
        # What the real dispatcher persists for the creator's words.
        job.all_candidates.update(_dispatched(prompt))
    gb._run_phone_subtitled_job(str(job.id), snapshot, job.all_candidates, ownership_epoch=3)
    [variant] = job.assembly_plan["variants"]
    return variant, device_status(job, "subtitled").request.recipe


def _spoken_layers(recipe) -> list:
    return [layer for layer in recipe.text_layers if "-tr-" not in layer.id]


def _translated_layers(recipe) -> list:
    return [layer for layer in recipe.text_layers if "-tr-" in layer.id]


def _strip_ids(cues: list[dict]) -> list[dict]:
    return [{k: v for k, v in cue.items() if k not in {"id", "translation"}} for cue in cues]


def _dispatched(prompt: str) -> dict:
    """The caption keys `build_generative_job` (the dispatcher) writes for ``prompt``."""
    import uuid

    from app.services.generative_jobs import build_generative_job

    job = build_generative_job(
        user_id=uuid.uuid4(), clip_paths=["music-uploads/test.mp4"], creator_request=prompt
    )
    return {
        key: value
        for key, value in (job.all_candidates or {}).items()
        if key in {"creator_request", "caption_language_request", "caption_translation_request"}
    }


@pytest.mark.parametrize(
    ("prompt", "codes"),
    [
        (ASK, ["en", "tr"]),
        ("altyazının altına İngilizce çeviri", ["en"]),
        ("zweisprachige Untertitel, Englisch darunter", ["en"]),
    ],
)
def test_the_dispatcher_persists_the_ask_and_never_a_spoken_language_override(prompt, codes):
    dispatched = _dispatched(prompt)
    assert dispatched["caption_translation_request"] == codes
    assert "caption_language_request" not in dispatched


def test_a_plain_language_ask_is_dispatched_as_before():
    dispatched = _dispatched("English captions please")
    assert dispatched.get("caption_language_request") == "en"
    assert "caption_translation_request" not in dispatched


# ------------------------------------------------------------------------------ [delivered]


def test_delivered_spoken_line_kept_and_one_translated_layer_per_cue(monkeypatch, events):
    translator = Mock(side_effect=_english)
    monkeypatch.setattr("app.pipeline.caption_translate._llm_translate_lines", translator)
    baseline, baseline_recipe = _render(monkeypatch, prompt=None, on=False)
    variant, recipe = _render(monkeypatch)

    # 2: the spoken words are the no-ask run's, word for word (never translated).
    assert _strip_ids(variant["caption_cues"]) == _strip_ids(baseline["caption_cues"])
    assert variant["caption_language"] == baseline["caption_language"] == "tr"
    # One call, one line per FINAL cue text, each with its reading-speed budget.
    assert variant.get("caption_translation", {}).get("outcome") != "unmet", variant.get(
        "caption_translation"
    )
    translator.assert_called_once()
    sent = translator.call_args.args[0]
    assert sent == [cue["text"] for cue in variant["caption_cues"]]
    budgets = translator.call_args.kwargs["budgets"]
    assert len(budgets) == len(sent) and all(1 <= b <= 42 for b in budgets)
    assert translator.call_args.kwargs["target_language"] == "English"
    # 3/8: every cue carries an id and its own translated line, in order.
    cues = variant["caption_cues"]
    assert all(isinstance(cue.get("id"), str) for cue in cues)
    assert [cue["translation"] for cue in cues] == _english(sent)
    document = variant["caption_translation"]
    assert document["enabled"] is True
    assert document["language"] == "en" and document["source_language"] == "tr"
    assert document["outcome"] == "translated" and document["dropped_cue_ids"] == []
    assert document["color"] == "#F5D33C"
    # 9: one translated layer per cue, spanning exactly that cue (no split pieces).
    translated = _translated_layers(recipe)
    not_drawn = [
        call.args[2]
        for call in events.call_args_list
        if call.args[1] == "caption_translation_lines_not_drawn"
    ]
    assert not not_drawn, not_drawn
    assert len(translated) == len(cues)
    assert [layer.id for layer in translated] == [f"caption-tr-{i}" for i in range(len(cues))]
    for layer, cue in zip(translated, cues, strict=True):
        assert " ".join(run.text for run in layer.runs).split() == cue["translation"].split()
        assert len({run.baseline_y for run in layer.runs}) == 1, "one row, never wrapped"
        assert layer.start == pytest.approx(cue["start_s"], abs=1e-3)
        assert layer.end == pytest.approx(cue["end_s"], abs=1e-3)
        assert layer.effect != "karaoke-line"
    # The spoken layers are the same layers the no-ask run compiled.
    assert [layer.model_dump() for layer in _spoken_layers(recipe)] == [
        layer.model_dump() for layer in baseline_recipe.text_layers
    ]


# ------------------------------------------------------------------------------- [gate_off]


@pytest.mark.parametrize(
    ("on", "verified", "protocol"),
    [
        (False, True, DUAL_CAPTIONS_PROTOCOL),
        (True, False, DUAL_CAPTIONS_PROTOCOL),
        (True, True, DUAL_CAPTIONS_PROTOCOL - 1),
    ],
    ids=["switch_off", "not_verified", "old_build"],
)
def test_gate_off_is_byte_identical_and_honest(monkeypatch, events, on, verified, protocol):
    translator = Mock(side_effect=AssertionError("no model call with the gate off"))
    monkeypatch.setattr("app.pipeline.caption_translate._llm_translate_lines", translator)
    gate = {"on": on, "verified": verified, "protocol": protocol}
    baseline, baseline_recipe = _render(monkeypatch, prompt=None, **gate)
    variant, recipe = _render(monkeypatch, **gate)

    assert variant["caption_cues"] == baseline["caption_cues"]
    assert recipe.text_layers == baseline_recipe.text_layers
    assert variant["caption_translation"]["outcome"] == "unmet"
    assert variant["caption_translation"]["reason"] == "dual_captions_unavailable"
    assert "caption_translation" not in baseline
    translator.assert_not_called()


# ----------------------------------------------------------------------------- [wrong_line]


def test_a_line_answered_in_the_wrong_language_is_dropped_never_drawn(monkeypatch, events):
    def answer(lines, **_kwargs):
        out = _english(lines)
        longest = max(range(len(lines)), key=lambda index: len(lines[index]))
        out[longest] = lines[longest]  # the Turkish handed back for this one line
        return out

    monkeypatch.setattr(
        "app.pipeline.caption_translate._llm_translate_lines", Mock(side_effect=answer)
    )
    variant, recipe = _render(monkeypatch)

    cues = variant["caption_cues"]
    longest = max(range(len(cues)), key=lambda index: len(cues[index]["text"]))
    assert "translation" not in cues[longest]
    assert variant["caption_translation"]["outcome"] == "partial"
    assert variant["caption_translation"]["dropped_cue_ids"] == [cues[longest]["id"]]
    drawn = " ".join(run.text for layer in _translated_layers(recipe) for run in layer.runs)
    assert cues[longest]["text"].split()[0] not in drawn.split()
    assert f"caption-tr-{longest}" not in {layer.id for layer in recipe.text_layers}
    assert len(_translated_layers(recipe)) == len(cues) - 1


# --------------------------------------------------------------------------- [wrong_answer]


def test_an_answer_in_another_language_altogether_draws_nothing(monkeypatch, events):
    monkeypatch.setattr(
        "app.pipeline.caption_translate._llm_translate_lines",
        Mock(side_effect=lambda lines, **_kw: list(lines)),
    )
    variant, recipe = _render(monkeypatch)

    assert all("translation" not in cue for cue in variant["caption_cues"])
    assert variant["caption_translation"]["outcome"] == "unmet"
    assert not _translated_layers(recipe)


# ----------------------------------------------------------------------------------- [down]


def test_provider_down_renders_the_spoken_captions_alone(monkeypatch, events):
    baseline, baseline_recipe = _render(monkeypatch, prompt=None, on=False)
    # conftest's guard makes the provider call raise: exactly the provider-down path.
    variant, recipe = _render(monkeypatch)

    assert _strip_ids(variant["caption_cues"]) == _strip_ids(baseline["caption_cues"])
    assert all("translation" not in cue for cue in variant["caption_cues"])
    assert variant["caption_translation"] == {
        "outcome": "unmet",
        "reason": "translation_failed",
        "requested": variant["caption_translation"]["requested"],
        "source_language": "tr",
        "language": "en",
    }
    assert [layer.model_dump() for layer in recipe.text_layers] == [
        layer.model_dump() for layer in baseline_recipe.text_layers
    ]


# ------------------------------------------------------------------------------ [same_lang]


def test_nothing_to_translate_into_is_unmet_without_a_model_call(monkeypatch, events):
    """7: a Turkish translation under Turkish speech: nothing to translate into."""
    translator = Mock(side_effect=AssertionError("nothing to translate into"))
    monkeypatch.setattr("app.pipeline.caption_translate._llm_translate_lines", translator)
    variant, recipe = _render(monkeypatch, prompt="add a Turkish translation under the captions")

    assert variant["caption_translation"]["outcome"] == "unmet"
    assert variant["caption_translation"]["reason"] == "no_translation_language"
    assert all("translation" not in cue for cue in variant["caption_cues"])
    assert not _translated_layers(recipe)
    translator.assert_not_called()


def test_a_german_take_keeps_german_and_gets_an_english_line(monkeypatch, events):
    """The real German take (de1, `null zu null`): the spoken line stays German, the English
    line is asked for with German named as the source."""
    golden = json.loads(
        (
            FIXTURES / "agent_evals" / "spoken_cue_resolver" / "golden" / "de1_null_zu_null.json"
        ).read_text(encoding="utf-8")
    )
    de_words = [
        Word(text=w["text"], start_s=w["start_s"], end_s=w["end_s"], confidence=0.95)
        for w in golden["input"]["words"]
    ]
    translator = Mock(side_effect=_english)
    monkeypatch.setattr("app.pipeline.caption_translate._llm_translate_lines", translator)
    variant, recipe = _render(
        monkeypatch, prompt="zweisprachige Untertitel, Englisch darunter", words=de_words
    )

    assert translator.call_args.kwargs["source_language"] == "German"
    assert variant["caption_translation"]["source_language"] == "de"
    assert variant["caption_translation"]["language"] == "en"
    assert len(_translated_layers(recipe)) == sum(
        1 for cue in variant["caption_cues"] if cue.get("translation")
    )
