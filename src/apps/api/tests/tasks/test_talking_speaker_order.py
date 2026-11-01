"""Clip-analysis completion order must never decide who the talking spine is.

Gemini analyses finish in whatever order the network returns them. A multi-clip
Talking edit reads its speakers in `clip_metas` order, so a racy order put the
creator's own take (uploaded first) last and a stranger's clip first.
"""

from __future__ import annotations

import threading
import types
import uuid
from unittest.mock import patch

import app.tasks.generative_build as gb
import app.tasks.template_orchestrate as template_orchestrate
from app.pipeline.agents.gemini_analyzer import ClipMeta
from app.services.speech_cleanup_identity import ClipSourceIdentity

_GATE_TIMEOUT_S = 10.0


def _meta(clip_id: str) -> ClipMeta:
    return ClipMeta(
        clip_id=clip_id,
        transcript="t",
        hook_text="h",
        hook_score=5.0,
        best_moments=[{"start_s": 0.0, "end_s": 5.0, "energy": 5.0, "description": ""}],
    )


def _reverse_completion_analyzer(refs: list):
    """`analyze_clip` stand-in where clip i finishes only after clip i+1 has."""
    done = [threading.Event() for _ in refs]
    slot_by_ref = {id(ref): slot for slot, ref in enumerate(refs)}

    def analyze(ref, **_kwargs):
        slot = slot_by_ref[id(ref)]
        if slot + 1 < len(refs):
            assert done[slot + 1].wait(_GATE_TIMEOUT_S), "reverse completion gate timed out"
        done[slot].set()
        return _meta(ref.name)

    return analyze


def test_analyze_clips_returns_records_in_input_order_when_completion_is_reversed() -> None:
    refs = [types.SimpleNamespace(name=f"files/{slot}") for slot in range(4)]
    paths = [f"/tmp/clip_{slot}.mp4" for slot in range(4)]

    with patch.object(template_orchestrate, "analyze_clip", _reverse_completion_analyzer(refs)):
        records, failed = template_orchestrate._analyze_clips_parallel_indexed(refs, paths)
        plain, failed_count = template_orchestrate._analyze_clips_parallel(refs, paths)

    assert failed == []
    assert [slot for slot, _meta in records] == [0, 1, 2, 3]
    assert [meta.clip_path for _slot, meta in records] == paths
    assert failed_count == 0
    assert [meta.clip_path for meta in plain] == paths


def _words(count: int) -> list:
    return [
        types.SimpleNamespace(text=f"kelime{index}", start_s=index * 0.3, end_s=index * 0.3 + 0.25)
        for index in range(count)
    ]


def test_talking_spine_is_the_first_uploaded_speaker_whatever_the_analysis_order(
    monkeypatch, tmp_path
) -> None:
    paths = [f"gcs/source-{slot}.mp4" for slot in range(4)]
    identity = ClipSourceIdentity("assigned", tuple(paths), tuple(str(uuid.uuid4()) for _ in paths))
    refs = [types.SimpleNamespace(name=f"files/slot{slot}") for slot in range(4)]
    spoken_words = {0: 40, 1: 0, 2: 12, 3: 20}  # slot 0 is the creator's own take
    local_to_slot: dict[str, int] = {}

    def download(gcs_paths, _tmpdir):
        local = [str(tmp_path / f"source-{slot}.mp4") for slot in range(len(gcs_paths))]
        local_to_slot.update({path: slot for slot, path in enumerate(local)})
        return local

    probe = types.SimpleNamespace(duration_s=20.0, width=1080, height=1920, color_trc="bt709")
    monkeypatch.setattr(gb, "_load_preprocessed_source_cache", lambda *a, **k: None)
    monkeypatch.setattr(gb, "_record_render_subphase", lambda *a, **k: None)
    monkeypatch.setattr(gb, "_read_all_candidates", lambda _job_id: {})
    monkeypatch.setattr(gb, "_merge_all_candidates", lambda *_a, **_k: True)
    monkeypatch.setattr(gb, "_speech_cleanup_identity_for_paths", lambda *_a: identity)
    monkeypatch.setattr(
        "app.pipeline.source_guard.downscale_oversized_sources", lambda *a, **k: None
    )
    monkeypatch.setattr("app.services.pipeline_trace.record_pipeline_event", lambda *_a, **_k: None)
    monkeypatch.setattr(template_orchestrate, "_download_clips_parallel", download)
    monkeypatch.setattr(
        template_orchestrate, "_probe_clips", lambda local: {path: probe for path in local}
    )
    monkeypatch.setattr(template_orchestrate, "_upload_clips_parallel", lambda _local: refs)
    monkeypatch.setattr(template_orchestrate, "analyze_clip", _reverse_completion_analyzer(refs))

    ingest = gb._ingest_clips(paths, str(tmp_path), job_id=str(uuid.uuid4()))

    def transcribe(path, language=None):
        count = spoken_words[local_to_slot[path]]
        return types.SimpleNamespace(words=_words(count), full_text=" ".join(["w"] * count))

    with (
        patch(
            "app.services.clip_speech.audio_presence",
            return_value=types.SimpleNamespace(status="ok"),
        ),
        patch("app.pipeline.transcribe.transcribe_whisper_cached", side_effect=transcribe),
    ):
        roles, _transcripts = gb._classify_phone_talking_clips(
            ingest["clip_metas"],
            ingest["clip_id_to_local"],
            ingest["probe_map"],
            caption_language=None,
            job_id=str(uuid.uuid4()),
        )

    assert roles.speakers == ("files/slot0", "files/slot2", "files/slot3")
    assert roles.footage == ("files/slot1",)


