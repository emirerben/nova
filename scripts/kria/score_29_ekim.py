#!/usr/bin/env python3
"""Scorecard for the 29 Ekim conversation (prod thread 729b3686, 6 creator messages).

Replays the recorded conversation through the REAL router, brief and coverage code and reports
one JSON verdict per definition-of-done check. Offline by default: a socket guard makes any
network attempt fail the check that tried it, and no model is called. Model outputs come from
``tests/fixtures/kria_replay/29-ekim/fixture.json`` (see ``source.model_outputs`` there for what
prod kept and what is reconstructed).

    cd src/apps/api && .venv/bin/python ../../../scripts/kria/score_29_ekim.py [--out report.json]
    make kria-replay FIXTURE=29-ekim

Checks (all must hold; a lane closes only on its own checks):

1. T1 clip order is a seeded permutation of all 27 clips, not attachment order, stable on rerun.
2. No requirement is filed as captions:* or voiceover; the quoted sentence is stored verbatim as
   on-screen text.
3. phone_text_style places the title for every font the typography agent may pick, with no
   CreatorRenderContractError.
4. "Split it into pieces, word by word" becomes N one-word text elements with ordered windows
   covering the title span, built with editor ops, with zero nova.creator.main calls.
5a. Every follow-up turn (everything after the creation turn T1) has a replayed wall time under
   60 s. Owned jointly by lane C (bounded failure) and lane E (no Main Creator call on an
   edit-shaped follow-up); lane C alone cannot turn it green.
5b. The creation turn and every failure/degrade path finish inside the 180 s turn budget: T1's
   wall time (recorded, so measured) and the lane C fault-injection scenarios (rung order,
   timeouts and budget are production code; lighter-rung latencies are ASSUMED). The old
   per-turn numbers stay in the report (``turns``), including the ones lane C cannot shorten.
   A successful high-thinking creation call (T1: 52 s) is not shortened by a retry ladder, so
   a 60 s bar on the creation turn is not a lane C gate.
6. Live shape, approval gate (prod thread 64bb9853, the same first message in a new thread after
   every lane shipped): the live Main Creator, clip-intent planner and resolver answers to that
   exact message (``29-ekim/live_new_thread_t1.json``; prod's brief, proven by its digest) go
   through the real parsers, clip-intent planning, the planner's order fields, approval
   readiness and the choice gate. The turn must be a draft, not a question: random order is an
   executable seeded shuffle, the text sequence is planned for the render, nothing is
   needs_choice or evidence_missing, and the same holds when the planner omits ``order_by``.
7. After approval, no model: the approved job through the real phone worker
   (``_run_phone_unified_montage_job``) hands the creation text composer exactly one text bar,
   the opening title, carrying the text_sequence's sentence (``title_source_exists`` True).
   Before the fix it got none, and every such render failed as ``creation_text_failed``.
8. After approval, replay: every recorded live composer answer
   (``29-ekim/live_post_approval.json``, pro and flash fallback, Turkish and English) through the
   worker, the phone text style pass and the device pin (``verify_phone_recipe``): the seeded
   shuffle of all 28 clips, one one-word piece per word in ordered contiguous windows of at least
   0.25 s that rejoin to exactly the sentence, no static copy of it, every piece styled, and r3's
   receipt met. Captured with ``scripts/kria/capture_29_ekim_post_approval.py`` (paid ledger).

Verdicts: VERIFIED, NOT VERIFIED, INCONCLUSIVE (the check could not run).
"""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import copy
import json
import os
import socket
import sys
import traceback
import unicodedata
import uuid
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, patch

REPO = Path(__file__).resolve().parents[2]
API = REPO / "src" / "apps" / "api"
FIXTURE = API / "tests" / "fixtures" / "kria_replay" / "29-ekim" / "fixture.json"
LIVE_LEDGER_ENV = "NOVA_29EKIM_LIVE_LEDGER_APPROVED"
TURN_BUDGET_MS = 60_000
# No copilot call exists in the recording (it never ran on this thread). Typical flash-class
# structured edit call; reported as `assumed` wherever it is used.
ASSUMED_COPILOT_MS = 12_000
ASSUMED_EXTRACTOR_MS = 8_000

_SAFE_ENV = {
    "STORAGE_BUCKET": "offline-scorecard",
    "STORAGE_PROVIDER": "gcs",
    "DATABASE_URL": "postgresql://localhost:5432/offline",
    "REDIS_URL": "redis://localhost:6379",
    "OPENAI_API_KEY": "offline",
    "GEMINI_API_KEY": "offline-scorecard",
    "TOKEN_ENCRYPTION_KEY": "offline",
    "INTERNAL_API_KEY": "offline",
    "ALLOWED_ORIGINS": '["http://localhost:3000"]',
}


class OfflineViolation(RuntimeError):
    """A scorecard check tried to reach the network."""


_NET_ATTEMPTS: list[str] = []


def _bootstrap() -> None:
    for key, value in _SAFE_ENV.items():
        os.environ[key] = value  # always overwrite: the scorecard never uses real credentials
    if str(API) not in sys.path:
        sys.path.insert(0, str(API))

    def _blocked(*args: Any, **kwargs: Any):  # noqa: ANN202
        _NET_ATTEMPTS.append(repr(args[:2]))
        raise OfflineViolation("network access is blocked in the offline scorecard")

    socket.socket.connect = _blocked  # type: ignore[method-assign]
    socket.create_connection = _blocked  # type: ignore[assignment]


# ----------------------------------------------------------------------------- fixture


def load_fixture() -> dict[str, Any]:
    return json.loads(FIXTURE.read_text(encoding="utf-8"))


def _nfc(text: str) -> str:
    return unicodedata.normalize("NFC", text).strip()


def message(fx: dict, turn: str) -> dict:
    return next(m for m in fx["messages"] if m["turn"] == turn)


def clip_rows(fx: dict) -> list[dict]:
    """The item's clip assignments as the approved media snapshot carries them."""
    return [
        {
            "media_id": f"analysis-proxy-{clip['id']}",
            "gcs_path": (
                f"users/redacted/creation-threads/redacted/analysis-proxy-ios-{clip['id']}.mp4"
            ),
            "kind": clip["kind"],
            "duration_s": clip["duration_s"],
            "generation": "1",
            "has_analysis": True,
        }
        for clip in fx["clips"]
    ]


def _media(fx: dict) -> dict:
    rows = clip_rows(fx)
    return {"clip_paths": [r["gcs_path"] for r in rows], "clip_assignments": rows}


def _with_media_ids(fx: dict, value: Any) -> Any:
    """Fixture ids are `clip-NN`; the pipeline's ids are `analysis-proxy-clip-NN`."""
    text = json.dumps(value, ensure_ascii=False)
    for clip in fx["clips"]:
        text = text.replace(f'"{clip["id"]}"', f'"analysis-proxy-{clip["id"]}"')
    return json.loads(text)


def strategy(fx: dict) -> dict:
    """The recorded T4 strategy (the only Main Creator strategy prod kept), as a plain dict."""
    from app.agents._schemas.creator_agent import CreativeStrategy

    raw = _with_media_ids(fx, fx["strategy_t4"])
    return CreativeStrategy.model_validate(raw).model_dump(mode="json", exclude_none=True)


def ledger_at(fx: dict, turn: str):  # noqa: ANN201
    """The brief ledger the thread held while ``turn`` ran.

    Prod persisted only the final ledger (v3). r2 was withdrawn by T2 and r1 by the T4 "make it
    without these" answer, so earlier versions are the final rows with those statuses restored.
    """
    from app.kria.brief import CreativeBrief

    withdrawn = {
        "T1": set(),
        "T2": set(),
        "T3": {"r2"},
        "T4": {"r2"},
        "T5": {"r1", "r2"},
        "T6": {"r1", "r2"},
    }[turn]
    version = {"T1": 1, "T2": 1, "T3": 2, "T4": 2, "T5": 3, "T6": 3}[turn]
    rows = []
    for row in copy.deepcopy(fx["brief_ledger_v3"]):
        row["status"] = "superseded" if row["id"] in withdrawn else "open"
        rows.append(row)
    return CreativeBrief.model_validate({"version": version, "requirements": rows})


def unified_clips(fx: dict):  # noqa: ANN201
    from app.pipeline.unified_montage import UnifiedClip

    return [
        UnifiedClip(
            media_id=f"analysis-proxy-{c['id']}",
            proxy_path=f"users/redacted/analysis-proxy-ios-{c['id']}.mp4",
            generation="1",
            duration_s=c["duration_s"],
            width=568,
            height=320,
        )
        for c in fx["clips"]
    ]


# ----------------------------------------------------------------------------- check 1


def check_order(fx: dict) -> dict:
    from app.kria.brief import CurrentPlanShape, route_requirements
    from app.kria.brief_checks import build_receipts, plan_facts_from_unified_montage
    from app.pipeline.unified_montage import brief_view, plan_unified_montage
    from app.services.choice_questions import (
        CONFLICT_ORDER_BASIS,
        ChoiceCapability,
        collect_conflicts,
    )

    brief = ledger_at(fx, "T1")
    strat = strategy(fx)
    order_req = next(r for r in brief.live() if r.kind == "order")
    attachment = [f"analysis-proxy-{c['id']}" for c in fx["clips"]]

    route = route_requirements([order_req], CurrentPlanShape(has_render=False))
    conflicts = collect_conflicts(
        strat, brief, _media(fx), ChoiceCapability(creator_id=uuid.uuid4())
    )
    order_questions = [c.kind for c in conflicts if c.kind == CONFLICT_ORDER_BASIS]

    def compile_once():  # noqa: ANN202
        return plan_unified_montage(
            unified_clips(fx),
            brief_view(brief),
            strategy={
                "opening_title": strat.get("opening_title"),
                "edit_format": "montage",
            },
        )

    first, second = compile_once(), compile_once()
    order = list(first.clip_ids)
    is_permutation = sorted(order) == sorted(attachment) and len(order) == len(attachment) == 27
    differs = order != attachment
    stable = order == list(second.clip_ids)
    receipts = build_receipts(
        brief.live(), plan_facts_from_unified_montage(first.record()), strict_order=True
    )
    r1 = next(r for r in receipts if r.requirement_id == order_req.id)
    passed = not order_questions and is_permutation and differs and stable and r1.status == "met"
    why = []
    if order_questions:
        why.append(
            f"the choice gate asks the creator a {CONFLICT_ORDER_BASIS} question instead of "
            "making a draft: the key-less order requirement is an ordering rule it 'cannot "
            "verify' (services/choice_questions._order_basis); shuffle is not a known basis"
        )
    if not differs:
        why.append(
            f"the montage planner compiled attachment order (ordering_basis="
            f"{first.ordering_basis!r}): no random order exists in plan_unified_montage or in "
            "CreativeStrategy.ordering_choice (group_first | chronological only)"
        )
    if r1.status != "met":
        why.append(f"the order receipt for r1 is {r1.status}: {r1.reason!r}")
    return _result(
        1,
        "T1 clip order is a seeded permutation of all 27 clips",
        passed,
        why,
        {
            "route": route,
            "order_questions_asked": order_questions,
            "ordering_basis": first.ordering_basis,
            "clip_count": len(order),
            "is_permutation": is_permutation,
            "differs_from_attachment": differs,
            "stable_on_rerun": stable,
            "r1_receipt": {"status": r1.status, "reason": r1.reason},
        },
    )


