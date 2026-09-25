"""Unit tests for the deterministic requirement checkers."""

from __future__ import annotations

import pytest

from .checkers import fold, min_display_s, nfc, run_checker
from .models import (
    ClipFactRecord,
    ClipRecord,
    FinalPlan,
    Footage,
    PlanClip,
    PlanText,
    Requirement,
)


def _req(checker: str, params: dict | None = None, **kw) -> Requirement:
    return Requirement(
        id=kw.pop("id", "r"),
        kind=kw.pop("kind", "text"),
        request_type=kw.pop("request_type", "title_exact"),
        checker=checker,
        params=params or {},
        **kw,
    )


def _footage(*clips: ClipRecord) -> Footage:
    return Footage(footage_id="f", clips=list(clips))


def _clip(clip_id: str, dur: float = 3.0, *, when: str | None = None, rank: int | None = None):
    facts = [ClipFactRecord(kind="capture_time", value=when, provenance="exif")] if when else []
    return ClipRecord(clip_id=clip_id, duration_s=dur, facts=facts, route_rank=rank)


def _plan(order: list[str], dur: float = 2.0, texts: list[PlanText] | None = None) -> FinalPlan:
    clips, cursor = [], 0.0
    for clip_id in order:
        clips.append(PlanClip(clip_id=clip_id, start_s=cursor, end_s=cursor + dur))
        cursor += dur
    return FinalPlan(clips=clips, texts=texts or [])


def _title(text: str) -> PlanText:
    return PlanText(id="t", role="title", text=text, start_s=0, end_s=2)


def _label(text: str, start: float, end: float, clip_id: str | None = None) -> PlanText:
    return PlanText(
        id=f"l{start}", role="label", text=text, start_s=start, end_s=end, clip_id=clip_id
    )


def _check(req: Requirement, plan: FinalPlan, footage: Footage | None = None, previous=None):
    return run_checker(req, plan, footage or _footage(), previous)


# ── text helpers ─────────────────────────────────────────────────────────────


def test_fold_handles_turkish_letters_and_case():
    assert fold("Kadıköy'de PAZAR") == fold("kadikoy'de pazar")
    assert fold("İstanbul") == fold("istanbul")
    assert nfc("Küche") == "Küche"  # NFC composes; never decomposes or folds


# ── title_exact ──────────────────────────────────────────────────────────────


def test_title_exact_met_on_identical_unicode_even_if_decomposed():
    req = _req("title_exact", {"literal": "Eminönü"})
    assert _check(req, _plan(["a"], texts=[_title("Eminönü")]))[0] == "met"
    assert _check(req, _plan(["a"], texts=[_title("Eminönü")]))[0] == "met"
    assert _check(req, _plan(["a"], texts=[_title("Eminönü")]))[0] == "met"


def test_title_exact_partial_when_only_diacritics_are_lost():
    req = _req("title_exact", {"literal": "Eminönü"})
    status, reason = _check(req, _plan(["a"], texts=[_title("Eminonu")]))
    assert status == "partial" and "diacritics" in reason


def test_title_exact_unmet_without_title():
    req = _req("title_exact", {"literal": "X"})
    assert _check(req, _plan(["a"]))[0] == "unmet"


# ── text_contains ────────────────────────────────────────────────────────────


def test_text_contains_ignores_diacritics_by_default_and_flags_them_when_strict():
    plan = _plan(["a"], texts=[_title("20k run from arnavutkoy to eminonu")])
    loose = _req("text_contains", {"terms": ["20k", "Arnavutköy", "Eminönü"]})
    strict = _req("text_contains", {"terms": ["Arnavutköy", "Eminönü"], "strict_unicode": True})
    assert _check(loose, plan)[0] == "met"
    status, reason = _check(strict, plan)
    assert status == "partial" and "diacritics lost" in reason


