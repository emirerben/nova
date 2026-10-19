"""KRI-750: the editor Save contract, as one matrix.

A new Save failure mode is a new ROW below, never a new test file. The failure-mode list this
matrix implements is ``docs/reviews/kri-750-save-failure-modes.md``; write the line there first.

The contract: whatever the creator's own manual edits produce, Save accepts it, unless it would
lose the words or the media -- and then it refuses with a STABLE code, never a bare 500 and never
a silent rewrite. KRI-736 needed five fix PRs in a day because each face of this contract was
discovered on a real job. Each row is one face. Every row drives the real route sequence
(``plan_items._editor_commit_item``): ``editor_deletion_timeline`` ->
``stage_initial_authored_baseline`` -> ``normalize_editor_deletion_request`` ->
``require_guided_story_editor_commit`` -> ``prepare_authored_editor_commit`` (a clip delete), or
``prepare_editor_commit`` (every other Save). See ``_editor_save_matrix.py`` for the builders.

The iOS app decides some of the same rows BEFORE it sends a Save (the source-window bound, the
``role: null`` encoding). ``build_fixture`` derives those from the REAL server code into
``src/apps/ios/Tests/Fixtures/editor_save_matrix.json``; ``EditorSaveMatrixTests`` replays it.
Regenerate after a deliberate contract change::

    cd src/apps/api && UPDATE_EDITOR_SAVE_FIXTURE=1 .venv/bin/python -m pytest \\
        tests/routes/test_editor_save_contract_matrix.py -k ios_fixture_is_the_server_truth
"""

from __future__ import annotations

import json
import os
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import pytest
from fastapi import HTTPException

from app.agents._schemas.text_element import TextElement, coerce_text_elements
from app.routes import generative_jobs as gj
from app.services.authored_text_fit import fit_text_to_timeline
from app.services.creator_render_contract import (
    CreatorRenderContractError,
    TextPlacementError,
    TextRequirement,
)
from app.services.device_render import CONTRACT_REVISIONS_FIELD, device_status
from tests.routes._editor_save_matrix import (
    NULL_ROLE_TEXT_ID,
    ORDER,
    SOURCE_S,
    TITLE,
    VARIANT,
    Edit,
    FirstSlot,
    Outcome,
    archetype_save,
    closing_moved_lane,
    label_deleted_lane,
    null_role_lane,
    old_clock_lane,
    order_required_job,
    phone_retimed_lane,
    requirements,
    run_save,
    title_edit_lane,
)

FIXTURE = (
    Path(__file__).resolve().parents[5] / "src/apps/ios/Tests/Fixtures/editor_save_matrix.json"
)
OUT_OF_BOUNDS = "TIMELINE_OUT_OF_BOUNDS"


@dataclass(frozen=True)
class Expect:
    """What the contract requires of one row. Unset fields are not checked."""

    ok: bool | None = None
    status: int | None = None
    code: str | None = None
    detail_has: dict = field(default_factory=dict)
    detail_keys: tuple[str, ...] = ()
    error_contains: str | None = None
    error_types: tuple[type, ...] = ()
    order_ids: list[str] | None = None
    order_required: bool | None = None
    authored: bool | None = None
    clip_count: int | None = None
    text_ids: tuple[str, ...] = ()
    texts_fit_recipe: bool = False
    requires: tuple[tuple[str, str, float | None], ...] = ()
    requires_no_role: tuple[str, ...] = ()
    requires_no_text: tuple[str, ...] = ()
    recipe_shows: str | None = None
    check: Callable[[Outcome], None] | None = None


@dataclass(frozen=True)
class Row:
    id: str
    archetype: str  # guided_authored | guided_direct | subtitled | narrated | voiceover | ...
    expect: Expect
    edit: Edit | None = None
    run: Callable[[pytest.MonkeyPatch], Outcome] | None = None  # pure-helper (unit) rows


