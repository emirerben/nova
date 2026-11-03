"""What the server lets an intent review ask about, and what could ever be repaired.

Failure modes, written before the code (matrix row in parentheses). Each has a test below.

  A requirement is issued that the frames cannot judge (5, 43):
    audio is never issued and gets no alias; removed, superseded and impossible requirements are
    withheld with their reason; more than 12 are capped in priority order (text first).
  A candidate that does nothing, or something unapproved (23, 24, 25):
    nothing is emitted when the draft already carries the creator's exact words (equal, longer,
    or only differently cased); a restore writes the contract text byte for byte, accents and
    case intact; two different exact targets on one element are ambiguous and emit nothing;
    removed or caption or lyric or sequence-fragment text is never a candidate, and a
    pin that IS mapped to the requirement (by its words or by its clip) gets no restore, no fit
    and no move.
  A restore that rewrites text the requirement never covered (23, 24, 25):
    a requirement is covered only by rows that carry the creator's words as whole words, so
    "Wholesale Day" is not about "Sale" and is never mapped; a requirement that one mapped row
    already satisfies gets no restore on any other row; a row found only through the creator's
    words (global scope) is rewritten only when the creator's words are all it holds, so "Day 3
    in Istanbul" is never reduced to "İstanbul" (a model claim there is a question, not a repair);
    a title or clip label the scope itself names may still be restored whole.
  A candidate that touches what the creator protected (23):
    selected or ordered footage on a clip-linked label, an absolute sequence timing, a
    protected region, an explicit timing ask, a placement ask, and preserve_editor_text each
    remove the candidates they cover; preserve_editor_text forbids touching the text at all, so it
    removes the restore as well as the retime and the move.
  A fit that cannot hold (44):
    no rendered duration, text that ends with the video or inside it, or less than a second of
    visible text left, emits nothing; only text that really runs past the end is fitted, and only
    a cut-off claim can stage it; the window never starts earlier or ends later than approved.
  A catalog that drifts (28):
    ids and digest are deterministic, change with the target, and a strategy draft (no editable
    text elements) still issues requirements but no candidate.
"""

# ruff: noqa: F811

import json

import pytest

from app.kria.brief import BriefRequirement, CreativeBrief
from app.kria.brief_binding import BriefBinding
from app.kria.drafts import KriaDraftDocument
from app.kria.intent_review_catalog import FrameRef, build_review_catalog
from app.kria.intent_review_disposition import RepairPreconditions, evaluate_review
from app.services.creator_render_contract import CreatorRenderContract, TextRequirement
from tests.integration.test_intent_review_lifecycle_postgres import (  # noqa: F401
    paid,
    ready_review,
    review_row,
)
from tests.kria.test_render_review_admission_postgres import approved_publication  # noqa: F401

FRAMES = [FrameRef(frame_id="f0", t_ms=500, sha256="a" * 64, w=360, h=640, anchor_ref="text-0")]


def bar(identifier="title", text="Before", start=0.0, end=3.0, **fields):
    return {
        "id": identifier,
        "text": text,
        "role": "generative_intro",
        "start_s": start,
        "end_s": end,
        "position": "middle",
        "removed": False,
        **fields,
    }


def requirement(identifier="r1", kind="text", scope="title", literal="Before", **fields):
    return BriefRequirement(
        id=identifier, kind=kind, scope=scope, literal=literal, description="ask", **fields
    )


def catalog_for(
    rows,
    requirements=(requirement(),),
    *,
    exact=("Before",),
    duration=10.0,
    protected=None,
    kind="editor",
):
    brief = CreativeBrief(version=1, requirements=list(requirements))
    binding = BriefBinding.create("thread", brief, protected_regions=protected)
    draft = (
        KriaDraftDocument(kind="editor", editor_payload={"text_elements": rows})
        if kind == "editor"
        else KriaDraftDocument(kind="strategy", strategy={"opening_title": "Before"})
    )
    contract = CreatorRenderContract(
        generation_id="g1",
        exact_texts=tuple(TextRequirement(role="any", text=text) for text in exact),
    )
    return build_review_catalog(
        {"requirement_generation": "g1"}, draft, contract, binding, FRAMES, duration
    )


def kinds(catalog):
    return sorted(c.kind for c in catalog.candidates)


def note(identifier, text, start=4.0, end=6.0, **fields):
    """A plain on-screen note: not a title, not a caption, and nothing to move or fit."""
    return bar(identifier, text, start, end, role="text", position="custom", **fields)


