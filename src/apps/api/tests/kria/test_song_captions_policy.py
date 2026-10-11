"""KRI-569: "lipsync + add captions" with no uploaded song asks for the song.

Replays the strategy the Main Creator recorded for the failed prod job (430219e4): the
clips' own audio, ``caption_style="karaoke"``, no song. It used to reach the render and
die as ``phone_plan_unsupported``; now the chat asks for the song first.
"""

from __future__ import annotations

import uuid
from types import SimpleNamespace

import pytest

from app.agents._schemas.creator_agent import CreativeStrategy, ProposeStrategy
from app.config import settings
from app.kria import planner
from app.kria.planner import _plan_from_creator_output
from app.kria.strategy_policy import (
    SONG_CAPTIONS_NEED_SONG_CODE,
    CheckedStrategy,
    song_captions_refusal,
)

ASK = "Add the song you lip-synced to and I'll caption its lyrics."
LIPSYNC_CAPTIONS = "Lipsync\nAdd captions"

# Recorded from the failed prod job's `creator_strategy` (2026-10-11 06:35 UTC).
PROD_STRATEGY = {
    "pacing": "fast",
    "direction": "fast_montage",
    "rationale": (
        "I will cut a fast-paced performance montage using the best moments from all your "
        "clips. By keeping your original audio, the lip-syncing will match perfectly, and "
        "the karaoke caption style will make the words pop on screen."
    ),
    "edit_format": "montage",
    "media_scope": "all",
    "caption_style": "karaoke",
    "montage_audio": {
        "source_media_ids": [],
        "preview_source_beds": False,
        "preserve_source_audio": True,
    },
    "audio_strategy": "original_audio",
    "render_program": "guided",
    "story_structure": [
        "Open with strong eye contact and energetic lip-syncing",
        "Cut quickly between expressive gestures and subtle reactions",
        "End on a confident lingering look at the camera",
    ],
    "target_duration_s": 20,
    "selected_media_ids": [],
    "video_reuse_policy": "once",
    "optional_treatments": [],
    "target_duration_requested": True,
}


def _strategy(**changes) -> CreativeStrategy:
    return CreativeStrategy.model_validate({**PROD_STRATEGY, **changes})


def _manifest(**changes) -> SimpleNamespace:
    return SimpleNamespace(
        **{
            "media": [SimpleNamespace(media_id="a", kind="video")],
            "manifest_hash": "m" * 64,
            "context_hash": "c" * 64,
            "has_user_song": False,
            "has_voiceover": False,
            **changes,
        }
    )


def test_the_failed_prod_shape_asks_for_the_song():
    refused = song_captions_refusal(
        _manifest(), _strategy(), creator_text=f"{LIPSYNC_CAPTIONS}\nLatest message: Add captions"
    )
    assert refused is not None
    assert refused.code == SONG_CAPTIONS_NEED_SONG_CODE
    assert refused.question == ASK


@pytest.mark.parametrize(
    ("manifest", "strategy", "text"),
    [
        # The song is attached: lyrics come from it, nothing to ask.
        (_manifest(has_user_song=True), _strategy(), LIPSYNC_CAPTIONS),
        # An ordinary montage with a caption ask is not a lip-sync: not rerouted.
        (_manifest(), _strategy(), "Add captions"),
        # A lip-sync that never asked for captions is left alone, whatever the model chose.
        (_manifest(), _strategy(), "Lipsync"),
        # "No captions" is an explicit opt-out.
        (_manifest(), _strategy(), "Lipsync, no captions please"),
        (_manifest(), _strategy(), "Lipsync, I mouth the lyrics to my song"),
        (_manifest(), _strategy(caption_style="none"), LIPSYNC_CAPTIONS),
        # A voiceover edit has its own caption lane.
        (_manifest(has_voiceover=True), _strategy(), LIPSYNC_CAPTIONS),
        (_manifest(), _strategy(audio_strategy="user_song", song_sync="lipsync"), LIPSYNC_CAPTIONS),
    ],
)
def test_it_only_asks_when_the_creator_wants_lyrics_without_a_song(manifest, strategy, text):
    assert song_captions_refusal(manifest, strategy, creator_text=text) is None


