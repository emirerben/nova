"""Nova steps projection: allowlist pin, sanitizer pin, AgentRun exclusion pin,
synthetic-trace snapshot, and the status-route flag gate.

This is a security-relevant surface: `project_nova_steps` is the ONLY thing
standing between the raw `pipeline_trace`/`phase_log`/`AgentRun` sinks (which
intentionally carry admin-only content -- overlay text, prompts, signed
URLs) and a public, owner-facing API response. Every test here pins a
specific promise:

  - test_allowlist_pin: adding a new (stage, event) pair is a conscious,
    reviewed change, not an accidental one.
  - test_sanitizer_strips_blocked_keys*: the second-defense key-substring
    blocklist actually strips text|prompt|url|path|note-keyed fields, even
    on an allowlisted event.
  - test_agent_run_projection_never_touches_llm_io: AgentRun's `input_json`/
    `output_json`/`raw_text`/`error_message` never reach a NovaStep, even
    when they contain sensitive content.
  - test_project_nova_steps_synthetic_snapshot: the full merge (trace +
    phase_log + agent_runs), sorted, capped, and status-annotated.
  - test_status_route_steps_gated_by_flag: the route only populates `steps`
    when `nova_steps_feed_enabled` is True; off is byte-identical (None).
"""

from __future__ import annotations

import types
import uuid as _uuid
from datetime import UTC, datetime, timedelta

import pytest

from app.services.nova_steps import (
    STEP_ALLOWLIST,
    NovaStep,
    _sanitize_event_data,
    project_nova_steps,
)

# ---------------------------------------------------------------------------
# (a) Allowlist pin
# ---------------------------------------------------------------------------


def test_allowlist_pin() -> None:
    """Exact allowlist contents -- any addition/removal must touch this test."""
    assert STEP_ALLOWLIST == {
        "assembly": frozenset(
            {
                "clip_metadata_done",
                "song_match_done",
                "archetype_selected",
                "archetype_fallback",
                "narrative_order_applied",
            }
        ),
        "reframe": frozenset({"hdr_pretonemap_done"}),
        "overlay": frozenset({"style_set_selected"}),
        "silence_cut": frozenset({"silence_cut_plan"}),
        "smart_captions": frozenset({"plan_compiled"}),
        "media_overlay": frozenset({"cards_applied"}),
        "sound_effects": frozenset({"effects_applied"}),
        "audio_mix": frozenset({"voiceover_mixed"}),
        "render_stage": frozenset({"*"}),
        "custom_effect": frozenset({"burn_start", "burn_done"}),
        "render": frozenset({"custom_effect_reapply_failed"}),
    }


def test_allowlist_excludes_known_text_carrying_events() -> None:
    # overlay.render_window and overlay.agent_text_done carry literal overlay
    # text (see agents/DECISIONS.md + explorer-3 findings) -- these must
    # never be allowlisted, regardless of how the dict above is edited.
    assert "render_window" not in STEP_ALLOWLIST.get("overlay", frozenset())
    assert "agent_text_done" not in STEP_ALLOWLIST.get("overlay", frozenset())


# ---------------------------------------------------------------------------
# (b) Sanitizer pin
# ---------------------------------------------------------------------------


def test_sanitizer_strips_blocked_keys_directly() -> None:
    dirty = {
        "track_id": "trk_123",
        "card_count": 3,
        "user_note": "SECRET NOTE CONTENT",
        "overlay_text": "SECRET OVERLAY TEXT",
        "signed_url": "https://storage.example/leak?sig=abc",
        "clip_path": "gs://bucket/private/clip.mp4",
        "prompt_used": "SECRET PROMPT",
    }
    safe = _sanitize_event_data(dirty)
    assert safe == {"track_id": "trk_123", "card_count": 3}
    for blocked_value in (
        "SECRET NOTE CONTENT",
        "SECRET OVERLAY TEXT",
        "https://storage.example/leak?sig=abc",
        "gs://bucket/private/clip.mp4",
        "SECRET PROMPT",
    ):
        assert blocked_value not in safe.values()


def test_sanitizer_drops_non_scalar_values() -> None:
    safe = _sanitize_event_data({"nested": {"a": 1}, "listy": [1, 2], "ok_count": 5})
    assert safe == {"ok_count": 5}


