"""Qendresa item 19 (Q1): a spoken list on a full-frame talking-head take becomes numbered labels.

Her V7: she talks to camera (no background, no speaker frame) and says "When I list the four
things, show each one as big text, like "1. which company it sends data to?"". The approved brief
holds ONE global `text` requirement with that literal and NO spoken trigger (`facts={}`). She then
lists the four things in one fast breath. Behind `SPOKEN_LIST_LABELS_ENABLED` the worker finds the
list in the take's timed words and draws one big text row per item, numbered in order, each when
she starts saying it, each replaced by the next.

Written before the code. The numbers map to `docs/reviews/qendresa-list-labels-failure-modes.md`
(Q1 list): 21 no list found, 22 false list, 23 wrong boundaries, 24 wrong count, 25 item text,
26 cards/captions/face; plus 13 receipts and 14 flag off from the Q2 list. Sibling (Q2 version):
`test_qendresa_list_labels_worker.py`.

No model calls, no network: `_run_generative_job` with synthetic timed words.
"""

from __future__ import annotations

import copy
import dataclasses
import hashlib
import json
import re
import uuid

import pytest

from app.config import settings
from app.kria.brief import BriefRequirement, CreativeBrief
from app.kria.brief_binding import BriefBinding
from app.kria.brief_checks import (
    build_receipts,
    plan_facts_from_phone_variant,
    plan_facts_from_strategy,
)
from app.pipeline.phone_spoken_text import caption_boxes
from app.pipeline.phone_subtitled_plan import _STORY_CANVAS
from app.pipeline.phone_subtitled_title import _measure_title_box
from app.pipeline.transcribe import Word
from app.services.client_protocol import clear_request_context
from app.tasks import generative_build as gb
from tests.tasks.test_kri841_talking_ending_clip import (
    _offline,  # noqa: F401 - autouse fixture: no Gemini, no face sampling, no trace DB
    _recipe,
)
from tests.tasks.test_qendresa_list_labels_worker import FULL_FRAME_FACE, _faces, _flag

TAKE_S = 62.0
STEP = 0.2  # seconds per spoken word

# ------------------------------------------------------------------ her take (synthetic words)

INTRO = (
    "let's build an agent that reads an app before i trust it because every app tells you "
    "what it does and none of them tell you where your data goes"
)
INTRO_NO_WHAT = INTRO.replace("what it does", "a nice story")
FIRST = "so the first thing i do is install ray"
JOB = "then i give it one job for any site i send"
LIST = (
    "tell me which companies it sends data to what it saves on my machine what it reads from "
    "my browser and what runs the moment the page loads that i never asked for and for each "
    "one where in the code i found it"
)
AFTER = (
    "that is the whole idea and it takes one minute to set up so go and try it on the "
    "apps you use every day then tell me how it went"
)
CLOSE = "comment read and i'll send the prompt"

LIST_AT = 18.0
FULL_SENTENCES = [
    (0.0, INTRO),
    (8.0, FIRST),
    (13.0, JOB),
    (LIST_AT, LIST),
    (32.0, AFTER),
    (58.0, CLOSE),
]
# No list: she says the typed item once, then ordinary talk. No second "which" / "what".
ONE_ITEM = (
    "which companies it sends data to is the scary part because nobody reads the policy "
    "and nobody checks the network tab so you simply never find out"
)
NO_LIST_SENTENCES = [
    (0.0, INTRO_NO_WHAT),
    (8.0, FIRST),
    (13.0, JOB),
    (LIST_AT, ONE_ITEM),
    (32.0, AFTER),
    (58.0, CLOSE),
]
# The typed item is never said (and nothing like it).
NEVER_SAID_SENTENCES = [
    (0.0, INTRO_NO_WHAT),
    (8.0, FIRST),
    (13.0, JOB),
    (LIST_AT, "the answer is in the report and the report is in the repository for anyone to read"),
    (32.0, AFTER),
    (58.0, CLOSE),
]


def _words(sentences) -> list[Word]:
    out: list[Word] = []
    for start, sentence in sentences:
        for i, text in enumerate(sentence.split()):
            out.append(
                Word(
                    text=text,
                    start_s=round(start + i * STEP, 3),
                    end_s=round(start + (i + 1) * STEP - 0.03, 3),
                    confidence=1.0,
                )
            )
    return out


