"""The clips' own sound in a phone montage: what it decides, and what the reply says (KRI-585).

A phone montage with no song, no voiceover and no named voice source plays every
clip's own sound (``guided_story.plan_preserves_source_audio``), whatever the
strategy's ``audio_strategy`` says: ``licensed_music`` only becomes a song suggestion
for the platform. Prod thread 7CCBDE0A (cabin kit M1) replied about labels and order,
never said the creator's narration would be heard, and the order ignored what it said.

``spoken_plan`` is where the clips' words may decide anything: only on that route, and
only in an order nobody chose (no filming- or upload-order ask, no grouping answer, no
pinned contract order). It is ``services.spoken_roles.spoken_seats`` over the clips the
dispatch keeps, so the length plan (``kria/planner``) seats the same windows and the
montage planner (``pipeline/unified_montage``) lays out the same order.

The planner records that plan on the approved length plan (``spoken_order``), and the
phone worker reorders only when the record is there (``speech_order_approved``), so the
render never moves a clip the reply did not mention (KRI-129: no silent overrides).

``narration_note_parts`` is the reply's deterministic copy, read off that plan and the
approved length plan's windows. It says something only when the clips' words changed
the edit, so every other reply is unchanged. Kill switch: ``SPOKEN_ORDER_ENABLED``.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Any

from app.agents._schemas.edit_format import GUIDED_EDIT_FORMATS, coerce_edit_format
from app.config import settings
from app.kria.reply_language import say
from app.services.clip_order_sequence import SequenceRow, sequence_rows
from app.services.spoken_roles import (
    SpokenClip,
    SpokenLine,
    SpokenSeats,
    speech_of,
    spoken_seats,
    window_inside,
)

_QUOTE_MAX_CHARS = 60


def _field(strategy: object, name: str) -> Any:
    if isinstance(strategy, Mapping):
        return strategy.get(name)
    return getattr(strategy, name, None)


def keeps_camera_sound(manifest: Any, strategy: object) -> bool:
    """Will this strategy render as a phone montage that plays every clip's own sound?"""
    capability = (getattr(manifest, "capabilities", None) or {}).get("phone_source_audio")
    if capability is None or not getattr(capability, "available", False):
        return False
    if getattr(manifest, "has_voiceover", False):
        return False
    return strategy_keeps_camera_sound(strategy)


def strategy_keeps_camera_sound(strategy: object) -> bool:
    """The strategy half of ``keeps_camera_sound``: a guided montage with no song, no
    voiceover, no voice mode and no named voice source."""
    if coerce_edit_format(_field(strategy, "edit_format")) not in GUIDED_EDIT_FORMATS:
        return False
    if _field(strategy, "audio_strategy") in ("voiceover", "user_song"):
        return False
    if _field(strategy, "voice_mode") is not None:
        return False
    if _field(strategy, "execution_contract") is not None:
        return False
    audio = _field(strategy, "montage_audio")
    if audio is None:
        return True
    # "Keep the original sound" with no clip named is still every clip's own sound; a
    # named voice source plays only those clips (a different writer).
    return _field(audio, "preserve_source_audio") is True and not _field(audio, "source_media_ids")


def speech_order_approved(
    strategy: object, contract: Any, approved_length_plan: Mapping[str, Any] | None
) -> bool:
    """May the phone worker let the clips' words order this montage?

    Only what the reply already said: the approved length plan carries the planner's
    ``spoken_order`` record (camera-sound route, an order nobody chose), the strategy
    still keeps every clip's sound, and no pinned contract order (the phone verifier
    checks that order cut for cut).
    """
    if not settings.spoken_order_enabled:
        return False
    if contract is not None and getattr(contract, "order_required", False):
        return False
    if not isinstance(approved_length_plan, Mapping) or not approved_length_plan.get(
        "spoken_order"
    ):
        return False
    return strategy_keeps_camera_sound(strategy)


