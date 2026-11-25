#if DEBUG
import Foundation

/// STUB, replaced in the next commit: passes everything on, so the tests are seen failing first.
struct AnimationStartLedger {
    enum Verdict: Equatable { case pass, drop }
    private(set) var passedOnBalance = 0
    private(set) var unmatchedStops = 0
    mutating func start() -> Verdict { passedOnBalance += 1; return .pass }
    mutating func stop() -> Verdict { passedOnBalance -= 1; return .pass }
}
#endif
