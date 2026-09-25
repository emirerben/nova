"""KRI-208 / KRI-210 / KRI-209 / KRI-207: what the unified montage receipts say.

Every place name below is invented (same shape as a real route, no real data).
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from app.agents.landmark_guess import (
    LandmarkGuessAgent,
    LandmarkGuessInput,
    creator_language,
)
from app.kria.brief import BriefRequirement, CreativeBrief
from app.kria.brief_checks import (
    PlanFacts,
    build_receipts,
    check_requirement,
    plan_facts_from_unified_montage,
    reply_from_receipts,
)
from app.kria.contracts import RequirementReceipt
from app.pipeline.unified_montage import (
    UnifiedClip,
    brief_view,
    plan_unified_montage,
    skia_font_covers,
)

T0 = datetime(2026, 9, 20, 7, 0, tzinfo=UTC)
START = "Kumluca Köyü"
END = "Yeşilova Limanı"
START_PLACE = "Kumluca Köyü, Tepeli, Karadeniz İli, Türkiye"
END_PLACE = "Yeşilova Limanı, Bahçeli, Karadeniz İli, Türkiye"


def clip(index: int, *, minutes: int, place: str | None = None, landmark: str | None = None):
    rows = [
        {
            "kind": "capture_time",
            "value": (T0 + timedelta(minutes=minutes)).strftime("%Y-%m-%dT%H:%M:%SZ"),
            "provenance": "exif",
        }
    ]
    if place:
        rows.append({"kind": "place", "value": place, "provenance": "geocode"})
    if landmark:
        rows.append({"kind": "landmark", "value": landmark, "provenance": "inferred"})
    return UnifiedClip(
        media_id=f"c{index}",
        proxy_path=f"users/u/analysis-proxy-c{index}.mp4",
        generation="1",
        duration_s=5.0,
        width=1080,
        height=1920,
        facts=tuple(rows),
        capture_time=T0 + timedelta(minutes=minutes),
    )


def brief(*, start: str | None = START, end: str | None = END, key: str = "capture_time"):
    facts = {k: v for k, v in (("start", start), ("end", end)) if v}
    return CreativeBrief(
        version=3,
        requirements=[
            BriefRequirement(id="r1", kind="text", scope="per_clip", description="places"),
            BriefRequirement(id="r2", kind="order", scope="global", facts={"key": key, **facts}),
            BriefRequirement(id="r3", kind="text", scope="title", description="a title"),
        ],
    )


def receipts_for(clips, brief_):
    plan = plan_unified_montage(clips, brief_view(brief_))
    facts = plan_facts_from_unified_montage(plan.record())
    return plan, {r.requirement_id: r for r in build_receipts(brief_.live(), facts)}


# --------------------------------------------------------------------- KRI-208


def test_a_route_walked_in_reverse_is_named_not_silently_flipped():
    """The East Run shape: the creator said start -> end, the clips say end -> start."""
    clips = [  # attached in the creator's stated order, filmed in the opposite one
        clip(0, minutes=60, place=START_PLACE),
        clip(1, minutes=35, place="Ortaköy, Tepeli, Karadeniz İli, Türkiye"),
        clip(2, minutes=10, place=END_PLACE),
    ]
    plan, receipts = receipts_for(clips, brief())

    # No silent override in either direction: filming order was kept.
    assert plan.ordering_basis == "capture_time"
    assert plan.clip_ids == ["c2", "c1", "c0"]
    order = receipts["r2"]
    assert order.status == "partial"
    reason = order.reason or ""
    assert "filmed starting at Yeşilova Limanı and ending at Kumluca Köyü" in reason
    assert "reverse" in reason and "Kumluca Köyü → Yeşilova Limanı" in reason
    assert "I kept filming order" in reason and "tell me" in reason
    assert len(reason) <= 300
    # The creator-facing reply carries it as a plain sentence.
    text = reply_from_receipts(brief(), list(receipts.values()))
    assert "Not everything you asked for made it in" in text and "reverse" in text


def test_a_route_filmed_in_the_stated_direction_is_met():
    clips = [
        clip(0, minutes=10, place=START_PLACE),
        clip(1, minutes=35, place="Ortaköy, Tepeli, Karadeniz İli, Türkiye"),
        clip(2, minutes=60, place=END_PLACE),
    ]
    _plan, receipts = receipts_for(clips, brief())
    assert receipts["r2"].status == "met"


def test_route_names_match_without_turkish_letters_and_inside_longer_places():
    clips = [clip(0, minutes=10, place=END_PLACE), clip(1, minutes=60, place=START_PLACE)]
    _plan, receipts = receipts_for(clips, brief(start="kumluca koyu", end="Yesilova"))
    assert receipts["r2"].status == "partial"
    assert "reverse" in (receipts["r2"].reason or "")


def test_no_stated_route_or_no_place_facts_never_claims_a_reversal():
    reversed_clips = [clip(0, minutes=10, place=END_PLACE), clip(1, minutes=60, place=START_PLACE)]
    _p, no_route = receipts_for(reversed_clips, brief(start=None, end=None))
    assert no_route["r2"].status == "met"
    no_places = [clip(0, minutes=10), clip(1, minutes=60)]
    _p, blind = receipts_for(no_places, brief())
    assert blind["r2"].status == "met"


def test_a_place_that_matches_both_ends_is_not_a_contradiction():
    # A city-level name matches every clip: forward evidence exists, so stay quiet.
    same_city = "Tepeli, Karadeniz İli, Türkiye"
    clips = [clip(0, minutes=10, place=same_city), clip(1, minutes=60, place=same_city)]
    _p, receipts = receipts_for(clips, brief(start="Tepeli", end="Yeşilova Limanı"))
    assert receipts["r2"].status == "met"


def test_the_landmark_can_show_the_reversal_when_the_geocode_is_silent():
    clips = [
        clip(0, minutes=10, landmark="Yeşilova Limanı"),
        clip(1, minutes=60, landmark="Kumluca Köyü"),
    ]
    _p, receipts = receipts_for(clips, brief())
    assert receipts["r2"].status == "partial"


def test_a_reversal_is_only_judged_on_a_capture_time_order():
    clips = [clip(0, minutes=10, place=END_PLACE), clip(1, minutes=60, place=START_PLACE)]
    plan = plan_unified_montage(clips, brief_view(brief(key="alphabetical")))
    assert plan.ordering_basis == "attachment"
    facts = plan_facts_from_unified_montage(plan.record())
    receipt = check_requirement(brief(key="alphabetical").requirements[1], facts)
    assert "reverse" not in (receipt.reason or "")


def test_the_receipt_reads_the_route_off_the_order_requirement_too():
    """No brief-level start/end in the plan record: the requirement's own facts count."""
    record = {
        "clip_ids": ["a", "b"],
        "ordering_basis": "capture_time",
        "endpoint_places": {
            "first": {"label": "Yeşilova Limanı", "places": [{"text": END_PLACE}]},
            "last": {"label": "Kumluca Köyü", "places": [{"text": START_PLACE}]},
        },
    }
    req = BriefRequirement(
        id="r9", kind="order", scope="global", facts={"key": "route", "from": START, "to": END}
    )
    receipt = check_requirement(req, plan_facts_from_unified_montage(record))
    assert receipt.status == "partial" and "reverse" in (receipt.reason or "")


