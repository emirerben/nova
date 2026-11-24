"""KRI-1611: server-issued options and a seeded pick for a Talking edit's intro scene. No model.

`intro_scene` holds the closed `IntroScene` shape and its compiler to today's title rows. This
module picks one for a job and wires it in, flagged (``INTRO_SCENE_ENABLED`` +
``INTRO_SCENE_USER_IDS``, default off) and stamped at dispatch like `phone_title_look`'s
`TitleLookAsk`, so a re-render draws the same scene.

* Dispatch (`stamp_intro_scene`, next to `stamp_title_look`): admits the job, then pins a seed,
  an ordered list of footage-free candidates (accent split of HER words, font pair that can draw
  them, entrance) and per-candidate zone / ink orders. The job's earlier looks are excluded from
  the first picks. It reads the pinned phone text policy and title-look ask, never her prose, and
  needs no footage, Skia or trace event (dispatch holds the item lock).
* Worker (`fit_intro_scene`, called from `_phone_talking_title_rows`): the footage half. Each
  candidate x zone is compiled (`compile_intro_scene`, its typical-speaker preflight) and placed
  with the real `place_title_look`; the first whose REAL receipt is clean
  (`intro_scene.placement_is_clean`) wins. Nothing fits: today's title rows, byte-identical.
* The ink is the first of the pinned order that reads on the sampled take frames
  (`intro_ink_pass`, run where the title legibility pass has those frames).

Ways this could go wrong, and the guard for each (tests/pipeline/test_intro_scene_pick.py and
the `make verify-intro-scenes` picks section):

* P1: flag off, an unadmitted account, or a job without a stamp changes a row. Nothing is stamped
  and the worker reads the stamp only.
* P2: the scene overrides the creator. It yields to her typed " / " stack, a pinned title-look
  ask, any pinned text rule, a logo above the title, and a job without the informative-text stamp
  (protocol 55 approving build), so her edits and chat ops keep the tier above it.
* P3: a re-render picks differently. The candidate list is pinned; the worker never re-seeds.
* P4: the same look every video. Her last `RECENT_LIMIT` pinned looks are tried last.
* P5: a scene that does not fit this footage ships. Every candidate must compile AND get a clean
  real receipt, else the next one, else today's rows.
* P6: the split drops or reorders her words. Voices are one per word of her title, and the
  compiler re-checks it (`check_scene`).
* P7: the Turkish title lacks glyphs or caps wrong. `issue_menu` filters faces by coverage and
  the row carries ``text_language`` ("trendi" -> "TRENDİ").
* P8: more than one ink, or ink invisible on the footage. One ink per scene, chosen on the
  sampled frames; her own colour is never overwritten.
* P9: the approval check refuses a stack of her title (`phone_title_look.stacked_title_matches`).
"""

from __future__ import annotations

import hashlib
import random
from collections.abc import Callable, Mapping, Sequence
from typing import Any

import structlog

from app.pipeline import intro_scene as scene_mod

log = structlog.get_logger(__name__)

INTRO_SCENE_ASK_FIELD = "intro_scene_ask"
INTRO_SCENE_VERSION = 1
MAX_CANDIDATES = 6
MAX_ATTEMPTS = 9  # compiles + real placements per render, across candidates and zones
RECENT_LIMIT = 4
_MAX_ACCENT_WORDS = 3
_MIN_INK_CONTRAST = 4.5
INTRO_INKS_PARAM = "intro_inks"
# Explicit text asks that put the creator above a generated look (`requested` roles and
# `preferences` roles of the pinned phone text policy).
_ASK_ROLES = ("all_text", "title")


def _rng(seed: str, *parts: object) -> random.Random:
    digest = hashlib.sha256("|".join([seed, *map(str, parts)]).encode()).digest()
    return random.Random(int.from_bytes(digest[:8], "big"))


# --- the footage-free pick ------------------------------------------------------------------


