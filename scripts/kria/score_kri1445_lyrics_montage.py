#!/usr/bin/env python3
"""Scorecard for prod thread 6db7ecd4 (2026-11-14): typed lyrics shown twice, then no video.

One 14 s clip, a montage, and the creator's pasted lyrics (3 lines) as on-screen text. The
approved plan held the lyric twice: a clip-intent caption with the whole lyric as one block and
`pinned_texts` with the same lyric as 3 lines. Both jobs ended `creation_text_failed` in the
`assembly` stage, before any phone render. This runs the REAL phone worker
(`_run_phone_unified_montage_job` -> `plan_unified_montage` -> `compose_creation_text` ->
`_run_phone_guided_job` -> phone compile) on the recorded, scrubbed job. Only the text composer's
model call is replaced, by a cassette (its stored input and output are redacted, so the cassette
is authored from the recorded error text; see ``source`` in ``fixture.json``). Offline: a socket
guard fails any network attempt.

    cd src/apps/api && .venv/bin/python ../../../scripts/kria/score_kri1445_lyrics_montage.py
    make kria-replay FIXTURE=kri1445_lyrics_montage

Composer cassettes (``cassettes`` in ``fixture.json``): A the recorded clarification, B a
hand-authored bundle that reaches the recorded "Text changed" raise site, C and D probes (the
obvious removal of the duplicate block; a composer that leaves the lane alone).

Checks (VERIFIED / NOT VERIFIED / INCONCLUSIVE); 1 and 2 run once per cassette:

1. each_lyric_line_once: each lyric line is drawn exactly once in the compiled recipe's text layers.
2. job_not_text_failed: the worker finishes without CreationTextCompositionError.
3. clarification_becomes_question: a composer question is saved through the request-recovery
   channel as a creator-facing question, not raised as a failure (cassette A).
4. dropped_line_rejected: negative control, a recipe with one lyric line removed fails check 1.
5. composer_not_called: the lyric is already drawn and every ask met, so no cassette is consumed.

The strategy is not read from the fixture as stored: it goes through the real planning boundary
(`adapt_creator_action`) first, as a new request's does.

Exits 1 unless every check is VERIFIED. Check 3 stays NOT VERIFIED until a composer question
is saved as the creator's question (KRI-1445).
"""

from __future__ import annotations

import argparse
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
from unittest.mock import patch

REPO = Path(__file__).resolve().parents[2]
API = REPO / "src" / "apps" / "api"
FIXTURE = (
    API
    / "tests"
    / "fixtures"
    / "kria_replay"
    / "kri1445_lyrics_montage"
    / "fixture.json"
)
_GENERATION = "kri1445-approved-generation"

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
    pass


def _bootstrap() -> None:
    for key, value in _SAFE_ENV.items():
        os.environ[key] = value
    if str(API) not in sys.path:
        sys.path.insert(0, str(API))

    def _blocked(*_args: Any, **_kwargs: Any):  # noqa: ANN202
        raise OfflineViolation("network access is blocked in the offline scorecard")

    socket.socket.connect = _blocked  # type: ignore[method-assign]
    socket.create_connection = _blocked  # type: ignore[assignment]


def load_fixture() -> dict[str, Any]:
    return json.loads(FIXTURE.read_text(encoding="utf-8"))


def fold(text: object) -> str:
    """NFC, diacritics folded (dotless i included), lower-cased, whitespace collapsed."""
    nfd = unicodedata.normalize("NFD", unicodedata.normalize("NFC", str(text or "")))
    base = "".join(ch for ch in nfd if not unicodedata.combining(ch))
    return " ".join(base.replace("ı", "i").replace("İ", "I").casefold().split())


@contextlib.contextmanager
def _prod_settings():  # noqa: ANN202
    import pytest

    from tests._prod_profile import apply_prod_profile

    with pytest.MonkeyPatch.context() as monkeypatch:
        apply_prod_profile(monkeypatch)
        yield


def _cassette(raw_text: str):  # noqa: ANN202
    from app.agents._runtime import ModelClient, ModelInvocation

    class _Recorded(ModelClient):
        calls = 0

        def invoke(self, **_kwargs: Any) -> ModelInvocation:
            type(self).calls += 1
            return ModelInvocation(raw_text=raw_text)

    return _Recorded()


