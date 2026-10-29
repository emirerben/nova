#if DEBUG
import SwiftUI
import Foundation
import CryptoKit
import KriaMediaEngine

/// KRI-822 modes: a device-only "horse" video whose original lives on this iPhone.
enum SavedUploadsFixtureMode: String {
    case reuse, deviceOnlyVisual = "device-only-visual", deviceOnlyRelink = "device-only-relink", imagesOnly = "images-only", footageBoth = "footage-both"
    static var current: Self { Self(rawValue: ProcessInfo.processInfo.environment["KRIA_SAVED_UPLOADS_MODE"] ?? "") ?? .reuse }
    var isHorse: Bool { self != .reuse }
    static let horseID = "DDDDDDDD-0000-4000-8000-000000000822"
    static let proxyID = "DDDDDDDD-AAAA-4AAA-8AAA-AAAAAAAAAAAA"
    static let originalID = "EEEEEEEE-EEEE-4EEE-8EEE-EEEEEEEEEEEE"
    /// Stand-in for the iPhone original; its hash is the asset's declared original identity.
    static let originalBytes = Data((0..<4096).map { UInt8(($0 * 31 + 822) % 251) })
    static var originalSHA: String { SHA256.hash(data: originalBytes).map { String(format: "%02x", $0) }.joined() }
}