def _assert_expect(outcome: Outcome, expect: Expect) -> None:
    if expect.ok is True:
        assert outcome.error is None, f"Save refused: {outcome.error_text}"
    if expect.ok is False:
        assert outcome.error is not None, "Save should have been refused"
    if expect.error_types:
        assert isinstance(outcome.error, expect.error_types), repr(outcome.error)
    if expect.status is not None:
        assert outcome.status == expect.status, outcome.error_text
    if expect.code is not None:
        assert isinstance(outcome.detail, dict), outcome.error_text
        assert outcome.detail.get("code") == expect.code
    for key, want in expect.detail_has.items():
        assert outcome.detail[key] == want, key
    for key in expect.detail_keys:
        assert outcome.detail.get(key), key
    if expect.error_contains is not None:
        assert expect.error_contains in outcome.error_text
    if outcome.error is not None:
        return
    if expect.order_ids is not None:
        assert outcome.contract["order_ids"] == expect.order_ids
    if expect.order_required is not None:
        assert outcome.contract["order_required"] is expect.order_required
    if expect.authored is not None:
        assert outcome.prep["authored_timeline"] is expect.authored
    if expect.clip_count is not None:
        assert len(outcome.recipe.tracks[0].clips) == expect.clip_count
    if expect.text_ids:
        assert {t["id"] for t in outcome.variant["text_elements"]} >= set(expect.text_ids)
    if expect.texts_fit_recipe:
        recipe = outcome.recipe
        assert recipe.text_layers, "the authored cut must still carry its text"
        assert max(layer.end for layer in recipe.text_layers) <= recipe.duration
        assert recipe.duration == pytest.approx(outcome.total)
    saved = (
        outcome.saved_requirements
        if expect.requires or expect.requires_no_role or expect.requires_no_text
        else []
    )
    for need in expect.requires:
        assert need in saved
    for role in expect.requires_no_role:
        assert not any(r == role for r, _, _ in saved), f"{role!r} rule survived the manual edit"
    for text in expect.requires_no_text:
        assert all(t != text for _, t, _ in saved), f"{text!r} is still required"
    if expect.recipe_shows is not None:
        assert any(
            expect.recipe_shows in "".join(run.text for run in layer.runs)
            for layer in outcome.recipe.text_layers
        )
    if expect.check is not None:
        expect.check(outcome)


# --- row-specific checks ---------------------------------------------------------------------


def _old_clock_fitted(outcome: Outcome) -> None:
    assert outcome.total == pytest.approx(22.5)
    by_id = {t["id"]: t for t in outcome.variant["text_elements"]}
    # Wholly past the new end, but its clip survived: it follows that clip (was 22.5-30).
    assert by_id["clip-label-cut-3"]["start_s"] == pytest.approx(15.0, abs=0.05)
    assert by_id["clip-label-cut-3"]["end_s"] == pytest.approx(22.5, abs=0.05)
    assert by_id["clip-label-cut-2"]["end_s"] == pytest.approx(15.0, abs=0.05)
    # The whole-video pin is clamped, never moved or dropped.
    pin = by_id["guided-pinned-0"]
    assert (pin["start_s"], pin["end_s"]) == (0.0, 22.5)
    assert all(t["end_s"] <= outcome.total + 1e-6 for t in outcome.variant["text_elements"])
    assert max(layer.end for layer in outcome.recipe.text_layers) <= outcome.recipe.duration


def _null_role_accepted(outcome: Outcome) -> None:
    created = next(t for t in outcome.variant["text_elements"] if t["id"] == NULL_ROLE_TEXT_ID)
    # null means "no role" -> the same default an absent role gets, never a new value.
    assert created["role"] == "generative_intro"
    assert created["text"] == NULL_ROLE_TEXT_ID


def _first_kept_slot(outcome: Outcome) -> dict:
    slots = outcome.variant["user_timeline"]["slots"]
    return next(s for s in slots if not s.get("removed"))


def _window_check(overshoot: float) -> Callable[[Outcome], None]:
    def check(outcome: Outcome) -> None:
        saved = _first_kept_slot(outcome)
        posted = SOURCE_S + overshoot - 1.0
        if overshoot > 0.001:  # shortened onto the footage
            assert saved["in_s"] + saved["duration_s"] <= SOURCE_S
            assert saved["duration_s"] == pytest.approx(SOURCE_S - 1.0, abs=1e-5)
            assert saved["duration_s"] < posted
        else:  # frame-rounding overshoot is persisted exactly as posted
            assert saved["duration_s"] == pytest.approx(posted)

    return check


