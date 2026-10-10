"""Before/after diff of one compiled editor bundle (KRI-558).

``compile_editor_ops`` holds the working set before and after the creator's ops. Receipts used
to see only the finished draft, so most asks ("add a fade-in to all of them", "make it
smaller") could not be judged and ended as "can't check". This module records what the bundle
actually changed, per lane and field, and says it in plain words ("Added a fade-in to all 3
texts"), so a receipt can name the change instead of guessing at it.

The diff is computed on the SAME working set the handlers mutate (not on the merged draft
payload), so unsaved client-state edits and partial merges never show up as this turn's work.
Everything is compared on EFFECTIVE values: a legacy ``effect: fade-in`` and
``animation_phases.entrance == "fade"`` are one entrance, ``Inter-Bold`` and ``Inter`` one
family, and a named position ignores stale ``x_frac``/``y_frac``. Entries a bundle only caused
as a side effect (guided retiming of labels) are marked ``derived`` and never prove a request.
"""

from __future__ import annotations

import colorsys
import copy
import re
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from typing import Any, NamedTuple

from app.kria.reply_language import say

# --------------------------------------------------------------------------- vocabulary

_FACE_WORDS = re.compile(
    r"[\s\-_]+(regular|medium|semibold|bold|extrabold|black|light|italic)$", re.IGNORECASE
)
_LEGACY_EFFECT_ENTRANCE = {
    "static": "none",
    "none": "none",
    "fade-in": "fade",
    "pop-in": "pop",
    "slide-in": "slide",
    "typewriter": "typewriter",
}
# Style keys compared as-is (after rounding); each is its own diff field.
_PLAIN_STYLE_KEYS = (
    "alignment",
    "text_case",
    "color",
    "highlight_color",
    "background_color",
    "letter_spacing",
    "line_spacing",
    "max_width_frac",
    "stroke_width",
    "shadow_enabled",
    "rotation_deg",
    "behind_subject",
)
_STYLE_DEFAULTS = {"alignment": "center", "text_case": "none"}
# Ops that retime text on purpose; a text timing change without one is a guided side effect.
_TEXT_TIMING_OPS = frozenset(
    {
        "set_text_timing",
        "set_texts_timing",
        "add_text",
        "realign_labels",
        "label_each_clip",
        "replace_text_sequence",
        "patch_text_appearance",
    }
)
_DEFAULT_SIZE_PX = 72.0  # what ``size_scale`` multiplies when a row has no size (apply_patch)
_TOLERANCE = 0.005


class DiffTarget(NamedTuple):
    id: str
    kind: str  # title | label | text | clip | track | ...
    clip_id: str | None
    text: str
    before: Any
    after: Any
    # A clip target carries how many clips the timeline holds ("the last clip" needs it).
    extra: Any = None


@dataclass(frozen=True)
class DiffEntry:
    lane: str  # text | timeline | transition | audio | captions | caption_meta | title | ...
    field: str
    targets: tuple[DiffTarget, ...]
    derived: bool = False


@dataclass(frozen=True)
class EditorDiff:
    entries: tuple[DiffEntry, ...] = ()
    live_text_count: int = 0
    # True when the diff could not be computed. Nothing is then claimed about what changed,
    # and an edit is never rolled back or reported as "nothing changed" because of it.
    unavailable: bool = False

    def real(self) -> tuple[DiffEntry, ...]:
        """Entries the creator's ops made on purpose (not a side effect of another edit)."""
        return tuple(entry for entry in self.entries if not entry.derived)

    def empty(self) -> bool:
        return not self.unavailable and not self.real()

    def changed_text_ids(self) -> tuple[str, ...]:
        """Ids of the text rows this bundle styled, retimed, rewrote or added."""
        seen: dict[str, None] = {}
        for entry in self.real():
            if entry.lane == "text" and entry.field != "removed":
                for target in entry.targets:
                    seen.setdefault(target.id, None)
        return tuple(seen)

    def to_json(self, limit: int = 24) -> list[dict[str, Any]]:
        return [
            {
                "lane": entry.lane,
                "field": entry.field,
                "derived": entry.derived,
                "targets": [
                    {
                        "id": t.id,
                        "kind": t.kind,
                        "before": _jsonable(t.before),
                        "after": _jsonable(t.after),
                    }
                    for t in entry.targets[:8]
                ],
            }
            for entry in self.entries[:limit]
        ]


