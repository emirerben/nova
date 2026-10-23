"""KRI-848: a song suggestion is honest about the genre the creator asked for.

Prod 2026-10-21 (Don Julio M1, thread F4083F1D): "...Suggest an upbeat tango song for it.
Make it 15 seconds." became brief requirement r8 [audio/global]. The chat-time song freeze
(`planner._freeze_licensed_music_section`) ran the music matcher with that ask as a soft
"strongest mood signal" and froze whatever it ranked first (top_track b8e5463c...).
Nothing looks at the genre: ``MusicMatcherOutput.ranked`` always holds a track, the
genre enum has no tango (`MusicLabels` coerces it to "other"), and the chat copy is Main
Creator free text written BEFORE the match, so the reply can call any pick "tango".

Product rule (Yasin 2026-10-15): music stays recommend-only. Nothing here may mix audio
into a video or cap its length by the song; this is about the suggestion being honest.

Ways this can fail (written before the fix; the tests below cover the starred ones):
 1* No library track fits the asked genre: the closest pick is suggested as if it fit.
 2* A fitting track exists but the matcher ranks a non-fitting one first.
 3* The fitting track is not in the matcher's top rows at all (it emits only
    n_variants + 3 picks), so re-checking the ranked rows alone can never find it.
 4* The genre word lives only in free-text labels (`vibe_tags`, `mood`,
    `ideal_content_profile`): the 7-value genre enum cannot hold it.
 5* Matching is case/accent/hyphen sensitive ("Tango", "electro-tango"), or matches
    inside other words ("rap" in "wrapped", "therapy").
 6* A mood-only ask ("calm music", "upbeat") raises a bogus "no genre" notice, or the
    keep-audible asks (also audio/global, joined into the music ask) are read as a genre.
 7* A genre the enum DOES know ("hip hop", "folk" -> acoustic, "EDM") does not count as
    fitting, or the catch-all "other" counts as fitting everything.
 8* Two genres in one ask ("tango or jazz"): the second one is ignored.
 9* The notice is only English, or its Turkish wording is missing.
10* The notice is appended to the summary and then cut by the 1000-character summary cap.
11* The frozen section is reused on a later turn (`_prior_frozen_music`) and loses the
    fit information, or a section frozen before this change (no fit fields) is trusted.
12* The library has no analysed track at all (`fallback_footage`) and the reply is silent
    about the missing song.
13* Main Creator's own summary already says "tango": the correction must still be said.
14* The notice lives only in the draft summary, and `reply_from_receipts` replaces the summary
    whenever a judged ask is not met (prod's M1 draft: the keep-audible asks were refused),
    keeping only ``planned.policy_notices``: the notice must ride both.
15  The receipts report the music ask as Done. (Already neutral: a lone music ask is an
    `unchecked` receipt, `test_brief_glued_ending_ask`; nothing to add here.)
16* A genre word that is also an everyday word ("the house tour", "the pop-in sound",
    "rock climbing b-roll") is read as a genre: the ask holds non-music audio asks, and a
    song's clip profile describes the clips it suits, not its genre.

Seams (the implementer may rename the two section fields; change the constants below):
- creator-facing text = the apply-strategy ``summary`` (and ``response`` for a question),
  plus ``PlannedKriaTurn.policy_notices`` (what survives when receipts replace the summary).
- ``strategy["selected_music_section"]`` (the frozen section) records the asked genre and
  whether the suggested track fits it: ``ASKED_KEY`` / ``FIT_KEY`` (proposed names).

Everything runs the real gate (`planner._gate_unresolved_choices`), the real song freeze,
the real `_match_best_track` / `_run_music_matcher` and the real matcher agent classes.
Only the model transport (`MusicMatcherAgent.run`, a ranker that ignores the creator's ask
like the prod run did) and the catalog read (in-memory tracks carrying `ai_labels`) are
fixed values. No paid call, no database.
"""

from __future__ import annotations

import re
import uuid
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from app.agents._schemas.creator_agent import CreativeStrategy, ProposeStrategy
from app.agents.music_matcher import (
    MusicMatcherAgent,
    MusicMatcherInput,
    MusicMatcherOutput,
    RankedTrack,
)
from app.kria import planner
from app.kria.brief import BriefRequirement, CreativeBrief
from app.kria.planner import PlannedKriaTurn
from app.kria.reply_language import reply_language_for
from app.schemas.edit_proposal import MontageAudioPlan
from tests.agents.test_music_matcher import _labels

# Proposed fields on the frozen section (see the module docstring).
ASKED_KEY = "asked_genre"
FIT_KEY = "genre_fit"

