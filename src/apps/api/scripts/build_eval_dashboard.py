"""Generate a single self-contained HTML dashboard from the agent eval fixtures + test log.

Reads:
  - tests/fixtures/agent_evals/<agent>/{prod_snapshots,golden}/*.json
    where <agent> ∈ {template_recipe, creative_direction, clip_metadata,
                     transcript, platform_copy, audio_template}
  - .dev/agent-evals-report/03_pytest_run.log (optional)

Writes:
  - .dev/agent-evals-report/dashboard.html

Open with:
  open .dev/agent-evals-report/dashboard.html

Regenerate any time fixtures change. No external deps, no build step.
"""

from __future__ import annotations

import html
import json
import re
import subprocess
import sys
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
FIXTURES_ROOT = ROOT / "tests" / "fixtures" / "agent_evals"
PROMPTS_ROOT = ROOT / "prompts"
AGENTS_ROOT = ROOT / "app" / "agents"
REPO_ROOT = ROOT.parent.parent.parent
DASHBOARD_DIR = REPO_ROOT / ".dev" / "agent-evals-report"
DASHBOARD_DIR.mkdir(parents=True, exist_ok=True)
PYTEST_LOG = DASHBOARD_DIR / "03_pytest_run.log"
DASHBOARD_FILE = DASHBOARD_DIR / "dashboard.html"

AGENTS = (
    "template_recipe",
    "creative_direction",
    "clip_metadata",
    "transcript",
    "platform_copy",
    "audio_template",
)

# Per-agent narrative metadata (phase, cardinality, what it does)
AGENT_NARRATIVE: dict[str, dict[str, str]] = {
    "nova.compose.creative_direction": {
        "phase": "admin",
        "cardinality": "once · template onboarding · Pass 1",
        "what": "Watches the reference video and writes a freeform paragraph describing its editing style — pacing, transitions, color, beats. Feeds Pass 2.",
    },
    "nova.compose.template_recipe": {
        "phase": "admin",
        "cardinality": "once · template onboarding · Pass 2",
        "what": "Turns the reference video plus the Pass 1 paragraph into a structured JSON recipe: shot count, slot durations, transitions, overlays, interstitials, color grade.",
    },
    "nova.audio.template_recipe": {
        "phase": "admin",
        "cardinality": "once · per music track",
        "what": "Beat-synced version of template_recipe. Reads detected beats from a music track and produces a slot layout where every slot snaps to a beat.",
    },
    "nova.audio.song_classifier": {
        "phase": "admin",
        "cardinality": "once · per music track",
        "what": "Creative-direction labels for a music track (genre, vibe_tags, energy, pacing, mood, ideal_content_profile, copy_tone, transition_style, color_grade). Producer of MusicLabels; the matcher (Phase 2) uses these to pair clip sets with songs in auto-music mode.",
    },
    "nova.video.clip_metadata": {
        "phase": "job",
        "cardinality": "per-clip · in parallel",
        "what": "Scores the user's clip and finds 2-5 best moments with action descriptions. Produces the data the matcher uses to decide which clip goes in which slot.",
    },
    "nova.audio.transcript": {
        "phase": "job",
        "cardinality": "per-job · once",
        "what": "Transcribes audio to word-level timestamps. Feeds caption rendering and provides hooks for clip_metadata to reason over.",
    },
    "nova.compose.platform_copy": {
        "phase": "job",
        "cardinality": "per-clip · post-render",
        "what": "Writes platform-specific captions (TikTok, Instagram, YouTube) from the chosen hook + transcript excerpt. Used after rendering finishes.",
    },
    "nova.layout.text_designer": {
        "phase": "job",
        "cardinality": "per-slot · per-overlay · agentic templates",
        "what": "Tunes overlay text per slot — font cycle behavior, accel timing, settle phase. Promoted from shadow mode to production for <code>is_agentic=true</code> templates (PR #136); manual templates still use static <code>_LABEL_CONFIG</code>.",
        "status_badge": "PR #136 · in prod",
        "status_badge_kind": "has-fixtures",
    },
    "nova.layout.transition_picker": {
        "phase": "job",
        "cardinality": "per-slot · shadow mode",
        "what": "Picks the transition into each slot from the recipe's vocabulary (whip-pan, dissolve, zoom-in, cut). Still shadow-mode; production wiring deferred per PR2 scope decision.",
        "status_badge": "shadow only",
    },
    "nova.video.shot_ranker": {
        "phase": "job",
        "cardinality": "per-clip · candidate ranking",
        "what": "Moment ranking within a clip. Codified but not yet wired — deferred until <code>clip_router</code> proves itself in production.",
        "status_badge": "codified · not wired",
    },
    "nova.video.clip_router": {
        "phase": "job",
        "cardinality": "slot → clip · agentic templates",
        "what": "Slot → clip assignment. Newly wired into production for <code>is_agentic=true</code> templates (PR #136) with a greedy fallback via the new <code>agentic_matcher</code> module. Manual templates still use the rule-based matcher.",
        "status_badge": "PR #136 · in prod",
        "status_badge_kind": "has-fixtures",
    },
    "nova.audio.beat_aligner": {
        "phase": "job",
        "cardinality": "rule-based · per-job",
        "what": "Snaps cumulative slot timestamps to the nearest beat. Pure Python — no LLM. Used only in music-template mode.",
    },
    "nova.qa.output_validator": {
        "phase": "job",
        "cardinality": "rule-based · per-job · post-render",
        "what": "Validates the final assembly plan against structural invariants. Pure Python. Catches anything that would break FFmpeg before render.",
    },
}

# Make `app` importable so we can introspect agent specs and pydantic schemas.
sys.path.insert(0, str(ROOT))

# Map fixture-dir name → registered agent name (for cross-referencing)
FIXTURE_AGENT_NAME = {
    "template_recipe": "nova.compose.template_recipe",
    "creative_direction": "nova.compose.creative_direction",
    "clip_metadata": "nova.video.clip_metadata",
    "transcript": "nova.audio.transcript",
    "platform_copy": "nova.compose.platform_copy",
    "audio_template": "nova.audio.template_recipe",
    "song_classifier": "nova.audio.song_classifier",
}

# Map agent_name → prompt files to display (some agents share prompts)
AGENT_PROMPT_FILES: dict[str, list[str]] = {
    "nova.video.clip_metadata": ["analyze_clip.txt"],
    "nova.compose.creative_direction": ["analyze_template_pass1.txt"],
    "nova.compose.template_recipe": [
        "analyze_template_single.txt",
        "analyze_template_pass2.txt",
        "analyze_template_schema.txt",
    ],
    "nova.audio.template_recipe": [
        "analyze_audio_template.txt",
        "analyze_template_schema.txt",
    ],
    "nova.audio.song_classifier": ["classify_song.txt"],
    "nova.audio.transcript": ["transcribe.txt"],
}


def _load_fixtures(agent: str) -> list[dict]:
    """Walk both prod_snapshots/ and golden/ subdirs and return all fixtures.

    Source dir is recorded on the fixture dict as `_source` ("prod_snapshots"
    or "golden") so the renderer can label them. Slugs are namespaced by source
    to avoid collision when the same name appears in both subdirs.
    """
    out: list[dict] = []
    for source in ("prod_snapshots", "golden"):
        base = FIXTURES_ROOT / agent / source
        if not base.exists():
            continue
        for path in sorted(base.glob("*.json")):
            try:
                data = json.loads(path.read_text())
                data["_path"] = str(path.relative_to(REPO_ROOT))
                data["_slug"] = path.stem
                data["_source"] = source
                out.append(data)
            except Exception as exc:
                out.append(
                    {
                        "_path": str(path),
                        "_slug": path.stem,
                        "_source": source,
                        "_error": str(exc),
                    }
                )
    return out


def _maybe_run_pytest() -> str:
    """Read existing pytest log; if missing, run it."""
    if PYTEST_LOG.exists():
        return PYTEST_LOG.read_text()
    try:
        result = subprocess.run(
            [".venv/bin/pytest", "tests/evals/", "-v", "--no-header", "--color=no"],
            cwd=str(ROOT),
            capture_output=True,
            text=True,
            timeout=60,
        )
        log = result.stdout + result.stderr
        PYTEST_LOG.write_text(log)
        return log
    except Exception as exc:
        return f"pytest run failed: {exc}"


def _parse_pytest(log: str) -> dict:
    fixtures: list[tuple[str, str]] = []
    pass_count = fail_count = skip_count = 0
    for line in log.splitlines():
        m = re.search(
            r"tests/evals/(test_\w+_evals\.py)::test_\w+\[([^\]]+)\]\s+(PASSED|FAILED|SKIPPED)",
            line,
        )
        if m:
            fixtures.append((f"{m.group(1)}::{m.group(2)}", m.group(3)))
    summary = re.search(r"(\d+)\s+passed(?:,\s+(\d+)\s+failed)?(?:,\s+(\d+)\s+skipped)?", log)
    if summary:
        pass_count = int(summary.group(1) or 0)
        fail_count = int(summary.group(2) or 0)
        skip_count = int(summary.group(3) or 0)
    duration = re.search(r"in\s+([\d.]+)s", log)
    return {
        "fixtures": fixtures,
        "pass_count": pass_count,
        "fail_count": fail_count,
        "skip_count": skip_count,
        "duration_s": float(duration.group(1)) if duration else 0.0,
    }


# ── Issue detection ──────────────────────────────────────────────────────────


def _detect_issues(
    template_recipe_fxs: list[dict], creative_direction_fxs: list[dict]
) -> list[dict]:
    issues: list[dict] = []
    cd_slugs = {f["_slug"] for f in creative_direction_fxs}

    # Empty niche
    bad = [
        f["_slug"]
        for f in template_recipe_fxs
        if not (f.get("output", {}).get("subject_niche") or "").strip()
    ]
    if bad:
        issues.append(
            {
                "title": "Empty `subject_niche` field",
                "severity": "med",
                "count": len(bad),
                "templates": bad,
                "what": f"{len(bad)} of {len(template_recipe_fxs)} templates leave `subject_niche` blank. The schema asks for it.",
                "fix": "Re-run two-pass analysis on these templates so the recipe captures niche.",
            }
        )

    # has_* None
    bad = [
        f["_slug"]
        for f in template_recipe_fxs
        if f.get("output", {}).get("has_talking_head") is None
        and f.get("output", {}).get("has_voiceover") is None
        and f.get("output", {}).get("has_permanent_letterbox") is None
    ]
    if bad:
        issues.append(
            {
                "title": "All `has_*` flags are None (pre-two-pass schema)",
                "severity": "high",
                "count": len(bad),
                "templates": bad,
                "what": f"{len(bad)} templates have None for all three boolean flags. These were analyzed before two-pass mode shipped.",
                "fix": "Reanalyze with `analysis_mode='two_pass'` so the agent populates booleans.",
            }
        )

    # Default copy_tone
    bad = [
        f["_slug"] for f in template_recipe_fxs if f.get("output", {}).get("copy_tone") == "casual"
    ]
    if bad:
        issues.append(
            {
                "title": "Default `copy_tone='casual'` (likely fallback)",
                "severity": "med",
                "count": len(bad),
                "templates": bad,
                "what": f"{len(bad)} templates returned the schema default 'casual'. Strong signal the agent gave up rather than reasoning.",
                "fix": "Investigate prompt — the agent should refuse rather than emit defaults silently.",
            }
        )

    # Sub-0.5s slots
    bad = []
    for f in template_recipe_fxs:
        for s in f.get("output", {}).get("slots", []) or []:
            try:
                if float(s.get("target_duration_s", 0)) < 0.5:
                    bad.append(f["_slug"])
                    break
            except Exception:
                pass
    if bad:
        issues.append(
            {
                "title": "Sub-0.5s slot durations (borderline unrenderable)",
                "severity": "med",
                "count": len(bad),
                "templates": bad,
                "what": "Slots under 0.5s are at the edge of what FFmpeg can render cleanly.",
                "fix": "Add a structural check + clamp at parse time, or tune the prompt to enforce a floor.",
            }
        )

    # Energy=0 slots
    bad = []
    for f in template_recipe_fxs:
        for s in f.get("output", {}).get("slots", []) or []:
            try:
                if float(s.get("energy", 5)) == 0:
                    bad.append(f["_slug"])
                    break
            except Exception:
                pass
    if bad:
        issues.append(
            {
                "title": "Slots with energy=0 (likely failed scoring)",
                "severity": "low",
                "count": len(bad),
                "templates": bad,
                "what": "Energy=0 inside an otherwise active recipe usually means the agent failed to score that slot, not that it's literally dead.",
                "fix": "Default to mid-range or refuse. Tune prompt to require an energy value.",
            }
        )

    # Missing creative_direction fixture
    missing_cd = [f["_slug"] for f in template_recipe_fxs if f["_slug"] not in cd_slugs]
    if missing_cd:
        issues.append(
            {
                "title": "No `creative_direction` fixture (under-baked Pass 1)",
                "severity": "high",
                "count": len(missing_cd),
                "templates": missing_cd,
                "what": "Templates whose stored creative_direction was rejected at export time (text < 50 words). These ran Pass 2 with no editorial guidance.",
                "fix": "Trigger reanalysis from admin UI — these templates predate the two-pass schema.",
            }
        )

    # Saygimdan v1/v2 byte-identical
    saygimdan = next((f for f in template_recipe_fxs if f["_slug"] == "saygimdan"), None)
    saygimdan_v2 = next(
        (f for f in template_recipe_fxs if f["_slug"] == "saygimdan_v2__gemini"), None
    )
    if saygimdan and saygimdan_v2:
        if saygimdan.get("output") == saygimdan_v2.get("output"):
            issues.append(
                {
                    "title": "Saygimdan v1/v2 are byte-identical recipes",
                    "severity": "low",
                    "count": 1,
                    "templates": ["saygimdan_v2__gemini"],
                    "what": "v2 is supposed to be an independent re-run, but the JSON matches v1 exactly — likely checkpointed without re-running Gemini.",
                    "fix": "Drop or actually rerun v2 with a real Gemini call.",
                }
            )

    return issues


