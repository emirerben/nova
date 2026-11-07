"""Parent KRI-1151: "after I say X, show this clip with its own sound" is a strategy field the
planner is taught only where the iPhone can pause the take, repaired honestly, and reported as
met or unmet from what the draft and the finished video really hold.

Failure modes pinned (written before the code):

 1. the field leaks into the model-facing / Kria tool schema, or changes the serialization /
    hash of every strategy that does not use it;
 2. a malformed row (no position, two positions) rejects the whole strategy instead of being
    dropped;
 3. the field is missing from the render-contract matrix or the placed-media set;
 4. the repair keeps an insert where the phone cannot render it (rollout off, montage, photo,
    Visuals video, only clip, the ending clip, the same clip twice);
 5. the prompt teaches it while the rollout is closed, or changes bytes for any other manifest;
 6. a draft or a finished video is called "Done" without a plan / render receipt that plays
    the clip, in English and Turkish, and a dropped one says why.
"""

from __future__ import annotations

import dataclasses
import hashlib
import json

import pytest

from app.agents._schemas.creator_agent import CreativeStrategy
from app.agents._schemas.reaction_beats import MAX_INSERT_CLIPS, InsertClip
from app.config import settings
from app.kria.brief import BriefRequirement
from app.kria.brief_checks import (
    InsertClipFact,
    build_receipts,
    plan_facts_from_phone_variant,
    plan_facts_from_strategy,
)
from app.services import creator_capabilities as capabilities
from tests.kria.test_kri841_ending_clip_ask import (
    _LEGACY_STRATEGY,
    FOOTAGE_A,
    FOOTAGE_B,
    FOOTAGE_C,
    _julio_manifest,
    _manifest,
    _phone_settings,
    _strategy,
)

ASK = "After I say congress, show the congress clip with its own sound"


def _open(monkeypatch, *, insert: bool = True):
    _phone_settings(monkeypatch, multi_clip=True)
    monkeypatch.setattr(settings, "phone_talking_insert_clips_enabled", insert)


def _insert(media_id=FOOTAGE_C, **kw):
    return {"media_id": media_id, "at_phrase": "congress", **kw}


def _req():
    return BriefRequirement(
        id="r5",
        kind="style",
        scope="global",
        description=ASK,
        facts={"insert_clip": "the congress clip", "insert_at_phrase": "congress"},
    )


# --- 1-3. schema ------------------------------------------------------------------------------


def test_the_strategy_round_trips_insert_clips_and_drops_malformed_rows():
    strategy = CreativeStrategy(
        edit_format="subtitled",
        insert_clips=[
            _insert(),
            {"media_id": FOOTAGE_B},
            {"media_id": "x", "at_phrase": "a", "at_s": 3},
        ],
    )
    # A row with no position is the sentence-about-the-clip form and is kept; a row with two
    # positions is the one dropped.
    assert [c.media_id for c in strategy.insert_clips] == [FOOTAGE_C, FOOTAGE_B]
    dumped = strategy.model_dump(mode="json", exclude_none=True)
    assert dumped["insert_clips"] == [
        {"media_id": FOOTAGE_C, "at_phrase": "congress", "keep_audio": True},
        {"media_id": FOOTAGE_B, "keep_audio": True},
    ]
    assert CreativeStrategy.model_validate(dumped) == strategy
    # More than the cap parse (so the repair can name each extra one), they are not cut here.
    many = CreativeStrategy(
        edit_format="subtitled",
        insert_clips=[_insert(f"m{i}") for i in range(MAX_INSERT_CLIPS + 4)],
    )
    assert len(many.insert_clips) > MAX_INSERT_CLIPS
    with pytest.raises(ValueError):
        InsertClip(media_id=FOOTAGE_C, at_phrase="a", at_s=3)  # at most one position


