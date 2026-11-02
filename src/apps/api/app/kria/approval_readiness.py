"""Server-owned, revision-bound evidence for offering render approval (KRI-721).

This assesses executable commitments, not completed output. Actual export
verification must still pass independently. Unknown is never an executable pass.
"""

from __future__ import annotations

import dataclasses
import hashlib
import json
from collections.abc import Mapping, Sequence
from types import SimpleNamespace
from typing import Any

from pydantic import BaseModel

from app.agents._schemas.creator_agent import CreativeStrategy, ResolvedCreatorManifest
from app.config import settings
from app.kria.brief import CreativeBrief
from app.kria.brief_binding import media_identity, snapshot_media
from app.kria.brief_checks import (
    _has_checker,
    _wants_source_sound_kept,
    build_receipts,
    defers_caption_words_to_phone_render,
    defers_text_to_phone,
    plan_facts_from_editor_payload,
    plan_facts_from_strategy,
)
from app.kria.contracts import RequirementReceipt
from app.kria.order_replacement_receipts import with_verified_order_execution
from app.kria.order_replacement_sources import OrderReplacementSourceContext
from app.kria.reply_language import say
from app.kria.route_disclosure import ask_declines, draft_route
from app.kria.strategy_policy import RefusedStrategy, check_strategy_for_runtime_v2
from app.models import ContentPlan, Persona
from app.services.clip_order_sequence import stated_anchors
from app.services.creator_render_contract import (
    ADAPTER_DECLARATIONS,
    CREATOR_LIST_ORDER_BASIS,
    FIELD_MATRIX,
    _pin_not_found,
    build_render_contract,
    field_matrix_for_strategy,
)
from app.services.creator_sessions import resolve_item_creator_context
from app.services.length_planning import read_length_plan

READINESS_KEY = "approval_readiness"
NOT_READY = "approval_requirements_not_ready"
# The dispositions approval may go ahead with. KRI-983: `unverified` is an ask nothing can
# settle before the video is made; it is named for the creator to look at after render.
# `declined`: the route that renders this draft cannot do the ask; the draft says so plainly
# (`route_disclosure`), and the creator approves the closest thing, never a dead end.
APPROVABLE = frozenset({"executable", "delegated", "unverified", "declined"})


def _json(value: Any) -> Any:
    if isinstance(value, BaseModel):
        return value.model_dump(mode="json")
    if dataclasses.is_dataclass(value):
        return _json(dataclasses.asdict(value))
    if isinstance(value, Mapping):
        return {str(k): _json(v) for k, v in value.items()}
    if isinstance(value, (set, frozenset)):
        return sorted(_json(v) for v in value)
    if isinstance(value, (list, tuple)):
        return [_json(v) for v in value]
    return value


def digest(value: Any) -> str:
    return hashlib.sha256(
        json.dumps(
            _json(value),
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=False,
        ).encode()
    ).hexdigest()


class StrategyItemView:
    """Resolve the capabilities of the proposed format without mutating the item."""

    def __init__(self, item: Any, strategy: Mapping[str, Any]):
        self._item = item
        self.edit_format = strategy.get("edit_format") or item.edit_format
        from app.agents._schemas.creator_agent import CreativeStrategy  # noqa: PLC0415
        from app.services.speech_cleanup_decision import resolve_next_audio_mode  # noqa: PLC0415

        self.audio_mode = (
            resolve_next_audio_mode(CreativeStrategy.model_validate(strategy), item)
            or item.audio_mode
        )

    def __getattr__(self, name: str) -> Any:
        return getattr(self._item, name)


async def resolve_readiness_manifest(db, item, strategy):  # noqa: ANN001, ANN201
    plan = await db.get(ContentPlan, item.content_plan_id)
    persona = await db.get(Persona, plan.persona_id) if plan is not None else None
    if persona is None:
        raise ValueError("The edit's capability context is unavailable")
    manifest, _ = await resolve_item_creator_context(
        db,
        StrategyItemView(item, strategy) if strategy else item,
        persona=persona,
        guided_capability_enabled=True,
        runs_guided_voiceover=False,
    )
    return manifest


# The sentence the apps send for a media-only send and for the "Let Kria cut the first version"
# chip. It states no requirement by construction: the creator delegated every choice, so an
# absent brief is the true record, not missing evidence (KRI-774).
_OPEN_REQUESTS = frozenset({"suggest an edit"})
# BriefBinding.create wraps the pinned request in this heading (and repeats the latest
# message), so the approval-time recheck sees the same text through it.
_BINDING_HEADING = "Full creator request (chronological):"


def _normalized(line: str) -> str:
    folded = "".join(ch if ch.isalnum() or ch.isspace() else " " for ch in line.casefold())
    return " ".join(folded.split())


def is_open_request(text: str | None) -> bool:
    """True only when every line is the stock open-request sentence, never prose containing it."""
    lines = [
        line for line in (text or "").replace(_BINDING_HEADING, "").splitlines() if line.strip()
    ]
    return bool(lines) and all(_normalized(line) in _OPEN_REQUESTS for line in lines)


