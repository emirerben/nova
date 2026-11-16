"""KRI-1501: the translated caption line as the phone compiler draws it, and the translator.

Contract: docs/pipelines/dual-captions.md ("Layout rules", "Translation step").

Failure modes, written before the code (each is a test below):

 1. Gate off (switch off or device not verified), or no translated track: a cue carrying a
    ``translation`` compiles byte-identically to the same cue without one.        [gate_off]
 2. The translated line wraps to two rows or is cut off: a German line longer than the frame
    must shrink to fit one row (never below 24 px), and a line still too wide at 24 px is
    dropped, never drawn cut.                                                   [german]
 3. The spoken cue is split in time to clear the watermark and the translated line splits
    with it (two layers, or one that covers only half the speech): exactly ONE translated
    layer, ``caption-tr-<i>``, spanning the original cue's time.                [split]
 4. The translated line covers a face: attached below the spoken line onto a face it flips
    above; with a face both above and below it is not drawn at all.             [face]
 5. The translated line sits on the spoken line, or not under it: attached, its ink starts
    0.25 x its size below the spoken ink, centred under it.                     [attached]
 6. A detached position / one line's own override is ignored.                   [detached]
 7. The track switch (``enabled: false``) still draws.                          [switch]
 8. The translator: a wrong-language line is drawn; the line budget is not told to the model;
    a provider failure raises; the call is not deterministic JSON.              [translator]
 9. (Review round) A short line handed back exactly as spoken ("Evet." under "Evet.") is too
    short for the language-ID and is drawn as its own "translation". It must be dropped and
    listed like a wrong-language line; a line that is only names from the glossary or
    numbers stays.                                                              [echo]

Spoken speech: en ``tests/fixtures/kri720_cigdem_take_a_words.json`` (Çiğdem take A) and the
prod Kadıköy caption (a cue the watermark splits in time). Translations are authored stubs.
"""

from __future__ import annotations

import json
import types
from pathlib import Path

import pytest

import app.pipeline.caption_translate as ct
from app.config import settings
from app.pipeline import text_overlay_skia as cloud
from app.pipeline.phone_captions import (
    CAPTION_FONT_FAMILY,
    caption_ink_box,
    caption_look_from_variant,
    compile_caption_layers,
)
from app.services.phone_text_styling import phone_text_policy_scope

_REAL_LLM_TRANSLATE_LINES = ct._llm_translate_lines
FIXTURES = Path(__file__).resolve().parents[1] / "fixtures"
EN_WORDS = json.loads((FIXTURES / "kri720_cigdem_take_a_words.json").read_text(encoding="utf-8"))
_KADIKOY_ILK = "İlk durak Moda'da, deniz kenarında küçücük bir yer."


def _gate(monkeypatch, *, on: bool = True, verified: bool = True) -> None:
    monkeypatch.setattr(settings, "dual_captions_enabled", on)
    monkeypatch.setattr(
        settings,
        "phone_render_verified_features",
        [
            *[f for f in settings.phone_render_verified_features if f != "dualCaptions"],
            *(["dualCaptions"] if verified else []),
        ],
    )


def _en_cues() -> list[dict]:
    """Çiğdem take A cut into phone caption cues by the real cue builder (first four)."""
    from app.pipeline.captions import PHONE_CUE_BUDGET, build_plain_cues
    from app.pipeline.transcribe import Word

    words = [
        Word(text=w["text"], start_s=w["start_s"], end_s=w["end_s"], confidence=1.0)
        for w in EN_WORDS
    ]
    cues = build_plain_cues(words, attach_words=True, budget=PHONE_CUE_BUDGET)[:4]
    return [{**cue, "id": f"native-caption-{index}"} for index, cue in enumerate(cues)]


def _track(language: str = "de", source: str = "en", **extra) -> dict:
    return {
        "enabled": True,
        "language": language,
        "source_language": source,
        "size_px": None,
        "color": "#F5D33C",
        "font": None,
        "stroke_width": None,
        "position_x_frac": None,
        "position_y_frac": None,
        "line_overrides": {},
        "outcome": "translated",
        "dropped_cue_ids": [],
        **extra,
    }


def _compile(cues, variant, *, style="sentence"):
    return compile_caption_layers(
        cues,
        canvas_width=1080,
        canvas_height=1920,
        style=style,
        look=caption_look_from_variant(variant),
    )


def _with(cues: list[dict], lines: list[str]) -> list[dict]:
    return [{**cue, "translation": line} for cue, line in zip(cues, lines, strict=False)]


