"""A real v8 render must honor the scheduled frames after candidate expansion."""

from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path

import pytest

from app.agents.edit_proposal import EditProposalAgentInput, EditProposalMedia
from app.pipeline.canvas import Canvas
from app.pipeline.guided_story import compile_execution_plan, render_execution_plan
from app.schemas.edit_proposal import EditProposalSnapshot, MediaRef, MontageAudioPlan
from app.schemas.semantic_edit import SemanticEditPlan
from app.services.semantic_edit_scheduler import schedule_semantic_edit
from tests.pipeline.test_guided_story_ffmpeg import _video
from tests.pipeline.test_semantic_schedule_compiler import _raw

pytestmark = pytest.mark.skipif(
    shutil.which("ffmpeg") is None or shutil.which("ffprobe") is None,
    reason="ffmpeg/ffprobe not installed",
)


def test_late_candidate_renders_exactly_450_frames(tmp_path: Path, monkeypatch) -> None:
    from app import storage
    from app.pipeline import guided_story

    source = tmp_path / "source.mp4"
    _video(source, size="180x320", duration_s=10)
    inputs = EditProposalAgentInput(
        direction="fast_montage",
        pace="fast",
        target_duration_s=15,
        media_scope="all",
        video_reuse_policy="once",
        opening_title="A day outside",
        montage_audio=MontageAudioPlan(preserve_source_audio=True),
        media=[
            EditProposalMedia(
                media_id=f"clip-{index}",
                lane="clip",
                kind="video",
                duration_s=10,
                best_moments=[{"start_s": 9, "end_s": 10}],
            )
            for index in range(3)
        ],
    )
    semantic = SemanticEditPlan(
        chapters=[
            {
                "chapter_id": f"chapter-{index}",
                "topic": "Day",
                "role": "hook" if index == 0 else "build",
                "sources": [{"media_id": row.media_id, "candidate_index": 0}],
            }
            for index, row in enumerate(inputs.media)
        ]
    )
    result = schedule_semantic_edit(semantic, inputs)
    snapshot = EditProposalSnapshot(
        direction=inputs.direction,
        duration_s=result.duration_s,
        title=inputs.opening_title,
        opening_title=inputs.opening_title,
        video_reuse_policy=inputs.video_reuse_policy,
        media=[
            MediaRef(**row.model_dump(), gcs_path=f"users/{row.media_id}.mp4", generation="1")
            for row in inputs.media
        ],
        story_beats=result.story_beats,
        fast_cuts=result.fast_cuts,
        montage_audio=inputs.montage_audio,
        frame_schedule=result.schedule,
    )
    assert [
        (moment.source_start_frame, moment.source_end_frame) for moment in result.schedule.moments
    ] == [(150, 300)] * 3
    plan = compile_execution_plan(_raw(snapshot), track=None)
    assert plan["compiler_version"] == 8
    for element in plan["text_elements"]:
        element["size_px"] = 24

    uploads = tmp_path / "uploads"
    uploads.mkdir()

    def download(object_path, local_path, *, generation):
        assert object_path.startswith("users/") and generation == "1"
        shutil.copy2(source, local_path)

    def upload(local_path, object_path, content_type="video/mp4"):
        shutil.copy2(local_path, uploads / Path(object_path).name)
        return f"https://example.test/{object_path}"

    canvas = Canvas(180, 320)
    monkeypatch.setattr(guided_story, "PORTRAIT", canvas)
    monkeypatch.setattr(guided_story.settings, "output_width", canvas.width)
    monkeypatch.setattr(guided_story.settings, "output_height", canvas.height)
    monkeypatch.setattr(storage, "download_generation_to_file", download)
    monkeypatch.setattr(storage, "upload_public_read", upload)
    monkeypatch.setattr(
        storage,
        "object_metadata",
        lambda path: storage.ObjectMetadata(
            path=path,
            generation=f"stored-{Path(path).name}",
            etag=None,
            size=(uploads / Path(path).name).stat().st_size,
            content_type="video/mp4",
            md5_hash=None,
        ),
    )
    rendered = render_execution_plan(
        plan, job_id="semantic-frame-test", tmpdir=str(tmp_path), track=None
    )
    assert rendered["ok"] is True
    assert rendered["render_receipt"]["verified"] is True
    assert rendered["render_receipt"]["source_audio_preserved"] is True
    final = next(uploads.glob("variant_1_guided_story_*.mp4"))
    probe = json.loads(
        subprocess.check_output(
            [
                "ffprobe",
                "-v",
                "error",
                "-count_frames",
                "-select_streams",
                "v:0",
                "-show_entries",
                "stream=nb_read_frames,r_frame_rate",
                "-of",
                "json",
                str(final),
            ]
        )
    )
    assert probe["streams"][0]["r_frame_rate"] == "30/1"
    assert int(probe["streams"][0]["nb_read_frames"]) == 450