TANGO_ASK = "Suggest an upbeat tango song for it"
# No negation word and no genre word, so any such word in the reply comes from the notice.
BASE_SUMMARY = "A lively montage of your lunch at Don Julio."
BASE_SUMMARY_TR = "Don Julio'daki öğle yemeğinden canlı bir montaj."

_NEGATION = re.compile(
    r"\b(?:don't|do not|didn't|did not|couldn't|could not|can't|cannot|haven't|have no|"
    r"no|not|isn't|none|nothing|without|closest|nearest|unable)\b",
    re.IGNORECASE,
)
_NEGATION_TR = re.compile(r"(yok|bulamadım|bulunmuyor|bulunamadı|değil|en yakın)", re.IGNORECASE)


# ── the catalog (in-memory tracks that carry ai_labels) ──────────────────────


def _track(title: str, artist: str, **label_overrides) -> SimpleNamespace:
    return SimpleNamespace(
        id=str(uuid.uuid4()),
        title=title,
        artist=artist,
        duration_s=180.0,
        ai_labels={"labels": _labels(**label_overrides)},
        lyrics_cached=None,
        beat_timestamps_s=[],
    )


def _pop() -> SimpleNamespace:
    return _track(
        "Sunny Side Up",
        "The Brightsiders",
        genre="pop",
        vibe_tags=["upbeat", "bright", "fun"],
        energy="high",
        pacing="fast",
        mood="sunny, feel-good pop",
        ideal_content_profile="bright, happy, fast cuts of friends and food",
    )


def _lofi() -> SimpleNamespace:
    return _track(
        "Quiet Porch",
        "Marlowe Hale",
        genre="acoustic",
        vibe_tags=["calm", "warm"],
        energy="low",
        pacing="slow",
        mood="calm and warm",
        ideal_content_profile="slow golden-hour b-roll",
    )


def _tango(**overrides) -> SimpleNamespace:
    labels = dict(
        genre="other",  # what MusicLabels makes of "tango" (not in the 7-value enum)
        vibe_tags=["tango", "upbeat", "sultry"],
        energy="high",
        pacing="fast",
        mood="driving bandoneon, dance-floor drama",
        ideal_content_profile="nightlife, dancers and close-up food b-roll",
    )
    return _track("Midnight on Corrientes", "Orquesta Brava", **{**labels, **overrides})


class Library:
    """The catalog plus a ranker that, like the prod run, ignores the creator's ask."""

    def __init__(self, tracks: list[SimpleNamespace], preference: list[SimpleNamespace]):
        self.tracks = tracks
        self.preference = [t.id for t in preference]
        self.inputs: list[MusicMatcherInput] = []

    def rank(self, matcher_input: MusicMatcherInput) -> MusicMatcherOutput:
        self.inputs.append(matcher_input)
        available = [t.track_id for t in matcher_input.available_tracks]
        order = [tid for tid in self.preference if tid in available]
        order += [tid for tid in available if tid not in order]
        # The real matcher emits only the top `n_variants + 3` picks (prompt max_return).
        top = order[: min(matcher_input.n_variants + 3, len(available))]
        return MusicMatcherOutput(
            ranked=[
                RankedTrack(track_id=tid, score=max(0.0, 9.0 - i), rationale="fits the clips")
                for i, tid in enumerate(top)
            ]
        )


def _install(
    monkeypatch: pytest.MonkeyPatch,
    tracks: list[SimpleNamespace],
    preference: list[SimpleNamespace] | None = None,
) -> Library:
    import app.agents._model_client as model_client
    import app.services.music_sections as music_sections
    import app.tasks.auto_music_orchestrate as amo

    library = Library(tracks, preference if preference is not None else tracks)

    def fake_run(self, matcher_input, ctx=None):  # noqa: ANN001, ARG001
        return library.rank(matcher_input)

    monkeypatch.setattr(amo, "_load_matcher_candidates", lambda *a, **k: list(tracks))
    monkeypatch.setattr(amo, "_track_slot_count", lambda *a, **k: 4)
    monkeypatch.setattr(model_client, "default_client", lambda: None)
    monkeypatch.setattr(MusicMatcherAgent, "run", fake_run)
    monkeypatch.setattr(
        music_sections,
        "track_config_with_rank_one",
        lambda _t: {"best_start_s": 10.0, "best_end_s": 40.0},
    )
    return library


# ── the Don Julio M1 chat: brief, strategy, gate ─────────────────────────────