# ── Agent introspection ──────────────────────────────────────────────────────


def _load_agent_catalog() -> list[dict]:
    """For every registered agent, pull spec + schema + source paths.

    Returns a list of dicts with: name, module, class_name, model, prompt_id,
    prompt_version, max_attempts, cost_per_1k_input_usd, cost_per_1k_output_usd,
    response_json, max_output_tokens, input_fields, output_fields, prompt_files,
    source_file.
    """
    try:
        from app.agents._registry import _REGISTRATIONS  # type: ignore
    except Exception as exc:
        print(f"warn: could not import agent registry: {exc}", flush=True)
        return []

    catalog: list[dict] = []
    for name, mod_path, cls_name in _REGISTRATIONS:
        entry: dict = {
            "name": name,
            "module": mod_path,
            "class_name": cls_name,
            "model": "—",
            "prompt_id": "—",
            "prompt_version": "—",
            "max_attempts": "—",
            "cost_in": 0.0,
            "cost_out": 0.0,
            "response_json": True,
            "max_output_tokens": None,
            "input_fields": [],
            "output_fields": [],
            "prompt_files": AGENT_PROMPT_FILES.get(name, []),
            "source_file": str(
                (AGENTS_ROOT / mod_path.split(".")[-1]).with_suffix(".py").relative_to(REPO_ROOT)
            ),
            "load_error": None,
        }
        try:
            import importlib

            mod = importlib.import_module(mod_path)
            cls = getattr(mod, cls_name)
            spec = cls.spec
            entry.update(
                {
                    "model": spec.model,
                    "prompt_id": spec.prompt_id,
                    "prompt_version": spec.prompt_version,
                    "max_attempts": spec.max_attempts,
                    "cost_in": spec.cost_per_1k_input_usd,
                    "cost_out": spec.cost_per_1k_output_usd,
                    "response_json": getattr(cls, "response_json", True),
                    "max_output_tokens": getattr(cls, "max_output_tokens", None),
                    "input_fields": _pydantic_fields(cls.Input),
                    "output_fields": _pydantic_fields(cls.Output),
                }
            )
        except Exception as exc:
            entry["load_error"] = f"{type(exc).__name__}: {exc}"
        catalog.append(entry)
    return catalog


def _pydantic_fields(model_cls) -> list[dict]:
    """Extract fields from a pydantic model: name, type, default, required."""
    out: list[dict] = []
    try:
        fields = getattr(model_cls, "model_fields", {})
        for fname, finfo in fields.items():
            ann = finfo.annotation
            type_str = _format_annotation(ann)
            default_str = "required"
            if not finfo.is_required():
                default = finfo.default
                try:
                    default_str = repr(default) if default is not None else "None"
                except Exception:
                    default_str = "<unrepr>"
                if hasattr(finfo, "default_factory") and finfo.default_factory is not None:
                    default_str = f"{finfo.default_factory.__name__}()"
            out.append(
                {
                    "name": fname,
                    "type": type_str,
                    "default": default_str,
                    "required": finfo.is_required(),
                }
            )
    except Exception:
        pass
    return out


def _format_annotation(ann) -> str:
    """Best-effort human-readable type string."""
    if ann is None:
        return "None"
    name = getattr(ann, "__name__", None)
    if name:
        return name
    s = str(ann)
    s = s.replace("typing.", "").replace("<class '", "").replace("'>", "")
    return s


def _load_prompt_text(filename: str) -> str | None:
    p = PROMPTS_ROOT / filename
    if not p.exists():
        return None
    return p.read_text()


def _get_render_prompt_source(agent_cls) -> str | None:
    """Return the source code of `render_prompt` (and any helper methods on the class)."""
    import inspect
    import textwrap

    try:
        method = getattr(agent_cls, "render_prompt", None)
        if method is None:
            return None
        src = inspect.getsource(method)
        return textwrap.dedent(src)
    except (OSError, TypeError):
        return None


def _highlight_python(code: str) -> str:
    """Lightweight Python syntax highlighter — regex-based, not lex-perfect.

    Wraps keywords, strings, comments, and decorators in <span class="…"> so the CSS
    can color them. Order matters: strings/comments first so keywords inside them
    aren't double-wrapped.
    """
    import re as _re

    code = html.escape(code)

    # Triple-quoted strings (handles f-strings like f""" too)
    def repl_triple(m):
        return f'<span class="py-str">{m.group(0)}</span>'

    code = _re.sub(r'([fFrRbB]?)("""|\'\'\')([\s\S]*?)\2', repl_triple, code)

    # Single-line strings
    code = _re.sub(
        r'([fFrRbB]?)("(?:[^"\\\n]|\\.)*"|\'(?:[^\'\\\n]|\\.)*\')',
        lambda m: f'<span class="py-str">{m.group(0)}</span>',
        code,
    )

    # Comments
    code = _re.sub(
        r"(?m)(#[^\n]*)$",
        lambda m: f'<span class="py-com">{m.group(0)}</span>',
        code,
    )

    keywords = (
        "def|return|if|elif|else|for|while|in|not|and|or|is|None|True|False|"
        "class|import|from|as|with|try|except|finally|raise|pass|continue|break|"
        "lambda|yield|async|await|global|nonlocal|del|assert"
    )
    code = _re.sub(
        rf"(?<![\w.])({keywords})(?![\w.])",
        lambda m: f'<span class="py-kw">{m.group(0)}</span>',
        code,
    )

    # Function definitions (def name)
    code = _re.sub(
        r'(<span class="py-kw">def</span>\s+)([\w_]+)',
        lambda m: f'{m.group(1)}<span class="py-fn">{m.group(2)}</span>',
        code,
    )

    # Common builtins / types
    builtins = "str|int|float|bool|list|dict|tuple|set|isinstance|len|range|enumerate"
    code = _re.sub(
        rf"(?<![\w.])({builtins})(?=\()",
        lambda m: f'<span class="py-bi">{m.group(0)}</span>',
        code,
    )

    # self
    code = _re.sub(
        r"(?<![\w.])(self)(?![\w.])",
        '<span class="py-self">self</span>',
        code,
    )

    # decorators
    code = _re.sub(
        r"(?m)^(\s*)(@[\w.]+)",
        lambda m: f'{m.group(1)}<span class="py-dec">{m.group(2)}</span>',
        code,
    )

    return code


# ── HTML rendering ───────────────────────────────────────────────────────────


def _esc(s) -> str:
    return html.escape(str(s) if s is not None else "")


def _slot_row(i: int, s: dict) -> str:
    return f"""<tr>
      <td class="num">{i}</td>
      <td>{_esc(s.get("slot_type", "—"))}</td>
      <td class="num">{_esc(s.get("target_duration_s", "—"))}s</td>
      <td class="num">{_esc(s.get("energy", "—"))}</td>
      <td><code>{_esc(s.get("transition_in", "—"))}</code></td>
      <td><code>{_esc(s.get("color_hint", "—"))}</code></td>
      <td class="num">{_esc(s.get("speed_factor", "—"))}</td>
      <td class="num">{len(s.get("text_overlays", []) or [])}</td>
    </tr>"""


def _interstitial_row(it: dict) -> str:
    return f"""<tr>
      <td class="num">{_esc(it.get("after_slot", "—"))}</td>
      <td><code>{_esc(it.get("type", "—"))}</code></td>
      <td class="num">{_esc(it.get("animate_s", "—"))}s</td>
      <td class="num">{_esc(it.get("hold_s", "—"))}s</td>
      <td><code>{_esc(it.get("hold_color", "—"))}</code></td>
    </tr>"""


def _render_template_card(
    tr_fx: dict, cd_fx: dict | None, fixture_test_status: dict[str, str]
) -> str:
    out = tr_fx.get("output", {})
    meta = tr_fx.get("meta", {})
    name = meta.get("template_name", tr_fx["_slug"])
    slug = tr_fx["_slug"]
    test_key = f"prod_snapshots/{slug}"
    tr_status = fixture_test_status.get(("template_recipe", test_key), "—")
    cd_status = fixture_test_status.get(("creative_direction", test_key), "—") if cd_fx else "—"

    # Issue tags for this card
    issue_tags: list[str] = []
    if not (out.get("subject_niche") or "").strip():
        issue_tags.append('<span class="tag warn">no niche</span>')
    if (
        out.get("has_talking_head") is None
        and out.get("has_voiceover") is None
        and out.get("has_permanent_letterbox") is None
    ):
        issue_tags.append('<span class="tag warn">stale schema</span>')
    if out.get("copy_tone") == "casual":
        issue_tags.append('<span class="tag warn">default tone</span>')
    if not cd_fx:
        issue_tags.append('<span class="tag err">no creative_direction</span>')

    slots_html = "".join(_slot_row(i + 1, s) for i, s in enumerate(out.get("slots", []) or []))
    inters = out.get("interstitials", []) or []
    inters_html = (
        "".join(_interstitial_row(it) for it in inters)
        if inters
        else '<tr><td colspan="5" class="dim">no interstitials</td></tr>'
    )

    cd_text = cd_fx["output"]["text"] if cd_fx else None
    cd_word_count = len(cd_text.split()) if cd_text else 0

    cd_block = (
        f"""
      <details class="cd-block">
        <summary>creative_direction · {cd_word_count} words · status={cd_status}</summary>
        <div class="cd-text">{_esc(cd_text)}</div>
      </details>"""
        if cd_text
        else '<div class="cd-block dim">no creative_direction fixture</div>'
    )

    raw_recipe_json = json.dumps(out, indent=2, default=str)

    return f"""<article class="card" data-slug="{_esc(slug)}" data-name="{_esc(name).lower()}">
  <header>
    <h3>{_esc(name)}</h3>
    <div class="card-meta">
      <span class="slug">{_esc(slug)}</span>
      <span class="status status-{tr_status.lower()}">recipe: {tr_status}</span>
      {"".join(issue_tags)}
    </div>
  </header>

  <div class="kv-grid">
    <div><dt>shot_count</dt><dd>{_esc(out.get("shot_count"))}</dd></div>
    <div><dt>total</dt><dd>{_esc(out.get("total_duration_s"))}s</dd></div>
    <div><dt>hook</dt><dd>{_esc(out.get("hook_duration_s"))}s</dd></div>
    <div><dt>copy_tone</dt><dd>{_esc(out.get("copy_tone"))}</dd></div>
    <div><dt>color_grade</dt><dd>{_esc(out.get("color_grade"))}</dd></div>
    <div><dt>sync_style</dt><dd>{_esc(out.get("sync_style"))}</dd></div>
    <div><dt>pacing</dt><dd>{_esc(out.get("pacing_style"))}</dd></div>
    <div><dt>niche</dt><dd>{_esc(out.get("subject_niche")) or '<span class="dim">—</span>'}</dd></div>
    <div><dt>talking_head</dt><dd>{_esc(out.get("has_talking_head"))}</dd></div>
    <div><dt>voiceover</dt><dd>{_esc(out.get("has_voiceover"))}</dd></div>
    <div><dt>letterbox</dt><dd>{_esc(out.get("has_permanent_letterbox"))}</dd></div>
  </div>

  <details>
    <summary>{len(out.get("slots", []) or [])} slot(s)</summary>
    <table class="slots">
      <thead><tr><th>#</th><th>type</th><th>dur</th><th>energy</th><th>transition</th><th>color</th><th>speed</th><th>overlays</th></tr></thead>
      <tbody>{slots_html}</tbody>
    </table>
  </details>

  <details>
    <summary>{len(inters)} interstitial(s)</summary>
    <table class="slots">
      <thead><tr><th>after_slot</th><th>type</th><th>animate</th><th>hold</th><th>color</th></tr></thead>
      <tbody>{inters_html}</tbody>
    </table>
  </details>

  {cd_block}

  <details>
    <summary>raw recipe JSON</summary>
    <pre><code>{_esc(raw_recipe_json)}</code></pre>
  </details>
</article>"""


