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

Second review of #2314, written before the fixes:
 12. four or more rows bad the same way: a fixed round cap gives up and the video fails, or
     each dropped row gets its own line and the finished reply fills with near-copies;
 13. the incomplete note never says which part of her ask is missing, though the composer
     named it in her words;
 14. an op the parser refused, which no unmet ask of hers names, is reported as part of her
     text request.

Third review of #2314, written before the fixes:
 15. her literal drawn as stacked lines (prod d6c58d7b: three pinned lines, the composer wrote
     no op and asked which layout she wanted) counts as missing, so her video fails;
 16. the stacked-lines reading counts her literal as shown when a line is missing or the lines
     read out of order;
 17. two left-out story beats are said as one ("Left out one explanation").

Fourth review of #2314, written before the fixes:
 18. a hierarchy ask the composer sized wrong renders with a note that names none of her lines;
 19. a hierarchy ask whose own line the composer never wrote is said as a size miss, so she is
     not told her line was left out of the text pass;
 20. her literal counts as shown when its words start or end inside unrelated lines ("Sabah
     oldu" read across "Günaydın sabah" and "oldu bitti").

Rebase onto the composer's question (KRI-1456, #2357), written before the merge fixes:
 21. a composer that asks her something, or names an unmet ask with its reason, is degraded
     into a video with a drop note, so the question never reaches her and a missing wording is
     dropped instead of asked;
 22. a composer that refuses with nothing to ask (KRI-1456 ``composer_rejects``) renders as
     planned with a generic note instead of failing;
 23. her lines stacked as whole rows count as drawn for a composer that wrote no op, but not
     for one that asks which layout she wants, so the same plan renders or blocks on the
     composer's wording (prod d6c58d7b), or a drop of the asking candidate is said beside the
     video made as planned.

Seventh review of #2314 (prod d4f1b2d8 replayed live), written before the fixes:
 24. a label the composer ties to a clip (``clip_id``) without a window is refused as missing a
     detail, so her two approved labels become a question and no video is made;
 25. a window is guessed for a clip the cut does not play, or plays twice;
 26. a window the composer did write is replaced by its clip's window;
 27. her literal ending a few hundredths of a second past the video (0.02 to 0.04 s past
     20.003 s) fails the whole video, because the op placing it is never dropped;
 28. fitting windows into the video turns a row it cannot show at all (it starts after the end)
     into an empty window the parser refuses, so a droppable row becomes a question;
 29. her title, already on screen as confirmed copy, written again by the composer as a second
     line when she asked only for the title and its style, fails the video because that op is
     protected as her literal; or the only op placing her literal becomes droppable.
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
from tests.tasks.test_unified_montage_dispatch import CLIPS, harness  # noqa: F401

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


def _gone_beats(count: int) -> Callable[[Any], None]:
    """A planner row: after the first beat, ``count`` approved story beats name clips the cut
    does not play."""

    def planner(plan: Any) -> None:
        played = [cut.media_id for cut in plan.snapshot.fast_cuts]
        gone = [
            EditorialBeat(purpose=f"The finish {n}", media_ids=[f"clip-gone-{n}"], text_goal="Why")
            for n in range(count)
        ]
        plan.snapshot = plan.snapshot.model_copy(
            update={
                "editorial_plan": EditorialPlan(
                    viewer_goal="Show the route",
                    beats=[
                        EditorialBeat(purpose="The route", media_ids=played, text_goal="Name it"),
                        *gone,
                    ],
                )
            }
        )

    return planner


# Prod d6c58d7b (2026-11-14): her song lines are one brief literal that Main Creator pinned as
# three stacked lines.
LINES = ("Işıklar söndü İstanbul'da", "Yağmur yağdı sokaklarda", "Sabah oldu yine")
_LYRICS = CreativeBrief(
    version=1,
    requirements=[
        BriefRequirement(id="lyrics", kind="text", scope="global", literal=" ".join(LINES))
    ],
)
# The composer wrote no op and asked which layout she wanted.
_CLARIFY = {"intent": "clarify", "needs_clarification": True, "reply": "Which layout?"}

SUBTITLE = "Fresh air"
_SMALLER = BriefRequirement(
    id="subtitle", kind="text", scope="global", literal=SUBTITLE, facts={"smaller_than": TITLE}
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
    # Main Creator's pinned lines (`creator_strategy.pinned_texts`), bottom centre.
    pinned: tuple[str, ...] = ()
    # Fields of the composer's answer that differ from a plain edit.
    answer: dict = field(default_factory=dict)
    # Fields Main Creator's approved strategy adds (`creator_strategy`).
    strategy: dict = field(default_factory=dict)


def _after_end(text: str) -> Callable[[dict], dict]:
    """A row the video cannot show at all: it starts after the video ends."""
    return lambda snap: _add(text, snap["total_duration_s"] + 1, snap["total_duration_s"] + 5)


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
        lambda snap: [_add(GOOD), _after_end("Past the end")(snap)],
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
        lambda snap: [_add(GOOD), _after_end("Past the end")(snap), _after_end("Also past")(snap)],
        note="Left out one text change for “Past the end” because I couldn't make it fit",
        gone="Also past",
        reasons=("replay_failed", "replay_failed"),
    ),
    Gate(
        "replay_failed_five_times",
        lambda snap: [_add(GOOD), *(_after_end(f"Past {n}")(snap) for n in range(5))],
        note="Left out 5 text changes I couldn't make fit",
        gone="Past 4",
        reasons=("replay_failed",) * 5,
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
    # An unmet ask with no reason and nothing asked: a reason makes it the composer's question.
    Gate(
        "incomplete",
        lambda snap: [_add(GOOD)],
        note="I couldn't do “English word by word”, so the video shows the rest of your text",
        unmet=({"request": "English word by word"},),
    ),
    Gate(
        "incomplete_nothing_written",
        lambda snap: [],
        note="I couldn't do “make the title pop in”, so the video shows the rest of your text",
        kept=TITLE,
        unmet=({"request": "make the title pop in"},),
        reasons=("incomplete",),
        keeps_good=False,
    ),
    # Prod d6c58d7b's edit answer with no op.
    Gate(
        "incomplete_her_lines_stacked",
        lambda snap: [],
        note="I couldn't do every part of your text request, so the video shows the parts I could",
        kept=LINES[0],
        brief=_LYRICS,
        pinned=LINES,
        reasons=("incomplete",),
        keeps_good=False,
    ),
    Gate(
        "hierarchy_unmet",
        lambda snap: [_add(GOOD), {**_add(SUBTITLE), "patch": {"size_px": 400}}],
        note=f"I couldn't size “{SUBTITLE}” exactly as you asked",
        kept=SUBTITLE,
        brief=_brief(_EXPLAIN, _SMALLER),
    ),
    Gate(
        "hierarchy_unmet_line_never_written",
        lambda snap: [
            {
                "op": "patch_text",
                "selector": {"ids": [_title_id(snap)]},
                "patch": {"color": "#FFD400"},
            }
        ],
        note=f"I couldn't add “{SUBTITLE}” the way you asked",
        kept=TITLE,
        brief=_brief(_SMALLER),
        reasons=("hierarchy_unmet",),
        keeps_good=False,
    ),
    Gate(
        "editorial_unmet",
        lambda snap: [_add(GOOD)],
        note="Left out one explanation that didn't fit its part of the video",
        planner=_gone_beats(1),
    ),
    Gate(
        "editorial_unmet_twice",
        lambda snap: [_add(GOOD)],
        note="Left out 2 explanations that didn't fit their part of the video",
        planner=_gone_beats(2),
        reasons=("editorial_unmet", "editorial_unmet"),
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
            **gate.answer,
        }
        return self.parse(json.dumps(reply), input)

    return run


def _job(harness, monkeypatch, gate: Gate):  # noqa: F811
    job, *_ = harness(brief=gate.brief)
    job.all_candidates["creator_request"] = "Title: Morning run. Say where each place is."
    job.assembly_plan["creator_brief_binding"] = BriefBinding.create(THREAD, gate.brief).model_dump(
        mode="json"
    )
    job.all_candidates["creator_strategy"].update(gate.strategy)
    if gate.pinned:
        job.all_candidates["creator_strategy"]["pinned_texts"] = [
            {"text": line, "corner": "bottom_center"} for line in gate.pinned
        ]
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

    def no_shrink(error, **_kwargs):
        raise error

    monkeypatch.setattr(composition, "degrade_text_ops", no_degrade)
    monkeypatch.setattr(composition, "shrink", no_shrink)
    failure = Mock(return_value=True)
    monkeypatch.setattr(gb, "_fail_job", failure)

    gb._run_generative_job(str(job.id))

    assert failure.call_args.kwargs["failure_reason"] == "creation_text_failed"


def test_a_bad_row_only_the_creators_own_words_could_fix_still_fails(harness, monkeypatch):  # noqa: F811
    """Failure modes 6 and 29: the only op that places her literal is never dropped, so a row of
    hers the video cannot show at all still fails the video."""
    literal = BriefRequirement(id="place", kind="text", scope="per_clip", literal=GOOD)
    gate = Gate(
        "confirmed",
        lambda snap: [_after_end(GOOD)(snap)],
        note=None,
        brief=_brief(literal),
    )
    job, _ = _job(harness, monkeypatch, gate)
    failure = Mock(return_value=True)
    monkeypatch.setattr(gb, "_fail_job", failure)

    gb._run_generative_job(str(job.id))

    assert failure.call_args.kwargs["failure_reason"] == "creation_text_failed"
    assert job.assembly_plan.get("variants") is None


# Prod d4f1b2d8 (2026-10-19): her title and a label for each of two clips, each placed by a
# resolved label intent. The harness labels every third clip with no place, as prod had none.
BITE = "Atakan'ın ilk ısırığı"
PLATE_CLIP = "clip-2"
BITE_CLIP = "clip-11"
LAST_CLIP = f"clip-{CLIPS - 1}"


def _slot(snap: dict, media_id: str) -> dict:
    return next(slot for slot in snap["slots"] if slot["media_id"] == media_id)


def _labelled(*labels: tuple[str, str], extra: tuple[BriefRequirement, ...] = ()) -> dict:
    """The Gate fields for her words on named clips: one described shot text per clip, which a
    resolved label intent places on it (prod d4f1b2d8's brief and strategy)."""
    brief = _brief(
        *(
            BriefRequirement(
                id=media, kind="text", scope="per_clip", literal=text, description=f"{media} shot"
            )
            for media, text in labels
        ),
        *extra,
    )
    intents = [
        {
            "op": "label",
            "status": "resolved",
            "intent_id": f"label_{media}",
            "attribute": f"{media} shot",
            "assignments": [{"value": text, "media_id": media, "confidence": 1.0}],
        }
        for media, text in labels
    ]
    return {"brief": brief, "strategy": {"resolved_clip_intents": intents}}


def _label(slot: dict, text: str, **window: float) -> dict:
    """Prod d4f1b2d8's label op: tied to its clip by id, styled like the title."""
    return {
        "op": "add_text",
        "text": text,
        "clip_id": slot["media_id"],
        "style_from": "title",
        "patch": {"position": "bottom"},
        **window,
    }


def _rows(job, text: str) -> list[dict]:
    rows = job.assembly_plan["variants"][0]["text_elements"]
    return [row for row in rows if row["text"] == text]


def _text_recoveries(events) -> list[str]:
    return [
        data["reason"]
        for _stage, event, data in events
        if event == "requirement_recovery" and data.get("lane") == "text"
    ]


def _renders(harness, monkeypatch, gate: Gate):  # noqa: F811
    job, contract = _job(harness, monkeypatch, gate)
    events = _events(monkeypatch)
    failure = Mock(return_value=True)
    monkeypatch.setattr(gb, "_fail_job", failure)

    gb._run_generative_job(str(job.id))

    failure.assert_not_called()
    assert job.status == "awaiting_device"
    assert job.assembly_plan.get("request_recovery") is None
    verify_phone_recipe(
        contract, device_status(job, "guided_story").request.recipe, source_audio={"source": True}
    )
    return job, events


def test_her_clip_labels_without_a_window_show_over_their_clips(harness, monkeypatch):  # noqa: F811
    """Failure modes 24 and 26 (prod d4f1b2d8, replayed live): the composer tied her two labels
    to their clips and wrote no window. Each shows over its clip, the one bound it did write is
    kept, and nothing is asked or left out."""
    clips: dict[str, dict] = {}

    def ops(snap: dict) -> list[dict]:
        plate, bite = _slot(snap, PLATE_CLIP), _slot(snap, BITE_CLIP)
        clips.update(plate=plate, bite=bite)
        return [_label(plate, GOOD), _label(bite, BITE, start_s=bite["output_start_s"] + 0.2)]

    gate = Gate("labels", ops, note=None, **_labelled((PLATE_CLIP, GOOD), (BITE_CLIP, BITE)))
    job, events = _renders(harness, monkeypatch, gate)

    [plate] = _rows(job, GOOD)
    [bite] = _rows(job, BITE)
    assert (plate["start_s"], plate["end_s"]) == pytest.approx(
        (clips["plate"]["output_start_s"], clips["plate"]["output_end_s"])
    )
    assert (bite["start_s"], bite["end_s"]) == pytest.approx(
        (clips["bite"]["output_start_s"] + 0.2, clips["bite"]["output_end_s"])
    )
    assert {_squash(GOOD), _squash(BITE)} <= _drawn(job)
    assert _text_recoveries(events) == []


def test_windows_past_the_video_end_show_until_it_ends(harness, monkeypatch):  # noqa: F811
    """Failure mode 27 (prod d4f1b2d8): her literal ends 0.04 s past the video, another line
    5 s past. Both show until the video ends and nothing is left out."""
    total: list[float] = []

    def ops(snap: dict) -> list[dict]:
        total.append(snap["total_duration_s"])
        last = _slot(snap, LAST_CLIP)
        return [
            _add(BITE, last["output_start_s"], total[0] + 0.04),
            _add("Until the end", 2.0, total[0] + 5),
        ]

    gate = Gate("past_end", ops, note=None, **_labelled((LAST_CLIP, BITE), extra=(_EXPLAIN,)))
    job, events = _renders(harness, monkeypatch, gate)

    for text in (BITE, "Until the end"):
        [row] = _rows(job, text)
        assert row["end_s"] == pytest.approx(total[0])
    assert _text_recoveries(events) == []


_YELLOW = BriefRequirement(id="style", kind="style", scope="title", description="Make it yellow")
# Prod 3c312af4's second ask: described, so a line it wanted is said when left out.
_DESCRIBED = BriefRequirement(
    id="r2",
    kind="text_sequence",
    scope="title",
    description="the same but for the english translation at the bottom",
)


@pytest.mark.parametrize("extra", [_YELLOW, _DESCRIBED], ids=["title_and_style", "described"])
def test_her_title_written_again_is_left_out_and_the_video_renders(harness, monkeypatch, extra):  # noqa: F811
    """Failure mode 29: she asked only for her title, and its colour or a described line. The
    composer coloured the title and wrote it again as a second line; the second copy goes
    without a note, since her title still shows, once."""
    gate = Gate(
        "title_again",
        lambda snap: [
            {
                "op": "patch_text",
                "selector": {"ids": [_title_id(snap)]},
                "patch": {"color": "#FFD400"},
            },
            _add(TITLE, 0.0, 2.0),
        ],
        note=None,
        brief=_brief(extra),
    )
    job, events = _renders(harness, monkeypatch, gate)

    assert len(_rows(job, TITLE)) == 1
    assert _squash(TITLE) in _drawn(job)
    assert _text_recoveries(events) == ["unasked_text"]
    variant = job.assembly_plan["variants"][0]
    assert not variant.get("phone_repair_notes")
    assert "Left out" not in _finished_reply(job, variant)


@pytest.mark.parametrize(
    ("asks", "held", "protected"),
    [
        ((("title", TITLE),), True, False),
        ((("per_clip", TITLE),), True, True),
        ((("title", TITLE), ("global", TITLE)), True, True),
        ((("title", TITLE),), False, True),
    ],
    ids=["asked_once_and_shown", "on_every_clip", "asked_twice", "not_shown_yet"],
)
def test_only_a_second_copy_of_her_words_may_be_dropped(asks, held, protected):
    """Failure mode 29, its other side: an op writing her literal is droppable only when
    confirmed rows already show it and she asked for it once, not on every clip."""
    from app.services.creation_text_degrade import literal_ops

    requirements = [
        BriefRequirement(id=f"r{n}", kind="text", scope=scope, literal=literal)
        for n, (scope, literal) in enumerate(asks)
    ]
    literals = literal_ops(requirements, held=lambda _literal: held)

    assert literals.protected(_add(TITLE)) is protected
    assert literals.protected({**_add("Something else"), "research": {"id": "r"}})
    assert not literals.protected(_add("Something else"))


def test_a_label_on_a_clip_played_twice_gets_no_guessed_window():
    """Failure mode 25: a clip the cut plays twice has two windows, so a label tied to it
    without one is refused as before instead of guessing which."""
    from app.agents.edit_copilot import EditCopilotInput

    slots = [
        {
            "slot_id": f"cut-{n}",
            "media_id": media,
            "output_start_s": n * 2.0,
            "output_end_s": n * 2.0 + 2,
        }
        for n, media in enumerate(["clip-a", "clip-b", "clip-a"])
    ]
    snapshot = {
        "allowed_op_families": ["text", "text_timeline"],
        "editor_ops_version": 2,
        "text_bars": [],
        "slots": slots,
        "total_duration_s": 6.0,
    }
    answer = {
        "intent": "edit",
        "confidence": 0.9,
        "reply": "Labelled.",
        "ops": [
            {"op": "add_text", "text": "Twice", "clip_id": "clip-a"},
            {"op": "add_text", "text": "Once", "clip_id": "clip-b"},
        ],
    }
    composer = composition.CreationTextComposer(Mock())

    output = composer.parse(
        json.dumps(answer), EditCopilotInput(utterance="Label them", variant_snapshot=snapshot)
    )

    assert [(op["text"], op["start_s"], op["end_s"]) for op in output.ops] == [("Once", 2.0, 4.0)]
    assert output.unmet_requests


_QUESTIONS = [
    Gate(
        "unmet_with_its_reason",
        lambda snap: [_add(GOOD)],
        note="No text holds the English yet",
        unmet=({"request": "English word by word", "reason": "No text holds the English yet"},),
    ),
    Gate(
        "an_op_the_parser_refused",
        lambda snap: [_add(GOOD), {"op": "remove_text", "selector": {"ids": [_title_id(snap)]}}],
        note="I couldn't change the text",
    ),
    # Failure mode 25: no window is guessed for a clip the cut does not play.
    Gate(
        "a_label_on_a_clip_the_cut_does_not_play",
        lambda snap: [_add(GOOD), {"op": "add_text", "text": "Ghost", "clip_id": "clip-gone"}],
        note="I couldn't change the text",
    ),
    Gate(
        "asks_with_her_line_missing",
        lambda snap: [],
        note="Which layout?",
        brief=_LYRICS,
        pinned=LINES[:2],
        answer=_CLARIFY,
    ),
]


@pytest.mark.parametrize("gate", _QUESTIONS, ids=[gate.name for gate in _QUESTIONS])
def test_a_composer_question_reaches_her_and_is_never_degraded(harness, monkeypatch, gate):  # noqa: F811
    """Failure mode 21: the composer's question (KRI-1456) is saved for her and no video is made
    without what it asks about; nothing is said as a text-pass drop."""
    job, _ = _job(harness, monkeypatch, gate)
    events = _events(monkeypatch)
    failure = Mock(return_value=True)
    monkeypatch.setattr(gb, "_fail_job", failure)

    gb._run_generative_job(str(job.id))

    assert job.assembly_plan.get("variants") is None
    assert failure.call_args.kwargs["failure_reason"] != "creation_text_failed"
    assert gate.note in job.assembly_plan["request_recovery"]["message"]
    assert not [data for _s, event, data in events if event == "requirement_recovery"]


def test_her_stacked_lines_are_made_as_planned_when_the_composer_asks(harness, monkeypatch):  # noqa: F811
    """Failure mode 23 (prod d6c58d7b): her literal reads across her three lines, so a "which
    layout?" answer makes the video as planned, as an answer with no op does, with KRI-1456's
    note and no text-pass drop."""
    gate = Gate(
        "asks",
        lambda snap: [],
        note=None,
        kept=LINES[0],
        brief=_LYRICS,
        pinned=LINES,
        answer=_CLARIFY,
    )
    job, contract = _job(harness, monkeypatch, gate)
    events = _events(monkeypatch)
    failure = Mock(return_value=True)
    monkeypatch.setattr(gb, "_fail_job", failure)

    gb._run_generative_job(str(job.id))

    failure.assert_not_called()
    assert job.status == "awaiting_device"
    verify_phone_recipe(
        contract, device_status(job, "guided_story").request.recipe, source_audio={"source": True}
    )
    assert {_squash(line) for line in LINES} <= _drawn(job)
    notes = job.assembly_plan["variants"][0].get("phone_repair_notes") or []
    assert gb._text_shown_as_planned_note() in notes
    assert not [data for _s, event, data in events if event == "requirement_recovery"]


def test_a_composer_that_refuses_with_nothing_to_ask_still_fails(harness, monkeypatch):  # noqa: F811
    """Failure mode 22 (KRI-1456 ``composer_rejects``): her title is on screen, yet a refusal
    with no question is a failure, not a video with a generic note."""
    gate = Gate(
        "rejects",
        lambda snap: [],
        note=None,
        answer={"intent": "reject", "reply": "I can't do that with the text tool."},
    )
    job, _ = _job(harness, monkeypatch, gate)
    failure = Mock(return_value=True)
    monkeypatch.setattr(gb, "_fail_job", failure)

    gb._run_generative_job(str(job.id))

    assert failure.call_args.kwargs["failure_reason"] == "creation_text_failed"
    assert job.assembly_plan.get("variants") is None
    assert job.assembly_plan.get("request_recovery") is None


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
        unmet=({"request": "explain"},),
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
        unmet=({"request": "name each place"},),
    )
    job, _ = _job(harness, monkeypatch, gate)
    failure = Mock(return_value=True)
    monkeypatch.setattr(gb, "_fail_job", failure)

    gb._run_generative_job(str(job.id))

    assert failure.call_args.kwargs["failure_reason"] == "creation_text_failed"
    assert job.assembly_plan.get("variants") is None


_MORNING = CreativeBrief(
    version=1,
    requirements=[BriefRequirement(id="line", kind="text", scope="global", literal="Sabah oldu")],
)


@pytest.mark.parametrize("answer", [{}, _CLARIFY], ids=["no_op", "asks"])
@pytest.mark.parametrize(
    ("brief", "pinned"),
    [
        (_LYRICS, LINES[:2]),
        (_LYRICS, (LINES[1], LINES[0], LINES[2])),
        (_MORNING, ("Günaydın sabah", "oldu bitti")),
    ],
    ids=["a_line_missing", "lines_out_of_order", "words_inside_unrelated_lines"],
)
def test_her_lines_not_all_shown_in_order_still_fail(harness, monkeypatch, brief, pinned, answer):  # noqa: F811
    """Failure modes 16, 20 and 23: the composer did nothing and her literal does not read word
    for word across whole lines on screen, so no video is made. With no op the pass failed as a
    whole; a composer that asked has its question saved for her (KRI-1456)."""
    gate = Gate("partial", lambda snap: [], note=None, brief=brief, pinned=pinned, answer=answer)
    job, _ = _job(harness, monkeypatch, gate)
    failure = Mock(return_value=True)
    monkeypatch.setattr(gb, "_fail_job", failure)

    gb._run_generative_job(str(job.id))

    assert job.assembly_plan.get("variants") is None
    if answer:
        assert _CLARIFY["reply"] in job.assembly_plan["request_recovery"]["message"]
    else:
        assert failure.call_args.kwargs["failure_reason"] == "creation_text_failed"
