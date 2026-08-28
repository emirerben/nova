"""E1 render-intent guard — every terminal variant write is token-checked.

A render task launched with `render_gen_id` may only land its DB-visible patch
while the variant's persisted `render_generation_id` still equals that token; a
newer editor commit bumps the token, so the older task finishes compute but
DISCARDS its write (D8 queue/supersede). Tasks without a token (legacy per-field
dispatchers) always write — flag-off surfaces unchanged.

House style: no DB, no ffmpeg — fake Job via `_sync_session` monkeypatch, writes
captured through `_update_variant_entry` (mirrors test_generative_timeline_render).
"""

from __future__ import annotations

import contextlib
import types

import pytest
from billiard.exceptions import SoftTimeLimitExceeded

import app.tasks.generative_build as gb

JOB_ID = "12345678-1234-5678-1234-567812345678"

CLIP_PATHS = [
    f"generative-jobs/{JOB_ID}/sources/000_a.mp4",
    f"generative-jobs/{JOB_ID}/sources/001_b.mp4",
]


class _FakeJob:
    def __init__(self, variants):
        self.all_candidates = {"clip_paths": list(CLIP_PATHS)}
        self.assembly_plan = {"variants": list(variants)}
        self.status = "variants_ready"
        self.mode = "generative"


def _variant(gen_id: str | None, **extra) -> dict:
    v = {
        "variant_id": "original_text",
        "rank": 3,
        "text_mode": "agent_text",
        "render_status": "rendering",
        "music_track_id": None,
        "video_path": f"generative-jobs/{JOB_ID}/variant_3_original_text.mp4",
        "output_url": "https://signed/last-good",
        "base_video_path": f"generative-jobs/{JOB_ID}/base_3.mp4",
        "intro_text": "hook",
        "ok": True,
    }
    if gen_id is not None:
        v["render_generation_id"] = gen_id
    v.update(extra)
    return v


def test_nonterminal_cleanup_propagates_celery_soft_timeout(monkeypatch):
    def raise_soft_timeout(_job_id):
        raise SoftTimeLimitExceeded()

    monkeypatch.setattr(gb, "reconcile_video_poster_cleanup_receipts", raise_soft_timeout)

    with pytest.raises(SoftTimeLimitExceeded):
        gb._reconcile_retired_variant_posters(JOB_ID, ["poster.jpg"])


def test_terminal_cleanup_defers_celery_soft_timeout(monkeypatch):
    monkeypatch.setattr(
        gb,
        "_reconcile_retired_variant_posters",
        lambda _job_id, _paths: (_ for _ in ()).throw(SoftTimeLimitExceeded()),
    )

    gb._reconcile_retired_variant_posters_after_terminal_commit(
        JOB_ID,
        ["poster.jpg"],
    )


def _patch_sessions(monkeypatch, job):
    class _Sess:
        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def get(self, model, pk, **kw):
            return job if model is gb.Job else None

        def commit(self):
            pass

    monkeypatch.setattr(gb, "_sync_session", lambda: _Sess())


def _capture_updates(monkeypatch, job) -> list[dict]:
    updates: list[dict] = []

    def _fake_update(
        jid,
        vid,
        patch,
        *,
        expected_render_gen_id=None,
        **_kwargs,
    ):
        match = next(
            (
                (index, variant)
                for index, variant in enumerate(job.assembly_plan["variants"])
                if variant.get("variant_id") == vid
            ),
            None,
        )
        if match is None:
            return False
        index, variant = match
        current = variant.get("render_generation_id")
        if (
            expected_render_gen_id is not None
            and current is not None
            and current != expected_render_gen_id
        ):
            return False
        update = {k: v for k, v in patch.items() if k != "variant_id"}
        job.assembly_plan["variants"][index] = {**variant, **update}
        updates.append(dict(patch))
        return True

    monkeypatch.setattr(gb, "_update_variant_entry", _fake_update, raising=False)
    return updates


# ── _stale_render_discarded unit behavior ─────────────────────────────────────


def test_guard_no_token_never_discards(monkeypatch):
    """Legacy dispatchers (no render_gen_id) keep writing — flag-off unchanged."""
    _patch_sessions(monkeypatch, _FakeJob([_variant("current")]))
    assert gb._stale_render_discarded(JOB_ID, "original_text", None, outcome="x") is False


def test_guard_matching_token_writes(monkeypatch):
    _patch_sessions(monkeypatch, _FakeJob([_variant("tok-1")]))
    assert gb._stale_render_discarded(JOB_ID, "original_text", "tok-1", outcome="x") is False


def test_guard_stale_token_discards(monkeypatch):
    _patch_sessions(monkeypatch, _FakeJob([_variant("tok-2-newer")]))
    assert gb._stale_render_discarded(JOB_ID, "original_text", "tok-1", outcome="x") is True


def test_guard_variant_without_gen_id_writes(monkeypatch):
    """A variant never token-stamped (legacy row) can't be stale-checked — write."""
    _patch_sessions(monkeypatch, _FakeJob([_variant(None)]))
    assert gb._stale_render_discarded(JOB_ID, "original_text", "tok-1", outcome="x") is False


def test_stale_swap_worker_does_not_clear_newer_timeline(monkeypatch):
    timeline = {"slots": [{"clip_index": 0, "duration_s": 1.0}]}
    job = _FakeJob([_variant("tok-new", user_timeline=timeline)])
    _patch_sessions(monkeypatch, job)

    assert (
        gb._clear_user_timeline(
            JOB_ID,
            "original_text",
            expected_render_gen_id="tok-old",
        )
        is False
    )
    assert job.assembly_plan["variants"][0]["user_timeline"] == timeline


def test_current_swap_worker_clears_its_timeline(monkeypatch):
    job = _FakeJob([_variant("tok-current", user_timeline={"slots": []})])
    _patch_sessions(monkeypatch, job)

    assert (
        gb._clear_user_timeline(
            JOB_ID,
            "original_text",
            expected_render_gen_id="tok-current",
        )
        is True
    )
    assert "user_timeline" not in job.assembly_plan["variants"][0]


def test_lock_serialization_requires_real_concurrent_db_sessions():
    """The unit harness fakes `_sync_session`, so it cannot exercise blocking
    SELECT ... FOR UPDATE behavior across two live sessions."""
    pytest.skip("requires the integration DB harness with concurrent sessions")


# ── fast-reburn terminal write: stale discarded, current lands ────────────────


def _arm_reburn(monkeypatch, job, result: dict):
    """Wire _run_regenerate_variant to take the fast-reburn path without IO."""
    _patch_sessions(monkeypatch, job)
    updates = _capture_updates(monkeypatch, job)
    monkeypatch.setattr(gb, "_is_fast_reburn_eligible", lambda *a, **k: True, raising=False)
    monkeypatch.setattr(gb, "_reburn_text_on_base", lambda **kw: dict(result), raising=False)
    overlay_calls: list = []
    sfx_calls: list = []
    monkeypatch.setattr(
        gb,
        "_reapply_persisted_media_overlays_if_any",
        lambda **kw: overlay_calls.append(kw) or False,
        raising=False,
    )
    monkeypatch.setattr(
        gb,
        "_reapply_persisted_sfx_if_any",
        lambda **kw: sfx_calls.append(kw),
        raising=False,
    )
    return updates, overlay_calls, sfx_calls


_READY_RESULT = {"render_status": "ready", "ok": True, "output_url": "https://signed/new"}


def _sfx_placement() -> dict:
    return {
        "id": "sfx-1",
        "sound_effect_id": None,
        "src_gcs_path": "sound-effects/pop/audio.mp3",
        "at_s": 1.0,
        "gain": 0.8,
        "trim_start_s": None,
        "trim_end_s": None,
        "duration_s": 0.5,
        "label": "Pop",
    }


def _media_overlay_card() -> dict:
    return {
        "id": "ov-1",
        "kind": "image",
        "src_gcs_path": "users/u123/plan/item/overlays/card.png",
        "position": "center",
        "scale": 0.35,
        "start_s": 0.0,
        "end_s": 2.0,
        "z": 0,
    }


