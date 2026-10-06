"""KRI-301: slide-post chat editing through the edit copilot (no network).

Ops go through the REAL copilot parser (`EditCopilotAgent.parse`) so the snapshot
the service builds is exercised against the same gates the model's output meets.
"""

from __future__ import annotations

import json
import uuid
from types import SimpleNamespace
from typing import Any

import pytest

from app.agents import edit_copilot, editor_ops_v2
from app.agents._runtime import ModelClient, TerminalError
from app.agents.edit_copilot import (
    EditCopilotAgent,
    EditCopilotInput,
    EditCopilotOutput,
    _family_allowed,
)
from app.config import settings
from app.schemas.slide_post import (
    SlideEdits,
    SlidePostDraft,
    SlideRef,
    SlideTextElement,
)
from app.services import slide_post_chat_edit as svc
from app.services.kria_editor_ops import KriaEditorOpError
from app.services.slide_post_chat_edit import (
    build_slide_post_snapshot,
    compile_slide_post_ops,
    run_slide_post_chat_edit,
    slide_wording,
)

USER_ID = uuid.uuid4()


# ---------------------------------------------------------------- fixtures / helpers


def _asset(*, time: str | None = None, place: str | None = None) -> SimpleNamespace:
    capture: dict[str, Any] = {}
    if time:
        capture["capture_time"] = time
    if place:
        capture["place"] = {"locality": place, "country": "Türkiye"}
    return SimpleNamespace(id=uuid.uuid4(), kind="image", analysis=None, capture=capture or None)


def _draft(
    assets: list[SimpleNamespace],
    *,
    edits: dict[int, SlideEdits] | None = None,
    cover_index: int = 0,
    version: int = 3,
    caption: str = "",
) -> SlidePostDraft:
    return SlidePostDraft(
        platform_profile="tiktok_photo",
        slides=[
            SlideRef(
                id=f"s{i}",
                asset_id=a.id,
                kind="image",
                edits=(edits or {}).get(i),
            )
            for i, a in enumerate(assets)
        ],
        cover_index=cover_index,
        version=version,
        caption=caption,
    )


def _by_id(assets: list[SimpleNamespace]) -> dict[Any, SimpleNamespace]:
    return {a.id: a for a in assets}


