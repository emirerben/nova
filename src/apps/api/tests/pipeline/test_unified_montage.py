"""KRI-190: the deterministic unified montage planner (order, text, reading time, title)."""

from __future__ import annotations

import unicodedata
from datetime import UTC, datetime, timedelta

import pytest

from app.kria.brief import BriefRequirement, CreativeBrief
from app.kria.brief_checks import (
    build_receipts,
    plan_facts_from_unified_montage,
)
from app.pipeline.guided_story import (
    compile_execution_plan,
    validate_execution_plan,
    validate_guided_snapshot,
    validate_proposal_compiles,
)
from app.pipeline.unified_montage import (
    BriefView,
    UnifiedClip,
    brief_view,
    min_display_s,
    plan_unified_montage,
    selected_visual_ids,
    skia_font_covers,
    title_from_facts,
)
from app.schemas.edit_proposal import EditProposalSnapshot

T0 = datetime(2026, 9, 20, 7, 0, tzinfo=UTC)


def clip(
    index: int,
    *,
    duration: float = 5.0,
    minutes: int | None = None,
    landmark: str | None = None,
    place: str | None = None,
    facts: tuple[dict, ...] = (),
) -> UnifiedClip:
    rows = list(facts)
    taken = None
    if minutes is not None:
        taken = T0 + timedelta(minutes=minutes)
        rows.append(
            {
                "kind": "capture_time",
                "value": taken.strftime("%Y-%m-%dT%H:%M:%SZ"),
                "provenance": "exif",
            }
        )
    if place:
        rows.append({"kind": "place", "value": place, "provenance": "geocode"})
    if landmark:
        rows.append({"kind": "landmark", "value": landmark, "provenance": "inferred"})
    return UnifiedClip(
        media_id=f"c{index}",
        proxy_path=f"users/u/analysis-proxy-c{index}.mp4",
        generation="1",
        duration_s=duration,
        width=1920,
        height=1080,
        facts=tuple(rows),
        capture_time=taken,
    )


def labels_view(**kwargs) -> BriefView:
    return BriefView(wants_per_clip_text=True, **kwargs)


@pytest.mark.parametrize(
    ("chars", "expected"),
    [
        (0, 1.2),
        (3, 1.2),
        (7, 1.22),  # 0.8 + 0.06 * 7
        (13, 1.58),
        (30, 2.6),
        (37, 3.0),
        (200, 3.0),
    ],
)
def test_reading_time_rule(chars, expected):
    assert min_display_s(chars) == pytest.approx(expected)


def test_order_is_capture_time_when_the_brief_asks_and_two_clips_have_one():
    clips = [clip(0, minutes=30), clip(1, minutes=10), clip(2, minutes=20)]
    plan = plan_unified_montage(clips, BriefView(wants_order=True, order_by_capture=True))
    assert plan.clip_ids == ["c1", "c2", "c0"]
    assert plan.ordering_basis == "capture_time"
    assert plan.ordering_fallback_clip_ids == []


def test_untimed_clips_keep_their_attachment_slot_and_are_reported():
    clips = [clip(0, minutes=30), clip(1), clip(2, minutes=10)]
    plan = plan_unified_montage(clips, BriefView(wants_order=True, order_by_capture=True))
    assert plan.clip_ids == ["c2", "c1", "c0"]
    assert plan.ordering_fallback_clip_ids == ["c1"]


def test_order_is_attachment_when_the_brief_did_not_ask():
    # No silent override of the creator's selection order.
    clips = [clip(0, minutes=30), clip(1, minutes=10)]
    plan = plan_unified_montage(clips, BriefView())
    assert plan.clip_ids == ["c0", "c1"]
    assert plan.ordering_basis == "attachment"


def test_fewer_than_two_capture_times_falls_back_to_attachment():
    plan = plan_unified_montage(
        [clip(0, minutes=5), clip(1), clip(2)], BriefView(order_by_capture=True)
    )
    assert plan.ordering_basis == "attachment"
    assert plan.clip_ids == ["c0", "c1", "c2"]


def test_labels_are_grounded_and_ungrounded_clips_are_dropped():
    clips = [
        clip(0, landmark="Rumeli Hisarı"),
        clip(1, place="Sarıyer, İstanbul, Türkiye"),
        clip(2),
        clip(3, facts=({"kind": "creator", "value": "Bebek Parkı", "provenance": "creator"},)),
    ]
    plan = plan_unified_montage(clips, labels_view())
    by_id = {label.media_id: label for label in plan.snapshot.clip_labels or []}
    assert by_id["c0"].text == "Rumeli Hisarı" and by_id["c0"].inferred is True
    assert by_id["c0"].fact_kind == "landmark" and by_id["c0"].provenance == "fact"
    # A place is the most specific part only, never the whole geocoded string.
    assert by_id["c1"].text == "Sarıyer" and by_id["c1"].inferred is False
    assert by_id["c3"].text == "Bebek Parkı" and by_id["c3"].provenance == "creator"
    assert "c2" not in by_id
    assert plan.dropped_label_clip_ids == ["c2"]


