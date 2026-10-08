"""Deterministic requirement checkers.

Every checker is a pure function `(req, plan, footage, previous) -> (status, reason)`.
No model, no network, no clock. A checker never guesses: when the information needed to
judge is missing (no capture facts to order by, no previous plan to compare with) it says
`unmet` and names why, because "the system could not have known" is still "the creator's
request was not followed".

Status thresholds are deliberately strict — `met` means the request was satisfied in full.
"""

from __future__ import annotations

import re
import unicodedata
from collections.abc import Callable
from datetime import datetime
from itertools import combinations
from statistics import fmean

from .models import (
    ClipRecord,
    FinalPlan,
    Footage,
    PlanText,
    Requirement,
    ScoreStatus,
)

CheckResult = tuple[ScoreStatus, str]
Checker = Callable[[Requirement, FinalPlan, Footage, FinalPlan | None], CheckResult]

# A caption must be on screen long enough to read (P4 reading-time rule).
LABEL_MIN_DISPLAY_BASE_S = 0.8
LABEL_MIN_DISPLAY_PER_CHAR_S = 0.06
LABEL_MIN_DISPLAY_FLOOR_S = 1.2
LABEL_MIN_DISPLAY_CEIL_S = 3.0
# A label "covers" a clip when it is on screen for at least this share of the clip.
LABEL_CLIP_OVERLAP_FRAC = 0.5


# ── Text helpers ─────────────────────────────────────────────────────────────


def nfc(text: str) -> str:
    return unicodedata.normalize("NFC", text).strip()


def fold(text: str) -> str:
    """Case- and diacritic-insensitive form. Turkish dotless i is folded explicitly
    because NFKD does not decompose it."""
    text = unicodedata.normalize("NFC", text).replace("ı", "i").replace("İ", "I")
    decomposed = unicodedata.normalize("NFKD", text)
    return "".join(ch for ch in decomposed if not unicodedata.combining(ch)).casefold().strip()


def _fraction_status(fraction: float, *, partial_from: float = 0.5) -> ScoreStatus:
    if fraction >= 1.0 - 1e-9:
        return "met"
    if fraction >= partial_from:
        return "partial"
    return "unmet"


def label_texts(plan: FinalPlan) -> list[PlanText]:
    return [t for t in plan.texts if t.role == "label" and t.text.strip()]


def _overlap(a_start: float, a_end: float, b_start: float, b_end: float) -> float:
    return max(0.0, min(a_end, b_end) - max(a_start, b_start))


def labels_covering(plan: FinalPlan) -> dict[str, list[PlanText]]:
    """clip_id -> the label texts that cover that clip (first occurrence of each clip)."""
    covering: dict[str, list[PlanText]] = {}
    for clip in plan.clips:
        if clip.clip_id in covering:
            continue
        hits = []
        for text in label_texts(plan):
            if text.clip_id is not None:
                bound = text.clip_id == clip.clip_id
            else:
                bound = (
                    _overlap(text.start_s, text.end_s, clip.start_s, clip.end_s)
                    >= LABEL_CLIP_OVERLAP_FRAC * clip.duration_s
                )
            if bound:
                hits.append(text)
        covering[clip.clip_id] = hits
    return covering


def _scope_texts(plan: FinalPlan, scope: str) -> list[str]:
    if scope == "title":
        title = plan.title()
        return [title.text] if title else []
    if scope == "labels":
        return [t.text for t in label_texts(plan)]
    return [t.text for t in plan.texts if t.text.strip()]


# ── Checkers ─────────────────────────────────────────────────────────────────


def title_exact(
    req: Requirement, plan: FinalPlan, _f: Footage, _p: FinalPlan | None
) -> CheckResult:
    literal = str(req.params["literal"])
    title = plan.title()
    if title is None:
        return "unmet", "no title in the edit"
    if nfc(title.text) == nfc(literal):
        return "met", "title matches exactly"
    if fold(title.text) == fold(literal):
        return "partial", f"title differs only in case/diacritics: {title.text!r}"
    return "unmet", f"title is {title.text!r}, wanted {literal!r}"