# --------------------------------------------------------------------- KRI-210


def test_consecutive_identical_labels_are_dropped_as_repeats_and_counted_apart():
    clips = [
        clip(0, minutes=1, landmark="Bosphorus Strait"),
        clip(1, minutes=2, landmark="Bosphorus  strait"),
        clip(2, minutes=3, landmark="Bosphorus Strait"),
        clip(3, minutes=4, landmark="Galata Bridge"),
        clip(4, minutes=5, landmark="Bosphorus Strait"),  # not consecutive: kept
        clip(5, minutes=6),  # no grounded fact
    ]
    brief_ = brief(start=None, end=None)
    plan, receipts = receipts_for(clips, brief_)

    assert [label.text for label in plan.snapshot.clip_labels or []] == [
        "Bosphorus Strait",
        "Galata Bridge",
        "Bosphorus Strait",
    ]
    record = plan.record()
    assert record["dropped_label_reasons"] == {"c1": "repeat", "c2": "repeat", "c5": "no_fact"}
    assert set(record["dropped_label_clip_ids"]) == {"c1", "c2", "c5"}
    reason = receipts["r1"].reason or ""
    assert receipts["r1"].status == "partial" and "3 of 6" in reason
    assert "2 more repeated the label before" in reason


def test_a_creators_own_repeated_words_are_never_deduped():
    facts = ({"kind": "creator", "value": "Km 5", "provenance": "creator"},)
    rows = [UnifiedClip(**{**clip(i, minutes=i).__dict__, "facts": facts}) for i in range(3)]
    plan = plan_unified_montage(rows, brief_view(brief(start=None, end=None)))
    assert [label.text for label in plan.snapshot.clip_labels or []] == ["Km 5"] * 3


