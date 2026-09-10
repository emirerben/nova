import Foundation

/// Whole-layer transforms sampled in output-canvas coordinates (positive y is down).
/// Reveal glyph/mask drawing is applied separately by the native compositor.
public struct TextTransformSample: Codable, Equatable, Sendable {
    public let alpha: Double
    public let scale: Double
    public let xTranslate: Double
    public let yTranslate: Double
    public let revealProgress: Double
    public let blurPx: Double
    public init(alpha: Double, scale: Double, xTranslate: Double, yTranslate: Double, revealProgress: Double = 1, blurPx: Double = 0) {
        self.alpha = alpha; self.scale = scale; self.xTranslate = xTranslate; self.yTranslate = yTranslate; self.revealProgress = revealProgress; self.blurPx = blurPx
    }
    private enum CodingKeys: String, CodingKey { case alpha, scale, xTranslate, yTranslate, revealProgress, blurPx }
    public init(from decoder: Decoder) throws {
        let c = try decoder.container(keyedBy: CodingKeys.self)
        alpha = try c.decode(Double.self, forKey: .alpha); scale = try c.decode(Double.self, forKey: .scale)
        xTranslate = try c.decode(Double.self, forKey: .xTranslate); yTranslate = try c.decode(Double.self, forKey: .yTranslate)
        revealProgress = try c.decodeIfPresent(Double.self, forKey: .revealProgress) ?? 1
        blurPx = try c.decodeIfPresent(Double.self, forKey: .blurPx) ?? 0
    }
}

public enum TextTransformTiming {
    public static func sample(effect: PortableTextEffect, text: String, localTime: Double,
                              duration: Double, motion: TextMotionParameters?) throws -> TextTransformSample {
        guard duration.isFinite, duration > 0, localTime.isFinite else { throw RecipeError.invalidTimeline }
        // The current cloud renderer treats slide-in as a static hold.
        if effect == .staggeredSlice || effect == .dissolveOut || effect == .slideIn { return TextTransformSample(alpha: 1, scale: 1, xTranslate: 0, yTranslate: 0, revealProgress: 1) }
        guard let motion else { return try legacySample(effect: effect, localTime: localTime, duration: duration) }
        let time = try TextMotionTiming.authoredTime(effect: effect, text: text, localTime: localTime, motion: motion)
        let base = try TextMotionTiming.settleDuration(effect: effect, text: text, motion: motion) * motion.speed
        var alpha = 1.0, scale = 1.0, x = 0.0, y = 0.0, reveal = 1.0, blur = 0.0
        func ease(_ value: Double) -> Double { TextMotionTiming.ease(value, motion.easing) }
        switch effect {
        case .static, .none, .typewriter, .streamIn: break
        case .smoothType:
            let state = try TextMotionTiming.smoothType(text: text, localTime: localTime, motion: motion)
            alpha = state.alpha; x = state.xTranslate; y = state.yTranslate
            reveal = state.revealProgress; blur = state.blurPx
        case .inkReveal, .handwriting: reveal = ease(inkRevealProgress(time: time, duration: base))
        case .fadeIn: alpha = ease(time / max(base, 0.01))
        case .scaleUp: scale = 0.6 + 0.4 * ease(time / max(base, 0.01))
        case .slideUp, .slideDown:
            let distance = motion.travelPx * (1 - ease(time / base))
            switch motion.direction {
            case .left: x = -distance
            case .right: x = distance
            case .up: y = -distance
            case .down: y = distance
            case .none: break
            }
        case .popIn:
            if time < 0.15 { scale = 0.30 + 0.85 * time / 0.15 }
            else if time < 0.25 { scale = 1.15 - 0.15 * (time - 0.15) / 0.10 }
            if scale > 1 { scale = 1 + (scale - 1) * motion.overshoot / 0.15 }
        case .bounce:
            let p = time / base
            if p < 0.36 { scale = 1 + 0.25 * p / 0.36 }
            else if p < 0.72 { scale = 1.25 - 0.35 * (p - 0.36) / 0.36 }
            else if p < 1 { scale = 0.90 + 0.10 * (p - 0.72) / 0.28 }
            if scale > 1 { scale = 1 + (scale - 1) * motion.overshoot / 0.15 }
        default: throw MediaEngineError.unsupportedCapability
        }
        if effect != .smoothType {
            scale = 1 + (scale - 1) * motion.intensity
            alpha = 1 - (1 - alpha) * motion.intensity
            x *= motion.intensity; y *= motion.intensity
            reveal = 1 - (1 - reveal) * motion.intensity
        }
        if motion.exitS > 0 {
            let exit = min(duration, TextMotionTiming.roundOutputFrame(motion.exitS))
            if localTime >= duration - exit {
                alpha *= 1 - TextMotionTiming.ease((localTime - duration + exit) / max(exit, 1e-6), .easeInOutCubic)
            }
        }
        return TextTransformSample(alpha: alpha, scale: scale, xTranslate: x, yTranslate: y, revealProgress: reveal, blurPx: blur)
    }