def _jsonable(value: Any) -> Any:
    if isinstance(value, (str, int, float, bool)) or value is None:
        return value
    if isinstance(value, (list, tuple)):
        return [_jsonable(v) for v in value][:8]
    if isinstance(value, Mapping):
        return {str(k): _jsonable(v) for k, v in list(value.items())[:8]}
    return str(value)


# ------------------------------------------------------------------ effective values


def _num(value: object) -> float | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    return float(value)


def _r(value: object, places: int = 3) -> Any:
    number = _num(value)
    return round(number, places) if number is not None else value


def font_family_key(name: object) -> str | None:
    """A font's FAMILY, so Inter, Inter Regular and the legacy ``Inter-Bold`` compare equal."""
    if not isinstance(name, str) or not name.strip():
        return None
    from app.pipeline.font_aliases import registry_font_name  # noqa: PLC0415

    key = registry_font_name(name.strip())
    stripped = _FACE_WORDS.sub("", key).strip()
    return (stripped or key).casefold()


def effective_entrance(row: Mapping[str, Any]) -> str | None:
    """The entrance a saved row plays: explicit phases win, else the legacy effect's."""
    phases = row.get("animation_phases")
    if isinstance(phases, Mapping):
        entrance = phases.get("entrance")
        return entrance if isinstance(entrance, str) else None
    effect = row.get("effect")
    return _LEGACY_EFFECT_ENTRANCE.get(effect) if effect is not None else "none"


def _phase(row: Mapping[str, Any], key: str) -> Any:
    phases = row.get("animation_phases")
    if isinstance(phases, Mapping):
        return phases.get(key)
    return {"exit": "none", "loop": "none", "speed": 1}[key]


def effective_position(row: Mapping[str, Any]) -> tuple[str | None, float | None, float | None]:
    """(named position, x_frac, y_frac); the fractions only mean something for ``custom``."""
    position = row.get("position")
    named = position if isinstance(position, str) and position else None
    if named == "custom":
        return named, _num(row.get("x_frac")), _num(row.get("y_frac"))
    return named, None, None


def _position_signature(row: Mapping[str, Any]) -> tuple[Any, ...]:
    named, x, y = effective_position(row)
    return (named, _r(x, 2), _r(y, 2))


def effective_size(row: Mapping[str, Any]) -> float | None:
    return _num(row.get("size_px"))


def color_family(value: object) -> str | None:
    """A coarse colour name for ``#RRGGBB`` (white, black, grey, red, ...), else None."""
    if not isinstance(value, str) or not re.fullmatch(r"#[0-9A-Fa-f]{6}", value):
        return None
    red, green, blue = (int(value[i : i + 2], 16) / 255 for i in (1, 3, 5))
    hue, sat, val = colorsys.rgb_to_hsv(red, green, blue)
    if val < 0.18:
        return "black"
    if sat < 0.12:
        return "white" if val > 0.8 else "grey"
    degrees = hue * 360
    for limit, name in (
        (15, "red"),
        (45, "orange"),
        (70, "yellow"),
        (165, "green"),
        (200, "cyan"),
        (260, "blue"),
        (290, "purple"),
        (345, "pink"),
    ):
        if degrees < limit:
            return name
    return "red"


# ------------------------------------------------------------------------ snapshots


class BeforeState(NamedTuple):
    text: dict[str, dict[str, Any]]
    slots: list[tuple[str, dict[str, Any]]]
    captions: list[dict[str, Any]]
    camera: list[dict[str, Any]]
    sfx: list[dict[str, Any]]


def _slot_key(row: dict[str, Any]) -> str:
    slot_id = row.get("slot_id")
    return slot_id if isinstance(slot_id, str) and slot_id else f"obj{id(row)}"


def snapshot_before(state: Any) -> BeforeState:
    """Deep copies of every lane a handler may mutate in place, taken before the first op."""
    from app.agents._schemas.text_element import narrated_storyboard_row_updates  # noqa: PLC0415

    text: dict[str, dict[str, Any]] = {}
    for row in state.text:
        if isinstance(row, dict) and isinstance(row.get("id"), str):
            copied = copy.deepcopy(row)
            # The compiler spells a storyboard bar's burned look out after the ops run;
            # do the same here so that normalisation never reads as this turn's change.
            copied.update(narrated_storyboard_row_updates(copied))
            text[row["id"]] = copied
    return BeforeState(
        text=text,
        slots=[(_slot_key(row), copy.deepcopy(row)) for row in state.slots],
        captions=copy.deepcopy(state.captions),
        camera=copy.deepcopy(state.camera_effects),
        sfx=copy.deepcopy(state.sound_effects),
    )


# ---------------------------------------------------------------------------- diff


