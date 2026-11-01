#!/usr/bin/env python3
"""Scorecard for the Assos request: does the creator's prompt reach the agents, and do the
agents' decisions reach the renderer? (prod thread "Why Visit Assos, Turkey? (Animated)")

Replays the recorded request ("With animations, explain why people should visit Assos,
Turkey", montage, 16 clips) through the REAL planner-side card code, the REAL phone
unified-montage worker, the REAL research and text-composition code, the phone text style pass
and the device recipe compile. Only I/O is replaced: storage, the database row, the web search
and the model clients (answered by recorded or reconstructed model text). Offline: a socket
guard fails any network attempt, no model is called, cost is $0.

    cd src/apps/api && .venv/bin/python ../../../scripts/kria/score_assos.py [--out report.json]
    make kria-replay FIXTURE=assos

The assertions are written from the creator's words, not from what the code does today. Phase 0
(this file) builds only the harness: on unchanged code they are RED, and later phases turn them
green. A named check is VERIFIED or NOT VERIFIED; a check that could not run is INCONCLUSIVE.

Checks:

1.  animation: "with animations" is applied to the rendered text, or declined with a typed reason
    BEFORE approval.
2.  pace and direction are not constants: the planner's pacing changes the compiled cut lengths
    in the expected direction, and the snapshot direction and pace come from the strategy.
3.  hook: the planner's intro_hook is rendered, or declined with a reason before approval.
4.  story order: story_structure changes the story order, or is declined or disclosed.
5.  length: every length the creator is shown before approval equals the recipe length.
6.  "creator selected": stamped only for an explicit orientation pick, not an echoed default.
7.  research that runs after approval is disclosed on the approval card.
8.  the look chip names the font the text is burned in.
9.  a provider failure on a follow-up is not reported as an unreadable request.
10. consumer gate (REPORT ONLY, owner rule: the product never fails a user for these gaps):
    reads the unified montage's registry table (`app.kria.route_capabilities.unified_montage`,
    shared gate in `score_route.py`): lists every CreativeStrategy field the route does not read
    and the card does not disclose (its own registry line, `score_route.field_disclosed`). It
    never fails the wrapper, CI or exit status.
11. song rule: no catalog music is mixed on the phone route for this job (green control).
12. registry claims: every field the unified table marks applied is read off the strategy in
    the route's code, or names a ``file:symbol`` carrier that reads it (`score_route`
    ``10.claims``). Scored.

Checks 3 and 4 accept the card only through that field's own registry line, never free text.

Negative controls (mandatory): checks 1 and 2 each get a synthetic output that ignores the ask
(the check must FAIL) and one that honours it (the check must PASS); check 10 gets a synthetic
new field and a synthetic applied claim with no reader (both must be flagged). A failed control
makes the whole scorecard INVALID.

``--fixture-dir`` replays another capture of the same shape (default: the assos fixture).
"""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import copy
import importlib.util
import io
import json
import re
import sys
import uuid
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, patch

HERE = Path(__file__).resolve().parent
REPO = HERE.parents[1]
API = REPO / "src" / "apps" / "api"
FIXTURE_DIR = API / "tests" / "fixtures" / "kria_replay" / "assos"
APP = API / "app"
CREATOR_SELECTED_REASON = "The creator selected this output format."
FRAME_S = 1 / 30


def _load_29_ekim():  # noqa: ANN202
    """The 29-ekim scorecard's helpers (socket guard, prod flags, cassette, worker driver)."""
    spec = importlib.util.spec_from_file_location("score_29_ekim", HERE / "score_29_ekim.py")
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


_BASE = _load_29_ekim()
_result = _BASE._result
_guarded = _BASE._guarded


def _load_route_grader():  # noqa: ANN202
    """The generic route scorecard: card words and the registry consumer gate."""
    spec = importlib.util.spec_from_file_location("score_route", HERE / "score_route.py")
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


_ROUTE = _load_route_grader()


def _prod_flags():  # noqa: ANN202
    """The shared prod flags plus the device capability the story directions draw with.

    Real prod verifies ``crossfade`` (`tests/_prod_profile.PROD_VERIFIED_FEATURES`); the shared
    29-Ekim set lists only the two text capabilities that edit needed. A text_explainer or
    guided_story plan crossfades its moments, so the Assos replay verifies it too.
    """
    from app.config import settings

    stack = _BASE._prod_flags()
    stack.enter_context(
        patch.object(
            settings,
            "phone_render_verified_features",
            [*settings.phone_render_verified_features, "crossfade"],
        )
    )
    return stack


_cassette = _BASE._cassette


def load_fixture(fixture_dir: Path = FIXTURE_DIR) -> dict[str, Any]:
    return json.loads((fixture_dir / "fixture.json").read_text(encoding="utf-8"))


def load_research(fixture_dir: Path = FIXTURE_DIR) -> dict[str, Any]:
    return json.loads((fixture_dir / "research.json").read_text(encoding="utf-8"))


# ------------------------------------------------------------------------------- the job


def _upload_contract(clip: dict) -> dict:
    original = clip["original"]
    return {
        "proxy": {
            "width": clip["proxy"]["width"],
            "height": clip["proxy"]["height"],
            "original": {
                "kind": "video",
                "width": original["width"],
                "height": original["height"],
                "sha256": original["sha256"],
                "has_audio": original["has_audio"],
                "byte_count": original["byte_count"],
                "duration_s": original["duration_s"],
                "orientation_degrees": original["orientation_degrees"],
            },
            "duration_s": clip["proxy"]["duration_s"],
            "frame_rate": clip["proxy"]["frame_rate"],
            "timing_version": 1,
            "orientation_degrees": 0,
        },
        "purpose": "analysis_proxy",
    }


def media_rows(fx: dict) -> list[dict]:
    return [
        {
            "kind": clip["kind"],
            "analysis": copy.deepcopy(clip["analysis"]),
            "gcs_path": f"users/synthetic/threads/synthetic/analysis-proxy-ios-{clip['id']}.mp4",
            "media_id": clip["id"],
            "has_audio": clip["has_audio"],
            "duration_s": clip["duration_s"],
            "source_filename": clip["source_filename"],
            "upload_contract": _upload_contract(clip),
            "storage_generation": "1",
            "generation": "1",
            "has_analysis": True,
        }
        for clip in fx["clips"]
    ]


