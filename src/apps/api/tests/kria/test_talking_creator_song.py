"""KRI-1578: the creator's own uploaded song under a phone Talking edit, mixed by Kria.

Offline replay over the real chain: the real Talking worker (`_run_generative_job` on the
subtitled harness) lays the song under the talk with the real compiler and pins the recipe; the
real editor Save (`prepare_editor_commit`) adds, changes, re-pins and removes it; the real chat
lane (`set_user_song`, the copilot's `swap_music` boundary) and the real receipt checks read the
pinned recipe. Stood in: the plan item row (read through the real `uploaded_song` predicate), the
song's storage hash (`inspect_song_asset`), and the library read the closed library must never
reach. No model call.

Ways this can fail, written down before the guards:
 C1  an uploaded song stays silent on a first render, or plays at full level over her voice;
 C2  a music ask with no upload still picks a library song, or answers nothing / "stays as filmed"
     instead of "send me the song" (EN and TR);
 C3  the receipt says "met" when the pinned recipe has no song (negative control);
 C4  "no music" loses to her upload, or a later caption Save brings the song back;
 C5  an app build below protocol 53 gets a song it would play over her voice (worker and Save);
 C6  a caption or sound Save drops her song or re-hashes it;
 C7  the Save's volume, start, end or removal does not reach the recipe; removal does not stick;
 C8  a song added at Save is not pinned; a replaced upload keeps the old generation (the device
     would 409) or an old window; a song taken off the item keeps playing;
 C9  the editor cannot see the song (`variants[].user_song`) or its controls (`user_song.*`);
 C10 chat cannot change her song, or swaps in a library song while the library is closed;
 C11 lip-sync on a Talking edit is accepted silently instead of refused in words (EN and TR);
 C12 with the flags off anything changes: the render, the Save section (still 422);
 C13 her library song from before KRI-1578 is silently replaced or lost without her upload;
 C14 attaching or keeping a song on a Talking item changes its speech fingerprint (cleanup consent).
"""

from __future__ import annotations

import uuid
from types import SimpleNamespace

import pytest
from fastapi import HTTPException

from app.agents._schemas.creator_policy import (
    USER_SONG_TALKING_LIPSYNC_CODE,
    UserSongUnavailableError,
    repair_creator_user_song,
)
from app.kria.brief import BriefRequirement
from app.kria.brief_checks import build_receipts, plan_facts_from_phone_variant
from app.kria.render_assets import RenderFingerprint, SongRenderAsset
from app.kria.reply_language import reply_language_for
from app.kria.strategy_policy import localize_known_message
from app.routes import generative_jobs as gj
from app.services import talking_music
from app.services.client_protocol import (
    SPEECH_DUCK_PROTOCOL,
    clear_request_context,
    set_client_protocol,
)
from app.services.device_render import device_status
from app.services.user_song_projection import user_song_for_variant
from app.tasks import generative_build as gb
from app.tasks.kria_runtime import _with_recipe_music
from tests.agents.test_main_creator_user_song import _render as render_prompt
from tests.agents.test_main_creator_user_song import _song_input as song_input
from tests.kria.test_talking_music_requests import MUSIC_EN, MUSIC_TR, NO_MUSIC, _Copilot
from tests.kria.test_talking_music_requests import _binding as brief_binding
from tests.routes.test_phone_subtitled_editor_commit import _enable_subtitled_editor
from tests.services.test_creator_user_song import _manifest as song_manifest
from tests.services.test_creator_user_song import _song as song_item
from tests.tasks.test_phone_subtitled_narrated_dispatch import _setup_subtitled, _words
from tests.tasks.test_phone_talking_music_worker import TREATMENT, WORDS, _pin

# Production settings first (`tests/prod_flags.snapshot.json`); each case opens what it tests.
pytestmark = pytest.mark.usefixtures("prod_profile")

VERIFIED = ["audioMix", "musicBed", "speechDuck"]
ITEM_ID = str(uuid.uuid4())
SPOKEN = [(0.5, 1.6), (4.0, 4.6)]
TALKING_LEVEL = round(10 ** (-18 / 20), 6)
SEND_SONG = (
    "I can't add songs from the music library. Send me the song you want and I'll play it "
    "quietly under your voice"
)
SEND_SONG_TR = (
    "Müzik kütüphanesinden şarkı ekleyemiyorum. İstediğin şarkıyı gönder, sesinin altında "
    "kısık çalayım"
)


