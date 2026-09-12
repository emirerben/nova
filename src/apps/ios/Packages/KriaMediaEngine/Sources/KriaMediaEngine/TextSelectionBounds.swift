import Foundation

/// Normalized glyph bounds, excluding shadow blur and including rotation.
/// Selection uses the same resolved font geometry as the rendered frame.
public struct TextSelectionBounds: Equatable, Sendable {
    public let centerX: Double
    public let centerY: Double
    public let width: Double
    public let height: Double
    public let rotationDegrees: Double
}

#if canImport(AVFoundation)
import CoreGraphics

struct ResolvedTextSelectionBounds: Sendable {
    let rectangle: CGRect // unrotated, top-origin canvas pixels
    let canvas: CGSize

    func sample(layer: PortableTextLayer, time: Double) throws -> TextSelectionBounds {
        let state = try layer.animationPhases?.sample(time: time - layer.start, duration: layer.end - layer.start)
            ?? TextTransformTiming.sample(effect: PortableTextEffect(rawValue: layer.effect.rawValue)!,
                text: layer.runs.map(\.text).joined(separator: "\n"), localTime: time - layer.start,
                duration: layer.end - layer.start, motion: layer.motion, fade: layer.fade)
        let angle = layer.rotationDegrees * .pi / 180
        let x = (rectangle.midX - layer.anchorX) * state.scale + state.xTranslate
        let y = (rectangle.midY - layer.anchorY) * state.scale + state.yTranslate
        return TextSelectionBounds(centerX: (layer.anchorX + x * cos(angle) - y * sin(angle)) / canvas.width,
            centerY: (layer.anchorY + x * sin(angle) + y * cos(angle)) / canvas.height,
            width: rectangle.width * state.scale / canvas.width,
            height: rectangle.height * state.scale / canvas.height, rotationDegrees: layer.rotationDegrees)
    }
}
#endif
