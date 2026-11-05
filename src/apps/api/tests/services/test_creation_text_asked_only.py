"""Item 22: the text composer may only write the words the creator asked for.

With ``settings.text_composer_asked_only_enabled`` on, a composer ``add_text`` that is not the
creator's own literal (an invented line, or a second copy of her line) is DROPPED from the
program instead of reaching the render. Every model answer is faked; nothing here calls a
paid model.
"""

from __future__ import annotations

import dataclasses
import json

import pytest

from app.agents._runtime import ModelInvocation, RunContext
from app.config import settings
from app.kria.brief import BriefRequirement, CreativeBrief
from app.kria.media_sources import OriginalMediaDescriptor
from app.pipeline.guided_story import GuidedStoryExecutionPlan, compile_proposal_execution_plan
from app.pipeline.phone_guided_plan import compile_phone_guided_plan
from app.pipeline.unified_montage import (
    UnifiedClip,
    brief_view,
    plan_unified_montage,
    skia_font_covers,
)
from app.schemas.edit_proposal import EditProposalSnapshot
from app.services.creation_text_composition import (
    CreationTextCompositionError,
    _lanes,
    compose_creation_text,
    snapshot_copy_constraints,
)
from app.services.kria_editor_ops import apply_text_lane_ops, is_caption_text_bar
from app.services.phone_sources import PhoneSourceBinding

T3 = "The prompt I run at the end of every Claude session"
PROMPT = (
    'Just put this text in the middle for the whole video: "' + T3 + '". Small white text '
    "like TikTok's, no captions and nothing else. Keep the whole clip, don't cut it. "
    "Mute my sound, I'll add a song on TikTok."
)
CTX = RunContext(extra={"skip_agent_run_persist": True})


@pytest.fixture(autouse=True)
def _recorded_prod_windows(monkeypatch):
    monkeypatch.setattr(settings, "kria_compose_unmet_with_ops_enabled", False)


def set_switch(monkeypatch, value: bool) -> None:
    # The field may not exist yet (pydantic refuses setattr for unknown names), so write the
    # instance dict, which works before and after the field is added and is undone on teardown.
    monkeypatch.setitem(settings.__dict__, "text_composer_asked_only_enabled", value)


@pytest.fixture
def switch_on(monkeypatch):
    set_switch(monkeypatch, True)


class Transport:
    def __init__(self, response):
        self.response = response
        self.calls = []

    def invoke(self, **kwargs):
        self.calls.append(kwargs)
        return ModelInvocation(raw_text=json.dumps(self.response))


def transport(monkeypatch, ops):
    client = Transport({"intent": "edit", "confidence": 1, "reply": "Prepared", "ops": ops})
    monkeypatch.setattr("app.services.creation_text_composition.default_client", lambda: client)
    return client


def req(rid="r1", kind="text", scope="global", **kw):
    kw.setdefault("description", "asked text")
    return BriefRequirement(id=rid, kind=kind, scope=scope, **kw)


STYLE_ONLY = [
    req("s1", "style", "global", description="no captions", facts={"captions": "off"}),
    req("s2", "style", "global", description="no other text, nothing else"),
]


def q3_requirements():
    return [
        req(
            "r1",
            "text",
            "global",
            literal=T3,
            description="small white TikTok-style text in the middle for the whole video",
            facts={"position": "middle"},
        ),
        *STYLE_ONLY,
        req(
            "r4",
            "timing",
            "global",
            description="keep the whole clip",
            facts={"keep_whole_take": True},
        ),
        req(
            "r5",
            "audio",
            "global",
            description="mute the clip's own sound",
            facts={"clip_sound": "off"},
        ),
    ]


def clips(n=1):
    return [
        UnifiedClip(
            media_id=f"clip-{i + 1}",
            proxy_path=f"users/u/analysis-proxy-clip-{i + 1}.mp4",
            generation="1",
            duration_s=11.0,
            width=1080,
            height=1920,
            analysis={"best_moments": []},
        )
        for i in range(n)
    ]


def q3_plan():
    """Her case. The pin sits top_center while the brief says "middle", so the planner's bar
    is not proven for the ask and the composer is really called (no proven-ask shortcut)."""
    brief = CreativeBrief(version=1, requirements=q3_requirements())
    strategy = {
        "edit_format": "montage",
        "audio_strategy": "original_audio",
        "direction": "fast_montage",
        "pinned_texts": [{"text": T3, "corner": "top_center"}],
    }
    plan = plan_unified_montage(
        clips(), brief_view(brief), strategy=strategy, font_covers=skia_font_covers
    )
    return plan.snapshot, brief.live()


