"""Timeline-editor dispatch tests (post-generation clip timeline).

House style (mirrors test_generative_jobs.py): NO TestClient, NO DB — the dispatch
helpers are called directly with fake SimpleNamespace jobs; `persist_user_timeline`,
`storage.object_exists`, `signed_get_url`, and the Celery task are monkeypatched.

The cumulative beat-grid walk is the load-bearing math here: slot i's seconds are
grid[offset+beats] - grid[offset] with the offset advancing, so the SAME
`duration_beats` yields different seconds at different positions on a non-uniform
grid. Tests pin that walk, every rejection code, the persist-before-enqueue order,
and the re-sign-on-failed-variant fix in `_variants_for_response`.
"""

from __future__ import annotations

import types
import uuid

import pytest
from fastapi import HTTPException
from pydantic import ValidationError

import app.routes.generative_jobs as gj

# Deliberately NON-uniform: spacings 0.5, 0.6, 0.5, 0.8, 0.6, 0.8.
GRID = [0.0, 0.5, 1.1, 1.6, 2.4, 3.0, 3.8]


# ── Fixtures ─────────────────────────────────────────────────────────────────────


def _ai_slots(prefix: str, *, src_dur: float = 10.0, beats: bool = True) -> list[dict]:
    return [
        {
            "slot_id": "s1",
            "clip_index": 0,
            "source_gcs_path": f"{prefix}clip_0.mp4",
            "source_duration_s": src_dur,
            "in_s": 0.0,
            "duration_s": 1.1,
            "duration_beats": 2 if beats else None,
            "order": 0,
            "moment_energy": 0.8,
            "moment_description": "crowd wave",
        },
        {
            "slot_id": "s2",
            "clip_index": 1,
            "source_gcs_path": f"{prefix}clip_1.mp4",
            "source_duration_s": src_dur,
            "in_s": 1.0,
            "duration_s": 1.9,
            "duration_beats": 3 if beats else None,
            "order": 1,
            "moment_energy": None,
            "moment_description": None,
        },
    ]


def _timeline_job(
    *,
    variant_id="song_text",
    text_mode="agent_text",
    render_status="ready",
    beat_grid=None,
    user_slots=None,
    archetype=None,
    durable=True,
    with_ai=True,
    src_dur=10.0,
    sibling_rendering=False,
    n_clips=3,
):
    """Fake Job whose target variant carries an ai_timeline (lane-2 shape)."""
    jid = uuid.uuid4()
    grid = GRID if beat_grid is None else beat_grid
    prefix = f"generative-jobs/{jid}/sources/" if durable else "slot-uploads/legacy/"
    variant: dict = {
        "variant_id": variant_id,
        "render_status": render_status,
        "text_mode": text_mode,
    }
    if with_ai:
        variant["ai_timeline"] = {
            "beat_grid": list(grid),
            "slots": _ai_slots(prefix, src_dur=src_dur, beats=bool(grid)),
        }
    if archetype is not None:
        variant["resolved_archetype"] = archetype
    if user_slots is not None:
        variant["user_timeline"] = {"slots": user_slots}
    variants = [variant]
    if sibling_rendering:
        variants.append({"variant_id": "original_text", "render_status": "rendering"})
    return types.SimpleNamespace(
        id=jid,
        assembly_plan={"variants": variants},
        all_candidates={"clip_paths": [f"slot-uploads/u/clip_{i}.mp4" for i in range(n_clips)]},
    )


def _set_flag(monkeypatch, enabled: bool) -> None:
    from app.config import settings

    # Patch the REAL (case-sensitive, UPPERCASE) pydantic field — the routes read
    # `settings.GENERATIVE_TIMELINE_EDITOR_ENABLED` directly. A lowercase patch
    # here would pass trivially against dead getattr-with-default code.
    monkeypatch.setattr(settings, "GENERATIVE_TIMELINE_EDITOR_ENABLED", enabled)


def _arm(monkeypatch, *, enabled=True, object_exists=True):
    """Patch flag + persist + task + storage; return (seq, persists, delays)."""
    import app.tasks.generative_build as gb

    _set_flag(monkeypatch, enabled)
    seq: list[str] = []
    persists: list[tuple] = []
    delays: list[tuple] = []

    async def fake_persist(db, job_id, variant_id, slots):
        seq.append("persist")
        persists.append((job_id, variant_id, slots))

    def fake_delay(*a, **k):
        seq.append("enqueue")
        delays.append((a, k))

    monkeypatch.setattr(gj, "persist_user_timeline", fake_persist)
    monkeypatch.setattr(
        gb, "regenerate_generative_variant", types.SimpleNamespace(delay=fake_delay), raising=False
    )
    monkeypatch.setattr(gj.storage, "object_exists", lambda p: object_exists)
    return seq, persists, delays


