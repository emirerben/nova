"""An approved edit whose text pass rejects one item renders without it (KRI-1376, KRI-129).

Prod, 2026-10-29 .. 2026-11-10: 6 of 13 failed phone jobs ended as ``creation_text_failed``
after the creator approved (c7c8fc75, 98eac78f, fba2c864, 3c312af4, 4e8c925d, 89fefe4d). Each
gate below gets one bad row next to a good one, through the REAL worker task
(``_run_generative_job`` -> unified montage planner -> text pass -> guided compiler -> pin-time
``verify_phone_recipe``); only storage, the DB row and the composer's answer are fixed.

Ways this could fail, written before the code (each is asserted below):
  1. the degrade drops the creator's confirmed words, or an op placing a brief literal;
  2. the drop is silent: no note on the variant, no ``requirement_recovery`` trace event;
  3. the good row next to the bad one is dropped with it (the whole lane falls back);
  4. the recipe no longer passes ``verify_phone_recipe`` (the contract check was weakened);
  5. with the degrade switched off the row does NOT fail (the case would prove nothing);
  6. a bad row only a confirmed item could fix still renders instead of failing.

Review of #2314, written before the fixes:
  7. a line her described text ask wanted (prod 3c312af4: "the same but for the English
     translation at the bottom") is dropped as unasked and nothing says so;
  8. after approval the note reaches only fields no client reads, so the finished-render
     reply (the one place she reads it) does not say what was left out, or a later edit's
     reply repeats it;
  9. two rows bad the same way: every single drop still fails, so the video fails;
 10. a composer bundle rejected as a whole renders the planner's lane with a generic note;
 11. the stricter incomplete rule fails a pass where only an asked behavior was missing.
"""

from __future__ import annotations

import json
import uuid
from collections.abc import Callable
from dataclasses import dataclass, field
from types import SimpleNamespace
from typing import Any, get_args
from unittest.mock import Mock

import pytest

from app.agents.edit_copilot import EditCopilotAgent
from app.config import settings
from app.kria.brief import BriefRequirement, CreativeBrief
from app.kria.brief_binding import BriefBinding
from app.kria.recipes import MediaCapability
from app.kria.reply_language import reply_language_for
from app.pipeline.phone_plan_repair import _quoted
from app.schemas.editorial_plan import EditorialBeat, EditorialPlan
from app.services import creation_text_composition as composition
from app.services.creation_text_degrade import Degraded, finished_render_notes
from app.services.creator_render_contract import (
    CONTRACT_FIELD,
    REQUIREMENT_VERSION_FIELD,
    build_render_contract,
    verify_phone_recipe,
)
from app.services.device_render import device_status
from app.tasks import generative_build as gb
from app.tasks import kria_runtime
from tests.kria import test_approved_long_sequence_copy as base
from tests.tasks.test_unified_montage_dispatch import harness  # noqa: F401

TITLE = "Morning run"
GOOD = "Bebek sahili"
FIRST_CLIP_END = 3.5
THREAD = uuid.UUID("3c312af4-0000-4000-8000-000000001376")
# Prod 3c312af4: the English line her described title ask wanted, which no new row may hold.
ENGLISH = "November 10 is not just a day of mourning"


def _brief(*extra: BriefRequirement) -> CreativeBrief:
    return CreativeBrief(
        version=1,
        requirements=[
            BriefRequirement(id="title", kind="text", scope="title", literal=TITLE),
            *extra,
        ],
    )


_EXPLAIN = BriefRequirement(
    id="explain", kind="text", scope="per_clip", description="say where each place is"
)


def _add(text: str, start: float = 0.5, end: float = FIRST_CLIP_END) -> dict:
    return {"op": "add_text", "text": text, "start_s": start, "end_s": end}


def _title_id(snapshot: dict) -> str:
    return next(bar["id"] for bar in snapshot["text_bars"] if bar["text"] == TITLE)


def _gone_beat(plan: Any) -> None:
    """A planner row: the approved story's second beat names a clip the cut does not play."""
    played = [cut.media_id for cut in plan.snapshot.fast_cuts]
    plan.snapshot = plan.snapshot.model_copy(
        update={
            "editorial_plan": EditorialPlan(
                viewer_goal="Show the route",
                beats=[
                    EditorialBeat(purpose="The route", media_ids=played, text_goal="Name it"),
                    EditorialBeat(purpose="The finish", media_ids=["clip-gone"], text_goal="Why"),
                ],
            )
        }
    )


# Prod thread 147eb928 (89fefe4d): a paragraph in the middle and its translation at the bottom,
# both asked phrase by phrase. The translation is longer than a label, so the text pass seeds its
# bar and records both as approved layers shown together.
MIDDLE = base.LOCAL
BOTTOM = base.ENGLISH