def title_persistent(
    req: Requirement, plan: FinalPlan, _f: Footage, _p: FinalPlan | None
) -> CheckResult:
    """Require one exact title bar to cover the complete output duration."""
    titles = [text for text in plan.texts if text.role == "title"]
    if not titles:
        return "unmet", "no title in the edit"
    literal = req.params.get("literal")
    title_ids = req.params.get("title_ids")
    if title_ids:
        wanted_ids = {str(value) for value in title_ids}
        titles = [title for title in titles if title.id in wanted_ids]
        if len(titles) != len(wanted_ids):
            return "unmet", "one or more expected title bars are missing"
    if plan.duration_s <= 0:
        return "unmet", "output duration is unverified"
    if not plan.timing_verified:
        return "unmet", "output duration is unverified: timeline contains unresolved timing"
    bad = []
    for title in titles:
        if literal is not None and nfc(title.text) != nfc(str(literal)):
            bad.append(f"{title.id}: text is {title.text!r}")
        elif title.start_s > 1e-9 or title.end_s + 1e-9 < plan.duration_s:
            bad.append(f"{title.id}: range {title.start_s:.2f}-{title.end_s:.2f}s")
    return (
        ("unmet", "; ".join(bad))
        if bad
        else ("met", "all title bars persist through the full output")
    )


def source_preserved(
    req: Requirement, plan: FinalPlan, _f: Footage, _p: FinalPlan | None
) -> CheckResult:
    """Compare compiled source ranges with fixture-owned expected values."""
    expected = req.params.get("clips") or req.params.get("source_ranges") or []
    if not isinstance(expected, (dict, list)) or not expected:
        return "unmet", "no independent source ranges supplied"
    actual = list(plan.clips)
    bad: list[str] = []
    entries = (
        list(expected.items())
        if isinstance(expected, dict)
        else [(str(row.get("clip_id")), row) for row in expected if isinstance(row, dict)]
    )
    if len(entries) != len(expected):
        return "unmet", "invalid independent source ranges supplied"
    if len(actual) != len(entries):
        return "unmet", f"source occurrence count {len(actual)} != expected {len(entries)}"
    for clip_id, wanted in entries:
        match_index = next(
            (index for index, clip in enumerate(actual) if clip.clip_id == str(clip_id)), None
        )
        clip = actual.pop(match_index) if match_index is not None else None
        if clip is None:
            bad.append(f"{clip_id}: missing")
            continue
        if not isinstance(wanted, dict):
            bad.append(f"{clip_id}: invalid expected range")
            continue
        start, end = clip.source_start_s, clip.source_end_s
        expected_start = wanted.get("source_start_s", wanted.get("start_s"))
        expected_end = wanted.get("source_end_s", wanted.get("end_s"))
        if (
            start is None
            or end is None
            or expected_start is None
            or expected_end is None
            or abs(start - float(expected_start)) > 1e-9
            or abs(end - float(expected_end)) > 1e-9
        ):
            bad.append(f"{clip_id}: {(start, end)} != {(expected_start, expected_end)}")
    if bad:
        return "unmet", "; ".join(bad)
    return "met", f"{len(entries)} source ranges preserved"


def text_geometry(
    req: Requirement, plan: FinalPlan, _f: Footage, _p: FinalPlan | None
) -> CheckResult:
    """Check compiled placement fields against fixture-owned expected geometry."""
    expected = req.params.get("texts") or {}
    if not isinstance(expected, dict) or not expected:
        return "unmet", "no independent text geometry supplied"
    actual = {text.id: text for text in plan.texts}
    bad: list[str] = []
    for text_id, wanted in expected.items():
        text = actual.get(str(text_id))
        if text is None:
            bad.append(f"{text_id}: missing")
            continue
        for field in ("position", "alignment", "x_frac", "y_frac"):
            if field in wanted and getattr(text, field) != wanted[field]:
                bad.append(f"{text_id}.{field}: {getattr(text, field)!r} != {wanted[field]!r}")
    return ("unmet", "; ".join(bad)) if bad else ("met", f"{len(expected)} text geometries match")


