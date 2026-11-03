"""Creator-facing wording for the intent review. Every sentence is server-templated.

The model supplies no prose: roles, issues and fixes come from closed tables keyed by catalog
candidate kind and element role, and every string goes through `say()` so the chat's language
decides. Nothing here claims a check the review did not make.
"""

from __future__ import annotations

from collections.abc import Sequence

from app.kria.reply_language import say

_ROLES = {
    "title": ("title", "başlık"),
    "caption": ("caption", "altyazı"),
    "text": ("text", "yazı"),
    "pin": ("pinned text", "sabit yazı"),
}


def role_word(role: str) -> str:
    en, tr = _ROLES.get(role, _ROLES["text"])
    return say(en=en, tr=tr)


def repair_issue(kind: str, role: str) -> str:
    word = role_word(role)
    if kind == "restore_exact_text":
        return say(
            en=f"the {word} isn't your exact wording",
            tr=f"{word} tam olarak yazdığın gibi değil",
        )
    if kind == "fit_text_to_duration":
        return say(
            en=f"the {word} runs past the end of the video",
            tr=f"{word} videonun sonundan taşıyor",
        )
    raise ValueError(f"no repair wording for {kind}")


def repair_summary(items: Sequence[tuple[str, str]]) -> str:
    """The repair draft's reply: what did not match, what was prepared, what the creator may do."""
    issues = [repair_issue(kind, role) for kind, role in items]
    if len(issues) == 1:
        return say(
            en=(
                f"One thing didn't match your request: {issues[0]}. I prepared a fix in your "
                "draft. Approve it to render, or undo it."
            ),
            tr=(
                f"Bir şey isteğinle uyuşmadı: {issues[0]}. Taslağında bir düzeltme hazırladım. "
                "Oluşturmak için onayla ya da geri al."
            ),
        )
    joined = "; ".join(issues)
    return say(
        en=(
            f"A few things didn't match your request: {joined}. I prepared fixes in your "
            "draft. Approve them to render, or undo them."
        ),
        tr=(
            f"Birkaç şey isteğinle uyuşmadı: {joined}. Taslağında düzeltmeler hazırladım. "
            "Oluşturmak için onayla ya da geri al."
        ),
    )
