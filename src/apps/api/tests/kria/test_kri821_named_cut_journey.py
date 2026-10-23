"""KRI-821: "cut the half question 'how are you gonna'" removes that line from a phone Talking take.

Real take: Caro at Merope (tests/fixtures/kri790_merope_t1_take.json, 68.4 s, the creator's own
prompt). Before KRI-821 only a start/end line was cut; a line in the middle of the take stayed
and every receipt could only say "I can't cut out one exact line yet".

Failure modes (written before the fix):

N1  The named line stays in the take (no span removed), or the receipt still says it can't.
N2  The cut removes a neighbouring word (the cut reaches past the line's own words).
N3  A line said more than once is cut at a guessed occurrence instead of asking which.
N4  A line the take never says (misheard, or invented) is reported as cut.
N5  A cut line the model recorded but the requirement's own text does not carry is cut
    (model output promoted to creator intent).
N6  Captions still show the cut words (the plan is not the one captions remap through).
N7  The receipt is lost when cleanup is off (no cleanup record), or cleanup's own counts
    swallow the named cut.
N8  A stale/withdrawn (superseded) requirement still cuts.
"""

from __future__ import annotations

import json
import os
from pathlib import Path

from app.kria.brief import BriefRequirement, CreativeBrief
from app.kria.brief_binding import BriefBinding
from app.kria.brief_checks import build_receipts, plan_facts_from_phone_variant
from app.kria.opening_cut_ask import brief_named_cuts
from app.pipeline.phone_opening_cut import NAMED_CUT_REASON
from app.pipeline.silence_cut import no_op_plan, remap_words
from app.tasks.generative_build import _phone_opening_cut

_TAKE = json.loads(
    (Path(__file__).resolve().parents[1] / "fixtures" / "kri790_merope_t1_take.json").read_text()
)
WORDS = _TAKE["words"]
DURATION = float(_TAKE["duration_s"])
THREAD = "32a5b608-6397-4608-889f-dff5ad9bec40"


def _req(rid: str, description: str, cut_lines: list[str], **extra) -> BriefRequirement:
    return BriefRequirement(
        id=rid,
        kind="audio",
        scope="global",
        description=description,
        facts={"speech_cleanup": True, "cut_lines": cut_lines},
        **extra,
    )


HALF_QUESTION = _req("r4", "Cut the half question 'how are you gonna'", ["how are you gonna"])
SLIP = _req(
    "r3",
    "Cut her slip 'with bread, no,' so it says 'waffle cookie with blackberry jam'",
    ["with bread, no,"],
)
TWICE = _req("r8", "Cut where she says 'thank you Karo'", ["thank you Karo"])
INVENTED = _req("r9", "Cut the long pauses", ["and whipped cream"])
NEVER_SAID = _req("r11", "Cut 'the pistachio croissant'", ["the pistachio croissant"])
WITHDRAWN = _req(
    "r10", "Cut 'Merope, it's a Colombian coffee'", ["Merope, it's a Colombian coffee"]
).model_copy(update={"status": "superseded"})


def _run(*reqs: BriefRequirement):
    brief = CreativeBrief(version=1, requirements=list(reqs))
    binding = BriefBinding.create(THREAD, brief).model_dump(mode="json")
    return brief, _phone_opening_cut(
        {"creator_brief_binding": binding}, no_op_plan(DURATION), WORDS, duration_s=DURATION
    )


def _said(words) -> str:
    return " ".join(w["text"] for w in words)


def test_a_named_line_in_the_middle_of_the_take_is_cut_and_receipted():
    brief, (plan, receipts) = _run(HALF_QUESTION, SLIP, TWICE, INVENTED, WITHDRAWN, NEVER_SAID)
    # N5/N8: only lines the requirement itself carries, from live requirements.
    assert [a.quote for a in brief_named_cuts(brief)] == [
        "how are you gonna",
        "with bread, no,",
        "thank you Karo",
        "the pistachio croissant",
    ]
    by_quote = {row["quote"]: row for row in receipts["named_cuts"]}
    cut = by_quote["how are you gonna"]
    assert cut["status"] == "applied" and cut["requirement_ids"] == ["r4"]
    # The take misheard her "bread" as "red": the line is still found, and cut.
    slip = by_quote["with bread, no,"]
    assert slip["status"] == "applied" and slip["exact"] is False
    assert by_quote["the pistachio croissant"]["reason"] == "not_found"  # N4
    assert by_quote["thank you Karo"]["reason"] == "said_more_than_once"  # N3
    assert by_quote["thank you Karo"]["status"] == "not_applied"

    named = [r for r in plan.removed if r.reason == NAMED_CUT_REASON]
    assert [(r.start_s, r.end_s) for r in named] == [
        (slip["start_s"], slip["end_s"]),
        (cut["start_s"], cut["end_s"]),
    ]

    def inside(row):
        return [w for w in WORDS if w["start_s"] < row["end_s"] and w["end_s"] > row["start_s"]]

    # N2: exactly the named words, nothing either side of them.
    assert _said(inside(cut)).lower().strip(".") == "how are you gonna"
    assert _said(inside(slip)) == "with red no,"
    # The plan still partitions the take.
    spans = sorted([*plan.keep_segments, *((r.start_s, r.end_s) for r in plan.removed)])
    assert spans[0][0] == 0.0 and abs(spans[-1][1] - DURATION) < 1e-6
    assert all(abs(a[1] - b[0]) < 1e-6 for a, b in zip(spans, spans[1:], strict=False))

    # N6: the captions remap through the same plan, so the line is gone from them.
    before = _said(WORDS)
    after = _said(remap_words(WORDS, plan))
    assert "how are you gonna" in before
    assert "how are you gonna" not in after
    assert "where we are is right now Merope, Merope, it's a Colombian" in after
    assert "wafer cookie with blackberry jam" in after

    # N1/N7: the render's receipt answers the requirement (cleanup off: no cleanup record).
    variant = {
        "variant_id": "subtitled",
        "resolved_archetype": "subtitled",
        "render_destination": "device",
        "render_status": "awaiting_device",
        "named_cuts": receipts["named_cuts"],
    }
    facts = plan_facts_from_phone_variant(variant)
    out = {
        r.requirement_id: r
        for r in build_receipts(
            [HALF_QUESTION, SLIP, TWICE, NEVER_SAID], facts, include_unchecked=True
        )
    }
    assert out["r4"].status == "met", out["r4"]
    assert 'Cut "how are you gonna"' in (out["r4"].reason or "")
    assert out["r3"].status == "met"
    assert out["r11"].status == "partial" and "couldn't find it" in out["r11"].reason  # N4
    assert out["r8"].status == "partial" and "more than once" in out["r8"].reason
    for receipt in out.values():
        assert "I can't cut out one exact line yet" not in (receipt.reason or "")

    target = os.environ.get("KRI821_CUT_REPORT")
    if target:
        Path(target).parent.mkdir(parents=True, exist_ok=True)
        Path(target).write_text(
            json.dumps(
                {
                    "ticket": "KRI-821",
                    "request": _TAKE["prompt"],
                    "brief": brief.model_dump(mode="json"),
                    "source_duration_s": DURATION,
                    "named_cuts": receipts["named_cuts"],
                    "keep_segments": plan.keep_segments,
                    "captions_after": after,
                    "receipts": {k: v.model_dump(mode="json") for k, v in out.items()},
                },
                indent=2,
            )
        )


