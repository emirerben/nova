import Foundation

public struct StaggeredGlyph: Codable, Equatable, Sendable {
    public let logicalLine: Int
    public let glyphIndex: Int
    public let run: PositionedTextRun
    public let pivotX: Double
    public let pivotY: Double
    private enum CodingKeys: String, CodingKey { case logicalLine, glyphIndex, run, pivotX, pivotY }
    public init(from decoder: Decoder) throws {
        try rejectUnknownAssetFields(decoder, allowed: ["logicalLine", "glyphIndex", "run", "pivotX", "pivotY"])
        let c = try decoder.container(keyedBy: CodingKeys.self)
        logicalLine = try c.decode(Int.self, forKey: .logicalLine); glyphIndex = try c.decode(Int.self, forKey: .glyphIndex)
        run = try c.decode(PositionedTextRun.self, forKey: .run)
        pivotX = try c.decode(Double.self, forKey: .pivotX); pivotY = try c.decode(Double.self, forKey: .pivotY)
    }
}
public struct StaggeredContent: Codable, Equatable, Sendable {
    public let text: String
    public let glyphs: [StaggeredGlyph]
    private enum CodingKeys: String, CodingKey { case text, glyphs }
    public init(from decoder: Decoder) throws {
        try rejectUnknownAssetFields(decoder, allowed: ["text", "glyphs"])
        let c = try decoder.container(keyedBy: CodingKeys.self)
        text = try c.decode(String.self, forKey: .text); glyphs = try c.decode([StaggeredGlyph].self, forKey: .glyphs)
    }
    func validate() throws {
        let lines = text.components(separatedBy: "\n").map(Array.init)
        guard text.unicodeScalars.elementsEqual(TextRevealTiming.normalized(text).unicodeScalars), !text.isEmpty, text.unicodeScalars.count <= 5000, lines.count <= 100, (1...5000).contains(glyphs.count),
              glyphs.reduce(0, { $0 + ($1.run.glyphs?.count ?? 0) }) <= 10000 else { throw RecipeError.invalidTimeline }
        var previousLine = -1, previousIndex = -1
        for glyph in glyphs {
            guard lines.indices.contains(glyph.logicalLine), lines[glyph.logicalLine].indices.contains(glyph.glyphIndex),
                  glyph.glyphIndex <= 4999, !glyph.run.shaped,
                  glyph.run.text.unicodeScalars.elementsEqual(String(lines[glyph.logicalLine][glyph.glyphIndex]).unicodeScalars),
                  glyph.logicalLine > previousLine || (glyph.logicalLine == previousLine && glyph.glyphIndex > previousIndex),
                  [glyph.pivotX, glyph.pivotY].allSatisfy({ $0.isFinite && abs($0) <= 20000 }) else { throw RecipeError.invalidTimeline }
            try glyph.run.validate()
            previousLine = glyph.logicalLine; previousIndex = glyph.glyphIndex
        }
    }
}
