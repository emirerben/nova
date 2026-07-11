"""Plan 010 — caption re-renders preserve the user's SFX/overlay lanes.

The three caption terminals (_run_reburn_narrated_captions,
_run_reburn_narrated_bed_level, _run_retranscribe_subtitled) rebuild video_path
from the caption-free base, so persisted media lanes must be rebuilt through the
shared _reapply_user_media_layers chain (3A) with the OV-7 deferred terminal
status, 2A/OV-3 supersede tokens, OV-4 delete gating, and D16-C snapshot
freeing. House style of test_generative_build.py: fake job session, storage and
burn IO monkeypatched — no DB, no ffmpeg, no network.
"""

from __future__ import annotations

import pytest

import app.tasks.generative_build as gb
from tests.tasks.conftest import FakeJob, patch_job_session

JOB_ID = "12345678-1234-5678-1234-567812345678"

_SFX = [{"id": "sfx-1", "src_gcs_path": "sound-effects/pop/audio.mp3", "at_s": 1.0, "gain": 0.8}]
_OVERLAYS = [
    {
        "id": "ov-1",
        "kind": "image",
        "src_gcs_path": "users/u1/plan/item/overlays/card.png",
        "position": "center",
        "scale": 0.35,
        "start_s": 0.0,
        "end_s": 2.0,
        "z": 0,
    }
]

_BED_CANDIDATES = {
    "voiceover_gcs_path": "voiceover-uploads/x/voice.webm",
    "filming_guide": [],
    "clip_paths": ["slot-uploads/a.mp4"],
    "narrative_shot_count": 0,
    "landscape_fit": "fit",
}


def _make_job(assembly_plan=None, all_candidates=None) -> FakeJob:
    return FakeJob(
        assembly_plan=assembly_plan or {},
        all_candidates=all_candidates,
        status="variants_ready",
        job_id=JOB_ID,
    )


def _patch_job_session(monkeypatch, job):
    # The reapply preps flag_modified() the fake (non-ORM) job — no-op it.
    patch_job_session(monkeypatch, job, noop_flag_modified=True)


def _narrated_variant(**over):
    v = {
        "variant_id": "narrated",
        "rank": 1,
        "render_status": "ready",
        "resolved_archetype": "narrated",
        "base_video_path": "generative-jobs/j/variant_1_narrated_base.mp4",
        "video_path": "generative-jobs/j/variant_1_narrated.mp4",
        "output_url": "https://signed/old",
        "caption_cues": [{"text": "the energy here", "start_s": 0.0, "end_s": 1.2}],
    }
    v.update(over)
    return v


def _subtitled_variant(**over):
    return _narrated_variant(
        variant_id="subtitled",
        resolved_archetype="subtitled",
        caption_language="en",
        voiceover_caption_style="sentence",
        **over,
    )


def _bed_variant(**over):
    return _narrated_variant(
        captions_enabled=True,
        voiceover_caption_style="sentence",
        voiceover_caption_font=None,
        voiceover_bed_level=0.25,
        narrated_timings=[
            {"step_id": "shot_1", "start_s": 0.0, "end_s": 1.2, "confidence": 1.0},
        ],
        **over,
    )


def _arm_flags(monkeypatch, *, sfx=True, overlays=True):
    monkeypatch.setattr(gb.settings, "sound_effects_enabled", sfx, raising=False)
    monkeypatch.setattr(gb.settings, "media_overlays_enabled", overlays, raising=False)


def _capture_reapply(monkeypatch) -> list[dict]:
    calls: list[dict] = []
    # Returns True = "chain took ownership of the terminal status" (R1-3), so the
    # OV-7 deferred-status assertions see the terminal exactly as prod would.
    monkeypatch.setattr(
        gb, "_reapply_user_media_layers", lambda **kw: calls.append(kw) or True, raising=False
    )
    return calls


def _assert_reapply_call(reapply: list[dict], *, variant_id: str, gen: str | None):
    assert len(reapply) == 1
    call = reapply[0]
    assert call["job_id"] == JOB_ID
    assert call["variant_id"] == variant_id
    assert call["expected_render_gen_id"] == gen
    # R4-2: caption terminals thread a wall-clock deadline into the chain.
    assert isinstance(call["deadline_monotonic"], float)


def _patch_storage(monkeypatch, seen: dict, *, upload_hook=None):
    def _dl(_src, dst):
        with open(dst, "wb") as f:
            f.write(b"x")

    def _upload(_local, gcs_path, **_k):
        seen.setdefault("uploaded", []).append(gcs_path)
        if upload_hook is not None:
            upload_hook(gcs_path)
        return "https://signed/new"

    monkeypatch.setattr("app.storage.download_to_file", _dl)
    monkeypatch.setattr("app.storage.upload_public_read", _upload)
    monkeypatch.setattr(
        "app.storage.delete_object_best_effort",
        lambda path: seen.setdefault("deleted", []).append(path) or True,
    )


