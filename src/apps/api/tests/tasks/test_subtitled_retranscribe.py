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


def test_retranscribe_rejects_smart_variant_without_atomic_replan(monkeypatch):
    job = _FakeJob(assembly_plan={"variants": [_subtitled_variant(smart_captions_applied=True)]})
    _patch_job_session(monkeypatch, job)

    with pytest.raises(ValueError, match="require a new render"):
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


def test_retranscribe_flag_on_routes_through_subtitled_compose(monkeypatch):
    monkeypatch.setattr(gb.settings, "subtitled_text_lane_enabled", True, raising=False)
    job = _FakeJob(
        assembly_plan={
            "variants": [
                _subtitled_variant(
                    text_elements=[
                        {
                            "id": "title",
                            "text": "TITLE",
                            "start_s": 0.0,
                            "end_s": 2.0,
                            "role": "generative_intro",
                            "position": "middle",
                        }
                    ],
                    text_elements_user_edited=True,
                )
            ]
        }
    )
    _patch_job_session(monkeypatch, job)
    seen: dict = {}
    _patch_retx_io(monkeypatch, seen)

    def _compose(base_local, variant, tmpdir):
        seen["compose_variant"] = dict(variant)
        out = f"{tmpdir}/composed.mp4"
        with open(out, "wb") as f:
            f.write(b"composed")
        return out

    monkeypatch.setattr(gb, "_compose_subtitled_final", _compose)

    gb._run_retranscribe_subtitled(str(job.id), "subtitled", "tr")

    assert seen["compose_variant"]["caption_language"] == "tr"
    assert seen["compose_variant"]["caption_cues"][0]["text"].startswith("merhaba")
    assert "gen_kwargs" not in seen and "pop_kwargs" not in seen


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


# ── Smart Captions v2 metadata round-trip ────────────────────────────────────
#
# The captions PATCH replaces the ENTIRE cue list with
# `[CaptionCue.model_validate(c).model_dump(exclude_none=True) for c in cues]`
# (persist_variant_captions + the editor-commit path), so editing ONE cue's text
# round-trips EVERY cue through this model. Anything not whitelisted here is
# permanently stripped from all cues on the first edit.


def _smart_persisted_cue() -> dict:
    """A cue as the Smart compiler persists it (chunk_words_into_cues fields +
    compiler smart_style + prepare_smart_caption_cues render caches)."""
    return {
        "text": "sana bir sey gosterecegim",
        "start_s": 1.0,
        "end_s": 2.4,
        "words": [
            {"text": "sana", "start_s": 1.0, "end_s": 1.4, "timing_quality": "aligned"},
            {"text": "bir", "start_s": 1.4, "end_s": 1.7, "timing_quality": "aligned"},
            {"text": "sey", "start_s": 1.7, "end_s": 2.0, "timing_quality": "segment_estimate"},
            {"text": "gosterecegim", "start_s": 2.0, "end_s": 2.4, "timing_quality": "aligned"},
        ],
        "smart_word_ids": ["w000001", "w000002", "w000003", "w000004"],
        # context_shift is a SemanticRole but NOT a smart_style token — the two
        # vocabularies differ, so this also pins that they stay distinct fields.
        "smart_role": "context_shift",
        "smart_style": "context",
        "smart_render_lines": ["sana bir sey", "gosterecegim"],
        "smart_render_font_size_px": 64,
        "smart_render_box": {"x_px": 108, "y_px": 1150, "width_px": 864, "height_px": 210},
    }


def test_caption_cue_roundtrip_preserves_smart_metadata():
    """Server-authored Smart provenance must survive the caption-edit round-trip.

    smart_style is the ASS styling carrier at reburn; smart_role/smart_word_ids
    are the planner's semantic provenance (plan 011 builds on this same
    round-trip); words[].timing_quality is the chunker's alignment confidence.
    The derived smart_render_* caches are deliberately dropped —
    generate_ass_from_cues recomputes them from text + the pinned policy at
    every burn, so persisting client-sent values would be dead weight.
    """
    from app.routes.generative_jobs import CaptionCue

    dumped = CaptionCue.model_validate(_smart_persisted_cue()).model_dump(exclude_none=True)
    assert dumped["smart_style"] == "context"
    assert dumped["smart_role"] == "context_shift"
    assert dumped["smart_word_ids"] == ["w000001", "w000002", "w000003", "w000004"]
    assert [w["timing_quality"] for w in dumped["words"]] == [
        "aligned",
        "aligned",
        "segment_estimate",
        "aligned",
    ]
    assert "smart_render_lines" not in dumped
    assert "smart_render_font_size_px" not in dumped
    assert "smart_render_box" not in dumped


def test_caption_cue_smart_word_ids_bounded():
    """smart_word_ids is user input on the PATCH edge — enforce the same closed
    format the smart_edit schemas use (w000001) and a hard count cap so the
    debounced caption PATCH can't become an unbounded JSONB write surface."""
    from pydantic import ValidationError

    from app.routes.generative_jobs import CaptionCue

    base = {"text": "x", "start_s": 0.0, "end_s": 1.0}
    with pytest.raises(ValidationError):
        CaptionCue.model_validate({**base, "smart_word_ids": ["not-a-word-id"]})
    with pytest.raises(ValidationError):
        CaptionCue.model_validate({**base, "smart_word_ids": [f"w{i:06d}" for i in range(101)]})
    with pytest.raises(ValidationError):
        CaptionCue.model_validate({**base, "smart_role": "not-a-role"})


def test_caption_edit_roundtrip_keeps_smart_style_in_reburn_ass(tmp_path):
    """End-to-end sentinel for the plan-011 review suspicion: edit ONE cue's
    text, round-trip ALL cues through CaptionCue exactly as the captions PATCH
    does, reburn — the untouched cue must keep its role styling in the ASS.
    Guards the smart_style whitelist entry: removing it silently un-styles
    every Smart caption on the first edit."""
    from app.pipeline.captions import _SMART_CAPTION_TAGS, generate_ass_from_cues
    from app.routes.generative_jobs import CaptionCue

    stored = [
        {**_smart_persisted_cue(), "smart_role": "hook", "smart_style": "hook"},
        {**_smart_persisted_cue(), "start_s": 2.4, "end_s": 3.8, "text": "ilk adim"},
    ]
    edited = [dict(stored[0]), {**stored[1], "text": "ilk adim (duzeltildi)"}]
    roundtripped = [CaptionCue.model_validate(c).model_dump(exclude_none=True) for c in edited]

    out = tmp_path / "reburn.ass"
    generate_ass_from_cues(roundtripped, str(out), font_name="TikTok Sans")
    content = out.read_text(encoding="utf-8")
    assert _SMART_CAPTION_TAGS["hook"] in content
    assert _SMART_CAPTION_TAGS["context"] in content
