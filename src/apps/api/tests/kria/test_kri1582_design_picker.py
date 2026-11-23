"""KRI-1582: the creator picks the informative text design (A1, A2, A3, B1) before research.

Ways this can fail, written before the code:

1. Flag off, the gate asks anything new or changes the draft (byte-identical guard).
2. The question reaches a build that cannot draw a design (below protocol 55), promising a
   look the phone will draw as plain text.
3. A 55 build gets version 2 (its parser drops the whole card), or a 56 build gets no
   previews, or previews whose layers are not the design (A2 without its scrim, the hook in
   the wrong face, a font the app cannot find).
4. The recommended option is not the agent's default (B1 for a subtitle ask anywhere in her
   messages, A2 otherwise), so "Let Kria decide" and an unanswered question disagree.
5. The tapped pick never reaches the snapshot, so research writes the default's shape; or the
   research basis does not move with the pick, so research cached for B1 is pinned under A2.
6. An unanswered question holds approval or is asked again on every turn.
7. A typed "A2" is not read as the answer.
8. Asked where the pick changes nothing: no research ask, research written as text layers, a
   route that does not research before approval.
9. Her heading colour, saved in the editor after the pick, is undone by a later restyle of
   the same design or a switch to another one.
10. The iOS UI fixture drifts from what the server sends.

The gate is the real `planner._gate_unresolved_choices` (DB reads stubbed), the previews the
real resolver and phone compile, the snapshot the real unified montage planner, the Save the
real `prepare_editor_commit`.
"""

from __future__ import annotations

import copy
import json
import uuid
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from app.agents._schemas.creator_agent import CreativeStrategy, ProposeStrategy
from app.config import settings
from app.kria import planner, research_preview
from app.kria.brief import BriefRequirement, CreativeBrief
from app.kria.planner import PlannedKriaTurn
from app.kria.reply_language import reply_language_for
from app.kria.text_design_ask import MAX_PREVIEWS_BYTES, with_design_previews
from app.pipeline.informative_text import POLICY_KEY, SHAPES_KEY, creator_fields
from app.pipeline.text_designs import HEADING_COLOR, TEXT_DESIGNS
from app.pipeline.unified_montage import BriefView, UnifiedClip, plan_unified_montage
from app.services.choice_questions import (
    CONFLICT_TEXT_DESIGN,
    ChoiceCapability,
    open_conflicts,
    tag_event,
    text_design_answer,
)
from app.services.client_protocol import (
    INFORMATIVE_TEXT_DESIGNS_PROTOCOL,
    TEXT_DESIGN_PICKER_PROTOCOL,
)

CREATOR = uuid.uuid4()
_FIXTURES = Path(__file__).resolve().parents[1] / "fixtures"
_KYOTO = _FIXTURES / "kri1585_kyoto"
_IOS_FIXTURE = Path(__file__).resolve().parents[3] / "ios/Tests/Fixtures/KRI1582DesignPicker.json"
_ASK = "Bu Kyoto'daki Toroko treni tren hakkında bilgilendirici alt yazı istiyorum"
_INFO_ASK = "Bu Kyoto'daki Toroko treni hakkında bilgilendirici yazılar istiyorum"
# The editor palette's blue swatch: the colour the XCUITest picks for a heading.
_EDITED_HEADING = "#9BCAFF"


def _research_ask(quote: str = _ASK, *, rid: str = "r1", **extra) -> BriefRequirement:  # noqa: ANN003
    return BriefRequirement(
        id=rid,
        kind="text",
        scope="global",
        description="Informative text about the Torokko train",
        facts={"research": {"topic": "Kyoto'daki Toroko treni", "request_quote": quote}},
        **extra,
    )


def _brief(*requirements: BriefRequirement) -> CreativeBrief:
    return CreativeBrief(version=1, requirements=list(requirements or (_research_ask(),)))


def _snapshot(count: int = 4, *, proxies: bool = True) -> dict:
    return {
        "clip_assignments": [
            {"media_id": f"kyoto-{i}", "kind": "video", "duration_s": 9.0} for i in range(count)
        ],
        "clip_paths": [
            f"users/u/{'analysis-proxy-' if proxies else ''}kyoto-{i}.mp4" for i in range(count)
        ],
    }


