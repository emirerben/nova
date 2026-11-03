"""CLI: qualify the intent review on generated footage, under a preregistered spend cap (KRI-1171).

Zero cost by default. A dry run builds the five cases through the real catalog and prompt path,
prints the exact spend table, tests the scorer against reviewers that must fail, and writes the
frames, prompts, a contact sheet and `dry-run.json` for inspection. It never builds an OpenAI
client, opens a database connection or sends a byte.

    python -m app.cli.qualify_intent_review                      # dry run (the default)
    python -m app.cli.qualify_intent_review --stage full         # the same, gates shown for full

A provider is called only when every gate holds at once: `--execute`, an approved run id and
maximum in the environment, a run estimate at or under that maximum ($2 per run, $5 in total
across runs), a development environment on a local database, a pinned preregistration digest and
a passed pilot before the full run. Every refused gate is printed together. Preregistration and
exact commands: docs/reviews/intent-review/QUALIFICATION.md.
"""

from __future__ import annotations

import argparse
import io
import json
import math
import os
import re
import statistics
import subprocess
import sys
from collections.abc import Mapping
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from PIL import Image, ImageDraw, ImageFont
from sqlalchemy.engine import make_url

from app.agents._openai_vision import VISION_MODEL, OpenAIVisionClient, VisionResult
from app.agents._runtime import RunContext
from app.agents.intent_review import PROMPT_VERSION
from app.config import settings
from app.kria import intent_review_qualification as q
from app.services import ai_cost_control as costs
from app.services.bounded_media_review import (
    INTENT_REVIEW_FEATURE,
    MediaReviewProvenance,
    invoke_bounded_media_review,
)

RUN_ID_ENV = "INTENT_REVIEW_QUALIFICATION_RUN_ID"
MAX_USD_ENV = "INTENT_REVIEW_QUALIFICATION_MAX_USD"
RUN_ID_PATTERN = re.compile(r"^iqual-[a-z0-9][a-z0-9-]{3,40}-(pilot|full)$")
DEFAULT_OUT = Path(".intent-review-qualification")
DOC_PATH = ("docs", "reviews", "intent-review", "QUALIFICATION.md")
_DIGEST_LINE = re.compile(r"^Preregistration digest:\s*`?([0-9a-f]{64})`?\s*$", re.MULTILINE)
_LOCAL_HOSTS = frozenset({None, "", "localhost", "127.0.0.1", "::1"})
EXIT_REFUSED = 3


# --- the spend journal ------------------------------------------------------------------------


class Journal:
    """What this machine has already committed, across runs: the $5 total is checked here."""

    def __init__(self, path: Path):
        self.path = path
        self.data: dict[str, Any] = {"version": 1, "runs": {}}
        if path.is_file():
            self.data = json.loads(path.read_text())

    @property
    def runs(self) -> dict[str, Any]:
        return self.data["runs"]

    def committed_usd(self) -> float:
        total = 0.0
        for run in self.runs.values():
            for entry in run["items"].values():
                if entry["state"] == "settled":
                    total += (
                        entry["settled_usd"]
                        if entry["settled_usd"] is not None
                        else entry["estimate_usd"]
                    )
                elif entry["state"] in {"started", "unknown"}:
                    total += entry["estimate_usd"]
        return round(total, 6)

    def unknown(self) -> list[str]:
        return [
            f"{run_id}/{key}"
            for run_id, run in self.runs.items()
            for key, entry in run["items"].items()
            if entry["state"] in {"started", "unknown"}
        ]

    def save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        scratch = self.path.with_suffix(".tmp")
        scratch.write_text(json.dumps(self.data, indent=2, sort_keys=True))
        os.replace(scratch, self.path)

    def open_run(self, run_id: str, stage: str, max_usd: float, digest: str) -> None:
        self.runs[run_id] = {
            "stage": stage,
            "approved_max_usd": max_usd,
            "digest": digest,
            "opened_at": datetime.now(UTC).isoformat(),
            "verdict": None,
            "items": {},
        }
        self.save()

    def record(self, run_id: str, key: str, state: str, estimate: float, settled=None) -> None:
        self.runs[run_id]["items"][key] = {
            "state": state,
            "estimate_usd": estimate,
            "settled_usd": settled,
        }
        self.save()

    def close_run(self, run_id: str, verdict: str) -> None:
        self.runs[run_id]["verdict"] = verdict
        self.save()


