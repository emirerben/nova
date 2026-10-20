"""KRI-756 guard tests for the suggestion-chip engine's contracts (exempt from the
no-tests-after-code rule: each pins an invariant the ticket names, none restates a
computation). The behavioural matrix lives in tests/evals/test_suggestion_chips_evals.py.
"""

from __future__ import annotations

import inspect
import re
from types import SimpleNamespace

import pytest
from pydantic import ValidationError

from app.config import settings
from app.services import suggestion_chips as sc
from app.services.suggestion_serve import Served

# ── CHIP_KIND_GATE: one row per ChipKind ─────────────────────────────────────────────


def test_every_chip_kind_has_a_gate_row():
    assert set(sc.CHIP_KIND_GATE) == set(sc.ChipKind), (
        "a ChipKind without a CHIP_KIND_GATE row ships unchecked: add the row"
    )


@pytest.mark.parametrize("kind", list(sc.ChipKind), ids=str)
def test_every_gate_row_is_complete(kind):
    spec = sc.CHIP_KIND_GATE[kind]
    assert spec.kind is kind
    if kind is sc.ChipKind.PINNED_TEXT:
        # Dark this pass (PINNED_TEXT_PROVEN): the row exists so clients and the guard know
        # the kind, but it can never fire in any state.
        assert not spec.states and sc.PINNED_TEXT_PROVEN is False
        return
    assert spec.states and spec.states <= {"first_prompt", "ready"}
    assert set(spec.capability) == set(spec.states), "a capability per state it can fire in"
    assert set(spec.dry_run) == set(spec.states), "a dry-run mode per state it can fire in"
    assert set(spec.dry_run.values()) <= {"none", "strategy", "editor", "allowlist"}
    assert spec.expects.strip(), "say what a successful tap must change"
    if "editor" in spec.dry_run.values():
        assert spec.op is not None, "an editor witness needs the op to compile"
    if kind is not sc.ChipKind.DEFAULT_EDIT:
        # only the open request may skip the dry run
        assert "none" not in spec.dry_run.values()


def test_only_default_edit_is_exempt_from_the_witness():
    exempt = {k for k, s in sc.CHIP_KIND_GATE.items() if "none" in s.dry_run.values()}
    assert exempt == {sc.ChipKind.DEFAULT_EDIT}


# ── The wire contract stays additive ─────────────────────────────────────────────────


def test_the_turn_body_takes_one_optional_additive_suggestion_field():
    """The turn POST gains exactly `suggestion: {set_id, chip_id}`; both are required inside,
    nothing else is accepted, and omitting it is the old contract. The v1 message body stays
    untouched (text-match only)."""
    from app.kria.api_schemas import SubmitTurnBody
    from app.routes.creation_threads import MessageBody

    v2 = {"message": "Add captions", "client_event_id": "e1", "expected_thread_revision": 0}
    v1 = {"message": "Add captions", "client_event_id": "e1", "expected_revision": 0}
    assert SubmitTurnBody.model_validate(v2).suggestion is None
    ok = SubmitTurnBody.model_validate({**v2, "suggestion": {"set_id": "sg_1", "chip_id": "sc_1"}})
    assert ok.suggestion.set_id == "sg_1" and ok.suggestion.chip_id == "sc_1"
    for bad in ({"chip_id": "sc_1"}, {"set_id": "sg_1"}, {"set_id": "s", "chip_id": "c", "x": 1}):
        with pytest.raises(ValidationError):
            SubmitTurnBody.model_validate({**v2, "suggestion": bad})
    with pytest.raises(ValidationError):
        MessageBody.model_validate({**v1, "suggestion": {"set_id": "s", "chip_id": "c"}})


def test_payload_suggestion_is_stamped_after_request_digest_and_never_in_it():
    """request_digest(body) hashes the BODY minus `suggestion`; the stamp goes on the stored
    payload only, so a retry of the same client_event_id keeps its digest whether or not a
    chip matched, and a client that adds or drops the field never trips idempotency."""
    from app.kria import runtime
    from app.kria.api_schemas import SubmitTurnBody

    source = inspect.getsource(runtime.submit_turn)
    digest_at = source.find("request_digest(")
    stamp_at = source.find("suggestion_tap_payload(")
    assert 0 <= digest_at < stamp_at, "the stamp must be built after the digest is taken"
    base = {"message": "Add captions", "client_event_id": "e1", "expected_thread_revision": 0}
    plain = SubmitTurnBody.model_validate(base)
    tapped = SubmitTurnBody.model_validate(
        {**base, "suggestion": {"set_id": "sg_1", "chip_id": "sc_1"}}
    )
    assert runtime.request_digest(plain) == runtime.request_digest(tapped)


