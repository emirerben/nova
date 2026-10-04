"""KRI-374 lane B: song analysis + take alignment tasks (stubbed I/O, real test DB)."""

from __future__ import annotations

import math
import shutil
import struct
import uuid
import wave
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from app.config import settings
from app.database import sync_session
from app.models import ContentPlan, Persona, PlanItem, User
from app.schemas.user_song import (
    SONG_ALIGNMENT_VERSION,
    SongAlignment,
    SongAnalysis,
    SongWord,
    TakeAlignment,
)
from app.tasks import user_song as task

SONG_PATH = "users/u/creation-threads/t/song-1.m4a"


def _seed(
    *,
    song: bool = True,
    generation: int = 77,
    analysis: dict | None = None,
    alignment: dict | None = None,
    clips: list[dict] | None = None,
) -> uuid.UUID:
    user_id, persona_id, plan_id, item_id = (uuid.uuid4() for _ in range(4))
    with sync_session() as db:
        db.add(User(id=user_id, email=f"{user_id}@test.local"))
        db.flush()
        db.add(Persona(id=persona_id, user_id=user_id, persona_status="ready", persona={}))
        db.flush()
        db.add(ContentPlan(id=plan_id, user_id=user_id, persona_id=persona_id))
        db.flush()
        db.add(
            PlanItem(
                id=item_id,
                content_plan_id=plan_id,
                position=1,
                idea="x",
                item_status="awaiting_clips",
                clip_assignments=clips or [],
                audio_mode="song" if song else "kria",
                **(
                    {
                        "song_gcs_path": SONG_PATH,
                        "song_generation": generation,
                        "song_duration_s": 4.0,
                        "song_filename": "track.m4a",
                        "song_analysis": analysis,
                        "song_alignment": alignment,
                    }
                    if song
                    else {}
                ),
            )
        )
        db.commit()
    return item_id


def _row(i: int, generation: str = "7") -> dict:
    return {
        "gcs_path": f"users/u/analysis-proxy-ios-{i}.mp4",
        "media_id": f"m{i}",
        "kind": "video",
        "storage_generation": generation,
    }


def _reload(item_id: uuid.UUID) -> PlanItem:
    with sync_session() as db:
        item = db.get(PlanItem, item_id)
        db.expunge(item)
        return item


def _click_wav(path: Path, seconds: float = 4.0, rate: int = 16000) -> None:
    """A tiny synthetic 'song': a 1 kHz burst every half second over low noise."""
    frames = bytearray()
    for n in range(int(seconds * rate)):
        t = n / rate
        in_click = (t % 0.5) < 0.05
        value = 0.8 * math.sin(2 * math.pi * 1000 * t) if in_click else 0.01
        frames += struct.pack("<h", int(value * 32767))
    with wave.open(str(path), "wb") as out:
        out.setnchannels(1)
        out.setsampwidth(2)
        out.setframerate(rate)
        out.writeframes(bytes(frames))


@pytest.fixture(autouse=True)
def _sent(monkeypatch: pytest.MonkeyPatch) -> MagicMock:
    """Capture enqueues instead of publishing to a broker."""
    sent = MagicMock()
    monkeypatch.setattr(task.align_user_song_takes_task, "apply_async", sent)
    monkeypatch.setattr(task.analyze_user_song_task, "apply_async", sent)
    return sent


# ── line grouping ────────────────────────────────────────────────────────────


def _w(text: str, start: float, end: float) -> SongWord:
    return SongWord(text=text, start_s=start, end_s=end)


def test_a_long_gap_ends_a_line() -> None:
    lines = task.group_words_into_lines(
        [_w("hello", 0.0, 0.4), _w("world", 0.5, 0.9), _w("again", 2.0, 2.4)]
    )
    assert [(line.text, line.start_s, line.end_s) for line in lines] == [
        ("hello world", 0.0, 0.9),
        ("again", 2.0, 2.4),
    ]


def test_a_gap_of_exactly_the_threshold_keeps_the_line() -> None:
    lines = task.group_words_into_lines([_w("a", 0.0, 0.2), _w("b", 1.0, 1.2)])
    assert len(lines) == 1


