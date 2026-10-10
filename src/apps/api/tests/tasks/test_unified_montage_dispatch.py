"""KRI-190: a phone montage with no approved proposal renders through the guided plan.

The fixture mirrors the first device test of the East Run thread (job 94c4c865):
runtime v2, `render_program: guided`, no `edit_proposal`, the item's default
`landscape_fit="fit"`, fourteen short clips, a phone account and a brief with a
per-clip text, a timing and a global-text requirement. Synthetic data only.
"""

from __future__ import annotations

import copy
import unicodedata
import uuid
from contextlib import contextmanager
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from app.kria.brief import BriefRequirement, CreativeBrief
from app.kria.media_sources import OriginalMediaDescriptor
from app.pipeline.guided_story import GuidedStoryExecutionPlan, compile_execution_plan
from app.pipeline.phone_guided_plan import UnsupportedPhonePlan
from app.pipeline.unified_montage import min_display_s
from app.schemas.clip_understanding import ClipFact
from app.services import clip_facts
from app.services.device_render import device_status
from app.services.phone_sources import PHONE_SOURCES_FIELD, PhoneSourceBinding
from app.tasks import generative_build as gb

PLACES = [
    "Arnavutköy",
    "Bebek",
    "Rumeli Hisarı",
    "Sarıyer",
    "Beşiktaş",
    "Ortaköy",
    "Kabataş",
    "Dolmabahçe",
    "Karaköy",
    "Galata",
    "Eminönü",
]
T0 = datetime(2026, 9, 20, 7, 0, tzinfo=UTC)
CLIPS = 14


def _brief() -> CreativeBrief:
    return CreativeBrief(
        version=3,
        requirements=[
            BriefRequirement(
                id="r1",
                kind="text",
                scope="per_clip",
                description="the landmark on each clip",
                facts={"distance_km": 20, "start": "Arnavutköy", "end": "Eminönü"},
            ),
            BriefRequirement(
                id="r2", kind="timing", scope="global", description="fast but readable"
            ),
            BriefRequirement(id="r3", kind="text", scope="global", literal="20k run"),
            BriefRequirement(
                id="r4",
                kind="order",
                scope="global",
                description="in the order I filmed",
                facts={"key": "capture_time"},
            ),
        ],
    )


def _fixture(*, capture: bool = True):
    bindings = []
    assignments = []
    for i in range(CLIPS):
        media_id = f"clip-{i}"
        path = f"users/u/analysis-proxy-{media_id}.mp4"
        duration = 3.5 + (i % 4) * 0.5
        bindings.append(
            PhoneSourceBinding(
                media_id=media_id,
                proxy_path=path,
                generation="7",
                original=OriginalMediaDescriptor(
                    sha256=f"{i:x}".rjust(64, "a"),
                    byte_count=1000 + i,
                    duration_s=duration,
                    width=1920,
                    height=1080,
                    has_audio=True,
                ),
            )
        )
        # Filmed in the reverse of the attachment order.
        taken = T0 + timedelta(minutes=(CLIPS - 1 - i) * 4)
        assignments.append(
            {
                "gcs_path": path,
                "media_id": media_id,
                "storage_generation": "7",
                "duration_s": duration,
                **(
                    {
                        "capture": {
                            "capture_time": taken.isoformat(),
                            # Every third clip has a time but no place and no landmark.
                            **(
                                {}
                                if i % 3 == 2
                                else {
                                    "place": {
                                        "sub_locality": PLACES[i % len(PLACES)],
                                        "locality": "İstanbul",
                                        "country": "Türkiye",
                                    }
                                }
                            ),
                        }
                    }
                    if capture
                    else {}
                ),
            }
        )
    return tuple(bindings), assignments


@pytest.fixture
def harness(monkeypatch):
    def build(*, brief: CreativeBrief | None = None, capture: bool = True):
        bindings, assignments = _fixture(capture=capture)
        user_id = uuid.uuid4()
        snapshot = {
            PHONE_SOURCES_FIELD: [b.model_dump(mode="json") for b in bindings],
            "creator_generation_id": "generation",
        }
        job = SimpleNamespace(
            id=uuid.uuid4(),
            user_id=user_id,
            assembly_plan=copy.deepcopy(snapshot),
            status="queued",
            all_candidates={
                "clip_paths": [b.proxy_path for b in bindings],
                "edit_format": "montage",
                # The item's column default: what broke every plain-lane job.
                "landscape_fit": "fit",
                "creator_strategy": {
                    "render_program": "guided",
                    "direction": "guided_story",
                    "archetype": "day_vlog",
                },
            },
            error_detail=None,
            failure_reason=None,
            content_plan_item_id=uuid.uuid4(),
        )
        session = Mock()

        @contextmanager
        def sessions():
            yield session

        monkeypatch.setattr(gb, "_sync_session", sessions)
        monkeypatch.setattr(gb, "_lock_owned_entry_job", lambda *args: (job, 3))
        monkeypatch.setattr(
            gb, "_load_unified_montage_inputs", lambda _job_id: (user_id, assignments, brief)
        )
        # KRI-217: no Visuals unless a test adds them.
        monkeypatch.setattr(gb, "_load_unified_montage_visuals", lambda *_a, **_k: [])

        def fake_guided_plan(_job_id, guided):
            plan = compile_execution_plan(guided, track=None)
            job.assembly_plan["guided_story_execution_plan"] = plan
            return plan, None

        monkeypatch.setattr(gb, "_guided_execution_plan", fake_guided_plan)

        def fake_enrich(results, *, make_ctx, on_updated=None, budget_s=45.0, creator_text=""):
            out = []
            for entry, ref in results:
                index = int(str(entry["media_id"]).rsplit("-", 1)[1])
                if index % 3 != 2:  # every third clip: the landmark agent had no answer
                    entry = clip_facts.with_landmark_fact(
                        entry,
                        ClipFact(
                            kind="landmark",
                            value=PLACES[index % len(PLACES)],
                            provenance="inferred",
                            confidence=0.8,
                        ),
                    )
                out.append((entry, ref))
            return out

        monkeypatch.setattr(clip_facts, "enrich_clip_facts", fake_enrich)
        monkeypatch.setattr(gb.settings, "phone_rendering_enabled", True)
        monkeypatch.setattr(gb.settings, "clip_facts_enabled", True)
        monkeypatch.setattr(
            gb.settings,
            "phone_render_verified_features",
            [
                "basicComposition",
                "local1080Export",
                "positionedText",
                "animatedText",
                "authoredText",
                "audioMix",
            ],
        )
        plain = Mock(
            side_effect=AssertionError(
                "the voiceover montage lane must not run for a non-voiceover montage"
            )
        )
        monkeypatch.setattr(gb, "_run_phone_voiceover_montage_job", plain)
        cloud = Mock(side_effect=AssertionError("phone job entered the cloud renderer"))
        monkeypatch.setattr(gb, "_run_guided_story_job", cloud)
        return job, snapshot, session, bindings, plain

    return build