def test_the_stamp_has_exactly_the_documented_keys():
    thread = SimpleNamespace(state={sc.RING_KEY: [_entry("sg_1", "first_prompt", "d1")]})
    hit = sc.match_offered_suggestion(thread, "Add captions")
    assert set(hit) == {
        "set_id",
        "chip_id",
        "kind",
        "rank",
        "state",
        "label_source",
        "prompt_version",
        "default_edit",
    }
    assert hit == {
        "set_id": "sg_1",
        "chip_id": "sc_captions",
        "kind": "captions",
        "rank": 2,
        "state": "first_prompt",
        "label_source": "agent",
        "prompt_version": sc.PROMPT_VERSION,
        "default_edit": False,
    }


def test_an_explicit_suggestion_id_is_validated_against_the_ring_then_falls_back():
    thread = _thread(_entry("sg_1", "first_prompt", "d1"))
    tap = {"set_id": "sg_1", "chip_id": "sc_captions"}
    assert sc.match_offered_suggestion(thread, "Add captions", suggestion=tap)["chip_id"] == (
        "sc_captions"
    )
    # unknown set / chip id: ignored, the text match still applies
    for bad in ({"set_id": "sg_x", "chip_id": "sc_captions"}, {"set_id": "sg_1", "chip_id": "x"}):
        assert sc.match_offered_suggestion(thread, "Add captions", suggestion=bad)["set_id"] == (
            "sg_1"
        )
    # the message must still BE that chip: an id can never stamp a chip the creator did not send
    assert sc.match_offered_suggestion(thread, "make it moodier", suggestion=tap) is None
    # an id for another chip does not hijack the message that equals this one
    other = {"set_id": "sg_1", "chip_id": "sc_hook"}
    assert sc.match_offered_suggestion(thread, "Add captions", suggestion=other)["chip_id"] == (
        "sc_captions"
    )


# ── match_offered_suggestion ─────────────────────────────────────────────────────────


def _entry(set_id, state, media_digest, kinds=("default_edit", "captions", "hook")):
    messages = {
        "default_edit": "Suggest an edit.",
        "captions": "Add captions",
        "hook": "Add a hook that matches the video",
        "sfx": "Add sound effects",
    }
    return {
        "set_id": set_id,
        "state": state,
        "digest": media_digest,
        "media_digest": media_digest,
        "created_at": "2026-10-19T00:00:00+00:00",
        "final": True,
        "wording": "agent",
        "prompt_version": sc.PROMPT_VERSION,
        "chips": [
            {
                "id": f"sc_{k}",
                "kind": k,
                "rank": i + 1,
                "message": messages[k],
                "label_source": "agent",
                "default_edit": k == "default_edit",
            }
            for i, k in enumerate(kinds)
        ],
    }


def _thread(*entries):
    return SimpleNamespace(
        id="t-1", creator_id="u-1", active_plan_item_id="item-1", state={sc.RING_KEY: list(entries)}
    )


@pytest.mark.parametrize(
    "typed",
    [
        "Add captions",
        "add captions",
        "ADD CAPTIONS",
        "  Add   captions  ",
        "Add\tcaptions\n",
        "Ａdd captions",  # NFKC: full-width A
    ],
)
def test_match_ignores_case_and_whitespace(typed):
    assert sc.match_offered_suggestion(_thread(_entry("sg_1", "first_prompt", "d1")), typed)


@pytest.mark.parametrize(
    "typed", ["", "   ", "Add caption", "Add captions please", "Add captions.", "captions"]
)
def test_match_is_exact_not_fuzzy(typed):
    assert sc.match_offered_suggestion(_thread(_entry("sg_1", "first_prompt", "d1")), typed) is None


def test_match_ignores_sets_for_stale_media():
    thread = _thread(_entry("sg_old", "first_prompt", "digest-OLD"))
    assert sc.match_offered_suggestion(thread, "Add captions", media_digest="digest-NEW") is None
    assert sc.match_offered_suggestion(thread, "Add captions", media_digest="digest-OLD")


def test_match_prefers_the_newest_set():
    thread = _thread(
        _entry("sg_old", "first_prompt", "d1", kinds=("default_edit", "hook", "captions")),
        _entry("sg_new", "first_prompt", "d1", kinds=("default_edit", "captions", "hook")),
    )
    hit = sc.match_offered_suggestion(thread, "Add captions", media_digest="d1")
    assert hit["set_id"] == "sg_new" and hit["rank"] == 2


