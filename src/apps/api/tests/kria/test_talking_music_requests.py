"""KRI-1562 PR3: a creator asks for, changes and removes the song under a phone Talking edit.

Offline replay over the real chain. First request: the real runtime-v2 planner (real Postgres)
reads the request into a typed music ask; that ask on the approved brief reaches the real Talking
worker, whose real compiler lays the song under the talk; the real receipt checks read the pinned
recipe. Chat: the real snapshot, the real copilot agent (prompt and parser), the real compiler and
the real editor Save recompile the phone recipe. Stood in: each model's transport (authored
answers, no recording exists for these prompts), the library read and the catalog hash.

Ways this can fail, each pinned below:
 F1  flag off: the brief writer or the copilot is taught anything new, or chat can touch a song;
 F2  a live "no music" is outvoted by a music ask, a stored song or a chat swap;
 F3  the matcher picks a song the pin or the Save then refuses (silently dropped);
 F4  an empty library or a failed matcher fails the render, or the receipt claims a song;
 F5  an app build that cannot duck music still gets one (it would play over her voice);
 F6  a song first chosen outside the worker has no length, so the bed loses its export margin;
 F7  the creator's mood never reaches the matcher;
 F8  the receipt says "met" from a stored treatment the recipe does not play (negative control);
 F9  a chat swap inherits the old song's part instead of the new song's best section;
 F10 a chat add or swap travels as the montage soundtrack lane, which a Talking Save refuses;
 F11 "no music" later: a caption Save brings a carried song back;
 F12 a music ask on footage that is not Talking reaches the Talking checker or the matcher.
"""

from __future__ import annotations

import copy
import json
import uuid
from datetime import UTC, datetime
from types import SimpleNamespace
from typing import Any

import pytest

from app.agents._runtime import ModelClient, ModelInvocation
from app.agents._schemas.music_labels import CURRENT_LABEL_VERSION
from app.agents._schemas.song_sections import CURRENT_SECTION_VERSION
from app.kria.brief import BriefRequirement, CreativeBrief
from app.kria.brief_binding import BriefBinding
from app.kria.brief_checks import (
    PlanFacts,
    build_receipts,
    plan_facts_from_phone_variant,
)
from app.kria.reply_language import reply_language_for
from app.services.client_protocol import SPEECH_DUCK_PROTOCOL
from app.services.device_render import device_status
from app.tasks import generative_build as gb
from app.tasks.kria_runtime import _with_recipe_music
from tests.tasks.test_phone_subtitled_narrated_dispatch import _setup_subtitled, _words
from tests.tasks.test_phone_talking_music_worker import WORDS, _pin

VERIFIED = ["audioMix", "musicBed", "speechDuck"]


def _track(track_id: str, *, mood: str, start_s: float, end_s: float) -> SimpleNamespace:
    return SimpleNamespace(
        id=track_id,
        title=f"Song {track_id}",
        artist="Someone",
        analysis_status="ready",
        published_at=datetime(2026, 1, 1, tzinfo=UTC),
        archived_at=None,
        audio_gcs_path=f"music/{track_id}/audio.m4a",
        duration_s=60.0,
        ai_labels={
            "labels": {
                "label_version": CURRENT_LABEL_VERSION,
                "genre": "acoustic",
                "vibe_tags": ["soft"],
                "energy": "low",
                "pacing": "slow",
                "mood": mood,
                "ideal_content_profile": "talking to camera",
                "copy_tone": "sentimental",
                "transition_style": "dissolve",
                "color_grade": "warm",
            }
        },
        label_version=CURRENT_LABEL_VERSION,
        best_sections=[{"start_s": start_s, "end_s": end_s}],
        section_version=CURRENT_SECTION_VERSION,
        beat_timestamps_s=[],
        track_config={},
        recipe_cached=None,
        lyrics_cached=None,
    )


CALM = _track("song-calm", mood="calm and warm", start_s=30.0, end_s=56.0)
BRIGHT = _track("song-bright", mood="upbeat", start_s=5.0, end_s=20.0)


def _req(req_id: str, **facts: Any) -> BriefRequirement:
    return BriefRequirement(
        id=req_id, kind="audio", scope="global", description="the music", facts=facts
    )


MUSIC_EN = _req("m1", audio_intent="music", music_mood="chill")
MUSIC_TR = _req("m1", audio_intent="music", music_mood="sakin")
MUSIC_ANY = _req("m1", audio_intent="music")
NO_MUSIC = _req("n1", audio_intent="no_music")


def _binding(*reqs: BriefRequirement) -> dict:
    brief = CreativeBrief(version=1, requirements=list(reqs))
    return BriefBinding.create(uuid.uuid4(), brief).model_dump(mode="json")