def accent_splits(title: str) -> list[tuple[str, ...]]:
    """Every way to set one run of 1-3 of HER words as the accent voice, the rest solid, that the
    compiler can read as 2-5 stacked lines. Voices are one per word, in her order."""
    words = scene_mod.title_words(title)
    count = len(words)
    found: list[tuple[str, ...]] = []
    for length in range(1, min(_MAX_ACCENT_WORDS, count - 1) + 1):
        for start in range(count - length + 1):
            voices = tuple(
                "accent" if start <= index < start + length else "solid" for index in range(count)
            )
            runs = scene_mod._runs(words, voices)
            if 2 <= len(runs) <= scene_mod._MAX_RUNS and all(
                scene_mod._run_reads_as_a_line(run.text) for run in runs
            ):
                found.append(voices)
    return found


def pick_candidates(title: str, *, seed: str, recent: Sequence[str] = ()) -> list[dict[str, Any]]:
    """Up to ``MAX_CANDIDATES`` scenes for ``title`` in the order to try them. Footage-free: each
    names an accent split, a font pair that can draw the title, an entrance and a zone order. A
    pair she has used lately is tried last; with no other pair it is still offered."""
    splits = accent_splits(title)
    menu = scene_mod.issue_menu(title)
    if not splits or not menu.font_pairs:
        return []
    rng = _rng(seed, "candidates", title)
    pairs = list(menu.font_pairs)
    rng.shuffle(pairs)
    pairs.sort(key=lambda pair: pair in recent)  # stable: only moves recent looks back
    out: list[dict[str, Any]] = []
    for round_index in range(2):
        for pair in pairs:
            if len(out) >= MAX_CANDIDATES:
                break
            voices = splits[rng.randrange(len(splits))]
            entrance = rng.choice(menu.entrances_for(pair))
            zones = list(menu.zones)
            rng.shuffle(zones)
            if rng.random() < 0.6 and "above_head" in zones:  # the references' usual spot
                zones.remove("above_head")
                zones.insert(0, "above_head")
            candidate = {
                "voices": list(voices),
                "font_pair": pair,
                "entrance": entrance,
                "zones": zones,
            }
            if round_index == 0 or candidate not in out:
                out.append(candidate)
    return out


def ink_order(seed: str) -> list[str]:
    inks = list(scene_mod.INKS)
    _rng(seed, "inks").shuffle(inks)
    return inks


# --- dispatch -------------------------------------------------------------------------------


def has_text_ask(policy: Mapping[str, Any]) -> bool:
    """Did the creator (or her saved rules) already say how the title or all text looks?"""
    for source in (policy.get("requested"), policy.get("preferences")):
        if not isinstance(source, Mapping):
            continue
        for role in _ASK_ROLES:
            rule = source.get(role)
            if isinstance(rule, Mapping) and any(k != "automatic_font" for k in rule):
                return True
    return False


def language_of(title: str, spoken: object = None) -> str | None:
    """``"tr"`` for a Turkish title (its own letters, else a Turkish spoken language), so a caps
    face sets İ and ı right; ``None`` otherwise."""
    if scene_mod._TURKISH_LETTERS & set(title):
        return "tr"
    return "tr" if str(spoken or "").lower().startswith("tr") else None


def recent_intro_looks(session: Any, user_id: Any) -> tuple[str, ...]:
    """The font pairs of the creator's last pinned scenes, newest first. Never raises."""
    try:
        from sqlalchemy import func, select  # noqa: PLC0415

        from app.models import Job  # noqa: PLC0415

        rows = session.execute(
            select(func.jsonb_extract_path_text(Job.assembly_plan, INTRO_SCENE_ASK_FIELD, "look"))
            .where(Job.user_id == user_id)
            .order_by(Job.created_at.desc())
            .limit(RECENT_LIMIT)
        ).scalars()
        return tuple(look for look in rows if look)
    except Exception as exc:  # noqa: BLE001 - no history only means no exclusion
        log.warning("intro_scene.recent_failed", error=str(exc)[:200])
        return ()