def _planned(protocol: int | None, **strategy: object) -> PlannedKriaTurn:
    base: dict = {
        "direction": "guided_story",
        "edit_format": "montage",
        "audio_strategy": "licensed_music",
        "pacing": "balanced",
        "render_program": "guided",
        "rationale": "A montage with sourced facts.",
    }
    plan = planner.adapt_creator_action(
        ProposeStrategy(
            kind="propose_strategy",
            strategy=CreativeStrategy(**{**base, **strategy}),
            summary="A montage.",
        ),
    )
    return PlannedKriaTurn(
        plan=plan, manifest_hash="a" * 64, context_hash="b" * 64, client_protocol=protocol
    )


def _said(text: str) -> tuple[str, dict]:
    return tag_event("user", {}, "user_message", text)


@pytest.fixture
def designs_on(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(settings, "informative_text_designs_enabled", True)
    monkeypatch.setattr(settings, "phone_text_styling_enabled", True)
    monkeypatch.setattr(settings, "kria_choice_questions_enabled", True)
    monkeypatch.setattr(
        settings,
        "phone_render_verified_features",
        [*settings.phone_render_verified_features, "textDesigns"],
    )


async def _gate(
    monkeypatch: pytest.MonkeyPatch,
    *,
    protocol: int | None = TEXT_DESIGN_PICKER_PROTOCOL,
    brief: CreativeBrief | None = None,
    events: tuple = (),
    snapshot: dict | None = None,
    language: str | None = "tr",
    **strategy: object,
) -> PlannedKriaTurn:
    monkeypatch.setattr(planner, "load_latest_brief", AsyncMock(return_value=brief or _brief()))
    monkeypatch.setattr(type(settings), "creative_brief_for", lambda _s, _i: True)
    monkeypatch.setattr(type(settings), "brief_binding_for", lambda _s, _i: True)
    monkeypatch.setattr(type(settings), "phone_rendering_for", lambda _s, _i: True)
    history = list(events) or [_said(_ASK)]
    monkeypatch.setattr(planner, "_load_thread_events", AsyncMock(return_value=history))
    planned = replace(_planned(protocol, **strategy), media_snapshot=snapshot or _snapshot())
    with reply_language_for(language):
        return await planner._gate_unresolved_choices(
            SimpleNamespace(), planned, thread_id=uuid.uuid4(), creator_id=CREATOR
        )


def _question(result: PlannedKriaTurn) -> dict:
    assert result.plan.turn_value == "question", result.plan.response
    question = result.plan.choice_question
    assert question and question["kind"] == CONFLICT_TEXT_DESIGN, question
    return question


def _picked(question: dict, option: str) -> tuple[str, dict]:
    return tag_event(
        "user",
        {"choice_selection": {"question_id": question["question_id"], "option_key": option}},
        "user_message",
        option,
    )


def _strategy(result: PlannedKriaTurn) -> dict:
    assert result.plan.mode == "act", result.plan.response
    return result.plan.intents[0].arguments["strategy"]


# -- 1. flag off ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_flag_off_the_gate_asks_nothing_and_keeps_the_draft(monkeypatch):
    monkeypatch.setattr(settings, "informative_text_designs_enabled", False)
    monkeypatch.setattr(settings, "phone_text_styling_enabled", True)
    monkeypatch.setattr(settings, "kria_choice_questions_enabled", True)
    before = _planned(TEXT_DESIGN_PICKER_PROTOCOL).plan.intents[0].arguments["strategy"]
    result = await _gate(monkeypatch)
    assert result.plan.mode == "act"
    assert _strategy(result) == before
    assert "choice_answers" not in _strategy(result)


# -- 2/3. who is asked, and in which version ------------------------------------------------


@pytest.mark.asyncio
async def test_a_build_that_cannot_draw_designs_is_not_asked(monkeypatch, designs_on):
    result = await _gate(monkeypatch, protocol=INFORMATIVE_TEXT_DESIGNS_PROTOCOL - 1)
    assert result.plan.mode == "act"


@pytest.mark.asyncio
async def test_the_designs_build_before_the_picker_gets_text_only_cards(monkeypatch, designs_on):
    question = _question(await _gate(monkeypatch, protocol=INFORMATIVE_TEXT_DESIGNS_PROTOCOL))
    assert question["version"] == 1
    assert [option["key"] for option in question["options"]] == ["A1", "A2", "A3", "B1"]
    assert not any("preview" in option for option in question["options"])
    assert [o["key"] for o in question["options"] if o["recommended"]] == ["B1"]


@pytest.mark.asyncio
async def test_the_picker_build_gets_a_preview_drawn_in_each_design(monkeypatch, designs_on):
    result = await _gate(monkeypatch)
    question = _question(result)
    assert result.plan.mode == "respond" and not result.plan.intents
    assert question["version"] == 2
    fonts_dir = Path(__file__).resolve().parents[2] / "assets" / "fonts"
    for option in question["options"]:
        design = TEXT_DESIGNS[option["key"]]
        preview = option["preview"]
        assert preview["media_id"] == "kyoto-0" and preview["media_kind"] == "video"
        assert preview["at_s"] == pytest.approx(3.6)
        layers = preview["text_layers"]
        texts = [" ".join(run["text"] for run in layer["runs"]) for layer in layers]
        assert texts[0].startswith("Kyoto")  # her topic opens as the hook
        expected = 2 if design.mode == "running_guide" else 3
        assert len(layers) == expected, texts
        assert all(layer["start"] <= preview["time_s"] < layer["end"] for layer in layers)
        faces = {
            preview["fonts"][run["font_asset_id"]] for layer in layers for run in layer["runs"]
        }
        assert all((fonts_dir / face).is_file() for face in faces), faces
        hook_face = preview["fonts"][layers[0]["runs"][0]["font_asset_id"]]
        assert hook_face.startswith(design.hook.font_family.replace(" ", "")[:6]), hook_face
        scrims = [layer for layer in layers if layer.get("scrim")]
        assert len(scrims) == (1 if design.scrim else 0), option["key"]
        assert all(layer.get("srgb_blend") for layer in layers)
    assert len(json.dumps(question["options"]).encode()) <= MAX_PREVIEWS_BYTES
    text = result.plan.response
    assert "Yazılar hangi görünümde olsun?" in text and "Akan anlatım (önerilen)" in text


# -- 4. the default ------------------------------------------------------------------------


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("messages", "quote", "default"),
    [
        ((_ASK,), _ASK, "B1"),
        ((_INFO_ASK,), _INFO_ASK, "A2"),
        # The quote research picked dropped the word; her message still asks for subtitles.
        ((_ASK,), _INFO_ASK, "B1"),
        (("Bilgilendirici yazılar olsun ama altyazı istemiyorum",), _INFO_ASK, "A2"),
    ],
)
async def test_the_recommended_design_is_the_default_research_would_write(
    monkeypatch, designs_on, messages, quote, default
):
    question = _question(
        await _gate(
            monkeypatch,
            protocol=INFORMATIVE_TEXT_DESIGNS_PROTOCOL,
            brief=_brief(_research_ask(quote)),
            events=tuple(_said(message) for message in messages),
        )
    )
    assert [o["key"] for o in question["options"] if o["recommended"]] == [default]


