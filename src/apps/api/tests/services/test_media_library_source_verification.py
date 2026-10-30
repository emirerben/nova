import hashlib
import io
import json
from pathlib import Path
from types import SimpleNamespace

import pytest
from fastapi import HTTPException
from PIL import Image

from app.services import media_library_ingest as ingest


def _video_payload(file_size: int) -> dict:
    return {
        "filename": "phone-proxy.mp4",
        "kind": "video",
        "content_type": "video/mp4",
        "file_size_bytes": file_size,
        "upload_contract": {
            "purpose": "analysis_proxy",
            "proxy": {
                "original": {
                    "kind": "video",
                    "sha256": "a" * 64,
                    "byte_count": 1234,
                    "duration_s": 10,
                    "width": 1920,
                    "height": 1080,
                    "orientation_degrees": 90,
                    "has_audio": True,
                },
                "duration_s": 10,
                "width": 640,
                "height": 360,
                "frame_rate": 15,
                "orientation_degrees": 0,
            },
        },
    }


def _video_probe(**changes) -> dict:
    video = {
        "codec_type": "video",
        "codec_name": "h264",
        "width": 640,
        "height": 360,
        "avg_frame_rate": "15/1",
        "tags": {},
        "side_data_list": [],
    }
    video.update(changes)
    return {
        "format": {"duration": "10"},
        "streams": [video, {"codec_type": "audio", "codec_name": "aac"}],
    }


def _install_probe(monkeypatch, body, probe):
    metadata = SimpleNamespace(generation="7", size=len(body))
    monkeypatch.setattr(ingest.storage, "object_metadata", lambda path: metadata)

    def download(path, destination, *, generation):
        Path(destination).write_bytes(body)

    monkeypatch.setattr(ingest.storage, "download_generation_to_file", download)
    completed = SimpleNamespace(stdout=json.dumps(probe))
    monkeypatch.setattr(ingest.subprocess, "run", lambda *args, **kwargs: completed)


# Value card: protects=uploaded-byte identity and proxy stream contract; fails_when=valid H264
# proxy bytes have a distinct full-byte hash and matching ffprobe facts; why_new=proves the
# real download/hash/ffprobe seam rather than mocking verified_source_metadata; seam=none;
# no_claim=does not prove database registration or analysis dispatch.
def test_valid_video_proxy_hashes_uploaded_bytes_and_checks_proxy_contract(monkeypatch):
    body = b"synthetic h264 proxy bytes"
    payload = _video_payload(len(body))
    _install_probe(monkeypatch, body, _video_probe())

    proof = ingest.verified_source_metadata(
        "users/u/media-library/proxy.mp4", "7", "video", payload
    )

    assert proof["uploaded_sha256"] == hashlib.sha256(body).hexdigest()
    assert proof["uploaded_sha256"] != payload["upload_contract"]["proxy"]["original"]["sha256"]
    assert proof["byte_count"] == len(body)
    assert proof["role"] == "analysis_proxy"
    assert proof["metadata"] == {"duration_s": 10.0, "has_audio": True, "width": 640, "height": 360}


@pytest.mark.parametrize(
    "probe_change",
    [
        {"codec_name": "hevc"},
        {"avg_frame_rate": "24/1"},
        {"side_data_list": [{"rotation": 90}]},
        {"tags": {"rotate": "90"}},
        {"_format_duration": "11"},
        {"_remove_audio": True},
    ],
    ids=["codec", "fps", "side-data-rotation", "tag-rotation", "duration", "audio"],
)
## Value card: protects=proxy codec/FPS/orientation/timing/audio invariants; fails_when=ffprobe
## reports any mismatched proxy fact; why_new=covers distinct video validation branches at the
## actual subprocess boundary; seam=none; no_claim=does not assess ffprobe itself.
def test_video_proxy_probe_mismatch_is_rejected(monkeypatch, probe_change):
    body = b"proxy"
    payload = _video_payload(len(body))
    probe_change = dict(probe_change)
    probe = _video_probe()
    if probe_change.pop("_format_duration", None):
        probe["format"]["duration"] = "11"
    if probe_change.pop("_remove_audio", False):
        probe["streams"] = [probe["streams"][0]]
    probe["streams"][0].update(probe_change)
    _install_probe(monkeypatch, body, probe)

    with pytest.raises(HTTPException) as error:
        ingest.verified_source_metadata("proxy.mp4", "7", "video", payload)

    assert error.value.status_code == 422


