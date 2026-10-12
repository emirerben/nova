"""KRI-593: a Voiceover edit keeps every attached clip unless the creator limited them.

Prod thread f77a5c16 (2026-10-11, turn b9a5d1e9): seven clips (river, kitchen, loft
bed, Deniz at the laptop, Deniz's bedroom, bathroom, boardwalk) and a 22 s Turkish
voiceover that talks about all of them. The creator asked to open on the river and
to show three clips when she says "Deniz", "kırkayak" and "mutfak". The Main Creator
answered `media_scope: "selected"` with only the four clips her cues named, the phone
rendered 4 of 7, and the reply never said three were left out. A cue says WHERE a
clip goes, not WHICH clips exist.

Each case runs the recorded turn through the REAL Main Creator parse and the REAL
runtime-v2 planner (`_plan_from_creator_output`), then hands the drafted strategy to
the REAL native dispatch selection (`_creator_selected_clip_paths`), under the
production flag profile. Only the model text and the clip-intent resolver (its
recorded answer) are stand-ins.

Ways the fix could go wrong, written down before the code (each has a case):

1. A creator who DID limit the clips gets every clip back: "only the kitchen and
   river clips", "skip the bathroom", "banyo klibini kullanma", "sadece ... kullan",
   "use 4 clips", "mutfak hariç".
2. A selection ask that only the Creative Brief still remembers (kind `select`) is
   ignored once it has scrolled out of the chat window.
3. A style negation reads as a clip limit, so the fix never fires: "Başlık dışında
   başka yazı olmasın" (the prod message), "no other text except the title",
   "without music".
4. Visuals (`asset-…`) or the creator's own photos are pushed into the native clip
   set (the phone voiceover edit only takes the project's own videos as clips).
5. Another format changes: a montage's pick of its strongest clips stays the model's.
6. More clips than one native edit takes (12): no silent cut to an arbitrary 12.
7. The widened set never reaches the phone: dispatch still hands the worker 4 clips,
   or the "start with the river" order is lost.
"""

from __future__ import annotations

import copy
import json
import uuid
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock

import pytest

from app.agents.main_creator import MainCreatorAgent, MainCreatorInput
from app.kria import planner
from app.kria.brief import BriefRequirement, CreativeBrief
from app.kria.voiceover_clips import creator_limits_clips
from app.schemas.clip_intents import ClipIntent, ResolvedClipIntent
from app.services.clip_intent_planning import PlannedIntentResolution
from app.services.clip_intent_resolution import IntentClip, IntentResolution
from app.tasks import content_plan_build

FIXTURE = Path(__file__).resolve().parents[1] / "fixtures" / "kri593_tigre_voiceover_turn.json"
CASE: dict[str, Any] = json.loads(FIXTURE.read_text())
MANIFEST = CASE["main_creator_input"]["capability_manifest"]
OWN_VIDEOS = [m["media_id"] for m in MANIFEST["media"] if m["kind"] == "video"]
CUE_CLIPS = list(CASE["model_strategy"]["selected_media_ids"])
BATHTUB_PHOTO = "asset-fe1c1382-89e1-440e-b54d-f3a4d945f4ff"
RIVER = "analysis-proxy-ios-ECCB47EC-0C2C-4F64-917F-85FB8039C9D7.mp4"
KITCHEN = "analysis-proxy-ios-80DCD337-53E9-426D-8DC8-31AEC9359F76.mp4"
LAPTOP = "analysis-proxy-ios-CFBEC1AE-2523-49F2-B675-04576EAED824.mp4"
BATHROOM = "analysis-proxy-ios-A9859821-B374-42D6-9F07-F9C4734506C7.mp4"
PROD_FIRST_MESSAGE = CASE["main_creator_input"]["conversation"][1]["content"]
PROD_LATEST_MESSAGE = CASE["main_creator_input"]["user_message"]