def test_a_non_voiceover_montage_renders_the_east_run_brief_as_a_guided_device_job(harness):
    job, _snapshot, session, _bindings, plain = harness(brief=_brief())

    gb._run_generative_job(str(job.id))

    assert job.status == "awaiting_device"
    plain.assert_not_called()
    assert isinstance(job.assembly_plan["guided_edit"], dict)
    status = device_status(job, "guided_story")
    assert status.request.identity.variant_id == "guided_story"
    variant = job.assembly_plan["variants"][0]
    assert variant["render_status"] == "awaiting_device"
    assert variant["render_destination"] == "device"

    record = job.assembly_plan["unified_montage"]
    # Filmed in reverse of the attachment order: capture time wins because the brief asked.
    assert record["ordering_basis"] == "capture_time"
    assert record["clip_ids"] == [f"clip-{i}" for i in reversed(range(CLIPS))]
    # A title written from the creator's words plus the route facts, Turkish kept.
    assert record["title"] == "20k run · Arnavutköy → Eminönü"
    assert unicodedata.is_normalized("NFC", record["title"])
    labels = {row["media_id"]: row for row in record["labels"]}
    assert labels["clip-0"]["text"] == "Arnavutköy"
    assert labels["clip-0"]["inferred"] is True
    # The clips the landmark agent could not name get no invented label.
    assert set(record["dropped_label_clip_ids"]) == {f"clip-{i}" for i in (2, 5, 8, 11)}

    plan = GuidedStoryExecutionPlan.model_validate(job.assembly_plan["guided_story_execution_plan"])
    windows = {
        moment.media_id: moment.output_end_s - moment.output_start_s
        for moment in plan.story_timeline
    }
    for media_id, row in labels.items():
        assert windows[media_id] + 0.001 >= min(min_display_s(len(row["text"])), 3.5)
    texts = [element.text for element in plan.text_elements]
    assert "20k run · Arnavutköy → Eminönü" in texts
    assert "Rumeli Hisarı" in texts
    assert len(status.request.recipe.text_layers) == len(texts)
    assert variant["text_elements"], "the editor needs the per-clip text lane"


def test_receipts_say_what_was_met_and_what_was_partial(harness):
    job, *_ = harness(brief=_brief())
    gb._run_generative_job(str(job.id))
    receipts = {
        row["requirement_id"]: row
        for row in job.assembly_plan["unified_montage"]["requirement_receipts"]
    }
    assert receipts["r1"]["status"] == "partial"
    assert "10 of 14" in receipts["r1"]["reason"]
    assert receipts["r1"]["inferred"], "guessed landmarks must be listed for correction"
    # KRI-208: the East Run shape. The creator said Arnavutköy -> Eminönü but the clips
    # were filmed the other way round; the plan keeps filming order and says so.
    assert receipts["r4"]["status"] == "partial"
    assert "reverse of the route you gave (Arnavutköy → Eminönü)" in receipts["r4"]["reason"]
    assert "ending at Arnavutköy" in receipts["r4"]["reason"]
    assert "I kept filming order" in receipts["r4"]["reason"]
    assert job.assembly_plan["unified_montage"]["ordering_basis"] == "capture_time"
    assert receipts["r3"]["status"] == "met"
    # "Fast but readable" has no number to check: nothing judged it, so no receipt.
    assert "r2" not in receipts


def test_without_capture_times_the_order_stays_attachment_and_says_so(harness):
    job, *_ = harness(brief=_brief(), capture=False)
    gb._run_generative_job(str(job.id))
    record = job.assembly_plan["unified_montage"]
    assert record["ordering_basis"] == "attachment"
    assert record["clip_ids"] == [f"clip-{i}" for i in range(CLIPS)]
    receipts = {row["requirement_id"]: row for row in record["requirement_receipts"]}
    # An unbound job keeps its original verdict ("partly"); the stricter "Couldn't" applies only
    # where an authority is bound (see the bound-job tests at the end of this file).
    assert receipts["r4"]["status"] == "partial"


def test_no_brief_still_produces_a_plain_guided_montage(harness):
    job, *_ = harness(brief=None)
    gb._run_generative_job(str(job.id))
    assert job.status == "awaiting_device"
    record = job.assembly_plan["unified_montage"]
    assert record["labels"] == []
    # Nothing to title with: omit visible text rather than leaking the internal
    # snapshot label or using unrequested place text.
    assert record["title"] is None
    assert record["title_source"] == "none"
    assert "opening_title" not in job.assembly_plan["guided_edit"]["approved_proposal"]
    plan = GuidedStoryExecutionPlan.model_validate(job.assembly_plan["guided_story_execution_plan"])
    assert all(element.id != "guided-title" for element in plan.text_elements)
    assert "requirement_receipts" not in record


def test_a_non_voiceover_montage_takes_unified_with_no_flag_set(harness):
    """KRI-220: no flag, no allowlist -- every non-voiceover montage is unified, even at
    the item's default `landscape_fit="fit"` the removed plain lane rejected."""
    assert not hasattr(gb.settings, "montage_unified_plan_enabled")
    assert not hasattr(gb.settings, "montage_unified_plan_for")
    job, _snapshot, _session, _bindings, plain = harness(brief=_brief())
    assert job.all_candidates["landscape_fit"] == "fit"
    assert not job.all_candidates.get("voiceover_gcs_path")
    gb._run_generative_job(str(job.id))
    plain.assert_not_called()
    assert job.status == "awaiting_device"
    assert "unified_montage" in job.assembly_plan


