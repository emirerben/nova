"""KRI-1558: the speaker's place and size per section, through the real chat parser and compiler.

Alanur's Talking session is the regression case. On the old code, "görseller koy" put three
pictures behind the whole video and shrank her to the bottom-right corner for all of it; "ben
köşede ufak kaldım" then answered that it could not; "sürekli arka planda görsel olmamalı" applied
nothing. Here each of her turns is the op a model answers it with (hand-written, no model call),
run through `EditCopilotAgent.parse` and `compile_editor_ops` on a phone snapshot of unrelated
footage (the harness's harbour run), one turn after the other, in Turkish and in English talk.

Failure modes these runs guard (docs/reviews/kri-1558-section-layout-failure-modes.md):
* the pictures still go on one corner for the whole take (fewer than two layouts, or a layout that
  does not start on a section);
* "put me back full screen" refused while any corner is in force (one corner, small sections, or a
  clip's own corner), with or without a picture pool;
* "no picture behind me after the intro" applies nothing;
* "keep me in the corner the whole time" leaves per-section layouts behind;
* a layout swapped between two sections passes the check against the section list;
* changing one section's framing is missing from the chat's "Done: ..." reply, or the reply
  names another section or the whole video;
* "make me smaller in part 1" asked as a checked requirement: the per-request summary (the ask
  ledger) does not count the part's layout change as that ask, so the line reads as not done (or
  "partly") and the change is listed under "also changed" instead;
* the same ask answered by changing another part (part 3) is marked done, because the check
  looks at the direction but not at which part was asked for.
"""

from __future__ import annotations

import copy
from collections.abc import Callable
from typing import Any

import pytest

from app.pipeline.talking_sections import (
    arrange_layouts,
    layout_mismatches,
)
from tests.evals.request_following.author_action_fixtures import (
    BACKGROUND_POOL,
    SECTIONS_OPEN,
    Scenario,
    _talking,
    build,
)

TALKS = {
    "tr": (
        "Bugün size kahve hakkında konuşacağım. Kahve sabahları enerji verir. "
        "Ama çok fazla kahve uykuyu bozar. Şimdi çay konusuna geçelim. Çay daha hafiftir. "
        "Yeşil çay mideyi yormaz. Son olarak su içmeyi unutmayın. Günde iki litre su için. "
        "Takip etmeyi unutmayın!"
    ),
    "en": (
        "Today I want to talk about coffee. Coffee gives me energy in the morning. "
        "Too much coffee ruins my sleep. Now let's move on to tea. Tea is much lighter. "
        "Green tea is easy on the stomach. Finally, drink water. Two litres of water a day. "
        "Follow for more!"
    ),
}


def _talk(language: str, *, pictures: bool = True) -> Callable[[dict[str, Any]], None]:
    def apply(variant: dict[str, Any]) -> None:
        from app.services.kria_editor_ops import background_duration_s  # noqa: PLC0415

        _talking(variant)
        duration = background_duration_s(variant)
        words = TALKS[language].split()
        cues, t = [], 0.3
        step = (duration - 1.5) / (len(words) + 6)
        for word in words:
            end = t + step * 0.8
            cues.append(
                {
                    "text": word,
                    "start_s": round(t, 3),
                    "end_s": round(end, 3),
                    "words": [{"text": word, "start_s": round(t, 3), "end_s": round(end, 3)}],
                }
            )
            t += step * (3 if word.endswith((".", "!")) else 1)
        variant["caption_cues"] = cues
        if not pictures:
            variant["speaker_background"] = {"kind": "color", "color": "#101010"}

    return apply


def _turn(
    setup: Callable[[dict[str, Any]], None], background: dict | None, op: dict, *, pool=True
) -> dict[str, Any]:
    def variant(v: dict[str, Any]) -> None:
        setup(v)
        if background is not None:
            v["speaker_background"] = copy.deepcopy(background)

    scenario = Scenario(
        "speaker_background",
        "",
        [],
        [op],
        variant=variant,
        context={"background_pool": BACKGROUND_POOL if pool else []},
        patches=SECTIONS_OPEN,
    )
    built = build(scenario)
    verdict = built["probe"]
    assert verdict["parsed_ops"] == [op["op"]], verdict
    assert verdict.get("compiles") is True, verdict.get("compile_error")
    payload = built["compiled"].payload
    section = payload.speaker_background
    from app.services.kria_editor_ops_diff import describe_diff  # noqa: PLC0415

    diff = built["compiled"].diff
    return {
        "background": section.model_dump(exclude_none=True) if section is not None else None,
        "snapshot": built["snapshot"]["background"],
        "summary": built["compiled"].changes,
        # What the chat's "Done: ..." reply of an editor turn says (kria_runtime: describe_diff
        # over the compiled diff, every change named).
        "said": describe_diff(diff.entries, live_text_count=diff.live_text_count, limit=None),
        "diff": diff,
        "compiled": built["compiled"],
    }


