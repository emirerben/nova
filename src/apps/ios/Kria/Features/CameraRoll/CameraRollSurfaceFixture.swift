#if DEBUG
import SwiftUI
import Foundation

/// Offline fixtures for the Camera roll surface. Every request is intercepted; nothing touches Photos, the network
/// or a provider. Scenarios come from launch arguments:
///   `-collection-mixed-states`  every tile state in one event, plus an event that already has ideas (default)
///   `-collection-empty`         a creator who has chosen nothing yet
///   `-collection-access-notices` the limited-access and access-removed notices (Paper A6)
/// `-camera-roll-collection` hosts the surface on its own; `-camera-roll-surface-fixture` swaps the fixture in under
/// the real drawer (`-ui-testing-chat`).
enum CameraRollSurfaceFixture {
    static var isDrawerFixture: Bool { ProcessInfo.processInfo.arguments.contains("-camera-roll-surface-fixture") }
    private static func has(_ flag: String) -> Bool { ProcessInfo.processInfo.arguments.contains(flag) }

    static let collectionID = UUID(uuidString: "DDDDDDDD-DDDD-4DDD-8DDD-DDDDDDDDDDDD")!
    static let saturday = "Saturday · Bodrum"
    static let sunday = "Sunday · Istanbul"

    /// A fixed asset id per fixture item, so the server fixture and the seeded collection agree.
    static func assetID(_ number: Int) -> String { String(format: "00000000-0000-4000-8000-%012d", number) }
    static func sourceID(_ number: Int) -> String { String(format: "00000000-0000-4000-9000-%012d", number) }

    struct Item {
        let key: String
        let number: Int
        let kind: String
        let duration: Double
        let group: String
        let state: String
        var reason: String? = nil
        var retryable = false
        /// Bound to an uploaded asset. The uploading tile is still on the phone, so it is not.
        var bound = true
        var entryID: String { "fixture-\(key)" }
    }

    static let items: [Item] = [
        Item(key: "queued", number: 1, kind: "video", duration: 9, group: saturday, state: "queued"),
        Item(key: "uploading", number: 2, kind: "image", duration: 0, group: saturday, state: "uploading", bound: false),
        Item(key: "analyzing", number: 3, kind: "video", duration: 22, group: saturday, state: "analyzing"),
        Item(key: "ready", number: 4, kind: "image", duration: 0, group: saturday, state: "ready"),
        Item(key: "needs-retry", number: 5, kind: "video", duration: 14, group: saturday, state: "needs_retry", reason: "provider_error", retryable: true),
        Item(key: "failed", number: 6, kind: "video", duration: 31, group: saturday, state: "failed", reason: "no_usable_moment"),
        Item(key: "excluded", number: 7, kind: "image", duration: 0, group: saturday, state: "excluded", reason: "unsupported_type"),
        Item(key: "ready-video", number: 8, kind: "video", duration: 17, group: saturday, state: "ready"),
        Item(key: "sunday-1", number: 11, kind: "video", duration: 14, group: sunday, state: "ready"),
        Item(key: "sunday-2", number: 12, kind: "image", duration: 0, group: sunday, state: "ready"),
        Item(key: "sunday-3", number: 13, kind: "video", duration: 6, group: sunday, state: "ready"),
        Item(key: "sunday-4", number: 14, kind: "video", duration: 8, group: sunday, state: "ready"),
        Item(key: "sunday-5", number: 15, kind: "image", duration: 0, group: sunday, state: "ready"),
        Item(key: "sunday-6", number: 16, kind: "video", duration: 11, group: sunday, state: "ready")
    ]

    static func assetJSON(_ item: Item) -> [String: Any] {
        let role = item.kind == "video" ? "analysis_proxy" : "original"
        return ["id": assetID(item.number), "kind": item.kind, "status": "ready", "original_availability": "cloud_backed",
                "metadata_json": ["filename": "\(item.key).\(item.kind == "video" ? "mov" : "heic")"],
                "sources": [["id": sourceID(item.number), "role": role, "status": "available", "byte_count": 1000,
                             "content_type": item.kind == "video" ? "video/mp4" : "image/heic", "generation": "42"]],
                "usage": [], "storage_warnings": []]
    }

