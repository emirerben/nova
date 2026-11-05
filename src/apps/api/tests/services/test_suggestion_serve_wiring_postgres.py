"""The serve side of the AI suggestion blocks, on a real thread: loader -> serve -> a v2 set.

Failure catalogue, written before the wiring. Each row is a fixture below that drives the REAL
path (``load_suggestion_context`` -> ``with_transcripts`` -> ``plan_for`` -> the set the GET
joins, and for the signals hook the real backfill write path) on real Postgres. The rule they
defend: a kind's card exists only when its own signal or evidence says so, and nothing missing
ever turns into a card.

Inputs
  W1  every signal present, a ready montage: the set carries the duplicates, mute, filler and
      hook cards, and only those the evidence supports.
  W2  one loader failing (duplicates input, brief read, hook inputs, filler evidence) drops only
      that kind. The others still serve; the kind whose input is missing offers nothing.
  W3  a signal MISSING versus ``measured: false``: neither makes a card (mute, duplicates).
  W4  understanding pending or empty: a clip whose record is not complete never reads as a
      speechless noisy clip (mute), and a pending first prompt serves no v2 card.
  W5  the creator's request wins (KRI-129): a live clip-scoped or ordering requirement keeps the
      clips it names out of every duplicates card; a title requirement or an earlier copy
      decision hides the hook card.
  W6  filler evidence on a row the scheduler is about to redo (a Gemini transcript contradicts
      whisper's language, no crosscheck yet, or an old engine version) offers no card.
  W7  Turkish thread: card titles, prompts and measured evidence are Turkish.
  W8  the hook card's chat prompt is a closed sentence that never carries the copy.

Freshness and digest
  W9  the freshness key moves with the head draft, the brief version and the signals version, and
      the GET and the worker (both: loader + with_transcripts + plan_for) agree on it.
 W10  every flag off: the loader context, the plan and the wire set are byte-identical to the
      pre-change golden (tests/fixtures/suggestion_serve_wiring/flags_off_golden.json), and no
      brief, freshness or kind-input read happens.
 W11  a Redis outage serves the deterministic set (cards included) and never raises.

Signals hook
 W12  the backfill write fires ``on_signals_computed`` once per item, only after a clip's signals
      were stored, never in a dry run, and after the plan item's row lock is released.
 W13  clip understanding measures signals only with ``KRIA_SUGGESTION_SIGNALS_ENABLED`` and the
      creator on the chips allow-list; a failing measurement never fails the task; an almost
      spent run defers instead of starting.

Skips unless DATABASE_URL names a reachable migrated ``*_test`` database; where
``NOVA_REQUIRE_POSTGRES_TESTS`` is set (the api shards) that is a collection failure, not a skip.
"""

from __future__ import annotations

import json
import os
import uuid
from pathlib import Path
from types import SimpleNamespace

import pytest
import pytest_asyncio
from sqlalchemy import select, text
from sqlalchemy.engine import make_url

from app.config import settings
from app.database import AsyncSessionLocal, sync_session
from app.database import engine as async_engine
from app.models import CreationThread, CreationThreadEvent, PlanItem
from app.services import clip_signals
from app.services import suggestion_chips as sc
from app.services import suggestion_serve as serve
from app.services import suggestion_store as store
from app.services.suggestions import brief_asks
from app.services.suggestions.kinds import filler_words, mute_clip
from app.services.suggestions.models import RankedPick, SuggestionAgentOutput
from tests.postgres_required import postgres_required


def _database_problem() -> str | None:
    name = make_url(settings.database_url).database or ""
    if not name.endswith("_test"):
        return f"refusing to use non-test database {name!r}"
    try:
        with sync_session() as probe:
            probe.execute(text("select 1 from creative_brief_versions limit 1"))
    except Exception as exc:  # noqa: BLE001
        return f"nova_test Postgres not reachable or not migrated ({type(exc).__name__})"
    return None


_PROBLEM = _database_problem()
if _PROBLEM and postgres_required():
    pytest.fail(f"{_PROBLEM}: the serve wiring fixtures must run in CI", pytrace=False)
if _PROBLEM:
    pytest.skip(_PROBLEM, allow_module_level=True)

from tests.tasks import kria_pinned_suggestion_harness as h  # noqa: E402

FIXTURES = Path(__file__).resolve().parents[1] / "fixtures" / "suggestion_serve_wiring"
GOLDEN = FIXTURES / "flags_off_golden.json"

ALL_KIND_FLAGS = (
    "kria_suggestion_signals_enabled",
    "kria_suggestion_duplicates_enabled",
    "kria_suggestion_mute_clip_enabled",
    "kria_suggestion_filler_words_enabled",
    "kria_suggestion_hook_copy_enabled",
)

