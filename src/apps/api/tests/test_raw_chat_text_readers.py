"""KRI-470 PR-F: every reader of raw chat text is on a documented allow-list.

Raw chat text (``creator_request``, the first/latest user message, brief prose) can change
OUTPUT, not only the route: a regex over the request once added storyboard text, and a speech
hint once chose the render lane.  After approval, the pinned contract and strategy are the
only authority for route, duration, voice, order and required text.  So this test lists every
function under ``app/`` that reads raw chat text and fails when a new one appears without a
documented decision.

Each allowed entry carries a category:

``agent_prompt``      an LLM agent input/prompt: the text is context for the model, never a
                      branch condition.
``plan_time``         runs BEFORE approval (planner, brief, creator-session, proposal
                      builders).  Whatever it derives is pinned into the strategy / brief /
                      contract the creator approves; the worker never re-derives it.
``carry``             stores or forwards the text unchanged (job construction, dispatch,
                      approval claims).
``content_only``      a worker reads the text only to enrich CONTENT (grounding labels, text
                      agent context, landmark names).  It must never decide route, duration,
                      voice, order or required text.
``legacy_unstamped``  a raw-text gate that still exists for jobs WITHOUT the plan-authority
                      stamp / contract.  Each names the test that proves plan-authority jobs
                      bypass it (the PR-F flip pair).

Adding a reader: put it in the right category with a reason.  A new worker-side reader that
decides anything but content belongs on the PR-F list instead (stamp-gated), not here.
"""

from __future__ import annotations

import ast
import pathlib
import re

APP = pathlib.Path(__file__).resolve().parents[1] / "app"
TESTS = pathlib.Path(__file__).resolve().parent

RAW_TEXT_NAMES = frozenset(
    {
        "creator_request",
        "first_user_message",
        "_first_user_message",
        "latest_message",
        "latest_user_message",
        # Helpers whose argument IS raw request prose: calling one is reading it.
        "_creator_requests_narrated_treatment",
        "speech_montage_possible",
        "mentions_speech",
    }
)
# ``Requirement.text()`` is the brief's prose one-liner (zero-arg method call).
BRIEF_PROSE_CALL = "text"

CATEGORIES = frozenset({"agent_prompt", "plan_time", "carry", "content_only", "legacy_unstamped"})

