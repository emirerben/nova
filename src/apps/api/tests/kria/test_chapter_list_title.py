"""KRI-545: a chapter-titles ask never becomes the opening title.

Prod 2026-10-08, phone Montage, stress kit M3 "Berlin student day": "... Bölüm başlıkları koy:
Sabah, Üniversite, Öğle arası, Spor, Akşam ..." ("add section titles: Morning, University, Lunch
break, Gym, Evening"). The brief kept the whole list as one global text literal (r2, description
"Bölüm başlıkları") and the planner resolved one caption intent per chapter. Every chapter label
rendered on its clips, but `_title` also turned the global literal into a centred opening title
("Sabah, Üniversite, Öğle arası, Spor, Akşam", `guided-title`, 0-2.2 s) stacked on the "Sabah"
label.

Failure modes this file is written against:

* the joined list burns as an opening title on top of the first chapter label;
* with the title gone, r2 reads "That exact text isn't in this draft" and the bound-brief
  receipts block the render;
* with the title gone, the pinned render contract (`exact_texts`, role "any", the joined literal)
  refuses the phone recipe as "missing confirmed on-screen text";
* the "what are the title's words?" gate treats the chapter list as the title's words;
* a real title the creator asked for disappears, or a list naming text that is NOT on the clips
  is accepted as if it were.

The render side is the real `plan_unified_montage` -> receipts -> `compile_execution_plan` ->
`compile_phone_guided_plan` -> `verify_phone_recipe`.
"""

from __future__ import annotations

import pytest

from app.kria import plan_blocks
from app.kria.brief import BriefRequirement, CreativeBrief
from app.kria.brief_checks import (
    build_receipts,
    plan_facts_from_phone_variant,
    plan_facts_from_unified_montage,
)
from app.kria.brief_route import chapter_list
from app.kria.media_sources import OriginalMediaDescriptor
from app.pipeline.guided_story import GuidedStoryExecutionPlan, compile_execution_plan
from app.pipeline.phone_guided_plan import compile_phone_guided_plan
from app.pipeline.unified_montage import (
    _title,
    brief_view,
    plan_unified_montage,
    title_source_exists,
)
from app.services.creator_render_contract import (
    CreatorRenderContract,
    CreatorRenderContractError,
    TextRequirement,
    build_render_contract,
    verify_phone_recipe,
)
from app.services.phone_sources import PhoneSourceBinding
from tests.pipeline.test_unified_montage import clip

CHAPTERS = "Sabah, Üniversite, Öğle arası, Spor, Akşam"
# Clip -> chapter, in filming order, like the prod job (two clips for some chapters, the
# closing speech clip unlabelled).
SEATS = {
    "c0": "Sabah",
    "c1": "Sabah",
    "c2": "Üniversite",
    "c3": "Öğle arası",
    "c4": "Spor",
    "c5": "Akşam",
}
CLIP_COUNT = 7


def _caption(intent_id: str, text: str, media_ids: list[str]) -> dict:
    """A resolved caption intent as the prod turn carried it (creator_text grounding)."""
    return {
        "op": "caption",
        "status": "resolved",
        "attribute": text,
        "intent_id": intent_id,
        "assignments": [
            {"evidence": "matched", "media_id": media_id, "confidence": 0.9}
            for media_id in media_ids
        ],
        "caption_text": text,
        "creator_text": text,
        "caption_grounding": "creator_text",
    }


def _strategy(seats: dict[str, str] = SEATS) -> dict:
    by_text: dict[str, list[str]] = {}
    for media_id, text in seats.items():
        by_text.setdefault(text, []).append(media_id)
    return {
        "resolved_clip_intents": [
            _caption(f"i{n + 2}", text, ids) for n, (text, ids) in enumerate(by_text.items())
        ]
    }


def _brief(
    *extra: BriefRequirement, literal: str = CHAPTERS, scope: str = "global"
) -> CreativeBrief:
    return CreativeBrief(
        version=1,
        requirements=[
            BriefRequirement(
                id="r1",
                kind="order",
                scope="global",
                description="Videoları çektiğim saat sırasına göre diz, sabahtan geceye",
                facts={"key": "capture_time"},
            ),
            BriefRequirement(
                id="r2", kind="text", scope=scope, description="Bölüm başlıkları", literal=literal
            ),
            *extra,
        ],
    )


