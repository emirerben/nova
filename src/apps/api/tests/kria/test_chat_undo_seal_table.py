"""Guard: which phone chat edits "undo that" can put back exactly (KRI-1384).

A chat turn is undoable only when ``kria_editor_undo.seal`` replays its restore record over the
turn's own draft and gets back what the creator had. Each phone probe of the parity guard is
compiled and sealed over a clean editor, the way ``kria_runtime`` seals a committed turn.

Ways this can fail, each caught below:
* a new chat op lands and does not seal, so chat silently refuses to undo it while
  docs/reviews/kri-1384/chat-undo.md still says every group seals;
* an op that sealed stops sealing (the restore record lost a lane), so undo quietly disappears;
* an op listed as not sealing starts sealing, so the doc and this table understate undo;
* ``seal`` raises instead of answering None (it must never fail a chat turn).
"""

from __future__ import annotations

import pytest

from app.services import kria_editor_undo
from app.services.kria_editor_ops import (
    _clip_label_links,
    editor_payload_dump,
    merge_editor_draft,
)
from tests.evals.request_following.author_action_fixtures import (
    PHONE_OP_PROBES,
    SCENARIOS,
    Scenario,
    build,
)

# Turns chat refuses to undo, as "group.op [lanes the turn writes]": replaying the record does not
# give back exactly what the creator had (docs/reviews/kri-1384/chat-undo.md says why for each).
NOT_SEALED = {
    "text_authoring.set_title [title]",
    "clip_add_remove.remove_visual_media [visual_blocks]",
    "caption_animation.set_caption_meta [caption_meta]",
    "caption_style_scope.patch_caption_line [caption_meta]",
    "creator_song.set_user_song [user_song]",
    "layer_order.set_layer_order [visual_blocks]",
    "video_shape.set_video_shape [landscape_fit]",
    "video_shape.set_video_shape [orientation]",
}


# The parity guard's probes; an undo turn itself stores no record, so it is not sealed.
PROBES = [
    p for p in (*SCENARIOS, *PHONE_OP_PROBES) if p.reference[0]["op"] != kria_editor_undo.UNDO_OP
]


def _label(probe: Scenario) -> str:
    return f"{probe.group}.{probe.reference[0]['op']}"


def _sealed(probe: Scenario) -> tuple[str, bool] | None:
    built = build(probe)
    compiled = built["compiled"]
    if built["probe"].get("compiles") is not True or compiled is None:
        return None
    job, variant = built["job"], built["variant"]
    payload = editor_payload_dump(compiled.payload)
    lanes = ",".join(sorted(kria_editor_undo._edit_lanes(payload)))  # noqa: SLF001
    written = merge_editor_draft({}, payload)
    record = kria_editor_undo.chat_record(
        payload,
        compiled.before,
        turn_id="turn",
        base_job_id=str(job.id),
        timeline_rebase=compiled.timeline_rebase,
        clip_bar_ids=frozenset(_clip_label_links(job, variant)),
    )
    return f"{_label(probe)} [{lanes}]", (
        kria_editor_undo.seal(
            record,
            job=job,
            saved=variant,
            prior={},
            written=written,
            before=compiled.before,
            changes=compiled.changes,
        )
        is not None
    )


@pytest.mark.parametrize("probe", PROBES, ids=lambda p: f"{_label(p)}-{p.fixture_id}")
def test_a_phone_chat_edit_seals_unless_listed(probe: Scenario, prod_profile) -> None:
    verdict = _sealed(probe)
    if verdict is None:
        pytest.skip("the probe does not compile on a phone (the parity guard owns that)")
    turn, sealed = verdict
    if turn in NOT_SEALED:
        assert not sealed, f"{turn} now seals: drop it from NOT_SEALED and chat-undo.md"
    else:
        assert sealed, (
            f"{turn} does not seal, so chat refuses to undo it: fix its restore "
            "record or list it in NOT_SEALED and docs/reviews/kri-1384/chat-undo.md"
        )
