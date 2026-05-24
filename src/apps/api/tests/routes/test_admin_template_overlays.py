"""Integration tests for PATCH /admin/templates/{id}/overlays.

The endpoint is the escape hatch when the Layer-2 cumulative-reveal
pipeline produces wrong text. Admin sends a list of edits, each pointing
at a specific overlay in `recipe_cached.slots[].text_overlays[]`. The
endpoint validates the entire list against the recipe, then applies all
edits atomically.

Coverage:
  - Happy path: single + multi-edit, sample_text + text both updated
  - Empty sample_text allowed (hides the overlay)
  - Validation: out-of-range slot, out-of-range overlay, no slots,
    missing recipe_cached, empty edit list
  - Auth: rejects bad token, rejects missing token
"""

from collections.abc import AsyncGenerator
from datetime import UTC, datetime
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi.testclient import TestClient

from app.database import get_db
from app.main import app
from app.models import VideoTemplate

ADMIN_TOKEN = "test-admin-token"


@pytest.fixture(autouse=True)
def _patch_admin_key(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("ADMIN_API_KEY", ADMIN_TOKEN)
    from app.config import settings

    settings.admin_api_key = ADMIN_TOKEN


class _StubSession:
    """Minimal AsyncSession stand-in. The endpoint awaits commit/refresh
    and calls execute() once for the agent_run history query at the end.
    Every method is a no-op; the test verifies the route's mutation logic
    by reading recipe_cached on the (mock) template AFTER the endpoint
    runs, since the endpoint writes back to `template.recipe_cached`
    in place before commit.
    """

    async def commit(self) -> None:
        return None

    async def refresh(self, _obj) -> None:
        return None

    async def execute(self, _stmt):
        return _StubResult()


class _StubResult:
    def scalars(self):
        return self

    def all(self):
        return []


async def _stub_get_db() -> AsyncGenerator[_StubSession, None]:
    """FastAPI expects an async generator from get_db. A plain async fn
    or a sync lambda returning a session does NOT satisfy the contract —
    that's how an earlier iteration ended up calling refresh on a real
    sqlalchemy session and tripping UnmappedInstanceError on the
    MagicMock template.
    """
    yield _StubSession()


@pytest.fixture()
def client() -> TestClient:
    app.dependency_overrides[get_db] = _stub_get_db
    try:
        yield TestClient(app, raise_server_exceptions=False)
    finally:
        app.dependency_overrides.pop(get_db, None)


def _headers() -> dict:
    return {"X-Admin-Token": ADMIN_TOKEN, "Content-Type": "application/json"}


def _bad_token_headers() -> dict:
    return {"X-Admin-Token": "wrong-token", "Content-Type": "application/json"}


def _template_with_overlays() -> VideoTemplate:
    """Build a template whose recipe_cached has 2 slots, each with 2
    overlays. Slot 1's overlays carry both `sample_text` and `text` so
    the test can verify the legacy-field-sync behavior; slot 2's
    overlays carry only `sample_text` so the test can verify the route
    doesn't introduce a spurious `text` key when none existed.
    """
    t = MagicMock(spec=VideoTemplate)
    t.id = "tpl-overlay-001"
    t.name = "Overlay Test"
    t.gcs_path = "templates/test/video.mp4"
    t.template_type = "standard"
    t.is_agentic = True
    t.analysis_status = "ready"
    t.audio_gcs_path = None
    t.music_track_id = None
    t.error_detail = None
    t.recipe_cached_at = datetime.now(UTC)
    t.created_at = datetime.now(UTC)
    t.recipe_cached = {
        "slots": [
            {
                "target_duration_s": 2.0,
                "text_overlays": [
                    {"sample_text": "It's", "text": "It's", "start_s": 0.0, "end_s": 0.5},
                    {"sample_text": "not", "text": "not", "start_s": 0.5, "end_s": 1.0},
                ],
            },
            {
                "target_duration_s": 2.0,
                "text_overlays": [
                    {"sample_text": 'luck"', "start_s": 0.0, "end_s": 0.5},
                    {"sample_text": "W", "start_s": 0.5, "end_s": 1.0},
                ],
            },
        ],
    }
    return t


def _patch_get_template(template):
    return patch("app.routes.admin.get_template_or_404", new=AsyncMock(return_value=template))


# ── Auth ─────────────────────────────────────────────────────────────────────


def test_missing_admin_token_is_rejected(client: TestClient) -> None:
    """No X-Admin-Token header → FastAPI returns 422 (Header(...) required)."""
    res = client.patch(
        "/admin/templates/tpl-overlay-001/overlays",
        json={"edits": [{"slot_index": 0, "overlay_index": 0, "sample_text": "hello"}]},
    )
    assert res.status_code in (401, 422), res.text


def test_bad_admin_token_is_rejected(client: TestClient) -> None:
    """Wrong token value → 401."""
    res = client.patch(
        "/admin/templates/tpl-overlay-001/overlays",
        headers=_bad_token_headers(),
        json={"edits": [{"slot_index": 0, "overlay_index": 0, "sample_text": "hello"}]},
    )
    assert res.status_code == 401, res.text


# ── Happy path ───────────────────────────────────────────────────────────────


def test_single_edit_updates_sample_text_and_text(client: TestClient) -> None:
    """A single edit rewrites both `sample_text` AND the legacy `text`
    field (when the overlay carries one). The renderer's fallback at
    template_orchestrate._resolve_overlay_text reads `text` as a
    fallback for `sample_text`, so the two MUST stay in sync after edit.
    """
    template = _template_with_overlays()
    with _patch_get_template(template):
        res = client.patch(
            "/admin/templates/tpl-overlay-001/overlays",
            headers=_headers(),
            json={
                "edits": [
                    {
                        "slot_index": 0,
                        "overlay_index": 0,
                        "sample_text": "It's not just luck",
                    }
                ]
            },
        )
    assert res.status_code == 200, res.text
    overlay = res.json()["recipe_cached"]["slots"][0]["text_overlays"][0]
    assert overlay["sample_text"] == "It's not just luck"
    assert overlay["text"] == "It's not just luck"
    # Untouched neighbor still carries its original text.
    assert res.json()["recipe_cached"]["slots"][0]["text_overlays"][1]["sample_text"] == "not"


def test_edit_does_not_introduce_text_key_when_absent(client: TestClient) -> None:
    """Slot 1's overlays have only `sample_text`. Editing must NOT add a
    new `text` key — that would pollute the JSONB shape and downstream
    consumers expecting the canonical sample_text-only shape would see
    a phantom field.
    """
    template = _template_with_overlays()
    with _patch_get_template(template):
        res = client.patch(
            "/admin/templates/tpl-overlay-001/overlays",
            headers=_headers(),
            json={"edits": [{"slot_index": 1, "overlay_index": 0, "sample_text": "luck"}]},
        )
    assert res.status_code == 200, res.text
    overlay = res.json()["recipe_cached"]["slots"][1]["text_overlays"][0]
    assert overlay["sample_text"] == "luck"
    assert "text" not in overlay, f"unexpected legacy `text` key added: {overlay!r}"


def test_multiple_edits_apply_atomically(client: TestClient) -> None:
    """A 3-edit request rewrites three different overlays in one
    transaction. Validates the JSONB change detection works for nested
    list mutations (the route copies dicts at each nesting level so
    SQLAlchemy sees a new object reference).
    """
    template = _template_with_overlays()
    with _patch_get_template(template):
        res = client.patch(
            "/admin/templates/tpl-overlay-001/overlays",
            headers=_headers(),
            json={
                "edits": [
                    {"slot_index": 0, "overlay_index": 0, "sample_text": "It's"},
                    {"slot_index": 0, "overlay_index": 1, "sample_text": "It's not"},
                    {
                        "slot_index": 1,
                        "overlay_index": 0,
                        "sample_text": "It's not just luck",
                    },
                ]
            },
        )
    assert res.status_code == 200, res.text
    slots = res.json()["recipe_cached"]["slots"]
    assert slots[0]["text_overlays"][0]["sample_text"] == "It's"
    assert slots[0]["text_overlays"][1]["sample_text"] == "It's not"
    assert slots[1]["text_overlays"][0]["sample_text"] == "It's not just luck"


def test_empty_sample_text_is_allowed(client: TestClient) -> None:
    """Empty `sample_text` is a legitimate edit — effectively hides the
    overlay because the renderer skips empty strings. The schema
    validator must not reject the empty string at parse time.
    """
    template = _template_with_overlays()
    with _patch_get_template(template):
        res = client.patch(
            "/admin/templates/tpl-overlay-001/overlays",
            headers=_headers(),
            json={"edits": [{"slot_index": 1, "overlay_index": 1, "sample_text": ""}]},
        )
    assert res.status_code == 200, res.text
    assert res.json()["recipe_cached"]["slots"][1]["text_overlays"][1]["sample_text"] == ""


# ── Validation (422 / 409) ───────────────────────────────────────────────────


def test_out_of_range_slot_index_rejected(client: TestClient) -> None:
    template = _template_with_overlays()
    with _patch_get_template(template):
        res = client.patch(
            "/admin/templates/tpl-overlay-001/overlays",
            headers=_headers(),
            json={"edits": [{"slot_index": 99, "overlay_index": 0, "sample_text": "x"}]},
        )
    assert res.status_code == 422
    assert "slot_index=99 out of range" in res.text


def test_out_of_range_overlay_index_rejected(client: TestClient) -> None:
    template = _template_with_overlays()
    with _patch_get_template(template):
        res = client.patch(
            "/admin/templates/tpl-overlay-001/overlays",
            headers=_headers(),
            json={"edits": [{"slot_index": 0, "overlay_index": 99, "sample_text": "x"}]},
        )
    assert res.status_code == 422
    assert "overlay_index=99 out of range" in res.text


def test_validation_failure_on_one_edit_leaves_recipe_untouched(client: TestClient) -> None:
    """Atomicity: when ANY edit fails validation, NO edits apply. The
    pre-validation pass collects all mutations first, then commits — a
    bad edit at index 2 must not let edits 0-1 land in the DB.
    """
    template = _template_with_overlays()
    original_first = template.recipe_cached["slots"][0]["text_overlays"][0]["sample_text"]
    original_second = template.recipe_cached["slots"][0]["text_overlays"][1]["sample_text"]
    with _patch_get_template(template):
        res = client.patch(
            "/admin/templates/tpl-overlay-001/overlays",
            headers=_headers(),
            json={
                "edits": [
                    {"slot_index": 0, "overlay_index": 0, "sample_text": "FIRST"},
                    {"slot_index": 0, "overlay_index": 1, "sample_text": "SECOND"},
                    {"slot_index": 99, "overlay_index": 0, "sample_text": "OOPS"},
                ]
            },
        )
    assert res.status_code == 422
    # template.recipe_cached on the mock was never reassigned because the
    # route raises BEFORE the mutation loop runs.
    assert template.recipe_cached["slots"][0]["text_overlays"][0]["sample_text"] == original_first
    assert template.recipe_cached["slots"][0]["text_overlays"][1]["sample_text"] == original_second


def test_recipe_with_no_slots_rejected(client: TestClient) -> None:
    """Editing a template whose recipe has no slots returns 409, not a
    silent no-op. This catches the "tried to edit an unanalyzed template"
    case where the admin UI shouldn't have offered the editor anyway."""
    template = _template_with_overlays()
    template.recipe_cached = {"slots": []}
    with _patch_get_template(template):
        res = client.patch(
            "/admin/templates/tpl-overlay-001/overlays",
            headers=_headers(),
            json={"edits": [{"slot_index": 0, "overlay_index": 0, "sample_text": "x"}]},
        )
    assert res.status_code == 409
    assert "no slots" in res.text


def test_missing_recipe_cached_rejected(client: TestClient) -> None:
    template = _template_with_overlays()
    template.recipe_cached = None
    with _patch_get_template(template):
        res = client.patch(
            "/admin/templates/tpl-overlay-001/overlays",
            headers=_headers(),
            json={"edits": [{"slot_index": 0, "overlay_index": 0, "sample_text": "x"}]},
        )
    assert res.status_code == 409
    assert "no recipe" in res.text


def test_empty_edit_list_rejected_by_validator(client: TestClient) -> None:
    """Zero-edit requests are a schema violation (min_length=1) — return
    422 before the route runs. Avoids spurious commits on accidental
    empty requests from the admin UI.
    """
    template = _template_with_overlays()
    with _patch_get_template(template):
        res = client.patch(
            "/admin/templates/tpl-overlay-001/overlays",
            headers=_headers(),
            json={"edits": []},
        )
    assert res.status_code == 422


# ── POST /admin/templates/{id}/retime-phrase ─────────────────────────────────
#
# Unlike the text-only PATCH, retime-phrase re-derives the stage COUNT (= word
# count) and per-word timings from a fixed beat, so editing a phrase's wording
# reflows the reveal.


def test_retime_phrase_expands_stages_and_recomputes_timing(client: TestClient) -> None:
    """Slot 0's 2-member cumulative phrase ("It's"/"not") retimed to a 4-word
    line becomes 4 stages with per-word beat timing from the anchor's start."""
    template = _template_with_overlays()
    with _patch_get_template(template):
        res = client.post(
            "/admin/templates/tpl-overlay-001/retime-phrase",
            headers=_headers(),
            json={
                "slot_index": 0,
                "member_overlay_indices": [0, 1],
                "new_text": "It's not just luck",
                "beat_s": 0.4,
            },
        )
    assert res.status_code == 200, res.text
    overlays = template.recipe_cached["slots"][0]["text_overlays"]
    assert [o["sample_text"] for o in overlays] == [
        "It's",
        "It's not",
        "It's not just",
        "It's not just luck",
    ]
    # Anchor start (0.0) preserved; each word +0.4s; last holds +0.4 dwell.
    starts = [round(o["start_s"], 2) for o in overlays]
    assert starts == [0.0, 0.4, 0.8, 1.2]
    assert round(overlays[-1]["end_s"], 2) == round(0.0 + 4 * 0.4 + 0.4, 2)  # 2.0
    # Stages butt edge-to-edge.
    for a, b in zip(overlays, overlays[1:]):
        assert round(a["end_s"], 3) == round(b["start_s"], 3)
    # Per-stage pop suffix is the newly-revealed word.
    assert [o["pop_animated_suffix"] for o in overlays] == ["It's", "not", "just", "luck"]
    # legacy `text` kept in sync (anchor had it).
    assert overlays[0]["text"] == "It's"


def test_retime_phrase_enforces_legibility_floor(client: TestClient) -> None:
    """Retiming with a beat so small the words would flash by faster than the
    eye can read is corrected: each reveal stage is expanded to the per-word
    legibility floor (retime never compresses neighbours, so it only spreads)."""
    from app.pipeline.overlay_pacing import MIN_PER_WORD_S

    template = _template_with_overlays()
    with _patch_get_template(template):
        res = client.post(
            "/admin/templates/tpl-overlay-001/retime-phrase",
            headers=_headers(),
            json={
                "slot_index": 0,
                "member_overlay_indices": [0, 1],
                "new_text": "It's not just luck",
                "beat_s": 0.06,  # below the readable floor
            },
        )
    assert res.status_code == 200, res.text
    overlays = template.recipe_cached["slots"][0]["text_overlays"]
    assert [o["sample_text"] for o in overlays][:4] == [
        "It's",
        "It's not",
        "It's not just",
        "It's not just luck",
    ]
    for o in overlays[:4]:
        assert o["end_s"] - o["start_s"] >= MIN_PER_WORD_S - 1e-6


def test_retime_phrase_shrink_reduces_stage_count(client: TestClient) -> None:
    """Editing the 2-member phrase down to one word yields a single stage."""
    template = _template_with_overlays()
    with _patch_get_template(template):
        res = client.post(
            "/admin/templates/tpl-overlay-001/retime-phrase",
            headers=_headers(),
            json={"slot_index": 0, "member_overlay_indices": [0, 1], "new_text": "Hello"},
        )
    assert res.status_code == 200, res.text
    overlays = template.recipe_cached["slots"][0]["text_overlays"]
    assert [o["sample_text"] for o in overlays] == ["Hello"]


def test_retime_phrase_empty_text_deletes_phrase(client: TestClient) -> None:
    """Empty new_text removes the phrase's overlays from the slot."""
    template = _template_with_overlays()
    with _patch_get_template(template):
        res = client.post(
            "/admin/templates/tpl-overlay-001/retime-phrase",
            headers=_headers(),
            json={"slot_index": 0, "member_overlay_indices": [0, 1], "new_text": "   "},
        )
    assert res.status_code == 200, res.text
    assert template.recipe_cached["slots"][0]["text_overlays"] == []


def test_retime_phrase_rejects_non_contiguous_indices(client: TestClient) -> None:
    template = _template_with_overlays()
    with _patch_get_template(template):
        res = client.post(
            "/admin/templates/tpl-overlay-001/retime-phrase",
            headers=_headers(),
            json={"slot_index": 0, "member_overlay_indices": [0, 2], "new_text": "a b"},
        )
    assert res.status_code == 400, res.text


def test_retime_phrase_rejects_out_of_range(client: TestClient) -> None:
    template = _template_with_overlays()
    with _patch_get_template(template):
        res = client.post(
            "/admin/templates/tpl-overlay-001/retime-phrase",
            headers=_headers(),
            json={"slot_index": 0, "member_overlay_indices": [5], "new_text": "x"},
        )
    assert res.status_code == 400, res.text


def test_retime_phrase_singleton_stays_one_overlay(client: TestClient) -> None:
    """A singleton phrase edited to multiple words stays ONE static overlay
    (duration recomputed) — not exploded into per-word reveal stages, and with
    no pop_animated_suffix."""
    template = _template_with_overlays()
    with _patch_get_template(template):
        res = client.post(
            "/admin/templates/tpl-overlay-001/retime-phrase",
            headers=_headers(),
            json={
                "slot_index": 1,
                "member_overlay_indices": [0],
                "new_text": "the whole line at once",
                "pattern": "singleton",
            },
        )
    assert res.status_code == 200, res.text
    overlays = template.recipe_cached["slots"][1]["text_overlays"]
    # Original overlay #1 still present → exactly 2 overlays, not 5.
    assert len(overlays) == 2
    edited = overlays[0]
    assert edited["sample_text"] == "the whole line at once"
    assert "pop_animated_suffix" not in edited
    assert edited["start_s"] == 0.0
    # Duration is word-count driven (5 words × 0.4 beat + 0.4 dwell = 2.4),
    # NOT clamped against the neighbouring overlay.
    assert round(edited["end_s"], 2) == 2.4
    assert edited["end_s"] > edited["start_s"]


def test_retime_phrase_sequences_across_positions(client: TestClient) -> None:
    """Growing a phrase lays its reveal end-to-end at the beat from the anchor;
    then the slot is re-sequenced slot-wide so a phrase at a DIFFERENT on-screen
    position is rippled to play after it — one phrase on screen at a time."""
    t = _template_with_overlays()
    # Neighbour at a distinct y_frac. Slot-wide sequencing ripples it regardless.
    t.recipe_cached = {
        "slots": [
            {
                "target_duration_s": 10.0,
                "text_overlays": [
                    {"sample_text": "anchor", "start_s": 0.0, "end_s": 0.5},
                    {
                        "sample_text": "layered",
                        "start_s": 0.5,
                        "end_s": 1.0,
                        "position_y_frac": 0.85,
                    },
                ],
            }
        ]
    }
    with _patch_get_template(t):
        res = client.post(
            "/admin/templates/tpl-overlay-001/retime-phrase",
            headers=_headers(),
            json={
                "slot_index": 0,
                "member_overlay_indices": [0],
                "new_text": "one two three four five six",
                "beat_s": 0.4,
            },
        )
    assert res.status_code == 200, res.text
    overlays = t.recipe_cached["slots"][0]["text_overlays"]
    # 6 reveal stages + the rippled "layered" overlay (last).
    stages = overlays[:-1]
    assert [o["sample_text"] for o in stages] == [
        "one",
        "one two",
        "one two three",
        "one two three four",
        "one two three four five",
        "one two three four five six",
    ]
    # Strictly monotonic, edge-to-edge, each stage a positive window.
    for a, b in zip(stages, stages[1:]):
        assert a["start_s"] < a["end_s"]
        assert round(a["end_s"], 3) == round(b["start_s"], 3)
    assert round(stages[-1]["end_s"], 2) == round(6 * 0.4 + 0.4, 2)  # 2.8
    # The different-position neighbour is rippled to start where the phrase ends
    # (slot-wide, one-line sequencing), its 0.5s window preserved.
    layered = overlays[-1]
    assert layered["sample_text"] == "layered"
    assert round(layered["start_s"], 2) == 2.8
    assert round(layered["end_s"], 2) == 3.3


def test_retime_phrase_never_rejects_for_tight_neighbour(client: TestClient) -> None:
    """A phrase whose anchor sits right next to another overlay still saves —
    editing recomputes timing from word count and never blocks."""
    t = _template_with_overlays()
    t.recipe_cached = {
        "slots": [
            {
                "target_duration_s": 1.0,
                "text_overlays": [
                    {"sample_text": "a", "start_s": 0.5, "end_s": 0.6},
                    {"sample_text": "b", "start_s": 0.5, "end_s": 0.9},
                ],
            }
        ]
    }
    with _patch_get_template(t):
        res = client.post(
            "/admin/templates/tpl-overlay-001/retime-phrase",
            headers=_headers(),
            json={"slot_index": 0, "member_overlay_indices": [0], "new_text": "now this works"},
        )
    assert res.status_code == 200, res.text
    overlays = t.recipe_cached["slots"][0]["text_overlays"]
    stages = overlays[:-1]
    assert [o["sample_text"] for o in stages] == ["now", "now this", "now this works"]
    assert stages[0]["start_s"] == 0.5  # anchor start preserved
    for s in stages:
        assert s["end_s"] > s["start_s"]


# ── Slot reflow: ripple following overlays, never overlap ─────────────────────
#
# Editing a phrase must never leave two overlays in the same screen slot
# overlapping in time. The reflow ripples FOLLOWING overlays later (never moves
# them earlier, never compresses the edited phrase, never rejects the edit).


def _template_with_sequential_phrases() -> VideoTemplate:
    """One slot, all overlays at the same (default) screen position: phrase A
    (2 cumulative members, [0,0.8]) followed by phrase B (2 members, [0.8,1.6]).
    Editing phrase A so it grows must ripple phrase B later as a block."""
    t = _template_with_overlays()
    t.recipe_cached = {
        "slots": [
            {
                "target_duration_s": 4.0,
                "text_overlays": [
                    {"sample_text": "a", "start_s": 0.0, "end_s": 0.4},
                    {"sample_text": "a b", "start_s": 0.4, "end_s": 0.8},
                    {"sample_text": "X", "start_s": 0.8, "end_s": 1.2},
                    {"sample_text": "X Y", "start_s": 1.2, "end_s": 1.6},
                ],
            }
        ]
    }
    return t


def test_reflow_pushes_following_overlay_when_edit_grows(client: TestClient) -> None:
    """Growing phrase A (2→3 stages, ending at 1.2) ripples phrase B so it starts
    exactly where A ends, with B's own duration preserved."""
    t = _template_with_sequential_phrases()
    with _patch_get_template(t):
        res = client.post(
            "/admin/templates/tpl-overlay-001/retime-phrase",
            headers=_headers(),
            json={
                "slot_index": 0,
                "member_overlay_indices": [0, 1],
                "new_text": "a b c",
                "beat_s": 0.4,
            },
        )
    assert res.status_code == 200, res.text
    overlays = t.recipe_cached["slots"][0]["text_overlays"]
    samples = [o["sample_text"] for o in overlays]
    assert samples == ["a", "a b", "a b c", "X", "X Y"]
    # Phrase A keeps its full per-word timing (not compressed). The last stage
    # holds the +0.4s dwell, so "a b c" ends at 0.8 + 0.4 + 0.4 = 1.6.
    a_stages = overlays[:3]
    assert [round(o["start_s"], 2) for o in a_stages] == [0.0, 0.4, 0.8]
    assert round(a_stages[-1]["end_s"], 2) == 1.6
    # Phrase B rippled to butt against A's end; B's 0.4s windows preserved.
    b = overlays[3:]
    assert round(b[0]["start_s"], 2) == 1.6
    assert round(b[0]["end_s"], 2) == 2.0
    assert round(b[1]["start_s"], 2) == 2.0
    assert round(b[1]["end_s"], 2) == 2.4
    # No overlap anywhere in the slot.
    for x, y in zip(overlays, overlays[1:]):
        assert y["start_s"] >= x["end_s"] - 1e-6


def test_reflow_no_shift_when_edit_shrinks(client: TestClient) -> None:
    """Shrinking phrase A frees space — following overlays are never moved
    earlier (ripple is forward-only)."""
    t = _template_with_sequential_phrases()
    with _patch_get_template(t):
        res = client.post(
            "/admin/templates/tpl-overlay-001/retime-phrase",
            headers=_headers(),
            json={
                "slot_index": 0,
                "member_overlay_indices": [0, 1],
                "new_text": "a",
                "beat_s": 0.4,
            },
        )
    assert res.status_code == 200, res.text
    overlays = t.recipe_cached["slots"][0]["text_overlays"]
    assert [o["sample_text"] for o in overlays] == ["a", "X", "X Y"]
    # X and X Y stay put (0.8 / 1.2) — not pulled earlier into the freed space.
    assert round(overlays[1]["start_s"], 2) == 0.8
    assert round(overlays[2]["start_s"], 2) == 1.2


def test_reflow_recompute_strips_anchor_overrides(client: TestClient) -> None:
    """When the anchor carries a timing override, recomputed members must NOT
    inherit it (overrides would win over the freshly computed beat timing)."""
    t = _template_with_overlays()
    t.recipe_cached = {
        "slots": [
            {
                "target_duration_s": 3.0,
                "text_overlays": [
                    {
                        "sample_text": "x",
                        "start_s": 0.2,
                        "end_s": 0.6,
                        "start_s_override": 0.2,
                        "end_s_override": 0.6,
                    }
                ],
            }
        ]
    }
    with _patch_get_template(t):
        res = client.post(
            "/admin/templates/tpl-overlay-001/retime-phrase",
            headers=_headers(),
            json={"slot_index": 0, "member_overlay_indices": [0], "new_text": "one two three"},
        )
    assert res.status_code == 200, res.text
    overlays = t.recipe_cached["slots"][0]["text_overlays"]
    assert len(overlays) == 3
    for o in overlays:
        assert "start_s_override" not in o
        assert "end_s_override" not in o


def test_reflow_overflow_warning_surfaced(client: TestClient) -> None:
    """When ripple pushes an overlay's start past the slot's target duration, the
    response carries a non-blocking warning and the edit still succeeds (200)."""
    t = _template_with_overlays()
    t.recipe_cached = {
        "slots": [
            {
                "target_duration_s": 2.0,
                "text_overlays": [
                    {"sample_text": "a", "start_s": 0.0, "end_s": 0.4},
                    {"sample_text": "tail", "start_s": 0.4, "end_s": 0.8},
                ],
            }
        ]
    }
    with _patch_get_template(t):
        res = client.post(
            "/admin/templates/tpl-overlay-001/retime-phrase",
            headers=_headers(),
            json={
                "slot_index": 0,
                "member_overlay_indices": [0],
                "new_text": "one two three four five six",
                "beat_s": 0.4,
            },
        )
    assert res.status_code == 200, res.text
    body = res.json()
    assert body["reflow_warning"] is not None
    assert body["reflow_warning"]["overlays_pushed_past_target"] >= 1


def test_reflow_warning_absent_when_nothing_overflows(client: TestClient) -> None:
    """No overflow → reflow_warning is null (default)."""
    t = _template_with_sequential_phrases()
    with _patch_get_template(t):
        res = client.post(
            "/admin/templates/tpl-overlay-001/retime-phrase",
            headers=_headers(),
            json={"slot_index": 0, "member_overlay_indices": [0, 1], "new_text": "a b c"},
        )
    assert res.status_code == 200, res.text
    assert res.json()["reflow_warning"] is None


# ── Direct unit tests on the pure reflow helper ──────────────────────────────


def test_unit_reflow_strict_overlap_only_adjacency_ok() -> None:
    from app.pipeline.overlay_pacing import _resequence_slot_overlays

    # Butted (start == prev_end) is adjacency, not overlap → no shift.
    ovs = [
        {"sample_text": "a", "start_s": 0.0, "end_s": 0.4},
        {"sample_text": "b", "start_s": 0.4, "end_s": 0.8},
    ]
    out, w = _resequence_slot_overlays(ovs, target_duration_s=2.0)
    assert [o["start_s"] for o in out] == [0.0, 0.4]
    assert w["overlays_pushed_past_target"] == 0


def test_unit_resequence_ripples_across_positions() -> None:
    from app.pipeline.overlay_pacing import _resequence_slot_overlays

    # Slot-wide / position-agnostic: a phrase at a different y_frac still gets
    # sequenced (one phrase on screen at a time), not left overlapping.
    ovs = [
        {"sample_text": "big", "start_s": 0.0, "end_s": 2.8},
        {"sample_text": "other", "start_s": 0.5, "end_s": 1.0, "position_y_frac": 0.85},
    ]
    out, _ = _resequence_slot_overlays(ovs, target_duration_s=4.0)
    # "other" rippled to start where "big" ends — no overlap across positions.
    assert round(out[1]["start_s"], 2) == 2.8
    assert round(out[1]["end_s"], 2) == 3.3


def test_unit_resequence_closes_intra_phrase_gaps() -> None:
    from app.pipeline.overlay_pacing import _resequence_slot_overlays

    # One cumulative phrase block with an intra-phrase gap (same anchor). The
    # block ripple leaves internal gaps; the trailing butt-join must close them
    # so the reveal never blanks between words. Removing the
    # butt_join_cumulative_phrases(out) call makes this assertion fail.
    ovs = [
        {"sample_text": "a", "start_s": 0.0, "end_s": 0.4},
        {"sample_text": "a b", "start_s": 0.8, "end_s": 1.2},  # 0.4s gap before
        {"sample_text": "a b c", "start_s": 1.6, "end_s": 2.0},  # 0.4s gap before
    ]
    out, _ = _resequence_slot_overlays(ovs, target_duration_s=4.0)
    assert out[0]["end_s"] == pytest.approx(out[1]["start_s"])
    assert out[1]["end_s"] == pytest.approx(out[2]["start_s"])
    assert out[2]["end_s"] == pytest.approx(2.0)  # terminal dwell untouched


def test_unit_reflow_skips_agentic_pct_overlays() -> None:
    from app.pipeline.overlay_pacing import _resequence_slot_overlays

    ovs = [
        {"sample_text": "p", "start_s": 0.0, "end_s": 5.0, "start_pct": 0.0, "end_pct": 0.5},
        {"sample_text": "q", "start_s": 0.1, "end_s": 0.2, "start_pct": 0.5, "end_pct": 0.6},
    ]
    out, _ = _resequence_slot_overlays(ovs, target_duration_s=None)
    # pct-timed overlays are skipped (seconds-shift is a render no-op).
    assert out[0]["start_s"] == 0.0
    assert out[1]["start_s"] == 0.1


def test_unit_reflow_shifts_overrides_with_base() -> None:
    from app.pipeline.overlay_pacing import _resequence_slot_overlays

    # Follower's effective window is driven by its override; both base and
    # override must shift by the same delta when rippled.
    ovs = [
        {"sample_text": "a", "start_s": 0.0, "end_s": 1.0},
        {
            "sample_text": "b",
            "start_s": 0.5,
            "end_s": 0.9,
            "start_s_override": 0.5,
            "end_s_override": 0.9,
        },
    ]
    out, _ = _resequence_slot_overlays(ovs, target_duration_s=2.0)
    b = out[1]
    # Overlap was 1.0 - 0.5 = 0.5 → shift by 0.5.
    assert round(b["start_s_override"], 2) == 1.0
    assert round(b["end_s_override"], 2) == 1.4
    assert round(b["start_s"], 2) == 1.0
    assert round(b["end_s"], 2) == 1.4


def test_unit_reflow_accel_stays_in_window() -> None:
    from app.pipeline.overlay_pacing import _eff_end, _eff_start, _resequence_slot_overlays

    ovs = [
        {"sample_text": "a", "start_s": 0.0, "end_s": 1.0},
        {"sample_text": "b", "start_s": 0.5, "end_s": 1.5, "font_cycle_accel_at_s": 0.6},
    ]
    out, _ = _resequence_slot_overlays(ovs, target_duration_s=3.0)
    b = out[1]
    accel = b["font_cycle_accel_at_s"]
    assert _eff_start(b) <= accel < _eff_end(b)


def test_unit_reflow_cascades_multiple_followers_preserving_gaps() -> None:
    from app.pipeline.overlay_pacing import _resequence_slot_overlays

    # Edited phrase [0,1.2]; two followers each 0.4s. Both ripple, gaps preserved.
    ovs = [
        {"sample_text": "p", "start_s": 0.0, "end_s": 1.2},
        {"sample_text": "q", "start_s": 0.8, "end_s": 1.2},
        {"sample_text": "r", "start_s": 1.2, "end_s": 1.6},
    ]
    out, _ = _resequence_slot_overlays(ovs, target_duration_s=4.0)
    assert round(out[1]["start_s"], 2) == 1.2
    assert round(out[1]["end_s"], 2) == 1.6
    assert round(out[2]["start_s"], 2) == 1.6
    assert round(out[2]["end_s"], 2) == 2.0


def test_unit_reflow_single_overlay_noop() -> None:
    from app.pipeline.overlay_pacing import _resequence_slot_overlays

    ovs = [{"sample_text": "solo", "start_s": 0.0, "end_s": 1.0}]
    out, w = _resequence_slot_overlays(ovs, target_duration_s=2.0)
    assert out[0]["start_s"] == 0.0
    assert out[0]["end_s"] == 1.0
    assert w["overlays_pushed_past_target"] == 0


# ── Phrase grouping + slot-wide re-sequencing ────────────────────────────────


def test_unit_group_phrase_index_blocks_cumulative_and_singletons() -> None:
    from app.pipeline.text_reveal import group_phrase_index_blocks

    ovs = [
        {"sample_text": "a"},
        {"sample_text": "a b"},
        {"sample_text": "a b c"},
        {"sample_text": "X"},
        {"sample_text": "X Y"},
        {"sample_text": "solo"},
    ]
    assert group_phrase_index_blocks(ovs) == [[0, 1, 2], [3, 4], [5]]


def test_unit_resequence_separates_interleaved_phrases_as_blocks() -> None:
    from app.pipeline.overlay_pacing import _eff_end, _eff_start, _resequence_slot_overlays

    # Two phrases whose reveal stages are interleaved in time (the real prod bug).
    # Re-sequencing must move each WHOLE phrase, not fragment them.
    ovs = [
        {"sample_text": "if", "start_s": 1.0, "end_s": 1.4},
        {"sample_text": "if you", "start_s": 1.4, "end_s": 2.0},
        {"sample_text": "Luck", "start_s": 1.6, "end_s": 2.2},  # interleaved
        {"sample_text": "Luck is", "start_s": 2.2, "end_s": 2.8},
    ]
    out, _ = _resequence_slot_overlays(ovs, target_duration_s=10.0)
    # Phrase 1 ("if you") keeps its window; phrase 2 ("Luck is") rippled to start
    # at phrase 1's end (2.0), internal pacing intact — no interleaving, no frag.
    assert round(_eff_start(out[0]), 2) == 1.0
    assert round(_eff_end(out[1]), 2) == 2.0
    assert round(_eff_start(out[2]), 2) == 2.0  # "Luck" pushed to after "if you"
    assert round(_eff_end(out[3]), 2) == 3.2
    # Strictly no overlap across the whole slot.
    for x, y in zip(out, out[1:]):
        assert _eff_start(y) >= _eff_end(x) - 1e-6


# ── POST /admin/templates/{id}/resequence-slots ("Fix timings") ──────────────


def _template_with_overlapping_phrases() -> VideoTemplate:
    """One slot, two cumulative phrases that overlap in time (and would also
    interleave) — the shape the 'Fix timings' button exists to clean up."""
    t = _template_with_overlays()
    t.recipe_cached = {
        "slots": [
            {
                "target_duration_s": 10.0,
                "text_overlays": [
                    {"sample_text": "one", "start_s": 0.0, "end_s": 1.0},
                    {"sample_text": "one two", "start_s": 1.0, "end_s": 2.0},
                    {"sample_text": "later", "start_s": 1.5, "end_s": 2.5},
                    {"sample_text": "later phrase", "start_s": 2.5, "end_s": 3.5},
                ],
            }
        ]
    }
    return t


def test_resequence_slots_removes_overlap_without_changing_text(client: TestClient) -> None:
    t = _template_with_overlapping_phrases()
    original_texts = [o["sample_text"] for o in t.recipe_cached["slots"][0]["text_overlays"]]
    with _patch_get_template(t):
        res = client.post(
            "/admin/templates/tpl-overlay-001/resequence-slots",
            headers=_headers(),
            json={},
        )
    assert res.status_code == 200, res.text
    overlays = t.recipe_cached["slots"][0]["text_overlays"]
    # Text untouched.
    assert [o["sample_text"] for o in overlays] == original_texts
    # Second phrase rippled to start at the first phrase's end (2.0); no overlap.
    assert round(overlays[2]["start_s"], 2) == 2.0
    for x, y in zip(overlays, overlays[1:]):
        assert y["start_s"] >= x["end_s"] - 1e-6


def test_resequence_slots_single_slot_index(client: TestClient) -> None:
    t = _template_with_overlays()  # 2 slots
    with _patch_get_template(t):
        res = client.post(
            "/admin/templates/tpl-overlay-001/resequence-slots",
            headers=_headers(),
            json={"slot_index": 0},
        )
    assert res.status_code == 200, res.text


def test_resequence_slots_out_of_range_rejected(client: TestClient) -> None:
    t = _template_with_overlays()
    with _patch_get_template(t):
        res = client.post(
            "/admin/templates/tpl-overlay-001/resequence-slots",
            headers=_headers(),
            json={"slot_index": 99},
        )
    assert res.status_code == 400, res.text


def test_resequence_slots_overflow_warning(client: TestClient) -> None:
    t = _template_with_overlapping_phrases()
    # Tighten the slot so the rippled second phrase overflows it.
    t.recipe_cached["slots"][0]["target_duration_s"] = 1.0
    with _patch_get_template(t):
        res = client.post(
            "/admin/templates/tpl-overlay-001/resequence-slots",
            headers=_headers(),
            json={},
        )
    assert res.status_code == 200, res.text
    body = res.json()
    assert body["reflow_warning"] is not None
    assert body["reflow_warning"]["overlays_pushed_past_target"] >= 1


def test_resequence_slots_already_sequential_is_noop(client: TestClient) -> None:
    t = _template_with_overlays()
    t.recipe_cached = {
        "slots": [
            {
                "target_duration_s": 10.0,
                "text_overlays": [
                    {"sample_text": "first", "start_s": 0.0, "end_s": 1.0},
                    {"sample_text": "second", "start_s": 1.0, "end_s": 2.0},
                ],
            }
        ]
    }
    with _patch_get_template(t):
        res = client.post(
            "/admin/templates/tpl-overlay-001/resequence-slots",
            headers=_headers(),
            json={},
        )
    assert res.status_code == 200, res.text
    overlays = t.recipe_cached["slots"][0]["text_overlays"]
    assert [round(o["start_s"], 2) for o in overlays] == [0.0, 1.0]
    assert res.json()["reflow_warning"] is None


# ── fit_to_duration ("Fit to time") ──────────────────────────────────────────


def test_fit_to_duration_compresses_reveals_to_fit(client: TestClient) -> None:
    """fit_to_duration=true sequences the phrases AND speeds up the per-word
    pacing so the last overlay ends within the slot's target duration — without
    changing any wording."""
    t = _template_with_overlapping_phrases()
    # Sequenced, these four 1.0s stages run end-to-end to 4.0s. Squeeze into 2.0.
    t.recipe_cached["slots"][0]["target_duration_s"] = 2.0
    original_texts = [o["sample_text"] for o in t.recipe_cached["slots"][0]["text_overlays"]]
    with _patch_get_template(t):
        res = client.post(
            "/admin/templates/tpl-overlay-001/resequence-slots",
            headers=_headers(),
            json={"fit_to_duration": True},
        )
    assert res.status_code == 200, res.text
    overlays = t.recipe_cached["slots"][0]["text_overlays"]
    assert [o["sample_text"] for o in overlays] == original_texts  # wording untouched
    last_end = max(o["end_s"] for o in overlays)
    assert last_end <= 2.0 + 1e-6, f"slot still overflows after fit: last end {last_end}"
    # Compressed (not just sequenced): the timeline shrank from 4.0 toward 2.0.
    assert last_end < 4.0
    assert res.json()["reflow_warning"] is None


def test_fit_to_duration_respects_legibility_floor(client: TestClient) -> None:
    """When the target is so tight that fitting would push reveals below the
    legibility floor, compression stops at the floor and the residual overflow
    is still reported (non-blocking) rather than producing unreadable flashes."""
    t = _template_with_overlapping_phrases()
    t.recipe_cached["slots"][0]["target_duration_s"] = 0.15  # 4×1.0s stages can't fit legibly
    with _patch_get_template(t):
        res = client.post(
            "/admin/templates/tpl-overlay-001/resequence-slots",
            headers=_headers(),
            json={"fit_to_duration": True},
        )
    assert res.status_code == 200, res.text
    overlays = t.recipe_cached["slots"][0]["text_overlays"]
    last_end = max(o["end_s"] for o in overlays)
    # Compressed substantially from 4.0, but floored above the 0.15 target — the
    # second phrase still starts past the slot end, so the overflow notice fires.
    assert last_end < 4.0
    assert last_end > 0.15
    assert res.json()["reflow_warning"] is not None


def test_fit_to_duration_noop_when_already_fits(client: TestClient) -> None:
    """If the sequenced phrases already fit the slot, fit_to_duration leaves the
    timeline alone (no needless compression)."""
    t = _template_with_overlapping_phrases()
    t.recipe_cached["slots"][0]["target_duration_s"] = 10.0  # sequenced end (4.0) fits easily
    with _patch_get_template(t):
        res = client.post(
            "/admin/templates/tpl-overlay-001/resequence-slots",
            headers=_headers(),
            json={"fit_to_duration": True},
        )
    assert res.status_code == 200, res.text
    overlays = t.recipe_cached["slots"][0]["text_overlays"]
    # Sequenced (phrase 2 rippled to 2.0) but NOT compressed past that.
    assert round(overlays[2]["start_s"], 2) == 2.0
    assert max(o["end_s"] for o in overlays) == 4.0
    assert res.json()["reflow_warning"] is None
