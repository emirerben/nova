"""Durable, server-owned approval for AI-suggested on-screen copy (KRI-506).

The model may suggest words, but never records their approval.  A suggestion is
persisted in an assistant ``choice_question`` and becomes usable only when the
thread contains the matching, server-validated ``choice_selection``.
"""

from __future__ import annotations

import hashlib
import json
import unicodedata
import uuid
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from typing import Any, Literal

from app.kria.reply_language import current_reply_language

CREATIVE_COPY_CONFLICT = "creative_copy"
OPT_WRITE_MY_OWN = "write_my_own"
OPT_GENERATE = "generate"
OPT_APPROVE = "approve"
OPT_REVISE = "revise"
OPT_CANCEL = "cancel"

Events = Iterable[tuple[str, Mapping[str, Any] | None]]
Target = Literal["opening_title", "closing_title"]


def normalize_copy(value: object) -> str:
    return " ".join(unicodedata.normalize("NFC", str(value)).split())


def media_digest(media_snapshot: Mapping[str, Any] | None) -> str:
    """Stable dependency key: owned media identity, never chat/context revision."""
    from app.kria.brief_binding import media_identity  # avoids a service import cycle

    raw = json.dumps(
        media_identity(dict(media_snapshot or {})),
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )
    return hashlib.sha256(raw.encode()).hexdigest()[:24]


# Typed answers that decline the copy (exact match after normalisation). Turkish ones
# are accepted in every chat language: a reply may arrive in Turkish before the chat's
# language is known.
_SKIP_ALIASES: tuple[str, ...] = (
    "no",
    "none",
    "nope",
    "skip",
    "skip title",
    "no title",
    "no title please",
    "don't add a title",
    "without title",
    "without a title",
    "leave it off",
    "başlık olmasın",
    "başlıksız",
    "continue without a title",
    "hayır",
    "hayır teşekkürler",
    # No bare "yok": to "Aklında bir fikir var mı, yoksa ben mi yazayım?" it means
    # "I don't have one" (so write it), not "skip the title".
    "atla",
    "geç",
    "başlık yok",
    "başlık istemiyorum",
    "başlık istemem",
    "başlığa gerek yok",
    "başlık koyma",
    "başlık ekleme",
    "başlıksız devam et",
    "gerek yok",
)


def authorship_question(*, target: Target, dependency_digest: str) -> dict[str, Any]:
    question = _authorship_question(target=target, dependency_digest=dependency_digest)
    return _localize_for_current_chat(question)


def _authorship_question(*, target: Target, dependency_digest: str) -> dict[str, Any]:
    return {
        "version": 1,
        "question_id": str(uuid.uuid4()),
        "conflict": CREATIVE_COPY_CONFLICT,
        "kind": "creative_copy_authorship",
        "creative_target": target,
        "dependency_digest": dependency_digest,
        "allow_free_text": True,
        "options": [
            {"key": OPT_WRITE_MY_OWN, "label": "I have an idea", "recommended": False},
            {"key": OPT_GENERATE, "label": "Generate one", "recommended": False},
            {
                "key": OPT_CANCEL,
                "label": "Skip it",
                "recommended": False,
                "aliases": list(_SKIP_ALIASES),
            },
        ],
    }


def wording_question(*, target: Target, candidate: str, dependency_digest: str) -> dict[str, Any]:
    question = _wording_question(
        target=target, candidate=candidate, dependency_digest=dependency_digest
    )
    return _localize_for_current_chat(question)


def _wording_question(*, target: Target, candidate: str, dependency_digest: str) -> dict[str, Any]:
    candidate = normalize_copy(candidate)
    return {
        "version": 1,
        "question_id": str(uuid.uuid4()),
        "conflict": CREATIVE_COPY_CONFLICT,
        "kind": "creative_copy_wording",
        "creative_target": target,
        "candidate": candidate,
        "candidate_digest": hashlib.sha256(candidate.encode()).hexdigest()[:24],
        "dependency_digest": dependency_digest,
        "allow_free_text": True,
        "options": [
            {"key": OPT_APPROVE, "label": "Use this wording", "recommended": False},
            {"key": OPT_REVISE, "label": "Revise it", "recommended": False},
            {
                "key": OPT_CANCEL,
                "label": "Skip it",
                "recommended": False,
                "aliases": list(_SKIP_ALIASES),
            },
        ],
    }