def _raw_output(selected: list[str], *, titled: bool, **strategy_update: Any) -> dict[str, Any]:
    strategy = {**copy.deepcopy(CASE["model_strategy"]), **strategy_update}
    strategy["selected_media_ids"] = selected
    action: dict[str, Any] = {
        "kind": "propose_strategy",
        "strategy": strategy,
        "summary": CASE["model_summary"],
    }
    if titled:
        action["render_intent_evidence"] = {"opening_title": "Başlık: “Tigre’de bir gün”"}
    else:
        # The title is the prod creator's own words; a variant that never wrote them
        # gets no title (an ungrounded one would turn the reply into a wording question).
        strategy["opening_title"] = None
    return {"action": action, "brief_updates": []}


def _agent_input(messages: list[str], manifest: dict[str, Any]) -> MainCreatorInput:
    data = copy.deepcopy(CASE["main_creator_input"])
    data["capability_manifest"] = manifest
    data["user_message"] = messages[-1]
    conversation = [data["conversation"][0]]
    for index, message in enumerate(messages):
        if index:
            conversation.append({"role": "assistant", "content": "Tamam."})
        conversation.append({"role": "user", "content": message})
    data["conversation"] = conversation
    return MainCreatorInput.model_validate(data)


def _recorded_resolution() -> PlannedIntentResolution:
    resolved = [ResolvedClipIntent.model_validate(row) for row in CASE["resolved_clip_intents"]]
    requested = [ClipIntent.model_validate(row) for row in CASE["model_strategy"]["clip_intents"]]
    return PlannedIntentResolution(requested, IntentResolution(intents=resolved))


async def _draft(
    monkeypatch: pytest.MonkeyPatch,
    *,
    selected: list[str],
    messages: list[str] | None = None,
    manifest: dict[str, Any] | None = None,
    brief: CreativeBrief | None = None,
    **strategy_update: Any,
) -> dict[str, Any]:
    """One recorded planning turn; returns the strategy the draft would apply."""

    messages = messages or [PROD_FIRST_MESSAGE, PROD_LATEST_MESSAGE]
    manifest = manifest or MANIFEST
    agent_input = _agent_input(messages, manifest)
    raw = _raw_output(selected, titled=PROD_FIRST_MESSAGE in messages, **strategy_update)
    output = MainCreatorAgent(model_client=None).parse(json.dumps(raw), agent_input)
    monkeypatch.setattr(
        planner, "plan_and_resolve_clip_intents", AsyncMock(return_value=_recorded_resolution())
    )
    monkeypatch.setattr(planner, "_load_thread_events", AsyncMock(return_value=[]))
    monkeypatch.setattr(planner, "_kick_clip_understanding", AsyncMock())
    monkeypatch.setattr(type(planner.settings), "brief_binding_for", lambda _s, _i: True)
    clips = [
        IntentClip(media_id=m["media_id"], kind=m["kind"], analysis=None) for m in manifest["media"]
    ]
    kwargs: dict[str, Any] = {}
    if brief is not None:
        kwargs["brief_selects_clips"] = planner._brief_selects_clips(brief)
    planned = await planner._plan_from_creator_output(
        SimpleNamespace(),
        thread_id=uuid.uuid4(),
        item_id=uuid.UUID(manifest["item_id"]),
        creator_id=uuid.uuid4(),
        user_message=messages[-1],
        manifest=agent_input.capability_manifest,
        inputs=planner._CreatorInputs(
            agent_input=agent_input,
            intent_clips=clips,
            creator_request=None,
            raw_creator_request="\n".join(messages),
        ),
        output=output,
        brief_request=CASE["main_creator_input"]["creator_request"],
        **kwargs,
    )
    assert planned.plan.mode == "act", planned.plan.response
    apply_intent = planned.plan.intents[0]
    assert apply_intent.tool_name == "draft.apply_strategy"
    return apply_intent.arguments["strategy"]