def test_a_strategy_without_insert_clips_serializes_and_hashes_exactly_as_before():
    strategy = CreativeStrategy.model_validate(_LEGACY_STRATEGY)
    assert "insert_clips" not in strategy.model_dump(mode="json")
    digest = hashlib.sha256(
        json.dumps(
            strategy.model_dump(mode="json", exclude_none=True), sort_keys=True, ensure_ascii=False
        ).encode()
    ).hexdigest()
    assert digest == "57d6b457f2981cd1fb48d6f6c475b2c9e753c01a1212ac4537b11b3ca9148607"


def test_the_field_stays_out_of_model_and_tool_schemas_but_the_wire_lets_the_model_author_it():
    from app.agents._creator_wire import MODEL_HIDDEN, WireContract, main_creator_schema
    from app.kria.registry import ApplyStrategyArguments

    for schema in (
        CreativeStrategy.model_json_schema(),
        ApplyStrategyArguments.model_json_schema(),
    ):
        assert "insert_clips" not in json.dumps(schema)
    assert "insert_clips" in MODEL_HIDDEN
    contract = WireContract(main_creator_schema())
    assert "insert_clips" in contract.source["$defs"]["CreativeStrategy"]["properties"]


def test_the_render_contract_and_placed_media_know_the_field():
    from app.kria.clip_scope import placed_media
    from app.services.creator_render_contract import FIELD_MATRIX

    for path in (
        "insert_clips[]",
        "insert_clips[].media_id",
        "insert_clips[].at_phrase",
        "insert_clips[].at_s",
        "insert_clips[].keep_audio",
    ):
        assert path in FIELD_MATRIX, path
    strategy = CreativeStrategy(edit_format="subtitled", insert_clips=[_insert()])
    assert FOOTAGE_C in placed_media(strategy)


# --- 4. the repair -----------------------------------------------------------------------------


def _repair(manifest, strategy):
    return capabilities.repair_creator_insert_clips(manifest, strategy)


def test_the_repair_keeps_an_insert_where_the_phone_can_pause_the_take(monkeypatch):
    _open(monkeypatch)
    manifest = _julio_manifest()
    repaired, notices = _repair(manifest, _strategy(manifest, insert_clips=[_insert()]))
    assert notices == []
    assert [c.media_id for c in repaired.insert_clips] == [FOOTAGE_C]
    assert capabilities.insert_clip_candidates(manifest)


@pytest.mark.parametrize(
    ("setup", "code"),
    [
        (
            lambda m: m.setattr(settings, "phone_talking_insert_clips_enabled", False),
            "insert_clips_off",
        ),
        (lambda m: m.setattr(settings, "talking_multi_clip_enabled", False), "insert_clips_off"),
    ],
)
def test_the_repair_drops_an_insert_while_the_rollout_is_closed(monkeypatch, setup, code):
    _open(monkeypatch)
    setup(monkeypatch)
    manifest = _julio_manifest()
    repaired, notices = _repair(manifest, _strategy(manifest, insert_clips=[_insert()]))
    assert repaired.insert_clips is None
    assert notices == [capabilities.INSERT_CLIP_NOTICES[code]]
    assert capabilities.insert_clip_candidates(manifest) == []


def test_the_repair_drops_what_the_phone_cannot_play_by_name(monkeypatch):
    _open(monkeypatch)
    manifest = _manifest(
        [
            {"media_id": FOOTAGE_A, "kind": "video", "duration_s": 12.9},
            {"media_id": FOOTAGE_C, "kind": "video", "duration_s": 3.4},
            {"media_id": "asset-bite", "kind": "video", "duration_s": 6.4},
            {"media_id": "asset-photo", "kind": "image"},
        ]
    )
    for media_id, code in (
        ("asset-bite", "visual_unsupported"),
        ("asset-photo", "not_video"),
        ("nope.mp4", "not_found"),
    ):
        repaired, notices = _repair(manifest, _strategy(manifest, insert_clips=[_insert(media_id)]))
        assert repaired.insert_clips is None, media_id
        assert notices == [capabilities.INSERT_CLIP_NOTICES[code]], media_id
    both, notices = _repair(
        manifest,
        _strategy(manifest, insert_clips=[_insert(FOOTAGE_C), _insert(FOOTAGE_C, at_phrase="x")]),
    )
    assert [c.media_id for c in both.insert_clips] == [FOOTAGE_C]
    assert notices == [capabilities.INSERT_CLIP_NOTICES["duplicate_clip"]]


