"""Unit tests for `_carry_forward_overlays` — the helper that preserves manual
overlay edits across an agent re-run (the "Re-run agents never resets overlays"
behavior). Pure function: mutates `new_slots` in place from `prior_slots`.
"""

from app.tasks.template_orchestrate import _carry_forward_overlays


def _slot(overlays, dur=None):
    s = {"text_overlays": overlays}
    if dur is not None:
        s["target_duration_s"] = dur
    return s


def test_equal_counts_carries_overlays_verbatim() -> None:
    prior = [_slot([{"sample_text": "kept", "start_s": 0.0, "end_s": 1.0}])]
    new = [_slot([{"sample_text": "fresh", "start_s": 0.0, "end_s": 2.0}])]
    _carry_forward_overlays(new, prior)
    assert new[0]["text_overlays"][0]["sample_text"] == "kept"


def test_fewer_new_slots_drops_extra_prior() -> None:
    prior = [_slot([{"sample_text": "a"}]), _slot([{"sample_text": "b"}])]
    new = [_slot([{"sample_text": "fresh"}])]
    _carry_forward_overlays(new, prior)
    assert len(new) == 1
    assert new[0]["text_overlays"][0]["sample_text"] == "a"


def test_more_new_slots_keep_fresh_overlays_on_new_slots() -> None:
    prior = [_slot([{"sample_text": "a"}])]
    new = [_slot([{"sample_text": "freshA"}]), _slot([{"sample_text": "freshB"}])]
    _carry_forward_overlays(new, prior)
    assert new[0]["text_overlays"][0]["sample_text"] == "a"  # carried
    assert new[1]["text_overlays"][0]["sample_text"] == "freshB"  # kept (no prior)


def test_prior_overlays_empty_carries_empty() -> None:
    prior = [_slot([])]
    new = [_slot([{"sample_text": "fresh"}])]
    _carry_forward_overlays(new, prior)
    assert new[0]["text_overlays"] == []


def test_prior_missing_text_overlays_key_carries_empty() -> None:
    prior = [{"target_duration_s": 2.0}]  # no text_overlays key
    new = [_slot([{"sample_text": "fresh"}])]
    _carry_forward_overlays(new, prior)
    assert new[0]["text_overlays"] == []


def test_none_prior_is_noop() -> None:
    new = [_slot([{"sample_text": "fresh"}])]
    _carry_forward_overlays(new, None)
    assert new[0]["text_overlays"][0]["sample_text"] == "fresh"


def test_carried_overlay_clamped_to_shorter_new_slot() -> None:
    # Prior overlay ran to 5.0s; the rebuilt clip is only 3.0s long.
    prior = [_slot([{"sample_text": "kept", "start_s": 0.0, "end_s": 5.0}])]
    new = [_slot([{"sample_text": "fresh"}], dur=3.0)]
    _carry_forward_overlays(new, prior)
    ov = new[0]["text_overlays"][0]
    assert ov["sample_text"] == "kept"
    assert ov["end_s"] == 3.0  # clamped to the new clip


def test_carried_overlay_dropped_when_start_past_new_slot() -> None:
    prior = [
        _slot(
            [
                {"sample_text": "visible", "start_s": 0.0, "end_s": 1.0},
                {"sample_text": "gone", "start_s": 4.0, "end_s": 5.0},
            ]
        )
    ]
    new = [_slot([{"sample_text": "fresh"}], dur=3.0)]
    _carry_forward_overlays(new, prior)
    texts = [o["sample_text"] for o in new[0]["text_overlays"]]
    assert texts == ["visible"]  # the 4.0s-start overlay is dropped


def test_does_not_mutate_prior() -> None:
    prior = [_slot([{"sample_text": "kept", "start_s": 0.0, "end_s": 5.0}])]
    new = [_slot([{"sample_text": "fresh"}], dur=3.0)]
    _carry_forward_overlays(new, prior)
    # deep-copy means clamping the carried copy didn't touch the prior recipe.
    assert prior[0]["text_overlays"][0]["end_s"] == 5.0