def _phone_route(
    contract: Any,
    strategy: Mapping[str, Any],
    *,
    media: Mapping[str, Any],
    edit_format: object,
    creator_id: object,
) -> str | None:
    """The phone route dispatch would stamp on this draft (`render_route`), else None."""
    from app.kria.brief_checks import defers_to_unified_montage  # noqa: PLC0415
    from app.services.creator_render_contract import PLAN_AUTHORITY_FIELD  # noqa: PLC0415
    from app.services.render_route import (  # noqa: PLC0415
        resolve_route,
        route_capabilities_from_settings,
        route_inputs_from_job,
    )

    clip_paths = [str(path) for path in media.get("clip_paths") or []]
    if not defers_to_unified_montage(
        creator_id=creator_id,
        edit_format=edit_format,
        audio_strategy=strategy.get("audio_strategy"),
        clip_paths=clip_paths,
    ):
        return None
    candidates: dict[str, Any] = {
        "clip_paths": clip_paths,
        "edit_format": edit_format,
        "creator_strategy": dict(strategy),
        PLAN_AUTHORITY_FIELD: 1,
    }
    if media.get("voiceover_path"):
        candidates["voiceover_gcs_path"] = media["voiceover_path"]
    if media.get("song_path"):
        candidates["user_song"] = {"gcs_path": media["song_path"]}
    inputs = route_inputs_from_job(
        {"creator_brief_binding": {"media_snapshot": dict(media)}},
        candidates,
        platform="phone",
        capabilities=route_capabilities_from_settings(),
        contract=contract,
    )
    resolution = resolve_route(inputs) if inputs is not None else None
    return str(resolution.route) if resolution is not None and resolution.route else None


def contract_drawn_texts(
    contract: Any,
    strategy: Mapping[str, Any],
    *,
    media: Mapping[str, Any],
    manifest: ResolvedCreatorManifest | None,
    creator_id: object,
) -> tuple[str, ...]:
    """KRI-945: the pinned texts this draft's writer puts on screen whatever its plan holds."""
    from app.pipeline.phone_plan_repair import contract_text_drawn  # noqa: PLC0415

    if contract is None or not contract.exact_texts or creator_id is None:
        return ()
    route = _phone_route(
        contract,
        strategy,
        media=media,
        edit_format=strategy.get("edit_format") or (manifest.edit_format if manifest else None),
        creator_id=creator_id,
    )
    return tuple(row.text for row in contract.exact_texts if contract_text_drawn(row, route=route))


def _planned_order_basis(
    contract: Any,
    strategy: Mapping[str, Any],
    *,
    media: Mapping[str, Any],
    manifest: ResolvedCreatorManifest | None,
    creator_id: object,
) -> str | None:
    """The order the draft's writer commits to before it renders, when it is a server rule.

    29 Ekim: the planner stamps ``ordering_choice: "random"`` from the brief's random-order
    ask, and the phone unified montage writer draws exactly the requirement's stored-seed
    permutation (`unified_montage._order`), recording seed and source for the
    render's own receipt. That is an executable commitment, like a pinned title the writer
    draws. Any other writer, or no stamp, commits to nothing here.

    Leverkusen M1 run 2: a filming-order ask is the same commitment when the render contract
    pins it, i.e. every clip and Visual the edit shows has a capture time and the contract
    holds that exact order (the creator's first / last groups seated on it). The planner
    lays out that order (`unified_montage._order`, KRI-1069) and the phone verifier refuses a
    render that plays any other (`verify_phone_recipe`). Without this every filming-order
    draft read "I can't confirm the order this draft uses" and approval was held. A
    "group first" answer is not that order (the planner regroups it; the contract does not).

    KRI-1112 (Leverkusen M3, 13:39): so is a list the creator gave one clip at a time that
    places every clip and Visual the edit shows (the contract pins it as ``creator_list``):
    the planner seats exactly those rows (`apply_sequence`) and the verifier refuses any
    other order.
    """
    if strategy.get("ordering_choice") == "random":
        planned = "random"
    elif (
        contract is not None
        and contract.order_required
        and contract.order_basis == CREATOR_LIST_ORDER_BASIS
        and contract.order_ids
    ):
        planned = CREATOR_LIST_ORDER_BASIS
    elif (
        strategy.get("ordering_choice") != "group_first"
        and contract is not None
        and contract.order_required
        and contract.order_basis == "capture_time"
        and contract.order_ids
    ):
        planned = "capture_time"
    else:
        return None
    try:
        route = _phone_route(
            contract,
            strategy,
            media=media,
            edit_format=strategy.get("edit_format") or (manifest.edit_format if manifest else None),
            creator_id=creator_id,
        )
    except ValueError:
        return None
    return planned if route == "unified_montage" else None