def _ink(layer) -> tuple[float, float, float, float]:
    face = cloud._resolve_typeface_for_overlay({"font_family": CAPTION_FONT_FAMILY}).typeface
    boxes = [box for run in layer.runs if (box := caption_ink_box(run, face)) is not None]
    return (
        min(b[0] for b in boxes),
        min(b[1] for b in boxes),
        max(b[2] for b in boxes),
        max(b[3] for b in boxes),
    )


GERMAN = [
    "Warte, das Licht, okay.",
    "Warum machst du das so?",
    "Ich weiß es auch nicht genau.",
    "Lass es uns einfach noch einmal versuchen.",
]


# ------------------------------------------------------------------------------- [gate_off]


@pytest.mark.parametrize(("on", "verified"), [(False, True), (True, False)])
def test_gate_off_compiles_byte_identically(monkeypatch, on, verified):
    _gate(monkeypatch, on=on, verified=verified)
    cues = _en_cues()
    plain = _compile(cues, {})
    carrying = _compile(_with(cues, GERMAN), {"caption_translation": _track()})
    assert [layer.model_dump() for layer in carrying] == [layer.model_dump() for layer in plain]


def test_no_translated_track_compiles_byte_identically_even_with_the_gate_on(monkeypatch):
    _gate(monkeypatch)
    cues = _en_cues()
    plain = _compile(cues, {})
    assert [layer.model_dump() for layer in _compile(_with(cues, GERMAN), {})] == [
        layer.model_dump() for layer in plain
    ]


# ------------------------------------------------------------------------------- [attached]


def test_attached_line_sits_under_the_spoken_ink_and_spans_its_cue(monkeypatch):
    _gate(monkeypatch)
    cues = _en_cues()
    layers = _compile(_with(cues, GERMAN), {"caption_translation": _track()})
    spoken = {layer.id: layer for layer in layers if "-tr-" not in layer.id}
    translated = [layer for layer in layers if "-tr-" in layer.id]
    assert [layer.id for layer in translated] == [f"caption-tr-{i}" for i in range(len(cues))]
    for index, layer in enumerate(translated):
        cue, own = cues[index], spoken[f"caption-{index}"]
        assert (layer.start, layer.end) == pytest.approx((cue["start_s"], cue["end_s"]))
        assert " ".join(run.text for run in layer.runs) == GERMAN[index]
        size = layer.runs[0].font_size
        assert size == round(0.6 * own.runs[0].font_size)  # 0.6 x 78 = 47
        assert len({run.baseline_y for run in layer.runs}) == 1
        spoken_ink, line_ink = _ink(own), _ink(layer)
        assert line_ink[1] - spoken_ink[3] == pytest.approx(0.25 * size, abs=1.5)
        assert (line_ink[0] + line_ink[2]) / 2 == pytest.approx(
            (spoken_ink[0] + spoken_ink[2]) / 2, abs=2.0
        )
        assert layer.effect == "pop-in" and layer.karaoke is None
        assert layer.runs[0].fill.red == pytest.approx(0xF5 / 255, abs=0.01)


def test_karaoke_spoken_line_keeps_its_sweep_and_the_translation_has_none(monkeypatch):
    _gate(monkeypatch)
    cues = _en_cues()
    layers = _compile(_with(cues, GERMAN), {"caption_translation": _track()}, style="word")
    assert all(layer.karaoke is not None for layer in layers if "-tr-" not in layer.id)
    assert all(layer.karaoke is None for layer in layers if "-tr-" in layer.id)


# --------------------------------------------------------------------------------- [german]


def test_a_long_german_line_shrinks_to_one_row_never_cut(monkeypatch):
    _gate(monkeypatch)
    cues = _en_cues()[:1]
    long_line = "Warte mal kurz, das Licht hier vorne ist gerade überhaupt nicht in Ordnung, okay."
    [spoken, layer] = _compile(_with(cues, [long_line]), {"caption_translation": _track()})
    assert " ".join(run.text for run in layer.runs) == long_line  # every word, one row
    assert len({run.baseline_y for run in layer.runs}) == 1
    size = layer.runs[0].font_size
    assert 24 <= size < round(0.6 * spoken.runs[0].font_size)
    left, _top, right, _bottom = _ink(layer)
    assert left >= 0.05 * 1080 - 2 and right <= 0.95 * 1080 + 2