def _arm_direct_passes(monkeypatch, job):
    _patch_sessions(monkeypatch, job)
    monkeypatch.setattr("sqlalchemy.orm.attributes.flag_modified", lambda *a, **k: None)
    monkeypatch.setattr("app.storage.copy_object", lambda *a, **k: None)
    monkeypatch.setattr("app.storage.object_exists", lambda _path: False)
    monkeypatch.setattr("app.storage.signed_get_url", lambda path, **kw: f"https://signed/{path}")
    monkeypatch.setattr("app.services.pipeline_trace.record_pipeline_event", lambda *a, **k: None)
    monkeypatch.setattr(
        "app.pipeline.sound_effects.apply_sound_effects",
        lambda **kw: "https://signed/sfx-new",
    )
    monkeypatch.setattr(
        "app.pipeline.media_overlay.apply_media_overlays",
        lambda **kw: "https://signed/overlay-new",
    )
    monkeypatch.setattr(gb.settings, "sound_effects_enabled", False, raising=False)


def test_stale_sfx_pass_terminal_write_discarded(monkeypatch):
    job = _FakeJob([_variant("tok-new")])
    _arm_direct_passes(monkeypatch, job)

    gb._run_sfx_pass(
        job_id=JOB_ID,
        variant_id="original_text",
        sfx_raw=[_sfx_placement()],
        expected_render_gen_id="tok-old",
    )

    v = job.assembly_plan["variants"][0]
    assert v["output_url"] == "https://signed/last-good"
    assert "sound_effects" not in v


def test_current_sfx_pass_terminal_write_lands(monkeypatch):
    job = _FakeJob([_variant("tok-cur")])
    _arm_direct_passes(monkeypatch, job)

    gb._run_sfx_pass(
        job_id=JOB_ID,
        variant_id="original_text",
        sfx_raw=[_sfx_placement()],
        expected_render_gen_id="tok-cur",
    )

    v = job.assembly_plan["variants"][0]
    assert v["output_url"] == "https://signed/sfx-new"
    assert v["sound_effects"][0]["id"] == "sfx-1"
    assert v["render_status"] == "ready"


def test_tokenless_sfx_pass_terminal_write_lands(monkeypatch):
    job = _FakeJob([_variant("tok-new")])
    _arm_direct_passes(monkeypatch, job)

    gb._run_sfx_pass(
        job_id=JOB_ID,
        variant_id="original_text",
        sfx_raw=[_sfx_placement()],
        expected_render_gen_id=None,
    )

    assert job.assembly_plan["variants"][0]["output_url"] == "https://signed/sfx-new"


def test_sfx_terminal_cleanup_soft_timeout_keeps_accepted_ready_variant(monkeypatch):
    video = f"generative-jobs/{JOB_ID}/variant_3_original_text.mp4"
    old_poster = f"{video}.poster.backfill-11111111-1111-4111-8111-111111111111.jpg"
    new_poster = f"{video}.poster.jpg"
    job = _FakeJob([_variant("tok-cur", video_path=video, poster_path=old_poster)])
    _arm_direct_passes(monkeypatch, job)
    monkeypatch.setattr(gb, "generate_and_upload_from_gcs", lambda *_a, **_kw: new_poster)
    monkeypatch.setattr(
        gb,
        "reconcile_video_poster_cleanup_receipts",
        lambda _job_id: (_ for _ in ()).throw(SoftTimeLimitExceeded()),
    )

    gb._run_sfx_pass(
        job_id=JOB_ID,
        variant_id="original_text",
        sfx_raw=[_sfx_placement()],
        expected_render_gen_id="tok-cur",
    )

    persisted = job.assembly_plan["variants"][0]
    assert persisted["render_status"] == "ready"
    assert persisted["poster_path"] == new_poster
    assert job.assembly_plan[gb.VIDEO_POSTER_BACKFILL_CLEANUP_FIELD] == [
        {"old_path": old_poster, "replacement_path": new_poster}
    ]


def test_stale_media_overlay_pass_terminal_write_discarded(monkeypatch):
    job = _FakeJob([_variant("tok-new")])
    _arm_direct_passes(monkeypatch, job)

    gb._run_media_overlay_pass(
        job_id=JOB_ID,
        variant_id="original_text",
        overlays_raw=[_media_overlay_card()],
        expected_render_gen_id="tok-old",
    )

    v = job.assembly_plan["variants"][0]
    assert v["output_url"] == "https://signed/last-good"
    assert "media_overlays" not in v


def test_current_media_overlay_pass_terminal_write_lands(monkeypatch):
    job = _FakeJob([_variant("tok-cur")])
    _arm_direct_passes(monkeypatch, job)

    gb._run_media_overlay_pass(
        job_id=JOB_ID,
        variant_id="original_text",
        overlays_raw=[_media_overlay_card()],
        expected_render_gen_id="tok-cur",
    )

    v = job.assembly_plan["variants"][0]
    assert v["output_url"] == "https://signed/overlay-new"
    assert v["media_overlays"][0]["id"] == "ov-1"
    assert v["render_status"] == "ready"


def test_tokenless_media_overlay_pass_terminal_write_lands(monkeypatch):
    job = _FakeJob([_variant("tok-new")])
    _arm_direct_passes(monkeypatch, job)

    gb._run_media_overlay_pass(
        job_id=JOB_ID,
        variant_id="original_text",
        overlays_raw=[_media_overlay_card()],
        expected_render_gen_id=None,
    )

    assert job.assembly_plan["variants"][0]["output_url"] == "https://signed/overlay-new"


def test_media_overlay_terminal_cleanup_soft_timeout_keeps_accepted_ready_variant(
    monkeypatch,
):
    video = f"generative-jobs/{JOB_ID}/variant_3_original_text.mp4"
    old_poster = f"{video}.poster.backfill-11111111-1111-4111-8111-111111111111.jpg"
    new_poster = f"{video}.poster.jpg"
    job = _FakeJob([_variant("tok-cur", video_path=video, poster_path=old_poster)])
    _arm_direct_passes(monkeypatch, job)
    monkeypatch.setattr(gb, "generate_and_upload_from_gcs", lambda *_a, **_kw: new_poster)
    monkeypatch.setattr(
        gb,
        "reconcile_video_poster_cleanup_receipts",
        lambda _job_id: (_ for _ in ()).throw(SoftTimeLimitExceeded()),
    )

    gb._run_media_overlay_pass(
        job_id=JOB_ID,
        variant_id="original_text",
        overlays_raw=[_media_overlay_card()],
        expected_render_gen_id="tok-cur",
    )

    persisted = job.assembly_plan["variants"][0]
    assert persisted["render_status"] == "ready"
    assert persisted["poster_path"] == new_poster
    assert job.assembly_plan[gb.VIDEO_POSTER_BACKFILL_CLEANUP_FIELD] == [
        {"old_path": old_poster, "replacement_path": new_poster}
    ]


def test_stale_task_terminal_write_discarded(monkeypatch):
    """Task launched with tok-old; a newer commit bumped the variant to tok-new →
    the reburn completes but neither the ready patch nor the SFX hook lands."""
    job = _FakeJob([_variant("tok-new")])
    updates, overlay_calls, sfx_calls = _arm_reburn(monkeypatch, job, _READY_RESULT)

    gb._run_regenerate_variant(JOB_ID, "original_text", None, None, False, render_gen_id="tok-old")

    # Only the non-terminal "rendering" marker may have been written.
    assert all(u.get("render_status") == "rendering" for u in updates)
    assert not any(u.get("render_status") == "ready" for u in updates)
    assert overlay_calls == []
    assert sfx_calls == []