def _research_proven(req: Any, facts: Any) -> bool:
    """The sourced wording researched before approval stands for this ask's research part."""
    from app.kria.brief_checks import _check_research  # noqa: PLC0415

    receipt = _check_research(req, facts)
    return receipt is not None and receipt.status == "met" and receipt.verification == "checked"


def _no_pre_render_checker(req: Any, facts: Any) -> bool:
    """KRI-983: no draft check can settle this ask: none owns it, or its only check reads the
    finished video (`judged_at_render`). A researched-text ask and a keep-text-off mark are
    proven outside `_has_checker` (research preview, the mark route), so they still block.

    Assos (prod 2026-11-01, third refusal): "With animations, explain why people should visit
    Assos" is ONE requirement with a research fact and an animation fact. Its research part is
    proven by the preview (the sourced wording is on the card), and what is left, how the text
    animates, is judged on the finished video. Holding the draft for the research part again
    ("I can't confirm this draft does it yet") blocked a creator on something no draft can
    show, so a requirement whose research is proven falls through to the checks of its other
    facts. Research that is not proven (never researched, a limit, a failure) still blocks."""
    from app.kria.brief_checks import judged_at_render, text_words_met  # noqa: PLC0415
    from app.kria.protected_targets import KEEP_TEXT_OFF_KEY, _fact_entries  # noqa: PLC0415
    from app.kria.research_preview import RESEARCH_FACT  # noqa: PLC0415
    from app.kria.style_asks import text_look_facts  # noqa: PLC0415
    from app.kria.text_sequence import is_text_sequence  # noqa: PLC0415

    researched = req.kind == "text" and isinstance(req.facts.get(RESEARCH_FACT), Mapping)
    if researched and not (
        settings.kria_researched_look_after_render_enabled and _research_proven(req, facts)
    ):
        return False
    if _fact_entries(req.facts.get(KEEP_TEXT_OFF_KEY)):
        return False
    if is_text_sequence(req):
        # 29 Ekim (prod thread 64bb9853): words shown a piece at a time are laid out by the
        # creation text pass, which runs after approval on the planned draft
        # (`compose_creation_text`), and are judged on the finished text lane
        # (`plan_facts_from_phone_variant` reads the fragments). A strategy draft has no text
        # lane, so "I can't confirm this draft does it yet" blocked approval on a question no
        # one could answer, before the step that does it had run. An editor draft still
        # proves it from its own ops (the caller never reaches here for one).
        return True
    from app.kria.brief_checks import asks_text_animation  # noqa: PLC0415

    # A whole-video "with animations" has no words to check on a draft and its look is judged on
    # the finished text lane: never a reason to hold approval (`unverified`, named after render).
    if asks_text_animation(req) and req.scope == "global" and not req.literal:
        return True
    if req.kind == "text" and text_look_facts(req):
        # KRI-1113 (thread 83f2dcaa, "Animate the title"): the words are checked on the draft;
        # once they are there, what is left is a look only the finished video shows (a strategy
        # draft has no text lane), and the render-time review judges it
        # (`plan_facts_from_phone_variant`). Words the draft lacks still block.
        return text_words_met(req, facts)
    # A text ask's words are checked on the draft even when its look is judged at render.
    return not _has_checker(req) or (req.kind != "text" and judged_at_render(req))


def planned_length_s(media: Mapping[str, Any]) -> float | None:
    """The edit length ``media``'s approved length plan plays; None when it has none to read.

    Its source windows plus the Visuals beside them: since KRI-824/825 the plan sizes only
    the footage and every placed Visual keeps its own seconds (``visual_budget_s``). The
    windows alone read a 25 s montage with three photos as "about 21.4s" (Leverkusen M1
    run 2), so no length the creator named could pass. Raises ``ValueError`` for an invalid
    plan, like ``read_length_plan``.
    """
    envelope = media.get("length_plan")
    if not isinstance(envelope, Mapping) or envelope.get("window_eligible") is not True:
        return None
    parts = read_length_plan(
        envelope,
        [
            SimpleNamespace(
                media_id=row.get("media_id"),
                kind=row.get("kind", "video"),
                duration_s=row.get("duration_s"),
                proxy_path=row.get("gcs_path"),
                storage_generation=row.get("storage_generation", row.get("generation")),
            )
            for row in media.get("clip_assignments") or []
            if isinstance(row, Mapping)
        ],
    )
    if not parts:
        return None
    visuals_s = envelope.get("visual_budget_s")
    return sum(p.duration_s for p in parts) + (
        float(visuals_s) if isinstance(visuals_s, (int, float)) and visuals_s > 0 else 0.0
    )


