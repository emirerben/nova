"""Offline candidate -> real scratch draft -> committed reopen/undo evidence.

No provider, broker, render, upload or production write. Model inputs stay frozen;
the editor execution base is separately labeled as a reconstruction of recorded
lanes on an owned persisted job. All scratch identities are fresh and removed.
"""

from __future__ import annotations

import copy
import uuid
from types import SimpleNamespace

from sqlalchemy import delete, update
from sqlalchemy.engine import make_url

from app.services.request_following_cases import digest


def require_scratch_database(url):
    parsed = make_url(url)
    if (
        parsed.get_backend_name() != "postgresql"
        or parsed.host not in ("localhost", "127.0.0.1", "::1")
        or not (parsed.database or "").endswith("_test")
        # libpq query options can override the URL's apparent host/database.
        # An omitted host can instead inherit a remote PGHOST/PGSERVICE.
        or parsed.query
    ):
        raise ValueError("comparison persistence requires local PostgreSQL *_test database")


def editor_execution_base(case, context):
    from app.services.kria_editor_ops import project_editor_draft

    source = copy.deepcopy((context or {}).get("job") or {})
    if str(source.get("id")) != case.get("source_job_id") or str(source.get("user_id")) != case.get(
        "creator_id"
    ):
        raise ValueError("execution context does not match owned source")
    variants = (source.get("assembly_plan") or {}).get("variants") or []
    requested = (context or {}).get("variant_id")
    chosen = [v for v in variants if isinstance(v, dict) and v.get("variant_id") == requested]
    if requested is None and len(variants) == 1:
        chosen = variants
    if len(chosen) != 1:
        raise ValueError("one explicit execution variant required")
    snapshot = case["input"]["variant_snapshot"]
    variant = copy.deepcopy(chosen[0])
    # Preserve source identity/capabilities but use the actual pre-turn lanes.
    # This is not an assertion that the complete historical execution base exists.
    variant["render_generation_id"] = str(snapshot.get("base_generation") or "")
    job = SimpleNamespace(**source)
    variant = project_editor_draft(
        variant,
        {
            "text_elements": snapshot.get("text_bars", []),
            "timeline_slots": snapshot.get("slots", []),
        },
        job,
    )
    return job, variant