def test_stale_fast_reburn_deletes_only_its_generation_scoped_outputs(monkeypatch):
    """A loser may upload after the winner, but immutable keys keep winner bytes safe."""
    stale_result = {
        **_READY_RESULT,
        "video_path": f"generative-jobs/{JOB_ID}/variant_tokold.mp4",
        "visual_blocks_base_path": f"generative-jobs/{JOB_ID}/visual-blocks/tokold.mp4",
        "motion_base_path": f"generative-jobs/{JOB_ID}/motion/tokold.mp4",
    }
    job = _FakeJob([_variant("tok-old")])
    _arm_reburn(monkeypatch, job, stale_result)

    def _finish_after_supersede(**_kwargs):
        job.assembly_plan["variants"][0]["render_generation_id"] = "tok-new"
        return dict(stale_result)

    monkeypatch.setattr(gb, "_reburn_text_on_base", _finish_after_supersede)
    deleted: list[str] = []
    monkeypatch.setattr(
        "app.storage.delete_object_best_effort",
        lambda path: deleted.append(path) or True,
    )

    gb._run_regenerate_variant(
        JOB_ID,
        "original_text",
        None,
        None,
        False,
        render_gen_id="tok-old",
    )

    assert sorted(deleted) == sorted(
        [
            stale_result["video_path"],
            stale_result["visual_blocks_base_path"],
            stale_result["motion_base_path"],
        ]
    )
    assert job.assembly_plan["variants"][0]["video_path"].endswith("variant_3_original_text.mp4")


def test_fast_reburn_exception_cleans_tracked_generation_storage(monkeypatch):
    job = _FakeJob([_variant("tok-current")])
    _patch_sessions(monkeypatch, job)
    _capture_updates(monkeypatch, job)
    monkeypatch.setattr(gb, "_is_fast_reburn_eligible", lambda *a, **k: True)
    owned_path = f"generative-jobs/{JOB_ID}/visual-blocks/owned.mp4"

    def _failing_reburn(**kwargs):
        kwargs["created_storage_paths"].append(owned_path)
        raise RuntimeError("burn failed")

    monkeypatch.setattr(gb, "_reburn_text_on_base", _failing_reburn)
    deleted: list[str] = []
    monkeypatch.setattr(
        "app.storage.delete_object_best_effort",
        lambda path: deleted.append(path) or True,
    )

    with pytest.raises(RuntimeError, match="burn failed"):
        gb._run_regenerate_variant(
            JOB_ID,
            "original_text",
            None,
            None,
            False,
            render_gen_id="tok-current",
        )

    assert deleted == [owned_path]


def test_accepted_full_render_retires_previous_generation_outputs(monkeypatch):
    previous = {
        "video_path": f"generative-jobs/{JOB_ID}/variant_old.mp4",
        "base_video_path": f"generative-jobs/{JOB_ID}/base_old.mp4",
    }
    replacement = {
        "video_path": f"generative-jobs/{JOB_ID}/variant_new.mp4",
        "base_video_path": f"generative-jobs/{JOB_ID}/base_new.mp4",
    }
    deleted: list[str] = []
    monkeypatch.setattr(
        "app.storage.delete_object_best_effort",
        lambda path: deleted.append(path) or True,
    )
    gb._free_retired_generation_outputs(previous, replacement, job_id=JOB_ID)

    assert sorted(deleted) == sorted(previous.values())


def test_accepted_full_render_retires_uuid_backfill_posters(monkeypatch):
    old_video = f"generative-jobs/{JOB_ID}/variant_old.mp4"
    old_poster = f"{old_video}.poster.backfill-11111111-1111-4111-8111-111111111111.jpg"
    previous = {"video_path": old_video, "poster_path": old_poster}
    replacement = {"video_path": f"generative-jobs/{JOB_ID}/variant_new.mp4"}
    deleted: list[str] = []
    monkeypatch.setattr(
        "app.storage.delete_object_best_effort",
        lambda path: deleted.append(path) or True,
    )
    gb._free_retired_generation_outputs(previous, replacement, job_id=JOB_ID)

    assert deleted == [old_video]


def test_unreferenced_poster_cleanup_accepts_uuid_backfill_key(monkeypatch):
    poster = (
        f"generative-jobs/{JOB_ID}/variant.mp4.poster.backfill-"
        "11111111-1111-4111-8111-111111111111.jpg"
    )
    deleted: list[str] = []
    monkeypatch.setattr(
        gb,
        "_delete_cancelled_job_objects",
        lambda job_id, paths: deleted.extend(paths),
    )

    gb._delete_generated_poster_objects_if_unreferenced(
        JOB_ID,
        [poster],
        locked_job=types.SimpleNamespace(assembly_plan={"variants": []}),
    )

    assert deleted == [poster]


def test_same_video_poster_swap_retires_uuid_backfill_reference(monkeypatch):
    video = f"generative-jobs/{JOB_ID}/variant.mp4"
    old_poster = f"{video}.poster.backfill-11111111-1111-4111-8111-111111111111.jpg"
    new_poster = f"{video}.poster.jpg"
    job = _FakeJob([_variant("tok", video_path=video, poster_path=old_poster)])
    _patch_sessions(monkeypatch, job)
    journaled: list[str] = []
    monkeypatch.setattr(
        gb,
        "_reconcile_retired_variant_posters",
        lambda _job_id, paths: journaled.extend(paths),
    )

    assert gb._update_variant_entry(JOB_ID, "original_text", {"poster_path": new_poster})

    assert job.assembly_plan["variants"][0]["video_path"] == video
    assert job.assembly_plan["variants"][0]["poster_path"] == new_poster
    assert journaled == [old_poster]
    assert job.assembly_plan[gb.VIDEO_POSTER_BACKFILL_CLEANUP_FIELD] == [
        {"old_path": old_poster, "replacement_path": new_poster}
    ]


def test_full_render_upsert_journals_uuid_poster_before_reconcile(monkeypatch):
    old_video = f"generative-jobs/{JOB_ID}/variant_old.mp4"
    old_poster = f"{old_video}.poster.backfill-11111111-1111-4111-8111-111111111111.jpg"
    new_video = f"generative-jobs/{JOB_ID}/variant_new.mp4"
    new_poster = f"{new_video}.poster.jpg"
    job = _FakeJob([_variant("old-render", video_path=old_video, poster_path=old_poster)])
    _patch_sessions(monkeypatch, job)
    reconciled: list[tuple[str, list[str]]] = []
    monkeypatch.setattr(
        gb,
        "_reconcile_retired_variant_posters",
        lambda job_id, paths: reconciled.append((job_id, list(paths))),
    )

    replacement = _variant(
        "new-render",
        video_path=new_video,
        poster_path=new_poster,
        render_status="ready",
    )
    assert gb._upsert_variant_entry(JOB_ID, replacement)

    assert job.assembly_plan["variants"] == [replacement]
    assert job.assembly_plan[gb.VIDEO_POSTER_BACKFILL_CLEANUP_FIELD] == [
        {"old_path": old_poster, "replacement_path": new_poster}
    ]
    assert reconciled == [(JOB_ID, [old_poster])]


def test_full_render_upsert_cleanup_failure_keeps_durable_receipt(monkeypatch):
    old_video = f"generative-jobs/{JOB_ID}/variant_old.mp4"
    old_poster = f"{old_video}.poster.backfill-11111111-1111-4111-8111-111111111111.jpg"
    new_video = f"generative-jobs/{JOB_ID}/variant_new.mp4"
    new_poster = f"{new_video}.poster.jpg"
    job = _FakeJob([_variant("old-render", video_path=old_video, poster_path=old_poster)])
    _patch_sessions(monkeypatch, job)

    def _fail_cleanup(_job_id):
        raise RuntimeError("storage unavailable")

    monkeypatch.setattr(gb, "reconcile_video_poster_cleanup_receipts", _fail_cleanup)
    replacement = _variant(
        "new-render",
        video_path=new_video,
        poster_path=new_poster,
        render_status="ready",
    )

    assert gb._upsert_variant_entry(JOB_ID, replacement)

    assert job.assembly_plan["variants"] == [replacement]
    assert job.assembly_plan[gb.VIDEO_POSTER_BACKFILL_CLEANUP_FIELD] == [
        {"old_path": old_poster, "replacement_path": new_poster}
    ]


