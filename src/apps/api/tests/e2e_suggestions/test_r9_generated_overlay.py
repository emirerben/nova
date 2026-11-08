"""R9 (KRI-1253): the generated-image card, the one tap that spends money.

Failure catalogue (written before the code; each row is a test below):

  G1  served only where it can run: protocol 45+ (the build that asks first), the generated-images
      gate (the route's own), the flag, and a thread with no image yet. The wire carries the price
      and the action type, never the prompt or the action.
  G2  a tap spends ONCE: one provider operation keyed on the card, a reply instead of a draft, the
      card consumed in the reply's transaction, a second tap refused ``consumed``.
  G3  a duplicate send / retried turn finds the first operation (the generator is keyed on
      ``generated_overlay.event_id``); the phone derives the same id (the eval pins the vector).
  G4  the generation fails, the budget is exhausted, the gate closed after the serve, the service
      raises: a typed refusal, nothing spent that was not metered, the edit untouched.
  G5  the outcome is unknown (the provider may have been paid): the card is consumed and the reply
      says it started, never "failed", so nothing is paid for twice.
  G6  the idea changed after the serve: the witness refuses the old prompt, the rebuild builds the
      new one at the same price.

The model is a cassette, the generator is a recorder (``execute_generated_media`` has its own
offline suite); everything between the GET and the reply is the product's.
"""

from __future__ import annotations

import uuid

import pytest

from app.config import settings
from app.database import sync_session
from app.models import PlanItem
from app.services import generated_media
from app.services.generated_media import CardGeneration
from app.services.suggestions import pinned
from app.services.suggestions.kinds import generated_overlay
from tests.e2e_suggestions import threads
from tests.e2e_suggestions.driver import Phone
from tests.e2e_suggestions.test_ring_store import KEY, prefs
from tests.e2e_suggestions.threads import EDITOR

PROTOCOL = generated_overlay.MIN_PROTOCOL
GATE = {
    "kria_suggestion_generated_overlay_enabled": True,
    "ios_native_device_only_enabled": True,
    "ai_cost_control_enabled": True,
    "generated_images_enabled": True,
    "generated_media_live_approved": True,
    "generated_media_billing_organization": "org-offline",
    "openai_api_key": "offline",
    "generated_media_max_request_usd": 2.0,
    "ai_openai_monthly_budget_usd": 100.0,
    "ai_openai_image_budget_usd": 50.0,
    "phone_render_verified_features": ["visualBlocks"],
}


def open_gate(monkeypatch) -> None:  # noqa: ANN001
    for name, value in GATE.items():
        monkeypatch.setattr(settings, name, value)


class Generator:
    """Stands in for ``generate_image_for_card``: records every call, answers a scripted state."""

    def __init__(self, state: str = "ready", failure_code: str | None = None) -> None:
        self.calls: list[dict] = []
        self.state = state
        self.failure_code = failure_code

    def __call__(self, **kwargs):  # noqa: ANN003, ANN204
        self.calls.append(kwargs)
        return CardGeneration(
            self.state,
            None if self.state == "failed" and not self.failure_code else uuid.uuid4(),
            self.failure_code,
        )


def generates(monkeypatch, state: str = "ready", failure_code: str | None = None) -> Generator:  # noqa: ANN001
    generator = Generator(state, failure_code)
    monkeypatch.setattr(generated_media, "generate_image_for_card", generator)
    return generator


def with_card_prefs() -> list[dict]:
    return [
        {
            "kind": "generated_overlay",
            "targets": ["all"],
            "why": "A picture could help",
            "copy": None,
        },
        *prefs(),
    ]


async def served(world, *, protocol: int | None = PROTOCOL):  # noqa: ANN001, ANN202
    scn = threads.SCENARIOS[KEY]
    seeded = threads.seed_thread(scn, EDITOR)
    phone = Phone(seeded, world, with_card_prefs(), language=scn.language, protocol=protocol)
    return seeded, phone, await phone.serve()


def kinds(out) -> list[str]:  # noqa: ANN001
    return [c.kind for c in out.suggestions]


def card(out):  # noqa: ANN001, ANN202
    return next(c for c in out.suggestions if c.kind == "generated_overlay")


def idea_of(seeded) -> str:  # noqa: ANN001
    with sync_session() as db:
        return db.get(PlanItem, seeded.item_id).idea