# --- gates ------------------------------------------------------------------------------------


@dataclass(frozen=True)
class Gate:
    name: str
    ok: bool
    detail: str


@dataclass(frozen=True)
class Environment:
    run_id: str | None
    max_usd: str | None
    ai_environment: str
    database_host: str | None
    has_api_key: bool
    intent_budget_usd: float
    monthly_budget_usd: float


def load_environment(env: Mapping[str, str]) -> Environment:
    try:
        host = make_url(settings.database_url).host
    except Exception:  # noqa: BLE001
        host = "unparseable"
    return Environment(
        run_id=env.get(RUN_ID_ENV) or None,
        max_usd=env.get(MAX_USD_ENV) or None,
        ai_environment=settings.ai_usage_environment,
        database_host=host,
        has_api_key=bool(settings.openai_api_key),
        intent_budget_usd=settings.ai_openai_intent_review_budget_usd,
        monthly_budget_usd=settings.ai_openai_monthly_budget_usd,
    )


def code_revision() -> dict[str, Any] | None:
    """The commit that produced a report, and whether the tree had uncommitted changes."""
    root = Path(__file__).resolve()
    if len(root.parents) < 6:
        return None
    try:
        git = ["git", "-C", str(root.parents[5])]
        commit = subprocess.run(
            [*git, "rev-parse", "HEAD"], capture_output=True, text=True, timeout=10, check=True
        ).stdout.strip()
        dirty = subprocess.run(
            [*git, "status", "--porcelain", "--untracked-files=no"],
            capture_output=True,
            text=True,
            timeout=10,
            check=True,
        ).stdout.strip()
    except (OSError, subprocess.SubprocessError):
        return None
    return {"commit": commit, "dirty": bool(dirty)}


def pinned_digest() -> str | None:
    root = Path(__file__).resolve()
    if len(root.parents) < 6:
        return None
    doc = root.parents[5].joinpath(*DOC_PATH)
    if not doc.is_file():
        return None
    match = _DIGEST_LINE.search(doc.read_text())
    return match.group(1) if match else None


def _approved_max(raw: str | None) -> float | None:
    try:
        value = float(raw) if raw is not None else None
    except ValueError:
        return None
    return value if value is not None and math.isfinite(value) and value > 0 else None