def _phrases(req_id: str, literal: str, position: str) -> BriefRequirement:
    return BriefRequirement(
        id=req_id,
        kind="text_sequence",
        scope="title",
        literal=literal,
        description="sentence by sentence",
        facts={"mode": "replace", "unit": "phrase", "position": position},
    )


def _sentences(text: str) -> list[str]:
    parts = text.split(". ")
    return [part + "." for part in parts[:-1]] + parts[-1:]


def _paired(snapshot: dict) -> list[dict]:
    """Both paragraphs timed to the whole video, then a stray retime of the translation alone:
    the pair is no longer on screen together (the 89fefe4d refusal)."""
    bars = {bar["text"]: bar["id"] for bar in snapshot["text_bars"]}
    total = snapshot["total_duration_s"]

    def timing(text: str, end: float) -> dict:
        return {
            "op": "set_texts_timing",
            "selector": {"ids": [bars[text]]},
            "start_s": 0,
            "end_s": end,
        }

    def split(text: str, position: str) -> dict:
        return {
            "op": "replace_text_sequence",
            "selector": {"ids": [bars[text]]},
            "segments": _sentences(text),
            "patch": {"position": position},
        }

    return [
        timing(MIDDLE, total),
        timing(BOTTOM, total),
        timing(BOTTOM, 3),
        split(MIDDLE, "middle"),
        split(BOTTOM, "bottom"),
    ]


@dataclass(frozen=True)
class Gate:
    """One late text gate: the composer's answer (a bad row beside a good one) and what renders."""

    name: str
    ops: Callable[[dict], list[dict]]
    note: str | None
    gone: str | None = None
    kept: str = GOOD
    brief: CreativeBrief = field(default_factory=lambda: _brief(_EXPLAIN))
    unmet: tuple[dict, ...] = ()
    planner: Callable[[Any], None] | None = None
    # The `requirement_recovery` reasons, in order; defaults to one drop named like the gate.
    reasons: tuple[str, ...] | None = None
    keeps_good: bool = True


def _past_end(text: str) -> Callable[[dict], dict]:
    return lambda snap: _add(text, 1.0, snap["total_duration_s"] + 5)


GATES = [
    Gate(
        "copy_changed",
        lambda snap: [
            _add(GOOD),
            {"op": "rewrite_text", "selector": {"ids": [_title_id(snap)]}, "text": "Evening walk"},
        ],
        note=f"Kept “{TITLE}” exactly as you wrote it and left out the change I couldn't "
        "make to it",
        gone="Evening walk",
        kept=TITLE,
    ),
    Gate(
        "replay_failed",
        lambda snap: [_add(GOOD), _add("Past the end", 1.0, snap["total_duration_s"] + 5)],
        note="Left out one text change for “Past the end” because I couldn't make it fit",
        gone="Past the end",
    ),
    Gate(
        "unasked_text",
        lambda snap: [
            {
                "op": "patch_text",
                "selector": {"ids": [_title_id(snap)]},
                "patch": {"color": "#FFD400"},
            },
            _add("Follow for more"),
        ],
        note=None,
        gone="Follow for more",
        kept=TITLE,
        brief=_brief(
            BriefRequirement(
                id="style", kind="style", scope="title", description="Make the title yellow"
            )
        ),
        keeps_good=False,
    ),
    Gate(
        "unasked_text_she_described",
        lambda snap: [_add(GOOD), _add(ENGLISH, 0.0, 3.0)],
        note=f"Left out {_quoted(ENGLISH)} because I couldn't add it as its own line",
        gone=ENGLISH,
        kept=TITLE,
        brief=_brief(
            BriefRequirement(
                id="r2",
                kind="text_sequence",
                scope="title",
                description="the same but for the english translation at the bottom",
            )
        ),
        reasons=("unasked_text", "unasked_text"),
        keeps_good=False,
    ),
    Gate(
        "replay_failed_twice",
        lambda snap: [_add(GOOD), _past_end("Past the end")(snap), _past_end("Also past")(snap)],
        note="Left out one text change for “Past the end” because I couldn't make it fit",
        gone="Also past",
        reasons=("replay_failed", "replay_failed"),
    ),
    Gate(
        "layer_shape",
        _paired,
        note=f"Left out one text change for {_quoted(BOTTOM)} because I couldn't make it fit",
        kept=_sentences(BOTTOM)[0],
        brief=CreativeBrief(
            version=1,
            requirements=[_phrases("r1", MIDDLE, "middle"), _phrases("r2", BOTTOM, "bottom")],
        ),
        keeps_good=False,
    ),
    Gate(
        "incomplete",
        lambda snap: [_add(GOOD)],
        note="I couldn't do every part of your text request, so the video shows the parts I could",
        unmet=({"request": "English word by word", "reason": "No text holds the English yet"},),
    ),
    Gate(
        "incomplete_nothing_written",
        lambda snap: [],
        note="I couldn't do every part of your text request, so the video shows the parts I could",
        kept=TITLE,
        unmet=({"request": "make the title pop in", "reason": "No pop-in for a title"},),
        reasons=("incomplete",),
        keeps_good=False,
    ),
    Gate(
        "editorial_unmet",
        lambda snap: [_add(GOOD)],
        note="Left out one explanation that didn't fit its part of the video",
        planner=_gone_beat,
    ),
]