# ----------------------------------------------------------------------------- check 2


EVAL_FIXTURES = API / "tests" / "fixtures" / "agent_evals" / "main_creator"
T1_CASE = "kri1038_t1_29ekim_tr_quote_legacy"
T2_CASE = "kri1038_t2_29ekim_followup_legacy"


def replay_interpretation(fx: dict) -> dict[str, Any]:
    """Run the recorded old-prompt replies for T1, T2 and T4 through the REAL Main Creator
    parse (the replay eval runner) and ``apply_updates``, and return the ledger after each turn.

    Prod's own ledger (``ledger_at``) is the OLD code's reading of these messages, so check 2
    cannot judge it. The recorded model output is the fixed input; what the server does with
    it is what the lane changes."""
    sys.path.insert(0, str(API))
    from app.kria.brief import (
        BriefUpdate,
        CreativeBrief,
        apply_updates,
        parse_brief_updates,
    )
    from tests.evals.runners.eval_runner import load_fixture, run_eval

    brief = CreativeBrief()
    ledgers: dict[str, Any] = {}
    for turn, case in (("T1", T1_CASE), ("T2", T2_CASE)):
        with contextlib.redirect_stdout(sys.stderr):  # the agent runtime logs to stdout
            result = run_eval(load_fixture(EVAL_FIXTURES / f"{case}.json"))
        updates = parse_brief_updates((result.output or {}).get("brief_updates"))
        brief = apply_updates(brief, updates, source_turn_id=message(fx, turn)["turn_id"])
        ledgers[turn] = brief
    ledgers["T3"] = brief
    r3 = next(r for r in fx["brief_ledger_v3"] if r["id"] == "r3")
    brief = apply_updates(
        brief,
        [
            BriefUpdate(
                kind="text",
                scope=r3["scope"],
                literal=r3["literal"],
                description=r3["description"],
            )
        ],
        source_turn_id=message(fx, "T4")["turn_id"],
    )
    # The creator answered "make it without these": the order ask is withdrawn, as in prod.
    order_ids = [r.id for r in brief.live() if r.kind == "order"]
    if order_ids:
        brief = apply_updates(
            brief,
            [
                BriefUpdate(
                    operation="remove",
                    target_requirement_id=i,
                    expected_version=brief.version,
                )
                for i in order_ids
            ],
            source_turn_id=message(fx, "T4")["turn_id"],
        )
    for turn in ("T4", "T5", "T6"):
        ledgers[turn] = brief
    return ledgers


def run_golden_cases() -> list[dict]:
    """Every ``text_vs_speech`` golden eval fixture (the 29 Ekim T1/T2 messages, paraphrases in
    Turkish and English, and decoys where captions IS the right reading) through the replay
    eval runner, checked against the expectation the fixture carries."""
    sys.path.insert(0, str(API))
    from tests.evals.runners.eval_runner import load_fixture, run_eval

    rows = []
    for path in sorted(EVAL_FIXTURES.glob("*kri1038_*.json")):
        fixture = load_fixture(path)
        want = fixture.meta["text_vs_speech"]
        with contextlib.redirect_stdout(sys.stderr):  # the agent runtime logs to stdout
            result = run_eval(fixture)
        updates = (result.output or {}).get("brief_updates") or []
        captions = [u for u in updates if (u.get("facts") or {}).get("captions")]
        sequences = [u for u in updates if u["kind"] == "text_sequence"]
        ok = result.passed and len(captions) == want["captions_filings"]
        if want["text_sequence"] is None:
            ok = ok and not sequences
        else:
            expected = want["text_sequence"]
            ok = (
                ok
                and len(sequences) == 1
                and sequences[0]["literal"] == expected["literal"]
                and sequences[0]["facts"] == {"unit": expected["unit"], "mode": expected["mode"]}
            )
        rows.append(
            {
                "case": path.stem,
                "ok": ok,
                "captions_filings": len(captions),
                "text_sequences": [
                    {"literal": u["literal"], "facts": u["facts"]} for u in sequences
                ],
            }
        )
    return rows


def check_text_vs_speech(fx: dict) -> dict:
    from app.agents.main_creator import MainCreatorAgent, MainCreatorInput
    from app.kria import brief_checks as bc
    from app.services.creator_render_contract import build_render_contract

    sentence = _nfc(fx["exact_texts"][0]["text"])
    ledgers = replay_interpretation(fx)
    violations: list[dict] = []
    for turn, ledger in ledgers.items():
        for req in ledger.live():
            stated = bc._caption_fact(req)
            if stated is not None or bc._wants_captions(req) or bc._wants_word_captions(req):
                violations.append(
                    {
                        "turn": turn,
                        "requirement": req.id,
                        "kind": req.kind,
                        "captions_fact": stated,
                        "description": req.description,
                    }
                )
    sequences = [r for r in ledgers["T1"].live() if r.kind == "text_sequence"]
    stored_t1 = [(_nfc(r.literal or ""), r.facts) for r in sequences]
    t1_ok = any(lit == sentence for lit, _ in stored_t1)

    # The offer to record a voiceover came from the captions requirement the model was looking
    # at (its reply text is model-authored and cannot be regenerated offline). It is reachable
    # only while the ledger entering that turn still holds a captions filing.
    offers = []
    for turn in ("T1", "T2", "T3", "T4", "T5", "T6"):
        reply = fx["replies"][message(fx, turn)["turn_id"]]
        for option in reply["choice_options"]:
            if any(word in option["label"].casefold() for word in ("record", "voiceover", "voice")):
                premise = any(
                    bc._caption_fact(r) is not None or bc._wants_word_captions(r)
                    for r in ledgers[turn].live()
                )
                offers.append(
                    {
                        "turn": turn,
                        "option": option["label"],
                        "captions_premise_remains": premise,
                    }
                )
    reachable_offers = [o for o in offers if o["captions_premise_remains"]]

    final = ledgers["T5"]
    contract = build_render_contract(
        generation_id="scorecard",
        strategy=strategy(fx),
        brief=final,
        media_snapshot=_media(fx),
    )
    stored = [_nfc(t.text) for t in (contract.exact_texts if contract else ())]
    verbatim = sentence in stored

    from app.agents.main_creator import _BRIEF_PROMPT_SECTION

    prompt = MainCreatorAgent(None).render_prompt(  # type: ignore[arg-type]
        MainCreatorInput(
            user_message="x",
            capability_manifest=_golden_manifest(),
            brief_enabled=True,
        )
    )
    prompt_ok = (
        "text_sequence" in _BRIEF_PROMPT_SECTION
        and "one word at a time" not in _BRIEF_PROMPT_SECTION.split("Captions =>")[1][:400]
        and "text_sequence" in prompt
    )

    golden = run_golden_cases()
    golden_fail = [g["case"] for g in golden if not g["ok"]]
    passed = (
        not violations
        and not reachable_offers
        and t1_ok
        and verbatim
        and prompt_ok
        and bool(golden)
        and not golden_fail
    )
    why = []
    if violations:
        why.append(
            "the word-by-word ask is filed as a captions requirement (the speech lane): "
            + "; ".join(
                f"{v['turn']} {v['requirement']} facts.captions={v['captions_fact']}"
                for v in violations[:3]
            )
        )
    if reachable_offers:
        why.append(
            "a turn offers to record the sentence as a voiceover while a captions requirement "
            "is still in the ledger: " + ", ".join(o["option"] for o in reachable_offers)
        )
    if not t1_ok:
        why.append("T1 does not store the quoted sentence verbatim as a text_sequence")
    if not verbatim:
        why.append("the quoted sentence is not in the render contract's exact_texts verbatim")
    if not prompt_ok:
        why.append("the Main Creator brief prompt does not teach text_sequence over captions")
    if golden_fail:
        why.append("golden cases failed: " + ", ".join(golden_fail))
    return _result(
        2,
        "no captions/voiceover filing; quoted sentence stored verbatim as on-screen text",
        passed,
        why,
        {
            "basis": (
                "recorded old-prompt replies replayed through the real extractor parse + "
                "apply_updates; golden eval fixtures through the replay runner; offers judged by "
                "whether their captions premise survives (model-authored reply text is not "
                "regenerated offline; live proof needs the paid ledger)"
            ),
            "captions_filings": violations,
            "voiceover_offers": offers,
            "t1_text_sequence": [{"literal": lit, "facts": facts} for lit, facts in stored_t1],
            "sentence_verbatim_in_contract": verbatim,
            "prompt_teaches_text_sequence": prompt_ok,
            "golden_cases": golden,
        },
    )


def _golden_manifest():  # noqa: ANN202
    from app.agents.main_creator import MainCreatorInput

    data = json.loads((EVAL_FIXTURES / f"{T1_CASE}.json").read_text(encoding="utf-8"))
    return MainCreatorInput.model_validate(data["input"]).capability_manifest


# ----------------------------------------------------------------------------- check 3


def agent_eligible_fonts(fx: dict) -> tuple[list[str], list[str]]:
    """The fonts PhoneTypographyAgent may return for the title, exactly as ``select_fonts`` builds
    the list (favourites that cover the text, else every covering curated font)."""
    from app.pipeline.phone_text_style import _covers, coverage_text, text_role
    from app.schemas.phone_text_style import CURATED_PHONE_FONTS, effective_text_rule

    row = fx["title_row"]
    policy = fx["phone_text_style_policy"]
    role = text_role(row)
    rule = effective_text_rule(policy.get("preferences") or {}, policy.get("requested") or {}, role)
    text = coverage_text(row)
    eligible = [
        font
        for font in dict.fromkeys([*rule.get("favorite_fonts", []), *CURATED_PHONE_FONTS])
        if font not in rule.get("excluded_fonts", []) and _covers(font, text)
    ]
    favourites = [font for font in rule.get("favorite_fonts", []) if font in eligible]
    return favourites or eligible, eligible


