#if DEBUG
import Foundation

/// All requests are intercepted: account UI tests never contact a real account.
enum AccountUITestTransport {
    static func api(tokenStore: TokenStore) -> KriaAPI {
        let configuration = URLSessionConfiguration.ephemeral
        configuration.protocolClasses = [AccountUITestURLProtocol.self]
        return KriaAPI(baseURL: URL(string: "https://account-ui-testing.invalid")!,
                       tokenStore: tokenStore, session: URLSession(configuration: configuration))
    }
}

final class AccountUITestTokenStore: TokenStore, @unchecked Sendable {
    private let lock = NSLock()
    private var session: MobileSession?
    func updateSession(_ replacement: MobileSession?, ifCurrent expected: MobileSession) throws -> SessionStoreUpdate {
        lock.withLock {
            guard session == expected else { return SessionStoreUpdate(applied: false, current: session) }
            session = replacement
            return SessionStoreUpdate(applied: true, current: session)
        }
    }
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
        if path.hasPrefix("/me/memory"), mode.hasPrefix("text-preferences") || mode == "whats-new" {
            let result = AccountTextPreferenceFixture.shared.reply(request, stale: mode.hasSuffix("stale"), learning: mode.hasSuffix("learning"),
                                                                   customColor: mode.hasSuffix("custom-color"))
            deliver(result)
            return
        }
        switch path {
        case "/me/account/delete-request":
            result = mode == "unavailable" ? (503, "{}") : (202, "{\"requested\":true}")
        case "/me/account/delete-confirm":
            result = mode == "invalid-code" ? (400, "{}") : (204, "")
        case "/creation-threads/capabilities":
            // `whats-new` is an account where every gated guide card is available; any other mode fails the read.
            result = mode == "whats-new"
                ? (200, "{\"formats\":[{\"id\":\"montage\",\"edit_format\":\"montage\",\"max_clips\":50},{\"id\":\"lipsync\",\"edit_format\":\"montage\",\"max_clips\":10}],\"clip_selection_questions\":true,\"media_collections\":true}")
                : (503, "{}")
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
    /// Rows persist per key and id like the real ledger, so a Save of one type never replaces another.
    private var items: [[String: Any]] = []
    private var createdCount = 0
    private var recentOperation: [String: Any]?
    private var operationItemID: String?
    private var learningInitialized = false
    private var customColorSeeded = false
    private var learningEnabled = false
    private var learningDecided = false
    private var learningSuggestion: [String: Any] {
        ["id": "learning-position", "normalized_key": "text_style_title", "source_kind": "finished_edits",
         "instruction": "Place title text at top left in future videos.",
         "structured_value": ["text_style_title": ["position": "top_left"]],
         "evidence": ["supporting_video_count": 3]]
    }
    private static var textStyleDefaults: [String: Any] {
        func role(placement: String = "auto", usual: String? = nil, fontHint: String? = nil) -> [String: Any] {
            var value: [String: Any] = ["outline": "off", "shadow": "off", "color": "auto", "font": "auto", "placement": placement,
                                        "placement_order": placement == "auto" ? ["bottom_left", "top_left", "middle", "top_right"] : []]
            if let usual { value["usual_position"] = usual }
            if let fontHint { value["font_hint"] = fontHint }
            return value
        }
        let anchors: [String: (Double, Double, String)] = [
            "top_left": (0.08, 0.12, "left"), "top": (0.5, 0.12, "center"), "top_right": (0.92, 0.12, "right"),
            "middle": (0.5, 0.5, "center"), "bottom_left": (0.08, 0.78, "left"), "bottom": (0.5, 0.78, "center"),
            "bottom_right": (0.92, 0.78, "right"),
        ]
        return ["roles": ["all_text": role(), "title": role(), "label": role(placement: "usual", usual: "bottom"),
                          "pinned": role(placement: "usual"),
                          "caption": role(fontHint: "TikTok Sans Bold"), "lyric": role()],
                "position_anchors": anchors.mapValues { ["x": $0.0, "y": $0.1, "alignment": $0.2] },
                "color_hint": "White, black or gray, whichever reads best on each shot."]
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
    func reply(_ request: URLRequest, stale: Bool, learning: Bool, customColor: Bool = false, initiallyEnabled: Bool = false) -> (Int, String) {
        lock.lock(); defer { lock.unlock() }
        let path = request.url?.path ?? ""
        if learning && !learningInitialized {
            learningInitialized = true
            learningEnabled = initiallyEnabled
            items = [["id": "approved-title", "normalized_key": "text_style_title", "instruction": "My title choices",
                      "structured_value": ["text_style_title": ["shadow_enabled": false, "stroke_width": 0]]]]
        }
        if customColor && !customColorSeeded {
            customColorSeeded = true
            items = [["id": "approved-all-text", "normalized_key": "text_style_all_text", "instruction": "My text choices",
                      "structured_value": ["text_style_all_text": ["color": "#7A3FD1"]]]]
        }
        if request.httpMethod == "GET" {
            let body: [String: Any] = ["feature_available": true, "text_preferences_available": true, "enabled": true, "revision": revision,
                                      "active": items,
                                      "suggestions": learningEnabled && !learningDecided ? [learningSuggestion] : [],
                                      "edit_learning": ["available": learning, "enabled": learningEnabled], "recent_undo": recentOperation as Any? ?? NSNull(),
                                      "text_style_defaults": Self.textStyleDefaults]
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
            if let index = items.firstIndex(where: { $0["normalized_key"] as? String == "text_style_title" }) {
                items[index]["structured_value"] = ["text_style_title": ["position": "top_left", "shadow_enabled": false, "stroke_width": 0]]
            }
        } else if learning && path.hasSuffix("/learning-position/suggestion") {
            learningDecided = true
        } else if request.httpMethod == "DELETE" {
            let id = path.split(separator: "/").last.map(String.init)
            items.removeAll { $0["id"] as? String == id }
            operationItemID = id
        } else if path.contains("/items"), let data = bodyData(request),
                  var value = (try? JSONSerialization.jsonObject(with: data)) as? [String: Any] {
            let id = path.hasSuffix("/items") ? nil : path.split(separator: "/").last.map(String.init)
            if let id, let index = items.firstIndex(where: { $0["id"] as? String == id }) {
                value["id"] = id
                items[index] = value
            } else {
                createdCount += 1
                value["id"] = String(format: "00000000-0000-0000-0000-%012d", 647 + createdCount)
                items.append(value)
            }
            operationItemID = value["id"] as? String
        }
        revision += 1
        recentOperation = ["operation_id": "00000000-0000-0000-0000-000000000649",
                           "item_id": operationItemID as Any? ?? NSNull(), "revision": revision,
                           "undo_expires_at": "2099-01-01T00:00:00Z"]
        return (200, String(decoding: try! JSONSerialization.data(withJSONObject: recentOperation!), as: UTF8.self))
    }
}

#endif