def planned_strategy(fx: dict) -> dict:
    """The strategy a new request approves: the recorded Main Creator strategy and its resolved
    clip intents through the real planning boundary, ``adapt_creator_action``.

    The fixture holds the strategy as it was approved. Its model-authored part (pins, clip
    intents) goes in as the action; its server-owned part (`resolved_clip_intents`) goes in as
    the resolver's result, exactly the two inputs the planner gets.
    """
    from app.agents._schemas.creator_agent import CreativeStrategy, ProposeStrategy
    from app.kria.planner import adapt_creator_action
    from app.schemas.clip_intents import ClipIntent, ResolvedClipIntent

    stored = copy.deepcopy(fx["strategy"])
    resolved = [
        ResolvedClipIntent.model_validate(row)
        for row in stored.pop("resolved_clip_intents", None) or []
    ]
    intents = [ClipIntent.model_validate(row) for row in stored.get("clip_intents") or []]
    action = ProposeStrategy(
        kind="propose_strategy", strategy=CreativeStrategy.model_validate(stored)
    )
    plan = adapt_creator_action(
        action, server_clip_intents=intents, server_resolved_clip_intents=resolved
    )
    apply = next(i for i in plan.intents if i.tool_name == "draft.apply_strategy")
    return copy.deepcopy(apply.arguments["strategy"])


def build_job(fx: dict) -> SimpleNamespace:
    """The dispatched job: phone sources, pinned brief binding and render contract, the planned
    strategy and the generation's phone text style policy (the recorded job's)."""
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

    clip = fx["clip"]
    row = {
        "media_id": clip["media_id"],
        "gcs_path": clip["gcs_path"],
        "kind": "video",
        "duration_s": clip["duration_s"],
        "generation": "1",
        "storage_generation": "1",
        "has_audio": True,
        "analysis": copy.deepcopy(clip["analysis"]),
    }
    media_snapshot = {
        "clip_paths": [clip["gcs_path"]],
        "clip_assignments": [row],
        "best_parts": copy.deepcopy(fx["best_parts"]),
        "best_parts_conflicts": [],
        "best_parts_capacity_s": fx["length_plan"]["capacity_s"],
        "length_plan": copy.deepcopy(fx["length_plan"]),
    }
    strategy = planned_strategy(fx)
    brief = CreativeBrief.model_validate(fx["brief"])
    original = clip["original"]
    source = PhoneSourceBinding(
        media_id=clip["media_id"],
        proxy_path=clip["gcs_path"],
        generation="1",
        original=OriginalMediaDescriptor(
            sha256="a" * 64,
            byte_count=original["byte_count"],
            duration_s=original["duration_s"],
            width=original["width"],
            height=original["height"],
            orientation_degrees=original["orientation_degrees"],
            has_audio=original["has_audio"],
        ),
    )
    contract = build_render_contract(
        strategy, generation_id=_GENERATION, brief=brief, media_snapshot=media_snapshot
    )
    first, latest = fx["messages"]
    binding = BriefBinding.create(
        uuid.uuid4(),
        brief,
        latest_message=latest,
        full_creator_request=f"{first}\n{latest}",
        media_snapshot=media_snapshot,
    )
    snapshot = {
        PHONE_SOURCES_FIELD: [source.model_dump(mode="json")],
        "creator_generation_id": _GENERATION,
        "creator_brief_binding": binding.model_dump(mode="json"),
        "creator_reply_language": fx["observed"]["language"],
        CONTRACT_FIELD: contract.model_dump(mode="json"),
        PHONE_TEXT_STYLE_FIELD: copy.deepcopy(fx["phone_text_style_policy"]),
    }
    candidates = {
        "clip_paths": [clip["gcs_path"]],
        "edit_format": "montage",
        "creator_strategy": strategy,
        "creator_request": binding.creator_request,
        "creator_client_protocol": fx["client_protocol"],
        "landscape_fit": "fit",
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
        rows=[row],
        brief=binding.resolve(),
        strategy=strategy,
    )


class _ComposerReached(Exception):  # noqa: N818 - a control-flow sentinel, not an error
    pass