def _render_issue(issue: dict) -> str:
    tags = "".join(f'<span class="tag tpl">{_esc(t)}</span>' for t in issue["templates"])
    sev = issue["severity"]
    return f"""<div class="issue sev-{sev}">
  <header>
    <h3>{_esc(issue["title"])}</h3>
    <span class="badge sev-{sev}">{sev.upper()}</span>
    <span class="count">{issue["count"]} affected</span>
  </header>
  <p class="what">{_esc(issue["what"])}</p>
  <p class="fix"><b>fix:</b> {_esc(issue["fix"])}</p>
  <div class="issue-templates">{tags}</div>
</div>"""


def _render_agent_card(agent: dict, fixtures_by_agent_name: dict[str, list[dict]]) -> str:
    name = agent["name"]
    has_fixtures = name in fixtures_by_agent_name and fixtures_by_agent_name[name]

    # Spec table
    spec_html = f"""<div class="kv-grid">
    <div><dt>name</dt><dd>{_esc(name)}</dd></div>
    <div><dt>model</dt><dd>{_esc(agent["model"])}</dd></div>
    <div><dt>class</dt><dd>{_esc(agent["class_name"])}</dd></div>
    <div><dt>prompt_id</dt><dd>{_esc(agent["prompt_id"])}</dd></div>
    <div><dt>prompt_version</dt><dd>{_esc(agent["prompt_version"])}</dd></div>
    <div><dt>max_attempts</dt><dd>{_esc(agent["max_attempts"])}</dd></div>
    <div><dt>response_json</dt><dd>{_esc(agent["response_json"])}</dd></div>
    <div><dt>max_output_tokens</dt><dd>{_esc(agent["max_output_tokens"])}</dd></div>
    <div><dt>cost / 1k input</dt><dd>${agent["cost_in"]:.6f}</dd></div>
    <div><dt>cost / 1k output</dt><dd>${agent["cost_out"]:.6f}</dd></div>
    <div><dt>source</dt><dd><code>{_esc(agent["source_file"])}</code></dd></div>
  </div>"""

    if agent.get("load_error"):
        spec_html += f'<p class="dim">load error: {_esc(agent["load_error"])}</p>'

    # Input / Output schemas
    def _render_field_table(fields: list[dict]) -> str:
        if not fields:
            return '<p class="dim">no fields detected</p>'
        rows = "".join(
            f"""<tr>
              <td><code>{_esc(f["name"])}</code></td>
              <td><code>{_esc(f["type"])}</code></td>
              <td><code>{_esc(f["default"])}</code></td>
              <td>{"required" if f["required"] else "optional"}</td>
            </tr>"""
            for f in fields
        )
        return f"""<table class="slots">
          <thead><tr><th>field</th><th>type</th><th>default</th><th></th></tr></thead>
          <tbody>{rows}</tbody>
        </table>"""

    input_html = _render_field_table(agent["input_fields"])
    output_html = _render_field_table(agent["output_fields"])

    # Prompt files (or "inline" note) + render_prompt() source
    prompt_files = agent.get("prompt_files") or []

    # Try to load the agent class to extract render_prompt source
    render_src = None
    try:
        import importlib

        mod = importlib.import_module(agent["module"])
        cls = getattr(mod, agent["class_name"], None)
        if cls is not None:
            render_src = _get_render_prompt_source(cls)
    except Exception:
        render_src = None

    blocks: list[str] = []
    if prompt_files:
        for fname in prompt_files:
            text = _load_prompt_text(fname)
            if text is None:
                blocks.append(
                    f'<details><summary>{_esc(fname)} <span class="dim">(file missing)</span></summary></details>'
                )
            else:
                blocks.append(
                    f"<details open><summary>{_esc(fname)} · {len(text.split())} words</summary><pre><code>{_esc(text)}</code></pre></details>"
                )
    else:
        # No external file — explain what that means inline
        if agent["model"] == "rule_based":
            blocks.append(
                '<div class="prompt-note"><b>Rule-based</b>'
                "No prompt — this agent runs pure Python logic. No LLM call. "
                "See the <code>compute()</code> method in the source file."
                "</div>"
            )
        else:
            blocks.append(
                '<div class="prompt-note"><b>Built inline</b>'
                "This agent's prompt is constructed at call time from input fields, "
                "not loaded from a static <code>.txt</code> file. The full construction "
                "logic lives in <code>render_prompt()</code> below."
                "</div>"
            )

    # Always show render_prompt() source — it shows the full prompt-construction story
    if render_src and "raise NotImplementedError" not in render_src:
        short_path = agent["source_file"].split("/")[-1]
        highlighted = _highlight_python(render_src)
        blocks.append(
            f"<details open><summary>render_prompt() · python · {short_path}</summary>"
            f'<pre class="py-source" data-source="{_esc(short_path)}"><code>{highlighted}</code></pre>'
            f"</details>"
        )

    prompts_html = "\n".join(blocks)

    # Per-template returns (only for agents with fixtures)
    if has_fixtures:
        fxs = fixtures_by_agent_name[name]

        def _opt_key(fx: dict) -> str:
            return f"{fx.get('_source', 'fixture')}_{fx['_slug']}"

        def _opt_label(fx: dict) -> str:
            slug = fx["_slug"]
            tname = fx.get("meta", {}).get("template_name", slug)
            source = fx.get("_source", "")
            tag = " · golden" if source == "golden" else ""
            return f"{tname}{tag}"

        opts = "".join(
            f'<option value="{_esc(_opt_key(fx))}">{_esc(_opt_label(fx))}</option>' for fx in fxs
        )
        panels = []
        for fx in fxs:
            slug = fx["_slug"]
            opt_key = _opt_key(fx)
            tname = fx.get("meta", {}).get("template_name", slug)
            output = fx.get("output", {})
            input_data = fx.get("input", {})
            raw_text = fx.get("raw_text", "")

            output_pretty = json.dumps(output, indent=2, default=str)
            input_pretty = json.dumps(input_data, indent=2, default=str)

            # If the output has a freeform "text" field (creative_direction), show it
            text_block = ""
            if "text" in output and len(output) <= 2:
                text_block = f'<div class="cd-text">{_esc(output["text"])}</div>'

            source_tag = (
                f' <span class="tag">{_esc(fx.get("_source", ""))}</span>'
                if fx.get("_source")
                else ""
            )
            panels.append(f"""<div class="agent-output-panel" data-slug="{_esc(opt_key)}" hidden>
              <h4>{_esc(tname)} <span class="dim">· {_esc(slug)}</span>{source_tag}</h4>
              {text_block}
              <details open>
                <summary>parsed output</summary>
                <pre><code>{_esc(output_pretty)}</code></pre>
              </details>
              <details>
                <summary>input the agent received</summary>
                <pre><code>{_esc(input_pretty)}</code></pre>
              </details>
              <details>
                <summary>raw_text (recorded model response · {len(raw_text)} chars)</summary>
                <pre><code>{_esc(raw_text[:8000])}{"…" if len(raw_text) > 8000 else ""}</code></pre>
              </details>
            </div>""")

        outputs_html = f"""<div class="agent-output-picker">
          <label>Show output for: <select class="output-select">{opts}</select></label>
          <span class="dim">{len(fxs)} fixture(s) on disk</span>
        </div>
        {"".join(panels)}"""
    else:
        outputs_html = (
            '<p class="dim">No fixtures on disk yet for this agent. '
            "Either it doesn't ship in Phase 1 (clip_metadata fixtures need a separate capture step), "
            "or it's a follow-up agent (Phase 2 evals — see TODOS.md).</p>"
        )

    return f"""<article id="agent-{_esc(name).replace(".", "-")}" class="agent-card">
  <header>
    <h2>{_esc(name)}</h2>
    <span class="model-badge">{_esc(agent["model"])}</span>
    {'<span class="tag tpl">' + str(len(fixtures_by_agent_name.get(name, []))) + " fixtures</span>" if has_fixtures else '<span class="tag">no fixtures</span>'}
  </header>

  <h3>Spec</h3>
  {spec_html}

  <h3>Prompt</h3>
  {prompts_html}

  <h3>Input schema</h3>
  {input_html}

  <h3>Output schema</h3>
  {output_html}

  <h3>Per-template returns</h3>
  {outputs_html}
</article>"""


def _render_agent_tile(idx: int, agent: dict, fixtures_by_agent_name: dict[str, list[dict]]) -> str:
    name = agent["name"]
    short = name.rsplit(".", 1)[-1]
    nar = AGENT_NARRATIVE.get(name, {"phase": "job", "cardinality": "—", "what": "—"})
    phase = nar["phase"]  # "admin" | "job"
    has_fxs = bool(fixtures_by_agent_name.get(name))
    is_rule = agent["model"] == "rule_based"

    badges: list[str] = []
    badges.append(
        f'<span class="badge phase-{phase}">{"Admin" if phase == "admin" else "Per-job"}</span>'
    )
    if is_rule:
        badges.append('<span class="badge py">Python</span>')
    else:
        badges.append('<span class="badge llm">LLM · Gemini</span>')

    # A `status_badge` from AGENT_NARRATIVE overrides the default "N fixtures"
    # badge so per-agent prod-status copy ("PR #136 · in prod", "shadow only",
    # "codified · not wired") sticks across regens.
    status_badge = nar.get("status_badge")
    status_kind = nar.get("status_badge_kind", "")
    if status_badge:
        kind_attr = f" {status_kind}" if status_kind else ""
        badges.append(f'<span class="badge{kind_attr}">{_esc(status_badge)}</span>')
    elif has_fxs:
        n = len(fixtures_by_agent_name[name])
        badges.append(f'<span class="badge has-fixtures">{n} fixtures</span>')

    # `what` allows inline HTML (e.g., <code>) for agents whose narrative
    # references config flags. Authors of AGENT_NARRATIVE are trusted.
    what_html = nar["what"] if ("<" in nar["what"]) else _esc(nar["what"])

    return f"""<article class="agent-tile is-{phase}{" is-fxs" if has_fxs else ""}"
              data-target="agent-{name.replace(".", "-")}"
              data-name="{_esc(short)}">
  <div class="agent-tile-num">{idx:02d}</div>
  <div class="agent-badges">{"".join(badges)}</div>
  <h3 class="agent-name">{_esc(short)}</h3>
  <div class="agent-cardinality">{_esc(nar["cardinality"])}</div>
  <p class="agent-what">{what_html}</p>
  <div class="agent-meta-row">
    <span><b>model</b> {_esc(agent["model"])}</span>
    <span><b>v</b> {_esc(agent["prompt_version"])}</span>
  </div>
</article>"""


_AI_VARIANT_PLANNED_AGENTS: list[dict] = [
    {
        "name": "TemplateAnalyzer",
        "phase": "admin",
        "llm": True,
        "cardinality": "1 per template, run once on publish/reanalyze",
        "what": "Reads each template MP4 and writes a TemplateCard (group, capabilities, fit_signals, variant_axes) to ai_template_cards. Separate from the legacy analyze_template_task.",
        "writes": "ai_template_cards row",
    },
    {
        "name": "ClipUnderstanding",
        "phase": "job",
        "llm": True,
        "cardinality": "N per job (one per uploaded clip), parallel",
        "what": "Per-clip Gemini call. Returns ClipCard with hook, best moments, visual + audio signals, usability verdict. Own prompt; does not extend analyze_clip.",
        "writes": "ai_jobs.clip_cards",
    },
    {
        "name": "EditorialDirector",
        "phase": "job",
        "llm": True,
        "cardinality": "1 per job, single call",
        "what": "The brain. Reads ClipCards + all live TemplateCards, picks templates from the complex + basic_text groups, perturbs variant axes, emits all M VariantBriefs with text rewrites baked in.",
        "writes": "ai_jobs.variant_briefs",
    },
    {
        "name": "VariantComposer",
        "phase": "job",
        "llm": False,
        "cardinality": "M per job (one per brief), parallel",
        "what": "Pure Python. Forked copy of template_matcher.match() resolves clip-to-slot. Hard-validates must_use_clip_ids, validates the RenderPlan. No LLM call.",
        "writes": "ai_job_variants.render_plan",
    },
    {
        "name": "Renderer",
        "phase": "job",
        "llm": False,
        "cardinality": "M per job, parallel (FFmpeg-bound)",
        "what": "Thin caller into the shared app/lib/render/ library (extracted from today's _assemble_clips). Both this orchestrator and the legacy template orchestrator call the same renderer.",
        "writes": "ai_job_variants.output_gcs_path",
    },
    {
        "name": "Critic (two-stage)",
        "phase": "job",
        "llm": True,
        "cardinality": "up to 2 per job (brief pruner pre-render + final ranker post-render)",
        "what": "Stage 1 (chat-token): prunes weak briefs before paying for renders. Stage 2 (video-token): ranks the rendered MP4s and picks the gallery default. Both stages flag-disable-able.",
        "writes": "ai_jobs.variant_ranking + ai_job_variants.critic_rank",
    },
]


