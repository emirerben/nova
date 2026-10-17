"""KRI-686: "eight of them, about 2 seconds each" must be read as a moment ask.

Incident (prod thread 1390E686, 2026-10-15, Tigre boat kit M2): three 90 s night takes plus a
16 s arrival clip. "From the long takes only, pick the moments where a lit house, the ferry
or the amusement park passes -- eight of them, about 2 seconds each, no black-sky stretches
... 20 seconds." The brief kept the requirement as
"pick the moments where a lit house, the ferry or the amusement park passes -- eight of them,
about 2 seconds each, no black-sky stretches". `moment_ask` returned None for it, so the
KRI-596 moment-window path never ran: the phone cut 4 clips x 5.0 s, each source 0-5 s
(`analysis_fallback`), with no moment chosen and the dark opening of every take on screen.

Ways this can fail, each pinned below:

1. A number word apart from "moments" ("eight of them", "8 in total", "sekiz tane") is not a
   count, so the ask has no count and no moment windows are planned.
2. "about 2 seconds each" / "2 s per shot" / "her biri 2 saniye" is not read as a per-moment
   length, so "short" never turns on and the planner has no length to derive a count from.
3. The new readers over-reach: a count of the creator's footage ("4 of them" after "I filmed
   10 clips"), a left-out count ("skip 2 of them"), or a per-CLIP hold ("each clip 3
   seconds", "photos 2 seconds each") becomes a moment ask and slices their takes.
4. A decimal length ("1.5 seconds each") is read as "1" and "5" (a count of 5, a length of 5).
5. A per-moment length longer than 3 s is dropped, so "5 seconds each" cannot size the cuts.
6. The count is found, the receipt flips from "can't verify" to "met", yet the half of the
   ask no receipt can see ("no black-sky stretches") is silently dropped from the verdict.
7. The ask is read but the plan does not follow: still one 0-5 s window per take, windows
   off the source or overlapping, the total off the creator's 20 s, or a take left out.
8. The phone recipe the plan compiles to is refused by the contract verifier.
9. The count and the length are both given and disagree (8 x 2 s in 20 s): the creator's
   20 s still wins and the receipt does not call a 5 s moment "about 2 seconds".
"""

from __future__ import annotations

import dataclasses
import uuid

import pytest

from app.config import settings
from app.kria.brief import BriefRequirement, CreativeBrief
from app.kria.brief_binding import BriefBinding
from app.kria.brief_checks import check_requirement, plan_facts_from_rendered_montage
from app.kria.planner import _enrich_best_parts_snapshot, _moment_count_notice
from app.kria.style_asks import brief_moment_ask, moment_ask
from app.pipeline.guided_story import compile_execution_plan
from app.pipeline.unified_montage import brief_view, plan_unified_montage
from app.services.creator_render_contract import build_render_contract
from tests.pipeline.test_unified_montage import clip
from tests.tasks.test_selected_clip_dispatch import _verify


