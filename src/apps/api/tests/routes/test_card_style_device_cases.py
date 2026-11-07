"""KRI-1264: the server half of the card style device check (`scripts/ios/card-style-e2e.py`).

Not a regression test: a case builder, skipped unless ``KRIA_CARD_STYLE_E2E_OUT`` is set.

- ``prepare`` pins a phone Talking edit with one plain picture card on real stand-in media (a
  4 s grey take with a tone, a green 640x640 photo with a red corner marker), writes the editor
  projection the app loads (``editor.json``) and the plain case.
- ``save`` (once the app wrote ``save.json``, the ``media_overlays`` its Save sends) rebuilds the
  same edit and runs that payload through the REAL editor Save (`prepare_editor_commit`), then
  writes the pinned recipe as the styled case.

Each case dir gets ``recipe.json`` (as the device receives it) and ``assets.json`` (manifest
asset id -> real file, REAL sha256 / byte count so the device's fingerprint check passes).
"""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import subprocess
import uuid
from pathlib import Path
from types import SimpleNamespace

import pytest

from app.kria.device_render import make_device_request
from app.kria.media_sources import OriginalMediaDescriptor
from app.pipeline.phone_subtitled_lanes import PhoneSubtitledLanes, SubtitledOverlayCard
from app.pipeline.phone_subtitled_plan import compile_phone_subtitled_plan
from app.routes import generative_jobs as gj
from app.services.client_protocol import (
    CARD_STYLE_PROTOCOL,
    clear_request_context,
    set_client_protocol,
)
from app.services.device_render import device_status, pin_device_request
from app.services.phone_sources import (
    PHONE_SOURCES_FIELD,
    PHONE_VISUALS_FIELD,
    PhoneSourceBinding,
    PhoneVisualBinding,
)
from app.services.phone_subtitled_editor import project_phone_subtitled_editor_sections
from tests.routes.test_phone_subtitled_editor_commit import _CUES, _enable_subtitled_editor

OUT = os.environ.get("KRIA_CARD_STYLE_E2E_OUT")
pytestmark = pytest.mark.skipif(not OUT, reason="set KRIA_CARD_STYLE_E2E_OUT to write the cases")

API = Path(__file__).resolve().parents[2]
TAKE_S = 4.0
PHOTO_PATH = "users/owner/plan/item/pool/card-photo.png"


def _ffmpeg(*args: str) -> None:
    subprocess.run(["ffmpeg", "-hide_banner", "-loglevel", "error", "-y", *args], check=True)