def check_gates(
    *,
    execute: bool,
    stage: str,
    rows: list[q.SpendRow],
    journal: Journal,
    environment: Environment,
    digest: str,
    pinned: str | None,
) -> list[Gate]:
    estimate = q.subtotal([r for r in rows if r.item.stage == stage])
    approved = _approved_max(environment.max_usd)
    run_id = environment.run_id
    prefix = run_id.rsplit("-", 1)[0] if run_id else None
    pilot = journal.runs.get(f"{prefix}-pilot") if prefix else None
    gates = [
        Gate(
            "execute_flag",
            execute,
            "--execute was passed" if execute else "dry run: pass --execute",
        ),
        Gate(
            "approved_run_id",
            bool(run_id and RUN_ID_PATTERN.match(run_id) and run_id.endswith(f"-{stage}")),
            f"{RUN_ID_ENV} names this {stage} stage"
            if run_id and RUN_ID_PATTERN.match(run_id) and run_id.endswith(f"-{stage}")
            else f"set {RUN_ID_ENV}=iqual-<tag>-{stage}",
        ),
        Gate(
            "approved_maximum",
            approved is not None and approved <= q.PER_RUN_CAP_USD,
            f"{MAX_USD_ENV} is within the ${q.PER_RUN_CAP_USD:.2f} per-run hard cap"
            if approved is not None and approved <= q.PER_RUN_CAP_USD
            else f"set {MAX_USD_ENV} to a number above 0 and at most {q.PER_RUN_CAP_USD:.2f}",
        ),
        Gate(
            "estimate_within_approved_maximum",
            approved is not None and estimate <= approved,
            f"${estimate:.4f} for {stage} against an approved ${approved or 0:.4f}",
        ),
        Gate(
            "per_call_ceiling",
            all(r.estimate_usd <= q.PER_CALL_CEILING_USD for r in rows),
            f"every call estimates at or under ${q.PER_CALL_CEILING_USD:.2f}",
        ),
        Gate(
            "total_cap",
            journal.committed_usd() + estimate <= q.TOTAL_CAP_USD,
            f"${journal.committed_usd():.4f} committed + ${estimate:.4f} against "
            f"${q.TOTAL_CAP_USD:.2f} in total",
        ),
        Gate(
            "no_unreconciled_charge",
            not journal.unknown(),
            "no started or unknown call is outstanding"
            if not journal.unknown()
            else f"outstanding: {', '.join(journal.unknown())}",
        ),
        Gate(
            "run_not_yet_executed",
            run_id is None or run_id not in journal.runs,
            "this run id has not run" if run_id not in journal.runs else f"{run_id} already ran",
        ),
        Gate(
            "pilot_passed",
            stage == "pilot" or (pilot is not None and pilot["verdict"] == "pass"),
            "not needed for the pilot"
            if stage == "pilot"
            else "the pilot of this prefix passed"
            if pilot and pilot["verdict"] == "pass"
            else f"run and pass {prefix}-pilot first",
        ),
        Gate(
            "preregistration_pinned",
            pinned == digest,
            "QUALIFICATION.md pins this digest"
            if pinned == digest
            else "QUALIFICATION.md does not pin the digest this code computes",
        ),
        Gate(
            "development_environment",
            environment.ai_environment == "development",
            "AI_USAGE_ENVIRONMENT is development"
            if environment.ai_environment == "development"
            else "set AI_USAGE_ENVIRONMENT=development; production is never used",
        ),
        Gate(
            "local_database",
            environment.database_host in _LOCAL_HOSTS
            or str(environment.database_host).startswith("/"),
            "the ledger database is on this machine"
            if environment.database_host in _LOCAL_HOSTS
            or str(environment.database_host).startswith("/")
            else "DATABASE_URL must point at a local scratch database",
        ),
        Gate(
            "api_key_present",
            environment.has_api_key,
            "OPENAI_API_KEY is set (value never shown)"
            if environment.has_api_key
            else "export OPENAI_API_KEY in the shell",
        ),
        Gate(
            "capability_budgets",
            environment.intent_budget_usd >= estimate > 0
            and environment.monthly_budget_usd >= estimate,
            "the intent_review and monthly OpenAI budgets cover this stage"
            if environment.intent_budget_usd >= estimate
            and environment.monthly_budget_usd >= estimate
            else "set AI_OPENAI_INTENT_REVIEW_BUDGET_USD and AI_OPENAI_MONTHLY_BUDGET_USD to at "
            f"least ${estimate:.4f}",
        ),
    ]
    return gates


# --- artifacts --------------------------------------------------------------------------------


