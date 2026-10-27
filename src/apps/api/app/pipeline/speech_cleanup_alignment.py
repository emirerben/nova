"""Forced alignment of transcript words onto narration audio (speech-cleanup filler check).

whisper-1's word stamps can drift a whole stretch late: in the Merope café take (KRI-790) every
word from 3.8 s to 12 s sits 0.3-0.5 s after its sound, the fillers included, so rule 1 cut "we
are" and "tell" and left the "uh"/"um" in. A CTC acoustic model trained on read English
(facebook/wav2vec2-base-960h, pinned) places the same words on the sound. Speech cleanup uses
those positions only to place filler cuts (``speech_cleanup_analysis._filler_aligned_words``).

The production image installs the ``speech-align`` extra and bakes the model into
``settings.speech_cleanup_aligner_model_dir`` (Dockerfile). Anywhere either is missing,
:func:`align_words` raises :class:`AlignerUnavailable` and cleanup keeps whisper's stamps.
"""

from __future__ import annotations

import json
import re
import subprocess
import unicodedata
from collections.abc import Sequence
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path

import numpy as np

MODEL_ID = "facebook/wav2vec2-base-960h"
MODEL_REVISION = "22aad52d435eb6dbaf354bdad9b0da84ce7d6156"
SAMPLE_RATE = 16_000
_HOP = 320  # wav2vec2 feature-encoder stride: one output frame per 20 ms
_RECEPTIVE = 400  # samples the first output frame covers
_SECONDS_PER_FRAME = _HOP / SAMPLE_RATE
_CHUNK_S = 20.0  # audio per model pass; self-attention memory grows with its square
_CONTEXT_S = 1.0  # extra audio on each side of a chunk, dropped from that chunk's output
_DECODE_TIMEOUT_S = 120


class AlignerUnavailable(RuntimeError):
    """The model or its runtime is not installed here; callers keep whisper's stamps."""


class AlignmentFailed(RuntimeError):
    """The audio could not be decoded or cannot hold the transcript."""


@dataclass(frozen=True)
class AlignedWords:
    spans: list[tuple[float, float] | None]  # one per input text; None for a token with no letters
    median_score: float  # median per-word model confidence on its own letters, 0-1


def letters(text: str) -> str:
    """The model's alphabet for one token: A-Z and apostrophe, accents folded ("ş" -> "S")."""

    folded = unicodedata.normalize("NFKD", text.replace("ı", "i").replace("İ", "I"))
    folded = "".join(ch for ch in folded if not unicodedata.combining(ch)).upper()
    return re.sub(r"[^A-Z']", "", folded)


@lru_cache(maxsize=1)
def _model(model_dir: str):  # noqa: ANN202
    try:
        import torch  # noqa: F401, PLC0415
        from transformers import Wav2Vec2ForCTC  # noqa: PLC0415
    except ImportError as exc:
        raise AlignerUnavailable("transformers or torch is not installed") from exc
    vocab_path = Path(model_dir) / "vocab.json"
    if not vocab_path.is_file():
        raise AlignerUnavailable(f"no aligner model at {model_dir}")
    from transformers.utils import logging as transformers_logging  # noqa: PLC0415

    # The checkpoint has no training-only `masked_spec_embed`; its load report is noise per worker.
    transformers_logging.set_verbosity_error()
    vocab = json.loads(vocab_path.read_text())
    model = Wav2Vec2ForCTC.from_pretrained(model_dir, local_files_only=True).eval()
    return model, vocab


def _decode_audio(path: str) -> np.ndarray:
    try:
        result = subprocess.run(
            [
                "ffmpeg",
                "-nostdin",
                "-v",
                "error",
                "-i",
                path,
                "-ac",
                "1",
                "-ar",
                str(SAMPLE_RATE),
                "-f",
                "f32le",
                "-",
            ],
            capture_output=True,
            check=True,
            timeout=_DECODE_TIMEOUT_S,
        )
    except (subprocess.SubprocessError, OSError) as exc:
        raise AlignmentFailed(f"audio decode failed: {type(exc).__name__}") from exc
    return np.frombuffer(result.stdout, dtype=np.float32)


def _frames(n_samples: int) -> int:
    return 0 if n_samples < _RECEPTIVE else (n_samples - _RECEPTIVE) // _HOP + 1


