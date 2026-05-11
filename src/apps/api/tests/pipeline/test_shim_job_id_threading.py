"""Unit tests: pipeline/agents/* shims thread `job_id` into Agent.run() RunContext.

These verify the fix for empty Langfuse session_ids in production traces.
Every shim that wraps an `Agent.run()` call must accept a keyword-only
`job_id: str | None = None` parameter and pass it through as
`ctx=RunContext(job_id=job_id)` so traces cluster by Job in the Langfuse
Sessions tab. Default of None preserves back-compat.
"""

from types import SimpleNamespace
from unittest.mock import patch

import pytest

from app.agents._runtime import RunContext


def _file_ref(uri: str = "files/abc") -> SimpleNamespace:
    """Fake Gemini file ref with the minimal attributes shims read."""
    return SimpleNamespace(uri=uri, mime_type="video/mp4", name="clip_0")


# ── transcribe() ─────────────────────────────────────────────────────────────


def test_transcribe_threads_job_id_to_agent_run():
    from app.pipeline.agents import gemini_analyzer
    from app.pipeline.transcribe import Transcript

    captured: dict = {}

    class FakeTranscriptAgent:
        def __init__(self, client):  # noqa: ARG002
            pass

        def run(self, inp, *, ctx: RunContext | None = None):  # noqa: ARG002
            captured["ctx"] = ctx
            # Match TranscriptOutput shape used by the shim
            return SimpleNamespace(words=[], full_text="", low_confidence=False)

    with patch.object(gemini_analyzer, "transcribe") as _:
        pass  # placeholder — actual patch below

    # Patch the symbol the shim imports inside its function body.
    with (
        patch("app.agents.transcript.TranscriptAgent", FakeTranscriptAgent),
        patch("app.agents._model_client.default_client", return_value=object()),
    ):
        result = gemini_analyzer.transcribe(_file_ref(), job_id="job-xyz")

    assert isinstance(result, Transcript)
    assert captured["ctx"] is not None
    assert captured["ctx"].job_id == "job-xyz"


def test_transcribe_default_job_id_is_none():
    from app.pipeline.agents import gemini_analyzer

    captured: dict = {}

    class FakeTranscriptAgent:
        def __init__(self, client):  # noqa: ARG002
            pass

        def run(self, inp, *, ctx: RunContext | None = None):  # noqa: ARG002
            captured["ctx"] = ctx
            return SimpleNamespace(words=[], full_text="", low_confidence=False)

    with (
        patch("app.agents.transcript.TranscriptAgent", FakeTranscriptAgent),
        patch("app.agents._model_client.default_client", return_value=object()),
    ):
        gemini_analyzer.transcribe(_file_ref())

    assert captured["ctx"] is not None
    assert captured["ctx"].job_id is None


# ── analyze_clip() ───────────────────────────────────────────────────────────


def test_analyze_clip_threads_job_id_to_agent_run():
    from app.pipeline.agents import gemini_analyzer

    captured: dict = {}

    class FakeClipMetadataAgent:
        def __init__(self, client):  # noqa: ARG002
            pass

        def run(self, inp, *, ctx: RunContext | None = None):  # noqa: ARG002
            captured["ctx"] = ctx
            return SimpleNamespace(
                transcript="",
                hook_text="",
                hook_score=0.0,
                best_moments=[],
                detected_subject=None,
            )

    with (
        patch("app.agents.clip_metadata.ClipMetadataAgent", FakeClipMetadataAgent),
        patch("app.agents._model_client.default_client", return_value=object()),
    ):
        gemini_analyzer.analyze_clip(_file_ref(), job_id="job-clip-123")

    assert captured["ctx"].job_id == "job-clip-123"


# ── analyze_template() ───────────────────────────────────────────────────────


def test_analyze_template_threads_job_id_to_agent_run():
    from app.pipeline.agents import gemini_analyzer

    captured: dict = {}

    class FakeTemplateRecipeAgent:
        def __init__(self, client):  # noqa: ARG002
            pass

        def run(self, inp, *, ctx: RunContext | None = None):  # noqa: ARG002
            captured["ctx"] = ctx
            return SimpleNamespace(
                shot_count=0,
                total_duration_s=0.0,
                hook_duration_s=0.0,
                slots=[],
                copy_tone="",
                caption_style="",
                beat_timestamps_s=[],
                creative_direction="",
                transition_style="",
                color_grade="",
                pacing_style="",
                sync_style="",
                interstitials=[],
                subject_niche="",
                has_talking_head=False,
                has_voiceover=False,
                has_permanent_letterbox=False,
            )

    # analysis_mode="single" skips the creative_direction call.
    with (
        patch("app.agents.template_recipe.TemplateRecipeAgent", FakeTemplateRecipeAgent),
        patch("app.agents._model_client.default_client", return_value=object()),
    ):
        gemini_analyzer.analyze_template(
            _file_ref(),
            analysis_mode="single",
            job_id="template:tpl-7",
        )

    assert captured["ctx"].job_id == "template:tpl-7"


# ── _extract_creative_direction() ────────────────────────────────────────────


