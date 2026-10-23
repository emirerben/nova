"""Prove the N2 fullscreen fixture rejects the original narrated compiler."""

import os
from pathlib import Path
import subprocess
import sys
import types

root = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(root / "src/apps/api"))
for key, value in {
    "STORAGE_BUCKET": "nova-test",
    "DATABASE_URL": "postgresql://localhost/test",
    "REDIS_URL": "redis://localhost:6379/0",
    "INTERNAL_API_KEY": "test",
}.items():
    os.environ.setdefault(key, value)
source = subprocess.check_output(
    ["git", "show", "2dc84bc68:src/apps/api/app/pipeline/phone_narrated_plan.py"],
    cwd=root,
    text=True,
)
baseline = types.ModuleType("n2_pre_fix_narrated")
sys.modules[baseline.__name__] = baseline
exec(compile(source, "<baseline-phone-narrated>", "exec"), baseline.__dict__)
from tests.pipeline import test_phone_narrated_fullscreen as replay  # noqa: E402

replay.replace_editor_lanes = baseline.replace_editor_lanes
try:
    replay.test_fullscreen_photo_then_video_is_a_sequence_below_pip_and_preserves_recipe()
except AssertionError:
    print(
        "RED CONTROL: original narrated compiler fails fullscreen/PiP track fidelity assertion, as expected."
    )
else:
    raise SystemExit("RED CONTROL INVALID: baseline unexpectedly passed")