def test_full_render_upsert_cleanup_soft_timeout_propagates_after_commit(monkeypatch):
    old_video = f"generative-jobs/{JOB_ID}/variant_old.mp4"
    old_poster = f"{old_video}.poster.backfill-11111111-1111-4111-8111-111111111111.jpg"
    new_video = f"generative-jobs/{JOB_ID}/variant_new.mp4"
    new_poster = f"{new_video}.poster.jpg"
    job = _FakeJob([_variant("old-render", video_path=old_video, poster_path=old_poster)])
    _patch_sessions(monkeypatch, job)
    monkeypatch.setattr(
        gb,
        "reconcile_video_poster_cleanup_receipts",
        lambda _job_id: (_ for _ in ()).throw(SoftTimeLimitExceeded()),
    )
    replacement = _variant(
        "new-render",
        video_path=new_video,
        poster_path=new_poster,
        render_status="ready",
    )

    with pytest.raises(SoftTimeLimitExceeded):
        gb._upsert_variant_entry(JOB_ID, replacement)

    # The intermediate write and its receipt are durable, but the task must
    # still reach its outer timeout handler before Celery's hard kill.
    assert job.assembly_plan["variants"] == [replacement]
    assert job.assembly_plan[gb.VIDEO_POSTER_BACKFILL_CLEANUP_FIELD] == [
        {"old_path": old_poster, "replacement_path": new_poster}
    ]


def test_pending_upsert_preserves_every_recoverable_asset_until_replacement(monkeypatch):
    video = f"generative-jobs/{JOB_ID}/accepted.mp4"
    poster = f"{video}.poster.backfill-11111111-1111-4111-8111-111111111111.jpg"
    assets = {
        field: f"generative-jobs/{JOB_ID}/{field}.bin" for field in gb._PENDING_VARIANT_ASSET_FIELDS
    }
    assets.update({"video_path": video, "poster_path": poster})
    job = _FakeJob([_variant("old-generation", render_status="ready", **assets)])
    _patch_sessions(monkeypatch, job)

    assert gb._upsert_variant_entry(
        JOB_ID,
        {
            "variant_id": "original_text",
            "render_generation_id": "new-generation",
            "render_status": "pending",
            "ok": False,
        },
    )

    persisted = job.assembly_plan["variants"][0]
    assert persisted["render_status"] == "pending"
    assert persisted["render_generation_id"] == "new-generation"
    assert {field: persisted[field] for field in assets} == assets
    assert gb.VIDEO_POSTER_BACKFILL_CLEANUP_FIELD not in job.assembly_plan


def test_ambiguous_commit_confirms_exact_persisted_patch_before_reraising(monkeypatch):
    job = _FakeJob([_variant("tok-current")])
    calls = 0

    class _Sess:
        def __enter__(self):
            return self

        def __exit__(self, *args):
            return False

        def get(self, model, pk, **kwargs):
            return job if model is gb.Job else None

        def commit(self):
            nonlocal calls
            calls += 1
            if calls == 1:
                # Model PostgreSQL accepting COMMIT before Celery delivers the
                # soft-limit signal to the client.
                raise SoftTimeLimitExceeded()

    monkeypatch.setattr(gb, "_sync_session", lambda: _Sess())
    monkeypatch.setattr(gb, "_attach_variant_posters", lambda patch, **_kw: (dict(patch), []))
    accepted = {"accepted": False}
    patch = {"video_path": f"generative-jobs/{JOB_ID}/new.mp4", "render_status": "rendering"}

    with pytest.raises(SoftTimeLimitExceeded):
        gb._update_variant_entry(
            JOB_ID,
            "original_text",
            patch,
            expected_render_gen_id="tok-current",
            accepted_state=accepted,
        )

    assert accepted["accepted"] is True
    assert job.assembly_plan["variants"][0]["video_path"] == patch["video_path"]


def test_ambiguous_commit_does_not_accept_patch_absent_from_fresh_read(monkeypatch):
    staged = _FakeJob([_variant("tok-current")])
    persisted = _FakeJob([_variant("tok-current")])
    sessions = 0

    class _Sess:
        def __init__(self, job, *, interrupted=False):
            self.job = job
            self.interrupted = interrupted

        def __enter__(self):
            return self

        def __exit__(self, *args):
            return False

        def get(self, model, pk, **kwargs):
            return self.job if model is gb.Job else None

        def commit(self):
            if self.interrupted:
                raise SoftTimeLimitExceeded()

    def _session():
        nonlocal sessions
        sessions += 1
        return _Sess(staged, interrupted=True) if sessions == 1 else _Sess(persisted)

    monkeypatch.setattr(gb, "_sync_session", _session)
    monkeypatch.setattr(gb, "_attach_variant_posters", lambda patch, **_kw: (dict(patch), []))
    accepted = {"accepted": False}

    with pytest.raises(SoftTimeLimitExceeded):
        gb._update_variant_entry(
            JOB_ID,
            "original_text",
            {"video_path": f"generative-jobs/{JOB_ID}/never-committed.mp4"},
            expected_render_gen_id="tok-current",
            accepted_state=accepted,
        )

    assert accepted["accepted"] is False
    assert persisted.assembly_plan["variants"][0]["output_url"] == "https://signed/last-good"


def test_fast_rerender_auto_poster_retires_uuid_backfill_reference(monkeypatch):
    old_video = f"generative-jobs/{JOB_ID}/variant_old.mp4"
    new_video = f"generative-jobs/{JOB_ID}/variant_fast.mp4"
    old_poster = f"{old_video}.poster.backfill-11111111-1111-4111-8111-111111111111.jpg"
    new_poster = f"{new_video}.poster.jpg"
    job = _FakeJob([_variant("tok", video_path=old_video, poster_path=old_poster)])
    _patch_sessions(monkeypatch, job)
    monkeypatch.setattr(gb, "generate_and_upload_from_gcs", lambda *_a, **_kw: new_poster)
    journaled: list[str] = []
    monkeypatch.setattr(
        gb,
        "_reconcile_retired_variant_posters",
        lambda _job_id, paths: journaled.extend(paths),
    )

    assert gb._update_variant_entry(JOB_ID, "original_text", {"video_path": new_video})

    persisted = job.assembly_plan["variants"][0]
    assert persisted["video_path"] == new_video
    assert persisted["poster_path"] == new_poster
    assert journaled == [old_poster]
    assert job.assembly_plan[gb.VIDEO_POSTER_BACKFILL_CLEANUP_FIELD] == [
        {"old_path": old_poster, "replacement_path": new_poster}
    ]


def test_finalize_journals_backfill_poster_that_lands_after_render_upsert(monkeypatch):
    """A fail-open render can publish no poster, then race the backfill before finalize."""
    video = f"generative-jobs/{JOB_ID}/variant_new.mp4"
    backfill_poster = f"{video}.poster.backfill-11111111-1111-4111-8111-111111111111.jpg"
    job = _FakeJob(
        [
            _variant(
                "same-render",
                video_path=video,
                poster_path=backfill_poster,
                render_status="ready",
            )
        ]
    )
    _patch_sessions(monkeypatch, job)
    journaled: list[str] = []
    monkeypatch.setattr(
        gb,
        "_reconcile_retired_variant_posters",
        lambda _job_id, paths: journaled.extend(paths),
    )

    assert gb._set_status(
        JOB_ID,
        "variants_ready",
        extra_plan={
            "variants": [
                {
                    **job.assembly_plan["variants"][0],
                    "poster_path": None,
                }
            ]
        },
        merge_finalized_variants=True,
    )

    assert job.assembly_plan["variants"][0]["poster_path"] is None
    assert job.assembly_plan[gb.VIDEO_POSTER_BACKFILL_CLEANUP_FIELD] == [
        {"old_path": backfill_poster, "replacement_path": video}
    ]
    assert journaled == [backfill_poster]


def test_terminal_finalize_cleanup_soft_timeout_keeps_ready_state(monkeypatch):
    video = f"generative-jobs/{JOB_ID}/variant_new.mp4"
    old_poster = f"{video}.poster.backfill-11111111-1111-4111-8111-111111111111.jpg"
    job = _FakeJob(
        [_variant("same-render", video_path=video, poster_path=old_poster, render_status="ready")]
    )
    _patch_sessions(monkeypatch, job)
    monkeypatch.setattr(
        gb,
        "reconcile_video_poster_cleanup_receipts",
        lambda _job_id: (_ for _ in ()).throw(SoftTimeLimitExceeded()),
    )

    assert gb._set_status(
        JOB_ID,
        "variants_ready",
        extra_plan={
            "variants": [
                {
                    **job.assembly_plan["variants"][0],
                    "poster_path": None,
                }
            ]
        },
        merge_finalized_variants=True,
    )

    assert job.status == "variants_ready"
    assert job.assembly_plan["variants"][0]["poster_path"] is None
    assert job.assembly_plan[gb.VIDEO_POSTER_BACKFILL_CLEANUP_FIELD] == [
        {"old_path": old_poster, "replacement_path": video}
    ]


