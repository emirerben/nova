"""KRI-624: a moment ask's receipt checks the clips the moments came from.

The Tigre M2 re-send asked for "the best six moments from the long takes, each very short,
and skip the dark ones" and kept the original sound. Two of the four clips were short
ones the creator had ruled out, yet the moment receipt said "met" and the sound receipt
said "can't verify".
"""

from __future__ import annotations

from app.kria.brief import BriefRequirement
from app.kria.brief_checks import check_requirement, plan_facts_from_rendered_montage

LONG_A, LONG_B, SHORT_A, SHORT_B = "long-a", "long-b", "short-a", "short-b"
DURATIONS = {LONG_A: 48.0, LONG_B: 61.0, SHORT_A: 4.0, SHORT_B: 6.0}

PICK = BriefRequirement(
    id="r1",
    kind="select",
    scope="global",
    description="Pick the best six moments from the long takes, each very short",
)
PICK_AND_SKIP = BriefRequirement(
    id="r1",
    kind="select",
    scope="global",
    description="Pick the best six moments from the long takes, each very short, "
    "and skip the dark ones",
)
KEEP_SOUND = BriefRequirement(
    id="r2", kind="audio", scope="global", description="Keep the original sound"
)


def _facts(cut_ids: list[str], *, audio_kept: bool = True, song: bool = False):
    cuts = [
        {
            "lane": "clip",
            "media_id": media_id,
            "output_start_s": index * 2.0,
            "source_start_s": 1.0,
            "source_end_s": 3.0,
        }
        for index, media_id in enumerate(cut_ids)
    ]
    record = {
        "clip_ids": list(DURATIONS),
        "clip_durations_s": DURATIONS,
        "best_parts": [
            {"media_id": c["media_id"], "source_start_s": 1.0, "source_end_s": 3.0} for c in cuts
        ],
        **({"user_song": {"status": "placed"}} if song else {}),
    }
    variant = {"story_timeline": cuts, "source_audio_preserved": audio_kept}
    return plan_facts_from_rendered_montage(variant, record)


def test_moments_from_the_short_clips_are_not_met() -> None:
    facts = _facts([SHORT_A, SHORT_B, LONG_A, LONG_A, LONG_B, LONG_B])
    receipt = check_requirement(PICK, facts)
    assert receipt.status == "partial"
    assert "all 4 clips" in receipt.reason and "2 short ones" in receipt.reason


def test_moments_from_the_long_takes_only_are_met() -> None:
    facts = _facts([LONG_A, LONG_A, LONG_A, LONG_B, LONG_B, LONG_B])
    assert check_requirement(PICK, facts).status == "met"


def test_a_skip_clause_nobody_can_check_is_never_met() -> None:
    facts = _facts([LONG_A, LONG_A, LONG_A, LONG_B, LONG_B, LONG_B])
    receipt = check_requirement(PICK_AND_SKIP, facts)
    assert receipt.status == "partial"
    assert "left out" in receipt.reason


def test_unknown_clip_lengths_are_never_met() -> None:
    facts = _facts([LONG_A] * 6)
    import dataclasses

    blind = dataclasses.replace(facts, source_durations_s=None)
    assert check_requirement(PICK, blind).status == "partial"


def test_keeping_the_original_sound_is_met_when_nothing_plays_over_it() -> None:
    facts = _facts([LONG_A] * 6)
    receipt = check_requirement(KEEP_SOUND, facts)
    assert receipt.status == "met", receipt.reason


def test_keeping_the_original_sound_is_not_met_under_a_song_or_when_muted() -> None:
    assert check_requirement(KEEP_SOUND, _facts([LONG_A] * 6, song=True)).status == "partial"
    assert check_requirement(KEEP_SOUND, _facts([LONG_A] * 6, audio_kept=False)).status == "partial"
