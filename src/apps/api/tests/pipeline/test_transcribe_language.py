"""whisper-1 `language` hint plumbing (subtitled style, Turkish + English).

No network: the OpenAI client is faked so we only assert the `language` kwarg is
forwarded when the caller provides it and omitted otherwise (auto-detect). This is
the change that makes Turkish transcription reliable — whisper-1 auto-detect is
weak on short/accented clips, so callers pass an explicit ISO hint.
"""

from __future__ import annotations

import openai
import pytest

import app.pipeline.transcribe as tr


class _FakeResp:
    def __init__(self, language: str = "") -> None:
        self.words: list = []
        self.text = ""
        self.language = language  # whisper verbose_json reports the detected language


class _FakeTranscriptions:
    def __init__(self, sink: dict, resp_language: str = "") -> None:
        self._sink = sink
        self._resp_language = resp_language

    def create(self, **kwargs: object) -> _FakeResp:
        self._sink.clear()
        self._sink.update(kwargs)
        return _FakeResp(self._resp_language)


class _FakeClient:
    def __init__(self, sink: dict, resp_language: str = "") -> None:
        self.audio = type(
            "A", (), {"transcriptions": _FakeTranscriptions(sink, resp_language)}
        )()


@pytest.fixture()
def _audio(tmp_path):
    p = tmp_path / "a.wav"
    p.write_bytes(b"\x00")
    return str(p)


def test_openai_passes_language_when_provided(_audio, monkeypatch):
    sink: dict = {}
    monkeypatch.setattr(openai, "OpenAI", lambda **_k: _FakeClient(sink))
    tr._transcribe_openai(_audio, language="tr")
    assert sink.get("language") == "tr"
    assert sink["model"] == "whisper-1"


def test_openai_normalizes_language_case(_audio, monkeypatch):
    sink: dict = {}
    monkeypatch.setattr(openai, "OpenAI", lambda **_k: _FakeClient(sink))
    tr._transcribe_openai(_audio, language="  TR ")
    assert sink.get("language") == "tr"


def test_openai_captures_detected_language_on_autodetect(_audio, monkeypatch):
    # No hint passed → whisper auto-detects; the reported language ("turkish") is
    # normalized to ISO and carried on the Transcript so subtitled captions in it.
    monkeypatch.setattr(
        openai, "OpenAI", lambda **_k: _FakeClient({}, resp_language="turkish")
    )
    t = tr._transcribe_openai(_audio)  # language=None → auto-detect
    assert t.language == "tr"


def test_normalize_lang():
    assert tr._normalize_lang("english") == "en"
    assert tr._normalize_lang("turkish") == "tr"
    assert tr._normalize_lang("TR") == "tr"
    assert tr._normalize_lang("en") == "en"
    assert tr._normalize_lang("") == ""


def test_openai_omits_language_when_absent(_audio, monkeypatch):
    sink: dict = {}
    monkeypatch.setattr(openai, "OpenAI", lambda **_k: _FakeClient(sink))
    tr._transcribe_openai(_audio)
    assert "language" not in sink  # auto-detect — no forced hint


def test_openai_omits_language_when_blank(_audio, monkeypatch):
    sink: dict = {}
    monkeypatch.setattr(openai, "OpenAI", lambda **_k: _FakeClient(sink))
    tr._transcribe_openai(_audio, language="")
    assert "language" not in sink
