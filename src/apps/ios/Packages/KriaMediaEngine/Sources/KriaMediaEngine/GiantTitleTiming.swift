import Foundation

public struct GiantTitleTransition: Codable, Equatable, Sendable {
    public let originX: Double
    public let originY: Double
    public init(originX: Double, originY: Double) { self.originX = originX; self.originY = originY }
    private enum CodingKeys: String, CodingKey { case originX, originY }
    public init(from decoder: Decoder) throws {
        try rejectUnknownAssetFields(decoder, allowed: ["originX", "originY"])
        let c = try decoder.container(keyedBy: CodingKeys.self)
        originX = try c.decode(Double.self, forKey: .originX)
        originY = try c.decode(Double.self, forKey: .originY)
        try validate()
    }
    func validate() throws {
        guard [originX, originY].allSatisfy({ $0.isFinite && abs($0) <= 30000 }) else { throw RecipeError.invalidTimeline }
    }
}

public enum GiantTitleTiming {
    public struct Sample: Equatable, Sendable { public let scale: Double; public let alpha: Double }
    public static func sample(localTime: Double, duration: Double) throws -> Sample {
        guard localTime.isFinite, duration.isFinite, duration > 0, duration <= 1800 else { throw RecipeError.invalidTimeline }
        let progress = (localTime - duration * 0.68) / max(0.01, (duration - duration * 0.68))
        let scale = 1 + 59 * bezier(progress)
        let fade = max(0, min(1, (progress - 0.8) / (1 - 0.8)))
        let alpha = progress <= 0.8 ? 1 : 1 - (1 - pow(1 - fade, 3))
        return Sample(scale: scale, alpha: floor(max(0, min(255, 255 * alpha))) / 255)
    }
    // Same bounded Newton/bisection solver as production motion_cubic_bezier.
    private static func bezier(_ progress: Double) -> Double {
        let target = max(0, min(1, progress))
        if target <= 0 { return 0 }
        if target >= 1 { return 1 }
        func sample(_ a: Double, _ b: Double, _ u: Double) -> Double {
            let inverse = 1 - u
            return 3 * a * inverse * inverse * u + 3 * b * inverse * u * u + u * u * u
        }
        func x(_ u: Double) -> Double { sample(0.76, 0.24, u) }
        func y(_ u: Double) -> Double { sample(0, 1, u) }
        var u = target
        for _ in 0..<8 {
            let error = x(u) - target
            if abs(error) < 1e-6 { return y(u) }
            let inverse = 1 - u
            let derivative = 3 * 0.76 * inverse * inverse + 6 * (0.24 - 0.76) * inverse * u + 3 * (1 - 0.24) * u * u
            if abs(derivative) < 1e-6 { break }
            u = max(0, min(1, u - error / derivative))
        }
        var lower = 0.0, upper = 1.0
        u = target
        for _ in 0..<12 {
            if x(u) < target { lower = u } else { upper = u }
            u = (lower + upper) / 2
        }
        return y(u)
    }
}
