import pytest
from pydantic import ValidationError

from app.agents._schemas.text_animation_phases import TextAnimationPhases
from app.pipeline.text_animation_phases import sample_text_phases, visible_grapheme_count


def test_independent_edges_and_speed_do_not_change_item_window():
    phases = TextAnimationPhases(entrance="fade", exit="slide", loop="pulse", speed=2)
    start = sample_text_phases(phases, 0.1, 2)
    end = sample_text_phases(phases, 1.9, 2)
    assert start.alpha == pytest.approx(0.875)
    assert start.x == 0
    assert end.x == pytest.approx(5)
    assert end.alpha == pytest.approx(0.875)
    assert sample_text_phases(phases, 2, 2).alpha == 0
    assert sample_text_phases(phases, 1, 2).alpha == 1


def test_backward_seek_is_deterministic_and_short_windows_do_not_overlap():
    phases = TextAnimationPhases(entrance="typewriter", exit="typewriter")
    expected = sample_text_phases(phases, 0.05, 0.2)
    sample_text_phases(phases, 0.18, 0.2)
    assert sample_text_phases(phases, 0.05, 0.2) == expected
    assert expected.reveal == pytest.approx(0.5)
    assert visible_grapheme_count(4, expected.reveal) == 2
    assert sample_text_phases(phases, 0.1, 0.2).reveal == 1


@pytest.mark.parametrize("speed", [float("nan"), float("inf"), 0, 4])
def test_invalid_speed_is_rejected(speed):
    with pytest.raises(ValidationError):
        TextAnimationPhases(speed=speed)


@pytest.mark.parametrize(
    "loop,scale,y", [("none", 1, 0), ("pulse", 1.04, 0), ("bounce", 1, -12), ("float", 1, -8)]
)
def test_loop_extrema_preserve_visibility_and_reveal(loop, scale, y):
    sample = sample_text_phases(TextAnimationPhases(loop=loop), 0.3, 2)
    assert sample.scale == pytest.approx(scale)
    assert sample.y == pytest.approx(y)
    assert sample.alpha == sample.reveal == 1
    assert sample.x == 0


def test_pop_edges_and_inactive_windows():
    phases = TextAnimationPhases(entrance="pop", exit="pop")
    start = sample_text_phases(phases, 0, 2)
    assert start.scale == pytest.approx(0.7)
    assert start.alpha == 0
    leading = sample_text_phases(phases, 0.2, 2)
    trailing = sample_text_phases(phases, 1.8, 2)
    assert leading.alpha == pytest.approx(trailing.alpha)
    assert leading.scale == pytest.approx(trailing.scale)
    for time, duration in [(-0.1, 2), (0, 0), (0, -1), (2, 2), (3, 2)]:
        sample = sample_text_phases(phases, time, duration)
        assert sample.alpha == sample.reveal == 0


def test_grapheme_reveal_clamps_and_rounds_at_discrete_boundaries():
    assert visible_grapheme_count(0, 0.5) == 0
    assert visible_grapheme_count(4, -0.2) == 0
    assert visible_grapheme_count(4, 2) == 4
    assert visible_grapheme_count(4, 0.249) == 0
    assert visible_grapheme_count(4, 0.25) == 1
