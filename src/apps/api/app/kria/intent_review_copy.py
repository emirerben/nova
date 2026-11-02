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
# (English, Turkish "to the ...")
_ANCHORS = {
    "top": ("top", "üste"),
    "middle": ("middle", "ortaya"),
    "center": ("middle", "ortaya"),
    "bottom": ("bottom", "alta"),
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


def fix_done(kind: str, role: str) -> str | None:
    """A fix as it stands in the new video (past form); None for a kind that is never applied."""
    word = role_word(role)
    if kind == "restore_exact_text":
        return say(
            en=f"Restored your exact wording on the {word}.",
            tr=f"{_role_at_start(role)} tam yazdığın hâline döndü.",
        )
    if kind == "fit_text_to_duration":
        return say(
            en=f"Shortened the {word} so it ends before the video does.",
            tr=f"{_role_at_start(role)} video bitmeden bitecek şekilde kısaldı.",
        )
    return None


def fix_optional(kind: str, role: str, target: Mapping[str, Any] | None = None) -> str | None:
    """A fix offered, not made (optional form)."""
    word = role_word(role)
    if kind == "restore_exact_text":
        return say(
            en=f"Optional: put your exact wording back on the {word}.",
            tr=f"İstersen {word} tam yazdığın hâline döndürülebilir.",
        )
    if kind == "fit_text_to_duration":
        return say(
            en=f"Optional: shorten the {word} so it ends before the video does.",
            tr=f"İstersen {word} video bitmeden bitecek şekilde kısaltılabilir.",
        )
    if kind == "move_text_to_safe_anchor":
        en_anchor, tr_anchor = _ANCHORS.get(str((target or {}).get("anchor")), _ANCHORS["top"])
        return say(
            en=f"Optional: move the {word} to the {en_anchor} so it's easier to read.",
            tr=f"İstersen {word} daha kolay okunsun diye {tr_anchor} taşınabilir.",
        )
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


def headline_staged() -> str:
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


def question_text() -> str:
    return say(
        en="The text on screen doesn't match what you asked for. Want me to change it?",
        tr="Ekrandaki yazı istediğinle uyuşmuyor. Değiştireyim mi?",
    )


def question_options() -> list[str]:
    return [say(en="Change it", tr="Değiştir"), say(en="Keep it", tr="Böyle kalsın")]


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
        en=(
            "The fix may have made the video worse. Your newer edits are untouched; you can "
            "undo the fix from its card."
        ),
        tr=(
            "Düzeltme videoyu kötüleştirmiş olabilir. Yeni düzenlemelerine dokunmadım; "
            "düzeltmeyi kendi kartından geri alabilirsin."
        ),
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
