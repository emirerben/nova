"""KRI-720: the several-take speech-cleanup check, before any render.

Failure modes pinned here (docs/reviews/kri-720-multi-take-cleanup.md): a consent outliving the
takes it was given for (F3/F6), footage with ambient sound counted as a take that talks
(F2), a silent take failing the whole check or a check with no talking take passing (F4),
the card's numbers not summing the talking takes (F5), and a tampered or mismatched
payload reaching the render (wrong-take control). The flag-off resolver path is pinned by
`test_subtitled_never_guesses_between_multiple_embedded_sources`.
"""

from __future__ import annotations

import uuid
from types import SimpleNamespace

import pytest

from app.pipeline.speech_cleanup_analysis import (
    SpeechCleanupMultiTakeResult,
    SpeechCleanupTakeResult,
    multi_take_public_receipt,
)
from app.pipeline.speech_cleanup_apply import (
    SpeechCleanupSnapshotError,
    hydrate_speech_cleanup_snapshot,
)
from app.services.active_narration_source import resolve_active_narration_source
from app.services.speech_cleanup_preflight import (
    SPEECH_CLEANUP_ENGINE_VERSION,
    ClaimedSpeechCleanupAnalysis,
    SpeechCleanupOperationalError,
    analysis_snapshot,
)
from app.services.speech_cleanup_selection import DETECTOR_VERSION
from app.tasks import speech_cleanup_analysis as task_module
from tests.services.test_active_narration_source import media, request
from tests.tasks.test_generative_build_silence_cut import DURATION
from tests.tasks.test_phone_subtitled_narrated_dispatch import _raw_preflight_snapshot

# --- resolver -------------------------------------------------------------------------


def _takes(*names: str, **kw):
    return tuple(media(name, duration_s=DURATION, **kw) for name in names)


def test_several_takes_resolve_to_one_source_per_take_in_clip_order() -> None:
    result = resolve_active_narration_source(
        request(edit_format="subtitled", multi_take=True, clips=_takes("a.mp4", "b.mp4", "c.mp4"))
    )

    source = result.source
    assert source is not None and result.reason is None
    assert [take.media_id for take in source.takes] == ["a.mp4", "b.mp4", "c.mp4"]
    # The row's scalar source is the first take; every take keeps its own window.
    assert source.storage_path == "users/u/a.mp4"
    assert all(take.window_end_s == DURATION for take in source.takes)
    assert source.to_private_dict()["takes"][2]["storage_path"] == "users/u/c.mp4"


def test_a_clip_without_sound_is_not_a_take() -> None:
    clips = (*_takes("a.mp4", "b.mp4"), media("muted.mp4", has_audio=False))
    source = resolve_active_narration_source(
        request(edit_format="subtitled", multi_take=True, clips=clips)
    ).source
    assert source is not None
    assert [take.media_id for take in source.takes] == ["a.mp4", "b.mp4"]


def test_the_takes_identity_changes_with_order_bytes_or_a_new_take() -> None:
    def fingerprint(clips):
        source = resolve_active_narration_source(
            request(edit_format="subtitled", multi_take=True, clips=clips)
        ).source
        return source.source_policy_fingerprint

    base = fingerprint(_takes("a.mp4", "b.mp4"))
    assert fingerprint(_takes("b.mp4", "a.mp4")) != base
    assert (
        fingerprint(
            (
                media("a.mp4", duration_s=DURATION),
                media("b.mp4", generation="2", duration_s=DURATION),
            )
        )
        != base
    )
    assert fingerprint(_takes("a.mp4", "b.mp4", "c.mp4")) != base
    assert fingerprint(_takes("a.mp4", "b.mp4")) == base
    # Never equal to the single-source identity of the same first take.
    single = resolve_active_narration_source(
        request(edit_format="subtitled", clips=_takes("a.mp4"))
    ).source
    assert single.source_policy_fingerprint != base


def test_one_unverified_take_makes_the_whole_source_unavailable() -> None:
    clips = (*_takes("a.mp4"), media("b.mp4", generation="", duration_s=DURATION))
    result = resolve_active_narration_source(
        request(edit_format="subtitled", multi_take=True, clips=clips)
    )
    assert result.source is None and result.reason == "unverified_generation"


# --- analysis worker ------------------------------------------------------------------


def _fixture_result():
    from app.pipeline.speech_cleanup_analysis import SpeechCleanupAnalysisResult

    return SpeechCleanupAnalysisResult.from_payload(
        {
            **_raw_preflight_snapshot(storage_path="unused")["analysis"],
            "source_fingerprint": "ab" * 32,
        }
    )


