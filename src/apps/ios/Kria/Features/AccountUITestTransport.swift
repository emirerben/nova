#if DEBUG
import Foundation
import UIKit
import ObjectiveC

/// All requests are intercepted: account UI tests never contact a real account.
enum AccountUITestTransport {
    @MainActor static func api(tokenStore: TokenStore) -> KriaAPI {
        AccountPickerInputProbe.installIfEnabled()
        let configuration = URLSessionConfiguration.ephemeral
        configuration.protocolClasses = [AccountUITestURLProtocol.self]
        return KriaAPI(baseURL: URL(string: "https://account-ui-testing.invalid")!,
                       tokenStore: tokenStore, session: URLSession(configuration: configuration))
    }
}

final class AccountUITestTokenStore: TokenStore, @unchecked Sendable {
    private let lock = NSLock()
    private var session: MobileSession?
    func read() throws -> MobileSession? { lock.lock(); defer { lock.unlock() }; return session }
    func write(_ session: MobileSession) throws { lock.lock(); defer { lock.unlock() }; self.session = session }
    func delete() throws { lock.lock(); defer { lock.unlock() }; session = nil }
}

private final class AccountUITestURLProtocol: URLProtocol, @unchecked Sendable {
    override class func canInit(with request: URLRequest) -> Bool { true }
    override class func canonicalRequest(for request: URLRequest) -> URLRequest { request }
    override func startLoading() {
        let path = request.url?.path ?? ""
        let mode = ProcessInfo.processInfo.environment["KRIA_ACCOUNT_TEST_MODE"] ?? "normal"
        let result: (Int, String)
        if path.hasPrefix("/me/memory"), mode.hasPrefix("text-preferences") {
            let result = AccountTextPreferenceFixture.shared.reply(request, stale: mode.hasSuffix("stale"), learning: mode.hasSuffix("learning"))
            let delay = Int(ProcessInfo.processInfo.environment["KRIA_ACCOUNT_DISABLE_RESPONSE_DELAY_MS"] ?? "0") ?? 0
            if request.httpMethod == "PATCH", path == "/me/memory/edit-learning",
               AccountTextPreferenceFixture.shared.isLearningDisabled, (1...9000).contains(delay) {
                NSLog("%@", "[ACCOUNT-READINESS-CONTROL] delay-disable-response-start milliseconds=\(delay)")
                DispatchQueue.global().asyncAfter(deadline: .now() + .milliseconds(delay)) {
                    NSLog("%@", "[ACCOUNT-READINESS-CONTROL] delay-disable-response-complete milliseconds=\(delay)")
                    self.deliver(result)
                }
                return
            }
            deliver(result)
            return
        }
        switch path {
        case "/me/account/delete-request":
            result = mode == "unavailable" ? (503, "{}") : (202, "{\"requested\":true}")
        case "/me/account/delete-confirm":
            result = mode == "invalid-code" ? (400, "{}") : (204, "")
        case "/auth/mobile/revoke": result = (204, "")
        case "/auth/mobile/me":
            result = (200, "{\"id\":\"1\",\"email\":\"creator@example.com\",\"name\":\"Test creator\",\"onboarding_status\":\"complete\",\"linked_providers\":[\"google\"]}")
        default: result = (503, "{}")
        }
        deliver(result)
    }
    private func deliver(_ result: (Int, String)) {
        guard let url = request.url,
              let response = HTTPURLResponse(url: url, statusCode: result.0, httpVersion: nil,
                                             headerFields: ["Content-Type": "application/json"]) else { return }
        client?.urlProtocol(self, didReceive: response, cacheStoragePolicy: .notAllowed)
        client?.urlProtocol(self, didLoad: Data(result.1.utf8))
        client?.urlProtocolDidFinishLoading(self)
    }
    override func stopLoading() {}
}
final class AccountTextPreferenceFixture: @unchecked Sendable {
    static let shared = AccountTextPreferenceFixture()
    private let lock = NSLock()
    private var revision = 0
    private var item: [String: Any]?
    private var learningInitialized = false
    private var learningEnabled = false
    private var learningDecided = false
    var isLearningDisabled: Bool {
        lock.lock(); defer { lock.unlock() }
        return !learningEnabled
    }
    private var learningSuggestion: [String: Any] {
        ["id": "learning-position", "normalized_key": "text_style_title", "source_kind": "finished_edits",
         "instruction": "Place title text at top left in future videos.",
         "structured_value": ["text_style_title": ["position": "top_left"]],
         "evidence": ["supporting_video_count": 3]]
    }
    private func bodyData(_ request: URLRequest) -> Data? {
        if let data = request.httpBody { return data }
        guard let stream = request.httpBodyStream else { return nil }
        stream.open(); defer { stream.close() }
        var data = Data()
        var buffer = [UInt8](repeating: 0, count: 4096)
        while data.count < 65536 {
            let count = stream.read(&buffer, maxLength: 4096)
            if count <= 0 { break }
            data.append(buffer, count: count)
        }
        return data
    }
    func reply(_ request: URLRequest, stale: Bool, learning: Bool, initiallyEnabled: Bool = false) -> (Int, String) {
        lock.lock(); defer { lock.unlock() }
        let path = request.url?.path ?? ""
        if learning && !learningInitialized {
            learningInitialized = true
            learningEnabled = initiallyEnabled
            item = ["id": "approved-title", "normalized_key": "text_style_title", "instruction": "My title choices",
                    "structured_value": ["text_style_title": ["shadow_enabled": false, "stroke_width": 0]]]
        }
        if request.httpMethod == "GET" {
            let body: [String: Any] = ["feature_available": true, "text_preferences_available": true, "enabled": true, "revision": revision,
                                      "active": item.map { [$0] } ?? [],
                                      "suggestions": learningEnabled && !learningDecided ? [learningSuggestion] : [],
                                      "edit_learning": ["available": learning, "enabled": learningEnabled], "recent_undo": NSNull()]
            let data = try! JSONSerialization.data(withJSONObject: body)
            return (200, String(decoding: data, as: UTF8.self))
        }
        if stale { return (409, "{\"detail\":{\"code\":\"stale_revision\",\"message\":\"Preferences changed\"}}") }
        if learning && path == "/me/memory/edit-learning", let data = bodyData(request),
           let body = (try? JSONSerialization.jsonObject(with: data)) as? [String: Any] {
            learningEnabled = body["enabled"] as? Bool ?? false
            if !learningEnabled { learningDecided = true }
        } else if learning && path == "/me/memory/edit-learning/reset" {
            learningDecided = true
        } else if learning && path.hasSuffix("/learning-position/accept") {
            learningDecided = true
            let position = ProcessInfo.processInfo.environment["KRIA_ACCOUNT_WRONG_POSITION_CONTROL"] == "1" ? "top_right" : "top_left"
            item?["structured_value"] = ["text_style_title": ["position": position, "shadow_enabled": false, "stroke_width": 0]]
        } else if learning && path.hasSuffix("/learning-position/suggestion") {
            learningDecided = true
        } else if request.httpMethod == "DELETE" { item = nil }
        else if path.contains("/items"), let data = bodyData(request),
                var value = (try? JSONSerialization.jsonObject(with: data)) as? [String: Any] {
            value["id"] = "00000000-0000-0000-0000-000000000648"
            item = value
        }
        revision += 1
        return (200, "{\"id\":\"00000000-0000-0000-0000-000000000649\"}")
    }
}