@pytest.fixture(autouse=True)
def _no_request_context():
    clear_request_context()
    yield
    clear_request_context()


def _item(*, generation: int = 7, duration_s: float = 120.0, path: str | None = None, **changes):
    return SimpleNamespace(
        id=ITEM_ID,
        song_gcs_path=path or f"users/u/creation-threads/t/song-{generation}.m4a",
        song_generation=generation,
        song_duration_s=duration_s,
        song_filename="Sunday Song.m4a",
        audio_mode="original",
        edit_format="subtitled",
        **changes,
    )


class _Storage:
    """The song's storage hash (`inspect_song_asset`): records each object it hashed."""

    def __init__(self) -> None:
        self.hashed: list[tuple[str, str]] = []

    def __call__(self, path, *, asset_id, plan_item_id, expected_generation=None):
        self.hashed.append((path, str(expected_generation)))
        return SongRenderAsset(
            id=asset_id,
            plan_item_id=plan_item_id,
            generation=str(expected_generation),
            fingerprint=RenderFingerprint(sha256="b" * 64, byte_count=3_000_000),
        )


def _lanes(monkeypatch, *, flag: bool = True) -> _Storage:
    _enable_subtitled_editor(monkeypatch)
    monkeypatch.setattr(gb.settings, "talking_music_enabled", flag)
    monkeypatch.setattr(
        gb.settings,
        "phone_render_verified_features",
        [*gb.settings.phone_render_verified_features, *VERIFIED],
    )
    storage = _Storage()
    monkeypatch.setattr("app.services.phone_voiceover.inspect_song_asset", storage)

    def no_library(*_a, **_k):
        raise AssertionError("the closed library must never be read or matched")

    monkeypatch.setattr(gb, "_match_talking_song", no_library)
    monkeypatch.setattr(gb, "_talking_library_tracks", no_library)
    return storage


def _render(
    monkeypatch,
    *,
    item=None,
    reqs: tuple[BriefRequirement, ...] = (),
    protocol: int | None = SPEECH_DUCK_PROTOCOL,
    stored: dict | None = None,
    flag: bool = True,
):
    job, _snapshot, _session, _binding_ = _setup_subtitled(
        monkeypatch, transcript_words=_words(*WORDS)
    )
    storage = _lanes(monkeypatch, flag=flag)
    job.content_plan_item_id = ITEM_ID
    if reqs:
        job.assembly_plan["creator_brief_binding"] = brief_binding(*reqs)
    job.all_candidates["creator_client_protocol"] = protocol
    monkeypatch.setattr(gb, "_job_uploaded_song", lambda _job_id: talking_music.uploaded_song(item))
    monkeypatch.setattr(gb, "_persisted_variant", lambda **_: stored)
    monkeypatch.setattr(gb, "_pin_catalog_music", _pin)
    gb._run_generative_job(str(job.id))
    assert job.status == "awaiting_device", "the talk renders whatever happens to the song"
    variant = job.assembly_plan["variants"][0]
    # A phone render the device already published: the editor's lanes open on it.
    variant["output_url"] = "https://signed.test/out.mp4"
    return job, variant, storage


def _recipe(job):
    return device_status(job, "subtitled").request.recipe


def _song_clips(recipe):
    return [clip for track in recipe.tracks if track.role == "music" for clip in track.clips]


def _receipt(job, variant, req: BriefRequirement, *, language: str = "en"):
    facts = _with_recipe_music(job, variant, plan_facts_from_phone_variant(variant))
    with reply_language_for(language):
        [receipt] = build_receipts([req], facts)
    return receipt


def _save(job, *, item, read: bool = True, **sections):
    variant = job.assembly_plan["variants"][0]
    return gj.prepare_editor_commit(
        job,
        "subtitled",
        gj.EditorCommitRequest(base_generation=gj.variant_render_baseline(variant), **sections),
        user_id="owner",
        plan_item_id=ITEM_ID,
        uploaded_song=(
            talking_music.UploadedSongRead(talking_music.uploaded_song(item)) if read else None
        ),
    )


# --- C1 / C14: the first render plays her upload under her voice --------------------------------