def kept_clips(strategy: object, media_snapshot: Mapping[str, Any] | None) -> list[SpokenClip]:
    """The footage clips the dispatch renders, in attachment order."""
    rows = (media_snapshot or {}).get("clip_assignments")
    videos = [
        row
        for row in (rows if isinstance(rows, list) else [])
        if isinstance(row, Mapping)
        and isinstance(row.get("media_id"), str)
        and row.get("kind", "video") == "video"
    ]
    selected = {str(value) for value in _field(strategy, "selected_media_ids") or ()}
    if _field(strategy, "media_scope") == "selected" and selected:
        videos = [row for row in videos if row["media_id"] in selected] or videos
    clips: list[SpokenClip] = []
    for row in videos:
        analysis = row.get("analysis")
        duration = row.get("duration_s")
        clips.append(
            SpokenClip(
                row["media_id"],
                analysis if isinstance(analysis, Mapping) else {},
                float(duration)
                if isinstance(duration, int | float) and not isinstance(duration, bool)
                else 0.0,
            )
        )
    return clips


def _sequence(strategy: object, clip_intents_enabled: bool) -> list[SequenceRow]:
    return sequence_rows(_field(strategy, "resolved_clip_intents")) if clip_intents_enabled else []


def speech_decides_order(strategy: object, brief: Any, contract: Any) -> bool:
    """Is the montage order nobody's choice, so what the clips say may set it?"""
    from app.pipeline.unified_montage import brief_view  # noqa: PLC0415 - pipeline imports kria

    view = brief_view(brief)
    # A dropped filming-order ask still orders the montage (`BriefView.kept_filming_order`).
    if view.order_by_capture or view.order_basis_stated or view.kept_filming_order:
        return False
    if _field(strategy, "ordering_choice") in ("group_first", "chronological"):
        return False
    return not (contract is not None and getattr(contract, "order_required", False))


def _holds_closing_words(strategy: object, closing_quote: Mapping[str, Any] | None) -> bool:
    if _field(strategy, "audio_strategy") == "original_audio":
        return True
    audio = _field(strategy, "montage_audio")
    if audio is not None and _field(audio, "preserve_source_audio") is True:
        return True
    return isinstance(closing_quote, Mapping) and closing_quote.get("status") == "found"


def spoken_plan(
    strategy: object,
    media_snapshot: Mapping[str, Any] | None,
    brief: Any,
    *,
    contract: Any,
    clip_intents_enabled: bool,
    closing_quote: Mapping[str, Any] | None = None,
) -> SpokenSeats | None:
    """KRI-585: the order and windows the clips' own words set, or None when they set none.

    The caller has checked ``keeps_camera_sound``. None also when the order is someone's
    choice (``speech_decides_order``) or no kept clip has a spoken role.
    """
    if not speech_decides_order(strategy, brief, contract):
        return None
    seats = spoken_seats(
        kept_clips(strategy, media_snapshot),
        _sequence(strategy, clip_intents_enabled),
        hold_closing_words=_holds_closing_words(strategy, closing_quote),
    )
    return seats if seats.lines else None


def _quote(line: SpokenLine) -> str:
    text = " ".join(line.text.split()).strip(" ,;:.!?…")
    if len(text) > _QUOTE_MAX_CHARS:
        text = text[:_QUOTE_MAX_CHARS].rsplit(" ", 1)[0].rstrip(" ,;:") + "…"
    return f"“{text}”"


def _windows(length_plan: object, media_id: str) -> list[tuple[float, float]]:
    rows = length_plan.get("windows") if isinstance(length_plan, Mapping) else None
    out: list[tuple[float, float]] = []
    for row in rows if isinstance(rows, list) else []:
        if isinstance(row, Mapping) and row.get("media_id") == media_id:
            start, end = row.get("source_start_s"), row.get("source_end_s")
            if isinstance(start, int | float) and isinstance(end, int | float):
                out.append((float(start), float(end)))
    return out


