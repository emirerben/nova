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
