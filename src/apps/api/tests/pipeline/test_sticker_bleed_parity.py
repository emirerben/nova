"""KRI-1616: a transparent sticker hanging past the frame edge draws the same on the cloud card
pass and the phone engine.

The cloud half runs the real `build_media_overlay_command` (alpha kept, as with
``MEDIA_OVERLAY_ALPHA_ENABLED``) through ffmpeg over a flat base and measures each sticker: the
box of its ink, how much of its card box it covers (a star covers under half; a sticker
flattened to an opaque box covers all of it) and its mean colour. The numbers are the fixture
tests/fixtures/sticker-bleed-parity/v1.json, which `StickerBleedParityTests` (KriaMediaEngine)
renders through the phone compositor and compares. ``KRI1616_REGENERATE=1`` rewrites the fixture
(and the sticker PNG); otherwise this test re-renders and must match it.

Failure modes: the sticker's alpha flattens into a box (the negative control renders the cloud's
JPEG path and must be caught), the sticker moves or changes size between renderers, the part past
the frame is drawn (both cut it at the canvas edge), or the fixture drifts from the cloud command.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
from pathlib import Path

import pytest

from app.agents._schemas.media_overlay import MediaOverlay
from app.pipeline.image_clip import normalize_to_jpeg
from app.pipeline.media_overlay import build_media_overlay_command

pytestmark = pytest.mark.skipif(shutil.which("ffmpeg") is None, reason="needs ffmpeg")

ROOT = Path(__file__).resolve().parents[5] / "tests/fixtures/sticker-bleed-parity"
FIXTURE = ROOT / "v1.json"
STICKER = ROOT / "sticker.png"
WIDTH, HEIGHT = 1080, 1920
BASE_RGB = (40, 90, 160)
INK_DIFF = 40
FRAME_AT_S = 0.5
# One sticker 0.25 of its width past the left edge (a sticker's home), one 0.35 past the right
# (the most the offer hangs): centres inside [0, 1], as every offer card is.
CARDS = [
    {"id": "intro-card-sticker-1", "scale": 0.30, "x_frac": 0.15 - 0.25 * 0.30, "y_frac": 0.25},
    {"id": "intro-card-sticker-2", "scale": 0.22, "x_frac": 0.89 + 0.35 * 0.22, "y_frac": 0.62},
]


def _draw_sticker(path: Path) -> None:
    from PIL import Image, ImageDraw

    big = Image.new("RGBA", (1200, 1200), (0, 0, 0, 0))
    points = [
        (600, 60), (750, 450), (1140, 480), (830, 720), (930, 1140),
        (600, 900), (270, 1140), (370, 720), (60, 480), (450, 450),
    ]  # fmt: skip
    ImageDraw.Draw(big).polygon(points, fill=(246, 200, 40, 255))
    big.resize((300, 300), Image.LANCZOS).save(path, "PNG")


def _card_box_px(card: dict, aspect: float = 1.0) -> tuple[float, float, float, float]:
    width = card["scale"] * WIDTH
    height = width / aspect
    cx, cy = card["x_frac"] * WIDTH, card["y_frac"] * HEIGHT
    return cx - width / 2, cy - height / 2, cx + width / 2, cy + height / 2


def measure(pixels, width: int, height: int) -> list[dict]:
    """What both halves compare: per sticker, its ink box in canvas pixels (clipped to the
    frame), the share of its clipped card box that is ink, and the ink's mean colour."""
    out = []
    for card in CARDS:
        left, top, right, bottom = _card_box_px(card)
        x0, y0 = max(0, int(left) - 6), max(0, int(top) - 6)
        x1, y1 = min(width, int(right) + 7), min(height, int(bottom) + 7)
        ink, sums, box = 0, [0, 0, 0], [x1, y1, -1, -1]
        for y in range(y0, y1):
            for x in range(x0, x1):
                r, g, b = pixels[x, y][:3]
                if max(abs(r - BASE_RGB[0]), abs(g - BASE_RGB[1]), abs(b - BASE_RGB[2])) > INK_DIFF:
                    ink += 1
                    sums = [sums[0] + r, sums[1] + g, sums[2] + b]
                    box = [min(box[0], x), min(box[1], y), max(box[2], x), max(box[3], y)]
        clipped = (min(right, width) - max(left, 0)) * (min(bottom, height) - max(top, 0))
        out.append(
            {
                "id": card["id"],
                "ink_box": box,
                "coverage": round(ink / clipped, 4),
                "mean_rgb": [round(s / max(ink, 1), 1) for s in sums],
            }
        )
    return out


