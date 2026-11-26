import Foundation
import XCTest
import notify

/// The UI test bundle's principal class: XCTest makes one when the bundle loads.
///
/// In UI-test launches the app drops Core Animation's second "animation started" call for an animation whose
/// layer is already freed, so the app can be seen to go idle (`UITestAnimationStartGuard`, KRI-1604). Each drop
/// is posted here and printed at once, one line per event, so a run's log says how often the guard acted. A line
/// per event survives a test that hits its time limit; a summary printed at the end would not.
@objc(UITestAnimationGuardLog) final class UITestAnimationGuardLog: NSObject {
    private var tokens: [Int32] = []
    private let queue = DispatchQueue(label: "com.kria.uitest.animation-guard-log")

    override init() {
        super.init()
        listen("com.kria.uitest.animation-start-dropped", "ANIMATION-GUARD dropped a second start for an animation whose layer was already freed (KRI-1604)")
        listen("com.kria.uitest.animation-guard-moved", "ANIMATION-GUARD is in front of XCTest's own hook (KRI-1604)")
        listen("com.kria.uitest.animation-stop-dropped", "ANIMATION-GUARD the stop for a dropped start came after all; dropped too (KRI-1604)")
        listen("com.kria.uitest.animation-never-stopped-layer-freed", "ANIMATION-GUARD missed one: an animation was freed with a start never stopped, its layer freed (KRI-1604)")
        listen("com.kria.uitest.animation-never-stopped-layer-live", "ANIMATION-GUARD missed one: an animation was freed with a start never stopped, its layer still there (KRI-1604)")
        listen("com.kria.uitest.animation-stop-unmatched", "ANIMATION-GUARD saw an animation stop with no start; passed on untouched (KRI-1604)")
    }

    private func listen(_ name: String, _ line: String) {
        var token: Int32 = 0
        let status = notify_register_dispatch(name, &token, queue) { _ in print(line) }
        if status == NOTIFY_STATUS_OK { tokens.append(token) } else { print("ANIMATION-GUARD could not listen for \(name): status \(status)") }
    }
}
