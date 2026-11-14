#!/usr/bin/env python3
"""Scorecard for prod thread aa36d140 (2026-11-14): a length nobody asked for blocked the video.

One 14.03 s clip, the creator's lyrics as text, no length in any message. The planned 14.005 s
became ~14 s in the server's check, which counted that as a lost creator ask: five turns ended
"Klipler toplam yaklaşık 14 saniye, bu yüzden video 14.005 saniye olamıyor." This replays the
REAL policy check (with the planner's flags) and the REAL approval-readiness report over the
recorded media and brief and the reconstructed strategy (see ``source`` in ``fixture.json``).
Offline: a socket guard fails any network attempt.

    cd src/apps/api && .venv/bin/python ../../../scripts/kria/score_kri_tadic_length.py
    make kria-replay FIXTURE=kri_tadic_length

Checks (VERIFIED / NOT VERIFIED / INCONCLUSIVE):

1. The planner's check of the incident strategy drafts it (no "simpler version?" question).
2. Approval readiness on the drafted strategy has no policy problem and no length sentence.
3. Approval readiness on the draft as prod held it (14.005 s) has no policy problem.
4. Control: a creator-stated 20 s on the same clip still says the clips can't reach it.
5. Control: a creator-stated 14 s on a 13.98 s clip drafts (within max(0.5 s, 5 %)).
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
from pathlib import Path
from typing import Any

REPO = Path(__file__).resolve().parents[2]
API = REPO / "src" / "apps" / "api"
FIXTURE = API / "tests" / "fixtures" / "kria_replay" / "kri_tadic_length" / "fixture.json"

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
LENGTH_WORDS = ("olamıyor", "can't be")


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


@contextlib.contextmanager
def _prod_settings():  # noqa: ANN202
    """Production's flag profile (tests/_prod_profile.py), undone on exit."""
    import pytest

    from tests._prod_profile import apply_prod_profile

    with pytest.MonkeyPatch.context() as monkeypatch:
        apply_prod_profile(monkeypatch)
        yield


def _manifest(media: list[dict]):  # noqa: ANN202
    from app.services.creator_capabilities import resolve_creator_manifest

    ids = [row["media_id"] for row in media]
    return resolve_creator_manifest(
        item_id="item-tadic",
        edit_format="montage",
        media=copy.deepcopy(media),
        phone_source_media_ids=ids,
        phone_rendering_allowed=True,
    )


def _snapshot(media: list[dict]) -> dict:
    return {
        "clip_assignments": [
            {"media_id": row["media_id"], "duration_s": row["duration_s"], "analysis": {}}
            for row in media
        ],
        "visuals": [],
    }


def _strategy(fx: dict, **update: Any):  # noqa: ANN202
    from app.agents._schemas.creator_agent import CreativeStrategy

    return CreativeStrategy.model_validate({**copy.deepcopy(fx["strategy"]), **update})


def _planner_check(fx: dict, strategy, media: list[dict]):  # noqa: ANN001, ANN202
    """The check with the kwargs the planner passes when brief binding and choice questions
    are on (planner._plan_from_creator_output)."""
    from app.kria.reply_language import reply_language_for
    from app.kria.strategy_policy import check_strategy_for_runtime_v2

    with reply_language_for(fx["observed"]["language"]):
        return check_strategy_for_runtime_v2(
            _manifest(media),
            strategy,
            simplify_unsupported=True,
            ask_before_simplifying=True,
            ask_about_stated_settings=True,
        )


def _readiness(fx: dict, strategy: dict, media: list[dict]) -> dict:
    from app.kria.approval_readiness import assess_readiness
    from app.kria.brief import CreativeBrief
    from app.kria.reply_language import reply_language_for

    with reply_language_for(fx["observed"]["language"]):
        return assess_readiness(
            CreativeBrief.model_validate(fx["brief"]),
            strategy,
            media=_snapshot(media),
            manifest=_manifest(media),
        )


def _says_length(text: str | None) -> bool:
    return bool(text) and any(word in text for word in LENGTH_WORDS)


def _result(check_id: int, name: str, why: list[str], evidence: dict) -> dict:
    return {
        "id": check_id,
        "name": name,
        "verdict": "NOT VERIFIED" if why else "VERIFIED",
        "why": why,
        "evidence": evidence,
    }


def _guarded(check_id: int, name: str, fn) -> dict:  # noqa: ANN001
    try:
        return fn(check_id, name)
    except Exception as exc:  # noqa: BLE001
        return {
            "id": check_id,
            "name": name,
            "verdict": "INCONCLUSIVE",
            "why": [f"{type(exc).__name__}: {exc}"],
            "evidence": {"traceback": traceback.format_exc()[-2000:]},
        }


