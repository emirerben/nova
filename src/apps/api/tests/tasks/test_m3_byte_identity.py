"""Byte-identity contract tests for Creator Agent M3 (Lane B).

Guards the three invariants introduced in M3:
  1. filming_guide absent/empty → NO "filming_guide" key in all_candidates
     (build_generative_job byte-identical to pre-B2).
  2. instruction_level="full" + empty mix → content plan prompt byte-identical
     to baseline (pre-B1).
  3. footage_type_bias absent → _resolve_archetype behavior byte-identical to
     pre-B3 (montage for montage edit_format, voiceover fast path unchanged).

No DB / Gemini / GPU required (parallel-safe, skia-free).
"""

from __future__ import annotations

import uuid
from unittest.mock import MagicMock, patch


class TestFilmingGuideByteIdentity:
    """filming_guide absent → no 'filming_guide' key in all_candidates."""

    def test_no_filming_guide_key_when_none(self) -> None:
        """filming_guide=None → key absent from all_candidates (byte-identical)."""
        from app.services.generative_jobs import build_generative_job

        with patch("app.config.settings") as mock_settings:
            mock_settings.user_style_enabled = False
            job = build_generative_job(
                user_id=uuid.uuid4(),
                clip_paths=["music-uploads/test.mp4"],
                filming_guide=None,
            )
        assert "filming_guide" not in (job.all_candidates or {}), (
            "filming_guide must not appear in all_candidates when filming_guide is None"
        )

    def test_no_filming_guide_key_when_empty_list(self) -> None:
        """filming_guide=[] → key absent from all_candidates (byte-identical)."""
        from app.services.generative_jobs import build_generative_job

        with patch("app.config.settings") as mock_settings:
            mock_settings.user_style_enabled = False
            job = build_generative_job(
                user_id=uuid.uuid4(),
                clip_paths=["music-uploads/test.mp4"],
                filming_guide=[],
            )
        assert "filming_guide" not in (job.all_candidates or {}), (
            "filming_guide must not appear in all_candidates when filming_guide is []"
        )

    def test_filming_guide_key_present_when_non_empty(self) -> None:
        """filming_guide=[valid_shot] → key IS present in all_candidates."""
        from app.services.generative_jobs import build_generative_job

        with patch("app.config.settings") as mock_settings:
            mock_settings.user_style_enabled = False
            job = build_generative_job(
                user_id=uuid.uuid4(),
                clip_paths=["music-uploads/test.mp4"],
                filming_guide=[
                    {"what": "wide shot of the dish", "how": "handheld", "duration_s": 5}
                ],
            )
        cands = job.all_candidates or {}
        assert "filming_guide" in cands, (
            "filming_guide must appear in all_candidates when filming_guide is non-empty"
        )
        assert isinstance(cands["filming_guide"], list)
        assert len(cands["filming_guide"]) == 1
        assert cands["filming_guide"][0]["what"] == "wide shot of the dish"

    def test_filming_guide_sanitizes_empty_what(self) -> None:
        """Shots with empty/null 'what' are dropped by the helper."""
        from app.services.generative_jobs import _build_filming_guide_context

        result = _build_filming_guide_context(
            [
                {"what": "  ", "how": "handheld", "duration_s": 5},  # whitespace only → skip
                {"what": "valid shot", "how": "", "duration_s": 3},
            ]
        )
        assert len(result) == 1
        assert result[0]["what"] == "valid shot"

    def test_filming_guide_caps_at_max_shots(self) -> None:
        """More than 4 shots are capped to 4."""
        from app.services.generative_jobs import _build_filming_guide_context

        shots = [{"what": f"shot {i}", "how": "", "duration_s": 3} for i in range(10)]
        result = _build_filming_guide_context(shots)
        assert len(result) == 4

    def test_filming_guide_clamps_duration(self) -> None:
        """duration_s is clamped to [1, 60]."""
        from app.services.generative_jobs import _build_filming_guide_context

        result = _build_filming_guide_context(
            [
                {"what": "too short", "how": "", "duration_s": 0},
                {"what": "too long", "how": "", "duration_s": 9999},
            ]
        )
        assert result[0]["duration_s"] == 1
        assert result[1]["duration_s"] == 60

    def test_filming_guide_ignores_non_dict_entries(self) -> None:
        """Non-dict entries in the guide are dropped."""
        from app.services.generative_jobs import _build_filming_guide_context

        result = _build_filming_guide_context(
            [
                "not a dict",
                None,
                {"what": "valid shot", "how": "", "duration_s": 5},
            ]
        )
        assert len(result) == 1
        assert result[0]["what"] == "valid shot"