def _req(slots: list[dict]) -> gj.TimelineEditRequest:
    return gj.TimelineEditRequest(slots=slots)


# ── Request schema ───────────────────────────────────────────────────────────────


def test_edit_request_rejects_over_50_slots():
    with pytest.raises(ValidationError):
        _req([{"clip_index": 0, "in_s": 0.0, "duration_s": 1.0}] * 51)


def test_slot_edit_defaults():
    s = gj.TimelineSlotEdit(clip_index=1, in_s=0.5)
    assert s.slot_id is None
    assert s.duration_beats is None
    assert s.duration_s is None
    assert s.removed is False


# ── GET: eligibility matrix ──────────────────────────────────────────────────────


def test_get_unknown_variant_404(monkeypatch):
    _set_flag(monkeypatch, True)
    with pytest.raises(HTTPException) as exc:
        gj.dispatch_get_timeline(_timeline_job(), "nope")
    assert exc.value.status_code == 404


def test_get_disabled_reason(monkeypatch):
    _set_flag(monkeypatch, False)
    out = gj.dispatch_get_timeline(_timeline_job(), "song_text")
    assert out["editable"] is False
    assert out["reason"] == "disabled"


def test_get_lyrics_variant_reason(monkeypatch):
    _set_flag(monkeypatch, True)
    job = _timeline_job(variant_id="song_lyrics", text_mode="lyrics")
    out = gj.dispatch_get_timeline(job, "song_lyrics")
    assert (out["editable"], out["reason"]) == (False, "lyrics_sync")


def test_get_lyrics_text_mode_reason(monkeypatch):
    # Even on an otherwise-editable variant id, lyrics text_mode means beat-synced
    # lines — re-cutting breaks sync.
    _set_flag(monkeypatch, True)
    job = _timeline_job(text_mode="lyrics")
    assert gj.dispatch_get_timeline(job, "song_text")["reason"] == "lyrics_sync"


def test_get_voiceover_reason(monkeypatch):
    _set_flag(monkeypatch, True)
    job = _timeline_job(variant_id="voiceover_music")
    out = gj.dispatch_get_timeline(job, "voiceover_music")
    assert (out["editable"], out["reason"]) == (False, "voiceover_bed_fit")


def test_get_talking_head_reason(monkeypatch):
    _set_flag(monkeypatch, True)
    job = _timeline_job(archetype="talking_head")
    assert gj.dispatch_get_timeline(job, "song_text")["reason"] == "no_slot_timeline"


def test_get_no_timeline_reason(monkeypatch):
    # Legacy variant rendered before lane-2 instrumentation: no ai_timeline at all.
    _set_flag(monkeypatch, True)
    job = _timeline_job(with_ai=False)
    out = gj.dispatch_get_timeline(job, "song_text")
    assert (out["editable"], out["reason"]) == (False, "no_timeline")
    assert out["slots"] == []
    assert out["total_duration_s"] == 0.0


def test_get_sources_expired_reason(monkeypatch):
    # Non-durable source paths = legacy job cutting from 24h-swept uploads.
    _set_flag(monkeypatch, True)
    job = _timeline_job(durable=False)
    assert gj.dispatch_get_timeline(job, "song_text")["reason"] == "sources_expired"


# ── GET: payload shape ───────────────────────────────────────────────────────────


def test_get_editable_happy_path(monkeypatch):
    _set_flag(monkeypatch, True)
    monkeypatch.setattr(gj, "signed_get_url", lambda p, t: f"https://fresh.example/{p}")
    job = _timeline_job()
    out = gj.dispatch_get_timeline(job, "song_text")

    assert out["editable"] is True
    assert out["reason"] is None
    assert out["beat_grid"] == GRID
    assert out["has_user_edits"] is False
    assert [s["slot_id"] for s in out["slots"]] == ["s1", "s2"]
    assert out["total_duration_s"] == pytest.approx(3.0)
    # Full clip pool: every uploaded clip, signed, with used flags + known durations.
    assert [c["clip_index"] for c in out["clips"]] == [0, 1, 2]
    assert [c["used"] for c in out["clips"]] == [True, True, False]
    assert out["clips"][0]["signed_url"] == "https://fresh.example/slot-uploads/u/clip_0.mp4"
    assert out["clips"][0]["duration_s"] == 10.0
    assert out["clips"][2]["duration_s"] is None  # AI never probed clip 2


