"""Font identification — match burned-in reference-video text to registry fonts.

Pipeline (per overlay with a text_bbox):

    recipe.text_bbox                  reference video file
            │                                  │
            └──── sample_frame_t ──────────────┤
                                               ▼
                              FFmpeg single-frame extract
                                               │
                              PNG frame at sample_frame_t
                                               ▼
                              Pillow crop (center+wh norm → pixel TL+WH)
                                               │
                              cropped glyph region (PNG bytes)
                                               ▼
                              FontMatcher.embed_image (CLIP image tower)
                                               │
                              query embedding (np.ndarray)
                                               ▼
                              FontMatcher.rank_registry (cosine vs .npz)
                                               │
                              [FontMatch(family, similarity), ...]
                                               ▼
                              overlay.font_alternatives ← top-N

After all overlays:

    [overlay₁.font_alternatives, overlay₂.font_alternatives, ...]
                                  │
                                  ▼
              _aggregate_font_default(weight=similarity × duration)
                                  │
                                  ▼
                          recipe.font_default

The matcher implementation is swappable via the FontMatcher Protocol. Vanilla
open-clip ViT-B/32 ships in v1; FontCLIP (arXiv 2403.06453) is a future swap.
"""

from __future__ import annotations

import io
import json
import os
import subprocess
from dataclasses import dataclass
from typing import Any, Protocol

import numpy as np
import structlog
from PIL import Image

log = structlog.get_logger()


# ── Tunables ─────────────────────────────────────────────────────────────────

# Minimum cosine similarity for a match to count toward font_default voting.
# Below this, the matcher is signalling "I don't know" and we ignore the
# overlay rather than poisoning the template-level vote.
_SIMILARITY_FLOOR = 0.50

# Top-N alternatives returned per overlay. 5 is the largest the admin UI can
# show as tiles without scrolling.
_TOP_N = 5

# How many top-match families per overlay vote into font_default aggregation.
# 3 rewards consistent runner-ups across overlays (signal of the dominant
# family even when no single overlay is a perfect match).
_VOTE_DEPTH = 3


# ── Public types ─────────────────────────────────────────────────────────────


@dataclass(frozen=True)
class FontMatch:
    family: str
    similarity: float  # cosine [0, 1]


class FontMatcher(Protocol):
    """Protocol for any CLIP-style image-to-font embedder + ranker.

    Implementations: app.services.clip_font_matcher.ClipFontMatcher (v1).
    Future: FontCLIP swap behind the same Protocol.
    """

    def embed_image(self, image_bytes: bytes) -> np.ndarray: ...

    def rank_registry(
        self, query_embedding: np.ndarray, top_n: int = _TOP_N
    ) -> list[FontMatch]: ...


# ── Frame extract + crop ─────────────────────────────────────────────────────


def extract_frame_png(video_path: str, t_s: float) -> bytes:
    """Extract a single PNG frame at `t_s` seconds from `video_path`.

    -ss before -i is fast (input seek), accurate enough at frame boundaries for
    static text overlays. -frames:v 1 grabs exactly one frame. Output goes to
    stdout as a PNG byte stream so we don't touch disk.
    """
    cmd = [
        "ffmpeg",
        "-ss",
        f"{t_s:.3f}",
        "-i",
        video_path,
        "-frames:v",
        "1",
        "-f",
        "image2pipe",
        "-vcodec",
        "png",
        "-loglevel",
        "error",
        "pipe:1",
    ]
    proc = subprocess.run(cmd, capture_output=True, timeout=30, check=False)
    if proc.returncode != 0 or not proc.stdout:
        raise FontIdExtractError(
            f"ffmpeg frame-extract failed at t={t_s}s: rc={proc.returncode}, "
            f"stderr={proc.stderr.decode('utf-8', errors='replace')[:200]}"
        )
    return proc.stdout


