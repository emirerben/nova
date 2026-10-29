"""KRI-1028: a clip kept audible for a line the creator named holds that whole sentence.

Incident (prod thread A228E85B, 2026-10-28, Plaza de Mayo kit M1 "maximo 3", job 97d55f9a):
"Only the clip where I say "we are going to Recoleta" keeps its sound, mute the rest." The
brief recorded r4 as ``{"clip_sound": "off", "sound_except": [{"sound_media": <recoleta>}]}``;
the clip's window was seated on the timed sentence the ask describes (4.62-8.88 s, padded to
3.82-9.18 s) and the review said met "(10.5-15.8 s)" from the toggle alone.

Measured on the kit's source audio (``recoleta_measured`` in the fixture), the sentence "Well,
yeah, we started great. Now we are going to Recoleta." runs 4.6-8.9 s and the repeated
"Recoleta" starts at 9.35 s, so the incident's own 20 s cut held it whole. The ticket's "starts
inside 'yeah,'" came from a whole-clip whisper pass whose times run ~2.6 s early. What was left
to chance: the length plan floors that window at 4 s, so a shorter video cuts the sentence from
its end, and the receipt never checked the span.

Clip records, brief, strategy and approved windows are the incident's own
(``tests/fixtures/kri1028_plaza_m1_recoleta.json``).

Ways this can fail, each pinned below:
1. The incident's own window moves (it held the sentence; a "fix" that moves it plays noise).
2. A shorter video trims the kept window to the 4 s floor and cuts the sentence's end.
3. A sentence longer than the creator's length allows blocks the draft instead of saying so.
4. A clip kept without a named line gets a different window.
5. An ask that plays sound only during its line (``sound_line``) is touched by this path.
6. The receipt says met because the clip's sound is on while the cut misses the sentence.
7. A re-judge after the cut was trimmed (an editor Save) still says met.
8. The length drifts off the creator's total, or the phone verifier refuses the windows.
9. A kept clip left out of the edit is reported as a sentence that "doesn't fit".
"""

from __future__ import annotations

import copy
import dataclasses
import json
import uuid
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from app.config import settings
from app.kria.brief import BriefRequirement, CreativeBrief
from app.kria.brief_binding import BriefBinding
from app.kria.brief_checks import PlanFacts, check_requirement, plan_facts_from_phone_variant
from app.kria.media_sources import OriginalMediaDescriptor
from app.kria.planner import _enrich_best_parts_snapshot
from app.kria.reply_language import reply_language_for
from app.pipeline.guided_story import compile_execution_plan
from app.pipeline.unified_montage import brief_view, plan_unified_montage
from app.services.creator_render_contract import build_render_contract
from app.services.device_render import device_status
from app.services.phone_clip_sound_toggle import TOGGLE_RECEIPT_FIELD, rendered_toggle_receipt
from app.services.phone_sources import PHONE_SOURCES_FIELD, PhoneSourceBinding
from app.tasks import generative_build as gb
from tests.pipeline.test_unified_montage import clip
from tests.tasks.test_selected_clip_dispatch import _verify

_FIXTURE = Path(__file__).resolve().parents[1] / "fixtures" / "kri1028_plaza_m1_recoleta.json"
DATA = json.loads(_FIXTURE.read_text(encoding="utf-8"))
CLIPS = DATA["clips"]
RECOLETA = "m1_05"
LINE = "we are going to Recoleta"
# Measured on the source audio: the sentence, and where the repeated "Recoleta" starts.
SENTENCE = tuple(DATA["recoleta_measured"]["sentence_s"])
NEXT_WORD_S = DATA["recoleta_measured"]["next_word_s"]
# The incident's approved window, and the timed sentence it was seated on.
INCIDENT_WINDOW = (3.82, 9.18)
TIMED_SENTENCE = (4.62, 8.88)
_FEATURES = [
    "basicComposition",
    "local1080Export",
    "audioMix",
    "positionedText",
    "animatedText",
    "authoredText",
    "visualBlocks",
]