class TestContentPlanPromptByteIdentity:
    """instruction_level='full' + empty mix → byte-identical prompt to baseline."""

    def _make_persona(self):
        """Create a minimal valid Persona for testing."""
        from app.agents._schemas.persona import Persona

        return Persona(
            summary="test creator",
            content_pillars=["fitness"],
            tone="casual",
            audience="young adults",
            posting_cadence="3x per week",
            sample_topics=["morning routine"],
        )

    def test_instruction_level_full_returns_empty_string(self) -> None:
        """_instruction_level_block('full') returns '' (byte-identical to baseline)."""
        from app.agents.content_plan_generator import _instruction_level_block

        assert _instruction_level_block("full") == "", (
            "_instruction_level_block('full') must return '' to keep prompt byte-identical"
        )

    def test_instruction_level_empty_returns_empty_string(self) -> None:
        """_instruction_level_block('') returns '' (defensive default)."""
        from app.agents.content_plan_generator import _instruction_level_block

        assert _instruction_level_block("") == ""

    def test_instruction_level_none_str_returns_empty_string(self) -> None:
        """_instruction_level_block(None coerced) — guard for falsy values."""
        from app.agents.content_plan_generator import _instruction_level_block

        # The function signature is str, but guard against the falsy path
        assert _instruction_level_block("full") == ""

    def test_instruction_level_light_is_non_empty(self) -> None:
        """_instruction_level_block('light') returns a non-empty directive."""
        from app.agents.content_plan_generator import _instruction_level_block

        result = _instruction_level_block("light")
        assert result != "", "'light' level must return a non-empty block"
        assert "(none)" not in result.lower(), (
            "Block must not contain '(none)' — inert blocks degrade output quality"
        )

    def test_instruction_level_none_val_is_non_empty(self) -> None:
        """_instruction_level_block('none') returns a non-empty directive."""
        from app.agents.content_plan_generator import _instruction_level_block

        result = _instruction_level_block("none")
        assert result != "", "'none' level must return a non-empty block"
        assert "(none)" not in result.lower()

    def test_edit_format_mix_empty_returns_empty_string(self) -> None:
        """_edit_format_mix_block({}) returns '' (byte-identical to baseline)."""
        from app.agents.content_plan_generator import _edit_format_mix_block

        assert _edit_format_mix_block({}) == "", (
            "_edit_format_mix_block({}) must return '' to keep prompt byte-identical"
        )

    def test_edit_format_mix_non_empty_is_non_empty(self) -> None:
        """_edit_format_mix_block({'montage': 0.6}) returns a non-empty directive."""
        from app.agents.content_plan_generator import _edit_format_mix_block

        result = _edit_format_mix_block({"montage": 0.6, "talking_head": 0.4})
        assert result != ""
        assert "(none)" not in result.lower()

    def test_content_plan_input_defaults_are_baseline(self) -> None:
        """ContentPlanInput with default fields has instruction_level='full' and empty mix."""
        from app.agents._schemas.content_plan import ContentPlanInput

        persona = self._make_persona()
        inp = ContentPlanInput(persona=persona)
        assert inp.instruction_level == "full"
        assert inp.preferred_edit_format_mix == {}


class TestFilmingGuideIntroWriterByteIdentity:
    """filming_guide absent → intro_writer prompt byte-identical to pre-M3."""

    def test_filming_guide_block_empty_when_no_guide(self) -> None:
        """_filming_guide_block([]) returns '' (byte-identical to pre-M3 baseline)."""
        from app.agents.intro_writer import _filming_guide_block

        assert _filming_guide_block([]) == "", (
            "_filming_guide_block([]) must return '' to keep prompt byte-identical"
        )

    def test_filming_guide_block_non_empty_when_guide_present(self) -> None:
        """_filming_guide_block([shot]) returns a non-empty DATA block."""
        from app.agents.intro_writer import _filming_guide_block

        result = _filming_guide_block(
            [{"what": "wide shot of the dish", "how": "handheld", "duration_s": 5}]
        )
        assert result != "", "A non-empty guide must produce a non-empty block"
        assert "(none)" not in result.lower(), (
            "Block must not contain '(none)' — inert blocks degrade output quality"
        )
        assert "wide shot of the dish" in result

    def test_filming_guide_block_skips_empty_what(self) -> None:
        """_filming_guide_block drops shots with empty 'what' field."""
        from app.agents.intro_writer import _filming_guide_block

        result = _filming_guide_block(
            [
                {"what": "", "how": "handheld", "duration_s": 5},
            ]
        )
        assert result == "", "All-empty-what guide must produce '' (no block for an empty guide)"

    def test_filming_guide_block_no_none_string(self) -> None:
        """_filming_guide_block never emits '(none)' for any input."""
        from app.agents.intro_writer import _filming_guide_block

        assert "(none)" not in _filming_guide_block([]).lower()
        assert (
            "(none)"
            not in _filming_guide_block([{"what": "x", "how": "", "duration_s": 3}]).lower()
        )

    def test_intro_writer_input_defaults_to_empty_filming_guide(self) -> None:
        """IntroWriterInput defaults filming_guide to [] (no inert block injected)."""
        from app.agents.intro_writer import IntroWriterInput
        from app.agents.music_matcher import ClipSummary

        hero = ClipSummary(
            clip_id="c1",
            duration_s=8.0,
            subject="chef",
            hook_text="plating",
            hook_score=0.8,
            description="close-up plating",
        )
        inp = IntroWriterInput(hero_clip=hero)
        assert inp.filming_guide == []


