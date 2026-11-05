"""KRI-129 for the five ported editor kinds (AI suggestion blocks, fix-gates lane).

"The creator's request wins" was enforced in code for hook, duplicates and mute_clip only. The
five kinds that came over from the KRI-756 chips (captions, music, sfx, transition,
title_animation) never read the brief; the edit-improver skill text was the only protection, and
it does not exist on the deterministic path (agent budget stop, schema void, empty ranking) or
when the model simply picks the kind. Each row below was RED on ``codex/sb-prove`` and is GREEN
after (run both ways, see the commit message).

Failure catalogue, written before any code
  B1  the creator asked for the opposite and the card undoes it: "no captions" then a "turn the
      captions on" card; "no music" or "music off" then a music card; "no sound effects" then an
      sfx card; "hard cuts only" then a transition card; "no animation" then an animation card.
  B2  the creator already asked for exactly this and the card restates it: a second pass over
      something they said (captions asked on, music asked down, a crossfade asked, a typewriter
      title asked). A restating tap is refused later (GAP-5) and still commits brief updates
      (GAP-6), so it must never be served.
  B3  the ask is worded, not typed: no typed fact exists for transitions, sound effects or music
      level, and a Turkish ask carries no English word ("müzik olmasın", "geçiş olmasın",
      "altyazı", "animasyon"). The wording reader must fold Turkish letters like the rest of the
      server.
  B4  the deterministic fallback: the same rule must hold with no agent at all (picks=None), for
      the legacy chip set, and for an agent that picks the kind anyway.
  B5  a superseded requirement still suppresses a card, or an unrelated requirement (a title, a
      cut order, the on-screen words of a text ask) suppresses a card it has nothing to say about.
  B6  fail open on an unreadable brief: the loader handed over something that is not a brief
      (a dict, a string). The kind must offer nothing, not guess.
  B6b the loader itself fails (a transient DB error) with a v2 flag on. The key is then absent,
      which ``spoke`` reads as "flags off", so the five kinds were offered even though the creator
      may have said "no captions". An unreadable brief and an absent one must differ.
  B7  a legacy context (the loader never ran, flags off) changes: no "brief" key at all must be
      byte-identical to today, card for card and chip for chip.
"""

from __future__ import annotations

import dataclasses
from types import SimpleNamespace

import pytest

from app.config import settings
from app.kria.brief import BriefRequirement, CreativeBrief
from app.services import suggestion_chips as sc
from app.services import suggestion_serve
from app.services.suggestions import brief_asks, registry
from app.services.suggestions.models import RankedPick, SuggestionAgentOutput
from tests.evals import suggestion_chips_harness as h

CASES = {c.name: c for c in h.all_cases()}

# The case that serves each kind, and the kind's name on the wire.
HOME = {
    "captions": "rd_captions_off_with_cues",
    "music": "rd_music_loud",
    "sfx": "rd_music_loud",
    "transition": "rd_music_loud",
    "title_animation": "rd_title_present_guided_story",
}
FIRST_PROMPT_HOME = {"captions": "fp_speech_and_broll", "sfx": "fp_speech_and_broll"}


@pytest.fixture(autouse=True)
def _flags(monkeypatch):
    monkeypatch.setattr(settings, "kria_suggestion_hook_copy_enabled", True)


def req(
    rid: str, kind: str, description: str, *, facts=None, scope="global", status="open"
) -> BriefRequirement:
    return BriefRequirement(
        id=rid, kind=kind, scope=scope, description=description, facts=facts or {}, status=status
    )


def with_brief(ctx, *reqs: BriefRequirement, version: int = 1):
    brief = CreativeBrief(version=version, requirements=list(reqs))
    return dataclasses.replace(ctx, kind_inputs={**ctx.kind_inputs, brief_asks.BRIEF_KEY: brief})


def home_ctx(kind: str):
    return h.build_context(CASES[HOME[kind]])


def chip_kinds(ctx) -> list[str]:
    plan = sc.build_candidates(ctx)
    return [c.kind for c in sc.assemble_set(ctx, plan, None).chips]


def card_kinds(ctx, pick: str | None = None) -> list[str]:
    """The kinds of the cards the engine serves: deterministic (no pick) or an agent's pick."""
    plan = sc.build_candidates(ctx)
    picks = None
    if pick is not None:
        ranked = [RankedPick(kind=pick, target_ids=["all"], why="", copy=None, rank=1)]
        picks = SuggestionAgentOutput(ranked=ranked)
    built, _ = sc.assemble_suggestions(ctx, plan, picks)
    return [s.kind for s in (built.suggestions if built else [])]


def issued_kinds(ctx) -> list[str]:
    return [str(c.kind) for c in sc.build_candidates(ctx).pool]


