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
    TALKING_CUES,
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
    talking_segment_save,
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
        # A refused Save still owes its row-specific proof (diagnostics, "nothing
        # recompiled", "slides untouched"); only the success-shape asserts below are skipped.
        if expect.check is not None:
            expect.check(outcome)
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


# --- phone Talking speech segments after the first Delete (KRI-852) ------------------------
# Output clock of the cleaned take: segment windows [0, 2), [2, 5), [5, 8) carry source
# [0, 2), [3, 6), [7, 10); cues "one" 0-1.5, "two" 2-4.5, "three" 5-7.5 (TALKING_CUES).


def _saved_talking_windows(outcome: Outcome) -> list[tuple[str, float, float]]:
    slots = outcome.variant["user_timeline"]["slots"]
    return [(s["slot_id"], s["in_s"], s["duration_s"]) for s in slots if not s.get("removed")]


def _saved_talking_cues(outcome: Outcome) -> list[tuple[str, float, float]]:
    return [
        (c["text"], round(c["start_s"], 3), round(c["end_s"], 3))
        for c in outcome.variant["caption_cues"]
    ]


def _talking_delete_moves_later_captions(outcome: Outcome) -> None:
    # The kept segments keep their source windows; the deleted segment's caption is gone and
    # the caption after it moved up with its clip (5-7.5 -> 2-4.5). Nothing stays on the
    # pre-delete clock.
    assert _saved_talking_windows(outcome) == [
        ("native-phone-talking-source", 0.0, 2.0),
        ("native-phone-talking-source-2", 7.0, 3.0),
    ]
    assert _saved_talking_cues(outcome) == [("one", 0.0, 1.5), ("three", 2.0, 4.5)]
    assert not outcome.variant.get("speech_timing_unresolved")
    assert len(TALKING_CUES) == 3


def _talking_range_saves_and_clips_caption(outcome: Outcome) -> None:
    # The Range sheet shortened the last segment to source [7.5, 10): its caption keeps only
    # the spoken part still in the window (source 7.5-9.5 -> output 2.0-4.0).
    assert _saved_talking_windows(outcome) == [
        ("native-phone-talking-source", 0.0, 2.0),
        ("native-phone-talking-source-2", 7.5, 2.5),
    ]
    assert _saved_talking_cues(outcome) == [("one", 0.0, 1.5), ("three", 2.0, 4.0)]
    assert not outcome.variant.get("speech_timing_unresolved")


def _talking_slide_saves_and_flags_untranscribed_speech(outcome: Outcome) -> None:
    # "Slide source later" moved the first segment to source [0.5, 2.5). The Save is kept
    # exactly; the caption follows (source 0.5-1.5 -> output 0-1.0); the newly exposed half
    # second has no transcript here, so export asks for captions or the previous range
    # (`speech_timing_unresolved`, the genuine protection #1770 keeps).
    assert _saved_talking_windows(outcome) == [
        ("native-phone-talking-source", 0.5, 2.0),
        ("native-phone-talking-source-2", 7.0, 3.0),
    ]
    assert _saved_talking_cues(outcome) == [("one", 0.0, 1.0), ("three", 2.0, 4.5)]
    assert outcome.variant.get("speech_timing_unresolved") is True


def _talking_reorder_moves_captions_with_clips(outcome: Outcome) -> None:
    # The swap is saved and every caption travels with its source occurrence: "three" now
    # opens the video, "one" plays over the second clip. No word is left on the old clock.
    assert [s[0] for s in _saved_talking_windows(outcome)] == [
        "native-phone-talking-source-2",
        "native-phone-talking-source",
    ]
    assert _saved_talking_cues(outcome) == [("three", 0.0, 2.5), ("one", 3.0, 4.5)]
    assert not outcome.variant.get("speech_timing_unresolved")


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


