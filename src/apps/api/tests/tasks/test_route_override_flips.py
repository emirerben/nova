"""KRI-470 PR-F: retired heuristic overrides, through the REAL dispatchers.

Every override below used to let a legacy heuristic silently replace the approved plan.
For jobs STAMPED with ``creator_plan_authority_version`` the plan now wins (or a typed
decline is raised); UNSTAMPED jobs keep the legacy behaviour byte for byte.  Each flip has
a stamped test and an unstamped twin that drive the same entry point.

Failure modes written first:
  (1) the flip leaks to unstamped jobs (twin tests assert the legacy outcome);
  (2) a stamped job still silently reroutes (the stamped tests assert the approved route or
      a typed decline, never a different kind of edit);
  (3) a typed decline is raised without reason/field_path/alternative (so the creator would
      see an untyped crash);
  (4) the flip trusts a stray attachment or raw request text over the pinned contract.
"""

from __future__ import annotations

import pytest

from app.agents._schemas.creator_agent import CreativeStrategy
from app.services.creator_render_contract import (
    CONTRACT_FIELD,
    PLAN_AUTHORITY_FIELD,
    REQUIREMENT_VERSION_FIELD,
    build_render_contract,
)
from app.services.generative_jobs import CREATOR_RENDER_CONTRACT_VERSION
from app.tasks import generative_build as gb
from tests.tasks.test_phone_subtitled_narrated_dispatch import _setup_subtitled

VOICE_FILE = "voiceover-uploads/u/voice.m4a"


def _strategy(**fields) -> dict:
    return CreativeStrategy.model_validate(fields).model_dump(mode="json", exclude_none=True)


def _stamp(job, *, strategy: dict, generation: str = "generation") -> None:
    """Pin the approved contract + plan-authority stamp on a job (what dispatch writes)."""

    contract = build_render_contract(strategy, generation_id=generation)
    assert contract is not None
    job.assembly_plan[CONTRACT_FIELD] = contract.model_dump(mode="json")
    job.all_candidates.update(
        {
            PLAN_AUTHORITY_FIELD: 1,
            REQUIREMENT_VERSION_FIELD: 1,
            "creator_render_contract_version": CREATOR_RENDER_CONTRACT_VERSION,
            "creator_strategy": strategy,
        }
    )


# --- Flip 1: phone subtitled derives the voice from the contract ---------------------------


def _narrated_with_stray_recording(monkeypatch, *, stamped: bool):
    job, snapshot, _session, _binding = _setup_subtitled(monkeypatch, edit_format="narrated_ready")
    job.all_candidates["voiceover_gcs_path"] = VOICE_FILE
    resolved: list[tuple] = []

    def resolve(*args, **kwargs):  # noqa: ANN002, ANN003
        resolved.append((args, kwargs))
        return "subtitled", None, None

    monkeypatch.setattr(gb, "_resolve_archetype", resolve, raising=False)
    if stamped:
        # The approved plan uses the clip's own voice (library music, no recorded voice).
        _stamp(job, strategy=_strategy(edit_format="narrated_ready"))
    return job, snapshot, resolved


def test_stamped_narrated_format_with_a_stray_recording_still_renders_self_narration(
    monkeypatch,
) -> None:
    job, _snapshot, resolved = _narrated_with_stray_recording(monkeypatch, stamped=True)
    assert job.all_candidates["voiceover_gcs_path"] == VOICE_FILE
    gb._run_generative_job(str(job.id))
    # The dispatcher chose the subtitled worker from the contract, and the worker agrees:
    # it reaches the real archetype resolution instead of raising "No phone renderer".
    assert resolved, "the worker never reached post-ingest archetype resolution"
    assert job.status == "awaiting_device"
    assert job.assembly_plan["variants"][0]["resolved_archetype"] == "subtitled"


def test_unstamped_narrated_format_with_a_recording_keeps_the_legacy_file_decision(
    monkeypatch,
) -> None:
    job, snapshot, resolved = _narrated_with_stray_recording(monkeypatch, stamped=False)
    # Legacy: the attached file means "voiceover", which the subtitled worker cannot render.
    with pytest.raises(ValueError, match="No phone renderer is registered"):
        gb._run_phone_subtitled_job(str(job.id), snapshot, job.all_candidates, ownership_epoch=3)
    assert resolved == []


