"""KRI-558: asks only the finished video can show are judged there, and never block a draft.

Prod thread 92154c24: "Inter font in white", a typewriter hook, a bottom-left location label on
each clip and "make all clips 1 second long except the first and last" ended as "I couldn't
verify every requested change". Failure modes pinned here (written before the code):
* a record or draft with no text lane judges the look and blocks the render;
* a per-clip length is compared with the TOTAL length (a false fail that blocks the render);
* "Inter" is read as a different family because the saved face is the legacy ``Inter-Bold``;
* an off-white (#F2F2F2) reads as not white;
* a row whose font is unknown is read as a mismatch instead of staying neutral;
* the first and last clip are held to the per-clip length they were excluded from.
"""

from __future__ import annotations

from typing import Any

import pytest

from app.kria.brief import BriefRequirement, CreativeBrief
from app.kria.brief_checks import (
    PlanFacts,
    build_receipts,
    check_requirement,
    describe_text_look,
    is_judged,
    judged_at_render,
    plan_facts_from_phone_variant,
    reply_from_receipts,
)

HOOK = "come with me to my favorite restaurant and ice cream place in lisbon"


def _req(kind: str, description: str, rid: str, **kw: Any) -> BriefRequirement:
    return BriefRequirement(
        id=rid,
        kind=kind,
        scope=kw.pop("scope", "global"),
        description=description,
        literal=kw.pop("literal", None),
        facts=kw,
    )


def _title(**kw: Any) -> dict[str, Any]:
    return {
        "id": "guided-title",
        "text": HOOK,
        "role": "title",
        "start_s": 0.0,
        "end_s": 4.0,
        "font_family": "Inter-Bold",
        "color": "#FFFFFF",
        "animation_phases": {
            "entrance": "typewriter",
            "exit": "none",
            "loop": "none",
            "speed": 0.3,
        },
        **kw,
    }


def _label(clip: str, **kw: Any) -> dict[str, Any]:
    return {
        "id": f"clip-label-media-{clip}",
        "clip_id": clip,
        "text": "[Location]",
        "start_s": 0.5,
        "end_s": 3.0,
        "font_family": "Inter-Bold",
        "color": "#FFFFFF",
        "position": "custom",
        "x_frac": 0.08,
        "y_frac": 0.78,
        "alignment": "left",
        **kw,
    }


def _variant(
    *,
    labels: list[dict] | None = None,
    title: dict | None = None,
    lengths=(3.2, 1.0, 1.0, 1.0, 2.8),
):
    rows, start = [], 0.0
    for index, length in enumerate(lengths):
        rows.append(
            {
                "lane": "clip",
                "media_id": f"m{index}",
                "output_start_s": start,
                "output_end_s": start + length,
            }
        )
        start += length
    clips = labels if labels is not None else [_label(f"m{i}") for i in range(len(lengths))]
    return {"text_elements": [title or _title(), *clips], "story_timeline": rows}


R_TITLE = _req(
    "text",
    "title with a typewriter hook",
    "r2",
    scope="title",
    literal=HOOK,
    animation="typewriter",
)
R_LABEL = _req(
    "text",
    "location placeholder bottom left",
    "r3",
    scope="per_clip",
    position="bottom_left",
    placeholder=True,
)
R_FONT_FACTS = _req(
    "style", "use inter font and white", "r4", font_family="Inter", text_color="#FFFFFF"
)
R_FONT_WORDS = _req("style", "Inter font in white color", "r4")
R_LENGTH = _req(
    "timing",
    "make all clips 1 second long except for the first and the last one",
    "r5",
    literal="1",
)


def _verdict(req, facts):
    receipt = check_requirement(req, facts)
    return receipt, is_judged(req, receipt)


@pytest.mark.parametrize("req", [R_FONT_FACTS, R_FONT_WORDS])
def test_inter_and_white_are_judged_from_facts_or_words_against_the_saved_rows(req) -> None:
    facts = plan_facts_from_phone_variant(_variant())
    receipt, judged = _verdict(req, facts)
    assert (receipt.status, judged) == ("met", True)


def test_an_off_white_still_reads_as_white_but_a_different_family_does_not() -> None:
    off_white = [_title(color="#F2F2F2"), *[_label(f"m{i}", color="#F2F2F2") for i in range(5)]]
    variant = _variant(labels=off_white[1:], title=off_white[0])
    assert _verdict(R_FONT_WORDS, plan_facts_from_phone_variant(variant))[0].status == "met"
    other = _variant(labels=[_label(f"m{i}", font_family="Playfair Display") for i in range(5)])
    receipt, judged = _verdict(R_FONT_WORDS, plan_facts_from_phone_variant(other))
    assert (receipt.status, judged) == ("partial", False)  # a subset ask: not evidence of a miss


