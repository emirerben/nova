"""Editor ops v2, text lane (KRI-219 Lane A).

Selector ops that change many texts in one turn:

    rewrite_text{selector, text | replace:{find,with}}
    patch_text{selector, patch}
    remove_texts{selector}
    set_texts_timing{selector, start_s?, end_s?, shift_s?}
    realign_labels{selector?}   (server-computed windows; the model supplies no time)

plus an extension of the existing ``add_text`` (style_from / patch / position /
animation_phases / clip_id). The parser half lives here; the compile half and the
selector resolver live in ``app.services.kria_editor_ops_text`` so both halves
share one resolver.

Do not import app.agents.edit_copilot / app.services.kria_editor_ops at module
top level (circular); import lazily inside functions.
"""

from __future__ import annotations

from typing import Any

from app.agents.editor_ops_v2 import OpSpec, is_v2_snapshot

_FAMILY = frozenset({"text", "text_timeline"})
ADD_TEXT_EXTRAS = ("style_from", "patch", "position", "animation_phases", "clip_id")


def _clarify(state: Any, message: str) -> None:
    """Ask the creator instead of silently doing nothing (read by EditCopilot.parse)."""
    state.selector_clarification = message


def coerce_text_patch(patch: object, state: Any) -> dict[str, Any] | None:
    """Validate a text patch; unknown fields are rejected, never silently dropped."""
    from app.agents.edit_copilot import (  # noqa: PLC0415
        _STYLE_PATCH_FIELDS,
        _as_float,
        _coerce_patch,
    )
    from app.services.kria_editor_ops_text import EXTRA_PATCH_FIELDS, PHASE_KEYS  # noqa: PLC0415

    if not isinstance(patch, dict) or not patch:
        state.invalid_value()
        return None
    allowed = _STYLE_PATCH_FIELDS | EXTRA_PATCH_FIELDS | {"size_scale"}
    if set(patch) - allowed:
        state.invalid_value()
        return None
    clean: dict[str, Any] = {}
    style = {key: value for key, value in patch.items() if key in _STYLE_PATCH_FIELDS}
    if style:
        clean = _coerce_patch(style, state)
        if not clean:
            state.invalid_value()
            return None
    if "size_scale" in patch:
        scale = _as_float(patch["size_scale"])
        if scale is None or "size_px" in clean or not 0.25 <= scale <= 4.0:
            state.invalid_value()
            return None
        clean["size_scale"] = scale
    if "background_color" in patch:
        value = patch["background_color"]
        if value is not None:
            try:
                valid = isinstance(value, str) and len(value) == 7 and value[0] == "#"
                valid = valid and int(value[1:], 16) >= 0
            except ValueError:
                valid = False
            if not valid:
                state.invalid_value()
                return None
        clean["background_color"] = value
    if "behind_subject" in patch:
        if not isinstance(patch["behind_subject"], bool):
            state.invalid_value()
            return None
        clean["behind_subject"] = patch["behind_subject"]
    if "animation_phases" in patch:
        phases = patch["animation_phases"]
        if not isinstance(phases, dict) or not phases or set(phases) - PHASE_KEYS:
            state.invalid_value()
            return None
        from app.agents._schemas.text_animation_phases import (  # noqa: PLC0415
            TextAnimationPhases,
        )

        try:
            TextAnimationPhases(**phases)
        except Exception:  # noqa: BLE001
            state.invalid_value()
            return None
        clean["animation_phases"] = dict(phases)
    return clean or None


def _selected(
    payload: dict, snapshot: dict, state: Any, *, find: str | None = None
) -> tuple[dict[str, Any], list[str]] | None:
    from app.services.kria_editor_ops_text import (  # noqa: PLC0415
        bars_from_snapshot,
        contains_find,
        normalize_selector,
        resolve_selector,
        zero_match_message,
    )

    selector = normalize_selector(payload.get("selector"), snapshot)
    if selector is None:
        state.invalid_value()
        return None
    bars = bars_from_snapshot(snapshot)
    matched = resolve_selector(bars, selector)
    if find is not None:
        by_id = {bar["id"]: bar for bar in bars}
        matched = [bar_id for bar_id in matched if contains_find(by_id[bar_id]["text"], find)]
    if not matched:
        _clarify(state, zero_match_message(selector, find=find))
        return None
    if selector.get("group") == "labels" and not any(
        bar["clip_id"] or str(bar["id"]).startswith("clip-label-")
        for bar in bars
        if bar["id"] in set(matched)
    ):
        state.reply_notes.append(
            "I applied that to the captions you added in chat (free texts, not clip labels)."
        )
    return selector, matched