def assert_offered(kind: str, ctx) -> None:
    assert kind in issued_kinds(ctx), f"{kind}: not even a candidate"
    assert kind in chip_kinds(ctx), f"{kind}: no chip"
    if kind != "title_animation":  # a chip-only kind: no v2 card builder yet
        assert kind in card_kinds(ctx), f"{kind}: no deterministic card"


def assert_withheld(kind: str, ctx) -> None:
    assert kind not in issued_kinds(ctx)
    assert kind not in chip_kinds(ctx)
    assert kind not in card_kinds(ctx)
    assert kind not in card_kinds(ctx, pick=kind), "an agent pick brought the card back"


# ── B1-B3: the asks, typed and worded, in both languages ─────────────────────────────

ASKS = [
    # captions: typed off, typed on, worded, Turkish worded
    ("captions", req("r1", "style", "no captions please", facts={"captions": "off"})),
    ("captions", req("r1", "style", "burn the captions in", facts={"captions": "on"})),
    ("captions", req("r1", "style", "Captions small, white", facts={"caption_size": "small"})),
    ("captions", req("r1", "style", "hold off on any subtitles")),
    ("captions", req("r1", "style", "altyazı eklemeyelim")),
    # music: typed no_music, worded off, worded down, worded add, Turkish
    ("music", req("r1", "audio", "no music", facts={"audio_intent": "no_music"})),
    ("music", req("r1", "audio", "Add upbeat music")),
    ("music", req("r1", "audio", "turn the music down under my voice")),
    ("music", req("r1", "audio", "make the song louder at the end")),
    ("music", req("r1", "audio", "müzik olmasın")),
    # sfx
    ("sfx", req("r1", "audio", "no sound effects, keep it clean")),
    ("sfx", req("r1", "audio", "add a whoosh on every cut")),
    ("sfx", req("r1", "audio", "ses efekti ekle")),
    # transition
    ("transition", req("r1", "style", "hard cuts only, no transitions")),
    ("transition", req("r1", "style", "crossfade between the clips")),
    ("transition", req("r1", "style", "geçiş olmasın")),
    ("transition", req("r1", "style", "keep it as jump cuts")),
    # title animation
    (
        "title_animation",
        req("r1", "text", "title in the middle", facts={"animation": "typewriter"}),
    ),
    ("title_animation", req("r1", "text", "animated text", facts={"animation": "any"})),
    ("title_animation", req("r1", "style", "no animation on the title")),
    ("title_animation", req("r1", "text", "başlık animasyonlu olsun")),
]


@pytest.mark.parametrize(
    "kind, ask", [pytest.param(k, a, id=f"{k}:{a.description[:30]}") for k, a in ASKS]
)
def test_a_card_never_contradicts_or_restates_a_live_ask(kind, ask):
    ctx = home_ctx(kind)
    assert_offered(kind, ctx)  # control: the same context serves it with no ask
    assert_withheld(kind, with_brief(ctx, ask))


@pytest.mark.parametrize("kind", sorted(FIRST_PROMPT_HOME))
def test_the_first_prompt_chip_obeys_the_ask_too(kind):
    ctx = h.build_context(CASES[FIRST_PROMPT_HOME[kind]])
    ask = {
        "captions": req("r1", "style", "no captions", facts={"captions": "off"}),
        "sfx": req("r1", "audio", "no sound effects"),
    }[kind]
    assert kind in chip_kinds(ctx)
    assert kind not in chip_kinds(with_brief(ctx, ask))


def test_every_ported_kind_has_an_ask_row_and_a_home():
    assert set(HOME) == {"captions", "music", "sfx", "transition", "title_animation"}
    assert {k for k, _ in ASKS} == set(HOME)
    for kind in HOME:
        assert registry.get(kind) is not None


# ── B5: what does not suppress ───────────────────────────────────────────────────────


@pytest.mark.parametrize("kind", sorted(HOME))
def test_a_superseded_ask_is_not_an_ask(kind):
    ask = {
        "captions": req("r1", "style", "no captions", facts={"captions": "off"}),
        "music": req("r1", "audio", "no music", facts={"audio_intent": "no_music"}),
        "sfx": req("r1", "audio", "no sound effects"),
        "transition": req("r1", "style", "hard cuts only"),
        "title_animation": req("r1", "text", "title", facts={"animation": "any"}),
    }[kind]
    dead = ask.model_copy(update={"status": "superseded"})
    ctx = home_ctx(kind)
    assert_withheld(kind, with_brief(ctx, ask))
    assert_offered(kind, with_brief(ctx, dead))