def text_contains(
    req: Requirement, plan: FinalPlan, _f: Footage, _p: FinalPlan | None
) -> CheckResult:
    terms = [str(t) for t in req.params["terms"]]
    strict = bool(req.params.get("strict_unicode", False))
    scope = str(req.params.get("scope", "any"))
    haystack = _scope_texts(plan, scope)
    if not haystack:
        return "unmet", f"no text in scope {scope!r}"
    joined = " \n ".join(haystack)
    credit = 0.0
    missing: list[str] = []
    for term in terms:
        if nfc(term).casefold() in nfc(joined).casefold():
            credit += 1.0
        elif fold(term) in fold(joined):
            if strict:
                credit += 0.5
                missing.append(f"{term} (diacritics lost)")
            else:
                credit += 1.0
        else:
            missing.append(term)
    fraction = credit / len(terms)
    reason = "all terms present" if not missing else f"missing: {', '.join(missing)}"
    return _fraction_status(fraction), reason


def label_coverage(
    req: Requirement, plan: FinalPlan, _f: Footage, _p: FinalPlan | None
) -> CheckResult:
    if not plan.clips:
        return "unmet", "edit has no clips"
    covering = labels_covering(plan)
    labeled = sum(1 for hits in covering.values() if hits)
    fraction = labeled / len(covering)
    min_frac = float(req.params.get("min_frac", 1.0))
    reason = f"{labeled}/{len(covering)} clips carry a label"
    if fraction >= min_frac - 1e-9:
        return "met", reason
    return _fraction_status(fraction / max(min_frac, 1e-9)), reason


def label_exact(
    req: Requirement, plan: FinalPlan, _f: Footage, _p: FinalPlan | None
) -> CheckResult:
    wanted: dict[str, str] = req.params["labels"]
    covering = labels_covering(plan)
    credit = 0.0
    bad: list[str] = []
    for clip_id, literal in wanted.items():
        texts = [t.text for t in covering.get(clip_id, [])]
        if any(nfc(t) == nfc(literal) for t in texts):
            credit += 1.0
        elif any(fold(t) == fold(literal) for t in texts):
            credit += 0.5
            bad.append(f"{clip_id}: case/diacritics")
        else:
            bad.append(f"{clip_id}: {texts or 'no label'}")
    reason = "all labels exact" if not bad else "; ".join(bad)
    return _fraction_status(credit / len(wanted)), reason


def label_mentions(
    req: Requirement, plan: FinalPlan, _f: Footage, _p: FinalPlan | None
) -> CheckResult:
    """A described label must name the place: any alternative term for that clip."""
    wanted: dict[str, list[str]] = req.params["clips"]
    covering = labels_covering(plan)
    hit = 0
    bad: list[str] = []
    for clip_id, alternatives in wanted.items():
        text = fold(" ".join(t.text for t in covering.get(clip_id, [])))
        if any(fold(alt) in text for alt in alternatives):
            hit += 1
        else:
            bad.append(clip_id)
    reason = "every clip names its place" if not bad else f"no place named for: {', '.join(bad)}"
    return _fraction_status(hit / len(wanted)), reason


def _first_positions(plan: FinalPlan) -> dict[str, int]:
    positions: dict[str, int] = {}
    for index, clip in enumerate(sorted(plan.clips, key=lambda c: c.start_s)):
        positions.setdefault(clip.clip_id, index)
    return positions


def _concordance(order: list[str], rank: dict[str, float]) -> tuple[float, int]:
    ranked = [c for c in order if c in rank]
    pairs = list(combinations(ranked, 2))
    considered = [(a, b) for a, b in pairs if rank[a] != rank[b]]
    if not considered:
        return 0.0, 0
    good = sum(1 for a, b in considered if rank[a] < rank[b])
    return good / len(considered), len(considered)


def _capture_ts(clip: ClipRecord) -> float | None:
    fact = clip.fact("capture_time")
    if fact is None:
        return None
    try:
        return datetime.fromisoformat(fact.value.replace("Z", "+00:00")).timestamp()
    except ValueError:
        return None


