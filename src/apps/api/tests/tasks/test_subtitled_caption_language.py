"""KRI-177: talking-to-camera captions must default to the clip's SPOKEN
language, never silently to the job/plan's UI/content language — the only
thing allowed to override that default is the creator explicitly asking for
captions in a specific language (`caption_language_request`, parsed upstream
by `app.pipeline.caption_language.parse_caption_language_request` and wired
onto ``all_candidates``).

Cloud coverage exercises `_render_subtitled_variant` directly, reusing
`_patch_subtitled_smart_render`'s I/O stubs (see
`test_generative_build.py::test_subtitled_render_invokes_and_persists_smart_captions`).
Phone coverage exercises `_run_phone_subtitled_job` end-to-end via
`_run_generative_job`, reusing `_setup_subtitled` from
`test_phone_subtitled_narrated_dispatch.py`.
"""

from __future__ import annotations

import types
import uuid
from unittest.mock import Mock

import app.tasks.generative_build as gb
from app.pipeline.transcribe import Transcript
from tests.tasks.test_generative_build import _patch_subtitled_smart_render
from tests.tasks.test_phone_subtitled_narrated_dispatch import _setup_subtitled, _words

# --------------------------------------------------------------------------
# Cloud: _render_subtitled_variant
# --------------------------------------------------------------------------


def _mock_transcribe_whisper(monkeypatch, *, language: str, full_text: str = "") -> Mock:
    """Patch the flag-off/live-transcribe call site and return the Mock so
    callers can assert the `language=` hint it was invoked with."""
    mock = Mock(
        return_value=types.SimpleNamespace(
            language=language,
            full_text=full_text,
            words=[types.SimpleNamespace(text="Hello", start_s=0.0, end_s=1.0)],
        )
    )
    monkeypatch.setattr("app.pipeline.transcribe.transcribe_whisper", mock)
    return mock


def _mock_correct_caption_cues(monkeypatch) -> Mock:
    """Replace the identity stub `_patch_subtitled_smart_render` installs with
    a Mock so tests can assert the `detected_lang` passed as cue-correction
    language (the second positional arg)."""
    mock = Mock(side_effect=lambda cues, *_a, **_k: cues)
    monkeypatch.setattr("app.pipeline.caption_correct.correct_caption_cues", mock)
    return mock


def test_en_clip_with_tr_job_language_captions_en(monkeypatch, tmp_path):
    """(a) Whisper detects "en"; job/plan language is "tr" — captions stay en,
    auto-detect (language=None) was used, never the job language."""
    _patch_subtitled_smart_render(monkeypatch, tmp_path)
    transcribe_mock = _mock_transcribe_whisper(monkeypatch, language="en", full_text="Hello there")
    correct_mock = _mock_correct_caption_cues(monkeypatch)

    result = gb._render_subtitled_variant(
        job_id=str(uuid.uuid4()),
        rank=1,
        spec={"variant_id": "subtitled", "caption_style": "sentence"},
        clip_id_to_local={"clip-1": str(tmp_path / "source.mp4")},
        variant_dir=str(tmp_path),
        language="tr",
        smart_captions=None,
    )

    assert result["ok"] is True
    assert result["caption_language"] == "en"
    assert transcribe_mock.call_args.kwargs.get("language") is None
    assert correct_mock.call_args.args[1] == "en"


def test_tr_clip_with_en_job_language_captions_tr(monkeypatch, tmp_path):
    """(b) Mirror of (a): whisper detects "tr", job language "en" — captions tr."""
    _patch_subtitled_smart_render(monkeypatch, tmp_path)
    transcribe_mock = _mock_transcribe_whisper(monkeypatch, language="tr", full_text="Merhaba")

    result = gb._render_subtitled_variant(
        job_id=str(uuid.uuid4()),
        rank=1,
        spec={"variant_id": "subtitled", "caption_style": "sentence"},
        clip_id_to_local={"clip-1": str(tmp_path / "source.mp4")},
        variant_dir=str(tmp_path),
        language="en",
        smart_captions=None,
    )

    assert result["ok"] is True
    assert result["caption_language"] == "tr"
    assert transcribe_mock.call_args.kwargs.get("language") is None