def test_get_effective_timeline_prefers_user_edits(monkeypatch):
    _set_flag(monkeypatch, True)
    monkeypatch.setattr(gj, "signed_get_url", lambda p, t: "https://x")
    user_slots = [
        {"slot_id": "s1", "clip_index": 0, "in_s": 0.0, "duration_s": 2.4, "removed": False},
        {"slot_id": "s2", "clip_index": 1, "in_s": 1.0, "duration_s": 1.9, "removed": True},
    ]
    job = _timeline_job(user_slots=user_slots)
    out = gj.dispatch_get_timeline(job, "song_text")

    assert out["has_user_edits"] is True
    # Effective timeline = user_timeline verbatim (removed slots included, flagged).
    assert [s["slot_id"] for s in out["slots"]] == ["s1", "s2"]
    assert out["slots"][1]["removed"] is True
    # Removed slot contributes neither duration nor a `used` flag.
    assert out["total_duration_s"] == pytest.approx(2.4)
    assert [c["used"] for c in out["clips"]] == [True, False, False]


def test_get_does_not_raise_on_sign_failure(monkeypatch):
    _set_flag(monkeypatch, True)

    def boom(p, t):
        raise RuntimeError("no credentials")

    monkeypatch.setattr(gj, "signed_get_url", boom)
    out = gj.dispatch_get_timeline(_timeline_job(), "song_text")
    assert all(c["signed_url"] is None for c in out["clips"])


# ── POST: gates ──────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_edit_disabled_403(monkeypatch):
    seq, _, _ = _arm(monkeypatch, enabled=False)
    with pytest.raises(HTTPException) as exc:
        await gj.dispatch_edit_timeline(
            _timeline_job(),
            "song_text",
            _req([{"slot_id": "s1", "clip_index": 0, "in_s": 0.0, "duration_beats": 2}]),
            db=None,
        )
    assert exc.value.status_code == 403
    assert exc.value.detail == {"code": "disabled"}
    assert seq == []


@pytest.mark.asyncio
async def test_edit_409_while_variant_rendering(monkeypatch):
    seq, _, _ = _arm(monkeypatch)
    with pytest.raises(HTTPException) as exc:
        await gj.dispatch_edit_timeline(
            _timeline_job(render_status="rendering"),
            "song_text",
            _req([{"slot_id": "s1", "clip_index": 0, "in_s": 0.0, "duration_beats": 2}]),
            db=None,
        )
    assert exc.value.status_code == 409
    assert seq == []


@pytest.mark.asyncio
async def test_edit_job_busy_when_sibling_rendering(monkeypatch):
    seq, _, _ = _arm(monkeypatch)
    with pytest.raises(HTTPException) as exc:
        await gj.dispatch_edit_timeline(
            _timeline_job(sibling_rendering=True),
            "song_text",
            _req([{"slot_id": "s1", "clip_index": 0, "in_s": 0.0, "duration_beats": 2}]),
            db=None,
        )
    assert exc.value.status_code == 409
    assert exc.value.detail == {"code": "JOB_BUSY"}
    assert seq == []


@pytest.mark.asyncio
async def test_edit_ineligible_variant_422(monkeypatch):
    _arm(monkeypatch)
    job = _timeline_job(variant_id="song_lyrics", text_mode="lyrics")
    with pytest.raises(HTTPException) as exc:
        await gj.dispatch_edit_timeline(
            job,
            "song_lyrics",
            _req([{"slot_id": "s1", "clip_index": 0, "in_s": 0.0, "duration_beats": 2}]),
            db=None,
        )
    assert exc.value.status_code == 422
    assert exc.value.detail == {"code": "lyrics_sync"}


@pytest.mark.asyncio
async def test_edit_stale_slot_id_409(monkeypatch):
    seq, _, _ = _arm(monkeypatch)
    with pytest.raises(HTTPException) as exc:
        await gj.dispatch_edit_timeline(
            _timeline_job(),
            "song_text",
            _req([{"slot_id": "ghost-old-tab", "clip_index": 0, "in_s": 0, "duration_beats": 2}]),
            db=None,
        )
    assert exc.value.status_code == 409
    assert exc.value.detail == {"code": "TIMELINE_STALE"}
    assert seq == []


# ── POST: validation codes ───────────────────────────────────────────────────────


async def _expect_422(monkeypatch, job, slots, code):
    seq, _, _ = _arm(monkeypatch)
    with pytest.raises(HTTPException) as exc:
        await gj.dispatch_edit_timeline(job, "song_text", _req(slots), db=None)
    assert exc.value.status_code == 422
    assert exc.value.detail == {"code": code}
    assert seq == []  # nothing persisted, nothing enqueued


