"""The contract verifier and brief receipts must import without pulling in skia.

The CI creator-contract replay job runs without libEGL, so any import chain that reaches
`import skia` fails there with `ImportError: libEGL.so.1`. That is how item 14's CTA
ending (PR #2103) broke the job: the contract verifier lazily imported a module whose
top-level imports reach skia. The text rules now live in `phone_cta_text`, which has no
render imports; this runs in a fresh interpreter so other tests' imports cannot hide it.
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

API_DIR = Path(__file__).resolve().parents[2]

_SCRIPT = """
import sys
import app.services.creator_render_contract  # noqa: F401
import app.kria.brief_checks  # noqa: F401
from app.pipeline.phone_cta_text import cta_ending_parts

assert cta_ending_parts("comment READ") == ("comment", "READ")
if "skia" in sys.modules:
    sys.stderr.write("skia was imported\\n")
    sys.exit(3)
"""


def test_contract_and_receipts_import_without_skia() -> None:
    result = subprocess.run(
        [sys.executable, "-c", _SCRIPT],
        cwd=API_DIR,
        capture_output=True,
        text=True,
        timeout=120,
    )
    assert result.returncode == 0, result.stderr[-2000:]
