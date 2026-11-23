"""KRI-1595: research captions on a draft the creator edited on the phone.

Incident: prod thread 8c39b4d2 / job de56abfb (2026-11-20). A 7-clip Kyoto train guided
story, reordered in the phone editor. "Add informative captions about the Toroko train" was
refused six times with "A clip has no usable output window". The phone-saved timeline keeps
each clip's in-point, length and transition but no output start or end; the editor snapshot
copies those as null, and the research step read a null as a number.

Journey (server chain, model calls replaced by recorded-shape outputs, no network): the
edited variant -> ``build_editor_snapshot`` -> ``run_copilot_turn`` (research route) ->
``run_researched_revision`` -> a cited ``add_text`` on the clip it explains.

Failure modes written before the test:
1. Null output start/end on every phone-saved slot -> refusal (the incident).
2. Derived times ignore the crossfade overlap -> the explanation drifts late, over the next
   clip (other footage under a sourced claim).
3. A cut is treated as a crossfade, or the wrong neighbour's transition is used.
4. A removed (tombstoned) clip still counts on the clock -> later clips shift.
5. A removed FIRST clip is taken as the opening -> refusal, or the wrong protected opening.
6. Clips that state a window mixed with clips that do not -> derived ones restart at 0.
7. A clip with no window and no length is guessed (zero, or its natural length) -> wrong
   footage; it must be refused, naming the clip, before any paid search.
8. That refusal mixes languages (a Turkish sentence + an English suffix), or misses German.
9. The refusal log leaks content (media ids, text) instead of key names and value types.
10. A draft whose clips already state windows (unedited guided story, split screen) changes.
11. Garbage from an untrusted client snapshot (text, NaN, a flag) crashes the turn.
12. A job stamped with an informative text design (KRI-1580) reads its hook and per-clip
    windows from the raw slots, so the same request on the same draft is refused again.

Independent expectation: ``tests/fixtures/kri1595_edited_draft/prod_timeline.json``, the prod
timeline as stored and its clock worked out by hand, whose last end is the job's recorded
``edit_duration_s`` (34.866666, the renderer's own total for this exact timeline).
"""

from __future__ import annotations

import asyncio
import copy
import json
import uuid
from pathlib import Path
from types import SimpleNamespace

import pytest
from structlog.testing import capture_logs

from app.agents.edit_copilot import EditCopilotAgent
from app.config import settings
from app.kria.reply_language import reply_language_for
from app.routes._copilot import CopilotTurnBody, run_copilot_turn
from app.services.kria_editor_ops import build_editor_snapshot
from app.services.researched_text import (
    ClipTimingMissing,
    GroundedResearchAgent,
    ResearchCopyAgent,
    ResearchReviewAgent,
    ResearchReviewOutput,
    design_opening,
    design_windows,
    evidence_from_response,
    research_windows,
)

REQUEST = "Kyoto'daki Toroko treni hakkında bilgilendirici alt yazı ekle"
QUOTE = "Toroko treni hakkında bilgilendirici alt yazı"
SOURCE = "https://www.sagano-kanko.co.jp/en/"
FACT = (
    "The Sagano Scenic Railway runs open sightseeing carriages along the Hozu River "
    "between Saga and Kameoka."
)
# Prod job de56abfb's stored user_timeline after the creator's reorder, and the clock worked
# out by hand from it (``expected_clock``): see the fixture's notes.
PROD = json.loads(
    (
        Path(__file__).resolve().parents[2] / "fixtures/kri1595_edited_draft/prod_timeline.json"
    ).read_text(encoding="utf-8")
)
CLOCK = {media: tuple(span) for media, span in PROD["expected_clock"]["windows"].items()}
RECORDED_EDIT_DURATION_S = PROD["recorded"]["edit_duration_s"]


def edited_job():
    variant = {
        "variant_id": "guided_story",
        "render_status": "ready",
        "render_generation_id": "gen-1",
        "editor_timeline_mode": "authored",
        "text_elements": [],
        **PROD["recorded"],
        "user_timeline": copy.deepcopy(PROD["user_timeline"]),
    }
    job = SimpleNamespace(
        id=uuid.uuid4(),
        user_id=uuid.uuid4(),
        status="variants_ready",
        current_phase=None,
        mode="content_plan",
        all_candidates={"clip_paths": [f"proxy-{i}.mp4" for i in range(7)]},
        assembly_plan={"variants": [variant]},
    )
    return job, variant


