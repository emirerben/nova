"""Shared builders for the editor Save contract matrix (KRI-750).

One job factory per archetype and ONE ``run_save`` that drives the same sequence the
``POST /plan-items/{id}/variants/{v}/editor-commit`` route runs. The matrix
(``test_editor_save_contract_matrix.py``) holds the rows; this module holds how a row becomes a
real Save. Nothing here asserts.

Route fidelity (``routes/plan_items.py:_editor_commit_item``):

* a clip delete on a guided story stages ``stage_initial_authored_baseline`` and commits with
  ``prepare_authored_editor_commit`` (the "authored" flow);
* every other Save goes through ``prepare_editor_commit`` on the job itself (the "direct" flow).
"""

from __future__ import annotations

import copy
import uuid
from collections.abc import Callable
from dataclasses import dataclass, field
from types import SimpleNamespace
from typing import Any

from fastapi import HTTPException

from app.kria.device_render import make_device_request
from app.kria.media_sources import OriginalMediaDescriptor
from app.pipeline.guided_story import GuidedStoryExecutionPlan, compile_execution_plan
from app.pipeline.phone_guided_plan import compile_phone_guided_plan
from app.routes import generative_jobs as gj
from app.schemas.edit_proposal import (
    ClipLabel,
    EditProposalSnapshot,
    FastMontageCut,
    MediaRef,
    StoryBeat,
    canonical_media_digest,
)
from app.services.authored_editor import prepare_authored_editor_commit
from app.services.creator_render_contract import (
    CONTRACT_FIELD,
    CreatorRenderContract,
    TextRequirement,
    build_render_contract,
)
from app.services.device_render import (
    CONTRACT_REVISIONS_FIELD,
    device_status,
    pin_device_request,
)
from app.services.editor_empty_drafts import stage_initial_authored_baseline
from app.services.phone_sources import PHONE_SOURCES_FIELD, PhoneSourceBinding

VARIANT = "guided_story"
ORDER = ["m0", "m1", "m2", "m3"]
TITLE = "Weekend in Lisbon"

FRAME_S = 1.0 / 30.0
# `authoredText` (per-clip label bars) and `crossfade` (set by the transition ops) are the
# device-verified capabilities a guided timeline edit needs beyond the basic text set; the
# flag flip must be paired with both being in PHONE_RENDER_VERIFIED_FEATURES in prod.
VERIFIED_FEATURES = [
    "basicComposition",
    "local1080Export",
    "positionedText",
    "animatedText",
    "audioMix",
    "authoredText",
    "crossfade",
]
CLIP_COUNT = 4
CLIP_S = 7.5
SOURCE_S = 10.0


