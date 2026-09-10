#if DEBUG
import Foundation

/// Offline chat fixture: every HTTP request is intercepted, even if a caller
/// changes its URL. The existing UI-only fallback supplies projects and drafts.
enum ChatUITestTransport {
    static func api() -> KriaAPI {
        let configuration = URLSessionConfiguration.ephemeral
        configuration.protocolClasses = [ChatUITestURLProtocol.self]
        return KriaAPI(
            baseURL: URL(string: "https://chat-ui-testing.invalid")!,
            tokenStore: ChatUITestTokenStore(),
            session: URLSession(configuration: configuration)
        )
    }
}

/// UI fixtures must never read or rotate a developer's Keychain session.
struct ChatUITestTokenStore: TokenStore {
    func read() throws -> MobileSession? { nil }
    func write(_ session: MobileSession) throws {}
    func delete() throws {}
}

private final class ChatUITestURLProtocol: URLProtocol, @unchecked Sendable {
    override class func canInit(with request: URLRequest) -> Bool { true }
    override class func canonicalRequest(for request: URLRequest) -> URLRequest { request }
    private var delayedResponse: DispatchWorkItem?
    override func startLoading() {
        if request.url?.path.hasSuffix("/messages") == true,
           ProcessInfo.processInfo.environment["KRIA_CHAT_SLOW_CREATION"] == "1" {
            let work = DispatchWorkItem { [weak self] in self?.finishLoading() }
            delayedResponse = work
            DispatchQueue.global().asyncAfter(deadline: .now() + 5, execute: work)
        } else { finishLoading() }
    }
    private func finishLoading() {
        let result = ProcessInfo.processInfo.environment["KRIA_CHAT_CREATION_FLOW"] != nil
            ? CreationChatFixture.shared.respond(request)
            : (503, Data())
        guard let url = request.url,
              let response = HTTPURLResponse(url: url, statusCode: result.0, httpVersion: nil, headerFields: ["Content-Type": "application/json"]) else {
            client?.urlProtocol(self, didFailWithError: URLError(.badURL))
            return
        }
        client?.urlProtocol(self, didReceive: response, cacheStoragePolicy: .notAllowed)
        client?.urlProtocol(self, didLoad: result.1)
        client?.urlProtocolDidFinishLoading(self)
    }
    override func stopLoading() { delayedResponse?.cancel() }
}
/// Deterministic API fixture uses the same native request/response decoder as a
/// real account. Enabled only by explicit UI-test environment in Debug builds.
private final class CreationChatFixture: @unchecked Sendable {
    static let shared = CreationChatFixture()
    private let lock = NSLock()
    private var threads: [String: [String: Any]] = [:]
    private var renders: [String: Int] = [:]
    private var preparations: [String: Int] = [:]
    private let approvalID = "aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa"
    private var runtime: Int { ProcessInfo.processInfo.environment["KRIA_CHAT_CREATION_FLOW"] == "v2" ? 2 : 1 }
    func respond(_ request: URLRequest) -> (Int, Data) {
        lock.lock(); defer { lock.unlock() }
        let path = request.url?.path ?? ""
        let body = (try? JSONSerialization.jsonObject(with: bodyData(request))) as? [String: Any] ?? [:]
        func response(_ object: Any, status: Int = 200) -> (Int, Data) { (status, (try? JSONSerialization.data(withJSONObject: object)) ?? Data()) }
        if path == "/creation-threads/capabilities" {
            return response(["formats": [("montage", "montage", 10), ("narrated", "narrated_planned", 10), ("talking_to_camera", "subtitled", 1)].map { ["id": $0.0, "edit_format": $0.1, "max_clips": $0.2] as [String: Any] }, "runtime_versions": runtime == 2 ? [1, 2] : [1], "visuals_enabled": true])
        }
        if path == "/creation-threads" {
            if request.httpMethod == "POST" {
                let id = UUID().uuidString
                let thread: [String: Any] = ["id": id, "title": "Untitled project", "status": "active", "revision": 0, "runtime_version": runtime, "state": [:], "events": [], "active_plan_item_id": id, "updated_at": "2026-09-10T10:00:00Z"]
                threads[id] = thread
                return response(thread, status: 201)
            }
            return response(Array(threads.values))
        }
        let parts = path.split(separator: "/").map(String.init)
        if parts.first == "plan-items" { return response(["assets": [], "max_assets": 10]) }
        guard parts.count >= 2, var thread = threads[parts[1]] else { return response(["detail": "Fixture route missing"], status: 404) }
        let id = parts[1]
        var state = thread["state"] as? [String: Any] ?? [:]
        var events = thread["events"] as? [[String: Any]] ?? []
        var revision = thread["revision"] as? Int ?? 0
        func append(_ type: String, role: String = "assistant", text: String? = nil, payload: [String: Any] = [:]) {
            revision += 1
            events.append(["id": UUID().uuidString, "sequence": events.count, "revision": revision, "role": role, "event_type": type, "content": text ?? "", "payload": payload, "created_at": "2026-09-10T10:00:00Z"])
        }
        if parts.last == "actions" {
            let action = body["action"] as? String ?? ""
            let payload = body["payload"] as? [String: Any] ?? [:]
            if action == "select_format" {
                state["format"] = payload["format"]
                if ProcessInfo.processInfo.environment["KRIA_CHAT_FIXTURE_MEDIA"] == "1" {
                    state["media"] = [["media_id": "fixture-clip", "kind": "video", "filename": "sample.mov"]]
                }
                append("action_select_format", payload: payload)
            } else if action == "generate", ProcessInfo.processInfo.environment["KRIA_CHAT_SLOW_CREATION"] == "1" {
                thread["creator_agent"] = ["status": "failed", "summary": "Open on the laugh and keep the pacing quick."]
                state["generation"] = ["status": "failed"]
                append("agent_assistant_error", text: "I couldn't start that render. Your creative plan is still saved.")
            } else if action == "retry", ProcessInfo.processInfo.environment["KRIA_CHAT_SLOW_CREATION"] == "1" {
                thread["creator_agent"] = ["status": "executing", "summary": "Open on the laugh and keep the pacing quick."]
                state["generation"] = ["status": "preparing"]
                preparations[id] = 0
                append("agent_assistant_execution", text: "I started the confirmed edit.")
            } else if action == "generate" {
                thread["active_job_id"] = id
                thread["job"] = ["id": id, "status": "processing", "variants": []]
                renders[id] = 0
                append("generation_started")
            } else if action == "remove_media" { state["media"] = []; append("action_remove_media") }
        } else if parts.last == "messages" || parts.last == "turns" {
            append("user_message", role: "user", text: body["message"] as? String)
            append("assistant_response", text: "Open on the laugh and keep the pacing quick.")
            if runtime == 2 { append("approval_requested", payload: ["approval_id": approvalID]) }
            else { thread["creator_agent"] = ["status": "awaiting_confirmation", "summary": "Open on the laugh and keep the pacing quick."] }
        } else if parts.contains("approvals") {
            if parts.last == "approve" {
                append("approval_approved")
                thread["active_job_id"] = id
                thread["job"] = ["id": id, "status": "processing", "variants": []]
                renders[id] = 0
            } else {
                return response(["approval_id": approvalID, "turn_id": id, "draft_id": id, "draft_revision": 1, "status": "pending", "consequence_summary": "Open on the laugh and keep the pacing quick.", "expires_at": ProcessInfo.processInfo.environment["KRIA_CHAT_EXPIRED_APPROVAL"] == "1" ? "2000-09-10T10:00:00Z" : "2099-09-10T10:00:00Z", "approval_fingerprint": String(repeating: "a", count: 64)])
            }
        } else if parts.count == 2, let count = preparations[id] {
            preparations[id] = count + 1
            if count >= 5 {
                preparations.removeValue(forKey: id)
                renders[id] = 0
                thread["active_job_id"] = id
                thread["job"] = ["id": id, "status": "processing", "variants": []]
            }
        } else if parts.count == 2, let count = renders[id] {
            renders[id] = count + 1
            if count >= 2 {
                thread["job"] = ["id": id, "status": "ready", "variants": [["variant_id": "original_text", "render_status": "ready", "output_url": "https://fixture.invalid/result.mp4"]]]
                renders[id] = nil
                append("generation_ready")
            }
        }
        thread["state"] = state; thread["events"] = events; thread["revision"] = revision; threads[id] = thread
        if parts.last == "delta" {
            let after = URLComponents(url: request.url!, resolvingAgainstBaseURL: false)?.queryItems?.first(where: { $0.name == "after_sequence" })?.value.flatMap(Int.init) ?? -1
            return response(["thread_id": id, "runtime_version": runtime, "status": "active", "after_sequence": after, "has_more": false, "thread_revision": revision, "events": events.filter { ($0["sequence"] as? Int ?? 0) > after }, "next_after_sequence": events.count - 1])
        }
        if parts.last == "turns" { return response(["turn_id": id, "thread_revision": revision, "status": "queued"], status: 202) }
        if parts.last == "approve" { return response(["approval_id": approvalID, "thread_id": id, "status": "approved", "thread_revision": revision]) }
        return response(thread)
    }
    private func bodyData(_ request: URLRequest) -> Data {
        if let data = request.httpBody { return data }
        guard let stream = request.httpBodyStream else { return Data() }
        stream.open(); defer { stream.close() }
        var data = Data(), buffer = [UInt8](repeating: 0, count: 4096)
        while stream.hasBytesAvailable {
            let count = stream.read(&buffer, maxLength: buffer.count)
            if count <= 0 { break }
            data.append(buffer, count: count)
        }
        return data
    }
}
#endif
