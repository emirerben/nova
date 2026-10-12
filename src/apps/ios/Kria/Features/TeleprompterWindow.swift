import CoreGraphics
import Foundation

enum TeleprompterWindowAppearance: String, Codable, CaseIterable, Equatable, Sendable {
    case transparent
    case glass
    case solid
}

/// Persisted independently from pixels so a window remains useful after rotation or a new device size.
struct TeleprompterWindowPreference: Codable, Equatable, Sendable {
    var appearance: TeleprompterWindowAppearance
    var normalizedX: CGFloat
    var normalizedY: CGFloat
    var widthFraction: CGFloat
    var preferredHeight: CGFloat

    init(appearance: TeleprompterWindowAppearance = .transparent, normalizedX: CGFloat = 0.5,
         normalizedY: CGFloat = 0, widthFraction: CGFloat = 1, preferredHeight: CGFloat = 144) {
        self.appearance = appearance
        self.normalizedX = Self.unit(normalizedX, fallback: 0.5)
        self.normalizedY = Self.unit(normalizedY, fallback: 0)
        self.widthFraction = Self.bounded(widthFraction, lower: 0.35, upper: 1, fallback: 1)
        self.preferredHeight = Self.bounded(preferredHeight, lower: 96, upper: 320, fallback: 144)
    }

    private enum CodingKeys: String, CodingKey { case appearance, normalizedX, normalizedY, widthFraction, preferredHeight }
    init(from decoder: Decoder) throws {
        let values = try decoder.container(keyedBy: CodingKeys.self)
        let appearance = (try? values.decodeIfPresent(TeleprompterWindowAppearance.self, forKey: .appearance)) ?? .transparent
        self.init(appearance: appearance,
                  normalizedX: (try? values.decodeIfPresent(CGFloat.self, forKey: .normalizedX)) ?? 0.5,
                  normalizedY: (try? values.decodeIfPresent(CGFloat.self, forKey: .normalizedY)) ?? 0,
                  widthFraction: (try? values.decodeIfPresent(CGFloat.self, forKey: .widthFraction)) ?? 1,
                  preferredHeight: (try? values.decodeIfPresent(CGFloat.self, forKey: .preferredHeight)) ?? 144)
    }

    private static func unit(_ value: CGFloat, fallback: CGFloat) -> CGFloat {
        guard value.isFinite else { return fallback }
        return min(1, max(0, value))
    }
    private static func finite(_ value: CGFloat, fallback: CGFloat) -> CGFloat {
        guard value.isFinite else { return fallback }
        return max(0, value)
    }
    private static func bounded(_ value: CGFloat, lower: CGFloat, upper: CGFloat, fallback: CGFloat) -> CGFloat {
        guard value.isFinite else { return fallback }
        return min(upper, max(lower, value))
    }
}

enum TeleprompterWindowGeometry {
    static let minimumSize = CGSize(width: 180, height: 96)
    static let maximumHeight: CGFloat = 320

    static func frame(preference: TeleprompterWindowPreference, in canvas: CGRect,
                      minimumHeight: CGFloat = minimumSize.height) -> CGRect {
        guard canvas.width.isFinite, canvas.height.isFinite, canvas.width > 0, canvas.height > 0 else { return .zero }
        let preference = TeleprompterWindowPreference(appearance: preference.appearance,
                                                      normalizedX: preference.normalizedX, normalizedY: preference.normalizedY,
                                                      widthFraction: preference.widthFraction, preferredHeight: preference.preferredHeight)
        let minWidth = min(minimumSize.width, canvas.width)
        let minHeight = min(maximumHeight, min(max(minimumSize.height, finite(minimumHeight)), canvas.height))
        let width = min(canvas.width, max(minWidth, canvas.width * preference.widthFraction))
        let height = min(canvas.height, max(minHeight, min(maximumHeight, preference.preferredHeight)))
        let travelX = max(0, canvas.width - width)
        let travelY = max(0, canvas.height - height)
        return CGRect(x: canvas.minX + travelX * preference.normalizedX,
                      y: canvas.minY + travelY * preference.normalizedY,
                      width: width, height: height)
    }

    static func moving(_ preference: TeleprompterWindowPreference, in canvas: CGRect, by translation: CGSize,
                       minimumHeight: CGFloat = minimumSize.height) -> TeleprompterWindowPreference {
        let current = frame(preference: preference, in: canvas, minimumHeight: minimumHeight)
        guard current != .zero else { return preference }
        let maxX = max(canvas.minX, canvas.maxX - current.width)
        let maxY = max(canvas.minY, canvas.maxY - current.height)
        let x = min(max(canvas.minX, current.minX + finite(translation.width)), maxX)
        let y = min(max(canvas.minY, current.minY + finite(translation.height)), maxY)
        return Self.preference(from: CGRect(x: x, y: y, width: current.width, height: current.height), in: canvas, appearance: preference.appearance)
    }

    /// Resize from the lower-right grip. The upper-left remains fixed until a smaller canvas forces it inward.
    static func resizing(_ preference: TeleprompterWindowPreference, in canvas: CGRect, by translation: CGSize,
                         minimumHeight: CGFloat = minimumSize.height) -> TeleprompterWindowPreference {
        let current = frame(preference: preference, in: canvas, minimumHeight: minimumHeight)
        guard current != .zero else { return preference }
        let originX = min(max(canvas.minX, current.minX), canvas.maxX - min(current.width, canvas.width))
        let originY = min(max(canvas.minY, current.minY), canvas.maxY - min(current.height, canvas.height))
        let minWidth = min(minimumSize.width, canvas.width)
        let minHeight = min(maximumHeight, min(max(minimumSize.height, finite(minimumHeight)), canvas.height))
        let width = min(canvas.maxX - originX, max(minWidth, current.width + finite(translation.width)))
        let height = min(min(canvas.maxY - originY, maximumHeight), max(minHeight, current.height + finite(translation.height)))
        return Self.preference(from: CGRect(x: originX, y: originY, width: width, height: height), in: canvas, appearance: preference.appearance)
    }

    static func preference(from frame: CGRect, in canvas: CGRect, appearance: TeleprompterWindowAppearance) -> TeleprompterWindowPreference {
        guard canvas.width > 0, canvas.height > 0 else { return .init(appearance: appearance) }
        let width = min(canvas.width, max(minimumSize.width <= canvas.width ? minimumSize.width : canvas.width, frame.width))
        let height = min(canvas.height, max(minimumSize.height <= canvas.height ? minimumSize.height : canvas.height, frame.height))
        let travelX = canvas.width - width
        let travelY = canvas.height - height
        let x = travelX > 0 ? (frame.minX - canvas.minX) / travelX : 0.5
        let y = travelY > 0 ? (frame.minY - canvas.minY) / travelY : 0
        return .init(appearance: appearance, normalizedX: x, normalizedY: y,
                     widthFraction: width / canvas.width, preferredHeight: height)
    }

    private static func finite(_ value: CGFloat) -> CGFloat { value.isFinite ? value : 0 }
}
