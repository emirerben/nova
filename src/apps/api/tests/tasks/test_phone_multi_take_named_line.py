"""KRI-720 (G3): a line the creator named to cut ("cut the okay") in a Talking video of several
takes is cut from the ONE take that says it, and everything after it shifts.

Failure modes (docs/reviews/kri-720-multi-take-cleanup.md N1-N11, written before the code):

N1  the line is said in two takes -> nothing is cut, the receipt says "more than one take"
N2  said twice in one take, no pick -> refused as on one take (`said_more_than_once`)
N3  never said -> `not_found`
N5  the cut eats a neighbour word / the join between takes is not re-trimmed afterwards
N6  a line in take B is searched in take 1's words (wrong take): cut in take 1, or not found
N7  later takes, captions and photos do not shift by the removed time
N9  a start / end quote on several takes is applied to take 1 only (silently) instead of refused
N10 cleanup declined -> the takes play whole, the line is refused honestly
N11 flag off is byte-identical (the several-takes refusal)
"""

from __future__ import annotations

import uuid
from types import SimpleNamespace

import pytest

from app.kria.brief import BriefRequirement, CreativeBrief
from app.kria.brief_binding import BriefBinding
from app.pipeline.speech_cleanup_analysis import (
    SpeechCleanupAnalysisInput,
    SpeechCleanupMultiTakeResult,
    SpeechCleanupTakeResult,
    analyze_speech_cleanup,
    multi_take_public_receipt,
)
from app.pipeline.transcribe import Word
from app.services.clip_speech import SilenceDetectionResult
from app.services.device_render import device_status
from app.tasks import generative_build as gb
from tests._silent_pcm import silent_pcm
from tests.tasks.test_phone_subtitled_narrated_dispatch import (
    _raw_preflight_snapshot,
    _setup_talking_multi,
)

DUR = 6.5


def _w(*spans: tuple[str, float, float]) -> list[Word]:
    return [Word(text=t, start_s=s, end_s=e, confidence=1.0) for t, s, e in spans]


# Tightly packed, no fillers, no silences: every take's own cleanup plan removes nothing, so the
# only cut in the finished video is the one the creator named.
TAKE_A = _w(
    ("we", 0.5, 0.7),
    ("built", 0.8, 1.1),
    ("the", 1.2, 1.4),
    ("okay", 1.5, 1.9),
    ("thing", 2.0, 2.3),
    ("and", 2.4, 2.6),
    ("it", 2.7, 2.9),
    ("works", 3.0, 3.4),
    ("friends.", 3.5, 4.2),
)
TAKE_B = _w(
    ("then", 0.5, 0.8),
    ("the", 0.9, 1.1),
    ("second", 1.2, 1.7),
    ("part", 1.8, 2.2),
    ("goes", 2.3, 2.6),
    ("like", 2.7, 3.0),
    ("this.", 3.1, 3.6),
)
TAKE_C = _w(
    ("and", 0.5, 0.7),
    ("wait", 0.8, 1.1),
    ("the", 1.2, 1.4),
    ("light", 1.5, 1.9),
    ("is", 2.0, 2.2),
    ("gone", 2.3, 2.7),
    ("bye.", 2.8, 3.3),
)
HEARD = {"take1": TAKE_A, "take2": TAKE_B, "take3": TAKE_C}


def _analysis(words, fingerprint):
    return analyze_speech_cleanup(
        SpeechCleanupAnalysisInput(
            source_fingerprint=fingerprint,
            local_media_path="preflight.wav",
            duration_s=DUR,
            source_window_start_s=0.0,
            source_window_end_s=DUR,
            mixed_gap_mode="off",
            over_budget_policy="clamp",
        ),
        transcribe_fn=lambda *_a, **_k: SimpleNamespace(
            words=words, language="en", low_confidence=False
        ),
        silence_detect_fn=lambda *_a, **_k: SilenceDetectionResult(spans=[], status="ok"),
        pcm_loader=silent_pcm,
    )


