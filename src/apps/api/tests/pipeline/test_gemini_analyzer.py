"""Unit tests for pipeline/agents/gemini_analyzer.py — all Gemini calls mocked."""

import json
import time
from unittest.mock import MagicMock, patch

import pytest

from app.pipeline.agents.gemini_analyzer import (
    ClipMeta,
    GeminiAnalysisError,
    GeminiRefusalError,
    PollingTimeoutError,
    TemplateRecipe,
    _check_refusal,
    analyze_clip,
    analyze_template,
    gemini_upload_and_wait,
    transcribe,
)


# ── Helpers ───────────────────────────────────────────────────────────────────

def _make_file_ref(state: str = "ACTIVE", name: str = "files/abc123") -> MagicMock:
    ref = MagicMock()
    ref.name = name
    ref.uri = f"https://generativelanguage.googleapis.com/{name}"
    ref.mime_type = "video/mp4"
    ref.state.name = state
    return ref


def _make_gemini_response(data: dict, finish_reason: str = "STOP") -> MagicMock:
    response = MagicMock()
    response.text = json.dumps(data)
    candidate = MagicMock()
    candidate.finish_reason.name = finish_reason
    response.candidates = [candidate]
    return response


# ── gemini_upload_and_wait ─────────────────────────────────────────────────────

class TestGeminiUploadAndWait:
    def test_happy_path_returns_active_ref(self):
        file_ref = _make_file_ref("ACTIVE")

        with patch("app.pipeline.agents.gemini_analyzer._get_client") as mock_get_client:
            mock_client = MagicMock()
            mock_get_client.return_value = mock_client
            mock_client.files.upload.return_value = file_ref
            mock_client.files.get.return_value = file_ref

            result = gemini_upload_and_wait("/tmp/test.mp4")

        assert result.state.name == "ACTIVE"

    def test_polls_until_active(self):
        pending_ref = _make_file_ref("PROCESSING")
        active_ref = _make_file_ref("ACTIVE")

        with patch("app.pipeline.agents.gemini_analyzer._get_client") as mock_get_client:
            mock_client = MagicMock()
            mock_get_client.return_value = mock_client
            mock_client.files.upload.return_value = pending_ref
            mock_client.files.get.side_effect = [pending_ref, active_ref]

            with patch("time.sleep"):
                result = gemini_upload_and_wait("/tmp/test.mp4")

        assert result.state.name == "ACTIVE"
        assert mock_client.files.get.call_count == 2

    def test_timeout_raises_polling_timeout_error(self):
        pending_ref = _make_file_ref("PROCESSING")

        with patch("app.pipeline.agents.gemini_analyzer._get_client") as mock_get_client:
            mock_client = MagicMock()
            mock_get_client.return_value = mock_client
            mock_client.files.upload.return_value = pending_ref
            mock_client.files.get.return_value = pending_ref

            with patch("time.sleep"), patch("time.time", side_effect=[0, 0, 200]):
                with pytest.raises(PollingTimeoutError):
                    gemini_upload_and_wait("/tmp/test.mp4", timeout=120)

    def test_failed_state_raises_gemini_analysis_error(self):
        failed_ref = _make_file_ref("FAILED")

        with patch("app.pipeline.agents.gemini_analyzer._get_client") as mock_get_client:
            mock_client = MagicMock()
            mock_get_client.return_value = mock_client
            mock_client.files.upload.return_value = failed_ref
            mock_client.files.get.return_value = failed_ref

            with patch("time.sleep"):
                with pytest.raises(GeminiAnalysisError, match="processing failed"):
                    gemini_upload_and_wait("/tmp/test.mp4")


# ── _check_refusal ─────────────────────────────────────────────────────────────