def test_a_line_too_wide_even_at_24px_is_dropped(monkeypatch):
    _gate(monkeypatch)
    cues = _en_cues()[:2]
    absurd = " ".join(["Donaudampfschifffahrtsgesellschaftskapitän"] * 6)
    layers = _compile(_with(cues, [absurd, GERMAN[1]]), {"caption_translation": _track()})
    ids = [layer.id for layer in layers]
    assert "caption-tr-0" not in ids and "caption-tr-1" in ids


# ---------------------------------------------------------------------------------- [split]


def test_a_cue_split_in_time_keeps_one_translated_line_over_its_whole_time(monkeypatch):
    _gate(monkeypatch)
    cue = {"id": "native-caption-0", "text": _KADIKOY_ILK, "start_s": 0.0, "end_s": 1.9}
    layers = _compile(
        [{**cue, "translation": "Erster Halt in Moda, ein winziger Ort am Meer."}],
        {"caption_translation": _track(language="de", source="tr")},
    )
    spoken = [layer for layer in layers if "-tr-" not in layer.id]
    translated = [layer for layer in layers if "-tr-" in layer.id]
    assert len(spoken) > 1, "the prod Kadıköy cue is split in time to clear the watermark"
    assert [layer.id for layer in translated] == ["caption-tr-0"]
    assert (translated[0].start, translated[0].end) == pytest.approx((0.0, 1.9))
    lowest = max(_ink(layer)[3] for layer in spoken)
    assert _ink(translated[0])[1] >= lowest


# ----------------------------------------------------------------------------------- [face]


def test_a_face_under_the_spoken_line_flips_the_translation_above(monkeypatch):
    _gate(monkeypatch)
    cues = _en_cues()[:1]
    variant = {"caption_translation": _track()}
    [spoken, below] = _compile(_with(cues, GERMAN), variant)
    line = _ink(below)
    face = {
        "box": (0.0, line[1] / 1920, 1.0, line[3] / 1920),
        "start_s": 0.0,
        "end_s": 60.0,
    }
    with phone_text_policy_scope({"_face_boxes": [face]}):
        [_spoken, above] = _compile(_with(cues, GERMAN), variant)
    assert _ink(above)[3] <= _ink(spoken)[1]
    with phone_text_policy_scope(
        {"_face_boxes": [face, {**face, "box": (0.0, 0.0, 1.0, _ink(spoken)[1] / 1920)}]}
    ):
        layers = _compile(_with(cues, GERMAN), variant)
    assert [layer.id for layer in layers] == ["caption-0"], "never drawn on a face"


# ------------------------------------------------------------------------------- [detached]


def test_detached_track_and_one_line_override(monkeypatch):
    _gate(monkeypatch)
    cues = _en_cues()[:2]
    variant = {
        "caption_translation": _track(
            position_x_frac=0.5,
            position_y_frac=0.3,
            size_px=60,
            line_overrides={"native-caption-1": {"position_y_frac": 0.2, "size_px": 40}},
        )
    }
    layers = {layer.id: layer for layer in _compile(_with(cues, GERMAN), variant)}
    first, second = layers["caption-tr-0"], layers["caption-tr-1"]
    assert first.runs[0].font_size == 60 and second.runs[0].font_size == 40
    assert abs((sum(_ink(first)[1::2]) / 2) - 0.3 * 1920) < 40
    assert abs((sum(_ink(second)[1::2]) / 2) - 0.2 * 1920) < 40


# ---------------------------------------------------------------------------------- [switch]


def test_switched_off_track_draws_nothing(monkeypatch):
    _gate(monkeypatch)
    cues = _en_cues()
    plain = _compile(cues, {})
    off = _compile(_with(cues, GERMAN), {"caption_translation": _track(enabled=False)})
    assert [layer.model_dump() for layer in off] == [layer.model_dump() for layer in plain]


# ------------------------------------------------------------------------------ [translator]


class _FakeOpenAI:
    answer: str = "{}"
    last: _FakeOpenAI | None = None

    def __init__(self, **kwargs):
        type(self).last = self
        self.request: dict = {}
        outer = self

        class _Completions:
            def create(self, **kw):
                outer.request = kw
                message = types.SimpleNamespace(content=type(outer).answer)
                return types.SimpleNamespace(choices=[types.SimpleNamespace(message=message)])

        self.chat = types.SimpleNamespace(completions=_Completions())


@pytest.fixture
def provider(monkeypatch):
    """The real provider call over a fake OpenAI transport (no network)."""
    import openai

    monkeypatch.setattr(ct, "_llm_translate_lines", _REAL_LLM_TRANSLATE_LINES)
    monkeypatch.setattr(openai, "OpenAI", _FakeOpenAI)
    return _FakeOpenAI