# -- 5. the pick reaches the snapshot, research and its cache key ---------------------------


@pytest.mark.asyncio
async def test_a_tapped_design_reaches_the_snapshot_research_writes_for(monkeypatch, designs_on):
    asked = await _gate(monkeypatch)
    question = _question(asked)
    picked = await _gate(
        monkeypatch,
        events=(_said(_ASK), ("assistant", {"choice_question": question}), _picked(question, "A2")),
    )
    strategy = _strategy(picked)
    assert text_design_answer(strategy) == "A2"
    [answer] = strategy["choice_answers"]
    assert answer["kind"] == CONFLICT_TEXT_DESIGN and answer["option"] == "A2"
    # The design styles the research ask; it does not answer it.
    assert answer["requirement_ids"] == []

    clips = [
        UnifiedClip(
            media_id=f"kyoto-{i}", proxy_path=f"private/kyoto-{i}.mp4", generation="1", duration_s=9
        )
        for i in range(3)
    ]
    plan = plan_unified_montage(clips, BriefView(target_duration_s=24), strategy=strategy)
    assert plan.snapshot.text_design == "A2"
    unpicked = plan_unified_montage(clips, BriefView(target_duration_s=24), strategy={})
    assert "text_design" not in unpicked.snapshot.model_dump(mode="json")

    seen: list = []

    def research(*_args, text_design=None, **_kwargs):  # noqa: ANN002, ANN003, ANN202
        seen.append(text_design)
        raise research_preview.ResearchNotFinished("stop once research was asked")

    from app.services import phone_montage_preflight, researched_text

    monkeypatch.setattr(phone_montage_preflight, "preflight_montage_plan", lambda **_: (plan, {}))
    monkeypatch.setattr(researched_text, "run_researched_revision", research)
    inputs = research_preview.ResearchInputs(
        creator_id="creator-1",
        thread_id="thread-1",
        item_id="item-1",
        edit_format="montage",
        brief=_brief(),
        strategy=strategy,
        media_snapshot={},
        creator_request=_ASK,
    )
    with pytest.raises(research_preview.ResearchNotFinished):
        research_preview.prepare(inputs, visuals=[])
    assert seen == ["A2"]

    def basis(strategy: dict) -> str:
        found = research_preview.research_requirement(_brief())
        assert found is not None
        return research_preview.basis_digest(
            found[1], strategy, _snapshot(), creator_request=_ASK, brief=_brief(), language="tr"
        )

    b1 = copy.deepcopy(strategy)
    b1["choice_answers"][0]["option"] = "B1"
    assert basis(strategy) != basis(b1)


