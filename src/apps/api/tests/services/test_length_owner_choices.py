from app.kria.brief import BriefRequirement, CreativeBrief
from app.services.choice_questions import (
    CONFLICT_LENGTH_VS_NATURAL,
    OPT_ACCEPT_NATURAL,
    OPT_TRIM_TO_REQUESTED,
    build_choice_question,
    collect_conflicts,
    resolve_choices,
)


def brief(seconds):
    return CreativeBrief(
        version=1,
        requirements=[
            BriefRequirement(
                id="duration",
                kind="timing",
                scope="global",
                description=f"Keep {seconds} seconds",
                facts={"duration_s": seconds},
            )
        ],
    )


def choice(strategy, snapshot, seconds):
    found = collect_conflicts(strategy, brief(seconds), snapshot)
    return next(item for item in found if item.kind == CONFLICT_LENGTH_VS_NATURAL)


def test_requested_20_seconds_on_a_14_second_song_offers_actual_source_length():
    found = choice({"audio_strategy": "user_song"}, {"song_duration_s": 14}, 20)
    assert found.requirement_ids == ("duration",)
    assert [option.key for option in found.options] == [OPT_ACCEPT_NATURAL]
    assert found.effects[OPT_ACCEPT_NATURAL]["strategy"]["target_duration_s"] == 14
    assert "20" in found.intro and "14" in found.intro


def test_requested_30_seconds_on_a_12_second_spoken_take_offers_actual_length():
    found = choice(
        {"audio_strategy": "original_audio", "edit_format": "talking_head"},
        {"clip_assignments": [{"media_id": "take", "duration_s": 12}]},
        30,
    )
    assert [option.key for option in found.options] == [OPT_ACCEPT_NATURAL]
    assert found.effects[OPT_ACCEPT_NATURAL]["strategy"]["target_duration_s"] == 12


def test_supported_short_song_trim_keeps_the_creator_requested_length():
    found = choice(
        {"audio_strategy": "user_song"},
        {"song_duration_s": 30, "song_trim_supported": True},
        20,
    )
    assert [option.key for option in found.options] == [OPT_TRIM_TO_REQUESTED, OPT_ACCEPT_NATURAL]
    assert found.effects[OPT_TRIM_TO_REQUESTED]["strategy"]["target_duration_s"] == 20


def test_source_rounding_within_ten_percent_does_not_ask():
    found = collect_conflicts(
        {"audio_strategy": "user_song"},
        brief(24),
        {"song_duration_s": 23},
    )
    assert not any(item.kind == CONFLICT_LENGTH_VS_NATURAL for item in found)


def test_unknown_song_capacity_does_not_invent_a_question():
    found = collect_conflicts({"audio_strategy": "user_song"}, brief(20), {})
    assert not any(item.kind == CONFLICT_LENGTH_VS_NATURAL for item in found)


def test_unplaced_lipsync_track_never_offers_its_full_recording_as_an_edit():
    found = collect_conflicts(
        {"audio_strategy": "user_song", "song_sync": "lipsync"},
        brief(24),
        {"song_duration_s": 180},
    )
    assert not any(item.kind == CONFLICT_LENGTH_VS_NATURAL for item in found)


def test_unsupported_long_song_window_never_offers_an_invalid_duration():
    found = collect_conflicts({"audio_strategy": "user_song"}, brief(24), {"song_duration_s": 180})
    assert not any(item.kind == CONFLICT_LENGTH_VS_NATURAL for item in found)


def test_supported_long_song_trim_offers_only_executable_durations():
    from app.agents._schemas.creator_agent import CreativeStrategy

    found = choice(
        {"audio_strategy": "user_song"},
        {"song_duration_s": 180, "song_trim_supported": True},
        24,
    )
    assert found.effects[OPT_ACCEPT_NATURAL]["strategy"]["target_duration_s"] == 120
    assert "remaining" in found.options[-1].description
    for effect in found.effects.values():
        CreativeStrategy.model_validate({"audio_strategy": "user_song", **effect["strategy"]})


def _parts(seconds):
    return [{"media_id": "clip", "source_start_s": 0, "source_end_s": seconds}]


def test_requested_24_seconds_on_12_seconds_of_selected_footage_asks_to_use_12():
    found = choice({}, {"best_parts": _parts(12)}, 24)
    assert [option.key for option in found.options] == [OPT_ACCEPT_NATURAL]
    assert found.effects[OPT_ACCEPT_NATURAL]["strategy"]["target_duration_s"] == 12


def test_requested_24_seconds_on_60_seconds_of_selected_footage_needs_no_question():
    found = collect_conflicts({}, brief(24), {"best_parts": _parts(60)})
    assert not any(item.kind == CONFLICT_LENGTH_VS_NATURAL for item in found)