def _sections(snapshot: dict) -> list[Any]:
    from app.pipeline.talking_sections import TalkingSection  # noqa: PLC0415

    return [
        TalkingSection(row["n"], row["start_s"], row["end_s"], row["words"], 0, 0)
        for row in snapshot["sections"]
    ]


@pytest.mark.parametrize("language", ["tr", "en"])
def test_alanur_session_pictures_corner_back_full_then_no_constant_picture(
    language: str, prod_profile
) -> None:
    setup = _talk(language)
    # Turn 2: "görseller koy" -> pictures laid over the parts, her place changing per part.
    first = _turn(setup, None, {"op": "arrange_sections", "asset_ids": ["bg-1", "bg-2"]})
    sections = _sections(first["snapshot"])
    assert len(sections) >= 3, sections
    layouts = first["background"]["layout_windows"]
    assert len({row["layout"] for row in layouts}) >= 2, layouts
    # Every layout change starts on a section and each section draws what the plan says.
    planned = arrange_layouts(sections, ["bg-1", "bg-2"])
    assert not layout_mismatches(sections, layouts, planned)
    assert "speaker_frame" not in first["background"], "no corner for the whole take any more"
    # The review reads "small in a corner in some parts", never "you stay full size".
    from app.kria.brief_checks import _rendered_speaker_background_fact  # noqa: PLC0415

    fact = _rendered_speaker_background_fact({"speaker_background": first["background"]})
    assert fact is not None and fact.frame == "small_bottom_right" and fact.frame_in_parts

    # Turn 3: "ben köşede ufak kaldım" -> back full screen, and it says so (no refusal).
    second = _turn(setup, first["background"], {"op": "clear_speaker_frame"})
    after = second["background"]
    assert not any(row["layout"] in ("small", "picture") for row in after.get("layout_windows", []))
    assert "speaker_frame" not in after
    assert any("full screen" in str(change) for change in second["summary"]), second["summary"]

    # Turn 4: "sürekli arka planda görsel olmamalı" -> her own take after the intro.
    numbers = [section.index for section in sections[1:]]
    third = _turn(
        setup,
        first["background"],
        {"op": "set_section_layout", "speaker_layout": "own", "section_numbers": numbers},
    )
    own_after_intro = third["background"]["layout_windows"]
    for section in sections[1:]:
        middle = (section.start_s + section.end_s) / 2
        assert any(
            row["start_s"] <= middle < row["end_s"] and row["layout"] == "own"
            for row in own_after_intro
        ), (section, own_after_intro)


def test_keep_me_in_the_corner_the_whole_time_is_one_fixed_corner(prod_profile) -> None:
    setup = _talk("en")
    arranged = _turn(setup, None, {"op": "arrange_sections", "asset_ids": ["bg-1", "bg-2"]})
    cornered = _turn(
        setup, arranged["background"], {"op": "set_speaker_frame", "preset": "small_bottom_right"}
    )["background"]
    assert "layout_windows" not in cornered
    assert cornered["speaker_frame"] == {
        "width_fraction": 0.42,
        "x_fraction": 0.76,
        "y_fraction": 0.8,
    }


@pytest.mark.parametrize("language", ["tr", "en"])
def test_empty_picture_pool_back_full_screen_and_no_picture_after_intro_apply(
    language: str, prod_profile
) -> None:
    setup = _talk(language, pictures=False)
    small = _turn(
        setup,
        None,
        {"op": "set_section_layout", "speaker_layout": "small", "start_s": 0.0, "end_s": 6.0},
        pool=False,
    )["background"]
    back = _turn(setup, small, {"op": "clear_speaker_frame"}, pool=False)["background"]
    assert not back.get("layout_windows"), back
    probe = _turn(
        setup,
        None,
        {"op": "set_section_layout", "speaker_layout": "own", "section_numbers": [2]},
        pool=False,
    )
    assert probe["background"]["layout_windows"][0]["layout"] == "own"


