import Foundation

/// Compatibility for duration-only slots whose final source frame was rounded onto the 30fps clock.
/// The caller must try ordinary fit/reanchoring first and supply the resolved original's duration.
enum NativeLegacySourceWindow {
    static func hasNoEndpoint(_ slot: EditorTimelineSlot) -> Bool {
        slot.raw["source_end_s"] == nil || slot.raw["source_end_s"] == .null
    }

    /// Keep an authored fractional frame on its own clock before sources load.
    /// This is not source admission: only the compiler's resolved original can
    /// authorize a fit/reanchor or the bounded final-frame hold.
    static func preservesAuthoredClock(_ slot: EditorTimelineSlot) -> Bool {
        guard hasNoEndpoint(slot), slot.raw["native_source_span_s"] == nil,
              (slot.raw["freeze_hold_s"]?.numberValue ?? 0) == 0,
              let duration = slot.durationS, duration.isFinite, duration > 0 else { return false }
        return abs((duration * 30).rounded(.toNearestOrEven) / 30 - duration) <= 0.000_001
            && abs((duration * 1_000).rounded() / 1_000 - duration) > 0.000_001
    }

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
