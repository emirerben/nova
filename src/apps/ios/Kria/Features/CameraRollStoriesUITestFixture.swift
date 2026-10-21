#if DEBUG
import SwiftUI
import Foundation

/// Offline fixture of the production review screen and API encoders. No media/provider/network access.
struct CameraRollStoriesUITestHost: View {
    let api: any KriaAPIClient
    let collection: CameraRollCollection
    let result: CameraRollStories
    init() {
        let config = URLSessionConfiguration.ephemeral
        config.protocolClasses = [CameraRollStoriesURLProtocol.self]
        api = KriaAPI(baseURL: URL(string: "https://camera-roll-stories.invalid")!, tokenStore: ChatUITestTokenStore(), session: URLSession(configuration: config))
        result = try! JSONDecoder().decode(CameraRollStories.self, from: JSONSerialization.data(withJSONObject: ["stories": [CameraRollStoryFixture.story], "coverage": ["moments": CameraRollStoryFixture.moments, "incomplete_speech_asset_ids": [], "unindexed_asset_ids": []]]))
        var value = try! CameraRollCollection(name: "Weekend outing", identifiers: ["fixture-source"])
        value.serverRevision = 1
        let asset: [String: Any] = ["id": CameraRollStoryFixture.assetID, "kind": "video", "status": "ready", "original_availability": "cloud_backed", "metadata_json": ["filename": "Outing.mp4"], "sources": [], "usage": [], "storage_warnings": []]
        value.bind(try! JSONDecoder().decode(SavedMediaAsset.self, from: JSONSerialization.data(withJSONObject: asset)), to: "fixture-source")
        collection = value
    }
    var body: some View { CameraRollStoriesView(api: api, collection: collection, result: result, onSelected: { _ in }) }
}
/// Exercises the real collection -> index -> recommendation UI with an unavailable
/// timed fact. Optional private preview inputs stay in the simulator's Documents;
/// all API requests remain intercepted and nothing can be uploaded or published.
struct CameraRollCollectionReadyUITestHost: View {
    private let api: KriaAPI
    @State private var prepared = false
    init() {
        let config = URLSessionConfiguration.ephemeral
        config.protocolClasses = [CameraRollStoriesURLProtocol.self]
        api = KriaAPI(baseURL: URL(string: "https://camera-roll-stories.invalid")!, tokenStore: CollectionPreviewTokenStore(), session: URLSession(configuration: config))
        let assets = CameraRollStoryFixture.input?["assets"] as? [[String: Any]] ?? [CameraRollStoryFixture.asset(CameraRollStoryFixture.assetID, source: "BBBBBBBB-BBBB-BBBB-BBBB-BBBBBBBBBBBB"), CameraRollStoryFixture.asset("CCCCCCCC-CCCC-CCCC-CCCC-CCCCCCCCCCCC", source: "DDDDDDDD-DDDD-DDDD-DDDD-DDDDDDDDDDDD")]
        let decoded = assets.prefix(50).compactMap { try? JSONDecoder().decode(SavedMediaAsset.self, from: JSONSerialization.data(withJSONObject: $0)) }
        var value = try! CameraRollCollection(id: UUID(uuidString: "FFFFFFFF-FFFF-FFFF-FFFF-FFFFFFFFFFFF")!, name: CameraRollStoryFixture.input?["name"] as? String ?? "Weekend outing", identifiers: decoded.map { "saved:\($0.id)" })
        value.serverRevision = 1
        for asset in decoded { value.bind(asset, to: "saved:\(asset.id)") }
        if let owner = api.mediaLibraryOwner { try? value.save(in: MediaLibraryOriginals(owner: owner)) }
    }
    var body: some View {
        Group {
            if prepared { CameraRollCollectionView(api: api) }
            else { ProgressView().task {
                let files = CameraRollStoryFixture.input?["preview_files"] as? [String: String] ?? [:]
                for (id, name) in files.prefix(50) where name == URL(fileURLWithPath: name).lastPathComponent {
                    guard let assetID = UUID(uuidString: id) else { continue }
                    let file = CameraRollStoryFixture.directory.appendingPathComponent(name).resolvingSymlinksInPath()
                    guard file.path.hasPrefix(CameraRollStoryFixture.directory.resolvingSymlinksInPath().path + "/") else { continue }
                    if FileManager.default.fileExists(atPath: file.path) { await CreationMediaPreview.save(localURL: file, mediaID: "library-\(assetID.uuidString)") }
                }
                prepared = true
            } }
        }
    }
}
private struct CollectionPreviewTokenStore: TokenStore {
    func read() throws -> MobileSession? {
        let payload = Data("{\"sub\":\"EEEEEEEE-EEEE-EEEE-EEEE-EEEEEEEEEEEE\"}".utf8).base64EncodedString()
        return MobileSession(accessToken: "fixture.\(payload).fixture", refreshToken: "fixture", expiresIn: 3600)
    }
    func write(_ session: MobileSession) throws { }
    func delete() throws { }
}
private enum CameraRollStoryFixture {
    static var directory: URL { FileManager.default.urls(for: .documentDirectory, in: .userDomainMask)[0].appendingPathComponent("camera-roll-owned-preview") }
    private static let encodedInput: Data? = {
        guard ProcessInfo.processInfo.arguments.contains("-camera-roll-own-preview"), let data = try? Data(contentsOf: directory.appendingPathComponent("input.json")), data.count <= 2 * 1024 * 1024 else { return nil }
        return data
    }()
    static var input: [String: Any]? {
        guard let encodedInput else { return nil }
        return (try? JSONSerialization.jsonObject(with: encodedInput)) as? [String: Any]
    }
    static func asset(_ id: String, source: String) -> [String: Any] {
        ["id": id, "kind": "video", "status": "ready", "original_availability": "cloud_backed", "metadata_json": ["filename": "Outing.mp4"], "sources": [["id": source, "role": "analysis_proxy", "status": "available", "byte_count": 1000, "content_type": "video/mp4", "generation": "42"]], "usage": [], "storage_warnings": []]
    }
    static var result: [String: Any] {
        input?["result"] as? [String: Any] ?? ["stories": [story], "coverage": ["moments": moments, "incomplete_speech_asset_ids": [], "unindexed_asset_ids": ["CCCCCCCC-CCCC-CCCC-CCCC-CCCCCCCCCCCC"]]]
    }
    static let assetID = "AAAAAAAA-AAAA-AAAA-AAAA-AAAAAAAAAAAA"
    static var moments: [[String: Any]] {
        if let coverage = (input?["result"] as? [String: Any])?["coverage"] as? [String: Any], let moments = coverage["moments"] as? [[String: Any]] { return moments }
        return [
        moment("11111111-2222-2222-2222-222222222222", text: "An illuminated house", start: 150, end: 157),
        moment("22222222-2222-2222-2222-222222222222", text: "A ferry crossing", start: 160, end: 166),
        moment("33333333-3333-3333-3333-333333333333", text: "The final arrival", start: 170, end: 174),
    ] }
    static func moment(_ id: String, text: String, start: Double, end: Double) -> [String: Any] {
        ["id": id, "asset_id": assetID, "source_id": "BBBBBBBB-BBBB-BBBB-BBBB-BBBBBBBBBBBB", "generation": "42", "start_s": start, "end_s": end, "kind": "visual", "text": text, "uncertain": true]
    }
    static var story: [String: Any] {
        if let stories = (input?["result"] as? [String: Any])?["stories"] as? [[String: Any]], let first = stories.first { return first }
        return ["id": "11111111-1111-1111-1111-111111111111", "revision": 1, "title": "Boat journey", "hook": "Start on the water", "payoff": "Finish at the destination", "rationale": "A crossing becomes an arrival.", "duration_s": 13, "moments": Array(moments.prefix(2)), "rejected": false, "audio_policy": "preserve_source_audio"]
    }
}
private final class CameraRollStoriesURLProtocol: URLProtocol, @unchecked Sendable {
    override class func canInit(with request: URLRequest) -> Bool { true }
    override class func canonicalRequest(for request: URLRequest) -> URLRequest { request }
    private static let server = CameraRollStoriesFixtureServer()
    override func startLoading() {
        let (status, story) = Self.server.respond(request)
        let response = HTTPURLResponse(url: request.url!, statusCode: status, httpVersion: nil, headerFields: ["Content-Type": "application/json"])!
        client?.urlProtocol(self, didReceive: response, cacheStoragePolicy: .notAllowed)
        client?.urlProtocol(self, didLoad: try! JSONSerialization.data(withJSONObject: story))
        client?.urlProtocolDidFinishLoading(self)
    }
    override func stopLoading() {}
}
private final class CameraRollStoriesFixtureServer: @unchecked Sendable {
    private let lock = NSLock()
    private var story = CameraRollStoryFixture.story
    private var taste: [String: Any] = ["revision": 0, "enabled": false, "memory_paused": false, "rules": [], "examples": []]
    func respond(_ request: URLRequest) -> (Int, Any) {
        lock.lock(); defer { lock.unlock() }
        if request.httpMethod == "GET", request.url?.path.hasSuffix("/editorial-taste") == true { return (200, taste) }
        if request.httpMethod == "GET", request.url?.path.hasSuffix("/creation-threads") == true {
            return (200, [["id": "AAAAAAAA-AAAA-AAAA-AAAA-AAAAAAAAAAAA", "title": "A saved outing", "status": "active", "revision": 1, "runtime_version": 2, "active_plan_item_id": "AAAAAAAA-AAAA-AAAA-AAAA-AAAAAAAAAAAA", "updated_at": "2026-10-19T10:00:00Z"]])
        }
        if request.httpMethod == "GET", request.url?.path.hasSuffix("/access-url") == true,
           let files = CameraRollStoryFixture.input?["preview_files"] as? [String: String],
           let assets = CameraRollStoryFixture.input?["assets"] as? [[String: Any]],
           let asset = assets.first(where: { request.url!.path.lowercased().contains(($0["id"] as? String ?? "invalid").lowercased()) }),
           let id = asset["id"] as? String, let name = files[id], name == URL(fileURLWithPath: name).lastPathComponent,
           let source = (asset["sources"] as? [[String: Any]])?.first, let generation = source["generation"] {
            let file = CameraRollStoryFixture.directory.appendingPathComponent(name).resolvingSymlinksInPath()
            guard file.path.hasPrefix(CameraRollStoryFixture.directory.resolvingSymlinksInPath().path + "/") else { return (404, [:]) }
            return (200, ["url": file.absoluteString, "generation": generation])
        }
        var data = request.httpBody ?? Data()
        if data.isEmpty, let stream = request.httpBodyStream {
            stream.open(); defer { stream.close() }
            var buffer = [UInt8](repeating: 0, count: 1024)
            while stream.hasBytesAvailable && data.count < 65536 {
                let count = stream.read(&buffer, maxLength: buffer.count)
                if count <= 0 { break }; data.append(contentsOf: buffer.prefix(count))
            }
        }
        guard let body = (try? JSONSerialization.jsonObject(with: data)) as? [String: Any] else { return (400, story) }
        if request.url?.path.hasSuffix("/editorial-taste") == true {
            guard body["expected_revision"] as? Int == taste["revision"] as? Int else { return (409, ["detail": "Taste changed"]) }
            let revision = (taste["revision"] as? Int ?? 0) + 1
            if request.httpMethod == "DELETE" {
                taste = ["revision": revision, "enabled": false, "memory_paused": false, "rules": [], "examples": []]
            } else {
                guard body["confirmed"] as? Bool == true, let ids = body["project_ids"] as? [String], !ids.isEmpty else { return (422, ["detail": "Confirmation required"]) }
                taste = ["revision": revision, "enabled": body["enabled"] ?? false, "memory_paused": false, "rules": body["rules"] ?? [], "examples": ids.map { ["id": $0, "project_id": $0, "title": "A saved outing", "kind": "video"] }]
            }
            return (200, taste)
        }
        if request.url?.path.hasSuffix("/editorial-taste/feedback") == true {
            guard body["verdict"] as? String == "would_post", body["expected_story_revision"] as? Int == story["revision"] as? Int, body["story_id"] as? String == "11111111-1111-1111-1111-111111111111" else { return (422, ["detail": "Unexpected judgment payload"]) }
            taste["revision"] = (taste["revision"] as? Int ?? 0) + 1
            return (200, ["id": UUID().uuidString, "recorded": true])
        }
        if request.httpMethod == "PUT" {
            return (200, ["id": request.url!.lastPathComponent, "revision": (body["expected_revision"] as? Int ?? 0) + 1])
        }
        if request.url?.path.hasSuffix("/index") == true {
            let missing = (CameraRollStoryFixture.result["coverage"] as? [String: Any])?["unindexed_asset_ids"] ?? ["CCCCCCCC-CCCC-CCCC-CCCC-CCCCCCCCCCCC"]
            return (200, ["next_offset": NSNull(), "missing_analysis_asset_ids": missing, "unavailable_asset_ids": [], "untimed_asset_ids": []])
        }
        if request.url?.path.hasSuffix("/stories") == true { return (200, CameraRollStoryFixture.result) }
        if request.httpMethod == "PATCH" {
            if let id = body["moment_id"] as? String, let index = body["replace_index"] as? Int,
               let replacement = CameraRollStoryFixture.moments.first(where: { ($0["id"] as? String)?.lowercased() == id.lowercased() }) {
                var moments = story["moments"] as! [[String: Any]]
                moments[index] = replacement; story["moments"] = moments
            } else if let policy = body["audio_policy"] as? String { story["audio_policy"] = policy }
            else if let order = body["moment_order"] as? [String] {
                let moments = story["moments"] as! [[String: Any]]
                story["moments"] = order.compactMap { id in moments.first { ($0["id"] as? String)?.lowercased() == id.lowercased() } }
            } else { return (400, story) }
            story["revision"] = (story["revision"] as! Int) + 1
            story["duration_s"] = (story["moments"] as! [[String: Any]]).reduce(0.0) { $0 + ($1["end_s"] as! Double) - ($1["start_s"] as! Double) }
        } else if request.url?.path.hasSuffix("/expose") == true { return (200, story) }
        else if request.url?.path.hasSuffix("/reject") == true { story["rejected"] = true }
        else { return (500, story) }
        return (200, story)
    }
}
#endif