@pytest.mark.asyncio
async def test_edit_empty_timeline(monkeypatch):
    await _expect_422(
        monkeypatch,
        _timeline_job(),
        [{"slot_id": "s1", "clip_index": 0, "in_s": 0.0, "duration_beats": 2, "removed": True}],
        "TIMELINE_EMPTY",
    )


@pytest.mark.asyncio
async def test_edit_unknown_clip_index(monkeypatch):
    await _expect_422(
        monkeypatch,
        _timeline_job(n_clips=3),
        [{"slot_id": "s1", "clip_index": 3, "in_s": 0.0, "duration_beats": 2}],
        "TIMELINE_UNKNOWN_CLIP",
    )


@pytest.mark.asyncio
async def test_edit_grid_slot_without_beats_or_seconds_invalid(monkeypatch):
    # INVALID_DURATION only when BOTH duration_beats and duration_s are unusable.
    await _expect_422(
        monkeypatch,
        _timeline_job(),
        [{"slot_id": "s1", "clip_index": 0, "in_s": 0.0}],  # neither field
        "TIMELINE_INVALID_DURATION",
    )


@pytest.mark.asyncio
async def test_edit_grid_slot_with_nonpositive_values_invalid(monkeypatch):
    await _expect_422(
        monkeypatch,
        _timeline_job(),
        [{"slot_id": "s1", "clip_index": 0, "in_s": 0.0, "duration_beats": 0, "duration_s": 0.0}],
        "TIMELINE_INVALID_DURATION",
    )


@pytest.mark.asyncio
async def test_edit_grid_null_beats_slot_uses_duration_s_and_skips_grid(monkeypatch):
    # B2: a footage-trimmed slot (duration_beats null) on a no-music GRID
    # variant snaps its seconds window but must NOT walk or advance the grid —
    # the next beats slot still starts at offset 0.
    seq, _, delays = _arm(monkeypatch)
    await gj.dispatch_edit_timeline(
        _timeline_job(),
        "song_text",
        _req(
            [
                {"slot_id": "s1", "clip_index": 0, "in_s": 0.0, "duration_s": 1.137},
                {"slot_id": "s2", "clip_index": 1, "in_s": 1.0, "duration_beats": 2},
            ]
        ),
        db=None,
    )
    override = delays[0][1]["timeline_override"]
    assert override[0]["duration_s"] == pytest.approx(1.0)
    assert override[0]["duration_beats"] is None
    # s2 walked from offset 0 (null-beats slot consumed no beats): grid[2]-grid[0].
    assert override[1]["duration_s"] == pytest.approx(1.1)


@pytest.mark.asyncio
async def test_edit_beats_exhausted_on_non_uniform_grid(monkeypatch):
    # Cumulative walk: slot1 consumes beats 0→3 (1.6s), slot2 then needs grid[3+4=7]
    # — past the last index (6) of the 7-point grid → exhausted. A naive
    # "beats × mean spacing" check would NOT reject this.
    await _expect_422(
        monkeypatch,
        _timeline_job(),
        [
            {"slot_id": "s1", "clip_index": 0, "in_s": 0.0, "duration_beats": 3},
            {"slot_id": "s2", "clip_index": 1, "in_s": 1.0, "duration_beats": 4},
        ],
        "TIMELINE_BEATS_EXHAUSTED",
    )


@pytest.mark.asyncio
async def test_edit_cumulative_walk_durations(monkeypatch):
    # THE beat-walk pin: same grid, offsets advance, durations come out non-uniform.
    # slot1: grid[2]-grid[0] = 1.1; slot2: grid[6]-grid[2] = 3.8-1.1 = 2.7.
    seq, persists, delays = _arm(monkeypatch)
    await gj.dispatch_edit_timeline(
        _timeline_job(),
        "song_text",
        _req(
            [
                {"slot_id": "s1", "clip_index": 0, "in_s": 0.0, "duration_beats": 2},
                {"slot_id": "s2", "clip_index": 1, "in_s": 1.0, "duration_beats": 4},
            ]
        ),
        db=None,
    )
    assert len(delays) == 1
    override = delays[0][1]["timeline_override"]
    assert [s["duration_s"] for s in override] == [pytest.approx(1.1), pytest.approx(2.7)]
    assert [s["duration_beats"] for s in override] == [2, 4]
    assert [s["order"] for s in override] == [0, 1]


@pytest.mark.asyncio
async def test_edit_no_grid_requires_duration_s(monkeypatch):
    await _expect_422(
        monkeypatch,
        _timeline_job(beat_grid=[]),
        [{"slot_id": "s1", "clip_index": 0, "in_s": 0.0, "duration_beats": 2}],  # no seconds
        "TIMELINE_INVALID_DURATION",
    )