@pytest.mark.parametrize(
    "streams,duration,accepted",
    [
        ([{"codec_type": "video", "width": 640, "height": 360}], "10", False),
        ([{"codec_type": "audio"}, {"codec_type": "video"}], "10", False),
        ([{"codec_type": "audio"}], "0", False),
        ([{"codec_type": "audio"}], "nan", False),
        (
            [{"codec_type": "audio"}, {"codec_type": "video", "disposition": {"attached_pic": 1}}],
            "10",
            True,
        ),
    ],
    ids=[
        "audio-without-audio",
        "audio-with-video",
        "zero-duration",
        "non-finite-duration",
        "embedded-cover-art",
    ],
)
# Value card: protects=audio kind and finite positive duration constraints; fails_when=audio
# uploads contain video, lack audio, or report invalid duration; why_new=exercises audio-specific
# ffprobe shape checks absent from image and video coverage; seam=none; no_claim=does not validate
# audio decoding quality.
def test_audio_upload_requires_only_a_valid_audio_stream(monkeypatch, streams, duration, accepted):
    body = b"synthetic audio bytes"
    payload = {
        "filename": "voice.m4a",
        "kind": "audio",
        "content_type": "audio/mp4",
        "file_size_bytes": len(body),
        "upload_contract": {"purpose": "cloud_render_source", "proxy": None},
    }
    _install_probe(monkeypatch, body, {"format": {"duration": duration}, "streams": streams})

    if accepted:
        proof = ingest.verified_source_metadata("audio.m4a", "7", "audio", payload)
        assert proof["role"] == "original"
        assert proof["metadata"] == {"duration_s": 10.0, "has_audio": True}
        return

    with pytest.raises(HTTPException) as error:
        ingest.verified_source_metadata("audio.m4a", "7", "audio", payload)

    assert error.value.status_code == 422


# Value card: protects=immutability of the verified storage generation; fails_when=object
# metadata changes after bytes are downloaded and probed; why_new=proves the second metadata
# read rejects a TOCTOU mutation; seam=none; no_claim=does not cover concurrent cloud writes.
def test_generation_changed_during_verification_is_rejected(monkeypatch):
    body = b"stable bytes"
    payload = _video_payload(len(body))
    generations = iter(("7", "8"))
    monkeypatch.setattr(
        ingest.storage,
        "object_metadata",
        lambda path: SimpleNamespace(generation=next(generations), size=len(body)),
    )
    monkeypatch.setattr(
        ingest.storage,
        "download_generation_to_file",
        lambda path, destination, *, generation: Path(destination).write_bytes(body),
    )

    with pytest.raises(HTTPException) as error:
        ingest.verified_source_metadata("proxy.mp4", "7", "video", payload)

    assert error.value.status_code == 422


@pytest.mark.parametrize(
    "changes",
    [
        {"content_type": "video/webm"},
        {"file_size_bytes": 0},
        {"file_size_bytes": 512 * 1024**2 + 1},
        {"kind": "video", "upload_contract": {"purpose": "cloud_render_source", "proxy": None}},
    ],
    ids=["unsupported-mime", "empty", "oversize", "missing-proxy-contract"],
)
# Value card: protects=per-kind size/MIME and proxy-purpose admission constraints; fails_when=
# payload metadata is unsupported, empty, oversized, or lacks the required video proxy binding;
# why_new=keeps cheap request-level guards explicit before storage access; seam=none;
# no_claim=does not prove cloud object metadata validation.
def test_upload_size_mime_and_contract_constraints_are_rejected(monkeypatch, changes):
    body = b"proxy"
    payload = _video_payload(len(body)) | changes

    with pytest.raises(HTTPException) as error:
        ingest.verified_source_metadata("proxy.mp4", "7", "video", payload)

    assert error.value.status_code == 422


