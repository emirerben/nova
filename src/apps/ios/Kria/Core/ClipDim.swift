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

    /// A strength rounded half up to hundredths and limited to 1. Nil below 0.01 (no dim) or when not a number.
    init?(strength: Double) {
        guard strength.isFinite, strength >= 0.01 else { return nil }
        self.strength = Self.hundredths(min(strength, 1))
    }

    init?(percent: Int) { self.init(strength: Double(percent) / 100) }

    var percent: Int { Int((strength * 100).rounded()) }

    var rowValue: JSONValue { .number(strength) }

    /// Parses a row value. `nil`, `.null` and a strength below 0.01 mean no dim; a negative value, anything that is
    /// not a number and a strength that rounds above 1 throw.
    init?(row: JSONValue?) throws {
        guard let row, row != .null else { return nil }
        guard case .number(let value) = row, value.isFinite else { throw Invalid.notANumber }
        guard value >= 0 else { throw Invalid.outOfRange }
        guard value >= 0.01 else { return nil }
        let rounded = Self.hundredths(value)
        guard rounded <= 1 else { throw Invalid.outOfRange }
        strength = rounded
    }

    /// The same IEEE steps as `app.kria.clip_dim`, so both sides land on the same hundredth.
    private static func hundredths(_ value: Double) -> Double { (value * 100 + 0.5).rounded(.down) / 100 }
}
