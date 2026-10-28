"""Free, deterministic voice checks for camera-roll ideas (no network, no skia).

These are a heuristic floor, not a verdict: they catch the loud diary tells and the
absence of a viewer-directed claim. The judge rubric (`rubrics/camera_roll_stories.md`)
owns the nuance. English copy only.

Per idea the checks read the viewer-facing copy (title, opening_text, closing_text, caption)
and, for recap and place detection only, the angle as well.

  diary_recap   a first-person or diary-time marker AND no viewer-directed claim.
  filler        stock travel-brochure or admiration words, or a vague-invitation sentence
                opener ('Experience ...'), that name nothing in the footage.
  names_place   whether the copy names the event's place; None when no place is in context.
"""

from __future__ import annotations

import re
from dataclasses import asdict, dataclass, field

from app.agents.camera_roll_copy_edit import STOCK_OPENERS, STOCK_PHRASES

COPY_FIELDS = ("opening_text", "closing_text", "caption")
GENERIC_PLACE_WORDS = frozenset(
    {"island", "trip", "weekend", "day", "holiday", "vacation", "outing", "summer", "town", "city"}
)

_FIRST_PERSON = re.compile(
    r"\b(?:i(?:'m|'ve|'d|'ll)?|my|mine|we(?:'re|'ve|'ll|'d)?|our|ours)\b", re.IGNORECASE
)
_DIARY_TIME = re.compile(
    r"\b(?:today|yesterday|this (?:morning|afternoon|evening)|last (?:weekend|summer|week)"
    r"|day in the life|spent the day|a (?:slow|lazy|quiet|relaxed|perfect) day)\b",
    re.IGNORECASE,
)
_VIEWER_DIRECTED = re.compile(
    r"\byou(?:r|rs|'ll|'re|'ve|'d)?\b"
    r"|\b(?:why|worth|here'?s|how to|before you|don'?t miss|must[- ](?:see|visit)|should"
    r"|reasons?|tips?|things to|places to|spots? to|stop scrolling|save this"
    r"|visitors|travell?ers|anyone|everyone|nobody|no one|most people)\b",
    re.IGNORECASE,
)
_IMPERATIVE_OPENER = re.compile(
    r"^\s*(?:visit|go to|see|walk|watch|try|book|come|look|skip)\b", re.I
)
# The vocabulary is the app's (the copy edit pass shows it to its model as examples), so the
# eval and the product mean the same thing by stock copy.
_STOCK_OPENER = re.compile(
    r"(?:^|[.!?:]\s+)(" + "|".join(re.escape(o) for o in STOCK_OPENERS) + r")\b",
    re.IGNORECASE | re.MULTILINE,
)


def stock_copy(text: str) -> tuple[str, ...]:
    """Stock phrases and vague-invitation sentence openers found in `text`, lowercased."""
    lowered = text.lower()
    phrases = tuple(p for p in STOCK_PHRASES if p in lowered)
    return phrases + tuple(m.group(1).lower() for m in _STOCK_OPENER.finditer(text))


@dataclass(frozen=True)
class IdeaVoice:
    title: str
    diary_recap: bool
    recap_markers: tuple[str, ...]
    viewer_directed: bool
    filler: tuple[str, ...]
    names_place: bool | None


@dataclass
class VoiceReport:
    ideas: list[IdeaVoice] = field(default_factory=list)

    def rate(self, flag: str) -> float | None:
        """Share of ideas with `flag` true; None for an empty set or an inapplicable check."""
        values = [getattr(idea, flag) for idea in self.ideas]
        if not values or any(v is None for v in values):
            return None
        return sum(1 for v in values if v) / len(values)

    def summary(self) -> dict:
        named = self.rate("names_place")
        return {
            "ideas": len(self.ideas),
            "diary_recap_rate": self.rate("diary_recap"),
            "filler_rate": (
                sum(1 for i in self.ideas if i.filler) / len(self.ideas) if self.ideas else None
            ),
            "missing_place_rate": None if named is None else 1 - named,
        }

    def to_dict(self) -> dict:
        return {"summary": self.summary(), "ideas": [asdict(i) for i in self.ideas]}


def place_terms(place: str | None) -> tuple[str, ...]:
    """Distinctive lowercase words of an event title: 'Cunda Island' -> ('cunda',)."""
    words = re.findall(r"[^\W\d_]{3,}", (place or "").casefold())
    return tuple(w for w in words if w not in GENERIC_PLACE_WORDS)


