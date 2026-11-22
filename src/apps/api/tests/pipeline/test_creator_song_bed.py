"""KRI-1578: the creator's own uploaded song as the music bed under a phone Talking edit.

Real boundaries: the real `compile_phone_subtitled_plan` lays the clips out, the real bed
compiler loops the song and the real `EditRecipeV2` validates the result. Expected clip spans
are worked out by hand from the song and video lengths, never read back from the code.

Failure modes guarded here:

B1  every bed that already ships (Talking library song, Voiceover and speech-montage library
    beds, guided background and lip-sync songs) compiles byte-identically after the bed gained
    a second source (digests pinned from the pre-change code)
"""

from __future__ import annotations

from app.kria.render_assets import RenderFingerprint
from app.pipeline.phone_guided_plan import compile_phone_guided_plan
from app.pipeline.phone_recipe_shared import PhoneMusicBed
from tests.pipeline.test_phone_song_lane import background_plan, lipsync_plan
from tests.pipeline.test_phone_speech_montage_plan import _compile as compile_speech_montage
from tests.pipeline.test_phone_speech_montage_plan import _montage, _speech
from tests.pipeline.test_talking_music_bed import (
    _bed,
    _digest_without_font_measurements,
    _talking,
    _words,
)
from tests.pipeline.user_song_helpers import bindings_for, compiled_plan, song_bed

# --- B1: byte identity -------------------------------------------------------------------------


def test_every_shipped_bed_compiles_byte_identical_to_before():
    talking_library = _talking(
        keep_segments=[(0.0, 3.0), (5.0, 10.0)],
        music=_bed(),
        speech_source_windows={"speaker": _words((1.0, 2.0), (6.0, 7.0))},
    )
    speech_montage, _ = compile_speech_montage(
        [_montage(4.0), _speech(0.0, 5.0, "speaker"), _montage(3.0), _speech(8.0, 12.0)],
        music=PhoneMusicBed(
            catalog_id="song-1",
            generation="7",
            fingerprint=RenderFingerprint(sha256="d" * 64, byte_count=500),
            duration_s=120.0,
            start_s=10.0,
            volume=0.9,
        ),
    )
    guided = {}
    for name, make_plan in (("lipsync", lipsync_plan), ("background", background_plan)):
        result = make_plan()
        footage, visuals = bindings_for(result)
        guided[name] = compile_phone_guided_plan(
            compiled_plan(result), footage, visuals, song=song_bed()
        )
    assert {
        "talking_library": _digest_without_font_measurements(talking_library),
        "speech_montage": _digest_without_font_measurements(speech_montage),
        "guided_lipsync": _digest_without_font_measurements(guided["lipsync"]),
        "guided_background": _digest_without_font_measurements(guided["background"]),
    } == {
        "talking_library": "d1641c69651610ab5e67125d010ddd308b419c0e1cc394b642f7694aa3f5b9ae",
        "speech_montage": "6a1775cd9055a670ff74ea00448e329e2948ee109a8c6e676bc14767262e37f8",
        "guided_lipsync": "ec572445b2626b5d7571921c7a5be5c5fb5475d96b6a3111ed32a903e2298092",
        "guided_background": "690b9ca0efe681e111442f7f9ea9ce59c00ec8a5a4d3c170e19e029cb1d386f7",
    }
