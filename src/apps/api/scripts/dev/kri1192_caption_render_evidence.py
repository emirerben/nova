"""KRI-1192 caption render evidence: the REAL caption writers burned by the REAL libass path.

For each caption language (the ten new ones plus English and Turkish controls) two FLEURS
sentences (CC BY 4.0, the KRI-1192 fixture; Serbian in whisper's Latin spelling, the product
decision) become cues and are written and burned exactly as production does it:

- ``talk_sentence``  Talk to camera, sentence look: ``build_plain_cues(attach_words=True)`` ->
  ``resplit_cues_into_sentences`` -> ``generate_ass_from_cues(margin_v=384, pop_in=True)``;
- ``talk_word_pop``  Talk to camera, word highlight: ``generate_word_pop_ass``;
- ``talk_smart``     Smart Captions (measured, wrapped by ``measure_caption``);
- ``voiceover``      Voiceover/Narrated sentence look (``_build_caption_overlay``);
- ``voiceover_word`` Voiceover/Narrated one-big-word look (``caption_style="word"``).

Each ASS is burned with ``narrated_assembler.burn_captions_on_video`` onto a 1080x1920 solid
clip with the local ffmpeg/libass, one frame per language/format is extracted, and a second
libass pass with ``-v trace`` (same ``caption_burn_filter``) records every font libass chose
and every "Glyph ... not found" fallback. Per language/format ``report.json`` holds: the ASS
face, the letters that face lacks (``font_coverage``; must be 0), the libass fallbacks (must
be 0), the ASS ``Encoding`` (-1 = auto base direction, set only for right-to-left text), the
ink box (must stay inside the frame) and, for the word highlight, the direction check (first
spoken word on the right for Arabic, on the left otherwise). It also records the phone
caption recipe for each language (font asset, shaped runs, right-to-left word order) and the
Skia overlay verify of ``tests/fixtures/overlay_verify/kri1192_caption_scripts.json``.

Usage (from src/apps/api; needs ffmpeg with libass; no network, no paid calls):

    PYTHONPATH=$PWD python scripts/dev/kri1192_caption_render_evidence.py \\
        --out ../../../.caption-verify

Writes ``montage.png`` (rows = languages, columns = formats) and ``report.json`` to --out.
Temporary videos are deleted; only the extracted PNG frames are kept (frames/).
"""

from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

API_DIR = Path(__file__).resolve().parents[2]
FIXTURE = API_DIR / "tests" / "fixtures" / "kri1192_caption_languages" / "fleurs_whisper_turbo.json"
SKIA_FIXTURE = API_DIR / "tests" / "fixtures" / "overlay_verify" / "kri1192_caption_scripts.json"
LANGUAGES = ("en", "tr", "pt", "es", "el", "de", "ko", "it", "fr", "ar", "da", "sr")
FORMATS = ("talk_sentence", "talk_word_pop", "talk_smart", "voiceover", "voiceover_word")
RTL = {"ar"}
PER_WORD_S = 0.35
SMART_POLICY = {
    "font_family": "TikTok Sans",
    "font_size_px": 78,
    "y_frac": 0.8,
    "width_frac": 0.84,
    "max_lines": 2,
    "color": "#FFFFFF",
    "stroke_color": "#000000",
    "stroke_width": 6,
}
LIME = (0x84, 0xCC, 0x16)


def _sentences(clips: list[dict], language: str) -> list[str]:
    key = "whisper_text" if language == "sr" else "reference"
    texts = [
        " ".join(str(clip[key]).replace('""', '"').strip().strip('"').split())
        for clip in clips
        if clip["language"] == language
    ]
    # Two sentences that end in punctuation where the fixture has them (direction evidence).
    ended = [t for t in texts if t and t[-1] in ".?!؟;"]
    return (ended + [t for t in texts if t not in ended])[:2]