def test_unified_call_site_passes_the_explicit_creator_fit_to_the_guided_runner(
    harness, monkeypatch
):
    """KRI-285: only an explicit `creator_render_shape` letterboxes the unified plan;
    the item's own default `landscape_fit="fit"` never does."""
    job, _snapshot, _session, _bindings, _plain = harness(brief=_brief())
    real = gb._run_phone_guided_job
    seen: list[str] = []

    def spy(*args, **kwargs):
        seen.append(kwargs.get("landscape_fit"))
        return real(*args, **kwargs)

    monkeypatch.setattr(gb, "_run_phone_guided_job", spy)
    assert job.all_candidates["landscape_fit"] == "fit"
    gb._run_generative_job(str(job.id))
    assert seen == ["fill"]
    assert job.assembly_plan["variants"][0]["landscape_fit"] == "fill"

    job2, _s, _se, _b, _p = harness(brief=_brief())
    job2.all_candidates = {
        **job2.all_candidates,
        "creator_render_shape": {"output_orientation": "portrait", "landscape_fit": "fit"},
    }
    seen.clear()
    gb._run_generative_job(str(job2.id))
    assert seen == ["fit"]
    assert job2.assembly_plan["variants"][0]["landscape_fit"] == "fit"


def test_a_voiceover_montage_never_takes_the_unified_lane(harness, monkeypatch):
    job, _snapshot, _session, _bindings, plain = harness(brief=_brief())
    plain.side_effect = None
    job.all_candidates["voiceover_gcs_path"] = "voiceover-uploads/direct/u/i/voice.m4a"
    unified = Mock(side_effect=AssertionError("a voiceover edit is not a unified montage"))
    monkeypatch.setattr(gb, "_run_phone_unified_montage_job", unified)
    monkeypatch.setattr(gb.settings, "phone_narration_rendering_enabled", True)
    gb._run_generative_job(str(job.id))
    plain.assert_called_once()


def test_approved_montage_skips_raw_speech_probe_and_unrequested_recording(harness, monkeypatch):
    from app.services import phone_speech_montage_job
    from app.services.creator_render_contract import CONTRACT_FIELD, CreatorRenderContract

    job, *_ = harness(brief=None)
    contract = CreatorRenderContract(
        generation_id=job.assembly_plan["creator_generation_id"]
    ).rebind()
    job.assembly_plan[CONTRACT_FIELD] = contract.model_dump(mode="json")
    job.all_candidates["voiceover_gcs_path"] = "unselected-recording.m4a"
    speech = Mock(side_effect=AssertionError("raw speech inference is not route authority"))
    unified = Mock(return_value=None)
    monkeypatch.setattr(phone_speech_montage_job, "run_phone_speech_montage_job", speech)
    monkeypatch.setattr(gb, "_run_phone_unified_montage_job", unified)
    gb._run_generative_job(str(job.id))
    speech.assert_not_called()
    unified.assert_called_once()


def test_approved_source_cannot_fall_back_to_unified_when_planner_declines(harness, monkeypatch):
    from app.services import phone_speech_montage_job
    from app.services.creator_render_contract import CONTRACT_FIELD, CreatorRenderContract

    job, *_ = harness(brief=None)
    contract = CreatorRenderContract(
        generation_id=job.assembly_plan["creator_generation_id"]
    ).rebind(
        original_audio="require",
        audio_source_ids=("clip-0",),
    )
    job.assembly_plan[CONTRACT_FIELD] = contract.model_dump(mode="json")
    speech = Mock(return_value=False)
    unified = Mock(side_effect=AssertionError("silent fallback"))
    failure = Mock(return_value=True)
    monkeypatch.setattr(phone_speech_montage_job, "run_phone_speech_montage_job", speech)
    monkeypatch.setattr(gb, "_run_phone_unified_montage_job", unified)
    monkeypatch.setattr(gb, "_fail_job", failure)
    gb._run_generative_job(str(job.id))
    speech.assert_called_once()
    unified.assert_not_called()
    assert "confirmed" in failure.call_args.args[1]
    # The unified montage cannot carry camera-audio sources: the decline is typed
    # and rides beside the unchanged `phone_plan_unsupported` failure code.
    assert failure.call_args.kwargs["failure_reason"] == "phone_plan_unsupported"
    decline = failure.call_args.kwargs["decline"]
    assert decline["decline_reason"] == "capability_unavailable"
    assert decline["field_path"] == "montage_audio.source_media_ids[]"
    assert decline["alternative"]


def test_unresolved_contract_declines_as_a_typed_choice_before_any_planning(harness, monkeypatch):
    from app.services import phone_speech_montage_job
    from app.services.creator_render_contract import CONTRACT_FIELD, CreatorRenderContract

    job, *_ = harness(brief=None)
    contract = CreatorRenderContract(
        generation_id=job.assembly_plan["creator_generation_id"]
    ).rebind(unresolved=("I need capture times for every selected clip.",))
    job.assembly_plan[CONTRACT_FIELD] = contract.model_dump(mode="json")
    speech = Mock(side_effect=AssertionError("planning started on an unresolved contract"))
    unified = Mock(side_effect=AssertionError("planning started on an unresolved contract"))
    failure = Mock(return_value=True)
    monkeypatch.setattr(phone_speech_montage_job, "run_phone_speech_montage_job", speech)
    monkeypatch.setattr(gb, "_run_phone_unified_montage_job", unified)
    monkeypatch.setattr(gb, "_fail_job", failure)
    gb._run_generative_job(str(job.id))
    assert failure.call_args.kwargs["failure_reason"] == "phone_plan_unsupported"
    assert failure.call_args.kwargs["decline"]["decline_reason"] == "needs_choice"
    assert "capture times" in failure.call_args.args[1]


def test_redelivery_after_planning_reuses_the_pinned_plan(harness):
    job, *_ = harness(brief=_brief())
    gb._run_generative_job(str(job.id))
    first = copy.deepcopy(job.assembly_plan["guided_edit"])
    request = device_status(job, "guided_story").request
    gb._run_generative_job(str(job.id))
    assert job.assembly_plan["guided_edit"] == first
    assert device_status(job, "guided_story").request == request


@pytest.mark.parametrize("race", ["cancel", "owner", "generation"])
def test_stale_delivery_cannot_publish_a_plan(harness, monkeypatch, race):
    job, snapshot, _session, _bindings, _plain = harness(brief=_brief())
    if race == "cancel":
        job.status = "cancelled"
    elif race == "owner":
        monkeypatch.setattr(gb, "_lock_owned_entry_job", lambda *args: (job, 4))
    else:
        job.assembly_plan["creator_generation_id"] = "newer"
    result = gb._run_phone_unified_montage_job(
        str(job.id), snapshot, job.all_candidates, ownership_epoch=3
    )
    assert result is None
    assert "guided_edit" not in job.assembly_plan