def test_a_brief_sourced_title_is_met_and_a_default_one_is_not():
    clips = [clip(0, minutes=1, place=START_PLACE), clip(1, minutes=2, place=END_PLACE)]
    brief_ = CreativeBrief(
        version=1,
        requirements=[
            BriefRequirement(
                id="t1",
                kind="text",
                scope="title",
                description="a title",
                facts={"activity": "run", "distance_km": 20, "start": START, "end": END},
            ),
        ],
    )
    plan = plan_unified_montage(clips, brief_view(brief_))
    assert plan.title_source == "brief"
    receipt = build_receipts(brief_.live(), plan_facts_from_unified_montage(plan.record()))[0]
    assert receipt.status == "met"

    default = plan_unified_montage(clips, brief_view(None))
    assert default.title_source == "default"
    weak = build_receipts(brief_.live(), plan_facts_from_unified_montage(default.record()))[0]
    assert weak.status == "partial" and "default title" in (weak.reason or "")


def test_a_title_of_unknown_origin_stays_neutral_not_a_failure_notice():
    req = BriefRequirement(id="t1", kind="text", scope="title", description="a title")
    receipt = check_requirement(req, PlanFacts(title="Anything"))
    assert receipt.status == "partial"
    brief_ = CreativeBrief(version=1, requirements=[req])
    text = reply_from_receipts(brief_, [receipt], summary="Done.")
    assert text.startswith("Done.") and "Not everything" not in text


def test_a_title_literal_only_has_to_appear_in_a_brief_written_title():
    req = BriefRequirement(id="t2", kind="text", scope="title", literal="20k run")
    written = PlanFacts(title="20k run · A → B", title_source="brief")
    assert check_requirement(req, written).status == "met"
    creator_exact = PlanFacts(title="20K RUN", title_source="creator")
    assert check_requirement(req, creator_exact).status == "met"
    other = PlanFacts(title="A long run", title_source="creator")
    assert check_requirement(req, other).status == "partial"


