"""Creator-facing wording for the intent review. Every sentence is server-templated.

The model supplies no prose: roles, issues, fixes and reasons come from closed tables keyed by
catalog candidate kind and element role, and every string goes through `say()` so the chat's
language decides. Nothing here claims a check the review did not make: a sentence that says the
video was checked is only available once the transport has proven it handed the frames to the
model (`require_visual_inspection`).
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Any

from app.kria.reply_language import say

INTENT_REVIEW_TURN_VALUE = "intent_review"

# (English, Turkish, Turkish at the start of a sentence)
_ROLES = {
    "title": ("title", "başlık", "Başlık"),
    "caption": ("caption", "altyazı", "Altyazı"),
    "text": ("text", "yazı", "Yazı"),
    "pin": ("pinned text", "sabit yazı", "Sabit yazı"),
}
# The optional "move it" fix, one whole sentence per anchor: German can't take an English
# anchor word mid-sentence (KRI-1362).
_MOVE_TO_ANCHOR = {
    "top": (
        "Optional: move the {word} to the top so it's easier to read.",
        "İstersen {word} daha kolay okunsun diye üste taşınabilir.",
    ),
    "middle": (
        "Optional: move the {word} to the middle so it's easier to read.",
        "İstersen {word} daha kolay okunsun diye ortaya taşınabilir.",
    ),
    "center": (
        "Optional: move the {word} to the middle so it's easier to read.",
        "İstersen {word} daha kolay okunsun diye ortaya taşınabilir.",
    ),
    "bottom": (
        "Optional: move the {word} to the bottom so it's easier to read.",
        "İstersen {word} daha kolay okunsun diye alta taşınabilir.",
    ),
}


class UncheckedReview(Exception):
    """The review cannot honestly be reported: it was not proven, or it cannot be read back."""


def visual_inspection_proven(review: Mapping[str, Any] | None) -> bool:
    """True only when the transport itself recorded that the model received the frames."""
    provenance = (review or {}).get("provenance")
    return isinstance(provenance, Mapping) and provenance.get("visual_input_received") is True


def require_visual_inspection(review: Mapping[str, Any] | None) -> None:
    if not visual_inspection_proven(review):
        raise UncheckedReview("the model was not proven to have received the frames")


def role_word(role: str) -> str:
    en, tr, _ = _ROLES.get(role, _ROLES["text"])
    return say(en=en, tr=tr)


def _role_at_start(role: str) -> str:
    en, _, tr_start = _ROLES.get(role, _ROLES["text"])
    return say(en=en, tr=tr_start)


def repair_issue(kind: str, role: str) -> str:
    word = role_word(role)
    if kind == "restore_exact_text":
        return say(
            en="the {word} isn't your exact wording",
            tr="{word} tam olarak yazdığın gibi değil",
            word=word,
        )
    if kind == "fit_text_to_duration":
        return say(
            en="the {word} runs past the end of the video",
            tr="{word} videonun sonundan taşıyor",
            word=word,
        )
    raise ValueError(f"no repair wording for {kind}")


def repair_summary(items: Sequence[tuple[str, str]]) -> str:
    """The repair draft's reply: what did not match, what was prepared, what the creator may do."""
    issues = [repair_issue(kind, role) for kind, role in items]
    if len(issues) == 1:
        return say(
            en=(
                "One thing didn't match your request: {issues_0}. I prepared a fix in your "
                "draft. Approve it to render, or undo it."
            ),
            tr=(
                "Bir şey isteğinle uyuşmadı: {issues_0}. Taslağında bir düzeltme hazırladım. "
                "Oluşturmak için onayla ya da geri al."
            ),
            issues_0=issues[0],
        )
    joined = "; ".join(issues)
    return say(
        en=(
            "A few things didn't match your request: {joined}. I prepared fixes in your "
            "draft. Approve them to render, or undo them."
        ),
        tr=(
            "Birkaç şey isteğinle uyuşmadı: {joined}. Taslağında düzeltmeler hazırladım. "
            "Oluşturmak için onayla ya da geri al."
        ),
        joined=joined,
    )


def fix_done(kind: str, role: str) -> str | None:
    """A fix as it stands in the new video (past form); None for a kind that is never applied."""
    word = role_word(role)
    if kind == "restore_exact_text":
        return say(
            en="Restored your exact wording on the {word}.",
            tr="{role_at_start_role} tam yazdığın hâline döndü.",
            word=word,
            role_at_start_role=_role_at_start(role),
        )
    if kind == "fit_text_to_duration":
        return say(
            en="Shortened the {word} so it ends before the video does.",
            tr="{role_at_start_role} video bitmeden bitecek şekilde kısaldı.",
            word=word,
            role_at_start_role=_role_at_start(role),
        )
    return None


def fix_optional(kind: str, role: str, target: Mapping[str, Any] | None = None) -> str | None:
    """A fix offered, not made (optional form)."""
    word = role_word(role)
    if kind == "restore_exact_text":
        return say(
            en="Optional: put your exact wording back on the {word}.",
            tr="İstersen {word} tam yazdığın hâline döndürülebilir.",
            word=word,
        )
    if kind == "fit_text_to_duration":
        return say(
            en="Optional: shorten the {word} so it ends before the video does.",
            tr="İstersen {word} video bitmeden bitecek şekilde kısaltılabilir.",
            word=word,
        )
    if kind == "move_text_to_safe_anchor":
        anchor = str((target or {}).get("anchor"))
        if anchor not in _MOVE_TO_ANCHOR:
            anchor = "top"
        return say(en=_MOVE_TO_ANCHOR[anchor][0], tr=_MOVE_TO_ANCHOR[anchor][1], word=word)
    return None


