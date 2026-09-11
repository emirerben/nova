import Foundation

/// FFmpeg xfade's progress runs from one to zero. Public sampling uses elapsed
/// progress instead, like the rest of the native timeline.
public enum ClipTransitionTiming {
    public struct Weights: Equatable, Sendable {
        public let outgoing: Double
        public let incoming: Double
        public let background: Double
    }

    public static func fadeWeights(progress: Double) -> Weights {
        let p = Float(1 - min(1, max(0, progress.isFinite ? progress : 0)))
        func smoothstep(_ low: Float, _ high: Float, _ value: Float) -> Float {
            let t = min(1, max(0, (value - low) / (high - low)))
            return t * t * (3 - 2 * t)
        }
        let outgoing = p * smoothstep(0.8, 1, p)
        let incoming = (1 - p) * (1 - smoothstep(0.2, 1, p))
        return Weights(outgoing: Double(outgoing), incoming: Double(incoming),
                       background: Double(1 - outgoing - incoming))
    }

    /// Incoming pixels for wipeleft are x > floor(width * remainingProgress).
    /// Wiperight uses x <= floor(width * elapsedProgress), including x=0 at start.
    public static func wipeIncomingRange(width: Int, progress: Double, left: Bool) -> Range<Int> {
        let width = max(0, width)
        let remaining = Float(1 - min(1, max(0, progress.isFinite ? progress : 0)))
        if left {
            let boundary = Int(Float(width) * remaining)
            return min(width, boundary + 1)..<width
        }
        let boundary = Int(Float(width) * (1 - remaining))
        return 0..<min(width, boundary + 1)
    }
}
