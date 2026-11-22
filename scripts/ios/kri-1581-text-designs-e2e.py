#!/usr/bin/env python3
"""KRI-1581: the Kyoto edit in designs A1, A2, A3 and B1, exported on the iOS simulator and
compared with the approved teardown mock-ups and the cloud Skia burn.

Three steps, run from the repo root (any worktree):

  1. prepare (real resolver + real phone compile, no provider calls):
     src/apps/api/.venv/bin/python scripts/ios/kri-1581-text-designs-e2e.py prepare OUT
  2. render (iOS simulator, production exporter, no watermark or outro, like the mock-ups):
     TEST_RUNNER_KRIA_INFORMATIVE_TEXT_E2E_DIR=OUT xcodebuild test -project ... -scheme Kria \
       -destination '...' \
       -only-testing:KriaTests/DeviceInformativeTextRenderE2ETests/testRendersEveryPreparedDesign
  3. score (text boxes and scrim against the mock-up stills, montage):
     src/apps/api/.venv/bin/python scripts/ios/kri-1581-text-designs-e2e.py score OUT

Inputs come from the gitignored ``.cache/kri1585-kyoto/mockup/``: ``base.mp4`` (the textless
edit the mock-ups were drawn over), ``variants/*.json`` (the generator's rows) and
``site/*_still*.jpg`` (the published stills). The rows are the generator's own copy and
windows (``tests/fixtures/kri1585_kyoto/designed_rows.json``), resolved under the job's real
text policy with the design stamp. prepare writes ``cases/<design>/{recipe.json,assets.json}``,
a ``cases/none`` recipe with no text (the phone's own base), and the Skia burn of every still
time (``skia/<design>_still<i>.png``).

score reads one frame per mock-up still from ``cases/<design>/phone.mp4``, at the still's own
time. Text is the pixels at least 60 luminance levels brighter than the base frame (the mock-up
and Skia against ``base.mp4``, the phone against its own ``cases/none/phone.mp4``, so encoder
and frame timing cancel out). The scrim is the luminance ratio against the base in the band
y 0.80-0.86, under the text and above the footage's letterbox. It asserts, at 1080x1920:

* each text box's left, top and bottom edge within 8 px of the mock-up's, the right edge
  within 8 px for Skia and 20 px for the phone;
* under a scrim the band's ratio within 0.06 of the mock-up's (0.40), elsewhere of 1.00;
* the soft shadow: the luminance ratio in a 2-7 px ring around the glyphs (where the Skia text
  is) within 0.04 of the mock-up's on every still outside a scrim, and the medians within 0.02;
* the same for the Skia burn, so the phone and the cloud agree with each other too.

The phone's right-edge allowance: the device shapes these runs itself, and in Literata,
Instrument Serif and Inter Bold its lines run up to about 2% wider than Skia's from the same
left edge. ``cases/control`` is A1's hook without this change's keys (the standard shadow);
score reports its width against the same Skia hook, showing the gap is the device's own.

It writes ``montage.png`` (one row per still: mock-up | iPhone export | Skia) and
``result.json``. Limits: the footage is the 320x568 analysis proxy scaled up (the mock-ups' own
base), JPEG stills at half scale, and text measured by brightness, so text over a bright area
can lose a few pixels.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import subprocess
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
API = REPO / "src/apps/api"
MOCKUP = REPO / ".cache/kri1585-kyoto/mockup"
DESIGNS = ("A1", "A2", "A3", "B1")
TEST_ENV = {
    "STORAGE_BUCKET": "nova-test",
    "STORAGE_PROVIDER": "gcs",
    "OPENAI_API_KEY": "sk-test",
    "TOKEN_ENCRYPTION_KEY": "x",
    "DATABASE_URL": "postgresql://localhost:5432/nova_test",
    "ALLOWED_ORIGINS": '["http://localhost:3000"]',
}
BOX_SLACK_PX = 8
HALO_SLACK = 0.04
HALO_MEDIAN_SLACK = 0.02
PHONE_RIGHT_SLACK_PX = 20
SCRIM_SLACK = 0.06


def _sha256(path: Path) -> tuple[str, int]:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1 << 20), b""):
            digest.update(chunk)
    return digest.hexdigest(), path.stat().st_size


def _still_times(design: str) -> list[float]:
    rows = json.loads((MOCKUP / f"variants/{design.lower()}.json").read_text())["rows"]
    return [round((row["start_s"] + row["end_s"]) / 2, 2) for row in rows]


def _frame(video: Path, t: float, out: Path, *, width: int = 540) -> Path:
    subprocess.run(
        [
            "ffmpeg",
            "-v",
            "error",
            "-y",
            "-ss",
            f"{t:.2f}",
            "-i",
            str(video),
            "-frames:v",
            "1",
            "-vf",
            f"scale={width}:-1",
            str(out),
        ],
        check=True,
    )
    return out


# ---------------------------------------------------------------------------- prepare


def prepare(out: Path) -> int:
    for key, value in TEST_ENV.items():
        os.environ.setdefault(key, value)
    sys.path.insert(0, str(API))
    os.chdir(API)
    import skia
    from PIL import Image

    from app.agents._schemas.text_element import TextElement
    from app.config import settings
    from app.kria.recipes import (
        AssetFingerprint,
        Canvas,
        MediaAsset,
        MediaSize,
        TimelineClip,
        TimelineTrack,
    )
    from app.kria.recipes_v2 import EditRecipeV2
    from app.kria.render_assets import (
        OriginalRenderAsset,
        RenderAssetManifest,
        RenderFingerprint,
    )
    from app.pipeline import text_overlay_skia as cloud
    from app.pipeline.canvas import PORTRAIT
    from app.pipeline.generative_overlays import build_overlays_from_text_elements
    from app.pipeline.phone_text_style import resolve_phone_text_style
    from app.pipeline.portable_text_layout import compile_text_overlay

    base = MOCKUP / "base.mp4"
    if not base.exists():
        print(
            f"missing {base}; copy the teardown generator's base.mp4 there",
            file=sys.stderr,
        )
        return 2
    # The phone gate the device check will open: flag on and `textDesigns` verified.
    settings.informative_text_designs_enabled = True
    settings.phone_render_verified_features = [
        *settings.phone_render_verified_features,
        "textDesigns",
    ]
    fixture = API / "tests/fixtures/kri1585_kyoto"
    policy = json.loads((fixture / "plan.json").read_text())["variant"]["phone_text_style"]
    designed = json.loads((fixture / "designed_rows.json").read_text())["designs"]
    base_sha, base_bytes = _sha256(base)
    fonts_dir = API / "assets/fonts"
    (out / "skia").mkdir(parents=True, exist_ok=True)
    (out / "base").mkdir(parents=True, exist_ok=True)

    def make_recipe(layers, manifest, assets):
        fingerprint = AssetFingerprint(hex=base_sha, byte_count=base_bytes)
        return EditRecipeV2(
            canvas=Canvas(width=1080, height=1920),
            frame_rate=30,
            assets=[
                MediaAsset(
                    id="base",
                    relative_path="base",
                    fingerprint=fingerprint,
                    duration=60,
                    natural_size=MediaSize(width=1080, height=1920),
                    frame_rate=30,
                ),
                *assets.values(),
            ],
            tracks=[
                TimelineTrack(
                    id="main",
                    kind="video",
                    clips=[
                        TimelineClip(
                            id="clip-0",
                            source_asset_id="base",
                            source_start=0,
                            source_duration=60,
                            timeline_start=0,
                            rate=1,
                            volume=0,
                        )
                    ],
                )
            ],
            required_capabilities={
                "basicComposition",
                "positionedText",
                "local1080Export",
            },
            asset_manifest=RenderAssetManifest(
                version=1,
                assets=[
                    OriginalRenderAsset(
                        id="base",
                        media_id="base",
                        fingerprint=RenderFingerprint(sha256=base_sha, byte_count=base_bytes),
                    ),
                    *manifest.values(),
                ],
            ),
            text_layers=layers,
        )

    summary = {}
    # The phone's own textless export: the base its frames are measured against.
    none = out / "cases" / "none"
    none.mkdir(parents=True, exist_ok=True)
    (none / "recipe.json").write_text(
        json.dumps(make_recipe([], {}, {}).model_dump(mode="json"), indent=1)
    )
    (none / "assets.json").write_text(json.dumps({"base": str(base)}, indent=1))
    for design in DESIGNS:
        rows = [TextElement.model_validate(r).model_dump(mode="json") for r in designed[design]]
        rows = resolve_phone_text_style(
            rows,
            preferences=policy["preferences"],
            requested=policy["requested"],
            selected_fonts=policy["selected_fonts"],
            informative=True,
            shapes=True,
        )
        overlays = build_overlays_from_text_elements(
            [TextElement.model_validate(row) for row in rows],
            video_duration_s=60,
            independent_box_alignment=True,
        )
        layers, manifest, assets, paths = [], {}, {}, {"base": str(base)}
        for index, overlay in enumerate(overlays):
            layer, font = compile_text_overlay(overlay, layer_id=f"text-{index}", canvas=PORTRAIT)
            layers.append(layer)
            if font is not None and font.id not in manifest:
                manifest[font.id] = font
                assets[font.id] = MediaAsset(
                    id=font.id,
                    relative_path=font.id,
                    fingerprint=AssetFingerprint(
                        hex=font.fingerprint.sha256,
                        byte_count=font.fingerprint.byte_count,
                    ),
                )
                paths[font.id] = str(fonts_dir / font.catalog_id)
        recipe = make_recipe(layers, manifest, assets)
        case = out / "cases" / design
        case.mkdir(parents=True, exist_ok=True)
        (case / "recipe.json").write_text(json.dumps(recipe.model_dump(mode="json"), indent=1))
        (case / "assets.json").write_text(json.dumps(paths, indent=1))
        (case / "rows.json").write_text(json.dumps(rows, ensure_ascii=False, indent=1))
        # The cloud burn of the same rows at every still time, over the same base frame.
        for still, t in enumerate(_still_times(design)):
            frame = _frame(base, t, out / "base" / f"{design}_still{still}.png", width=1080)
            surface = skia.Surfaces.MakeRasterN32Premul(1080, 1920)
            for overlay in overlays:
                if overlay["start_s"] <= t < overlay["end_s"]:
                    cloud._draw_overlay_on_canvas(
                        surface.getCanvas(),
                        overlay,
                        t - overlay["start_s"],
                        overlay["end_s"] - overlay["start_s"],
                    )
            text = Image.fromarray(
                surface.makeImageSnapshot().toarray(
                    colorType=skia.ColorType.kRGBA_8888_ColorType,
                    alphaType=skia.AlphaType.kUnpremul_AlphaType,
                )
            )
            composed = Image.open(frame).convert("RGBA")
            composed.alpha_composite(text)
            composed.convert("RGB").resize((540, 960), Image.LANCZOS).save(
                out / "skia" / f"{design}_still{still}.png"
            )
        summary[design] = {
            "layers": len(layers),
            "scrims": sum(layer.scrim is not None for layer in layers),
            "capabilities": sorted(recipe.required_capabilities),
            "fonts": sorted({Path(p).name for k, p in paths.items() if k != "base"}),
        }
    # Control: A1's hook with the KRI-1581 keys taken out (standard shadow), to show the phone's
    # line widths are the device's own shaping, not something this change draws.
    hook = {
        key: value
        for key, value in json.loads((out / "cases" / "A1" / "rows.json").read_text())[0].items()
        if key not in ("shadow_blur", "shadow_offset_y", "scrim")
    }
    (overlay,) = build_overlays_from_text_elements(
        [TextElement.model_validate(hook)], video_duration_s=60, independent_box_alignment=True
    )
    layer, font = compile_text_overlay(overlay, layer_id="text-0", canvas=PORTRAIT)
    font_asset = MediaAsset(
        id=font.id,
        relative_path=font.id,
        fingerprint=AssetFingerprint(
            hex=font.fingerprint.sha256, byte_count=font.fingerprint.byte_count
        ),
    )
    control = out / "cases" / "control"
    control.mkdir(parents=True, exist_ok=True)
    (control / "recipe.json").write_text(
        json.dumps(
            make_recipe([layer], {font.id: font}, {font.id: font_asset}).model_dump(mode="json"),
            indent=1,
        )
    )
    (control / "assets.json").write_text(
        json.dumps({"base": str(base), font.id: str(fonts_dir / font.catalog_id)}, indent=1)
    )
    (out / "prepare.json").write_text(json.dumps(summary, indent=1))
    print(json.dumps(summary, indent=1))
    return 0


# ---------------------------------------------------------------------------- score


def _luma(image):
    import numpy as np

    pixels = np.asarray(image.convert("RGB")).astype(np.float64)
    return pixels @ np.array([0.299, 0.587, 0.114])


def _text_box(image, base) -> list[int] | None:
    import numpy as np

    mask = (_luma(image) - _luma(base)) > 60
    ys, xs = np.nonzero(mask)
    if len(xs) < 20:
        return None
    # Half-scale stills, reported at 1080x1920.
    return [
        int(xs.min()) * 2,
        int(ys.min()) * 2,
        int(xs.max()) * 2 + 1,
        int(ys.max()) * 2 + 1,
    ]


def _bottom_ratio(image, base) -> float:
    import numpy as np

    band = slice(int(960 * 0.80), int(960 * 0.86))
    shown, under = _luma(image)[band], _luma(base)[band]
    keep = under > 30
    return float(np.median(shown[keep] / under[keep])) if keep.any() else 1.0


def _halo_ratio(image, base, text_mask) -> float:
    """Median luminance ratio against the base in a 2-7 px ring around the text: the shadow."""
    import numpy as np
    from PIL import Image, ImageFilter

    mask = Image.fromarray((text_mask * 255).astype(np.uint8))
    outer = np.asarray(mask.filter(ImageFilter.MaxFilter(15))) > 0
    inner = np.asarray(mask.filter(ImageFilter.MaxFilter(3))) > 0
    ring = outer & ~inner
    shown, under = _luma(image)[ring], _luma(base)[ring]
    keep = under > 40
    return float(np.median(shown[keep] / under[keep]))


def score(out: Path) -> int:
    from PIL import Image, ImageDraw

    results, tiles, ok, halos = {}, [], True, {}
    for design in DESIGNS:
        video = out / "cases" / design / "phone.mp4"
        if not video.exists():
            print(f"missing {video}; run the render step", file=sys.stderr)
            return 2
        rows = json.loads((out / "cases" / design / "recipe.json").read_text())["text_layers"]
        scrim_windows = [(r["start"], r["end"]) for r in rows if r.get("scrim")]
        results[design] = []
        for still, t in enumerate(_still_times(design)):
            base = Image.open(out / "base" / f"{design}_still{still}.png").convert("RGB")
            base = base.resize((540, 960), Image.LANCZOS)
            mock = Image.open(MOCKUP / "site" / f"{design.lower()}_still{still}.jpg").convert("RGB")
            phone = Image.open(
                _frame(video, t, out / "cases" / design / f"phone_still{still}.png")
            ).convert("RGB")
            phone_base = Image.open(
                _frame(
                    out / "cases" / "none" / "phone.mp4",
                    t,
                    out / "cases" / "none" / f"{design}_still{still}.png",
                )
            ).convert("RGB")
            skia = Image.open(out / "skia" / f"{design}_still{still}.png").convert("RGB")
            mock_box = _text_box(mock, base)
            mock_ratio = _bottom_ratio(mock, base)
            entry = {
                "t": t,
                "mock_box": mock_box,
                "mock_bottom_ratio": round(mock_ratio, 3),
                "under_scrim": any(s <= t < e for s, e in scrim_windows),
            }
            target = mock_ratio if entry["under_scrim"] else 1.0
            text_mask = (_luma(skia) - _luma(base)) > 60
            if not entry["under_scrim"]:
                entry["mock_halo"] = round(_halo_ratio(mock, base, text_mask), 3)
            for name, image, under in (("phone", phone, phone_base), ("skia", skia, base)):
                box = _text_box(image, under)
                ratio = _bottom_ratio(image, under)
                if "mock_halo" in entry:
                    halo = _halo_ratio(image, under, text_mask)
                    halos.setdefault(name, []).append(halo)
                    if name == "phone":
                        halos.setdefault("mock", []).append(entry["mock_halo"])
                    ok &= abs(halo - entry["mock_halo"]) <= HALO_SLACK
                deltas = [b - m for b, m in zip(box, mock_box, strict=True)] if box else None
                right = PHONE_RIGHT_SLACK_PX if name == "phone" else BOX_SLACK_PX
                passed = (
                    deltas is not None
                    and all(abs(d) <= BOX_SLACK_PX for d in (deltas[0], deltas[3]))
                    and abs(deltas[1]) <= BOX_SLACK_PX
                    and abs(deltas[2]) <= right
                    and abs(ratio - target) <= SCRIM_SLACK
                )
                entry[name] = {
                    "box": box,
                    "delta": deltas,
                    "bottom_ratio": round(ratio, 3),
                    **({"halo": round(halos[name][-1], 3)} if "mock_halo" in entry else {}),
                }
                entry[name]["pass"] = passed
                ok &= passed
            results[design].append(entry)
            tile = Image.new("RGB", (540 * 3, 960 + 36), "white")
            for column, (label, image) in enumerate(
                (("mock-up", mock), ("iPhone export", phone), ("cloud Skia", skia))
            ):
                tile.paste(image, (column * 540, 36))
                ImageDraw.Draw(tile).text(
                    (column * 540 + 10, 10),
                    f"{design} still {still} t={t}s  {label}",
                    fill="black",
                )
            tiles.append(tile.resize((810, 498)))
    medians = {name: round(float(sorted(v)[len(v) // 2]), 3) for name, v in halos.items()}
    results["halo_median"] = medians
    ok &= all(
        abs(medians[name] - medians["mock"]) <= HALO_MEDIAN_SLACK for name in ("phone", "skia")
    )
    t = _still_times("A1")[0]
    control = Image.open(
        _frame(out / "cases" / "control" / "phone.mp4", t, out / "cases" / "control" / "still0.png")
    ).convert("RGB")
    none = Image.open(out / "cases" / "none" / "A1_still0.png").convert("RGB")
    designed = results["A1"][0]["phone"]["box"]
    control_box = _text_box(control, none)
    results["control"] = {
        "what": "A1 hook without shadow_blur, shadow_offset_y or scrim, exported on the phone",
        "box": control_box,
        "designed_box": designed,
        "skia_box": results["A1"][0]["skia"]["box"],
        "same_width_as_designed": bool(
            control_box and designed and abs(control_box[2] - designed[2]) <= 2
        ),
    }
    montage = Image.new("RGB", (810 * 2, 498 * ((len(tiles) + 1) // 2)), "white")
    for index, tile in enumerate(tiles):
        montage.paste(tile, ((index % 2) * 810, (index // 2) * 498))
    montage.save(out / "montage.png")
    (out / "result.json").write_text(json.dumps({"pass": ok, "designs": results}, indent=1))
    print(json.dumps({"pass": ok, "montage": str(out / "montage.png")}, indent=1))
    return 0 if ok else 1


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n", 1)[0])
    parser.add_argument("step", choices=("prepare", "score"))
    parser.add_argument("out", type=Path)
    args = parser.parse_args()
    out = args.out.resolve()
    return prepare(out) if args.step == "prepare" else score(out)


if __name__ == "__main__":
    raise SystemExit(main())
