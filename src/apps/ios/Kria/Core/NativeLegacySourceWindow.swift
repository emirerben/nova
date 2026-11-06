import Foundation

/// Compatibility for duration-only slots whose final source frame was rounded onto the 30fps clock.
/// The caller must try ordinary fit/reanchoring first and supply the resolved original's duration.
enum NativeLegacySourceWindow {
    static func sourceSpan(start: Double, duration: Double, rate: Double, sourceDuration: Double) -> Double? {
        guard start.isFinite, duration.isFinite, rate.isFinite, sourceDuration.isFinite,
              start >= 0, duration > 0, rate > 0, sourceDuration > start else { return nil }
        let span = sourceDuration - start
        let moving = span / rate
        let padding = duration - moving
        let rounded = (moving * 30).rounded(.toNearestOrEven) / 30
        guard padding > 0, padding <= 1.0 / 60 + 0.000_001,
              abs(rounded - duration) <= 0.000_001 else { return nil }
        return span
    }
}
