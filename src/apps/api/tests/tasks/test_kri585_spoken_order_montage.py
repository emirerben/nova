"""KRI-585: a phone montage that keeps the camera sound follows what the clips say.

Incident (prod thread 7ccbde0a, job 136712e8, 2026-10-11, cabin kit M1): twelve house-tour
clips with the creator's Turkish narration; "start with the river, end on Deniz at his
laptop, four room labels, skip my selfie, fast cuts, 20 s". No song, so every clip's own
sound played. The kitchen clip that says "Evet arkadaşlar, Tigre'de ev turumuza hoş
geldiniz" (welcome to our home tour) played ninth, the closing Deniz clip played "Bu da
Deniz ... kanalımızın kurucu ekibinden" (this is Deniz, from our founding team), and the
reply never said the narration would be heard.

The clip data is the incident's own (``tests/fixtures/kri585_tigre_m1_clips``, ids renamed
to the kit's upload files). Everything runs the real gate (``_gate_unresolved_choices``,
the code ``plan_live_turn`` calls once the approved snapshot is attached), the real length
plan, the real montage planner, the phone compiler and the phone verifier; only the
database reads are fixed values.

Ways this can fail, each pinned below:
1. The welcome clip stays where the attachment order put it.
2. It moves, but its window misses the welcome: the analysed best moment (2.2-4.0 s)
   cuts "Tigre'de ev turumuza hoş geldiniz" (1.94-3.4 s) in half.
3. The spoken order outranks the creator's own "start with the river" / "Deniz last".
4. The closing Deniz window plays his introduction: the analysed best moment (3.3-4.9 s)
   is "kanalımızın kurucu ekibinden".
5. The length drifts off 20 s, the phone verifier refuses the recipe, or the order
   receipt stops being met.
6. The reply says nothing about the narration, speaks English in the Turkish chat, or
   claims an ending the approved windows don't do.
7. A filming-order ask, a song, a voiceover or the cloud route still gets reordered or
   noted.
8. The phone worker reorders clips the reply never mentioned: a voice mode, a named voice
   source, a guided voiceover, no ``spoken_order`` record on the approved plan, or a
   pinned contract order (KRI-129: no silent overrides).
9. The kill switch (``SPOKEN_ORDER_ENABLED=false``) leaves a reorder, a seat or a note.
10. Background chatter alone gets a "your narration plays" sentence.
11. A welcome said on several clips ("welcome to my kitchen", "welcome to the bedroom")
    moves one of them.
"""

from __future__ import annotations

import copy
import dataclasses
import json
import uuid
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from app.agents._schemas.creator_agent import (
    CapabilityAvailability,
    CreativeStrategy,
    ProposeStrategy,
)
from app.config import settings
from app.kria import planner
from app.kria.brief import BriefRequirement, CreativeBrief
from app.kria.brief_binding import BriefBinding
from app.kria.brief_checks import check_requirement, plan_facts_from_rendered_montage
from app.kria.media_sources import OriginalMediaDescriptor
from app.kria.narration_notes import speech_order_approved
from app.kria.planner import PlannedKriaTurn
from app.kria.reply_language import reply_language_for
from app.pipeline import unified_montage
from app.pipeline.guided_story import compile_execution_plan
from app.pipeline.unified_montage import brief_view, plan_unified_montage
from app.schemas.clip_intents import ResolvedClipIntent
from app.services.creator_render_contract import CONTRACT_FIELD, build_render_contract
from app.services.phone_sources import PHONE_SOURCES_FIELD, PhoneSourceBinding
from app.tasks import generative_build as gb
from tests.pipeline.test_unified_montage import clip
from tests.tasks.test_selected_clip_dispatch import _verify

_FIXTURE = Path(__file__).resolve().parents[1] / "fixtures" / "kri585_tigre_m1_clips.json"
DATA = json.loads(_FIXTURE.read_text(encoding="utf-8"))
CLIPS = DATA["clips"]
SELECTED = DATA["selected_media_ids"]
RIVER, WELCOME, DENIZ, SELFIE = "m1_02", "m1_03", "m1_04", "m1_07"
# "Tigre'de ev turumuza hoş geldiniz" in the welcome clip's timed segments.
WELCOME_LINE = (1.94, 3.4)
# Deniz's clip: "Bu da Deniz," 0-1.22 s, "kanalımızın kurucu ekibinden." 3.3-5.74 s.
DENIZ_WORDS = ((0.0, 1.22), (3.3, 5.74))
PHONE = SimpleNamespace(
    capabilities={"phone_source_audio": CapabilityAvailability(available=True)},
    has_voiceover=False,
)
CLOUD = SimpleNamespace(capabilities={}, has_voiceover=False)