def check_title_fit(fx: dict) -> dict:
    from app.pipeline.phone_text_style import text_role
    from app.services.creator_render_contract import CreatorRenderContractError
    from app.services.phone_text_styling import style_rows

    row = fx["title_row"]
    policy = fx["phone_text_style_policy"]
    agent_fonts, all_covering = agent_eligible_fonts(fx)
    outcomes: dict[str, str] = {}
    errors: list[str] = []
    for font in all_covering:
        try:
            frames = [
                {
                    "start_s": row["start_s"],
                    "end_s": row["end_s"],
                    "pixels": [128] * (108 * 192),
                }
            ]
            placed = style_rows(
                [copy.deepcopy(row)],
                policy,
                selected_fonts={text_role(row): font},
                background_frames=frames,
            )
            outcomes[font] = f"placed size={placed[0]['size_px']}"
        except CreatorRenderContractError as exc:
            outcomes[font] = "CreatorRenderContractError"
            errors.append(str(exc))
    agent_failures = [f for f in agent_fonts if outcomes[f] == "CreatorRenderContractError"]
    all_failures = [f for f, o in outcomes.items() if o == "CreatorRenderContractError"]
    from app.pipeline.phone_text_style import measure_box, phone_text_measurement_scope

    height_at_floor: dict[str, float] = {}
    with phone_text_measurement_scope():
        for font in all_failures:
            box = measure_box(
                {
                    **row,
                    "font_family": font,
                    "size_px": 40,
                    "position": "custom",
                    "max_width_frac": 0.68,
                    "alignment": "left",
                    "x_frac": 0.06,
                    "y_frac": 0.5,
                }
            )
            height_at_floor[font] = round(box[3] - box[1], 3)
    passed = not all_failures
    why = []
    if all_failures:
        why.append(
            f"phone_text_style._place fails closed for {len(all_failures)} of {len(outcomes)} "
            f"fonts that draw the sentence ({', '.join(all_failures)}); the typography agent may "
            f"pick {agent_fonts} and the recorded render failed with "
            f"{fx['render_failure']['error_class']} / {fx['render_failure']['code']}: the "
            "0.18-of-canvas height cap, 4 title anchors, 40 px floor and no fallback leave a "
            f"{len(fx['title_row']['text'])}-character title nowhere to go"
        )
    return _result(
        3,
        "phone_text_style places the title for every candidate font",
        passed,
        why,
        {
            "agent_eligible_fonts": agent_fonts,
            "agent_font_failures": agent_failures,
            "outcome_by_font": outcomes,
            "block_height_at_the_40px_floor_vs_0.18_cap": height_at_floor,
            "recorded_render_failure": fx["render_failure"],
            "note": "prod's typography pick is stored as key names only; the recorded failure "
            "is consistent with a pick from the failing set",
        },
    )


# ----------------------------------------------------------------------------- planner replay


def _prod_flags() -> contextlib.ExitStack:
    from app.config import settings

    stack = contextlib.ExitStack()
    flags = {
        "phone_rendering_enabled": True,
        "phone_render_user_ids": [],
        "kria_runtime_v2_phone_enabled": True,
        "kria_runtime_v2_phone_user_ids": [],
        "guided_edit_capability_enabled": True,
        "subtitled_archetype_enabled": True,
        "clip_intents_enabled": True,
        "clip_facts_enabled": True,
        "kria_creative_brief_enabled": True,
        "kria_creative_brief_user_ids": [],
        "kria_brief_binding_enabled": True,
        "kria_brief_binding_user_ids": [],
        "kria_choice_questions_enabled": True,
        "kria_plan_authority_enabled": True,
        # The clip-analysis wait polls storage and the model; the clips here are already analysed.
        "kria_clip_understanding_enabled": False,
        "phone_render_verified_features": ["positionedText", "animatedText"],
    }
    for name, value in flags.items():
        stack.enter_context(patch.object(settings, name, value))
    return stack


class _Counting:
    """Records every model call the planner makes on one turn."""

    def __init__(self) -> None:
        self.calls: list[str] = []


def _planner_item(fx: dict, *, job_status: str | None):  # noqa: ANN202
    """In-memory PlanItem + session. Only the Job status differs between baseline and fixed."""
    from app.models import ContentPlan, CreationThread, Job, Persona, PlanItem

    item = SimpleNamespace(
        id=uuid.uuid4(),
        content_plan_id=uuid.uuid4(),
        current_job_id=uuid.uuid4() if job_status else None,
        edit_format="montage",
        clip_gcs_paths=[r["gcs_path"] for r in clip_rows(fx)],
        clip_assignments=clip_rows(fx),
    )
    creator_id = uuid.uuid4()
    plan = SimpleNamespace(id=item.content_plan_id, user_id=creator_id, persona_id=uuid.uuid4())
    persona = SimpleNamespace(user_id=creator_id)
    thread = SimpleNamespace(active_creator_agent_session_id=None)

    async def get(model, _identifier, **_kwargs):  # noqa: ANN001, ANN202
        return {
            PlanItem: item,
            ContentPlan: plan,
            Persona: persona,
            CreationThread: thread,
            Job: SimpleNamespace(status=job_status or "done"),
        }[model]

    class _Rows(list):
        def all(self):  # noqa: ANN202
            return list(self)

    db = SimpleNamespace(
        get=AsyncMock(side_effect=get),
        execute=AsyncMock(return_value=SimpleNamespace(all=lambda: [], scalars=lambda: _Rows())),
        rollback=AsyncMock(),
        commit=AsyncMock(),
    )
    return db, item, creator_id


def _copilot_plan(fx: dict):  # noqa: ANN202
    """What the copilot is expected to author for "split it, word by word": one atomic
    replace_text_sequence over the title (the op already exists; see editor_ops_v2/text.py)."""
    from app.kria.contracts import KriaTurnPlan

    title = fx["title_row"]
    return KriaTurnPlan.model_validate(
        {
            "mode": "act",
            "turn_value": "action",
            "intents": [
                {
                    "intent_id": "apply-editor-ops",
                    "tool_name": "draft.apply_editor_ops",
                    "tool_version": 1,
                    "arguments": {
                        "operations": [
                            {
                                "op": "replace_text_sequence",
                                "selector": {"ids": [title["id"]]},
                                "segments": title["text"].split(),
                            }
                        ],
                        "summary": "Show the title one word at a time",
                    },
                }
            ],
        }
    )


MIN_READABLE_PIECE_S = 0.25
_NEVER_RENDERED = frozenset({"processing_failed", "variants_failed", "matching_failed"})


async def replay_followup_turn(fx: dict, turn: str, *, job_status: str) -> dict:
    """Run the real ``plan_live_turn`` for T5 or T6 with only the model calls replaced.

    ``job_status`` is the state T4 left behind: ``processing_failed`` (the recorded render) or
    ``variants_ready`` (a title that fit). Returns which model calls the real router made.
    """
    from app.agents._runtime import ProviderOutcomeUnknownError
    from app.kria import planner
    from app.kria.brief import BriefUpdate

    db, item, creator_id = _planner_item(fx, job_status=job_status)
    counting = _Counting()
    copilot_plan = _copilot_plan(fx)
    brief = ledger_at(fx, turn)
    from app.agents._schemas.creator_agent import (
        CapabilityAvailability,
        ResolvedCreatorManifest,
    )

    manifest = ResolvedCreatorManifest(
        item_id=str(item.id),
        edit_format="montage",
        render_program="guided",
        capabilities={"dispatch_render": CapabilityAvailability(available=True)},
        context_hash="a" * 64,
        manifest_hash="b" * 64,
    )

    class FakeMainCreator:
        def __init__(self, _client) -> None:  # noqa: ANN001
            pass

        def run(self, *_a, **_k):  # noqa: ANN002, ANN003, ANN201
            counting.calls.append("nova.creator.main")
            raise ProviderOutcomeUnknownError("recorded provider deadline (179.7 s)")

    class FakeExtractor:
        def __init__(self, _client) -> None:  # noqa: ANN001
            pass

        def run(self, *_a, **_k):  # noqa: ANN002, ANN003, ANN201
            counting.calls.append("nova.brief.extractor")
            update = BriefUpdate(
                kind="text",
                scope="title",
                description="Show the title one word at a time, each word after the other",
            )
            return SimpleNamespace(brief_updates=[update], request_scope=None, clarification=None)

    async def plan_revision(_db, **_kwargs):  # noqa: ANN001, ANN202
        counting.calls.append("nova.edit.copilot")
        return copilot_plan

    snapshot = {
        "text_bars": [
            {
                "id": fx["title_row"]["id"],
                "text": fx["title_row"]["text"],
                "start_s": fx["title_row"]["start_s"],
                "end_s": fx["title_row"]["end_s"],
            }
        ],
        "allowed_op_families": ["text", "clip"],
    }
    target = SimpleNamespace(job_id=item.current_job_id, snapshot=snapshot, conversation=[])
    patches = [
        patch.object(planner, "_load_thread_events", AsyncMock(return_value=[])),
        patch.object(planner, "load_latest_brief", AsyncMock(return_value=brief)),
        patch.object(
            planner,
            "resolve_item_creator_context",
            AsyncMock(return_value=(manifest, [])),
        ),
        patch.object(planner, "creator_context", lambda *_a: ("creator", "item")),
        patch.object(
            planner,
            "_load_editor_target",
            # The real loader returns None for a job that never produced a variant.
            AsyncMock(return_value=None if job_status in _NEVER_RENDERED else target),
        ),
        patch.object(planner, "_plan_editor_revision", AsyncMock(side_effect=plan_revision)),
        patch.object(planner, "MainCreatorAgent", FakeMainCreator),
        patch.object(planner, "BriefExtractorAgent", FakeExtractor),
        patch.object(planner, "default_client", lambda: object()),
    ]
    outcome: dict[str, Any] = {"error": None, "route": None, "tools": [], "ops": []}
    with contextlib.ExitStack() as stack:
        for p in patches:
            stack.enter_context(p)
        try:
            planned = await planner.plan_live_turn(
                db,
                thread_id=uuid.uuid4(),
                item_id=item.id,
                creator_id=creator_id,
                user_message=message(fx, turn)["text"],
            )
            outcome["route"] = planned.brief_route
            for intent in planned.plan.intents:
                outcome["tools"].append(intent.tool_name)
                args = intent.arguments
                ops = args.get("operations") if isinstance(args, dict) else None
                outcome["ops"] += [o.get("op") for o in ops or []]
            outcome["mode"] = planned.plan.mode
        except ProviderOutcomeUnknownError as exc:
            outcome["error"] = f"ProviderOutcomeUnknownError: {exc}"
    outcome["model_calls"] = counting.calls
    return outcome