# --- Cloud harness: the real entry up to (and including) archetype resolution --------------


class _StopAfterResolution(Exception):  # noqa: N818
    """Raised from ``_set_status('rendering')``: everything after is render work."""


class CloudRun:
    """Drive ``_run_generative_job`` for a cloud job.

    Real: the entry gates, cloud preflight, guided-snapshot applicability, the typed-decline
    plumbing and ``_resolve_archetype``.  Replaced: ingest, the model agents and everything
    that renders (the first call after resolution raises ``_StopAfterResolution``).
    """

    def __init__(self, monkeypatch, *, strategy: dict, edit_format: str, stamped: bool, clips=3):
        import types
        import uuid
        from contextlib import contextmanager

        from tests.tasks.test_generative_build import _Meta, _Probe

        self.resolved: list[tuple] = []
        self.rendered = False
        candidates: dict = {
            "clip_paths": [f"users/u/clip-{i}.mp4" for i in range(clips)],
            "edit_format": edit_format,
            "creator_strategy": strategy,
            "creator_render_contract_version": CREATOR_RENDER_CONTRACT_VERSION,
            # Raw request text rides the job; no flip may read it.
            "creator_request": "USE-MY-WORDS",
        }
        assembly: dict = {"creator_generation_id": "gen-1"}
        self.job = types.SimpleNamespace(
            id=uuid.uuid4(),
            status="queued",
            mode="content_plan",
            assembly_plan=assembly,
            all_candidates=candidates,
            content_plan_item_id=uuid.uuid4(),
            user_id=uuid.uuid4(),
            raw_storage_path=None,
            error_detail=None,
            failure_reason=None,
        )
        if stamped:
            contract = build_render_contract(strategy, generation_id="gen-1")
            assert contract is not None
            assembly[CONTRACT_FIELD] = contract.model_dump(mode="json")
            candidates.update(
                {
                    PLAN_AUTHORITY_FIELD: 1,
                    REQUIREMENT_VERSION_FIELD: 1,
                }
            )

        @contextmanager
        def session():
            yield types.SimpleNamespace(commit=lambda: None)

        metas = [_Meta(f"c{i}", 5.0) for i in range(clips)]
        paths = {f"c{i}": candidates["clip_paths"][i] for i in range(clips)}
        monkeypatch.setattr(gb, "_sync_session", session)
        monkeypatch.setattr(gb, "_lock_owned_entry_job", lambda _db, _id: (self.job, None))
        monkeypatch.setattr(
            "app.services.creator_direction_snapshot.ensure_job_snapshot", lambda *a, **k: None
        )
        monkeypatch.setattr(gb, "mark_started", lambda _id: None)
        monkeypatch.setattr(gb, "record_phase", lambda *a, **k: None)
        monkeypatch.setattr(gb, "_emit_plan_blocks", lambda *a, **k: None)
        monkeypatch.setattr(gb, "_provision_clip_source_identity", lambda _id: None)
        monkeypatch.setattr(gb, "_speech_cleanup_identity_for_paths", lambda *a, **k: None)
        monkeypatch.setattr(gb, "_persist_durable_sources", lambda _id, p, **k: p)
        monkeypatch.setattr(
            gb,
            "_ingest_clips",
            lambda *a, **k: {
                "clip_metas": metas,
                "clip_id_to_gcs": paths,
                "clip_id_to_local": {k: f"/tmp/{k}.mp4" for k in paths},
                "probe_map": {f"/tmp/{k}.mp4": _Probe(6.0) for k in paths},
                "hero": metas[0],
            },
        )
        monkeypatch.setattr(gb, "_pretonemap_hdr_clips", lambda *a, **k: 0)
        monkeypatch.setattr(gb, "_run_text_agents", lambda *a, **k: ("Text", {}))
        monkeypatch.setattr(gb, "_select_generative_style_set", lambda *a, **k: "default")
        monkeypatch.setattr(gb, "_match_best_track", lambda *a, **k: None)
        monkeypatch.setattr("app.services.pipeline_trace.record_pipeline_event", lambda *a, **k: 0)
        monkeypatch.setattr(gb, "record_pipeline_event", lambda *a, **k: 0, raising=False)

        def stop(*_a, **_k):
            raise _StopAfterResolution

        monkeypatch.setattr(gb, "_set_status", stop)
        real_resolve = gb._resolve_archetype

        def recording_resolve(*args, **kwargs):  # noqa: ANN002, ANN003
            result = real_resolve(*args, **kwargs)
            self.resolved.append((args, kwargs, result))
            return result

        monkeypatch.setattr(gb, "_resolve_archetype", recording_resolve)
        # Wide-open rollout, except what a test turns off.
        for flag in (
            "narrated_archetype_enabled",
            "narrated_self_narration_enabled",
            "subtitled_archetype_enabled",
            "edit_format_talking_head_enabled",
            "edit_format_day_vlog_enabled",
            "edit_format_single_hero_enabled",
            "text_renderer_skia_enabled",
        ):
            monkeypatch.setattr(gb.settings, flag, True, raising=False)
        monkeypatch.setattr(gb.settings, "narrated_storyboard_enabled", False, raising=False)
        # Footage with speech on every clip (the speech probe is the only footage read).
        monkeypatch.setattr("app.services.clip_speech.speech_coverage", lambda _p: 0.9)

    def run(self, expect: type[BaseException] = _StopAfterResolution):
        """Run the real entry. Returns the resolved archetype (None when it never resolved)."""
        with pytest.raises(expect) as raised:
            gb._run_generative_job(str(self.job.id))
        self.raised = raised.value
        return self.resolved[-1][2][0] if self.resolved else None