@pytest.mark.parametrize(
    "text",
    [
        "Lipsync\nAdd captions",
        "Add subtitles to my lipsync",
        "Show the lyrics on screen",
        "put the lyrics on the video",
        "I want lyrics on screen",
        "karaoke style please",
        "Lipsync\nno captions\nActually, add subtitles",
        "Şarkı sözlerini ekle",
        "altyazı ekle",
    ],
)
def test_real_caption_asks_are_asks(text):
    from app.pipeline.song_captions import text_asks_for_captions

    assert text_asks_for_captions(text) is True


@pytest.mark.parametrize(
    "text",
    [
        "",
        "Just a fast montage",
        "don't add captions",
        "Don\u2019t add captions",
        "I don't want captions",
        "I do not want subtitles",
        "without any captions",
        "don't show lyrics, just lipsync",
        "no lyrics needed on screen",
        "Make a lip sync video, I mouth the lyrics to my song",
        "use a song with lyrics",
        "Lipsync, subtitles off",
        "Lipsync\nAdd captions\nWait, without captions please",
        "altyazı istemiyorum",
        "altyazısız olsun",
        "altyazi olmasin",
    ],
)
def test_negations_and_lyrics_as_a_topic_are_not_asks(text):
    from app.pipeline.song_captions import text_asks_for_captions

    assert text_asks_for_captions(text) is False


# ── wiring through the planner ───────────────────────────────────────────────


async def _plan(
    monkeypatch,
    *,
    manifest,
    text="Add captions",
    strategy=None,
    raw=LIPSYNC_CAPTIONS,
    history=None,
    **flags,
):
    monkeypatch.setattr(settings, "clip_intents_enabled", False)

    async def no_gate(*_a, **_k):  # the song-order gate reads the DB; it is not under test
        return SimpleNamespace(plan=None, strategy=None, resolved_takes=None)

    monkeypatch.setattr(planner, "_song_order_gate", no_gate)
    for name, value in flags.items():
        monkeypatch.setattr(settings, name, value)
    monkeypatch.setattr(
        planner,
        "check_strategy_for_runtime_v2",
        lambda _m, strategy, **_k: CheckedStrategy(strategy=strategy, notices=()),
    )
    action = ProposeStrategy(
        kind="propose_strategy",
        strategy=strategy or _strategy(),
        summary="A fast performance montage.",
    )
    return await _plan_from_creator_output(
        SimpleNamespace(),
        thread_id=uuid.uuid4(),
        item_id=uuid.uuid4(),
        creator_id=uuid.uuid4(),
        user_message=text,
        manifest=manifest,
        inputs=SimpleNamespace(
            intent_clips=[],
            creator_request=history if history is not None else text,
            raw_creator_request=raw,
        ),
        output=SimpleNamespace(action=action),
        brief_request=None,
    )


async def test_the_planner_asks_for_the_song_instead_of_minting_a_render(monkeypatch):
    planned = await _plan(monkeypatch, manifest=_manifest())
    assert planned.plan.mode == "respond"
    assert planned.plan.turn_value == "question"
    assert planned.plan.response == ASK


async def test_the_planner_goes_on_when_the_song_is_attached(monkeypatch):
    planned = await _plan(monkeypatch, manifest=_manifest(has_user_song=True))
    assert planned.plan.response != ASK


async def test_the_kill_switch_restores_the_old_path(monkeypatch):
    planned = await _plan(monkeypatch, manifest=_manifest(), song_captions_enabled=False)
    assert planned.plan.response != ASK


def test_an_attached_but_unusable_song_is_not_asked_for_again():
    """Phone-only / disabled-by-setting songs leave `has_user_song` False but mark the
    capability unavailable: that messaging owns it, we must not say "Add the song" each turn."""
    from app.agents._schemas.creator_agent import CapabilityAvailability
    from app.agents._schemas.creator_policy import CAPABILITY_USER_SONG

    unusable = _manifest(
        capabilities={
            CAPABILITY_USER_SONG: CapabilityAvailability(
                available=False, reason_code="disabled_by_setting", reason="Songs are off."
            )
        }
    )
    assert song_captions_refusal(unusable, _strategy(), creator_text=LIPSYNC_CAPTIONS) is None
    assert song_captions_refusal(_manifest(), _strategy(), creator_text=LIPSYNC_CAPTIONS)