def _dj_brief(music_ask: str | None = TANGO_ASK, *, keep_audible: bool = True) -> CreativeBrief:
    """The real M1 brief shape (test_kri805): r6/r7 keep-audible and r8 are ALL audio/global."""
    reqs = [
        BriefRequirement(
            id="r5",
            kind="timing",
            scope="global",
            description="Make it 15 seconds",
            facts={"duration_s": 15},
        )
    ]
    if keep_audible:
        reqs += [
            BriefRequirement(
                id="r6",
                kind="audio",
                scope="global",
                description="Keep waiter's name exchange audible",
                facts={
                    "audio_intent": "source_unchanged",
                    "triggers": ["What's your name?", "Lucas."],
                },
            ),
            BriefRequirement(
                id="r7",
                kind="audio",
                scope="global",
                description='Keep that "Cheers!" audible too',
                facts={"audio_intent": "source_unchanged", "triggers": ["Cheers!"]},
            ),
        ]
    if music_ask is not None:
        reqs.append(BriefRequirement(id="r8", kind="audio", scope="global", description=music_ask))
    return CreativeBrief(version=1, requirements=reqs)


def _rows() -> list[dict]:
    return [
        {
            "media_id": f"c{i:02d}",
            "kind": "video",
            "duration_s": 3.0,
            "capture": {"capture_time": f"2026-09-20T09:{i:02d}:00Z"},
        }
        for i in range(9)
    ]


def _planned(summary: str = BASE_SUMMARY) -> PlannedKriaTurn:
    strategy = CreativeStrategy(
        direction="guided_story",
        edit_format="montage",
        audio_strategy="licensed_music",
        pacing="fast",
        render_program="guided",
        rationale="A montage.",
        target_duration_s=15,
        target_duration_requested=True,
        montage_audio=MontageAudioPlan(),  # "keep the Cheers! audible"
    )
    plan = planner.adapt_creator_action(
        ProposeStrategy(kind="propose_strategy", strategy=strategy, summary=summary)
    )
    return PlannedKriaTurn(plan=plan, manifest_hash="a" * 64, context_hash="b" * 64)


async def _gate(
    monkeypatch: pytest.MonkeyPatch,
    brief: CreativeBrief,
    *,
    summary: str = BASE_SUMMARY,
    events: tuple = (),
    language: str | None = None,
) -> PlannedKriaTurn:
    from dataclasses import replace

    monkeypatch.setattr(planner, "load_latest_brief", AsyncMock(return_value=brief))
    monkeypatch.setattr(type(planner.settings), "creative_brief_for", lambda _s, _i: True)
    monkeypatch.setattr(type(planner.settings), "brief_binding_for", lambda _s, _i: True)
    monkeypatch.setattr(planner, "_load_thread_events", AsyncMock(return_value=list(events)))
    planned = replace(_planned(summary), media_snapshot={"clip_assignments": _rows()})
    with reply_language_for(language):
        return await planner._gate_unresolved_choices(
            MagicMock(spec=AsyncSession),
            planned,
            thread_id=uuid.uuid4(),
            creator_id=uuid.uuid4(),
            item_id=uuid.uuid4(),
        )


def _text(result: PlannedKriaTurn) -> str:
    """Everything the creator reads from this turn: the draft summary and any reply."""
    parts = [str(result.plan.response or "")]
    for intent in result.plan.intents:
        parts.append(str(intent.arguments.get("summary") or ""))
    return " ".join(part for part in parts if part).replace("’", "'")


def _section(result: PlannedKriaTurn) -> dict:
    assert result.plan.mode == "act", result.plan.response
    section = result.plan.intents[0].arguments["strategy"].get("selected_music_section")
    assert isinstance(section, dict), "the song freeze left no section on the draft"
    return section


def _assert_suggests(result: PlannedKriaTurn, track: SimpleNamespace) -> None:
    section = _section(result)
    assert section["track_id"] == track.id, (
        f"suggested {section.get('title')!r} by {section.get('artist')!r}, not {track.title!r}"
    )


def _says_no_match(text: str) -> bool:
    return bool(_NEGATION.search(text))


def _claims_genre_for(text: str, track: SimpleNamespace, genre: str) -> bool:
    """Does a sentence that names this track call it ``genre`` without a negation?"""
    for sentence in re.split(r"(?<=[.!?])\s+", text):
        names_track = track.title in sentence or track.artist in sentence
        if names_track and genre.lower() in sentence.lower() and not _NEGATION.search(sentence):
            return True
    return False


# ── 1. no tango in the library: say so, name the closest, never call it tango ───


@pytest.mark.asyncio
async def test_no_tango_track_says_so_and_names_the_closest(monkeypatch) -> None:
    """The prod repro. Library: pop / calm / electronic, none is tango; the ranker picks pop."""
    pop, calm = _pop(), _lofi()
    electronic = _track("Neon Run", "Pulse Club", genre="electronic", vibe_tags=["club", "fast"])
    _install(monkeypatch, [pop, calm, electronic])

    result = await _gate(monkeypatch, _dj_brief())

    text = _text(result)
    assert "tango" in text.lower(), f"the reply never mentions the asked genre: {text!r}"
    assert _says_no_match(text), f"the reply does not say there is no tango track: {text!r}"
    assert pop.title in text and pop.artist in text, f"the closest song is not named: {text!r}"
    assert not _claims_genre_for(text, pop, "tango"), f"a non-tango song is called tango: {text!r}"