def _multi_work(names: list[str]):
    from app.services.active_narration_source import NarrationTake

    takes = tuple(
        (
            NarrationTake(
                media_id=name,
                storage_path=f"users/u/{name}",
                generation="1",
                manifest_identity=name,
                window_start_s=0.0,
                window_end_s=DURATION,
            ),
            None,
        )
        for name in names
    )
    return task_module._ClaimedWork(
        claim=ClaimedSpeechCleanupAnalysis(
            analysis_id=uuid.uuid4(), attempt_token="t", source_policy_fingerprint="ab" * 32
        ),
        source_storage_path=f"users/u/{names[0]}",
        source_generation="1",
        window_start_s=0.0,
        window_end_s=DURATION,
        engine_version=SPEECH_CLEANUP_ENGINE_VERSION,
        detector_version=DETECTOR_VERSION,
        source_kind="embedded_spine",
        multi_take=True,
        takes=takes,
    )


def test_every_take_is_analysed_and_only_talking_takes_count(monkeypatch) -> None:
    seen: list[str] = []

    def analyse(work, *, allow_silent=False):
        assert allow_silent and not work.multi_take
        seen.append(work.source_storage_path)
        return None if work.source_storage_path.endswith("street") else _fixture_result()

    monkeypatch.setattr(task_module, "_analyze_source", analyse)
    result = task_module._analyze_work(_multi_work(["take1", "street", "take2"]))

    assert sorted(seen) == ["users/u/street", "users/u/take1", "users/u/take2"]
    assert [(t.media_id, t.speaker, t.analysis is not None) for t in result.takes] == [
        ("take1", True, True),
        ("street", False, False),
        ("take2", True, True),
    ]
    one = _fixture_result().public_receipt
    assert result.public_receipt.candidate_count == 2 * one.candidate_count
    assert result.public_receipt.estimated_removed_ms == 2 * one.estimated_removed_ms
    assert result.public_receipt.source_duration_ms == 2 * one.source_duration_ms
    # The persisted payload hydrates back exactly.
    assert SpeechCleanupMultiTakeResult.from_payload(result.to_payload()) == result


def test_no_talking_take_fails_like_a_silent_single_source(monkeypatch) -> None:
    monkeypatch.setattr(task_module, "_analyze_source", lambda work, **_: None)
    with pytest.raises(SpeechCleanupOperationalError) as caught:
        task_module._analyze_work(_multi_work(["a", "b"]))
    assert caught.value.code == "no_speech_track"


def test_a_multi_take_row_whose_takes_no_longer_resolve_fails_closed() -> None:
    work = _multi_work(["a", "b"])
    from dataclasses import replace

    with pytest.raises(SpeechCleanupOperationalError) as caught:
        task_module._validate_work(replace(work, takes=()))
    assert caught.value.code == "snapshot_mismatch"


# --- payload + job snapshot -----------------------------------------------------------


def _payload(takes):
    rows = tuple(SpeechCleanupTakeResult.model_validate(row) for row in takes)
    return SpeechCleanupMultiTakeResult(
        source_fingerprint="ab" * 32,
        detector_version=DETECTOR_VERSION,
        takes=rows,
        public_receipt=multi_take_public_receipt(rows),
    ).to_payload()


def _take(name, *, speaker=True):
    return {
        "media_id": name,
        "storage_path": f"users/u/{name}",
        "generation": "1",
        "window_start_s": 0.0,
        "window_end_s": DURATION,
        "speaker": speaker,
        "analysis": _fixture_result().to_payload() if speaker else None,
    }


def _row(payload):
    return SimpleNamespace(
        id=uuid.uuid4(),
        status="ready",
        analysis_payload=payload,
        analysis_payload_version="2",
        engine_version=SPEECH_CLEANUP_ENGINE_VERSION,
        detector_version=DETECTOR_VERSION,
        source_kind="embedded_spine",
        source_media_identity="footage",
        source_storage_path="users/u/footage",
        source_generation="1",
        window_start_s=0.0,
        window_end_s=DURATION,
        source_policy_fingerprint="ab" * 32,
    )


