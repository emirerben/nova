from __future__ import annotations

import uuid
from contextlib import nullcontext

import pytest

from app.config import settings
from app.tasks import autoplace

JOB_ID = "11111111-1111-1111-1111-111111111111"


class _Result:
    def __init__(self, rows: list):
        self.rows = rows

    def scalars(self):
        return self

    def all(self):
        return self.rows


class _Job:
    def __init__(self):
        self.content_plan_item_id = uuid.uuid4()
        self.assembly_plan = {
            "variants": [
                {
                    "variant_id": "subtitled",
                    "text_mode": "agent_text",
                    "base_video_path": "users/u/clean-base.mp4",
                    "video_path": "users/u/rendered.mp4",
                    "duration_s": 134.442,
                    "resolved_archetype": "subtitled",
                }
            ]
        }


class _Asset:
    def __init__(self):
        self.id = uuid.uuid4()
        self.gcs_path = f"users/u/plan/i/pool/{self.id}.jpg"
        self.kind = "image"
        self.analysis = {"subject": "source frame", "description": "speaker"}


class _Session:
    def __init__(self, job: _Job, assets: list[_Asset]):
        self.job = job
        self.assets = assets

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return False

    def get(self, _model, _pk, **_kwargs):
        return self.job

    def execute(self, *_args, **_kwargs):
        return _Result(self.assets)

    def commit(self):
        return None


def test_asset_preparation_happens_once_before_variant_fanout(monkeypatch) -> None:
    events: list[tuple[str, object]] = []
    monkeypatch.setattr(settings, "visual_blocks_enabled", True)
    monkeypatch.setattr(settings, "visual_block_autoplan_enabled", True)
    monkeypatch.setattr(
        autoplace,
        "_materialize_extracted_frames",
        lambda job_id: events.append(("extract", job_id)),
    )
    monkeypatch.setattr(
        autoplace.plan_visual_blocks,
        "apply_async",
        lambda *, args, queue: events.append(("plan", (args, queue))),
    )
    monkeypatch.setattr(
        "app.services.pipeline_trace.pipeline_trace_for",
        lambda _job_id: nullcontext(),
    )

    autoplace.prepare_visual_block_assets.run("job-1", ["variant-a", "variant-b"])

    assert events[0] == ("extract", "job-1")
    assert [event[0] for event in events] == ["extract", "plan", "plan"]


def test_real_planner_branch_persists_long_video_card_transcript_and_dispatches(
    monkeypatch,
) -> None:
    from app.agents.visual_treatment_planner import (
        RawVisualTreatment,
        VisualTreatmentPlannerOutput,
    )

    job = _Job()
    job.assembly_plan["variants"][0]["visual_blocks_autoplan_attempted"] = True
    monkeypatch.setattr(settings, "visual_blocks_enabled", True)
    monkeypatch.setattr(settings, "visual_block_autoplan_enabled", True)
    monkeypatch.setattr(settings, "gemini_api_key", "gemini-test")
    monkeypatch.setattr(autoplace, "_sync_session", lambda: _Session(job, [_Asset()]))
    monkeypatch.setattr(
        "app.services.pipeline_trace.pipeline_trace_for", lambda _job_id: nullcontext()
    )
    monkeypatch.setattr("sqlalchemy.orm.attributes.flag_modified", lambda *_args: None)

    words = [
        {"word": "Dört,", "start_s": 101.68, "end_s": 102.28},
        {"word": "Storytelling.", "start_s": 102.58, "end_s": 103.42},
        {"word": "Yani hikâye anlatıcılığı.", "start_s": 103.70, "end_s": 105.32},
    ]
    monkeypatch.setattr(
        "app.services.transcript_source.transcript_source",
        lambda _v, **_kwargs: (words, "transcript-hash"),
    )
    monkeypatch.setattr("app.agents._model_client.default_client", lambda: object())
    seen_duration: list[float] = []

    def _run(_self, planner_input, **_kwargs):
        seen_duration.append(planner_input.duration_s)
        return VisualTreatmentPlannerOutput(
            treatments=[
                RawVisualTreatment(
                    kind="text_card",
                    purpose="section_item",
                    start_s=101.0,
                    end_s=104.0,
                    text="4. Storytelling",
                    confidence="high",
                )
            ]
        )

    monkeypatch.setattr("app.agents.visual_treatment_planner.VisualTreatmentPlannerAgent.run", _run)
    dispatched: list[tuple[list, dict, str]] = []
    monkeypatch.setattr(
        "app.tasks.generative_build.regenerate_generative_variant.apply_async",
        lambda *, args, kwargs, queue: dispatched.append((args, kwargs, queue)),
    )

    autoplace.plan_visual_blocks.run(JOB_ID, "subtitled")

    variant = job.assembly_plan["variants"][0]
    assert seen_duration == [134.442]
    assert variant["overlay_transcript"] == words
    assert variant["visual_blocks"][0]["start_s"] == 101.68
    assert variant["visual_blocks"][0]["end_s"] == 103.42
    assert variant["text_elements"][0]["text"] == "4. Storytelling"
    assert variant["render_status"] == "rendering"
    assert dispatched[0][0] == [JOB_ID, "subtitled"]
    assert dispatched[0][2] == "overlay-jobs"