def test_finalize_journals_every_displaced_duplicate_variant_poster(monkeypatch):
    old_videos = [f"generative-jobs/{JOB_ID}/duplicate-{index}.mp4" for index in range(2)]
    old_posters = [
        f"{video}.poster.backfill-{token}.jpg"
        for video, token in zip(
            old_videos,
            (
                "11111111-1111-4111-8111-111111111111",
                "22222222-2222-4222-8222-222222222222",
            ),
            strict=True,
        )
    ]
    live_variants = [
        _variant(
            "same-render",
            video_path=video,
            poster_path=poster,
            render_status="ready",
        )
        for video, poster in zip(old_videos, old_posters, strict=True)
    ]
    job = _FakeJob(live_variants)
    _patch_sessions(monkeypatch, job)
    journaled: list[str] = []
    monkeypatch.setattr(
        gb,
        "_reconcile_retired_variant_posters",
        lambda _job_id, paths: journaled.extend(paths),
    )
    replacement_video = f"generative-jobs/{JOB_ID}/final.mp4"
    replacement_poster = f"{replacement_video}.poster.jpg"

    assert gb._set_status(
        JOB_ID,
        "variants_ready",
        extra_plan={
            "variants": [
                _variant(
                    "same-render",
                    video_path=replacement_video,
                    poster_path=replacement_poster,
                    render_status="ready",
                )
            ]
        },
        merge_finalized_variants=True,
    )

    assert job.assembly_plan[gb.VIDEO_POSTER_BACKFILL_CLEANUP_FIELD] == [
        {"old_path": old_posters[0], "replacement_path": replacement_poster},
        {"old_path": old_posters[1], "replacement_path": replacement_poster},
    ]
    assert journaled == old_posters


def test_current_task_terminal_write_lands(monkeypatch):
    """Same run with the CURRENT token → ready patch + layer reapply hooks land."""
    job = _FakeJob([_variant("tok-current")])
    updates, overlay_calls, sfx_calls = _arm_reburn(monkeypatch, job, _READY_RESULT)

    gb._run_regenerate_variant(
        JOB_ID, "original_text", None, None, False, render_gen_id="tok-current"
    )

    ready = [u for u in updates if u.get("render_status") == "ready"]
    assert len(ready) == 1
    assert ready[0]["output_url"] == "https://signed/new"
    assert len(overlay_calls) == 1
    assert len(sfx_calls) == 1


def test_tokenless_task_terminal_write_lands(monkeypatch):
    """Regression rule: a legacy task (no token) writes even on a stamped variant."""
    job = _FakeJob([_variant("tok-any")])
    updates, _, _ = _arm_reburn(monkeypatch, job, _READY_RESULT)

    gb._run_regenerate_variant(JOB_ID, "original_text", None, None, False, render_gen_id=None)

    assert any(u.get("render_status") == "ready" for u in updates)


def test_reburn_reapplies_persisted_media_overlays_before_sfx(monkeypatch):
    overlays = [_media_overlay_card()]
    job = _FakeJob([_variant("tok-cur", media_overlays=overlays, sound_effects=[_sfx_placement()])])
    _patch_sessions(monkeypatch, job)
    updates = _capture_updates(monkeypatch, job)
    monkeypatch.setattr(gb, "_is_fast_reburn_eligible", lambda *a, **k: True, raising=False)
    monkeypatch.setattr(gb, "_reburn_text_on_base", lambda **kw: dict(_READY_RESULT), raising=False)
    calls: list[str] = []
    monkeypatch.setattr(
        gb,
        "_reapply_persisted_media_overlays_if_any",
        lambda **kw: calls.append("overlay") or True,
        raising=False,
    )
    monkeypatch.setattr(
        gb,
        "_reapply_persisted_sfx_if_any",
        lambda **kw: calls.append("sfx"),
        raising=False,
    )

    gb._run_regenerate_variant(JOB_ID, "original_text", None, None, False, render_gen_id="tok-cur")

    assert any(u.get("render_status") == "ready" for u in updates)
    assert calls == ["overlay"]


def test_reburn_falls_back_to_sfx_when_no_persisted_media_overlays(monkeypatch):
    job = _FakeJob([_variant("tok-cur", sound_effects=[_sfx_placement()])])
    _patch_sessions(monkeypatch, job)
    _capture_updates(monkeypatch, job)
    monkeypatch.setattr(gb, "_is_fast_reburn_eligible", lambda *a, **k: True, raising=False)
    monkeypatch.setattr(gb, "_reburn_text_on_base", lambda **kw: dict(_READY_RESULT), raising=False)
    calls: list[str] = []
    monkeypatch.setattr(
        gb,
        "_reapply_persisted_media_overlays_if_any",
        lambda **kw: calls.append("overlay") or False,
        raising=False,
    )
    monkeypatch.setattr(
        gb,
        "_reapply_persisted_sfx_if_any",
        lambda **kw: calls.append("sfx"),
        raising=False,
    )

    gb._run_regenerate_variant(JOB_ID, "original_text", None, None, False, render_gen_id="tok-cur")

    assert calls == ["overlay", "sfx"]


# ── failure terminal write (task exception handler) ───────────────────────────


def _arm_failure(monkeypatch, job):
    import app.services.pipeline_trace as pt

    _patch_sessions(monkeypatch, job)
    updates = _capture_updates(monkeypatch, job)
    monkeypatch.setattr(pt, "pipeline_trace_for", lambda job_id: contextlib.nullcontext())

    def _boom(*a, **k):
        raise ValueError("render exploded")

    monkeypatch.setattr(gb, "_run_regenerate_variant", _boom, raising=False)
    return updates


def test_stale_task_failure_write_discarded(monkeypatch):
    """A superseded task's exception must not flip render_status to failed —
    the newer commit's task owns the terminal state now."""
    job = _FakeJob([_variant("tok-new")])
    updates = _arm_failure(monkeypatch, job)

    gb.regenerate_generative_variant.run(JOB_ID, "original_text", render_gen_id="tok-old")

    assert not any(u.get("render_status") == "failed" for u in updates)


def test_current_task_failure_write_lands(monkeypatch):
    job = _FakeJob([_variant("tok-cur")])
    updates = _arm_failure(monkeypatch, job)

    gb.regenerate_generative_variant.run(JOB_ID, "original_text", render_gen_id="tok-cur")

    failed = [u for u in updates if u.get("render_status") == "failed"]
    assert len(failed) == 1
    assert failed[0]["ok"] is False


# ── full re-render terminal write is guarded too ──────────────────────────────


def test_full_render_success_write_guarded(monkeypatch):
    """The non-reburn (full re-assembly) terminal branch checks the token as well:
    stale → discard, no SFX reapply."""
    # Force the full path by making fast-reburn ineligible.
    job = _FakeJob([_variant("tok-new", user_timeline=None)])
    _patch_sessions(monkeypatch, job)
    updates = _capture_updates(monkeypatch, job)
    monkeypatch.setattr(gb, "_is_fast_reburn_eligible", lambda *a, **k: False, raising=False)
    sfx_calls: list = []
    monkeypatch.setattr(
        gb, "_reapply_persisted_sfx_if_any", lambda **kw: sfx_calls.append(kw), raising=False
    )
    monkeypatch.setattr(
        gb,
        "_render_generative_variant",
        lambda **kw: dict(_READY_RESULT),
        raising=False,
    )
    monkeypatch.setattr(gb, "_ingest_clips", lambda *a, **k: _fake_ingest(), raising=False)
    monkeypatch.setattr(gb, "_resolve_narrative_order", lambda *a, **k: None, raising=False)
    monkeypatch.setattr(gb, "_run_text_agents", lambda *a, **k: (None, None), raising=False)

    gb._run_regenerate_variant(JOB_ID, "original_text", None, None, False, render_gen_id="tok-old")

    assert not any(u.get("render_status") == "ready" for u in updates)
    assert sfx_calls == []