def test_a_clip_without_a_phone_binding_fails_closed(harness):
    job, snapshot, *_ = harness(brief=_brief())
    job.all_candidates["clip_paths"] = [*job.all_candidates["clip_paths"], "users/u/other.mp4"]
    with pytest.raises(UnsupportedPhonePlan):
        gb._run_phone_unified_montage_job(
            str(job.id), snapshot, job.all_candidates, ownership_epoch=3
        )


def test_chat_text_edit_on_the_unified_plan_succeeds(harness, monkeypatch):
    """The 422 `unsupported_phone_edit` of plain-lane jobs is gone: a text-only
    save on the unified variant recompiles the recipe with the edited label."""
    from app.routes import generative_jobs as gj
    from app.services.phone_editor import prepare_phone_editor_commit

    job, *_ = harness(brief=_brief())
    gb._run_generative_job(str(job.id))
    monkeypatch.setattr(gj.settings, "guided_story_editor_v2_enabled", True)
    old = device_status(job, "guided_story").request
    variant = job.assembly_plan["variants"][0]
    edited = copy.deepcopy(variant["text_elements"])
    label = next(el for el in edited if el["text"] == "Rumeli Hisarı")
    label["text"] = "Rumelihisarı"

    def prepare(staged):
        staged.assembly_plan["variants"][0]["text_elements"] = edited
        return {
            "has_render_section": True,
            "guided_revision": {"revision_number": 2},
            "sections": {"text_elements": True, "timeline": False},
            "generation": "second",
        }

    prep = prepare_phone_editor_commit(job, "guided_story", prepare=prepare)

    new = device_status(job, "guided_story").request
    assert prep["render_destination"] == "device"
    assert new.identity.recipe_revision == old.identity.recipe_revision + 1
    assert len(new.recipe.text_layers) == len(old.recipe.text_layers)
    assert new.recipe.duration == old.recipe.duration


def test_a_creator_pinned_clip_order_survives_a_revision_render(harness):
    job, *_ = harness(brief=None)
    job.all_candidates["creator_clip_order"] = [3, 0, True, 99]
    gb._run_generative_job(str(job.id))
    record = job.assembly_plan["unified_montage"]
    assert record["clip_ids"][:2] == ["clip-3", "clip-0"]
    assert sorted(record["clip_ids"]) == sorted(f"clip-{i}" for i in range(CLIPS))
    assert record["ordering_basis"] == "creator_order"


def test_the_thread_projection_matches_a_unified_job_without_a_guided_attempt(harness):
    """A v2 thread session that dispatched with no guided proposal has no attempt
    id; a minted one on the unified guided_edit would make the projection drop the
    job and leave the chat on "preparing"."""
    from app.routes.creation_threads import _render_projection

    job, *_ = harness(brief=_brief())
    gb._run_generative_job(str(job.id))
    assert "generation_attempt_id" not in job.assembly_plan["guided_edit"]

    owner_id, item_id, plan_id, session_id = (uuid.uuid4() for _ in range(4))
    job.user_id = owner_id
    job.content_plan_item_id = item_id
    job.content_plan_ownership_epoch = 3
    thread = SimpleNamespace(
        creator_id=owner_id, content_plan_id=plan_id, active_creator_agent_session_id=session_id
    )
    plan = SimpleNamespace(id=plan_id, user_id=owner_id, ownership_epoch=3)
    item = SimpleNamespace(id=item_id, content_plan_id=plan_id, current_job_id=job.id)
    session = SimpleNamespace(
        id=session_id,
        creator_id=owner_id,
        plan_item_id=item_id,
        target_job_id=None,
        ownership_epoch=3,
        active_plan={},
        revision=1,
        render_attempts=0,
        target_variant_id=None,
        target_generation_id=None,
    )
    projection = _render_projection(thread, item=item, plan=plan, session=session, job=job)
    assert projection is not None
    assert projection["job_id"] == str(job.id)


# ---- the real inputs loader (every test above stubs it) ----------------------


class _Result:
    def __init__(self, value):
        self._value = value

    def scalar_one_or_none(self):
        return self._value


class _LoaderDb:
    def __init__(self, job, item, thread_id):
        self.job, self.item, self.thread_id = job, item, thread_id
        self.statements: list[str] = []

    def get(self, model, _identity):
        return self.job if model.__name__ == "Job" else self.item

    def execute(self, statement):
        self.statements.append(
            str(statement.compile(compile_kwargs={"literal_binds": True})).replace("-", "")
        )
        return _Result(self.thread_id)


def _loader(monkeypatch, *, item, thread_id, brief_flag):
    from app.kria import brief as brief_module

    user_id = uuid.uuid4()
    job = SimpleNamespace(
        id=uuid.uuid4(),
        user_id=user_id,
        assembly_plan={},
        content_plan_item_id=item.id if item is not None else None,
    )
    db = _LoaderDb(job, item, thread_id)

    @contextmanager
    def sessions():
        yield db

    latest = _brief()
    loaded: list[object] = []

    def load(_db, thread):
        loaded.append(thread)
        return latest

    monkeypatch.setattr(gb, "_sync_session", sessions)
    monkeypatch.setattr(brief_module, "load_latest_brief_sync", load)
    monkeypatch.setattr(gb.settings, "kria_creative_brief_enabled", brief_flag)
    monkeypatch.setattr(gb.settings, "kria_creative_brief_user_ids", [])
    return job, db, latest, loaded


def test_loader_reads_approved_snapshot_even_after_writer_flag_rollback(monkeypatch):
    item = SimpleNamespace(id=uuid.uuid4(), clip_assignments=[{"gcs_path": "a"}, "junk"])
    thread_id = uuid.uuid4()
    job, db, latest, loaded = _loader(monkeypatch, item=item, thread_id=thread_id, brief_flag=True)

    from app.kria.brief_binding import BriefBinding, snapshot_media

    job.assembly_plan = {
        "creator_brief_binding": BriefBinding.create(
            thread_id, latest, media_snapshot=snapshot_media(item)
        ).model_dump(mode="json")
    }
    item.clip_assignments = [{"gcs_path": "replacement"}]
    monkeypatch.setattr(gb.settings, "kria_creative_brief_enabled", False)
    user_id, assignments, brief = gb._load_unified_montage_inputs(str(job.id))

    assert user_id == job.user_id
    assert assignments == [{"gcs_path": "a"}]
    assert assignments[0] is not item.clip_assignments[0], "a copy, never the live row"
    assert brief == latest and brief is not latest and loaded == []
    # The thread lookup is scoped to this item and this creator.
    (statement,) = db.statements
    assert str(item.id).replace("-", "") in statement
    assert str(job.user_id).replace("-", "") in statement