@pytest.mark.parametrize(
    ("planner_error", "expected_events"),
    [
        (None, ["visual_blocks_plan_zero"]),
        (RuntimeError("model unavailable"), ["visual_blocks_planner_failed"]),
    ],
)
def test_zero_card_persists_whisper_and_distinguishes_planner_failure(
    monkeypatch,
    planner_error,
    expected_events,
) -> None:
    from app.agents.visual_treatment_planner import VisualTreatmentPlannerOutput

    job = _Job()
    job.assembly_plan["variants"][0]["visual_blocks_autoplan_attempted"] = True
    monkeypatch.setattr(settings, "visual_blocks_enabled", True)
    monkeypatch.setattr(settings, "visual_block_autoplan_enabled", True)
    monkeypatch.setattr(settings, "gemini_api_key", "gemini-test")
    monkeypatch.setattr(autoplace, "_sync_session", lambda: _Session(job, []))
    monkeypatch.setattr(
        "app.services.pipeline_trace.pipeline_trace_for", lambda _job_id: nullcontext()
    )
    monkeypatch.setattr("sqlalchemy.orm.attributes.flag_modified", lambda *_args: None)
    words = [{"word": "No structured list here", "start_s": 1.0, "end_s": 2.0}]
    monkeypatch.setattr(
        "app.services.transcript_source.transcript_source",
        lambda _v, **_kwargs: (words, "transcript-hash"),
    )
    monkeypatch.setattr("app.agents._model_client.default_client", lambda: object())

    def _run(_self, _planner_input, **_kwargs):
        if planner_error is not None:
            raise planner_error
        return VisualTreatmentPlannerOutput(treatments=[])

    monkeypatch.setattr("app.agents.visual_treatment_planner.VisualTreatmentPlannerAgent.run", _run)
    events: list[str] = []
    monkeypatch.setattr(autoplace, "_record", lambda event, **_fields: events.append(event))

    autoplace.plan_visual_blocks.run(JOB_ID, "subtitled")

    assert job.assembly_plan["variants"][0]["overlay_transcript"] == words
    assert job.assembly_plan["variants"][0]["visual_blocks_autoplan_attempted"] is (
        planner_error is None
    )
    assert events == expected_events