def _split_op_result(fx: dict) -> dict:
    """Parse then compile the word-by-word op with the real copilot parser and op compiler.

    The model's JSON is the one assumed value (prod's copilot never ran on this thread); the
    parser (selector resolution, flag-dependent timing fields) and the compiler are real code.
    """
    from app.agents._model_client import ModelClient
    from app.agents.edit_copilot import EditCopilotAgent, EditCopilotInput
    from app.services.kria_editor_ops import compile_editor_ops

    title = copy.deepcopy(fx["title_row"])
    words = title["text"].split()
    variant = {
        "variant_id": "original_text",
        "render_status": "ready",
        "render_generation_id": "scorecard",
        "resolved_archetype": "montage",
        "duration_s": float(fx["strategy_t4"].get("target_duration_s") or 60),
        "text_elements": [title],
        "caption_cues": [],
        "ai_timeline": {"slots": []},
    }
    job = SimpleNamespace(assembly_plan={"variants": [variant]}, all_candidates={"clip_paths": []})
    snapshot = {
        "editor_ops_version": 2,
        "allowed_op_families": ["text"],
        "text_bars": [title],
    }
    raw = {
        "intent": "edit",
        "ops": [
            {
                "op": "replace_text_sequence",
                "selector": {"ids": [title["id"]]},
                "segments": words,
            }
        ],
        "confidence": 0.9,
        "reply": "Showing the title one word at a time.",
        "suggestions": [],
        "needs_clarification": False,
    }
    parsed = EditCopilotAgent(ModelClient()).parse(
        json.dumps(raw),
        EditCopilotInput(
            utterance="split it, word by word",
            prior_turns=[],
            variant_snapshot=snapshot,
        ),
    )
    compiled = compile_editor_ops(job, variant, parsed.ops)
    rows = compiled.payload.text_elements
    windows = [(r["start_s"], r["end_s"]) for r in rows]
    ordered = all(a[1] <= b[0] + 1e-9 for a, b in zip(windows, windows[1:], strict=False))
    covers = (
        bool(windows)
        and abs(windows[0][0] - title["start_s"]) < 1e-6
        and windows[-1][1] >= title["end_s"] - 1e-6
    )
    lengths = [len(r["text"]) for r in rows]
    shares = [(e - s) / (windows[-1][1] - windows[0][0]) for s, e in windows] if windows else []
    longest = lengths.index(max(lengths)) if lengths else 0
    shortest = lengths.index(min(lengths)) if lengths else 0
    return {
        "elements": len(rows),
        "words": len(words),
        "one_word_each": all(len(r["text"].split()) == 1 for r in rows),
        "ordered_windows": ordered,
        "covers_title_span": covers,
        "weighted_by_length": bool(shares) and shares[longest] > shares[shortest],
        "min_window_s": min(e - s for s, e in windows) if windows else None,
        "window_end_s": windows[-1][1] if windows else None,
        "text_conserved": " ".join(r["text"] for r in rows) == title["text"],
        "op_fields": sorted(k for k in parsed.ops[0] if k in {"mode", "weight"})
        if parsed.ops
        else [],
    }


def _t4_job_status(_fx: dict, title_fits: bool) -> str:
    return "variants_ready" if title_fits else "processing_failed"


def check_followup_with_ops(fx: dict, title_fits: bool) -> tuple[dict, dict]:
    """T5 is replayed from BOTH states T4 can leave: its recorded failed render, and a ready one.

    The state the system is actually in must reach the copilot with zero Main Creator calls
    (it feeds check 5). A render that failed before producing any variant has no editable
    lanes (prod job debug: no `variants` key), so its follow-up is a re-plan by construction;
    that state is reported, not scored, and the fix for it is that T4 no longer fails.
    """
    built = _split_op_result(fx)
    states = {
        "failed_render": "processing_failed",
        "ready_render": "variants_ready",
    }
    replays = {
        name: asyncio.run(replay_followup_turn(fx, "T5", job_status=status))
        for name, status in states.items()
    }
    actual = "ready_render" if title_fits else "failed_render"
    built_ok = (
        built["elements"] == built["words"]
        and built["one_word_each"]
        and built["ordered_windows"]
        and built["covers_title_span"]
        and built["text_conserved"]
        and built["weighted_by_length"]
        and built["min_window_s"] is not None
        and built["min_window_s"] >= MIN_READABLE_PIECE_S
    )
    summary = {}
    why = []
    for name, replay in replays.items():
        creator_calls = replay["model_calls"].count("nova.creator.main")
        used_ops = (
            "draft.apply_editor_ops" in replay["tools"] and "replace_text_sequence" in replay["ops"]
        )
        summary[name] = {
            "model_calls": replay["model_calls"],
            "route": replay["route"],
            "tools": replay["tools"],
            "error": replay["error"],
            "creator_main_calls": creator_calls,
            "editor_ops_reach_the_plan": used_ops,
            "scored": name == actual,
        }
        if name != actual:
            continue
        if creator_calls:
            why.append(
                f"[{name}] T5 reaches nova.creator.main ({creator_calls} call) and no editor op "
                "is ever tried: the render left no editable draft, so the turn is a fresh plan; "
                "prod's real call hit the provider deadline at 179.8 s"
            )
        if not used_ops:
            why.append(
                f"[{name}] no editor op reaches the turn plan (replace_text_sequence is not in "
                "planner._FAST_PATH_OPS and Main Creator's strategy has no field for it)"
            )
    if not built_ok:
        why.append(f"the op does not compile to ordered one-word elements: {built}")
    passed = built_ok and all(
        v["creator_main_calls"] == 0 and v["editor_ops_reach_the_plan"]
        for v in summary.values()
        if v["scored"]
    )
    return _result(
        4,
        "word-by-word follow-up becomes N one-word text elements via editor ops, 0 Main Creator",
        passed,
        why,
        {
            "turn_state_by_t4_outcome": summary,
            "state_the_system_is_in": actual,
            "op_compile": built,
            "readability_note": "recorded title window is "
            f"{fx['title_row']['end_s'] - fx['title_row']['start_s']:.1f} s for "
            f"{built['words']} words; the compiler lengthens it to {built['window_end_s']:.1f} s "
            f"so the shortest piece holds {built['min_window_s']:.3f} s "
            f"(floor {MIN_READABLE_PIECE_S} s)",
            "copilot_output": "assumed: the prod copilot never ran on this thread, so the op the "
            "copilot authors is a fixture value; routing and the op compile are real code",
        },
    ), replays[actual]


# ----------------------------------------------------------------------------- check 5


def _latency_rows(fx: dict, followup_replays: dict[str, dict]) -> list[dict]:
    """Replayed wall time per turn: recorded agent latency for the calls the real router made
    (assumed values where the recording has none), plus the recorded non-agent overhead."""
    runs = fx["agent_runs"]
    recorded_wall = {t["turn"]: t["wall_ms"] for t in fx["timeline"]}
    rows = []
    # T3 and T4 are taps on choice cards that T2's refusal opened. When T2 now goes straight to
    # the copilot (no question), those cards are never shown, so there is no turn to time.
    t2 = followup_replays.get("T2")
    cards_gone = bool(t2) and "nova.creator.main" not in t2["model_calls"] and bool(t2["tools"])
    for turn in ("T1", "T2", "T3", "T4", "T5", "T6"):
        if cards_gone and turn in {"T3", "T4"}:
            rows.append(
                {
                    "turn": turn,
                    "recorded_wall_ms": recorded_wall[turn],
                    "replayed_ms": None,
                    "basis": "not asked: answers a choice card the fixed router never shows",
                    "under_budget": None,
                    "under_turn_budget": None,
                    "model_calls": [],
                }
            )
            continue
        turn_runs = [r for r in runs if r["turn"] == turn]
        recorded_agents = sum(r["latency_ms"] for r in turn_runs)
        overhead = max(0, recorded_wall[turn] - recorded_agents)
        basis = "recorded"
        if turn in followup_replays:
            calls = followup_replays[turn]["model_calls"]
            agents_ms = 0
            for name in calls:
                if name == "nova.creator.main":
                    agents_ms += next(
                        r["latency_ms"] for r in turn_runs if r["agent"] == "nova.creator.main"
                    )
                elif name == "nova.edit.copilot":
                    agents_ms += ASSUMED_COPILOT_MS
                    basis = "assumed copilot"
                elif name == "nova.brief.extractor":
                    agents_ms += ASSUMED_EXTRACTOR_MS
                    basis = "assumed copilot"
            replayed = agents_ms + (0 if "nova.creator.main" not in calls else overhead)
        else:
            replayed = recorded_wall[turn]
        rows.append(
            {
                "turn": turn,
                "recorded_wall_ms": recorded_wall[turn],
                "replayed_ms": replayed,
                "basis": basis,
                "under_budget": replayed < TURN_BUDGET_MS,
                "under_turn_budget": replayed / 1000 <= TURN_BUDGET_S,
                "model_calls": [
                    f"{r['agent']} {r['latency_ms']} ms x{r['attempts']}"
                    + (f" [{r['outcome']}]" if r["outcome"] != "ok" else "")
                    for r in turn_runs
                ]
                if turn not in followup_replays
                else followup_replays[turn]["model_calls"],
            }
        )
    return rows


def check_followup_latency(fx: dict, rows: list[dict]) -> dict:
    """5a: every follow-up turn (T2 to T6) under 60 s."""
    runs = fx["agent_runs"]
    follow = [r for r in rows if r["turn"] != "T1"]
    slow = [r for r in follow if r["under_budget"] is False]
    why = []
    if slow:
        why.append(
            "follow-up turns over the 60 s budget: "
            + ", ".join(f"{r['turn']} {r['replayed_ms'] / 1000:.1f} s" for r in slow)
            + ". Each one still runs nova.creator.main (gemini-3.1-pro, thinking high; 27 clips "
            "of full facts) or ends at the provider deadline; lane E routes edit-shaped "
            "follow-ups to the copilot instead"
        )
    # Who owns each slow turn. Lane C bounds a provider-deadline dead end (see 5b); it does not
    # shorten a Main Creator call that succeeds, and it does not remove the call.
    owners = {}
    for r in slow:
        turn_runs = [x for x in runs if x["turn"] == r["turn"]]
        if any(x["outcome"] != "ok" for x in turn_runs):
            owners[r["turn"]] = (
                "provider deadline dead end: lane C bounds and degrades it (5b), but the turn "
                "only gets under 60 s when lane E routes it away from nova.creator.main"
            )
        elif "nova.creator.main" in " ".join(r["model_calls"]):
            owners[r["turn"]] = (
                "successful nova.creator.main call at thinking high: lane C does not shorten it; "
                "lane E removes the call on edit-shaped follow-ups"
            )
        else:
            owners[r["turn"]] = "other"
    return _result(
        "5a",
        "every follow-up turn finishes inside 60 s (replayed latency)",
        not slow,
        why,
        {
            "turns": follow,
            "budget_ms": TURN_BUDGET_MS,
            "slow_turn_owner": owners,
            "not_timed": [r["turn"] for r in follow if r["under_budget"] is None],
            "timed_on_assumed_latency": [
                r["turn"] for r in follow if r["basis"] == "assumed copilot"
            ],
        },
    )


