import CoreGraphics
import Foundation
import KriaMediaEngine

// The text move / resize / rotate / snap layer, shared by the native video
// preview (`NativeVideoPreview`) and the slide-post canvas
// (`SlidePostTextCanvas`). Everything here is value-only math or a value-type
// state machine: no SwiftUI state, no session, no document model. Each host
// supplies a `TextTransformBaseline` (the text as it was when the gesture
// began) and, optionally, the text's measured `TextTransformBounds`, then reads
// the live result back out and commits it through its own model.

/// A text as it stood when a gesture began. Every sample of that gesture is
/// applied against this immutable value, so nothing compounds.
struct TextTransformBaseline: Equatable {
    var id: String
    /// The text's anchor point as fractions of the canvas.
    var anchor: CGPoint
    var sizePx: Double
    /// Wrap width as a fraction of the canvas width.
    var widthFrac: Double
    var rotationDeg: Double
}

/// A text block's measured footprint as fractions of the canvas. The centre
/// already includes the baseline rotation; width and height are unrotated.
struct TextTransformBounds: Equatable {
    var centerX: Double
    var centerY: Double
    var width: Double
    var height: Double

    init(centerX: Double, centerY: Double, width: Double, height: Double) {
        self.centerX = centerX; self.centerY = centerY; self.width = width; self.height = height
    }

    init(_ bounds: TextSelectionBounds) {
        self.init(centerX: bounds.centerX, centerY: bounds.centerY, width: bounds.width, height: bounds.height)
    }
}

enum NativeTextTransformMath {
    /// The grab radius around the selected text's resize/rotate corner.
    static let cornerGrabRadius: CGFloat = 22
    /// Floors shared with `NativeEditorSession.transformText`.
    static let minimumPointSize = 8.0
    static let minimumWidthFrac = 0.2

    /// The bottom-right corner of `bounds` after rotating it about its own centre.
    static func cornerPoint(of bounds: CGRect, rotationDegrees: Double) -> CGPoint {
        let radians = CGFloat(rotationDegrees) * .pi / 180
        let dx = bounds.width / 2
        let dy = bounds.height / 2
        return CGPoint(x: bounds.midX + dx * cos(radians) - dy * sin(radians),
                       y: bounds.midY + dx * sin(radians) + dy * cos(radians))
    }

    /// The corner handle, optionally pulled inside `canvas` so a text near an
    /// edge never leaves its handle clipped or unreachable.
    static func handleCenter(of bounds: CGRect, rotationDegrees: Double, canvas: CGSize, inset: CGFloat?) -> CGPoint {
        let corner = cornerPoint(of: bounds, rotationDegrees: rotationDegrees)
        guard let inset, canvas.width > inset * 2, canvas.height > inset * 2 else { return corner }
        return CGPoint(x: min(max(inset, corner.x), canvas.width - inset),
                       y: min(max(inset, corner.y), canvas.height - inset))
    }

    /// Whether a drag that began at `point` grabs the corner rather than the body.
    static func grabsCorner(at point: CGPoint, corner: CGPoint, bounds: CGRect) -> Bool {
        let cornerDistance = hypot(point.x - corner.x, point.y - corner.y)
        let centerDistance = hypot(point.x - bounds.midX, point.y - bounds.midY)
        return cornerDistance <= cornerGrabRadius && cornerDistance < centerDistance
    }

    /// Scale and rotation implied by dragging the corner from `start` to
    /// `next`, both measured from the text's anchor. Nil when the grab began
    /// too close to the anchor to define a direction.
    static func cornerDelta(start: CGVector, next: CGVector) -> (scale: Double, rotationDegrees: Double)? {
        let radius = hypot(start.dx, start.dy)
        guard radius > 1 else { return nil }
        let angle = atan2(next.dy, next.dx) - atan2(start.dy, start.dx)
        return (Double(hypot(next.dx, next.dy) / radius), Double(angle) * 180 / .pi)
    }

    /// The anchor after a drag of `translation` points, clamped to the canvas.
    static func movedPosition(from baseline: CGPoint, translation: CGSize, canvas: CGSize) -> CGPoint {
        CGPoint(x: min(max(0, baseline.x + translation.width / canvas.width), 1),
                y: min(max(0, baseline.y + translation.height / canvas.height), 1))
    }

    /// The smallest scale that keeps both the point size and wrap width legible.
    static func minimumRatio(sizePx: Double, widthFrac: Double) -> Double {
        max(minimumPointSize / sizePx, minimumWidthFrac / widthFrac)
    }