def test_a_layout_swapped_between_two_sections_is_caught(prod_profile) -> None:
    setup = _talk("en")
    first = _turn(setup, None, {"op": "arrange_sections", "asset_ids": ["bg-1", "bg-2"]})
    sections = _sections(first["snapshot"])
    planned = arrange_layouts(sections, ["bg-1", "bg-2"])
    layouts = first["background"]["layout_windows"]
    distinct = [i for i in range(1, len(layouts)) if layouts[i]["layout"] != layouts[0]["layout"]]
    assert distinct, "the control needs two sections with different layouts"
    swapped = copy.deepcopy(layouts)
    j = distinct[0]
    for key in ("layout", "speaker_frame"):
        swapped[0][key], swapped[j][key] = layouts[j].get(key), layouts[0].get(key)
        if swapped[0][key] is None:
            swapped[0].pop(key)
        if swapped[j][key] is None:
            swapped[j].pop(key)
    assert layout_mismatches(sections, swapped, planned), "the swap control must be caught"
    # And a change off a section boundary is caught too.
    shifted = copy.deepcopy(layouts)
    shifted[0]["end_s"] = round(shifted[0]["end_s"] + 1.0, 3)
    shifted[1]["start_s"] = shifted[0]["end_s"]
    assert any(
        "not a section boundary" in problem for problem in layout_mismatches(sections, shifted, [])
    )


def test_sections_come_from_the_recorded_list_when_the_edit_has_no_caption_words() -> None:
    from app.services.kria_editor_ops import speaker_sections_for  # noqa: PLC0415

    rows = [
        {"index": 1, "start_s": 0.0, "end_s": 6.0, "text": "a"},
        {"index": 2, "start_s": 6.0, "end_s": 12.0, "text": "b"},
    ]
    sections = speaker_sections_for({"caption_cues": [], "speaker_sections": rows})
    assert [(s.index, s.start_s, s.end_s) for s in sections] == [(1, 0.0, 6.0), (2, 6.0, 12.0)]


def test_one_section_framing_change_is_named_in_the_chat_reply(prod_profile) -> None:
    from app.kria.editor_receipts import reply_from_diff  # noqa: PLC0415

    setup = _talk("en")
    arranged = _turn(setup, None, {"op": "arrange_sections", "asset_ids": ["bg-1", "bg-2"]})
    sections = _sections(arranged["snapshot"])
    assert len(sections) >= 3, sections
    before = arranged["background"]
    assert before["layout_windows"][0]["layout"] == "own"
    # "Make me small in the bottom-left corner in the first part" and nothing else.
    turn = _turn(
        setup,
        before,
        {
            "op": "set_section_layout",
            "speaker_layout": "small",
            "preset": "small_bottom_left",
            "section_numbers": [1],
        },
    )
    after = turn["background"]["layout_windows"]
    first = sections[0]
    assert any(
        row["start_s"] <= (first.start_s + first.end_s) / 2 < row["end_s"]
        and row["layout"] == "small"
        for row in after
    ), after
    # Every other section draws what it drew before.
    from app.pipeline.talking_sections import drawn_layout  # noqa: PLC0415

    for section in sections[1:]:
        middle = (section.start_s + section.end_s) / 2
        assert drawn_layout(after, middle) == drawn_layout(before["layout_windows"], middle)

    said = turn["said"]
    framing = [phrase for phrase in said if "part" in phrase]
    assert framing == ["Made you small in the bottom-left corner in part 1"], said
    assert not any("whole video" in phrase for phrase in said), said
    assert reply_from_diff(said).startswith(
        "Done: Made you small in the bottom-left corner in part 1"
    ), said
    # The same reply in a Turkish chat.
    from app.kria.reply_language import reply_language_for  # noqa: PLC0415
    from app.services.kria_editor_ops_diff import describe_diff  # noqa: PLC0415

    with reply_language_for("tr"):
        said_tr = describe_diff(turn["diff"].entries, limit=None)
    assert "1. bölümde sol alt köşede küçük gösteriliyorsun" in said_tr, said_tr


# What the brief writer records for "make me smaller in part 1" while speaker sections are open
# (`with_speaker_parts_brief`): the part is a structured fact, never read from the words (KRI-667).
PART_1_ASKED = {"speaker_parts": [1]}

ASKS = {
    "en": (
        "make me smaller in part 1",
        "Done: Made you small in the bottom-right corner in part 1",
    ),
    "tr": ("beni 1. bölümde küçült", "Yapıldı: 1. bölümde sağ alt köşede küçük gösteriliyorsun"),
}