def run_worker(
    job: SimpleNamespace,
    *,
    composer_raw: str | None = None,
    stop_at_composer: bool = False,
) -> dict[str, Any]:
    """The real phone worker on ``job`` up to the device pin. Returns what it saw."""
    from app import storage
    from app.pipeline.guided_story import compile_execution_plan
    from app.services import clip_facts, creation_text_composition, phone_destination
    from app.tasks import generative_build as gb

    seen: dict[str, Any] = {"composer_calls": 0}
    real_run = creation_text_composition.CreationTextComposer.run
    from app.config import settings

    def composer_run(self, inp, *args, **kwargs):  # noqa: ANN001, ANN002, ANN003, ANN202
        seen["composer_calls"] += 1
        seen.setdefault("composer_inputs", []).append(
            copy.deepcopy(inp.variant_snapshot)
        )
        if stop_at_composer:
            raise _ComposerReached
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

    def locked(_db, _job_id):  # noqa: ANN001, ANN202
        return job, 1

    def save_to_job(job_id, snapshot, *, ownership_epoch, message, receipts=None):  # noqa: ANN001, ANN202
        return real_save(
            job_id,
            snapshot,
            ownership_epoch=ownership_epoch,
            message=message,
            receipts=receipts,
        )

    real_save = gb._save_request_recovery
    with contextlib.ExitStack() as stack:
        patches = [
            (
                gb,
                "_load_unified_montage_inputs",
                lambda _j: (job.user_id, job.rows, job.brief),
            ),
            (gb, "_load_unified_montage_visuals", lambda *_a, **_k: []),
            (gb, "_sync_session", sessions),
            (gb, "_lock_owned_entry_job", locked),
            (gb, "_guided_execution_plan", guided_plan),
            (gb, "_emit_post_caption", lambda *_a, **_k: None),
            (clip_facts, "enrich_clip_facts", lambda pairs, **_k: list(pairs)),
            (phone_destination, "phone_rendering_allowed_for_job", lambda _job: True),
            (storage, "download_to_file", no_storage),
            (creation_text_composition.CreationTextComposer, "run", composer_run),
            (settings, "phone_rendering_enabled", True),
            (settings, "phone_text_styling_enabled", True),
        ]
        if composer_raw is not None:
            client = _cassette(composer_raw)
            patches.append(
                (creation_text_composition, "default_client", lambda: client)
            )
        for target, name, value in patches:
            stack.enter_context(patch.object(target, name, value))
        try:
            unified = gb._run_phone_unified_montage_job(
                str(job.id),
                copy.deepcopy(job.assembly_plan),
                job.all_candidates,
                ownership_epoch=1,
            )
        except _ComposerReached:
            return seen
        except Exception as exc:  # noqa: BLE001 - reported as evidence, never green
            seen["worker_exc"] = exc
            seen["worker_error"] = f"{type(exc).__name__}: {exc}"
            seen["traceback"] = traceback.format_exc()
            seen["assembly_plan"] = copy.deepcopy(job.assembly_plan)
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
            seen["pin_exc"] = exc
            seen["pin_error"] = f"{type(exc).__name__}: {exc}"
            seen["traceback"] = traceback.format_exc()
    seen["assembly_plan"] = copy.deepcopy(job.assembly_plan)
    seen["status"] = job.status
    return seen


def _where(exc: BaseException | None) -> str | None:
    """``file:line`` of the innermost frame inside the app that raised ``exc``."""
    if exc is None:
        return None
    frames = [
        f for f in traceback.extract_tb(exc.__traceback__) if "/app/" in f.filename
    ]
    if not frames:
        return None
    frame = frames[-1]
    return f"{Path(frame.filename).relative_to(API)}:{frame.lineno}"


def _chain(exc: BaseException | None) -> list[BaseException]:
    out: list[BaseException] = []
    while exc is not None and exc not in out:
        out.append(exc)
        exc = exc.__cause__ or exc.__context__
    return out


def _text_failed(exc: BaseException | None) -> bool:
    from app.services.creation_text_composition import CreationTextCompositionError
    from app.tasks.generative_build import _classify_phone_plan_failure

    if exc is None:
        return False
    return isinstance(exc, CreationTextCompositionError) or (
        _classify_phone_plan_failure(exc)[0] == "creation_text_failed"
    )


def lyric_counts(text_layers: list[dict], lines: list[str]) -> dict[str, int]:
    """How many times each lyric line is drawn. A layer's runs read as one text (a wrapped line
    spans runs), and a layer drawing the whole lyric draws every line in it."""
    layers = [
        fold(" ".join(run.get("text", "") for run in layer.get("runs") or []))
        for layer in text_layers
    ]
    return {line: sum(layer.count(fold(line)) for layer in layers) for line in lines}


