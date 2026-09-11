import Foundation

public struct TextRevealSample: Codable, Equatable, Sendable {
    public let visibleText: String
    public let showCursor: Bool
    public let cursorStyle: TextMotionParameters.Cursor
}

/// Discrete prefix decisions for the glyph painter. Legacy slicing deliberately
/// uses Unicode scalars; v2 uses graphemes, matching the production dispatcher.
public enum TextRevealTiming {
    private static func whitespace(_ scalar: Unicode.Scalar) -> Bool {
        scalar.properties.isWhitespace || (0x1c...0x1f).contains(scalar.value)
    }

    public static func normalized(_ text: String) -> String {
        text.components(separatedBy: "\n").map { line in
            line.unicodeScalars.split(whereSeparator: whitespace).map(String.init).joined(separator: " ")
        }.joined(separator: "\n")
    }

    public static func sample(effect: PortableTextEffect, text: String, localTime: Double,
                              start: Double = 0, schedule: [Double]? = nil,
                              motion: TextMotionParameters?) throws -> TextRevealSample {
        guard localTime.isFinite, start.isFinite, schedule?.allSatisfy(\.isFinite) ?? true,
              effect == .typewriter || effect == .streamIn else { throw RecipeError.invalidTimeline }
        let time = try motion.map { try TextMotionTiming.authoredTime(effect: effect, text: text, localTime: localTime, motion: $0) } ?? localTime
        let full = normalized(text)
        let scalars = Array(full.unicodeScalars)
        var visible = "", cursor = false
        var style: TextMotionParameters.Cursor = .bar
        // Bound before converting to Int; very late seeks simply show all text.
        func steps(_ rate: Double, limit: Int) -> Int {
            Int(min(Double(limit), max(1, (time * rate).rounded(.towardZero) + 1)))
        }
        if effect == .typewriter {
            if motion == nil, let schedule, !schedule.isEmpty {
                let revealed = max(1, schedule.filter {
                    (max(0, $0 - start) * 1000).rounded(.toNearestOrEven) / 1000 <= time
                }.count)
                let count = max(1, Int(ceil(Double(scalars.count) * Double(revealed) / Double(schedule.count))))
                visible = String(String.UnicodeScalarView(scalars.prefix(count)))
            } else if motion != nil {
                visible = String(full.prefix(steps(12, limit: full.count)))
            } else {
                visible = String(String.UnicodeScalarView(scalars.prefix(steps(12, limit: scalars.count))))
            }
            if let motion {
                style = motion.cursorStyle
                cursor = style != .none && visible.unicodeScalars.count < scalars.count && blink(time, milliseconds: motion.cursorBlinkMs)
            }
        } else {
            var wordEnds: [Int] = []
            for index in scalars.indices where !whitespace(scalars[index]) {
                if index + 1 == scalars.count || whitespace(scalars[index + 1]) { wordEnds.append(index + 1) }
            }
            let count = steps(6, limit: wordEnds.count)
            if count > 0 { visible = String(String.UnicodeScalarView(scalars.prefix(wordEnds[count - 1]))) }
            style = motion?.cursorStyle ?? .bar
            cursor = style != .none && count < wordEnds.count && blink(time, milliseconds: motion?.cursorBlinkMs ?? 500)
        }
        if let motion {
            let count = min(full.count, Int(ceil(Double(visible.count) + Double(full.count - visible.count) * (1 - motion.intensity))))
            visible = String(full.prefix(count))
            if count >= full.count { cursor = false }
        }
        return TextRevealSample(visibleText: visible, showCursor: cursor, cursorStyle: style)
    }

    private static func blink(_ time: Double, milliseconds: Double) -> Bool {
        (time * 1000 / milliseconds).rounded(.towardZero).truncatingRemainder(dividingBy: 2) == 0
    }

    /// Full wrapped lines own geometry; this returns their visible prefixes.
    public static func fixedLines(_ lines: [String], visibleText: String) -> (lines: [String], cursorLine: Int) {
        var remaining = visibleText.unicodeScalars.count, cursor = 0
        let visible = lines.enumerated().map { index, line in
            let count = min(line.unicodeScalars.count, remaining)
            if count > 0 { cursor = index }
            remaining = remaining <= line.unicodeScalars.count ? 0 : max(0, remaining - line.unicodeScalars.count - (index < lines.count - 1 ? 1 : 0))
            return String(String.UnicodeScalarView(line.unicodeScalars.prefix(count)))
        }
        return (visible, cursor)
    }
}