@pytest.mark.parametrize("language", ["en", "tr"])
def test_make_me_smaller_in_part_1_is_done_in_the_per_request_summary(
    language: str, prod_profile
) -> None:
    """Tadic's hold: the asked change is the receipt, never "failed + changed as an extra"."""
    from dataclasses import replace  # noqa: PLC0415

    from app.kria.ask_ledger import editor_ledger  # noqa: PLC0415
    from app.kria.brief import BriefRequirement  # noqa: PLC0415
    from app.kria.brief_checks import (  # noqa: PLC0415
        build_receipts,
        plan_facts_from_editor_payload,
    )
    from app.kria.editor_receipts import reply_from_editor_receipts  # noqa: PLC0415
    from app.kria.reply_language import reply_language_for  # noqa: PLC0415

    ask, done_line = ASKS[language]
    setup = _talk(language)
    arranged = _turn(setup, None, {"op": "arrange_sections", "asset_ids": ["bg-1", "bg-2"]})
    assert arranged["background"]["layout_windows"][0]["layout"] == "own"
    # The op a model answers "make me smaller in part 1" with (no model call).
    turn = _turn(
        setup,
        arranged["background"],
        {"op": "set_section_layout", "speaker_layout": "small", "section_numbers": [1]},
    )
    compiled = turn["compiled"]
    reqs = [
        BriefRequirement(id="r1", kind="style", scope="global", description=ask, facts=PART_1_ASKED)
    ]
    with reply_language_for(language):
        payload = compiled.payload.model_dump(mode="json", exclude_none=True)
        facts = plan_facts_from_editor_payload(
            payload, compiled.text_diff or None, compiled.changes
        )
        facts = replace(facts, anaphora_rows=compiled.diff.changed_text_ids())
        receipts = build_receipts(reqs, facts, include_unchecked=True)
        ledger = editor_ledger(reqs, receipts, compiled.diff, facts.text_styles or ())
        reply = reply_from_editor_receipts(reqs, list(ledger.receipts), unbound=ledger.extras)

    (receipt,) = ledger.receipts
    # On 434f19085: status "not_possible", reply "Couldn't: make me smaller in part 1 (Nothing
    # changed for this). Also changed: Made you small in the bottom-right corner in part 1".
    assert (receipt.status, receipt.verification) == ("met", "checked"), (receipt, reply)
    assert not ledger.extras, reply
    assert reply.startswith(done_line), reply
    for wrong in ("Couldn't", "Also changed", "Partly", "Yapamadım", "Ayrıca değişti", "Kısmen"):
        assert wrong not in reply, reply
    assert ledger.gaps(["r1"], reply) == [], reply


def test_make_me_bigger_in_part_1_answered_by_making_her_small_is_not_done(prod_profile) -> None:
    """Red control: the size ask is checked for its direction, not just for any layout change."""
    from dataclasses import replace  # noqa: PLC0415

    from app.kria.ask_ledger import editor_ledger  # noqa: PLC0415
    from app.kria.brief import BriefRequirement  # noqa: PLC0415
    from app.kria.brief_checks import (  # noqa: PLC0415
        build_receipts,
        plan_facts_from_editor_payload,
    )

    setup = _talk("en")
    arranged = _turn(setup, None, {"op": "arrange_sections", "asset_ids": ["bg-1", "bg-2"]})
    turn = _turn(
        setup,
        arranged["background"],
        {"op": "set_section_layout", "speaker_layout": "small", "section_numbers": [1]},
    )
    compiled = turn["compiled"]
    reqs = [
        BriefRequirement(
            id="r1",
            kind="style",
            scope="global",
            description="make me bigger in part 1",
            facts=PART_1_ASKED,
        )
    ]
    payload = compiled.payload.model_dump(mode="json", exclude_none=True)
    facts = plan_facts_from_editor_payload(payload, compiled.text_diff or None, compiled.changes)
    facts = replace(facts, anaphora_rows=compiled.diff.changed_text_ids())
    receipts = build_receipts(reqs, facts, include_unchecked=True)
    ledger = editor_ledger(reqs, receipts, compiled.diff, facts.text_styles or ())
    (receipt,) = ledger.receipts
    assert receipt.status != "met", receipt