def test_explicit_request_en_clip_to_tr(monkeypatch, tmp_path):
    """(c) Creator explicitly asked for Turkish captions — the whisper hint
    carries the request, and it wins regardless of the job's own language."""
    _patch_subtitled_smart_render(monkeypatch, tmp_path)
    transcribe_mock = _mock_transcribe_whisper(monkeypatch, language="tr", full_text="Merhaba")

    result = gb._render_subtitled_variant(
        job_id=str(uuid.uuid4()),
        rank=1,
        spec={"variant_id": "subtitled", "caption_style": "sentence"},
        clip_id_to_local={"clip-1": str(tmp_path / "source.mp4")},
        variant_dir=str(tmp_path),
        language="en",
        smart_captions=None,
        caption_language_request="tr",
    )

    assert result["ok"] is True
    assert result["caption_language"] == "tr"
    assert transcribe_mock.call_args.kwargs.get("language") == "tr"


def test_explicit_request_tr_clip_to_en(monkeypatch, tmp_path):
    """(c) Same override, opposite direction."""
    _patch_subtitled_smart_render(monkeypatch, tmp_path)
    transcribe_mock = _mock_transcribe_whisper(monkeypatch, language="en", full_text="Hello")

    result = gb._render_subtitled_variant(
        job_id=str(uuid.uuid4()),
        rank=1,
        spec={"variant_id": "subtitled", "caption_style": "sentence"},
        clip_id_to_local={"clip-1": str(tmp_path / "source.mp4")},
        variant_dir=str(tmp_path),
        language="tr",
        smart_captions=None,
        caption_language_request="en",
    )

    assert result["ok"] is True
    assert result["caption_language"] == "en"
    assert transcribe_mock.call_args.kwargs.get("language") == "en"


def test_unsupported_request_value_is_ignored(monkeypatch, tmp_path):
    """A stray/legacy value (not in SUPPORTED_CAPTION_LANGUAGES) must never
    silently become an override — re-validated inside the render fn too."""
    _patch_subtitled_smart_render(monkeypatch, tmp_path)
    transcribe_mock = _mock_transcribe_whisper(monkeypatch, language="en", full_text="Hello")

    result = gb._render_subtitled_variant(
        job_id=str(uuid.uuid4()),
        rank=1,
        spec={"variant_id": "subtitled", "caption_style": "sentence"},
        clip_id_to_local={"clip-1": str(tmp_path / "source.mp4")},
        variant_dir=str(tmp_path),
        language="tr",
        smart_captions=None,
        caption_language_request="fr",
    )

    assert result["ok"] is True
    assert result["caption_language"] == "en"
    assert transcribe_mock.call_args.kwargs.get("language") is None


def test_empty_detection_infers_turkish_from_transcript_text(monkeypatch, tmp_path):
    """(d) Whisper reports no language, but the transcript text is clearly
    Turkish — infer "tr" (never the job's "en") and record the fallback."""
    _patch_subtitled_smart_render(monkeypatch, tmp_path)
    _mock_transcribe_whisper(monkeypatch, language="", full_text="bu çok güzel bir gün bugün")
    from app.services import pipeline_trace

    events = Mock()
    monkeypatch.setattr(pipeline_trace, "record_pipeline_event", events)

    result = gb._render_subtitled_variant(
        job_id=str(uuid.uuid4()),
        rank=1,
        spec={"variant_id": "subtitled", "caption_style": "sentence"},
        clip_id_to_local={"clip-1": str(tmp_path / "source.mp4")},
        variant_dir=str(tmp_path),
        language="en",
        smart_captions=None,
    )

    assert result["ok"] is True
    assert result["caption_language"] == "tr"
    fallback_calls = [c for c in events.call_args_list if c.args[1] == "caption_language_fallback"]
    assert len(fallback_calls) == 1
    payload = fallback_calls[0].args[2]
    assert payload["source"] == "transcript_text"
    assert payload["language"] == "tr"
    assert payload["job_language"] == "en"