@pytest.mark.asyncio
async def test_no_tango_track_section_records_the_misfit(monkeypatch) -> None:
    pop = _pop()
    _install(monkeypatch, [pop, _lofi()])

    section = _section(await _gate(monkeypatch, _dj_brief()))

    assert section["status"] == "selected" and section["track_id"] == pop.id
    assert section[ASKED_KEY] == "tango"
    assert section[FIT_KEY] is False


@pytest.mark.asyncio
async def test_the_notice_survives_when_the_receipts_reply_replaces_the_summary(
    monkeypatch,
) -> None:
    """`reply_from_receipts` keeps the draft summary only when every judged ask is met; in
    prod's M1 draft the keep-audible asks were refused, so the reply was built from the
    receipts plus ``planned.policy_notices`` and a notice that lives only in the summary
    never reached the creator. The notice must ride ``policy_notices`` as well."""
    from app.kria.brief_checks import reply_from_receipts
    from app.kria.contracts import RequirementReceipt

    pop = _pop()
    _install(monkeypatch, [pop, _lofi()])
    brief = _dj_brief()
    result = await _gate(monkeypatch, brief)
    refused = RequirementReceipt(
        requirement_id="r6",
        status="not_possible",
        verification="checked",
        reason="Photo and sound pop-ins timed to your words aren't available",
    )
    keep_audible = next(req for req in brief.requirements if req.id == "r6")

    reply = reply_from_receipts(
        CreativeBrief(version=1, requirements=[keep_audible]),
        [refused],
        summary=str(result.plan.intents[0].arguments["summary"]),
        notices=result.policy_notices,
    ).replace("’", "'")

    assert "tango" in reply.lower(), f"the receipts reply drops the song notice: {reply!r}"
    assert pop.title in reply and pop.artist in reply, reply
    assert not _claims_genre_for(reply, pop, "tango"), reply


@pytest.mark.asyncio
async def test_main_creators_own_tango_claim_is_corrected(monkeypatch) -> None:
    """Main Creator writes its summary before the match and may already promise tango."""
    pop = _pop()
    _install(monkeypatch, [pop, _lofi()])

    result = await _gate(
        monkeypatch, _dj_brief(), summary="A lively lunch montage with an upbeat tango song."
    )

    text = _text(result)
    assert pop.title in text and pop.artist in text, f"the picked song is not named: {text!r}"
    assert _says_no_match(text), f"the tango claim is not corrected: {text!r}"
    assert not _claims_genre_for(text, pop, "tango")


@pytest.mark.asyncio
async def test_the_notice_survives_a_full_length_summary(monkeypatch) -> None:
    """The apply-strategy summary holds 1000 characters; a notice appended after a long
    Main Creator summary must not be the part that gets cut."""
    pop = _pop()
    _install(monkeypatch, [pop, _lofi()])
    long_summary = ("A lively montage of your lunch at Don Julio. " * 22)[:985].rstrip()

    result = await _gate(monkeypatch, _dj_brief(), summary=long_summary)

    text = _text(result)
    assert len(result.plan.intents[0].arguments["summary"]) <= 1000
    assert "tango" in text.lower() and _says_no_match(text), "the notice was cut off"
    assert pop.title in text


@pytest.mark.asyncio
async def test_turkish_reply_says_it_in_turkish(monkeypatch) -> None:
    pop = _pop()
    _install(monkeypatch, [pop, _lofi()])

    result = await _gate(
        monkeypatch,
        _dj_brief("Tango tarzı hareketli bir şarkı öner"),
        summary=BASE_SUMMARY_TR,
        language="tr",
    )

    text = _text(result)
    assert "tango" in text.lower()
    assert _NEGATION_TR.search(text), f"no Turkish 'none found' wording: {text!r}"
    assert pop.title in text and pop.artist in text
    assert "don't have" not in text.lower(), f"English copy in a Turkish chat: {text!r}"


@pytest.mark.asyncio
async def test_empty_library_with_a_tango_ask_is_not_silent(monkeypatch) -> None:
    """No analysed track at all -> `fallback_footage`; the creator still asked for a song."""
    _install(monkeypatch, [])

    result = await _gate(monkeypatch, _dj_brief())

    text = _text(result)
    assert "tango" in text.lower() and _says_no_match(text), f"silent about the song: {text!r}"