def test_text_contains_scope_and_missing_terms():
    plan = _plan(["a"], texts=[_title("Cappadocia"), _label("balloons", 0, 2)])
    assert (
        _check(_req("text_contains", {"terms": ["balloons"], "scope": "title"}), plan)[0] == "unmet"
    )
    assert (
        _check(_req("text_contains", {"terms": ["balloons"], "scope": "labels"}), plan)[0] == "met"
    )
    both = _req("text_contains", {"terms": ["Cappadocia", "Goreme"], "scope": "any"})
    assert _check(both, plan)[0] == "partial"
    assert (
        _check(_req("text_contains", {"terms": ["x"], "scope": "title"}), _plan(["a"]))[0]
        == "unmet"
    )


# ── labels ───────────────────────────────────────────────────────────────────


def test_label_coverage_uses_time_overlap_and_ignores_the_title():
    plan = _plan(["a", "b", "c", "d"], texts=[_title("T"), _label("first half", 0, 4)])
    status, reason = _check(_req("label_coverage"), plan)
    assert status == "partial" and "2/4" in reason
    full = _plan(["a", "b"], texts=[_label("one", 0, 2), _label("two", 2, 4)])
    assert _check(_req("label_coverage"), full)[0] == "met"
    assert (
        _check(_req("label_coverage"), _plan(["a"], texts=[_title("only a title")]))[0] == "unmet"
    )


def test_label_covers_a_clip_only_when_on_screen_for_half_of_it():
    barely = _plan(["a"], dur=4.0, texts=[_label("x", 0, 1.0)])
    enough = _plan(["a"], dur=4.0, texts=[_label("x", 0, 2.0)])
    assert _check(_req("label_coverage"), barely)[0] == "unmet"
    assert _check(_req("label_coverage"), enough)[0] == "met"


def test_label_bound_to_a_clip_id_ignores_timing():
    plan = _plan(["a", "b"], texts=[_label("A!", 3.5, 3.9, clip_id="a")])
    assert _check(_req("label_coverage", {"min_frac": 0.5}), plan)[0] == "met"


def test_label_exact_scores_each_named_clip():
    req = _req("label_exact", {"labels": {"a": "GOAL", "b": "Full Time"}})
    ok = _plan(["a", "b"], texts=[_label("GOAL", 0, 2, "a"), _label("Full Time", 2, 4, "b")])
    half = _plan(["a", "b"], texts=[_label("GOAL", 0, 2, "a")])
    folded = _plan(["a", "b"], texts=[_label("GOAL", 0, 2, "a"), _label("full time", 2, 4, "b")])
    assert _check(req, ok)[0] == "met"
    assert _check(req, half)[0] == "partial"
    assert _check(req, folded)[0] == "partial"
    assert _check(req, _plan(["a", "b"]))[0] == "unmet"


def test_label_mentions_accepts_any_alternative_term():
    req = _req("label_mentions", {"clips": {"a": ["lahmacun"], "b": ["dondurma", "ice cream"]}})
    plan = _plan(["a", "b"], texts=[_label("Fresh Lahmacun", 0, 2), _label("Ice cream!", 2, 4)])
    assert _check(req, plan)[0] == "met"
    assert _check(req, _plan(["a", "b"], texts=[_label("Fresh Lahmacun", 0, 2)]))[0] == "partial"


# ── order ────────────────────────────────────────────────────────────────────

_FOOT = _footage(
    _clip("a", when="2026-01-01T09:00:00+03:00", rank=1),
    _clip("b", when="2026-01-01T10:00:00+03:00", rank=2),
    _clip("c", when="2026-01-01T11:00:00+03:00", rank=3),
    _clip("d"),
)


def test_order_by_capture_time_met_partial_unmet():
    req = _req("order_by_key", {"key": "capture_time"}, kind="order")
    assert _check(req, _plan(["c", "a", "d", "b"]), _FOOT)[0] == "unmet"
    assert _check(req, _plan(["a", "d", "b", "c"]), _FOOT)[0] == "met"
    status, reason = _check(req, _plan(["a", "c", "b"]), _FOOT)
    assert status == "unmet" and "67%" in reason  # 2 of 3 pairs, below the 75% partial bar


def test_order_by_key_partial_band():
    foot = _footage(*[_clip(str(i), when=f"2026-01-01T{9 + i:02d}:00:00+00:00") for i in range(5)])
    req = _req("order_by_key", {"key": "capture_time"}, kind="order")
    # one adjacent swap out of 10 pairs -> 90% concordant
    assert _check(req, _plan(["0", "2", "1", "3", "4"]), foot)[0] == "partial"