class _Matcher(ModelClient):
    """The music matcher's transport: records each prompt and answers with ``ranked``."""

    def __init__(self, ranked: list[str] | None) -> None:
        self.ranked = ranked
        self.prompts: list[str] = []

    def invoke(self, *, prompt: str, **_: object) -> ModelInvocation:
        self.prompts.append(prompt)
        if self.ranked is None:
            raise RuntimeError("provider unavailable")
        answer = {
            "ranked": [
                {"track_id": track_id, "score": 8.0, "rationale": "fits the talk"}
                for track_id in self.ranked
            ]
        }
        return ModelInvocation(raw_text=json.dumps(answer), tokens_in=10, tokens_out=10)


def _render(
    monkeypatch,
    *,
    reqs: tuple[BriefRequirement, ...],
    library: list[Any],
    ranked: list[str] | None = None,
    flag: bool = True,
    protocol: int | None = SPEECH_DUCK_PROTOCOL,
    stored: dict | None = None,
):
    job, _snapshot, _session, _binding_ = _setup_subtitled(
        monkeypatch, transcript_words=_words(*WORDS)
    )
    monkeypatch.setattr(gb.settings, "talking_music_enabled", flag)
    monkeypatch.setattr(
        gb.settings,
        "phone_render_verified_features",
        [*gb.settings.phone_render_verified_features, *VERIFIED],
    )
    if reqs:
        job.assembly_plan["creator_brief_binding"] = _binding(*reqs)
    job.all_candidates["creator_client_protocol"] = protocol
    matcher = _Matcher(ranked)
    monkeypatch.setattr("app.agents._model_client.default_client", lambda: matcher)
    monkeypatch.setattr(gb, "_talking_library_tracks", lambda: list(library))
    monkeypatch.setattr(gb, "_persisted_music_treatment", lambda **_: stored)
    monkeypatch.setattr(gb, "_pin_catalog_music", _pin)
    gb._run_generative_job(str(job.id))
    assert job.status == "awaiting_device", "the talk renders whatever happens to the song"
    variant = job.assembly_plan["variants"][0]
    return job, variant, device_status(job, "subtitled").request.recipe, matcher


def _music(recipe) -> list:
    return [track for track in recipe.tracks if track.role == "music"]


def _receipt(job, variant, req: BriefRequirement, *, language: str = "en"):
    facts = _with_recipe_music(job, variant, plan_facts_from_phone_variant(variant))
    with reply_language_for(language):
        [receipt] = build_receipts([req], facts)
    return receipt


# --- the first request: "a talking video with some chill music" --------------------------------


@pytest.mark.usefixtures("prod_profile")
@pytest.mark.parametrize(("ask", "mood"), [(MUSIC_EN, "chill"), (MUSIC_TR, "sakin")])
def test_the_first_request_plays_the_song_the_matcher_picks_for_her_mood(monkeypatch, ask, mood):
    job, variant, recipe, matcher = _render(
        monkeypatch, reqs=(ask,), library=[BRIGHT, CALM], ranked=["song-calm", "song-bright"]
    )
    [prompt] = matcher.prompts
    assert mood in prompt, "the creator's own words for the music reach the matcher"
    [bed] = _music(recipe)
    assert bed.clips[0].source_asset_id == "music-song-calm"
    assert bed.clips[0].source_start == 30.0, "the song's rank-1 section, not its start"
    assert recipe.audio.speech_windows and "speechDuck" in recipe.required_capabilities
    treatment = variant["smart_music_treatment"]
    assert (treatment["track_id"], treatment["section_end_s"]) == ("song-calm", 50.0)
    assert (treatment["gain_db"], treatment["speech_duck_db"], treatment["duration_s"]) == (
        -18.0,
        -12.0,
        60.0,
    )
    assert variant["talking_music"] == {"status": "played", "track_id": "song-calm"}
    receipt = _receipt(job, variant, ask, language="tr" if mood == "sakin" else "en")
    assert receipt.status == "met"