def guided_phone_job(monkeypatch, *, count=CLIP_COUNT, clip_s=CLIP_S):
    from app.config import settings

    monkeypatch.setattr(gj.settings, "phone_rendering_enabled", True)
    monkeypatch.setattr(
        gj.settings,
        "phone_render_verified_features",
        VERIFIED_FEATURES,
    )
    monkeypatch.setattr(gj.settings, "guided_story_editor_v2_enabled", True)
    monkeypatch.setattr(settings, "edit_transitions_enabled", True, raising=False)
    monkeypatch.setattr(settings, "kria_guided_timeline_ops", True, raising=False)

    job_id = uuid.uuid4()
    media = [
        MediaRef(
            lane="clip",
            media_id=f"m{i}",
            gcs_path=f"generative-jobs/{job_id}/sources/analysis-proxy-m{i}.mp4",
            generation="123",
            kind="video",
            duration_s=SOURCE_S,
        )
        for i in range(count)
    ]
    snapshot = EditProposalSnapshot(
        direction="fast_montage",
        duration_s=round(count * clip_s),
        title="Weekend",
        opening_title="Weekend in Lisbon",
        video_reuse_policy="once",
        media=media,
        fast_cuts=[
            FastMontageCut(
                cut_id=f"cut-{i}",
                media_id=f"m{i}",
                source_start_s=1.0,
                source_end_s=1.0 + clip_s,
                output_duration_s=clip_s,
                role="hook" if i == 0 else "build",
            )
            for i in range(count)
        ],
        clip_labels=[
            ClipLabel(
                media_id=f"m{i}",
                text=f"Place {i}",
                provenance="creator",
                min_display_s=1.0,
            )
            for i in range(count)
        ],
        story_beats=[
            StoryBeat(
                beat_id="story",
                topic="Scene",
                media_ids=[m.media_id for m in media][:4],
                duration_s=round(count * clip_s),
            )
        ],
    )
    guided = {
        "proposal_version": 1,
        "media_digest": canonical_media_digest(media),
        "approved_proposal": snapshot.model_dump(mode="json"),
        "media_identities": [
            {k: getattr(m, k) for k in ("lane", "media_id", "gcs_path", "generation", "kind")}
            for m in media
        ],
    }
    plan = compile_execution_plan(guided, track=None)
    bindings = tuple(
        PhoneSourceBinding(
            media_id=m.media_id,
            proxy_path=m.gcs_path,
            generation="123",
            original=OriginalMediaDescriptor(
                sha256=f"{i + 1:x}" * 64,
                byte_count=1000,
                duration_s=SOURCE_S,
                width=1920,
                height=1080,
                has_audio=True,
            ),
        )
        for i, m in enumerate(media)
    )
    job = SimpleNamespace(
        id=job_id,
        user_id=uuid.uuid4(),
        status="awaiting_device",
        current_phase=None,
        mode="content_plan",
        all_candidates={"clip_paths": []},
        assembly_plan={
            "guided_edit": guided,
            "guided_story_execution_plan": plan,
            PHONE_SOURCES_FIELD: [b.model_dump(mode="json") for b in bindings],
            "variants": [
                {
                    "variant_id": "guided_story",
                    "resolved_archetype": "guided_story",
                    "render_destination": "device",
                    "render_status": "awaiting_device",
                    "render_generation_id": "first",
                    "duration_s": plan["resolved_duration_s"],
                    "text_mode": "agent_text",
                    "intro_mode": "linear",
                    "intro_layout": "linear",
                    "text_elements": copy.deepcopy(plan["text_elements"]),
                }
            ],
        },
    )
    pin_device_request(
        job,
        make_device_request(
            job_id=job.id,
            variant_id="guided_story",
            revision=1,
            recipe=compile_phone_guided_plan(
                GuidedStoryExecutionPlan.model_validate(plan), bindings
            ),
        ),
        base_generation="first",
    )
    variant = job.assembly_plan["variants"][0]
    revision = gj._guided_v2_revision(job, variant)
    assert revision is not None
    return job, variant, revision


# --- guided-story jobs with a creator contract -------------------------------------------


def order_required_job(monkeypatch, *, order_required=True):
    """A phone guided story whose approved request fixed the clip order (KRI-470)."""
    job, variant, revision = guided_phone_job(monkeypatch)
    contract = build_render_contract({}, generation_id="first", clip_order=ORDER)
    assert contract is not None
    if order_required:
        contract = contract.rebind(
            order_ids=tuple(ORDER), order_required=True, order_basis="capture_time"
        )
    job.assembly_plan[CONTRACT_FIELD] = contract.model_dump(mode="json")
    return job, variant, revision


def requirements(**overrides) -> tuple[TextRequirement, ...]:
    """The approved text rules: an opening title, the same words anywhere, a shot label."""
    base = {
        "opening": TextRequirement(role="opening", text=TITLE),
        "any_title": TextRequirement(role="any", text=TITLE),
        "any_label": TextRequirement(role="any", text="Place 2"),
    }
    base.update(overrides)
    return tuple(v for v in base.values() if v is not None)