def _montage(built: Mapping[str, q.BuiltCase]) -> Image.Image:
    thumb_w, thumb_h, label_h = 180, 320, 34
    columns = max(len(c.frames) for c in built.values())
    sheet = Image.new("RGB", (columns * thumb_w, len(built) * (thumb_h + label_h)), (28, 28, 30))
    draw = ImageDraw.Draw(sheet)
    font = ImageFont.load_default(size=13)
    for row, case_built in enumerate(built.values()):
        top = row * (thumb_h + label_h)
        for column, frame in enumerate(case_built.frames):
            thumb = Image.open(io.BytesIO(frame.jpeg)).resize((thumb_w, thumb_h))
            sheet.paste(thumb, (column * thumb_w, top + label_h))
            mark = " (text span)" if frame.anchor_ref else ""
            draw.text(
                (column * thumb_w + 4, top + 18),
                f"{frame.frame_id}  {frame.timestamp_s:.2f}s{mark}",
                fill=(220, 220, 220),
                font=font,
            )
        draw.text((4, top + 2), case_built.case.name, fill=(255, 214, 90), font=font)
    return sheet


def write_artifacts(
    out: Path,
    built: Mapping[str, q.BuiltCase],
    rows: list[q.SpendRow],
    checks: list[q.DryRunCheck],
    gates: list[Gate],
    digest: str,
    pinned: str | None,
) -> Path:
    out.mkdir(parents=True, exist_ok=True)
    cases: dict[str, Any] = {}
    for name, case_built in built.items():
        (out / "frames" / name).mkdir(parents=True, exist_ok=True)
        (out / "prompts").mkdir(exist_ok=True)
        frames = []
        for frame in case_built.frames:
            path = out / "frames" / name / f"{frame.frame_id}.jpg"
            path.write_bytes(frame.jpeg)
            frames.append(
                {
                    "id": frame.frame_id,
                    "t_s": frame.timestamp_s,
                    "sha256": frame.sha256,
                    "bytes": len(frame.jpeg),
                    "anchored_on_text_span": bool(frame.anchor_ref),
                }
            )
        (out / "prompts" / f"{name}.txt").write_text(case_built.built.prompt)
        catalog = case_built.catalog
        cases[name] = {
            "summary": case_built.case.summary,
            "decoy": case_built.case.decoy,
            "frames": frames,
            "catalog_digest": catalog.digest,
            "requirements": [r.model_dump() for r in catalog.requirements],
            "withheld": [w.model_dump() for w in catalog.withheld],
            "candidates": [
                {"kind": c.kind, "element": c.element_ref, "target": c.target, "auto": c.auto}
                for c in catalog.candidates
            ],
            "estimate_usd": case_built.estimate_usd,
            "estimated_input_tokens": case_built.input_tokens,
            "predicates": [
                {"id": p.id, "kind": p.kind, "statement": p.statement}
                for p in case_built.case.predicates
            ],
        }
    _montage(built).save(out / "montage.png")
    summary = {
        "version": 1,
        "mode": "dry_run",
        "model": VISION_MODEL,
        "prompt_version": PROMPT_VERSION,
        "price_version": costs.PRICE_VERSION,
        "preregistration_digest": digest,
        "pinned_digest": pinned,
        "code_revision": code_revision(),
        "caps_usd": {
            "per_call": q.PER_CALL_CEILING_USD,
            "per_run": q.PER_RUN_CAP_USD,
            "total": q.TOTAL_CAP_USD,
        },
        "spend": {
            "pilot_usd": q.subtotal(rows, "pilot"),
            "full_usd": q.subtotal(rows, "full"),
            "all_usd": q.subtotal(rows),
            "rows": [
                {
                    "stage": r.item.stage,
                    "item": r.item.key,
                    "frames": r.frames,
                    "estimated_input_tokens": r.input_tokens,
                    "max_output_tokens": r.output_tokens,
                    "estimate_usd": r.estimate_usd,
                    "cumulative_usd": r.cumulative_usd,
                }
                for r in rows
            ],
        },
        "cases": cases,
        "checks": [asdict(c) for c in checks],
        "execution_gates": [asdict(g) for g in gates],
    }
    path = out / "dry-run.json"
    path.write_text(json.dumps(summary, indent=2, sort_keys=True))
    return path


# --- printing ---------------------------------------------------------------------------------


