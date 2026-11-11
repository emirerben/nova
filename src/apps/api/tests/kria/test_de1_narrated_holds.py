"""de1 (KRI-1353): Leverkusen N3, a German phone Voiceover chat, replayed offline.

Recorded data: ``tests/kria/de1_leverkusen_n3.json`` (scrubbed; see its ``source``). Prod took
28 min and five holds to a video: "Make it without these" looped (KRI-1354), "Kein Titel" was
held as an unmet title (KRI-1355), "Null zu null" never met the transcript's "0 zu 0"
(KRI-1356), footage on a spoken word was refused as "No pop-in" (KRI-1357), the announcer under
the voice was refused as "only works in a voiceover edit made on your iPhone" on the iPhone
(KRI-1358) and "Change my request" kept the old asks (KRI-1360).

Ways the fixes could go wrong, written down before the code:

KRI-1358 (readiness reads the phone Voiceover fact)
1. Readiness still refuses under-voice on a phone Voiceover draft (the bug).
2. Readiness now calls a cloud or montage draft a phone Voiceover draft and promises clip sound
   the cloud writer never plays.
3. Giving readiness the narrated media snapshot changes another requirement's verdict that the
   draft turn already judged the same way (the snapshot was Talking-only before).
4. The kill switch does not restore the old verdict.

KRI-1354 ("Make it without these" looped)
5. Unreadable scope words are treated as silence and drop an explicit all-media ask: a German
   "Nutze alle meine Fotos und Videos" (the scope reader knows only EN/TR) would turn the
   Visuals into a notice and draft without asking. The first "simpler version?" (hold A) is an
   honest question and stays; only its repeat is the bug.
6. The fix asks hold A more than once in the de1 sequence (the loop survives).
7. The fix skips hold A itself (consent taken before it was asked).
8. The "simpler version" tap on the strategy refusal is lost once the readiness card is tapped,
   so the planner refuses again with the same question (the loop).
9. The carried consent outlives a new typed request ("Make it 30 seconds"), a "Change my
   request" tap, or a draft that was applied since: an old yes covers a new ask.
10. The carried consent covers MORE than the card offered (a refusal that now also drops
    another field is drafted without asking).
11. A tap on the receipts path's card (no dropped fields) is taken as consent to a policy
    refusal it never named.
12. The kill switch does not restore per-turn consent.

KRI-1355 ("Kein Titel" held as an unmet title)
13. The typed `title: "off"` fact is ignored and the no-title ask is held as "I didn't add a
    title" (the bug).
14. A no-title ask is called met while the draft HAS a title (it must say "Couldn't").
15. The met receipt is not approvable (readiness still holds it).
16. The unified montage still asks "what words for the title?" for a no-title ask.
17. The brief model is never taught the fact (the prompt line is missing), so prod never
    records it; or the kill switch changes receipts when off.

KRI-1360 ("Change my request" kept the old asks)
18. The brief model is not told the message answers "Change my request", so it only adds
    deltas and the old asks stay.
19. The marker leaks onto a later message, a tap, or a turn that followed a different answer
    (it must mark only the typed message right after the change_request tap).
20. The server decides which asks to drop by reading words (it must be the model's removals).
21. The kill switch leaves the input marked.
21b. The marker over-removes: "Change my request" + a message restating only some asks drops
     restrictions it does not repeat (de1 message 3 would lose "Kein Titel" and "mute
     everything else", so the next draft gets a title or clip sound with no receipt).

KRI-1356 ("Null zu null" never met "0 zu 0")
22. The render still reports never_heard for a cue the voice says in another written form
    (digits vs words, a dropped "1.", inflection) although a model could locate it (the bug).
23. The resolver places a cue on a DIFFERENT moment (a span it invented, ids that don't exist,
    end before start, a 20-word span, one cue twice, a cue id nobody asked about) and the
    grounding trusts it: strict parse rejects every one of those, never a partial answer.
24. The resolver's heard words leak into the receipt as the creator's trigger, so the review
    compares the wrong text and says "I never heard 0 zu 0": the receipt keeps the creator's
    trigger (by beat id) and carries the heard words as `heard_as`.
25. A resolver failure, timeout or null answer fails the render or changes anything (it must be
    exactly today's never_heard receipt).
26. The resolver runs (and costs a model call) for a cue the matcher already hears exactly, or
    runs once per beat instead of once per render.
27. The kill switch still calls the model, or changes grounding output by a byte.
28. Before approval a Voiceover draft says "Done" for a pop-in no voice has been matched
    against; the promise that replaces it is not approvable (held again), or reaches Talking /
    montage drafts, or a finished render (whose receipt decides: placed = met, never heard =
    the honest "I never heard").

KRI-1357 (footage on a spoken word refused as "No pop-in")
29. A Voiceover draft still holds "show my footage clip at word X" as "No pop-in for X"
    (needs_choice) although the narration places footage on the words, not a pop-in.
30. The server tells footage from a Visuals pop-in by reading the ask's words (forbidden): with
    no typed signal it must not guess, only promise what the render checks.
31. The promise leaks to a Talking or montage draft, or to a finished render.
32. The finished render still says "No pop-in" without looking at what played at those words,
    or calls a clip "met" because it was merely on screen somewhere.
33. The kill switch does not restore "No pop-in".
"""

from __future__ import annotations

import json
import uuid
from pathlib import Path

import pytest

from app.config import settings
from app.kria.brief import BriefRequirement, CreativeBrief

FIXTURE = json.loads((Path(__file__).parent / "de1_leverkusen_n3.json").read_text("utf-8"))
CREATOR = uuid.UUID("00000000-0000-4000-8000-0000000de100")
UNDER_VOICE_REFUSAL = FIXTURE["replies"]["under_voice_reason"]


def _req(rid: str) -> BriefRequirement:
    [row] = [r for r in FIXTURE["brief_v1"]["requirements"] if r["id"] == rid]
    return BriefRequirement.model_validate(row)


def _media(clip_root: str = "users/de1/analysis-proxies") -> dict:
    clips = FIXTURE["clips"]
    return {
        "clip_paths": [f"{clip_root}/{c['media_id']}" for c in clips],
        "clip_assignments": clips,
        "voiceover": {"duration_s": FIXTURE["voiceover"]["duration_s"]},
        "voiceover_duration_s": FIXTURE["voiceover"]["duration_s"],
    }


def _strategy(edit_format: str = "narrated_planned") -> dict:
    clips = [c["media_id"] for c in FIXTURE["clips"]]
    return {
        "edit_format": edit_format,
        "audio_strategy": "voiceover",
        "render_program": "native",
        "media_scope": "selected",
        "selected_media_ids": clips,
        "rationale": "Follow the German voiceover.",
    }


