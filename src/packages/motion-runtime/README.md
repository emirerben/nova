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
