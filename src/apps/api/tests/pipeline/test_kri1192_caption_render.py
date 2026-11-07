"""KRI-1192: spoken captions in ten more languages draw correctly in the cloud and on the phone.

Failure modes (docs/reviews/kri-1192-caption-languages.md section 3, written before the code):

F6  a caption letter has no glyph in the face that draws it: libass falls back per letter
    (DejaVu in prod, nothing at all for Hangul) or Skia draws a box.
F7  Arabic is drawn left to right, unjoined, with its end mark on the wrong side, or the
    word highlight sweeps the wrong way.
F9  a Hangul cue is too wide for a character budget; a long German/Portuguese word runs
    off the frame in the one-big-word style.
F10 a sentence end is missed (Greek ";" is a question mark, Arabic "؟"), so two
    sentences share one cue; an Arabic comma "،" is not a clause break.
F11 the cloud burn and the phone recipe draw a caption in different faces.
F15 English or Turkish output changes (see test_kri1192_en_tr_byte_identity.py; this file
    adds the ";" rule check for English).
F16 an old iPhone build receives a recipe naming Noto Sans KR / Arabic, which it does not
    bundle: the phone lane only uses those faces when ``scriptFonts`` is verified and the
    requesting build is new enough; otherwise it keeps today's decline. The cloud never gates.

Text: real FLEURS sentences (CC BY 4.0) from the KRI-1192 fixture. Serbian uses whisper's
Latin text (product decision: Serbian captions are Latin). Every check goes through the
real caption functions; the libass checks burn real frames with the local ffmpeg.
"""

from __future__ import annotations

import json
import re
import shutil
import subprocess
from pathlib import Path

import pytest

from app.pipeline import font_coverage
from app.pipeline.captions import (
    PHONE_CUE_BUDGET,
    build_plain_cues,
    build_word_cues,
    generate_ass_from_cues,
    generate_word_pop_ass,
    resplit_cues_into_sentences,
)
from app.pipeline.narrated_assembler import resolve_caption_font
from app.pipeline.transcribe import Word

FIXTURE = (
    Path(__file__).resolve().parents[1]
    / "fixtures"
    / "kri1192_caption_languages"
    / "fleurs_whisper_turbo.json"
)
CLIPS = json.loads(FIXTURE.read_text(encoding="utf-8"))["clips"]
NEW = ("pt", "es", "el", "de", "ko", "it", "fr", "ar", "da", "sr")
FONTS_DIR = str(font_coverage.FONTS_DIR)
HAS_FFMPEG = shutil.which("ffmpeg") is not None
needs_ffmpeg = pytest.mark.skipif(not HAS_FFMPEG, reason="ffmpeg with libass is required")


@pytest.fixture(autouse=True)
def _script_fonts_verified(monkeypatch):
    """The phone lane may name the script faces (F16 gate open: verified, worker context)."""
    from app.config import settings

    monkeypatch.setattr(
        settings,
        "phone_render_verified_features",
        [*settings.phone_render_verified_features, "scriptFonts"],
    )


def _sentences(language: str, count: int = 12) -> list[str]:
    key = "whisper_text" if language == "sr" else "reference"
    return [
        " ".join(str(clip[key]).replace('""', '"').split())
        for clip in CLIPS
        if clip["language"] == language
    ][:count]


def _words(text: str, start_s: float = 0.0, per_word_s: float = 0.4) -> list[Word]:
    return [
        Word(
            text=token,
            start_s=round(start_s + index * per_word_s, 3),
            end_s=round(start_s + index * per_word_s + per_word_s * 0.9, 3),
            confidence=0.95,
        )
        for index, token in enumerate(text.split())
    ]


def _timeline(texts: list[str]) -> list[Word]:
    words: list[Word] = []
    cursor = 0.0
    for text in texts:
        part = _words(text, cursor)
        words.extend(part)
        cursor = part[-1].end_s + 0.6
    return words


