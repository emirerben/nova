import hashlib
import json
import uuid
from pathlib import Path
from types import SimpleNamespace

import pytest

from app.kria.device_render import DeviceRenderStatus, make_device_request
from app.pipeline.phone_guided_plan import compile_phone_guided_plan
from app.routes import device_render as routes
from tests.pipeline.test_phone_guided_plan import fixture


@pytest.mark.parametrize("with_audio", [False, True])
def test_portable_export_requires_aac_even_for_silent_recipe(monkeypatch, with_audio):
    plan, bindings = fixture()
    recipe = compile_phone_guided_plan(plan, bindings)
    status = DeviceRenderStatus(
        phase="awaiting_device",
        request=make_device_request(
            job_id=uuid.uuid4(),
            variant_id="first",
            revision=1,
            recipe=recipe,
        ),
    )
    payload = b"export fixture"
    monkeypatch.setattr(
        routes.storage,
        "download_generation_to_file",
        lambda path, local, **kwargs: Path(local).write_bytes(payload),
    )
    streams = [
        {
            "codec_type": "video",
            "codec_name": "h264",
            "width": 1080,
            "height": 1920,
            "pix_fmt": "yuv420p",
            "avg_frame_rate": "30/1",
        }
    ]
    if with_audio:
        streams.append({"codec_type": "audio", "codec_name": "aac"})
    monkeypatch.setattr(
        routes.subprocess,
        "run",
        lambda *args, **kwargs: SimpleNamespace(
            stdout=json.dumps({"streams": streams, "format": {"duration": "3"}})
        ),
    )
    args = ("final.mp4", "123", len(payload), hashlib.sha256(payload).hexdigest(), status)
    if with_audio:
        routes._verify_export(*args)
    else:
        with pytest.raises(ValueError, match="audio format"):
            routes._verify_export(*args)
