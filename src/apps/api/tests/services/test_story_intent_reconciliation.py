from dataclasses import replace

import pytest

from app.schemas.clip_intents import ClipIntent
from app.schemas.story_intents import StoryIntentRefs, StoryRowRef
from app.services.story_intent_reconciliation import (
    inventory_revision,
    reconcile_story_intents,
    story_revision,
)

REJECTED = ClipIntent(intent_id="interior", op="include", attribute="Cabildo interior")
RETAINED = ClipIntent(intent_id="palace", op="include", attribute="Palace exterior")
CAPTION = ClipIntent(
    intent_id="caption", op="caption", attribute="Palace exterior", creator_text="Cabildo interior"
)
CHECKED = (REJECTED, RETAINED, CAPTION)
ROWS = ("Cabildo interior", "Palace exterior", "Cabildo interior")


def references():
    return StoryIntentRefs(
        story_revision(ROWS),
        inventory_revision(CHECKED),
        (
            StoryRowRef(0, ("interior",)),
            StoryRowRef(1, ("palace",)),
            StoryRowRef(2, ("caption",), kind="literal"),
        ),
    )


def reconcile(refs=None, rejected=(REJECTED,), rows=ROWS, checked=CHECKED):
    return reconcile_story_intents(
        rows, refs=refs, checked_intents=checked, rejected_intents=rejected
    )


def test_removes_only_referenced_visual_row_preserving_identical_literal():
    result = reconcile(references())
    assert result.status == "reconciled"
    assert result.rows == ("Palace exterior", "Cabildo interior")
    assert result.removed_row_indexes == (0,)
    assert CHECKED == (REJECTED, RETAINED, CAPTION)
    assert result.refs.rows == (
        StoryRowRef(0, ("palace",)),
        StoryRowRef(1, ("caption",), kind="literal"),
    )


def test_replaying_answer_with_returned_references_is_idempotent():
    first = reconcile(references())
    second = reconcile(first.refs, rows=first.rows)
    assert second.status == "reconciled"
    assert second.rows == first.rows
    assert second.refs == first.refs
    assert second.removed_row_indexes == ()


def test_no_authoritative_rejection_leaves_unmapped_story_unchanged():
    result = reconcile(rejected=())
    assert result.status == "unchanged"
    assert result.rows == ROWS


@pytest.mark.parametrize("question_id", ["different", "asked"])
def test_only_matching_question_rejection_reaches_this_boundary(question_id):
    from app.services.clip_intent_planning import apply_creator_selections
    from app.services.clip_selection import fold_clip_selections, intent_key

    key = intent_key(REJECTED)
    events = [
        (
            "assistant",
            {
                "clip_question": {
                    "question_id": "asked",
                    "categories": [
                        {
                            "key": key,
                            "op": "include",
                            "label": "Cabildo interior",
                            "intent_id": "interior",
                            "candidate_media_ids": [],
                        }
                    ],
                }
            },
        ),
        (
            "user",
            {
                "clip_selection": {
                    "question_id": question_id,
                    "none_keys": [key],
                }
            },
        ),
    ]
    _, _, dropped = apply_creator_selections(
        list(CHECKED), clips=[], selections=fold_clip_selections(events), creator_request=""
    )
    assert dropped == ({"interior"} if question_id == "asked" else set())
    result = reconcile(references(), rejected=tuple(i for i in CHECKED if i.intent_id in dropped))
    assert result.status == ("reconciled" if dropped else "unchanged")
    assert result.rows == (ROWS[1:] if dropped else ROWS)


@pytest.mark.parametrize(
    "refs,reason",
    [
        (None, "missing_references"),
        (replace(references(), story_revision="old"), "stale_story_revision"),
        (replace(references(), inventory_revision="old"), "stale_inventory_revision"),
        (replace(references(), rows=references().rows[:2]), "incomplete_row_references"),
        (
            replace(references(), rows=(StoryRowRef(0, ("unknown",)), *references().rows[1:])),
            "invalid_row_reference",
        ),
        (
            replace(references(), rows=(StoryRowRef(0), *references().rows[1:])),
            "unmapped_derived_row",
        ),
        (
            replace(
                references(), rows=(StoryRowRef(0, ("interior", "palace")), *references().rows[1:])
            ),
            "mixed_row",
        ),
    ],
)
def test_ambiguous_references_preserve_entire_story(refs, reason):
    result = reconcile(refs)
    assert result.status == "unresolved"
    assert result.reason == reason
    assert result.rows == ROWS
    assert result.refs == refs
    assert result.removed_row_indexes == ()


def test_reused_id_with_different_meaning_does_not_authorize_removal():
    result = reconcile(
        references(), rejected=(REJECTED.model_copy(update={"attribute": "different"}),)
    )
    assert result.status == "unresolved"
    assert result.reason == "rejection_not_checked"
    assert result.rows == ROWS


def test_same_referent_retained_intent_survives():
    same_source = RETAINED.model_copy(update={"attribute": REJECTED.attribute})
    checked = (REJECTED, same_source, CAPTION)
    result = reconcile(
        replace(references(), inventory_revision=inventory_revision(checked)), checked=checked
    )
    assert result.status == "reconciled"
    assert result.rows == ("Palace exterior", "Cabildo interior")


def test_marking_rejected_visual_as_literal_cannot_bypass_gate():
    refs = replace(
        references(), rows=(*references().rows[:2], StoryRowRef(2, ("interior",), "literal"))
    )
    result = reconcile(refs)
    assert result.status == "unresolved"
    assert result.reason == "unverified_literal_row"
    assert result.rows == ROWS


@pytest.mark.parametrize(
    "literal_ref",
    [
        StoryRowRef(2, (), "literal"),
        StoryRowRef(2, ("palace",), "literal"),
    ],
)
def test_unanchored_literal_cannot_bypass_gate(literal_ref):
    refs = replace(references(), rows=(*references().rows[:2], literal_ref))
    result = reconcile(refs)
    assert result.status == "unresolved"
    assert result.reason == "unverified_literal_row"
    assert result.rows == ROWS