def _cues(language: str, count: int = 12) -> list[dict]:
    words = _timeline(_sentences(language, count))
    return resplit_cues_into_sentences(build_plain_cues(words, attach_words=True))


def _ass_text(tmp_path: Path, write, name: str = "captions.ass") -> str:  # noqa: ANN001
    path = tmp_path / name
    write(str(path))
    return path.read_text(encoding="utf-8")


def _style(ass: str) -> list[str]:
    line = next(line for line in ass.splitlines() if line.startswith("Style: Default,"))
    return line.removeprefix("Style: ").split(",")


def _faces(ass: str) -> set[str]:
    faces = {_style(ass)[1]}
    for line in ass.splitlines():
        faces.update(match.group(1) for match in re.finditer(r"\\fn([^\\}]+)", line))
    return faces


def _registry_keys(face: str) -> list[str]:
    fonts = font_coverage._registry()["fonts"]
    return [key for key, entry in fonts.items() if entry.get("ass_name") == face]


def _face_missing(face: str, text: str) -> str:
    keys = _registry_keys(face)
    assert keys, f"ASS face {face!r} is not a bundled registry face"
    return "".join(sorted({ch for key in keys for ch in font_coverage.missing_glyphs(key, text)}))


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


def _all_styles(cues: list[dict], font: str) -> dict:
    words = [w for cue in cues for w in cue.get("words") or []]
    word_cues = build_word_cues([Word(w["text"], w["start_s"], w["end_s"], 0.95) for w in words])
    return {
        "narrated_plain": lambda p: generate_ass_from_cues(cues, p, font_name=font),
        "narrated_word": lambda p: generate_ass_from_cues(
            word_cues, p, font_name=font, style="word"
        ),
        "subtitled_sentence": lambda p: generate_ass_from_cues(
            cues, p, font_name=font, style="plain", margin_v=384, pop_in=True
        ),
        "subtitled_word_pop": lambda p: generate_word_pop_ass(
            cues, p, font_name=font, margin_v=384
        ),
        "editor_highlight": lambda p: generate_ass_from_cues(
            cues,
            p,
            font_name=font,
            margin_v=384,
            appearance={"highlight_spoken_word": True, "display_style": "sentence"},
        ),
        "smart": lambda p: generate_ass_from_cues(
            cues,
            p,
            font_name=font,
            margin_v=384,
            pop_in=True,
            smart_policy={**SMART_POLICY, "font_family": font},
        ),
    }


# ── F6: one covering face per burn (cloud ASS) ──────────────────────────────


@pytest.mark.parametrize("language", NEW)
@pytest.mark.parametrize("creator_font", [None, "Montserrat Bold", "Inter Tight"])
def test_every_caption_style_burns_in_a_face_that_has_every_letter(
    tmp_path, language, creator_font
):
    cues = _cues(language)
    text = " ".join(cue["text"] for cue in cues)
    font = resolve_caption_font(creator_font)
    for style, write in _all_styles(cues, font).items():
        ass = _ass_text(tmp_path, write, f"{style}.ass")
        faces = _faces(ass)
        assert len(faces) == 1, f"{language}/{style}: one face per burn, got {faces}"
        missing = _face_missing(next(iter(faces)), text)
        assert not missing, f"{language}/{style}: {faces} lacks {missing!r}"


@pytest.mark.parametrize("language", ["el", "ko", "ar"])
def test_a_per_cue_font_override_is_made_glyph_safe_for_that_cue(tmp_path, language):
    cues = _cues(language, 3)
    cues[1] = {**cues[1], "font_family": resolve_caption_font("Montserrat Bold")}
    ass = _ass_text(
        tmp_path,
        lambda p: generate_word_pop_ass(cues, p, font_name="TikTok Sans", margin_v=384),
    )
    overrides = {match.group(1) for match in re.finditer(r"\\fn([^\\}]+)", ass)}
    assert overrides, "the per-cue override must still be emitted"
    for face in overrides:
        assert not _face_missing(face, cues[1]["text"]), (language, face)