# module (relative to app/) -> (category, reason, top-level functions or None for the module)
ALLOWED: dict[str, tuple[str, str, frozenset[str] | None]] = {
    # --- agent prompts: model context, never a branch -----------------------------------------
    "agents/_schemas/brief_extractor.py": ("agent_prompt", "extractor input schema", None),
    "agents/brief_extractor.py": ("agent_prompt", "extractor prompt", None),
    "agents/clip_intent_planner.py": ("agent_prompt", "clip-intent planner input/prompt", None),
    "agents/clip_request_resolver.py": ("agent_prompt", "request resolver input/prompt", None),
    "agents/edit_proposal.py": (
        "agent_prompt",
        "proposal agent: quoted on-screen text / spoken script keys are parsed for the "
        "proposal draft, which the creator approves",
        None,
    ),
    "agents/main_creator.py": (
        "agent_prompt",
        "Main Creator input/prompt (produces the plan)",
        None,
    ),
    "agents/montage_reviewer.py": ("agent_prompt", "montage review input/prompt", None),
    "agents/narrated_clip_alignment.py": ("agent_prompt", "alignment input/prompt", None),
    "agents/narrated_storyboard.py": ("agent_prompt", "storyboard input/prompt", None),
    "agents/narration_annotations.py": ("agent_prompt", "annotation input/prompt", None),
    "agents/semantic_edit_proposal.py": ("agent_prompt", "semantic proposal prompt", None),
    "agents/speech_excerpt_planner.py": ("agent_prompt", "excerpt planner input/prompt", None),
    # --- plan time: pinned by approval ----------------------------------------------------------
    "kria/brief.py": ("plan_time", "brief extraction and rendering of live requirements", None),
    "kria/brief_binding.py": ("plan_time", "binds the approved request digest to the brief", None),
    "kria/planner.py": ("plan_time", "the planner turns the request into the strategy", None),
    "kria/runtime.py": (
        "carry",
        "approval decision forwards the request to the claim",
        frozenset({"_validated_render_shape", "decide_approval"}),
    ),
    "kria/brief_checks.py": (
        "plan_time",
        "receipt wording for the approved brief requirements",
        None,
    ),
    "routes/admin_plan_items.py": ("plan_time", "admin debug payload", None),
    "routes/creation_threads.py": ("plan_time", "render-shape projection / repair", None),
    "routes/creator_agent.py": (
        "plan_time",
        "creator session: derives typed plan fields from the live thread before approval",
        None,
    ),
    "routes/plan_items.py": ("plan_time", "item copilot / edit-proposal turns", None),
    "schemas/clip_intents.py": ("plan_time", "caption grounding of clip intents", None),
    "schemas/edit_proposal.py": ("plan_time", "proposal brief schema", None),
    "services/clip_intent_planning.py": ("plan_time", "clip-intent planning", None),
    "services/clip_intent_resolution.py": ("plan_time", "clip-intent resolution", None),
    "services/creator_sessions.py": ("plan_time", "compiles the active plan", None),
    "services/edit_direction_planner.py": ("plan_time", "direction snapshot planning", None),
    "services/edit_proposals.py": ("plan_time", "scheduled draft validation", None),
    "services/proposal_planning.py": ("plan_time", "proposal snapshot planning", None),
    "services/slide_post_chat_edit.py": ("plan_time", "slide chat edit (own plan)", None),
    "tasks/edit_proposal_build.py": ("plan_time", "guided proposal drafting", None),
    # --- carry -----------------------------------------------------------------------------------
    "services/generative_jobs.py": (
        "carry",
        "stores creator_request on the job; derives the caption LANGUAGE request from it "
        "(content: which language the captions use). Not route, duration, voice, order or text.",
        frozenset({"build_generative_job"}),
    ),
    "tasks/content_plan_build.py": (
        "carry",
        "forwards the request into the job at dispatch",
        frozenset({"_dispatch_item_render", "dispatch_item_render_for"}),
    ),
    "tasks/kria_runtime.py": (
        "carry",
        "approval claims / dispatch carry the request",
        frozenset(
            {
                "_ApprovalDispatchClaim",
                "_claim_approval_dispatch",
                "_complete_draft_turn",
                "execute_kria_approval",
            }
        ),
    ),
    "services/guided_narration_labels.py": (
        "content_only",
        "narration label wording for the approved guided plan",
        None,
    ),
    # --- worker side -----------------------------------------------------------------------------
    "services/phone_speech_montage_job.py": (
        "legacy_unstamped",
        "`speech_montage_possible` over request words runs only when the job has no contract "
        "(`contract is None`); a contracted job's lane is its contract audio_source_ids",
        frozenset({"_request_text", "run_phone_speech_montage_job"}),
    ),
    "services/speech_montage_planning.py": (
        "legacy_unstamped",
        "`mentions_speech` / `speech_montage_possible` run only when speech is not required by a "
        "contract; the request text is also the planner's prompt context",
        frozenset(
            {"<module>", "mentions_speech", "plan_speech_montage", "speech_montage_possible"}
        ),
    ),
    "services/render_shape.py": (
        "legacy_unstamped",
        "the creation offer's speech-lane predicate reads request words only with "
        "KRIA_PLAN_AUTHORITY_ENABLED off",
        frozenset({"_may_route_to_speech_montage", "creation_offer", "offer_for_item"}),
    ),
    "tasks/generative_build.py": (
        "content_only",
        "worker readers; see the per-function reasons in WORKER_FUNCTIONS",
        frozenset(),  # filled from WORKER_FUNCTIONS below
    ),
}

