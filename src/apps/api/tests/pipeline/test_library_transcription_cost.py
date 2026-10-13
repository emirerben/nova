"""Library transcription spends once, records uncertainty, and isolates cache versions."""

import wave
from types import SimpleNamespace

import httpx
import openai
import pytest

from app.agents._runtime import RunContext
from app.pipeline import transcribe as tr
from app.services import ai_cost_control as ledger


@pytest.fixture
def audio(tmp_path):
    path = tmp_path / "audio.wav"
    with wave.open(str(path), "wb") as stream:
        stream.setnchannels(1)
        stream.setsampwidth(2)
        stream.setframerate(16000)
        stream.writeframes(b"\0\0" * 16000 * 60)
    return str(path)


@pytest.mark.parametrize("outcome", ["success", "budget", "transport", "rejected"])
def test_library_paid_call_fence_and_duration_cost(audio, monkeypatch, outcome):
    calls = []
    receipt = object()

    def reserve(request):
        calls.append(("reserve", request))
        if outcome == "budget":
            raise RuntimeError("budget unavailable")
        return receipt

    def provider(**kwargs):
        calls.append(("provider", kwargs["model"]))
        if outcome == "transport":
            raise openai.APIConnectionError(request=httpx.Request("POST", "https://example.test"))
        if outcome == "rejected":
            raise openai.BadRequestError(
                "invalid",
                response=httpx.Response(400, request=httpx.Request("POST", "https://example.test")),
                body=None,
            )
        return SimpleNamespace(words=[], text="", language="en")

    def client(**kwargs):
        assert kwargs["max_retries"] == 0
        return SimpleNamespace(
            audio=SimpleNamespace(transcriptions=SimpleNamespace(create=provider))
        )

    monkeypatch.setattr(openai, "OpenAI", client)
    monkeypatch.setattr(ledger, "reserve_paid_call", reserve)
    for name in ("mark_paid_call_started", "mark_paid_call_unknown", "release_paid_call"):
        monkeypatch.setattr(ledger, name, lambda value, name=name: calls.append((name, value)))
    monkeypatch.setattr(
        ledger, "settle_paid_call_cost", lambda value, **kw: calls.append(("settle", kw))
    )
    ctx = RunContext(
        creator_id="00000000-0000-0000-0000-000000000001", request_id="library-attempt"
    )
    if outcome == "success":
        tr._transcribe_openai(audio, run_context=ctx)
        assert calls[-1][0] == "settle"
        assert calls[-1][1]["cost_usd"] == pytest.approx(0.006)
    else:
        with pytest.raises(Exception):
            tr._transcribe_openai(audio, run_context=ctx)
        if outcome == "budget":
            assert len(calls) == 1
        else:
            assert calls[-1][0] == (
                "mark_paid_call_unknown" if outcome == "transport" else "release_paid_call"
            )
    request = calls[0][1]
    assert request.provider == "openai"
    assert request.estimated_cost_usd == pytest.approx(0.006)
    assert request.ctx.creator_id == ctx.creator_id


def test_library_transcript_cache_tracks_owner_and_backend_model(audio, monkeypatch):
    from app import storage

    store, calls = {}, []
    monkeypatch.setattr(storage, "object_exists", lambda path: path in store)
    monkeypatch.setattr(
        storage, "download_to_file", lambda path, local: open(local, "wb").write(store[path])
    )
    monkeypatch.setattr(
        storage, "upload_bytes_public_read", lambda raw, path, **kw: store.__setitem__(path, raw)
    )
    monkeypatch.setattr(
        tr, "transcribe_whisper", lambda path, **kw: calls.append(kw) or tr.Transcript()
    )
    monkeypatch.setattr(tr.settings, "smart_caption_transcript_cache_enabled", True)
    monkeypatch.setattr(tr.settings, "whisper_backend", "openai-api")
    owner = RunContext(creator_id="creator-a", request_id="one")
    tr.transcribe_whisper_cached(audio, run_context=owner)
    tr.transcribe_whisper_cached(audio, run_context=owner)
    assert len(calls) == 1
    monkeypatch.setattr(tr.settings, "whisper_backend", "local")
    tr.transcribe_whisper_cached(audio, run_context=owner)
    tr.transcribe_whisper_cached(audio, run_context=RunContext(creator_id="creator-b"))
    assert len(calls) == 3