@pytest.mark.parametrize("language", ["pt", "es", "de", "it", "fr", "da", "sr"])
def test_latin_languages_keep_the_chosen_caption_face(tmp_path, language):
    cues = _cues(language)
    for creator_font in (None, "Montserrat Bold", "Inter Tight"):
        font = resolve_caption_font(creator_font)
        ass = _ass_text(
            tmp_path,
            lambda p, font=font: generate_ass_from_cues(cues, p, font_name=font),
        )
        assert _faces(ass) == {font}


# ── F7: Arabic base direction in the ASS ────────────────────────────────────


def test_arabic_captions_let_libass_detect_the_base_direction(tmp_path):
    cues = _cues("ar", 4)
    for style, write in _all_styles(cues, "TikTok Sans").items():
        ass = _ass_text(tmp_path, write, f"{style}.ass")
        assert _style(ass)[-1] == "-1", f"{style}: Encoding must be -1 (auto direction)"


@pytest.mark.parametrize("language", ["en", "tr", "el", "ko", "de"])
def test_left_to_right_captions_keep_encoding_one(tmp_path, language):
    cues = _cues(language, 3)
    ass = _ass_text(tmp_path, lambda p: generate_ass_from_cues(cues, p, font_name="TikTok Sans"))
    assert _style(ass)[-1] == "1"


# ── libass burns (real frames) ──────────────────────────────────────────────


def _burn_frame(ass_path: Path, at_s: float, out_png: Path, *, trace: bool = False) -> str:
    from app.pipeline.narrated_assembler import caption_burn_filter

    duration = at_s + 0.3
    cmd = [
        "ffmpeg",
        "-hide_banner",
        "-v",
        "trace" if trace else "error",
        "-f",
        "lavfi",
        "-i",
        f"color=c=0x303030:s=1080x1920:r=10:d={duration:.2f}",
        "-vf",
        caption_burn_filter(str(ass_path), FONTS_DIR),
        "-ss",
        f"{at_s:.2f}",
        "-frames:v",
        "1",
        "-y",
        str(out_png),
    ]
    result = subprocess.run(cmd, capture_output=True, timeout=120, check=False)
    assert result.returncode == 0, result.stderr.decode(errors="replace")[-800:]
    return result.stderr.decode(errors="replace")


def _fallback_lines(log: str) -> list[str]:
    """libass log lines that mean a letter was drawn from a face outside the bundle."""
    bad = []
    for line in log.splitlines():
        if "not found, selecting one more font" in line:
            bad.append(line.strip())
        elif "fontselect:" in line and "->" in line:
            target = line.split("->", 1)[1]
            if "/" in target and FONTS_DIR not in target:
                bad.append(line.strip())
    return bad


@needs_ffmpeg
@pytest.mark.parametrize("language", ["el", "ko", "ar", "sr", "de"])
def test_libass_draws_every_letter_from_the_bundled_face(tmp_path, language):
    cues = _cues(language, 2)
    ass_path = tmp_path / "captions.ass"
    generate_ass_from_cues(
        cues, str(ass_path), font_name="TikTok Sans", style="plain", margin_v=384, pop_in=True
    )
    for index, cue in enumerate(cues):
        at = (float(cue["start_s"]) + float(cue["end_s"])) / 2
        log = _burn_frame(ass_path, at, tmp_path / f"f{index}.png", trace=True)
        assert "fontselect" in log, "libass trace logging is unavailable"
        assert not _fallback_lines(log), _fallback_lines(log)


LIME = (0x84, 0xCC, 0x16)


def _ink(png: Path, predicate, top: int = 900) -> tuple[int, int, int, int] | None:  # noqa: ANN001
    from PIL import Image

    image = Image.open(png).convert("RGB")
    pixels = image.load()
    xs, ys = [], []
    for y in range(top, image.height):
        for x in range(image.width):
            if predicate(pixels[x, y]):
                xs.append(x)
                ys.append(y)
    if not xs:
        return None
    return min(xs), min(ys), max(xs), max(ys)


