"""Complete, deterministic requests over the current video's text timing."""

from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Any, Literal

from app.kria.reply_language import say

GapPolicy = Literal["scale", "close", "fixed"]


@dataclass(frozen=True)
class SequenceTimingIntent:
    mode: Literal["preserve", "fill"]
    target_s: float | None = None
    gap_policy: GapPolicy = "scale"


@dataclass(frozen=True)
class SequenceTimingPlan:
    operations: tuple[dict[str, Any], ...]
    reply: str
    intent: SequenceTimingIntent | None = None
    source_id: str | None = None
    target_ids: tuple[str, ...] = ()
    span_s: tuple[float, float] | None = None


def sequence_timing_plan(intent: SequenceTimingIntent, snapshot: dict) -> SequenceTimingPlan | None:
    if snapshot.get("editor_ops_version") != 2:
        return None
    total_raw = snapshot.get("total_duration_s")
    total = (
        float(total_raw)
        if isinstance(total_raw, (int, float)) and not isinstance(total_raw, bool)
        else 0.0
    )
    end = intent.target_s if intent.target_s is not None else total
    if not math.isfinite(end) or end <= 0 or end > float(snapshot.get("max_duration_s") or 120):
        return SequenceTimingPlan(
            (),
            say(
                en="Which supported video length should I use?",
                tr="Hangi desteklenen video uzunluğunu kullanayım?",
            ),
            intent,
        )
    operations = []
    if intent.target_s is not None:
        operations.append({"op": "set_total_duration", "target_s": end, "strategy": "proportional"})
    if intent.mode == "preserve":
        return SequenceTimingPlan(
            tuple(operations),
            say(
                en=f"Set the video to {end:g}s while preserving the text timing.",
                tr=f"Yazıların zamanlamasını koruyarak videoyu {end:g} saniye yap.",
            ),
            intent,
        )
    rows = [
        r
        for r in snapshot.get("text_bars", [])
        if not r.get("removed") and not r.get("caption_cue") and r.get("sequence_source_id")
    ]
    groups = {r["sequence_source_id"] for r in rows}
    if not rows or len(groups) != 1:
        return SequenceTimingPlan(
            (),
            say(
                en="Which complete word sequence should I retime?",
                tr="Hangi kelime dizisinin tamamını yeniden zamanlayayım?",
            ),
            intent,
        )
    source = next(iter(groups))

    def order(row):
        parts = str(row.get("id") or "").removeprefix(source).split("::sequence-")
        if parts[0] or len(parts) < 2 or any(not p.isdigit() for p in parts[1:]):
            raise ValueError("Missing word order")
        return tuple(int(p) for p in parts[1:])

    try:
        rows = sorted(rows, key=order)
    except ValueError:
        return SequenceTimingPlan(
            (),
            say(
                en="The word order is unavailable. Which sequence should I use?",
                tr="Kelime sırası bulunamadı. Hangi diziyi kullanayım?",
            ),
            intent,
        )
    ids = tuple(r["id"] for r in rows)
    operations.append(
        {
            "op": "retime_text_sequence",
            "selector": {"ids": list(ids)},
            "target_ids": list(ids),
            "expected_count": len(rows),
            "start_s": 0.0,
            "end_s": end,
            "fit_to_video": True,
            "gap_policy": intent.gap_policy,
        }
    )
    from app.agents.edit_copilot import EditorOperationParseState, parse_editor_operation

    parsed = [
        parse_editor_operation(op, snapshot, EditorOperationParseState(1.0)) for op in operations
    ]
    if any(op is None for op in parsed):
        return SequenceTimingPlan(
            (),
            say(
                en="That sequence cannot be safely edited in this version.",
                tr="Bu sürümde bu dizi güvenle düzenlenemiyor.",
            ),
            intent,
        )
    reply = say(
        en=(
            (
                f"Retime all {len(rows)} words in sequence over {end:g}s, "
                "keeping their wording and appearance."
            )
            if intent.target_s is not None
            else (
                f"Spread all {len(rows)} words in sequence across the video, "
                "keeping their wording and appearance."
            )
        ),
        tr=(
            f"{len(rows)} kelimenin tamamını metin ve görünümlerini koruyarak "
            "video boyunca sırayla yay."
        ),
    )
    return SequenceTimingPlan(tuple(parsed), reply, intent, source, ids)


