"""Consented, provider-free learning from validated saves and verified exports.

Save -> normalized delta -> exact exported recipe -> distinct project vote
     -> latest five -> inactive proposal -> existing revision-checked approval.
No raw words, messages, filenames, frames or media are retained here.
"""

from __future__ import annotations

import copy
import hashlib
import json
import uuid
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import delete, func, or_, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.models import (
    ContentPlan,
    CreationThread,
    CreationThreadEvent,
    CreatorMemoryItem,
    CreatorMemoryOperation,
    CreatorStyleLearningProject,
    PlanItem,
    User,
)
from app.schemas.phone_text_style import TEXT_STYLE_ROLES, TextStyleRule
from app.services.creator_direction import (
    CreatorDirectionService,
    DirectionError,
    LimitReached,
    request_fingerprint,
)

service = CreatorDirectionService()
PROPERTIES = ("font_family", "position", "color", "stroke_width", "shadow_enabled")
TEMPORARY_MARKERS = (
    "just this once",
    "only this video",
    "only for this video",
    "for this video only",
    "just for this video",
    "this project only",
    "only this time",
    "this time only",
    "temporarily",
    "sadece bu video",
    "sadece bu videoda",
    "sadece bu sefer",
    "bu videoya özel",
    "geçici",
)


def learning_available(user_id: uuid.UUID) -> bool:
    return bool(
        settings.creator_memory_enabled
        and settings.creator_edit_learning_enabled
        and settings.creator_edit_learning_privacy_approved
        and str(user_id) in settings.creator_edit_learning_user_ids
    )


def explicitly_temporary(message: str | None) -> bool:
    return isinstance(message, str) and any(
        marker in message.casefold() for marker in TEMPORARY_MARKERS
    )


def _position(row: dict) -> str | None:
    position = row.get("position")
    if position != "custom":
        return (
            position
            if position
            in ("top_left", "top", "top_right", "middle", "bottom_left", "bottom", "bottom_right")
            else None
        )
    x, y = row.get("x_frac"), row.get("y_frac")
    if not all(
        isinstance(v, (float, int)) and not isinstance(v, bool) and 0 <= v <= 1 for v in (x, y)
    ):
        return None
    vertical = "top" if y < 0.34 else "bottom" if y > 0.66 else "middle"
    horizontal = "left" if x < 0.34 else "right" if x > 0.66 else ""
    return (
        vertical + ("_" + horizontal if horizontal else "")
        if vertical != "middle"
        else ("middle" if not horizontal else None)
    )


def _values(row: dict) -> dict:
    raw = {prop: row.get(prop) for prop in PROPERTIES}
    raw["position"] = _position(row)
    if isinstance(raw["color"], str):
        raw["color"] = raw["color"].upper()
    result = {}
    for prop, value in raw.items():
        if value is None:
            continue
        try:
            valid = TextStyleRule.model_validate({prop: value}).values()
        except ValueError:
            continue
        result.update(valid)
    return result


def style_snapshot(variant: dict, sections: set[str]) -> dict:
    from app.pipeline.phone_text_style import text_role

    result = {}
    if "text_elements" in sections or "guided_revision" in sections:
        for row in (variant.get("text_elements") or [])[:500]:
            if not isinstance(row, dict) or not isinstance(row.get("id"), str):
                continue
            key = hashlib.sha256(row["id"].encode()).hexdigest()
            result[key] = {"role": text_role(row), "values": _values(row)}
    if "caption_meta" in sections or "guided_revision" in sections:
        from app.pipeline.phone_captions import caption_look_from_variant

        look = caption_look_from_variant(variant)
        result["captions"] = {
            "role": "caption",
            "values": _values(
                {
                    "font_family": look.font_family,
                    "color": look.text_color,
                    "stroke_width": look.outline_px,
                    "shadow_enabled": look.shadow_enabled,
                    "position": "custom",
                    "x_frac": look.position_x_frac,
                    "y_frac": look.position_y_frac,
                }
            ),
        }
    return result