def _render_planned_ai_variant_tile(t: dict) -> str:
    phase = t["phase"]
    badges: list[str] = []
    badges.append(
        f'<span class="badge phase-{phase}">{"Admin" if phase == "admin" else "Per-job"}</span>'
    )
    badges.append(
        '<span class="badge llm">LLM · Gemini</span>'
        if t["llm"]
        else '<span class="badge py">Python</span>'
    )
    badges.append(
        '<span class="badge" style="background:var(--accent-soft);color:var(--accent);border-color:var(--accent);">PLANNED</span>'
    )
    return f"""<article class="agent-tile is-{phase}"
              style="border-style:dashed;border-color:var(--line-strong);background:transparent;">
  <div class="agent-tile-num" style="color:var(--ink-4);">··</div>
  <div class="agent-badges">{"".join(badges)}</div>
  <h3 class="agent-name">{_esc(t["name"])}</h3>
  <div class="agent-cardinality">{_esc(t["cardinality"])}</div>
  <p class="agent-what">{_esc(t["what"])}</p>
  <div class="agent-meta-row">
    <span><b>writes</b> <code style="font-size:11px;">{_esc(t["writes"])}</code></span>
  </div>
</article>"""


def _render_flow_steps(agent_catalog: list[dict]) -> str:
    """A numbered flow with rail showing the actual pipeline stages."""
    catalog_by_name = {a["name"]: a for a in agent_catalog}
    steps = [
        (
            "admin",
            "is-llm",
            "Reference video → CreativeDirection",
            "nova.compose.creative_direction",
            "An admin imports a TikTok template. Pass 1 watches the video and writes a freeform paragraph about its editing style — pacing, transitions, color grading, beat sync.",
        ),
        (
            "admin",
            "is-llm",
            "Pass 1 text → TemplateRecipe",
            "nova.compose.template_recipe",
            "Pass 2 receives the Pass 1 paragraph and re-watches the video. Returns a structured JSON: shot count, per-slot durations, transitions, overlays, interstitials, color grade. This is the recipe.",
        ),
        (
            "admin",
            "is-store",
            "Recipe stored in Postgres",
            "VideoTemplate.recipe_cached",
            "The recipe is cached on the VideoTemplate row. Reused on every job that targets this template — never re-analyzed unless the admin clicks Reanalyze.",
        ),
        (
            "job",
            "is-llm",
            "User clip → ClipMetadata",
            "nova.video.clip_metadata",
            "User submits N clips. Each runs in parallel. Returns hook_text, hook_score (0-10), 2-5 best_moments with action descriptions, transcript, detected_subject.",
        ),
        (
            "job",
            "is-py",
            "Matcher assigns clips to slots",
            "template_matcher (rule-based)",
            "Pure Python. Reads the recipe's slot constraints (target duration, energy, color hint) and assigns each user clip's best moment to the most-suited slot. No LLM.",
        ),
        (
            "job",
            "is-py",
            "FFmpeg renders the final video",
            "render pipeline",
            "Subprocess FFmpeg stitches the clips, applies overlays, runs interstitials, mixes the template audio. 9:16, sub-60s, H.264/AAC.",
        ),
        (
            "job",
            "is-store",
            "Final video → GCS",
            "JobClip.video_path",
            "Stored in object storage. JobClip rows persist hook_text, hook_score, the final timestamp window, and platform_copy if the user requests caption generation.",
        ),
    ]
    out: list[str] = []
    for i, (phase, cls, title, agent_or_label, desc) in enumerate(steps, start=1):
        agent_data = catalog_by_name.get(agent_or_label)
        agent_html = _esc(agent_or_label)
        if agent_data and not agent_or_label.startswith("Video") and "·" not in agent_or_label:
            agent_html = (
                f'<a href="#agent-{agent_or_label.replace(".", "-")}" '
                f'data-target="agent-{agent_or_label.replace(".", "-")}">{_esc(agent_or_label)}</a>'
            )
        out.append(f"""<div class="flow-step {cls}">
  <div class="flow-rail"><div class="flow-num">{i}</div></div>
  <div class="flow-body">
    <h3 class="flow-title">{_esc(title)}</h3>
    <div class="flow-agent">{agent_html}</div>
    <p class="flow-desc">{_esc(desc)}</p>
  </div>
</div>""")
    return f'<div class="flow">{"".join(out)}</div>'


def _render_flow_diagram() -> str:
    return """<div class="flow-wrap">
  <div class="flow-stage">
    <div class="flow-stage-label">Stage 01 · <span class="stage-title">admin onboards a reference video</span></div>
    <div class="flow-row">
      <div class="flow-node input">
        <div class="flow-node-label">Source</div>
        <div class="flow-node-name">Reference video</div>
        <div class="flow-node-sub">a TikTok template the admin imports</div>
      </div>
      <div class="flow-arrow"><span class="label">analyzes</span></div>
      <div class="flow-node agent">
        <div class="flow-node-label">Pass 1 · LLM</div>
        <div class="flow-node-name"><code>creative_direction</code></div>
        <div class="flow-node-sub">freeform paragraph describing the editing style</div>
      </div>
      <div class="flow-arrow"><span class="label">feeds</span></div>
      <div class="flow-node agent">
        <div class="flow-node-label">Pass 2 · LLM</div>
        <div class="flow-node-name"><code>template_recipe</code></div>
        <div class="flow-node-sub">structured JSON: slots, transitions, overlays</div>
      </div>
      <div class="flow-arrow down"><span class="label">stored as</span></div>
      <div class="flow-node store" style="margin-left: auto;">
        <div class="flow-node-label">Postgres</div>
        <div class="flow-node-name"><code>VideoTemplate.recipe_cached</code></div>
        <div class="flow-node-sub">cached recipe, reused on every job</div>
      </div>
    </div>
  </div>

  <div class="flow-stage">
    <div class="flow-stage-label">Stage 02 · <span class="stage-title">user submits clips against a template</span></div>
    <div class="flow-row">
      <div class="flow-node input">
        <div class="flow-node-label">Input</div>
        <div class="flow-node-name">N user clips</div>
        <div class="flow-node-sub">each clip analyzed in parallel</div>
      </div>
      <div class="flow-arrow"><span class="label">scores</span></div>
      <div class="flow-node agent">
        <div class="flow-node-label">Per-clip · LLM</div>
        <div class="flow-node-name"><code>clip_metadata</code></div>
        <div class="flow-node-sub">hook_text, best_moments, transcript</div>
      </div>
      <div class="flow-arrow"><span class="label">assigns</span></div>
      <div class="flow-node action">
        <div class="flow-node-label">Rule-based</div>
        <div class="flow-node-name">template_matcher</div>
        <div class="flow-node-sub">slot assignment</div>
      </div>
      <div class="flow-arrow"><span class="label">outputs</span></div>
      <div class="flow-node store">
        <div class="flow-node-label">FFmpeg</div>
        <div class="flow-node-name">final video</div>
        <div class="flow-node-sub">9:16, sub-60s, H.264/AAC</div>
      </div>
    </div>
  </div>
</div>"""


def _render_pytest_log(log: str) -> str:
    """Apply minimal coloring to PASSED/FAILED/SKIPPED tokens."""
    out = _esc(log)
    out = re.sub(r"\bPASSED\b", '<span class="ok">PASSED</span>', out)
    out = re.sub(r"\bFAILED\b", '<span class="err">FAILED</span>', out)
    out = re.sub(r"\bSKIPPED\b", '<span class="warn">SKIPPED</span>', out)
    out = re.sub(r"\bERROR\b", '<span class="err">ERROR</span>', out)
    return out


