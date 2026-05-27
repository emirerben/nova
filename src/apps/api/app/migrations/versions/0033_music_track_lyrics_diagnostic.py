"""Add lyrics diagnostic + Whisper draft + extraction version columns.

Revision ID: 0033
Revises: 0032
Create Date: 2026-05-27

Non-destructive schema-only migration. Auto-applied on deploy via the
release_command. NO data mutation — the destructive cleanup of existing
`lyrics_source='whisper_only'` rows lives in
`scripts/migrate_whisper_only_rows.py` and is run manually by an operator.

Adds three columns to `music_tracks`:

  * `lyrics_diagnostic` JSONB NULL — structured trace of every LRCLIB
    lookup attempt: cleaned title/artist sent, response status of /api/get
    and /api/search, top fuzzy-search candidate score, duration delta of
    the matched recording, fallback path taken. Surfaced in the admin UI
    so a failed extraction is debuggable from the admin Lyrics tab without
    grepping worker logs.

  * `lyrics_whisper_draft` JSONB NULL — the Whisper-only transcription kept
    as a draft for admin reference when the production extraction fails
    (LRCLIB miss, low-confidence alignment, etc.). Lives in its OWN column
    so production consumers (`lyric_injector`, music-job render, agentic
    template path) can safely read `lyrics_cached` without worrying about
    leaking non-publishable Whisper hallucinations onto burned video.

  * `lyrics_extraction_version` INT NOT NULL DEFAULT 0 — monotonic counter
    bumped on every re-extract or force-LRCLIB-ID admin action. The
    extraction task takes an `expected_version` parameter and updates
    conditionally on it — if a newer task has already bumped the row, the
    older task's mutation is discarded. Prevents an admin who rapidly
    pastes a wrong ID then a right ID from racing the older task's
    completion overwriting the newer one's result.
"""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "0033"
down_revision = "0032"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "music_tracks",
        sa.Column("lyrics_diagnostic", postgresql.JSONB, nullable=True),
    )
    op.add_column(
        "music_tracks",
        sa.Column("lyrics_whisper_draft", postgresql.JSONB, nullable=True),
    )
    op.add_column(
        "music_tracks",
        sa.Column(
            "lyrics_extraction_version",
            sa.Integer(),
            nullable=False,
            server_default="0",
        ),
    )


def downgrade() -> None:
    op.drop_column("music_tracks", "lyrics_extraction_version")
    op.drop_column("music_tracks", "lyrics_whisper_draft")
    op.drop_column("music_tracks", "lyrics_diagnostic")
