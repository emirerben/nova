"""KRI-523: text that stays on screen for the whole video in a named corner.

Prod thread 2026-10-08: "Add the title 'Free to do in Lisbon' and 'Part 1' ... they should stay
the whole video ... bottom left. On the top left, write 'Miradouro de Santa Catarina'."
The Main Creator had no field for corner/whole-video text, offered a workaround, and the tap
on that workaround dead-ended in "I couldn't reliably read every requested change".

Failure modes this file is written against (before the code):

* the pins reach the strategy but never the snapshot, so the render silently has no text;
* the brief's own text literals are not in the plan facts, so the bound-brief receipts block
  the render ("That exact text isn't in this draft");
* the same words burn twice: once pinned, once as the centred opening title that the brief's
  title/global literal would otherwise produce;
* the "what are the title's words?" gate asks for words the creator already pinned;
* a pin the creator never wrote is burned on every frame;
* pins collide with the per-clip label lane at the bottom;
* a snapshot WITHOUT pins changes (hash, record, compiled text) -- must stay byte-identical;
* a whole-video bar follows a clip when the editor trims a single-clip video;
* a planner failure is reported as a reading failure.

The render side is the real `plan_unified_montage` -> receipts -> `compile_execution_plan` ->
`compile_phone_guided_plan`, so "the text is on screen the whole video" is checked on the
recipe the phone would draw.
"""

from __future__ import annotations

import uuid

import pytest

from app.agents._schemas.creator_agent import CreativeStrategy
from app.kria import planner
from app.kria.brief import BriefRequirement, BriefUpdateBatchError, CreativeBrief
from app.kria.brief_checks import build_receipts, plan_facts_from_unified_montage
from app.kria.media_sources import OriginalMediaDescriptor
from app.pipeline.guided_story import GuidedStoryExecutionPlan, compile_execution_plan
from app.pipeline.phone_guided_plan import compile_phone_guided_plan
from app.pipeline.unified_montage import brief_view, plan_unified_montage, title_source_exists
from app.schemas.edit_proposal import PinnedText
from app.services.phone_sources import PhoneSourceBinding
from tests.pipeline.test_unified_montage import clip

LISBON = [
    {"text": "Free to do in Lisbon", "corner": "bottom_left"},
    {"text": "Part 1", "corner": "bottom_left"},
    {"text": "Miradouro de Santa Catarina", "corner": "top_left"},
]


def _brief(*literals: str, title_scope: bool = False) -> CreativeBrief:
    return CreativeBrief(
        version=1,
        requirements=[
            BriefRequirement(
                id=f"r{i}",
                kind="text",
                scope="title" if title_scope and i == 0 else "global",
                description="on-screen text",
                literal=literal,
            )
            for i, literal in enumerate(literals)
        ],
    )


def _plan(strategy: dict, brief: CreativeBrief | None = None, count: int = 3):  # noqa: ANN202
    return plan_unified_montage(
        [clip(i, minutes=i) for i in range(count)],
        brief_view(brief) if brief is not None else None,
        strategy=strategy,
    )


def _compiled(plan) -> dict:  # noqa: ANN001
    return compile_execution_plan(plan.guided_edit(), track=None)


def _pinned(compiled: dict) -> list[dict]:
    return [e for e in compiled["text_elements"] if e["id"].startswith("guided-pinned-")]


# -- the incident, end to end ------------------------------------------------------------