async def _project(
    db: AsyncSession, user: User, job: Any, variant_id: str, digest: str
) -> CreatorStyleLearningProject | None:
    item_id = getattr(job, "content_plan_item_id", None)
    if item_id is None or job.user_id != user.id:
        return None
    owned = await db.scalar(
        select(PlanItem.id)
        .join(ContentPlan)
        .where(
            PlanItem.id == item_id,
            ContentPlan.user_id == user.id,
            PlanItem.current_job_id == job.id,
        )
    )
    if owned is None:
        return None
    row = await db.scalar(
        select(CreatorStyleLearningProject)
        .where(
            CreatorStyleLearningProject.user_id == user.id,
            CreatorStyleLearningProject.plan_item_id == item_id,
        )
        .with_for_update()
    )
    if row is None:
        unfinished = await db.scalar(
            select(func.count())
            .select_from(CreatorStyleLearningProject)
            .where(
                CreatorStyleLearningProject.user_id == user.id,
                CreatorStyleLearningProject.finished_at.is_(None),
            )
        )
        if int(unfinished or 0) >= 20:
            return None
        row = CreatorStyleLearningProject(
            user_id=user.id,
            plan_item_id=item_id,
            job_id=job.id,
            variant_id=variant_id,
            consent_epoch=user.creator_edit_learning_epoch,
            pending_recipe_digest=digest,
            pending_changes={},
            finished_changes={},
        )
        db.add(row)
        await db.flush()
    if row.job_id != job.id or row.variant_id != variant_id:
        row.pending_changes = {}
    row.job_id = job.id
    row.variant_id = variant_id
    row.pending_recipe_digest = digest
    return row


def _merge_pending(prior: dict, old: dict, new: dict, temporary: bool) -> dict:
    pending = copy.deepcopy(prior)
    # Keep only surviving lanes. Deleted or undone text cannot vote.
    for key in old.keys() - new.keys():
        pending.pop(key, None)
    for key in old.keys() & new.keys():
        if old[key]["role"] != new[key]["role"]:
            continue
        edits = pending.get(key, {"role": new[key]["role"], "properties": {}})
        for prop in PROPERTIES:
            previous, current = old[key]["values"].get(prop), new[key]["values"].get(prop)
            if previous == current:
                continue
            if previous is None and prop not in edits["properties"]:
                # An implicit legacy renderer value is not a demonstrated
                # style change. New generation styles carry resolved values.
                continue
            original = edits["properties"].get(prop, {}).get("from", previous)
            if temporary or current is None or current == original:
                edits["properties"].pop(prop, None)
            else:
                edits["properties"][prop] = {"from": original, "to": current}
        if edits["properties"]:
            pending[key] = edits
        else:
            pending.pop(key, None)
    return pending


async def stage_style_edit(
    db: AsyncSession,
    user_id: uuid.UUID,
    job: Any,
    variant_id: str,
    before: dict,
    after: dict,
    *,
    sections: set[str],
    identity: Any,
    temporary: bool = False,
    normalized: bool = False,
) -> None:
    if not learning_available(user_id):
        return
    user = await service._user(db, user_id, lock=True)
    if not user.creator_edit_learning_enabled:
        return
    row = await _project(db, user, job, variant_id, identity.recipe_digest)
    if row is None:
        return
    old = before if normalized else style_snapshot(before, sections)
    new = after if normalized else style_snapshot(after, sections)
    row.pending_changes = _merge_pending(row.pending_changes or {}, old, new, temporary)

    await db.flush()


async def stage_prepared_style_edit(
    db: AsyncSession,
    user_id: uuid.UUID,
    job: Any,
    variant_id: str,
    prep: dict,
    *,
    temporary: bool = False,
) -> None:
    transition = prep.pop("style_learning_transition", None)
    if not isinstance(transition, dict):
        return
    from app.services.device_render import device_status

    status = device_status(job, variant_id)
    if status is None:
        return
    if not learning_available(user_id):
        return
    user = await service._user(db, user_id, lock=True)
    if not user.creator_edit_learning_enabled:
        return
    if learning_available(user_id):
        initial_request = await db.scalar(
            select(CreationThreadEvent.content)
            .join(CreationThread)
            .where(
                CreationThread.creator_id == user_id,
                CreationThread.active_plan_item_id == getattr(job, "content_plan_item_id", None),
                CreationThreadEvent.event_type == "user_message",
            )
            .order_by(CreationThreadEvent.sequence)
            .limit(1)
        )
        latest_request = await db.scalar(
            select(CreationThreadEvent.content)
            .join(CreationThread)
            .where(
                CreationThread.creator_id == user_id,
                CreationThread.active_plan_item_id == getattr(job, "content_plan_item_id", None),
                CreationThreadEvent.event_type == "user_message",
            )
            .order_by(CreationThreadEvent.sequence.desc())
            .limit(1)
        )
        temporary = (
            temporary
            or explicitly_temporary(initial_request)
            or explicitly_temporary(latest_request)
        )
    await stage_style_edit(
        db,
        user_id,
        job,
        variant_id,
        transition["before"],
        transition["after"],
        sections=set(),
        identity=status.request.identity,
        normalized=True,
        temporary=temporary,
    )