async def test_the_planner_does_not_loop_on_an_unusable_song(monkeypatch):
    from app.agents._schemas.creator_agent import CapabilityAvailability
    from app.agents._schemas.creator_policy import CAPABILITY_USER_SONG

    unusable = _manifest(
        capabilities={
            CAPABILITY_USER_SONG: CapabilityAvailability(
                available=False, reason_code="phone_only", reason="Phone only."
            )
        }
    )
    planned = await _plan(monkeypatch, manifest=unusable)
    assert planned.plan.response != ASK


# ── KRI-578: the plan-time lyric-caption pin ─────────────────────────────────
#
# Failure modes written first: (1) the planner never pins, so the worker must re-read the
# request; (2) a model-authored `song_captions` survives into the approved draft;
# (3) the pin is set with no song attached; (4) the explicit `caption_style=none` opt-out
# is ignored; (5) the kill switch still pins.


SONG = {"audio_strategy": "user_song", "song_sync": "background"}


def _pinned(planned):
    (apply,) = [i for i in planned.plan.intents if i.tool_name == "draft.apply_strategy"]
    return apply.arguments["strategy"].get("song_captions")


async def test_the_planner_pins_word_captions_from_the_creators_words(monkeypatch):
    planned = await _plan(
        monkeypatch, manifest=_manifest(has_user_song=True), strategy=_strategy(**SONG)
    )
    assert _pinned(planned) == "word"  # karaoke is the strategy's own style


async def test_the_planner_pins_whole_lines_for_a_plain_captions_ask(monkeypatch):
    planned = await _plan(
        monkeypatch,
        manifest=_manifest(has_user_song=True),
        strategy=_strategy(caption_style="clean", **SONG),
    )
    assert _pinned(planned) == "sentence"


async def test_a_model_authored_pin_is_dropped(monkeypatch):
    # No caption ask in the creator's words anywhere: whatever the model wrote is discarded.
    monkeypatch.setattr(planner, "song_captions_refusal", lambda *_a, **_k: None)
    strategy = _strategy(song_captions="word", caption_style="clean", **SONG)
    planned = await _plan(
        monkeypatch,
        manifest=_manifest(has_user_song=True),
        text="Make a fast montage",
        raw="Make a fast montage",
        strategy=strategy,
    )
    assert _pinned(planned) is None


async def test_no_pin_when_the_strategy_does_not_play_the_song(monkeypatch):
    monkeypatch.setattr(planner, "song_captions_refusal", lambda *_a, **_k: None)
    planned = await _plan(monkeypatch, manifest=_manifest(has_user_song=True))
    assert _pinned(planned) is None  # the prod strategy keeps the clips' own audio


async def test_the_explicit_no_captions_style_is_never_pinned(monkeypatch):
    planned = await _plan(
        monkeypatch,
        manifest=_manifest(has_user_song=True),
        strategy=_strategy(caption_style="none", **SONG),
    )
    assert _pinned(planned) is None


async def test_the_kill_switch_pins_nothing(monkeypatch):
    planned = await _plan(
        monkeypatch,
        manifest=_manifest(has_user_song=True),
        strategy=_strategy(**SONG),
        song_captions_enabled=False,
    )
    assert _pinned(planned) is None


def test_the_pin_never_reaches_the_model_or_unrelated_stored_strategies():
    """Server-owned: no Main Creator prompt bump, and a song-less strategy stays byte-identical."""
    assert "song_captions" not in CreativeStrategy.model_json_schema()["properties"]
    assert "song_captions" not in _strategy().model_dump(mode="json", exclude_none=True)
    assert _strategy(song_captions="word").model_dump(mode="json")["song_captions"] == "word"


async def test_a_later_message_does_not_unpin_captions_the_thread_asked_for(monkeypatch):
    """Brief-off turn: only the thread text (`inputs.creator_request`) holds the ask."""
    planned = await _plan(
        monkeypatch,
        manifest=_manifest(has_user_song=True),
        strategy=_strategy(**SONG),
        text="make the cuts faster",
        raw="make the cuts faster",
        history="Lipsync to my song and add captions",
    )
    assert _pinned(planned) == "word"


def test_a_stray_pin_on_a_strategy_without_the_song_is_dropped():
    from app.agents._schemas.creator_policy import repair_creator_user_song

    repaired, _ = repair_creator_user_song(_manifest(), _strategy(song_captions="word"))
    assert repaired.song_captions is None