def assess_readiness(
    brief: CreativeBrief | None,
    strategy: Mapping[str, Any] | None,
    *,
    media: dict,
    manifest: ResolvedCreatorManifest | None,
    editor_payload: dict | None = None,
    editor_text_diff: list[dict] | None = None,
    creator_request: str | None = None,
    protected_regions: list[Any] | None = None,
    protected_marks: bool = False,
    prepared_collection_story: dict | None = None,
    creator_id: object = None,
    prepared_research: Mapping[str, Any] | None = None,
    order_execution: Mapping[str, Any] | None = None,
    order_source_context: OrderReplacementSourceContext | None = None,
    plan_blockers: Sequence[RequirementReceipt] = (),
) -> dict[str, Any]:
    """Account for every live ID using existing policy, checkers and contract.

    ``plan_blockers`` (Leverkusen M2 run 3) are checked receipts the phone montage plan the
    worker will build falls short on (``phone_montage_preflight.readiness_plan_blockers``): a
    speed ask that plan can't place. Each one replaces the draft check's verdict, so the
    creator hears it before the card. They only ever hold a draft back, never make one ready,
    so an approval re-check that does not run the plan can never disagree with a ready draft.

    KRI-901: ``protected_marks`` says this draft renders on the phone Talking writer for an
    app that shows the mark card (`protected_targets.marks_route`); only then do
    ``protected_regions`` (the marks this approval would pin) change a verdict.

    KRI-945: ``creator_id`` decides the phone route the draft renders on, so a pinned
    contract text the writer puts back on screen is executable; without it none is.
    ``prepared_research`` is the research done before approval (the draft's, or the one
    its binding pinned), which alone can prove a researched-facts ask."""
    requirements = brief.live() if brief is not None else []
    strategy = strategy or {}
    policy_problem = None
    contract = None
    if manifest is None:
        policy_problem = say(
            en="I haven't established which renderer can make this edit yet.",
            tr="Bu düzenlemeyi hangi oluşturucunun yapabileceğini henüz belirleyemedim.",
        )
    elif editor_payload is not None:
        dispatch = manifest.capabilities.get("dispatch_render")
        if dispatch is None or not dispatch.available:
            policy_problem = say(
                en="Rendering is unavailable for this edit right now.",
                tr="Bu düzenleme şu anda oluşturulamıyor.",
            )
    else:
        try:
            typed = CreativeStrategy.model_validate(strategy)
            checked = check_strategy_for_runtime_v2(
                manifest, typed, ask_before_simplifying=True, ask_about_stated_settings=True
            )
            # Re-running policy must not silently repair an approved promise.
            if isinstance(checked, RefusedStrategy):
                policy_problem = checked.question
            elif checked.strategy.model_dump(mode="json") != typed.model_dump(mode="json"):
                policy_problem = say(
                    en=(
                        "This renderer would change the proposed edit. "
                        "I need to prepare the supported version first."
                    ),
                    tr=(
                        "Bu oluşturucu önerilen düzenlemeyi değiştirir. "
                        "Önce desteklenen sürümü hazırlamam gerekiyor."
                    ),
                )
            dispatch = manifest.capabilities.get("dispatch_render")
            if dispatch is None or not dispatch.available:
                policy_problem = say(
                    en="Rendering is unavailable for this edit right now.",
                    tr="Bu düzenleme şu anda oluşturulamıyor.",
                )
            contract = build_render_contract(
                generation_id="approval-readiness",
                strategy=strategy,
                brief=brief,
                media_snapshot=media,
            )
            if contract is not None and contract.unresolved:
                policy_problem = contract.unresolved[0]
        except ValueError:
            policy_problem = say(
                en="The proposed edit isn't supported by the current renderer or footage.",
                tr="Önerilen düzenleme mevcut oluşturucu veya çekimlerle desteklenmiyor.",
            )
    if brief is None and not is_open_request(creator_request):
        policy_problem = say(
            en="I need to establish the requested changes before offering approval.",
            tr="Onay sunmadan önce istenen değişiklikleri belirlemem gerekiyor.",
        )
    story_requirement = None
    story_evidence_digest = None
    if prepared_collection_story is not None:
        from app.services.media_collection_preparation import prepared_story_readiness

        try:
            story_requirement, story_evidence_digest = prepared_story_readiness(
                prepared_collection_story, strategy=strategy, media=media
            )
        except ValueError:
            policy_problem = say(
                en="The selected idea's execution or sources changed. Review the idea again.",
                tr="Seçilen fikrin düzeni veya kaynakları değişti. Fikri yeniden gözden geçir.",
            )
    ids = [m.media_id for m in manifest.media] if manifest else []
    # KRI-1161: the draft turn tells its facts a phone Talking draft is one
    # (`defers_caption_words_to_phone_render`), so an ask outside what that edit draws is a checked
    # refusal there (`_talking_honest_receipt`). Readiness must read the same facts, or the same
    # requirement is `not_possible` in the reply and `unverified` (approvable, hidden) here.
    talking_edit_format = strategy.get("edit_format") or (
        manifest.edit_format if manifest else None
    )
    talking_writer = defers_caption_words_to_phone_render(
        creator_id=creator_id,
        edit_format=talking_edit_format,
        audio_strategy=strategy.get("audio_strategy"),
        clip_paths=media.get("clip_paths") or (),
    )
    facts = (
        plan_facts_from_editor_payload(editor_payload, editor_text_diff)
        if editor_payload is not None
        else dataclasses.replace(
            plan_facts_from_strategy(strategy, clip_ids=ids, manifest=manifest),
            contract_drawn_texts=contract_drawn_texts(
                contract, strategy, media=media, manifest=manifest, creator_id=creator_id
            ),
            research=dict(prepared_research) if prepared_research is not None else None,
            user_song_attached=bool(media.get("song_path")),
            # KRI-1024: a literal no phone font draws is never executable on the phone.
            phone_text=defers_text_to_phone(
                creator_id=creator_id, clip_paths=media.get("clip_paths") or ()
            ),
            phone_talking=talking_writer and talking_edit_format in {"subtitled", "talking_head"},
            caption_words_at_render=talking_writer,
        )
    )
    if editor_payload is None and facts.ordering_basis is None:
        facts = dataclasses.replace(
            facts,
            ordering_basis=_planned_order_basis(
                contract, strategy, media=media, manifest=manifest, creator_id=creator_id
            ),
        )
    # Use the existing source-bounded execution plan, never the model's duration
    # target as proof. The same validator is used by the render path.
    envelope = media.get("length_plan")
    if editor_payload is None:
        try:
            planned_s = planned_length_s(media)
            if planned_s is not None:
                facts = dataclasses.replace(facts, duration_s=planned_s)
        except ValueError:
            pass  # Missing/invalid execution evidence stays unknown, never a pass.
    facts = with_verified_order_execution(
        facts,
        brief=brief,
        strategy=strategy,
        execution=order_execution,
        source_context=order_source_context,
        creator_id=creator_id,
    )
    receipts = {
        r.requirement_id: r
        for r in build_receipts(
            requirements,
            facts,
            include_unchecked=True,
            strict_order=True,
        )
    }
    for blocker in plan_blockers:
        if (
            blocker.requirement_id in receipts
            and blocker.verification == "checked"
            and blocker.status != "met"
        ):
            receipts[blocker.requirement_id] = blocker
    delegated = {
        str(req_id)
        for answer in strategy.get("choice_answers") or []
        if isinstance(answer, Mapping) and answer.get("source") == "creator_delegated"
        for req_id in answer.get("requirement_ids") or []
    }
    # The unified montage draft says what its route cannot read (animated text outside the
    # title) and approves the closest thing instead of holding the draft.
    on_unified_montage = (
        settings.kria_route_disclosure_enabled
        and not policy_problem
        and editor_payload is None
        and _defers_to_unified_montage(strategy, media, manifest, creator_id)
    )
    # The same route the draft card reads (`route_disclosure.draft_route`), so a decline on the
    # card is the decline readiness approves.
    decline_route = (
        draft_route(
            creator_id=creator_id,
            edit_format=strategy.get("edit_format") or (manifest.edit_format if manifest else None),
            strategy=strategy,
            clip_paths=[str(path) for path in media.get("clip_paths") or []],
            phone_montage=on_unified_montage,
        )
        if settings.kria_route_disclosure_enabled and not policy_problem and editor_payload is None
        else None
    )
    declined = (
        {d.requirement_id: d for d in ask_declines(requirements, strategy, route=decline_route)}
        if not policy_problem
        else {}
    )
    rows = []
    for req in requirements:
        receipt = receipts.get(req.id)
        proven = (
            receipt is not None and receipt.verification == "checked" and receipt.status == "met"
        )
        owner = "brief_checks"
        is_story_requirement = "collection_story" in req.facts
        if is_story_requirement:
            proven = (
                story_requirement is not None
                and req.kind == story_requirement.kind
                and req.scope == story_requirement.scope
                and req.description == story_requirement.description
                and req.literal == story_requirement.literal
                and req.facts == story_requirement.facts
            )
            owner = "prepared_collection_story"
            if not proven:
                # A reviewed story cannot fall back to a checker for its title's
                # incidental words when its bound execution evidence is absent.
                receipt = None
        # Original camera audio is an explicit executable contract commitment;
        # it is not a claim that the final mix has already been listened to.
        if (
            not is_story_requirement
            and contract is not None
            and _wants_source_sound_kept(req)
            and contract.original_audio == "require"
            and strategy.get("audio_strategy") == "original_audio"
            and not strategy.get("licensed_music")
        ):
            proven, owner = True, FIELD_MATRIX["audio_strategy"].owner
        checked = receipt is not None and receipt.verification == "checked"
        disposition = (
            "unsupported"
            if policy_problem
            else "delegated"
            if proven and req.id in delegated
            else "executable"
            if proven
            else "unsupported"
            if checked and receipt.status == "not_possible"
            else "needs_choice"
            if checked
            else "unverified"
            if receipt is not None
            and not is_story_requirement
            # An editor draft is its own change set, so what its ops did not do is not done.
            and editor_payload is None
            and _no_pre_render_checker(req, facts)
            else "evidence_missing"
        )
        if req.id in declined and disposition in {"unverified", "evidence_missing"}:
            # Nothing checked this ask, and the route cannot read it: said plainly, approvable.
            # (A checker's own verdict, e.g. the words are missing, is never hidden by this.)
            disposition = "declined"
        reason = policy_problem or (
            declined[req.id].text
            if disposition == "declined"
            else receipt.reason
            if checked
            else say(
                en="I'll show these words one at a time when I prepare the video, then check.",
                tr="Bu kelimeleri videoyu hazırlarken tek tek göstereceğim, sonra kontrol ederim.",
            )
            if disposition == "unverified" and req.kind == "text_sequence"
            else say(
                en="I can't check this before the video is made; I'll point it out after.",
                tr="Bunu video hazırlanmadan önce kontrol edemiyorum; sonra sana göstereceğim.",
            )
            if disposition == "unverified"
            else say(
                en="I can't confirm this draft does it yet.",
                tr="Bu taslağın bunu yaptığını henüz doğrulayamıyorum.",
            )
        )
        if proven and owner == "prepared_collection_story":
            reason = say(
                en=(
                    "The reviewed story's source windows, wording and sound "
                    "match its frozen execution."
                ),
                tr=(
                    "Gözden geçirilen hikâyenin kaynak aralıkları, metni ve sesi "
                    "sabitlenen düzenle eşleşiyor."
                ),
            )
        elif owner == "prepared_collection_story":
            reason = policy_problem or say(
                en="I need the selected idea's unchanged execution evidence before approval.",
                tr="Onaydan önce seçilen fikrin değişmemiş düzen kanıtına ihtiyacım var.",
            )
        elif proven and owner != "brief_checks":
            reason = say(
                en="Original camera audio is required by the render contract.",
                tr="Oluşturma sözleşmesi kameranın özgün sesini zorunlu tutuyor.",
            )
        rows.append(
            {
                "requirement_id": req.id,
                "disposition": disposition,
                "reason": reason,
                "evidence_owner": owner,
            }
        )
    if protected_marks and not policy_problem:
        rows = _with_protected_marks(
            rows,
            requirements,
            brief,
            marks=protected_regions or [],
            media=media,
        )
    if on_unified_montage:
        # Only the route whose render judges the ask itself (the phone unified montage) may
        # approve what no draft check can decide; every other route keeps its hold.
        rows = _approve_undecidable(rows, requirements)
    report = {
        "schema_version": 1,
        "ready": not policy_problem and all(r["disposition"] in APPROVABLE for r in rows),
        "brief_version": brief.version if brief else 0,
        "brief_digest": digest(brief.model_dump(mode="json") if brief else None),
        "strategy_digest": digest(
            {
                "strategy": strategy,
                "editor_payload": editor_payload,
                "editor_text_diff": editor_text_diff,
            }
        ),
        "media_digest": digest(media_identity(media)),
        "prepared_evidence_digest": story_evidence_digest or digest(envelope),
        "capabilities_digest": digest(
            {
                "capabilities": manifest.capabilities if manifest else None,
                "verified_phone_features": sorted(settings.phone_render_verified_features),
                "field_matrix": field_matrix_for_strategy(strategy),
                "adapters": ADAPTER_DECLARATIONS,
            }
        ),
        "requirements": rows,
        "policy_problem": policy_problem,
        # KRI-901: an input, not a verdict (the dispositions above carry it into the
        # digest). Saved so approval re-checks with the client the draft was made for.
        "protected_marks": bool(protected_marks),
    }
    # Pydantic values in capability records are serialized without model text.
    report["digest"] = digest(_identity(report))
    return report


