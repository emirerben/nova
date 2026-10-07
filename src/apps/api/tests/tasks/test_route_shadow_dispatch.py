"""KRI-470 PR-D: shadow-mode route comparison through the REAL dispatchers.

The phone fork and the cloud entry of ``_run_generative_job_impl`` run for real; only the
render entries (``_run_phone_*``, ``_run_guided_story_job``, ...) are replaced by recorders.
What these prove:

* a stamped job where the resolver and the legacy decision agree records nothing;
* where they disagree it records exactly ONE ``route_mismatch`` and the LEGACY entry still
  runs (shadow mode renders nothing different);
* a fault inside the resolver or the trace write never breaks the dispatch;
* an unstamped job records nothing AND never even builds the resolver inputs.

Failure modes written first: (1) the event is recorded while the legacy branch is skipped;
(2) a resolver exception propagates into the render; (3) unstamped jobs pay for / are changed
by the shadow; (4) the event leaks request text, URLs or user ids; (5) the event fires twice.
"""

from __future__ import annotations

import ast
import inspect
import json
import textwrap
import uuid
from contextlib import contextmanager
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from app.agents._schemas.creator_agent import CreativeStrategy
from app.agents._schemas.edit_format import PHONE_RENDER_SUPPORTED_FORMATS
from app.services import render_route
from app.services.creator_render_contract import (
    CONTRACT_FIELD,
    PLAN_AUTHORITY_FIELD,
    REQUIREMENT_VERSION_FIELD,
    build_render_contract,
    read_render_contract,
)
from app.services.generative_jobs import CREATOR_RENDER_CONTRACT_VERSION
from app.services.phone_sources import PHONE_SOURCES_FIELD
from app.tasks import generative_build as gb

SECRET = "USE-MY-SECRET-WORDS-https://example.test/x?sig=abc"


def _strategy(**fields) -> dict:
    return CreativeStrategy.model_validate(fields).model_dump(mode="json", exclude_none=True)


def _job(
    *,
    platform: str,
    edit_format: str = "montage",
    strategy: dict | None = None,
    voiceover: bool = False,
    song: dict | None = None,
    guided: bool = False,
    clips: int = 3,
    stamped: bool = True,
    route_stamp: bool = False,
) -> SimpleNamespace:
    strategy = strategy if strategy is not None else _strategy(edit_format=edit_format)
    contract = build_render_contract(strategy, generation_id="gen-1", has_voiceover=voiceover)
    assert contract is not None
    assembly: dict = {
        CONTRACT_FIELD: contract.model_dump(mode="json"),
        "creator_generation_id": "gen-1",
    }
    if platform == "phone":
        assembly[PHONE_SOURCES_FIELD] = []
    if guided:
        assembly["guided_edit"] = {
            "proposal_version": 1,
            "media_digest": "a" * 64,
            "approved_proposal": {"title": "Run"},
            "media_identities": [],
        }
    candidates: dict = {
        REQUIREMENT_VERSION_FIELD: 1,
        "creator_render_contract_version": CREATOR_RENDER_CONTRACT_VERSION,
        "edit_format": edit_format,
        "creator_strategy": strategy,
        "clip_paths": [f"users/u/clip-{i}.mp4" for i in range(clips)],
        # Raw request text rides the job today; the resolver must never read it.
        "creator_request": SECRET,
    }
    if stamped:
        candidates[PLAN_AUTHORITY_FIELD] = 1
    if voiceover:
        candidates["voiceover_gcs_path"] = "voiceover-uploads/u/voice.m4a"
    if song is not None:
        candidates["user_song"] = {"gcs_path": "songs/u/s.mp3", "generation": 1, **song}
    if route_stamp:
        assembly = render_route.stamp_route(assembly, candidates)
    return SimpleNamespace(
        id=uuid.uuid4(),
        status="queued",
        mode="content_plan",
        assembly_plan=assembly,
        all_candidates=candidates,
        content_plan_item_id=uuid.uuid4(),
        user_id=uuid.uuid4(),
    )


