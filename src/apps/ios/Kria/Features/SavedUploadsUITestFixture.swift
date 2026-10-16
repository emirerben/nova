#if DEBUG
import SwiftUI
import Foundation

/// Offline network fixture drives the production picker, API encoder and attachment coordinator.
struct SavedUploadsUITestHost: View {
    private static let first = UUID(uuidString: "11111111-1111-1111-1111-111111111111")!
    private static let second = UUID(uuidString: "22222222-2222-2222-2222-222222222222")!
    @State private var projectID = Self.first
    @State private var showing = false
    @State private var photos = false
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
            Text("Reuse used no upload requests")
            Button("Saved uploads") { showing = true }.accessibilityIdentifier("fixture-open-saved")
            Button("Photo posts") { photos = true }.accessibilityIdentifier("fixture-photo-posts")
            Button("Second post") { projectID = Self.second }.accessibilityIdentifier("fixture-second-post")
        }
        .sheet(isPresented: $showing) {
            SavedUploadsView(projectID: projectID, itemID: photos ? projectID.uuidString : nil, role: photos ? .visual : .song, destination: .cloud, capacity: 1, uploads: uploads, onAdded: {})
        }
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
    func respond(_ request: URLRequest) -> (Int, Any) {
        lock.lock(); defer { lock.unlock() }
        let path = request.url!.path
        let parts = path.split(separator: "/").map(String.init)
        if request.httpMethod == "PUT" || path.contains("upload") { return (500, ["detail": "Reuse unexpectedly requested an upload"]) }
        if path == "/me/media-assets" {
            if !failed && ProcessInfo.processInfo.environment["KRIA_SAVED_UPLOADS_FAIL_FIRST"] == "1" { failed = true; return (503, ["detail": "Temporarily unavailable"]) }
            let query = URLComponents(url: request.url!, resolvingAgainstBaseURL: false)?.queryItems ?? []
            let search = query.first { $0.name == "query" }?.value ?? ""
            let next = query.contains { $0.name == "cursor" }
            let image = query.first { $0.name == "kind" }?.value == "image" || request.url?.query?.contains("kind=") == false
            let asset = asset(second: next, image: image)
            let matches = search.isEmpty || (asset["metadata_json"] as! [String: String])["filename"]!.localizedCaseInsensitiveContains(search)
            return (200, ["assets": matches ? [asset] : [], "next_cursor": next || !search.isEmpty ? NSNull() : "page-two", "active_count": 2, "limit": 500])
        }
        if parts.count >= 2, parts[0] == "plan-items" {
            let id = parts[1]
            if request.httpMethod == "POST" {
                let body = (try? JSONSerialization.jsonObject(with: readBody(request))) as? [String: Any] ?? [:]
                guard let libraryID = body["library_asset_id"] as? String, body["library_source_id"] as? String == "BBBBBBBB-BBBB-BBBB-BBBB-BBBBBBBBBBBB" else { return (422, ["detail": "Invalid visual library binding"]) }
                if let existing = visuals[id]?.first(where: { $0["library_asset_id"] as? String == libraryID }) { return (200, existing) }
                let visual: [String: Any] = ["id": UUID().uuidString, "library_asset_id": libraryID, "kind": "image", "status": "ready", "source_filename": "Morning photo.jpg"]
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
            guard let item = incoming.first, item["library_asset_id"] as? String == "AAAAAAAA-AAAA-AAAA-AAAA-AAAAAAAAAAAA", item["library_source_id"] as? String == "BBBBBBBB-BBBB-BBBB-BBBB-BBBBBBBBBBBB", item["role"] as? String == "song" else { return (422, ["detail": "Invalid library attachment contract"]) }
            var existing = media[id] ?? []
            if !existing.contains(where: { $0["media_id"] as? String == item["media_id"] as? String }) { existing.append(item) }
            media[id] = existing
        }
        return (200, ["id": id, "title": "Saved upload fixture", "status": "draft", "revision": (media[id]?.count ?? 0) + 1, "runtime_version": 1, "state": ["media": media[id] ?? []], "media_capabilities": ["visuals": ["current": visuals[id]?.count ?? 0]], "events": [], "updated_at": "2026-10-16T10:00:00Z"])
    }
    private func asset(second: Bool, image: Bool) -> [String: Any] {
        ["id": second ? "CCCCCCCC-CCCC-CCCC-CCCC-CCCCCCCCCCCC" : "AAAAAAAA-AAAA-AAAA-AAAA-AAAAAAAAAAAA", "kind": image ? "image" : "audio", "status": "ready", "original_declared_sha256": NSNull(), "original_byte_count": NSNull(), "original_availability": "cloud_backed", "metadata_json": ["filename": image ? "Morning photo.jpg" : second ? "Evening song.mp3" : "Morning song.mp3"], "usage": [], "storage_warnings": [], "sources": [["id": "BBBBBBBB-BBBB-BBBB-BBBB-BBBBBBBBBBBB", "role": "original", "status": "available", "byte_count": 10, "content_type": image ? "image/jpeg" : "audio/mpeg", "uploaded_sha256": String(repeating: "a", count: 64), "generation": "1"]]]
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
