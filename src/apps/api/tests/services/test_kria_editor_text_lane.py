"""KRI-219 Lane A + KRI-218: bulk text selector ops, text diff, honest receipts.

Fixture: the East Run-shaped guided montage from test_kria_editor_clip_context
(synthetic clips with per-clip label bars), with the labels renamed to the
KRI-218 shape ("Arnavutköy Sahili" ...).
"""

from __future__ import annotations

import copy

import pytest

from app.kria.brief import BriefRequirement, CreativeBrief
from app.kria.brief_checks import (
    build_receipts,
    plan_facts_from_editor_payload,
    reply_from_receipts,
)
from app.services.kria_editor_ops import (
    KriaEditorOpError,
    build_editor_snapshot,
    compile_editor_ops,
)
from app.services.kria_editor_ops_text import (
    apply_replace,
    bars_from_snapshot,
    fold,
    normalize_selector,
    resolve_selector,
)
from tests.services.test_kria_editor_clip_context import (
    _caps,  # noqa: F401  (autouse fixture)
    _context,
    _job_and_variant,
    _parse,
    _saved_text,
)

LABEL_TEXTS = ["Arnavutköy Sahili", "arnavutkoy iskelesi", "Beta Bridge", "ARNAVUTKÖY"]


def _east_run():
    job, variant = _job_and_variant()
    labels = [r for r in variant["text_elements"] if r["id"].startswith("clip-label-")]
    assert len(labels) == 4
    for row, text in zip(labels, LABEL_TEXTS, strict=True):
        row["text"] = text
    snapshot = build_editor_snapshot(job, variant, clip_context=_context(job, variant))
    return job, variant, snapshot, labels


def _ops(snapshot, ops, utterance="do it"):
    return _parse(snapshot, ops, utterance)


def _texts(compiled) -> dict[str, str]:
    return {row["id"]: row["text"] for row in _saved_text(compiled)}


# ── folding + selectors ───────────────────────────────────────────────────────


def test_fold_is_turkish_safe() -> None:
    assert fold("İSTANBUL") == fold("istanbul") == fold("Istanbul") == fold("ıstanbul")
    assert fold("Arnavutköy Sahili") == fold("ARNAVUTKOY   sahili")
    assert fold("ŞİŞLİ Çarşı") == fold("sisli carsi")


def test_apply_replace_is_diacritic_and_case_insensitive() -> None:
    assert apply_replace("Istanbul Sahili", "istanbul", "İstanbul") == ("İstanbul Sahili", 1)
    assert apply_replace("İstanbul ve ıstanbul", "Istanbul", "X")[0] == "X ve X"
    assert apply_replace("Kadıköy", "kadikoy", "Kadıköy Rıhtım")[0] == "Kadıköy Rıhtım"
    assert apply_replace("nothing here", "zzz", "x") == ("nothing here", 0)


def test_selector_groups_and_contains() -> None:
    _, _, snapshot, labels = _east_run()
    bars = bars_from_snapshot(snapshot)

    def pick(**sel):
        normalized = normalize_selector(sel, snapshot)
        assert normalized is not None
        return resolve_selector(bars, normalized)

    label_ids = {r["id"] for r in labels}
    assert set(pick(group="labels")) == label_ids
    assert pick(group="title") == ["guided-title"]
    assert "guided-title" not in pick(group="free")
    assert set(pick(group="all")) >= label_ids | {"guided-title"}
    # Turkish-safe contains: dotless/ASCII/uppercase spellings all match.
    assert set(pick(group="labels", contains="arnavutkoy")) == {
        labels[0]["id"],
        labels[1]["id"],
        labels[3]["id"],
    }
    assert pick(group="labels", equals="arnavutköy") == [labels[3]["id"]]
    assert pick(clip_indexes=[0]) == [labels[0]["id"]]


def test_selector_rejects_malformed_and_empty() -> None:
    _, _, snapshot, _ = _east_run()
    for bad in ({}, {"group": "nope"}, {"contains": ""}, {"ids": []}, {"wat": 1}, "x", None):
        assert normalize_selector(bad, snapshot) is None
    assert normalize_selector({"clip_indexes": [99]}, snapshot) is None
    assert normalize_selector({"bar_indexes": [99]}, snapshot) is None


