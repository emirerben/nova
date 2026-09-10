import Foundation

/// Resolved cloud-planner parameters. Defaults and clamping belong to the
/// shared planner; the device validates the complete normalized program.
public struct TextMotionParameters: Codable, Equatable, Sendable {
    public enum Easing: String, Codable, Sendable { case linear, easeOutCubic = "ease-out-cubic", easeInOutCubic = "ease-in-out-cubic" }
    public enum Order: String, Codable, Sendable { case forward, reverse, centerOut = "center-out" }
    public enum Direction: String, Codable, Sendable { case none, up, down, left, right }
    public enum Cursor: String, Codable, Sendable { case none, bar, block, underscore }
    public let speed: Double
    public let intensity: Double
    public let easing: Easing
    public let staggerMs: Double
    public let order: Order
    public let direction: Direction
    public let travelPx: Double
    public let overshoot: Double
    public let blurPx: Double
    public let cursorStyle: Cursor
    public let cursorBlinkMs: Double
    public let holdS: Double
    public let exitS: Double
    public let revealRampMs: Double

    private enum CodingKeys: String, CodingKey { case speed, intensity, easing, staggerMs, order, direction, travelPx, overshoot, blurPx, cursorStyle, cursorBlinkMs, holdS, exitS, revealRampMs }
    public init(from decoder: Decoder) throws {
        try rejectUnknownAssetFields(decoder, allowed: ["speed", "intensity", "easing", "staggerMs", "order", "direction", "travelPx", "overshoot", "blurPx", "cursorStyle", "cursorBlinkMs", "holdS", "exitS", "revealRampMs"])
        let c = try decoder.container(keyedBy: CodingKeys.self)
        speed = try c.decode(Double.self, forKey: .speed)
        intensity = try c.decode(Double.self, forKey: .intensity)
        easing = try c.decode(Easing.self, forKey: .easing)
        staggerMs = try c.decode(Double.self, forKey: .staggerMs)
        order = try c.decode(Order.self, forKey: .order)
        direction = try c.decode(Direction.self, forKey: .direction)
        travelPx = try c.decode(Double.self, forKey: .travelPx)
        overshoot = try c.decode(Double.self, forKey: .overshoot)
        blurPx = try c.decode(Double.self, forKey: .blurPx)
        cursorStyle = try c.decode(Cursor.self, forKey: .cursorStyle)
        cursorBlinkMs = try c.decode(Double.self, forKey: .cursorBlinkMs)
        holdS = try c.decode(Double.self, forKey: .holdS)
        exitS = try c.decode(Double.self, forKey: .exitS)
        revealRampMs = try c.decode(Double.self, forKey: .revealRampMs)
    }

    public func validate() throws {
        let ranges: [(Double, ClosedRange<Double>)] = [
            (speed, 0.25...4), (intensity, 0...1), (staggerMs, 0...250), (travelPx, 0...600),
            (overshoot, 0...1), (blurPx, 0...12), (cursorBlinkMs, 100...2000),
            (holdS, 0...3600), (exitS, 0...2), (revealRampMs, 40...400)
        ]
        guard ranges.allSatisfy({ $0.0.isFinite && $0.1.contains($0.0) }) else { throw RecipeError.invalidTimeline }
    }
}

public enum PortableTextEffect: String, Codable, CaseIterable, Sendable {
    case `static`, none, fadeIn = "fade-in", slideUp = "slide-up", slideDown = "slide-down"
    case lyricLine = "lyric-line", karaokeLine = "karaoke-line", popIn = "pop-in", scaleUp = "scale-up", typewriter
    case streamIn = "stream-in", staggeredSlice = "staggered-slice", inkReveal = "ink-reveal"
    case handwriting, dissolveOut = "dissolve-out", bounce, slideIn = "slide-in", smoothType = "smooth-type"
}

public struct SmoothTypeSample: Codable, Equatable, Sendable {
    public let alpha: Double
    public let xTranslate: Double
    public let yTranslate: Double
    public let blurPx: Double
    public let revealProgress: Double
    public let revealOrigin: TextMotionParameters.Order
    public let settled: Bool
}

/// Mirrors app/pipeline/text_motion_v2.py. Uses composition time and a fixed
/// 30fps phase grid for both preview seeks and offline export.
public enum TextMotionTiming {
    public static let outputFPS = 30.0
    private static func clamp(_ value: Double) -> Double { min(1, max(0, value)) }

    public static func ease(_ progress: Double, _ easing: TextMotionParameters.Easing) -> Double {
        let t = clamp(progress)
        switch easing {
        case .linear: return t
        case .easeOutCubic: return 1 - pow(1 - t, 3)
        case .easeInOutCubic: return t < 0.5 ? 4 * pow(t, 3) : 1 - pow(-2 * t + 2, 3) / 2
        }
    }

