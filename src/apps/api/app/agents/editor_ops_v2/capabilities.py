"""Provider-neutral discovery of a bounded subset of the real editor parser."""

from __future__ import annotations

import json
from typing import Any

from app.agents.editor_ops_v2 import lane_specs

COMPOSITION_OPERATIONS = (
    "set_total_duration",
    "retime_text_sequence",
    "patch_text",
    "reorder_clips_by",
    "set_texts_timing",
    "replace_text_sequence",
)


def build_editor_capability_manifest(snapshot: dict[str, Any]) -> dict[str, Any]:
    """Describe existing contracts. Discovery never replaces parse or draft dry-run."""
    from app.agents import edit_copilot as parser
    from app.agents.editor_ops_v2 import timeline
    from app.kria.compose_with_ops import compose_unmet_with_ops
    from app.services.kria_editor_ops_text import EXTRA_PATCH_FIELDS

    specs = {spec.name: spec for spec in lane_specs()}
    operations = []
    for name in COMPOSITION_OPERATIONS:
        available = parser._family_allowed(name, snapshot)
        constraints: dict[str, Any] = {}
        if name == "set_total_duration":
            constraints["strategies"] = list(timeline._STRATEGIES)
            constraints["maximum_target_s"] = timeline._max_total_s()
        elif name == "reorder_clips_by":
            constraints["criteria"] = list(timeline._ORDER_CRITERIA)
            constraints["directions"] = list(timeline._ORDER_DIRECTIONS)
        elif name == "patch_text":
            constraints["accepted_patch_fields"] = sorted(
                parser._STYLE_PATCH_FIELDS | EXTRA_PATCH_FIELDS | {"size_scale"}
            )
        elif name == "replace_text_sequence":
            constraints["compose_unmet_with_ops"] = compose_unmet_with_ops()
            constraints["mode_weight_honored"] = compose_unmet_with_ops()
            if compose_unmet_with_ops():
                constraints["modes"] = ["replace", "cumulative"]
                constraints["weights"] = ["equal", "length"]
            else:
                constraints["effective_mode"] = "replace"
                constraints["effective_weight"] = "equal"
                constraints["mode_weight_input"] = "nondefault_values_refused"
        operations.append(
            {
                "name": name,
                "required_fields": sorted(specs[name].required | parser._OP_REQUIRED[name]),
                "accepted_fields": sorted({"op"} | parser._OP_FIELDS[name]),
                "families": sorted(specs[name].family),
                "availability": available,
                "availability_scope": "parser_version_family_surface_gate",
                "unavailable_reason": None if available else "snapshot_version_or_family_gate",
                "constraints": constraints,
                "constraint_coverage": "partial",
                "validation": "existing_parser_and_compiler_dry_run_required",
            }
        )
    return {
        "schema_version": 1,
        "editor_ops_version": snapshot.get("editor_ops_version"),
        "base_generation": snapshot.get("base_generation"),
        "allowed_op_families": snapshot.get("allowed_op_families", []),
        "max_ops": parser._operation_limit(snapshot),
        "limits": {
            key: snapshot[key]
            for key in ("max_duration_s", "remaining_duration_s", "editor_limits")
            if key in snapshot
        },
        "timeline_patch_capabilities": snapshot.get("timeline_patch_capabilities", {}),
        "text_appearance": snapshot.get("text_appearance", {}),
        "targets": {
            "slots": [
                {
                    "index": index,
                    **{
                        key: row[key]
                        for key in (
                            "key",
                            "slot_id",
                            "parent_segment_id",
                            "clip_index",
                            "removed",
                            "read_only",
                            "source_end_s",
                            "playback_rate",
                            "output_start_s",
                            "output_end_s",
                            "clip_id",
                            "media_id",
                            "source_duration_s",
                            "in_s",
                            "duration_s",
                        )
                        if key in row
                    },
                }
                for index, row in enumerate(snapshot.get("slots", []))
                if isinstance(row, dict)
            ],
            "texts": [
                {
                    "index": index,
                    **{
                        key: row[key]
                        for key in (
                            "id",
                            "sequence_source_id",
                            "start_s",
                            "end_s",
                            "x_frac",
                            "y_frac",
                            "alignment",
                        )
                        if key in row
                    },
                }
                for index, row in enumerate(snapshot.get("text_bars", []))
                if isinstance(row, dict)
            ],
        },
        "operations": operations,
        "composition_constraints": [
            "Distinct per-target patches exceeding "
            f"the {parser._operation_limit(snapshot)}-operation "
            "budget need multi-batch composition, which this discovery slice does not provide; "
            "one bulk selector can apply the same patch to multiple targets",
            "preserve_text on any operation suppresses text operations; do not combine it "
            "with retime_text_sequence or patch_text",
            "duration alone preserves authored text timing; explicit fit_to_video retimes "
            "selected sequence after the final timeline duration",
            "patch_text normalizes supported numeric style values; "
            "discovery is not a complete JSON schema",
            "random ordering uses the existing server-owned seed and complete clip permutation",
            "parse, compile and dry-run against the current draft before offering approval; "
            "existing Undo and Save own persistence",
        ],
    }


def serialize_editor_capabilities(snapshot: dict[str, Any]) -> str:
    """The same deterministic input catalog can be supplied to either provider."""
    return json.dumps(
        build_editor_capability_manifest(snapshot),
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
    )
