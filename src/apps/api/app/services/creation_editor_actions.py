"""Narrow runtime-1 editor execution, using the canonical atomic Save validators."""

from __future__ import annotations

import copy
import functools
import hashlib
import re
from collections.abc import Awaitable, Callable
from typing import Any, TypeVar

import structlog
from fastapi import HTTPException
from sqlalchemy import select

from app.config import settings
from app.kria.reply_language import (
    current_reply_language,
    detect_chat_language,
    reply_language_for,
    resolve_reply_language,
    say,
    thread_reply_language,
)
from app.models import CreationThreadEvent, Job, PlanItem, PlanItemAsset
from app.routes._copilot import CopilotTurnBody, run_copilot_turn
from app.routes.generative_jobs import (
    EditorCommitRequest,
    _phone_subtitled_sfx_paths,
    enqueue_editor_commit_render,
    prepare_editor_commit,
    variant_render_baseline,
)
from app.services.kria_editor_ops import (
    build_editor_snapshot,
    compile_editor_ops,
)
from app.services.phone_speaker_background import (
    BACKGROUND_REFUSALS,
    pin_background_for_commit,
)

log = structlog.get_logger()

_T = TypeVar("_T")


def _in_chat_language(
    func: Callable[..., Awaitable[_T]],
) -> Callable[..., Awaitable[_T]]:
    """Bind the chat's reply language for one v1 editor turn (KRI-784).

    The creator's message decides it when it clearly reads as one language; else the
    language already stored on the thread. Unknown means English, byte-identical.
    """

    @functools.wraps(func)
    async def wrapper(db: Any, thread: Any, *args: Any, **kwargs: Any) -> _T:
        body = args[0] if args and hasattr(args[0], "message") else None
        message = getattr(body, "message", None)
        language = resolve_reply_language(
            message if isinstance(message, str) else None,
            previous=thread_reply_language(thread),
        )
        with reply_language_for(language):
            return await func(db, thread, *args, **kwargs)

    return wrapper


# Job.status values a generative render lands on once its variants exist and
# are addressable by chat.  Kept in sync with the literal set the messages
# route uses to decide whether a Creator session can reconcile
# (`{"done", "variants_ready", "variants_ready_partial"}` in
# `creation_threads.message_thread`) so both call sites agree on "this job is
# a finished cut, not still rendering."
POST_RENDER_EDIT_JOB_STATUSES = frozenset({"done", "variants_ready", "variants_ready_partial"})


def is_visual_removal_request(message: str) -> bool:
    """Admission only; the model must still resolve an explicit safe operation."""
    return bool(
        re.search(r"\b(remove|delete|clear|erase|strip|take out|get rid of)\b", message, re.I)
        and re.search(
            r"\b(visuals?|overlays?|images?|pictures?|photos?|video layers?)\b", message, re.I
        )
    )


def selected_variant(thread: Any, job: Any) -> dict | None:
    variants = [v for v in (job.assembly_plan or {}).get("variants", []) if isinstance(v, dict)]
    selected = (thread.state or {}).get("selected_variant_id")
    if selected:
        return next((v for v in variants if v.get("variant_id") == selected), None)
    return variants[0] if len(variants) == 1 else None


