"""KRI-1365 PR 2: footage with no sound is never planned around camera audio.

Incident: all 15 uploaded clips had ``has_audio=false``. A pacing follow-up produced
``audio_strategy=original_audio`` and the server added ``montage_audio.preserve_source_audio``.
The creator approved it, then the pin-time verifier refused with "This edit needs audible camera
audio" and offered to "rebuild the edit so it shows this", which can never work. A pacing-only
turn had also flipped ``licensed_music`` to ``original_audio``.

Failure modes, written before the code:

F1  the server auto-adds ``preserve_source_audio`` for ``original_audio`` although every selected
    clip is silent (both insertion sites in ``normalize_creator_strategy_media``).
F2  the plan check passes a strategy that needs camera audio on silent footage, so the creator is
    offered an approval that cannot render.
F3  a creator who explicitly asked to keep the camera sound is silently handed music instead of
    being told the footage has none.
F4  a turn that never touched the sound re-emits the strategy and flips ``audio_strategy``
    (music to camera audio); only ``montage_audio`` and ``voice_mode`` were carried.
F5  approval readiness calls "keep source sound" proven although no selected clip is audible.
F6  the pin-time verifier says "rebuild the edit so it shows this" when NO clip has sound; the
    creator is sent in a circle. A mixed set (some clip audible) keeps the ordinary refusal.
F7  controls: footage WITH sound still keeps camera audio, and unknown audio (``has_audio``
    absent) is never read as silent.
"""

from __future__ import annotations

import pytest
from sqlalchemy import select

from app.agents._schemas.creator_agent import CreativeStrategy
from app.database import sync_session
from app.kria.strategy_policy import CheckedStrategy, RefusedStrategy, check_strategy_for_runtime_v2
from app.models import CreatorEditDraft
from app.schemas.edit_proposal import MontageAudioPlan
from app.services.creator_render_contract import (
    CreatorRenderContract,
    CreatorRenderContractError,
    verify_phone_recipe,
)
from tests.kria.test_approval_readiness_postgres import prepare, readiness_flags  # noqa: F401


def _head_strategy(thread_id) -> dict:  # noqa: ANN001
    with sync_session() as db:
        draft = db.scalars(
            select(CreatorEditDraft).where(
                CreatorEditDraft.thread_id == thread_id, CreatorEditDraft.is_head.is_(True)
            )
        ).one()
        return draft.snapshot_json["strategy"]


@pytest.mark.asyncio
async def test_silent_clips_never_draft_an_edit_that_needs_camera_audio(monkeypatch) -> None:
    """The incident through the real planner, policy check and draft store."""
    _, thread_id, approvals, events = await prepare(monkeypatch, phone=True, silent=True)
    strategy = _head_strategy(thread_id)
    assert strategy["audio_strategy"] != "original_audio", (
        strategy["audio_strategy"],
        strategy.get("montage_audio"),
    )
    assert not (strategy.get("montage_audio") or {}).get("preserve_source_audio"), strategy
    assert any(
        "none of your clips has sound" in (event.content or "").lower() for event in events
    ), [event.content for event in events]
    assert len(approvals) == 1


@pytest.mark.asyncio
async def test_control_clips_with_sound_keep_camera_audio(monkeypatch) -> None:
    _, thread_id, approvals, _ = await prepare(monkeypatch, phone=True, silent=False)
    strategy = _head_strategy(thread_id)
    assert strategy["audio_strategy"] == "original_audio", strategy
    assert len(approvals) == 1


# ---------------------------------------------------------------- plan check (F2, F3, F7)


def _manifest(*has_audio: bool | None):
    from app.services import creator_capabilities as capabilities

    media = [
        {
            "media_id": f"clip-{index}",
            "kind": "video",
            **({} if audio is None else {"has_audio": audio}),
        }
        for index, audio in enumerate(has_audio, start=1)
    ]
    return capabilities.resolve_creator_manifest(
        item_id="item-silent",
        edit_format="montage",
        media=media,
        phone_source_media_ids=[m["media_id"] for m in media],
        phone_rendering_allowed=True,
    )


def _montage(manifest, **update) -> CreativeStrategy:
    fields = {
        "edit_format": "montage",
        "direction": "fast_montage",
        "audio_strategy": "original_audio",
        "render_program": "guided",
        "selected_media_ids": [m.media_id for m in manifest.media],
        "montage_audio": MontageAudioPlan(preserve_source_audio=True),
        **update,
    }
    return CreativeStrategy(**fields)


def _check(manifest, strategy, **kwargs):
    return check_strategy_for_runtime_v2(manifest, strategy, simplify_unsupported=True, **kwargs)


def test_camera_audio_on_silent_footage_falls_back_before_approval(prod_profile) -> None:
    manifest = _manifest(False, False)
    checked = _check(manifest, _montage(manifest))
    assert isinstance(checked, CheckedStrategy), checked
    assert checked.strategy.audio_strategy == "licensed_music"
    assert checked.strategy.montage_audio is None
    assert any("none of your clips has sound" in n.lower() for n in checked.notices)