def test_order_by_key_without_basis_is_unmet_and_says_why():
    req = _req("order_by_key", {"key": "capture_time"}, kind="order")
    bare = _footage(_clip("a"), _clip("b"))
    status, reason = _check(req, _plan(["a", "b"]), bare)
    assert status == "unmet" and "order_basis_unavailable" in reason


def test_order_by_route_rank_uses_first_occurrence_of_a_reused_clip():
    req = _req("order_by_key", {"key": "route_rank"}, kind="order")
    assert _check(req, _plan(["a", "b", "a", "c"]), _FOOT)[0] == "met"


def test_order_explicit_requires_every_named_clip():
    req = _req("order_explicit", {"sequence": ["a", "c"]}, kind="order")
    assert _check(req, _plan(["a", "b", "c"]))[0] == "met"
    status, reason = _check(req, _plan(["a", "b"]))
    assert status == "unmet" and "fewer than two" in reason
    three = _req("order_explicit", {"sequence": ["a", "b", "c"]}, kind="order")
    status, reason = _check(three, _plan(["a", "b"]))
    assert status == "partial" and "missing clips: c" in reason
    assert _check(req, _plan(["c", "a"]))[0] == "unmet"


# ── selection / duration / pacing ────────────────────────────────────────────


def test_selection_checkers():
    foot = _footage(_clip("a"), _clip("b"), _clip("c"), _clip("d"))
    every = _req("select_include", {"all": True}, kind="select")
    assert _check(every, _plan(["a", "b", "c", "d"]), foot)[0] == "met"
    assert _check(every, _plan(["a", "b", "c"]), foot)[0] == "partial"
    assert _check(every, _plan(["a"]), foot)[0] == "unmet"
    named = _req("select_include", {"clip_ids": ["a", "b"]}, kind="select")
    assert _check(named, _plan(["a"]), foot)[0] == "partial"
    skip = _req("select_exclude", {"clip_ids": ["a", "b"]}, kind="select")
    assert _check(skip, _plan(["c", "d"]), foot)[0] == "met"
    assert _check(skip, _plan(["a", "c"]), foot)[0] == "partial"
    assert _check(skip, _plan(["a", "b"]), foot)[0] == "unmet"


@pytest.mark.parametrize(
    ("actual", "status"),
    [
        (15.0, "met"),
        (16.4, "met"),
        (13.6, "met"),
        (16.6, "partial"),
        (18.7, "partial"),
        (19.0, "unmet"),
    ],
)
def test_duration_within_bands(actual, status):
    req = _req("duration_within", {"target_s": 15.0}, kind="timing")
    plan = FinalPlan(clips=[PlanClip(clip_id="a", start_s=0, end_s=actual)])
    assert _check(req, plan)[0] == status


def test_duration_uses_total_when_the_edit_has_a_tail():
    req = _req("duration_within", {"target_s": 15.0}, kind="timing")
    plan = FinalPlan(clips=[PlanClip(clip_id="a", start_s=0, end_s=15.0)], total_duration_s=16.6)
    assert _check(req, plan)[0] == "partial"


def test_pacing_average_clip_bands():
    req = _req("pacing_max_avg_clip", {"max_avg_clip_s": 2.0}, kind="timing")
    assert _check(req, _plan(["a", "b"], dur=2.0))[0] == "met"
    assert _check(req, _plan(["a", "b"], dur=2.9))[0] == "partial"
    assert _check(req, _plan(["a", "b"], dur=3.5))[0] == "unmet"
    assert _check(req, FinalPlan())[0] == "unmet"


# ── readability ──────────────────────────────────────────────────────────────


def test_min_display_rule_is_clamped():
    assert min_display_s("") == 1.2  # floor
    assert min_display_s("x" * 200) == 3.0  # ceiling
    assert min_display_s("x" * 20) == pytest.approx(2.0)