def _patch_reburn_io(monkeypatch, seen: dict, *, upload_hook=None):
    _patch_storage(monkeypatch, seen, upload_hook=upload_hook)
    monkeypatch.setattr(
        gb,
        "_burn_persisted_captions_onto_base",
        lambda base_local, out_local, variant, tmpdir: open(out_local, "wb").write(b"x"),
    )


def _patch_bed_io(monkeypatch, seen: dict, *, upload_hook=None):
    _patch_reburn_io(monkeypatch, seen, upload_hook=upload_hook)
    monkeypatch.setattr(
        "app.tasks.template_orchestrate._download_clips_parallel",
        lambda paths, tmpdir: [f"{tmpdir}/local_{i}.mp4" for i in range(len(paths))],
    )

    def _fake_assemble(step_timings, clip_assignments, voiceover_local, output_path, tmpdir, **kw):
        with open(output_path, "wb") as f:
            f.write(b"x")
        base_path = kw.get("base_output_path")
        if base_path:
            with open(base_path, "wb") as f:
                f.write(b"x")
        return []

    monkeypatch.setattr("app.pipeline.narrated_assembler.assemble_narrated", _fake_assemble)


class _Word:
    def __init__(self, text, start_s, end_s):
        self.text, self.start_s, self.end_s = text, start_s, end_s


class _Transcript:
    def __init__(self, words, language="tr"):
        self.words = words
        self.language = language


def _patch_retx_io(monkeypatch, seen: dict, *, words=None, upload_hook=None):
    _patch_storage(monkeypatch, seen, upload_hook=upload_hook)
    default_words = [_Word("merhaba", 0.0, 0.6), _Word("dünya.", 0.6, 1.2)]
    monkeypatch.setattr(
        "app.pipeline.transcribe.transcribe_whisper",
        lambda _p, language=None: _Transcript(
            default_words if words is None else words, language=language or "tr"
        ),
    )
    monkeypatch.setattr(
        "app.pipeline.caption_correct.correct_caption_cues", lambda cues, *a, **k: cues
    )

    def _write_ass(cues, path, **kwargs):
        with open(path, "w", encoding="utf-8") as f:
            f.write("ass")

    monkeypatch.setattr("app.pipeline.captions.generate_ass_from_cues", _write_ass)
    monkeypatch.setattr("app.pipeline.captions.generate_word_pop_ass", _write_ass)
    monkeypatch.setattr(
        "app.pipeline.narrated_assembler.burn_captions_on_video", lambda *a, **k: None
    )


_PATHS = ["caption_reburn", "bed_level", "retranscribe"]


def _lane_variant(path: str, **over) -> dict:
    if path == "bed_level":
        return _bed_variant(**over)
    if path == "retranscribe":
        return _subtitled_variant(**over)
    return _narrated_variant(**over)


def _run_path(monkeypatch, path: str, variant: dict, *, render_gen_id="tok-1", upload_hook=None):
    """Wire the path-specific IO and run its worker terminal against a fake job."""
    all_candidates = dict(_BED_CANDIDATES) if path == "bed_level" else {}
    job = _make_job(assembly_plan={"variants": [variant]}, all_candidates=all_candidates)
    _patch_job_session(monkeypatch, job)
    seen: dict = {}
    if path == "caption_reburn":
        _patch_reburn_io(monkeypatch, seen, upload_hook=upload_hook)
        gb._run_reburn_narrated_captions(JOB_ID, variant["variant_id"], render_gen_id=render_gen_id)
    elif path == "bed_level":
        _patch_bed_io(monkeypatch, seen, upload_hook=upload_hook)
        gb._run_reburn_narrated_bed_level(
            JOB_ID, variant["variant_id"], 0.6, render_gen_id=render_gen_id
        )
    else:
        _patch_retx_io(monkeypatch, seen, upload_hook=upload_hook)
        gb._run_retranscribe_subtitled(
            JOB_ID, variant["variant_id"], "tr", render_gen_id=render_gen_id
        )
    return job, seen


# ── CRITICAL wipe regression: lanes reapplied after every caption re-render ────


@pytest.mark.parametrize("path", _PATHS)
def test_caption_terminal_reapplies_persisted_lanes(monkeypatch, path):
    _arm_flags(monkeypatch)
    reapply = _capture_reapply(monkeypatch)
    variant = _lane_variant(
        path,
        media_overlays=[dict(_OVERLAYS[0])],
        sound_effects=[dict(_SFX[0])],
        render_generation_id="tok-1",
        pre_media_overlay_video_path="generative-jobs/j/old_pre_overlay.mp4",
        pre_sfx_video_path="generative-jobs/j/old_pre_sfx.mp4",
    )
    vid = variant["variant_id"]
    old_video = variant["video_path"]

    job, seen = _run_path(monkeypatch, path, variant)

    v = job.assembly_plan["variants"][0]
    _assert_reapply_call(reapply, variant_id=vid, gen="tok-1")
    # Deliberate snapshot reset — stale keys point at the deleted pre-reburn video.
    assert v["pre_media_overlay_video_path"] is None
    assert v["pre_sfx_video_path"] is None
    # OV-7: the reapply chain owns the final ready/failed — no effect-less "ready".
    assert v["render_status"] == "rendering"
    assert v.get("render_finished_at") is None
    # D16-C + old burn: orphaned snapshots and the superseded video are freed.
    assert "generative-jobs/j/old_pre_overlay.mp4" in seen["deleted"]
    assert "generative-jobs/j/old_pre_sfx.mp4" in seen["deleted"]
    assert old_video in seen["deleted"]