def test_the_drafts_sound_route_wins_over_the_default_fallback(prod_profile) -> None:
    manifest = _manifest(False, False)
    checked = _check(manifest, _montage(manifest), draft_audio_strategy="licensed_music")
    assert isinstance(checked, CheckedStrategy)
    assert checked.strategy.audio_strategy == "licensed_music"


def test_an_explicit_camera_sound_ask_on_silent_footage_is_a_question_not_a_swap(
    prod_profile,
) -> None:
    manifest = _manifest(False, False)
    refused = _check(manifest, _montage(manifest), creator_asked_camera_sound=True)
    assert isinstance(refused, RefusedStrategy)
    assert refused.code == "silent_footage_camera_audio"
    assert "none of your clips has sound" in refused.question.lower()
    assert "music" in refused.question.lower()


def test_a_no_music_ask_on_silent_footage_stays_silent_instead_of_gaining_music(
    prod_profile,
) -> None:
    """KRI-1294 Q3: "I'll add a song on TikTok" is a stated ask; music must not be swapped in."""
    manifest = _manifest(False, False)
    checked = _check(manifest, _montage(manifest), creator_asked_no_music=True)
    assert isinstance(checked, CheckedStrategy), checked
    assert checked.strategy.audio_strategy == "original_audio"
    assert checked.strategy.montage_audio == MontageAudioPlan(preserve_source_audio=False)
    assert not any("has sound" in n for n in checked.notices)
    # Re-checking the saved strategy (approval, readiness) without the flag changes nothing.
    again = _check(manifest, checked.strategy)
    assert isinstance(again, CheckedStrategy), again
    assert again.strategy == checked.strategy


def test_neighbours_of_the_no_music_ask_keep_their_verdicts(prod_profile) -> None:
    manifest = _manifest(False, False)
    # No stated sound ask: music still replaces camera audio (the KRI-1365 default).
    assert _check(manifest, _montage(manifest)).strategy.audio_strategy == "licensed_music"
    # A camera-sound ask still wins as a question, even beside no-music.
    refused = _check(
        manifest, _montage(manifest), creator_asked_camera_sound=True, creator_asked_no_music=True
    )
    assert isinstance(refused, RefusedStrategy)
    # A strategy that already chose music is not flipped to silence by the no-music flag.
    music = _montage(manifest, audio_strategy="licensed_music", montage_audio=None)
    kept = _check(manifest, music, creator_asked_no_music=True)
    assert isinstance(kept, CheckedStrategy)
    assert kept.strategy.audio_strategy == "licensed_music"


@pytest.mark.parametrize("audio", [(True, False), (True, True), (None, None), (False, None)])
def test_control_footage_with_or_possibly_with_sound_keeps_camera_audio(
    prod_profile, audio
) -> None:
    manifest = _manifest(*audio)
    checked = _check(manifest, _montage(manifest))
    assert isinstance(checked, CheckedStrategy), checked
    assert checked.strategy.audio_strategy == "original_audio"
    assert checked.strategy.montage_audio is not None
    assert not any("has sound" in n for n in checked.notices)


def test_only_the_selected_clips_decide_whether_the_footage_is_silent(prod_profile) -> None:
    manifest = _manifest(False, True)
    silent_pick = _montage(manifest, selected_media_ids=["clip-1"])
    assert _check(manifest, silent_pick).strategy.audio_strategy == "licensed_music"
    audible_pick = _montage(manifest, selected_media_ids=["clip-2"])
    assert _check(manifest, audible_pick).strategy.audio_strategy == "original_audio"


# ---------------------------------------------------------------- model boundary (F1)


def test_the_server_does_not_add_camera_audio_to_silent_footage() -> None:
    from app.agents._schemas.creator_policy import normalize_creator_strategy_media

    for audio, preserved in ((False, False), (True, True)):
        manifest = _manifest(audio, audio)
        strategy = CreativeStrategy(
            edit_format="montage",
            direction="fast_montage",
            audio_strategy="original_audio",
            render_program="guided",
            video_reuse_policy="allow_repeat",
            selected_media_ids=[m.media_id for m in manifest.media],
        )
        normalized = normalize_creator_strategy_media(manifest, strategy)
        assert bool(normalized.montage_audio) is preserved, audio


# ---------------------------------------------------------------- draft carry (F4)


def _draft(audio_strategy: str, **extra) -> dict:
    return {"edit_format": "montage", "audio_strategy": audio_strategy, **extra}


def test_a_turn_with_no_sound_ask_keeps_the_drafts_music_route() -> None:
    from app.kria.draft_sound_carry import carry_draft_audio_strategy

    manifest = _manifest(True, True)
    flipped = _montage(manifest)
    assert flipped.audio_strategy == "original_audio"
    assert flipped.montage_audio is not None

    carried = carry_draft_audio_strategy(flipped, _draft("licensed_music"))
    assert carried.audio_strategy == "licensed_music"
    assert carried.montage_audio is None
    # Same route, a different edit format, or a route outside the music/camera pair: untouched.
    assert carry_draft_audio_strategy(flipped, _draft("original_audio")) is flipped
    assert (
        carry_draft_audio_strategy(flipped, {**_draft("licensed_music"), "edit_format": "day_vlog"})
        is flipped
    )
    assert carry_draft_audio_strategy(flipped, _draft("voiceover")) is flipped
    assert carry_draft_audio_strategy(flipped, None) is flipped