def _defers_to_unified_montage(
    strategy: Mapping[str, Any],
    media: Mapping[str, Any],
    manifest: ResolvedCreatorManifest | None,
    creator_id: object,
) -> bool:
    """True when this strategy draft renders through the phone unified montage planner."""
    from app.kria.brief_checks import defers_to_unified_montage  # noqa: PLC0415

    return defers_to_unified_montage(
        creator_id=creator_id,
        edit_format=strategy.get("edit_format") or (manifest.edit_format if manifest else None),
        audio_strategy=strategy.get("audio_strategy"),
        clip_paths=[str(path) for path in media.get("clip_paths") or []],
    )


def _approve_undecidable(
    rows: list[dict[str, Any]], requirements: list[Any]
) -> list[dict[str, Any]]:
    """On the unified montage, an ask a checker cannot decide before render is approvable.

    A vague ask ("slow pace") has no checker and was already approved with a look-after line
    (`unverified`), while an ask a checker did recognise but lacked the facts to decide held
    the draft behind "I can't confirm this draft does it yet" (`evidence_missing`): the more
    precise the ask, the harder the stop. Both now wait for the render's own verdict, named to
    the creator. Researched-text and keep-text-off asks keep their own flows (the research
    preview and the mark card), and a prepared story keeps its frozen-evidence rule."""
    from app.kria.brief_checks import _has_duration_target  # noqa: PLC0415
    from app.kria.protected_targets import KEEP_TEXT_OFF_KEY, _fact_entries  # noqa: PLC0415
    from app.kria.research_preview import RESEARCH_FACT  # noqa: PLC0415

    by_id = {req.id: req for req in requirements}
    out = []
    for row in rows:
        req = by_id.get(row["requirement_id"])
        if (
            row["disposition"] != "evidence_missing"
            or row["evidence_owner"] != "brief_checks"
            or req is None
            # An order ask is judged on proof of who asked and what ran: a missing or wrong
            # proof is held (the worker would refuse the render after approval).
            or req.kind == "order"
            # A quoted text is checked on the draft and the worker refuses a render whose
            # confirmed text is missing from the plan: the creator fixes it before approval.
            or (req.kind == "text" and bool(req.literal))
            # A length ask whose evidence is missing stays unknown, never a pass.
            or (req.kind == "timing" and _has_duration_target(req))
            or "collection_story" in req.facts
            or (req.kind == "text" and isinstance(req.facts.get(RESEARCH_FACT), Mapping))
            or _fact_entries(req.facts.get(KEEP_TEXT_OFF_KEY))
        ):
            out.append(row)
            continue
        out.append(
            {
                **row,
                "disposition": "unverified",
                "reason": say(
                    en="I can't check this before the video is made; I'll point it out after.",
                    tr="Bunu video hazırlanmadan önce kontrol edemiyorum; sonra sana göstereceğim.",
                ),
            }
        )
    return out