def snapshot_of(job, variant) -> dict:
    return build_editor_snapshot(job, variant, clip_context={"seen": PROD["seen"]})


def _grounding():
    return SimpleNamespace(
        text=FACT,
        candidates=[
            SimpleNamespace(
                grounding_metadata=SimpleNamespace(
                    web_search_queries=["Sagano Scenic Railway Toroko train"],
                    grounding_chunks=[
                        SimpleNamespace(
                            web=SimpleNamespace(uri=SOURCE, title="Sagano Scenic Railway")
                        )
                    ],
                    grounding_supports=[
                        SimpleNamespace(
                            segment=SimpleNamespace(text=FACT), grounding_chunk_indices=[0]
                        )
                    ],
                    search_entry_point=SimpleNamespace(rendered_content="<div>Search</div>"),
                )
            )
        ],
    )


def spans(windows: list[dict]) -> dict[tuple[float, float], list[str]]:
    """Research windows by their output span (to the microsecond), with the clips under each."""
    return {(round(r["start_s"], 6), round(r["end_s"], 6)): r["media_ids"] for r in windows}


def window_on(windows: list[dict], media_id: str) -> dict:
    """The window spanning exactly ``media_id``'s place on the hand-written clock."""
    start, end = CLOCK[media_id]
    return next(
        r for r in windows if abs(r["start_s"] - start) < 1e-6 and abs(r["end_s"] - end) < 1e-6
    )


# Each clip's window, and the clips pictured in it: a crossfade's 0.12 s dissolve shows the
# neighbour too (the rule for windows a draft states, unchanged here).
WINDOWS = {
    CLOCK["friends"]: ["friends", "wave"],
    CLOCK["wave"]: ["friends", "wave"],
    CLOCK["arrival"]: ["arrival", "platform"],
    CLOCK["platform"]: ["arrival", "platform", "map"],
    CLOCK["map"]: ["platform", "map", "ticket"],
    CLOCK["ticket"]: ["map", "ticket", "valley"],
    CLOCK["valley"]: ["ticket", "valley"],
}


def stub_models(monkeypatch, *, on="valley"):
    """Route to research, search once, write the train caption on clip ``on``'s window."""
    calls: list[tuple] = []

    def route(self, input, *, ctx=None):
        return self.parse(
            json.dumps(
                {"research_request": {"topic": "Kyoto Toroko train", "request_quote": QUOTE}}
            ),
            input,
        )

    def search(self, input, *, ctx=None):
        calls.append(("search", input.topic))
        return evidence_from_response(_grounding())

    def write(self, input, *, ctx=None):
        windows = input.snapshot["research_windows"]
        calls.append(("copy", copy.deepcopy(windows), copy.deepcopy(input.snapshot["slots"])))
        window = window_on(windows, on)
        return self.parse(
            json.dumps(
                {
                    "captions": [
                        {
                            "window_id": window["id"],
                            "text": "Sagano Scenic Railway: open carriages along the Hozu River",
                            "context_label": "Sagano Scenic Railway",
                            "media_ids": window["media_ids"],
                            "evidence_ids": ["fact-0"],
                            "visual_quote": "open sightseeing carriage",
                            "scope": "context",
                            "subject_name": "",
                            "identity_quote": "",
                        }
                    ],
                    "limitations": "",
                    "unmet_requests": [],
                }
            ),
            input,
        )

    monkeypatch.setattr(EditCopilotAgent, "run", route)
    monkeypatch.setattr(GroundedResearchAgent, "run", search)
    monkeypatch.setattr(ResearchCopyAgent, "run", write)
    monkeypatch.setattr(
        ResearchReviewAgent,
        "run",
        lambda *a, **k: ResearchReviewOutput(approved=True, reason="Supported by the operator"),
    )
    return calls


