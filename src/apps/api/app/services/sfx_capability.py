"""Why a route does not play a sound-effect intent, read from the gates the writer reads (KRI-1561).

The route card used to say "I can't add sound effects on this video yet" for every phone
route, whatever the gates were. `guided_sfx_limit` answers from the same predicate the
guided worker, Save and the manifest read (`phone_rollout.phone_guided_sfx_supported`), and
names the gate that is closed. It never answers from a copy of the gate list: when the
predicate says yes the limit is None, and `tests/kria/test_route_sfx_truth.py` holds the
explanation to that.

Nothing here reads the catalogue: an empty catalogue is found when effects are chosen
(`sfx_chooser.choose_sfx`), not on a card.
"""

from __future__ import annotations

from collections.abc import Iterable, Sequence

from app.config import settings
from app.kria.reply_language import say
from app.services.sfx_chooser import SfxLimit


def _closed_gate(flags: Sequence[bool], features: Iterable[str]) -> SfxLimit:
    """Which kind of gate is closed: a switch first, then the device features."""
    if not all(flags):
        return SfxLimit.flag_off
    if not set(features) <= set(settings.phone_render_verified_features):
        return SfxLimit.feature_unverified
    return SfxLimit.flag_off


def guided_sfx_limit() -> SfxLimit | None:
    """None when a guided / montage story plays sound effects right now, else the closed gate."""
    from app.services.phone_rollout import (  # noqa: PLC0415
        PHONE_GUIDED_SFX_FEATURES,
        phone_guided_sfx_supported,
    )

    if phone_guided_sfx_supported():
        return None
    return _closed_gate(
        (settings.sound_effects_enabled, settings.phone_guided_sfx_enabled),
        PHONE_GUIDED_SFX_FEATURES,
    )


def subtitled_sfx_limit() -> SfxLimit | None:
    """None when a phone Talking edit takes chosen sound effects as editable lane rows.

    The rows are the Talking `sound_effects` lane an editor Save recompiles, so the gate is the
    lane's (`phone_rollout.phone_subtitled_editor_lanes_supported`) plus the effects switch."""
    from app.services.phone_rollout import (  # noqa: PLC0415
        PHONE_SUBTITLED_EDITOR_FEATURES,
        phone_subtitled_editor_lanes_supported,
    )

    if settings.sound_effects_enabled and phone_subtitled_editor_lanes_supported():
        return None
    return _closed_gate(
        (
            settings.sound_effects_enabled,
            settings.phone_subtitled_editor_lanes_enabled,
            settings.phone_subtitled_media_lanes_enabled,
        ),
        PHONE_SUBTITLED_EDITOR_FEATURES,
    )


def voiceover_sfx_limit() -> SfxLimit | None:
    """None when a phone Voiceover (narrated) edit takes chosen sound effects as lane rows.

    The worker's own answer: `phone_voiceover_editor_lanes_supported` judged on the server-side
    gates alone (``require_client=False``), as the narrated worker reads it."""
    from app.services.phone_rollout import (  # noqa: PLC0415
        PHONE_VOICEOVER_EDITOR_FEATURES,
        phone_voiceover_editor_lanes_supported,
    )

    if phone_voiceover_editor_lanes_supported(require_client=False):
        return None
    return _closed_gate(
        (
            settings.sound_effects_enabled,
            settings.phone_voiceover_editor_lanes_enabled,
            settings.media_overlays_enabled,
        ),
        PHONE_VOICEOVER_EDITOR_FEATURES,
    )