def test_the_outro_is_named_only_when_the_record_says_the_video_carries_one():
    """KRI-210: 28.0s of edit becomes a ~29.6s file (the 1.6s Kria outro), but the phone
    declares its tail at export, after the plan's receipts exist: never assume it."""
    req = BriefRequirement(id="d1", kind="timing", scope="global", facts={"duration_s": 40})
    record = {"clip_ids": ["a"], "duration_s": 28.0, "ordering_basis": "attachment"}

    plan_time = plan_facts_from_unified_montage(record)
    assert plan_time.duration_s == 28.0 and plan_time.outro_s == 0.0
    miss = check_requirement(req, plan_time)
    assert miss.status == "partial" and "outro" not in (miss.reason or "")
    assert "28s; you asked for 40s" in (miss.reason or "")

    declared = plan_facts_from_unified_montage({**record, "brand_tail": "standard"})
    assert declared.outro_s == pytest.approx(1.6)
    assert "28s (plus a 1.6s outro on the finished video)" in (
        check_requirement(req, declared).reason or ""
    )
    stored = plan_facts_from_unified_montage({**record, "brand_tail_s": 1.6})
    assert stored.outro_s == pytest.approx(1.6)
    none = plan_facts_from_unified_montage({**record, "brand_tail": "none"})
    assert none.outro_s == 0.0
    # The comparison is always against the edit's own length.
    ok = BriefRequirement(id="d2", kind="timing", scope="global", facts={"duration_s": 28})
    assert check_requirement(ok, declared).status == "met"


# --------------------------------------------------------------------- KRI-207


def test_guessed_names_travel_with_their_clip_and_the_plain_strings_stay():
    clips = [
        clip(0, minutes=1, landmark="Mavi Köprü"),
        clip(1, minutes=2, place="Ortaköy, Tepeli, Karadeniz İli, Türkiye"),
        clip(2, minutes=3, landmark="Yeşil Kule"),
    ]
    _plan, receipts = receipts_for(clips, brief(start=None, end=None))
    receipt = receipts["r1"]
    assert receipt.status == "met"
    assert receipt.inferred == ["Mavi Köprü", "Yeşil Kule"]
    assert [(g.text, g.media_id, g.clip_index) for g in receipt.inferred_labels] == [
        ("Mavi Köprü", "c0", 0),
        ("Yeşil Kule", "c2", 2),
    ]
    dumped = receipt.model_dump(mode="json")
    assert RequirementReceipt.model_validate(dumped) == receipt
    # A receipt stored before the field existed still validates.
    legacy = {"requirement_id": "r1", "status": "met", "reason": None, "inferred": ["X"]}
    assert RequirementReceipt.model_validate(legacy).inferred_labels == []


def test_a_scoped_clip_requirement_names_only_its_own_guess():
    clips = [clip(0, minutes=1, landmark="Mavi Köprü"), clip(1, minutes=2, landmark="Yeşil Kule")]
    req = BriefRequirement(id="s1", kind="text", scope="clip:c1", description="name it")
    plan = plan_unified_montage(clips, brief_view(brief(start=None, end=None)))
    receipt = check_requirement(req, plan_facts_from_unified_montage(plan.record()))
    assert receipt.inferred == ["Yeşil Kule"]
    assert [(g.media_id, g.clip_index) for g in receipt.inferred_labels] == [("c1", 1)]


# --------------------------------------------------------------------- landmark language


def test_creator_language_reads_stopwords_not_proper_nouns():
    assert creator_language("") == ""
    assert creator_language("   ") == ""
    assert creator_language("my run from Eminönü to Arnavutköy") == "en"
    assert creator_language("Arnavutköy'den Eminönü'ne koşu ve bir yol") == "tr"
    assert creator_language("Eminönü Arnavutköy") == "und"
    # The stopword set is folded like the input ("başladım" is not a lost match)...
    assert creator_language("sabah başladım, nasıl bir yol") == "tr"
    # ...and "run" is a loanword in Turkish running-event names: not English evidence.
    assert creator_language("20k run") == "und"
    assert creator_language("Arnavutköy 20k run ve bir yol") == "tr"


def test_the_language_rule_is_injected_only_when_the_creator_wrote_something():
    agent = LandmarkGuessAgent(None)  # type: ignore[arg-type]
    base = {"file_uri": "files/x", "place": "Sarıyer, İstanbul", "lat": 41.09, "lon": 29.06}
    plain = agent.render_prompt(LandmarkGuessInput(**base))
    assert "LANGUAGE" not in plain and "Creator text" not in plain and "$" not in plain
    written = agent.render_prompt(LandmarkGuessInput(**base, creator_text="my run  from A to B"))
    assert "LANGUAGE" in written and 'Creator text: "my run from A to B"' in written
    assert "never a mix" in written