@_in_chat_language
async def execute_visual_removal(db: Any, thread: Any, body: Any, user: Any) -> Any:
    """Resolve outside locks, then persist user event, mutation and receipt together."""
    # Lazy import avoids the route/service module cycle.
    from app.routes import creation_threads as routes

    _, _, job, _ = await routes._load_authorized_projection_rows(db, thread)
    variant = selected_variant(thread, job) if job is not None else None
    if variant is None or len(body.message) > 2000:
        reply = (
            say(
                en="Please shorten this Visuals request to 2,000 characters so I can apply it "
                "safely.",
                tr="Bu görsel isteğini güvenle uygulayabilmem için 2.000 karakterin altına "
                "kısaltır mısın?",
            )
            if len(body.message) > 2000
            else say(
                en=(
                    "Open the video version you want to edit, "
                    "then ask me to remove its uploaded Visuals."
                ),
                tr=(
                    "Düzenlemek istediğin video sürümünü aç, sonra yüklediğin görselleri "
                    "kaldırmamı iste."
                ),
            )
        )
        await routes._append(
            db,
            thread,
            event_type="user_message",
            role="user",
            content=body.message,
            client_event_id=body.client_event_id,
        )
        await routes._append(
            db, thread, event_type="assistant_response", role="assistant", content=reply
        )
        await db.commit()
        return thread

    user_id, user_type = user.id, type(user)
    job_id, variant_id = job.id, variant["variant_id"]
    selection = (thread.state or {}).get("selected_variant_id")
    baseline = variant_render_baseline(variant)
    snapshot = build_editor_snapshot(job, copy.deepcopy(variant))
    # This first release executes only uploaded media removals. Other editor
    # families retain their existing client Save / planner workflows.
    snapshot["component_context_version"] = 1
    snapshot["allowed_op_families"] = [
        family for family in snapshot["allowed_op_families"] if family == "visual_media"
    ]
    thread_id = str(thread.id)
    await db.rollback()
    response = await run_copilot_turn(
        CopilotTurnBody(
            message=body.message,
            snapshot=snapshot,
            client_contract_version=2,
            client_request_id=hashlib.sha256(body.client_event_id.encode("utf-8")).hexdigest(),
        ),
        job_id=job_id,
    )
    # Preserve the global mutation lock order: account -> thread -> item -> job.
    user = await db.get(user_type, user_id, with_for_update=True, populate_existing=True)
    if user is None:
        raise HTTPException(status_code=401, detail="Authentication required")
    thread = await routes._load(thread_id, user, db, lock=True)
    routes._require_runtime_v1_mutation(thread)
    duplicate = await routes._duplicate(db, thread.id, routes._client_id(body.client_event_id))
    if duplicate:
        if duplicate.event_type != "user_message" or duplicate.content != body.message:
            raise HTTPException(status_code=409, detail="Idempotency key reused")
        # rollback() expires ``user``; reload with the id captured above.
        await db.rollback()
        return await routes._load(thread_id, user, db, creator_id=user_id)
    if thread.status != "active" or thread.revision != body.expected_revision:
        raise HTTPException(status_code=409, detail="Creation thread changed")
    # Refresh and hold the item before validating its current-job pointer, as
    # canonical manual Save does. A job replacement cannot pass this fence.
    locked_item = await db.get(
        PlanItem, thread.active_plan_item_id, populate_existing=True, with_for_update=True
    )
    if locked_item is None or locked_item.current_job_id != job_id:
        raise HTTPException(status_code=409, detail="Editor target changed")
    item, _, current_job, _ = await routes._load_authorized_projection_rows(db, thread)
    if (
        current_job is None
        or current_job.id != job_id
        or (thread.state or {}).get("selected_variant_id") != selection
    ):
        raise HTTPException(status_code=409, detail="Editor target changed")
    job = await db.get(Job, job_id, populate_existing=True, with_for_update=True)
    if job.status == "cancelled":
        raise HTTPException(
            status_code=409,
            detail=say(
                en="Cancelled videos cannot be edited.",
                tr="İptal edilen videolar düzenlenemez.",
            ),
        )
    variant = selected_variant(thread, job)
    if (
        variant is None
        or variant.get("variant_id") != variant_id
        or variant_render_baseline(variant) != baseline
    ):
        raise HTTPException(status_code=409, detail="baseline_conflict")

    prep = None
    receipt: dict[str, Any] = {
        "job_id": str(job_id),
        "variant_id": variant_id,
        "outcome": response.outcome,
    }
    if response.ops and all(op.get("op") == "remove_visual_media" for op in response.ops):
        original_plan = copy.deepcopy(job.assembly_plan)
        try:
            compiled = compile_editor_ops(job, variant, response.ops)
            payload = EditorCommitRequest.model_validate(compiled.payload)
            assets = (
                (
                    await db.execute(
                        select(PlanItemAsset).where(
                            PlanItemAsset.plan_item_id == thread.active_plan_item_id
                        )
                    )
                )
                .scalars()
                .all()
            )
            visual_assets = {
                str(asset.id): {
                    "status": asset.status,
                    "gcs_path": asset.gcs_path,
                    "kind": asset.kind,
                    "duration_s": getattr(asset, "duration_s", None),
                    "preview_gcs_path": getattr(asset, "preview_gcs_path", None),
                    "user_context": getattr(asset, "user_context", None),
                }
                for asset in assets
            }
            if variant.get("resolved_archetype") == "guided_story":
                from app.pipeline.guided_story import (  # noqa: PLC0415
                    GuidedStoryError,
                    validate_guided_snapshot,
                )
                from app.routes.plan_items import _proposal_media_is_current  # noqa: PLC0415

                try:
                    _, _, guided_snapshot = validate_guided_snapshot(
                        (job.assembly_plan or {}).get("guided_edit")
                    )
                    media_current = await _proposal_media_is_current(
                        item, guided_snapshot, db, user_id=user.id
                    )
                except GuidedStoryError as exc:
                    raise ValueError("The guided source is stale.") from exc
                if not media_current:
                    raise ValueError("The guided source is stale.")
            prep = prepare_editor_commit(
                job,
                variant_id,
                payload,
                user_id=str(user.id),
                plan_item_id=str(thread.active_plan_item_id),
                visual_assets=visual_assets,
            )
            from app.services.finished_style_learning import stage_prepared_style_edit

            await stage_prepared_style_edit(db, user.id, job, variant_id, prep)
            if not prep["sections"].get("visual_blocks"):
                raise ValueError("This video cannot currently save uploaded Visuals changes.")
            receipt.update(
                outcome="saved", generation=prep["generation"], sections=prep["sections"]
            )
            reply = say(
                en=(
                    "Removed the requested uploaded Visuals from the saved edit. "
                    "Text, captions, footage and audio are unchanged."
                ),
                tr=(
                    "İstediğin yüklenmiş görselleri kayıtlı kurgudan kaldırdım. Yazılar, "
                    "altyazılar, görüntüler ve ses aynı kaldı."
                ),
            )
        except (ValueError, HTTPException) as exc:
            # Validation stages no writes on failure. Do not convert a moved
            # baseline into a misleading assistant response.
            if isinstance(exc, HTTPException) and exc.status_code == 409:
                raise
            job.assembly_plan = original_plan
            prep = None
            receipt["outcome"] = "unsupported"
            reply = say(
                en="I couldn’t safely save that Visuals removal. The edit is unchanged.",
                tr="Görselleri kaldırma işlemini güvenle kaydedemedim. Kurgu aynı kaldı.",
            )
    else:
        # Model prose is not evidence of persistence, even for clarification
        # or no_effect (a negated unrelated clause can mask a success claim).
        receipt["outcome"] = response.outcome if not response.ops else "unsupported"
        reply = (
            say(
                en=(
                    "Which uploaded image or video Visuals should I remove? "
                    "Please identify them or say all uploaded Visuals. Nothing was changed."
                ),
                tr=(
                    "Yüklediğin görsellerden hangilerini kaldırayım? Hangileri olduğunu söyle "
                    "ya da hepsini kaldırmamı iste. Hiçbir şey değişmedi."
                ),
            )
            if response.outcome == "clarification" and not response.ops
            else say(
                en=(
                    "I couldn’t apply that uploaded Visuals removal. Nothing was changed. "
                    "I can remove supported uploaded image and video Visuals; "
                    "please identify the ones you want removed."
                ),
                tr=(
                    "Görselleri kaldırma isteğini uygulayamadım. Hiçbir şey değişmedi. "
                    "Yüklediğin fotoğraf ve videoları kaldırabilirim; hangilerini "
                    "kaldırmamı istediğini söyle."
                ),
            )
        )
    await routes._append(
        db,
        thread,
        event_type="user_message",
        role="user",
        content=body.message,
        client_event_id=body.client_event_id,
    )
    await routes._append(
        db,
        thread,
        event_type="assistant_response",
        role="assistant",
        content=reply,
        payload=receipt,
    )
    await db.commit()
    if prep:
        try:
            enqueue_editor_commit_render(str(job_id), variant_id, prep)
        except Exception:
            # Desired state is durable; terminalize only this exact attempt.
            thread = await routes._load(thread_id, user, db, lock=True)
            job = await db.get(Job, job_id, populate_existing=True, with_for_update=True)
            plan = copy.deepcopy(job.assembly_plan or {})
            terminalized = False
            for row in plan.get("variants", []):
                if (
                    row.get("variant_id") == variant_id
                    and row.get("render_generation_id") == prep["generation"]
                    and row.get("render_status") == "rendering"
                ):
                    row.update(
                        render_status="failed",
                        ok=False,
                        error="The saved render could not be queued.",
                        error_class="render_enqueue_failed",
                    )
                    terminalized = True
                    job.assembly_plan = plan
                    if row.get("video_path") or row.get("output_url"):
                        job.status = "variants_ready_partial"
                    break
            if terminalized:
                await routes._append(
                    db,
                    thread,
                    event_type="assistant_render_failed",
                    role="assistant",
                    content=say(
                        en=(
                            "Your Visuals change was saved, but rendering could not start. "
                            "Retry the render to update the exported video."
                        ),
                        tr=(
                            "Görsel değişikliğin kaydedildi ama video hazırlanmaya "
                            "başlayamadı. Dışa aktarılan videoyu güncellemek için yeniden dene."
                        ),
                    ),
                    payload={**receipt, "outcome": "render_enqueue_failed"},
                )
            await db.commit()
    return thread