    /// Legacy curves use the authored layer duration, including compressed
    /// pop keyframes and the shorter entrance window on brief slides/bounces.
    private static func legacySample(effect: PortableTextEffect, localTime time: Double,
                                     duration: Double) throws -> TextTransformSample {
        var alpha = 1.0, scale = 1.0, y = 0.0, reveal = 1.0
        func ease(_ progress: Double) -> Double { TextMotionTiming.ease(progress, .easeOutCubic) }
        switch effect {
        case .static, .none, .typewriter, .streamIn, .smoothType: break
        case .inkReveal, .handwriting: reveal = inkRevealProgress(time: time, duration: duration)
        case .fadeIn: alpha = ease(time / max(min(0.4, duration), 0.01))
        case .scaleUp: scale = 0.6 + 0.4 * ease(time / max(min(0.6, duration), 0.01))
        case .slideUp, .slideDown:
            let distance = 220 * (1 - ease(time / min(0.35, duration * 0.5)))
            y = effect == .slideUp ? -distance : distance
        case .popIn:
            let ratio = min(1, duration / 0.25)
            let peak = 0.15 * ratio, end = 0.25 * ratio
            if time <= 0 { scale = 0.30 }
            else if time < peak { scale = 0.30 + 0.85 * time / peak }
            else if time < end { scale = 1.15 - 0.15 * (time - peak) / (end - peak) }
        case .bounce:
            let p = time / min(0.5, duration * 0.8)
            if p < 0.36 { scale = 1 + 0.25 * p / 0.36 }
            else if p < 0.72 { scale = 1.25 - 0.35 * (p - 0.36) / 0.36 }
            else if p < 1 { scale = 0.90 + 0.10 * (p - 0.72) / 0.28 }
        default: throw MediaEngineError.unsupportedCapability
        }
        return TextTransformSample(alpha: alpha, scale: scale, xTranslate: 0, yTranslate: y, revealProgress: reveal)
    }


    /// Mirrors text_animation_math.handwriting_progress, including its short-window
    /// compression and bounded Newton/bisection solver for CSS ease.
    private static func inkRevealProgress(time: Double, duration: Double) -> Double {
        let ratio = min(1, duration / 2.2)
        let progress = (max(0, time) - 0.2 * ratio) / max(0.001, 2 * ratio)
        let target = min(1, max(0, progress))
        if target <= 0 || target >= 1 { return target }
        func sample(_ a: Double, _ b: Double, _ u: Double) -> Double {
            let inverse = 1 - u
            return 3 * a * inverse * inverse * u + 3 * b * inverse * u * u + u * u * u
        }
        var u = target
        for _ in 0..<8 {
            let error = sample(0.25, 0.25, u) - target
            if abs(error) < 1e-6 { return sample(0.1, 1, u) }
            let inverse = 1 - u
            let derivative = 3 * 0.25 * inverse * inverse + 3 * 0.75 * u * u
            if abs(derivative) < 1e-6 { break }
            u = min(1, max(0, u - error / derivative))
        }
        var lower = 0.0, upper = 1.0
        u = target
        for _ in 0..<12 {
            if sample(0.25, 0.25, u) < target { lower = u } else { upper = u }
            u = (lower + upper) / 2
        }
        return sample(0.1, 1, u)
    }

}