def test_identity_cache_hit_returns_metas_in_source_slot_order(monkeypatch, tmp_path) -> None:
    from app.services.speech_cleanup_identity import (
        CLIP_METADATA_IDENTITY_INDEX_KEY,
        build_clip_metadata_identity_index,
    )

    paths = [f"gcs/source-{slot}.mp4" for slot in range(3)]
    source_ids = tuple(str(uuid.uuid4()) for _ in paths)
    identity = ClipSourceIdentity("assigned", tuple(paths), source_ids)
    metas = {slot: _meta(f"files/slot{slot}") for slot in range(3)}
    # A cache written before the source fix holds records in completion order.
    envelope = build_clip_metadata_identity_index(
        records=[
            (slot, f"files/slot{slot}", gb._clip_meta_to_cache(metas[slot])) for slot in (2, 0, 1)
        ],
        failed_source_slots=[],
        source_instance_ids=source_ids,
    )
    candidates = {
        "clip_metadata_cache": {
            "version": gb._CLIP_METADATA_CACHE_VERSION,
            "fingerprint": gb._cache_fingerprint(paths),
            "clip_metas": [gb._clip_meta_to_cache(metas[slot]) for slot in (2, 0, 1)],
        },
        CLIP_METADATA_IDENTITY_INDEX_KEY: envelope,
    }
    probe = types.SimpleNamespace(duration_s=6.0, width=1080, height=1920, color_trc="bt709")
    monkeypatch.setattr(gb, "_load_preprocessed_source_cache", lambda *a, **k: None)
    monkeypatch.setattr(gb, "_record_render_subphase", lambda *a, **k: None)
    monkeypatch.setattr(gb, "_read_all_candidates", lambda _job_id: candidates)
    monkeypatch.setattr(gb, "_speech_cleanup_identity_for_paths", lambda *_a: identity)
    monkeypatch.setattr(
        "app.pipeline.source_guard.downscale_oversized_sources", lambda *a, **k: None
    )
    monkeypatch.setattr("app.services.pipeline_trace.record_pipeline_event", lambda *_a, **_k: None)
    monkeypatch.setattr(
        template_orchestrate,
        "_download_clips_parallel",
        lambda gcs, _tmpdir: [str(tmp_path / f"source-{slot}.mp4") for slot in range(len(gcs))],
    )
    monkeypatch.setattr(
        template_orchestrate, "_probe_clips", lambda local: {path: probe for path in local}
    )

    ingest = gb._ingest_clips(paths, str(tmp_path), job_id=str(uuid.uuid4()))

    assert [meta.clip_id for meta in ingest["clip_metas"]] == [
        "files/slot0",
        "files/slot1",
        "files/slot2",
    ]
    assert list(ingest["clip_id_to_local"]) == ["files/slot0", "files/slot1", "files/slot2"]
