import Foundation

public struct StaggeredSliceGlyphSample: Codable, Equatable, Sendable {
    public let grapheme: String
    public let opacity: Double
    public let translateYEm: Double
    public let rotateDeg: Double
}
public struct StaggeredSliceLineSample: Codable, Equatable, Sendable {
    public let text: String
    public let kind: String
    public let glyphs: [StaggeredSliceGlyphSample]
}
public struct StaggeredSliceSample: Codable, Equatable, Sendable {
    public let lines: [StaggeredSliceLineSample]
    public let settled: Bool
    public let settleS: Double
}

/// The production effect has a first-word build, a delayed remainder, then
/// subsequent lines. Short legacy windows compress the complete choreography.
public enum StaggeredSliceTiming {
    public static func sample(text: String, localTime: Double, duration: Double, motion: TextMotionParameters?) throws -> StaggeredSliceSample {
        guard localTime.isFinite, duration.isFinite, duration > 0,
              text.unicodeScalars.count <= 5000 else { throw RecipeError.invalidTimeline }
        let normalized = TextRevealTiming.normalized(text)
        let logicalLines = normalized.components(separatedBy: "\n")
        let choreography = logicalLines.count == 1 ? 1.35 : 1.5 + Double(max(0, logicalLines.count - 2)) * 0.12 + 0.35
        let settle = min(2.4, choreography)
        let authored = try motion.map { try TextMotionTiming.authoredTime(effect: .staggeredSlice, text: normalized, localTime: localTime, motion: $0) } ?? localTime
        let time = max(0, authored) / (max(0.01, min(motion == nil ? duration : settle, settle)) / choreography)
        let intensity = motion?.intensity ?? 1
        let lines = logicalLines.enumerated().map { lineIndex, line in
            let graphemes = Array(line)
            let firstCount = graphemes.firstIndex(where: \.isWhitespace) ?? graphemes.count
            let remainderCount = max(0, graphemes.count - firstCount)
            let firstStagger = firstCount <= 1 ? 0 : min(0.07, (0.55 - 0.16) / Double(firstCount - 1))
            let remainderStagger = remainderCount <= 1 ? 0 : (1.35 - 0.95 - 0.16) / Double(remainderCount - 1)
            let laterStagger = graphemes.count <= 1 ? 0 : (0.35 - 0.16) / Double(graphemes.count - 1)
            let glyphs = graphemes.enumerated().map { index, grapheme in
                let start: Double
                if lineIndex == 0 {
                    start = index < firstCount ? Double(index) * firstStagger : 0.95 + Double(index - firstCount) * remainderStagger
                } else {
                    start = 1.5 + Double(lineIndex - 1) * 0.12 + Double(index) * laterStagger
                }
                let progress = TextMotionTiming.ease((time - start) / 0.16, .easeOutCubic)
                let remaining = (1 - progress) * intensity
                return StaggeredSliceGlyphSample(grapheme: String(grapheme), opacity: 1 - remaining,
                                                 translateYEm: 0.18 * remaining, rotateDeg: (index % 2 == 0 ? -4 : 4) * remaining)
            }
            return StaggeredSliceLineSample(text: line, kind: "glyphs", glyphs: glyphs)
        }
        return StaggeredSliceSample(lines: lines, settled: time + 1e-9 >= choreography, settleS: settle)
    }
}