def test_no_label_is_invented_when_the_brief_asked_for_none():
    plan = plan_unified_montage([clip(0, landmark="Galata")], BriefView())
    assert plan.snapshot.clip_labels == []
    assert plan.dropped_label_clip_ids == []


def test_brief_facts_ground_the_first_and_last_unlabelled_clip_only():
    clips = [clip(0), clip(1), clip(2)]
    plan = plan_unified_montage(clips, labels_view(facts={"start": "Arnavutköy", "end": "Eminönü"}))
    by_id = {label.media_id: label for label in plan.snapshot.clip_labels or []}
    assert by_id["c0"].text == "Arnavutköy" and by_id["c0"].provenance == "brief"
    assert by_id["c2"].text == "Eminönü"
    assert "c1" not in by_id


def test_exact_creator_words_beat_facts_and_shot_labels_are_positional():
    clips = [clip(0, landmark="Galata"), clip(1, landmark="Bebek"), clip(2, landmark="Ortaköy")]
    plan = plan_unified_montage(
        clips,
        labels_view(clip_literals={"c1": "Kilometre 10"}),
        strategy={"shot_labels": ["Başlangıç"]},
    )
    by_id = {label.media_id: label for label in plan.snapshot.clip_labels or []}
    assert by_id["c0"].text == "Başlangıç" and by_id["c0"].provenance == "creator"
    assert by_id["c1"].text == "Kilometre 10"
    assert by_id["c2"].text == "Ortaköy"


def test_verified_clip_intents_label_clips_only_when_enabled():
    strategy = {
        "resolved_clip_intents": [
            {
                "op": "label",
                "assignments": [
                    {"media_id": "c0", "value": "Kadıköy", "grounding": "creator_text"},
                    {"media_id": "c1", "value": "Moda", "grounding": "clip_evidence"},
                ],
            }
        ]
    }
    clips = [clip(0), clip(1)]
    off = plan_unified_montage(clips, BriefView(), strategy=strategy)
    on = plan_unified_montage(clips, BriefView(), strategy=strategy, clip_intents_enabled=True)
    assert off.snapshot.clip_labels == []
    labels = {label.media_id: label for label in on.snapshot.clip_labels or []}
    assert labels["c0"].provenance == "creator" and labels["c0"].inferred is False
    assert labels["c1"].provenance == "fact" and labels["c1"].inferred is True


def _described_shots_strategy() -> dict:
    # KRI-296 (job 7565bcbb): six shots described in the creator's own order, each
    # with its exact text, over seven clips uploaded in a different order.
    shots = [
        ("Borahan came to win tonight's tournament", "c5"),
        ("The only girl in the tournament: Elfin", "c6"),
        ("Mahmoud from Tunisia: reads books, bowls too", "c4"),
        ("Ervahan means business", "c0"),
        ("It all came down to Eren vs Borahan", "c3"),
        ("Borahan 131, Eren 124. So close, Eren", "c2"),
    ]
    return {
        "opening_title": "An evening in Istanbul with Eren",
        "closing_title": "See you at the rematch, Eren",
        "shot_labels": [text for text, _ in shots],
        "resolved_clip_intents": [
            {
                "op": "caption",
                "status": "resolved",
                "intent_id": f"caption_shot{n}",
                "creator_text": text,
                "caption_text": text,
                "caption_grounding": "creator_text",
                "assignments": [{"media_id": media_id, "confidence": 1.0}],
            }
            for n, (text, media_id) in enumerate(shots, start=1)
        ],
    }


def test_described_shot_text_goes_on_the_matched_clip_not_its_list_position():
    strategy = _described_shots_strategy()
    clips = [clip(i, landmark="Funloft Akmerkez" if i == 6 else None) for i in range(7)]
    plan = plan_unified_montage(clips, labels_view(), strategy=strategy, clip_intents_enabled=True)
    labels = {label.media_id: label for label in plan.snapshot.clip_labels or []}
    assert {media_id: label.text for media_id, label in labels.items()} == {
        "c5": "Borahan came to win tonight's tournament",
        "c6": "The only girl in the tournament: Elfin",
        "c4": "Mahmoud from Tunisia: reads books, bowls too",
        "c0": "Ervahan means business",
        "c3": "It all came down to Eren vs Borahan",
        "c2": "Borahan 131, Eren 124. So close, Eren",
    }
    assert all(label.provenance == "creator" and not label.inferred for label in labels.values())
    # The clip nobody described gets no text: no guessed venue, and no "left off" note.
    assert "c1" not in labels
    assert plan.dropped_label_clip_ids == []
    assert plan.record()["label_scope_clip_ids"] == ["c0", "c2", "c3", "c4", "c5", "c6"]


