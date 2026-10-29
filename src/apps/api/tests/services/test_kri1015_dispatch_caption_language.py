"""KRI-1015: what the dispatcher stamps as `caption_language_request` on a real job.

Failure modes, written before the fix:
 1. A mixed ask ("English captions for the English, Turkish captions for my Turkish")
    stamps a target, so every English line of a mixed take is translated into Turkish
    (Plaza T2, job e46baf42). The stamp must be absent: keep each language as spoken.
 2. The dispatched request ends with the "Superseded requirements" block, which lists
    requirements the creator REMOVED. A removed "Turkish captions" requirement read as the
    ask would stamp "tr" over the live "English captions please".
 3. Over-correction: a plain one-language ask stops being stamped.
"""

from __future__ import annotations

import uuid

from app.kria.brief_binding import SUPERSEDED_REQUIREMENTS_HEADING
from app.services.generative_jobs import build_generative_job
from tests.pipeline.test_caption_language import _T2_DISPATCHED_REQUEST


def _stamped(creator_request: str) -> dict:
    return build_generative_job(
        user_id=uuid.uuid4(),
        clip_paths=["users/u/plan/i/a.mp4"],
        mode="content_plan",
        content_plan_item_id=uuid.uuid4(),
        content_plan_ownership_epoch=0,
        edit_format="subtitled",
        creator_request=creator_request,
    ).all_candidates


def _with_superseded(live: str, *removed: str) -> str:
    return (
        f"{live}\n\n{SUPERSEDED_REQUIREMENTS_HEADING} "
        "The current creative brief above is authoritative:\n"
        + "\n".join(f"- r{i}: {text}" for i, text in enumerate(removed, start=2))
    )


def test_mixed_ask_dispatched_request_stamps_no_caption_language() -> None:
    candidates = _stamped(_T2_DISPATCHED_REQUEST)

    assert "caption_language_request" not in candidates
    assert candidates["creator_request"] == _T2_DISPATCHED_REQUEST.strip()


def test_superseded_requirement_is_not_read_as_the_ask() -> None:
    request = _with_superseded("Latest message: English captions please", "Turkish captions")

    assert _stamped(request)["caption_language_request"] == "en"


def test_superseded_block_alone_stamps_nothing() -> None:
    request = _with_superseded("Latest message: make it punchy", "Turkish captions")

    assert "caption_language_request" not in _stamped(request)


def test_plain_one_language_ask_is_still_stamped() -> None:
    assert _stamped("Turkish captions")["caption_language_request"] == "tr"