# Only server translations can label consent. Model-generated option ordering must
# never map "revise" onto the stable "approve" action.
_COPY_LABELS = {
    "tr": ("Bir fikrim var", "Bir tane yaz", "Bu ifadeyi kullan", "Düzenle", "Atla"),
    "es": ("Tengo una idea", "Escribe una", "Usa este texto", "Revísalo", "Omitir"),
    "pt": ("Tenho uma ideia", "Escreve uma", "Usa este texto", "Revê o texto", "Ignorar"),
    "fr": ("J’ai une idée", "Propose un texte", "Utiliser ce texte", "Modifier", "Ignorer"),
    "de": (
        "Ich habe eine Idee",
        "Schreib einen Text",
        "Diesen Text verwenden",
        "Überarbeiten",
        "Weglassen",
    ),
    "it": ("Ho un’idea", "Scrivine una", "Usa questo testo", "Modifica", "Salta"),
    "ja": ("アイデアがあります", "文章を提案して", "この文章を使う", "修正する", "省略する"),
    "ko": ("아이디어가 있어요", "문구를 제안해 주세요", "이 문구 사용", "수정", "건너뛰기"),
    "zh": ("我有想法", "帮我写一句", "使用这段文字", "修改", "跳过"),
    "ar": ("لدي فكرة", "اكتب اقتراحًا", "استخدم هذه الصياغة", "عدّل الصياغة", "تخطّ"),
    "ru": (
        "У меня есть идея",
        "Предложи текст",
        "Использовать этот текст",
        "Изменить",
        "Пропустить",
    ),
}


# Typed Turkish answers for the non-skip options, added when a question is shown in
# Turkish. Wording consent stays a deliberate tap or an explicit "use it": a bare
# "tamam" or "evet" is not an alias, exactly like English "ok" / "yes".
_TR_TYPED_ALIASES: dict[str, tuple[str, ...]] = {
    OPT_WRITE_MY_OWN: ("fikrim var", "kendim yazacağım", "kendim yazarım", "kendim yazayım"),
    OPT_GENERATE: ("sen yaz", "sen bir tane yaz", "bir tane öner", "öner", "sen öner"),
    OPT_APPROVE: ("bunu kullan", "kullan", "bu ifadeyi kullan"),
    OPT_REVISE: ("değiştir", "düzenle", "başka bir şey"),
}


def localize_question(question: dict[str, Any], language: str) -> dict[str, Any]:
    """Relabel the options in ``language``, keeping the previous label as a typed alias.

    Safe to call again (the builders already localize for a Turkish chat and the planner
    localizes once more with the model's language): a label that is already in the target
    language is left alone and no alias is added twice, so the English label stays
    matchable and never duplicates.
    """

    code = language.lower().split("-")[0]
    labels = _COPY_LABELS.get(code)
    if labels:
        by_key = dict(
            zip(
                (OPT_WRITE_MY_OWN, OPT_GENERATE, OPT_APPROVE, OPT_REVISE, OPT_CANCEL),
                labels,
                strict=True,
            )
        )
        for option in question["options"]:
            shown = by_key[option["key"]]
            aliases = list(option.get("aliases") or [])
            additions = [option["label"]] if option["label"] != shown else []
            if code == "tr":
                additions += _TR_TYPED_ALIASES.get(option["key"], ())
            for alias in additions:
                if alias not in aliases:
                    aliases.append(alias)
            if aliases:
                option["aliases"] = aliases
            option["label"] = shown
    return question


def _localize_for_current_chat(question: dict[str, Any]) -> dict[str, Any]:
    """Turkish chats get Turkish buttons from every path that builds these questions."""

    if current_reply_language() == "tr":
        localize_question(question, "tr")
    return question


def question_message(question: Mapping[str, Any], message: str) -> str:
    """Keep the offered answers usable on clients that render only event text."""
    labels = [str(option["label"]) for option in question.get("options", [])]
    if labels and not all(label in message for label in labels):
        return message + "\n" + " · ".join(labels)
    return message


@dataclass(frozen=True)
class CreativeCopyState:
    target: Target
    dependency_digest: str = ""
    status: Literal[
        "authorship", "write_my_own", "generate", "revise", "approved", "cancelled", "stale"
    ] = "authorship"
    candidate: str | None = None
    approved: str | None = None
    cancelled: bool = False
    provenance: str | None = None