def test_nine_quick_words_split_into_eight_plus_one() -> None:
    words = [_w(f"w{i}", i * 0.3, i * 0.3 + 0.2) for i in range(9)]
    lines = task.group_words_into_lines(words)
    assert [len(line.text.split()) for line in lines] == [8, 1]


def test_no_words_no_lines() -> None:
    assert task.group_words_into_lines([]) == []


# ── analyze_user_song_task ───────────────────────────────────────────────────


@pytest.mark.skipif(shutil.which("ffmpeg") is None, reason="ffmpeg required for beat detection")
def test_analysis_writes_a_ready_song_analysis(monkeypatch, tmp_path, _sent) -> None:
    wav = tmp_path / "song.wav"
    _click_wav(wav)
    item_id = _seed(analysis={"version": 1, "generation": 77, "status": "pending"})
    monkeypatch.setattr(task, "_download_song", lambda _path, _gen, local: shutil.copy(wav, local))
    monkeypatch.setattr(
        task,
        "_transcribe_words",
        lambda _local: [
            {"text": "la", "start_s": 0.1, "end_s": 0.3},
            {"text": "la", "start_s": 0.4, "end_s": 0.6},
            {"text": "land", "start_s": 3.0, "end_s": 3.5},
        ],
    )

    result = task.analyze_user_song_task.run(str(item_id), 77)

    assert result["status"] == "ready"
    stored = SongAnalysis.model_validate(_reload(item_id).song_analysis)
    assert stored.status == "ready"
    assert stored.generation == 77
    assert stored.duration_s == 4.0
    assert [word.text for word in stored.words] == ["la", "la", "land"]
    assert [line.text for line in stored.lines] == ["la la", "land"]
    assert stored.beats_s == sorted(stored.beats_s)
    assert len(stored.beats_s) >= 1
    assert stored.error is None
    # Alignment is owed once the analysis lands.
    assert _sent.call_args.kwargs["args"] == [str(item_id)]


def test_no_lyrics_is_still_ready(monkeypatch, _sent) -> None:
    item_id = _seed()
    monkeypatch.setattr(task, "_download_song", lambda *_a: None)
    monkeypatch.setattr(task, "detect_music_beats", lambda _local: [0.5, 1.0])

    def boom(_local: str) -> list:
        raise RuntimeError("whisper down")

    monkeypatch.setattr(task, "_transcribe_words", boom)

    result = task.analyze_user_song_task.run(str(item_id), 77)

    stored = SongAnalysis.model_validate(_reload(item_id).song_analysis)
    assert result["status"] == "ready"
    assert (stored.status, stored.words, stored.lines) == ("ready", [], [])
    assert stored.beats_s == [0.5, 1.0]


def test_a_download_failure_marks_the_analysis_failed_without_raising(monkeypatch) -> None:
    item_id = _seed()

    def boom(*_a) -> None:
        raise FileNotFoundError("gone")

    monkeypatch.setattr(task, "_download_song", boom)

    result = task.analyze_user_song_task.run(str(item_id), 77)

    stored = SongAnalysis.model_validate(_reload(item_id).song_analysis)
    assert result["status"] == "failed"
    assert stored.status == "failed" and stored.error


def test_a_stale_generation_writes_nothing(monkeypatch) -> None:
    item_id = _seed(generation=77)
    monkeypatch.setattr(task, "_download_song", lambda *_a: pytest.fail("must not download"))

    assert task.analyze_user_song_task.run(str(item_id), 12)["status"] == "stale"
    assert _reload(item_id).song_analysis is None


def test_a_song_replaced_mid_analysis_is_not_overwritten(monkeypatch) -> None:
    item_id = _seed(generation=77)

    def replace_then_analyse(song: dict, generation: int) -> SongAnalysis:
        with sync_session() as db:
            row = db.get(PlanItem, item_id)
            row.song_generation = 78
            db.commit()
        return SongAnalysis(generation=generation, status="ready")

    monkeypatch.setattr(task, "_analyze", replace_then_analyse)

    assert task.analyze_user_song_task.run(str(item_id), 77)["status"] == "stale"
    assert _reload(item_id).song_analysis is None