def test_match_falls_back_to_an_older_set_only_when_the_newest_lacks_it():
    thread = _thread(
        _entry("sg_old", "first_prompt", "d1", kinds=("default_edit", "sfx")),
        _entry("sg_new", "first_prompt", "d1", kinds=("default_edit", "captions")),
    )
    assert sc.match_offered_suggestion(thread, "Add sound effects")["set_id"] == "sg_old"


@pytest.mark.parametrize(
    "state", [None, {}, {sc.RING_KEY: None}, {sc.RING_KEY: "x"}, {sc.RING_KEY: [3, None]}]
)
def test_match_survives_missing_or_malformed_rings(state):
    assert sc.match_offered_suggestion(SimpleNamespace(state=state), "Add captions") is None


class _ExplodingDb:
    """Any use means the flag-off path touched the database."""

    async def get(self, *a, **k):
        raise AssertionError("flag off must not touch the database")

    async def execute(self, *a, **k):
        raise AssertionError("flag off must not touch the database")


async def test_tap_stamping_is_off_when_the_flag_is_off(monkeypatch):
    """Flag off => {} before any database access => payload and request_digest byte-identical."""
    monkeypatch.setattr(settings, "kria_suggestion_chips_enabled", False)
    monkeypatch.setattr(settings, "kria_suggestion_chips_user_ids", [])
    thread = _thread(_entry("sg_1", "first_prompt", "d1"))
    assert await sc.suggestion_tap_payload(_ExplodingDb(), thread, "Add captions") == {}


async def test_the_pure_matcher_has_no_flag_so_the_wrapper_owns_the_gate(monkeypatch):
    """Spec drift note: the ticket says match_offered_suggestion returns None with the flag
    off. It does not (it is pure); suggestion_tap_payload is the gate. Pin the real shape so
    nobody 'fixes' one half and silently loses the other."""
    monkeypatch.setattr(settings, "kria_suggestion_chips_enabled", False)
    thread = _thread(_entry("sg_1", "first_prompt", "d1"))
    assert sc.match_offered_suggestion(thread, "Add captions") is not None
    assert await sc.suggestion_tap_payload(_ExplodingDb(), thread, "Add captions") == {}


class _Db:
    def __init__(self, item):
        self.item = item

    async def get(self, model, key):
        return self.item


def _plan_item(media_id="m1"):
    return SimpleNamespace(
        clip_gcs_paths=[f"users/u/{media_id}.mp4"],
        clip_assignments=[{"media_id": media_id, "kind": "video", "analysis": None}],
    )


def _digest_of(item):
    from app.kria.brief_binding import snapshot_media
    from app.services.creative_copy_decisions import media_digest

    return media_digest(snapshot_media(item))


async def test_tap_stamp_is_added_only_for_the_current_media(monkeypatch):
    monkeypatch.setattr(settings, "kria_suggestion_chips_enabled", True)
    item = _plan_item("m1")
    thread = _thread(_entry("sg_1", "first_prompt", _digest_of(item)))
    stamp = await sc.suggestion_tap_payload(_Db(item), thread, "  add CAPTIONS ")
    assert stamp == {
        "suggestion": {
            "set_id": "sg_1",
            "chip_id": "sc_captions",
            "kind": "captions",
            "rank": 2,
            "state": "first_prompt",
            "label_source": "agent",
            "prompt_version": sc.PROMPT_VERSION,
            "default_edit": False,
        }
    }
    # an explicit id from the client is honoured on the same path
    explicit = {"set_id": "sg_1", "chip_id": "sc_captions"}
    got = await sc.suggestion_tap_payload(_Db(item), thread, "Add captions", suggestion=explicit)
    assert got["suggestion"]["chip_id"] == "sc_captions"
    # the creator swapped a clip after the set was offered: the old set no longer applies
    assert await sc.suggestion_tap_payload(_Db(_plan_item("m2")), thread, "Add captions") == {}
    # a message that is not a chip is never stamped
    assert await sc.suggestion_tap_payload(_Db(item), thread, "make it moodier") == {}


async def test_tap_stamping_never_fails_a_message(monkeypatch):
    monkeypatch.setattr(settings, "kria_suggestion_chips_enabled", True)

    class Broken:
        async def get(self, *a, **k):
            raise RuntimeError("db down")

    thread = _thread(_entry("sg_1", "first_prompt", "d1"))
    assert await sc.suggestion_tap_payload(Broken(), thread, "Add captions") == {}


# ── Route control flow (no database): flag, ownership, fail-open ─────────────────────


