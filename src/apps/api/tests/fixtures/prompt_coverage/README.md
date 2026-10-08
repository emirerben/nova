# KRI-524 prompt coverage corpus

`corpus.json` is a reviewable library of 252 distinct, authored prompts. It
crosses all nine canonical `EDIT_FORMATS`, both `creation` and `editing` stages,
and fourteen request families: text, positioning, timing, preservation,
selection, order, captions, audio, pacing, style, compound, correction,
ambiguity, and capability.

Cases are explicitly classified as `user_example`, `negative`, `clarification`,
or `unsupported`. The user-facing catalog lists every prompt grouped by its
friendly format description and family at
[`docs/reviews/kri-524/prompt-catalog.md`](../../../../../docs/reviews/kri-524/prompt-catalog.md).

The corpus is intentionally separate from model execution. Every current case
has `evidence_status: "unexecuted"` and no `execution` payload. A capture may
promote a case only when it records the run identifier, result, and independent
assertions; the validator rejects an executed case without that evidence and
rejects unexecuted cases carrying an execution result.

Inspect the structural report from `src/apps/api`:

```bash
PYTHONPATH=. .venv/bin/python -m tests.evals.prompt_coverage --report
PYTHONPATH=. .venv/bin/python -m pytest tests/evals/test_prompt_coverage.py -q
```

Prompts are `authored` from the KRI-524 brief and the KRI-523 incident summary.
They are useful executable inputs, but authored provenance does not imply a
production capture, render, device export, or paid model run.

The reviewed corpus currently keeps cases library-only until exact evidence is
attached. `--replay` runs named offline regression suites in one subprocess and
reports that structural tier separately; it does not promote corpus evidence.