def _bounds_diagnostics(outcome: Outcome) -> None:
    detail = outcome.detail
    assert detail["clip_index"] == 0
    assert detail["in_s"] == 1.0
    assert detail["source_duration_s"] == SOURCE_S
    assert detail["overshoot_s"] == pytest.approx(0.06, abs=1e-3)
    assert detail["rate"] == 1.0
    assert "slot_id" in detail and "slot_order" in detail


def _strict_verifier_refuses(outcome: Outcome) -> None:
    """Only the manual Save relaxes a role: the shared verifier keeps refusing it."""
    from app.services.creator_render_contract import CreatorRenderContract, verify_phone_recipe

    strict = CreatorRenderContract.model_validate(
        outcome.job.assembly_plan[CONTRACT_REVISIONS_FIELD][VARIANT]
    ).rebind(exact_texts=requirements())
    with pytest.raises(TextPlacementError) as raised:
        verify_phone_recipe(strict, outcome.recipe, source_audio={})
    assert raised.value.field_path == "opening_title"


def _duration_follows_trim(outcome: Outcome) -> None:
    assert outcome.contract["duration_s"] == pytest.approx(outcome.total)


def _original_audio_cleared(outcome: Outcome) -> None:
    assert outcome.contract["original_audio"] is None


def _original_audio_relaxed(outcome: Outcome) -> None:
    _original_audio_cleared(outcome)
    assert {"requirement": "original_audio"} in outcome.prep["requirements_relaxed"]


def _caption_text_saved(want: str) -> Callable[[Outcome], None]:
    def check(outcome: Outcome) -> None:
        variant = outcome.job.assembly_plan["variants"][0]
        request = device_status(outcome.job, outcome.value["variant_id"]).request
        assert request.identity.recipe_revision == 2
        assert variant["render_status"] == "awaiting_device"
        assert variant["caption_cues"][0]["text"] == want
        layers = request.recipe.text_layers
        assert any(want in " ".join(run.text for run in layer.runs) for layer in layers)

    return check


def _reason_mentions(outcome: Outcome, text: str) -> None:
    assert text in str(outcome.detail)


def _nothing_recompiled(outcome: Outcome) -> None:
    assert (
        device_status(outcome.job, outcome.value["variant_id"]).request.identity.recipe_revision
        == 1
    )


def _slides_untouched(outcome: Outcome) -> None:
    variant = outcome.job.assembly_plan["variants"][0]
    assert variant["render_status"] == "ready"
    assert "text_elements" not in variant


# --- pure-helper rows (the seam a Save leans on, pinned where it can be decided) --------------

_FIT_ROWS = [
    {"slot_id": f"s{i}", "media_id": f"m{i}", "in_s": 0.0, "duration_s": 10.0, "clip_index": i}
    for i in range(3)
]


def _fit(elements: list[dict]) -> Callable[[pytest.MonkeyPatch], Outcome]:
    def run(_monkeypatch) -> Outcome:
        fitted = fit_text_to_timeline(
            elements,
            old_rows=_FIT_ROWS,
            new_rows=[r for r in _FIT_ROWS if r["slot_id"] != "s1"],
        )
        return Outcome(value={"before": elements, "after": fitted})

    return run


def _fit_drops_deleted_footage_only(outcome: Outcome) -> None:
    after = outcome.value["after"]
    assert [t["id"] for t in after] == ["clip-label-s2"]
    assert (after[0]["start_s"], after[0]["end_s"]) == (10.0, 20.0)


def _fit_returns_same_objects(outcome: Outcome) -> None:
    assert outcome.value["after"][0] is outcome.value["before"][0]


def _fit_clamps(outcome: Outcome) -> None:
    fitted = outcome.value["after"][0]
    assert (fitted["start_s"], fitted["end_s"]) == (2.0, 20.0)


def _fit_leaves_own_clock(outcome: Outcome) -> None:
    assert outcome.value["after"] == outcome.value["before"]


_TEXT = {"id": "a", "text": "hi", "start_s": 0.0, "end_s": 2.0}