CSS = r"""
@import url('https://fonts.googleapis.com/css2?family=Fraunces:opsz,wght@9..144,400;9..144,500;9..144,600;9..144,700&family=Inter:wght@400;500;600;700&family=JetBrains+Mono:wght@400;500;600&display=swap');

:root {
  --bg: #fafaf7;
  --bg-elev: #ffffff;
  --bg-sunken: #f3f2ed;
  --ink: #14110d;
  --ink-2: #3a3631;
  --ink-3: #6b6660;
  --ink-4: #9a948c;
  --line: #e6e2dc;
  --line-strong: #d4cfc6;
  --accent: #8a3324;        /* oxblood */
  --accent-2: #b04a35;
  --accent-soft: #f3e7e3;
  --admin: #4a5d3a;         /* forest, used for admin-time badges */
  --admin-soft: #ecefe6;
  --job: #8a3324;
  --job-soft: #f3e7e3;
  --warn: #c98c2e;
  --code-bg: #1b1916;
  --code-ink: #e8e3da;
  --code-key: #d8a657;
  --code-str: #a9b665;
  --code-com: #7c6f64;
  --code-type: #e78a4e;
  --code-num: #d3869b;
  --code-self: #ea6962;

  --serif: "Fraunces", "Charter", "Iowan Old Style", "Apple Garamond", Georgia, serif;
  --sans: "Inter", system-ui, -apple-system, "Segoe UI", Roboto, sans-serif;
  --mono: "JetBrains Mono", "IBM Plex Mono", ui-monospace, "SF Mono", Menlo, monospace;

  --maxw: 1180px;
  --readw: 68ch;
  --r: 6px;
}

@media (prefers-color-scheme: dark) {
  :root {
    --bg: #100f0d;
    --bg-elev: #1a1916;
    --bg-sunken: #16140f;
    --ink: #f1ece2;
    --ink-2: #cdc7bc;
    --ink-3: #8a8278;
    --ink-4: #5e574e;
    --line: #2a2722;
    --line-strong: #3a362f;
    --accent: #d97757;
    --accent-2: #e78a6b;
    --accent-soft: #2a1a14;
    --admin: #95b07a;
    --admin-soft: #1c2418;
    --job: #d97757;
    --job-soft: #2a1a14;
    --warn: #d8a657;
    --code-bg: #0c0b09;
  }
}

* { box-sizing: border-box; }
html { scroll-behavior: smooth; }
body {
  margin: 0;
  background: var(--bg);
  color: var(--ink);
  font-family: var(--sans);
  font-size: 16px;
  line-height: 1.6;
  -webkit-font-smoothing: antialiased;
  -moz-osx-font-smoothing: grayscale;
  text-rendering: optimizeLegibility;
}

a { color: var(--accent); text-decoration: none; border-bottom: 1px solid transparent; transition: border-color .15s ease; }
a:hover { border-bottom-color: var(--accent); }
::selection { background: var(--accent); color: #fff; }

code, pre { font-family: var(--mono); font-feature-settings: 'tnum', 'zero'; }

/* Layout */
.wrap { max-width: var(--maxw); margin: 0 auto; padding: 0 28px; }
.read { max-width: var(--readw); }

/* ── TOPBAR ───────────────────────────────────────────────────── */
.topbar {
  position: sticky; top: 0; z-index: 50;
  background: color-mix(in oklab, var(--bg) 88%, transparent);
  backdrop-filter: saturate(140%) blur(10px);
  -webkit-backdrop-filter: saturate(140%) blur(10px);
  border-bottom: 1px solid var(--line);
}
.topbar-inner {
  max-width: var(--maxw); margin: 0 auto;
  display: flex; align-items: center; justify-content: space-between;
  padding: 12px 28px; gap: 24px;
}
.brand {
  font-family: var(--serif); font-weight: 600; font-size: 17px;
  letter-spacing: -0.01em; color: var(--ink);
}
.brand .dot { color: var(--accent); }
.nav { display: flex; gap: 22px; font-size: 13.5px; color: var(--ink-3); }
.nav button {
  background: transparent; border: none; padding: 0;
  font: inherit; color: var(--ink-3); cursor: pointer;
  border-bottom: 1px solid transparent;
  transition: color .15s ease, border-color .15s ease;
}
.nav button:hover { color: var(--ink); }
.nav button.active { color: var(--ink); border-bottom-color: var(--accent); }
.nav .live {
  display: inline-flex; align-items: center; gap: 6px;
  font-family: var(--mono); font-size: 11px; color: var(--admin);
  letter-spacing: 0.04em;
}
.nav .live::before {
  content: ""; width: 6px; height: 6px; border-radius: 50%;
  background: var(--admin); animation: livepulse 2s infinite;
}
@keyframes livepulse { 0%,100% { opacity: 1; } 50% { opacity: 0.35; } }
@media (max-width: 760px) { .nav button:not(.live) { display: none; } }

/* ── HERO ─────────────────────────────────────────────────────── */
.hero { padding: 88px 0 64px; border-bottom: 1px solid var(--line); }
.eyebrow {
  font-size: 12.5px; text-transform: uppercase; letter-spacing: 0.14em;
  color: var(--accent); font-weight: 600; margin-bottom: 22px;
}
h1 {
  font-family: var(--serif); font-weight: 500;
  font-size: clamp(36px, 5.4vw, 60px); line-height: 1.04;
  letter-spacing: -0.025em; margin: 0 0 24px; color: var(--ink);
  font-variation-settings: "opsz" 96;
}
h1 em { font-style: italic; font-weight: 400; color: var(--accent); }
.lede {
  font-family: var(--serif); font-size: 20px; line-height: 1.55;
  color: var(--ink-2); max-width: 62ch; font-variation-settings: "opsz" 24;
}
.meta {
  display: flex; flex-wrap: wrap; gap: 24px; margin-top: 36px;
  font-size: 13px; color: var(--ink-3);
  border-top: 1px solid var(--line); padding-top: 22px;
}
.meta dt { font-weight: 500; color: var(--ink-4); margin-right: 6px; display: inline; }
.meta dd { display: inline; margin: 0; color: var(--ink-2); font-family: var(--mono); font-size: 12.5px; }
.meta .meta-row { display: flex; align-items: baseline; gap: 4px; }

/* ── SECTIONS ─────────────────────────────────────────────────── */
section.tab { display: none; padding: 56px 0; border-bottom: 1px solid var(--line); }
section.tab.active { display: block; animation: fadeUp 0.35s ease-out; }
section.tab:last-of-type { border-bottom: none; }
@keyframes fadeUp { from { opacity: 0; transform: translateY(8px); } to { opacity: 1; transform: none; } }

.section-head { margin-bottom: 48px; }
.section-num {
  font-family: var(--mono); font-size: 11.5px; color: var(--ink-4);
  letter-spacing: 0.06em; margin-bottom: 14px;
}
h2 {
  font-family: var(--serif); font-weight: 500;
  font-size: clamp(28px, 3.4vw, 38px); line-height: 1.1;
  letter-spacing: -0.02em; margin: 0 0 16px; color: var(--ink);
  font-variation-settings: "opsz" 48;
}
h2 em { font-style: italic; color: var(--accent); font-weight: 400; }
.section-lede {
  font-family: var(--serif); font-size: 18px; line-height: 1.55;
  color: var(--ink-2); max-width: 64ch; font-variation-settings: "opsz" 22;
}

/* ── BADGES & PILLS ───────────────────────────────────────────── */
.badge {
  font-family: var(--mono); font-size: 10.5px; letter-spacing: 0.04em;
  text-transform: uppercase; padding: 3px 8px; border-radius: 3px;
  background: var(--bg-sunken); color: var(--ink-3);
  border: 1px solid var(--line); display: inline-block;
}
.badge.phase-admin { background: var(--admin-soft); color: var(--admin); border-color: color-mix(in oklab, var(--admin) 30%, transparent); }
.badge.phase-job   { background: var(--job-soft); color: var(--job); border-color: color-mix(in oklab, var(--job) 30%, transparent); }
.badge.llm         { background: transparent; color: var(--accent); border-color: color-mix(in oklab, var(--accent) 35%, transparent); }
.badge.py          { background: transparent; color: var(--ink-2); border-color: var(--line-strong); }
.badge.optional    { background: transparent; color: var(--ink-4); border-style: dashed; }
.badge.has-fixtures{ background: var(--admin-soft); color: var(--admin); border-color: color-mix(in oklab, var(--admin) 30%, transparent); }

.phase-legend { display: flex; gap: 22px; flex-wrap: wrap; margin-bottom: 28px; font-size: 13px; color: var(--ink-3); }
.phase-legend .swatch { display: inline-block; width: 14px; height: 14px; border-radius: 2px; vertical-align: -3px; margin-right: 8px; }
.phase-legend .swatch.admin { background: var(--admin); }
.phase-legend .swatch.job { background: var(--accent); }

/* ── FLOW (numbered rail with circles) ────────────────────────── */
.flow {
  display: grid; grid-template-columns: 60px 1fr; gap: 0;
  margin-top: 12px; position: relative;
}
.flow-step { display: contents; }
.flow-rail {
  border-right: 1px solid var(--line); position: relative; padding-top: 26px;
}
.flow-step:last-child .flow-rail { border-right: 1px dashed var(--line); }
.flow-num {
  position: absolute; top: 18px; left: -1px;
  width: 28px; height: 28px;
  background: var(--bg); border: 1px solid var(--line-strong);
  border-radius: 50%;
  display: flex; align-items: center; justify-content: center;
  font-family: var(--mono); font-size: 11.5px; font-weight: 600;
  color: var(--ink-2); transform: translateX(-50%);
}
.flow-step.is-llm .flow-num { background: var(--accent); color: #fff; border-color: var(--accent); }
.flow-step.is-py .flow-num { background: var(--bg); color: var(--ink); }
.flow-step.is-store .flow-num { background: var(--admin); color: #fff; border-color: var(--admin); }
.flow-body {
  padding: 14px 0 36px 28px; border-bottom: 1px solid var(--line);
}
.flow-step:last-child .flow-body { border-bottom: none; padding-bottom: 8px; }
.flow-title {
  font-family: var(--serif); font-size: 19px; font-weight: 500;
  letter-spacing: -0.01em; color: var(--ink); margin: 0 0 4px;
  font-variation-settings: "opsz" 22;
}
.flow-agent { font-family: var(--mono); font-size: 12px; color: var(--accent); margin-bottom: 10px; }
.flow-step.is-py .flow-agent { color: var(--admin); }
.flow-step.is-store .flow-agent { color: var(--admin); }
.flow-desc { font-size: 14.5px; color: var(--ink-2); line-height: 1.55; max-width: 64ch; }
.flow-io {
  margin-top: 12px; font-family: var(--mono); font-size: 11.5px;
  color: var(--ink-3); display: flex; flex-wrap: wrap; gap: 14px;
}
.flow-io .io-pair { display: inline-flex; align-items: center; gap: 6px; }
.flow-io .io-arrow { color: var(--ink-4); }
.flow-io .io-tag {
  background: var(--bg-sunken); border: 1px solid var(--line);
  border-radius: 4px; padding: 2px 7px; color: var(--ink-2);
}

/* ── AGENTS GRID ──────────────────────────────────────────────── */
.agents { display: grid; grid-template-columns: repeat(3, 1fr); gap: 18px; }
@media (max-width: 980px) { .agents { grid-template-columns: repeat(2, 1fr); } }
@media (max-width: 640px) { .agents { grid-template-columns: 1fr; } }

.agent-tile {
  background: var(--bg-elev); border: 1px solid var(--line);
  border-radius: var(--r); padding: 24px 22px;
  display: flex; flex-direction: column;
  position: relative; cursor: pointer;
  transition: border-color .15s, transform .15s;
}
.agent-tile:hover { border-color: var(--ink-4); transform: translateY(-1px); }
.agent-tile.is-admin { border-top: 3px solid var(--admin); }
.agent-tile.is-job { border-top: 3px solid var(--accent); }
.agent-tile.is-active { border-color: var(--accent); box-shadow: 0 4px 18px color-mix(in oklab, var(--accent) 14%, transparent); }
.agent-tile-num { font-family: var(--mono); font-size: 11px; color: var(--ink-4); margin-bottom: 6px; }
.agent-badges { display: flex; flex-wrap: wrap; gap: 6px; margin-bottom: 14px; }
.agent-name {
  font-family: var(--serif); font-size: 22px; line-height: 1.2; font-weight: 500;
  letter-spacing: -0.012em; color: var(--ink); margin: 0 0 4px;
  font-variation-settings: "opsz" 28;
}
.agent-cardinality { font-family: var(--mono); font-size: 11.5px; color: var(--ink-3); margin-bottom: 14px; }
.agent-what { font-size: 14px; line-height: 1.55; color: var(--ink-2); margin-bottom: 16px; flex: 1; }
.agent-meta-row {
  font-family: var(--mono); font-size: 11.5px; color: var(--ink-3);
  border-top: 1px solid var(--line); padding-top: 12px;
  display: flex; gap: 12px; flex-wrap: wrap;
}
.agent-meta-row span { display: inline-flex; align-items: center; gap: 6px; }
.agent-meta-row b { color: var(--ink-2); font-weight: 600; }

/* ── AGENT DETAIL (expanded panel below grid) ─────────────────── */
.agent-detail {
  display: none;
  margin-top: 28px;
  background: var(--bg-elev); border: 1px solid var(--line);
  border-top: 3px solid var(--accent);
  border-radius: var(--r);
  padding: 36px 36px 32px;
}
.agent-detail.active { display: block; animation: fadeUp 0.3s ease-out; }
.agent-detail header { margin-bottom: 24px; padding-bottom: 22px; border-bottom: 1px solid var(--line); }
.agent-detail header .crumb {
  font-family: var(--mono); font-size: 11px; color: var(--ink-4);
  letter-spacing: 0.06em; margin-bottom: 14px;
}
.agent-detail header h2 {
  font-family: var(--serif); font-weight: 500;
  font-size: clamp(28px, 3.4vw, 42px); line-height: 1.05;
  letter-spacing: -0.025em; color: var(--ink); margin: 0 0 12px;
  font-variation-settings: "opsz" 48;
  word-break: break-word;
}
.agent-detail header h2 em { font-style: italic; color: var(--accent); font-weight: 400; }
.agent-detail header .ns {
  font-family: var(--mono); font-size: 13px; color: var(--ink-3);
  letter-spacing: 0.02em;
}
.agent-detail header .badges { margin-top: 14px; display: flex; flex-wrap: wrap; gap: 8px; }
.agent-detail h3 {
  font-family: var(--mono); font-size: 11px; font-weight: 600;
  color: var(--ink-3); text-transform: uppercase; letter-spacing: 0.12em;
  margin: 32px 0 14px; display: flex; align-items: center; gap: 10px;
}
.agent-detail h3::after { content: ""; flex: 1; height: 1px; background: var(--line); }
.agent-detail h3 .num { color: var(--accent); font-weight: 700; }

/* ── KEY/VALUE GRID ───────────────────────────────────────────── */
.kv-grid {
  display: grid; grid-template-columns: repeat(auto-fill, minmax(180px, 1fr));
  gap: 18px 24px; padding: 20px 0;
  border-top: 1px solid var(--line); border-bottom: 1px solid var(--line);
}
.kv-grid > div { display: flex; flex-direction: column; gap: 4px; }
.kv-grid dt {
  color: var(--ink-4); font-family: var(--mono); font-size: 10px;
  text-transform: uppercase; letter-spacing: 0.12em; font-weight: 500;
}
.kv-grid dd {
  margin: 0; font-family: var(--serif); font-size: 17px; line-height: 1.25;
  font-weight: 500; color: var(--ink); letter-spacing: -0.005em;
  font-variation-settings: "opsz" 22;
  overflow: hidden; text-overflow: ellipsis;
}
.kv-grid dd code, .kv-grid dd .mono {
  font-family: var(--mono); font-size: 13px; color: var(--ink); font-weight: 400;
  font-variation-settings: normal;
}

/* ── DETAILS / DISCLOSURE ─────────────────────────────────────── */
details {
  background: var(--bg-elev); border-radius: var(--r);
  border: 1px solid var(--line); overflow: hidden;
}
details > summary {
  padding: 12px 18px; cursor: pointer; user-select: none;
  font-family: var(--mono); font-size: 12px; font-weight: 500;
  color: var(--ink-2);
  letter-spacing: 0.02em;
  list-style: none; display: flex; align-items: center; gap: 10px;
  background: var(--bg-sunken);
}
details > summary::-webkit-details-marker { display: none; }
details > summary::before {
  content: "+"; color: var(--accent); font-weight: 700; width: 12px;
  font-family: var(--mono); transition: transform .15s;
}
details[open] > summary::before { content: "−"; }
details[open] > summary { color: var(--ink); border-bottom: 1px solid var(--line); }
details > *:not(summary) { padding: 18px; }
details pre { margin: 0; border-radius: 0; padding: 18px; }
details table { padding: 0; }
details + details { margin-top: 10px; }

/* ── TABLES (slot tables, schemas) ────────────────────────────── */
table.slots { width: 100%; border-collapse: collapse; font-family: var(--mono); font-size: 12.5px; }
table.slots th, table.slots td {
  padding: 10px 14px; text-align: left;
  border-bottom: 1px solid var(--line);
}
table.slots th {
  color: var(--ink-4); font-weight: 600; text-transform: uppercase;
  font-size: 10px; letter-spacing: 0.12em; background: var(--bg-sunken);
}
table.slots td.num { text-align: right; font-variant-numeric: tabular-nums; color: var(--ink); }
table.slots tr:last-child td { border-bottom: none; }
table.slots td code { color: var(--accent); }

/* ── PROMPT FILE / NOTES ──────────────────────────────────────── */
.prompt-note {
  background: var(--bg-elev); border: 1px solid var(--line);
  border-left: 3px solid var(--admin); border-radius: var(--r);
  padding: 16px 20px; margin: 0 0 14px;
  font-family: var(--serif); font-style: italic; font-variation-settings: "opsz" 20;
  font-size: 15.5px; line-height: 1.55; color: var(--ink-2);
}
.prompt-note b {
  color: var(--admin); font-style: normal;
  font-family: var(--mono); font-size: 11px;
  text-transform: uppercase; letter-spacing: 0.12em;
  font-weight: 600; margin-right: 8px;
}
.prompt-note code { font-family: var(--mono); font-size: 13px; color: var(--ink); font-style: normal; }

/* Prompt file content (the .txt file body) — wrap long lines */
details pre {
  background: var(--code-bg); color: var(--code-ink);
  font-size: 12.5px; line-height: 1.65;
  white-space: pre-wrap;
  word-wrap: break-word;
  overflow-wrap: anywhere;
  overflow-x: hidden;
}

/* ── PYTHON SOURCE BLOCK ──────────────────────────────────────── */
.py-source {
  background: var(--code-bg); border-radius: var(--r);
  padding: 22px 26px; margin: 0;
  font-family: var(--mono); font-size: 12.5px; line-height: 1.65;
  color: var(--code-ink);
  white-space: pre-wrap;
  word-wrap: break-word;
  overflow-wrap: anywhere;
  overflow-x: hidden;
  position: relative; border: 1px solid color-mix(in oklab, var(--code-bg) 80%, var(--line));
}
.py-source::before {
  content: attr(data-source); position: absolute; top: 8px; right: 14px;
  font-size: 10px; color: var(--code-com); letter-spacing: 0.14em;
  text-transform: uppercase;
}
.py-kw   { color: var(--code-key); font-weight: 500; }
.py-str  { color: var(--code-str); }
.py-com  { color: var(--code-com); font-style: italic; }
.py-fn   { color: var(--code-type); font-weight: 500; }
.py-bi   { color: var(--code-num); }
.py-self { color: var(--code-self); font-style: italic; }
.py-dec  { color: var(--code-key); }

/* ── CREATIVE DIRECTION TEXT ──────────────────────────────────── */
.cd-block { background: var(--bg-elev); border-radius: var(--r); border: 1px solid var(--line); }
.cd-block.dim { padding: 18px; color: var(--ink-4); font-family: var(--serif); font-style: italic; font-variation-settings: "opsz" 20; }
.cd-text {
  position: relative;
  border-left: 3px solid var(--accent); padding: 22px 26px;
  background: var(--bg-elev);
  font-family: var(--serif); font-size: 17px; line-height: 1.65;
  color: var(--ink-2); white-space: pre-wrap;
  font-variation-settings: "opsz" 22; font-weight: 400;
}

/* ── PER-TEMPLATE OUTPUT PICKER ───────────────────────────────── */
.agent-output-picker {
  display: flex; align-items: center; gap: 18px; margin-bottom: 14px;
  padding: 16px 20px; background: var(--bg-sunken);
  border-radius: var(--r); border: 1px solid var(--line);
}
.agent-output-picker label {
  font-family: var(--mono); font-size: 11px; color: var(--ink-3);
  text-transform: uppercase; letter-spacing: 0.1em;
  display: flex; align-items: center; gap: 12px;
}
.agent-output-picker select {
  background: var(--bg); border: 1px solid var(--line-strong); color: var(--ink);
  padding: 8px 12px; border-radius: 4px;
  font-family: var(--mono); font-size: 13px;
  text-transform: none; letter-spacing: 0;
  cursor: pointer; min-width: 280px;
}
.agent-output-picker select:focus { outline: none; border-color: var(--accent); }
.agent-output-picker .dim { font-family: var(--mono); font-size: 11px; color: var(--ink-4); margin-left: auto; }
.agent-output-panel { margin-top: 14px; padding: 24px; background: var(--bg-elev); border-radius: var(--r); border: 1px solid var(--line); }
.agent-output-panel h4 {
  margin: 0 0 18px; font-family: var(--serif); font-weight: 500;
  font-size: 22px; line-height: 1.2; letter-spacing: -0.015em;
  font-variation-settings: "opsz" 26;
}
.agent-output-panel h4 .dim { font-family: var(--mono); font-size: 12px; font-weight: 400; color: var(--ink-4); margin-left: 8px; font-variation-settings: normal; }
.agent-output-panel > details { margin-top: 12px; }

/* ── TEMPLATE CARDS (Templates tab) ───────────────────────────── */
.cards { display: grid; gap: 18px; grid-template-columns: 1fr; }
@media (min-width: 1000px) { .cards { grid-template-columns: 1fr 1fr; } }

.card {
  background: var(--bg-elev); border: 1px solid var(--line);
  border-radius: var(--r); padding: 24px;
  display: flex; flex-direction: column; gap: 18px;
  transition: border-color 0.2s, transform 0.2s;
}
.card:hover { border-color: var(--ink-4); }
.card.hidden { display: none; }
.card > header { display: flex; flex-direction: column; gap: 10px; }
.card h3 {
  margin: 0; font-family: var(--serif); font-weight: 500;
  font-variation-settings: "opsz" 28;
  font-size: 24px; line-height: 1.15; letter-spacing: -0.015em; color: var(--ink);
}
.card-meta { display: flex; flex-wrap: wrap; gap: 8px; align-items: center; font-size: 11.5px; }
.card-meta .slug {
  color: var(--ink-4); font-family: var(--mono);
  text-transform: lowercase; letter-spacing: 0.02em;
  padding-right: 8px; border-right: 1px solid var(--line);
}
.card-meta .status {
  font-family: var(--mono); padding: 3px 9px; border-radius: 3px;
  font-size: 10px; text-transform: uppercase; letter-spacing: 0.1em; font-weight: 600;
}
.status-passed { color: var(--admin); background: var(--admin-soft); border: 1px solid color-mix(in oklab, var(--admin) 30%, transparent); }
.status-failed { color: var(--accent); background: var(--accent-soft); border: 1px solid color-mix(in oklab, var(--accent) 30%, transparent); }
.status-skipped { color: var(--warn); background: color-mix(in oklab, var(--warn) 14%, var(--bg)); border: 1px solid color-mix(in oklab, var(--warn) 30%, transparent); }
.status-—, .status-unknown { color: var(--ink-4); }

.tag {
  font-size: 10.5px; padding: 3px 8px; border-radius: 3px;
  font-family: var(--mono); font-weight: 500;
  text-transform: uppercase; letter-spacing: 0.06em;
  border: 1px solid var(--line); color: var(--ink-3);
  background: var(--bg-sunken);
}
.tag.warn { color: var(--warn); border-color: color-mix(in oklab, var(--warn) 30%, transparent); background: color-mix(in oklab, var(--warn) 10%, var(--bg)); }
.tag.err { color: var(--accent); border-color: color-mix(in oklab, var(--accent) 30%, transparent); background: var(--accent-soft); }
.tag.tpl { color: var(--admin); border-color: color-mix(in oklab, var(--admin) 30%, transparent); background: var(--admin-soft); }

/* ── TOOLBAR (Templates tab) ──────────────────────────────────── */
.toolbar { display: flex; gap: 10px; margin-bottom: 28px; align-items: center; flex-wrap: wrap; }
.toolbar input {
  background: var(--bg-elev); border: 1px solid var(--line); color: var(--ink);
  padding: 10px 14px; border-radius: 4px; width: 360px;
  font-family: var(--mono); font-size: 13px;
}
.toolbar input::placeholder { color: var(--ink-4); }
.toolbar input:focus { outline: none; border-color: var(--accent); }
.toolbar .filter-pill {
  background: transparent; border: 1px solid var(--line); color: var(--ink-3);
  padding: 8px 14px; border-radius: 4px; font-size: 11px; cursor: pointer;
  font-family: var(--mono); text-transform: uppercase; letter-spacing: 0.08em;
  transition: all 0.15s ease;
}
.toolbar .filter-pill:hover { color: var(--ink); border-color: var(--ink-4); }
.toolbar .filter-pill.active { color: #fff; border-color: var(--accent); background: var(--accent); }

/* ── ISSUES (decision callout style) ──────────────────────────── */
.issue {
  background: var(--bg-elev); border: 1px solid var(--line);
  border-left: 3px solid var(--ink-4);
  border-radius: var(--r); padding: 22px 26px; margin-bottom: 14px;
}
.issue.sev-high { border-left-color: var(--accent); }
.issue.sev-med { border-left-color: var(--warn); }
.issue.sev-low { border-left-color: var(--ink-4); }
.issue header { display: flex; align-items: center; gap: 14px; margin-bottom: 10px; }
.issue h3 {
  margin: 0; font-family: var(--serif); font-weight: 500;
  font-variation-settings: "opsz" 26;
  font-size: 21px; line-height: 1.2; letter-spacing: -0.015em; color: var(--ink);
}
.issue .badge.sev {
  font-size: 10px; padding: 3px 9px; border-radius: 3px;
  font-family: var(--mono); font-weight: 700;
  letter-spacing: 0.12em; text-transform: uppercase;
}
.issue .badge.sev-high { background: var(--accent-soft); color: var(--accent); border: 1px solid color-mix(in oklab, var(--accent) 30%, transparent); }
.issue .badge.sev-med { background: color-mix(in oklab, var(--warn) 12%, var(--bg)); color: var(--warn); border: 1px solid color-mix(in oklab, var(--warn) 30%, transparent); }
.issue .badge.sev-low { background: var(--bg-sunken); color: var(--ink-3); border: 1px solid var(--line); }
.issue .count { color: var(--ink-4); font-size: 11px; font-family: var(--mono); margin-left: auto; text-transform: uppercase; letter-spacing: 0.06em; }
.issue p { margin: 6px 0; font-size: 14.5px; line-height: 1.55; color: var(--ink-2); }
.issue p.fix { color: var(--ink-3); font-family: var(--serif); font-style: italic; font-variation-settings: "opsz" 20; }
.issue p.fix b { color: var(--accent); font-weight: 600; font-style: normal; font-family: var(--mono); font-size: 11px; text-transform: uppercase; letter-spacing: 0.1em; }
.issue-templates { margin-top: 12px; display: flex; flex-wrap: wrap; gap: 5px; }

/* ── COMMANDS ─────────────────────────────────────────────────── */
.commands {
  background: var(--code-bg); color: var(--code-ink);
  border-radius: var(--r); padding: 18px 22px; margin-top: 16px;
  border: 1px solid color-mix(in oklab, var(--code-bg) 70%, var(--line));
}
.commands h4 {
  margin: 0 0 12px; font-family: var(--mono); font-size: 11px;
  color: var(--code-com); text-transform: uppercase; letter-spacing: 0.12em;
  font-weight: 500;
}
.commands code {
  display: block; padding: 4px 0; font-size: 12.5px;
  color: var(--code-ink); font-family: var(--mono);
  white-space: pre-wrap; word-break: break-all;
}
.commands code::before { content: "$ "; color: var(--code-key); }
.commands code.dim::before { content: "# "; }
.commands code.dim { color: var(--code-com); font-style: italic; }

/* ── PYTEST LOG ───────────────────────────────────────────────── */
.pytest-log {
  background: var(--code-bg); color: var(--code-ink);
  border-radius: var(--r); padding: 24px;
  max-height: 80vh; overflow: auto;
  font-family: var(--mono); font-size: 12.5px; line-height: 1.65;
  white-space: pre; border: 1px solid color-mix(in oklab, var(--code-bg) 70%, var(--line));
}
.pytest-log .ok { color: var(--code-str); font-weight: 600; }
.pytest-log .warn { color: var(--code-key); font-weight: 600; }
.pytest-log .err { color: var(--accent-2); font-weight: 600; }

/* ── FOOTER ───────────────────────────────────────────────────── */
footer {
  padding: 56px 0 80px; border-top: 1px solid var(--line);
  color: var(--ink-3); font-size: 13px;
  margin-top: 64px;
}
footer .foot-grid { display: grid; grid-template-columns: 1fr auto; gap: 20px; align-items: end; }
@media (max-width: 640px) { footer .foot-grid { grid-template-columns: 1fr; } }
footer .source-label { font-size: 11px; text-transform: uppercase; letter-spacing: 0.08em; color: var(--ink-4); margin-bottom: 6px; font-family: var(--mono); }
footer .source { font-family: var(--mono); font-size: 12px; color: var(--ink-3); word-break: break-all; }

/* ── DIM HELPER ───────────────────────────────────────────────── */
.dim { color: var(--ink-4); }

@media (max-width: 760px) {
  .topbar-inner, .wrap { padding-left: 20px; padding-right: 20px; }
  section.tab { padding: 56px 0; }
  .hero { padding: 64px 0 48px; }
}
"""