def _plan(strategy: dict, brief: CreativeBrief):  # noqa: ANN202
    return plan_unified_montage(
        [clip(i, minutes=i) for i in range(CLIP_COUNT)],
        brief_view(brief),
        strategy=strategy,
        clip_intents_enabled=True,
    )


def _labels(plan) -> dict[str, str]:  # noqa: ANN001
    return {label.media_id: label.text for label in plan.snapshot.clip_labels or []}


def _receipts(brief: CreativeBrief, plan) -> dict[str, tuple[str, str]]:  # noqa: ANN001
    rows = build_receipts(
        brief.live(), plan_facts_from_unified_montage(plan.record()), include_unchecked=True
    )
    return {r.requirement_id: (r.status, r.verification) for r in rows}


def _recipe(compiled: dict):  # noqa: ANN202
    bindings = tuple(
        PhoneSourceBinding(
            media_id=f"c{i}",
            proxy_path=f"users/u/analysis-proxy-c{i}.mp4",
            generation="1",
            original=OriginalMediaDescriptor(
                sha256="a" * 64,
                byte_count=1000,
                duration_s=5,
                width=1920,
                height=1080,
                has_audio=True,
            ),
        )
        for i in range(CLIP_COUNT)
    )
    return compile_phone_guided_plan(GuidedStoryExecutionPlan.model_validate(compiled), bindings)


def _texts(**kwargs) -> CreatorRenderContract:  # noqa: ANN003
    return CreatorRenderContract(generation_id="g").rebind(
        exact_texts=(TextRequirement(role="any", **kwargs),)
    )


# -- the incident, end to end ------------------------------------------------------------


def test_berlin_chapter_titles_print_on_their_clips_and_never_as_an_opening_title() -> None:
    brief = _brief()
    plan = _plan(_strategy(), brief)

    # No opening title from the chapter list; every chapter name is on its own clips.
    assert plan.title is None and plan.title_source == "none"
    assert plan.snapshot.opening_title is None
    assert _labels(plan) == SEATS
    record = plan.record()
    assert record["title"] is None and record["title_source"] == "none"

    # r2 still reads as met (the names are on screen), so the bound brief does not block.
    assert _receipts(brief, plan)["r2"] == ("met", "checked")

    compiled = compile_execution_plan(plan.guided_edit(), track=None)
    assert all(e["id"] != "guided-title" for e in compiled["text_elements"])
    labels = [e for e in compiled["text_elements"] if e["id"].startswith("clip-label-")]
    assert [e["text"] for e in labels] == list(SEATS.values())
    # The first thing on screen is the "Sabah" chapter label, alone.
    assert [e["text"] for e in compiled["text_elements"] if e["start_s"] == 0.0] == ["Sabah"]
    # The plan card's title row says "Not used", not the first chapter name.
    blocks = {b["section_id"]: b for b in plan_blocks.blocks_from_guided_plan(compiled)}
    assert blocks["title"]["skipped"] is True

    # The finished phone render's own text lanes satisfy r2 too (KRI-541 rendered facts).
    rendered = build_receipts(
        brief.live(),
        plan_facts_from_phone_variant({"text_elements": compiled["text_elements"]}),
        include_unchecked=True,
    )
    assert {r.requirement_id: r.status for r in rendered}["r2"] == "met"

    # The approval contract pins the brief literal as role "any" exact text (prod shape);
    # the phone recipe, which draws the names as separate label layers, still verifies.
    contract = build_render_contract(
        {},
        generation_id="g",
        brief=CreativeBrief(
            version=1, requirements=[r for r in brief.requirements if r.id == "r2"]
        ),
    )
    assert contract is not None
    assert [(t.role, t.text) for t in contract.exact_texts] == [("any", CHAPTERS)]
    assert verify_phone_recipe(contract, _recipe(compiled))


def test_a_listed_name_missing_from_the_clips_is_not_on_screen() -> None:
    """The phone verifier accepts the list only when EVERY name is a whole visible layer."""
    compiled = compile_execution_plan(_plan(_strategy(), _brief()).guided_edit(), track=None)
    recipe = _recipe(compiled)
    assert verify_phone_recipe(_texts(text="Sabah, Akşam"), recipe)
    for text in ("Sabah, Gece", "Sabah Akşam", "Sabah"[:3] + ", Akşam"):
        with pytest.raises(CreatorRenderContractError, match="missing"):
            verify_phone_recipe(_texts(text=text), recipe)
    # A list never satisfies a role that pins one place or a hold time.
    with pytest.raises(CreatorRenderContractError, match="missing"):
        verify_phone_recipe(
            CreatorRenderContract(generation_id="g").rebind(
                exact_texts=(TextRequirement(role="opening", text="Sabah, Akşam"),)
            ),
            recipe,
        )