def _schema(raw: dict, *, plain: dict | None = None) -> Callable[[pytest.MonkeyPatch], Outcome]:
    def run(_monkeypatch) -> Outcome:
        parsed = TextElement.model_validate(raw).model_dump()
        reference = TextElement.model_validate(plain if plain is not None else _TEXT).model_dump()
        return Outcome(value={"parsed": parsed, "reference": reference})

    return run


def _schema_same_as_reference(outcome: Outcome) -> None:
    assert outcome.value["parsed"] == outcome.value["reference"]


def _unknown_role_run(_monkeypatch) -> Outcome:
    return Outcome(value=coerce_text_elements([{**_TEXT, "role": "bogus"}]))


def _is_rejected(outcome: Outcome) -> None:
    assert outcome.value is None


def _rate_run(monkeypatch) -> Outcome:
    """Speed is not enabled on the phone pilot, so this one drives the resolver itself."""
    from app.services.authored_editor import resolve_authored_phone_slots

    job, variant, _ = order_required_job(monkeypatch)
    row = gj.editor_deletion_timeline(job, variant)[0]
    slot = gj.TimelineSlotEdit(
        slot_id=row["slot_id"],
        clip_index=row["clip_index"],
        in_s=1.0,
        duration_s=(SOURCE_S + 0.04 - 1.0) / 2.0,
        playback_rate=2.0,
        transition_after="cut",
    )
    [saved] = resolve_authored_phone_slots(job, variant, [slot])
    return Outcome(value=saved)


def _rate_respected(outcome: Outcome) -> None:
    saved = outcome.value
    assert saved["in_s"] + saved["duration_s"] * 2.0 <= SOURCE_S + 1e-9
    assert saved["duration_s"] == pytest.approx((SOURCE_S - 1.0) / 2.0, abs=1e-5)


# --- the rows ----------------------------------------------------------------------------------


def _manual(**changes) -> Edit:
    """An authored-flow Save carrying the approved text rules (manual edits-win rows)."""
    changes.setdefault("requirements", requirements())
    return Edit(**changes)


def _delete_row(media_id: str) -> Row:
    return Row(
        f"delete-{media_id}-order-required",
        "guided_authored",
        Expect(
            ok=True,
            order_required=True,
            order_ids=[m for m in ORDER if m != media_id],
            authored=True,
            clip_count=len(ORDER) - 1,
        ),
        Edit(deleted=media_id),
    )


def _retimed_row(clip_s: float) -> Row:
    return Row(
        f"retimed-lane-total-{clip_s}",
        "guided_authored",
        Expect(
            ok=True,
            text_ids=("clip-label-cut-3", "guided-pinned-0", "kria-chat-note"),
            texts_fit_recipe=True,
        ),
        Edit(clip_s=clip_s, texts=phone_retimed_lane),
    )


def _overshoot_row(overshoot: float) -> Row:
    return Row(
        f"overshoot-{overshoot}s",
        "guided_authored",
        Expect(ok=True, check=_window_check(overshoot)),
        Edit(first_slot=FirstSlot(overshoot=overshoot)),
    )


def _null_field_row(name: str) -> Row:
    return Row(
        f"null-defaulted-field-{name}",
        "unit",
        Expect(check=_schema_same_as_reference),
        run=_schema({**_TEXT, name: None}),
    )


def _fit_own_clock_row(name: str, element: dict) -> Row:
    return Row(
        f"fit-own-clock-{name}",
        "unit",
        Expect(check=_fit_leaves_own_clock),
        run=_fit([{"start_s": 25.0, "end_s": 30.0, **element}]),
    )