@pytest.mark.usefixtures("prod_profile")
def test_a_receipt_never_says_met_when_the_recipe_has_no_song(monkeypatch):
    """Negative control: the same render with the song taken out of the pinned recipe."""
    job, variant, _recipe, _matcher = _render(
        monkeypatch, reqs=(MUSIC_EN,), library=[CALM], ranked=["song-calm"]
    )
    assert _receipt(job, variant, MUSIC_EN).status == "met"
    record = job.assembly_plan["_device_render_v1"]["subtitled"]
    request = record["status"]["request"]
    request["recipe"]["tracks"] = [
        track for track in request["recipe"]["tracks"] if track.get("role") != "music"
    ]
    request["recipe"]["audio"]["speech_windows"] = []
    request["recipe"]["audio"].pop("speech_duck_gain", None)
    from app.kria.device_render import recipe_digest  # noqa: PLC0415
    from app.kria.recipes_v2 import EditRecipeV2  # noqa: PLC0415

    request["identity"]["recipe_digest"] = recipe_digest(
        EditRecipeV2.model_validate(request["recipe"])
    )
    assert variant.get("smart_music_treatment"), "the stored treatment still names the song"
    receipt = _receipt(job, variant, MUSIC_EN)
    assert receipt.status != "met"
    assert receipt.reason == "The finished video has no music"


@pytest.mark.usefixtures("prod_profile")
@pytest.mark.parametrize(
    ("library", "ranked", "protocol", "reason", "tr"),
    [
        (
            [],
            ["song-calm"],
            SPEECH_DUCK_PROTOCOL,
            "The music library has no song I can add yet, so the video has no music",
            "Müzik kütüphanesinde henüz ekleyebileceğim bir şarkı yok, o yüzden videoda müzik yok",
        ),
        (
            [CALM],
            None,
            SPEECH_DUCK_PROTOCOL,
            "I couldn't pick a song from the music library, so the video has no music",
            "Müzik kütüphanesinden bir şarkı seçemedim, o yüzden videoda müzik yok",
        ),
        (
            [CALM],
            ["song-calm"],
            SPEECH_DUCK_PROTOCOL - 1,
            "Your iPhone app is too old to play music under your voice; update it first",
            "iPhone uygulaman sesinin altında müzik çalmak için çok eski; önce güncelle",
        ),
    ],
    ids=["empty_library", "matcher_failed", "app_too_old"],
)
def test_a_song_that_cannot_be_had_renders_the_talk_and_says_why(
    monkeypatch, library, ranked, protocol, reason, tr
):
    job, variant, recipe, matcher = _render(
        monkeypatch, reqs=(MUSIC_ANY,), library=library, ranked=ranked, protocol=protocol
    )
    assert _music(recipe) == [] and recipe.audio.speech_windows == []
    assert "smart_music_treatment" not in variant
    if protocol < SPEECH_DUCK_PROTOCOL:
        assert matcher.prompts == [], "no paid ranking for a song this build can't play"
    for language, text in (("en", reason), ("tr", tr)):
        receipt = _receipt(job, variant, MUSIC_ANY, language=language)
        assert (receipt.status, receipt.reason) == ("not_possible", text)


@pytest.mark.usefixtures("prod_profile")
def test_no_music_wins_over_a_music_ask_and_a_stored_song(monkeypatch):
    stored = {
        "track_id": "song-bright",
        "src_gcs_path": "music/song-bright/audio.m4a",
        "section_start_s": 5.0,
        "section_end_s": 20.0,
        "gain_db": -18.0,
        "speech_duck_db": -12.0,
    }
    job, variant, recipe, matcher = _render(
        monkeypatch,
        reqs=(MUSIC_EN, NO_MUSIC),
        library=[CALM],
        ranked=["song-calm"],
        stored=stored,
    )
    assert _music(recipe) == [] and matcher.prompts == []
    assert "smart_music_treatment" not in variant and "talking_music" not in variant
    receipt = _receipt(job, variant, NO_MUSIC)
    assert receipt.status == "met", receipt.reason


@pytest.mark.usefixtures("prod_profile")
def test_flag_off_a_music_ask_changes_nothing_and_asks_no_matcher(monkeypatch):
    _job, plain, plain_recipe, _ = _render(monkeypatch, reqs=(), library=[CALM], flag=False)
    job, variant, recipe, matcher = _render(
        monkeypatch, reqs=(MUSIC_EN,), library=[CALM], ranked=["song-calm"], flag=False
    )
    assert matcher.prompts == []
    assert recipe.model_dump_json() == plain_recipe.model_dump_json()
    assert "talking_music" not in variant and "smart_music_treatment" not in variant
    receipt = _receipt(job, variant, MUSIC_EN)
    assert receipt.status == "not_possible"
    assert receipt.reason == "A Talking video can't do this, so it stays as filmed"


