"""KRI-881 T06: the creator sees the sourced wording and its citations BEFORE approving,
and the render puts exactly that approved wording on exactly those clips.

T06 (prod, KRI-816): "Let's do The Historical Journey as a researched explanation video.
... Keep original camera audio, with no music. Include sources for the historical facts."
On the phone montage path the search ran only after approval, inside the render's text
lane. The approval card said nothing about research, and a research failure arrived as a
failed render.

This drives the real runtime turn (``run_kria_turn`` over scratch Postgres, the real server
gate) and the real phone montage worker up to its pin. Only the model I/O is recorded: the
grounded search response (provider-owned citations, filtered by the real institution
check), the copy writer and the reviewer, each answered by a recorded client through the
real agents. Nothing calls a network.

Failure modes written before the fix:
F1  The approval card goes out before any sourced wording exists (today's behaviour).
F2  The reply shows wording but not its sources, or sources the provider never returned.
F3  Research that finds no institutional source still reaches a ready approval card,
    instead of a precise limit and a choice before approval.
F5/F6 The pinned research is not the one the creator saw (the draft changed in between).
F7  The render plan no longer fits the approved windows, and the render silently drops or
    re-researches the text instead of failing with the reason.
F9  The render searches again (cost + different wording) although research was approved.
F11 Sourced text becomes caption cues / speech instead of on-screen text.
F14 Kill switch off changes anything: no research call, no pinned field, approval as before.
F15 Research outlasts the 15 s turn lease (planning's heartbeat has stopped), so the draft
    commit finds the turn lost and writes no reply and no approval card.
F16 A turn deferred to the background task re-plans with different wording, so its cache
    key never matches the task's result: it defers again until the run cap, paying for
    research every time, and the creator ends on "send your message again".
F17 A continued run researches inline while the background task for the same draft is
    still running (search, copy and review paid twice).
F18 A transient timeout in the background task is cached as a final failure, so every
    later run (and the creator's resend) shows "sources could not be confirmed" for an hour.
F19 A newer instruction ("keep each explanation under 6 words") reuses wording researched
    under the old one, because the cache key ignores the full request and requirements.
F20 A Visual added after research is cached, F21 clip analysis that moves a slot window:
    the cached wording, bound to the old windows, is pinned and the job fails after
    approval. F22 The draft commit checks the result against the planning step's Visuals
    instead of its own.
(Lyric caption rows under an approved explanation: test_creation_composition.py.)

Artifact: ``.dev/kri-881/report.json`` (approval-time wording and sources, render-time text
elements, model call counts and a verdict per arm).
"""

from __future__ import annotations

import asyncio
import contextlib
import copy
import json
import threading
import time
import uuid
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace

import pytest
import pytest_asyncio
from sqlalchemy import select

from app.agents._runtime import ModelClient, ModelInvocation
from app.config import settings
from app.database import engine as async_engine
from app.database import sync_session
from app.kria.brief import BriefUpdate, CreativeBrief, apply_updates
from app.kria.brief_binding import BriefBinding
from app.kria.drafts import KriaDraftDocument
from app.kria.media_sources import OriginalMediaDescriptor
from app.models import (
    CreationThread,
    CreationThreadEvent,
    CreatorAgentApproval,
    CreatorEditDraft,
    PlanItem,
)
from app.services import creation_text_composition as text_lane
from app.services import researched_text
from app.services.phone_sources import PHONE_SOURCES_FIELD, PhoneSourceBinding
from app.tasks import generative_build as gb
from app.tasks.kria_runtime import run_kria_turn
from tests.kria.test_kri775_merope_m2_replay import FIXTURE as MEROPE
from tests.kria.test_kri775_merope_m2_replay import prod_flags, run_gate

# Skips this module (like every runtime Postgres journey) when nova_test is unreachable.
from tests.kria.test_runtime_postgres_integration import (
    _seed_runtime_project,
    _submit,
)

T06 = json.loads(
    (
        Path(__file__).resolve().parents[2]
        / "fixtures"
        / "kri816_troya_historical_journey_turn.json"
    ).read_text()
)
MESSAGE = T06["user_message"]
QUOTE = "Include sources for the historical facts"
REPORT = Path(__file__).resolve().parents[6] / ".dev" / "kri-881" / "report.json"
_REPORT: dict[str, dict] = {}

# Four T06-shaped Troya clips (the fixture kept durations and ids, not analysis).
CLIPS = [
    (
        "t06-horse",
        6.0,
        "the wooden horse",
        "A large wooden horse replica at the Troy site entrance",
    ),
    ("t06-car", 5.0, "a car interior", "Driving through the countryside inside a car"),
    ("t06-ruins", 9.0, "stone ruins", "Excavated stone walls and ruins of ancient Troy"),
    (
        "t06-museum",
        9.0,
        "museum galleries",
        "Display cases with pottery in the Troy Museum galleries",
    ),
]

UNESCO = "https://whc.unesco.org/en/list/849/"
MUSEUM = "https://muze.ktb.gov.tr/troya"
BLOG = "https://www.troy-travel-blog.com/history"


def _assignment(media_id: str, duration: float, subject: str, summary: str) -> dict:
    template = copy.deepcopy(MEROPE["renders"][0]["media_snapshot"]["clip_assignments"][0])
    original = template["upload_contract"]["proxy"]["original"]
    original.update(duration_s=duration, sha256=uuid.uuid5(uuid.NAMESPACE_URL, media_id).hex * 2)
    template["upload_contract"]["proxy"]["duration_s"] = duration
    template.update(
        media_id=media_id,
        manifest_identity=media_id,
        gcs_path=f"users/test/creation-threads/t06/analysis-proxy-ios-{media_id}.mp4",
        duration_s=duration,
        source_filename=f"{media_id}.mov",
        library_asset_id=str(uuid.uuid5(uuid.NAMESPACE_URL, media_id)),
        storage_generation="1",
        analysis={
            "subject": subject,
            "description": summary,
            "understanding": {"summary": summary},
            "on_screen_text": "",
            "best_moments": [],
            "transcript": "",
        },
    )
    return template


def _render() -> dict:
    rows = [_assignment(*clip) for clip in CLIPS]
    strategy = copy.deepcopy(MEROPE["renders"][0]["strategy"])
    strategy.update(
        rationale="A researched walk through Troy: arrival, the drive, the ruins, the museum.",
        clip_intents=[],
        resolved_clip_intents=[],
        opening_title="The Historical Journey",
        story_structure=["Arrival at the horse", "The drive", "The ruins", "The museum"],
        target_duration_s=25,
        selected_media_ids=[row["media_id"] for row in rows],
        audio_strategy="original_audio",
    )
    return {
        "strategy": strategy,
        "draft_reply": "A researched walk through Troy with sourced on-screen explanations.",
        "media_snapshot": {
            "clip_paths": [row["gcs_path"] for row in rows],
            "clip_assignments": rows,
            "voiceover_path": None,
            "song_path": None,
        },
    }