# -- 6/7. unanswered, typed --------------------------------------------------------------


@pytest.mark.asyncio
async def test_an_unanswered_question_is_asked_once_and_never_holds_approval(
    monkeypatch, designs_on
):
    question = _question(await _gate(monkeypatch))
    moved_on = await _gate(
        monkeypatch,
        events=(
            _said(_ASK),
            ("assistant", {"choice_question": question}),
            _said("Videoyu biraz daha kısa yap"),
        ),
    )
    strategy = _strategy(moved_on)
    assert text_design_answer(strategy) is None  # the default stands, research picks it
    capability = ChoiceCapability(
        creator_id=CREATOR, client_protocol=TEXT_DESIGN_PICKER_PROTOCOL, creator_request=_ASK
    )
    with reply_language_for("tr"):
        assert not open_conflicts(strategy, _brief(), _snapshot(), capability)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("typed", "key"), [("A3", "A3"), ("a2", "A2"), ("Akan anlatım", "B1"), ("ikinci", "A2")]
)
async def test_a_typed_design_answers_the_question(monkeypatch, designs_on, typed, key):
    """The runtime turns a typed reply into the answer with `match_open_choice`."""
    from app.services.choice_questions import match_open_choice

    question = _question(await _gate(monkeypatch))
    assert match_open_choice(question, typed) == key


# -- 8. never asked where the pick changes nothing --------------------------------------------


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("label", "brief", "snapshot", "strategy"),
    [
        (
            "no research ask",
            _brief(BriefRequirement(id="r1", kind="text", scope="global", description="a title")),
            None,
            {},
        ),
        ("research written as two text layers", None, None, {}),
        ("not a phone montage", None, _snapshot(proxies=False), {}),
        ("a voiceover lane", None, None, {"audio_strategy": "voiceover"}),
    ],
)
async def test_not_asked_where_the_pick_changes_nothing(
    monkeypatch, designs_on, label, brief, snapshot, strategy
):
    if label == "research written as two text layers":
        brief = _brief(
            BriefRequirement(
                id="r1",
                kind="text",
                scope="global",
                description="Facts at the bottom",
                facts={
                    "research": {"topic": "Kyoto'daki Toroko treni", "request_quote": _ASK},
                    "position": "bottom",
                },
            ),
            BriefRequirement(
                id="r2",
                kind="text",
                scope="global",
                description="An English translation on top",
                facts={"position": "top"},
            ),
        )
    result = await _gate(monkeypatch, brief=brief, snapshot=snapshot, **strategy)
    assert not (
        result.plan.choice_question
        and result.plan.choice_question.get("kind") == CONFLICT_TEXT_DESIGN
    ), label


# -- 9. her heading colour sticks -------------------------------------------------------------