def order_by_key(
    req: Requirement, plan: FinalPlan, footage: Footage, _p: FinalPlan | None
) -> CheckResult:
    key = str(req.params["key"])
    clips = {c.clip_id: c for c in footage.clips}
    rank: dict[str, float] = {}
    for clip_id, clip in clips.items():
        if key == "capture_time":
            value = _capture_ts(clip)
        elif key == "route_rank":
            value = float(clip.route_rank) if clip.route_rank is not None else None
        else:
            raise ValueError(f"unknown order key {key!r}")
        if value is not None:
            rank[clip_id] = value
    order = [c for c, _ in sorted(_first_positions(plan).items(), key=lambda kv: kv[1])]
    fraction, pairs = _concordance(order, rank)
    if pairs == 0:
        return "unmet", f"order_basis_unavailable: fewer than two clips have a {key}"
    reason = f"{fraction:.0%} of {pairs} clip pairs follow {key}"
    if fraction >= 1.0 - 1e-9:
        return "met", reason
    return ("partial" if fraction >= 0.75 else "unmet"), reason


def order_explicit(
    req: Requirement, plan: FinalPlan, _f: Footage, _p: FinalPlan | None
) -> CheckResult:
    sequence = [str(c) for c in req.params["sequence"]]
    rank = {c: float(i) for i, c in enumerate(sequence)}
    positions = _first_positions(plan)
    order = [c for c, _ in sorted(positions.items(), key=lambda kv: kv[1])]
    absent = [c for c in sequence if c not in positions]
    fraction, pairs = _concordance(order, rank)
    if pairs == 0:
        return "unmet", "fewer than two requested clips are in the edit"
    reason = f"{fraction:.0%} of {pairs} requested pairs are in order"
    if absent:
        reason += f"; missing clips: {', '.join(absent)}"
    if fraction >= 1.0 - 1e-9 and not absent:
        return "met", reason
    return ("partial" if fraction >= 0.75 else "unmet"), reason


def select_include(
    req: Requirement, plan: FinalPlan, footage: Footage, _p: FinalPlan | None
) -> CheckResult:
    wanted = (
        [c.clip_id for c in footage.clips]
        if req.params.get("all")
        else [str(c) for c in req.params["clip_ids"]]
    )
    used = {c.clip_id for c in plan.clips}
    missing = [c for c in wanted if c not in used]
    fraction = (len(wanted) - len(missing)) / len(wanted)
    reason = f"{len(wanted) - len(missing)}/{len(wanted)} requested clips used"
    if missing:
        reason += f"; left out: {', '.join(missing)}"
    return _fraction_status(fraction), reason


def select_exclude(
    req: Requirement, plan: FinalPlan, _f: Footage, _p: FinalPlan | None
) -> CheckResult:
    banned = [str(c) for c in req.params["clip_ids"]]
    used = {c.clip_id for c in plan.clips}
    present = [c for c in banned if c in used]
    fraction = (len(banned) - len(present)) / len(banned)
    reason = "excluded clips are absent" if not present else f"still used: {', '.join(present)}"
    return _fraction_status(fraction), reason


def duration_within(
    req: Requirement, plan: FinalPlan, _f: Footage, _p: FinalPlan | None
) -> CheckResult:
    target = float(req.params["target_s"])
    tol = float(req.params.get("tol_frac", 0.10))
    actual = plan.duration_s
    error = abs(actual - target) / target
    reason = f"{actual:.1f}s vs {target:.1f}s target ({error:.0%} off)"
    if error <= tol + 1e-9:
        return "met", reason
    return ("partial" if error <= 2.5 * tol else "unmet"), reason


def pacing_max_avg_clip(
    req: Requirement, plan: FinalPlan, _f: Footage, _p: FinalPlan | None
) -> CheckResult:
    if not plan.clips:
        return "unmet", "edit has no clips"
    limit = float(req.params["max_avg_clip_s"])
    average = fmean(c.duration_s for c in plan.clips)
    reason = f"average clip {average:.2f}s vs {limit:.2f}s limit"
    if average <= limit + 1e-9:
        return "met", reason
    return ("partial" if average <= 1.5 * limit else "unmet"), reason


