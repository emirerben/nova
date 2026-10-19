# Native schema evidence — 19 October 2026

`app/agents/_creator_wire.py` implements typed-presence-v2 for model-owned Main
fields and all 66 registered Copilot operations. Server-owned fields are excluded.
Maps use typed entries; unions retain tagged alternatives; `_nulls` distinguishes
clear from omission. Original semantic/ownership/cross-field validators remain
mandatory. Existing 61 Main and 179 Copilot fixture round trips pass offline.

Sol accepted live native Main v1/v2 and Copilot v2 responses. Gemini used its JSON
baseline in the matched comparison. A later native smoke failed before a usable
response and its reservation released at zero; the specific provider diagnostic
was not retained, so the exact cause is unverified. Gemini native is not certified. These observations
establish input-specific compatibility, not subjective quality or full export safety.

After funding, Sonnet rejected recursive JSONValue definitions. An explicit acyclic
projection preserves typed values but caps dynamic JSON at eight nested containers;
unsupported scalar/cardinality bounds remain enforced locally. This passed the
structural audit but the provider rejected the full compiled grammar as too large.
The audit now detects reference cycles and still does not claim server acceptance.
Full native Sonnet profiles fail locally before SDK construction. Capability-scoped
native contracts remain necessary before that option can be enabled safely.

Sonnet JSON was separately frozen, not a hidden fallback. It parsed/compiled two
Main plans; its Copilot response was invalid JSON and remains a first-attempt failure.
The raw Pydantic `schema-audit.json` is historical gap evidence; do not mistake it
for the delivered wire schema or a live provider certification.

Sources checked through gstack browse:
- [OpenAI structured outputs](https://developers.openai.com/api/docs/guides/structured-outputs)
- [Anthropic structured outputs](https://platform.claude.com/docs/en/build-with-claude/structured-outputs)

Actual provider rejections are retained in the private journals; sanitized outcomes
and frozen profiles are in the preregistration/result artifacts in this directory.

The explicit `json_guarded` profile appends a versioned serialization instruction,
counts it in the prompt hash/estimate and preserves task rules. It can be selected
independently through server configuration without accidentally selecting native
transport. A live guarded compound Copilot response still emitted invalid markup.
The profile is an experiment, not a successful format-reliability fix. Lower-effort
legacy JSON alignment controls parsed and preserved the recorded stacked layout;
one task cannot establish broad effort equivalence.
