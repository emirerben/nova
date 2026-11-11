"""KRI-720 G1: a Talking video made of several takes cuts EVERY pause tight, joins included.

Çiğdem's real S3 message says plain "Cut the pauses" and her format allows no pause over 0.4 s.
Failure modes written before the code (docs/reviews/kri-720-multi-take-cleanup.md T1-T11), each
pinned below against REAL synthesized audio (speech-shaped tone + pauses) and the real cutter:

T1  a tight ask on several takes got no pause pass at all (long pass skips tight briefs, tight pass
    skips several takes), and a plain ask only the >= 0.6 s pass.
T2/T3 the join trim cuts a soft word ending, or keeps 0.15 + 0.15 s, on the limit.
T4  every kept segment is a clip on ONE main track: three tight takes can exceed it.
T5/T6 a noisy / unmeasured take is summed away instead of reported on its own.
T8  the outcome and the receipts see only the first take.
T10 a take with no plan (footage ending) sits between two cut takes.
T11 flag off / one take: byte-identical to before.
"""

from __future__ import annotations

import wave
from pathlib import Path

import numpy as np
import pytest

from app.pipeline.speech_cleanup_analysis import (
    SpeechCleanupAnalysisInput,
    SpeechCleanupMultiTakeResult,
    SpeechCleanupTakeResult,
    analyze_speech_cleanup,
    multi_take_public_receipt,
)
from app.pipeline.transcribe import Word
from app.services import clip_speech
from app.services.device_render import device_status
from app.tasks import generative_build as gb
from tests._silent_pcm import silent_pcm
from tests.pipeline.test_tight_pause_cut import SR, _floor, _word_tone
from tests.tasks.test_phone_subtitled_narrated_dispatch import (
    _raw_preflight_snapshot,
    _setup_talking_multi,
)
from tests.tasks.test_phone_tight_pauses import TIGHT_ASK
from tests.tasks.test_phone_tight_pauses import _snapshot as _brief_snapshot

# gaps between consecutive words, seconds: plain natural pauses that a 0.6 s rule keeps.
TAKE_GAPS = {
    "take1": [0.45, 0.9, 0.35, 0.55],
    "take2": [0.5, 0.45, 0.4],
    "take3": [0.5, 0.7, 0.45],
}
TIGHT_LIMIT = 0.355