def manifest(fx: dict):  # noqa: ANN201
    from app.agents._schemas.creator_agent import (
        CapabilityAvailability,
        CreatorCatalogRef,
        CreatorMediaRef,
        ResolvedCreatorManifest,
    )

    on = CapabilityAvailability(available=True)
    return ResolvedCreatorManifest(
        item_id="assos-item",
        edit_format="montage",
        render_program="guided",
        media=[
            CreatorMediaRef(media_id=c["id"], kind=c["kind"], duration_s=c["duration_s"])
            for c in fx["clips"]
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


def server_default_shape(fx: dict) -> dict:
    """What the confirm screen preselects for these clips, from the real offer code. The app
    echoes it back unless the creator touches the picker."""
    from app.services import render_shape

    item = SimpleNamespace(clip_assignments=media_rows(fx), landscape_fit="fit")
    offer = render_shape.creation_offer(item, fx["strategy"], landscape_enabled=True)
    choice = render_shape.resolve_choice(offer, offer.default_orientation, offer.default_fit)
    return dict(choice or {})


def build_job(
    fx: dict, *, strategy: dict | None = None, shape: dict | None = None
) -> SimpleNamespace:
    """The dispatched job: pinned brief binding and render contract, the approved strategy,
    phone sources and the approval-time length plan (the real planner function builds it)."""
    from app.kria import planner
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

    strategy = copy.deepcopy(strategy or fx["strategy"])
    brief = CreativeBrief.model_validate(fx["brief"])
    rows = media_rows(fx)
    sources = []
    for clip, row in zip(fx["clips"], rows, strict=True):
        original = clip["original"]
        sources.append(
            PhoneSourceBinding(
                media_id=clip["id"],
                proxy_path=row["gcs_path"],
                generation="1",
                original=OriginalMediaDescriptor(
                    sha256=original["sha256"],
                    byte_count=original["byte_count"],
                    duration_s=original["duration_s"],
                    width=original["width"],
                    height=original["height"],
                    orientation_degrees=original["orientation_degrees"],
                    has_audio=original["has_audio"],
                ),
            )
        )
    media = {"clip_paths": [r["gcs_path"] for r in rows], "clip_assignments": rows}
    media = planner._enrich_best_parts_snapshot(strategy, media, brief)
    contract = build_render_contract(
        strategy, generation_id="assos-generation", brief=brief, media_snapshot=media
    )
    binding = BriefBinding.create(
        uuid.uuid4(),
        brief,
        latest_message=fx["message"],
        full_creator_request=fx["message"],
        media_snapshot=media,
    )
    snapshot = {
        PHONE_SOURCES_FIELD: [s.model_dump(mode="json") for s in sources],
        "creator_generation_id": "assos-generation",
        "creator_brief_binding": binding.model_dump(mode="json"),
        "creator_reply_language": "en",
        CONTRACT_FIELD: contract.model_dump(mode="json"),
        PHONE_TEXT_STYLE_FIELD: copy.deepcopy(fx["phone_text_style_policy"]),
    }
    candidates = {
        "clip_paths": [s.proxy_path for s in sources],
        "edit_format": "montage",
        "creator_strategy": copy.deepcopy(strategy),
        "creator_request": binding.creator_request,
        PLAN_AUTHORITY_FIELD: 1,
        "creator_render_shape": dict(shape or fx["render_shape"]),
        "creator_client_protocol": fx["client_protocol"],
        "landscape_fit": "fit",
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
        binding=binding,
        media=media,
    )


class _Sequence:
    """A model client that answers a fixed list of recorded texts in call order.

    The recorded caption-binding answer names research windows by id ("window-3"), and a
    window id only means something against the timeline it was written for. ``rebind`` keeps
    what the model decided (each caption's words, evidence and the quote of the picture it
    describes) and re-seats each caption on the first window of THIS plan that shows that
    picture and does not overlap one already used, in the recorded order; a caption whose
    picture is in no free window is left out, as the model would have left it out. With the
    plan the answer was recorded against it picks the same windows.
    """

    def __init__(self, texts: list[str], *, rebind: bool = False) -> None:
        self.texts = list(texts)
        self.rebind = rebind

    def client(self):  # noqa: ANN202
        from app.agents._runtime import ModelClient, ModelInvocation

        texts, rebind = self.texts, self.rebind

        class _Recorded(ModelClient):
            def invoke(self, **kwargs: Any) -> ModelInvocation:
                raw = texts.pop(0)
                return ModelInvocation(
                    raw_text=_rebound(raw, str(kwargs.get("prompt") or "")) if rebind else raw
                )

        return _Recorded()


def _rebound(raw: str, prompt: str) -> str:
    """``raw`` (a recorded caption-binding answer) bound to the windows in ``prompt``."""
    try:
        answer = json.loads(raw)
        windows = json.loads(prompt.rsplit("Data:\n", 1)[1])["snapshot"]["research_windows"]
    except (ValueError, KeyError, IndexError):
        return raw
    if not isinstance(answer, dict) or not isinstance(answer.get("captions"), list):
        return raw
    used: list[tuple[float, float]] = []
    kept = []
    for caption in answer["captions"]:
        quote = str(caption.get("visual_quote") or "")
        shown = f"{caption.get('context_label') or ''}: {caption.get('text') or ''}"
        for window in windows:
            span = (float(window["start_s"]), float(window["end_s"]))
            seen = "\n".join(str((row or {}).get("text", "")) for row in window.get("seen") or [])
            if (
                quote in seen
                and len(shown) <= int(window.get("max_characters") or 0)
                and not any(span[0] < b and span[1] > a for a, b in used)
            ):
                used.append(span)
                kept.append({**caption, "window_id": window["id"]})
                break
    return json.dumps({**answer, "captions": kept})


def replay(job: SimpleNamespace, research: dict) -> dict[str, Any]:
    """The real phone worker with recorded model text, through the device recipe compile.

    Returns what it saw: the plan, the variant, the compiled recipe object, and how many
    grounded searches the render ran (research that happens after approval)."""
    from app.services import phone_rollout, researched_text

    evidence = researched_text.ResearchEvidence.model_validate(research["evidence"])
    searches: list[str] = []
    recipes: list[Any] = []
    real_validate = phone_rollout.validate_phone_pilot_recipe

    def search(_self: Any, request: Any, ctx: Any = None) -> Any:  # noqa: ARG001
        searches.append(request.topic)
        return evidence

    def capture(recipe: Any, *args: Any, **kwargs: Any) -> Any:
        recipes.append(recipe)
        return real_validate(recipe, *args, **kwargs)

    sequence = _Sequence(
        [research["caption_binding_raw_text"], research["claim_review_raw_text"]], rebind=True
    )
    from app.kria import plan_blocks
    from app.tasks import generative_build as _GB

    live_blocks: list[dict[str, Any]] = []

    def record_blocks(_job_id: Any, blocks: list[dict[str, Any]]) -> None:
        # The live plan feed the creator sees, in emission order (later wins per section).
        live_blocks.extend(copy.deepcopy(blocks))

    quiet = io.StringIO()
    with (
        contextlib.redirect_stdout(quiet),
        contextlib.redirect_stderr(quiet),
        patch.object(plan_blocks, "enabled", lambda: True),
        patch.object(plan_blocks, "_emit", lambda job_id, blocks: record_blocks(job_id, blocks)),
        patch.object(plan_blocks, "emit_post_caption", lambda _job_id: None),
        patch.object(_GB, "_emit_post_caption", lambda _job_id: None),
        patch.object(researched_text.GroundedResearchAgent, "run", search),
        patch.object(researched_text, "default_client", sequence.client),
        patch.object(phone_rollout, "validate_phone_pilot_recipe", capture),
    ):
        seen = _BASE.run_post_approval(
            job,
            composer_client=_cassette(research["composer_raw_text"]),
            typography_client=_cassette(research["typography_raw_text"]),
            extra_features=("crossfade",),
        )
    plan = seen.get("assembly_plan") or {}
    variant = (plan.get("variants") or [{}])[0]
    execution = plan.get("guided_story_execution_plan") or {}
    return {
        "seen": seen,
        "error": seen.get("worker_error") or seen.get("pin_error"),
        "plan": plan,
        "variant": variant,
        "execution": execution,
        "recipe": recipes[-1] if recipes else None,
        "searches": searches,
        "live_blocks": live_blocks,
        "text_elements": [t for t in variant.get("text_elements", []) if not t.get("removed")],
        "cuts": [
            round(float(w["resolved_duration_s"]), 4) for w in execution.get("beat_windows") or []
        ],
        "order": [m.get("media_id") for m in variant.get("story_timeline") or []],
    }


# ---------------------------------------------------------------------- the approval card


def approval_card(fx: dict, job: SimpleNamespace) -> dict[str, Any]:
    """The creator-visible approval card for the approved strategy.

    Assembled from the same pure functions the draft turn calls
    (``tasks.kria_runtime._strategy_changes``, ``brief_checks.build_receipts`` /
    ``reply_from_receipts``, ``approval_readiness.assess_readiness``,
    ``drafts._sanitize_strategy_duration_provenance``, ``research_preview.research_requirement``).
    The database-bound turn body is not run; ``reproduction`` proves this assembly gives the
    card prod showed for the same inputs."""
    from app.agents._schemas.creator_agent import CreativeStrategy
    from app.kria.approval_readiness import assess_readiness, readiness_reply
    from app.kria.brief import CreativeBrief
    from app.kria.brief_checks import (
        build_receipts,
        defers_text_to_phone,
        defers_to_unified_montage,
        plan_facts_from_strategy,
        reply_from_receipts,
        with_planned_cuts,
    )
    from app.kria.drafts import _sanitize_strategy_duration_provenance
    from app.kria.route_disclosure import (
        UNIFIED_MONTAGE,
        ask_declines,
        card_disclosures,
        card_text,
        planned_card_length,
        with_route_declines,
    )
    from app.tasks.kria_runtime import _strategy_changes

    strategy = job.strategy
    man = manifest(fx)
    clip_paths = job.media["clip_paths"]
    creator_id = job.user_id
    facts = plan_facts_from_strategy(
        strategy,
        clip_ids=[c["id"] for c in fx["clips"]],
        manifest=man,
        speech_cleanup_enabled=False,
        speech_cleanup_offered=False,
    )
    facts = replace(
        facts,
        phone_text=defers_text_to_phone(creator_id=creator_id, clip_paths=clip_paths),
        phone_montage=defers_to_unified_montage(
            creator_id=creator_id,
            edit_format="montage",
            audio_strategy=strategy.get("audio_strategy"),
            clip_paths=clip_paths,
        ),
        media_snapshot=job.media,
    )
    facts = with_planned_cuts(facts, job.media)
    brief = CreativeBrief.model_validate(fx["brief"])
    checked = brief.live()
    receipts = build_receipts(checked, facts, include_unchecked=True, strict_order=True)
    # The same disclosure steps the draft turn runs (tasks.kria_runtime): asks the route does
    # not read are answered plainly, unread strategy fields and research are named.
    route = UNIFIED_MONTAGE if facts.phone_montage else None
    receipts = with_route_declines(receipts, checked, strategy, route=route)
    report = assess_readiness(
        brief,
        strategy,
        media=job.media,
        manifest=man,
        creator_request=fx["message"],
        creator_id=creator_id,
    )
    reply = reply_from_receipts(
        CreativeBrief(version=brief.version, requirements=checked),
        receipts,
        summary=fx["planner_summary"],
        notices=(),
    )
    if not report["ready"]:
        reply = readiness_reply(brief, report)
    disclosures = card_disclosures(checked, strategy, route=route)
    if disclosures:
        reply = f"{reply}\n{card_text(disclosures)}"
    arguments = SimpleNamespace(
        strategy=CreativeStrategy.model_validate(strategy), summary=fx["planner_summary"]
    )
    changes = [c for c in _strategy_changes(arguments) if c != fx["planner_summary"]]
    draft_strategy = _sanitize_strategy_duration_provenance(strategy, brief_binding=job.binding)
    shown_length = planned_card_length(draft_strategy, job.media, route=route)
    if shown_length is not None:
        draft_strategy = {**draft_strategy, "target_duration_s": shown_length}
    consequence = f"Render this draft: {reply}"
    receipt_records = [r.model_dump(mode="json") for r in receipts]
    return {
        "reply": reply,
        "changes": changes,
        "consequence_summary": consequence,
        "receipts": receipt_records,
        "ready": bool(report["ready"]),
        "not_applied": _ROUTE.card_records(disclosures, reply),
        "declines": _ROUTE.decline_records(
            ask_declines(checked, strategy, route=route), receipt_records, reply
        ),
        "draft_target_duration_s": draft_strategy.get("target_duration_s"),
        "text": "\n".join([reply, consequence, *changes, *[str(r.reason or "") for r in receipts]]),
    }


_LENGTH = re.compile(r"(\d+(?:\.\d+)?)\s*(?:s\b|sec\b|secs\b|seconds?\b)", re.I)
_RESEARCH_WORDS = re.compile(
    r"research|web\s*search|search(?:es|ed|ing)?\s+the\s+web|look(?:s|ed|ing)?\s+up|"
    r"fact[- ]check|sources?\b|google",
    re.I,
)


# ------------------------------------------------------------------------- recipe readers


def _norm_tokens(text: str) -> list[str]:
    return re.findall(r"[a-z0-9]+", text.lower())


def _font_key(name: str) -> str:
    base = re.sub(r"^font[-_]", "", str(name).rsplit("/", 1)[-1])
    base = re.sub(r"\.(ttf|otf)$", "", base, flags=re.I)
    base = re.sub(
        r"[-_ ]?(extrabold|semibold|bold|regular|medium|light|black|italic|variable)$",
        "",
        base,
        flags=re.I,
    )
    return re.sub(r"[^a-z0-9]", "", base.lower())


def text_layers(recipe: Any) -> list[dict[str, Any]]:
    rows = []
    for layer in getattr(recipe, "text_layers", None) or []:
        runs = list(getattr(layer, "runs", None) or [])
        rows.append(
            {
                "text": " ".join(str(getattr(run, "text", "")) for run in runs),
                "start_s": float(getattr(layer, "start", 0.0)),
                "end_s": float(getattr(layer, "end", 0.0)),
                "animation_phases": getattr(layer, "animation_phases", None),
                "fonts": sorted({_font_key(getattr(r, "font_asset_id", "")) for r in runs}),
            }
        )
    return rows


# ------------------------------------------------------------------------- verdict logic
#
# Pure predicates. The checks feed them the real outputs; the negative controls feed them
# synthetic outputs that ignore or honour the ask.


def animation_verdict(
    layers: list[dict[str, Any]], elements: list[dict[str, Any]], receipts: list[dict[str, Any]]
) -> dict[str, Any]:
    animated_layers = [i for i, layer in enumerate(layers) if layer.get("animation_phases")]
    animated_elements = [
        i
        for i, el in enumerate(elements)
        if str(el.get("effect") or "static") != "static" or el.get("animation_phases")
    ]
    declined = [
        r
        for r in receipts
        if r.get("status") == "not_possible"
        and str(r.get("reason") or "").strip()
        and re.search(r"animat", str(r["reason"]), re.I)
    ]
    applied = bool(animated_layers or animated_elements)
    return {
        "pass": applied or bool(declined),
        "applied": applied,
        "declined_before_approval": bool(declined),
        "animated_recipe_text_layers": len(animated_layers),
        "animated_text_elements": len(animated_elements),
        "text_layers": len(layers),
        "effects_seen": sorted({str(el.get("effect")) for el in elements}),
        "r1_receipt": [
            {
                "status": r.get("status"),
                "verification": r.get("verification"),
                "reason": r.get("reason"),
            }
            for r in receipts
        ],
    }


def pace_verdict(cases: dict[str, dict[str, Any]]) -> dict[str, Any]:
    """cases: relaxed, fast (same strategy but pacing) and recorded (text_explainer, balanced)."""

    def mean(case: dict) -> float | None:
        cuts = case.get("cuts") or []
        return sum(cuts) / len(cuts) if cuts else None

    relaxed, fast, recorded = (mean(cases[k]) for k in ("relaxed", "fast", "recorded"))
    longer = bool(relaxed and fast and relaxed >= fast * 1.10)
    pace_ok = (
        cases["relaxed"].get("pace") == "relaxed"
        and cases["recorded"].get("pace") == "balanced"
        and cases["fast"].get("pace") == "fast"
    )
    direction_ok = cases["recorded"].get("direction") == "text_explainer"
    return {
        "pass": bool(longer and pace_ok and direction_ok),
        "relaxed_cuts_longer_than_fast": longer,
        "snapshot_pace_follows_strategy": pace_ok,
        "snapshot_direction_follows_strategy": direction_ok,
        "mean_cut_s": {
            "relaxed": relaxed and round(relaxed, 4),
            "fast": fast and round(fast, 4),
            "balanced": recorded and round(recorded, 4),
        },
        "snapshot": {
            k: {"direction": v.get("direction"), "pace": v.get("pace"), "cuts": len(v["cuts"])}
            for k, v in cases.items()
        },
    }


# ------------------------------------------------------------------------------ the checks


def _base_run(fx: dict, rs: dict) -> dict[str, Any]:
    job = build_job(fx)
    run = replay(job, rs)
    run["job"] = job
    run["card"] = approval_card(fx, job)
    return run


def check_1_animation(fx: dict, base: dict) -> dict:
    verdict = animation_verdict(
        text_layers(base["recipe"]), base["text_elements"], base["card"]["receipts"]
    )
    why = []
    if not verdict["pass"]:
        why.append(
            "the creator asked for animated text: every rendered text is static "
            f"(effects {verdict['effects_seen']}, animated recipe layers "
            f"{verdict['animated_recipe_text_layers']} of {verdict['text_layers']}) and the "
            f"approval receipt for r1 is {verdict['r1_receipt']}, not a typed decline"
        )
    return _result(
        1,
        "animation: applied to the rendered text or declined before approval",
        verdict["pass"],
        why,
        verdict,
    )


def check_2_pace_direction(fx: dict, base: dict, rs: dict) -> dict:
    cases = {}
    for name, pacing in (("relaxed", "relaxed"), ("fast", "fast")):
        strategy = copy.deepcopy(fx["strategy"])
        strategy["pacing"] = pacing
        cases[name] = replay(build_job(fx, strategy=strategy), rs)
    cases["recorded"] = base
    flat = {
        k: {
            "cuts": v["cuts"],
            "pace": v["execution"].get("pace"),
            "direction": v["execution"].get("direction"),
        }
        for k, v in cases.items()
    }
    verdict = pace_verdict(flat)
    verdict["strategy"] = {
        "direction": fx["strategy"]["direction"],
        "pacing": fx["strategy"]["pacing"],
    }
    why = []
    if not verdict["pass"]:
        why.append(
            f"strategy pacing relaxed/fast/balanced gave mean cuts {verdict['mean_cut_s']} "
            f"and snapshot {verdict['snapshot']}: pace and direction are constants "
            "(unified_montage hard-codes direction='fast_montage', pace='fast')"
        )
    return _result(
        2, "pace and direction come from the strategy, not constants", verdict["pass"], why, verdict
    )


def _hook_overlap(hook: str, text: str) -> float:
    wanted = set(_norm_tokens(hook))
    return len(wanted & set(_norm_tokens(text))) / len(wanted) if wanted else 1.0


def check_3_hook(fx: dict, base: dict) -> dict:
    hook = fx["strategy"]["intro_hook"]
    texts = [t["text"] for t in base["text_elements"]] + [
        x["text"] for x in text_layers(base["recipe"])
    ]
    best = max((_hook_overlap(hook, t) for t in texts), default=0.0)
    rendered = best >= 0.75
    declined = _ROUTE.field_disclosed(base["card"], "intro_hook")
    ok = rendered or declined
    return _result(
        3,
        "hook: intro_hook rendered or declined with a reason before approval",
        ok,
        []
        if ok
        else [
            f"intro_hook {hook!r} appears in no rendered text (best word overlap "
            f"{best:.2f}; texts {texts[:4]}) and the card does not say it is dropped"
        ],
        {
            "intro_hook": hook,
            "best_overlap": round(best, 2),
            "rendered": rendered,
            "declined_on_card": declined,
            "rendered_texts": texts[:4],
        },
    )


def check_4_story(fx: dict, base: dict, rs: dict) -> dict:
    strategy = copy.deepcopy(fx["strategy"])
    strategy["story_structure"] = list(reversed(strategy["story_structure"]))
    other = replay(build_job(fx, strategy=strategy), rs)
    applied = base["order"] != other["order"] or base["cuts"] != other["cuts"]
    declined = _ROUTE.field_disclosed(base["card"], "story_structure")
    ok = applied or declined
    return _result(
        4,
        "story order: story_structure applied or declined/disclosed",
        ok,
        []
        if ok
        else [
            "reversing story_structure leaves the story order and cuts unchanged "
            "(nothing reads it) and the card does not say it is dropped"
        ],
        {
            "story_structure": fx["strategy"]["story_structure"],
            "order_changes_with_structure": applied,
            "declined_on_card": declined,
            "order_head": base["order"][:3],
        },
    )


def check_5_length(fx: dict, base: dict) -> dict:
    recipe_s = float(base["execution"].get("resolved_duration_s") or 0)
    shown = [("card_text", float(m)) for m in _LENGTH.findall(base["card"]["text"])]
    target = base["card"]["draft_target_duration_s"]
    if isinstance(target, int | float) and target > 0:
        shown.append(("draft_strategy.target_duration_s", float(target)))
    off = [(src, v) for src, v in shown if abs(v - recipe_s) > 0.05]
    ok = not off
    return _result(
        5,
        "length: every length shown before approval equals the recipe length",
        ok,
        [] if ok else [f"the creator is shown {off} but the recipe is {recipe_s:.3f} s"],
        {
            "recipe_length_s": round(recipe_s, 3),
            "lengths_shown": shown,
            "length_owner_in_prod": fx["prod_outcome"]["length_owners"],
        },
    )


def check_6_creator_selected(fx: dict, base: dict, rs: dict) -> dict:
    default = server_default_shape(fx)
    echo = replay(build_job(fx, shape=default), rs)
    pick = {"output_orientation": "portrait", "landscape_fit": "fit"}
    # An explicit pick as the approval stage records it (the origin stamp rides the shape).
    explicit = replay(build_job(fx, shape={**pick, "origin": "creator"}), rs)
    echo_reason = echo["execution"].get("output_orientation_reason")
    pick_reason = explicit["execution"].get("output_orientation_reason")
    ok = echo_reason != CREATOR_SELECTED_REASON and pick_reason == CREATOR_SELECTED_REASON
    return _result(
        6,
        '"creator selected" is stamped only for an explicit pick, not an echoed default',
        ok,
        []
        if ok
        else [
            f"the app echoed the server's preselected shape {default} (the creator "
            f"picked nothing) and the plan says {echo_reason!r}; an explicit pick says "
            f"{pick_reason!r}"
        ],
        {
            "server_default_shape": default,
            "echoed_default_reason": echo_reason,
            "explicit_portrait_pick_reason": pick_reason,
            "recorded_prod_reason": fx["prod_outcome"]["recipe"]["output_orientation_reason"],
            "note": "the worker tells an echo from a pick by the origin stamp inside the "
            "stored shape (written at approval); a shape without it reads as an echoed "
            "default, and an explicit pick must still be labelled as the creator's",
        },
    )


def check_7_research_disclosed(fx: dict, base: dict) -> dict:
    ran = bool(base["searches"])
    disclosed = bool(_RESEARCH_WORDS.search(base["card"]["text"]))
    ok = (not ran) or disclosed
    return _result(
        7,
        "research that runs after approval is disclosed on the approval card",
        ok,
        []
        if ok
        else [
            f"the render ran {len(base['searches'])} web search(es) for "
            f"{base['searches']} after approval; the card text never says so"
        ],
        {
            "searches_after_approval": base["searches"],
            "card_discloses_research": disclosed,
            "card_text": base["card"]["text"],
        },
    )


def check_8_look_chip(fx: dict, base: dict) -> dict:
    from app.kria import plan_blocks
    from app.tasks import generative_build as gb

    # What the creator is shown is the LAST look block on the live plan feed. The first one
    # is built from the pinned plan before the phone typography step (the replay stubs that
    # step out, so it is rebuilt here with the worker's own helper); the typography step
    # then emits the final one, which `replay` recorded.
    with (
        patch.object(plan_blocks, "enabled", lambda: True),
        patch.object(gb, "current_phone_text_policy", lambda: {"active": True}),
    ):
        early = plan_blocks.blocks_from_guided_plan(base["execution"], **gb._early_look_font())
    looks = [b for b in [*early, *base["live_blocks"]] if b.get("section_id") == "look"]
    look = looks[-1]
    chips = (look.get("payload") or {}).get("chips") or []
    chip_fonts = [
        _font_key(re.sub(r"\s+titles$", "", c, flags=re.I))
        for c in chips
        if re.search(r"titles$", c, re.I)
    ]
    burned = sorted(
        {_font_key(t.get("font_family") or "") for t in base["text_elements"]}
        | {f for layer in text_layers(base["recipe"]) for f in layer["fonts"]}
    )
    ok = all(any(c == b or c in b or b in c for b in burned if b) for c in chip_fonts)
    return _result(
        8,
        "look chip font equals the burned font",
        ok,
        [] if ok else [f"the look chip says {chips} but the text is burned in {burned}"],
        {
            "look_chips": chips,
            "chip_fonts": chip_fonts,
            "burned_fonts": burned,
            "recorded_prod_chips": fx["prod_outcome"]["plan_blocks"]["look_chips"],
        },
    )


def followup_turn(fx: dict) -> dict[str, Any]:
    """Turn 2 ("Animate the title" chip) through the real ``plan_live_turn`` with the brief
    extractor/copilot provider failing the way prod's did (TerminalError)."""
    from app.agents._runtime import TerminalError
    from app.kria import planner
    from app.kria.brief import CreativeBrief

    item = SimpleNamespace(
        id=uuid.uuid4(),
        content_plan_id=uuid.uuid4(),
        current_job_id=uuid.uuid4(),
        edit_format="montage",
        clip_gcs_paths=[r["gcs_path"] for r in media_rows(fx)],
        clip_assignments=media_rows(fx),
    )
    creator_id = uuid.uuid4()
    from app.models import ContentPlan, CreationThread, Job, Persona, PlanItem

    plan_row = SimpleNamespace(id=item.content_plan_id, user_id=creator_id, persona_id=uuid.uuid4())

    async def get(model, _identifier, **_kwargs):  # noqa: ANN001, ANN202
        return {
            PlanItem: item,
            ContentPlan: plan_row,
            Persona: SimpleNamespace(user_id=creator_id),
            CreationThread: SimpleNamespace(active_creator_agent_session_id=None),
            Job: SimpleNamespace(status="variants_ready"),
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
    brief = CreativeBrief.model_validate(fx["brief"])

    class _FailingExtractor:
        def __init__(self, _client: Any) -> None:
            pass

        def run(self, *_a: Any, **_k: Any) -> Any:
            raise TerminalError("terminal_transient: provider failed after 2 attempts, 0 tokens")

    target = SimpleNamespace(
        job_id=item.current_job_id,
        snapshot={"text_bars": [], "allowed_op_families": ["text", "clip"]},
        conversation=[],
    )
    patches = [
        patch.object(planner, "_load_thread_events", AsyncMock(return_value=[])),
        patch.object(planner, "load_latest_brief", AsyncMock(return_value=brief)),
        patch.object(
            planner, "resolve_item_creator_context", AsyncMock(return_value=(manifest(fx), []))
        ),
        patch.object(planner, "creator_context", lambda *_a: ("creator", "item")),
        patch.object(planner, "_load_editor_target", AsyncMock(return_value=target)),
        patch.object(planner, "_plan_editor_revision", AsyncMock(return_value=None)),
        patch.object(planner, "BriefExtractorAgent", _FailingExtractor),
        patch.object(planner, "default_client", lambda: object()),
    ]
    quiet = io.StringIO()
    with (
        contextlib.ExitStack() as stack,
        contextlib.redirect_stdout(quiet),
        contextlib.redirect_stderr(quiet),
    ):
        for p in patches:
            stack.enter_context(p)
        planned = asyncio.run(
            planner.plan_live_turn(
                db,
                thread_id=uuid.uuid4(),
                item_id=item.id,
                creator_id=creator_id,
                user_message="Animate the title",
            )
        )
    return {
        "response": planned.plan.response,
        "mode": planned.plan.mode,
        "reason": (planned.brief_coverage or {}).get("reason"),
        "cause": (planned.brief_coverage or {}).get("cause"),
    }


def check_9_provider_failure(fx: dict) -> dict:
    turn = followup_turn(fx)
    text = str(turn["response"] or "")
    cause = turn.get("cause") or {}
    provider_fault = cause.get("cause_type") == "TerminalError"
    unreadable = "reliably read" in text or turn["reason"] == "request_extraction_failed"
    ok = not (provider_fault and unreadable)
    return _result(
        9,
        "a provider failure on a follow-up is not reported as an unreadable request",
        ok,
        []
        if ok
        else [
            f"the provider failed ({cause}) and the creator was told {text!r} "
            f"(coverage reason {turn['reason']!r}), asking them to rephrase"
        ],
        {
            **turn,
            "recorded_prod": {
                "reply": fx["prod_outcome"]["turn2"]["reply"],
                "brief_coverage": fx["prod_outcome"]["turn2"]["brief_coverage"],
                "copilot_outcome": fx["prod_outcome"]["turn2"]["copilot_run"]["outcome"],
            },
        },
    )


def consumer_gate(
    card: dict,
    extra_fields: tuple[str, ...] = (),
    extra_handling: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """The unified montage's registry table against its code and the card (`score_route`)."""
    return _ROUTE.consumer_gate(
        "unified_montage", card, extra_fields=extra_fields, extra_handling=extra_handling
    )


def check_12_registry_claims(base: dict) -> dict:
    claims = _ROUTE.claims_result("unified_montage", base["card"])
    return {**claims, "id": 12}


def check_10_consumer_gate(base: dict) -> dict:
    gate = consumer_gate(base["card"])
    result = _result(
        10,
        "consumer gate (REPORT ONLY): fields the route ignores and the card does not disclose",
        gate["pass"],
        []
        if gate["pass"]
        else [
            *(
                [
                    f"the unified-montage table marks {gate['claimed_applied_unread']} applied, "
                    "but no reader in the route's code touches them"
                ]
                if gate["claimed_applied_unread"]
                else []
            ),
            *(
                [
                    f"the unified-montage route reads none of {gate['undisclosed']} "
                    "and the approval card does not say they are not applied"
                ]
                if gate["undisclosed"]
                else []
            ),
        ],
        gate,
    )
    result["report_only"] = True
    result["verdict"] = "REPORT (clean)" if gate["pass"] else "REPORT (gaps)"
    return result


def check_11_song_rule(fx: dict, base: dict) -> dict:
    recipe, variant, plan = base["recipe"], base["variant"], base["execution"]
    audio = getattr(recipe, "audio", None)
    kinds = sorted(
        {str(getattr(a, "kind", type(a).__name__)) for a in recipe.asset_manifest.assets}
    )
    audio_tracks = [t.id for t in recipe.tracks if getattr(t, "kind", None) == "audio"]
    uploaded_song = bool(base["job"].all_candidates.get("user_song"))
    observed = {
        "creator_uploaded_song": uploaded_song,
        "recipe_music_asset_id": getattr(audio, "music_asset_id", None),
        "audio_tracks": audio_tracks,
        "variant_music_track_id": variant.get("music_track_id"),
        "variant_music_playback_mode": variant.get("music_playback_mode"),
        "execution_plan_music": plan.get("music"),
        "asset_kinds": kinds,
        "frozen_catalog_section_in_strategy": bool(fx["strategy"].get("selected_music_section")),
    }
    ok = bool(
        not uploaded_song
        and observed["recipe_music_asset_id"] is None
        and not audio_tracks
        and observed["variant_music_track_id"] is None
        and observed["variant_music_playback_mode"] in {None, "none", "reference_only"}
        and observed["execution_plan_music"] is None
    )
    return _result(
        11,
        "song rule: no catalog music is mixed on the phone route (control)",
        ok,
        []
        if ok
        else ["catalog music reaches the phone recipe although the creator uploaded no song"],
        observed,
    )


# ------------------------------------------------------------------------ negative controls


def negative_controls() -> list[dict]:
    def row(name, expect_pass, got, detail):  # noqa: ANN001, ANN202
        return {
            "name": name,
            "expected_pass": expect_pass,
            "got_pass": got,
            "ok": expect_pass == got,
            "detail": detail,
        }

    static = [{"effect": "static", "animation_phases": None}] * 3
    moving = [{"effect": "typewriter", "animation_phases": {"entrance": "rise"}}] * 3
    undecided = [
        {
            "status": "partial",
            "verification": "unchecked",
            "reason": "I can't verify this one automatically yet.",
        }
    ]
    declined = [
        {
            "status": "not_possible",
            "verification": "checked",
            "reason": "Animated text isn't available on this build, so the text stays still.",
        }
    ]
    controls = [
        row(
            "1 ignores the ask (static text, undecided receipt) must FAIL",
            False,
            animation_verdict([{"animation_phases": None}] * 3, static, undecided)["pass"],
            "",
        ),
        row(
            "1 honours the ask (animated text) must PASS",
            True,
            animation_verdict([{"animation_phases": {"entrance": "rise"}}] * 3, moving, undecided)[
                "pass"
            ],
            "",
        ),
        row(
            "1 honours the ask (typed decline before approval) must PASS",
            True,
            animation_verdict([{"animation_phases": None}] * 3, static, declined)["pass"],
            "",
        ),
    ]
    flat = {"cuts": [1.127] * 16, "pace": "fast", "direction": "fast_montage"}
    ignoring = {"relaxed": flat, "fast": flat, "recorded": flat}
    honouring = {
        "relaxed": {"cuts": [2.0] * 9, "pace": "relaxed", "direction": "text_explainer"},
        "fast": {"cuts": [0.8] * 22, "pace": "fast", "direction": "text_explainer"},
        "recorded": {"cuts": [1.3] * 14, "pace": "balanced", "direction": "text_explainer"},
    }
    controls += [
        row(
            "2 ignores the strategy (constant cuts, fast_montage) must FAIL",
            False,
            pace_verdict(ignoring)["pass"],
            "",
        ),
        row("2 honours the strategy must PASS", True, pace_verdict(honouring)["pass"], ""),
    ]
    card = {"not_applied": [], "declines": []}
    gate = consumer_gate(card, extra_fields=("zz_new_unconsumed_field",))
    controls.append(
        row(
            "10 a new unconsumed field must be flagged",
            False,
            "zz_new_unconsumed_field" not in gate["undisclosed"],
            gate["undisclosed"][-1:],
        )
    )
    from app.kria.route_capabilities import Applied

    gate = consumer_gate(
        card,
        extra_fields=("zz_claimed_field",),
        extra_handling={"zz_claimed_field": Applied()},
    )
    controls.append(
        row(
            "10 an applied claim with no reader must be flagged",
            False,
            "zz_claimed_field" not in gate["claimed_applied_unread"],
            gate["claimed_applied_unread"],
        )
    )
    return controls


def hold_controls(fx: dict) -> list[dict]:
    """Asks the draft check can still fix before approval must stay held on the unified route.

    A conversion from held to approvable is wrong when the worker would refuse the render
    after approval (a quoted text missing from the plan, a wrong order) or when the length
    evidence is missing (never a pass). A vague ask nothing can check is the approvable control.
    Each row builds the Assos draft with one requirement swapped in and reads approval readiness."""

    def ready(job, req) -> tuple[bool, str]:  # noqa: ANN001
        from app.kria.approval_readiness import assess_readiness
        from app.kria.brief import CreativeBrief

        report = assess_readiness(
            CreativeBrief(version=1, requirements=[req]),
            job.strategy,
            media=job.media,
            manifest=manifest(fx),
            creator_request="x",
            creator_id=job.user_id,
        )
        return bool(report["ready"]), report["requirements"][0]["disposition"]

    def req(**kw):  # noqa: ANN003, ANN202
        from app.kria.brief import BriefRequirement

        base = {"id": "r1", "kind": "text", "scope": "global", "description": "x", "facts": {}}
        return BriefRequirement(**{**base, **kw})

    with _prod_flags():
        job = build_job(fx)
        no_length = copy.deepcopy(job)
        no_length.media = {k: v for k, v in job.media.items() if k != "length_plan"}
        cases = [
            (
                "a length ask with no length evidence stays held",
                no_length,
                req(kind="timing", description="make it 8 seconds", facts={"duration_s": 8}),
                False,
            ),
            (
                "a vague ask nothing can check is approvable with a look-after line",
                job,
                req(kind="style", description="make it feel dreamy"),
                True,
            ),
        ]
        rows = []
        # The Assos draft never reaches `evidence_missing` for a quoted text (its phone text is
        # executable or a choice), so the conversion step itself is fed the held row: a quoted
        # text and an order ask the worker would refuse after approval must come back unchanged.
        from app.kria.approval_readiness import _approve_undecidable

        for name, kind in (
            ("a quoted text missing from the draft stays held", "text"),
            ("an order ask with no proof stays held", "order"),
        ):
            requirement = req(kind=kind, description="x", literal="Zzyzx Qvortle")
            held = [
                {
                    "requirement_id": "r1",
                    "disposition": "evidence_missing",
                    "reason": "held",
                    "evidence_owner": "brief_checks",
                }
            ]
            got = _approve_undecidable(held, [requirement])[0]["disposition"]
            rows.append(
                {
                    "name": f"hold: {name}",
                    "expected_pass": False,
                    "got_pass": got != "evidence_missing",
                    "ok": got == "evidence_missing",
                    "detail": got,
                }
            )
        for name, case_job, requirement, expect in cases:
            got, disposition = ready(case_job, requirement)
            rows.append(
                {
                    "name": f"hold: {name}",
                    "expected_pass": expect,
                    "got_pass": got,
                    "ok": expect == got,
                    "detail": disposition,
                }
            )
    return rows


# --------------------------------------------------------------------------- reproduction


def reproduction(fx: dict, base: dict) -> list[dict]:
    """The replay of the unchanged code reproduces what prod recorded, so the red checks are
    about the code and not about a drifted fixture. Informational: a later phase that changes
    the output on purpose changes these rows too."""
    prod = fx["prod_outcome"]
    recipe = prod["recipe"]
    card = prod["approval_card"]
    default = server_default_shape(fx)

    def entry(name, same, ours, theirs):  # noqa: ANN001, ANN202
        return {"name": name, "reproduced": bool(same), "replay": ours, "prod": theirs}

    return [
        entry(
            "recipe cut lengths",
            base["cuts"] == recipe["cut_lengths_s"],
            base["cuts"][:3] + ["..."],
            recipe["cut_lengths_s"][:3] + ["..."],
        ),
        entry(
            "recipe duration",
            abs(base["execution"].get("resolved_duration_s", 0) - recipe["duration_s"]) < 0.01,
            base["execution"].get("resolved_duration_s"),
            recipe["duration_s"],
        ),
        entry(
            "direction and pace constants",
            (base["execution"].get("direction"), base["execution"].get("pace"))
            == (recipe["direction"], recipe["pace"]),
            [base["execution"].get("direction"), base["execution"].get("pace")],
            [recipe["direction"], recipe["pace"]],
        ),
        entry(
            "researched caption texts",
            [t["text"] for t in base["text_elements"]]
            == [t["text"] for t in recipe["text_elements"]],
            [t["text"] for t in base["text_elements"]],
            [t["text"] for t in recipe["text_elements"]],
        ),
        entry(
            "text effects",
            {t["effect"] for t in base["text_elements"]}
            == {t["effect"] for t in recipe["text_elements"]},
            sorted({t["effect"] for t in base["text_elements"]}),
            sorted({t["effect"] for t in recipe["text_elements"]}),
        ),
        entry(
            "burned font",
            {t["font_family"] for t in base["text_elements"]} == {recipe["burned_font"]},
            sorted({t["font_family"] for t in base["text_elements"]}),
            recipe["burned_font"],
        ),
        entry(
            "typography font in plan",
            base["execution"].get("typography", {}).get("font")
            == recipe["typography_font_in_plan"],
            base["execution"].get("typography", {}).get("font"),
            recipe["typography_font_in_plan"],
        ),
        entry(
            "approval card text",
            base["card"]["reply"] == card["draft_applied_content"],
            base["card"]["reply"],
            card["draft_applied_content"],
        ),
        entry(
            "approval card changes",
            base["card"]["changes"] == card["changes"],
            base["card"]["changes"],
            card["changes"],
        ),
        entry(
            "approval consequence summary",
            base["card"]["consequence_summary"] == card["consequence_summary"],
            base["card"]["consequence_summary"][:60],
            card["consequence_summary"][:60],
        ),
        entry(
            "r1 receipt",
            [(r["status"], r["verification"], r["reason"]) for r in base["card"]["receipts"]]
            == [
                (r["status"], r["verification"], r["reason"]) for r in card["requirement_receipts"]
            ],
            [(r["status"], r["verification"]) for r in base["card"]["receipts"]],
            [(r["status"], r["verification"]) for r in card["requirement_receipts"]],
        ),
        entry(
            "server default render shape",
            default == fx["render_shape"],
            default,
            fx["render_shape"],
        ),
        entry(
            "orientation reason stamp",
            base["execution"].get("output_orientation_reason")
            == recipe["output_orientation_reason"],
            base["execution"].get("output_orientation_reason"),
            recipe["output_orientation_reason"],
        ),
        entry(
            "no music on the phone",
            base["variant"].get("music_track_id") == recipe["music_track_id"]
            and base["variant"].get("music_playback_mode") == recipe["music_playback_mode"],
            [base["variant"].get("music_track_id"), base["variant"].get("music_playback_mode")],
            [recipe["music_track_id"], recipe["music_playback_mode"]],
        ),
    ]


# ----------------------------------------------------------------------------------- report


def run_offline(fixture_dir: Path = FIXTURE_DIR) -> dict:
    fx, rs = load_fixture(fixture_dir), load_research(fixture_dir)
    with _prod_flags():
        base_holder: dict[str, Any] = {}

        def base() -> dict:
            if "run" not in base_holder:
                base_holder["run"] = _base_run(fx, rs)
                if base_holder["run"]["error"]:
                    raise RuntimeError(
                        f"the worker did not reach the device pin: {base_holder['run']['error']}"
                    )
            return base_holder["run"]

        checks = [
            _guarded(1, "animation", lambda: check_1_animation(fx, base())),
            _guarded(2, "pace and direction", lambda: check_2_pace_direction(fx, base(), rs)),
            _guarded(3, "hook", lambda: check_3_hook(fx, base())),
            _guarded(4, "story order", lambda: check_4_story(fx, base(), rs)),
            _guarded(5, "length", lambda: check_5_length(fx, base())),
            _guarded(6, "creator selected", lambda: check_6_creator_selected(fx, base(), rs)),
            _guarded(7, "research disclosed", lambda: check_7_research_disclosed(fx, base())),
            _guarded(8, "look chip font", lambda: check_8_look_chip(fx, base())),
            _guarded(9, "provider failure", lambda: check_9_provider_failure(fx)),
            {
                **_guarded(10, "consumer gate", lambda: check_10_consumer_gate(base())),
                "report_only": True,
            },
            _guarded(11, "song rule", lambda: check_11_song_rule(fx, base())),
            _guarded(12, "registry claims", lambda: check_12_registry_claims(base())),
        ]
        try:
            repro = reproduction(fx, base())
        except Exception as exc:  # noqa: BLE001 - reported, never green
            repro = [
                {
                    "name": "reproduction",
                    "reproduced": False,
                    "error": f"{type(exc).__name__}: {exc}",
                }
            ]
    controls = negative_controls() + hold_controls(fx)
    scored = [c for c in checks if not c.get("report_only")]
    return {
        "fixture": fx["fixture_id"],
        "mode": "offline",
        "source": {
            "code_under_audit": "origin/main "
            + fx["source"]["what"].rsplit(" ", 1)[-1].rstrip("."),
            "recorded": fx["source"]["recorded"],
            "reconstructed": fx["source"]["reconstructed"],
        },
        "network_attempts_blocked": len(_BASE._NET_ATTEMPTS),
        "passed": sum(1 for c in scored if c["pass"]),
        "failed": sum(1 for c in scored if not c["pass"]),
        "all_green": all(c["pass"] for c in scored),
        "report_only": [c["id"] for c in checks if c.get("report_only")],
        "controls_ok": all(c["ok"] for c in controls),
        "negative_controls": controls,
        "reproduction": repro,
        "checks": checks,
    }


def summary(report: dict) -> str:
    out = [
        f"Assos scorecard (offline, {report['network_attempts_blocked']} network attempts blocked)",
        f"code: {report['source']['code_under_audit']}",
        "",
    ]
    out.append(f"{'#':<3} {'verdict':<13} check")
    for c in report["checks"]:
        out.append(f"{str(c['id']):<3} {c['verdict']:<13} {c['name']}")
        for line in c["why_it_fails"]:
            out.append(f"      - {line}")
    out += [
        "",
        f"{report['passed']} verified, {report['failed']} not verified "
        f"(check(s) {report['report_only']} are report-only and never fail anything)",
        "",
        "negative controls (each must behave as stated):",
    ]
    for c in report["negative_controls"]:
        out.append(f"  {'ok ' if c['ok'] else 'BAD'} {c['name']}")
    out += ["", "baseline reproduction of prod (informational):"]
    for r in report["reproduction"]:
        out.append(f"  {'same' if r['reproduced'] else 'DIFF'} {r['name']}")
    if not report["controls_ok"]:
        out.append("\nSCORECARD INVALID: a negative control did not behave")
    return "\n".join(out)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument(
        "--out", type=Path, help="JSON report path (default test-results/assos-replay/report.json)"
    )
    parser.add_argument(
        "--json", action="store_true", help="print the full JSON instead of the summary"
    )
    parser.add_argument("--strict", action="store_true", help="exit 1 unless every check passes")
    parser.add_argument(
        "--fixture-dir", type=Path, default=FIXTURE_DIR, help="a capture of the same shape"
    )
    args = parser.parse_args(argv)
    _BASE._bootstrap()
    report = run_offline(args.fixture_dir)
    out = args.out or REPO / "test-results" / "assos-replay" / "report.json"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(
        json.dumps(report, ensure_ascii=False, indent=2, default=str) + "\n", encoding="utf-8"
    )
    text = summary(report)
    (out.parent / "summary.txt").write_text(text + "\n", encoding="utf-8")
    print(json.dumps(report, ensure_ascii=False, indent=2, default=str) if args.json else text)
    print(f"\nreport: {out}", file=sys.stderr)
    if not report["controls_ok"]:
        return 2
    return 1 if args.strict and not report["all_green"] else 0


if __name__ == "__main__":
    raise SystemExit(main())