def _generic_rejection_reply() -> str:
    return say(
        en="I couldn't safely apply that change. Nothing was changed.",
        tr="Bu değişikliği güvenle uygulayamadım. Hiçbir şey değişmedi.",
    )


def _rejection_detail(exc: Exception) -> str:
    """Operator-facing reason for a rejected chat edit (never a traceback)."""
    detail = exc.detail if isinstance(exc, HTTPException) else str(exc)
    if isinstance(detail, dict):
        code = detail.get("code") or ""
        reason = detail.get("reason") or ""
        detail = detail.get("message") or (f"{code}: {reason}" if reason else code) or ""
    return str(detail or exc.__class__.__name__)[:300]


def _rejection_reply(exc: Exception) -> str:
    """Creator-facing copy: only a validator's own authored sentence is safe to show.

    `_GUIDED_STORY_TEXT_REQUIRED_ERROR`-style dict details carry a `message`
    written for creators; bare strings and codes are internal and stay generic.
    """
    if isinstance(exc, HTTPException) and isinstance(exc.detail, dict):
        refusal = BACKGROUND_REFUSALS.get(str(exc.detail.get("code") or ""))
        if refusal is not None:
            # KRI-1117 (S18): the honest "Couldn't ..." for a background the Save refused.
            return say(en=f"{refusal[0]}. ", tr=f"{refusal[1]}. ") + say(
                en="Nothing was changed.", tr="Hiçbir şey değişmedi."
            )
        message = str(exc.detail.get("message") or "").strip()
        if message and not _mixes_languages(message):
            return f"{message} " + say(en="Nothing was changed.", tr="Hiçbir şey değişmedi.")
    return _generic_rejection_reply()