def test_readability_counts_labels_held_long_enough():
    req = _req("readability", kind="timing")
    text = "x" * 20  # needs 2.0s
    ok = _plan(["a", "b"], texts=[_label(text, 0, 2.0), _label(text, 2, 4.0)])
    half = _plan(["a", "b"], texts=[_label(text, 0, 2.0), _label(text, 2, 3.0)])
    assert _check(req, ok)[0] == "met"
    assert _check(req, half)[0] == "partial"
    status, reason = _check(req, _plan(["a"]))
    assert status == "unmet" and "no labels" in reason


# ── restructure / style ──────────────────────────────────────────────────────


def test_restructure_needs_a_different_edit():
    req = _req("restructure_changed", kind="select")
    before = _plan(["a", "b"], texts=[_title("T")])
    assert _check(req, before, previous=None)[0] == "unmet"
    assert _check(req, _plan(["a", "b"], texts=[_title("T")]), previous=before)[0] == "unmet"
    assert _check(req, _plan(["b", "a"], texts=[_title("T")]), previous=before)[0] == "met"
    assert _check(req, _plan(["a", "b"], texts=[_title("New")]), previous=before)[0] == "met"


def test_font_forbidden_is_case_insensitive_and_vacuous_without_styled_text():
    req = _req("font_forbidden", {"fonts": ["Fraunces"]}, kind="style")

    def text(font):
        return PlanText(id="t", role="title", text="x", start_s=0, end_s=1, font_family=font)

    assert _check(req, _plan(["a"], texts=[text("fraunces")]))[0] == "unmet"
    assert _check(req, _plan(["a"], texts=[text("Inter")]))[0] == "met"
    assert _check(req, _plan(["a"], texts=[text("Inter"), text("FRAUNCES")]))[0] == "partial"
    assert _check(req, _plan(["a"]))[0] == "met"


def test_unknown_checker_and_unknown_order_key_fail_loudly():
    with pytest.raises(KeyError, match="unknown checker"):
        _check(_req("nope"), _plan(["a"]))
    with pytest.raises(ValueError, match="unknown order key"):
        _check(_req("order_by_key", {"key": "vibes"}, kind="order"), _plan(["a", "b"]), _FOOT)


# ── P6b: receipts, dedupe, corrections, bulk fonts ───────────────────────────


def _labelled(*pairs: tuple[str, str], font: str | None = "Inter") -> FinalPlan:
    """Clips in order, each 2s, each with a label bound to its clip."""
    plan = _plan([c for c, _ in pairs])
    texts = [
        PlanText(
            id=f"l{i}",
            role="label",
            text=text,
            start_s=i * 2.0,
            end_s=i * 2.0 + 2.0,
            clip_id=clip,
            font_family=font,
        )
        for i, (clip, text) in enumerate(pairs)
        if text
    ]
    return plan.model_copy(update={"texts": texts})


def test_label_no_consecutive_repeat_compares_with_the_previous_kept_label():
    req = _req("label_no_consecutive_repeat", request_type="label_described")
    assert (
        _check(
            req, _labelled(("a", "Tern Lighthouse"), ("b", "Old Pier"), ("c", "Tern Lighthouse"))
        )[0]
        == "met"
    )
    # An unlabelled clip between two equal labels does not hide the repeat.
    assert _check(req, _labelled(("a", "Pier"), ("b", ""), ("c", "pier")))[0] != "met"
    status, reason = _check(
        req, _labelled(("a", "Pier"), ("b", "Pier"), ("c", "Pier"), ("d", "Fort"))
    )
    assert status == "partial" and "2 label(s)" in reason
    assert _check(req, _plan(["a"]))[0] == "unmet"


def test_label_no_consecutive_repeat_folds_case_and_diacritics():
    req = _req("label_no_consecutive_repeat", request_type="label_described")
    assert _check(req, _labelled(("a", "Kadıköy"), ("b", "KADIKOY")))[0] != "met"