ROWS: list[Row] = [
    # 1. clip delete, reorder, split ------------------------------------------------------------
    _delete_row("m0"),
    _delete_row("m1"),
    _delete_row("m3"),
    Row(
        "reorder-reversed-cut",
        "guided_authored",
        # The reversed cut is approved against ITS order, never against the capture order.
        Expect(ok=True, order_ids=["m3", "m2", "m0"]),
        Edit(reorder=True),
    ),
    Row(
        "unidentified-slot-order-required",
        "guided_authored",
        Expect(
            ok=False,
            error_types=(CreatorRenderContractError, HTTPException),
            error_contains="source-identified",
        ),
        Edit(drop_media_id=True),
    ),
    Row(
        "delete-order-not-required",
        "guided_authored",
        Expect(ok=True, order_required=False),
        Edit(order_required=False),
    ),
    Row(
        "split-keeps-required-order",
        "guided_authored",
        # Five pieces of four sources: the required order is the four SOURCES, in order.
        Expect(ok=True, authored=True, clip_count=5, order_ids=ORDER),
        Edit(deleted=None, split=True),
    ),
    # 2. text vs the new timeline length --------------------------------------------------------
    *[_retimed_row(s) for s in (7.5, 7.4333333, 7.4999999, 7.4916667)],
    Row(
        "old-clock-lane-fitted",
        "guided_authored",
        Expect(ok=True, check=_old_clock_fitted),
        Edit(texts=old_clock_lane),
    ),
    Row(
        "fit-text-of-deleted-footage-goes-with-it",
        "unit",
        Expect(check=_fit_drops_deleted_footage_only),
        run=_fit(
            [
                {"id": "clip-label-s1", "start_s": 10.0, "end_s": 20.0, "text": "gone"},
                {"id": "clip-label-s2", "start_s": 20.0, "end_s": 30.0, "text": "tail"},
            ]
        ),
    ),
    Row(
        "fit-already-fitting-text-untouched",
        "unit",
        Expect(check=_fit_returns_same_objects),
        run=_fit([{"id": "free", "start_s": 1.0, "end_s": 20.0, "text": "x"}]),
    ),
    Row(
        "fit-small-overshoot-clamped",
        "unit",
        Expect(check=_fit_clamps),
        run=_fit([{"id": "free-text", "start_s": 2.0, "end_s": 20.05, "text": "x"}]),
    ),
    _fit_own_clock_row(
        "caption",
        {"id": "caption-1", "text": "c", "source_params": {"source": "caption_cue"}},
    ),
    _fit_own_clock_row("lyric-id", {"id": "lyric_3", "text": "l"}),
    _fit_own_clock_row("lyric-role", {"id": "n1", "text": "n", "role": "lyric_line"}),
    _fit_own_clock_row(
        "narration-score",
        {"id": "score", "text": "s", "source_params": {"narration_label_kind": "score"}},
    ),
    # 3. text schema: the iOS editor encodes `role: null` ---------------------------------------
    Row(
        "new-text-null-role",
        "guided_authored",
        Expect(ok=True, check=_null_role_accepted),
        Edit(texts=null_role_lane),
    ),
    Row(
        "null-role-equals-absent-role-absent",
        "unit",
        Expect(check=_schema_same_as_reference),
        run=_schema(dict(_TEXT)),
    ),
    Row(
        "null-role-equals-absent-role-none",
        "unit",
        Expect(check=_schema_same_as_reference),
        run=_schema({**_TEXT, "role": None}),
    ),
    Row("unknown-role-rejected", "unit", Expect(check=_is_rejected), run=_unknown_role_run),
    *[
        _null_field_row(name)
        for name in ("role", "position", "wrap_lines", "removed", "behind_subject")
    ],
    # 4. source-window bounds -------------------------------------------------------------------
    *[_overshoot_row(o) for o in (0.0003, 0.02, 0.05)],
    Row("overshoot-rate-2x", "unit", Expect(check=_rate_respected), run=_rate_run),
    Row(
        "overshoot-0.06s-refused",
        "guided_authored",
        Expect(ok=False, status=422, code=OUT_OF_BOUNDS, check=_bounds_diagnostics),
        Edit(first_slot=FirstSlot(overshoot=0.06)),
    ),
    Row(
        "in-point-past-end-refused",
        "guided_authored",
        Expect(ok=False, status=422, code=OUT_OF_BOUNDS),
        Edit(first_slot=FirstSlot(in_s=SOURCE_S + 0.01, duration_s=0.3)),
    ),
    Row(
        "below-minimum-length-refused",
        "guided_authored",
        # 40 ms of footage left: the shortened slot would be under the 0.1 s minimum.
        Expect(ok=False, status=422, code=OUT_OF_BOUNDS),
        Edit(first_slot=FirstSlot(in_s=SOURCE_S - 0.04, duration_s=0.5)),
    ),
    # 5. the creator's manual edits win over approved text-role rules ---------------------------
    Row(
        "opening-unmoved",
        "guided_authored",
        Expect(ok=True, requires=(("opening", TITLE, None),)),
        _manual(texts=title_edit_lane(start_s=0.0, end_s=2.2)),
    ),
    Row(
        "opening-moved-later",
        "guided_authored",
        # Never to nothing: the words are still a required, verified presence.
        Expect(
            ok=True,
            requires=(("any", TITLE, None),),
            requires_no_role=("opening",),
            recipe_shows=TITLE,
        ),
        _manual(texts=title_edit_lane(start_s=3.0, end_s=6.0)),
    ),
    Row(
        "opening-nudged-off-zero",
        "guided_authored",
        Expect(
            ok=True,
            requires=(("any", TITLE, None),),
            requires_no_role=("opening",),
            recipe_shows=TITLE,
        ),
        _manual(texts=title_edit_lane(start_s=1.0, end_s=2.2)),
    ),
    Row(
        "opening-shortened",
        "guided_authored",
        Expect(ok=True, requires=(("any", TITLE, None),), recipe_shows=TITLE),
        _manual(texts=title_edit_lane(start_s=0.0, end_s=0.6, text=TITLE)),
    ),
    Row(
        "opening-held-shorter-than-approved",
        "guided_authored",
        Expect(ok=True, requires=(("any", TITLE, None),), requires_no_role=("opening",)),
        _manual(
            requirements=requirements(
                opening=TextRequirement(role="opening", text=TITLE, duration_s=2.0)
            ),
            texts=title_edit_lane(start_s=0.0, end_s=1.0),
        ),
    ),
    Row(
        "opening-reworded-and-moved",
        "guided_authored",
        Expect(
            ok=True,
            requires=(("any", "Lisbon, finally", None),),
            requires_no_text=(TITLE,),
        ),
        _manual(texts=title_edit_lane(start_s=2.0, end_s=5.0, text="Lisbon, finally")),
    ),
    Row(
        "closing-moved-off-last-frame",
        "guided_authored",
        Expect(ok=True, requires=(("any", "Place 3", None),), requires_no_role=("closing",)),
        _manual(
            requirements=requirements(closing=TextRequirement(role="closing", text="Place 3")),
            texts=closing_moved_lane,
        ),
    ),
    Row(
        "text-deleted-by-creator",
        "guided_authored",
        Expect(ok=True, requires_no_text=("Place 2",)),
        _manual(texts=label_deleted_lane),
    ),
    Row(
        "words-missing-from-screen-refused",
        "guided_authored",
        # Manual edits win over WHERE a text sits, never over the words being shown at all.
        Expect(
            ok=False,
            status=422,
            code="unsupported_phone_edit",
            detail_keys=("field_path", "decline_reason"),
        ),
        _manual(texts=phone_retimed_lane, smuggled_requirement="never shown"),
    ),
    Row(
        "strict-verifier-for-automatic-callers",
        "guided_authored",
        Expect(ok=True, check=_strict_verifier_refuses),
        _manual(texts=title_edit_lane(start_s=3.0, end_s=6.0)),
    ),
    Row(
        "manual-trim-moves-length-target",
        "guided_authored",
        Expect(ok=True, check=_duration_follows_trim),
        _manual(texts=title_edit_lane(start_s=0.0, end_s=2.2), clip_s=3.0, duration_s=30.0),
    ),
    Row(
        "guided-opening-moved",
        "guided_direct",
        Expect(ok=True, requires=(("any", TITLE, None),), requires_no_role=("opening",)),
        _manual(flow="direct", deleted=None, title={"start_s": 3.0, "end_s": 5.0}),
    ),
    Row(
        "guided-shot-label-moved-out-of-shot",
        "guided_direct",
        Expect(ok=True, requires=(("any", "Place 2", None),), requires_no_role=("clip",)),
        Edit(
            flow="direct",
            deleted=None,
            requirements=requirements(
                any_label=TextRequirement(role="clip", text="Place 2", media_id="m2", shot_index=2)
            ),
            labels={"clip-label-cut-2": {"start_s": 1.0, "end_s": 3.0}},
            with_timeline=False,
        ),
    ),
    Row(
        "mute-all-clears-required-original-audio",
        "guided_direct",
        Expect(ok=True, check=_original_audio_relaxed),
        _manual(
            flow="direct", deleted=None, contract_changes={"original_audio": "require"}, muted=True
        ),
    ),
    Row(
        "unmute-clears-forbidden-original-audio",
        "guided_direct",
        Expect(ok=True, check=_original_audio_cleared),
        _manual(
            flow="direct", deleted=None, contract_changes={"original_audio": "forbid"}, muted=False
        ),
    ),
    # 6. other archetypes: a text-only Save --------------------------------------------------------
    Row(
        "talking-caption-text-save",
        "subtitled",
        Expect(ok=True, check=_caption_text_saved("Reworded caption")),
    ),
    Row(
        "narrated-caption-text-save",
        "narrated",
        Expect(ok=True, check=_caption_text_saved("First we pack light")),
    ),
    Row(
        "voiceover-montage-text-save",
        "voiceover",
        # A montage voiceover has no caption base to edit: refused with the editor's own reason.
        Expect(
            ok=False,
            status=422,
            error_contains="Captions can be selected and edited",
            check=_nothing_recompiled,
        ),
    ),
    Row(
        "speech-montage-save-refused",
        "speech_montage",
        Expect(
            ok=False,
            status=422,
            code="unsupported_phone_edit",
            check=lambda o: (
                _nothing_recompiled(o),
                _reason_mentions(o, "spoken-excerpt montage"),
            ),
        ),
    ),
    Row(
        "slides-save-refused",
        "slides",
        # The capability map closes every lane for a slide post; Save agrees (KRI-750 fix).
        Expect(ok=False, status=422, code="slide_post_edit_unsupported", check=_slides_untouched),
    ),
]