def _words(texts: list[str]):
    from app.pipeline.transcribe import Word

    words, cursor = [], 0.2
    for text in texts:
        for token in text.split():
            words.append(Word(token, round(cursor, 3), round(cursor + PER_WORD_S * 0.9, 3), 0.95))
            cursor += PER_WORD_S
        cursor += 0.6
    return words


def _write_ass(fmt: str, words, tmp: Path) -> tuple[Path, list[dict]]:
    from app.pipeline.captions import (
        SUBTITLED_CAPTION_MARGIN_V,
        build_plain_cues,
        generate_ass_from_cues,
        generate_word_pop_ass,
        resplit_cues_into_sentences,
    )
    from app.pipeline.narrated_alignment import StepTiming
    from app.pipeline.narrated_assembler import _build_caption_overlay, resolve_caption_font
    from app.pipeline.transcribe import Transcript

    font = resolve_caption_font(None)
    path = tmp / f"{fmt}.ass"
    if fmt.startswith("voiceover"):
        end = words[-1].end_s + 0.5
        os.makedirs(tmp / fmt, exist_ok=True)
        paths, _fonts_dir, cues = _build_caption_overlay(
            Transcript(words=words),
            [StepTiming("s1", 0.0, end, 1.0)],
            end,
            str(tmp / fmt),
            caption_style="word" if fmt == "voiceover_word" else "sentence",
        )
        shutil.copy2(paths[0], path)
        return path, cues
    cues = resplit_cues_into_sentences(build_plain_cues(words, attach_words=True))
    if fmt == "talk_word_pop":
        generate_word_pop_ass(cues, str(path), font_name=font, margin_v=SUBTITLED_CAPTION_MARGIN_V)
    else:
        generate_ass_from_cues(
            cues,
            str(path),
            font_name=font,
            style="plain",
            margin_v=SUBTITLED_CAPTION_MARGIN_V,
            pop_in=True,
            **({"smart_policy": SMART_POLICY} if fmt == "talk_smart" else {}),
        )
    return path, cues


def _style_fields(ass: str) -> list[str]:
    line = next(line for line in ass.splitlines() if line.startswith("Style: Default,"))
    return line.removeprefix("Style: ").split(",")


def _faces(ass: str) -> list[str]:
    faces = {_style_fields(ass)[1]}
    for line in ass.splitlines():
        faces.update(m.group(1) for m in re.finditer(r"\\fn([^\\}]+)", line))
    return sorted(faces)


def _missing(face: str, text: str) -> str:
    from app.pipeline.font_coverage import _registry, missing_glyphs

    keys = [k for k, e in _registry()["fonts"].items() if e.get("ass_name") == face]
    if not keys:
        return f"<{face} is not a bundled face>"
    return "".join(sorted({ch for key in keys for ch in missing_glyphs(key, text)}))


def _ffmpeg(cmd: list[str], timeout: int = 600) -> str:
    result = subprocess.run(cmd, capture_output=True, timeout=timeout, check=False)
    if result.returncode != 0:
        raise RuntimeError(result.stderr.decode(errors="replace")[-600:])
    return result.stderr.decode(errors="replace")


def _base_clip(path: Path, duration: float) -> None:
    _ffmpeg(
        [
            "ffmpeg", "-hide_banner", "-loglevel", "error", "-y",
            "-f", "lavfi", "-i", f"color=c=0x2b2b2b:s=1080x1920:r=5:d={duration:.2f}",
            "-f", "lavfi", "-i", f"anullsrc=r=44100:cl=stereo:d={duration:.2f}",
            "-shortest", "-c:v", "libx264", "-preset", "ultrafast", "-pix_fmt", "yuv420p",
            "-c:a", "aac", str(path),
        ]
    )  # fmt: skip


def _frame(video: Path, at_s: float, png: Path) -> None:
    _ffmpeg(
        ["ffmpeg", "-hide_banner", "-loglevel", "error", "-y", "-ss", f"{at_s:.2f}",
         "-i", str(video), "-frames:v", "1", str(png)]
    )  # fmt: skip