@pytest.mark.parametrize(
    ("ask", "language"), [((), "en"), ((MUSIC_EN,), "en"), ((MUSIC_TR,), "tr")]
)
def test_the_first_render_plays_her_uploaded_song_under_her_voice(monkeypatch, ask, language):
    item = _item(duration_s=120.0)
    job, variant, storage = _render(monkeypatch, item=item, reqs=ask)
    recipe = _recipe(job)
    clips = _song_clips(recipe)
    assert [(c.timeline_start, c.source_start, c.source_duration) for c in clips] == [
        (0.0, 0.0, recipe.duration)
    ]
    assert {c.volume for c in clips} == {TALKING_LEVEL}
    assert [(w.start, w.end) for w in recipe.audio.speech_windows] == SPOKEN
    assert recipe.audio.music_asset_id is None, "the engine would play the song a second time"
    assert recipe.audio.original_volume == 1.0
    [asset] = [a for a in recipe.asset_manifest.assets if a.kind == "song"]
    assert (asset.plan_item_id, asset.generation) == (ITEM_ID, "7")
    assert storage.hashed == [(item.song_gcs_path, "7")]
    assert variant[talking_music.TALKING_SONG_FIELD]["generation"] == 7
    assert not variant.get("smart_music_treatment")
    if ask:
        assert variant["talking_music"] == {"status": "played", "source": "creator_song"}
        assert _receipt(job, variant, ask[0], language=language).status == "met"


def test_a_song_shorter_than_the_talk_loops_to_the_end(monkeypatch):
    job, _variant, _storage = _render(monkeypatch, item=_item(duration_s=4.0))
    recipe = _recipe(job)
    clips = _song_clips(recipe)
    assert [c.timeline_start for c in clips] == [0.0, 3.95, 7.9]
    assert clips[-1].timeline_start + clips[-1].source_duration == pytest.approx(recipe.duration)


def test_her_upload_replaces_a_library_song_from_before(monkeypatch):
    stored = {"smart_music_treatment": dict(TREATMENT)}
    job, variant, _storage = _render(monkeypatch, item=_item(), stored=stored)
    assert [
        a.kind for a in _recipe(job).asset_manifest.assets if a.id.startswith(("song", "music"))
    ] == ["song"]
    assert variant["smart_music_treatment"] is None


def test_a_library_song_from_before_keeps_playing_without_an_upload(monkeypatch):
    stored = {"smart_music_treatment": dict(TREATMENT)}
    job, variant, _storage = _render(monkeypatch, item=None, stored=stored)
    [asset] = [a for a in _recipe(job).asset_manifest.assets if a.id.startswith("music-")]
    assert asset.kind == "library"
    assert variant["smart_music_treatment"]["track_id"] == TREATMENT["track_id"]


def test_the_talking_speech_fingerprint_moves_with_audio_mode():
    """The premise for keeping a Talking item at `original`: its speech fingerprint (what the
    creator's cleanup consent is bound to) changes if `audio_mode` does."""
    from app.services.active_narration_source import resolve_active_narration_source
    from tests.services.test_active_narration_source import media, request

    take = (media("take.mp4", coverage=0.99),)
    original, song = (
        resolve_active_narration_source(
            request(edit_format="subtitled", audio_mode=mode, clips=take)
        ).source
        for mode in ("original", "song")
    )
    assert original is not None and song is not None
    assert original.source_policy_fingerprint != song.source_policy_fingerprint


def test_a_talking_items_policy_stays_original_so_its_speech_fingerprint_holds():
    from app.services.speech_cleanup_decision import resolve_next_audio_mode

    talking = SimpleNamespace(
        song_gcs_path="s.m4a", voiceover_gcs_path=None, edit_format="subtitled"
    )
    montage = SimpleNamespace(song_gcs_path="s.m4a", voiceover_gcs_path=None, edit_format="montage")
    strategy = SimpleNamespace(audio_strategy="user_song")
    assert resolve_next_audio_mode(strategy, talking) == "original"
    assert resolve_next_audio_mode(strategy, montage) == "song"


# --- C2 / C3: a music ask with no upload asks for the song --------------------------------------


@pytest.mark.parametrize(
    ("ask", "language", "reason"), [(MUSIC_EN, "en", SEND_SONG), (MUSIC_TR, "tr", SEND_SONG_TR)]
)
def test_a_music_ask_without_an_upload_never_picks_a_library_song_and_asks_for_hers(
    monkeypatch, ask, language, reason
):
    job, variant, storage = _render(monkeypatch, item=None, reqs=(ask,))
    assert _song_clips(_recipe(job)) == []
    assert variant["talking_music"] == {"status": "not_played", "reason": "no_upload"}
    receipt = _receipt(job, variant, ask, language=language)
    assert (receipt.status, receipt.reason) == ("not_possible", reason)
    assert storage.hashed == []