class TestCheckRefusal:
    def test_safety_refusal_raises(self):
        response = _make_gemini_response({}, finish_reason="SAFETY")
        with pytest.raises(GeminiRefusalError, match="Content policy"):
            _check_refusal(response, [])

    def test_json_parse_error_raises(self):
        response = MagicMock()
        response.text = "not json {"
        response.candidates = []
        with pytest.raises(GeminiRefusalError, match="Invalid JSON"):
            _check_refusal(response, [])

    def test_missing_required_field_raises(self):
        response = _make_gemini_response({"present_field": "value"})
        with pytest.raises(GeminiRefusalError, match="missing_field"):
            _check_refusal(response, ["present_field", "missing_field"])

    def test_empty_required_field_raises(self):
        response = _make_gemini_response({"hook_text": "", "hook_score": 5.0, "best_moments": []})
        with pytest.raises(GeminiRefusalError, match="hook_text"):
            _check_refusal(response, ["hook_text"])

    def test_valid_response_returns_dict(self):
        data = {"hook_text": "Amazing!", "hook_score": 8.0, "best_moments": [{"start_s": 0}]}
        response = _make_gemini_response(data)
        result = _check_refusal(response, ["hook_text", "hook_score", "best_moments"])
        assert result["hook_score"] == 8.0


# ── analyze_clip ──────────────────────────────────────────────────────────────

class TestAnalyzeClip:
    def test_happy_path_returns_clip_meta(self):
        file_ref = _make_file_ref()
        data = {
            "transcript": "This is a video about something",
            "hook_text": "You won't believe this!",
            "hook_score": 8.5,
            "best_moments": [{"start_s": 0.0, "end_s": 10.0, "energy": 7.0, "description": "intro"}],
        }

        with patch("app.pipeline.agents.gemini_analyzer._get_client") as mock_get_client:
            mock_client = MagicMock()
            mock_get_client.return_value = mock_client
            mock_client.models.generate_content.return_value = _make_gemini_response(data)

            result = analyze_clip(file_ref)

        assert isinstance(result, ClipMeta)
        assert result.hook_score == pytest.approx(8.5)
        assert result.hook_text == "You won't believe this!"
        assert len(result.best_moments) == 1

    def test_with_timestamp_range(self):
        file_ref = _make_file_ref()
        data = {
            "transcript": "segment transcript",
            "hook_text": "Segment hook",
            "hook_score": 7.0,
            "best_moments": [{"start_s": 5.0, "end_s": 15.0, "energy": 6.0, "description": ""}],
        }

        with patch("app.pipeline.agents.gemini_analyzer._get_client") as mock_get_client:
            mock_client = MagicMock()
            mock_get_client.return_value = mock_client
            mock_client.models.generate_content.return_value = _make_gemini_response(data)

            result = analyze_clip(file_ref, start_s=5.0, end_s=15.0)

        # Verify segment timestamps were included in the prompt
        call_args = mock_client.models.generate_content.call_args
        contents = call_args.kwargs["contents"]
        prompt = contents[1] if len(contents) > 1 else contents[0]
        assert "5.0s" in prompt or "5.0" in prompt
        assert isinstance(result, ClipMeta)

    def test_hook_score_clamped_to_0_10(self):
        file_ref = _make_file_ref()
        data = {
            "transcript": "t",
            "hook_text": "hook",
            "hook_score": 15.0,  # out of range
            "best_moments": [{"start_s": 0.0, "end_s": 5.0, "energy": 5.0, "description": ""}],
        }

        with patch("app.pipeline.agents.gemini_analyzer._get_client") as mock_get_client:
            mock_client = MagicMock()
            mock_get_client.return_value = mock_client
            mock_client.models.generate_content.return_value = _make_gemini_response(data)

            result = analyze_clip(file_ref)

        assert result.hook_score == pytest.approx(10.0)

    def test_safety_refusal_raises_gemini_refusal_error(self):
        file_ref = _make_file_ref()

        with patch("app.pipeline.agents.gemini_analyzer._get_client") as mock_get_client:
            mock_client = MagicMock()
            mock_get_client.return_value = mock_client
            mock_client.models.generate_content.return_value = _make_gemini_response(
                {}, finish_reason="SAFETY"
            )
            with pytest.raises(GeminiRefusalError):
                analyze_clip(file_ref)

    def test_json_parse_error_raises_gemini_analysis_error(self):
        file_ref = _make_file_ref()

        with patch("app.pipeline.agents.gemini_analyzer._get_client") as mock_get_client:
            mock_client = MagicMock()
            mock_get_client.return_value = mock_client
            bad_response = MagicMock()
            bad_response.text = "invalid json {"
            bad_response.candidates = []
            mock_client.models.generate_content.return_value = bad_response

            with pytest.raises((GeminiRefusalError, GeminiAnalysisError)):
                analyze_clip(file_ref)