@pytest.mark.parametrize(
    "facts",
    [
        PlanFacts(edit_format="montage", audio_strategy="licensed_music"),
        PlanFacts(edit_format="montage", audio_strategy="voiceover"),
        PlanFacts(edit_format="voiceover", audio_strategy="voiceover", voiceover_edit=True),
    ],
    ids=["montage", "montage_voiceover", "voiceover"],
)
def test_a_music_ask_on_footage_that_is_not_talking_reads_as_it_did_untyped(facts):
    from app.kria.brief_checks import is_judged  # noqa: PLC0415

    untyped = BriefRequirement(
        id="m1", kind="audio", scope="global", description="add some chill music"
    )
    typed = untyped.model_copy(update={"facts": {"audio_intent": "music", "music_mood": "chill"}})
    [before] = build_receipts([untyped], facts, include_unchecked=True)
    [after] = build_receipts([typed], facts, include_unchecked=True)
    assert after.model_dump() == before.model_dump()
    assert is_judged(typed, after) == is_judged(untyped, before)


@pytest.mark.usefixtures("prod_profile")
def test_a_library_that_cannot_be_read_renders_the_talk_and_says_no_song(monkeypatch):
    def unreadable():
        raise RuntimeError("database went away")

    job, _snapshot, _session, _binding_ = _setup_subtitled(
        monkeypatch, transcript_words=_words(*WORDS)
    )
    monkeypatch.setattr(gb.settings, "talking_music_enabled", True)
    monkeypatch.setattr(
        gb.settings,
        "phone_render_verified_features",
        [*gb.settings.phone_render_verified_features, *VERIFIED],
    )
    job.assembly_plan["creator_brief_binding"] = _binding(MUSIC_EN)
    job.all_candidates["creator_client_protocol"] = SPEECH_DUCK_PROTOCOL
    monkeypatch.setattr(gb, "_talking_library_tracks", unreadable)
    monkeypatch.setattr(gb, "_persisted_music_treatment", lambda **_: None)
    gb._run_generative_job(str(job.id))
    assert job.status == "awaiting_device"
    variant = job.assembly_plan["variants"][0]
    assert _music(device_status(job, "subtitled").request.recipe) == []
    assert variant["talking_music"] == {"status": "not_played", "reason": "no_match"}


def test_a_draft_promises_the_song_without_holding_approval(monkeypatch):
    monkeypatch.setattr(gb.settings, "talking_music_enabled", True)
    monkeypatch.setattr(
        gb.settings,
        "phone_render_verified_features",
        [*gb.settings.phone_render_verified_features, *VERIFIED],
    )
    from app.kria.approval_readiness import _promised_at_render  # noqa: PLC0415

    facts = PlanFacts(edit_format="subtitled", phone_talking=True)
    [receipt] = build_receipts([MUSIC_EN], facts)
    assert receipt.status == "partial"
    assert receipt.reason == (
        "I'll pick a song from the music library and play it quietly under your voice"
    )
    assert _promised_at_render(MUSIC_EN, receipt, facts)
    with reply_language_for("tr"):
        [turkish] = build_receipts([MUSIC_TR], facts)
    assert _promised_at_render(MUSIC_TR, turkish, facts)


def test_a_new_song_keeps_its_export_margin_from_the_length_the_treatment_carries():
    from app.pipeline.phone_recipe_shared import music_bed_from_treatment  # noqa: PLC0415
    from app.services.talking_music import song_treatment  # noqa: PLC0415

    def unpinned(track_id, _path):
        asset, _length = _pin(track_id)
        return asset, None

    treatment = song_treatment(_track("edge", mood="calm", start_s=45.0, end_s=60.0))
    bed = music_bed_from_treatment(treatment, unpinned)
    assert bed.duration_s == 60.0
    legacy = {k: v for k, v in treatment.items() if k != "duration_s"}
    assert music_bed_from_treatment(copy.deepcopy(legacy), unpinned).duration_s is None


# --- chat: add, swap and remove the song; "no music" holds -------------------------------------


def _rows() -> list[dict]:
    from app.services.talking_music import candidate_row  # noqa: PLC0415

    return [candidate_row(track) for track in (CALM, BRIGHT)]


STORED_BRIGHT = {
    "track_id": "song-bright",
    "src_gcs_path": "music/song-bright/audio.m4a",
    "section_start_s": 5.0,
    "section_end_s": 20.0,
    "gain_db": -18.0,
    "speech_duck_db": -12.0,
    "final_lufs": -14.0,
    "duration_s": 60.0,
}


class _Copilot(ModelClient):
    """The copilot's transport: records the prompt, answers with the authored ops."""

    def __init__(self, ops: list[dict]) -> None:
        self.ops = ops
        self.prompts: list[str] = []

    def invoke(self, *, prompt: str, **_: object) -> ModelInvocation:
        self.prompts.append(prompt)
        answer = {
            "intent": "edit",
            "ops": self.ops,
            "confidence": 0.9,
            "reply": "Done.",
            "claimed_ops": [op["op"] for op in self.ops],
        }
        return ModelInvocation(raw_text=json.dumps(answer), tokens_in=10, tokens_out=10)