def test_a_receipt_never_says_met_when_the_pinned_recipe_has_no_song(monkeypatch):
    """Negative control: the same render with her song taken out of the pinned recipe."""
    from app.kria.device_render import recipe_digest
    from app.kria.recipes_v2 import EditRecipeV2

    job, variant, _storage = _render(monkeypatch, item=_item(), reqs=(MUSIC_EN,))
    assert _receipt(job, variant, MUSIC_EN).status == "met"
    request = job.assembly_plan["_device_render_v1"]["subtitled"]["status"]["request"]
    request["recipe"]["tracks"] = [
        t for t in request["recipe"]["tracks"] if t.get("role") != "music"
    ]
    request["recipe"]["audio"]["speech_windows"] = []
    request["recipe"]["audio"].pop("speech_duck_gain", None)
    request["identity"]["recipe_digest"] = recipe_digest(
        EditRecipeV2.model_validate(request["recipe"])
    )
    assert variant[talking_music.TALKING_SONG_FIELD], "the stored choices still name her song"
    assert _receipt(job, variant, MUSIC_EN).status != "met"


# --- C4 / C5 / C12: no music, old builds, flags off ----------------------------------------------


def test_no_music_wins_over_her_upload_and_holds_through_a_later_caption_save(monkeypatch):
    item = _item()
    job, variant, storage = _render(monkeypatch, item=item, reqs=(NO_MUSIC, MUSIC_EN))
    assert _song_clips(_recipe(job)) == []
    assert talking_music.TALKING_SONG_FIELD not in variant
    _save(job, item=item, caption_meta={"hidden": False})
    assert _song_clips(_recipe(job)) == []
    assert storage.hashed == []


def test_a_save_bound_to_a_later_no_music_takes_her_song_off_for_good(monkeypatch):
    item = _item()
    job, _variant, _storage = _render(monkeypatch, item=item)
    assert _song_clips(_recipe(job))
    job.assembly_plan["creator_brief_binding"] = brief_binding(MUSIC_EN, NO_MUSIC)
    _save(job, item=item, caption_meta={"hidden": False})
    assert _song_clips(_recipe(job)) == []
    assert job.assembly_plan["variants"][0][talking_music.TALKING_SONG_FIELD] == {"removed": True}
    job.assembly_plan["creator_brief_binding"] = brief_binding(MUSIC_EN)
    _save(job, item=item, caption_meta={"hidden": False})
    assert _song_clips(_recipe(job)) == [], "a withdrawn no-music never re-adds a removed song"


def test_the_same_caption_save_keeps_her_song_without_a_no_music_ask(monkeypatch):
    """Control for the test above."""
    item = _item()
    job, _variant, _storage = _render(monkeypatch, item=item)
    job.assembly_plan["creator_brief_binding"] = brief_binding(MUSIC_EN)
    _save(job, item=item, caption_meta={"hidden": False})
    assert _song_clips(_recipe(job))


def test_an_app_build_below_protocol_53_gets_no_song_and_is_told_why(monkeypatch):
    job, variant, _storage = _render(
        monkeypatch, item=_item(), reqs=(MUSIC_EN,), protocol=SPEECH_DUCK_PROTOCOL - 1
    )
    assert _song_clips(_recipe(job)) == []
    assert variant["talking_music"]["reason"] == "app_too_old"


def test_an_unstamped_or_old_build_without_an_ask_still_records_why_her_song_is_missing(
    monkeypatch,
):
    job, variant, storage = _render(monkeypatch, item=_item(), protocol=None)
    assert _song_clips(_recipe(job)) == []
    assert variant["talking_music"] == {
        "status": "not_played",
        "reason": "app_too_old",
        "source": "creator_song",
    }
    assert storage.hashed == []


def test_flags_off_an_upload_changes_nothing(monkeypatch):
    from app.kria.device_render import recipe_digest

    with_song, _v, storage = _render(monkeypatch, item=_item(), flag=False)
    monkeypatch.undo()
    without, _v2, _s = _render(monkeypatch, item=None, flag=False)
    assert recipe_digest(_recipe(with_song)) == recipe_digest(_recipe(without))
    assert storage.hashed == []
    with pytest.raises(HTTPException) as refused:
        gj.require_guided_story_editor_commit(
            with_song,
            "subtitled",
            gj.EditorCommitRequest(base_generation="x", user_song={"volume": 0.5}),
        )
    assert refused.value.status_code == 422