# Two takes of one shot and a different shot: the real hashes of the duplicates replay fixture.
_TAKE_A = ["dec1ede0758d8515", "9e3408b2456f8785", "9b9def52dd9b3d51"]
_TAKE_B = ["dec1ede0758d8516", "9e3408b2456f8786", "9b9def52dd9b3d52"]
_OTHER = ["e9daf9c495f635b9", "2f15a0a0d2b70b80", "50f24a15eb3bb540"]
_THIRD = ["64300800c7681e43", "2efd92ee3cdd95b5", "88adf9b95ce4c4f7"]
NOISY = {
    "wind_score": 0.91,
    "crowd_score": 0.1,
    "clipping": 0.0,
    "clipped": False,
    "noise_floor_db": -28.0,
    "peak_db": -3.0,
    "noisy": True,
    "silent": False,
    "evidence": ["steady wind"],
}
QUIET = {**NOISY, "wind_score": 0.05, "noisy": False, "evidence": []}
UNMEASURED = {"measured": False, "reason": "no_audio_stream"}


def _dup(hashes, sharpness):  # noqa: ANN001, ANN202
    return {"hashes": hashes, "sharpness": sharpness, "mean_luma": 120.0}


@pytest_asyncio.fixture(autouse=True)
async def _dispose_async_engine():
    yield
    await async_engine.dispose()


@pytest.fixture
def world(monkeypatch):
    w = h.install_world(monkeypatch)
    # Understanding is not what is under test here: the clips below carry finished records.
    monkeypatch.setattr(settings, "kria_clip_understanding_enabled", False)
    monkeypatch.setattr(settings, "silence_cut_enabled", True)
    monkeypatch.setattr(mute_clip, "PINNED_TAP_APPLIES_CLIP_SOUND", True)
    return w


@pytest.fixture
def v2_on(monkeypatch):
    for name in (*ALL_KIND_FLAGS, "kria_suggestion_editor_triggers_enabled"):
        monkeypatch.setattr(settings, name, True)


# ── Seeding: a ready montage whose clips carry understanding and signals ─────────────


def _clip(i, *, speech=False, understood=True, **signals):  # noqa: ANN001, ANN003, ANN202
    analysis: dict = {"duration_s": 8.0}
    if understood:
        analysis["understanding"] = {
            "subject": f"shot number {i + 1}",
            "speech": {"has_speech": speech, "to_camera": False},
        }
    if signals:
        analysis = clip_signals.merge_signals(analysis, signals)
    return {
        "media_id": h.MEDIA_IDS[i],
        "kind": "video",
        "source_filename": f"{h.MEDIA_IDS[i]}.mov",
        "gcs_path": h.CLIP_PATHS[i],
        "duration_s": 8.0,
        "analysis": analysis,
    }


def footage(**per_clip):  # noqa: ANN003, ANN202
    """clip-a and clip-b are takes of one shot (b sharper), clip-c is a noisy different shot."""
    base = [
        dict(near_dup_hash=_dup(_TAKE_A, 60)),
        dict(near_dup_hash=_dup(_TAKE_B, 140)),
        dict(near_dup_hash=_dup(_OTHER, 100), audio_quality=NOISY),
    ]
    out = []
    for i, signals in enumerate(base):
        merged = {**signals, **per_clip.get(h.MEDIA_IDS[i], {})}
        speech = merged.pop("speech", False)
        understood = merged.pop("understood", True)
        out.append(_clip(i, speech=speech, understood=understood, **merged))
    return out


def untitled_variant():
    variant = h.ready_variant()
    variant["text_elements"] = []  # no title in the edit: the hook card has a place to land
    return variant


def seed(clips=None, *, variant=None):  # noqa: ANN001, ANN202
    seeded = h.seed_ready_thread(variant or untitled_variant())
    with sync_session() as db:
        item = db.get(PlanItem, seeded.item_id, with_for_update=True)
        item.clip_assignments = clips if clips is not None else footage()
        db.commit()
    return seeded


def add_brief(seeded, requirements):  # noqa: ANN001, ANN202
    from app.models import CreativeBriefVersion

    with sync_session() as db:
        db.add(
            CreativeBriefVersion(
                thread_id=seeded.thread_id,
                version=1,
                requirements=requirements,
            )
        )
        db.commit()


async def loaded(seeded, *, language=None):  # noqa: ANN001, ANN202
    """What the GET and the worker both do: the loader, then ``with_transcripts``."""
    async with AsyncSessionLocal() as db:
        thread = await db.get(CreationThread, seeded.thread_id)
        ctx = await sc.load_suggestion_context(db, thread, None, 13, language_hint=language)
        ctx = await serve.with_transcripts(db, thread, ctx)
        plan = await serve.plan_for(db, thread, ctx)
    assert ctx.state == "ready", ctx.state
    return ctx, plan


def kinds_of(ctx, plan, picks=None):  # noqa: ANN001, ANN202
    built, drops = sc.assemble_suggestions(ctx, plan, picks)
    cards = {c.kind: c for c in (built.suggestions if built else [])}
    return cards, drops


def with_hook(ctx, plan, text="Matcha before sunrise"):  # noqa: ANN001, ANN202
    """Every card the set offers: the deterministic ones, plus the hook the agent wrote."""
    cards, _ = kinds_of(ctx, plan)
    cards.update({k: v for k, v in kinds_of(ctx, plan, hook_pick(text))[0].items() if k == "hook"})
    return cards


def hook_pick(text="Matcha before sunrise"):  # noqa: ANN001, ANN202
    return SuggestionAgentOutput(
        ranked=[RankedPick(kind="hook", target_ids=["all"], why="", copy=text, rank=1)]
    )


