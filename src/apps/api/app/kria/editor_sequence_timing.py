"""Complete, deterministic requests over the current video's text timing."""

from __future__ import annotations

import math
import re
from dataclasses import dataclass
from typing import Any, Literal

from app.kria.brief_route import loose_text
from app.kria.creator_cues import preserves_editor_text
from app.kria.reply_language import say

GapPolicy = Literal["scale", "close", "fixed"]
_NUMBER = r"([0-9]+(?:[.,][0-9]+)?)\s*(?:s|seconds?)"
_VIDEO_DURATION = (
    r"(?:make|shorten|extend|resize|keep) (?:the )?video (?:(?:to|at) )?"
    r"([0-9]+(?:[.,][0-9]+)?)(?:\s*(?:s|seconds?))?"
)
_WHOLE_VIDEO = (
    r"(?:please )?(?:fill|spread|distribute|redistribute|stretch|fit|shrink|compress) "
    r"(?:the )?(?:text|texts|words|word sequence) (?:across |over |throughout |to |for )?"
    r"(?:the )?(?:whole|entire|full) video"
)
_FIT_LENGTH = (
    r"(?:please )?(?:fit|stretch|shrink|compress) (?:the )?(?:text|words|word sequence) "
    r"(?:to |across )?(?:the )?video(?:s)? (?:length|duration)"
)


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


def sequence_timing_intent(message: str) -> SequenceTimingIntent | None:
    if len(message) > 600:
        return None
    text = loose_text(message.replace("'", "").replace("’", "")).strip(" .?!")
    text = re.sub(r"(?<![0-9])[,;.!?]|[,;.!?](?![0-9])", " ", text)
    text = " ".join(text.split())
    preserved = preserves_editor_text(message)
    if preserved:
        text = re.sub(
            r"(?:dont|do not|never) (?:touch|change|alter|modify|retime|edit|move) "
            r"(?:the )?(?:text|texts|words)[,; ]*(?:and )?",
            "",
            text,
        )
        text = re.sub(
            r"(?:yazilara|yazilari|metne|metni|kelimelere|kelimeleri) "
            r"(?:dokunma|dokunmayin|degistirme|degistirmeyin|degistirmeden|dokunmadan) "
            r"(?:ve )?",
            "",
            text,
        )
    tr_duration = re.fullmatch(
        r"videoyu ([0-9]+(?:[.,][0-9]+)?) saniye(?:ye)? (?:kisalt|uzat|yap|getir|tut)", text
    )
    if tr_duration:
        return SequenceTimingIntent("preserve", float(tr_duration[1].replace(",", ".")))
    duration = re.fullmatch(_VIDEO_DURATION, text)
    if duration:
        return SequenceTimingIntent("preserve", float(duration[1].replace(",", ".")))
    clip_duration = re.fullmatch(rf"shorten all clips to make (?:the )?video {_NUMBER}", text)
    if clip_duration:
        return SequenceTimingIntent("preserve", float(clip_duration[1].replace(",", ".")))
    if preserved:
        return None
    gap_policy: GapPolicy = "scale"
    seen_gap_policy = None
    while True:
        match = re.search(
            r"(?: and)? (?:without (?:overlap|overlapping)|(?:keep|keeping) (?:the )?"
            r"(?:sequential format|sequence|word order)(?: and)?)$",
            text,
        )
        if match:
            text = text[: match.start()]
            continue
        matched_gap = False
        for suffix, policy in (
            (r"(?: and)? (?:with no|without) gaps", "close"),
            (r"(?: and)? keep (?:the )?gaps unchanged", "fixed"),
            (r"(?: and)? keep (?:the )?gaps", "scale"),
        ):
            match = re.search(suffix + r"$", text)
            if match:
                if seen_gap_policy is not None and seen_gap_policy != policy:
                    return None
                gap_policy = policy
                seen_gap_policy = policy
                text = text[: match.start()]
                matched_gap = True
                break
        if not matched_gap:
            break
    target = None
    combined = re.fullmatch(rf"{_VIDEO_DURATION} (?:and|then) (.+)", text)
    if combined:
        target, text = float(combined[1].replace(",", ".")), combined[2]
    else:
        tail_duration = _VIDEO_DURATION.replace("(?:the )?video", "(?:the )?(?:video|it)")
        combined = re.fullmatch(rf"(.+) (?:and|then) {tail_duration}", text)
        if combined:
            text, target = combined[1], float(combined[2].replace(",", "."))
    requested = re.fullmatch(
        rf"(?:please )?(?:retime|spread) (?:the )?(?:text|words|word sequence) "
        rf"(?:to|over|across) {_NUMBER}",
        text,
    )
    tr_requested = re.fullmatch(
        r"(?:yazilari|kelimeleri|metni) ([0-9]+(?:[.,][0-9]+)?) "
        r"saniye(?:ye|lik)? (?:yay|dagit|yeniden zamanla)",
        text,
    )
    matched = requested or tr_requested
    if matched:
        if target is not None:
            return None
        target = float(matched[1].replace(",", "."))
    whole = any(
        re.fullmatch(pattern, text)
        for pattern in (
            _WHOLE_VIDEO,
            _FIT_LENGTH,
            r"(?:make|have) (?:the )?(?:text|words) (?:fill|fit|span|cover) "
            r"(?:the )?(?:whole|entire|full) video",
            r"(?:yazilari|kelimeleri|metni) videonun (?:tamamina|suresine|uzunluguna) "
            r"(?:yay|sigdir|uyarla)",
            r"(?:the )?texts? (?:are|is) overlapping[. ]+they shouldnt overlap "
            r"and the last one should end at the end of the video",
        )
    )
    return SequenceTimingIntent("fill", target, gap_policy) if matched or whole else None


def sequence_timing_plan(message: str, snapshot: dict) -> SequenceTimingPlan | None:
    intent = sequence_timing_intent(message)
    if intent is None or snapshot.get("editor_ops_version") != 2:
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
            f"Video length {plan.intent.target_s:g}s",
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
            "mode": "fill",
            "gap_policy": plan.intent.gap_policy,
        }
        if plan.intent.gap_policy == "fixed":
            policy["gaps_s"] = [
                max(0, float(rows[right]["start_s"]) - float(rows[left]["end_s"]))
                for left, right in zip(plan.target_ids, plan.target_ids[1:])
            ]
        matches = [
            req
            for req in live
            if isinstance(req.facts.get("sequence_timing"), dict)
            and req.facts["sequence_timing"].get("source_id") == plan.source_id
        ]
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
                matches, "Preserve the current word timings", {"sequence_timing": policy}
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