def _design_rows(design: str) -> list[dict]:
    from app.agents._schemas.text_element import TextElement

    rows = json.loads((_KYOTO / "designed_rows.json").read_text())["designs"][design]
    return [TextElement.model_validate(row).model_dump(mode="json") for row in rows]


def _restyle(rows: list[dict]) -> list[dict]:
    from app.pipeline.phone_text_style import resolve_phone_text_style

    policy = json.loads((_KYOTO / "plan.json").read_text())["variant"]["phone_text_style"]
    return resolve_phone_text_style(
        copy.deepcopy(rows),
        preferences=policy["preferences"],
        requested=policy["requested"],
        selected_fonts=policy["selected_fonts"],
        informative=True,
        shapes=True,
    )


def _switched(rows: list[dict], design: str) -> list[dict]:
    out = copy.deepcopy(rows)
    for row in out:
        row["source_params"]["text_design"] = design
    return out


def test_her_heading_colour_saved_after_the_pick_sticks_through_a_restyle(monkeypatch, designs_on):
    """The iPhone Save (`prepare_editor_commit`) of the picked A2 edit with one heading recoloured,
    then the next style pass over the saved rows: the same design, and a switch to A1."""
    import app.routes.generative_jobs as gj
    from app.pipeline.portable_text_layout import compile_text_overlay
    from app.services.phone_text_styling import PHONE_TEXT_STYLE_FIELD
    from tests.routes.test_editor_commit import _arm, _narrated_guided_job

    _arm(monkeypatch)
    monkeypatch.setattr(settings, "guided_story_editor_v2_enabled", True)
    monkeypatch.setattr(gj, "_TEXT_ELEMENTS_ENABLED", True)
    picked = [row for row in _restyle(_design_rows("A2")) if row["end_s"] <= 25]
    job = _narrated_guided_job()
    job.assembly_plan["guided_story_execution_plan"]["text_elements"].extend(copy.deepcopy(picked))
    variant = job.assembly_plan["variants"][0]
    variant["text_elements"].extend(copy.deepcopy(picked))
    variant[PHONE_TEXT_STYLE_FIELD] = {
        "version": 1,
        "preferences": {},
        "requested": {},
        POLICY_KEY: True,
        SHAPES_KEY: True,
    }
    revision = gj._guided_v2_revision(job, variant)
    elements = copy.deepcopy(revision["text_elements"])
    heading = next(row for row in elements if row["id"] == "kri1585-fact-0-heading")
    assert heading["color"] == HEADING_COLOR
    heading["color"] = _EDITED_HEADING
    gj.prepare_editor_commit(
        job,
        variant["variant_id"],
        gj.EditorCommitRequest(
            base_generation=revision["base_generation"],
            guided_revision_number=1,
            text_elements=elements,
        ),
    )
    saved = [
        row
        for row in job.assembly_plan["variants"][0]["text_elements"]
        if row["id"].startswith("kri1585-")
    ]
    by_id = {row["id"]: row for row in saved}
    assert creator_fields(by_id["kri1585-fact-0-heading"]) == {"color"}
    assert not creator_fields(by_id["kri1585-fact-1-heading"])

    for design in ("A2", "A1"):
        restyled = {row["id"]: row for row in _restyle(_switched(saved, design))}
        assert restyled["kri1585-fact-0-heading"]["color"] == _EDITED_HEADING, design
        assert restyled["kri1585-fact-1-heading"]["color"] == HEADING_COLOR, design
        assert (
            restyled["kri1585-fact-0-heading"]["font_family"]
            == TEXT_DESIGNS[design].heading.font_family
        ), design
    from app.agents._schemas.text_element import TextElement
    from app.pipeline.canvas import PORTRAIT
    from app.pipeline.generative_overlays import build_overlays_from_text_elements

    overlays = build_overlays_from_text_elements(
        [TextElement.model_validate(by_id["kri1585-fact-0-heading"])],
        video_duration_s=60,
        independent_box_alignment=True,
    )
    layer, _font = compile_text_overlay(overlays[0], layer_id="text-0", canvas=PORTRAIT)
    fill = layer.runs[0].fill
    assert (round(fill.red * 255), round(fill.green * 255), round(fill.blue * 255)) == (
        0x9B,
        0xCA,
        0xFF,
    )