@pytest.mark.parametrize("path", _PATHS)
def test_caption_terminal_sfx_only_still_reapplies(monkeypatch, path):
    _arm_flags(monkeypatch)
    reapply = _capture_reapply(monkeypatch)
    variant = _lane_variant(path, sound_effects=[dict(_SFX[0])], render_generation_id="tok-1")
    vid = variant["variant_id"]

    job, _seen = _run_path(monkeypatch, path, variant)

    v = job.assembly_plan["variants"][0]
    _assert_reapply_call(reapply, variant_id=vid, gen="tok-1")
    assert v["render_status"] == "rendering"


@pytest.mark.parametrize("path", _PATHS)
def test_caption_terminal_without_lanes_stays_terminal_no_reapply(monkeypatch, path):
    """No persisted lanes + flags off (the shipped default): no reapply hop, the
    terminal write itself lands "ready" — same surface as before plan 010."""
    _arm_flags(monkeypatch, sfx=False, overlays=False)
    reapply = _capture_reapply(monkeypatch)
    variant = _lane_variant(path, render_generation_id="tok-1")
    old_video = variant["video_path"]

    job, seen = _run_path(monkeypatch, path, variant)

    v = job.assembly_plan["variants"][0]
    assert reapply == []
    assert v["render_status"] == "ready"
    assert v["render_finished_at"]
    assert old_video in seen["deleted"]  # old-blob free still happens


# ── 2A/OV-3/OV-4: stale supersede token discards write, delete, and reapply ────


@pytest.mark.parametrize("path", _PATHS)
def test_caption_terminal_stale_token_discards_write_and_skips_delete(monkeypatch, path):
    _arm_flags(monkeypatch)
    reapply = _capture_reapply(monkeypatch)
    variant = _lane_variant(
        path,
        media_overlays=[dict(_OVERLAYS[0])],
        sound_effects=[dict(_SFX[0])],
        render_generation_id="tok-2-newer",
    )
    old_video = variant["video_path"]

    job, seen = _run_path(monkeypatch, path, variant, render_gen_id="tok-old")

    v = job.assembly_plan["variants"][0]
    assert v["video_path"] == old_video  # terminal write discarded
    assert v["render_status"] == "ready"  # untouched — the winner owns the state
    assert seen.get("deleted") is None  # OV-4: no old-blob delete
    assert reapply == []


@pytest.mark.parametrize("path", _PATHS)
def test_caption_terminal_superseded_mid_run_skips_delete(monkeypatch, path):
    """Token bumped between task start and the terminal write (an editor Save
    landing mid-run, inside the upload stub): the terminal write, the live-blob
    deletes, and the reapply are all skipped. The ONLY deletes allowed are the
    just-uploaded, never-referenced key(s) — F3 frees those on the discard."""
    _arm_flags(monkeypatch)
    reapply = _capture_reapply(monkeypatch)
    variant = _lane_variant(
        path,
        media_overlays=[dict(_OVERLAYS[0])],
        sound_effects=[dict(_SFX[0])],
        render_generation_id="tok-1",
        pre_media_overlay_video_path="generative-jobs/j/old_pre_overlay.mp4",
        pre_sfx_video_path="generative-jobs/j/old_pre_sfx.mp4",
    )
    old_video = variant["video_path"]
    old_base = variant["base_video_path"]
    job_holder: dict = {}

    def _supersede(_gcs_path):
        # Replace (don't mutate) the entry — a DB-side write never touches the
        # task's in-memory snapshot; the fake job must not either.
        plan = job_holder["job"].assembly_plan
        newer = {**plan["variants"][0], "render_generation_id": "tok-2-newer"}
        plan["variants"] = [newer]

    all_candidates = dict(_BED_CANDIDATES) if path == "bed_level" else {}
    job = _make_job(assembly_plan={"variants": [variant]}, all_candidates=all_candidates)
    job_holder["job"] = job
    _patch_job_session(monkeypatch, job)
    seen: dict = {}
    if path == "caption_reburn":
        _patch_reburn_io(monkeypatch, seen, upload_hook=_supersede)
        gb._run_reburn_narrated_captions(JOB_ID, variant["variant_id"], render_gen_id="tok-1")
    elif path == "bed_level":
        _patch_bed_io(monkeypatch, seen, upload_hook=_supersede)
        gb._run_reburn_narrated_bed_level(JOB_ID, variant["variant_id"], 0.6, render_gen_id="tok-1")
    else:
        _patch_retx_io(monkeypatch, seen, upload_hook=_supersede)
        gb._run_retranscribe_subtitled(JOB_ID, variant["variant_id"], "tr", render_gen_id="tok-1")

    v = job.assembly_plan["variants"][0]
    assert v["video_path"] == old_video  # terminal write discarded
    assert v["base_video_path"] == old_base
    # Live blobs (old video/base, snapshots) untouched; only the fresh uploads
    # were freed (F3 — they were never referenced by any accepted write).
    assert sorted(seen.get("deleted", [])) == sorted(seen.get("uploaded", []))
    assert reapply == []