def stamp_intro_scene(job: Any, *, session: Any = None) -> None:
    """Initial dispatch pins the fitted scene's candidates on the job, after
    `stamp_phone_text_style` and `stamp_title_look` (it reads their pins). Nothing is stamped
    with the flag off or the account not admitted, on a legacy saved generation, for a title she
    typed as a stack or styled, on a job without the informative-text stamp, or when no scene can
    be built for her words. Workers read the stamp, never the flag."""
    from app.config import settings  # noqa: PLC0415
    from app.pipeline import informative_text  # noqa: PLC0415
    from app.pipeline.phone_title_look import (  # noqa: PLC0415
        TITLE_LOOK_ASK_FIELD,
        split_title_lines,
    )
    from app.services.phone_text_styling import PHONE_TEXT_STYLE_FIELD  # noqa: PLC0415

    current = dict(job.assembly_plan or {})
    if INTRO_SCENE_ASK_FIELD in current or TITLE_LOOK_ASK_FIELD in current:
        return
    user_id = getattr(job, "user_id", None)
    if not (settings.intro_scene_for(user_id) and settings.title_look_enabled):
        return
    if current.get("variants") or current.get("_device_render_v1"):
        return
    policy = current.get(PHONE_TEXT_STYLE_FIELD)
    if not informative_text.shapes_enabled(policy) or has_text_ask(policy):
        return
    candidates = job.all_candidates or {}
    strategy = candidates.get("creator_strategy")
    title = strategy.get("opening_title") if isinstance(strategy, dict) else None
    if not isinstance(title, str) or not title.strip() or split_title_lines(title) is not None:
        return
    try:
        seed = hashlib.sha256(
            f"{current.get('creator_generation_id') or ''}|{user_id}|{title}".encode()
        ).hexdigest()[:16]
        recent = recent_intro_looks(session, user_id) if session is not None else ()
        picked = pick_candidates(title, seed=seed, recent=recent)
    except Exception as exc:  # noqa: BLE001 - a styled intro never blocks a dispatch
        log.warning("intro_scene.stamp_failed", error=str(exc)[:200])
        return
    if not picked:
        return
    current[INTRO_SCENE_ASK_FIELD] = {
        "version": INTRO_SCENE_VERSION,
        "seed": seed,
        "look": picked[0]["font_pair"],
        "inks": ink_order(seed),
        "candidates": picked,
        "excluded": list(recent),
    }
    job.assembly_plan = current


def pinned_intro_scene(plan: Mapping[str, Any] | None) -> dict[str, Any] | None:
    """The job's stamped scene ask, or ``None`` (unstamped, or a shape this build cannot read)."""
    raw = (plan or {}).get(INTRO_SCENE_ASK_FIELD)
    if not isinstance(raw, Mapping) or raw.get("version") != INTRO_SCENE_VERSION:
        return None
    inks = raw.get("inks")
    candidates = raw.get("candidates")
    if not (isinstance(inks, list) and inks and isinstance(candidates, list) and candidates):
        return None
    return dict(raw)


# --- the worker: footage half -----------------------------------------------------------------

Place = Callable[
    [list[dict[str, Any]], "str | None"], "tuple[list[dict[str, Any]], dict[str, Any]]"
]


def fit_intro_scene(
    ask: Mapping[str, Any],
    title: str,
    *,
    canvas: Any,
    timeline_duration_s: float,
    duration_s: float | None,
    language: str | None,
    place: Place,
) -> tuple[list[dict[str, Any]] | None, dict[str, Any] | None, dict[str, Any]]:
    """The first stamped candidate x zone that compiles and gets a clean REAL placement, as
    ``(placed rows, receipt, info)``; ``(None, None, info)`` means "use today's title". ``place``
    is the real `place_title_look` call of this render. Never raises."""
    info: dict[str, Any] = {"version": INTRO_SCENE_VERSION, "seed": ask.get("seed"), "tried": []}
    inks = [ink for ink in ask.get("inks") or [] if ink in scene_mod.INKS]
    if not inks:
        info["reason"] = "no_inks"
        return None, None, info
    attempts = 0
    try:
        for index, candidate in enumerate(ask.get("candidates") or []):
            for zone in candidate.get("zones") or []:
                if attempts >= MAX_ATTEMPTS:
                    info["reason"] = "attempts_exhausted"
                    return None, None, info
                attempts += 1
                step = {"candidate": index, "font_pair": candidate.get("font_pair"), "zone": zone}
                try:
                    scene = scene_mod.IntroScene(
                        decision="compose",
                        voices=tuple(candidate["voices"]),
                        font_pair=candidate["font_pair"],
                        ink=inks[0],
                        zone=zone,
                        entrance=candidate["entrance"],
                        sound="none",  # a sound is a suggestion, not an automatic add (project Q1)
                    )
                except Exception:  # noqa: BLE001 - a stamp this build cannot read
                    info["tried"].append({**step, "reason": "invalid_scene"})
                    continue
                out = scene_mod.compile_intro_scene(
                    scene,
                    title,
                    canvas=canvas,
                    timeline_duration_s=timeline_duration_s,
                    language=language,
                    duration_s=duration_s,
                )
                if out.rows is None:
                    info["tried"].append({**step, "reason": out.reason})
                    continue
                placed, receipt = place(out.rows, out.position)
                if not scene_mod.placement_is_clean(receipt):
                    info["tried"].append({**step, "reason": "placement_unclean"})
                    continue
                info.update(chosen={**step, "ink": inks[0]}, reason="ok")
                return _tag_rows(placed, inks), receipt, info
        info["reason"] = "no_candidate_fits"
    except Exception as exc:  # noqa: BLE001 - a styled intro never blocks a render
        log.warning("intro_scene.fit_failed", error=str(exc)[:200])
        info["reason"] = "fit_error"
    return None, None, info