def _job(
    *,
    status: str = "rendering",
    pipeline_trace: list[dict] | None = None,
    phase_log: list[dict] | None = None,
) -> types.SimpleNamespace:
    return types.SimpleNamespace(
        status=status,
        pipeline_trace=pipeline_trace if pipeline_trace is not None else [],
        phase_log=phase_log if phase_log is not None else [],
    )


def test_sanitizer_pin_end_to_end_via_allowlisted_event() -> None:
    """An allowlisted (stage, event) whose data smuggles a blocked-key field
    must never leak that field's value into the projected NovaStep."""
    job = _job(
        status="variants_ready",
        pipeline_trace=[
            {
                "ts": "2026-08-11T10:00:00+00:00",
                "stage": "media_overlay",
                "event": "cards_applied",
                "data": {
                    "variant_id": "var_1",
                    "card_count": 2,
                    "elapsed_ms": 500,
                    # Attacker-shaped keys that must never survive.
                    "card_note": "user's private caption text",
                    "asset_url": "https://signed.example/leak",
                },
            }
        ],
    )
    steps = project_nova_steps(job)
    assert len(steps) == 1
    step = steps[0]
    blob = f"{step.label} {' '.join(step.detail or [])}"
    assert "private caption text" not in blob
    assert "signed.example" not in blob


# ---------------------------------------------------------------------------
# (c) AgentRun projection never includes LLM I/O
# ---------------------------------------------------------------------------


def _agent_run(
    *,
    agent_name: str = "clip_metadata",
    outcome: str = "ok",
    latency_ms: int | None = 2500,
    created_at: datetime | None = None,
    run_id: _uuid.UUID | None = None,
    input_json: object = "SECRET INPUT JSON",
    output_json: object = "SECRET OUTPUT JSON",
    raw_text: str = "SECRET RAW TEXT",
    error_message: str | None = "SECRET ERROR MESSAGE",
) -> types.SimpleNamespace:
    return types.SimpleNamespace(
        id=run_id or _uuid.uuid4(),
        agent_name=agent_name,
        outcome=outcome,
        latency_ms=latency_ms,
        created_at=created_at or datetime.now(UTC),
        input_json=input_json,
        output_json=output_json,
        raw_text=raw_text,
        error_message=error_message,
    )


def test_agent_run_projection_never_touches_llm_io() -> None:
    run = _agent_run()
    job = _job(status="variants_ready")
    steps = project_nova_steps(job, [run])
    assert len(steps) == 1
    step = steps[0]
    blob = f"{step.id} {step.label} {' '.join(step.detail or [])}"
    assert "SECRET INPUT JSON" not in blob
    assert "SECRET OUTPUT JSON" not in blob
    assert "SECRET RAW TEXT" not in blob
    assert "SECRET ERROR MESSAGE" not in blob


def test_agent_run_failed_outcome_maps_to_failed_status() -> None:
    run = _agent_run(outcome="error_parse", latency_ms=None)
    job = _job(status="variants_ready")
    steps = project_nova_steps(job, [run])
    assert steps[0].status == "failed"


def test_agent_run_success_outcomes_map_to_done_or_active() -> None:
    for outcome in ("ok", "ok_fallback"):
        run = _agent_run(outcome=outcome)
        # Terminal job -> done, not active.
        job = _job(status="variants_ready")
        steps = project_nova_steps(job, [run])
        assert steps[0].status == "done"


# ---------------------------------------------------------------------------
# (d) Synthetic merge snapshot
# ---------------------------------------------------------------------------


