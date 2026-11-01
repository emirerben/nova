# Generate media

The deliverable is a Visuals entry that plans supporting overlays from a prompt or automatically, previews assets, and applies reviewed placements through the native editor. It must preserve original footage and audio.

## Work sequence

1. Read the poteto-mode principles. Completed.
2. Frame the integration and inspect generation, evidence, editor admission and persistence.
3. Define the plan contract and failure fixtures before implementation.
4. Implement a read-only grounded planner and reusable taste instructions.
5. Implement native review, durable generation recovery and explicit timeline placement.
6. Verify planner failures, native compilation, timeline contracts and actual preview/export where available.
7. Review the diff independently, publish a draft PR, and resolve material checks before landing.

The first throughput checkpoint is a planner response consumed by the native review sheet without any paid image request. The second is one accepted fixture asset placed at its authored time and restored after saving. Live generation remains disabled unless existing account capabilities allow it. Development does not change production spend flags.

## Data and ownership

An overlay plan contains a document fingerprint, base generation and bounded proposals. A proposal identifies image generation, native text or an owned library asset. It carries explicit base start/end times, position, size, rationale and supporting evidence. The client persists the plan and a separate generation event for each image. Existing provider operations own spending and recovery.

Model the Domain led to one typed proposal instead of separate booleans for image, logo and text. Foundational Thinking led to fingerprint and stable insertion identity before UI work. Prove It Works requires checks against actual admitted media and native rendering before claiming end-to-end completion.

## Timeline decisions

1. Duration remains owned by NativeEditorTimelineProjection and the existing timeline clips.
2. Overlays occupy existing base time. No lane ripples, including music.
3. Existing output-time transport and inverse projection remain canonical. Proposals carry editor base time.
4. Review inputs clamp to the current base timeline. Existing editor controls own subsequent resize gestures.
5. One accepted proposal creates one transaction. A stable proposal UUID prevents duplicate insertion. Undo remains a normal editor snapshot.
6. Existing NativeEditorRenderCompiler compiles the same visual/text records used by preview. Tests must prove timing and placement survive snapshot round-trip and compilation.

## Risks and limits to verify

Local unsaved cuts must be part of planning. Requests must reject stale generation and unrecognized snapshots. Model-proposed speech triggers require real evidence. Logo requests must select a supplied asset or ask for one. Silent footage requires visual grounding. Closing generation must not retry an uncertain paid request. A developer skill cannot change runtime prompts by itself.