def min_display_s(text: str) -> float:
    raw = LABEL_MIN_DISPLAY_BASE_S + LABEL_MIN_DISPLAY_PER_CHAR_S * len(text)
    return min(max(raw, LABEL_MIN_DISPLAY_FLOOR_S), LABEL_MIN_DISPLAY_CEIL_S)


def readability(
    req: Requirement, plan: FinalPlan, _f: Footage, _p: FinalPlan | None
) -> CheckResult:
    labels = label_texts(plan)
    if not labels:
        return "unmet", "no labels on screen to read"
    ok = sum(1 for t in labels if (t.end_s - t.start_s) + 1e-9 >= min_display_s(t.text))
    return _fraction_status(ok / len(labels)), f"{ok}/{len(labels)} labels stay long enough to read"


def _signature(plan: FinalPlan) -> tuple:
    return (
        tuple((c.clip_id, round(c.end_s - c.start_s, 2)) for c in plan.clips),
        tuple(sorted((t.role, t.text) for t in plan.texts)),
    )


def restructure_changed(
    _req: Requirement, plan: FinalPlan, _f: Footage, previous: FinalPlan | None
) -> CheckResult:
    if previous is None:
        return "unmet", "no earlier edit to restructure"
    if _signature(plan) == _signature(previous):
        return "unmet", "the edit is unchanged"
    return "met", "the edit was restructured"


def font_forbidden(
    req: Requirement, plan: FinalPlan, _f: Footage, _p: FinalPlan | None
) -> CheckResult:
    banned = {fold(str(f)) for f in req.params["fonts"]}
    texts = [t for t in plan.texts if t.font_family]
    if not texts:
        return "met", "no styled text to violate the rule"
    clean = sum(1 for t in texts if fold(str(t.font_family)) not in banned)
    reason = f"{clean}/{len(texts)} text lanes avoid {sorted(banned)}"
    return _fraction_status(clean / len(texts)), reason


# ── Receipt, dedupe and correction checkers (KRI-185 P6b) ────────────────────


def _label_sequence(plan: FinalPlan) -> list[str]:
    """The first label on each labelled clip, in the order the clips play."""
    covering = labels_covering(plan)
    ordered = [c for c, _ in sorted(_first_positions(plan).items(), key=lambda kv: kv[1])]
    return [covering[c][0].text for c in ordered if covering.get(c)]


def label_no_consecutive_repeat(
    _req: Requirement, plan: FinalPlan, _f: Footage, _p: FinalPlan | None
) -> CheckResult:
    """A label that repeats the previous kept label is noise, not a caption (KRI-210)."""
    sequence = _label_sequence(plan)
    if not sequence:
        return "unmet", "no labels on screen to compare"
    repeats = sum(1 for a, b in zip(sequence, sequence[1:], strict=False) if fold(a) == fold(b))
    reason = f"{repeats} label(s) repeat the one before them"
    return _fraction_status(1 - repeats / len(sequence)), reason


def text_avoids(
    req: Requirement, plan: FinalPlan, _f: Footage, _p: FinalPlan | None
) -> CheckResult:
    """None of these terms may appear (a mixed-language name, a banned phrase)."""
    terms = [str(t) for t in req.params["terms"]]
    joined = fold(" \n ".join(_scope_texts(plan, str(req.params.get("scope", "any")))))
    present = [t for t in terms if fold(t) in joined]
    if not present:
        return "met", "none of the avoided terms appear"
    return _fraction_status(1 - len(present) / len(terms)), f"still present: {', '.join(present)}"


def labels_none(
    _req: Requirement, plan: FinalPlan, _f: Footage, _p: FinalPlan | None
) -> CheckResult:
    """No label may be invented when no fact grounds it."""
    labels = label_texts(plan)
    if not labels:
        return "met", "no invented labels"
    return "unmet", f"{len(labels)} label(s) were made up: {[t.text for t in labels][:3]}"


