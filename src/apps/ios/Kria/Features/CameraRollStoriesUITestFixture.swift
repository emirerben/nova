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
private enum CameraRollStoryFixture {
    static let assetID = "AAAAAAAA-AAAA-AAAA-AAAA-AAAAAAAAAAAA"
    static var moments: [[String: Any]] { [
        moment("11111111-2222-2222-2222-222222222222", text: "An illuminated house", start: 150, end: 157),
        moment("22222222-2222-2222-2222-222222222222", text: "A ferry crossing", start: 160, end: 166),
        moment("33333333-3333-3333-3333-333333333333", text: "The final arrival", start: 170, end: 174),
    ] }
    static func moment(_ id: String, text: String, start: Double, end: Double) -> [String: Any] {
        ["id": id, "asset_id": assetID, "source_id": "BBBBBBBB-BBBB-BBBB-BBBB-BBBBBBBBBBBB", "generation": "42", "start_s": start, "end_s": end, "kind": "visual", "text": text, "uncertain": true]
    }
    static var story: [String: Any] {
        ["id": "11111111-1111-1111-1111-111111111111", "revision": 1, "title": "Boat journey", "hook": "Start on the water", "payoff": "Finish at the destination", "rationale": "A crossing becomes an arrival.", "duration_s": 13, "moments": Array(moments.prefix(2)), "rejected": false, "audio_policy": "preserve_source_audio"]
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
    func respond(_ request: URLRequest) -> (Int, [String: Any]) {
        lock.lock(); defer { lock.unlock() }
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
