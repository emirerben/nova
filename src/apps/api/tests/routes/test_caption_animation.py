"""Caption animation (KRI-1007): entrance / exit / loop / speed on captions, per line or for all.

Every case drives the real editor Save (`prepare_editor_commit`) and reads back what the iPhone
loads and what the device recipe draws. Failure modes written first:

  (a) phases saved on one line leak onto other lines, or are dropped on persist;
  (b) the all-captions default overrides a line's own phases, or misses lines without one;
  (c) a Save that sets no phases moves any output (recipe, stored cues, caption meta);
  (d) hidden lyric rows (KRI-1000) draw because phases were set, or lose phases when shown;
  (e) an app build below the caption-animation protocol saves phases, or wipes stored ones;
  (f) an invalid phase name or an out-of-range speed is stored;
  (g) a guided revision round trip drops a caption row's phases.
"""

from __future__ import annotations

import json
from pathlib import Path

from tests.routes.caption_animation_fixture import GOLDEN_FILE, all_cases
from tests.tasks.test_user_song_montage_job import composer, harness  # noqa: F401

GOLDEN = json.loads(Path(__file__).with_name(GOLDEN_FILE).read_text())


def test_saves_without_caption_animation_match_origin_main(harness, composer):  # noqa: F811
    assert all_cases(harness) == GOLDEN