# ── W1 ──────────────────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_w1_every_signal_present_serves_each_kind_its_evidence_supports(
    world, v2_on, monkeypatch
):
    seeded = seed()
    install_filler(monkeypatch)
    ctx, plan = await loaded(seeded)
    cards, drops = kinds_of(ctx, plan)
    assert {"duplicates", "mute_clip", "filler_words"} <= set(cards), drops
    # With no agent reason the server's own measured line is the card's reason.
    assert cards["duplicates"].why.startswith("Keeps clip 2"), cards["duplicates"].why
    assert [cards["filler_words"].why, *cards["filler_words"].evidence] == [
        "4 filler sounds",
        "Saves 1.8 s",
    ]
    # The hook needs the agent's own words: it is dropped from the deterministic order.
    assert "hook" not in cards
    with_pick, _ = kinds_of(ctx, plan, hook_pick())
    assert with_pick["hook"].copy_text == "Matcha before sunrise"
    assert with_pick["hook"].prompt == "Add the hook shown on the card"
    # Clip 3 is the noisy one; clips 1 and 2 are the takes.
    assert [c.clip_ordinal for c in [cards["mute_clip"].anchor]] == [3]
    assert ctx.kind_inputs["duplicates"]["needed"] == []
    assert ctx.kind_inputs["hook"]["opening_copy"] == ""
    assert ctx.kind_inputs["brief"] is None


def install_filler(monkeypatch, *, ready=True, language="en", engine_version=None, **counts):  # noqa: ANN001, ANN003, ANN201
    """The two inputs the filler kind reads that depend on deployment state, not on the wiring:
    the capability (flags + renderer) and the narration source. The analysis row, the evidence
    and the redo check are the real ones, over the item's real clip analysis."""
    from app.models import SpeechCleanupAnalysis
    from app.services import plan_item_media, speech_cleanup_preflight
    from app.services.speech_cleanup_preflight import SPEECH_CLEANUP_ENGINE_VERSION

    source = SimpleNamespace(
        storage_path=h.CLIP_PATHS[0], generation="1", source_policy_fingerprint="fp-1"
    )
    row = SpeechCleanupAnalysis(
        id=uuid.uuid4(),
        source_policy_fingerprint="fp-1",
        engine_version=engine_version or SPEECH_CLEANUP_ENGINE_VERSION,
        status="ready" if ready else "queued",
        decision=None,
        category_counts={
            "filler_sounds": counts.get("filler", 4),
            "long_pauses": 1,
            "retakes": 0,
        },
        estimated_removed_ms=counts.get("removed_ms", 1760),
        analysis_payload={"language": language, "diagnostics": {}},
        source_storage_path=h.CLIP_PATHS[0],
    )

    async def current(_db, _item_id, **_k):  # noqa: ANN001, ANN202
        return row

    monkeypatch.setattr(speech_cleanup_preflight, "current_analysis_async", current)
    monkeypatch.setattr(
        plan_item_media,
        "resolve_item_narration",
        lambda item, detector_policy: SimpleNamespace(source=source),
    )
    monkeypatch.setattr(
        filler_words,
        "_capability",
        lambda item, settings: SimpleNamespace(available=True, reason=None),
    )
    return row


# ── W2 ──────────────────────────────────────────────────────────────────────────────


def _boom(*_a, **_k):  # noqa: ANN002, ANN003, ANN202
    raise RuntimeError("loader failure")


async def _boom_async(*_a, **_k):  # noqa: ANN002, ANN003, ANN202
    raise RuntimeError("loader failure")


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("patch", "gone"),
    [
        (("app.services.suggestion_serve.duplicates_input", _boom), "duplicates"),
        (("app.services.suggestion_serve.filler_evidence", _boom_async), "filler_words"),
        (
            ("app.services.speech_cleanup_preflight.current_analysis_async", _boom_async),
            "filler_words",
        ),
        (("app.services.suggestions.kinds.mute_clip.loader_input", _boom), "mute_clip"),
        (("app.services.suggestion_serve._hook_input", _boom_async), "hook"),
        (("app.kria.brief.load_latest_brief", _boom_async), "brief_readers"),
    ],
)
async def test_w2_one_loader_failing_drops_only_that_kind(world, v2_on, monkeypatch, patch, gone):
    seeded = seed()
    install_filler(monkeypatch)
    monkeypatch.setattr(patch[0], patch[1])
    ctx, plan = await loaded(seeded)
    cards = with_hook(ctx, plan)
    served = {"duplicates", "mute_clip", "filler_words", "hook"} & set(cards)
    expected = {"duplicates", "mute_clip", "filler_words", "hook"}
    # Without a brief the kinds that read it (all but filler) cannot prove the creator asked for
    # nothing that conflicts, so they offer nothing: failing closed, not dropping everything.
    removed = {"brief_readers": {"duplicates", "mute_clip", "hook"}}.get(gone, {gone})
    assert served == expected - removed
    assert ctx.kind_inputs.get("transcripts") is not None