def _write_take(tmp_path: Path, name: str, gaps: list[float], *, floor_amp: float = 3e-4):
    """A take file: 0.5 s lead, 0.4 s words separated by ``gaps``, 0.8 s tail. Returns the
    path, words (dicts) and duration."""
    rng = np.random.default_rng(7)
    words: list[dict] = []
    t = 0.5
    pieces = [_floor(int(0.5 * SR), rng, floor_amp)]
    for i in range(len(gaps) + 1):
        words.append({"text": f"{name}w{i}", "start_s": t, "end_s": t + 0.4})
        pieces.append(_word_tone(0.4))
        t += 0.4
        if i < len(gaps):
            pieces.append(_floor(int(gaps[i] * SR), rng, floor_amp))
            t += gaps[i]
    pieces.append(_floor(int(0.8 * SR), rng, floor_amp))
    samples = np.concatenate(pieces).astype(np.float32)
    path = tmp_path / f"{name}.wav"
    with wave.open(str(path), "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(SR)
        w.writeframes((np.clip(samples, -1, 1) * 32767).astype(np.int16).tobytes())
    return str(path), words, len(samples) / SR


def _analysis(path: str, words: list[dict], duration: float):
    single = _raw_preflight_snapshot(storage_path="unused")
    silences = clip_speech.detect_silences_with_status(
        path, min_silence_s=0.1, ambient_adaptive=True
    ).spans
    transcript_words = [
        Word(text=w["text"], start_s=w["start_s"], end_s=w["end_s"], confidence=1.0) for w in words
    ]
    return analyze_speech_cleanup(
        SpeechCleanupAnalysisInput(
            source_fingerprint=single["source"]["source_policy_fingerprint"],
            local_media_path=path,
            duration_s=duration,
            source_window_start_s=0.0,
            source_window_end_s=duration,
            mixed_gap_mode="off",
            over_budget_policy="clamp",
        ),
        transcribe_fn=lambda *_a, **_k: type(
            "T", (), {"words": transcript_words, "language": "en", "low_confidence": False}
        )(),
        silence_detect_fn=lambda *_a, **_k: clip_speech.SilenceDetectionResult(
            spans=silences, status="ok"
        ),
        pcm_loader=silent_pcm,
    )


def _snapshot_for(takes: list[tuple[str, str, bool, list[dict], float, str]]) -> dict:
    """A schema-2 preflight snapshot: takes = [(id, storage path, analysed, words, duration,
    local path)]."""
    single = _raw_preflight_snapshot(storage_path="unused")
    rows = []
    for media_id, storage, analysed, words, duration, local in takes:
        analysis = _analysis(local, words, duration) if analysed else None
        rows.append(
            SpeechCleanupTakeResult.model_validate(
                {
                    "media_id": media_id,
                    "storage_path": storage,
                    "generation": "1",
                    "window_start_s": 0.0,
                    "window_end_s": duration,
                    "speaker": analysed,
                    "analysis": analysis.model_dump(mode="json") if analysis else None,
                }
            )
        )
    payload = SpeechCleanupMultiTakeResult(
        source_fingerprint=single["source"]["source_policy_fingerprint"],
        detector_version=single["detector_version"],
        takes=tuple(rows),
        public_receipt=multi_take_public_receipt(tuple(rows)),
    ).to_payload()
    first = takes[0]
    return {
        **single,
        "schema_version": 2,
        "source": {
            **single["source"],
            "media_identity": first[0],
            "storage_path": first[1],
            "window_end_s": first[4],
        },
        "analysis": payload,
    }


def _setup(monkeypatch, tmp_path, *, noisy=(), brief=None, tight=True, long=False):
    files = {}
    for name, gaps in TAKE_GAPS.items():
        files[name] = _write_take(tmp_path, name, gaps, floor_amp=0.03 if name in noisy else 3e-4)
    clips = [(name, f[2], 12) for name, f in files.items()]
    job, snapshot, bindings = _setup_talking_multi(monkeypatch, clips=clips)
    local = {cid: files[cid][0] for cid in files}
    monkeypatch.setattr(
        gb,
        "_ingest_clips",
        lambda *a, **k: {
            "clip_metas": [gb_meta(cid) for cid in files],
            "clip_id_to_gcs": {cid: b.proxy_path for cid, b in bindings.items()},
            "clip_id_to_local": local,
            "probe_map": {
                local[cid]: type("P", (), {"duration_s": files[cid][2]})() for cid in files
            },
            "hero": gb_meta("take1"),
        },
        raising=False,
    )
    monkeypatch.setattr(gb.settings, "talking_multi_take_cleanup_enabled", True)
    monkeypatch.setattr(gb.settings, "tight_pause_cut_enabled", tight)
    monkeypatch.setattr(gb.settings, "speech_cleanup_long_pauses_enabled", long)
    analysed = [
        (
            cid,
            bindings[cid].proxy_path,
            True,
            files[cid][1],
            files[cid][2],
            files[cid][0],
        )
        for cid in files
    ]
    snapshot["speech_cleanup_contract"] = "required_v1"
    snapshot["_speech_cleanup_internal"] = {"preflight_snapshot": _snapshot_for(analysed)}
    if brief is not None:
        snapshot.update(_brief_snapshot(brief))
    return job, snapshot, files


def gb_meta(cid):
    from tests.tasks.test_generative_build import _Meta

    return _Meta(cid, 5.0)


def _kept(job, take: str) -> list[tuple[float, float]]:
    recipe = device_status(job, "subtitled").request.recipe
    main = next(t for t in recipe.tracks if t.id == "subtitled").clips
    return [
        (c.source_start, c.source_start + c.source_duration)
        for c in main
        if c.source_asset_id == take
    ]


def _out_gap(kept, a_end: float, b_start: float) -> float:
    return sum(max(0.0, min(b_start, hi) - max(a_end, lo)) for lo, hi in kept)


def _assert_every_pause_is_tight(job, files):
    for name in TAKE_GAPS:
        kept = _kept(job, name)
        words = files[name][1]
        for a, b in zip(words, words[1:], strict=False):
            assert _out_gap(kept, a["end_s"], b["start_s"]) <= TIGHT_LIMIT, (name, a, b, kept)
        # No word is cut into.
        for w in words:
            assert any(lo <= w["start_s"] + 1e-3 and w["end_s"] <= hi + 1e-3 for lo, hi in kept), (
                name,
                w,
                kept,
            )


def _join_gap(job, files, left: str, right: str) -> float:
    lk, rk = _kept(job, left), _kept(job, right)
    tail = lk[-1][1] - files[left][1][-1]["end_s"]
    lead = files[right][1][0]["start_s"] - rk[0][0]
    return tail + lead


def _run(job, snapshot):
    gb._run_phone_subtitled_job(str(job.id), snapshot, job.all_candidates, ownership_epoch=3)
    assert job.status == "awaiting_device"
    return job.assembly_plan["variants"][0]


def test_a_plain_ask_on_three_takes_cuts_every_take_tight_and_the_joins(monkeypatch, tmp_path):
    job, snapshot, files = _setup(monkeypatch, tmp_path)
    variant = _run(job, snapshot)

    _assert_every_pause_is_tight(job, files)
    assert _join_gap(job, files, "take1", "take2") <= 0.3
    assert _join_gap(job, files, "take2", "take3") <= 0.3
    # Every take has its own report; the first is the variant's, the others are per take.
    assert variant["tight_pauses"]["status"] == "applied"
    others = variant["tight_pauses_other_takes"]
    assert [row["take"] for row in others] == [1, 2]
    assert all(row["status"] == "applied" for row in others)
    # Measured gaps, one per pair of neighbours, on the acceptance definition.
    gaps = variant["take_join_gaps"]
    assert [row["after_take"] for row in gaps] == [0, 1]
    assert all(row["gap_s"] is not None and row["gap_s"] <= 0.3 for row in gaps)
    assert variant["silence_cut_outcome"] == "applied"
    # The long pass did not run on top: tight owns the pauses.
    assert "long_pauses" not in variant and "long_pauses_other_takes" not in variant


def test_a_tight_ask_on_three_takes_is_the_same_cut(monkeypatch, tmp_path):
    plain_job, plain_snapshot, files = _setup(monkeypatch, tmp_path)
    plain = _run(plain_job, plain_snapshot)
    job, snapshot, _ = _setup(monkeypatch, tmp_path, brief=TIGHT_ASK)
    variant = _run(job, snapshot)

    _assert_every_pause_is_tight(job, files)
    assert variant["tight_pauses"]["status"] == "applied"
    assert [row["take"] for row in variant["tight_pauses_other_takes"]] == [1, 2]
    for take in TAKE_GAPS:
        assert _kept(job, take) == _kept(plain_job, take)
    assert variant["take_join_gaps"] == plain["take_join_gaps"]


def test_one_noisy_take_is_reported_on_its_own(monkeypatch, tmp_path):
    job, snapshot, files = _setup(monkeypatch, tmp_path, noisy=("take2",))
    variant = _run(job, snapshot)

    by_take = {
        0: variant["tight_pauses"],
        **{r["take"]: r for r in variant["tight_pauses_other_takes"]},
    }
    assert by_take[1]["reason"] == "noisy_take" and by_take[1]["added_cuts"] == 0
    assert by_take[0]["status"] == "applied" and by_take[2]["status"] == "applied"
    # The noisy take's pauses stay, so its join gaps are measured and reported, not promised.
    assert all("gap_s" in row for row in variant["take_join_gaps"])


def test_the_main_track_budget_is_shared_across_takes(monkeypatch, tmp_path):
    job, snapshot, files = _setup(monkeypatch, tmp_path)
    free = _run(job, snapshot)
    free_clips = sum(len(_kept(job, t)) for t in TAKE_GAPS)

    job2, snapshot2, _ = _setup(monkeypatch, tmp_path)
    monkeypatch.setattr(gb, "TAKE_TIGHT_MAX_CLIPS", free_clips - 3)
    capped = _run(job2, snapshot2)
    capped_clips = sum(len(_kept(job2, t)) for t in TAKE_GAPS)

    assert capped_clips <= free_clips - 3
    reports = [capped["tight_pauses"], *capped["tight_pauses_other_takes"]]
    assert any(r["capped"] for r in reports)
    assert free["tight_pauses"]["capped"] is False


def test_an_uncut_neighbour_plays_whole_and_the_budget_counts_it_as_one_clip(tmp_path):
    from app.pipeline.silence_cut import no_op_plan

    ending, _words, ending_s = _write_take(tmp_path, "ending", [0.4])
    take1, words1, take1_s = _write_take(tmp_path, "take1", [0.45, 0.9])
    plan = no_op_plan(take1_s)
    # The take before an uncut ending (no plan) keeps its measured tail; the ending's lead is its
    # own whole lead-in (0.5 s), so the join is measured, never assumed.
    (row,) = gb._measure_take_join_gaps([(plan, take1, take1_s), (None, ending, ending_s)])
    assert row["after_take"] == 0
    assert row["gap_s"] == pytest.approx(0.8 + 0.5, abs=0.05)
    # An unmeasurable neighbour is reported as unmeasured, not as a zero gap.
    (missing,) = gb._measure_take_join_gaps(
        [(plan, take1, take1_s), (None, str(tmp_path / "gone.wav"), 2.0)]
    )
    assert missing["gap_s"] is None
    # Budget: every other take counts its kept segments (an uncut take is one clip).
    order = [None, "b", "c"]
    plans = {None: plan, "b": None, "c": plan}
    assert gb._take_clip_budget(plans, order, None) == gb.TAKE_TIGHT_MAX_CLIPS - 1 - 1


def test_flag_off_keeps_the_old_long_pass_and_join_trim(monkeypatch, tmp_path):
    job, snapshot, files = _setup(monkeypatch, tmp_path, tight=False, long=True)
    variant = _run(job, snapshot)

    for key in ("tight_pauses", "tight_pauses_other_takes", "take_join_gaps"):
        assert key not in variant
    assert variant["long_pauses"]["mode"] == "long"
    assert len(variant["long_pauses_other_takes"]) == 2
    assert variant["take_join_trims"]


def test_one_take_plain_keeps_the_long_only_rule(monkeypatch, tmp_path):
    gb_snapshot = {}
    monkeypatch.setattr(gb.settings, "tight_pause_cut_enabled", True)
    plan = object()
    # No ask and no several-take flag: the glue never runs the cutter (same plan object, no report).
    assert gb._phone_tight_pause_cut(
        gb_snapshot, plan, [], clip_path="/nope.wav", duration_s=1.0
    ) == (plan, None)