class Harness:
    def __init__(self, monkeypatch: pytest.MonkeyPatch, job: SimpleNamespace) -> None:
        self.job = job
        self.events: list[tuple[str, str, dict]] = []
        self.calls: list[str] = []

        @contextmanager
        def session():
            yield SimpleNamespace(commit=lambda: None)

        monkeypatch.setattr(gb, "_sync_session", session)
        # Epoch None on purpose: a real epoch makes the impl set the module-level
        # _CONTENT_PLAN_FENCE contextvar, which only the public wrapper resets, and it
        # would leak into later tests in the same process.
        monkeypatch.setattr(gb, "_lock_owned_entry_job", lambda _db, _job_id: (job, None))
        monkeypatch.setattr(gb, "mark_started", lambda _job_id: None)
        monkeypatch.setattr(gb, "record_phase", lambda *_a, **_k: None)
        monkeypatch.setattr(
            "app.services.phone_rollout.phone_render_supported_formats",
            lambda: frozenset(PHONE_RENDER_SUPPORTED_FORMATS),
        )
        monkeypatch.setattr(
            render_route, "route_capabilities_from_settings", render_route.RouteCapabilities
        )

        # Hermetic: the redelivery probe reads the jobs table; tests drive it explicitly.
        self.already_recorded = False
        monkeypatch.setattr(
            render_route, "_already_recorded", lambda _job_id, _data: self.already_recorded
        )

        def trace(stage: str, event: str, data: dict | None = None) -> None:
            self.events.append((stage, event, data or {}))

        monkeypatch.setattr("app.services.pipeline_trace.record_pipeline_event", trace)
        for name in (
            "_run_phone_guided_job",
            "_run_phone_voiceover_montage_job",
            "_run_phone_subtitled_job",
            "_run_phone_narrated_job",
            "_run_guided_story_job",
            "_run_slide_post_job",
        ):
            monkeypatch.setattr(gb, name, self._recorder(name))
        monkeypatch.setattr(
            gb, "_run_phone_unified_montage_job", self._recorder("_run_phone_unified_montage_job")
        )
        self.speech = self._recorder("run_phone_speech_montage_job", result=False)
        monkeypatch.setattr(
            "app.services.phone_speech_montage_job.run_phone_speech_montage_job", self.speech
        )

    def _recorder(self, name: str, result=None) -> Mock:  # noqa: ANN001
        return Mock(side_effect=lambda *_a, **_k: self.calls.append(name) or result)

    def run(self) -> None:
        gb._run_generative_job_impl(str(self.job.id))

    @property
    def mismatches(self) -> list[dict]:
        return [data for _s, event, data in self.events if event == "route_mismatch"]


def _phone(monkeypatch, **job) -> Harness:  # noqa: ANN001, ANN003
    return Harness(monkeypatch, _job(platform="phone", **job))


def _cloud(monkeypatch, **job) -> Harness:  # noqa: ANN001, ANN003
    return Harness(monkeypatch, _job(platform="cloud", **job))


# --- Agreement: nothing is recorded --------------------------------------------------------


def test_phone_unified_montage_agrees_and_records_nothing(monkeypatch) -> None:
    h = _phone(monkeypatch)
    h.run()
    assert h.calls == ["_run_phone_unified_montage_job"]
    assert h.mismatches == []


def test_phone_voiceover_montage_agrees(monkeypatch) -> None:
    h = _phone(monkeypatch, strategy=_strategy(audio_strategy="voiceover"), voiceover=True)
    h.run()
    assert h.calls == ["_run_phone_voiceover_montage_job"]
    assert h.mismatches == []


def test_phone_required_speech_montage_agrees(monkeypatch) -> None:
    strategy = _strategy(
        audio_strategy="original_audio",
        montage_audio={"preserve_source_audio": True, "source_media_ids": ["c0"]},
    )
    h = _phone(monkeypatch, strategy=strategy)
    h.speech.side_effect = lambda *_a, **_k: h.calls.append("speech") or True
    h.run()
    assert h.calls == ["speech"]
    assert h.mismatches == []


def test_phone_guided_snapshot_subtitled_and_narrated_agree(monkeypatch) -> None:
    for job, entry in (
        ({"guided": True}, "_run_phone_guided_job"),
        ({"edit_format": "subtitled", "clips": 1}, "_run_phone_subtitled_job"),
        (
            {
                "edit_format": "narrated_ready",
                "strategy": _strategy(edit_format="narrated_ready", audio_strategy="voiceover"),
                "voiceover": True,
            },
            "_run_phone_narrated_job",
        ),
    ):
        h = _phone(monkeypatch, **job)
        h.run()
        assert h.calls == [entry], job
        assert h.mismatches == [], job


# --- Disagreement: exactly one event, the legacy path still renders -----------------------


