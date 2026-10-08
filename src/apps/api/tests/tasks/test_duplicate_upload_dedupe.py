"""KRI-544: two uploads of the same file render once under "each video once".

Incident (prod 2026-10-08, runtime-v2 phone Montage, stress kit M3 "Berlin student day"):
12 uploads, two of them byte-identical (IMG_4400 and IMG_4400_copy, the U-Bahn; same
``upload_contract.proxy.original.sha256``). The creator asked "Aynı videodan iki tane
varsa birini kullan" (if there are two of the same video, use one). The Creator left
``selected_media_ids`` empty (= every clip), the render contract pinned all 12 in filming
order, and the unified montage played the U-Bahn twice back to back while the draft said
"kopya videolardan birini çıkardım" (I removed one of the copies).

The end-to-end tests run the REAL dispatch narrowing + collapse, contract builder,
unified planner, guided compiler, phone compiler and verifier (the KRI-503/515 harness).
"""

from __future__ import annotations

import copy
import uuid
from contextlib import ExitStack
from datetime import timedelta
from types import SimpleNamespace
from unittest.mock import MagicMock, Mock, patch

import pytest

from app.config import settings
from app.kria.brief import BriefRequirement, CreativeBrief
from app.kria.brief_binding import BriefBinding, snapshot_media
from app.kria.duplicate_uploads import (
    DROPPED_DUPLICATES_FIELD,
    aliases_from_candidates,
    collapse_strategy,
    duplicate_aliases,
    montage_record_fields,
    original_sha256,
    receipt,
)
from app.kria.media_sources import OriginalMediaDescriptor
from app.pipeline.guided_story import GuidedStoryExecutionPlan, compile_execution_plan
from app.pipeline.phone_guided_plan import compile_phone_guided_plan
from app.pipeline.unified_montage import brief_view, plan_unified_montage
from app.services.creator_render_contract import (
    CONTRACT_FIELD,
    build_render_contract,
    verify_phone_recipe,
)
from app.services.phone_sources import PhoneSourceBinding
from app.tasks import generative_build as gb
from app.tasks.content_plan_build import (
    _collapse_duplicate_uploads,
    _creator_selected_clip_paths,
    _dispatch_item_render,
)
from tests.pipeline.test_unified_montage import T0, clip
from tests.tasks.test_content_plan_build import _phone_dispatch_item
from tests.tasks.test_unified_montage_dispatch import harness  # noqa: F401 -- fixture

# The M3 kit in its prod attachment order: minutes after 05:00 each clip was filmed.
# c0 (IMG_4400_copy) and c7 (IMG_4400) are the same U-Bahn bytes, filmed 05:48 both.
# c3 is a 1.4 s fridge shot; c11 is Elif talking to camera, filmed last.
MINUTES = [48, 95, 425, 715, 630, 200, 885, 48, 750, 340, 12, 970]
KEPT, DUPLICATE, ELIF = 0, 7, 11
DURATIONS = {3: 1.433, ELIF: 7.2}
UBAHN_SHA = "6e8c3888ca2a4ea0e4e66b9139a37bdcf8ba84d34434e9f731ea530e081a2635"