@pytest.mark.asyncio
async def test_edit_no_grid_snaps_off_step_duration(monkeypatch):
    # Hotfix 2026-07-05: no-music editor input may be off-step, but the server
    # persists the snapped 0.5s value so GET timeline mirrors what will bake.
    seq, _, delays = _arm(monkeypatch)
    await gj.dispatch_edit_timeline(
        _timeline_job(beat_grid=[]),
        "song_text",
        _req([{"slot_id": "s1", "clip_index": 0, "in_s": 0.0, "duration_s": 1.3}]),
        db=None,
    )
    assert delays[0][1]["timeline_override"][0]["duration_s"] == pytest.approx(1.5)


@pytest.mark.asyncio
async def test_edit_no_grid_accepts_half_steps(monkeypatch):
    seq, _, delays = _arm(monkeypatch)
    await gj.dispatch_edit_timeline(
        _timeline_job(beat_grid=[]),
        "song_text",
        _req([{"slot_id": "s1", "clip_index": 0, "in_s": 0.0, "duration_s": 1.5}]),
        db=None,
    )
    assert delays[0][1]["timeline_override"][0]["duration_s"] == pytest.approx(1.5)


@pytest.mark.asyncio
async def test_edit_too_short_floor(monkeypatch):
    # 1 beat from grid start = 0.5s < the 0.6s floor; the slot's window CHANGED
    # (baseline s1 is 2 beats), so the floor applies.
    await _expect_422(
        monkeypatch,
        _timeline_job(),
        [{"slot_id": "s1", "clip_index": 0, "in_s": 0.0, "duration_beats": 1}],
        "TIMELINE_TOO_SHORT",
    )


@pytest.mark.asyncio
async def test_edit_floor_skipped_for_untouched_sub_floor_slot(monkeypatch):
    # B2: the worker legitimately produces sub-0.6s slots (1 beat at fast BPM).
    # An UNTOUCHED slot (same slot_id + in_s + duration_beats as the baseline)
    # must never trip the floor on round-trip.
    fast_grid = [0.0, 0.45, 0.9, 1.35]
    job = _timeline_job(beat_grid=fast_grid)
    variant = job.assembly_plan["variants"][0]
    variant["ai_timeline"]["slots"][0].update({"duration_beats": 1, "duration_s": 0.45})
    variant["ai_timeline"]["slots"][1].update({"duration_beats": 2, "duration_s": 0.9})
    seq, _, delays = _arm(monkeypatch)
    await gj.dispatch_edit_timeline(
        job,
        "song_text",
        _req(
            [
                {"slot_id": "s1", "clip_index": 0, "in_s": 0.0, "duration_beats": 1},
                {"slot_id": "s2", "clip_index": 1, "in_s": 1.0, "duration_beats": 2},
            ]
        ),
        db=None,
    )
    assert seq == ["persist", "enqueue"]
    assert delays[0][1]["timeline_override"][0]["duration_s"] == pytest.approx(0.45)


@pytest.mark.asyncio
async def test_edit_too_long_cap(monkeypatch):
    await _expect_422(
        monkeypatch,
        _timeline_job(beat_grid=[], src_dur=120.0),
        [
            {"slot_id": "s1", "clip_index": 0, "in_s": 0.0, "duration_s": 30.0},
            {"slot_id": "s2", "clip_index": 1, "in_s": 0.0, "duration_s": 30.0},
            {"slot_id": None, "clip_index": 0, "in_s": 0.0, "duration_s": 1.0},  # 61s total
        ],
        "TIMELINE_TOO_LONG",
    )


@pytest.mark.asyncio
async def test_edit_out_of_bounds(monkeypatch):
    # in_s + duration runs past the probed 10s source.
    await _expect_422(
        monkeypatch,
        _timeline_job(beat_grid=[]),
        [{"slot_id": "s1", "clip_index": 0, "in_s": 9.5, "duration_s": 1.0}],
        "TIMELINE_OUT_OF_BOUNDS",
    )


@pytest.mark.asyncio
async def test_edit_negative_in_s_out_of_bounds(monkeypatch):
    await _expect_422(
        monkeypatch,
        _timeline_job(beat_grid=[]),
        [{"slot_id": "s1", "clip_index": 0, "in_s": -0.5, "duration_s": 1.0}],
        "TIMELINE_OUT_OF_BOUNDS",
    )