def _coerce_rewrite_text(name: str, payload: dict, snapshot: dict, state: Any) -> dict | None:
    from app.services.kria_editor_ops_text import (  # noqa: PLC0415
        MAX_TEXT_CHARS,
        apply_replace,
        bars_from_snapshot,
        clean_text,
    )

    text, replace = payload.get("text"), payload.get("replace")
    if (text is None) == (replace is None):
        state.invalid_value()
        return None
    find: str | None = None
    out: dict[str, Any] = {}
    if replace is not None:
        if (
            not isinstance(replace, dict)
            or set(replace) - {"find", "with"}
            or not isinstance(replace.get("find"), str)
            or not isinstance(replace.get("with"), str)
        ):
            state.invalid_value()
            return None
        find = clean_text(replace["find"])
        if not find or len(find) > 200 or len(replace["with"]) > MAX_TEXT_CHARS:
            state.invalid_value()
            return None
        out["replace"] = {"find": find, "with": clean_text(replace["with"])}
    else:
        cleaned = clean_text(text) if isinstance(text, str) else ""
        if not cleaned or len(cleaned) > MAX_TEXT_CHARS:
            state.invalid_value()
            return None
        out["text"] = cleaned
    picked = _selected(payload, snapshot, state, find=find)
    if picked is None:
        return None
    selector, matched = picked
    by_id = {bar["id"]: bar for bar in bars_from_snapshot(snapshot)}
    results: list[str] = []
    for bar_id in matched:
        if find is not None:
            new, _ = apply_replace(by_id[bar_id]["text"], find, out["replace"]["with"])
        else:
            new = out["text"]
        if not new or len(new) > MAX_TEXT_CHARS:
            state.invalid_value()
            return None
        results.append(new)
    if all(new == by_id[bar_id]["text"] for bar_id, new in zip(matched, results, strict=True)):
        _clarify(state, f"Those already read “{results[0]}”, so I left them as they are.")
        return None
    return {**out, "selector": selector, "target_ids": matched, "expected_count": len(matched)}


def _coerce_patch_text(name: str, payload: dict, snapshot: dict, state: Any) -> dict | None:
    patch = coerce_text_patch(payload.get("patch"), state)
    if patch is None:
        return None
    picked = _selected(payload, snapshot, state)
    if picked is None:
        return None
    selector, matched = picked
    return {
        "selector": selector,
        "patch": patch,
        "target_ids": matched,
        "expected_count": len(matched),
    }


def _coerce_remove_texts(name: str, payload: dict, snapshot: dict, state: Any) -> dict | None:
    picked = _selected(payload, snapshot, state)
    if picked is None:
        return None
    selector, matched = picked
    return {"selector": selector, "target_ids": matched, "expected_count": len(matched)}


def _coerce_set_texts_timing(name: str, payload: dict, snapshot: dict, state: Any) -> dict | None:
    from app.agents.edit_copilot import _as_float  # noqa: PLC0415

    out: dict[str, Any] = {}
    for key in ("start_s", "end_s", "shift_s"):
        if key in payload:
            number = _as_float(payload[key])
            if number is None or (key != "shift_s" and number < 0) or abs(number) > 3600:
                state.invalid_value()
                return None
            out[key] = number
    absolute = {"start_s", "end_s"} & out.keys()
    if ("shift_s" in out) == bool(absolute):
        state.invalid_value()
        return None
    if {"start_s", "end_s"} <= out.keys() and out["end_s"] <= out["start_s"]:
        state.invalid_value()
        return None
    picked = _selected(payload, snapshot, state)
    if picked is None:
        return None
    selector, matched = picked
    return {**out, "selector": selector, "target_ids": matched, "expected_count": len(matched)}


def _coerce_realign_labels(name: str, payload: dict, snapshot: dict, state: Any) -> dict | None:
    """``realign_labels``: the SERVER computes every time; the model supplies none.

    Optional ``selector`` limits which labels (default: all clip labels). The op
    carries only the labels that are actually off, so "nothing to do" is an
    honest clarification-style answer instead of a silent no-op.
    """
    from app.services.kria_editor_ops_text import (  # noqa: PLC0415
        bars_from_snapshot,
        classify,
        normalize_selector,
        plan_label_realign,
        resolve_selector,
    )

    raw = payload.get("selector")
    selector = {"group": "labels"} if raw is None else normalize_selector(raw, snapshot)
    if selector is None:
        state.invalid_value()
        return None
    bars = bars_from_snapshot(snapshot)
    kinds = classify(bars)
    ids = [bar_id for bar_id in resolve_selector(bars, selector) if kinds.get(bar_id) == "label"]
    if not ids:
        _clarify(state, "There are no clip labels on this video to realign.")
        return None
    rows = {
        str(row["id"]): row
        for row in snapshot.get("text_bars") or []
        if isinstance(row, dict) and isinstance(row.get("id"), str)
    }
    labels = [
        {
            "id": bar_id,
            "media_id": rows[bar_id].get("clip_id"),
            "segment_id": rows[bar_id].get("segment_id"),
            "start_s": rows[bar_id].get("start_s"),
            "end_s": rows[bar_id].get("end_s"),
        }
        for bar_id in ids
        if bar_id in rows and rows[bar_id].get("clip_id")
    ]
    slots = [
        slot
        for slot in snapshot.get("slots") or []
        if isinstance(slot, dict) and not slot.get("removed")
    ]
    plan = plan_label_realign(labels, slots)
    if not plan:
        _clarify(
            state,
            "The labels already line up with their clips, so I left them as they are.",
        )
        return None
    return {
        "selector": selector,
        "target_ids": [item["id"] for item in plan],
        "expected_count": len(plan),
    }


