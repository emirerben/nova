"""KRI-612: "explain the footages" puts a short line from the footage on every clip.

Prod thread 0ef4fdde (phone montage, 4 clips, 2026-10-11): the creator described the
match day and asked Kria to "explain the footages", then answered "summary". The first
turn asked what "explain the footages" means "for a label operation"; every later turn
was refused with "None of the 4 clips got its own text in this draft".

Ways this can fail (written down before the fix):
1. The resolver answers a label whose attribute is the instruction ("explain the
   footages") with a question, so the creator is asked what "explain" means.
2. The inventory planner reads "a summary" as ONE chapter caption: on a phone montage a
   caption Kria wrote never prints, and it never counts as text on each clip.
3. The inventory planner drops the ask as context: no label exists at all.
4. The resolver writes a value over 3 words, words that are not in the record, or a low
   confidence: the fence drops it and the clip is left without text or sent to vision.
5. The resolver asks for vision on a clip: a paid call, a "still checking" turn, or a
   creator question about a clip whose record already says what happens in it.
6. The resolver labels only the "relevant" clips: the rest get nothing.
7. A clip with an empty record: no line can be written, and the creator is never asked.
8. A footage line cut mid-phrase ("players on") or a lone article.
9. Two clips get the same line: the montage drops the repeat and refuses the render.
10. A false positive: "the dish shown in the clip" and a caption about the weather keep
    today's behaviour (question / vision / chapter caption).
11. A creator-written label or a placeholder is never a description, whatever it says.
12. Turkish ("videoları açıkla", "her klibe kısa bir özet") is understood too; narration
    ("anlatımdaki isimler") is not a description ask.
13. The added label never duplicates one the planner already made, nor its intent id.
14. The draft check counts the lines as text on each clip and reports them as read from
    the footage (inferred), never as the creator's own words.
"""

from __future__ import annotations

import uuid
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from app.agents._runtime import RunContext
from app.agents._schemas.creator_agent import (
    CapabilityAvailability,
    CreativeStrategy,
    ProposeStrategy,
    ResolvedCreatorManifest,
)
from app.agents.clip_intent_planner import ClipIntentPlannerOutput, PlannedClipIntent
from app.agents.clip_question import ClipQuestionAgent
from app.agents.clip_request_resolver import (
    ClipRequestResolverAgent,
    ClipRequestResolverInput,
    ClipRequestResolverOutput,
    ResolverAssignment,
    ResolverIntentOut,
    ResolverVisionQuestion,
)
from app.kria import planner
from app.kria.brief import BriefRequirement, CreativeBrief, wants_clip_descriptions
from app.kria.brief_checks import check_requirement, plan_facts_from_strategy
from app.kria.strategy_policy import CheckedStrategy
from app.schemas.clip_intents import (
    CLIP_DESCRIPTION_ATTRIBUTE,
    ClipIntent,
    asks_clip_description,
    is_clip_description_label,
)
from app.services import clip_intent_planning as planning
from app.services.clip_intent_planning import PlannedIntentResolution
from app.services.clip_intent_resolution import (
    IntentClip,
    IntentResolution,
    grounded_labels,
    resolve_clip_intents_for_turn,
)

REQUEST = (
    "these are the footages of mohammed salahs and trabzonspor football team first match "
    "for the 2026/27 football season there is welcome day of mohammed salah,streching of "
    "the team before the firts match against ferncvaros and there is also some gassed up "
    "funs from Trabzon this match was mohammed salah first game ı want you to collab all "
    "those videos and explain the footages"
)