def _dispatched_clips(strategy: dict[str, Any]) -> list[str]:
    """The REAL native dispatch selection + intent order, over the item's own clips."""

    root = "users/creator/creation-threads/thread/"
    item = SimpleNamespace(
        id=uuid.uuid4(),
        clip_assignments=[
            {"media_id": media_id, "gcs_path": root + media_id} for media_id in OWN_VIDEOS
        ],
    )
    paths = [root + media_id for media_id in OWN_VIDEOS]
    paths = content_plan_build._creator_selected_clip_paths(item, paths, strategy)
    paths, _placed = content_plan_build._creator_intent_clip_order(item, paths, strategy)
    return [path.removeprefix(root) for path in paths]


async def test_tigre_voiceover_cues_keep_all_seven_clips(prod_profile, monkeypatch) -> None:
    """The prod turn: cue clips only -> every attached video, river still first."""

    strategy = await _draft(monkeypatch, selected=CUE_CLIPS)

    assert set(strategy["selected_media_ids"]) == set(OWN_VIDEOS)
    assert BATHTUB_PHOTO not in strategy["selected_media_ids"]
    # The bathtub photo still pops in on "kırkayak"; it is a beat, never a clip.
    assert strategy["reaction_beats"][0]["visual_id"] == BATHTUB_PHOTO
    dispatched = _dispatched_clips(strategy)
    assert sorted(dispatched) == sorted(OWN_VIDEOS)
    assert dispatched[0] == RIVER


@pytest.mark.parametrize(
    "messages",
    [
        # 3: style negations name text, never footage.
        ["Title: 'A day in Tigre'. No other text except the title. Start with the river clip."],
        ["Make it without music. When I say Deniz, show Deniz at the laptop."],
        ["Altyazı olmasın. Nehir klibiyle başla."],
        # PR 1549 review: a negated removal keeps the clip, so every clip stays.
        ["don't take out the loft clip"],
        ["do not hide the loft clip"],
        ["Never remove the loft clip. Start with the river."],
        ["Çatı katı klibini çıkarma."],
        ["Çatı katı klibini gizleme."],
    ],
)
async def test_text_and_audio_negations_still_keep_every_clip(
    prod_profile, monkeypatch, messages
) -> None:
    strategy = await _draft(monkeypatch, selected=CUE_CLIPS, messages=messages)

    assert set(strategy["selected_media_ids"]) == set(OWN_VIDEOS)


@pytest.mark.parametrize(
    ("messages", "selected"),
    [
        (["Only use the river, kitchen and laptop clips."], [RIVER, KITCHEN, LAPTOP]),
        (
            ["Skip the bathroom. Start with the river clip."],
            [c for c in OWN_VIDEOS if c != BATHROOM],
        ),
        (["Use 4 clips and start with the river."], CUE_CLIPS),
        (["Sadece nehir, mutfak ve Deniz kliplerini kullan."], [RIVER, KITCHEN, LAPTOP]),
        (
            [PROD_FIRST_MESSAGE, "Banyo klibini kullanma."],
            [c for c in OWN_VIDEOS if c != BATHROOM],
        ),
        # ("hepsini kullan" would make the parse demand the all-media contract instead.)
        (["Mutfak hariç diğerlerini kullan."], [c for c in OWN_VIDEOS if c != KITCHEN]),
        # The limit was said turns ago; it still holds on a later, unrelated message.
        (
            ["Leave out the bathroom.", PROD_FIRST_MESSAGE, PROD_LATEST_MESSAGE],
            [c for c in OWN_VIDEOS if c != BATHROOM],
        ),
        # PR 1549 external review: common removal forms with no brief select ask.
        (["take out the bathroom clip"], [c for c in OWN_VIDEOS if c != BATHROOM]),
        (["hide the bathroom clip"], [c for c in OWN_VIDEOS if c != BATHROOM]),
        (["banyo klibini dahil etme"], [c for c in OWN_VIDEOS if c != BATHROOM]),
    ],
)
async def test_a_creator_who_limited_the_clips_keeps_that_subset(
    prod_profile, monkeypatch, messages, selected
) -> None:
    strategy = await _draft(monkeypatch, selected=selected, messages=messages)

    assert strategy["selected_media_ids"] == selected
    assert sorted(_dispatched_clips(strategy)) == sorted(selected)