def test_described_shot_receipts_judge_the_described_clips_and_see_the_closing_title():
    brief = CreativeBrief(
        version=1,
        requirements=[
            BriefRequirement(
                id="r2", kind="text", scope="per_clip", description="exact text on 6 shots"
            ),
            BriefRequirement(
                id="r3",
                kind="text",
                scope="global",
                literal="See you at the rematch, Eren",
                description="closing title",
            ),
        ],
    )
    plan = plan_unified_montage(
        [clip(i, landmark="Funloft Akmerkez" if i == 6 else None) for i in range(7)],
        brief_view(brief),
        strategy=_described_shots_strategy(),
        clip_intents_enabled=True,
    )
    facts = plan_facts_from_unified_montage(plan.record())
    receipts = {r.requirement_id: r for r in build_receipts(brief.live(), facts)}
    assert receipts["r2"].status == "met" and receipts["r2"].inferred == []
    assert receipts["r3"].status == "met"


def test_without_matched_shots_shot_labels_stay_positional():
    strategy = {**_described_shots_strategy(), "resolved_clip_intents": []}
    plan = plan_unified_montage(
        [clip(i) for i in range(7)], labels_view(), strategy=strategy, clip_intents_enabled=True
    )
    labels = {label.media_id: label.text for label in plan.snapshot.clip_labels or []}
    assert labels["c0"] == "Borahan came to win tonight's tournament"
    assert "label_scope_clip_ids" not in plan.record()


def test_a_label_lasts_at_least_its_reading_time_and_is_capped_by_the_clip():
    clips = [
        clip(0, landmark="Rumeli Hisarı"),  # 13 chars -> 1.58s
        clip(1, landmark="Dolmabahçe Sarayı Müzesi"),  # long label, short clip
        clip(2),
    ]
    clips[1] = UnifiedClip(**{**clips[1].__dict__, "duration_s": 1.5})
    plan = plan_unified_montage(clips, labels_view())
    durations = {cut.media_id: cut.output_duration_s for cut in plan.snapshot.fast_cuts}
    labels = {label.media_id: label for label in plan.snapshot.clip_labels or []}
    assert durations["c0"] >= labels["c0"].min_display_s - 0.001
    assert durations["c1"] == pytest.approx(1.5)  # the whole clip, no more
    assert "c1" in plan.short_label_clip_ids
    assert "c0" not in plan.short_label_clip_ids
    # An unlabelled cut keeps the classic fast-cut length.
    assert durations["c2"] == pytest.approx(1.2)


def test_cuts_are_frame_aligned_and_sum_to_the_proposal():
    plan = plan_unified_montage(
        [clip(i, landmark=f"Yer {i}", duration=4.0 + i / 7) for i in range(6)], labels_view()
    )
    for cut in plan.snapshot.fast_cuts:
        assert (cut.output_duration_s * 30) == pytest.approx(round(cut.output_duration_s * 30))
        assert cut.source_end_s <= 4.0 + 5 / 7 + 0.001
    assert sum(c.output_duration_s for c in plan.snapshot.fast_cuts) == pytest.approx(
        plan.snapshot.duration_s
    )


def test_the_requested_length_is_honoured_where_the_clips_allow():
    clips = [clip(i, duration=6.0) for i in range(4)]
    longer = plan_unified_montage(clips, BriefView(target_duration_s=9))
    assert longer.duration_s == pytest.approx(9.0)
    # Growth stops at what the clips hold when the ask is bigger.
    capped = plan_unified_montage(clips, BriefView(target_duration_s=60))
    assert capped.duration_s == pytest.approx(24.0)
    # Only unlabelled cuts shrink; labelled ones keep their reading time.
    mixed = [clip(0, landmark="Galata Kulesi"), clip(1), clip(2), clip(3)]
    shrunk = plan_unified_montage(mixed, labels_view(target_duration_s=3.5))
    cuts = {cut.media_id: cut.output_duration_s for cut in shrunk.snapshot.fast_cuts}
    assert cuts["c0"] >= 1.5
    assert all(cuts[c] >= 0.8 - 0.001 for c in ("c1", "c2", "c3"))


def test_clips_too_short_for_a_video_fail_loudly():
    with pytest.raises(ValueError, match="too short"):
        plan_unified_montage([clip(0, duration=0.5), clip(1, duration=0.5)])


def test_title_from_brief_facts_keeps_turkish_and_the_route():
    assert (
        title_from_facts(
            {"distance_km": 20, "activity": "run", "start": "Arnavutköy", "end": "Eminönü"}
        )
        == "20K Run · Arnavutköy → Eminönü"
    )
    assert title_from_facts({"start": "Arnavutköy", "end": "Eminönü"}) == "Arnavutköy → Eminönü"
    assert title_from_facts({"distance_km": 21.1}) == "21.1K"
    assert title_from_facts({}) is None
    # A half-stated route is not invented into a full one.
    assert title_from_facts({"start": "Arnavutköy"}) is None