def test_the_ending_clip_is_never_also_an_insert_and_a_single_clip_has_nothing_to_insert(
    monkeypatch,
):
    _open(monkeypatch)
    manifest = _julio_manifest()
    strategy = _strategy(
        manifest, ending_clip={"media_id": FOOTAGE_C, "keep_audio": True}, insert_clips=[_insert()]
    )
    full, _notices = capabilities._repair_creator_insert_clips(manifest, strategy)
    assert full.insert_clips is None
    single = _manifest([{"media_id": FOOTAGE_A, "kind": "video", "duration_s": 12.9}])
    repaired, notices = _repair(single, _strategy(single, insert_clips=[_insert(FOOTAGE_A)]))
    assert repaired.insert_clips is None
    assert notices == [capabilities.INSERT_CLIP_NOTICES["only_clip"]]


def test_a_montage_manifest_cannot_pause_a_take(monkeypatch):
    _open(monkeypatch)
    manifest = _manifest(
        [
            {"media_id": FOOTAGE_A, "kind": "video", "duration_s": 12.9},
            {"media_id": FOOTAGE_B, "kind": "video", "duration_s": 2.0},
        ],
        edit_format="montage",
    )
    assert capabilities.insert_clip_candidates(manifest) == []


# --- 5. the prompt -----------------------------------------------------------------------------


def _render_prompt(manifest) -> str:
    from app.agents.main_creator import MainCreatorAgent, MainCreatorInput

    return MainCreatorAgent(None).render_prompt(
        MainCreatorInput(
            user_message=ASK,
            media_context=[{"media_id": FOOTAGE_C, "kind": "video", "filename": "congress.mp4"}],
            capability_manifest=manifest,
        )
    )


def test_the_planner_is_taught_the_field_only_while_the_rollout_is_open(monkeypatch):
    from app.agents.main_creator import _insert_clip_section

    section = _insert_clip_section(cutaways_open=False)
    _open(monkeypatch, insert=True)
    manifest = _julio_manifest()
    open_prompt = _render_prompt(manifest)
    assert section in open_prompt
    monkeypatch.setattr(settings, "phone_talking_insert_clips_enabled", False)
    closed = _render_prompt(manifest)
    assert "insert_clips" not in closed and "A CLIP PLAYED IN THE MIDDLE" not in closed
    # Closed, the prompt is exactly the open one minus the section (no stray blank line).
    assert open_prompt.replace("\n" + section, "") == closed
    montage = _manifest(
        [
            {"media_id": FOOTAGE_A, "kind": "video", "duration_s": 12.9},
            {"media_id": FOOTAGE_B, "kind": "video", "duration_s": 2.0},
        ],
        edit_format="montage",
    )
    monkeypatch.setattr(settings, "phone_talking_insert_clips_enabled", True)
    assert "insert_clips" not in _render_prompt(montage)


def test_the_shared_brief_records_the_ask_as_an_insert_clip_fact():
    from app.agents._schemas.brief_extractor import BriefExtractionInput
    from app.agents.brief_extractor import BriefExtractorAgent
    from app.agents.main_creator import brief_prompt_section
    from app.kria.brief import STICKY_FACTS

    assert '"insert_clip"' in brief_prompt_section(True)
    assert {"insert_clip", "insert_at_phrase", "insert_at_s"} <= STICKY_FACTS
    narrow = BriefExtractorAgent(None).render_prompt(  # type: ignore[arg-type]
        BriefExtractionInput(
            creator_request="", user_message="x", conversation=[], insert_clips_open=True
        )
    )
    assert '"insert_clip"' in narrow


# --- 6. the receipts ---------------------------------------------------------------------------


def _facts_for_draft(strategy, manifest):
    return dataclasses.replace(
        plan_facts_from_strategy(
            strategy.model_dump(mode="json", exclude_none=True), manifest=manifest
        ),
        phone_talking=True,
    )