    static func api() -> KriaAPI {
        let config = URLSessionConfiguration.ephemeral
        config.protocolClasses = [CameraRollSurfaceURLProtocol.self]
        return KriaAPI(baseURL: URL(string: "https://camera-roll-surface.invalid")!, tokenStore: ChatUITestTokenStore(), session: URLSession(configuration: config))
    }

    @MainActor static func model() -> CameraRollSurfaceModel {
        let empty = has("-collection-empty")
        var collection = try! CameraRollCollection(id: collectionID, name: "My outing", identifiers: empty ? [] : items.map(\.entryID))
        var events: [String: [CameraRollItemEvent]] = [:]
        if !empty {
            collection.record(items.map { CameraRollMetadata(id: $0.entryID, kind: $0.kind, duration: $0.duration, width: 1080, height: 1920, modifiedAt: nil, capture: nil) }, sharesCaptureContext: false)
            for item in items {
                collection.setGroup(item.group, for: item.entryID)
                if item.bound, let asset = try? JSONDecoder().decode(SavedMediaAsset.self, from: JSONSerialization.data(withJSONObject: assetJSON(item))) {
                    collection.bind(asset, to: item.entryID)
                }
            }
            collection.serverRevision = 1
            events["fixture-uploading"] = [.exportStarted, .uploadProgress(0.38)]
        }
        let model = CameraRollSurfaceModel(api: api(), mode: .fixture, seed: collection, localEvents: events)
        if has("-collection-access-notices") { model.seedAccessNoticesForFixture(removed: 14, shared: 120) }
        return model
    }
}

/// The surface on its own, without the drawer (`-camera-roll-collection`).
struct CameraRollSurfaceUITestHost: View {
    @State private var model = CameraRollSurfaceFixture.model()
    @State private var opened = false
    var body: some View {
        ZStack {
            CameraRollSurfaceView(model: model, openProjects: {}, onSelected: { _ in opened = true })
            if opened { Text("Opened a chat").accessibilityIdentifier("camera-roll-opened-chat") }
        }
    }
}

/// Photos metadata only; nothing leaves the phone (`-camera-roll-device-collection`). For on-device QA of selection,
/// grouping and resume against a real library.
struct CameraRollDeviceDiagnosticHost: View {
    @State private var model: CameraRollSurfaceModel
    init(api: any KriaAPIClient) { _model = State(initialValue: CameraRollSurfaceModel(api: api, mode: .metadataOnly)) }
    var body: some View { CameraRollSurfaceView(model: model, openProjects: {}, onSelected: { _ in }) }
}

private final class CameraRollSurfaceURLProtocol: URLProtocol, @unchecked Sendable {
    override class func canInit(with request: URLRequest) -> Bool { true }
    override class func canonicalRequest(for request: URLRequest) -> URLRequest { request }
    private static let server = CameraRollSurfaceFixtureServer()
    override func startLoading() {
        let (status, body) = Self.server.respond(request)
        let response = HTTPURLResponse(url: request.url!, statusCode: status, httpVersion: nil, headerFields: ["Content-Type": "application/json"])!
        client?.urlProtocol(self, didReceive: response, cacheStoragePolicy: .notAllowed)
        client?.urlProtocol(self, didLoad: (try? JSONSerialization.data(withJSONObject: body)) ?? Data())
        client?.urlProtocolDidFinishLoading(self)
    }
    override func stopLoading() {}
}

/// The collection routes the surface calls, with just enough state to make delete, Undo, retry and "Another idea"
/// behave like the real API (soft delete and restore, a retried item staying "analyzing").
private final class CameraRollSurfaceFixtureServer: @unchecked Sendable {
    private let lock = NSLock()
    private var retried = Set<String>()
    private var deleted = Set<String>()
    private var stories: [[String: Any]] = CameraRollSurfaceFixtureServer.seedStories()
    private var revision = 1
    private var generated = 0
    private var taste: [String: Any] = ["revision": 0, "enabled": true, "memory_paused": false, "rules": [], "examples": []]