@pytest.mark.asyncio
async def test_w2_a_failed_brief_read_does_not_forge_a_freshness_key(world, v2_on, monkeypatch):
    seeded = seed()
    monkeypatch.setattr("app.kria.brief.load_latest_brief", _boom_async)
    ctx, plan = await loaded(seeded)
    # Not a forged brief and not an absent key (absent reads as "the loader never ran"): the
    # sentinel makes the five brief-obeying kinds offer nothing (KRI-129).
    assert ctx.kind_inputs["brief"] is brief_asks.UNREADABLE
    assert "freshness" not in ctx.kind_inputs
    # plan_for read the key itself from the database rather than keying a brief that failed to load.
    assert plan.input_digest != sc.build_candidates(ctx).input_digest


# ── W3, W4 ──────────────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("clip_c", "offered"),
    [
        (dict(audio_quality=NOISY), True),
        (dict(audio_quality=QUIET), False),
        (dict(audio_quality=UNMEASURED), False),
        (dict(audio_quality={**NOISY, "silent": True}), False),
        ({}, False),  # no audio_quality entry at all: the backfill has not reached this clip
        (dict(audio_quality=NOISY, speech=True), False),  # wind over a voice is not wind alone
        (dict(audio_quality=NOISY, understood=False), False),  # an unanalysed clip reads as silent
    ],
    ids=["noisy", "quiet", "measured_false", "silent", "missing", "speech", "not_understood"],
)
async def test_w3_w4_a_mute_card_needs_a_measured_noisy_and_understood_clip(
    world, v2_on, clip_c, offered
):
    seeded = seed(footage(**{"clip-c": clip_c}) if clip_c else footage_without("audio_quality"))
    ctx, plan = await loaded(seeded)
    cards, _ = kinds_of(ctx, plan)
    assert ("mute_clip" in cards) is offered
    if offered:
        assert cards["mute_clip"].anchor.clip_ordinal == 3


def footage_without(name):  # noqa: ANN001, ANN202
    """``footage()`` with one signal left out of clip-c entirely."""
    clips = footage()
    analysis = clips[2]["analysis"]
    analysis["signals"] = {k: v for k, v in analysis["signals"].items() if k != name}
    return clips


@pytest.mark.asyncio
async def test_w3_mute_stays_dark_until_a_tap_is_proven_to_apply_it(world, v2_on, monkeypatch):
    monkeypatch.setattr(mute_clip, "PINNED_TAP_APPLIES_CLIP_SOUND", False)
    ctx, plan = await loaded(seed())
    assert "mute_clip" not in kinds_of(ctx, plan)[0]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("per_clip", "offered"),
    [
        ({}, True),
        ({"clip-a": dict(near_dup_hash={"measured": False})}, False),  # measured, no hashes
        ({"clip-a": dict(near_dup_hash=_dup(_THIRD, 60))}, False),  # not a pair any more
        ({"clip-a": dict(speech=True)}, True),  # a take that speaks is never the one removed
        ({"clip-a": dict(speech=True), "clip-b": dict(speech=True)}, False),
    ],
    ids=["pair", "measured_false", "different_shot", "one_speaks", "both_speak"],
)
async def test_w3_a_duplicates_card_needs_hashes_that_pair_and_no_voice(
    world, v2_on, per_clip, offered
):
    clips = footage(**per_clip)
    if "measured_false" in str(per_clip):
        clips[0]["analysis"]["signals"]["near_dup_hash"] = {"v": 2, "data": {"measured": False}}
    ctx, plan = await loaded(seed(clips))
    cards, _ = kinds_of(ctx, plan)
    assert ("duplicates" in cards) is offered
    if offered and "clip-a" not in per_clip:
        # The card removes the take that is not kept, and names which one stays.
        assert cards["duplicates"].anchor.clip_ordinal == 1
        assert cards["duplicates"].why == "Keeps clip 2: sharper"
    if offered and per_clip == {"clip-a": {"speech": True}}:
        assert cards["duplicates"].anchor.clip_ordinal == 2
        assert cards["duplicates"].why.startswith("Keeps clip 1")


# ── W5 ──────────────────────────────────────────────────────────────────────────────


def _req(rid, kind, scope, **extra):  # noqa: ANN001, ANN003, ANN202
    return {"id": rid, "kind": kind, "scope": scope, "status": "open", **extra}


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("requirements", "offered", "keeps"),
    [
        ([], True, "Keeps clip 2: sharper"),
        ([_req("r1", "text", "clip:clip-b", description="show the market")], True, "Keeps clip 2"),
        (
            [_req("r1", "select", "clip:clip-a", description="open on the sunrise take")],
            True,
            "Keeps clip 1",
        ),
        (
            [
                _req("r1", "select", "clip:clip-a", description="use this"),
                _req("r2", "select", "clip:clip-b", description="and this"),
            ],
            False,
            None,
        ),
        ([_req("r1", "order", "global", description="by when I filmed it")], False, None),
        ([_req("r1", "text", "per_clip", description="label every clip")], False, None),
        ([_req("r1", "select", "clip:zzzz", description="a clip nobody has")], False, None),
        ([_req("r1", "timing", "global", description="twenty seconds")], True, "Keeps clip 2"),
    ],
    ids=[
        "none",
        "names_the_keeper",
        "names_the_dropped_take",
        "names_both",
        "orders_clips",
        "labels_every_clip",
        "unresolvable_scope",
        "unrelated",
    ],
)
async def test_w5_the_creators_request_keeps_its_clips_out_of_a_duplicates_card(
    world, v2_on, requirements, offered, keeps
):
    seeded = seed()
    if requirements:
        add_brief(seeded, requirements)
    ctx, plan = await loaded(seeded)
    cards, _ = kinds_of(ctx, plan)
    assert ("duplicates" in cards) is offered
    if keeps:
        assert cards["duplicates"].why.startswith(keeps), cards["duplicates"].why


