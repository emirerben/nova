"""Content-addressed transcript cache (plan 012 P1-4).

whisper-1 is non-deterministic, so re-renders of the same clip caption
differently. The cache keys the transcript by clip content hash so every
re-render reuses the identical words. Fully fail-open.
"""

from __future__ import annotations

import app.pipeline.transcribe as transcribe_mod
import app.storage as storage_mod
from app.config import settings
from app.pipeline.transcribe import (
    Transcript,
    Word,
    _transcript_from_json,
    _transcript_to_json,
    transcribe_whisper_cached,
)


def _fake_clip(tmp_path):
    clip = tmp_path / "clip.mp4"
    clip.write_bytes(b"FAKECLIPBYTES" * 200)
    return str(clip)


def _install_fakes(monkeypatch, store: dict, counter: dict):
    def fake_transcribe(path, *, model=None, language=None, verbatim_prompt=None):
        counter["n"] += 1
        return Transcript(words=[Word("Messi", 0.0, 0.5, 0.9)], full_text="Messi", language="en")

    monkeypatch.setattr(transcribe_mod, "transcribe_whisper", fake_transcribe)
    monkeypatch.setattr(storage_mod, "object_exists", lambda p: p in store)
    monkeypatch.setattr(
        storage_mod,
        "upload_bytes_public_read",
        lambda data, p, content_type="x": store.__setitem__(p, data) or "url",
    )
    monkeypatch.setattr(
        storage_mod, "download_to_file", lambda p, local: open(local, "wb").write(store[p])
    )


def test_transcript_roundtrip() -> None:
    tr = Transcript(
        words=[Word("Lionel", 0.0, 0.4, 0.9), Word("Messi", 0.4, 0.8, 0.95)],
        full_text="Lionel Messi",
        language="en",
    )
    back = _transcript_from_json(_transcript_to_json(tr))
    assert [w.text for w in back.words] == ["Lionel", "Messi"]
    assert back.language == "en"


def test_cache_hit_reuses_transcript(monkeypatch, tmp_path) -> None:
    store: dict = {}
    counter = {"n": 0}
    _install_fakes(monkeypatch, store, counter)
    monkeypatch.setattr(settings, "smart_caption_transcript_cache_enabled", True, raising=False)
    clip = _fake_clip(tmp_path)
    r1 = transcribe_whisper_cached(clip, language=None)  # miss → transcribe + store
    r2 = transcribe_whisper_cached(clip, language=None)  # hit → reuse
    assert counter["n"] == 1, "second render must not re-transcribe"
    assert r1.words[0].text == r2.words[0].text == "Messi"


def test_cache_disabled_always_transcribes(monkeypatch, tmp_path) -> None:
    store: dict = {}
    counter = {"n": 0}
    _install_fakes(monkeypatch, store, counter)
    monkeypatch.setattr(settings, "smart_caption_transcript_cache_enabled", False, raising=False)
    clip = _fake_clip(tmp_path)
    transcribe_whisper_cached(clip, language=None)
    transcribe_whisper_cached(clip, language=None)
    assert counter["n"] == 2


def test_cache_fails_open_on_missing_clip(monkeypatch, tmp_path) -> None:
    store: dict = {}
    counter = {"n": 0}
    _install_fakes(monkeypatch, store, counter)
    monkeypatch.setattr(settings, "smart_caption_transcript_cache_enabled", True, raising=False)
    # unhashable path → falls through to a live transcribe, never raises
    out = transcribe_whisper_cached("/nonexistent/clip.mp4", language=None)
    assert counter["n"] == 1
    assert out.words[0].text == "Messi"


def test_cache_read_error_falls_through(monkeypatch, tmp_path) -> None:
    counter = {"n": 0}

    def fake_transcribe(path, *, model=None, language=None, verbatim_prompt=None):
        counter["n"] += 1
        return Transcript(words=[Word("Messi", 0.0, 0.5, 0.9)], language="en")

    monkeypatch.setattr(transcribe_mod, "transcribe_whisper", fake_transcribe)

    def boom(_p):
        raise RuntimeError("gcs down")

    monkeypatch.setattr(storage_mod, "object_exists", boom)
    monkeypatch.setattr(storage_mod, "upload_bytes_public_read", lambda *a, **k: "url")
    monkeypatch.setattr(settings, "smart_caption_transcript_cache_enabled", True, raising=False)
    out = transcribe_whisper_cached(_fake_clip(tmp_path), language=None)
    assert counter["n"] == 1 and out.words[0].text == "Messi"  # no crash, live result


def test_cache_corrupt_hit_falls_through(monkeypatch, tmp_path) -> None:
    # object_exists reports a HIT but the stored bytes are not valid JSON — the
    # parse failure inside the hit path must fall through to a live transcribe,
    # not crash the render.
    counter = {"n": 0}

    def fake_transcribe(path, *, model=None, language=None, verbatim_prompt=None):
        counter["n"] += 1
        return Transcript(words=[Word("Messi", 0.0, 0.5, 0.9)], language="en")

    monkeypatch.setattr(transcribe_mod, "transcribe_whisper", fake_transcribe)
    monkeypatch.setattr(storage_mod, "object_exists", lambda _p: True)  # claims a hit
    monkeypatch.setattr(
        storage_mod, "download_to_file", lambda _p, local: open(local, "wb").write(b"not json{{{")
    )
    monkeypatch.setattr(storage_mod, "upload_bytes_public_read", lambda *a, **k: "url")
    monkeypatch.setattr(settings, "smart_caption_transcript_cache_enabled", True, raising=False)
    out = transcribe_whisper_cached(_fake_clip(tmp_path), language=None)
    assert counter["n"] == 1 and out.words[0].text == "Messi"  # corrupt hit → live result


def test_cache_write_failure_returns_live_result(monkeypatch, tmp_path) -> None:
    # A cache miss transcribes live, then the store upload raises — the docstring
    # promises "a cache write failure never affects the returned result".
    counter = {"n": 0}

    def fake_transcribe(path, *, model=None, language=None, verbatim_prompt=None):
        counter["n"] += 1
        return Transcript(words=[Word("Messi", 0.0, 0.5, 0.9)], language="en")

    def boom(*_a, **_k):
        raise RuntimeError("gcs write down")

    monkeypatch.setattr(transcribe_mod, "transcribe_whisper", fake_transcribe)
    monkeypatch.setattr(storage_mod, "object_exists", lambda _p: False)  # miss
    monkeypatch.setattr(storage_mod, "upload_bytes_public_read", boom)
    monkeypatch.setattr(settings, "smart_caption_transcript_cache_enabled", True, raising=False)
    out = transcribe_whisper_cached(_fake_clip(tmp_path), language=None)
    assert counter["n"] == 1 and out.words[0].text == "Messi"  # write fail → live result intact