def coerce_add_text_extras(out: dict, snapshot: dict, state: Any) -> dict | None:
    """Validate the v2 fields of ``add_text``; on a non-v2 snapshot they are ignored."""
    result = {key: value for key, value in out.items() if key not in ADD_TEXT_EXTRAS}
    if not is_v2_snapshot(snapshot):
        return result
    from app.agents.edit_copilot import _resolve_placement  # noqa: PLC0415
    from app.services.kria_editor_ops_text import bars_from_snapshot  # noqa: PLC0415

    bars = bars_from_snapshot(snapshot)
    style_from = out.get("style_from")
    if style_from is not None:
        if (
            not isinstance(style_from, str)
            or not style_from
            or (style_from not in {"title", "labels"} and style_from not in {b["id"] for b in bars})
        ):
            state.invalid_value()
            return None
        result["style_from"] = style_from
    if out.get("patch") is not None and not isinstance(out["patch"], dict):
        state.invalid_value()
        return None
    raw_patch: dict[str, Any] = dict(out["patch"]) if isinstance(out.get("patch"), dict) else {}
    if "position" in out:
        if not isinstance(out["position"], str):
            state.invalid_value()
            return None
        raw_patch["position"] = out["position"]
    if "animation_phases" in out:
        raw_patch["animation_phases"] = out["animation_phases"]
    if raw_patch:
        patch = coerce_text_patch(_resolve_placement(raw_patch), state)
        if patch is None:
            return None
        result["patch"] = patch
    clip_id = out.get("clip_id")
    if clip_id is not None:
        slots = snapshot.get("slots") if isinstance(snapshot.get("slots"), list) else []
        media = {s.get("media_id") for s in slots if isinstance(s, dict)}
        if not isinstance(clip_id, str) or clip_id not in media:
            state.invalid_value()
            return None
        if any(bar["clip_id"] == clip_id and not bar["removed"] for bar in bars):
            _clarify(
                state,
                "That clip already has a label. Should I change its wording instead of adding one?",
            )
            return None
        result["clip_id"] = clip_id
        result.setdefault("style_from", "labels")
    return result


SPECS: list[OpSpec] = [
    OpSpec(
        name="rewrite_text",
        required=frozenset({"selector"}),
        fields=frozenset({"text", "replace"}),
        family=_FAMILY,
        coerce=_coerce_rewrite_text,
    ),
    OpSpec(
        name="patch_text",
        required=frozenset({"selector", "patch"}),
        family=_FAMILY,
        coerce=_coerce_patch_text,
    ),
    OpSpec(
        name="remove_texts",
        required=frozenset({"selector"}),
        family=_FAMILY,
        coerce=_coerce_remove_texts,
    ),
    OpSpec(
        name="set_texts_timing",
        required=frozenset({"selector"}),
        fields=frozenset({"start_s", "end_s", "shift_s"}),
        family=_FAMILY,
        coerce=_coerce_set_texts_timing,
    ),
    OpSpec(
        name="realign_labels",
        fields=frozenset({"selector"}),
        family=_FAMILY,
        coerce=_coerce_realign_labels,
    ),
    # Extension of the existing op (no coerce): the extras are validated by
    # `coerce_add_text_extras`, called from edit_copilot._coerce_payload.
    OpSpec(name="add_text", fields=frozenset(ADD_TEXT_EXTRAS)),
]


def register_handlers() -> None:
    from app.services import kria_editor_ops as ops  # noqa: PLC0415
    from app.services import kria_editor_ops_text as text  # noqa: PLC0415

    ops.register_handler("rewrite_text", text.op_rewrite_text)
    ops.register_handler("patch_text", text.op_patch_text)
    ops.register_handler("remove_texts", text.op_remove_texts)
    ops.register_handler("set_texts_timing", text.op_set_texts_timing)
    ops.register_handler("realign_labels", text.op_realign_labels)
