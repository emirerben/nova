"""Narrated (voiceover) edits honour the creator's explicit caption-language ask.

Prod thread 2ef61a47 (Cappadocia): a Turkish voiceover, and the creator asked for
English subtitles so foreign followers understand. Kria promised English, but the
narrated renderers captioned from the voiceover's spoken language only — just the
Talking (subtitled) path read `caption_language_request`. Both narrated renderers
now caption from a hinted whisper pass in the requested language (the same
mechanism the Talking override uses), while every step timing still comes from the
spoken words.

Phone coverage drives `_run_phone_narrated_job` through `_setup_narrated` from
`test_phone_subtitled_narrated_dispatch.py`; cloud coverage calls
`_render_narrated_variant` with the I/O stubs `test_narrated_storyboard.py` uses.
"""

from __future__ import annotations

import uuid
from unittest.mock import Mock

import pytest

import app.tasks.generative_build as gb
from app.pipeline.transcribe import Transcript
from app.services.device_render import device_status
from tests.tasks.test_phone_subtitled_narrated_dispatch import (
    _raw_preflight_snapshot,
    _setup_narrated,
    _words,
)

_TR_WORDS = _words(
    ("Sabah", 0.0, 0.5),
    ("balonlar.", 0.5, 1.0),
    ("Sonra", 4.0, 4.5),
    ("yürü.", 4.5, 5.0),
    ("Gün", 8.0, 8.5),
    ("batımı.", 8.5, 9.0),
)
_EN_WORDS = _words(
    ("Morning", 0.0, 0.6),
    ("balloons.", 0.6, 1.1),
    ("Then", 4.0, 4.4),
    ("walk.", 4.4, 5.0),
    ("Sunset", 8.0, 8.6),
    ("ends.", 8.6, 9.1),
)
_SPOKEN_TR = Transcript(
    words=_TR_WORDS, language="tr", full_text=" ".join(w.text for w in _TR_WORDS)
)
_HINTED_EN = Transcript(
    words=_EN_WORDS, language="en", full_text=" ".join(w.text for w in _EN_WORDS)
)


def _whisper_by_hint(monkeypatch, by_hint: dict) -> Mock:
    """Patch `transcribe_whisper` to answer per `language=` hint (None = auto-detect)."""
    mock = Mock(side_effect=lambda *_a, language=None, **_k: by_hint[language])
    monkeypatch.setattr("app.pipeline.transcribe.transcribe_whisper", mock)
    return mock


def _events(monkeypatch) -> Mock:
    from app.services import pipeline_trace

    events = Mock()
    monkeypatch.setattr(pipeline_trace, "record_pipeline_event", events)
    return events


def _event_payloads(events: Mock, name: str) -> list[dict]:
    return [c.args[2] for c in events.call_args_list if c.args[1] == name]


def _phrase_words_spy(monkeypatch) -> list[list[str]]:
    """Record the words the step segmentation runs on (same phrases as the setup)."""
    seen: list[list[str]] = []

    def split(words, **_kwargs):
        seen.append([w.text for w in words])
        return [
            {"speech_start_s": 0.0, "speech_end_s": 4.0},
            {"speech_start_s": 4.0, "speech_end_s": 8.0},
            {"speech_start_s": 8.0, "speech_end_s": 12.0},
        ]

    monkeypatch.setattr("app.pipeline.phrase_sequence.split_phrases", split)
    return seen


def _cue_text(variant: dict) -> str:
    return " ".join(cue["text"] for cue in variant["caption_cues"])


# --------------------------------------------------------------------------
# Phone: _run_phone_narrated_job
# --------------------------------------------------------------------------


def test_phone_turkish_voiceover_with_english_ask_captions_in_english(monkeypatch):
    job, _snapshot, _session, _bindings = _setup_narrated(
        monkeypatch, extra_candidates={"caption_language_request": "en"}
    )
    whisper = _whisper_by_hint(monkeypatch, {None: _SPOKEN_TR, "en": _HINTED_EN})
    segmented = _phrase_words_spy(monkeypatch)
    events = _events(monkeypatch)

    gb._run_generative_job(str(job.id))

    assert job.status == "awaiting_device"
    [variant] = job.assembly_plan["variants"]
    assert variant["caption_language"] == "en"
    assert "Morning" in _cue_text(variant) and "Sabah" not in _cue_text(variant)
    # Timing still comes from the spoken (Turkish) words, never the translation.
    assert segmented == [[w.text for w in _TR_WORDS]]
    assert [(t["start_s"], t["end_s"]) for t in variant["narrated_timings"]] == [
        (0.0, 4.0),
        (4.0, 8.0),
        (8.0, 12.0),
    ]
    # The hinted pass transcribes the same local voiceover the recipe plays.
    assert [c.kwargs.get("language") for c in whisper.call_args_list] == [None, "en"]
    assert whisper.call_args_list[0].args[0] == whisper.call_args_list[1].args[0]
    assert _event_payloads(events, "caption_language_requested") == [
        {"variant_id": "narrated", "requested": "en", "spoken": "tr"}
    ]
    recipe = device_status(job, "narrated").request.recipe
    burned = " ".join(run.text for layer in recipe.text_layers for run in layer.runs)
    assert "Morning" in burned and "Sabah" not in burned