def _libass_trace(ass: Path, duration: float, fonts_dir: str) -> dict:
    from app.pipeline.narrated_assembler import caption_burn_filter

    log = _ffmpeg(
        [
            "ffmpeg", "-hide_banner", "-v", "trace",
            "-f", "lavfi", "-i", f"color=c=black:s=1080x1920:r=4:d={duration:.2f}",
            "-vf", caption_burn_filter(str(ass), fonts_dir), "-f", "null", "-",
        ]
    )  # fmt: skip
    selected, fallbacks = set(), []
    for line in log.splitlines():
        if "fontselect:" in line and "->" in line:
            target = line.split("->", 1)[1].strip()
            selected.add(target)
            if "/" in target and fonts_dir not in target:
                fallbacks.append(line.split("] ", 1)[-1].strip())
        elif "not found, selecting one more font" in line:
            fallbacks.append(line.split("] ", 1)[-1].strip())
    return {"fonts_selected": sorted(selected), "fallbacks": fallbacks}


def _box(png: Path, predicate, top: int = 700, bottom: int | None = None) -> list[int] | None:  # noqa: ANN001
    from PIL import Image

    image = Image.open(png).convert("RGB")
    pixels = image.load()
    xs, ys = [], []
    for y in range(max(0, top), min(image.height, bottom or image.height)):
        for x in range(image.width):
            if predicate(pixels[x, y]):
                xs.append(x)
                ys.append(y)
    return [min(xs), min(ys), max(xs), max(ys)] if xs else None


def _is_lime(p) -> bool:  # noqa: ANN001
    return abs(p[0] - LIME[0]) < 45 and abs(p[1] - LIME[1]) < 45 and p[2] < 90


def _is_ink(p) -> bool:  # noqa: ANN001
    return _is_lime(p) or min(p) > 200


def _direction(video: Path, cue: dict, language: str, frames: Path) -> dict:
    """Where the first and last spoken word light up on their own text row."""
    words = cue.get("words") or []
    sides: dict = {}
    for name, word in (("first", words[0]), ("last", words[-1])):
        png = frames / f"{language}_dir_{name}.png"
        _frame(video, (word["start_s"] + word["end_s"]) / 2, png)
        lime = _box(png, _is_lime)
        if lime is None:
            return {"pass": False, "reason": f"no highlight found for the {name} word"}
        row = _box(png, _is_ink, top=lime[1] - 4, bottom=lime[3] + 5)
        if row[2] - lime[2] <= 8:
            sides[name] = "right"
        elif lime[0] - row[0] <= 8:
            sides[name] = "left"
        else:
            sides[name] = "middle"
        sides[f"{name}_word_box"] = lime
        sides[f"{name}_row_box"] = row
    expected = ("right", "left") if language in RTL else ("left", "right")
    return {
        "expected": {"first": expected[0], "last": expected[1]},
        **sides,
        "pass": (sides["first"], sides["last"]) == expected,
    }


def _phone(language: str, cues: list[dict]) -> dict:
    from app.pipeline.phone_captions import compile_caption_layers

    report = {}
    for style in ("sentence", "word"):
        layers = compile_caption_layers(cues, canvas_width=1080, canvas_height=1920, style=style)
        runs = [run for layer in layers for run in layer.runs]
        order_ok = True
        for layer in layers:
            rows: dict[float, list] = {}
            for run in layer.runs:
                rows.setdefault(run.baseline_y, []).append(run)
            for row in rows.values():
                xs = [run.x for run in row]
                if style == "word" and len(xs) > 1:
                    want = sorted(xs, reverse=language in RTL)
                    order_ok = order_ok and xs == want
        report[style] = {
            "layers": len(layers),
            "font_assets": sorted({run.font_asset_id for run in runs}),
            "shaped_runs": sum(1 for run in runs if run.shaped),
            "positioned_glyph_runs": sum(1 for run in runs if run.glyphs),
            "max_rows": max(len({run.baseline_y for run in layer.runs}) for layer in layers),
            "karaoke_active_only": sorted(
                {bool(layer.karaoke and layer.karaoke.active_only) for layer in layers}
            ),
            "word_order_matches_direction": order_ok,
        }
    return report