def test_render_dispatch_failure_rolls_back_authored_state(monkeypatch) -> None:
    from app.agents.visual_treatment_planner import (
        RawVisualTreatment,
        VisualTreatmentPlannerOutput,
    )

    job = _Job()
    variant = job.assembly_plan["variants"][0]
    variant.update(
        {
            "text_elements": [{"id": "existing-text", "text": "Keep me"}],
            "text_elements_user_edited": False,
            "render_generation_id": "previous-generation",
            "render_status": "ready",
        }
    )
    monkeypatch.setattr(settings, "visual_blocks_enabled", True)
    monkeypatch.setattr(settings, "visual_block_autoplan_enabled", True)
    monkeypatch.setattr(settings, "gemini_api_key", "gemini-test")
    monkeypatch.setattr(autoplace, "_sync_session", lambda: _Session(job, []))
    monkeypatch.setattr(
        "app.services.pipeline_trace.pipeline_trace_for", lambda _job_id: nullcontext()
    )
    monkeypatch.setattr("sqlalchemy.orm.attributes.flag_modified", lambda *_args: None)
    words = [
        {"word": "Dört", "start_s": 101.68, "end_s": 102.28},
        {"word": "Storytelling", "start_s": 102.58, "end_s": 103.42},
    ]
    monkeypatch.setattr(
        "app.services.transcript_source.transcript_source",
        lambda _v, **_kwargs: (words, "transcript-hash"),
    )
    monkeypatch.setattr("app.agents._model_client.default_client", lambda: object())
    monkeypatch.setattr(
        "app.agents.visual_treatment_planner.VisualTreatmentPlannerAgent.run",
        lambda _self, _input, **_kwargs: VisualTreatmentPlannerOutput(
            treatments=[
                RawVisualTreatment(
                    kind="text_card",
                    purpose="section_item",
                    start_s=101.0,
                    end_s=104.0,
                    text="4. Storytelling",
                    confidence="high",
                )
            ]
        ),
    )
    monkeypatch.setattr(
        "app.tasks.generative_build.regenerate_generative_variant.apply_async",
        lambda **_kwargs: (_ for _ in ()).throw(RuntimeError("broker unavailable")),
    )
    events: list[str] = []
    monkeypatch.setattr(autoplace, "_record", lambda event, **_fields: events.append(event))

    with pytest.raises(RuntimeError, match="broker unavailable"):
        autoplace.plan_visual_blocks.run(JOB_ID, "subtitled")

    assert variant["overlay_transcript"] == words
    assert "visual_blocks" not in variant
    assert variant["text_elements"] == [{"id": "existing-text", "text": "Keep me"}]
    assert variant["text_elements_user_edited"] is False
    assert variant["render_status"] == "ready"
    assert variant["render_generation_id"] == "previous-generation"
    assert variant["visual_blocks_autoplan_attempted"] is False
    assert events == ["visual_blocks_render_dispatch_failed"]


def test_successful_zero_treatments_do_not_trigger_fallback_montage(monkeypatch) -> None:
    from app.agents.visual_treatment_planner import VisualTreatmentPlannerOutput

    job = _Job()
    monkeypatch.setattr(settings, "visual_blocks_enabled", True)
    monkeypatch.setattr(settings, "visual_block_autoplan_enabled", True)
    monkeypatch.setattr(settings, "gemini_api_key", "gemini-test")
    monkeypatch.setattr(autoplace, "_sync_session", lambda: _Session(job, [_Asset()] * 3))
    monkeypatch.setattr(
        "app.services.pipeline_trace.pipeline_trace_for", lambda _job_id: nullcontext()
    )
    monkeypatch.setattr("sqlalchemy.orm.attributes.flag_modified", lambda *_args: None)
    words = [{"word": "Ordinary explanation", "start_s": 1.0, "end_s": 2.0}]
    monkeypatch.setattr(
        "app.services.transcript_source.transcript_source",
        lambda _v, **_kwargs: (words, "transcript-hash"),
    )
    monkeypatch.setattr("app.agents._model_client.default_client", lambda: object())
    monkeypatch.setattr(
        "app.agents.visual_treatment_planner.VisualTreatmentPlannerAgent.run",
        lambda _self, _input, **_kwargs: VisualTreatmentPlannerOutput(treatments=[]),
    )
    monkeypatch.setattr(
        "app.tasks.generative_build.regenerate_generative_variant.apply_async",
        lambda **_kwargs: pytest.fail("successful zero result must not dispatch"),
    )
    events: list[str] = []
    monkeypatch.setattr(autoplace, "_record", lambda event, **_fields: events.append(event))

    autoplace.plan_visual_blocks.run(JOB_ID, "subtitled")

    variant = job.assembly_plan["variants"][0]
    assert "visual_blocks" not in variant
    assert variant["overlay_transcript"] == words
    assert events == ["visual_blocks_plan_zero"]