@pytest.fixture
def phone(monkeypatch, prod_profile):
    """A published phone Talking edit, production settings with the rollout open, and a
    protocol 53 build sending."""
    from app.services.client_protocol import clear_request_context, set_client_protocol
    from tests.routes.test_phone_subtitled_editor_commit import phone_job
    from tests.routes.test_phone_talking_music_save import SPOKEN, _flag, _hashes

    clear_request_context()
    job = phone_job(monkeypatch)
    _flag(monkeypatch, True)
    variant = job.assembly_plan["variants"][0]
    variant["speech_source_windows"] = SPOKEN
    variant["output_url"] = "https://signed.test/out.mp4"
    _hashes(monkeypatch)
    set_client_protocol(SPEECH_DUCK_PROTOCOL)
    yield job
    clear_request_context()


def _with_song(job, treatment: dict) -> None:
    """The edit after a Save that put ``treatment`` under it (through the real Save)."""
    from app.routes import generative_jobs as gj  # noqa: PLC0415

    track = {"song-bright": BRIGHT, "song-calm": CALM}[treatment["track_id"]]
    variant = job.assembly_plan["variants"][0]
    gj.prepare_editor_commit(
        job,
        "subtitled",
        gj.EditorCommitRequest(
            base_generation=gj.variant_render_baseline(variant),
            background_music={
                "track_id": treatment["track_id"],
                "start_s": treatment["section_start_s"],
                "end_s": treatment["section_end_s"],
                "gain_db": treatment["gain_db"],
            },
        ),
        user_id="owner",
        plan_item_id="item",
        background_music_track=track,
    )


def _chat(job, utterance: str, ops: list[dict], *, context: dict | None = None, language="en"):
    """One chat turn: the real snapshot and copilot agent, the real compile and editor Save."""
    from app.agents.edit_copilot import EditCopilotAgent, EditCopilotInput  # noqa: PLC0415
    from app.routes import generative_jobs as gj  # noqa: PLC0415
    from app.services.kria_editor_ops import (  # noqa: PLC0415
        build_editor_snapshot,
        compile_editor_ops,
        project_editor_draft,
    )

    base = project_editor_draft(job.assembly_plan["variants"][0], {}, job)
    snapshot = build_editor_snapshot(
        job, base, clip_context=context if context is not None else {"music_library": _rows()}
    )
    client = _Copilot(ops)
    with reply_language_for(language):
        output = EditCopilotAgent(client).run(
            EditCopilotInput(utterance=utterance, variant_snapshot=snapshot)
        )
    saved = None
    if output.ops:
        payload = compile_editor_ops(job, base, output.ops).payload
        bed = payload.background_music
        saved = gj.prepare_editor_commit(
            job,
            "subtitled",
            payload,
            user_id="owner",
            plan_item_id="item",
            background_music_track={"song-bright": BRIGHT, "song-calm": CALM}.get(
                bed.track_id if bed is not None else None
            ),
        )
    return snapshot, output, client.prompts, saved


def _song(job):
    recipe = device_status(job, "subtitled").request.recipe
    return [track for track in recipe.tracks if track.role == "music"]


@pytest.mark.parametrize(
    ("utterance", "language"),
    [("add some chill music", "en"), ("arkaya sakin bir müzik ekle", "tr")],
)
def test_chat_adds_a_library_song_at_its_best_section(phone, utterance, language):
    snapshot, output, [prompt], _saved = _chat(
        phone,
        utterance,
        [{"op": "swap_music", "track_id": "song-calm"}],
        language=language,
    )
    assert snapshot["music"]["current_track_id"] is None and snapshot["music"]["swappable"]
    assert "TALKING VIDEO MUSIC" in prompt
    assert "id='song-calm' title='Song song-calm' mood='calm and warm'" in prompt
    assert [op["op"] for op in output.ops] == ["swap_music"]
    [bed] = _song(phone)
    assert bed.clips[0].source_asset_id == "music-song-calm"
    assert bed.clips[0].source_start == 30.0
    treatment = phone.assembly_plan["variants"][0]["smart_music_treatment"]
    assert (treatment["section_start_s"], treatment["section_end_s"]) == (30.0, 50.0)
    assert treatment["duration_s"] == 60.0


@pytest.mark.parametrize(
    ("utterance", "language"), [("swap the song", "en"), ("müziği değiştir", "tr")]
)
def test_chat_swaps_the_song_onto_the_new_songs_own_part(phone, utterance, language):
    _with_song(phone, STORED_BRIGHT)
    snapshot, output, _prompts, _saved = _chat(
        phone, utterance, [{"op": "swap_music", "track_id": "song-calm"}], language=language
    )
    assert snapshot["music"]["current_track_id"] == "song-bright"
    [bed] = _song(phone)
    assert bed.clips[0].source_asset_id == "music-song-calm"
    assert bed.clips[0].source_start == 30.0, "never the old song's 5 s start"


