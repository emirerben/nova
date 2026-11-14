"""Guard: which phone chat edits "undo that" can put back exactly (KRI-1384).

A chat turn is undoable only when ``kria_editor_undo.seal`` replays its restore record over the
turn's own draft and gets back what the creator had. Each phone probe of the parity guard is
compiled and sealed over a clean editor, the way ``kria_runtime`` seals a committed turn.

Ways this can fail, each caught below:
* a new chat op lands and does not seal, so chat silently refuses to undo it while
  docs/reviews/kri-1384/chat-undo.md still says every group seals;
* an op that sealed stops sealing (the restore record lost a lane), so undo quietly disappears;
* an op listed as not sealing starts sealing, so the doc and this table understate undo;
* ``seal`` raises instead of answering None (it must never fail a chat turn);
* a restore that must take a key away (a caption line override, the all-lines size, the
  captions' animation default) keeps the chat's value, because a commit's None means "leave it";
* a lane read off the variant (picture blocks) lacks the schema defaults the replayed rows carry,
  so an exact restore compares unequal;
* a lane no plan-review section owns (``orientation``, ``landscape_fit``) is left out of the
  record, so the turn cannot be undone;
* a refusal that only holds for the probe's data (a video with no title yet, a picture row the
  commit schema rejects) hides that the same op seals on a saved video.
"""

from __future__ import annotations

import dataclasses
from typing import Any

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

# Turns chat refuses to undo, as "group.op [lanes the turn writes]", and why replaying the record
# does not give back exactly what the creator had (the same table is in
# docs/reviews/kri-1384/chat-undo.md).
NOT_SEALED = {
    "text_authoring.set_title [title]": (
        "the video has no title yet: the compiler records the old title only from `intro_text`, "
        "and a commit cannot clear a title"
    ),
    "clip_add_remove.remove_visual_media [visual_blocks]": (
        "the probe's picture row has no `asset_id` / `src_gcs_path`, so the commit schema "
        "rejects the pre-turn row"
    ),
    "creator_song.set_user_song [user_song]": (
        "the record holds the derived song state, not the creator's song section: the replay "
        "pins the song's end as the creator's (`creator_end_s`)"
    ),
}


def _titled(variant: dict[str, Any]) -> None:
    variant["intro_text"] = "Harbour run"


def _saved_rows(base: Any) -> Any:  # noqa: ANN401
    """``base``'s picture blocks as a Save stores them (the commit schema requires these)."""

    def apply(variant: dict[str, Any]) -> None:
        base(variant)
        for row in variant["visual_blocks"]:
            row["asset_id"] = row["id"]
            row["src_gcs_path"] = f"generative-jobs/job/sources/{row['id']}.jpg"
            row["display_mode"] = "overlay"

    return apply


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


def _probe(group: str, op: str) -> Scenario:
    return next(p for p in PROBES if p.group == group and p.reference[0]["op"] == op)


# The data-dependent refusals above, on the video a creator actually has.
ON_A_SAVED_VIDEO = {
    "a video with a title": dataclasses.replace(
        _probe("text_authoring", "set_title"), variant=_titled
    ),
    "picture rows as a Save stores them": dataclasses.replace(
        _probe("clip_add_remove", "remove_visual_media"),
        variant=_saved_rows(_probe("clip_add_remove", "remove_visual_media").variant),
    ),
}


@pytest.mark.parametrize("case", sorted(ON_A_SAVED_VIDEO))
def test_a_refusal_that_depends_on_the_probe_data_seals_on_a_saved_video(
    case: str, prod_profile
) -> None:
    verdict = _sealed(ON_A_SAVED_VIDEO[case])
    assert verdict is not None, f"{case}: the probe no longer compiles"
    turn, sealed = verdict
    assert turn in NOT_SEALED, f"{case}: {turn} seals on the probe data too; drop this case"
    assert sealed, f"{case}: {turn} does not seal even on a saved video ({NOT_SEALED[turn]})"