def _run(monkeypatch, row: Row) -> Outcome:
    if row.run is not None:
        return row.run(monkeypatch)
    if row.archetype in {"guided_authored", "guided_direct"}:
        assert row.edit is not None
        return run_save(monkeypatch, row.edit)
    return archetype_save(monkeypatch, row.archetype)


def test_row_ids_are_unique() -> None:
    ids = [row.id for row in ROWS]
    assert len(ids) == len(set(ids))


@pytest.mark.parametrize("row", ROWS, ids=[row.id for row in ROWS])
def test_editor_save_contract(monkeypatch, row: Row) -> None:
    _assert_expect(_run(monkeypatch, row), row.expect)


# --- the iOS fixture ---------------------------------------------------------------------------

_BOUND_CASES: list[tuple[str, float, float, float]] = [
    # name, in_s, duration_s (OUTPUT seconds), rate
    ("inside_the_source", 1.0, 5.0, 1.0),
    ("ends_exactly_on_the_source_end", 1.0, 9.0, 1.0),
    ("frame_rounding_overshoot_0_3ms", 1.0, 9.0003, 1.0),
    ("overshoot_20ms", 1.0, 9.02, 1.0),
    ("overshoot_exactly_the_50ms_tolerance", 1.0, 9.05, 1.0),
    ("overshoot_60ms", 1.0, 9.06, 1.0),
    ("overshoot_500ms", 1.0, 9.5, 1.0),
    ("in_point_past_the_end", 10.01, 0.3, 1.0),
    ("below_minimum_length_after_shortening", 9.96, 0.5, 1.0),
    ("shorter_than_the_minimum_length", 1.0, 0.05, 1.0),
    ("negative_in_point", -0.5, 2.0, 1.0),
    ("rate_2x_inside_the_source", 1.0, 4.0, 2.0),
    ("rate_2x_overshoot_40ms", 1.0, 4.52, 2.0),
    ("rate_2x_overshoot_200ms", 1.0, 4.6, 2.0),
    ("rate_half_inside_the_source", 1.0, 16.0, 0.5),
    ("rate_half_overshoot_200ms", 1.0, 18.4, 0.5),
]
_TEXT_ENCODE_CASES: list[tuple[str, str | None]] = [
    ("role_nil_is_sent_as_null", None),
    ("role_generative_intro", "generative_intro"),
    ("role_generative_sequence", "generative_sequence"),
    ("role_lyric_line", "lyric_line"),
]


