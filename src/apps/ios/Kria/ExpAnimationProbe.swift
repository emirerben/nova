import ObjectiveC
import UIKit

/// THROWAWAY (KRI-1604 capture, never merged): once a second, logs every layer animation that has been
/// running for 3 s or more, and every keyboard notification, so a UI test that waits 60 s for the app
/// to "idle" can be matched to whatever is still animating.
@MainActor enum ExpAnimationProbe {
    private static var firstSeen: [String: CFTimeInterval] = [:]
    private static var lastLogged: [String: CFTimeInterval] = [:]
    private static var timer: Timer?
    private static var tokens: [NSObjectProtocol] = []
    private static var ticks = 0
    private static var counts: [String: Int] = [:]
    private static var held = 0
    private static var quiet = 0
    private static var keyboardSeen = false
    private static var lastGlass = ""
    /// UIKit animation groups (one `UIViewAnimationState` each) whose animations reported more starts than stops.
    /// XCTest counts the same two calls (it hooks `UIViewAnimationState animationDidStart:` and
    /// `animationDidStop:finished:`) and calls the app idle only at zero.
    private final class Attached {
        weak var layer: CALayer?
        let key: String
        let keyPath: String
        let owner: String
        init(layer: CALayer, key: String, keyPath: String, owner: String) { self.layer = layer; self.key = key; self.keyPath = keyPath; self.owner = owner }
    }
    private final class Weak { weak var view: UIView?; init(_ view: UIView) { self.view = view } }
    private static func place(of layer: CALayer) -> String {
        let frame = layer.convert(layer.bounds, to: nil)
        return String(format: "at=%.0f,%.0f size=%.0fx%.0f radius=%.0f", frame.minX, frame.minY, frame.width, frame.height, layer.cornerRadius)
    }
    private static func glassViews() -> [UIView] {
        var found: [UIView] = []
        for window in allWindows() {
            var stack: [UIView] = [window]
            while let view = stack.popLast() {
                if String(describing: type(of: view)).contains("GlassInteraction") { found.append(view) }
                stack.append(contentsOf: view.subviews)
            }
        }
        return found
    }
    /// One animation as it was added to a layer (hooked `CALayer addAnimation:forKey:`): exact, unlike the window scan.
    /// It rides on the animation as an associated object and is never kept by the probe, so nothing lives longer
    /// than it would have. When the animation is freed after a start and with no stop, the track says so as it dies.
    private final class Track: @unchecked Sendable {
        weak var layer: CALayer?
        let key: String
        let keyPath: String
        let owner: String
        var starts = 0
        var stops = 0
        var firstStart: CFTimeInterval = 0
        let added = CACurrentMediaTime()
        init(layer: CALayer, key: String, keyPath: String, owner: String) {
            self.layer = layer; self.key = key; self.keyPath = keyPath; self.owner = owner
        }
        var text: String { "key=\(key) keyPath=\(keyPath) layer=\(layer == nil ? "freed" : "alive") was=\(owner)" }
        deinit {
            if starts != stops { NSLog("KBDPROBE lost animation freed with starts=%d stops=%d: %@", starts, stops, text) }
        }
    }
    private final class WeakAnimation { weak var animation: CAAnimation?; init(_ animation: CAAnimation) { self.animation = animation } }
    nonisolated(unsafe) private static var trackKey: UInt8 = 0
    private static var addHookMisses = 0
    private static func track(of animation: CAAnimation) -> Track? { objc_getAssociatedObject(animation, &trackKey) as? Track }
    private static func hookAddAnimation() {
        let selector = NSSelectorFromString("addAnimation:forKey:")
        guard let method = class_getInstanceMethod(CALayer.self, selector) else { NSLog("KBDPROBE add hook missing"); return }
        typealias Add = @convention(c) (CALayer, Selector, CAAnimation, NSString?) -> Void
        let original = unsafeBitCast(method_getImplementation(method), to: Add.self)
        let block: @convention(block) (CALayer, CAAnimation, NSString?) -> Void = { layer, animation, key in
            original(layer, selector, animation, key)
            guard Thread.isMainThread, let delegate = animation.delegate,
                  NSStringFromClass(type(of: delegate as AnyObject)) == "UIViewAnimationState" else { return }
            MainActor.assumeIsolated {
                guard let key = key as String?, let copy = layer.animation(forKey: key) else { addHookMisses += 1; return }
                let made = Track(layer: layer, key: key, keyPath: (copy as? CAPropertyAnimation)?.keyPath ?? "-",
                                 owner: "\(owners(of: layer, depth: 8)) \(place(of: layer))")
                objc_setAssociatedObject(copy, &trackKey, made, .OBJC_ASSOCIATION_RETAIN_NONATOMIC)
            }
        }
        method_setImplementation(method, imp_implementationWithBlock(block))
        NSLog("KBDPROBE add hook installed")
    }
    private final class OpenState {
        let state: AnyObject
        let since = CACurrentMediaTime()
        var balance = 0
        var total = 0
        var sample = ""
        var attached: [Attached] = []
        var seen: Set<String> = []
        var scans = 0
        /// Every interactive-glass view on screen when the group started: where it was and what it sat in.
        var glass: [(view: Weak, was: String)] = []
        /// Animations that reported a start and no stop yet, by identity.
        var live: [WeakAnimation] = []
        var stopsOnFreed: [String] = []
        var notFinished = 0
        var untrackedStops = 0
        init(_ state: AnyObject) { self.state = state }
    }