async def test_route_flag_off_is_unavailable_and_touches_nothing(monkeypatch):
    from starlette.responses import Response

    from app.routes import creation_threads as ct

    monkeypatch.setattr(settings, "kria_suggestion_chips_enabled", False)
    monkeypatch.setattr(settings, "kria_suggestion_chips_user_ids", [])

    async def no_load(*a, **k):
        raise AssertionError("flag off must not even load the thread")

    monkeypatch.setattr(ct, "_load", no_load)
    response = Response()
    out = await ct.thread_suggestions(
        SimpleNamespace(headers={}),
        "any",
        SimpleNamespace(id="u-1"),
        _ExplodingDb(),
        response,
        None,
    )
    assert (out.status, out.state, out.chips) == ("unavailable", "none", [])
    assert response.headers["cache-control"] == "no-store"


async def test_route_cohort_list_turns_it_on_for_one_account(monkeypatch):
    from starlette.responses import Response

    from app.routes import creation_threads as ct

    monkeypatch.setattr(settings, "kria_suggestion_chips_enabled", False)
    monkeypatch.setattr(settings, "kria_suggestion_chips_user_ids", ["u-canary"])
    loaded: list[str] = []

    async def fake_load(thread_id, user, db, **kw):
        loaded.append(str(user.id))
        return SimpleNamespace(id="thread-1")

    async def fake_serve(*a, **k):
        return Served(out=sc.unavailable_set(), media_digest="", input_digest="")

    class Db:
        async def rollback(self):
            return None

    monkeypatch.setattr(ct, "_load", fake_load)
    monkeypatch.setattr(ct, "serve_suggestion_set", fake_serve)
    for user_id, expect_loaded in (("u-canary", True), ("u-other", False)):
        loaded.clear()
        out = await ct.thread_suggestions(
            SimpleNamespace(headers={}), "t", SimpleNamespace(id=user_id), Db(), Response(), 2
        )
        assert bool(loaded) is expect_loaded
        if not expect_loaded:
            assert out.status == "unavailable"


async def test_route_is_fail_open_after_the_ownership_check(monkeypatch):
    from starlette.responses import Response

    from app.routes import creation_threads as ct

    monkeypatch.setattr(settings, "kria_suggestion_chips_enabled", True)
    rolled_back: list[bool] = []

    class Db:
        async def rollback(self):
            rolled_back.append(True)

    async def fake_load(*a, **k):
        return SimpleNamespace(id="thread-1")

    async def boom(*a, **k):
        raise RuntimeError("loader exploded")

    monkeypatch.setattr(ct, "_load", fake_load)
    monkeypatch.setattr(ct, "serve_suggestion_set", boom)
    out = await ct.thread_suggestions(
        SimpleNamespace(headers={}), "t", SimpleNamespace(id="u-1"), Db(), Response(), 2
    )
    assert (out.status, out.chips) == ("unavailable", [])
    assert rolled_back, "the failed read transaction must be released"


async def test_route_foreign_thread_is_the_usual_404_not_unavailable(monkeypatch):
    from fastapi import HTTPException
    from starlette.responses import Response

    from app.routes import creation_threads as ct

    monkeypatch.setattr(settings, "kria_suggestion_chips_enabled", True)

    async def not_yours(*a, **k):
        raise HTTPException(status_code=404, detail="not found")

    monkeypatch.setattr(ct, "_load", not_yours)
    with pytest.raises(HTTPException) as caught:
        await ct.thread_suggestions(
            SimpleNamespace(headers={}),
            "t",
            SimpleNamespace(id="u-1"),
            _ExplodingDb(),
            Response(),
            2,
        )
    assert caught.value.status_code == 404


def test_the_capability_bool_exists_and_defaults_off():
    from app.routes.creation_threads import CreationCapabilitiesOut

    field = CreationCapabilitiesOut.model_fields["suggestion_chips"]
    assert field.default is False


# ── Flag and cohort semantics ────────────────────────────────────────────────────────


def test_flag_defaults_off_and_cohort_only_ever_adds(monkeypatch):
    fresh = type(settings)(
        _env_file=None,
        storage_bucket="b",
        database_url="postgresql://x/y",
    )
    assert fresh.kria_suggestion_chips_enabled is False
    assert fresh.kria_suggestion_chips_user_ids == []
    assert fresh.suggestion_chips_for("u-1") is False
    monkeypatch.setattr(settings, "kria_suggestion_chips_enabled", False)
    monkeypatch.setattr(settings, "kria_suggestion_chips_user_ids", ["u-1"])
    assert settings.suggestion_chips_for("u-1") is True
    assert settings.suggestion_chips_for("u-2") is False
    assert settings.suggestion_chips_for(None) is False
    monkeypatch.setattr(settings, "kria_suggestion_chips_enabled", True)
    assert settings.suggestion_chips_for("u-2") is True


