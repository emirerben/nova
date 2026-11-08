"""Source-level floor tooth only; no HTTP/DB/render lifecycle claim."""

import ast
import copy
import hashlib
import importlib
import inspect
import json
import os
from pathlib import Path
import sys
from types import SimpleNamespace

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "src/apps/api"))


def deny_external(event, args):
    if event in {"socket.connect", "socket.bind", "subprocess.Popen", "os.system"}:
        raise RuntimeError(f"External operation forbidden in this control: {event}")


sys.addaudithook(deny_external)

os.environ["STORAGE_BUCKET"] = "offline-control-unused"
os.environ["DATABASE_URL"] = "postgresql+asyncpg://unused:unused@127.0.0.1:1/unused"

device_render = importlib.import_module("app.routes.device_render")
EditRecipeV2 = importlib.import_module("app.kria.recipes_v2").EditRecipeV2
client_protocol = importlib.import_module("app.services.client_protocol")
clear_request_context = client_protocol.clear_request_context
set_client_protocol = client_protocol.set_client_protocol

source = inspect.getsource(device_render._client_update_detail)
source_hash = hashlib.sha256(source.encode()).hexdigest()
pin = json.loads(Path(__file__).with_suffix(".pin.json").read_text())
assert source_hash == pin["function_sha256"], (
    "Source changed: review correspondence, do not silently repin"
)
fixture_path = ROOT / "src/apps/api/tests/fixtures/kria_edit_recipe_v2.json"
fixture_bytes = fixture_path.read_bytes()
assert hashlib.sha256(fixture_bytes).hexdigest() == pin["fixture_sha256"]
raw = json.loads(fixture_bytes)
raw["split_screen_sections"] = [
    {"id": "s", "start": 0, "end": 4, "panel_weights": [1, 1]}
]
raw["tracks"][0]["clips"][0]["split_screen"] = {"section_id": "s", "panel_index": 1}
recipe = EditRecipeV2.model_validate(raw)
assert "splitScreen" in recipe.required_capabilities
status = SimpleNamespace(request=SimpleNamespace(recipe=recipe))
job = SimpleNamespace(assembly_plan={})
record = {}
before = (
    copy.deepcopy(job.assembly_plan),
    recipe.model_dump(mode="json"),
    copy.deepcopy(record),
)

module = ast.parse(source)
fn = module.body[0]
branches = [
    node
    for node in fn.body
    if isinstance(node, ast.If)
    and ast.unparse(node.test)
    == "'splitScreen' in recipe.required_capabilities and protocol < 46"
]
assert len(branches) == 1, "Mutation must remove exactly the new floor branch"
fn.body.remove(branches[0])
ast.fix_missing_locations(module)
# Keep the actual original global namespace, including its imported helpers/context readers.
compiled = compile(module, "<isolated-remove-split-floor>", "exec")
local = {}
exec(compiled, device_render._client_update_detail.__globals__, local)
mutant = local["_client_update_detail"]
assert device_render._client_update_detail is not mutant
expected = {
    "code": "client_update_required",
    "feature": "split_screen",
    "minimum_client_protocol": 46,
}


def observe(function):
    results = {}
    for protocol in (45, 46):
        set_client_protocol(protocol)
        try:
            results[str(protocol)] = function(job, record, status)
        finally:
            clear_request_context()
    return results


def check(results):
    assert results == {"45": expected, "46": None}, results


baseline = observe(device_render._client_update_detail)
check(baseline)
mutated = observe(mutant)
try:
    check(mutated)
except AssertionError:
    caught = True
else:
    raise AssertionError("Mutation survived the identical boundary assertion")
assert mutated == {"45": None, "46": None}, "Unrelated gate confounds isolated mutation"
assert before == (job.assembly_plan, recipe.model_dump(mode="json"), record), (
    "Pure check mutated input"
)
print(
    json.dumps(
        {
            "source_sha256": source_hash,
            "fixture_sha256": pin["fixture_sha256"],
            "input": {
                "protocols": [45, 46],
                "required_capabilities": sorted(recipe.required_capabilities),
                "section": raw["split_screen_sections"],
                "member": raw["tracks"][0]["clips"][0]["split_screen"],
                "record": {},
                "assembly_plan": {},
            },
            "baseline": baseline,
            "remove_split_floor_mutant": mutated,
            "mutant_caught": caught,
            "inputs_unchanged": True,
            "scope": "direct helper return value; no HTTP/lifecycle/persistence proof",
        },
        indent=2,
    )
)
