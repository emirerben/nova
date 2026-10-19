# Typed output schema — 19 October 2026

The bounded wire contract is implemented in `app/agents/_creator_wire.py`.
`schema-audit.json` preserves the original raw-Pydantic audit; it is historical
problem evidence, not an audit of the new transport or provider acceptance.

The current protocol is **typed-presence-v2**. Main Creator has explicit
model-owned strategy/action fields; server-computed fields are excluded. Copilot
has all 66 registered operation names with typed payloads and required fields.
Unknown newly registered fields fail schema construction. Dynamic maps use typed
key/value entries; recursive JSON values remain typed, never opaque JSON strings.

Every provider object is closed and every property required. Optional fields use
zero-or-one arrays. A per-object `_nulls` enum array distinguishes explicit null
(clear) from omission (keep); conflicts and duplicate markers are rejected. Union
branch tags disambiguate values. Shared definitions reduce repeated Copilot union
complexity. Round trips cover existing 61 Main and 179 Copilot replay cases plus
explicit omission/null, operation, schema and duplicate-map failure controls.

Provider projections are explicit: Anthropic-unsupported scalar/cardinality bounds
are removed only from its wire schema and retained in the local schema and original
parser. Ownership, source identity, arithmetic, capability/preset semantics and
cross-field constraints always remain Nova authorities. Provider schema acceptance
cannot establish those properties.

Two live Sol Main responses accepted **v1** native output and passed the original
parser/compiler. Gemini used its existing JSON baseline. Sonnet rejected account
credits before inference; its native schema acceptance is **unverified**. Copilot
native schema acceptance is also unverified. V2's explicit-null correction was
verified offline after the pilot stopped, with no paid v2 submissions. Keep that
version boundary when comparing results. Native transport and provider effects
are confounded in this small pilot; no schema-driven quality gain is claimed.

Sources checked with gstack browse:
- [OpenAI structured outputs](https://developers.openai.com/api/docs/guides/structured-outputs)
- [Anthropic structured outputs](https://platform.claude.com/docs/en/build-with-claude/structured-outputs)
- [Anthropic effort](https://platform.claude.com/docs/en/build-with-claude/effort)
