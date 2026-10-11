import CoreGraphics
import Foundation

/// KRI-589: paints `PortableDrawingLayer`s over a finished frame with Core Graphics.
///
/// Contract stub frozen by the contract commit; the engine lane (ios worker) implements it:
/// - strokes reveal in recorded order by cumulative arc length over `drawDuration` (entrance `.draw`);
/// - `.fade` entrance/exit fades the whole layer over `min(0.25s, window/2)`;
/// - the authoring canvas (`canvasAspect`) is aspect-fit into the output canvas, then translated to
///   `centerX/centerY`, rotated and scaled about the strokes' bounding-box centre;
/// - textures are deterministic functions of (stroke seed, arc length), so preview and export match.
public enum NativeDrawingPainter {
    /// Layers visible at `time`, in paint order.
    public static func activeLayers(_ layers: [PortableDrawingLayer], at time: Double) -> [PortableDrawingLayer] {
        layers.filter { time >= $0.start && time < $0.end }
    }

    /// Paint one layer at `time` into `context` (top-left origin, `canvas` in output pixels).
    public static func paint(_ layer: PortableDrawingLayer, at time: Double, canvas: CGSize, in context: CGContext) {
        // Implemented by the engine lane.
    }
}