def test_loader_has_no_brief_when_the_flag_is_off(monkeypatch):
    item = SimpleNamespace(id=uuid.uuid4(), clip_assignments=[{"gcs_path": "a"}])
    job, db, _latest, loaded = _loader(
        monkeypatch, item=item, thread_id=uuid.uuid4(), brief_flag=False
    )
    _user, assignments, brief = gb._load_unified_montage_inputs(str(job.id))
    assert brief is None and loaded == [] and db.statements == []
    assert assignments == [{"gcs_path": "a"}]


def test_loader_has_no_brief_when_the_item_has_no_thread(monkeypatch):
    item = SimpleNamespace(id=uuid.uuid4(), clip_assignments=None)
    job, _db, _latest, loaded = _loader(monkeypatch, item=item, thread_id=None, brief_flag=True)
    _user, assignments, brief = gb._load_unified_montage_inputs(str(job.id))
    assert assignments == [] and brief is None and loaded == []


def test_loader_handles_a_job_with_no_plan_item(monkeypatch):
    job, _db, _latest, loaded = _loader(monkeypatch, item=None, thread_id=None, brief_flag=True)
    user_id, assignments, brief = gb._load_unified_montage_inputs(str(job.id))
    assert user_id == job.user_id and assignments == [] and brief is None and loaded == []


def test_landmark_creator_text_is_the_briefs_exact_words_plus_the_first_message():
    from app.pipeline.unified_montage import BriefView

    view = BriefView(
        clip_literals={"c1": "Km   5"}, title_literal="20k run", global_literal="Slow  Sunday"
    )
    text = gb._landmark_creator_text(view, "my run   from A to B")
    assert text == "Km 5 20k run Slow Sunday my run from A to B"
    assert gb._landmark_creator_text(BriefView(), "") == ""
    assert len(gb._landmark_creator_text(BriefView(), "x" * 900)) == 400


def test_first_user_message_is_best_effort_and_never_raises(monkeypatch):
    job = SimpleNamespace(content_plan_item_id=uuid.uuid4())
    session = Mock()
    session.get.return_value = job
    session.execute.return_value.scalar_one_or_none.return_value = "my run from A to B"

    @contextmanager
    def sessions():
        yield session

    monkeypatch.setattr(gb, "_sync_session", sessions)
    assert gb._first_user_message(str(uuid.uuid4())) == "my run from A to B"

    session.execute.return_value.scalar_one_or_none.return_value = None
    assert gb._first_user_message(str(uuid.uuid4())) == ""
    session.execute.side_effect = RuntimeError("db down")
    assert gb._first_user_message(str(uuid.uuid4())) == ""
    session.get.return_value = SimpleNamespace(content_plan_item_id=None)
    assert gb._first_user_message(str(uuid.uuid4())) == ""


def _checkpoint_harness(monkeypatch, item, *, job_item_id="present"):
    """A real (in-memory) PlanItem behind a fake session: the REAL sole-writer facade runs,
    which is what a Mock item hid."""
    session = Mock()
    session.get.return_value = (
        None
        if job_item_id == "no-job"
        else SimpleNamespace(
            content_plan_item_id=None if job_item_id == "no-item" else uuid.uuid4()
        )
    )
    session.execute.return_value.scalar_one_or_none.return_value = (
        None if job_item_id == "missing-row" else item
    )

    @contextmanager
    def sessions():
        yield session

    monkeypatch.setattr(gb, "_sync_session", sessions)
    # No speech-cleanup analysis rows in this harness (the rollout flag is off in prod paths
    # that reach here too); the facade only needs the "current analysis" to be None.
    monkeypatch.setattr(
        "app.services.speech_cleanup_preflight.mutation_current_analysis_sync",
        lambda *a, **k: None,
    )
    return session


def _plan_item(rows):
    from app.models import PlanItem

    item = PlanItem()
    item.id = uuid.uuid4()
    item.clip_assignments = rows
    item.clip_gcs_paths = [row["gcs_path"] for row in rows]
    item.edit_format = "montage"
    item.audio_mode = "kria"
    item.speech_cleanup_enabled = True
    return item


def _row(**overrides):
    return {
        "media_id": "clip-0",
        "gcs_path": "users/u/analysis-proxy-clip-0.mp4",
        "shot_id": None,
        "storage_generation": "7",
        "analysis": {"best_moments": [{"start_s": 1.0}]},
        **overrides,
    }


def _enriched(generation="7", **overrides):
    entry = {
        "media_id": "clip-0",
        "gcs_path": "users/u/analysis-proxy-clip-0.mp4",
        "storage_generation": generation,
        "analysis": {
            clip_facts.FACTS_KEY: [
                {"kind": "landmark", "value": "Galata Bridge", "provenance": "inferred"}
            ],
            clip_facts.LANDMARK_GENERATION_KEY: f"{generation}|en",
        },
    }
    entry.update(overrides)
    return entry


JOB = "00000000-0000-0000-0000-000000000001"


def test_unified_facts_are_persisted_through_the_sole_writer_facade(monkeypatch):
    """A landmark asked for during a render must survive it, or every re-render and Celery
    retry asks (and pays) again. Merged with what is stored, nothing else touched, and the
    speech-cleanup identity is not disturbed."""
    row = _row(
        analysis={
            "best_moments": [{"start_s": 1.0}],
            clip_facts.FACTS_KEY: [
                {"kind": "place", "value": "Ortaköy", "provenance": "geocode"},
                {"kind": "landmark", "value": "Old Guess", "provenance": "inferred"},
            ],
        }
    )
    other = {"media_id": "clip-1", "gcs_path": "users/u/x.mp4", "shot_id": None, "analysis": {}}
    item = _plan_item([row, other])
    session = _checkpoint_harness(monkeypatch, item)

    gb._checkpoint_unified_facts(JOB, _enriched(), None)

    saved = item.clip_assignments[0]["analysis"]
    assert saved["best_moments"] == [{"start_s": 1.0}], "other analysis is left alone"
    assert saved[clip_facts.LANDMARK_GENERATION_KEY] == "7|en"
    by_kind = {f["kind"]: f["value"] for f in saved[clip_facts.FACTS_KEY]}
    assert by_kind == {"place": "Ortaköy", "landmark": "Galata Bridge"}, "merged, not overwritten"
    assert item.clip_assignments[1]["gcs_path"] == other["gcs_path"]
    # The facade ran: an analysis-only write superseded nothing and kept the consent mirror.
    assert item.speech_cleanup_enabled is True
    session.commit.assert_called_once()