def _at(sentence_start: float, sentence: str, word: str, nth: int = 1) -> float:
    """Start time of the ``nth`` occurrence of ``word`` in the sentence (from the word list)."""
    seen = 0
    for i, w in enumerate(sentence.split()):
        if w == word:
            seen += 1
            if seen == nth:
                return round(sentence_start + i * STEP, 3)
    raise AssertionError((word, nth))


# Where each item is first said. "what" is also said earlier ("tells you what it does"): the
# list must start at "which".
ITEM_AT = [
    _at(LIST_AT, LIST, "which"),
    _at(LIST_AT, LIST, "what", 1),
    _at(LIST_AT, LIST, "what", 2),
    _at(LIST_AT, LIST, "what", 3),
]
# Her typed example (item 1) and the items she only SPEAKS (2-4), in her example's style.
TYPED = "1. which company it sends data to?"
EXPECTED = [
    TYPED,
    "2. what it saves on my machine?",
    "3. what it reads from my browser?",  # not "...browser and?"
    "4. what runs the moment the page loads?",
]
TYPED_2 = "2. what it saves on my machine?"


def _req(rid: str, text: str) -> BriefRequirement:
    return BriefRequirement(
        id=rid,
        kind="text",
        scope="global",
        description=f'Label "{text}"',
        literal=text,
        facts={},
    )


TYPED_REQ = _req("l1", TYPED)
TYPED_REQ_2 = _req("l2", TYPED_2)


def _job(monkeypatch, sentences, requirements):
    from tests.tasks.test_phone_subtitled_narrated_dispatch import _enable_beats, _setup_subtitled

    job, snapshot, _session, _binding = _setup_subtitled(
        monkeypatch, transcript_words=_words(sentences), duration_s=TAKE_S
    )
    _enable_beats(monkeypatch, extra_features=("authoredText",))
    clear_request_context()
    monkeypatch.setattr(
        gb.settings,
        "phone_render_verified_features",
        [*gb.settings.phone_render_verified_features, "positionedText", "animatedText"],
    )
    # Full-frame talking head: no background, no speaker frame, no opening title.
    job.all_candidates["creator_strategy"] = {
        "edit_format": "subtitled",
        "audio_strategy": "original_audio",
    }
    brief = CreativeBrief(version=1, requirements=list(requirements))
    binding = BriefBinding.create(uuid.uuid4(), brief).model_dump(mode="json")
    snapshot["creator_brief_binding"] = binding
    job.assembly_plan["creator_brief_binding"] = copy.deepcopy(binding)
    _faces(monkeypatch)
    return job


def _run(monkeypatch, *, flag: bool, sentences=FULL_SENTENCES, requirements=(TYPED_REQ,)):
    _flag(monkeypatch, flag)
    job = _job(monkeypatch, sentences, requirements)
    gb._run_generative_job(str(job.id))
    assert job.status == "awaiting_device", (job.status, job.error_detail)
    return job, job.assembly_plan["variants"][0]


def _label_rows(variant: dict) -> list[dict]:
    rows = [r for r in variant.get("text_elements") or [] if r["id"].startswith("list-label-")]
    return sorted(rows, key=lambda r: r["start_s"])


def _squash(text: str) -> str:
    return re.sub(r"\s+", " ", text).strip()


def _after(variant: dict, req: BriefRequirement):
    [receipt] = build_receipts(
        [req], plan_facts_from_phone_variant(variant), include_unchecked=True
    )
    return receipt


@pytest.fixture
def on(monkeypatch):
    return _run(monkeypatch, flag=True)


# ------------------------------------------------------------------ flag on


def test_01_four_numbered_labels_from_the_spoken_list(on):
    """FM 21, 23, 24, 25: four rows, her typed text first, then her spoken items numbered on,
    each starting when she starts that item (the first at "which", not at the early "what")."""
    _job, variant = on
    rows = _label_rows(variant)
    assert len(rows) == 4, [r["text"] for r in variant.get("text_elements") or []]
    assert rows[0]["text"] == TYPED
    assert [_squash(r["text"]) for r in rows[1:]] == EXPECTED[1:]
    for row, said in zip(rows, ITEM_AT, strict=True):
        assert abs(row["start_s"] - said) <= 0.3, (row["text"], row["start_s"], said)
    assert all(r["id"].startswith("list-label-") for r in rows)


