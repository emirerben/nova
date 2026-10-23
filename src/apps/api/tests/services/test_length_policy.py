from app.kria.brief import BriefRequirement, CreativeBrief
from app.services.length_policy import (
    creator_max_export_duration_s,
    has_creator_duration_proof,
    length_owner,
    natural_length_owner,
    strict_duration_limit_s,
    strict_duration_matches,
)


def _requirement(
    description: str, seconds: float, *, scope: str = "global", id: str = "duration"
) -> BriefRequirement:
    return BriefRequirement(
        id=id, kind="timing", scope=scope, description=description, facts={"duration_s": seconds}
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


def test_strict_duration_limit_recognizes_narrow_english_and_turkish_bounds() -> None:
    assert strict_duration_limit_s(_requirement("Keep it under 40 seconds", 40)) == 40
    assert strict_duration_limit_s(_requirement("Make it shorter than 20 seconds", 20)) == 20
    assert strict_duration_limit_s(_requirement("40 saniyenin altında olsun", 40)) == 40
    assert strict_duration_limit_s(_requirement("40 saniyeden kısa olsun", 40)) == 40


def test_strict_duration_limit_rejects_approximate_inclusive_moment_and_quoted_text() -> None:
    assert strict_duration_limit_s(_requirement("about 40 seconds", 40)) is None
    assert strict_duration_limit_s(_requirement("at most 40 seconds", 40)) is None
    assert strict_duration_limit_s(_requirement("Keep the moment under 40 seconds", 40)) is None
    assert strict_duration_limit_s(_requirement('Use the text "under 40 seconds"', 40)) is None
    assert (
        strict_duration_limit_s(_requirement("Keep it under 40 seconds", 40, scope="per_clip"))
        is None
    )


def test_strict_duration_reads_its_own_clause_without_rejecting_cleanup_or_clip_context() -> None:
    assert (
        strict_duration_limit_s(_requirement("Cut pauses and keep it under 40 seconds", 40)) == 40
    )
    assert (
        strict_duration_limit_s(_requirement("Use all clips; keep the video under40 seconds", 40))
        == 40
    )
    assert strict_duration_limit_s(_requirement("Keep each clip under 40 seconds", 40)) is None


def test_strict_duration_rejects_negation_and_title_copy_but_accepts_literal_only() -> None:
    assert strict_duration_limit_s(_requirement("not under 40 seconds", 40)) is None
    assert strict_duration_limit_s(_requirement("Don't keep it under40 seconds", 40)) is None
    assert strict_duration_limit_s(_requirement("under 40 seconds title text", 40)) is None
    literal = BriefRequirement(
        id="literal",
        kind="timing",
        scope="global",
        literal="Keep it under 40 seconds",
        facts={"duration_s": 40},
    )
    assert strict_duration_limit_s(literal) == 40


def test_strict_duration_keeps_an_unrelated_negative_cleanup_instruction() -> None:
    assert (
        strict_duration_limit_s(
            _requirement("Don't cut any words and keep it under 40 seconds", 40)
        )
        == 40
    )


def test_strict_duration_rejects_turkish_trailing_negation() -> None:
    assert strict_duration_limit_s(_requirement("40 saniyenin altında olmasın", 40)) is None
    assert (
        strict_duration_limit_s(_requirement("40 saniyenin altında olmasını istemiyorum", 40))
        is None
    )


def test_strict_duration_requires_the_recorded_fact_to_match_the_words() -> None:
    assert strict_duration_limit_s(_requirement("Keep it under 40 seconds", 39)) is None


def test_creator_max_export_duration_picks_the_tightest_live_bound() -> None:
    brief = CreativeBrief(
        requirements=[
            _requirement("Keep it under 40 seconds", 40),
            _requirement("Make it shorter than 20 seconds", 20, id="tighter"),
        ]
    )
    assert creator_max_export_duration_s(brief=brief) == 20


def test_strict_duration_matches_has_an_exclusive_exact_boundary() -> None:
    assert strict_duration_matches(39.99, 40)
    assert not strict_duration_matches(40, 40)
    assert not strict_duration_matches(40.01, 40)
    assert not strict_duration_matches(float("nan"), 40)