def fold_creative_copy(
    events: Events, *, dependency_digest: str
) -> dict[Target, CreativeCopyState]:
    """Fold only selections tied to exact persisted questions and current media."""
    questions: dict[str, Mapping[str, Any]] = {}
    latest_question_id: dict[Target, str] = {}
    states: dict[Target, CreativeCopyState] = {}
    for role, payload in events:
        if not isinstance(payload, Mapping):
            continue
        if role == "assistant":
            resolution = payload.get("creative_copy_resolution")
            if (
                isinstance(resolution, Mapping)
                and resolution.get("target") in {"opening_title", "closing_title"}
                and resolution.get("status") in {"creator_supplied", "cancelled"}
            ):
                target = resolution["target"]
                latest_question_id.pop(target, None)
                cancelled = resolution["status"] == "cancelled"
                current = resolution.get("dependency_digest") == dependency_digest
                text = resolution.get("text")
                states[target] = CreativeCopyState(
                    target=target,
                    dependency_digest=str(resolution.get("dependency_digest") or ""),
                    status="cancelled" if cancelled else "approved" if current else "stale",
                    approved=text if current and isinstance(text, str) and text else None,
                    candidate=text if isinstance(text, str) else None,
                    cancelled=cancelled,
                    provenance="creator",
                )
            question = payload.get("choice_question")
            if (
                isinstance(question, Mapping)
                and question.get("conflict") == CREATIVE_COPY_CONFLICT
                and question.get("creative_target") in {"opening_title", "closing_title"}
            ):
                questions[str(question.get("question_id"))] = question
                target = question["creative_target"]
                latest_question_id[target] = str(question.get("question_id"))
                # A new server question supersedes any older candidate or approval for
                # this target. This also makes a replacement suggestion re-approval-only.
                kind = str(question.get("kind"))
                status: Literal[
                    "authorship",
                    "write_my_own",
                    "generate",
                    "revise",
                    "approved",
                    "cancelled",
                    "stale",
                ] = (
                    "stale"
                    if question.get("dependency_digest") != dependency_digest
                    else ("authorship" if kind == "creative_copy_authorship" else "revise")
                )
                candidate = (
                    normalize_copy(question.get("candidate", ""))
                    if kind == "creative_copy_wording"
                    else None
                )
                states[target] = CreativeCopyState(
                    target=target,
                    dependency_digest=str(question.get("dependency_digest") or ""),
                    status=status,
                    candidate=candidate,
                )
            continue
        if role != "user":
            continue
        selection = payload.get("choice_selection")
        if not isinstance(selection, Mapping):
            continue
        question = questions.get(str(selection.get("question_id")))
        if question is None:
            continue
        if question.get("dependency_digest") != dependency_digest:
            # A stale answer is retained as a blocking state, never converted into
            # consent for replacement footage.
            continue
        if selection.get("delegated"):
            # Generic choice delegation is intentionally never wording consent.
            continue
        target = question["creative_target"]
        if latest_question_id.get(target) != str(selection.get("question_id")):
            continue
        option = str(selection.get("option_key"))
        options = {
            str(row.get("key")) for row in question.get("options") or [] if isinstance(row, Mapping)
        }
        if option not in options:
            continue
        if option == OPT_APPROVE and question.get("kind") == "creative_copy_wording":
            candidate = normalize_copy(question.get("candidate", ""))
            candidate_digest = hashlib.sha256(candidate.encode()).hexdigest()[:24]
            if candidate and question.get("candidate_digest") == candidate_digest:
                states[target] = CreativeCopyState(
                    target=target,
                    dependency_digest=dependency_digest,
                    status="approved",
                    candidate=candidate,
                    approved=candidate,
                    provenance="generated",
                )
        elif option == OPT_CANCEL:
            states[target] = CreativeCopyState(
                target=target,
                dependency_digest=dependency_digest,
                status="cancelled",
                cancelled=True,
            )
        elif option in {OPT_GENERATE, OPT_REVISE, OPT_WRITE_MY_OWN}:
            states[target] = CreativeCopyState(
                target=target, dependency_digest=dependency_digest, status=option
            )
    return states


def latest_open_creative_question(events: Events) -> dict[str, Any] | None:
    """Return only the newest unanswered creative-copy question.

    Unlike generic questions it survives discussion/re-asks; a replacement question
    invalidates every older id for the same target.
    """
    current: dict[str, dict[str, Any]] = {}
    for role, payload in events:
        if not isinstance(payload, Mapping):
            continue
        if role == "assistant" and isinstance(payload.get("creative_copy_resolution"), Mapping):
            current.pop(str(payload["creative_copy_resolution"].get("target")), None)
        if role == "assistant" and isinstance(payload.get("choice_question"), Mapping):
            question = dict(payload["choice_question"])
            if question.get("conflict") == CREATIVE_COPY_CONFLICT and question.get(
                "creative_target"
            ) in {"opening_title", "closing_title"}:
                current[str(question["creative_target"])] = question
            continue
        if role != "user" or not isinstance(payload.get("choice_selection"), Mapping):
            continue
        selection = payload["choice_selection"]
        for target, question in list(current.items()):
            offered = {row.get("key") for row in question.get("options", [])}
            if (
                selection.get("question_id") == question.get("question_id")
                and selection.get("option_key") in offered
                and not selection.get("delegated")
            ):
                current.pop(target)
    return list(current.values())[-1] if current else None


__all__ = [
    "CREATIVE_COPY_CONFLICT",
    "CreativeCopyState",
    "authorship_question",
    "fold_creative_copy",
    "latest_open_creative_question",
    "media_digest",
    "normalize_copy",
    "wording_question",
]
