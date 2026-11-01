import Foundation
import KriaMediaEngine

/// A creator's tilt of one clip or photo, as the editor row stores it in `clips[i].raw["tilt"]`
/// (KRI-1136): `{"rotation_deg": Double, "backdrop": {"kind": ...}}`.
///
/// `rotationDeg` is clockwise on screen, wrapped to `(-180, 180]` and rounded to hundredths. The row key is
/// omitted when the clip is upright with a blank backdrop, and a blank backdrop is absent from the row, so an
/// untouched or reset clip is byte-identical to one that was never tilted. Mirrors `app.kria.clip_tilt.ClipTilt`
/// (`row()`) and `app.kria.clip_tilt_row`; `tests/fixtures/clip-tilt-row/v1.json` pins both.
struct ClipTilt: Equatable, Sendable {
    enum Invalid: Error, Equatable { case notAnObject, unknownKey(String), badRotation, badBackdrop }

    static let upright = ClipTilt()
    static let quarterTurnDegrees = 90.0

    var rotationDeg: Double { didSet { rotationDeg = Self.wrap(rotationDeg) } }
    var backdrop: ClipBackdrop

    init(rotationDeg: Double = 0, backdrop: ClipBackdrop = .blank) {
        self.rotationDeg = Self.wrap(rotationDeg)
        self.backdrop = backdrop
    }

    /// Wraps to `(-180, 180]`, rounds to hundredths, no negative zero. A non-finite angle is not an angle: it becomes 0.
    static func wrap(_ degrees: Double) -> Double {
        guard degrees.isFinite else { return 0 }
        let wrapped = remainder(degrees, 360)
        return ((wrapped == -180 ? 180 : wrapped) * 100).rounded(.toNearestOrEven) / 100 + 0
    }

    var isIdentity: Bool { rotationDeg == 0 && backdrop == .blank }

    /// The `MediaTransform.rotationDegrees` a compiler sets (the recipe's own sign, clockwise).
    var recipeRotationDegrees: Double { rotationDeg }

    /// The `TimelineClip.backdrop` a compiler sets: nil for a blank one, like the server.
    var recipeBackdrop: ClipBackdrop? { backdrop == .blank ? nil : backdrop }

    /// This tilt turned a quarter further clockwise.
    func turnedQuarter() -> ClipTilt {
        ClipTilt(rotationDeg: rotationDeg + Self.quarterTurnDegrees, backdrop: backdrop)
    }

    // MARK: Row value

    /// The value stored under the row's `tilt` key, or nil when the key must be omitted.
    var rowValue: JSONValue? {
        guard !isIdentity else { return nil }
        var fields: [String: JSONValue] = ["rotation_deg": .number(rotationDeg)]
        switch backdrop {
        case .blank: break
        case .blur: fields["backdrop"] = .object(["kind": .string("blur")])
        case .color(let hex): fields["backdrop"] = .object(["kind": .string("color"), "color": .string(hex)])
        }
        return .object(fields)
    }

    /// Parses a row value. `nil` and `.null` mean no tilt; anything malformed throws.
    init?(row: JSONValue?) throws {
        guard let row, row != .null else { return nil }
        guard case .object(let fields) = row else { throw Invalid.notAnObject }
        if let unknown = fields.keys.sorted().first(where: { !["rotation_deg", "backdrop"].contains($0) }) {
            throw Invalid.unknownKey(unknown)
        }
        var rotation = 0.0
        if let value = fields["rotation_deg"] {
            guard case .number(let degrees) = value, degrees.isFinite else { throw Invalid.badRotation }
            rotation = degrees
        }
        var backdrop = ClipBackdrop.blank
        if let value = fields["backdrop"], value != .null {
            guard case .object = value,
                  let data = try? JSONEncoder().encode(value),
                  let decoded = try? RecipeJSON.decoder().decode(ClipBackdrop.self, from: data) else { throw Invalid.badBackdrop }
            backdrop = decoded
        }
        self.init(rotationDeg: rotation, backdrop: backdrop)
    }
}