UPDATES = (
    BriefUpdate(
        kind="audio",
        scope="global",
        description="Keep original camera audio, with no music",
        facts={"audio_intent": "source_unchanged"},
    ),
    BriefUpdate(
        kind="text",
        scope="global",
        description="Short on-screen explanations of the historical facts, with sources",
        facts={
            "research": {
                "topic": "History of the ancient city of Troy and the Troy Museum collection",
                "request_quote": QUOTE,
            }
        },
    ),
)


# -- recorded model I/O ---------------------------------------------------------------------


def _grounding(sources: list[tuple[str, str, str]]) -> SimpleNamespace:
    """A provider response: each (fact text, url, title) supported by its own chunk."""
    return SimpleNamespace(
        candidates=[
            SimpleNamespace(
                grounding_metadata=SimpleNamespace(
                    grounding_chunks=[
                        SimpleNamespace(web=SimpleNamespace(uri=url, title=title))
                        for _text, url, title in sources
                    ],
                    grounding_supports=[
                        SimpleNamespace(
                            segment=SimpleNamespace(text=text), grounding_chunk_indices=[index]
                        )
                        for index, (text, _url, _title) in enumerate(sources)
                    ],
                    search_entry_point=SimpleNamespace(
                        rendered_content="<div class='search'>Troy history</div>"
                    ),
                )
            )
        ]
    )


INSTITUTIONAL = [
    (
        "Troy was settled successively from the Bronze Age to Roman times.",
        UNESCO,
        "Archaeological Site of Troy",
    ),
    (
        "The Troy Museum shows finds from Troy and the wider Troad region.",
        MUSEUM,
        "Troy Museum",
    ),
]
BLOG_ONLY = [("Troy is over 5,000 years old.", BLOG, "Troy travel blog")]


class _Recorded(ModelClient):
    """Answers each research agent from the recorded outputs and counts every call.

    ``search_delay_s`` makes the search as slow as a real grounded call; ``search_failures``
    makes the first N searches end with an unknown provider outcome (a timeout).
    """

    def __init__(
        self,
        calls: dict[str, int],
        grounding: list[tuple[str, str, str]],
        *,
        search_delay_s: float = 0.0,
        search_failures: int = 0,
        copy_unmet: list[str] | None = None,
        review_demands_style: bool = False,
        copy_truncations: int = 0,
        review_rejections: int = 0,
    ) -> None:
        self.calls = calls
        self.grounding = grounding
        self.search_delay_s = search_delay_s
        self.search_failures = search_failures
        self.copy_unmet = copy_unmet or []
        self.review_demands_style = review_demands_style
        self.copy_truncations = copy_truncations
        self.review_rejections = review_rejections
        self.corrections: list[str] = []

    def invoke(self, *, model: str, prompt: str, **_: object) -> ModelInvocation:  # noqa: ARG002
        if prompt.startswith("Research this public topic"):
            self.calls["search"] += 1
            if self.search_delay_s:
                time.sleep(self.search_delay_s)
            if self.calls["search"] <= self.search_failures:
                from app.agents._runtime import ProviderOutcomeUnknownError  # noqa: PLC0415

                raise ProviderOutcomeUnknownError("grounded search timed out")
            try:
                evidence = researched_text.evidence_from_response(_grounding(self.grounding))
                raw = evidence.model_dump_json()
            except researched_text.ResearchUnavailable as exc:
                raw = json.dumps({"unavailable": str(exc)})
            return ModelInvocation(raw_text=raw)
        if prompt.startswith("Write SHORT researched"):
            self.calls["copy"] += 1
            if self.calls["copy"] <= self.copy_truncations:
                # The model spent its whole output budget thinking (finish_reason MAX_TOKENS).
                return ModelInvocation(raw_text="", finish_status="truncated")
            data = json.loads(prompt.split("Data:\n", 1)[1])
            self.corrections.append(data.get("correction", ""))
            windows = data["snapshot"]["research_windows"]
            captions = []
            for keyword, label, text, fact, quote in (
                ("ruins", "Troy", "Settled from the Bronze Age to Roman times", "fact-0", "ruins"),
                ("Museum", "Troy Museum", "Finds from Troy and the Troad", "fact-1", "Troy Museum"),
            ):
                window = next(
                    row
                    for row in windows
                    if len(row["media_ids"]) == 1 and keyword in json.dumps(row["seen"])
                )
                captions.append(
                    {
                        "text": text,
                        "window_id": window["id"],
                        "media_ids": [],
                        "evidence_ids": [fact],
                        "visual_quote": quote,
                        "scope": "context",
                        "context_label": label,
                        "subject_name": "",
                        "identity_quote": "",
                    }
                )
            return ModelInvocation(
                raw_text=json.dumps({"captions": captions, "unmet_requests": self.copy_unmet})
            )
        if prompt.startswith("Review proposed on-screen captions"):
            self.calls["review"] += 1
            context = json.loads(prompt.split("Data:\n", 1)[1])["context"]
            if self.calls["review"] <= self.review_rejections:
                return ModelInvocation(
                    raw_text=json.dumps(
                        {"approved": False, "reason": "Two explanations overlap the same footage."}
                    )
                )
            if (
                self.review_demands_style
                and "animation" in context["request"].lower()
                and "animation" not in context.get("handled_elsewhere", [])
            ):
                # Prod 2026-11-01 07:05: the reviewer held the request's animation ask against
                # research the moment the data did not say another step applies it.
                return ModelInvocation(
                    raw_text=json.dumps(
                        {
                            "approved": False,
                            "reason": "The request explicitly asked to include animations, "
                            "but there are no animations requested or provided in the output.",
                        }
                    )
                )
            return ModelInvocation(
                raw_text=json.dumps({"approved": True, "reason": "Each claim is cited."})
            )
        if prompt.startswith("CREATION TEXT COMPOSITION"):
            # The render's composer asks for research again: it must be answered from the pin.
            self.calls["composer"] += 1
            return ModelInvocation(
                raw_text=json.dumps(
                    {
                        "intent": "describe",
                        "confidence": 0.9,
                        "reply": "Research is needed.",
                        "ops": [],
                        "research_request": {
                            "topic": "History of Troy",
                            "request_quote": QUOTE,
                        },
                    }
                )
            )
        raise AssertionError("unexpected model call: " + prompt[:80])


_LAST_CLIENT: dict[str, _Recorded] = {}


def _record(monkeypatch, grounding, **behaviour) -> dict[str, int]:  # noqa: ANN001, ANN003
    calls = {"search": 0, "copy": 0, "review": 0, "composer": 0}
    client = _Recorded(calls, grounding, **behaviour)
    monkeypatch.setattr(researched_text, "GroundedGeminiClient", lambda: client)
    monkeypatch.setattr(researched_text, "default_client", lambda: client)
    monkeypatch.setattr(text_lane, "default_client", lambda: client)
    _LAST_CLIENT["client"] = client
    return calls


