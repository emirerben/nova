"""Chat-edit `set_slide_tilt` (KRI-1138): the copilot tilts slides, honestly.

Failure modes (listed before the code was written):
 C1  the op is offered or accepted while the gate (MEDIA_TILT_ENABLED + protocol 28) is closed
 C2  the wrong slide or the wrong sign: "right" must be clockwise-positive; a relative ask is
     resolved against the slot's CURRENT tilt, never added blindly
 C3  an unrelated text or look edit rebuilds a tilted slide and drops the tilt
 C4  a backdrop alone silently stores a background that never shows; a colour on blur/blank
 C5  video slides cannot be tilted (they can) or "all" skips them
 C6  a closed gate changes the snapshot a video or older app sees
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from app.config import settings
from app.schemas.slide_post import SlideEdits, SlidePostDraft
from app.services.slide_post_chat_edit import build_slide_post_snapshot, compile_slide_post_ops
from tests.services.test_slide_post_chat_edit import (
    USER_ID,
    _asset,
    _by_id,
    _draft,
    _parse,
)


def _snapshot(draft: SlidePostDraft, assets: list[SimpleNamespace], *, enabled: bool = True):
    return build_slide_post_snapshot(draft, _by_id(assets), user_id=USER_ID, tilt_enabled=enabled)


def _edit(draft: SlidePostDraft, assets: list[SimpleNamespace], ops: list[dict], **kw):
    output = _parse(_snapshot(draft, assets, **kw), ops)
    assert output.ops, output.rejection_reasons
    return compile_slide_post_ops(draft, output.ops), output


def _tilt(draft: SlidePostDraft, index: int):
    edits = draft.slides[index].edits
    return edits.tilt if edits else None


def _with_video(draft: SlidePostDraft, index: int) -> SlidePostDraft:
    slides = list(draft.slides)
    slides[index] = slides[index].model_copy(update={"kind": "video"})
    return draft.model_copy(update={"slides": slides})


TILT = {"rotation_deg": 10.0, "backdrop": {"kind": "blur"}}


class TestTheGate:
    def test_snapshot_offers_the_op_only_when_open_and_shows_current_tilts(self):
        assets = [_asset(), _asset()]
        draft = _draft(assets, edits={1: SlideEdits(tilt=TILT)})
        on = _snapshot(draft, assets)
        assert on["post"]["tilt_enabled"] is True
        assert "tilt" not in on["slots"][0]
        assert on["slots"][1]["tilt"] == {"rotation_deg": 10.0, "backdrop": {"kind": "blur"}}
        off = _snapshot(draft, assets, enabled=False)
        assert "tilt_enabled" not in off["post"]
        assert all("tilt" not in slot for slot in off["slots"])  # C6

    def test_a_closed_gate_refuses_the_op_honestly(self):
        assets = [_asset()]
        output = _parse(
            _snapshot(_draft(assets), assets, enabled=False),
            [{"op": "set_slide_tilt", "slide_indexes": [0], "rotation_deg": 10}],
        )
        assert not output.ops
        assert any(r.get("reason") == "capability_unavailable" for r in output.rejection_reasons)

    def test_the_op_is_refused_on_a_video_edit(self):
        from app.agents import edit_copilot  # noqa: PLC0415

        assert not edit_copilot._family_allowed("set_slide_tilt", {"surface": "timeline"})


class TestSignAndTargets:
    def test_right_is_clockwise_positive_and_only_the_named_slide_turns(self):
        assets = [_asset(), _asset(), _asset()]
        compiled, _ = _edit(
            _draft(assets),
            assets,
            [{"op": "set_slide_tilt", "slide_indexes": [1], "rotation_deg": 10}],
        )
        assert _tilt(compiled.draft, 1).rotation_deg == 10
        assert _tilt(compiled.draft, 1).backdrop.kind == "blank"
        assert _tilt(compiled.draft, 0) is None and _tilt(compiled.draft, 2) is None
        assert "Tilt changed on 1 slide" in compiled.changes

    def test_left_is_negative_and_an_angle_folds_into_range(self):
        assets = [_asset(), _asset()]
        compiled, _ = _edit(
            _draft(assets),
            assets,
            [
                {"op": "set_slide_tilt", "slide_indexes": [0], "rotation_deg": -12},
                {"op": "set_slide_tilt", "slide_indexes": [1], "rotation_deg": 200},
            ],
        )
        assert _tilt(compiled.draft, 0).rotation_deg == -12
        assert _tilt(compiled.draft, 1).rotation_deg == -160

    def test_the_model_resolves_a_relative_ask_from_the_slots_current_tilt(self):
        # "a bit more to the left" with the slot at +10: the prompt makes the model send 5.
        assets = [_asset()]
        draft = _draft(assets, edits={0: SlideEdits(tilt=TILT)})
        compiled, _ = _edit(
            draft, assets, [{"op": "set_slide_tilt", "slide_indexes": [0], "rotation_deg": 5}]
        )
        tilt = _tilt(compiled.draft, 0)
        assert tilt.rotation_deg == 5 and tilt.backdrop.kind == "blur"  # the backdrop is kept

    def test_all_turns_photos_and_videos_with_one_backdrop(self):
        assets = [_asset(), _asset(), _asset()]
        draft = _with_video(_draft(assets), 1)
        compiled, _ = _edit(
            draft,
            assets,
            [{"op": "set_slide_tilt", "all": True, "rotation_deg": 6, "backdrop": "blur"}],
        )
        assert [_tilt(compiled.draft, i).rotation_deg for i in range(3)] == [6, 6, 6]
        assert {_tilt(compiled.draft, i).backdrop.kind for i in range(3)} == {"blur"}
        assert "Tilt changed on 3 slides" in compiled.changes  # C5

    def test_reset_straightens_everything(self):
        assets = [_asset(), _asset()]
        draft = _draft(assets, edits={0: SlideEdits(tilt=TILT), 1: SlideEdits(tilt=TILT)})
        compiled, _ = _edit(draft, assets, [{"op": "set_slide_tilt", "all": True, "reset": True}])
        assert _tilt(compiled.draft, 0) is None and _tilt(compiled.draft, 1) is None

    def test_a_colour_backdrop_carries_its_colour(self):
        assets = [_asset()]
        compiled, _ = _edit(
            _draft(assets),
            assets,
            [
                {
                    "op": "set_slide_tilt",
                    "slide_indexes": [0],
                    "rotation_deg": 8,
                    "backdrop": "color",
                    "color": "#2563EB",
                }
            ],
        )
        assert _tilt(compiled.draft, 0).backdrop.color == "#2563EB"


class TestRefusals:
    @pytest.mark.parametrize(
        "op",
        [
            {"op": "set_slide_tilt", "slide_indexes": [0]},
            {"op": "set_slide_tilt", "slide_indexes": [0], "reset": True, "rotation_deg": 5},
            {"op": "set_slide_tilt", "slide_indexes": [0], "rotation_deg": 5, "backdrop": "color"},
            {
                "op": "set_slide_tilt",
                "slide_indexes": [0],
                "rotation_deg": 5,
                "backdrop": "blur",
                "color": "#112233",
            },
            {
                "op": "set_slide_tilt",
                "slide_indexes": [0],
                "rotation_deg": 5,
                "backdrop": "color",
                "color": "blue",
            },
            {"op": "set_slide_tilt", "slide_indexes": [9], "rotation_deg": 5},
            {"op": "set_slide_tilt", "slide_indexes": [0], "rotation_deg": "ten"},
        ],
    )
    def test_an_invalid_op_is_refused_by_the_parser(self, op):
        assets = [_asset()]
        assert not _parse(_snapshot(_draft(assets), assets), [op]).ops

    def test_a_backdrop_alone_on_an_upright_slide_is_refused_not_stored(self):
        assets = [_asset()]
        output = _parse(
            _snapshot(_draft(assets), assets),
            [{"op": "set_slide_tilt", "slide_indexes": [0], "backdrop": "blur"}],
        )
        assert not output.ops  # C4

    def test_a_backdrop_alone_changes_a_tilted_slides_background(self):
        assets = [_asset()]
        draft = _draft(assets, edits={0: SlideEdits(tilt={"rotation_deg": 20})})
        compiled, _ = _edit(
            draft, assets, [{"op": "set_slide_tilt", "slide_indexes": [0], "backdrop": "blur"}]
        )
        tilt = _tilt(compiled.draft, 0)
        assert tilt.rotation_deg == 20 and tilt.backdrop.kind == "blur"


class TestOtherEditsKeepTheTilt:
    @pytest.mark.parametrize(
        "op",
        [
            {"op": "add_text", "text": "Hello", "start_s": 0.0, "end_s": 1.0},
            {"op": "set_look_preset", "slot_index": 0, "look_preset": "stadium_diffusion"},
        ],
    )
    def test_a_text_or_look_change_to_a_tilted_slide_keeps_its_tilt(self, op):
        assets = [_asset(), _asset()]
        draft = _draft(assets, edits={0: SlideEdits(tilt=TILT)})
        compiled, _ = _edit(draft, assets, [op], enabled=False)
        assert _tilt(compiled.draft, 0).rotation_deg == 10.0  # C3
        assert _tilt(compiled.draft, 0).backdrop.kind == "blur"
        assert compiled.draft.slides[1].edits is None

    def test_a_tilt_op_keeps_text_and_stickers_on_the_slide(self):
        from app.schemas.slide_post import SlideTextElement  # noqa: PLC0415

        assets = [_asset()]
        edits = SlideEdits(
            texts=[SlideTextElement(id="t", text="Hi")],
            stickers=[
                {"id": "s1", "asset_id": "a", "x_frac": 0.5, "y_frac": 0.5, "width_frac": 0.3}
            ],
        )
        compiled, _ = _edit(
            _draft(assets, edits={0: edits}),
            assets,
            [{"op": "set_slide_tilt", "slide_indexes": [0], "rotation_deg": 12}],
        )
        kept = compiled.draft.slides[0].edits
        assert [t.text for t in kept.texts] == ["Hi"] and [s.id for s in kept.stickers] == ["s1"]
        assert kept.tilt.rotation_deg == 12


def _render(snapshot: dict) -> str:
    from app.agents._runtime import ModelClient  # noqa: PLC0415
    from app.agents.edit_copilot import EditCopilotAgent, EditCopilotInput  # noqa: PLC0415

    return EditCopilotAgent(ModelClient()).render_prompt(
        EditCopilotInput(utterance="tilt it", variant_snapshot=snapshot)
    )


def test_the_model_sees_the_gate_and_each_slides_current_angle_only_when_open() -> None:
    # C2: a relative ask ("a bit more") can only resolve if the prompt shows the current angle.
    assets = [_asset(), _asset()]
    draft = _draft(
        assets,
        edits={
            1: SlideEdits(
                tilt={"rotation_deg": -7.5, "backdrop": {"kind": "color", "color": "#336699"}}
            )
        },
    )
    open_prompt = _render(_snapshot(draft, assets))
    assert "\ntilt_enabled: true\n" in open_prompt
    assert "slide 2 tilt: rotation_deg=-7.5 backdrop=color #336699" in open_prompt
    assert "slide 1 tilt" not in open_prompt
    closed = _render(_snapshot(draft, assets, enabled=False))
    assert "\ntilt_enabled: true" not in closed and "rotation_deg=-7.5" not in closed


def test_the_slide_prompt_teaches_the_op_and_video_prompts_do_not() -> None:
    from app.agents import editor_ops_v2  # noqa: PLC0415

    assert "set_slide_tilt" in editor_ops_v2.prompt_fragments("slide_post")
    assert "CLOCKWISE-POSITIVE" in editor_ops_v2.prompt_fragments("slide_post")
    assert "set_slide_tilt" not in editor_ops_v2.prompt_fragments()


def test_the_flag_pair_is_what_opens_the_gate(monkeypatch) -> None:
    # The route opens it with MEDIA_TILT_ENABLED and a protocol-28 build; this pins both names.
    assert settings.media_tilt_min_client_protocol == 28
    assert hasattr(settings, "media_tilt_enabled")
