"""Property-by-property diff engine + severity classifier + summary writer."""
from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

from .events import TextEvent

Severity = Literal["critical", "major", "minor"]


# Severity thresholds — tunable named constants per the plan.
POSITION_MAJOR_FRAC = 0.15
POSITION_MINOR_FRAC = 0.05
SIZE_MAJOR_RATIO = 0.4   # >40% relative-size delta = major
SIZE_MINOR_RATIO = 0.10
COLOR_MAJOR_DELTA_E = 30.0
COLOR_MINOR_DELTA_E = 10.0
DURATION_MAJOR_S = 0.6
DURATION_MINOR_S = 0.3
ENTRANCE_DURATION_MINOR_S = 0.15
BEAT_ALIGN_TOLERANCE_S = 0.15
BEAT_ALIGN_CRITICAL_OFFSET_S = 0.3


@dataclass
class DiffFinding:
    severity: Severity
    property: str
    recipe: object  # JSON-serializable
    output: object
    reason: str

    def to_dict(self) -> dict:
        return {
            "severity": self.severity,
            "property": self.property,
            "recipe": self.recipe,
            "output": self.output,
            "reason": self.reason,
        }


def levenshtein(a: str, b: str) -> int:
    if a == b:
        return 0
    if not a:
        return len(b)
    if not b:
        return len(a)
    a = a.lower()
    b = b.lower()
    prev = list(range(len(b) + 1))
    for i, ca in enumerate(a, 1):
        curr = [i]
        for j, cb in enumerate(b, 1):
            curr.append(min(
                prev[j] + 1,
                curr[j - 1] + 1,
                prev[j - 1] + (0 if ca == cb else 1),
            ))
        prev = curr
    return prev[-1]


@dataclass
class PairedEvent:
    recipe: TextEvent | None
    output: TextEvent | None
    pair_key: str  # human-readable label for the diff entries


def pair_events(
    recipe_events: list[TextEvent],
    output_events: list[TextEvent],
) -> list[PairedEvent]:
    """Greedy pairing: each recipe event takes its best output match (by
    color + text similarity + temporal closeness). Unpaired events
    surface as missing or extra.

    Pairing key for diff property paths uses the recipe event's text (or
    color+order if text is missing), so a recipe "This" pairs with an
    output event whose text is also "This" if one exists.
    """
    paired: list[PairedEvent] = []
    used_output_indices: set[int] = set()

    for r_ev in recipe_events:
        best_idx = None
        best_score = -1.0
        for i, o_ev in enumerate(output_events):
            if i in used_output_indices:
                continue
            score = _pair_score(r_ev, o_ev)
            if score > best_score:
                best_score = score
                best_idx = i
        # Require minimum score to count as a match; otherwise leave unpaired.
        if best_idx is not None and best_score > 0.0:
            used_output_indices.add(best_idx)
            paired.append(PairedEvent(
                recipe=r_ev, output=output_events[best_idx],
                pair_key=_event_label(r_ev),
            ))
        else:
            paired.append(PairedEvent(recipe=r_ev, output=None, pair_key=_event_label(r_ev)))

    for i, o_ev in enumerate(output_events):
        if i not in used_output_indices:
            paired.append(PairedEvent(recipe=None, output=o_ev, pair_key=_event_label(o_ev)))

    return paired


def _event_label(ev: TextEvent) -> str:
    return (ev.text or f"<{ev.color_key}>").strip()


def _pair_score(a: TextEvent, b: TextEvent) -> float:
    """0..3 score: 1pt for same color, up to 1pt for text similarity, 1pt for
    temporal overlap."""
    s = 0.0
    if a.color_key == b.color_key:
        s += 1.0
    if a.text and b.text:
        dist = levenshtein(a.text, b.text)
        max_len = max(len(a.text), len(b.text), 1)
        s += max(0.0, 1.0 - dist / max_len)
    # Temporal overlap: both events must overlap somewhere in their windows.
    if not (a.t_end < b.t_start or b.t_end < a.t_start):
        s += 1.0
    return s