# -- what still is a title ----------------------------------------------------------------


def test_an_explicit_title_still_opens_the_chapter_edit() -> None:
    title = BriefRequirement(
        id="r9", kind="text", scope="title", literal="Berlin'de Öğrenci Bir Günüm"
    )
    brief = _brief(title)
    plan = _plan(_strategy(), brief)
    assert plan.title == "Berlin'de Öğrenci Bir Günüm" and plan.title_source == "creator"
    assert _labels(plan) == SEATS
    compiled = compile_execution_plan(plan.guided_edit(), track=None)
    blocks = {b["section_id"]: b for b in plan_blocks.blocks_from_guided_plan(compiled)}
    assert blocks["title"]["summary"] == "Berlin'de Öğrenci Bir Günüm"
    receipts = _receipts(brief, plan)
    assert receipts["r2"] == ("met", "checked") and receipts["r9"] == ("met", "checked")


def test_a_confirmed_opening_title_is_kept_next_to_the_chapters() -> None:
    plan = _plan({**_strategy(), "opening_title": "Bir Günüm"}, _brief())
    assert plan.title == "Bir Günüm"


def test_a_chapter_list_filed_as_the_title_is_still_chapters() -> None:
    """ "Başlık" reads as "title": the same list under the title scope is not burned either."""
    brief = _brief(scope="title")
    plan = _plan(_strategy(), brief)
    assert plan.title is None
    assert _receipts(brief, plan)["r2"] == ("met", "checked")


def test_a_literal_that_is_not_only_the_clip_labels_still_stands() -> None:
    # One name is on no clip: the literal is not just the chapter labels, so it is unchanged.
    plan = _plan(_strategy(), _brief(literal="Sabah, Üniversite, Gece"))
    assert plan.title == "Sabah, Üniversite, Gece" and plan.title_source == "brief"
    # Without chapter labels the literal keeps its old place as the title.
    plan = _plan({}, _brief())
    assert plan.title == CHAPTERS and _labels(plan) == {}
    # A plan that prints no labels (the lip-sync montage) passes none and is unchanged.
    assert _title({}, brief_view(_brief()))[0] == CHAPTERS


def test_the_title_gate_does_not_take_the_chapter_list_for_the_titles_words() -> None:
    """A wordless title ask next to the chapter list still needs words: the chapter names are
    labels, so the gate asks before approval instead of the render blocking after it."""
    wordless = BriefRequirement(id="r9", kind="text", scope="title", description="a hook title")
    assert not title_source_exists(_strategy(), _brief(wordless))
    # Without chapter intents the literal is still the title source, as before.
    assert title_source_exists({}, _brief(wordless))


# -- the shared reader ----------------------------------------------------------------------


@pytest.mark.parametrize(
    ("literal", "expected"),
    [
        (CHAPTERS, ("Sabah", "Üniversite", "Öğle arası", "Spor", "Akşam")),
        ("SABAH / ÜNİVERSİTE ve Akşam.", ("Sabah", "Üniversite", "Akşam")),
        ('"Sabah" → "Akşam"', ("Sabah", "Akşam")),
        ("Sabah\nAkşam", ("Sabah", "Akşam")),
        ("Sabah", None),  # one name is a label, not a list
        ("Sabah, Sabah", None),
        ("Sabah Akşam", None),  # spaces alone never separate
        ("Sabah, Gece", None),
        ("Sporlar, Akşam", None),
    ],
)
def test_chapter_list_reads_only_lists_of_the_labels(literal: str, expected) -> None:  # noqa: ANN001
    assert chapter_list(literal, ["Sabah", "Üniversite", "Öğle arası", "Spor", "Akşam"]) == expected


def test_chapter_list_keeps_a_label_that_contains_a_separator_word() -> None:
    assert chapter_list("Rock and Roll, Jazz", ["Rock and Roll", "Jazz"]) == (
        "Rock and Roll",
        "Jazz",
    )
    assert chapter_list("Öğle arası, Akşam", ["Öğle", "Öğle arası", "Akşam"]) == (
        "Öğle arası",
        "Akşam",
    )