def text_row(base: dict, text_id: str, start: float, end: float, text: str | None = None) -> dict:
    return {
        **copy.deepcopy(base),
        "id": text_id,
        "start_s": start,
        "end_s": end,
        "text": text or text_id,
    }


# --- text lanes the client can send: (variant, windows, total) -> lane ---------------------


def phone_retimed_lane(variant, windows, total):
    """What the current phone sends: surviving labels follow their clips, the pin spans the cut."""
    base = variant["text_elements"][1]
    by_id = {t["id"]: t for t in variant["text_elements"]}
    survivors = {
        "clip-label-cut-0": windows["cut-0"],
        "clip-label-cut-2": windows["cut-2"],
        "clip-label-cut-3": windows["cut-3"],
    }
    lane = [by_id["guided-title"]]
    lane += [text_row(base, i, *w, text=by_id[i]["text"]) for i, w in survivors.items()]
    lane.append(text_row(base, "guided-pinned-0", 0.0, total, "Week 2"))
    lane.append(text_row(base, "kria-chat-note", 1.0, total, "added in chat"))
    return lane


def old_clock_lane(variant, windows, total):
    """An app build that does not re-time: every window is still on the 30 s pre-delete clock."""
    lane = copy.deepcopy(variant["text_elements"])
    lane.append(text_row(lane[1], "guided-pinned-0", 0.0, 30.0, "Week 2"))
    return [t for t in lane if t["id"] != "clip-label-cut-1"]


NULL_ROLE_TEXT_ID = "7631D9DC-FFAC-47DF-8955-E31216DDEA41"


def null_role_lane(variant, windows, total):
    """A text the editor created itself: ``encodeText`` sends an explicit ``role: null``."""
    lane = phone_retimed_lane(variant, windows, total)
    new = text_row(variant["text_elements"][1], NULL_ROLE_TEXT_ID, 1.0, 4.0)
    new["role"] = None
    lane.append(new)
    return lane


def title_edit_lane(**changes):
    """The retimed lane with ``guided-title`` patched (moved, shortened, reworded)."""

    def lane(variant, windows, total):
        out = phone_retimed_lane(variant, windows, total)
        next(t for t in out if t["id"] == "guided-title").update(changes)
        return out

    return lane


def closing_moved_lane(variant, windows, total):
    out = phone_retimed_lane(variant, windows, total)
    next(t for t in out if t["id"] == "clip-label-cut-3").update(
        start_s=windows["cut-3"][0], end_s=total - 3.0
    )
    return out


def label_deleted_lane(variant, windows, total):
    return [t for t in phone_retimed_lane(variant, windows, total) if t["id"] != "clip-label-cut-2"]


# --- one Save ------------------------------------------------------------------------------


@dataclass(frozen=True)
class FirstSlot:
    """Override the first KEPT slot's window (source-bounds rows)."""

    in_s: float = 1.0
    duration_s: float | None = None
    overshoot: float = 0.0
    rate: float | None = None
    slot_id: str | None = None  # the id the phone sends for this slot (None = keep the saved one)


@dataclass(frozen=True)
class Edit:
    """What the creator did in the editor before pressing Save.

    ``flow="authored"``: delete ``deleted`` (or split when ``split``), send the full
    ``timeline_slots`` (+ ``texts``). ``flow="direct"``: a Save on the unconverted guided
    editor (``title`` / ``labels`` patch the approved lane, ``muted`` marks every clip).
    """

    flow: str = "authored"
    deleted: str | None = "m1"
    reorder: bool = False
    split: bool = False
    clip_s: float = CLIP_S
    texts: Callable[[dict, dict, float], list[dict]] | None = None
    requirements: tuple[TextRequirement, ...] | None = None
    order_required: bool = True
    contract_changes: dict = field(default_factory=dict)
    duration_s: float | None = None
    muted: bool | None = None
    first_slot: FirstSlot | None = None
    drop_media_id: bool = False
    smuggled_requirement: str | None = None
    # direct flow only
    title: dict | None = None
    labels: dict | None = None
    with_timeline: bool = True