def _is_text_row(row: Mapping[str, Any]) -> bool:
    from app.services.kria_editor_ops import is_caption_text_bar  # noqa: PLC0415

    return (
        isinstance(row.get("id"), str)
        and not row.get("removed")
        and row.get("role") != "lyric_line"
        and not is_caption_text_bar(dict(row))
    )


def _style_signature(row: Mapping[str, Any], key: str) -> Any:
    if key == "font_family":
        return font_family_key(row.get("font_family"))
    if key == "size":
        return _r(effective_size(row), 1)
    if key == "entrance":
        return effective_entrance(row)
    if key in ("exit", "loop", "speed"):
        return _r(_phase(row, key), 2)
    if key == "position":
        return _position_signature(row)
    value = row.get(key, _STYLE_DEFAULTS.get(key))
    if key in ("color", "highlight_color", "background_color") and isinstance(value, str):
        return value.upper()
    return _r(value)


def _style_display(row: Mapping[str, Any], key: str, signature: Any) -> Any:
    """What a target shows: the font's real name (the signature is its folded family key)."""
    if key == "font_family":
        font = row.get("font_family")
        return font if isinstance(font, str) and font else None
    if key == "size":
        # A row with no size is drawn at the default, which is what ``size_scale`` multiplies.
        return signature if signature is not None else _DEFAULT_SIZE_PX
    return signature


_TEXT_STYLE_DIFF_KEYS = (
    "font_family",
    "size",
    "entrance",
    "exit",
    "loop",
    "speed",
    "position",
    *_PLAIN_STYLE_KEYS,
)


def _text_entries(
    job: Any,
    variant: dict[str, Any],
    before: BeforeState,
    state: Any,
    text_diff: Iterable[Mapping[str, Any]],
    op_names: frozenset[str],
) -> tuple[list[DiffEntry], int]:
    from app.services.kria_editor_ops_text import bars_from_variant, classify  # noqa: PLC0415

    try:
        kinds = classify(bars_from_variant(job, variant))
    except Exception:  # noqa: BLE001 - classification is best effort, never blocks a draft
        kinds = {}
    for item in text_diff:
        if isinstance(item, Mapping) and isinstance(item.get("id"), str):
            kinds.setdefault(item["id"], str(item.get("role") or "text"))
    after = {row["id"]: row for row in state.text if isinstance(row, dict) and _is_text_row(row)}
    live_before = {rid: row for rid, row in before.text.items() if _is_text_row(row)}
    entries: list[DiffEntry] = []

    def target(rid: str, old: Any, new: Any, row: Mapping[str, Any]) -> DiffTarget:
        clip = row.get("clip_id")
        return DiffTarget(
            id=rid,
            kind=kinds.get(rid, "text"),
            clip_id=clip if isinstance(clip, str) and clip else None,
            text=str(row.get("text") or "")[:60],
            before=old,
            after=new,
        )

    added = [rid for rid in after if rid not in live_before]
    removed = [rid for rid in live_before if rid not in after]
    if added:
        entries.append(
            DiffEntry(
                "text",
                "added",
                tuple(target(r, None, after[r].get("text"), after[r]) for r in added),
            )
        )
    if removed:
        entries.append(
            DiffEntry(
                "text",
                "removed",
                tuple(target(r, live_before[r].get("text"), None, live_before[r]) for r in removed),
            )
        )
    common = [rid for rid in after if rid in live_before]
    reworded = [
        r
        for r in common
        if str(after[r].get("text") or "") != str(live_before[r].get("text") or "")
    ]
    if reworded:
        entries.append(
            DiffEntry(
                "text",
                "wording",
                tuple(
                    target(r, live_before[r].get("text"), after[r].get("text"), after[r])
                    for r in reworded
                ),
            )
        )
    for key in _TEXT_STYLE_DIFF_KEYS:
        moved = []
        for rid in common:
            old, new = _style_signature(live_before[rid], key), _style_signature(after[rid], key)
            if old != new:
                moved.append(
                    target(
                        rid,
                        _style_display(live_before[rid], key, old),
                        _style_display(after[rid], key, new),
                        after[rid],
                    )
                )
        if moved:
            entries.append(DiffEntry("text", key, tuple(moved)))
    retimed = []
    for rid in common:
        old = (_r(live_before[rid].get("start_s"), 2), _r(live_before[rid].get("end_s"), 2))
        new = (_r(after[rid].get("start_s"), 2), _r(after[rid].get("end_s"), 2))
        if old != new:
            retimed.append(target(rid, old, new, after[rid]))
    if retimed:
        deliberate = bool(op_names & _TEXT_TIMING_OPS)
        entries.append(DiffEntry("text", "timing", tuple(retimed), derived=not deliberate))
    return entries, len(after)