def test_phone_without_an_ask_captions_in_the_spoken_language(monkeypatch):
    job, snapshot, _session, _bindings = _setup_narrated(monkeypatch)
    whisper = _whisper_by_hint(monkeypatch, {None: _SPOKEN_TR})

    gb._run_phone_narrated_job(str(job.id), snapshot, job.all_candidates, ownership_epoch=3)

    [variant] = job.assembly_plan["variants"]
    assert variant["caption_language"] == "tr"
    assert "Sabah" in _cue_text(variant)
    assert whisper.call_count == 1


@pytest.mark.parametrize("request_value", ["tr", "fr", 7])
def test_phone_ask_matching_the_voice_or_unsupported_is_a_noop(monkeypatch, request_value):
    job, snapshot, _session, _bindings = _setup_narrated(
        monkeypatch, extra_candidates={"caption_language_request": request_value}
    )
    whisper = _whisper_by_hint(monkeypatch, {None: _SPOKEN_TR})

    gb._run_phone_narrated_job(str(job.id), snapshot, job.all_candidates, ownership_epoch=3)

    [variant] = job.assembly_plan["variants"]
    assert variant["caption_language"] == "tr"
    assert "Sabah" in _cue_text(variant)
    assert whisper.call_count == 1


def test_phone_empty_hinted_pass_keeps_spoken_captions_and_records_it(monkeypatch):
    job, snapshot, _session, _bindings = _setup_narrated(
        monkeypatch, extra_candidates={"caption_language_request": "en"}
    )
    _whisper_by_hint(monkeypatch, {None: _SPOKEN_TR, "en": Transcript(words=[], language="en")})
    events = _events(monkeypatch)

    gb._run_phone_narrated_job(str(job.id), snapshot, job.all_candidates, ownership_epoch=3)

    [variant] = job.assembly_plan["variants"]
    assert variant["caption_language"] == "tr"
    assert "Sabah" in _cue_text(variant)
    assert _event_payloads(events, "caption_language_request_empty") == [
        {"variant_id": "narrated", "requested": "en", "spoken": "tr"}
    ]
    assert _event_payloads(events, "caption_language_requested") == []


def test_phone_required_cleanup_translates_the_cleaned_voiceover_only(monkeypatch):
    """`required_v1` still never re-transcribes the cleaned voiceover for TIMING
    (the cut's own words drive it); the one hinted pass is for the captions, on
    the cleaned audio the recipe plays, so cue times match it."""
    from app.schemas.edit_proposal import NarrationSpeechCleanup, NarrationTrack, NarrationWord

    job, snapshot, _session, _bindings = _setup_narrated(
        monkeypatch, extra_candidates={"caption_language_request": "en"}
    )
    snapshot["speech_cleanup_contract"] = "required_v1"
    snapshot["_speech_cleanup_internal"] = {
        "preflight_snapshot": _raw_preflight_snapshot(
            storage_path=job.all_candidates["voiceover_gcs_path"],
            kind="voiceover",
            media_identity="voiceover-1",
        )
    }
    cleaned = NarrationTrack(
        gcs_path="users/u/plan/i/speech-cleanup/analysis/deadbeef.wav",
        generation="9",
        duration_s=9.0,
        language="tr",
        words=[
            NarrationWord(text="Sabah", start_s=0.0, end_s=0.5),
            NarrationWord(text="balonlar.", start_s=0.5, end_s=1.0),
            NarrationWord(text="Gün", start_s=6.0, end_s=6.5),
            NarrationWord(text="batımı.", start_s=6.5, end_s=7.0),
        ],
        speech_cleanup=NarrationSpeechCleanup(
            analysis_id=str(uuid.uuid4()),
            source_gcs_path=job.all_candidates["voiceover_gcs_path"],
            source_generation="1",
            source_duration_s=12.0,
            cut_sha256="b" * 64,
        ),
    )
    monkeypatch.setattr(gb, "_phone_narrated_cleaned_narration", lambda *a, **k: cleaned)
    downloads: list[tuple[str, str]] = []
    monkeypatch.setattr(
        "app.storage.download_to_file", lambda src, dst, *a, **k: downloads.append((src, dst))
    )
    whisper = _whisper_by_hint(monkeypatch, {"en": _HINTED_EN})
    segmented = _phrase_words_spy(monkeypatch)

    gb._run_phone_narrated_job(str(job.id), snapshot, job.all_candidates, ownership_epoch=3)

    [variant] = job.assembly_plan["variants"]
    assert variant["caption_language"] == "en"
    assert "Morning" in _cue_text(variant)
    assert segmented == [["Sabah", "balonlar.", "Gün", "batımı."]]
    [call] = whisper.call_args_list
    assert call.kwargs["language"] == "en"
    assert downloads == [(cleaned.gcs_path, call.args[0])]
    assert device_status(job, "narrated").request.recipe.duration == pytest.approx(9.0)