def test_removed_tombstones_are_never_selected() -> None:
    job, variant, _, labels = _east_run()
    labels[0]["removed"] = True
    snapshot = build_editor_snapshot(job, variant)
    normalized = normalize_selector({"group": "labels"}, snapshot)
    assert labels[0]["id"] not in resolve_selector(bars_from_snapshot(snapshot), normalized)


# ── rewrite_text (KRI-218 regression) ─────────────────────────────────────────


def test_rewrite_all_matching_labels_in_one_compile_with_accurate_receipt() -> None:
    job, variant, snapshot, labels = _east_run()
    output = _ops(
        snapshot,
        [
            {
                "op": "rewrite_text",
                "selector": {"group": "labels", "contains": "Arnavutköy"},
                "text": "Arnavutköy",
            }
        ],
        "make all Arnavutköy labels just say Arnavutköy",
    )
    assert output.outcome == "proposed", output.rejection_reasons
    op = output.ops[0]
    assert op["expected_count"] == 3
    assert set(op["target_ids"]) == {labels[0]["id"], labels[1]["id"], labels[3]["id"]}

    compiled = compile_editor_ops(job, variant, output.ops)
    texts = _texts(compiled)
    assert [texts[labels[i]["id"]] for i in (0, 1, 3)] == ["Arnavutköy"] * 3
    assert texts[labels[2]["id"]] == "Beta Bridge", "non-matching labels are untouched"
    assert compiled.changes == ["Rewrite 3 texts"]

    diff = {row["id"]: row for row in compiled.text_diff}
    assert set(diff) == {labels[0]["id"], labels[1]["id"], labels[3]["id"]}
    entry = diff[labels[0]["id"]]
    assert entry["before"] == "Arnavutköy Sahili" and entry["after"] == "Arnavutköy"
    assert entry["role"] == "label" and entry["clip_id"] == "clip-0"
    # ARNAVUTKÖY -> Arnavutköy is a real wording (casing) change and is reported.
    assert diff[labels[3]["id"]]["before"] == "ARNAVUTKÖY"

    requirement = BriefRequirement(
        id="r1",
        kind="text",
        scope="per_clip",
        literal="Arnavutköy",
        description="the labels just say Arnavutköy",
    )
    facts = plan_facts_from_editor_payload(
        compiled.payload.model_dump(mode="json", exclude_none=True), compiled.text_diff
    )
    assert facts.editor and facts.has_clip_structure
    assert facts.per_clip_text == {
        "clip-0": "Arnavutköy",
        "clip-1": "Arnavutköy",
        "clip-4": "Arnavutköy",
    }
    receipts = build_receipts([requirement], facts)
    assert receipts[0].status == "met", receipts[0]
    reply = reply_from_receipts(
        CreativeBrief(version=1, requirements=[requirement]),
        receipts,
        summary="Changed 3 labels.",
    )
    assert "can't verify" not in reply.lower()
    assert reply.startswith("Changed 3 labels.")


def test_exact_text_requirement_is_partial_when_a_clip_does_not_read_exactly() -> None:
    requirement = BriefRequirement(
        id="r1",
        kind="text",
        scope="per_clip",
        literal="Arnavutköy",
        description="labels only say X",
    )
    diff = [
        {"id": "a", "clip_id": "c1", "role": "label", "before": "x", "after": "Arnavutköy"},
        {"id": "b", "clip_id": "c2", "role": "label", "before": "y", "after": "Arnavutköy Sahili"},
    ]
    facts = plan_facts_from_editor_payload({"text_elements": []}, diff)
    receipt = build_receipts([requirement], facts)[0]
    assert receipt.status == "partial"
    assert "1 of 2" in (receipt.reason or "")


def test_per_clip_receipt_without_structure_is_unchecked_not_a_failure() -> None:
    requirement = BriefRequirement(
        id="r1", kind="text", scope="per_clip", literal="Zeta", description="label each clip"
    )
    facts = plan_facts_from_editor_payload({"text_elements": [{"text": "Alpha"}]})
    receipt = build_receipts([requirement], facts)[0]
    assert receipt.status == "unchecked"
    reply = reply_from_receipts(
        CreativeBrief(version=1, requirements=[requirement]),
        [receipt],
        summary="Done.",
    )
    assert reply.startswith("Done.") and "Not everything" not in reply
    # A literal that IS on screen stays met on the structure-less path.
    ok = plan_facts_from_editor_payload({"text_elements": [{"text": "Zeta"}]})
    assert build_receipts([requirement], ok)[0].status == "met"