def test_a_ready_analysis_is_not_recomputed(monkeypatch, _sent) -> None:
    ready = SongAnalysis(generation=77, status="ready", duration_s=4.0).model_dump(mode="json")
    item_id = _seed(analysis=ready)
    monkeypatch.setattr(task, "_analyze", lambda *_a: pytest.fail("must not re-analyse"))

    assert task.analyze_user_song_task.run(str(item_id), 77)["status"] == "cached"
    assert _reload(item_id).song_analysis == ready


def test_unknown_item_never_raises() -> None:
    assert task.analyze_user_song_task.run(str(uuid.uuid4()), 1)["status"] == "stale"
    assert task.analyze_user_song_task.run("not-a-uuid", 1)["status"] == "invalid"


# ── align_user_song_takes_task ───────────────────────────────────────────────


def _ready_analysis(generation: int = 77) -> dict:
    return SongAnalysis(generation=generation, status="ready", duration_s=4.0).model_dump(
        mode="json"
    )


class _Aligner:
    """Injected stand-in for Lane A's pure aligner."""

    def __init__(self, status: str = "confident", delta_s: float = 12.5) -> None:
        self.calls: list[list[str]] = []
        self.status = status
        self.delta_s = delta_s

    def __call__(self, song_pcm, song, takes):  # noqa: ANN001, ANN204
        self.calls.append([take["media_id"] for take in takes])
        return {
            take["media_id"]: TakeAlignment(
                media_id=take["media_id"],
                status=self.status,
                delta_s=self.delta_s,
                confidence=0.9,
            )
            for take in takes
        }


@pytest.fixture
def aligner(monkeypatch) -> _Aligner:
    stub = _Aligner()
    monkeypatch.setattr(task, "_run_aligner", stub)
    monkeypatch.setattr(task, "_download_song", lambda *_a: None)
    monkeypatch.setattr(task, "_download_take", lambda *_a: None)
    monkeypatch.setattr(task, "_decode_pcm", lambda _local: [0.1, 0.2, 0.3])
    monkeypatch.setattr(task, "_transcribe_words", lambda _local: [])
    return stub


def _alignment(item_id: uuid.UUID) -> SongAlignment:
    return SongAlignment.model_validate(_reload(item_id).song_alignment)


def test_alignment_waits_for_the_analysis(aligner) -> None:
    pending = {"version": 1, "generation": 77, "status": "pending"}
    item_id = _seed(analysis=pending, clips=[_row(1)])

    assert task.align_user_song_takes_task.run(str(item_id))["status"] == "waiting_analysis"
    assert aligner.calls == []
    assert _reload(item_id).song_alignment is None


def test_alignment_persists_one_row_per_take(aligner) -> None:
    item_id = _seed(analysis=_ready_analysis(), clips=[_row(1), _row(2, "9")])

    result = task.align_user_song_takes_task.run(str(item_id))

    assert (result["status"], result["aligned"]) == ("ok", 2)
    stored = _alignment(item_id)
    assert stored.version == SONG_ALIGNMENT_VERSION
    assert stored.song_generation == 77
    assert set(stored.takes) == {"m1", "m2"}
    assert stored.takes["m1"].proxy_generation == 7
    assert stored.takes["m2"].proxy_generation == 9
    assert stored.takes["m1"].delta_s == 12.5


def test_alignment_is_idempotent_per_generation_triple(aligner) -> None:
    item_id = _seed(analysis=_ready_analysis(), clips=[_row(1)])
    assert task.align_user_song_takes_task.run(str(item_id))["status"] == "ok"
    first = _reload(item_id).song_alignment

    again = task.align_user_song_takes_task.run(str(item_id))

    assert again["status"] == "unchanged"
    assert aligner.calls == [["m1"]]
    assert _reload(item_id).song_alignment == first