def test_empty_detection_undecidable_text_falls_back_to_job_language(monkeypatch, tmp_path):
    """(e) Whisper reports no language and the text gives no EN/TR signal —
    the job language is used deliberately, and the fallback is still recorded
    (never a silent, untraceable "en")."""
    _patch_subtitled_smart_render(monkeypatch, tmp_path)
    _mock_transcribe_whisper(monkeypatch, language="", full_text="xyzzy plugh qwerty")
    from app.services import pipeline_trace

    events = Mock()
    monkeypatch.setattr(pipeline_trace, "record_pipeline_event", events)

    result = gb._render_subtitled_variant(
        job_id=str(uuid.uuid4()),
        rank=1,
        spec={"variant_id": "subtitled", "caption_style": "sentence"},
        clip_id_to_local={"clip-1": str(tmp_path / "source.mp4")},
        variant_dir=str(tmp_path),
        language="tr",
        smart_captions=None,
    )

    assert result["ok"] is True
    assert result["caption_language"] == "tr"
    fallback_calls = [c for c in events.call_args_list if c.args[1] == "caption_language_fallback"]
    assert len(fallback_calls) == 1
    payload = fallback_calls[0].args[2]
    assert payload["source"] == "fallback"
    assert payload["language"] == "tr"


def test_silence_cut_verbatim_request_retranscribes_original_clip(monkeypatch, tmp_path):
    """(f) The verbatim silence-cut word list is in English, but the creator
    asked for Turkish captions: the ORIGINAL clip is re-transcribed with the
    hint (never the already-cut base) and its words replace the verbatim ones."""
    from app.pipeline.silence_cut import CutPlan

    _patch_subtitled_smart_render(monkeypatch, tmp_path)
    monkeypatch.setattr(gb.settings, "silence_cut_enabled", True, raising=False)

    sc_plan = CutPlan(keep_segments=[(0.0, 2.0)], removed=[], time_saved_s=0.0)
    sc_entry = {
        "failed": False,
        "words": [{"text": "Hello", "start_s": 0.0, "end_s": 1.0}],
        "language": "en",
        "plan": sc_plan,
        "speech_cleanup_outcome_context": None,
        "retake_span_count": 0,
    }
    monkeypatch.setattr(gb, "_silence_cut_analysis", lambda *a, **k: sc_entry)

    cached_mock = Mock(
        return_value=Transcript(
            words=_words(("Merhaba", 0.0, 1.0)), language="tr", full_text="Merhaba"
        )
    )
    monkeypatch.setattr("app.pipeline.transcribe.transcribe_whisper_cached", cached_mock)
    from app.services import pipeline_trace

    events = Mock()
    monkeypatch.setattr(pipeline_trace, "record_pipeline_event", events)

    result = gb._render_subtitled_variant(
        job_id=str(uuid.uuid4()),
        rank=1,
        spec={"variant_id": "subtitled", "caption_style": "sentence"},
        clip_id_to_local={"clip-1": str(tmp_path / "source.mp4")},
        variant_dir=str(tmp_path),
        language="en",
        smart_captions=None,
        caption_language_request="tr",
    )

    assert result["ok"] is True
    assert result["caption_language"] == "tr"
    cached_mock.assert_called_once()
    assert cached_mock.call_args.args[0] == str(tmp_path / "source.mp4")
    assert cached_mock.call_args.kwargs.get("language") == "tr"
    requested_calls = [
        c for c in events.call_args_list if c.args[1] == "caption_language_requested"
    ]
    assert len(requested_calls) == 1
    assert requested_calls[0].args[2]["requested"] == "tr"
    assert requested_calls[0].args[2]["spoken"] == "en"


def test_silence_cut_verbatim_request_matching_spoken_language_is_a_noop(monkeypatch, tmp_path):
    """(f, negative case) When the request equals what was actually spoken,
    today's verbatim path is untouched — no second transcription call."""
    from app.pipeline.silence_cut import CutPlan

    _patch_subtitled_smart_render(monkeypatch, tmp_path)
    monkeypatch.setattr(gb.settings, "silence_cut_enabled", True, raising=False)

    sc_plan = CutPlan(keep_segments=[(0.0, 2.0)], removed=[], time_saved_s=0.0)
    sc_entry = {
        "failed": False,
        "words": [{"text": "Hello", "start_s": 0.0, "end_s": 1.0}],
        "language": "en",
        "plan": sc_plan,
        "speech_cleanup_outcome_context": None,
        "retake_span_count": 0,
    }
    monkeypatch.setattr(gb, "_silence_cut_analysis", lambda *a, **k: sc_entry)
    cached_mock = Mock()
    monkeypatch.setattr("app.pipeline.transcribe.transcribe_whisper_cached", cached_mock)

    result = gb._render_subtitled_variant(
        job_id=str(uuid.uuid4()),
        rank=1,
        spec={"variant_id": "subtitled", "caption_style": "sentence"},
        clip_id_to_local={"clip-1": str(tmp_path / "source.mp4")},
        variant_dir=str(tmp_path),
        language="tr",
        smart_captions=None,
        caption_language_request="en",
    )

    assert result["ok"] is True
    assert result["caption_language"] == "en"
    cached_mock.assert_not_called()