def _is_lime(pixel) -> bool:  # noqa: ANN001
    return abs(pixel[0] - LIME[0]) < 40 and abs(pixel[1] - LIME[1]) < 40 and pixel[2] < 80


def _is_text(pixel) -> bool:  # noqa: ANN001
    return _is_lime(pixel) or min(pixel) > 200


@needs_ffmpeg
def test_libass_word_highlight_sweeps_right_to_left_for_arabic(tmp_path):
    text = "سلم محمد حسن ممكن؟"
    cue = {
        "text": text,
        "start_s": 0.0,
        "end_s": 2.0,
        "words": [
            {"text": token, "start_s": 0.5 * i, "end_s": 0.5 * i + 0.45}
            for i, token in enumerate(text.split())
        ],
    }
    ass_path = tmp_path / "pop.ass"
    generate_word_pop_ass([cue], str(ass_path), font_name="TikTok Sans", margin_v=384)
    first = _burn_frame(ass_path, 0.2, tmp_path / "first.png")
    assert first == ""
    _burn_frame(ass_path, 1.7, tmp_path / "last.png")
    line = _ink(tmp_path / "first.png", _is_text)
    first_word = _ink(tmp_path / "first.png", _is_lime)
    last_word = _ink(tmp_path / "last.png", _is_lime)
    assert line and first_word and last_word
    # The first spoken word is the rightmost; the last word, with its "؟", the leftmost.
    assert line[2] - first_word[2] <= 6, (line, first_word)
    assert last_word[0] - line[0] <= 6, (line, last_word)
    assert last_word[2] < (line[0] + line[2]) / 2, "the end mark must sit left, with its word"


def _components(png: Path, predicate, *, min_pixels: int = 40) -> list[tuple[int, int, int, int]]:  # noqa: ANN001
    """Bounding boxes of the 8-connected ink blobs, left to right (dots dropped)."""
    from PIL import Image

    image = Image.open(png).convert("RGBA")
    width, height = image.size
    pixels = image.load()
    seen = bytearray(width * height)
    boxes = []
    for y0 in range(height):
        for x0 in range(width):
            if seen[y0 * width + x0] or not predicate(pixels[x0, y0]):
                continue
            stack = [(x0, y0)]
            seen[y0 * width + x0] = 1
            count, x1, y1, x2, y2 = 0, x0, y0, x0, y0
            while stack:
                x, y = stack.pop()
                count += 1
                x1, y1, x2, y2 = min(x1, x), min(y1, y), max(x2, x), max(y2, y)
                for dx in (-1, 0, 1):
                    for dy in (-1, 0, 1):
                        nx, ny = x + dx, y + dy
                        if 0 <= nx < width and 0 <= ny < height and not seen[ny * width + nx]:
                            if predicate(pixels[nx, ny]):
                                seen[ny * width + nx] = 1
                                stack.append((nx, ny))
            if count >= min_pixels:
                boxes.append((x1, y1, x2, y2))
    return sorted(boxes)