def check_turn_budget(rows: list[dict], lane_c: dict) -> dict:
    """5b: the creation turn and every failure/degrade path end inside the 180 s turn budget."""
    t1 = next(r for r in rows if r["turn"] == "T1")
    scenarios = lane_c.get("scenarios", {})
    # What T5 and T6 would have taken with the ladder: T5's first call hits the deadline and the
    # medium rung answers; T6 (Try again) opens on medium. Lighter-rung latencies are ASSUMED.
    with_ladder = {
        "T5": scenarios.get("A_high_deadline_then_medium_ok"),
        "T6": scenarios.get("C_try_again_starts_degraded"),
    }
    paths = {
        name: {
            "elapsed_s": run["elapsed_s"],
            "within_turn_budget": run["within_turn_budget"],
            "outcome": run["outcome"],
            "path": run["path"],
        }
        for name, run in scenarios.items()
    }
    failure_rows = [r for r in rows if r["turn"] in ("T5", "T6")]
    why = []
    if not t1["under_turn_budget"]:
        why.append(
            f"creation turn T1 took {t1['replayed_ms'] / 1000:.1f} s, over {TURN_BUDGET_S:.0f} s"
        )
    if not lane_c.get("all_pass"):
        why.append(
            f"lane C fault injection did not pass: {lane_c.get('error') or lane_c.get('checks')}"
        )
    bad = [n for n, p in paths.items() if not p["within_turn_budget"]]
    if bad:
        why.append(f"scenarios over the turn budget: {bad}")
    return _result(
        "5b",
        f"creation turn and every failure/degrade path finish inside the {TURN_BUDGET_S:.0f} s "
        "turn budget",
        not why,
        why,
        {
            "turn_budget_s": TURN_BUDGET_S,
            "creation_turn": {
                "turn": "T1",
                "wall_s": round(t1["replayed_ms"] / 1000, 1),
                "basis": "measured (recorded prod wall time)",
                "within_turn_budget": t1["under_turn_budget"],
                "model_calls": t1["model_calls"],
                "note": "a successful high-thinking call; no retry ladder shortens it (and it "
                "exceeds the 60 s follow-up bar by design)",
            },
            "old_numbers_without_ladder": [
                {
                    "turn": r["turn"],
                    "recorded_wall_s": round(r["recorded_wall_ms"] / 1000, 1),
                    "within_turn_budget": r["under_turn_budget"],
                }
                for r in failure_rows
            ],
            "t5_t6_with_ladder": {
                turn: {
                    "elapsed_s": run["elapsed_s"],
                    "path": run["path"],
                    "basis": "assumed lighter-rung latencies (medium 40 s, low 12 s); rung "
                    "order, timeouts and budget are production code",
                }
                for turn, run in with_ladder.items()
                if run is not None
            },
            "failure_and_degrade_paths": paths,
        },
    )


# ----------------------------------------------------------------------------- lane C


TURN_BUDGET_S = 180.0  # run_kria_turn soft limit (200 s) minus the planner's 20 s reserve


def _virtual_clock():  # noqa: ANN202
    """A clock the fault-injection client advances; nothing sleeps for real."""
    state = {"now": 1000.0}
    return state


@contextlib.contextmanager
def _patched_time(state: dict):  # noqa: ANN202
    import time as _time

    originals = (_time.monotonic, _time.sleep)
    _time.monotonic = lambda: state["now"]  # type: ignore[assignment]
    _time.sleep = lambda seconds: state.__setitem__("now", state["now"] + seconds)  # type: ignore[assignment]
    try:
        yield
    finally:
        _time.monotonic, _time.sleep = originals


def _main_creator_run(
    *,
    latency_s_by_level: dict[str, float | None],
    start_degraded: bool = False,
    ladder: bool = True,
) -> dict:
    """Run the REAL MainCreatorAgent retry/degrade code against injected provider latency.

    ``latency_s_by_level`` is the injected time a call takes at each thinking level; None means
    the provider never answers (the deadline fires). Latencies of the lighter rungs are ASSUMED
    (prod never completed one on this thread); the timeouts, rung order, budget arithmetic and
    final outcome come from the production code.
    """
    from dataclasses import replace

    from app.agents import _text_providers
    from app.agents._runtime import (
        ModelClient,
        ModelInvocation,
        ProviderDeadlineExceededError,
        ProviderOutcomeUnknownError,
        RunContext,
    )
    from app.agents.main_creator import MainCreatorAgent

    state = _virtual_clock()
    calls: list[dict] = []

    class _Client(ModelClient):
        def invoke(self, **kwargs: Any) -> ModelInvocation:
            level = kwargs.get("thinking_level") or "none"
            latency = latency_s_by_level.get(level, latency_s_by_level.get("*"))
            timeout_s = float(kwargs["timeout_s"])
            started = state["now"]
            if latency is None or latency > timeout_s:
                state["now"] += timeout_s
                calls.append(
                    {
                        "model": kwargs["model"],
                        "thinking_level": level,
                        "timeout_s": round(timeout_s, 1),
                        "outcome": "deadline",
                        "t_start_s": round(started - 1000.0, 1),
                    }
                )
                raise ProviderDeadlineExceededError(
                    f"gemini provider outcome unknown after {timeout_s:.1f}s"
                )
            state["now"] += latency
            calls.append(
                {
                    "model": kwargs["model"],
                    "thinking_level": level,
                    "timeout_s": round(timeout_s, 1),
                    "outcome": "ok",
                    "t_start_s": round(started - 1000.0, 1),
                }
            )
            return ModelInvocation(raw_text="{}", tokens_in=1, tokens_out=1)

    class _Agent(MainCreatorAgent):
        spec = replace(MainCreatorAgent.spec, degrade_on_deadline=ladder)

        def render_prompt(self, input: Any) -> str:  # noqa: A002, ANN401
            return "fault injection"

        def required_fields(self) -> list[str]:
            return []

        def parse(self, raw_text: str, input: Any) -> str:  # noqa: A002, ANN401
            return "plan"

    outcome = "ok"
    with _patched_time(state), patch.object(_text_providers, "paid_provider", lambda _m: None):
        ctx = RunContext(
            request_id="fault-injection",
            deadline_monotonic=state["now"] + TURN_BUDGET_S,
            start_degraded=start_degraded,
            extra={"skip_agent_run_persist": True},
        )
        try:
            _Agent(_Client()).run(
                {"user_message": "x", "capability_manifest": _EMPTY_MANIFEST}, ctx=ctx
            )
        except ProviderOutcomeUnknownError as exc:
            outcome = type(exc).__name__
        except Exception as exc:  # noqa: BLE001
            outcome = f"{type(exc).__name__}: {exc}"
    elapsed = state["now"] - 1000.0
    return {
        "calls": calls,
        "outcome": outcome,
        "elapsed_s": round(elapsed, 1),
        "within_turn_budget": elapsed <= TURN_BUDGET_S + 1e-6,
    }


def _empty_manifest():  # noqa: ANN202
    from app.agents._schemas.creator_agent import CapabilityAvailability, ResolvedCreatorManifest

    available = CapabilityAvailability(available=True)
    return ResolvedCreatorManifest(
        item_id="fault-injection",
        edit_format="montage",
        render_program="guided",
        media=[],
        capabilities={"edit_format:montage": available},
        context_hash="a" * 64,
        manifest_hash="b" * 64,
    )


_EMPTY_MANIFEST: Any = None


def lane_c_fault_injection() -> dict:
    """Lane C proof: a provider deadline is answered by a lighter call inside the turn budget."""
    global _EMPTY_MANIFEST
    _EMPTY_MANIFEST = _empty_manifest()
    assumed = {"medium": 40.0, "low": 12.0}  # ASSUMED latencies of the lighter rungs
    scenarios = {
        "A_high_deadline_then_medium_ok": _main_creator_run(
            latency_s_by_level={"high": None, **assumed}
        ),
        "B_high_and_medium_deadline_then_flash_ok": _main_creator_run(
            latency_s_by_level={"high": None, "medium": None, "low": 12.0}
        ),
        "C_try_again_starts_degraded": _main_creator_run(
            latency_s_by_level={"high": None, **assumed}, start_degraded=True
        ),
        "D_every_rung_deadline_stays_in_budget": _main_creator_run(latency_s_by_level={"*": None}),
        "E_ladder_off_is_the_legacy_single_call": _main_creator_run(
            latency_s_by_level={"high": None, **assumed}, ladder=False
        ),
    }
    a, b, c, d, e = scenarios.values()

    def levels(run: dict) -> list[str]:
        return [f"{x['model']}@{x['thinking_level']}:{x['outcome']}" for x in run["calls"]]

    checks = {
        "A: finishes ok on the medium rung": a["outcome"] == "ok"
        and [x["thinking_level"] for x in a["calls"]] == ["high", "medium"]
        and a["within_turn_budget"],
        "B: finishes ok on the fallback model at low": b["outcome"] == "ok"
        and [x["thinking_level"] for x in b["calls"]] == ["high", "medium", "low"]
        and b["calls"][-1]["model"] == "gemini-3.6-flash"
        and b["within_turn_budget"],
        "C: Try again opens on medium, never repeats high": c["outcome"] == "ok"
        and c["calls"][0]["thinking_level"] == "medium",
        "D: when everything times out the turn still ends inside its budget": d[
            "within_turn_budget"
        ]
        and d["outcome"] == "ProviderDeadlineExceededError",
        "E: ladder off ends after the single heavy call (legacy)": len(e["calls"]) == 1
        and e["outcome"] == "ProviderDeadlineExceededError",
    }
    return {
        "turn_budget_s": TURN_BUDGET_S,
        "assumed_latency_s": assumed,
        # What the rung sizes rest on: the recorded successful high calls on THIS clip set.
        # The first rung is capped at turn budget minus reserve (105 s), above every one of
        # these; a call that has not answered by then is the T5/T6 shape, not a slow success.
        "recorded_high_success_s": sorted(
            round(r["latency_ms"] / 1000, 1)
            for r in load_fixture()["agent_runs"]
            if r["agent"] == "nova.creator.main" and r["outcome"] == "ok"
        ),
        "scenarios": {k: {**v, "path": levels(v)} for k, v in scenarios.items()},
        "checks": checks,
        "all_pass": all(checks.values()),
        "note": "recorded T5/T6 were a 180 s dead end (attempts: 2 and 3, 2k output tokens); "
        "lighter-rung latencies are assumed, rung order, timeouts and budget are production code",
    }


# ----------------------------------------------------------------------------- check 6

LIVE_FIXTURE = FIXTURE.parent / "live_new_thread_t1.json"
_BLOCKING = frozenset({"needs_choice", "evidence_missing", "unsupported"})


