from unittest.mock import MagicMock, patch

from app.tasks.template_orchestrate import _analyze_clips_parallel, _upload_clips_parallel


def test_upload_clips_parallel_skips_still_images(tmp_path):
    image_path = tmp_path / "still.png"
    image_path.write_bytes(b"png")

    with patch("app.tasks.template_orchestrate.gemini_upload_and_wait") as mock_upload:
        result = _upload_clips_parallel([str(image_path)])

    assert result == [None]
    mock_upload.assert_not_called()


def test_analyze_clips_parallel_synthesizes_still_image_metadata(tmp_path):
    image_path = tmp_path / "still.jpg"
    image_path.write_bytes(b"not-a-real-jpeg-but-extension-is-enough")

    with patch("app.pipeline.transcribe.transcribe_whisper") as whisper:
        metas, failed = _analyze_clips_parallel(
            [None],
            [str(image_path)],
            probe_map={str(image_path): MagicMock(duration_s=15.0)},
        )

    assert failed == 0
    assert len(metas) == 1
    assert metas[0].clip_id == "clip_0"
    assert metas[0].clip_path == str(image_path)
    assert metas[0].analysis_degraded is True
    assert getattr(metas[0], "is_image") is True
    assert metas[0].best_moments[0]["end_s"] == 15.0
    whisper.assert_not_called()
