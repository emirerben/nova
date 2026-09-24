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
from fastapi import Response

from app.services.nova_steps import (
    STEP_ALLOWLIST,
    NovaStep,
    _sanitize_event_data,
    beat_miss_sentence,
    project_nova_steps,
    render_notes_from_beat_receipt,
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
        "phone": frozenset({"subtitled_overlay_grounding", "subtitled_reaction_beats"}),
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
    """A dict value is dropped outright. A flat list of scalars (KRI-178:
    `missed`, a capped list of the creator's own trigger phrases) survives;
    a list containing anything else (a nested dict) is still dropped whole,
    same as before this extension."""
    safe = _sanitize_event_data(
        {"nested": {"a": 1}, "listy": [1, 2], "ok_count": 5, "bad_listy": [{"a": 1}]}
    )
    assert safe == {"listy": [1, 2], "ok_count": 5}


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


def test_subtitled_overlay_grounding_placed_cards_project_with_nova_voiced_label() -> None:
    """KRI-176: phone Talking PiP grounding, cards actually placed."""
    job = _job(
        status="variants_ready",
        pipeline_trace=[
            {
                "ts": datetime.now(UTC).isoformat(),
                "stage": "phone",
                "event": "subtitled_overlay_grounding",
                "data": {"placed": 2, "unplaced": 1, "matcher": "agent", "face_sampling": "ok"},
            }
        ],
    )
    steps = project_nova_steps(job)
    assert len(steps) == 1
    assert steps[0].label == "Nova popped your Visuals in as cards"
    assert steps[0].detail == ["2 cards placed", "1 Visual couldn't be placed"]
    # matcher/face_sampling are enum-like internal status, never surfaced.
    assert "agent" not in steps[0].detail
    assert "face_sampling" not in " ".join(steps[0].detail)


def test_subtitled_overlay_grounding_no_cards_placed_projects_lookedfor_label() -> None:
    """KRI-176: nothing placed -- softer "looked for" phrasing, no card count."""
    job = _job(
        pipeline_trace=[
            {
                "ts": datetime.now(UTC).isoformat(),
                "stage": "phone",
                "event": "subtitled_overlay_grounding",
                "data": {
                    "placed": 0,
                    "unplaced": 3,
                    "matcher": "heuristic",
                    "face_sampling": "skipped",
                },
            }
        ],
    )
    steps = project_nova_steps(job)
    assert len(steps) == 1
    assert steps[0].label == "Nova looked for moments to show your Visuals"
    assert steps[0].detail == ["3 Visuals couldn't be placed"]


def test_subtitled_overlay_grounding_zero_and_zero_has_no_detail() -> None:
    """KRI-176: neither placed nor unplaced -- no detail line at all."""
    job = _job(
        pipeline_trace=[
            {
                "ts": datetime.now(UTC).isoformat(),
                "stage": "phone",
                "event": "subtitled_overlay_grounding",
                "data": {"placed": 0, "unplaced": 0, "matcher": "none", "face_sampling": "failed"},
            }
        ],
    )
    steps = project_nova_steps(job)
    assert len(steps) == 1
    assert steps[0].label == "Nova looked for moments to show your Visuals"
    assert steps[0].detail is None


# ---------------------------------------------------------------------------
# KRI-178: reaction beats (allowlist pass-through, humanizer, render_notes)
# ---------------------------------------------------------------------------


def test_subtitled_reaction_beats_placed_projects_with_kria_voiced_label() -> None:
    """Any unplaced beat (even alongside placed ones) switches the summary
    line to 'Placed N of M moments you named'."""
    job = _job(
        status="variants_ready",
        pipeline_trace=[
            {
                "ts": datetime.now(UTC).isoformat(),
                "stage": "phone",
                "event": "subtitled_reaction_beats",
                "data": {
                    "placed": 2,
                    "unplaced": 1,
                    "missed": ["when he scores"],
                    "missed_reasons": ["never_heard"],
                    "closing": "placed",
                },
            }
        ],
    )
    steps = project_nova_steps(job)
    assert len(steps) == 1
    assert steps[0].label == "Kria timed your photos and sounds to your words"
    assert steps[0].detail == [
        "Placed 2 of 3 moments you named",
        'I never heard "when he scores", so its photo or sound wasn\'t shown',
    ]


def test_subtitled_reaction_beats_none_placed_projects_listened_for_label() -> None:
    """`placed == 0` still gets a 'Placed 0 of N' summary line when there
    are unplaced beats to report."""
    job = _job(
        pipeline_trace=[
            {
                "ts": datetime.now(UTC).isoformat(),
                "stage": "phone",
                "event": "subtitled_reaction_beats",
                "data": {
                    "placed": 0,
                    "unplaced": 2,
                    "missed": ["when he scores", "final whistle"],
                    "closing": "none",
                },
            }
        ],
    )
    steps = project_nova_steps(job)
    assert len(steps) == 1
    assert steps[0].label == "Kria listened for the moments you named"
    assert steps[0].detail == [
        "Placed 0 of 2 moments you named",
        'I never heard "when he scores", so its photo or sound wasn\'t shown',
        'I never heard "final whistle", so its photo or sound wasn\'t shown',
    ]


def test_subtitled_reaction_beats_missed_reasons_map_to_specific_sentences() -> None:
    """Each `missed_reasons[i]` picks a DIFFERENT sentence for `missed[i]` --
    the bug this follow-up fixes was one blanket "I never heard" sentence
    regardless of why the beat actually missed."""
    job = _job(
        pipeline_trace=[
            {
                "ts": datetime.now(UTC).isoformat(),
                "stage": "phone",
                "event": "subtitled_reaction_beats",
                "data": {
                    "placed": 0,
                    "unplaced": 5,
                    "missed": ["a", "b", "c", "d", "e"],
                    "missed_reasons": [
                        "visual_not_in_pool",
                        "sound_not_found",
                        "no_safe_spot",
                        "bind_failed",
                        "never_heard",
                    ],
                    "closing": "none",
                },
            }
        ],
    )
    steps = project_nova_steps(job)
    assert steps[0].detail == [
        "Placed 0 of 5 moments you named",
        'Couldn\'t find the photo or sticker for "a" in your Visuals',
        'Couldn\'t find a sound for "b" in the library',
        'No room to show "c" without covering your face or the captions',
        'Couldn\'t add "d" to the phone render',
        'I never heard "e", so its photo or sound wasn\'t shown',
    ]


def test_subtitled_reaction_beats_missing_missed_reasons_defaults_to_never_heard() -> None:
    """An event predating `missed_reasons` (or one that dropped it) still
    projects the old, honest-for-that-case default."""
    job = _job(
        pipeline_trace=[
            {
                "ts": datetime.now(UTC).isoformat(),
                "stage": "phone",
                "event": "subtitled_reaction_beats",
                "data": {"placed": 0, "unplaced": 1, "missed": ["x"], "closing": "none"},
            }
        ],
    )
    steps = project_nova_steps(job)
    assert steps[0].detail == [
        "Placed 0 of 1 moments you named",
        'I never heard "x", so its photo or sound wasn\'t shown',
    ]


def test_subtitled_reaction_beats_unplaced_closing_adds_detail_line() -> None:
    job = _job(
        pipeline_trace=[
            {
                "ts": datetime.now(UTC).isoformat(),
                "stage": "phone",
                "event": "subtitled_reaction_beats",
                "data": {"placed": 1, "unplaced": 0, "missed": [], "closing": "unplaced"},
            }
        ],
    )
    steps = project_nova_steps(job)
    assert len(steps) == 1
    assert steps[0].detail == ["1 moment placed", "Couldn't place the closing photo"]


def test_beat_miss_sentence_reason_buckets() -> None:
    """Pins every reason bucket `beat_miss_sentence` maps -- the fix for the
    misleading blanket "I never heard" sentence."""
    never_heard = 'I never heard "x", so its photo or sound wasn\'t shown'
    assert beat_miss_sentence("x", None) == never_heard
    assert beat_miss_sentence("x", "never_heard") == never_heard
    assert beat_miss_sentence("x", "after_not_heard") == never_heard

    visual_missing = 'Couldn\'t find the photo or sticker for "x" in your Visuals'
    assert beat_miss_sentence("x", "visual_not_in_pool") == visual_missing
    assert beat_miss_sentence("x", "visual_is_video") == visual_missing

    assert (
        beat_miss_sentence("x", "sound_not_found")
        == 'Couldn\'t find a sound for "x" in the library'
    )

    no_room = 'No room to show "x" without covering your face or the captions'
    assert beat_miss_sentence("x", "no_safe_spot") == no_room
    assert beat_miss_sentence("x", "too_short") == no_room
    assert beat_miss_sentence("x", "overlap") == no_room

    generic = 'Couldn\'t add "x" to the phone render'
    assert beat_miss_sentence("x", "bind_failed") == generic
    assert beat_miss_sentence("x", "compile_dropped") == generic
    assert beat_miss_sentence("x", "error: boom") == generic


def test_subtitled_reaction_beats_zero_and_zero_has_no_detail() -> None:
    job = _job(
        pipeline_trace=[
            {
                "ts": datetime.now(UTC).isoformat(),
                "stage": "phone",
                "event": "subtitled_reaction_beats",
                "data": {"placed": 0, "unplaced": 0, "missed": [], "closing": "none"},
            }
        ],
    )
    steps = project_nova_steps(job)
    assert len(steps) == 1
    assert steps[0].label == "Kria timed your photos and sounds to your words"
    assert steps[0].detail is None


def test_subtitled_reaction_beats_missed_list_survives_sanitizer() -> None:
    """`missed` is a list, not a scalar -- confirms `_sanitize_event_data`'s
    KRI-178 extension (a flat list of scalars) actually keeps it, while a
    list containing a non-scalar item is still stripped entirely."""
    assert _sanitize_event_data({"missed": ["a", "b"], "placed": 1}) == {
        "missed": ["a", "b"],
        "placed": 1,
    }
    assert _sanitize_event_data({"missed": [{"nested": True}]}) == {}


def test_render_notes_from_beat_receipt_none_and_manual_and_empty() -> None:
    assert render_notes_from_beat_receipt(None) == []
    assert render_notes_from_beat_receipt({"matcher": "manual"}) == []
    assert render_notes_from_beat_receipt({"matcher": "phrase", "placed": [], "unplaced": []}) == []


def test_render_notes_from_beat_receipt_placed_and_missed_and_closing() -> None:
    """An unplaced beat forces the 'Placed N of M' summary; an unplaced
    closing whose reason is a no-room reason gets the specific sentence."""
    receipt = {
        "version": 1,
        "matcher": "phrase",
        "face_sampling": "ok",
        "placed": [{"beat_id": "beat-0", "trigger": "goal", "at_s": 1.0, "end_s": 2.0}],
        "unplaced": [{"beat_id": "beat-1", "trigger": "when he scores", "reason": "never_heard"}],
        "closing": {"status": "unplaced", "reason": "no_safe_spot", "badge": "none"},
    }
    assert render_notes_from_beat_receipt(receipt) == [
        "Placed 1 of 2 moments you named",
        'I never heard "when he scores", so its photo or sound wasn\'t shown',
        "Couldn't place the closing photo without covering your face or the captions",
    ]


def test_render_notes_from_beat_receipt_reason_specific_sentences() -> None:
    """Each unplaced beat's OWN `reason` picks a different sentence -- not
    the blanket "I never heard" the pre-fix code always used."""
    receipt = {
        "version": 1,
        "matcher": "phrase",
        "face_sampling": "ok",
        "placed": [],
        "unplaced": [
            {"beat_id": "beat-0", "trigger": "goal photo", "reason": "visual_not_in_pool"},
            {"beat_id": "beat-1", "trigger": "cheer sound", "reason": "sound_not_found"},
            {"beat_id": "beat-2", "trigger": "final whistle", "reason": "overlap"},
            {"beat_id": "beat-3", "trigger": "confetti", "reason": "compile_dropped"},
        ],
        "closing": {"status": "unplaced", "reason": "visual_not_in_pool", "badge": "none"},
    }
    assert render_notes_from_beat_receipt(receipt) == [
        "Placed 0 of 4 moments you named",
        'Couldn\'t find the photo or sticker for "goal photo" in your Visuals',
        'Couldn\'t find a sound for "cheer sound" in the library',
        'No room to show "final whistle" without covering your face or the captions',
        'Couldn\'t add "confetti" to the phone render',
        # A non-no-room closing reason keeps the generic sentence.
        "Couldn't place the closing photo",
    ]


def test_render_notes_from_beat_receipt_failed_matcher_yields_empty() -> None:
    receipt = {
        "version": 1,
        "matcher": "failed",
        "face_sampling": "skipped",
        "placed": [],
        "unplaced": [],
        "closing": {"status": "none", "badge": "none"},
        "error": "boom",
    }
    assert render_notes_from_beat_receipt(receipt) == []


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
    return await gj.get_generative_job_status(
        str(job.id), current_user=object(), http_response=Response(), db=object()
    )


async def test_status_route_steps_none_when_flag_off(monkeypatch: pytest.MonkeyPatch) -> None:
    resp = await _status_response_for(monkeypatch, flag_on=False)
    assert resp.steps is None


async def test_status_route_steps_populated_when_flag_on(monkeypatch: pytest.MonkeyPatch) -> None:
    resp = await _status_response_for(monkeypatch, flag_on=True)
    assert resp.steps is not None
    assert len(resp.steps) == 1
    assert resp.steps[0].kind == "phase"