def print_spend_table(rows: list[q.SpendRow]) -> None:
    print(
        "SPEND TABLE (the repo's reservation estimate over the exact request; "
        "a ceiling, not a bill)"
    )
    print(
        f"{'stage':<6} {'item':<34} {'frames':>6} {'in_tok':>8} {'out_tok':>8} "
        f"{'est_usd':>9} {'cum_usd':>9}"
    )
    for r in rows:
        print(
            f"{r.item.stage:<6} {r.item.key:<34} {r.frames:>6} {r.input_tokens:>8,} "
            f"{r.output_tokens:>8,} {r.estimate_usd:>9.4f} {r.cumulative_usd:>9.4f}"
        )
    pilot, full, total = q.subtotal(rows, "pilot"), q.subtotal(rows, "full"), q.subtotal(rows)
    print(f"SPEND_TOTAL: pilot=${pilot:.4f} full=${full:.4f} all=${total:.4f}")
    print(
        f"CAPS: per_call=${q.PER_CALL_CEILING_USD:.2f} per_run=${q.PER_RUN_CAP_USD:.2f} "
        f"total=${q.TOTAL_CAP_USD:.2f} headroom_total=${q.TOTAL_CAP_USD - total:.4f} "
        f"headroom_per_run=${q.PER_RUN_CAP_USD - max(pilot, full):.4f}"
    )


def print_checks(title: str, checks: list[q.DryRunCheck]) -> bool:
    ok = all(c.ok for c in checks)
    print(f"{title}: {'ok' if ok else 'FAILED'} ({sum(c.ok for c in checks)}/{len(checks)})")
    for check in checks:
        print(f"  {'ok  ' if check.ok else 'FAIL'} {check.name}: {check.detail}")
    return ok


# --- running ----------------------------------------------------------------------------------


@dataclass
class ItemOutcome:
    item: q.Item
    status: str
    results: list[q.PredicateResult]
    provenance: dict[str, Any] | None
    reply: str | None
    void: str | None
    estimate_usd: float
    settled_usd: float | None
    posts: dict[str, Any]


def _posts_json(judgement: q.Judgement) -> dict[str, Any]:
    return {
        language: None
        if posts is None
        else [
            {"kind": p.kind, "event_type": p.event_type, "content": p.content, "payload": p.payload}
            for p in posts
        ]
        for language, posts in judgement.posts.items()
    }


def _percentile(values: list[float], fraction: float) -> float | None:
    if not values:
        return None
    ordered = sorted(values)
    return ordered[min(len(ordered) - 1, math.ceil(fraction * len(ordered)) - 1)]


def _write_json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, indent=2, sort_keys=True, default=str))