@pytest.fixture(autouse=True)
def _clip_intents_on(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(settings, "clip_intents_enabled", True)


def _brief(length_s: float | None = None, **r4: object) -> CreativeBrief:
    rows = copy.deepcopy(DATA["brief"]["requirements"])
    for row in rows:
        if row["id"] == "r4":
            row.update(r4)
        if row["id"] == "r8" and length_s is not None:
            row.update(description=f"About {length_s:g} seconds", facts={"duration_s": length_s})
    return CreativeBrief(
        version=DATA["brief"]["version"], requirements=[BriefRequirement(**r) for r in rows]
    )


def _r4(brief: CreativeBrief) -> BriefRequirement:
    return next(r for r in brief.requirements if r.id == "r4")


def _strategy() -> dict:
    return copy.deepcopy(DATA["strategy"])


def _snapshot() -> dict:
    return {
        "clip_assignments": [
            {
                **copy.deepcopy(c),
                "gcs_path": f"users/u/analysis-proxy-{c['media_id']}.mp4",
                "storage_generation": "1",
            }
            for c in CLIPS
        ]
    }


def _plan(brief: CreativeBrief | None = None) -> dict:
    """The planner's length plan for the approved strategy and this brief."""
    return _enrich_best_parts_snapshot(_strategy(), _snapshot(), brief or _brief())


def _window(snapshot: dict, media_id: str = RECOLETA) -> tuple[float, float]:
    (row,) = [w for w in snapshot["length_plan"]["windows"] if w["media_id"] == media_id]
    return row["source_start_s"], row["source_end_s"]


def _total(snapshot: dict) -> float:
    return sum(w["source_end_s"] - w["source_start_s"] for w in snapshot["length_plan"]["windows"])


def _holds(start: float, end: float, span: tuple[float, float]) -> bool:
    return start <= span[0] + 0.06 and end >= span[1] - 0.06


# -- the length plan -------------------------------------------------------------------


def test_incident_window_already_held_the_sentence_and_stays() -> None:
    """1, 8: the 20 s plan is the incident's own, now with the span on record."""
    snapshot = _plan()
    start, end = _window(snapshot)
    assert (start, end) == INCIDENT_WINDOW
    assert _holds(start, end, SENTENCE) and end < NEXT_WORD_S
    (record,) = snapshot["length_plan"]["kept_lines"]
    assert record["requirement_id"] == "r4" and record["media_id"] == RECOLETA
    assert record["line"] == LINE
    assert (record["start_s"], record["end_s"]) == TIMED_SENTENCE
    assert (record["window_start_s"], record["window_end_s"]) == INCIDENT_WINDOW
    assert record["held"] is True
    assert not snapshot["length_plan"]["conflicts"]
    assert _total(snapshot) == pytest.approx(20.0, abs=0.05)
    approved = {
        (w["media_id"], w["source_start_s"], w["source_end_s"]) for w in DATA["approved_windows"]
    }
    assert (RECOLETA, *INCIDENT_WINDOW) in approved


@pytest.mark.parametrize("length_s", [10, 9, 8])
def test_a_shorter_video_keeps_the_whole_sentence(length_s: int) -> None:
    """2, 8: main trims the kept window to its 4 s floor's share (10 s: 3.82-8.707, cutting
    "Recoleta."); the silent clips give way instead."""
    snapshot = _plan(_brief(length_s))
    start, end = _window(snapshot)
    assert _holds(start, end, SENTENCE), (start, end)
    assert snapshot["length_plan"]["kept_lines"][0]["held"] is True
    assert not snapshot["length_plan"]["conflicts"]
    assert _total(snapshot) == pytest.approx(length_s, abs=0.05)
    assert {w["media_id"] for w in snapshot["length_plan"]["windows"]} == {
        c["media_id"] for c in CLIPS
    }


def test_a_sentence_longer_than_the_video_says_so_before_approval() -> None:
    """3: a 5 s video can't hold the 5.4 s window beside six other clips: the plan keeps what
    fits and the draft's receipt says the line won't be whole (no conflict, no blocked draft)."""
    brief = _brief(5)
    snapshot = _plan(brief)
    plan = snapshot["length_plan"]
    assert not plan["conflicts"]
    assert plan["kept_lines"][0]["held"] is False
    assert _total(snapshot) == pytest.approx(5.0, abs=0.05)
    facts = PlanFacts(
        phone_montage=True, media_snapshot=snapshot, clip_ids=tuple(c["media_id"] for c in CLIPS)
    )
    receipt = check_requirement(_r4(brief), facts)
    assert receipt.status == "partial"
    assert receipt.reason == (
        f"\"{LINE}\" doesn't fit whole in the video's length, so part of it is cut"
    )


def test_a_clip_kept_without_a_named_line_is_untouched() -> None:
    """4: no record, and the same plan as with no sound facts at all."""
    description = "Only the bus clip keeps its sound, mute the rest"
    kept = _brief(
        10,
        description=description,
        facts={"clip_sound": "off", "sound_except": [{"sound_media": "m1_08"}]},
    )
    snapshot = _plan(kept)
    assert "kept_lines" not in snapshot["length_plan"]
    plain = _plan(_brief(10, description=description, facts={}))
    assert snapshot["length_plan"] == plain["length_plan"]


def test_a_kept_clip_left_out_of_the_edit_has_no_sentence_to_hold() -> None:
    """9: the toggle says "not in edit"; the plan claims no sentence for it."""
    others = [c["media_id"] for c in CLIPS if c["media_id"] != RECOLETA]
    snapshot = _enrich_best_parts_snapshot(
        {**_strategy(), "selected_media_ids": others}, _snapshot(), _brief()
    )
    assert snapshot["length_plan"].get("kept_lines", []) == []
    assert RECOLETA not in {w["media_id"] for w in snapshot["length_plan"]["windows"]}


def test_a_line_that_plays_alone_is_left_to_the_toggle() -> None:
    """5: ``sound_line`` plays only where the line is said; this path doesn't seat it."""
    brief = _brief(
        facts={"clip_sound": "off", "sound_except": [{"sound_media": RECOLETA, "sound_line": LINE}]}
    )
    assert "kept_lines" not in _plan(brief)["length_plan"]


def test_draft_says_the_line_plays_whole() -> None:
    facts = PlanFacts(
        phone_montage=True, media_snapshot=_plan(), clip_ids=tuple(c["media_id"] for c in CLIPS)
    )
    receipt = check_requirement(_r4(_brief()), facts)
    assert receipt.status == "met"
    assert receipt.reason == f'Clip sound off except that clip; "{LINE}" plays whole'


# -- the finished video ----------------------------------------------------------------


def _unified_clips():
    return [
        dataclasses.replace(
            clip(i, duration=c["duration_s"]),
            capture_time=datetime.fromisoformat(c["capture"]["capture_time"]),
            media_id=c["media_id"],
            proxy_path=f"users/u/analysis-proxy-{c['media_id']}.mp4",
            analysis=c["analysis"],
            width=c["width"] or 1080,
            height=c["height"] or 1920,
        )
        for i, c in enumerate(CLIPS)
    ]


def _binding(brief: CreativeBrief, snapshot: dict) -> dict:
    return BriefBinding.create(
        uuid.uuid5(uuid.NAMESPACE_URL, "kri-1028"),
        brief,
        latest_message="Make the title just Plaza de Mayo, no flag.",
        media_snapshot=snapshot,
    ).model_dump(mode="json")


def _montage(brief: CreativeBrief, snapshot: dict):
    resolved = BriefBinding.model_validate(_binding(brief, snapshot)).resolve()
    clips = _unified_clips()
    plan = plan_unified_montage(
        clips,
        brief_view(resolved),
        strategy=_strategy(),
        clip_intents_enabled=True,
        creator_duration_proven=True,
        approved_length_plan=snapshot["length_plan"],
    )
    contract = build_render_contract(
        _strategy(), generation_id="gen", brief=resolved, media_snapshot=snapshot
    )
    return plan, contract, clips


def _bindings() -> tuple[PhoneSourceBinding, ...]:
    return tuple(
        PhoneSourceBinding(
            media_id=c["media_id"],
            proxy_path=f"users/u/analysis-proxy-{c['media_id']}.mp4",
            generation="1",
            original=OriginalMediaDescriptor(
                sha256=f"{index:x}".rjust(64, "c"),
                byte_count=10_000 + index,
                duration_s=c["duration_s"],
                width=c["width"] or 1080,
                height=c["height"] or 1920,
                has_audio=c["has_audio"],
            ),
        )
        for index, c in enumerate(CLIPS)
    )


def _phone_job(monkeypatch: pytest.MonkeyPatch, brief: CreativeBrief, snapshot: dict, plan):
    """The approved plan through the real phone writer (dispatcher, clip-sound lane, pilot)."""
    execution = compile_execution_plan(plan.guided_edit(), track=None)
    job = SimpleNamespace(
        id=uuid.uuid4(),
        user_id=uuid.uuid4(),
        assembly_plan={
            PHONE_SOURCES_FIELD: [b.model_dump(mode="json") for b in _bindings()],
            "creator_generation_id": "generation",
            "guided_edit": {"approved": True},
            "creator_brief_binding": _binding(brief, snapshot),
        },
        status="queued",
        all_candidates={"creator_strategy": _strategy()},
        error_detail=None,
        failure_reason=None,
    )

    @contextmanager
    def sessions():
        yield Mock()

    monkeypatch.setattr(gb, "_sync_session", sessions)
    monkeypatch.setattr(gb, "_lock_owned_entry_job", lambda *args: (job, 3))
    monkeypatch.setattr(gb, "_guided_execution_plan", Mock(return_value=(execution, None)))
    monkeypatch.setattr(gb.settings, "phone_rendering_enabled", True)
    monkeypatch.setattr(gb.settings, "phone_clip_sound_windows_enabled", False)
    monkeypatch.setattr(gb.settings, "phone_render_verified_features", _FEATURES)
    monkeypatch.setattr(
        gb, "_run_guided_story_job", Mock(side_effect=AssertionError("entered the cloud renderer"))
    )
    gb._run_generative_job(str(job.id))
    assert job.status == "awaiting_device", (job.error_detail, job.failure_reason)
    return job


def _recipe(job):
    return device_status(job, "guided_story").request.recipe


def _story_clips(recipe):
    return next(t for t in recipe.tracks if t.id == "story").clips


def _rendered(monkeypatch: pytest.MonkeyPatch, brief: CreativeBrief):
    snapshot = _plan(brief)
    plan, contract, clips = _montage(brief, snapshot)
    assert _verify(contract, plan, clips)
    return plan, _phone_job(monkeypatch, brief, snapshot, plan)


def test_finished_video_plays_the_sentence_and_the_receipt_checks_it(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """6, 8: the phone recipe plays the sentence with its sound, every other clip silent, and
    the review reads the span."""
    brief = _brief()
    plan, job = _rendered(monkeypatch, brief)
    (cut,) = [c for c in plan.snapshot.fast_cuts if c.media_id == RECOLETA]
    assert _holds(cut.source_start_s, cut.source_end_s, SENTENCE)
    assert plan.duration_s == pytest.approx(20.0, abs=0.05)

    recipe = _recipe(job)
    gain = recipe.audio.original_volume
    audible = [c for c in _story_clips(recipe) if c.volume * gain > 0]
    assert [c.source_asset_id for c in audible] == [RECOLETA]
    (kept,) = audible
    assert _holds(kept.source_start, kept.source_start + kept.source_duration, SENTENCE)

    variant = job.assembly_plan["variants"][0]
    (row,) = [r for r in variant[TOGGLE_RECEIPT_FIELD]["rows"] if r["requirement_id"] == "r4"]
    assert row["status"] == "met", row
    assert row["line"] == LINE and (row["line_start_s"], row["line_end_s"]) == TIMED_SENTENCE
    receipt = check_requirement(_r4(brief), plan_facts_from_phone_variant(variant))
    assert receipt.status == "met"
    assert receipt.reason.startswith("Clip sound off except that clip (")
    assert receipt.reason.endswith(f'; "{LINE}" plays whole (4.6-8.9 s of that clip)')
    with reply_language_for("tr"):
        turkish = check_requirement(_r4(brief), plan_facts_from_phone_variant(variant))
    assert f'"{LINE}" baştan sona duyuluyor (o klibin 4.6-8.9 s kısmı)' in turkish.reason


def test_a_cut_that_misses_the_sentence_is_not_met(monkeypatch: pytest.MonkeyPatch) -> None:
    """6, 7 (negative control): the same video with the kept cut ending at 8.37 s, where main's
    8 s plan ends it (inside "Recoleta."), re-judged as the review does: the clip's sound is on,
    but the receipt is partial, never met."""
    brief = _brief()
    _plan_, job = _rendered(monkeypatch, brief)
    recipe = _recipe(job)
    clips = []
    for c in _story_clips(recipe):
        if c.source_asset_id == RECOLETA:
            c = c.model_copy(update={"source_duration": round(8.37 - c.source_start, 3)})
        clips.append(c)
    tracks = [
        t.model_copy(update={"clips": clips}) if t.id == "story" else t for t in recipe.tracks
    ]
    trimmed = recipe.model_copy(update={"tracks": tracks})

    variant = job.assembly_plan["variants"][0]
    rows = rendered_toggle_receipt(job.assembly_plan, variant, trimmed)["rows"]
    (row,) = [r for r in rows if r["requirement_id"] == "r4"]
    assert row["status"] == "partial", row
    assert row["reason_code"] == "line_partly_in_edit"
    facts = plan_facts_from_phone_variant({**variant, TOGGLE_RECEIPT_FIELD: {"rows": rows}})
    receipt = check_requirement(_r4(brief), facts)
    assert receipt.status == "partial"
    assert receipt.reason == f'Only part of "{LINE}" is in the edit'
