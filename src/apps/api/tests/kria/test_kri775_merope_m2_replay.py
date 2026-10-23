"""KRI-775: the Merope café M2 chat, replayed offline end to end.

Prod 6b689783a, 2026-10-21, thread EE7B4DCA, plan item 9fecc54d: one 68.37 s take and one
Cotopaxi photo. "Make a montage of six moments from it, about 2 seconds each, in this order:
... Put the Cotopaxi photo right after the hello wave for 1 second ... Freeze the frame for
one second on Atakan's bite. ... About 14 seconds." Two approved drafts, two renders refused
by the phone planner ten seconds after approval: "This draft is about 4.7s; you asked for
14s." No video.

``tests/fixtures/kri775_merope_m2_turns.json`` holds both renders' approved brief, strategy
and media snapshot (the 68 s take's clip record: duration, timed speech, best moments,
answers) exactly as prod pinned them, plus prod's own length plan and refusal. The first
two tests replay prod's recorded brief and reproduce prod's failure: the real gate rebuilds
prod's length plan and the real phone worker refuses it on prod's own "about 4.7s; you asked
for 14s" (on 857ab6738 its whole message and every receipt match prod word for word).

The six-windows and buildable tests replay the same turns with the creator's messages run
through ``carry_moment_ask`` the way the planner does since KRI-776 (#1711). The freeze test
judges prod's own recorded draft: its outcome does not depend on the moment ask. Together
they pin what the creator should have had:

- KRI-776 ("six moments, about 2 seconds each" reaches the brief): six ~2 s windows, in order.
- KRI-777 (freeze): an honest "Couldn't" for the freeze, never "you asked for 1s".
- Both, plus the photo seated between the take's moments: the draft is buildable (no
  blockers), so the approval card is offered and the worker renders it.

If the moment ask is dropped again, the six-windows and buildable tests go red while the
recorded-brief tests stay green.

What none of these need is a guess at where each moment sits in the take: that is KRI-779's
(and the moment-window step's) call.
"""

from __future__ import annotations

import contextlib
import copy
import json
import uuid
from dataclasses import dataclass
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from app.config import settings
from app.kria import planner
from app.kria.brief import CreativeBrief, apply_updates
from app.kria.brief_binding import BriefBinding
from app.kria.brief_checks import is_format_limit, needs_creator_choice
from app.kria.contracts import KriaToolIntent, KriaTurnPlan
from app.kria.media_sources import OriginalMediaDescriptor
from app.kria.planner import PlannedKriaTurn
from app.kria.style_asks import carry_moment_ask
from app.pipeline.phone_guided_plan import UnsupportedPhonePlan
from app.pipeline.unified_montage import UnifiedClip
from app.services.phone_sources import PHONE_SOURCES_FIELD, PhoneSourceBinding
from app.tasks import generative_build as gb

FIXTURE = json.loads(
    (Path(__file__).resolve().parents[1] / "fixtures" / "kri775_merope_m2_turns.json").read_text(
        encoding="utf-8"
    )
)
RENDERS = FIXTURE["renders"]
TAKE = RENDERS[0]["media_snapshot"]["clip_assignments"][0]["media_id"]
TAKE_S = RENDERS[0]["media_snapshot"]["clip_assignments"][0]["duration_s"]
PHOTO = f"asset-{FIXTURE['visual']['id']}"
CREATOR_ID = uuid.UUID("7b56dd6f-6b9a-4634-89c8-ce91bb50a50d")
THREAD_ID = uuid.UUID("ee7b4dca-b2e9-44f9-a073-c7a6a2e60e62")


# How many of FIXTURE["creator_messages"] each render's turn had seen. The fixture has no
# explicit field, so this is an inference from the brief version and the draft reply: brief
# v1 with the freeze still live ("freeze the frame on the bite") fits the first prompt plus
# the short second message (likely a picker answer); brief v2 with r3 superseded ("I'll skip
# the freeze frame") fits the recovery message too. Only whether message 0 or 2 is carried
# changes the result: any subset with either gives six windows, message 1 alone or none
# gives one.
MESSAGES_SEEN = {
    "417eecd4-681b-4c7f-8075-4a1dddfe6e85": 2,
    "50f67a06-2c78-4691-831c-5754e3bf9f74": 3,
}


def m2_brief(render: dict) -> CreativeBrief:
    """The brief approval pinned for ``render``, exactly as prod stored it: no moment ask.

    The faithfulness tests (and ``test_kri775_phone_montage_preflight``) judge prod's own
    draft with it.
    """
    return CreativeBrief.model_validate(copy.deepcopy(render["brief"]))