@pytest.mark.asyncio
async def test_edit_new_slot_unknown_source_skips_bounds(monkeypatch):
    # clip 2 was never probed by the AI: no source_duration_s → bounds skipped
    # (the worker's probe clamps), source falls back to the pool path, and the
    # server mints a uuid slot_id.
    checked: list[str] = []
    seq, persists, delays = _arm(monkeypatch)
    monkeypatch.setattr(gj.storage, "object_exists", lambda p: checked.append(p) or True)

    await gj.dispatch_edit_timeline(
        _timeline_job(beat_grid=[]),
        "song_text",
        _req(
            [
                {"slot_id": "s1", "clip_index": 0, "in_s": 0.0, "duration_s": 1.0},
                {"slot_id": None, "clip_index": 2, "in_s": 999.0, "duration_s": 1.0},
            ]
        ),
        db=None,
    )
    new_slot = delays[0][1]["timeline_override"][1]
    assert new_slot["slot_id"] and new_slot["slot_id"] != "s1"
    uuid.UUID(new_slot["slot_id"])  # server-assigned uuid4
    assert new_slot["source_gcs_path"] == "slot-uploads/u/clip_2.mp4"
    assert new_slot["source_duration_s"] is None
    # The non-durable pool path is NOT existence-checked (only durable sources are).
    assert "slot-uploads/u/clip_2.mp4" not in checked
    assert any(p.startswith("generative-jobs/") for p in checked)


@pytest.mark.asyncio
async def test_edit_missing_durable_source_422(monkeypatch):
    seq, _, _ = _arm(monkeypatch, object_exists=False)
    with pytest.raises(HTTPException) as exc:
        await gj.dispatch_edit_timeline(
            _timeline_job(),
            "song_text",
            _req([{"slot_id": "s1", "clip_index": 0, "in_s": 0.0, "duration_beats": 2}]),
            db=None,
        )
    assert exc.value.status_code == 422
    assert exc.value.detail == {"code": "sources_expired"}
    assert seq == []  # fails BEFORE persisting anything


# ── POST: persist-before-enqueue + payload plumbing ──────────────────────────────


@pytest.mark.asyncio
async def test_edit_persists_before_enqueue(monkeypatch):
    seq, persists, delays = _arm(monkeypatch)
    job = _timeline_job()
    await gj.dispatch_edit_timeline(
        job,
        "song_text",
        _req(
            [
                {"slot_id": "s1", "clip_index": 0, "in_s": 0.0, "duration_beats": 2},
                {
                    "slot_id": "s2",
                    "clip_index": 1,
                    "in_s": 1.0,
                    "duration_beats": 3,
                    "removed": True,
                },
            ]
        ),
        db=None,
    )
    # Order is the contract: a worker pickup must observe the committed timeline.
    assert seq == ["persist", "enqueue"]
    job_id, variant_id, persisted = persists[0]
    assert (job_id, variant_id) == (str(job.id), "song_text")
    args, kwargs = delays[0]
    assert args == (str(job.id), "song_text")
    # The exact slots persisted are the exact override the task receives.
    assert kwargs["timeline_override"] is persisted
    assert persisted[1]["removed"] is True
    # Removed slots don't consume beats: only slot 1 walked the grid.
    assert persisted[0]["duration_s"] == pytest.approx(1.1)
    # Full lane-2 slot shape travels through (worker + GET merge both rely on it).
    assert set(persisted[0]) == {
        "slot_id",
        "clip_index",
        "source_gcs_path",
        "source_duration_s",
        "in_s",
        "duration_s",
        "duration_beats",
        "order",
        "moment_energy",
        "moment_description",
        "removed",
    }
    assert persisted[0]["moment_description"] == "crowd wave"


# ── DELETE: reset ────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_reset_deletes_user_timeline_and_enqueues_ai_slots(monkeypatch):
    seq, persists, delays = _arm(monkeypatch)
    user_slots = [{"slot_id": "s1", "clip_index": 0, "in_s": 0.0, "duration_s": 2.4}]
    job = _timeline_job(user_slots=user_slots)
    ai_slots = job.assembly_plan["variants"][0]["ai_timeline"]["slots"]

    await gj.dispatch_reset_timeline(job, "song_text", db=None)

    assert seq == ["persist", "enqueue"]
    assert persists[0] == (str(job.id), "song_text", None)  # None = remove user_timeline
    args, kwargs = delays[0]
    assert args == (str(job.id), "song_text")
    assert kwargs["timeline_override"] == ai_slots


@pytest.mark.asyncio
async def test_reset_disabled_403(monkeypatch):
    seq, _, _ = _arm(monkeypatch, enabled=False)
    with pytest.raises(HTTPException) as exc:
        await gj.dispatch_reset_timeline(_timeline_job(), "song_text", db=None)
    assert exc.value.status_code == 403
    assert seq == []