def _mixes_languages(*texts: str) -> bool:
    """True when English server text would land inside a Turkish reply (KRI-784).

    Validator messages and op change summaries are still English; a Turkish chat
    gets the generic Turkish sentence rather than a half-English one.
    """
    if current_reply_language() != "tr":
        return False
    return any(detect_chat_language(text) != "tr" for text in texts)


def _applied_summary(changes: list[str]) -> str:
    if not changes or _mixes_languages(*changes):
        return say(en="Applied your edit", tr="Düzenlemeni uyguladım")
    if len(changes) == 1:
        return changes[0]
    return f"{', '.join(changes[:-1])}" + say(en=" and ", tr=" ve ") + changes[-1]


# KRI-186: rejection code -> plain phrase shown to the creator.  Codes come from
# `_ParseState.reject` in the copilot agent; anything unmapped gets the generic
# phrase so an internal code never leaks into chat copy.
_REJECTION_PHRASES = {
    "capability_unavailable": "that isn't something I can change in this edit yet",
    "unknown_operation": "I don't have a way to do that here",
    "stale_target": "the edit changed since I looked, so I skipped it",
    "missing_required": "I needed one more detail to do it",
    "invalid_value": "the value wasn't one I can apply",
}
_GENERIC_REJECTION_PHRASE = "I couldn't apply it"
# KRI-784: the same phrases for a Turkish chat (same keys as `_REJECTION_PHRASES`).
_REJECTION_PHRASES_TR = {
    "capability_unavailable": "bu düzenlemede bunu henüz değiştiremiyorum",
    "unknown_operation": "bunu burada yapmanın bir yolu yok",
    "stale_target": "ben bakarken düzenleme değişti, bu yüzden atladım",
    "missing_required": "bunu yapmak için bir bilgiye daha ihtiyacım vardı",
    "invalid_value": "bu değeri uygulayamıyorum",
}
_GENERIC_REJECTION_PHRASE_TR = "uygulayamadım"
_PRIOR_TURN_LIMIT = 6
_PRIOR_TURN_EVENT_TYPES = ("user_message", "assistant_response")


def honest_replies_enabled() -> bool:
    return bool(settings.copilot_honest_replies_enabled)