def _translate(texts, durations, **kwargs):
    return ct.translate_caption_lines(
        texts,
        durations,
        source_language=kwargs.pop("source_language", "en"),
        target_language=kwargs.pop("target_language", "de"),
        enabled=kwargs.pop("enabled", True),
        **kwargs,
    )


def test_one_deterministic_call_with_a_budget_per_line(provider):
    cues = _en_cues()
    provider.answer = json.dumps({"lines": GERMAN})
    result = _translate([c["text"] for c in cues], [c["end_s"] - c["start_s"] for c in cues])

    request = provider.last.request
    assert request["temperature"] == 0
    assert request["response_format"] == {"type": "json_object"}
    system, user = request["messages"]
    assert "Condense like a subtitle" in system["content"] and "German" in system["content"]
    payload = json.loads(user["content"])
    assert payload["lines"] == [c["text"] for c in cues]
    assert payload["max_chars"] == [
        min(42, int(17 * (c["end_s"] - c["start_s"]))) or 1 for c in cues
    ]
    assert result.lines == GERMAN and result.outcome == "translated" and result.dropped == []


def test_a_wrong_language_line_is_dropped_and_listed(provider):
    cues = _en_cues()
    answer = [*GERMAN]
    answer[3] = cues[3]["text"]  # the English handed back
    provider.answer = json.dumps({"lines": answer})
    result = _translate([c["text"] for c in cues], [2.0] * len(cues))
    assert result.lines[3] is None and result.dropped == [3]
    assert result.outcome == "partial"
    assert result.lines[:3] == GERMAN[:3]


@pytest.mark.parametrize(
    "answer",
    [
        "not json",
        json.dumps({"lines": ["nur eine Zeile"]}),
        json.dumps({"lines": [1, 2, 3, 4]}),
    ],
    ids=["malformed", "line_count", "not_strings"],
)
def test_a_bad_answer_is_unmet_and_never_raises(provider, answer):
    cues = _en_cues()
    provider.answer = answer
    result = _translate([c["text"] for c in cues], [2.0] * len(cues))
    assert result.outcome == "unmet" and result.lines == [None] * len(cues)


def test_switch_off_or_same_language_never_calls_the_model(provider):
    provider.last = None
    off = _translate(["Hello there."], [1.0], enabled=False)
    same = _translate(["Hello there."], [1.0], target_language="en")
    assert (off.outcome, off.reason) == ("unmet", "dual_captions_disabled")
    assert (same.outcome, same.reason) == ("unmet", "same_language")
    assert provider.last is None


def test_empty_cues_get_no_line_and_are_not_dropped(provider):
    provider.answer = json.dumps({"lines": ["Hallo."]})
    result = _translate(["", "Hello."], [1.0, 1.0])
    assert result.lines == [None, "Hallo."] and result.dropped == []
    assert json.loads(provider.last.request["messages"][1]["content"])["lines"] == ["Hello."]


def test_subtitle_char_budget_follows_reading_speed():
    assert ct.subtitle_char_budget(1.5, "en") == 30
    assert ct.subtitle_char_budget(1.5, "de") == 25
    assert ct.subtitle_char_budget(10.0, "tr") == 42  # one row
    assert ct.subtitle_char_budget(0.01, "pt") == 1


# ------------------------------------------------------------------------------------ [echo]


def test_a_short_untranslated_echo_is_dropped_and_listed(provider):
    provider.answer = json.dumps({"lines": ["evet .", "Come on, we are leaving soon."]})
    result = _translate(
        ["Evet.", "Hadi, çıkacağız az sonra."],
        [1.0, 2.0],
        source_language="tr",
        target_language="en",
    )
    assert result.lines == [None, "Come on, we are leaving soon."]
    assert result.dropped == [0] and result.outcome == "partial"


def test_names_and_numbers_may_stay_as_spoken(provider):
    provider.answer = json.dumps({"lines": ["Loy Tree!", "2026", "Hello, Loy Tree."]})
    result = _translate(
        ["Loy Tree!", "2026", "Merhaba, Loy Tree."],
        [1.0, 1.0, 1.0],
        source_language="tr",
        target_language="en",
        glossary=["Loy Tree"],
    )
    assert result.lines == ["Loy Tree!", "2026", "Hello, Loy Tree."]
    assert result.dropped == [] and result.outcome == "translated"