def stage_prepared_style_edit_sync(
    db, user_id: uuid.UUID, job: Any, variant_id: str, prep: dict, *, temporary: bool = False
) -> None:
    """Celery's existing synchronous transaction uses the same delta projection."""
    transition = prep.pop("style_learning_transition", None)
    if not isinstance(transition, dict) or not learning_available(user_id):
        return
    user = db.scalar(
        select(User)
        .where(User.id == user_id)
        .with_for_update()
        .execution_options(populate_existing=True)
    )
    if user is None or not user.creator_edit_learning_enabled or job.user_id != user_id:
        return
    item_id = job.content_plan_item_id
    owned = db.scalar(
        select(PlanItem.id)
        .join(ContentPlan)
        .where(
            PlanItem.id == item_id,
            ContentPlan.user_id == user_id,
            PlanItem.current_job_id == job.id,
        )
    )
    if owned is None:
        return
    from app.services.device_render import device_status

    status = device_status(job, variant_id)
    if status is None:
        return
    digest = status.request.identity.recipe_digest
    row = db.scalar(
        select(CreatorStyleLearningProject)
        .where(
            CreatorStyleLearningProject.user_id == user_id,
            CreatorStyleLearningProject.plan_item_id == item_id,
        )
        .with_for_update()
    )
    if row is None:
        unfinished = db.scalar(
            select(func.count())
            .select_from(CreatorStyleLearningProject)
            .where(
                CreatorStyleLearningProject.user_id == user_id,
                CreatorStyleLearningProject.finished_at.is_(None),
            )
        )
        if int(unfinished or 0) >= 20:
            return
        row = CreatorStyleLearningProject(
            user_id=user_id,
            plan_item_id=item_id,
            job_id=job.id,
            variant_id=variant_id,
            consent_epoch=user.creator_edit_learning_epoch,
            pending_recipe_digest=digest,
            pending_changes={},
            finished_changes={},
        )
        db.add(row)
    prior = row.pending_changes if row.job_id == job.id and row.variant_id == variant_id else {}
    row.pending_changes = _merge_pending(
        prior or {}, transition["before"], transition["after"], temporary
    )
    row.job_id = job.id
    row.variant_id = variant_id
    row.pending_recipe_digest = digest
    db.flush()


def _votes(pending: dict) -> dict:
    values: dict[str, dict[str, list]] = {}
    for lane in pending.values():
        role = lane["role"]
        if role not in TEXT_STYLE_ROLES:
            continue
        for prop, change in lane["properties"].items():
            if prop in PROPERTIES:
                values.setdefault(role, {}).setdefault(prop, []).append(change)
    # A role/property gets one vote only when all demonstrated changes agree.
    return {
        role: {
            prop: changes[0]
            for prop, changes in props.items()
            if all(c["to"] == changes[0]["to"] for c in changes)
        }
        for role, props in values.items()
    }


async def record_finished_style_edit(
    db: AsyncSession, user_id: uuid.UUID, job: Any, variant_id: str, status: Any
) -> None:
    if not learning_available(user_id) or status.phase != "published":
        return
    user = await service._user(db, user_id, lock=True)
    if not user.creator_edit_learning_enabled:
        return
    row = await db.scalar(
        select(CreatorStyleLearningProject)
        .where(
            CreatorStyleLearningProject.user_id == user_id,
            CreatorStyleLearningProject.plan_item_id == getattr(job, "content_plan_item_id", None),
        )
        .with_for_update()
    )
    digest = status.request.identity.recipe_digest
    if (
        row is None
        or row.job_id != job.id
        or row.variant_id != variant_id
        or row.pending_recipe_digest != digest
        or row.consent_epoch != user.creator_edit_learning_epoch
        or row.published_recipe_digest == digest
    ):
        return
    row.finished_changes = _votes(row.pending_changes or {})
    row.published_recipe_digest = digest
    row.finished_at = datetime.now(UTC)
    await db.flush()
    await _suggest(db, user)
    older = (
        select(CreatorStyleLearningProject.id)
        .where(
            CreatorStyleLearningProject.user_id == user_id,
            CreatorStyleLearningProject.finished_at.is_not(None),
        )
        .order_by(CreatorStyleLearningProject.finished_at.desc(), CreatorStyleLearningProject.id)
        .offset(5)
    )
    await db.execute(
        delete(CreatorStyleLearningProject).where(CreatorStyleLearningProject.id.in_(older))
    )


def _signature(role: str, prop: str, value: Any) -> str:
    return hashlib.sha256(json.dumps([role, prop, value], sort_keys=True).encode()).hexdigest()