def _copy_event(seeded, payload, *, role="assistant", seq):  # noqa: ANN001, ANN202
    with sync_session() as db:
        db.add(
            CreationThreadEvent(
                thread_id=seeded.thread_id,
                sequence=seq,
                revision=5 + seq,
                role=role,
                event_type="assistant_message" if role == "assistant" else "user_message",
                content="x",
                payload=payload,
            )
        )
        db.commit()


@pytest.mark.asyncio
async def test_w5_a_title_requirement_or_an_earlier_decision_hides_the_hook_card(world, v2_on):
    seeded = seed()
    ctx, plan = await loaded(seeded)
    assert "hook" in kinds_of(ctx, plan, hook_pick())[0]

    add_brief(seeded, [_req("r1", "text", "title", literal="Sunday at the market")])
    ctx, plan = await loaded(seeded)
    assert ctx.kind_inputs["hook"]["title_requirement"] is True
    assert "hook" not in kinds_of(ctx, plan, hook_pick())[0]
    assert ctx.brief_headline == 'text: "Sunday at the market"'


@pytest.mark.asyncio
async def test_w5_a_tapped_hook_card_is_a_decision_and_hides_the_next_one(world, v2_on):
    seeded = seed()
    ctx, _ = await loaded(seeded)
    pin = {"set_id": "sg_1", "id": "ss_1"}
    _copy_event(seeded, {"pinned_suggestion": pin}, role="user", seq=4)
    _copy_event(
        seeded,
        {
            "creative_copy_resolution": {
                "target": "opening_title",
                "status": "suggestion_approved",
                "source": "suggestion",
                "text": "Matcha before sunrise",
                "dependency_digest": ctx.media_digest,
                "suggestion": {"set_id": "sg_1", "id": "ss_1"},
            }
        },
        seq=5,
    )
    ctx, plan = await loaded(seeded)
    assert ctx.kind_inputs["hook"]["opening_copy"] == "approved"
    assert "hook" not in kinds_of(ctx, plan, hook_pick())[0]


# ── W6 ──────────────────────────────────────────────────────────────────────────────

GEMINI_ENGLISH = "so today we are going to make the best matcha latte you have ever had"


def _speaking_clips():  # noqa: ANN202
    from app.tasks import autoplace

    clips = footage()
    first = clips[0]
    first["generation"] = "1"
    first["analysis"]["analysis_version"] = autoplace.ANALYSIS_VERSION
    first["analysis"]["understanding"]["speech"] = {
        "has_speech": True,
        "to_camera": False,
        "transcript": GEMINI_ENGLISH,
    }
    return clips


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("row", "offered"),
    [
        (dict(language="en"), True),  # whisper and Gemini agree
        (dict(language="tr"), False),  # whisper translated into Turkish: the scheduler redoes it
        (dict(engine_version="preflight-v0-old"), False),  # superseded by the scheduler
        (dict(ready=False), False),
        (dict(filler=2, removed_ms=900), False),
    ],
    ids=["agree", "language_redo", "old_engine", "not_ready", "too_few"],
)
async def test_w6_filler_evidence_never_offers_a_row_the_scheduler_will_redo(
    world, v2_on, monkeypatch, row, offered
):
    seeded = seed(_speaking_clips())
    install_filler(monkeypatch, **row)
    ctx, plan = await loaded(seeded)
    assert ("filler_words" in kinds_of(ctx, plan)[0]) is offered


# ── W7, W8 ──────────────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_w7_a_turkish_thread_gets_turkish_cards_and_a_closed_hook_sentence(
    world, v2_on, monkeypatch
):
    seeded = seed()
    install_filler(monkeypatch)
    ctx, plan = await loaded(seeded, language="tr")
    assert ctx.language == "tr"
    cards = with_hook(ctx, plan, "Gün doğmadan matcha")
    assert cards["filler_words"].title == "Dolgu sözcüklerini kes"
    assert [cards["filler_words"].why, *cards["filler_words"].evidence] == [
        "4 dolgu sesi",
        "1,8 sn kısalır",
    ]
    assert cards["duplicates"].why == "2. klip kalır: daha net"
    assert cards["duplicates"].prompt == "1, 2. kliplerden daha iyisini tut"
    assert cards["mute_clip"].title == "3. klibi sustur"
    hook = cards["hook"]
    assert hook.prompt == "Kartta gösterilen kancayı ekle"
    assert hook.copy_text == "Gün doğmadan matcha"