def _slot_dur(row: Mapping[str, Any]) -> float | None:
    duration = _num(row.get("duration_s"))
    if duration is None or duration <= 0:
        return None
    rate = _num(row.get("playback_rate"))
    return duration / (rate if rate and rate > 0 else 1.0)


_SLOT_FIELDS = (
    ("duration_s", "timeline", lambda r: _r(_slot_dur(r), 2)),
    ("in_s", "timeline", lambda r: _r(r.get("in_s") or 0.0, 2)),
    ("look_preset", "timeline", lambda r: r.get("look_preset") or "none"),
    ("playback_rate", "timeline", lambda r: _r(r.get("playback_rate") or 1.0, 2)),
    ("source_crop", "timeline", lambda r: r.get("source_crop")),
    (
        "transition_after",
        "transition",
        lambda r: (r.get("transition_after") or "cut", _r(r.get("transition_duration_s"), 2)),
    ),
)


def _slot_entries(before: BeforeState, state: Any) -> list[DiffEntry]:
    after_rows = [(_slot_key(row), row) for row in state.slots if isinstance(row, dict)]
    after = dict(after_rows)
    before_map = dict(before.slots)
    live_before = [(k, r) for k, r in before.slots if not r.get("removed")]
    live_after = [(k, r) for k, r in after_rows if not r.get("removed")]
    live_before_keys = [k for k, _ in live_before]
    live_after_keys = [k for k, _ in live_after]
    ordinal_before = {k: i + 1 for i, k in enumerate(live_before_keys)}
    ordinal_after = {k: i + 1 for i, k in enumerate(live_after_keys)}
    entries: list[DiffEntry] = []

    def slot_target(key: str, old: Any, new: Any, ordinal: dict[str, int]) -> DiffTarget:
        return DiffTarget(key, "clip", None, f"clip {ordinal.get(key, 0)}", old, new, len(ordinal))

    gone = [k for k in live_before_keys if k not in {a for a in live_after_keys}]
    if gone:
        entries.append(
            DiffEntry(
                "timeline",
                "removed",
                tuple(slot_target(k, None, None, ordinal_before) for k in gone),
            )
        )
    new_keys = [k for k in live_after_keys if k not in before_map]
    if new_keys:
        entries.append(
            DiffEntry(
                "timeline",
                "added",
                tuple(slot_target(k, None, None, ordinal_after) for k in new_keys),
            )
        )
    common = [k for k in live_after_keys if k in set(live_before_keys)]
    for name, lane, read in _SLOT_FIELDS:
        moved = []
        for key in common:
            old, new = read(before_map[key]), read(after[key])
            if old != new:
                moved.append(slot_target(key, old, new, ordinal_after))
        if moved:
            entries.append(DiffEntry(lane, name, tuple(moved)))
    seq_before = [k for k in live_before_keys if k in set(common)]
    seq_after = [k for k in live_after_keys if k in set(common)]
    if seq_before != seq_after:
        shifted = [k for i, k in enumerate(seq_after) if seq_before[i] != k]
        entries.append(
            DiffEntry(
                "timeline",
                "order",
                tuple(
                    DiffTarget(
                        k,
                        "clip",
                        None,
                        f"clip {ordinal_before.get(k, 0)}",
                        ordinal_before.get(k),
                        ordinal_after.get(k),
                    )
                    for k in shifted
                ),
            )
        )
    total_before = [_slot_dur(r) for _, r in live_before]
    total_after = [_slot_dur(r) for _, r in live_after]
    if (
        total_before
        and total_after
        and None not in total_before
        and None not in total_after
        and abs(sum(total_before) - sum(total_after)) > 0.05  # type: ignore[arg-type]
    ):
        entries.append(
            DiffEntry(
                "timeline",
                "total_duration",
                (
                    DiffTarget(
                        "total",
                        "video",
                        None,
                        "the video",
                        round(sum(total_before), 2),
                        round(sum(total_after), 2),  # type: ignore[arg-type]
                    ),
                ),
            )
        )
    return entries