@pytest.mark.parametrize(
    ("utterance", "language"), [("remove the music", "en"), ("müziği kaldır", "tr")]
)
def test_chat_removes_the_song(phone, utterance, language):
    _with_song(phone, STORED_BRIGHT)
    assert _song(phone)
    _snapshot, output, _prompts, _saved = _chat(
        phone, utterance, [{"op": "remove_music"}], language=language
    )
    assert [op["op"] for op in output.ops] == ["remove_music"]
    assert _song(phone) == []
    assert phone.assembly_plan["variants"][0].get("smart_music_treatment") is None


@pytest.mark.parametrize(
    ("language", "note"),
    [
        ("en", "you asked for no music, so I won't add a song"),
        ("tr", "müzik istemediğini söylemiştin, o yüzden şarkı eklemiyorum"),
    ],
)
def test_chat_refuses_a_song_while_no_music_holds_and_still_removes_one(phone, language, note):
    _with_song(phone, STORED_BRIGHT)
    held = {"no_music": True}
    snapshot, output, [prompt], _saved = _chat(
        phone,
        "müziği değiştir",
        [{"op": "swap_music", "track_id": "song-calm"}],
        context=held,
        language=language,
    )
    assert snapshot["music"]["candidates"] == [] and "no_music=True" in prompt
    assert output.ops == []
    assert note in output.reply
    assert [bed.clips[0].source_asset_id for bed in _song(phone)] == ["music-song-bright"]
    _snapshot, removed, _prompts, _saved = _chat(
        phone, "no music", [{"op": "remove_music"}], context=held, language=language
    )
    assert [op["op"] for op in removed.ops] == ["remove_music"] and _song(phone) == []


def test_no_music_holds_through_later_caption_saves(phone):
    """A song carried from before (chat never removed it) is taken off by the next Save once the
    brief the edit is bound to says "no music", and it never comes back."""
    from app.routes import generative_jobs as gj  # noqa: PLC0415

    _with_song(phone, STORED_BRIGHT)
    assert _song(phone)
    phone.assembly_plan["creator_brief_binding"] = _binding(MUSIC_EN, NO_MUSIC)
    for text in ("Hello there, friends.", "Hello there, everyone."):
        variant = phone.assembly_plan["variants"][0]
        cues = copy.deepcopy(variant["caption_cues"])
        cues[0]["text"] = text
        gj.prepare_editor_commit(
            phone,
            "subtitled",
            gj.EditorCommitRequest(
                base_generation=gj.variant_render_baseline(variant), caption_cues=cues
            ),
            user_id="owner",
            plan_item_id="item",
        )
        assert _song(phone) == []
        assert phone.assembly_plan["variants"][0].get("smart_music_treatment") is None


def test_a_song_carried_by_a_caption_save_stays_without_a_no_music_ask(phone):
    """Control for the test above: the same caption Save keeps the song when no one asked."""
    from app.routes import generative_jobs as gj  # noqa: PLC0415

    _with_song(phone, STORED_BRIGHT)
    phone.assembly_plan["creator_brief_binding"] = _binding(MUSIC_EN)
    variant = phone.assembly_plan["variants"][0]
    cues = copy.deepcopy(variant["caption_cues"])
    cues[0]["text"] = "Hello there, friends."
    gj.prepare_editor_commit(
        phone,
        "subtitled",
        gj.EditorCommitRequest(
            base_generation=gj.variant_render_baseline(variant), caption_cues=cues
        ),
        user_id="owner",
        plan_item_id="item",
    )
    assert [bed.clips[0].source_asset_id for bed in _song(phone)] == ["music-song-bright"]


def test_chat_sets_the_songs_level_on_its_gain(phone):
    _with_song(phone, STORED_BRIGHT)
    snapshot, output, _prompts, _saved = _chat(
        phone, "make the music quieter", [{"op": "set_mix", "music_gain_db": -24}]
    )
    assert snapshot["mix"] == {
        "music_level_locked": True,
        "background_music": {"track_id": "song-bright", "gain_db": -18.0},
    }
    [bed] = _song(phone)
    assert bed.clips[0].volume == pytest.approx(10 ** (-24 / 20), rel=1e-4)


