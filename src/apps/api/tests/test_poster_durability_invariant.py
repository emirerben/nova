"""Poster durability invariant: a library thumbnail must never be deletable.

The 2026-08 incident class this pins: posters were written as `<video>.poster.jpg`
siblings, so they inherited the source video's GCS lifecycle rule and were
silently deleted (music at 24h, template at 30d) — every affected library tile
went blank with nothing able to repair it. The fix moved all NEW posters to the
`job-posters/{job_id}/` prefix, which no lifecycle rule matches.

That fact is only as durable as this file makes it. Three ways it could quietly
regress, each pinned below:

1. Someone adds a lifecycle rule to `infra/gcs-lifecycle.json` that matches
   `job-posters/` (directly, via a parent prefix, or via an age-only rule with
   no prefix condition at all, which matches EVERYTHING).
2. A new poster write-path forgets to thread `job_id`, silently falling back to
   the legacy lifecycle-bound sibling key.
3. The durable prefix drifts out of `JOB_OUTPUT_PREFIXES`, orphaning posters
   from account deletion (retention is the flip side of durability).

Note the limit of this guard: it pins the CONFIG file. The live bucket is
updated manually (`gsutil lifecycle set infra/gcs-lifecycle.json ...` — see
CLAUDE.md); only apply rules from that file, never ad hoc.
"""

from __future__ import annotations

import ast
import json
import uuid
from pathlib import Path

from app.services.job_storage_paths import JOB_OUTPUT_PREFIXES, JOB_POSTER_PATH_PREFIX
from app.services.template_poster import JOB_POSTER_PREFIX, poster_object_path

REPO_ROOT = Path(__file__).resolve().parents[4]
API_ROOT = Path(__file__).resolve().parents[1]
LIFECYCLE_CONFIG = REPO_ROOT / "infra" / "gcs-lifecycle.json"

# The one durable home for job posters, as both modules spell it.
_DURABLE = "job-posters/"


def _delete_rules() -> list[dict]:
    payload = json.loads(LIFECYCLE_CONFIG.read_text())
    rules = payload.get("rule") or payload.get("lifecycle", {}).get("rule") or []
    return [r for r in rules if (r.get("action") or {}).get("type") == "Delete"]


def test_prefix_constants_agree() -> None:
    assert JOB_POSTER_PATH_PREFIX == "job-posters/{job_id}/"
    assert JOB_POSTER_PREFIX == "job-posters"


def test_no_lifecycle_delete_rule_can_ever_match_a_durable_poster() -> None:
    rules = _delete_rules()
    assert rules, "lifecycle config unreadable or empty — guard cannot vouch for posters"
    for rule in rules:
        condition = rule.get("condition") or {}
        prefixes = condition.get("matchesPrefix")
        # An age-only Delete rule with no prefix condition matches the WHOLE
        # bucket, posters included. Refuse it outright.
        assert prefixes, (
            "Delete rule without matchesPrefix would match job-posters/ (and "
            f"everything else): {rule!r}"
        )
        for prefix in prefixes:
            assert not _DURABLE.startswith(prefix) and not prefix.startswith(_DURABLE), (
                f"lifecycle Delete prefix {prefix!r} matches the durable poster "
                "prefix — this re-creates the deleted-thumbnails incident. "
                "Posters must never live under a lifecycle-deleted prefix."
            )


def test_durable_keys_land_on_the_durable_prefix_for_any_job() -> None:
    for job_id in (uuid.uuid4(), str(uuid.uuid4())):
        key = poster_object_path("generative-jobs/x/video.mp4", job_id=job_id)
        assert key.startswith(f"{_DURABLE}{job_id}/"), key


def test_account_deletion_covers_the_durable_prefix() -> None:
    # `purge_user_storage` iterates JOB_OUTPUT_PREFIXES; if the poster prefix
    # falls out of it, deleted users leave thumbnails behind forever.
    assert JOB_POSTER_PATH_PREFIX in JOB_OUTPUT_PREFIXES


def _poster_write_calls(tree: ast.AST) -> list[ast.Call]:
    """Every call to a poster WRITE entrypoint in one parsed module."""
    writers = {"upload_video_poster", "generate_and_upload_from_gcs"}
    calls = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        func = node.func
        name = getattr(func, "attr", None) or getattr(func, "id", None)
        if name in writers:
            calls.append(node)
    return calls


def test_every_poster_write_call_site_threads_job_id() -> None:
    """A write path that omits job_id silently regresses to the doomed sibling key.

    `poster_object_path` keeps a legacy no-job_id fallback for READING old keys,
    which makes forgetting the kwarg at a new write site a silent regression —
    the poster lands back on the video's lifecycle-bound prefix and dies with
    it. Scan every call site of the two write entrypoints instead of trusting
    review to catch it.
    """
    offenders: list[str] = []
    scan_roots = [API_ROOT / "app", API_ROOT / "scripts"]
    for root in scan_roots:
        for path in sorted(root.rglob("*.py")):
            tree = ast.parse(path.read_text(), filename=str(path))
            for call in _poster_write_calls(tree):
                if not any(kw.arg == "job_id" for kw in call.keywords):
                    offenders.append(f"{path.relative_to(API_ROOT)}:{call.lineno}")
    assert not offenders, (
        "poster write call sites missing job_id= (the poster would land on a "
        f"lifecycle-deleted prefix): {offenders}"
    )