def test_title_priority_and_nfc():
    view = BriefView(
        title_literal="Koşu Günlüğü",
        global_literal="20k run",
        facts={"distance_km": 20, "start": "A", "end": "B"},
    )
    creator = plan_unified_montage([clip(0)], view, strategy={"opening_title": "Onaylı Başlık"})
    assert creator.title == "Onaylı Başlık" and creator.title_source == "creator"
    assert plan_unified_montage([clip(0)], view).title == "Koşu Günlüğü"
    generated = plan_unified_montage(
        [clip(0)], BriefView(facts={"distance_km": 20, "activity": "run", "start": "A", "end": "B"})
    )
    assert generated.title == "20K Run · A → B" and generated.title_source == "brief"
    # Decomposed input is normalised to NFC, never folded.
    decomposed = "Arnavutköy"
    normalised = plan_unified_montage([clip(0)], BriefView(title_literal=decomposed))
    assert normalised.title == unicodedata.normalize("NFC", decomposed)
    assert normalised.title == "Arnavutköy"


def test_missing_title_is_internal_only_and_never_comes_from_a_model_hook():
    bare = plan_unified_montage([clip(0), clip(1)])
    assert bare.title is None and bare.title_source == "none"
    assert bare.snapshot.title == "Montage"  # required internal snapshot label
    assert bare.snapshot.opening_title is None
    compiled = compile_execution_plan(bare.guided_edit(), track=None)
    assert all(element["id"] != "guided-title" for element in compiled["text_elements"])
    # Place facts label clips; they are never printed as an unrequested title.
    placed = plan_unified_montage(
        [clip(0, place="Sarıyer, İstanbul, Türkiye"), clip(1, place="Bebek, İstanbul, Türkiye")]
    )
    assert placed.title is None and placed.title_source == "none"
    assert placed.snapshot.opening_title is None


def test_creator_title_remains_visible_in_the_compiled_plan():
    plan = plan_unified_montage([clip(0), clip(1)], strategy={"opening_title": "Onaylı Başlık"})
    compiled = compile_execution_plan(plan.guided_edit(), track=None)
    title = next(
        element for element in compiled["text_elements"] if element["id"] == "guided-title"
    )
    assert title["text"] == "Onaylı Başlık"


def test_long_clips_and_a_stated_length_are_honoured_for_single_hero_and_day_vlog():
    # One 20s clip with a 12s ask, and three 20s clips with a 45s ask, are not
    # cut down to a 3s beat each.
    single = plan_unified_montage([clip(0, duration=20.0)], strategy={"target_duration_s": 12})
    assert single.duration_s == pytest.approx(12.0)
    vlog = plan_unified_montage(
        [clip(i, duration=20.0) for i in range(3)], strategy={"target_duration_s": 45}
    )
    assert vlog.duration_s == pytest.approx(45.0)


def test_a_creator_pinned_order_is_kept_unless_the_brief_asks_for_capture_time():
    clips = [clip(i, minutes=10 - i) for i in range(4)]
    kept = plan_unified_montage(clips, creator_order=[2, 0, 3, 1])
    assert kept.clip_ids == ["c2", "c0", "c3", "c1"]
    assert kept.ordering_basis == "creator_order"
    # Bad and partial indices never drop or duplicate a clip.
    partial = plan_unified_montage(clips, creator_order=[3, 3, 9, -1])
    assert partial.clip_ids == ["c3", "c0", "c1", "c2"]
    by_time = plan_unified_montage(
        clips, BriefView(order_by_capture=True), creator_order=[0, 1, 2, 3]
    )
    assert by_time.clip_ids == ["c3", "c2", "c1", "c0"]
    assert by_time.ordering_basis != "creator_order"


def test_creator_written_labels_are_never_cut_short_but_facts_are():
    long_text = "Kilometre otuz beş, Bebek sahilinde son düzlüğe girerken " * 2
    long_text = long_text[:100]
    plan = plan_unified_montage(
        [clip(0), clip(1, landmark="x" * 90)],
        labels_view(clip_literals={"c0": long_text}),
    )
    texts = {label.media_id: label.text for label in plan.snapshot.clip_labels}
    assert texts["c0"] == long_text
    assert len(texts["c1"]) == 60


def test_turkish_dotted_i_is_not_case_folded_to_ascii():
    assert title_from_facts({"activity": "izci yürüyüşü"}) == "izci yürüyüşü"
    assert title_from_facts({"activity": "run", "distance_km": 20}) == "20K Run"
    assert title_from_facts({"distance": "20 km"}) == "20 KM"