# --- C6 / C7 / C8: the Save keeps, changes, adds, re-pins and removes her song -----------------


def test_a_caption_or_sound_save_keeps_her_song_without_hashing_it_again(monkeypatch):
    item = _item()
    job, _variant, storage = _render(monkeypatch, item=item)
    before = _song_clips(_recipe(job))
    _save(job, item=item, caption_meta={"hidden": False})
    _save(job, item=item, read=False, caption_meta={"hidden": False})
    assert _song_clips(_recipe(job)) == before
    assert len(storage.hashed) == 1


def test_the_save_moves_her_song_and_its_level_into_the_recipe(monkeypatch):
    item = _item()
    job, variant, _storage = _render(monkeypatch, item=item)
    _save(job, item=item, user_song={"volume": 0.4, "window_start_s": 30.0, "window_end_s": 33.0})
    variant = job.assembly_plan["variants"][0]
    clips = _song_clips(_recipe(job))
    assert [(c.timeline_start, c.source_start) for c in clips][:2] == [(0.0, 30.0), (3.0, 30.0)]
    assert {c.volume for c in clips} == {0.4}
    stored = variant[talking_music.TALKING_SONG_FIELD]
    assert (stored["volume"], stored["window_start_s"], stored["window_end_s"]) == (0.4, 30.0, 33.0)
    projected = user_song_for_variant(job, variant, song_filename="Sunday Song.m4a")
    assert projected == {
        "title": "Sunday Song",
        "mode": "background",
        "duration_s": 120.0,
        "window_start_s": 30.0,
        "window_end_s": 33.0,
        "volume": 0.4,
    }
    assert set(gj._editor_capabilities(job, variant)["user_song"]) == {
        "volume",
        "window",
        "remove",
        "trim",
    }


def test_removing_her_song_takes_it_off_and_a_later_save_never_brings_it_back(monkeypatch):
    item = _item()
    job, variant, _storage = _render(monkeypatch, item=item)
    _save(job, item=item, user_song={"removed": True})
    variant = job.assembly_plan["variants"][0]
    assert _song_clips(_recipe(job)) == []
    assert variant[talking_music.TALKING_SONG_FIELD] == {"removed": True}
    assert user_song_for_variant(job, variant, song_filename=None) is None
    _save(job, item=item, caption_meta={"hidden": False})
    assert _song_clips(_recipe(job)) == []


def test_a_song_uploaded_after_the_render_is_added_by_a_save_and_pinned_once(monkeypatch):
    job, variant, storage = _render(monkeypatch, item=None)
    assert _song_clips(_recipe(job)) == []
    item = _item(generation=9)
    _save(job, item=item, caption_meta={"hidden": False})
    assert _song_clips(_recipe(job)) == [], "a Save never adds a song by itself"
    _save(job, item=item, user_song={})
    [asset] = [a for a in _recipe(job).asset_manifest.assets if a.kind == "song"]
    assert asset.generation == "9"
    assert storage.hashed == [(item.song_gcs_path, "9")]
    variant = job.assembly_plan["variants"][0]
    assert variant[talking_music.TALKING_SONG_FIELD]["volume"] == TALKING_LEVEL


def test_a_replaced_upload_is_re_pinned_with_her_level_and_a_fresh_window(monkeypatch):
    first = _item(generation=7)
    job, variant, storage = _render(monkeypatch, item=first)
    _save(job, item=first, user_song={"volume": 0.3, "window_start_s": 40.0})
    replaced = _item(generation=8, duration_s=90.0)
    _save(job, item=replaced, caption_meta={"hidden": False})
    [asset] = [a for a in _recipe(job).asset_manifest.assets if a.kind == "song"]
    assert asset.generation == "8"
    assert storage.hashed[-1] == (replaced.song_gcs_path, "8")
    stored = job.assembly_plan["variants"][0][talking_music.TALKING_SONG_FIELD]
    assert (stored["generation"], stored["volume"], stored["window_start_s"]) == (8, 0.3, 0.0)


