import CoreGraphics

/// Pure math shared by drag gestures. The release spring itself is `DrawerMotion.settle`.
enum KriaGesturePhysics {
    /// Resistance past a soft edge: follows the finger at first, then slows toward `limit`.
    /// `overshoot` is how far past the edge the finger has gone; the result is how far the content moves.
    static func rubberBand(_ overshoot: CGFloat, limit: CGFloat) -> CGFloat {
        guard overshoot > 0, limit > 0 else { return 0 }
        return limit * overshoot / (overshoot + limit)
    }

    /// Slope of `rubberBand` at `overshoot`: content speed per unit of finger speed.
    static func rubberBandSlope(_ overshoot: CGFloat, limit: CGFloat) -> CGFloat {
        guard overshoot > 0, limit > 0 else { return overshoot > 0 ? 0 : 1 }
        let denominator = overshoot + limit
        return limit * limit / (denominator * denominator)
    }

    /// Release speed in `DrawerMotion.settle` terms: points per second as a fraction of the
    /// distance still to travel. Zero when there is nothing to travel or the speed is not finite.
    static func unitVelocity(_ pointsPerSecond: CGFloat, from current: CGFloat, to target: CGFloat) -> CGFloat {
        let remaining = target - current
        guard pointsPerSecond.isFinite, abs(remaining) > 0.5 else { return 0 }
        return pointsPerSecond / remaining
    }
}
