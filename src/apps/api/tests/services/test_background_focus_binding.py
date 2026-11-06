"""Qendresa item 18: `bind_phone_visual_assets` measures where the text sits (flag on only).

The measure rides the in-memory binding (`text_column`, never dumped), so the persisted
`_phone_visuals_v1` receipts and every dict equality check on them stay byte-identical.
"""

from __future__ import annotations

import shutil
import uuid
from contextlib import contextmanager
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from app import storage
from app.config import settings
from app.services import background_text_focus as btf
from app.services import phone_visuals
from app.services.phone_visuals import bind_phone_visual_assets
from tests.services.test_background_text_focus import _encode, _page, _save

needs_ffmpeg = pytest.mark.skipif(shutil.which("ffmpeg") is None, reason="ffmpeg not on PATH")


def _bind(monkeypatch, tmp_path: Path, *, kind: str, local_file: str, measure_text: bool = True):
    """Bind one pool asset whose bytes are `local_file`; storage and DB are faked."""
    owner, item, media = (uuid.UUID(int=n) for n in (1, 2, 3))  # same ids every call
    path = f"users/{owner}/plan/{item}/pool/{media}.bin"
    row = SimpleNamespace(
        id=media,
        plan_item_id=item,
        user_id=owner,
        status="ready",
        kind=kind,
        gcs_path=path,
        gcs_generation="77",
    )
    db = Mock()
    db.get.return_value = SimpleNamespace(content_plan_item_id=item, user_id=owner)
    db.execute.return_value.scalars.return_value.all.return_value = [row]

    @contextmanager
    def session():
        yield db

    def download(_path, local, *, generation):
        shutil.copyfile(local_file, local)

    monkeypatch.setattr(storage, "download_generation_to_file", download)
    pins = {str(media): (kind, path, "77")}
    return bind_phone_visual_assets(
        session, job_id=str(uuid.uuid4()), pins=pins, measure_text=measure_text
    )[0]


def _image(tmp_path: Path) -> str:
    return _save(_page(1920, 1080, column=(0.18, 0.60)), tmp_path / "readme.png")


@pytest.fixture
def spy(monkeypatch):
    calls: list[dict] = []
    real = btf.measure_text_column

    def measure(path, **kwargs):
        calls.append(kwargs)
        return real(path, **kwargs)

    monkeypatch.setattr(phone_visuals, "measure_text_column", measure, raising=False)
    return calls


def test_flag_off_never_measures(monkeypatch, tmp_path):
    monkeypatch.setattr(settings, "background_focus_enabled", False)

    def boom(*_a, **_k):
        raise AssertionError("measure must not run with the flag off")

    monkeypatch.setattr(phone_visuals, "measure_text_column", boom, raising=False)
    binding = _bind(monkeypatch, tmp_path, kind="image", local_file=_image(tmp_path))
    assert binding.text_column is None


def test_image_is_measured_and_the_dump_is_unchanged(monkeypatch, tmp_path, spy):
    source = _image(tmp_path)
    monkeypatch.setattr(settings, "background_focus_enabled", False)
    off = _bind(monkeypatch, tmp_path, kind="image", local_file=source)
    monkeypatch.setattr(settings, "background_focus_enabled", True)
    on = _bind(monkeypatch, tmp_path, kind="image", local_file=source)
    assert len(spy) == 1 and spy[0]["kind"] == "image"
    assert on.text_column is not None
    assert on.text_column.left == pytest.approx(0.18, abs=0.03)
    assert (on.text_column.source_width, on.text_column.source_height) == (1920, 1080)
    # the persisted receipt never carries it, in either dump mode
    assert on.model_dump(mode="json") == off.model_dump(mode="json")
    assert on.model_dump() == off.model_dump()
    assert "text_column" not in on.model_dump_json()
    # measured and unmeasured bindings of the same file are the same identity
    assert on == off
    assert hash(on) == hash(off)
    assert {on, off} == {off}


@needs_ffmpeg
def test_video_is_measured_with_its_upright_size(monkeypatch, tmp_path, spy):
    fdir = tmp_path / "frames"
    fdir.mkdir()
    page = _page(1280, 720, column=(0.18, 0.60), pitch=18, font_size=11, sidebar=None, seed=5)
    for i in range(6):
        page.save(fdir / f"f{i + 1:03d}.png")
    video = _encode(fdir, 2, tmp_path / "readme.mp4")
    monkeypatch.setattr(settings, "background_focus_enabled", True)
    binding = _bind(monkeypatch, tmp_path, kind="video", local_file=video)
    assert spy == [{"kind": "video", "width": 1280, "height": 720}]
    assert binding.text_column is not None
    assert (binding.text_column.source_width, binding.text_column.source_height) == (1280, 720)
    assert "text_column" not in binding.model_dump_json()


def test_a_rotated_video_is_measured_at_the_swapped_size(monkeypatch, tmp_path):
    seen: list[dict] = []
    monkeypatch.setattr(settings, "background_focus_enabled", True)
    monkeypatch.setattr(
        phone_visuals,
        "_probed_video",
        lambda _local: {
            "duration_s": 3.0,
            "width": 720,
            "height": 1280,
            "orientation_degrees": 90,
        },
    )
    monkeypatch.setattr(
        phone_visuals,
        "measure_text_column",
        lambda _path, **kwargs: seen.append(kwargs),
        raising=False,
    )
    source = tmp_path / "fake.mp4"
    source.write_bytes(b"\x00\x00\x00\x18ftypmp42" + b"\0" * 64)
    binding = _bind(monkeypatch, tmp_path, kind="video", local_file=str(source))
    assert seen == [{"kind": "video", "width": 1280, "height": 720}]
    assert binding.text_column is None


def test_a_failing_measure_never_fails_the_bind(monkeypatch, tmp_path):
    monkeypatch.setattr(settings, "background_focus_enabled", True)

    def boom(*_a, **_k):
        raise RuntimeError("measure exploded")

    monkeypatch.setattr(phone_visuals, "measure_text_column", boom, raising=False)
    binding = _bind(monkeypatch, tmp_path, kind="image", local_file=_image(tmp_path))
    assert binding.text_column is None
    assert binding.sha256


def test_a_bind_that_did_not_ask_for_the_measure_never_measures(monkeypatch, tmp_path):
    monkeypatch.setattr(settings, "background_focus_enabled", True)

    def boom(*_a, **_k):
        raise AssertionError("measure_text defaults to False")

    monkeypatch.setattr(phone_visuals, "measure_text_column", boom, raising=False)
    binding = _bind(
        monkeypatch, tmp_path, kind="image", local_file=_image(tmp_path), measure_text=False
    )
    assert binding.text_column is None


def test_a_soft_time_limit_in_the_measure_is_never_swallowed(monkeypatch, tmp_path):
    from celery.exceptions import SoftTimeLimitExceeded

    monkeypatch.setattr(settings, "background_focus_enabled", True)

    def limit(*_a, **_k):
        raise SoftTimeLimitExceeded()

    monkeypatch.setattr(phone_visuals, "measure_text_column", limit, raising=False)
    with pytest.raises(SoftTimeLimitExceeded):
        _bind(monkeypatch, tmp_path, kind="image", local_file=_image(tmp_path))