def test_stale_planning_revision_does_not_mutate_or_dispatch(monkeypatch) -> None:
    from app.agents.visual_treatment_planner import (
        RawVisualTreatment,
        VisualTreatmentPlannerOutput,
    )

    job = _Job()
    monkeypatch.setattr(settings, "visual_blocks_enabled", True)
    monkeypatch.setattr(settings, "visual_block_autoplan_enabled", True)
    monkeypatch.setattr(settings, "gemini_api_key", "gemini-test")
    monkeypatch.setattr(autoplace, "_sync_session", lambda: _Session(job, []))
    monkeypatch.setattr(
        "app.services.pipeline_trace.pipeline_trace_for", lambda _job_id: nullcontext()
    )
    monkeypatch.setattr("sqlalchemy.orm.attributes.flag_modified", lambda *_args: None)
    words = [
        {"word": "Four", "start_s": 101.68, "end_s": 102.28},
        {"word": "Storytelling", "start_s": 102.58, "end_s": 103.42},
    ]
    monkeypatch.setattr(
        "app.services.transcript_source.transcript_source",
        lambda _v, **_kwargs: (words, "transcript-hash"),
    )
    monkeypatch.setattr("app.agents._model_client.default_client", lambda: object())

    def _run(_self, _input, **_kwargs):
        job.assembly_plan["variants"][0]["render_generation_id"] = "newer-user-render"
        return VisualTreatmentPlannerOutput(
            treatments=[
                RawVisualTreatment(
                    kind="text_card",
                    purpose="section_item",
                    start_s=101.0,
                    end_s=104.0,
                    text="4. Storytelling",
                    confidence="high",
                )
            ]
        )

    monkeypatch.setattr("app.agents.visual_treatment_planner.VisualTreatmentPlannerAgent.run", _run)
    monkeypatch.setattr(
        "app.tasks.generative_build.regenerate_generative_variant.apply_async",
        lambda **_kwargs: pytest.fail("stale plan must not dispatch"),
    )
    events: list[str] = []
    monkeypatch.setattr(autoplace, "_record", lambda event, **_fields: events.append(event))

    autoplace.plan_visual_blocks.run(JOB_ID, "subtitled")

    variant = job.assembly_plan["variants"][0]
    assert "visual_blocks" not in variant
    assert variant["render_generation_id"] == "newer-user-render"
    assert variant["visual_blocks_autoplan_attempted"] is False
    assert events == ["visual_blocks_plan_stale"]


def test_concurrent_transcript_correction_is_used_and_never_overwritten(monkeypatch) -> None:
    from app.agents.visual_treatment_planner import VisualTreatmentPlannerOutput

    job = _Job()
    monkeypatch.setattr(settings, "visual_blocks_enabled", True)
    monkeypatch.setattr(settings, "visual_block_autoplan_enabled", True)
    monkeypatch.setattr(settings, "gemini_api_key", "gemini-test")
    monkeypatch.setattr(autoplace, "_sync_session", lambda: _Session(job, []))
    monkeypatch.setattr(
        "app.services.pipeline_trace.pipeline_trace_for", lambda _job_id: nullcontext()
    )
    monkeypatch.setattr("sqlalchemy.orm.attributes.flag_modified", lambda *_args: None)
    whisper_words = [{"word": "Wrong title", "start_s": 1.0, "end_s": 2.0}]
    corrected_words = [{"word": "Correct title", "start_s": 1.0, "end_s": 2.0}]

    def _transcript_source(variant, **_kwargs):
        variant["transcript"] = corrected_words
        return whisper_words, "whisper-hash"

    monkeypatch.setattr("app.services.transcript_source.transcript_source", _transcript_source)
    monkeypatch.setattr("app.agents._model_client.default_client", lambda: object())
    seen_words: list[list[dict]] = []

    def _run(_self, planner_input, **_kwargs):
        seen_words.append(planner_input.words)
        return VisualTreatmentPlannerOutput(treatments=[])

    monkeypatch.setattr("app.agents.visual_treatment_planner.VisualTreatmentPlannerAgent.run", _run)

    autoplace.plan_visual_blocks.run(JOB_ID, "subtitled")

    variant = job.assembly_plan["variants"][0]
    assert variant["transcript"] == corrected_words
    assert "overlay_transcript" not in variant
    assert seen_words == [corrected_words]