def turn(snapshot, text_design=None):
    return asyncio.run(
        run_copilot_turn(
            CopilotTurnBody(message=REQUEST, snapshot=snapshot, client_contract_version=2),
            job_id=uuid.uuid4(),
            text_design=text_design,
        )
    )


def test_edited_phone_draft_gets_sourced_captions_on_the_renderer_clock(monkeypatch):
    """Failure modes 1-3: the incident request on the incident timeline."""
    job, variant = edited_job()
    snapshot = snapshot_of(job, variant)
    # The incident shape: every slot is in the snapshot with null output times.
    assert [slot["output_start_s"] for slot in snapshot["slots"]] == [None] * 7
    calls = stub_models(monkeypatch)

    result = turn(snapshot)

    assert result.outcome == "proposed", result.reply
    assert "output window" not in result.reply
    [op] = result.ops
    assert op["op"] == "add_text"
    assert op["research"]["sources"][0]["url"] == SOURCE
    assert op["research"]["media_ids"] == ["ticket", "valley"]
    # On the clip it explains, by the renderer's clock, not the naive sum (26.633333).
    assert (op["start_s"], op["end_s"]) == pytest.approx(CLOCK["valley"])
    assert op["end_s"] == pytest.approx(RECORDED_EDIT_DURATION_S)
    # The writer saw every clip at its place on that clock, and only those windows.
    [_search, (_copy, windows, bound)] = calls
    placed = {row["media_id"]: (row["output_start_s"], row["output_end_s"]) for row in bound}
    assert placed.keys() == CLOCK.keys()
    for media_id, span in CLOCK.items():
        assert placed[media_id] == pytest.approx(span), media_id
    assert spans(windows) == WINDOWS
    # Nothing but the cited text is touched: the snapshot the editor keeps is unchanged.
    assert all(slot["output_start_s"] is None for slot in snapshot["slots"])


def test_a_claim_bound_to_the_wrong_clip_is_still_refused(monkeypatch):
    """Negative control: same request and wording, but the train claim lands on the map
    clip's window. The clips under that window on the derived clock do not show the carriage,
    so the turn changes nothing."""
    job, variant = edited_job()
    stub_models(monkeypatch, on="map")

    result = turn(snapshot_of(job, variant))

    assert result.ops == []
    assert result.outcome == "clarification"
    assert result.reply.startswith("The pictured subject could not be confirmed")


def test_removed_and_stated_clips_keep_the_clock(monkeypatch):
    """Failure modes 4-6 and 10: a tombstoned first clip, a clip that states its window, and
    an unedited guided draft that states every window give the same windows."""
    job, variant = edited_job()
    rows = variant["user_timeline"]["slots"]
    tombstone = {**rows[1], "slot_id": "deleted", "media_id": "deleted", "removed": True}
    rows.insert(0, tombstone)
    rows[3]["output_start_s"], rows[3]["output_end_s"] = CLOCK["arrival"]
    edited = spans(research_windows(snapshot_of(job, variant)))
    assert edited == WINDOWS

    # The unedited draft: the renderer's segments state each window (guided v2 rows).
    for row in rows:
        if not row["removed"]:
            row["output_start_s"], row["output_end_s"] = CLOCK[row["media_id"]]
    assert spans(research_windows(snapshot_of(job, variant))) == edited


@pytest.mark.parametrize(
    "length",
    [None, "8 beats", float("nan"), True, 0],
    ids=["beat-sized", "text", "nan", "flag", "zero"],
)
def test_a_clip_with_no_length_is_named_in_the_creators_language(monkeypatch, length):
    """Failure modes 7-9 and 11: no window and no usable length on clip 4."""
    monkeypatch.setattr(settings, "kria_german_replies_enabled", True, raising=False)
    job, variant = edited_job()
    variant["user_timeline"]["slots"][3]["duration_s"] = length
    variant["user_timeline"]["slots"][3]["duration_beats"] = 8
    calls = stub_models(monkeypatch)

    replies = {}
    for language in ("en", "tr", "de"):
        with reply_language_for(language), capture_logs() as logs:
            result = turn(snapshot_of(job, variant))
        assert result.ops == []
        assert result.outcome == "clarification"
        replies[language] = result.reply
        [event] = [row for row in logs if row["event"] == "research.clip_timing_missing"]
        assert event["clip_number"] == 4
        assert event["slot_count"] == 7
        assert "media_id" in event["slots"][3]["keys"]
        assert event["slots"][3]["timing"]["output_start_s"] == "NoneType"
        # Key names and value types only: no media id, no clip description.
        assert "platform" not in json.dumps(event, default=str)

    assert calls == []  # refused before any paid search
    assert replies["en"] == (
        "I couldn't tell where clip 4 plays in your video, because it has no usable length. "
        "Your video is unchanged. I can still add titles without researched facts."
    )
    assert replies["tr"].startswith("4. klibin videonda nerede oynadığını anlayamadım")
    assert "unchanged" not in replies["tr"]
    assert replies["de"].startswith("Ich konnte nicht erkennen, wo Clip 4")
    assert "unchanged" not in replies["de"]


