"""KRI-569: lyric captions follow the song window through editor revisions.

Plans a real song montage, pins the lyric track exactly as the worker does
(``_attach_song_captions``), then replays editor revisions through
``compile_guided_runtime_plan`` and the phone compiler. The approved ``song-caption-N``
rows keep the creator's words and style; only their timing follows the song clock.
"""

from __future__ import annotations

import copy

import pytest

from app.pipeline.guided_story import (
    GuidedStoryExecutionPlan,
    compile_execution_plan,
    compile_guided_runtime_plan,
)
from app.pipeline.phone_guided_plan import compile_phone_guided_plan
from app.pipeline.song_captions import project_words, track_from_analysis
from app.pipeline.unified_montage import BriefView, plan_unified_montage
from app.schemas.guided_edit_revision import guided_editor_revision_from_approval
from app.schemas.user_song import SongWord
from app.tasks import generative_build as gb
from tests._prod_profile import PROD_VERIFIED_FEATURES
from tests.pipeline.user_song_helpers import (
    SONG_DURATION_S,
    SONG_GENERATION,
    SONG_ITEM_ID,
    analysis,
    bindings_for,
    song_bed,
    take,
)


@pytest.fixture(autouse=True)
def _prod_profile(monkeypatch):
    from app.config import settings

    monkeypatch.setattr(settings, "phone_render_verified_features", list(PROD_VERIFIED_FEATURES))


def _lyrics():
    words = [
        SongWord(text=f"la{i}", start_s=2.0 + i * 0.5, end_s=2.0 + i * 0.5 + 0.4)
        for i in range(230)
    ]
    return analysis().model_copy(update={"words": words})


def _planned():
    info = _lyrics()
    clips = [take(f"c{i}", 6.0) for i in range(1, 6)]
    plan = plan_unified_montage(
        clips,
        song_beats=info.beats_s,
        song_lines=info.lines,
        song_duration_s=SONG_DURATION_S,
        song_plan_item_id=SONG_ITEM_ID,
        song_generation=SONG_GENERATION,
    )
    gb._attach_song_captions(plan, info, {"caption_style": "karaoke"}, "add captions")
    guided = plan.guided_edit()
    canonical = compile_execution_plan(guided, track=None)
    return plan, guided, canonical


def _revision(guided, canonical, **song):
    revision = guided_editor_revision_from_approval(
        proposal_version=guided["proposal_version"],
        media_digest=guided["media_digest"],
        snapshot=guided["approved_proposal"],
        execution_plan=canonical,
    )
    if song:
        revision["user_song"] = song
    revision["state_hash"] = ""
    return revision


def _captions(plan):
    return {
        row["id"]: row for row in plan["text_elements"] if row["id"].startswith("song-caption-")
    }


def _visible(rows):
    return {key: row for key, row in rows.items() if not row.get("removed")}


def test_the_approved_plan_projects_the_songs_words_inside_its_window():
    plan, _guided, canonical = _planned()
    window = plan.snapshot.user_song
    rows = _captions(canonical)
    expected = project_words(
        plan.snapshot.song_captions, window.window_start_s, window.window_end_s
    )
    assert [int(key.removeprefix("song-caption-")) for key in rows] == [w.index for w in expected]
    # The song clock: a word sung 3 s into the window shows 3 s into the video.
    first = expected[0]
    assert rows[f"song-caption-{first.index}"]["start_s"] == pytest.approx(first.start_s)
    assert canonical["editor_caption_meta"] == {"style": "word", "y_frac": 0.7}


def test_moving_the_background_start_retimes_captions_and_keeps_every_identity():
    plan, guided, canonical = _planned()
    window = plan.snapshot.user_song
    before = _captions(canonical)
    shift = 4.0

    runtime = compile_guided_runtime_plan(
        canonical,
        guided,
        _revision(guided, canonical, window_start_s=window.window_start_s + shift),
    )
    after = _captions(runtime)

    # Every approved identity survives (a word that left the window is hidden, not dropped).
    assert set(before) <= set(after)
    visible = _visible(after)
    both = set(before) & set(visible)
    assert both
    for key in both:
        assert visible[key]["start_s"] == pytest.approx(before[key]["start_s"] - shift)
    # The words that slid out of the front are kept as hidden rows.
    gone = set(before) - set(visible)
    assert gone and all(after[key]["removed"] is True for key in gone)
    # A word the window now reaches at the tail is a new row.
    assert set(visible) - set(before)
    assert all(row["end_s"] <= runtime["resolved_duration_s"] + 1e-6 for row in visible.values())