def _log_probs(model, audio: np.ndarray) -> np.ndarray:  # noqa: ANN001
    """Frame-level log-probabilities over the whole take, computed in context-padded chunks."""

    import torch  # noqa: PLC0415

    chunk, context = int(_CHUNK_S * SAMPLE_RATE), int(_CONTEXT_S * SAMPLE_RATE)
    total = _frames(len(audio))
    parts: list[np.ndarray] = []
    for start in range(0, len(audio), chunk):
        lo, hi = max(0, start - context), min(len(audio), start + chunk + context)
        piece = audio[lo:hi].astype(np.float32)
        if _frames(len(piece)) == 0:
            continue
        piece = (piece - piece.mean()) / np.sqrt(piece.var() + 1e-7)  # the model's preprocessor
        with torch.inference_mode():
            logits = model(torch.from_numpy(piece)[None]).logits[0]
        lp = torch.log_softmax(logits, dim=-1).numpy()
        first, last = start // _HOP, min(total, (start + chunk) // _HOP)  # this chunk's own frames
        offset = lo // _HOP
        parts.append(lp[max(0, first - offset) : max(0, last - offset)])
    if not parts:
        raise AlignmentFailed("audio too short")
    return np.concatenate(parts)[:total]


def viterbi(log_probs: np.ndarray, targets: Sequence[int], blank: int) -> list[tuple[int, int]]:
    """Best CTC path that emits exactly ``targets``; returns each token's (first, last) frame."""

    n_frames, n_tokens = log_probs.shape[0], len(targets)
    ext = np.full(2 * n_tokens + 1, blank, dtype=np.int64)
    ext[1::2] = targets
    n_states = len(ext)
    repeats = sum(1 for a, b in zip(targets, targets[1:], strict=False) if a == b)
    if n_frames < n_tokens + repeats:
        raise AlignmentFailed("audio too short for its transcript")
    can_skip = np.zeros(n_states, dtype=bool)
    can_skip[2:] = (ext[2:] != blank) & (ext[2:] != ext[:-2])
    neg = -np.inf
    score = np.full(n_states, neg)
    score[0] = log_probs[0, ext[0]]
    score[1] = log_probs[0, ext[1]]
    back = np.zeros((n_frames, n_states), dtype=np.int8)  # 0 stay, 1 from s-1, 2 from s-2
    for t in range(1, n_frames):
        step1 = np.concatenate(([neg], score[:-1]))
        step2 = np.where(can_skip, np.concatenate(([neg, neg], score[:-2])), neg)
        cand = np.stack((score, step1, step2))
        choice = cand.argmax(axis=0)
        score = cand[choice, np.arange(n_states)] + log_probs[t, ext]
        back[t] = choice
    state = n_states - 1 if score[-1] >= score[-2] else n_states - 2
    if not np.isfinite(score[state]):
        raise AlignmentFailed("no path emits the transcript")
    path = np.empty(n_frames, dtype=np.int64)
    for t in range(n_frames - 1, -1, -1):
        path[t] = state
        state -= int(back[t, state])
    spans: list[list[int]] = [[-1, -1] for _ in range(n_tokens)]
    for t, s in enumerate(path):
        if s % 2:
            token = s // 2
            if spans[token][0] < 0:
                spans[token][0] = t
            spans[token][1] = t
    return [(a, b) for a, b in spans]


def align_words(audio_path: str, texts: Sequence[str], *, model_dir: str) -> AlignedWords:
    """Place each text on the audio. Raises AlignerUnavailable / AlignmentFailed."""

    model, vocab = _model(model_dir)
    blank, delimiter = vocab["<pad>"], vocab["|"]
    targets: list[int] = []
    owner: list[int | None] = []
    for index, text in enumerate(texts):
        word = letters(text)
        if not word:
            continue
        if targets:
            targets.append(delimiter)
            owner.append(None)
        targets.extend(vocab[ch] for ch in word)
        owner.extend([index] * len(word))
    spans: list[tuple[float, float] | None] = [None] * len(texts)
    if not targets:
        return AlignedWords(spans=spans, median_score=0.0)
    log_probs = _log_probs(model, _decode_audio(audio_path))
    frames = viterbi(log_probs, targets, blank)
    scores: dict[int, list[float]] = {}
    for (first, last), token, who in zip(frames, targets, owner, strict=True):
        if who is None:
            continue
        start, end = first * _SECONDS_PER_FRAME, (last + 1) * _SECONDS_PER_FRAME
        current = spans[who]
        spans[who] = (
            (start, end) if current is None else (min(current[0], start), max(current[1], end))
        )
        scores.setdefault(who, []).append(float(np.exp(log_probs[first : last + 1, token].max())))
    per_word = [sum(v) / len(v) for v in scores.values()]
    return AlignedWords(spans=spans, median_score=float(np.median(per_word)) if per_word else 0.0)
