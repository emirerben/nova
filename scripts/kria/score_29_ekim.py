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
5. Every turn's replayed wall time is under 60 s.

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
            strategy={"opening_title": strat.get("opening_title"), "edit_format": "montage"},
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
    from app.kria.brief import BriefUpdate, CreativeBrief, apply_updates, parse_brief_updates
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
                kind="text", scope=r3["scope"], literal=r3["literal"], description=r3["description"]
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
                    operation="remove", target_requirement_id=i, expected_version=brief.version
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
                    {"turn": turn, "option": option["label"], "captions_premise_remains": premise}
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
            placed = style_rows([copy.deepcopy(row)], policy, selected_fonts={text_role(row): font})
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
    from app.agents._schemas.creator_agent import CapabilityAvailability, ResolvedCreatorManifest

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
            planner, "resolve_item_creator_context", AsyncMock(return_value=(manifest, []))
        ),
        patch.object(planner, "creator_context", lambda *_a: ("creator", "item")),
        patch.object(planner, "_load_editor_target", AsyncMock(return_value=target)),
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
    """Compile the word-by-word op with the real editor-op compiler against the recorded title."""
    from app.services.kria_editor_ops import compile_editor_ops

    title = copy.deepcopy(fx["title_row"])
    words = title["text"].split()
    variant = {
        "variant_id": "original_text",
        "render_status": "ready",
        "render_generation_id": "scorecard",
        "resolved_archetype": "montage",
        "duration_s": max(title["end_s"], 1.0),
        "text_elements": [title],
        "caption_cues": [],
        "ai_timeline": {"slots": []},
    }
    job = SimpleNamespace(assembly_plan={"variants": [variant]}, all_candidates={"clip_paths": []})
    compiled = compile_editor_ops(
        job,
        variant,
        [
            {
                "op": "replace_text_sequence",
                "selector": {"ids": [title["id"]]},
                "target_ids": [title["id"]],
                "expected_count": 1,
                "expected_source_text": title["text"],
                "segments": words,
            }
        ],
    )
    rows = compiled.payload.text_elements
    windows = [(r["start_s"], r["end_s"]) for r in rows]
    ordered = all(a[1] <= b[0] + 1e-9 for a, b in zip(windows, windows[1:], strict=False))
    covers = (
        bool(windows)
        and abs(windows[0][0] - title["start_s"]) < 1e-6
        and abs(windows[-1][1] - title["end_s"]) < 1e-6
    )
    return {
        "elements": len(rows),
        "words": len(words),
        "one_word_each": all(len(r["text"].split()) == 1 for r in rows),
        "ordered_windows": ordered,
        "covers_title_span": covers,
        "min_window_s": min(e - s for s, e in windows) if windows else None,
        "text_conserved": " ".join(r["text"] for r in rows) == title["text"],
    }


def _t4_job_status(_fx: dict, title_fits: bool) -> str:
    return "variants_ready" if title_fits else "processing_failed"