def test_a_song_taken_off_the_item_stops_playing_on_the_next_save(monkeypatch):
    item = _item()
    job, variant, _storage = _render(monkeypatch, item=item)
    _save(job, item=None, caption_meta={"hidden": False})
    assert _song_clips(_recipe(job)) == []
    assert talking_music.TALKING_SONG_FIELD not in job.assembly_plan["variants"][0]


def test_an_older_build_cannot_save_a_song_over_her_voice_but_can_remove_it(monkeypatch):
    item = _item()
    job, _variant, _storage = _render(monkeypatch, item=item)
    set_client_protocol(SPEECH_DUCK_PROTOCOL - 1)
    with pytest.raises(HTTPException) as refused:
        _save(job, item=item, user_song={"volume": 0.5})
    assert refused.value.status_code == 426
    assert refused.value.detail["feature"] == "speech_duck"
    _save(job, item=item, user_song={"removed": True})
    assert _song_clips(_recipe(job)) == []


def test_a_new_library_song_is_refused_while_the_library_is_closed(monkeypatch):
    item = _item()
    job, _variant, _storage = _render(monkeypatch, item=item)
    with pytest.raises(HTTPException) as refused:
        _save(job, item=item, background_music={"track_id": "song-1", "gain_db": -18})
    assert refused.value.status_code == 422
    assert refused.value.detail["code"] == "unsupported_phone_edit"
    assert "Send the song you want" in refused.value.detail["reason"]


# --- C10: chat ---------------------------------------------------------------------------------


def _chat_turn(job, item, utterance: str, ops: list[dict], *, context: dict, language: str):
    """One chat turn on the rendered edit: the real snapshot and copilot agent (authored answer),
    the real compile and the real Talking Save, from a protocol 53 build."""
    from app.agents.edit_copilot import EditCopilotAgent, EditCopilotInput
    from app.services.kria_editor_ops import (
        build_editor_snapshot,
        compile_editor_ops,
        project_editor_draft,
    )

    set_client_protocol(SPEECH_DUCK_PROTOCOL)
    base = project_editor_draft(job.assembly_plan["variants"][0], {}, job)
    snapshot = build_editor_snapshot(job, base, clip_context=context)
    client = _Copilot(ops)
    with reply_language_for(language):
        output = EditCopilotAgent(client).run(
            EditCopilotInput(utterance=utterance, variant_snapshot=snapshot)
        )
    if output.ops:
        payload = compile_editor_ops(job, base, output.ops).payload
        gj.prepare_editor_commit(
            job,
            "subtitled",
            payload,
            user_id="owner",
            plan_item_id=ITEM_ID,
            uploaded_song=talking_music.UploadedSongRead(talking_music.uploaded_song(item)),
        )
    return snapshot, output, client.prompts


@pytest.mark.parametrize(
    ("utterance", "language"),
    [("start my song at 0:20 and a bit louder", "en"), ("şarkım 0:20'den başlasın", "tr")],
)
def test_chat_changes_and_removes_her_song_on_a_talking_edit(monkeypatch, utterance, language):
    item = _item()
    job, _variant, _storage = _render(monkeypatch, item=item)
    snapshot, output, [prompt] = _chat_turn(
        job,
        item,
        utterance,
        [{"op": "set_user_song", "volume": 0.6, "window_start_s": 20.0}],
        context={"music_library_closed": True},
        language=language,
    )
    assert snapshot["user_song"]["mode"] == "background" and "YOUR SONG" in prompt
    assert [op["op"] for op in output.ops] == ["set_user_song"]
    first = _song_clips(_recipe(job))[0]
    assert (first.source_start, first.volume) == (20.0, 0.6)
    _chat_turn(
        job,
        item,
        "şarkıyı kaldır" if language == "tr" else "remove my song",
        [{"op": "set_user_song", "remove": True}],
        context={"music_library_closed": True},
        language=language,
    )
    assert _song_clips(_recipe(job)) == []


@pytest.mark.parametrize(("utterance", "language"), [("no music", "en"), ("müziksiz olsun", "tr")])
def test_chat_no_music_over_her_song_takes_her_song_off(monkeypatch, utterance, language):
    item = _item()
    job, _variant, _storage = _render(monkeypatch, item=item)
    _snapshot, output, _prompts = _chat_turn(
        job,
        item,
        utterance,
        [{"op": "remove_music"}],
        context={"music_library_closed": True},
        language=language,
    )
    assert [op["op"] for op in output.ops] == ["remove_music"]
    assert _song_clips(_recipe(job)) == []
    assert job.assembly_plan["variants"][0][talking_music.TALKING_SONG_FIELD] == {"removed": True}