def test_rewrite_replace_edits_inside_matches_and_ignores_the_rest() -> None:
    job, variant, snapshot, labels = _east_run()
    labels[2]["text"] = "Istanbul Bridge"
    snapshot = build_editor_snapshot(job, variant)
    output = _ops(
        snapshot,
        [
            {
                "op": "rewrite_text",
                "selector": {"group": "all"},
                "replace": {"find": "Istanbul", "with": "İstanbul"},
            }
        ],
    )
    assert output.outcome == "proposed"
    assert output.ops[0]["target_ids"] == [labels[2]["id"]]
    compiled = compile_editor_ops(job, variant, output.ops)
    assert _texts(compiled)[labels[2]["id"]] == "İstanbul Bridge"
    assert len(compiled.text_diff) == 1


def test_rewrite_requires_exactly_one_of_text_or_replace() -> None:
    _, _, snapshot, _ = _east_run()
    both = _ops(
        snapshot,
        [
            {
                "op": "rewrite_text",
                "selector": {"group": "labels"},
                "text": "x",
                "replace": {"find": "a", "with": "b"},
            }
        ],
    )
    neither = _ops(snapshot, [{"op": "rewrite_text", "selector": {"group": "labels"}}])
    assert both.ops == [] and neither.ops == []


def test_zero_match_is_an_honest_clarification_naming_the_selector() -> None:
    _, _, snapshot, _ = _east_run()
    output = _ops(
        snapshot,
        [
            {
                "op": "rewrite_text",
                "selector": {"group": "labels", "contains": "Galata"},
                "text": "Galata",
            }
        ],
    )
    assert output.ops == []
    assert output.outcome == "clarification" and output.needs_clarification
    assert "Galata" in output.reply and "any clip label" in output.reply
    assert "nothing" in output.reply.lower()


def test_already_matching_text_is_reported_not_silently_applied() -> None:
    job, variant, snapshot, labels = _east_run()
    output = _ops(
        snapshot,
        [{"op": "rewrite_text", "selector": {"ids": [labels[2]["id"]]}, "text": "Beta Bridge"}],
    )
    assert output.ops == [] and "already read" in output.reply


def test_hand_edited_labels_are_still_targetable_by_selector() -> None:
    job, variant, _, labels = _east_run()
    # `edited` = differs from the approved AI label; an explicit rewrite still applies.
    snapshot = build_editor_snapshot(job, variant, clip_context=_context(job, variant))
    assert all(b.get("edited") for b in snapshot["text_bars"] if b["id"] == labels[0]["id"])
    output = _ops(
        snapshot,
        [{"op": "rewrite_text", "selector": {"group": "labels"}, "text": "Spot"}],
    )
    compiled = compile_editor_ops(job, variant, output.ops)
    assert {_texts(compiled)[row["id"]] for row in labels} == {"Spot"}
    assert compiled.changes == ["Rewrite 4 texts"]


def test_compile_raises_when_text_drifted_after_parse() -> None:
    job, variant, snapshot, labels = _east_run()
    output = _ops(
        snapshot,
        [
            {
                "op": "rewrite_text",
                "selector": {"group": "labels", "contains": "Arnavutköy"},
                "text": "A",
            }
        ],
    )
    drifted = copy.deepcopy(variant)
    next(r for r in drifted["text_elements"] if r["id"] == labels[0]["id"])["text"] = "Different"
    with pytest.raises(KriaEditorOpError, match="Text changed"):
        compile_editor_ops(job, drifted, output.ops)
    removed = copy.deepcopy(variant)
    removed["text_elements"] = [r for r in removed["text_elements"] if r["id"] != labels[1]["id"]]
    with pytest.raises(KriaEditorOpError, match="Text changed"):
        compile_editor_ops(job, removed, output.ops)


# ── patch_text / remove_texts / set_texts_timing ──────────────────────────────