async def verify_saved_candidate(case, output, *, execution_context=None):
    from app.config import settings

    require_scratch_database(settings.database_url)
    if case.get("input_sha256") != digest(case.get("input")):
        raise ValueError("candidate input hash mismatch")

    from app.database import AsyncSessionLocal
    from app.kria.drafts import (
        KriaDraftDocument,
        read_or_bootstrap_draft,
        undo_draft,
        write_draft,
    )
    from app.kria.runtime import RuntimeFailure
    from app.models import ContentPlan, CreationThread, Job, Persona, PlanItem, User
    from app.services.kria_editor_ops import (
        compile_editor_ops,
        editor_payload_dump,
        merge_editor_draft,
    )

    agent = case.get("agent")
    variant = None
    job_source = None
    compiled = None
    strategy = None
    if agent == "main_creator":
        from app.agents.main_creator import MainCreatorAgent
        from app.services.creator_capabilities import compile_strategy_to_plan

        inp = MainCreatorAgent.Input.model_validate(case["input"])
        response = MainCreatorAgent.Output.model_validate(output)
        strategy = getattr(response.action, "strategy", None)
        if strategy is not None:
            compile_strategy_to_plan(inp.capability_manifest, strategy)
    elif agent == "edit_copilot":
        if output.get("ops"):
            job_source, variant = editor_execution_base(case, execution_context)
            before = copy.deepcopy(variant)
            compiled = compile_editor_ops(job_source, variant, copy.deepcopy(output["ops"]))
            if before != variant:
                raise ValueError("compiler mutated execution base")
    else:
        raise ValueError("unsupported comparison agent")
    if strategy is None and compiled is None:
        return {"status": "not_applicable", "reason": "no_actionable_candidate", "rendered": False}

    owner_id, plan_id, item_id, thread_id, job_id, persona_id = (uuid.uuid4() for _ in range(6))
    result = {
        "status": "verified",
        "agent": agent,
        "input_sha256": case["input_sha256"],
        "source_kind": case.get("source_kind"),
        "rendered": False,
        "execution_base": (
            "reconstructed_from_recorded_lanes_and_owned_job"
            if variant
            else "fresh_scratch_project"
        ),
        "checks": {},
    }
    try:
        async with AsyncSessionLocal() as db:
            db.add(User(id=owner_id, email=f"comparison-{owner_id}@scratch.test"))
            await db.flush()
            db.add(Persona(id=persona_id, user_id=owner_id, persona_status="ready", persona={}))
            await db.flush()
            db.add(ContentPlan(id=plan_id, user_id=owner_id, persona_id=persona_id))
            await db.flush()
            item = PlanItem(
                id=item_id, content_plan_id=plan_id, position=1, idea="Private comparison"
            )
            db.add(item)
            await db.flush()
            if variant is not None:
                plan = copy.deepcopy(job_source.assembly_plan)
                plan["variants"] = [copy.deepcopy(variant)]
                db.add(
                    Job(
                        id=job_id,
                        user_id=owner_id,
                        content_plan_item_id=item_id,
                        raw_storage_path=f"scratch/{owner_id}/no-render",
                        status="variants_ready",
                        mode=getattr(job_source, "mode", None),
                        assembly_plan=plan,
                        all_candidates=copy.deepcopy(getattr(job_source, "all_candidates", None)),
                    )
                )
                await db.flush()
                item.current_job_id = job_id
            db.add(
                CreationThread(
                    id=thread_id,
                    creator_id=owner_id,
                    runtime_version=2,
                    content_plan_id=plan_id,
                    active_plan_item_id=item_id,
                    revision=0,
                    state={"selected_variant_id": variant["variant_id"]} if variant else {},
                )
            )
            await db.commit()

        async with AsyncSessionLocal() as db:
            initial = await read_or_bootstrap_draft(db, thread_id=thread_id, creator_id=owner_id)
        if strategy is not None:
            document = KriaDraftDocument(
                kind="strategy",
                strategy=strategy.model_dump(mode="json"),
                intent=case["input"].get("user_message", "")[:4000],
                edit_format=strategy.edit_format,
                changes=["Prepared model strategy"],
            )
        else:
            document = KriaDraftDocument(
                kind="editor",
                edit_format=initial.snapshot["edit_format"],
                editor_payload=merge_editor_draft(
                    initial.snapshot["editor_payload"],
                    editor_payload_dump(compiled.payload),
                ),
                editor_text_diff=compiled.text_diff,
                changes=compiled.changes,
            )
        async with AsyncSessionLocal() as db:
            saved = await write_draft(
                db,
                thread_id=thread_id,
                creator_id=owner_id,
                expected_revision=initial.draft_revision,
                expected_etag=initial.etag,
                snapshot=document.model_dump(mode="json"),
            )
        async with AsyncSessionLocal() as db:
            reopened = await read_or_bootstrap_draft(db, thread_id=thread_id, creator_id=owner_id)
        if reopened.snapshot != saved.snapshot or reopened.snapshot_hash != saved.snapshot_hash:
            raise ValueError("saved candidate did not reopen exactly")
        result["checks"]["committed_reopen"] = True
        async with AsyncSessionLocal() as db:
            try:
                await write_draft(
                    db,
                    thread_id=thread_id,
                    creator_id=owner_id,
                    expected_revision=initial.draft_revision,
                    expected_etag=initial.etag,
                    snapshot=document.model_dump(mode="json"),
                )
            except RuntimeFailure as exc:
                if exc.code != "draft_stale":
                    raise
            else:
                raise ValueError("stale draft write unexpectedly accepted")
        result["checks"]["stale_write_rejected"] = True
        async with AsyncSessionLocal() as db:
            restored, _ = await undo_draft(
                db, thread_id=thread_id, creator_id=owner_id, expected_revision=saved.draft_revision
            )
        if restored.snapshot != initial.snapshot:
            raise ValueError("undo did not restore exact prior snapshot")
        result["checks"]["undo_restores_prior"] = True
        async with AsyncSessionLocal() as db:
            final = await write_draft(
                db,
                thread_id=thread_id,
                creator_id=owner_id,
                expected_revision=restored.draft_revision,
                expected_etag=restored.etag,
                snapshot=saved.snapshot,
            )
        async with AsyncSessionLocal() as db:
            final_read = await read_or_bootstrap_draft(db, thread_id=thread_id, creator_id=owner_id)
        if final_read.snapshot != saved.snapshot or final.snapshot != saved.snapshot:
            raise ValueError("restored candidate changed after reopen")
        result["checks"]["restore_reopens_exactly"] = True
        result["saved_snapshot"] = final_read.snapshot
        result["saved_snapshot_sha256"] = final_read.snapshot_hash
    finally:
        async with AsyncSessionLocal() as db:
            await db.execute(delete(CreationThread).where(CreationThread.id == thread_id))
            await db.execute(
                update(PlanItem)
                .where(PlanItem.id == item_id, PlanItem.content_plan_id == plan_id)
                .values(current_job_id=None)
            )
            await db.execute(delete(Job).where(Job.id == job_id, Job.user_id == owner_id))
            await db.execute(delete(User).where(User.id == owner_id))
            await db.commit()
        async with AsyncSessionLocal() as db:
            if await db.get(User, owner_id) is not None:
                raise ValueError("scratch owner cleanup failed")
        result["checks"]["scratch_rows_removed"] = True
    return result