def _skia_verify() -> dict:
    from app.pipeline import overlay_verify as ov

    recipe = json.loads(SKIA_FIXTURE.read_text(encoding="utf-8"))
    report = ov.verify_recipe(recipe, render_mode=ov.RENDER_DRAW_FRAME, run_ocr=False)
    return {
        "overall": report.overall,
        "overlays": [
            {
                "text": o.text[:40],
                "effect": o.effect,
                "verdict": o.verdict,
                "face": o.resolved_typeface.get("name"),
                "reasons": [r for r in o.reasons if "glyph" in r or "unshaped" in r],
            }
            for o in report.overlays
        ],
    }


def _montage(cells: dict, out: Path) -> None:
    from PIL import Image, ImageDraw

    cell_w, cell_h, label_h = 324, 280, 34
    sheet = Image.new(
        "RGB",
        (cell_w * len(FORMATS) + 60, (cell_h + label_h) * len(LANGUAGES) + 30),
        (20, 22, 26),
    )
    draw = ImageDraw.Draw(sheet)
    for col, fmt in enumerate(FORMATS):
        draw.text((60 + col * cell_w + 6, 8), fmt, fill=(230, 230, 230))
    for row, language in enumerate(LANGUAGES):
        y0 = 30 + row * (cell_h + label_h)
        draw.text((10, y0 + cell_h // 2), language, fill=(230, 230, 230))
        for col, fmt in enumerate(FORMATS):
            cell = cells.get((language, fmt))
            if not cell:
                continue
            frame = Image.open(cell["png"]).convert("RGB").crop((0, 900, 1080, 1840))
            frame = frame.resize((cell_w - 8, cell_h))
            x0 = 60 + col * cell_w
            sheet.paste(frame, (x0 + 4, y0))
            ok = cell["ok"]
            draw.rectangle(
                (x0 + 3, y0 - 1, x0 + cell_w - 4, y0 + cell_h),
                outline=(60, 180, 75) if ok else (220, 50, 50),
                width=2,
            )
            draw.text((x0 + 6, y0 + cell_h + 2), cell["label"], fill=(200, 200, 200))
    sheet.save(out)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n", 1)[0])
    parser.add_argument("--out", default=str(API_DIR.parents[2] / ".caption-verify"))
    parser.add_argument("--languages", default=",".join(LANGUAGES))
    args = parser.parse_args()
    if shutil.which("ffmpeg") is None:
        print("ffmpeg with libass is required", file=sys.stderr)
        return 2

    from app.config import settings
    from app.pipeline import captions
    from app.pipeline.font_coverage import FONTS_DIR
    from app.pipeline.narrated_assembler import burn_captions_on_video

    # The phone recipe section simulates a build that bundles the script faces (KRI-1192 F16).
    settings.phone_render_verified_features = [
        *settings.phone_render_verified_features,
        "scriptFonts",
    ]
    out = Path(args.out).resolve()
    frames = out / "frames"
    frames.mkdir(parents=True, exist_ok=True)
    clips = json.loads(FIXTURE.read_text(encoding="utf-8"))["clips"]
    languages = [lang for lang in args.languages.split(",") if lang in LANGUAGES]
    report: dict = {
        "captions_module": captions.__file__,
        "ffmpeg": subprocess.run(
            ["ffmpeg", "-version"], capture_output=True, text=True, check=False
        ).stdout.split("\n")[0],
        "languages": {},
    }
    cells: dict = {}
    failures = 0
    with tempfile.TemporaryDirectory(prefix="kri1192_evidence_") as tmp_root:
        for language in languages:
            texts = _sentences(clips, language)
            words = _words(texts)
            duration = words[-1].end_s + 0.8
            tmp = Path(tmp_root) / language
            tmp.mkdir()
            base = tmp / "base.mp4"
            _base_clip(base, duration)
            entry: dict = {"sentences": texts, "formats": {}}
            for fmt in FORMATS:
                ass_path, cues = _write_ass(fmt, words, tmp)
                ass = ass_path.read_text(encoding="utf-8")
                text = " ".join(str(cue["text"]) for cue in cues)
                faces = _faces(ass)
                missing = {face: _missing(face, text) for face in faces}
                burned = tmp / f"{fmt}.mp4"
                burn_captions_on_video(str(base), str(ass_path), FONTS_DIR, str(burned))
                if fmt == "voiceover_word":
                    probe = max(cues, key=lambda c: len(str(c["text"])))
                else:
                    probe = cues[0]
                png = frames / f"{language}_{fmt}.png"
                _frame(burned, (float(probe["start_s"]) + float(probe["end_s"])) / 2, png)
                ink = _box(png, _is_ink)
                trace = _libass_trace(ass_path, duration, FONTS_DIR)
                result = {
                    "ass_faces": faces,
                    "missing_glyphs_in_face": missing,
                    "ass_encoding": _style_fields(ass)[-1].split()[0],
                    "libass_fonts_selected": trace["fonts_selected"],
                    "libass_fallbacks": trace["fallbacks"],
                    "probe_cue": str(probe["text"]),
                    "ink_box": ink,
                    "ink_inside_frame": bool(ink and ink[0] > 2 and ink[2] < 1077),
                    "frame": str(png.relative_to(out)),
                }
                if fmt == "talk_word_pop":
                    result["direction"] = _direction(burned, cues[0], language, frames)
                want_encoding = "-1" if language in RTL else "1"
                ok = (
                    not any(missing.values())
                    and not trace["fallbacks"]
                    and result["ass_encoding"] == want_encoding
                    and result["ink_inside_frame"]
                    and result.get("direction", {"pass": True})["pass"]
                )
                result["pass"] = ok
                failures += 0 if ok else 1
                entry["formats"][fmt] = result
                cells[(language, fmt)] = {
                    "png": png,
                    "ok": ok,
                    "label": f"{'/'.join(faces)} enc={result['ass_encoding']}"
                    f" fb={len(trace['fallbacks'])}",
                }
                burned.unlink(missing_ok=True)
            # The phone Talking lane's cues: the phone cue budget, then the sentence re-split.
            cues = captions.resplit_cues_into_sentences(
                captions.build_plain_cues(
                    words, attach_words=True, budget=captions.PHONE_CUE_BUDGET
                )
            )
            entry["phone_recipe"] = _phone(language, cues)
            report["languages"][language] = entry
            base.unlink(missing_ok=True)
            print(f"{language}: " + ", ".join(
                f"{fmt}={'PASS' if entry['formats'][fmt]['pass'] else 'FAIL'}" for fmt in FORMATS
            ))  # fmt: skip
    report["skia_overlay_verify"] = _skia_verify()
    report["summary"] = {
        "cells": len(cells),
        "failed_cells": failures,
        "skia_overall": report["skia_overlay_verify"]["overall"],
    }
    (out / "report.json").write_text(
        json.dumps(report, ensure_ascii=False, indent=1, default=str), encoding="utf-8"
    )
    _montage(cells, out / "montage.png")
    print(f"report: {out / 'report.json'}\nmontage: {out / 'montage.png'}")
    print(json.dumps(report["summary"]))
    return 1 if failures or report["skia_overlay_verify"]["overall"] == "FAIL" else 0


if __name__ == "__main__":
    raise SystemExit(main())
