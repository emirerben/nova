"""Qendresa Q1 case 10: a title's 3 s hold becomes the whole video's length.

Ported from the kit (`kria-qendresa-test/tests/bugs/test_q1_title_hold_length.py`). Her prompt
says "Start with a big title ... for the first 3 seconds". When the brief writer files that hold
as a `timing` row with `duration_s: 3`, `length_policy.creator_duration` took it as the creator's
exact total, and an ~80 s Talking edit then "can't be made the length you asked for".

Only the typed field decides (KRI-667): a `timing` row scoped to the title holds the title. The
same hold filed with scope `global` has the typed shape of a real length ask ("make it 3
seconds"); telling them apart would mean reading the words, so that filing is the brief writer's
to fix (kit params `title_hold_scope_global` / `ending_hold`, not ported here).
Failure modes: docs/reviews/qendresa-says-done-b-failure-modes.md (case 10).
"""

from __future__ import annotations

import pytest

from app.kria.brief import BriefRequirement, CreativeBrief
from app.services.length_policy import creator_duration

TITLE_HOLD = BriefRequirement(
    id="t1",
    kind="timing",
    scope="title",
    description="Show the big opening title for the first 3 seconds",
    facts={"duration_s": 3},
)
LENGTH = BriefRequirement(
    id="l1",
    kind="timing",
    scope="global",
    description="make it 30 seconds",
    facts={"duration_s": 30},
)


def test_a_title_hold_never_sets_the_video_length() -> None:
    found = creator_duration(brief=CreativeBrief(version=1, requirements=[TITLE_HOLD]))
    assert found is None, f"a 3 s title hold was read as the video's total length: {found}"


def test_a_global_length_still_counts_beside_a_title_hold() -> None:
    found = creator_duration(brief=CreativeBrief(version=1, requirements=[TITLE_HOLD, LENGTH]))
    assert found is not None and found.seconds == 30.0, found


def test_switch_off_reads_the_title_row_as_before(monkeypatch: pytest.MonkeyPatch) -> None:
    from app.config import settings

    monkeypatch.setattr(settings, "title_hold_not_length_enabled", False)
    found = creator_duration(brief=CreativeBrief(version=1, requirements=[TITLE_HOLD]))
    assert found is not None and found.seconds == 3.0
