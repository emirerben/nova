"""Subtitled language re-transcribe task (D5 override) + caption-language lockstep.

Mirrors the reburn test pattern (fake job session + IO patches). No network: whisper,
correction, storage, and burn are all monkeypatched.
"""

from __future__ import annotations

import typing
import uuid

import pytest

import app.tasks.generative_build as gb


class _FakeSession:
    def __init__(self, job):
        self._job = job

    def get(self, _model, _pk, **_kw):  # with_for_update etc.
        return self._job

    def commit(self):  # pragma: no cover - no-op
        pass

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


class _FakeJob:
    def __init__(self, assembly_plan=None):
        self.id = uuid.uuid4()
        self.assembly_plan = assembly_plan or {}


def _patch_job_session(monkeypatch, job):
    monkeypatch.setattr(gb, "_sync_session", lambda: _FakeSession(job))


def _subtitled_variant(**over):
    v = {
        "variant_id": "subtitled",
        "rank": 1,
        "render_status": "ready",
        "resolved_archetype": "subtitled",
        "base_video_path": "generative-jobs/j/variant_1_subtitled_base.mp4",
        "video_path": "generative-jobs/j/variant_1_subtitled.mp4",
        "output_url": "https://signed/old",
        "caption_language": "en",
        "voiceover_caption_style": "sentence",
        "caption_cues": [{"text": "hello there", "start_s": 0.0, "end_s": 1.2}],
    }
    v.update(over)
    return v


class _Word:
    def __init__(self, text, start_s, end_s):
        self.text, self.start_s, self.end_s = text, start_s, end_s


class _Transcript:
    def __init__(self, words, language="tr"):
        self.words = words
        self.language = language


def _patch_retx_io(monkeypatch, seen: dict):
    def _dl(_src, dst):
        with open(dst, "wb") as f:
            f.write(b"x")

    monkeypatch.setattr("app.storage.download_to_file", _dl)
    monkeypatch.setattr("app.storage.upload_public_read", lambda *a, **k: "https://signed/new")
    monkeypatch.setattr(
        "app.storage.delete_object_best_effort",
        lambda path: seen.setdefault("deleted", []).append(path) or True,
    )
    monkeypatch.setattr(
        "app.pipeline.transcribe.transcribe_whisper",
        lambda _p, language=None: _Transcript(
            [_Word("merhaba", 0.0, 0.6), _Word("dünya.", 0.6, 1.2)], language=language or "tr"
        ),
    )
    # Correction passes text through untouched (its own tests cover behavior).
    monkeypatch.setattr(
        "app.pipeline.caption_correct.correct_caption_cues", lambda cues, *a, **k: cues
    )

    def _gen(cues, path, **kwargs):
        seen["gen_kwargs"] = kwargs
        with open(path, "w", encoding="utf-8") as f:
            f.write("ass")

    def _gen_pop(cues, path, **kwargs):
        seen["pop_kwargs"] = kwargs
        with open(path, "w", encoding="utf-8") as f:
            f.write("ass")

    monkeypatch.setattr("app.pipeline.captions.generate_ass_from_cues", _gen)
    monkeypatch.setattr("app.pipeline.captions.generate_word_pop_ass", _gen_pop)

    def _burn(*_a, **_k):
        seen["burned"] = True

    monkeypatch.setattr("app.pipeline.narrated_assembler.burn_captions_on_video", _burn)


# ── guards ────────────────────────────────────────────────────────────────────


def test_retranscribe_rejects_unsupported_language(monkeypatch):
    job = _FakeJob(assembly_plan={"variants": [_subtitled_variant()]})
    _patch_job_session(monkeypatch, job)
    with pytest.raises(ValueError, match="unsupported caption language"):
        gb._run_retranscribe_subtitled(str(uuid.uuid4()), "subtitled", "de")