def _size_ask_receipt(
    language: str, ask: str, section_numbers: list[int], asked: tuple[int, ...] = (1,)
):
    """Ask `ask`, make her small in `section_numbers`, then run the per-request summary."""
    from dataclasses import replace  # noqa: PLC0415

    from app.kria.ask_ledger import editor_ledger  # noqa: PLC0415
    from app.kria.brief import BriefRequirement  # noqa: PLC0415
    from app.kria.brief_checks import (  # noqa: PLC0415
        build_receipts,
        plan_facts_from_editor_payload,
    )
    from app.kria.editor_receipts import reply_from_editor_receipts  # noqa: PLC0415
    from app.kria.reply_language import reply_language_for  # noqa: PLC0415

    setup = _talk(language)
    arranged = _turn(setup, None, {"op": "arrange_sections", "asset_ids": ["bg-1", "bg-2"]})
    sections = _sections(arranged["snapshot"])
    assert len(sections) >= 3, sections
    layouts = arranged["background"]["layout_windows"]
    assert layouts[0]["layout"] == "own" and layouts[-1]["layout"] == "own", layouts
    turn = _turn(
        setup,
        arranged["background"],
        {"op": "set_section_layout", "speaker_layout": "small", "section_numbers": section_numbers},
    )
    compiled = turn["compiled"]
    reqs = [
        BriefRequirement(
            id="r1",
            kind="style",
            scope="global",
            description=ask,
            facts={"speaker_parts": list(asked)},
        )
    ]
    with reply_language_for(language):
        payload = compiled.payload.model_dump(mode="json", exclude_none=True)
        facts = plan_facts_from_editor_payload(
            payload, compiled.text_diff or None, compiled.changes
        )
        facts = replace(facts, anaphora_rows=compiled.diff.changed_text_ids())
        receipts = build_receipts(reqs, facts, include_unchecked=True)
        ledger = editor_ledger(reqs, receipts, compiled.diff, facts.text_styles or ())
        reply = reply_from_editor_receipts(reqs, list(ledger.receipts), unbound=ledger.extras)
    (receipt,) = ledger.receipts
    return receipt, ledger, reply, sections


@pytest.mark.parametrize("language", ["en", "tr"])
def test_make_me_smaller_in_part_1_answered_in_another_part_is_not_done(
    language: str, prod_profile
) -> None:
    """Tadic's wrong-part control: part 3 changed for a part-1 ask is never "Done ... in part 3"."""
    ask, _done = ASKS[language]
    receipt, _ledger, reply, _sections = _size_ask_receipt(language, ask, [3])
    assert receipt.status != "met", (receipt, reply)
    assert not reply.startswith(("Done", "Yapıldı")), reply
    # The part-3 change is still named, honestly, as something else that changed.
    assert ("part 3" if language == "en" else "3. bölümde") in reply, reply


@pytest.mark.parametrize("language", ["en", "tr"])
def test_make_me_smaller_in_part_1_answered_in_parts_1_and_3_is_done(
    language: str, prod_profile
) -> None:
    ask, _done = ASKS[language]
    receipt, _ledger, reply, _sections = _size_ask_receipt(language, ask, [1, 3])
    assert (receipt.status, receipt.verification) == ("met", "checked"), (receipt, reply)


def test_the_asked_parts_come_from_the_structured_fact_only() -> None:
    from app.kria.editor_receipts import _speaker_parts  # noqa: PLC0415

    assert _speaker_parts({"speaker_parts": [1, 3, -1]}) == {1, 3, -1}
    # Anything the brief writer could get wrong is no part at all (the ask is then judged as
    # before: by direction only), never a guess.
    assert _speaker_parts({"speaker_parts": [0, 41, "2", True, 2.0]}) == frozenset()
    assert _speaker_parts({"speaker_parts": 1}) == frozenset()
    assert _speaker_parts({}) == frozenset()


def test_the_brief_writer_is_taught_speaker_parts_only_while_sections_are_open(
    monkeypatch,
) -> None:
    from app.agents._schemas.brief_extractor import BriefExtractionInput  # noqa: PLC0415
    from app.agents.brief_extractor import BriefExtractorAgent  # noqa: PLC0415
    from app.agents.main_creator import brief_prompt_section  # noqa: PLC0415
    from app.services import phone_rollout  # noqa: PLC0415

    def prompts() -> tuple[str, str]:
        agent = BriefExtractorAgent.__new__(BriefExtractorAgent)
        extractor = agent.render_prompt(BriefExtractionInput(user_message="make me smaller"))
        return extractor, brief_prompt_section(False)

    closed = prompts()
    assert all("speaker_parts" not in prompt for prompt in closed)
    monkeypatch.setattr(phone_rollout, "speaker_sections_supported", lambda **_: True)
    opened = prompts()
    assert all('"speaker_parts"' in prompt for prompt in opened)
    # Opening adds the one sentence and nothing else, so closed prompts keep their bytes.
    for before, after in zip(closed, opened, strict=True):
        sentence = next(line for line in after.splitlines() if "speaker_parts" in line)
        assert after.replace("\n" + sentence, "", 1) == before
