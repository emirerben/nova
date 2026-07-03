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
        variant = next(
            (v for v in job.assembly_plan["variants"] if v.get("variant_id") == vid),
            None,
        )
        if variant is None:
            return False
        current = variant.get("render_generation_id")
        if (
            expected_render_gen_id is not None
            and current is not None
            and current != expected_render_gen_id
        ):
            return False
        update = {k: v for k, v in patch.items() if k != "variant_id"}
        variant.update(update)
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
    sfx_calls: list = []
    monkeypatch.setattr(
        gb,
        "_reapply_persisted_sfx_if_any",
        lambda **kw: sfx_calls.append(kw),
        raising=False,
    )
    return updates, sfx_calls


_READY_RESULT = {"render_status": "ready", "ok": True, "output_url": "https://signed/new"}


def test_stale_task_terminal_write_discarded(monkeypatch):
    """Task launched with tok-old; a newer commit bumped the variant to tok-new →
    the reburn completes but neither the ready patch nor the SFX hook lands."""
    job = _FakeJob([_variant("tok-new")])
    updates, sfx_calls = _arm_reburn(monkeypatch, job, _READY_RESULT)

    gb._run_regenerate_variant(JOB_ID, "original_text", None, None, False, render_gen_id="tok-old")

    # Only the non-terminal "rendering" marker may have been written.
    assert all(u.get("render_status") == "rendering" for u in updates)
    assert not any(u.get("render_status") == "ready" for u in updates)
    assert sfx_calls == []


def test_current_task_terminal_write_lands(monkeypatch):
    """Same run with the CURRENT token → the ready patch + SFX hook both land."""
    job = _FakeJob([_variant("tok-current")])
    updates, sfx_calls = _arm_reburn(monkeypatch, job, _READY_RESULT)

    gb._run_regenerate_variant(
        JOB_ID, "original_text", None, None, False, render_gen_id="tok-current"
    )

    ready = [u for u in updates if u.get("render_status") == "ready"]
    assert len(ready) == 1
    assert ready[0]["output_url"] == "https://signed/new"
    assert len(sfx_calls) == 1


def test_tokenless_task_terminal_write_lands(monkeypatch):
    """Regression rule: a legacy task (no token) writes even on a stamped variant."""
    job = _FakeJob([_variant("tok-any")])
    updates, _ = _arm_reburn(monkeypatch, job, _READY_RESULT)

    gb._run_regenerate_variant(JOB_ID, "original_text", None, None, False, render_gen_id=None)

    assert any(u.get("render_status") == "ready" for u in updates)


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