# ── R1-3: chain no-op must never strand a deferred "rendering" terminal ─────────


@pytest.mark.parametrize("path", _PATHS)
def test_caption_terminal_lanes_cleared_midrun_tokenless_finalizes_ready(monkeypatch, path):
    """Stranded-rendering lockout (R1-3): variant HAS lanes at task start (so the
    terminal defers status, OV-7), the run is tokenless (render_gen_id=None), and
    the lanes are cleared in the DB mid-run (upload stub). The REAL reapply chain
    then no-ops — the terminal must finalize "ready", never leave "rendering"."""
    _arm_flags(monkeypatch)
    variant = _lane_variant(
        path,
        media_overlays=[dict(_OVERLAYS[0])],
        sound_effects=[dict(_SFX[0])],
    )
    job_holder: dict = {}

    def _clear_lanes(_gcs_path):
        # Replace (don't mutate) the entry — a DB-side clear never touches the
        # task's in-memory snapshot, so will_reapply stays True (OV-7 defers).
        plan = job_holder["job"].assembly_plan
        cleared = {**plan["variants"][0], "media_overlays": None, "sound_effects": None}
        plan["variants"] = [cleared]

    all_candidates = dict(_BED_CANDIDATES) if path == "bed_level" else {}
    job = _make_job(assembly_plan={"variants": [variant]}, all_candidates=all_candidates)
    job_holder["job"] = job
    _patch_job_session(monkeypatch, job)
    seen: dict = {}
    if path == "caption_reburn":
        _patch_reburn_io(monkeypatch, seen, upload_hook=_clear_lanes)
        gb._run_reburn_narrated_captions(JOB_ID, variant["variant_id"], render_gen_id=None)
    elif path == "bed_level":
        _patch_bed_io(monkeypatch, seen, upload_hook=_clear_lanes)
        gb._run_reburn_narrated_bed_level(JOB_ID, variant["variant_id"], 0.6, render_gen_id=None)
    else:
        _patch_retx_io(monkeypatch, seen, upload_hook=_clear_lanes)
        gb._run_retranscribe_subtitled(JOB_ID, variant["variant_id"], "tr", render_gen_id=None)

    v = job.assembly_plan["variants"][0]
    assert v["render_status"] == "ready"
    assert v["render_finished_at"]


# ── F5: exception AFTER the accepted video swap must land failed, not ready ─────


def test_exception_after_accepted_swap_marks_failed_not_ready(monkeypatch):
    """The task wrapper's except handler wrote "ready" unconditionally — but once
    the video swap landed, a later exception means the persisted lanes are
    missing from the new video; "ready" would lie (F5)."""
    _arm_flags(monkeypatch)
    variant = _narrated_variant(
        media_overlays=[dict(_OVERLAYS[0])],
        sound_effects=[dict(_SFX[0])],
        render_generation_id="tok-1",
    )
    job = _make_job(assembly_plan={"variants": [variant]})
    _patch_job_session(monkeypatch, job)
    seen: dict = {}
    _patch_reburn_io(monkeypatch, seen)

    def _boom(**_kw):
        raise RuntimeError("reapply exploded")

    monkeypatch.setattr(gb, "_reapply_user_media_layers", _boom, raising=False)

    gb.reburn_narrated_captions.run(JOB_ID, "narrated", render_gen_id="tok-1")

    v = job.assembly_plan["variants"][0]
    assert v["render_status"] == "failed"
    assert "reapply exploded" in v["render_error"]


def test_exception_before_swap_still_reverts_to_ready(monkeypatch):
    """Companion pin: an exception BEFORE the video swap keeps the pre-fix
    behavior — last-good video intact, in-flight state cleared to "ready"."""
    _arm_flags(monkeypatch)
    variant = _narrated_variant(render_generation_id="tok-1", render_status="rendering")
    job = _make_job(assembly_plan={"variants": [variant]})
    _patch_job_session(monkeypatch, job)
    seen: dict = {}
    _patch_reburn_io(monkeypatch, seen)
    monkeypatch.setattr(
        "app.storage.download_to_file",
        lambda *_a, **_k: (_ for _ in ()).throw(RuntimeError("gcs down")),
    )

    gb.reburn_narrated_captions.run(JOB_ID, "narrated", render_gen_id="tok-1")

    v = job.assembly_plan["variants"][0]
    assert v["render_status"] == "ready"
    assert v["video_path"] == "generative-jobs/j/variant_1_narrated.mp4"


# ── OV-2: card saved during the bed rebuild survives the terminal patch ────────