def test_a_stale_song_attachment_records_one_mismatch_and_legacy_still_renders(monkeypatch) -> None:
    # The item still has a creator song attached, but the approved plan says library music.
    h = _phone(monkeypatch, song={"sync": "background"}, route_stamp=True)
    h.run()
    assert h.calls == ["_run_phone_unified_montage_job"], "the legacy entry must still run"
    assert len(h.mismatches) == 1
    event = h.mismatches[0]
    assert event["point"] == "phone_dispatch" and event["platform"] == "phone"
    assert event["legacy_route"] == "user_song_montage"
    assert (event["resolver_outcome"], event["resolver_route"]) == ("route", "unified_montage")
    assert event["contract_digest"] == h.job.assembly_plan[CONTRACT_FIELD]["digest"]
    assert event["stamped_route"] == "unified_montage"
    assert set(event["drivers"]) >= {"edit_format"}


def test_a_recorded_voice_on_a_subtitled_edit_is_reported_as_a_typed_refusal(monkeypatch) -> None:
    h = _phone(
        monkeypatch,
        edit_format="subtitled",
        strategy=_strategy(edit_format="subtitled", audio_strategy="voiceover"),
        voiceover=True,
        clips=1,
    )
    h.run()
    assert h.calls == ["_run_phone_subtitled_job"]
    assert len(h.mismatches) == 1
    event = h.mismatches[0]
    assert event["legacy_route"] == "subtitled"
    assert event["resolver_outcome"] == "refusal"
    assert (event["decline_reason"], event["field_path"]) == ("requirement_conflict", "edit_format")
    assert event["resolver_route"] is None


def test_the_event_carries_no_request_text_urls_or_user_ids(monkeypatch) -> None:
    h = _phone(monkeypatch, song={"sync": "background"})
    h.run()
    blob = json.dumps(h.mismatches)
    assert SECRET not in blob and "example.test" not in blob and "sig=" not in blob
    assert str(h.job.user_id) not in blob and str(h.job.id) not in blob
    assert "gs://" not in blob and "voiceover-uploads" not in blob and "songs/u" not in blob


def test_the_cloud_guided_entry_agrees_for_a_guided_plan(monkeypatch) -> None:
    h = _cloud(monkeypatch, guided=True, strategy=_strategy(render_program="guided"))
    h.run()
    assert h.calls == ["_run_guided_story_job"]
    assert h.mismatches == []


def test_a_guided_snapshot_on_a_native_plan_is_one_mismatch_and_still_renders_guided(
    monkeypatch,
) -> None:
    h = _cloud(monkeypatch, guided=True, strategy=_strategy(render_program="native"))
    h.run()
    assert h.calls == ["_run_guided_story_job"]
    assert len(h.mismatches) == 1
    event = h.mismatches[0]
    assert (event["point"], event["legacy_route"]) == ("cloud_guided", "guided_story")
    assert (event["resolver_outcome"], event["decline_reason"]) == (
        "refusal",
        "requirement_conflict",
    )


def test_the_cloud_slides_entry_agrees(monkeypatch) -> None:
    h = _cloud(monkeypatch, edit_format="slides", clips=0)
    h.job.all_candidates["slides_renderer_version"] = 1
    h.run()
    assert h.calls == ["_run_slide_post_job"]
    assert h.mismatches == []


# --- Shadow mode never changes control flow ------------------------------------------------


def test_a_resolver_fault_never_breaks_the_dispatch(monkeypatch) -> None:
    h = _phone(monkeypatch, song={"sync": "background"})

    def boom(_inputs):
        raise RuntimeError("resolver fault")

    monkeypatch.setattr(render_route, "resolve_route", boom)
    h.run()
    assert h.calls == ["_run_phone_unified_montage_job"]
    assert h.mismatches == []


def test_a_trace_write_fault_never_breaks_the_dispatch(monkeypatch) -> None:
    h = _phone(monkeypatch, song={"sync": "background"})

    def broken_trace(*_a, **_k):
        raise RuntimeError("trace store down")

    monkeypatch.setattr("app.services.pipeline_trace.record_pipeline_event", broken_trace)
    h.run()
    assert h.calls == ["_run_phone_unified_montage_job"]


def test_an_unstamped_job_records_nothing_and_reads_nothing(monkeypatch) -> None:
    h = _phone(monkeypatch, song={"sync": "background"}, stamped=False)
    inputs = Mock(side_effect=AssertionError("unstamped job built resolver inputs"))
    capabilities = Mock(side_effect=AssertionError("unstamped job read live flags"))
    monkeypatch.setattr(render_route, "route_inputs_from_job", inputs)
    monkeypatch.setattr(render_route, "route_capabilities_from_settings", capabilities)
    h.run()
    assert h.calls == ["_run_phone_unified_montage_job"]
    assert h.mismatches == []
    inputs.assert_not_called()
    capabilities.assert_not_called()