@pytest.mark.parametrize(
    "text",
    [
        # PR 1549 external review.
        "take out the bathroom clip",
        "hide the bathroom clip",
        "banyo klibini dahil etme",
        # The same family.
        "take the bathroom clip out",
        "ditch the bathroom clip",
        "throw out the bathroom clip",
        "toss the dark clips",
        "keep the bathroom clip out of it",
        "the bathroom clip should be hidden",
        "banyo klibi dahil olmasın",
        "banyo klibini gizle",
        "banyo klibini dışarıda bırak",
        "banyo klibini çıkart",
    ],
)
def test_common_removal_wordings_limit_the_clips(text: str) -> None:
    assert creator_limits_clips([text], own_clip_count=len(OWN_VIDEOS))


@pytest.mark.parametrize(
    "text",
    [
        # Removal words about text, audio or the finished video, not the footage.
        "take out the music",
        "hide the title",
        "altyazıyı gizle",
        "müziği dahil etme",
        "Start with the river clip and take your time",
        # A negated removal keeps the clip (PR 1549 review).
        "don't take out the loft clip",
        "do not hide the loft clip",
        "Don't skip the bathroom clip",
        "never remove the loft clip",
        "please don't leave the loft clip out",
        "you shouldn't drop the dark clips",
        "çatı katı klibini çıkarma",
        "çatı katı klibini gizleme",
        "banyo klibini atlama",
        "çatı katı klibini dahil etmeyi unutma",
    ],
)
def test_removal_words_about_text_or_audio_do_not_limit_the_clips(text: str) -> None:
    assert not creator_limits_clips([text], own_clip_count=len(OWN_VIDEOS))


async def test_a_brief_selection_ask_keeps_the_subset(prod_profile, monkeypatch) -> None:
    """2: the chat window lost the ask, the brief still carries it."""

    brief = CreativeBrief(
        version=2,
        requirements=[
            BriefRequirement(
                id="r8",
                kind="select",
                scope="global",
                description="Banyo klibi kullanılmayacak",
            )
        ],
    )
    selected = [c for c in OWN_VIDEOS if c != BATHROOM]

    strategy = await _draft(monkeypatch, selected=selected, brief=brief)

    assert strategy["selected_media_ids"] == selected


async def test_own_photos_and_visuals_are_never_added_as_clips(prod_profile, monkeypatch) -> None:
    """4: only the project's own videos are added; a selected own photo stays selected."""

    photo = "analysis-proxy-ios-PHOTO-1.jpg"
    manifest = copy.deepcopy(MANIFEST)
    manifest["media"].insert(0, {"kind": "image", "label": None, "media_id": photo})

    strategy = await _draft(monkeypatch, selected=CUE_CLIPS, manifest=manifest)

    assert set(strategy["selected_media_ids"]) == set(OWN_VIDEOS)
    assert photo not in strategy["selected_media_ids"]
    assert BATHTUB_PHOTO not in strategy["selected_media_ids"]


async def test_a_montage_keeps_the_models_pick(prod_profile, monkeypatch) -> None:
    """5: only Voiceover edits change; a montage's strongest-clips pick is editorial."""

    strategy = await _draft(
        monkeypatch,
        selected=CUE_CLIPS,
        edit_format="montage",
        audio_strategy="original_audio",
        reaction_beats=None,
    )

    assert strategy["selected_media_ids"] == CUE_CLIPS