/// Offline network fixture drives the production picker, API encoder and attachment coordinator.
struct SavedUploadsUITestHost: View {
    private static let first = UUID(uuidString: "11111111-1111-1111-1111-111111111111")!
    private static let second = UUID(uuidString: "22222222-2222-2222-2222-222222222222")!
    @State private var projectID = Self.first
    @State private var showing = false
    @State private var photos = false
    @State private var ready = SavedUploadsFixtureMode.current == .reuse
    @State private var summary = SavedUploadsURLProtocol.summary
    @StateObject private var uploads: BackgroundUploadCoordinator
    init() {
        let config = URLSessionConfiguration.ephemeral
        config.protocolClasses = [SavedUploadsURLProtocol.self]
        let api = KriaAPI(baseURL: URL(string: "https://saved-uploads.invalid")!, tokenStore: SavedUploadsTokenStore(), session: URLSession(configuration: config))
        _uploads = StateObject(wrappedValue: BackgroundUploadCoordinator(api: api, defaultsKey: "saved-uploads-ui-fixture", sessionConfiguration: .ephemeral))
    }
    var body: some View {
        VStack(spacing: 20) {
            Text("Post 1: \(count(Self.first)) saved attachment")
            Text("Post 2: \(count(Self.second)) saved attachment")
            Text(mode.isHorse ? summary : "Reuse used no upload requests")
            if ready { Button("Saved uploads") { showing = true }.accessibilityIdentifier("fixture-open-saved") }
            Button("Photo posts") { photos = true }.accessibilityIdentifier("fixture-photo-posts")
            Button("Second post") { projectID = Self.second }.accessibilityIdentifier("fixture-second-post")
        }
        .sheet(isPresented: $showing, onDismiss: { summary = SavedUploadsURLProtocol.summary }) {
            SavedUploadsView(projectID: projectID, itemID: role == .visual ? projectID.uuidString : nil, role: role, destination: destination, capacity: mode == .reuse && !photos ? 2 : 1, uploads: uploads, onAdded: {})
        }
        .task { await prepareHorse() }
    }
    private var mode: SavedUploadsFixtureMode { .current }
    private var role: CreationMediaRole {
        switch mode { case .reuse: photos ? .visual : .song; case .footageBoth: .clip; default: .visual }
    }
    private var destination: ProjectUploadDestination {
        switch mode { case .reuse: .cloud; case .footageBoth: .phone; case .imagesOnly: .phoneVisuals([.image]); default: .phoneVisuals([.image, .video]) }
    }
    /// Start each run from a clean library store; keep the original on this iPhone unless the
    /// journey is about locating it in Files.
    private func prepareHorse() async {
        guard mode.isHorse, !ready, let owner = MediaLibraryOriginals.owner(accessToken: (try? SavedUploadsTokenStore().read())?.accessToken) else { return }
        let library = MediaLibraryOriginals(owner: owner)
        try? FileManager.default.removeItem(at: library.root)
        try? FileManager.default.removeItem(at: library.erasureMarker)
        let file = FileManager.default.temporaryDirectory.appendingPathComponent("Horse-\(UUID().uuidString).mov")
        try? SavedUploadsFixtureMode.originalBytes.write(to: file)
        let fingerprint = AssetFingerprint(hex: SavedUploadsFixtureMode.originalSHA, byteCount: Int64(SavedUploadsFixtureMode.originalBytes.count))
        if mode == .deviceOnlyRelink { SavedUploadsView.debugRelinkFile = file }
        else { try? await library.relink(assetID: UUID(uuidString: SavedUploadsFixtureMode.horseID)!, fingerprint: fingerprint, file: file) }
        ready = true
    }
    private func count(_ id: UUID) -> Int { CreationAttachedMedia.parse(uploads.attachedThreads[id]?.state ?? [:]).count + (uploads.attachedThreads[id]?.visualCount ?? 0) }
}
private struct SavedUploadsTokenStore: TokenStore {
    func read() throws -> MobileSession? {
        let payload = Data("{\"sub\":\"saved-uploads-fixture\"}".utf8).base64EncodedString()
        return MobileSession(accessToken: "fixture.\(payload).fixture", refreshToken: "fixture", expiresIn: 3600)
    }
    func write(_ session: MobileSession) throws {}
    func delete() throws {}
}
private final class SavedUploadsURLProtocol: URLProtocol, @unchecked Sendable {
    private static let state = SavedUploadsServer()
    static var summary: String { state.summary }
    override class func canInit(with request: URLRequest) -> Bool { true }
    override class func canonicalRequest(for request: URLRequest) -> URLRequest { request }
    override func startLoading() {
        let (status, body) = Self.state.respond(request)
        let response = HTTPURLResponse(url: request.url!, statusCode: status, httpVersion: nil, headerFields: ["Content-Type": "application/json"])!
        client?.urlProtocol(self, didReceive: response, cacheStoragePolicy: .notAllowed)
        client?.urlProtocol(self, didLoad: try! JSONSerialization.data(withJSONObject: body))
        client?.urlProtocolDidFinishLoading(self)
    }
    override func stopLoading() {}
}
private final class SavedUploadsServer: @unchecked Sendable {
    private let lock = NSLock()
    private var failed = false
    private var media: [String: [[String: Any]]] = [:]
    private var visuals: [String: [[String: Any]]] = [:]
    private let mode = SavedUploadsFixtureMode.current
    private var upgraded = SavedUploadsFixtureMode.current == .footageBoth
    private var originalPuts = 0
    private var attachedSource = "none"
    var summary: String {
        lock.lock(); defer { lock.unlock() }
        return "Uploaded original \(originalPuts)x, \(mode == .footageBoth ? "Footage" : "Visuals") source \(attachedSource.prefix(8))"
    }
    func respond(_ request: URLRequest) -> (Int, Any) {
        lock.lock(); defer { lock.unlock() }
        let path = request.url!.path
        let parts = path.split(separator: "/").map(String.init)
        if mode.isHorse, let response = horseUpload(request, path: path) { return response }
        if request.httpMethod == "PUT" || path.contains("upload") { return (500, ["detail": "Reuse unexpectedly requested an upload"]) }
        if path == "/me/media-assets" {
            if !failed && ProcessInfo.processInfo.environment["KRIA_SAVED_UPLOADS_FAIL_FIRST"] == "1" { failed = true; return (503, ["detail": "Temporarily unavailable"]) }
            let query = URLComponents(url: request.url!, resolvingAgainstBaseURL: false)?.queryItems ?? []
            let search = query.first { $0.name == "query" }?.value ?? ""
            let next = query.contains { $0.name == "cursor" }
            let image = query.first { $0.name == "kind" }?.value == "image" || request.url?.query?.contains("kind=") == false
            if mode.isHorse { return (200, ["assets": [horse()], "next_cursor": NSNull(), "active_count": 1, "limit": 500]) }
            let asset = asset(second: next, image: image)
            let matches = search.isEmpty || (asset["metadata_json"] as! [String: String])["filename"]!.localizedCaseInsensitiveContains(search)
            return (200, ["assets": matches ? [asset] : [], "next_cursor": next || !search.isEmpty ? NSNull() : "page-two", "active_count": 2, "limit": 500])
        }
        if parts.count >= 2, parts[0] == "plan-items" {
            let id = parts[1]
            if request.httpMethod == "POST" {
                let body = (try? JSONSerialization.jsonObject(with: readBody(request))) as? [String: Any] ?? [:]
                // Visuals bind only a full original: the proxy is refused like the real server's 422.
                let wanted = mode.isHorse ? SavedUploadsFixtureMode.originalID : "BBBBBBBB-BBBB-BBBB-BBBB-BBBBBBBBBBBB"
                guard let libraryID = body["library_asset_id"] as? String, body["library_source_id"] as? String == wanted, !mode.isHorse || upgraded else { return (422, ["detail": "Invalid visual library binding"]) }
                if let existing = visuals[id]?.first(where: { $0["library_asset_id"] as? String == libraryID }) { return (200, existing) }
                attachedSource = wanted
                let visual: [String: Any] = ["id": UUID().uuidString, "library_asset_id": libraryID, "kind": mode.isHorse ? "video" : "image", "status": "ready", "source_filename": mode.isHorse ? "Horse.mov" : "Morning photo.jpg"]
                visuals[id, default: []].append(visual)
                return (200, visual)
            }
            return (200, ["assets": visuals[id] ?? [], "max_assets": 10])
        }
        guard parts.count >= 2 && parts[0] == "creation-threads" else { return (404, ["detail": "Unexpected fixture request"]) }
        let id = parts[1]
        if request.httpMethod == "POST", parts.last == "media" {
            let body = (try? JSONSerialization.jsonObject(with: readBody(request))) as? [String: Any] ?? [:]
            let incoming = (body["media"] as? [[String: Any]]) ?? []
            let valid: Bool
            if mode == .footageBoth {
                // A device-only phone thread accepts only the analysis proxy (creation_threads.py).
                valid = incoming.first?["library_asset_id"] as? String == SavedUploadsFixtureMode.horseID && incoming.first?["library_source_id"] as? String == SavedUploadsFixtureMode.proxyID && (incoming.first?["media_id"] as? String)?.hasPrefix("analysis-proxy-") == true && incoming.first?["role"] == nil
                if valid { attachedSource = SavedUploadsFixtureMode.proxyID }
            } else {
                valid = ["AAAAAAAA-AAAA-AAAA-AAAA-AAAAAAAAAAAA", "CCCCCCCC-CCCC-CCCC-CCCC-CCCCCCCCCCCC"].contains(incoming.first?["library_asset_id"] as? String ?? "") && incoming.first?["library_source_id"] as? String == "BBBBBBBB-BBBB-BBBB-BBBB-BBBBBBBBBBBB" && incoming.first?["role"] as? String == "song"
            }
            guard valid, let item = incoming.first else { return (422, ["detail": "Invalid library attachment contract"]) }
            var existing = media[id] ?? []
            if !existing.contains(where: { $0["media_id"] as? String == item["media_id"] as? String }) { existing.append(item) }
            media[id] = existing
        }
        return (200, ["id": id, "title": "Saved upload fixture", "status": "draft", "revision": (media[id]?.count ?? 0) + 1, "runtime_version": 1, "state": ["media": media[id] ?? []], "media_capabilities": ["visuals": ["current": visuals[id]?.count ?? 0]], "events": [], "updated_at": "2026-10-16T10:00:00Z"])
    }
    /// Mirrors the KRI-822 server contract: attach_original + the declared original identity.
    private func horseUpload(_ request: URLRequest, path: String) -> (Int, Any)? {
        if request.httpMethod == "PUT", path == "/put/horse-original" { originalPuts += 1; return (200, [:] as [String: Any]) }
        if path == "/me/media-assets/uploads", request.httpMethod == "POST" {
            let body = (try? JSONSerialization.jsonObject(with: readBody(request))) as? [String: Any] ?? [:]
            let contract = body["upload_contract"] as? [String: Any] ?? [:]
            guard body["attach_original"] as? Bool == true, body["repair_asset_id"] as? String == SavedUploadsFixtureMode.horseID,
                  body["kind"] as? String == "video", body["content_type"] as? String == "video/quicktime",
                  (body["file_size_bytes"] as? NSNumber)?.intValue == SavedUploadsFixtureMode.originalBytes.count,
                  contract["purpose"] as? String == "cloud_render_source", contract["proxy"] == nil || contract["proxy"] is NSNull,
                  (body["client_upload_id"] as? String)?.hasPrefix("ios-original-\(SavedUploadsFixtureMode.horseID)") == true
            else { return (422, ["detail": "Original upgrade contract mismatch"]) }
            return (200, ["reservation_id": "FFFFFFFF-FFFF-4FFF-8FFF-FFFFFFFFFFFF", "object_path": "users/fixture/media-library/source-horse.mov", "upload_url": upgraded ? NSNull() : "https://saved-uploads.invalid/put/horse-original", "upload_headers": [:] as [String: String], "expires_at": "2026-10-22T10:00:00Z", "asset_id": upgraded ? SavedUploadsFixtureMode.horseID : NSNull()] as [String: Any])
        }
        if path.hasSuffix("/finalize") {
            guard originalPuts > 0 else { return (409, ["detail": "Upload is not available"]) }
            upgraded = true
            return (200, horse())
        }
        if path == "/me/media-assets/\(SavedUploadsFixtureMode.horseID)" { return (200, horse()) }
        return nil
    }
    private func horse() -> [String: Any] {
        var sources: [[String: Any]] = [["id": SavedUploadsFixtureMode.proxyID, "role": "analysis_proxy", "status": "available", "byte_count": 12, "content_type": "video/mp4", "uploaded_sha256": String(repeating: "e", count: 64), "generation": "10"]]
        if upgraded { sources.append(["id": SavedUploadsFixtureMode.originalID, "role": "original", "status": "available", "byte_count": SavedUploadsFixtureMode.originalBytes.count, "content_type": "video/quicktime", "uploaded_sha256": SavedUploadsFixtureMode.originalSHA, "generation": "77"]) }
        return ["id": SavedUploadsFixtureMode.horseID, "kind": "video", "status": "ready", "original_declared_sha256": SavedUploadsFixtureMode.originalSHA, "original_byte_count": SavedUploadsFixtureMode.originalBytes.count, "original_availability": "device_only", "metadata_json": ["filename": "Horse.mov"], "usage": [], "storage_warnings": [], "sources": sources]
    }
    private func asset(second: Bool, image: Bool) -> [String: Any] {
        ["id": second ? "CCCCCCCC-CCCC-CCCC-CCCC-CCCCCCCCCCCC" : "AAAAAAAA-AAAA-AAAA-AAAA-AAAAAAAAAAAA", "kind": image ? "image" : "audio", "status": "ready", "original_declared_sha256": NSNull(), "original_byte_count": NSNull(), "original_availability": "cloud_backed", "metadata_json": ["filename": image ? "Morning photo.jpg" : second ? "Evening song.mp3" : "Morning song.mp3"], "usage": [], "storage_warnings": ProcessInfo.processInfo.environment["KRIA_SAVED_UPLOADS_WARNING"] == "1" ? [["source_id": "BBBBBBBB-BBBB-BBBB-BBBB-BBBBBBBBBBBB", "eligible_at": "2026-11-03T10:00:00Z"]] : [], "sources": [["id": "BBBBBBBB-BBBB-BBBB-BBBB-BBBBBBBBBBBB", "role": "original", "status": "available", "byte_count": 10, "content_type": image ? "image/jpeg" : "audio/mpeg", "uploaded_sha256": String(repeating: "a", count: 64), "generation": "1"]]]
    }
    private func readBody(_ request: URLRequest) -> Data {
        if let data = request.httpBody { return data }
        guard let stream = request.httpBodyStream else { return Data() }
        stream.open(); defer { stream.close() }
        var data = Data(); var buffer = [UInt8](repeating: 0, count: 4096)
        while stream.hasBytesAvailable { let count = stream.read(&buffer, maxLength: buffer.count); if count <= 0 { break }; data.append(buffer, count: count) }
        return data
    }
}
#endif