def test_a_stale_route_stamp_is_reported_as_absent(monkeypatch) -> None:
    h = _phone(monkeypatch, song={"sync": "background"}, route_stamp=True)
    # the contract was rebound after the stamp (e.g. an edited duration): the stamp is stale
    contract = read_render_contract(h.job.assembly_plan).rebind(duration_s=15)
    h.job.assembly_plan = {**h.job.assembly_plan, CONTRACT_FIELD: contract.model_dump(mode="json")}
    h.run()
    assert len(h.mismatches) == 1
    assert h.mismatches[0]["stamped_route"] is None


def test_a_redelivered_task_does_not_append_the_same_mismatch_twice(monkeypatch) -> None:
    h = _phone(monkeypatch, song={"sync": "background"})
    h.already_recorded = True
    h.run()
    assert h.calls == ["_run_phone_unified_montage_job"]
    assert h.mismatches == []


def test_an_import_time_fault_in_the_shadow_module_cannot_reach_a_job(monkeypatch) -> None:
    import sys

    for stamped in (True, False):
        h = _phone(monkeypatch, song={"sync": "background"}, stamped=stamped)
        monkeypatch.setitem(sys.modules, "app.services.render_route", None)  # import raises
        h.run()
        assert h.calls == ["_run_phone_unified_montage_job"]


def _is_commit(node: ast.AST) -> bool:
    return (
        isinstance(node, ast.Expr)
        and isinstance(node.value, ast.Call)
        and isinstance(node.value.func, ast.Attribute)
        and node.value.func.attr == "commit"
    )


def shadow_calls_under_a_held_lock(source: str) -> tuple[int, list[int]]:
    """(number of ``_shadow_route`` calls, lines of those made while the entry lock is held).

    A call is under the lock when it sits inside a ``with _sync_session()`` block and no
    ``.commit()`` statement precedes it in any enclosing statement list within that block.
    """
    tree = ast.parse(textwrap.dedent(source))
    parents = {c: p for p in ast.walk(tree) for c in ast.iter_child_nodes(p)}
    calls = [
        n
        for n in ast.walk(tree)
        if isinstance(n, ast.Call) and getattr(n.func, "id", None) == "_shadow_route"
    ]
    held: list[int] = []
    for call in calls:
        node: ast.AST = call
        released = False
        while node in parents:
            parent = parents[node]
            for field in ("body", "orelse", "finalbody"):
                block = getattr(parent, field, None)
                if isinstance(block, list) and node in block:
                    released = released or any(_is_commit(x) for x in block[: block.index(node)])
            if isinstance(parent, ast.With) and any(
                getattr(getattr(item.context_expr, "func", None), "id", None) == "_sync_session"
                for item in parent.items
            ):
                if not released:
                    held.append(call.lineno)
                break
            node = parent
    return len(calls), held


def test_the_trace_is_never_written_while_the_job_row_is_locked() -> None:
    """``record_pipeline_event`` writes the jobs row on a SEPARATE connection, so a shadow call
    made while this worker holds the entry ``FOR UPDATE`` would deadlock against itself.
    Every ``_shadow_route`` call must be outside the ``with _sync_session()`` block or after a
    ``db.commit()`` in it. Checked on the AST, call site by call site."""
    total, held = shadow_calls_under_a_held_lock(inspect.getsource(gb._run_generative_job_impl))
    assert total >= 8, "every dispatcher decision point is shadowed"
    assert held == [], f"_shadow_route runs while the job lock is held (lines {held})"


def test_the_lock_check_itself_can_fail() -> None:
    locked = """
def f(job_id):
    with _sync_session() as db:
        _shadow_route(job_id)
"""
    released = """
def f(job_id):
    with _sync_session() as db:
        db.commit()
        if x:
            _shadow_route(job_id)
"""
    outside = """
def f(job_id):
    with _sync_session() as db:
        db.commit()
    _shadow_route(job_id)
"""
    assert shadow_calls_under_a_held_lock(locked) == (1, [4])
    assert shadow_calls_under_a_held_lock(released) == (1, [])
    assert shadow_calls_under_a_held_lock(outside) == (1, [])