def _hdr_jpeg_bytes() -> bytes:
    """A JPEG shaped like an iPhone HDR photo: the photo plus a smaller gain-map frame (MPO)."""
    buffer = io.BytesIO()
    Image.new("RGB", (64, 48), (200, 120, 40)).save(
        buffer, "MPO", save_all=True, append_images=[Image.new("L", (16, 12), 128)]
    )
    return buffer.getvalue()


def _png_bytes() -> bytes:
    buffer = io.BytesIO()
    Image.new("RGB", (64, 48), (200, 120, 40)).save(buffer, "PNG")
    return buffer.getvalue()


def _image_payload(content_type: str, file_size: int) -> dict:
    return {
        "kind": "image",
        "content_type": content_type,
        "file_size_bytes": file_size,
        "upload_contract": {"purpose": "cloud_render_source", "proxy": None},
    }


def _install_image(monkeypatch, body: bytes) -> None:
    metadata = SimpleNamespace(generation="7", size=len(body))
    monkeypatch.setattr(ingest.storage, "object_metadata", lambda path: metadata)
    monkeypatch.setattr(
        ingest.storage,
        "download_generation_to_file",
        lambda path, destination, *, generation: Path(destination).write_bytes(body),
    )


@pytest.mark.parametrize("legacy", [True, False], ids=["plan-visual", "library-upload"])
# Value card: protects=iPhone HDR photos added as Visuals or library uploads; fails_when=a JPEG
# carrying a gain-map frame (Pillow reads it as MPO) is refused as "not a JPEG", which left the
# 2026-10-30 Leverkusen photos stuck "waiting to analyze" behind a 422; why_new=no image bytes
# reached this check with more than one frame; seam=none (real Pillow decode of real MPO bytes);
# no_claim=does not prove Gemini analysis or the phone render of the photo.
def test_hdr_iphone_jpeg_with_gain_map_is_accepted_as_jpeg(monkeypatch, legacy):
    body = _hdr_jpeg_bytes()
    _install_image(monkeypatch, body)

    proof = ingest.verified_source_metadata(
        "users/u/plan/i/pool/IMG_1019.jpeg",
        "7",
        "image",
        _image_payload("image/jpeg", len(body)),
        legacy=legacy,
    )

    assert proof["uploaded_sha256"] == hashlib.sha256(body).hexdigest()
    assert proof["role"] == "original"
    # The photo's own size, never the gain map's.
    assert (proof["metadata"]["width"], proof["metadata"]["height"]) == (64, 48)


@pytest.mark.parametrize(
    "body_factory,content_type",
    [(_hdr_jpeg_bytes, "image/png"), (_hdr_jpeg_bytes, "image/heic"), (_png_bytes, "image/jpeg")],
    ids=["hdr-jpeg-labelled-png", "hdr-jpeg-labelled-heic", "png-labelled-jpeg"],
)
# Value card: protects=uploaded bytes must match their declared media type; fails_when=accepting
# HDR JPEGs widens the check so a mislabelled file passes; why_new=negative control for the MPO
# admission above; seam=none; no_claim=does not cover formats Pillow cannot open.
def test_image_bytes_that_do_not_match_their_label_are_still_rejected(
    monkeypatch, body_factory, content_type
):
    body = body_factory()
    _install_image(monkeypatch, body)

    with pytest.raises(HTTPException) as error:
        ingest.verified_source_metadata(
            "photo", "7", "image", _image_payload(content_type, len(body)), legacy=True
        )

    assert error.value.status_code == 422