def m2_brief_with_moment_ask(render: dict) -> CreativeBrief:
    """``m2_brief`` as the turns store it since KRI-776 (#1711).

    The planner runs the creator's message through ``carry_moment_ask`` on each turn's brief
    updates before applying them. Here there are no model updates, only the creator's
    messages the render's turn had seen, in order.
    """
    brief = m2_brief(render)
    for message in FIXTURE["creator_messages"][: MESSAGES_SEEN[render["job_id"]]]:
        brief = apply_updates(brief, carry_moment_ask(brief, (), message), source_turn_id=None)
    return brief


def m2_visual() -> UnifiedClip:
    """The Cotopaxi photo as the worker's Visuals loader builds it (900x1600 JPEG)."""
    row = FIXTURE["visual"]
    return UnifiedClip(
        media_id=row["id"],
        proxy_path=f"users/{CREATOR_ID}/plan-item-assets/{row['id']}.jpg",
        generation="1",
        duration_s=0.0,
        analysis=dict(row["analysis"]),
        lane="asset",
        kind="image",
        manifest_id=PHOTO,
        aspect=row["aspect"],
        source_filename=row["source_filename"],
    )


def approved_media(render: dict, *, length_plan: dict | None = None) -> dict:
    """The media snapshot approval pinned: the clip record plus its length plan."""
    media = copy.deepcopy(render["media_snapshot"])
    media["length_plan"] = copy.deepcopy(
        render["prod_length_plan"] if length_plan is None else length_plan
    )
    return media


def prod_flags(monkeypatch: pytest.MonkeyPatch) -> None:
    """The prod settings this chat ran under (phone pilot, clip intents + facts on)."""
    monkeypatch.setattr(settings, "phone_rendering_enabled", True)
    monkeypatch.setattr(settings, "clip_intents_enabled", True)
    monkeypatch.setattr(settings, "clip_facts_enabled", True)
    monkeypatch.setattr(settings, "kria_creative_brief_enabled", True)
    monkeypatch.setattr(settings, "kria_brief_binding_enabled", True)
    monkeypatch.setattr(
        settings,
        "phone_render_verified_features",
        [*settings.phone_render_verified_features, "stillImages"],
    )


@dataclass(frozen=True)
class WorkerOutcome:
    refusal: str | None
    receipts: list[dict]

    @property
    def failures(self) -> list[dict]:
        return [row for row in self.receipts if needs_creator_choice(row)]


def run_worker(
    monkeypatch: pytest.MonkeyPatch,
    render: dict,
    *,
    brief: CreativeBrief | None = None,
    media: dict | None = None,
    strategy: dict | None = None,
    reply_language: str = "en",
) -> WorkerOutcome:
    """``_run_phone_unified_montage_job`` on an approved M2 draft, up to the pin.

    Only the database reads and writes are fixed values, plus the two model steps, which
    only write text (landmark names for per-clip labels, the creation-text lane). A plan
    that passes stops at the job lock (``refusal is None``). ``receipts`` are the worker's
    own requirement receipts in both cases, read off the plan record it judged.
    """
    import app.kria.brief_checks as brief_checks  # noqa: PLC0415
    import app.services.clip_facts as clip_facts  # noqa: PLC0415
    import app.services.creation_text_composition as text_lane  # noqa: PLC0415

    brief = brief if brief is not None else m2_brief(render)
    media = media if media is not None else approved_media(render)
    binding = BriefBinding.create(THREAD_ID, brief, latest_message="Continue", media_snapshot=media)
    rows = [row for row in media["clip_assignments"] if isinstance(row, dict)]
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
        "creator_generation_id": "kri775-generation",
        "creator_brief_binding": binding.model_dump(mode="json"),
        "creator_reply_language": reply_language,
    }
    candidates = {
        "clip_paths": [source.proxy_path for source in sources],
        "edit_format": "montage",
        "creator_strategy": copy.deepcopy(strategy if strategy is not None else render["strategy"]),
    }
    seen: dict = {}

    def save(_job_id, _snapshot, *, ownership_epoch, message, receipts=None):  # noqa: ANN001, ANN202, ARG001
        seen["receipts"] = list(receipts or [])
        return True

    def plan_stops_at_the_pin(_db, _job_id):  # noqa: ANN001, ANN202
        seen["planned"] = True
        return None

    read_record = brief_checks.plan_facts_from_unified_montage

    def judged(record):  # noqa: ANN001, ANN202
        seen["record"] = copy.deepcopy(dict(record or {}))
        return read_record(record)

    monkeypatch.setattr(
        gb, "_load_unified_montage_inputs", lambda _job: (CREATOR_ID, rows, binding.resolve())
    )
    monkeypatch.setattr(gb, "_load_unified_montage_visuals", lambda *_a, **_k: [m2_visual()])
    monkeypatch.setattr(clip_facts, "enrich_clip_facts", lambda pairs, **_k: list(pairs))
    monkeypatch.setattr(
        text_lane, "compose_creation_text", lambda plan_snapshot, **_k: plan_snapshot
    )
    monkeypatch.setattr(gb, "_save_request_recovery", save)
    monkeypatch.setattr(gb, "_sync_session", contextlib.nullcontext)
    monkeypatch.setattr(gb, "_lock_owned_entry_job", plan_stops_at_the_pin)
    monkeypatch.setattr(brief_checks, "plan_facts_from_unified_montage", judged)
    try:
        result = gb._run_phone_unified_montage_job(
            str(uuid.uuid4()), snapshot, candidates, ownership_epoch=1
        )
    except UnsupportedPhonePlan as exc:
        return WorkerOutcome(refusal=str(exc), receipts=seen["receipts"])
    assert result is None and seen.get("planned"), "the plan must reach the job lock"
    receipts = brief_checks.build_receipts(
        brief.live(), read_record(seen["record"]), include_unchecked=True
    )
    return WorkerOutcome(refusal=None, receipts=[r.model_dump(mode="json") for r in receipts])