# ── 2. a tango track exists: it is the suggestion, whatever the ranker preferred ─


@pytest.mark.asyncio
async def test_tango_track_wins_over_a_better_ranked_non_tango_track(monkeypatch) -> None:
    pop, tango = _pop(), _tango()
    _install(monkeypatch, [pop, tango], preference=[pop, tango])

    result = await _gate(monkeypatch, _dj_brief())

    text = _text(result)
    _assert_suggests(result, tango)
    assert tango.title in text and tango.artist in text, f"the song is not named: {text!r}"
    assert "tango" in text.lower()
    assert not _says_no_match(text), f"claims no tango track exists: {text!r}"
    assert pop.title not in text


@pytest.mark.asyncio
async def test_tango_track_section_records_the_fit(monkeypatch) -> None:
    pop, tango = _pop(), _tango()
    _install(monkeypatch, [pop, tango], preference=[pop, tango])

    section = _section(await _gate(monkeypatch, _dj_brief()))

    assert section["track_id"] == tango.id, f"suggested {section.get('title')!r}"
    assert section[ASKED_KEY] == "tango"
    assert section[FIT_KEY] is True


@pytest.mark.asyncio
async def test_tango_track_ranked_beyond_the_matchers_top_picks_is_still_found(
    monkeypatch,
) -> None:
    """The matcher emits only n_variants + 3 rows (4 here). Re-checking those rows can
    never find a tango track the model ranked ninth; the genre has to be applied to the
    candidates the matcher sees (or to a wider ask)."""
    fillers = [
        _track(f"Filler {i}", f"Band {i}", genre="pop", vibe_tags=["upbeat", "fun"])
        for i in range(8)
    ]
    tango = _tango()
    _install(monkeypatch, [*fillers, tango], preference=[*fillers, tango])

    result = await _gate(monkeypatch, _dj_brief())

    _assert_suggests(result, tango)
    text = _text(result)
    assert tango.title in text and not _says_no_match(text), text


@pytest.mark.asyncio
async def test_matcher_already_ranking_the_tango_track_first_is_unchanged(monkeypatch) -> None:
    """Control (passes before and after): the matcher's pick already fits."""
    pop, tango = _pop(), _tango()
    _install(monkeypatch, [pop, tango], preference=[tango, pop])

    result = await _gate(monkeypatch, _dj_brief())

    _assert_suggests(result, tango)
    assert not _says_no_match(_text(result))


# ── 3. a mood-only ask is not a genre ask ───────────────────────────────────


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("ask", "keep_audible"),
    [
        ("Suggest some calm music for it", True),
        ("Suggest an upbeat song for it", True),
        ("Kalan kliplerde sakin bir müzik olsun", True),
        ("something happy and energetic", False),
        (None, True),  # only the keep-audible asks: they still reach the matcher as the "ask"
    ],
    ids=["calm", "upbeat", "turkish-calm", "happy-energetic", "keep-audible-only"],
)
async def test_mood_only_ask_keeps_the_matchers_pick_without_a_genre_notice(
    monkeypatch, ask, keep_audible
) -> None:
    """Control (passes before and after): no genre was asked, so nothing may be missing."""
    pop, calm = _pop(), _lofi()
    _install(monkeypatch, [pop, calm], preference=[pop, calm])

    result = await _gate(monkeypatch, _dj_brief(ask, keep_audible=keep_audible))

    text = _text(result)
    section = _section(result)
    assert section["track_id"] == pop.id
    assert section.get(FIT_KEY) is not False
    assert not _says_no_match(text), f"a mood ask raised a 'none found' notice: {text!r}"


# ── 3b. everyday words that are only sometimes a genre ──────────────────────
# The music ask is every live audio/global requirement joined with "; ", so it can hold a
# keep-this-sound ask ("the house tour", "the pop-in sound") that is not about the song.


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "ask",
    [
        "Keep the sound of the house tour audible",
        "Keep the pop-in sound",
        "Keep the ambient sound of the kitchen audible",
        "Keep the dance floor chatter audible",
        "Keep the rock climbing instructor audible",
    ],
    ids=["house-tour", "pop-in", "ambient", "dance-floor", "rock-climbing"],
)
async def test_an_everyday_word_in_a_non_music_audio_ask_is_not_a_genre(monkeypatch, ask) -> None:
    """Control for the lexicon: no genre was asked, so nothing may be missing."""
    pop, calm = _pop(), _lofi()
    _install(monkeypatch, [pop, calm], preference=[pop, calm])

    result = await _gate(monkeypatch, _dj_brief(ask))

    section = _section(result)
    assert ASKED_KEY not in section and FIT_KEY not in section
    assert section["track_id"] == pop.id
    assert not _says_no_match(_text(result)), _text(result)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("ask", "enum_genre"),
    [
        ("Suggest a house track for it", "electronic"),
        ("Suggest some dance music for it", "electronic"),
        ("Suggest an ambient song for it", "cinematic"),
    ],
    ids=["house", "dance", "ambient"],
)
async def test_an_everyday_word_next_to_a_music_word_is_a_genre(
    monkeypatch, ask, enum_genre
) -> None:
    pop = _pop()
    fit = _track("Right Genre", "Some Artist", genre=enum_genre, vibe_tags=["steady"])
    _install(monkeypatch, [pop, fit], preference=[pop, fit])

    result = await _gate(monkeypatch, _dj_brief(ask))

    _assert_suggests(result, fit)
    assert not _says_no_match(_text(result)), _text(result)


