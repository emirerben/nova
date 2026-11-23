"""KRI-1561 PR 7: "no sound effects" holds through later chat edits until the creator withdraws it.

Failure modes, written before the tests:

1. A later chat edit ("add effects where they fit", a bundle of creative changes) adds effects
   although the creator said "no sound effects".
2. The gate sticks: after the creator withdraws it ("actually add sound effects") the copilot
   still refuses because it read the brief from before this turn's updates (the same bug made
   "actually add music" need two asks).
3. The gate blocks what it should not: removing or patching an effect that is already there.
4. The re-plan path (an approved plan whose strategy carries `sfx_ask`) places effects anyway,
   or declines without saying why.
5. The gate reads the creator's words instead of the typed fact.
"""

from __future__ import annotations

import pytest

from app.kria.brief import BriefRequirement, CreativeBrief
from app.kria.draft_sound_carry import asks_no_sound_effects
from app.kria.planner import _plan_editor_revision_with_brief, _turn_brief
from tests.kria.test_talking_music_requests import _binding, _Copilot, phone  # noqa: F401

NO_SFX = BriefRequirement(
    id="n1", kind="audio", scope="global", description="no sound effects",
    facts={"audio_intent": "no_sound_effects"},
)  # fmt: skip
UNTYPED = BriefRequirement(
    id="n2", kind="audio", scope="global", description="no sound effects please"
)
CATALOG = [
    {
        "id": "fx-pop",
        "name": "Pop",
        "role_tags": ["appear"],
        "duration_s": 0.4,
        "audio_gcs_path": "sound-effects/fx-pop/a.wav",
        "src_gcs_path": "sound-effects/fx-pop/a.wav",
    }
]
ADD = {"op": "add_sfx", "effect_id": "fx-pop", "at_s": 2.0, "gain": 0.8}


def _open_sfx(job) -> None:
    """The edit after a Save that wrote its effect lane (the `sfx` family opens only then)."""
    job.assembly_plan["variants"][0]["sound_effects"] = [
        {
            "id": "fx-old",
            "sound_effect_id": "fx-old",
            "src_gcs_path": "sound-effects/fx-old/a.wav",
            "label": "Old pop",
            "at_s": 1.0,
            "gain": 1.0,
            "duration_s": 0.4,
            "effect_group_id": "g1",
        }
    ]


def _chat(job, utterance: str, ops: list[dict], *, context: dict, language: str = "en"):
    """One copilot turn on the real snapshot and the real agent (parser, notes); the compile and
    Save are not the subject here, so none runs."""
    from app.agents.edit_copilot import EditCopilotAgent, EditCopilotInput
    from app.kria.reply_language import reply_language_for
    from app.services.kria_editor_ops import build_editor_snapshot, project_editor_draft

    base = project_editor_draft(job.assembly_plan["variants"][0], {}, job)
    snapshot = build_editor_snapshot(job, base, clip_context=context)
    client = _Copilot(ops)
    with reply_language_for(language):
        output = EditCopilotAgent(client).run(
            EditCopilotInput(utterance=utterance, variant_snapshot=snapshot)
        )
    return snapshot, output, client.prompts, None


def _brief(*reqs: BriefRequirement) -> CreativeBrief:
    return CreativeBrief(version=1, requirements=list(reqs))


def test_the_typed_fact_decides_never_the_words():
    assert asks_no_sound_effects(_brief(NO_SFX))
    assert not asks_no_sound_effects(_brief(UNTYPED))
    assert not asks_no_sound_effects(_brief())
    assert not asks_no_sound_effects(None)


def test_a_withdrawn_no_sound_effects_is_no_longer_live():
    """The creator changes their mind: the brief writer `remove`s the "no sound effects" row
    (taught beside the sound-effects fact), and the gate opens."""
    from app.kria.brief import BriefUpdate, apply_updates

    held = _brief(NO_SFX)
    assert asks_no_sound_effects(held)
    withdrawn = apply_updates(
        held,
        [BriefUpdate(operation="remove", target_requirement_id="n1", expected_version=1)],
        source_turn_id="turn-2",
    )
    assert not asks_no_sound_effects(withdrawn)