def _arm_full_render(monkeypatch, job, render_result: dict):
    _patch_sessions(monkeypatch, job)
    updates = _capture_updates(monkeypatch, job)
    monkeypatch.setattr(gb, "_is_fast_reburn_eligible", lambda *a, **k: False, raising=False)
    monkeypatch.setattr(
        gb,
        "_render_generative_variant",
        lambda **kw: dict(render_result),
        raising=False,
    )
    monkeypatch.setattr(gb, "_ingest_clips", lambda *a, **k: _fake_ingest(), raising=False)
    monkeypatch.setattr(gb, "_resolve_narrative_order", lambda *a, **k: None, raising=False)
    monkeypatch.setattr(gb, "_run_text_agents", lambda *a, **k: (None, None), raising=False)
    monkeypatch.setattr(gb, "_reapply_user_media_layers", lambda **kw: None, raising=False)
    return updates


def test_rejected_full_render_cleans_unscoped_derived_caches(monkeypatch):
    job = _FakeJob(
        [
            _variant(
                "tok-old",
                text_elements=[{"id": "t1"}],
                text_elements_user_edited=True,
            )
        ]
    )
    render_result = {
        **_READY_RESULT,
        "video_path": f"generative-jobs/{JOB_ID}/variant_tokold.mp4",
        "base_video_path": f"generative-jobs/{JOB_ID}/base_tokold.mp4",
    }
    _arm_full_render(monkeypatch, job, render_result)
    visual_cache = f"generative-jobs/{JOB_ID}/visual-blocks/{'a' * 32}.mp4"
    motion_cache = f"generative-jobs/{JOB_ID}/motion/{'b' * 32}.mp4"

    def _reburn(**kwargs):
        kwargs["created_storage_paths"].extend([visual_cache, motion_cache])
        job.assembly_plan["variants"][0]["render_generation_id"] = "tok-new"
        return {
            "video_path": f"generative-jobs/{JOB_ID}/reburn_tokold.mp4",
            "visual_blocks_base_path": visual_cache,
            "motion_base_path": motion_cache,
        }

    monkeypatch.setattr(gb, "_reburn_text_on_base", _reburn, raising=False)
    deleted: list[str] = []
    monkeypatch.setattr(
        "app.storage.delete_object_best_effort",
        lambda path: deleted.append(path) or True,
    )

    gb._run_regenerate_variant(
        JOB_ID,
        "original_text",
        None,
        None,
        False,
        render_gen_id="tok-old",
    )

    assert visual_cache in deleted
    assert motion_cache in deleted


def test_post_reburn_exception_cleans_original_full_render_objects(monkeypatch):
    job = _FakeJob(
        [
            _variant(
                "tok-current",
                text_elements=[{"id": "t1"}],
                text_elements_user_edited=True,
            )
        ]
    )
    render_result = {
        **_READY_RESULT,
        "video_path": f"generative-jobs/{JOB_ID}/variant_tokcurrent.mp4",
        "base_video_path": f"generative-jobs/{JOB_ID}/base_tokcurrent.mp4",
    }
    _arm_full_render(monkeypatch, job, render_result)
    derived_cache = f"generative-jobs/{JOB_ID}/visual-blocks/{'c' * 32}.mp4"

    def _reburn(**kwargs):
        kwargs["created_storage_paths"].append(derived_cache)
        raise RuntimeError("post-render reburn failed")

    monkeypatch.setattr(gb, "_reburn_text_on_base", _reburn, raising=False)
    deleted: list[str] = []
    monkeypatch.setattr(
        "app.storage.delete_object_best_effort",
        lambda path: deleted.append(path) or True,
    )

    with pytest.raises(RuntimeError, match="post-render reburn failed"):
        gb._run_regenerate_variant(
            JOB_ID,
            "original_text",
            None,
            None,
            False,
            render_gen_id="tok-current",
        )

    assert render_result["video_path"] in deleted
    assert render_result["base_video_path"] in deleted
    assert derived_cache in deleted


def test_rejected_failed_full_render_cleans_generation_objects(monkeypatch):
    job = _FakeJob([_variant("tok-old")])
    render_result = {
        "ok": False,
        "render_status": "failed",
        "error": "encode failed",
        "video_path": f"generative-jobs/{JOB_ID}/variant_tokold.mp4",
        "base_video_path": f"generative-jobs/{JOB_ID}/base_tokold.mp4",
    }
    _arm_full_render(monkeypatch, job, render_result)

    def _finish_after_supersede(**_kwargs):
        job.assembly_plan["variants"][0]["render_generation_id"] = "tok-new"
        return dict(render_result)

    monkeypatch.setattr(gb, "_render_generative_variant", _finish_after_supersede)
    deleted: list[str] = []
    monkeypatch.setattr(
        "app.storage.delete_object_best_effort",
        lambda path: deleted.append(path) or True,
    )

    gb._run_regenerate_variant(
        JOB_ID,
        "original_text",
        None,
        None,
        False,
        render_gen_id="tok-old",
    )

    assert sorted(deleted) == sorted(
        [render_result["video_path"], render_result["base_video_path"]]
    )


def test_accepted_failed_full_render_retires_old_base_and_unpublished_video(monkeypatch):
    old_base = f"generative-jobs/{JOB_ID}/base_previous.mp4"
    job = _FakeJob(
        [
            _variant(
                "tok-current",
                base_video_path=old_base,
                overlay_camera_rebuild_pending=True,
                media_overlays_render_dirty=True,
            )
        ]
    )
    render_result = {
        "ok": False,
        "render_status": "failed",
        "error": "final encode failed",
        "video_path": f"generative-jobs/{JOB_ID}/variant_tokcurrent.mp4",
        "base_video_path": f"generative-jobs/{JOB_ID}/base_tokcurrent.mp4",
    }
    _arm_full_render(monkeypatch, job, render_result)
    deleted: list[str] = []
    monkeypatch.setattr(
        "app.storage.delete_object_best_effort",
        lambda path: deleted.append(path) or True,
    )

    gb._run_regenerate_variant(
        JOB_ID,
        "original_text",
        None,
        None,
        False,
        render_gen_id="tok-current",
        force_full_render=True,
    )

    persisted = job.assembly_plan["variants"][0]
    assert persisted["base_video_path"] == render_result["base_video_path"]
    assert old_base in deleted
    assert render_result["video_path"] in deleted
    assert render_result["base_video_path"] not in deleted
    assert persisted["overlay_camera_rebuild_pending"] is True
    assert persisted["media_overlays_render_dirty"] is True


def _fake_ingest() -> dict:
    meta = types.SimpleNamespace(
        clip_id="c0",
        hook_score=1.0,
        best_moments=[],
        transcript="",
        detected_subject="",
        hook_text="",
    )
    probe = types.SimpleNamespace(duration_s=6.0)
    return {
        "clip_metas": [meta],
        "clip_id_to_local": {"c0": "/tmp/c0.mp4"},
        "clip_id_to_gcs": {"c0": CLIP_PATHS[0]},
        "probe_map": {"/tmp/c0.mp4": probe},
        "hero": meta,
    }


def test_full_render_success_write_lands_with_current_token(monkeypatch):
    job = _FakeJob([_variant("tok-cur")])
    _patch_sessions(monkeypatch, job)
    updates = _capture_updates(monkeypatch, job)
    monkeypatch.setattr(gb, "_is_fast_reburn_eligible", lambda *a, **k: False, raising=False)
    monkeypatch.setattr(gb, "_reapply_persisted_sfx_if_any", lambda **kw: None, raising=False)
    monkeypatch.setattr(
        gb, "_render_generative_variant", lambda **kw: dict(_READY_RESULT), raising=False
    )
    monkeypatch.setattr(gb, "_ingest_clips", lambda *a, **k: _fake_ingest(), raising=False)
    monkeypatch.setattr(gb, "_resolve_narrative_order", lambda *a, **k: None, raising=False)
    monkeypatch.setattr(gb, "_run_text_agents", lambda *a, **k: (None, None), raising=False)

    gb._run_regenerate_variant(JOB_ID, "original_text", None, None, False, render_gen_id="tok-cur")

    assert any(u.get("render_status") == "ready" for u in updates)


