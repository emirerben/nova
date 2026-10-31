"""KRI-1076: "Skip the photo of the empty train seat" found no photo, and nothing showed why.

Prod thread 5dda77f9 (Leverkusen kit M1 run 1, 2026-10-30): 8 iPhone videos + 5 photos. The
only seat photo (IMG_1001) is recorded as "Train seat and window view", yet both planning
passes logged ``kria_clip_exclusions_contested ... suspected=0 unmatched=1``: the resolver put
no clip forward, and the creator picked the photo out of a 12-clip picker with nothing
pre-ticked. Two things made it worse:

* The clip-intent planner and resolver ran with no plan item on their run context, so their
  ``agent_run`` rows were never written. Their input (what the resolver read) and output (what
  it answered) are gone, and why it missed cannot be shown.
* The skip's picker offered the 7 videos too. Every other picker row keeps only clips of the
  intent's ``source_kind`` (KRI-1052 readers); the skip's row ignored it.

Telling a "photo of" ask apart is the planner model's job (``source_kind``, KRI-1052); the
server never reads the creator's words for it (language policy, DECISIONS 2026-10-21).

Ways this could go wrong, written down before the code:

a. The planner's and the resolver's runs are still not saved for the plan item.
b. A run is saved, but its input does not show what the resolver read (the photo records),
   so a miss still cannot be explained.
c. A photo-only skip still offers the videos, or pre-ticks a video the resolver suspected.
d. A skip with no source kind loses clips from its picker (it must offer every unpinned clip,
   as before), or a video-only skip offers photos.
e. The picker's clip cap is applied before the kind filter, so photos after that many videos
   are never offered.
f. A suspect of the right kind is no longer pre-ticked.
"""

from __future__ import annotations

import copy
import json
import uuid
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock

import pytest
from sqlalchemy import select, text
from sqlalchemy.engine import make_url
from sqlalchemy.exc import OperationalError

from app.agents._runtime import ModelInvocation
from app.agents._schemas.creator_agent import CreativeStrategy
from app.agents._skills import load_skill
from app.agents.main_creator import MainCreatorAgent, MainCreatorInput
from app.config import settings
from app.database import sync_session
from app.kria import planner
from app.kria.clip_exclusions import exclusion_question, exclusions_to_ask
from app.models import AgentRun, CreatorAgentSession
from app.pipeline.prompt_loader import load_prompt
from app.schemas.clip_intents import ResolvedClipIntent
from app.services import clip_intent_planning as planning
from app.services import clip_intent_resolution as resolution
from app.services.clip_intent_resolution import IntentClip
from app.services.clip_selection import MAX_QUESTION_CLIPS
from app.services.creator_capabilities import resolve_creator_manifest

_db_name = make_url(settings.database_url).database or ""
if not _db_name.endswith("_test"):
    pytest.skip(f"refusing to write to non-test database {_db_name!r}", allow_module_level=True)
try:
    with sync_session() as _probe:
        _probe.execute(text("select 1"))
except OperationalError:
    pytest.skip("nova_test Postgres not reachable", allow_module_level=True)

from tests.kria.test_runtime_postgres_integration import _seed_runtime_project  # noqa: E402

FIXTURES = Path(__file__).resolve().parents[1] / "fixtures"
CASE: dict[str, Any] = json.loads((FIXTURES / "kri1076_leverkusen_m1_clips.json").read_text())
# The KRI-623 cabin turn supplies the rest of a real Main Creator input.
CABIN: dict[str, Any] = json.loads((FIXTURES / "kri623_tigre_m2_montage_turn.json").read_text())
BY_NAME = {c["file_name"]: c["media_id"] for c in CASE["clips"]}
ALL = [c["media_id"] for c in CASE["clips"]]
PHOTOS = [c["media_id"] for c in CASE["clips"] if c["kind"] == "image"]
VIDEOS = [c["media_id"] for c in CASE["clips"] if c["kind"] == "video"]
SEAT = BY_NAME["IMG_1001"]
TRAIN_VIDEO = BY_NAME["IMG_1002"]
INTENT_ID = "exclude_empty_seat_photo"
ATTRIBUTE = "the photo of the empty train seat"
SKIP = "Skip the photo of the empty train seat"
_PLANNER_PROMPT = load_prompt("clip_intent_planner")[:60]
_RESOLVER_PROMPT = (
    load_skill("meaningful-moment") + "\n\n" + load_prompt("clip_request_resolver")[:60]
)


