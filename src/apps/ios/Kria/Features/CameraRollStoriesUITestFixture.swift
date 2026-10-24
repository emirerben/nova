#if DEBUG
import SwiftUI
import Foundation
import AVFoundation
import KriaMediaEngine

/// Offline fixture of the production review screen and API encoders. No media/provider/network access.
struct CameraRollStoriesUITestHost: View {
    let api: any KriaAPIClient
    let collection: CameraRollCollection
    let result: CameraRollStories
    init() {
        let config = URLSessionConfiguration.ephemeral
        config.protocolClasses = [CameraRollStoriesURLProtocol.self]
        api = KriaAPI(baseURL: URL(string: "https://camera-roll-stories.invalid")!, tokenStore: ChatUITestTokenStore(), session: URLSession(configuration: config))
        result = try! JSONDecoder().decode(CameraRollStories.self, from: JSONSerialization.data(withJSONObject: ["stories": CameraRollStoryFixture.reviewStories, "coverage": ["moments": CameraRollStoryFixture.moments, "incomplete_speech_asset_ids": [], "unindexed_asset_ids": []]]))
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
    @State private var savedReadWriteAttempted = false
    @State private var recovered = false
    init() {
        let config = URLSessionConfiguration.ephemeral
        config.protocolClasses = [CameraRollStoriesURLProtocol.self]
        api = KriaAPI(baseURL: URL(string: "https://camera-roll-stories.invalid")!, tokenStore: CollectionPreviewTokenStore(), session: URLSession(configuration: config))
        let assets = CameraRollStoryFixture.input?["assets"] as? [[String: Any]] ?? [CameraRollStoryFixture.asset(CameraRollStoryFixture.assetID, source: "BBBBBBBB-BBBB-BBBB-BBBB-BBBBBBBBBBBB"), CameraRollStoryFixture.asset("CCCCCCCC-CCCC-CCCC-CCCC-CCCCCCCCCCCC", source: "DDDDDDDD-DDDD-DDDD-DDDD-DDDDDDDDDDDD")]
        let decoded = assets.prefix(50).compactMap { try? JSONDecoder().decode(SavedMediaAsset.self, from: JSONSerialization.data(withJSONObject: $0)) }
        var value = try! CameraRollCollection(id: UUID(uuidString: "FFFFFFFF-FFFF-FFFF-FFFF-FFFFFFFFFFFF")!, name: CameraRollStoryFixture.input?["name"] as? String ?? "Weekend outing", identifiers: decoded.map { "saved:\($0.id)" })
        value.serverRevision = 1
        for asset in decoded { value.bind(asset, to: "saved:\(asset.id)") }
        if ProcessInfo.processInfo.arguments.contains("-collection-saved-read-only"), let selection = value.remoteSelection {
            let result = try! JSONDecoder().decode(CameraRollStories.self, from: JSONSerialization.data(withJSONObject: CameraRollStoryFixture.result))
            value.savedIdeas = SavedCameraRollIdeas(revision: 1, selection: selection, query: "A previously saved query", researchTopic: nil, result: result)
        }
        if let owner = api.mediaLibraryOwner {
            let originals = MediaLibraryOriginals(owner: owner)
            if ProcessInfo.processInfo.arguments.contains("-collection-ready-reset") || (try? CameraRollCollection.load(id: value.id, in: originals, sharesCaptureContext: false)) == nil {
                try? value.save(in: originals)
            }
        }
    }
    var body: some View {
        Group {
            if recovered { Text("Saved idea reopened") }
            else if prepared { CameraRollCollectionView(api: api, onSelected: { thread in recovered = thread.revision == 10 }) }
            else { ProgressView().task {
                if ProcessInfo.processInfo.arguments.contains("-collection-saved-recovery"), let owner = api.mediaLibraryOwner {
                    do {
                        CameraRollStoryFixture.recoveryProgress("preparing video")
                        let file = try await CameraRollStoryFixture.prepareRecoveryOriginal()
                        CameraRollStoryFixture.recoveryProgress("video ready")
                        let fingerprint = try SHA256Fingerprinter().fingerprint(file: file)
                        let originals = MediaLibraryOriginals(owner: owner)
                        let id = UUID(uuidString: CameraRollStoryFixture.assetID)!
                        try await originals.relink(assetID: id, fingerprint: fingerprint, file: file)
                        CameraRollStoryFixture.recoveryProgress("original linked")
                        var collection = try CameraRollCollection.load(id: UUID(uuidString: "FFFFFFFF-FFFF-FFFF-FFFF-FFFFFFFFFFFF")!, in: originals, sharesCaptureContext: false)
                        let asset = try JSONDecoder().decode(SavedMediaAsset.self, from: JSONSerialization.data(withJSONObject: CameraRollStoryFixture.asset(CameraRollStoryFixture.assetID, source: "BBBBBBBB-BBBB-BBBB-BBBB-BBBBBBBBBBBB")))
                        collection.bind(asset, to: "saved:\(asset.id)")
                        CameraRollStoryFixture.recoveryProgress("bound; saved card matches: \(collection.matchingSavedIdeas != nil)")
                        try collection.save(in: originals)
                        CameraRollStoryFixture.recoveryProgress("collection saved")
                    } catch { CameraRollStoryFixture.recoveryProgress("setup failed: \(error)"); savedReadWriteAttempted = true }
                }
                let files = CameraRollStoryFixture.input?["preview_files"] as? [String: String] ?? [:]
                for (id, name) in files.prefix(50) where name == URL(fileURLWithPath: name).lastPathComponent {
                    guard let assetID = UUID(uuidString: id) else { continue }
                    let file = CameraRollStoryFixture.directory.appendingPathComponent(name).resolvingSymlinksInPath()
                    guard file.path.hasPrefix(CameraRollStoryFixture.directory.resolvingSymlinksInPath().path + "/") else { continue }
                    if FileManager.default.fileExists(atPath: file.path) { await CreationMediaPreview.save(localURL: file, mediaID: "library-\(assetID.uuidString)") }
                }
                prepared = true
            } }
        }.safeAreaInset(edge: .bottom) {
            if savedReadWriteAttempted { Text("Write attempted while reading saved idea") }
            if prepared && ProcessInfo.processInfo.arguments.contains("-collection-saved-recovery") {
                Text("Recovery fixture ready").font(.caption).accessibilityIdentifier("saved-recovery-fixture-ready")
            }
        }.onReceive(NotificationCenter.default.publisher(for: Notification.Name("CameraRollSavedReadWrite"))) { _ in
            savedReadWriteAttempted = true
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
        var value: [String: Any] = ["id": id, "kind": "video", "status": "ready", "original_availability": "cloud_backed", "metadata_json": ["filename": "Outing.mp4"], "sources": [["id": source, "role": "analysis_proxy", "status": "available", "byte_count": 1000, "content_type": "video/mp4", "generation": "42"]], "usage": [], "storage_warnings": []]
        if ProcessInfo.processInfo.arguments.contains("-collection-saved-recovery"), let fingerprint = try? SHA256Fingerprinter().fingerprint(file: recoveryOriginal) {
            value["original_declared_sha256"] = fingerprint.hex; value["original_byte_count"] = fingerprint.byteCount
        }
        return value
    }
    static func recoveryProgress(_ value: String) {
        try? FileManager.default.createDirectory(at: directory, withIntermediateDirectories: true)
        try? Data(value.utf8).write(to: directory.appendingPathComponent("saved-recovery-progress.txt"), options: .atomic)
    }
    static var recoveryOriginal: URL { directory.appendingPathComponent("saved-recovery-original-v3.mp4") }
    static var recoveryThreadID: String {
        // A newly encoded synthetic source must not reuse an immutable project's
        // earlier original binding. Replays of the same bytes retain one thread.
        let hash = (try? SHA256Fingerprinter().fingerprint(file: recoveryOriginal).hex) ?? String(repeating: "a", count: 64)
        return "\(hash.prefix(8))-\(hash.dropFirst(8).prefix(4))-\(hash.dropFirst(12).prefix(4))-\(hash.dropFirst(16).prefix(4))-\(hash.dropFirst(20).prefix(12))"
    }
    static func prepareRecoveryOriginal() async throws -> URL {
        let url = recoveryOriginal
        if FileManager.default.fileExists(atPath: url.path) { return url }
        try FileManager.default.createDirectory(at: directory, withIntermediateDirectories: true)
        let writer = try AVAssetWriter(outputURL: url, fileType: .mp4)
        let input = AVAssetWriterInput(mediaType: .video, outputSettings: [AVVideoCodecKey: AVVideoCodecType.h264, AVVideoWidthKey: 96, AVVideoHeightKey: 160])
        let adaptor = AVAssetWriterInputPixelBufferAdaptor(assetWriterInput: input, sourcePixelBufferAttributes: [kCVPixelBufferPixelFormatTypeKey as String: kCVPixelFormatType_32ARGB, kCVPixelBufferWidthKey as String: 96, kCVPixelBufferHeightKey as String: 160])
        writer.add(input)
        guard writer.startWriting() else { throw APIError.invalidResponse }
        writer.startSession(atSourceTime: .zero)
        for second in [0, 179] {
            while !input.isReadyForMoreMediaData { try await Task.sleep(for: .milliseconds(2)) }
            var buffer: CVPixelBuffer?
            guard let pool = adaptor.pixelBufferPool, CVPixelBufferPoolCreatePixelBuffer(nil, pool, &buffer) == kCVReturnSuccess, let pixel = buffer else { throw APIError.invalidResponse }
            CVPixelBufferLockBaseAddress(pixel, [])
            guard let context = CGContext(data: CVPixelBufferGetBaseAddress(pixel), width: 96, height: 160, bitsPerComponent: 8, bytesPerRow: CVPixelBufferGetBytesPerRow(pixel), space: CGColorSpaceCreateDeviceRGB(), bitmapInfo: CGImageAlphaInfo.noneSkipFirst.rawValue) else { throw APIError.invalidResponse }
            context.setFillColor(CGColor(red: 0.8, green: 0.1, blue: 0.1, alpha: 1)); context.fill(CGRect(x: 0, y: 0, width: 96, height: 160))
            CVPixelBufferUnlockBaseAddress(pixel, [])
            guard adaptor.append(pixel, withPresentationTime: CMTime(value: Int64(second), timescale: 1)) else { throw APIError.invalidResponse }
        }
        writer.endSession(atSourceTime: CMTime(value: 180, timescale: 1))
        input.markAsFinished(); await writer.finishWriting()
        guard writer.status == .completed else { throw APIError.invalidResponse }
        return url
    }
    static var result: [String: Any] {
        input?["result"] as? [String: Any] ?? ["stories": [story], "coverage": ["moments": moments, "incomplete_speech_asset_ids": [], "unindexed_asset_ids": ["CCCCCCCC-CCCC-CCCC-CCCC-CCCCCCCCCCCC"]]]
    }
    static var reviewStories: [[String: Any]] {
        if ProcessInfo.processInfo.arguments.contains("-camera-roll-whole-source-context") {
            var value = story
            var context = moments[0]
            context["start_s"] = 0.0; context["end_s"] = 8.166667
            context["text"] = "Whole analysed clip; no localized highlight. Cached overall description: Synthetic red test source."
            context["evidence_scope"] = "whole_source_context"
            value["moments"] = [context]; value["duration_s"] = 8.166667
            return [value]
        }
        guard ProcessInfo.processInfo.arguments.contains("-camera-roll-multiple-ideas") else { return [story] }
        let options = [
            ("11111111-1111-1111-1111-111111111111", "Boat journey"),
            ("22222222-1111-1111-1111-111111111111", "The light at the end"),
            ("33333333-1111-1111-1111-111111111111", "An evening crossing")
        ]
        return options.map { id, title in
            var value = story
            value["id"] = id; value["title"] = title
            return value
        }
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
        var value: [String: Any] = ["id": "11111111-1111-1111-1111-111111111111", "revision": 1, "title": "Boat journey", "hook": "Start on the water", "payoff": "Finish at the destination", "rationale": "A crossing becomes an arrival.", "duration_s": 13, "moments": Array(moments.prefix(2)), "rejected": false, "audio_policy": "preserve_source_audio"]
        if ProcessInfo.processInfo.arguments.contains("-camera-roll-story-research") {
            value["research"] = ["topic": "Ferry terminal history", "review_current": true, "evidence": ["facts": [["id": "fact-0", "text": "Recorded research: the terminal was rebuilt.", "sources": [["title": "Recorded public archive", "url": "https://si.edu/recorded-fixture"]]]], "retrieved_at": "2026-10-21T00:00:00Z", "search_entry_html": "<div>Recorded attribution</div>"]]
        }
        return value
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
    private var recovered = false
    private var recoveryThread: [String: Any] { ["id": CameraRollStoryFixture.recoveryThreadID, "title": "Saved harbor", "status": "active", "revision": recovered ? 10 : 9, "runtime_version": 2, "active_plan_item_id": "AAAAAAAA-AAAA-AAAA-AAAA-AAAAAAAAAAAA", "updated_at": "2026-10-19T10:00:00Z"] }
    private let lock = NSLock()
    private var story = CameraRollStoryFixture.story
    private var taste: [String: Any] = ["revision": 0, "enabled": false, "memory_paused": false, "rules": [], "examples": []]
    func respond(_ request: URLRequest) -> (Int, Any) {
        lock.lock(); defer { lock.unlock() }
        if request.httpMethod == "GET", request.url?.path.hasSuffix("/stories") == true {
            if ProcessInfo.processInfo.arguments.contains("-collection-saved-old-server") { return (404, ["detail": "Not Found"]) }
            let query = URLComponents(url: request.url!, resolvingAgainstBaseURL: false)?.queryItems ?? []
            guard query.first(where: { $0.name == "expected_revision" })?.value == "1",
                  query.filter({ $0.name == "story_ids" }).compactMap(\.value).map({ $0.lowercased() }) == ["11111111-1111-1111-1111-111111111111"] else { return (409, ["detail": "Saved-card identity changed"]) }
            return (200, ["revision": 1, "stories": [story]])
        }
        let recovery = ProcessInfo.processInfo.arguments.contains("-collection-saved-recovery")
        if recovery, request.httpMethod == "GET", request.url?.path.hasSuffix("/capabilities") == true { return (200, ["formats": [], "runtime_versions": [2], "media_collections": true]) }
        if recovery, request.httpMethod == "GET", request.url?.path.contains("/creation-threads/") == true { return (200, recoveryThread) }
        let recoveryWrite = recovery && (request.url?.path.contains("/creation-threads") == true || request.url?.path.hasSuffix("/select") == true)
        if ProcessInfo.processInfo.arguments.contains("-collection-saved-read-only"), request.httpMethod != "GET", !recoveryWrite {
            NotificationCenter.default.post(name: Notification.Name("CameraRollSavedReadWrite"), object: nil)
            return (500, ["detail": "Write attempted while reading saved idea"])
        }
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
        if request.httpMethod == "GET", request.url?.path.contains("/media-collections/") == true {
            return (200, ["id": request.url!.lastPathComponent, "revision": 2, "name": "Updated outing", "members": [["asset_id": CameraRollStoryFixture.assetID, "source_id": "BBBBBBBB-BBBB-BBBB-BBBB-BBBBBBBBBBBB", "group": "Updated outing"]]])
        }
        if request.httpMethod == "GET", request.url?.path.contains("/media-assets/") == true {
            return (200, CameraRollStoryFixture.asset(CameraRollStoryFixture.assetID, source: "BBBBBBBB-BBBB-BBBB-BBBB-BBBBBBBBBBBB"))
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
        if recoveryWrite {
            if request.url?.path.hasSuffix("/select") == true {
                guard body["client_event_id"] as? String == "choose-11111111-1111-1111-1111-111111111111-1" else { return (409, ["detail": "Selection identity changed"]) }
                let held = "44444444-4444-4444-4444-444444444444"
                if let original = body["recover_held_turn_id"] as? String {
                    guard original == held, body["expected_thread_revision"] as? Int == 9 else { return (409, ["detail": "Hold changed"]) }
                    recovered = true
                    return (200, ["turn_id": "55555555-5555-5555-5555-555555555555", "thread_revision": 10, "status": "pending", "replayed": false])
                }
                return (200, ["turn_id": held, "thread_revision": 9, "status": "completed", "replayed": true, "recoverable_held_turn_id": held])
            }
            return (200, recoveryThread)
        }
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
            if ProcessInfo.processInfo.arguments.contains("-collection-conflict"), body["name"] as? String != "Updated outing" {
                return (409, ["detail": ["code": "collection_changed", "message": "This collection changed. Refresh before continuing."]])
            }
            return (200, ["id": request.url!.lastPathComponent, "revision": body["name"] as? String == "Updated outing" ? 2 : 1])
        }
        if request.url?.path.hasSuffix("/index") == true {
            if ProcessInfo.processInfo.arguments.contains("-collection-unusable-timing") {
                return (200, ["next_offset": NSNull(), "missing_analysis_asset_ids": [], "unusable_timing_asset_ids": ["CCCCCCCC-CCCC-CCCC-CCCC-CCCCCCCCCCCC"], "unavailable_asset_ids": [], "untimed_asset_ids": []])
            }
            if ProcessInfo.processInfo.arguments.contains("-collection-coverage-changed") {
                return (200, ["next_offset": NSNull(), "missing_analysis_asset_ids": [CameraRollStoryFixture.assetID], "unavailable_asset_ids": [], "untimed_asset_ids": []])
            }
            let missing = (CameraRollStoryFixture.result["coverage"] as? [String: Any])?["unindexed_asset_ids"] ?? ["CCCCCCCC-CCCC-CCCC-CCCC-CCCCCCCCCCCC"]
            return (200, ["next_offset": NSNull(), "missing_analysis_asset_ids": missing, "unavailable_asset_ids": [], "untimed_asset_ids": []])
        }
        if request.url?.path.hasSuffix("/stories") == true {
            if ProcessInfo.processInfo.arguments.contains("-camera-roll-collection-ready"),
               (body["allow_partial_analysis"] as? Bool != true || body["query"] as? String != "Suggest different content ideas from this outing") {
                return (409, ["detail": "Missing analysis requires an explicit partial choice"])
            }
            if ProcessInfo.processInfo.arguments.contains("-collection-cache-miss") {
                return (409, ["detail": ["code": "collection_analysis_unavailable", "message": "No eligible cached result; new model calls are disabled"]])
            }
            return (200, CameraRollStoryFixture.result)
        }
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
