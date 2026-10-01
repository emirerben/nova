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


class _StubFont:
    """A font whose glyph map is controlled by the test (platform-independent)."""

    def __init__(self, unmapped: str = "") -> None:
        self.unmapped = unmapped

    def textToGlyphs(self, text: str) -> list[int]:  # noqa: N802 - skia API name
        return [0 if char in self.unmapped else 7 for char in text]

    def getWidths(self, glyphs: list[int]) -> list[float]:  # noqa: N802
        return [10.0 for _ in glyphs]


def test_the_legacy_glyph_error_names_the_offending_character() -> None:
    from app.pipeline.portable_text_layout import resolve_legacy_glyphs

    with pytest.raises(ValueError, match=r"U\+1F600 in 'a.*b'"):
        resolve_legacy_glyphs(_StubFont(unmapped="\U0001f600"), "a\U0001f600b", 0.0)  # type: ignore[arg-type]
    assert len(resolve_legacy_glyphs(_StubFont(), "abc", 1.0)) == 3  # type: ignore[arg-type]


def test_the_predicate_flags_zero_glyphs_and_count_mismatches() -> None:
    from app.pipeline.portable_text_layout import legacy_glyphs_resolvable

    assert legacy_glyphs_resolvable(_StubFont(), "abc\ndef")  # type: ignore[arg-type]
    assert not legacy_glyphs_resolvable(_StubFont(unmapped="x"), "axb")  # type: ignore[arg-type]

    class _Merged(_StubFont):
        def textToGlyphs(self, text: str) -> list[int]:  # noqa: N802
            return [7] * (len(text) - 1)  # e.g. a combining sequence collapsed into one glyph

    assert not legacy_glyphs_resolvable(_Merged(), "ab")  # type: ignore[arg-type]


def _shaped_flags(text: str) -> list[bool]:
    return [run.shaped for layer in _compile(text).text_layers for run in layer.runs]


def test_an_unmappable_text_falls_back_to_the_shaped_path(monkeypatch) -> None:  # noqa: ANN001
    import app.pipeline.portable_text_layout as layout

    monkeypatch.setattr(layout, "legacy_glyphs_resolvable", lambda _font, _text: False)
    flags = _shaped_flags("Window reflection")
    assert flags and all(flags)


def test_a_mappable_text_keeps_the_unshaped_path_on_every_platform(monkeypatch) -> None:  # noqa: ANN001
    """Simulates a skia build that maps the emoji 1:1: the fallback must not trigger."""
    import app.pipeline.portable_text_layout as layout

    monkeypatch.setattr(layout, "legacy_glyphs_resolvable", lambda _font, _text: True)
    monkeypatch.setattr(
        layout,
        "resolve_legacy_glyphs",
        lambda _f, text, _s: [
            layout.PositionedGlyph(glyph_id=7, x=float(i), y=0) for i, _ in enumerate(text)
        ],
    )
    flags = _shaped_flags("emoji \U0001f600 here")
    assert flags and not any(flags)
    assert not any(_shaped_flags("Window reflection"))  # real font, normal text: unshaped