def test_only_new_or_reuploaded_takes_are_recomputed(aligner) -> None:
    item_id = _seed(analysis=_ready_analysis(), clips=[_row(1), _row(2)])
    task.align_user_song_takes_task.run(str(item_id))
    assert aligner.calls == [["m1", "m2"]]

    with sync_session() as db:
        row = db.get(PlanItem, item_id)
        # m2's proxy is re-uploaded (new generation); m3 is a new take.
        row.clip_assignments = [_row(1), _row(2, "11"), _row(3)]
        db.commit()

    result = task.align_user_song_takes_task.run(str(item_id))

    assert result["aligned"] == 2
    assert aligner.calls[-1] == ["m2", "m3"]
    stored = _alignment(item_id)
    assert stored.takes["m2"].proxy_generation == 11
    assert set(stored.takes) == {"m1", "m2", "m3"}


def test_a_removed_take_is_pruned(aligner) -> None:
    item_id = _seed(analysis=_ready_analysis(), clips=[_row(1), _row(2)])
    task.align_user_song_takes_task.run(str(item_id))
    with sync_session() as db:
        row = db.get(PlanItem, item_id)
        row.clip_assignments = [_row(1)]
        db.commit()

    assert task.align_user_song_takes_task.run(str(item_id))["status"] == "ok"

    assert set(_alignment(item_id).takes) == {"m1"}
    assert aligner.calls == [["m1", "m2"]]


def test_a_new_song_generation_invalidates_the_alignment(aligner) -> None:
    stale = SongAlignment(
        song_generation=70, takes={"m1": TakeAlignment(media_id="m1", status="unmatched")}
    ).model_dump(mode="json")
    item_id = _seed(analysis=_ready_analysis(), alignment=stale, clips=[_row(1)])

    assert task.align_user_song_takes_task.run(str(item_id))["status"] == "ok"

    assert aligner.calls == [["m1"]]
    assert _alignment(item_id).song_generation == 77


def test_the_measured_proxy_offset_is_added_to_every_delta(monkeypatch, aligner) -> None:
    monkeypatch.setattr(settings, "song_alignment_proxy_offset_s", 0.025)
    item_id = _seed(analysis=_ready_analysis(), clips=[_row(1)])

    task.align_user_song_takes_task.run(str(item_id))

    assert _alignment(item_id).takes["m1"].delta_s == pytest.approx(12.525)


def test_a_take_without_audio_is_unmatched_and_never_raises(monkeypatch, aligner) -> None:
    monkeypatch.setattr(task, "_decode_pcm", lambda _local: [])
    item_id = _seed(analysis=_ready_analysis(), clips=[_row(1)])

    result = task.align_user_song_takes_task.run(str(item_id))

    assert result["status"] == "ok"
    take = _alignment(item_id).takes["m1"]
    assert (take.status, take.delta_s, take.proxy_generation) == ("unmatched", None, 7)
    assert aligner.calls == []


def test_an_aligner_crash_leaves_the_takes_unmatched(monkeypatch, aligner) -> None:
    def boom(*_a):  # noqa: ANN002, ANN202
        raise RuntimeError("numpy exploded")

    monkeypatch.setattr(task, "_run_aligner", boom)
    item_id = _seed(analysis=_ready_analysis(), clips=[_row(1)])

    assert task.align_user_song_takes_task.run(str(item_id))["status"] == "ok"
    assert _alignment(item_id).takes["m1"].status == "unmatched"


def test_a_missing_aligner_module_never_raises(monkeypatch) -> None:
    """Lane A's module may not be importable: takes stay unmatched, the task survives."""
    import sys

    monkeypatch.setitem(sys.modules, "app.pipeline.song_alignment", None)
    monkeypatch.setattr(task, "_download_song", lambda *_a: None)
    monkeypatch.setattr(task, "_download_take", lambda *_a: None)
    monkeypatch.setattr(task, "_decode_pcm", lambda _local: [0.1])
    monkeypatch.setattr(task, "_transcribe_words", lambda _local: [])
    item_id = _seed(analysis=_ready_analysis(), clips=[_row(1)])

    assert task.align_user_song_takes_task.run(str(item_id))["status"] == "ok"
    assert _alignment(item_id).takes["m1"].status == "unmatched"


def test_no_song_means_nothing_to_align(aligner) -> None:
    item_id = _seed(song=False, clips=[_row(1)])
    assert task.align_user_song_takes_task.run(str(item_id))["status"] == "no_song"


