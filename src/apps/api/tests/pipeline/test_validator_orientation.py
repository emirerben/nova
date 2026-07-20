import json
import types

from app.pipeline import validator


def _ffprobe_payload(width: int, height: int, *, duration_s: float = 50.0) -> str:
    return json.dumps(
        {
            "streams": [
                {
                    "codec_type": "video",
                    "codec_name": "h264",
                    "width": width,
                    "height": height,
                    "duration": str(duration_s),
                },
                {"codec_type": "audio", "codec_name": "aac"},
            ],
            "format": {"duration": str(duration_s)},
        }
    )


def _patch_probe(monkeypatch, *, width: int, height: int, duration_s: float = 50.0) -> None:
    monkeypatch.setattr(
        validator.subprocess,
        "run",
        lambda *_args, **_kwargs: types.SimpleNamespace(
            returncode=0,
            stdout=_ffprobe_payload(width, height, duration_s=duration_s),
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


def test_validate_output_accepts_short_generative_duration_range(monkeypatch) -> None:
    """Landscape generative edits are valid below the template-only 45s floor."""
    _patch_probe(monkeypatch, width=1920, height=1080, duration_s=17.0)

    result = validator.validate_output(
        "/tmp/out.mp4",
        expected_resolution=(1920, 1080),
        expected_duration_range=(0.1, 59.0),
    )

    assert result.passed is True
    assert result.errors == []


def test_validate_output_default_still_rejects_short_template_output(monkeypatch) -> None:
    _patch_probe(monkeypatch, width=1080, height=1920, duration_s=17.0)

    result = validator.validate_output("/tmp/out.mp4")

    assert result.passed is False
    assert result.errors == ["Duration 17.0s outside spec [45.0-59.0]"]


def test_validate_output_generative_range_keeps_sub_sixty_ceiling(monkeypatch) -> None:
    _patch_probe(monkeypatch, width=1920, height=1080, duration_s=60.0)

    result = validator.validate_output(
        "/tmp/out.mp4",
        expected_resolution=(1920, 1080),
        expected_duration_range=(0.1, 59.0),
    )

    assert result.passed is False
    assert result.errors == ["Duration 60.0s outside spec [0.1-59.0]"]