def _sha(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _media(out: Path) -> tuple[Path, Path]:
    media = out / "media"
    media.mkdir(parents=True, exist_ok=True)
    take, photo = media / "take.mp4", media / "card.png"
    if not take.exists():
        _ffmpeg(
            "-f", "lavfi", "-i", f"color=c=0x303030:s=1080x1920:r=30:d={TAKE_S}",
            "-f", "lavfi", "-i", f"sine=frequency=220:duration={TAKE_S}",
            "-c:v", "libx264", "-pix_fmt", "yuv420p", "-c:a", "aac", "-shortest", str(take),
        )  # fmt: skip
    if not photo.exists():
        _ffmpeg(
            "-f", "lavfi", "-i", "color=c=0x00CC00:s=640x640",
            "-f", "lavfi", "-i", "color=c=0xFF0000:s=96x96",
            "-filter_complex", "[0][1]overlay=x=480:y=64", "-frames:v", "1", str(photo),
        )  # fmt: skip
    return take, photo


def _job(monkeypatch, take: Path, photo: Path):
    _enable_subtitled_editor(monkeypatch)
    binding = PhoneSourceBinding(
        media_id="clip-0",
        proxy_path="user/analysis-proxy-clip-0.mp4",
        generation="1",
        original=OriginalMediaDescriptor(
            sha256=_sha(take),
            byte_count=take.stat().st_size,
            duration_s=TAKE_S,
            width=1080,
            height=1920,
            orientation_degrees=0,
            has_audio=True,
        ),
    )
    visual = PhoneVisualBinding(
        media_id="photo-1",
        gcs_path=PHOTO_PATH,
        generation="77",
        sha256=_sha(photo),
        byte_count=photo.stat().st_size,
    )
    card = SubtitledOverlayCard(
        id="card-1",
        media_id="photo-1",
        gcs_path=PHOTO_PATH,
        generation="77",
        start_s=1.0,
        end_s=3.0,
    )
    recipe = compile_phone_subtitled_plan(
        (binding,),
        caption_cues=_CUES,
        visuals=(visual,),
        lanes=PhoneSubtitledLanes(overlays=[card]),
    )
    job = SimpleNamespace(
        id=uuid.UUID(int=1264),
        user_id=uuid.UUID(int=1),
        status="awaiting_device",
        current_phase=None,
        assembly_plan={
            PHONE_SOURCES_FIELD: [binding.model_dump(mode="json")],
            PHONE_VISUALS_FIELD: [visual.model_dump(mode="json")],
            "variants": [
                {
                    "variant_id": "subtitled",
                    "resolved_archetype": "subtitled",
                    "render_destination": "device",
                    "render_status": "awaiting_device",
                    "render_generation_id": "first",
                    "duration_s": recipe.duration,
                    "caption_cues": _CUES,
                    "voiceover_caption_style": "sentence",
                    # A finished edit carries its rendered video; the card lane needs it.
                    "output_url": "https://storage.example/out.mp4",
                }
            ],
        },
    )
    pin_device_request(
        job,
        make_device_request(job_id=job.id, variant_id="subtitled", revision=1, recipe=recipe),
        base_generation="first",
    )
    return job


def _write_case(out: Path, name: str, job, take: Path, photo: Path) -> None:
    recipe = device_status(job, "subtitled").request.recipe
    directory = out / "cases" / name
    shutil.rmtree(directory, ignore_errors=True)  # a stale movie is never this run's evidence
    directory.mkdir(parents=True)
    (directory / "recipe.json").write_text(recipe.model_dump_json())
    assets: dict[str, str] = {}
    for asset in [*recipe.assets, *recipe.asset_manifest.assets]:
        if getattr(asset, "kind", None) == "original":
            assets[asset.id] = str(take)
        elif asset.id.startswith("visual-"):
            assets[asset.id] = str(photo)
        elif asset.id.startswith("font-"):
            font = API / "assets/fonts" / asset.id.removeprefix("font-")
            assert font.is_file(), font
            assets[asset.id] = str(font)
    missing = [a.id for a in recipe.asset_manifest.assets if a.id not in assets]
    assert not missing, f"assets.json cannot map {missing}"
    (directory / "assets.json").write_text(json.dumps(assets, indent=2))


def _open_style(monkeypatch) -> None:
    clear_request_context()
    monkeypatch.setattr(
        gj.settings,
        "phone_render_verified_features",
        [*gj.settings.phone_render_verified_features, "cardStyle"],
    )
    set_client_protocol(CARD_STYLE_PROTOCOL)


def test_prepare_the_plain_case_and_the_editor_projection(monkeypatch):
    out = Path(OUT)
    take, photo = _media(out)
    job = _job(monkeypatch, take, photo)
    _open_style(monkeypatch)
    variant = job.assembly_plan["variants"][0]
    capabilities = gj._editor_capabilities(job, variant)
    assert capabilities.get("media_overlay_style") is True, "the card style gate is closed"
    sections = project_phone_subtitled_editor_sections(job.assembly_plan, variant)
    editor = {
        "media_overlays": sections["media_overlays"],
        "editor_capabilities": {
            key: capabilities[key]
            for key in ("overlays", "media_overlays", "media_overlay_style", "visual_editor_style")
            if key in capabilities
        },
    }
    (out / "editor.json").write_text(json.dumps(editor, indent=2))
    _write_case(out, "plain", job, take, photo)
    clear_request_context()


def test_save_the_app_payload_and_write_the_styled_case(monkeypatch):
    out = Path(OUT)
    saved = out / "save.json"
    if not saved.exists():
        pytest.skip("run the app step first: it writes save.json")
    take, photo = _media(out)
    job = _job(monkeypatch, take, photo)
    _open_style(monkeypatch)
    media_overlays = json.loads(saved.read_text())["media_overlays"]
    gj.prepare_editor_commit(
        job,
        "subtitled",
        gj.EditorCommitRequest(base_generation="first", media_overlays=media_overlays),
        user_id="owner",
        plan_item_id="item",
    )
    clip = next(
        t
        for t in device_status(job, "subtitled").request.recipe.tracks
        if t.id == "subtitled-overlays"
    ).clips[0]
    assert clip.visual_placement.editor_style is not None, "the Save dropped the card style"
    _write_case(out, "zoom2_pop", job, take, photo)
    clear_request_context()
