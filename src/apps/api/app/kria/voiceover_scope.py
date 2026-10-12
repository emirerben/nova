"""KRI-598: "use all my clips" on a runtime-v2 Voiceover edit.

Runtime v2 resolves its manifest without the guided voiceover contract
(``GUIDED_VOICEOVER_NATIVE_ONLY_CODE``), so a recorded voiceover is always the native
edit: it plays the project's own clips, and a Visuals photo or video reaches it only
as a reaction beat (or the closing shot). The server's scope reader still turns "7
klibin hepsini kullan" / "use all my clips" into ``media_scope: "all"``; the model
boundary keeps that reading and selects every own clip
(``creator_policy.native_voiceover_all_scope``). Here, in the planner's policy check,
"all" becomes that explicit selection. What the edit cannot show is said or asked about
by the policy: a Visual the brief asks for that no spoken word places, and own clips
beyond what one native edit takes, are lost asks; other Visuals get a notice.
"""

from __future__ import annotations

from collections.abc import Collection, Mapping

from app.agents._schemas.creator_agent import CreativeStrategy, ResolvedCreatorManifest
from app.agents._schemas.creator_policy import (
    MAX_MAIN_CREATOR_SELECTED_MEDIA,
    native_voiceover_all_scope,
    own_video_ids,
)
from app.agents._schemas.edit_format import NARRATED_EDIT_FORMATS
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


def lost_visual_asks(
    manifest: ResolvedCreatorManifest,
    strategy: CreativeStrategy,
    requested_media: Mapping[str, str] | None,
) -> dict[str, str]:
    """Visuals a live brief requirement asks for that this native voiceover edit loses.

    PR 1556 review: a native voiceover edit shows a Visuals photo or video (anything but
    the project's own clips) only through a reaction beat or the closing shot. One the
    brief asks for (``requested_media``, from ``app.kria.brief.requested_media``) that no
    beat or closing shot places is lost, whether the Main Creator parse repaired a guided
    contract away or the model answered natively and forgot the beat. Maps each lost
    medium to the requirement's words. Own clips are selection (KRI-593), never asked
    about here, and a strategy that still carries the contract is left to the KRI-594
    downgrade, so nothing is asked twice.
    """

    if (
        not requested_media
        or not manifest.has_voiceover
        or strategy.edit_format not in NARRATED_EDIT_FORMATS
        or strategy.execution_contract is not None
    ):
        return {}
    own = set(own_video_ids(manifest))
    known = {media.media_id for media in manifest.media}
    placed = _placed_visuals(manifest, strategy)
    return {
        media_id: words
        for media_id, words in requested_media.items()
        if media_id in known and media_id not in own and media_id not in placed
    }


def resolve_native_all_scope(
    manifest: ResolvedCreatorManifest,
    strategy: CreativeStrategy,
    *,
    asked: Collection[str] = (),
) -> tuple[CreativeStrategy, list[str], list[str]]:
    """``media_scope: "all"`` on a native-only voiceover edit -> every own clip.

    ``asked`` are the lost Visuals the caller already names in its own words
    (``lost_visual_asks``); the generic "not tied to a word" notice covers only the rest.
    Returns ``(strategy, notices, clips_left_out)``: the own clips beyond what one native
    edit takes. "All" asked for each of them, so the caller treats them as a lost ask (PR
    1556 review, as the KRI-594 downgrade does). Unchanged, with nothing to say, for
    anything else.
    """

    if not native_voiceover_all_scope(manifest, strategy):
        return strategy, [], []
    own = own_video_ids(manifest)
    notices: list[str] = []
    placed = _placed_visuals(manifest, strategy)
    if any(
        media.media_id not in placed and media.media_id not in asked
        for media in manifest.media
        if media.media_id.startswith("asset-") or media.kind != "video"
    ):
        beats = manifest.capabilities.get(CAPABILITY_REACTION_BEATS)
        notices.append(
            say(en=VOICEOVER_VISUALS_LEFT_OUT_NOTICE, tr=_VOICEOVER_VISUALS_LEFT_OUT_NOTICE_TR)
            if beats is not None and beats.available
            else say(en=VOICEOVER_VISUALS_UNTIMED_NOTICE, tr=_VOICEOVER_VISUALS_UNTIMED_NOTICE_TR)
        )
    resolved = strategy.model_copy(
        update={
            "media_scope": "selected",
            "selected_media_ids": own[:MAX_MAIN_CREATOR_SELECTED_MEDIA],
            "render_program": "native",
        }
    )
    return resolved, notices, own[MAX_MAIN_CREATOR_SELECTED_MEDIA:]


__all__ = [
    "VOICEOVER_VISUALS_LEFT_OUT_NOTICE",
    "VOICEOVER_VISUALS_UNTIMED_NOTICE",
    "lost_visual_asks",
    "resolve_native_all_scope",
]