@pytest.mark.parametrize(
    "mismatch",
    [{"media_id": "other"}, {"gcs_path": "users/u/elsewhere.mp4"}, {"storage_generation": "8"}],
)
def test_unified_facts_are_only_written_for_the_exact_clip_and_generation(monkeypatch, mismatch):
    item = _plan_item([_row()])
    before = [dict(r) for r in item.clip_assignments]
    session = _checkpoint_harness(monkeypatch, item)
    gb._checkpoint_unified_facts(JOB, _enriched(**mismatch), None)
    assert item.clip_assignments == before
    session.commit.assert_not_called()


@pytest.mark.parametrize("missing", ["no-job", "no-item", "missing-row"])
def test_unified_facts_checkpoint_tolerates_a_missing_job_or_item(monkeypatch, missing):
    item = _plan_item([_row()])
    before = [dict(r) for r in item.clip_assignments]
    session = _checkpoint_harness(monkeypatch, item, job_item_id=missing)
    gb._checkpoint_unified_facts(JOB, _enriched(), None)
    assert item.clip_assignments == before
    session.commit.assert_not_called()


def test_a_failed_facts_checkpoint_never_fails_the_render(monkeypatch):
    item = _plan_item([_row()])
    session = _checkpoint_harness(monkeypatch, item)
    session.execute.side_effect = RuntimeError("db down")
    gb._checkpoint_unified_facts(JOB, _enriched(), None)


# ── KRI-217: Visuals photos in the unified phone montage ─────────────────────


def _photos(job, count: int = 2) -> list:
    from app.pipeline.unified_montage import UnifiedClip

    photos = []
    for index in range(count):
        row_id = str(uuid.uuid4())
        photos.append(
            UnifiedClip(
                media_id=row_id,
                proxy_path=f"users/{job.user_id}/plan/{job.content_plan_item_id}/pool/p{index}.jpg",
                generation="17",
                duration_s=0.0,
                lane="asset",
                kind="image",
                manifest_id=f"asset-{row_id}",
                aspect=4 / 3,
            )
        )
    return photos


def test_ready_photos_reach_the_phone_recipe(harness, monkeypatch):
    """Thread 6BF1213E: videos plus Visuals photos render on the phone, photos included."""
    import json

    from app.services import phone_visuals
    from app.services.phone_sources import PhoneVisualBinding
    from tests._prod_profile import PROD_VERIFIED_FEATURES

    job, *_ = harness(brief=None)
    monkeypatch.setattr(gb.settings, "phone_render_verified_features", list(PROD_VERIFIED_FEATURES))
    photos = _photos(job)
    monkeypatch.setattr(gb, "_load_unified_montage_visuals", lambda *_a, **_k: photos)
    timeline_visuals: list[set[str]] = []

    def bind(_open, *, job_id, story_timeline, kinds):  # noqa: ANN001, ANN202
        assert "image" in kinds
        timeline_visuals.append(
            {moment.media_id for moment in story_timeline if moment.lane == "asset"}
        )
        return tuple(
            PhoneVisualBinding(
                media_id=photo.media_id,
                gcs_path=photo.proxy_path,
                generation=photo.generation,
                sha256="b" * 64,
                byte_count=2048,
                kind="image",
            )
            for photo in photos
        )

    monkeypatch.setattr(phone_visuals, "bind_phone_visuals", bind)

    gb._run_generative_job(str(job.id))

    assert job.status == "awaiting_device"
    record = job.assembly_plan["unified_montage"]
    assert record["visual_ids"] == [photo.media_id for photo in photos]
    order = record["clip_ids"]
    assert order[0] == "clip-0", "the montage opens on a clip"
    assert timeline_visuals == [{photo.media_id for photo in photos}]
    recipe = json.dumps(device_status(job, "guided_story").request.recipe.model_dump(mode="json"))
    for photo in photos:
        assert photo.media_id in recipe


def test_a_photo_the_phone_cannot_draw_fails_instead_of_being_dropped(harness, monkeypatch):
    job, snapshot, *_ = harness(brief=None)  # the harness verifies no stillImages
    monkeypatch.setattr(gb, "_load_unified_montage_visuals", lambda *_a, **_k: _photos(job, 1))

    with pytest.raises(UnsupportedPhonePlan):
        gb._run_phone_unified_montage_job(
            str(job.id), snapshot, job.all_candidates, ownership_epoch=3
        )


def test_the_worker_narrows_visuals_to_an_explicit_selection(harness, monkeypatch):
    job, snapshot, *_ = harness(brief=None)
    job.all_candidates["creator_strategy"] = {
        **job.all_candidates["creator_strategy"],
        "media_scope": "selected",
        "selected_media_ids": ["clip-0", "asset-x"],
    }
    calls: list = []

    def load(_job_id, *, selected):  # noqa: ANN001, ANN202
        calls.append(selected)
        return []

    monkeypatch.setattr(gb, "_load_unified_montage_visuals", load)
    gb._run_phone_unified_montage_job(str(job.id), snapshot, job.all_candidates, ownership_epoch=3)

    assert calls == [frozenset({"clip-0", "asset-x"})]


def test_a_clip_shorter_than_any_cut_reaches_the_phone_recipe(harness):
    """Thread 6BF1213E carried a 0.3s iPhone clip: it plays whole on the phone."""
    job, *_ = harness(brief=None)
    job.assembly_plan[PHONE_SOURCES_FIELD][4]["original"]["duration_s"] = 0.29833333333333334

    gb._run_generative_job(str(job.id))

    assert job.status == "awaiting_device"
    plan = GuidedStoryExecutionPlan.model_validate(job.assembly_plan["guided_story_execution_plan"])
    short = next(moment for moment in plan.story_timeline if moment.media_id == "clip-4")
    assert short.source_start_s == 0.0
    assert 0.297 <= short.source_end_s <= 0.29834
    assert device_status(job, "guided_story").request.recipe is not None