def test_project_nova_steps_synthetic_snapshot() -> None:
    t0 = datetime(2026, 8, 11, 10, 0, 0, tzinfo=UTC)

    job = _job(
        status="rendering",  # non-terminal -> latest step promoted to "active"
        pipeline_trace=[
            {
                "ts": (t0 + timedelta(seconds=1)).isoformat(),
                "stage": "assembly",
                "event": "clip_metadata_done",
                "data": {"clips": 4, "available_footage_s": 42.0},
            },
            {
                "ts": (t0 + timedelta(seconds=2)).isoformat(),
                "stage": "assembly",
                "event": "song_match_done",
                "data": {"track_id": "trk_9"},
            },
            {
                # NOT allowlisted -- must be dropped entirely.
                "ts": (t0 + timedelta(seconds=3)).isoformat(),
                "stage": "overlay",
                "event": "render_window",
                "data": {"overlay_text": "should never appear"},
            },
            {
                "ts": (t0 + timedelta(seconds=5)).isoformat(),
                "stage": "assembly",
                "event": "archetype_selected",
                "data": {"archetype": "talking_head", "speech_coverage": 0.812},
            },
        ],
        phase_log=[
            {
                "ts": (t0 + timedelta(seconds=0)).isoformat(),
                "name": "analyze_clips",
                "elapsed_ms": 900,
            },
        ],
    )
    run = _agent_run(
        agent_name="music_matcher",
        outcome="ok",
        latency_ms=1300,
        created_at=t0 + timedelta(seconds=4),
    )

    steps = project_nova_steps(job, [run])

    assert [s.kind for s in steps] == ["phase", "decision", "decision", "agent", "decision"]
    assert [s.label for s in steps] == [
        "Nova analyzed your clips",  # phase_log "analyze_clips"
        "Nova analyzed your clips",  # assembly.clip_metadata_done
        "Nova matched a song to your footage",
        "Nova matched a song",
        "Nova chose the talking-head edit style",
    ]
    assert steps[0].detail is None
    assert steps[1].detail == ["4 clips analyzed", "42.0s of footage"]
    assert steps[4].detail == ["81% speech coverage"]

    # Chronologically sorted, non-terminal job -> only the LAST step is active.
    assert [s.status for s in steps[:-1]] == ["done", "done", "done", "done"]
    assert steps[-1].status == "active"

    # Stable, non-random ids derived from stage:event:index / agent:name:run_id.
    assert steps[1].id == "assembly:clip_metadata_done:0"
    assert steps[2].id == "assembly:song_match_done:1"
    assert steps[4].id == "assembly:archetype_selected:3"
    assert steps[3].id == f"agent:music_matcher:{run.id}"


def test_project_nova_steps_terminal_job_has_no_active_step() -> None:
    t0 = datetime(2026, 8, 11, 10, 0, 0, tzinfo=UTC)
    job = _job(
        status="variants_ready",
        pipeline_trace=[
            {
                "ts": t0.isoformat(),
                "stage": "assembly",
                "event": "clip_metadata_done",
                "data": {"clips": 2},
            },
        ],
    )
    steps = project_nova_steps(job)
    assert steps[0].status == "done"


def test_project_nova_steps_read_cap() -> None:
    t0 = datetime(2026, 8, 11, 10, 0, 0, tzinfo=UTC)
    trace = [
        {
            "ts": (t0 + timedelta(seconds=i)).isoformat(),
            "stage": "assembly",
            "event": "clip_metadata_done",
            "data": {"clips": i},
        }
        for i in range(60)
    ]
    job = _job(status="variants_ready", pipeline_trace=trace)
    steps = project_nova_steps(job)
    assert len(steps) == 40
    # Cap keeps the MOST RECENT 40, not the first 40.
    assert steps[-1].id == "assembly:clip_metadata_done:59"
    assert steps[0].id == "assembly:clip_metadata_done:20"


def test_custom_effect_burn_events_project_with_nova_voiced_labels() -> None:
    """Bug 1 (E2E fix): custom-effect render steps must appear in the feed."""
    t0 = datetime(2026, 8, 11, 10, 0, 0, tzinfo=UTC)
    job = _job(
        status="rendering",
        pipeline_trace=[
            {
                "ts": t0.isoformat(),
                "stage": "custom_effect",
                "event": "burn_start",
                "data": {"variant_id": "var_1", "filters": 3},
            },
            {
                "ts": (t0 + timedelta(seconds=5)).isoformat(),
                "stage": "custom_effect",
                "event": "burn_done",
                "data": {"variant_id": "var_1", "filters": 3},
            },
        ],
    )
    steps = project_nova_steps(job)
    assert [s.label for s in steps] == ["Applying your custom look", "Custom look applied"]
    assert [s.kind for s in steps] == ["render", "render"]
    assert steps[0].detail == ["3 filters"]
    assert steps[1].detail == ["3 filters"]
    # variant_id never leaks -- humanizers only ever read `filters`.
    assert "var_1" not in f"{steps[0].label} {steps[1].label}"
    # Non-terminal job -> only the chronologically last step is active.
    assert steps[0].status == "done"
    assert steps[1].status == "active"