@pytest.fixture(autouse=True)
def _clip_intents_on(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(settings, "clip_intents_enabled", True)


def _id(index: int) -> str:
    return f"c{index}"


def _sha(index: int) -> str:
    return UBAHN_SHA if index in (KEPT, DUPLICATE) else f"{index:x}".rjust(64, "b")


def _capture(index: int) -> dict:
    taken = T0 + timedelta(minutes=MINUTES[index])
    return {"capture_time": taken.strftime("%Y-%m-%dT%H:%M:%SZ")}


def _upload_contract(index: int, *, duration: float = 5.0, sha: str | None = None) -> dict:
    """The iPhone's analysis-proxy receipt (the same shape prod media_added events carry)."""

    return {
        "purpose": "analysis_proxy",
        "proxy": {
            "original": {
                "kind": "video",
                "sha256": sha or _sha(index),
                "byte_count": 6142247,
                "duration_s": duration,
                "width": 1080,
                "height": 1920,
                "has_audio": True,
            },
            "duration_s": duration,
            "width": 320,
            "height": 568,
            "frame_rate": 30.0,
            "timing_version": 1,
        },
    }


def _clips():
    return [
        clip(i, minutes=MINUTES[i], duration=DURATIONS.get(i, 5.0)) for i in range(len(MINUTES))
    ]


def _rows(**overrides: dict) -> list[dict]:
    """The item's clip assignments (attachment order), as the media snapshot stores them."""

    rows = []
    for c in _clips():
        index = int(c.media_id[1:])
        rows.append(
            {
                "media_id": c.media_id,
                "gcs_path": c.proxy_path,
                "kind": "video",
                "capture": _capture(index),
                "upload_contract": _upload_contract(index, duration=c.duration_s),
                **overrides.get(c.media_id, {}),
            }
        )
    return rows


def _item(rows: list[dict] | None = None):
    return SimpleNamespace(id=uuid.uuid4(), clip_assignments=rows or _rows())


def _strategy(**extra) -> dict:
    """The approved prod strategy's shape: every clip, each video once, filming order."""

    return {
        "edit_format": "montage",
        "render_program": "guided",
        "media_scope": "selected",
        "selected_media_ids": [],
        "video_reuse_policy": "once",
        "target_duration_s": 25,
        "target_duration_requested": True,
        "montage_audio": {
            "source_media_ids": [],
            "preview_source_beds": False,
            "preserve_source_audio": True,
        },
        "resolved_clip_intents": [
            {
                # The resolver put the "Sabah" chapter on the copy dispatch drops.
                "op": "caption",
                "status": "resolved",
                "intent_id": "i2",
                "attribute": "Sabah",
                "creator_text": "Sabah",
                "caption_text": "Sabah",
                "caption_grounding": "creator_text",
                "assignments": [
                    {"media_id": _id(10), "confidence": 0.9},
                    {"media_id": _id(DUPLICATE), "confidence": 0.8},
                ],
            },
            {
                "op": "include",
                "status": "resolved",
                "intent_id": "i7",
                "attribute": "aynı videodan iki tane varsa birini",
                "assignments": [{"media_id": _id(KEPT), "confidence": 0.9}],
            },
            {
                "op": "order",
                "status": "resolved",
                "intent_id": "i8",
                "attribute": "Elif'in kameraya söylediği cümle",
                "position": "last",
                "assignments": [{"media_id": _id(ELIF), "confidence": 1.0}],
            },
            {
                "op": "order",
                "status": "resolved",
                "intent_id": "i1",
                "attribute": "videoların çekildiği saat sırasına göre",
                "order_by": "capture_time",
                "assignments": [
                    {"media_id": _id(i), "confidence": 1.0} for i in range(len(MINUTES))
                ],
            },
        ],
        **extra,
    }


def _brief() -> CreativeBrief:
    return CreativeBrief(
        version=1,
        requirements=[
            BriefRequirement(
                id="r1",
                kind="order",
                scope="global",
                description="Videoları çektiğim saat sırasına göre diz, sabahtan geceye",
                facts={"key": "capture_time"},
            ),
            BriefRequirement(
                id="r3",
                kind="select",
                scope="global",
                description="Aynı videodan iki tane varsa birini kullan",
            ),
            BriefRequirement(
                id="r5", kind="timing", scope="global", description="25 s", facts={"duration_s": 25}
            ),
        ],
    )


def _binding(rows: list[dict] | None = None) -> BriefBinding:
    return BriefBinding.create(
        uuid.uuid5(uuid.NAMESPACE_URL, "kri-544"),
        _brief(),
        latest_message="Aynı videodan iki tane varsa birini kullan",
        media_snapshot={"clip_assignments": rows or _rows()},
    )


def _verify(contract, plan, clips) -> list[dict]:
    bindings = tuple(
        PhoneSourceBinding(
            media_id=c.media_id,
            proxy_path=c.proxy_path,
            generation=c.generation,
            original=OriginalMediaDescriptor(
                sha256=_sha(int(c.media_id[1:])),
                byte_count=1000,
                duration_s=c.duration_s,
                width=1080,
                height=1920,
                has_audio=True,
            ),
        )
        for c in clips
    )
    execution = compile_execution_plan(plan.guided_edit(), track=None)
    recipe = compile_phone_guided_plan(GuidedStoryExecutionPlan.model_validate(execution), bindings)
    return verify_phone_recipe(contract, recipe, source_audio={c.media_id: True for c in clips})


def _dispatch(strategy: dict, rows: list[dict] | None = None):
    """The real dispatch narrowing + collapse, then the clips the worker is handed."""

    every = _clips()
    item = _item(rows)
    paths = _creator_selected_clip_paths(
        item, [c.proxy_path for c in every], strategy, without_proposal=True
    )
    paths, aliases = _collapse_duplicate_uploads(
        item, paths, strategy, brief_binding=_binding(rows).model_dump(mode="json")
    )
    return [c for c in every if c.proxy_path in paths], aliases


# -- the incident, end to end ----------------------------------------------------------


def test_incident_the_copy_is_dropped_the_contract_agrees_and_the_recipe_verifies() -> None:
    strategy = _strategy()
    binding = _binding()
    clips, aliases = _dispatch(strategy)

    assert aliases == {_id(DUPLICATE): _id(KEPT)}
    assert [c.media_id for c in clips] == [_id(i) for i in range(len(MINUTES)) if i != DUPLICATE]
    contract = build_render_contract(
        strategy,
        generation_id="gen",
        brief=binding.resolve(),
        media_snapshot=binding.media_snapshot,
        duplicate_aliases=aliases,
    )
    plan = plan_unified_montage(
        clips,
        brief_view(binding.resolve()),
        strategy=collapse_strategy(strategy, aliases),
        clip_intents_enabled=True,
    )

    assert _id(DUPLICATE) not in plan.clip_ids
    assert plan.clip_ids.count(_id(KEPT)) == 1
    assert tuple(plan.clip_ids) == contract.order_ids
    assert plan.clip_ids[-1] == _id(ELIF)
    # The "Sabah" chapter the resolver put on the dropped copy now reads on the kept one.
    sabah = {label.media_id for label in plan.snapshot.clip_labels if label.text == "Sabah"}
    assert sabah == {_id(10), _id(KEPT)}
    assert _verify(contract, plan, clips)


def test_incident_reproduction_the_copy_played_twice_and_nothing_refused_it() -> None:
    """Pre-fix: the contract pinned both copies, so the verifier passed the repeat."""

    strategy = _strategy()
    binding = _binding()
    every = _clips()
    contract = build_render_contract(
        strategy,
        generation_id="gen",
        brief=binding.resolve(),
        media_snapshot=binding.media_snapshot,
    )
    plan = plan_unified_montage(
        every, brief_view(binding.resolve()), strategy=strategy, clip_intents_enabled=True
    )

    assert {_id(KEPT), _id(DUPLICATE)} <= set(contract.order_ids)
    kept_at = plan.clip_ids.index(_id(KEPT))
    assert plan.clip_ids[kept_at + 1] == _id(DUPLICATE), "the U-Bahn played back to back"
    assert _verify(contract, plan, every)


def test_contract_without_aliases_is_byte_identical_and_keeps_the_approved_digest() -> None:
    strategy = _strategy()
    binding = _binding()
    kwargs = {
        "generation_id": "gen",
        "brief": binding.resolve(),
        "media_snapshot": binding.media_snapshot,
    }
    plain = build_render_contract(strategy, **kwargs)
    assert build_render_contract(strategy, duplicate_aliases={}, **kwargs) == plain
    collapsed = build_render_contract(
        strategy, duplicate_aliases={_id(DUPLICATE): _id(KEPT)}, **kwargs
    )
    assert collapsed.strategy_digest == plain.strategy_digest
    assert collapsed.order_ids == tuple(m for m in plain.order_ids if m != _id(DUPLICATE))


# -- the collapse rule -------------------------------------------------------------------


def test_the_earliest_filmed_copy_stays_even_when_it_was_attached_later() -> None:
    rows = _rows(**{_id(DUPLICATE): {"capture": {"capture_time": "2026-09-20T05:00:00Z"}}})
    assert duplicate_aliases(_strategy(), rows) == {_id(KEPT): _id(DUPLICATE)}


def test_a_copy_without_a_capture_time_loses_to_one_with_it() -> None:
    rows = _rows(**{_id(KEPT): {"capture": {}}})
    assert duplicate_aliases(_strategy(), rows) == {_id(KEPT): _id(DUPLICATE)}


def test_three_copies_keep_one() -> None:
    rows = _rows(**{_id(5): {"upload_contract": _upload_contract(5, sha=UBAHN_SHA)}})
    assert duplicate_aliases(_strategy(), rows) == {_id(5): _id(KEPT), _id(DUPLICATE): _id(KEPT)}


@pytest.mark.parametrize(
    "strategy",
    [
        _strategy(video_reuse_policy="allow_repeat"),  # the creator asked to repeat footage
        {k: v for k, v in _strategy().items() if k != "video_reuse_policy"},  # legacy
        _strategy(shot_labels=["a", "b"]),  # positional labels would shift onto other clips
        _strategy(
            montage_cadence={
                "source_media_ids": [_id(KEPT), _id(1)],
                "cut_duration_s": 1.0,
            }
        ),
        _strategy(  # a copy is the named camera-audio source
            montage_audio={"preserve_source_audio": True, "source_media_ids": [_id(DUPLICATE)]}
        ),
        None,
    ],
    ids=["allow-repeat", "no-policy", "shot-labels", "cadence", "audio-source", "no-strategy"],
)
def test_nothing_collapses_outside_the_safe_shape(strategy) -> None:
    assert duplicate_aliases(strategy, _rows()) == {}


def test_a_clip_scoped_brief_requirement_on_a_copy_keeps_both() -> None:
    assert duplicate_aliases(_strategy(), _rows(), blocked_refs=["c7"]) == {}
    # A requirement on another clip does not.
    assert duplicate_aliases(_strategy(), _rows(), blocked_refs=["c11"]) == {
        _id(DUPLICATE): _id(KEPT)
    }


@pytest.mark.parametrize(
    "contract",
    [
        None,  # a web upload: no receipt at all
        {"purpose": "cloud_render_source"},  # a cloud upload carries no device hash
        {"purpose": "analysis_proxy", "proxy": {"original": {"sha256": UBAHN_SHA}}},  # malformed
    ],
    ids=["no-receipt", "cloud-source", "malformed"],
)
def test_uploads_without_a_device_hash_never_collapse(contract) -> None:
    override = {"upload_contract": contract} if contract is not None else {"upload_contract": None}
    rows = _rows(**{_id(KEPT): override, _id(DUPLICATE): override})
    assert original_sha256(rows[KEPT]) is None
    assert duplicate_aliases(_strategy(), rows) == {}


def test_only_the_clips_that_would_render_are_compared() -> None:
    """A Creator pick that already holds one copy leaves nothing to collapse."""

    rows = [row for row in _rows() if row["media_id"] != _id(KEPT)]
    assert duplicate_aliases(_strategy(), rows) == {}


def test_collapse_strategy_moves_intents_and_selection_to_the_kept_copy() -> None:
    strategy = _strategy(selected_media_ids=[_id(DUPLICATE), _id(KEPT), _id(1)])
    aliases = {_id(DUPLICATE): _id(KEPT)}
    out = collapse_strategy(strategy, aliases)

    assert out["selected_media_ids"] == [_id(KEPT), _id(1)]
    intents = {row["intent_id"]: row for row in out["resolved_clip_intents"]}
    assert [a["media_id"] for a in intents["i2"]["assignments"]] == [_id(10), _id(KEPT)]
    chrono = [a["media_id"] for a in intents["i1"]["assignments"]]
    assert chrono.count(_id(KEPT)) == 1 and _id(DUPLICATE) not in chrono
    assert len(chrono) == len(MINUTES) - 1
    # Idempotent, and the approved strategy itself is untouched.
    assert collapse_strategy(out, aliases) == out
    assert strategy == _strategy(selected_media_ids=[_id(DUPLICATE), _id(KEPT), _id(1)])


def test_the_job_receipt_round_trips_into_the_montage_record() -> None:
    aliases = {_id(DUPLICATE): _id(KEPT)}
    rows = receipt(aliases, _rows())
    assert rows == [{"media_id": _id(DUPLICATE), "kept_media_id": _id(KEPT)}]
    assert aliases_from_candidates({DROPPED_DUPLICATES_FIELD: rows}) == aliases
    assert montage_record_fields(aliases) == {
        "dropped_duplicate_clip_ids": [_id(DUPLICATE)],
        "duplicate_kept_clip_ids": {_id(DUPLICATE): _id(KEPT)},
    }
    assert montage_record_fields({}) == {}
    assert aliases_from_candidates({}) == {}
    assert aliases_from_candidates({DROPPED_DUPLICATES_FIELD: "garbage"}) == {}


# -- the real dispatch -------------------------------------------------------------------


def _dispatch_item(count: int = 12):
    """A phone montage item whose assignments carry verified iPhone receipts."""

    item = _phone_dispatch_item("montage", clip_count=count)
    rows = []
    for index, row in enumerate(item.clip_assignments):
        rows.append(
            {
                **row,
                "manifest_identity": row["media_id"],
                "capture": _capture(index),
                "upload_contract": _upload_contract(index, duration=row["duration_s"]),
            }
        )
    item.clip_assignments = rows
    item.clip_gcs_paths = [row["gcs_path"] for row in rows]
    return item


def _run_dispatch(
    monkeypatch: pytest.MonkeyPatch,
    strategy: dict,
    *,
    approved: bool = False,
    with_binding: bool = True,
):
    from app.services.phone_sources import bind_phone_sources

    monkeypatch.setattr(settings, "speech_cleanup_mode", "opt_in")
    monkeypatch.setattr(settings, "phone_rendering_enabled", True)
    monkeypatch.setattr(settings, "phone_render_user_ids", [])
    monkeypatch.setattr(settings, "guided_edit_capability_enabled", True)
    monkeypatch.setattr(settings, "kria_runtime_v2_phone_enabled", True)
    monkeypatch.setattr(settings, "kria_runtime_v2_phone_user_ids", [])

    item = _dispatch_item()
    plan = SimpleNamespace(
        id=uuid.uuid4(), user_id=uuid.uuid4(), preference_summary="", ownership_epoch=0
    )
    binding = BriefBinding.create(
        uuid.uuid4(),
        _brief(),
        latest_message="Aynı videodan iki tane varsa birini kullan",
        media_snapshot=snapshot_media(item),
    )
    session = MagicMock()
    session.execute.return_value.scalar_one.return_value = 0
    session.execute.return_value.all.return_value = []
    # The approved thread the binding belongs to.
    session.execute.return_value.scalar_one_or_none.return_value = SimpleNamespace(
        id=uuid.UUID(binding.thread_id)
    )
    built: dict = {}

    def build(**kwargs):
        built.update(kwargs)
        job = SimpleNamespace(
            id=uuid.uuid4(),
            user_id=plan.user_id,
            assembly_plan={},
            all_candidates={
                "clip_paths": list(kwargs["clip_paths"]),
                "creator_strategy": copy.deepcopy(kwargs["creator_strategy"]),
            },
        )
        built["job"] = job
        return job

    approved_proposal = (
        {"proposal_version": 1, "media_digest": "d" * 64, "snapshot": {"media": []}}
        if approved
        else None
    )
    bind = Mock(wraps=bind_phone_sources)
    with ExitStack() as stack:
        stack.enter_context(
            patch(
                "app.services.smart_captions.resolve_smart_captions_context_sync",
                return_value=None,
            )
        )
        stack.enter_context(
            patch(
                "app.services.edit_proposals.validate_approved_proposal_media_sync",
                return_value=(None, approved_proposal),
            )
        )
        stack.enter_context(patch("app.services.phone_sources.bind_phone_sources", bind))
        stack.enter_context(
            patch("app.services.generative_jobs.build_generative_job", side_effect=build)
        )
        stack.enter_context(patch("app.services.job_dispatch.enqueue_orchestrator_sync"))
        kwargs = (
            {}
            if approved
            else {"bypass_guided_edit_gate": True, "allow_phone_unapproved_montage": True}
        )
        result = _dispatch_item_render(
            session,
            item,
            plan,
            {"tone": "direct", "content_pillars": []},
            ownership_epoch=0,
            creator_strategy=strategy,
            **({"creator_brief_binding": binding.model_dump(mode="json")} if with_binding else {}),
            **kwargs,
        )
    return result, item, built, bind


def _ids(prefix: str = "registered-spine-") -> list[str]:
    return [f"{prefix}{n}" for n in range(12)]


def _dispatch_strategy() -> dict:
    strategy = _strategy()
    rename = dict(zip([_id(i) for i in range(12)], _ids(), strict=True))
    for intent in strategy["resolved_clip_intents"]:
        for assignment in intent["assignments"]:
            assignment["media_id"] = rename[assignment["media_id"]]
    return strategy


def test_v2_phone_dispatch_binds_builds_and_pins_one_copy(monkeypatch) -> None:
    strategy = _dispatch_strategy()
    result, item, built, bind = _run_dispatch(monkeypatch, strategy)

    assert result.outcome == "dispatched"
    every = list(item.clip_gcs_paths)
    expected = [path for index, path in enumerate(every) if index != DUPLICATE]
    assert built["clip_paths"] == expected
    assert bind.call_args.args[1] == expected
    assert [source.media_id for source in built["phone_sources"]] == [
        media_id for index, media_id in enumerate(_ids()) if index != DUPLICATE
    ]
    job = built["job"]
    assert job.all_candidates[DROPPED_DUPLICATES_FIELD] == [
        {"media_id": f"registered-spine-{DUPLICATE}", "kept_media_id": f"registered-spine-{KEPT}"}
    ]
    # The persisted strategy is the approved one (session/plan equality checks read it).
    assert job.all_candidates["creator_strategy"] == strategy
    order_ids = job.assembly_plan[CONTRACT_FIELD]["order_ids"]
    assert f"registered-spine-{DUPLICATE}" not in order_ids
    assert order_ids.count(f"registered-spine-{KEPT}") == 1
    assert len(order_ids) == len(MINUTES) - 1
    assert order_ids[-1] == f"registered-spine-{ELIF}"


def test_an_approved_guided_proposal_owns_its_media_choice(monkeypatch) -> None:
    """Byte-identical dispatch: the approved proposal pins exactly which sources play."""

    result, item, built, _bind = _run_dispatch(
        monkeypatch, _dispatch_strategy(), approved=True, with_binding=False
    )

    assert result.outcome == "dispatched"
    assert built["clip_paths"] == list(item.clip_gcs_paths)
    assert DROPPED_DUPLICATES_FIELD not in built["job"].all_candidates


def test_allow_repeat_dispatch_keeps_both_copies(monkeypatch) -> None:
    strategy = {**_dispatch_strategy(), "video_reuse_policy": "allow_repeat"}
    result, item, built, _bind = _run_dispatch(monkeypatch, strategy)

    assert result.outcome == "dispatched"
    assert built["clip_paths"] == list(item.clip_gcs_paths)
    assert DROPPED_DUPLICATES_FIELD not in built["job"].all_candidates
    assert len(built["job"].assembly_plan[CONTRACT_FIELD]["order_ids"]) == len(MINUTES)


# -- the worker --------------------------------------------------------------------------


def test_the_worker_records_the_dropped_copy_and_reads_its_intent_on_the_kept_one(
    harness,  # noqa: F811
    monkeypatch,
) -> None:
    """The unified montage receipt names the copy dispatch left out (for the r3 checker)."""

    monkeypatch.setattr(gb.settings, "clip_intents_enabled", True)
    job, *_ = harness(brief=None)
    dropped, kept = "clip-5", "clip-4"
    job.all_candidates["clip_paths"] = [
        path for path in job.all_candidates["clip_paths"] if not path.endswith(f"{dropped}.mp4")
    ]
    job.all_candidates[DROPPED_DUPLICATES_FIELD] = [{"media_id": dropped, "kept_media_id": kept}]
    job.all_candidates["creator_strategy"] = {
        **job.all_candidates["creator_strategy"],
        "video_reuse_policy": "once",
        "resolved_clip_intents": [
            {
                "op": "caption",
                "status": "resolved",
                "intent_id": "i1",
                "attribute": "the bridge",
                "creator_text": "Köprü",
                "caption_text": "Köprü",
                "caption_grounding": "creator_text",
                "assignments": [{"media_id": dropped, "confidence": 0.9}],
            }
        ],
    }

    gb._run_generative_job(str(job.id))

    record = job.assembly_plan["unified_montage"]
    assert dropped not in record["clip_ids"]
    assert record["dropped_duplicate_clip_ids"] == [dropped]
    assert record["duplicate_kept_clip_ids"] == {dropped: kept}
    assert [row["media_id"] for row in record["labels"] if row["text"] == "Köprü"] == [kept]


def test_a_worker_job_without_a_collapse_keeps_its_record_shape(harness) -> None:  # noqa: F811
    job, *_ = harness(brief=None)
    gb._run_generative_job(str(job.id))
    record = job.assembly_plan["unified_montage"]
    assert "dropped_duplicate_clip_ids" not in record
    assert "duplicate_kept_clip_ids" not in record
