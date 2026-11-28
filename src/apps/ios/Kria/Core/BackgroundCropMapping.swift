import Foundation
import KriaMediaEngine

/// KRI-1656: Crop for a Talk-to-camera background, stored as the wire's existing `focus {x, y, zoom}` (no new field).
///
/// The engine draws a background cover-fitted to the canvas, scaled by `zoom` (a multiple of cover), then slid by `x` / `y`
/// across the spare room (0 = left / top edge, 0.5 = centre, 1 = right / bottom edge). For `zoom >= 1` the part of the picture
/// that shows is therefore a rectangle with the canvas's shape, so it round-trips with a crop rectangle exactly:
///
///     w = fill.width / zoom,  h = fill.height / zoom
///     x = fx * (1 - w),       y = fy * (1 - h)           (top-left origin, normalised to the upright picture)
///
/// where `fill` is the centred crop a plain cover-fit shows (`KriaCropGeometry.fillCrop`). Below 1x the whole picture sits
/// inside the frame with bands, which no crop rectangle can express: that case reads as the plain fill and is replaced by
/// the first drag.
enum BackgroundCropMapping {
    /// The most a crop may zoom in: the editor's own ceiling, `NativeEditorSession.backgroundFocusZoomRange.upperBound` (the wire allows 4x).
    static let maxZoom = 3.0
    /// Within this of the centred 1x cover-fit, a crop is stored as "no crop".
    static let centredTolerance = 0.002
    private static let spare = 1e-6

    /// The crop rectangle a focus shows; nil for no focus, a centred 1x focus, or a zoom below 1x (nothing to crop).
    static func crop(from focus: SpeakerBackgroundFocus?, imageAspect: Double, outputAspect: Double) -> NormalizedSourceRect? {
        guard let focus, focus.zoom >= 1 - 1e-9 else { return nil }
        let fill = KriaCropGeometry.fillCrop(imageAspect: imageAspect, outputAspect: outputAspect)
        let width = fill.width / focus.zoom, height = fill.height / focus.zoom
        return NormalizedSourceRect(x: focus.x * (1 - width), y: focus.y * (1 - height), width: width, height: height)
    }

    /// The focus that shows `rect` (a crop rectangle with the canvas's shape). `current` supplies an axis with no spare room
    /// (a crop as wide as the picture cannot say where along x it sits). Nil when the result is the centred 1x cover-fit.
    static func focus(from rect: NormalizedSourceRect, current: SpeakerBackgroundFocus?, imageAspect: Double, outputAspect: Double) -> SpeakerBackgroundFocus? {
        let fill = KriaCropGeometry.fillCrop(imageAspect: imageAspect, outputAspect: outputAspect)
        guard rect.width > spare, rect.height > spare else { return current }
        let zoom = min(maxZoom, max(1, fill.width / rect.width))
        let roomX = 1 - rect.width, roomY = 1 - rect.height
        let x = roomX > spare ? rect.x / roomX : (current?.x ?? 0.5)
        let y = roomY > spare ? rect.y / roomY : (current?.y ?? 0.5)
        let value = SpeakerBackgroundFocus(x: rounded(min(1, max(0, x))), y: rounded(min(1, max(0, y))), zoom: rounded(zoom))
        let centred = abs(value.x - 0.5) <= centredTolerance && abs(value.y - 0.5) <= centredTolerance && abs(value.zoom - 1) <= centredTolerance
        return centred ? nil : value
    }

    /// The smallest width a crop may have so its zoom stays within `maxZoom`, or within a stored zoom already past it
    /// (a server-aimed focus is shown as it is and never jumps when touched).
    static func minimumWidth(imageAspect: Double, outputAspect: Double, storedZoom: Double?) -> Double {
        KriaCropGeometry.fillCrop(imageAspect: imageAspect, outputAspect: outputAspect).width / max(maxZoom, storedZoom ?? 1)
    }

    private static func rounded(_ value: Double) -> Double { (value * 10_000).rounded() / 10_000 }
}