@needs_ffmpeg
@pytest.mark.parametrize(
    ("language", "face_file"), [("ar", "NotoSansArabic-Bold.ttf"), ("ko", "NotoSansKR-Bold.ttf")]
)
def test_a_script_face_draws_at_the_size_of_the_face_it_replaces(tmp_path, language, face_file):
    """libass sizes a face by its OS/2 win height, and Noto Sans Arabic's is tall: at the
    same ASS Fontsize its letters came out at 63% of the TikTok Sans caption's em (the
    phone, sizing by em, drew them full size). The burn keeps TikTok Sans's em."""
    import skia
    from PIL import Image

    from app.pipeline.render_geometry import _libass_em_scale

    text = " ".join(_sentences(language, 1)[0].split()[:3])
    cue = {"text": text, "start_s": 0.0, "end_s": 1.0}
    ass_path = tmp_path / "size.ass"
    generate_ass_from_cues([cue], str(ass_path), font_name="TikTok Sans", margin_v=384)
    png = tmp_path / "size.png"
    _burn_frame(ass_path, 0.5, png)
    ink = _ink(png, lambda p: min(p) > 200)
    assert ink is not None
    em = 78 * _libass_em_scale("TikTokSans-Bold.ttf")
    typeface = skia.Typeface.MakeFromFile(str(Path(FONTS_DIR) / face_file))
    surface = skia.Surface(1600, 400)
    canvas = surface.getCanvas()
    canvas.clear(skia.ColorBLACK)
    from app.pipeline.text_overlay_skia import _shaped_paragraph

    paragraph = _shaped_paragraph(
        text, skia.Font(typeface, em), 0.0, skia.Paint(AntiAlias=True, Color=skia.ColorWHITE)
    )
    paragraph.paint(canvas, 20, 100)
    reference = Image.fromarray(surface.makeImageSnapshot().toarray()[:, :, :3])
    ref_path = tmp_path / "ref.png"
    reference.save(ref_path)
    ref = _ink(ref_path, lambda p: min(p) > 200, top=0)
    assert ref is not None
    drawn_w, want_w = ink[2] - ink[0], ref[2] - ref[0]
    assert abs(drawn_w - want_w) <= max(6, 0.04 * want_w), (drawn_w, want_w)


# ── F7 in Skia (authored text / overlay_verify path) ────────────────────────

# Every letter of these words joins on both sides (or, د, to the right) and has no dot,
# so a joined word is ONE blob and an unjoined one is one blob per letter.
_JOINING = ("سلم", "محمد")


def _skia_frame(text: str, tmp_path: Path, name: str) -> Path:
    from app.pipeline.overlay_verify import render_overlay_frame

    frame = render_overlay_frame(
        {
            "text": text,
            "effect": "static",
            "font_family": "TikTok Sans Bold",
            "text_size_px": 110,
            "position_x_frac": 0.5,
            "position_y_frac": 0.5,
            "text_color": "#FFFFFF",
            "stroke_width": 0,
            "shadow_enabled": False,
            "start_s": 0,
            "end_s": 2,
        }
    )
    path = tmp_path / f"{name}.png"
    frame.save(path)
    return path


def _opaque(pixel) -> bool:  # noqa: ANN001
    return pixel[3] > 128


def test_skia_draws_arabic_joined_and_right_to_left(tmp_path):
    line = _components(_skia_frame(" ".join(_JOINING), tmp_path, "line"), _opaque)
    first = _components(_skia_frame(_JOINING[0], tmp_path, "first"), _opaque)
    second = _components(_skia_frame(_JOINING[1], tmp_path, "second"), _opaque)
    assert len(first) == 1 and len(second) == 1, "each word must be one joined stroke"
    assert len(line) == 2, f"two joined words, got {len(line)} blobs"
    width = lambda box: box[2] - box[0]  # noqa: E731
    left, right = line
    # Right to left: the first word is the right blob.
    assert abs(width(right) - width(first[0])) <= 3, (right, first)
    assert abs(width(left) - width(second[0])) <= 3, (left, second)


def test_overlay_verify_fails_arabic_drawn_unshaped(monkeypatch):
    from app.pipeline import overlay_verify as ov
    from app.pipeline import text_overlay_skia as tos

    overlay = {
        "text": "سلم محمد",
        "effect": "static",
        "font_family": "TikTok Sans Bold",
        "text_size_px": 96,
        "position_x_frac": 0.5,
        "position_y_frac": 0.5,
        "start_s": 0,
        "end_s": 2,
    }
    assert ov.verify_overlay(overlay, run_ocr=False).verdict == "PASS"
    monkeypatch.setattr(tos, "_auto_shape", lambda typeface, text, shape_text: shape_text)
    result = ov.verify_overlay(overlay, run_ocr=False)
    assert result.verdict == "FAIL"
    assert "unshaped" in " ".join(result.reasons)


# ── F16: the phone lane gates the script faces ──────────────────────────────