def test_custom_effect_reapply_failed_is_visible_and_marked_failed() -> None:
    """Bug 1 (E2E fix): reapply failures must surface (fail-open != invisible)."""
    t0 = datetime(2026, 8, 11, 10, 0, 0, tzinfo=UTC)
    job = _job(
        status="variants_ready",
        pipeline_trace=[
            {
                "ts": t0.isoformat(),
                "stage": "render",
                "event": "custom_effect_reapply_failed",
                "data": {"variant_id": "var_1", "reason": "burn_failed", "stage": "render"},
            },
        ],
    )
    steps = project_nova_steps(job)
    assert len(steps) == 1
    assert steps[0].label == "Couldn't re-apply your custom look — kept the video without it"
    assert steps[0].kind == "render"
    assert steps[0].status == "failed"
    # The raw validator reason code never leaks into the user-facing copy.
    assert "burn_failed" not in steps[0].label


def test_other_render_stage_events_stay_excluded() -> None:
    """Only custom_effect_reapply_failed is allowlisted under "render" --
    unrelated internal events (e.g. fast_reburn_base_probe_failed) that carry
    base_path must stay dropped."""
    job = _job(
        pipeline_trace=[
            {
                "ts": datetime.now(UTC).isoformat(),
                "stage": "render",
                "event": "fast_reburn_base_probe_failed",
                "data": {"base_path": "gs://bucket/private/base.mp4"},
            }
        ]
    )
    assert project_nova_steps(job) == []


def test_unknown_event_within_allowlisted_stage_is_dropped() -> None:
    job = _job(
        pipeline_trace=[
            {
                "ts": datetime.now(UTC).isoformat(),
                "stage": "assembly",
                "event": "some_future_event_not_yet_allowlisted",
                "data": {},
            }
        ]
    )
    assert project_nova_steps(job) == []


def test_malformed_trace_entries_are_skipped_not_raised() -> None:
    job = _job(
        pipeline_trace=[
            "not a dict",
            {"stage": "assembly"},  # missing event
            {"stage": 123, "event": "clip_metadata_done"},  # wrong type
            {"stage": "assembly", "event": "clip_metadata_done", "data": {}},  # missing ts
        ]
    )
    assert project_nova_steps(job) == []


# ---------------------------------------------------------------------------
# NovaStep schema sanity
# ---------------------------------------------------------------------------


def test_nova_step_kind_and_status_are_constrained() -> None:
    with pytest.raises(Exception):
        NovaStep(
            id="x",
            ts=datetime.now(UTC),
            kind="not_a_real_kind",  # type: ignore[arg-type]
            label="x",
            status="done",
        )


# ---------------------------------------------------------------------------
# (e) Status-route flag gate
# ---------------------------------------------------------------------------


async def _status_response_for(
    monkeypatch: pytest.MonkeyPatch,
    *,
    flag_on: bool,
    agent_runs: list | None = None,
) -> object:
    """Drives the REAL status route (mirrors the retrying route-test pattern
    in tests/routes/test_generative_retrying.py)."""
    import app.routes.generative_jobs as gj
    import app.services.phase_baselines as pb
    from app.config import settings

    job = types.SimpleNamespace(
        id=_uuid.uuid4(),
        status="rendering",
        mode="generative",
        assembly_plan={"variants": []},
        error_detail=None,
        created_at=datetime.now(UTC),
        updated_at=datetime.now(UTC),
        all_candidates={},
        current_phase="assemble",
        phase_log=[
            {"ts": datetime.now(UTC).isoformat(), "name": "analyze_clips", "elapsed_ms": 100}
        ],
        pipeline_trace=[],
        started_at=datetime.now(UTC),
        finished_at=None,
        worker_heartbeat_at=None,
    )

    async def _load(job_id, db, user, allowed_modes=None, **kwargs):
        return job

    async def _load_runs(db, job_id):
        return agent_runs or []

    monkeypatch.setattr(gj, "_load_generative_job", _load)
    monkeypatch.setattr(gj, "_load_agent_runs_for_nova_steps", _load_runs)
    monkeypatch.setattr(pb, "get_baselines", lambda mode: None)
    monkeypatch.setattr(settings, "nova_steps_feed_enabled", flag_on)
    return await gj.get_generative_job_status(str(job.id), current_user=object(), db=object())


async def test_status_route_steps_none_when_flag_off(monkeypatch: pytest.MonkeyPatch) -> None:
    resp = await _status_response_for(monkeypatch, flag_on=False)
    assert resp.steps is None


async def test_status_route_steps_populated_when_flag_on(monkeypatch: pytest.MonkeyPatch) -> None:
    resp = await _status_response_for(monkeypatch, flag_on=True)
    assert resp.steps is not None
    assert len(resp.steps) == 1
    assert resp.steps[0].kind == "phase"
