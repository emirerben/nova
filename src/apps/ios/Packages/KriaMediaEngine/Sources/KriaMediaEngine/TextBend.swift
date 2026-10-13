import CoreGraphics
import Foundation

/// KRI-646: arc ("rainbow") text. Pure geometry, shared by the video painter
/// (`PortableTextVectorPainter`) and the slide-post canvas.
///
/// Everything is in y-UP coordinates (CoreGraphics bitmap space). `bend` is -100...100:
/// +100 curves the widest line over a 180 degree arc bowing up (a rainbow), -100 bows down
/// (a smile), 0 is straight. All lines share one centre (concentric arcs), so a multi-line
/// block curves as one shape. Glyphs keep their shape and are rotated to follow the arc; the
/// apex stays at the block's original height, so dragging the slider does not make the text
/// jump.
public enum TextBend {
    /// Below this magnitude the text is treated as straight.
    public static let straightEpsilon = 0.5

    public struct Glyph: Equatable, Sendable {
        /// Left edge of the glyph's advance box, on the baseline.
        public var x: Double
        public var advance: Double
        public init(x: Double, advance: Double) { self.x = x; self.advance = advance }
    }

    public struct Line: Equatable, Sendable {
        public var baselineY: Double
        public var fontSize: Double
        public var glyphs: [Glyph]
        public init(baselineY: Double, fontSize: Double, glyphs: [Glyph]) {
            self.baselineY = baselineY; self.fontSize = fontSize; self.glyphs = glyphs
        }
    }

    public static func isStraight(_ bend: Double?) -> Bool {
        guard let bend, bend.isFinite else { return true }
        return abs(bend) < straightEpsilon
    }

    /// One transform per glyph, in the same order as `lines[i].glyphs`. Apply it to the glyph
    /// drawn at its ORIGINAL position (it rotates about the glyph's baseline centre and moves
    /// that point onto the arc). Returns identities for straight text.
    public static func transforms(lines: [Line], bend: Double) -> [[CGAffineTransform]] {
        let identity: [[CGAffineTransform]] = lines.map { Array(repeating: .identity, count: $0.glyphs.count) }
        guard !isStraight(bend), bend.isFinite else { return identity }
        let spans: [(min: Double, max: Double)?] = lines.map { line in
            guard let first = line.glyphs.first else { return nil }
            return line.glyphs.reduce((first.x, first.x + first.advance)) { (min($0.0, $1.x), max($0.1, $1.x + $1.advance)) }
        }
        guard let widest = spans.compactMap({ $0 }).max(by: { ($0.max - $0.min) < ($1.max - $1.min) }),
              widest.max - widest.min > 0 else { return identity }
        let width = widest.max - widest.min
        let magnitude = min(abs(bend), 100) / 100
        let sign: Double = bend > 0 ? 1 : -1
        // The widest line spans `magnitude * 180` degrees.
        let radius = width / (magnitude * .pi)
        let baselines = lines.map(\.baselineY)
        let midY = ((baselines.min() ?? 0) + (baselines.max() ?? 0)) / 2
        let centerX = (widest.min + widest.max) / 2
        let centerY = midY - sign * radius
        return lines.map { line in
            // A line closer to the centre than half its font size would fold over itself.
            let lineRadius = max(radius + sign * (line.baselineY - midY), line.fontSize * 0.5)
            return line.glyphs.map { glyph in
                let baselineCenter = CGPoint(x: glyph.x + glyph.advance / 2, y: line.baselineY)
                let phi = (baselineCenter.x - centerX) / lineRadius
                let target = CGPoint(x: centerX + lineRadius * sin(phi), y: centerY + sign * lineRadius * cos(phi))
                // Up-bow: glyphs on the right lean clockwise (negative angle in y-up space).
                // `concatenating` applies left to right: centre the glyph, rotate, move onto the arc.
                return CGAffineTransform(translationX: -baselineCenter.x, y: -baselineCenter.y)
                    .concatenating(CGAffineTransform(rotationAngle: -sign * phi))
                    .concatenating(CGAffineTransform(translationX: target.x, y: target.y))
            }
        }
    }
}