def font_all_equal(
    req: Requirement, plan: FinalPlan, _f: Footage, _p: FinalPlan | None
) -> CheckResult:
    """Every text lane (optionally only some roles) is set in this font: a bulk style
    request must reach all of them, not the first eight."""
    font = fold(str(req.params["font"]))
    roles = set(req.params.get("roles") or [])
    texts = [t for t in plan.texts if not roles or t.role in roles]
    if not texts:
        return "unmet", "no text lanes to restyle"
    ok = sum(1 for t in texts if fold(t.font_family or "") == font)
    return _fraction_status(
        ok / len(texts)
    ), f"{ok}/{len(texts)} text lanes use {req.params['font']}"


def clips_unchanged(
    _req: Requirement, plan: FinalPlan, _f: Footage, previous: FinalPlan | None
) -> CheckResult:
    """A style or text edit must not re-plan the clips."""
    if previous is None:
        return "unmet", "no earlier edit to compare with"
    same = _signature_clips(plan) == _signature_clips(previous)
    return ("met", "clips untouched") if same else ("unmet", "the clip layout changed")


def _signature_clips(plan: FinalPlan) -> tuple:
    return tuple((c.clip_id, round(c.end_s - c.start_s, 2)) for c in plan.clips)


def label_single_change(
    req: Requirement, plan: FinalPlan, _f: Footage, previous: FinalPlan | None
) -> CheckResult:
    """ "That's X, not Y" edits ONE label: the named clip says the new text and every other
    label is exactly as it was."""
    if previous is None:
        return "unmet", "no earlier edit to compare with"
    clip_id, wanted = str(req.params["clip_id"]), str(req.params["text"])
    now, before = labels_covering(plan), labels_covering(previous)
    target = [t.text for t in now.get(clip_id, [])]
    if not any(nfc(t) == nfc(wanted) for t in target):
        return "unmet", f"{clip_id} reads {target or 'no label'}, wanted {wanted!r}"
    moved = [
        c
        for c in before
        if c != clip_id and [t.text for t in before[c]] != [t.text for t in now.get(c, [])]
    ]
    if moved:
        return "partial", f"corrected {clip_id} but also changed: {', '.join(moved)}"
    return "met", f"only {clip_id} changed"


def reply_states(
    req: Requirement, _plan: FinalPlan, _f: Footage, _p: FinalPlan | None, reply: str | None
) -> CheckResult:
    """The reply itself must say something (a receipt: what the AI saw and did), and/or
    must not say something (a false claim). Reply-only; judged on the turn it was asked."""
    all_of = [str(p) for p in req.params.get("all_of", [])]
    none_of = [str(p) for p in req.params.get("none_of", [])]
    if reply is None:
        return ("unmet", "no reply to read") if all_of else ("met", "no reply, so no false claim")
    text = unicodedata.normalize("NFC", reply)
    missing = [p for p in all_of if not re.search(p, text, re.IGNORECASE)]
    claimed = [p for p in none_of if re.search(p, text, re.IGNORECASE)]
    if claimed:
        return "unmet", f"reply claims: {', '.join(claimed)}"
    if not missing:
        return "met", "reply says what it must"
    if len(missing) < len(all_of):
        return "partial", f"reply omits: {', '.join(missing)}"
    return "unmet", f"reply omits: {', '.join(missing)}"