def _snapshot(takes):
    """Schema-2 job snapshot: ``takes`` = [(media_id, storage_path, words)] in play order."""
    single = _raw_preflight_snapshot(storage_path="unused")
    fingerprint = single["source"]["source_policy_fingerprint"]
    rows = tuple(
        SpeechCleanupTakeResult.model_validate(
            {
                "media_id": media_id,
                "storage_path": path,
                "generation": "1",
                "window_start_s": 0.0,
                "window_end_s": DUR,
                "speaker": True,
                "analysis": _analysis(words, fingerprint).to_payload(),
            }
        )
        for media_id, path, words in takes
    )
    payload = SpeechCleanupMultiTakeResult(
        source_fingerprint=fingerprint,
        detector_version=single["detector_version"],
        takes=rows,
        public_receipt=multi_take_public_receipt(rows),
    ).to_payload()
    first_id, first_path, _ = takes[0]
    return {
        **single,
        "schema_version": 2,
        "source": {
            **single["source"],
            "media_identity": first_id,
            "storage_path": first_path,
            "window_end_s": DUR,
        },
        "analysis": payload,
    }


def _req(cut_lines, description, rid="r1", kind="audio", **facts):
    return BriefRequirement(
        id=rid,
        kind=kind,
        scope="global",
        description=description,
        facts={"speech_cleanup": True, "cut_lines": cut_lines, **facts},
    )


def _render(monkeypatch, requirements, *, heard=None, flag=True, cleanup=True):
    heard = heard or HEARD
    clips = [(cid, DUR, 8) for cid in heard]
    job, snapshot, bindings = _setup_talking_multi(monkeypatch, clips=clips)
    monkeypatch.setattr(gb.settings, "talking_multi_take_cleanup_enabled", flag)
    monkeypatch.setattr(gb.settings, "speech_cleanup_long_pauses_enabled", False)
    if cleanup:
        snapshot["speech_cleanup_contract"] = "required_v1"
        snapshot["_speech_cleanup_internal"] = {
            "preflight_snapshot": _snapshot(
                [(cid, bindings[cid].proxy_path, heard[cid]) for cid in heard]
            )
        }
    brief = CreativeBrief(version=1, requirements=list(requirements))
    snapshot["creator_brief_binding"] = BriefBinding.create(uuid.uuid4(), brief).model_dump(
        mode="json"
    )
    job.assembly_plan["creator_brief_binding"] = snapshot["creator_brief_binding"]
    gb._run_phone_subtitled_job(str(job.id), snapshot, job.all_candidates, ownership_epoch=3)
    return job


def _main(job):
    recipe = device_status(job, "subtitled").request.recipe
    clips = next(t for t in recipe.tracks if t.id == "subtitled").clips
    return recipe, clips


def _kept(clips, asset):
    return [
        (round(c.source_start, 3), round(c.source_start + c.source_duration, 3))
        for c in clips
        if c.source_asset_id == asset
    ]


def _named_rows(job):
    return job.assembly_plan["variants"][0]["named_cuts"]


OKAY = _req(["okay"], "Cut the okay")