# --- KRI-306: the creator's output shape rides the Job into the phone recipe ---------------


def _guided_track_scales(job) -> list[float]:
    recipe = device_status(job, "guided_story").request.recipe
    track = next(t for t in recipe.tracks if t.id == "story")
    return [clip.transform.scale for clip in track.clips]


def test_without_a_creator_choice_the_shape_is_inferred_and_nothing_is_letterboxed(harness):
    job, *_ = harness(brief=_brief())
    assert "creator_render_shape" not in job.all_candidates
    gb._run_generative_job(str(job.id))
    # Fourteen 1920x1080 clips vote landscape on their own; no bars, ever, unasked.
    recipe = device_status(job, "guided_story").request.recipe
    assert (recipe.canvas.width, recipe.canvas.height) == (1920, 1080)
    assert set(_guided_track_scales(job)) == {1.0}
    assert job.assembly_plan["variants"][0]["landscape_fit"] == "fill"


def test_a_vertical_choice_with_bars_pins_the_canvas_and_letterboxes_sideways_clips(harness):
    job, *_ = harness(brief=_brief())
    job.all_candidates["creator_render_shape"] = {
        "output_orientation": "portrait",
        "landscape_fit": "fit",
    }

    gb._run_generative_job(str(job.id))

    snapshot = job.assembly_plan["guided_edit"]["approved_proposal"]
    assert snapshot["output_orientation"] == "portrait"
    assert snapshot["output_orientation_reason"] == "The creator selected this output format."
    recipe = device_status(job, "guided_story").request.recipe
    assert (recipe.canvas.width, recipe.canvas.height) == (1080, 1920)
    # The written value reaches `compile_phone_guided_plan(landscape_fit=...)`.
    assert _guided_track_scales(job) == pytest.approx([0.31640625] * CLIPS)
    assert job.assembly_plan["variants"][0]["landscape_fit"] == "fit"


def test_a_vertical_choice_with_crop_fills_the_frame(harness):
    job, *_ = harness(brief=_brief())
    job.all_candidates["creator_render_shape"] = {
        "output_orientation": "portrait",
        "landscape_fit": "fill",
    }
    gb._run_generative_job(str(job.id))
    recipe = device_status(job, "guided_story").request.recipe
    assert (recipe.canvas.width, recipe.canvas.height) == (1080, 1920)
    assert set(_guided_track_scales(job)) == {1.0}


def test_a_landscape_choice_pins_a_1920x1080_canvas_without_bars(harness):
    job, *_ = harness(brief=_brief())
    job.all_candidates["creator_render_shape"] = {
        "output_orientation": "landscape",
        "landscape_fit": "fill",
    }
    gb._run_generative_job(str(job.id))
    assert job.assembly_plan["guided_edit"]["approved_proposal"]["output_orientation"] == (
        "landscape"
    )
    recipe = device_status(job, "guided_story").request.recipe
    assert (recipe.canvas.width, recipe.canvas.height) == (1920, 1080)
    assert set(_guided_track_scales(job)) == {1.0}


# --- KRI-470 PR-G: an unmet required order blocks a bound montage (never a silent success) ---


def _bound_job(harness, *, extra_order_facts: dict):
    """Filming order (which the plan can follow) PLUS a second rule the checker has no key for."""
    from app.kria.brief_binding import BriefBinding

    brief = CreativeBrief(
        version=1,
        requirements=[
            BriefRequirement(
                id="r4",
                kind="order",
                scope="global",
                description="in the order I filmed",
                facts={"key": "capture_time"},
            ),
            BriefRequirement(
                id="r5",
                kind="order",
                scope="global",
                description="alphabetically by the place",
                facts={"key": "alphabetical", **extra_order_facts},
            ),
        ],
    )
    job, *_ = harness(brief=brief)
    binding = BriefBinding.create(uuid.uuid4(), brief)
    job.assembly_plan["creator_brief_binding"] = binding.model_dump(mode="json")
    return job, copy.deepcopy(job.assembly_plan)


def test_a_required_order_the_plan_cannot_follow_asks_before_simplifying(harness):
    job, snapshot = _bound_job(harness, extra_order_facts={})

    with pytest.raises(UnsupportedPhonePlan) as caught:
        gb._run_phone_unified_montage_job(
            str(job.id), snapshot, job.all_candidates, ownership_epoch=3
        )

    assert "Should I try again or simplify this request?" in str(caught.value)
    assert "ordering rule" in str(caught.value)
    recovery = job.assembly_plan["request_recovery"]
    failed = {
        row["requirement_id"]: row
        for row in recovery["requirement_receipts"]
        if row["verification"] == "checked" and row["status"] != "met"
    }
    assert failed["r5"]["status"] == "not_possible"
    assert "guided_edit" not in job.assembly_plan  # nothing was published to render


def test_an_optional_order_preference_the_plan_cannot_follow_does_not_block(harness):
    job, snapshot = _bound_job(harness, extra_order_facts={"strength": "preference"})

    planned = gb._run_phone_unified_montage_job(
        str(job.id), snapshot, job.all_candidates, ownership_epoch=3
    )

    assert planned is not None and isinstance(planned["guided_edit"], dict)
    receipts = {
        row["requirement_id"]: row for row in planned["unified_montage"]["requirement_receipts"]
    }
    assert receipts["r5"]["verification"] == "unchecked"
    assert receipts["r4"]["status"] == "met"


def test_a_bound_job_without_capture_times_fails_the_required_filming_order(harness):
    """The same shape as the unbound test above, but bound: "Couldn't", and it blocks."""
    from app.kria.brief_binding import BriefBinding

    brief = CreativeBrief(
        version=1,
        requirements=[
            BriefRequirement(
                id="r4",
                kind="order",
                scope="global",
                description="in the order I filmed",
                facts={"key": "capture_time"},
            )
        ],
    )
    job, *_ = harness(brief=brief, capture=False)
    job.assembly_plan["creator_brief_binding"] = BriefBinding.create(
        uuid.uuid4(), brief
    ).model_dump(mode="json")

    with pytest.raises(UnsupportedPhonePlan):
        gb._run_phone_unified_montage_job(
            str(job.id), copy.deepcopy(job.assembly_plan), job.all_candidates, ownership_epoch=3
        )

    failed = job.assembly_plan["request_recovery"]["requirement_receipts"]
    assert [(row["requirement_id"], row["status"]) for row in failed] == [("r4", "not_possible")]