def _verdict(facts, req=None):
    (receipt,) = build_receipts([req or _req()], facts, include_unchecked=True, strict_order=True)
    return receipt


def test_a_draft_is_met_only_when_the_repaired_strategy_keeps_the_insert(monkeypatch):
    _open(monkeypatch)
    manifest = _julio_manifest()
    kept = _facts_for_draft(_strategy(manifest, insert_clips=[_insert()]), manifest)
    assert kept.insert_clips == (InsertClipFact("planned", anchor="phrase"),)
    assert _verdict(kept).status == "met"
    none = _facts_for_draft(_strategy(manifest), manifest)
    assert _verdict(none).status == "not_possible"
    assert none.insert_clips == ()
    monkeypatch.setattr(settings, "phone_talking_insert_clips_enabled", False)
    closed = _verdict(_facts_for_draft(_strategy(manifest), manifest))
    assert closed.status == "not_possible" and "available yet" in closed.reason
    off = _facts_for_draft(_strategy(manifest, insert_clips=[_insert()]), manifest)
    verdict = _verdict(off)
    assert off.insert_clips == (
        InsertClipFact("dropped", reason="insert_clips_off", anchor="phrase"),
    )
    assert verdict.status == "not_possible"
    assert "available yet" in verdict.reason


def test_a_finished_video_is_judged_from_the_workers_receipt_not_the_strategy():
    base = {
        "resolved_archetype": "subtitled",
        "render_destination": "device",
        "render_status": "ready",
    }
    placed = plan_facts_from_phone_variant(
        {**base, "phone_insert_clips": {"placed": [{"media_id": FOOTAGE_C}], "dropped": []}}
    )
    assert placed.insert_clips == (InsertClipFact("applied"),)
    assert _verdict(placed).status == "met"
    refused = plan_facts_from_phone_variant(
        {
            **base,
            "phone_insert_clips": {
                "placed": [],
                "dropped": [{"media_id": FOOTAGE_C, "reason": "phrase_not_found"}],
            },
        }
    )
    verdict = _verdict(refused)
    assert verdict.status == "not_possible"
    assert "didn't hear you say" in verdict.reason
    silent = plan_facts_from_phone_variant(base)
    assert _verdict(silent).status == "not_possible"
    mixed = plan_facts_from_phone_variant(
        {
            **base,
            "phone_insert_clips": {
                "placed": [{"media_id": "a"}],
                "dropped": [{"media_id": "b", "reason": "too_long"}],
            },
        }
    )
    assert _verdict(mixed).status == "partial"


def test_the_turkish_twin_exists_for_every_reason_the_worker_can_write():
    from app.kria import brief_checks as bc
    from app.kria.reply_language import reply_language_for  # noqa: F401

    worker_reasons = [
        "clip_not_found",
        "not_a_video",
        "too_long",
        "duplicate_clip",
        "clip_in_use",
        "too_many",
        "phrase_not_found",
        "in_hook_window",
        "in_tail_window",
        "too_close_to_previous",
        "speaker_background",
        "talking_multi_off",
        "only_clip",
    ]
    generic = bc._insert_clip_why("zzz")
    known = [r for r in worker_reasons if bc._insert_clip_why(r) != generic]
    assert set(known) == set(worker_reasons)


# --- 5b. a closed rollout leaves BOTH prompts as they were (review of #2058, MAJOR 3) ---------


def _brief_prompt(manifest, *, brief=True) -> str:
    from app.agents.main_creator import MainCreatorAgent, MainCreatorInput

    return MainCreatorAgent(None).render_prompt(
        MainCreatorInput(
            user_message="x",
            media_context=[{"media_id": FOOTAGE_C, "kind": "video", "filename": "congress.mp4"}],
            capability_manifest=manifest,
            brief_enabled=brief,
        )
    )