# -- the turn -------------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def _flags(monkeypatch: pytest.MonkeyPatch) -> None:
    prod_flags(monkeypatch)
    monkeypatch.setattr(settings, "kria_runtime_v2_enabled", True)
    monkeypatch.setattr(settings, "main_creator_agent_enabled", True)
    monkeypatch.setattr(settings, "kria_creative_brief_user_ids", [])
    monkeypatch.setattr(settings, "kria_brief_binding_user_ids", [])
    # The result cache, in-flight markers and turn anchors are Redis: each arm gets its own
    # empty in-memory one, so the real cache code runs and every arm researches for itself.
    # (Before KRI-881 there is no research preview at all, and the arms fail on their
    # assertions.)
    with contextlib.suppress(ImportError):
        from app.kria import research_preview  # noqa: PLC0415

        fake = _FakeRedis()
        monkeypatch.setattr(research_preview, "_redis", lambda: fake)


class _FakeRedis:
    """The few Redis calls the research cache makes, in memory (thread-safe enough)."""

    def __init__(self) -> None:
        self.data: dict[str, bytes] = {}

    def get(self, key: str) -> bytes | None:
        return self.data.get(key)

    def set(self, key: str, value: str, ex: int | None = None, nx: bool = False) -> bool | None:  # noqa: ARG002
        if nx and key in self.data:
            return None
        self.data[key] = value.encode() if isinstance(value, str) else value
        return True

    def exists(self, key: str) -> int:
        return int(key in self.data)

    def delete(self, key: str) -> int:
        return int(self.data.pop(key, None) is not None)


@pytest_asyncio.fixture(autouse=True)
async def _dispose_async_engine():
    yield
    await async_engine.dispose()


@pytest.fixture(scope="module", autouse=True)
def _write_report():
    yield
    REPORT.parent.mkdir(parents=True, exist_ok=True)
    REPORT.write_text(json.dumps(_REPORT, indent=2, ensure_ascii=False, default=str))


def _seed(render: dict) -> tuple[uuid.UUID, uuid.UUID]:
    user_id, thread_id, _session = _seed_runtime_project()
    media = render["media_snapshot"]
    with sync_session() as db:
        thread = db.get(CreationThread, thread_id)
        item = db.get(PlanItem, thread.active_plan_item_id)
        item.edit_format = "montage"
        item.clip_gcs_paths = list(media["clip_paths"])
        item.clip_assignments = copy.deepcopy(media["clip_assignments"])
        thread.state = {
            "media": [{"media_id": row["media_id"]} for row in media["clip_assignments"]],
            "media_count": len(media["clip_assignments"]),
            "edit_format": "montage",
        }
        db.commit()
    return user_id, thread_id


def _reworded(planned, run: int):  # noqa: ANN001, ANN202
    """The same draft as the Main Creator re-writes it on another run: new wording."""
    intent = planned.plan.intents[0]
    arguments = copy.deepcopy(intent.arguments)
    arguments["strategy"]["rationale"] = f"Run {run}: a walk through Troy, told again."
    arguments["summary"] = f"Run {run}: The Historical Journey"
    plan = planned.plan.model_copy(
        update={
            "intents": [
                intent.model_copy(update={"arguments": arguments}),
                *planned.plan.intents[1:],
            ]
        }
    )
    return replace(planned, plan=plan)


async def _turn(  # noqa: ANN202
    monkeypatch,  # noqa: ANN001
    *,
    reword_each_run: bool = False,
    animated: bool = False,
    updates: tuple[BriefUpdate, ...] | None = None,
    message: str | None = None,
    through_readiness: bool = False,
):
    render = _render()
    user_id, thread_id = _seed(render)
    updates = updates if updates is not None else UPDATES
    message = message if message is not None else MESSAGE
    if animated:
        # "... With animations.": the brief carries an animation fact (the animation_ask lane).
        message = MESSAGE + " With animations."
        updates = (
            *UPDATES,
            BriefUpdate(
                kind="text",
                scope="global",
                description="animated on-screen text",
                facts={"animation": "any"},
            ),
        )
    stamped = apply_updates(None, updates, source_turn_id=None, creator_text=message)
    brief = CreativeBrief(version=len(stamped.requirements), requirements=stamped.requirements)
    gated = await run_gate(monkeypatch, render, brief=brief)
    runs: list[int] = []

    async def _planned(*_args, **_kwargs):  # noqa: ANN002, ANN003, ANN202
        runs.append(len(runs) + 1)
        planned = replace(
            gated,
            brief_updates=updates,
            brief_route="replan",
            brief_clip_ids=tuple(
                row["media_id"] for row in render["media_snapshot"]["clip_assignments"]
            ),
        )
        return _reworded(planned, len(runs)) if reword_each_run else planned

    # Stub one level lower for the readiness arms: `_plan_with_live_agent` then still resolves
    # the draft's renderer manifest, the input of the approval-readiness gate.
    monkeypatch.setattr(
        "app.tasks.kria_runtime.plan_live_turn"
        if through_readiness
        else "app.tasks.kria_runtime._plan_with_live_agent",
        _planned,
    )
    accepted = await _submit(user_id, thread_id, message, 2)
    result = await asyncio.to_thread(run_kria_turn.run, accepted.turn_id)
    return result, accepted, thread_id


def _reply(thread_id: uuid.UUID) -> CreationThreadEvent:
    with sync_session() as db:
        return (
            db.execute(
                select(CreationThreadEvent)
                .where(
                    CreationThreadEvent.thread_id == thread_id,
                    CreationThreadEvent.event_type.in_(["draft_applied", "assistant_response"]),
                )
                .order_by(CreationThreadEvent.sequence.desc())
            )
            .scalars()
            .first()
        )


def _approved_binding(turn_id: str) -> tuple[list[CreatorAgentApproval], BriefBinding | None]:
    with sync_session() as db:
        approvals = list(
            db.execute(
                select(CreatorAgentApproval).where(
                    CreatorAgentApproval.turn_id == uuid.UUID(turn_id)
                )
            ).scalars()
        )
        if not approvals:
            return [], None
        draft = db.get(CreatorEditDraft, approvals[0].draft_id)
        document = KriaDraftDocument.model_validate(draft.snapshot_json)
        return approvals, document.brief_binding


# -- the render -----------------------------------------------------------------------------


