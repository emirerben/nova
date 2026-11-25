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

    /// Only a second start whose layer is already freed is dropped: that is the one Core Animation never stops.
    /// A second start on a layer that is still there is how UIKit re-aims a running animation (a sheet); it gets
    /// its own stop, so it is passed on and the app is not called idle while the sheet still moves.
    mutating func start(layerIsGone: Bool) -> Verdict {
        if open > 0, layerIsGone { dropped += 1; return .drop }
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

/// Debug builds only, and only when a UI test is driving the app.
///
/// Core Animation sometimes reports one animation's start twice and its stop once. It happens when the layer is
/// freed a few milliseconds after the animation was added, which SwiftUI does on every keyboard move: both calls
/// come from QuartzCore's `run_animation_callbacks`. UIKit's animation group (`UIViewAnimationState`) and XCTest's
/// "is the app idle" check count starts minus stops, so after such a double start the count never returns to zero
/// and the next tap in a UI test waits its full 60 s (KRI-1604).
///
/// The guard does not pass on a second start for an animation whose layer is already freed. It draws nothing
/// differently, and every animation on a layer that still exists is counted exactly as UIKit reports it. Each
/// drop is logged and posted to the test bundle, which prints it in the test log.
enum UITestAnimationStartGuard {
    /// Darwin notifications; `UITestAnimationGuardLog` in the UI test bundle prints a line for each.
    static let droppedNotification = "com.kria.uitest.animation-start-dropped"
    /// The stop that pairs with a dropped start did come after all, and was dropped too.
    static let droppedStopNotification = "com.kria.uitest.animation-stop-dropped"
    /// A stop paired with no start the guard saw. Passed on untouched; printed so it is not silent.
    static let unmatchedStopNotification = "com.kria.uitest.animation-stop-unmatched"
    /// An animation was freed with a passed-on start that never got its stop: a leak the guard did not catch.
    static let neverStoppedFreedNotification = "com.kria.uitest.animation-never-stopped-layer-freed"
    static let neverStoppedLiveNotification = "com.kria.uitest.animation-never-stopped-layer-live"
    /// The guard moved in front of a hook added after launch (XCTest's). Once per launch.
    static let movedNotification = "com.kria.uitest.animation-guard-moved"
    /// Set to `off` by a test that wants the app exactly as UIKit ships it (the red control for the guard).
    static let switchName = "UI_TEST_ANIMATION_START_GUARD"

    /// Rides on the animation as an associated object, made when the animation is added to its layer.
    private final class Box {
        weak var layer: CALayer?
        let keyPath: String
        var ledger = AnimationStartLedger()
        init(layer: CALayer, keyPath: String) { self.layer = layer; self.keyPath = keyPath }
        /// Runs when the animation itself is freed. A start that was passed on and never stopped is the leak the
        /// guard exists for; one that gets here was not caught, so it is reported with what the guard knows.
        deinit {
            guard ledger.passedOnBalance > 0 else { return }
            let layerGone = layer == nil
            NSLog("UITestAnimationStartGuard: an animation was freed with %d start(s) never stopped (%@, layer %@)",
                  ledger.passedOnBalance, keyPath, layerGone ? "freed" : "still there")
            notify_post(layerGone ? neverStoppedFreedNotification : neverStoppedLiveNotification)
        }
    }
    private typealias Add = @convention(c) (CALayer, Selector, CAAnimation, NSString?) -> Void
    private typealias Start = @convention(c) (AnyObject, Selector, CAAnimation?) -> Void
    private typealias Stop = @convention(c) (AnyObject, Selector, CAAnimation?, Bool) -> Void
    nonisolated(unsafe) private static var boxKey: UInt8 = 0
    /// How many times the guard has put itself on the two methods. Only the newest copy acts; older ones pass on.
    nonisolated(unsafe) private static var generation = 0
    nonisolated(unsafe) private static var newestStart: IMP?
    nonisolated(unsafe) private static var newestStop: IMP?
    nonisolated(unsafe) private static var gaveUpMoving = false
    private static let addSelector = NSSelectorFromString("addAnimation:forKey:")
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
              let stopMethod = class_getInstanceMethod(stateClass, stopSelector),
              let addMethod = class_getInstanceMethod(CALayer.self, addSelector) else {
            // Loud on purpose: without the guard UI tests go back to waiting 60 s at random, and a retry would hide it.
            fatalError("UITestAnimationStartGuard: UIKit's animation group or the methods the guard hooks are gone. Update the guard (KRI-1604).")
        }
        // Remember which layer each UIKit animation was added to; nothing else about adding changes.
        let nextAdd = unsafeBitCast(method_getImplementation(addMethod), to: Add.self)
        let add: @convention(block) (CALayer, CAAnimation, NSString?) -> Void = { layer, animation, key in
            nextAdd(layer, addSelector, animation, key)
            guard Thread.isMainThread, let key, let delegate = animation.delegate, (delegate as AnyObject).isKind(of: stateClass),
                  let attached = layer.animation(forKey: key as String) else { return }
            // An animation added again keeps its ledger; only the layer it sits on is refreshed.
            if let known = box(of: attached) { known.layer = layer } else {
                let made = Box(layer: layer, keyPath: (attached as? CAPropertyAnimation)?.keyPath ?? "-")
                objc_setAssociatedObject(attached, &boxKey, made, .OBJC_ASSOCIATION_RETAIN_NONATOMIC)
            }
        }
        method_setImplementation(addMethod, imp_implementationWithBlock(add))
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
            if let animation, let found = box(of: animation), found.ledger.start(layerIsGone: found.layer == nil) == .drop {
                NSLog("UITestAnimationStartGuard: dropped a second start for an animation whose layer is freed (%@)", (animation as? CAPropertyAnimation)?.keyPath ?? "-")
                notify_post(droppedNotification)
                return
            }
            nextStart(state, startSelector, animation)
        }
        let stop: @convention(block) (AnyObject, CAAnimation?, Bool) -> Void = { state, animation, finished in
            guard mine == generation, Thread.isMainThread else { nextStop(state, stopSelector, animation, finished); return }
            stayOutermost(startMethod, stopMethod)
            if let animation, let found = box(of: animation) {
                let before = found.ledger.unmatchedStops
                if found.ledger.stop() == .drop {
                    NSLog("UITestAnimationStartGuard: dropped the stop that pairs with a dropped start")
                    notify_post(droppedStopNotification)
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

    private static func box(of animation: CAAnimation) -> Box? { objc_getAssociatedObject(animation, &boxKey) as? Box }
}
#endif
