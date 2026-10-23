"""KRI-821: "mute the camera sound under my voice" is delivered end to end.

Failure modes (written before the fix):

F1  The approved brief says "mute the crowd noise under my voice", yet the narrated render
    still plays the footage at the 25 % bed (only the web item slider set it).
F2  The receipt tells the creator to "lower it in the editor's mix" on a narrated edit,
    where `mix` is closed and iOS has no bed control: an unexecutable recovery.
F3  The phone Voiceover editor offers no Original-audio control, or offers one Save
    refuses (422 "Background sound ... item page"), or the Save is accepted but the
    recompiled recipe ignores the level / also silences the narration.
F4  An old app build (protocol < 15) is handed the new key and shows a row at a wrong
    level; a cloud render is offered a phone-only control.
F5  The saved level is lost on reopen (the variant row does not carry it) or a stale Save
    (old base generation) overwrites the newer one.
F6  "Keep the crowd noise low" (not a mute) is muted anyway; a mute without a voiceover lane
    changes nothing it should not.
F7  A montage Voiceover receipt says the footage "still plays softly" although the recipe
    plays it at 0.
F8  Turkish copy drifts from English.

The journey writes its evidence (request, brief, capabilities, recipes before/after,
receipts) to ``$KRI821_REPORT`` when set (CI: a tmp file).
"""

from __future__ import annotations

import json
import os
import uuid
from pathlib import Path

import pytest
from fastapi import HTTPException

from app.kria.brief import BriefRequirement, CreativeBrief
from app.kria.brief_checks import build_receipts, plan_facts_from_phone_variant
from app.kria.device_render import make_device_request
from app.kria.footage_mute_ask import approved_bed_level, brief_footage_mute
from app.kria.reply_language import reply_language_for
from app.pipeline.phone_narrated_plan import compile_phone_narrated_plan
from app.routes import generative_jobs as gj
from app.services.client_protocol import clear_request_context, set_client_protocol
from app.services.device_render import device_status, pin_device_request
from app.tasks.generative_build import _specs_for_archetype
from tests.pipeline.test_phone_narrated_plan import _narration, _step
from tests.routes.test_phone_narrated_editor_commit import _bindings, phone_job

REQUEST = "Mute the crowd noise under my voice, I only want my narration"
# As the brief writer records it (main_creator v77): the typed mute decides, not the words.
MUTE = BriefRequirement(
    id="r1",
    kind="audio",
    scope="global",
    description=REQUEST,
    facts={"audio_intent": "source_muted"},
)
KEEP_LOW = BriefRequirement(
    id="r2", kind="audio", scope="global", description="Keep the crowd noise low under my voice"
)

_report: dict = {"ticket": "KRI-821", "request": REQUEST}


def _brief(*reqs: BriefRequirement) -> CreativeBrief:
    return CreativeBrief(version=1, requirements=list(reqs))


def _receipt(variant: dict, req: BriefRequirement, *, control: str | None = None):
    import dataclasses

    facts = dataclasses.replace(
        plan_facts_from_phone_variant(variant), original_audio_control=control
    )
    (receipt,) = build_receipts([req], facts, include_unchecked=True)
    return receipt


@pytest.fixture(autouse=True)
def _no_request_context():
    clear_request_context()
    yield
    clear_request_context()


@pytest.fixture(scope="module", autouse=True)
def _write_report():
    yield
    target = os.environ.get("KRI821_REPORT")
    if target:
        Path(target).parent.mkdir(parents=True, exist_ok=True)
        Path(target).write_text(json.dumps(_report, indent=2, default=str))


def test_approved_mute_renders_silent_footage_on_both_narrated_renderers():
    brief = _brief(MUTE)
    assert brief_footage_mute(brief).requirement_ids == ("r1",)
    # F1: the approved brief wins over the item's slider level and Kria's default.
    assert approved_bed_level(brief, 0.4) == 0.0
    assert approved_bed_level(brief, None) == 0.0
    # F6: "keep it low" is not a mute; no ask keeps the item level.
    assert brief_footage_mute(_brief(KEEP_LOW)) is None
    assert approved_bed_level(_brief(KEEP_LOW), 0.4) == 0.4
    assert approved_bed_level(None, None) is None

    # Cloud narrated: the spec's bed is what `_mix_user_voiceover` plays.
    (spec,) = _specs_for_archetype("narrated", None, voiceover_bed_level=0.0)
    assert spec["voiceover_bed_level"] == 0.0

    # Phone narrated: `_run_phone_narrated_job` converts bed -> mix = 1 - bed.
    recipe = compile_phone_narrated_plan(
        [_step("s0", "c0", start_s=0.0, end_s=4.0), _step("s1", "c1", start_s=4.0, end_s=6.0)],
        _bindings(),
        _narration(duration_s=6.0),
        voiceover_duration_s=6.0,
        mix=1.0 - approved_bed_level(brief, None),
        caption_cues=[],
        caption_style="sentence",
        target_lufs=-14.0,
        duck_footage_bed=True,
    )
    assert recipe.audio.original_volume == 0.0
    narration = next(t for t in recipe.tracks if t.id == "narration")
    assert [clip.volume for clip in narration.clips] == [1.0]  # F11: the voice is untouched
    _report["brief"] = brief.model_dump(mode="json")
    _report["phone_narrated_recipe_audio"] = recipe.audio.model_dump(mode="json")
    _report["cloud_narrated_spec_bed"] = spec["voiceover_bed_level"]