def test_extract_creative_direction_threads_job_id_to_agent_run():
    from app.pipeline.agents import gemini_analyzer

    captured: dict = {}

    class FakeCreativeDirectionAgent:
        def __init__(self, client):  # noqa: ARG002
            pass

        def run(self, inp, *, ctx: RunContext | None = None):  # noqa: ARG002
            captured["ctx"] = ctx
            return SimpleNamespace(text="some direction")

    with (
        patch(
            "app.agents.creative_direction.CreativeDirectionAgent",
            FakeCreativeDirectionAgent,
        ),
        patch("app.agents._model_client.default_client", return_value=object()),
    ):
        result = gemini_analyzer._extract_creative_direction(
            client=object(),
            file_ref=_file_ref(),
            genai_types=object(),
            job_id="template:tpl-9",
        )

    assert result == "some direction"
    assert captured["ctx"].job_id == "template:tpl-9"


# ── analyze_audio_template() ─────────────────────────────────────────────────


def test_analyze_audio_template_threads_job_id_to_agent_run():
    from app.pipeline.agents import gemini_analyzer

    captured: dict = {}

    class FakeAudioTemplateAgent:
        def __init__(self, client):  # noqa: ARG002
            pass

        def run(self, inp, *, ctx: RunContext | None = None):  # noqa: ARG002
            captured["ctx"] = ctx
            out = SimpleNamespace(
                slots=[],
                color_grade="",
                interstitials=[],
            )
            out.to_dict = lambda: {"slots": [], "color_grade": "", "interstitials": []}
            return out

    file_ref = SimpleNamespace(uri="files/audio", mime_type="audio/mp4", name="a")
    with (
        patch("app.agents.audio_template.AudioTemplateAgent", FakeAudioTemplateAgent),
        patch("app.agents._model_client.default_client", return_value=object()),
    ):
        gemini_analyzer.analyze_audio_template(
            file_ref=file_ref,
            beat_timestamps_s=[1.0, 2.0],
            track_config={"best_start_s": 0.0, "best_end_s": 10.0},
            duration_s=10.0,
            job_id="track:trk-42",
        )

    assert captured["ctx"].job_id == "track:trk-42"


# ── generate_copy() ──────────────────────────────────────────────────────────


def test_generate_copy_threads_job_id_to_agent_run():
    from app.pipeline.agents import copy_writer

    captured: dict = {}

    class FakePlatformCopyAgent:
        def __init__(self, client):  # noqa: ARG002
            pass

        def run(self, inp, *, ctx: RunContext | None = None):  # noqa: ARG002
            captured["ctx"] = ctx
            return SimpleNamespace(
                value=copy_writer.PlatformCopy(
                    tiktok=copy_writer.TikTokCopy(
                        hook="h",
                        caption="c",
                        hashtags=["a"] * 5,
                    ),
                    instagram=copy_writer.InstagramCopy(
                        hook="h",
                        caption="c",
                        hashtags=["a"] * 10,
                    ),
                    youtube=copy_writer.YouTubeCopy(
                        title="t",
                        description="d",
                        tags=["a"] * 15,
                    ),
                ),
            )

    with (
        patch("app.agents.platform_copy.PlatformCopyAgent", FakePlatformCopyAgent),
        patch("app.agents._model_client.default_client", return_value=object()),
    ):
        _copy, status = copy_writer.generate_copy(
            hook_text="hook",
            transcript_excerpt="ex",
            platforms=["tiktok"],
            job_id="job-copy-99",
        )

    assert status == "generated"
    assert captured["ctx"].job_id == "job-copy-99"


def test_generate_copy_default_job_id_is_none():
    """Backward-compat: omitted job_id falls through as None (same as today)."""
    from app.pipeline.agents import copy_writer

    captured: dict = {}

    class FakePlatformCopyAgent:
        def __init__(self, client):  # noqa: ARG002
            pass

        def run(self, inp, *, ctx: RunContext | None = None):  # noqa: ARG002
            captured["ctx"] = ctx
            return SimpleNamespace(
                value=copy_writer.PlatformCopy(
                    tiktok=copy_writer.TikTokCopy(
                        hook="h",
                        caption="c",
                        hashtags=["a"] * 5,
                    ),
                    instagram=copy_writer.InstagramCopy(
                        hook="h",
                        caption="c",
                        hashtags=["a"] * 10,
                    ),
                    youtube=copy_writer.YouTubeCopy(
                        title="t",
                        description="d",
                        tags=["a"] * 15,
                    ),
                ),
            )

    with (
        patch("app.agents.platform_copy.PlatformCopyAgent", FakePlatformCopyAgent),
        patch("app.agents._model_client.default_client", return_value=object()),
    ):
        copy_writer.generate_copy(
            hook_text="hook",
            transcript_excerpt="ex",
            platforms=["tiktok"],
        )

    assert captured["ctx"] is not None
    assert captured["ctx"].job_id is None


# ── job_id is keyword-only (TypeError on positional pass) ────────────────────


def test_generate_copy_job_id_is_keyword_only():
    """`job_id` must be kwargs-only so the positional signature stays stable."""
    from app.pipeline.agents.copy_writer import generate_copy

    with pytest.raises(TypeError):
        # 6th positional arg should fail — job_id is kwarg-only.
        generate_copy("h", "t", ["tiktok"], True, "", "job-x")  # type: ignore[misc]


def test_analyze_clip_job_id_is_keyword_only():
    from app.pipeline.agents.gemini_analyzer import analyze_clip

    with pytest.raises(TypeError):
        # 5th positional arg should fail — job_id is kwarg-only.
        analyze_clip(_file_ref(), 0.0, 1.0, "", "job-x")  # type: ignore[misc]
