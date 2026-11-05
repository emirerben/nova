"""Qendresa item 20: prepare the before/after device cases from REAL stand-in footage.

Not a regression test: a case builder for `scripts/ios/qendresa-title-look-e2e.py`. Skipped
unless `KRIA_TITLE_LOOK_E2E_OUT` (output dir) and `KRIA_TITLE_LOOK_E2E_FOOT` (footage dir with
`take.mp4`, `words.json`, `faces.json`, `visuals/logo.png`) are set.

It drives the REAL worker (`_run_generative_job` -> `_run_phone_subtitled_job` -> title rows ->
reaction beats -> compiler -> recipe validation) twice on the same footage with her Q2 title and
request:

- `q2_before`: `title_look_enabled` off (today: one block with " / " drawn literally, the logo
  beat goes to word grounding);
- `q2_after`: flag on (three stacked lines, the logo right above them).

Everything else is identical. Only transport is replaced: Whisper words (the footage's
whisper.cpp JSON), probe, DB, the Visuals pool (the logo only), its binding (REAL sha256 / byte
count so the device's fingerprint check passes) and face sampling (Apple Vision boxes from
`faces.json`, patched on the title module AND the overlay grounding module).

Each case dir gets `recipe.json`, `assets.json` (manifest asset id -> real file) and
`title.json` (the variant's title rows, the title placement receipt, the logo card and the
beat receipt).
"""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import subprocess
import uuid
from pathlib import Path

import pytest

import app.pipeline.phone_subtitled_title as title_mod
import app.services.phone_overlay_grounding as ogm
import app.services.phone_reaction_grounding as reaction_mod
import app.services.phone_visuals as phone_visuals_mod
from app.config import settings
from app.kria.media_sources import OriginalMediaDescriptor
from app.pipeline.phone_title_look import stamp_title_look
from app.pipeline.render_geometry import NormalizedBox, ProtectedRegion, _face_protection_box
from app.pipeline.transcribe import Word
from app.services import phone_text_styling as styling
from app.services.client_protocol import clear_request_context
from app.services.phone_rollout import validate_phone_pilot_recipe
from app.services.phone_sources import PHONE_SOURCES_FIELD, PhoneSourceBinding, PhoneVisualBinding
from app.services.phone_text_styling import PHONE_TEXT_STYLE_FIELD, stamp_phone_text_style
from app.tasks import generative_build as gb
from tests.tasks.test_kri841_talking_ending_clip import (
    _offline,  # noqa: F401 - autouse fixture the harness needs
    _recipe,
)
from tests.tasks.test_phone_subtitled_narrated_dispatch import _enable_beats, _setup_subtitled

OUT = os.environ.get("KRIA_TITLE_LOOK_E2E_OUT")
FOOT = os.environ.get("KRIA_TITLE_LOOK_E2E_FOOT")
CANVAS_RATIO = 1080.0 / 1920.0
OVERLAY_TRACK = "subtitled-overlays"
API = Path(__file__).resolve().parents[2]
_NS = uuid.UUID("00000000-0000-4000-8000-000000000120")

TITLE = "5 PROJECTS / the AI builder community / can't stop talking about!!!!"
REQUEST = (
    'Start with me full screen and the title "5 PROJECTS / the AI builder community / '
    "can't stop talking about!!!!\" with the GitHub logo above it. Captions word by word. "
    "Only my voice."
)
TITLE_ROW_FIELDS = (
    "id",
    "text",
    "font_family",
    "size_px",
    "color",
    "alignment",
    "x_frac",
    "y_frac",
    "start_s",
    "end_s",
)


def _probe(path: Path) -> dict:
    out = json.loads(
        subprocess.run(
            ["ffprobe", "-v", "error", "-show_streams", "-show_format", "-of", "json", str(path)],
            check=True,
            capture_output=True,
            text=True,
        ).stdout
    )
    video = next(s for s in out["streams"] if s["codec_type"] == "video")
    return {
        "duration": float(out["format"].get("duration") or 0.0),
        "width": int(video["width"]),
        "height": int(video["height"]),
        "has_audio": any(s["codec_type"] == "audio" for s in out["streams"]),
    }


def _sha(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1 << 20), b""):
            digest.update(block)
    return digest.hexdigest()