def test_the_landmark_prompt_version_moved_with_its_text():
    assert LandmarkGuessAgent.spec.prompt_version == "2026-09-25.1"


def test_which_recorded_guesses_count_as_already_asked():
    from app.services import clip_facts as cf

    entry = {"media_id": "a", "storage_generation": "7"}
    bare = cf.with_landmark_fact(entry, None)
    tagged = cf.with_landmark_fact(entry, None, language="en")
    assert bare["analysis"][cf.LANDMARK_GENERATION_KEY] == "7"
    assert tagged["analysis"][cf.LANDMARK_GENERATION_KEY] == "7|en"
    # The edit-proposal path (no creator language) never re-asks and never overwrites:
    # any entry for this generation counts, bare or tagged.
    assert cf._landmark_attempted(bare) and cf._landmark_attempted(tagged)
    # Text in a language we cannot name shares the bare entry.
    assert cf._landmark_attempted(bare, "und")
    # A known language wants its own tagged entry: a bare guess is asked again once.
    assert not cf._landmark_attempted(bare, "en")
    assert cf._landmark_attempted(tagged, "en") and cf._landmark_attempted(tagged, "tr")
    # A new generation of the file never reuses an old answer.
    assert not cf._landmark_attempted({**tagged, "storage_generation": "8"})
    assert not cf._landmark_attempted(entry)


def _landmark_entry(*, key: str | None = "7"):
    from app.schemas.clip_understanding import ClipFact
    from app.services import clip_facts as cf

    entry = {
        "media_id": "a",
        "storage_generation": "7",
        "capture": {"place": {"locality": "Ortaköy"}, "capture_time": "2026-09-20T07:31:02Z"},
    }
    entry = cf.with_landmark_fact(
        entry, ClipFact(kind="landmark", value="Mavi Köprü", provenance="inferred")
    )
    if key is not None:
        entry["analysis"][cf.LANDMARK_GENERATION_KEY] = key
    return entry


def _ref():
    return type("Ref", (), {"kind": "video", "analysis": {}})()


ENGLISH = "my run from A to B"


def test_a_bare_key_landmark_is_re_asked_once_with_a_known_language_then_reused(monkeypatch):
    from app.schemas.clip_understanding import ClipFact
    from app.services import clip_facts as cf

    calls: list[str] = []

    def guess(assignment, *, ctx, creator_text=""):
        calls.append(creator_text)
        return ClipFact(kind="landmark", value="Blue Bridge", provenance="inferred")

    monkeypatch.setattr(cf, "_guess_landmark", guess)
    persisted: dict = {}
    first = cf.enrich_clip_facts(
        [(_landmark_entry(key="7"), _ref())],
        make_ctx=lambda m: object(),
        creator_text=ENGLISH,
        on_updated=lambda entry, ref: persisted.update(entry=entry),
    )
    assert calls == [ENGLISH], "the bare-key guess predates the creator's language: ask again"
    assert cf.landmark_fact_for_assignment(first[0][0]).value == "Blue Bridge"
    assert persisted["entry"]["analysis"][cf.LANDMARK_GENERATION_KEY] == "7|en"

    cf.enrich_clip_facts(
        [(persisted["entry"], _ref())], make_ctx=lambda m: object(), creator_text=ENGLISH
    )
    assert calls == [ENGLISH], "a second render must not call the agent"