def test_retranscribe_rejects_non_subtitled_variant(monkeypatch):
    job = _FakeJob(assembly_plan={"variants": [_subtitled_variant(resolved_archetype="narrated")]})
    _patch_job_session(monkeypatch, job)
    with pytest.raises(ValueError, match="not a subtitled variant"):
        gb._run_retranscribe_subtitled(str(uuid.uuid4()), "subtitled", "tr")


def test_retranscribe_rejects_missing_base(monkeypatch):
    job = _FakeJob(assembly_plan={"variants": [_subtitled_variant(base_video_path=None)]})
    _patch_job_session(monkeypatch, job)
    with pytest.raises(ValueError, match="no caption-free base"):
        gb._run_retranscribe_subtitled(str(uuid.uuid4()), "subtitled", "tr")


# ── happy path ────────────────────────────────────────────────────────────────


def test_retranscribe_happy_rebuilds_cues_and_language(monkeypatch):
    job = _FakeJob(assembly_plan={"variants": [_subtitled_variant()]})
    _patch_job_session(monkeypatch, job)
    seen: dict = {}
    _patch_retx_io(monkeypatch, seen)

    gb._run_retranscribe_subtitled(str(job.id), "subtitled", "tr")

    v = job.assembly_plan["variants"][0]
    assert seen.get("burned") is True
    assert v["caption_language"] == "tr"
    assert v["render_status"] == "ready"
    assert v["video_path"].startswith("generative-jobs/") and "_lang_" in v["video_path"]
    assert v["caption_cues"] and "merhaba" in v["caption_cues"][0]["text"]
    # sentence style → plain ASS at the platform-safe margin with the pop-in (must
    # match the first burn or edited captions jump)
    from app.pipeline.captions import SUBTITLED_CAPTION_MARGIN_V

    assert seen["gen_kwargs"]["style"] == "plain"
    assert seen["gen_kwargs"]["margin_v"] == SUBTITLED_CAPTION_MARGIN_V
    assert seen["gen_kwargs"]["pop_in"] is True
    # superseded burn freed (generative-jobs/* never expires on its own)
    assert seen.get("deleted") == ["generative-jobs/j/variant_1_subtitled.mp4"]


def test_retranscribe_word_style_routes_to_word_pop(monkeypatch):
    job = _FakeJob(assembly_plan={"variants": [_subtitled_variant(voiceover_caption_style="word")]})
    _patch_job_session(monkeypatch, job)
    seen: dict = {}
    _patch_retx_io(monkeypatch, seen)

    gb._run_retranscribe_subtitled(str(job.id), "subtitled", "tr")

    assert "pop_kwargs" in seen and "gen_kwargs" not in seen
    from app.pipeline.captions import SUBTITLED_CAPTION_MARGIN_V

    assert seen["pop_kwargs"]["margin_v"] == SUBTITLED_CAPTION_MARGIN_V


def test_retranscribe_empty_transcript_keeps_existing_captions(monkeypatch):
    """A wrong-language pass that hears nothing must NOT destroy the user's cues or
    swap the video — it keeps everything and just clears the rendering state."""
    original = _subtitled_variant(render_status="rendering")
    job = _FakeJob(assembly_plan={"variants": [dict(original)]})
    _patch_job_session(monkeypatch, job)
    seen: dict = {}
    _patch_retx_io(monkeypatch, seen)
    monkeypatch.setattr(
        "app.pipeline.transcribe.transcribe_whisper",
        lambda _p, language=None: _Transcript([], language=language or "tr"),
    )

    gb._run_retranscribe_subtitled(str(job.id), "subtitled", "tr")

    v = job.assembly_plan["variants"][0]
    assert seen.get("burned") is None  # nothing re-burned
    assert v["caption_cues"] == original["caption_cues"]  # cues intact
    assert v["video_path"] == original["video_path"]  # video untouched
    assert v["caption_language"] == "en"  # language NOT flipped to the failed target
    assert v["render_status"] == "ready"