def _answer(gate: Gate) -> Callable[..., Any]:
    def run(self, input, *, ctx=None):  # noqa: ARG001
        reply = {
            "intent": "edit",
            "confidence": 0.9,
            "reply": "Prepared.",
            "ops": gate.ops(input.variant_snapshot),
            "unmet_requests": list(gate.unmet),
        }
        return self.parse(json.dumps(reply), input)

    return run


def _job(harness, monkeypatch, gate: Gate):  # noqa: F811
    job, *_ = harness(brief=gate.brief)
    job.all_candidates["creator_request"] = "Title: Morning run. Say where each place is."
    job.assembly_plan["creator_brief_binding"] = BriefBinding.create(THREAD, gate.brief).model_dump(
        mode="json"
    )
    contract = build_render_contract(
        job.all_candidates["creator_strategy"], generation_id="generation", brief=gate.brief
    )
    if contract is not None:
        job.assembly_plan[CONTRACT_FIELD] = contract.model_dump(mode="json")
        job.all_candidates[REQUIREMENT_VERSION_FIELD] = 1
    monkeypatch.setattr(EditCopilotAgent, "run", _answer(gate))
    if gate.planner is not None:
        plan_montage = gb._plan_phone_unified_montage

        def planned(*args, **kwargs):
            plan = plan_montage(*args, **kwargs)
            gate.planner(plan)
            return plan

        monkeypatch.setattr(gb, "_plan_phone_unified_montage", planned)
    return job, contract


def _events(monkeypatch) -> list[tuple[str, str, dict]]:
    import app.services.pipeline_trace as pipeline_trace

    events: list[tuple[str, str, dict]] = []
    monkeypatch.setattr(
        pipeline_trace,
        "record_pipeline_event",
        lambda stage, event, data=None: events.append((stage, event, data or {})),
    )
    return events


def _drawn(job) -> set[str]:
    """Each text layer the phone draws, spaces dropped (a wrapped line is its own run)."""
    recipe = device_status(job, "guided_story").request.recipe
    return {
        "".join(run.text for run in layer.runs).replace(" ", "") for layer in recipe.text_layers
    }


def _squash(text: str) -> str:
    return text.replace(" ", "")


@pytest.fixture(autouse=True)
def _verified(monkeypatch):
    monkeypatch.setattr(settings, "phone_font_qualification_strict", False)
    monkeypatch.setattr(settings, "phone_render_verified_features", list(get_args(MediaCapability)))


@pytest.mark.parametrize("gate", GATES, ids=[gate.name for gate in GATES])
def test_one_bad_row_is_left_out_and_the_video_renders(harness, monkeypatch, gate):  # noqa: F811
    job, contract = _job(harness, monkeypatch, gate)
    events = _events(monkeypatch)
    failure = Mock(return_value=True)
    monkeypatch.setattr(gb, "_fail_job", failure)

    gb._run_generative_job(str(job.id))

    failure.assert_not_called()
    assert job.status == "awaiting_device"
    recipe = device_status(job, "guided_story").request.recipe
    if contract is not None:
        verify_phone_recipe(contract, recipe, source_audio={"source": True})
    drawn = _drawn(job)
    assert _squash(gate.kept) in drawn
    assert _squash(GOOD) in drawn or not gate.keeps_good
    assert gate.gone is None or _squash(gate.gone) not in drawn
    variant = job.assembly_plan["variants"][0]
    # The editor recompiles from the persisted rows: the dropped row stays dropped there too.
    rows = [row["text"] for row in variant["text_elements"]]
    assert gate.gone is None or gate.gone not in rows
    notes = variant.get("phone_repair_notes") or []
    recoveries = [
        data
        for _stage, event, data in events
        if event == "requirement_recovery" and data.get("lane") == "text"
    ]
    assert [data["reason"] for data in recoveries] == list(gate.reasons or (gate.name,))
    reply = _finished_reply(job, variant)
    if gate.note is None:
        assert not any("Follow for more" in note for note in notes)
        assert "Follow for more" not in reply
    else:
        assert gate.note in notes
        repaired = [data for _s, event, data in events if event == "phone_plan_repaired"]
        assert repaired and gate.note in repaired[-1]["notes"]
        # The reply to the finished video is where she reads it (KRI-1261 hid the rest).
        assert gate.note in reply
        later = {**variant, "render_generation_id": "a-later-edit"}
        assert finished_render_notes(job.assembly_plan, {"a-later-edit"}) == []
        assert gate.note not in _finished_reply(job, later)