@pytest.mark.parametrize(
    ("utterance", "language"), [("here's my song, add it", "en"), ("şarkımı ekle", "tr")]
)
def test_chat_puts_a_song_she_sent_after_the_render_under_her_voice(
    monkeypatch, utterance, language
):
    job, _variant, storage = _render(monkeypatch, item=None)
    item = _item(generation=11)
    snapshot, output, [prompt] = _chat_turn(
        job,
        item,
        utterance,
        [{"op": "set_user_song"}],
        context={"music_library_closed": True, "uploaded_song": {"duration_s": 120.0}},
        language=language,
    )
    assert snapshot["user_song"]["on_edit"] is False
    assert "not on this edit yet" in prompt
    assert [op["op"] for op in output.ops] == ["set_user_song"]
    clips = _song_clips(_recipe(job))
    assert clips and {c.volume for c in clips} == {TALKING_LEVEL}
    assert storage.hashed == [(item.song_gcs_path, "11")]


def test_a_talking_save_returns_a_prep_a_chat_turn_can_store(monkeypatch):
    """The chat Save stores its prep in a JSON column: the item read never rides in it."""
    import json

    item = _item()
    job, _variant, _storage = _render(monkeypatch, item=item)
    prep = _save(job, item=item, user_song={"volume": 0.5})
    assert "uploaded_song" not in prep
    json.dumps(prep, default=str)
    assert item.song_gcs_path not in json.dumps(prep, default=str)


@pytest.mark.parametrize(
    ("utterance", "language", "note"),
    [
        ("add some chill music", "en", SEND_SONG),
        ("arkaya sakin bir müzik ekle", "tr", SEND_SONG_TR),
    ],
)
def test_chat_answers_a_music_ask_with_send_me_the_song_while_the_library_is_closed(
    monkeypatch, utterance, language, note
):
    job, _variant, _storage = _render(monkeypatch, item=None)
    snapshot, output, [prompt] = _chat_turn(
        job,
        None,
        utterance,
        [{"op": "swap_music", "track_id": "song-calm"}],
        context={"music_library_closed": True},
        language=language,
    )
    assert snapshot["music"]["library_closed"] is True and "library_closed=True" in prompt
    assert output.ops == []
    assert note in output.reply
    assert _song_clips(_recipe(job)) == []


# --- C11: lip-sync on a Talking edit -------------------------------------------------------------


@pytest.mark.parametrize("language", ["en", "tr"])
def test_lipsync_on_a_talking_edit_is_refused_in_words_and_background_plays_under_her_voice(
    monkeypatch, language
):
    from app.agents._schemas.creator_agent import CreativeStrategy

    monkeypatch.setattr(gb.settings, "talking_music_enabled", True)
    manifest = SimpleNamespace(
        capabilities={
            "phone_source_audio": SimpleNamespace(available=True),
            "user_song": SimpleNamespace(available=True),
        },
        has_user_song=True,
        chosen_format=None,
    )
    base = CreativeStrategy.model_validate(
        {"edit_format": "subtitled", "audio_strategy": "user_song", "song_sync": "lipsync"}
    )
    with pytest.raises(UserSongUnavailableError) as refused:
        repair_creator_user_song(manifest, base)
    assert refused.value.code == USER_SONG_TALKING_LIPSYNC_CODE
    with reply_language_for(language):
        question = localize_known_message(str(refused.value))
    assert ("Lip-sync" if language == "en" else "Dudak senkronu") in question
    repaired, notices = repair_creator_user_song(
        manifest, base.model_copy(update={"song_sync": "background"})
    )
    assert (repaired.audio_strategy, repaired.song_sync) == ("original_audio", None)
    assert notices == ["I'll play your song quietly under your voice."]


# --- C11 / C12: the planner's manifest, prompt and draft receipt ---------------------------------


def _song_settings(monkeypatch, *, talking: bool):
    """The capability profile `tests/services/test_creator_user_song.py` pins (every feature
    on, the song lane on), with the Talking song lane ``talking``."""
    from app.services import creator_capabilities as capabilities

    settings = capabilities.settings
    monkeypatch.setattr(settings, "guided_edit_capability_enabled", True)
    for name in capabilities._FEATURE_SETTINGS.values():
        monkeypatch.setattr(settings, name, True, raising=False)
    monkeypatch.setattr(settings, "user_song_montage_enabled", True)
    monkeypatch.setattr(settings, "talking_music_enabled", talking)
    monkeypatch.setattr(settings, "phone_render_verified_features", VERIFIED)
    return settings