def test_a_song_replaced_mid_alignment_is_not_overwritten(monkeypatch, aligner) -> None:
    item_id = _seed(analysis=_ready_analysis(), clips=[_row(1)])
    real = task._compute_alignments

    def replace_then_compute(song, analysis, todo):  # noqa: ANN001, ANN202
        with sync_session() as db:
            row = db.get(PlanItem, item_id)
            row.song_generation = 78
            db.commit()
        return real(song, analysis, todo)

    monkeypatch.setattr(task, "_compute_alignments", replace_then_compute)

    assert task.align_user_song_takes_task.run(str(item_id))["status"] == "stale"
    assert _reload(item_id).song_alignment is None


# ── enqueue helpers ──────────────────────────────────────────────────────────


def test_enqueue_helpers_never_raise(monkeypatch) -> None:
    def boom(*_a, **_k):  # noqa: ANN002, ANN003, ANN202
        raise ConnectionError("broker down")

    monkeypatch.setattr(task.analyze_user_song_task, "apply_async", boom)
    monkeypatch.setattr(task.align_user_song_takes_task, "apply_async", boom)

    task.enqueue_user_song_analysis(uuid.uuid4(), 5)
    task.enqueue_user_song_alignment(uuid.uuid4())


def test_tasks_are_registered_on_the_worker() -> None:
    from app.worker import celery_app

    celery_app.loader.import_default_modules()
    assert "tasks.analyze_user_song" in celery_app.tasks
    assert "tasks.align_user_song_takes" in celery_app.tasks


# ── inline fallback used by the montage worker (KRI-374 D2) ──────────────────


def test_ensure_alignment_computes_inline_when_the_tasks_have_not_run(aligner, monkeypatch) -> None:
    item_id = _seed(analysis=None, clips=[_row(1), _row(2)])
    monkeypatch.setattr(task, "detect_music_beats", lambda _local: [0.5, 1.0])
    analysis, alignment = task.ensure_song_alignment(item_id)

    assert analysis is not None and analysis.status == "ready" and analysis.beats_s == [0.5, 1.0]
    assert alignment is not None and alignment.song_generation == 77
    assert set(alignment.takes) == {"m1", "m2"}
    # Persisted exactly as the background tasks would have left it.
    assert set(_alignment(item_id).takes) == {"m1", "m2"}


def test_ensure_alignment_reuses_a_current_row_without_recomputing(aligner) -> None:
    item_id = _seed(analysis=_ready_analysis(), clips=[_row(1)])
    task.align_user_song_takes_task.run(str(item_id))
    assert aligner.calls == [["m1"]]

    _analysis, alignment = task.ensure_song_alignment(item_id)

    assert alignment is not None and set(alignment.takes) == {"m1"}
    assert aligner.calls == [["m1"]]


def test_ensure_alignment_recomputes_only_the_replaced_take(aligner) -> None:
    item_id = _seed(analysis=_ready_analysis(), clips=[_row(1), _row(2)])
    task.align_user_song_takes_task.run(str(item_id))
    with sync_session() as db:
        item = db.get(PlanItem, item_id)
        item.clip_assignments = [_row(1), _row(2, "9")]  # m2's proxy was re-uploaded
        db.commit()

    _analysis, alignment = task.ensure_song_alignment(item_id)

    assert aligner.calls == [["m1", "m2"], ["m2"]]
    assert alignment is not None and alignment.takes["m2"].proxy_generation == 9


def test_ensure_alignment_without_a_song_returns_nothing(aligner) -> None:
    assert task.ensure_song_alignment(_seed(song=False)) == (None, None)
    assert task.ensure_song_analysis(_seed(song=False)) is None


def test_ensure_analysis_returns_the_recorded_failure_instead_of_raising(monkeypatch) -> None:
    item_id = _seed()

    def boom(*_a):
        raise RuntimeError("storage down")

    monkeypatch.setattr(task, "_download_song", boom)
    analysis = task.ensure_song_analysis(item_id)
    assert analysis is not None and analysis.status == "failed"