@pytest.fixture(autouse=True)
def _clip_intents_on(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(settings, "clip_intents_enabled", True)


def _brief(*extra: BriefRequirement) -> CreativeBrief:
    return CreativeBrief(
        version=1,
        requirements=[BriefRequirement(**row) for row in DATA["brief"]] + list(extra),
    )


def _snapshot(*, dated: bool = False) -> dict:
    rows = []
    for index, c in enumerate(CLIPS):
        row = {
            "media_id": c["media_id"],
            "kind": "video",
            "gcs_path": f"users/u/analysis-proxy-{c['media_id']}.mp4",
            "storage_generation": "1",
            "duration_s": c["duration_s"],
            "analysis": c["analysis"],
        }
        if dated:
            # Filmed in upload order (M1_01 first), unlike the Photos-picker attachment order.
            row["capture"] = {"capture_time": f"2025-11-15T19:{10 + 12 - index:02d}:00Z"}
        rows.append(row)
    return {"clip_assignments": rows}


def _planned(manifest=PHONE, **strategy: object) -> PlannedKriaTurn:
    base: dict = {
        "direction": "fast_montage",
        "edit_format": "montage",
        "audio_strategy": "licensed_music",
        "pacing": "fast",
        "render_program": "guided",
        "rationale": "A fast montage.",
        "opening_title": "Tigre’de bir kulübe",
        "media_scope": "selected",
        "selected_media_ids": SELECTED,
        "target_duration_s": 20,
        "target_duration_requested": True,
        "video_reuse_policy": "once",
    }
    plan = planner.adapt_creator_action(
        ProposeStrategy(
            kind="propose_strategy",
            strategy=CreativeStrategy(**{**base, **strategy}),
            summary="Tigre kulübe turunu 20 saniyelik hızlı bir montaj olarak tasarlıyorum.",
        ),
        server_resolved_clip_intents=[
            ResolvedClipIntent.model_validate(row) for row in DATA["resolved_clip_intents"]
        ],
    )
    return PlannedKriaTurn(
        plan=plan, manifest_hash="a" * 64, context_hash="b" * 64, brief_manifest=manifest
    )


async def _gate(
    monkeypatch: pytest.MonkeyPatch,
    planned: PlannedKriaTurn,
    brief: CreativeBrief,
    *,
    dated: bool = False,
) -> PlannedKriaTurn:
    monkeypatch.setattr(planner, "load_latest_brief", AsyncMock(return_value=brief))
    monkeypatch.setattr(type(planner.settings), "creative_brief_for", lambda _s, _i: True)
    monkeypatch.setattr(type(planner.settings), "brief_binding_for", lambda _s, _i: True)
    monkeypatch.setattr(planner, "_load_thread_events", AsyncMock(return_value=[]))
    planned = replace(planned, media_snapshot=_snapshot(dated=dated))
    result = await planner._gate_unresolved_choices(
        SimpleNamespace(), planned, thread_id=uuid.uuid4(), creator_id=uuid.uuid4()
    )
    assert result.plan.mode == "act", result.plan.response
    return result


def _render(result: PlannedKriaTurn, brief: CreativeBrief, *, speech_order: bool = True):
    """The montage the phone worker plans from the approved draft (its own gate)."""
    strategy = result.plan.intents[0].arguments["strategy"]
    snapshot = result.media_snapshot
    resolved = BriefBinding.create(
        uuid.uuid5(uuid.NAMESPACE_URL, "kri-585"),
        brief,
        latest_message="Continue",
        media_snapshot=snapshot,
    ).resolve()
    clips = [
        dataclasses.replace(
            clip(i, duration=c["duration_s"]),
            media_id=c["media_id"],
            proxy_path=f"users/u/analysis-proxy-{c['media_id']}.mp4",
            analysis=c["analysis"],
            width=1080,
            height=1920,
        )
        for i, c in enumerate(row for row in CLIPS if row["media_id"] in SELECTED)
    ]
    contract = build_render_contract(
        strategy, generation_id="gen", brief=resolved, media_snapshot=snapshot
    )
    plan = plan_unified_montage(
        clips,
        brief_view(resolved),
        strategy=strategy,
        clip_intents_enabled=True,
        creator_duration_proven=True,
        approved_length_plan=snapshot["length_plan"],
        speech_order=speech_order
        and speech_order_approved(strategy, contract, snapshot["length_plan"]),
    )
    return plan, contract, clips


def _window(snapshot: dict, media_id: str) -> tuple[float, float]:
    rows = [w for w in snapshot["length_plan"]["windows"] if w["media_id"] == media_id]
    assert len(rows) == 1, rows
    return rows[0]["source_start_s"], rows[0]["source_end_s"]


def _overlaps(window: tuple[float, float], words: tuple[float, float]) -> bool:
    return window[0] < words[1] and window[1] > words[0]


def _summary(result: PlannedKriaTurn) -> str:
    return result.plan.intents[0].arguments["summary"]


# -- the incident, end to end ----------------------------------------------------------


async def test_incident_the_welcome_follows_the_river_and_deniz_closes_without_his_intro(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    brief = _brief()
    result = await _gate(monkeypatch, _planned(), brief)
    snapshot = result.media_snapshot
    length_plan = snapshot["length_plan"]
    assert not length_plan["conflicts"]
    assert length_plan["spoken_order"]["roles"] == {WELCOME: "opening", DENIZ: "introduction"}

    # 2: the welcome clip's approved window holds the whole welcome.
    start, end = _window(snapshot, WELCOME)
    assert start <= WELCOME_LINE[0] and end >= WELCOME_LINE[1]
    # 4: Deniz's approved window stays clear of both of his lines.
    deniz = _window(snapshot, DENIZ)
    assert not any(_overlaps(deniz, words) for words in DENIZ_WORDS)
    assert deniz[1] - deniz[0] >= 1.2 - 1e-6

    plan, contract, clips = _render(result, brief)
    # 1 + 3: river first (the creator's rule), the welcome right after it, Deniz last.
    assert plan.clip_ids[0] == RIVER
    assert plan.clip_ids[1] == WELCOME
    assert plan.clip_ids[-1] == DENIZ
    assert SELFIE not in plan.clip_ids
    cuts = {cut.media_id: cut for cut in plan.snapshot.fast_cuts}
    assert cuts[WELCOME].source_start_s <= WELCOME_LINE[0]
    assert cuts[WELCOME].source_end_s >= WELCOME_LINE[1]
    last = plan.snapshot.fast_cuts[-1]
    assert not any(
        _overlaps((last.source_start_s, last.source_end_s), words) for words in DENIZ_WORDS
    )
    record = plan.record()
    assert record["opening_speech"]["media_id"] == WELCOME
    assert record["closing_quiet"]["media_id"] == DENIZ

    # 5: still 20 s, the phone recipe verifies, and "river first, Deniz last" is met.
    assert plan.duration_s == pytest.approx(20.0, abs=0.05)
    assert _verify(contract, plan, clips)
    execution = compile_execution_plan(plan.guided_edit(), track=None)
    facts = plan_facts_from_rendered_montage(
        {"story_timeline": execution["story_timeline"], "source_audio_preserved": True}, record
    )
    order = next(req for req in brief.live() if req.kind == "order")
    receipt = check_requirement(order, facts)
    assert receipt.status == "met", receipt.reason


async def test_incident_reply_says_the_narration_plays_and_where_the_lines_go(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    result = await _gate(monkeypatch, _planned(), _brief())
    summary = _summary(result)
    assert summary.startswith("Tigre kulübe turunu")  # the model's own words stay first
    assert "Your clips keep their own sound, so your narration plays in the video." in summary
    assert "“Tigre'de ev turumuza hoş geldiniz” comes right after your opening shot" in summary
    assert "introduces someone (“Bu da Deniz”)" in summary
    assert "I end on that clip without those words" in summary


async def test_incident_reply_in_the_turkish_chat(monkeypatch: pytest.MonkeyPatch) -> None:
    with reply_language_for("tr"):
        result = await _gate(monkeypatch, _planned(), _brief())
    summary = _summary(result)
    assert "Kliplerin kendi sesini koruyorum, yani anlatımın videoda duyulacak" in summary
    assert "“Tigre'de ev turumuza hoş geldiniz” dediğin klip açılış çekiminden hemen sonra" in (
        summary
    )
    assert "bu sözler olmadan bitiriyorum" in summary
    assert "narration" not in summary and "clip where you say" not in summary


# -- where the clips' words decide nothing ---------------------------------------------


async def test_a_filming_order_ask_keeps_its_order_and_the_reply_adds_nothing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    filmed = BriefRequirement(
        id="r10",
        kind="order",
        scope="global",
        description="in the order I filmed them",
        facts={"key": "capture_time"},
    )
    brief = _brief(filmed)
    result = await _gate(monkeypatch, _planned(), brief, dated=True)
    assert "spoken_order" not in result.media_snapshot["length_plan"]
    summary = _summary(result)
    assert "narration" not in summary
    assert "hoş geldiniz" not in summary and "Deniz" not in summary


@pytest.mark.parametrize(
    "case", ["cloud", "song", "voiceover", "named_voice", "voice_mode", "talking"]
)
async def test_no_spoken_order_or_note_off_the_camera_sound_route(
    monkeypatch: pytest.MonkeyPatch, case: str
) -> None:
    manifest, extra = PHONE, {}
    if case == "cloud":
        manifest = CLOUD
    elif case == "song":
        extra = {"audio_strategy": "user_song"}
    elif case == "voiceover":
        manifest = SimpleNamespace(**{**vars(PHONE), "has_voiceover": True})
    elif case == "voice_mode":
        extra = {"voice_mode": "continuous"}
    elif case == "talking":
        extra = {"edit_format": "subtitled"}
    else:
        extra = {"montage_audio": {"preserve_source_audio": True, "source_media_ids": [DENIZ]}}
    result = await _gate(monkeypatch, _planned(manifest, **extra), _brief())
    length_plan = (result.media_snapshot or {}).get("length_plan") or {}
    assert "spoken_order" not in length_plan
    assert "narration" not in _summary(result)


async def test_without_the_spoken_order_the_incident_order_returns(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The control: the same approved plan, rendered as before the fix, plays the welcome
    ninth and ends on the introduction's words."""
    brief = _brief()
    result = await _gate(monkeypatch, _planned(manifest=CLOUD), brief)
    plan, _contract, _clips = _render(result, brief, speech_order=False)
    assert plan.clip_ids.index(WELCOME) == 8
    last = plan.snapshot.fast_cuts[-1]
    assert last.media_id == DENIZ
    assert _overlaps((last.source_start_s, last.source_end_s), DENIZ_WORDS[1])


# -- the kill switch, chatter and room-by-room welcomes --------------------------------


async def test_the_kill_switch_leaves_no_seat_no_note_and_no_reorder(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(settings, "spoken_order_enabled", False)
    brief = _brief()
    result = await _gate(monkeypatch, _planned(), brief)
    assert "spoken_order" not in result.media_snapshot["length_plan"]
    assert "narration" not in _summary(result)
    plan, _contract, _clips = _render(result, brief)
    assert plan.clip_ids.index(WELCOME) == 8
    assert "spoken_roles" not in plan.record()


def _with_speech(media_id: str, text: str) -> list[dict]:
    rows = copy.deepcopy(CLIPS)
    for row in rows:
        if row["media_id"] == media_id:
            speech = row["analysis"]["understanding"]["speech"]
            speech["segments"] = [{"text": text, "start_s": 0.5, "end_s": 2.0}]
            speech["transcript"] = row["analysis"]["transcript"] = text
    return rows


async def test_chatter_that_moves_nothing_adds_no_note(monkeypatch: pytest.MonkeyPatch) -> None:
    """The clips still speak (narration, Deniz's line elsewhere), but nothing the words say
    moves a clip or shapes the ending: the reply stays the model's own."""
    rows = _with_speech(WELCOME, "Burası mutfak.")
    rows = [
        {
            **row,
            "analysis": {
                **row["analysis"],
                "understanding": {**row["analysis"]["understanding"], "people": {"count": None}},
            },
        }
        if row["media_id"] == DENIZ
        else row
        for row in rows
    ]
    monkeypatch.setattr(f"{__name__}.CLIPS", rows)
    result = await _gate(monkeypatch, _planned(), _brief())
    assert "spoken_order" not in result.media_snapshot["length_plan"]
    assert _summary(result) == (
        "Tigre kulübe turunu 20 saniyelik hızlı bir montaj olarak tasarlıyorum."
    )


async def test_a_welcome_on_two_clips_moves_neither(monkeypatch: pytest.MonkeyPatch) -> None:
    rows = _with_speech("m1_09", "Ve oturma odamıza hoş geldiniz!")
    monkeypatch.setattr(f"{__name__}.CLIPS", rows)
    brief = _brief()
    result = await _gate(monkeypatch, _planned(), brief)
    roles = result.media_snapshot["length_plan"]["spoken_order"]["roles"]
    assert roles == {DENIZ: "introduction"}  # the quiet ending still applies
    assert "hoş geldiniz" not in _summary(result)
    plan, _contract, _clips = _render(result, brief)
    assert plan.clip_ids.index(WELCOME) == 8


# -- the phone worker follows only what the reply said ----------------------------------


class _Planned(Exception):
    """Raised by the spy once the worker has planned: the rest of the job is not under test."""


def _worker(
    monkeypatch: pytest.MonkeyPatch,
    result: PlannedKriaTurn,
    brief: CreativeBrief,
    *,
    strategy_update: dict | None = None,
    keep_record: bool = True,
    order_required: bool = False,
) -> dict:
    """Run ``_run_phone_unified_montage_job`` on the approved draft up to its montage plan."""
    strategy = {**result.plan.intents[0].arguments["strategy"], **(strategy_update or {})}
    snapshot = copy.deepcopy(result.media_snapshot)
    if not keep_record:
        snapshot["length_plan"].pop("spoken_order", None)
    binding = BriefBinding.create(
        uuid.uuid5(uuid.NAMESPACE_URL, "kri-585-worker"),
        brief,
        latest_message="Continue",
        media_snapshot=snapshot,
    )
    rows = [row for row in snapshot["clip_assignments"] if row["media_id"] in SELECTED]
    bindings = [
        PhoneSourceBinding(
            media_id=row["media_id"],
            proxy_path=row["gcs_path"],
            generation=row["storage_generation"],
            original=OriginalMediaDescriptor(
                sha256=f"{index:x}".rjust(64, "a"),
                byte_count=1000 + index,
                duration_s=row["duration_s"],
                width=1080,
                height=1920,
                has_audio=True,
            ),
        )
        for index, row in enumerate(rows)
    ]
    job_snapshot: dict = {
        PHONE_SOURCES_FIELD: [b.model_dump(mode="json") for b in bindings],
        "creator_generation_id": "generation",
        "creator_brief_binding": binding.model_dump(mode="json"),
    }
    if order_required:
        contract = build_render_contract(
            {**strategy, "ordering_choice": "chronological"},
            generation_id="generation",
            brief=binding.resolve(),
            media_snapshot=snapshot,
        )
        assert contract is not None and contract.order_required
        job_snapshot[CONTRACT_FIELD] = contract.model_dump(mode="json")
    candidates = {
        "clip_paths": [b.proxy_path for b in bindings],
        "edit_format": "montage",
        "creator_strategy": strategy,
    }
    monkeypatch.setattr(
        gb, "_load_unified_montage_inputs", lambda _job: (uuid.uuid4(), rows, binding.resolve())
    )
    monkeypatch.setattr(gb, "_load_unified_montage_visuals", lambda *_a, **_k: [])
    monkeypatch.setattr(gb.settings, "phone_rendering_enabled", True)
    monkeypatch.setattr(gb.settings, "clip_facts_enabled", False)
    seen: dict = {}
    real = unified_montage.plan_unified_montage

    def spy(*args, **kwargs):
        seen["speech_order"] = kwargs.get("speech_order")
        seen["plan"] = real(*args, **kwargs)
        raise _Planned

    monkeypatch.setattr(unified_montage, "plan_unified_montage", spy)
    with pytest.raises(_Planned):
        gb._run_phone_unified_montage_job(
            str(uuid.uuid4()), job_snapshot, candidates, ownership_epoch=3
        )
    return seen


async def test_the_phone_worker_reorders_what_the_reply_said(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    brief = _brief()
    result = await _gate(monkeypatch, _planned(), brief)
    seen = _worker(monkeypatch, result, brief)
    assert seen["speech_order"] is True
    assert seen["plan"].clip_ids[:2] == [RIVER, WELCOME]
    assert seen["plan"].clip_ids[-1] == DENIZ


@pytest.mark.parametrize(
    "case", ["voice_mode", "named_voice", "guided_voiceover", "no_record", "pinned_order", "off"]
)
async def test_the_phone_worker_never_reorders_what_the_reply_did_not_say(
    monkeypatch: pytest.MonkeyPatch, case: str
) -> None:
    """The approved draft is the incident's (the reply named the welcome); the worker's
    own inputs then differ in one way the planner would have refused, or the record is
    gone. The montage keeps the order nobody chose."""
    brief = _brief()
    result = await _gate(monkeypatch, _planned(), brief)
    kwargs: dict = {}
    if case == "voice_mode":
        kwargs["strategy_update"] = {"voice_mode": "per_clip"}
    elif case == "named_voice":
        kwargs["strategy_update"] = {
            "montage_audio": {"preserve_source_audio": True, "source_media_ids": [DENIZ]}
        }
    elif case == "guided_voiceover":
        kwargs["strategy_update"] = {"execution_contract": "guided_voiceover_v1"}
    elif case == "no_record":
        kwargs["keep_record"] = False
    elif case == "pinned_order":
        kwargs["order_required"] = True
    else:
        monkeypatch.setattr(settings, "spoken_order_enabled", False)
    seen = _worker(monkeypatch, result, brief, **kwargs)
    assert seen["speech_order"] is False
    assert seen["plan"].clip_ids.index(WELCOME) == 8
    assert "spoken_roles" not in seen["plan"].record()