def _render_worker(monkeypatch, binding: BriefBinding, strategy: dict):  # noqa: ANN001, ANN202
    """``_run_phone_unified_montage_job`` on the approved binding, up to the job pin.

    Database reads/writes are fixed values; the composer, search, copy and review run
    through their real agents with recorded answers.
    """
    import app.services.clip_facts as clip_facts  # noqa: PLC0415

    rows = [dict(row) for row in binding.media_snapshot["clip_assignments"]]
    sources = [
        PhoneSourceBinding(
            media_id=row["media_id"],
            proxy_path=row["gcs_path"],
            generation=str(row["storage_generation"]),
            original=OriginalMediaDescriptor.model_validate(
                row["upload_contract"]["proxy"]["original"]
            ),
        )
        for row in rows
    ]
    snapshot = {
        PHONE_SOURCES_FIELD: [source.model_dump(mode="json") for source in sources],
        "creator_generation_id": "kri881-generation",
        "creator_brief_binding": binding.model_dump(mode="json"),
        "creator_reply_language": "en",
    }
    candidates = {
        "clip_paths": [source.proxy_path for source in sources],
        "edit_format": "montage",
        "creator_strategy": copy.deepcopy(strategy),
        "creator_request": binding.creator_request,
    }
    seen: dict = {}
    real_compose = text_lane.compose_creation_text

    def compose(plan_snapshot, **kwargs):  # noqa: ANN001, ANN003, ANN202
        seen["composed"] = real_compose(plan_snapshot, **kwargs)
        return seen["composed"]

    def save(_job_id, _snapshot, *, ownership_epoch, message, receipts=None):  # noqa: ANN001, ANN202, ARG001
        seen["refusal"] = message
        return True

    monkeypatch.setattr(
        gb, "_load_unified_montage_inputs", lambda _job: (uuid.uuid4(), rows, binding.resolve())
    )
    monkeypatch.setattr(gb, "_load_unified_montage_visuals", lambda *_a, **_k: [])
    monkeypatch.setattr(clip_facts, "enrich_clip_facts", lambda pairs, **_k: list(pairs))
    monkeypatch.setattr(text_lane, "compose_creation_text", compose)
    monkeypatch.setattr(gb, "_save_request_recovery", save)
    monkeypatch.setattr(gb, "_sync_session", contextlib.nullcontext)
    monkeypatch.setattr(gb, "_lock_owned_entry_job", lambda *_a: None)
    gb._run_phone_unified_montage_job(str(uuid.uuid4()), snapshot, candidates, ownership_epoch=1)
    return seen


def _rendered_texts(composed) -> list[dict]:  # noqa: ANN001
    from app.pipeline.guided_story import compile_proposal_execution_plan  # noqa: PLC0415

    return compile_proposal_execution_plan(composed)["text_elements"]


# -- arms -----------------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_t06_sourced_wording_is_shown_before_approval_and_rendered_as_approved(
    monkeypatch,
) -> None:
    calls = _record(monkeypatch, INSTITUTIONAL)
    result, accepted, thread_id = await _turn(monkeypatch)

    # F1: research ran before the card, once.
    assert result["status"] == "awaiting_approval", result
    assert (calls["search"], calls["copy"], calls["review"]) == (1, 1, 1)
    approvals, binding = _approved_binding(accepted.turn_id)
    assert len(approvals) == 1
    reply = _reply(thread_id)
    content = str(reply.content)
    # F2: the exact wording AND its provider-owned sources are in the reply, before approval.
    assert "Sourced on-screen explanations" in content
    assert "“Troy: Settled from the Bronze Age to Roman times”" in content
    assert "“Troy Museum: Finds from Troy and the Troad”" in content
    assert "whc.unesco.org" in content and "muze.ktb.gov.tr" in content
    preview = reply.payload["research_preview"]
    assert [row["text"] for row in preview["explanations"]] == [
        "Troy: Settled from the Bronze Age to Roman times",
        "Troy Museum: Finds from Troy and the Troad",
    ]
    assert {source["url"] for row in preview["explanations"] for source in row["sources"]} == {
        UNESCO,
        MUSEUM,
    }
    # F5/F10: what was shown is what approval pinned, under the binding's digest.
    assert binding is not None and binding.prepared_research is not None
    pinned = binding.prepared_research
    assert pinned["status"] == "ready"
    assert [row["text"] for row in pinned["texts"]] == [
        row["text"] for row in preview["explanations"]
    ]
    rebuilt = BriefBinding.model_validate(binding.model_dump(mode="json"))
    assert rebuilt.digest == binding.digest

    # The render: the approved wording on the approved clips, no new search (F9).
    strategy = KriaDraftDocument.model_validate(_draft_snapshot(approvals[0].draft_id)).strategy
    before = dict(calls)
    seen = _render_worker(monkeypatch, binding, strategy)
    assert "refusal" not in seen, seen.get("refusal")
    assert (calls["search"], calls["copy"], calls["review"]) == (
        before["search"],
        before["copy"],
        before["review"],
    )
    texts = _rendered_texts(seen["composed"])
    sourced = [row for row in texts if (row.get("source_params") or {}).get("research")]
    assert [(row["text"], row["start_s"], row["end_s"]) for row in sourced] == [
        (row["text"], row["start_s"], row["end_s"]) for row in pinned["texts"]
    ]
    for row, approved in zip(sourced, pinned["texts"], strict=True):
        research = row["source_params"]["research"]
        assert research["media_ids"] == approved["media_ids"]
        assert research["sources"] == approved["sources"]
        assert research["search_entry_html"]
    # F11: on-screen text only, never a caption cue or speech.
    from app.services.kria_editor_ops import is_caption_text_bar  # noqa: PLC0415

    assert not any(is_caption_text_bar(row) for row in sourced)
    assert all(row.get("role") != "caption" for row in sourced)
    # The title the creator approved is still the opening text.
    assert any(row["text"] == "The Historical Journey" for row in texts)

    _REPORT["ready"] = {
        "verdict": "pass",
        "approval_reply": content,
        "approval_sources": sorted(
            {source["url"] for row in preview["explanations"] for source in row["sources"]}
        ),
        "render_text_elements": [
            {"text": row["text"], "start_s": row["start_s"], "end_s": row["end_s"]} for row in texts
        ],
        "model_calls": calls,
    }


def _draft_snapshot(draft_id: uuid.UUID) -> dict:
    with sync_session() as db:
        return dict(db.get(CreatorEditDraft, draft_id).snapshot_json)


@pytest.mark.asyncio
async def test_an_ask_the_writer_cannot_source_never_holds_back_a_sourced_draft(
    monkeypatch,
) -> None:
    """Prod 2026-11-01, "With animations, explain why people should visit Assos, Turkey":
    the copy writer listed "animations" under unmet_requests next to four sourced captions,
    and the whole draft was held back with "I can't make this yet". A part of the ask the
    research step cannot do is disclosed; it never blocks the sourced wording."""
    calls = _record(monkeypatch, INSTITUTIONAL, copy_unmet=["animations"])
    result, accepted, thread_id = await _turn(monkeypatch)

    assert result["status"] == "awaiting_approval", result
    assert (calls["search"], calls["copy"], calls["review"]) == (1, 1, 1)
    approvals, binding = _approved_binding(accepted.turn_id)
    assert len(approvals) == 1
    content = str(_reply(thread_id).content)
    assert "Sourced on-screen explanations" in content
    assert "Sources could not be confirmed" not in content
    assert binding is not None and binding.prepared_research is not None
    assert binding.prepared_research["status"] == "ready"
    assert len(binding.prepared_research["texts"]) == 2