def timing_only_operations(operations: list[dict]) -> bool:
    return bool(operations) and all(
        op.get("op") in {"set_total_duration", "retime_text_sequence"}
        or (
            op.get("op") == "patch_slots"
            and bool(op.get("patch"))
            and set(op["patch"]) <= {"duration_s"}
        )
        for op in operations
    )


def sequence_timing_plan_from_operations(
    operations: list[dict], snapshot: dict, *, preserve_text: bool = False
) -> SequenceTimingPlan | None:
    durations = [op for op in operations if op.get("op") == "set_total_duration"]
    preserve_text = preserve_text or any(op.get("preserve_text") is True for op in operations)
    retimes = [
        op for op in operations if not preserve_text and op.get("op") == "retime_text_sequence"
    ]
    target = durations[-1]["target_s"] if durations else None
    if len(retimes) == 1:
        retime = retimes[0]
        intent = SequenceTimingIntent("fill", target, retime.get("gap_policy", "scale"))
        selected = set(retime.get("target_ids") or [])
        sources = {
            row.get("sequence_source_id")
            for row in snapshot.get("text_bars", [])
            if row.get("id") in selected
        }
        if len(sources) != 1 or None in sources:
            return None
        source = next(iter(sources))
        candidate = sequence_timing_plan(
            intent,
            {
                **snapshot,
                "text_bars": [
                    row
                    for row in snapshot.get("text_bars", [])
                    if row.get("sequence_source_id") == source
                ],
            },
        )
        if candidate is None or set(candidate.target_ids) != set(retime.get("target_ids") or []):
            return None
        return SequenceTimingPlan(
            tuple(operations),
            candidate.reply,
            intent,
            candidate.source_id,
            candidate.target_ids,
            None
            if retime.get("fit_to_video") is True
            else (float(retime["start_s"]), float(retime["end_s"])),
        )
    if not retimes and any(
        op.get("op") == "set_total_duration"
        or (op.get("op") == "patch_slots" and "duration_s" in (op.get("patch") or {}))
        for op in operations
    ):
        return SequenceTimingPlan(tuple(operations), "", SequenceTimingIntent("preserve", target))
    return None


def merge_sequence_brief_updates(prior, planned, plan, snapshot, message):
    from app.kria.brief import SUPERSEDED_BY_FACT, BriefUpdate, apply_updates

    projected = apply_updates(prior, planned, source_turn_id=None)
    reconciled = apply_updates(
        projected,
        sequence_brief_updates(projected, plan, snapshot, message),
        source_turn_id=None,
    )
    old = {req.id: req for req in (prior.live() if prior else ())}
    current = {req.id: req for req in reconciled.live()}
    version = prior.version if prior else 0
    updates = []
    fields = ("kind", "scope", "literal", "description", "facts")
    reconciled_by_id = {req.id: req for req in reconciled.requirements}
    for before in prior.requirements if prior else ():
        # A fresh start re-marks an already-superseded requirement as the creator's removal;
        # that is not a live change, so carry it as the remove that makes it.
        after = reconciled_by_id.get(before.id)
        if (
            not before.live
            and after is not None
            and after.facts.get(SUPERSEDED_BY_FACT) != before.facts.get(SUPERSEDED_BY_FACT)
        ):
            updates.append(
                BriefUpdate(
                    operation="remove", target_requirement_id=before.id, expected_version=version
                )
            )
    for identifier, before in old.items():
        after = current.get(identifier)
        if after is None:
            updates.append(
                BriefUpdate(
                    operation="remove", target_requirement_id=identifier, expected_version=version
                )
            )
        elif any(getattr(before, key) != getattr(after, key) for key in fields):
            updates.append(
                BriefUpdate(
                    operation="change",
                    target_requirement_id=identifier,
                    expected_version=version,
                    **{key: getattr(after, key) for key in fields},
                )
            )
    for identifier, after in current.items():
        if identifier not in old:
            updates.append(BriefUpdate(**{key: getattr(after, key) for key in fields}))
    return tuple(updates)