def test_bed_level_terminal_patch_never_carries_media_overlays(monkeypatch):
    """The pre-fix patch round-tripped the task-start snapshot's media_overlays,
    clobbering a card saved during the minutes-long rebuild. The patch must not
    contain the key at all — the _update_variant_entry merge keeps the DB value."""
    _arm_flags(monkeypatch)
    variant = _bed_variant(render_generation_id="tok-1")
    job = _make_job(assembly_plan={"variants": [variant]}, all_candidates=dict(_BED_CANDIDATES))
    _patch_job_session(monkeypatch, job)
    seen: dict = {}
    _patch_bed_io(monkeypatch, seen)

    updates: list[dict] = []
    real_update = gb._update_variant_entry

    def _spy(jid, vid, patch, **kw):
        updates.append(dict(patch))
        return real_update(jid, vid, patch, **kw)

    monkeypatch.setattr(gb, "_update_variant_entry", _spy)

    def _assemble_and_save_card(
        step_timings, clip_assignments, voiceover_local, output_path, tmpdir, **kw
    ):
        # Mid-render save: a card lands in the DB while the rebuild runs.
        job.assembly_plan["variants"][0]["media_overlays"] = [dict(_OVERLAYS[0])]
        with open(output_path, "wb") as f:
            f.write(b"x")
        base_path = kw.get("base_output_path")
        if base_path:
            with open(base_path, "wb") as f:
                f.write(b"x")
        return []

    monkeypatch.setattr(
        "app.pipeline.narrated_assembler.assemble_narrated", _assemble_and_save_card
    )

    gb._run_reburn_narrated_bed_level(JOB_ID, "narrated", 0.6, render_gen_id="tok-1")

    terminal = [u for u in updates if "video_path" in u]
    assert len(terminal) == 1
    assert "media_overlays" not in terminal[0]
    assert terminal[0]["pre_media_overlay_video_path"] is None
    assert terminal[0]["pre_sfx_video_path"] is None
    # The mid-render card survived the terminal write.
    assert job.assembly_plan["variants"][0]["media_overlays"] == [dict(_OVERLAYS[0])]


# ── OV-10: soft-timeout inside the reapply chain fails loudly ───────────────────


def test_soft_time_limit_inside_reapply_marks_failed_not_ready(monkeypatch):
    """SoftTimeLimitExceeded mid-reapply must land the variant `failed` — never a
    silent effect-less `ready` (the caption terminal deferred its status, OV-7)."""
    from celery.exceptions import SoftTimeLimitExceeded

    _arm_flags(monkeypatch)
    variant = _narrated_variant(
        media_overlays=[dict(_OVERLAYS[0])],
        sound_effects=[dict(_SFX[0])],
        render_generation_id="tok-1",
    )
    job = _make_job(assembly_plan={"variants": [variant]})
    _patch_job_session(monkeypatch, job)
    seen: dict = {}
    _patch_reburn_io(monkeypatch, seen)

    def _boom(**_kw):
        raise SoftTimeLimitExceeded()

    monkeypatch.setattr(gb, "_run_media_overlay_pass", _boom, raising=False)

    gb._run_reburn_narrated_captions(JOB_ID, "narrated", render_gen_id="tok-1")

    v = job.assembly_plan["variants"][0]
    assert v["render_status"] == "failed"
    assert v["render_error"]


# ── retranscribe empty-cues early return keeps the effect-bearing video ────────


def test_retranscribe_empty_cues_keeps_video_and_skips_reapply(monkeypatch):
    _arm_flags(monkeypatch)
    reapply = _capture_reapply(monkeypatch)
    variant = _subtitled_variant(
        media_overlays=[dict(_OVERLAYS[0])],
        sound_effects=[dict(_SFX[0])],
        render_generation_id="tok-1",
        render_status="rendering",
    )
    old_video = variant["video_path"]
    job = _make_job(assembly_plan={"variants": [variant]})
    _patch_job_session(monkeypatch, job)
    seen: dict = {}
    _patch_retx_io(monkeypatch, seen, words=[])

    gb._run_retranscribe_subtitled(JOB_ID, "subtitled", "tr", render_gen_id="tok-1")

    v = job.assembly_plan["variants"][0]
    assert reapply == []
    assert v["video_path"] == old_video  # untouched
    assert v["render_status"] == "ready"
    assert seen.get("deleted") is None


# ── _reapply_user_media_layers chain pins (3A) ──────────────────────────────────


def _chain_passes(monkeypatch, variant):
    job = _make_job(assembly_plan={"variants": [variant]})
    _patch_job_session(monkeypatch, job)
    overlay_calls: list[dict] = []
    sfx_calls: list[dict] = []
    monkeypatch.setattr(
        gb, "_run_media_overlay_pass", lambda **kw: overlay_calls.append(kw), raising=False
    )
    monkeypatch.setattr(gb, "_run_sfx_pass", lambda **kw: sfx_calls.append(kw), raising=False)
    return overlay_calls, sfx_calls