def test_the_snapshot_is_a_valid_strict_guided_proposal():
    clips = [clip(i, landmark=f"Yer {i}", minutes=20 - i) for i in range(6)]
    plan = plan_unified_montage(
        clips,
        BriefView(
            wants_per_clip_text=True, order_by_capture=True, facts={"start": "A", "end": "B"}
        ),
    )
    snapshot = EditProposalSnapshot.model_validate(plan.snapshot.model_dump(mode="json"))
    validate_proposal_compiles(snapshot)
    guided = plan.guided_edit()
    # No minted attempt id: it would never match a thread session's.
    assert "generation_attempt_id" not in guided
    _version, _digest, parsed = validate_guided_snapshot(guided)
    assert parsed.clip_labels == snapshot.clip_labels
    compiled = compile_execution_plan(guided, track=None)
    assert validate_execution_plan(compiled, guided) == compiled
    ids = [element["id"] for element in compiled["text_elements"]]
    assert ids[0] == "guided-title"
    assert sum(i.startswith("clip-label-") for i in ids) == 6
    windows = {e["id"]: (e["start_s"], e["end_s"]) for e in compiled["text_elements"]}
    for cut, moment in zip(plan.snapshot.fast_cuts, compiled["story_timeline"], strict=True):
        start, end = windows[f"clip-label-{cut.cut_id}"]
        assert start == pytest.approx(moment["output_start_s"], abs=0.002)
        assert end == pytest.approx(moment["output_end_s"], abs=0.002)


def test_snapshots_without_clip_labels_serialise_exactly_as_before():
    plan = plan_unified_montage([clip(0), clip(1)])
    legacy = plan.snapshot.model_copy(update={"clip_labels": None})
    assert "clip_labels" not in legacy.model_dump(mode="json")
    assert "clip_labels" in plan.snapshot.model_dump(mode="json")


def test_labels_require_fast_cuts_and_known_media():
    plan = plan_unified_montage([clip(0), clip(1)], labels_view())
    payload = plan.snapshot.model_dump(mode="json")
    payload["clip_labels"] = [
        {"media_id": "ghost", "text": "x", "provenance": "creator", "min_display_s": 1.2}
    ]
    with pytest.raises(ValueError, match="unknown media"):
        EditProposalSnapshot.model_validate(payload)
    payload["clip_labels"] = [
        {"media_id": "c0", "text": "x", "provenance": "creator", "min_display_s": 1.2},
        {"media_id": "c0", "text": "y", "provenance": "creator", "min_display_s": 1.2},
    ]
    with pytest.raises(ValueError, match="unique"):
        EditProposalSnapshot.model_validate(payload)


def test_a_font_without_the_arrow_falls_back_to_one_that_has_it():
    def covers(family: str, text: str) -> bool:
        return family != "Fraunces" or "→" not in text

    view = BriefView(facts={"start": "Arnavutköy", "end": "Eminönü"}, global_literal="20k run")
    plan = plan_unified_montage([clip(0), clip(1)], view, font_covers=covers)
    assert plan.snapshot.font_family == "DM Sans"
    assert plan.title == "20k run · Arnavutköy → Eminönü"


def test_glyphs_no_bundled_font_has_are_dropped_but_turkish_letters_stay():
    def covers(_family: str, text: str) -> bool:
        return "☃" not in text

    plan = plan_unified_montage(
        [clip(0, landmark="Şişli ☃ Çarşı"), clip(1)], labels_view(), font_covers=covers
    )
    label = (plan.snapshot.clip_labels or [])[0]
    assert label.text == "Şişli Çarşı"
    assert label.min_display_s == min_display_s(len(label.text))


def test_an_unrenderable_title_becomes_internal_only():
    def covers(_family: str, text: str) -> bool:
        return "☃" not in text

    plan = plan_unified_montage(
        [clip(0), clip(1)],
        strategy={"opening_title": "☃"},
        font_covers=covers,
    )

    assert plan.title is None and plan.title_source == "none"
    assert plan.snapshot.title == "Montage"
    assert plan.snapshot.opening_title is None
    compiled = compile_execution_plan(plan.guided_edit(), track=None)
    assert all(element["id"] != "guided-title" for element in compiled["text_elements"])


def test_the_bundled_fonts_really_lack_the_arrow_and_have_turkish():
    # The reason the fallback exists (Fraunces has no "→"); skipped without skia.
    pytest.importorskip("skia")
    assert skia_font_covers("Fraunces", "Arnavutköy Eminönü Şişli İğdır")
    assert not skia_font_covers("Fraunces", "→")
    assert skia_font_covers("DM Sans", "→")


def test_brief_view_reads_what_the_plan_needs():
    brief = CreativeBrief(
        version=4,
        requirements=[
            BriefRequirement(id="r1", kind="text", scope="per_clip", description="landmarks"),
            BriefRequirement(id="r2", kind="text", scope="title", literal="20K Koşu"),
            BriefRequirement(id="r3", kind="order", scope="global", facts={"key": "chronological"}),
            BriefRequirement(
                id="r4", kind="timing", scope="global", facts={"duration_s": 25, "start": "A"}
            ),
            BriefRequirement(id="r5", kind="text", scope="clip:c3", literal="Finish"),
            BriefRequirement(
                id="r6", kind="text", scope="global", literal="old", status="superseded"
            ),
        ],
    )
    view = brief_view(brief)
    assert view.version == 4
    assert view.wants_per_clip_text and view.wants_order and view.order_by_capture
    assert view.title_literal == "20K Koşu" and view.global_literal is None
    assert view.clip_literals == {"c3": "Finish"}
    assert view.target_duration_s == 25.0
    assert view.facts["start"] == "A"
    assert brief_view(None) == BriefView()