@pytest.mark.asyncio
async def test_an_everyday_word_in_the_clip_profile_does_not_make_a_track_that_genre(
    monkeypatch,
) -> None:
    """ "rock climbing b-roll" describes the clips a song suits, not the song's genre."""
    climbing = _track(
        "Summit Day",
        "Ridge Line",
        genre="pop",
        vibe_tags=["bright", "driving"],
        ideal_content_profile="rock climbing, hiking and outdoor b-roll",
    )
    _install(monkeypatch, [climbing])

    result = await _gate(monkeypatch, _dj_brief("Suggest a rock song for it"))

    text = _text(result)
    assert "rock" in text.lower() and _says_no_match(text), text
    assert not _claims_genre_for(text, climbing, "rock")


# ── 3c. a genre the creator ruled out is not a genre they asked for ──────────
# A named genre is a hard filter, so "no rap please" must never turn into "only rap".


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("ask", "ruled_out"),
    [
        ("Suggest an upbeat song, no rap please", {"genre": "hip_hop", "vibe_tags": ["hype"]}),
        ("Suggest a calm song, but not rap", {"genre": "hip_hop", "vibe_tags": ["hype"]}),
        ("Anything but reggaeton", {"genre": "other", "vibe_tags": ["reggaeton", "club"]}),
        ("Not jazz, something calm", {"genre": "other", "vibe_tags": ["jazz", "smoky"]}),
        ("Suggest a song without rock", {"genre": "other", "vibe_tags": ["rock", "loud"]}),
        ("I don't want any jazz in the song", {"genre": "other", "vibe_tags": ["jazz"]}),
        ("Suggest a song instead of rock", {"genre": "other", "vibe_tags": ["rock"]}),
        ("Pick a calm song rather than jazz", {"genre": "other", "vibe_tags": ["jazz"]}),
        ("Rap olmasın, sakin bir şarkı", {"genre": "hip_hop", "vibe_tags": ["hype"]}),
        ("Caz istemiyorum", {"genre": "other", "vibe_tags": ["jazz", "smoky"]}),
        ("Rock değil, sakin bir müzik olsun", {"genre": "other", "vibe_tags": ["rock"]}),
        ("Sin reggaeton por favor", {"genre": "other", "vibe_tags": ["reggaeton"]}),
        # A "no" rules out every genre after it in the ask, across commas and "or",
        # until a "but" (a list of banned genres is the common way to say it).
        ("No rap, reggaeton or rock", {"genre": "other", "vibe_tags": ["reggaeton"]}),
        ("Not jazz, reggaeton or samba please", {"genre": "other", "vibe_tags": ["samba"]}),
        ("Sin rap ni reggaeton", {"genre": "other", "vibe_tags": ["reggaeton"]}),
        # When unsure the genre is NOT asked: no filter, no notice, the matcher's pick stays.
        ("Without rock, a tango song", {"genre": "other", "vibe_tags": ["tango", "upbeat"]}),
        ("Sin reggaeton, quiero tango", {"genre": "other", "vibe_tags": ["tango", "upbeat"]}),
    ],
    ids=[
        "no-rap",
        "but-not-rap",
        "anything-but",
        "not-jazz",
        "without-rock",
        "dont-want-jazz",
        "instead-of-rock",
        "rather-than-jazz",
        "tr-olmasin",
        "tr-istemiyorum",
        "tr-degil",
        "es-sin",
        "list-after-no",
        "list-after-not",
        "es-ni",
        "unsure-without-then-tango",
        "unsure-sin-then-tango",
    ],
)
async def test_a_negated_genre_is_not_an_asked_genre(monkeypatch, ask, ruled_out) -> None:
    """No filter, no notice, no genre fields: the matcher's pick stands, the ruled-out
    genre is not forced (the library has a track of it, ranked behind the pick)."""
    pop = _pop()
    banned = _track("Ruled Out", "Some Band", **ruled_out)
    _install(monkeypatch, [pop, banned], preference=[pop, banned])

    result = await _gate(monkeypatch, _dj_brief(ask))

    section = _section(result)
    assert section["track_id"] == pop.id, f"suggested {section.get('title')!r}"
    assert ASKED_KEY not in section and FIT_KEY not in section
    assert not _says_no_match(_text(result)), _text(result)
    assert not result.policy_notices