# generative_build.py: top-level function (or class) -> (category, reason)
WORKER_FUNCTIONS: dict[str, tuple[str, str]] = {
    "_decide_generative_variant": ("content_only", "forwards the request to the text agents"),
    "_first_user_message": (
        "content_only",
        "loader of the thread's first message. Callers: the unified montage's landmark-name "
        "language hint (content) and the pre-contract speech lane request "
        "(`legacy_unstamped`, services/phone_speech_montage_job.py)",
    ),
    "_grounded_context_labels": ("content_only", "grounds context-label wording"),
    "_guided_execution_plan": ("content_only", "guided plan label wording"),
    "_narrated_clip_alignment_steps": ("content_only", "alignment agent context"),
    "_narrated_storyboard_plan": ("content_only", "storyboard agent context"),
    "_creator_requests_narrated_treatment": (
        "content_only",
        "OUTPUT-AFFECTING, content-carrying: regexes over the request add intro / player / score "
        "text. Stays for stamped and unstamped jobs alike until a typed treatment carrier exists "
        "(CreativeStrategy has none; dropping it would silently remove treatments the creator "
        "asked for). Never route, duration, voice, order or contract text.",
    ),
    "_narrated_storyboard_text_elements": (
        "content_only",
        "caller of _creator_requests_narrated_treatment (see above)",
    ),
    "_process_generative_variant": ("content_only", "forwards the request to the variant render"),
    "_render_narrated_variant": ("content_only", "forwards the request to the storyboard agent"),
    "_run_generative_job_impl": (
        "content_only",
        "reads and forwards the request as render context",
    ),
    "_run_phone_narrated_job": ("content_only", "text agent context"),
    "_run_phone_unified_montage_job": (
        "content_only",
        "landmark enrichment via _first_user_message: names places, never routes",
    ),
    "_run_phone_voiceover_montage_job": ("content_only", "text agent context"),
    "_run_regenerate_variant": ("content_only", "forwards the saved request to the re-render"),
}

# Each legacy gate must be proven bypassed for plan-authority jobs by a test that still exists,
# is collected (module-level ``test_*`` function) and is not skipped or xfailed.
LEGACY_GATE_PROOFS: dict[str, tuple[str, str]] = {
    "services/phone_speech_montage_job.py": (
        "tasks/test_route_override_flips.py",
        "test_a_contracted_phone_montage_ignores_speech_words_in_the_request",
    ),
    "services/speech_montage_planning.py": (
        "tasks/test_route_override_flips.py",
        "test_a_contracted_phone_montage_ignores_speech_words_in_the_request",
    ),
    "services/render_shape.py": (
        "services/test_render_shape.py",
        "test_plan_authority_offers_a_shape_however_the_request_talks_about_speech",
    ),
}

# Known limits: a reader that builds the name dynamically (getattr / string concatenation /
# an alias) or takes the prose under another parameter name is not seen; the helper names in
# RAW_TEXT_NAMES narrow that gap for the known prose-taking helpers. Reviewers still read new
# worker code that touches request text.


def _readers() -> set[tuple[str, str]]:
    """(module path relative to app/, top-level function/class) for every raw-text reader."""
    found: set[tuple[str, str]] = set()
    for path in sorted(APP.rglob("*.py")):
        relative = path.relative_to(APP).as_posix()
        tree = ast.parse(path.read_text())

        class Visitor(ast.NodeVisitor):
            def __init__(self) -> None:
                self.stack: list[str] = []

            def hit(self) -> None:
                found.add((relative, self.stack[0] if self.stack else "<module>"))

            def _scope(self, node: ast.AST, name: str) -> None:
                if name in RAW_TEXT_NAMES:
                    self.stack.append(name)
                    self.hit()
                    self.stack.pop()
                self.stack.append(name)
                self.generic_visit(node)
                self.stack.pop()

            def visit_FunctionDef(self, node: ast.FunctionDef) -> None:
                self._scope(node, node.name)

            visit_AsyncFunctionDef = visit_FunctionDef  # type: ignore[assignment]

            def visit_ClassDef(self, node: ast.ClassDef) -> None:
                self._scope(node, node.name)

            def visit_Name(self, node: ast.Name) -> None:
                if node.id in RAW_TEXT_NAMES:
                    self.hit()

            def visit_Attribute(self, node: ast.Attribute) -> None:
                if node.attr in RAW_TEXT_NAMES:
                    self.hit()
                self.generic_visit(node)

            def visit_arg(self, node: ast.arg) -> None:
                if node.arg in RAW_TEXT_NAMES:
                    self.hit()

            def visit_keyword(self, node: ast.keyword) -> None:
                if node.arg in RAW_TEXT_NAMES:
                    self.hit()
                self.generic_visit(node)

            def visit_Constant(self, node: ast.Constant) -> None:
                if isinstance(node.value, str) and node.value in RAW_TEXT_NAMES:
                    self.hit()

            def visit_Call(self, node: ast.Call) -> None:
                func = node.func
                if (
                    isinstance(func, ast.Attribute)
                    and func.attr == BRIEF_PROSE_CALL
                    and not node.args
                    and not node.keywords
                ):
                    self.hit()
                self.generic_visit(node)

        Visitor().visit(tree)
    return found


