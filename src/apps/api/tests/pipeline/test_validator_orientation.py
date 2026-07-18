import json
import types

from app.pipeline import validator


def _ffprobe_payload(width: int, height: int) -> str:
    return json.dumps(
        {
            "streams": [
                {
                    "codec_type": "video",
                    "codec_name": "h264",
                    "width": width,
                    "height": height,
                    "duration": "50.0",
                },
                {"codec_type": "audio", "codec_name": "aac"},
            ],
            "format": {"duration": "50.0"},
        }
    )


def _patch_probe(monkeypatch, *, width: int, height: int) -> None:
    monkeypatch.setattr(
        validator.subprocess,
        "run",
        lambda *_args, **_kwargs: types.SimpleNamespace(
            returncode=0,
            stdout=_ffprobe_payload(width, height),
            stderr="",
        ),
    )


def test_validate_output_default_portrait_unchanged(monkeypatch) -> None:
    _patch_probe(monkeypatch, width=1080, height=1920)

    result = validator.validate_output("/tmp/out.mp4")

    assert result.passed is True
    assert result.errors == []


def test_validate_output_accepts_landscape_when_expected(monkeypatch) -> None:
    _patch_probe(monkeypatch, width=1920, height=1080)

    result = validator.validate_output("/tmp/out.mp4", expected_resolution=(1920, 1080))

    assert result.passed is True
    assert result.errors == []


def test_validate_output_rejects_mismatch_against_expected_resolution(monkeypatch) -> None:
    _patch_probe(monkeypatch, width=1080, height=1920)

    result = validator.validate_output("/tmp/out.mp4", expected_resolution=(1920, 1080))

    assert result.passed is False
    assert result.errors[0] == "Resolution mismatch: expected 1920×1080, got 1080×1920"