def test_moving_the_start_earlier_reveals_the_words_before_it():
    plan, guided, canonical = _planned()
    window = plan.snapshot.user_song
    runtime = compile_guided_runtime_plan(
        canonical,
        guided,
        _revision(guided, canonical, window_start_s=max(0.0, window.window_start_s - 3.0)),
    )
    new = set(_visible(_captions(runtime))) - set(_captions(canonical))
    assert new, "words before the old start should now be captioned"


def test_the_creators_wording_and_deletions_survive_a_window_move():
    plan, guided, canonical = _planned()
    window = plan.snapshot.user_song
    revision = _revision(guided, canonical, window_start_s=window.window_start_s + 1.0)
    rows = [row for row in revision["text_elements"] if row["id"].startswith("song-caption-")]
    rows[5]["text"] = "my own words"
    rows[6]["removed"] = True  # the creator deleted this word

    runtime = compile_guided_runtime_plan(canonical, guided, revision)
    after = _captions(runtime)

    assert after[rows[5]["id"]]["text"] == "my own words"
    assert after[rows[6]["id"]]["removed"] is True


def test_a_creator_end_hides_the_words_after_it():
    plan, guided, canonical = _planned()
    window = plan.snapshot.user_song
    cut_at = window.window_start_s + 5.0
    runtime = compile_guided_runtime_plan(
        canonical, guided, _revision(guided, canonical, window_end_s=cut_at)
    )
    visible = _visible(_captions(runtime))
    assert visible
    assert max(row["end_s"] for row in visible.values()) <= 5.0 + 1e-6
    assert any(row["removed"] for row in _captions(runtime).values())


def test_removing_the_song_hides_its_lyrics():
    _plan, guided, canonical = _planned()
    runtime = compile_guided_runtime_plan(
        canonical, guided, _revision(guided, canonical, removed=True)
    )
    rows = _captions(runtime)
    assert rows and all(row["removed"] is True for row in rows.values())


def test_hidden_words_are_not_drawn_on_the_phone():
    plan, guided, canonical = _planned()
    window = plan.snapshot.user_song
    runtime = compile_guided_runtime_plan(
        canonical,
        guided,
        _revision(guided, canonical, window_start_s=window.window_start_s + 4.0),
    )
    bindings, _visuals = bindings_for(plan)
    recipe = compile_phone_guided_plan(
        GuidedStoryExecutionPlan.model_validate(copy.deepcopy(runtime)),
        bindings,
        song=song_bed(),
    )
    drawn = {run.text for layer in recipe.text_layers for run in layer.runs}
    hidden = {row["text"] for row in _captions(runtime).values() if row["removed"]}
    shown = {row["text"] for row in _visible(_captions(runtime)).values()}
    assert shown <= drawn
    assert hidden.isdisjoint(drawn)


def test_a_track_needs_the_words_and_lines_of_the_analysis():
    info = _lyrics()
    track = track_from_analysis(info, caption_style="word")
    assert track is not None and track.song_generation == SONG_GENERATION
    assert sum(1 for word in track.words if word.line_end) == len(info.lines) + (
        0 if track.words[-1].line_end else 1
    )
    assert track_from_analysis(analysis(), caption_style="word") is None


def _echo(guided, canonical, runtime, **song):
    """A later Save: the editor sends back exactly the rows the last compile returned."""
    revision = _revision(guided, canonical, **song)
    revision["text_elements"] = copy.deepcopy(runtime["text_elements"])
    return revision


def test_a_word_the_creator_deleted_stays_deleted_across_window_moves():
    plan, guided, canonical = _planned()
    start = plan.snapshot.user_song.window_start_s
    first = sorted(_captions(canonical), key=lambda key: int(key.rsplit("-", 1)[1]))[0]

    deleting = _revision(guided, canonical)
    next(row for row in deleting["text_elements"] if row["id"] == first)["removed"] = True
    runtime = compile_guided_runtime_plan(canonical, guided, deleting)
    assert _captions(runtime)[first]["removed"] is True

    # Slide the song past the word, then back: the creator's deletion must hold.
    away = compile_guided_runtime_plan(
        canonical, guided, _echo(guided, canonical, runtime, window_start_s=start + 8.0)
    )
    assert _captions(away)[first]["removed"] is True
    back = compile_guided_runtime_plan(
        canonical, guided, _echo(guided, canonical, away, window_start_s=start)
    )
    assert _captions(back)[first]["removed"] is True
    # ...while a word the window merely hid comes back.
    window_hidden = next(
        key
        for key, row in _captions(away).items()
        if row["removed"] and key != first and row["source_params"].get("window_hidden")
    )
    assert _captions(back)[window_hidden]["removed"] is False


