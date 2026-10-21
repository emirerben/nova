"""POST /dictation/transcribe (KRI-742).

Failure modes this guards, written before the route: flag off must 404; a non-audio upload,
an oversize file, an unreadable file and an over-long file must 422 without ever reaching
Whisper; silence (including Whisper echoing its own vocabulary prompt) must return empty text
with ``reason=no_speech`` instead of invented words (KRI-653); a slow Whisper must 503; the
temp file must be gone on every path; the creator's clip/song names must reach Whisper.
"""

from __future__ import annotations

import glob
import tempfile
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

from app.auth import get_current_user
from app.config import settings
from app.limiter import limiter
from app.main import app
from app.pipeline.transcribe import Transcript, Word

client = TestClient(app, raise_server_exceptions=False)


def _audio(size: int = 2048, content_type: str = "audio/mp4"):
    return {"file": ("dictation.m4a", b"\x00" * size, content_type)}


def _transcript(text: str, *, no_speech: float | None = 0.02) -> Transcript:
    words = [
        Word(
            text=w,
            start_s=i * 0.3,
            end_s=i * 0.3 + 0.25,
            confidence=1.0,
            segment_no_speech_prob=no_speech,
        )
        for i, w in enumerate(text.split())
    ]
    return Transcript(words=words, full_text=text, language="en")


@pytest.fixture(autouse=True)
def _signed_in(monkeypatch):
    app.dependency_overrides[get_current_user] = lambda: SimpleNamespace(id=1)
    monkeypatch.setattr(settings, "voice_dictation_enabled", True)
    monkeypatch.setattr("app.services.audio_download.probe_duration", lambda _p: 4.0)
    limiter.reset()  # every test posts; the 30/minute cap must not leak between them
    yield
    app.dependency_overrides.clear()


def _stub_whisper(monkeypatch, transcript: Transcript, calls: list | None = None):
    def fake(path, **kwargs):
        if calls is not None:
            calls.append({"path": path, **kwargs})
        return transcript

    monkeypatch.setattr("app.pipeline.transcribe.transcribe_whisper", fake)


def test_returns_whisper_text(monkeypatch) -> None:
    _stub_whisper(monkeypatch, _transcript("make the second clip shorter"))
    res = client.post("/dictation/transcribe", files=_audio())
    assert res.status_code == 200
    assert res.json() == {"text": "make the second clip shorter", "reason": None}


def test_flag_off_is_404(monkeypatch) -> None:
    monkeypatch.setattr(settings, "voice_dictation_enabled", False)
    assert client.post("/dictation/transcribe", files=_audio()).status_code == 404


def test_rejects_non_audio_without_calling_whisper(monkeypatch) -> None:
    calls: list = []
    _stub_whisper(monkeypatch, _transcript("x"), calls)
    res = client.post("/dictation/transcribe", files=_audio(content_type="image/png"))
    assert res.status_code == 422
    assert calls == []


def test_rejects_oversize_upload(monkeypatch) -> None:
    calls: list = []
    _stub_whisper(monkeypatch, _transcript("x"), calls)
    res = client.post("/dictation/transcribe", files=_audio(size=3 * 1024 * 1024 + 1))
    assert res.status_code == 422
    assert calls == []


def test_rejects_empty_upload() -> None:
    assert client.post("/dictation/transcribe", files=_audio(size=0)).status_code == 422


def test_rejects_unreadable_and_overlong_audio(monkeypatch) -> None:
    calls: list = []
    _stub_whisper(monkeypatch, _transcript("x"), calls)
    monkeypatch.setattr("app.services.audio_download.probe_duration", lambda _p: None)
    assert client.post("/dictation/transcribe", files=_audio()).status_code == 422
    monkeypatch.setattr("app.services.audio_download.probe_duration", lambda _p: 121.0)
    assert client.post("/dictation/transcribe", files=_audio()).status_code == 422
    assert calls == []


def test_silence_returns_no_speech_not_invented_words(monkeypatch) -> None:
    _stub_whisper(monkeypatch, _transcript("thanks for watching", no_speech=0.93))
    res = client.post("/dictation/transcribe", files=_audio())
    assert res.status_code == 200
    assert res.json() == {"text": "", "reason": "no_speech"}


def test_empty_transcript_is_no_speech(monkeypatch) -> None:
    _stub_whisper(monkeypatch, Transcript(words=[], full_text="", language=""))
    assert client.post("/dictation/transcribe", files=_audio()).json()["reason"] == "no_speech"


def test_whisper_echoing_the_vocabulary_prompt_is_no_speech(monkeypatch) -> None:
    _stub_whisper(monkeypatch, _transcript("Deniz, montage, lip-sync", no_speech=None))
    res = client.post(
        "/dictation/transcribe",
        files=_audio(),
        data={"context_terms": '["Deniz"]'},
    )
    assert res.json() == {"text": "", "reason": "no_speech"}


def test_a_short_spoken_word_that_is_in_the_vocabulary_is_not_silence(monkeypatch) -> None:
    for spoken in ("Captions.", "montage", "Bodrum"):
        _stub_whisper(monkeypatch, _transcript(spoken, no_speech=None))
        res = client.post(
            "/dictation/transcribe", files=_audio(), data={"context_terms": '["Bodrum"]'}
        )
        assert res.json()["text"] == spoken


