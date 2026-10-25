"""Packaged editorial guidance, selected by code rather than user-controlled paths."""

from functools import lru_cache
from pathlib import Path

_SKILLS = frozenset({"grounded-hook", "edit-story", "social-caption", "meaningful-moment"})
_ROOT = Path(__file__).with_name("skills")


@lru_cache(maxsize=len(_SKILLS))
def load_skill(name: str) -> str:
    """Load a trusted skill body. Missing assets are deployment errors, never silent."""
    if name not in _SKILLS:
        raise ValueError(f"Unknown runtime skill: {name}")
    source = (_ROOT / name / "SKILL.md").read_text(encoding="utf-8")
    if not source.startswith("---\n"):
        raise ValueError(f"Missing skill metadata: {name}")
    _, _, body = source.split("---\n", 2)
    if not body.strip() or len(body) > 6000:
        raise ValueError(f"Invalid runtime skill body: {name}")
    return body.strip()
