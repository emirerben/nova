import Foundation
import KriaMediaEngine

/// A creator's tilt of one clip or photo, as the editor row stores it in `clips[i].raw["tilt"]`
/// (KRI-1136): `{"rotation_deg": Double, "zoom": Double, "backdrop": {"kind": ...}}`.
///
/// `zoom` (KRI-1169) scales the clip about the canvas centre before the turn: `0.5...3`, 1 as framed, rounded to
/// hundredths. It is absent from the row while 1 and rides the recipe's `transform.scale` as the clip's own framing
/// scale (its letterbox) times the zoom (`recipeScale(framing:)`).
///
/// A `media` backdrop is `{"kind": "media", "asset_id": "<Visuals-pool visual id>"}` on the row and in the recipe
/// alike (KRI-1139); the compiler declares the pool visual that id names.
///
/// `rotationDeg` is clockwise on screen, wrapped to `(-180, 180]` and rounded to hundredths. The row key is
/// omitted when the clip is upright, unzoomed, with a blank backdrop, and a blank backdrop is absent from the row, so an
/// untouched or reset clip is byte-identical to one that was never tilted. Mirrors `app.kria.clip_tilt.ClipTilt`
/// (`row()`) and `app.kria.clip_tilt_row`; `tests/fixtures/clip-tilt-row/v1.json` pins both.
struct ClipTilt: Equatable, Sendable {
    enum Invalid: Error, Equatable { case notAnObject, unknownKey(String), badRotation, badZoom, badBackdrop }

    static let upright = ClipTilt()
    static let quarterTurnDegrees = 90.0
    static let zoomRange: ClosedRange<Double> = 0.5...3

    var rotationDeg: Double { didSet { rotationDeg = Self.wrap(rotationDeg) } }
    var zoom: Double { didSet { zoom = Self.clampZoom(zoom) } }
    var backdrop: ClipBackdrop

    init(rotationDeg: Double = 0, zoom: Double = 1, backdrop: ClipBackdrop = .blank) {
        self.rotationDeg = Self.wrap(rotationDeg)
        self.zoom = Self.clampZoom(zoom)
        self.backdrop = backdrop
    }

    /// Wraps to `(-180, 180]`, rounds to hundredths, no negative zero. A non-finite angle is not an angle: it becomes 0.
    static func wrap(_ degrees: Double) -> Double {
        guard degrees.isFinite else { return 0 }
        let wrapped = remainder(degrees, 360)
        return ((wrapped == -180 ? 180 : wrapped) * 100).rounded(.toNearestOrEven) / 100 + 0
    }

    /// A zoom limited to `0.5...3` and rounded to hundredths. A non-finite zoom is not a zoom: it becomes 1.
    static func clampZoom(_ zoom: Double) -> Double {
        guard zoom.isFinite else { return 1 }
        return (min(max(zoom, zoomRange.lowerBound), zoomRange.upperBound) * 100).rounded(.toNearestOrEven) / 100
    }

    var isIdentity: Bool { rotationDeg == 0 && zoom == 1 && backdrop == .blank }

    /// The `MediaTransform.scale` a compiler sets for a clip whose own framing scale (its letterbox, else 1) is
    /// `framing`: the framing times the zoom, and exactly `framing` while the zoom is 1.
    func recipeScale(framing: Double) -> Double { zoom == 1 ? framing : framing * zoom }

    /// The `MediaTransform.rotationDegrees` a compiler sets (the recipe's own sign, clockwise).
    var recipeRotationDegrees: Double { rotationDeg }

    /// The `TimelineClip.backdrop` a compiler sets: nil for a blank one, like the server.
    var recipeBackdrop: ClipBackdrop? { backdrop == .blank ? nil : backdrop }

    /// This tilt turned a quarter further clockwise.
    func turnedQuarter() -> ClipTilt {
        ClipTilt(rotationDeg: rotationDeg + Self.quarterTurnDegrees, zoom: zoom, backdrop: backdrop)
    }

    // MARK: Row value

    /// The value stored under the row's `tilt` key, or nil when the key must be omitted.
    var rowValue: JSONValue? {
        guard !isIdentity else { return nil }
        var fields: [String: JSONValue] = ["rotation_deg": .number(rotationDeg)]
        if zoom != 1 { fields["zoom"] = .number(zoom) }
        switch backdrop {
        case .blank: break
        case .blur: fields["backdrop"] = .object(["kind": .string("blur")])
        case .color(let hex): fields["backdrop"] = .object(["kind": .string("color"), "color": .string(hex)])
        case .media(let poolID): fields["backdrop"] = .object(["kind": .string("media"), "asset_id": .string(poolID)])
        }
        return .object(fields)
    }

    /// Parses a row value. `nil` and `.null` mean no tilt; anything malformed throws.
    init?(row: JSONValue?) throws {
        guard let row, row != .null else { return nil }
        guard case .object(let fields) = row else { throw Invalid.notAnObject }
        if let unknown = fields.keys.sorted().first(where: { !["rotation_deg", "zoom", "backdrop"].contains($0) }) {
            throw Invalid.unknownKey(unknown)
        }
        var rotation = 0.0
        if let value = fields["rotation_deg"] {
            guard case .number(let degrees) = value, degrees.isFinite else { throw Invalid.badRotation }
            rotation = degrees
        }
        var zoom = 1.0
        if let value = fields["zoom"] {
            guard case .number(let factor) = value, factor.isFinite else { throw Invalid.badZoom }
            let rounded = (factor * 100).rounded(.toNearestOrEven) / 100
            guard Self.zoomRange.contains(rounded) else { throw Invalid.badZoom }
            zoom = rounded
        }
        var backdrop = ClipBackdrop.blank
        if let value = fields["backdrop"], value != .null {
            guard case .object = value,
                  let data = try? JSONEncoder().encode(value),
                  let decoded = try? RecipeJSON.decoder().decode(ClipBackdrop.self, from: data) else { throw Invalid.badBackdrop }
            backdrop = decoded
        }
        self.init(rotationDeg: rotation, zoom: zoom, backdrop: backdrop)
    }
}