def recipe_layers(seen: dict[str, Any]) -> list[dict] | None:
    from app.services.device_render import DEVICE_RENDER_FIELD

    pinned = ((seen.get("assembly_plan") or {}).get(DEVICE_RENDER_FIELD) or {}).get(
        "guided_story"
    )
    try:
        return pinned["status"]["request"]["recipe"]["text_layers"]
    except (KeyError, TypeError):
        return None


def _result(
    check_id: int, name: str, scenario: str | None, why: list[str], evidence: dict
) -> dict:
    return {
        "id": check_id,
        "name": name,
        "scenario": scenario,
        "verdict": "NOT VERIFIED" if why else "VERIFIED",
        "why": why,
        "evidence": evidence,
    }


def _guarded(check_id: int, name: str, scenario: str | None, fn) -> dict:  # noqa: ANN001
    try:
        return fn()
    except Exception as exc:  # noqa: BLE001
        return {
            "id": check_id,
            "name": name,
            "scenario": scenario,
            "verdict": "INCONCLUSIVE",
            "why": [f"{type(exc).__name__}: {exc}"],
            "evidence": {"traceback": traceback.format_exc()[-2000:]},
        }


def check_each_lyric_line_once(fx: dict, scenario: str, seen: dict) -> dict:
    lines = fx["lyrics"]["lines"]
    layers = recipe_layers(seen)
    why: list[str] = []
    counts: dict[str, int] = {}
    if layers is None:
        stop = (
            seen.get("worker_error") or seen.get("pin_error") or "no recipe was pinned"
        )
        why.append(f"no compiled recipe to count lines in: {stop}")
    else:
        counts = lyric_counts(layers, lines)
        why += [
            f"line {i + 1} is drawn {n} times"
            for i, (_, n) in enumerate(counts.items())
            if n != 1
        ]
    return _result(
        1,
        "each_lyric_line_once",
        scenario,
        why,
        {
            "drawn_per_line": list(counts.values()),
            "layers": None
            if layers is None
            else [[r["text"] for r in row["runs"]] for row in layers],
        },
    )


def check_job_not_text_failed(scenario: str, seen: dict) -> dict:
    why: list[str] = []
    for key in ("worker_exc", "pin_exc"):
        exc = seen.get(key)
        if exc is None:
            continue
        why.append(f"{type(exc).__name__}: {exc}")
    exc = seen.get("worker_exc") or seen.get("pin_exc")
    evidence = {
        "failure_reason": "creation_text_failed" if _text_failed(exc) else None,
        "raised_at": _where(exc),
        "cause_classes": [type(link).__name__ for link in _chain(exc)[1:]],
        "cause_raised_at": _where(_chain(exc)[-1])
        if exc is not None and len(_chain(exc)) > 1
        else None,
    }
    return _result(2, "job_not_text_failed", scenario, why, evidence)


def check_clarification_becomes_question(fx: dict, scenario: str, seen: dict) -> dict:
    question = fx["cassettes"][scenario]["output"]["reply"]
    recovery = (seen.get("assembly_plan") or {}).get("request_recovery")
    binding = (seen.get("assembly_plan") or {}).get("creator_brief_binding") or {}
    exc = seen.get("worker_exc") or seen.get("pin_exc")
    why: list[str] = []
    if not isinstance(recovery, dict) or not str(recovery.get("message") or "").strip():
        why.append("no request_recovery question was saved for the creator")
    elif fold(question) not in fold(recovery["message"]):
        why.append(f"the saved question is not the composer's: {recovery['message']}")
    elif recovery.get("binding_digest") != binding.get("digest"):
        why.append("the saved question is not bound to the approved request")
    if _text_failed(exc):
        why.append(
            f"the same turn ends as a failure: {type(exc).__name__} at {_where(exc)}"
        )
    return _result(
        3,
        "clarification_becomes_question",
        scenario,
        why,
        {
            "composer_question": question,
            "request_recovery_saved": isinstance(recovery, dict),
            "saved_message": (recovery or {}).get("message")
            if isinstance(recovery, dict)
            else None,
        },
    )


def check_composer_not_called(runs: dict[str, dict]) -> dict:
    """The lyric is already on screen and every ask is met, so the text composer has nothing to
    compose: it must not be called under any cassette."""
    calls = {name: seen["composer_calls"] for name, seen in runs.items()}
    why = [f"the text composer was called {n} time(s) in {k}" for k, n in calls.items() if n]
    return _result(5, "composer_not_called", None, why, {"composer_calls": calls})


