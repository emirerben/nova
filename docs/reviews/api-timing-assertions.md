# API timing assertions with pytest 9.2 (KRI-577)

PR1884's required CI40588141906 failed16 existing tests: four cases in shard0,
twelve in shard1. Every failure was a TypeError from passing nested sequences to
`pytest.approx`. All four affected files were byte-identical to fetched main
f767ad63452dd49b48e3c85e9c0191e8a69301cc. CI installed pytest9.2.0; the shared
local environment used9.1.1 and accepted this comparison shape.

The same16 cases failed on unchanged main using an isolated pytest9.2.0 environment.
The repair compares each flat timing tuple using the existing `pytest.approx`
tolerances. `zip(..., strict=True)` retains the outer count requirement, and the
flat comparison retains tuple arity, order and every start/end/duration check.
No expected values, fixtures, product code, dependency constraints or skips change.
Fourteen assertion sites cover the16 failed cases, including three parameters at
the frame-grid site. Existing already-flat assertions remain as they were.

## Verification

All four full modules pass with pytest9.2.0: **171 passed, zero skipped**.
Ruff and formatting pass. The local interpreter is Python3.14 with inherited
application dependencies; GitHub's Python3.11 suite remains a required fresh check.
An AST replay of all14 changed loops tested exact and within-tolerance matches,
wrong starts/ends, reordered/missing/extra windows and truncated tuples, for both
pairs and triples. All224 control combinations behaved as expected. This count
is a comparison-control matrix, not224 product journeys.

```bash
bash scripts/check-api.sh test tests/pipeline/test_exact_window_steps.py tests/pipeline/test_reframe_keep_segments.py tests/pipeline/test_talking_head_assembler.py tests/tasks/test_generative_build_silence_cut.py -q
```

To repeat the controls with the same pytest environment, run the script below
from the repository root. It executes the actual changed loop AST, replacing
only the two sequence inputs with the independent control data.

```python
import ast
import copy
import json
from pathlib import Path
import pytest

root=Path('src/apps/api/tests')
files=['pipeline/test_exact_window_steps.py','pipeline/test_reframe_keep_segments.py','pipeline/test_talking_head_assembler.py','tasks/test_generative_build_silence_cut.py']
checked=0
sites=0
for rel in files:
    for node in ast.walk(ast.parse((root/rel).read_text())):
        if not isinstance(node, ast.For) or ast.unparse(node.target)!='(actual_window, expected_window)':
            continue
        loop=copy.deepcopy(node)
        loop.iter.args[:2]=[ast.Name(id='actual',ctx=ast.Load()),ast.Name(id='expected',ctx=ast.Load())]
        module=ast.fix_missing_locations(ast.Module(body=[loop],type_ignores=[]))
        code=compile(module,rel,'exec')
        sites+=1
        for arity in (2,3):
            expected=[tuple(float(v) for v in range(arity)),tuple(float(v+4) for v in range(arity))]
            near=[tuple(v+1e-13 for v in row) for row in expected]
            changed_start=[(expected[0][0]+0.01,)+expected[0][1:],expected[1]]
            changed_end=[expected[0][:-1]+(expected[0][-1]+0.01,),expected[1]]
            variants=[('exact',expected,True),('within_tolerance',near,True),('wrong_start',changed_start,False),('wrong_end',changed_end,False),('missing',expected[:1],False),('extra',expected+expected[:1],False),('reordered',list(reversed(expected)),False),('wrong_arity',[expected[0][:-1],expected[1]],False)]
            for name,actual,should_pass in variants:
                try:
                    exec(code,{'pytest':pytest,'actual':actual,'expected':expected})
                    passed=True
                except (AssertionError,ValueError):
                    passed=False
                assert passed==should_pass,(rel,node.lineno,arity,name,passed)
                checked+=1
assert sites==14,sites
print(json.dumps({'pytest':pytest.__version__,'changed_comparison_sites':sites,'controls_checked':checked,'all_passed':True}))
```

Private CI job logs, the base red run and final green/control logs are retained
under this task's ignored `.dev/timing-assertions/` and the Cunda task's private
evidence folder. The first control attempt used a perturbation above pytest's
absolute tolerance at zero; correcting the control to1e-13 established its stated
within-tolerance case. No comparison tolerance was changed to fit that control.

## Delivery limits

This repairs a required test contract that blocks the Cunda source-grant fix.
It does not change or prove Cunda timing preservation, native render, Photos save,
request fulfillment or TestFlight upload. PR1884 remains blocked until the repair
lands, its branch integrates current main and fresh required checks pass. Native
workflows already running are left intact. No manual deployment, workflow rerun,
production change or paid model call is part of this maintenance repair.