def _extractor_prompt(**kw) -> str:
    from app.agents._schemas.brief_extractor import BriefExtractionInput
    from app.agents.brief_extractor import BriefExtractorAgent

    return BriefExtractorAgent(None).render_prompt(  # type: ignore[arg-type]
        BriefExtractionInput(creator_request="", user_message="x", conversation=[], **kw)
    )


# sha256 of the shared brief section at origin/main before the insert paragraph existed.
# KRI-1162 (PR #2045) adds the `speaker_background` requirement fact to the brief
# section (KRI-1161); without that sentence the digest is
# a3798035e169f3aeff144526a6d8f9bf3f044b0d0985267fb1547831f17a4386.
# KRI-1177 / KRI-1164 add the caption look facts and `audio_intent: no_music`, and KRI-1173 one
# `background_color` clause; without those passages the digest is
# 90ef88660728fd8938da0de42c9874b73d52e25664c5633a6c8b2fd63066a718.
# KRI-1189 (creator v119) teaches `cut_restarts` in the cleanup sentence; with v118's sentence the
# digest is c3c94cd31f4cdd6dcea91de409be84016351caaa111403fb43258c7c24c0114c.
# Qendresa 13 (creator v121, on top of Qendresa 12's v120 caption facts) adds the `pauses: "tight"`
# clause to the cleanup sentence; without both
# it the digest is 562441ab494ae1bf4c0f79dd9811163c782b8548712f05ed7334ceaba675ffe6.
# Qendresa item 17 (creator v123, on top of v121) leaves the brief section unchanged: the pin
# covers v121 + v123. Qendresa item 16 (creator v125, now v133) changes it only with its flag on.
# Says-done group E (#2131) rewords the caption-count and cleanup sentences (her Q1/Q2
# sentences as examples); with v136's sentences the digest is
# 14ad8c6e284569817ed3c380fea76ae35b5983b1fe42a2f53c42a6c14bcaa6d7.
# KRI-971 (keep-a-take-whole also records speech_cleanup false) moves it from 001bd937bb08cea8….
# KRI-1292 (`hold_s` for a text at a spoken word) moves it from 5d020e50a866f3d4….
_BRIEF_SECTION_BEFORE_INSERTS = "95a08fb8d26594ee15ecc44d0a05c1597e276823f8607b4c8230d1207fa4786e"


def test_a_closed_rollout_leaves_the_brief_section_byte_identical_to_before_inserts():
    from app.agents.main_creator import _BRIEF_PROMPT_SECTION, brief_prompt_section

    assert brief_prompt_section(False) == _BRIEF_PROMPT_SECTION
    assert hashlib.sha256(_BRIEF_PROMPT_SECTION.encode()).hexdigest() == (
        _BRIEF_SECTION_BEFORE_INSERTS
    )
    assert "insert_clip" not in _BRIEF_PROMPT_SECTION
    assert "insert_clip" in brief_prompt_section(True)


def test_the_creator_prompt_with_the_brief_on_changes_only_while_inserts_are_open(monkeypatch):
    from app.agents.main_creator import _BRIEF_PROMPT_SECTION, brief_prompt_section

    _open(monkeypatch, insert=False)
    manifest = _julio_manifest()
    closed = _brief_prompt(manifest)
    assert "insert_clip" not in closed and "IN THE MIDDLE of the talk" not in closed
    assert "\n" + _BRIEF_PROMPT_SECTION in closed
    # A montage / single-clip manifest is untouched even with the rollout open.
    monkeypatch.setattr(settings, "phone_talking_insert_clips_enabled", True)
    montage = _manifest(
        [
            {"media_id": FOOTAGE_A, "kind": "video", "duration_s": 12.9},
            {"media_id": FOOTAGE_B, "kind": "video", "duration_s": 2.0},
        ],
        edit_format="montage",
    )
    assert "insert_clip" not in _brief_prompt(montage)
    opened = _brief_prompt(manifest)
    assert "\n" + brief_prompt_section(True) in opened
    assert opened.replace(brief_prompt_section(True), _BRIEF_PROMPT_SECTION) != opened