# Plain phrases for op names that can show up in a `Not done:` line.  An op
# missing here is left out of the copy rather than leaking its internal name.
_OP_PHRASES = {
    "set_edit_direction": "change the edit direction",
    "set_clip_label": "label the clips",
    "apply_custom_effect": "add that effect",
    "set_background": "put that behind you",
    "remove_background": "remove the background behind you",
}
_OP_PHRASES_TR = {
    "set_edit_direction": "kurgunun yönünü değiştirmek",
    "set_clip_label": "kliplere etiket vermek",
    "apply_custom_effect": "o efekti eklemek",
    "set_background": "onu arkana koymak",
    "remove_background": "arkandaki arka planı kaldırmak",
}


def _op_label(op: object) -> str:
    key = str(op or "").strip().lower()
    return say(en=_OP_PHRASES.get(key, ""), tr=_OP_PHRASES_TR.get(key, ""))


def _rejection_phrase(reason: object) -> str:
    key = str(reason or "")
    return say(
        en=_REJECTION_PHRASES.get(key, _GENERIC_REJECTION_PHRASE),
        tr=_REJECTION_PHRASES_TR.get(key, _GENERIC_REJECTION_PHRASE_TR),
    )


def _not_done(text: str) -> str:
    return say(en=f"Not done: {text}.", tr=f"Yapılmadı: {text}.")


def _sentence(text: str) -> str:
    text = " ".join(str(text or "").split()).strip()
    return text if not text or text[-1] in ".!?" else f"{text}."


def _not_done_lines(rejections: list[dict], unmet: list[dict]) -> list[str]:
    """One `Not done:` sentence per rejected op and per unmet request part."""
    lines: list[str] = []
    seen: set[str] = set()
    for item in rejections or []:
        if not isinstance(item, dict):
            continue
        phrase = _rejection_phrase(item.get("reason"))
        label = _op_label(item.get("op"))
        line = _not_done(f"{label} ({phrase})" if label else phrase)
        if line not in seen:
            seen.add(line)
            lines.append(line)
    for item in unmet or []:
        if not isinstance(item, dict):
            continue
        request = " ".join(str(item.get("request") or "").split()).strip().rstrip(".")
        if not request:
            continue
        reason = " ".join(str(item.get("reason") or "").split()).strip().rstrip(".")
        line = _not_done(f"{request} ({reason})" if reason else request)
        if line not in seen:
            seen.add(line)
            lines.append(line)
    return lines


def build_honest_reply(
    applied: list[str],
    rejections: list[dict],
    unmet: list[dict],
    *,
    nothing_applied: bool = False,
) -> str:
    """Compose a chat reply only from facts: what applied, what was refused, what was skipped.

    Never uses the model's free-text reply, which can claim a rejected op.
    "Everything else is unchanged." is stated only when nothing was refused or
    skipped, because otherwise it would blur what the creator still has to do.
    """
    not_done = _not_done_lines(rejections, unmet)
    parts: list[str] = []
    if nothing_applied:
        parts.append(say(en="Nothing was changed.", tr="Hiçbir şey değişmedi."))
    else:
        parts.append(_sentence(_applied_summary(applied)))
    parts.extend(not_done)
    if not nothing_applied and not not_done:
        parts.append(say(en="Everything else is unchanged.", tr="Geri kalan her şey aynı."))
    return " ".join(parts)


async def _thread_memory(db: Any, thread: Any, message: str) -> tuple[list[dict], str | None]:
    """Fail-open wrapper: memory is a nicety and must never abort the copilot turn."""
    try:
        return await _load_thread_memory(db, thread, message)
    except Exception:
        log.warning("creation_copilot_memory_failed", exc_info=True)
        return [], None