def test_text_avoids_flags_mixed_language_and_is_vacuous_without_text():
    req = _req(
        "text_avoids",
        {"terms": ["Castle", "Valley"], "scope": "labels"},
        request_type="label_described",
    )
    assert _check(req, _labelled(("a", "Uçhisar Kalesi")))[0] == "met"
    assert _check(req, _plan(["a"]))[0] == "met"
    status, reason = _check(req, _labelled(("a", "Uçhisar Castle Kalesi")))
    assert status == "partial" and "Castle" in reason
    assert _check(req, _labelled(("a", "Castle Valley")))[0] == "unmet"


def test_labels_none_rejects_an_invented_label():
    req = _req("labels_none", request_type="label_described")
    assert _check(req, _plan(["a", "b"], texts=[_title("Five-a-side")]))[0] == "met"
    status, reason = _check(req, _labelled(("a", "Somewhere nice")))
    assert status == "unmet" and "made up" in reason


def test_font_all_equal_needs_every_lane_and_can_be_scoped_to_roles():
    plan = _labelled(("a", "One"), ("b", "Two"), font="Montserrat").model_copy(
        update={
            "texts": [
                *_labelled(("a", "One"), ("b", "Two"), font="Montserrat").texts,
                PlanText(id="t", role="title", text="T", start_s=0, end_s=2, font_family="Inter"),
            ]
        }
    )
    every = _req("font_all_equal", {"font": "montserrat"}, request_type="style")
    assert _check(every, plan)[0] == "partial"  # the title still says Inter
    labels = _req(
        "font_all_equal", {"font": "Montserrat", "roles": ["label"]}, request_type="style"
    )
    assert _check(labels, plan)[0] == "met"
    titles = _req(
        "font_all_equal", {"font": "Montserrat", "roles": ["title"]}, request_type="style"
    )
    assert _check(titles, plan)[0] == "unmet"
    assert _check(every, _plan(["a"]))[0] == "unmet"  # nothing to restyle is not a pass


def test_clips_unchanged_is_an_event_check_against_the_previous_edit():
    req = _req("clips_unchanged", request_type="style")
    before = _plan(["a", "b"])
    assert _check(req, before, previous=before)[0] == "met"
    assert _check(req, _plan(["b", "a"]), previous=before)[0] == "unmet"
    assert _check(req, before)[0] == "unmet"


def test_label_single_change_allows_only_the_named_label_to_move():
    req = _req(
        "label_single_change",
        {"clip_id": "b", "text": "Fort Halden"},
        request_type="label_exact",
    )
    before = _labelled(("a", "Pier"), ("b", "Watchtower"), ("c", "Beach"))
    assert (
        _check(
            req, _labelled(("a", "Pier"), ("b", "Fort Halden"), ("c", "Beach")), previous=before
        )[0]
        == "met"
    )
    status, reason = _check(
        req, _labelled(("a", "Harbour"), ("b", "Fort Halden"), ("c", "Beach")), previous=before
    )
    assert status == "partial" and "a" in reason
    assert _check(req, before, previous=before)[0] == "unmet"
    assert _check(req, before)[0] == "unmet"


def test_reply_states_reads_the_reply_not_the_edit():
    req = _req(
        "reply_states",
        {"all_of": ["reverse", "route order"], "none_of": ["re-?plan"]},
        request_type="order_route",
    )
    plan = _plan(["a"])
    full = "Filmed in the reverse of your route. Tell me if you want route order."
    assert run_checker(req, plan, _footage(), None, full)[0] == "met"
    assert run_checker(req, plan, _footage(), None, "It is the reverse.")[0] == "partial"
    assert run_checker(req, plan, _footage(), None, "Done.")[0] == "unmet"
    assert run_checker(req, plan, _footage(), None, None)[0] == "unmet"
    assert run_checker(req, plan, _footage(), None, full + " I had to re-plan.")[0] == "unmet"
    only_negative = _req("reply_states", {"none_of": ["reverse"]}, request_type="order_route")
    assert run_checker(only_negative, plan, _footage(), None, None)[0] == "met"
    assert run_checker(only_negative, plan, _footage(), None, "the reverse")[0] == "unmet"


def test_every_registered_checker_id_is_unique_across_plan_and_reply_registries():
    from .checkers import CHECKERS, REPLY_CHECKERS

    assert not set(CHECKERS) & set(REPLY_CHECKERS)