    public static func roundOutputFrame(_ time: Double) -> Double { floor(max(0, time) * outputFPS + 0.5) / outputFPS }

    private static func baseDuration(_ effect: PortableTextEffect, _ text: String, _ motion: TextMotionParameters) -> Double {
        switch effect {
        case .smoothType: return max(0.12, (Double(max(1, text.count) - 1) * motion.staggerMs + motion.revealRampMs) / 1000)
        case .typewriter: return max(0.12, Double(text.count) / 12)
        case .streamIn: return max(0.12, Double(text.split(whereSeparator: \.isWhitespace).count) / 6)
        case .staggeredSlice:
            let count = text.components(separatedBy: "\n").count
            return count <= 1 ? 1.35 : min(2.4, 1.5 + Double(max(0, count - 2)) * 0.12 + 0.35)
        case .handwriting, .inkReveal: return 2.2
        case .scaleUp: return 0.6
        case .bounce: return 0.5
        case .fadeIn: return 0.4
        case .slideUp, .slideDown: return 0.35
        case .popIn: return 0.25
        default: return 0
        }
    }

    public static func settleDuration(effect: PortableTextEffect, text: String, motion: TextMotionParameters) throws -> Double {
        try motion.validate()
        return baseDuration(effect, text, motion) / motion.speed
    }

    public static func rendererSettleDuration(effect: PortableTextEffect, text: String, motion: TextMotionParameters) throws -> Double {
        max(1 / outputFPS, roundOutputFrame(try settleDuration(effect: effect, text: text, motion: motion)))
    }

    public static func totalDuration(effect: PortableTextEffect, text: String, motion: TextMotionParameters) throws -> Double {
        try settleDuration(effect: effect, text: text, motion: motion) + motion.holdS + motion.exitS
    }

    public static func authoredTime(effect: PortableTextEffect, text: String, localTime: Double, motion: TextMotionParameters) throws -> Double {
        try motion.validate()
        guard localTime.isFinite else { throw RecipeError.invalidTimeline }
        let base = baseDuration(effect, text, motion)
        if base <= 0 { return max(0, localTime) * motion.speed }
        return max(0, localTime) * (base / (try rendererSettleDuration(effect: effect, text: text, motion: motion)))
    }

    public static func smoothType(text: String, localTime: Double, motion: TextMotionParameters) throws -> SmoothTypeSample {
        let time = try authoredTime(effect: .smoothType, text: text, localTime: localTime, motion: motion)
        let count = max(1, text.count)
        let stagger = motion.staggerMs / 1000
        let ramp = motion.revealRampMs / 1000
        let reveal = min(1, (0..<count).reduce(0.0) { $0 + ease((time - Double($1) * stagger) / max(ramp, 1e-6), motion.easing) } / Double(count))
        let entrance = ease(time / max(baseDuration(.smoothType, text, motion), 1e-6), motion.easing)
        let remaining = 1 - entrance
        let distance = motion.travelPx * motion.intensity * remaining
        let x = motion.direction == .left ? -distance : motion.direction == .right ? distance : 0
        let y = motion.direction == .up ? -distance : motion.direction == .down ? distance : 0
        return SmoothTypeSample(alpha: clamp(1 - motion.intensity * (1 - entrance)), xTranslate: x, yTranslate: y,
                                blurPx: motion.blurPx * motion.intensity * remaining, revealProgress: reveal, revealOrigin: motion.order,
                                settled: max(0, localTime) + 1e-9 >= (try rendererSettleDuration(effect: .smoothType, text: text, motion: motion)))
    }

    public static func smoothTypeLineProgresses(lines: [String], localTime: Double, motion: TextMotionParameters) throws -> [Double] {
        let time = try authoredTime(effect: .smoothType, text: lines.joined(separator: "\n"), localTime: localTime, motion: motion)
        let counts = lines.map(\.count)
        let total = max(1, counts.reduce(0, +) + max(0, lines.count - 1))
        let indices = Array(0..<total)
        let ordered: [Int]
        switch motion.order {
        case .forward: ordered = indices
        case .reverse: ordered = indices.reversed()
        case .centerOut:
            let center = Double(total - 1) / 2
            ordered = indices.sorted {
                let left = abs(Double($0) - center); let right = abs(Double($1) - center)
                return left == right ? $0 < $1 : left < right
            }
        }
        var ranks = Array(repeating: 0, count: total)
        for (rank, index) in ordered.enumerated() { ranks[index] = rank }
        var offset = 0
        return counts.enumerated().map { lineIndex, count in
            defer { offset += count + (lineIndex < counts.count - 1 ? 1 : 0) }
            if count == 0 { return 1 }
            return clamp((0..<count).reduce(0.0) { value, index in
                value + ease((time - Double(ranks[offset + index]) * motion.staggerMs / 1000) / max(motion.revealRampMs / 1000, 1e-6), motion.easing)
            } / Double(count))
        }
    }
}