def test_stale_successful_zero_releases_claim_without_recording_zero(monkeypatch) -> None:
    from app.agents.visual_treatment_planner import VisualTreatmentPlannerOutput

    job = _Job()
    monkeypatch.setattr(settings, "visual_blocks_enabled", True)
    monkeypatch.setattr(settings, "visual_block_autoplan_enabled", True)
    monkeypatch.setattr(settings, "gemini_api_key", "gemini-test")
    monkeypatch.setattr(autoplace, "_sync_session", lambda: _Session(job, []))
    monkeypatch.setattr(
        "app.services.pipeline_trace.pipeline_trace_for", lambda _job_id: nullcontext()
    )
    monkeypatch.setattr("sqlalchemy.orm.attributes.flag_modified", lambda *_args: None)
    words = [{"word": "Ordinary explanation", "start_s": 1.0, "end_s": 2.0}]
    monkeypatch.setattr(
        "app.services.transcript_source.transcript_source",
        lambda _v, **_kwargs: (words, "transcript-hash"),
    )
    monkeypatch.setattr("app.agents._model_client.default_client", lambda: object())

    def _run(_self, _input, **_kwargs):
        job.assembly_plan["variants"][0]["render_generation_id"] = "newer-user-render"
        return VisualTreatmentPlannerOutput(treatments=[])

    monkeypatch.setattr("app.agents.visual_treatment_planner.VisualTreatmentPlannerAgent.run", _run)
    events: list[str] = []
    monkeypatch.setattr(autoplace, "_record", lambda event, **_fields: events.append(event))

    autoplace.plan_visual_blocks.run(JOB_ID, "subtitled")

    variant = job.assembly_plan["variants"][0]
    assert variant["visual_blocks_autoplan_attempted"] is False
    assert events == ["visual_blocks_plan_stale"]


def test_invalid_authoritative_transcript_is_terminal_without_whisper_loop(monkeypatch) -> None:
    job = _Job()
    variant = job.assembly_plan["variants"][0]
    variant["transcript"] = [{"word": "Heading", "start_s": "bad", "end_s": 1.0}]
    variant["visual_blocks_autoplan_attempted"] = True
    monkeypatch.setattr(settings, "visual_blocks_enabled", True)
    monkeypatch.setattr(settings, "visual_block_autoplan_enabled", True)
    monkeypatch.setattr(autoplace, "_sync_session", lambda: _Session(job, []))
    monkeypatch.setattr(
        "app.services.pipeline_trace.pipeline_trace_for", lambda _job_id: nullcontext()
    )
    monkeypatch.setattr(
        "app.services.transcript_source.transcript_source",
        lambda *_args, **_kwargs: pytest.fail("invalid authoritative transcript must fail closed"),
    )
    events: list[str] = []
    monkeypatch.setattr(autoplace, "_record", lambda event, **_fields: events.append(event))

    autoplace.plan_visual_blocks.run(JOB_ID, "subtitled")
    autoplace.plan_visual_blocks.run(JOB_ID, "subtitled")

    assert "overlay_transcript" not in variant
    assert variant["visual_blocks_autoplan_attempted"] is True
    assert events == ["visual_blocks_transcript_invalid", "visual_blocks_transcript_invalid"]


def test_prepare_failure_releases_unqueued_variant_claims(monkeypatch) -> None:
    job = _Job()
    variant = job.assembly_plan["variants"][0]
    variant["visual_blocks_autoplan_attempted"] = True
    monkeypatch.setattr(settings, "visual_blocks_enabled", True)
    monkeypatch.setattr(settings, "visual_block_autoplan_enabled", True)
    monkeypatch.setattr(autoplace, "_sync_session", lambda: _Session(job, []))
    monkeypatch.setattr(
        "app.services.pipeline_trace.pipeline_trace_for", lambda _job_id: nullcontext()
    )
    monkeypatch.setattr("sqlalchemy.orm.attributes.flag_modified", lambda *_args: None)
    monkeypatch.setattr(
        autoplace,
        "_materialize_extracted_frames",
        lambda _job_id: (_ for _ in ()).throw(RuntimeError("extract failed")),
    )

    with pytest.raises(RuntimeError, match="extract failed"):
        autoplace.prepare_visual_block_assets.run(JOB_ID, ["subtitled"])

    assert variant["visual_blocks_autoplan_attempted"] is False


