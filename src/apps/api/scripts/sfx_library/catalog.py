"""The creator sound-effect library, one entry per published effect (KRI-173).

Names are plain ("Wrong buzzer", "Correct ding") and unique: the creator agent
resolves an effect named in chat by exact, case-insensitive name. Keep the
words pop / bubble / click / tap / whoosh / swoosh / swipe where they fit —
overlay auto-SFX (``overlay_autoplace.map_sfx_intent``) matches them.

``contains_voice`` is True for anything with speech, singing or laughter; the
AI placers never auto-place those, but creators can still pick them.
"""

# ruff: noqa: E501 - one effect per line keeps the library table reviewable.

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Literal

import numpy as np

from app.services.sfx_catalog import SFX_CATEGORY_TERMS
from scripts.sfx_library import sources, synth
from scripts.sfx_library.master import Kind

LIBRARY_VERSION = "kria-sfx-library-v1"

Category = Literal[
    "rejection", "approval", "suspense", "transition", "impact", "comedy", "ui", "sports", "money"
]
CATEGORY_TERMS = SFX_CATEGORY_TERMS


@dataclass(frozen=True)
class Synth:
    recipe: str


@dataclass(frozen=True)
class Kenney:
    pack: str
    member: str


@dataclass(frozen=True)
class Freesound:
    sound_id: int


Source = Synth | Kenney | Freesound


@dataclass(frozen=True)
class Layer:
    source: Source
    at_s: float = 0.0
    start_s: float = 0.0
    dur_s: float | None = None


@dataclass(frozen=True)
class Effect:
    slug: str
    name: str
    category: Category
    layers: tuple[Layer, ...]
    terms: tuple[str, ...] = ()
    kind: Kind = "one_shot"
    contains_voice: bool = False
    tail_fade_ms: float | None = None

    @property
    def filename(self) -> str:
        return f"{self.slug}.m4a"

    @property
    def license(self) -> str:
        kinds = {isinstance(layer.source, Synth) for layer in self.layers}
        return "generated" if kinds == {True} else "cc0" if kinds == {False} else "cc0+generated"

    @property
    def provenance(self) -> str:
        return f"{LIBRARY_VERSION}:{self.slug} " + "; ".join(
            _provenance(layer.source) for layer in self.layers
        )

    @property
    def search_terms(self) -> list[str]:
        words = [w for w in re.split(r"[^a-z0-9+]+", self.name.casefold()) if len(w) > 1]
        ordered = [*self.terms, *words, *CATEGORY_TERMS[self.category], self.category]
        seen: dict[str, None] = {}
        for term in ordered:
            clean = " ".join(term.casefold().split())
            if clean:
                seen.setdefault(clean, None)
        return list(seen)[:32]


def _provenance(source: Source) -> str:
    if isinstance(source, Synth):
        return f"procedural numpy recipe {source.recipe!r}"
    if isinstance(source, Kenney):
        return f"Kenney {source.pack}/{source.member} (CC0) {sources.kenney_url(source.pack)}"
    return f"Freesound #{source.sound_id} (CC0) {sources.freesound_page(source.sound_id)}"


def _load(source: Source, slug: str) -> np.ndarray:
    if isinstance(source, Synth):
        return synth.stereo(synth.render(source.recipe, slug))
    if isinstance(source, Kenney):
        return sources.load_kenney(source.pack, source.member)
    return sources.load_freesound(source.sound_id)


# Ramps on layer edges cut out of a longer source. Without them a slice that
# ends mid-sound clicks, and inside a multi-layer effect (the triple whistle)
# master() can't fade it away because the cut isn't at the file's end.
_CUT_FADE_IN_S = 0.005
_CUT_FADE_OUT_S = 0.015


def _ramp(n: int) -> np.ndarray:
    return 0.5 - 0.5 * np.cos(np.linspace(0.0, np.pi, n))