@pytest.mark.asyncio
async def test_w8_the_hook_prompt_is_closed_and_the_legacy_chip_is_untouched(world, v2_on):
    seeded = seed()
    ctx, plan = await loaded(seeded)
    text = "Matcha before sunrise"
    cards, _ = kinds_of(ctx, plan, hook_pick(text))
    assert cards["hook"].prompt == "Add the hook shown on the card"
    assert text.casefold() not in cards["hook"].prompt.casefold()
    out = sc.assemble_set(ctx, plan, hook_pick(text))
    chip = next(c for c in out.chips if c.kind == "hook")
    assert chip.message == "Add a hook that matches the video"  # the KRI-756 chip keeps its words


# ── W9 ──────────────────────────────────────────────────────────────────────────────


def add_head_draft(seeded, snapshot_hash):  # noqa: ANN001, ANN202
    from app.models import CreatorEditDraft

    with sync_session() as db:
        db.add(
            CreatorEditDraft(
                creator_id=seeded.user_id,
                thread_id=seeded.thread_id,
                item_id=seeded.item_id,
                variant_key="original_text",
                base_job_id=seeded.job_id,
                draft_revision=1,
                snapshot_json=None,
                snapshot_hash=snapshot_hash,
                is_head=True,
            )
        )
        db.commit()


@pytest.mark.asyncio
async def test_w9_the_freshness_key_moves_with_the_brief_the_draft_and_the_signals(
    world, v2_on, monkeypatch
):
    seeded = seed()
    ctx, base = await loaded(seeded)
    assert ctx.kind_inputs["freshness"] == {"snapshot_hash": None, "brief_version": None}
    keys = {base.input_digest}

    add_brief(seeded, [_req("r1", "timing", "global", description="twenty seconds")])
    ctx, briefed = await loaded(seeded)
    assert ctx.kind_inputs["freshness"]["brief_version"] == 1
    keys.add(briefed.input_digest)

    add_head_draft(seeded, "h" * 32)
    ctx, drafted = await loaded(seeded)
    assert ctx.kind_inputs["freshness"]["snapshot_hash"] == "h" * 32
    keys.add(drafted.input_digest)

    monkeypatch.setattr(clip_signals, "signals_version", lambda: "sv_new00000")
    ctx, resignaled = await loaded(seeded)
    keys.add(resignaled.input_digest)
    assert len(keys) == 4, "each of the brief, the head draft and the signals re-keys the set"

    # The worker and the GET both run loader + with_transcripts + plan_for: same inputs, same key.
    _, again = await loaded(seeded)
    assert again.input_digest == resignaled.input_digest
    assert again.set_id == resignaled.set_id

    # And the digest the wire carries is the same ``SuggestionDigest`` the key was built from.
    built, _ = sc.assemble_suggestions(ctx, resignaled)
    assert built.digest.brief_version == 1
    assert built.digest.snapshot_hash == "h" * 32
    assert built.digest.signals_version == "sv_new00000"


# ── W10, W11 ────────────────────────────────────────────────────────────────────────


@pytest.fixture
def no_redis(monkeypatch):
    def down():  # noqa: ANN202
        raise ConnectionError("redis is down")

    monkeypatch.setattr(store, "_redis", down)


async def serve_get(seeded, *, language="en"):  # noqa: ANN001, ANN202
    async with AsyncSessionLocal() as db:
        thread = await db.get(CreationThread, seeded.thread_id)
        return await serve.serve_suggestion_set(db, thread, None, 13, locale=language)


def _normalized(served):  # noqa: ANN001, ANN202
    wire = served.out.model_dump(mode="json")
    wire["set_id"] = "<set>"
    return {
        "wire": wire,
        "input_digest": served.input_digest,
        "media_digest": served.media_digest,
        "witness_tiers": served.witness_tiers,
    }


@pytest.mark.asyncio
async def test_w10_every_flag_off_is_byte_identical_to_the_pre_change_golden(world, no_redis):
    # Pinned taps stay on (the world turns them on) so the golden also holds the ported editor
    # kinds' v2 cards and the digest; every flag this lane adds is off.
    for name in (*ALL_KIND_FLAGS, "kria_suggestion_editor_triggers_enabled"):
        assert getattr(settings, name) is False, name
    seeded = seed(footage())
    ctx, plan = await loaded(seeded)
    assert set(ctx.kind_inputs) == {"transcripts"}
    assert ctx.brief_headline is None
    assert plan == sc.build_candidates(ctx)
    got = _normalized(await serve_get(seeded))
    assert got["wire"]["suggestions"], "the golden must hold v2 cards, not only chips"
    if os.environ.get("WIRE_SERVE_WRITE_GOLDEN"):
        GOLDEN.parent.mkdir(parents=True, exist_ok=True)
        GOLDEN.write_text(json.dumps(got, indent=2, sort_keys=True) + "\n")
    assert got == json.loads(GOLDEN.read_text())


@pytest.mark.asyncio
async def test_w10_flag_off_reads_no_brief_no_head_and_no_kind_input(world, monkeypatch):
    seeded = seed()
    monkeypatch.setattr("app.kria.brief.load_latest_brief", _boom_async)
    monkeypatch.setattr(serve, "_head_snapshot_hash", _boom_async)
    monkeypatch.setattr(serve, "filler_evidence", _boom_async)
    ctx, plan = await loaded(seeded)  # would log and degrade if any of those ran; it must not
    assert set(ctx.kind_inputs) == {"transcripts"}