def test_02_one_at_a_time_in_order_last_one_stays_about_3s(on):
    """FM 23: each ends exactly where the next starts; the last is shown 1.5-3.5 s; no overlap."""
    _job, variant = on
    rows = _label_rows(variant)
    assert len(rows) == 4
    for row, nxt in zip(rows, rows[1:], strict=False):
        assert row["end_s"] == pytest.approx(nxt["start_s"], abs=1e-6), "replaced by the next"
        assert row["end_s"] <= nxt["start_s"] + 1e-6, "two at once"
    last = rows[-1]
    assert 1.5 <= last["end_s"] - last["start_s"] <= 3.5, last
    for row in rows:
        assert 0.0 <= row["start_s"] < row["end_s"] <= TAKE_S + 1e-6


def test_03_the_device_recipe_draws_the_labels(on):
    """FM 16: ordinary text layers on the compiled recipe."""
    job, variant = on
    recipe = _recipe(job)
    layers = {}
    for layer in recipe.text_layers:
        # A wrapped label has one run per line: join them with a space, as the Q2 sibling does.
        text = _squash(" ".join(run.text for run in layer.runs))
        if text in EXPECTED:
            layers[text] = layer
    assert sorted(layers) == sorted(EXPECTED)
    by_text = {_squash(r["text"]): r for r in _label_rows(variant)}
    for text, layer in layers.items():
        assert layer.start == pytest.approx(by_text[text]["start_s"], abs=0.02)
        assert layer.end == pytest.approx(by_text[text]["end_s"], abs=0.02)


def test_04_labels_stay_off_her_face_and_the_captions(on):
    """FM 26: full-frame face (x 0.35-0.65, y 0.25-0.50) and the caption boxes in the window."""
    _job, variant = on
    rows = _label_rows(variant)
    assert len(rows) == 4
    cues = variant["caption_cues"]
    for row in rows:
        box = _measure_title_box(row, canvas=_STORY_CANVAS)
        assert box.left >= -1e-6 and box.right <= 1.0 + 1e-6, ("off the canvas", row["text"], box)
        assert box.intersection_area(FULL_FRAME_FACE) == 0.0, (row["text"], box)
        window = (row["start_s"], row["end_s"])
        for cap in caption_boxes(
            cues, window=window, caption_style="sentence", canvas=_STORY_CANVAS
        ):
            assert box.intersection_area(cap) == 0.0, (row["text"], box, cap)


def test_05_no_list_in_the_take_invents_nothing(monkeypatch):
    """FM 22: she says the typed item once and then ordinary talk: exactly her one label."""
    _job, variant = _run(monkeypatch, flag=True, sentences=NO_LIST_SENTENCES)
    rows = _label_rows(variant)
    assert [r["text"] for r in rows] == [TYPED], [
        r["text"] for r in variant.get("text_elements") or []
    ]
    said = _at(LIST_AT, ONE_ITEM, "which")
    assert abs(rows[0]["start_s"] - said) <= 0.3
    assert "list_label_placement" in variant


def test_06_typed_item_never_said_places_nothing_and_says_so(monkeypatch):
    """FM 13: no row; a non-placed status in the receipt; the finished-video receipt is not met."""
    _job, variant = _run(monkeypatch, flag=True, sentences=NEVER_SAID_SENTENCES)
    assert not _label_rows(variant)
    receipt = variant["list_label_placement"]
    statuses = [r["status"] for r in receipt["rows"]]
    assert statuses and "placed" not in statuses, receipt
    assert _after(variant, TYPED_REQ).status != "met"


def test_07_a_typed_list_is_not_extended(monkeypatch):
    """Two typed labels ("1." and "2."): each lands where its item is said; nothing is added."""
    _job, variant = _run(monkeypatch, flag=True, requirements=(TYPED_REQ, TYPED_REQ_2))
    rows = _label_rows(variant)
    assert [_squash(r["text"]) for r in rows] == [TYPED, TYPED_2]
    for row, said in zip(rows, ITEM_AT[:2], strict=True):
        assert abs(row["start_s"] - said) <= 0.3, (row["text"], row["start_s"], said)


# ------------------------------------------------------------------ receipts


def test_09_after_render_receipt_for_the_typed_item_is_met(on):
    """FM 13: the finished video does contain her typed text."""
    _job, variant = on
    assert _after(variant, TYPED_REQ).status == "met"


def _draft(monkeypatch, *, flag: bool):
    _flag(monkeypatch, flag)
    monkeypatch.setattr(
        settings, "phone_render_verified_features", ["positionedText", "animatedText"]
    )
    strategy = {"edit_format": "subtitled", "audio_strategy": "original_audio"}
    facts = dataclasses.replace(plan_facts_from_strategy(strategy), phone_talking=True)
    [receipt] = build_receipts([TYPED_REQ], facts, include_unchecked=True)
    return receipt


