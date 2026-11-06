"""N7: the first prompt. Footage uploaded, nothing said, no render.

Failure catalogue (written first; each row is a line of ``First prompt`` in the report):

  F1  a v2 card is served before there is an edit. Nothing exists for a card to apply to, so the
      GET carries no ``suggestions`` (the plan's hook and duplicates cards at first prompt are
      prompt-only chips, not pinned cards).
  F2  a ready-only kind leaks into a first prompt: duplicates (needs two takes in a cut), mute,
      filler words (needs a speech-cleanup analysis of a take), music level, sound effects or
      transitions (nothing is cut yet).
  F3  a first-prompt chip is pinned. A chip tap is the creator's own sentence for the planner, the
      KRI-756 path. It must reach the planner exactly once with the chip's words, must not be
      stamped as a pinned suggestion, must not write a draft or a brief behind the planner's back.
  F4  the first-prompt thread is thinner than a real one. The shared pinned harness stubs the
      creator manifest (no media, no render capabilities), so every strategy dry-run refuses and a
      first prompt serves the default-edit chip alone. This module resolves the REAL manifest from
      the seeded clips, which is what makes the lever chips (a hook, captions) appear at all.
  F5  a language only one of the two gets: the chips and their messages are tapped in English and in
      Turkish (the same footage told in both).
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from app.kria import planner
from tests.e2e_suggestions import driver as d
from tests.e2e_suggestions import threads
from tests.e2e_suggestions.driver import Phone
from tests.e2e_suggestions.report import Recorder
from tests.tasks import kria_pinned_suggestion_harness as h

CASSETTES = Path(__file__).resolve().parent / "cassettes"

# Footage only (a first prompt has no recorded brief); the same footage in both languages.
FIRST_PROMPT = (
    "nermin-matcha-update",
    "nermin-matcha-update/tr",
    "east-run-brief/footage-only",
    "east-run-brief/footage-only/en",
)
# Kinds the first prompt must never offer, as a card or as a chip.
NOT_AT_FIRST_PROMPT = frozenset(
    {"duplicates", "mute_clip", "filler_words", "music", "sfx", "transition", "title_animation"}
)


def prefs(key: str) -> list[dict]:
    path = CASSETTES / f"{key.replace('/', '_')}.json"
    return json.loads(path.read_text(encoding="utf-8"))["prefs"]


def real_manifest(monkeypatch) -> None:  # noqa: ANN001
    monkeypatch.setattr(planner, "resolve_item_creator_context", d.REAL_MANIFEST_RESOLVER)
    monkeypatch.setattr(
        "app.services.creator_sessions.resolve_item_creator_context", d.REAL_MANIFEST_RESOLVER
    )


async def serve_first_prompt(world, monkeypatch, key):  # noqa: ANN001, ANN202
    real_manifest(monkeypatch)
    scn = threads.SCENARIOS[key]
    seeded = threads.seed_first_prompt(scn)
    phone = Phone(seeded, world, prefs(key), language=scn.language)
    return scn, seeded, phone, await phone.serve()


@pytest.mark.asyncio
@pytest.mark.parametrize("key", FIRST_PROMPT)
async def test_n7_first_prompt(talking_world, monkeypatch, key):
    rec = Recorder(f"first prompt {key}")
    scn, _seeded, _phone, out = await serve_first_prompt(talking_world, monkeypatch, key)
    chips = [c.kind for c in out.chips]
    rec.negative(
        "first prompt serves the default-edit chip first, lever chips after it, and no v2 card",
        key,
        "state first_prompt, understanding done, default_edit then levers, no cards on the wire",
        out.state == "first_prompt"
        and out.understanding == "done"
        and chips[:1] == ["default_edit"]
        and not out.suggestions
        and "suggestions" not in _wire(_phone),
        f"state={out.state} understanding={out.understanding} chips={chips} "
        f"cards={[c.kind for c in out.suggestions]}",
    )
    rec.negative(
        "first prompt offers none of the kinds that need an edit",
        key,
        f"none of {sorted(NOT_AT_FIRST_PROMPT)}",
        not (set(chips) & NOT_AT_FIRST_PROMPT),
        f"chips {chips}",
    )

    # Tap every chip, each on its own fresh first-prompt thread (a tap ends the first prompt).
    taps: dict[str, dict] = {}
    for kind in chips:
        scn, seeded, phone, out = await serve_first_prompt(talking_world, monkeypatch, key)
        chip = next(c for c in out.chips if c.kind == kind)
        planner_double = h.legacy_planner_answers(monkeypatch)
        before = h.counts(seeded)
        accepted, _ = await h.submit(
            seeded, chip.message, tap={"set_id": out.set_id, "chip_id": chip.id}
        )
        await h.run(accepted.turn_id)
        stamped = h.events(seeded.thread_id, "user_message")[-1].payload.get("pinned_suggestion")
        said = h.events(seeded.thread_id, "user_message")[-1].content
        after = h.counts(seeded)
        ok = bool(
            planner_double.await_count == 1
            and stamped is None
            and said == chip.message
            and after["drafts"] == before["drafts"]
            and after["briefs"] == before["briefs"]
        )
        taps[kind] = {
            "language": scn.language,
            "message": chip.message,
            "planner_calls": planner_double.await_count,
            "pinned": stamped is not None,
            "drafts_written": after["drafts"] - before["drafts"],
            "ok": ok,
        }
        rec.negative(
            f"a first-prompt {kind} chip tap stays on the KRI-756 path: the planner reads its "
            "words once, nothing is pinned or written behind it",
            key,
            "planner asked once; no pin; the chat line is the chip's message; no draft or brief",
            ok,
            f"planner calls={planner_double.await_count} stamp={stamped} said={said!r}",
        )
    rec.sections["first_prompt"] = {
        "scenario": key,
        "language": scn.language,
        "chips": chips,
        "taps": taps,
    }
    rec.finish()


def _wire(phone: Phone) -> dict:
    return phone.last_wire