def _by_id_entries(lane: str, label: str, old: list[dict], new: list[dict]) -> list[DiffEntry]:
    def key(row: Mapping[str, Any], index: int) -> str:
        rid = row.get("id")
        return rid if isinstance(rid, str) and rid else f"#{index}"

    before = {key(r, i): r for i, r in enumerate(old) if isinstance(r, dict)}
    after = {key(r, i): r for i, r in enumerate(new) if isinstance(r, dict)}
    out: list[DiffEntry] = []
    for name, ids in (
        ("added", [k for k in after if k not in before]),
        ("removed", [k for k in before if k not in after]),
        ("changed", [k for k in after if k in before and after[k] != before[k]]),
    ):
        if ids:
            out.append(
                DiffEntry(
                    lane, name, tuple(DiffTarget(k, label, None, label, None, None) for k in ids)
                )
            )
    return out


def _scalar(lane: str, field_name: str, label: str, old: Any, new: Any) -> DiffEntry | None:
    if old is not None and _r(old, 3) == _r(new, 3):
        return None
    return DiffEntry(lane, field_name, (DiffTarget(field_name, label, None, label, old, new),))


def diff_compiled_state(
    job: Any,
    variant: dict[str, Any],
    before: BeforeState,
    state: Any,
    text_diff: Iterable[Mapping[str, Any]] = (),
    op_names: Iterable[str] = (),
) -> EditorDiff:
    """What the bundle changed, lane by lane. Never raises: a failed lane is simply absent."""
    names = frozenset(op_names)
    entries: list[DiffEntry] = []
    live_text = 0
    failed = False
    lanes: list[tuple[str, Any]] = [
        ("text", lambda: _text_entries(job, variant, before, state, tuple(text_diff), names)),
        ("slots", lambda: _slot_entries(before, state)),
        (
            "captions",
            lambda: _by_id_entries("captions", "caption", before.captions, state.captions),
        ),
        (
            "camera",
            lambda: _by_id_entries(
                "camera_effects", "camera effect", before.camera, state.camera_effects
            ),
        ),
        ("sfx", lambda: _by_id_entries("sfx", "sound effect", before.sfx, state.sound_effects)),
    ]
    for name, build in lanes:
        try:
            result = build()
        except Exception:  # noqa: BLE001 - a lane we cannot read must never crash a draft
            # ...and must never read as "nothing changed" either: the diff is then unavailable.
            failed = True
            continue
        if name == "text":
            lane_entries, live_text = result
            entries.extend(lane_entries)
        else:
            entries.extend(result)
    mix = variant.get("mix") if isinstance(variant.get("mix"), Mapping) else {}
    for entry in (
        _scalar("audio", "music_level", "music level", mix.get("music_level"), state.mix_level)
        if state.mix_level is not None
        else None,
        _scalar(
            "audio",
            "original_level",
            "original sound",
            mix.get("original_level"),
            state.original_level,
        )
        if state.original_level is not None
        else None,
        _scalar("audio", "music_gain_db", "music volume", None, state.music_gain_db)
        if state.music_gain_db is not None
        else None,
        _scalar(
            "audio", "music_track", "music", variant.get("music_track_id"), state.music_track_id
        )
        if state.music_track_id
        else None,
        DiffEntry(
            "audio", "remove_music", (DiffTarget("music", "music", None, "music", None, None),)
        )
        if state.remove_music
        else None,
        _scalar("title", "title", "title", None, state.title)
        if state.title is not None
        and state.title
        not in {r.get("text") for r in before.text.values() if r.get("role") == "title"}
        else None,
    ):
        if entry is not None:
            entries.append(entry)
    for key, value in (state.caption_patch or {}).items():
        entries.append(
            DiffEntry(
                "caption_meta",
                str(key),
                (DiffTarget(str(key), "captions", None, "captions", None, value),),
            )
        )
    if state.visual_blocks is not None and "visual_media" in state.changed:
        entries.append(
            DiffEntry(
                "visual_media",
                "removed",
                (DiffTarget("visual", "visual", None, "visual items", None, None),),
            )
        )
    return EditorDiff(entries=tuple(entries), live_text_count=live_text, unavailable=failed)


def speech_cut_diff() -> EditorDiff:
    """A reviewed speech cut is applied whole by the worker; the bundle's one change."""
    return EditorDiff(
        entries=(
            DiffEntry(
                "speech_cut", "applied", (DiffTarget("cut", "speech", None, "speech", None, None),)
            ),
        )
    )


# ------------------------------------------------------------------------ describing


def _plural(count: int, noun: str, noun_tr: str) -> str:
    return say(
        en=f"{count} {noun}" if count == 1 else f"{count} {noun}s",
        tr=f"{count} {noun_tr}",
    )


def _quote(text: str) -> str:
    text = text.strip()
    return "'" + (text if len(text) <= 30 else text[:29].rstrip() + "…") + "'"