def test_patch_text_sets_style_animation_background_and_scale() -> None:
    job, variant, snapshot, labels = _east_run()
    before = {r["id"]: r for r in variant["text_elements"]}
    output = _ops(
        snapshot,
        [
            {
                "op": "patch_text",
                "selector": {"group": "labels"},
                "patch": {
                    "color": "#FFD60A",
                    "background_color": "#000000",
                    "size_scale": 1.5,
                    "animation_phases": {"entrance": "pop"},
                    "behind_subject": False,
                },
            }
        ],
        "make every label yellow, bigger, with a background and pop-in",
    )
    assert output.outcome == "proposed", output.rejection_reasons
    compiled = compile_editor_ops(job, variant, output.ops)
    saved = {row["id"]: row for row in _saved_text(compiled)}
    for row in labels:
        out = saved[row["id"]]
        assert out["color"] == "#FFD60A" and out["background_color"] == "#000000"
        assert out["size_px"] == round(before[row["id"]]["size_px"] * 1.5, 1)
        assert out["animation_phases"]["entrance"] == "pop"
        assert out["animation_phases"]["exit"] == "none"
    assert saved["guided-title"]["color"] == before["guided-title"].get("color")
    assert compiled.text_diff == []  # style-only: no wording changed


def test_patch_text_rejects_unknown_fields_and_bad_values() -> None:
    _, _, snapshot, _ = _east_run()
    for patch in (
        {"italic": True},
        {"color": "yellow"},
        {"animation_phases": {"entrance": "spin"}},
        {"size_scale": 9},
        {"size_px": 40, "size_scale": 2},
        {},
    ):
        out = _ops(
            snapshot, [{"op": "patch_text", "selector": {"group": "labels"}, "patch": patch}]
        )
        assert out.ops == [], patch


def test_patch_text_moves_title_to_top() -> None:
    job, variant, snapshot, _ = _east_run()
    output = _ops(
        snapshot,
        [{"op": "patch_text", "selector": {"group": "title"}, "patch": {"position": "top"}}],
    )
    compiled = compile_editor_ops(job, variant, output.ops)
    title = next(r for r in _saved_text(compiled) if r["id"] == "guided-title")
    assert title["position"] == "top"


def test_remove_texts_deletes_every_label_and_reports_the_diff() -> None:
    job, variant, snapshot, labels = _east_run()
    output = _ops(snapshot, [{"op": "remove_texts", "selector": {"group": "labels"}}])
    compiled = compile_editor_ops(job, variant, output.ops)
    saved = _texts(compiled)
    assert not any(row["id"] in saved for row in labels)
    assert "guided-title" in saved
    assert compiled.changes == ["Remove 4 texts"]
    assert {d["id"] for d in compiled.text_diff} == {r["id"] for r in labels}
    assert all(d["after"] is None and d["role"] == "label" for d in compiled.text_diff)


def test_set_texts_timing_shift_and_absolute() -> None:
    job, variant, snapshot, labels = _east_run()
    starts = {r["id"]: (r["start_s"], r["end_s"]) for r in labels}
    output = _ops(
        snapshot,
        [{"op": "set_texts_timing", "selector": {"group": "labels"}, "shift_s": 0.25}],
    )
    saved = {row["id"]: row for row in _saved_text(compile_editor_ops(job, variant, output.ops))}
    for row in labels:
        start, end = starts[row["id"]]
        assert saved[row["id"]]["start_s"] == pytest.approx(start + 0.25)
        assert saved[row["id"]]["end_s"] > saved[row["id"]]["start_s"]
        assert saved[row["id"]]["end_s"] <= end + 0.25 + 1e-6
    absolute = _ops(
        snapshot,
        [{"op": "set_texts_timing", "selector": {"group": "title"}, "start_s": 0, "end_s": 2}],
    )
    title = next(
        r
        for r in _saved_text(compile_editor_ops(job, variant, absolute.ops))
        if r["id"] == "guided-title"
    )
    assert (title["start_s"], title["end_s"]) == (0, 2)
    both = _ops(
        snapshot,
        [{"op": "set_texts_timing", "selector": {"group": "labels"}, "start_s": 1, "shift_s": 1}],
    )
    assert both.ops == []


# ── add_text (extended) ───────────────────────────────────────────────────────


def test_add_text_without_new_fields_is_the_legacy_bar() -> None:
    job, variant, snapshot, _ = _east_run()
    output = _ops(snapshot, [{"op": "add_text", "text": "hi", "start_s": 1, "end_s": 2}])
    added = next(
        r for r in _saved_text(compile_editor_ops(job, variant, output.ops)) if r["text"] == "hi"
    )
    assert added["font_family"] == "Playfair Display" and added["size_px"] == 72
    assert added["color"] == "#FFFFFF" and added["position"] == "middle"