def test_reapply_layers_routes_to_overlay_pass_when_overlays_persisted(monkeypatch):
    _arm_flags(monkeypatch)
    variant = _narrated_variant(media_overlays=[dict(_OVERLAYS[0])], sound_effects=[dict(_SFX[0])])
    overlay_calls, sfx_calls = _chain_passes(monkeypatch, variant)

    owned = gb._reapply_user_media_layers(
        job_id=JOB_ID, variant_id="narrated", expected_render_gen_id="t"
    )

    assert owned is True  # R1-3: a pass ran → chain owns the terminal status
    assert len(overlay_calls) == 1
    assert overlay_calls[0]["overlays_raw"] == [dict(_OVERLAYS[0])]
    assert overlay_calls[0]["expected_render_gen_id"] == "t"
    # The overlay pass owns the SFX hook internally — the chain must not double-run it.
    assert sfx_calls == []


def test_reapply_layers_sfx_only_branch(monkeypatch):
    _arm_flags(monkeypatch)
    variant = _narrated_variant(sound_effects=[dict(_SFX[0])])
    overlay_calls, sfx_calls = _chain_passes(monkeypatch, variant)

    owned = gb._reapply_user_media_layers(job_id=JOB_ID, variant_id="narrated")

    assert owned is True
    assert overlay_calls == []
    assert len(sfx_calls) == 1
    assert sfx_calls[0]["sfx_raw"] == [dict(_SFX[0])]


def test_reapply_layers_neither_lane_noop(monkeypatch):
    _arm_flags(monkeypatch)
    variant = _narrated_variant()
    overlay_calls, sfx_calls = _chain_passes(monkeypatch, variant)

    owned = gb._reapply_user_media_layers(job_id=JOB_ID, variant_id="narrated")

    assert owned is False  # R1-3: no-op → caller must finalize its deferred status
    assert overlay_calls == [] and sfx_calls == []


def test_reapply_layers_overlay_flag_off_falls_through_to_sfx(monkeypatch):
    _arm_flags(monkeypatch, overlays=False)
    variant = _narrated_variant(media_overlays=[dict(_OVERLAYS[0])], sound_effects=[dict(_SFX[0])])
    overlay_calls, sfx_calls = _chain_passes(monkeypatch, variant)

    owned = gb._reapply_user_media_layers(job_id=JOB_ID, variant_id="narrated")

    assert owned is True
    assert overlay_calls == []
    assert len(sfx_calls) == 1


def test_reapply_layers_both_flags_off_noop(monkeypatch):
    _arm_flags(monkeypatch, sfx=False, overlays=False)
    variant = _narrated_variant(media_overlays=[dict(_OVERLAYS[0])], sound_effects=[dict(_SFX[0])])
    overlay_calls, sfx_calls = _chain_passes(monkeypatch, variant)

    owned = gb._reapply_user_media_layers(job_id=JOB_ID, variant_id="narrated")

    assert owned is False
    assert overlay_calls == [] and sfx_calls == []


# ── F4: reapply-prep RMW hardening (row lock, gen guard, delete-after-commit) ──


@pytest.mark.parametrize(
    ("helper", "lane_key"),
    [
        (gb._reapply_persisted_media_overlays_if_any, "media_overlays"),
        (gb._reapply_persisted_sfx_if_any, "sound_effects"),
    ],
    ids=["overlay_prep", "sfx_prep"],
)
def test_reapply_prep_superseded_gen_discards_and_skips_deletes(monkeypatch, helper, lane_key):
    """A prep whose expected gen no longer matches the row must not touch the
    snapshots, run a pass, or delete anything — but it DOES own the outcome
    (returns True): the newer generation controls the terminal status."""
    _arm_flags(monkeypatch)
    lane = [dict(_OVERLAYS[0])] if lane_key == "media_overlays" else [dict(_SFX[0])]
    variant = _narrated_variant(
        render_generation_id="tok-2-newer",
        pre_media_overlay_video_path="generative-jobs/j/old_pre_overlay.mp4",
        pre_sfx_video_path="generative-jobs/j/old_pre_sfx.mp4",
        **{lane_key: lane},
    )
    overlay_calls: list[dict] = []
    sfx_calls: list[dict] = []
    monkeypatch.setattr(
        gb, "_run_media_overlay_pass", lambda **kw: overlay_calls.append(kw), raising=False
    )
    monkeypatch.setattr(gb, "_run_sfx_pass", lambda **kw: sfx_calls.append(kw), raising=False)
    deleted: list[str] = []
    monkeypatch.setattr(
        "app.storage.delete_object_best_effort", lambda p: deleted.append(p) or True
    )
    job = _make_job(assembly_plan={"variants": [variant]})
    _patch_job_session(monkeypatch, job)

    owned = helper(job_id=JOB_ID, variant_id="narrated", expected_render_gen_id="tok-old")

    assert owned is True
    assert overlay_calls == [] and sfx_calls == []
    assert deleted == []
    v = job.assembly_plan["variants"][0]
    assert v["pre_media_overlay_video_path"] == "generative-jobs/j/old_pre_overlay.mp4"
    assert v["pre_sfx_video_path"] == "generative-jobs/j/old_pre_sfx.mp4"