def _prepare_raises_run(exc: BaseException) -> Callable[[pytest.MonkeyPatch], Outcome]:
    """A Save whose staged prepare raises a non-HTTP error (a state the catalog cannot read)."""

    def run(monkeypatch) -> Outcome:
        job, variant, revision = order_required_job(monkeypatch)

        def boom(*_args, **_kwargs):
            raise exc

        monkeypatch.setattr(gj, "_prepare_editor_commit", boom)
        body = gj.EditorCommitRequest(
            base_generation=gj.variant_render_baseline(variant),
            editor_state_version=1,
            guided_revision_number=revision["revision_number"],
            text_elements=variant["text_elements"],
        )
        outcome = Outcome(job=job)
        try:
            outcome.prep = gj.prepare_editor_commit(job, VARIANT, body)
        except Exception as raised:  # noqa: BLE001
            outcome.error = raised
        return outcome

    return run


def _capabilities_run(monkeypatch) -> Outcome:
    job, variant, _ = order_required_job(monkeypatch)
    return Outcome(job=job, value=gj._compute_base_editor_capabilities(job, variant))


def _speed_not_advertised_on_device(outcome: Outcome) -> None:
    speed = outcome.value["clips"]["playback_rate"]
    assert speed["editable"] is False
    assert speed["reason"] == "phone_playback_rate_unsupported"
    # The other clip operations stay open: only the unsupported lane closes.
    assert outcome.value["clips"]["trim"]["editable"] is True


def _trim_min_trailing_row(in_s: float, flow: str = "authored") -> Row:
    """The phone's trailing-edge drag to the 0.1 s minimum encodes ``(in_s + 0.1) - in_s``."""
    duration = (in_s + 0.1) - in_s
    if flow == "authored":
        return Row(
            f"trim-min-trailing[in_s={in_s}]",
            "guided_authored",
            Expect(ok=True, check=_kept_window(in_s)),
            Edit(first_slot=FirstSlot(in_s=in_s, duration_s=duration)),
        )
    return Row(
        f"direct-trim-min-trailing[in_s={in_s}]",
        "guided_direct",
        Expect(ok=True),
        Edit(flow="direct", first_slot=FirstSlot(in_s=in_s, duration_s=duration)),
    )


def _new_placement_got_a_permanent_id(minted: str) -> Callable[[Outcome], None]:
    def check(outcome: Outcome) -> None:
        saved = _first_kept_slot(outcome)
        assert saved["slot_id"] and saved["slot_id"] != minted, saved["slot_id"]
        assert not saved["slot_id"].startswith("cs-")

    return check


