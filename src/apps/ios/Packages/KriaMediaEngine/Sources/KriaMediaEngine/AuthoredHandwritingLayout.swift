#if canImport(AVFoundation)
import Foundation

/// Uses the same bundled centerline glyphs and pen pauses as handwriting_strokes.py.
public struct AuthoredHandwritingLayout: Sendable {
    private struct Glyph: Decodable, Sendable { let advance: Double; let paths: [[[Double]]] }
    private struct Asset: Decodable, Sendable {
        let ascent: Double; let descent: Double; let stroke_width: Double; let glyphs: [String: Glyph]
    }
    private let asset: Asset
    public init(url: URL) throws { asset = try JSONDecoder().decode(Asset.self, from: Data(contentsOf: url)) }

    public func compile(id: String, text: String, start: Double, end: Double,
                        style: AuthoredTextLayout.Style, canvas: Canvas,
                        motion: TextMotionParameters? = nil, lineSpacing: Double = 1.15) throws -> PortableTextLayer {
        func glyph(_ char: Unicode.Scalar) -> Glyph { asset.glyphs[String(char)] ?? asset.glyphs["?"]! }
        let tracking = 0.035 + style.letterSpacing
        func width(_ text: String) -> Double {
            max(0, text.unicodeScalars.reduce(0) { $0 + glyph($1).advance + tracking } - (text.isEmpty ? 0 : tracking))
        }
        let maxWidth = max(0.1, Double(canvas.width) * style.widthFraction / style.size)
        var lines: [String] = []
        for raw in text.replacingOccurrences(of: "\r\n", with: "\n").replacingOccurrences(of: "\r", with: "\n").components(separatedBy: "\n") {
            if !style.wrapsLines { lines.append(raw); continue }
            var current = ""
            for word in raw.split(whereSeparator: \.isWhitespace).map(String.init) {
                let candidate = current.isEmpty ? word : current + " " + word
                if width(candidate) <= maxWidth { current = candidate; continue }
                if !current.isEmpty { lines.append(current); current = "" }
                if width(word) <= maxWidth { current = word; continue }
                for scalar in word.unicodeScalars {
                    let next = current + String(scalar)
                    if !current.isEmpty && width(next) > maxWidth { lines.append(current); current = String(scalar) }
                    else { current = next }
                }
            }
            lines.append(current)
        }
        let lineStep = (asset.ascent + asset.descent) * max(0.5, lineSpacing)
        let height = asset.ascent + asset.descent + lineStep * Double(max(0, lines.count - 1))
        let anchorX = Double(canvas.width) * style.xFraction, anchorY = Double(canvas.height) * style.yFraction
        let top = anchorY - height * style.size / (style.bottomAligned ? 1 : 2)
        var pending: [(points: [TextStrokePoint], start: Double, end: Double)] = []
        var weight = 0.0
        for (lineIndex, line) in lines.enumerated() {
            let lineWidth = width(line) * style.size
            let left: Double = switch style.alignment {
            case .left: anchorX
            case .center: anchorX - lineWidth / 2
            case .right: anchorX - lineWidth
            }
            var x = 0.0
            for scalar in line.unicodeScalars {
                let shape = glyph(scalar)
                if scalar.properties.isWhitespace { weight += 0.14; x += shape.advance + tracking; continue }
                for path in shape.paths where path.count >= 2 {
                    guard path.allSatisfy({ $0.count == 2 && $0.allSatisfy(\.isFinite) }) else { throw RecipeError.invalidTimeline }
                    let length = max(0.045, zip(path, path.dropFirst()).reduce(0) { $0 + hypot($1.1[0] - $1.0[0], $1.1[1] - $1.0[1]) })
                    let points = path.map { TextStrokePoint(x: left + (x + $0[0]) * style.size,
                        y: top + (asset.ascent + Double(lineIndex) * lineStep + $0[1]) * style.size) }
                    pending.append((points, weight, weight + length)); weight += length
                }
                weight += 0.055; x += shape.advance + tracking
            }
            if lineIndex < lines.count - 1 { weight += 0.21 }
        }
        let total = max(weight, 0.000001)
        let content = HandwritingContent(text: text, strokes: pending.map {
            TextPenStroke(points: $0.points, startProgress: $0.start / total, endProgress: $0.end / total)
        }, inkWidth: max(1, asset.stroke_width * style.size), fill: style.color, outline: style.stroke,
        outlineWidth: 2 * style.strokeWidth, blurLayers: style.shadows)
        try content.validate()
        return PortableTextLayer(id: id, start: start, end: end, anchorX: anchorX, anchorY: anchorY,
            rotationDegrees: style.rotation, runs: [], effect: .handwriting, motion: motion, handwriting: content)
    }
}
#endif