    /// Where each animation of a group sits, read while it is still attached. All of a group's animations are added
    /// in one transaction, so the first start usually finds them all; a group is scanned at most four times.
    private static func recordLayers(of entry: OpenState) {
        guard entry.attached.count < entry.total, entry.scans < 4 else { return }
        entry.scans += 1
        if entry.scans == 1 {
            entry.glass = glassViews().map { (Weak($0), "\(owners(of: $0.layer, depth: 14)) \(place(of: $0.layer))") }
            let summary = entry.glass.map(\.was).joined(separator: " | ")
            if summary != lastGlass {
                lastGlass = summary
                NSLog("KBDPROBE glass n=%d", entry.glass.count)
                for item in entry.glass { NSLog("KBDPROBE glass one %@", item.was) }
            }
        }
        for window in allWindows() {
            var stack: [CALayer] = [window.layer]
            while let layer = stack.popLast() {
                if let sublayers = layer.sublayers { stack.append(contentsOf: sublayers) }
                for key in layer.animationKeys() ?? [] {
                    guard let animation = layer.animation(forKey: key), let delegate = animation.delegate, delegate === entry.state else { continue }
                    let id = "\(ObjectIdentifier(layer).hashValue)|\(key)"
                    guard entry.seen.insert(id).inserted else { continue }
                    entry.attached.append(Attached(layer: layer, key: key, keyPath: (animation as? CAPropertyAnimation)?.keyPath ?? "-",
                                                   owner: "\(String(describing: type(of: window))) \(owners(of: layer)) \(place(of: layer))"))
                }
            }
        }
    }

    private static func inWindow(_ layer: CALayer, _ roots: Set<ObjectIdentifier>) -> Bool {
        var current: CALayer? = layer
        while let next = current { if roots.contains(ObjectIdentifier(next)) { return true }; current = next.superlayer }
        return false
    }
    private static var open: [ObjectIdentifier: OpenState] = [:]
    private static var starts = 0
    private static var stops = 0
    private static var strayStops = 0
    private static var balance = 0