def test_flag_is_documented_in_claude_md_and_env_example():
    from pathlib import Path

    root = Path(__file__).resolve().parents[5]
    claude = (root / "CLAUDE.md").read_text(encoding="utf-8")
    assert re.search(r"KRIA_SUGGESTION_CHIPS_ENABLED", claude)
    assert "docs/pipelines/suggestion-chips.md" in claude
    assert "KRIA_SUGGESTION_CHIPS_ENABLED" in (root / ".env.example").read_text(encoding="utf-8")


# ── Editor-op decisions: nothing text-carrying can become a chip (KRI-756 denylist) ──

_TEXT_FIELDS = frozenset(
    {"text", "title", "caption", "find", "replace", "segments", "label_from", "minimal_text"}
    | {"words", "labels", "label"}
)


def _text_carrying_ops() -> set[str]:
    from app.agents import edit_copilot as ec

    ops: set[str] = set()
    for op in ec._VALID_OPS:
        fields = set(ec._OP_FIELDS.get(op, ())) | set(ec._OP_REQUIRED.get(op, ()))
        if fields & _TEXT_FIELDS:
            ops.add(op)
    return ops | set(ec._TEXT_OPS) | set(ec._CLIP_LABEL_OPS) | set(ec._TEXT_APPEARANCE_OPS)


def test_every_text_carrying_editor_op_has_an_explicit_chip_decision():
    """FAILS when a new op that carries creator-facing text enters the editor op vocabulary
    (edit_copilot._VALID_OPS, including the v2 ops merged into it) without being added to
    CHIP_OP_DENY (or, for a dry-run probe only, CHIP_OP_WITNESS_PROBES). A chip may never be
    the way words reach the screen: decide here, on purpose, when the vocabulary grows."""
    undecided = _text_carrying_ops() - sc.CHIP_OP_DENY - sc.CHIP_OP_WITNESS_PROBES
    assert not undecided, (
        f"editor ops {sorted(undecided)} carry text but have no chip decision: add each to "
        "CHIP_OP_DENY in app/services/suggestion_chips.py (a chip never carries words)"
    )


def test_the_chip_op_decisions_are_consistent():
    from app.agents import edit_copilot as ec

    assert not sc.CHIP_OP_ALLOW & sc.CHIP_OP_DENY, "an op is allowed or denied, never both"
    assert not sc.CHIP_OP_ALLOW & sc.CHIP_OP_WITNESS_PROBES
    assert not (sc.CHIP_OP_ALLOW | sc.CHIP_OP_DENY | sc.CHIP_OP_WITNESS_PROBES) - set(
        ec._VALID_OPS
    ), "a decision names an op that no longer exists: prune it"
    assert not _text_carrying_ops() & sc.CHIP_OP_ALLOW, "an allowed op must carry no text"
    # The explicit denials the ticket names.
    for op in (
        "add_text",
        "rewrite_text",
        "set_title",
        "label_each_clip",
        "edit_caption",
        "replace_caption_text",
        "swap_music",
        "add_overlay",
        "set_intro_layout",
        "apply_speech_cut_candidate",
        "add_camera_effect",
    ):
        assert op in sc.CHIP_OP_DENY, op
    for spec in sc.CHIP_KIND_GATE.values():
        if spec.claimed_op is not None:
            assert spec.claimed_op in sc.CHIP_OP_ALLOW, (spec.kind, spec.claimed_op)
        if spec.witness_op is not None:
            assert spec.witness_op in sc.CHIP_OP_ALLOW | sc.CHIP_OP_WITNESS_PROBES, spec.kind
    # The hook routes into the creative-copy flow: it claims no op and only PROBES set_title.
    assert sc.CHIP_KIND_GATE[sc.ChipKind.HOOK].claimed_op is None


# ── Copy: the template deck is its own validator's fixed point ───────────────────────


@pytest.mark.parametrize("language", ["en", "tr"])
@pytest.mark.parametrize("kind", [k for k in sc.ChipKind if k in sc._COPY], ids=str)
def test_every_template_label_passes_the_label_validator(kind, language):
    from app.services.suggestion_chip_copy import validate_label

    label, message = sc._copy(kind, language)
    slots = {"filename": "IMG_7382"} if kind is sc.ChipKind.OPENER_CLIP else {}
    rendered, reason = validate_label(label, kind=str(kind), language=language, slots=slots)
    assert reason is None and rendered, (kind, language, label, reason)
    assert len(message.replace("{filename}", "IMG_7382")) <= sc.MAX_MESSAGE_CHARS