@pytest.mark.asyncio
async def test_reset_job_busy(monkeypatch):
    seq, _, _ = _arm(monkeypatch)
    with pytest.raises(HTTPException) as exc:
        await gj.dispatch_reset_timeline(
            _timeline_job(sibling_rendering=True), "song_text", db=None
        )
    assert exc.value.detail == {"code": "JOB_BUSY"}
    assert seq == []


@pytest.mark.asyncio
async def test_reset_without_ai_timeline_422(monkeypatch):
    seq, _, _ = _arm(monkeypatch)
    with pytest.raises(HTTPException) as exc:
        await gj.dispatch_reset_timeline(_timeline_job(with_ai=False), "song_text", db=None)
    assert exc.value.detail == {"code": "no_timeline"}
    assert seq == []


@pytest.mark.asyncio
async def test_reset_ineligible_lyrics_variant_422(monkeypatch):
    # M3: reset runs the SAME eligibility gate as POST — a lyrics variant must
    # not be re-renderable through the reset endpoint either.
    seq, _, _ = _arm(monkeypatch)
    job = _timeline_job(variant_id="song_lyrics", text_mode="lyrics")
    with pytest.raises(HTTPException) as exc:
        await gj.dispatch_reset_timeline(job, "song_lyrics", db=None)
    assert exc.value.status_code == 422
    assert exc.value.detail == {"code": "lyrics_sync"}
    assert seq == []


@pytest.mark.asyncio
async def test_reset_expired_sources_422(monkeypatch):
    seq, _, _ = _arm(monkeypatch)
    with pytest.raises(HTTPException) as exc:
        await gj.dispatch_reset_timeline(_timeline_job(durable=False), "song_text", db=None)
    assert exc.value.status_code == 422
    assert exc.value.detail == {"code": "sources_expired"}
    assert seq == []


# ── persist_user_timeline (real function, fake row-locked session) ──────────────


class _FakeLockingDB:
    def __init__(self, job):
        self._job = job
        self.committed = False
        self.locked = None

    async def get(self, model, pk, with_for_update=False):
        self.locked = with_for_update
        return self._job

    async def commit(self):
        self.committed = True


@pytest.mark.asyncio
async def test_persist_user_timeline_merges_row_locked():
    row = types.SimpleNamespace(
        assembly_plan={"variants": [{"variant_id": "song_text", "render_status": "ready"}]}
    )
    db = _FakeLockingDB(row)
    slots = [{"slot_id": "s1", "clip_index": 0}]

    await gj.persist_user_timeline(db, str(uuid.uuid4()), "song_text", slots)

    assert db.locked is True  # SELECT ... FOR UPDATE — mirrors _update_variant_entry
    assert db.committed is True
    assert row.assembly_plan["variants"][0]["user_timeline"] == {"slots": slots}
    # Sibling fields survive the merge.
    assert row.assembly_plan["variants"][0]["render_status"] == "ready"


@pytest.mark.asyncio
async def test_persist_user_timeline_none_removes_key():
    row = types.SimpleNamespace(
        assembly_plan={
            "variants": [{"variant_id": "song_text", "user_timeline": {"slots": [{"a": 1}]}}]
        }
    )
    db = _FakeLockingDB(row)
    await gj.persist_user_timeline(db, str(uuid.uuid4()), "song_text", None)
    assert "user_timeline" not in row.assembly_plan["variants"][0]
    assert db.committed is True


# ── Re-sign fix: failed re-render keeps serving the last good video ─────────────


def _last_good_job(render_status: str):
    return types.SimpleNamespace(
        id=uuid.uuid4(),
        assembly_plan={
            "variants": [
                {
                    "variant_id": "song_text",
                    "render_status": render_status,
                    "video_path": "generative-jobs/j/variant_1_song_text.mp4",
                    "output_url": "https://stale.example/expired",
                }
            ]
        },
    )


def test_failed_variant_with_video_path_is_resigned(monkeypatch):
    # THE fix pin: a variant whose RE-render failed still has its last good
    # video_path — that video must stay playable past the 24h signature expiry,
    # so re-sign on read exactly like a ready variant.
    monkeypatch.setattr(gj, "signed_get_url", lambda p, t: f"https://fresh.example/{p}")
    out = gj._variants_for_response(_last_good_job("failed"))
    assert out[0]["output_url"] == (
        "https://fresh.example/generative-jobs/j/variant_1_song_text.mp4"
    )