@dataclass
class Outcome:
    """Everything a row may assert on; ``error`` is set when the Save raised."""

    job: Any = None
    prep: dict | None = None
    total: float = 0.0
    error: BaseException | None = None
    value: Any = None  # non-Save rows (fit / schema / resolver) put their result here

    @property
    def status(self) -> int | None:
        return self.error.status_code if isinstance(self.error, HTTPException) else None

    @property
    def detail(self) -> Any:
        return self.error.detail if isinstance(self.error, HTTPException) else None

    @property
    def error_text(self) -> str:
        if self.error is None:
            return ""
        return str(self.detail if isinstance(self.error, HTTPException) else self.error)

    @property
    def variant(self) -> dict:
        return self.job.assembly_plan["variants"][0]

    @property
    def contract(self) -> dict:
        return self.job.assembly_plan[CONTRACT_REVISIONS_FIELD][VARIANT]

    @property
    def recipe(self):
        return device_status(self.job, VARIANT).request.recipe

    @property
    def saved_requirements(self) -> list[tuple[str, str, float | None]]:
        return [(r["role"], r["text"], r["duration_s"]) for r in self.contract["exact_texts"]]


def _rebind(job, edit: Edit) -> None:
    changes = dict(edit.contract_changes)
    if edit.requirements is not None:
        changes["exact_texts"] = edit.requirements
    if edit.duration_s:
        changes["duration_s"] = edit.duration_s
    if changes:
        contract = CreatorRenderContract.model_validate(job.assembly_plan[CONTRACT_FIELD])
        job.assembly_plan[CONTRACT_FIELD] = contract.rebind(**changes).model_dump(mode="json")


def _slot(row: dict, **overrides) -> gj.TimelineSlotEdit:
    base = {
        "slot_id": row["slot_id"],
        "clip_index": row["clip_index"],
        "in_s": row["in_s"],
        "duration_s": row["duration_s"],
        "transition_after": row.get("transition_after") or "cut",
        "transition_duration_s": row.get("transition_duration_s"),
    }
    return gj.TimelineSlotEdit(**{**base, **overrides})


def _first_slot_trim(row: dict, spec: FirstSlot) -> gj.TimelineSlotEdit:
    """The first kept slot with ``spec``'s window (a trim, a rate change or an overshoot)."""
    span = SOURCE_S + spec.overshoot - spec.in_s
    change: dict[str, Any] = {
        "in_s": spec.in_s,
        "duration_s": spec.duration_s if spec.duration_s is not None else span / (spec.rate or 1.0),
    }
    if spec.rate is not None:
        change["playback_rate"] = spec.rate
    if spec.slot_id is not None:
        change["slot_id"] = spec.slot_id
    return _slot(row, **change)