def test_a_line_said_only_in_take_a_is_cut_there_and_everything_after_shifts(monkeypatch):
    job = _render(monkeypatch, [OKAY])

    recipe, clips = _main(job)
    (row,) = _named_rows(job)
    assert row["status"] == "applied" and row["take"]["index"] == 1
    assert row["take"]["media_id"] == "take1"
    start, end = row["start_s"], row["end_s"]
    # Word boundaries: the cut sits between "the" (ends 1.4) and "thing" (starts 2.0).
    assert 1.4 <= start <= 1.5 and 1.9 <= end <= 2.0
    assert _kept(clips, "take1") == [(0.0, start), (end, DUR)]
    # B and C play whole, after A's remaining seconds.
    assert _kept(clips, "take2") == [(0.0, DUR)] and _kept(clips, "take3") == [(0.0, DUR)]
    removed = end - start
    assert recipe.duration == pytest.approx(3 * DUR - removed, abs=1e-3)
    cursor = 0.0
    for clip in clips:
        assert clip.timeline_start == pytest.approx(cursor, abs=1e-6)
        cursor += clip.source_duration

    cues = job.assembly_plan["variants"][0]["caption_cues"]
    text = " ".join(cue["text"] for cue in cues).lower()
    assert "okay" not in text and "thing" in text and "built" in text
    # Take B's first word starts exactly where A's kept video ends (shifted by the cut).
    take_a_s = DUR - removed
    then = next(cue for cue in cues if cue["text"].lower().startswith("then"))
    assert then["start_s"] == pytest.approx(take_a_s + 0.5, abs=1e-3)
    # "thing" is the next word after the cut; it moved up by exactly the removed time.
    spoken = {w["text"]: w for cue in cues for w in cue["words"]}
    assert spoken["thing"]["start_s"] == pytest.approx(2.0 - removed, abs=1e-3)
    assert spoken["built"]["end_s"] == pytest.approx(1.1, abs=1e-3)  # before the cut: unmoved


def test_the_cut_is_a_named_cut_not_counted_as_cleanup(monkeypatch):
    job = _render(monkeypatch, [OKAY])
    context = job.assembly_plan["variants"][0]["_speech_cleanup_outcome_context"]
    # Same as one take: a named line is the creator's cut, not cleanup's own count.
    assert context["output_removal_count"] == 0


def test_a_line_said_only_in_take_c_is_cut_in_take_c_only(monkeypatch):
    job = _render(monkeypatch, [_req(["wait the light"], "Cut where I say 'wait the light'")])

    _recipe, clips = _main(job)
    (row,) = _named_rows(job)
    assert row["status"] == "applied" and row["take"]["index"] == 3
    assert row["take"]["media_id"] == "take3"
    assert _kept(clips, "take1") == [(0.0, DUR)] and _kept(clips, "take2") == [(0.0, DUR)]
    (first, second) = _kept(clips, "take3")
    assert first[0] == 0.0 and second[1] == DUR
    # "and" (ends 0.7) survives, "is" (starts 2.0) survives.
    assert 0.7 <= first[1] <= 0.8 and 1.9 <= second[0] <= 2.0
    # The cut take's own caption words moved; the earlier takes' did not.
    cues = job.assembly_plan["variants"][0]["caption_cues"]
    assert not any("light" in cue["text"].lower() for cue in cues)
    assert any(cue["text"].lower().startswith("then") for cue in cues)


def test_a_line_not_named_stays(monkeypatch):
    job = _render(monkeypatch, [_req(["zebra stripes"], "Cut 'zebra stripes'")])
    _recipe, clips = _main(job)
    (row,) = _named_rows(job)
    assert row["status"] == "not_applied" and row["reason"] == "not_found"
    assert [_kept(clips, t) for t in HEARD] == [[(0.0, DUR)]] * 3
    cues = job.assembly_plan["variants"][0]["caption_cues"]
    assert "okay" in " ".join(cue["text"] for cue in cues).lower()  # control: untouched


def test_the_same_line_in_two_takes_cuts_nothing(monkeypatch):
    heard = {
        "take1": TAKE_A,
        "take2": _w(("so", 0.5, 0.7), ("okay", 1.0, 1.4), ("next", 1.5, 1.9)),
        "take3": TAKE_C,
    }
    job = _render(monkeypatch, [OKAY], heard=heard)
    _recipe, clips = _main(job)
    (row,) = _named_rows(job)
    assert row["status"] == "not_applied" and row["reason"] == "said_in_several_takes"
    assert row["takes"] == [1, 2]
    assert [_kept(clips, t) for t in heard] == [[(0.0, DUR)]] * 3


def test_a_line_said_twice_in_one_take_without_a_pick_is_refused(monkeypatch):
    heard = {
        "take1": _w(("okay", 0.5, 0.9), ("so", 1.0, 1.2), ("okay", 1.5, 1.9), ("go", 2.0, 2.3)),
        "take2": TAKE_B,
    }
    job = _render(monkeypatch, [OKAY], heard=heard)
    (row,) = _named_rows(job)
    assert row["status"] == "not_applied" and row["reason"] == "said_more_than_once"