def test_an_unknown_order_key_is_not_treated_as_capture_time():
    brief = CreativeBrief(
        version=1,
        requirements=[
            BriefRequirement(id="r1", kind="order", scope="global", facts={"key": "alphabetical"})
        ],
    )
    view = brief_view(brief)
    assert view.wants_order and not view.order_by_capture


def test_receipts_come_from_what_the_plan_really_contains():
    clips = [
        clip(0, minutes=30, landmark="Galata"),
        clip(1, minutes=10, landmark="Bebek"),
        clip(2, minutes=20),
    ]
    brief = CreativeBrief(
        version=2,
        requirements=[
            BriefRequirement(id="r1", kind="text", scope="per_clip", description="landmarks"),
            BriefRequirement(id="r2", kind="order", scope="global", facts={"key": "capture_time"}),
            BriefRequirement(id="r3", kind="timing", scope="global", facts={"duration_s": 3.6}),
        ],
    )
    plan = plan_unified_montage(clips, brief_view(brief))
    facts = plan_facts_from_unified_montage(plan.record())
    receipts = {r.requirement_id: r for r in build_receipts(brief.live(), facts)}
    assert receipts["r1"].status == "partial"
    assert "2 of 3" in (receipts["r1"].reason or "")
    assert receipts["r1"].inferred == ["Bebek", "Galata"] or set(receipts["r1"].inferred) == {
        "Bebek",
        "Galata",
    }
    assert receipts["r2"].status == "met"
    assert receipts["r3"].status == "met"


def test_a_label_on_a_clip_too_short_to_read_is_not_reported_as_met():
    clips = [
        UnifiedClip(**{**clip(0, landmark="Dolmabahçe Sarayı").__dict__, "duration_s": 1.3}),
        clip(1, landmark="Galata"),
        clip(2, landmark="Bebek"),
    ]
    brief = CreativeBrief(
        version=1,
        requirements=[BriefRequirement(id="r1", kind="text", scope="per_clip", description="x")],
    )
    plan = plan_unified_montage(clips, brief_view(brief))
    receipt = build_receipts(brief.live(), plan_facts_from_unified_montage(plan.record()))[0]
    assert receipt.status == "partial"
    assert "too short" in (receipt.reason or "")


def test_a_landmark_still_labels_a_clip_whose_place_is_only_a_country():
    plan = plan_unified_montage([clip(0, landmark="Galata Tower", place="Türkiye")], labels_view())
    label = (plan.snapshot.clip_labels or [])[0]
    assert (label.text, label.fact_kind) == ("Galata Tower", "landmark")


def test_city_and_country_is_a_label_and_the_country_part_is_never_used():
    plan = plan_unified_montage([clip(0, place="İstanbul, Türkiye")], labels_view())
    assert (plan.snapshot.clip_labels or [])[0].text == "İstanbul"


# ── KRI-217: Visuals-pool photos and videos ──────────────────────────────────


def visual(index: int, *, kind: str = "image", duration: float = 0.0) -> UnifiedClip:
    row_id = f"00000000-0000-4000-8000-{index:012d}"
    return UnifiedClip(
        media_id=row_id,
        proxy_path=f"users/u/plan/i/pool/p{index}.{'jpg' if kind == 'image' else 'mp4'}",
        generation="9",
        duration_s=duration,
        lane="asset",
        kind=kind,
        manifest_id=f"asset-{row_id}",
        aspect=4 / 3,
        source_filename=f"IMG_{index}.jpeg",
    )


def _pattern(plan) -> str:  # noqa: ANN001
    return "".join(
        "P" if cut.media_id in plan.visual_ids else "C" for cut in plan.snapshot.fast_cuts
    )


def test_photos_are_spread_between_the_clips_and_keep_their_pool_identity():
    clips = [clip(i) for i in range(6)]
    photos = [visual(1), visual(2)]
    plan = plan_unified_montage(clips, visuals=photos)

    order = [cut.media_id for cut in plan.snapshot.fast_cuts]
    assert order == ["c0", "c1", photos[0].media_id, "c2", "c3", photos[1].media_id, "c4", "c5"]
    assert plan.visual_ids == [photos[0].media_id, photos[1].media_id]
    assert plan.record()["clip_ids"] == order
    assert plan.record()["visual_ids"] == plan.visual_ids
    ref = next(ref for ref in plan.snapshot.media if ref.media_id == photos[0].media_id)
    assert (ref.lane, ref.kind, ref.duration_s) == ("asset", "image", None)
    assert (ref.gcs_path, ref.generation) == (photos[0].proxy_path, "9")
    assert ref.aspect == pytest.approx(4 / 3)
    assert ref.source_filename == "IMG_1.jpeg"