def _authored_save(monkeypatch, edit: Edit) -> Outcome:
    job, variant, revision = order_required_job(monkeypatch, order_required=edit.order_required)
    _rebind(job, edit)
    rows = gj.editor_deletion_timeline(job, variant)
    sources = revision["sources"]
    deletions: list[dict] = []
    target = None
    if edit.deleted is not None:
        target = next(r for r in rows if sources[r["clip_index"]]["media_id"] == edit.deleted)
        deletions = [{"kind": "clip", "id": target["slot_id"]}]

    overrides: dict[str, Any] = {"muted": edit.muted} if edit.muted is not None else {}
    if edit.clip_s != CLIP_S:
        overrides["duration_s"] = edit.clip_s
    slots = [_slot(r, **overrides) for r in rows]
    if edit.first_slot is not None:
        slots[0] = _first_slot_trim(rows[0], edit.first_slot)
    if edit.split:
        half = rows[0]["duration_s"] / 2
        slots[0] = _slot(rows[0], duration_s=half)
        slots.insert(
            1,
            gj.TimelineSlotEdit(
                slot_id=None,
                clip_index=rows[0]["clip_index"],
                in_s=rows[0]["in_s"] + half,
                duration_s=half,
                transition_after="cut",
                transition_duration_s=None,
            ),
        )
    if edit.reorder:
        slots.reverse()

    kept = [s for s in slots if target is None or s.slot_id != target["slot_id"]]
    windows, cursor = {}, 0.0
    for slot in kept:
        if slot.slot_id:
            windows[slot.slot_id] = (cursor, cursor + slot.duration_s)
        cursor += slot.duration_s
    total = sum(s.duration_s for s in kept)

    baseline = stage_initial_authored_baseline(job, VARIANT, rows)
    body = gj.EditorCommitRequest(
        base_generation=gj.variant_render_baseline(variant),
        editor_state_version=1,
        guided_revision_number=revision["revision_number"],
        deletions=deletions,
        timeline_slots=slots,
        text_elements=edit.texts(variant, windows, total) if edit.texts else None,
    )
    outcome = Outcome(job=baseline, total=total)
    if edit.drop_media_id:
        from app.services import authored_editor

        real = authored_editor.resolve_authored_phone_slots

        def without_media_id(*args, **kwargs):
            resolved = real(*args, **kwargs)
            resolved[0].pop("media_id")
            return resolved

        monkeypatch.setattr(authored_editor, "resolve_authored_phone_slots", without_media_id)
    if edit.smuggled_requirement:
        from app.services import phone_editor

        real_requirements = phone_editor._text_requirements_for_save
        extra = TextRequirement(role="any", text=edit.smuggled_requirement)

        def with_smuggled(*args, **kwargs):
            return (*real_requirements(*args, **kwargs), extra)

        monkeypatch.setattr(phone_editor, "_text_requirements_for_save", with_smuggled)
    try:
        body = gj.normalize_editor_deletion_request(baseline, VARIANT, body)
        gj.require_guided_story_editor_commit(baseline, VARIANT, body)
        outcome.prep = prepare_authored_editor_commit(
            baseline, VARIANT, body, validation_arguments={}
        )
    except Exception as exc:  # noqa: BLE001 - the row decides what an error must look like
        outcome.error = exc
    return outcome


def _direct_slots(rows: list[dict], edit: Edit) -> list[gj.TimelineSlotEdit]:
    slots = [_slot(r, muted=edit.muted) for r in rows]
    if edit.first_slot is not None:
        slots[0] = _first_slot_trim(rows[0], edit.first_slot)
    return slots


def _direct_save(monkeypatch, edit: Edit) -> Outcome:
    """A manual Save on the (non-authored) guided-story editor, via the real commit path."""
    job, variant, revision = order_required_job(monkeypatch, order_required=False)
    _rebind(job, edit)
    rows = gj.editor_deletion_timeline(job, variant)
    lane = copy.deepcopy(variant["text_elements"])
    if edit.title:
        next(t for t in lane if t["id"] == "guided-title").update(edit.title)
    for text_id, window in (edit.labels or {}).items():
        next(t for t in lane if t["id"] == text_id).update(window)
    slots = _direct_slots(rows, edit) if edit.with_timeline else None
    body = gj.EditorCommitRequest(
        base_generation=gj.variant_render_baseline(variant),
        editor_state_version=1,
        guided_revision_number=revision["revision_number"],
        timeline_slots=slots,
        text_elements=lane,
    )
    total = sum(s.duration_s for s in slots) if slots else 0.0
    outcome = Outcome(job=job, total=total)
    try:
        outcome.prep = gj.prepare_editor_commit(job, VARIANT, body)
    except Exception as exc:  # noqa: BLE001
        outcome.error = exc
    return outcome


def run_save(monkeypatch, edit: Edit) -> Outcome:
    if edit.flow == "authored":
        return _authored_save(monkeypatch, edit)
    if edit.flow == "direct":
        return _direct_save(monkeypatch, edit)
    raise AssertionError(f"unknown flow {edit.flow!r}")