def execute_stage(
    *,
    stage: str,
    items: list[q.Item],
    built: Mapping[str, q.BuiltCase],
    run_id: str,
    max_usd: float,
    out: Path,
    journal: Journal,
    digest: str,
    client: OpenAIVisionClient | None,
) -> tuple[list[ItemOutcome], str | None]:
    run_dir = out / "runs" / run_id
    journal.open_run(run_id, stage, max_usd, digest)
    outcomes: list[ItemOutcome] = []
    stopped: str | None = None
    for item in [i for i in items if i.stage == stage]:
        case_built = built[item.case]
        context = RunContext(
            request_id=f"intent-review-qualification:{run_id}:{item.slug}",
            request_id_authoritative=True,
            usage_purpose="live_eval",
            test_run_id=run_id,
            estimated_max_cost_usd=max_usd,
            reservation_approved=True,
            extra={"intent_review_qualification": item.key},
        )
        key = costs.logical_call_id(
            feature=INTENT_REVIEW_FEATURE,
            model=VISION_MODEL,
            prompt="",
            ctx=context,
            attempt=0,
        )
        request = q.request_for(case_built, idempotency_key=key, run_context=context)
        journal.record(run_id, item.key, "started", case_built.estimate_usd)

        def persist(vision: VisionResult, provenance: MediaReviewProvenance, _item=item) -> None:
            _write_json(
                run_dir / "replies" / f"{_item.slug}.json",
                {
                    "item": _item.key,
                    "raw_text": vision.raw_text,
                    "response_id": vision.response_id,
                    "status": vision.status,
                    "resolved_model": vision.model_used,
                    "usage": provenance.usage,
                },
            )

        result = invoke_bounded_media_review(request, client=client, persist_response=persist)
        provenance = result.provenance.as_json()
        outcome = ItemOutcome(
            item=item,
            status=result.status,
            results=[],
            provenance=provenance,
            reply=result.raw_text,
            void=None,
            estimate_usd=case_built.estimate_usd,
            settled_usd=result.provenance.settled_usd,
            posts={},
        )
        if result.status != "ok":
            released = result.status != "provider_unknown_outcome"
            journal.record(
                run_id,
                item.key,
                "released" if released else "unknown",
                case_built.estimate_usd,
                0.0 if released else None,
            )
            outcomes.append(outcome)
            stopped = f"{item.key}: provider {result.status}" + (
                f" ({result.detail})" if result.detail else ""
            )
            break
        journal.record(
            run_id, item.key, "settled", case_built.estimate_usd, result.provenance.settled_usd
        )
        if result.provenance.response_status != "completed":
            judgement = q.judge(
                case_built, "", visual_input_received=result.provenance.visual_input_received
            )
            outcome.void = f"response_{result.provenance.response_status}"
        else:
            judgement = q.judge(
                case_built,
                result.raw_text or "",
                visual_input_received=result.provenance.visual_input_received,
            )
            outcome.void = judgement.void
        outcome.results = q.score(case_built.case, judgement)
        outcome.posts = _posts_json(judgement)
        _write_json(run_dir / "receipts" / f"{item.slug}.json", outcome.posts)
        outcomes.append(outcome)
        if outcome.void is not None:
            stopped = f"{item.key}: void review ({outcome.void})"
            break
        if (result.provenance.settled_usd or 0) > q.PER_CALL_CEILING_USD:
            stopped = f"{item.key}: settled above the ${q.PER_CALL_CEILING_USD:.2f} ceiling"
            break
    return outcomes, stopped


_T95 = {1: 12.706, 2: 4.303}


def pilot_projection(
    outcomes: list[ItemOutcome], items: list[q.Item], built: Mapping[str, q.BuiltCase]
) -> dict[str, Any] | None:
    """What the full run should settle at, from the pilot's calls; the ceiling is the bound."""
    settled = [o.settled_usd for o in outcomes if o.settled_usd is not None]
    if len(settled) - 1 not in _T95:
        return None
    remaining = [i for i in items if i.stage == "full"]
    mean, sd = statistics.fmean(settled), statistics.stdev(settled)
    half = _T95[len(settled) - 1] * sd / math.sqrt(len(settled))
    return {
        "items_remaining": len(remaining),
        "mean_per_call_usd": round(mean, 6),
        "sd_usd": round(sd, 6),
        "projected_full_usd": round(mean * len(remaining), 6),
        "band95_usd": [
            round(max(0.0, mean - half) * len(remaining), 6),
            round((mean + half) * len(remaining), 6),
        ],
        "ceiling_usd": round(sum(built[i.case].estimate_usd for i in remaining), 6),
    }