JS = r"""
// ── Top-level tabs ───────────────────────────────────────────────
const tabs = document.querySelectorAll('nav.nav button[data-tab]');
const sections = document.querySelectorAll('section.tab');
tabs.forEach(btn => {
  btn.addEventListener('click', () => {
    tabs.forEach(b => b.classList.remove('active'));
    sections.forEach(s => s.classList.remove('active'));
    btn.classList.add('active');
    const target = document.getElementById(btn.dataset.tab);
    if (target) target.classList.add('active');
    window.scrollTo({ top: 0, behavior: 'smooth' });
  });
});

// ── Templates tab: filter ──────────────────────────────────────
const search = document.getElementById('search');
const cards = document.querySelectorAll('.card');
const filterPills = document.querySelectorAll('.filter-pill');
let activeFilter = 'all';

function applyFilters() {
  const q = (search?.value || '').toLowerCase().trim();
  cards.forEach(card => {
    const name = card.dataset.name || '';
    const slug = card.dataset.slug || '';
    const matchesQ = !q || name.includes(q) || slug.includes(q);
    let matchesFilter = true;
    if (activeFilter === 'no-niche') matchesFilter = /no niche/.test(card.innerText);
    if (activeFilter === 'no-cd') matchesFilter = /no creative_direction/.test(card.innerText);
    if (activeFilter === 'stale') matchesFilter = /stale schema/.test(card.innerText);
    card.classList.toggle('hidden', !(matchesQ && matchesFilter));
  });
}
if (search) search.addEventListener('input', applyFilters);
filterPills.forEach(p => p.addEventListener('click', () => {
  filterPills.forEach(x => x.classList.remove('active'));
  p.classList.add('active');
  activeFilter = p.dataset.filter;
  applyFilters();
}));

// ── Agents tab: tile → detail panel ─────────────────────────────
const agentTiles = document.querySelectorAll('.agent-tile');
const agentDetails = document.querySelectorAll('.agent-detail');

function activateAgent(targetId) {
  agentTiles.forEach(t => t.classList.toggle('is-active', t.dataset.target === targetId));
  agentDetails.forEach(d => d.classList.toggle('active', d.id === targetId));
  const panel = document.getElementById(targetId);
  if (panel) {
    panel.scrollIntoView({ behavior: 'smooth', block: 'start' });
  }
}
agentTiles.forEach(tile => {
  tile.addEventListener('click', () => activateAgent(tile.dataset.target));
});

// flow-agent links jump to the corresponding detail panel
document.querySelectorAll('.flow-agent a[data-target]').forEach(a => {
  a.addEventListener('click', (e) => {
    e.preventDefault();
    activateAgent(a.dataset.target);
  });
});

// ── Per-template output picker (inside each agent-detail) ──────
document.querySelectorAll('.output-select').forEach(sel => {
  const card = sel.closest('.agent-detail');
  const panels = card.querySelectorAll('.agent-output-panel');
  function show(slug) {
    panels.forEach(p => p.hidden = (p.dataset.slug !== slug));
  }
  sel.addEventListener('change', () => show(sel.value));
  if (sel.options.length) show(sel.options[0].value);
});

// ── Live-reload heartbeat ──────────────────────────────────────
// When the build script runs in --watch mode, it rewrites the dashboard's
// data-build attribute on every regen. We poll for the file's modification
// via fetch HEAD; if it changed since first load, we soft-reload.
(function liveReload() {
  const myBuild = document.body.dataset.build;
  const indicator = document.getElementById('live-indicator');
  const url = location.pathname + '?_=' + Date.now();
  let consecutiveFails = 0;

  async function check() {
    try {
      const r = await fetch(location.pathname + '?_t=' + Date.now(), { method: 'GET', cache: 'no-store' });
      if (!r.ok) throw new Error(r.status);
      const text = await r.text();
      const match = text.match(/<body data-build="([^"]+)"/);
      if (match && match[1] !== myBuild) {
        if (indicator) {
          indicator.textContent = 'reloading';
          indicator.style.color = 'var(--accent)';
        }
        // Preserve which tab + which agent the user was on
        const activeTab = document.querySelector('nav.nav button.active')?.dataset.tab;
        const activeAgent = document.querySelector('.agent-tile.is-active')?.dataset.target;
        if (activeTab) sessionStorage.setItem('nova-eval-tab', activeTab);
        if (activeAgent) sessionStorage.setItem('nova-eval-agent', activeAgent);
        location.reload();
      }
      consecutiveFails = 0;
    } catch (e) {
      consecutiveFails++;
      if (indicator && consecutiveFails > 3) {
        indicator.textContent = 'offline';
        indicator.style.color = 'var(--ink-4)';
      }
    }
  }
  // Restore prior tab/agent if reloaded
  const restoreTab = sessionStorage.getItem('nova-eval-tab');
  const restoreAgent = sessionStorage.getItem('nova-eval-agent');
  if (restoreTab) {
    sessionStorage.removeItem('nova-eval-tab');
    const btn = document.querySelector(`nav.nav button[data-tab="${restoreTab}"]`);
    if (btn) btn.click();
  }
  if (restoreAgent) {
    sessionStorage.removeItem('nova-eval-agent');
    setTimeout(() => activateAgent(restoreAgent), 50);
  }
  setInterval(check, 2000);
})();
"""