def test_out_of_order_whisper_words_still_make_a_valid_track():
    info = _lyrics()
    words = list(info.words)
    words[10], words[11] = words[11], words[10]  # whisper emitted two words out of order
    track = track_from_analysis(info.model_copy(update={"words": words}), caption_style="word")
    assert track is not None
    starts = [word.start_s for word in track.words]
    assert starts == sorted(starts)
    assert [word.text for word in track.words] == [word.text for word in info.words]


def _long_track(lines: int, per_line: int):
    from app.schemas.user_song import SongCaptionTrack, SongCaptionWord

    words = []
    for line in range(lines):
        for position in range(per_line):
            at = (line * per_line + position) * 0.25
            words.append(
                SongCaptionWord(
                    text="la", start_s=at, end_s=at + 0.2, line_end=position == per_line - 1
                )
            )
    return SongCaptionTrack(song_generation=SONG_GENERATION, words=words)


def test_a_capped_lane_ends_on_a_whole_lyric_line():
    from app.pipeline.song_captions import MAX_PROJECTED_WORDS, window_words

    track = _long_track(lines=90, per_line=7)  # 630 words, lines of seven
    everything = window_words(track, 0, 200)
    drawn = project_words(track, 0, 200)
    assert len(everything) == 630
    assert len(drawn) <= MAX_PROJECTED_WORDS
    assert drawn[-1].line_end is True and len(drawn) % 7 == 0
    assert len(drawn) > MAX_PROJECTED_WORDS - 7
    # No line end anywhere: nothing better to cut on, so it is a hard cut at the limit.
    flat = _long_track(lines=1, per_line=630)
    assert len(project_words(flat, 0, 200)) == MAX_PROJECTED_WORDS


def test_the_layer_budget_leaves_room_for_the_montages_other_text():
    from app.kria.recipes_v2 import EditRecipeV2
    from app.pipeline.phone_captions import MAX_CAPTION_LAYERS
    from app.pipeline.song_captions import RECIPE_TEXT_LAYERS, lyric_word_budget

    field = EditRecipeV2.model_fields["text_layers"]
    cap = next(m.max_length for m in field.metadata if hasattr(m, "max_length"))
    assert RECIPE_TEXT_LAYERS == cap == MAX_CAPTION_LAYERS
    for labels, pins in [(0, 0), (8, 4), (30, 4), (150, 4)]:
        words = lyric_word_budget(label_count=labels, pin_count=pins)
        # words + a title + closing + a label per clip + pins + composer headroom <= the cap
        assert words + 2 + labels + pins + 60 <= RECIPE_TEXT_LAYERS
    assert lyric_word_budget(label_count=150, pin_count=4) < lyric_word_budget()


def test_a_dense_long_song_still_compiles_under_the_recipe_layer_cap(monkeypatch):
    """A 2-minute montage over 4 words a second would be 480 lyric layers + labels."""
    from app.config import settings
    from app.kria.recipes_v2 import EditRecipeV2

    info = analysis(duration_s=300.0, line_starts=tuple(float(i) for i in range(0, 296, 2)))
    info = info.model_copy(
        update={
            "words": [
                SongWord(text="la", start_s=i * 0.25, end_s=i * 0.25 + 0.2) for i in range(1190)
            ]
        }
    )
    clips = [take(f"c{i}", 6.0) for i in range(1, 21)]
    plan = plan_unified_montage(
        clips,
        BriefView(target_duration_s=118),
        song_beats=info.beats_s,
        song_lines=info.lines,
        song_duration_s=300.0,
        song_plan_item_id=SONG_ITEM_ID,
        song_generation=SONG_GENERATION,
    )
    gb._attach_song_captions(plan, info, {"caption_style": "karaoke"}, "add captions")
    receipt = plan.caption_receipt
    assert receipt["truncated"] is True and receipt["words"] <= 400, (
        receipt,
        plan.snapshot.duration_s,
    )
    assert receipt["in_window"] > receipt["words"]
    canonical = compile_execution_plan(plan.guided_edit(), track=None)
    bindings, _visuals = bindings_for(plan)
    monkeypatch.setattr(settings, "phone_render_verified_features", list(PROD_VERIFIED_FEATURES))
    recipe = compile_phone_guided_plan(
        GuidedStoryExecutionPlan.model_validate(canonical),
        bindings,
        song=song_bed(duration_s=300.0),
    )
    assert (
        len(recipe.text_layers) <= EditRecipeV2.model_fields["text_layers"].metadata[0].max_length
    )