def test_only_a_brief_without_sound_asks_carries_the_route() -> None:
    from app.kria.brief import BriefRequirement, CreativeBrief
    from app.kria.draft_sound_carry import asks_camera_sound, has_sound_ask

    keep = BriefRequirement(
        id="r1",
        kind="audio",
        scope="global",
        description="keep the original sound",
        facts={"audio_intent": "source_unchanged"},
    )
    music = BriefRequirement(
        id="r2", kind="audio", scope="global", description="upbeat music", facts={}
    )
    title = BriefRequirement(id="r3", kind="text", scope="title", literal="Gameday")
    assert not has_sound_ask(None)
    assert not has_sound_ask(CreativeBrief(requirements=[title]))
    assert has_sound_ask(CreativeBrief(requirements=[music]))
    assert asks_camera_sound(CreativeBrief(requirements=[keep]))
    assert not asks_camera_sound(CreativeBrief(requirements=[music, title]))


# ---------------------------------------------------------------- approval readiness (F5)


def test_keep_source_sound_is_not_proven_on_silent_footage() -> None:
    from app.kria.approval_readiness import _selected_clip_audible

    rows = [
        {"media_id": "clip-1", "kind": "video", "has_audio": False},
        {"media_id": "clip-2", "kind": "video", "has_audio": True},
        {"media_id": "clip-3", "kind": "video"},
    ]
    snapshot = {"clip_assignments": rows}
    assert not _selected_clip_audible({"selected_media_ids": ["clip-1"]}, snapshot)
    assert _selected_clip_audible({"selected_media_ids": ["clip-1", "clip-2"]}, snapshot)
    assert _selected_clip_audible({"selected_media_ids": ["clip-3"]}, snapshot)
    assert _selected_clip_audible({}, snapshot)
    assert not _selected_clip_audible({}, {"clip_assignments": rows[:1]})
    assert _selected_clip_audible({}, {})


# ---------------------------------------------------------------- pin-time verifier (F6)


def _silent_contract_and_recipe():
    from app.pipeline.guided_story import compile_execution_plan
    from tests.evals.request_following.test_creation_composition import base_plan
    from tests.evals.request_following.test_kri559_journey import _bindings, _phone_recipe

    compiled = compile_execution_plan(base_plan().guided_edit(), track=None)
    recipe = _phone_recipe(compiled, _bindings())
    return CreatorRenderContract(generation_id="kri1365", original_audio="require").rebind(), recipe


def test_a_silent_source_set_declines_as_unavailable_and_offers_music(prod_profile) -> None:
    contract, recipe = _silent_contract_and_recipe()
    with pytest.raises(CreatorRenderContractError) as refused:
        verify_phone_recipe(contract, recipe, source_audio={"c0": False, "c1": False})
    assert refused.value.decline_reason == "capability_unavailable"
    assert "rebuild" not in (refused.value.alternative or "")
    assert refused.value.alternative == "Want me to use music instead?"
    assert str(refused.value) == "None of your clips has sound."


def test_a_partly_audible_source_set_keeps_the_ordinary_refusal(prod_profile) -> None:
    contract, recipe = _silent_contract_and_recipe()
    with pytest.raises(CreatorRenderContractError) as refused:
        verify_phone_recipe(contract, recipe, source_audio={"c0": False, "c1": False, "c2": True})
    assert refused.value.decline_reason == "evidence_missing"
    assert "rebuild" in (refused.value.alternative or "")


def test_control_audible_sources_still_verify(prod_profile) -> None:
    contract, recipe = _silent_contract_and_recipe()
    assert verify_phone_recipe(contract, recipe, source_audio={"c0": True, "c1": True})


@pytest.mark.parametrize(
    ("language", "expected"),
    [
        ("en", "None of your clips has sound. Want me to use music instead?"),
        ("tr", "Kliplerinin hiçbirinde ses yok. Onun yerine müzik kullanayım mı?"),
    ],
)
def test_the_chat_says_it_in_the_creators_language(language: str, expected: str) -> None:
    from app.kria.reply_language import reply_language_for
    from app.tasks.kria_runtime import _capability_refusal_copy

    contract, recipe = _silent_contract_and_recipe()
    with pytest.raises(CreatorRenderContractError) as refused:
        verify_phone_recipe(contract, recipe, source_audio={"c0": False, "c1": False})
    decline = {
        "decline_reason": refused.value.decline_reason,
        "message": str(refused.value),
        "alternative": refused.value.alternative,
    }
    with reply_language_for(language):
        assert _capability_refusal_copy(decline) == expected
