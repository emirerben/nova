"""Production-shaped settings for tests of the iPhone-rendering (pilot) path.

Why this exists: two KRI-132 fixes shipped green and failed on the first real
iPhone, because every unit test resolved its manifest under config DEFAULTS and
with no narration identity, while production runs with ~80 feature flags on.
The capability that broke (`guided_voiceover_executable`) is only ever True
when `creator_prompt_fidelity_enabled` and guided editing are both on AND the
voiceover has a resolved narration identity -- i.e. always in production, never
in the old tests.

The values come from `tests/prod_flags.snapshot.json`, which
`scripts/prod_flag_snapshot.py` writes (KRI-1375); this file no longer carries a hand
copy. Each snapshot entry names its evidence: the code default, the former Fly secret
digest comparison (2026-10-08, no value read), or a live read of the production API
(`GET /admin/kria/phone-render-config`). Refresh with
`python scripts/prod_flag_snapshot.py refresh --live`; record newly learned flags with
`set NAME=VALUE --source "..."`; `check` fails CI-side when `Settings` gains a field the
snapshot has not seen. Flags still at an unconfirmed default are listed by `unverified`.
Allowlists (`*_user_ids`) are Fly secrets: the snapshot holds the code default, not the
production membership.

Use `apply_prod_profile(monkeypatch)` (or the `prod_profile` fixture in
conftest.py) at the top of any test that claims "this renders on the phone in
production".
"""

import json
from pathlib import Path

from app.config import settings

SNAPSHOT_PATH = Path(__file__).with_name("prod_flags.snapshot.json")
PROD_SNAPSHOT: dict[str, dict[str, object]] = json.loads(SNAPSHOT_PATH.read_text(encoding="utf-8"))[
    "settings"
]

# `PHONE_RENDER_VERIFIED_FEATURES` as read from the production API: a capability-name
# list, not a secret. `behindSubject` etc. are present only when production verified them.
PROD_VERIFIED_FEATURES: tuple[str, ...] = tuple(
    PROD_SNAPSHOT["phone_render_verified_features"]["value"]  # type: ignore[arg-type]
)

# A voiceover as production sees it: the upload has been probed, so the
# manifest carries a narration identity. Tests that omit this never exercise
# `guided_voiceover_executable`.
PROD_NARRATION_IDENTITY: dict[str, object] = {
    "gcs_path": "users/creator/creation-threads/thread/ios-voiceover.m4a",
    "generation": "1758493638000000",
    "duration_s": 48.0,
}


def prod_setting_overrides() -> dict[str, object]:
    """Every snapshot value backed by evidence, by `Settings` attribute.

    Entries still at ``default`` are left to the running `Settings`: they carry no
    information about production, and a test's own environment may legitimately differ.
    """

    return {
        name: entry["value"]
        for name, entry in PROD_SNAPSHOT.items()
        if entry["source"] != "default" and hasattr(settings, name)
    }


def apply_prod_profile(monkeypatch) -> list[str]:
    """Run with the settings production has; return the names actually applied."""

    applied: list[str] = []
    for name, value in prod_setting_overrides().items():
        monkeypatch.setattr(settings, name, value)
        applied.append(name)
    return applied