def bare_plan(n=1, **strategy):
    """A montage with no planned text at all: the asked text exists only in the brief."""
    base = {
        "edit_format": "montage",
        "audio_strategy": "original_audio",
        "direction": "fast_montage",
    }
    return plan_unified_montage(
        clips(n), strategy={**base, **strategy}, font_covers=skia_font_covers
    ).snapshot


def compose(snapshot, reqs, request=PROMPT):
    return compose_creation_text(
        snapshot,
        creator_request=request,
        text_requirements=reqs,
        copy_constraints=snapshot_copy_constraints(snapshot) if reqs is not None else None,
        ctx=CTX,
    )


def bars(snapshot):
    rows = compile_proposal_execution_plan(snapshot)["text_elements"]
    return [row for row in rows if not row.get("removed") and not is_caption_text_bar(row)]


def texts_of(snapshot):
    return [row["text"] for row in bars(snapshot)]


def add(text, start=0.0, end=2.5, **extra):
    return {"op": "add_text", "text": text, "start_s": start, "end_s": end, **extra}


def pin_id(snapshot):
    return next(row["id"] for row in bars(snapshot) if row["id"] == "guided-pinned-0")


def remove(row_id):
    return {
        "op": "remove_texts",
        "selector": {"ids": [row_id]},
        "target_ids": [row_id],
        "expected_count": 1,
    }


def phone_recipe(snapshot):
    execution = GuidedStoryExecutionPlan.model_validate(compile_proposal_execution_plan(snapshot))
    clip = clips()[0]
    binding = PhoneSourceBinding(
        media_id=clip.media_id,
        proxy_path=clip.proxy_path,
        generation=clip.generation,
        original=OriginalMediaDescriptor(
            sha256="a" * 64,
            byte_count=1000,
            duration_s=clip.duration_s,
            width=1080,
            height=1920,
            has_audio=True,
            orientation_degrees=0,
        ),
    )
    return compile_phone_guided_plan(execution, (binding,))


# --- the fixture really reaches the composer ----------------------------------------


def test_q3_base_lane_has_her_line_as_the_pin_and_the_composer_is_called(monkeypatch):
    snapshot, reqs = q3_plan()
    assert texts_of(snapshot) == [T3]
    assert pin_id(snapshot) == "guided-pinned-0"
    client = transport(monkeypatch, [add("Anything", 0, 1)])
    compose(snapshot, reqs)
    assert len(client.calls) == 1


# --- FAIL today: extra text must be dropped, not kept or raised ----------------------


def test_q3_invented_line_is_dropped_and_her_line_stays(monkeypatch, switch_on):
    snapshot, reqs = q3_plan()
    client = transport(monkeypatch, [add("Wait till the end 👀")])
    result = compose(snapshot, reqs)
    assert len(client.calls) == 1
    assert "Wait till the end 👀" not in texts_of(result)
    assert texts_of(result) == [T3]
    recipe = phone_recipe(result)
    assert len(recipe.text_layers) == 1
    assert " ".join(run.text for run in recipe.text_layers[0].runs) == T3


def test_q3_second_copy_of_her_line_is_dropped(monkeypatch, switch_on):
    snapshot, reqs = q3_plan()
    client = transport(monkeypatch, [add(T3, 0, 11)])
    result = compose(snapshot, reqs)
    assert len(client.calls) == 1
    assert texts_of(result) == [T3]
    assert len(phone_recipe(result).text_layers) == 1


def test_title_only_brief_drops_added_text_instead_of_failing(monkeypatch, switch_on):
    snapshot = bare_plan()
    reqs = [req("t1", "text", "title", description="a hook"), *STYLE_ONLY]
    client = transport(monkeypatch, [add("Wait till the end 👀")])
    result = compose(snapshot, reqs, request="Make it feel cinematic")
    assert len(client.calls) == 1
    assert "Wait till the end 👀" not in texts_of(result)


def test_style_only_brief_drops_added_text_instead_of_failing(monkeypatch, switch_on):
    snapshot = bare_plan()
    client = transport(monkeypatch, [add("Wait till the end 👀")])
    result = compose(snapshot, STYLE_ONLY, request="Make it feel cinematic")
    assert len(client.calls) == 1
    assert texts_of(result) == []