    private static func hookAnimationState() {
        guard let stateClass = NSClassFromString("UIViewAnimationState") else { NSLog("KBDPROBE hook no UIViewAnimationState"); return }
        let startSelector = NSSelectorFromString("animationDidStart:")
        let stopSelector = NSSelectorFromString("animationDidStop:finished:")
        guard let startMethod = class_getInstanceMethod(stateClass, startSelector),
              let stopMethod = class_getInstanceMethod(stateClass, stopSelector) else { NSLog("KBDPROBE hook methods missing"); return }
        typealias Start = @convention(c) (AnyObject, Selector, CAAnimation?) -> Void
        typealias Stop = @convention(c) (AnyObject, Selector, CAAnimation?, Bool) -> Void
        let originalStart = unsafeBitCast(method_getImplementation(startMethod), to: Start.self)
        let originalStop = unsafeBitCast(method_getImplementation(stopMethod), to: Stop.self)
        // Control arm only (EXP_BALANCE_START=1): a second start for one animation is answered with one extra stop,
        // sent through whatever is hooked on the class now, so UIKit's and XCTest's counts both come back to zero.
        let balances = ProcessInfo.processInfo.environment["EXP_BALANCE_START"] == "1"
        let startBlock: @convention(block) (AnyObject, CAAnimation?) -> Void = { state, animation in
            var duplicate = false
            if Thread.isMainThread {
                MainActor.assumeIsolated {
                    starts += 1; balance += 1
                    let entry = open[ObjectIdentifier(state)] ?? OpenState(state)
                    open[ObjectIdentifier(state)] = entry
                    entry.balance += 1; entry.total += 1
                    recordLayers(of: entry)
                    if let animation {
                        entry.live.append(WeakAnimation(animation))
                        if let found = track(of: animation) {
                            found.starts += 1
                            if found.starts == 1 { found.firstStart = CACurrentMediaTime() } else {
                                let now = found.layer.map { "\(owners(of: $0, depth: 8)) \(place(of: $0)) attached=\($0.animation(forKey: found.key) === animation ? 1 : 0)" } ?? "layer freed"
                                NSLog("KBDPROBE double start #%d stops=%d msAfterAdd=%.0f msAfterFirstStart=%.0f key=%@ keyPath=%@ was=%@ now=%@",
                                      found.starts, found.stops, (CACurrentMediaTime() - found.added) * 1000, (CACurrentMediaTime() - found.firstStart) * 1000,
                                      found.key, found.keyPath, found.owner, now)
                            }
                            duplicate = found.starts > 1
                        } else { entry.untrackedStops += 1 }
                    }
                    if entry.sample.isEmpty, animation == nil { entry.sample = "no animation object" }
                    if entry.sample.isEmpty, let animation {
                        entry.sample = String(format: "class=%@ keyPath=%@ duration=%.2f repeat=%.0f removed=%d fill=%@",
                                              String(describing: type(of: animation)), (animation as? CAPropertyAnimation)?.keyPath ?? "-",
                                              animation.duration, Double(animation.repeatCount), animation.isRemovedOnCompletion ? 1 : 0,
                                              animation.fillMode.rawValue)
                    }
                }
            } else { NSLog("KBDPROBE hook start off the main thread") }
            originalStart(state, startSelector, animation)
            if balances, duplicate, let current = class_getMethodImplementation(object_getClass(state), stopSelector) {
                NSLog("KBDPROBE balanced a double start with one extra stop")
                unsafeBitCast(current, to: Stop.self)(state, stopSelector, animation, false)
            }
        }
        let stopBlock: @convention(block) (AnyObject, CAAnimation?, Bool) -> Void = { state, animation, finished in
            if Thread.isMainThread {
                MainActor.assumeIsolated {
                    stops += 1; balance -= 1
                    if let entry = open[ObjectIdentifier(state)] {
                        entry.balance -= 1
                        if !finished { entry.notFinished += 1 }
                        if let animation, let found = track(of: animation) {
                            found.stops += 1
                            if found.layer == nil { entry.stopsOnFreed.append("\(found.keyPath) \(found.owner)") }
                        }
                        if entry.balance <= 0 {
                            open[ObjectIdentifier(state)] = nil
                            // Every group of 20 or more animations (the keyboard's has 47 to 57), hung or not.
                            if entry.total >= 20 {
                                NSLog("KBDPROBE closed total=%d ms=%.0f notFinished=%d stopsOnFreedLayer=%d untracked=%d freed=[%@]",
                                      entry.total, (CACurrentMediaTime() - entry.since) * 1000, entry.notFinished, entry.stopsOnFreed.count,
                                      entry.untrackedStops, entry.stopsOnFreed.joined(separator: " ; "))
                            }
                        }
                    } else { strayStops += 1 }
                }
            } else { NSLog("KBDPROBE hook stop off the main thread") }
            originalStop(state, stopSelector, animation, finished)
        }
        method_setImplementation(startMethod, imp_implementationWithBlock(startBlock))
        method_setImplementation(stopMethod, imp_implementationWithBlock(stopBlock))
        NSLog("KBDPROBE hook installed")
    }

    /// XCTest's own answer to "is an animation in progress", when its support code is loaded in the app.
    private static func xctestSaysAnimating() -> Int {
        guard let notifier = NSClassFromString("XCTAnimationsIdleNotifier") else { return -1 }
        let selector = NSSelectorFromString("isAnimationInProgress")
        guard let method = class_getClassMethod(notifier, selector) else { return -2 }
        typealias Function = @convention(c) (AnyClass, Selector) -> Bool
        return unsafeBitCast(method_getImplementation(method), to: Function.self)(notifier, selector) ? 1 : 0
    }