def render_dashboard() -> str:
    template_recipe_fxs = _load_fixtures("template_recipe")
    creative_direction_fxs = _load_fixtures("creative_direction")
    cd_by_slug = {f["_slug"]: f for f in creative_direction_fxs}
    log = _maybe_run_pytest()
    test_summary = _parse_pytest(log)

    # Build lookup: (agent, fixture_id) → status
    fixture_test_status: dict[tuple[str, str], str] = {}
    for name, status in test_summary["fixtures"]:
        # name shape: "test_template_recipe_evals.py::prod_snapshots/foo"
        m = re.match(r"test_(\w+)_evals\.py::(.+)", name)
        if m:
            fixture_test_status[(m.group(1), m.group(2))] = status

    issues = _detect_issues(template_recipe_fxs, creative_direction_fxs)

    # Build agents tab data — load every fixture-dir we know about.
    agent_catalog = _load_agent_catalog()
    fixtures_by_agent_name: dict[str, list[dict]] = {
        FIXTURE_AGENT_NAME["template_recipe"]: template_recipe_fxs,
        FIXTURE_AGENT_NAME["creative_direction"]: creative_direction_fxs,
    }
    for fixture_dir in (
        "clip_metadata",
        "transcript",
        "platform_copy",
        "audio_template",
        "song_classifier",
    ):
        fixtures_by_agent_name[FIXTURE_AGENT_NAME[fixture_dir]] = _load_fixtures(fixture_dir)

    # Sort agents: ones with fixtures first, then the rest
    def _sort_key(a: dict) -> tuple[int, str]:
        has_fxs = bool(fixtures_by_agent_name.get(a["name"]))
        return (0 if has_fxs else 1, a["name"])

    agent_catalog.sort(key=_sort_key)

    agent_tiles_html = []
    agent_detail_html = []
    for i, agent in enumerate(agent_catalog, start=1):
        agent_tiles_html.append(_render_agent_tile(i, agent, fixtures_by_agent_name))
        card_html = _render_agent_card(agent, fixtures_by_agent_name)
        # The renderer produces <article id="agent-..." class="agent-card">. Re-class it
        # to match the new "agent-detail" component, and make the first one active.
        card_html = card_html.replace('class="agent-card"', 'class="agent-detail"', 1)
        if i == 1:
            card_html = card_html.replace('class="agent-detail"', 'class="agent-detail active"', 1)
        agent_detail_html.append(card_html)

    flow_steps_html = _render_flow_steps(agent_catalog)

    cards_html = "\n".join(
        _render_template_card(tr, cd_by_slug.get(tr["_slug"]), fixture_test_status)
        for tr in template_recipe_fxs
    )
    if not cards_html:
        cards_html = '<p class="dim">No template_recipe fixtures yet. Run <code>scripts/export_eval_fixtures.py</code>.</p>'

    issues_html = (
        "\n".join(_render_issue(i) for i in issues)
        or '<p class="dim">No issues detected — every template passes the structural floor.</p>'
    )

    pytest_log_html = _render_pytest_log(log)

    n_templates = len(template_recipe_fxs)
    n_pass = test_summary["pass_count"]
    n_fail = test_summary["fail_count"]
    n_skip = test_summary["skip_count"]
    duration = test_summary["duration_s"]
    ts = datetime.now().strftime("%Y-%m-%d %H:%M")
    n_issues = len(issues)
    high_sev = sum(1 for i in issues if i["severity"] == "high")

    n_total_tests = n_pass + n_fail + n_skip
    n_with_fixtures = sum(1 for a in agent_catalog if fixtures_by_agent_name.get(a["name"]))

    return f"""<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <title>Nova · Agent Evals</title>
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <link rel="preconnect" href="https://fonts.googleapis.com">
  <link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>
  <style>{CSS}</style>
</head>
<body data-build="{ts}">
  <header class="topbar">
    <div class="topbar-inner">
      <div class="brand">Nova<span class="dot"> · </span>Agent Evals</div>
      <nav class="nav">
        <button class="active" data-tab="overview">Overview</button>
        <button data-tab="agents">Agents</button>
        <button data-tab="ai-variants">AI Variants <span style="display:inline-block;margin-left:4px;padding:1px 6px;border-radius:8px;background:var(--accent-soft);color:var(--accent);font-size:10px;font-weight:600;letter-spacing:.04em;vertical-align:1px;">PLANNED</span></button>
        <button data-tab="templates">Templates</button>
        <button data-tab="issues">Issues</button>
        <button data-tab="tests">Tests</button>
        <span class="live" id="live-indicator">live</span>
      </nav>
    </div>
  </header>

  <main class="wrap">
    <section id="overview" class="tab active">
      <div class="hero" style="border:none; padding-top:64px;">
        <div class="eyebrow">Issue 02 · 2026-05-15 · agentic templates land</div>
        <h1>Inspect every <em>agent</em> in the pipeline.</h1>
        <p class="lede">
          Twelve agents shape every Nova video. This dashboard reads their stored outputs
          across {n_templates} production templates — prompts, schemas, fixtures, tests — so
          quality regressions are visible before they ship. Today the <em>agentic</em>
          template-build path graduates from shadow mode: <code>text_designer</code> and
          <code>clip_router</code> are wired into production for templates flagged
          <code>is_agentic=true</code>.
        </p>
        <dl class="meta">
          <div class="meta-row"><dt>Templates</dt><dd>{n_templates} analyzed · agentic flag live</dd></div>
          <div class="meta-row"><dt>Agents</dt><dd>{len(agent_catalog)} ({n_with_fixtures} with fixtures · 2 newly in prod)</dd></div>
          <div class="meta-row"><dt>Tests</dt><dd>{n_pass}/{n_total_tests} passing · {duration:.2f}s · new e2e eval in CI</dd></div>
          <div class="meta-row"><dt>Issues</dt><dd>{n_issues} detected · {high_sev} high</dd></div>
          <div class="meta-row"><dt>Source</dt><dd>VideoTemplate.recipe_cached · VideoTemplate.is_agentic · JobClip · TemplateRecipeVersion</dd></div>
          <div class="meta-row"><dt>Landed today</dt><dd>PR #135 · PR #136 · PR #137 · PR #142</dd></div>
        </dl>
      </div>

      <div class="section-head" style="margin-top: 48px;">
        <div class="section-num">§ 00 · Landed 2026-05-15</div>
        <h2>The agentic-templates <em>cut-over</em>.</h2>
        <p class="section-lede">Four PRs reshape how templates are built and how their quality is measured. The shorthand: <strong>edit a prompt → CI auto-runs the e2e eval with judge → see score delta on the PR → decide before merge.</strong> This replaces the old loop of hand-editing recipes in the visual overlay editor.</p>
      </div>

      <div class="commands">
        <h4>PR #135 · <span class="dim">merged</span> · <code>is_agentic</code> flag on VideoTemplate</h4>
        <code class="dim">Admin UI: green Agentic badge · All/Manual/Agentic filter · checkbox on create form · read-only banner on visual overlay editor for agentic templates.</code>
        <code class="dim">Backend: PUT /admin/templates/{{id}}/recipe returns 409 for agentic rows.</code>
      </div>
      <div class="commands">
        <h4>PR #136 · <span class="dim">open</span> · agentic build orchestrator</h4>
        <code class="dim">New tasks.agentic_template_build_task runs Big 3 + per-slot text_designer.</code>
        <code class="dim">Job-time branch in template_orchestrate.py skips static _LABEL_CONFIG and calls clip_router instead of the greedy matcher.</code>
        <code class="dim">New agentic_matcher module with greedy fallback. Manual templates produce byte-identical output (locked by regression test).</code>
      </div>
      <div class="commands">
        <h4>PR #137 · <span class="dim">open · stacked on #136</span> · end-to-end eval</h4>
        <code>tests/evals/test_agentic_template_e2e.py</code>
        <code class="dim">10 structural assertions + LLM judge against new rubric: overlay_styling_coherence, transition_pacing_fit, beat_snap_realism, first_slot_hook_design.</code>
        <code class="dim">CI auto-trigger on agentic-stack prompt/source changes via .github/workflows/agent-evals.yml.</code>
      </div>
      <div class="commands">
        <h4>PR #142 · <span class="dim">open · independent hygiene</span> · alembic bootstrap</h4>
        <code class="dim">Commits the previously-untracked 0000_initial_schema.py bootstrap migration.</code>
        <code class="dim">Chains 0001_add_waitlist_signups onto it so alembic has a single head. Fixes fresh-DB bootstrap on every new machine.</code>
      </div>

      <div class="section-head" style="margin-top: 48px;">
        <div class="section-num">§ 01 · Local commands</div>
        <h2>Run, refresh, <em>iterate</em>.</h2>
        <p class="section-lede">Every artifact in this dashboard is regenerable in one command. The structural eval suite is free and offline. Quality scoring (judge mode) and live Gemini calls are opt-in.</p>
      </div>

      <div class="commands">
        <h4>regenerate this dashboard</h4>
        <code>cd src/apps/api && .venv/bin/python scripts/build_eval_dashboard.py</code>
      </div>
      <div class="commands">
        <h4>watch fixtures + agent code, auto-rebuild</h4>
        <code>cd src/apps/api && .venv/bin/python scripts/build_eval_dashboard.py --watch</code>
        <code class="dim">browser auto-reloads every time fixtures change</code>
      </div>
      <div class="commands">
        <h4>refresh fixtures from your local DB</h4>
        <code>set -a && source ../../.env && set +a</code>
        <code>cd src/apps/api && .venv/bin/python scripts/export_eval_fixtures.py</code>
      </div>
      <div class="commands">
        <h4>structural eval suite (replay mode, no network, free)</h4>
        <code>cd src/apps/api && .venv/bin/pytest tests/evals/ -v</code>
      </div>
      <div class="commands">
        <h4>with Claude Sonnet 4.6 quality judge</h4>
        <code class="dim">one-time setup</code>
        <code>cd src/apps/api && .venv/bin/pip install -e ".[dev]"</code>
        <code>echo 'ANTHROPIC_API_KEY=sk-ant-…' >> ../../.env</code>
        <code class="dim">then</code>
        <code>set -a && source ../../.env && set +a && .venv/bin/pytest tests/evals/ -v --with-judge</code>
      </div>
    </section>

    <section id="agents" class="tab">
      <div class="section-head">
        <div class="section-num">§ 02 · The flow</div>
        <h2>Two stages, <em>twelve</em> agents.</h2>
        <p class="section-lede">Once at template onboarding (Pass 1 → Pass 2 → cached recipe). Then per job (clip analysis → matcher → render). Six agents have replay fixtures wired up to the eval suite (Phase 1: template_recipe, creative_direction, clip_metadata; Phase 2: transcript, platform_copy, audio_template). The four below them ship evals when they wire into the pipeline.</p>
      </div>

      {flow_steps_html}

      <div class="section-head" style="margin-top: 80px;">
        <div class="section-num">§ 03 · The catalog</div>
        <h2>Click any tile for the <em>full</em> spec.</h2>
        <p class="section-lede">Each agent gets a card showing its phase, model, cardinality, and one-line purpose. Click to expand below the grid: prompt files, render_prompt() source, input/output schemas, and per-template returns from real fixtures.</p>
      </div>

      <div class="phase-legend">
        <span><span class="swatch admin"></span>Admin · runs once at template onboarding</span>
        <span><span class="swatch job"></span>Per-job · runs every render</span>
      </div>

      <div class="agents">
        {"".join(agent_tiles_html)}
      </div>

      <div id="agent-detail-region">
        {"".join(agent_detail_html)}
      </div>
    </section>

    <section id="ai-variants" class="tab">
      <div class="section-head">
        <div class="section-num">§ — Parallel system · planned, not yet built</div>
        <h2>A second flow lives <em>alongside</em> this one.</h2>
        <p class="section-lede">
          The agents on the <strong>Agents</strong> tab replace hardcoded heuristics inside today's
          template-pick pipeline. Separately, a new product is in design: the user uploads clips and
          immediately sees a gallery of finished variants — no template-pick step. That flow is its
          own stack of agents under <code>app/ai_agents/</code> (planned, not yet built). It does not
          modify, extend, or import from <code>app/agents/</code>. The two systems share infrastructure
          (Postgres, Redis, Celery worker, GCS) and the FFmpeg renderer; nothing else.
        </p>
        <p class="section-lede" style="margin-top:8px;color:var(--ink-3);">
          Architecture spec: <code>~/.claude/plans/the-app-is-template-deep-octopus.md</code> ·
          Visualization: <code>~/.gstack/projects/emirerben-nova/designs/ai-variant-architecture-20260510/finalized.html</code>
        </p>
      </div>

      <div style="display:grid;grid-template-columns:1fr 1fr;gap:18px;margin:24px 0 40px;">
        <div style="border:1px solid var(--line);border-radius:var(--r);padding:18px 20px;background:var(--bg-elev);">
          <div style="font-family:var(--mono);font-size:11px;color:var(--ink-3);letter-spacing:.06em;text-transform:uppercase;margin-bottom:6px;">This dashboard's tab · Agents</div>
          <div style="font-family:var(--serif);font-size:18px;line-height:1.3;color:var(--ink);margin-bottom:6px;">May-9 agent platform</div>
          <div style="font-size:13.5px;line-height:1.55;color:var(--ink-2);">
            12 agents · runtime + registry + model-client · {n_pass} passing eval tests.
            Lives in <code>app/agents/</code>. <strong>Shipped.</strong> Untouched by the new flow.
          </div>
        </div>
        <div style="border:1.5px dashed var(--accent);border-radius:var(--r);padding:18px 20px;background:var(--accent-soft);">
          <div style="font-family:var(--mono);font-size:11px;color:var(--accent);letter-spacing:.06em;text-transform:uppercase;margin-bottom:6px;">This tab · AI Variants</div>
          <div style="font-family:var(--serif);font-size:18px;line-height:1.3;color:var(--ink);margin-bottom:6px;">AI-variant gallery</div>
          <div style="font-size:13.5px;line-height:1.55;color:var(--ink-2);">
            6 agents · its own minimal runtime · its own eval scaffolding.
            Will live in <code>app/ai_agents/</code>. <strong>Planned.</strong> Zero code yet.
          </div>
        </div>
      </div>

      <div class="section-head" style="margin-top: 40px;">
        <div class="section-num">§ — The new agents</div>
        <h2>One admin role, four job-time agents, <em>one ranker</em>.</h2>
        <p class="section-lede">From the architecture plan. Tiles are dashed to mark them as planned, not implemented. Roles, contracts, and rationale are in the plan file linked above.</p>
      </div>

      <div class="phase-legend">
        <span><span class="swatch admin"></span>Admin · runs once at template onboarding</span>
        <span><span class="swatch job"></span>Per-job · runs every gallery generation</span>
      </div>

      <div class="agents">
        {"".join(_render_planned_ai_variant_tile(t) for t in _AI_VARIANT_PLANNED_AGENTS)}
      </div>

      <div style="margin-top:48px;padding:20px 24px;border:1px solid var(--line);border-radius:var(--r);background:var(--bg-sunken);">
        <div style="font-family:var(--mono);font-size:11px;color:var(--ink-3);letter-spacing:.06em;text-transform:uppercase;margin-bottom:10px;">Separation rule</div>
        <ul style="margin:0;padding-left:20px;font-size:13.5px;line-height:1.7;color:var(--ink-2);">
          <li>New agents in <code>app/ai_agents/</code>. New tables: <code>ai_template_cards</code>, <code>ai_jobs</code>, <code>ai_job_variants</code>. New routes: <code>/ai/jobs</code>. New eval scaffolding: <code>tests/ai_evals/</code>.</li>
          <li>The May-9 platform on the <strong>Agents</strong> tab is not modified, not extended, not imported. Its {n_pass} passing tests stay green.</li>
          <li>Only shared library: FFmpeg rendering primitives extracted to <code>app/lib/render/</code> (the one piece that is genuinely too expensive to duplicate). Both orchestrators call it.</li>
          <li>Three Gemini wrappers in the codebase will be intentional: legacy <code>gemini_analyzer.py</code>, May-9 <code>app/agents/_model_client.py</code>, new <code>app/ai_agents/_model_client.py</code>. The duplication is the price of isolation.</li>
        </ul>
      </div>
    </section>

    <section id="templates" class="tab">
      <div class="section-head">
        <div class="section-num">§ 04 · Production templates</div>
        <h2>Every template, <em>every recipe</em>.</h2>
        <p class="section-lede">The {n_templates} templates currently in your DB. Each card shows what <code>template_recipe</code> returned, with the slot table, interstitials, and full creative_direction text expandable below.</p>
      </div>

      <div class="toolbar">
        <input id="search" type="search" placeholder="filter by name or slug">
        <span class="filter-pill active" data-filter="all">All</span>
        <span class="filter-pill" data-filter="no-niche">Empty niche</span>
        <span class="filter-pill" data-filter="no-cd">Missing creative_direction</span>
        <span class="filter-pill" data-filter="stale">Stale schema</span>
      </div>
      <div class="cards">{cards_html}</div>
    </section>

    <section id="issues" class="tab">
      <div class="section-head">
        <div class="section-num">§ 05 · What the eval surfaced</div>
        <h2>Patterns the inspector <em>caught</em>.</h2>
        <p class="section-lede">Cross-template quality issues found in the stored recipes. None break structural assertions — all are real prompt-engineering targets that the LLM judge would catch.</p>
      </div>
      {issues_html}
    </section>

    <section id="tests" class="tab">
      <div class="section-head">
        <div class="section-num">§ 06 · Verbatim pytest output</div>
        <h2>{n_pass} pass, {n_fail} fail, <em>{n_skip} skip</em>.</h2>
        <p class="section-lede">Captured at the time this dashboard was generated. Run <code>pytest tests/evals/ -v</code> for a fresh check; pass <code>--with-judge</code> to score quality.</p>
      </div>
      <div class="pytest-log">{pytest_log_html}</div>
    </section>

    <footer>
      <div class="foot-grid">
        <div>
          <div class="source-label">Source of truth</div>
          <div class="source">VideoTemplate.recipe_cached · JobClip · TemplateRecipeVersion · prompts/*.txt · app/agents/</div>
        </div>
        <div>
          <div class="source-label">Generated</div>
          <div class="source">{ts}</div>
        </div>
      </div>
    </footer>
  </main>

  <script>{JS}</script>
</body>
</html>
"""