def test_the_gate_is_open_without_the_flag_and_the_effect_is_added(phone):  # noqa: F811
    """Control: the same turn without a live "no sound effects" adds the effect."""
    _open_sfx(phone)
    snapshot, output, _prompts, _saved = _chat(
        phone, "add a pop at 2 seconds", [ADD], context={"sfx_catalog": CATALOG}
    )
    assert "no_sound_effects" not in snapshot.get("sfx", {})
    assert [op["op"] for op in output.ops] == ["add_sfx"]


@pytest.mark.parametrize(
    ("utterance", "language", "note"),
    [
        ("add sound effects where they fit", "en", "you asked for no sound effects"),
        ("uygun yerlere ses efekti ekle", "tr", "ses efekti istemediğini söylemiştin"),
    ],
)
def test_a_live_no_sound_effects_refuses_the_add_and_says_why(phone, utterance, language, note):  # noqa: F811
    _open_sfx(phone)
    snapshot, output, prompts, _saved = _chat(
        phone,
        utterance,
        [ADD],
        context={"sfx_catalog": CATALOG, "no_sound_effects": True},
        language=language,
    )
    assert snapshot["sfx"]["no_sound_effects"] is True and snapshot["sfx"]["catalog"] == []
    assert output.ops == []
    assert note in output.reply
    assert "no_sound_effects=True" in prompts[0]


def test_a_live_no_sound_effects_still_lets_the_creator_remove_an_effect(phone):  # noqa: F811
    _open_sfx(phone)
    snapshot, output, _prompts, _saved = _chat(
        phone,
        "remove that sound effect",
        [{"op": "remove_sfx", "sfx_index": 0}],
        context={"sfx_catalog": CATALOG, "no_sound_effects": True},
    )
    assert snapshot["sfx"]["placements"], "the fixture edit has one pinned effect"
    assert [op["op"] for op in output.ops] == ["remove_sfx"]


@pytest.mark.asyncio
async def test_the_copilot_turn_reads_the_brief_with_this_turns_updates(monkeypatch):
    """The routing step already saw this turn's brief; the copilot turn did not (it read the
    saved one), so withdrawing "no effects" and adding them in one message was refused."""
    import app.kria.planner as planner

    effective = _brief()
    seen: list = []

    async def stub(*_args, **_kwargs):
        seen.append(_turn_brief.get())
        return "plan"

    monkeypatch.setattr(planner, "_plan_editor_revision", stub)
    assert _turn_brief.get() is None
    assert await _plan_editor_revision_with_brief(effective, None) == "plan"
    assert seen == [effective]
    assert _turn_brief.get() is None, "the turn brief never outlives the call"


def test_binding_helper_roundtrips_a_typed_no_sound_effects():
    from app.kria.brief_binding import BriefBinding

    binding = _binding(NO_SFX)
    assert asks_no_sound_effects(BriefBinding.model_validate(binding).resolve())


@pytest.mark.asyncio
async def test_the_planner_flags_no_sound_effects_from_this_turns_brief(monkeypatch):
    """What the creator said THIS turn decides: a "no sound effects" said now flags the context,
    and its withdrawal clears it even though the saved brief still holds it. A DB that cannot be
    read fails closed (no context), never a failed turn."""
    import uuid
    from types import SimpleNamespace
    from unittest.mock import AsyncMock

    from app.kria import planner

    thread = SimpleNamespace(creator_id=uuid.uuid4())
    job = SimpleNamespace(
        id=uuid.uuid4(), user_id=thread.creator_id, all_candidates={}, assembly_plan={}
    )
    item = SimpleNamespace(id=uuid.uuid4(), clip_assignments=[])
    variant = {"variant_id": "subtitled", "render_destination": "device"}

    async def context(turn_brief):
        with planner._turn_brief_is(turn_brief):
            return await planner._copilot_clip_context(
                AsyncMock(),
                thread=thread,
                thread_id=uuid.uuid4(),
                job=job,
                variant=variant,
                item=item,
            )

    assert (await context(_brief(NO_SFX))).get("no_sound_effects") is True
    assert "no_sound_effects" not in await context(_brief())
