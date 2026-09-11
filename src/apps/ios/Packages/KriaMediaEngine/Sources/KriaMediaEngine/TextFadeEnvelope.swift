import Foundation

public struct TextFadeEnvelope: Codable, Equatable, Sendable {
    public enum Kind: String, Codable, Sendable { case lyric, sequence }
    public enum Curve: String, Codable, Sendable { case square, sqrt }
    public let kind: Kind
    public let inMs: Int
    public let outMs: Int
    public let curve: Curve
    public init(kind: Kind, inMs: Int, outMs: Int, curve: Curve) {
        self.kind = kind; self.inMs = inMs; self.outMs = outMs; self.curve = curve
    }
    private enum CodingKeys: String, CodingKey { case kind, inMs, outMs, curve }
    public init(from decoder: Decoder) throws {
        try rejectUnknownAssetFields(decoder, allowed: ["kind", "inMs", "outMs", "curve"])
        let c = try decoder.container(keyedBy: CodingKeys.self)
        kind = try c.decode(Kind.self, forKey: .kind); inMs = try c.decode(Int.self, forKey: .inMs)
        outMs = try c.decode(Int.self, forKey: .outMs); curve = try c.decode(Curve.self, forKey: .curve)
    }
    func validate() throws {
        guard (0...1_800_000).contains(inMs), (0...1_800_000).contains(outMs),
              kind != .sequence || inMs == 0 else { throw RecipeError.invalidTimeline }
    }
    public func alpha(localTime: Double, duration: Double) throws -> Double {
        try validate()
        guard localTime.isFinite, duration.isFinite, duration > 0, duration <= 1800 else { throw RecipeError.invalidTimeline }
        // Python round uses ties-to-even when resolving millisecond windows.
        let durationMs = max(0, Int((duration * 1000).rounded(.toNearestOrEven)))
        let head = min(inMs, durationMs)
        let tail = min(outMs, max(0, durationMs - head))
        let time = max(0, min(localTime, duration)) * 1000
        if head > 0 && time < Double(head) { return sqrt(time / Double(head)) }
        if tail > 0 && time >= Double(durationMs - tail) {
            let progress = (time - Double(durationMs - tail)) / Double(tail)
            return max(0, 1 - (curve == .sqrt ? sqrt(progress) : progress * progress))
        }
        return 1
    }
}
