"""KRI-679: a footage-rich montage must not shrink to the matcher's song section.

Prod 2026-10-15 (Tigre night M1, thread 133BDDED): 12 clips (~150 s), "calm music ... 25
seconds". The server froze The Weeknd "The Hills" 42.86-59.50 s (16.6 s of a 234 s track)
as the length owner and asked "use 16.6 s?". After the creator accepted, the clip
selection changed, the music input digest no longer matched, the matcher ran again and
froze "Hawái (Remix)" 48.53-58.53 s, and the next turn asked "use 10 s?".

Ways this can fail (written before the fix):
1. A section shorter than the creator's length stays short -> the length question fires.
2. Extending past the end of the track -> the render rejects the window (end > catalog).
3. A section near the end of the track cannot extend forward -> must start earlier, >= 0.
4. A track shorter than the asked length -> use the whole track, never invent audio.
5. A section longer than the asked length still asks "trim it?" for a background bed.
6. No creator length, a footage fallback, or malformed numbers -> must stay untouched.
7. Footage shorter than the asked length -> the footage fit must still win afterwards.
8. A selection change after an answer re-runs the matcher -> a new, shorter section.
9. New footage (a different clip pool) or a different audio choice must still re-match.
10. Old question contexts without the pool key must not be reused on a digest miss.
"""

import pytest

from app.kria.brief import BriefRequirement, CreativeBrief
from app.kria.planner import (
    _fit_music_section_to_footage,
    _fit_music_section_to_length,
    _music_pool_digest,
    _prior_frozen_music,
)
from app.services.choice_questions import CONFLICT_LENGTH_VS_NATURAL, collect_conflicts

HILLS = {
    "status": "selected",
    "track_id": "bc902aad-41b5-40e0-9089-14cbd5d0ffbe",
    "title": "The Hills",
    "artist": "The Weeknd",
    "start_s": 42.86,
    "end_s": 59.5,
    "catalog_duration_s": 234.194,
    "beat_timestamps_s": [42.9, 43.4],
}


def _video(media_id: str, duration_s: float) -> dict:
    return {"media_id": media_id, "kind": "video", "duration_s": duration_s}


def _brief(seconds: float) -> CreativeBrief:
    return CreativeBrief(
        requirements=[
            BriefRequirement(
                id="r1",
                kind="timing",
                scope="global",
                description=f"Hızlı kesmeler, {seconds:g} saniye",
                facts={"duration_s": seconds},
            )
        ]
    )


def test_short_section_is_extended_to_the_asked_length() -> None:
    fitted = _fit_music_section_to_length(HILLS, 25.0)

    assert fitted["start_s"] == 42.86
    assert fitted["end_s"] == pytest.approx(67.86)
    assert fitted["track_id"] == HILLS["track_id"]
    assert fitted["beat_timestamps_s"] == HILLS["beat_timestamps_s"]


def test_section_near_the_track_end_starts_earlier_instead_of_running_past_it() -> None:
    fitted = _fit_music_section_to_length({**HILLS, "start_s": 220.0, "end_s": 230.0}, 25.0)

    assert fitted["end_s"] == pytest.approx(234.194)
    assert fitted["start_s"] == pytest.approx(209.194)
    assert fitted["end_s"] <= HILLS["catalog_duration_s"]


def test_track_shorter_than_the_asked_length_uses_the_whole_track() -> None:
    fitted = _fit_music_section_to_length(
        {**HILLS, "start_s": 5.0, "end_s": 12.0, "catalog_duration_s": 20.0}, 25.0
    )

    assert (fitted["start_s"], fitted["end_s"]) == (0.0, 20.0)


def test_section_longer_than_the_asked_length_is_trimmed_to_it() -> None:
    fitted = _fit_music_section_to_length({**HILLS, "end_s": 82.86}, 25.0)

    assert fitted["start_s"] == 42.86
    assert fitted["end_s"] == pytest.approx(67.86)


@pytest.mark.parametrize(
    ("section", "target"),
    (
        (HILLS, None),
        (HILLS, 0.0),
        ({"status": "fallback_footage"}, 25.0),
        ({**HILLS, "start_s": True}, 25.0),
        ({**HILLS, "end_s": "59.5"}, 25.0),
        ({**HILLS, "end_s": 40.0}, 25.0),
        ({**HILLS, "end_s": 42.86 + 25.0}, 25.0),
    ),
    ids=("no-target", "zero-target", "fallback", "bool", "string", "inverted", "already-fits"),
)
def test_untouched_when_there_is_nothing_safe_to_change(section: dict, target) -> None:
    assert _fit_music_section_to_length(section, target) == section