# --- KRI-470: a requested title with no words blocks the render with a typed, askable decline ---


def _title_block_job(harness):
    from app.kria.brief_binding import BriefBinding

    brief = CreativeBrief(
        version=1,
        requirements=[
            BriefRequirement(
                id="r4",
                kind="order",
                scope="global",
                description="in the order I filmed",
                facts={"key": "capture_time"},
            ),
            BriefRequirement(
                id="r5",
                kind="text",
                scope="title",
                description="a hook animated with typewriter",
            ),
        ],
    )
    job, *_ = harness(brief=brief)
    binding = BriefBinding.create(uuid.uuid4(), brief)
    job.assembly_plan["creator_brief_binding"] = binding.model_dump(mode="json")
    return job, copy.deepcopy(job.assembly_plan)


def test_a_title_with_no_words_blocks_with_a_typed_needs_choice_decline(harness):
    from app.services.creator_render_contract import CreatorRenderContractError, decline_payload

    job, snapshot = _title_block_job(harness)
    with pytest.raises(CreatorRenderContractError) as caught:
        gb._run_phone_unified_montage_job(
            str(job.id), snapshot, job.all_candidates, ownership_epoch=3
        )
    message = str(caught.value)
    # No video exists: the copy says so, names what is missing and the way forward.
    assert message.startswith("I couldn't make the video yet:")
    assert "I didn't add a title" not in message
    assert 'say "continue without a title"' in message
    assert decline_payload(caught.value) == {
        "decline_reason": "needs_choice",
        "field_path": "opening_title",
        "alternative": 'Tell me the words for the title, or say "continue without a title".',
    }
    assert job.assembly_plan["request_recovery"]["message"] == message
    assert "guided_edit" not in job.assembly_plan  # nothing was published to render


def test_the_dispatcher_persists_the_typed_title_decline(harness, monkeypatch):
    job, _snapshot = _title_block_job(harness)
    failure = Mock(return_value=True)
    monkeypatch.setattr(gb, "_fail_job", failure)
    gb._run_generative_job(str(job.id))
    assert failure.call_args.kwargs["failure_reason"] == "phone_plan_unsupported"
    decline = failure.call_args.kwargs["decline"]
    assert decline["decline_reason"] == "needs_choice" and decline["field_path"] == "opening_title"
    assert decline["alternative"] in failure.call_args.args[1]


def test_a_title_with_words_does_not_block(harness):
    from app.kria.brief_binding import BriefBinding

    brief = CreativeBrief(
        version=1,
        requirements=[
            BriefRequirement(
                id="r5", kind="text", scope="title", literal="Weekend away", description="a title"
            )
        ],
    )
    job, *_ = harness(brief=brief)
    job.assembly_plan["creator_brief_binding"] = BriefBinding.create(
        uuid.uuid4(), brief
    ).model_dump(mode="json")
    planned = gb._run_phone_unified_montage_job(
        str(job.id), copy.deepcopy(job.assembly_plan), job.all_candidates, ownership_epoch=3
    )
    assert planned is not None and planned["unified_montage"]["title"] == "Weekend away"


def test_a_wordless_title_next_to_another_blocker_keeps_the_generic_ask_but_honest_copy(harness):
    from app.kria.brief_binding import BriefBinding
    from app.services.creator_render_contract import CreatorRenderContractError

    brief = CreativeBrief(
        version=1,
        requirements=[
            BriefRequirement(id="r4", kind="text", scope="title", description="a hook"),
            BriefRequirement(
                id="r5",
                kind="order",
                scope="global",
                description="alphabetically by the place",
                facts={"key": "alphabetical"},
            ),
        ],
    )
    job, *_ = harness(brief=brief)
    job.assembly_plan["creator_brief_binding"] = BriefBinding.create(
        uuid.uuid4(), brief
    ).model_dump(mode="json")
    with pytest.raises(UnsupportedPhonePlan) as caught:
        gb._run_phone_unified_montage_job(
            str(job.id), copy.deepcopy(job.assembly_plan), job.all_candidates, ownership_epoch=3
        )
    message = str(caught.value)
    assert not isinstance(caught.value, CreatorRenderContractError)  # two blockers: untyped
    assert "I couldn't make the video yet" in message and "I didn't add a title" not in message
    assert "stay in the order you attached them" in message
    assert "Should I try again or simplify this request?" in message
    # The title's own way forward is not lost behind the generic ask.
    assert message.endswith('For the title, tell me the words or say "continue without a title".')


def test_initial_creation_composes_full_request_before_pinning_phone_plan(harness, monkeypatch):
    import json

    from app.agents.edit_copilot import EditCopilotAgent

    job, snapshot, *_ = harness(brief=None)
    title = "Morning coffee by the river"
    job.all_candidates["creator_request"] = (
        f"Title: {title}. Show separate words one after another with fade in and out."
    )
    job.all_candidates["creator_strategy"]["opening_title"] = title
    seen = []

    def run(self, input, *, ctx=None):
        seen.append(input.utterance)
        return self.parse(
            json.dumps(
                {
                    "intent": "edit",
                    "confidence": 0.99,
                    "reply": "Prepared",
                    "ops": [
                        {
                            "op": "replace_text_sequence",
                            "selector": {"ids": ["guided-title"]},
                            "segments": title.split(),
                            "patch": {
                                "animation_phases": {
                                    "entrance": "fade",
                                    "exit": "fade",
                                    "loop": "none",
                                    "speed": 1,
                                }
                            },
                        }
                    ],
                }
            ),
            input,
        )

    monkeypatch.setattr(EditCopilotAgent, "run", run)
    result = gb._run_phone_unified_montage_job(
        str(job.id), snapshot, job.all_candidates, ownership_epoch=3
    )
    assert seen == [job.all_candidates["creator_request"]]
    assert result["guided_edit"]["approved_proposal"]["text_composition"]
    compiled = compile_execution_plan(result["guided_edit"], track=None)
    assert [
        row["text"] for row in compiled["text_elements"] if "::sequence-" in row["id"]
    ] == title.split()
