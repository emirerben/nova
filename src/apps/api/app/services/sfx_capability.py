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


def limit_line(limit: SfxLimit) -> str:
    """The creator sentence for a limit, in the chat's language."""
    if limit is SfxLimit.flag_off:
        return say(
            en="Sound effects are switched off right now, so none will be played",
            tr="Ses efektleri şu an kapalı, o yüzden hiçbiri çalmayacak",
        )
    if limit is SfxLimit.feature_unverified:
        return say(
            en="This version of the app can't play sound effects yet, so none will be played",
            tr="Uygulamanın bu sürümü henüz ses efekti çalamıyor, o yüzden hiçbiri çalmayacak",
        )
    if limit is SfxLimit.format_unsupported:
        return say(
            en="I can't add sound effects on this video yet, so none will be played",
            tr="Bu videoya henüz ses efekti ekleyemiyorum, hiçbiri çalmayacak",
        )
    if limit is SfxLimit.empty_catalog:
        return say(
            en="There are no sound effects to choose from right now, so I added none",
            tr="Şu an seçilebilecek ses efekti yok, o yüzden hiçbirini eklemedim",
        )
    if limit is SfxLimit.multi_speaker:
        return say(
            en="Sound effects need one speaker in the video, so I added none",
            tr="Ses efektleri videoda tek konuşmacı gerektiriyor, o yüzden hiçbirini eklemedim",
        )
    if limit is SfxLimit.no_timed_evidence:
        return say(
            en="I couldn't find a spoken word or a clear moment to put sound effects on, "
            "so I added none",
            tr="Ses efektlerini koyacak bir söz ya da net bir an bulamadım, o yüzden "
            "hiçbirini eklemedim",
        )
    if limit is SfxLimit.creator_said_no:
        return say(
            en="You asked for no sound effects, so I added none",
            tr="Ses efekti istemedin, o yüzden hiçbirini eklemedim",
        )
    return say(
        en="Nothing in this video clearly called for a sound effect, so I added none",
        tr="Bu videoda ses efekti isteyen net bir an olmadı, o yüzden hiçbirini eklemedim",
    )