// Disposable diagnostic only: fake-account opt-in, no extra input or binding writes.
@MainActor enum AccountPickerInputProbe {
    private static var installed = false
    private static weak var currentModel: TextPreferencesModel?
    private static var currentRole = "all_text"
    private static var enabled: Bool {
        ProcessInfo.processInfo.arguments.contains("-ui-testing-account") &&
        ProcessInfo.processInfo.environment["KRIA_ACCOUNT_INPUT_TRACE"] == "1"
    }
    static func installIfEnabled() {
        guard enabled, !installed,
              let original = class_getInstanceMethod(UIWindow.self, #selector(UIWindow.sendEvent(_:))),
              let observation = class_getInstanceMethod(UIWindow.self, #selector(UIWindow.accountProbeSendEvent(_:))) else { return }
        method_exchangeImplementations(original, observation)
        installed = true
        NSLog("%@", "[ACCOUNT-INPUT-INSTALL] UIWindow.sendEvent observer installed for fake account only")
    }
    static func remember(_ model: TextPreferencesModel, role: String) {
        guard enabled else { return }
        currentModel = model
        currentRole = role
    }
    static func sample(_ event: UIEvent, window: UIWindow, stage: String) -> [[String: Any]] {
        guard enabled else { return [] }
        var samples: [[String: Any]] = []
        for touch in event.allTouches ?? [] where [UITouch.Phase.began, .ended, .cancelled].contains(touch.phase) {
            let point = touch.location(in: window)
            var path: [[String: Any]] = []
            var view = touch.view
            while let node = view, path.count < (stage == "before" ? 2 : 6) {
                var item: [String: Any] = [
                    "class": NSStringFromClass(type(of: node)),
                    "frameInWindow": geometry(node.convert(node.bounds, to: window)),
                    "interactionEnabled": node.isUserInteractionEnabled, "hidden": node.isHidden,
                    "alpha": Double(node.alpha),
                ]
                if let control = node as? UIControl { item["controlEnabled"] = control.isEnabled }
                // Read richer accessibility/gesture state after original delivery, not before it.
                if stage == "after" {
                    item["identifier"] = node.accessibilityIdentifier ?? ""
                    item["label"] = String((node.accessibilityLabel ?? "").prefix(60))
                    if let label = node as? UILabel { item["text"] = String((label.text ?? "").prefix(60)) }
                    item["gestures"] = (node.gestureRecognizers ?? []).prefix(4).map { recognizer -> [String: Any] in
                        ["class": NSStringFromClass(type(of: recognizer)), "state": recognizer.state.rawValue,
                         "enabled": recognizer.isEnabled, "cancelsTouches": recognizer.cancelsTouchesInView,
                         "delaysBegan": recognizer.delaysTouchesBegan, "delaysEnded": recognizer.delaysTouchesEnded]
                    }
                }
                path.append(item)
                view = node.superview
            }
            var payload: [String: Any] = [
                "time": Date().timeIntervalSince1970, "eventTime": event.timestamp, "eventType": event.type.rawValue,
                "eventKey": "\(ObjectIdentifier(touch)):\(touch.timestamp):\(touch.phase.rawValue)",
                "touchObject": String(describing: ObjectIdentifier(touch)),
                "touchTime": touch.timestamp, "stage": stage, "phase": touch.phase.rawValue, "touchType": touch.type.rawValue,
                "point": ["x": Double(point.x), "y": Double(point.y)],
                "windowClass": NSStringFromClass(type(of: window)), "window": String(describing: ObjectIdentifier(window)),
                "touchWindow": touch.window.map { String(describing: ObjectIdentifier($0)) } ?? "nil",
                "touchWindowMatches": touch.window === window, "keyWindow": window.isKeyWindow,
                "windowFrame": geometry(window.frame), "windowBounds": geometry(window.bounds),
                "windowLevel": Double(window.windowLevel.rawValue), "path": path,
                "role": currentRole,
            ]
            if let model = currentModel {
                payload["model"] = String(describing: ObjectIdentifier(model))
                payload["working"] = model.isWorking
                payload["revision"] = model.memory?.revision ?? -1
                payload["titlePosition"] = model.memory?.rule(role: "title").position ?? "nil"
            }
            samples.append(payload)
        }
        return samples
    }
    static func emit(_ samples: [[String: Any]]) {
        for var sample in samples {
            let nodes = sample.removeValue(forKey: "path") as? [[String: Any]] ?? []
            sample["pathCount"] = nodes.count
            log(sample, prefix: "[ACCOUNT-INPUT] ")
            for (index, node) in nodes.enumerated() {
                log(["eventKey": sample["eventKey"] ?? "", "stage": sample["stage"] ?? "",
                     "index": index, "node": node], prefix: "[ACCOUNT-INPUT-PATH] ")
            }
        }
    }
    private static func geometry(_ rect: CGRect) -> [String: Double] {
        ["x": Double(rect.origin.x), "y": Double(rect.origin.y),
         "width": Double(rect.width), "height": Double(rect.height)]
    }
    private static func log(_ payload: [String: Any], prefix: String) {
        if let data = try? JSONSerialization.data(withJSONObject: payload, options: [.sortedKeys]),
           let line = String(data: data, encoding: .utf8) { NSLog("%@", prefix + line) }
        else { NSLog("%@", "[ACCOUNT-INPUT-ENCODING-ERROR] \(prefix) \(payload)") }
    }
}

private extension UIWindow {
    @objc dynamic func accountProbeSendEvent(_ event: UIEvent) {
        let before = AccountPickerInputProbe.sample(event, window: self, stage: "before")
        accountProbeSendEvent(event) // After the exchange, this is original sendEvent, exactly once.
        let after = AccountPickerInputProbe.sample(event, window: self, stage: "after")
        AccountPickerInputProbe.emit(before + after) // Logging follows original event delivery.
    }
}

#endif