def test_chip_one_is_the_media_only_message_the_app_already_sends():
    from pathlib import Path

    swift = (
        Path(__file__).resolve().parents[5] / "src/apps/ios/Kria/Features/ChatTimeline.swift"
    ).read_text(encoding="utf-8")
    assert f'static let mediaOnlyMessage = "{sc.MEDIA_ONLY_MESSAGE}"' in swift
    for language in ("en", "tr"):
        assert sc._copy(sc.ChipKind.DEFAULT_EDIT, language)[1] == sc.MEDIA_ONLY_MESSAGE


# ── The wording agent can only word what the generator allowed ───────────────────────


def _fp_ctx(**overrides):
    clips = [
        {
            "media_id": "m-a",
            "ordinal": 1,
            "kind": "video",
            "subject": "barista pouring coffee",
            "setting": "sunny kitchen counter",
            "has_speech": True,
            "to_camera": True,
            "notable_moments": [{"start_s": 1.0, "end_s": 3.0, "label": "latte art pour"}],
        },
        {
            "media_id": "m-b",
            "ordinal": 2,
            "kind": "video",
            "subject": "city street at dusk",
            "setting": "downtown sidewalk",
            "has_speech": False,
            "to_camera": False,
            "notable_moments": [],
        },
    ]
    base = dict(
        thread_id="t-1",
        state="first_prompt",
        language="en",
        understanding="complete",
        media_digest="media-1",
        manifest_hash="hash-1",
        edit_format="montage",
        runtime_version=2,
        manifest={
            "capabilities": {
                name: {"available": True}
                for name in ("dispatch_render", "sound_effects", "transitions")
            }
        },
        clips=clips,
        strategy_witness=lambda kind: frozenset({f"strategy:{kind}"}),
    )
    return sc.SuggestionContext(**{**base, **overrides})


def test_wording_cannot_add_rename_or_retarget_a_chip():
    ctx = _fp_ctx()
    plan = sc.build_candidates(ctx)
    lever = plan.levers[0]
    wording, stats = sc.resolve_wording(
        ctx,
        plan,
        default_label="Let Kria cut something viral for the barista",
        ranked=[
            ("sc_invented", "Add fireworks", None),
            (lever.cid, 'Add "captions" 3 times', "Your barista talks to the camera"),
        ],
    )
    out = sc.assemble_set(ctx, plan, wording)
    live = {c.cid: c for c in (plan.default, *plan.levers)}
    assert {c.id for c in out.chips} <= set(live)
    for chip in out.chips:
        assert chip.message == live[chip.id].message, "message is always the server template"
        assert chip.kind == str(live[chip.id].kind)
    assert stats.drop_reasons["unknown_cid"] == 1
    # both the default and the lever label failed validation: template labels, honest source
    assert out.chips[0].message == sc.MEDIA_ONLY_MESSAGE and out.chips[0].default_edit
    assert out.chips[0].label == live[out.chips[0].id].fallback_label
    assert {c.label_source for c in out.chips} == {"template"}
    assert out.chips[1].why == "Your barista talks to the camera"


def test_a_grounded_label_and_why_reach_the_screen_and_an_ungrounded_why_does_not():
    ctx = _fp_ctx()
    plan = sc.build_candidates(ctx)
    cands = {c.kind: c for c in plan.levers}
    hook = cands[sc.ChipKind.HOOK]
    sfx = cands.get(sc.ChipKind.SFX)
    ranked = [(hook.cid, "Open with a stronger hook", "Open on the latte art pour")]
    if sfx is not None:
        ranked.append((sfx.cid, "Add a pile of sound effects", "Great vibes all around"))
    wording, _ = sc.resolve_wording(ctx, plan, default_label=None, ranked=ranked)
    out = sc.assemble_set(ctx, plan, wording)
    by_kind = {c.kind: c for c in out.chips}
    assert by_kind["hook"].label == "Open with a stronger hook"
    assert by_kind["hook"].label_source == "agent"
    assert by_kind["hook"].why == "Open on the latte art pour"
    assert out.wording == "agent"
    if sfx is not None:
        assert by_kind["sfx"].why is None, "a why with no footage word is dropped"
        assert by_kind["sfx"].label_source == "template", "'pile' is off the sfx lexicon"


