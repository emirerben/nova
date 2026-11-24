import Foundation
import KriaMediaEngine

/// How dark a creator draws one clip or photo, as the editor row stores it in `clips[i].raw["dim"]`: the share of the
/// way to black, `0.01...1` in hundredths (one per whole percent). The engine draws each encoded pixel of the clip's
/// picture at `1 - strength`, under every text, caption, sticker and drawing (`TimelineClip.dim`). No dim omits the
/// key, so a cleared clip is byte-identical to one that was never dimmed. Mirrors `app.kria.clip_dim`;
/// `tests/fixtures/clip-dim-row/v1.json` pins both.
struct ClipDim: Equatable, Sendable {
    enum Invalid: Error, Equatable { case notANumber, outOfRange }

    let strength: Double

    /// A strength rounded to hundredths and limited to 1. Nil when it rounds to 0 (no dim) or is not a number.
    init?(strength: Double) {
        guard strength.isFinite else { return nil }
        let rounded = Self.hundredths(min(strength, 1))
        guard rounded > 0 else { return nil }
        self.strength = rounded
    }

    init?(percent: Int) { self.init(strength: Double(percent) / 100) }

    var percent: Int { Int((strength * 100).rounded()) }

    var rowValue: JSONValue { .number(strength) }

    /// Parses a row value. `nil` and `.null` mean no dim, and so does a strength that rounds to 0; anything else
    /// that is not a number in `0...1` throws.
    init?(row: JSONValue?) throws {
        guard let row, row != .null else { return nil }
        guard case .number(let value) = row, value.isFinite else { throw Invalid.notANumber }
        let rounded = Self.hundredths(value)
        guard (0...1).contains(rounded) else { throw Invalid.outOfRange }
        self.init(strength: rounded)
    }

    private static func hundredths(_ value: Double) -> Double { (value * 100).rounded(.toNearestOrEven) / 100 + 0 }
}
