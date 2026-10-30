# Capability composition: first qualified slice

This exposes six existing primitives through one deterministic catalog: `set_total_duration`, `retime_text_sequence`, `set_texts_timing`, `replace_text_sequence`, `patch_text`, and `reorder_clips_by`. Names, accepted/required fields, families and available limits come from the existing parser, registry and editor snapshot. The real parser/compiler remain authoritative. The catalog is partial discovery, not a complete schema or a universal capability claim.

Gemini receives this catalog through its actual prompt formatter (v94). The same serializer is available for OpenAI; no new OpenAI caller, media interface, paid capability or production activation is included.

## Inventory and limits

The canonical manual-control inventory remains `app/kria/editor_capability_parity.py`, which maps native actions to agent operations or explicit unsupported reasons. The catalog additionally follows current version/family/surface gates; discovery does not prove that a target or source interval is feasible.

- Existing shuffle supports a complete ten-clip permutation with stable retry behavior.
- Existing generic timing and split operations turn one block into 27 exact word blocks at 12/20 seconds; no allocator was added.
- Bulk position, alignment, color and a metadata-backed DM Sans family survive backend Save preparation and database reopen.
- Twenty-seven distinct per-target placements exceed the current 16-operation limit. Bounded multi-batch composition remains open. Bulk selectors can apply one patch to many targets.
- Caption-specific placement, freehand drawing, layer order, crop and creator-owned-song windows retain their explicit manual-only gaps. Source-window/reader and reference-font Save writers remain with their existing owners.
- Numeric normalization is not a readability guarantee. Font and geometry constraints are partial and rely on the existing snapshot, parser and native preview.
- With composition disabled, unsupported nondefault sequence mode/weight now refuses with clarification instead of silently applying defaults. Explicit equivalent replace/equal remains valid; enabled behavior is unchanged.

## Executed evidence

Independent API verification: 62 tests passed with real PostgreSQL, parser/runtime, Save preparation, persistence and reopen. Provider JSON is replayed, not live-model understanding. Wrong-word, preservation, unknown/version/operation-limit, shuffled-source and unsupported-mode controls are included.

Native verification: five tests passed for retiming and original-one-block splitting at 12/20 seconds. Real NativeEditorSession, native compiler, preview and AVFoundation export preserve 27 words, lineage, font resolution, anchors and source order. Wrong-source and simultaneous-text controls are detected. The native Save request and acknowledgement use replayed transport; the backend Save service was tested separately. The native test makes an explicit first-word size increase of 1px. Session-local asset-cache UUIDs are excluded; semantic source/timeline identity stays exact. Native timing uses existing millisecond quantization.

Synthetic images and words were used. No original footage, physical-phone acceptance, authenticated iOS HTTP roundtrip, visual font recognition, universal contrast/readability or live OpenAI review is claimed. Local UI work has ended and the task-owned simulator was shut down on user request; any further shared-screen work needs coordination with the parent session.