def _tag_rows(rows: list[dict[str, Any]], inks: Sequence[str]) -> list[dict[str, Any]]:
    """Carry the ink order on each row so the legibility step can choose the ink on the take."""
    from app.pipeline.phone_title_look import TITLE_LOOK_PARAM  # noqa: PLC0415

    tagged = []
    for row in rows:
        params = dict(row.get("source_params") or {})
        look = dict(params.get(TITLE_LOOK_PARAM) or {})
        look[INTRO_INKS_PARAM] = list(inks)
        params[TITLE_LOOK_PARAM] = look
        tagged.append({**row, "source_params": params})
    return tagged


def intro_ink_pass(
    rows: list[dict[str, Any]], frames: Sequence[Any], *, canvas: Any
) -> list[dict[str, Any]]:
    """One ink for the whole scene: the first of the pinned order that reads (4.5:1) under every
    row on the sampled take frames, else the best reading. Rows she coloured stay as they are;
    with no frames the pinned first ink stands. Never raises."""
    from app.pipeline import caption_legibility as legibility  # noqa: PLC0415
    from app.pipeline import phone_subtitled_title as title_mod  # noqa: PLC0415
    from app.pipeline.phone_title_look import TITLE_LOOK_PARAM  # noqa: PLC0415

    def inks_of(row: Mapping[str, Any]) -> list[str]:
        look = (row.get("source_params") or {}).get(TITLE_LOOK_PARAM) or {}
        found = look.get(INTRO_INKS_PARAM)
        return [i for i in found if i in scene_mod.INKS] if isinstance(found, list) else []

    scene_rows = [row for row in rows if inks_of(row)]
    if not scene_rows or not frames:
        return rows
    try:
        order = inks_of(scene_rows[0])
        boxes = []
        for row in scene_rows:
            box = title_mod._measure_title_box(row, canvas=canvas)
            under = [
                f
                for f in frames
                if f.start_s < float(row["end_s"]) and float(row["start_s"]) < f.end_s
            ]
            boxes.append(((box.left, box.top, box.right, box.bottom), under))
        scored: list[tuple[str, float]] = []
        for name in order:
            fill = scene_mod.INKS[name]
            readings = [legibility.worst_contrast(fill, box, under) for box, under in boxes]
            known = [r for r in readings if r is not None]
            scored.append((name, min(known) if known else 0.0))
        if not any(score for _, score in scored):
            return rows
        chosen = next(
            (name for name, score in scored if score >= _MIN_INK_CONTRAST),
            max(scored, key=lambda item: item[1])[0],
        )
        fill = scene_mod.INKS[chosen]
        out = []
        for row in rows:
            authored = (row.get("source_params") or {}).get("creator_style_fields") or []
            if inks_of(row) and "color" not in authored:
                row = {**row, "color": fill}
            out.append(row)
        return out
    except Exception as exc:  # noqa: BLE001 - an ink nicety never blocks a render
        log.warning("intro_scene.ink_failed", error=str(exc)[:200])
        return rows