# The four clip records Kria held for that thread (analysis_only_not_copy, trimmed).
SALAH_RECORDS = {
    "clip-warmup": {
        "subject": "football players on pitch",
        "summary": (
            "Football players walk and jog on the pitch near the corner flag, stretching "
            "and warming up, while spectators fill the stadium stands in the background."
        ),
        "setting": "outdoor football stadium during an event",
        "activity": "football players jogging and stretching on the pitch",
        "notable_moments": [
            {"start_s": 1.75, "end_s": 3.75, "description": "players walking on field"},
            {"start_s": 9.5, "end_s": 10.8, "description": "players gather at corner flag"},
        ],
    },
    "clip-kids": {
        "subject": "children on football pitch",
        "summary": (
            "Two children and an adult are on a football pitch, possibly participating in a "
            "pre-game ceremony, while a large crowd watches from the stands and cheers."
        ),
        "setting": "football stadium",
        "activity": "children waving and acknowledging crowd at a football match",
        "notable_moments": [
            {"start_s": 1.25, "end_s": 4.5, "description": "children waving on pitch"},
        ],
    },
    "clip-fans": {
        "subject": "stadium crowd",
        "summary": (
            "A large crowd in a football stadium cheers enthusiastically. A safety net is "
            "pulled up in front of one section of the stands, then lowered again."
        ),
        "setting": "indoor football stadium",
        "activity": "cheering for a football match",
        "notable_moments": [
            {
                "start_s": 0.0,
                "end_s": 2.5,
                "description": "crowd cheering and safety net deploying",
            },
        ],
    },
    "clip-welcome": {
        "subject": "football player on pitch",
        "summary": (
            "A football player in a jersey is on a pitch, holding up a phone and interacting "
            "with crew members, while a crowd in the background holds up lights."
        ),
        "setting": "football stadium pitch at night",
        "activity": "football player acknowledging crowd and crew while holding up a phone",
        "notable_moments": [
            {
                "start_s": 0.0,
                "end_s": 3.0,
                "description": "player holding phone up, looking at crowd",
            },
        ],
    },
}
CLIP_IDS = list(SALAH_RECORDS)
ALIAS = {media_id: f"m{i + 1:03d}" for i, media_id in enumerate(CLIP_IDS)}

SUMMARY_REQ = BriefRequirement(
    id="r2",
    kind="text",
    scope="per_clip",
    literal=None,
    description="a summary of the match day event",
)
SALAH_BRIEF = CreativeBrief(
    version=2,
    requirements=[
        BriefRequirement(
            id="r1", kind="select", scope="global", description="collab all those videos"
        ),
        SUMMARY_REQ,
    ],
)


def _clips(records: dict[str, dict] = SALAH_RECORDS) -> list[IntentClip]:
    return [
        IntentClip(
            media_id=media_id,
            kind="video",
            analysis={"understanding": record} if record else None,
            gcs_path=f"users/u/clips/{media_id}.mp4",
        )
        for media_id, record in records.items()
    ]


@pytest.fixture(autouse=True)
def _no_vision_no_picker(monkeypatch: pytest.MonkeyPatch) -> None:
    """A description never needs a vision re-check: every record already answers it."""

    def _boom(*args, **kwargs):  # noqa: ANN002, ANN003
        raise AssertionError("no vision call for a clip description")

    monkeypatch.setattr("app.storage.download_to_file", _boom)
    monkeypatch.setattr("app.pipeline.agents.gemini_analyzer.gemini_upload_and_wait", _boom)
    monkeypatch.setattr(ClipQuestionAgent, "run", _boom)
    monkeypatch.setattr("app.config.settings.kria_clip_selection_questions_enabled", False)


def _fake_resolver(monkeypatch: pytest.MonkeyPatch, answer) -> list[ClipRequestResolverInput]:
    """``answer(input)`` -> resolver output; returns every input the resolver saw."""
    seen: list[ClipRequestResolverInput] = []

    def _run(self, input, *, ctx=None):  # noqa: A002, ANN001
        seen.append(input)
        return answer(input)

    monkeypatch.setattr(ClipRequestResolverAgent, "run", _run)
    return seen


def _asks_what_explain_means(input: ClipRequestResolverInput) -> ClipRequestResolverOutput:
    """The 18:46 answer: an intent-level question, no clip at all (failure 1)."""
    return ClipRequestResolverOutput(
        intents=[
            ResolverIntentOut(
                intent_id=intent.intent_id,
                question=(
                    "Please describe what 'explain the footages' means for a label "
                    "operation. Do you want a summary, a play-by-play, or something else?"
                ),
            )
            if intent.op == "label"
            else ResolverIntentOut(
                intent_id=intent.intent_id,
                assignments=[
                    ResolverAssignment(media=clip.alias, confidence=1.0) for clip in input.clips
                ],
            )
            for intent in input.intents
        ]
    )