@pytest.mark.parametrize("gate", ["not_verified", "old_build"])
def test_an_old_or_unverified_phone_build_never_gets_a_script_face(monkeypatch, tmp_path, gate):
    from app.config import settings
    from app.pipeline.phone_captions import compile_caption_layers
    from app.pipeline.phone_guided_plan import UnsupportedPhonePlan
    from app.pipeline.phone_text_style import drawable_family
    from app.services import phone_rollout

    if gate == "not_verified":
        monkeypatch.setattr(settings, "phone_render_verified_features", [])
    else:
        monkeypatch.setattr(phone_rollout, "script_fonts_client_supported", lambda **_: False)
    for language in ("ko", "ar"):
        cues = _cues(language, 2)
        assert drawable_family("TikTok Sans Bold", [cue["text"] for cue in cues]) is None
        with pytest.raises(UnsupportedPhonePlan, match="newer app build"):
            compile_caption_layers(cues, canvas_width=1080, canvas_height=1920)
        # The cloud burn is not gated: it still draws the script face.
        ass = _ass_text(
            tmp_path, lambda p, cues=cues: generate_ass_from_cues(cues, p, font_name="TikTok Sans")
        )
        assert _faces(ass) == {"Noto Sans KR" if language == "ko" else "Noto Sans Arabic"}
    # Greek and Cyrillic draw in TikTok Sans, which every build bundles: never gated.
    greek = _cues("el", 2)
    assert drawable_family("TikTok Sans Bold", [cue["text"] for cue in greek]) == "TikTok Sans Bold"
    assert compile_caption_layers(greek, canvas_width=1080, canvas_height=1920)


# ── F7 on the phone recipe ──────────────────────────────────────────────────


def _arabic_cue(text: str, start: float = 0.0) -> dict:
    tokens = text.split()
    return {
        "text": text,
        "start_s": start,
        "end_s": start + 0.5 * len(tokens),
        "words": [
            {"text": token, "start_s": start + 0.5 * i, "end_s": start + 0.5 * i + 0.45}
            for i, token in enumerate(tokens)
        ],
    }


def test_phone_karaoke_caption_sends_arabic_as_shaped_words_right_to_left():
    from app.pipeline import text_overlay_skia as cloud
    from app.pipeline.phone_captions import compile_caption_layers

    text = _sentences("ar", 1)[0]
    layers = compile_caption_layers(
        [_arabic_cue(text)], canvas_width=1080, canvas_height=1920, style="word"
    )
    # A long sentence may come back as consecutive pieces (KRI-632 row budget); every piece is
    # one shaped karaoke line and together they carry the words in order.
    assert layers and all(layer.effect == "karaoke-line" for layer in layers)
    # Shaped karaoke runs are only valid in active-word mode (the cloud word-pop look).
    assert all(layer.karaoke.active_only is True for layer in layers)
    runs = [run for layer in layers for run in layer.runs]
    assert [run.text for run in runs] == text.split()
    assert all(run.shaped and run.glyphs is None for run in runs)
    assert runs[0].font_asset_id.startswith("font-NotoSansArabic")
    typeface = cloud._resolve_typeface_for_overlay({"font_family": "Noto Sans Arabic"}).typeface
    for layer in layers:
        rows: dict[float, list] = {}
        for run in layer.runs:
            rows.setdefault(run.baseline_y, []).append(run)
        for row in rows.values():
            xs = [run.x for run in row]
            assert xs == sorted(xs, reverse=True), "logical order must run right to left"
            font = cloud.skia.Font(typeface, row[0].font_size)
            right = row[0].x + cloud._measure_line(font, row[0].text, 0.0, shape_text=True)
            left = row[-1].x
            assert abs((left + right) / 2 - 540) <= 2, "a centred row stays centred"


def test_phone_sentence_caption_sends_arabic_as_shaped_lines():
    from app.pipeline.phone_captions import compile_caption_layers

    layers = compile_caption_layers(
        [
            {"text": t, "start_s": i * 3.0, "end_s": i * 3.0 + 2.5}
            for i, t in enumerate(_sentences("ar", 3))
        ],
        canvas_width=1080,
        canvas_height=1920,
    )
    runs = [run for layer in layers for run in layer.runs]
    assert runs and all(run.shaped and run.glyphs is None for run in runs)