# ── G1: served only where it can run ──────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_g1_the_card_quotes_its_price_and_hides_its_action(world, monkeypatch):
    open_gate(monkeypatch)
    _seeded, phone, out = await served(world)
    assert "generated_overlay" in kinds(out)
    wire = next(c for c in phone.last_wire["suggestions"] if c["kind"] == "generated_overlay")
    assert wire["action_type"] == "async_generate"
    assert wire["cost_usd"] == generated_overlay.QUOTED_COST_USD
    assert wire["requires_protocol"] == PROTOCOL
    assert "prompt" in wire and "A simple, bright" not in str(wire), (
        "the image prompt stays server-side"
    )
    assert "action" not in wire and "spec" not in wire


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "why",
    ["older_build", "no_protocol", "flag_off", "images_off", "ledger_off", "no_visual_blocks"],
)
async def test_g1_the_card_is_not_served_where_it_cannot_run(world, monkeypatch, why):
    open_gate(monkeypatch)
    protocol = {"older_build": PROTOCOL - 1, "no_protocol": None}.get(why, PROTOCOL)
    if why == "flag_off":
        monkeypatch.setattr(settings, "kria_suggestion_generated_overlay_enabled", False)
    elif why == "images_off":
        monkeypatch.setattr(settings, "generated_images_enabled", False)
    elif why == "ledger_off":
        monkeypatch.setattr(settings, "ai_cost_control_enabled", False)
    elif why == "no_visual_blocks":
        monkeypatch.setattr(settings, "phone_render_verified_features", [])
    _seeded, _phone, out = await served(world, protocol=protocol)
    assert "generated_overlay" not in kinds(out), why


# ── G2/G3: a tap spends once ──────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_g2_a_tap_spends_once_and_edits_nothing(world, monkeypatch):
    open_gate(monkeypatch)
    generator = generates(monkeypatch)
    seeded, phone, out = await served(world)
    tapped = await phone.tap(card(out), out)

    assert len(generator.calls) == 1
    call = generator.calls[0]
    assert call["owner_id"] == seeded.user_id and call["item_id"] == seeded.item_id
    assert idea_of(seeded) in call["prompt"], "the image is drawn from the item's own idea"
    assert call["event_id"] == generated_overlay.event_id(out.set_id, card(out).id)

    assert tapped.new_drafts == 0 and tapped.before == tapped.after, "no draft, no brief, no render"
    assert tapped.head_before == tapped.head_after
    record = tapped.refused or {}
    assert record.get("outcome") == "generated", record
    assert record.get("event_id") == str(call["event_id"]) and record.get("state") == "ready"
    assert "review" in tapped.reply.lower(), tapped.reply

    again = await phone.tap(card(out), out)
    assert (again.refused or {}).get("reason") == pinned.CONSUMED, again.refused
    assert len(generator.calls) == 1, "a second tap never reaches the provider"

    after = await phone.serve()
    assert "generated_overlay" not in kinds(after), "a spent card is not offered again"


@pytest.mark.asyncio
async def test_g3_the_operation_is_keyed_on_the_card_so_a_retry_finds_it(world, monkeypatch):
    open_gate(monkeypatch)
    generator = generates(monkeypatch)
    _seeded, phone, out = await served(world)
    await phone.tap(card(out), out)
    # A client retry of the same tap (a new turn, the same card): the key is the same.
    with_other_set = generated_overlay.event_id("sg_other", card(out).id)
    assert generator.calls[0]["event_id"] != with_other_set
    assert generator.calls[0]["event_id"] == generated_overlay.event_id(out.set_id, card(out).id)


# ── G4/G5: what a tap can end in besides an image ─────────────────────────────────────


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("state", "code", "reason"),
    [
        ("failed", "provider_rejected", pinned.GENERATION_FAILED),
        ("failed", "budget_unavailable", pinned.BUDGET_UNAVAILABLE),
        ("failed", None, pinned.GENERATION_FAILED),
    ],
)
async def test_g4_a_failed_generation_is_a_typed_refusal(world, monkeypatch, state, code, reason):
    open_gate(monkeypatch)
    generator = generates(monkeypatch, state, code)
    _seeded, phone, out = await served(world)
    tapped = await phone.tap(card(out), out)
    assert len(generator.calls) == 1
    assert tapped.refused and tapped.refused["outcome"] == "refused", tapped.refused
    assert tapped.refused["reason"] == reason
    assert tapped.new_drafts == 0 and tapped.before == tapped.after
    assert "Nothing in the edit changed" in tapped.reply or "Düzenlemede" in tapped.reply
    after = await phone.serve()
    assert "generated_overlay" not in kinds(after) or card(after).id != card(out).id