def test_09b_draft_receipt_is_not_done_and_not_missing_with_the_flag_on(monkeypatch):
    """FM 13: before the render: not "met", and no longer "That exact text isn't in this draft".
    Flag off: today's text."""
    off = _draft(monkeypatch, flag=False)
    assert "That exact text isn't in this draft" in (off.reason or ""), (off.status, off.reason)
    on_receipt = _draft(monkeypatch, flag=True)
    assert on_receipt.status != "met"
    assert "isn't in this draft" not in (on_receipt.reason or "")
    again = _draft(monkeypatch, flag=False)
    assert (again.status, again.reason) == (off.status, off.reason)


# ------------------------------------------------------------------ flag off

# Frozen on today's code (clean `git archive HEAD` copy, flag off): printed by `_digests(...)`.
# The whole recipe's digest is not frozen: its text geometry differs between this Mac and CI's
# Linux, so the recipe is compared within one run instead (`test_08`).
TODAY_TEXT_ELEMENTS_SHA = "74234e98afe7498fb5daf1f36ac2d78acc339464f950703b8c019892f982b90b"
TODAY_RECEIPTS_SHA = "de15ad5ec99413755e539046a12a219f49199aa1ace0ed5935ae0ebd6daa51ff"


def _sha(obj) -> str:
    return hashlib.sha256(json.dumps(obj, sort_keys=True, default=str).encode()).hexdigest()


def _digests(job, variant) -> dict[str, str]:
    dumped = _recipe(job).model_dump(mode="json")
    # The required-capability list comes out of a set: its order changes between processes.
    dumped["required_capabilities"] = sorted(dumped["required_capabilities"])
    receipts = [
        (r.status, r.reason)
        for r in build_receipts(
            [TYPED_REQ], plan_facts_from_phone_variant(variant), include_unchecked=True
        )
    ]
    return {
        "recipe": _sha(dumped),
        "text_elements": _sha(variant.get("text_elements")),
        "receipts": _sha(receipts),
    }


def test_08_flag_off_is_byte_identical_to_today(monkeypatch):
    """FM 14: flag off, nothing of item 19 runs, the rows and receipts are today's (frozen), and
    the recipe is the one the same job renders with no label in its brief."""
    from tests.tasks.test_qendresa_list_labels_worker import _item19_never_runs

    _item19_never_runs(monkeypatch)
    job, variant = _run(monkeypatch, flag=False)
    assert not _label_rows(variant)
    assert "list_label_placement" not in variant
    digests = _digests(job, variant)
    assert {key: digests[key] for key in ("text_elements", "receipts")} == {
        "text_elements": TODAY_TEXT_ELEMENTS_SHA,
        "receipts": TODAY_RECEIPTS_SHA,
    }
    plain_job, plain_variant = _run(monkeypatch, flag=False, requirements=())
    assert _digests(plain_job, plain_variant)["recipe"] == digests["recipe"]


# ------------------------------------------------------------------ review round

FIRST_SECOND_LIST = (
    "the first thing is which company it sends data to the second thing is whether it keeps "
    "your recordings the third thing is who can read your chats and the fourth thing is how "
    "long they keep it"
)
FIRST_SECOND_SENTENCES = [
    (0.0, INTRO_NO_WHAT),
    (8.0, FIRST),
    (13.0, JOB),
    (LIST_AT, FIRST_SECOND_LIST),
    (40.0, AFTER),
    (58.0, CLOSE),
]


def test_10_a_first_second_third_take_draws_no_wrong_text_and_the_receipt_is_partial(
    monkeypatch,
):
    """The openers ("the second thing is ...") are not right after item 1: only her typed item
    is placed (never "2. thing is whether it keeps your recordings the third?"), the receipt
    says the list was not found, and the finished-video requirement is not "met"."""
    _job, variant = _run(monkeypatch, flag=True, sentences=FIRST_SECOND_SENTENCES)
    assert [r["text"] for r in _label_rows(variant)] == [TYPED]
    assert variant["list_label_placement"]["rows"][0]["list_found"] == 1
    receipt = _after(variant, TYPED_REQ)
    assert receipt.status == "partial"
    assert "rest of your list" in (receipt.reason or "")