# --- other archetypes (text-only Save) ----------------------------------------------------


def archetype_save(monkeypatch, archetype: str) -> Outcome:
    """A caption-text Save on a phone archetype that is not a guided story.

    ``subtitled`` (Talking) and ``narrated`` reword their first caption; the montage
    ``voiceover`` carries no caption lane; ``speech_montage`` and ``slides`` have no server
    recompile at all.
    """
    if archetype == "subtitled":
        from tests.pipeline.test_phone_subtitled_plan import _CUES as cues
        from tests.routes import test_phone_subtitled_editor_commit as mod

        job = mod.phone_job(monkeypatch)
        variant_id = "subtitled"
        sections = {"caption_cues": [dict(cues[0], text="Reworded caption"), *cues[1:]]}
    elif archetype == "narrated":
        from tests.routes import test_phone_narrated_editor_commit as mod

        job = mod.phone_job(monkeypatch)
        variant_id = "narrated"
        sections = {"caption_cues": [dict(mod._CUES[0], text="First we pack light"), mod._CUES[1]]}
    elif archetype == "slides":
        # A slide post is cloud-rendered: the capability map closes every lane for it.
        from tests.routes.test_editor_commit import _arm, _job

        _arm(monkeypatch)
        job = _job(resolved_archetype="slides")
        variant_id = job.assembly_plan["variants"][0]["variant_id"]
        sections = {
            "text_elements": [{"id": "t-1", "text": "Packed", "start_s": 0.0, "end_s": 2.0}]
        }
    elif archetype in {"voiceover", "speech_montage"}:
        from tests.routes import test_phone_voiceover_editor_lanes as mod

        mod._enable(monkeypatch)
        job, variant_id = mod.voiceover_job(
            archetype="voiceover" if archetype == "voiceover" else "narrated"
        )
        sections = {"caption_cues": [{"text": "Packed", "start_s": 0.0, "end_s": 2.0}]}
        if archetype == "speech_montage":
            job.assembly_plan["variants"][0]["resolved_archetype"] = archetype
            # No caption base to edit: the text lane is the only render-affecting Save left.
            sections = {
                "text_elements": [{"id": "t-1", "text": "Packed", "start_s": 0.0, "end_s": 2.0}]
            }
    else:
        raise AssertionError(f"unknown archetype {archetype!r}")
    variant = job.assembly_plan["variants"][0]
    outcome = Outcome(job=job, value={"variant_id": variant_id})
    try:
        outcome.prep = gj.prepare_editor_commit(
            job,
            variant_id,
            gj.EditorCommitRequest(base_generation=gj.variant_render_baseline(variant), **sections),
            user_id="owner",
            plan_item_id="item",
        )
    except Exception as exc:  # noqa: BLE001
        outcome.error = exc
    return outcome


# --- phone Talking speech segments after the first Delete (KRI-852) -----------------------

TALKING_VARIANT = "subtitled"
# A 10 s take with two pauses cut: segments [0, 2), [3, 6), [7, 10) of the original.
TALKING_REMOVED = ((2.0, 3.0), (6.0, 7.0))
TALKING_CUES = (
    {"text": "one", "start_s": 0.0, "end_s": 1.5},
    {"text": "two", "start_s": 2.0, "end_s": 4.5},
    {"text": "three", "start_s": 5.0, "end_s": 7.5},
)


