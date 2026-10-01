"""The phone guided compile must never make Save impossible because of a text's glyphs."""

from __future__ import annotations

import pytest

from app.agents._schemas.text_element import TextElement
from app.pipeline.phone_guided_plan import compile_phone_guided_plan
from tests.pipeline.test_phone_guided_plan import fixture


def _compile(text: str, **fields):  # noqa: ANN003, ANN202
    plan, bindings = fixture()
    plan.text_elements = [
        TextElement(
            id="t",
            text=text,
            start_s=0,
            end_s=plan.resolved_duration_s,
            font_family=fields.pop("font_family", "Instrument Serif"),
            effect=fields.pop("effect", "static"),
            **fields,
        )
    ]
    return compile_phone_guided_plan(plan, bindings)


@pytest.mark.parametrize(
    "text",
    [
        "5 am in my room",
        "Window reflection",
        "Dwight bobblehead",
        "It’s mine",
        "café",
        "İstanbul",
        "i̇stanbul",
        "line one\nline two",
        "  padded  ",
        "emoji \U0001f600 here",
    ],
)
@pytest.mark.parametrize("text_case", [None, "lower", "upper", "title"])
def test_text_compiles_or_fails_with_a_precise_reason(text: str, text_case: str | None) -> None:
    try:
        recipe = _compile(text, text_case=text_case) if text_case else _compile(text)
    except Exception as exc:  # noqa: BLE001
        pytest.fail(f"{text!r} case={text_case}: {type(exc).__name__}: {exc}")
    assert recipe.text_layers


@pytest.mark.parametrize("effect", ["typewriter", "fade-in", "pop-in"])
@pytest.mark.parametrize(
    "text",
    ["5 am in my room", "Window reflection", "Flower greeting card", "Man at camera"],
)
def test_the_users_real_texts_compile_under_every_entrance_effect(text: str, effect: str) -> None:
    for case in (None, "lower"):
        kwargs = {"text_case": case} if case else {}
        assert _compile(text, effect=effect, **kwargs).text_layers


def test_an_unmappable_character_falls_back_to_the_shaped_path_instead_of_blocking_save() -> None:
    recipe = _compile("emoji \U0001f600 here")
    runs = [run for layer in recipe.text_layers for run in getattr(layer, "runs", [])]
    assert runs and all(run.shaped for run in runs)
    ascii_runs = [run for layer in _compile("Window reflection").text_layers for run in layer.runs]
    assert ascii_runs and not any(run.shaped for run in ascii_runs)  # unchanged for normal text


def test_the_legacy_glyph_error_names_the_offending_character() -> None:
    import skia

    from app.pipeline.portable_text_layout import resolve_legacy_glyphs

    font = skia.Font(skia.Typeface.MakeDefault(), 40)
    with pytest.raises(ValueError, match=r"U\+1F600 in 'a.*'"):
        resolve_legacy_glyphs(font, "a\U0001f600b", 0.0)