async def _suggest(db: AsyncSession, user: User) -> None:
    projects = list(
        (
            await db.scalars(
                select(CreatorStyleLearningProject)
                .where(
                    CreatorStyleLearningProject.user_id == user.id,
                    CreatorStyleLearningProject.finished_at.is_not(None),
                    CreatorStyleLearningProject.consent_epoch == user.creator_edit_learning_epoch,
                )
                .order_by(
                    CreatorStyleLearningProject.finished_at.desc(), CreatorStyleLearningProject.id
                )
                .limit(5)
            )
        ).all()
    )
    candidates = {}
    for role in TEXT_STYLE_ROLES:
        for prop in PROPERTIES:
            relevant = [(p, p.finished_changes.get(role, {}).get(prop)) for p in projects]
            relevant = [(p, v) for p, v in relevant if v is not None]
            if not relevant:
                continue
            latest = relevant[0][1]["to"]
            support = [p for p, v in relevant if v["to"] == latest]
            if len(support) >= 3:
                target_prop, value = (
                    ("favorite_fonts", [latest]) if prop == "font_family" else (prop, latest)
                )
                candidates[_signature(role, target_prop, value)] = (
                    role,
                    target_prop,
                    value,
                    support,
                )
            if prop == "font_family":
                rejected = relevant[0][1]["from"]
                support = [p for p, v in relevant if v["from"] == rejected]
                replacements = {v["to"] for _, v in relevant if v["from"] == rejected}
                if rejected and len(support) >= 3 and len(replacements) >= 2:
                    candidates[_signature(role, "excluded_fonts", [rejected])] = (
                        role,
                        "excluded_fonts",
                        [rejected],
                        support,
                    )
    existing = list(
        (
            await db.scalars(
                select(CreatorMemoryItem).where(
                    CreatorMemoryItem.user_id == user.id,
                    CreatorMemoryItem.source_kind == "finished_edits",
                    or_(
                        CreatorMemoryItem.state == "suggested",
                        CreatorMemoryItem.evidence["signature"].astext.in_(list(candidates)),
                    ),
                )
            )
        ).all()
    )
    for item in existing:
        signature = (item.evidence or {}).get("signature")
        if item.state == "suggested" and signature not in candidates:
            item.state = "superseded"
            user.creator_memory_revision += 1
    for signature, (role, prop, value, support) in candidates.items():
        matched = [i for i in existing if (i.evidence or {}).get("signature") == signature]
        if any(i.state in ("dismissed", "active") for i in matched):
            continue
        evidence = {
            "version": 1,
            "signature": signature,
            "role": role,
            "property": prop,
            "supporting_video_count": len(support),
            "project_ids": [str(p.plan_item_id) for p in support],
            "consent_epoch": user.creator_edit_learning_epoch,
        }
        current = next((i for i in matched if i.state == "suggested"), None)
        if current:
            if current.evidence != evidence:
                current.evidence = evidence
                user.creator_memory_revision += 1
            continue
        key = f"text_style_{role}"
        active = await db.scalar(
            select(CreatorMemoryItem).where(
                CreatorMemoryItem.user_id == user.id,
                CreatorMemoryItem.normalized_key == key,
                CreatorMemoryItem.state == "active",
            )
        )
        if active:
            current_value = (active.structured_value or {}).get(key, {}).get(prop)
            if current_value == value or (
                isinstance(value, list) and set(value).issubset(current_value or [])
            ):
                continue
        instruction = _instruction(role, prop, value)
        if (
            prop == "excluded_fonts"
            and active
            and (active.structured_value or {}).get(key, {}).get("font_family") in value
        ):
            instruction = f"Use automatic {role} fonts and exclude {value[0]} from selection."
        window_digest = hashlib.sha256(
            json.dumps(
                [(str(p.plan_item_id), p.published_recipe_digest) for p in projects]
            ).encode()
        ).hexdigest()[:24]
        try:
            op = await service.create_item(
                db,
                user.id,
                instruction=instruction,
                category="video_style",
                enforcement="default",
                normalized_key=key,
                structured_value={key: {prop: value}},
                expected_revision=None,
                idempotency_key=f"finished:{user.creator_edit_learning_epoch}:{signature}:{window_digest}",
                source_kind="finished_edits",
                user_locked=False,
                initial_state="suggested",
            )
        except LimitReached:
            break
        item = await db.get(CreatorMemoryItem, op.item_id)
        item.evidence = evidence
    await db.flush()