def render_cloud(tmp: Path, *, alpha: bool) -> list[dict]:
    from PIL import Image

    base = tmp / "base.mp4"
    subprocess.run(
        ["ffmpeg", "-y", "-loglevel", "error", "-f", "lavfi", "-i",
         f"color=c=0x{''.join(f'{c:02x}' for c in BASE_RGB)}:s={WIDTH}x{HEIGHT}:r=30:d=1",
         "-pix_fmt", "yuv420p", str(base)],
        check=True,
    )  # fmt: skip
    local = str(STICKER)
    if not alpha:
        local = str(tmp / "sticker.jpg")
        normalize_to_jpeg(str(STICKER), local)
    cards = [
        MediaOverlay.model_validate(
            {**card, "kind": "image", "src_gcs_path": f"users/u/{card['id']}.png",
             "display_mode": "pip", "position": "custom", "start_s": 0.0, "end_s": 1.0, "z": n}
        )
        for n, card in enumerate(CARDS)
    ]  # fmt: skip
    out = tmp / "out.mp4"
    command = build_media_overlay_command(
        str(base),
        cards,
        [local] * len(cards),
        [card.card_width_px() for card in cards],
        str(out),
        card_has_alpha=[alpha] * len(cards),
    )
    subprocess.run(command, check=True, capture_output=True)
    frame = tmp / "frame.png"
    subprocess.run(
        ["ffmpeg", "-y", "-loglevel", "error", "-ss", str(FRAME_AT_S), "-i", str(out),
         "-frames:v", "1", str(frame)],
        check=True,
    )  # fmt: skip
    with Image.open(frame) as image:
        rgb = image.convert("RGB")
        return measure(rgb.load(), *rgb.size)


def _assert_same_drawing(recorded: list[dict], measured: list[dict]) -> None:
    """The ffmpeg build and its scaler differ a little between machines (a macOS run and CI's Linux
    differ by 0.3 per colour channel and 1e-4 coverage), so the fixture pins what must not move:
    which card, where it is cut, and roughly how much of its box is ink and in which colour."""
    assert [c["id"] for c in recorded] == [c["id"] for c in measured]
    for want, got in zip(recorded, measured, strict=True):
        assert all(abs(a - b) <= 2 for a, b in zip(want["ink_box"], got["ink_box"], strict=True)), (
            want,
            got,
        )
        assert abs(want["coverage"] - got["coverage"]) <= 0.01, (want, got)
        assert all(
            abs(a - b) <= 3 for a, b in zip(want["mean_rgb"], got["mean_rgb"], strict=True)
        ), (
            want,
            got,
        )


def test_the_cloud_draws_a_bled_sticker_with_its_alpha_as_the_fixture_records(tmp_path):
    regenerate = os.environ.get("KRI1616_REGENERATE") == "1"
    if regenerate:
        ROOT.mkdir(parents=True, exist_ok=True)
        _draw_sticker(STICKER)
    measured = render_cloud(tmp_path, alpha=True)
    for sticker in measured:
        assert 0.25 < sticker["coverage"] < 0.6, sticker  # a star, not a box
    left, right = measured
    assert left["ink_box"][0] == 0, "the sticker past the left edge is cut by the frame"
    assert right["ink_box"][2] == WIDTH - 1, "the sticker past the right edge is cut by the frame"
    record = {
        "comment": __doc__.split("\n\n")[0],
        "canvas": [WIDTH, HEIGHT],
        "base_rgb": list(BASE_RGB),
        "ink_diff": INK_DIFF,
        "frame_at_s": FRAME_AT_S,
        "cards": CARDS,
        "cloud": measured,
    }
    if regenerate:
        FIXTURE.write_text(json.dumps(record, indent=2) + "\n")
    recorded = json.loads(FIXTURE.read_text())
    assert {k: v for k, v in recorded.items() if k != "cloud"} == {
        k: v for k, v in record.items() if k != "cloud"
    }
    _assert_same_drawing(recorded["cloud"], measured)


def test_a_sticker_flattened_to_a_box_is_caught(tmp_path):
    for sticker in render_cloud(tmp_path, alpha=False):
        assert sticker["coverage"] > 0.9, sticker