def load_live_fixture() -> dict[str, Any]:
    return json.loads(LIVE_FIXTURE.read_text(encoding="utf-8"))


def _cassette(raw_text: str):  # noqa: ANN202
    """A model client that answers with one recorded live response (any agent, any prompt)."""
    from app.agents._runtime import ModelClient, ModelInvocation

    class _Recorded(ModelClient):
        calls = 0

        def invoke(self, **_kwargs: Any) -> ModelInvocation:
            type(self).calls += 1
            return ModelInvocation(raw_text=raw_text)

    return _Recorded()


def _live_manifest(lf: dict):  # noqa: ANN202
    from app.agents._schemas.creator_agent import (
        CapabilityAvailability,
        CreatorCatalogRef,
        CreatorMediaRef,
        ResolvedCreatorManifest,
    )

    on = CapabilityAvailability(available=True)
    return ResolvedCreatorManifest(
        item_id="29-ekim-live-item",
        edit_format="montage",
        render_program="guided",
        media=[
            CreatorMediaRef(media_id=c["id"], kind=c["kind"], duration_s=c["duration_s"])
            for c in lf["clips"]
        ],
        catalog=[CreatorCatalogRef(catalog_id="music-track-1", kind="music", label="Licensed")],
        capabilities={
            "dispatch_render": on,
            "draft_guided_proposal": on,
            "edit_format:montage": on,
        },
        context_hash="c" * 64,
        manifest_hash="d" * 64,
    )


def _live_media(lf: dict) -> dict:
    rows = [
        {
            "media_id": c["id"],
            "gcs_path": f"users/redacted/threads/redacted/analysis-proxy-ios-{c['id']}.mp4",
            "kind": c["kind"],
            "duration_s": c["duration_s"],
            "generation": "1",
            "has_analysis": True,
        }
        for c in lf["clips"]
    ]
    return {"clip_paths": [r["gcs_path"] for r in rows], "clip_assignments": rows}


def replay_live_t1(lf: dict, *, planner_raw: str | None = None) -> dict[str, Any]:
    """The new-thread T1 through the real parsers, clip-intent planning, the planner's order
    fields, the approval-readiness gate and the choice gate. Only the three model calls are
    replaced, each by the response the live model gave to this exact message.

    ``planner_raw`` swaps in a variant of the clip-intent planner's answer (the same live
    answer with one field changed), to prove the server holds when the model does not emit
    that field exactly."""
    from app.agents._runtime import RunContext
    from app.agents.main_creator import MainCreatorAgent, MainCreatorInput
    from app.config import settings
    from app.kria import planner
    from app.kria.approval_readiness import assess_readiness, digest
    from app.kria.brief import CreativeBrief, apply_updates, render_brief_request
    from app.services import clip_intent_planning, clip_intent_resolution
    from app.services.clip_intent_planning import (
        plan_and_resolve_clip_intents,
        sequence_text_literals,
        whole_video_text_asks,
    )
    from app.services.clip_intent_resolution import IntentClip
    from app.services.clip_order_sequence import unconfirmed_order_groups
    from app.tasks.kria_runtime import _unresolved_choice_plan

    msg = lf["message"]
    turn_id = lf["source"]["turn_id"]
    live = lf["live"]
    brief = CreativeBrief.model_validate(lf["prod_brief"])
    manifest = _live_manifest(lf)
    ctx = RunContext(
        request_id="29ekim-live-replay",
        extra={"skip_langfuse_trace": True, "skip_agent_run_persist": True},
    )
    mc = MainCreatorAgent(_cassette(live["main_creator"]["raw_text"])).run(
        MainCreatorInput(
            user_message=msg,
            media_context=[
                {"media_id": c["id"], "kind": c["kind"], "duration_s": c["duration_s"]}
                for c in lf["clips"]
            ],
            conversation=[{"role": "user", "content": msg}],
            capability_manifest=manifest,
            brief_enabled=True,
            reply_language="tr",
        ),
        ctx=ctx,
    )
    live_brief = apply_updates(None, mc.brief_updates, source_turn_id=turn_id)

    def shape(b: CreativeBrief) -> list:
        return [(r.kind, r.scope, r.literal, r.facts) for r in b.live()]

    clips = [
        IntentClip(
            media_id=c["id"],
            kind=c["kind"],
            analysis={"subject": c["subject"], "duration_s": c["duration_s"]},
        )
        for c in lf["clips"]
    ]
    clients = {
        clip_intent_planning: _cassette(planner_raw or live["clip_intent_planner"]["raw_text"]),
        clip_intent_resolution: _cassette(live["clip_request_resolver"]["raw_text"]),
    }
    with contextlib.ExitStack() as stack:
        for module, client in clients.items():
            stack.enter_context(patch.object(module, "default_client", lambda c=client: c))
        stack.enter_context(patch.object(settings, "kria_compose_unmet_with_ops_enabled", True))
        stack.enter_context(patch.object(settings, "clip_intent_exclude_enabled", True))
        planned = asyncio.run(
            plan_and_resolve_clip_intents(
                creator_request=msg,
                latest_user_message=msg,
                generated_brief=render_brief_request(brief, latest_message=msg),
                candidate_intents=mc.action.strategy.clip_intents,
                clips=clips,
                run_context=ctx,
                whole_video_texts=whole_video_text_asks(brief),
                sequence_texts=sequence_text_literals(brief),
            )
        )
        resolved, ordering_choice = planner.brief_order_fields(
            planned.resolution.intents, brief, None
        )
        plan = planner.adapt_creator_action(
            mc.action,
            server_clip_intents=planned.requested_intents,
            server_resolved_clip_intents=resolved,
            ordering_choice=ordering_choice,
        )
        strategy = plan.intents[0].arguments["strategy"]
        media = _live_media(lf)
        creator_id = uuid.uuid4()
        report = assess_readiness(
            brief,
            strategy,
            media=media,
            manifest=manifest,
            creator_request=msg,
            creator_id=creator_id,
        )
        blocked = _unresolved_choice_plan(
            strategy,
            brief,
            media,
            contract_brief=brief,
            has_draft=False,
            creator_id=creator_id,
            allow_choices=True,
        )
    literal = next((r.literal for r in brief.live() if r.kind == "text_sequence"), None)
    return {
        "brief_digest_ok": digest(brief.model_dump(mode="json")) == lf["prod_brief_digest"],
        "live_brief_matches_prod": shape(live_brief) == shape(brief),
        "requested_intents": [
            {k: v for k, v in i.model_dump(mode="json").items() if k != "source_quote"}
            for i in planned.requested_intents
        ],
        "unconfirmed_order_groups": sorted(
            name for _p, name, _m in unconfirmed_order_groups(strategy.get("resolved_clip_intents"))
        ),
        "duplicate_sequence_captions": [
            i.intent_id
            for i in planned.requested_intents
            if i.op == "caption" and literal and _nfc(i.creator_text or "") == _nfc(literal)
        ],
        "ordering_choice": strategy.get("ordering_choice"),
        "random_seed": next((r.facts.get("seed") for r in brief.live() if r.kind == "order"), None),
        "ready": report["ready"],
        "policy_problem": report["policy_problem"],
        "rows": [
            {k: r[k] for k in ("requirement_id", "disposition", "evidence_owner", "reason")}
            for r in report["requirements"]
        ],
        "choice_gate": None if blocked is None else blocked[1],
        "strategy": strategy,
    }


# The live planner wrote `order_by: "random"`; a run that writes null (or omits it) must land
# the same way, through the brief's random-order key rather than that field.
_ORDER_BY_VARIANTS = {"order_by_null": ('"order_by": "random"', '"order_by": null')}


def _gate_passes(out: dict) -> bool:
    return bool(
        out["ready"]
        and out["choice_gate"] is None
        and not out["unconfirmed_order_groups"]
        and not [r for r in out["rows"] if r["disposition"] in _BLOCKING]
    )


def check_live_approval_gate() -> dict:
    lf = load_live_fixture()
    out = replay_live_t1(lf)
    variants = {}
    for name, (old, new) in _ORDER_BY_VARIANTS.items():
        raw = lf["live"]["clip_intent_planner"]["raw_text"]
        assert old in raw, f"variant {name} no longer applies to the recorded answer"
        variant = replay_live_t1(lf, planner_raw=raw.replace(old, new))
        variants[name] = {
            "passes": _gate_passes(variant),
            "rows": [(r["requirement_id"], r["disposition"]) for r in variant["rows"]],
            "unconfirmed_order_groups": variant["unconfirmed_order_groups"],
        }
    rows = {r["requirement_id"]: r for r in out["rows"]}
    kinds = {r["id"]: r["kind"] for r in lf["prod_brief"]["requirements"]}
    order_row = next(r for rid, r in rows.items() if kinds.get(rid) == "order")
    seq_row = next(r for rid, r in rows.items() if kinds.get(rid) == "text_sequence")
    blocking = [r for r in out["rows"] if r["disposition"] in _BLOCKING]
    passed = bool(
        out["brief_digest_ok"]
        and out["ready"]
        and not blocking
        and out["choice_gate"] is None
        and order_row["disposition"] == "executable"
        and out["ordering_choice"] == "random"
        and isinstance(out["random_seed"], int)
        and seq_row["disposition"] in {"executable", "unverified"}
        and not out["unconfirmed_order_groups"]
        and not out["duplicate_sequence_captions"]
        and all(v["passes"] for v in variants.values())
    )
    why = []
    for name, variant in variants.items():
        if not variant["passes"]:
            why.append(f"variant {name} of the live planner answer still blocks: {variant['rows']}")
    if not out["brief_digest_ok"]:
        why.append("the fixture brief no longer matches prod's approval_readiness.brief_digest")
    for row in blocking:
        why.append(
            f"{row['requirement_id']} ({kinds.get(row['requirement_id'])}) is "
            f"{row['disposition']}: {row['reason']!r}, so the turn becomes a question"
        )
    if out["unconfirmed_order_groups"]:
        why.append(
            "the clip-intent planner's random order became a positionless order group "
            f"{out['unconfirmed_order_groups']} whose inner order 'wasn't recorded' "
            "(clip_order_sequence.unconfirmed_order_groups -> _check_order needs_choice)"
        )
    if out["duplicate_sequence_captions"]:
        why.append(
            "the text-sequence words also became a clip caption intent "
            f"{out['duplicate_sequence_captions']}: a static copy of the sentence on every clip"
        )
    if out["choice_gate"] is not None:
        why.append(f"the choice gate still blocks: {out['choice_gate']}")
    return _result(
        6,
        "live-shape T1 passes the approval gate (no question)",
        passed,
        why,
        {
            **{k: v for k, v in out.items() if k != "strategy"},
            "planner_variants": variants,
            "prod_outcome": lf["prod_outcome"],
            "ledger": lf["source"]["ledger"],
        },
    )


