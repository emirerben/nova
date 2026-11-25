#if DEBUG
import Foundation
import MachO
import ObjectiveC
import QuartzCore
import notify

/// Start and stop calls seen for one animation, and which of them are passed on to UIKit.
struct AnimationStartLedger {
    enum Verdict: Equatable { case pass, drop }
    /// Starts passed on and not yet stopped.
    private var open = 0
    /// Second starts that were not passed on. If a stop for one ever comes, it is not passed on either.
    private var dropped = 0
    /// Starts passed on minus stops passed on.
    private(set) var passedOnBalance = 0
    /// Stops that paired with nothing. Passed on untouched, and counted so a run can say it saw one.
    private(set) var unmatchedStops = 0

    mutating func start() -> Verdict {
        if open > 0 { dropped += 1; return .drop }
        open += 1; passedOnBalance += 1
        return .pass
    }

    mutating func stop() -> Verdict {
        if open > 0 { open -= 1; passedOnBalance -= 1; return .pass }
        if dropped > 0 { dropped -= 1; return .drop }
        unmatchedStops += 1; passedOnBalance -= 1
        return .pass
    }
}

/// Debug builds only, and only when a UI test launched the app.
///
/// Core Animation sometimes reports one animation's start twice and its stop once. It happens when the layer is
/// freed a few milliseconds after the animation was added, which SwiftUI does on every keyboard move: both calls
/// come from QuartzCore's `run_animation_callbacks`. UIKit's animation group (`UIViewAnimationState`) and XCTest's
/// "is the app idle" check count starts minus stops, so after a double start the count never returns to zero and
/// the next tap in a UI test waits its full 60 s (KRI-1604).
///
/// The guard does not pass on a second start for an animation that has not stopped. It draws nothing differently
/// and a real animation still has to end before the app counts as idle. Every drop is logged and posted to the
/// test bundle, which prints it in the test log.
enum UITestAnimationStartGuard {
    /// Darwin notification posted for each dropped start; `UITestAnimationGuardLog` in the UI test bundle prints it.
    static let droppedNotification = "com.kria.uitest.animation-start-dropped"
    /// Posted when a stop pairs with no start the guard saw. Passed on untouched; printed so it is not silent.
    static let unmatchedStopNotification = "com.kria.uitest.animation-stop-unmatched"
    /// Posted when the guard moves in front of a hook added after launch (XCTest's). Printed once per launch.
    static let movedNotification = "com.kria.uitest.animation-guard-moved"
    /// Set to `off` by a test that wants the app exactly as UIKit ships it (the red control for the guard).
    static let switchName = "UI_TEST_ANIMATION_START_GUARD"

    private final class Box { var ledger = AnimationStartLedger() }
    private typealias Start = @convention(c) (AnyObject, Selector, CAAnimation?) -> Void
    private typealias Stop = @convention(c) (AnyObject, Selector, CAAnimation?, Bool) -> Void
    nonisolated(unsafe) private static var boxKey: UInt8 = 0
    /// How many times the guard has put itself on the two methods. Only the newest copy acts; older ones pass on.
    nonisolated(unsafe) private static var generation = 0
    nonisolated(unsafe) private static var newestStart: IMP?
    nonisolated(unsafe) private static var newestStop: IMP?
    nonisolated(unsafe) private static var gaveUpMoving = false
    private static let startSelector = NSSelectorFromString("animationDidStart:")
    private static let stopSelector = NSSelectorFromString("animationDidStop:finished:")
    /// A hook that keeps re-hooking would make this loop; eight moves is far more than XCTest's one.
    private static let mostMoves = 8

    /// Called once at launch. The guard goes in when a UI test is driving the app: at once for the app's own
    /// `-ui-testing…` launches, and otherwise when XCTest's support code arrives in the process, which happens
    /// shortly after launch (a few UI tests launch with other arguments).
    static func installIfUITesting() {
        let process = ProcessInfo.processInfo
        guard process.environment[switchName] != "off" else { return }
        if process.arguments.contains(where: { $0.hasPrefix("-ui-testing") }) { install() }
        _dyld_register_func_for_add_image { _, _ in
            guard NSClassFromString("XCTAnimationsIdleNotifier") != nil else { return }
            DispatchQueue.main.async { UITestAnimationStartGuard.install() }
        }
    }