def _build_once() -> None:
    html_text = render_dashboard()
    DASHBOARD_FILE.write_text(html_text)


def _collect_mtimes() -> dict[str, float]:
    """Hash of paths to mtime for everything that should trigger a rebuild."""
    paths: list[Path] = []
    paths.extend(FIXTURES_ROOT.rglob("*.json"))
    paths.extend(PROMPTS_ROOT.rglob("*.txt"))
    paths.extend(AGENTS_ROOT.rglob("*.py"))
    paths.append(PYTEST_LOG)
    paths.append(Path(__file__))  # rebuild if the generator itself changes
    out: dict[str, float] = {}
    for p in paths:
        try:
            out[str(p)] = p.stat().st_mtime
        except OSError:
            pass
    return out


def _watch_loop(interval_s: float = 1.5) -> None:
    """Rebuild whenever fixtures, prompts, or agent code change. Ctrl-C to stop.

    If the build script ITSELF is edited, re-exec the process so the new code
    actually runs (Python doesn't auto-reimport its own source). The HTTP
    server, if any, is preserved by the re-exec.
    """
    import os
    import sys
    import time

    self_path = str(Path(__file__).resolve())
    print(f"[watch] watching for changes (every {interval_s}s) — Ctrl-C to stop")
    print(f"[watch] dashboard: {DASHBOARD_FILE}")
    last = _collect_mtimes()
    _build_once()
    print(f"[watch] initial build complete · {datetime.now():%H:%M:%S}")
    while True:
        try:
            time.sleep(interval_s)
            now = _collect_mtimes()
            if now != last:
                changed = sorted(
                    [p for p in now if last.get(p) != now.get(p)]
                    + [p for p in last if p not in now]
                )
                # If the build script itself changed, re-exec — Python won't
                # auto-pick up edits to functions defined in the running module.
                if self_path in changed:
                    print(f"[watch] {Path(self_path).name} changed — re-exec")
                    os.execv(sys.executable, [sys.executable, *sys.argv])
                short = [Path(p).name for p in changed[:3]]
                more = "" if len(changed) <= 3 else f" (+{len(changed) - 3})"
                try:
                    _build_once()
                    print(f"[watch] rebuilt · {datetime.now():%H:%M:%S} · {', '.join(short)}{more}")
                except Exception as exc:
                    print(f"[watch] rebuild failed: {exc}")
                last = now
        except KeyboardInterrupt:
            print("\n[watch] stopped")
            return


def _serve(port: int) -> None:
    """Run a tiny HTTP server in the dashboard dir so the browser can poll for reload."""
    import http.server
    import socketserver
    import threading
    import webbrowser

    class _Handler(http.server.SimpleHTTPRequestHandler):
        def __init__(self, *args, **kwargs):
            super().__init__(*args, directory=str(DASHBOARD_DIR), **kwargs)

        def end_headers(self):
            self.send_header("Cache-Control", "no-store, no-cache, must-revalidate")
            super().end_headers()

        def log_message(self, *args, **kwargs):
            pass  # quiet

    class _ReuseServer(socketserver.ThreadingTCPServer):
        allow_reuse_address = True
        daemon_threads = True

    httpd = _ReuseServer(("127.0.0.1", port), _Handler)
    actual_port = httpd.server_address[1]
    url = f"http://127.0.0.1:{actual_port}/dashboard.html"
    print(f"[serve] serving {DASHBOARD_DIR} at {url}")
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    try:
        webbrowser.open(url)
    except Exception:
        pass


def main() -> None:
    import argparse

    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--watch", action="store_true", help="Rebuild on fixture/prompt/agent changes.")
    p.add_argument(
        "--serve", action="store_true", help="Start an HTTP server + open browser. Implies --watch."
    )
    p.add_argument("--port", type=int, default=8765, help="Port for --serve (default 8765).")
    args = p.parse_args()

    if args.serve:
        _build_once()
        _serve(args.port)
        _watch_loop()
        return

    if args.watch:
        _watch_loop()
        return

    _build_once()
    print(f"Wrote {DASHBOARD_FILE}")
    print(f"Open: open {DASHBOARD_FILE}")
    print("Or run: cd src/apps/api && .venv/bin/python scripts/build_eval_dashboard.py --serve")


if __name__ == "__main__":
    main()