def test_an_unnamed_language_reuses_the_bare_key_with_no_agent_call(monkeypatch):
    from app.services import clip_facts as cf

    monkeypatch.setattr(
        cf, "_guess_landmark", lambda *a, **k: pytest.fail("the agent ran for a cached clip")
    )
    out = cf.enrich_clip_facts(
        [(_landmark_entry(key="7"), _ref())],
        make_ctx=lambda m: object(),
        creator_text="Eminönü Arnavutköy",  # names only: language "und"
    )
    assert cf.landmark_fact_for_assignment(out[0][0]).value == "Mavi Köprü"
    assert out[0][0]["analysis"][cf.LANDMARK_GENERATION_KEY] == "7"


def test_the_proposal_path_leaves_a_language_tagged_entry_alone(monkeypatch):
    """edit_proposal_build enriches with NO creator text, before any render."""
    from app.services import clip_facts as cf

    monkeypatch.setattr(
        cf, "_guess_landmark", lambda *a, **k: pytest.fail("the proposal path re-asked")
    )
    entry = _landmark_entry(key="7|en")
    out = cf.enrich_clip_facts([(entry, _ref())], make_ctx=lambda m: object())
    assert out[0][0]["analysis"][cf.LANDMARK_GENERATION_KEY] == "7|en"
    assert cf.landmark_fact_for_assignment(out[0][0]).value == "Mavi Köprü"


def test_an_unknown_answer_never_deletes_a_landmark_found_for_the_same_generation(monkeypatch):
    from app.services import clip_facts as cf

    monkeypatch.setattr(cf, "_guess_landmark", lambda *a, **k: None)
    out = cf.enrich_clip_facts(
        [(_landmark_entry(key="7"), _ref())], make_ctx=lambda m: object(), creator_text=ENGLISH
    )
    kept = cf.landmark_fact_for_assignment(out[0][0])
    assert kept is not None and kept.value == "Mavi Köprü"
    assert out[0][0]["analysis"][cf.LANDMARK_GENERATION_KEY] == "7|en"
    # A NEW generation of the file does drop the old file's landmark.
    moved = {**_landmark_entry(key="7"), "storage_generation": "8"}
    assert (
        cf.landmark_fact_for_assignment(cf.with_landmark_fact(moved, None, language="en")) is None
    )


def test_enrich_passes_the_creators_words_to_the_agent_and_keys_the_answer(monkeypatch):
    from app.services import clip_facts as cf

    seen: list[str] = []

    def guess(assignment, *, ctx, creator_text=""):
        seen.append(creator_text)
        return None

    monkeypatch.setattr(cf, "_guess_landmark", guess)
    capture = {"place": {"locality": "Ortaköy"}, "capture_time": "2026-09-20T07:31:02Z"}
    entry = {"media_id": "a", "storage_generation": "7", "capture": capture}
    ref = type("Ref", (), {"kind": "video", "analysis": {}})()
    out = cf.enrich_clip_facts(
        [(entry, ref)], make_ctx=lambda m: object(), creator_text="my run from A to B"
    )
    assert seen == ["my run from A to B"]
    assert out[0][0]["analysis"][cf.LANDMARK_GENERATION_KEY] == "7|en"


# --------------------------------------------------------------------- KRI-209