def stage_report(
    *,
    stage: str,
    built: Mapping[str, q.BuiltCase],
    run_id: str,
    max_usd: float,
    digest: str,
    items: list[q.Item],
    outcomes: list[ItemOutcome],
    stopped: str | None,
) -> dict[str, Any]:
    planned = [i for i in items if i.stage == stage]
    ran = {o.item.key for o in outcomes}
    failures = [f"{o.item.key}:{r.id}" for o in outcomes for r in o.results if not r.passed]
    not_run = [i.key for i in planned if i.key not in ran]
    latencies = [
        o.provenance["latency_ms"] / 1000
        for o in outcomes
        if o.provenance and o.provenance.get("latency_ms") is not None
    ]
    settled = [o.settled_usd for o in outcomes if o.settled_usd is not None]
    estimates = [o.estimate_usd for o in outcomes if o.settled_usd is not None]
    verdict = "pass" if not failures and not stopped and not not_run else "fail"
    return {
        "version": 1,
        "run_id": run_id,
        "stage": stage,
        "model": VISION_MODEL,
        "prompt_version": PROMPT_VERSION,
        "price_version": costs.PRICE_VERSION,
        "preregistration_digest": digest,
        "code_revision": code_revision(),
        "approved_max_usd": max_usd,
        "verdict": verdict,
        "stopped": stopped,
        "failed_predicates": failures,
        "not_run": not_run,
        "spend": {
            "estimated_usd": round(sum(o.estimate_usd for o in outcomes), 6),
            "settled_usd": round(sum(settled), 6),
            "max_call_settled_usd": max(settled) if settled else None,
            "settled_over_estimate": round(sum(settled) / sum(estimates), 4) if estimates else None,
        },
        "latency_s": {"p50": _percentile(latencies, 0.5), "p95": _percentile(latencies, 0.95)},
        "projection": pilot_projection(outcomes, items, built) if stage == "pilot" else None,
        "items": [
            {
                "item": o.item.key,
                "status": o.status,
                "void": o.void,
                "predicates": [asdict(r) for r in o.results],
                "estimate_usd": o.estimate_usd,
                "settled_usd": o.settled_usd,
                "provenance": o.provenance,
                "reply_file": f"runs/{run_id}/replies/{o.item.slug}.json",
                "receipts_file": f"runs/{run_id}/receipts/{o.item.slug}.json",
            }
            for o in outcomes
        ],
        "finished_at": datetime.now(UTC).isoformat(),
    }


def print_report(report: dict[str, Any]) -> None:
    print(f"RUN: {report['run_id']} stage={report['stage']} verdict={report['verdict']}")
    for item in report["items"]:
        failed = [p["id"] for p in item["predicates"] if not p["passed"]]
        verdict = (
            "pass"
            if not failed and not item["void"]
            else "FAIL " + ",".join(failed or [item["void"]])
        )
        print(
            f"  {item['item']:<34} {item['status']:<10} settled=${item['settled_usd'] or 0:.4f} "
            f"{verdict}"
        )
    spend = report["spend"]
    print(
        f"SPEND_SETTLED: ${spend['settled_usd']:.4f} of ${spend['estimated_usd']:.4f} estimated; "
        f"max call ${spend['max_call_settled_usd'] or 0:.4f}"
    )
    projection = report["projection"]
    if projection:
        low, high = projection["band95_usd"]
        print(
            f"PILOT_PROJECTION: the {projection['items_remaining']}-item full run settles near "
            f"${projection['projected_full_usd']:.4f} (95% band ${low:.4f} to ${high:.4f}; "
            f"hard ceiling ${projection['ceiling_usd']:.4f})"
        )
    if report["stopped"]:
        print(f"STOPPED: {report['stopped']}")
    if report["not_run"]:
        print(f"NOT_RUN: {', '.join(report['not_run'])}")


# --- entry point ------------------------------------------------------------------------------