def describe_targets(entry: DiffEntry, live_text_count: int) -> str:
    """Who an entry touched: "all 3 texts", "the title", "'Lisbon'", "2 labels"."""
    targets = entry.targets
    count = len(targets)
    if entry.lane != "text":
        kinds = {t.kind for t in targets}
        if kinds == {"clip"}:
            if count == 1:
                number = re.sub(r"\D", "", targets[0].text)
                return say(en=f"clip {number}", tr=f"{number}. klip")
            return _plural(count, "clip", "klip")
        names = {
            "video": ("the video", "video"),
            "music": ("the music", "müzik"),
            "caption": ("the captions", "altyazılar"),
            "captions": ("the captions", "altyazılar"),
            "title": ("the title", "başlık"),
        }
        en, tr = names.get(targets[0].kind, (targets[0].text, targets[0].text))
        return say(en=en, tr=tr) if count == 1 else _plural(count, targets[0].kind, tr)
    if count == 2 and count == live_text_count:
        return say(en="both texts", tr="iki yazı")
    if count > 2 and count == live_text_count:
        return say(en=f"all {count} texts", tr=f"{count} yazının hepsi")
    if count == 1:
        only = targets[0]
        if only.kind == "title":
            return say(en="the title", tr="başlık")
        return _quote(only.text) if only.text else say(en="1 text", tr="1 yazı")
    kinds = {t.kind for t in targets}
    if kinds == {"label"}:
        return _plural(count, "label", "etiket")
    return _plural(count, "text", "yazı")


_ENTRANCE_EN = {
    "fade": "a fade-in",
    "pop": "a pop-in",
    "slide": "a slide-in",
    "typewriter": "a typewriter",
}
_ENTRANCE_TR = {"fade": "solma", "pop": "pop", "slide": "kayma", "typewriter": "daktilo"}
_CASE_EN = {
    "upper": "UPPERCASE",
    "lower": "lowercase",
    "title": "Title Case",
    "none": "its original case",
}
_CASE_TR = {
    "upper": "BÜYÜK HARF",
    "lower": "küçük harf",
    "title": "Baş Harfler Büyük",
    "none": "özgün harf",
}
_PLAIN_LABELS = {
    "highlight_color": ("highlight colour", "vurgu rengi"),
    "background_color": ("background", "arka plan"),
    "letter_spacing": ("letter spacing", "harf aralığı"),
    "line_spacing": ("line spacing", "satır aralığı"),
    "max_width_frac": ("text width", "yazı genişliği"),
    "stroke_width": ("outline", "kontur"),
    "shadow_enabled": ("shadow", "gölge"),
    "rotation_deg": ("rotation", "dönüş"),
    "behind_subject": ("behind-subject look", "özne arkası görünümü"),
    "exit": ("exit animation", "çıkış animasyonu"),
    "loop": ("looping animation", "döngü animasyonu"),
    "speed": ("animation speed", "animasyon hızı"),
}


def _common(entry: DiffEntry) -> Any:
    values = {repr(t.after) for t in entry.targets}
    return entry.targets[0].after if len(values) == 1 else None


def _where(x: float | None, y: float | None) -> tuple[str, str]:
    horizontal = "left" if (x or 0.5) < 0.34 else "right" if (x or 0.5) > 0.66 else "center"
    vertical = "top" if (y or 0.5) < 0.34 else "bottom" if (y or 0.5) > 0.66 else "middle"
    return horizontal, vertical


_SIDE_TR = {
    "left": "sola",
    "right": "sağa",
    "center": "ortaya",
    "top": "üste",
    "bottom": "alta",
    "middle": "ortaya",
}