def test_the_brief_extractor_prompt_teaches_inserts_only_when_told_they_are_open():
    from app.agents.brief_extractor import _NARROW_BRIEF_SECTION

    assert "insert_clip" not in _extractor_prompt()
    assert "insert_clip" not in _NARROW_BRIEF_SECTION
    assert '"insert_clip"' in _extractor_prompt(insert_clips_open=True)


def test_the_planner_tells_the_brief_extractor_whether_inserts_are_open(monkeypatch):
    import asyncio
    import uuid

    from app.agents.main_creator import MainCreatorInput
    from app.kria import planner

    seen = []

    class _Agent:
        def __init__(self, _client):
            pass

        def run(self, agent_input, ctx=None):  # noqa: ARG002
            seen.append(agent_input.insert_clips_open)
            return planner.BriefExtractionOutput(brief_updates=[])

    monkeypatch.setattr(planner, "BriefExtractorAgent", _Agent)
    monkeypatch.setattr(planner, "default_client", lambda: None)
    manifest = _julio_manifest()
    inputs = planner._CreatorInputs(
        agent_input=MainCreatorInput(user_message="x", capability_manifest=manifest),
        intent_clips=[],
        creator_request=None,
    )

    def _call():
        return asyncio.run(
            planner._call_brief_extractor(
                inputs,
                creator_request="",
                thread_id=uuid.uuid4(),
                creator_id=uuid.uuid4(),
                item_id=uuid.uuid4(),
                creator_agent_session_id=None,
            )
        )

    _open(monkeypatch, insert=False)
    _call()
    monkeypatch.setattr(settings, "phone_talking_insert_clips_enabled", True)
    _call()
    assert seen == [False, True]


def test_only_an_explicit_pause_is_an_insert_show_it_when_i_mention_it_is_not():
    from app.agents.main_creator import _insert_clip_section, brief_prompt_section

    for text in (
        brief_prompt_section(True),
        _insert_clip_section(cutaways_open=False),
        _insert_clip_section(cutaways_open=True),
    ):
        # The ask that keeps the talk running is served by the existing cutaway features.
        ask = "kongre dediğimde kongre videosunu"
        assert ask in text
        assert "NOT an insert" in text[text.index(ask) : text.index(ask) + 160]
        assert "NOT an insert" in text
        assert "keeps running" in text
        assert "pause" in text.lower()


def test_the_limit_is_the_engines_and_both_copies_of_it_agree():
    from app.pipeline.phone_subtitled_plan import MAX_INSERT_CLIPS as ENGINE_MAX

    assert ENGINE_MAX == MAX_INSERT_CLIPS == 20


def test_a_fourth_insert_is_kept_and_only_one_past_the_engine_limit_is_dropped_by_name(
    monkeypatch,
):
    _open(monkeypatch)
    ids = [f"clip-{i}.mp4" for i in range(MAX_INSERT_CLIPS + 3)]
    manifest = _manifest([{"media_id": m, "kind": "video", "duration_s": 5.0} for m in ids])
    strategy = _strategy(
        manifest, insert_clips=[_insert(m, at_phrase=f"word {i}") for i, m in enumerate(ids[1:])]
    )
    repaired, notices = _repair(manifest, strategy)
    assert [c.media_id for c in repaired.insert_clips] == ids[1 : MAX_INSERT_CLIPS + 1]
    assert notices == [capabilities.INSERT_CLIP_NOTICES["too_many"]] * 2
    assert "20" in capabilities.INSERT_CLIP_NOTICES["too_many"]
    problems = capabilities.insert_clips_problems(manifest, strategy)
    assert problems == [None] * MAX_INSERT_CLIPS + ["too_many"] * 2
    facts = _facts_for_draft(strategy, manifest)
    assert [row.reason for row in facts.insert_clips if row.status == "dropped"] == ["too_many"] * 2
    assert _verdict(facts).status == "partial"
    four = _strategy(
        manifest, insert_clips=[_insert(m, at_phrase=f"word {i}") for i, m in enumerate(ids[1:5])]
    )
    kept, none = _repair(manifest, four)
    assert len(kept.insert_clips) == 4 and none == []