    func respond(_ request: URLRequest) -> (Int, Any) {
        lock.lock(); defer { lock.unlock() }
        let path = request.url?.path ?? ""
        let method = request.httpMethod ?? "GET"
        let body = (try? JSONSerialization.jsonObject(with: Self.bodyData(request))) as? [String: Any] ?? [:]
        let query = URLComponents(url: request.url!, resolvingAgainstBaseURL: false)?.queryItems ?? []

        if method == "GET", path.hasSuffix("/editorial-taste") { return (200, taste) }
        if method == "PUT", path.hasSuffix("/editorial-taste") {
            let next = (taste["revision"] as? Int ?? 0) + 1
            let ids = body["project_ids"] as? [String] ?? []
            taste = ["revision": next, "enabled": body["enabled"] ?? true, "memory_paused": false, "rules": body["rules"] ?? [],
                     "examples": ids.map { ["id": $0, "project_id": $0, "title": "A saved outing", "kind": "video"] }]
            return (200, taste)
        }
        if method == "DELETE", path.hasSuffix("/editorial-taste") {
            taste = ["revision": (taste["revision"] as? Int ?? 0) + 1, "enabled": false, "memory_paused": false, "rules": [], "examples": []]
            return (200, taste)
        }
        if method == "GET", path.hasSuffix("/creation-threads") {
            return (200, [["id": CameraRollSurfaceFixture.assetID(90), "title": "Ferry day", "status": "active", "revision": 1, "runtime_version": 2,
                           "active_plan_item_id": CameraRollSurfaceFixture.assetID(90), "updated_at": "2026-10-19T10:00:00Z"]])
        }
        if method == "POST", path.hasSuffix("/retry"), let item = CameraRollSurfaceFixture.items.first(where: { path.contains($0.assetIDString) }) {
            retried.insert(item.assetIDString)
            return (200, CameraRollSurfaceFixture.assetJSON(item))
        }
        if method == "GET", path.hasSuffix("/items") { return (200, ["revision": revision, "items": itemsSnapshot()]) }
        if method == "POST", path.hasSuffix("/index") {
            return (200, ["next_offset": NSNull(), "missing_analysis_asset_ids": [], "unavailable_asset_ids": [], "untimed_asset_ids": []])
        }
        if method == "PUT", path.contains("/media-collections/") {
            return (200, ["id": CameraRollSurfaceFixture.collectionID.uuidString, "revision": revision, "name": body["name"] ?? "My outing", "members": body["members"] ?? []])
        }
        if method == "DELETE", path.hasSuffix("/groups") {
            let group = body["group"] as? String ?? ""
            var removed: [String] = []
            for story in stories where story["group"] as? String == group && !deleted.contains(story["id"] as? String ?? "") {
                let id = story["id"] as! String; deleted.insert(id); removed.append(id)
            }
            revision += 1
            return (200, ["revision": revision, "deleted_story_ids": removed])
        }
        if path.contains("/stories/") {
            let id = path.split(separator: "/").last(where: { UUID(uuidString: String($0)) != nil }).map { String($0).lowercased() } ?? ""
            if method == "DELETE" {
                deleted.insert(id)
                return (200, ["restore_until": "2026-10-26T12:00:00Z"])
            }
            if method == "POST", path.hasSuffix("/restore") {
                deleted.remove(id)
                return (200, stories.first { ($0["id"] as? String)?.lowercased() == id } ?? [:])
            }
            if method == "POST", path.hasSuffix("/expose") { return (200, stories.first { ($0["id"] as? String)?.lowercased() == id } ?? [:]) }
            return (200, stories.first { ($0["id"] as? String)?.lowercased() == id } ?? [:])
        }
        if method == "GET", path.hasSuffix("/stories") {
            let group = query.first { $0.name == "group" }?.value
            let live = stories.filter { !deleted.contains($0["id"] as? String ?? "") && (group == nil || $0["group"] as? String == group) }
            return (200, ["revision": revision, "stories": live])
        }
        if method == "POST", path.hasSuffix("/stories") {
            let group = body["group"] as? String ?? CameraRollSurfaceFixture.saturday
            generated += 1
            let story = Self.story(index: 100 + generated, group: group, title: generated == 1 ? "The boat day, start to finish" : "Another angle on Bodrum", form: "atmosphere", assets: [4, 8])
            stories.append(story)
            return (200, ["revision": revision, "stories": [story], "suggested_research_topic": "Bodrum",
                          "coverage": ["moments": [], "incomplete_speech_asset_ids": [], "unindexed_asset_ids": []]])
        }
        return (404, ["detail": "Not found"])
    }

