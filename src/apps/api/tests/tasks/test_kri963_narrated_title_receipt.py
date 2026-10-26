"""KRI-963: a finished phone Voiceover video said its title "isn't in this draft".

Don Julio walk-in N1 (thread 99627FBC, job bdb0c798, 2026-10-26): the approved title
"Don Julio, Palermo" was drawn 0-1.66 s (frames prove it, the variant carries it in
`narrated_title_text_elements`), yet the after-render review listed r1 as
partial: "That exact text isn't in this draft." The draft had said met.

Failure modes this change must not have:

- FM1 the render-time narrated alignment record (clip windows, labels, spoken words, caption
  language) judges a text ask it holds no evidence for, and its "partial" pre-empts the
  finished-video review (the bug).
- FM2 the finished-video review (`plan_facts_from_phone_variant`) never reads
  `narrated_title_text_elements`, so even unpre-empted the title still reads as missing.
- FM3 a title the creator removed in the editor (`removed: true`) or a variant with no title
  element is reported met (false Done).
- FM4 a title with other wording than the creator's literal satisfies the ask.
- FM5 a text-at-a-spoken-word ask, whose row shares the same store, is met by the text merely
  existing instead of by its placement receipt (KRI-699/KRI-791 behaviour changes).
- FM6 the record stops judging what it can judge (order, timing, caption language).
"""

from __future__ import annotations

import json
import uuid
from pathlib import Path
from types import SimpleNamespace

import app.tasks.generative_build as gb
from app.kria.brief import BriefRequirement, CreativeBrief
from app.kria.brief_binding import BriefBinding
from app.kria.brief_checks import build_receipts, plan_facts_from_phone_variant
from app.tasks import kria_runtime
from tests.tasks.test_phone_subtitled_narrated_dispatch import _setup_narrated

# The prod variant's own title row, the receipts it got, and what its frames showed.
N1 = json.loads(
    (Path(__file__).parents[1] / "fixtures" / "kri963_donjulio_n1_title.json").read_text("utf-8")
)
TITLE = N1["requirement"]["literal"]

TITLE_REQ = BriefRequirement(**N1["requirement"])
CAPTIONS_REQ = BriefRequirement(
    id="r2",
    kind="style",
    scope="global",
    description="add captions in English",
    facts={"captions": "on", "caption_language": "en"},
)


def _render(monkeypatch, *, requirements, opening_title=TITLE):
    job, snapshot, _session, _bindings = _setup_narrated(monkeypatch)
    binding = BriefBinding.create(
        uuid.uuid4(),
        CreativeBrief(version=2, requirements=list(requirements)),
        media_snapshot={"clip_assignments": []},
    )
    snapshot["creator_brief_binding"] = binding.model_dump(mode="json")
    job.assembly_plan["creator_brief_binding"] = snapshot["creator_brief_binding"]
    candidates = {
        **job.all_candidates,
        "creator_strategy": {"opening_title": opening_title} if opening_title else {},
    }
    gb._run_phone_narrated_job(str(job.id), snapshot, candidates, ownership_epoch=3)
    [variant] = job.assembly_plan["variants"]
    return job, variant, binding


def _review(job, variant, binding):
    thread = SimpleNamespace(id=uuid.UUID(binding.thread_id), creator_id=uuid.uuid4())
    text, payload = kria_runtime._approved_generation_review(
        None, thread, job, variant, SimpleNamespace(result={}), "Ready."
    )
    return text, {row["requirement_id"]: row for row in payload}


def test_the_drawn_title_is_done_on_the_finished_voiceover_video(monkeypatch):
    job, variant, binding = _render(monkeypatch, requirements=[TITLE_REQ, CAPTIONS_REQ])
    assert [r["text"] for r in variant["narrated_title_text_elements"]] == [TITLE]

    # FM1: the alignment record carries no text, so it leaves the title to the review.
    record = job.assembly_plan["narrated_alignment"]
    judged = {r["requirement_id"] for r in record["requirement_receipts"]}
    assert "r1" not in judged
    # FM6: what the record can see is still judged there.
    assert "r2" in judged

    text, by_id = _review(job, variant, binding)
    assert (by_id["r1"]["status"], by_id["r1"]["reason"]) == ("met", None)
    assert "isn't in this draft" not in text


def test_a_voiceover_video_without_its_title_still_says_so(monkeypatch):
    # FM3: no title was drawn, so no title row exists, and the review must not say Done.
    job, variant, binding = _render(monkeypatch, requirements=[TITLE_REQ], opening_title=None)
    assert not variant.get("narrated_title_text_elements")

    _text, by_id = _review(job, variant, binding)
    assert by_id["r1"]["status"] != "met"


def _narrated_variant(**title_row):
    [row] = N1["variant_title_rows"]
    return {
        "resolved_archetype": "narrated",
        "narrated_title_text_elements": [{**row, **title_row}],
    }


def _title_status(variant):
    [receipt] = build_receipts(
        [TITLE_REQ], plan_facts_from_phone_variant(variant), include_unchecked=True
    )
    return receipt.status


def test_a_removed_title_row_is_not_on_screen():
    # FM2: the prod row, whose frames show the title, is on screen; prod said it was not.
    assert all(frame["title_on_screen"] for frame in N1["title_frames"])
    assert N1["observed"]["review_receipt"] == "That exact text isn't in this draft."
    assert _title_status(_narrated_variant()) == "met"
    # FM3: the editor's delete keeps the row, marked removed.
    assert _title_status(_narrated_variant(removed=True)) != "met"


def test_a_reworded_title_does_not_satisfy_the_literal():
    # FM4
    assert _title_status(_narrated_variant(text="Parrilla Don Julio")) != "met"