    private static func logOpenAnimations(_ windows: [UIWindow]) {
        let now = CACurrentMediaTime()
        var owners: [ObjectIdentifier: [String]] = [:]
        for window in windows {
            var stack: [CALayer] = [window.layer]
            while let layer = stack.popLast() {
                if let sublayers = layer.sublayers { stack.append(contentsOf: sublayers) }
                for key in layer.animationKeys() ?? [] {
                    guard let delegate = layer.animation(forKey: key)?.delegate else { continue }
                    let id = ObjectIdentifier(delegate as AnyObject)
                    guard open[id] != nil, (owners[id]?.count ?? 0) < 3 else { continue }
                    owners[id, default: []].append("\(String(describing: type(of: window))) key=\(key) layer=\(Self.owners(of: layer))")
                }
            }
        }
        NSLog("KBDPROBE open groups=%d balance=%d", open.count, balance)
        for (id, entry) in open.sorted(by: { $0.value.since < $1.value.since }).prefix(12) {
            NSLog(
                "KBDPROBE open %.1fs unfinished=%d of %d state=%@ first[%@] on=%@",
                now - entry.since, entry.balance, entry.total, String(describing: entry.state), entry.sample,
                owners[id]?.joined(separator: " ; ") ?? "no layer in any window"
            )
            let roots = Set(windows.map { ObjectIdentifier($0.layer) })
            var gone = 0, detached = 0, finished = 0, running = 0
            for record in entry.attached {
                guard let layer = record.layer else {
                    gone += 1
                    NSLog("KBDPROBE open layer freed key=%@ keyPath=%@ was=%@", record.key, record.keyPath, record.owner)
                    continue
                }
                let stillAnimating = layer.animation(forKey: record.key) != nil
                if !inWindow(layer, roots) {
                    detached += 1
                    NSLog("KBDPROBE open layer out of every window stillHasAnimation=%d key=%@ keyPath=%@ was=%@", stillAnimating ? 1 : 0, record.key, record.keyPath, record.owner)
                } else if stillAnimating {
                    running += 1
                    NSLog("KBDPROBE open layer in a window and still animating key=%@ keyPath=%@ on=%@", record.key, record.keyPath, record.owner)
                } else { finished += 1 }
            }
            var alive = 0, gonePending = 0
            for item in entry.live {
                guard let animation = item.animation else { gonePending += 1; continue }
                guard let found = track(of: animation) else { continue }
                if found.stops < found.starts { alive += 1; NSLog("KBDPROBE open anim alive and never stopped %@", found.text) }
            }
            NSLog("KBDPROBE open sofar started=%d animationsFreed=%d aliveNeverStopped=%d stopsOnFreedLayer=%d notFinished=%d untracked=%d freed=[%@]",
                  entry.live.count, gonePending, alive, entry.stopsOnFreed.count, entry.notFinished, entry.untrackedStops,
                  entry.stopsOnFreed.joined(separator: " ; "))
            for item in entry.glass {
                if let view = item.view.view {
                    NSLog("KBDPROBE open glass %@ now=%@ was=%@", view.window == nil ? "out of every window" : "still on screen", place(of: view.layer), item.was)
                } else {
                    NSLog("KBDPROBE open glass freed was=%@", item.was)
                }
            }
            NSLog("KBDPROBE open group recorded=%d of %d scans=%d freed=%d detached=%d stillAnimating=%d finished=%d",
                  entry.attached.count, entry.total, entry.scans, gone, detached, running, finished)
        }
    }

    /// Counts one main-thread event (a layout pass, a caret scroll, a run-loop wake) since the last beat.
    static func count(_ key: String) { counts[key, default: 0] += 1 }

    private static func flag(_ view: UIScrollView, _ name: String) -> Int {
        let selector = NSSelectorFromString(name)
        guard view.responds(to: selector), let method = class_getInstanceMethod(type(of: view), selector) else { return -1 }
        typealias Function = @convention(c) (AnyObject, Selector) -> Bool
        return unsafeBitCast(method_getImplementation(method), to: Function.self)(view, selector) ? 1 : 0
    }

