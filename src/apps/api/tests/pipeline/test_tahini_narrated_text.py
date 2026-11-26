"""Tahini run (2026-11-17, phone Voiceover): short caption blocks and a readable opening title.

The creator asked for "White captions, a few words at a time, following my voice" and the
title "Roasted carrots, crispy topping". The phone narrated render showed whole sentences (up
to 14 words, 4 lines, 6 s on screen), "2." and "3." as captions of their own, and a mid-grey
(#767676) title in the middle of the food that was gone after 1.56 s.

Replays the run's own timed words (fixture `tahini_narrated_text.json`, from the job's
persisted caption cues) through the worker's cue sequence and the real narrated compile
(`compile_phone_narrated_plan`, phone text styling, caption and title legibility), over grey
frames sampled from the kit's own clips. The worker hooks are driven end to end by
`_run_phone_narrated_job` in the last two tests.

Written before the code. Ways it could go wrong, and the test for each:

* F1 a whole sentence stays one caption (more than 8 words / 40 characters / 3 s):
  `test_every_caption_block_fits_the_phone_budget`.
* F2 a spoken list's number shows alone ("2.", "3."):
  `test_a_list_number_joins_the_words_it_numbers`.
* F3 a number in the middle of a sentence ("Bu 3. gün", the Turkish ordinal) is joined or split
  differently than before: `test_a_mid_sentence_number_is_unchanged`.
* F4 the blocks drop, repeat or reorder words, leave a caption-free gap inside a sentence or
  overlap: `test_blocks_keep_every_word_and_tile_each_sentence`.
* F5 a name the creator spelled is capitalised when a block starts with it ("the Ojo de
  bife"): the blocks are cut after the spelling pass, so the pass reads whole sentences
  (`tests/tasks/test_kri961_narrated_named_phrase.py::
  test_captions_card_and_receipts_all_hear_ojo_de_bife`).
* F6 the title stays the automatic mid grey, or white with nothing to hold it on the light
  plate: `test_the_title_is_white_with_a_dark_outline_on_the_light_plate`.
* F7 a title colour the creator set is replaced by white:
  `test_a_title_colour_she_set_is_kept`.
* F8 the title drops to the middle although the top band is clear:
  `test_the_title_keeps_the_top_band`.
* F9 the title takes the top band over a face: `test_a_face_in_the_top_band_moves_the_title`.
* F10 the captions are drawn on the title while it is up:
  `test_the_captions_keep_off_the_title_while_it_is_up`.
* F11 the title is gone before it can be read (1.56 s), or stays past its moment:
  `test_the_title_holds_to_the_first_sentence_within_2_5_to_3_s`,
  `test_the_hold_follows_the_first_sentence`.
* F12 a switch off still changes something (red control): with both switches off the cues are
  exactly the run's and the title is the run's grey 1.56 s one:
  `test_switches_off_reproduce_the_run`.
* F13 the worker does not use the new path (or uses it with the switch off):
  `test_the_worker_cuts_blocks_and_holds_the_title`,
  `test_the_worker_with_the_switches_off_is_the_run`.
"""

from __future__ import annotations

import base64
import json
import zlib
from pathlib import Path
from types import SimpleNamespace

import pytest

from app.config import settings
from app.kria.media_sources import OriginalMediaDescriptor
from app.kria.render_assets import RenderFingerprint
from app.pipeline.captions import (
    PHONE_CUE_BUDGET,
    build_plain_cues,
    caption_display_width,
    resplit_cues_into_sentences,
)
from app.pipeline.phone_narrated_plan import (
    NarratedPhoneStep,
    compile_phone_narrated_plan,
    narrated_caption_blocks,
    narrated_cue_blocks,
    narrated_title_end_s,
    narrated_title_hold_end_s,
    narrated_title_text_elements,
)
from app.pipeline.phone_recipe_shared import PhoneNarrationBed
from app.pipeline.phone_text_style import _overlap
from app.pipeline.transcribe import Word
from app.services import phone_text_styling
from app.services.phone_sources import PhoneSourceBinding
from app.services.phone_text_styling import (
    generation_caption_cues,
    generation_style_fields,
    phone_text_policy_scope,
)