@pytest.fixture(autouse=True)
def _clip_intents_on(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(settings, "clip_intents_enabled", True)


# -- the incident's own words ------------------------------------------------------------

M2_R3 = (
    "pick the moments where a lit house, the ferry or the amusement park passes "
    "— eight of them, about 2 seconds each, no black-sky stretches"
)


def _req(text: str, *, kind: str = "select", facts: dict | None = None) -> BriefRequirement:
    return BriefRequirement(id="r3", kind=kind, scope="global", description=text, facts=facts or {})


def _shape(text: str, **kwargs) -> tuple | None:
    ask = moment_ask(_req(text, **kwargs))
    if ask is None:
        return None
    return (ask.count, ask.short, ask.each_s, ask.long_takes_only, ask.excludes)


@pytest.mark.parametrize(
    ("text", "shape"),
    [
        # count, short, each_s, long_takes_only, excludes
        (M2_R3, (8, True, 2.0, False, True)),
        # 1: a count that is not next to "moments"
        ("pick the moments where the ferry passes, 8 of them", (8, False, None, False, False)),
        ("pick the moments where the ferry passes, ten of them", (10, False, None, False, False)),
        ("pick some moments, eight in total", (8, False, None, False, False)),
        ("choose the best moments, 6 total", (6, False, None, False, False)),
        # 2: the length of each moment
        ("pick 8 moments, about 2 seconds each", (8, True, 2.0, False, False)),
        ("pick six moments, 2 s each", (6, True, 2.0, False, False)),
        ("pick six moments, 2s each", (6, True, 2.0, False, False)),
        ("six shots, 2 seconds per shot", (6, True, 2.0, False, False)),
        ("pick six moments, each about 3 seconds", (6, True, 3.0, False, False)),
        ("pick six moments, each one 2 seconds long", (6, True, 2.0, False, False)),
        ("eight 2-second moments", (8, True, 2.0, False, False)),
        ("the best moments, 2 seconds each", (None, True, 2.0, False, False)),
        # 4: decimals stay one number
        ("pick six moments, about 1.5 seconds each", (6, True, 1.5, False, False)),
        ("pick six moments, 2,5 seconds each", (6, True, 2.5, False, False)),
        # 5: a longer moment is still a length
        ("pick six moments, about 5 seconds each", (6, False, 5.0, False, False)),
        (
            "pick the moments where the ferry passes, 5 seconds each",
            (None, False, 5.0, False, False),
        ),
        # Turkish
        ("sekiz tane an seç, her biri 2 saniye", (8, True, 2.0, False, False)),
        ("8 an seç", (8, False, None, False, False)),
        ("bu anlardan 8 tane seç", (8, False, None, False, False)),
        ("2 saniyelik sekiz an seç", (8, True, 2.0, False, False)),
        ("feneri geçtiği anları seç, her biri yaklaşık 2 saniye", (None, True, 2.0, False, False)),
        # the shape of the old readers is untouched
        (
            "Pick the best six moments from the long takes, each very short",
            (6, True, None, True, False),
        ),
        ("give me the best 5 shots", (5, False, None, False, False)),
        # 6: a clause no receipt can see
        ("pick 6 moments, no dark stretches", (6, False, None, False, True)),
        ("8 an seç, karanlık kısımlar olmasın", (8, False, None, False, True)),
        ("pick 6 moments, no more than 3 seconds", (6, False, None, False, False)),
    ],
)
def test_moment_asks_are_read_in_the_wording_creators_use(text: str, shape: tuple) -> None:
    assert _shape(text) == shape


@pytest.mark.parametrize(
    "text",
    [
        # 3: the creator's footage, what to leave out, and per-clip holds are not moments
        "I filmed 10 clips, use 4 of them",
        "use my 4 clips, 3 of them from the beach",
        "use the clips, 4 of them",
        "pick the best 4 of my 10 clips",
        "use 4 clips, 2 seconds each",
        "photos 2 seconds each",
        "show each clip 2 seconds",
        "make each clip 3 seconds long",
        "pick the dark moments and skip 2 of them",
        "skip 2 of them",
        "keep it short, 18 seconds",
        "make it 20 seconds",
        "Speed the river shots up 2x",
        "End on the short arrival clip where I say tam 20 dakika sürdü",
        "10 klip çektim, 4 tanesini kullan",
        "her klip 3 saniye olsun",
        "2 tane atla",
        "pick the moments, 1 of them",
    ],
)
def test_footage_counts_and_per_clip_holds_are_not_moment_asks(text: str) -> None:
    assert moment_ask(_req(text)) is None, text
    assert moment_ask(_req(text, kind="timing")) is None, text


def test_a_recorded_count_and_length_decide_over_the_words() -> None:
    # The model that read the message may record the meaning in `facts` (KRI-667).
    ask = moment_ask(_req("pick the good parts", facts={"moment_count": 8, "moment_length_s": 2}))
    assert ask is not None and (ask.count, ask.each_s, ask.short) == (8, 2.0, True)
    junk = moment_ask(_req(M2_R3, facts={"moment_count": 99, "moment_length_s": -1}))
    assert junk is not None and (junk.count, junk.each_s) == (8, 2.0)


def test_the_newest_length_wins_across_the_brief() -> None:
    brief = CreativeBrief(
        version=1,
        requirements=[
            _req("pick six moments, 2 seconds each"),
            BriefRequirement(
                id="r4",
                kind="select",
                scope="global",
                description="make them 3 s each, six moments",
            ),
        ],
    )
    ask = brief_moment_ask(brief)
    assert ask is not None and (ask.count, ask.each_s) == (6, 3.0)


# -- the incident, end to end ------------------------------------------------------------

FERRY_PARK, LIT_HOUSES, DEPARTURE, ARRIVAL = "c0", "c1", "c2", "c3"
DURATIONS = {FERRY_PARK: 90.0, LIT_HOUSES: 90.0, DEPARTURE: 90.0, ARRIVAL: 16.0}
CLIP_ORDER = [FERRY_PARK, LIT_HOUSES, DEPARTURE, ARRIVAL]


def _analysis(media_id: str) -> dict:
    """What clip analysis returned on the phone (job 6e00b13b): only the first take had moments."""
    if media_id == FERRY_PARK:
        return {
            "best_moments": [
                {"start_s": 0.0, "end_s": 10.0, "energy": 5.0, "description": "shore lights"},
                {"start_s": 16.0, "end_s": 25.0, "energy": 4.0, "description": "river pan"},
                {"start_s": 33.0, "end_s": 42.0, "energy": 5.0, "description": "industrial shore"},
                {"start_s": 49.0, "end_s": 58.0, "energy": 5.0, "description": "amusement park"},
                # past the end of a 90 s take: the model's clock ran ahead
                {"start_s": 115.0, "end_s": 129.0, "energy": 6.0, "description": "ferris wheel"},
            ],
            "understanding": {"speech": {"has_speech": False}},
        }
    return {"best_moments": [], "understanding": {"speech": {"has_speech": False}}}


def _clips():
    return [
        dataclasses.replace(
            clip(i, duration=DURATIONS[f"c{i}"]),
            analysis=_analysis(f"c{i}"),
            width=1080,
            height=1920,
        )
        for i in range(4)
    ]


def _snapshot() -> dict:
    return {
        "clip_assignments": [
            {
                "media_id": c.media_id,
                "kind": "video",
                "gcs_path": c.proxy_path,
                "storage_generation": c.generation,
                "duration_s": c.duration_s,
                "analysis": _analysis(c.media_id),
            }
            for c in _clips()
        ]
    }


def _strategy() -> dict:
    return {
        "edit_format": "montage",
        "render_program": "guided",
        "direction": "guided_story",
        "pacing": "fast",
        "media_scope": "selected",
        "selected_media_ids": CLIP_ORDER,
        "audio_strategy": "original_audio",
        "montage_audio": {"preserve_source_audio": True, "source_media_ids": []},
        "opening_title": "20 minutes in the dark",
        "target_duration_s": 20.0,
        "creator_duration_s": 20.0,
        "target_duration_requested": True,
        "video_reuse_policy": "once",
        # r6: the resolver seated "the short arrival clip" last
        "resolved_clip_intents": [
            {
                "op": "order",
                "status": "resolved",
                "intent_id": "order_last_arrival",
                "attribute": "the short arrival clip where I say tam 20 dakika sürdü",
                "position": "last",
                "assignments": [
                    {"media_id": ARRIVAL, "confidence": 1.0, "evidence": "16 s, says it"}
                ],
            }
        ],
    }


def _brief(pick: str = M2_R3) -> CreativeBrief:
    return CreativeBrief(
        version=2,
        requirements=[
            BriefRequirement(id="r1", kind="text", scope="title", literal="20 minutes in the dark"),
            BriefRequirement(
                id="r2",
                kind="timing",
                scope="global",
                description="20 seconds",
                facts={"duration_s": 20},
            ),
            BriefRequirement(id="r3", kind="select", scope="global", description=pick),
            BriefRequirement(
                id="r4", kind="style", scope="global", description="Speed the river shots up 2x"
            ),
            BriefRequirement(
                id="r5",
                kind="audio",
                scope="global",
                description="Keep every clip's own sound without any music",
            ),
            BriefRequirement(
                id="r6",
                kind="order",
                scope="global",
                description="End on the short arrival clip where I say tam 20 dakika sürdü",
                facts={"last_clip": "the short arrival clip where I say tam 20 dakika sürdü"},
            ),
        ],
    )


def _windows(brief: CreativeBrief) -> list[dict]:
    plan = _enrich_best_parts_snapshot(_strategy(), _snapshot(), brief)["length_plan"]
    assert not plan["conflicts"]
    return plan["windows"]


def test_incident_eight_moments_of_about_two_seconds_are_planned() -> None:
    ask = brief_moment_ask(_brief())
    assert ask is not None and (ask.count, ask.short, ask.each_s) == (8, True, 2.0)

    windows = _windows(_brief())
    # 7: eight separate moments, not one 0-5 s window per take
    assert len(windows) == 8
    spans = [(w["media_id"], w["source_start_s"], w["source_end_s"]) for w in windows]
    assert {media_id for media_id, _s, _e in spans} == set(CLIP_ORDER)  # no take left out
    assert not any(w["reason"] == "analysis_fallback" for w in windows)
    assert all(e - s <= 3.0 + 1e-6 for _m, s, e in spans)  # short
    # the creator's 20 s still wins over 8 x 2 s: each moment is 2.5 s
    assert sum(e - s for _m, s, e in spans) == pytest.approx(20.0, abs=0.05)
    for media_id in CLIP_ORDER:
        own = sorted((s, e) for m, s, e in spans if m == media_id)
        assert all(0 <= s < e <= DURATIONS[media_id] + 1e-6 for s, e in own)
        assert all(b[0] >= a[1] - 1e-6 for a, b in zip(own, own[1:], strict=False))  # no overlap
    # a take with analysed moments plays them (never the in-range-clipped 115 s one);
    # a take without any is spread over its length, not cut from second 0
    first = [w for w in windows if w["media_id"] == FERRY_PARK]
    assert first and all(w["reason"] == "best_moment" for w in first)
    assert all(w["source_end_s"] <= 58.0 + 1e-6 for w in first)
    for media_id in (LIT_HOUSES, DEPARTURE):
        own = [w for w in windows if w["media_id"] == media_id]
        assert own and all(w["reason"] == "moment_fill" for w in own)
        assert all(w["source_start_s"] > 5.0 for w in own)
    # the reply does not claim a shortfall
    strategy = _strategy()
    plan = _enrich_best_parts_snapshot(strategy, _snapshot(), _brief())["length_plan"]
    assert _moment_count_notice(strategy, _brief(), plan) is None


def test_incident_plan_renders_eight_cuts_the_phone_can_verify() -> None:
    strategy, snapshot = _strategy(), _snapshot()
    brief = _brief()
    enriched = _enrich_best_parts_snapshot(strategy, snapshot, brief)
    binding = BriefBinding.create(
        uuid.uuid5(uuid.NAMESPACE_URL, "kri-686"),
        brief,
        latest_message="eight moments of about 2 seconds",
        media_snapshot=enriched,
    )
    resolved = binding.resolve()
    clips = _clips()
    plan = plan_unified_montage(
        clips,
        brief_view(resolved),
        strategy=strategy,
        clip_intents_enabled=True,
        creator_duration_proven=True,
        approved_length_plan=enriched["length_plan"],
    )
    cuts = plan.snapshot.fast_cuts
    assert len(cuts) == 8
    assert plan.duration_s == pytest.approx(20.0, abs=0.05)
    assert all(c.source_end_s - c.source_start_s <= 3.0 + 1e-6 for c in cuts)
    contract = build_render_contract(
        strategy, generation_id="gen", brief=resolved, media_snapshot=enriched
    )
    assert _verify(contract, plan, clips)  # 8

    # 6: the finished video is judged honestly
    execution = compile_execution_plan(plan.guided_edit(), track=None)
    finished = {"story_timeline": execution["story_timeline"], "source_audio_preserved": True}
    facts = plan_facts_from_rendered_montage(finished, plan.record())
    receipt = check_requirement(_brief().live()[2], facts)
    assert receipt.status == "partial" and "left out" in receipt.reason
    clear = _req(
        "pick the moments where a lit house, the ferry or the amusement park passes "
        "— eight of them, about 2 seconds each"
    )
    met = check_requirement(clear, facts)
    assert met.status == "met", met.reason
    assert "8 moments" in met.reason
    # 9: moments of 2.5 s are not "about 5 seconds", and a count off by one is not met
    assert check_requirement(_req("pick 8 moments, about 1 second each"), facts).status == "partial"
    assert (
        check_requirement(_req("pick 9 moments, about 2 seconds each"), facts).status == "partial"
    )


def test_a_length_without_a_count_sizes_the_moments() -> None:
    # "2 seconds each" in 20 s is ten moments of 2 s, shared among the takes.
    windows = _windows(_brief("pick the moments where the ferry passes, 2 seconds each"))
    assert len(windows) == 10
    assert all(w["source_end_s"] - w["source_start_s"] == pytest.approx(2.0) for w in windows)
    # "about 5 seconds each" in 20 s is four moments, one per take (no slicing of a take)
    five = _windows(_brief("pick the moments where the ferry passes, about 5 seconds each"))
    assert len(five) == 4


def test_a_per_clip_hold_or_a_footage_count_leaves_the_plan_alone() -> None:
    # 3: the same takes, the same windows as an edit that asked for none of this.
    plain = _windows(_brief("Use all the clips"))
    for text in (
        "show each clip 3 seconds",
        "use 4 clips, 2 seconds each",
        "use my 4 clips, 3 of them from the beach",
    ):
        assert _windows(_brief(text)) == plain, text