def _instruction(role: str, prop: str, value: Any) -> str:
    label = role.capitalize()
    if prop == "favorite_fonts":
        return f"Favor {value[0]} when automatically choosing {role} fonts."
    if prop == "excluded_fonts":
        return f"Exclude {value[0]} from automatic {role} text selection."
    if prop == "position":
        return f"Place {role} text at {value.replace('_', ' ')} in future videos."
    if prop == "color":
        return f"Use {value} for {role} text in future videos."
    if prop == "stroke_width":
        return f"{label} outlines {'off' if value == 0 else f'{value:g}px'} in future videos."
    return f"{label} shadows {'on' if value else 'off'} in future videos."


async def _clear_history(db: AsyncSession, user: User) -> None:
    await db.execute(
        delete(CreatorStyleLearningProject).where(CreatorStyleLearningProject.user_id == user.id)
    )
    items = list(
        (
            await db.scalars(
                select(CreatorMemoryItem).where(
                    CreatorMemoryItem.user_id == user.id,
                    CreatorMemoryItem.source_kind == "finished_edits",
                )
            )
        ).all()
    )
    all_ids = [i.id for i in items]
    if all_ids:
        await db.execute(
            update(CreatorMemoryOperation)
            .where(
                CreatorMemoryOperation.user_id == user.id,
                CreatorMemoryOperation.item_id.in_(all_ids),
            )
            .values(undo_expires_at=None, prior_state=None)
        )
    removed = [i.id for i in items if i.state != "active"]
    # Remove historical operation payloads as well; Undo must not recreate revoked evidence.
    if removed:
        await db.execute(
            delete(CreatorMemoryOperation).where(
                CreatorMemoryOperation.user_id == user.id,
                CreatorMemoryOperation.item_id.in_(removed),
            )
        )
        await db.execute(delete(CreatorMemoryItem).where(CreatorMemoryItem.id.in_(removed)))
    for item in items:
        if item.state == "active":
            item.evidence = None
            item.source_kind = "profile"
    user.creator_edit_learning_epoch += 1


async def _control(
    db: AsyncSession,
    user_id: uuid.UUID,
    expected_revision: int,
    idempotency_key: str,
    enabled: bool | None,
) -> CreatorMemoryOperation:
    user = await service._user(db, user_id, lock=True)
    kind = "reset_edit_learning" if enabled is None else "set_edit_learning"
    fingerprint = request_fingerprint(
        kind, {"enabled": enabled, "expected_revision": expected_revision}
    )
    duplicate = await service._op(db, user_id, idempotency_key, fingerprint)
    if duplicate:
        return duplicate
    service._check_revision(user, expected_revision)
    if enabled is True and not learning_available(user_id):
        raise DirectionError("Finished-edit learning is not available for this account yet")
    if enabled is None or enabled is False:
        await _clear_history(db, user)
    if enabled is not None:
        user.creator_edit_learning_enabled = enabled
    user.creator_memory_revision += 1
    op = CreatorMemoryOperation(
        user_id=user_id,
        idempotency_key=idempotency_key,
        request_fingerprint=fingerprint,
        operation_kind=kind,
        resulting_revision=user.creator_memory_revision,
        actor_kind="user",
    )
    db.add(op)
    await db.flush()
    return op


async def set_learning_enabled(db, user_id, enabled, expected_revision, idempotency_key):
    return await _control(db, user_id, expected_revision, idempotency_key, enabled)


async def reset_learning_history(db, user_id, expected_revision, idempotency_key):
    return await _control(db, user_id, expected_revision, idempotency_key, None)


async def erase_project_learning(
    db: AsyncSession, user_id: uuid.UUID, project_id: uuid.UUID
) -> None:
    """Deleting a project also removes its evidence links; approved rules remain."""
    user = await service._user(db, user_id, lock=True)
    await db.execute(
        delete(CreatorStyleLearningProject).where(
            CreatorStyleLearningProject.user_id == user_id,
            CreatorStyleLearningProject.plan_item_id == project_id,
        )
    )
    items = list(
        (
            await db.scalars(
                select(CreatorMemoryItem).where(
                    CreatorMemoryItem.user_id == user_id,
                    CreatorMemoryItem.source_kind == "finished_edits",
                    CreatorMemoryItem.evidence["project_ids"].contains([str(project_id)]),
                )
            )
        ).all()
    )
    for item in items:
        evidence = copy.deepcopy(item.evidence)
        evidence["project_ids"] = [
            value for value in evidence["project_ids"] if value != str(project_id)
        ]
        evidence["supporting_video_count"] = len(evidence["project_ids"])
        item.evidence = evidence
        if item.state == "suggested" and len(evidence["project_ids"]) < 3:
            item.state = "superseded"
        user.creator_memory_revision += 1
    await db.flush()
