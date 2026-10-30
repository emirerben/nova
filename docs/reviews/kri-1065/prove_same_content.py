"""Re-render each agent's prompt with the NEW code, re-substitute the OLD (origin/main)
templates with the same kwargs, and compare line multisets. Layout moves only."""
import subprocess, sys, collections
from string import Template
sys.path.insert(0, ".")
sys.path.insert(0, "tests")
import app.agents.edit_copilot as ec, app.agents.main_creator as mc
from app.pipeline import prompt_loader

def old(name):
    return subprocess.check_output(["git", "show", f"origin/main:src/apps/api/prompts/{name}.txt"], text=True)

def capture(module, agent_cls, make_input, tmpl):
    calls = []
    real = module.load_prompt
    def spy(name, **kw):
        calls.append((name, kw)); return real(name, **kw)
    module.load_prompt = spy
    try:
        new = agent_cls(None).render_prompt(make_input())
    finally:
        module.load_prompt = real
    kw = {}
    for _, k in calls: kw.update({a: str(b) for a, b in k.items()})
    return new, Template(old(tmpl)).safe_substitute(**kw)

def compare(label, new, oldp):
    cn, co = collections.Counter(new.split("\n")), collections.Counter(oldp.split("\n"))
    added = cn - co; removed = co - cn
    print(f"== {label}: new={len(new)} old={len(oldp)} chars")
    print("  only in NEW:", [l[:100] for l in added.elements() if l.strip()])
    print("  only in OLD:", [l[:100] for l in removed.elements() if l.strip()])
    # first divergence of shared prefix between two different turn inputs reported by caller

from agents.test_main_creator_agent import _input as mc_input
new, o = capture(mc, mc.MainCreatorAgent, mc_input, "main_creator")
# old output also had the skills prefix; strip it from new for the comparison
skills = mc.load_skill("edit-story") + "\n\n" + mc.load_skill("grounded-hook") + "\n\n" + mc.load_skill("meaningful-moment") + "\n\n"
compare("main_creator", new.removeprefix(skills), o)

from test_edit_copilot import _snapshot
from app.agents.edit_copilot import EditCopilotInput
new, o = capture(ec, ec.EditCopilotAgent, lambda: EditCopilotInput(utterance="make it pop", variant_snapshot=_snapshot()), "edit_copilot")
compare("edit_copilot", new, o)