# -- 10. the iOS UI fixture is what the server sends ------------------------------------------


def _ios_fixture(monkeypatch: pytest.MonkeyPatch) -> dict:
    """What `DesignPickerUITestFixture` serves, all from the server's own code: the design question
    as the gate builds it for the app's fixture project (one fixture clip on the phone, an English
    chat, a subtitle ask) with its plain text, and each design's rows as the resolver draws them on
    the editor fixture's 6 s timeline (a hook, then a heading over its detail, or a guide line)."""
    from app.agents._schemas.text_element import TextElement
    from app.pipeline.phone_text_style import resolve_phone_text_style
    from app.services.choice_questions import (
        build_choice_question,
        choice_question_text,
        collect_conflicts,
    )

    monkeypatch.setattr(type(settings), "phone_rendering_for", lambda _s, _i: True)
    snapshot = {
        "clip_assignments": [{"media_id": "fixture-clip", "kind": "video", "duration_s": 8.0}],
        "clip_paths": ["users/u/analysis-proxy-fixture-clip.mp4"],
    }
    brief = CreativeBrief(
        version=1,
        requirements=[
            BriefRequirement(
                id="r1",
                kind="text",
                scope="global",
                description="Informative subtitles about the Lisbon tram",
                facts={
                    "research": {
                        "topic": "The Lisbon tram",
                        "request_quote": "informative subtitles about the Lisbon tram",
                    }
                },
            )
        ],
    )
    capability = ChoiceCapability(
        creator_id=CREATOR,
        client_protocol=TEXT_DESIGN_PICKER_PROTOCOL,
        creator_request="Add informative subtitles about the Lisbon tram",
    )
    strategy = {"edit_format": "montage", "audio_strategy": "licensed_music"}
    with reply_language_for("en"):
        [choice] = [
            c
            for c in collect_conflicts(strategy, brief, snapshot, capability)
            if c.kind == CONFLICT_TEXT_DESIGN
        ]
        candidate = choice.candidate()
        question = with_design_previews(
            build_choice_question(candidate, question_id="design-q-fixture"),
            brief=brief,
            media_snapshot=snapshot,
            client_protocol=TEXT_DESIGN_PICKER_PROTOCOL,
        )
        text = choice_question_text(candidate)

    def rows(design: str) -> list[dict]:
        def row(row_id: str, words: str, start: float, end: float, level: str) -> dict:
            pair = {"pair": 0} if level in ("heading", "detail") else {}
            tag = {"id": design, "level": level, **pair}
            source = {"text_design": design, "research": {"design": tag}}
            if level == "hook":
                source["phone_text_role"] = "hook"
            return {
                "id": row_id,
                "text": words,
                "start_s": start,
                "end_s": end,
                "role": "generative_intro",
                "effect": "static",
                "position": "middle",
                "text_language": "en",
                "source_params": source,
            }

        written = [row("design-hook", "The Lisbon tram", 0.0, 2.0, "hook")]
        if TEXT_DESIGNS[design].mode == "running_guide":
            written.append(row("design-line", "Tram 28 climbs the old town", 2.5, 6.0, "line"))
        else:
            written += [
                row("design-heading", "Tram 28", 2.5, 6.0, "heading"),
                row(
                    "design-detail", "It climbs through the old town since 1914", 2.5, 6.0, "detail"
                ),
            ]
        written = [TextElement.model_validate(r).model_dump(mode="json") for r in written]
        return resolve_phone_text_style(written, informative=True, shapes=True)

    return {
        "question": question,
        "text": text,
        "editor_rows": {design: rows(design) for design in TEXT_DESIGNS},
    }


def _shape(fixture: dict) -> dict:
    """What the app reads, without the floats Skia lays out differently on macOS and Linux."""
    question = fixture["question"]
    return {
        "text": fixture["text"],
        "version": question["version"],
        "conflict": question["conflict"],
        "options": [
            {
                "key": option["key"],
                "label": option["label"],
                "recommended": option["recommended"],
                "media_id": option["preview"]["media_id"],
                "fonts": sorted(option["preview"]["fonts"].values()),
                "layers": [
                    {
                        "text": " ".join(run["text"] for run in layer["runs"]),
                        "scrim": bool(layer.get("scrim")),
                        "srgb_blend": bool(layer.get("srgb_blend")),
                    }
                    for layer in option["preview"]["text_layers"]
                ],
            }
            for option in question["options"]
        ],
        "editor_rows": {
            design: [
                {key: row.get(key) for key in ("id", "text", "font_family", "color", "scrim")}
                for row in rows
            ]
            for design, rows in fixture["editor_rows"].items()
        },
    }