def load_words(path: Path) -> list[Word]:
    """whisper.cpp `-ml 1 -sow` JSON -> `Word`s: no-space tokens continue the previous word."""
    merged: list[list] = []  # [text, start_s, end_s]
    for token in json.loads(path.read_text())["transcription"]:
        raw = token["text"]
        text = raw.strip()
        if not text or (text.startswith("[") and text.endswith("]")):
            continue
        start, end = token["offsets"]["from"] / 1000, token["offsets"]["to"] / 1000
        if merged and not raw.startswith(" "):
            merged[-1][0] += text
            merged[-1][2] = max(merged[-1][2], end)
        else:
            merged.append([text, start, end])
    return [Word(text, start, max(end, start + 0.01), 1.0) for text, start, end in merged]


@pytest.mark.skipif(
    not (OUT and FOOT), reason="set KRIA_TITLE_LOOK_E2E_OUT and KRIA_TITLE_LOOK_E2E_FOOT"
)
def test_prepare_device_cases(monkeypatch):
    foot = Path(FOOT)
    out = Path(OUT)
    take = (foot / "take.mp4").resolve()
    logo = (foot / "visuals/logo.png").resolve()
    samples = json.loads((foot / "faces.json").read_text())
    info = _probe(take)
    assert (info["width"], info["height"]) == (1080, 1920), info
    words = load_words(foot / "words.json")
    assert words
    assert logo.is_file() and not logo.is_symlink(), logo
    logo_info = _probe(logo)

    # ---- the Visuals pool (the logo only), with REAL file fingerprints
    logo_id = str(uuid.uuid5(_NS, "github-logo"))
    gcs_path = f"users/u1/plan/item1/pool/{logo_id}.png"
    pool = [
        {
            "id": logo_id,
            "gcs_path": gcs_path,
            "gcs_generation": "7",
            "kind": "image",
            "source_filename": "github logo.png",
            "duration_s": None,
            "aspect": logo_info["width"] / logo_info["height"],
            "user_context": "",
            "analysis": {
                "subject": "GitHub logo",
                "on_screen_text": "",
                "width": logo_info["width"],
                "height": logo_info["height"],
            },
            "capture": None,
        }
    ]
    visual_binding = PhoneVisualBinding(
        media_id=logo_id,
        gcs_path=gcs_path,
        generation="7",
        sha256=_sha(logo),
        byte_count=logo.stat().st_size,
        kind="image",
    )

    def bind(_open, *, job_id, pins):  # noqa: ARG001
        return tuple(visual_binding for media_id in pins)

    # ---- face sampling: nearest Apple Vision sample (searching outwards for a non-null one)
    times = [s["t"] for s in samples]

    def face_at(at: float):
        order = sorted(range(len(samples)), key=lambda i: abs(times[i] - at))
        for i in order:
            if samples[i]["face"] is not None:
                x, y, w, h = samples[i]["face"]
                return _face_protection_box(NormalizedBox(x, y, x + w, y + h))
        return None

    def faces(_clip_path, anchors, **_kwargs):
        regions = []
        for at in anchors:
            box = face_at(at)
            if box is not None:
                regions.append(ProtectedRegion(max(0.0, at - 0.5), at + 0.5, box, kind="face"))
        return regions, {"attempted": len(anchors), "detected": len(regions)}

    real_binding = PhoneSourceBinding(
        media_id="c0",
        proxy_path="user/analysis-proxy-c0.mp4",
        generation="1",
        original=OriginalMediaDescriptor(
            sha256=_sha(take),
            byte_count=take.stat().st_size,
            duration_s=info["duration"],
            width=info["width"],
            height=info["height"],
            orientation_degrees=0,
            has_audio=info["has_audio"],
        ),
    )

    def build_job(*, after: bool):
        clear_request_context()
        job, snapshot, _session, _binding = _setup_subtitled(
            monkeypatch, transcript_words=words, duration_s=info["duration"]
        )
        row = [real_binding.model_dump(mode="json")]
        job.assembly_plan[PHONE_SOURCES_FIELD] = row
        snapshot[PHONE_SOURCES_FIELD] = row
        _enable_beats(monkeypatch, extra_features=("authoredText",))
        monkeypatch.setattr(gb.settings, "phone_subtitled_video_overlays_enabled", True)
        monkeypatch.setattr(settings, "title_look_enabled", after)
        monkeypatch.setattr(settings, "phone_text_styling_enabled", True)
        monkeypatch.setattr(settings, "phone_subtitled_title_enabled", True)
        monkeypatch.setattr(gb.settings, "subtitled_caption_correction_enabled", False)
        monkeypatch.setattr(phone_visuals_mod, "bind_phone_visual_assets", bind)
        monkeypatch.setattr(ogm, "_load_ready_pool_assets", lambda *a, **k: [dict(r) for r in pool])
        monkeypatch.setattr(
            reaction_mod, "_load_ready_pool_assets", lambda *a, **k: [dict(r) for r in pool]
        )
        # The sampler is looked up per call on each module: patch the title's and grounding's.
        monkeypatch.setattr(title_mod, "sample_face_regions", faces)
        monkeypatch.setattr(ogm, "sample_face_regions", faces)

        def environment(canvas):  # no proxy on disk: the title does its own face check
            styling._POLICY.get()["_face_boxes"] = []

        monkeypatch.setattr(styling, "_ensure_generation_environment", environment)
        job.all_candidates["creator_request"] = REQUEST
        job.all_candidates["voiceover_caption_style"] = "word"
        job.all_candidates["creator_strategy"] = {
            "edit_format": "subtitled",
            "audio_strategy": "original_audio",
            "opening_title": TITLE,
            "reaction_beats": [
                {
                    "beat_id": "logo",
                    "trigger": "GitHub logo",
                    "visual_id": f"asset-{logo_id}",
                    "anchor": "title",
                }
            ],
        }
        stamp_phone_text_style(job)
        stamp_title_look(job)  # as dispatch does (content_plan_build)
        job.assembly_plan[PHONE_TEXT_STYLE_FIELD]["selection_calls"] = 2  # no font model call
        return job

    for name, after in (("q2_before", False), ("q2_after", True)):
        job = build_job(after=after)
        gb._run_generative_job(str(job.id))
        assert job.status == "awaiting_device", (name, job.status)
        variant = job.assembly_plan["variants"][0]
        recipe = _recipe(job)
        validate_phone_pilot_recipe(recipe, allow_editor_media=True)  # as the worker does

        directory = out / "cases" / name
        shutil.rmtree(directory, ignore_errors=True)  # a stale movie is never this run's evidence
        directory.mkdir(parents=True)
        (directory / "recipe.json").write_text(recipe.model_dump_json())

        assets: dict[str, str] = {}
        for asset in recipe.asset_manifest.assets:
            if asset.kind == "original":
                assets[asset.id] = str(take)
            elif asset.id == f"visual-{logo_id}":
                assets[asset.id] = str(logo)
        for asset in [*recipe.assets, *recipe.asset_manifest.assets]:
            if asset.id.startswith("font-"):
                font = API / "assets/fonts" / asset.id.removeprefix("font-")
                assert font.is_file() and not font.is_symlink(), font
                assets[asset.id] = str(font)
        missing = [a.id for a in recipe.asset_manifest.assets if a.id not in assets]
        assert not missing, f"assets.json cannot map {missing}"
        (directory / "assets.json").write_text(json.dumps(assets, indent=2))

        title_rows = [
            {key: row.get(key) for key in TITLE_ROW_FIELDS}
            for row in variant["text_elements"]
            if (row.get("source_params") or {}).get("source") == title_mod.TALKING_TITLE_SOURCE
        ]
        track = next((t for t in recipe.tracks if t.id == OVERLAY_TRACK), None)
        logo_cards = []
        for clip in track.clips if track else []:
            place = clip.visual_placement
            width = place.width_fraction
            logo_cards.append(
                {
                    "asset": clip.source_asset_id,
                    "x": place.x_fraction,
                    "y": place.y_fraction,
                    "width": width,
                    "height": width * CANVAS_RATIO / pool[0]["aspect"],
                    "window_start": place.window_start,
                    "window_end": place.window_end,
                }
            )
        (directory / "title.json").write_text(
            json.dumps(
                {
                    "title_rows": title_rows,
                    "opening_title_placement": variant.get("opening_title_placement"),
                    "logo_cards": logo_cards,
                    "phone_subtitled_lanes": variant.get("phone_subtitled_lanes"),
                    "phone_beat_receipt": variant.get("phone_beat_receipt"),
                },
                indent=2,
                default=str,
            )
        )
        assert title_rows, f"{name}: no title rows"