def _cloud(monkeypatch, *, stamped: bool, edit_format: str = "montage", clips: int = 3, **strategy):
    strategy.setdefault("edit_format", edit_format)
    return CloudRun(
        monkeypatch,
        strategy=_strategy(**strategy),
        edit_format=edit_format,
        stamped=stamped,
        clips=clips,
    )


def _decline_of(exc: BaseException) -> dict:
    return gb._creator_decline_payload(exc)


# --- Flip 5: an attached recording does not pick the cloud route ---------------------------


def test_stamped_cloud_montage_with_a_stray_recording_stays_a_montage(monkeypatch) -> None:
    run = _cloud(monkeypatch, stamped=True)  # library music: the plan never asked for a voice
    run.job.all_candidates["voiceover_gcs_path"] = VOICE_FILE
    assert run.run() == "montage"
    # The route decision never saw the file: the worker's own variable is the plan's.
    assert run.resolved[-1][1]["voiceover_gcs_path"] is None


def test_unstamped_cloud_montage_with_a_recording_is_still_a_voiceover(monkeypatch) -> None:
    run = _cloud(monkeypatch, stamped=False)
    run.job.all_candidates["voiceover_gcs_path"] = VOICE_FILE
    assert run.run() == "narrated"  # content_plan montage + recording (legacy, out of scope)
    assert run.resolved[-1][1]["voiceover_gcs_path"] == VOICE_FILE


def test_stamped_cloud_plan_that_needs_a_voice_with_no_recording_asks_for_it(monkeypatch) -> None:
    from app.services.cloud_render_contract import CloudRenderContractError

    run = _cloud(monkeypatch, stamped=True, audio_strategy="voiceover")
    assert run.run(CloudRenderContractError) is None  # before any archetype resolution
    assert _decline_of(run.raised) == {
        "decline_reason": "needs_choice",
        "field_path": "audio_strategy",
        "alternative": "Record or upload your voice, or tell me to use music instead.",
    }


def test_unstamped_cloud_voice_plan_with_no_recording_keeps_rendering_without_it(
    monkeypatch,
) -> None:
    run = _cloud(monkeypatch, stamped=False, audio_strategy="voiceover")
    assert run.run() == "montage"


def test_stamped_cloud_plan_with_its_recording_resolves_the_voice_route(monkeypatch) -> None:
    run = _cloud(monkeypatch, stamped=True, audio_strategy="voiceover")
    run.job.all_candidates["voiceover_gcs_path"] = VOICE_FILE
    assert run.run() == "narrated"
    assert run.resolved[-1][1]["voiceover_gcs_path"] == VOICE_FILE