def talking_cut_job(monkeypatch):
    """A phone Talking variant with a pinned speech cleanup: three ``native-phone-talking-source*``
    rows in ``editor_deletion_timeline``."""
    from app.kria.device_render import make_device_request
    from app.pipeline.phone_subtitled_plan import compile_phone_subtitled_plan
    from app.pipeline.silence_cut import CutPlan, Removal
    from app.services.phone_sources import PHONE_VISUALS_FIELD
    from tests.pipeline.test_phone_subtitled_plan import _binding
    from tests.routes.test_phone_subtitled_editor_commit import _enable_subtitled_editor

    _enable_subtitled_editor(monkeypatch)
    monkeypatch.setattr(gj.settings, "phone_sfx_speech_duck_enabled", False)
    binding = _binding(duration_s=SOURCE_S)
    cut_plan = CutPlan(
        keep_segments=[(0.0, 2.0), (3.0, 6.0), (7.0, 10.0)],
        removed=[Removal(start_s=s, end_s=e, reason="pause") for s, e in TALKING_REMOVED],
        time_saved_s=2.0,
    )
    cues = [dict(cue) for cue in TALKING_CUES]
    recipe = compile_phone_subtitled_plan((binding,), caption_cues=cues, cut_plan=cut_plan)
    job = SimpleNamespace(
        id=uuid.uuid4(),
        user_id=uuid.uuid4(),
        status="awaiting_device",
        current_phase=None,
        all_candidates={"clip_paths": [binding.proxy_path]},
        assembly_plan={
            PHONE_SOURCES_FIELD: [binding.model_dump(mode="json")],
            PHONE_VISUALS_FIELD: [],
            "variants": [
                {
                    "variant_id": TALKING_VARIANT,
                    "resolved_archetype": "subtitled",
                    "render_destination": "device",
                    "render_status": "awaiting_device",
                    "render_generation_id": "first",
                    "duration_s": recipe.duration,
                    "caption_cues": cues,
                    "voiceover_caption_style": "sentence",
                    "silence_cut": {
                        "removed": [{"start_s": s, "end_s": e} for s, e in TALKING_REMOVED]
                    },
                }
            ],
        },
    )
    pin_device_request(
        job,
        make_device_request(job_id=job.id, variant_id=TALKING_VARIANT, revision=1, recipe=recipe),
        base_generation="first",
    )
    return job


def talking_segment_save(monkeypatch, change: str) -> Outcome:
    """Delete the middle speech segment, then ``change`` the others, through the route's
    authored sequence (a clip delete on a Talking edit is not a phone Voiceover cut).

    ``change``: ``"none"`` (the Delete alone), ``"range"`` (a new window on the last segment,
    what the Range sheet writes) or ``"reorder"`` (the two kept segments swapped).
    """
    job = talking_cut_job(monkeypatch)
    variant = job.assembly_plan["variants"][0]
    rows = gj.editor_deletion_timeline(job, variant)
    assert [r["slot_id"] for r in rows] == [
        "native-phone-talking-source",
        "native-phone-talking-source-1",
        "native-phone-talking-source-2",
    ]
    slots = [_slot(r) for r in rows]
    if change == "range":
        slots[2] = _slot(
            rows[2], in_s=rows[2]["in_s"] + 0.5, duration_s=rows[2]["duration_s"] - 0.5
        )
    elif change == "reorder":
        slots = [slots[2], slots[1], slots[0]]
    elif change != "none":
        raise AssertionError(f"unknown change {change!r}")
    baseline = stage_initial_authored_baseline(job, TALKING_VARIANT, rows)
    body = gj.EditorCommitRequest(
        base_generation=gj.variant_render_baseline(variant),
        editor_state_version=1,
        deletions=[{"kind": "clip", "id": rows[1]["slot_id"]}],
        timeline_slots=slots,
    )
    outcome = Outcome(job=baseline, value={"variant_id": TALKING_VARIANT})
    try:
        body = gj.normalize_editor_deletion_request(baseline, TALKING_VARIANT, body)
        gj.require_guided_story_editor_commit(baseline, TALKING_VARIANT, body)
        outcome.prep = prepare_authored_editor_commit(
            baseline, TALKING_VARIANT, body, validation_arguments={}
        )
    except Exception as exc:  # noqa: BLE001
        outcome.error = exc
    return outcome