def test_unknown_track_length_never_extends_but_still_trims() -> None:
    unknown = {key: value for key, value in HILLS.items() if key != "catalog_duration_s"}

    assert _fit_music_section_to_length(unknown, 25.0) == unknown
    assert _fit_music_section_to_length({**unknown, "end_s": 90.0}, 25.0)["end_s"] == (
        pytest.approx(67.86)
    )


def test_input_section_is_never_mutated() -> None:
    before = dict(HILLS)
    _fit_music_section_to_length(HILLS, 25.0)

    assert before == HILLS


def test_footage_shorter_than_the_asked_length_still_wins() -> None:
    fitted = _fit_music_section_to_footage(
        _fit_music_section_to_length(HILLS, 25.0), [_video("a", 9.0)], selected_ids=set()
    )

    assert fitted["end_s"] == pytest.approx(51.86)


def test_incident_footage_rich_montage_asks_no_length_question() -> None:
    rows = [_video(f"m{i}", 12.5) for i in range(12)]
    brief = _brief(25)

    def asked(section: dict) -> bool:
        strategy = {
            "edit_format": "montage",
            "audio_strategy": "licensed_music",
            "selected_music_section": section,
        }
        conflicts = collect_conflicts(strategy, brief, {"clip_assignments": rows})
        return any(c.kind == CONFLICT_LENGTH_VS_NATURAL for c in conflicts)

    assert asked(HILLS)  # before: "the available song is 16.6 seconds"
    fitted = _fit_music_section_to_footage(
        _fit_music_section_to_length(HILLS, 25.0), rows, selected_ids=set()
    )
    assert not asked(fitted)


def _question_event(input_digest: str, section: dict, pool_digest: str | None) -> tuple:
    context = {"input_digest": input_digest, "licensed_music": section}
    if pool_digest is not None:
        context["pool_digest"] = pool_digest
    return (
        "assistant",
        {"choice_question": {"kind": "length_vs_natural", "length_context": context}},
    )


ROWS = [_video("a", 10.0), _video("b", 20.0)]


def test_selection_change_after_an_answer_keeps_the_same_song() -> None:
    pool = _music_pool_digest(ROWS, "licensed_music")
    events = [_question_event("digest-before-answer", HILLS, pool), ("user", {})]

    assert _prior_frozen_music(events, "digest-after-answer", pool) == HILLS


def test_exact_input_digest_match_wins_and_latest_question_wins() -> None:
    pool = _music_pool_digest(ROWS, "licensed_music")
    older = {**HILLS, "track_id": "older"}
    events = [_question_event("d1", older, pool), _question_event("d2", HILLS, pool)]

    assert _prior_frozen_music(events, "d1", pool) == older
    assert _prior_frozen_music(events, "d3", pool) == HILLS


def test_exact_digest_still_reuses_a_footage_fallback() -> None:
    fallback = {"status": "fallback_footage"}
    events = [_question_event("d1", fallback, "pool")]

    assert _prior_frozen_music(events, "d1", "pool") == fallback
    assert _prior_frozen_music(events, "d2", "pool") is None


@pytest.mark.parametrize(
    ("rows", "audio"),
    (
        ([*ROWS, _video("c", 5.0)], "licensed_music"),
        (ROWS, "original_audio"),
    ),
    ids=("new-footage", "different-audio"),
)
def test_new_footage_or_audio_choice_re_matches(rows: list, audio: str) -> None:
    events = [_question_event("d1", HILLS, _music_pool_digest(ROWS, "licensed_music"))]

    assert _prior_frozen_music(events, "d2", _music_pool_digest(rows, audio)) is None


def test_pool_digest_ignores_row_order_and_analysis_updates() -> None:
    analysed = [{**ROWS[1], "analysis": {"description": "boat"}}, ROWS[0]]

    assert _music_pool_digest(analysed, "licensed_music") == _music_pool_digest(
        ROWS, "licensed_music"
    )


def test_old_contexts_without_a_pool_key_only_match_exactly() -> None:
    events = [_question_event("d1", HILLS, None)]

    assert _prior_frozen_music(events, "d1", "pool") == HILLS
    assert _prior_frozen_music(events, "d2", "pool") is None
