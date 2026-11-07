"""R8: a tapped mute card, rendered. The creator's tap reaches the phone montage writer and the clip
goes silent.

The mute card is a restating card (``pinned.restates_strategy``): the tap stages a render approval,
approving it dispatches a NEW phone render (``dispatch_item_render_for``) with the brief binding the
tap wrote, and ``_run_phone_guided_job`` builds the recipe from the binding's ``clip_sound`` asks
(``plan_toggle_lane``). It never runs the editor-commit route (``prepare_editor_commit``): that one
recompiles the saved plan and is for editor-ops cards.

Failure catalogue (written first):

  R1  the tap writes a requirement the writer does not read: ``brief_sound_toggle_asks`` finds no
      "off" ask for the card's clip in the binding the dispatch is handed.
  R2  the writer reads it but the clip stays audible, or another clip goes silent: the recipe's
      per-cut volume is judged for every cut, the muted clip's cuts and every other cut.
  R3  the receipt claims "met" for a recipe that plays the sound: the re-judged receipt is read
      from the pinned recipe, and a control with the requirement removed must NOT be met.

The thread's clip ids differ from the KRI-942 fixture's, so the one requirement the tap wrote is
carried over by moving its ``clip:<media id>`` scope onto the fixture's butcher clip; everything
else (the typed update, the binding, the ask reader, the writer, the compiler, the pilot check)
is the real code. Artifact: ``.dev/phone-render-verify/mute-card-render.json``.
"""

from __future__ import annotations

import copy
import json
from pathlib import Path

import pytest

from app.kria.brief import BriefRequirement, CreativeBrief
from app.kria.clip_sound_toggle_ask import SOUND_OFF, brief_sound_toggle_asks
from app.services.suggestions.kinds import mute_clip
from tests.e2e_suggestions import scenes as s
from tests.e2e_suggestions.threads import SIGNALS
from tests.tasks import test_kri942_clip_sound_toggle as k

ARTIFACT = Path(__file__).resolve().parents[5] / ".dev" / "phone-render-verify"


@pytest.mark.asyncio
async def test_r8_a_tapped_mute_card_silences_the_clip_in_the_phone_recipe(
    talking_world, monkeypatch
):
    monkeypatch.setattr(mute_clip, "PINNED_TAP_APPLIES_CLIP_SOUND", True)
    scene = s.build(talking_world, "nermin-matcha-update", SIGNALS)
    out = await scene.phone.serve()
    card = next(c for c in out.suggestions if c.kind == "mute_clip")
    tap = await scene.phone.tap(card, out)
    assert tap.planned and tap.result.get("status") == "awaiting_approval", (tap.refused, tap.reply)
    run = await scene.phone.approve_render(monkeypatch)
    assert run.decision == "approved" and len(run.calls) == 1
    binding = run.calls[0]["creator_brief_binding"]

    thread_brief = CreativeBrief.model_validate(binding["brief"])
    asks = brief_sound_toggle_asks(thread_brief)
    tapped = [a for a in asks if a.state == SOUND_OFF]
    assert len(tapped) == 1, asks  # R1
    thread_clip = card.anchor.media_id
    assert tapped[0].target.media_ref == thread_clip

    row = next(
        r
        for r in thread_brief.live()
        if (r.facts or {}).get("clip_sound") == SOUND_OFF and r.scope == f"clip:{thread_clip}"
    )
    moved = BriefRequirement.model_validate(
        {**row.model_dump(mode="json"), "scope": f"clip:{k.BUTCHER}"}
    )
    muted_brief = CreativeBrief(version=thread_brief.version, requirements=[moved])
    control_brief = CreativeBrief(version=thread_brief.version, requirements=[])

    control = k._run(monkeypatch, brief=control_brief)
    muted = k._run(monkeypatch, brief=muted_brief)
    butcher_cuts = [
        m["moment_id"]
        for m in k.DATA["execution_plan"]["story_timeline"]
        if m["media_id"] == k.BUTCHER
    ]
    others = [c for c in k.CUTS if c not in butcher_cuts]
    before, after = k._volumes(control), k._volumes(muted)
    assert butcher_cuts and set(before.values()) == {1.0}
    assert {c: after[c] for c in butcher_cuts} == dict.fromkeys(butcher_cuts, 0.0)  # R2
    assert {c: after[c] for c in others} == dict.fromkeys(others, 1.0)

    verdict = k._judge(muted, muted_brief)[moved.id]
    refused = k._judge(control, muted_brief)[moved.id]
    assert verdict["status"] == "met", verdict  # R3
    assert refused["status"] != "met", refused

    ARTIFACT.mkdir(parents=True, exist_ok=True)
    (ARTIFACT / "mute-card-render.json").write_text(
        json.dumps(
            {
                "card": {"kind": card.kind, "prompt": card.prompt, "clip": thread_clip},
                "dispatch_binding_requirement": copy.deepcopy(row.model_dump(mode="json")),
                "carried_to_fixture_clip": k.BUTCHER,
                "cut_volumes_before": before,
                "cut_volumes_after": after,
                "muted_cuts": butcher_cuts,
                "receipt_with_mute": verdict["status"],
                "receipt_without_mute": refused["status"],
            },
            indent=2,
            sort_keys=True,
        )
    )