def _server_bound_verdict(monkeypatch, in_s: float, duration_s: float, rate: float) -> dict:
    """Ask the real authored resolver about ONE slot of a 10 s source."""
    from app.services.authored_editor import resolve_authored_phone_slots

    job, variant, _ = order_required_job(monkeypatch)
    row = gj.editor_deletion_timeline(job, variant)[0]
    slot = gj.TimelineSlotEdit(
        slot_id=row["slot_id"],
        clip_index=row["clip_index"],
        in_s=in_s,
        duration_s=duration_s,
        playback_rate=rate,
        transition_after="cut",
    )
    try:
        [saved] = resolve_authored_phone_slots(job, variant, [slot])
    except HTTPException as exc:
        code = exc.detail.get("code") if isinstance(exc.detail, dict) else str(exc.detail)
        return {"verdict": "refuse", "code": code}
    shortened = saved["duration_s"] < duration_s - 1e-6
    return {"verdict": "fit" if shortened else "accept", "code": None}


def build_fixture(monkeypatch) -> dict[str, Any]:
    bounds = []
    for name, in_s, duration_s, rate in _BOUND_CASES:
        verdict = _server_bound_verdict(monkeypatch, in_s, duration_s, rate)
        bounds.append(
            {
                "name": name,
                "in_s": in_s,
                "duration_s": duration_s,
                "rate": rate,
                "source_duration_s": SOURCE_S,
                **verdict,
                # The phone asks this BEFORE it writes; it must agree with the server.
                "within_bounds": verdict["verdict"] != "refuse",
            }
        )
    text_encode = []
    for name, role in _TEXT_ENCODE_CASES:
        wire = {"id": "t-1", "text": "hello", "start_s": 1.0, "end_s": 3.0, "role": role}
        parsed = TextElement.model_validate(wire)  # the strict Save validation
        text_encode.append(
            {
                "name": name,
                "id": "t-1",
                "text": "hello",
                "start_s": 1.0,
                "end_s": 3.0,
                "role": role,
                "wire_role": role,
                "server_role": parsed.role,
            }
        )
    return {"source_duration_s": SOURCE_S, "bounds": bounds, "text_encode": text_encode}