def test_reapply_prep_locks_row_and_deletes_only_after_commit(monkeypatch):
    """F4: the prep's RMW must run under SELECT ... FOR UPDATE and the GCS deletes
    must run AFTER the commit — no network I/O inside the open transaction."""
    _arm_flags(monkeypatch)
    variant = _narrated_variant(
        media_overlays=[dict(_OVERLAYS[0])],
        pre_media_overlay_video_path="generative-jobs/j/old_pre_overlay.mp4",
    )
    job = _make_job(assembly_plan={"variants": [variant]})
    events: list[str] = []

    class _Sess:
        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def get(self, model, pk, **kw):
            events.append(f"get(for_update={kw.get('with_for_update')})")
            return job

        def commit(self):
            events.append("commit")

    monkeypatch.setattr(gb, "_sync_session", lambda: _Sess())
    monkeypatch.setattr("sqlalchemy.orm.attributes.flag_modified", lambda *a, **k: None)
    monkeypatch.setattr(
        "app.storage.delete_object_best_effort", lambda p: events.append(f"delete({p})") or True
    )
    monkeypatch.setattr(gb, "_run_media_overlay_pass", lambda **kw: None, raising=False)

    gb._reapply_persisted_media_overlays_if_any(job_id=JOB_ID, variant_id="narrated")

    assert events[0] == "get(for_update=True)"
    assert events.index("commit") < events.index("delete(generative-jobs/j/old_pre_overlay.mp4)")


# ── _null_and_free_media_snapshots (D16-C) ──────────────────────────────────────


def test_null_and_free_deletes_stale_snapshot_keys(monkeypatch):
    deleted: list[str] = []
    monkeypatch.setattr(
        "app.storage.delete_object_best_effort", lambda p: deleted.append(p) or True
    )
    current = {
        "video_path": "generative-jobs/j/v.mp4",
        "base_video_path": "generative-jobs/j/base.mp4",
        "pre_media_overlay_video_path": "generative-jobs/j/v.mp4_pre_overlay",
        "pre_sfx_video_path": "generative-jobs/j/v.mp4_pre_sfx",
    }
    patch: dict = {}

    gb._null_and_free_media_snapshots(patch, current)

    assert sorted(deleted) == [
        "generative-jobs/j/v.mp4_pre_overlay",
        "generative-jobs/j/v.mp4_pre_sfx",
    ]
    assert patch["pre_media_overlay_video_path"] is None
    assert patch["pre_sfx_video_path"] is None


def test_null_and_free_never_deletes_live_references(monkeypatch):
    """The passes alias the snapshot to video_path when their durable copy fails —
    the keep-set guard must protect it (and base_video_path, and the patch's keys)."""
    deleted: list[str] = []
    monkeypatch.setattr(
        "app.storage.delete_object_best_effort", lambda p: deleted.append(p) or True
    )
    current = {
        "video_path": "generative-jobs/j/v.mp4",
        "base_video_path": "generative-jobs/j/base.mp4",
        "pre_media_overlay_video_path": "generative-jobs/j/v.mp4",  # alias: copy failed
        "pre_sfx_video_path": "generative-jobs/j/base.mp4",  # alias to the base
    }
    patch = {"video_path": "generative-jobs/j/new.mp4"}

    gb._null_and_free_media_snapshots(patch, current)

    assert deleted == []
    assert patch["pre_media_overlay_video_path"] is None
    assert patch["pre_sfx_video_path"] is None


def test_null_and_free_fields_selector_leaves_other_snapshot(monkeypatch):
    """The SFX reapply prep retires ONLY pre_sfx — the overlay snapshot the
    overlay pass just wrote must survive."""
    deleted: list[str] = []
    monkeypatch.setattr(
        "app.storage.delete_object_best_effort", lambda p: deleted.append(p) or True
    )
    current = {
        "video_path": "generative-jobs/j/v.mp4",
        "pre_media_overlay_video_path": "generative-jobs/j/keep_pre_overlay.mp4",
        "pre_sfx_video_path": "generative-jobs/j/retired_pre_sfx.mp4",
    }
    patch: dict = {}

    gb._null_and_free_media_snapshots(patch, current, fields=("pre_sfx_video_path",))

    assert deleted == ["generative-jobs/j/retired_pre_sfx.mp4"]
    assert "pre_media_overlay_video_path" not in patch
    assert patch["pre_sfx_video_path"] is None


def test_null_and_free_swallows_storage_errors(monkeypatch):
    # delete_object_best_effort never raises (storage.py contract) — it reports
    # failure by returning False; the free helper logs and moves on.
    monkeypatch.setattr("app.storage.delete_object_best_effort", lambda _p: False)
    current = {
        "video_path": "generative-jobs/j/v.mp4",
        "pre_media_overlay_video_path": "generative-jobs/j/orphan.mp4",
    }
    patch: dict = {}

    gb._null_and_free_media_snapshots(patch, current)  # must not raise

    assert patch["pre_media_overlay_video_path"] is None
    assert patch["pre_sfx_video_path"] is None