def _with_protected_marks(
    rows: list[dict[str, Any]],
    requirements: list[Any],
    brief: CreativeBrief | None,
    *,
    marks: list[Any],
    media: Mapping[str, Any],
) -> list[dict[str, Any]]:
    """KRI-901: "no text on her face or on the plate" before approval, on the mark route.

    Called only when the draft renders on the phone Talking writer for an app that shows the
    mark card. No pre-render checker can prove where text lands, so the ask is a commitment
    that writer measures (`phone_protected_text`; its receipt must still read ``clear``). An
    object with no current mark asks for it; once marked, a requirement whose only open ask
    is keeping text off becomes executable. Faces need no mark, and a requirement's other
    asks (a title, a literal) keep their own checker's verdict."""
    from app.kria.protected_targets import (  # noqa: PLC0415
        KEEP_TEXT_OFF_KEY,
        _fact_entries,
        clip_generations,
        mark_request_text,
        speaker_candidates,
        unmarked_object_targets,
    )

    by_id = {req.id: req for req in requirements}
    clip_assignments = media.get("clip_assignments") or []
    # A mark promises protection only on a clip the writer may put the text on.
    unmarked = unmarked_object_targets(
        brief,
        marks,
        clip_generations(clip_assignments),
        render_clips=speaker_candidates(clip_assignments),
    )
    out = []
    for row in rows:
        req = by_id.get(row["requirement_id"])
        if (
            req is None
            or not _fact_entries(req.facts.get(KEEP_TEXT_OFF_KEY))
            or row["disposition"] == "unsupported"
        ):
            out.append(row)
            continue
        missing = [t for t in unmarked if req.id in t.requirement_ids]
        if missing:
            row = {
                **row,
                "disposition": "needs_choice",
                "reason": mark_request_text(missing[0].label),
            }
        elif row["disposition"] == "evidence_missing" and not _has_checker(
            req.model_copy(
                update={"facts": {k: v for k, v in req.facts.items() if k != KEEP_TEXT_OFF_KEY}}
            )
        ):
            row = {
                **row,
                "disposition": "executable",
                "evidence_owner": "protected_text_render",
                "reason": say(
                    en="The phone render keeps the text off what you named and checks it.",
                    tr=(
                        "Telefon oluşturması yazıyı belirttiğin yerlerden uzak tutup "
                        "kontrol ediyor."
                    ),
                ),
            }
        out.append(row)
    return out