def crop_bbox(frame_png: bytes, bbox: dict[str, float]) -> bytes:
    """Crop the bbox region out of a PNG-encoded frame, return cropped PNG.

    bbox uses normalized center+wh coords (PR1 spec). Convert to pixel
    top-left + width/height, clamp to frame, crop with Pillow, re-encode PNG.
    """
    img = Image.open(io.BytesIO(frame_png)).convert("RGB")
    fw, fh = img.size

    # Center → top-left. PR1 validation guarantees these stay in [0, 1] and the
    # bbox doesn't overflow the frame, but we still clamp defensively in case
    # of float-rounding drift at the edges.
    cx, cy = bbox["x_norm"], bbox["y_norm"]
    bw, bh = bbox["w_norm"], bbox["h_norm"]
    left = max(0, int(round((cx - bw / 2.0) * fw)))
    top = max(0, int(round((cy - bh / 2.0) * fh)))
    right = min(fw, int(round((cx + bw / 2.0) * fw)))
    bottom = min(fh, int(round((cy + bh / 2.0) * fh)))

    if right <= left or bottom <= top:
        raise FontIdExtractError(
            f"bbox cropped to empty region: bbox={bbox} frame={fw}x{fh} "
            f"→ pixel box ({left},{top},{right},{bottom})"
        )

    cropped = img.crop((left, top, right, bottom))
    out = io.BytesIO()
    cropped.save(out, format="PNG")
    return out.getvalue()


# ── Aggregation: per-overlay matches → template-level font_default ───────────


def _aggregate_font_default(per_overlay: list[tuple[list[FontMatch], float]]) -> str:
    """Aggregate per-overlay top-N matches into a single template default.

    Args:
        per_overlay: list of (matches, overlay_duration_s) tuples. Pass an
                     empty matches list for overlays that were skipped (null
                     bbox, low similarity, extract error).

    Algorithm: weighted vote.
        weight(overlay, family) = top_match.similarity × overlay_duration_s
        per_family_score[family] = Σ weight for overlays where family is in
                                    top-`_VOTE_DEPTH`
        font_default = argmax(per_family_score)

    Why duration: a 6s subject label dominates a 1.5s incidental caption.
    Why top-3: consistent runner-up matches across overlays still signal the
    dominant family even when no single overlay is a perfect match.

    Returns "" when no overlay produced an above-floor match.
    """
    scores: dict[str, float] = {}
    for matches, duration_s in per_overlay:
        if not matches:
            continue
        top = matches[0]
        if top.similarity < _SIMILARITY_FLOOR:
            continue
        weight = float(top.similarity) * float(max(duration_s, 0.0))
        for m in matches[:_VOTE_DEPTH]:
            scores[m.family] = scores.get(m.family, 0.0) + weight
    if not scores:
        return ""
    return max(scores.items(), key=lambda kv: kv[1])[0]


# ── Orchestrator: drive the whole pipeline against a recipe + video ──────────


def identify_fonts(
    recipe: Any,
    video_path: str,
    matcher: FontMatcher,
    *,
    top_n: int = _TOP_N,
) -> None:
    """Walk a TemplateRecipe in-place, populate font_alternatives + font_default.

    Mutates `recipe.slots[*]["text_overlays"][*]["font_alternatives"]` and
    `recipe.font_default`. Overlays without `text_bbox` are skipped (their
    font_alternatives stays absent — frontend treats absent and null
    equivalently).

    Errors during a single overlay's extract/crop/embed are logged and the
    overlay is skipped, never raised. Font identification is a non-critical
    enrichment; a failure here must not abort template processing.
    """
    per_overlay: list[tuple[list[FontMatch], float]] = []

    for slot in getattr(recipe, "slots", []):
        for overlay in slot.get("text_overlays", []):
            bbox = overlay.get("text_bbox")
            if not bbox:
                continue

            duration_s = float(overlay.get("end_s", 0.0)) - float(overlay.get("start_s", 0.0))
            try:
                frame = extract_frame_png(video_path, float(bbox["sample_frame_t"]))
                crop = crop_bbox(frame, bbox)
                embedding = matcher.embed_image(crop)
                matches = matcher.rank_registry(embedding, top_n=top_n)
            except Exception as exc:  # noqa: BLE001
                log.warning(
                    "font_identification_overlay_failed",
                    error=str(exc),
                    sample_frame_t=bbox.get("sample_frame_t"),
                )
                continue

            overlay["font_alternatives"] = [
                {"family": m.family, "similarity": round(float(m.similarity), 4)} for m in matches
            ]
            per_overlay.append((matches, duration_s))

    aggregated = _aggregate_font_default(per_overlay)
    if aggregated:
        recipe.font_default = aggregated
        log.info(
            "font_identification_complete",
            font_default=aggregated,
            overlays_matched=len(per_overlay),
        )
    else:
        log.info("font_identification_no_match", overlays_seen=len(per_overlay))


