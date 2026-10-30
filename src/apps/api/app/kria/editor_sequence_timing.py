"""Deterministic timing requests over one existing, complete word sequence."""

from __future__ import annotations

import math
import re
from dataclasses import dataclass
from typing import Any

from app.kria.brief_route import loose_text
from app.kria.creator_cues import preserves_editor_text
from app.kria.reply_language import say


@dataclass(frozen=True)
class SequenceTimingPlan:
    operations: tuple[dict[str, Any], ...]
    reply: str


def sequence_timing_plan(message: str, snapshot: dict) -> SequenceTimingPlan | None:
    if preserves_editor_text(message) or snapshot.get("editor_ops_version") != 2:
        return None
    text = loose_text(message.replace("'", "").replace("’", "")).strip(" .?!")
    words = re.findall(r"[a-z0-9]+", text)
    if any(
        w
        in {
            "font",
            "color",
            "size",
            "position",
            "delete",
            "remove",
            "rewrite",
            "replace",
            "music",
            "audio",
            "volume",
        }
        for w in words
    ):
        return None
    has_text = any(
        w in {"text", "texts", "words", "sequence", "yazilari", "kelimeleri", "metni"}
        for w in words
    )
    if not has_text:
        return None
    requested = re.fullmatch(
        r"(?:please )?(?:retime|spread) (?:the )?(?:text|words|word sequence) "
        r"(?:to|over|across) ([0-9]+(?:[.,][0-9]+)?)\s*(?:s|seconds?)",
        text,
    )
    tr_requested = re.fullmatch(
        r"(?:yazilari|kelimeleri|metni) ([0-9]+(?:[.,][0-9]+)?) "
        r"saniye(?:ye|lik)? (?:yay|dagit|yeniden zamanla)",
        text,
    )
    normalized = " ".join(words)
    full = bool(
        re.fullmatch(
            r"(?:please )?(?:spread|distribute|redistribute) (?:the )?(?:text|texts|words) "
            r"(?:across|over|throughout) (?:the )?(?:whole|entire|full) video",
            normalized,
        )
    )
    overlap_to_end = bool(
        re.fullmatch(
            r"(?:the )?texts? (?:are|is) overlapping they shouldnt overlap "
            r"and the last one should end at the end of the video",
            normalized,
        )
    )
    if not (requested or tr_requested or full or overlap_to_end):
        return None
    if not (requested or tr_requested) and any(
        w.isdigit()
        or w
        in {
            "shorten",
            "trim",
            "extend",
            "crop",
            "zoom",
            "rotate",
            "speed",
            "mute",
            "add",
            "move",
            "align",
        }
        for w in words
    ):
        return None
    if any(w in {"dont", "never", "not"} for w in words) and any(
        w in {"spread", "distribute", "retime"} for w in words
    ):
        return None
    matched = requested or tr_requested
    total_raw = snapshot.get("total_duration_s")
    total = (
        float(total_raw)
        if isinstance(total_raw, (int, float)) and not isinstance(total_raw, bool)
        else 0.0
    )
    end = float(matched.group(1).replace(",", ".")) if matched else total
    if not math.isfinite(end) or end <= 0 or end > float(snapshot.get("max_duration_s") or 120):
        return SequenceTimingPlan(
            (),
            say(
                en="Which supported video length should I use for the words?",
                tr="Yazılar için hangi desteklenen video uzunluğunu kullanayım?",
            ),
        )
    rows = [
        r
        for r in snapshot.get("text_bars", [])
        if not r.get("removed") and not r.get("caption_cue")
    ]
    groups = {r.get("sequence_source_id") for r in rows}
    if not rows or len(groups) != 1 or None in groups:
        return SequenceTimingPlan(
            (),
            say(
                en="Which complete word sequence should I retime?",
                tr="Hangi kelime dizisinin tamamını yeniden zamanlayayım?",
            ),
        )
    operations = []
    if matched and abs(total - end) > 1e-6:
        operations.append({"op": "set_total_duration", "target_s": end, "strategy": "proportional"})
    operations.append(
        {
            "op": "retime_text_sequence",
            "selector": {"ids": [r["id"] for r in rows]},
            "target_ids": [r["id"] for r in rows],
            "expected_count": len(rows),
            "start_s": 0.0,
            "end_s": end,
        }
    )
    return SequenceTimingPlan(
        tuple(operations),
        say(
            en=(
                f"Retime all {len(rows)} words in sequence over {end:g}s, "
                "keeping their wording and appearance."
            ),
            tr=(
                f"{len(rows)} kelimenin tamamını, metin ve görünümlerini koruyarak "
                f"{end:g} saniyeye sırayla yay."
            ),
        ),
    )