def test_rendering_variant_keeps_stored_url(monkeypatch):
    # In-flight re-render: stored URL untouched (existing suite pins the same).
    monkeypatch.setattr(gj, "signed_get_url", lambda p, t: f"https://fresh.example/{p}")
    out = gj._variants_for_response(_last_good_job("rendering"))
    assert out[0]["output_url"] == "https://stale.example/expired"


# ── Regression lock: unmodified GET→POST round-trip must never 422 ───────────────
# The worker's ai_timeline legitimately contains slots route validation used to
# reject: `duration_beats: null` footage-trimmed slots, round(x, 3) durations
# like 1.137 that are no 0.5-multiple, and sub-0.6s 1-beat slots at fast BPM.
# Feeding the GET's effective timeline straight back through POST (exactly what
# the frontend does on an untouched draft) must succeed and enqueue.


def _frontend_payload(slots: list[dict]) -> list[dict]:
    """Mirror TimelineEditor.buildPayload: beats slots post beats only; null-beats
    slots post their exact duration_s."""
    return [
        {
            "slot_id": s["slot_id"],
            "clip_index": s["clip_index"],
            "in_s": round(float(s["in_s"]), 3),
            "duration_beats": s.get("duration_beats"),
            "duration_s": s["duration_s"] if s.get("duration_beats") is None else None,
            "removed": bool(s.get("removed") or False),
        }
        for s in slots
    ]


def _worker_slot(slot_id, clip_index, prefix, *, in_s, duration_s, duration_beats, src_dur):
    return {
        "slot_id": slot_id,
        "clip_index": clip_index,
        "source_gcs_path": f"{prefix}clip_{clip_index}.mp4",
        "source_duration_s": src_dur,
        "in_s": in_s,
        "duration_s": duration_s,
        "duration_beats": duration_beats,
        "order": 0,
        "moment_energy": 5.0,
        "moment_description": None,
    }


def _realistic_job(*, variant_id: str, grid: list[float]):
    job = _timeline_job(variant_id=variant_id, beat_grid=grid)
    prefix = f"generative-jobs/{job.id}/sources/"
    if grid:
        # Fast-BPM song_text: a sub-floor 1-beat slot, a footage-trimmed
        # null-beats slot with a non-0.5-multiple duration, and a 3-beat slot.
        slots = [
            _worker_slot(
                "s1", 0, prefix, in_s=2.4, duration_s=0.45, duration_beats=1, src_dur=10.0
            ),
            _worker_slot(
                "s2", 1, prefix, in_s=0.0, duration_s=1.137, duration_beats=None, src_dur=1.137
            ),
            _worker_slot("s3", 2, prefix, in_s=1.2, duration_s=1.35, duration_beats=3, src_dur=8.0),
        ]
    else:
        # original_text: every duration is round(x, 3) — none are 0.5-multiples,
        # one is a sub-floor footage trim.
        slots = [
            _worker_slot(
                "s1", 0, prefix, in_s=0.8, duration_s=1.137, duration_beats=None, src_dur=10.0
            ),
            _worker_slot(
                "s2", 1, prefix, in_s=0.0, duration_s=2.503, duration_beats=None, src_dur=6.0
            ),
            _worker_slot(
                "s3", 2, prefix, in_s=0.0, duration_s=0.583, duration_beats=None, src_dur=0.583
            ),
        ]
    job.assembly_plan["variants"][0]["ai_timeline"]["slots"] = slots
    return job


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("variant_id", "grid"),
    [
        ("song_text", [0.0, 0.45, 0.9, 1.35, 1.8, 2.25, 2.7, 3.15]),  # fast BPM
        ("original_text", []),  # no grid
    ],
)
async def test_unmodified_roundtrip_get_then_post_succeeds(monkeypatch, variant_id, grid):
    seq, persists, delays = _arm(monkeypatch)
    monkeypatch.setattr(gj, "signed_get_url", lambda p, t: f"https://fresh.example/{p}")
    job = _realistic_job(variant_id=variant_id, grid=grid)

    out = gj.dispatch_get_timeline(job, variant_id)
    assert out["editable"] is True

    await gj.dispatch_edit_timeline(job, variant_id, _req(_frontend_payload(out["slots"])), db=None)

    assert seq == ["persist", "enqueue"]
    override = delays[0][1]["timeline_override"]
    assert [s["slot_id"] for s in override] == ["s1", "s2", "s3"]
    # Null-beats slots round-trip their exact window; beats slots re-derive from
    # the same grid at the same offsets.
    expected = [s["duration_s"] for s in job.assembly_plan["variants"][0]["ai_timeline"]["slots"]]
    assert [s["duration_s"] for s in override] == [pytest.approx(d) for d in expected]