def test_a_row_with_no_known_font_is_neutral_not_a_mismatch() -> None:
    rows = [_label(f"m{i}", font_family=None) for i in range(5)]
    receipt, judged = _verdict(R_FONT_WORDS, plan_facts_from_phone_variant(_variant(labels=rows)))
    assert judged is False


def test_title_typewriter_and_bottom_left_labels_are_judged_on_the_finished_text_lane() -> None:
    facts = plan_facts_from_phone_variant(_variant())
    for req in (R_TITLE, R_LABEL):
        receipt, judged = _verdict(req, facts)
        assert (receipt.status, judged) == ("met", True), req.id


def test_a_title_that_fades_and_labels_in_the_middle_are_partly_with_the_mismatch() -> None:
    title = _title(
        animation_phases={"entrance": "fade", "exit": "none", "loop": "none", "speed": 1}
    )
    labels = [_label(f"m{i}", x_frac=0.5, y_frac=0.5, alignment="center") for i in range(5)]
    facts = plan_facts_from_phone_variant(_variant(title=title, labels=labels))
    for req, word in ((R_TITLE, "animation"), (R_LABEL, "position")):
        receipt, judged = _verdict(req, facts)
        assert receipt.status == "partial" and is_judged(req, receipt)
        assert word in (receipt.reason or "")


def test_per_clip_length_excludes_the_first_and_last_clip() -> None:
    receipt, judged = _verdict(R_LENGTH, plan_facts_from_phone_variant(_variant()))
    assert (receipt.status, judged) == ("met", True)
    long_middle = _variant(lengths=(3.2, 1.0, 2.0, 1.0, 2.8))
    receipt, judged = _verdict(R_LENGTH, plan_facts_from_phone_variant(long_middle))
    assert receipt.status == "partial" and "1 of 3 clips aren't 1s (2.0s)" in (receipt.reason or "")


def test_per_clip_length_is_never_compared_with_the_total_length() -> None:
    """The extractor may also put 1 in ``duration_s``: that is not "the video is 1s long"."""
    req = _req("timing", R_LENGTH.description, "r5", literal="1", duration_s=1)
    facts = plan_facts_from_phone_variant(_variant())
    assert _verdict(req, facts)[0].status == "met"


@pytest.mark.parametrize("req", [R_FONT_FACTS, R_FONT_WORDS, R_LENGTH])
def test_a_draft_or_record_with_no_text_lane_never_judges_style_or_lengths(req) -> None:
    """The unified montage record and the strategy draft have no finished text or timeline."""
    for facts in (
        PlanFacts(),
        PlanFacts(rendered_output=True, clip_ids=("m0", "m1")),
        PlanFacts(editor=True),
    ):
        receipt, judged = _verdict(req, facts)
        assert judged is False and receipt.status != "not_possible", (req.id, facts)


@pytest.mark.parametrize("req", [R_TITLE, R_LABEL])
def test_a_record_that_has_the_words_but_no_text_lane_cannot_certify_the_look(req) -> None:
    """The words are there; whether they animate or sit where asked is only visible once
    rendered, so the record leaves it unjudged instead of claiming (or failing) it."""
    record = PlanFacts(
        title=HOOK,
        texts=(HOOK, "[Location]"),
        title_source="creator",
        clip_ids=("m0", "m1"),
        per_clip_text={"m0": "[Location]", "m1": "[Location]"},
        rendered_output=True,
        has_clip_structure=True,
    )
    receipt, judged = _verdict(req, record)
    assert judged is False and receipt.status != "not_possible"
    assert judged_at_render(req)


def test_the_render_ready_reply_says_what_the_video_holds_for_an_ask_it_could_not_decide() -> None:
    """Fonts differ across the texts and the ask names no target: not evidence of a miss, so
    the creator is shown what the video uses instead of "I can't verify this"."""
    req = _req("style", "use inter font", "r9")
    mixed = _variant(labels=[_label(f"m{i}", font_family="Playfair Display") for i in range(5)])
    facts = plan_facts_from_phone_variant(mixed)
    [receipt] = build_receipts([req], facts, include_unchecked=True)
    assert receipt.verification == "unchecked"
    assert describe_text_look(req, facts) == "font: Inter-Bold, Playfair Display ×5"
    reply = reply_from_receipts(
        CreativeBrief(version=1, requirements=[req]), [receipt], summary="Ready."
    )
    assert reply.startswith("Have a look at these in the video:")
    assert "verify" not in reply.lower()
