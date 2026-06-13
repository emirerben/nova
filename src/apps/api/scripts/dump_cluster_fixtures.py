"""Dump ground-truth fixtures for the TS editorial-cluster parity test.

Runs intro_cluster.py's EDITORIAL_STYLE path (the layout generative/plan
``intro_layout == "cluster"`` variants render) over representative hooks and
emits the per-block geometry plus the exact Skia (family, text, px) ->
width/height table the TS port re-uses. Output is the committed fixture consumed
by ``src/apps/web/src/__tests__/lib/overlay-cluster-layout.test.ts``.

Regenerate when intro_cluster.py geometry changes::

    cd src/apps/api && PYTHONPATH=$PWD \\
      .venv-test/bin/python scripts/dump_cluster_fixtures.py \\
      > ../web/src/__tests__/lib/fixtures/cluster-layout-fixtures.json
"""

import json

import skia

from app.pipeline.intro_cluster import (
    EDITORIAL_STYLE,
    _derive_word_roles_with_guarantees,
    _group_styled_blocks,
    _styled_role_px,
    compute_cluster_blocks,
    normalize_typography,
)
from app.pipeline.text_overlay_skia import _typeface_for_overlay

CANVAS_W = 1080
CANVAS_H = 1920
EDGE = 0.06
YMIN, YMAX = 0.15, 0.85
MIN_SCALE = 0.55
REVEAL, BASE = 3.0, 60

HERO_FONT = EDITORIAL_STYLE["hero_font"]
BODY = EDITORIAL_STYLE["body_font"]
ACCENT = EDITORIAL_STYLE["accent_font"]

HOOKS = [
    "what is your favorite place",
    "the days we lost found us",
    "I never wanted to leave",
    "before the sun comes up",
    "she said goodbye",
    "we drove all night long",
    "good morning beautiful world",
    "this is how it ends",
    "lost in the city lights",
    "you are my home",
    "running through golden fields",
    "the place where dreams begin",
    "where did the summer go?",
    "stop. breathe. you made it",
    "incomprehensibly extraordinary serendipitous discoveries",
    "go go go go go go",
    "tiny",  # 1 word -> valid
    "this hook is way too long for an editorial cluster intro layout",  # 11 words -> null
]

_tf_cache: dict = {}


def tf(family):
    if family not in _tf_cache:
        _tf_cache[family] = _typeface_for_overlay({"font_family": family})
    return _tf_cache[family]


def measure(family, text, px):
    font = skia.Font(tf(family), px)
    font.setSubpixel(True)
    metrics = font.getMetrics()
    return {"wPx": font.measureText(text), "hPx": metrics.fDescent - metrics.fAscent}


def faces_for(blocks, accent_parity=0):
    out = []
    k = 0
    for block in blocks:
        if block["role"] == "hero":
            out.append(HERO_FONT)
        else:
            out.append(ACCENT if (k + accent_parity) % 2 == 1 else BODY)
            k += 1
    return out


cases: list = []
table: dict = {}


def remember(family, text, px):
    key = f"{family}|||{text}|||{px}"
    if key not in table:
        table[key] = measure(family, text, px)


def px_key(block, family, scale):
    px = _styled_role_px(block["role"], BASE, scale, EDITORIAL_STYLE)
    return f"{family}|||{block['text']}|||{px}"


for hook in HOOKS:
    blocks = compute_cluster_blocks(
        hook,
        word_roles=None,
        base_size_px=BASE,
        font_family=None,
        reveal_window_s=REVEAL,
        style=EDITORIAL_STYLE,
    )
    cases.append(
        {"hook": hook, "base_size_px": BASE, "reveal_window_s": REVEAL, "blocks": blocks}
    )
    # Replay the px ladder to record the measures the TS port needs (only for the
    # non-null shapes that reach the measure stage: word count in [1, 6]).
    words = hook.split()
    if not (1 <= len(words) <= 6):
        continue
    roles, _ = _derive_word_roles_with_guarantees(words)
    grp = _group_styled_blocks(words, list(roles), EDITORIAL_STYLE)
    if not grp:
        continue
    for block in grp:
        block["text"] = normalize_typography(block["text"])
    faces = faces_for(grp)
    scale = 1.0
    for _ in range(40):
        for block, family in zip(grp, faces):
            px = _styled_role_px(block["role"], BASE, scale, EDITORIAL_STYLE)
            remember(family, block["text"], px)
        widest = max(
            table[px_key(block, family, scale)]["wPx"] / CANVAS_W
            for block, family in zip(grp, faces)
        )
        heights = [
            table[px_key(block, family, scale)]["hPx"] / CANVAS_H
            for block, family in zip(grp, faces)
        ]
        ch = heights[0] / 2 + heights[-1] / 2
        for i in range(1, len(heights)):
            ch += (heights[i - 1] + heights[i]) / 2 * EDITORIAL_STYLE["cascade_y_step_ratio"]
        usable_w = 1 - 2 * EDGE
        margin = EDITORIAL_STYLE["scene_shift_margin"]
        usable_h = (YMAX - margin) - (YMIN + margin)
        if widest <= usable_w and ch <= usable_h:
            break
        scale *= 0.92
        if scale < MIN_SCALE:
            break

print(json.dumps({"cases": cases, "measure_table": table}, indent=1))
