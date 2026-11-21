import CoreGraphics
import XCTest

/// Apple's minimum touch target. The app asks for it with `minHeight: 44` / `minWidth: 44`.
let minimumTouchTarget: CGFloat = 44

/// How far under 44 pt a frame read back from the accessibility tree may be and still count as 44.
///
/// A control laid out at a fractional position reads one floating-point step short: the shape picker's 44 pt row
/// read 43.99999999999994 once the approval card above it moved (KRI-1538), and every exact `>= 44` check can
/// trip the same way after any layout change (KRI-1550). A control that is really short misses by far more.
let touchTargetTolerance: CGFloat = 0.1

/// Asserts that a frame's height or width (`size`) is a full 44 pt touch target, allowing only layout rounding.
func assertTouchTarget(
    _ size: CGFloat,
    _ message: @autoclosure () -> String = "",
    file: StaticString = #filePath,
    line: UInt = #line
) {
    // The message is only built on failure: it may read an element (a label), which is another query.
    XCTAssertGreaterThanOrEqual(
        size, minimumTouchTarget - touchTargetTolerance,
        { () -> String in
            let detail = message()
            return "\(size) pt is under the \(Int(minimumTouchTarget)) pt touch target" + (detail.isEmpty ? "" : ": \(detail)")
        }(),
        file: file, line: line
    )
}
