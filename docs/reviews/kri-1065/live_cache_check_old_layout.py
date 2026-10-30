import sys, time, subprocess
from string import Template
sys.path.insert(0, "."); sys.path.insert(0, "tests")
import app.agents.edit_copilot as ec
from app.agents._model_client import GeminiClient
from test_edit_copilot import _snapshot
old = subprocess.check_output(["git", "show", "origin/main:src/apps/api/prompts/edit_copilot.txt"], text=True)
client = GeminiClient(); res = []
for m in ("make the first title bigger", "swap the song for something calmer", "trim clip two by a second", "add a whoosh on the cut"):
    cap = {}
    real = ec.load_prompt
    ec.load_prompt = lambda n, **kw: (cap.update({k: str(v) for k, v in kw.items()}), real(n, **kw))[1]
    ec.EditCopilotAgent(None).render_prompt(ec.EditCopilotInput(utterance=m + " [probe-old-v2]", variant_snapshot=_snapshot()))
    ec.load_prompt = real
    p = Template(old).safe_substitute(**cap)
    i = client.invoke(model="gemini-3.1-pro-preview", prompt=p + "\n\n(Cache probe: reply OK.)", response_json=False, max_output_tokens=64, thinking_level="low", timeout_s=120)
    res.append((i.tokens_in, i.tokens_cached)); time.sleep(10)
print("OLD layout edit_copilot, different turns:", res)