def test_add_text_style_from_title_copies_the_look() -> None:
    job, variant, snapshot, _ = _east_run()
    title = next(r for r in variant["text_elements"] if r["id"] == "guided-title")
    output = _ops(
        snapshot,
        [
            {
                "op": "add_text",
                "text": "see you soon",
                "start_s": 20,
                "end_s": 23,
                "style_from": "title",
                "position": "bottom",
                "animation_phases": {"entrance": "fade"},
            }
        ],
    )
    assert output.outcome == "proposed", output.rejection_reasons
    compiled = compile_editor_ops(job, variant, output.ops)
    added = next(r for r in _saved_text(compiled) if r["text"] == "see you soon")
    for key in ("font_family", "color"):
        if key in title:
            assert added[key] == title[key]
    assert added["position"] == "bottom"
    assert added["animation_phases"]["entrance"] == "fade"
    assert [d["after"] for d in compiled.text_diff] == ["see you soon"]
    assert compiled.text_diff[0]["before"] is None


def test_add_text_for_clip_makes_a_linked_label_that_later_selectors_find() -> None:
    job, variant, snapshot, labels = _east_run()
    # Clip 5 (slot index 5) has no label bar in the fixture.
    slot = snapshot["slots"][5]
    output = _ops(
        snapshot,
        [
            {
                "op": "add_text",
                "text": "the bridge",
                "start_s": slot["output_start_s"],
                "end_s": slot["output_end_s"],
                "clip_id": slot["media_id"],
            }
        ],
        "label clip 6 'the bridge'",
    )
    assert output.outcome == "proposed", output.rejection_reasons
    compiled = compile_editor_ops(job, variant, output.ops)
    added = next(r for r in _saved_text(compiled) if r["text"] == "the bridge")
    assert added["id"] == f"clip-label-media-{slot['media_id']}"
    sibling = next(r for r in _saved_text(compiled) if r["id"] == labels[0]["id"])
    assert added["font_family"] == sibling["font_family"]
    entry = compiled.text_diff[0]
    assert entry["clip_id"] == slot["media_id"] and entry["role"] == "label"
    # A clip that already has a label asks instead of duplicating.
    dup = _ops(
        snapshot,
        [
            {
                "op": "add_text",
                "text": "x",
                "start_s": 0,
                "end_s": 1,
                "clip_id": snapshot["slots"][0]["media_id"],
            }
        ],
    )
    assert dup.ops == [] and "already has a label" in dup.reply


def test_v2_fields_are_ignored_on_a_marker_less_snapshot() -> None:
    _, _, snapshot, _ = _east_run()
    plain = {k: v for k, v in snapshot.items() if k != "editor_ops_version"}
    output = _parse(
        plain,
        [{"op": "add_text", "text": "hi", "start_s": 1, "end_s": 2, "style_from": "title"}],
    )
    assert output.ops and "style_from" not in output.ops[0]
    gated = _parse(plain, [{"op": "remove_texts", "selector": {"group": "labels"}}])
    assert gated.ops == []


def test_text_diff_covers_legacy_ops_too() -> None:
    job, variant, snapshot, labels = _east_run()
    index = next(i for i, b in enumerate(snapshot["text_bars"]) if b["id"] == labels[2]["id"])
    output = _ops(snapshot, [{"op": "edit_text", "bar_index": index, "text": "Besiktas Pier"}])
    compiled = compile_editor_ops(job, variant, output.ops)
    assert compiled.text_diff == [
        {
            "id": labels[2]["id"],
            "clip_id": "clip-3",
            "role": "label",
            "before": "Beta Bridge",
            "after": "Besiktas Pier",
        }
    ]


# ── draft document (KRI-218 persistence) ──────────────────────────────────────


def test_draft_document_persists_text_diff_without_changing_legacy_hashes() -> None:
    from app.kria.drafts import KriaDraftDocument, canonical_snapshot

    legacy = KriaDraftDocument(kind="editor", editor_payload={"text_elements": []})
    snapshot, digest = canonical_snapshot(legacy)
    assert "editor_text_diff" not in snapshot
    diff = [{"id": "a", "clip_id": "c", "role": "label", "before": "x", "after": "y"}]
    with_diff = KriaDraftDocument(
        kind="editor", editor_payload={"text_elements": []}, editor_text_diff=diff
    )
    snap2, digest2 = canonical_snapshot(with_diff)
    assert snap2["editor_text_diff"] == diff and digest2 != digest
    assert KriaDraftDocument.model_validate(snap2).editor_text_diff == diff
    with pytest.raises(ValueError):
        KriaDraftDocument(kind="strategy", strategy={}, editor_text_diff=diff)