def test_voiceover_uses_selected_footage_capacity_and_never_offers_recording_trim():
    found = choice(
        {"audio_strategy": "voiceover"},
        {"voiceover_duration_s": 30, "best_parts": _parts(12), "voiceover_trim_supported": True},
        30,
    )
    assert [option.key for option in found.options] == [OPT_ACCEPT_NATURAL]
    assert found.effects[OPT_ACCEPT_NATURAL]["strategy"]["target_duration_s"] == 12


def test_lipsync_uses_placed_song_alignment_window_not_full_track_length():
    found = choice(
        {"audio_strategy": "user_song", "song_sync": "lipsync"},
        {
            "song_duration_s": 71,
            "song_alignment": {"placements": [{"start_s": 20, "end_s": 34}]},
        },
        20,
    )
    assert found.effects[OPT_ACCEPT_NATURAL]["strategy"]["target_duration_s"] == 14


def test_lipsync_alignment_accepts_a_zero_delta_take_when_joining_durations():
    found = collect_conflicts(
        {"audio_strategy": "user_song", "song_sync": "lipsync"},
        brief(20),
        {
            "song_alignment": {
                "takes": {
                    "a": {"delta_s": 0, "duration_s": 10},
                    "b": {"delta_s": 10, "duration_s": 10},
                }
            }
        },
    )
    assert not any(item.kind == CONFLICT_LENGTH_VS_NATURAL for item in found)


def test_answer_is_replayed_until_selected_footage_changes_then_reopens():
    strategy = {}
    initial_snapshot = {"best_parts": _parts(12)}
    open_resolution = resolve_choices(strategy, brief(24), initial_snapshot, [])
    question = build_choice_question(open_resolution.question, question_id="length-1")
    events = [
        ("assistant", {"choice_question": question}),
        (
            "user",
            {"choice_selection": {"question_id": "length-1", "option_key": OPT_ACCEPT_NATURAL}},
        ),
    ]
    resolved = resolve_choices(strategy, brief(24), initial_snapshot, events)
    assert resolved.question is None
    assert resolved.strategy["target_duration_s"] == 12

    changed = resolve_choices(strategy, brief(24), {"best_parts": _parts(10)}, events)
    assert changed.question is not None
    assert changed.question.kind == CONFLICT_LENGTH_VS_NATURAL
    assert changed.question.effects[OPT_ACCEPT_NATURAL]["strategy"]["target_duration_s"] == 10


_TAKE_STRATEGY = {"audio_strategy": "original_audio", "edit_format": "talking_head"}
_TAKE_SNAPSHOT = {"clip_assignments": [{"media_id": "take", "duration_s": 51.6}]}


def _take_brief(*cleanup_asks: str) -> CreativeBrief:
    base = brief(40)
    extra = [
        BriefRequirement(id=f"cleanup{i}", kind="timing", scope="global", description=text)
        for i, text in enumerate(cleanup_asks)
    ]
    return CreativeBrief(version=1, requirements=[*base.requirements, *extra])


def test_a_long_take_still_asks_when_the_brief_cannot_shorten_it():
    # KRI-629 control: the question stays for a brief with no cleanup in it.
    found = collect_conflicts(_TAKE_STRATEGY, _take_brief(), _TAKE_SNAPSHOT)
    assert any(item.kind == CONFLICT_LENGTH_VS_NATURAL for item in found)


def test_a_long_take_does_not_ask_about_length_when_the_brief_cuts_the_pauses():
    found = collect_conflicts(
        _TAKE_STRATEGY, _take_brief("Cut out the long pauses"), _TAKE_SNAPSHOT
    )
    assert not any(item.kind == CONFLICT_LENGTH_VS_NATURAL for item in found)


def test_turkish_chat_gets_no_english_in_the_length_question():
    """KRI-630: intro and reason were English, glued behind Turkish connectors."""
    from app.kria.reply_language import reply_language_for
    from app.services.choice_questions import choice_question_text

    strategy = {"audio_strategy": "original_audio", "edit_format": "talking_head"}
    snapshot = {"clip_assignments": [{"media_id": "take", "duration_s": 51.6}]}
    with reply_language_for("tr"):
        found = choice(strategy, snapshot, 40)
        text = choice_question_text(found.candidate())
    assert "Your brief" not in text and "cannot make" not in text
    assert "konuşma kaydı" in found.intro and "51.6" in found.intro
    assert "Maalesef kaynağın" in text
    english = choice(strategy, snapshot, 40)
    assert english.intro.startswith("Your brief says")
