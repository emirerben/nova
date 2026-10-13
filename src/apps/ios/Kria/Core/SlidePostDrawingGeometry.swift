import CoreGraphics
import Foundation
import KriaMediaEngine

/// KRI-589: how a slide's saved ink is painted and where Draw mode lays its canvas. Pure geometry; the
/// frozen contract is `tests/fixtures/slide-drawing-parity/v1.json` (`geometry`).
extension SlidePostDrawing {
    /// The aspect new strokes are authored in: the drawing's own when the slide already has one (so "draw more" lands
    /// where the old ink is, whatever the canvas looks like now), else the canvas's, to 4 decimals like the wire.
    static func authoringAspect(existing: SlidePostDrawing?, canvas: CGSize) -> Double {
        existing?.canvasAspect ?? aspect(of: canvas)
    }
    static func aspect(of canvas: CGSize) -> Double {
        guard canvas.width > 0, canvas.height > 0 else { return 1 }
        return (Double(canvas.width / canvas.height) * 10_000).rounded() / 10_000
    }

    /// Where the authoring canvas of `aspect` lands inside `size`: aspect-fit, centred (the painter's identity placement).
    static func fitRect(aspect: Double, in size: CGSize) -> CGRect {
        guard aspect > 0, size.width > 0, size.height > 0 else { return CGRect(origin: .zero, size: size) }
        let fitted = aspect > Double(size.width / size.height)
            ? CGSize(width: size.width, height: size.width / CGFloat(aspect))
            : CGSize(width: size.height * CGFloat(aspect), height: size.height)
        return CGRect(x: (size.width - fitted.width) / 2, y: (size.height - fitted.height) / 2, width: fitted.width, height: fitted.height)
    }

    /// The ink as one always-visible, un-animated layer with an identity transform. The painter moves the strokes'
    /// bounding-box centre onto `centerX/centerY`, so the layer must be centred on its own bbox or the whole drawing
    /// slides off where it was drawn (the PR 1 bug); that is what keeps the stored points where the fixture says.
    static func paintLayer(strokes: [DrawingStroke], canvasAspect: Double) -> PortableDrawingLayer {
        let points = strokes.flatMap(\.points).filter { $0.count == 2 }
        let xs = points.map { $0[0] }, ys = points.map { $0[1] }
        let centerX = ((xs.min() ?? 0.5) + (xs.max() ?? 0.5)) / 2, centerY = ((ys.min() ?? 0.5) + (ys.max() ?? 0.5)) / 2
        return PortableDrawingLayer(id: "slide-drawing", start: 0, end: 1, canvasAspect: canvasAspect, centerX: centerX, centerY: centerY,
                                    entrance: .none, drawDuration: 1, strokes: strokes)
    }
    var paintLayer: PortableDrawingLayer { Self.paintLayer(strokes: strokes, canvasAspect: canvasAspect) }

    /// Paints into a top-left-origin context (UIKit's renderer context or a SwiftUI `Canvas`) of `canvas` pixels.
    func paint(in context: CGContext, canvas: CGSize) {
        NativeDrawingPainter.paint(paintLayer, at: 0.5, canvas: canvas, in: context)
    }
}