def sequence_brief_updates(prior, plan: SequenceTimingPlan, snapshot: dict, message: str):
    from app.kria.brief import BriefUpdate
    from app.kria.freeze_ask import times_one_moment
    from app.kria.style_asks import clip_length_ask

    if plan.intent is None or not plan.operations:
        return ()
    updates = []
    live = prior.live() if prior else ()
    version = prior.version if prior else 0

    def replace_matching(matches, description, facts):
        if matches:
            keep = matches[-1]
            updates.append(
                BriefUpdate(
                    operation="change",
                    target_requirement_id=keep.id,
                    expected_version=version,
                    description=description,
                    facts={**keep.facts, **facts},
                )
            )
            updates.extend(
                BriefUpdate(
                    operation="remove",
                    target_requirement_id=req.id,
                    expected_version=version,
                )
                for req in matches[:-1]
            )
        else:
            updates.append(
                BriefUpdate(
                    kind="timing",
                    scope="global",
                    description=description,
                    facts=facts,
                )
            )

    if plan.intent.target_s is not None:
        matches = [
            req
            for req in live
            if req.kind == "timing"
            and req.scope == "global"
            and isinstance(req.facts.get("duration_s"), (int, float))
            and not isinstance(req.facts.get("duration_s"), bool)
            and not req.facts.get("sequence_timing")
            and clip_length_ask(req) is None
            and not times_one_moment(req)
        ]
        replace_matching(
            matches,
            say(
                en=f"Video length {plan.intent.target_s:g}s",
                tr=f"Video süresi {plan.intent.target_s:g} saniye",
            ),
            {
                "duration_s": plan.intent.target_s,
                "editor_video_duration": True,
            },
        )
    rows = {r["id"]: r for r in snapshot.get("text_bars", [])}
    if plan.intent.mode == "fill":
        policy = {
            "source_id": plan.source_id,
            "target_ids": list(plan.target_ids),
            "mode": "range" if plan.span_s is not None else "fill",
            "gap_policy": plan.intent.gap_policy,
        }
        if plan.span_s is not None:
            policy["start_s"], policy["end_s"] = plan.span_s
        if plan.intent.gap_policy == "fixed":
            policy["gaps_s"] = [
                max(0, float(rows[right]["start_s"]) - float(rows[left]["end_s"]))
                for left, right in zip(plan.target_ids, plan.target_ids[1:])
            ]
        matches = [
            req
            for req in live
            if (
                isinstance(req.facts.get("sequence_timing"), dict)
                and req.facts["sequence_timing"].get("source_id") == plan.source_id
            )
            or (
                req.kind in {"timing", "text_sequence"}
                and req.scope == "global"
                and req.facts.get("editor_timing_only") is True
                and not isinstance(req.facts.get("duration_s"), (int, float))
            )
        ]
        matches.sort(key=lambda req: isinstance(req.facts.get("sequence_timing"), dict))
        replace_matching(matches, message, {"sequence_timing": policy})
    else:
        sources = {
            req.facts["sequence_timing"].get("source_id")
            for req in live
            if isinstance(req.facts.get("sequence_timing"), dict)
        }
        for source in sources:
            matches = [
                req
                for req in live
                if isinstance(req.facts.get("sequence_timing"), dict)
                and req.facts["sequence_timing"].get("source_id") == source
            ]
            ids = matches[-1].facts["sequence_timing"].get("target_ids") or []
            if not ids or any(value not in rows for value in ids):
                continue
            policy = {
                "source_id": source,
                "target_ids": ids,
                "mode": "absolute",
                "timing_digest": sequence_timing_digest(
                    (value, float(rows[value]["start_s"]), float(rows[value]["end_s"]))
                    for value in ids
                ),
            }
            replace_matching(
                matches,
                say(
                    en="Preserve the current word timings", tr="Mevcut kelime zamanlamalarını koru"
                ),
                {"sequence_timing": policy},
            )
    return tuple(updates)


def sequence_timing_digest(spans) -> str:
    import hashlib
    import json

    values = [
        (identifier, round(float(start), 6), round(float(end), 6))
        for identifier, start, end in spans
    ]
    return hashlib.sha256(json.dumps(values, separators=(",", ":")).encode()).hexdigest()
