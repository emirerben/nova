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
collapse_raw = copy.deepcopy(raw)
collapse_raw["split_screen_sections"][0]["collapse_empty_panels"] = True
collapse_recipe = EditRecipeV2.model_validate(collapse_raw)
assert {"splitScreen", "splitCollapse"} <= collapse_recipe.required_capabilities
collapse_status = SimpleNamespace(request=SimpleNamespace(recipe=collapse_recipe))
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
collapse_branches = [
    node
    for node in fn.body
    if isinstance(node, ast.If)
    and ast.unparse(node.test)
    == "'splitCollapse' in recipe.required_capabilities and protocol < SPLIT_COLLAPSE_PROTOCOL"
]
assert len(collapse_branches) == 1, "Mutation must remove exactly the collapse floor branch"
assert client_protocol.SPLIT_COLLAPSE_PROTOCOL == 47
blank_branches = [
    node
    for node in fn.body
    if isinstance(node, ast.If)
    and ast.unparse(node.test)
    == "'splitBlankPanel' in recipe.required_capabilities and protocol < SPLIT_BLANK_PANEL_PROTOCOL"
]
assert len(blank_branches) == 1, "Mutation must remove exactly the blank floor branch"
blank_protocol = client_protocol.SPLIT_BLANK_PANEL_PROTOCOL
blank_raw = copy.deepcopy(raw)
blank_raw["split_screen_sections"][0]["blanks"] = [
    {"id": "b", "panel_index": 0, "start": 1, "end": 2, "color": "#FF0000"}
]
blank_raw["tracks"][0]["clips"][0]["split_screen"] = {"section_id": "s", "panel_index": 1}
blank_recipe = EditRecipeV2.model_validate(blank_raw)
assert {"splitScreen", "splitBlankPanel"} <= blank_recipe.required_capabilities
blank_status = SimpleNamespace(request=SimpleNamespace(recipe=blank_recipe))
for protocol, expected_blank in ((blank_protocol - 1, "split_blank_panel"), (blank_protocol, None)):
    set_client_protocol(protocol)
    try:
        detail = device_render._client_update_detail(SimpleNamespace(assembly_plan={}), {}, blank_status)
    finally:
        clear_request_context()
    assert (detail or {}).get("feature") == expected_blank, (protocol, detail)
collapse_module = copy.deepcopy(module)


def isolate(tree, removed, label):
    tree.body[0].body.remove(removed)
    ast.fix_missing_locations(tree)
    compiled = compile(tree, label, "exec")
    namespace = {}
    # Keep the original global namespace, including its imported helpers/context readers.
    exec(compiled, device_render._client_update_detail.__globals__, namespace)
    function = namespace["_client_update_detail"]
    assert device_render._client_update_detail is not function
    return function


mutant = isolate(module, branches[0], "<isolated-remove-split-floor>")
collapse_mutant = isolate(
    collapse_module,
    next(
        node
        for node in collapse_module.body[0].body
        if isinstance(node, ast.If)
        and ast.unparse(node.test)
        == "'splitCollapse' in recipe.required_capabilities and protocol < SPLIT_COLLAPSE_PROTOCOL"
    ),
    "<isolated-remove-collapse-floor>",
)
expected = {
    "code": "client_update_required",
    "feature": "split_screen",
    "minimum_client_protocol": 46,
}


def observe(function, subject=None, protocols=(45, 46)):
    results = {}
    for protocol in protocols:
        set_client_protocol(protocol)
        try:
            results[str(protocol)] = function(job, record, subject or status)
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
collapse_expected = {
    "code": "client_update_required",
    "feature": "split_collapse",
    "minimum_client_protocol": 47,
}
collapse_baseline = observe(
    device_render._client_update_detail, collapse_status, (45, 46, 47)
)
assert collapse_baseline == {"45": expected, "46": collapse_expected, "47": None}, collapse_baseline
assert observe(device_render._client_update_detail, status, (47,)) == {"47": None}
collapse_mutated = observe(collapse_mutant, collapse_status, (45, 46, 47))
assert collapse_mutated == {"45": expected, "46": None, "47": None}, (
    "Removing the collapse floor must change only the build-46 answer"
)
assert collapse_mutated != collapse_baseline, "Collapse mutation survived"
assert observe(collapse_mutant, status) == baseline, "Collapse branch confounds a plain split"
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
            "collapse": {
                "protocols": [45, 46, 47],
                "baseline": collapse_baseline,
                "remove_collapse_floor_mutant": collapse_mutated,
                "mutant_caught": True,
            },
            "inputs_unchanged": True,
            "scope": "direct helper return value; no HTTP/lifecycle/persistence proof",
        },
        indent=2,
    )
)