def check_followup_with_ops(fx: dict, title_fits: bool) -> tuple[dict, dict]:
    """T5 is replayed from BOTH states T4 can leave: its recorded failed render, and a ready one.

    A follow-up must not depend on the previous render having succeeded, so both must reach the
    copilot with zero Main Creator calls. Returns the result and the replay for the state the
    system is actually in (it feeds check 5).
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
    built_ok = (
        built["elements"] == built["words"]
        and built["one_word_each"]
        and built["ordered_windows"]
        and built["covers_title_span"]
        and built["text_conserved"]
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
        }
        if creator_calls:
            why.append(
                f"[{name}] T5 reaches nova.creator.main ({creator_calls} call) and no editor op "
                "is ever tried: planner._plan_live_turn treats a turn after a FAILED latest job "
                "as a fresh plan (extract_first needs a non-failed job), so the full replan is "
                "the only path; prod's real call hit the provider deadline at 179.8 s"
            )
        if not used_ops:
            why.append(
                f"[{name}] no editor op reaches the turn plan (replace_text_sequence is not in "
                "planner._FAST_PATH_OPS and Main Creator's strategy has no field for it)"
            )
    if not built_ok:
        why.append(f"the op does not compile to ordered one-word elements: {built}")
    passed = built_ok and all(
        v["creator_main_calls"] == 0 and v["editor_ops_reach_the_plan"] for v in summary.values()
    )
    actual = "ready_render" if title_fits else "failed_render"
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
            f"{built['words']} words ({built['min_window_s']:.3f} s each); the op conserves "
            "wording and spans the window, it does not make the window readable",
            "copilot_output": "assumed: the prod copilot never ran on this thread, so the op the "
            "copilot authors is a fixture value; routing and the op compile are real code",
        },
    ), replays[actual]


# ----------------------------------------------------------------------------- check 5


def check_latency(fx: dict, followup_replays: dict[str, dict]) -> dict:
    """Replayed wall time per turn: recorded agent latency for the calls the real router made
    (assumed values where the recording has none), plus the recorded non-agent overhead."""
    runs = fx["agent_runs"]
    recorded_wall = {t["turn"]: t["wall_ms"] for t in fx["timeline"]}
    rows = []
    for turn in ("T1", "T2", "T3", "T4", "T5", "T6"):
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
                "model_calls": [
                    f"{r['agent']} {r['latency_ms']} ms x{r['attempts']}"
                    + (f" [{r['outcome']}]" if r["outcome"] != "ok" else "")
                    for r in turn_runs
                ]
                if turn not in followup_replays
                else followup_replays[turn]["model_calls"],
            }
        )
    slow = [r for r in rows if not r["under_budget"]]
    passed = not slow
    why = []
    if slow:
        why.append(
            "turns over the 60 s budget: "
            + ", ".join(f"{r['turn']} {r['replayed_ms'] / 1000:.1f} s" for r in slow)
            + ". Every one runs nova.creator.main (gemini-3.1-pro, thinking high; 31k to 63k "
            "input tokens, 27 clips of full facts); T5 and T6 end at the provider deadline "
            "(179.8 s and 179.7 s of model time) and T6 retries the same call unchanged"
        )
    # Who owns each slow turn. Lane C bounds a provider-deadline dead end (see `lane_c`); it
    # does not shorten a Main Creator call that succeeds, and it does not remove the call.
    owners = {}
    for r in rows:
        if r["under_budget"]:
            continue
        turn_runs = [x for x in runs if x["turn"] == r["turn"]]
        if any(x["outcome"] != "ok" for x in turn_runs):
            owners[r["turn"]] = (
                "provider deadline dead end: lane C bounds and degrades it (lane_c block), but "
                "the turn only gets under 60 s when lane E routes it away from nova.creator.main"
            )
        elif "nova.creator.main" in " ".join(r["model_calls"]):
            owners[r["turn"]] = (
                "successful nova.creator.main call at thinking high: lane C does not shorten it; "
                "lane E removes the call on edit-shaped follow-ups, creation turns keep it"
            )
        else:
            owners[r["turn"]] = "other"
    return _result(
        5,
        "every turn finishes inside 60 s (replayed latency)",
        passed,
        why,
        {
            "turns": rows,
            "budget_ms": TURN_BUDGET_MS,
            "slow_turn_owner": owners,
            "joint_check": "needs lane C (bounded failure) AND lane E (no Main Creator call on "
            "follow-ups); lane C alone cannot turn it green",
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


# ----------------------------------------------------------------------------- report


def _result(check_id: int, name: str, passed: bool, why: list[str], evidence: dict) -> dict:
    return {
        "id": check_id,
        "name": name,
        "pass": passed,
        "verdict": "VERIFIED" if passed else "NOT VERIFIED",
        "why_it_fails": [] if passed else why,
        "evidence": evidence,
    }


def _guarded(check_id: int, name: str, fn) -> dict:  # noqa: ANN001
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
        c5 = _guarded(5, "latency", lambda: check_latency(fx, followups))
    checks = [c1, c2, c3, c4, c5]
    try:
        lane_c = lane_c_fault_injection()
    except Exception as exc:  # noqa: BLE001 - reported, never green
        lane_c = {"all_pass": False, "error": f"{type(exc).__name__}: {exc}"}
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
