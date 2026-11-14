"""Content-derived prompt versions.

A hand-numbered ``prompt_version`` shared by many PRs is one line every prompt PR
edits, so each merge conflicts with every other open prompt PR (2026-11-05: 31 of
64 conflicting merges were only that line, its history comment and the tests that
pinned it). A fingerprint of the prompt's source files changes exactly when they
change, and nobody edits it, so two PRs can't collide on it.

The version labels traces, stored agent runs and eval proofs, and it is one input of
the turn-checkpoint identity digest (``services/turn_checkpoint.py``): a new value only
makes a continued turn re-call the model instead of reusing a cached answer, and
``FLY_IMAGE_REF`` already does that on every deploy. Nothing orders these versions.

Covered: the files each agent lists, including the file behind every ``{{TOKEN}}``
vocabulary it renders (``prompt_vocab.PROMPT_VOCABULARY_SOURCES``). Not covered: text
built at render time from capability manifests and other runtime inputs.
"""

from __future__ import annotations

import hashlib
from collections.abc import Iterable
from pathlib import Path

# src/apps/api in a checkout, /app in the prod image: both hold app/ and prompts/.
API_ROOT = Path(__file__).resolve().parents[2]


def prompt_fingerprint(
    prefix: str, sources: Iterable[str], *, root: Path = API_ROOT, extra: str = ""
) -> str:
    """Return ``"<prefix>-<12 hex>"`` over the named files (paths relative to ``root``).

    Order-independent and line-ending-independent, so every machine computes the
    same value for the same code. Each file's name is hashed with its bytes, so
    moving text between files also changes the version. ``extra`` is hashed in as well, for a
    value the prompt renders that no source file holds (it lives outside ``root`` in the prod
    image, so it cannot be a source).
    """
    digest = hashlib.sha256()
    for rel in sorted(set(sources)):
        path = root / rel
        # A missing file must not take the API down at import (load_prompt falls back to
        # inline defaults); it still yields a distinct version, and
        # tests/agents/test_prompt_fingerprint.py fails on it in CI.
        data = path.read_bytes().replace(b"\r\n", b"\n") if path.is_file() else b"\0missing"
        digest.update(rel.encode("utf-8") + b"\0" + data + b"\0")
    if extra:
        digest.update(b"extra\0" + extra.encode("utf-8") + b"\0")
    return f"{prefix}-{digest.hexdigest()[:12]}"