def _lines(resolution: IntentResolution, intent_id: str) -> dict[str, str]:
    intent = next(i for i in resolution.intents if i.intent_id == intent_id)
    return {a.media_id: a.value for a in intent.assignments}


async def test_explain_question_becomes_a_footage_line_on_every_clip(monkeypatch) -> None:
    _fake_resolver(monkeypatch, _asks_what_explain_means)
    explain = ClipIntent(intent_id="i5", op="label", attribute="explain the footages")

    resolution = await resolve_clip_intents_for_turn(
        intents=[explain], creator_request=REQUEST, clips=_clips(), run_context=RunContext()
    )

    assert resolution.status == "resolved" and resolution.question is None
    assert _lines(resolution, "i5") == {
        "clip-warmup": "Football players jogging",
        "clip-kids": "Children waving",
        "clip-fans": "Stadium crowd",
        "clip-welcome": "Football player",
    }
    # Every line passed the on-screen text fence as words the vision model wrote.
    assert [label.grounding for label in grounded_labels(resolution.intents)] == ["record_span"] * 4


async def test_resolver_lines_win_and_every_gap_is_filled_from_footage(monkeypatch) -> None:
    def answer(input: ClipRequestResolverInput) -> ClipRequestResolverOutput:
        a = {media_id: ALIAS[media_id] for media_id in CLIP_IDS}
        return ClipRequestResolverOutput(
            intents=[
                ResolverIntentOut(
                    intent_id="describe",
                    assignments=[
                        # Not in the record: the fence drops it (failure 4).
                        ResolverAssignment(
                            media=a["clip-warmup"], value="Salah arrives", confidence=0.9
                        ),
                        ResolverAssignment(
                            media=a["clip-kids"], value="Stadium crowd", confidence=0.9
                        ),
                        # The same line twice in a row (failure 9).
                        ResolverAssignment(
                            media=a["clip-fans"], value="Stadium crowd", confidence=0.9
                        ),
                    ],
                    # A vision re-check of a clip the record answers (failure 5).
                    needs_vision=[
                        ResolverVisionQuestion(media=a["clip-welcome"], question="Who is this?")
                    ],
                )
            ]
        )

    _fake_resolver(monkeypatch, answer)
    describe = ClipIntent(intent_id="describe", op="label", attribute=CLIP_DESCRIPTION_ATTRIBUTE)

    resolution = await resolve_clip_intents_for_turn(
        intents=[describe], creator_request=REQUEST, clips=_clips(), run_context=RunContext()
    )

    assert resolution.status == "resolved" and resolution.question is None
    assert _lines(resolution, "describe") == {
        "clip-warmup": "Football players jogging",
        "clip-kids": "Stadium crowd",
        "clip-fans": "Crowd cheering",
        "clip-welcome": "Football player",
    }


async def test_a_clip_with_no_record_is_left_without_text_and_nobody_is_asked(
    monkeypatch,
) -> None:
    _fake_resolver(monkeypatch, _asks_what_explain_means)
    records = {**SALAH_RECORDS, "clip-welcome": {}}
    describe = ClipIntent(intent_id="describe", op="label", attribute=CLIP_DESCRIPTION_ATTRIBUTE)

    resolution = await resolve_clip_intents_for_turn(
        intents=[describe],
        creator_request=REQUEST,
        clips=_clips(records),
        run_context=RunContext(),
    )

    assert resolution.status == "resolved" and resolution.question is None
    assert set(_lines(resolution, "describe")) == {"clip-warmup", "clip-kids", "clip-fans"}


async def test_an_ordinary_label_still_asks_when_the_resolver_cannot_tell(monkeypatch) -> None:
    def answer(input: ClipRequestResolverInput) -> ClipRequestResolverOutput:
        return ClipRequestResolverOutput(
            intents=[ResolverIntentOut(intent_id="dish", question="Which dish do you mean?")]
        )

    _fake_resolver(monkeypatch, answer)
    dish = ClipIntent(intent_id="dish", op="label", attribute="the dish shown in the clip")

    resolution = await resolve_clip_intents_for_turn(
        intents=[dish], creator_request="name the dish", clips=_clips(), run_context=RunContext()
    )

    assert resolution.needs_creator
    assert resolution.intents[0].status == "needs_creator"