# ── Registry embeddings loader ───────────────────────────────────────────────


_REGISTRY_EMBEDDINGS_PATH = os.path.join(
    os.path.dirname(__file__), "..", "..", "assets", "fonts", "registry-embeddings.npz"
)


@dataclass(frozen=True)
class RegistryEmbeddings:
    """Loaded registry embeddings + sidecar metadata.

    The .npz file MUST carry these sidecar arrays so the loader can refuse
    artifacts produced by a different model version, preventing silent
    semantic drift when a contributor regenerates with a bumped open-clip.
    """

    families: np.ndarray  # shape (N,), dtype object (str)
    embeddings: np.ndarray  # shape (N, D), dtype float32, L2-normalized
    model_id: str
    model_version: str
    registry_sha256: str
    generated_at: str


class FontIdLoadError(RuntimeError):
    pass


class FontIdExtractError(RuntimeError):
    pass


def load_registry_embeddings(
    expected_model_id: str,
    expected_model_version: str,
    path: str = _REGISTRY_EMBEDDINGS_PATH,
) -> RegistryEmbeddings:
    """Load the precomputed registry .npz and verify sidecar matches runtime.

    Raises FontIdLoadError if the artifact is missing, malformed, or was
    produced by a different model_id/version than the matcher in use. The
    matcher is expected to call this once at first use and cache the result.
    """
    if not os.path.exists(path):
        raise FontIdLoadError(
            f"registry embeddings not found at {path}. Run "
            "`python scripts/generate_font_embeddings.py` to produce it."
        )
    try:
        data = np.load(path, allow_pickle=True)
        families = data["families"]
        embeddings = data["embeddings"]
        meta = json.loads(str(data["meta"]))
    except Exception as exc:  # noqa: BLE001
        raise FontIdLoadError(f"failed to parse {path}: {exc}") from exc

    if meta.get("model_id") != expected_model_id:
        raise FontIdLoadError(
            f"registry embeddings model_id mismatch: artifact={meta.get('model_id')!r}, "
            f"runtime={expected_model_id!r}. Regenerate with "
            "`python scripts/generate_font_embeddings.py`."
        )
    if meta.get("model_version") != expected_model_version:
        raise FontIdLoadError(
            f"registry embeddings model_version mismatch: "
            f"artifact={meta.get('model_version')!r}, runtime={expected_model_version!r}. "
            "Regenerate the artifact."
        )

    return RegistryEmbeddings(
        families=families,
        embeddings=embeddings.astype(np.float32, copy=False),
        model_id=meta["model_id"],
        model_version=meta["model_version"],
        registry_sha256=meta.get("registry_sha256", ""),
        generated_at=meta.get("generated_at", ""),
    )


def cosine_rank(
    query: np.ndarray,
    registry: RegistryEmbeddings,
    top_n: int,
) -> list[FontMatch]:
    """Cosine similarity between query and registry; return top-N descending.

    Both `query` and `registry.embeddings` are expected to be L2-normalized;
    cosine reduces to a single dot product. Defensive re-normalization on the
    query keeps the math correct even if a caller forgets.
    """
    q = query.astype(np.float32, copy=False)
    norm = np.linalg.norm(q)
    if norm > 0:
        q = q / norm
    sims = registry.embeddings @ q
    order = np.argsort(-sims)[:top_n]
    return [FontMatch(family=str(registry.families[i]), similarity=float(sims[i])) for i in order]


# ── Public re-exports for app.services.clip_font_matcher ─────────────────────

__all__ = [
    "FontMatch",
    "FontMatcher",
    "FontIdLoadError",
    "FontIdExtractError",
    "RegistryEmbeddings",
    "extract_frame_png",
    "crop_bbox",
    "identify_fonts",
    "load_registry_embeddings",
    "cosine_rank",
]
