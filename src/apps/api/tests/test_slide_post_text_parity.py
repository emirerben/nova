"""Slide-post text parity with the video editor's Text tool (schema, PUT merge,
chat-edit round trip). Render/golden tests live in
tests/pipeline/test_slide_post_text_parity_render.py."""

from __future__ import annotations

import uuid

import pytest
from pydantic import ValidationError

from app.schemas.slide_post import (
    SlideEdits,
    SlidePostDraft,
    SlideRef,
    SlideTextElement,
    merge_legacy_text_edits,
)
from tests.services.test_slide_post_chat_edit import _asset, _draft, _edit, _text

PARITY_KEYS = (
    "rotation_deg",
    "stroke_color",
    "shadow_color",
    "shadow_opacity",
    "background_color",
    "editor_preset",
    "text_case",
    "letter_spacing",
    "line_spacing",
)


class TestSchema:
    def test_defaults_are_none_and_omitted_from_serialization(self):
        el = SlideTextElement(id="a", text="x")
        dumped = el.model_dump(mode="json")
        for key in PARITY_KEYS:
            assert getattr(el, key) is None
            assert key not in dumped
        assert el.size_px == 86 and isinstance(dumped["size_px"], int)

    def test_set_fields_round_trip(self):
        payload = {
            "id": "a",
            "text": "x",
            "rotation_deg": -12.5,
            "stroke_color": "#112233",
            "shadow_color": "#000000",
            "shadow_opacity": 0.4,
            "background_color": "#FFF0A6",
            "editor_preset": "Highlight",
            "text_case": "upper",
            "letter_spacing": 0.1,
            "line_spacing": 1.4,
            "size_px": 12.5,
            "stroke_width": 15,
        }
        el = SlideTextElement.model_validate(payload)
        dumped = el.model_dump(mode="json")
        assert dumped | payload == dumped
        assert SlideTextElement.model_validate(dumped) == el

    @pytest.mark.parametrize(
        "bad",
        [
            {"stroke_color": "red"},
            {"shadow_color": "#12"},
            {"background_color": "yellow"},
            {"shadow_opacity": 1.5},
            {"editor_preset": "Neon"},
            {"size_px": 7},
            {"size_px": 201},
            {"size_px": float("nan")},
            {"stroke_width": 21},
        ],
    )
    def test_bad_values_rejected(self, bad):
        with pytest.raises(ValidationError):
            SlideTextElement(id="a", text="x", **bad)

    def test_out_of_range_values_clamp_like_the_video_editor(self):
        el = SlideTextElement(
            id="a", text="x", rotation_deg=999, letter_spacing=9, line_spacing=0, text_case="WAT"
        )
        assert el.rotation_deg == 360.0
        assert el.letter_spacing == 0.5
        assert el.line_spacing == 0.5
        assert el.text_case is None

    def test_unknown_keys_are_ignored(self):
        el = SlideTextElement.model_validate(
            {"id": "a", "text": "x", "highlight_color": "#FFFFFF", "effect": "pop-in"}
        )
        assert "highlight_color" not in el.model_dump()

    def test_legacy_mirror_still_follows_first_element(self):
        edits = SlideEdits(texts=[SlideTextElement(id="a", text="One", rotation_deg=5)])
        assert edits.text is not None and edits.text.content == "One"


class TestPutMerge:
    def test_old_client_put_keeps_new_fields_of_stored_elements(self):
        aid = uuid.uuid4()
        styled = SlideTextElement(
            id="a",
            text="Lisbon",
            rotation_deg=10,
            stroke_color="#00FF00",
            background_color="#FFF0A6",
            letter_spacing=0.2,
            text_case="title",
        )
        stored = SlidePostDraft(
            platform_profile="tiktok_photo",
            slides=[SlideRef(id="s", asset_id=aid, kind="image", edits=SlideEdits(texts=[styled]))],
        )

        def body(edits: dict) -> SlideRef:
            return SlideRef.model_validate(
                {"id": "s", "asset_id": str(aid), "kind": "image", "edits": edits}
            )

        for text in ("Lisbon", "Porto"):
            out = merge_legacy_text_edits(
                stored, [body({"text": {"content": text, "position": "bottom"}})]
            )
            el = out[0].edits.texts[0]
            assert el.text == text
            assert (el.rotation_deg, el.stroke_color, el.background_color) == (
                10,
                "#00FF00",
                "#FFF0A6",
            )
            assert (el.letter_spacing, el.text_case) == (0.2, "title")


class TestChatEditRoundTrip:
    STYLED = dict(
        rotation_deg=15,
        stroke_color="#00FF00",
        shadow_color="#222222",
        shadow_opacity=0.5,
        background_color="#FFF0A6",
        editor_preset="Highlight",
        text_case="upper",
        letter_spacing=0.2,
        line_spacing=1.6,
        size_px=12.5,
        stroke_width=15,
    )

    def test_restyle_op_keeps_every_new_field(self):
        assets = [_asset(), _asset()]
        styled = _text("a", "Keep me", **self.STYLED)
        draft = _draft(assets, edits={0: SlideEdits(texts=[styled])})
        compiled, _ = _edit(
            draft,
            assets,
            [{"op": "patch_text", "selector": {"group": "all"}, "patch": {"color": "#00FF00"}}],
        )
        el = compiled.draft.slides[0].edits.texts[0]
        assert el.color == "#00FF00"
        for key in (*PARITY_KEYS, "size_px", "stroke_width"):
            assert getattr(el, key) == getattr(styled, key), key

    def test_unrelated_op_leaves_styled_element_untouched(self):
        assets = [_asset(), _asset()]
        styled = _text("a", "Keep me", **self.STYLED)
        draft = _draft(assets, edits={0: SlideEdits(texts=[styled])})
        compiled, _ = _edit(draft, assets, [{"op": "remove_clip", "slot_index": 1}])
        assert [s.id for s in compiled.draft.slides] == ["s0"]
        assert compiled.draft.slides[0].edits.texts[0] == styled