    private static func install() {
        guard generation == 0 else { return }
        guard let stateClass = NSClassFromString("UIViewAnimationState"),
              let startMethod = class_getInstanceMethod(stateClass, startSelector),
              let stopMethod = class_getInstanceMethod(stateClass, stopSelector) else {
            // Loud on purpose: without the guard UI tests go back to waiting 60 s at random, and a retry would hide it.
            fatalError("UITestAnimationStartGuard: UIKit's animation group or its start/stop methods are gone. Update the guard (KRI-1604).")
        }
        hook(startMethod, stopMethod)
        NSLog("UITestAnimationStartGuard: installed")
    }

    /// Puts a new copy of the guard on the two methods, in front of whatever is there now.
    private static func hook(_ startMethod: Method, _ stopMethod: Method) {
        generation += 1
        let mine = generation
        let nextStart = unsafeBitCast(method_getImplementation(startMethod), to: Start.self)
        let nextStop = unsafeBitCast(method_getImplementation(stopMethod), to: Stop.self)
        let start: @convention(block) (AnyObject, CAAnimation?) -> Void = { state, animation in
            // Core Animation calls its delegates on the main thread. Anything else is passed on untouched.
            guard mine == generation, Thread.isMainThread else { nextStart(state, startSelector, animation); return }
            stayOutermost(startMethod, stopMethod)
            if let animation, box(for: animation, create: true)?.ledger.start() == .drop {
                NSLog("UITestAnimationStartGuard: dropped a second start for one animation (%@)", (animation as? CAPropertyAnimation)?.keyPath ?? "-")
                notify_post(droppedNotification)
                return
            }
            nextStart(state, startSelector, animation)
        }
        let stop: @convention(block) (AnyObject, CAAnimation?, Bool) -> Void = { state, animation, finished in
            guard mine == generation, Thread.isMainThread else { nextStop(state, stopSelector, animation, finished); return }
            stayOutermost(startMethod, stopMethod)
            if let animation, let found = box(for: animation, create: false) {
                let before = found.ledger.unmatchedStops
                if found.ledger.stop() == .drop {
                    NSLog("UITestAnimationStartGuard: dropped the stop that pairs with a dropped start")
                    return
                }
                if found.ledger.unmatchedStops > before {
                    NSLog("UITestAnimationStartGuard: a stop with no start, passed on")
                    notify_post(unmatchedStopNotification)
                }
            }
            nextStop(state, stopSelector, animation, finished)
        }
        let startIMP = imp_implementationWithBlock(start), stopIMP = imp_implementationWithBlock(stop)
        newestStart = startIMP; newestStop = stopIMP
        method_setImplementation(startMethod, startIMP)
        method_setImplementation(stopMethod, stopIMP)
    }

    /// XCTest hooks the same two methods once it attaches, after launch. A hook in front of the guard counts a
    /// duplicate start before the guard can drop it, so when the guard finds something in front it moves back
    /// to the front. The call that found it is still judged by the copy it reached; the ledger rides on the animation.
    private static func stayOutermost(_ startMethod: Method, _ stopMethod: Method) {
        guard method_getImplementation(startMethod) != newestStart || method_getImplementation(stopMethod) != newestStop else { return }
        guard generation < mostMoves else {
            if !gaveUpMoving { gaveUpMoving = true; NSLog("UITestAnimationStartGuard: something keeps hooking in front of the guard; it stays where it is") }
            return
        }
        hook(startMethod, stopMethod)
        NSLog("UITestAnimationStartGuard: moved in front of a hook added after launch (move %d)", generation - 1)
        notify_post(movedNotification)
    }

    private static func box(for animation: CAAnimation, create: Bool) -> Box? {
        if let found = objc_getAssociatedObject(animation, &boxKey) as? Box { return found }
        guard create else { return nil }
        let made = Box()
        objc_setAssociatedObject(animation, &boxKey, made, .OBJC_ASSOCIATION_RETAIN_NONATOMIC)
        return made
    }
}
#endif