def test_chip_limits_hold_for_any_wording():
    ctx = _fp_ctx()
    plan = sc.build_candidates(ctx)
    ranked = [(c.cid, c.fallback_label, None) for c in plan.levers]
    wording, _ = sc.resolve_wording(ctx, plan, default_label=None, ranked=ranked)
    out = sc.assemble_set(ctx, plan, wording)
    assert 1 <= len(out.chips) <= 4
    assert [c.rank for c in out.chips] == list(range(1, len(out.chips) + 1))
    assert len({c.kind for c in out.chips}) == len(out.chips), "one chip per kind"
    assert len({c.message for c in out.chips}) == len(out.chips), "no duplicate message"
    assert [c.default_edit for c in out.chips] == [True] + [False] * (len(out.chips) - 1)


# ── The GET never waits long, never calls a model and never changes a served label ───


class _FakeStore:
    def __init__(self):
        self.results: dict = {}
        self.served: dict = {}
        self.seen: dict = {}
        self.locks: set = set()
        self.hint: dict = {}
        self.calls: list = []

    def install(self, monkeypatch):
        from app.services import suggestion_store as store

        monkeypatch.setattr(store, "get_hint", lambda t: self.hint)
        monkeypatch.setattr(store, "put_hint", lambda t, **kw: self.hint.update(kw))
        monkeypatch.setattr(store, "get_served", lambda t, d: self.served.get((t, d)))
        monkeypatch.setattr(store, "put_served", lambda t, d, v: self.served.setdefault((t, d), v))
        monkeypatch.setattr(store, "get_result", lambda t, d: self.results.get((t, d)))

        def try_lock(t, d, *a, **k):
            first = (t, d) not in self.locks
            self.locks.add((t, d))
            return first

        monkeypatch.setattr(store, "try_lock", try_lock)
        monkeypatch.setattr(
            store, "first_seen", lambda t, d, now=None: self.seen.setdefault((t, d), 1000.0)
        )


@pytest.fixture
def serve_env(monkeypatch):
    from app.services import suggestion_serve as serve

    fake = _FakeStore()
    fake.install(monkeypatch)
    ctx = _fp_ctx()
    enqueued: list = []
    clock = {"now": 1000.0}

    env = SimpleNamespace(serve=serve, fake=fake, ctx=ctx, enqueued=enqueued, clock=clock)

    async def fake_ctx(*a, **k):
        return env.ctx

    monkeypatch.setattr(serve, "load_suggestion_context", fake_ctx)
    monkeypatch.setattr(serve.time, "time", lambda: clock["now"])
    monkeypatch.setattr(serve, "enqueue_suggester", lambda *a, **k: enqueued.append(k) or True)
    monkeypatch.setattr(settings, "kria_suggester_max_runs_per_thread", 12)
    return env


async def _get(env):
    return await env.serve.serve_suggestion_set(
        None, SimpleNamespace(id="t-1", creator_id="u-1"), None, 13
    )


async def test_cache_miss_waits_at_most_six_seconds_then_freezes_template_labels(serve_env):
    env = serve_env
    first = (await _get(env)).out
    assert (first.status, first.wording, first.retry_after_s) == ("pending", "pending", 2)
    assert [c.kind for c in first.chips] == ["default_edit"]
    assert len(env.enqueued) == 1 and env.enqueued[0]["trigger"] == "lazy"
    env.clock["now"] = 1003.0
    assert (await _get(env)).out.status == "pending"
    assert len(env.enqueued) == 1, "the lazy enqueue is single-flight per digest"
    env.clock["now"] = 1007.0
    final = (await _get(env)).out
    assert (final.status, final.wording, final.retry_after_s) == ("ok", "fallback", None)
    assert final.set_id == first.set_id, "the same digest keeps its set_id"
    assert {c.label_source for c in final.chips} == {"template"} and len(final.chips) > 1
    # An agent result that lands AFTER the freeze never rewrites a served label.
    plan = sc.build_candidates(env.ctx)
    wording, _ = sc.resolve_wording(
        env.ctx, plan, default_label=None, ranked=[(plan.levers[0].cid, "Open with a hook", None)]
    )
    env.fake.results[(env.ctx.thread_id, plan.input_digest)] = {
        "status": "ok",
        "wording": sc.wording_to_dict(wording),
    }
    assert (await _get(env)).out == final


