# Native schema feasibility — 19 October 2026

No provider compilation or inference was purchased. `schema-audit.json` is a
conservative offline audit of product contracts, not server acceptance evidence.

Attempt 1: export the complete current Pydantic outputs. Main Creator has 42
union nodes and 73 optional properties in the full schema (not just its strategy).
Its four open object definitions include BriefUpdate.facts, MixedMediaTimingProfile,
MontageAudioPlan and MontageCadenceConstraint. Sol's strict mode additionally needs
explicit required/nullable semantics and a oneOf projection. Sonnet's published
limits are 24 optional parameters and 16 union parameters. The full schema exceeds
both. Gemini may accept some open objects, but doing so would not constrain all
product choices; the audit does not advertise this as a strict product contract.

Attempt 2: inspect capability-scoped Copilot operations using `editor_ops_v2.OpSpec`
and the existing `_OP_FIELDS` / `_OP_REQUIRED` registries. These registries list
names and fields, but their payload semantics live in coercers; they do not provide
a full typed wire contract. The parsed output also includes server-derived fields.
Its generic ops, pending actions and clarification objects account for seven open
object paths. Setting `additionalProperties:false` would erase valid payloads;
turning them into JSON strings would only conceal the gap. Neither was done.

The adapters support native schema submission, with explicit rejection and no
fallback. The experiment runner blocks full native arms until their wire contracts
are implemented. JSON comparisons are distinctly labeled `json_mime` (Gemini),
`prompt_json` (Sonnet, which has no JSON-object API mode), and `json_object` (Sol).
All still pass through Nova's existing semantic validators.

The bounded next change is a dedicated versioned model wire protocol: typed,
closed operation payloads for one server-selected Copilot capability surface first;
then Main Creator action envelopes and server-selected format strategy contracts.
Define explicit presence semantics for optional fields, map them deterministically
to today's product types, and retain local bounds, ownership and cross-field checks.
Compare each projection against every existing replay fixture before enabling it.
Keep full JSON comparison as the baseline. This is a contract design task; a model
switch must not conceal it. No native-schema quality gain is claimed here.

Sources checked with gstack browse:
- [OpenAI structured outputs](https://developers.openai.com/api/docs/guides/structured-outputs)
- [Anthropic structured outputs](https://platform.claude.com/docs/en/build-with-claude/structured-outputs)
- [Anthropic effort](https://platform.claude.com/docs/en/build-with-claude/effort)