def _describe_text(entry: DiffEntry, who: str) -> str:
    name, first = entry.field, entry.targets[0]
    common = _common(entry)
    if name == "wording":
        if len(entry.targets) == 1:
            return say(
                en=f"Changed {_quote(str(first.before or ''))} to {_quote(str(first.after or ''))}",
                tr=(
                    f"{_quote(str(first.before or ''))} yazısını "
                    f"{_quote(str(first.after or ''))} yaptım"
                ),
            )
        return say(en=f"Rewrote {who}", tr=f"{who} yeniden yazıldı")
    if name == "added":
        return say(en=f"Added {who}", tr=f"{who} eklendi")
    if name == "removed":
        return say(en=f"Removed {who}", tr=f"{who} kaldırıldı")
    if name == "entrance":
        if common in (None, "none"):
            if common == "none":
                return say(
                    en=f"Removed the entrance animation from {who}",
                    tr=f"{who} için giriş animasyonu kaldırıldı",
                )
            return say(
                en=f"Changed the entrance animation on {who}",
                tr=f"{who} için giriş animasyonu değişti",
            )
        return say(
            en=f"Added {_ENTRANCE_EN.get(str(common), 'an')} animation to {who}".replace(
                " animation animation", " animation"
            ),
            tr=f"{who} için {_ENTRANCE_TR.get(str(common), str(common))} animasyonu eklendi",
        )
    if name == "font_family":
        return say(
            en=f"Font → {_font_display(first)} on {who}",
            tr=f"{who} için yazı tipi → {_font_display(first)}",
        )
    if name == "color":
        hexa = str(common) if isinstance(common, str) else None
        family = color_family(hexa)
        shown = f"{family} ({hexa})" if family in ("white", "black") and hexa else (hexa or "")
        if not shown:
            return say(en=f"Changed the colour of {who}", tr=f"{who} rengi değişti")
        return say(en=f"Colour → {shown} on {who}", tr=f"{who} için renk → {shown}")
    if name == "size":
        old, new = _num(first.before), _num(first.after)
        if len(entry.targets) == 1 and old is not None and new is not None:
            word_en, word_tr = ("bigger", "büyüttüm") if new > old else ("smaller", "küçülttüm")
            return say(
                en=f"Made {who} {word_en} ({old:g}→{new:g}px)",
                tr=f"{who} {word_tr} ({old:g}→{new:g}px)",
            )
        return say(en=f"Resized {who}", tr=f"{who} yeniden boyutlandı")
    if name == "position":
        afters = [t.after for t in entry.targets if isinstance(t.after, tuple)]
        befores = [t.before for t in entry.targets if isinstance(t.before, tuple)]
        named = afters[0][0] if afters and afters[0][0] not in (None, "custom") else None
        if named and len({a[0] for a in afters}) == 1:
            spot_tr = {"top": "üst", "middle": "orta", "bottom": "alt"}.get(str(named), str(named))
            return say(en=f"Moved {who} to the {named}", tr=f"{who} {spot_tr} konuma taşındı")
        xs = {a[1] for a in afters}
        height_kept = len(afters) == len(befores) and all(
            a[0] == "custom" and b[0] == "custom" and a[2] == b[2]
            for a, b in zip(afters, befores, strict=True)
        )
        if len(xs) == 1 and height_kept and isinstance(next(iter(xs)), float):
            horizontal, _vertical = _where(next(iter(xs)), None)
            return say(
                en=f"Lined up {who} on the {horizontal}",
                tr=f"{who} {_SIDE_TR[horizontal]} hizalandı",
            )
        return say(en=f"Moved {who}", tr=f"{who} taşındı")
    if name == "alignment":
        side = str(common) if isinstance(common, str) else None
        if side:
            return say(
                en=f"Aligned {who} to the {side}", tr=f"{who} {_SIDE_TR.get(side, side)} hizalandı"
            )
        return say(en=f"Changed the alignment of {who}", tr=f"{who} hizalaması değişti")
    if name == "text_case":
        return say(
            en=f"Set {who} to {_CASE_EN.get(str(common), 'a new case')}",
            tr=f"{who} → {_CASE_TR.get(str(common), 'yeni harf biçimi')}",
        )
    if name == "timing":
        if (
            len(entry.targets) == 1
            and isinstance(first.before, tuple)
            and isinstance(first.after, tuple)
        ):

            def span(value: tuple) -> str:
                return (
                    "–".join(f"{v:g}" if isinstance(v, (int, float)) else "?" for v in value) + "s"
                )

            return say(
                en=f"Retimed {who} ({span(first.before)} → {span(first.after)})",
                tr=f"{who} zamanlaması değişti ({span(first.before)} → {span(first.after)})",
            )
        return say(en=f"Retimed {who}", tr=f"{who} zamanlaması değişti")
    en, tr = _PLAIN_LABELS.get(name, (name.replace("_", " "), name.replace("_", " ")))
    return say(en=f"Changed the {en} of {who}", tr=f"{who} için {tr} değişti")


def _font_display(target: DiffTarget) -> str:
    return str(target.after) if isinstance(target.after, str) else "new font"