    private static func describe(_ view: UIScrollView) -> String {
        String(format: "off=%.1f,%.1f content=%.1fx%.1f bounds=%.1fx%.1f drag=%d decel=%d track=%d animScroll=%d keys=%d",
               view.contentOffset.x, view.contentOffset.y, view.contentSize.width, view.contentSize.height,
               view.bounds.width, view.bounds.height, view.isDragging ? 1 : 0, view.isDecelerating ? 1 : 0, view.isTracking ? 1 : 0,
               flag(view, "_isAnimatingScroll"), view.layer.animationKeys()?.count ?? 0)
    }

    static func start() {
        guard timer == nil, ProcessInfo.processInfo.arguments.contains(where: { $0.hasPrefix("-ui-testing") }) else { return }
        let names: [(Notification.Name, String)] = [
            (UIResponder.keyboardWillShowNotification, "willShow"), (UIResponder.keyboardDidShowNotification, "didShow"),
            (UIResponder.keyboardWillHideNotification, "willHide"), (UIResponder.keyboardDidHideNotification, "didHide"),
            (UIResponder.keyboardWillChangeFrameNotification, "willChangeFrame"), (UIResponder.keyboardDidChangeFrameNotification, "didChangeFrame"),
        ]
        for (name, label) in names {
            tokens.append(NotificationCenter.default.addObserver(forName: name, object: nil, queue: .main) { note in
                let end = (note.userInfo?[UIResponder.keyboardFrameEndUserInfoKey] as? CGRect) ?? .null
                let duration = (note.userInfo?[UIResponder.keyboardAnimationDurationUserInfoKey] as? Double) ?? -1
                let local = (note.userInfo?[UIResponder.keyboardIsLocalUserInfoKey] as? Bool) ?? false
                MainActor.assumeIsolated { keyboardSeen = true }
                NSLog("KBDPROBE keyboard %@ endY=%.0f endH=%.0f duration=%.2f local=%d", label, end.minY, end.height, duration, local ? 1 : 0)
            })
        }
        let made = Timer(timeInterval: 1, repeats: true) { _ in MainActor.assumeIsolated { tick() } }
        RunLoop.main.add(made, forMode: .common)
        timer = made
        let observer = CFRunLoopObserverCreateWithHandler(nil, CFRunLoopActivity.afterWaiting.rawValue, true, 0) { _, _ in
            MainActor.assumeIsolated { count("wakes") }
        }
        CFRunLoopAddObserver(CFRunLoopGetMain(), observer, .commonModes)
        hookAnimationState()
        hookAddAnimation()
        NSLog("KBDPROBE started")
    }

    private static func allWindows() -> [UIWindow] {
        let selector = NSSelectorFromString("allWindowsIncludingInternalWindows:onlyVisibleWindows:")
        if let method = class_getClassMethod(UIWindow.self, selector) {
            typealias Function = @convention(c) (AnyClass, Selector, Bool, Bool) -> NSArray?
            let function = unsafeBitCast(method_getImplementation(method), to: Function.self)
            if let found = function(UIWindow.self, selector, true, false) as? [UIWindow], !found.isEmpty { return found }
        }
        return UIApplication.shared.connectedScenes.compactMap { $0 as? UIWindowScene }.flatMap(\.windows)
    }

    private static func owners(of layer: CALayer, depth: Int = 6) -> String {
        var names: [String] = []
        var current: CALayer? = layer
        while let next = current, names.count < depth {
            if let view = next.delegate as? UIView {
                var name = String(String(describing: type(of: view)).prefix { $0 != "<" })
                if let identifier = view.accessibilityIdentifier, !identifier.isEmpty { name += "#\(identifier)" }
                names.append(name)
            } else if names.isEmpty {
                names.append(String(describing: type(of: next)))
            }
            current = next.superlayer
        }
        return names.joined(separator: " < ")
    }