def test_the_ios_ui_fixture_is_what_the_server_sends(monkeypatch, designs_on):
    fresh = _ios_fixture(monkeypatch)
    heading = next(r for r in fresh["editor_rows"]["A2"] if r["id"] == "design-heading")
    assert heading["color"] == HEADING_COLOR and _EDITED_HEADING != HEADING_COLOR
    if _IOS_FIXTURE.exists() and not _regenerate():
        stored = json.loads(_IOS_FIXTURE.read_text())
        assert _shape(stored) == _shape(fresh), (
            "regenerate: KRI1582_REGENERATE=1 pytest tests/kria/test_kri1582_design_picker.py"
        )
        return
    _IOS_FIXTURE.write_text(json.dumps(fresh, indent=1, ensure_ascii=False) + "\n")
    if not _regenerate():
        pytest.fail(f"wrote {_IOS_FIXTURE}; commit it")


def _regenerate() -> bool:
    import os

    return os.environ.get("KRI1582_REGENERATE") == "1"


# -- the approval card reads a heading and its detail as one fact (decision 2.28) ------------


def _prepared(design: str) -> object:
    from app.schemas.research import PreparedResearch

    def source(domain: str) -> dict:
        return {"title": domain, "url": f"https://{domain}/torokko"}

    def text(words: str, start: float, end: float, level: str, pair: int | None, domain: str):
        tag = {"id": design, "level": level, **({"pair": pair} if pair is not None else {})}
        fact = {"id": "fact-6", "text": "7.3 km, 25 dakika", "sources": [source(domain)]}
        return {
            "text": words,
            "media_ids": ["kyoto-0"],
            "start_s": start,
            "end_s": end,
            "sources": [source(domain)],
            "facts": [fact],
            "design": tag,
        }

    rows = [text("Kyoto'nun en güzel 25 dakikası", 0.0, 3.4, "hook", None, "kyoto.travel")]
    if design == "B1":
        rows.append(text("Saga'dan Kameoka'ya 7,3 km", 4.0, 9.0, "line", None, "jnto.go.jp"))
    else:
        rows += [
            text("7,3 km", 4.0, 9.0, "heading", 0, "jnto.go.jp"),
            text("Saga'dan Kameoka'ya, yaklaşık 25 dakika", 4.0, 9.0, "detail", 0, "sagano.co.jp"),
        ]
    return PreparedResearch(
        status="ready",
        requirement_id="r1",
        topic="Kyoto'daki Toroko treni",
        request_quote=_ASK,
        basis_digest="0" * 64,
        texts=rows,
    )


def test_the_approval_card_lists_a_heading_and_its_detail_as_one_fact():
    payload = research_preview.preview_payload(_prepared("A2"))
    assert [row["text"] for row in payload["explanations"]] == [
        "Kyoto'nun en güzel 25 dakikası",
        "7,3 km\nSaga'dan Kameoka'ya, yaklaşık 25 dakika",
    ]
    assert [s["url"] for s in payload["explanations"][1]["sources"]] == [
        "https://jnto.go.jp/torokko",
        "https://sagano.co.jp/torokko",
    ]
    with reply_language_for("tr"):
        lines = research_preview.preview_lines(_prepared("A2")).splitlines()
    assert lines[2] == (
        "- 0:04–0:09 · “7,3 km” / “Saga'dan Kameoka'ya, yaklaşık 25 dakika” · "
        "Kaynak: jnto.go.jp, sagano.co.jp"
    )
    guide = research_preview.preview_payload(_prepared("B1"))
    assert [row["text"] for row in guide["explanations"]] == [
        "Kyoto'nun en güzel 25 dakikası",
        "Saga'dan Kameoka'ya 7,3 km",
    ]