def _identity(report: dict[str, Any]) -> dict[str, Any]:
    # Localized explanatory copy is not execution authority.
    return {
        **{
            key: report[key]
            for key in (
                "schema_version",
                "ready",
                "brief_version",
                "brief_digest",
                "strategy_digest",
                "media_digest",
                "prepared_evidence_digest",
                "capabilities_digest",
            )
        },
        "requirements": [
            {
                key: row[key]
                for key in (
                    "requirement_id",
                    "disposition",
                    "evidence_owner",
                )
            }
            for row in report["requirements"]
        ],
    }


def readiness_matches(saved: object, current: dict[str, Any]) -> bool:
    try:
        return (
            isinstance(saved, dict)
            and current["ready"]
            and saved["digest"] == digest(_identity(saved)) == current["digest"]
        )
    except (KeyError, TypeError):
        return False


def readiness_reply(brief: CreativeBrief | None, report: dict[str, Any]) -> str:
    """Display identified blocked requirements; never derive execution from prose.

    A ``policy_problem`` blocks the whole draft and is every requirement's reason (KRI-1123:
    Leverkusen M4 showed one unplaced first clip as the reason under "Make it 20 seconds",
    "Skip the dark photo" and every label). It is said once, ahead of the list; a requirement
    whose reason is that problem is named without repeating it, and one with a reason of its
    own keeps it. A refused first / last clip says what to do next: name the clip.
    """
    by_id = {r.id: r for r in brief.live()} if brief else {}
    lines = [say(en="I can't make this yet:", tr="Bunu henüz yapamıyorum:")]
    policy = report.get("policy_problem")
    if policy:
        lines.append(f"{policy} {_pin_next_step(brief, str(policy))}".rstrip())
    for row in report["requirements"]:
        if row["disposition"] in APPROVABLE:
            continue
        name = by_id[row["requirement_id"]].text()
        lines.append(
            f"- {name}" if policy and row["reason"] == policy else f"- {name}: {row['reason']}"
        )
    return "\n".join(lines)