@pytest.fixture(autouse=True)
def _flags(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(settings, "clip_intents_enabled", True)
    monkeypatch.setattr(settings, "clip_intent_exclude_enabled", True)
    monkeypatch.setattr(settings, "kria_clip_selection_questions_enabled", True)


def test_the_fixture_is_the_prod_turn() -> None:
    seat = next(c for c in CASE["clips"] if c["media_id"] == SEAT)
    assert seat["analysis"]["subject"] == "Train seat and window view"
    assert (len(VIDEOS), len(PHOTOS)) == (8, 5)
    asked = CASE["prod_clip_question"]
    assert (asked["intent_id"], asked["label"]) == (INTENT_ID, ATTRIBUTE)
    assert asked["suggested_media_ids"] == []
    # Prod offered 7 videos (the train clip is placed by the strategy) next to the 5 photos.
    assert sum(m in VIDEOS for m in asked["candidate_media_ids"]) == 7


# -- the picker row of a skip follows its source kind (c, d, e, f) --------------------------


def _skip(source_kind: str | None) -> ResolvedClipIntent:
    return ResolvedClipIntent(
        intent_id=INTENT_ID,
        op="exclude",
        attribute=ATTRIBUTE,
        **({"source_kind": source_kind} if source_kind else {}),
        status="resolved",
    )


def _row(source_kind: str | None, *, suspects: list[str], clips: list[str]) -> dict[str, Any]:
    kinds = {c["media_id"]: c["kind"] for c in CASE["clips"]}
    asked = exclusions_to_ask(
        [_skip(source_kind)],
        CreativeStrategy.model_validate({"edit_format": "montage"}),
        suspects={INTENT_ID: suspects},
        clip_count=len(clips),
    )
    _text, question = exclusion_question(
        asked, [SimpleNamespace(media_id=m, kind=kinds.get(m, "video")) for m in clips]
    )
    [row] = question["categories"]
    return row


def test_a_photo_skip_offers_only_photos_and_never_ticks_a_video() -> None:
    row = _row("image", suspects=[TRAIN_VIDEO, SEAT], clips=ALL)
    assert row["candidate_media_ids"] == PHOTOS  # c
    assert row["suggested_media_ids"] == [SEAT]  # c, f


def test_a_video_skip_offers_only_videos() -> None:
    row = _row("video", suspects=[SEAT], clips=ALL)
    assert row["candidate_media_ids"] == VIDEOS  # d
    assert row["suggested_media_ids"] == []


def test_a_skip_without_a_kind_offers_every_clip_as_before() -> None:
    row = _row(None, suspects=[SEAT], clips=ALL)
    assert row["candidate_media_ids"] == ALL  # d
    assert row["suggested_media_ids"] == [SEAT]  # f


def test_photos_after_a_full_picker_of_videos_are_still_offered() -> None:
    # e: a full picker of videos ahead of the photos; the cap counts only the skip's kind.
    extra = [f"analysis-proxy-ios-extra-{n:02d}.mp4" for n in range(MAX_QUESTION_CLIPS)]
    row = _row("image", suspects=[SEAT], clips=[*extra, *ALL])
    assert row["candidate_media_ids"] == PHOTOS
    assert row["suggested_media_ids"] == [SEAT]


# -- the turn, through the real planner and resolver, saves both runs (a, b) ----------------


class _Provider:
    """The model provider, answering as it did on turn a8da158a.

    Only the transport is a stand-in: the real clip-intent planner and resolver agents render
    their prompts, call it, and parse its raw text. The resolver puts no clip forward for the
    skip (prod: ``suspected=0 unmatched=1``).
    """

    def __init__(self, *, source_kind: str | None = None) -> None:
        self.source_kind = source_kind
        self.calls: list[str] = []

    def invoke(self, *, prompt: str, **_kwargs: Any) -> ModelInvocation:
        if prompt.startswith(_PLANNER_PROMPT):
            self.calls.append("planner")
            skip = {
                "intent_id": INTENT_ID,
                "op": "exclude",
                "attribute": ATTRIBUTE,
                "source_quote": SKIP,
                **({"source_kind": self.source_kind} if self.source_kind else {}),
            }
            return ModelInvocation(raw_text=json.dumps({"intents": [skip], "question": None}))
        if prompt.startswith(_RESOLVER_PROMPT):
            self.calls.append("resolver")
            empty = {"intent_id": INTENT_ID, "assignments": [], "needs_vision": []}
            return ModelInvocation(raw_text=json.dumps({"intents": [empty]}))
        self.calls.append("unexpected")
        raise AssertionError(f"unexpected model call: {prompt[:80]!r}")


def _intent_clips() -> list[IntentClip]:
    return [
        IntentClip(
            media_id=c["media_id"],
            kind=c["kind"],
            analysis=c["analysis"],
            gcs_path=f"users/u/clips/{c['media_id']}",
            file_name=c["file_name"],
            capture_time=datetime.fromisoformat(c["capture_time"].replace("Z", "+00:00")),
        )
        for c in CASE["clips"]
    ]


def _agent_input(item_id: uuid.UUID) -> MainCreatorInput:
    data = copy.deepcopy(CABIN["main_creator_input"])
    message = CASE["creator_request"]
    data["user_message"] = message
    data["creator_request"] = message
    data["conversation"] = [data["conversation"][0], {"role": "user", "content": message}]
    data["capability_manifest"] = resolve_creator_manifest(
        item_id=str(item_id),
        edit_format="montage",
        media=[
            {"media_id": c["media_id"], "kind": c["kind"], "duration_s": c.get("duration_s") or 3}
            for c in CASE["clips"]
        ],
        guided_capability_enabled=True,
    ).model_dump(mode="json")
    return MainCreatorInput.model_validate(data)


async def _turn(monkeypatch: pytest.MonkeyPatch, provider: _Provider) -> tuple[Any, uuid.UUID]:
    monkeypatch.setattr(planning, "default_client", lambda: provider)
    monkeypatch.setattr(resolution, "default_client", lambda: provider)
    user_id, thread_id, session_id = _seed_runtime_project()
    with sync_session() as db:
        item_id = db.get(CreatorAgentSession, session_id).plan_item_id
    agent_input = _agent_input(item_id)
    raw = {
        "action": {
            "kind": "propose_strategy",
            "strategy": {
                "edit_format": "montage",
                "media_scope": "all",
                "target_duration_s": 25,
                "rationale": "The match night in the order it was filmed.",
            },
            "summary": "Here's a 25-second montage of your night at the BayArena.",
        },
        "brief_updates": [],
    }
    output = MainCreatorAgent(model_client=None).parse(json.dumps(raw), agent_input)
    monkeypatch.setattr(planner, "_load_thread_events", AsyncMock(return_value=[]))
    monkeypatch.setattr(planner, "_kick_clip_understanding", AsyncMock())
    monkeypatch.setattr(planner, "_earlier_exclusions", AsyncMock(return_value=[]))
    monkeypatch.setattr(type(planner.settings), "brief_binding_for", lambda _s, _i: True)
    planned = await planner._plan_from_creator_output(
        SimpleNamespace(),
        thread_id=thread_id,
        item_id=item_id,
        creator_id=user_id,
        user_message=CASE["creator_request"],
        manifest=agent_input.capability_manifest,
        inputs=planner._CreatorInputs(
            agent_input=agent_input,
            intent_clips=_intent_clips(),
            creator_request=None,
            raw_creator_request=CASE["creator_request"],
        ),
        output=output,
        brief_selects_clips=planner._brief_selects_clips(None),
        clip_scope_texts=planner._brief_clip_scope_texts(None),
    )
    return planned, item_id


def _saved_runs(item_id: uuid.UUID) -> list[AgentRun]:
    with sync_session() as db:
        return list(
            db.execute(
                select(AgentRun).where(AgentRun.plan_item_id == item_id).order_by(AgentRun.id)
            ).scalars()
        )


async def test_the_missed_skip_is_saved_with_what_the_resolver_read(
    prod_profile, monkeypatch
) -> None:
    provider = _Provider()
    planned, item_id = await _turn(monkeypatch, provider)

    # The prod outcome is unchanged: one picker row, every clip offered, nothing pre-ticked.
    assert provider.calls == ["planner", "resolver", "resolver"]  # 13 clips = two batches
    assert (planned.plan.mode, planned.plan.turn_value) == ("respond", "question")
    [row] = planned.plan.clip_question["categories"]
    assert (row["op"], row["intent_id"]) == ("exclude", INTENT_ID)
    assert row["candidate_media_ids"] == ALL
    assert row["suggested_media_ids"] == []

    # a: both agents' runs are saved for the plan item.
    runs = _saved_runs(item_id)
    names = [r.agent_name for r in runs]
    assert names.count("nova.plan.clip_intent_planner") == 1
    assert names.count("nova.plan.clip_request_resolver") == 2
    # b: the resolver's saved input shows the seat photo's record and the skip it was asked,
    # and its saved output shows it put no clip forward.
    resolver_runs = [r for r in runs if r.agent_name == "nova.plan.clip_request_resolver"]
    seat_batch = next(
        r for r in resolver_runs if "Train seat and window view" in json.dumps(r.input_json)
    )
    [intent] = seat_batch.input_json["intents"]
    assert (intent["intent_id"], intent["attribute"]) == (INTENT_ID, ATTRIBUTE)
    assert seat_batch.output_json["intents"][0]["assignments"] == []


async def test_a_photo_only_skip_from_the_planner_offers_only_photos(
    prod_profile, monkeypatch
) -> None:
    # c: once the planner marks the skip photo-only (KRI-1052), the picker shows the 5 photos.
    planned, _item_id = await _turn(monkeypatch, _Provider(source_kind="image"))
    [row] = planned.plan.clip_question["categories"]
    assert row["candidate_media_ids"] == PHOTOS
    assert row["suggested_media_ids"] == []