def narration_note_parts(
    strategy: object,
    media_snapshot: Mapping[str, Any] | None,
    seats: SpokenSeats | None,
    *,
    clip_intents_enabled: bool,
) -> list[str]:
    """The reply's sentences about the clips' sound, the sound sentence first.

    The caller has checked ``keeps_camera_sound``; ``seats`` is ``spoken_plan`` for the
    same strategy and snapshot (None: the clips' words decide nothing). ``media_snapshot``
    carries the approved ``length_plan``, whose windows the ending sentence is read off.
    The sound sentence leads only when the words changed the edit (a clip moved, or the
    ending was chosen around them): background chatter alone says nothing.
    """
    if seats is None:
        return []
    clips = kept_clips(strategy, media_snapshot)
    if not any(speech_of(clip.analysis) is not None for clip in clips):
        return []
    parts: list[str] = []
    rows = _sequence(strategy, clip_intents_enabled)
    resolved = [row for row in rows if row[3] == "resolved" and row[2]]
    seated = {member for row in resolved for member in row[2]}
    leads = any(position == "first" for position, *_rest in resolved)
    ends = any(position == "last" for position, *_rest in resolved)
    if seats.opening is not None:
        quote = _quote(seats.lines[seats.opening[0]])
        parts.append(
            say(
                en=f"The clip where you say {quote} comes right after your opening shot."
                if leads
                else f"The clip where you say {quote} opens the video.",
                tr=f"{quote} dediğin klip açılış çekiminden hemen sonra geliyor."
                if leads
                else f"{quote} dediğin klip videoyu açıyor.",
            )
        )
    sign_off = next(
        (
            clip_id
            for clip_id in reversed(seats.order)
            if clip_id not in seated
            and (line := seats.lines.get(clip_id)) is not None
            and line.role == "closing"
        ),
        None,
    )
    if sign_off is not None:
        quote = _quote(seats.lines[sign_off])
        parts.append(
            say(
                en=f"The clip where you say {quote} comes right before your last shot."
                if ends
                else f"The clip where you say {quote} closes the video.",
                tr=f"{quote} dediğin klip son çekiminden hemen önce geliyor."
                if ends
                else f"{quote} dediğin klip videoyu kapatıyor.",
            )
        )
    closing = seats.order[-1] if seats.order else None
    line = seats.lines.get(closing) if closing is not None else None
    pinned_last = closing is not None and any(
        position == "last" and closing in members for position, _name, members, _s in resolved
    )
    if line is None or line.role != "introduction" or not pinned_last:
        return _with_sound_sentence(parts)
    quote = _quote(line)
    length_plan = (media_snapshot or {}).get("length_plan")
    windows = _windows(length_plan, closing)
    quiet = seats.quiet_close
    # Claimed only when the approved windows stay off the words (or, with none approved,
    # the montage planner seats the same quiet stretch itself).
    quiet_held = quiet is not None and (
        all(window_inside(window, quiet[1]) for window in windows)
        if windows
        else not (isinstance(length_plan, Mapping) and length_plan.get("window_eligible"))
    )
    if quiet_held:
        parts.append(
            say(
                en=f"Your last clip introduces someone ({quote}) with nothing before it, "
                "so I end on that clip without those words. Tell me if you want the "
                "line heard.",
                tr=f"Son klibin birini tanıtıyor ({quote}) ama öncesinde bir şey yok; "
                "bu yüzden videoyu o klipte bu sözler olmadan bitiriyorum. Cümlenin "
                "duyulmasını istersen söyle.",
            )
        )
    else:
        parts.append(
            say(
                en=f"Your last clip introduces someone ({quote}), so the video ends on "
                "that introduction. Tell me if it should come earlier.",
                tr=f"Son klibin birini tanıtıyor ({quote}); video bu tanıtımla bitiyor. "
                "Daha önce gelmesini istersen söyle.",
            )
        )
    return _with_sound_sentence(parts)


def _with_sound_sentence(parts: list[str]) -> list[str]:
    if not parts:
        return []
    return [
        say(
            en="Your clips keep their own sound, so your narration plays in the video.",
            tr="Kliplerin kendi sesini koruyorum, yani anlatımın videoda duyulacak.",
        ),
        *parts,
    ]


def with_note(summary: str, parts: Sequence[str], *, budget: int) -> str:
    """``summary`` plus as many of ``parts`` as fit ``budget`` (dropped from the end)."""
    kept = list(parts)
    base = summary.strip()
    while kept and len(" ".join([base, *kept])) > budget:
        kept.pop()
    return " ".join([base, *kept]).strip()