def claim(catalog, defect="copy_mismatch", tier="repair"):
    """The model says R1 is unmet; what does the server make of it?"""
    reply = {
        "schema_version": 1,
        "findings": [
            {
                "requirement_id": "R1",
                "verdict": "unmet",
                "defect_kind": defect,
                "evidence_frame_ids": ["f0"],
                "confidence": "high",
            }
        ],
    }
    return evaluate_review(
        json.dumps(reply), catalog, tier=tier, repair_preconditions=RepairPreconditions()
    )


# --- the real frozen packet ---------------------------------------------------------------


@pytest.mark.asyncio
async def test_the_real_frozen_packet_issues_its_one_requirement(
    approved_publication, paid, monkeypatch
):
    rid, _, _ = await ready_review(approved_publication, monkeypatch)
    packet = review_row(rid).result["packet"]
    catalog = build_review_catalog(
        packet,
        KriaDraftDocument.model_validate(json.loads(packet["approved_plan_json"])),
        CreatorRenderContract.model_validate_json(packet["contract_json"]),
        BriefBinding.model_validate_json(packet["binding_json"]),
        FRAMES,
        10.0,
    )
    (issued,) = catalog.requirements
    assert (issued.requirement_id, issued.brief_id, issued.kind) == ("R1", "r1", "text")
    assert issued.literal == "Before" and issued.element_refs == ["title"]
    assert issued.draft_matches_authority is True
    assert catalog.withheld == []
    assert all(c.kind != "restore_exact_text" for c in catalog.candidates)


# --- issuance -----------------------------------------------------------------------------


def test_audio_and_declined_requirements_are_withheld_never_issued():
    catalog = catalog_for(
        [bar()],
        [
            requirement("r1"),
            requirement("r2", "audio", "global", None, status="open"),
            requirement("r3", "text", "global", "Old", status="superseded"),
            requirement(
                "r4",
                "text",
                "global",
                "Gone",
                status="superseded",
                facts={"superseded_by": "creator"},
            ),
            requirement("r5", "select", "global", None, status="not_possible"),
        ],
    )
    assert [(r.requirement_id, r.brief_id) for r in catalog.requirements] == [("R1", "r1")]
    assert [(w.brief_id, w.reason) for w in catalog.withheld] == [
        ("r2", "audio"),
        ("r3", "superseded"),
        ("r4", "removed"),
        ("r5", "not_possible"),
    ]


def test_more_than_twelve_requirements_are_capped_with_text_first():
    asks = [requirement(f"s{n}", "style", "global", None) for n in range(10)]
    asks += [requirement(f"t{n}", "text", "global", f"Word{n}") for n in range(4)]
    catalog = catalog_for([bar()], asks)
    assert [r.kind for r in catalog.requirements] == ["text"] * 4 + ["style"] * 8
    assert [r.requirement_id for r in catalog.requirements] == [f"R{n}" for n in range(1, 13)]
    assert [(w.brief_id, w.reason) for w in catalog.withheld] == [
        ("s8", "cap"),
        ("s9", "cap"),
    ]


# --- restore_exact_text -------------------------------------------------------------------


def test_a_misspelt_title_gets_a_restore_of_the_contract_text_byte_for_byte():
    catalog = catalog_for([bar(text="Bef0re")], exact=("Before",))
    (restore,) = [c for c in catalog.candidates if c.kind == "restore_exact_text"]
    assert restore.target == {"text": "Before"} and restore.element_ref == "title"
    assert restore.requirement_ids == ["R1"] and restore.auto is True
    assert restore.defect_kinds == ["copy_mismatch", "text_missing"]
    assert restore.allowed_paths == ["editor_payload.text_elements[0].text"]
    assert catalog.requirements[0].draft_matches_authority is False


def test_the_restore_keeps_accents_and_case_the_creator_confirmed():
    catalog = catalog_for(
        [bar(text="Canakkale")],
        [requirement(literal="Çanakkale")],
        exact=("Çanakkale",),
    )
    (restore,) = [c for c in catalog.candidates if c.kind == "restore_exact_text"]
    assert restore.target == {"text": "Çanakkale"}


@pytest.mark.parametrize(
    "draft_text", ["Before", "before", "BEFORE", "Before — my day", "  Before "]
)
def test_nothing_is_emitted_when_the_draft_already_carries_the_exact_words(draft_text):
    catalog = catalog_for([bar(text=draft_text)])
    assert "restore_exact_text" not in kinds(catalog)
    assert catalog.requirements[0].draft_matches_authority is True