def _wire_planner(monkeypatch, *intents: PlannedClipIntent) -> MagicMock:
    agent = MagicMock()
    agent.run.return_value = ClipIntentPlannerOutput(intents=list(intents))
    monkeypatch.setattr(planning, "default_client", MagicMock())
    monkeypatch.setattr(planning, "ClipIntentPlannerAgent", MagicMock(return_value=agent))
    return agent


SUMMARY_CAPTION = PlannedClipIntent(
    intent_id="caption_footages_r2",
    op="caption",
    attribute="the footages",
    caption_attribute="a summary of the match day event",
    source_quote="explain the footages",
)
ALL_CLIPS = PlannedClipIntent(
    intent_id="include_all_clips_r1",
    op="include",
    attribute="all the videos",
    source_quote="collab all those videos",
)


async def _plan(*, describe_clips: bool = True):
    return await planning.plan_and_resolve_clip_intents(
        creator_request=REQUEST,
        latest_user_message="summary",
        candidate_intents=None,
        clips=_clips(),
        run_context=RunContext(),
        describe_clips=describe_clips,
    )


async def test_a_summary_caption_becomes_a_line_on_each_clip(monkeypatch) -> None:
    _wire_planner(monkeypatch, ALL_CLIPS, SUMMARY_CAPTION)
    seen = _fake_resolver(monkeypatch, _asks_what_explain_means)

    planned = await _plan()

    ops = [(i.op, i.attribute) for i in planned.requested_intents]
    assert ops == [("include", "all the videos"), ("label", CLIP_DESCRIPTION_ATTRIBUTE)]
    assert [i.op for i in seen[0].intents] == ["include", "label"]
    assert planned.resolution.status == "resolved" and planned.resolution.question is None
    label = next(i for i in planned.resolution.intents if i.op == "label")
    assert len(label.assignments) == 4


async def test_an_ask_the_planner_dropped_still_gets_its_lines(monkeypatch) -> None:
    _wire_planner(monkeypatch)
    _fake_resolver(monkeypatch, _asks_what_explain_means)

    planned = await _plan()

    assert [i.attribute for i in planned.requested_intents] == [CLIP_DESCRIPTION_ATTRIBUTE]
    assert len(planned.resolution.intents[0].assignments) == 4


async def test_the_planners_own_description_label_is_never_doubled(monkeypatch) -> None:
    own = PlannedClipIntent(
        intent_id="clip-descriptions",
        op="label",
        attribute="explain the footages",
        source_quote="explain the footages",
    )
    _wire_planner(monkeypatch, own, SUMMARY_CAPTION)
    _fake_resolver(monkeypatch, _asks_what_explain_means)

    planned = await _plan()

    assert [(i.intent_id, i.op) for i in planned.requested_intents] == [
        ("clip-descriptions", "label")
    ]


async def test_without_the_brief_ask_a_described_caption_is_left_alone(monkeypatch) -> None:
    weather = PlannedClipIntent(
        intent_id="weather",
        op="caption",
        attribute="the park clips",
        caption_attribute="the weather",
        source_quote="a caption about the weather on the park clips",
    )
    _wire_planner(monkeypatch, weather)
    resolver = AsyncMock(return_value=IntentResolution())
    monkeypatch.setattr(planning, "resolve_clip_intents_for_turn", resolver)

    planned = await _plan(describe_clips=False)

    assert [i.intent_id for i in planned.requested_intents] == ["weather"]
    assert [i.intent_id for i in resolver.await_args.kwargs["intents"]] == ["weather"]


async def test_the_summary_ask_is_met_with_lines_read_from_the_footage(monkeypatch) -> None:
    """The whole server path of the prod turn: brief -> inventory -> resolver -> receipt."""
    _wire_planner(monkeypatch, ALL_CLIPS, SUMMARY_CAPTION)
    _fake_resolver(monkeypatch, _asks_what_explain_means)

    planned = await _plan(describe_clips=wants_clip_descriptions(SALAH_BRIEF))

    strategy = {
        "resolved_clip_intents": [i.model_dump(mode="json") for i in planned.resolution.intents]
    }
    facts = plan_facts_from_strategy(strategy, clip_ids=CLIP_IDS)
    receipt = check_requirement(SUMMARY_REQ, facts)
    assert receipt.status == "met"
    assert receipt.inferred == [
        "Football players jogging",
        "Children waving",
        "Stadium crowd",
        "Football player",
    ]