def render_raw(effect: Effect) -> np.ndarray:
    """Mix an effect's layers before mastering (float stereo, 48 kHz)."""
    parts = []
    for layer in effect.layers:
        audio = _load(layer.source, effect.slug)
        start = int(round(layer.start_s * synth.SR))
        stop = start + int(round(layer.dur_s * synth.SR)) if layer.dur_s is not None else None
        piece = np.array(audio[start:stop], dtype=np.float64, copy=True)
        if start > 0 and len(piece):
            n = min(len(piece), int(_CUT_FADE_IN_S * synth.SR))
            piece[:n] *= _ramp(n)[:, None]
        if stop is not None and stop < len(audio) and len(piece):
            n = min(len(piece), int(_CUT_FADE_OUT_S * synth.SR))
            piece[-n:] *= _ramp(n)[::-1, None]
        parts.append((layer.at_s, piece))
    return synth.place(parts)


def _s(recipe: str) -> tuple[Layer, ...]:
    return (Layer(Synth(recipe)),)


def _k(pack: str, member: str, **clip: float) -> tuple[Layer, ...]:
    return (Layer(Kenney(pack, member), **clip),)


def _f(sound_id: int, **clip: float) -> tuple[Layer, ...]:
    return (Layer(Freesound(sound_id), **clip),)


def _whistle_triple() -> tuple[Layer, ...]:
    blast = Freesound(218318)
    return (
        Layer(blast, at_s=0.0, start_s=0.08, dur_s=0.36),
        Layer(blast, at_s=0.42, start_s=0.08, dur_s=0.36),
        Layer(blast, at_s=0.84, start_s=0.08, dur_s=1.2),
    )