# --------------------------------------------------------------------------
# Cloud: _render_narrated_variant
# --------------------------------------------------------------------------


def _render_cloud(monkeypatch, tmp_path, **kwargs) -> dict:
    seen: dict = {}

    def fake_download(_gcs_path, local_path):
        with open(local_path, "wb") as handle:
            handle.write(b"voice")

    def fake_assemble(step_timings, _assignments, voiceover, output_path, _tmpdir, **kw):
        seen["timings"] = step_timings
        seen["voiceover"] = voiceover
        seen["caption_words"] = [w.text for w in kw["transcript"].words]
        for path in (output_path, kw["base_output_path"]):
            with open(path, "wb") as handle:
                handle.write(b"video")
        return []

    monkeypatch.setattr(gb.settings, "narrated_storyboard_enabled", False)
    monkeypatch.setattr("app.storage.download_to_file", fake_download)
    monkeypatch.setattr("app.storage.upload_public_read", lambda *_a, **_k: "signed")
    monkeypatch.setattr("app.pipeline.narrated_assembler.assemble_narrated", fake_assemble)
    monkeypatch.setattr("app.tasks.template_orchestrate._probe_duration", lambda _path: 9.0)
    segmented = _phrase_words_spy(monkeypatch)
    result = gb._render_narrated_variant(
        job_id="job",
        rank=1,
        spec={"variant_id": "narrated", "voiceover_gcs_path": "voiceover/file"},
        filming_guide=[],
        narrative_order=["clip_0"],
        clip_id_to_local={"clip_0": "/tmp/clip.mp4"},
        variant_dir=str(tmp_path),
        **kwargs,
    )
    seen["segmented"] = segmented
    return {"result": result, **seen}


def test_cloud_turkish_voiceover_with_english_ask_burns_english_captions(monkeypatch, tmp_path):
    whisper = _whisper_by_hint(monkeypatch, {None: _SPOKEN_TR, "en": _HINTED_EN})

    rendered = _render_cloud(monkeypatch, tmp_path, caption_language_request="en")

    assert rendered["result"]["ok"] is True
    assert rendered["caption_words"] == [w.text for w in _EN_WORDS]
    assert rendered["segmented"] == [[w.text for w in _TR_WORDS]]
    assert [c.kwargs.get("language") for c in whisper.call_args_list] == [None, "en"]
    assert whisper.call_args_list[1].args[0] == rendered["voiceover"]


def test_cloud_without_an_ask_burns_the_spoken_captions(monkeypatch, tmp_path):
    whisper = _whisper_by_hint(monkeypatch, {None: _SPOKEN_TR})

    rendered = _render_cloud(monkeypatch, tmp_path)

    assert rendered["caption_words"] == [w.text for w in _TR_WORDS]
    assert whisper.call_count == 1


def test_cloud_dispatcher_passes_the_ask_to_the_narrated_render(monkeypatch):
    """The dispatcher's parsed ask must reach `_render_narrated_variant` (the
    subtitled branch right below it already passes the same key)."""
    from tests.tasks.test_route_override_flips import VOICE_FILE, _cloud

    run = _cloud(
        monkeypatch, stamped=True, edit_format="narrated_ready", audio_strategy="voiceover"
    )
    run.job.all_candidates["voiceover_gcs_path"] = VOICE_FILE
    run.job.all_candidates["caption_language_request"] = "en"
    monkeypatch.setattr(gb, "_set_status", lambda *a, **k: True)
    monkeypatch.setattr(gb, "_existing_variants", lambda *a, **k: [])
    monkeypatch.setattr(gb, "_persist_archetype_fallback", lambda *a, **k: None)
    monkeypatch.setattr(gb, "_update_variant_entry", lambda *a, **k: True)
    monkeypatch.setattr(gb, "_upsert_variant_entry", lambda *a, **k: True)
    monkeypatch.setattr(gb, "_maybe_add_text_elements_snapshot", lambda *a, **k: None)
    monkeypatch.setattr(gb, "_finalize_job", lambda *a, **k: None)
    monkeypatch.setattr(gb, "_maybe_autoplace_after_finalize", lambda *a, **k: None)
    monkeypatch.setattr(gb, "_merge_speech_cut_prior_state", lambda _id, result, **k: result)
    seen: dict = {}

    def narrated(**kwargs):
        seen.update(kwargs)
        return {"ok": True, "variant_id": "narrated", "rank": 1, "render_status": "ready"}

    monkeypatch.setattr(gb, "_render_narrated_variant", narrated)
    gb._run_generative_job(str(run.job.id))

    assert seen["caption_language_request"] == "en"
