"""Leverkusen M1 run 2 #3: "Make it without these" must not turn filming order into upload order.

Prod thread 1a2189cc (2026-10-30, Leverkusen-Nürnberg DFB-Pokal, phone Montage): 8 iPhone
videos and 3 Visuals photos, every one dated. The creator asked "Put everything in the order
I filmed it" (brief r1, key ``capture_time``). Kria could not confirm that order, offered
"Make it without these" (leaves out r1 and the length), and the creator took it. The
simpler version superseded r1, the montage planner then read no order ask at all and fell
back to the order the media was added: the train ride, filmed second, played 10th of 11.
The approval card said nothing about order.

Dropping an ask in the simpler version waives its check, not the creator's wish: with no
order ask left, a filming order the creator once asked for still sets the order, and
nothing claims or verifies it.

Ways this can fail, written before the test (each is pinned below):

1. The incident: the approved job re-planned by the worker plays in upload order.
2. Clips follow filming time, but the dated photos are still spread evenly (KRI-1069
   seats them by time only under an order ask), so the display photo is not first.
3. The kept order becomes a promise again: a receipt says "in the order you filmed it",
   or the phone draws another order than the plan.
4. It overrides a later word: a hand reorder in the editor, a live "the order I added
   them" ask, or a dropped upload-order ask (the latest dropped basis was upload order).
5. A brief that never asked for filming order changes order (must stay upload order).
6. Undated footage crashes or blocks instead of keeping upload order.
7. The chat's spoken-order note (KRI-585) still calls the order "nobody's choice", so a
   reply could promise "the welcome leads" while the render plays in filming order.

Everything runs the REAL worker plan (`_plan_phone_unified_montage`, the one the
pre-approval check shares), the job's own pinned brief binding, length plan and contract,
the guided + phone compilers and the plan receipts. No model call. The expected order is
the kit's answer key (capture times of the originals), never the planner's output.
"""

from __future__ import annotations

import copy
import json
import pathlib
import uuid
from types import SimpleNamespace

import pytest

from app.config import settings
from app.kria.brief import CreativeBrief
from app.kria.brief_checks import build_receipts, plan_facts_from_unified_montage
from app.kria.narration_notes import speech_decides_order
from app.pipeline.guided_story import GuidedStoryExecutionPlan, compile_execution_plan
from app.pipeline.phone_guided_plan import compile_phone_guided_plan
from app.pipeline.unified_montage import brief_view, selected_visual_ids
from app.services.creator_render_contract import read_render_contract
from app.services.phone_sources import PhoneSourceBinding, PhoneVisualBinding
from app.tasks.generative_build import (
    _approved_length_plan_envelope,
    _plan_phone_unified_montage,
    _unified_montage_clips,
    unified_visual,
)

FIXTURE = json.loads(
    (
        pathlib.Path(__file__).resolve().parents[1]
        / "fixtures"
        / "leverkusen_m1_run2_simpler_order.json"
    ).read_text()
)
ASSEMBLY = FIXTURE["assembly_plan"]
CANDIDATES = FIXTURE["all_candidates"]
MEDIA = ASSEMBLY["creator_brief_binding"]["media_snapshot"]
FILMING_ORDER = FIXTURE["filming_order"]
SHIPPED_ORDER = FIXTURE["shipped_order"]