async def test_a_cached_agent_result_is_served_at_once_and_frozen(serve_env):
    env = serve_env
    plan = sc.build_candidates(env.ctx)
    hook = next(c for c in plan.levers if c.kind is sc.ChipKind.HOOK)
    wording, _ = sc.resolve_wording(
        env.ctx,
        plan,
        default_label=None,
        ranked=[(hook.cid, "Open with a stronger hook", "Open on the latte art pour")],
    )
    env.fake.results[(env.ctx.thread_id, plan.input_digest)] = {
        "status": "ok",
        "wording": sc.wording_to_dict(wording),
    }
    out = (await _get(env)).out
    assert (out.status, out.wording) == ("ok", "agent")
    assert [c.kind for c in out.chips] == ["default_edit", "hook"]
    assert env.enqueued == []
    # the cache changes underneath: the creator keeps seeing the served set
    env.fake.results.clear()
    assert (await _get(env)).out == out


async def test_a_fallback_marker_serves_template_labels_without_waiting(serve_env):
    env = serve_env
    plan = sc.build_candidates(env.ctx)
    env.fake.results[(env.ctx.thread_id, plan.input_digest)] = {
        "status": "fallback",
        "reason": "budget",
    }
    out = (await _get(env)).out
    assert (out.status, out.wording) == ("ok", "fallback")


async def test_the_run_cap_lever_serves_template_labels_immediately(serve_env, monkeypatch):
    env = serve_env
    monkeypatch.setattr(settings, "kria_suggester_max_runs_per_thread", 0)
    out = (await _get(env)).out
    assert (out.status, out.wording) == ("ok", "fallback")
    assert env.enqueued == [] and not env.fake.locks


async def test_understanding_still_running_serves_only_the_default_edit_and_asks_again(serve_env):
    env = serve_env
    env.ctx = _fp_ctx(understanding="pending")
    out = (await _get(env)).out
    assert (out.status, out.retry_after_s, out.understanding) == ("pending", 3, "pending")
    assert [c.kind for c in out.chips] == ["default_edit"]
    assert env.enqueued == []


# ── Wiring that is only ever checked at deploy time ──────────────────────────────────


def test_the_suggester_is_registered_routed_and_costed_by_one_name():
    from app.agents import _runtime
    from app.agents._registry import AGENTS
    from app.services import ai_cost_control
    from app.worker import celery_app

    cls = AGENTS()["nova.chat.suggester"]
    assert cls.spec.prompt_id == "chip_suggester"
    assert cls.spec.prompt_version == sc.PROMPT_VERSION
    assert cls.spec.model == "gemini-2.5-flash" and cls.spec.sensitive_io is False
    assert "app.tasks.kria_suggester" in celery_app.conf.include
    assert celery_app.conf.task_routes["tasks.kria_suggester"] == {"queue": "agent-control"}
    # The per-user daily cap keys on a literal feature name: the runtime must emit it.
    assert ai_cost_control._SUGGESTER_FEATURE == "chip_suggester"
    assert 'else "chip_suggester"' in inspect.getsource(_runtime.Agent)


def test_the_prompt_renders_every_placeholder():
    from app.agents.chip_suggester import ChipSuggesterAgent

    ctx = _fp_ctx()
    plan = sc.build_candidates(ctx)
    payload = sc.build_agent_input(ctx, plan)
    prompt = ChipSuggesterAgent(None).render_prompt(
        ChipSuggesterAgent.Input.model_validate(payload)
    )
    assert not re.search(r"\$(state|language|max_chips|vocabulary|payload)\b", prompt)
    assert plan.levers[0].cid in prompt and "c1.m1" in prompt
    # opaque handles only: no media id, no timestamp, no transcript reaches the model
    assert "m-a" not in prompt and "start_s" not in prompt


def test_agent_parse_drops_unknown_duplicate_and_over_limit_items():
    from app.agents.chip_suggester import ChipSuggesterAgent, ChipSuggesterInput

    ctx = _fp_ctx()
    plan = sc.build_candidates(ctx)
    agent_input = ChipSuggesterInput.model_validate(sc.build_agent_input(ctx, plan))
    cids = [c.cid for c in plan.pool]
    raw = (
        '{"default_label": null, "ranked": ['
        + ",".join(
            f'{{"cid": "{cid}", "label_template": "x", "why": null}}'
            for cid in ["sc_nope", cids[0], cids[0], *cids[1:], *cids[1:]]
        )
        + "]}"
    )
    out = ChipSuggesterAgent(None).parse(raw, agent_input)
    assert [r.cid for r in out.ranked] == cids[: agent_input.max_chips]
    assert out.drop_reasons["unknown_cid"] == 1
    assert out.drop_reasons["duplicate_kind"] >= 1