@pytest.mark.parametrize(
    ("clips_n", "photos_n", "pattern"),
    [(6, 2, "CCPCCPCC"), (6, 1, "CCCPCCC"), (1, 2, "CPP"), (2, 3, "CPPCP")],
)
def test_the_montage_always_opens_on_a_clip(clips_n, photos_n, pattern):
    plan = plan_unified_montage(
        [clip(i) for i in range(clips_n)], visuals=[visual(j) for j in range(photos_n)]
    )
    assert _pattern(plan) == pattern
    assert plan.snapshot.fast_cuts[0].role == "hook"


def test_a_montage_with_photos_compiles_to_pool_still_moments():
    clips = [clip(i) for i in range(4)]
    photos = [visual(1), visual(2)]
    plan = plan_unified_montage(clips, visuals=photos)

    snapshot = EditProposalSnapshot.model_validate(plan.snapshot.model_dump(mode="json"))
    validate_proposal_compiles(snapshot)
    guided = plan.guided_edit()
    compiled = compile_execution_plan(guided, track=None)
    assert validate_execution_plan(compiled, guided) == compiled
    stills = [moment for moment in compiled["story_timeline"] if moment["lane"] == "asset"]
    assert [moment["media_id"] for moment in stills] == [photo.media_id for photo in photos]
    assert all(moment["kind"] == "image" for moment in stills)
    assert all(moment["layout"] == "fullscreen" for moment in stills)
    identities = {row["media_id"]: row for row in guided["media_identities"]}
    assert identities[photos[0].media_id]["lane"] == "asset"
    assert identities[photos[0].media_id]["kind"] == "image"


def test_a_photo_holds_like_a_cut_and_never_past_a_beat_of_attention():
    photo = visual(1)
    default = plan_unified_montage([clip(i) for i in range(3)], visuals=[photo])
    cut = next(cut for cut in default.snapshot.fast_cuts if cut.media_id == photo.media_id)
    assert cut.output_duration_s == pytest.approx(1.2)

    # A stated length with little footage grows the photo only to its cap.
    stretched = plan_unified_montage(
        [clip(0, duration=2.0)], visuals=[photo], strategy={"target_duration_s": 30}
    )
    cut = next(cut for cut in stretched.snapshot.fast_cuts if cut.media_id == photo.media_id)
    assert (cut.source_start_s, cut.source_end_s) == (0.0, 3.0)
    assert cut.output_duration_s == pytest.approx(3.0)


def test_a_visuals_video_is_cut_from_its_own_length():
    video = visual(3, kind="video", duration=2.0)
    plan = plan_unified_montage([clip(0)], visuals=[video], strategy={"target_duration_s": 30})

    ref = next(ref for ref in plan.snapshot.media if ref.media_id == video.media_id)
    assert (ref.lane, ref.kind, ref.duration_s) == ("asset", "video", 2.0)
    cut = next(cut for cut in plan.snapshot.fast_cuts if cut.media_id == video.media_id)
    assert cut.source_end_s <= 2.0 + 1e-6
    compile_execution_plan(plan.guided_edit(), track=None)


def test_a_creator_literal_for_a_photo_uses_its_manifest_id():
    photo = visual(1)
    plan = plan_unified_montage(
        [clip(0), clip(1)],
        BriefView(wants_per_clip_text=True, clip_literals={photo.manifest_id: "Class of 2016"}),
        visuals=[photo],
    )
    labels = {label.media_id: label.text for label in plan.snapshot.clip_labels or []}
    assert labels[photo.media_id] == "Class of 2016"


def test_per_clip_text_reports_an_unlabelled_photo_honestly():
    photo = visual(1)
    brief = CreativeBrief(
        version=1,
        requirements=[BriefRequirement(id="r1", kind="text", scope="per_clip", description="x")],
    )
    plan = plan_unified_montage(
        [clip(0, landmark="Galata"), clip(1, landmark="Bebek")], brief_view(brief), visuals=[photo]
    )
    assert photo.media_id in plan.dropped_label_clip_ids
    receipt = build_receipts(brief.live(), plan_facts_from_unified_montage(plan.record()))[0]
    assert receipt.status == "partial"


def test_a_filmed_order_brief_reports_photos_as_not_in_filmed_order():
    photo = visual(1)
    plan = plan_unified_montage(
        [clip(0, minutes=5), clip(1, minutes=1)], BriefView(order_by_capture=True), visuals=[photo]
    )
    assert plan.ordering_basis == "capture_time"
    assert [cut.media_id for cut in plan.snapshot.fast_cuts] == ["c1", photo.media_id, "c0"]
    assert photo.media_id in plan.ordering_fallback_clip_ids


def test_the_creators_uncropped_photo_choice_is_kept_only_with_a_photo():
    plan = plan_unified_montage(
        [clip(0)], visuals=[visual(1)], strategy={"image_layout": "supporting_card"}
    )
    assert plan.snapshot.image_layout == "supporting_card"
    compiled = compile_execution_plan(plan.guided_edit(), track=None)
    still = next(moment for moment in compiled["story_timeline"] if moment["lane"] == "asset")
    assert still["layout"] == "supporting_card"

    clips_only = plan_unified_montage(
        [clip(0), clip(1)], strategy={"image_layout": "supporting_card"}
    )
    assert clips_only.snapshot.image_layout is None


