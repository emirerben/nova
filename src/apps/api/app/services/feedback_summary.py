"""Deterministic rollup of a content plan's video_feedback → preference_summary.

The feedback loop's aggregation step (Phase 2, D5). Compresses raw feedback signals
into a SHORT, BOUNDED string that threads into content_plan_generator (on a
user-triggered regenerate) and intro_writer (future videos). Bounded BY CONSTRUCTION:
signal counts (O(1)) + the most-recent-N notes, with the whole string clamped to
``MAX_SUMMARY_CHARS`` — so the downstream agent prompt stays fixed-size no matter how
many thousands of feedback rows a power user accumulates.

Deterministic on purpose (no LLM): the input is already structured (4 enum signals +
short note texts), so a pure function gives a serviceable summary with zero model
cost, zero latency, and — crucially — no extra prompt-injection hop. Notes are
UNTRUSTED user free-text, so every one is `_sanitize_text`'d (strips control chars,
role markers, code fences) before it enters the summary that becomes prompt input.

This is a deliberate seam: swap in a `FeedbackSummarizerAgent` here later if natural-
language synthesis is wanted — the call sites only depend on `build_preference_summary`
returning a bounded string.
"""

from __future__ import annotations

from app.agents.music_matcher import _sanitize_text

# Caps that keep the summary — and therefore every downstream agent prompt — fixed-
# size regardless of feedback volume. The most recent notes carry the most signal;
# older ones are folded into the counts.
MAX_NOTES_IN_SUMMARY = 12
MAX_SUMMARY_CHARS = 800

# Human labels for the thumb-class signals, in display order.
_SIGNAL_LABELS: tuple[tuple[str, str], ...] = (
    ("up", "liked"),
    ("down", "disliked"),
    ("more_like_this", "more like this"),
)


def build_preference_summary(
    *,
    signal_counts: dict[str, int],
    recent_notes: list[str],
) -> str:
    """Build the bounded preference_summary string.

    Args:
        signal_counts: per-signal totals over the WHOLE feedback set, e.g.
            ``{"up": 12, "down": 3, "more_like_this": 5}``. Missing keys count as 0.
        recent_notes: note texts ordered MOST-RECENT-FIRST. Capped to
            ``MAX_NOTES_IN_SUMMARY`` here, so callers may pass the full list.

    Returns:
        A short labeled string, or "" when there is no usable feedback at all
        (callers persist that as NULL, and the generator reads NULL as "(none)").
    """
    parts: list[str] = []

    count_bits = [
        f"{label}: {n}"
        for key, label in _SIGNAL_LABELS
        if (n := int(signal_counts.get(key, 0) or 0)) > 0
    ]
    if count_bits:
        parts.append("Reactions on past videos — " + ", ".join(count_bits) + ".")

    clean_notes = [n for note in recent_notes[:MAX_NOTES_IN_SUMMARY] if (n := _sanitize_text(note))]
    if clean_notes:
        parts.append("What the creator told us (most recent first):")
        parts.extend(f"- {n}" for n in clean_notes)

    summary = "\n".join(parts).strip()
    if len(summary) > MAX_SUMMARY_CHARS:
        # Hard clamp at a word boundary so the downstream prompt is always bounded.
        summary = summary[:MAX_SUMMARY_CHARS].rsplit(" ", 1)[0].rstrip()
    return summary


def rollup_user_feedback(session, user_id) -> str:  # noqa: ANN001
    """Compute the bounded preference_summary for a user from their video_feedback.

    USER-scoped, not plan-scoped: a 👎 on a one-off library video is as much a
    preference signal as one on a planned video, so the rollup captures every row
    the user owns. The query is O(1) counts + the most-recent-N notes (indexed on
    `(user_id, created_at)`), so it stays bounded no matter how much feedback exists.
    Sync session — called from the re-tune Celery tasks. Returns "" when the user
    has left no feedback (callers persist that as NULL).
    """
    from sqlalchemy import func as safunc  # noqa: PLC0415
    from sqlalchemy import select  # noqa: PLC0415

    from app.models import VideoFeedback  # noqa: PLC0415

    count_rows = session.execute(
        select(VideoFeedback.signal, safunc.count())
        .where(VideoFeedback.user_id == user_id)
        .group_by(VideoFeedback.signal)
    ).all()
    signal_counts = {sig: int(n) for sig, n in count_rows}

    note_rows = (
        session.execute(
            select(VideoFeedback.note)
            .where(
                VideoFeedback.user_id == user_id,
                VideoFeedback.signal == "note",
                VideoFeedback.note.isnot(None),
            )
            .order_by(VideoFeedback.created_at.desc())
            .limit(MAX_NOTES_IN_SUMMARY)
        )
        .scalars()
        .all()
    )
    return build_preference_summary(
        signal_counts=signal_counts,
        recent_notes=[n for n in note_rows if n],
    )