    private func itemsSnapshot() -> [[String: Any]] {
        CameraRollSurfaceFixture.items.filter(\.bound).map { item in
            var state = item.state
            if retried.contains(item.assetIDString) { state = "analyzing" }
            var value: [String: Any] = ["asset_id": item.assetIDString, "kind": item.kind, "group": item.group, "state": state,
                                        "retryable": state == "needs_retry", "attempts": retried.contains(item.assetIDString) ? 2 : 1, "updated_at": "2026-10-26T10:00:00Z"]
            if state != "analyzing", let reason = item.reason { value["reason"] = reason }
            return value
        }
    }

    private static func seedStories() -> [[String: Any]] {
        [story(index: 1, group: CameraRollSurfaceFixture.sunday, title: "Three stops for a perfect day in Istanbul", form: "guide", assets: [11, 13, 14, 16], question: true),
         story(index: 2, group: CameraRollSurfaceFixture.sunday, title: "Ferry light, Sunday morning", form: "atmosphere", assets: [12, 15]),
         story(index: 3, group: CameraRollSurfaceFixture.sunday, title: "The simit I'd cross the city for", form: "review", assets: [13, 16])]
    }

    static func storyID(_ index: Int) -> String { String(format: "11111111-1111-4111-8111-%012d", index) }

    static func story(index: Int, group: String, title: String, form: String, assets: [Int], question: Bool = false) -> [String: Any] {
        let moments: [[String: Any]] = assets.enumerated().map { offset, number in
            ["id": String(format: "22222222-2222-4222-8222-%06d%06d", index, offset), "asset_id": CameraRollSurfaceFixture.assetID(number),
             "source_id": CameraRollSurfaceFixture.sourceID(number), "generation": "42", "start_s": 1.0, "end_s": 5.0, "kind": "visual",
             "text": "Moment \(offset + 1) of \(title)", "uncertain": false]
        }
        var plan: [String: Any] = ["version": 1, "format": "montage", "form": form, "audience": "People planning a trip", "angle": title,
                                   "why_it_works": "Real moments, in the order they happened.", "opening_text": "One day, three stops, zero tourist traps.",
                                   "caption": "A day worth repeating", "beat_reasons": assets.map { _ in "Shows the place" }]
        if question { plan["open_questions"] = [["id": "q1", "question": "What was the name of the ferry café?", "dismissed": false]] }
        return ["id": storyID(index), "revision": 1, "title": title, "hook": "Start on the water", "payoff": "Finish at the destination",
                "rationale": "A day becomes a story.", "duration_s": Double(assets.count * 4), "moments": moments, "rejected": false,
                "audio_policy": "preserve_source_audio", "content_plan": plan, "group": group]
    }

    private static func bodyData(_ request: URLRequest) -> Data {
        if let body = request.httpBody { return body }
        guard let stream = request.httpBodyStream else { return Data() }
        stream.open(); defer { stream.close() }
        var data = Data(); var buffer = [UInt8](repeating: 0, count: 1024)
        while stream.hasBytesAvailable && data.count < 65536 {
            let count = stream.read(&buffer, maxLength: buffer.count)
            if count <= 0 { break }
            data.append(contentsOf: buffer.prefix(count))
        }
        return data
    }
}

private extension CameraRollSurfaceFixture.Item {
    var assetIDString: String { CameraRollSurfaceFixture.assetID(number) }
}
#endif
