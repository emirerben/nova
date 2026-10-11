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