def check_planner_drafts(fx: dict, check_id: int, name: str) -> dict:
    from app.kria.strategy_policy import CheckedStrategy

    checked = _planner_check(fx, _strategy(fx), fx["media"])
    why = []
    if not isinstance(checked, CheckedStrategy):
        why.append(f"refused: {checked.question}")
    evidence = {
        "result": type(checked).__name__,
        "target_duration_s": getattr(getattr(checked, "strategy", None), "target_duration_s", None),
        "question": getattr(checked, "question", None),
    }
    return _result(check_id, name, why, evidence)


def _report_problems(report: dict) -> tuple[str | None, list[str]]:
    reasons = [row.get("reason") or "" for row in report.get("requirements") or []]
    return report.get("policy_problem"), reasons


def check_readiness_drafted(fx: dict, check_id: int, name: str) -> dict:
    checked = _planner_check(fx, _strategy(fx), fx["media"])
    strategy = checked.strategy.model_dump(mode="json", exclude_none=True)
    report = _readiness(fx, strategy, fx["media"])
    problem, reasons = _report_problems(report)
    why = []
    if problem:
        why.append(f"policy_problem: {problem}")
    why += [f"requirement reason: {r}" for r in reasons if _says_length(r)]
    return _result(check_id, name, why, {"policy_problem": problem, "requirement_reasons": reasons})


def check_readiness_as_prod_held(fx: dict, check_id: int, name: str) -> dict:
    """The draft as readiness read it in prod: the planner's draft carrying 14.005 s."""
    checked = _planner_check(fx, _strategy(fx), fx["media"])
    strategy = checked.strategy.model_dump(mode="json", exclude_none=True)
    strategy["target_duration_s"] = fx["strategy"]["target_duration_s"]
    report = _readiness(fx, strategy, fx["media"])
    problem, reasons = _report_problems(report)
    why = [f"says the length: {t}" for t in (problem, *reasons) if _says_length(t)]
    if problem:
        why.append(f"policy_problem: {problem}")
    return _result(check_id, name, why, {"policy_problem": problem, "requirement_reasons": reasons})


def check_stated_unreachable(fx: dict, check_id: int, name: str) -> dict:
    from app.kria.strategy_policy import RefusedStrategy

    seconds = fx["controls"]["stated_unreachable_s"]
    refused = _planner_check(
        fx,
        _strategy(
            fx,
            target_duration_s=seconds,
            target_duration_requested=True,
            creator_duration_s=seconds,
        ),
        fx["media"],
    )
    why = []
    if not isinstance(refused, RefusedStrategy) or "target_duration_s" not in refused.fields:
        why.append(f"a stated {seconds} s on 14 s of clips is no longer asked about")
    elif f"{seconds:g} saniye olamıyor" not in refused.question:
        why.append(f"the question does not name the {seconds} s ask: {refused.question}")
    return _result(check_id, name, why, {"question": getattr(refused, "question", None)})


def check_stated_within_tolerance(fx: dict, check_id: int, name: str) -> dict:
    from app.kria.strategy_policy import CheckedStrategy

    control = fx["controls"]["stated_within_tolerance"]
    media = [{**fx["media"][0], "duration_s": control["clip_s"]}]
    stated = control["stated_s"]
    checked = _planner_check(
        fx,
        _strategy(
            fx,
            target_duration_s=stated,
            target_duration_requested=True,
            creator_duration_s=stated,
        ),
        media,
    )
    why = [] if isinstance(checked, CheckedStrategy) else [f"refused: {checked.question}"]
    return _result(check_id, name, why, {"result": type(checked).__name__})


def run_offline() -> dict:
    fx = load_fixture()
    with _prod_settings():
        checks = [
            _guarded(
                1,
                "the planner drafts the incident strategy",
                lambda i, n: check_planner_drafts(fx, i, n),
            ),
            _guarded(
                2,
                "readiness on the draft has no policy problem",
                lambda i, n: check_readiness_drafted(fx, i, n),
            ),
            _guarded(
                3,
                "readiness on the draft at 14.005 s has no policy problem",
                lambda i, n: check_readiness_as_prod_held(fx, i, n),
            ),
            _guarded(
                4,
                "control: a stated 20 s still says it can't fit",
                lambda i, n: check_stated_unreachable(fx, i, n),
            ),
            _guarded(
                5,
                "control: a stated 14 s on 13.98 s drafts",
                lambda i, n: check_stated_within_tolerance(fx, i, n),
            ),
        ]
    return {
        "fixture": fx["fixture_id"],
        "incident": fx["incident"],
        "evidence_level": "offline replay (real policy check and approval readiness; "
        "reconstructed strategy)",
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
    report = run_offline()
    text = json.dumps(report, ensure_ascii=False, indent=2, default=str)
    if args.out:
        args.out.write_text(text, encoding="utf-8")
    for check in report["checks"]:
        print(f"{check['id']}. {check['verdict']:<13} {check['name']}")
        for line in check["why"]:
            print(f"     - {line}")
    print(json.dumps(report["summary"]))
    return 0 if report["summary"]["VERIFIED"] == len(report["checks"]) else 1


if __name__ == "__main__":
    raise SystemExit(main())