@pytest.mark.asyncio
async def test_g4_a_gate_closed_after_the_serve_never_reaches_the_provider(world, monkeypatch):
    open_gate(monkeypatch)
    generator = generates(monkeypatch)
    _seeded, phone, out = await served(world)
    monkeypatch.setattr(settings, "generated_images_enabled", False)
    tapped = await phone.tap(card(out), out)
    assert generator.calls == []
    assert (tapped.refused or {}).get("outcome") == "refused"
    assert tapped.new_drafts == 0 and tapped.before == tapped.after


@pytest.mark.asyncio
async def test_g4_the_kill_switch_stops_a_card_served_before_it_was_pulled(world, monkeypatch):
    open_gate(monkeypatch)
    generator = generates(monkeypatch)
    _seeded, phone, out = await served(world)
    monkeypatch.setattr(settings, "kria_suggestion_generated_overlay_enabled", False)
    tapped = await phone.tap(card(out), out)
    assert generator.calls == []
    assert (tapped.refused or {}).get("outcome") == "refused"


@pytest.mark.asyncio
async def test_g4_a_service_that_raises_is_a_refusal_not_a_failed_turn(world, monkeypatch):
    open_gate(monkeypatch)

    def boom(*_a, **_k):  # noqa: ANN002, ANN003
        raise ValueError("Generated media requires an enforced ledger")

    monkeypatch.setattr(generated_media, "execute_generated_media", boom)
    _seeded, phone, out = await served(world)
    tapped = await phone.tap(card(out), out)
    assert (tapped.refused or {}).get("reason") == pinned.GENERATION_FAILED, tapped.refused
    assert tapped.new_drafts == 0 and tapped.before == tapped.after


@pytest.mark.asyncio
async def test_g5_an_unknown_outcome_is_consumed_and_never_retried(world, monkeypatch):
    open_gate(monkeypatch)
    generator = generates(monkeypatch, "unknown", "provider_outcome_unknown")
    _seeded, phone, out = await served(world)
    tapped = await phone.tap(card(out), out)
    record = tapped.refused or {}
    assert record.get("outcome") == "generated" and record.get("state") == "unknown", record
    assert "started" in tapped.reply.lower(), tapped.reply
    again = await phone.tap(card(out), out)
    assert (again.refused or {}).get("reason") == pinned.CONSUMED
    assert len(generator.calls) == 1


# ── G6: the idea moved after the serve ────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_g6_a_changed_idea_rebuilds_the_prompt_at_the_same_price(world, monkeypatch):
    open_gate(monkeypatch)
    generator = generates(monkeypatch)
    seeded, phone, out = await served(world)
    with sync_session() as db:
        db.get(PlanItem, seeded.item_id).idea = "A quiet morning at the market"
        db.commit()
    tapped = await phone.tap(card(out), out)
    assert (tapped.refused or {}).get("outcome") == "generated", tapped.refused
    assert "A quiet morning at the market" in generator.calls[0]["prompt"]


@pytest.mark.asyncio
async def test_g7_a_price_that_moved_after_the_serve_is_never_charged_unseen(world, monkeypatch):
    open_gate(monkeypatch)
    generator = generates(monkeypatch)
    _seeded, phone, out = await served(world)
    monkeypatch.setattr(generated_overlay, "QUOTED_COST_USD", generated_overlay.QUOTED_COST_USD * 2)
    tapped = await phone.tap(card(out), out)
    assert (tapped.refused or {}).get("reason") == pinned.BASE_MOVED, tapped.refused
    assert generator.calls == [], "the creator agreed to the old price, so nothing was spent"


def test_the_witness_proves_the_offer_it_was_built_from():
    from app.services.suggestions.context import SuggestionContext

    ctx = SuggestionContext(
        state="ready",
        client_protocol=PROTOCOL,
        kind_inputs={generated_overlay.INPUT_KEY: {"allowed": True, "subject": "Matcha latte art"}},
    )
    kind = generated_overlay.KIND
    action = kind.build_action(ctx, [], None)
    assert action is not None and kind.witness(action, ctx).ok
    other = SuggestionContext(
        state="ready",
        client_protocol=PROTOCOL,
        kind_inputs={generated_overlay.INPUT_KEY: {"allowed": True, "subject": "Something else"}},
    )
    assert kind.witness(action, other).reason == "prompt_changed"