UNRELATED = [
    req("r9", "text", "the opening title", facts={"position": "top"}),
    req("r9", "order", "start with the harbour shot"),
    req("r9", "timing", "keep the video under thirty seconds"),
    # the creator's on-screen words are not a statement about the editor's lanes
    BriefRequirement(
        id="r9",
        kind="text",
        scope="global",
        literal="Captions for coffee lovers",
        description="the title",
    ),
]


@pytest.mark.parametrize("kind", sorted(HOME))
@pytest.mark.parametrize("other", UNRELATED, ids=lambda r: f"{r.kind}:{r.description[:20]}")
def test_an_unrelated_ask_leaves_the_card_alone(kind, other):
    assert_offered(kind, with_brief(home_ctx(kind), other))


# ── B6, B7: unreadable and absent briefs ─────────────────────────────────────────────


@pytest.mark.parametrize("kind", sorted(HOME))
@pytest.mark.parametrize("junk", ["a brief", {"requirements": []}, 7])
def test_an_unreadable_brief_offers_nothing(kind, junk):
    ctx = home_ctx(kind)
    ctx = dataclasses.replace(ctx, kind_inputs={**ctx.kind_inputs, brief_asks.BRIEF_KEY: junk})
    assert_withheld(kind, ctx)


class _Db:
    def __init__(self, *, get_fails: bool = False):
        self.get_fails = get_fails

    async def get(self, model, key):
        if self.get_fails:
            raise RuntimeError("db down")
        return SimpleNamespace(clip_assignments=[])


def _break_loader(monkeypatch, how: str) -> None:
    async def boom(*args, **kwargs):
        raise RuntimeError("brief read failed")

    if how == "brief_read":
        monkeypatch.setattr("app.kria.brief.load_latest_brief", boom)
    elif how == "v2_stage":
        monkeypatch.setattr(suggestion_serve, "_with_v2_inputs", boom)


@pytest.mark.parametrize("how", ["brief_read", "v2_stage", "transcripts"])
@pytest.mark.parametrize("kind", sorted(HOME))
async def test_a_failed_brief_read_offers_nothing_not_everything(monkeypatch, kind, how):
    """B6b: through the real ``with_transcripts``, with a v2 flag on and the read failing."""
    monkeypatch.setattr(settings, "kria_suggestion_editor_triggers_enabled", True)
    assert suggestion_serve.v2_inputs_on()
    _break_loader(monkeypatch, how)
    ctx = home_ctx(kind)
    assert_offered(kind, ctx)  # the same context with no brief key is what the bug served
    thread = SimpleNamespace(id="t-1", active_plan_item_id="item-1")
    served = await suggestion_serve.with_transcripts(
        _Db(get_fails=how == "transcripts"), thread, ctx
    )
    assert_withheld(kind, served)


async def test_a_failed_brief_read_with_every_v2_flag_off_changes_nothing(monkeypatch):
    """Flags off is the legacy path: the read is never attempted, the context is as it was."""
    monkeypatch.setattr(settings, "kria_suggestion_editor_triggers_enabled", False)
    monkeypatch.setattr(suggestion_serve, "input_kinds_on", lambda: frozenset())
    _break_loader(monkeypatch, "brief_read")
    ctx = home_ctx("captions")
    thread = SimpleNamespace(id="t-1", active_plan_item_id="item-1")
    served = await suggestion_serve.with_transcripts(_Db(), thread, ctx)
    assert brief_asks.BRIEF_KEY not in served.kind_inputs
    assert_offered("captions", served)


@pytest.mark.parametrize("kind", sorted(HOME))
def test_no_brief_at_all_is_a_thread_without_asks(kind):
    """The loader ran and found no brief (``None``) or an empty one: nothing to obey."""
    ctx = home_ctx(kind)
    for brief in (None, CreativeBrief(version=0, requirements=[])):
        loaded = dataclasses.replace(
            ctx, kind_inputs={**ctx.kind_inputs, brief_asks.BRIEF_KEY: brief}
        )
        assert_offered(kind, loaded)


def test_a_context_without_the_brief_key_is_byte_identical_to_legacy():
    """B7: flags off, the loader never reads the brief; the plan and the wire do not move."""
    for name in HOME.values():
        ctx = h.build_context(CASES[name])
        assert brief_asks.BRIEF_KEY not in ctx.kind_inputs
        plan = sc.build_candidates(ctx)
        loaded = dataclasses.replace(
            ctx, kind_inputs={**ctx.kind_inputs, brief_asks.BRIEF_KEY: None}
        )
        assert sc.assemble_set(ctx, plan, None).model_dump() == (
            sc.assemble_set(loaded, sc.build_candidates(loaded), None).model_dump()
        )


def test_the_brief_key_is_the_one_the_serve_loader_writes():
    assert brief_asks.BRIEF_KEY == suggestion_serve.BRIEF_KEY