def test_dispatch_passes_the_approved_mute_to_the_job(monkeypatch):
    from app.kria.brief_binding import BriefBinding
    from tests.tasks import test_content_plan_build as cpb

    def run(brief: CreativeBrief, item_level: float | None) -> dict:
        thread_id = uuid.uuid4()
        binding = BriefBinding.create(thread_id, brief, media_snapshot={}).model_dump(mode="json")
        holder: dict = {}
        original_setup = cpb._guided_voiceover_dispatch_setup

        def setup(*args, **kwargs):
            result = original_setup(*args, **kwargs)
            result[0].voiceover_bed_level = item_level
            holder["session"] = result[2]
            return result

        def before() -> None:
            holder["session"].execute.return_value.scalar_one_or_none.return_value = type(
                "Thread", (), {"id": thread_id, "state": None}
            )()

        monkeypatch.setattr(cpb, "_guided_voiceover_dispatch_setup", setup)
        out = cpb._dispatch_guided_voiceover_cleanup(
            monkeypatch,
            cleanup_choice=None,
            narration_for=lambda *_a: {},
            before_dispatch=before,
            dispatch_kwargs={"creator_brief_binding": binding},
            thread_id=thread_id,
        )
        return out.mock_build.call_args.kwargs

    assert run(_brief(MUTE), 0.4)["voiceover_bed_level"] == 0.0
    assert run(_brief(KEEP_LOW), 0.4)["voiceover_bed_level"] == 0.4


def test_phone_narrated_original_audio_control_saves_reopens_and_receipts(monkeypatch):
    job = phone_job(monkeypatch)
    row = job.assembly_plan["variants"][0]
    # As `_run_phone_narrated_job` now leaves it after an approved mute.
    recipe = device_status(job, "narrated").request.recipe
    muted = recipe.model_copy(
        update={"audio": recipe.audio.model_copy(update={"original_volume": 0.0})}
    )
    pin_device_request(
        job,
        make_device_request(job_id=job.id, variant_id="narrated", revision=2, recipe=muted),
        base_generation="first",
    )
    row.update(voiceover_bed_level=0.0, original_audio_level=0.0)

    # F7/F2: the muted render answers the mute ask.
    assert _receipt(row, MUTE).status == "met"

    # F4: the capability follows the app build and the renderer.
    set_client_protocol(15)
    caps_new = gj._editor_capabilities(job, row)
    set_client_protocol(14)
    caps_old = gj._editor_capabilities(job, row)
    clear_request_context()
    cloud_row = {**row, "render_destination": "cloud"}
    caps_cloud = gj._editor_capabilities(job, cloud_row)
    assert caps_new["original_audio"] == {"editable": True, "reason": None}
    assert caps_new["mix"] is False
    assert "original_audio" not in caps_old  # missing = unavailable on old builds
    assert caps_cloud["original_audio"] == {
        "editable": False,
        "reason": "original_audio_phone_only",
    }
    assert caps_new.keys() - {"original_audio"} <= caps_old.keys() | {"original_audio"}

    # F3: Save `mix.original_level` (what iOS commitMix sends when `mix` is closed).
    from tests.routes.test_phone_narrated_editor_commit import save

    before = device_status(job, "narrated").request
    save(job, mix={"original_level": 0.3})
    after = device_status(job, "narrated").request
    assert after.identity.recipe_revision == before.identity.recipe_revision + 1
    assert after.recipe.audio.original_volume == pytest.approx(0.3)
    assert after.recipe.audio.narration_asset_id == before.recipe.audio.narration_asset_id
    assert [t.model_dump() for t in after.recipe.tracks] == [
        t.model_dump() for t in before.recipe.tracks
    ]
    # F5: reopen reads the saved level from the variant row.
    saved_row = job.assembly_plan["variants"][0]
    assert saved_row["original_audio_level"] == pytest.approx(0.3)
    assert saved_row["original_audio_level_source"] == "editor"
    # A Save on the old generation is refused, so it cannot overwrite the newer level.
    with pytest.raises(HTTPException):
        save(job, mix={"original_level": 0.9})
    assert device_status(job, "narrated").request.recipe.audio.original_volume == pytest.approx(0.3)

    # A music level is still not a phone Voiceover control (refused, not dropped).
    with pytest.raises(HTTPException):
        gj.prepare_editor_commit(
            job,
            "narrated",
            gj.EditorCommitRequest(
                base_generation=str(saved_row["render_generation_id"]),
                mix={"music_level": 0.5, "original_level": 0.2},
            ),
            user_id="owner",
            plan_item_id="item",
        )

    # F2: the receipt now reports the saved 30 % and names only executable recovery.
    in_editor = _receipt(saved_row, MUTE, control="editor")
    by_chat = _receipt(saved_row, MUTE, control="chat")
    none = _receipt(saved_row, MUTE, control=None)
    assert {r.status for r in (in_editor, by_chat, none)} == {"partial"}
    assert "Sounds > Original audio" in in_editor.reason
    assert "tell me to mute it" in by_chat.reason and "Sounds" not in by_chat.reason
    assert "no control" in none.reason
    for receipt in (in_editor, by_chat, none):
        assert "editor's mix" not in receipt.reason
        assert "item page" not in receipt.reason
    with reply_language_for("tr"):
        tr = _receipt(saved_row, MUTE, control="editor")
    assert "Sesler > Orijinal ses" in tr.reason and "editor" not in tr.reason  # F8

    # The live recovery read: a worker (no app build) never names the editor row.
    assert gj.original_audio_recovery(job, saved_row) == "chat"
    set_client_protocol(15)
    assert gj.original_audio_recovery(job, saved_row) == "editor"
    set_client_protocol(14)
    assert gj.original_audio_recovery(job, saved_row) == "chat"
    clear_request_context()
    assert gj.original_audio_recovery(job, cloud_row) is None

    _report["capabilities"] = {
        "protocol_15": caps_new.get("original_audio"),
        "protocol_14": caps_old.get("original_audio"),
        "cloud": caps_cloud.get("original_audio"),
    }
    _report["save"] = {
        "before_revision": before.identity.recipe_revision,
        "after_revision": after.identity.recipe_revision,
        "before_original_volume": before.recipe.audio.original_volume,
        "after_original_volume": after.recipe.audio.original_volume,
        "reopen_original_audio_level": saved_row["original_audio_level"],
    }
    _report["receipts"] = {
        "editor": in_editor.model_dump(mode="json"),
        "chat": by_chat.model_dump(mode="json"),
        "none": none.model_dump(mode="json"),
        "tr": tr.model_dump(mode="json"),
    }


