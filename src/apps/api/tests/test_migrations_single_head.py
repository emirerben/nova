"""Regression guard for the multi-head alembic bug class.

History: PR #163 (2c0d148, auto-music Phase 3) landed
``0020_auto_music_job_clip_columns.py`` without rebasing onto PR #166
(89552c5, song_sections) which had already shipped
``0020_music_track_best_sections.py``. Both files claimed
``revision = "0020"`` with ``down_revision = "0019"``. Alembic refused
to pick a head and every Fly deploy after #163 aborted at
``python -m alembic upgrade head`` with:

    UserWarning: Revision 0020 is present more than once
    ERROR  Multiple head revisions are present for given argument 'head'

Three PRs piled up behind the broken queue before someone caught it
(at deploy time, not PR time). This test fails the PR instead — the
fingerprint is structural (count heads, count duplicate revision IDs),
not behavioral, so it runs in <100ms with no DB connection.
"""
from __future__ import annotations

from collections import Counter

from alembic.config import Config
from alembic.script import ScriptDirectory


def _script_dir() -> ScriptDirectory:
    """Load the project's alembic config the same way `alembic upgrade
    head` does in the Fly release_command. Resolves relative to
    ``alembic.ini`` at the repo root of the api app."""
    return ScriptDirectory.from_config(Config("alembic.ini"))


def test_single_alembic_head() -> None:
    """The migration DAG must have exactly one head. Multiple heads
    abort ``alembic upgrade head`` in the release_command and block
    every subsequent deploy."""
    heads = _script_dir().get_heads()
    assert len(heads) == 1, (
        f"alembic has {len(heads)} heads {heads!r} — pick one and re-chain "
        f"the others onto it. The Fly release_command would abort with "
        f"'Multiple head revisions are present'."
    )


def test_no_duplicate_revision_ids() -> None:
    """No two migration files may claim the same ``revision`` ID.
    Catches the exact filename-collision class from the May 2026
    incident before the merge button is reachable."""
    script = _script_dir()
    revisions = [rev.revision for rev in script.walk_revisions("base", "heads")]
    duplicates = [rev for rev, count in Counter(revisions).items() if count > 1]
    assert not duplicates, (
        f"Duplicate alembic revision IDs: {duplicates!r}. Rename one of the "
        f"colliding migration files and update its ``revision = ...`` line."
    )