def _facts_on(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(settings, "clip_facts_enabled", True)


def _text(el_id: str, text: str, **extra: Any) -> SlideTextElement:
    return SlideTextElement(id=el_id, text=text, **extra)


def _parse(snapshot: dict, ops: list[dict], utterance: str = "do it") -> EditCopilotOutput:
    raw = json.dumps(
        {"intent": "edit", "ops": ops, "confidence": 0.9, "reply": "ok", "suggestions": []}
    )
    return EditCopilotAgent(ModelClient()).parse(
        raw, EditCopilotInput(utterance=utterance, variant_snapshot=snapshot)
    )


def _edit(
    draft: SlidePostDraft,
    assets: list[SimpleNamespace],
    ops: list[dict],
    utterance: str = "do it",
):
    snapshot = build_slide_post_snapshot(draft, _by_id(assets), user_id=USER_ID)
    output = _parse(snapshot, ops, utterance)
    assert output.ops, output.rejection_reasons
    return compile_slide_post_ops(draft, output.ops), output


CHRONO = {"op": "reorder_clips_by", "criterion": "capture_time", "direction": "asc"}
LABEL_PLACE = {"op": "label_each_clip", "source": "facts", "label_from": "place"}


# --------------------------------------------------------------------- the snapshot


def test_snapshot_shape_and_ids(monkeypatch: pytest.MonkeyPatch) -> None:
    _facts_on(monkeypatch)
    assets = [_asset(time="2024-07-02T10:00:00Z", place="Kadıköy"), _asset()]
    labelled = SlideEdits(
        texts=[
            _text("a", "Kadıköy", role="label", label_source="place", edited=True),
            _text("b", "Hello"),
        ]
    )
    legacy = SlideEdits.model_validate({"text": {"content": "Old", "position": "top"}})
    draft = _draft(assets, edits={0: labelled, 1: legacy}, cover_index=1, caption="Cap")
    snap = build_slide_post_snapshot(draft, _by_id(assets), user_id=USER_ID)

    assert snap["surface"] == "slide_post" and snap["editor_ops_version"] == 2
    assert [s["media_id"] for s in snap["slots"]] == ["s0", "s1"]
    assert [s["is_cover"] for s in snap["slots"]] == [False, True]
    assert (snap["slots"][1]["output_start_s"], snap["slots"][1]["output_end_s"]) == (1.0, 2.0)
    kinds = {f["kind"] for f in snap["slots"][0]["facts"]}
    assert kinds == {"capture_time", "place"}
    assert "facts" not in snap["slots"][1]
    assert snap["label_facts"] is True
    assert snap["post"] == {
        "caption": "Cap",
        "cover_index": 1,
        "platform_profile": "tiktok_photo",
    }
    bars = {b["id"]: b for b in snap["text_bars"]}
    label = bars["clip-label-media-s0"]
    assert label["clip_id"] == "s0" and label["edited"] is True
    assert bars["kria-s0:b"].get("clip_id") is None  # only labels link to a slide
    assert (bars["kria-s0:b"]["start_s"], bars["kria-s0:b"]["end_s"]) == (0.0, 1.0)
    # legacy text is lifted into a bar (top position survives)
    assert bars["kria-s1:legacy"]["position"] == "top" and bars["kria-s1:legacy"]["text"] == "Old"


def test_snapshot_without_facts_flag_has_no_facts_and_no_label_op(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(settings, "clip_facts_enabled", False)
    monkeypatch.setattr(settings, "clip_facts_user_ids", [])
    assets = [_asset(time="2024-07-02T10:00:00Z", place="Kadıköy"), _asset()]
    snap = build_slide_post_snapshot(_draft(assets), _by_id(assets), user_id=USER_ID)
    assert all("facts" not in s for s in snap["slots"])
    assert "label_facts" not in snap
    assert not _family_allowed("label_each_clip", snap)


def test_slide_facts_returns_empty_without_capture() -> None:
    assert svc._slide_facts(SimpleNamespace(analysis=None)) == []
    assert svc._slide_facts(SimpleNamespace(analysis="garbage", capture="garbage")) == []


# ------------------------------------------------------------------ text operations


def test_spanning_add_text_is_copied_per_slide_with_fresh_ids() -> None:
    assets = [_asset(), _asset(), _asset()]
    existing = SlideEdits(texts=[_text("keep", "Kept")])
    draft = _draft(assets, edits={1: existing})
    compiled, _ = _edit(
        draft, assets, [{"op": "add_text", "text": "Summer 2026", "start_s": 0, "end_s": 3}]
    )
    slides = compiled.draft.slides
    new_ids = []
    for slide in slides:
        texts = slide.edits.effective_texts()
        assert [t.text for t in texts if t.text == "Summer 2026"] == ["Summer 2026"]
        new_ids += [t.id for t in texts if t.text == "Summer 2026"]
    assert len(set(new_ids)) == 3  # fresh id per slide
    # slide 1 keeps its original element first
    assert [t.text for t in slides[1].edits.effective_texts()] == ["Kept", "Summer 2026"]
    assert slides[1].edits.texts[0].id == "keep"
    # mirror invariant: legacy text is texts[0]
    assert slides[0].edits.text.content == "Summer 2026"
    assert compiled.draft.version == draft.version + 1


def test_single_slide_add_text_lands_on_that_slide_only() -> None:
    assets = [_asset(), _asset(), _asset()]
    compiled, _ = _edit(
        _draft(assets), assets, [{"op": "add_text", "text": "Only two", "start_s": 1, "end_s": 2}]
    )
    texts = [s.edits.effective_texts() if s.edits else [] for s in compiled.draft.slides]
    assert [len(t) for t in texts] == [0, 1, 0]


def test_remove_slide_with_text_drops_its_text_and_keeps_the_rest() -> None:
    assets = [_asset(), _asset(), _asset()]
    edits = {
        0: SlideEdits(texts=[_text("a", "Zero")]),
        1: SlideEdits(texts=[_text("b", "One")]),
        2: SlideEdits(texts=[_text("c", "Two")]),
    }
    compiled, _ = _edit(
        _draft(assets, edits=edits), assets, [{"op": "remove_clip", "slot_index": 1}]
    )
    slides = compiled.draft.slides
    assert [s.id for s in slides] == ["s0", "s2"]
    assert [s.edits.effective_texts()[0].text for s in slides] == ["Zero", "Two"]
    assert compiled.changes[0] == "Removed 1 slide"


def test_removing_the_last_slide_fails_honestly() -> None:
    assets = [_asset()]
    snap = build_slide_post_snapshot(_draft(assets), _by_id(assets), user_id=USER_ID)
    output = _parse(snap, [{"op": "remove_clip", "slot_index": 0}])
    assert output.ops
    with pytest.raises(KriaEditorOpError, match="final clip"):
        compile_slide_post_ops(_draft(assets), output.ops)


def test_patch_text_all_restyles_every_text_and_keeps_legacy_box() -> None:
    assets = [_asset(), _asset(), _asset()]
    legacy = SlideEdits.model_validate({"text": {"content": "Legacy", "position": "bottom"}})
    rich = SlideEdits(texts=[_text("a", "Rich", color="#FF0000")])
    draft = _draft(assets, edits={0: legacy, 1: rich})
    compiled, _ = _edit(
        draft,
        assets,
        [
            {
                "op": "patch_text",
                "selector": {"group": "all"},
                "patch": {"color": "#00FF00", "stroke_width": 3, "size_px": 400},
            }
        ],
    )
    s0, s1, s2 = compiled.draft.slides
    for slide in (s0, s1):
        el = slide.edits.effective_texts()[0]
        assert el.color == "#00FF00" and el.stroke_width == 3
        assert el.size_px <= 200  # clamped to the slide schema
    # legacy upgrade: now an explicit rich element but its box background survives
    assert s0.edits.texts is not None and s0.edits.texts[0].background == "box"
    assert s0.edits.texts[0].text == "Legacy"
    assert s2.edits is None  # untouched slide stays byte-identical
    assert compiled.changes == ["Text updated on 2 slides"]


def test_text_limits_fail_honestly() -> None:
    assets = [_asset()]
    full = SlideEdits(texts=[_text(f"t{i}", f"Text {i}") for i in range(4)])
    draft = _draft(assets, edits={0: full})
    snap = build_slide_post_snapshot(draft, _by_id(assets), user_id=USER_ID)
    output = _parse(snap, [{"op": "add_text", "text": "Fifth", "start_s": 0, "end_s": 1}])
    assert output.ops
    with pytest.raises(KriaEditorOpError, match="more than 4 texts"):
        compile_slide_post_ops(draft, output.ops)


def test_text_longer_than_slide_limit_fails_honestly() -> None:
    assets = [_asset()]
    draft = _draft(assets, edits={0: SlideEdits(texts=[_text("a", "x" * 100)])})
    snap = build_slide_post_snapshot(draft, _by_id(assets), user_id=USER_ID)
    output = _parse(
        snap,
        [
            {
                "op": "rewrite_text",
                "selector": {"group": "all"},
                "replace": {"find": "x", "with": "xyz"},
            }
        ],
    )
    assert output.ops
    with pytest.raises(KriaEditorOpError, match="at most 120"):
        compile_slide_post_ops(draft, output.ops)


# ----------------------------------------------------------- ordering and labels


def _dated_assets(monkeypatch: pytest.MonkeyPatch) -> list[SimpleNamespace]:
    _facts_on(monkeypatch)
    return [
        _asset(time="2024-07-03T10:00:00Z", place="Beşiktaş"),  # s0 (latest)
        _asset(),  # s1 (no capture time: keeps its slot)
        _asset(time="2024-07-01T10:00:00Z", place="Kadıköy"),  # s2 (earliest)
        _asset(time="2024-07-02T10:00:00Z", place="Kadıköy"),  # s3
    ]


def test_chronological_order_keeps_untimed_slides_in_place(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    assets = _dated_assets(monkeypatch)
    compiled, output = _edit(_draft(assets), assets, [CHRONO])
    assert [s.id for s in compiled.draft.slides] == ["s2", "s1", "s3", "s0"]
    assert "No filming time for slide" in output.reply_notes.replace("clip", "slide")
    assert compiled.changes == ["Reordered 3 slides"]


def test_cover_follows_its_slide_through_a_reorder(monkeypatch: pytest.MonkeyPatch) -> None:
    assets = _dated_assets(monkeypatch)
    compiled, _ = _edit(_draft(assets, cover_index=0), assets, [CHRONO])
    new_order = [s.id for s in compiled.draft.slides]
    assert new_order[compiled.draft.cover_index] == "s0"
    assert compiled.draft.cover_index == 3


def test_removed_cover_falls_back_to_first_slide() -> None:
    assets = [_asset(), _asset(), _asset()]
    compiled, _ = _edit(
        _draft(assets, cover_index=2), assets, [{"op": "remove_clip", "slot_index": 2}]
    )
    assert compiled.draft.cover_index == 0
    assert "Cover is now slide 1" in compiled.changes


def test_chronological_then_place_labels_with_dedupe_and_notes(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    assets = _dated_assets(monkeypatch)
    compiled, _ = _edit(_draft(assets), assets, [CHRONO, LABEL_PLACE])
    slides = compiled.draft.slides
    assert [s.id for s in slides] == ["s2", "s1", "s3", "s0"]
    labels = {
        s.id: [t for t in s.edits.effective_texts() if t.role == "label"] if s.edits else []
        for s in slides
    }
    # s2 and s3 are both Kadıköy and consecutive in the new order: the repeat is skipped
    assert [t.text for t in labels["s2"]] == ["Kadıköy"]
    assert labels["s3"] == []
    assert [t.text for t in labels["s0"]][0].startswith("Beşiktaş")
    assert labels["s1"] == []
    first = labels["s2"][0]
    assert first.label_source == "place" and first.edited is False
    assert "Location on 2 slides" in compiled.changes
    # a slide with no place is reported, never invented
    assert any("no location" in n for n in compiled.notes)


def test_hand_edited_label_is_kept_when_relabelling(monkeypatch: pytest.MonkeyPatch) -> None:
    _facts_on(monkeypatch)
    assets = [_asset(place="Kadıköy"), _asset(place="Beşiktaş")]
    edits = {
        0: SlideEdits(
            texts=[_text("l", "My own words", role="label", label_source="place", edited=True)]
        ),
    }
    compiled, _ = _edit(_draft(assets, edits=edits), assets, [LABEL_PLACE])
    s0, s1 = compiled.draft.slides
    assert s0.edits.texts[0].text == "My own words"  # untouched
    assert s0.edits.texts[0].edited is True
    assert s1.edits.effective_texts()[0].text.startswith("Beşiktaş")


def test_editing_a_label_marks_it_edited(monkeypatch: pytest.MonkeyPatch) -> None:
    _facts_on(monkeypatch)
    assets = [_asset(place="Kadıköy")]
    edits = {0: SlideEdits(texts=[_text("l", "Kadıköy", role="label", label_source="place")])}
    draft = _draft(assets, edits=edits)
    snap = build_slide_post_snapshot(draft, _by_id(assets), user_id=USER_ID)
    index = [b["id"] for b in snap["text_bars"]].index("clip-label-media-s0")
    compiled, _ = _edit(draft, assets, [{"op": "edit_text", "bar_index": index, "text": "Home"}])
    label = compiled.draft.slides[0].edits.texts[0]
    assert label.text == "Home" and label.edited is True and label.role == "label"


def test_cover_and_caption_ops() -> None:
    assets = [_asset(), _asset(), _asset()]
    compiled, _ = _edit(
        _draft(assets, caption="old"),
        assets,
        [
            {"op": "set_slide_cover", "slide_index": 2},
            {"op": "set_post_caption", "caption": "Summer in Istanbul"},
        ],
    )
    assert compiled.draft.cover_index == 2
    assert compiled.draft.caption == "Summer in Istanbul"
    assert "Cover is now slide 3" in compiled.changes and "Caption updated" in compiled.changes


def test_cover_follows_slide_when_reordered_in_same_bundle(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    assets = _dated_assets(monkeypatch)
    compiled, _ = _edit(
        _draft(assets), assets, [{"op": "set_slide_cover", "slide_index": 2}, CHRONO]
    )
    ids = [s.id for s in compiled.draft.slides]
    assert ids[compiled.draft.cover_index] == "s2"


def test_look_preset_is_applied_per_slide() -> None:
    assets = [_asset(), _asset()]
    compiled, _ = _edit(
        _draft(assets),
        assets,
        [{"op": "set_look_preset", "slot_index": 1, "look_preset": "stadium_diffusion"}],
    )
    assert compiled.draft.slides[1].edits.look_preset == "stadium_diffusion"
    assert compiled.draft.slides[0].edits is None


# ---------------------------------------------------------------------- allowlist


@pytest.mark.parametrize(
    "op",
    [
        {"op": "split_clip", "slot_index": 0, "at_s": 0.5},
        {"op": "swap_music", "track_id": "x"},
        {"op": "set_mix", "music_level": 0.5},
        {"op": "set_transition", "boundary_index": 0, "transition": "crossfade"},
        {"op": "set_clip_duration", "slot_index": 0, "duration_s": 3},
        {"op": "patch_slots", "selector": {"all": True}, "patch": {"duration_s": 2}},
        {"op": "set_total_duration", "target_s": 10},
    ],
)
def test_disallowed_ops_are_refused_by_parser_and_compiler(op: dict) -> None:
    assets = [_asset(), _asset()]
    draft = _draft(assets)
    snap = build_slide_post_snapshot(draft, _by_id(assets), user_id=USER_ID)
    assert not _family_allowed(op["op"], snap)
    assert _parse(snap, [op]).ops == []
    with pytest.raises(KriaEditorOpError, match="isn't available for slide posts"):
        compile_slide_post_ops(draft, [op])


def test_slide_ops_are_refused_on_video_snapshots() -> None:
    video = {"allowed_op_families": ["clip", "text", "slides"], "editor_ops_version": 2}
    assert not _family_allowed("set_slide_cover", video)
    assert not _family_allowed("set_post_caption", video)
    slide = {**video, "surface": "slide_post"}
    assert _family_allowed("set_slide_cover", slide)


# ---------------------------------------------------- video prompts stay byte-identical


def _render(snapshot: dict) -> str:
    return EditCopilotAgent(ModelClient()).render_prompt(
        EditCopilotInput(utterance="make it pop", variant_snapshot=snapshot)
    )


def test_video_prompt_never_contains_slide_fragment() -> None:
    video = {"allowed_op_families": ["text", "clip"], "editor_ops_version": 2, "slots": []}
    assert "SLIDE POST MODE" not in editor_ops_v2.prompt_fragments()
    assert "SLIDE POST MODE" not in _render(video)
    assert "CURRENT POST" not in _render(video)
    # exactly what it was before the slides lane existed: the three video fragments
    lanes = [editor_ops_v2._FRAGMENT_DIR / f"{n}.txt" for n in ("text", "timeline", "audio")]
    expected = "\n\n".join(
        p.read_text(encoding="utf-8").strip() for p in lanes if p.read_text().strip()
    )
    assert editor_ops_v2.prompt_fragments() == expected


def test_slide_prompt_appends_the_slide_fragment_and_post_block() -> None:
    assets = [_asset(), _asset()]
    snap = build_slide_post_snapshot(_draft(assets, caption="hi"), _by_id(assets))
    prompt = _render(snap)
    assert "SLIDE POST MODE" in prompt
    assert "CURRENT POST" in prompt and "cover_slide: 1" in prompt and "'hi'" in prompt
    assert editor_ops_v2.prompt_fragments("slide_post").endswith(
        (editor_ops_v2._FRAGMENT_DIR / "slides.txt").read_text(encoding="utf-8").strip()
    )


def test_prompt_version_was_bumped_for_the_slide_surface() -> None:
    assert edit_copilot.EDIT_COPILOT_PROMPT_VERSION != "2026-10-02-v66"


# -------------------------------------------------------------------- run helper


class _Stub:
    def __init__(self, output: EditCopilotOutput | Exception) -> None:
        self.output = output

    def run(self, _input: EditCopilotInput, ctx: Any = None) -> EditCopilotOutput:
        if isinstance(self.output, Exception):
            raise self.output
        return self.output


def _patch_agent(monkeypatch: pytest.MonkeyPatch, output: EditCopilotOutput | Exception) -> None:
    stub = _Stub(output)
    monkeypatch.setattr("app.agents._model_client.default_client", lambda: object())
    monkeypatch.setattr(svc, "EditCopilotAgent", lambda _client: stub)


async def _run(draft: SlidePostDraft, assets: list[SimpleNamespace], server_version: int = 7):
    return await run_slide_post_chat_edit(
        draft=draft,
        assets_by_id=_by_id(assets),
        message="do it",
        turns=[],
        user_id=USER_ID,
        server_version=server_version,
    )


def _output(ops: list[dict], **kw: Any) -> EditCopilotOutput:
    snapshot_ops = ops
    return EditCopilotOutput(
        intent=kw.pop("intent", "edit"),
        ops=snapshot_ops,
        reply=kw.pop("reply", "ok"),
        **kw,
    )


@pytest.mark.asyncio
async def test_run_edited_outcome_reply_is_server_composed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    assets = _dated_assets(monkeypatch)
    draft = _draft(assets)
    snap = build_slide_post_snapshot(draft, _by_id(assets), user_id=USER_ID)
    parsed = _parse(snap, [CHRONO, LABEL_PLACE])
    _patch_agent(monkeypatch, parsed.model_copy(update={"reply": "I made it all perfect!!"}))
    result = await _run(draft, assets)
    assert result.outcome == "edited"
    assert result.draft is not None and result.base_version == 7
    assert "perfect" not in result.reply  # never the model's prose
    assert "Reordered" in result.reply and "Save when you're happy" in result.reply
    assert "clip" not in result.reply.lower()
    assert result.changes and result.draft.version == draft.version + 1


@pytest.mark.asyncio
async def test_run_clarification_and_refusal_use_slide_wording(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    assets = [_asset(), _asset()]
    _patch_agent(
        monkeypatch,
        _output(
            [],
            intent="clarify",
            needs_clarification=True,
            reply="Which clip do you mean?",
            suggestions=["Clip 1", "Clip 2"],
        ),
    )
    result = await _run(_draft(assets), assets)
    assert result.outcome == "clarification" and result.draft is None
    assert result.reply == "Which slide do you mean?"
    assert result.suggestions == ["Slide 1", "Slide 2"]

    _patch_agent(
        monkeypatch,
        _output([], intent="reject", reply="Can't do that."),
    )
    result = await _run(_draft(assets), assets)
    assert result.outcome == "unsupported" and result.draft is None


@pytest.mark.asyncio
async def test_run_compile_failure_is_failed_not_edited(monkeypatch: pytest.MonkeyPatch) -> None:
    assets = [_asset()]
    full = SlideEdits(texts=[_text(f"t{i}", f"T{i}") for i in range(4)])
    draft = _draft(assets, edits={0: full})
    snap = build_slide_post_snapshot(draft, _by_id(assets), user_id=USER_ID)
    parsed = _parse(snap, [{"op": "add_text", "text": "Fifth", "start_s": 0, "end_s": 1}])
    _patch_agent(monkeypatch, parsed)
    result = await _run(draft, assets)
    assert result.outcome == "failed" and result.draft is None
    assert "more than 4 texts" in result.reply


@pytest.mark.asyncio
async def test_run_no_op_result_is_no_effect(monkeypatch: pytest.MonkeyPatch) -> None:
    assets = [_asset(), _asset()]
    draft = _draft(assets, caption="same")
    snap = build_slide_post_snapshot(draft, _by_id(assets), user_id=USER_ID)
    parsed = _parse(snap, [{"op": "set_post_caption", "caption": "same"}])
    _patch_agent(monkeypatch, parsed)
    result = await _run(draft, assets)
    assert result.outcome == "no_effect" and result.draft is None


@pytest.mark.asyncio
async def test_run_agent_failure_is_honest(monkeypatch: pytest.MonkeyPatch) -> None:
    assets = [_asset()]
    _patch_agent(monkeypatch, TerminalError("boom"))
    result = await _run(_draft(assets), assets)
    assert result.outcome == "failed" and result.draft is None
    assert result.base_version == 7


def test_slide_wording() -> None:
    assert slide_wording("Clip 2 and clips 3; a clipboard") == "Slide 2 and slides 3; a clipboard"


def test_slide_wording_leaves_quoted_text_alone() -> None:
    assert slide_wording('Text "my clip 2" on clip 2') == 'Text "my clip 2" on slide 2'
    assert (
        slide_wording("Said \u201cclip it\u201d on clips") == "Said \u201cclip it\u201d on slides"
    )


def test_untouched_elements_are_not_normalised() -> None:
    """Stored text with edge whitespace / off-grid size must not count as changed."""
    assets = [_asset(), _asset()]
    odd = SlideEdits(texts=[_text("a", " padded ", size_px=87)])
    draft = _draft(assets, edits={0: odd})
    compiled, _ = _edit(draft, assets, [{"op": "set_post_caption", "caption": "hi"}])
    assert compiled.draft.slides[0].edits.texts[0].text == " padded "
    assert compiled.draft.slides[0] == draft.slides[0]
    assert compiled.changes == ["Caption updated"]


def test_reorder_and_remove_keep_every_text_on_its_own_slide(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _facts_on(monkeypatch)
    assets = [
        _asset(time="2024-07-04T10:00:00Z", place="Beşiktaş"),  # s0 (latest)
        _asset(time="2024-07-03T10:00:00Z", place="Kadıköy"),  # s1: removed
        _asset(time="2024-07-02T10:00:00Z", place="Moda"),  # s2
        _asset(time="2024-07-01T10:00:00Z", place="Fener"),  # s3: earliest
    ]
    edits = {
        0: SlideEdits(texts=[_text("t0", "zero")]),
        2: SlideEdits(
            texts=[_text("t2", "two"), _text("l2", "Moda", role="label", label_source="place")]
        ),
        3: SlideEdits(texts=[_text("t3", "three")]),
    }
    draft = _draft(assets, edits=edits)
    compiled, _ = _edit(
        draft,
        assets,
        [{"op": "remove_clip", "slot_index": 1}, CHRONO],
    )
    order = [s.id for s in compiled.draft.slides]
    assert order == ["s3", "s2", "s0"]
    texts = {
        s.id: [(t.role, t.text) for t in (s.edits.effective_texts() if s.edits else [])]
        for s in compiled.draft.slides
    }
    assert texts == {
        "s3": [("text", "three")],
        "s2": [("text", "two"), ("label", "Moda")],
        "s0": [("text", "zero")],
    }


# ------------------------------------------------- facts reach the agent (KRI-305)


def test_slide_facts_delegates_to_slide_asset_facts(monkeypatch: pytest.MonkeyPatch) -> None:
    seen: list[Any] = []

    def _fake(asset: Any) -> list:
        seen.append(asset)
        return []

    monkeypatch.setattr("app.services.clip_facts.slide_asset_facts", _fake)
    asset = _asset(time="2024-07-01T10:00:00Z", place="Kadıköy")
    assert svc._slide_facts(asset) == []
    assert seen == [asset]

    def _boom(_a: Any) -> list:
        raise RuntimeError("x")

    # a raising helper is "no facts", never a failure
    monkeypatch.setattr("app.services.clip_facts.slide_asset_facts", _boom)
    assert svc._slide_facts(asset) == []


@pytest.mark.asyncio
async def test_run_passes_capture_facts_to_agent_and_orders_and_labels(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    assets = _dated_assets(monkeypatch)
    draft = _draft(assets)
    snap = build_slide_post_snapshot(draft, _by_id(assets), user_id=USER_ID)
    parsed = _parse(snap, [CHRONO, LABEL_PLACE])
    recorded: list[dict] = []

    class _Recorder:
        def run(self, agent_input: EditCopilotInput, ctx: Any = None) -> EditCopilotOutput:
            recorded.append(agent_input.variant_snapshot)
            return parsed

    monkeypatch.setattr("app.agents._model_client.default_client", lambda: object())
    monkeypatch.setattr(svc, "EditCopilotAgent", lambda _client: _Recorder())

    result = await _run(draft, assets)

    slots = recorded[0]["slots"]
    kinds = {i: {f["kind"] for f in slot.get("facts", [])} for i, slot in enumerate(slots)}
    assert {"capture_time", "place"} <= kinds[0]
    assert kinds[1] == set()  # untimed, unplaced slide carries no facts
    assert result.outcome == "edited" and result.draft is not None
    slides = result.draft.slides
    assert [s.id for s in slides] == ["s2", "s1", "s3", "s0"]
    place_labels = {
        s.id: [t.text for t in s.edits.effective_texts() if t.role == "label"] if s.edits else []
        for s in slides
    }
    assert place_labels["s2"] == ["Kadıköy"]
    assert place_labels["s0"] and place_labels["s0"][0].startswith("Beşiktaş")
