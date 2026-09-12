import Foundation

public struct SmoothRevealLine: Codable, Equatable, Sendable {
    public let text: String
    public let runIndex: Int?
    public let bounds: TextRevealBounds?
    public let firstStrongRTL: Bool
    public init(text: String, runIndex: Int?, bounds: TextRevealBounds?, firstStrongRTL: Bool) {
        self.text = text; self.runIndex = runIndex; self.bounds = bounds; self.firstStrongRTL = firstStrongRTL
    }
    private enum CodingKeys: String, CodingKey { case text, runIndex, bounds, firstStrongRTL = "firstStrongRtl" }
    public init(from decoder: Decoder) throws {
        try rejectUnknownAssetFields(decoder, allowed: ["text", "runIndex", "bounds", "firstStrongRtl"])
        let c = try decoder.container(keyedBy: CodingKeys.self)
        text = try c.decode(String.self, forKey: .text)
        runIndex = try c.decodeIfPresent(Int.self, forKey: .runIndex)
        bounds = try c.decodeIfPresent(TextRevealBounds.self, forKey: .bounds)
        firstStrongRTL = try c.decode(Bool.self, forKey: .firstStrongRTL)
    }
}

public struct SmoothRevealContent: Codable, Equatable, Sendable {
    public let text: String
    public let lines: [SmoothRevealLine]
    public init(text: String, lines: [SmoothRevealLine]) { self.text = text; self.lines = lines }
    private enum CodingKeys: String, CodingKey { case text, lines }
    public init(from decoder: Decoder) throws {
        try rejectUnknownAssetFields(decoder, allowed: ["text", "lines"])
        let c = try decoder.container(keyedBy: CodingKeys.self)
        text = try c.decode(String.self, forKey: .text)
        lines = try c.decode([SmoothRevealLine].self, forKey: .lines)
    }
    func validate(runs: [PositionedTextRun]) throws {
        guard !text.isEmpty, text.unicodeScalars.count <= 5000, (1...100).contains(lines.count),
              lines.compactMap(\.runIndex) == Array(runs.indices) else { throw RecipeError.invalidTimeline }
        for line in lines {
            guard line.text.unicodeScalars.count <= 2000, line.text.isEmpty == (line.runIndex == nil),
                  line.text.isEmpty == (line.bounds == nil) else { throw RecipeError.invalidTimeline }
            try line.bounds?.validate()
            if let index = line.runIndex {
                guard runs.indices.contains(index), runs[index].shaped,
                      line.text.unicodeScalars.elementsEqual(runs[index].text.unicodeScalars) else { throw RecipeError.invalidTimeline }
            }
        }
    }
    func clips(localTime: Double, motion: TextMotionParameters?) throws -> [SmoothTypeClip?] {
        guard let motion else { return lines.map { _ in nil } }
        let progresses = try TextMotionTiming.smoothTypeLineProgresses(lines: lines.map(\.text), localTime: localTime, motion: motion)
        return try zip(lines, progresses).map { line, progress in
            guard let bounds = line.bounds else { return nil }
            return try SmoothTypeClip(left: bounds.left, top: bounds.top, right: bounds.right, bottom: bounds.bottom)
                .revealed(progress: progress, order: motion.order, firstStrongRTL: line.firstStrongRTL)
        }
    }
}