    /// Rotates `vector` by `degrees`.
    static func rotate(_ vector: CGVector, degrees: Double) -> CGVector {
        let angle = degrees * .pi / 180
        return CGVector(dx: vector.dx * cos(angle) - vector.dy * sin(angle),
                        dy: vector.dx * sin(angle) + vector.dy * cos(angle))
    }
}

/// A gesture's in-flight transform: a scale, a rotation delta and a
/// translation, all relative to the baseline. It is the single source of the
/// live frame, the alignment-guide geometry and the final commit values.
struct NativeTextLiveTransform {
    private(set) var baseline: TextTransformBaseline?
    private(set) var bounds: TextTransformBounds?
    private(set) var scale: Double = 1
    private(set) var rotation: Double = 0
    private(set) var translation = CGPoint.zero

    var isActive: Bool { baseline != nil }
    var isIdentity: Bool { scale == 1 && rotation == 0 && translation == .zero }

    /// Pins the gesture's starting text. A second call within one gesture is a no-op.
    mutating func begin(baseline: TextTransformBaseline, bounds: TextTransformBounds?) {
        guard self.baseline == nil else { return }
        self.baseline = baseline
        self.bounds = bounds
    }

    mutating func reset() {
        baseline = nil; bounds = nil
        scale = 1; rotation = 0; translation = .zero
    }

    /// Drops the pinned text only when no gesture is mid-flight (a stale
    /// scrub frame arriving between gestures).
    mutating func resetScaleAndRotation() {
        scale = 1; rotation = 0; translation = .zero
    }

    /// Applies one sample of a resize/rotate against the pinned baseline.
    /// `current` is the text being manipulated (the baseline itself for
    /// corner/pinch gestures); `snapRotation` engages the 90-degree detents.
    mutating func resize(current: TextTransformBaseline, scale requested: Double, rotation delta: Double, snapRotation: Bool) {
        let reference = baseline ?? current
        let size = reference.sizePx
        let width = reference.widthFrac
        scale = max(requested * current.sizePx / size, max(NativeTextTransformMath.minimumPointSize / size, NativeTextTransformMath.minimumWidthFrac / width))
        let rawAngle = delta + current.rotationDeg
        let displayed = snapRotation ? NativeTextRotationSnap.angle(rawAngle) : rawAngle
        rotation = displayed - reference.rotationDeg
        translation = CGPoint(x: current.anchor.x - reference.anchor.x, y: current.anchor.y - reference.anchor.y)
    }

    /// Records a move to `position` (canvas fractions) after `resize(scale: 1)`.
    mutating func move(to position: CGPoint) {
        let origin = baseline?.anchor ?? position
        translation = CGPoint(x: position.x - origin.x, y: position.y - origin.y)
    }

    /// The text block's centre in canvas points under the live transform.
    func center(in canvas: CGSize) -> CGPoint? {
        guard let baseline, let bounds else { return nil }
        let anchor = baseline.anchor
        let angle = rotation * .pi / 180
        let dx = (bounds.centerX - anchor.x) * canvas.width * scale
        let dy = (bounds.centerY - anchor.y) * canvas.height * scale
        return CGPoint(x: (anchor.x + translation.x) * canvas.width + dx * cos(angle) - dy * sin(angle),
                       y: (anchor.y + translation.y) * canvas.height + dx * sin(angle) + dy * cos(angle))
    }

    /// The (unrotated) frame of the block under the live transform; rotate it
    /// by `baseline.rotationDeg + rotation` about its own centre to draw it.
    func frame(in canvas: CGSize) -> CGRect? {
        guard let center = center(in: canvas), let bounds else { return nil }
        let width = bounds.width * canvas.width * scale
        let height = bounds.height * canvas.height * scale
        return CGRect(x: center.x - width / 2, y: center.y - height / 2, width: width, height: height)
    }

    /// Feeds the alignment guides; true exactly when a guide was newly hit.
    func updateAlignment(_ feedback: inout NativeTextAlignmentFeedback, in canvas: CGSize) -> Bool {
        guard let baseline, let bounds, let center = center(in: canvas) else { return false }
        return feedback.update(
            center: center,
            size: CGSize(width: bounds.width * canvas.width * scale, height: bounds.height * canvas.height * scale),
            rotation: baseline.rotationDeg + rotation, canvas: canvas)
    }
}

// MARK: - Slide-post adapter