def test_lisbon_request_renders_three_pinned_lines_for_the_whole_video() -> None:
    brief = _brief(*(pin["text"] for pin in LISBON))
    plan = _plan({"pinned_texts": LISBON}, brief)

    # Nothing is asked and nothing blocks the render.
    receipts = build_receipts(
        brief.live(), plan_facts_from_unified_montage(plan.record()), include_unchecked=True
    )
    assert [(r.status, r.verification) for r in receipts] == [("met", "checked")] * 3

    # The brief's global text literals did not become a second, centred title.
    assert plan.title is None and plan.snapshot.opening_title is None
    compiled = _compiled(plan)
    assert all(e["id"] != "guided-title" for e in compiled["text_elements"])

    pins = _pinned(compiled)
    assert [p["text"] for p in pins] == [pin["text"] for pin in LISBON]
    total = plan.duration_s
    assert all(p["start_s"] == 0.0 and p["end_s"] == pytest.approx(total) for p in pins)
    # Left-aligned at the margin; "Free to do in Lisbon" sits ABOVE "Part 1" in the bottom
    # zone, and the top-left line is above both.
    assert {p["x_frac"] for p in pins} == {0.08}
    assert {p["alignment"] for p in pins} == {"left"}
    lisbon, part, miradouro = pins
    assert miradouro["y_frac"] < lisbon["y_frac"] < part["y_frac"]
    assert part["y_frac"] <= 0.90 and miradouro["y_frac"] >= 0.10  # inside the safe band

    # The phone draws exactly those lines, for the whole video.
    bindings = tuple(
        PhoneSourceBinding(
            media_id=f"c{i}",
            proxy_path=f"users/u/analysis-proxy-c{i}.mp4",
            generation="1",
            original=OriginalMediaDescriptor(
                sha256="a" * 64,
                byte_count=1000,
                duration_s=5,
                width=1920,
                height=1080,
                has_audio=True,
            ),
        )
        for i in range(3)
    )
    recipe = compile_phone_guided_plan(GuidedStoryExecutionPlan.model_validate(compiled), bindings)
    layers = [layer for layer in recipe.text_layers if layer.id.startswith("text-")]
    assert sorted(" ".join(run.text for run in layer.runs) for layer in layers) == sorted(
        pin["text"] for pin in LISBON
    )
    assert all(layer.start == 0.0 and layer.end == pytest.approx(total) for layer in layers)


def test_pins_move_clip_labels_out_of_the_bottom_zone() -> None:
    labelled = [clip(i, minutes=i, place=f"Place {i}, Town") for i in range(3)]
    brief = CreativeBrief(
        version=1,
        requirements=[
            BriefRequirement(id="r0", kind="text", scope="per_clip", description="the location")
        ],
    )

    def label_ys(strategy: dict) -> set[float]:
        plan = plan_unified_montage(labelled, brief_view(brief), strategy=strategy)
        return {
            e["y_frac"]
            for e in _compiled(plan)["text_elements"]
            if e["id"].startswith("clip-label")
        }

    assert label_ys({}) == {0.78}
    assert label_ys({"pinned_texts": [LISBON[1]]}) == {0.70}
    # A top-only pin leaves the label lane where it was.
    assert label_ys({"pinned_texts": [LISBON[2]]}) == {0.78}


def test_a_label_corner_and_a_bottom_pin_do_not_share_the_bottom_zone() -> None:
    """KRI-522 lets the creator anchor per-clip labels in a corner (bottom-left is the common
    one); a bottom pin lives in the same zone, so labels rise out of it and keep their anchor."""
    from app.schemas.text_style_intent import LABEL_ANCHORS

    labelled = [clip(i, minutes=i, place=f"Place {i}, Town") for i in range(3)]
    brief = CreativeBrief(
        version=1,
        requirements=[
            BriefRequirement(id="r0", kind="text", scope="per_clip", description="the location")
        ],
    )

    def labels(strategy: dict) -> list[dict]:
        plan = plan_unified_montage(labelled, brief_view(brief), strategy=strategy)
        return [e for e in _compiled(plan)["text_elements"] if e["id"].startswith("clip-label")]

    x, y, alignment = LABEL_ANCHORS["bottom_left"]
    plain = labels({"label_position": "bottom_left"})
    assert {(e["x_frac"], e["y_frac"], e["alignment"]) for e in plain} == {(x, y, alignment)}
    assert y > 0.70  # the premise: the corner anchor is inside the bottom zone
    pinned = labels({"label_position": "bottom_left", "pinned_texts": [LISBON[1]]})
    assert {(e["x_frac"], e["alignment"]) for e in pinned} == {(x, alignment)}
    assert {e["y_frac"] for e in pinned} == {0.70}
    # A top-anchored label is not in the way of a bottom pin.
    top_x, top_y, _ = LABEL_ANCHORS["top_left"]
    top = labels({"label_position": "top_left", "pinned_texts": [LISBON[1]]})
    assert {(e["x_frac"], e["y_frac"]) for e in top} == {(top_x, top_y)}