def test_full_render_success_reapplies_persisted_media_overlays(monkeypatch):
    overlays = [_media_overlay_card()]
    job = _FakeJob([_variant("tok-cur", media_overlays=overlays)])
    _patch_sessions(monkeypatch, job)
    updates = _capture_updates(monkeypatch, job)
    monkeypatch.setattr(gb, "_is_fast_reburn_eligible", lambda *a, **k: False, raising=False)
    overlay_calls: list = []
    sfx_calls: list = []
    monkeypatch.setattr(
        gb,
        "_reapply_persisted_media_overlays_if_any",
        lambda **kw: overlay_calls.append(kw) or True,
        raising=False,
    )
    monkeypatch.setattr(
        gb, "_reapply_persisted_sfx_if_any", lambda **kw: sfx_calls.append(kw), raising=False
    )
    monkeypatch.setattr(
        gb, "_render_generative_variant", lambda **kw: dict(_READY_RESULT), raising=False
    )
    monkeypatch.setattr(gb, "_ingest_clips", lambda *a, **k: _fake_ingest(), raising=False)
    monkeypatch.setattr(gb, "_resolve_narrative_order", lambda *a, **k: None, raising=False)
    monkeypatch.setattr(gb, "_run_text_agents", lambda *a, **k: (None, None), raising=False)

    gb._run_regenerate_variant(JOB_ID, "original_text", None, None, False, render_gen_id="tok-cur")

    ready = [u for u in updates if u.get("render_status") == "ready"]
    assert len(ready) == 1
    assert ready[0]["media_overlays"] == overlays
    assert ready[0]["pre_media_overlay_video_path"] is None
    assert overlay_calls == [
        {
            "job_id": JOB_ID,
            "variant_id": "original_text",
            "expected_render_gen_id": "tok-cur",
            # Montage terminals pass no wall-clock deadline (R4-2 default —
            # byte-identical standalone budget); only caption terminals thread one.
            "deadline_monotonic": None,
        }
    ]
    assert sfx_calls == []


def test_full_render_superseded_does_not_delete_snapshot_blobs(monkeypatch):
    """R1-2: the montage full-render terminal STAGES the snapshot nulls before the
    gen-gated write and frees the blobs only AFTER it is accepted — a superseded
    render must never delete the winning render's snapshot blobs."""
    overlays = [_media_overlay_card()]
    job = _FakeJob(
        [
            _variant(
                "tok-new",
                media_overlays=overlays,
                pre_media_overlay_video_path=f"generative-jobs/{JOB_ID}/pre_overlay.mp4",
                pre_sfx_video_path=f"generative-jobs/{JOB_ID}/pre_sfx.mp4",
                overlay_camera_rebuild_pending=True,
                media_overlays_render_dirty=True,
            )
        ]
    )
    _patch_sessions(monkeypatch, job)
    _capture_updates(monkeypatch, job)
    deleted: list[str] = []
    monkeypatch.setattr(
        "app.storage.delete_object_best_effort", lambda p: deleted.append(p) or True
    )
    monkeypatch.setattr(gb, "_is_fast_reburn_eligible", lambda *a, **k: False, raising=False)
    monkeypatch.setattr(
        gb, "_render_generative_variant", lambda **kw: dict(_READY_RESULT), raising=False
    )
    monkeypatch.setattr(gb, "_ingest_clips", lambda *a, **k: _fake_ingest(), raising=False)
    monkeypatch.setattr(gb, "_resolve_narrative_order", lambda *a, **k: None, raising=False)
    monkeypatch.setattr(gb, "_run_text_agents", lambda *a, **k: (None, None), raising=False)

    gb._run_regenerate_variant(
        JOB_ID,
        "original_text",
        None,
        None,
        False,
        render_gen_id="tok-old",
        force_full_render=True,
    )

    assert deleted == []
    persisted = job.assembly_plan["variants"][0]
    assert persisted["overlay_camera_rebuild_pending"] is True
    assert persisted["media_overlays_render_dirty"] is True


def test_full_render_accepted_frees_retired_snapshot_blobs(monkeypatch):
    """Companion: with the CURRENT token the write lands and the now-orphaned
    snapshot blobs are freed after it (same keys, same run shape)."""
    overlays = [_media_overlay_card()]
    job = _FakeJob(
        [
            _variant(
                "tok-cur",
                media_overlays=overlays,
                pre_media_overlay_video_path=f"generative-jobs/{JOB_ID}/pre_overlay.mp4",
                pre_sfx_video_path=f"generative-jobs/{JOB_ID}/pre_sfx.mp4",
                overlay_camera_rebuild_pending=True,
                media_overlays_render_dirty=True,
            )
        ]
    )
    _patch_sessions(monkeypatch, job)
    _capture_updates(monkeypatch, job)
    deleted: list[str] = []
    monkeypatch.setattr(
        "app.storage.delete_object_best_effort", lambda p: deleted.append(p) or True
    )
    monkeypatch.setattr(gb, "_is_fast_reburn_eligible", lambda *a, **k: False, raising=False)
    monkeypatch.setattr(
        gb, "_reapply_persisted_media_overlays_if_any", lambda **kw: True, raising=False
    )
    monkeypatch.setattr(gb, "_reapply_persisted_sfx_if_any", lambda **kw: True, raising=False)
    monkeypatch.setattr(
        gb, "_render_generative_variant", lambda **kw: dict(_READY_RESULT), raising=False
    )
    monkeypatch.setattr(gb, "_ingest_clips", lambda *a, **k: _fake_ingest(), raising=False)
    monkeypatch.setattr(gb, "_resolve_narrative_order", lambda *a, **k: None, raising=False)
    monkeypatch.setattr(gb, "_run_text_agents", lambda *a, **k: (None, None), raising=False)

    gb._run_regenerate_variant(
        JOB_ID,
        "original_text",
        None,
        None,
        False,
        render_gen_id="tok-cur",
        force_full_render=True,
    )

    assert sorted(deleted) == [
        f"generative-jobs/{JOB_ID}/pre_overlay.mp4",
        f"generative-jobs/{JOB_ID}/pre_sfx.mp4",
    ]
    persisted = job.assembly_plan["variants"][0]
    assert persisted["overlay_camera_rebuild_pending"] is False


def _arm_caption_camera_render(monkeypatch, job, *, upload):
    _patch_sessions(monkeypatch, job)
    _capture_updates(monkeypatch, job)
    monkeypatch.setattr("app.storage.download_to_file", lambda *a, **k: None)
    monkeypatch.setattr("app.storage.upload_public_read", upload)
    monkeypatch.setattr(
        "app.pipeline.probe.probe_video",
        lambda _path: types.SimpleNamespace(duration_s=5.0, has_audio=True),
    )
    monkeypatch.setattr(gb, "_should_compose_subtitled_final", lambda _v: False)
    monkeypatch.setattr(gb, "_burn_persisted_captions_onto_base", lambda *a, **k: None)
    monkeypatch.setattr(gb, "_rendered_duration_s", lambda _path: 5.0)
    monkeypatch.setattr(gb, "_will_reapply_media_layers", lambda _v: False)
    monkeypatch.setattr(gb, "_free_retired_visual_blocks_base", lambda *a, **k: None)
    monkeypatch.setattr(gb, "_free_retired_media_snapshots", lambda *a, **k: None)
    monkeypatch.setattr(
        "app.services.pipeline_trace.pipeline_trace_for",
        lambda _job_id: contextlib.nullcontext(),
    )


def _caption_camera_variant(token: str) -> dict:
    return _variant(
        token,
        variant_id="subtitled",
        rank=1,
        resolved_archetype="subtitled",
        camera_effects=None,
        overlay_camera_rebuild_pending=True,
        media_overlays_render_dirty=True,
    )