    private static func tick() {
        ticks += 1
        let now = CACurrentMediaTime()
        var present: Set<String> = []
        var layerCount = 0
        var animated = 0
        let windows = allWindows()
        for window in windows {
            var stack: [CALayer] = [window.layer]
            while let layer = stack.popLast() {
                layerCount += 1
                if let sublayers = layer.sublayers { stack.append(contentsOf: sublayers) }
                guard let keys = layer.animationKeys(), !keys.isEmpty else { continue }
                for key in keys {
                    animated += 1
                    let id = "\(ObjectIdentifier(layer).hashValue)|\(key)"
                    present.insert(id)
                    let since = firstSeen[id] ?? now
                    firstSeen[id] = since
                    let age = now - since
                    guard age >= 3, now - (lastLogged[id] ?? 0) >= 10 else { continue }
                    lastLogged[id] = now
                    let animation = layer.animation(forKey: key)
                    let keyPath = (animation as? CAPropertyAnimation)?.keyPath ?? "-"
                    NSLog(
                        "KBDPROBE running %.0fs window=%@ hidden=%d layer=%@ key=%@ class=%@ keyPath=%@ duration=%.2f repeat=%.0f removed=%d",
                        age, String(describing: type(of: window)), window.isHidden ? 1 : 0, owners(of: layer), key,
                        animation.map { String(describing: type(of: $0)) } ?? "nil", keyPath,
                        animation?.duration ?? -1, Double(animation?.repeatCount ?? -1), (animation?.isRemovedOnCompletion ?? true) ? 1 : 0
                    )
                }
            }
        }
        firstSeen = firstSeen.filter { present.contains($0.key) }
        lastLogged = lastLogged.filter { present.contains($0.key) }
        if ticks % 5 == 0 {
            let responder = windows.lazy.compactMap { firstResponder(in: $0) }.first
            NSLog(
                "KBDPROBE beat windows=%d layers=%d animating=%d responder=%@ animationsEnabled=%d",
                windows.count, layerCount, animated, responder.map { String(describing: type(of: $0)) } ?? "none", UIView.areAnimationsEnabled ? 1 : 0
            )
            let seen = counts
            counts = [:]
            var inner = "-", outer = "-"
            if let field = responder as? UIScrollView {
                inner = describe(field)
                var parent = field.superview
                while let next = parent, !(next is UIScrollView) { parent = next.superview }
                if let found = parent as? UIScrollView { outer = describe(found) }
            }
            NSLog(
                "KBDPROBE work wakes=%d layout=%d caret=%d update=%d selection=%d change=%d field[%@] outer[%@]",
                seen["wakes"] ?? 0, seen["layout"] ?? 0, seen["caret"] ?? 0, seen["update"] ?? 0, seen["selection"] ?? 0, seen["change"] ?? 0, inner, outer
            )
            NSLog("KBDPROBE uikit starts=%d stops=%d strayStops=%d groups=%d balance=%d xctestAnimating=%d", starts, stops, strayStops, open.count, balance, xctestSaysAnimating())
            // Any group still unfinished 3 s after it began is listed, hang or not: the keyboard's group lasts 0.38 s.
            if open.values.contains(where: { CACurrentMediaTime() - $0.since > 3 }) { logOpenAnimations(windows) }
            held = responder is ExplicitLineTextView ? held + 1 : 0
            if held == 4 || held == 8 { NSLog("KBDPROBE stall held=%d", held) }
            // While a UI test drives the app the run loop wakes hundreds of times per beat. In both hangs of
            // the second capture it woke 10 to 31 times: nothing was asking the app anything.
            quiet = keyboardSeen && (seen["wakes"] ?? 0) < 60 ? quiet + 1 : 0
            if quiet == 2 || quiet == 6 {
                NSLog("KBDPROBE stall quiet=%d wakes=%d state=%d", quiet, seen["wakes"] ?? 0, UIApplication.shared.applicationState.rawValue)
                logOpenAnimations(windows)
                for window in windows {
                    var animatedLayers = 0
                    var stack: [CALayer] = [window.layer]
                    while let layer = stack.popLast() {
                        if let sublayers = layer.sublayers { stack.append(contentsOf: sublayers) }
                        if !(layer.animationKeys() ?? []).isEmpty { animatedLayers += 1 }
                    }
                    NSLog(
                        "KBDPROBE window %@ hidden=%d alpha=%.2f level=%.0f key=%d frame=%@ animatedLayers=%d root=%@",
                        String(describing: type(of: window)), window.isHidden ? 1 : 0, window.alpha, window.windowLevel.rawValue,
                        window.isKeyWindow ? 1 : 0, NSCoder.string(for: window.frame), animatedLayers,
                        window.rootViewController.map { String(describing: type(of: $0)) } ?? "none"
                    )
                }
            }
        }
    }

    private static func firstResponder(in view: UIView) -> UIView? {
        if view.isFirstResponder { return view }
        for child in view.subviews { if let found = firstResponder(in: child) { return found } }
        return nil
    }
}