def test_unified_montage_needs_authored_text_only_for_its_variable_font_coordinates(monkeypatch):
    """KRI-209: `PHONE_RENDER_VERIFIED_FEATURES` defaults to [] and prod's value is a Fly
    secret, so which capabilities an East Run-shaped plan needs is worth pinning.

    Finding: its labels use none of the authored-text *features* (animation phases,
    backgrounds, caption pop). But the default typography is the variable Fraunces face
    whose runs carry `font_variations` (opsz/wght), and `EditRecipeV2` adds `authoredText`
    for any such run. So the capability IS required, and a missing `authoredText` in the
    secret fails every default-styled montage at the rollout gate. Prod has it (read
    2026-09-22, tests/_prod_profile.py); `GET /admin/kria/phone-render-config` confirms."""
    from app.config import Settings, settings
    from app.kria.media_sources import OriginalMediaDescriptor
    from app.pipeline.guided_story import GuidedStoryExecutionPlan, compile_execution_plan
    from app.pipeline.phone_guided_plan import compile_phone_guided_plan
    from app.services.phone_rollout import validate_phone_pilot_recipe
    from app.services.phone_sources import PhoneSourceBinding

    assert Settings.model_fields["phone_render_verified_features"].default_factory() == []
    clips = [
        clip(0, minutes=10, place=START_PLACE, landmark="Mavi Köprü"),
        clip(1, minutes=35, landmark="Yeşil Kule"),
        clip(2, minutes=60, place=END_PLACE),
    ]
    plan = plan_unified_montage(clips, brief_view(brief()), font_covers=skia_font_covers)
    execution = GuidedStoryExecutionPlan.model_validate(
        compile_execution_plan(plan.guided_edit(), track=None)
    )
    bindings = tuple(
        PhoneSourceBinding(
            media_id=c.media_id,
            proxy_path=c.proxy_path,
            generation=c.generation,
            original=OriginalMediaDescriptor(
                sha256="a" * 64,
                byte_count=1000,
                duration_s=c.duration_s,
                width=1080,
                height=1920,
                has_audio=True,
            ),
        )
        for c in clips
    )
    recipe = compile_phone_guided_plan(execution, bindings)

    assert recipe.text_layers, "the title and labels must actually be drawn"
    # None of the authored-text features a creator would think of as "styled text"...
    assert all(
        layer.animation_phases is None
        and layer.background is None
        and layer.effect != "caption-pop"
        and layer.karaoke is None
        for layer in recipe.text_layers
    )
    # ...the capability comes from the variable font's coordinates alone.
    assert any(run.font_variations for layer in recipe.text_layers for run in layer.runs)
    assert "authoredText" in recipe.required_capabilities

    enabled = sorted(recipe.required_capabilities)
    monkeypatch.setattr(settings, "phone_render_verified_features", enabled)
    validate_phone_pilot_recipe(recipe)
    missing = [name for name in enabled if name != "authoredText"]
    monkeypatch.setattr(settings, "phone_render_verified_features", missing)
    with pytest.raises(ValueError, match="phone capability that is not enabled"):
        validate_phone_pilot_recipe(recipe)


# --------------------------------------------------------------------- review follow-ups


def test_an_untimed_endpoint_says_nothing_about_direction():
    """The first/last clip has no capture time: it sits in an attachment slot."""
    clips = [
        UnifiedClip(
            **{
                **clip(0, minutes=10, place=END_PLACE).__dict__,
                "capture_time": None,
                "facts": ({"kind": "place", "value": END_PLACE, "provenance": "geocode"},),
            }
        ),
        clip(1, minutes=30, place="Ortaköy, Tepeli, Karadeniz İli, Türkiye"),
        clip(2, minutes=60, place=START_PLACE),
    ]
    plan = plan_unified_montage(clips, brief_view(brief()))
    assert plan.ordering_fallback_clip_ids == ["c0"]
    receipt = build_receipts(brief().live(), plan_facts_from_unified_montage(plan.record()))[1]
    assert "reverse" not in (receipt.reason or "")


def test_a_clip_scoped_label_dropped_as_a_repeat_says_so():
    clips = [
        clip(0, minutes=1, landmark="Mavi Köprü"),
        clip(1, minutes=2, landmark="Mavi Köprü"),
        clip(2, minutes=3, landmark="Yeşil Kule"),
    ]
    plan = plan_unified_montage(clips, brief_view(brief(start=None, end=None)))
    facts = plan_facts_from_unified_montage(plan.record())
    req = BriefRequirement(id="s9", kind="text", scope="clip:c1", description="name it")
    receipt = check_requirement(req, facts)
    assert receipt.status == "partial"
    assert "repeated the clip before it" in (receipt.reason or "")
    other = BriefRequirement(id="s8", kind="text", scope="clip:c2", description="name it")
    assert check_requirement(other, facts).status == "met"
