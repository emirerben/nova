"""KRI-815: a footage-grounded voiceover script in Kria chat, before any recording exists.

With clips attached and no voiceover, a creator asking for "a narration script for these
clips" used to get either a generic question or a narrated draft that the approval claim then
cancelled with ``voiceover_required`` (Troya T03, finding F08). This module is the single
owner of the reply that replaces both:

* the script is written by the existing ``nova.voiceover.script_writer`` agent from the
  clips' STORED understanding (every attached clip, in play order; no new vision call);
* it is persisted on the reply event as ``voiceover_script`` with a stable ``script_id``,
  a per-thread ``revision`` and the ``source_media_ids`` it was written from, plus the
  ``record_or_import`` continuation the app turns into "Record with this script";
* nothing here mints an approval, a draft or a Job, and nothing here touches the brief,
  the item's saved edits or its media. The script is spoken words: it never becomes
  overlay/caption text and never creator intent;
* a writer failure is an honest ``status: "failed"`` reply with no ``text``, never a
  refusal sentence presented as a script.

Claims the inputs do not back are listed in ``needs_check``: the writer's own list plus a
deterministic backstop for numbers and mid-sentence capitalised names absent from the inputs.
"""

from __future__ import annotations

import asyncio
import hashlib
import string
import uuid
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Any

import structlog
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.agents._model_client import default_client
from app.agents._runtime import ProviderOutcomeUnknownError, RunContext
from app.agents.voiceover_script_writer import (
    ScriptCheck,
    VoiceoverScriptWriterAgent,
    VoiceoverScriptWriterInput,
)
from app.config import settings
from app.kria.contracts import KriaTurnPlan
from app.kria.reply_language import current_reply_language, say
from app.models import CreationThreadEvent
from app.services.clip_intent_resolution import IntentClip
from app.services.clip_understanding import clip_record

log = structlog.get_logger()

SCRIPT_PAYLOAD_VERSION = 1
CONTINUATION = "record_or_import"
# The writer's own per-attempt timeout is 25 s with up to 3 attempts; inside a chat turn the
# turn deadline bounds it further (see `draft_script`).
_DEFAULT_DURATION_S = 30.0
_MAX_SCRIPT_CLIPS = 24
_PUNCT = string.punctuation + "“”‘’«»…—–"


@dataclass(frozen=True)
class PriorScript:
    script_id: str
    revision: int
    text: str
    source_media_ids: tuple[str, ...]


def available(manifest: Any, *, voiceover_attached: bool) -> bool:
    """A script can be drafted: flag on, at least one attached video clip, no voiceover."""
    if not settings.kria_voiceover_script_enabled or voiceover_attached:
        return False
    return any(getattr(media, "kind", None) == "video" for media in manifest.media)


async def latest_script(db: AsyncSession, thread_id: uuid.UUID) -> PriorScript | None:
    """The newest drafted script in this thread (failed replies carry no text and are skipped)."""
    rows = (
        await db.execute(
            select(CreationThreadEvent.payload)
            .where(
                CreationThreadEvent.thread_id == thread_id,
                CreationThreadEvent.role == "assistant",
                CreationThreadEvent.event_type == "assistant_response",
            )
            .order_by(CreationThreadEvent.sequence.desc())
            .limit(200)
        )
    ).scalars()
    for payload in rows:
        script = payload.get("voiceover_script") if isinstance(payload, dict) else None
        if not isinstance(script, dict) or script.get("status") != "draft":
            continue
        text = script.get("text")
        if not isinstance(text, str) or not text.strip():
            continue
        return PriorScript(
            script_id=str(script.get("script_id") or ""),
            revision=int(script.get("revision") or 1),
            text=text,
            source_media_ids=tuple(str(m) for m in script.get("source_media_ids") or ()),
        )
    return None


def _clip_line(
    number: int, clip: IntentClip, duration_s: float | None, *, include_facts: bool
) -> str:
    """One clip's stored understanding as one prompt line ("Clip 2 (6s): ...")."""
    record = clip_record(clip.analysis, kind=clip.kind)
    parts: list[str] = []
    for value in (record.subject, record.summary, record.setting, record.activity):
        if value and value not in parts:
            parts.append(value)
    if record.on_screen_text:
        parts.append(f"text visible on screen: {record.on_screen_text}")
    if record.speech.transcript:
        parts.append(f"said in the clip: {record.speech.transcript[:200]}")
    moments = [note.description for note in record.notable_moments if note.description][:3]
    if moments:
        parts.append("moments: " + "; ".join(moments))
    if include_facts:
        for fact in record.facts:
            # A model's guess ("inferred") is never handed on as a fact to say aloud.
            if fact.kind in {"place", "landmark", "creator"} and fact.provenance != "inferred":
                parts.append(f"filmed at {fact.value}")
    seconds = f" ({round(duration_s)}s)" if duration_s else ""
    body = ". ".join(parts) if parts else "no description available"
    return f"Clip {number}{seconds}: {body}"


