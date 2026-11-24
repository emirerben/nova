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

    private static func owners(of layer: CALayer) -> String {
        var names: [String] = []
        var current: CALayer? = layer
        while let next = current, names.count < 6 {
            if let view = next.delegate as? UIView {
                var name = String(describing: type(of: view))
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
            held = responder is ExplicitLineTextView ? held + 1 : 0
            if held == 4 || held == 8 { NSLog("KBDPROBE stall held=%d", held) }
            // While a UI test drives the app the run loop wakes hundreds of times per beat. In both hangs of
            // the second capture it woke 10 to 31 times: nothing was asking the app anything.
            quiet = keyboardSeen && (seen["wakes"] ?? 0) < 60 ? quiet + 1 : 0
            if quiet == 2 || quiet == 6 {
                NSLog("KBDPROBE stall quiet=%d wakes=%d state=%d", quiet, seen["wakes"] ?? 0, UIApplication.shared.applicationState.rawValue)
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