def test_the_job_snapshot_hydrates_every_take_and_reads_as_its_first_talking_take() -> None:
    payload = _payload([_take("footage", speaker=False), _take("a"), _take("b")])
    hydrated = hydrate_speech_cleanup_snapshot(analysis_snapshot(_row(payload)))

    assert hydrated.multi_take
    # Single-source readers see one coherent source: the first TALKING take, not the footage.
    assert hydrated.storage_path == "users/u/a"
    assert hydrated.take("users/u/footage").snapshot is None
    take_b = hydrated.take("users/u/b").snapshot
    assert take_b.storage_path == "users/u/b"
    assert take_b.cut_plan.keep_segments == hydrated.cut_plan.keep_segments
    assert hydrated.take("users/u/other") is None


def test_a_tampered_card_count_never_reaches_the_render() -> None:
    payload = _payload([_take("a"), _take("b")])
    payload["public_receipt"]["candidate_count"] += 1
    with pytest.raises(SpeechCleanupSnapshotError) as caught:
        hydrate_speech_cleanup_snapshot(analysis_snapshot(_row(payload)))
    assert caught.value.detail == "analysis_schema"


def test_a_snapshot_whose_first_take_is_not_the_rows_source_is_refused() -> None:
    payload = _payload([_take("a"), _take("b")])
    row = _row(payload)
    row.source_storage_path = "users/u/b"  # the row says b came first; the payload says a
    with pytest.raises(SpeechCleanupSnapshotError):
        hydrate_speech_cleanup_snapshot(analysis_snapshot(row))


# --- joins between cut takes (F7) ------------------------------------------------------


def _tone_file(tmp_path, name: str, pattern: list[tuple[str, float]]) -> str:
    """A wav of tone/silence pieces, e.g. [("tone", 1.0), ("silence", 1.0)]."""
    import subprocess

    inputs, labels = [], []
    for index, (kind, seconds) in enumerate(pattern):
        source = (
            f"sine=frequency=440:duration={seconds}"
            if kind == "tone"
            else f"anullsrc=r=16000:cl=mono:d={seconds}"
        )
        inputs += ["-f", "lavfi", "-i", source]
        labels.append(f"[{index}:a]")
    out = tmp_path / f"{name}.wav"
    subprocess.run(
        [
            "ffmpeg",
            "-nostdin",
            "-loglevel",
            "error",
            *inputs,
            "-filter_complex",
            f"{''.join(labels)}concat=n={len(pattern)}:v=0:a=1,aresample=16000",
            "-ac",
            "1",
            str(out),
        ],
        check=True,
    )
    return str(out)


def test_the_pause_where_two_takes_meet_is_trimmed_on_both_sides(tmp_path) -> None:
    from app.pipeline.silence_cut import no_op_plan
    from app.tasks.generative_build import TAKE_JOIN_KEEP_S, _trim_take_joins

    first = _tone_file(tmp_path, "first", [("tone", 1.0), ("silence", 1.0)])
    second = _tone_file(tmp_path, "second", [("silence", 1.0), ("tone", 1.0)])
    plans, rows = _trim_take_joins([(no_op_plan(2.0), first, 2.0), (no_op_plan(2.0), second, 2.0)])

    # The first take keeps its sound and TAKE_JOIN_KEEP_S of its tail; the second its sound
    # and TAKE_JOIN_KEEP_S of its lead-in: the join pause is ~0.3 s instead of 2 s.
    (first_keep,) = plans[0].keep_segments
    (second_keep,) = plans[1].keep_segments
    assert first_keep[0] == 0.0
    assert first_keep[1] == pytest.approx(1.0 + TAKE_JOIN_KEEP_S, abs=0.05)
    assert second_keep[0] == pytest.approx(1.0 - TAKE_JOIN_KEEP_S, abs=0.05)
    assert second_keep[1] == 2.0
    assert [row["edge"] for row in rows] == ["end", "start"]
    # The outer edges (video start and end) are never trimmed here.
    assert plans[0].keep_segments[0][0] == 0.0 and plans[1].keep_segments[-1][1] == 2.0
    for plan in plans:
        assert plan.time_saved_s == pytest.approx(sum(r.end_s - r.start_s for r in plan.removed))


def test_a_take_without_a_measurable_track_is_left_as_it_is(tmp_path) -> None:
    from app.pipeline.silence_cut import no_op_plan
    from app.tasks.generative_build import _trim_take_joins

    plans, rows = _trim_take_joins([(no_op_plan(2.0), str(tmp_path / "missing.wav"), 2.0)] * 2)
    assert rows == [] and all(p.keep_segments == [(0.0, 2.0)] for p in plans)