@pytest.mark.parametrize("language", ["en", "de", "el", "ko"])
def test_phone_left_to_right_captions_keep_positioned_glyphs(language):
    from app.pipeline.phone_captions import compile_caption_layers

    layers = compile_caption_layers(
        _cues(language, 2), canvas_width=1080, canvas_height=1920, style="word"
    )
    assert all(not run.shaped and run.glyphs for layer in layers for run in layer.runs)


# ── F11: the cloud and the phone draw the same face ─────────────────────────


@pytest.mark.parametrize(
    ("language", "family", "file_prefix"),
    [
        ("ko", "Noto Sans KR", "font-NotoSansKR"),
        ("ar", "Noto Sans Arabic", "font-NotoSansArabic"),
        ("el", "TikTok Sans", "font-TikTokSans"),
    ],
)
def test_cloud_and_phone_pick_the_same_caption_face(tmp_path, language, family, file_prefix):
    from app.pipeline.phone_captions import compile_caption_layers
    from app.pipeline.phone_text_style import drawable_family

    cues = _cues(language, 3)
    ass = _ass_text(tmp_path, lambda p: generate_ass_from_cues(cues, p, font_name="TikTok Sans"))
    assert _faces(ass) == {family}
    layers = compile_caption_layers(cues, canvas_width=1080, canvas_height=1920)
    assert {
        run.font_asset_id.split("-Regular")[0].split("-Bold")[0]
        for layer in layers
        for run in layer.runs
    } == {file_prefix}
    texts = [cue["text"] for cue in cues]
    chosen = drawable_family("TikTok Sans Bold", texts)
    assert chosen is not None
    assert font_coverage._registry()["fonts"][chosen]["ass_name"] == family


# ── F9: width ────────────────────────────────────────────────────────────────


def _display_width(text: str) -> int:
    import unicodedata

    return sum(2 if unicodedata.east_asian_width(ch) in "WF" else 1 for ch in text)


def test_korean_phone_cues_count_a_hangul_syllable_as_two_letters():
    for text in _sentences("ko"):
        for cue in build_plain_cues(_words(text), attach_words=True, budget=PHONE_CUE_BUDGET):
            if len(cue["text"].split()) > 1:
                assert _display_width(cue["text"]) <= PHONE_CUE_BUDGET.max_chars, cue["text"]


def test_korean_phone_captions_stay_within_two_rows():
    from app.pipeline.phone_captions import compile_caption_layers

    cues = build_plain_cues(_timeline(_sentences("ko")), attach_words=True, budget=PHONE_CUE_BUDGET)
    for style in ("sentence", "word"):
        layers = compile_caption_layers(cues, canvas_width=1080, canvas_height=1920, style=style)
        for cue, layer in zip(cues, layers, strict=True):
            rows = len({run.baseline_y for run in layer.runs})
            assert rows <= 2, f"{style}: {rows} rows for {cue['text']!r}"


def test_guided_word_groups_count_a_hangul_syllable_as_two_letters():
    from app.pipeline.guided_caption_presentation import _legacy_groups

    words = _words(" ".join(_sentences("ko", 4)), per_word_s=0.3)
    rows = [{"text": w.text, "start_s": w.start_s, "end_s": w.end_s, "role": "x"} for w in words]
    for group in _legacy_groups(rows):
        combined = " ".join(row["text"] for row in group)
        if len(group) > 1:
            assert _display_width(combined) <= 42, combined