@pytest.mark.asyncio
async def test_the_reviewer_is_told_which_asks_another_step_applies(monkeypatch) -> None:
    """Prod 2026-11-01 07:05, the same Assos prompt after the unmet-list fix: the claim reviewer
    rejected the sourced captions with "The request explicitly asked to include animations".
    The research agents read the whole request, so they must be told the animation ask is
    applied elsewhere, as data, not left to the model's reading of the prompt."""
    calls = _record(monkeypatch, INSTITUTIONAL, review_demands_style=True)
    result, accepted, thread_id = await _turn(monkeypatch, animated=True)

    assert result["status"] == "awaiting_approval", result
    assert (calls["search"], calls["copy"], calls["review"]) == (1, 1, 1)
    approvals, binding = _approved_binding(accepted.turn_id)
    assert len(approvals) == 1
    content = str(_reply(thread_id).content)
    assert "Sources could not be confirmed" not in content
    assert binding is not None and binding.prepared_research is not None
    assert binding.prepared_research["status"] == "ready"


ASSOS_LIVE = json.loads(
    (
        Path(__file__).resolve().parents[2] / "fixtures" / "kria_replay" / "assos" / "live_r1.json"
    ).read_text()
)


def _assos_live_updates(**extra_facts: object) -> tuple[BriefUpdate, ...]:
    """r1 exactly as the live Main Creator wrote it for the Assos prompt (one requirement
    with the research fact AND the animation fact), optionally with more look facts."""
    recorded = ASSOS_LIVE["brief_update"]
    return (
        BriefUpdate(
            kind=recorded["kind"],
            scope=recorded["scope"],
            literal=recorded["literal"],
            description=recorded["description"],
            facts={**recorded["facts"], **extra_facts},
        ),
    )


def _readiness_rows(approval_turn_id: str) -> list[dict]:
    approvals, _binding = _approved_binding(approval_turn_id)
    document = KriaDraftDocument.model_validate(_draft_snapshot(approvals[0].draft_id))
    return list(((document.brief_coverage or {}).get("approval_readiness") or {})["requirements"])


@pytest.fixture
def readiness_on(monkeypatch: pytest.MonkeyPatch) -> None:
    """The approval-readiness gate is ON in prod (code default off): without it this file
    never exercised the gate that held the Assos draft three times."""
    monkeypatch.setattr(settings, "kria_approval_readiness_enabled", True)
    from tests._prod_profile import PROD_VERIFIED_FEATURES  # noqa: PLC0415

    monkeypatch.setattr(settings, "phone_render_verified_features", list(PROD_VERIFIED_FEATURES))


@pytest.mark.asyncio
async def test_a_researched_ask_with_animations_is_approved_when_its_research_is_proven(
    monkeypatch, readiness_on
) -> None:
    """Prod 2026-11-01 07:54, third refusal of "With animations, explain why people should
    visit Assos, Turkey": "I can't make this yet: ... I can't confirm this draft does it
    yet". The live Main Creator writes ONE requirement carrying the research fact and the
    animation fact (`live_r1.json`, recorded). Research passed and its sourced wording was
    ready, but the readiness gate saw a research requirement whose receipt was not checked
    (the animation is judged on the finished video) and held the draft. The animation part
    is named as judged after render; the sourced wording is still shown before approval."""
    assert ASSOS_LIVE["observed_hold"]["readiness_row"]["disposition"] == "evidence_missing"
    calls = _record(monkeypatch, INSTITUTIONAL)
    result, accepted, thread_id = await _turn(
        monkeypatch,
        updates=_assos_live_updates(),
        message=ASSOS_LIVE["message"],
        through_readiness=True,
    )

    assert result["status"] == "awaiting_approval", result
    assert (calls["search"], calls["copy"], calls["review"]) == (1, 1, 1)
    content = str(_reply(thread_id).content)
    assert "I can't make this yet" not in content
    assert "Sourced on-screen explanations" in content
    assert "whc.unesco.org" in content and "muze.ktb.gov.tr" in content
    assert "the text will animate in as it appears" in content
    rows = _readiness_rows(accepted.turn_id)
    assert [(row["requirement_id"], row["disposition"]) for row in rows] == [("r1", "unverified")]
    _approvals, binding = _approved_binding(accepted.turn_id)
    assert binding is not None and binding.prepared_research is not None
    assert binding.prepared_research["status"] == "ready"


@pytest.mark.asyncio
async def test_kill_switch_off_holds_the_researched_animation_ask_as_before(
    monkeypatch, readiness_on
) -> None:
    monkeypatch.setattr(settings, "kria_researched_look_after_render_enabled", False)
    _record(monkeypatch, INSTITUTIONAL)
    result, accepted, thread_id = await _turn(
        monkeypatch,
        updates=_assos_live_updates(),
        message=ASSOS_LIVE["message"],
        through_readiness=True,
    )

    assert result["status"] == "completed", result
    assert _approved_binding(accepted.turn_id)[0] == []
    assert str(_reply(thread_id).content) == ASSOS_LIVE["observed_hold"]["reply"]


@pytest.mark.asyncio
async def test_unproven_research_still_holds_a_researched_animation_ask(
    monkeypatch, readiness_on
) -> None:
    """The fix never weakens research: with no institutional source there is no proof, so the
    precise limit and the choice come first, animation ask or not."""
    _record(monkeypatch, BLOG_ONLY)
    result, accepted, thread_id = await _turn(
        monkeypatch,
        updates=_assos_live_updates(),
        message=ASSOS_LIVE["message"],
        through_readiness=True,
    )

    assert result["status"] == "completed", result
    assert _approved_binding(accepted.turn_id)[0] == []
    content = str(_reply(thread_id).content)
    assert "Sources could not be confirmed before approval" in content
    assert "Sourced on-screen explanations" not in content


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "look",
    [
        {"animation": "fade"},
        {"position": "top_right"},
        {"font_family": "Playfair Display"},
        {"text_color": "#ff0000"},
        {"animation": "any", "position": "top_right", "text_color": "#ff0000"},
    ],
    ids=["named-animation", "position", "font", "colour", "animation-position-colour"],
)
async def test_research_with_any_look_fact_is_never_held_once_the_research_is_proven(
    monkeypatch, readiness_on, look: dict
) -> None:
    """The same class as Assos: a research fact beside a look fact on one requirement. Whatever
    the look is, proven research plus a look only the finished video shows is approvable."""
    recorded = ASSOS_LIVE["brief_update"]["facts"]
    update = _assos_live_updates()[0]
    update = update.model_copy(update={"facts": {"research": recorded["research"], **look}})
    calls = _record(monkeypatch, INSTITUTIONAL)
    result, accepted, thread_id = await _turn(
        monkeypatch,
        updates=(update,),
        message=ASSOS_LIVE["message"],
        through_readiness=True,
    )

    assert result["status"] == "awaiting_approval", (look, result)
    assert calls["search"] == 1
    content = str(_reply(thread_id).content)
    assert "I can't make this yet" not in content
    assert "Sourced on-screen explanations" in content


