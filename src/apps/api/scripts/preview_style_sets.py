"""Render a visual comparison grid of every style set through the REAL renderer.

For each set in assets/style_sets/style-sets.json this resolves a representative
role via `resolve_overlay_style`, runs the same `apply_overlay_constraints`
universal pass that ships, and renders the overlay layer with the production
`render_overlays_at_time` (pixel-identical to the exported Pillow overlay layer).
Each set tile shows a HERO sample + a deliberately-LONG label so the
shrink-to-fit constraint is visible. Writes PNGs + an index.html grid.

Run:  python scripts/preview_style_sets.py
Output: .devtest/preview/  (open index.html)
No DB / Gemini / Docker needed — just fonts + Pillow.
"""

from __future__ import annotations

import os

from PIL import Image, ImageDraw

from app.pipeline.overlay_constraints import apply_overlay_constraints
from app.pipeline.style_sets import (
    _STYLE_SETS_DATA,
    get_style_set,
    resolve_overlay_style,
)
from app.pipeline.text_overlay import CANVAS_H, CANVAS_W, render_overlays_at_time

OUT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..", "..", ".devtest", "preview"))

# Showcase role + sample copy per set. `long` exercises the constraint pass.
SHOWCASE: dict[str, dict] = {
    "default": {"role": "hook", "short": "YOUR HOOK HERE", "long": "Antananarivo Madagascar"},
    "lyric_karaoke_bold": {"role": "lyric_karaoke", "short": "city lights call my name", "long": "we were dancing all night under neon skies forever"},
    "lyric_line_calm": {"role": "lyric_line", "short": "and I think to myself", "long": "what a wonderful world it is when the morning comes around"},
    "lyric_word_pop_punchy": {"role": "lyric_word_pop", "short": "LET'S GO", "long": "every single moment counts so make it loud right now"},
    "travel_editorial": {"role": "label", "short": "BARCELONA", "long": "ANTANANARIVO MADAGASCAR"},
    "lifestyle_clean": {"role": "hook", "short": "morning routine", "long": "the little things that make a big difference"},
}

# A few backgrounds so white/gold text is legible against varied content.
BGS = [(18, 18, 22), (40, 28, 60), (20, 45, 50)]


def _gradient_bg(idx: int) -> Image.Image:
    top = BGS[idx % len(BGS)]
    img = Image.new("RGB", (CANVAS_W, CANVAS_H), top)
    d = ImageDraw.Draw(img)
    for y in range(CANVAS_H):
        f = y / CANVAS_H
        d.line([(0, y), (CANVAS_W, y)], fill=tuple(int(c * (1 - 0.45 * f)) for c in top))
    return img


def _overlay(set_id: str, role: str, text: str, y_frac: float) -> dict:
    style = resolve_overlay_style(set_id, role)
    ov = {"text": text, "start_s": 0.0, "end_s": 3.0, "position_y_frac": y_frac}
    ov.update(style)
    ov.pop("timing", None)  # render-time timing not needed for a static frame
    # stroke_width -> outline_px alias the Pillow draw path reads
    if "stroke_width" in ov:
        ov["outline_px"] = ov["stroke_width"]
    return ov


def main() -> None:
    os.makedirs(OUT, exist_ok=True)
    tiles: list[tuple[str, str]] = []  # (set_id, png filename)

    for i, s in enumerate(_STYLE_SETS_DATA["sets"]):
        sid = s["id"]
        spec = SHOWCASE.get(sid)
        if not spec:
            continue
        overlays = [
            _overlay(sid, spec["role"], spec["short"], 0.30),
            _overlay(sid, spec["role"], spec["long"], 0.62),
        ]
        # The universal constraint pass — exactly what ships.
        overlays = apply_overlay_constraints(overlays)

        layer_png = os.path.join(OUT, f"_layer_{sid}.png")
        render_overlays_at_time(overlays, 3.0, 1.5, layer_png)

        bg = _gradient_bg(i).convert("RGBA")
        layer = Image.open(layer_png).convert("RGBA")
        bg.alpha_composite(layer)
        # Thumbnail for the grid (keep 9:16).
        thumb = bg.convert("RGB").resize((CANVAS_W // 3, CANVAS_H // 3))
        fname = f"{sid}.png"
        thumb.save(os.path.join(OUT, fname))
        os.remove(layer_png)
        tiles.append((sid, fname))
        print(f"rendered {sid}: short={spec['short']!r} long={spec['long']!r}")

    # HTML grid
    cards = []
    for sid, fname in tiles:
        st = get_style_set(sid)
        cards.append(
            f'<figure><img src="{fname}" width="360"/>'
            f"<figcaption><b>{sid}</b><br><span>{st.get('label','')}</span><br>"
            f"<small>{', '.join(st.get('tags', []))}</small><br>"
            f"<small>applies_to: {', '.join(st['applies_to'])}</small></figcaption></figure>"
        )
    html = (
        "<!doctype html><meta charset=utf-8><title>Nova style sets</title>"
        "<style>body{background:#0d0d0f;color:#eee;font:14px/1.4 -apple-system,sans-serif;margin:24px}"
        "h1{font-weight:600}.grid{display:flex;flex-wrap:wrap;gap:24px}"
        "figure{margin:0;background:#1a1a1f;border-radius:12px;padding:12px;width:360px}"
        "img{border-radius:8px;display:block}figcaption{margin-top:8px}"
        "span{color:#bbb}small{color:#888}</style>"
        f"<h1>Nova style sets — {_STYLE_SETS_DATA['version']}</h1>"
        "<p>Each tile: HERO sample (top) + deliberately-long label (bottom) run through "
        "the real renderer + universal constraint pass.</p>"
        f'<div class="grid">{"".join(cards)}</div>'
    )
    with open(os.path.join(OUT, "index.html"), "w") as f:
        f.write(html)
    print(f"\nGrid: {os.path.join(OUT, 'index.html')}")


if __name__ == "__main__":
    main()