def _pin_next_step(brief: CreativeBrief | None, problem: str) -> str:
    """What to do about a stated first / last clip the contract could not place, else nothing.

    Compares the problem with the contract's own sentence for each clip the brief states, so
    it reads no words and any other problem gets no next step."""
    if any(problem == _pin_not_found(words) for words in stated_anchors(brief).values()):
        return say(
            en="Tell me which clip you mean, and I'll make a new draft.",
            tr="Hangi klibi kastettiğini söyle, yeni bir taslak hazırlayayım.",
        )
    return ""


class ApprovalReadinessError(ValueError):
    pass


def saved_protected_marks(
    document,  # noqa: ANN001
    *,
    creator_id: object,
    item,  # noqa: ANN001
    saved: object = None,
) -> bool:
    """KRI-901: the mark route a saved draft was assessed on, re-checked against today's
    route (a draft made for the mark card whose account left the phone cohort no longer
    gets the promise, so its readiness changes and it is prepared again)."""
    from app.kria.protected_targets import marks_route  # noqa: PLC0415

    if saved is None:
        saved = (document.brief_coverage or {}).get(READINESS_KEY)
    return marks_route(
        client_marks=isinstance(saved, dict) and saved.get("protected_marks") is True,
        creator_id=creator_id,
        strategy=document.strategy,
        edit_format=getattr(item, "edit_format", None) or document.edit_format,
        clip_paths=getattr(item, "clip_gcs_paths", None) or (),
        voiceover_path=getattr(item, "voiceover_gcs_path", None),
    )


def requires_readiness(document, creator_id):  # noqa: ANN001, ANN201
    return (document.brief_coverage or {}).get(READINESS_KEY) is not None or (
        settings.approval_readiness_for(creator_id) and document.brief_binding is not None
    )


async def assess_saved_document(  # noqa: ANN201
    db,  # noqa: ANN001
    item,  # noqa: ANN001
    document,  # noqa: ANN001
    *,
    creator_id: object = None,
    require_same_assessment: bool = True,
):
    """Retry/Undo use the same gate without rebuilding the approved content.

    ``creator_id`` re-checks the KRI-901 mark route; without it a draft saved on that route
    reads as off it (its readiness no longer matches), never the other way round."""
    if document.brief_binding is None:
        raise ApprovalReadinessError(
            say(
                en="I need a saved request and its source evidence before offering this render.",
                tr="Bu oluşturmayı sunmadan önce kayıtlı istek ve kaynak kanıtları gerekiyor.",
            )
        )
    brief = document.brief_binding.resolve()
    manifest = await resolve_readiness_manifest(db, item, document.strategy)
    report = assess_readiness(
        brief,
        document.strategy,
        media=document.brief_binding.media_snapshot,
        manifest=manifest,
        editor_payload=document.editor_payload,
        editor_text_diff=document.editor_text_diff,
        creator_request=document.brief_binding.creator_request,
        protected_regions=list(document.brief_binding.protected_regions or []),
        protected_marks=saved_protected_marks(document, creator_id=creator_id, item=item),
        prepared_collection_story=document.prepared_collection_story,
        creator_id=creator_id,
        prepared_research=document.brief_binding.prepared_research,
    )
    if digest(media_identity(snapshot_media(item))) != report["media_digest"]:
        raise ApprovalReadinessError(
            say(
                en="The saved edit's footage changed. Ask me to prepare it again.",
                tr="Kayıtlı düzenlemenin çekimleri değişti. Yeniden hazırlamamı iste.",
            )
        )
    if not report["ready"]:
        raise ApprovalReadinessError(readiness_reply(brief, report))
    saved = (document.brief_coverage or {}).get(READINESS_KEY)
    if require_same_assessment and saved is not None and not readiness_matches(saved, report):
        raise ApprovalReadinessError(
            say(
                en="The saved edit's rendering evidence changed. Ask me to prepare it again.",
                tr="Kayıtlı düzenlemenin oluşturma kanıtları değişti. Yeniden hazırlamamı iste.",
            )
        )
    return report