def _manifest(strategy: dict, *, phone: bool):
    from app.services import creator_capabilities as capabilities  # noqa: PLC0415

    clips = FIXTURE["clips"]
    return capabilities.resolve_creator_manifest(
        item_id="item-de1",
        edit_format=strategy["edit_format"],
        has_voiceover=True,
        media=[{k: c[k] for k in ("media_id", "kind", "duration_s")} for c in clips],
        phone_source_media_ids=[c["media_id"] for c in clips] if phone else None,
        phone_rendering_allowed=phone,
        runs_guided_voiceover=False,
    )


def _readiness(strategy: dict, media: dict, *, phone: bool = True) -> dict:
    from app.kria.approval_readiness import assess_readiness  # noqa: PLC0415

    brief = CreativeBrief(version=1, requirements=[_req("r7")])
    report = assess_readiness(
        brief,
        strategy,
        media=media,
        manifest=_manifest(strategy, phone=phone),
        creator_id=CREATOR,
    )
    return {row["requirement_id"]: row for row in report["requirements"]}


@pytest.fixture
def phone_voiceover(prod_profile, monkeypatch: pytest.MonkeyPatch) -> None:
    from app.services.phone_rollout import phone_render_supported_formats  # noqa: PLC0415

    monkeypatch.setattr(settings, "phone_render_user_ids", [])
    assert "narrated_planned" in phone_render_supported_formats(), "prod profile renders Voiceover"


# -- KRI-1358 -------------------------------------------------------------------------------------


def test_under_voice_on_the_phone_voiceover_draft_is_not_refused(phone_voiceover) -> None:
    from app.kria.approval_readiness import APPROVABLE  # noqa: PLC0415

    row = _readiness(_strategy(), _media())["r7"]
    assert row["disposition"] in APPROVABLE, row
    assert row["reason"] != UNDER_VOICE_REFUSAL