def main(
    argv: list[str] | None = None,
    *,
    client: OpenAIVisionClient | None = None,
    env: Mapping[str, str] | None = None,
) -> int:
    parser = argparse.ArgumentParser(
        prog="python -m app.cli.qualify_intent_review",
        description="Zero-cost by default. "
        "Preregistration: docs/reviews/intent-review/QUALIFICATION.md",
    )
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--dry-run", action="store_true", help="the default: no provider, no ledger")
    mode.add_argument("--execute", action="store_true", help="spend, only if every gate holds")
    parser.add_argument("--stage", choices=("pilot", "full"), default="pilot")
    parser.add_argument("--out", type=Path, default=DEFAULT_OUT)
    args = parser.parse_args(argv)
    environment = load_environment(os.environ if env is None else env)

    built = q.build_all()
    items = q.plan_items()
    rows = q.spend_table(built, items)
    digest = q.preregistration_digest(built, items)
    pinned = pinned_digest()
    journal = Journal(args.out / "spend.json")
    gates = check_gates(
        execute=args.execute,
        stage=args.stage,
        rows=rows,
        journal=journal,
        environment=environment,
        digest=digest,
        pinned=pinned,
    )

    checks = [
        *q.run_scorer_checks(built),
        q.run_honesty_check(built),
        *q.run_discrimination_checks(built),
        q.DryRunCheck(
            "preregistration_pinned",
            pinned == digest,
            "QUALIFICATION.md pins the digest this code computes"
            if pinned == digest
            else "QUALIFICATION.md is missing or pins another digest",
        ),
    ]
    mode_name = "execute" if args.execute else "dry-run"
    allowance = (
        "provider calls allowed if every gate holds"
        if args.execute
        else "no provider call, no ledger write"
    )
    print(f"INTENT_REVIEW_QUALIFICATION: {mode_name} ({allowance})")
    print(
        f"MODEL: {VISION_MODEL}  PROMPT_VERSION: {PROMPT_VERSION}  "
        f"PRICE_VERSION: {costs.PRICE_VERSION}"
    )
    print(f"PREREG_DIGEST: {digest}")
    print_spend_table(rows)
    checks_ok = print_checks("DRY_RUN_CHECKS", checks)
    path = write_artifacts(args.out, built, rows, checks, gates, digest, pinned)
    print(f"ARTIFACTS: {path.parent}/ (dry-run.json, montage.png, frames/, prompts/)")
    print(f"DRY_RUN_RESULT: {'pass' if checks_ok else 'fail'}")

    failed = [g for g in gates if not g.ok]
    print(f"EXECUTION_GATES for stage {args.stage}: {len(gates) - len(failed)}/{len(gates)} hold")
    for gate in gates:
        print(f"  {'ok  ' if gate.ok else 'FAIL'} {gate.name}: {gate.detail}")
    if not args.execute:
        print("EXECUTION: not requested (dry run). No provider was contacted.")
        return 0 if checks_ok else 1
    if failed or not checks_ok:
        print("EXECUTION: refused. No client was built and nothing was reserved.")
        return EXIT_REFUSED
    max_usd = _approved_max(environment.max_usd)
    if max_usd is None or environment.run_id is None:
        return EXIT_REFUSED
    outcomes, stopped = execute_stage(
        stage=args.stage,
        items=items,
        built=built,
        run_id=environment.run_id,
        max_usd=max_usd,
        out=args.out,
        journal=journal,
        digest=digest,
        client=client,
    )
    report = stage_report(
        stage=args.stage,
        built=built,
        run_id=environment.run_id,
        max_usd=max_usd,
        digest=digest,
        items=items,
        outcomes=outcomes,
        stopped=stopped,
    )
    _write_json(args.out / "runs" / environment.run_id / "report.json", report)
    journal.close_run(environment.run_id, report["verdict"])
    print_report(report)
    print(f"REPORT: {args.out / 'runs' / environment.run_id / 'report.json'}")
    print(f"QUALIFICATION_STAGE: {args.stage} {report['verdict']}")
    pilot = journal.runs.get(f"{environment.run_id.rsplit('-', 1)[0]}-pilot")
    if args.stage == "full":
        both = report["verdict"] == "pass" and bool(pilot) and pilot["verdict"] == "pass"
        print(f"QUALIFICATION: {'pass' if both else 'fail'} (pilot and full, every predicate)")
    return 0 if report["verdict"] == "pass" else 1


if __name__ == "__main__":
    sys.exit(main())