def check_dropped_line_rejected(fx: dict) -> dict:
    """Negative control on the counter itself: it accepts a recipe that draws each line once
    and rejects one that lost a line (or draws the lyric twice)."""
    lines = fx["lyrics"]["lines"]

    def layer(text: str) -> dict:
        return {"runs": [{"text": text}]}

    intact = [layer(line) for line in lines]
    dropped = intact[:1] + intact[2:]
    doubled = [layer(" ".join(lines)), *intact]
    verdicts = {
        name: all(n == 1 for n in lyric_counts(layers, lines).values())
        for name, layers in (
            ("intact", intact),
            ("dropped", dropped),
            ("doubled", doubled),
        )
    }
    why = []
    if not verdicts["intact"]:
        why.append("the counter rejects a recipe that draws each line once")
    if verdicts["dropped"]:
        why.append("the counter accepts a recipe with a lyric line removed")
    if verdicts["doubled"]:
        why.append("the counter accepts a recipe that draws the lyric twice")
    return _result(4, "dropped_line_rejected", None, why, {"passes_check_1": verdicts})


def run_offline() -> dict:
    fx = load_fixture()
    checks: list[dict] = []
    runs: dict[str, dict] = {}
    with _prod_settings():
        for scenario, cassette in fx["cassettes"].items():
            job = build_job(fx)
            runs[scenario] = run_worker(
                job, composer_raw=json.dumps(cassette["output"])
            )
    for scenario, seen in runs.items():
        checks.append(
            _guarded(
                1,
                "each_lyric_line_once",
                scenario,
                lambda s=scenario, v=seen: check_each_lyric_line_once(fx, s, v),
            )
        )
        checks.append(
            _guarded(
                2,
                "job_not_text_failed",
                scenario,
                lambda s=scenario, v=seen: check_job_not_text_failed(s, v),
            )
        )
    asking = next(
        k for k, c in fx["cassettes"].items() if c["output"].get("needs_clarification")
    )
    checks.append(
        _guarded(
            3,
            "clarification_becomes_question",
            asking,
            lambda: check_clarification_becomes_question(fx, asking, runs[asking]),
        )
    )
    checks.append(
        _guarded(
            4, "dropped_line_rejected", None, lambda: check_dropped_line_rejected(fx)
        )
    )
    checks.append(
        _guarded(5, "composer_not_called", None, lambda: check_composer_not_called(runs))
    )
    checks.sort(key=lambda c: (c["id"], c["scenario"] or ""))
    inputs = (runs[asking].get("composer_inputs") or [{}])[0]
    return {
        "fixture": fx["fixture_id"],
        "incident": fx["incident"],
        "evidence_level": "offline replay (real phone worker, text composer and phone compile; "
        "authored composer cassettes, scrubbed lyrics)",
        "composer_lane": [
            {"id": bar.get("id"), "text": bar.get("text")}
            for bar in inputs.get("text_bars") or []
        ],
        "composer_copy_constraints": sorted(inputs.get("copy_constraints") or {}),
        "checks": checks,
        "summary": {
            verdict: sum(1 for c in checks if c["verdict"] == verdict)
            for verdict in ("VERIFIED", "NOT VERIFIED", "INCONCLUSIVE")
        },
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--out", type=Path)
    args = parser.parse_args(argv)
    _bootstrap()
    import logging

    import structlog

    structlog.configure(
        wrapper_class=structlog.make_filtering_bound_logger(logging.CRITICAL)
    )
    report = run_offline()
    text = json.dumps(report, ensure_ascii=False, indent=2, default=str)
    if args.out:
        args.out.write_text(text, encoding="utf-8")
    print(
        "composer lane:", [(r["id"], r["text"][:24]) for r in report["composer_lane"]]
    )
    for check in report["checks"]:
        tag = f" [{check['scenario']}]" if check["scenario"] else ""
        print(f"{check['id']}. {check['verdict']:<13} {check['name']}{tag}")
        for line in check["why"]:
            print(f"     - {line}")
        evidence = check["evidence"]
        for key in ("raised_at", "cause_raised_at", "drawn_per_line", "composer_calls"):
            if evidence.get(key):
                print(f"     . {key}: {evidence[key]}")
    print(json.dumps(report["summary"]))
    return 0 if report["summary"]["VERIFIED"] == len(report["checks"]) else 1


if __name__ == "__main__":
    raise SystemExit(main())