def _kept_window(in_s: float) -> Callable[[Outcome], None]:
    def check(outcome: Outcome) -> None:
        saved = _first_kept_slot(outcome)
        assert saved["in_s"] == pytest.approx(in_s, abs=1e-6)
        assert saved["duration_s"] == pytest.approx(0.1, abs=1e-6)

    return check


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
    # 4b. trim windows the phone can produce (KRI-770) -------------------------------------------
    *[_trim_min_trailing_row(i) for i in (0.4, 0.5, 0.9, 5.0, 7.0)],
    Row(
        "trim-min-exact",
        "guided_authored",
        Expect(ok=True, check=_kept_window(0.9)),
        Edit(first_slot=FirstSlot(in_s=0.9, duration_s=0.1)),
    ),
    Row(
        "trim-below-min-real",
        "guided_authored",
        # A real 90 ms window is still refused: the float tolerance must not swallow it.
        Expect(ok=False, status=422, code="TIMELINE_INVALID_WINDOW"),
        Edit(first_slot=FirstSlot(in_s=0.9, duration_s=0.09)),
    ),
    *[_trim_min_trailing_row(i, "direct") for i in (0.4, 0.9, 5.0)],
    Row(
        "direct-trim-shortens-clip",
        "guided_direct",
        Expect(ok=True, texts_fit_recipe=True),
        Edit(flow="direct", first_slot=FirstSlot(in_s=1.0, duration_s=3.0)),
    ),
    # The phone's story compiler has no retime, so speed is not an advertised lane there: the
    # capability map (which the chat copilot and the app read) must say so instead of offering
    # a change every Save refuses.
    Row(
        "device-guided-story-does-not-advertise-speed",
        "unit",
        Expect(check=_speed_not_advertised_on_device),
        run=_capabilities_run,
    ),
    # A trimmed total under the planner's 3 s floor is the creator's call: the floor guards AI
    # proposals (EditProposalSnapshot), never a manual Save. Troya's sibling job failed at
    # generation from a 2.987 s plan, not from a Save.
    Row(
        "authored-total-under-planner-floor",
        "guided_authored",
        Expect(ok=True, texts_fit_recipe=True),
        Edit(clip_s=0.5, texts=phone_retimed_lane),
    ),
    Row(
        "direct-total-under-planner-floor",
        "guided_direct",
        Expect(ok=True),
        Edit(flow="direct", first_slot=FirstSlot(in_s=1.0, duration_s=0.5)),
    ),
    # 8b. ids the phone sends for a clip the server has no record of (KRI-773) --------------------
    # Before #1698 the chat compile minted `cs-<state>-<n>` for any clip without an id (Troya's
    # restored car clip). Those ids were stored in the draft the phone opens, and every Save then
    # failed 409 TIMELINE_STALE; "Reload and keep my edits" re-applied the same local timeline.
    Row(
        "legacy-minted-slot-id-is-a-new-placement",
        "guided_authored",
        Expect(ok=True, check=_new_placement_got_a_permanent_id("cs-369BA950-52A-27")),
        Edit(first_slot=FirstSlot(in_s=1.0, duration_s=2.0, slot_id="cs-369BA950-52A-27")),
    ),
    Row(
        "unknown-real-looking-slot-id-is-still-stale",
        "guided_authored",
        Expect(ok=False, status=409, code="TIMELINE_STALE"),
        Edit(first_slot=FirstSlot(in_s=1.0, duration_s=2.0, slot_id="unified-cut-99")),
    ),
    # 9. a Save never answers 500 for a state it can name (KRI-770) -------------------------------
    Row(
        "unreadable-source-catalog-is-409",
        "unit",
        Expect(ok=False, status=409, code="editor_source_catalog_invalid"),
        run=_prepare_raises_run(ValueError("editor_source_catalog_invalid")),
    ),
    Row(
        "source-identity-conflict-is-409",
        "unit",
        Expect(ok=False, status=409, code="editor_source_catalog_invalid"),
        run=_prepare_raises_run(ValueError("editor_source_identity_conflict")),
    ),
    Row(
        "index-error-in-prepare-is-422",
        "unit",
        Expect(ok=False, status=422, code="unsupported_phone_edit"),
        run=_prepare_raises_run(IndexError("list index out of range")),
    ),
    Row(
        "key-error-in-prepare-is-422",
        "unit",
        Expect(ok=False, status=422, code="unsupported_phone_edit"),
        run=_prepare_raises_run(KeyError("sources")),
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
    # 12. a phone Talking speech segment after the first Delete (KRI-852) -------------------------
    # The Delete makes the timeline authored and editable. Every window or order change on a
    # speech segment saves, and captions follow their source occurrence (no 409, no stale clock).
    Row(
        "talking-segment-delete-moves-later-captions",
        "talking_authored",
        Expect(ok=True, authored=True, check=_talking_delete_moves_later_captions),
        run=lambda monkeypatch: talking_segment_save(monkeypatch, "none"),
    ),
    Row(
        "talking-segment-range-saves-and-clips-caption",
        "talking_authored",
        Expect(ok=True, authored=True, check=_talking_range_saves_and_clips_caption),
        run=lambda monkeypatch: talking_segment_save(monkeypatch, "range"),
    ),
    Row(
        "talking-segment-slide-saves-and-flags-untranscribed-speech",
        "talking_authored",
        Expect(ok=True, authored=True, check=_talking_slide_saves_and_flags_untranscribed_speech),
        run=lambda monkeypatch: talking_segment_save(monkeypatch, "slide"),
    ),
    Row(
        "talking-segment-reorder-moves-captions-with-clips",
        "talking_authored",
        Expect(ok=True, authored=True, check=_talking_reorder_moves_captions_with_clips),
        run=lambda monkeypatch: talking_segment_save(monkeypatch, "reorder"),
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


# KRI-852 failure modes and six timing decisions: docs/reviews/kri-852-speech-editor.md.
def _talking_edit_job(monkeypatch, *, job_id=None):
    import copy
    import uuid

    from app.kria.device_render import make_device_request
    from app.pipeline.phone_subtitled_plan import compile_phone_subtitled_plan
    from app.services.device_render import pin_device_request
    from app.services.phone_sources import PHONE_SOURCES_FIELD
    from tests.pipeline.test_authored_timeline import binding

    monkeypatch.setattr(gj.settings, "phone_rendering_enabled", True)
    monkeypatch.setattr(gj.settings, "GENERATIVE_TIMELINE_EDITOR_ENABLED", True)
    monkeypatch.setattr(
        gj.settings,
        "phone_render_verified_features",
        [
            "basicComposition",
            "local1080Export",
            "audioMix",
            "positionedText",
            "animatedText",
            "authoredText",
        ],
    )
    from app.services.phone_rollout import PHONE_SUBTITLED_EDITOR_FEATURES

    monkeypatch.setattr(gj.settings, "phone_subtitled_editor_lanes_enabled", True)
    monkeypatch.setattr(gj.settings, "phone_subtitled_media_lanes_enabled", True)
    monkeypatch.setattr(
        gj.settings,
        "phone_render_verified_features",
        list(
            set(gj.settings.phone_render_verified_features) | set(PHONE_SUBTITLED_EDITOR_FEATURES)
        ),
    )
    source = binding("old")
    cues = [
        {
            "id": "a",
            "start_s": 0,
            "end_s": 2,
            "text": "Alpha",
            "words": [{"text": "Alpha", "start_s": 0.2, "end_s": 1.5}],
        },
        {
            "id": "b",
            "start_s": 2,
            "end_s": 4,
            "text": "Bravo",
            "words": [{"text": "Bravo", "start_s": 2.2, "end_s": 3.5}],
        },
        {
            "id": "c",
            "start_s": 4,
            "end_s": 8,
            "text": "Charlie Delta",
            "words": [
                {"text": "Charlie", "start_s": 4.2, "end_s": 5},
                {"text": "Delta", "start_s": 6.2, "end_s": 7.5},
            ],
        },
    ]
    variant = {
        "variant_id": "original_text",
        "resolved_archetype": "subtitled",
        "render_destination": "device",
        "render_status": "ready",
        "render_generation_id": "first",
        "duration_s": 8,
        "text_mode": "none",
        "caption_cues": copy.deepcopy(cues),
        "text_elements": [],
        "silence_cut": {"removed": [{"start_s": 2, "end_s": 3}, {"start_s": 5, "end_s": 8}]},
    }
    from types import SimpleNamespace

    job = SimpleNamespace(
        id=job_id or uuid.uuid4(),
        user_id=uuid.uuid4(),
        status="variants_ready",
        current_phase=None,
        all_candidates={"clip_paths": [source.proxy_path]},
        assembly_plan={
            PHONE_SOURCES_FIELD: [source.model_dump(mode="json")],
            "variants": [variant],
        },
    )
    recipe = compile_phone_subtitled_plan((source,), caption_cues=cues)
    pin_device_request(
        job,
        make_device_request(job_id=job.id, variant_id="original_text", revision=1, recipe=recipe),
        base_generation="first",
    )
    job.assembly_plan["_speech_cleanup_internal"] = {
        "preflight_snapshot": _talking_full_speech_snapshot(job)
    }
    return job


def _talking_save(job, rows, *, deletions=None, generation=None, **sections):
    from app.services.authored_editor import prepare_authored_editor_commit
    from app.services.editor_empty_drafts import stage_initial_authored_baseline

    variant = job.assembly_plan["variants"][0]
    if variant.get("editor_timeline_mode") != "authored":
        job = stage_initial_authored_baseline(
            job, "original_text", gj.editor_deletion_timeline(job, variant)
        )
    body = gj.EditorCommitRequest(
        base_generation=generation or gj.variant_render_baseline(variant),
        editor_state_version=1,
        timeline_slots=[{k: v for k, v in r.items() if v is not None} for r in rows],
        deletions=deletions or [],
        **sections,
    )
    body = gj.normalize_editor_deletion_request(job, "original_text", body)
    prepare_authored_editor_commit(job, "original_text", body, validation_arguments={})
    return job


@pytest.mark.parametrize("operation", ["trim", "slide", "reorder", "combined"])
def test_talking_delete_then_window_and_order_save(monkeypatch, operation):
    import copy

    job = _talking_edit_job(monkeypatch)
    rows = gj.editor_deletion_timeline(job, job.assembly_plan["variants"][0])
    job = _talking_save(job, rows, deletions=[{"kind": "clip", "id": rows[1]["slot_id"]}])
    rows = copy.deepcopy(job.assembly_plan["variants"][0]["user_timeline"]["slots"])
    rows = [row for row in rows if not row.get("removed")]
    if operation in {"trim", "combined"}:
        rows[1].update(in_s=10, duration_s=2, source_start_locked=True)
    if operation == "slide":
        rows[1].update(in_s=7, source_start_locked=True)
    if operation in {"reorder", "combined"}:
        rows.reverse()
    job = _talking_save(job, rows)
    variant = job.assembly_plan["variants"][0]
    active = [row for row in variant["user_timeline"]["slots"] if not row.get("removed")]
    assert [(r["in_s"], r["duration_s"]) for r in active] == [
        (r["in_s"], r["duration_s"]) for r in rows
    ]
    cues = variant["caption_cues"]
    assert not any("Bravo" in c["text"] for c in cues)
    delta = next(c for c in cues if "Delta" in c["text"])
    expected = {"trim": 2.2, "slide": 5.2, "reorder": 2.2, "combined": 0.2}[operation]
    word = next(w for w in delta["words"] if w["text"] == "Delta")
    assert word["start_s"] == pytest.approx(expected, abs=0.001)
    if operation in {"trim", "combined"}:
        assert "Charlie" not in delta["text"]
    request = device_status(job, "original_text").request
    assert request.recipe.duration == pytest.approx(sum(r["duration_s"] for r in active))
    # Same-duration wrong-segment words cannot satisfy the expected source occurrence.
    assert word["start_s"] != pytest.approx((expected + 1) % request.recipe.duration)


@pytest.mark.parametrize("operation", ["trim", "slide"])
def test_talking_source_words_return_after_save_and_reopen(monkeypatch, operation):
    import copy

    from app.services.editor_empty_drafts import editor_sections

    job = _talking_edit_job(monkeypatch)
    original = gj.editor_deletion_timeline(job, job.assembly_plan["variants"][0])
    job = _talking_save(job, original, deletions=[{"kind": "clip", "id": original[1]["slot_id"]}])
    rows = [
        copy.deepcopy(r)
        for r in job.assembly_plan["variants"][0]["user_timeline"]["slots"]
        if not r.get("removed")
    ]
    restored = copy.deepcopy(rows)
    rows[1].update(in_s=10, duration_s=2 if operation == "trim" else 4)
    if operation == "slide":
        rows[1].update(in_s=6, duration_s=4)
    job = _talking_save(job, rows)
    # Reopen uses a serialized copy, rather than an in-process reference to old cues.
    job.assembly_plan = json.loads(json.dumps(job.assembly_plan))
    sections = editor_sections(job, "original_text")
    assert "speech_timing_basis" in sections
    job = _talking_save(job, restored)
    cues = job.assembly_plan["variants"][0]["caption_cues"]
    assert next(c for c in cues if c["id"] == "c")["text"] == "Charlie Delta"


@pytest.mark.parametrize(
    "fault,code",
    [
        ("generation", "baseline_conflict"),
        ("source", "TIMELINE_STALE"),
        ("slot", "TIMELINE_STALE"),
        ("bounds", "TIMELINE_OUT_OF_BOUNDS"),
    ],
)
def test_talking_authored_negative_controls_are_atomic(monkeypatch, fault, code):
    import copy

    job = _talking_edit_job(monkeypatch)
    rows = gj.editor_deletion_timeline(job, job.assembly_plan["variants"][0])
    job = _talking_save(job, rows, deletions=[{"kind": "clip", "id": rows[1]["slot_id"]}])
    rows = [
        copy.deepcopy(r)
        for r in job.assembly_plan["variants"][0]["user_timeline"]["slots"]
        if not r.get("removed")
    ]
    before = copy.deepcopy(job.assembly_plan)
    if fault == "source":
        rows[0]["clip_index"] = 1
    if fault == "slot":
        rows[0]["slot_id"] += "-foreign"
    if fault == "bounds":
        rows[0].update(in_s=11, duration_s=2)
    with pytest.raises(HTTPException) as caught:
        _talking_save(job, rows, generation="stale" if fault == "generation" else None)
    assert caught.value.detail == code or caught.value.detail.get("code") == code
    assert job.assembly_plan == before


def _talking_full_speech_snapshot(job, *, duration=12, words=None):
    """Synthetic words, hydrated through the real persisted preflight contract."""
    import uuid

    from app.pipeline.transcribe import Word
    from tests.services.test_speech_cleanup_preflight import (
        analysis_input,
        fake_transcript,
        run_analysis,
    )

    fingerprint = "c" * 64
    result = run_analysis(
        analysis_input(
            source_fingerprint=fingerprint,
            duration_s=duration,
            source_window_start_s=0,
            source_window_end_s=duration,
        ),
        transcript=fake_transcript(
            words
            or [
                Word("Alpha", 0.2, 1.5, 1),
                Word("Hidden", 2.2, 2.7, 1),
                Word("Bravo", 3.2, 4.5, 1),
                Word("Charlie", 8.2, 9, 1),
                Word("Delta", 10.2, 11.5, 1),
            ]
        ),
        silences=(),
    )
    from app.services.phone_sources import PHONE_SOURCES_FIELD

    source = job.assembly_plan[PHONE_SOURCES_FIELD][0]
    return {
        "schema_version": 1,
        "analysis_id": str(uuid.uuid4()),
        "engine_version": "preflight-v1-2026-09-05",
        "detector_version": result.detector_version,
        "source": {
            "kind": "embedded_spine",
            "media_identity": source["media_id"],
            "storage_path": source["proxy_path"],
            "generation": source["generation"],
            "window_start_s": 0,
            "window_end_s": duration,
            "source_policy_fingerprint": fingerprint,
        },
        "analysis": result.to_payload(),
    }


@pytest.mark.parametrize("generation", ["1", "replaced"])
def test_talking_newly_exposed_words_use_only_matching_source_evidence(monkeypatch, generation):
    import copy

    job = _talking_edit_job(monkeypatch)
    snapshot = _talking_full_speech_snapshot(job)
    snapshot["source"]["generation"] = generation
    job.assembly_plan["_speech_cleanup_internal"] = {"preflight_snapshot": snapshot}
    rows = gj.editor_deletion_timeline(job, job.assembly_plan["variants"][0])
    job = _talking_save(job, rows, deletions=[{"kind": "clip", "id": rows[1]["slot_id"]}])
    rows = [
        copy.deepcopy(r)
        for r in job.assembly_plan["variants"][0]["user_timeline"]["slots"]
        if not r.get("removed")
    ]
    rows[0].update(in_s=1, source_start_locked=True)
    if generation == "replaced":
        with pytest.raises(HTTPException) as error:
            _talking_save(job, rows)
        assert error.value.detail["code"] == "unsupported_phone_edit"
        assert "caption" in error.value.detail["reason"].lower()
    else:
        job = _talking_save(job, rows)
        hidden = next(
            w
            for c in job.assembly_plan["variants"][0]["caption_cues"]
            for w in c.get("words", [])
            if w["text"] == "Hidden"
        )
        assert hidden["start_s"] == pytest.approx(1.2)


def test_talking_creator_projected_text_and_server_captions_map_once(monkeypatch):
    import copy

    job = _talking_edit_job(monkeypatch)
    variant = job.assembly_plan["variants"][0]
    variant.update(
        text_mode="agent_text",
        text_elements_user_edited=True,
        text_elements=[
            {
                "id": "on-delta",
                "text": "Before",
                "start_s": 6.2,
                "end_s": 7.5,
                "effect": "none",
                "font_family": "TikTok Sans",
                "size_px": 64,
                "position": "middle",
                "role": "generative_intro",
            }
        ],
    )
    variant["text_elements"].append(
        {
            **copy.deepcopy(variant["text_elements"][0]),
            "id": "caption-mirror",
            "source_params": {"source": "caption_cue"},
        }
    )
    rows = gj.editor_deletion_timeline(job, variant)
    projected = [
        {
            **copy.deepcopy(variant["text_elements"][0]),
            "text": "Creator changed this",
            "start_s": 4.2,
            "end_s": 5.5,
        }
    ]
    job = _talking_save(
        job, rows, deletions=[{"kind": "clip", "id": rows[1]["slot_id"]}], text_elements=projected
    )
    saved = job.assembly_plan["variants"][0]
    title = next(t for t in saved["text_elements"] if t["id"] == "on-delta")
    assert title["text"] == "Creator changed this"
    assert (title["start_s"], title["end_s"]) == (4.2, 5.5)
    delta = next(
        w for c in saved["caption_cues"] for w in c.get("words", []) if w["text"] == "Delta"
    )
    assert (delta["start_s"], delta["end_s"]) == (4.2, 5.5)

    assert not any(
        (entry["record"].get("source_params") or {}).get("source") == "caption_cue"
        for entry in saved["speech_timing_basis"].get("text_elements", [])
    )


def test_talking_maximum_slots_share_one_full_source_caption_pool(monkeypatch):
    from app.pipeline.speech_cut_state import project_speech_basis
    from app.pipeline.transcribe import Word
    from app.services.authored_editor import speech_timing_basis_for_job
    from app.services.editor_limits import EDITOR_MAX_TIMELINE_SLOTS
    from app.services.phone_sources import PHONE_SOURCES_FIELD

    job = _talking_edit_job(monkeypatch)
    job.assembly_plan[PHONE_SOURCES_FIELD][0]["original"]["duration_s"] = 300
    words = [Word(f"word{i}", i * 0.2, i * 0.2 + 0.15, 1) for i in range(1500)]
    job.assembly_plan["_speech_cleanup_internal"] = {
        "preflight_snapshot": _talking_full_speech_snapshot(job, duration=300, words=words)
    }
    slots = [
        {
            "slot_id": f"native-phone-talking-source-{i}",
            "clip_index": 0,
            "in_s": i * 2.5,
            "duration_s": 2.5,
            "transition_after": "cut",
        }
        for i in range(EDITOR_MAX_TIMELINE_SLOTS)
    ]
    variant = {**job.assembly_plan["variants"][0], "caption_cues": []}
    basis = speech_timing_basis_for_job(job, variant, slots)
    pool = basis["caption_cues"]
    assert len(pool) == 1
    assert len(pool[0]["source_groups"]) == EDITOR_MAX_TIMELINE_SLOTS
    assert sum(len(c["words"]) for c in pool[0]["source_captions"]) == 1500
    # A representation budget assertion, not a new admission limit or word cap.
    assert len(json.dumps(basis).encode()) < 300_000
    moved = [dict(row, in_s=297.5) for row in slots]
    projected = project_speech_basis(pool, moved, captions=True)
    assert projected and len(json.dumps(basis).encode()) < 300_000
    assert all(c["end_s"] <= 300 for c in projected)
