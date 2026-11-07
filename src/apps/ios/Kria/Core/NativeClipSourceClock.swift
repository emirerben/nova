import Foundation

/// Source seconds and output seconds remain independent across rate changes and held tails.
struct NativeClipSourceClock {
    var start: Double
    var end: Double
    var rate: Double
    var hold: Double
    var frame: Double?
    var movingDuration: Double { max(0, end - start) / rate }
    var duration: Double { movingDuration + hold }

    init(slot: EditorTimelineSlot) {
        start = slot.inS
        rate = slot.raw["playback_rate"]?.numberValue ?? 1
        hold = max(0, slot.raw["freeze_hold_s"]?.numberValue ?? 0)
        frame = slot.raw["freeze_frame_s"]?.numberValue
        end = frame ?? slot.raw["source_end_s"]?.numberValue
            ?? (start + (slot.raw["native_source_span_s"]?.numberValue ?? max(0, (slot.durationS ?? 0) - hold) * rate))
        if frame != nil { hold = max(0, (slot.durationS ?? duration) - movingDuration) }
    }

    func sliced(from lower: Double, to upper: Double) -> Self {
        var result = self
        result.start = max(0, start + min(lower, movingDuration) * rate)
        result.end = max(result.start, start + min(upper, movingDuration) * rate)
        result.hold = max(0, upper - max(lower, movingDuration))
        if result.end - result.start < 0.000001, result.hold > 0, let frame {
            result.end = frame; result.start = max(0, frame - 1.0 / 30); result.rate = 1
            result.hold = max(0, upper - lower - result.movingDuration)
        }
        return result
    }

    func shifted(by delta: Double, sourceDuration: Double) -> Self {
        var result = self
        let lastSourceTime = max(end, frame ?? end)
        let bounded = min(max(delta, -start), max(0, sourceDuration - lastSourceTime))
        result.start += bounded
        result.end += bounded
        result.frame = frame.map { $0 + bounded }
        return result
    }

    func applying(to slot: EditorTimelineSlot) -> EditorTimelineSlot {
        var result = slot
        result.inS = start; result.durationS = duration; result.durationBeats = nil
        result.raw["source_end_s"] = .number(end)
        result.raw["playback_rate"] = .number(rate)
        result.raw.removeValue(forKey: "native_source_span_s")
        if let frame, hold > 0 {
            result.raw["freeze_frame_s"] = .number(frame)
            result.raw["freeze_hold_s"] = .number(hold)
        } else {
            result.raw["freeze_frame_s"] = .null; result.raw["freeze_hold_s"] = .null
        }
        return result
    }
}
