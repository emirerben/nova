"""Replay a prod montage's phone contract gate from its admin debug capture (KRI-1376).

    cd src/apps/api && python -m scripts.replay_phone_contract_gate <debug.json> <config.json>

Both inputs come from ``scripts/admin.py --prod GET``. The worker's plan is rebuilt from the
job's pinned brief, strategy and media snapshot (``phone_montage_preflight``, the worker's own
planner), then each text layout goes through ``compile_phone_guided_repaired`` with the job's
contract and prod's verified phone features, then the pin-time ``verify_phone_recipe``. Besides
the planner's own text it tries the layouts a text pass may return: the longest pinned text
dropped, every other pinned text dropped, and the longest moved to the top. No model call, no
storage, no write; the capture's media is never read. Prints one JSON line per layout.
"""

from __future__ import annotations

import json
import sys
from unittest import mock


def _normal(text: str) -> str:
    return " ".join(text.split())


def main(debug_path: str, config_path: str) -> int:
    from app.config import settings
    from app.kria.brief import CreativeBrief
    from app.pipeline.guided_story import GuidedStoryExecutionPlan, compile_proposal_execution_plan
    from app.pipeline.phone_plan_repair import _dry_run_bindings, compile_phone_guided_repaired
    from app.services import phone_montage_preflight
    from app.services.creator_render_contract import build_render_contract, verify_phone_recipe

    job = json.load(open(debug_path))["job"]
    features = json.load(open(config_path))["verified_features"]
    candidates = job["all_candidates"]
    binding = job["assembly_plan"]["creator_brief_binding"]
    brief = CreativeBrief.model_validate(binding["brief"])
    media = binding["media_snapshot"]
    strategy = candidates["creator_strategy"]
    with (
        mock.patch.object(type(settings), "phone_rendering_for", lambda _self, _user: True),
        mock.patch.object(settings, "phone_render_verified_features", features),
    ):
        planned = phone_montage_preflight.preflight_montage_plan(
            creator_id=job["user_id"],
            edit_format=candidates["edit_format"],
            brief=brief,
            strategy=strategy,
            media_snapshot=media,
            visuals=[],
            client_protocol=candidates.get("creator_client_protocol"),
        )
        if not isinstance(planned, tuple):
            print(json.dumps({"job_id": job["id"], "replayed": False, "reason": str(planned)}))
            return 2
        plan = GuidedStoryExecutionPlan.model_validate(
            compile_proposal_execution_plan(planned[0].snapshot)
        )
        contract = build_render_contract(
            strategy, generation_id="replay", brief=brief, media_snapshot=media
        )
        pinned = sorted(
            t["text"] for t in job["assembly_plan"]["creator_render_requirements"]["exact_texts"]
        )
        bindings, visuals, narration = _dry_run_bindings(
            plan, planned[0].snapshot, media.get("clip_assignments") or []
        )
        source_audio = {b.media_id: b.original.has_audio for b in bindings}
        longest = _normal(max((t.text for t in contract.exact_texts), key=len))
        others = {_normal(t.text) for t in contract.exact_texts} - {longest}
        texts = plan.text_elements
        layouts = {
            "planner": texts,
            "longest_dropped": [e for e in texts if _normal(e.text) != longest],
            "others_dropped": [e for e in texts if _normal(e.text) not in others],
            "longest_on_top": [
                e.model_copy(update={"y_frac": 0.16}) if _normal(e.text) == longest else e
                for e in texts
            ],
        }
        for name, rows in layouts.items():
            row = {
                "job_id": job["id"],
                "layout": name,
                "contract_matches_pinned": sorted(t.text for t in contract.exact_texts) == pinned,
            }
            try:
                result = compile_phone_guided_repaired(
                    plan.model_copy(update={"text_elements": rows}),
                    bindings,
                    contract=contract,
                    visuals=visuals,
                    narration=narration,
                    landscape_fit="fit" if candidates.get("landscape_fit") == "fit" else "fill",
                )
                verify_phone_recipe(contract, result.recipe, source_audio=source_audio)
                row |= {"outcome": "renders", "notes": result.notes}
            except Exception as exc:  # noqa: BLE001 - the refusal is the result
                row |= {
                    "outcome": "refused",
                    "error": type(exc).__name__,
                    "message": str(exc),
                    "decline_reason": getattr(exc, "decline_reason", None),
                }
            print(json.dumps(row, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    sys.exit(main(*sys.argv[1:3]))
