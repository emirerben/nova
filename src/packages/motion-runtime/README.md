# Nova motion runtime

This package is the single frame evaluator and CanvasKit drawing implementation
for Nova's motion-preset lane.

The public editor/API payload contains only bounded, immutable preset instances.
Trusted SVG source is compiled into checked-in path data; raw SVG and executable
scene graphs never cross the API.

Parity means that the browser and export worker produce identical motion-layer
pixels for the same output dimensions, integer frame, runtime hash, and instances.
Browser-decoded video and H.264 compression are outside that byte-level contract.

Frame intervals use inclusive start and exclusive end at 30 fps. Color output is
sRGB RGBA8. Runtime and preset versions are immutable.

`creator-blocks.catalog.json` is the source of truth for Creator Block labels,
defaults, typed controls, timing phases, safe speed ranges, AI exposure, and
complexity weights. `scripts/generate-contract.mjs` derives the strict JSON
Schema and `creator-blocks.ai.json`; generated drift fails the package check.

Runtime v4 renders persisted Creator Block preset v1 and v2 payloads. New
insertions are preset v2. Content, palette, and timeline edits preserve v1;
touching a motion control explicitly upgrades only that block. The Evolving
Type v2 preset defaults to 159 frames and uses deterministic fixed-topology
organic paths—uploaded SVG, shaders, scripts, and arbitrary scene graphs are
never accepted.

```bash
npm run check:contract
```