def add_plan_draft(seeded):  # noqa: ANN001, ANN202
    """The strategy draft the render was made from: what a restating card re-drafts."""
    from app.agents._schemas.creator_agent import CreativeStrategy
    from app.kria.drafts import KriaDraftDocument, canonical_snapshot
    from app.models import CreatorEditDraft

    strategy = CreativeStrategy.model_validate(
        {"edit_format": "montage", "rationale": "A matcha restock."}
    ).model_dump(mode="json", exclude_none=True)
    snapshot, digest = canonical_snapshot(
        KriaDraftDocument(
            kind="strategy",
            intent="A matcha video",
            edit_format="montage",
            strategy=strategy,
            changes=[],
        )
    )
    with sync_session() as db:
        db.add(
            CreatorEditDraft(
                creator_id=seeded.user_id,
                thread_id=seeded.thread_id,
                item_id=seeded.item_id,
                variant_key="original_text",
                base_job_id=seeded.job_id,
                base_generation_id="generation-1",
                draft_revision=0,
                snapshot_json=snapshot,
                snapshot_hash=digest,
                is_head=True,
            )
        )
        db.commit()


@pytest.mark.asyncio
async def test_w11_a_redis_outage_serves_the_deterministic_cards_and_never_raises(
    world, v2_on, no_redis, monkeypatch
):
    seeded = seed()
    # A restating card (mute, filler) is only served over a plan it can re-draft; without one its
    # tap is refused, so the GET leaves it out.
    add_plan_draft(seeded)
    install_filler(monkeypatch)
    served = await serve_get(seeded)
    assert served.out.status == "ok"
    kinds = {c.kind for c in served.out.suggestions}
    assert {"duplicates", "mute_clip", "filler_words"} <= kinds
    assert [c.kind for c in served.out.chips]  # the legacy chips still come with it


# ── W12, W13: the signals hook ──────────────────────────────────────────────────────

MEASURED = {"audio_quality": NOISY, "near_dup_hash": _dup(_TAKE_A, 80)}


def understood_only():  # noqa: ANN202
    """Clips whose understanding is done and whose signals were never measured (a clip understood
    before the signals existed, or just now)."""
    return [_clip(i) for i in range(3)]


def stored_signals(seeded):  # noqa: ANN001, ANN202
    with sync_session() as db:
        item = db.get(PlanItem, seeded.item_id)
        return {
            a["media_id"]: sorted(clip_signals.read_signals(a.get("analysis")))
            for a in item.clip_assignments
        }


@pytest.fixture
def lock_probe(world, monkeypatch):
    """Record every suggester enqueue, and whether the plan item's row lock was free at that moment
    (a trigger fired inside the store's transaction would find it held)."""
    calls: list[dict] = []

    def probe_factory(item_id):  # noqa: ANN001, ANN202
        def enqueue(thread_id, **kwargs):  # noqa: ANN001, ANN003, ANN202
            with sync_session() as other:
                try:
                    other.execute(
                        select(PlanItem.id)
                        .where(PlanItem.id == item_id)
                        .with_for_update(nowait=True)
                    ).first()
                    free = True
                except Exception:  # noqa: BLE001 - a held lock is LockNotAvailable
                    free = False
            calls.append({"trigger": kwargs.get("trigger"), "lock_free": free})
            return True

        return enqueue

    calls_obj = SimpleNamespace(
        calls=calls,
        arm=lambda item_id: monkeypatch.setattr(serve, "enqueue_suggester", probe_factory(item_id)),
    )
    return calls_obj


@pytest.fixture
def measure(monkeypatch):
    """The audio and frame measurement is replaced (it needs a downloaded file); the fenced store
    write, the selection of what is missing and the trigger are the real ones."""
    from app.cli import backfill_clip_signals as cli

    seen: list[str] = []

    def fake(row, pending, todo, _max_bytes):  # noqa: ANN001, ANN202
        seen.append(pending.media_id)
        return "measured", "7", {name: MEASURED[name] for name in todo}

    monkeypatch.setattr(cli, "_measure", fake)
    return seen


@pytest.mark.asyncio
async def test_w12_the_backfill_write_fires_the_trigger_once_per_item_after_the_lock(
    world, v2_on, lock_probe, measure
):
    from app.cli import backfill_clip_signals as cli

    seeded = seed(understood_only())
    lock_probe.arm(seeded.item_id)

    dry = cli.run(item_id=seeded.item_id, write=False)
    assert dry["clips_needing_signals"] == 3 and lock_probe.calls == []

    report = cli.run(item_id=seeded.item_id, write=True, rate_per_s=0)
    assert report["outcomes"] == {"stored": 3}
    assert measure == ["clip-a", "clip-b", "clip-c"]
    assert stored_signals(seeded) == {m: ["audio_quality", "near_dup_hash"] for m in h.MEDIA_IDS}
    assert lock_probe.calls == [{"trigger": "signals_computed", "lock_free": True}]

    again = cli.run(item_id=seeded.item_id, write=True, rate_per_s=0)
    assert again["clips_needing_signals"] == 0 and len(lock_probe.calls) == 1


