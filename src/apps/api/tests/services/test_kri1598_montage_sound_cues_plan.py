"""KRI-1598 item 3, planning side: what the planner is offered, what approval keeps, what the
draft judge says, for named sound cues on a phone montage with a recorded voiceover.

The failure-mode list is in `tests/tasks/test_kri1598_montage_sound_cues.py` (written before
the code); this file covers 1-5 and the draft half of 13 through the real manifest resolver, the
real strategy compiler, the real prompt renderer and the real brief checker.
"""

from __future__ import annotations

import pytest

from app.agents._schemas.creator_agent import CreativeStrategy
from app.agents.main_creator import MainCreatorAgent
from app.kria.brief import BriefRequirement
from app.kria.brief_checks import check_requirement, plan_facts_from_strategy
from app.kria.reply_language import reply_language_for
from app.kria.strategy_policy import localize_known_message
from app.services import creator_capabilities as capabilities
from tests.agents.test_main_creator_agent import _input
from tests.services.test_creator_capabilities_sfx_ask import VOICE, _gates

SECTION = "SOUND CUES ON THE VOICEOVER"
WHOOSH = {"beat_id": "barcelona", "trigger": "Barcelona", "sound": "Whoosh"}
CATALOG = [{"catalog_id": "sfx-whoosh", "kind": "sound_effect", "label": "Whoosh"}]
MEDIA = [
    {"media_id": "phone-a", "kind": "video"},
    {"media_id": "phone-b", "kind": "video"},
    {"media_id": "asset-map", "kind": "image", "label": "map.jpg"},
]


def _manifest(
    monkeypatch, edit_format="montage", *, voiceover=True, phone=True, flag=True, **gates
):
    _gates(monkeypatch, phone_voiceover_montage_sound_cues_enabled=flag, **gates)
    ids = ["phone-a", "phone-b"]
    return capabilities.resolve_creator_manifest(
        item_id="item-phone",
        edit_format=edit_format,
        media=MEDIA,
        catalog=CATALOG,
        has_voiceover=voiceover,
        narration=VOICE if voiceover else None,
        **({"phone_source_media_ids": ids, "phone_rendering_allowed": True} if phone else {}),
    )


def _open(manifest) -> bool:
    entry = manifest.capabilities.get(capabilities.CAPABILITY_SOUND_CUES)
    return bool(entry is not None and entry.available)


def _strategy(**fields) -> CreativeStrategy:
    return CreativeStrategy.model_validate(
        {
            "edit_format": "montage",
            "audio_strategy": "voiceover",
            "media_scope": "selected",
            "selected_media_ids": ["phone-a", "phone-b"],
            **fields,
        }
    )


def _prompt(manifest) -> str:
    agent_input = _input().model_copy(update={"capability_manifest": manifest})
    return MainCreatorAgent(None).render_prompt(agent_input)  # type: ignore[arg-type]


# ------------------------------------------------------------------ 1-2: the gate


def test_open_only_on_a_phone_montage_with_a_recorded_voiceover(monkeypatch):
    assert _open(_manifest(monkeypatch, "montage"))
    assert _open(_manifest(monkeypatch, "day_vlog"))
    # Photos stay off there: every reader of `reaction_beats` keeps today's answer.
    beats = _manifest(monkeypatch).capabilities[capabilities.CAPABILITY_REACTION_BEATS]
    assert not beats.available and beats.reason_code == "phone_talking_only"


@pytest.mark.parametrize(
    ("edit_format", "voiceover", "phone"),
    [
        ("montage", False, True),
        ("montage", True, False),
        ("subtitled", False, True),
        ("narrated_planned", True, True),
    ],
    ids=["montage-no-voice", "montage-cloud", "talking", "narrated-voiceover"],
)
def test_never_advertised_where_this_writer_does_not_render(
    monkeypatch, edit_format, voiceover, phone
):
    manifest = _manifest(monkeypatch, edit_format, voiceover=voiceover, phone=phone)
    assert capabilities.CAPABILITY_SOUND_CUES not in manifest.capabilities


@pytest.mark.parametrize(
    "gate",
    [
        "phone_voiceover_reaction_beats_enabled",
        "phone_subtitled_reaction_beats_enabled",
        "phone_voiceover_editor_lanes_enabled",
        "sound_effects_enabled",
    ],
)
def test_the_worker_gate_closes_it(monkeypatch, gate):
    """The manifest asks `phone_voiceover_montage_sound_cues_supported`, as the worker does."""
    assert not _open(_manifest(monkeypatch, **{gate: False}))