def test_retranscribe_task_failure_resets_render_status(monkeypatch):
    """The Celery wrapper's except must flip render_status back to 'ready' — without
    it a failed re-transcribe leaves the variant stuck 'rendering' forever."""
    job = _FakeJob(assembly_plan={"variants": [_subtitled_variant(render_status="rendering")]})
    _patch_job_session(monkeypatch, job)

    def _boom(*_a, **_k):
        raise RuntimeError("whisper 500")

    monkeypatch.setattr(gb, "_run_retranscribe_subtitled", _boom)
    patched: dict = {}
    monkeypatch.setattr(
        gb, "_update_variant_entry", lambda _j, _v, patch, **kw: patched.update(patch)
    )

    gb.retranscribe_subtitled_captions.run(str(job.id), "subtitled", "tr")

    assert patched.get("render_status") == "ready"


# ── regen guard ───────────────────────────────────────────────────────────────


def test_regenerate_rejects_caption_variants(monkeypatch):
    """The generic re-render funnels into the MONTAGE path — running it on a caption
    variant would overwrite resolved_archetype/video_path and orphan the user's
    (possibly hand-edited) captions. Hard-reject, mirroring the reburn guard."""

    class _Job2(_FakeJob):
        def __init__(self, variant):
            super().__init__(assembly_plan={"variants": [variant]})
            self.all_candidates = {"clip_paths": [], "language": "en"}

    for archetype in ("subtitled", "narrated"):
        variant = _subtitled_variant(resolved_archetype=archetype, render_status="rendering")
        job = _Job2(variant)
        _patch_job_session(monkeypatch, job)
        patched: dict = {}
        monkeypatch.setattr(
            gb, "_update_variant_entry", lambda _j, _v, patch, _p=patched, **kw: _p.update(patch)
        )

        gb._run_regenerate_variant(str(job.id), "subtitled", None, None, False)

        # rejected: no montage render ran; the in-flight state was released.
        assert patched.get("render_status") == "ready"
        assert job.assembly_plan["variants"][0]["resolved_archetype"] == archetype


# ── lockstep (route ⇄ worker ⇄ request Literal) ──────────────────────────────


def test_caption_language_sets_lockstep():
    from app.routes import generative_jobs as gj

    assert gj._SUBTITLED_CAPTION_LANGUAGES == gb._SUBTITLED_CAPTION_LANGUAGES
    literal = typing.get_args(gj.CaptionLanguageRequest.model_fields["language"].annotation)
    assert set(literal) == gj._SUBTITLED_CAPTION_LANGUAGES


# ── CaptionWord bounds ────────────────────────────────────────────────────────


def test_caption_word_rejects_infinity():
    from pydantic import ValidationError

    from app.routes.generative_jobs import CaptionCue

    with pytest.raises(ValidationError):
        CaptionCue(
            text="x",
            start_s=0.0,
            end_s=1.0,
            words=[{"text": "x", "start_s": float("inf"), "end_s": 1.0}],
        )


def test_caption_cue_caps_words_length():
    from pydantic import ValidationError

    from app.routes.generative_jobs import CaptionCue

    too_many = [{"text": "w", "start_s": i * 0.1, "end_s": i * 0.1 + 0.1} for i in range(101)]
    with pytest.raises(ValidationError):
        CaptionCue(text="x", start_s=0.0, end_s=20.0, words=too_many)


def test_caption_cue_words_roundtrip_exclude_none():
    from app.routes.generative_jobs import CaptionCue

    plain = CaptionCue(text="a", start_s=0.0, end_s=1.0)
    assert "words" not in plain.model_dump(exclude_none=True)
    word = CaptionCue(
        text="a b",
        start_s=0.0,
        end_s=1.0,
        words=[{"text": "a", "start_s": 0.0, "end_s": 0.5}],
    )
    assert word.model_dump(exclude_none=True)["words"][0]["text"] == "a"