def test_context_terms_reach_whisper_and_bad_terms_are_ignored(monkeypatch) -> None:
    calls: list = []
    _stub_whisper(monkeypatch, _transcript("add Bodrum clip first"), calls)
    res = client.post(
        "/dictation/transcribe",
        files=_audio(),
        data={"context_terms": '["Bodrum sunset", 7, "Bodrum sunset", ""]', "locale_hint": "TR-tr"},
    )
    assert res.status_code == 200
    prompt = calls[0]["verbatim_prompt"]
    # KRI-780: a bare word list (no English sentence); a Turkish locale adds Turkish words.
    assert prompt.startswith("Bodrum sunset, montage")
    assert "altyazı" in prompt
    assert prompt.count("Bodrum sunset") == 1
    assert calls[0]["language"] == "tr"

    calls.clear()
    client.post(
        "/dictation/transcribe",
        files=_audio(),
        data={"context_terms": "not json", "locale_hint": "turkish!!"},
    )
    assert calls[0]["verbatim_prompt"].startswith("montage")
    assert calls[0]["language"] is None

    # An English locale keeps Whisper's auto-detect: forcing "en" would translate a
    # Turkish speaker on an English-language phone.
    calls.clear()
    client.post(
        "/dictation/transcribe",
        files=_audio(),
        data={"context_terms": "[]", "locale_hint": "en-US"},
    )
    assert calls[0]["language"] is None
    assert "altyazı" not in calls[0]["verbatim_prompt"]


def test_long_utterance_is_not_truncated_to_400_chars(monkeypatch) -> None:
    text = " ".join(["word"] * 150)
    _stub_whisper(monkeypatch, _transcript(text))
    assert client.post("/dictation/transcribe", files=_audio()).json()["text"] == text


def test_whisper_timeout_is_503(monkeypatch) -> None:
    def slow(_path, **_kwargs):
        raise TimeoutError

    monkeypatch.setattr("app.pipeline.transcribe.transcribe_whisper", slow)
    assert client.post("/dictation/transcribe", files=_audio()).status_code == 503


def test_whisper_failure_is_422(monkeypatch) -> None:
    def boom(_path, **_kwargs):
        raise RuntimeError("openai down")

    monkeypatch.setattr("app.pipeline.transcribe.transcribe_whisper", boom)
    assert client.post("/dictation/transcribe", files=_audio()).status_code == 422


def test_temp_file_is_deleted_on_every_path(monkeypatch) -> None:
    before = set(glob.glob(f"{tempfile.gettempdir()}/tmp*.m4a"))
    _stub_whisper(monkeypatch, _transcript("hello there"))
    client.post("/dictation/transcribe", files=_audio())

    def boom(_path, **_kwargs):
        raise RuntimeError("x")

    monkeypatch.setattr("app.pipeline.transcribe.transcribe_whisper", boom)
    client.post("/dictation/transcribe", files=_audio())
    client.post("/dictation/transcribe", files=_audio(content_type="image/png"))
    client.post("/dictation/transcribe", files=_audio(size=3 * 1024 * 1024 + 1))
    assert set(glob.glob(f"{tempfile.gettempdir()}/tmp*.m4a")) == before


def test_only_a_top_ranked_kria_language_is_forced(monkeypatch) -> None:
    """KRI-780 review: forcing a device language makes Whisper translate speech in another
    one, and codes Whisper does not know ("nb") fail the call. Only a top-ranked Turkish
    locale forces; a later Turkish entry only adds the Turkish word list."""

    calls: list = []
    _stub_whisper(monkeypatch, _transcript("add captions and remove the music"), calls)
    cases = {
        "de-DE": (None, False),
        "nb-NO": (None, False),
        "az-AZ": (None, False),
        "de-DE,tr-TR": (None, True),
        "en-TR,tr-TR": (None, True),
        "tr-TR,en-US": ("tr", True),
    }
    for hint, (language, turkish_words) in cases.items():
        calls.clear()
        res = client.post("/dictation/transcribe", files=_audio(), data={"locale_hint": hint})
        assert res.status_code == 200, hint
        assert calls[0]["language"] == language, hint
        assert ("altyazı" in calls[0]["verbatim_prompt"]) is turkish_words, hint


def test_turkish_credit_filler_after_the_request_is_dropped(monkeypatch) -> None:
    for heard, kept in (
        ("klipleri sırala Altyazı M.K.", "klipleri sırala"),
        ("müziği kaldır. İzlediğiniz için teşekkür ederim.", "müziği kaldır."),
    ):
        _stub_whisper(monkeypatch, _transcript(heard))
        res = client.post("/dictation/transcribe", files=_audio(), data={"locale_hint": "tr-TR"})
        assert res.json()["text"] == kept

    _stub_whisper(monkeypatch, _transcript("Altyazı M.K."))
    res = client.post("/dictation/transcribe", files=_audio(), data={"locale_hint": "tr-TR"})
    assert res.json() == {"text": "", "reason": "no_speech"}


def test_all_caps_turkish_prompt_echo_is_no_speech(monkeypatch) -> None:
    _stub_whisper(monkeypatch, _transcript("ALTYAZI SESLENDİRME GEÇİŞ", no_speech=None))
    res = client.post("/dictation/transcribe", files=_audio(), data={"locale_hint": "tr-TR"})
    assert res.json() == {"text": "", "reason": "no_speech"}