/// How a slide text element reads and writes the shared transform values.
/// Rotation lives in the element's `extra` bucket as `rotation_deg` (the
/// server's key); it is removed again when the text is upright so an untouched
/// text round-trips byte-identically.
extension SlidePostTextElement {
    static let defaultWidthFrac = 0.88
    /// Server bounds for size_px (the Text panel's slider stays narrower).
    static let transformSizeRange = 8...200
    static let rotationKey = "rotation_deg"

    var rotationDeg: Double { extra[Self.rotationKey]?.numberValue ?? 0 }

    mutating func setRotation(_ degrees: Double) {
        // The server clamps rotation_deg to [-360, 360]; the remainder keeps it inside.
        let value = degrees.truncatingRemainder(dividingBy: 360)
        if abs(value) < 0.005 { extra.removeValue(forKey: Self.rotationKey) } else { extra[Self.rotationKey] = .number(value) }
    }

    var transformBaseline: TextTransformBaseline {
        let anchor = SlidePostTextLayout.anchor(for: self)
        return TextTransformBaseline(id: id, anchor: CGPoint(x: anchor.x, y: anchor.y), sizePx: Double(sizePx),
                                     widthFrac: maxWidthFrac ?? Self.defaultWidthFrac, rotationDeg: rotationDeg)
    }

    /// Writes one live sample: size and wrap width scale together (like the
    /// native editor), rotation lands in `extra`, and a changed position
    /// switches the text to a free (`custom`) position.
    mutating func applyTransform(from baseline: TextTransformBaseline, live: NativeTextLiveTransform, position: CGPoint? = nil) {
        let ratio = live.scale
        sizePx = min(max(Int((baseline.sizePx * ratio).rounded()), Self.transformSizeRange.lowerBound), Self.transformSizeRange.upperBound)
        maxWidthFrac = min(max(baseline.widthFrac * ratio, NativeTextTransformMath.minimumWidthFrac), 1)
        setRotation(baseline.rotationDeg + live.rotation)
        if let position {
            self.position = "custom"
            xFrac = position.x
            yFrac = position.y
        }
    }
}

/// Where a slide text block sits, from its anchor and measured size, matching
/// the server render: x is the left edge / centre / right edge by alignment and
/// y is the block's vertical centre. The block rotates about its anchor.
enum SlidePostTextGeometry {
    /// The unrotated block centre as canvas fractions.
    static func blockCenter(for element: SlidePostTextElement, blockSize: CGSize, canvas: CGSize) -> CGPoint {
        let anchor = SlidePostTextLayout.anchor(for: element)
        let half = Double(blockSize.width / max(canvas.width, 1)) / 2
        let x: Double
        switch element.alignment {
        case "left": x = anchor.x + half
        case "right": x = anchor.x - half
        default: x = anchor.x
        }
        return CGPoint(x: x, y: anchor.y)
    }

    /// The block's visual centre in points after rotating about the anchor.
    static func visualCenter(for element: SlidePostTextElement, blockSize: CGSize, canvas: CGSize) -> CGPoint {
        let anchor = SlidePostTextLayout.anchor(for: element)
        let center = blockCenter(for: element, blockSize: blockSize, canvas: canvas)
        let offset = NativeTextTransformMath.rotate(
            CGVector(dx: (center.x - anchor.x) * canvas.width, dy: (center.y - anchor.y) * canvas.height),
            degrees: element.rotationDeg)
        return CGPoint(x: anchor.x * canvas.width + offset.dx, y: anchor.y * canvas.height + offset.dy)
    }

    static func frame(for element: SlidePostTextElement, blockSize: CGSize, canvas: CGSize) -> CGRect {
        let center = visualCenter(for: element, blockSize: blockSize, canvas: canvas)
        return CGRect(x: center.x - blockSize.width / 2, y: center.y - blockSize.height / 2,
                      width: blockSize.width, height: blockSize.height)
    }

    /// The footprint handed to `NativeTextLiveTransform` for alignment guides.
    static func bounds(for element: SlidePostTextElement, blockSize: CGSize, canvas: CGSize) -> TextTransformBounds {
        let visual = visualCenter(for: element, blockSize: blockSize, canvas: canvas)
        return TextTransformBounds(centerX: Double(visual.x / max(canvas.width, 1)), centerY: Double(visual.y / max(canvas.height, 1)),
                                   width: Double(blockSize.width / max(canvas.width, 1)), height: Double(blockSize.height / max(canvas.height, 1)))
    }
}
