"""open-clip ViT-B/32 implementation of FontMatcher.

Lazy singleton — model loads once per worker process, stays resident across
Celery tasks. Image-tower-only inference: text encoder weights are loaded
because open-clip ships them bundled, but `embed_image` only calls
`model.encode_image`.

Memory profile (estimated, image-tower fp32 since we want determinism for the
artifact):
  * weights resident:        ~150MB (visual tower)
  * torch CPU runtime:       ~250MB
  * per-call activations:    ~40-80MB (image-tower forward pass)
  * peak per worker:         ~450-600MB

Combined with FFmpeg subprocess working sets at 1080×1920 (800MB-1.4GB peak)
this is why the worker bumped to 4096MB. See fly.toml [[vm]] worker block.
"""

from __future__ import annotations

import io
import threading
from typing import Any

import numpy as np
import structlog
from PIL import Image

from app.pipeline.font_identification import (
    FontMatch,
    RegistryEmbeddings,
    cosine_rank,
    load_registry_embeddings,
)

log = structlog.get_logger()


# ── Model identity (pinned — bumping requires regenerating the .npz) ─────────

# open-clip's canonical name for ViT-B/32 with OpenAI's published weights.
# This pair (architecture, pretrained tag) is what the embeddings artifact is
# locked to. Bumping either side requires regenerating registry-embeddings.npz.
MODEL_ARCHITECTURE = "ViT-B-32"
MODEL_PRETRAINED = "openai"
MODEL_ID = f"open-clip/{MODEL_ARCHITECTURE}/{MODEL_PRETRAINED}"

# Version tag stored alongside the artifact. Bumping this without bumping the
# weights themselves still forces the loader to reject old artifacts — useful
# when we change preprocessing (different resize, different normalization)
# even with identical weights.
MODEL_VERSION = "1"


# ── Lazy singleton state ─────────────────────────────────────────────────────

_lock = threading.Lock()
_state: dict[str, Any] = {
    "model": None,  # open_clip model (visual tower only used)
    "preprocess": None,  # callable: PIL.Image -> torch.Tensor
    "device": None,  # "cpu"
    "registry": None,  # RegistryEmbeddings
}


# ── ClipFontMatcher ──────────────────────────────────────────────────────────


class ClipFontMatcher:
    """FontMatcher implementation backed by open-clip ViT-B/32 image tower.

    Conforms structurally to `app.pipeline.font_identification.FontMatcher`
    Protocol (duck-typed; no inheritance needed). Constructing this object
    is cheap — the heavy model load is deferred to the first `embed_image`
    call, gated by a process-wide lock so multiple Celery tasks racing on a
    cold worker don't load the model twice.
    """

    def embed_image(self, image_bytes: bytes) -> np.ndarray:
        _ensure_loaded()
        import torch  # local import — torch is heavy, keep it off module init path

        img = Image.open(io.BytesIO(image_bytes)).convert("RGB")
        # preprocess returns a CHW tensor; unsqueeze to NCHW with batch=1
        tensor = _state["preprocess"](img).unsqueeze(0).to(_state["device"])
        with torch.no_grad():
            features = _state["model"].encode_image(tensor)
        # Match the L2 normalization used at registry-embed time. Without this,
        # cosine_rank's defensive renorm still produces correct ranking, but
        # the absolute similarity values are off-distribution and the eval's
        # > 0.5 floor stops being meaningful.
        features = features / features.norm(dim=-1, keepdim=True)
        return features.squeeze(0).cpu().numpy().astype(np.float32, copy=False)

    def rank_registry(self, query_embedding: np.ndarray, top_n: int = 5) -> list[FontMatch]:
        _ensure_loaded()
        registry: RegistryEmbeddings = _state["registry"]
        return cosine_rank(query_embedding, registry, top_n=top_n)


# ── Singleton management ─────────────────────────────────────────────────────


def _ensure_loaded() -> None:
    """Idempotent: load model + registry exactly once per process."""
    if _state["model"] is not None:
        return
    with _lock:
        if _state["model"] is not None:
            return
        import open_clip  # local import; heavy

        device = "cpu"
        model, _train_pre, preprocess = open_clip.create_model_and_transforms(
            MODEL_ARCHITECTURE,
            pretrained=MODEL_PRETRAINED,
            device=device,
        )
        model.eval()
        # Drop the text encoder weights — we never call encode_text. Frees
        # ~75MB without affecting determinism of image embeddings.
        if hasattr(model, "transformer"):
            try:
                del model.transformer
            except (AttributeError, TypeError):
                # Some open-clip variants don't expose `transformer` directly
                # or guard against deletion. Non-fatal — we keep the memory.
                pass

        registry = load_registry_embeddings(
            expected_model_id=MODEL_ID,
            expected_model_version=MODEL_VERSION,
        )

        _state["model"] = model
        _state["preprocess"] = preprocess
        _state["device"] = device
        _state["registry"] = registry
        log.info(
            "clip_font_matcher_loaded",
            model_id=MODEL_ID,
            model_version=MODEL_VERSION,
            registry_families=len(registry.families),
        )


def get_matcher() -> ClipFontMatcher:
    """Return the process-wide ClipFontMatcher. Triggers lazy model load on
    the first call; subsequent calls are O(1). Use this from Celery tasks
    and the `worker_ready` signal handler for prewarming."""
    matcher = ClipFontMatcher()
    _ensure_loaded()
    return matcher