def test_ios_fixture_is_the_server_truth(monkeypatch) -> None:
    """The checked-in fixture equals what the real server code says today.

    Fails when the server contract moved without regenerating the file iOS replays::

        cd src/apps/api && UPDATE_EDITOR_SAVE_FIXTURE=1 .venv/bin/python -m pytest \\
            tests/routes/test_editor_save_contract_matrix.py -k ios_fixture_is_the_server_truth
    """
    if os.environ.get("UPDATE_EDITOR_SAVE_FIXTURE") == "1":  # deliberate regeneration
        FIXTURE.write_text(json.dumps(build_fixture(monkeypatch), indent=2) + "\n")
    assert FIXTURE.exists(), "set UPDATE_EDITOR_SAVE_FIXTURE=1 to generate the fixture"
    assert json.loads(FIXTURE.read_text()) == json.loads(json.dumps(build_fixture(monkeypatch))), (
        "editor_save_matrix.json drifted from the Save contract; regenerate deliberately"
    )


def test_ios_fixture_pins_the_incident_cases(monkeypatch) -> None:
    """The cases KRI-736 shipped for must be in the fixture with the verdicts that fixed them."""
    cases = {c["name"]: c for c in build_fixture(monkeypatch)["bounds"]}
    assert cases["overshoot_exactly_the_50ms_tolerance"]["verdict"] == "fit"
    assert cases["overshoot_60ms"]["verdict"] == "refuse"
    assert cases["overshoot_60ms"]["code"] == OUT_OF_BOUNDS
    assert cases["in_point_past_the_end"]["verdict"] == "refuse"
    encode = {c["name"]: c for c in build_fixture(monkeypatch)["text_encode"]}
    assert encode["role_nil_is_sent_as_null"]["server_role"] == "generative_intro"