def test_free_helper_skips_foreign_prefix_keys(monkeypatch):
    """Prefix confinement (defense-in-depth): the shared bucket holds curated
    forever-assets — only generative-jobs/* keys may ever be freed."""
    deleted: list[str] = []
    monkeypatch.setattr(
        "app.storage.delete_object_best_effort", lambda p: deleted.append(p) or True
    )
    current = {
        "video_path": "generative-jobs/j/v.mp4",
        "pre_media_overlay_video_path": "music/curated/track_art.png",
        "pre_sfx_video_path": "generative-jobs/j/orphan.mp4",
    }
    patch: dict = {}

    gb._null_and_free_media_snapshots(patch, current)

    assert deleted == ["generative-jobs/j/orphan.mp4"]  # curated key skipped
    assert patch["pre_media_overlay_video_path"] is None
    assert patch["pre_sfx_video_path"] is None


def test_stage_media_snapshot_nulls_collects_without_deleting(monkeypatch):
    """R1-2 split: the staging half nulls the patch fields and RETURNS the
    retired keys — no storage I/O until the caller's write is accepted."""

    def _never(_p):
        raise AssertionError("stage must not delete")

    monkeypatch.setattr("app.storage.delete_object_best_effort", _never)
    current = {
        "video_path": "generative-jobs/j/v.mp4",
        "pre_media_overlay_video_path": "generative-jobs/j/v.mp4_pre_overlay",
        "pre_sfx_video_path": "generative-jobs/j/v.mp4",  # alias — kept out
    }
    patch: dict = {}

    retired = gb._stage_media_snapshot_nulls(patch, current)

    assert retired == ["generative-jobs/j/v.mp4_pre_overlay"]
    assert patch["pre_media_overlay_video_path"] is None
    assert patch["pre_sfx_video_path"] is None


def test_free_retired_media_snapshots_respects_keep_paths(monkeypatch):
    deleted: list[str] = []
    monkeypatch.setattr(
        "app.storage.delete_object_best_effort", lambda p: deleted.append(p) or True
    )
    current = {
        "video_path": "generative-jobs/j/old.mp4",
        "pre_media_overlay_video_path": "generative-jobs/j/new.mp4",  # == the new video
        "pre_sfx_video_path": "generative-jobs/j/retired.mp4",
    }

    gb._free_retired_media_snapshots(current, ("generative-jobs/j/new.mp4", None))

    assert deleted == ["generative-jobs/j/retired.mp4"]


# ── OV-7 status handoff at the seam (integration: real chain + real passes) ────


def test_caption_reburn_with_lanes_real_chain_ends_ready_with_effects(monkeypatch):
    """Drives a caption reburn with persisted overlays+SFX through the REAL
    _reapply_user_media_layers and the REAL _run_media_overlay_pass /
    _run_sfx_pass — only the storage/ffmpeg boundaries are stubbed. Pins the
    OV-7 handoff: deferred "rendering" through both passes, terminal "ready"
    written by the SFX pass onto an effects-bearing video_path."""
    _arm_flags(monkeypatch)
    variant = _narrated_variant(
        media_overlays=[dict(_OVERLAYS[0])],
        sound_effects=[dict(_SFX[0])],
        render_generation_id="tok-1",
    )
    job = _make_job(assembly_plan={"variants": [variant]})
    _patch_job_session(monkeypatch, job)
    seen: dict = {}
    _patch_reburn_io(monkeypatch, seen)
    monkeypatch.setattr("app.storage.copy_object", lambda src, dst: None)
    monkeypatch.setattr("app.storage.signed_get_url", lambda p, **k: f"https://signed/{p}")
    monkeypatch.setattr("app.services.pipeline_trace.record_pipeline_event", lambda *a, **k: None)
    overlay_applies: list[dict] = []
    sfx_applies: list[dict] = []
    monkeypatch.setattr(
        "app.pipeline.media_overlay.apply_media_overlays",
        lambda **kw: overlay_applies.append(kw) or "https://signed/overlaid",
    )
    monkeypatch.setattr(
        "app.pipeline.sound_effects.apply_sound_effects",
        lambda **kw: sfx_applies.append(kw) or "https://signed/with-sfx",
    )

    gb._run_reburn_narrated_captions(JOB_ID, "narrated", render_gen_id="tok-1")

    v = job.assembly_plan["variants"][0]
    assert v["render_status"] == "ready"
    assert v["render_finished_at"]
    new_video = v["video_path"]
    assert "_cap_" in new_video  # the fresh burn key
    # Overlay pass composited onto the clean copy of the new burn, into video_path.
    assert len(overlay_applies) == 1
    assert overlay_applies[0]["base_gcs_path"] == f"{new_video}_pre_overlay"
    assert overlay_applies[0]["output_gcs_path"] == new_video
    # SFX pass remixed on top (outermost layer) and owns the final output_url.
    assert len(sfx_applies) == 1
    assert sfx_applies[0]["base_gcs_path"] == f"{new_video}_pre_sfx"
    assert sfx_applies[0]["output_gcs_path"] == new_video
    assert v["output_url"] == "https://signed/with-sfx"
    assert v["pre_media_overlay_video_path"] == f"{new_video}_pre_overlay"
    assert v["pre_sfx_video_path"] == f"{new_video}_pre_sfx"