async def test_more_clips_than_one_edit_takes_keeps_the_models_pick(
    prod_profile, monkeypatch
) -> None:
    """6: 13 videos cannot all fit the native 12; never cut to an arbitrary 12."""

    manifest = copy.deepcopy(MANIFEST)
    extra = [
        {
            "kind": "video",
            "label": None,
            "media_id": f"analysis-proxy-ios-EXTRA-{i}.mp4",
            "duration_s": 4.0,
        }
        for i in range(6)
    ]
    manifest["media"][len(OWN_VIDEOS) : len(OWN_VIDEOS)] = extra

    strategy = await _draft(monkeypatch, selected=CUE_CLIPS, manifest=manifest)

    assert strategy["selected_media_ids"] == CUE_CLIPS


# PR 1549 review: every supported removal form, plain and negated, so polarity can't
# regress one verb at a time.
_REMOVAL_FORMS = [
    "skip {x}",
    "exclude {x}",
    "omit {x}",
    "ignore {x}",
    "drop {x}",
    "remove {x}",
    "delete {x}",
    "cut out {x}",
    "lose {x}",
    "ditch {x}",
    "toss {x}",
    "hide {x}",
    "leave out {x}",
    "leave {x} out",
    "take out {x}",
    "take {x} out",
    "keep {x} out",
    "throw out {x}",
    "throw {x} away",
    "get rid of {x}",
]
_TR_REMOVAL_PAIRS = [
    ("banyo klibini çıkar", "banyo klibini çıkarma"),
    ("banyo klibini atla", "banyo klibini atlama"),
    ("banyo klibini sil", "banyo klibini silme"),
    ("banyo klibini kaldır", "banyo klibini kaldırma"),
    ("banyo klibini gizle", "banyo klibini gizleme"),
    ("banyo klibini dışarıda bırak", "banyo klibini dışarıda bırakma"),
    ("banyo klibini kullanma", "banyo klibini kullan"),
    ("banyo klibini dahil etme", "banyo klibini dahil et"),
]


@pytest.mark.parametrize("form", _REMOVAL_FORMS)
@pytest.mark.parametrize("negator", ["don't", "do not", "never"])
def test_each_removal_form_limits_and_its_negation_does_not(form: str, negator: str) -> None:
    removal = form.format(x="the bathroom clip")
    count = len(OWN_VIDEOS)

    assert creator_limits_clips([removal], own_clip_count=count)
    assert not creator_limits_clips([f"{negator} {removal}"], own_clip_count=count)


@pytest.mark.parametrize(("removal", "kept"), _TR_REMOVAL_PAIRS)
def test_each_turkish_removal_limits_and_its_negation_does_not(removal: str, kept: str) -> None:
    count = len(OWN_VIDEOS)

    assert creator_limits_clips([removal], own_clip_count=count)
    assert not creator_limits_clips([kept], own_clip_count=count)


@pytest.mark.parametrize(
    "text",
    [
        # A negated removal never swallows a real one after it.
        "Don't skip the river clip, take out the bathroom clip",
        "don't drop the river and leave out the bathroom",
        "never remove the river clip; hide the bathroom clip",
        "don't take the river clip out and throw the bathroom clip away",
        "Nehir klibini atlama, banyo klibini çıkar",
    ],
)
def test_a_negated_removal_next_to_a_real_one_still_limits(text: str) -> None:
    assert creator_limits_clips([text], own_clip_count=len(OWN_VIDEOS))


async def test_mixed_keep_and_remove_keeps_the_models_subset(prod_profile, monkeypatch) -> None:
    selected = [c for c in OWN_VIDEOS if c != BATHROOM]
    strategy = await _draft(
        monkeypatch,
        selected=selected,
        messages=["Don't skip the river clip, take out the bathroom clip"],
    )

    assert strategy["selected_media_ids"] == selected
    assert sorted(_dispatched_clips(strategy)) == sorted(selected)
