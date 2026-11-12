"""Real-Postgres coverage for ``python -m app.cli.repair_editor_heads``.

Each test is one failure mode from the "Repair CLI" section of
``docs/reviews/latest-wins-convergence/server-failure-modes.md``. Assertions are rows and
the printed report only.
"""

from __future__ import annotations

import uuid

import pytest
from sqlalchemy import text

from app.cli import repair_editor_heads as cli
from app.config import settings
from app.database import engine as async_engine
from app.database import sync_session
from app.models import CreatorEditDraft, Job, PlanItem
from tests.kria.test_editor_head_generation_postgres import (
    VARIANT,
    _add_job,
    _chat_head,
    _committed_sections,
    _flat,
    _head,
    _rows,
    _sections,
    _seed,
    _text,
    _update_variant,
    _variant,
)
from tests.kria.test_runtime_postgres_integration import _seed_runtime_project


@pytest.fixture(autouse=True)
def _flags(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(settings, "kria_runtime_v2_enabled", True)
    monkeypatch.setattr(settings, "main_creator_agent_enabled", True)
    monkeypatch.setattr(
        "app.tasks.kria_runtime.enqueue_editor_commit_render", lambda *_a, **_k: None
    )


async def _stranded(monkeypatch):  # noqa: ANN001, ANN202
    """An AI chat edit on generation-1, then a dispatcher moves the variant to generation-2."""
    user_id, thread_id, item_id, job_id = _seed()
    await _chat_head(monkeypatch, user_id, thread_id, "AI hook")
    _update_variant(job_id, render_generation_id="generation-2")
    return user_id, thread_id, item_id, job_id


def _run(capsys: pytest.CaptureFixture[str], *argv: str) -> tuple[int, str]:
    capsys.readouterr()
    code = cli.main(list(argv))
    return code, capsys.readouterr().out


@pytest.mark.asyncio
async def test_dry_run_reports_the_stranded_head_and_changes_nothing(capsys, monkeypatch) -> None:
    try:
        _user, _thread, item_id, job_id = await _stranded(monkeypatch)
        before = _rows(item_id)
        code, out = _run(capsys, "--item", str(item_id))
        assert code == 0
        head = _head(item_id)
        assert str(head["id"]) in out
        for expected in (
            f"variant {VARIANT}",
            "generation=generation-2",
            "column_base=generation-1",
            "payload_base=generation-1",
            f"base_job_id={job_id}",
            "on_baseline=no",
            "flat lanes: mix, text_elements, timeline_slots; has sections: yes",
            "heal (selected): sections become the committed document; flat lanes kept: mix",
            "retire: becomes the committed document: sections replaced, no flat lane kept",
            "dry run",
        ):
            assert expected in out
        assert _rows(item_id) == before, "a dry run adds, demotes and rewrites nothing"
    finally:
        await async_engine.dispose()


@pytest.mark.asyncio
async def test_apply_heals_by_default_and_a_second_apply_adds_nothing(capsys, monkeypatch) -> None:
    try:
        _user, _thread, item_id, job_id = await _stranded(monkeypatch)
        stranded = _head(item_id)
        code, out = _run(capsys, "--item", str(item_id), "--apply")
        assert code == 0
        assert "heal -> new head=" in out and "on_baseline=yes" in out
        healed = _head(item_id)
        assert healed["id"] != stranded["id"] and healed["parent"] == stranded["id"]
        assert healed["col"] == healed["payload_base"] == "generation-2"
        assert _flat(healed) == _flat(stranded), "the AI's unsaved lanes survive the repair"
        assert _sections(healed) == _committed_sections(job_id)

        rows = len(_rows(item_id))
        code, out = _run(capsys, "--item", str(item_id), "--apply")
        assert code == 0 and "nothing to do" in out
        assert len(_rows(item_id)) == rows, "a second apply adds zero rows"
        assert _head(item_id)["id"] == healed["id"]
    finally:
        await async_engine.dispose()


@pytest.mark.asyncio
async def test_apply_with_the_retire_operation_makes_the_head_the_committed_document(
    capsys, monkeypatch
) -> None:
    try:
        # Stranded by "approval rendered, then the phone published": the video holds the draft.
        _user, _thread, item_id, job_id = await _stranded(monkeypatch)
        _update_variant(
            job_id, text_elements=[{**_variant()["text_elements"][0], "text": "AI hook"}]
        )
        code, out = _run(capsys, "--item", str(item_id), "--apply", "--operation", "retire")
        assert code == 0 and "retire -> new head=" in out
        head = _head(item_id)
        assert head["col"] == head["payload_base"] == "generation-2"
        assert _flat(head) == {} and head["snap"]["changes"] == []
        assert _sections(head) == _committed_sections(job_id) and _text(head) == "AI hook"

        rows = len(_rows(item_id))
        code, out = _run(capsys, "--item", str(item_id), "--apply", "--operation", "retire")
        assert code == 0 and "nothing to do" in out
        assert len(_rows(item_id)) == rows
    finally:
        await async_engine.dispose()


@pytest.mark.asyncio
async def test_apply_repoints_the_pinned_editor_draft(capsys, monkeypatch) -> None:
    try:
        user_id, thread_id, item_id, job_id = _seed()
        await _chat_head(monkeypatch, user_id, thread_id, "AI hook")
        staged = _head(item_id)
        _update_variant(
            job_id,
            editor_state_version=2,
            editor_draft={"draft_id": str(staged["id"])},
            render_generation_id="generation-2",
        )
        _code, out = _run(capsys, "--item", str(item_id))
        assert f"pinned={staged['id']}" in out
        _run(capsys, "--item", str(item_id), "--apply")
        with sync_session() as db:
            variant = db.get(Job, job_id).assembly_plan["variants"][0]
        assert variant["editor_draft"] == {"draft_id": str(_head(item_id)["id"])}
    finally:
        await async_engine.dispose()


@pytest.mark.asyncio
async def test_a_head_from_another_job_is_named_in_the_dry_run(capsys, monkeypatch) -> None:
    try:
        user_id, thread_id, item_id, old_job_id = _seed()
        await _chat_head(monkeypatch, user_id, thread_id, "Old job AI hook")
        new_job_id = _add_job(item_id, user_id, _variant("generation-1", text="New job hook"))
        _code, out = _run(capsys, "--item", str(item_id))
        assert f"base_job_id={old_job_id}" in out and "other job" in out
        _run(capsys, "--item", str(item_id), "--apply")
        assert _head(item_id)["job"] == new_job_id
    finally:
        await async_engine.dispose()


@pytest.mark.asyncio
async def test_missing_job_head_variant_and_item_are_reported_not_crashed(
    capsys, monkeypatch
) -> None:
    try:
        _user, _thread, item_id, _job = _seed()
        code, out = _run(capsys, "--item", str(item_id))  # a job, but no head yet
        assert code == 0 and "no head" in out and "nothing to do" in out
        code, _out = _run(capsys, "--item", str(item_id), "--variant", "nope")
        assert code == 2
        code, _out = _run(capsys, "--item", str(uuid.uuid4()))
        assert code == 2
        _user, thread_id, _session = _seed_runtime_project()
        with sync_session() as db:
            from app.models import CreationThread

            bare = db.get(CreationThread, thread_id).active_plan_item_id
            assert db.get(PlanItem, bare).current_job_id is None
        code, out = _run(capsys, "--item", str(bare), "--apply")
        assert code == 0 and "no current job" in out
    finally:
        await async_engine.dispose()


@pytest.mark.asyncio
async def test_a_non_editor_head_is_never_touched(capsys, monkeypatch) -> None:
    try:
        _user, _thread, item_id, _job = _seed()
        from app.kria.drafts import KriaDraftDocument, canonical_snapshot

        snapshot, digest = canonical_snapshot(KriaDraftDocument(kind="initial", changes=[]))
        with sync_session() as db:
            item = db.get(PlanItem, item_id)
            job = db.get(Job, item.current_job_id)
            db.add(
                CreatorEditDraft(
                    creator_id=job.user_id,
                    thread_id=_thread,
                    item_id=item_id,
                    variant_key=VARIANT,
                    base_job_id=job.id,
                    base_generation_id="old",
                    draft_revision=0,
                    snapshot_json=snapshot,
                    snapshot_hash=digest,
                    is_head=True,
                )
            )
            db.commit()
        before = _rows(item_id)
        _code, out = _run(capsys, "--item", str(item_id), "--apply")
        assert "no editor payload" in out
        assert _rows(item_id) == before
        _code, out = _run(capsys, "--list-stranded")
        assert str(item_id) not in out
    finally:
        await async_engine.dispose()


@pytest.mark.asyncio
async def test_list_stranded_finds_the_item_until_it_is_repaired(capsys, monkeypatch) -> None:
    try:
        _user, _thread, item_id, _job_id = await _stranded(monkeypatch)
        _code, out = _run(capsys, "--list-stranded", "--limit", "500")
        assert str(item_id) in out and "payload_base=generation-1" in out
        _run(capsys, "--item", str(item_id), "--apply")
        _code, out = _run(capsys, "--list-stranded", "--limit", "500")
        assert str(item_id) not in out
    finally:
        await async_engine.dispose()


@pytest.mark.asyncio
async def test_list_stranded_survives_a_job_whose_variants_is_not_an_array(
    capsys, monkeypatch
) -> None:
    try:
        user_id, thread_id, item_id, job_id = _seed()
        await _chat_head(monkeypatch, user_id, thread_id, "AI hook")
        with sync_session() as db:
            db.execute(
                text(
                    'update jobs set assembly_plan = \'{"variants": "oops"}\'::jsonb where id = :j'
                ),
                {"j": job_id},
            )
            db.commit()
        code, out = _run(capsys, "--list-stranded", "--limit", "500")
        assert code == 0 and str(item_id) not in out
    finally:
        await async_engine.dispose()