async def _load_thread_memory(db: Any, thread: Any, message: str) -> tuple[list[dict], str | None]:
    """Last few chat turns + the thread's first creator brief for the copilot.

    The just-appended current user message is dropped from the turns so the
    model does not see it twice.
    """
    thread_id = getattr(thread, "id", None)
    if thread_id is None:
        return [], None
    events = (
        (
            await db.execute(
                select(CreationThreadEvent)
                .where(
                    CreationThreadEvent.thread_id == thread_id,
                    CreationThreadEvent.event_type.in_(_PRIOR_TURN_EVENT_TYPES),
                )
                .order_by(CreationThreadEvent.sequence.desc())
                .limit(_PRIOR_TURN_LIMIT + 2)
            )
        )
        .scalars()
        .all()
    )
    ordered = [e for e in reversed(list(events)) if getattr(e, "content", None)]
    if ordered and ordered[-1].role == "user" and ordered[-1].content == message:
        ordered = ordered[:-1]
    turns: list[dict] = []
    for event in ordered[-_PRIOR_TURN_LIMIT:]:
        turn: dict[str, Any] = {
            "role": "assistant" if event.role == "assistant" else "user",
            "content": str(event.content or ""),
        }
        payload = event.payload if isinstance(event.payload, dict) else {}
        if isinstance(payload.get("applied"), list) and payload["applied"]:
            turn["applied"] = [str(x) for x in payload["applied"]][:6]
        if isinstance(payload.get("not_done"), list) and payload["not_done"]:
            turn["rejected"] = [str(x) for x in payload["not_done"]][:6]
        turns.append(turn)
    first = (
        (
            await db.execute(
                select(CreationThreadEvent.content)
                .where(
                    CreationThreadEvent.thread_id == thread_id,
                    CreationThreadEvent.event_type == "user_message",
                    CreationThreadEvent.role == "user",
                )
                .order_by(CreationThreadEvent.sequence.asc())
                .limit(1)
            )
        )
        .scalars()
        .first()
    )
    original = str(first or "").strip() or str((thread.state or {}).get("intent") or "").strip()
    return turns, (original[:2000] or None)