def test_same_literal_added_twice_keeps_one(monkeypatch, switch_on):
    snapshot = bare_plan()
    reqs = [req("r1", "text", "global", literal=T3, description="this line")]
    client = transport(monkeypatch, [add(T3, 0, 5), add(T3, 5, 10)])
    result = compose(snapshot, reqs)
    assert len(client.calls) == 1
    assert texts_of(result) == [T3]


def test_superseded_ask_does_not_authorize_added_text(monkeypatch, switch_on):
    snapshot = bare_plan()
    old = req("r1", "text", "global", literal=T3, description="old line", status="superseded")
    client = transport(monkeypatch, [add(T3, 0, 5)])
    result = compose(snapshot, [old, *STYLE_ONLY])
    assert len(client.calls) == 1
    assert texts_of(result) == []


# --- PASS today: the legitimate cases stay green with the switch on -----------------


def test_move_removes_her_pin_and_adds_it_elsewhere(monkeypatch, switch_on):
    snapshot, reqs = q3_plan()
    client = transport(monkeypatch, [remove(pin_id(snapshot)), add(T3, 0, 11)])
    result = compose(snapshot, reqs)
    assert len(client.calls) == 1
    assert texts_of(result) == [T3]


def test_two_asked_lines_are_both_kept(monkeypatch, switch_on):
    snapshot = bare_plan()
    reqs = [
        req("a", "text", "global", literal="First line"),
        req("b", "text", "global", literal="Second line"),
    ]
    client = transport(monkeypatch, [add("First line", 0, 4), add("Second line", 5, 9)])
    result = compose(snapshot, reqs)
    assert len(client.calls) == 1
    assert sorted(texts_of(result)) == ["First line", "Second line"]


def test_literal_split_into_chunks_keeps_both(monkeypatch, switch_on):
    snapshot = bare_plan(2)
    reqs = [req("r1", "text", "clip:clip-1", literal=T3)]
    client = transport(
        monkeypatch,
        [add("The prompt I run", 0, 2), add("at the end of every Claude session", 2, 5)],
    )
    result = compose(snapshot, reqs)
    assert len(client.calls) == 1
    assert sorted(texts_of(result)) == sorted(
        ["The prompt I run", "at the end of every Claude session"]
    )


@pytest.mark.parametrize(
    ("literal", "written"),
    [
        (T3, "THE PROMPT I RUN\nAT THE END OF EVERY CLAUDE SESSION!"),
        ("Kahvaltı İstanbul'da", "KAHVALTI İSTANBUL'DA"),
        ("Kahvaltı İstanbul'da", "Kahvalti Istanbul da"),
        ("Best day ever", "best day ever 😍"),
    ],
)
def test_normalisation_differences_are_still_her_words(monkeypatch, switch_on, literal, written):
    snapshot = bare_plan()
    reqs = [req("r1", "text", "global", literal=literal)]
    client = transport(monkeypatch, [add(written, 0, 4)])
    result = compose(snapshot, reqs)
    assert len(client.calls) == 1
    assert texts_of(result) == [written]


def test_described_ask_lets_the_composer_write_the_copy(monkeypatch, switch_on):
    snapshot = bare_plan(2)
    reqs = [req("r1", "text", "per_clip", description="a label naming the place on each clip")]
    client = transport(monkeypatch, [add("Sagrada Família", 0, 4), add("Park Güell", 5, 9)])
    result = compose(snapshot, reqs)
    assert len(client.calls) == 1
    assert sorted(texts_of(result)) == ["Park Güell", "Sagrada Família"]


def test_per_shot_texts_are_each_kept(monkeypatch, switch_on):
    snapshot = bare_plan(2)
    reqs = [
        req("r1", "text", "per_clip", literal="Shot one", description="first shot text"),
        req("r2", "text", "per_clip", literal="Shot two", description="second shot text"),
    ]
    client = transport(monkeypatch, [add("Shot one", 0, 4), add("Shot two", 5, 9)])
    result = compose(snapshot, reqs)
    assert len(client.calls) == 1
    assert sorted(texts_of(result)) == ["Shot one", "Shot two"]


def test_text_below_the_title_is_kept(monkeypatch, switch_on):
    snapshot = bare_plan(opening_title="Big Title")
    reqs = [
        req("t", "text", "title", literal="Big Title"),
        req(
            "sub",
            "text",
            "global",
            literal="Smaller subtitle",
            # No below_text fact: the hierarchy receipt needs measured layout rows, which a
            # fake op cannot supply; the literal ask is what the switch is judged on.
            description="smaller text below the title",
        ),
    ]
    client = transport(monkeypatch, [add("Smaller subtitle", 0, 4)])
    result = compose(snapshot, reqs, request="Title Big Title with smaller subtitle")
    assert len(client.calls) == 1
    assert "Smaller subtitle" in texts_of(result)