def describe_entry(entry: DiffEntry, *, live_text_count: int = 0) -> str:
    """One short sentence for an entry, in the turn's language."""
    who = describe_targets(entry, live_text_count)
    lane, name, first = entry.lane, entry.field, entry.targets[0]
    if lane == "text":
        return _describe_text(entry, who)
    if lane == "timeline":
        if name == "duration_s":
            common = _common(entry)
            if isinstance(common, float):
                return say(en=f"Set {who} to {common:g}s", tr=f"{who} {common:g} sn yapıldı")
            return say(en=f"Changed the length of {who}", tr=f"{who} süresi değişti")
        if name == "removed":
            return say(en=f"Removed {who}", tr=f"{who} kaldırıldı")
        if name == "added":
            return say(en=f"Added {who}", tr=f"{who} eklendi")
        if name == "order":
            if len(entry.targets) == 1 or not all(isinstance(t.after, int) for t in entry.targets):
                return say(en="Reordered the clips", tr="Klipler yeniden sıralandı")
            return say(en="Reordered the clips", tr="Klipler yeniden sıralandı")
        if name == "in_s":
            return say(en=f"Trimmed {who}", tr=f"{who} kırpıldı")
        if name == "look_preset":
            return say(en=f"Changed the look of {who}", tr=f"{who} görünümü değişti")
        if name == "playback_rate":
            return say(en=f"Changed the speed of {who}", tr=f"{who} hızı değişti")
        if name == "source_crop":
            return say(en=f"Re-cropped {who}", tr=f"{who} yeniden kırpıldı")
        if name == "total_duration":
            return say(
                en=f"Video length {first.before:g}s → {first.after:g}s",
                tr=f"Video süresi {first.before:g} sn → {first.after:g} sn",
            )
    if lane == "transition":
        value = _common(entry)
        kind = value[0] if isinstance(value, tuple) and value else None
        return say(
            en=f"Set the transition after {who}" + (f" to {kind}" if isinstance(kind, str) else ""),
            tr=f"{who} sonrası geçiş değişti" + (f" → {kind}" if isinstance(kind, str) else ""),
        )
    if lane == "audio":
        if name == "remove_music":
            return say(en="Removed the music", tr="Müzik kaldırıldı")
        if name == "music_track":
            return say(en="Changed the music", tr="Müzik değişti")
        label = {
            "music_level": ("Music", "Müzik"),
            "original_level": ("Original sound", "Orijinal ses"),
        }.get(name, ("Music volume", "Müzik sesi"))
        unit = " dB" if name == "music_gain_db" else "%"

        def level(value: Any) -> str:
            number = _num(value)
            if number is None:
                return "?"
            return (
                f"{number:g}"
                if unit == " dB"
                else f"{round(number * 100 if number <= 1 else number):g}"
            )

        before = f"{level(first.before)}{unit} → " if first.before is not None else "→ "
        return say(
            en=f"{label[0]} {before}{level(first.after)}{unit}",
            tr=f"{label[1]} {before}{level(first.after)}{unit}",
        )
    if lane == "title":
        return say(
            en=f"Title → {_quote(str(first.after or ''))}",
            tr=f"Başlık → {_quote(str(first.after or ''))}",
        )
    if lane == "caption_meta":
        return say(en=f"Changed the caption {name.replace('_', ' ')}", tr="Altyazı ayarı değişti")
    if lane == "captions":
        return say(
            en=f"Edited {_plural(len(entry.targets), 'caption', 'altyazı')}",
            tr=f"{_plural(len(entry.targets), 'caption', 'altyazı')} düzenlendi",
        )
    if lane in ("sfx", "camera_effects"):
        noun = (
            ("sound effect", "ses efekti") if lane == "sfx" else ("camera effect", "kamera efekti")
        )
        verb = {"added": ("Added", "eklendi"), "removed": ("Removed", "kaldırıldı")}.get(
            name, ("Changed", "değişti")
        )
        return say(
            en=f"{verb[0]} {_plural(len(entry.targets), noun[0], noun[1])}",
            tr=f"{_plural(len(entry.targets), noun[0], noun[1])} {verb[1]}",
        )
    if lane == "visual_media":
        return say(en="Removed visual items", tr="Görsel öğeler kaldırıldı")
    if lane == "speech_cut":
        return say(en="Applied the speech cut", tr="Konuşma kesimi uygulandı")
    return say(en=f"Changed {name.replace('_', ' ')}", tr=f"{name.replace('_', ' ')} değişti")


def describe_diff(
    entries: Iterable[DiffEntry], *, live_text_count: int = 0, limit: int = 4
) -> list[str]:
    """Short sentences for the deliberate entries (derived side effects are left out)."""
    out: list[str] = []
    for entry in entries:
        if entry.derived or not entry.targets:
            continue
        phrase = describe_entry(entry, live_text_count=live_text_count)
        if phrase not in out:
            out.append(phrase)
    return out[:limit]
