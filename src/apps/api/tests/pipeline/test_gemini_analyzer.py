"""Unit tests for pipeline/agents/gemini_analyzer.py — all Gemini calls mocked."""

import json
from unittest.mock import MagicMock, patch

import pytest

from app.pipeline.agents.gemini_analyzer import (
    ClipMeta,
    GeminiAnalysisError,
    GeminiRefusalError,
    PollingTimeoutError,
    TemplateRecipe,
    _check_refusal,
    _validate_slots,
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
            "best_moments": [
                {"start_s": 0.0, "end_s": 10.0, "energy": 7.0, "description": "intro"}
            ],
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

    def test_text_overlays_parsed(self):
        """Slot with text_overlays appears in recipe."""
        file_ref = _make_file_ref()
        data = {
            "shot_count": 1,
            "total_duration_s": 5.0,
            "hook_duration_s": 2.0,
            "slots": [
                {
                    "position": 1,
                    "target_duration_s": 5.0,
                    "priority": 10,
                    "slot_type": "hook",
                    "text_overlays": [
                        {
                            "role": "hook",
                            "start_s": 0.5,
                            "end_s": 2.5,
                            "position": "center",
                            "effect": "pop-in",
                            "has_darkening": True,
                            "has_narrowing": False,
                            "sample_text": "YOU WON'T BELIEVE THIS",
                        }
                    ],
                }
            ],
            "copy_tone": "energetic",
            "caption_style": "bold",
        }

        with patch("app.pipeline.agents.gemini_analyzer._get_client") as mock_get_client:
            mock_client = MagicMock()
            mock_get_client.return_value = mock_client
            mock_client.models.generate_content.return_value = _make_gemini_response(data)

            result = analyze_template(file_ref)

        assert len(result.slots) == 1
        overlays = result.slots[0].get("text_overlays", [])
        assert len(overlays) == 1
        assert overlays[0]["role"] == "hook"
        assert overlays[0]["effect"] == "pop-in"

    def test_text_overlays_missing_defaults_empty(self):
        """Response without text_overlays defaults to []."""
        file_ref = _make_file_ref()
        data = {
            "shot_count": 1,
            "total_duration_s": 5.0,
            "hook_duration_s": 2.0,
            "slots": [
                {
                    "position": 1,
                    "target_duration_s": 5.0,
                    "priority": 5,
                    "slot_type": "broll",
                }
            ],
            "copy_tone": "casual",
            "caption_style": "",
        }

        with patch("app.pipeline.agents.gemini_analyzer._get_client") as mock_get_client:
            mock_client = MagicMock()
            mock_get_client.return_value = mock_client
            mock_client.models.generate_content.return_value = _make_gemini_response(data)

            result = analyze_template(file_ref)

        assert result.slots[0].get("text_overlays", []) == []

    def test_invalid_overlays_stripped(self):
        """Bad role/timing removed from list."""
        file_ref = _make_file_ref()
        data = {
            "shot_count": 1,
            "total_duration_s": 5.0,
            "hook_duration_s": 2.0,
            "slots": [
                {
                    "position": 1,
                    "target_duration_s": 5.0,
                    "priority": 5,
                    "slot_type": "hook",
                    "text_overlays": [
                        {"role": "invalid_role", "start_s": 0.0, "end_s": 2.0,
                         "position": "center", "effect": "none", "sample_text": "x"},
                        {"role": "hook", "start_s": 3.0, "end_s": 1.0,  # bad timing
                         "position": "center", "effect": "none", "sample_text": "y"},
                        {"role": "hook", "start_s": 0.0, "end_s": 2.0,  # valid
                         "position": "center", "effect": "fade-in", "sample_text": "z"},
                    ],
                }
            ],
            "copy_tone": "casual",
            "caption_style": "",
        }

        with patch("app.pipeline.agents.gemini_analyzer._get_client") as mock_get_client:
            mock_client = MagicMock()
            mock_get_client.return_value = mock_client
            mock_client.models.generate_content.return_value = _make_gemini_response(data)

            result = analyze_template(file_ref)

        overlays = result.slots[0].get("text_overlays", [])
        assert len(overlays) == 1
        assert overlays[0]["sample_text"] == "z"


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


# ── Prompt improvement tests ─────────────────────────────────────────────────

def _base_template_data(**overrides):
    """Minimal valid template response data with optional overrides."""
    data = {
        "shot_count": 2,
        "total_duration_s": 10.0,
        "hook_duration_s": 3.0,
        "slots": [
            {"position": 1, "target_duration_s": 5.0, "priority": 10, "slot_type": "hook"},
            {"position": 2, "target_duration_s": 5.0, "priority": 5, "slot_type": "broll"},
        ],
        "copy_tone": "energetic",
        "caption_style": "bold",
    }
    data.update(overrides)
    return data


class TestSinglePassCreativeDirection:
    def test_single_pass_creative_direction_in_json(self):
        """Single-pass mode extracts creative_direction from JSON response."""
        file_ref = _make_file_ref()
        data = _base_template_data(
            creative_direction="Fast-paced cuts on every beat with warm color grading.",
            color_grade="warm",
            sync_style="cut-on-beat",
        )

        with patch("app.pipeline.agents.gemini_analyzer._get_client") as mock_gc:
            mock_client = MagicMock()
            mock_gc.return_value = mock_client
            mock_client.models.generate_content.return_value = _make_gemini_response(data)

            result = analyze_template(file_ref, analysis_mode="single")

        assert result.creative_direction == "Fast-paced cuts on every beat with warm color grading."
        assert result.color_grade == "warm"
        assert result.sync_style == "cut-on-beat"
        # Only one Gemini call for single-pass
        assert mock_client.models.generate_content.call_count == 1


class TestTwoPassAnalysis:
    def test_two_pass_creative_direction_extracted(self):
        """Two-pass mode calls Gemini twice: Pass 1 (text) + Pass 2 (JSON)."""
        file_ref = _make_file_ref()
        pass1_response = MagicMock()
        pass1_response.text = "Fast-paced whip-pans with warm vintage color grading."
        pass1_response.candidates = [MagicMock()]
        pass1_response.candidates[0].finish_reason.name = "STOP"

        data = _base_template_data(color_grade="warm")
        pass2_response = _make_gemini_response(data)

        with patch("app.pipeline.agents.gemini_analyzer._get_client") as mock_gc:
            mock_client = MagicMock()
            mock_gc.return_value = mock_client
            mock_client.models.generate_content.side_effect = [pass1_response, pass2_response]

            result = analyze_template(file_ref, analysis_mode="two_pass")

        assert result.creative_direction == "Fast-paced whip-pans with warm vintage color grading."
        assert mock_client.models.generate_content.call_count == 2

    def test_two_pass_pass1_injected_into_pass2(self):
        """Pass 1 output appears in the Pass 2 prompt."""
        file_ref = _make_file_ref()
        pass1_response = MagicMock()
        pass1_response.text = "UNIQUE_CREATIVE_DIRECTION_TEXT"
        pass1_response.candidates = [MagicMock()]
        pass1_response.candidates[0].finish_reason.name = "STOP"

        data = _base_template_data()
        pass2_response = _make_gemini_response(data)

        with patch("app.pipeline.agents.gemini_analyzer._get_client") as mock_gc:
            mock_client = MagicMock()
            mock_gc.return_value = mock_client
            mock_client.models.generate_content.side_effect = [pass1_response, pass2_response]

            analyze_template(file_ref, analysis_mode="two_pass")

        # Check that the Pass 2 call included the creative direction
        pass2_call = mock_client.models.generate_content.call_args_list[1]
        pass2_prompt = pass2_call.kwargs["contents"][1]
        assert "UNIQUE_CREATIVE_DIRECTION_TEXT" in pass2_prompt

    def test_pass1_failure_graceful_degradation(self):
        """If Pass 1 fails, proceed with single-pass behavior."""
        file_ref = _make_file_ref()
        data = _base_template_data()

        with patch("app.pipeline.agents.gemini_analyzer._get_client") as mock_gc:
            mock_client = MagicMock()
            mock_gc.return_value = mock_client
            # Pass 1 raises, Pass 2 succeeds
            mock_client.models.generate_content.side_effect = [
                Exception("Gemini timeout"),
                _make_gemini_response(data),
            ]

            result = analyze_template(file_ref, analysis_mode="two_pass")

        assert isinstance(result, TemplateRecipe)
        assert result.creative_direction == ""
        assert result.shot_count == 2

    def test_pass1_empty_response_proceeds(self):
        """If Pass 1 returns empty text, proceed with empty creative_direction."""
        file_ref = _make_file_ref()
        pass1_response = MagicMock()
        pass1_response.text = ""
        pass1_response.candidates = [MagicMock()]
        pass1_response.candidates[0].finish_reason.name = "STOP"

        data = _base_template_data()

        with patch("app.pipeline.agents.gemini_analyzer._get_client") as mock_gc:
            mock_client = MagicMock()
            mock_gc.return_value = mock_client
            mock_client.models.generate_content.side_effect = [
                pass1_response,
                _make_gemini_response(data),
            ]

            result = analyze_template(file_ref, analysis_mode="two_pass")

        assert result.creative_direction == ""


class TestNewSlotFields:
    def test_new_slot_fields_parsed(self):
        """transition_in, color_hint, speed_factor are extracted from response."""
        file_ref = _make_file_ref()
        data = _base_template_data()
        data["color_grade"] = "warm"
        data["slots"][0]["transition_in"] = "whip-pan"
        data["slots"][0]["color_hint"] = "cool"
        data["slots"][0]["speed_factor"] = 0.5

        with patch("app.pipeline.agents.gemini_analyzer._get_client") as mock_gc:
            mock_client = MagicMock()
            mock_gc.return_value = mock_client
            mock_client.models.generate_content.return_value = _make_gemini_response(data)

            result = analyze_template(file_ref)

        assert result.slots[0]["transition_in"] == "whip-pan"
        assert result.slots[0]["color_hint"] == "cool"  # explicit override
        assert result.slots[0]["speed_factor"] == 0.5

    def test_new_slot_fields_default_when_missing(self):
        """Missing new fields get safe defaults."""
        file_ref = _make_file_ref()
        data = _base_template_data(color_grade="warm")

        with patch("app.pipeline.agents.gemini_analyzer._get_client") as mock_gc:
            mock_client = MagicMock()
            mock_gc.return_value = mock_client
            mock_client.models.generate_content.return_value = _make_gemini_response(data)

            result = analyze_template(file_ref)

        slot = result.slots[0]
        assert slot["transition_in"] == "none"
        assert slot["color_hint"] == "warm"  # inherits global
        assert slot["speed_factor"] == 1.0

    def test_color_hint_inherits_global_color_grade(self):
        """Slots without explicit color_hint inherit global color_grade."""
        file_ref = _make_file_ref()
        data = _base_template_data(color_grade="vintage")

        with patch("app.pipeline.agents.gemini_analyzer._get_client") as mock_gc:
            mock_client = MagicMock()
            mock_gc.return_value = mock_client
            mock_client.models.generate_content.return_value = _make_gemini_response(data)

            result = analyze_template(file_ref)

        for slot in result.slots:
            assert slot["color_hint"] == "vintage"

    def test_transition_in_invalid_defaults_to_none(self):
        """Unknown transition type defaults to 'none'."""
        file_ref = _make_file_ref()
        data = _base_template_data()
        data["slots"][0]["transition_in"] = "wipe-left"  # not in allowlist

        with patch("app.pipeline.agents.gemini_analyzer._get_client") as mock_gc:
            mock_client = MagicMock()
            mock_gc.return_value = mock_client
            mock_client.models.generate_content.return_value = _make_gemini_response(data)

            result = analyze_template(file_ref)

        assert result.slots[0]["transition_in"] == "none"

    def test_speed_factor_out_of_range_clamped(self):
        """speed_factor outside [0.25, 4.0] is clamped."""
        file_ref = _make_file_ref()
        data = _base_template_data()
        data["slots"][0]["speed_factor"] = 0.1  # below min
        data["slots"][1]["speed_factor"] = 10.0  # above max

        with patch("app.pipeline.agents.gemini_analyzer._get_client") as mock_gc:
            mock_client = MagicMock()
            mock_gc.return_value = mock_client
            mock_client.models.generate_content.return_value = _make_gemini_response(data)

            result = analyze_template(file_ref)

        assert result.slots[0]["speed_factor"] == 0.25
        assert result.slots[1]["speed_factor"] == 4.0

    def test_expanded_text_effects_validated(self):
        """New effect types (font-cycle, typewriter, etc.) are accepted."""
        file_ref = _make_file_ref()
        data = _base_template_data()
        data["slots"][0]["text_overlays"] = [
            {"role": "label", "start_s": 0.0, "end_s": 2.0,
             "position": "center", "effect": "font-cycle", "sample_text": "TOKYO"},
        ]

        with patch("app.pipeline.agents.gemini_analyzer._get_client") as mock_gc:
            mock_client = MagicMock()
            mock_gc.return_value = mock_client
            mock_client.models.generate_content.return_value = _make_gemini_response(data)

            result = analyze_template(file_ref)

        overlays = result.slots[0]["text_overlays"]
        assert len(overlays) == 1
        assert overlays[0]["effect"] == "font-cycle"

    def test_sync_style_parsed(self):
        """sync_style field is extracted from response."""
        file_ref = _make_file_ref()
        data = _base_template_data(sync_style="cut-on-beat")

        with patch("app.pipeline.agents.gemini_analyzer._get_client") as mock_gc:
            mock_client = MagicMock()
            mock_gc.return_value = mock_client
            mock_client.models.generate_content.return_value = _make_gemini_response(data)

            result = analyze_template(file_ref)

        assert result.sync_style == "cut-on-beat"

    def test_sync_style_defaults_to_freeform(self):
        """Missing or invalid sync_style defaults to 'freeform'."""
        file_ref = _make_file_ref()
        data = _base_template_data(sync_style="invalid_style")

        with patch("app.pipeline.agents.gemini_analyzer._get_client") as mock_gc:
            mock_client = MagicMock()
            mock_gc.return_value = mock_client
            mock_client.models.generate_content.return_value = _make_gemini_response(data)

            result = analyze_template(file_ref)

        assert result.sync_style == "freeform"


class TestCacheLoadValidation:
    def test_old_recipe_missing_new_fields_constructs_ok(self):
        """Old cached recipe without new fields → TemplateRecipe with defaults."""
        old_data = {
            "shot_count": 3,
            "total_duration_s": 15.0,
            "hook_duration_s": 3.0,
            "slots": [{"position": 1, "target_duration_s": 5.0}],
            "copy_tone": "casual",
            "caption_style": "",
        }
        recipe = TemplateRecipe(**old_data)
        assert recipe.creative_direction == ""
        assert recipe.color_grade == "none"
        assert recipe.sync_style == "freeform"

    def test_validate_slots_fixes_malformed_color_hint(self):
        """_validate_slots normalizes invalid color_hint to global default."""
        slots = [
            {"position": 1, "target_duration_s": 5.0, "color_hint": "banana"},
        ]
        _validate_slots(slots, global_color_grade="warm")
        assert slots[0]["color_hint"] == "warm"

    def test_validate_slots_fixes_malformed_transition_in(self):
        """_validate_slots normalizes invalid transition_in to 'none'."""
        slots = [
            {"position": 1, "target_duration_s": 5.0, "transition_in": "wipe-left"},
        ]
        _validate_slots(slots, global_color_grade="none")
        assert slots[0]["transition_in"] == "none"

    def test_validate_slots_clamps_speed_factor(self):
        """_validate_slots clamps out-of-range speed_factor."""
        slots = [
            {"position": 1, "target_duration_s": 5.0, "speed_factor": 100},
        ]
        _validate_slots(slots, global_color_grade="none")
        assert slots[0]["speed_factor"] == 4.0