# ── analyze_template ──────────────────────────────────────────────────────────

class TestAnalyzeTemplate:
    def test_happy_path_returns_template_recipe(self):
        file_ref = _make_file_ref()
        data = {
            "shot_count": 5,
            "total_duration_s": 45.0,
            "hook_duration_s": 3.0,
            "slots": [
                {"position": 1, "target_duration_s": 5.0, "priority": 10, "slot_type": "hook"},
                {"position": 2, "target_duration_s": 10.0, "priority": 5, "slot_type": "broll"},
            ],
            "copy_tone": "casual",
            "caption_style": "bold text overlay",
        }

        with patch("app.pipeline.agents.gemini_analyzer._get_client") as mock_get_client:
            mock_client = MagicMock()
            mock_get_client.return_value = mock_client
            mock_client.models.generate_content.return_value = _make_gemini_response(data)

            result = analyze_template(file_ref)

        assert isinstance(result, TemplateRecipe)
        assert result.shot_count == 5
        assert result.copy_tone == "casual"
        assert len(result.slots) == 2

    def test_refusal_propagates(self):
        file_ref = _make_file_ref()

        with patch("app.pipeline.agents.gemini_analyzer._get_client") as mock_get_client:
            mock_client = MagicMock()
            mock_get_client.return_value = mock_client
            mock_client.models.generate_content.return_value = _make_gemini_response(
                {}, finish_reason="SAFETY"
            )
            with pytest.raises(GeminiRefusalError):
                analyze_template(file_ref)


# ── transcribe ────────────────────────────────────────────────────────────────

class TestTranscribe:
    def test_happy_path_returns_transcript(self):
        file_ref = _make_file_ref()
        data = {
            "full_text": "Hello world this is a test",
            "words": [
                {"text": "Hello", "start_s": 0.0, "end_s": 0.5},
                {"text": "world", "start_s": 0.6, "end_s": 1.0},
            ],
            "low_confidence": False,
        }

        with patch("app.pipeline.agents.gemini_analyzer._get_client") as mock_get_client:
            mock_client = MagicMock()
            mock_get_client.return_value = mock_client
            mock_client.models.generate_content.return_value = _make_gemini_response(data)

            result = transcribe(file_ref)

        assert result.full_text == "Hello world this is a test"
        assert len(result.words) == 2
        assert not result.low_confidence

    def test_gemini_failure_returns_degraded_transcript(self):
        file_ref = _make_file_ref()
        file_ref._local_path = None  # no local path → can't Whisper fallback

        with patch("app.pipeline.agents.gemini_analyzer._get_client") as mock_get_client:
            mock_client = MagicMock()
            mock_get_client.return_value = mock_client
            mock_client.models.generate_content.side_effect = Exception("API down")

            result = transcribe(file_ref)

        assert result.low_confidence is True
        assert result.full_text == ""

    def test_whisper_fallback_used_when_local_path_set(self):
        file_ref = _make_file_ref()
        file_ref._local_path = "/tmp/test.mp4"

        mock_transcript = MagicMock()
        mock_transcript.full_text = "Whisper transcript"
        mock_transcript.words = []
        mock_transcript.low_confidence = False

        with patch("app.pipeline.agents.gemini_analyzer._get_client") as mock_get_client, \
             patch("app.pipeline.transcribe.transcribe_whisper") as mock_whisper:
            mock_client = MagicMock()
            mock_get_client.return_value = mock_client
            mock_client.models.generate_content.side_effect = Exception("API down")
            mock_whisper.return_value = mock_transcript

            result = transcribe(file_ref)

        mock_whisper.assert_called_once_with("/tmp/test.mp4")
        assert result.full_text == "Whisper transcript"
