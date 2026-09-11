import Foundation

/// A line's measured clipping rectangle in the cloud's top-left canvas coordinates.
/// Direction is resolved by the planner from the first strong Unicode bidi class,
/// before font shaping. Each line must be masked separately, including its shadow.
public struct SmoothTypeClip: Equatable, Sendable {
    public let left: Double
    public let top: Double
    public let right: Double
    public let bottom: Double

    public init(left: Double, top: Double, right: Double, bottom: Double) throws {
        guard [left, top, right, bottom].allSatisfy({ $0.isFinite && abs($0) <= 20000 }),
              right >= left, bottom > top else { throw RecipeError.invalidTimeline }
        self.left = left; self.top = top; self.right = right; self.bottom = bottom
    }

    /// Nil means the settled line is unmasked. Zero progress is an empty mask.
    public func revealed(progress: Double, order: TextMotionParameters.Order, firstStrongRTL: Bool) throws -> Self? {
        guard progress.isFinite, (0...1).contains(progress) else { throw RecipeError.invalidTimeline }
        if progress == 1 { return nil }
        let width = (right - left) * progress
        let start: Double
        switch order {
        case .centerOut: start = (left + right - width) / 2
        case .forward: start = firstStrongRTL ? right - width : left
        case .reverse: start = firstStrongRTL ? left : right - width
        }
        return try Self(left: start, top: top, right: start + width, bottom: bottom)
    }
}