@pytest.mark.asyncio
async def test_w12_control_the_probe_sees_a_lock_that_is_held(world, lock_probe):
    seeded = seed(understood_only())
    lock_probe.arm(seeded.item_id)
    with sync_session() as db:
        db.execute(select(PlanItem.id).where(PlanItem.id == seeded.item_id).with_for_update())
        serve.enqueue_suggester("t", trigger="inside_the_lock")
        db.rollback()
    assert lock_probe.calls == [{"trigger": "inside_the_lock", "lock_free": False}]


@pytest.mark.asyncio
async def test_w12_nothing_stored_means_no_trigger(world, v2_on, lock_probe, monkeypatch):
    from app.cli import backfill_clip_signals as cli

    seeded = seed(understood_only())
    lock_probe.arm(seeded.item_id)
    monkeypatch.setattr(cli, "_measure", lambda *a: ("generation_changed", "8", {}))
    report = cli.run(item_id=seeded.item_id, write=True, rate_per_s=0)
    assert report["outcomes"] == {"generation_changed": 3}
    assert lock_probe.calls == []


@pytest.mark.asyncio
async def test_w12_the_trigger_is_off_without_the_editor_triggers_flag(
    world, lock_probe, measure, monkeypatch
):
    from app.cli import backfill_clip_signals as cli

    monkeypatch.setattr(settings, "kria_suggestion_signals_enabled", True)
    seeded = seed(understood_only())
    lock_probe.arm(seeded.item_id)
    cli.run(item_id=seeded.item_id, write=True, rate_per_s=0)
    assert lock_probe.calls == []
    assert stored_signals(seeded)["clip-a"] == ["audio_quality", "near_dup_hash"]


@pytest.mark.parametrize("outage", [True, False], ids=["redis_down", "lookup_fails"])
def test_w12_a_failing_trigger_never_raises(world, v2_on, monkeypatch, outage):
    if outage:
        monkeypatch.setattr(
            store,
            "claim_refresh_slot",
            lambda *a: (_ for _ in ()).throw(store.StoreUnavailable("down")),
        )
        seeded = seed(understood_only())
        assert serve.on_signals_computed(seeded.item_id) >= 0
    else:
        monkeypatch.setattr(serve, "_active_threads", _boom)
        assert serve.on_signals_computed(uuid.uuid4()) == 0


@pytest.mark.asyncio
async def test_w13_understanding_measures_signals_then_words_the_set(
    world, v2_on, lock_probe, measure
):
    from app.tasks import kria_clip_understanding as task

    seeded = seed(understood_only())
    lock_probe.arm(seeded.item_id)
    result = task._run(str(seeded.item_id), 0)
    assert result["status"] == "done"
    assert stored_signals(seeded)["clip-c"] == ["audio_quality", "near_dup_hash"]
    # Signals first, so the first set already carries them; both triggers then fire.
    assert [c["trigger"] for c in lock_probe.calls] == ["signals_computed", "understanding_done"]
    assert all(c["lock_free"] for c in lock_probe.calls)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "off",
    ["kria_suggestion_signals_enabled", "kria_suggestion_chips_enabled"],
)
async def test_w13_no_flag_no_measurement(world, v2_on, lock_probe, monkeypatch, off):
    from app.cli import backfill_clip_signals as cli
    from app.tasks import kria_clip_understanding as task

    seeded = seed(understood_only())
    lock_probe.arm(seeded.item_id)
    monkeypatch.setattr(settings, off, False)
    monkeypatch.setattr(cli, "_measure", _boom)
    assert task._run(str(seeded.item_id), 0)["status"] == "done"
    assert stored_signals(seeded) == {m: [] for m in h.MEDIA_IDS}


@pytest.mark.asyncio
async def test_w13_a_failing_measurement_never_fails_the_task(
    world, v2_on, lock_probe, monkeypatch
):
    from app.cli import backfill_clip_signals as cli
    from app.tasks import kria_clip_understanding as task

    seeded = seed(understood_only())
    lock_probe.arm(seeded.item_id)
    monkeypatch.setattr(cli, "_measure", _boom)
    assert task._run(str(seeded.item_id), 0)["status"] == "done"
    monkeypatch.setattr(cli, "run", _boom)  # even the whole backfill blowing up
    assert task._run(str(seeded.item_id), 0)["status"] == "done"
    assert [c["trigger"] for c in lock_probe.calls] == ["understanding_done"] * 2


@pytest.mark.asyncio
async def test_w13_an_almost_spent_run_defers_instead_of_measuring(
    world, v2_on, lock_probe, monkeypatch
):
    from app.cli import backfill_clip_signals as cli
    from app.tasks import kria_clip_understanding as task

    seeded = seed(understood_only())
    lock_probe.arm(seeded.item_id)
    monkeypatch.setattr(cli, "_measure", _boom)
    monkeypatch.setattr(task, "RUN_DEADLINE_S", task.SIGNALS_RESERVE_S - 1)
    resumed: list[tuple] = []
    monkeypatch.setattr(task, "_resume", lambda *a, **k: resumed.append((a, k)) or True)
    result = task._run(str(seeded.item_id), 0)
    assert result["resumed"] is True and len(resumed) == 1
    assert stored_signals(seeded) == {m: [] for m in h.MEDIA_IDS}
    assert lock_probe.calls == []  # the resumed run words the set, once the signals are in