# ----------------------------------------------------------------------------- checks 7-8
#
# After approval: the job the approved T1 above dispatches, through the real phone worker
# (`_run_phone_unified_montage_job`, then `_run_phone_guided_job` up to the device pin). Only
# storage, the database row and the model clients are stand-ins.

POST_APPROVAL_FIXTURE = FIXTURE.parent / "live_post_approval.json"
_GENERATION = "29ekim-approved-generation"
_APPROVED: dict[str, dict] = {}


def approved_t1(lf: dict) -> dict:
    """The approved T1 strategy, from the live answers to that exact message (check 6)."""
    if "out" not in _APPROVED:
        _APPROVED["out"] = replay_live_t1(lf)
    return _APPROVED["out"]


def _canonical(text: object) -> str:
    return " ".join(unicodedata.normalize("NFC", str(text or "")).split())


def post_approval_job(
    lf: dict, *, brief_raw: dict | None = None, message: str | None = None
) -> SimpleNamespace:
    """The dispatched job: phone sources, pinned brief binding and render contract, the
    approved strategy and the generation's phone text style policy (prod's, KRI-1024)."""
    from app.kria.brief import CreativeBrief
    from app.kria.brief_binding import BriefBinding
    from app.kria.media_sources import OriginalMediaDescriptor
    from app.services.creator_render_contract import (
        CONTRACT_FIELD,
        PLAN_AUTHORITY_FIELD,
        build_render_contract,
    )
    from app.services.phone_sources import PHONE_SOURCES_FIELD, PhoneSourceBinding
    from app.services.phone_text_styling import PHONE_TEXT_STYLE_FIELD

    strategy = approved_t1(lf)["strategy"]
    brief = CreativeBrief.model_validate(brief_raw or lf["prod_brief"])
    media = _live_media(lf)
    rows, sources = [], []
    for index, (clip, row) in enumerate(zip(lf["clips"], media["clip_assignments"], strict=True)):
        rows.append(
            {
                **row,
                "storage_generation": "1",
                "analysis": {"subject": clip["subject"], "duration_s": clip["duration_s"]},
            }
        )
        sources.append(
            PhoneSourceBinding(
                media_id=clip["id"],
                proxy_path=row["gcs_path"],
                generation="1",
                original=OriginalMediaDescriptor(
                    sha256=f"{index:x}".rjust(64, "a"),
                    byte_count=1000 + index,
                    duration_s=clip["duration_s"],
                    width=1080,
                    height=1920,
                    has_audio=True,
                ),
            )
        )
    contract = build_render_contract(
        strategy, generation_id=_GENERATION, brief=brief, media_snapshot=media
    )
    binding = BriefBinding.create(
        uuid.uuid4(),
        brief,
        latest_message=message or lf["message"],
        full_creator_request=message or lf["message"],
        media_snapshot=media,
    )
    snapshot = {
        PHONE_SOURCES_FIELD: [source.model_dump(mode="json") for source in sources],
        "creator_generation_id": _GENERATION,
        "creator_brief_binding": binding.model_dump(mode="json"),
        "creator_reply_language": "tr",
        CONTRACT_FIELD: contract.model_dump(mode="json"),
        PHONE_TEXT_STYLE_FIELD: copy.deepcopy(load_fixture()["phone_text_style_policy"]),
    }
    candidates = {
        "clip_paths": [source.proxy_path for source in sources],
        "edit_format": "montage",
        "creator_strategy": copy.deepcopy(strategy),
        "creator_request": binding.creator_request,
        PLAN_AUTHORITY_FIELD: 1,
    }
    return SimpleNamespace(
        id=uuid.uuid4(),
        user_id=uuid.uuid4(),
        assembly_plan=copy.deepcopy(snapshot),
        all_candidates=candidates,
        status="queued",
        error_detail=None,
        failure_reason=None,
        content_plan_item_id=uuid.uuid4(),
        rows=rows,
        brief=binding.resolve(),
        strategy=strategy,
    )


class _ComposerReached(Exception):  # noqa: N818 - a control-flow sentinel, not an error
    pass


def run_post_approval(
    job: SimpleNamespace,
    *,
    composer_client: Any = None,
    typography_client: Any = None,
    stop_at_composer: bool = False,
    force_flash_fallback: bool = False,
    extra_features: tuple[str, ...] = (),
) -> dict:
    """The real phone worker on ``job`` up to the device pin. Returns what it saw.

    ``extra_features`` are further verified device capabilities for the run (the Assos replay
    verifies ``crossfade``, which a text_explainer / guided_story plan draws; this edit did not).

    ``stop_at_composer`` records the creation text composer's input (no model) and stops
    there. Otherwise ``composer_client`` / ``typography_client`` answer the two model calls
    after approval (the text composer, the phone font pick). ``force_flash_fallback`` makes
    the pro composer's outcome unknown before it reaches the provider, so the worker takes its
    real flash fallback (``CreationTextComposerFast``)."""
    from app import storage
    from app.agents import _model_client
    from app.config import settings
    from app.pipeline.guided_story import compile_execution_plan
    from app.services import clip_facts, creation_text_composition, phone_destination
    from app.tasks import generative_build as gb

    seen: dict[str, Any] = {}
    real_run = creation_text_composition.CreationTextComposer.run

    def composer_run(self, inp, *args, **kwargs):  # noqa: ANN001, ANN002, ANN003, ANN202
        seen.setdefault("composer_inputs", []).append(copy.deepcopy(inp.variant_snapshot))
        if stop_at_composer:
            raise _ComposerReached
        if force_flash_fallback and type(self) is creation_text_composition.CreationTextComposer:
            from app.agents._runtime import ProviderOutcomeUnknownError

            raise ProviderOutcomeUnknownError("forced: the pro composer's outcome is unknown")
        return real_run(self, inp, *args, **kwargs)

    @contextlib.contextmanager
    def sessions():  # noqa: ANN202
        yield SimpleNamespace(commit=lambda: None, rollback=lambda: None)

    def guided_plan(_job_id, guided, **_kwargs):  # noqa: ANN001, ANN202
        plan = compile_execution_plan(guided, track=None)
        job.assembly_plan["guided_story_execution_plan"] = plan
        return plan, None

    def no_storage(*_args, **_kwargs):  # noqa: ANN202
        raise OSError("the scorecard has no storage")

    with contextlib.ExitStack() as stack:
        patches = [
            (gb, "_load_unified_montage_inputs", lambda _j: (job.user_id, job.rows, job.brief)),
            (gb, "_load_unified_montage_visuals", lambda *_a, **_k: []),
            (gb, "_sync_session", sessions),
            (gb, "_lock_owned_entry_job", lambda *_a: (job, 1)),
            (gb, "_guided_execution_plan", guided_plan),
            (gb, "_emit_post_caption", lambda *_a, **_k: None),
            (clip_facts, "enrich_clip_facts", lambda pairs, **_k: list(pairs)),
            (phone_destination, "phone_rendering_allowed_for_job", lambda _job: True),
            (storage, "download_to_file", no_storage),
            (creation_text_composition.CreationTextComposer, "run", composer_run),
            (settings, "phone_rendering_enabled", True),
            (settings, "phone_text_styling_enabled", True),
            (
                settings,
                "phone_render_verified_features",
                [
                    "basicComposition",
                    "local1080Export",
                    "positionedText",
                    "animatedText",
                    "authoredText",
                    "audioMix",
                    *extra_features,
                ],
            ),
        ]
        if composer_client is not None:
            patches.append((creation_text_composition, "default_client", lambda: composer_client))
        if typography_client is not None:
            patches.append((_model_client, "default_client", lambda: typography_client))
        for target, name, value in patches:
            stack.enter_context(patch.object(target, name, value))
        try:
            unified = gb._run_phone_unified_montage_job(
                str(job.id), copy.deepcopy(job.assembly_plan), job.all_candidates, ownership_epoch=1
            )
        except _ComposerReached:
            return seen
        except Exception as exc:  # noqa: BLE001 - reported as evidence, never green
            seen["worker_error"] = f"{type(exc).__name__}: {exc}"
            return seen
        seen["unified"] = copy.deepcopy(unified)
        try:
            gb._run_phone_guided_job(
                str(job.id),
                copy.deepcopy(unified),
                ownership_epoch=1,
                creator_strategy=job.all_candidates["creator_strategy"],
            )
        except Exception as exc:  # noqa: BLE001 - reported as evidence, never green
            seen["pin_error"] = f"{type(exc).__name__}: {exc}"
    seen["assembly_plan"] = copy.deepcopy(job.assembly_plan)
    seen["status"] = job.status
    return seen


def check_composer_input() -> dict:
    """Check 7: the approved job hands the creation text composer the title bar to split."""
    from app.pipeline.unified_montage import title_source_exists
    from app.services.kria_editor_ops import is_caption_text_bar

    lf = load_live_fixture()
    job = post_approval_job(lf)
    literal = next(r.literal for r in job.brief.live() if r.kind == "text_sequence")
    seen = run_post_approval(job, stop_at_composer=True)
    inputs = seen.get("composer_inputs") or []
    bars = [bar for bar in inputs[0]["text_bars"] if not is_caption_text_bar(bar)] if inputs else []
    exists = title_source_exists(job.strategy, job.brief)
    sequence_reqs = (
        [
            r["id"]
            for r in (inputs[0].get("text_requirements") or [])
            if r["kind"] == "text_sequence"
        ]
        if inputs
        else []
    )
    passed = bool(
        inputs
        and exists
        and len(bars) == 1
        and _canonical(bars[0]["text"]) == _canonical(literal)
        and sequence_reqs
    )
    why = []
    if not inputs:
        why.append(f"the worker never reached the text composer: {seen.get('worker_error')}")
    if not exists:
        why.append(
            "unified_montage.title_source_exists is False for the approved brief: brief_view "
            "sets title_literal only for kind == 'text', so the text_sequence has no title bar"
        )
    if inputs and len(bars) != 1:
        why.append(
            f"the composer got {len(bars)} text bars ({[b['text'][:40] for b in bars]}): with "
            "no bar to split it answers 'the text must already be established' / 'I couldn't "
            "find a title' and the job fails as creation_text_failed"
        )
    if inputs and not sequence_reqs:
        why.append("the composer's text brief does not carry the text_sequence requirement")
    total = inputs[0]["total_duration_s"] if inputs else None
    return _result(
        7,
        "post-approval: the text composer receives the title bar to split (no model)",
        passed,
        why,
        {
            "title_source_exists": exists,
            "text_bars": [{k: b[k] for k in ("id", "text", "start_s", "end_s")} for b in bars],
            "text_requirement_ids": sequence_reqs,
            "total_duration_s": total,
            "words": len(literal.split()),
            "slot_order": [s["media_id"] for s in inputs[0]["slots"]] if inputs else [],
        },
    )