def speech_excerpts(
    req: Requirement, plan: FinalPlan, _f: Footage, _p: FinalPlan | None
) -> CheckResult:
    """KRI-282: spoken excerpts are grounded, play over other footage where asked, and the
    edit returns to the speaker.

    Params: ``min_excerpts`` (default 1), ``over_broll`` (bool), ``return_to_speaker`` (bool),
    ``speaker_clip_id`` (str, optional; inferred from the speech sections when absent).
    """
    min_excerpts = int(req.params.get("min_excerpts", 1))
    grounded = [s for s in plan.speech if s.grounded]
    if not grounded:
        return "unmet", "no grounded spoken excerpt in the edit"
    speaker_id = str(req.params.get("speaker_clip_id") or grounded[0].clip_id)
    checks: list[tuple[bool, str]] = [
        (
            len(grounded) >= min_excerpts,
            f"{len(grounded)}/{min_excerpts} grounded excerpts",
        )
    ]
    if len(grounded) < len(plan.speech):
        checks.append((False, f"{len(plan.speech) - len(grounded)} excerpt(s) not grounded"))
    if req.params.get("over_broll"):
        over = [
            s
            for s in grounded
            if s.visual == "cutaways"
            and any(
                c.clip_id != s.clip_id and _overlap(c.start_s, c.end_s, s.start_s, s.end_s) > 0.25
                for c in plan.clips
            )
        ]
        checks.append(
            (
                bool(over),
                "speech plays over other footage"
                if over
                else "speech never plays over other footage",
            )
        )
    if req.params.get("return_to_speaker"):
        first_other = min((c.start_s for c in plan.clips if c.clip_id != speaker_id), default=None)
        returned = first_other is not None and any(
            s.visual == "speaker" and s.start_s >= first_other - 1e-6 for s in grounded
        )
        checks.append(
            (
                returned,
                "returns to the speaker" if returned else "never returns to the speaker",
            )
        )
    passed = sum(1 for ok, _ in checks if ok)
    reason = "; ".join(text for _ok, text in checks)
    if passed == len(checks):
        return "met", reason
    return ("partial" if passed else "unmet"), reason


CHECKERS: dict[str, Checker] = {
    "speech_excerpts": speech_excerpts,
    "title_exact": title_exact,
    "title_persistent": title_persistent,
    "source_preserved": source_preserved,
    "text_geometry": text_geometry,
    "text_contains": text_contains,
    "label_coverage": label_coverage,
    "label_exact": label_exact,
    "label_mentions": label_mentions,
    "order_by_key": order_by_key,
    "order_explicit": order_explicit,
    "select_include": select_include,
    "select_exclude": select_exclude,
    "duration_within": duration_within,
    "pacing_max_avg_clip": pacing_max_avg_clip,
    "readability": readability,
    "restructure_changed": restructure_changed,
    "font_forbidden": font_forbidden,
    "label_no_consecutive_repeat": label_no_consecutive_repeat,
    "text_avoids": text_avoids,
    "labels_none": labels_none,
    "font_all_equal": font_all_equal,
    "clips_unchanged": clips_unchanged,
    "label_single_change": label_single_change,
}

# Checkers that read the reply as well as the edit.
ReplyChecker = Callable[
    [Requirement, FinalPlan, Footage, FinalPlan | None, str | None], CheckResult
]
REPLY_CHECKERS: dict[str, ReplyChecker] = {"reply_states": reply_states}

# Requirements about something that must HAPPEN on the turn they are asked (compare with the
# edit before it), not a property the finished edit must keep having.
EVENT_CHECKERS = frozenset(
    {"restructure_changed", "clips_unchanged", "label_single_change", "reply_states"}
)

# Reply markers that say "this part was not done". Deliberately excludes "unchanged":
# "Everything else is unchanged" is exactly the sentence that hid the KRI-185 drops.
DISCLAIMER = re.compile(
    r"\b(not done|couldn'?t|could not|can'?t|cannot|unable|wasn'?t able|not able|didn'?t|"
    r"isn'?t available|is unavailable|not possible|unavailable|unsupported|not supported)\b",
    re.IGNORECASE,
)


def known_checker_ids() -> frozenset[str]:
    return frozenset(CHECKERS) | frozenset(REPLY_CHECKERS)


def run_checker(
    req: Requirement,
    plan: FinalPlan,
    footage: Footage,
    previous: FinalPlan | None = None,
    reply: str | None = None,
) -> CheckResult:
    if req.checker in REPLY_CHECKERS:
        return REPLY_CHECKERS[req.checker](req, plan, footage, previous, reply)
    try:
        checker = CHECKERS[req.checker]
    except KeyError as exc:
        raise KeyError(f"{req.id}: unknown checker {req.checker!r}") from exc
    return checker(req, plan, footage, previous)
