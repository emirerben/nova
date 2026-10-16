#if DEBUG
import Foundation
import SwiftData
import SwiftUI

/// Knobs that make the offline fixtures behave like a slow, large, real account, so launch, screen-open and
/// scroll work can be measured without a backend. Only `KriaPerfUITests` and `scripts/ios/bench.sh` set them.
///
/// | Environment variable             | Effect                                                                         |
/// |----------------------------------|--------------------------------------------------------------------------------|
/// | `KRIA_FIXTURE_LATENCY_MS`        | every fixture response arrives this late; requests overlap like on a network    |
/// | `KRIA_FIXTURE_FULL_PROJECTION_MS`| extra delay on `GET /creation-threads/<id>?projection=full` (server lock/repair)|
/// | `KRIA_CHAT_SEED_THREADS`         | the account already has this many chats (newest first, paged by `limit/cursor`) |
/// | `KRIA_CHAT_LONG_HISTORY`         | `1` = 24 messages (legacy), any other integer = that many, in every chat        |
/// | `KRIA_FIXTURE_EXPIRED_TOKEN`     | the stored session is expired: first request 401s until `auth/mobile/refresh`   |
/// | `KRIA_GALLERY_ITEMS`             | the library holds this many finished videos, paged 60 at a time                  |
/// | `KRIA_FIXTURE_UPLOAD_TICKS`      | `1` = an upload reports progress ten times a second                             |
/// | `KRIA_PERF_DISK_STORE`           | `1` = SwiftData/transcript cache live on disk so a second launch starts warm    |
/// | `KRIA_PERF_COUNTERS`             | `1` = show the invisible `perf-counters` element (request/rebuild/fetch counts)  |
///
/// `-perf-reset` wipes the disk store on launch.
enum PerfFixture {
    private static let env = ProcessInfo.processInfo.environment

    static var latencyMS: Int { Int(env["KRIA_FIXTURE_LATENCY_MS"] ?? "") ?? 0 }
    static var fullProjectionMS: Int { Int(env["KRIA_FIXTURE_FULL_PROJECTION_MS"] ?? "") ?? 0 }
    static var seedThreadCount: Int { Int(env["KRIA_CHAT_SEED_THREADS"] ?? "") ?? 0 }
    static var galleryItemCount: Int { Int(env["KRIA_GALLERY_ITEMS"] ?? "") ?? 0 }
    static var expiredToken: Bool { env["KRIA_FIXTURE_EXPIRED_TOKEN"] == "1" }
    static var uploadTicks: Bool { env["KRIA_FIXTURE_UPLOAD_TICKS"] == "1" }
    static var diskStore: Bool { env["KRIA_PERF_DISK_STORE"] == "1" }
    /// `KRIA_PERF_COUNTERS=1` mounts the invisible `perf-counters` readout over the chat fixture.
    static var countersProbe: Bool { env["KRIA_PERF_COUNTERS"] == "1" }
    static var resetRequested: Bool { ProcessInfo.processInfo.arguments.contains("-perf-reset") }

    /// `KRIA_CHAT_LONG_HISTORY`: `1` keeps the 24 messages drawer tests rely on; any other integer is a message count.
    static var longHistoryCount: Int? {
        guard let raw = env["KRIA_CHAT_LONG_HISTORY"], let value = Int(raw), value > 0 else { return nil }
        return value == 1 ? 24 : value
    }

    // MARK: Latency

    /// How long a fixture response is held back: the base latency, plus the full-projection cost on that one route.
    static func delay(for request: URLRequest) -> TimeInterval {
        var milliseconds = latencyMS
        if fullProjectionMS > 0, isFullProjectionRead(request) { milliseconds += fullProjectionMS }
        return TimeInterval(milliseconds) / 1000
    }

    private static func isFullProjectionRead(_ request: URLRequest) -> Bool {
        guard (request.httpMethod ?? "GET") == "GET", let url = request.url else { return false }
        let parts = url.path.split(separator: "/")
        guard parts.count == 2, parts[0] == "creation-threads", UUID(uuidString: String(parts[1])) != nil else { return false }
        return URLComponents(url: url, resolvingAgainstBaseURL: false)?.queryItems?
            .contains { $0.name == "projection" && $0.value == "full" } ?? false
    }