# --------------------------------------------------------------------------
# Phone: _run_phone_subtitled_job (via _run_generative_job dispatch)
# --------------------------------------------------------------------------


def test_phone_en_clip_with_tr_job_language_captions_en(monkeypatch):
    job, _snapshot, _session, _binding = _setup_subtitled(monkeypatch)
    job.all_candidates["language"] = "tr"
    mock = Mock(
        return_value=Transcript(words=_words(("Hello", 0.0, 0.5)), language="en", full_text="Hello")
    )
    monkeypatch.setattr("app.pipeline.transcribe.transcribe_whisper_cached", mock)

    gb._run_generative_job(str(job.id))

    variant = job.assembly_plan["variants"][0]
    assert variant["caption_language"] == "en"
    assert mock.call_args.kwargs.get("language") is None


def test_phone_tr_clip_with_en_job_language_captions_tr(monkeypatch):
    job, _snapshot, _session, _binding = _setup_subtitled(monkeypatch)
    job.all_candidates["language"] = "en"
    mock = Mock(
        return_value=Transcript(
            words=_words(("Merhaba", 0.0, 0.5)), language="tr", full_text="Merhaba"
        )
    )
    monkeypatch.setattr("app.pipeline.transcribe.transcribe_whisper_cached", mock)

    gb._run_generative_job(str(job.id))

    variant = job.assembly_plan["variants"][0]
    assert variant["caption_language"] == "tr"
    assert mock.call_args.kwargs.get("language") is None


def test_phone_explicit_request_overrides_hint_and_persists(monkeypatch):
    job, _snapshot, _session, _binding = _setup_subtitled(monkeypatch)
    job.all_candidates["language"] = "en"
    job.all_candidates["caption_language_request"] = "tr"
    mock = Mock(
        return_value=Transcript(words=_words(("Hello", 0.0, 0.5)), language="en", full_text="Hello")
    )
    monkeypatch.setattr("app.pipeline.transcribe.transcribe_whisper_cached", mock)

    gb._run_generative_job(str(job.id))

    variant = job.assembly_plan["variants"][0]
    assert variant["caption_language"] == "tr"
    assert mock.call_args.kwargs.get("language") == "tr"


def test_phone_empty_detection_infers_from_transcript_text(monkeypatch):
    job, _snapshot, _session, _binding = _setup_subtitled(monkeypatch)
    job.all_candidates["language"] = "en"
    tr_text = "bu çok güzel bir gün bugün"
    mock = Mock(
        return_value=Transcript(words=_words(("bu", 0.0, 0.5)), language="", full_text=tr_text)
    )
    monkeypatch.setattr("app.pipeline.transcribe.transcribe_whisper_cached", mock)
    from app.services import pipeline_trace

    events = Mock()
    monkeypatch.setattr(pipeline_trace, "record_pipeline_event", events)

    gb._run_generative_job(str(job.id))

    variant = job.assembly_plan["variants"][0]
    assert variant["caption_language"] == "tr"
    fallback_calls = [c for c in events.call_args_list if c.args[1] == "caption_language_fallback"]
    assert len(fallback_calls) == 1
    assert fallback_calls[0].args[2]["source"] == "transcript_text"
    assert fallback_calls[0].args[2]["job_language"] == "en"


# --------------------------------------------------------------------------
# Phone: whisper's detection cross-checked against Gemini's clip transcript
# --------------------------------------------------------------------------

# Job 385e3b13 (2026-09-24): Turkish-accented English. whisper-1 auto-detected
# "tr" and wrote a Turkish translation ("Number three" -> "Üçüncü", "no" ->
# "Hayır"), so every English reaction-beat trigger was "never heard".
_GEMINI_EN = (
    "Let's talk about the best football players in Turkish Super League this season. "
    "Number three, Mac Ingram? No, no, no, no, no. Number three, Rafael Leao."
)
_WHISPER_TR_TEXT = "Bu sezon Türkiye Super Ligi'de en iyi futbolcular hakkında konuşalım. Üçüncü?"