def planned_turn(render: dict) -> PlannedKriaTurn:
    """The Main Creator's planned turn for ``render`` (draft + render request, as prod's),
    before the server's gate."""
    strategy = copy.deepcopy(render["strategy"])
    return PlannedKriaTurn(
        plan=KriaTurnPlan(
            mode="act",
            turn_value="action",
            intents=[
                KriaToolIntent(
                    intent_id="apply-strategy",
                    tool_name="draft.apply_strategy",
                    tool_version=1,
                    arguments={"summary": render["draft_reply"], "strategy": strategy},
                ),
                KriaToolIntent(
                    intent_id="request-render",
                    tool_name="render.request",
                    tool_version=1,
                    depends_on=["apply-strategy"],
                ),
            ],
        ),
        manifest_hash="a" * 64,
        context_hash="b" * 64,
        media_snapshot=copy.deepcopy(render["media_snapshot"]),
    )


async def run_gate(
    monkeypatch: pytest.MonkeyPatch, render: dict, *, brief: CreativeBrief | None = None
) -> PlannedKriaTurn:
    """The real server gate (`plan_live_turn` runs it once the snapshot is attached)."""
    brief = brief if brief is not None else m2_brief(render)
    monkeypatch.setattr(planner, "load_latest_brief", AsyncMock(return_value=brief))
    monkeypatch.setattr(planner, "_load_thread_events", AsyncMock(return_value=[]))
    result = await planner._gate_unresolved_choices(
        SimpleNamespace(), planned_turn(render), thread_id=THREAD_ID, creator_id=CREATOR_ID
    )
    assert result.plan.mode == "act", result.plan.response
    return result


def gate_media(result: PlannedKriaTurn) -> dict:
    return copy.deepcopy(result.media_snapshot)


def gate_strategy(result: PlannedKriaTurn) -> dict:
    return copy.deepcopy(result.plan.intents[0].arguments["strategy"])


@pytest.fixture(autouse=True)
def _prod(monkeypatch: pytest.MonkeyPatch) -> None:
    prod_flags(monkeypatch)


# -- the replay is faithful ---------------------------------------------------------------


@pytest.mark.parametrize("index", [0, 1], ids=["job-417eecd4", "job-50f67a06"])
async def test_the_gate_rebuilds_the_length_plan_prod_approved(
    monkeypatch: pytest.MonkeyPatch, index: int
) -> None:
    render = RENDERS[index]
    result = await run_gate(monkeypatch, render)
    plan = gate_media(result)["length_plan"]
    assert plan["windows"] == render["prod_length_plan"]["windows"]
    assert plan["conflicts"] == []