# -- no pins: byte-identical -------------------------------------------------------------


def test_a_snapshot_without_pins_is_unchanged() -> None:
    plan = _plan({"opening_title": "Weekend away"})
    assert "pinned_texts" not in plan.snapshot.model_dump(mode="json", exclude_none=True)
    assert "pinned_texts" not in plan.record()
    assert _pinned(_compiled(plan)) == []
    assert "pinned_texts" not in CreativeStrategy().model_dump(mode="json")


def test_the_apply_strategy_tool_schema_does_not_grow() -> None:
    assert "pinned_texts" not in CreativeStrategy.model_json_schema()["properties"]


# -- title handling ----------------------------------------------------------------------


def test_a_title_literal_that_is_pinned_is_not_burned_a_second_time() -> None:
    brief = _brief("Free to do in Lisbon", title_scope=True)
    plan = _plan({"pinned_texts": [LISBON[0]]}, brief)
    assert plan.title is None and plan.title_source == "none"


def test_a_different_title_literal_still_stands_next_to_the_pins() -> None:
    brief = _brief("Lisbon on a budget", title_scope=True)
    plan = _plan({"pinned_texts": [LISBON[0]]}, brief)
    assert plan.title == "Lisbon on a budget"


def test_a_confirmed_opening_title_is_kept() -> None:
    plan = _plan({"opening_title": "Lisbon", "pinned_texts": [LISBON[1]]})
    assert plan.title == "Lisbon"


def test_the_title_gate_still_asks_when_the_title_has_no_words_of_its_own() -> None:
    """Pins are separate lines: a wordless title requirement stays unanswered (the render
    would otherwise block on its receipt after approval), exactly as the renderer's `_title`
    says. A title literal that differs from the pins is an answer; one that IS a pin is not
    a second title."""
    wordless = CreativeBrief(
        version=1,
        requirements=[
            BriefRequirement(id="r0", kind="text", scope="title", description="a hook title")
        ],
    )
    assert not title_source_exists({}, wordless)
    assert not title_source_exists({"pinned_texts": LISBON}, wordless)
    assert title_source_exists(
        {"pinned_texts": LISBON}, _brief("Lisbon on a budget", title_scope=True)
    )
    assert not title_source_exists(
        {"pinned_texts": LISBON}, _brief("Free to do in Lisbon", title_scope=True)
    )


# -- grounding ---------------------------------------------------------------------------


def test_only_text_the_creator_wrote_is_pinned() -> None:
    pins = [PinnedText(**pin) for pin in LISBON]
    sources = ["Add the title Free to do in Lisbon and Part 1, bottom left."]
    kept, dropped = planner.ground_pinned_texts(pins, evidence="", user_sources=sources)
    assert [pin.text for pin in kept] == ["Free to do in Lisbon", "Part 1"]
    assert dropped == 1  # "Miradouro de Santa Catarina" was never written by the creator

    # The model's quoted evidence grounds a pin only when it is itself the creator's words.
    quote = "write Miradouro de Santa Catarina"
    kept, dropped = planner.ground_pinned_texts(
        pins, evidence=quote, user_sources=[*sources, f"On the top left, {quote}"]
    )
    assert dropped == 0
    kept, dropped = planner.ground_pinned_texts(pins, evidence=quote, user_sources=sources)
    assert dropped == 1


def test_pin_limits_and_corners_are_enforced() -> None:
    with pytest.raises(ValueError):
        CreativeStrategy(pinned_texts=[{"text": "x", "corner": "middle"}])
    with pytest.raises(ValueError):
        CreativeStrategy(pinned_texts=[{"text": "x", "corner": "top_left"}] * 5)
    with pytest.raises(ValueError):
        CreativeStrategy(pinned_texts=[{"text": "   ", "corner": "top_left"}])