def test_two_different_exact_targets_on_one_element_are_ambiguous():
    catalog = catalog_for(
        [bar(text="Bef0re Aft3r")],
        [requirement("r1", literal="Before"), requirement("r2", literal="After")],
        exact=("Before", "After"),
    )
    assert "restore_exact_text" not in kinds(catalog)


def test_a_literal_with_no_contract_row_is_never_restored():
    catalog = catalog_for([bar(text="Bef0re")], exact=())
    assert "restore_exact_text" not in kinds(catalog)
    assert catalog.requirements[0].draft_matches_authority is False


def test_a_row_that_only_contains_the_words_inside_another_word_is_never_mapped():
    catalog = catalog_for(
        [note("t1", "Wholesale Day")],
        [requirement(scope="global", literal="Sale")],
        exact=("Sale",),
    )
    assert catalog.requirements[0].element_refs == []
    assert catalog.candidates == []


def test_a_requirement_another_row_already_satisfies_rewrites_no_row():
    rows = [note("t1", "Wholesale Day"), note("t2", "Big Sale", start=6.0, end=8.0)]
    catalog = catalog_for(rows, [requirement(scope="global", literal="Sale")], exact=("Sale",))
    assert catalog.requirements[0].element_refs == ["t2"]
    assert catalog.requirements[0].draft_matches_authority is True
    assert catalog.candidates == []
    result = claim(catalog)
    assert result.status == "suggestion" and result.disposition.repair_candidate_ids == []
    assert [(s.candidate_id, s.why) for s in result.disposition.suggestions] == [
        (None, "render_fidelity")
    ]


def test_a_satisfied_title_leaves_a_second_stacked_title_alone():
    rows = [
        bar("title", "Before", start=0.0, end=3.0),
        bar("title-2", "Bef0re and after", start=0.0, end=3.0),
    ]
    catalog = catalog_for(rows)
    assert catalog.requirements[0].element_refs == ["title", "title-2"]
    assert catalog.requirements[0].draft_matches_authority is True
    assert "restore_exact_text" not in kinds(catalog)


def test_a_row_with_other_words_is_never_cut_down_to_the_creators_words():
    asks = [requirement(scope="global", literal="İstanbul")]
    catalog = catalog_for([note("t1", "Day 3 in Istanbul")], asks, exact=("İstanbul",))
    assert catalog.requirements[0].element_refs == ["t1"]
    assert catalog.requirements[0].draft_matches_authority is False
    assert catalog.candidates == []
    for tier in ("review", "repair"):
        asked = claim(catalog, tier=tier)
        assert asked.status == "needs_confirmation", tier
        assert asked.disposition.repair_candidate_ids == []


def test_a_row_holding_only_the_creators_words_is_restored_to_their_exact_spelling():
    asks = [requirement(scope="global", literal="Çanakkale")]
    catalog = catalog_for([note("t1", "Canakkale")], asks, exact=("Çanakkale",))
    (restore,) = catalog.candidates
    assert (restore.kind, restore.target, restore.element_ref) == (
        "restore_exact_text",
        {"text": "Çanakkale"},
        "t1",
    )
    assert claim(catalog).status == "repair_staged"


@pytest.mark.parametrize(
    "rows,asks,exact",
    [
        pytest.param(
            [bar("guided-pinned-0", "Before", start=0.0, end=10.0)],
            [requirement(scope="global")],
            ("Before",),
            id="pin_found_by_its_words",
        ),
        pytest.param(
            [bar("guided-pinned-0", "Before", start=0.0, end=10.0, clip_id="clip-a")],
            [requirement(scope="clip:clip-a")],
            ("Before",),
            id="pin_found_by_its_clip",
        ),
        pytest.param(
            [bar("guided-pinned-0", "Bef0re", start=0.0, end=10.0, clip_id="clip-a")],
            [requirement(scope="clip:clip-a")],
            ("Before",),
            id="misspelt_pin_found_by_its_clip",
        ),
    ],
)
def test_a_pin_the_requirement_is_mapped_to_gets_no_candidate_of_any_kind(rows, asks, exact):
    catalog = catalog_for(rows, asks, exact=exact, duration=10.0)
    assert catalog.requirements[0].element_refs == ["guided-pinned-0"]
    assert catalog.candidates == []


