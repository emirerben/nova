import Foundation

public struct DiscreteRevealLine: Codable, Equatable, Sendable {
    public let text: String
    public let runIndex: Int?
    public let cursorOffsets: [Double]
    public let cursorRun: PositionedTextRun
    public init(text: String, runIndex: Int?, cursorOffsets: [Double], cursorRun: PositionedTextRun) {
        self.text = text; self.runIndex = runIndex; self.cursorOffsets = cursorOffsets; self.cursorRun = cursorRun
    }
    private enum CodingKeys: String, CodingKey { case text, runIndex, cursorOffsets, cursorRun }
    public init(from decoder: Decoder) throws {
        try rejectUnknownAssetFields(decoder, allowed: ["text", "runIndex", "cursorOffsets", "cursorRun"])
        let c = try decoder.container(keyedBy: CodingKeys.self)
        text = try c.decode(String.self, forKey: .text); runIndex = try c.decodeIfPresent(Int.self, forKey: .runIndex)
        cursorOffsets = try c.decode([Double].self, forKey: .cursorOffsets)
        cursorRun = try c.decode(PositionedTextRun.self, forKey: .cursorRun)
    }
}

public struct DiscreteRevealContent: Codable, Equatable, Sendable {
    public let text: String
    public let schedule: [Double]?
    public let lines: [DiscreteRevealLine]
    public init(text: String, schedule: [Double]?, lines: [DiscreteRevealLine]) {
        self.text = text; self.schedule = schedule; self.lines = lines
    }
    private enum CodingKeys: String, CodingKey { case text, schedule, lines }
    public init(from decoder: Decoder) throws {
        try rejectUnknownAssetFields(decoder, allowed: ["text", "schedule", "lines"])
        let c = try decoder.container(keyedBy: CodingKeys.self)
        text = try c.decode(String.self, forKey: .text); schedule = try c.decodeIfPresent([Double].self, forKey: .schedule)
        lines = try c.decode([DiscreteRevealLine].self, forKey: .lines)
    }
    func validate(runs: [PositionedTextRun]) throws {
        guard !text.isEmpty, text.unicodeScalars.count <= 5000, (1...100).contains(lines.count),
              schedule?.count ?? 0 <= 5000, schedule?.allSatisfy(\.isFinite) ?? true,
              lines.compactMap(\.runIndex) == Array(runs.indices) else { throw RecipeError.invalidTimeline }
        for line in lines {
            guard line.text.unicodeScalars.count <= 2000,
                  line.cursorOffsets.count == line.text.unicodeScalars.count + 1,
                  line.cursorOffsets.allSatisfy({ $0.isFinite && abs($0) <= 10000 }),
                  [" |", " _", " ▮"].contains(line.cursorRun.text),
                  line.text.isEmpty == (line.runIndex == nil) else { throw RecipeError.invalidTimeline }
            if let index = line.runIndex {
                guard runs.indices.contains(index), line.text.unicodeScalars.elementsEqual(runs[index].text.unicodeScalars),
                      runs[index].shaped || runs[index].glyphs?.count == line.text.unicodeScalars.count else { throw RecipeError.invalidTimeline }
            }
            try line.cursorRun.validate()
        }
    }

    func visibleRuns(_ runs: [PositionedTextRun], sample: TextRevealSample) -> [PositionedTextRun] {
        let prefixes = TextRevealTiming.fixedLines(lines.map(\.text), visibleText: sample.visibleText)
        var result: [PositionedTextRun] = []
        for (index, line) in lines.enumerated() {
            let prefix = prefixes.lines[index]
            let count = prefix.unicodeScalars.count
            if let runIndex = line.runIndex, !prefix.isEmpty {
                let run = runs[runIndex]
                result.append(run.replacing(text: prefix, glyphs: run.glyphs.map { Array($0.prefix(count)) }))
            }
            // Skia's empty shaped paragraph has a sentinel width and places
            // this cursor outside the canvas; preserve its invisible result.
            if sample.showCursor && index == prefixes.cursorLine && !(line.cursorRun.shaped && prefix.isEmpty) {
                let cursor = line.cursorRun
                result.append(cursor.replacing(text: cursor.text, x: cursor.x + line.cursorOffsets[count], glyphs: cursor.glyphs))
            }
        }
        return result
    }
}

extension PositionedTextRun {
    func replacing(text: String, x: Double? = nil, glyphs: [PositionedGlyph]?) -> Self {
        Self(text: text, fontAssetID: fontAssetID, fontSize: fontSize, x: x ?? self.x, baselineY: baselineY,
             letterSpacing: letterSpacing, shaped: shaped, fill: fill, stroke: stroke, strokeWidth: strokeWidth,
             blurLayers: blurLayers, gradient: gradient, glyphs: glyphs, fontVariations: fontVariations)
    }
}