@pytest.mark.asyncio
async def test_a_writer_that_runs_out_of_output_budget_is_retried_not_refused(monkeypatch) -> None:
    """Live local run on main 2026-11-01 (the Assos prompt): the copy writer thought past its
    output budget (finish_reason MAX_TOKENS), had no retry, and the preview reported "no usable
    cited facts from institutional sources" and held the draft back. A truncation is the
    model's budget, not a fact about the sources: the runtime's retry at low thinking runs."""
    calls = _record(monkeypatch, INSTITUTIONAL, copy_truncations=1)
    result, accepted, thread_id = await _turn(monkeypatch)

    assert result["status"] == "awaiting_approval", result
    assert calls["copy"] == 2  # the truncated call, then the retry
    approvals, binding = _approved_binding(accepted.turn_id)
    assert len(approvals) == 1
    assert "no usable cited facts" not in str(_reply(thread_id).content)
    assert binding is not None and binding.prepared_research["status"] == "ready"


@pytest.mark.asyncio
async def test_a_rejected_draft_is_corrected_once_before_the_draft_is_held_back(
    monkeypatch,
) -> None:
    """Live local run on main 2026-11-01: "Research explanations overlap" / the reviewer asking
    for a correction ended the turn in "I can't make this yet" on the first miss, though the
    writer was never told what to fix. One correction round, with the reason, before giving up."""
    calls = _record(monkeypatch, INSTITUTIONAL, review_rejections=1)
    result, accepted, thread_id = await _turn(monkeypatch)

    assert result["status"] == "awaiting_approval", result
    assert (calls["copy"], calls["review"]) == (2, 2)
    corrections = _LAST_CLIENT["client"].corrections
    assert corrections[0] == "" and "overlap" in corrections[1]
    approvals, binding = _approved_binding(accepted.turn_id)
    assert len(approvals) == 1
    assert binding is not None and binding.prepared_research["status"] == "ready"


def test_a_truncated_model_answer_is_never_filed_as_a_source_limit() -> None:
    """A truncation cannot be cached for an hour as "no institutional source": it says nothing
    about the sources. A malformed answer still is that limit."""
    from app.agents._runtime import (
        SchemaError,
        TerminalOutputTruncatedError,
        TerminalSchemaError,
        _OutputTruncatedError,
    )
    from app.kria import research_preview

    truncated = TerminalOutputTruncatedError("writer: output truncated")
    truncated.__cause__ = _OutputTruncatedError("model truncated output")
    assert research_preview._schema_failure(truncated) is None
    malformed = TerminalSchemaError("writer: schema")
    malformed.__cause__ = SchemaError("not well formed")
    assert research_preview._schema_failure(malformed) is not None


@pytest.mark.asyncio
async def test_no_institutional_source_is_a_precise_limit_before_approval(monkeypatch) -> None:
    calls = _record(monkeypatch, BLOG_ONLY)
    result, accepted, thread_id = await _turn(monkeypatch)

    # F3: no ready card; the reason and the choice come first.
    assert result["status"] == "completed", result
    assert _approved_binding(accepted.turn_id)[0] == []
    reply = _reply(thread_id)
    content = str(reply.content)
    assert "Sources could not be confirmed before approval" in content
    assert "institutional" in content
    assert content.endswith(
        "Should I make it without these, or do you want to change your request?"
    )
    question = (reply.payload or {}).get("choice_question") or {}
    assert {option["key"] for option in question.get("options", [])} >= {"simpler_version"}
    assert (calls["copy"], calls["review"]) == (0, 0)
    _REPORT["no_institutional_source"] = {"verdict": "pass", "reply": content, "calls": calls}


@pytest.mark.asyncio
async def test_a_render_plan_that_no_longer_fits_fails_with_the_reason(monkeypatch) -> None:
    calls = _record(monkeypatch, INSTITUTIONAL)
    _result, accepted, _thread_id = await _turn(monkeypatch)
    approvals, binding = _approved_binding(accepted.turn_id)
    strategy = KriaDraftDocument.model_validate(_draft_snapshot(approvals[0].draft_id)).strategy
    # F7: the approved museum explanation now names a clip the render plan does not have.
    drifted = copy.deepcopy(binding.prepared_research)
    drifted["texts"][1]["media_ids"] = ["t06-elsewhere"]
    tampered = BriefBinding.create(
        binding.thread_id,
        binding.brief,
        full_creator_request=binding.creator_request,
        media_snapshot=binding.media_snapshot,
        prepared_research=drifted,
    )
    before = dict(calls)
    with pytest.raises(text_lane.CreationTextCompositionError) as failed:
        _render_worker(monkeypatch, tampered, strategy)
    assert "approved sourced explanation 2" in str(failed.value)
    assert "no longer matches its approved clips" in str(failed.value)
    assert calls["search"] == before["search"]  # never re-searched
    _REPORT["window_drift"] = {"verdict": "pass", "error": str(failed.value)}


@pytest.mark.asyncio
async def test_kill_switch_off_changes_nothing(monkeypatch) -> None:
    monkeypatch.setattr(settings, "kria_research_preview_enabled", False)
    calls = _record(monkeypatch, INSTITUTIONAL)
    result, accepted, thread_id = await _turn(monkeypatch)

    # F14: the approval card goes out as before, with no research and no pinned field.
    assert result["status"] == "awaiting_approval", result
    assert (calls["search"], calls["copy"], calls["review"]) == (0, 0, 0)
    approvals, binding = _approved_binding(accepted.turn_id)
    assert len(approvals) == 1
    assert binding is not None and binding.prepared_research is None
    assert "prepared_research" not in binding.model_dump(mode="json")
    assert "research_preview" not in (_reply(thread_id).payload or {})
    _REPORT["kill_switch_off"] = {"verdict": "pass", "calls": calls}


def test_tampered_pin_fails_the_binding_digest() -> None:
    """F10: the pinned research is under the binding digest; an edited copy is refused."""
    binding = BriefBinding.create(
        uuid.uuid4(),
        CreativeBrief(version=1),
        media_snapshot={"clip_paths": []},
        prepared_research={"status": "ready", "texts": [{"text": "A"}]},
    )
    raw = binding.model_dump(mode="json")
    raw["prepared_research"]["texts"][0]["text"] = "B"
    with pytest.raises(ValueError, match="digest"):
        BriefBinding.model_validate(raw)