def test_title_sequence_split_keeps_the_pieces(monkeypatch, switch_on):
    words = ["Big", "Bold", "Title"]
    snapshot = bare_plan(opening_title=" ".join(words))
    reqs = [
        req(
            "seq",
            "text_sequence",
            "title",
            literal=" ".join(words),
            facts={"unit": "word", "mode": "replace"},
        )
    ]
    client = transport(
        monkeypatch,
        [{"op": "replace_text_sequence", "selector": {"group": "title"}, "segments": words}],
    )
    result = compose(snapshot, reqs, request="Show each word of the title separately")
    assert len(client.calls) == 1
    assert texts_of(result) == words


def test_no_brief_bound_keeps_the_composer_text(monkeypatch, switch_on):
    snapshot = bare_plan()
    client = transport(monkeypatch, [add("Wait till the end 👀")])
    result = compose_creation_text(
        snapshot, creator_request="Add a teaser line", text_requirements=None, ctx=CTX
    )
    assert len(client.calls) == 1
    assert texts_of(result) == ["Wait till the end 👀"]


def test_inferred_label_rewrite_is_kept(monkeypatch, switch_on):
    from app.pipeline.unified_montage import BriefView

    landmarks = ("Sagrada Família", "Park Güell")
    snapshot = plan_unified_montage(
        [
            dataclasses.replace(
                clip,
                facts=({"kind": "landmark", "value": landmark, "provenance": "inferred"},),
            )
            for clip, landmark in zip(clips(2), landmarks, strict=True)
        ],
        BriefView(wants_per_clip_text=True),
        strategy={"edit_format": "montage", "direction": "fast_montage"},
        font_covers=skia_font_covers,
    ).snapshot
    labels = [row for row in bars(snapshot) if str(row["id"]).startswith("clip-label-")]
    if not labels:
        pytest.skip("simple montage plan plants no inferred label to rewrite")
    client = transport(
        monkeypatch,
        [{"op": "rewrite_text", "selector": {"ids": [labels[0]["id"]]}, "text": "Reworded"}],
    )
    result = compose(snapshot, STYLE_ONLY, request="Make the labels friendlier")
    assert len(client.calls) == 1
    assert "Reworded" in texts_of(result)


def test_pinned_program_replays_as_today(monkeypatch, switch_on):
    snapshot, reqs = q3_plan()
    # Composed once with the switch off: the extra line is part of the stored program.
    set_switch(monkeypatch, False)
    client = transport(monkeypatch, [add("Wait till the end 👀")])
    first = compose(snapshot, reqs)
    assert len(client.calls) == 1
    set_switch(monkeypatch, True)
    restored = EditProposalSnapshot.model_validate_json(first.model_dump_json())
    second = compose(restored, reqs)
    assert len(client.calls) == 1
    assert second.text_composition.operations == first.text_composition.operations
    assert "Wait till the end 👀" in texts_of(second)


def test_switch_off_keeps_the_composer_line_exactly_as_written(monkeypatch):
    snapshot, reqs = q3_plan()
    ops = [add("Wait till the end 👀")]
    client = transport(monkeypatch, ops)
    result = compose(snapshot, reqs)
    assert len(client.calls) == 1
    assert [op["op"] for op in result.text_composition.operations] == ["add_text"]
    assert result.text_composition.operations[0]["text"] == "Wait till the end 👀"
    assert "Wait till the end 👀" in texts_of(result)


def test_switch_off_title_only_add_still_raises(monkeypatch):
    snapshot = bare_plan()
    reqs = [req("t1", "text", "title", description="a hook"), *STYLE_ONLY]
    transport(monkeypatch, [add("Wait till the end 👀")])
    with pytest.raises(CreationTextCompositionError, match="unrequested text"):
        compose(snapshot, reqs, request="Make it feel cinematic")


def test_chat_editor_add_text_is_untouched_by_the_switch(switch_on):
    snapshot = bare_plan()
    texts, slots = _lanes(compile_proposal_execution_plan(snapshot))
    state = apply_text_lane_ops(texts, slots, [add("Whatever the creator typed", 0, 3)])
    assert [row["text"] for row in state.text] == ["Whatever the creator typed"]