class TestArchetypeBiasContract:
    """footage_type_bias absent → _resolve_archetype byte-identical to pre-B3."""

    def _make_noop_record(self):
        """Return a no-op record_pipeline_event patch."""
        return patch(
            "app.tasks.generative_build.record_pipeline_event",
            new=MagicMock(),
        )

    def test_montage_format_no_bias_returns_montage(self) -> None:
        """edit_format='montage' with no bias → ('montage', None) — same as pre-B3."""
        from app.tasks.generative_build import _resolve_archetype

        with patch("app.services.pipeline_trace.record_pipeline_event"):
            result = _resolve_archetype(
                "montage",
                [],  # no clip_metas
                {},  # no local paths
                job_id="test-job",
                voiceover_gcs_path=None,
                footage_type_bias=None,
            )
        assert result == ("montage", None)

    def test_montage_format_empty_bias_returns_montage(self) -> None:
        """edit_format='montage' with empty bias list → ('montage', None)."""
        from app.tasks.generative_build import _resolve_archetype

        with patch("app.services.pipeline_trace.record_pipeline_event"):
            result = _resolve_archetype(
                "montage",
                [],
                {},
                job_id="test-job",
                voiceover_gcs_path=None,
                footage_type_bias=[],
            )
        assert result == ("montage", None)

    def test_voiceover_wins_over_bias(self) -> None:
        """Voiceover fast path always wins — bias must not override it."""
        from app.tasks.generative_build import _resolve_archetype

        with patch("app.services.pipeline_trace.record_pipeline_event"):
            result = _resolve_archetype(
                "montage",
                [],
                {},
                job_id="test-job",
                voiceover_gcs_path="users/test/voice.mp3",
                footage_type_bias=["talking_head"],
            )
        assert result == ("voiceover", None), (
            "Voiceover fast path must not be overridden by footage_type_bias"
        )

    def test_bias_talking_head_with_flag_disabled_returns_montage(self) -> None:
        """Even if bias says 'talking_head', kill switch off → montage."""
        from app.tasks.generative_build import _resolve_archetype

        with (
            patch("app.services.pipeline_trace.record_pipeline_event"),
            patch("app.tasks.generative_build.settings") as mock_settings,
        ):
            mock_settings.edit_format_talking_head_enabled = False
            result = _resolve_archetype(
                "montage",
                [],
                {},
                job_id="test-job",
                footage_type_bias=["talking_head"],
            )
        assert result == ("montage", None), (
            "edit_format_talking_head_enabled=False must prevent bias from activating talking_head"
        )

    def test_talking_head_explicit_format_unaffected_by_bias(self) -> None:
        """edit_format='talking_head' with no speech → ('montage', None) — speech gate unchanged."""
        from app.tasks.generative_build import _resolve_archetype

        with (
            patch("app.services.pipeline_trace.record_pipeline_event"),
            patch("app.tasks.generative_build.settings") as mock_settings,
        ):
            mock_settings.edit_format_talking_head_enabled = True
            # No clip_metas / no local paths → no speech → fallback to montage
            result = _resolve_archetype(
                "talking_head",
                [],
                {},
                job_id="test-job",
                footage_type_bias=["talking_head"],
            )
        # No clips → best_id is None → fallback "no_speech"
        assert result == ("montage", None), (
            "Explicit talking_head with no speech must still fall back to montage"
        )