LIBRARY: tuple[Effect, ...] = (
    # ── Rejection / fail ────────────────────────────────────────────────────
    Effect("wrong-buzzer", "Wrong buzzer", "rejection", _s("wrong_buzzer_short"), ("buzzer", "quiz", "game show", "wrong answer", "nope")),
    Effect("wrong-buzzer-long", "Wrong buzzer long", "rejection", _s("wrong_buzzer_long"), ("buzzer", "quiz", "game show", "wrong answer")),
    Effect("wrong-buzzer-double", "Wrong buzzer double", "rejection", _s("wrong_buzzer_double"), ("buzzer", "quiz", "eh eh", "wrong answer")),
    Effect("error-beep", "Error beep", "rejection", _s("error_beep"), ("beep", "denied", "invalid", "alert")),
    Effect("error-blip", "Error blip", "rejection", _k("interface-sounds", "error_006.ogg"), ("blip", "denied", "invalid", "ui error")),
    Effect("nope-boing", "Nope boing", "rejection", _s("nope_boing"), ("nope", "boing", "denied", "cartoon")),
    Effect("sad-trombone", "Sad trombone", "rejection", _s("sad_trombone"), ("wah wah", "trombone", "womp womp", "disappointed", "sad")),
    Effect("fail-horn", "Fail horn", "rejection", _s("fail_horn"), ("horn", "bwamp", "disappointed", "sad")),
    Effect("game-over-jingle", "Game over jingle", "rejection", _s("game_over_jingle"), ("game over", "8 bit", "retro", "arcade", "lose")),
    Effect("record-scratch", "Record scratch", "rejection", _f(43404), ("scratch", "vinyl", "freeze frame", "wait what", "stop")),
    Effect("record-scratch-short", "Record scratch short", "rejection", _f(71853), ("scratch", "vinyl", "stop", "interrupt")),
    Effect("glass-break", "Glass break", "rejection", _f(221528), ("glass", "shatter", "smash", "break", "crash")),
    Effect("cartoon-slip", "Cartoon slip", "rejection", _s("cartoon_slip"), ("slip", "fall", "trip", "cartoon", "oops")),
    Effect("crowd-aww", "Crowd aww", "rejection", _f(124996), ("aww", "crowd", "disappointed", "sympathy"), contains_voice=True),
    Effect("crowd-boo", "Crowd boo", "rejection", _f(752707), ("boo", "crowd", "hate", "unpopular opinion"), contains_voice=True),
    Effect("crowd-booing", "Crowd booing", "rejection", _f(264378, start_s=1.0, dur_s=6.0), ("boo", "crowd", "angry"), kind="bed", contains_voice=True),
    # ── Approval / success ──────────────────────────────────────────────────
    Effect("correct-ding", "Correct ding", "approval", _s("correct_ding"), ("ding", "bell", "quiz", "correct answer", "check")),
    Effect("correct-ding-bright", "Correct ding bright", "approval", _s("correct_ding_bright"), ("ding", "bell", "quiz", "correct answer")),
    Effect("double-ding", "Double ding", "approval", _s("double_ding"), ("ding", "bell", "quiz", "correct answer", "ding ding")),
    Effect("success-chime", "Success chime", "approval", _s("success_chime"), ("chime", "done", "complete", "achievement", "unlock")),
    Effect("level-up", "Level up", "approval", _s("level_up"), ("level up", "8 bit", "arcade", "power up", "upgrade", "glow up")),
    Effect("tada-fanfare", "Tada fanfare", "approval", _s("tada_fanfare"), ("tada", "ta da", "fanfare", "reveal", "announce", "trumpet")),
    Effect("checkmark-pop", "Checkmark pop", "approval", _s("checkmark_pop"), ("check", "checkmark", "tick", "pop", "done")),
    Effect("confirm-blip", "Confirm blip", "approval", _k("interface-sounds", "confirmation_002.ogg"), ("blip", "confirm", "ok", "ui")),
    Effect("arcade-yes-blip", "Arcade yes blip", "approval", _s("arcade_yes_blip"), ("blip", "arcade", "8 bit", "retro", "ok")),
    Effect("applause", "Applause", "approval", _f(478414, start_s=2.0, dur_s=3.0), ("applause", "clap", "clapping", "bravo", "crowd")),
    Effect("applause-long", "Applause long", "approval", _f(478414, start_s=1.5, dur_s=10.5), ("applause", "clap", "clapping", "ovation", "crowd"), kind="bed"),
    Effect("rhythmic-clapping", "Rhythmic clapping", "approval", _f(777709, dur_s=8.0), ("clap", "clapping", "hype", "crowd", "chant"), kind="bed"),
    Effect("crowd-cheer", "Crowd cheer", "approval", _f(346689, start_s=4.4, dur_s=3.0), ("cheer", "cheering", "crowd", "hooray", "yay", "hype"), contains_voice=True),
    Effect("crowd-cheer-long", "Crowd cheer long", "approval", _f(346689, start_s=4.4, dur_s=9.5), ("cheer", "crowd", "hooray", "celebration"), kind="bed", contains_voice=True),
    Effect("small-crowd-cheer", "Small crowd cheer", "approval", _f(651646, dur_s=3.0), ("cheer", "crowd", "friends", "yay"), contains_voice=True),
    # ── Suspense / reveal ───────────────────────────────────────────────────
    Effect("drum-roll-crash", "Drum roll + crash", "suspense", _f(201211, start_s=2.2, dur_s=5.8), ("drum roll", "drumroll", "crash", "cymbal", "announcement", "and the winner is"), kind="bed"),
    Effect("drum-roll", "Drum roll", "suspense", _f(77305, dur_s=4.3), ("drum roll", "drumroll", "snare", "waiting", "countdown"), kind="bed"),
    Effect("heartbeat", "Heartbeat", "suspense", _s("heartbeat"), ("heartbeat", "heart", "nervous", "scared", "pulse"), kind="bed"),
    Effect("heartbeat-fast", "Heartbeat fast", "suspense", _s("heartbeat_fast"), ("heartbeat", "heart", "panic", "nervous", "pulse")),
    Effect("clock-ticking", "Clock ticking", "suspense", _s("clock_ticking"), ("clock", "tick tock", "ticking", "time", "deadline", "waiting"), kind="bed"),
    Effect("countdown-beeps", "Countdown beeps", "suspense", _s("countdown_beeps"), ("countdown", "3 2 1", "beep", "timer", "start")),
    Effect("dramatic-sting", "Dramatic sting", "suspense", _s("dramatic_sting"), ("dun dun dun", "dramatic", "plot twist", "shock", "sting")),
    Effect("riser", "Riser", "suspense", _s("riser_short"), ("riser", "build up", "rise", "swell", "before the drop"), tail_fade_ms=20),
    Effect("riser-long", "Riser long", "suspense", _s("riser_long"), ("riser", "build up", "rise", "swell", "before the drop"), tail_fade_ms=20),
    Effect("reverse-cymbal", "Reverse cymbal", "suspense", _f(383903, start_s=5.6, dur_s=2.75), ("reverse", "cymbal", "swell", "build up", "suck in"), tail_fade_ms=15),
    Effect("crowd-gasp", "Crowd gasp", "suspense", _f(264376, dur_s=2.2), ("gasp", "shock", "surprise", "omg", "crowd"), contains_voice=True),
    Effect("reveal-boom", "Reveal boom", "suspense", _s("reveal_boom"), ("boom", "reveal", "trailer", "big moment", "drop")),
    Effect("sparkle", "Sparkle", "suspense", _s("sparkle_magic"), ("sparkle", "magic", "shine", "glitter", "transformation", "glow up")),
    Effect("sparkle-short", "Sparkle short", "suspense", _s("sparkle_short"), ("sparkle", "magic", "shine", "twinkle")),
    # ── Transitions ─────────────────────────────────────────────────────────
    Effect("whoosh-fast", "Whoosh fast", "transition", _s("whoosh_fast"), ("whoosh", "swish", "pass by", "quick")),
    Effect("whoosh-slow", "Whoosh slow", "transition", _s("whoosh_slow"), ("whoosh", "air", "pass by", "smooth")),
    Effect("whoosh-heavy", "Whoosh heavy", "transition", _s("whoosh_heavy"), ("whoosh", "heavy", "deep", "cinematic")),
    Effect("swoosh", "Swoosh", "transition", _s("swoosh"), ("swoosh", "swish", "whoosh", "slide")),
    Effect("swipe", "Swipe", "transition", _s("swipe"), ("swipe", "slide", "next", "flick")),
    Effect("whip-pan", "Whip pan whoosh", "transition", _s("whip_pan"), ("whip pan", "whoosh", "camera move", "quick cut")),
    Effect("zoom-whoosh", "Zoom whoosh", "transition", _s("zoom_whoosh"), ("zoom", "zoom in", "whoosh", "punch in")),
    Effect("glitch", "Glitch", "transition", _s("glitch"), ("glitch", "digital", "error", "stutter", "tech")),
    Effect("glitch-short", "Glitch short", "transition", _s("glitch_short"), ("glitch", "digital", "stutter", "tech")),
    Effect("tape-rewind", "Tape rewind", "transition", _f(679970), ("rewind", "cassette", "flashback", "go back", "throwback")),
    Effect("tape-stop", "Tape stop", "transition", _s("tape_stop"), ("tape stop", "record stop", "slow down", "halt", "freeze")),
    Effect("camera-shutter", "Camera shutter", "transition", _f(520684), ("camera", "shutter", "photo", "snapshot", "picture")),
    Effect("camera-shutter-click", "Camera shutter click", "transition", _f(271010, start_s=0.6), ("camera", "shutter", "photo", "click", "snapshot")),
    Effect("page-flip", "Page flip", "transition", _k("rpg-audio", "bookFlip2.ogg"), ("page", "flip", "book", "next", "chapter")),
    # ── Impacts ─────────────────────────────────────────────────────────────
    Effect("punch-hit", "Punch hit", "impact", _k("impact-sounds", "impactPunch_heavy_000.ogg"), ("punch", "hit", "smack", "fight", "knockout")),
    Effect("punch-hit-light", "Punch hit light", "impact", _k("impact-sounds", "impactPunch_medium_002.ogg"), ("punch", "hit", "slap", "tap")),
    Effect("bass-boom", "Bass boom", "impact", _s("vine_boom"), ("vine boom", "boom", "bass", "meme", "bruh moment", "emphasis")),
    Effect("sub-drop", "Sub drop", "impact", _s("sub_drop"), ("sub", "bass drop", "drop", "low", "cinematic")),
    Effect("thud", "Thud", "impact", _k("impact-sounds", "impactSoft_heavy_000.ogg"), ("thud", "fall", "land", "body drop", "soft hit")),
    Effect("door-slam", "Door slam", "impact", _k("rpg-audio", "doorClose_1.ogg"), ("door", "slam", "shut", "leave", "angry")),
    Effect("metal-clang", "Metal clang", "impact", _k("impact-sounds", "impactMetal_heavy_000.ogg"), ("metal", "clang", "pan", "hit")),
    Effect("wood-knock", "Wood knock", "impact", _k("impact-sounds", "impactWood_heavy_000.ogg"), ("wood", "knock", "hit", "block")),
    Effect("glass-clink", "Glass clink", "impact", _k("impact-sounds", "impactGlass_light_000.ogg"), ("glass", "clink", "cheers", "toast", "cup")),
    Effect("bell-hit", "Bell hit", "impact", _k("impact-sounds", "impactBell_heavy_000.ogg"), ("bell", "gong", "ring", "round")),
    Effect("small-explosion", "Small explosion", "impact", _s("small_explosion"), ("explosion", "blast", "kaboom", "mind blown", "boom")),
    Effect("cinematic-hit", "Cinematic hit", "impact", _s("cinematic_hit"), ("trailer", "hit", "boom", "epic", "title")),
    # ── Comedy / meme ───────────────────────────────────────────────────────
    Effect("boing", "Boing", "comedy", _s("boing"), ("boing", "spring", "bounce", "jump", "cartoon")),
    Effect("boing-high", "Boing high", "comedy", _s("boing_high"), ("boing", "spring", "bounce", "hop")),
    Effect("slide-whistle-up", "Slide whistle up", "comedy", _s("slide_whistle_up"), ("slide whistle", "whistle", "rise", "jump", "up")),
    Effect("slide-whistle-down", "Slide whistle down", "comedy", _s("slide_whistle_down"), ("slide whistle", "whistle", "fall", "drop", "down")),
    Effect("rimshot", "Rimshot", "comedy", _s("rimshot"), ("ba dum tss", "badum tss", "drum", "punchline", "dad joke")),
    Effect("crickets", "Crickets", "comedy", _s("crickets"), ("crickets", "silence", "awkward silence", "nobody", "night"), kind="bed"),
    Effect("bonk", "Bonk", "comedy", _s("bonk"), ("bonk", "head", "hit", "go to jail", "cartoon")),
    Effect("clown-horn", "Clown horn", "comedy", _s("clown_horn"), ("honk", "clown", "horn", "circus", "silly")),
    Effect("squeaky-toy", "Squeaky toy", "comedy", _s("squeaky_toy"), ("squeak", "squeaky", "toy", "dog toy", "cute")),
    Effect("air-horn", "Air horn", "comedy", _s("air_horn"), ("air horn", "horn", "mlg", "hype", "drop")),
    Effect("laugh-track", "Laugh track", "comedy", _f(371562, dur_s=5.5), ("laugh", "laughing", "sitcom", "audience", "haha"), kind="bed", contains_voice=True),
    # ── UI / text ───────────────────────────────────────────────────────────
    Effect("soft-pop", "Soft pop", "ui", _s("pop_soft"), ("pop", "appear", "pop up", "sticker", "emoji")),
    Effect("accent-pop", "Accent pop", "ui", _s("pop_accent"), ("pop", "appear", "pop up", "sticker", "highlight")),
    Effect("bubble-pop", "Bubble pop", "ui", _s("bubble_pop"), ("bubble", "pop", "bloop", "cute", "appear")),
    Effect("bubble-pops", "Bubble pops", "ui", _s("bubble_pops"), ("bubble", "pop", "bloop", "list", "bullet points")),
    Effect("click", "Click", "ui", _k("ui-audio", "click1.ogg"), ("click", "button", "select", "press")),
    Effect("soft-click", "Soft click", "ui", _k("interface-sounds", "click_001.ogg"), ("click", "button", "subtle", "select")),
    Effect("mouse-click", "Mouse click", "ui", _k("ui-audio", "mouseclick1.ogg"), ("click", "mouse", "computer", "select")),
    Effect("tap", "Tap", "ui", _k("ui-audio", "click3.ogg"), ("tap", "touch", "phone", "screen")),
    Effect("switch-click", "Switch click", "ui", _k("ui-audio", "switch3.ogg"), ("switch", "toggle", "click", "on off")),
    Effect("typewriter-tick", "Typewriter tick", "ui", _f(380138), ("typewriter", "key", "type", "letter", "typing")),
    Effect("typewriter-bell", "Typewriter bell", "ui", _f(318687), ("typewriter", "bell", "ding", "end of line")),
    Effect("keyboard-typing", "Keyboard typing", "ui", _f(527386, start_s=0.2, dur_s=5.8), ("typing", "keyboard", "computer", "texting", "writing"), kind="bed"),
    Effect("notification", "Notification", "ui", _s("notification"), ("notification", "alert", "ping", "phone", "message")),
    Effect("notification-chime", "Notification chime", "ui", _s("notification_chime"), ("notification", "chime", "ding", "alert")),
    Effect("message-sent", "Message sent", "ui", _s("message_sent"), ("message", "sent", "send", "text", "whoosh")),
    Effect("message-received", "Message received", "ui", _s("message_received"), ("message", "received", "text", "dm", "notification")),
    Effect("question-blip", "Question blip", "ui", _k("interface-sounds", "question_001.ogg"), ("question", "blip", "hmm", "curious")),
    # ── Sports / football ───────────────────────────────────────────────────
    Effect("referee-whistle", "Referee whistle", "sports", _f(218318, start_s=0.08, dur_s=1.25), ("whistle", "referee", "foul", "kick off", "start")),
    Effect("referee-whistle-long", "Referee whistle long", "sports", _s("referee_whistle_long"), ("whistle", "referee", "full time", "end")),
    Effect("referee-whistle-triple", "Referee whistle triple", "sports", _whistle_triple(), ("whistle", "referee", "full time", "final whistle")),
    Effect("stadium-roar", "Stadium roar", "sports", _f(113698), ("stadium", "roar", "goal", "crowd", "fans", "cheer"), kind="bed", contains_voice=True),
    Effect("stadium-crowd", "Stadium crowd", "sports", _f(274516, dur_s=8.0), ("stadium", "crowd", "fans", "atmosphere"), kind="bed", contains_voice=True),
    Effect("goal-horn", "Goal horn", "sports", _s("goal_horn"), ("goal", "horn", "score", "hockey", "celebration")),
    Effect("crowd-ooh-near-miss", "Crowd ooh near miss", "sports", _f(494362, start_s=2.6, dur_s=3.0), ("ooh", "near miss", "so close", "almost", "crowd"), contains_voice=True),
    Effect("crowd-ooh", "Crowd ooh", "sports", _f(264499, start_s=5.9, dur_s=2.3), ("ooh", "wow", "impressed", "crowd"), contains_voice=True),
    Effect("crowd-chant", "Crowd chant", "sports", _f(588617, dur_s=8.0), ("chant", "fans", "singing", "stadium", "ultras"), kind="bed", contains_voice=True),
    Effect("ball-kick", "Ball kick", "sports", _f(555042), ("kick", "ball", "shot", "pass", "strike")),
    Effect("net-swish", "Net swish", "sports", _f(518048, start_s=1.62, dur_s=0.34), ("swish", "net", "basketball", "score", "nothing but net")),
    # ── Money / wins ────────────────────────────────────────────────────────
    Effect("cash-register", "Cash register", "money", _f(209578), ("ka ching", "cha ching", "register", "sale", "paid")),
    Effect("coins", "Coins", "money", _k("rpg-audio", "handleCoins.ogg"), ("coins", "change", "pocket", "jingle")),
    Effect("coin-drop", "Coin drop", "money", _k("rpg-audio", "handleCoins2.ogg"), ("coin", "drop", "tip", "pay")),
    Effect("arcade-coin", "Arcade coin", "money", _s("arcade_coin"), ("coin", "arcade", "8 bit", "collect", "points")),
    Effect("slot-machine-win", "Slot machine win", "money", _s("slot_machine_win"), ("slot machine", "casino", "win", "lucky", "payout")),
    Effect("jackpot", "Jackpot", "money", _s("jackpot"), ("jackpot", "casino", "big win", "lucky", "winner")),
)  # fmt: skip


def by_slug() -> dict[str, Effect]:
    return {effect.slug: effect for effect in LIBRARY}