def diff_paired_event(
    pair: PairedEvent,
    recipe_frame_w: int,
    recipe_frame_h: int,
    output_frame_w: int,
    output_frame_h: int,
) -> list[DiffFinding]:
    out: list[DiffFinding] = []
    label = pair.pair_key

    if pair.recipe is None and pair.output is not None:
        out.append(DiffFinding(
            severity="critical",
            property=f"event[{label}].existence",
            recipe=None,
            output=_event_summary(pair.output, output_frame_w, output_frame_h),
            reason=(
                f"Output has an unexpected text event '{label}' (color={pair.output.color_key}, "
                f"t={pair.output.t_start:.2f}-{pair.output.t_end:.2f}s) with no recipe counterpart"
            ),
        ))
        return out

    if pair.output is None and pair.recipe is not None:
        out.append(DiffFinding(
            severity="critical",
            property=f"event[{label}].existence",
            recipe=_event_summary(pair.recipe, recipe_frame_w, recipe_frame_h),
            output=None,
            reason=(
                f"Recipe text event '{label}' is missing from output entirely "
                f"(recipe color={pair.recipe.color_key}, t={pair.recipe.t_start:.2f}-"
                f"{pair.recipe.t_end:.2f}s)"
            ),
        ))
        return out

    assert pair.recipe is not None and pair.output is not None
    r = pair.recipe
    o = pair.output

    # Text identity
    if r.text and o.text:
        dist = levenshtein(r.text, o.text)
        if dist > 1:
            out.append(DiffFinding(
                severity="critical",
                property=f"event[{label}].text",
                recipe=r.text,
                output=o.text,
                reason=f"Text identity mismatch: recipe={r.text!r} vs output={o.text!r}",
            ))

    # Position diff (relative coords)
    r_rel = r.median_relative_bbox(recipe_frame_w, recipe_frame_h)
    o_rel = o.median_relative_bbox(output_frame_w, output_frame_h)
    dx = abs(r_rel.cx_frac - o_rel.cx_frac)
    dy = abs(r_rel.cy_frac - o_rel.cy_frac)
    pos_delta = max(dx, dy)
    if pos_delta > POSITION_MAJOR_FRAC:
        sev: Severity = "major"
    elif pos_delta > POSITION_MINOR_FRAC:
        sev = "minor"
    else:
        sev = None  # type: ignore[assignment]
    if sev is not None:
        out.append(DiffFinding(
            severity=sev,
            property=f"event[{label}].position",
            recipe={"cx_frac": round(r_rel.cx_frac, 3), "cy_frac": round(r_rel.cy_frac, 3)},
            output={"cx_frac": round(o_rel.cx_frac, 3), "cy_frac": round(o_rel.cy_frac, 3)},
            reason=f"centroid delta dx={dx:.3f}, dy={dy:.3f} (max {pos_delta:.3f})",
        ))

    # Size diff (height fraction is the most reliable size signal)
    if r_rel.h_frac > 0 and o_rel.h_frac > 0:
        ratio_delta = abs(o_rel.h_frac - r_rel.h_frac) / r_rel.h_frac
        if ratio_delta > SIZE_MAJOR_RATIO:
            sev = "major"
        elif ratio_delta > SIZE_MINOR_RATIO:
            sev = "minor"
        else:
            sev = None  # type: ignore[assignment]
        if sev is not None:
            out.append(DiffFinding(
                severity=sev,
                property=f"event[{label}].size",
                recipe={"h_frac": round(r_rel.h_frac, 3)},
                output={"h_frac": round(o_rel.h_frac, 3)},
                reason=f"text size differs by {ratio_delta:.1%} (relative to recipe)",
            ))

    # Animation entrance kind
    if hasattr(r, "_entrance") and hasattr(o, "_entrance"):
        r_ent = r._entrance  # type: ignore[attr-defined]
        o_ent = o._entrance  # type: ignore[attr-defined]
        if r_ent[0] != o_ent[0]:
            out.append(DiffFinding(
                severity="major",
                property=f"event[{label}].animation.entrance",
                recipe=r_ent[0],
                output=o_ent[0],
                reason=f"different animation family: recipe={r_ent[0]} vs output={o_ent[0]}",
            ))
        elif abs(r_ent[1] - o_ent[1]) > ENTRANCE_DURATION_MINOR_S:
            out.append(DiffFinding(
                severity="minor",
                property=f"event[{label}].animation.entrance_duration_s",
                recipe=round(r_ent[1], 3),
                output=round(o_ent[1], 3),
                reason=(
                    f"same entrance family ({r_ent[0]}) but settle duration differs by "
                    f"{abs(r_ent[1] - o_ent[1]):.2f}s"
                ),
            ))

    # Visible duration
    dur_delta = abs(r.duration_s - o.duration_s)
    if dur_delta > DURATION_MAJOR_S:
        sev = "major"
    elif dur_delta > DURATION_MINOR_S:
        sev = "minor"
    else:
        sev = None  # type: ignore[assignment]
    if sev is not None:
        out.append(DiffFinding(
            severity=sev,
            property=f"event[{label}].duration_s",
            recipe=round(r.duration_s, 3),
            output=round(o.duration_s, 3),
            reason=f"on-screen duration differs by {dur_delta:.2f}s",
        ))

    return out


