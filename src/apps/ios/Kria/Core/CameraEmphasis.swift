import Foundation

/// Camera emphasis shapes (KRI-7), mirroring `app/pipeline/camera_effects.py`.
///
/// `pulse` is the original symmetric accent; `zoomInHold` pushes in, holds the
/// moment, then eases back out. The wire token stays `semantic_crop_pulse` for
/// both — the shape is carried by `easing`, so older builds keep rendering the
/// pulse they already understand.
enum CameraEmphasis {
    static let token = "semantic_crop_pulse"
    static let pulseEasing = "sine_pulse"
    static let holdEasing = "ease_in_hold"
    static let easings = [holdEasing, pulseEasing]
    static let maxIntensity = 0.08

    struct Bounds {
        let minDuration: Double
        let maxDuration: Double
        let defaultDuration: Double
        let defaultIntensity: Double
    }

    static func resolve(_ easing: String?) -> String {
        guard let easing, easings.contains(easing) else { return pulseEasing }
        return easing
    }

    static func bounds(_ easing: String?) -> Bounds {
        resolve(easing) == holdEasing
            ? Bounds(minDuration: 0.6, maxDuration: 6, defaultDuration: 1.8, defaultIntensity: 0.06)
            : Bounds(minDuration: 0.4, maxDuration: 2, defaultDuration: 1.2, defaultIntensity: 0.04)
    }

    /// Shortest window any easing allows — the floor for a timeline drag before
    /// the effect's own easing is known.
    static var shortestDuration: Double { bounds(pulseEasing).minDuration }

    static func label(_ easing: String?) -> String {
        resolve(easing) == holdEasing ? "Zoom in" : "Pulse"
    }

    /// Clamp an authored window to the easing's bounds.
    static func clampWindow(start: Double, end: Double, easing: String?) -> (Double, Double) {
        let bounds = bounds(easing)
        let safeStart = max(0, start)
        return (
            safeStart,
            min(safeStart + bounds.maxDuration, max(safeStart + bounds.minDuration, end))
        )
    }
}
