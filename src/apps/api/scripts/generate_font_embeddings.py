"""Precompute CLIP embeddings for every registry font + admin-UI preview PNGs.

One-time script. Run after any change to font-registry.json or after bumping
the CLIP model. Writes:

  assets/fonts/registry-embeddings.npz
    - families:   shape (N,), dtype object (str)
    - embeddings: shape (N, D), dtype float32, L2-normalized
    - meta:       JSON string with {model_id, model_version,
                                    registry_sha256, generated_at}

  src/apps/web/public/font-previews/<family-slug>.png
    - 600×120 PNG of the family name rendered in the family itself, used by
      the admin FontAlternatives tile picker. Slug = family.lower().replace(' ', '-').

Usage (from worktree root):

  cd src/apps/api
  python scripts/generate_font_embeddings.py

The script depends on torch + open-clip-torch + Pillow + structlog being
installed in the active env (structlog is pulled in transitively because the
script imports `app.services.clip_font_matcher`, which logs via structlog).
In CI/prod the deps are present (Dockerfile installs torch CPU + open-clip at
build time, structlog is a top-level dep). Local devs not running the font
pipeline can skip; the precomputed .npz is committed to the repo and is the
artifact that runtime actually consumes.
"""

from __future__ import annotations

import argparse
import datetime as _dt
import hashlib
import io
import json
import os
import sys
from pathlib import Path

# Make `app.*` imports resolve when invoked as `python scripts/foo.py` from
# src/apps/api/. Matches the pattern in backfill_dimples_passport_inputs.py and
# export_clip_metadata_fixtures.py — sys.path[0] after `python scripts/foo.py`
# is the scripts/ directory, so we add the api root (its parent) explicitly.
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import numpy as np  # noqa: E402
from PIL import Image, ImageDraw, ImageFont  # noqa: E402

# ── Paths ────────────────────────────────────────────────────────────────────

_API_ROOT = Path(__file__).resolve().parents[1]
_REGISTRY_JSON = _API_ROOT / "assets" / "fonts" / "font-registry.json"
_FONT_DIR = _API_ROOT / "assets" / "fonts"
_EMBEDDINGS_OUT = _API_ROOT / "assets" / "fonts" / "registry-embeddings.npz"

# The web app's public dir is two levels up from src/apps/api.
_WEB_PUBLIC = _API_ROOT.parent / "web" / "public" / "font-previews"


# ── Render a font name in its own face → PNG bytes ───────────────────────────


def render_sample(family: str, ttf_path: Path, *, size: int = 96) -> bytes:
    """Render `family` text in its own TTF face at `size` px → PNG bytes.

    The sample text being the family name is intentional: at admin-UI scale
    the user reads the family name (so the preview is useful) and the CLIP
    embedding sees real glyphs from the face (so the registry embedding
    encodes the face, not a synthetic prompt). 600×120 fits the largest
    family name comfortably and is the tile size we render at in the UI.
    """
    img = Image.new("RGB", (600, 120), color=(255, 255, 255))
    draw = ImageDraw.Draw(img)
    try:
        font = ImageFont.truetype(str(ttf_path), size=size)
    except OSError as exc:
        raise RuntimeError(f"could not load TTF {ttf_path}: {exc}") from exc
    # Center the text. textbbox returns (left, top, right, bottom).
    bbox = draw.textbbox((0, 0), family, font=font)
    text_w = bbox[2] - bbox[0]
    text_h = bbox[3] - bbox[1]
    x = (600 - text_w) // 2 - bbox[0]
    y = (120 - text_h) // 2 - bbox[1]
    draw.text((x, y), family, fill=(20, 20, 20), font=font)
    out = io.BytesIO()
    img.save(out, format="PNG")
    return out.getvalue()


def slugify(family: str) -> str:
    return family.lower().replace(" ", "-")


# ── Main ─────────────────────────────────────────────────────────────────────


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--skip-previews",
        action="store_true",
        help="Skip writing preview PNGs to web/public/font-previews/",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Render previews + compute embeddings but write nothing",
    )
    args = parser.parse_args()

    # Heavy imports gated behind argparse so `--help` works without torch.
    import open_clip  # noqa: PLC0415
    import torch  # noqa: PLC0415

    from app.services.clip_font_matcher import (  # noqa: PLC0415
        MODEL_ARCHITECTURE,
        MODEL_ID,
        MODEL_PRETRAINED,
        MODEL_VERSION,
    )

    registry = json.loads(_REGISTRY_JSON.read_text())["fonts"]
    print(f"Loaded {len(registry)} fonts from registry.")

    # Sanity: every key must reference a file that exists on disk.
    for family, entry in registry.items():
        ttf = _FONT_DIR / entry["file"]
        if not ttf.exists():
            print(f"ERROR: {family} → {ttf} missing", file=sys.stderr)
            return 1

    print(f"Loading open-clip {MODEL_ARCHITECTURE}/{MODEL_PRETRAINED}...")
    model, _train_pre, preprocess = open_clip.create_model_and_transforms(
        MODEL_ARCHITECTURE,
        pretrained=MODEL_PRETRAINED,
        device="cpu",
    )
    model.eval()

    families: list[str] = []
    embeddings: list[np.ndarray] = []

    if not args.skip_previews and not args.dry_run:
        _WEB_PUBLIC.mkdir(parents=True, exist_ok=True)

    for family, entry in registry.items():
        ttf = _FONT_DIR / entry["file"]
        png = render_sample(family, ttf)

        # Embed via the same path runtime uses.
        img = Image.open(io.BytesIO(png)).convert("RGB")
        tensor = preprocess(img).unsqueeze(0)
        with torch.no_grad():
            feat = model.encode_image(tensor)
        feat = feat / feat.norm(dim=-1, keepdim=True)
        embeddings.append(feat.squeeze(0).numpy().astype(np.float32))
        families.append(family)
        print(f"  embedded {family} ({entry['file']})")

        if not args.skip_previews and not args.dry_run:
            (_WEB_PUBLIC / f"{slugify(family)}.png").write_bytes(png)

    embeddings_arr = np.stack(embeddings, axis=0)
    families_arr = np.array(families, dtype=object)

    registry_sha = hashlib.sha256(_REGISTRY_JSON.read_bytes()).hexdigest()
    meta = {
        "model_id": MODEL_ID,
        "model_version": MODEL_VERSION,
        "registry_sha256": registry_sha,
        "generated_at": _dt.datetime.now(tz=_dt.UTC).isoformat(),
    }
    print(f"Embeddings shape: {embeddings_arr.shape}  (families × dim)")
    print(f"meta: {meta}")

    if args.dry_run:
        print("--dry-run: skipping disk writes")
        return 0

    np.savez(
        _EMBEDDINGS_OUT,
        families=families_arr,
        embeddings=embeddings_arr,
        meta=np.array(json.dumps(meta), dtype=object),
    )
    print(f"Wrote {_EMBEDDINGS_OUT}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
