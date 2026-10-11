"""KRI-598: "use all my clips" on a runtime-v2 Voiceover edit.

Runtime v2 resolves its manifest without the guided voiceover contract
(``GUIDED_VOICEOVER_NATIVE_ONLY_CODE``), so a recorded voiceover is always the native
edit: it plays the project's own clips, and a Visuals photo or video reaches it only
as a reaction beat (or the closing shot). The server's scope reader still turns "7
klibin hepsini kullan" / "use all my clips" into ``media_scope: "all"``; the model
boundary keeps that reading and selects every own clip
(``creator_policy.native_voiceover_all_scope``). Here, in the planner's policy check,
"all" becomes that explicit selection, and the reply says what the edit cannot show:
Visuals no spoken word places, and own clips beyond what one native edit takes.
"""

from __future__ import annotations

from app.agents._schemas.creator_agent import CreativeStrategy, ResolvedCreatorManifest
from app.agents._schemas.creator_policy import (
    MAX_MAIN_CREATOR_SELECTED_MEDIA,
    native_voiceover_all_scope,
    own_video_ids,
)
from app.kria.reply_language import say
from app.services.creator_capabilities import (
    CAPABILITY_REACTION_BEATS,
    resolve_creator_image_media_ref,
    resolve_creator_visual_video_media_ref,
)

VOICEOVER_VISUALS_LEFT_OUT_NOTICE = (
    "This voiceover edit uses all of your project's clips. Photos and videos from Visuals "
    "show up only on a word you say, so I left out the ones not tied to a word."
)
_VOICEOVER_VISUALS_LEFT_OUT_NOTICE_TR = (
    "Bu seslendirmeli düzenleme projendeki tüm klipleri kullanıyor. Visuals'taki fotoğraf "
    "ve videolar sadece söylediğin bir kelimede beliriyor, o yüzden bir kelimeye bağlı "
    "olmayanları koymadım."
)
# Said where nothing can time a Visual to the voice (no reaction beats on this edit).
VOICEOVER_VISUALS_UNTIMED_NOTICE = (
    "This voiceover edit uses all of your project's clips. Photos and videos from Visuals "
    "can't be timed to a voiceover here yet, so I left them out."
)
_VOICEOVER_VISUALS_UNTIMED_NOTICE_TR = (
    "Bu seslendirmeli düzenleme projendeki tüm klipleri kullanıyor. Visuals'taki fotoğraf "
    "ve videolar henüz seslendirmeye göre zamanlanamıyor, o yüzden onları koymadım."
)
VOICEOVER_ALL_CLIPS_CAP_NOTICE = (
    f"A voiceover edit can use up to {MAX_MAIN_CREATOR_SELECTED_MEDIA} clips, so I used the "
    f"first {MAX_MAIN_CREATOR_SELECTED_MEDIA} of yours and left out the rest."
)
_VOICEOVER_ALL_CLIPS_CAP_NOTICE_TR = (
    f"Seslendirmeli düzenleme en fazla {MAX_MAIN_CREATOR_SELECTED_MEDIA} klip kullanabiliyor, "
    f"o yüzden ilk {MAX_MAIN_CREATOR_SELECTED_MEDIA} klibini kullandım, gerisini koymadım."
)


def _placed_visuals(manifest: ResolvedCreatorManifest, strategy: CreativeStrategy) -> set[str]:
    """Media a spoken word (`reaction_beats`) or the closing shot puts on screen."""

    refs = [beat.visual_id for beat in strategy.reaction_beats or ()]
    if strategy.closing_media is not None:
        refs += [strategy.closing_media.visual_id, strategy.closing_media.badge_visual_id]
    placed: set[str] = set()
    for ref in refs:
        if not ref:
            continue
        media = resolve_creator_image_media_ref(
            manifest, ref
        ) or resolve_creator_visual_video_media_ref(manifest, ref)
        placed.add(media.media_id if media is not None else ref)
    return placed


def resolve_native_all_scope(
    manifest: ResolvedCreatorManifest, strategy: CreativeStrategy
) -> tuple[CreativeStrategy, list[str]]:
    """``media_scope: "all"`` on a native-only voiceover edit -> every own clip.

    Returns the strategy unchanged (and no notices) for anything else.
    """

    if not native_voiceover_all_scope(manifest, strategy):
        return strategy, []
    own = own_video_ids(manifest)
    notices: list[str] = []
    placed = _placed_visuals(manifest, strategy)
    if any(
        media.media_id not in placed
        for media in manifest.media
        if media.media_id.startswith("asset-") or media.kind != "video"
    ):
        beats = manifest.capabilities.get(CAPABILITY_REACTION_BEATS)
        notices.append(
            say(en=VOICEOVER_VISUALS_LEFT_OUT_NOTICE, tr=_VOICEOVER_VISUALS_LEFT_OUT_NOTICE_TR)
            if beats is not None and beats.available
            else say(en=VOICEOVER_VISUALS_UNTIMED_NOTICE, tr=_VOICEOVER_VISUALS_UNTIMED_NOTICE_TR)
        )
    if len(own) > MAX_MAIN_CREATOR_SELECTED_MEDIA:
        notices.append(
            say(en=VOICEOVER_ALL_CLIPS_CAP_NOTICE, tr=_VOICEOVER_ALL_CLIPS_CAP_NOTICE_TR)
        )
    resolved = strategy.model_copy(
        update={
            "media_scope": "selected",
            "selected_media_ids": own[:MAX_MAIN_CREATOR_SELECTED_MEDIA],
            "render_program": "native",
        }
    )
    return resolved, notices


__all__ = [
    "VOICEOVER_ALL_CLIPS_CAP_NOTICE",
    "VOICEOVER_VISUALS_LEFT_OUT_NOTICE",
    "VOICEOVER_VISUALS_UNTIMED_NOTICE",
    "resolve_native_all_scope",
]