def test_grounding_survives_smart_punctuation_and_rejects_fragments() -> None:
    pins = [PinnedText(text="Don't miss it", corner="top_left")]
    kept, dropped = planner.ground_pinned_texts(
        pins, evidence="", user_sources=["Write \u201cDon\u2019t  miss it\u201d top left"]
    )
    assert (len(kept), dropped) == (1, 0)  # a phone's curly quote still grounds the copy

    # A one-letter pin, and a pin that only occurs inside another word, are not grounded.
    sources = ["Make a fun video of my Lisbon trip"]
    tiny = [PinnedText(text="a", corner="top_left"), PinnedText(text="fun", corner="top_left")]
    kept, dropped = planner.ground_pinned_texts(tiny, evidence="", user_sources=sources)
    assert [pin.text for pin in kept] == ["fun"] and dropped == 1
    inside = [PinnedText(text="Lisb", corner="top_left")]
    assert planner.ground_pinned_texts(inside, evidence="", user_sources=sources)[1] == 1


def test_a_malformed_action_envelope_is_a_schema_error_not_a_crash() -> None:
    """`ValidationError.loc` is empty for an invalid envelope; the title-hold retry hint must
    not index into it (it would escape the agent runtime's retry handling)."""
    from app.agents.main_creator import MainCreatorAgent, SchemaError
    from tests.agents.test_main_creator_agent import _input

    agent = MainCreatorAgent(None)
    for raw in ('{"action":{"kind":"nope"}}', '{"action":null}', "{}"):
        with pytest.raises(SchemaError):
            agent.parse(raw, _input())


def test_a_too_long_title_hold_points_the_retry_at_pinned_texts() -> None:
    import json

    from app.agents.main_creator import MainCreatorAgent, SchemaError
    from tests.agents.test_main_creator_agent import _input

    agent = MainCreatorAgent(None)
    raw = json.dumps(
        {
            "action": {
                "kind": "propose_strategy",
                "strategy": {
                    "direction": "guided_story",
                    "edit_format": "montage",
                    "audio_strategy": "licensed_music",
                    "render_program": "guided",
                    "target_duration_s": 24,
                    "opening_title": "Free to do in Lisbon",
                    "opening_title_duration_s": 24,
                    "rationale": "x",
                },
                "summary": "x",
            }
        }
    )
    with pytest.raises(SchemaError):
        agent.parse(raw, _input())
    assert "pinned_texts" in agent.schema_clarification()


def test_each_corner_is_its_own_stack_and_a_pin_gets_room_for_one_line() -> None:
    plan = _plan(
        {
            "pinned_texts": [
                {"text": "A", "corner": "bottom_left"},
                {"text": "B", "corner": "bottom_right"},
                {"text": "C", "corner": "top_left"},
                {"text": "D", "corner": "top_right"},
            ]
        }
    )
    by_text = {p["text"]: p for p in _pinned(_compiled(plan))}
    # Left and right corners share a row; only lines in the SAME corner stack (the Lisbon
    # test covers a two-line column).
    assert by_text["A"]["y_frac"] == by_text["B"]["y_frac"]
    assert by_text["C"]["y_frac"] == by_text["D"]["y_frac"] < by_text["A"]["y_frac"]
    assert by_text["B"]["x_frac"] == 0.92 and by_text["B"]["alignment"] == "right"
    # Two sides share a zone, so each keeps to its own half; a lone side may use the width.
    assert by_text["A"]["max_width_frac"] == 0.42
    lone = _pinned(_compiled(_plan({"pinned_texts": [LISBON[0]]})))[0]
    assert lone["max_width_frac"] == pytest.approx(0.84)


def test_narration_captions_leave_the_bottom_zone_to_bottom_pins() -> None:
    from types import SimpleNamespace

    from app.pipeline.guided_story import _narration_caption_elements
    from app.schemas.edit_proposal import NarrationTrack

    track = NarrationTrack(
        gcs_path="voice",
        generation="1",
        duration_s=2,
        words=[
            {"text": "In", "start_s": 0, "end_s": 0.5},
            {"text": "Lisbon", "start_s": 0.5, "end_s": 1},
        ],
    )

    def caption_ys(pinned) -> set[float]:  # noqa: ANN001
        snapshot = SimpleNamespace(narration=track, font_family=None, pinned_texts=pinned)
        return {cue["y_frac"] for cue in _narration_caption_elements(snapshot)}

    bottom = [PinnedText(text="Part 1", corner="bottom_left")]
    top = [PinnedText(text="Part 1", corner="top_left")]
    assert caption_ys(None) == caption_ys(top) == {0.82}
    assert caption_ys(bottom) == {0.70}