def _gemini_heard(monkeypatch, binding, transcript: str) -> None:
    from tests.tasks.test_generative_build import _Meta

    meta = _Meta("c0", 5.0, transcript=transcript)
    monkeypatch.setattr(
        gb,
        "_ingest_clips",
        lambda *a, **k: {
            "clip_metas": [meta],
            "clip_id_to_gcs": {"c0": binding.proxy_path},
            "clip_id_to_local": {"c0": "/tmp/c0.mp4"},
            "probe_map": {},
            "hero": meta,
        },
        raising=False,
    )


def _whisper_by_language(monkeypatch, by_language: dict) -> Mock:
    mock = Mock(side_effect=lambda *_a, language=None, **_k: by_language[language])
    monkeypatch.setattr("app.pipeline.transcribe.transcribe_whisper_cached", mock)
    return mock


def test_phone_retranscribes_in_the_language_gemini_heard(monkeypatch):
    job, _snapshot, _session, binding = _setup_subtitled(monkeypatch)
    _gemini_heard(monkeypatch, binding, _GEMINI_EN)
    mock = _whisper_by_language(
        monkeypatch,
        {
            None: Transcript(
                words=_words(("Üçüncü?", 5.76, 6.3)), language="tr", full_text=_WHISPER_TR_TEXT
            ),
            "en": Transcript(
                words=_words(("Number", 5.76, 6.0), ("three.", 6.0, 6.3)),
                language="en",
                full_text="Number three.",
            ),
        },
    )
    from app.services import pipeline_trace

    events = Mock()
    monkeypatch.setattr(pipeline_trace, "record_pipeline_event", events)

    gb._run_generative_job(str(job.id))

    assert [c.kwargs.get("language") for c in mock.call_args_list] == [None, "en"]
    variant = job.assembly_plan["variants"][0]
    assert variant["caption_language"] == "en"
    crosscheck = [c for c in events.call_args_list if c.args[1] == "caption_language_crosscheck"]
    assert [c.args[2] for c in crosscheck] == [
        {
            "variant_id": "subtitled",
            "whisper_language": "tr",
            "reference_language": "en",
            "applied": True,
        }
    ]


def test_phone_keeps_whisper_when_gemini_agrees(monkeypatch):
    job, _snapshot, _session, binding = _setup_subtitled(monkeypatch)
    _gemini_heard(monkeypatch, binding, _GEMINI_EN)
    mock = _whisper_by_language(
        monkeypatch,
        {None: Transcript(words=_words(("Number", 0.0, 0.5)), language="en", full_text="Number")},
    )

    gb._run_generative_job(str(job.id))

    assert mock.call_count == 1
    assert job.assembly_plan["variants"][0]["caption_language"] == "en"


def test_phone_explicit_request_skips_the_crosscheck(monkeypatch):
    job, _snapshot, _session, binding = _setup_subtitled(monkeypatch)
    job.all_candidates["caption_language_request"] = "tr"
    _gemini_heard(monkeypatch, binding, _GEMINI_EN)
    mock = _whisper_by_language(
        monkeypatch,
        {"tr": Transcript(words=_words(("Üçüncü", 0.0, 0.5)), language="tr", full_text="Üçüncü")},
    )

    gb._run_generative_job(str(job.id))

    assert [c.kwargs.get("language") for c in mock.call_args_list] == ["tr"]
    assert job.assembly_plan["variants"][0]["caption_language"] == "tr"


def test_phone_keeps_first_pass_when_the_retranscribe_is_empty(monkeypatch):
    job, _snapshot, _session, binding = _setup_subtitled(monkeypatch)
    _gemini_heard(monkeypatch, binding, _GEMINI_EN)
    _whisper_by_language(
        monkeypatch,
        {
            None: Transcript(
                words=_words(("Üçüncü?", 5.76, 6.3)), language="tr", full_text=_WHISPER_TR_TEXT
            ),
            "en": Transcript(words=[], language="en", full_text=""),
        },
    )

    gb._run_generative_job(str(job.id))

    assert job.assembly_plan["variants"][0]["caption_language"] == "tr"