@_in_chat_language
async def execute_copilot_edit(
    db: Any, thread: Any, body: Any, user: Any, *, job: Any
) -> dict[str, Any] | None:
    """Apply one post-render chat turn through the edit copilot, in place.

    2026-09-19 incident: a creator's chat message about an already-rendered
    cut ("remove the random texts that aren't the title") was routed into the
    Main Creator as a brand-new planning turn, producing a full re-plan and a
    second render. An edit to an existing cut must be applied in the editor,
    never re-planned. This is the server-side equivalent of the web editor's
    Nova chat drawer (`POST .../copilot/turn` + client-side editor-commit):
    since there is no client here to stage ops locally, this runs the same
    copilot turn and, on a genuine edit, applies it directly through
    `compile_editor_ops` + `prepare_editor_commit` + `enqueue_editor_commit_render`
    -- the exact validators/dispatchers the browser's Save button uses, which
    already route a phone-rendered ("device") variant through
    `prepare_phone_editor_commit` (revision N+1, `awaiting_device`) and a
    cloud variant through the normal fast-reburn/full-render task selection.

    Unlike `execute_visual_removal`, the caller keeps its thread/user row
    locks held for the whole turn (no rollback-then-relock dance): this
    function only re-fetches and locks the Job row right before it mutates
    `assembly_plan`, and `prepare_editor_commit`'s own `base_generation`
    compare is what actually catches a concurrent render or Save landing in
    between (surfaced as the usual 409 `baseline_conflict`).

    Returns a result dict when this turn was fully handled here -- an edit
    was applied, needs clarification, or was rejected as unsafe/invalid --
    with keys `thread` (possibly updated), and, only when a render must be
    enqueued after the caller's commit, `job_id` / `variant_id` / `prep`.
    The caller MUST NOT invoke the Main Creator or open a new proposal when
    this returns non-None.

    Returns None when the copilot's own outcome is "unsupported" (`intent ==
    "reject"`, or a `capability_unavailable` / `unknown_operation` rejection
    reason -- see `app.routes._copilot._honest_outcome`): the signal that this
    request needs different footage, direction, format or duration than an
    in-place edit can address. The caller should fall through to the existing
    Main Creator planning turn, unchanged. Note this same "unsupported"
    outcome also covers a capability that is simply unavailable on this one
    variant (e.g. removing music from a variant with none) -- the copilot has
    no separate "needs a new plan" signal today; see the PR notes for that
    follow-up.
    """
    from app.routes import creation_threads as routes

    if len(body.message) > 2000:
        await routes._append(
            db,
            thread,
            event_type="assistant_response",
            role="assistant",
            content=say(
                en="Please shorten this request to 2,000 characters so I can apply it safely.",
                tr="Bu isteği güvenle uygulayabilmem için 2.000 karakterin altına kısaltır mısın?",
            ),
        )
        return {"thread": thread}

    variant = selected_variant(thread, job)
    if variant is None:
        await routes._append(
            db,
            thread,
            event_type="assistant_response",
            role="assistant",
            content=say(
                en="Open the specific video version you want to edit, then ask me again.",
                tr="Düzenlemek istediğin video sürümünü aç, sonra bana tekrar sor.",
            ),
        )
        return {"thread": thread}

    job_id, variant_id = job.id, variant["variant_id"]
    snapshot = build_editor_snapshot(job, copy.deepcopy(variant))
    honest = honest_replies_enabled()
    prior_turns: list[dict] = []
    original_request: str | None = None
    if honest:
        prior_turns, original_request = await _thread_memory(db, thread, body.message)
    response = await run_copilot_turn(
        CopilotTurnBody(
            message=body.message,
            turns=prior_turns,
            original_request=original_request,
            snapshot=snapshot,
            client_contract_version=2,
            client_request_id=hashlib.sha256(
                f"chat-edit:{body.client_event_id}".encode()
            ).hexdigest(),
        ),
        job_id=job_id,
    )
    rejections = list(getattr(response, "rejection_reasons", None) or []) if honest else []
    unmet = list(getattr(response, "unmet_requests", None) or []) if honest else []
    if response.outcome == "unsupported" and not (rejections or unmet):
        # Nothing concrete to report: fall through to the Main Creator (new plan).
        return None

    receipt: dict[str, Any] = {
        "job_id": str(job_id),
        "variant_id": variant_id,
        "outcome": response.outcome,
    }
    if honest and (rejections or unmet):
        receipt["not_done"] = _not_done_lines(rejections, unmet)
    if not response.ops:
        # clarification / no_effect / failed / stale / unsupported-with-facts.  When
        # the copilot refused or skipped part of the ask, the reply is built from
        # those facts: the model's own prose can describe a rejected op as done
        # (KRI-185 East Run).  A pure question/describe turn keeps its real answer.
        reply = response.reply
        if honest and (rejections or unmet):
            intent = getattr(response, "intent", None)
            if response.outcome == "clarification" and not rejections:
                reply = " ".join([response.reply, *_not_done_lines([], unmet)])
            elif not rejections and intent in {"describe", "clarify"}:
                receipt.pop("not_done", None)
            else:
                reply = build_honest_reply([], rejections, unmet, nothing_applied=True)
                if response.outcome == "stale":
                    reply += say(
                        en=" Refresh the editor and try again.",
                        tr=" Düzenleyiciyi yenileyip tekrar dene.",
                    )
        await routes._append(
            db,
            thread,
            event_type="assistant_response",
            role="assistant",
            content=reply,
            payload=receipt,
        )
        return {"thread": thread}

    # Re-fetch and lock the Job row only now, right before mutating it. The
    # thread row lock has been held by the caller since the top of this turn,
    # so `thread.state["selected_variant_id"]` cannot have moved; the render
    # itself can still have moved underneath us (a worker finishing, or a
    # concurrent Save from another tab), which is exactly what
    # `prepare_editor_commit`'s base_generation compare below catches.
    job = await db.get(Job, job_id, populate_existing=True, with_for_update=True)
    variant = selected_variant(thread, job)
    if variant is None or variant.get("variant_id") != variant_id:
        raise HTTPException(status_code=409, detail="Editor target changed")

    prep = None
    plan_before_background_pin = job.assembly_plan
    try:
        compiled = compile_editor_ops(job, variant, response.ops)
        payload = EditorCommitRequest.model_validate(compiled.payload)
        assets = (
            (
                await db.execute(
                    select(PlanItemAsset).where(
                        PlanItemAsset.plan_item_id == thread.active_plan_item_id
                    )
                )
            )
            .scalars()
            .all()
        )
        visual_assets = {
            str(asset.id): {
                "status": asset.status,
                "gcs_path": asset.gcs_path,
                "kind": asset.kind,
                "duration_s": getattr(asset, "duration_s", None),
                "preview_gcs_path": getattr(asset, "preview_gcs_path", None),
                "user_context": getattr(asset, "user_context", None),
            }
            for asset in assets
        }
        if variant.get("resolved_archetype") == "guided_story":
            from app.pipeline.guided_story import (  # noqa: PLC0415
                GuidedStoryError,
                validate_guided_snapshot,
            )
            from app.routes.plan_items import _proposal_media_is_current  # noqa: PLC0415

            item = await db.get(PlanItem, thread.active_plan_item_id)
            try:
                _, _, guided_snapshot = validate_guided_snapshot(
                    (job.assembly_plan or {}).get("guided_edit")
                )
                media_current = await _proposal_media_is_current(
                    item, guided_snapshot, db, user_id=user.id
                )
            except GuidedStoryError as exc:
                raise ValueError("The guided source is stale.") from exc
            if not media_current:
                raise ValueError("The guided source is stale.")
        # KRI-1117: a chat edit that sets a Talking background pins its pool visual first,
        # like the HTTP Save route (never any other Save).
        await pin_background_for_commit(
            db, job, variant_id, payload, plan_item_id=thread.active_plan_item_id
        )
        prep = prepare_editor_commit(
            job,
            variant_id,
            payload,
            user_id=str(user.id),
            plan_item_id=str(thread.active_plan_item_id),
            visual_assets=visual_assets,
            phone_sfx_catalog_paths=await _phone_subtitled_sfx_paths(db, job, variant),
        )
        from app.services.finished_style_learning import (
            explicitly_temporary,
            stage_prepared_style_edit,
        )

        await stage_prepared_style_edit(
            db, user.id, job, variant_id, prep, temporary=explicitly_temporary(body.message)
        )
        if not any((prep["sections"] or {}).values()):
            raise ValueError("This change has no effect on the saved edit.")
        receipt.update(outcome="saved", generation=prep["generation"], sections=prep["sections"])
        if honest:
            receipt["applied"] = list(compiled.changes)[:6]
            reply = build_honest_reply(list(compiled.changes), rejections, unmet)
        else:
            reply = f"{_applied_summary(compiled.changes)}. " + say(
                en="Everything else is unchanged.", tr="Geri kalan her şey aynı."
            )
    except (ValueError, HTTPException) as exc:
        # Validation stages no writes on failure. Do not convert a moved
        # baseline into a misleading assistant response.
        if isinstance(exc, HTTPException) and exc.status_code == 409:
            raise
        # A refused Save leaves no pinned receipt behind.
        job.assembly_plan = plan_before_background_pin
        prep = None
        receipt["outcome"] = "unsupported"
        receipt["rejection"] = _rejection_detail(exc)
        log.warning(
            "creation_copilot_edit_rejected",
            job_id=str(job_id),
            variant_id=variant_id,
            ops=[str(op.get("op") or "") for op in response.ops],
            detail=receipt["rejection"],
        )
        reply = _rejection_reply(exc)
        if honest:
            reply = " ".join([reply, *_not_done_lines(rejections, unmet)])

    await routes._append(
        db,
        thread,
        event_type="assistant_response",
        role="assistant",
        content=reply,
        payload=receipt,
    )
    if prep is None:
        return {"thread": thread}
    return {"thread": thread, "job_id": job_id, "variant_id": variant_id, "prep": prep}