def footage_lines(
    manifest: Any, clips: Sequence[IntentClip], *, include_facts: bool = False
) -> tuple[list[str], tuple[str, ...], float]:
    """Per-clip prompt lines for every attached VIDEO clip in the manifest's (play) order,
    the media ids they came from, and the total footage seconds (sizes the script)."""
    by_id = {clip.media_id: clip for clip in clips}
    lines: list[str] = []
    ids: list[str] = []
    total = 0.0
    for media in manifest.media:
        if getattr(media, "kind", None) != "video" or len(ids) >= _MAX_SCRIPT_CLIPS:
            continue
        clip = by_id.get(media.media_id) or IntentClip(
            media_id=media.media_id, kind="video", analysis=None
        )
        duration = getattr(media, "duration_s", None)
        lines.append(_clip_line(len(ids) + 1, clip, duration, include_facts=include_facts))
        ids.append(media.media_id)
        total += float(duration or 0.0)
    return lines, tuple(ids), (total or _DEFAULT_DURATION_S)


def _tokens(text: str) -> list[str]:
    return [word.strip(_PUNCT) for word in text.split()]


def unbacked_claims(text: str, sources: Sequence[str]) -> list[ScriptCheck]:
    """Deterministic backstop: numbers (years, dates, counts) and mid-sentence capitalised
    names in the script that appear nowhere in the footage lines or the creator's words.

    Exact token comparison only (no folding, no word lists): a token the inputs contain is
    grounded, anything else is surfaced for the creator to check, never silently kept.
    """
    grounded = {token for source in sources for token in _tokens(source) if token}
    checks: list[ScriptCheck] = []
    words = text.split()
    for index, word in enumerate(words):
        token = word.strip(_PUNCT)
        if not token or token in grounded:
            continue
        sentence_start = index == 0 or words[index - 1].rstrip("”’\"')").endswith((".", "!", "?"))
        has_digit = any(ch.isdigit() for ch in token)
        proper = (
            not sentence_start
            and len(token) > 2
            and token[0].isupper()
            and token[1:2].islower()
            and "'" not in token
            and "’" not in token
        )
        if not (has_digit or proper) or any(c.phrase == token for c in checks):
            continue
        checks.append(
            ScriptCheck(
                phrase=token,
                reason=say(
                    en="Not in your clips or your message.",
                    tr="Kliplerinde ya da mesajında yok.",
                ),
            )
        )
    return checks[:8]


def _writer_client():  # noqa: ANN202 - the shared model client (tests swap a cassette in)
    return default_client()


def _script_id(thread_id: uuid.UUID, source_media_ids: Sequence[str]) -> str:
    digest = hashlib.sha256(f"{thread_id}:{','.join(source_media_ids)}".encode()).hexdigest()
    return f"vos-{digest[:24]}"


def _reply(status: str, *, revision: int, checks: Sequence[ScriptCheck]) -> str:
    if status != "draft":
        return say(
            en=(
                "I couldn't write a script from your clips right now. Ask me again, "
                "or record or import your own voiceover and I'll plan the edit around it."
            ),
            tr=(
                "Şu anda kliplerinden bir metin yazamadım. Tekrar sor ya da kendi "
                "seslendirmeni kaydet veya içe aktar, düzenlemeyi ona göre planlayayım."
            ),
        )
    lead = (
        say(
            en="Here's the updated script, written from what your clips show.",
            tr="Kliplerinde görünenlere göre güncellenmiş metin burada.",
        )
        if revision > 1
        else say(
            en="Here's a script written from what your clips show.",
            tr="Kliplerinde görünenlere göre yazılmış bir metin burada.",
        )
    )
    tail = say(
        en="Record it with “Record with this script” or import your own voiceover, "
        "then I'll plan the narrated edit around it.",
        tr="“Bu metinle kaydet” ile kaydet ya da kendi seslendirmeni içe aktar, "
        "sonra anlatımlı düzenlemeyi ona göre planlayayım.",
    )
    if not checks:
        return f"{lead} {tail}"
    listed = ", ".join(f"“{check.phrase}”" for check in checks)
    check_line = say(
        en=f"Check before recording, I couldn't confirm these from your clips: {listed}.",
        tr=f"Kaydetmeden önce kontrol et, bunları kliplerinden doğrulayamadım: {listed}.",
    )
    return f"{lead} {check_line} {tail}"


