import CoreGraphics

enum NativeTextBlockPlacement: String, CaseIterable, Sendable {
    case left, center, right, top, middle, bottom

    var isHorizontal: Bool { self == .left || self == .center || self == .right }
}

/// Where a text's anchor goes so its drawn block stays put or lands against an edge. Blocks are rects in canvas
/// fractions, as measured by `NativeEditorRenderCompiler.textBlockRect`. Mirrors web `xFracForTextAlignment` and
/// `xFracForTextBoxPosition` (src/apps/web/src/lib/overlay-layout.ts), with the block measured from the drawn lines.
enum NativeTextBlockGeometry {
    static let tolerance = 0.002
    static var safeRect: CGRect { NativePreviewDangerZones.safeRect(in: CGSize(width: 1, height: 1)) }

    /// How far along the block its anchor sits: left edge, centre, right edge.
    static func anchorFactor(_ alignment: String?) -> Double {
        switch alignment { case "left": 0; case "right": 1; default: 0.5 }
    }

    /// The `x_frac` that draws `block` where it is once the text is aligned `alignment`. A block that hangs off
    /// the frame is pulled back in first, so the stored value stays a valid fraction.
    static func anchorX(keeping block: CGRect, alignment: String?) -> Double {
        let left = min(max(0, block.minX), max(0, 1 - block.width))
        return left + anchorFactor(alignment) * block.width
    }

    /// The anchor that puts `block` against `placement`, moving along that one axis only.
    static func anchor(placing block: CGRect, anchor: CGPoint, at placement: NativeTextBlockPlacement) -> CGPoint {
        let safe = safeRect
        switch placement {
        case .left: return CGPoint(x: anchor.x + safe.minX - block.minX, y: anchor.y)
        case .center: return CGPoint(x: anchor.x + 0.5 - block.midX, y: anchor.y)
        case .right: return CGPoint(x: anchor.x + safe.maxX - block.maxX, y: anchor.y)
        case .top: return CGPoint(x: anchor.x, y: anchor.y + safe.minY - block.minY)
        case .middle: return CGPoint(x: anchor.x, y: anchor.y + 0.5 - block.midY)
        case .bottom: return CGPoint(x: anchor.x, y: anchor.y + safe.maxY - block.maxY)
        }
    }

    /// Every placement the block currently satisfies, like web `inferTextBoxPosition`.
    static func placements(of block: CGRect) -> Set<NativeTextBlockPlacement> {
        let safe = safeRect
        var result: Set<NativeTextBlockPlacement> = []
        func near(_ a: CGFloat, _ b: CGFloat) -> Bool { abs(a - b) <= tolerance }
        if near(block.minX, safe.minX) { result.insert(.left) }
        if near(block.midX, 0.5) { result.insert(.center) }
        if near(block.maxX, safe.maxX) { result.insert(.right) }
        if near(block.minY, safe.minY) { result.insert(.top) }
        if near(block.midY, 0.5) { result.insert(.middle) }
        if near(block.maxY, safe.maxY) { result.insert(.bottom) }
        return result
    }
}