def diff_cooccurrence_rule(
    rule_name: str,
    recipe_satisfied: bool | None,
    output_satisfied: bool | None,
    description: str,
) -> DiffFinding | None:
    """A co-occurrence rule pair — same expected behavior in both, but the
    output failed. Critical when recipe satisfies the rule but output does
    not. None is treated as "could not determine" and not flagged.
    """
    if recipe_satisfied and not output_satisfied:
        return DiffFinding(
            severity="critical",
            property=f"cooccurrence.{rule_name}",
            recipe=True,
            output=output_satisfied,
            reason=description,
        )
    return None


def diff_beat_alignment(
    label: str,
    recipe_offset_s: float | None,
    output_offset_s: float | None,
) -> DiffFinding | None:
    """If the recipe event is beat-aligned but the output's offset to nearest
    beat is well past tolerance, flag it as critical."""
    if recipe_offset_s is None or output_offset_s is None:
        return None
    if (
        abs(recipe_offset_s) <= BEAT_ALIGN_TOLERANCE_S
        and abs(output_offset_s) > BEAT_ALIGN_CRITICAL_OFFSET_S
    ):
        return DiffFinding(
            severity="critical",
            property=f"event[{label}].beat_alignment",
            recipe=round(recipe_offset_s, 3),
            output=round(output_offset_s, 3),
            reason=(
                f"recipe '{label}' lands within {BEAT_ALIGN_TOLERANCE_S:.2f}s of a beat "
                f"(offset {recipe_offset_s:+.3f}s); output is {output_offset_s:+.3f}s off — "
                "audio-cued moment lost"
            ),
        )
    return None


def diff_safe_crop(label: str, projection_note: str, survives: bool) -> DiffFinding | None:
    if survives:
        return None
    return DiffFinding(
        severity="major",
        property=f"event[{label}].safe_crop_9x16",
        recipe={"survives": False, "note": projection_note},
        output=None,
        reason=(
            f"Recipe text '{label}' would not survive a 9:16 center-crop of the recipe — "
            f"{projection_note}. A naive vertical adaptation loses this element."
        ),
    )


def _event_summary(ev: TextEvent, frame_w: int, frame_h: int) -> dict:
    rel = ev.median_relative_bbox(frame_w, frame_h)
    return {
        "text": ev.text,
        "color_key": ev.color_key,
        "t_start": round(ev.t_start, 3),
        "t_end": round(ev.t_end, 3),
        "duration_s": round(ev.duration_s, 3),
        "position": {"cx_frac": round(rel.cx_frac, 3), "cy_frac": round(rel.cy_frac, 3)},
        "size": {"h_frac": round(rel.h_frac, 3)},
    }


_SEVERITY_ORDER = {"critical": 0, "major": 1, "minor": 2}


def sort_findings(findings: list[DiffFinding]) -> list[DiffFinding]:
    return sorted(findings, key=lambda f: (_SEVERITY_ORDER[f.severity], f.property))


def plain_english_summary(findings: list[DiffFinding], top_n: int = 5) -> list[str]:
    """One-line-per-finding plain English, severity-first, capped at top_n."""
    out: list[str] = []
    for f in sort_findings(findings)[:top_n]:
        out.append(f"[{f.severity.upper()}] {f.reason}")
    return out
