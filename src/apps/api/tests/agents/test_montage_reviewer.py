from __future__ import annotations

import json
from types import SimpleNamespace

import pytest

import app.tasks.edit_proposal_build as proposal_build
from app.agents._runtime import SchemaError
from app.agents.montage_reviewer import (
    MontageReviewAgent,
    MontageReviewInput,
)
from app.schemas.edit_proposal import MediaRef


def _input() -> MontageReviewInput:
    return MontageReviewInput(
        file_uri="files/source-a",
        source_media_id="source-a",
        source_duration_s=12,
        creator_request="Use the strongest action from this match.",
        proposed_cuts=[
            {
                "cut_id": "cut-1",
                "media_id": "source-a",
                "source_start_s": 2,
                "source_end_s": 3,
                "output_duration_s": 1,
            },
            {
                "cut_id": "cut-2",
                "media_id": "source-a",
                "source_start_s": 7,
                "source_end_s": 8,
                "output_duration_s": 1,
            },
        ],
    )


def _raw(*, cut_ids: list[str] | None = None) -> str:
    ids = cut_ids or ["cut-1", "cut-2"]
    return json.dumps(
        {
            "overall_score": 8,
            "needs_replan": False,
            "cut_reviews": [
                {
                    "cut_id": cut_id,
                    "keep": True,
                    "quality_score": 8,
                    "observed_action": "shot on goal",
                    "feedback": "Clear action with a decisive payoff.",
                }
                for cut_id in ids
            ],
            "summary": "Both windows are strong.",
        }
    )


def test_reviewer_requires_one_review_per_proposed_cut() -> None:
    agent = MontageReviewAgent(None)  # type: ignore[arg-type]
    with pytest.raises(SchemaError, match="every proposed cut"):
        agent.parse(_raw(cut_ids=["cut-1"]), _input())

    duplicate_reviews = json.loads(_raw())
    duplicate_reviews["cut_reviews"][1]["cut_id"] = "cut-1"
    with pytest.raises(SchemaError, match="every proposed cut"):
        agent.parse(json.dumps(duplicate_reviews), _input())


def test_reviewer_accepts_valid_replacement_window() -> None:
    agent = MontageReviewAgent(None)  # type: ignore[arg-type]
    payload = json.loads(_raw())
    payload["cut_reviews"][0].update(
        {
            "keep": False,
            "quality_score": 3,
            "feedback": "The frame is mostly setup.",
            "replacement_start_s": 4,
            "replacement_end_s": 5,
        }
    )

    output = agent.parse(json.dumps(payload), _input())

    assert output.cut_reviews[0].replacement_start_s == 4
    assert output.cut_reviews[0].replacement_end_s == 5


def test_reviewer_rejects_replacement_with_wrong_duration() -> None:
    agent = MontageReviewAgent(None)  # type: ignore[arg-type]
    payload = json.loads(_raw())
    payload["cut_reviews"][0].update({"replacement_start_s": 4, "replacement_end_s": 6})

    with pytest.raises(SchemaError, match="replacement duration"):
        agent.parse(json.dumps(payload), _input())


def test_reviewer_prompt_contains_source_timestamps_and_request() -> None:
    prompt = MontageReviewAgent(None).render_prompt(_input())  # type: ignore[arg-type]

    assert "source-a" in prompt
    assert "shot" not in prompt
    assert "2.0" in prompt
    assert "Use the strongest action" in prompt


def test_review_feedback_contains_only_weak_cut_findings() -> None:
    feedback = proposal_build._montage_review_feedback(
        [
            (
                "source-a",
                SimpleNamespace(
                    needs_replan=True,
                    summary="The second window is mostly setup.",
                    cut_reviews=[
                        SimpleNamespace(
                            cut_id="cut-1",
                            keep=True,
                            quality_score=8,
                            observed_action="shot on goal",
                            feedback="Clear action.",
                            replacement_start_s=None,
                            replacement_end_s=None,
                        ),
                        SimpleNamespace(
                            cut_id="cut-2",
                            keep=False,
                            quality_score=3,
                            observed_action="players waiting",
                            feedback="No decisive action.",
                            replacement_start_s=5,
                            replacement_end_s=6,
                        ),
                    ],
                ),
            )
        ]
    )

    assert "cut-2" in feedback
    assert "cut-1" not in feedback
    assert "replacement_start_s" in feedback


def _source(media_id: str) -> MediaRef:
    return MediaRef(
        lane="clip",
        media_id=media_id,
        gcs_path=f"users/u/{media_id}.mp4",
        generation="1",
        kind="video",
        duration_s=12,
        analysis={"best_moments": [{"start_s": 4, "end_s": 5, "energy": "high"}]},
    )


def _authored_output() -> SimpleNamespace:
    return SimpleNamespace(
        fast_cuts=[
            SimpleNamespace(
                cut_id="cut-a",
                media_id="source-a",
                source_start_s=2,
                source_end_s=3,
                output_duration_s=1,
            ),
            SimpleNamespace(
                cut_id="cut-b",
                media_id="source-b",
                source_start_s=6,
                source_end_s=7,
                output_duration_s=1,
            ),
        ]
    )


def test_authored_montage_review_is_per_source_and_fail_open(monkeypatch) -> None:  # noqa: ANN001
    uploaded = SimpleNamespace(uri="files/review-source")
    calls: list[tuple[str, str | None]] = []

    class FakeClient:
        def upload_media(self, path: str):  # noqa: ANN001
            return uploaded

    class FakeReviewer:
        def __init__(self, client):  # noqa: ANN001, ARG002
            pass

        def run(self, review_input, *, ctx=None):  # noqa: ANN001
            calls.append((review_input.source_media_id, ctx.job_id if ctx else None))
            return SimpleNamespace(
                needs_replan=False,
                summary="Strong windows.",
                cut_reviews=[],
            )

    monkeypatch.setattr("app.agents._model_client.default_client", lambda: FakeClient())
    monkeypatch.setattr("app.agents.montage_reviewer.MontageReviewAgent", FakeReviewer)
    monkeypatch.setattr(
        "app.storage.download_generation_to_file",
        lambda path, destination, generation: None,
    )

    result = proposal_build._review_authored_montage(
        _authored_output(),
        [_source("source-a"), _source("source-b")],
        "Use the action.",
        "item-123",
    )

    assert {source_id for source_id, _job_id in result} == {"source-a", "source-b"}
    assert {job_id for _source_id, job_id in calls} == {"item-123"}


def test_authored_montage_review_returns_empty_when_provider_fails(monkeypatch) -> None:  # noqa: ANN001
    class FailingClient:
        def upload_media(self, path: str):  # noqa: ANN001
            raise RuntimeError("provider unavailable")

    monkeypatch.setattr("app.agents._model_client.default_client", lambda: FailingClient())
    monkeypatch.setattr(
        "app.storage.download_generation_to_file",
        lambda path, destination, generation: None,
    )

    assert (
        proposal_build._review_authored_montage(
            _authored_output(),
            [_source("source-a"), _source("source-b")],
            "Use the action.",
            "item-123",
        )
        == []
    )