def test_copilot_offers_the_footage_level_on_a_phone_voiceover_edit(monkeypatch):
    from app.services.kria_editor_ops import _allowed_families

    job = phone_job(monkeypatch)
    row = job.assembly_plan["variants"][0]
    row["original_audio_level"] = 0.0
    assert "music" in _allowed_families(job, row)


# F12 (review): the family opens only for the footage level, yet the copilot is shown a
#     music_level and emits set_mix{music_level} that Save always refuses.
def test_copilot_cannot_stage_a_music_level_the_voiceover_save_refuses(monkeypatch):
    from app.agents.edit_copilot import _ParseState
    from app.agents.editor_ops_v2.audio import validate_set_mix
    from app.services.kria_editor_ops import build_editor_snapshot

    job = phone_job(monkeypatch)
    job.all_candidates = None
    row = job.assembly_plan["variants"][0]
    row["original_audio_level"] = 0.25
    snapshot = build_editor_snapshot(job, row)
    assert "music_level" not in snapshot["mix"]
    assert snapshot["mix"]["music_level_locked"] is True
    assert snapshot["mix"]["original_level"] == 0.25
    state = _ParseState(1.0)
    assert validate_set_mix({"op": "set_mix", "music_level": 0.8}, snapshot, state) is None
    ok = validate_set_mix({"op": "set_mix", "original_level": 0.0}, snapshot, _ParseState(1.0))
    assert ok == {"op": "set_mix", "original_level": 0.0}


# F13 (review): a typed source_muted whose approved description says "keep it lower"
#     (no mute wording) must never silence the footage.
def test_a_typed_mute_needs_mute_wording_in_the_approved_text():
    mislabeled = BriefRequirement(
        id="r5",
        kind="audio",
        scope="global",
        description="Keep the crowd noise lower under the voice",
        facts={"audio_intent": "source_muted"},
    )
    assert brief_footage_mute(_brief(mislabeled)) is None
    assert approved_bed_level(_brief(mislabeled), 0.4) == 0.4
    turkish = mislabeled.model_copy(update={"description": "Kamera sesini kapat"})
    assert brief_footage_mute(_brief(turkish)) is not None


# F14 (review): a variant rendered before the level was stamped gets no editable row
#     (it would open at 100 % while the footage really plays at the 25 % bed).
def test_unstamped_voiceover_variant_gets_no_original_audio_row(monkeypatch):
    job = phone_job(monkeypatch)
    row = dict(job.assembly_plan["variants"][0])
    row.pop("original_audio_level", None)
    assert gj._voiceover_original_audio_capability(row) == {
        "editable": False,
        "reason": "original_audio_level_unknown",
    }
    assert gj.original_audio_recovery(job, row) is None


def test_montage_voiceover_receipt_reads_the_pinned_footage_level():
    row = {
        "variant_id": "voiceover_only",
        "resolved_archetype": "voiceover",
        "render_destination": "device",
        "original_audio_level": 0.0,
        "ok": False,
    }
    assert _receipt(row, MUTE).status == "met"
    loud = {**row, "original_audio_level": 1.0}
    receipt = _receipt(loud, MUTE, control="chat")
    assert receipt.status == "partial" and "full volume" in receipt.reason
    _report["montage_voiceover_receipt"] = _receipt(row, MUTE).model_dump(mode="json")