def test_t06_main_creator_research_update_survives_parse_and_is_authorized() -> None:
    """F1/F2: the typed requirement the prompt now teaches reaches the brief intact, and its
    quote is the creator's own words (a forged quote or a URL topic is dropped)."""
    from app.agents.main_creator import MainCreatorAgent, MainCreatorInput  # noqa: PLC0415
    from app.kria.research_preview import research_requirement  # noqa: PLC0415

    data = {key: value for key, value in T06.items() if key != "_source"}
    inp = MainCreatorInput.model_validate(data)
    ids = [media.media_id for media in inp.capability_manifest.media]
    research = {
        "kind": "text",
        "scope": "global",
        "description": "Sourced on-screen explanations of the historical facts",
        "facts": {
            "research": {"topic": "History of Troy and the Troy Museum", "request_quote": QUOTE}
        },
    }
    raw = {
        "brief_updates": [
            {
                "kind": "audio",
                "scope": "global",
                "description": "Keep original camera audio, with no music",
                "facts": {"audio_intent": "source_unchanged"},
            },
            research,
        ],
        "action": {
            "kind": "propose_strategy",
            "summary": "The Historical Journey",
            "strategy": {
                "rationale": "A researched walk through Troy.",
                "target_duration_s": 30,
                "selected_media_ids": ids[:12],
                "edit_format": "montage",
                "audio_strategy": "original_audio",
            },
        },
    }
    output = MainCreatorAgent.__new__(MainCreatorAgent).parse(json.dumps(raw), inp)
    brief = apply_updates(None, output.brief_updates, source_turn_id=None, creator_text=MESSAGE)
    found = research_requirement(brief)
    assert found is not None
    _req, request = found
    request.authorize(MESSAGE)  # the quote is the creator's own words

    forged = copy.deepcopy(research)
    forged["facts"]["research"]["topic"] = "https://example.org/private/troy"
    brief = apply_updates(
        None,
        [BriefUpdate.model_validate(forged)],
        source_turn_id=None,
        creator_text=MESSAGE,
    )
    assert research_requirement(brief) is None
    from app.services.researched_text import ResearchRequest, ResearchUnavailable  # noqa: PLC0415

    with pytest.raises(ResearchUnavailable):
        ResearchRequest(topic="Troy", request_quote="please invent facts").authorize(MESSAGE)


# -- budget arms: the lease, the background task and its cache ------------------------------


def _defer_to_background(monkeypatch) -> list[dict]:  # noqa: ANN001
    """Run 1 has no budget left for inline research; capture the task instead of a broker."""
    from app.kria import research_preview  # noqa: PLC0415
    from app.tasks import kria_runtime  # noqa: PLC0415

    tasks: list[dict] = []
    monkeypatch.setattr(
        kria_runtime.prepare_kria_research, "apply_async", lambda **kw: tasks.append(kw)
    )
    monkeypatch.setattr(run_kria_turn, "apply_async", lambda **_kw: None)
    monkeypatch.setattr(research_preview, "INLINE_MIN_S", 10_000.0)
    monkeypatch.setattr(research_preview, "_WAIT_POLL_S", 0.1)
    return tasks


@pytest.mark.asyncio
async def test_research_longer_than_the_turn_lease_still_reaches_the_approval_card(
    monkeypatch,
) -> None:
    """F15: recorded models answer instantly, so the lease never lapsed in the other arms.
    Here the search takes longer than the whole lease."""
    from app.tasks import kria_runtime  # noqa: PLC0415

    monkeypatch.setattr(settings, "kria_turn_lease_seconds", 2)
    monkeypatch.setattr(kria_runtime, "_LEASE_HEARTBEAT_SECONDS", 0.5)
    calls = _record(monkeypatch, INSTITUTIONAL, search_delay_s=4.0)
    result, accepted, thread_id = await _turn(monkeypatch)

    assert result["status"] == "awaiting_approval", result
    assert (calls["search"], calls["copy"], calls["review"]) == (1, 1, 1)
    approvals, binding = _approved_binding(accepted.turn_id)
    assert len(approvals) == 1 and binding.prepared_research["status"] == "ready"
    assert "Sourced on-screen explanations" in str(_reply(thread_id).content)
    _REPORT["lease_outlasted"] = {"verdict": "pass", "calls": calls}


@pytest.mark.asyncio
async def test_a_deferred_turn_drafts_exactly_what_the_background_task_researched(
    monkeypatch,
) -> None:
    """F16/F17: the continued run re-plans with new wording, starts while the task is still
    researching, and has budget to research inline. It drafts the researched strategy and
    waits for the task: one search, one copy, one review, one approval card."""
    from app.kria import research_preview  # noqa: PLC0415

    calls = _record(monkeypatch, INSTITUTIONAL)
    tasks = _defer_to_background(monkeypatch)
    first, accepted, thread_id = await _turn(monkeypatch, reword_each_run=True)

    assert first["status"] == "continued", first
    assert len(tasks) == 1 and sum(calls.values()) == 0
    assert _approved_binding(accepted.turn_id)[0] == []

    monkeypatch.setattr(research_preview, "INLINE_MIN_S", 100.0)
    outcome: list[str] = []

    def _background() -> None:
        time.sleep(0.5)
        outcome.append(research_preview.run_task_body(tasks[0]["args"][0]))

    worker = threading.Thread(target=_background)
    worker.start()
    second = await asyncio.to_thread(run_kria_turn.run, accepted.turn_id)
    worker.join()

    assert outcome == ["ready"]
    assert second["status"] == "awaiting_approval", second
    assert (calls["search"], calls["copy"], calls["review"]) == (1, 1, 1)
    assert len(tasks) == 1  # never enqueued twice
    approvals, binding = _approved_binding(accepted.turn_id)
    assert len(approvals) == 1
    strategy = KriaDraftDocument.model_validate(_draft_snapshot(approvals[0].draft_id)).strategy
    # The draft the sources were checked for, not the re-plan's new wording.
    assert strategy["rationale"].startswith("Run 1:")
    assert binding.prepared_research["status"] == "ready"
    assert "Sourced on-screen explanations" in str(_reply(thread_id).content)
    _REPORT["deferred_then_continued"] = {"verdict": "pass", "calls": calls}


@pytest.mark.asyncio
async def test_a_background_timeout_is_not_cached_and_the_next_run_researches_again(
    monkeypatch,
) -> None:
    """F18: a slow provider once is not a final answer for the next hour."""
    from app.kria import research_preview  # noqa: PLC0415

    calls = _record(monkeypatch, INSTITUTIONAL, search_failures=1)
    tasks = _defer_to_background(monkeypatch)
    first, accepted, thread_id = await _turn(monkeypatch)
    assert first["status"] == "continued", first

    assert research_preview.run_task_body(tasks[0]["args"][0]) == "timed_out"
    assert calls["search"] == 1

    monkeypatch.setattr(research_preview, "INLINE_MIN_S", 100.0)
    second = await asyncio.to_thread(run_kria_turn.run, accepted.turn_id)

    assert second["status"] == "awaiting_approval", second
    assert (calls["search"], calls["copy"], calls["review"]) == (2, 1, 1)
    content = str(_reply(thread_id).content)
    assert "could not be confirmed" not in content
    assert "Sourced on-screen explanations" in content
    _REPORT["timeout_not_cached"] = {"verdict": "pass", "calls": calls}


