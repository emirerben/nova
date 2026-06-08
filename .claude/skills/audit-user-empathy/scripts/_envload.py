"""Shared .env loader for the audit-plan-quality scripts (stdlib only).

The scripts import `app.*` and call external APIs, so they need keys from the
repo-root `.env`. Wrinkle: Nova's workflow runs in git worktrees (CLAUDE.md), and
`.env` is gitignored — a fresh worktree has NO `.env`. So we look in two places:

  1. Walk up from the start dir (covers the primary checkout case).
  2. The MAIN worktree root (via `git --git-common-dir`) — covers the worktree
     case, where the real `.env` lives back in the primary checkout.

Existing environment variables always win (`setdefault`), so an explicitly
exported key is never overwritten.
"""

from __future__ import annotations

import os
import subprocess
from pathlib import Path


def _apply(env_file: Path) -> bool:
    if not env_file.exists():
        return False
    for line in env_file.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, val = line.partition("=")
        os.environ.setdefault(key.strip(), val.strip().strip('"').strip("'"))
    return True


def _main_worktree_root(start: Path) -> Path | None:
    """Resolve the primary checkout root, even when called from a linked worktree."""
    try:
        out = subprocess.run(
            ["git", "rev-parse", "--path-format=absolute", "--git-common-dir"],
            cwd=str(start),
            capture_output=True,
            text=True,
            timeout=5,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    if out.returncode != 0 or not out.stdout.strip():
        return None
    # --git-common-dir points at <primary>/.git; its parent is the checkout root.
    common = Path(out.stdout.strip())
    return common.parent if common.name == ".git" else None


def load_dotenv(start: Path | None = None) -> None:
    """Populate os.environ from the nearest `.env`, with a main-worktree fallback."""
    start = (start or Path.cwd()).resolve()
    for parent in (start, *start.parents):
        if _apply(parent / ".env"):
            return
    root = _main_worktree_root(start)
    if root is not None:
        _apply(root / ".env")