def test_the_same_text_off_the_pin_prefix_is_a_candidate_so_the_pin_guard_is_what_blocks():
    rows = [bar("note-0", "Bef0re", start=0.0, end=12.0, clip_id="clip-a")]
    catalog = catalog_for(rows, [requirement(scope="clip:clip-a")], duration=10.0)
    assert kinds(catalog) == [
        "fit_text_to_duration",
        "move_text_to_safe_anchor",
        "restore_exact_text",
    ]


@pytest.mark.parametrize(
    "fields",
    [
        {"removed": True},
        {"source_params": {"sequence_timing": {"mode": "relative"}}},
        {"source_params": {"sequence_source_id": "seq-1"}},
    ],
)
def test_removed_or_sequence_text_is_never_a_candidate(fields):
    catalog = catalog_for([bar(text="Bef0re", **fields)])
    assert catalog.candidates == []


def test_captions_and_lyric_lines_are_never_mapped_to_a_requirement():
    rows = [
        bar("narration-caption-1", "Before today", role="caption"),
        bar("cue-1", "Before then", source_params={"source": "caption_cue"}),
        bar("lyric-1", "Before sunrise", role="lyric_line"),
        bar("note", "Before tomorrow", role="text", start=4.0, end=6.0),
    ]
    catalog = catalog_for(rows, [requirement(scope="global")])
    assert catalog.requirements[0].element_refs == ["note"]


def test_a_clip_label_is_mapped_by_its_clip_scope():
    rows = [bar("label-1", "Bakery", start=2.0, end=5.0, clip_id="clip-a", role="clip_label")]
    asks = [requirement("r1", "text", "clip:clip-a", "Bakery!")]
    catalog = catalog_for(rows, asks, exact=("Bakery!",))
    assert catalog.requirements[0].element_refs == ["label-1"]
    assert [c.kind for c in catalog.candidates if c.kind == "restore_exact_text"] == [
        "restore_exact_text"
    ]


# --- protections --------------------------------------------------------------------------


def test_selected_footage_protects_text_bound_to_a_clip():
    rows = [bar("label-1", "Bakery", clip_id="clip-a", role="clip_label")]
    asks = [
        requirement("r1", "text", "clip:clip-a", "Bakery!"),
        requirement("r2", "select", "global", None),
    ]
    assert catalog_for(rows, asks, exact=("Bakery!",)).candidates == []


@pytest.mark.parametrize(
    "ask",
    [
        requirement("r9", "style", "global", None, facts={"sequence_timing": {"mode": "absolute"}}),
    ],
)
def test_an_absolute_sequence_timing_removes_every_candidate(ask):
    assert catalog_for([bar(text="Bef0re", end=12.0)], [requirement(), ask]).candidates == []


def test_a_protected_region_removes_every_candidate():
    catalog = catalog_for(
        [bar(text="Bef0re", end=12.0)], protected=[{"clip_id": "c", "box": [0, 0, 1, 1]}]
    )
    assert catalog.candidates == []


def test_preserve_editor_text_removes_every_candidate_including_the_restore():
    rows = [bar(text="Bef0re", end=12.0)]
    assert kinds(catalog_for(rows, [requirement()])) == [
        "fit_text_to_duration",
        "move_text_to_safe_anchor",
        "restore_exact_text",
    ]
    on_the_timing_ask = [
        requirement(),
        requirement("r2", "timing", "title", None, facts={"preserve_editor_text": True}),
    ]
    on_the_text_ask = [requirement(facts={"preserve_editor_text": True})]
    assert catalog_for(rows, on_the_timing_ask).candidates == []
    assert catalog_for(rows, on_the_text_ask).candidates == []


@pytest.mark.parametrize(
    "ask",
    [
        requirement("r2", "timing", "global", None),
        requirement("r2", "timing", "title", None),
        requirement("r1", facts={"duration_s": 4}),
    ],
)
def test_an_explicit_timing_ask_blocks_retime_and_move(ask):
    asks = [ask] if ask.id == "r1" else [requirement(), ask]
    assert kinds(catalog_for([bar(text="Bef0re", end=12.0)], asks)) == ["restore_exact_text"]


def test_a_placement_ask_blocks_the_move_only():
    asks = [requirement(facts={"position": "top"})]
    assert kinds(catalog_for([bar(text="Bef0re")], asks)) == ["restore_exact_text"]