@pytest.mark.asyncio
async def test_a_newer_instruction_is_researched_again_not_served_from_the_cache(
    monkeypatch,
) -> None:
    """F19: the same draft under a newer instruction is a different research basis."""
    from app.kria import research_preview  # noqa: PLC0415

    calls = _record(monkeypatch, INSTITUTIONAL)
    tasks = _defer_to_background(monkeypatch)
    first, _accepted, _thread_id = await _turn(monkeypatch)
    assert first["status"] == "continued", first
    payload = tasks[0]["args"][0]
    assert research_preview.run_task_body(payload) == "ready"
    assert research_preview.run_task_body(payload) == "cached"
    assert calls["search"] == 1

    newer = copy.deepcopy(payload)
    newer["creator_request"] = f"{payload['creator_request']}\nKeep each explanation under 6 words."
    brief = CreativeBrief.model_validate(newer["brief"])
    newer["brief"] = apply_updates(
        brief,
        [
            BriefUpdate(
                kind="text",
                scope="global",
                description="Keep each explanation under 6 words",
            )
        ],
        source_turn_id=None,
        creator_text=newer["creator_request"],
    ).model_dump(mode="json")

    assert research_preview.run_task_body(newer) == "ready"
    assert calls["search"] == 2
    _REPORT["newer_instruction"] = {"verdict": "pass", "calls": calls}


# -- F20/F21: the cache basis covers what research read beyond the draft ---------------------
#
# F20 A Visual added (or re-analysed) after research is cached: the cached wording, bound to
#     the old slot windows, is pinned to an approval whose render plans other windows, and
#     the job fails after approval.
# F21 Clip analysis that moves a slot window (a stronger best moment) does the same: the
#     media identity ignores analysis, so the old basis still matched.
# F22 The planning step researched one set of Visuals and the draft commit sees another:
#     ``for_draft`` must recompute the basis from the commit's own Visuals, not echo the
#     planning step's.


def _visual(media_id: str = "t06-map") -> object:
    from app.pipeline.unified_montage import UnifiedClip  # noqa: PLC0415

    return UnifiedClip(
        media_id=media_id,
        proxy_path=f"users/test/visuals/{media_id}.jpg",
        generation="1",
        duration_s=0.0,
        analysis={"subject": "a map of the Troad", "description": "A map of ancient Troy"},
        lane="asset",
        kind="image",
        manifest_id=f"asset-{media_id}",
        aspect=0.5625,
        source_filename=f"{media_id}.jpg",
        user_context="",
        content_hash=None,
    )


async def _researched_payload(monkeypatch) -> tuple[dict, dict[str, int]]:  # noqa: ANN001
    from app.kria import research_preview  # noqa: PLC0415

    calls = _record(monkeypatch, INSTITUTIONAL)
    tasks = _defer_to_background(monkeypatch)
    first, _accepted, _thread_id = await _turn(monkeypatch)
    assert first["status"] == "continued", first
    payload = tasks[0]["args"][0]
    assert research_preview.run_task_body(payload) == "ready"
    assert research_preview.run_task_body(payload) == "cached"
    assert calls["search"] == 1
    return payload, calls


@pytest.mark.asyncio
async def test_a_visual_added_after_research_is_researched_again(monkeypatch) -> None:
    """F20."""
    from app.kria import research_preview  # noqa: PLC0415

    payload, calls = await _researched_payload(monkeypatch)
    monkeypatch.setattr(research_preview, "visuals_for_task", lambda _inputs: [_visual()])

    assert research_preview.run_task_body(payload) == "ready"
    assert calls["search"] == 2
    _REPORT["visual_changed"] = {"verdict": "pass", "calls": calls}


def _moved_window(payload: dict, *, best_moment_s: float) -> dict:
    """``payload`` after clip analysis found the ruins' strongest moment at ``best_moment_s``."""
    out = copy.deepcopy(payload)
    ruins = next(
        row for row in out["media_snapshot"]["clip_assignments"] if row["media_id"] == "t06-ruins"
    )
    ruins["analysis"]["best_moments"] = [
        {
            "start_s": best_moment_s,
            "end_s": best_moment_s + 4.0,
            "energy": 9.0,
            "description": "The walls up close",
        }
    ]
    return out


@pytest.mark.asyncio
async def test_analysis_that_moves_a_window_is_researched_again(monkeypatch) -> None:
    """F21. A 40 s ruins take, so its cut is a window inside the clip, not the whole clip."""
    import sys  # noqa: PLC0415

    from app.kria import research_preview  # noqa: PLC0415

    module = sys.modules[__name__]
    monkeypatch.setattr(
        module,
        "CLIPS",
        [(*clip[:1], 40.0, *clip[2:]) if clip[0] == "t06-ruins" else clip for clip in CLIPS],
    )
    researched, calls = await _researched_payload(monkeypatch)
    # A draft whose media carries no length plan: the planner cuts each window from the
    # clip analysis itself (`unified_montage._window_start_s`), at plan time.
    payload = copy.deepcopy(researched)
    payload["media_snapshot"].pop("length_plan", None)
    moved = _moved_window(payload, best_moment_s=20.0)
    before = research_preview.ResearchInputs.from_payload(payload).with_visuals([])
    after = research_preview.ResearchInputs.from_payload(moved).with_visuals([])
    # The precondition: the analysis moved a planned window, and nothing the draft names.
    assert before.analysis["slots"] != after.analysis["slots"]
    assert before.media_snapshot["clip_paths"] == after.media_snapshot["clip_paths"]
    assert before.strategy == after.strategy

    assert research_preview.run_task_body(payload) == "ready"
    assert research_preview.run_task_body(payload) == "cached"
    assert calls["search"] == 2
    assert research_preview.run_task_body(moved) == "ready"
    assert calls["search"] == 3
    _REPORT["analysis_moved_window"] = {"verdict": "pass", "calls": calls}


@pytest.mark.asyncio
async def test_the_draft_commit_checks_research_against_its_own_visuals(monkeypatch) -> None:
    """F22."""
    from app.kria import research_preview  # noqa: PLC0415

    payload, _calls = await _researched_payload(monkeypatch)
    inputs = research_preview.ResearchInputs.from_payload(payload).with_visuals([])
    prepared = research_preview.cache_get(inputs.thread_id, inputs.basis)
    assert prepared is not None and prepared.status == "ready"

    def draft(visuals: list) -> research_preview.DraftResearch | None:  # noqa: ANN001
        return research_preview.for_draft(
            prepared.model_dump(mode="json"),
            brief=inputs.brief,
            strategy=inputs.strategy,
            media_snapshot=inputs.media_snapshot,
            terms=inputs.terms,
            creator_id=inputs.creator_id,
            edit_format=inputs.edit_format,
            visuals=lambda: visuals,
            client_protocol=inputs.client_protocol,
        )

    same = draft([])
    assert same is not None and same.ready
    changed = draft([_visual()])
    assert changed is not None and not changed.ready
    assert changed.prepared.reason == "The draft changed while its sources were checked."
    _REPORT["draft_commit_visuals"] = {"verdict": "pass"}