# The creator sentence for each limit, English and Turkish (German: `copy/de.json`, keyed by the
# English). Module constants so the judge can register them (`brief_checks.is_format_limit`).
FLAG_OFF_EN = "Sound effects are switched off right now, so none will be played"
FLAG_OFF_TR = "Ses efektleri şu an kapalı, o yüzden hiçbiri çalmayacak"
FEATURE_UNVERIFIED_EN = (
    "This version of the app can't play sound effects yet, so none will be played"
)
FEATURE_UNVERIFIED_TR = (
    "Uygulamanın bu sürümü henüz ses efekti çalamıyor, o yüzden hiçbiri çalmayacak"
)
FORMAT_UNSUPPORTED_EN = "I can't add sound effects on this video yet, so none will be played"
FORMAT_UNSUPPORTED_TR = "Bu videoya henüz ses efekti ekleyemiyorum, hiçbiri çalmayacak"
EMPTY_CATALOG_EN = "There are no sound effects to choose from right now, so I added none"
EMPTY_CATALOG_TR = "Şu an seçilebilecek ses efekti yok, o yüzden hiçbirini eklemedim"
MULTI_SPEAKER_EN = "Sound effects need one speaker in the video, so I added none"
MULTI_SPEAKER_TR = "Ses efektleri videoda tek konuşmacı gerektiriyor, o yüzden hiçbirini eklemedim"
NO_TIMED_EVIDENCE_EN = (
    "I couldn't find a spoken word or a clear moment to put sound effects on, so I added none"
)
NO_TIMED_EVIDENCE_TR = (
    "Ses efektlerini koyacak bir söz ya da net bir an bulamadım, o yüzden hiçbirini eklemedim"
)
CREATOR_SAID_NO_EN = "You asked for no sound effects, so I added none"
CREATOR_SAID_NO_TR = "Ses efekti istemedin, o yüzden hiçbirini eklemedim"
NAMED_EFFECT_EN = "You named a sound effect, so I played that one and added no others"
NAMED_EFFECT_TR = "Bir ses efekti söyledin, o yüzden onu çaldım ve başka efekt eklemedim"
NOTHING_FIT_EN = "Nothing in this video clearly called for a sound effect, so I added none"
NOTHING_FIT_TR = "Bu videoda ses efekti isteyen net bir an olmadı, o yüzden hiçbirini eklemedim"

# KRI-1598: the limits that are the writer's, the app's or the footage's, never a simpler plan
# the creator could choose instead, as (english, turkish). The judge registers them as format
# limits, so a draft that cannot place effects says "Couldn't: <line>" and goes ahead instead of
# being held behind "Should I make a simpler version?". `creator_said_no` is left out: it is the
# creator's own choice, not a limit (the `_SB_NO_MEDIA` precedent in `brief_checks`).
FORMAT_LIMIT_LINES: tuple[tuple[str, str], ...] = (
    (FLAG_OFF_EN, FLAG_OFF_TR),
    (FEATURE_UNVERIFIED_EN, FEATURE_UNVERIFIED_TR),
    (FORMAT_UNSUPPORTED_EN, FORMAT_UNSUPPORTED_TR),
    (EMPTY_CATALOG_EN, EMPTY_CATALOG_TR),
    (MULTI_SPEAKER_EN, MULTI_SPEAKER_TR),
    (NO_TIMED_EVIDENCE_EN, NO_TIMED_EVIDENCE_TR),
    (NAMED_EFFECT_EN, NAMED_EFFECT_TR),
    (NOTHING_FIT_EN, NOTHING_FIT_TR),
)


def limit_line(limit: SfxLimit) -> str:
    """The creator sentence for a limit, in the chat's language."""
    if limit is SfxLimit.flag_off:
        return say(en=FLAG_OFF_EN, tr=FLAG_OFF_TR)
    if limit is SfxLimit.feature_unverified:
        return say(en=FEATURE_UNVERIFIED_EN, tr=FEATURE_UNVERIFIED_TR)
    if limit is SfxLimit.format_unsupported:
        return say(en=FORMAT_UNSUPPORTED_EN, tr=FORMAT_UNSUPPORTED_TR)
    if limit is SfxLimit.empty_catalog:
        return say(en=EMPTY_CATALOG_EN, tr=EMPTY_CATALOG_TR)
    if limit is SfxLimit.multi_speaker:
        return say(en=MULTI_SPEAKER_EN, tr=MULTI_SPEAKER_TR)
    if limit is SfxLimit.no_timed_evidence:
        return say(en=NO_TIMED_EVIDENCE_EN, tr=NO_TIMED_EVIDENCE_TR)
    if limit is SfxLimit.creator_said_no:
        return say(en=CREATOR_SAID_NO_EN, tr=CREATOR_SAID_NO_TR)
    if limit is SfxLimit.named_effect:
        return say(en=NAMED_EFFECT_EN, tr=NAMED_EFFECT_TR)
    return say(en=NOTHING_FIT_EN, tr=NOTHING_FIT_TR)