@pytest.mark.parametrize("index", [0, 1], ids=["job-417eecd4", "job-50f67a06"])
def test_the_phone_worker_refuses_it_on_prods_length_shortfall(
    monkeypatch: pytest.MonkeyPatch, index: int
) -> None:
    """On 857ab6738 (= prod 6b689783a for these paths) the worker's message and every
    receipt equal prod's word for word. Pinned here is the part the sibling fixes leave
    alone (KRI-777 rewords the freeze row, KRI-778 the message): the refusal itself and the
    total-length receipt, "about 4.7s; you asked for 14s"."""
    render = RENDERS[index]
    outcome = run_worker(monkeypatch, render)
    assert outcome.refusal is not None
    assert "This draft is about 4.7s; you asked for 14s." in render["prod_message"]
    ours = {row["requirement_id"]: row for row in outcome.receipts}
    assert (ours["r7"]["status"], ours["r7"]["verification"], ours["r7"]["reason"]) == (
        "partial",
        "checked",
        "This draft is about 4.7s; you asked for 14s.",
    )
    assert "r7" in {row["requirement_id"] for row in outcome.failures}
    if "prod_receipts" in render:
        prod = {row["requirement_id"]: row for row in render["prod_receipts"]}
        assert (prod["r7"]["status"], prod["r7"]["reason"]) == (
            ours["r7"]["status"],
            ours["r7"]["reason"],
        )


# -- what the creator should have had ------------------------------------------------------


def _windows(result: PlannedKriaTurn) -> list[dict]:
    return [w for w in gate_media(result)["length_plan"]["windows"] if w["media_id"] == TAKE]


@pytest.mark.parametrize("index", [0, 1], ids=["job-417eecd4", "job-50f67a06"])
async def test_six_moments_get_six_two_second_windows_in_order(
    monkeypatch: pytest.MonkeyPatch, index: int
) -> None:
    render = RENDERS[index]
    result = await run_gate(monkeypatch, render, brief=m2_brief_with_moment_ask(render))
    windows = _windows(result)
    assert len(windows) == 6, windows
    for window in windows:
        length = window["source_end_s"] - window["source_start_s"]
        assert length == pytest.approx(2.0, abs=0.05), window
        assert 0.0 <= window["source_start_s"] < window["source_end_s"] <= TAKE_S
    # The asked order is the order the moments happen in the take (tray, hello wave, plate,
    # bite, thumbs-up, bye-bye wave), so each window starts after the previous one ends.
    for earlier, later in zip(windows, windows[1:], strict=False):
        assert earlier["source_end_s"] <= later["source_start_s"], (earlier, later)


def test_the_freeze_is_an_honest_couldnt_never_a_total_length(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    render = RENDERS[0]
    receipts = run_worker(monkeypatch, render).receipts
    freeze = next((row for row in receipts if row["requirement_id"] == "r3"), None)
    for row in receipts:
        assert "you asked for 1s" not in str(row.get("reason")), row
    assert freeze is not None and freeze["status"] == "not_possible", freeze
    assert is_format_limit(freeze["reason"]) and "freez" in freeze["reason"].lower(), freeze


@pytest.mark.parametrize("index", [0, 1], ids=["job-417eecd4", "job-50f67a06"])
async def test_the_m2_draft_is_buildable_so_approval_is_offered(
    monkeypatch: pytest.MonkeyPatch, index: int
) -> None:
    from app.services.phone_montage_preflight import montage_blockers  # noqa: PLC0415

    render = RENDERS[index]
    brief = m2_brief_with_moment_ask(render)
    result = await run_gate(monkeypatch, render, brief=brief)
    media, strategy = gate_media(result), gate_strategy(result)
    blockers = montage_blockers(
        creator_id=CREATOR_ID,
        edit_format="montage",
        brief=brief,
        strategy=strategy,
        media_snapshot=media,
        visuals=[m2_visual()],
    )
    assert blockers == [], [b.reason for b in blockers]
    outcome = run_worker(monkeypatch, render, brief=brief, media=media, strategy=strategy)
    assert outcome.refusal is None, outcome.refusal


def test_the_fixture_is_the_kits_one_take_and_one_photo() -> None:
    """Guards the fixture itself: a hand edit that changes the footage changes the incident."""
    for render in RENDERS:
        rows = render["media_snapshot"]["clip_assignments"]
        assert [row["media_id"] for row in rows] == [TAKE]
        assert rows[0]["duration_s"] == 68.37
        assert render["prod_length_plan"]["windows"][0]["source_start_s"] == 56.94
    assert FIXTURE["creator_messages"][0].startswith("One 68-second take at Merope")
    assert "six moments from it, about 2 seconds each" in FIXTURE["creator_messages"][0]