def test_flag_off_chat_cannot_touch_a_song_and_sees_what_it_saw_before(monkeypatch, phone):
    from app.services.kria_editor_ops import build_editor_snapshot  # noqa: PLC0415
    from tests.routes.test_phone_talking_music_save import _flag

    _flag(monkeypatch, False)
    variant = phone.assembly_plan["variants"][0]
    open_context = build_editor_snapshot(phone, variant, clip_context={"music_library": _rows()})
    plain = build_editor_snapshot(phone, variant)
    assert open_context == plain
    assert "music" not in plain["allowed_op_families"] and "music" not in plain
    _snapshot, output, [prompt], _saved = _chat(
        phone, "add some chill music", [{"op": "swap_music", "track_id": "song-calm"}]
    )
    assert output.ops == [] and "TALKING VIDEO MUSIC" not in prompt
    assert _song(phone) == []


# --- the first request through the real runtime-v2 planner (real Postgres) ---------------------


class _Creator(ModelClient):
    """The Main Creator's transport: the brief writer's answer to a first Talking request."""

    def __init__(self, mood: str) -> None:
        self.mood = mood
        self.prompts: list[str] = []

    def invoke(self, *, prompt: str, **_: object) -> ModelInvocation:
        from tests.kria.talking_thread import MEDIA_ID  # noqa: PLC0415

        self.prompts.append(prompt)
        answer = {
            "action": {
                "kind": "propose_strategy",
                "strategy": {
                    "direction": "native",
                    "edit_format": "subtitled",
                    "audio_strategy": "original_audio",
                    "media_scope": "selected",
                    "caption_style": "auto",
                    "render_program": "native",
                    "selected_media_ids": [MEDIA_ID],
                    "target_duration_s": 14.8,
                    "rationale": "Her talk with captions and a quiet song under it.",
                },
                "summary": "I'll caption your talk and add a quiet song under it.",
            },
            "brief_updates": [
                {
                    "kind": "audio",
                    "scope": "global",
                    "literal": None,
                    "description": "background music",
                    "facts": {"audio_intent": "music", "music_mood": self.mood},
                }
            ],
        }
        return ModelInvocation(raw_text=json.dumps(answer), tokens_in=10, tokens_out=20)


@pytest.mark.parametrize(
    ("message", "mood"),
    [
        ("Talking video with some chill music", "chill"),
        ("Konuşma videosu, arkada sakin bir müzik olsun", "sakin"),
    ],
)
@pytest.mark.asyncio
async def test_a_first_request_for_music_stays_a_talking_edit_with_the_typed_ask(
    monkeypatch, prod_profile_pre_cutover, message, mood
):
    from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine  # noqa: PLC0415
    from sqlalchemy.pool import NullPool  # noqa: PLC0415

    from app.config import settings  # noqa: PLC0415
    from app.kria import planner  # noqa: PLC0415
    from tests.kria.talking_thread import seed_talking_thread  # noqa: PLC0415

    monkeypatch.setattr(settings, "talking_music_enabled", True)
    monkeypatch.setattr(
        settings,
        "phone_render_verified_features",
        [*settings.phone_render_verified_features, *VERIFIED],
    )
    # The thread's last event is its own first line; the turn reads the request it is given.
    creator, thread_id, item_id = seed_talking_thread()
    model = _Creator(mood)
    monkeypatch.setattr(planner, "default_client", lambda: model)
    engine = create_async_engine(settings.asyncpg_database_url, poolclass=NullPool)
    try:
        async with async_sessionmaker(engine, expire_on_commit=False)() as db:
            planned = await planner.plan_live_turn(
                db, thread_id=thread_id, item_id=item_id, creator_id=creator, user_message=message
            )
    finally:
        await engine.dispose()
    assert any('{"audio_intent": "music"}' in prompt for prompt in model.prompts), (
        "the brief writer is taught the music fact while the rollout is open"
    )
    assert planned.plan.mode == "act", planned.plan.response
    apply = planned.plan.intents[0]
    assert apply.arguments["strategy"]["edit_format"] == "subtitled"
    assert "music library" not in apply.arguments["summary"], "never the withheld-song line"
    [ask] = [update for update in planned.brief_updates if update.kind == "audio"]
    assert ask.facts == {"audio_intent": "music", "music_mood": mood}


# --- the planner's library read and the chat Save's track (real Postgres / the claim path) -----


def _seed_track(*, published: bool) -> str:
    from app.database import sync_session  # noqa: PLC0415
    from app.models import MusicTrack  # noqa: PLC0415

    track_id = f"kri1562-{uuid.uuid4().hex[:12]}"
    row = _track(track_id, mood="calm and warm", start_s=30.0, end_s=56.0)
    with sync_session() as db:
        db.add(
            MusicTrack(
                id=track_id,
                title=row.title,
                artist=row.artist,
                source_url=f"https://example.test/{track_id}",
                audio_gcs_path=row.audio_gcs_path,
                duration_s=row.duration_s,
                analysis_status="ready",
                published_at=row.published_at if published else None,
                ai_labels=row.ai_labels,
                label_version=row.label_version,
                best_sections=row.best_sections,
                section_version=row.section_version,
            )
        )
        db.commit()
    return track_id