@pytest.fixture(autouse=True)
def _clip_intents_on(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(settings, "clip_intents_enabled", True)


def _name(media_id: str) -> str:
    """IMG_xxxx for a clip id, or a photo's row id (bare, ``asset-`` or recipe ``visual-``)."""
    for row in MEDIA["clip_assignments"]:
        if row["media_id"] == media_id:
            return row["source_filename"].rsplit("-", 1)[-1].split(".")[0]
    row_id = media_id.removeprefix("asset-").removeprefix("visual-")
    for row in MEDIA["visuals"]:
        if row["media_id"].removeprefix("asset-") == row_id:
            return row["source_filename"].rsplit("-", 1)[-1].split(".")[0]
    raise KeyError(media_id)


def _pinned_brief() -> CreativeBrief:
    return CreativeBrief.model_validate(ASSEMBLY["creator_brief_binding"]["brief"])


def _brief_with_r1(**changes) -> CreativeBrief:
    """The pinned brief with the filming-order ask ``r1`` changed (None drops it)."""
    raw = copy.deepcopy(ASSEMBLY["creator_brief_binding"]["brief"])
    requirements = []
    for req in raw["requirements"]:
        if req["id"] == "r1":
            if changes.get("drop"):
                continue
            req.update({k: v for k, v in changes.items() if k != "drop"})
        requirements.append(req)
    raw["requirements"] = requirements
    return CreativeBrief.model_validate(raw)


def _bindings() -> tuple[PhoneSourceBinding, ...]:
    return tuple(PhoneSourceBinding.model_validate(row) for row in ASSEMBLY["_phone_sources_v1"])


def _visual_rows(*, dated: bool = True) -> list[SimpleNamespace]:
    """The selected ready Visuals, as `_load_unified_montage_visuals` returns them."""
    selected = selected_visual_ids(CANDIDATES["creator_strategy"])
    rows = []
    for row in MEDIA["visuals"]:
        if selected is not None and row["media_id"] not in selected:
            continue
        row_id = uuid.UUID(row["media_id"].removeprefix("asset-"))
        rows.append(
            SimpleNamespace(
                id=row_id,
                kind="image",
                gcs_path=f"users/u/plan/i/pool/{row_id}.jpg",
                gcs_generation="1",
                duration_s=None,
                analysis={},
                aspect=4 / 3,
                source_filename=row["source_filename"],
                user_context="",
                content_hash=None,
                capture=row.get("capture") if dated else None,
            )
        )
    return rows


def _plan(brief: CreativeBrief, *, creator_order: list[int] | None = None, dated: bool = True):
    """The worker's own plan of the approved job (`_run_phone_unified_montage_job`)."""
    by_path = {row["gcs_path"]: row for row in MEDIA["clip_assignments"]}
    entries = [copy.deepcopy(by_path[path]) for path in CANDIDATES["clip_paths"]]
    if not dated:
        for entry in entries:
            entry.pop("capture", None)
    candidates = dict(CANDIDATES)
    if creator_order is not None:
        candidates["creator_clip_order"] = creator_order
    return _plan_phone_unified_montage(
        _unified_montage_clips(
            CANDIDATES["clip_paths"],
            entries,
            {binding.proxy_path: binding for binding in _bindings()},
            facts_on=True,
        ),
        brief_view(brief),
        strategy=CANDIDATES["creator_strategy"],
        visuals=[unified_visual(row) for row in _visual_rows(dated=dated)],
        all_candidates=candidates,
        creator_duration_proven=False,
        approved_length_plan=_approved_length_plan_envelope(ASSEMBLY),
        contract=read_render_contract(ASSEMBLY),
    )


def _order(plan) -> list[str]:  # noqa: ANN001
    return [_name(media_id) for media_id in plan.clip_ids]


def _recipe(plan):  # noqa: ANN001
    """The REAL guided compiler + REAL phone compiler, the photos bound as Visuals."""
    visuals = tuple(
        PhoneVisualBinding(
            media_id=str(row.id),
            gcs_path=row.gcs_path,
            generation=row.gcs_generation,
            sha256="c" * 64,
            byte_count=2048,
        )
        for row in _visual_rows()
    )
    execution = compile_execution_plan(plan.guided_edit(), track=None)
    return compile_phone_guided_plan(
        GuidedStoryExecutionPlan.model_validate(execution), _bindings(), visuals
    )


def _recipe_order(recipe) -> list[str]:  # noqa: ANN001
    """The picture order the phone draws: each source's first appearance on the timeline."""
    picture = sorted(
        (clip for track in recipe.tracks if track.kind == "video" for clip in track.clips),
        key=lambda clip: clip.timeline_start,
    )
    order: list[str] = []
    for clip in picture:
        name = _name(clip.source_asset_id)
        if name not in order:
            order.append(name)
    return order


# -- the incident --------------------------------------------------------------


def test_the_incident_input_is_a_dropped_filming_order_ask() -> None:
    brief = _pinned_brief()
    r1 = next(req for req in brief.requirements if req.id == "r1")
    assert (r1.kind, r1.status, r1.facts.get("key")) == ("order", "superseded", "capture_time")
    assert not any(req.kind == "order" for req in brief.live())
    assert set(FIXTURE["simpler_offer"]["omitted_requirement_ids"]) == {"r1", "r5"}
    assert FIXTURE["simpler_offer"]["selection"]["option_key"] == "simpler_version"
    assert read_render_contract(ASSEMBLY).order_required is False
    # The answer key itself disagrees with what shipped (the negative control).
    assert SHIPPED_ORDER != FILMING_ORDER
    assert SHIPPED_ORDER.index("IMG_1002") == 9


def test_the_approved_job_plays_in_filming_order_with_photos_at_their_time() -> None:
    plan = _plan(_pinned_brief())

    assert _order(plan) == FILMING_ORDER  # was the upload order, train 10th of 11
    assert plan.ordering_basis == "capture_time"
    assert plan.ordering_fallback_clip_ids == []


def test_the_phone_draws_that_order_and_the_pinned_contract_promises_none() -> None:
    recipe = _recipe(_plan(_pinned_brief()))

    assert _recipe_order(recipe) == FILMING_ORDER
    # `verify_phone_recipe` checks picture order only under `order_required`; this job's
    # pinned contract has none, so the kept order cannot be refused there. (Its camera-audio
    # clause needs the worker's KRI-942 sound lane, which this replay does not run and
    # which never reads the order.)
    contract = read_render_contract(ASSEMBLY)
    assert (contract.order_required, contract.order_ids, contract.order_basis) == (
        False,
        (),
        None,
    )


def test_nothing_claims_or_checks_the_dropped_order() -> None:
    brief = _pinned_brief()
    receipts = build_receipts(
        brief.live(),
        plan_facts_from_unified_montage(_plan(brief).record()),
        include_unchecked=True,
    )
    assert "r1" not in {receipt.requirement_id for receipt in receipts}


# -- the later word still wins ---------------------------------------------------


def test_a_hand_reorder_after_the_drop_keeps_its_order() -> None:
    reversed_upload = list(range(len(CANDIDATES["clip_paths"])))[::-1]
    plan = _plan(_pinned_brief(), creator_order=reversed_upload)

    assert plan.ordering_basis == "creator_order"
    clips_only = [name for name in _order(plan) if name not in ("IMG_0995", "IMG_1019", "IMG_1038")]
    upload_clips = [
        name for name in SHIPPED_ORDER if name not in ("IMG_0995", "IMG_1019", "IMG_1038")
    ]
    assert clips_only == upload_clips[::-1]


def test_a_live_upload_order_ask_keeps_upload_order() -> None:
    brief = _brief_with_r1(status="open", facts={"key": "attachment"})
    assert _order(_plan(brief)) == SHIPPED_ORDER


def test_a_dropped_upload_order_ask_is_not_turned_into_filming_order() -> None:
    brief = _brief_with_r1(facts={"key": "attachment"})  # still superseded
    assert _order(_plan(brief)) == SHIPPED_ORDER


def test_a_brief_that_never_asked_for_filming_order_is_unchanged() -> None:
    plan = _plan(_brief_with_r1(drop=True))
    assert plan.ordering_basis == "attachment"
    assert _order(plan) == SHIPPED_ORDER


def test_undated_footage_keeps_upload_order_without_failing() -> None:
    plan = _plan(_pinned_brief(), dated=False)
    assert plan.ordering_basis == "attachment"
    assert _order(plan) == SHIPPED_ORDER


def test_the_spoken_order_note_treats_the_kept_filming_order_as_chosen() -> None:
    strategy = CANDIDATES["creator_strategy"]
    contract = read_render_contract(ASSEMBLY)

    assert speech_decides_order(strategy, _pinned_brief(), contract) is False
    assert speech_decides_order(strategy, _brief_with_r1(drop=True), contract) is True