def test_flag_off_leaves_the_manifest_and_the_prompt_byte_identical(monkeypatch):
    shut = _manifest(monkeypatch, flag=False)
    opened = _manifest(monkeypatch)
    assert set(opened.capabilities) - set(shut.capabilities) == {capabilities.CAPABILITY_SOUND_CUES}
    assert {
        k: v for k, v in opened.capabilities.items() if k != capabilities.CAPABILITY_SOUND_CUES
    } == shut.capabilities
    shut_prompt = _prompt(shut)
    assert SECTION not in shut_prompt
    assert capabilities.CAPABILITY_SOUND_CUES not in shut_prompt
    opened_prompt = _prompt(opened)
    assert SECTION in opened_prompt and "needs a `sound`" in opened_prompt
    assert "Photos and stickers cannot pop up on the words of this edit" in " ".join(
        opened_prompt.split()
    )


# ------------------------------------------------------------------ 3-5: the repair


def test_approval_keeps_each_sound_and_tells_the_pictures_left_out(monkeypatch):
    manifest = _manifest(monkeypatch)
    strategy = _strategy(
        reaction_beats=[
            {**WHOOSH, "visual_id": "asset-map", "display_mode": "fullscreen"},
            {"beat_id": "map", "trigger": "the map", "visual_id": "asset-map"},
        ],
        closing_media={"visual_id": "asset-map"},
    )

    plan = capabilities.compile_strategy_to_plan(manifest, strategy)

    [beat] = plan.strategy.reaction_beats
    assert beat.trigger == "Barcelona" and beat.sound == "sfx-whoosh"  # resolved to the catalog
    assert beat.visual_id is None and beat.display_mode == "pip"
    assert plan.strategy.closing_media is None
    assert plan.notices.count(capabilities.SOUND_CUES_PICTURES_LEFT_OUT_NOTICE) == 1
    with reply_language_for("tr"):
        assert localize_known_message(capabilities.SOUND_CUES_PICTURES_LEFT_OUT_NOTICE).startswith(
            "Bu düzenlemede sözlerine göre fotoğraf"
        )


def test_a_sound_only_strategy_is_kept_with_no_notice(monkeypatch):
    plan = capabilities.compile_strategy_to_plan(
        _manifest(monkeypatch), _strategy(reaction_beats=[WHOOSH])
    )
    assert [b.trigger for b in plan.strategy.reaction_beats] == ["Barcelona"]
    assert capabilities.SOUND_CUES_PICTURES_LEFT_OUT_NOTICE not in plan.notices
    assert not any("aren't available for this edit yet" in n for n in plan.notices)


def test_flag_off_still_strips_the_cue_with_todays_notice(monkeypatch):
    plan = capabilities.compile_strategy_to_plan(
        _manifest(monkeypatch, flag=False), _strategy(reaction_beats=[WHOOSH])
    )
    assert plan.strategy.reaction_beats is None
    assert any("Sound and photo pop-ins timed to your words" in n for n in plan.notices)


def test_a_guided_voiceover_contract_does_not_keep_cues_its_writer_never_plays(monkeypatch):
    manifest = _manifest(monkeypatch)
    strategy = _strategy(reaction_beats=[WHOOSH]).model_copy(
        update={"execution_contract": "guided_voiceover_v1"}
    )
    repaired, notices = capabilities.repair_creator_reaction_beats(manifest, strategy)
    assert repaired.reaction_beats is None
    assert any("Sound and photo pop-ins timed to your words" in n for n in notices)


# ------------------------------------------------------------------ 13: the draft judge


def _judge(monkeypatch, strategy: CreativeStrategy, description: str):
    facts = plan_facts_from_strategy(
        strategy.model_dump(mode="json", exclude_none=True), manifest=_manifest(monkeypatch)
    )
    req = BriefRequirement(id="r1", kind="audio", scope="global", description=description)
    return facts, check_requirement(req, facts)


def test_a_draft_sound_cue_waits_for_the_voice_never_reads_not_available(monkeypatch):
    facts, receipt = _judge(
        monkeypatch, _strategy(reaction_beats=[WHOOSH]), 'Play a whoosh when I say "Barcelona"'
    )
    assert facts.reaction_beats_available is True and facts.beats_sound_only is True
    assert receipt.status == "partial"
    assert receipt.reason == "I'll play it when I hear those words in your voiceover"


def test_a_draft_photo_at_a_word_says_this_edit_cannot_show_it(monkeypatch):
    facts, receipt = _judge(
        monkeypatch,
        _strategy(reaction_beats=[{"beat_id": "map", "trigger": "map", "visual_id": "asset-map"}]),
        'Show my map photo when I say "map"',
    )
    assert facts.dropped_beat_triggers == ()
    assert receipt.status == "not_possible"
    assert "can't pop up on your words in this edit yet, only sounds" in receipt.reason
    assert "couldn't find" not in receipt.reason
