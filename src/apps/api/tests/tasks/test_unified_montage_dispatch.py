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
    def build(*, flag: bool = True, brief: CreativeBrief | None = None, capture: bool = True):
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

        def fake_guided_plan(_job_id, guided):
            plan = compile_execution_plan(guided, track=None)
            job.assembly_plan["guided_story_execution_plan"] = plan
            return plan, None

        monkeypatch.setattr(gb, "_guided_execution_plan", fake_guided_plan)

        def fake_enrich(results, *, make_ctx, on_updated=None, budget_s=45.0):
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
        monkeypatch.setattr(gb.settings, "montage_unified_plan_enabled", flag)
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
        plain = Mock(side_effect=AssertionError("the plain phone-montage lane must not run"))
        monkeypatch.setattr(gb, "_run_phone_montage_job", plain)
        cloud = Mock(side_effect=AssertionError("phone job entered the cloud renderer"))
        monkeypatch.setattr(gb, "_run_guided_story_job", cloud)
        return job, snapshot, session, bindings, plain

    return build


def test_flag_on_renders_the_east_run_brief_as_a_guided_device_job(harness):
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
    assert receipts["r4"]["status"] == "met"
    assert receipts["r3"]["status"] == "met"


def test_without_capture_times_the_order_stays_attachment_and_says_so(harness):
    job, *_ = harness(brief=_brief(), capture=False)
    gb._run_generative_job(str(job.id))
    record = job.assembly_plan["unified_montage"]
    assert record["ordering_basis"] == "attachment"
    assert record["clip_ids"] == [f"clip-{i}" for i in range(CLIPS)]
    receipts = {row["requirement_id"]: row for row in record["requirement_receipts"]}
    assert receipts["r4"]["status"] == "partial"


def test_no_brief_still_produces_a_plain_guided_montage(harness):
    job, *_ = harness(brief=None)
    gb._run_generative_job(str(job.id))
    assert job.status == "awaiting_device"
    record = job.assembly_plan["unified_montage"]
    assert record["labels"] == []
    # Nothing to title with: the place the clips were filmed in, a phone-read fact.
    assert record["title"] == "İstanbul"
    assert record["title_source"] == "fact"
    assert "requirement_receipts" not in record


def test_flag_off_keeps_the_plain_lane_and_never_plans_unified(harness, monkeypatch):
    job, _snapshot, _session, _bindings, plain = harness(flag=False, brief=_brief())
    plain.side_effect = None
    unified = Mock(side_effect=AssertionError("flag off must not run the unified planner"))
    monkeypatch.setattr(gb, "_run_phone_unified_montage_job", unified)

    gb._run_generative_job(str(job.id))

    plain.assert_called_once()
    unified.assert_not_called()
    assert "guided_edit" not in job.assembly_plan
    assert "unified_montage" not in job.assembly_plan


def test_allowlisted_account_gets_unified_while_the_global_flag_is_off(harness, monkeypatch):
    job, *_ = harness(flag=False, brief=_brief())
    monkeypatch.setattr(gb.settings, "montage_unified_plan_user_ids", [str(job.user_id)])
    gb._run_generative_job(str(job.id))
    assert job.status == "awaiting_device"
    assert "unified_montage" in job.assembly_plan


def test_a_voiceover_montage_never_takes_the_unified_lane(harness, monkeypatch):
    job, _snapshot, _session, _bindings, plain = harness(brief=_brief())
    plain.side_effect = None
    job.all_candidates["voiceover_gcs_path"] = "voiceover-uploads/direct/u/i/voice.m4a"
    unified = Mock(side_effect=AssertionError("a voiceover edit is not a unified montage"))
    monkeypatch.setattr(gb, "_run_phone_unified_montage_job", unified)
    monkeypatch.setattr(gb.settings, "phone_narration_rendering_enabled", True)
    gb._run_generative_job(str(job.id))
    plain.assert_called_once()


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


def test_flag_off_the_plain_lane_is_untouched_including_its_fit_guard(monkeypatch):
    """The failure the East Run thread hit stays byte-identical with the flag off."""
    from tests.tasks.test_phone_montage_dispatch import setup as montage_setup

    job, _snapshot, _session, _bindings, _cloud = montage_setup(monkeypatch)
    job.all_candidates["landscape_fit"] = "fit"
    monkeypatch.setattr(gb.settings, "montage_unified_plan_enabled", False)
    failures: list[tuple[str, str | None]] = []
    monkeypatch.setattr(gb, "mark_failed_phase", lambda *_a, **_k: None)
    monkeypatch.setattr(
        gb,
        "_fail_job",
        lambda _job_id, detail, failure_reason=None: (
            failures.append((detail, failure_reason)) or True
        ),
    )
    unified = Mock(side_effect=AssertionError("flag off must not run the unified planner"))
    monkeypatch.setattr(gb, "_run_phone_unified_montage_job", unified)

    gb._run_generative_job(str(job.id))

    assert failures == [
        ("letterboxed landscape fit is not yet supported on the phone", "phone_plan_unsupported")
    ]
    unified.assert_not_called()


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