def test_initial_chain_enqueue_failure_releases_claim(monkeypatch) -> None:
    from app.tasks import generative_build

    job = _Job()
    variant = job.assembly_plan["variants"][0]
    variant["render_status"] = "ready"
    monkeypatch.setattr(settings, "visual_blocks_enabled", True)
    monkeypatch.setattr(settings, "visual_block_autoplan_enabled", True)
    monkeypatch.setattr(generative_build, "_sync_session", lambda: _Session(job, []))
    monkeypatch.setattr(autoplace, "_sync_session", lambda: _Session(job, []))
    monkeypatch.setattr("sqlalchemy.orm.attributes.flag_modified", lambda *_args: None)
    monkeypatch.setattr(
        autoplace.prepare_visual_block_assets,
        "apply_async",
        lambda **_kwargs: (_ for _ in ()).throw(RuntimeError("broker unavailable")),
    )

    with pytest.raises(RuntimeError, match="broker unavailable"):
        generative_build._maybe_visual_blocks_after_finalize(JOB_ID)

    assert variant["visual_blocks_autoplan_attempted"] is False


def test_beat_only_plan_treats_absent_transcript_as_stable(monkeypatch) -> None:
    from app.agents.visual_treatment_planner import (
        RawVisualTreatment,
        VisualTreatmentPlannerOutput,
    )

    job = _Job()
    variant = job.assembly_plan["variants"][0]
    variant["beat_grid"] = [0.0, 0.5, 1.0, 1.5]
    variant["visual_blocks_autoplan_attempted"] = True
    assets = [_Asset(), _Asset(), _Asset()]
    monkeypatch.setattr(settings, "visual_blocks_enabled", True)
    monkeypatch.setattr(settings, "visual_block_autoplan_enabled", True)
    monkeypatch.setattr(settings, "gemini_api_key", "gemini-test")
    monkeypatch.setattr(autoplace, "_sync_session", lambda: _Session(job, assets))
    monkeypatch.setattr(
        "app.services.pipeline_trace.pipeline_trace_for", lambda _job_id: nullcontext()
    )
    monkeypatch.setattr("sqlalchemy.orm.attributes.flag_modified", lambda *_args: None)
    monkeypatch.setattr(
        "app.services.transcript_source.transcript_source", lambda _v, **_kwargs: None
    )
    monkeypatch.setattr("app.agents._model_client.default_client", lambda: object())
    monkeypatch.setattr(
        "app.agents.visual_treatment_planner.VisualTreatmentPlannerAgent.run",
        lambda _self, _input, **_kwargs: VisualTreatmentPlannerOutput(
            treatments=[
                RawVisualTreatment(
                    kind="montage",
                    purpose="hook",
                    start_s=0.0,
                    end_s=2.0,
                    asset_ids=[str(asset.id) for asset in assets],
                    confidence="high",
                )
            ]
        ),
    )
    dispatched: list[bool] = []
    monkeypatch.setattr(
        "app.tasks.generative_build.regenerate_generative_variant.apply_async",
        lambda **_kwargs: dispatched.append(True),
    )

    autoplace.plan_visual_blocks.run(JOB_ID, "subtitled")

    assert variant["visual_blocks"][0]["kind"] == "montage"
    assert dispatched == [True]


def test_prepare_flag_off_releases_claim(monkeypatch) -> None:
    job = _Job()
    variant = job.assembly_plan["variants"][0]
    variant["visual_blocks_autoplan_attempted"] = True
    monkeypatch.setattr(settings, "visual_blocks_enabled", True)
    monkeypatch.setattr(settings, "visual_block_autoplan_enabled", False)
    monkeypatch.setattr(autoplace, "_sync_session", lambda: _Session(job, []))
    monkeypatch.setattr("sqlalchemy.orm.attributes.flag_modified", lambda *_args: None)

    autoplace.prepare_visual_block_assets.run(JOB_ID, ["subtitled"])

    assert variant["visual_blocks_autoplan_attempted"] is False


def test_planner_flag_off_releases_claim(monkeypatch) -> None:
    job = _Job()
    variant = job.assembly_plan["variants"][0]
    variant["visual_blocks_autoplan_attempted"] = True
    monkeypatch.setattr(settings, "visual_blocks_enabled", True)
    monkeypatch.setattr(settings, "visual_block_autoplan_enabled", False)
    monkeypatch.setattr(autoplace, "_sync_session", lambda: _Session(job, []))
    monkeypatch.setattr("sqlalchemy.orm.attributes.flag_modified", lambda *_args: None)

    autoplace.plan_visual_blocks.run(JOB_ID, "subtitled")

    assert variant["visual_blocks_autoplan_attempted"] is False