def test_a_montage_without_visuals_records_exactly_as_before():
    plan = plan_unified_montage([clip(0), clip(1)])
    assert plan.visual_ids == []
    assert "visual_ids" not in plan.record()
    assert all(ref.lane == "clip" for ref in plan.snapshot.media)


def test_visuals_must_come_from_the_pool_and_be_unique():
    with pytest.raises(ValueError, match="Visuals pool"):
        plan_unified_montage([clip(0)], visuals=[clip(1)])
    with pytest.raises(ValueError, match="unique"):
        plan_unified_montage([clip(0)], visuals=[visual(1), visual(1)])


@pytest.mark.parametrize(
    ("strategy", "expected"),
    [
        (None, None),
        ({"media_scope": "all", "selected_media_ids": ["asset-x"]}, None),
        ({"media_scope": "selected", "selected_media_ids": []}, None),
        ({"media_scope": "selected"}, None),
        (
            {"media_scope": "selected", "selected_media_ids": ["c0", "asset-x"]},
            frozenset({"c0", "asset-x"}),
        ),
    ],
)
def test_only_an_explicit_selection_narrows_the_visuals(strategy, expected):
    assert selected_visual_ids(strategy) == expected


@pytest.mark.parametrize("duration", [0.298, 0.2983333, 0.21, 0.35])
def test_a_clip_shorter_than_any_cut_is_shown_whole(duration):
    """A 0.3s iPhone clip (thread 6BF1213E) failed the strict snapshot: whole
    frames stopped a fraction of a frame short of the clip."""
    plan = plan_unified_montage([clip(0), clip(1, duration=duration), clip(2)])

    cut = next(cut for cut in plan.snapshot.fast_cuts if cut.media_id == "c1")
    assert cut.source_start_s == 0.0
    assert duration - 0.001 <= cut.source_end_s <= duration
    compiled = compile_execution_plan(plan.guided_edit(), track=None)
    assert validate_execution_plan(compiled, plan.guided_edit()) == compiled


def test_the_reported_reunion_thread_plans_and_compiles():
    """Thread 6BF1213E as prod saw it: six iPhone videos (one of them 0.3s),
    two ready Visuals photos, a 15s fast montage."""
    durations = [18.943, 16.472, 3.235, 4.235, 0.298, 8.705]
    videos = [
        UnifiedClip(
            media_id=f"analysis-proxy-ios-{index}.mp4",
            proxy_path=f"users/u/creation-threads/t/analysis-proxy-ios-{index}.mp4",
            generation="1",
            duration_s=duration,
            width=1920,
            height=1080,
            orientation_degrees=90,
        )
        for index, duration in enumerate(durations)
    ]
    photos = [visual(1), visual(2)]
    plan = plan_unified_montage(videos, strategy={"target_duration_s": 15}, visuals=photos)

    assert plan.duration_s == pytest.approx(15.0, abs=0.05)
    assert _pattern(plan) == "CCPCCPCC"
    compiled = compile_execution_plan(plan.guided_edit(), track=None)
    assert validate_execution_plan(compiled, plan.guided_edit()) == compiled
    kinds = [(moment["lane"], moment["kind"]) for moment in compiled["story_timeline"]]
    assert kinds.count(("asset", "image")) == 2
    assert kinds.count(("clip", "video")) == 6


# --- KRI-306: the creator's explicit output shape --------------------------------------


@pytest.mark.parametrize("orientation", ["portrait", "landscape"])
def test_creator_output_orientation_pins_the_canvas_with_its_reason(orientation):
    plan = plan_unified_montage([clip(0), clip(1)], output_orientation=orientation)
    assert plan.snapshot.output_orientation == orientation
    assert plan.snapshot.output_orientation_reason == "The creator selected this output format."
    assert plan.guided_edit()["approved_proposal"]["output_orientation"] == orientation


def test_no_creator_choice_keeps_the_inferred_orientation_untouched():
    plan = plan_unified_montage([clip(0), clip(1)])  # two 1920x1080 clips
    assert plan.snapshot.output_orientation == "landscape"
    assert plan.snapshot.output_orientation_reason.startswith("Auto-selected landscape")
    # An unknown value is never trusted: it falls back to inference.
    odd = plan_unified_montage([clip(0), clip(1)], output_orientation="square")
    assert odd.snapshot.output_orientation == "landscape"
    assert odd.snapshot.output_orientation_reason.startswith("Auto-selected")


def test_a_vertical_choice_overrides_a_landscape_vote():
    plan = plan_unified_montage([clip(0), clip(1)], output_orientation="portrait")
    assert plan.snapshot.output_orientation == "portrait"
