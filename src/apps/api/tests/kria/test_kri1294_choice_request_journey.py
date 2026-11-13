"""KRI-1294: Q3's choice echo must not become a new render instruction.

Original incident: KRI-1293, 2026-11-07, thread 2113c778, job fba2c864.
The request/strategy below are from read-only production events/debug. Provider I/O is
redacted in production: the provider answers are authored, not recorded observations.
Real planner, runtime, consent, approval, binding, composer and compiler run locally.

Failure modes, recorded before the fix:
- The choice label leaks into history/latest text and text composition asks what "these" means.
- Removing only the latest text leaves its chronological copy (and planner history) behind.
- String-based filtering erases a creator's literal text or an unrelated typed request.
- Filtering changes named omission consent or reinstates the superseded whole-clip ask.
- A future turn resurrects the old echo; a real unsupported text ask is silently ignored.
Native rendering, live provider behavior and production rollout are separate pending evidence.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import os
from pathlib import Path

import pytest
from sqlalchemy import select

from app.agents._runtime import ModelInvocation, RunContext
from app.config import settings
from app.database import AsyncSessionLocal, sync_session
from app.kria.api_schemas import ApprovalDecisionBody
from app.kria.brief_binding import BriefBinding
from app.kria.runtime import approval_fingerprint, decide_approval
from app.models import CreationThread, CreationThreadEvent, CreatorAgentApproval, PlanItem
from app.pipeline.guided_story import GuidedStoryExecutionPlan, compile_proposal_execution_plan
from app.pipeline.phone_guided_plan import compile_phone_guided_plan
from app.services.creation_text_composition import (
    compose_creation_text,
)
from app.services.phone_montage_preflight import preflight_montage_plan
from app.services.phone_sources import bind_phone_sources
from app.services.song_order import model_text
from app.tasks.kria_runtime import _claim_approval_dispatch
from tests._prod_profile import apply_prod_profile
from tests.kria.test_kri945_global_text_journey import Chat, _row, _wire
from tests.kria.test_runtime_postgres_integration import _seed_runtime_project

TEXT = "The prompt I run at the end of every Claude session"
PROMPT = (
    f'Just put this text in the middle for the whole video: "{TEXT}". Small white text '
    "like TikTok's, no captions and nothing else. Keep the whole clip, don't cut it. "
    "Mute my sound, I'll add a song on TikTok."
)
TAP = "Make it without these"
SELECTION = {"choice_selection": {"question_id": "q", "option_key": "simpler_version"}}
UPDATES = [
    {
        "kind": "text",
        "scope": "global",
        "literal": TEXT,
        "description": (
            f'Just put this text in the middle for the whole video: "{TEXT}". '
            "Small white text like TikTok's"
        ),
        "facts": {"position": "middle"},
    },
    {
        "kind": "style",
        "scope": "global",
        "description": "no captions and nothing else",
        "facts": {"captions": "off"},
    },
    {
        "kind": "style",
        "scope": "global",
        "description": "Keep the whole clip, don't cut it.",
        "facts": {"keep_whole_take": True},
    },
    {
        "kind": "audio",
        "scope": "global",
        "description": "Mute my sound",
        "facts": {"audio_intent": "source_muted"},
    },
    {
        "kind": "audio",
        "scope": "global",
        "description": "I'll add a song on TikTok.",
        "facts": {"audio_intent": "no_music"},
    },
]
STRATEGY = {
    "pacing": "balanced",
    "direction": "guided_story",
    "edit_format": "montage",
    "pinned_texts": [{"look": "tiktok", "text": TEXT, "corner": "middle_center"}],
    "caption_style": "none",
    "audio_strategy": "original_audio",
    "render_program": "guided",
    "montage_audio": {
        "source_media_ids": [],
        "preview_source_beds": False,
        "preserve_source_audio": True,
    },
    "target_duration_s": 4.5,
    "selected_media_ids": [],
    "video_reuse_policy": "once",
    "story_structure": [],
    "optional_treatments": [],
    "rationale": "The requested middle text, no captions, no music, muted clip.",
}


def answers(updates=()):
    return {
        "main_creator": {
            "action": {
                "kind": "propose_strategy",
                "strategy": STRATEGY,
                "summary": "Middle text with no captions or music.",
            },
            "brief_updates": list(updates),
            "delegates_all_choices": False,
            "starts_fresh": False,
        },
        "clip_intent_planner": [],
    }


@pytest.fixture(autouse=True)
def flags(monkeypatch):
    apply_prod_profile(monkeypatch)
    # Preserve the historical hold while KRI-1295 makes the enabled one-clip path work.
    monkeypatch.setattr(settings, "keep_whole_take_plan_enabled", False)
    for name in (
        "kria_brief_binding_enabled",
        "kria_approval_readiness_enabled",
        "kria_choice_questions_enabled",
    ):
        monkeypatch.setattr(settings, name, True)


def seed():
    user_id, thread_id, _ = _seed_runtime_project()
    row = _row(
        {
            "uuid": "q3",
            "duration_s": 11.0,
            "subject": "Desk",
            "summary": "A silent desk clip",
            "setting": "room",
            "activity": "working",
        },
        0,
        speaking=False,
    )
    row["analysis"]["understanding"]["speech"] = {"has_speech": False}
    row["analysis"]["best_moments"] = [{"start_s": 5.0, "end_s": 9.5, "energy": 0.9}]
    row["has_audio"] = False
    row["upload_contract"]["proxy"]["original"]["has_audio"] = False
    if export_dir := os.environ.get("KRIA_Q3_EXPORT_DIR"):
        source = Path(export_dir) / "clip-1.mp4"
        original = row["upload_contract"]["proxy"]["original"]
        original.update(
            sha256=hashlib.sha256(source.read_bytes()).hexdigest(), byte_count=source.stat().st_size
        )
    with sync_session() as db:
        thread = db.get(CreationThread, thread_id)
        item = db.get(PlanItem, thread.active_plan_item_id)
        item.edit_format = "montage"
        item.clip_gcs_paths = [row["gcs_path"]]
        item.clip_assignments = [row]
        thread.state = {
            "edit_format": "montage",
            "reply_language": "en",
            "media_count": 1,
            "media": [{"media_id": row["media_id"]}],
        }
        db.commit()
    return user_id, thread_id


async def approve(chat):
    with sync_session() as db:
        approval = db.execute(
            select(CreatorAgentApproval).where(
                CreatorAgentApproval.thread_id == chat.thread_id,
                CreatorAgentApproval.status == "pending",
            )
        ).scalar_one()
        aid = approval.id
        body = ApprovalDecisionBody(
            expected_thread_revision=db.get(CreationThread, chat.thread_id).revision,
            expected_draft_revision=approval.draft_revision,
            expected_approval_fingerprint=approval_fingerprint(approval),
        )
    async with AsyncSessionLocal() as db:
        await decide_approval(
            db,
            thread_id=chat.thread_id,
            approval_id=aid,
            creator_id=chat.user_id,
            decision="approve",
            body=body,
        )
    claim = await asyncio.to_thread(_claim_approval_dispatch, aid)
    assert claim is not None
    return claim


class ComposerTransport:
    def __init__(self, *, unmet=False):
        self.prompts = []
        self.unmet = unmet

    def invoke(self, *, prompt, **kwargs):
        self.prompts.append(prompt)
        if self.unmet or TAP in prompt:
            return ModelInvocation(
                raw_text=json.dumps(
                    {
                        "intent": "clarify",
                        "confidence": 1,
                        "reply": "What does these refer to?",
                        "ops": [],
                        "needs_clarification": True,
                        "unmet_requests": [
                            {"request": TAP, "reason": "It is unclear what 'these' refers to"}
                        ],
                    }
                )
            )
        return ModelInvocation(
            raw_text=json.dumps(
                {
                    "intent": "edit",
                    "confidence": 1,
                    "reply": "Text placed in the middle",
                    "ops": [
                        {
                            "op": "patch_text_style",
                            "bar_index": 0,
                            "patch": {
                                "y_frac": 0.5,
                                "font_family": "TikTok Sans",
                                "size_px": 42,
                                "color": "#FFFFFF",
                            },
                        }
                    ],
                }
            )
        )


@pytest.mark.asyncio
@pytest.mark.parametrize("tap_option", ["simpler_version", None], ids=["button", "typed-alias"])
async def test_q3_hold_choice_approval_composition_has_no_button_instruction(
    monkeypatch, tap_option
):
    chat = Chat(*seed(), _wire(monkeypatch))
    held = await chat.send(PROMPT, answers(UPDATES))
    assert "choice_question" in held.payload, (held.content, held.payload)
    question = held.payload["choice_question"]
    assert question["omitted_requirement_ids"] == ["r3"]
    draft = await chat.send(TAP, answers(), option=tap_option)
    assert "Left out as agreed" in draft.content
    assert chat.readiness()["ready"] is True
    claim = await approve(chat)
    binding = BriefBinding.model_validate(claim.brief_binding)
    assert {r.id for r in binding.brief.live()} == {"r1", "r2", "r4", "r5"}
    assert TAP not in claim.creator_request
    assert TEXT in claim.creator_request
    assert "Superseded requirements" in claim.creator_request
    # The UI still echoes the exact label; only the machine instruction changes.
    with sync_session() as db:
        tap = (
            db.execute(
                select(CreationThreadEvent)
                .where(
                    CreationThreadEvent.thread_id == chat.thread_id,
                    CreationThreadEvent.role == "user",
                )
                .order_by(CreationThreadEvent.sequence.desc())
            )
            .scalars()
            .first()
        )
        assert (
            tap.content == TAP
            and tap.payload["choice_selection"]["option_key"] == "simpler_version"
        )
    # Continue with the approved strategy through the real planner and composer.
    planned = preflight_montage_plan(
        creator_id=chat.user_id,
        edit_format="montage",
        brief=binding.brief,
        strategy=claim.strategy,
        media_snapshot=binding.media_snapshot,
        visuals=[],
        client_protocol=44,
    )
    assert isinstance(planned, tuple)
    snapshot = planned[0].snapshot
    provider = ComposerTransport()
    monkeypatch.setattr("app.services.creation_text_composition.default_client", lambda: provider)
    output = compose_creation_text(
        snapshot,
        creator_request=claim.creator_request,
        text_requirements=binding.brief.live(),
        ctx=RunContext(extra={"skip_agent_run_persist": True}),
    )
    plan = compile_proposal_execution_plan(output)
    assert provider.prompts and all(TAP not in p for p in provider.prompts)
    assert len(plan["text_elements"]) == 1
    assert plan["text_elements"][0]["text"] == TEXT
    assert plan["text_elements"][0]["y_frac"] == 0.5
    sources = bind_phone_sources(
        binding.media_snapshot["clip_assignments"], binding.media_snapshot["clip_paths"]
    )
    recipe = compile_phone_guided_plan(GuidedStoryExecutionPlan.model_validate(plan), sources)
    assert len(recipe.text_layers) == 1
    if export_dir := os.environ.get("KRIA_Q3_EXPORT_DIR"):
        root = Path(export_dir)
        for name, value in (
            ("approved", recipe),
            ("missing_text", recipe.model_copy(update={"text_layers": []})),
        ):
            case = root / "cases" / name
            case.mkdir(parents=True, exist_ok=True)
            (case / "recipe.json").write_text(value.model_dump_json(indent=2))
            paths = {
                asset.id: str(
                    Path(__file__).resolve().parents[2]
                    / "assets/fonts"
                    / asset.id.removeprefix("font-")
                    if asset.id.startswith("font-")
                    else root / "clip-1.mp4"
                )
                for asset in value.asset_manifest.assets
            }
            (case / "assets.json").write_text(json.dumps(paths, indent=2))
        (root / "approval.json").write_text(
            json.dumps(
                {
                    "creator_request": claim.creator_request,
                    "binding_digest": binding.digest,
                    "live_requirements": [r.id for r in binding.brief.live()],
                    "planned_duration_s": output.duration_s,
                    "text": plan["text_elements"],
                    "evidence": (
                        "Real planner/runtime/approval/composer/compiler; "
                        "authored provider responses; synthetic source."
                    ),
                },
                indent=2,
            )
        )
    # A genuine unmet text request is never swallowed: since KRI-1376 the approved video renders
    # what the composer could do and the drop carries the note that says the rest could not.
    provider.unmet = True
    drops = []
    unmet = compose_creation_text(
        snapshot,
        creator_request=claim.creator_request,
        text_requirements=binding.brief.live(),
        ctx=RunContext(extra={"skip_agent_run_persist": True}),
        drops=drops,
    )
    assert [row["text"] for row in compile_proposal_execution_plan(unmet)["text_elements"]] == [
        TEXT
    ]
    assert [(drop.reason, drop.action) for drop in drops] == [("incomplete", "shrunk")]


@pytest.mark.parametrize("language,label", [("en", TAP), ("tr", "Bunlar olmadan hazırla")])
def test_structured_choice_is_not_an_ambiguous_model_instruction(language, label):
    result = model_text(SELECTION, label, language=language)
    assert label not in result
    assert result


@pytest.mark.parametrize(
    "message", [TAP, f'Use the exact words "{TAP}" as the title.', "Remove only the blue title."]
)
def test_typed_text_is_not_filtered_by_label(message):
    assert model_text({}, message) == message
    assert model_text({"choice_selection": {}}, message) == message