def test_a_second_time_counts_across_takes_in_play_order(monkeypatch):
    heard = {
        "take1": TAKE_A,
        "take2": _w(("so", 0.5, 0.7), ("okay", 1.0, 1.4), ("next", 1.5, 1.9)),
    }
    job = _render(
        monkeypatch,
        [_req(["okay"], "Cut the second okay", cut_line_occurrences={"okay": 2})],
        heard=heard,
    )
    (row,) = _named_rows(job)
    assert row["status"] == "applied" and row["take"]["index"] == 2
    _recipe, clips = _main(job)
    assert _kept(clips, "take1") == [(0.0, DUR)]
    assert len(_kept(clips, "take2")) == 2


def test_a_start_quote_on_several_takes_is_refused_not_applied_to_take_one(monkeypatch):
    job = _render(
        monkeypatch,
        [
            _req(
                [],
                "Start the video at 'we built the okay' and end after 'gone bye'",
                kind="timing",
                start_line="we built the okay",
                end_line="gone bye",
            )
        ],
    )
    variant = job.assembly_plan["variants"][0]
    _recipe, clips = _main(job)
    assert [_kept(clips, t) for t in HEARD] == [[(0.0, DUR)]] * 3
    for field in ("opening_cut", "end_cut"):
        assert variant[field]["status"] == "not_applied", field
        assert variant[field]["reason"] == "several_takes", field


def test_flag_off_is_the_several_takes_refusal(monkeypatch):
    with pytest.raises(gb.SpeechCleanupFailure):
        _render(monkeypatch, [OKAY], flag=False)


def test_editor_save_keeps_a_cut_whose_only_cause_is_the_named_line(monkeypatch):
    """N8: the editor rebuilds each take's kept windows from the pinned recipe, and the receipt
    lists the removed spans of a later take (`speaker_removed`) / the first take (`silence_cut`)."""
    from app.pipeline.phone_subtitled_plan import additional_speaker_keep_segments_from_recipe
    from app.services.phone_sources import PHONE_SOURCES_FIELD, PhoneSourceBinding

    # A named line in take C: the extra take's only cut.
    job = _render(monkeypatch, [_req(["wait the light"], "Cut where I say 'wait the light'")])
    recipe, clips = _main(job)
    (row,) = _named_rows(job)
    bindings = tuple(
        PhoneSourceBinding.model_validate(raw) for raw in job.assembly_plan[PHONE_SOURCES_FIELD]
    )
    keep = additional_speaker_keep_segments_from_recipe(recipe, bindings)
    assert set(keep) == {"take3"}
    assert [tuple(round(x, 3) for x in w) for w in keep["take3"]] == _kept(clips, "take3")
    multi = job.assembly_plan["variants"][0][gb.PHONE_TALKING_MULTI_CLIP_FIELD]
    (removed_row,) = multi["speaker_removed"]
    assert removed_row["media_id"] == "take3"
    assert [(r["start_s"], r["end_s"]) for r in removed_row["removed"]] == [
        (row["start_s"], row["end_s"])
    ]

    # A named line in take A: the first take's cut is the variant's own `silence_cut` summary.
    job = _render(monkeypatch, [OKAY])
    (row,) = _named_rows(job)
    first = job.assembly_plan["variants"][0]["silence_cut"]
    assert [(r["start_s"], r["end_s"]) for r in first["removed"]] == [
        (row["start_s"], row["end_s"])
    ]


def test_cleanup_declined_plays_whole_and_refuses_the_line(monkeypatch):
    job = _render(monkeypatch, [OKAY], cleanup=False)
    _recipe, clips = _main(job)
    (row,) = _named_rows(job)
    assert row["status"] == "not_applied" and row["reason"] == "several_takes"
    assert [_kept(clips, t) for t in HEARD] == [[(0.0, DUR)]] * 3