@pytest.fixture
def talking_lane(monkeypatch):
    return _song_settings(monkeypatch, talking=True)


def test_a_talking_manifest_offers_her_song_only_on_the_talking_song_lane(monkeypatch):
    _song_settings(monkeypatch, talking=False)
    closed = song_manifest(song=song_item(), edit_format="subtitled")
    assert closed.capabilities["user_song"].reason_code == "unsupported_format"
    assert closed.has_user_song is False
    _song_settings(monkeypatch, talking=True)
    opened = song_manifest(song=song_item(), edit_format="subtitled")
    assert opened.capabilities["user_song"].available is True
    assert opened.has_user_song is True


def test_the_planner_learns_the_talking_song_rule_not_the_montage_one(talking_lane):
    from app.agents import main_creator as main_creator_module

    talking = song_input()
    talking = talking.model_copy(
        update={
            "capability_manifest": talking.capability_manifest.model_copy(
                update={"edit_format": "subtitled"}
            )
        }
    )
    prompt = render_prompt(talking)
    assert main_creator_module._USER_SONG_TALKING_PROMPT_SECTION in prompt
    assert main_creator_module._USER_SONG_PROMPT_SECTION not in prompt
    assert main_creator_module._USER_SONG_PROMPT_SECTION in render_prompt(song_input())


@pytest.mark.parametrize(
    ("song", "language", "status", "reason"),
    [
        (True, "en", "partial", "I'll play your song quietly under your voice"),
        (True, "tr", "partial", "Şarkını sesinin altında kısık çalarım"),
        (False, "en", "not_possible", SEND_SONG),
        (False, "tr", "not_possible", SEND_SONG_TR),
    ],
)
def test_a_talking_draft_promises_her_song_or_asks_for_it(
    talking_lane, song, language, status, reason
):
    import dataclasses

    from app.kria.brief_checks import plan_facts_from_strategy

    manifest = song_manifest(song=song_item() if song else None, edit_format="subtitled")
    facts = plan_facts_from_strategy(
        {"edit_format": "subtitled", "audio_strategy": "original_audio"}, manifest=manifest
    )
    with reply_language_for(language):
        [receipt] = build_receipts([MUSIC_EN], dataclasses.replace(facts, phone_talking=True))
    assert (receipt.status, receipt.reason) == (status, reason)


# --- the upload gate: a Talking song is never taken where it would stay silent -----------------


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("edit_format", "talking_on", "refused"),
    [("subtitled", False, True), ("subtitled", True, False), ("montage", False, False)],
)
async def test_a_song_upload_onto_a_talking_item_needs_the_talking_song_lane(
    monkeypatch, edit_format, talking_on, refused
):
    from unittest.mock import AsyncMock, Mock

    import app.routes.creation_threads as routes
    from tests.routes.test_creation_threads import _request

    _song_settings(monkeypatch, talking=talking_on)
    monkeypatch.setattr(routes, "_user_song_enabled", lambda *_a, **_k: True)
    set_client_protocol(SPEECH_DUCK_PROTOCOL)
    user = SimpleNamespace(id=uuid.uuid4())
    thread = SimpleNamespace(
        id=uuid.uuid4(),
        creator_id=user.id,
        status="active",
        active_plan_item_id=uuid.uuid4(),
        state={},
    )
    item = SimpleNamespace(edit_format=edit_format, clip_gcs_paths=[], song_gcs_path=None)
    monkeypatch.setattr(routes, "_load", AsyncMock(return_value=thread))
    db = Mock()
    db.get = AsyncMock(return_value=item)
    body = routes.UploadBody(
        files=[
            routes.UploadFile(
                filename="song.m4a",
                content_type="audio/mp4",
                file_size_bytes=10,
                client_upload_id="song-1",
                role="song",
            )
        ]
    )
    try:
        await routes.upload_urls(_request(), str(thread.id), body, user, db)
    except HTTPException as exc:
        detail = exc.detail
    except TypeError:
        detail = None  # past every gate: the stand-in session has no reservation to write
    talking_refusal = "Your own song can't play under a Talking video yet"
    assert (talking_refusal in str(detail)) is refused