def post_approval_verdict(lf: dict, seen: dict, *, brief_raw: dict | None = None) -> dict:
    """(a)-(e) of the post-approval acceptance on one worker run; shared by check 8 and the
    live capture script."""
    import random

    from app.config import settings
    from app.kria.brief import CreativeBrief
    from app.kria.brief_checks import check_requirement, plan_facts_from_phone_variant
    from app.pipeline.sequence_text_evidence import sequence_lineage
    from app.services.device_render import DEVICE_RENDER_FIELD

    brief = CreativeBrief.model_validate(brief_raw or lf["prod_brief"])
    sequence_req = next(r for r in brief.live() if r.kind == "text_sequence")
    order_req = next(r for r in brief.live() if r.kind == "order")
    literal = _canonical(sequence_req.literal)
    plan = seen.get("assembly_plan") or {}
    variant = (plan.get("variants") or [{}])[0]
    attachment = [c["id"] for c in lf["clips"]]
    expected = list(attachment)
    random.Random(order_req.facts["seed"]).shuffle(expected)
    order = [m["media_id"] for m in variant.get("story_timeline") or [] if m.get("media_id")]
    lanes = [*variant.get("text_elements", []), *variant.get("context_label_text_elements", [])]
    live = [row for row in lanes if not row.get("removed") and _canonical(row.get("text"))]
    pieces = sorted(
        (row for row in live if (sequence_lineage(row) or ("", 0))[0] == "guided-title"),
        key=lambda row: sequence_lineage(row)[1],  # type: ignore[index]
    )
    frame = 1 / 30
    windows = [(float(r["start_s"]), float(r["end_s"])) for r in pieces]
    durations = [round(end - start, 4) for start, end in windows]
    contiguous = all(
        abs(windows[i][1] - windows[i + 1][0]) <= frame for i in range(len(windows) - 1)
    )
    static = [row["id"] for row in live if _canonical(row.get("text")) == literal]
    styled = [
        row["id"]
        for row in pieces
        if not (row.get("source_params") or {}).get("phone_text_style_version")
    ]
    with patch.object(settings, "kria_compose_unmet_with_ops_enabled", True):
        receipt = check_requirement(sequence_req, plan_facts_from_phone_variant(variant))
    pinned = (plan.get(DEVICE_RENDER_FIELD) or {}).get("guided_story")
    words = literal.split()
    result = {
        "a_shuffle": bool(order == expected and order != attachment),
        "b_one_word_pieces": bool(
            len(pieces) == len(words)
            and all(len(_canonical(r["text"]).split()) == 1 for r in pieces)
            and " ".join(_canonical(r["text"]) for r in pieces) == literal
            and contiguous
            and windows
            and windows[0][0] <= frame
            and min(durations) >= 0.25 - 1e-6
        ),
        "c_no_static_sentence": not static,
        "d_phone_text_style_and_pin": bool(
            pinned and not seen.get("pin_error") and not seen.get("worker_error") and not styled
        ),
        "e_r3_met": receipt.status == "met",
    }
    return {
        "pass": all(result.values()),
        "parts": result,
        "evidence": {
            "worker_error": seen.get("worker_error"),
            "pin_error": seen.get("pin_error"),
            "status": seen.get("status"),
            "slot_order": order,
            "seeded_order": expected,
            "pieces": len(pieces),
            "words": len(words),
            "title_span_s": [windows[0][0], windows[-1][1]] if windows else None,
            "piece_durations_s": durations,
            "min_piece_s": min(durations) if durations else None,
            "contiguous": contiguous,
            "static_sentence_rows": static,
            "unstyled_pieces": styled,
            "r3_receipt": {"status": receipt.status, "reason": receipt.reason},
            "device_pin": bool(pinned),
        },
    }


def english_variant(lf: dict, recorded: dict) -> tuple[dict, str]:
    """The same job with r3's words (and the creator's message) in English."""
    variant = recorded["source"]["english_variant"]
    brief_raw = copy.deepcopy(lf["prod_brief"])
    for row in brief_raw["requirements"]:
        if row["kind"] == "text_sequence":
            row["literal"] = variant["literal"]
    return brief_raw, variant["message"]


def check_post_approval_replay() -> dict:
    """Check 8: every recorded live composer answer, replayed through the worker to the pin."""
    lf = load_live_fixture()
    recorded = json.loads(POST_APPROVAL_FIXTURE.read_text(encoding="utf-8"))
    primary = recorded["runs"][recorded["primary"]]
    rows = []
    for index, run in enumerate(recorded["runs"]):
        brief_raw, message = (
            english_variant(lf, recorded) if run["language"] == "en" else (None, None)
        )
        job = post_approval_job(lf, brief_raw=brief_raw, message=message)
        typography = run["typography"] or primary["typography"]
        seen = run_post_approval(
            job,
            composer_client=_cassette(run["composer"]["raw_text"]),
            typography_client=_cassette(typography["raw_text"]),
        )
        verdict = post_approval_verdict(lf, seen, brief_raw=brief_raw)
        rows.append(
            {
                "run": index,
                "language": run["language"],
                "path": run["path"],
                "prompt_version": run["prompt_version"],
                "pass": verdict["pass"],
                "failed_parts": [part for part, ok in verdict["parts"].items() if not ok],
                "title_span_s": verdict["evidence"]["title_span_s"],
                "min_piece_s": verdict["evidence"]["min_piece_s"],
                "pieces": verdict["evidence"]["pieces"],
                "worker_error": verdict["evidence"]["worker_error"],
                **({"detail": verdict["evidence"]} if index == recorded["primary"] else {}),
            }
        )
    why = [
        f"recorded run {row['run']} ({row['language']}, {row['path']}, {row['prompt_version']}) "
        f"fails {row['failed_parts']}: {row['worker_error'] or row.get('detail', '')}"
        for row in rows
        if not row["pass"]
    ]
    return _result(
        8,
        "post-approval: the recorded live composer splits render to the device pin (replay)",
        all(row["pass"] for row in rows),
        why,
        {"runs": rows, "live_summary": recorded["summary"], "ledger": recorded["source"]["ledger"]},
    )


# ----------------------------------------------------------------------------- report


def _result(check_id: int | str, name: str, passed: bool, why: list[str], evidence: dict) -> dict:
    return {
        "id": check_id,
        "name": name,
        "pass": passed,
        "verdict": "VERIFIED" if passed else "NOT VERIFIED",
        "why_it_fails": [] if passed else why,
        "evidence": evidence,
    }


def _guarded(check_id: int | str, name: str, fn) -> dict:  # noqa: ANN001
    try:
        return fn()
    except Exception as exc:  # noqa: BLE001 - a check that cannot run is INCONCLUSIVE, never green
        return {
            "id": check_id,
            "name": name,
            "pass": False,
            "verdict": "INCONCLUSIVE",
            "why_it_fails": [f"check could not run: {type(exc).__name__}: {exc}"],
            "evidence": {"traceback": traceback.format_exc(limit=6)},
        }


def run_offline() -> dict:
    fx = load_fixture()
    try:
        lane_c = lane_c_fault_injection()
    except Exception as exc:  # noqa: BLE001 - reported, never green
        lane_c = {"all_pass": False, "error": f"{type(exc).__name__}: {exc}"}
    with _prod_flags():
        c1 = _guarded(1, "order", lambda: check_order(fx))
        c2 = _guarded(2, "text vs speech", lambda: check_text_vs_speech(fx))
        c3 = _guarded(3, "title fit", lambda: check_title_fit(fx))
        title_fits = bool(c3["pass"])
        followups: dict[str, dict] = {}

        def _c4() -> dict:
            result, replay = check_followup_with_ops(fx, title_fits)
            followups["T5"] = replay
            return result

        c4 = _guarded(4, "followup with ops", _c4)
        if "T5" in followups:
            with contextlib.suppress(Exception):
                followups["T6"] = asyncio.run(
                    replay_followup_turn(fx, "T6", job_status=_t4_job_status(fx, title_fits))
                )
        # T2 is an edit-shaped follow-up on T1's ready draft ("show each word after the other"):
        # the question it used to open is exactly what lane E removes, so it goes through the
        # real router too. Only the model outputs are fixtures.
        with contextlib.suppress(Exception):
            followups["T2"] = asyncio.run(
                replay_followup_turn(fx, "T2", job_status="variants_ready")
            )
        rows: list[dict] = []

        def _rows() -> list[dict]:
            rows.extend(_latency_rows(fx, followups))
            return rows

        c5a = _guarded("5a", "follow-up latency", lambda: check_followup_latency(fx, _rows()))
        c5b = _guarded("5b", "turn budget", lambda: check_turn_budget(rows, lane_c))
        c6 = _guarded(6, "live-shape approval gate", check_live_approval_gate)
        c7 = _guarded(7, "post-approval composer input", check_composer_input)
        c8 = _guarded(8, "post-approval live replay", check_post_approval_replay)
    checks = [c1, c2, c3, c4, c5a, c5b, c6, c7, c8]
    return {
        "lane_c": lane_c,
        "fixture": fx["fixture_id"],
        "mode": "offline",
        "source": {
            "prod_main_sha": fx["source"]["prod_main_sha"],
            "thread_id": fx["source"]["thread_id"],
            "model_outputs": fx["source"]["model_outputs"],
        },
        "network_attempts_blocked": len(_NET_ATTEMPTS),
        "passed": sum(1 for c in checks if c["pass"]),
        "failed": sum(1 for c in checks if not c["pass"]),
        "all_green": all(c["pass"] for c in checks),
        "checks": checks,
    }


def refuse_live() -> int:
    if not os.environ.get(LIVE_LEDGER_ENV, "").strip():
        print(
            f"--live refused: paid Gemini calls need an approved development ledger "
            f"(hard cap $2, no judge). Set {LIVE_LEDGER_ENV}=<ledger id> after approval.",
            file=sys.stderr,
        )
        return 2
    print(
        "--live is gated but not wired in the harness commit: each lane records its own live "
        "capture into the fixture after approval (tests/evals/README.md, paid items).",
        file=sys.stderr,
    )
    return 2


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--out", type=Path, help="also write the JSON report here")
    parser.add_argument("--live", action="store_true", help=f"paid; needs ${LIVE_LEDGER_ENV}")
    parser.add_argument("--strict", action="store_true", help="exit 1 unless every check passes")
    args = parser.parse_args(argv)
    if args.live:
        return refuse_live()
    _bootstrap()
    report = run_offline()
    text = json.dumps(report, ensure_ascii=False, indent=2)
    if args.out:
        args.out.parent.mkdir(parents=True, exist_ok=True)
        args.out.write_text(text + "\n", encoding="utf-8")
    print(text)
    return 1 if args.strict and not report["all_green"] else 0


if __name__ == "__main__":
    raise SystemExit(main())
