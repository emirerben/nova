from app.kria.brief import BriefRequirement, CreativeBrief
from app.services.length_policy import (
    has_creator_duration_proof,
    length_owner,
    natural_length_owner,
)


def test_natural_owner_covers_every_known_format_and_audio_source() -> None:
    formats = (
        "montage",
        "talking_head",
        "day_vlog",
        "single_hero",
        "subtitled",
        "narrated",
        "narrated_planned",
        "narrated_ready",
        "slides",
    )
    audio_sources = ("licensed_music", "original_audio", "voiceover", "user_song")
    owners = {
        natural_length_owner({"edit_format": edit_format, "audio_strategy": audio})
        for edit_format in formats
        for audio in audio_sources
    }
    assert owners == {"song", "voiceover", "take", "footage"}


def test_explicit_creator_proof_overrides_natural_owner() -> None:
    strategy = {"edit_format": "subtitled", "audio_strategy": "original_audio"}
    assert natural_length_owner(strategy) == "take"
    assert length_owner(strategy, creator_duration_proven=False) == "take"
    assert length_owner(strategy, creator_duration_proven=True) == "creator"


def test_only_creator_text_live_brief_or_authoritative_answer_prove_duration() -> None:
    brief = CreativeBrief(
        requirements=[
            BriefRequirement(
                id="length", kind="timing", scope="global", description="", facts={"duration_s": 24}
            )
        ]
    )
    assert not has_creator_duration_proof(
        strategy={"target_duration_s": 24, "target_duration_requested": True}
    )
    assert has_creator_duration_proof(creator_texts=["Make it 24 seconds."])
    assert has_creator_duration_proof(brief=brief)
    assert has_creator_duration_proof(
        authoritative_answers=[{"conflict": "duration_vs_count", "duration_s": 30}]
    )


def test_licensed_music_is_song_owned_only_with_a_frozen_section() -> None:
    strategy = {"edit_format": "montage", "audio_strategy": "licensed_music"}
    assert natural_length_owner(strategy) == "footage"
    assert (
        natural_length_owner(
            {**strategy, "selected_music_section": {"start_s": 12.0, "end_s": 30.0}}
        )
        == "song"
    )