async def test_the_chat_planner_passes_the_brief_ask_to_clip_planning(monkeypatch) -> None:
    item_id = uuid.uuid4()
    manifest = ResolvedCreatorManifest(
        item_id=str(item_id),
        edit_format="montage",
        render_program="guided",
        capabilities={"dispatch_render": CapabilityAvailability(available=True)},
        context_hash="a" * 64,
        manifest_hash="b" * 64,
    )
    action = ProposeStrategy(
        kind="propose_strategy",
        strategy=CreativeStrategy(
            direction="guided_story",
            edit_format="montage",
            audio_strategy="licensed_music",
            pacing="fast",
            render_program="guided",
            selected_media_ids=[],
            rationale="Salah's first match day.",
        ),
        summary="A match day montage.",
    )
    resolve = AsyncMock(return_value=PlannedIntentResolution([], IntentResolution()))
    monkeypatch.setattr(planner.settings, "clip_intents_enabled", True)
    monkeypatch.setattr(planner.settings, "kria_choice_questions_enabled", False)
    monkeypatch.setattr(planner.settings, "kria_clip_selection_questions_enabled", False)
    monkeypatch.setattr(planner, "plan_and_resolve_clip_intents", resolve)
    monkeypatch.setattr(planner, "_kick_clip_understanding", AsyncMock())
    monkeypatch.setattr(
        planner,
        "check_strategy_for_runtime_v2",
        lambda _manifest, strategy: CheckedStrategy(strategy=strategy, notices=()),
    )

    await planner._plan_from_creator_output(
        SimpleNamespace(),
        thread_id=uuid.uuid4(),
        item_id=item_id,
        creator_id=uuid.uuid4(),
        user_message="summary",
        manifest=manifest,
        inputs=planner._CreatorInputs(
            agent_input=SimpleNamespace(), intent_clips=_clips(), creator_request=REQUEST
        ),
        output=SimpleNamespace(action=action),
        describe_clips=wants_clip_descriptions(SALAH_BRIEF),
    )

    assert resolve.await_args.kwargs["describe_clips"] is True


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("explain the footages", True),
        ("a summary of the match day event", True),
        ("describe what happens in each clip", True),
        ("say what's happening in each video", True),
        (CLIP_DESCRIPTION_ATTRIBUTE, True),
        ("videoları açıkla", True),
        ("her klibe kısa bir özet", True),
        ("the dish shown in the clip", False),
        ("the landmark shown in each clip", False),
        ("the place I described", False),
        ("anlatımdaki isimler", False),
        ("the weather", False),
    ],
)
def test_which_words_ask_for_a_clip_description(text: str, expected: bool) -> None:
    assert asks_clip_description(text) is expected


def test_only_a_footage_label_without_the_creators_words_is_a_description() -> None:
    def label(**kw) -> ClipIntent:
        return ClipIntent(intent_id="x", op="label", attribute="explain each clip", **kw)

    assert is_clip_description_label(label())
    assert not is_clip_description_label(label(creator_text="Explained"))
    assert not is_clip_description_label(label(placeholder=True))
    assert not is_clip_description_label(
        ClipIntent(intent_id="x", op="caption", attribute="explain", caption_attribute="x")
    )


def test_only_a_described_text_on_each_clip_asks_for_descriptions() -> None:
    def brief(**kw) -> CreativeBrief:
        fields = {"id": "r1", "kind": "text", "scope": "per_clip", **kw}
        return CreativeBrief(version=1, requirements=[BriefRequirement(**fields)])

    assert wants_clip_descriptions(SALAH_BRIEF)
    assert not wants_clip_descriptions(None)
    assert not wants_clip_descriptions(brief(description="the landmark shown in each clip"))
    assert not wants_clip_descriptions(brief(description="a summary", literal="Match day"))
    assert not wants_clip_descriptions(brief(scope="title", description="a summary"))
    assert not wants_clip_descriptions(
        brief(description="a summary placeholder", facts={"placeholder": True})
    )