# --- Flip 6: a guided snapshot never silently becomes the classic path ---------------------


def _guided_snapshot(run: CloudRun) -> dict:
    return {
        "proposal_version": 1,
        "media_digest": "a" * 64,
        "approved_proposal": {"title": "Run"},
        "media_identities": [
            {"lane": "clip", "gcs_path": path} for path in run.job.all_candidates["clip_paths"]
        ],
    }


def _guided_cloud(monkeypatch, *, stamped: bool, edit_format: str = "montage", **strategy):
    strategy.setdefault("render_program", "guided")
    run = _cloud(monkeypatch, stamped=stamped, edit_format=edit_format, **strategy)
    run.job.assembly_plan["guided_edit"] = _guided_snapshot(run)
    run.guided_calls = []
    monkeypatch.setattr(
        gb, "_run_guided_story_job", lambda *a, **k: run.guided_calls.append((a, k)), raising=False
    )
    return run


def test_stamped_guided_plan_with_a_stray_recording_stays_guided(monkeypatch) -> None:
    run = _guided_cloud(monkeypatch, stamped=True)
    run.job.all_candidates["voiceover_gcs_path"] = VOICE_FILE
    gb._run_generative_job(str(run.job.id))  # no archetype resolution: the guided runner owns it
    assert len(run.guided_calls) == 1 and run.resolved == []
    from app.services.cloud_render_contract import cloud_adapter_for_job

    assert cloud_adapter_for_job(run.job.assembly_plan, run.job.all_candidates) == (
        "cloud_guided_story"
    )


def test_unstamped_guided_plan_with_a_recording_skips_to_the_classic_voice_route(
    monkeypatch,
) -> None:
    run = _guided_cloud(monkeypatch, stamped=False)
    run.job.all_candidates["voiceover_gcs_path"] = VOICE_FILE
    assert run.run() == "narrated"
    assert run.guided_calls == []
    from app.services.cloud_render_contract import cloud_adapter_for_job

    assert cloud_adapter_for_job(run.job.assembly_plan, run.job.all_candidates) == "cloud_classic"


def test_stamped_guided_snapshot_on_an_audio_led_format_is_a_typed_decline(monkeypatch) -> None:
    from app.services.cloud_render_contract import CloudRenderContractError

    run = _guided_cloud(
        monkeypatch, stamped=True, edit_format="talking_head", render_program="native"
    )
    assert run.run(CloudRenderContractError) is None
    decline = _decline_of(run.raised)
    assert decline["decline_reason"] == "requirement_conflict"
    assert decline["field_path"] == "edit_format"
    assert "guided story" in decline["alternative"]
    assert run.guided_calls == [] and run.resolved == []


def test_unstamped_guided_snapshot_on_an_audio_led_format_still_skips_to_classic(
    monkeypatch,
) -> None:
    run = _guided_cloud(
        monkeypatch, stamped=False, edit_format="talking_head", render_program="native"
    )
    assert run.run() == "talking_head"
    assert run.guided_calls == []


def _voiceover_execution_strategy() -> dict:
    return {
        "audio_strategy": "voiceover",
        "render_program": "guided",
        "execution_contract": "guided_voiceover_v1",
    }


def test_stamped_voiceover_execution_binding_that_no_longer_holds_is_a_typed_repair(
    monkeypatch,
) -> None:
    from app.services.cloud_render_contract import CloudRenderContractError

    run = _guided_cloud(monkeypatch, stamped=True, **_voiceover_execution_strategy())
    run.job.all_candidates["voiceover_gcs_path"] = VOICE_FILE
    assert run.run(CloudRenderContractError) is None
    decline = _decline_of(run.raised)
    assert decline["decline_reason"] == "evidence_missing"
    assert decline["field_path"] == "audio_strategy"


def test_unstamped_voiceover_execution_binding_that_no_longer_holds_keeps_the_value_error(
    monkeypatch,
) -> None:
    run = _guided_cloud(monkeypatch, stamped=False, **_voiceover_execution_strategy())
    run.job.all_candidates["voiceover_gcs_path"] = VOICE_FILE
    run.run(ValueError)
    assert type(run.raised) is ValueError
    assert "voiceover" in str(run.raised)