def test_caption_camera_winning_terminal_clears_pending_markers(monkeypatch):
    job = _FakeJob([_caption_camera_variant("tok-cur")])
    _arm_caption_camera_render(
        monkeypatch,
        job,
        upload=lambda _local, _gcs: "https://signed/camera",
    )
    terminal = {"accepted": False}

    gb._run_rerender_caption_camera_effects(
        JOB_ID,
        "subtitled",
        render_gen_id="tok-cur",
        terminal_state=terminal,
    )

    persisted = job.assembly_plan["variants"][0]
    assert terminal["accepted"] is True
    assert persisted["overlay_camera_rebuild_pending"] is False
    assert persisted["media_overlays_render_dirty"] is False
    assert persisted["render_status"] == "ready"
    assert "_camera_" in persisted["video_path"]


def test_caption_camera_failure_before_swap_preserves_pending_markers(monkeypatch):
    job = _FakeJob([_caption_camera_variant("tok-cur")])

    def _fail_upload(_local, _gcs):
        raise RuntimeError("upload failed")

    _arm_caption_camera_render(monkeypatch, job, upload=_fail_upload)

    gb.rerender_caption_camera_effects.run(JOB_ID, "subtitled", "tok-cur")

    persisted = job.assembly_plan["variants"][0]
    assert persisted["overlay_camera_rebuild_pending"] is True
    assert persisted["media_overlays_render_dirty"] is True
    assert persisted["render_status"] == "ready"
    assert persisted["render_error"] == "upload failed"


def test_caption_camera_stale_terminal_preserves_pending_markers(monkeypatch):
    job = _FakeJob([_caption_camera_variant("tok-old")])
    old_video_path = job.assembly_plan["variants"][0]["video_path"]
    deleted: list[str] = []

    def _supersede_during_upload(_local, _gcs):
        job.assembly_plan["variants"][0]["render_generation_id"] = "tok-new"
        return "https://signed/stale-camera"

    _arm_caption_camera_render(monkeypatch, job, upload=_supersede_during_upload)
    monkeypatch.setattr(
        "app.storage.delete_object_best_effort",
        lambda path: deleted.append(path) or True,
    )
    terminal = {"accepted": False}

    gb._run_rerender_caption_camera_effects(
        JOB_ID,
        "subtitled",
        render_gen_id="tok-old",
        terminal_state=terminal,
    )

    persisted = job.assembly_plan["variants"][0]
    assert terminal["accepted"] is False
    assert persisted["overlay_camera_rebuild_pending"] is True
    assert persisted["media_overlays_render_dirty"] is True
    assert persisted["video_path"] == old_video_path
    assert len(deleted) == 1
    assert "_camera_" in deleted[0]


def test_full_render_success_reburns_persisted_text_elements_after_new_base(monkeypatch):
    elements = [
        {
            "id": "edited-intro",
            "text": "Edited intro",
            "start_s": 0.0,
            "end_s": 3.0,
            "role": "generative_intro",
        }
    ]
    job = _FakeJob([_variant("tok-cur", text_elements=elements, text_elements_user_edited=True)])
    _patch_sessions(monkeypatch, job)
    updates = _capture_updates(monkeypatch, job)
    monkeypatch.setattr(gb, "_is_fast_reburn_eligible", lambda *a, **k: False, raising=False)
    monkeypatch.setattr(gb, "_reapply_persisted_sfx_if_any", lambda **kw: None, raising=False)
    render_result = {
        **_READY_RESULT,
        "base_video_path": f"generative-jobs/{JOB_ID}/base_new.mp4",
        "video_path": f"generative-jobs/{JOB_ID}/variant_new.mp4",
    }
    monkeypatch.setattr(
        gb, "_render_generative_variant", lambda **kw: dict(render_result), raising=False
    )
    monkeypatch.setattr(gb, "_ingest_clips", lambda *a, **k: _fake_ingest(), raising=False)
    monkeypatch.setattr(gb, "_resolve_narrative_order", lambda *a, **k: None, raising=False)
    monkeypatch.setattr(gb, "_run_text_agents", lambda *a, **k: (None, None), raising=False)
    reburn_calls: list[dict] = []

    def _fake_reburn_text_on_base(**kwargs):
        reburn_calls.append(kwargs)
        return {
            "render_status": "ready",
            "ok": True,
            "video_path": "generative-jobs/reburned.mp4",
            "output_url": "https://signed/reburned",
            "text_elements_user_edited": True,
        }

    monkeypatch.setattr(gb, "_reburn_text_on_base", _fake_reburn_text_on_base, raising=False)

    gb._run_regenerate_variant(JOB_ID, "original_text", None, None, False, render_gen_id="tok-cur")

    assert reburn_calls
    assert reburn_calls[0]["existing"]["base_video_path"] == render_result["base_video_path"]
    assert reburn_calls[0]["existing"]["text_elements"] == elements
    ready = [u for u in updates if u.get("render_status") == "ready"]
    assert ready[-1]["video_path"] == "generative-jobs/reburned.mp4"
    assert ready[-1]["output_url"] == "https://signed/reburned"


@pytest.mark.parametrize(
    ("public_text_lane_enabled", "visual_blocks"),
    [
        (True, []),
        (
            False,
            [
                {
                    "id": "section-card",
                    "kind": "text_card",
                    "start_s": 0.0,
                    "end_s": 2.0,
                    "purpose": "section_item",
                }
            ],
        ),
    ],
)
def test_subtitled_text_fast_reburn_mints_new_key_deletes_old_and_rereads(
    monkeypatch,
    public_text_lane_enabled,
    visual_blocks,
):
    elements = [
        {
            "id": "title",
            "text": "TITLE",
            "start_s": 0.0,
            "end_s": 2.0,
            "role": "generative_intro",
            "position": "middle",
        }
    ]
    original = _variant(
        "tok-sub",
        variant_id="subtitled",
        rank=1,
        text_mode="none",
        resolved_archetype="subtitled",
        video_path=f"generative-jobs/{JOB_ID}/variant_1_subtitled_old.mp4",
        base_video_path=f"generative-jobs/{JOB_ID}/variant_1_subtitled_base.mp4",
        intro_text=None,
        text_elements=elements,
        text_elements_user_edited=True,
        visual_blocks=visual_blocks,
        caption_cues=[{"text": "old caption", "start_s": 0.0, "end_s": 1.0}],
    )
    job = _FakeJob([original])
    _patch_sessions(monkeypatch, job)
    updates = _capture_updates(monkeypatch, job)
    deleted: list[str] = []
    compose_variants: list[dict] = []

    monkeypatch.setattr(
        gb.settings,
        "subtitled_text_lane_enabled",
        public_text_lane_enabled,
        raising=False,
    )
    monkeypatch.setattr(gb.settings, "visual_blocks_enabled", True, raising=False)
    monkeypatch.setattr(gb, "_reapply_persisted_media_overlays_if_any", lambda **kw: False)
    monkeypatch.setattr(gb, "_reapply_persisted_sfx_if_any", lambda **kw: None)

    def _download(_src, dst):
        with open(dst, "wb") as f:
            f.write(b"base")
        job.assembly_plan["variants"][0] = {
            **job.assembly_plan["variants"][0],
            "caption_cues": [{"text": "latest caption", "start_s": 0.0, "end_s": 1.0}],
        }

    def _compose(_base_local, variant, tmpdir, **_matte_kwargs):
        compose_variants.append(dict(variant))
        out = f"{tmpdir}/out.mp4"
        with open(out, "wb") as f:
            f.write(b"composed")
        return out, variant.get("subject_matte_path")

    monkeypatch.setattr("app.storage.download_to_file", _download)
    monkeypatch.setattr(
        "app.storage.upload_public_read", lambda _local, gcs: f"https://signed/{gcs}"
    )
    monkeypatch.setattr(
        "app.storage.delete_object_best_effort",
        lambda path: deleted.append(path) or True,
    )
    monkeypatch.setattr(
        "app.pipeline.probe.probe_video",
        lambda _path: types.SimpleNamespace(duration_s=5.0, width=1080, height=1920),
    )
    monkeypatch.setattr(gb, "_compose_subtitled_final", _compose)

    gb._run_regenerate_variant(
        JOB_ID,
        "subtitled",
        None,
        None,
        False,
        render_gen_id="tok-sub",
    )

    ready = updates[-1]
    assert ready["video_path"] != original["video_path"]
    assert "_text_" in ready["video_path"]
    assert deleted == [original["video_path"]]
    assert compose_variants[0]["caption_cues"][0]["text"] == "latest caption"