def test_the_kill_switch_restores_the_old_refusal(
    phone_voiceover, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(settings, "narrated_readiness_phone_facts_enabled", False)
    row = _readiness(_strategy(), _media())["r7"]
    assert row["disposition"] == "unsupported"
    assert row["reason"] == UNDER_VOICE_REFUSAL


def test_a_cloud_voiceover_draft_still_names_the_phone_limit(phone_voiceover) -> None:
    # No analysis proxies -> the cloud writer renders it, which plays no per-clip sound.
    media = _media() | {
        "clip_paths": [
            f"users/de1/originals/{c['media_id'].replace('analysis-proxy-', '')}"
            for c in FIXTURE["clips"]
        ]
    }
    row = _readiness(_strategy(), media, phone=False)["r7"]
    assert row["disposition"] == "unsupported"
    assert row["reason"] == UNDER_VOICE_REFUSAL


# -- KRI-1354: "Make it without these" looped -----------------------------------------------------

DE1_VISUALS = ["asset-de1-lineup", "asset-de1-score", "asset-de1-player"]


def _de1_visuals_manifest():
    """The de1 project as the policy saw it: two own clips, three Visuals photos, a voiceover."""
    from app.services import creator_capabilities as capabilities  # noqa: PLC0415

    clips = [{k: c[k] for k in ("media_id", "kind", "duration_s")} for c in FIXTURE["clips"]]
    photos = [{"media_id": m, "kind": "image", "duration_s": None} for m in DE1_VISUALS]
    return capabilities.resolve_creator_manifest(
        item_id="item-de1",
        edit_format="narrated_planned",
        has_voiceover=True,
        narration={
            "gcs_path": "users/de1/voiceover.m4a",
            "duration_s": FIXTURE["voiceover"]["duration_s"],
            "generation": "1",
        },
        media=[*clips, *photos],
        phone_source_media_ids=[c["media_id"] for c in clips],
        phone_rendering_allowed=True,
        runs_guided_voiceover=False,
    )


def _all_scope_with_two_beats():
    from app.agents._schemas.creator_agent import CreativeStrategy  # noqa: PLC0415

    return CreativeStrategy.model_validate(
        {
            **_strategy(),
            "media_scope": "all",
            "selected_media_ids": [],
            "reaction_beats": [
                {
                    "beat_id": "lineup",
                    "trigger": "Aufstellung",
                    "visual_id": "asset-de1-lineup",
                    "visual_role": "photo",
                },
                {
                    "beat_id": "score",
                    "trigger": "Null zu null",
                    "visual_id": "asset-de1-score",
                    "visual_role": "photo",
                },
            ],
        }
    )


def _all_scope_check(creator_text: str):
    from app.kria.strategy_policy import check_strategy_for_runtime_v2  # noqa: PLC0415

    return check_strategy_for_runtime_v2(
        _de1_visuals_manifest(),
        _all_scope_with_two_beats(),
        ask_before_simplifying=True,
        creator_text=creator_text,
        requested_media={},
    )


@pytest.fixture
def _rollout(prod_profile, monkeypatch: pytest.MonkeyPatch) -> None:
    from app.services import creator_capabilities as capabilities  # noqa: PLC0415

    monkeypatch.setattr(capabilities.settings, "main_creator_agent_rollout_percent", 100)


@pytest.mark.parametrize(
    "creator_text",
    [
        "\n".join(FIXTURE["messages"]),
        # Explicit all-media asks, read or not by the EN/TR scope reader: never dropped.
        "Nutze alle meine Fotos und Videos",
        "Use all my photos and videos",
        "Fotoğrafların ve videoların hepsini kullan",
    ],
    ids=["de1-messages", "de-all-media", "en-all-media", "tr-all-media"],
)
def test_hold_a_is_still_asked_once(_rollout, creator_text: str) -> None:
    """The first "Make it without these?" is honest: a Visual no word places is left out of an
    all-scope voiceover edit, and nothing reads unknown words as consent to that."""
    from app.kria.strategy_policy import RefusedStrategy  # noqa: PLC0415

    checked = _all_scope_check(creator_text)

    assert isinstance(checked, RefusedStrategy), checked
    assert checked.fields == ("left_out_media",)
    if creator_text == "\n".join(FIXTURE["messages"]):
        assert checked.question == FIXTURE["replies"]["hold_left_out_media"]


def _de1_simpler_thread(*after: tuple[str, dict]) -> list[tuple[str, dict]]:
    """de1 T1-T3 as the planner reads them: the policy refusal's card, the simpler tap, the
    readiness hold's card (it names brief ids, no fields), and the tap on that card."""
    from app.services.choice_questions import (  # noqa: PLC0415
        SIMPLER_OMITTED_IDS_KEY,
        simpler_version_question,
        tag_event,
    )

    refusal = simpler_version_question(("left_out_media",))
    readiness = simpler_version_question()
    readiness[SIMPLER_OMITTED_IDS_KEY] = ["r2", "r3", "r7", "r8"]

    def tap(question: dict) -> tuple[str, dict]:
        selection = {"question_id": question["question_id"], "option_key": "simpler_version"}
        return tag_event(
            "user", {"choice_selection": selection}, "user_message", "Make it without these"
        )

    return [
        tag_event("user", {}, "user_message", FIXTURE["messages"][0]),
        tag_event(
            "assistant",
            {"turn_value": "question", "choice_question": refusal},
            "assistant_response",
            FIXTURE["replies"]["hold_left_out_media"],
        ),
        tap(refusal),
        tag_event(
            "assistant",
            {
                "turn_value": "question",
                "choice_question": readiness,
                "brief_coverage": {"reason": "approval_requirements_not_ready"},
            },
            "assistant_response",
            "I can't make this yet.",
        ),
        tap(readiness),
        *after,
    ]


def test_the_policy_tap_still_counts_after_the_readiness_card_is_tapped(prod_profile) -> None:
    from app.kria.planner import _simpler_version_consented  # noqa: PLC0415

    assert _simpler_version_consented(_de1_simpler_thread(), ("left_out_media",))


def test_one_simpler_tap_on_hold_a_ends_the_de1_sequence(_rollout) -> None:
    """de1 T1-T3: hold A is asked (T1), tapped once (T2), the readiness card is tapped (T3); the
    planner's re-run of the same refusal now drafts what the check keeps instead of asking hold
    A again. Before the tap nothing is consented."""
    from app.kria.planner import _simpler_version_consented  # noqa: PLC0415
    from app.kria.strategy_policy import (  # noqa: PLC0415
        CheckedStrategy,
        RefusedStrategy,
        check_strategy_for_runtime_v2,
    )

    hold_a = _all_scope_check("\n".join(FIXTURE["messages"]))
    assert isinstance(hold_a, RefusedStrategy)
    thread = _de1_simpler_thread()
    # T1: hold A asked, no tap yet -> the question stands.
    assert not _simpler_version_consented(thread[:2], hold_a.fields)
    # T3: after ONE tap on hold A and a tap on the readiness card, the same refusal is consented.
    assert _simpler_version_consented(thread, hold_a.fields)
    drafted = check_strategy_for_runtime_v2(
        _de1_visuals_manifest(),
        _all_scope_with_two_beats(),
        creator_text="\n".join(FIXTURE["messages"]),
        requested_media={},
    )
    assert isinstance(drafted, CheckedStrategy), drafted
    assert sorted(drafted.strategy.selected_media_ids) == sorted(
        c["media_id"] for c in FIXTURE["clips"]
    )


def test_the_carried_tap_never_covers_more_or_a_new_request(prod_profile) -> None:
    from app.kria.planner import _simpler_version_consented  # noqa: PLC0415
    from app.services.choice_questions import (  # noqa: PLC0415
        CHANGE_REQUEST_KEY,
        simpler_version_question,
        tag_event,
    )

    fields = ("left_out_media",)
    # A refusal that now also drops something the card never offered is asked again.
    assert not _simpler_version_consented(_de1_simpler_thread(), (*fields, "opening_title"))
    # A typed message is a new request: the old tap ends there.
    typed = tag_event("user", {}, "user_message", "Mach es ohne die anderen Fotos")
    assert not _simpler_version_consented(_de1_simpler_thread(typed), fields)
    # A draft applied since the tap ends it.
    drafted = _de1_simpler_thread()
    drafted.insert(-1, tag_event("assistant", {"turn_id": "t"}, "draft_applied", "Draft ready."))
    assert not _simpler_version_consented(drafted, fields)
    # "Change my request" on the readiness card ends it.
    changed = _de1_simpler_thread()
    readiness = changed[3][1]["choice_question"]
    changed[-1] = tag_event(
        "user",
        {
            "choice_selection": {
                "question_id": readiness["question_id"],
                "option_key": CHANGE_REQUEST_KEY,
            }
        },
        "user_message",
        "Change my request",
    )
    assert not _simpler_version_consented(changed, fields)
    # Only readiness / receipts cards were tapped: nothing named these fields.
    receipts_only = _de1_simpler_thread()[3:]
    assert not _simpler_version_consented(receipts_only, fields)
    # A tap on a card that was not the open question.
    stray = simpler_version_question(fields)
    stale = _de1_simpler_thread()
    stale[2] = tag_event(
        "user",
        {
            "choice_selection": {
                "question_id": stray["question_id"],
                "option_key": "simpler_version",
            }
        },
        "user_message",
        "Make it without these",
    )
    assert not _simpler_version_consented(stale, fields)


@pytest.mark.asyncio
@pytest.mark.parametrize("fixed", [True, False], ids=["fix-on", "kill-switch"])
async def test_the_real_planner_drafts_after_the_readiness_tap(
    prod_profile, monkeypatch: pytest.MonkeyPatch, fixed: bool
) -> None:
    """The de1 sequence through the REAL planner and thread (Postgres; skipped without it):
    policy refusal -> simpler tap -> readiness hold card -> simpler tap -> the same refusal.
    The KRI-759 harness's 20 s cap stands in for the left-out Visuals (any dropped field)."""
    from unittest.mock import AsyncMock  # noqa: PLC0415

    from app.database import engine as async_engine  # noqa: PLC0415
    from app.database import sync_session  # noqa: PLC0415
    from app.kria import planner  # noqa: PLC0415
    from app.models import CreationThread  # noqa: PLC0415
    from app.services.choice_questions import (  # noqa: PLC0415
        SIMPLER_OMITTED_IDS_KEY,
        simpler_version_question,
    )
    from app.tasks.kria_runtime import _append_sync_event  # noqa: PLC0415
    from tests.kria import test_kri759_simpler_version_refusal as k759  # noqa: PLC0415

    # The KRI-759 harness's flags (its autouse fixture does not reach this module).
    monkeypatch.setattr(settings, "kria_runtime_v2_enabled", True)
    monkeypatch.setattr(settings, "main_creator_agent_enabled", True)
    monkeypatch.setattr(settings, "kria_choice_questions_enabled", True)
    monkeypatch.setattr(type(planner.settings), "brief_binding_for", lambda _s, _i: True)
    monkeypatch.setattr(
        planner,
        "plan_and_resolve_clip_intents",
        AsyncMock(return_value=k759.PlannedIntentResolution([], k759.IntentResolution(intents=[]))),
    )
    monkeypatch.setattr(planner, "_kick_clip_understanding", AsyncMock())
    monkeypatch.setattr(settings, "narrated_simpler_consent_fixes_enabled", fixed)
    user_id, thread_id, item_id = k759._seed_runtime_project()
    try:
        refusal = k759._persist_assistant_question(
            thread_id, await k759._plan(thread_id, item_id, "20 saniye olsun")
        )
        tap = {"question_id": refusal["question_id"], "option_key": "simpler_version"}
        await k759._send(user_id, thread_id, "Make it without these", tap)
        k759._drafted(await k759._plan(thread_id, item_id, "Make it without these"))
        # Approval readiness held that draft: its own card names brief ids, no fields.
        readiness = simpler_version_question()
        readiness[SIMPLER_OMITTED_IDS_KEY] = ["r2", "r3"]
        with sync_session() as db:
            thread = db.get(CreationThread, thread_id, with_for_update=True)
            _append_sync_event(
                db,
                thread,
                role="assistant",
                event_type="assistant_response",
                content="I can't make this yet.",
                payload={
                    "turn_value": "question",
                    "choice_question": readiness,
                    "brief_coverage": {"reason": "approval_requirements_not_ready"},
                },
            )
            db.commit()
        tap = {"question_id": readiness["question_id"], "option_key": "simpler_version"}
        await k759._send(user_id, thread_id, "Make it without these", tap)

        planned = await k759._plan(thread_id, item_id, "Make it without these")

        if fixed:
            assert k759._drafted(planned)["strategy"]["target_duration_s"] == 15
        else:
            assert planned.plan.mode == "respond"
            assert planned.plan.choice_question["dropped_fields"] == ["target_duration_s"]
    finally:
        await async_engine.dispose()


def test_the_kill_switch_keeps_consent_per_turn(
    prod_profile, monkeypatch: pytest.MonkeyPatch
) -> None:
    from app.kria.planner import _simpler_version_consented  # noqa: PLC0415

    monkeypatch.setattr(settings, "narrated_simpler_consent_fixes_enabled", False)
    assert not _simpler_version_consented(_de1_simpler_thread(), ("left_out_media",))


# -- KRI-1355: "Kein Titel" held as an unmet title ------------------------------------------------

TITLE_OFF = "No title, as asked."


def _no_title_req(**facts) -> BriefRequirement:
    return _req("r8").model_copy(update={"facts": facts})


def _title_readiness(req: BriefRequirement, strategy: dict) -> dict:
    from app.kria.approval_readiness import assess_readiness  # noqa: PLC0415

    report = assess_readiness(
        CreativeBrief(version=1, requirements=[req]),
        strategy,
        media=_media(),
        manifest=_manifest(strategy, phone=True),
        creator_id=CREATOR,
    )
    return {row["requirement_id"]: row for row in report["requirements"]}["r8"]


def test_a_typed_no_title_ask_is_met_and_approvable(phone_voiceover) -> None:
    from app.kria.approval_readiness import APPROVABLE  # noqa: PLC0415

    row = _title_readiness(_no_title_req(title="off"), _strategy())

    assert row["disposition"] in APPROVABLE, row
    assert row["reason"] == TITLE_OFF


def test_a_no_title_ask_with_a_title_on_the_draft_is_couldnt() -> None:
    from app.kria.brief_checks import PlanFacts, _check_title  # noqa: PLC0415

    receipt = _check_title(_no_title_req(title="off"), PlanFacts(title="Leverkusen gegen Nürnberg"))
    assert receipt.status == "not_possible"
    assert receipt.reason == "This draft has a title, but you asked for none."
    assert _check_title(_no_title_req(title="off"), PlanFacts()).status == "met"


def test_the_no_title_kill_switch_restores_the_old_hold(
    phone_voiceover, monkeypatch: pytest.MonkeyPatch
) -> None:
    from app.kria.brief_checks import NO_TITLE_REASON  # noqa: PLC0415

    monkeypatch.setattr(settings, "title_off_fact_enabled", False)
    row = _title_readiness(_no_title_req(title="off"), _strategy())

    assert row["disposition"] == "needs_choice", row
    assert row["reason"] == NO_TITLE_REASON == FIXTURE["replies"]["no_title_reason"]


def test_an_untyped_no_title_ask_is_still_held(phone_voiceover) -> None:
    """No word reading: the recorded r8 (facts {}) stays what it was; only the model's typed
    fact changes the verdict."""
    from app.kria.brief_checks import NO_TITLE_REASON  # noqa: PLC0415

    row = _title_readiness(_req("r8"), _strategy())
    assert row["disposition"] == "needs_choice", row
    assert row["reason"] == NO_TITLE_REASON


def test_the_brief_prompts_teach_the_no_title_fact() -> None:
    from app.agents.brief_extractor import _NARROW_BRIEF_SECTION  # noqa: PLC0415
    from app.agents.main_creator import _BRIEF_PROMPT_SECTION  # noqa: PLC0415

    for section in (_BRIEF_PROMPT_SECTION, _NARROW_BRIEF_SECTION):
        assert '{"title": "off"}' in section


def test_the_recorded_kein_titel_answer_parses_into_a_met_no_title_ask() -> None:
    """Replay only: the hand-authored golden answer, re-parsed by the REAL extractor parse and
    judged by the REAL checker. It proves the shape, not that the live model writes it."""
    from app.agents.brief_extractor import (  # noqa: PLC0415
        BriefExtractionInput,
        BriefExtractorAgent,
    )
    from app.kria.brief import apply_updates  # noqa: PLC0415
    from app.kria.brief_checks import PlanFacts, _check_title, title_off  # noqa: PLC0415

    golden = json.loads(
        (
            Path(__file__).parents[1]
            / "fixtures/agent_evals/brief_extractor/kri1355_kein_titel_de.json"
        ).read_text("utf-8")
    )
    agent = BriefExtractorAgent(model_client=None)
    output = agent.parse(golden["raw_text"], BriefExtractionInput(**golden["input"]))
    [req] = apply_updates(None, output.brief_updates, source_turn_id=None).live()

    assert title_off(req)
    assert _check_title(req, PlanFacts()).reason == TITLE_OFF


# -- KRI-1360: "Change my request" kept the old asks ----------------------------------------------


def _change_request_thread(*after: tuple[str, dict]) -> list[tuple[str, dict]]:
    from app.services.choice_questions import (  # noqa: PLC0415
        CHANGE_REQUEST_KEY,
        SIMPLER_OMITTED_IDS_KEY,
        simpler_version_question,
        tag_event,
    )

    card = simpler_version_question()
    card[SIMPLER_OMITTED_IDS_KEY] = ["r2", "r3", "r6"]
    return [
        tag_event("user", {}, "user_message", FIXTURE["messages"][0]),
        tag_event(
            "assistant",
            {"turn_value": "question", "choice_question": card},
            "assistant_response",
            "I can't make this yet.",
        ),
        tag_event(
            "user",
            {
                "choice_selection": {
                    "question_id": card["question_id"],
                    "option_key": CHANGE_REQUEST_KEY,
                }
            },
            "user_message",
            "Change my request",
        ),
        tag_event(
            "assistant",
            {"turn_value": "question"},
            "assistant_response",
            "Nothing is made yet. Tell me what to do differently.",
        ),
        *after,
    ]


def test_only_the_message_right_after_change_my_request_is_marked(prod_profile) -> None:
    from app.kria.planner import _change_request_context  # noqa: PLC0415
    from app.services.choice_questions import tag_event  # noqa: PLC0415

    new_request = tag_event("user", {}, "user_message", FIXTURE["messages"][2])
    assert _change_request_context(_change_request_thread(new_request))
    # The tap turn itself, a later message, and a thread with no such tap are not.
    assert not _change_request_context(_change_request_thread())
    later = tag_event("user", {}, "user_message", "Mach es ohne die anderen Fotos")
    reply = tag_event("assistant", {"turn_value": "act"}, "assistant_response", "OK.")
    assert not _change_request_context(_change_request_thread(new_request, reply, later))
    assert not _change_request_context([new_request])


def test_the_change_request_marker_reaches_both_brief_prompts(_rollout) -> None:
    from app.agents.brief_extractor import (  # noqa: PLC0415
        BriefExtractionInput,
        BriefExtractorAgent,
    )
    from app.agents.main_creator import (  # noqa: PLC0415
        CHANGED_REQUEST_SECTION,
        MainCreatorAgent,
        MainCreatorInput,
    )

    plain = BriefExtractionInput(user_message=FIXTURE["messages"][2])
    marked = plain.model_copy(update={"changes_request": True})
    extractor = BriefExtractorAgent(model_client=None)
    assert CHANGED_REQUEST_SECTION not in extractor.render_prompt(plain)
    assert extractor.render_prompt(marked).endswith(CHANGED_REQUEST_SECTION + "\n")
    # Unmarked inputs dump exactly as before (checkpoints and recorded traces unchanged).
    assert "changes_request" not in plain.model_dump()

    creator = MainCreatorInput(
        user_message=FIXTURE["messages"][2],
        capability_manifest=_de1_visuals_manifest(),
        brief_enabled=True,
    )
    agent = MainCreatorAgent(model_client=None)
    assert CHANGED_REQUEST_SECTION not in agent.render_prompt(creator)
    assert "changes_request" not in creator.model_dump()
    assert CHANGED_REQUEST_SECTION in agent.render_prompt(
        creator.model_copy(update={"changes_request": True})
    )


def test_the_change_request_section_keeps_restrictions() -> None:
    """Review fix: the new message replaces CONTENT asks; restrictions it does not repeat (no
    title, mute the rest, leave a photo out) stay. Language-neutral: no word lists."""
    from app.agents.main_creator import CHANGED_REQUEST_SECTION  # noqa: PLC0415

    text = " ".join(CHANGED_REQUEST_SECTION.split())
    assert "Restrictions the creator set stay in force" in text
    for kept in ("no title", "no captions", "muted", "leave out"):
        assert kept in text
    assert "no longer includes" not in text  # the over-removing rule is gone
    for word in ("auch", '"de"', "also", "too"):
        assert word not in text


def test_the_recorded_change_request_answer_keeps_the_restrictions() -> None:
    """Replay only: the hand-authored golden answer for de1 message 3 after "Change my
    request", re-parsed by the REAL extractor parse (bound to the shown brief) and applied.
    It proves the shape, not that the live model writes it."""
    from app.agents.brief_extractor import (  # noqa: PLC0415
        BriefExtractionInput,
        BriefExtractorAgent,
    )
    from app.kria.brief import apply_updates  # noqa: PLC0415

    golden = json.loads(
        (
            Path(__file__).parents[1] / "fixtures/agent_evals/brief_extractor/"
            "kri1360_change_request_keeps_restrictions_de.json"
        ).read_text("utf-8")
    )
    agent_input = BriefExtractionInput(**golden["input"])
    assert agent_input.changes_request
    output = BriefExtractorAgent(model_client=None).parse(golden["raw_text"], agent_input)
    after = apply_updates(agent_input.current_brief, output.brief_updates, source_turn_id=None)

    assert [req.id for req in after.live()] == golden["meta"]["live_after_change"]
    assert {"r6", "r7", "r8"} <= {req.id for req in after.live()}


def test_the_change_request_kill_switch_leaves_the_input_unmarked(
    prod_profile, monkeypatch: pytest.MonkeyPatch
) -> None:
    from app.kria.planner import _change_request_context  # noqa: PLC0415
    from app.services.choice_questions import tag_event  # noqa: PLC0415

    monkeypatch.setattr(settings, "change_request_fresh_brief_enabled", False)
    new_request = tag_event("user", {}, "user_message", FIXTURE["messages"][2])
    assert not _change_request_context(_change_request_thread(new_request))


# -- KRI-1356: "Null zu null" never met "0 zu 0" --------------------------------------------------
#
# Drives the real `_run_phone_narrated_job` with de1's voiceover words and the two Visuals beats
# the Main Creator wrote, through the real grounding and lane compile; only storage, the pool,
# asset binding and the resolver MODEL are faked (the resolver's strict `parse` runs on a
# recorded-style answer). The review is the real finished-video review.

DE1_WORDS = [(w["text"], w["start_s"], w["end_s"]) for w in FIXTURE["voiceover"]["words"]]
NULL_AT_S = next(w["start_s"] for w in FIXTURE["voiceover"]["words"] if w["i"] == 32)
DE1_BEATS = [
    {
        "beat_id": "aufstellung",
        "trigger": "Aufstellung",
        "visual_id": "asset-de1-lineup",
        "visual_role": "photo",
        "display_mode": "fullscreen",
    },
    {
        "beat_id": "null",
        "trigger": "Null zu null",
        "visual_id": "asset-de1-score",
        "visual_role": "photo",
        "display_mode": "fullscreen",
    },
]
# The answer a model gives for the de1 words (ids from the same rows the worker builds).
RESOLVED_NULL = (
    '{"resolutions": [{"cue_id": "c1", "start_word_id": "w000032", "end_word_id": "w000034"}]}'  # noqa: E501
)


def _resolver(monkeypatch: pytest.MonkeyPatch, answer: str | Exception | None) -> list:
    """Fake the resolver MODEL only: its strict parse runs on ``answer``."""
    from app.agents import _model_client  # noqa: PLC0415
    from app.agents.spoken_cue_resolver import SpokenCueResolverAgent  # noqa: PLC0415

    calls: list = []

    def _run(self, input, *, ctx=None):  # noqa: A002, ANN001, ARG001
        calls.append(input)
        if isinstance(answer, Exception):
            raise answer
        return self.parse(answer, input)

    monkeypatch.setattr(SpokenCueResolverAgent, "run", _run)
    monkeypatch.setattr(_model_client, "default_client", lambda: None)
    return calls


def _de1_render(monkeypatch: pytest.MonkeyPatch, *, beats=DE1_BEATS, requirements=("r4", "r5")):
    import app.services.phone_reaction_grounding as grounding  # noqa: PLC0415
    import app.services.phone_visuals as phone_visuals  # noqa: PLC0415
    import app.tasks.generative_build as gb  # noqa: PLC0415
    from app.kria.brief_binding import BriefBinding  # noqa: PLC0415
    from app.services.device_render import device_status  # noqa: PLC0415
    from tests.tasks.test_phone_subtitled_narrated_dispatch import (  # noqa: PLC0415
        _enable_beats,
        _make_fake_bind,
        _pool_asset,
        _setup_narrated,
        _words,
    )

    job, snapshot, _session, _bindings = _setup_narrated(
        monkeypatch,
        edit_format="narrated_planned",
        n_clips=2,
        spoken_words=_words(*DE1_WORDS),
        voiceover_s=FIXTURE["voiceover"]["duration_s"],
        clip_duration_s=30.0,
    )
    _enable_beats(monkeypatch)
    monkeypatch.setattr(gb.settings, "phone_voiceover_editor_lanes_enabled", True)
    monkeypatch.setattr(gb.settings, "phone_voiceover_reaction_beats_enabled", True)
    monkeypatch.setattr(gb.settings, "phone_subtitled_fullscreen_overlays_enabled", True)
    monkeypatch.setattr(
        grounding,
        "_load_ready_pool_assets",
        lambda *a, **k: [
            _pool_asset("de1-lineup", "IMG_1038.HEIC"),
            _pool_asset("de1-score", "IMG_1040.HEIC"),
        ],
    )
    monkeypatch.setattr(grounding, "_load_sfx_entries", lambda *a, **k: [])
    monkeypatch.setattr(phone_visuals, "bind_phone_visual_assets", _make_fake_bind([]))
    job.all_candidates["creator_strategy"] = {
        "edit_format": "narrated_planned",
        "audio_strategy": "voiceover",
        "reaction_beats": [dict(b) for b in beats],
    }
    binding = BriefBinding.create(
        uuid.uuid4(), CreativeBrief(version=2, requirements=[_req(r) for r in requirements])
    )
    snapshot["creator_brief_binding"] = binding.model_dump(mode="json")
    job.assembly_plan["creator_brief_binding"] = snapshot["creator_brief_binding"]
    gb._run_phone_narrated_job(str(job.id), snapshot, job.all_candidates, ownership_epoch=3)
    assert job.status == "awaiting_device"
    [variant] = job.assembly_plan["variants"]
    return job, variant, binding, device_status(job, "narrated").request.recipe


def _review(job, variant, binding) -> dict:
    from types import SimpleNamespace  # noqa: PLC0415

    from app.tasks import kria_runtime  # noqa: PLC0415

    thread = SimpleNamespace(id=uuid.UUID(binding.thread_id), creator_id=uuid.uuid4())
    _text, payload = kria_runtime._approved_generation_review(
        None, thread, job, variant, SimpleNamespace(result={}), "Ready."
    )
    return {row["requirement_id"]: row for row in payload}


def _overlay_starts(recipe) -> list[float]:
    """Start of every picture the compiled recipe lays over the clips (corner or full screen)."""
    return sorted(
        round(clip.timeline_start, 2)
        for track in recipe.tracks
        if str(track.id) in ("subtitled-overlays", "subtitled-fullscreen")
        for clip in track.clips
    )


def test_resolved_cue_places_the_score_photo_on_0_zu_0(monkeypatch: pytest.MonkeyPatch) -> None:
    calls = _resolver(monkeypatch, RESOLVED_NULL)
    job, variant, binding, recipe = _de1_render(monkeypatch)

    receipt = variant["phone_beat_receipt"]
    assert receipt["unplaced"] == []
    null = next(p for p in receipt["placed"] if p["beat_id"] == "null")
    assert null["trigger"] == "Null zu null"  # the creator's words, never the heard ones
    assert null["heard_as"] == "0 zu 0"
    assert null["at_s"] == pytest.approx(NULL_AT_S, abs=0.01)
    assert round(NULL_AT_S, 2) in _overlay_starts(recipe)
    # One call for the one cue the matcher never heard; "Aufstellung" is heard exactly.
    [call] = calls
    assert [c.trigger for c in call.cues] == ["Null zu null"]
    assert call.words[32]["word_id"] == "w000032"
    rows = _review(job, variant, binding)
    assert rows["r5"]["status"] == "met", rows["r5"]
    assert rows["r4"]["status"] == "met", rows["r4"]


def test_a_resolved_named_cue_keeps_its_word_start_with_phrase_timing(monkeypatch) -> None:  # noqa: ANN001
    """Emir's #2266 re-review: with BEAT_PHRASE_TIMING_ENABLED on, the creator NAMED
    "Null zu null" (r5's trigger), so its photo keeps its exact word start even after the
    resolver rewrites the cue to the transcript's "0 zu 0" -- never the phrase's earlier start.

    Failure modes covered: the alias drops the word-cued exemption (photo moves to 13.18 s);
    the exemption leaks to an alias of a cue the creator never named (that beat must still
    follow its phrase); the flag-off path changes (it must stay byte-identical)."""
    _resolver(monkeypatch, RESOLVED_NULL)
    monkeypatch.setattr(settings, "beat_phrase_timing_enabled", True)
    _job, variant, _binding, recipe = _de1_render(monkeypatch)

    null = next(p for p in variant["phone_beat_receipt"]["placed"] if p["beat_id"] == "null")
    assert null["heard_as"] == "0 zu 0"
    assert null["at_s"] == pytest.approx(NULL_AT_S, abs=0.01)
    assert round(NULL_AT_S, 2) in _overlay_starts(recipe), _overlay_starts(recipe)


def test_only_a_named_cue_passes_its_exemption_to_the_heard_words(monkeypatch) -> None:  # noqa: ANN001
    import app.services.phone_reaction_grounding as grounding  # noqa: PLC0415
    import app.tasks.generative_build as gb  # noqa: PLC0415

    monkeypatch.setattr(settings, "beat_phrase_timing_enabled", True)
    timing = gb._beat_phrase_timing_kwargs(  # noqa: SLF001
        grounding.VOICEOVER_PHRASE_TIMING,
        [_req("r5")],
        heard_as={"Null zu null": "0 zu 0", "Abpfiff": "der Schlusspfiff"},
    )["phrase_timing"]
    assert timing.word_cued == frozenset({"null zu null", "0 zu 0"})
    assert not grounding._phrase_cued(timing, trigger="0 zu 0", after=None, slot="fullscreen")  # noqa: SLF001
    assert grounding._phrase_cued(  # noqa: SLF001
        timing, trigger="der Schlusspfiff", after=None, slot="fullscreen"
    )
    monkeypatch.setattr(settings, "beat_phrase_timing_enabled", False)
    assert (
        gb._beat_phrase_timing_kwargs(  # noqa: SLF001
            grounding.VOICEOVER_PHRASE_TIMING, [_req("r5")], heard_as={"Null zu null": "0 zu 0"}
        )
        == {}
    )


@pytest.mark.parametrize(
    "answer",
    [
        '{"resolutions": [{"cue_id": "c1", "start_word_id": null, "end_word_id": null}]}',
        RuntimeError("model timeout"),
        # Strict parse: an invented id, a reversed span, a cue nobody asked about, too long.
        '{"resolutions": [{"cue_id": "c1", "start_word_id": "w000999", "end_word_id": "w000999"}]}',
        '{"resolutions": [{"cue_id": "c1", "start_word_id": "w000034", "end_word_id": "w000032"}]}',
        '{"resolutions": [{"cue_id": "c9", "start_word_id": "w000032", "end_word_id": "w000034"}]}',
        '{"resolutions": [{"cue_id": "c1", "start_word_id": "w000020", "end_word_id": "w000034"}]}',
    ],
)
def test_an_unresolved_cue_keeps_the_honest_never_heard(monkeypatch, answer) -> None:  # noqa: ANN001
    _resolver(monkeypatch, answer)
    job, variant, binding, _recipe = _de1_render(monkeypatch)

    receipt = variant["phone_beat_receipt"]
    assert receipt["unplaced"] == FIXTURE["phone_beat_receipt"]["unplaced"]
    assert [p["beat_id"] for p in receipt["placed"]] == ["aufstellung"]
    row = _review(job, variant, binding)["r5"]
    assert row["status"] != "met"
    assert "I never heard Null zu null in your voice" in row["reason"]


def test_a_resolved_span_heard_first_elsewhere_is_rejected(monkeypatch) -> None:  # noqa: ANN001
    # The model points at the second "0" alone; the matcher would first hear "0" at w000032,
    # a different moment, so the span is not trusted.
    _resolver(
        monkeypatch,
        '{"resolutions": [{"cue_id": "c1", "start_word_id": "w000034", "end_word_id": "w000034"}]}',
    )
    _job, variant, _binding, _recipe = _de1_render(monkeypatch)
    assert variant["phone_beat_receipt"]["unplaced"] == FIXTURE["phone_beat_receipt"]["unplaced"]


def test_the_resolver_kill_switch_never_calls_the_model(monkeypatch) -> None:  # noqa: ANN001
    calls = _resolver(monkeypatch, RESOLVED_NULL)
    monkeypatch.setattr(settings, "spoken_cue_resolver_enabled", False)
    _job, off_variant, _binding, off_recipe = _de1_render(monkeypatch)
    assert calls == []
    # The old receipt, byte for byte (the recorded prod receipt, bar the label of the pool row).
    assert (
        off_variant["phone_beat_receipt"]["unplaced"] == FIXTURE["phone_beat_receipt"]["unplaced"]
    )

    # ... and identical to a render whose resolver had nothing to say.
    _resolver(
        monkeypatch,
        '{"resolutions": [{"cue_id": "c1", "start_word_id": null, "end_word_id": null}]}',
    )
    monkeypatch.setattr(settings, "spoken_cue_resolver_enabled", True)
    _job, null_variant, _binding, null_recipe = _de1_render(monkeypatch)
    assert off_variant["phone_beat_receipt"] == null_variant["phone_beat_receipt"]
    assert _overlay_starts(off_recipe) == _overlay_starts(null_recipe)


def test_an_exactly_heard_cue_never_calls_the_resolver(monkeypatch) -> None:  # noqa: ANN001
    calls = _resolver(monkeypatch, RESOLVED_NULL)
    _job, variant, _binding, _recipe = _de1_render(monkeypatch, beats=DE1_BEATS[:1])
    assert calls == []
    assert [p["trigger"] for p in variant["phone_beat_receipt"]["placed"]] == ["Aufstellung"]


def test_the_resolver_helper_is_byte_identical_without_misses() -> None:
    from app.services.phone_reaction_grounding import trigger_is_heard  # noqa: PLC0415

    words = [w | {} for w in FIXTURE["voiceover"]["words"]]
    assert trigger_is_heard(words, "Aufstellung")
    assert trigger_is_heard(words, "das Licht aus")
    assert not trigger_is_heard(words, "Null zu null")


# -- KRI-1356 D2 + KRI-1357: the draft says what the render will check -----------------------------

DRAFT_BEATS = (("Aufstellung", "asset-de1-lineup"), ("Null zu null", "asset-de1-score"))


def _draft_facts(*, phone_narrated: bool = True, **extra):
    from app.kria.brief_checks import BeatFact, PlanFacts  # noqa: PLC0415

    beats = tuple(
        BeatFact(trigger=t, visual_id=v, display_mode="fullscreen") for t, v in DRAFT_BEATS
    )
    return PlanFacts(
        reaction_beats=beats,
        reaction_beats_available=True,
        phone_narrated=phone_narrated,
        **extra,
    )


def _check(rid: str, facts):
    from app.kria.brief_checks import check_requirement  # noqa: PLC0415

    return check_requirement(_req(rid), facts)


def _approvable(rid: str, receipt) -> bool:
    from app.kria.approval_readiness import _promised_at_render  # noqa: PLC0415

    return _promised_at_render(_req(rid), receipt, _draft_facts())


def test_a_voiceover_draft_promises_the_pop_in_instead_of_done() -> None:
    from app.kria.brief_checks import _NARRATED_BEAT_PLAN  # noqa: PLC0415

    for rid in ("r4", "r5"):
        receipt = _check(rid, _draft_facts())
        assert (receipt.status, receipt.reason) == ("partial", _NARRATED_BEAT_PLAN), rid
        assert _approvable(rid, receipt)


def test_the_beat_promise_kill_switch_says_met_again(monkeypatch) -> None:  # noqa: ANN001
    monkeypatch.setattr(settings, "narrated_beat_render_promise_enabled", False)
    assert _check("r5", _draft_facts()).status == "met"


def test_footage_on_a_spoken_word_is_promised_not_no_pop_in() -> None:
    from app.kria.brief_checks import _NARRATED_FOOTAGE_CUE_PLAN  # noqa: PLC0415

    for rid in ("r2", "r3"):
        receipt = _check(rid, _draft_facts())
        assert (receipt.status, receipt.reason) == ("partial", _NARRATED_FOOTAGE_CUE_PLAN), rid
        assert "No pop-in" not in receipt.reason
        assert _approvable(rid, receipt)


def test_footage_cue_kill_switch_restores_no_pop_in(monkeypatch) -> None:  # noqa: ANN001
    monkeypatch.setattr(settings, "narrated_footage_cue_promise_enabled", False)
    assert _check("r2", _draft_facts()).reason == "No pop-in for 1. FC Nürnberg."
    assert _check("r3", _draft_facts()).reason == "No pop-in for das Licht aus."


def test_talking_and_montage_drafts_are_unchanged() -> None:
    # Not a phone Voiceover draft (Talking / montage / cloud): the old verdicts, word for word.
    facts = _draft_facts(phone_narrated=False)
    assert _check("r5", facts).status == "met"
    assert _check("r2", facts).reason == "No pop-in for 1. FC Nürnberg."
    talking = _draft_facts(phone_narrated=False, phone_talking=True)
    assert _check("r5", talking).status == "met"
    assert _check("r3", talking).reason == "No pop-in for das Licht aus."


def test_a_voiceover_draft_with_no_pop_ins_at_all_promises_the_check() -> None:
    from app.kria.brief_checks import _NARRATED_FOOTAGE_CUE_PLAN, PlanFacts  # noqa: PLC0415

    facts = PlanFacts(reaction_beats=(), reaction_beats_available=True, phone_narrated=True)
    receipt = _check("r2", facts)
    assert (receipt.status, receipt.reason) == ("partial", _NARRATED_FOOTAGE_CUE_PLAN)


def test_turkish_twins_are_approvable() -> None:
    from app.kria.brief_checks import (  # noqa: PLC0415
        _NARRATED_BEAT_PLAN,
        _NARRATED_FOOTAGE_CUE_PLAN,
        _REASON_TR,
    )
    from app.kria.contracts import RequirementReceipt  # noqa: PLC0415

    for promise in (_NARRATED_BEAT_PLAN, _NARRATED_FOOTAGE_CUE_PLAN):
        receipt = RequirementReceipt(requirement_id="r5", status="partial").with_reason(
            _REASON_TR[promise]
        )
        assert _approvable("r5", receipt), promise


def test_the_de1_draft_is_approvable_on_readiness(phone_voiceover, monkeypatch) -> None:  # noqa: ANN001
    """The prod hold, end to end through readiness: the draft strategy carries the two beats
    the Main Creator wrote, on the manifest that renders them (Visuals pop-ins on the voice)."""
    from app.kria.approval_readiness import APPROVABLE, assess_readiness  # noqa: PLC0415
    from app.services import creator_capabilities as capabilities  # noqa: PLC0415
    from app.services.phone_rollout import (  # noqa: PLC0415
        PHONE_SUBTITLED_OVERLAY_FEATURES,
        PHONE_SUBTITLED_SFX_FEATURES,
        PHONE_VOICEOVER_EDITOR_FEATURES,
    )

    # The Voiceover pop-in lane as prod rendered de1 (KRI-519 + N2 full screen).
    for flag in (
        "phone_voiceover_editor_lanes_enabled",
        "phone_subtitled_media_lanes_enabled",
        "phone_subtitled_editor_lanes_enabled",
        "phone_subtitled_reaction_beats_enabled",
        "phone_voiceover_reaction_beats_enabled",
        "phone_subtitled_fullscreen_overlays_enabled",
        "media_overlays_enabled",
        "sound_effects_enabled",
    ):
        monkeypatch.setattr(settings, flag, True)
    monkeypatch.setattr(
        settings,
        "phone_render_verified_features",
        sorted(
            set(settings.phone_render_verified_features)
            | set(PHONE_SUBTITLED_OVERLAY_FEATURES)
            | set(PHONE_SUBTITLED_SFX_FEATURES)
            | set(PHONE_VOICEOVER_EDITOR_FEATURES)
            | {"narrationAudio"}
        ),
    )
    strategy = _strategy() | {"reaction_beats": [dict(b) for b in DE1_BEATS]}
    clips = FIXTURE["clips"]
    manifest = capabilities.resolve_creator_manifest(
        item_id="item-de1",
        edit_format=strategy["edit_format"],
        has_voiceover=True,
        narration={
            "gcs_path": "users/de1/voice.m4a",
            "generation": "1",
            "duration_s": FIXTURE["voiceover"]["duration_s"],
        },
        media=[{k: c[k] for k in ("media_id", "kind", "duration_s")} for c in clips]
        + [
            {"media_id": f"asset-de1-{name}", "kind": "image", "duration_s": None}
            for name in ("lineup", "score")
        ],
        phone_source_media_ids=[c["media_id"] for c in clips],
        phone_rendering_allowed=True,
        runs_guided_voiceover=False,
    )
    assert manifest.capabilities["reaction_beats"].available
    brief = CreativeBrief(version=1, requirements=[_req(r) for r in ("r2", "r3", "r4", "r5")])

    def _rows() -> dict:
        report = assess_readiness(
            brief, strategy, media=_media(), manifest=manifest, creator_id=CREATOR
        )
        return {row["requirement_id"]: row for row in report["requirements"]}

    rows = _rows()
    for rid in ("r2", "r3", "r4", "r5"):
        assert rows[rid]["disposition"] in APPROVABLE, rows[rid]
        assert "No pop-in" not in (rows[rid]["reason"] or ""), rows[rid]
    # With both switches off: the prod hold ("No pop-in" for the two footage cues).
    monkeypatch.setattr(settings, "narrated_footage_cue_promise_enabled", False)
    monkeypatch.setattr(settings, "narrated_beat_render_promise_enabled", False)
    old = _rows()
    for rid in ("r2", "r3"):
        assert old[rid]["disposition"] not in APPROVABLE, old[rid]
        assert "No pop-in" in old[rid]["reason"], old[rid]


def test_after_render_footage_cues_name_the_clip_under_the_words(monkeypatch) -> None:  # noqa: ANN001
    _resolver(monkeypatch, RESOLVED_NULL)
    job, variant, binding, _recipe = _de1_render(monkeypatch, requirements=("r2", "r3", "r5"))
    rows = _review(job, variant, binding)
    assert rows["r5"]["status"] == "met"
    # "das Licht aus" is said under one clip: the review names it and its window, partly (it
    # cannot know the clip is the one the creator meant).
    assert rows["r3"]["status"] == "partial", rows["r3"]
    assert rows["r3"]["reason"].startswith("When you say das Licht aus, clip ")
    # "1. FC Nürnberg" is said "FC Nürnberg": no step holds the creator's exact words.
    assert rows["r2"]["reason"] == (
        "I couldn't tell which clip is on screen when you say 1. FC Nürnberg."
    )
    for rid in ("r2", "r3"):
        assert "No pop-in" not in rows[rid]["reason"]


def test_a_cue_dropped_for_lack_of_room_keeps_the_room_sentence(monkeypatch) -> None:  # noqa: ANN001
    """Review probe: a pop-in the render placed none of for lack of room is a Visuals ask that
    was heard, never a footage cue; its honest "no room" sentence stays (flag on or off)."""
    from app.kria.brief_checks import BeatFact, NarratedStepFact, PlanFacts  # noqa: PLC0415

    req = BriefRequirement(
        id="r1",
        kind="style",
        scope="global",
        description='show the logo when I say "Bayer" and the trophy when I say "Pokal"',
        facts={"triggers": ["Bayer", "Pokal"]},
    )
    facts = PlanFacts(
        rendered_variant=True,
        reaction_beats_available=True,
        reaction_beats=(BeatFact(trigger="Bayer", visual_id="v1", at_s=1.0),),
        beat_room_drops=(("Pokal", "no_room"),),
        narrated_cue_steps=(
            NarratedStepFact(media_id="a", start_s=0.0, end_s=3.0, text="wir sind Bayer"),
            NarratedStepFact(media_id="b", start_s=3.0, end_s=6.0, text="der Pokal ist da"),
        ),
    )
    from app.kria.brief_checks import check_requirement  # noqa: PLC0415

    monkeypatch.setattr(settings, "narrated_footage_cue_promise_enabled", False)
    old = check_requirement(req, facts)
    monkeypatch.setattr(settings, "narrated_footage_cue_promise_enabled", True)
    new = check_requirement(req, facts)
    assert "no room on screen for Pokal" in old.reason, old.reason
    assert (new.status, new.reason) == (old.status, old.reason)
    assert "When you say" not in new.reason