@pytest.mark.asyncio
async def test_a_negated_genre_next_to_an_asked_one_only_the_asked_one_counts(monkeypatch) -> None:
    """ "Suggest a tango song, not jazz": jazz is ruled out, so a jazz-only track is not a fit
    and the notice talks about tango only."""
    pop = _pop()
    jazz = _track("Smoky Room", "Ella Blue", genre="other", vibe_tags=["jazz", "smoky"])
    _install(monkeypatch, [pop, jazz], preference=[jazz, pop])

    result = await _gate(monkeypatch, _dj_brief("Suggest a tango song, not jazz"))

    text = _text(result)
    section = _section(result)
    assert section[ASKED_KEY] == "tango" and section[FIT_KEY] is False
    assert "tango" in text.lower() and _says_no_match(text), text
    assert "jazz" not in text.lower(), text
    assert not _claims_genre_for(text, jazz, "tango")


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "ask",
    [
        "Suggest a tango song, not jazz",
        "No rap, but a tango song please",
        "No rap or reggaeton, but a tango song",
        "Sin reggaeton, pero un tango",
        "Rap olmasın ama bir tango şarkısı öner",
    ],
    ids=["tango-not-jazz", "no-rap-but-tango", "list-then-but", "es-pero", "tr-ama"],
)
async def test_the_asked_genre_is_still_found_beside_a_ruled_out_one(monkeypatch, ask) -> None:
    pop, tango = _pop(), _tango()
    banned = _track(
        "Ruled Out", "Some Band", genre="hip_hop", vibe_tags=["jazz", "reggaeton", "hype"]
    )
    _install(monkeypatch, [banned, pop, tango], preference=[banned, pop, tango])

    result = await _gate(monkeypatch, _dj_brief(ask))

    _assert_suggests(result, tango)
    section = _section(result)
    assert section[ASKED_KEY] == "tango" and section[FIT_KEY] is True
    assert not _says_no_match(_text(result)), _text(result)


# ── 4. what counts as "the label says tango" ────────────────────────────────


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("ask", "label_overrides"),
    [
        ("Suggest an upbeat tango song", {"vibe_tags": ["Tango", "upbeat"]}),
        ("SUGGEST A TANGO", {"mood": "sultry Argentine tango"}),
        (
            "Suggest a tango song",
            {"vibe_tags": ["upbeat"], "ideal_content_profile": "tango dancers and nightlife"},
        ),
        ("Suggest a tango song", {"vibe_tags": ["electro-tango", "club"]}),
        ("Suggest a Tángo song", {"vibe_tags": ["tango"]}),
        ("Tango şarkısı öner", {"vibe_tags": ["tango"]}),
    ],
    ids=["case-tag", "case-ask-mood", "profile-only", "hyphen", "accent", "turkish-ask"],
)
async def test_a_genre_word_in_any_label_text_counts_as_the_tango_label(
    monkeypatch, ask, label_overrides
) -> None:
    pop, tango = _pop(), _tango(**{"mood": "bright and driving", **label_overrides})
    # "other" is the enum's catch-all: the genre word must be found in the free text.
    _install(monkeypatch, [pop, tango], preference=[pop, tango])

    result = await _gate(monkeypatch, _dj_brief(ask))

    text = _text(result)
    _assert_suggests(result, tango)
    assert tango.title in text and not _says_no_match(text), text


@pytest.mark.asyncio
async def test_the_other_genre_is_not_a_wildcard(monkeypatch) -> None:
    """Ask for rock; the library has pop and an "other"-genre jazz track: neither is rock."""
    pop = _pop()
    jazz = _track("Smoky Room", "Ella Blue", genre="other", vibe_tags=["jazz", "smoky"])
    _install(monkeypatch, [pop, jazz], preference=[pop, jazz])

    result = await _gate(monkeypatch, _dj_brief("Suggest an upbeat rock song for it"))

    text = _text(result)
    assert "rock" in text.lower() and _says_no_match(text), text
    assert not _claims_genre_for(text, pop, "rock") and not _claims_genre_for(text, jazz, "rock")