def _allowed(module: str, function: str) -> bool:
    entry = ALLOWED.get(module)
    if entry is None:
        return False
    if module == "tasks/generative_build.py":
        return function in WORKER_FUNCTIONS
    return entry[2] is None or function in entry[2]


def test_every_reader_of_raw_chat_text_is_on_the_allow_list() -> None:
    unlisted = sorted((m, f) for m, f in _readers() if not _allowed(m, f))
    assert not unlisted, (
        "These functions read raw chat text (creator_request / first or latest user message / "
        "brief prose) and are not on the KRI-470 PR-F allow-list.\n"
        + "\n".join(f"  {m}::{f}" for m, f in unlisted)
        + "\nAdd each to ALLOWED (or WORKER_FUNCTIONS) with its category and why it can never "
        "decide route, duration, voice, order or required text, or read the pinned contract "
        "instead."
    )


def test_the_allow_list_has_no_stale_entries() -> None:
    readers = _readers()
    modules = {m for m, _ in readers}
    stale_modules = sorted(set(ALLOWED) - modules)
    stale_functions = sorted(
        f for f in WORKER_FUNCTIONS if ("tasks/generative_build.py", f) not in readers
    )
    assert not stale_modules and not stale_functions, (stale_modules, stale_functions)


def test_every_entry_has_a_known_category_and_a_reason() -> None:
    for module, (category, reason, _functions) in ALLOWED.items():
        assert category in CATEGORIES, module
        assert reason.strip(), module
    for function, (category, reason) in WORKER_FUNCTIONS.items():
        assert category in CATEGORIES, function
        assert reason.strip(), function


def _proof_is_a_live_collected_test(test_file: str, test_name: str) -> bool:
    tree = ast.parse((TESTS / test_file).read_text())
    for node in tree.body:
        if isinstance(node, ast.Assign) and any(
            isinstance(t, ast.Name) and t.id == "pytestmark" for t in node.targets
        ):
            if re.search(r"skip|xfail", ast.unparse(node.value)):
                return False
        if isinstance(node, ast.FunctionDef) and node.name == test_name:
            if not node.name.startswith("test_"):
                return False
            return not any(re.search(r"skip|xfail", ast.unparse(d)) for d in node.decorator_list)
    return False


def test_each_legacy_raw_text_gate_names_a_live_test_proving_plan_authority_bypasses_it() -> None:
    legacy_modules = {m for m, (c, _r, _f) in ALLOWED.items() if c == "legacy_unstamped"}
    legacy_modules |= {
        "tasks/generative_build.py"
        for c, _r in WORKER_FUNCTIONS.values()
        if c == "legacy_unstamped"
    }
    assert legacy_modules <= set(LEGACY_GATE_PROOFS), legacy_modules - set(LEGACY_GATE_PROOFS)
    for module, (test_file, test_name) in LEGACY_GATE_PROOFS.items():
        assert _proof_is_a_live_collected_test(test_file, test_name), (module, test_name)


def test_carry_and_legacy_modules_are_pinned_per_function() -> None:
    """A new reader inside an already-allowed carry/legacy module must be a conscious entry."""
    for module, (category, _reason, functions) in ALLOWED.items():
        if category in {"carry", "legacy_unstamped"}:
            assert functions, f"{module}: {category} entries list their functions"


def test_the_proof_check_rejects_a_skipped_or_missing_test(tmp_path) -> None:
    sample = tmp_path / "test_sample.py"
    sample.write_text(
        "import pytest\n\n\n@pytest.mark.skip\ndef test_skipped():\n    pass\n\n\n"
        "@pytest.mark.xfail\ndef test_xfailed():\n    pass\n\n\n"
        "def test_live():\n    pass\n"
    )
    globals()["TESTS"], saved = tmp_path, TESTS
    try:
        assert _proof_is_a_live_collected_test("test_sample.py", "test_live")
        assert not _proof_is_a_live_collected_test("test_sample.py", "test_skipped")
        assert not _proof_is_a_live_collected_test("test_sample.py", "test_xfailed")
        assert not _proof_is_a_live_collected_test("test_sample.py", "test_missing")
    finally:
        globals()["TESTS"] = saved