def test_timing_refusal_reads_in_english_for_other_callers():
    """The render-time composer wraps ``str(exc)``; it stays an English reason."""
    exc = ClipTimingMissing(2)
    assert str(exc) == "Clip 2 has no usable length, so its place in the video is unknown"


@pytest.mark.parametrize("text_design", ["A1", "A2", "A3", "B1"])
def test_every_design_reads_the_same_derived_clock(text_design):
    """Failure mode 12: a design's hook and windows sit on the hand-written clock."""
    job, variant = edited_job()
    snapshot = snapshot_of(job, variant)

    hook, facts_start = design_opening(snapshot)
    windows = design_windows(snapshot, text_design)

    assert (hook["start_s"], hook["end_s"], hook["media_ids"]) == (0.0, 3.4, ["friends"])
    assert windows and windows[0]["start_s"] >= facts_start
    assert windows[-1]["end_s"] == pytest.approx(RECORDED_EDIT_DURATION_S, abs=1e-3)
    for window in windows:
        assert window["media_ids"], window
        for media_id in window["media_ids"]:
            start, end = CLOCK[media_id]
            assert window["start_s"] < end and window["end_s"] > start, (media_id, window)
    for before, after in zip(windows, windows[1:], strict=False):
        assert before["end_s"] <= after["start_s"]


def test_a_design_job_gets_its_hook_and_titled_fact_on_the_edited_draft(monkeypatch):
    """Failure mode 12, whole turn: design A1 on the incident draft, hook plus one fact."""
    job, variant = edited_job()
    calls = stub_models(monkeypatch)

    def write(self, input, *, ctx=None):
        windows = input.snapshot["research_windows"]
        calls.append(("design", input.text_design, copy.deepcopy(windows)))
        # The last window: the valley clip after the ticket clip's cross-fade.
        valley = windows[-1]
        return self.parse(
            json.dumps(
                {
                    "hook": {
                        "text": "This is the Sagano Scenic Railway",
                        "evidence_ids": ["fact-0"],
                    },
                    "captions": [
                        {
                            "window_id": valley["id"],
                            "heading": "Sagano Scenic Railway",
                            "text": "Open carriages run along the Hozu River.",
                            "evidence_ids": ["fact-0"],
                            "visual_quote": "open sightseeing carriage",
                            "scope": "context",
                        }
                    ],
                }
            ),
            input,
        )

    monkeypatch.setattr(ResearchCopyAgent, "run", write)

    result = turn(snapshot_of(job, variant), text_design="A1")

    assert result.outcome == "proposed", result.reply
    assert "output window" not in result.reply
    [_search, (_design, design, _windows)] = calls
    assert design == "A1"
    hook, heading, detail = result.ops
    assert (hook["start_s"], hook["end_s"]) == pytest.approx((0.0, 3.4))
    assert hook["research"]["media_ids"] == ["friends"]
    # The fact runs from the end of the ticket clip's cross-fade to the end of the edit.
    valley = (CLOCK["ticket"][1], CLOCK["valley"][1])
    for row in (heading, detail):
        assert (row["start_s"], row["end_s"]) == pytest.approx(valley, abs=1e-3)
        assert "valley" in row["research"]["media_ids"]
        assert row["research"]["sources"][0]["url"] == SOURCE