def test_no_named_line_leaves_the_render_exactly_as_before():
    plan = no_op_plan(DURATION)
    brief = CreativeBrief(version=1, requirements=[_req("r1", "Cut the long pauses", [])])
    binding = BriefBinding.create(THREAD, brief).model_dump(mode="json")
    assert _phone_opening_cut(
        {"creator_brief_binding": binding}, plan, WORDS, duration_s=DURATION
    ) == (plan, None)


# Retakes (KRI-821 review). Failure modes, written before the fix:
# N9   A retake transcribed differently each time (only fuzzy-matched) counts as said once,
#      so the system picks which take to cut.
# N10  "Cut the FIRST take of ..." can't be expressed, so the retake stays in the export.
# N11  An ordinal the model recorded but the approved text never says picks the take.
# N12  A copy already removed by an earlier cut makes a single remaining copy ambiguous.


def test_the_creator_names_which_take_of_a_repeated_line_to_cut():
    def which(word: str, value: str, rid: str) -> BriefRequirement:
        return _req(
            rid,
            f"Cut the {word} time she says 'thank you Karo'",
            ["thank you Karo"],
        ).model_copy(
            update={
                "facts": {
                    "speech_cleanup": True,
                    "cut_lines": ["thank you Karo"],
                    "cut_line_occurrences": {"thank you Karo": value},
                }
            }
        )

    occurrences = [
        (w["start_s"], w["end_s"])
        for i, w in enumerate(WORDS)
        if w["text"].lower().startswith("thank")
        and WORDS[i + 1]["text"].lower().startswith("you")
        and WORDS[i + 2]["text"].lower().startswith("karo")
    ]
    assert len(occurrences) == 2

    _, (plan, receipts) = _run(which("first", "first", "r20"))
    first = receipts["named_cuts"][0]
    assert first["status"] == "applied" and first["occurrence"] == 1  # N10
    assert first["start_s"] <= occurrences[0][0] < first["end_s"]
    assert first["occurrences_found"] == 2

    _, (_, receipts) = _run(which("last", "last", "r21"))
    last = receipts["named_cuts"][0]
    assert last["status"] == "applied" and last["start_s"] <= occurrences[1][0] < last["end_s"]

    # N11: the model says "first" but the approved text names no take: never guessed.
    unapproved = which("", "first", "r22").model_copy(
        update={"description": "Cut where she says 'thank you Karo'"}
    )
    _, (_, receipts) = _run(unapproved)
    assert receipts["named_cuts"][0]["reason"] == "said_more_than_once"


def test_a_retake_transcribed_differently_is_never_cut_at_a_guessed_take():
    from app.pipeline.phone_opening_cut import apply_named_line_cuts, cut_span

    def words(*spoken: tuple[str, float]):
        return [{"text": t, "start_s": s, "end_s": s + 0.3} for t, s in spoken]

    take = words(
        ("so", 0.5), ("this", 1.0), ("is", 1.4),
        ("with", 4.0), ("bread", 4.4), ("no", 4.8),
        ("okay", 7.0), ("again", 7.5),
        ("with", 12.0), ("the", 12.4), ("bread,", 12.8), ("no", 13.2),
        ("and", 16.0), ("that's", 16.5), ("it", 17.0),
    )  # fmt: skip
    plan = no_op_plan(20.0)
    cut, receipts = apply_named_line_cuts(
        plan, take, [("with bread, no", ("r1",))], duration_s=20.0
    )
    assert receipts[0]["reason"] == "said_more_than_once"  # N9
    assert cut == plan

    # N12: the first copy already sits in a removed span; the one left in the video is cut.
    trimmed = cut_span(plan, 3.8, 5.3, duration_s=20.0)
    cut, receipts = apply_named_line_cuts(
        trimmed, take, [("with bread, no", ("r1",))], duration_s=20.0
    )
    assert receipts[0]["status"] == "applied"
    assert receipts[0]["start_s"] >= 11.5