async def draft_script(
    *,
    thread_id: uuid.UUID,
    item_id: uuid.UUID,
    creator_id: uuid.UUID,
    manifest: Any,
    clips: Sequence[IntentClip],
    creator_words: Sequence[str],
    prior: PriorScript | None,
    revision_note: str | None,
    deadline_monotonic: float | None,
) -> KriaTurnPlan:
    """Write (or revise) the script and return the respond plan carrying it.

    ``creator_words`` are the creator's own messages (oldest first); they are the brief and
    the grounding the backstop compares against. ``prior`` + ``revision_note`` make this a
    revision: ``revision + 1``, same ``script_id`` unless the footage changed.
    """
    lines, media_ids, duration = footage_lines(
        manifest, clips, include_facts=settings.clip_facts_for(creator_id)
    )
    revising = prior is not None and bool(revision_note)
    # Provenance is always the footage this revision is written from; changed footage
    # mints a new script_id so a stale draft is never mistaken for the current one.
    same_footage = prior is not None and tuple(prior.source_media_ids) == tuple(media_ids)
    script_id = (prior.script_id if revising and same_footage and prior else "") or _script_id(
        thread_id, media_ids
    )
    revision = (prior.revision + 1) if prior is not None else 1
    language = current_reply_language()
    brief = "\n".join(word for word in creator_words if word)[-1500:]
    agent_input = VoiceoverScriptWriterInput(
        brief=brief,
        footage_clips=lines,
        target_duration_s=max(5.0, min(duration, 180.0)),
        reply_language=language,
        previous_script=prior.text if revising and prior is not None else "",
        revision_note=(revision_note or "") if revising else "",
    )
    payload: dict[str, Any] = {
        "version": SCRIPT_PAYLOAD_VERSION,
        "script_id": script_id,
        "revision": revision,
        "item_id": str(item_id),
        "source_media_ids": list(media_ids),
        "language": language or "en",
        "continuation": CONTINUATION,
    }

    def _run():  # noqa: ANN202 - VoiceoverScriptWriterOutput
        return VoiceoverScriptWriterAgent(_writer_client()).run(
            agent_input,
            ctx=RunContext(
                request_id=str(thread_id),
                creator_id=str(creator_id),
                plan_item_id=str(item_id),
                deadline_monotonic=deadline_monotonic,
            ),
        )

    try:
        output = await asyncio.to_thread(_run)
    except ProviderOutcomeUnknownError:
        raise
    except Exception as exc:  # noqa: BLE001 - any writer failure is the honest fallback
        log.warning(
            "kria_voiceover_script_failed", thread_id=str(thread_id), error_type=type(exc).__name__
        )
        payload.update(status="failed", revision=prior.revision if prior else 0, needs_check=[])
        return KriaTurnPlan(
            mode="respond",
            turn_value="question",
            response=_reply("failed", revision=0, checks=()),
            voiceover_script=payload,
            diagnostics={"code": "voiceover_script_failed", "clips": len(media_ids)},
        )
    checks = list(output.needs_check)
    for check in unbacked_claims(output.text, [*lines, *creator_words]):
        if not any(check.phrase in existing.phrase for existing in checks):
            checks.append(check)
    checks = checks[:8]
    payload.update(
        status="draft",
        text=output.text,
        lines=list(output.lines),
        needs_check=[check.model_dump() for check in checks],
    )
    log.info(
        "kria_voiceover_script_drafted",
        thread_id=str(thread_id),
        revision=revision,
        clips=len(media_ids),
        checks=len(checks),
    )
    return KriaTurnPlan(
        mode="respond",
        turn_value="question",
        response=_reply("draft", revision=revision, checks=checks),
        voiceover_script=payload,
    )


def record_first_plan() -> KriaTurnPlan:
    """No script possible (flag off, or no video footage): ask for the voiceover itself."""
    return KriaTurnPlan(
        mode="respond",
        turn_value="question",
        response=say(
            en=(
                "A narrated edit is built around your voiceover. Record or import it first, "
                "then I'll plan the edit around it."
            ),
            tr=(
                "Anlatımlı düzenleme seslendirmen üzerine kurulur. Önce onu kaydet veya içe "
                "aktar, sonra düzenlemeyi ona göre planlayayım."
            ),
        ),
    )


def event_content(plan: KriaTurnPlan) -> str | None:
    """The reply text old clients read: the short reply, then the script itself as prose."""
    script = plan.voiceover_script or {}
    text = script.get("text") if script.get("status") == "draft" else None
    if not text:
        return plan.response
    return f"{plan.response}\n\n{text}" if plan.response else text