@pytest.mark.asyncio
async def test_the_planner_lists_published_songs_and_none_while_this_turn_says_no_music(
    monkeypatch,
):
    from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine  # noqa: PLC0415
    from sqlalchemy.pool import NullPool  # noqa: PLC0415

    from app.config import settings  # noqa: PLC0415
    from app.kria import planner  # noqa: PLC0415
    from app.services import kria_editor_ops  # noqa: PLC0415

    published = _seed_track(published=True)
    hidden = _seed_track(published=False)
    monkeypatch.setattr(kria_editor_ops, "talking_song_open", lambda *_: True)
    thread = SimpleNamespace(creator_id=uuid.uuid4())
    job = SimpleNamespace(
        id=uuid.uuid4(), user_id=thread.creator_id, all_candidates={}, assembly_plan={}
    )
    item = SimpleNamespace(id=uuid.uuid4(), clip_assignments=[])
    variant = {"variant_id": "subtitled", "render_destination": "device"}

    async def context(turn_brief):
        token = planner._turn_brief.set(turn_brief)
        engine = create_async_engine(settings.asyncpg_database_url, poolclass=NullPool)
        try:
            async with async_sessionmaker(engine, expire_on_commit=False)() as db:
                return await planner._copilot_clip_context(
                    db, thread=thread, thread_id=uuid.uuid4(), job=job, variant=variant, item=item
                )
        finally:
            planner._turn_brief.reset(token)
            await engine.dispose()

    asked = await context(CreativeBrief(version=2, requirements=[MUSIC_EN]))
    ids = [row["id"] for row in asked["music_library"]]
    assert published in ids and hidden not in ids
    row = next(row for row in asked["music_library"] if row["id"] == published)
    assert row["mood"] == "calm and warm" and "no_music" not in asked
    held = await context(CreativeBrief(version=2, requirements=[NO_MUSIC]))
    assert held.get("no_music") is True and "music_library" not in held


def test_the_chat_save_hands_the_new_songs_track_to_the_editor_commit():
    from contextlib import contextmanager  # noqa: PLC0415
    from unittest.mock import patch  # noqa: PLC0415

    from app.kria.device_render import make_device_request  # noqa: PLC0415
    from app.models import MusicTrack  # noqa: PLC0415
    from app.services.device_render import pin_device_request  # noqa: PLC0415
    from app.tasks import kria_runtime  # noqa: PLC0415
    from tests.kria.test_runtime_phone_v2 import (  # noqa: PLC0415
        _RECIPE,
        _claim_fixture,
        _device_job,
        _publish,
    )

    job = _device_job(status="variants_ready")
    _publish(job)
    db, approval, _execution, _ = _claim_fixture(job)
    db._gets[MusicTrack] = CALM
    seen: dict = {}

    def prepare(current_job, variant_id, *_args, **kwargs):
        seen.update(kwargs)
        pin_device_request(
            current_job,
            make_device_request(
                job_id=current_job.id, variant_id=variant_id, revision=2, recipe=_RECIPE
            ),
            base_generation="edit-gen-2",
        )
        return {
            "generation": "edit-gen-2",
            "has_render_section": True,
            "render_destination": "device",
            "sections": {},
        }

    @contextmanager
    def sessions():
        yield db

    document = SimpleNamespace(
        brief_binding=None,
        kind="editor",
        editor_payload={"base_generation": approval.target_generation_id},
        strategy=None,
        intent="x",
    )
    payload = SimpleNamespace(
        music_track_id=None, background_music=SimpleNamespace(track_id="song-calm")
    )
    with (
        patch.object(kria_runtime, "sync_session", sessions),
        patch.object(
            kria_runtime, "_append_sync_event", lambda *_a, **_k: SimpleNamespace(id=uuid.uuid4())
        ),
        patch.object(kria_runtime.KriaDraftDocument, "model_validate", return_value=document),
        patch.object(kria_runtime.EditorCommitRequest, "model_validate", return_value=payload),
        patch.object(kria_runtime, "phone_subtitled_sfx_paths_sync", lambda *_: {}),
        patch.object(kria_runtime, "prepare_editor_commit", prepare),
    ):
        claim = kria_runtime._claim_approval_dispatch(approval.id)
    assert claim is not None
    assert seen["background_music_track"] is CALM
