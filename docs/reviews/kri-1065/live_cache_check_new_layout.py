import sys, time
sys.path.insert(0, "."); sys.path.insert(0, "tests")
from app.agents._model_client import GeminiClient
from app.agents.edit_copilot import EditCopilotAgent, EditCopilotInput
from app.agents.main_creator import MainCreatorAgent
from agents.test_main_creator_agent import _input as mc_input
from test_edit_copilot import _snapshot
client = GeminiClient(); M = "gemini-3.1-pro-preview"
def call(p):
    i = client.invoke(model=M, prompt=p + "\n\n(Cache probe: reply OK.)", response_json=False, max_output_tokens=64, thinking_level="low", timeout_s=120)
    return i.tokens_in, i.tokens_cached
cop = lambda m: EditCopilotAgent(None).render_prompt(EditCopilotInput(utterance=m, variant_snapshot=_snapshot()))
mc = lambda m: MainCreatorAgent(None).render_prompt(mc_input().model_copy(update={"user_message": m, "creator_request": m}))
for name, r in (("edit_copilot", cop), ("main_creator", mc)):
    res = []
    for m in ("make the first title bigger", "swap the song for something calmer", "trim clip two by a second", "add a whoosh on the cut"):
        res.append(call(r(m + " [probe-" + name + "-v2]"))); time.sleep(10)
    print(name, "different turns, 10s apart:", res)