@pytest.mark.asyncio
async def test_a_genre_word_inside_another_word_is_not_a_match(monkeypatch) -> None:
    """ "rap" sits inside "wrapped" and "therapy" (and "rap" is an alias of the hip_hop genre)."""
    wrapped = _track(
        "Warm Blanket",
        "Soft Hours",
        genre="pop",
        vibe_tags=["therapeutic", "cozy"],
        mood="wrapped in warm strings, like therapy",
    )
    _install(monkeypatch, [wrapped])

    result = await _gate(monkeypatch, _dj_brief("Suggest a rap song for it"))

    text = _text(result)
    # The notice may name the genre as the creator said it ("rap") or as the library does.
    assert re.search(r"\b(rap|hip[ -]?hop)\b", text.lower()) and _says_no_match(text), text
    assert not _claims_genre_for(text, wrapped, "rap")


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("ask", "enum_genre", "free_text"),
    [
        ("Suggest a hip hop song for it", "hip_hop", ["hype"]),
        ("Suggest a rap song for it", "hip_hop", ["hype"]),  # alias of hip_hop
        ("Suggest some folk music for it", "acoustic", ["warm"]),  # alias of acoustic
        ("Suggest an EDM track for it", "electronic", ["club"]),  # alias of electronic
        ("Suggest an orchestral score for it", "cinematic", ["epic"]),  # alias of cinematic
    ],
    ids=["hip-hop", "rap", "folk", "edm", "orchestral"],
)
async def test_a_genre_the_enum_knows_counts_as_fitting(
    monkeypatch, ask, enum_genre, free_text
) -> None:
    """The genre sits in `labels.genre`, not in any free text: the ask's aliases must map to it."""
    pop = _pop()
    fit = _track("Right Genre", "Some Artist", genre=enum_genre, vibe_tags=free_text)
    _install(monkeypatch, [pop, fit], preference=[pop, fit])

    result = await _gate(monkeypatch, _dj_brief(ask))

    text = _text(result)
    _assert_suggests(result, fit)
    assert fit.title in text and not _says_no_match(text), text


@pytest.mark.asyncio
async def test_two_genres_in_one_ask_either_one_fits(monkeypatch) -> None:
    pop = _pop()
    jazz = _track("Smoky Room", "Ella Blue", genre="other", vibe_tags=["jazz", "smoky"])
    _install(monkeypatch, [pop, jazz], preference=[pop, jazz])

    result = await _gate(monkeypatch, _dj_brief("Suggest a tango or a jazz song for it"))

    text = _text(result)
    _assert_suggests(result, jazz)
    assert jazz.title in text and not _says_no_match(text), text


# ── 5. the frozen section on a later turn ───────────────────────────────────


def _reuse_events(section: dict, brief: CreativeBrief) -> tuple:
    """A thread whose earlier length question carried this frozen section (KRI-679)."""
    ask = planner._creator_music_ask(brief)
    pool = planner._music_pool_digest(_rows(), "licensed_music", ask)
    return (
        (
            "assistant",
            {
                "choice_question": {
                    "length_context": {
                        "input_digest": "digest-of-an-earlier-turn",
                        "pool_digest": pool,
                        "licensed_music": section,
                    }
                }
            },
        ),
    )


@pytest.mark.asyncio
async def test_a_reused_frozen_section_keeps_saying_there_is_no_tango(monkeypatch) -> None:
    pop = _pop()
    library = _install(monkeypatch, [pop, _lofi()])
    brief = _dj_brief()
    first = _section(await _gate(monkeypatch, brief))
    matches_before = len(library.inputs)

    # Later turn, same clips and ask: the section is reused, the matcher is not run again.
    second = await _gate(monkeypatch, brief, events=_reuse_events(first, brief))

    assert len(library.inputs) == matches_before, "the frozen section was not reused"
    text = _text(second)
    assert "tango" in text.lower() and _says_no_match(text), text
    assert pop.title in text and pop.artist in text


@pytest.mark.asyncio
async def test_a_section_frozen_before_this_change_is_not_trusted_as_tango(monkeypatch) -> None:
    """A thread mid-flight at deploy: its frozen section (no fit fields) came from a ranker
    that never looked at the genre. Re-check it (or re-match) instead of staying silent."""
    pop = _pop()
    _install(monkeypatch, [pop, _lofi()])
    brief = _dj_brief()
    legacy = {  # exactly what `_freeze_licensed_music_section` froze before KRI-848
        "status": "selected",
        "track_id": pop.id,
        "title": pop.title,
        "artist": pop.artist,
        "start_s": 10.0,
        "end_s": 25.0,
        "catalog_duration_s": 180.0,
        "beat_timestamps_s": [],
    }

    result = await _gate(monkeypatch, brief, events=_reuse_events(legacy, brief))

    text = _text(result)
    assert "tango" in text.lower() and _says_no_match(text), text
    assert not _claims_genre_for(text, pop, "tango")
