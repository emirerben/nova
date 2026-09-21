"""KRI-127 sentinel: a new ``ClipMeta`` field must not switch on a dormant reader.

Several modules read optional attributes off ``ClipMeta`` with ``getattr(meta,
"<name>", default)`` for names the dataclass never had (music matcher
``summary``, talking-head assembler ``content_type``/``audio_type``, autoplace
``brands``, conformance ``composition_note``). Adding a field with one of those
names silently turns that code path on in production with no flag. New
understanding fields therefore use a ``clip_`` prefix when the bare name is
already read somewhere, and this test fails if a bare-name reader exists for
any field ``ClipMeta`` carries.
"""

from __future__ import annotations

import dataclasses
import re
from pathlib import Path

from app.pipeline.agents.gemini_analyzer import ClipMeta

APP_ROOT = Path(__file__).parents[2] / "app"

# Fields ClipMeta had before the shared understanding record (KRI-127).
_PRE_KRI127_FIELDS = {
    "clip_id",
    "transcript",
    "hook_text",
    "hook_score",
    "best_moments",
    "detected_subject",
    "analysis_degraded",
    "moments_synthetic",
    "failed",
    "clip_path",
    "text_safe_zone",
    "visual_density",
}
# Allowed: the shared-record writer, and the shim that fills ClipMeta from the
# analyzer agent's output (it reads the agent output, not ClipMeta).
_ALLOWED_READERS = {
    APP_ROOT / "services" / "clip_understanding.py",
    APP_ROOT / "pipeline" / "agents" / "gemini_analyzer.py",
}
_GETATTR = re.compile(r"""getattr\(\s*[\w.]+\s*,\s*["\'](\w+)["\']""")


def test_no_module_reads_a_new_clip_meta_field_by_its_bare_name() -> None:
    new_fields = {f.name for f in dataclasses.fields(ClipMeta)} - _PRE_KRI127_FIELDS
    assert new_fields, "expected the KRI-127 understanding fields on ClipMeta"

    offenders: list[str] = []
    for path in APP_ROOT.rglob("*.py"):
        if path in _ALLOWED_READERS:
            continue
        for lineno, line in enumerate(path.read_text().splitlines(), 1):
            for name in _GETATTR.findall(line):
                if name in new_fields:
                    offenders.append(f"{path.relative_to(APP_ROOT)}:{lineno} reads {name!r}")
    assert not offenders, (
        "a module reads a new ClipMeta field via getattr -- this would change its behavior "
        "with no flag; rename the ClipMeta field with a `clip_` prefix or wire it on purpose: "
        + "; ".join(offenders)
    )


def test_bare_names_with_dormant_readers_are_not_clip_meta_fields() -> None:
    names = {f.name for f in dataclasses.fields(ClipMeta)}
    for dormant in (
        "summary",
        "description",
        "brands",
        "composition_note",
        "content_type",
        "audio_type",
    ):
        assert dormant not in names