# --- fit_text_to_duration -----------------------------------------------------------------


def test_text_running_past_the_end_is_fitted_inside_the_rendered_video():
    catalog = catalog_for([bar(start=1.0, end=12.0)], duration=10.0)
    (fit,) = [c for c in catalog.candidates if c.kind == "fit_text_to_duration"]
    assert fit.target == {"start_s": 1.0, "end_s": 9.7} and fit.auto is True
    assert fit.defect_kinds == ["text_cut_off"]
    assert fit.allowed_paths == [
        "editor_payload.text_elements[0].start_s",
        "editor_payload.text_elements[0].end_s",
    ]


@pytest.mark.parametrize(
    "start,end,duration",
    [
        (0.0, 9.0, 10.0),
        (0.0, 9.7, 10.0),
        (0.0, 9.9, 10.0),
        (0.0, 10.0, 10.0),
        (0.0, 10.05, 10.0),
        (9.0, 12.0, 10.0),
        (8.9, 12.0, 10.0),
        (0.0, 12.0, None),
    ],
)
def test_no_fit_when_the_text_ends_in_time_or_cannot_stay_a_second(start, end, duration):
    catalog = catalog_for([bar(start=start, end=end)], duration=duration)
    assert "fit_text_to_duration" not in kinds(catalog)


def test_a_fit_never_widens_the_approved_window():
    (fit,) = [
        c
        for c in catalog_for([bar(start=2.0, end=10.4)], duration=10.0).candidates
        if c.kind == "fit_text_to_duration"
    ]
    assert fit.target["start_s"] >= 2.0 and fit.target["end_s"] <= 10.4


def test_a_missing_text_claim_on_a_title_held_to_the_end_never_stages_a_fit():
    catalog = catalog_for([bar(start=1.0, end=10.0)], duration=10.03)
    for defect in ("text_missing", "text_cut_off"):
        result = claim(catalog, defect=defect)
        assert result.disposition.repair_candidate_ids == [], defect


def test_a_cut_off_claim_stages_the_fit_only_when_the_text_really_outlives_the_video():
    catalog = catalog_for([bar(start=1.0, end=12.0)], duration=10.0)
    (fit,) = [c for c in catalog.candidates if c.kind == "fit_text_to_duration"]
    assert claim(catalog, defect="text_cut_off").disposition.repair_candidate_ids == [
        fit.candidate_id
    ]
    assert claim(catalog, defect="text_missing").disposition.repair_candidate_ids == [
        c.candidate_id for c in catalog.candidates if c.kind == "restore_exact_text"
    ]


# --- move_text_to_safe_anchor -------------------------------------------------------------


@pytest.mark.parametrize(
    "position,anchor", [("bottom", "top"), ("top", "bottom"), ("middle", "top"), ("center", "top")]
)
def test_the_server_picks_the_safe_anchor_and_a_move_is_never_automatic(position, anchor):
    catalog = catalog_for([bar(position=position)])
    (move,) = [c for c in catalog.candidates if c.kind == "move_text_to_safe_anchor"]
    assert move.target == {"anchor": anchor} and move.auto is False
    assert move.defect_kinds == ["text_obstructed"]


def test_no_move_for_a_custom_position():
    assert "move_text_to_safe_anchor" not in kinds(catalog_for([bar(position="custom")]))


# --- determinism --------------------------------------------------------------------------


def test_ids_and_digest_are_deterministic_and_follow_the_target():
    first = catalog_for([bar(text="Bef0re")])
    again = catalog_for([bar(text="Bef0re")])
    changed = catalog_for([bar(text="Bef0re")], exact=("Before ",))
    assert first == again and first.digest == again.digest
    assert first.digest != changed.digest
    ids = [c.candidate_id for c in first.candidates]
    assert len(set(ids)) == len(ids) and all(len(i) == 16 for i in ids)
    assert [c.candidate_id for c in changed.candidates] != ids


def test_a_strategy_draft_issues_requirements_but_no_candidates():
    catalog = catalog_for([], kind="strategy")
    assert [r.requirement_id for r in catalog.requirements] == ["R1"]
    assert catalog.candidates == [] and catalog.requirements[0].draft_matches_authority is None


def test_frames_are_carried_with_their_anchor():
    (frame,) = catalog_for([bar()]).frames
    assert (frame.frame_id, frame.t_ms, frame.anchor_ref) == ("f0", 500, "text-0")