def test_the_editor_title_selector_never_resolves_to_a_pinned_line() -> None:
    from app.services.kria_editor_ops_text import classify

    def bar(bar_id: str, start: float = 0.0) -> dict:
        return {
            "id": bar_id,
            "text": bar_id,
            "role": "generative_intro",
            "clip_id": None,
            "removed": False,
            "start_s": start,
            "caption": False,
        }

    kinds = classify([bar("guided-pinned-0"), bar("guided-pinned-1")])
    assert set(kinds.values()) == {"text"}  # no title exists, and a pin is not one
    kinds = classify([bar("guided-pinned-0"), bar("guided-title")])
    assert kinds == {"guided-pinned-0": "text", "guided-title": "title"}


def test_pinned_texts_survive_the_apply_strategy_tool_boundary() -> None:
    """The model-authored field is hidden from the tool JSON schema but must still validate."""
    from app.agents._schemas.creator_agent import ProposeStrategy
    from app.kria.registry import KRIA_TOOLS

    strategy = CreativeStrategy(
        direction="guided_story",
        edit_format="montage",
        audio_strategy="licensed_music",
        pacing="fast",
        render_program="guided",
        rationale="A montage.",
        pinned_texts=LISBON,
    )
    plan = planner.adapt_creator_action(
        ProposeStrategy(kind="propose_strategy", strategy=strategy, summary="A montage."),
    )
    arguments = plan.intents[0].arguments
    assert arguments["strategy"]["pinned_texts"] == LISBON
    tool = KRIA_TOOLS.get(plan.intents[0].tool_name, plan.intents[0].tool_version)
    parsed = tool.arguments_model.model_validate(arguments)
    assert [pin.text for pin in parsed.strategy.pinned_texts] == [pin["text"] for pin in LISBON]


# -- planner failure copy ----------------------------------------------------------------


def test_a_planner_failure_does_not_claim_the_request_was_unreadable() -> None:
    from types import SimpleNamespace

    manifest = SimpleNamespace(manifest_hash="a" * 64, context_hash="b" * 64)
    turn = planner._request_recovery(manifest, None, reason="creator_planning_failed")
    text = turn.plan.response
    assert "couldn't reliably read" not in text
    assert "Which clip or part of the edit" not in text
    assert "saved" in text and "draft is unchanged" in text
    old = planner._request_recovery(manifest, None, reason="request_extraction_failed")
    assert "couldn't reliably read" in old.plan.response  # the extractor branch is unchanged


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("error", "reason"),
    [
        (
            RuntimeError("Kria could not produce a reliable editorial plan"),
            "creator_planning_failed",
        ),
        (BriefUpdateBatchError("change without brief version"), "request_extraction_failed"),
    ],
)
async def test_main_creator_failure_on_an_answer_turn_names_the_real_cause(
    monkeypatch: pytest.MonkeyPatch, error: Exception, reason: str
) -> None:
    from structlog.testing import capture_logs

    from app.config import settings
    from tests.kria.test_planner_editor_target_miss import _wire_real

    db, item, creator_id, _runs, _copilot = _wire_real(monkeypatch, render_status="ready")
    monkeypatch.setattr(settings, "kria_brief_binding_enabled", True)

    class Failing:
        def __init__(self, _client) -> None:  # noqa: ANN001
            pass

        def run(self, *_a, **_k):  # noqa: ANN002, ANN003
            raise error

    monkeypatch.setattr(planner, "MainCreatorAgent", Failing)
    with capture_logs() as logs:
        result = await planner.plan_live_turn(
            db,
            thread_id=uuid.uuid4(),
            item_id=item._fields["id"],
            creator_id=creator_id,
            user_message="Opening title & label",
            answers_clip_question=True,
        )
    assert (result.brief_coverage or {})["reason"] == reason
    # Only a genuine reading failure may say the request could not be read.
    assert ("couldn't reliably read" in result.plan.response) == (
        reason == "request_extraction_failed"
    )
    assert any(e["event"] == "kria_request_recovery_cause" for e in logs)