@_in_chat_language
async def finalize_copilot_edit_render(
    db: Any, thread: Any, user: Any, result: dict[str, Any]
) -> None:
    """Enqueue the render staged by `execute_copilot_edit`. Call AFTER db.commit()."""

    prep = result.get("prep")
    if not prep:
        return
    from app.routes import creation_threads as routes

    job_id, variant_id = result["job_id"], result["variant_id"]
    receipt = {"job_id": str(job_id), "variant_id": variant_id, "outcome": "saved"}
    try:
        enqueue_editor_commit_render(str(job_id), variant_id, prep)
    except Exception:
        # Desired state is durable; terminalize only this exact attempt.
        thread_id = str(thread.id)
        thread = await routes._load(thread_id, user, db, lock=True)
        job = await db.get(Job, job_id, populate_existing=True, with_for_update=True)
        plan = copy.deepcopy(job.assembly_plan or {})
        terminalized = False
        for row in plan.get("variants", []):
            if (
                row.get("variant_id") == variant_id
                and row.get("render_generation_id") == prep["generation"]
                and row.get("render_status") == "rendering"
            ):
                row.update(
                    render_status="failed",
                    ok=False,
                    error="The saved render could not be queued.",
                    error_class="render_enqueue_failed",
                )
                terminalized = True
                job.assembly_plan = plan
                if row.get("video_path") or row.get("output_url"):
                    job.status = "variants_ready_partial"
                break
        if terminalized:
            await routes._append(
                db,
                thread,
                event_type="assistant_render_failed",
                role="assistant",
                content=say(
                    en=(
                        "Your change was saved, but rendering could not start. "
                        "Retry the render to update the exported video."
                    ),
                    tr=(
                        "Değişikliğin kaydedildi ama video hazırlanmaya başlayamadı. "
                        "Dışa aktarılan videoyu güncellemek için yeniden dene."
                    ),
                ),
                payload={**receipt, "outcome": "render_enqueue_failed"},
            )
        await db.commit()