def _finished_reply(job, variant: dict) -> str:
    """The `assistant_review` text the creator reads when this render is ready."""
    thread = SimpleNamespace(id=THREAD, creator_id=uuid.uuid4())
    execution = SimpleNamespace(result={}, target_draft_id=None)
    with reply_language_for("en"):
        text, _receipts = kria_runtime._approved_generation_review(
            None, thread, job, variant, execution, "The confirmed edit is ready."
        )
    return text


@pytest.mark.parametrize("gate", GATES, ids=[gate.name for gate in GATES])
def test_without_the_degrade_each_row_fails_the_video_as_in_prod(harness, monkeypatch, gate):  # noqa: F811
    """Guard for failure mode 5: each row really is a ``creation_text_failed`` shape."""
    job, _ = _job(harness, monkeypatch, gate)

    def no_degrade(ops, attempt, **_kwargs):
        return Degraded(attempt(ops), ops, [], None)

    def no_shrink(error):
        raise error

    monkeypatch.setattr(composition, "degrade_text_ops", no_degrade)
    monkeypatch.setattr(composition, "shrink", no_shrink)
    failure = Mock(return_value=True)
    monkeypatch.setattr(gb, "_fail_job", failure)

    gb._run_generative_job(str(job.id))

    assert failure.call_args.kwargs["failure_reason"] == "creation_text_failed"


def test_a_bad_row_only_the_creators_own_words_could_fix_still_fails(harness, monkeypatch):  # noqa: F811
    """Failure mode 6: the op that places her literal is never dropped, so the video fails."""
    literal = BriefRequirement(id="place", kind="text", scope="per_clip", literal=GOOD)
    gate = Gate(
        "confirmed",
        lambda snap: [_add(GOOD, 1.0, snap["total_duration_s"] + 5)],
        note=None,
        brief=_brief(literal),
    )
    job, _ = _job(harness, monkeypatch, gate)
    failure = Mock(return_value=True)
    monkeypatch.setattr(gb, "_fail_job", failure)

    gb._run_generative_job(str(job.id))

    assert failure.call_args.kwargs["failure_reason"] == "creation_text_failed"
    assert job.assembly_plan.get("variants") is None


def test_a_degrade_that_would_leave_no_asked_text_fails(harness, monkeypatch):  # noqa: F811
    """A text ask never turns into a video with no text: the composer removed every bar and
    then broke its own explanation, so dropping the broken op leaves nothing on screen."""
    gate = Gate(
        "empty",
        lambda snap: [
            {"op": "remove_texts", "selector": {"ids": [bar["id"] for bar in snap["text_bars"]]}},
        ],
        note=None,
        brief=CreativeBrief(version=1, requirements=[_EXPLAIN]),
        unmet=({"request": "explain", "reason": "nothing to explain"},),
    )
    job, _ = _job(harness, monkeypatch, gate)
    failure = Mock(return_value=True)
    monkeypatch.setattr(gb, "_fail_job", failure)

    gb._run_generative_job(str(job.id))

    assert failure.call_args.kwargs["failure_reason"] == "creation_text_failed"


def test_a_composer_bundle_rejected_as_a_whole_still_fails(harness, monkeypatch):  # noqa: F811
    """Failure mode 10: over the op limit the composer's whole answer is refused (no op is
    left), so nothing of the text pass is one bad item and the video fails as before."""
    gate = Gate(
        "bundle",
        lambda snap: [_add(f"Place {n}", 0.5, 1.5) for n in range(60)],
        note=None,
        unmet=({"request": "name each place", "reason": "Too many places"},),
    )
    job, _ = _job(harness, monkeypatch, gate)
    failure = Mock(return_value=True)
    monkeypatch.setattr(gb, "_fail_job", failure)

    gb._run_generative_job(str(job.id))

    assert failure.call_args.kwargs["failure_reason"] == "creation_text_failed"
    assert job.assembly_plan.get("variants") is None
