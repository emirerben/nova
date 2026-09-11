import Foundation

public struct DissolveSample: Codable, Equatable, Sendable {
    public let linearProgress: Double
    public let progress: Double
    public let alpha: Double
    public let displacementScale: Double
    public let transformScale: Double
}

/// Timing for the shared production dissolve presets. The text rendering path
/// uses the WebKit cap and the final one-second window independently of text motion.
public enum DissolveTiming {
    public enum Preset: String, Codable, Sendable { case text, media }
    public static func sample(localTime: Double, duration: Double, preset: Preset = .text, capToWebkit: Bool = true) throws -> DissolveSample {
        guard localTime.isFinite, duration.isFinite, duration > 0 else { throw RecipeError.invalidTimeline }
        let total = max(0.001, duration)
        let window = max(0.001, min(1, total))
        let linear = min(1, max(0, (max(0, localTime) - max(0, total - window)) / window))
        let progress = TextMotionTiming.ease(linear, .easeOutCubic)
        let fadeStart = preset == .text ? 0.5 : 0.55
        let alpha = progress <= fadeStart ? 1 : max(0, 1 - (progress - fadeStart) / (1 - fadeStart))
        let displacement = preset == .media ? 700.0 : (capToWebkit ? 920.0 : 2000.0)
        let growth = preset == .media || capToWebkit ? 0.035 : 0.1
        return DissolveSample(linearProgress: linear, progress: progress, alpha: alpha,
                              displacementScale: displacement * progress, transformScale: 1 + growth * progress)
    }

    /// Stable 16-bit particle noise, expanded in 3px text cells (1px media cells)
    /// by the painter. UInt32 wrapping matches the production numpy hash.
    public static func particleNoise(cellX: UInt32, cellY: UInt32, seed: UInt32) -> Float {
        let seedHash = UInt32((UInt64(seed) * 2246822519) % 4294967295)
        var hash = (cellX &* 374761393) &+ (cellY &* 668265263) &+ seedHash
        hash = (hash ^ (hash >> 13)) &* 1274126177
        return Float((hash ^ (hash >> 16)) & 0xFFFF) / 65535
    }

    public static func particleAlpha(sourceAlpha: UInt8, cellX: UInt32, cellY: UInt32, seed: UInt32, progress: Double, preset: Preset = .text) throws -> UInt8 {
        guard progress.isFinite else { throw RecipeError.invalidTimeline }
        let p = min(1, max(0, progress))
        let start = preset == .text ? 0.18 : 0.42
        if p <= start { return sourceAlpha }
        let breakup = Float(min(1, max(0, (p - start) / 0.72)))
        let keep = min(1, max(0, (particleNoise(cellX: cellX, cellY: cellY, seed: seed) - breakup * 0.82) / 0.18))
        return UInt8(min(255, max(0, Float(sourceAlpha) * keep)))
    }
}