@needs_ffmpeg
@pytest.mark.parametrize(
    ("language", "word"),
    [
        ("de", "Unabhängigkeitserklärung"),
        ("pt", "Consequentemente,"),
        ("da", "Nyiragongo-vulkanen"),
    ],
)
def test_one_big_word_captions_never_run_off_the_frame(tmp_path, language, word):
    cues = build_word_cues(_words(f"Das {word} war"))
    ass_path = tmp_path / "word.ass"
    generate_ass_from_cues(cues, str(ass_path), font_name="TikTok Sans", style="word")
    long_cue = next(cue for cue in cues if cue["text"] == word)
    png = tmp_path / "word.png"
    _burn_frame(ass_path, (long_cue["start_s"] + long_cue["end_s"]) / 2, png)
    box = _ink(png, lambda p: min(p) > 200 or max(p) < 10)
    assert box is not None
    assert box[0] > 4 and box[2] < 1075, f"{word!r} ink {box} touches the frame edge"


@needs_ffmpeg
def test_smart_korean_captions_wrap_inside_the_policy_width(tmp_path):
    cues = _cues("ko", 4)
    ass_path = tmp_path / "smart.ass"
    generate_ass_from_cues(
        cues,
        str(ass_path),
        font_name="TikTok Sans",
        margin_v=384,
        pop_in=True,
        smart_policy=SMART_POLICY,
    )
    limit = 1080 * SMART_POLICY["width_frac"] + 2 * SMART_POLICY["stroke_width"] + 8
    for index, cue in enumerate(cues):
        png = tmp_path / f"s{index}.png"
        _burn_frame(ass_path, (float(cue["start_s"]) + float(cue["end_s"])) / 2, png)
        box = _ink(png, lambda p: min(p) > 200)
        assert box is not None
        assert box[2] - box[0] <= limit, f"{cue['text']!r}: {box[2] - box[0]}px > {limit}"


# ── F10: sentence ends ───────────────────────────────────────────────────────


def test_a_greek_question_mark_ends_the_cue():
    cues = build_plain_cues(_words("Τι κάνεις σήμερα; Είμαι πολύ καλά, ευχαριστώ."))
    assert [cue["text"] for cue in cues] == ["Τι κάνεις σήμερα;", "Είμαι πολύ καλά, ευχαριστώ."]


def test_an_english_or_turkish_semicolon_never_ends_the_cue():
    for text in ("Wait here; then we go home.", "Burada bekle; sonra eve gideriz."):
        assert len(build_plain_cues(_words(text))) == 1


def test_an_arabic_question_mark_ends_the_cue():
    cues = build_plain_cues(_words("كيف حالك اليوم؟ أنا بخير والحمد لله."))
    assert [cue["text"] for cue in cues] == ["كيف حالك اليوم؟", "أنا بخير والحمد لله."]


def test_a_long_arabic_sentence_splits_after_its_comma():
    text = (
        "تغادر الحافلات المحطة الداخلية بين المناطق في خلال اليوم كله، "
        "على الرغم من أن معظمها يتجه إلى الشرق بين السادسة والسابعة والنصف صباحا."
    )
    cues = build_plain_cues(_words(text))
    assert cues[0]["text"].endswith("كله،"), [cue["text"] for cue in cues]


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("Τι κάνεις; Είμαι καλά.", ["Τι κάνεις;", "Είμαι καλά."]),
        ("كيف حالك؟ أنا بخير.", ["كيف حالك؟", "أنا بخير."]),
        ("Wait here; then go.", ["Wait here; then go."]),
    ],
)
def test_resplit_splits_greek_and_arabic_sentences(text, expected):
    cue = {"text": text, "start_s": 0.0, "end_s": 3.0}
    assert [piece["text"] for piece in resplit_cues_into_sentences([cue])] == expected


def test_guided_sentence_groups_close_on_greek_and_arabic_question_marks():
    from app.pipeline.guided_caption_presentation import _sentence_groups

    rows = [
        {"text": token, "start_s": i * 0.4, "end_s": i * 0.4 + 0.3}
        for i, token in enumerate("Τι κάνεις; كيف حالك؟ Fine; ok.".split())
    ]
    groups = [" ".join(row["text"] for row in group) for group in _sentence_groups(rows)]
    assert groups == ["Τι κάνεις;", "كيف حالك؟", "Fine; ok."]