    // MARK: Responses owned by the perf fixture

    struct Response {
        let status: Int
        let data: Data
        var headers: [String: String] = [:]
    }

    /// The perf fixture answers these routes itself; everything else falls through to `CreationChatFixture`.
    static func respond(_ request: URLRequest) -> Response? {
        guard let url = request.url else { return nil }
        let path = url.path
        let method = request.httpMethod ?? "GET"
        let query = Dictionary(
            (URLComponents(url: url, resolvingAgainstBaseURL: false)?.queryItems ?? []).map { ($0.name, $0.value ?? "") },
            uniquingKeysWith: { first, _ in first }
        )

        if expiredToken {
            if path == "/auth/mobile/refresh", method == "POST" { return PerfTokenState.shared.rotate() }
            if path == "/auth/mobile/me" {
                guard PerfTokenState.shared.accepts(request) else { return unauthorized }
                return json([
                    "id": "perf-user", "email": "perf@kria.test", "name": "Perf creator",
                    "onboarding_status": "complete", "linked_providers": ["google"],
                ])
            }
            if !PerfTokenState.shared.accepts(request) { return unauthorized }
        }
        if method == "GET", path == "/creation-threads", seedThreadCount > 0 {
            return seededThreadList(query: query)
        }
        if method == "GET", path == "/me/jobs", galleryItemCount > 0 {
            return galleryPage(query: query)
        }
        return nil
    }

