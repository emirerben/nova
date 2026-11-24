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
                NSLog("KBDPROBE keyboard %@ endY=%.0f endH=%.0f duration=%.2f local=%d", label, end.minY, end.height, duration, local ? 1 : 0)
            })
        }
        let made = Timer(timeInterval: 1, repeats: true) { _ in MainActor.assumeIsolated { tick() } }
        RunLoop.main.add(made, forMode: .common)
        timer = made
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
        }
    }

    private static func firstResponder(in view: UIView) -> UIView? {
        if view.isFirstResponder { return view }
        for child in view.subviews { if let found = firstResponder(in: child) { return found } }
        return nil
    }
}