def headline_checked(*, everything_judged: bool) -> str:
    if everything_judged:
        return say(
            en=(
                "I checked the finished video against your request. Everything I could see matches."
            ),
            tr="Bitmiş videoyu isteğinle karşılaştırdım. Görebildiğim her şey uyuyor.",
        )
    return say(
        en=(
            "I checked frames from the finished video against your request. Everything I could "
            "see matches. I can't judge sound or motion from still frames."
        ),
        tr=(
            "Bitmiş videodan kareleri isteğinle karşılaştırdım. Görebildiğim her şey uyuyor. "
            "Sesi ve hareketi durağan karelerden değerlendiremem."
        ),
    )


def headline_suggestion() -> str:
    return say(
        en=(
            "I checked the finished video. Something may not match your request. "
            "I didn't change anything."
        ),
        tr=(
            "Bitmiş videoyu kontrol ettim. Bir şey isteğinle uyuşmuyor olabilir. "
            "Hiçbir şeyi değiştirmedim."
        ),
    )


def headline_staged(count: int = 1) -> str:
    if count > 1:
        return say(
            en=(
                "I checked the finished video against your request. A few things didn't match, "
                "and fixes are waiting for your approval."
            ),
            tr=(
                "Bitmiş videoyu isteğinle karşılaştırdım. Birkaç şey uyuşmadı ve düzeltmeler "
                "onayını bekliyor."
            ),
        )
    return say(
        en=(
            "I checked the finished video against your request. One thing didn't match, and a "
            "fix is waiting for your approval."
        ),
        tr=(
            "Bitmiş videoyu isteğinle karşılaştırdım. Bir şey uyuşmadı ve bir düzeltme "
            "onayını bekliyor."
        ),
    )


MAX_QUOTED_CHARS = 60


def _quoted(literal: str | None) -> str | None:
    """The creator's words in quotes, or None when one line cannot hold them exactly.

    A tap on "Change it to ..." authorises those words, so they are never shortened or
    reflowed: a literal that is too long or spans lines gets the plain wording instead.
    """
    words = (literal or "").strip()
    if not words or len(words) > MAX_QUOTED_CHARS or words != " ".join(words.split()):
        return None
    return f'"{words}"'


def question_text(literal: str | None = None) -> str:
    quoted = _quoted(literal)
    if quoted is None:
        return say(
            en="The text on screen doesn't match what you asked for. Want me to change it?",
            tr="Ekrandaki yazı istediğinle uyuşmuyor. Değiştireyim mi?",
        )
    return say(
        en=(
            "The text on screen doesn't match what you asked for. Want me to change it to {quoted}?"
        ),
        tr="Ekrandaki yazı istediğinle uyuşmuyor. {quoted} olarak değiştireyim mi?",
        quoted=quoted,
    )


def question_options(literal: str | None = None) -> list[str]:
    """The tap is an ordinary message, so the first option carries the words it authorises."""
    quoted = _quoted(literal)
    change = (
        say(en="Change it", tr="Değiştir")
        if quoted is None
        else say(en="Change it to {quoted}", tr="{quoted} olarak değiştir", quoted=quoted)
    )
    return [change, say(en="Keep it", tr="Böyle kalsın")]


def headline_verified() -> str:
    return say(
        en="I checked the new video: the fix worked. You can still undo it.",
        tr="Yeni videoyu kontrol ettim: düzeltme işe yaradı. İstersen hâlâ geri alabilirsin.",
    )


def headline_unverified() -> str:
    return say(
        en=(
            "The fix is in the new video, but I couldn't confirm it worked. You can still undo it."
        ),
        tr=(
            "Düzeltme yeni videoda, ama işe yaradığını doğrulayamadım. İstersen hâlâ geri "
            "alabilirsin."
        ),
    )


def headline_reverted() -> str:
    return say(
        en=(
            "The fix made the video worse, so I undid it in your draft. The video still shows "
            "the fixed version. Ask me to render your previous version if you want it."
        ),
        tr=(
            "Düzeltme videoyu kötüleştirdi, bu yüzden taslağında geri aldım. Video hâlâ "
            "düzeltilmiş hâliyle duruyor. İstersen önceki sürümü oluşturmamı söyle."
        ),
    )


def headline_reverted_over_newer_edits() -> str:
    return say(
        en="The fix may have made the video worse. Your newer edits are untouched.",
        tr=("Düzeltme videoyu kötüleştirmiş olabilir. Yeni düzenlemelerine dokunmadım."),
    )


def reason_checked() -> str:
    return say(en="Checked in the video.", tr="Videoda kontrol edildi.")


def reason_may_not_match() -> str:
    return say(
        en="May not match in the video. Nothing changed.",
        tr="Videoda uyuşmuyor olabilir. Hiçbir şey değişmedi.",
    )


def reason_fix_staged() -> str:
    return say(en="Fix waiting for your approval.", tr="Düzeltme onayını bekliyor.")


def reason_fix_verified() -> str:
    return say(en="Fixed and checked again.", tr="Düzeltildi ve tekrar kontrol edildi.")


def reason_fix_unverified() -> str:
    return say(en="Fixed, but not confirmed.", tr="Düzeltildi ama doğrulanamadı.")


def reason_fix_undone() -> str:
    return say(en="Fix undone in your draft.", tr="Düzeltme taslağında geri alındı.")