def _hits(pattern: re.Pattern[str], text: str) -> tuple[str, ...]:
    return tuple(m.group(0).lower() for m in pattern.finditer(text))


def check_idea(story: dict, *, place: str | None = None) -> IdeaVoice:
    plan = story.get("content_plan") or {}
    title = str(story.get("title") or "")
    surfaces = [title, *(str(plan.get(f) or "") for f in COPY_FIELDS)]
    copy = "\n".join(surfaces)
    recap_text = copy + "\n" + str(plan.get("angle") or "")
    markers = _hits(_FIRST_PERSON, recap_text) + _hits(_DIARY_TIME, recap_text)
    viewer = bool(_VIEWER_DIRECTED.search(copy)) or any(
        _IMPERATIVE_OPENER.match(s) for s in surfaces
    )
    terms = place_terms(place)
    return IdeaVoice(
        title=title,
        diary_recap=bool(markers) and not viewer,
        recap_markers=markers,
        viewer_directed=viewer,
        filler=stock_copy(copy),
        names_place=(any(t in (recap_text).casefold() for t in terms) if terms else None),
    )


def analyze_stories(stories: list[dict], *, place: str | None = None) -> VoiceReport:
    """`stories` are `CameraRollStoriesOutput.model_dump()["stories"]` entries."""
    return VoiceReport([check_idea(s, place=place) for s in stories])


# ── Judge-side helpers and the baseline file shape ──────────────────────────────

JUDGE_DIMENSIONS = ("diary_voice", "viewer_value", "specificity", "grounding")


def composite(scores: dict[str, float]) -> float | None:
    """Higher is better. diary_voice points the other way, so it is flipped."""
    if not all(d in scores for d in JUDGE_DIMENSIONS):
        return None
    flipped = [6 - scores["diary_voice"], *(scores[d] for d in JUDGE_DIMENSIONS[1:])]
    return sum(flipped) / len(flipped)


def judge_view(agent_input: dict) -> dict:
    """The part of the agent input the judge needs; drops ids, timings and quality JSON."""
    view = {k: agent_input[k] for k in ("query", "event_title", "event_about") if k in agent_input}
    view["footage"] = [
        f"[{c.get('media', 'video')}] {c['text']}" for c in agent_input.get("candidates", [])
    ]
    return view


def score_run(output: dict, *, place: str | None, agent_input: dict, judge=None) -> dict:
    """One run's record: structural report plus (optionally) judge scores."""
    record = {
        "output": output,
        "voice": analyze_stories(output.get("stories", []), place=place).to_dict(),
    }
    if judge is not None:
        result = judge.score(
            agent_name="nova.media.camera_roll_stories",
            agent_input=judge_view(agent_input),
            agent_output=output,
        )
        record["judge"] = {
            "scores": result.scores,
            "composite": composite(result.scores),
            "reasoning": result.reasoning,
        }
    return record


def _mean(values: list) -> float | None:
    values = [v for v in values if v is not None]
    return sum(values) / len(values) if values else None


def _filler_rate(output: dict) -> float | None:
    return analyze_stories(output.get("stories", [])).summary()["filler_rate"]


def aggregate(runs: list[dict]) -> dict[str, float | None]:
    """Mean of every structural rate and judge dimension across a variant's runs.

    filler_rate is rescored from the saved output, so a file recorded under an older stock list
    compares fairly. draft_filler_rate is the same check before the copy edit pass, when the run
    recorded its draft.
    """
    metrics: dict[str, float | None] = {}
    for key in ("diary_recap_rate", "missing_place_rate"):
        metrics[key] = _mean([r["voice"]["summary"][key] for r in runs])
    metrics["filler_rate"] = _mean([_filler_rate(r["output"]) for r in runs])
    metrics["draft_filler_rate"] = _mean([_filler_rate(r["draft"]) for r in runs if "draft" in r])
    judged = [r["judge"] for r in runs if "judge" in r]
    for dim in JUDGE_DIMENSIONS:
        metrics[dim] = _mean([j["scores"].get(dim) for j in judged])
    metrics["composite"] = _mean([j["composite"] for j in judged])
    return metrics


def compare(before: dict, after: dict) -> dict[str, dict[str, float | None]]:
    """Per shared variant, after minus before for every aggregate metric."""
    result = {}
    for name in sorted(set(before["variants"]) & set(after["variants"])):
        old = aggregate(before["variants"][name]["runs"])
        new = aggregate(after["variants"][name]["runs"])
        result[name] = {
            k: None if old[k] is None or new[k] is None else round(new[k] - old[k], 3) for k in old
        }
    return result