    private static var unauthorized: Response {
        Response(status: 401, data: Data(#"{"detail":"Not authenticated"}"#.utf8))
    }

    private static func json(_ object: Any, status: Int = 200, headers: [String: String] = [:]) -> Response {
        Response(status: status, data: (try? JSONSerialization.data(withJSONObject: object)) ?? Data(), headers: headers)
    }

    // MARK: Seeded chats

    static func seededThreadID(_ index: Int) -> String {
        String(format: "5EED0000-0000-4000-8000-%012d", index)
    }

    private static func timestamp(hoursAgo: Int) -> String {
        let base = Date(timeIntervalSince1970: 1_791_000_000) // 2026-10-03; fixed so lists are stable across runs
        return ISO8601DateFormatter().string(from: base.addingTimeInterval(-Double(hoursAgo) * 3600))
    }

    /// Chat `index` of the seeded account (0 is the newest). Every chat carries the long-history transcript.
    static func seededThread(_ index: Int, runtime: Int) -> [String: Any] {
        let id = seededThreadID(index)
        let count = longHistoryCount ?? 12
        let events: [[String: Any]] = (0..<count).map { sequence in
            let isUser = sequence.isMultiple(of: 2)
            return [
                "id": UUID().uuidString, "sequence": sequence, "revision": sequence + 1,
                "role": isUser ? "user" : "assistant",
                "event_type": isUser ? "user_message" : "assistant_response",
                "content": isUser
                    ? "Chat \(index + 1), message \(sequence + 1): keep the laughter at the table and open on the arrival."
                    : "Got it. For chat \(index + 1) I'll open on the arrival, keep the candid reactions, and end on the wide shot. **Anything else?**",
                "payload": [String: Any](), "created_at": timestamp(hoursAgo: index),
            ]
        }
        return [
            "id": id, "title": "Chat \(index + 1)", "status": "active", "revision": count, "runtime_version": runtime,
            "state": ["format": "montage", "edit_format": "montage"], "events": events,
            "active_plan_item_id": id, "updated_at": timestamp(hoursAgo: index),
        ]
    }

    static func seedThreads(runtime: Int, into threads: inout [String: [String: Any]]) {
        for index in 0..<seedThreadCount { threads[seededThreadID(index)] = seededThread(index, runtime: runtime) }
    }

    /// `GET /creation-threads`: newest first, `limit` (default 20, max 50) and an opaque `cursor`. The next page's
    /// cursor goes in `X-Next-Cursor`; today's client sends neither, which is exactly how older chats get cut off.
    private static func seededThreadList(query: [String: String]) -> Response {
        let limit = min(max(Int(query["limit"] ?? "") ?? 20, 1), 50)
        let offset = query["cursor"].flatMap { Int($0.dropFirst()) } ?? 0
        let runtime = env["KRIA_CHAT_CREATION_FLOW"] == "v2" ? 2 : 1
        let end = min(offset + limit, seedThreadCount)
        let rows: [[String: Any]] = (offset..<max(offset, end)).map { index in
            var row = seededThread(index, runtime: runtime)
            row["events"] = [] // list rows carry state but not the transcript
            return row
        }
        var headers: [String: String] = [:]
        if end < seedThreadCount { headers["X-Next-Cursor"] = "o\(end)" }
        return json(rows, headers: headers)
    }

    // MARK: Gallery

    static func galleryJobID(_ index: Int) -> String {
        String(format: "6A11E000-0000-4000-8000-%012d", index)
    }

    /// `GET /me/jobs`: `limit` (max 60) and a keyset-style `cursor`. Every response signs poster URLs afresh, so a
    /// poster seen on one fetch has a different URL (same object) on the next, like the real server's V4 signatures.
    private static func galleryPage(query: [String: String]) -> Response {
        let limit = min(max(Int(query["limit"] ?? "") ?? 60, 1), 60)
        let offset = query["cursor"].flatMap { Int($0.dropFirst()) } ?? 0
        let end = min(offset + limit, galleryItemCount)
        let jobs: [[String: Any]] = (offset..<max(offset, end)).map { index in
            let id = galleryJobID(index)
            return [
                "id": id, "title": "Cut \(index + 1)", "status": "ready", "output_variant_id": "montage",
                "poster_url": "https://\(PerfPosterURLProtocol.host)/\(id).jpg?sig=\(UUID().uuidString)",
                "poster_identity": "\(id)-v1", "poster_status": "ready",
                "output_url": "https://\(PerfPosterURLProtocol.host)/\(id).mp4?sig=\(UUID().uuidString)",
                "content_plan_item_id": id, "created_at": timestamp(hoursAgo: index),
            ]
        }
        let nextCursor: Any = end < galleryItemCount ? "o\(end)" : NSNull()
        return json(["jobs": jobs, "next_cursor": nextCursor])
    }
}

/// Holds the expired session `ChatUITestTokenStore` hands out under `KRIA_FIXTURE_EXPIRED_TOKEN`, and issues the
/// fresh one when the client refreshes. Requests carrying the stale token are refused with 401.
final class PerfTokenState: @unchecked Sendable {
    static let shared = PerfTokenState()
    private let lock = NSLock()
    private var session: MobileSession
    private var rotated = false

    private init() {
        session = MobileSession(accessToken: Self.jwt(expiresIn: -3600, tag: "stale"), refreshToken: "perf-refresh-1", expiresIn: 3600)
    }

    var current: MobileSession {
        lock.lock(); defer { lock.unlock() }
        return session
    }

    func store(_ new: MobileSession) {
        lock.lock(); defer { lock.unlock() }
        session = new
    }

    func accepts(_ request: URLRequest) -> Bool {
        lock.lock(); defer { lock.unlock() }
        guard let header = request.value(forHTTPHeaderField: "Authorization") else { return true } // unauthenticated route
        return rotated && header == "Bearer \(session.accessToken)"
    }

    func rotate() -> PerfFixture.Response {
        lock.lock(); defer { lock.unlock() }
        rotated = true
        session = MobileSession(accessToken: Self.jwt(expiresIn: 3600, tag: "fresh"), refreshToken: "perf-refresh-2", expiresIn: 3600)
        PerfCounters.shared.increment("auth refresh")
        let body: [String: Any] = ["access_token": session.accessToken, "refresh_token": session.refreshToken, "expires_in": 3600]
        return PerfFixture.Response(status: 200, data: (try? JSONSerialization.data(withJSONObject: body)) ?? Data())
    }

    private static func jwt(expiresIn: Int, tag: String) -> String {
        func encode(_ object: [String: Any]) -> String {
            ((try? JSONSerialization.data(withJSONObject: object)) ?? Data()).base64EncodedString()
                .replacingOccurrences(of: "+", with: "-").replacingOccurrences(of: "/", with: "_")
                .replacingOccurrences(of: "=", with: "")
        }
        let exp = Int(Date().timeIntervalSince1970) + expiresIn
        return [encode(["alg": "none", "typ": "JWT"]), encode(["sub": "perf-user", "exp": exp, "tag": tag]), "fixture"].joined(separator: ".")
    }
}

/// Serves the gallery's poster and video URLs (`https://fixture-posters.invalid/<id>.jpg?sig=...`) from a bundled
/// image, with the fixture latency, and counts every one that reaches "the network". Registered process-wide so
/// `AsyncImage` and `URLSession.shared` are intercepted, not only the fixture API session.
final class PerfPosterURLProtocol: URLProtocol, @unchecked Sendable {
    static let host = "fixture-posters.invalid"
    private static let posters = ["montage", "voiceover", "broll", "talking", "trulli-street", "lisbon"]
    private var pending: DispatchWorkItem?

    static func registerIfNeeded() {
        guard PerfFixture.galleryItemCount > 0 else { return }
        URLProtocol.registerClass(PerfPosterURLProtocol.self)
    }

    override class func canInit(with request: URLRequest) -> Bool { request.url?.host == host }
    override class func canonicalRequest(for request: URLRequest) -> URLRequest { request }

    override func startLoading() {
        PerfCounters.shared.increment("poster.network")
        let work = DispatchWorkItem { [weak self] in self?.respond() }
        pending = work
        DispatchQueue.global().asyncAfter(deadline: .now() + PerfFixture.delay(for: request), execute: work)
    }

    private func respond() {
        guard let url = request.url else { return }
        let name = Self.posters[abs(url.lastPathComponent.hashValue) % Self.posters.count]
        guard let file = Bundle.main.url(forResource: name, withExtension: "jpg"), let data = try? Data(contentsOf: file),
              let response = HTTPURLResponse(url: url, statusCode: 200, httpVersion: nil, headerFields: ["Content-Type": "image/jpeg"]) else {
            client?.urlProtocol(self, didFailWithError: URLError(.fileDoesNotExist))
            return
        }
        client?.urlProtocol(self, didReceive: response, cacheStoragePolicy: .allowed)
        client?.urlProtocol(self, didLoad: data)
        client?.urlProtocolDidFinishLoading(self)
    }

    override func stopLoading() { pending?.cancel() }
}

extension PerfFixture {
    /// SwiftData container for `-ui-testing-chat`. In memory by default; `KRIA_PERF_DISK_STORE=1` keeps it on disk so
    /// a second launch finds the chats the first one cached.
    static func container() -> ModelContainer {
        let models: [any PersistentModel.Type] = [CachedProject.self, CachedAsset.self, CachedUploadJob.self, CachedReceipt.self]
        let schema = Schema(models)
        guard diskStore else {
            return try! ModelContainer(for: schema, configurations: ModelConfiguration(isStoredInMemoryOnly: true))
        }
        let directory = URL.applicationSupportDirectory.appending(path: "perf", directoryHint: .isDirectory)
        if resetRequested { try? FileManager.default.removeItem(at: directory) }
        try? FileManager.default.createDirectory(at: directory, withIntermediateDirectories: true)
        return try! ModelContainer(for: schema, configurations: ModelConfiguration(schema: schema, url: directory.appending(path: "Kria.store")))
    }
}

/// Test-only readout of `PerfCounters`: an invisible element whose label is `key=count` lines, so a UI test can
/// assert "poster.network=0 after reopening the gallery" or "timeline.build is at most N".
struct PerfCountersProbe: View {
    @State private var summary = PerfCounters.shared.summary

    var body: some View {
        Text(summary)
            .font(.system(size: 1))
            .opacity(0.01)
            .frame(width: 1, height: 1)
            .allowsHitTesting(false)
            .accessibilityIdentifier("perf-counters")
            .accessibilityLabel(summary)
            .task {
                while !Task.isCancelled {
                    summary = PerfCounters.shared.summary
                    try? await Task.sleep(for: .milliseconds(250))
                }
            }
    }
}
#endif