RUN = json.loads(
    (Path(__file__).parents[1] / "fixtures" / "tahini_narrated_text.json").read_text("utf-8")
)
WORDS = [
    Word(text=w["text"], start_s=w["start_s"], end_s=w["end_s"], confidence=1.0)
    for cue in RUN["prod_caption_cues"]
    for w in cue["words"]
]
PROD_TITLE = RUN["prod_title"]


@pytest.fixture(autouse=True)
def _switches_on(monkeypatch):
    # Their defaults; the red controls turn both off.
    monkeypatch.setattr(settings, "narrated_caption_blocks_enabled", True)
    monkeypatch.setattr(settings, "narrated_title_legibility_enabled", True)


def _switches(monkeypatch, on: bool) -> None:
    monkeypatch.setattr(settings, "narrated_caption_blocks_enabled", on)
    monkeypatch.setattr(settings, "narrated_title_legibility_enabled", on)


def _frames() -> list[dict]:
    frames = []
    for frame in RUN["frames"]:
        half = zlib.decompress(base64.b64decode(frame["grey_54x96"]))
        full = bytes(half[(y // 2) * 54 + x // 2] for y in range(192) for x in range(108))
        frames.append({"start_s": frame["start_s"], "end_s": frame["end_s"], "pixels": full})
    return frames


def _worker_cues(words=WORDS) -> list[dict]:
    """The phone narrated writer's cue sequence (`_run_phone_narrated_job`), with no caption
    correction and no spellings asked (the run changed none)."""
    cues = build_plain_cues(words, attach_words=True, **narrated_cue_blocks())
    cues = resplit_cues_into_sentences(cues)
    return narrated_caption_blocks(cues)


def _title_end_s() -> float:
    # The worker's choice (`_run_phone_narrated_job`).
    if settings.narrated_title_legibility_enabled:
        return narrated_title_hold_end_s(WORDS)
    return narrated_title_end_s(WORDS[0].end_s)


def _compile(monkeypatch, *, faces=(), requested=None):
    # The run's styling switches (all on in prod).
    monkeypatch.setattr(settings, "phone_text_styling_enabled", True)
    monkeypatch.setattr(settings, "caption_legibility_enabled", True)
    monkeypatch.setattr(settings, "title_legibility_enabled", True)
    steps = [
        NarratedPhoneStep(
            step_id=f"seg_{i}",
            media_id=f"c{i}",
            source_start_s=0.0,
            start_s=step["start_s"],
            end_s=step["end_s"],
        )
        for i, step in enumerate(RUN["steps"])
    ]
    bindings = tuple(
        PhoneSourceBinding(
            media_id=f"c{i}",
            proxy_path=f"user/analysis-proxy-c{i}.mp4",
            generation="1",
            original=OriginalMediaDescriptor(
                sha256="a" * 64,
                byte_count=1000,
                duration_s=10.0,
                width=1080,
                height=1920,
                has_audio=True,
            ),
        )
        for i in range(len(steps))
    )
    narration = PhoneNarrationBed(
        plan_item_id="item-1",
        generation="9",
        fingerprint=RenderFingerprint(sha256="d" * 64, byte_count=999),
        duration_s=RUN["voiceover_duration_s"],
    )
    # The styling environment as the worker samples it (faces, frames), no font model call.
    policy = {
        "version": 1,
        "preferences": {"all_text": {}},
        "requested": requested or {},
        "selected_fonts": {},
        "selection_calls": 2,
        "_environment_sampled": True,
        "_face_boxes": list(faces),
        "_background_frames": _frames(),
    }
    end_s = _title_end_s()
    with phone_text_policy_scope(policy):
        cues = generation_caption_cues(_worker_cues(), language="en")
        recipe = compile_phone_narrated_plan(
            steps,
            bindings,
            narration,
            voiceover_duration_s=RUN["voiceover_duration_s"],
            caption_cues=cues,
            opening_title=RUN["title"],
            opening_title_end_s=end_s,
        )
        [title] = narrated_title_text_elements(recipe, RUN["title"], end_s=end_s)
        fields = generation_style_fields()
        live = phone_text_styling._POLICY.get()
    return SimpleNamespace(cues=cues, recipe=recipe, title=title, fields=fields, policy=live)


# --- captions -------------------------------------------------------------------------------


def test_every_caption_block_fits_the_phone_budget():
    cues = _worker_cues()

    assert max(len(c["text"].split()) for c in cues) <= PHONE_CUE_BUDGET.max_words
    assert all(
        caption_display_width(c["text"]) <= PHONE_CUE_BUDGET.max_chars
        or len(c["text"].split()) == 1
        for c in cues
    )
    assert all(c["end_s"] - c["start_s"] <= PHONE_CUE_BUDGET.max_s + 1.0 for c in cues)
    # The run: 13 sentence cues, the longest 14 words over 6 s.
    assert len(cues) > len(RUN["prod_caption_cues"])


def test_a_list_number_joins_the_words_it_numbers():
    texts = [c["text"] for c in _worker_cues()]

    assert "2. The topping." in texts
    assert any(t.startswith("3. Thick yogurt") for t in texts)
    assert not {"2.", "3."} & set(texts)


def test_a_mid_sentence_number_is_unchanged(monkeypatch):
    words = [
        Word(text=t, start_s=i * 0.4, end_s=i * 0.4 + 0.3, confidence=1.0)
        for i, t in enumerate(["Bu", "3.", "gün", "böyle", "geçti."])
    ]
    on = build_plain_cues(words, attach_words=True, join_list_numbers=True)
    off = build_plain_cues(words, attach_words=True)

    assert on == off


def test_blocks_keep_every_word_and_tile_each_sentence():
    cues = _worker_cues()

    assert " ".join(c["text"] for c in cues) == " ".join(
        c["text"] for c in RUN["prod_caption_cues"]
    )
    assert [w for c in cues for w in c["words"]] == [
        w for c in RUN["prod_caption_cues"] for w in c["words"]
    ]
    for before, after in zip(cues, cues[1:], strict=False):
        assert before["end_s"] <= after["start_s"] + 1e-9
        if not before["text"].endswith((".", "!", "?")):
            # Inside a sentence the next block takes over where this one ends: no gap.
            assert before["end_s"] == pytest.approx(after["start_s"])


# --- title ----------------------------------------------------------------------------------


def test_the_title_is_white_with_a_dark_outline_on_the_light_plate(monkeypatch):
    run = _compile(monkeypatch)

    assert PROD_TITLE["color"] == "#767676"  # the run
    assert run.title["color"] == "#FFFFFF"
    assert run.title["stroke_color"] == "#000000"
    assert run.title["stroke_width"] >= 4
    [line] = run.fields["title_legibility"]["lines"]
    assert line["treated"] is True and line["contrast"] < 4.5


def test_a_title_colour_she_set_is_kept(monkeypatch):
    run = _compile(monkeypatch, requested={"title": {"color": "#FFD400"}})

    assert run.title["color"] == "#FFD400"


def test_the_title_keeps_the_top_band(monkeypatch):
    run = _compile(monkeypatch)

    assert run.title["x_frac"] == pytest.approx(0.5)
    assert run.title["alignment"] == "center"
    assert run.title["y_frac"] < 0.3
    [layer] = [layer for layer in run.recipe.text_layers if layer.id.startswith("title-")]
    assert layer.anchor_y / run.recipe.canvas.height < 0.3


def test_a_face_in_the_top_band_moves_the_title(monkeypatch):
    face = {"box": (0.15, 0.05, 0.85, 0.32), "start_s": 0.0, "end_s": 34.0, "kind": "face"}
    run = _compile(monkeypatch, faces=[face])

    [title_box] = run.policy["_title_boxes"]
    assert not _overlap(title_box["box"], face["box"])
    assert run.title["y_frac"] > 0.3


def test_the_captions_keep_off_the_title_while_it_is_up(monkeypatch):
    run = _compile(monkeypatch)

    [title_box] = run.policy["_title_boxes"]
    up = [
        caption
        for caption in run.policy["_caption_boxes"]
        if caption["start_s"] < title_box["end_s"] and title_box["start_s"] < caption["end_s"]
    ]
    assert up
    assert not any(_overlap(caption["box"], title_box["box"]) for caption in up)


def test_the_title_holds_to_the_first_sentence_within_2_5_to_3_s(monkeypatch):
    run = _compile(monkeypatch)

    assert PROD_TITLE["end_s"] == pytest.approx(1.56)  # the run
    # The first sentence ends at 5.76 s: held to the 3 s ceiling.
    assert run.title["end_s"] == pytest.approx(3.0)
    [layer] = [layer for layer in run.recipe.text_layers if layer.id.startswith("title-")]
    assert layer.end == pytest.approx(3.0)


@pytest.mark.parametrize(
    ("spans", "end_s"),
    [
        ([("Hi.", 0.0, 0.4), ("More", 1.0, 1.4)], 2.5),
        ([("A", 0.0, 0.4), ("long", 0.5, 2.0), ("one.", 2.0, 2.7)], 2.7),
        ([("No", 0.0, 0.4), ("end", 0.5, 0.9)], 3.0),
        ([], 3.0),
    ],
)
def test_the_hold_follows_the_first_sentence(spans, end_s):
    words = [Word(text=t, start_s=s, end_s=e, confidence=1.0) for t, s, e in spans]

    assert narrated_title_hold_end_s(words) == pytest.approx(end_s)


# --- red control ----------------------------------------------------------------------------


def test_switches_off_reproduce_the_run(monkeypatch):
    _switches(monkeypatch, on=False)
    run = _compile(monkeypatch)

    assert run.cues == RUN["prod_caption_cues"]
    title = run.title
    assert title["color"] == PROD_TITLE["color"]
    assert (title["start_s"], title["end_s"]) == (
        PROD_TITLE["start_s"],
        pytest.approx(PROD_TITLE["end_s"]),
    )
    assert title["font_family"] == PROD_TITLE["font_family"]
    assert title["stroke_width"] == PROD_TITLE["stroke_width"]
    assert "stroke_color" not in title
    assert "title_legibility" not in run.fields
    assert "_title_boxes" not in run.policy


# --- the worker -----------------------------------------------------------------------------


def _worker(monkeypatch):
    import app.tasks.generative_build as gb
    from tests.tasks.test_phone_subtitled_narrated_dispatch import _setup_narrated

    job, snapshot, _session, _bindings = _setup_narrated(
        monkeypatch,
        spoken_words=list(WORDS),
        voiceover_s=RUN["voiceover_duration_s"],
        clip_duration_s=40.0,  # every clip outlasts its step: no retime
    )
    candidates = {**job.all_candidates, "creator_strategy": {"opening_title": RUN["title"]}}
    gb._run_phone_narrated_job(str(job.id), snapshot, candidates, ownership_epoch=3)
    [variant] = job.assembly_plan["variants"]
    return variant


def test_the_worker_cuts_blocks_and_holds_the_title(monkeypatch):
    variant = _worker(monkeypatch)

    texts = [c["text"] for c in variant["caption_cues"]]
    assert max(len(t.split()) for t in texts) <= PHONE_CUE_BUDGET.max_words
    assert "2. The topping." in texts
    [title] = variant["narrated_title_text_elements"]
    assert title["end_s"] == pytest.approx(3.0)


def test_the_worker_with_the_switches_off_is_the_run(monkeypatch):
    _switches(monkeypatch, on=False)
    variant = _worker(monkeypatch)

    assert [c["text"] for c in variant["caption_cues"]] == [
        c["text"] for c in RUN["prod_caption_cues"]
    ]
    [title] = variant["narrated_title_text_elements"]
    assert title["end_s"] == pytest.approx(PROD_TITLE["end_s"])
