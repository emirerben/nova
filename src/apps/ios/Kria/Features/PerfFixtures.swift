#if DEBUG
import Foundation
import KriaMediaEngine
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
/// | `KRIA_EDITOR_FIXTURE`            | `1` = seeded chats have a finished render the editor can open: job status, timeline, three source clips |
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
    /// `KRIA_EDITOR_FIXTURE=1`: seeded chats are ready, and the editor's own requests are answered (see `editorResponse`).
    static var editorFixture: Bool { env["KRIA_EDITOR_FIXTURE"] == "1" }
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

    static func isFullProjectionRead(_ request: URLRequest) -> Bool {
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
        if editorFixture, method == "GET", let response = editorResponse(path: path) { return response }
        if method == "GET", path == "/creation-threads", seedThreadCount > 0 {
            return seededThreadList(query: query)
        }
        if method == "GET", path == "/me/jobs", galleryItemCount > 0 {
            return galleryPage(query: query)
        }
        return nil
    }

    // MARK: Editor

    /// The finished render the editor plays first: a bundled clip, as a `file://` URL (AVPlayer's network stack is not
    /// interceptable, so an https URL would fail and trigger retries that are not what is being measured).
    static var editorRenderURL: String {
        Bundle.main.url(forResource: "montage", withExtension: "mp4")?.absoluteString ?? "file:///missing.mp4"
    }
    /// Three source clips per project: the bundled montage, the bundled Istanbul clip, and the montage again.
    static let editorSourceFiles = ["montage", "istanbul", "montage"]

    /// The routes the editor's load path calls beyond the chat's own: job status (the variant and its timeline),
    /// the source pool, and no saved draft.
    private static func editorResponse(path: String) -> Response? {
        let parts = path.split(separator: "/").map(String.init)
        if parts.count == 3, parts[0] == "generative-jobs", parts[2] == "status" {
            let slots: [[String: Any]] = [(4.6, 0), (6.0, 1), (4.6, 2)].map { duration, index in
                ["slot_id": "s\(index)", "clip_index": index, "in_s": 0, "duration_s": 2, "source_duration_s": duration, "removed": false]
            }
            return json(["variants": [[
                "variant_id": "montage", "render_generation_id": "gen-1", "render_status": "ready", "duration_s": 6.0,
                "output_url": editorRenderURL, "resolved_archetype": "montage",
                "editor_capabilities": ["timeline": true, "text_elements": true, "mix": false],
                "user_timeline": ["slots": slots],
            ]]])
        }
        if parts.count == 5, parts[0] == "generative-jobs", parts[2] == "variants", parts[4] == "timeline" {
            let clips: [[String: Any]] = editorSourceFiles.indices.map { index in
                ["clip_index": index, "native_source": [
                    "media_id": "clip-\(index)", "source_url": "https://\(PerfSourceURLProtocol.host)/clip\(index).mp4",
                    "original": NSNull(), "local_required": false,
                ] as [String: Any]]
            }
            return json(["base_generation": "gen-1", "clips": clips, "slots": [], "native_assets": []])
        }
        if parts.count == 3, parts[0] == "creation-threads", parts[2] == "draft" {
            return json(["detail": "No draft"], status: 404)
        }
        return nil
    }

    /// Counts every file the media engine hashes, wherever it does it (the editor hashes whole video files, and how
    /// many bytes is what the benchmark reports; the bundled clips are far too small to show it in time).
    static func installHashObserver() {
        SHA256Fingerprinter.observer = { byteCount in
            PerfCounters.shared.increment("sha256.files")
            PerfCounters.shared.increment("sha256.bytes", by: Int(byteCount))
        }
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
    static func seededThread(_ index: Int, runtime: Int, includeEvents: Bool = true) -> [String: Any] {
        let id = seededThreadID(index)
        let count = longHistoryCount ?? 12
        let events: [[String: Any]] = !includeEvents ? [] : (0..<count).map { sequence in
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
        var thread: [String: Any] = [
            "id": id, "title": "Chat \(index + 1)", "status": "active", "revision": count, "runtime_version": runtime,
            "state": ["format": "montage", "edit_format": "montage"], "events": events,
            "active_plan_item_id": id, "updated_at": timestamp(hoursAgo: index),
        ]
        if editorFixture {
            // A finished render, so the chat offers "Open editor". The job id is the thread id.
            thread["active_job_id"] = id
            thread["job"] = ["id": id, "status": "ready", "variants": [
                ["variant_id": "montage", "render_status": "ready", "output_url": editorRenderURL],
            ]]
        }
        return thread
    }

    /// The seeded chat a thread id names, if any. Chats are built when first asked for, not up front: building 60 chats
    /// of 200 events on the first request would put hundreds of milliseconds of fixture CPU in front of every
    /// response, and the benchmark would measure the fixture instead of the app.
    static func seededIndex(of id: String) -> Int? {
        guard seedThreadCount > 0, id.uppercased().hasPrefix("5EED0000-0000-4000-8000-"),
              let index = Int(id.suffix(12)), index < seedThreadCount else { return nil }
        return index
    }

    /// `GET /creation-threads`: newest first, `limit` (default 20, max 50) and an opaque `cursor`. The next page's
    /// cursor goes in `X-Next-Cursor`; today's client sends neither, which is exactly how older chats get cut off.
    private static func seededThreadList(query: [String: String]) -> Response {
        let limit = min(max(Int(query["limit"] ?? "") ?? 20, 1), 50)
        let offset = query["cursor"].flatMap { Int($0.dropFirst()) } ?? 0
        let runtime = env["KRIA_CHAT_CREATION_FLOW"] == "v2" ? 2 : 1
        let end = min(offset + limit, seedThreadCount)
        let rows: [[String: Any]] = (offset..<max(offset, end)).map { index in
            seededThread(index, runtime: runtime, includeEvents: false) // list rows carry state, not the transcript
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
    private var session: MobileSession?
    private var rotated = false

    private init() {
        session = MobileSession(accessToken: Self.jwt(expiresIn: -3600, tag: "stale"), refreshToken: "perf-refresh-1", expiresIn: 3600)
    }

    var current: MobileSession? {
        lock.lock(); defer { lock.unlock() }
        return session
    }

    func store(_ new: MobileSession) {
        lock.lock(); defer { lock.unlock() }
        session = new
    }

    func updateSession(_ replacement: MobileSession?, ifCurrent expected: MobileSession) -> SessionStoreUpdate {
        lock.withLock {
            guard session == expected else { return SessionStoreUpdate(applied: false, current: session) }
            session = replacement
            return SessionStoreUpdate(applied: true, current: session)
        }
    }

    func accepts(_ request: URLRequest) -> Bool {
        lock.lock(); defer { lock.unlock() }
        guard let header = request.value(forHTTPHeaderField: "Authorization") else { return true } // unauthenticated route
        return rotated && header == "Bearer \(session?.accessToken ?? "")"
    }

    func rotate() -> PerfFixture.Response {
        lock.lock(); defer { lock.unlock() }
        rotated = true
        let session = MobileSession(accessToken: Self.jwt(expiresIn: 3600, tag: "fresh"), refreshToken: "perf-refresh-2", expiresIn: 3600)
        self.session = session
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
        PerfCounters.shared.increment("poster.protocolRegistered")
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

/// Serves the editor's source clips (`https://fixture-sources.invalid/clipN.mp4`) from bundled videos, after the
/// fixture latency. Attached to the source resolver's private download session in Debug builds, because that session
/// is created by the resolver and can never see a process-wide protocol.
final class PerfSourceURLProtocol: URLProtocol, @unchecked Sendable {
    static let host = "fixture-sources.invalid"
    private var pending: DispatchWorkItem?

    override class func canInit(with request: URLRequest) -> Bool { request.url?.host == host }
    override class func canonicalRequest(for request: URLRequest) -> URLRequest { request }

    override func startLoading() {
        PerfCounters.shared.increment("source.download")
        let work = DispatchWorkItem { [weak self] in self?.respond() }
        pending = work
        DispatchQueue.global().asyncAfter(deadline: .now() + PerfFixture.delay(for: request), execute: work)
    }

    private func respond() {
        guard let url = request.url,
              let index = Int(url.lastPathComponent.dropFirst("clip".count).prefix { $0.isNumber }),
              PerfFixture.editorSourceFiles.indices.contains(index),
              let file = Bundle.main.url(forResource: PerfFixture.editorSourceFiles[index], withExtension: "mp4"),
              let data = try? Data(contentsOf: file),
              let response = HTTPURLResponse(url: url, statusCode: 200, httpVersion: nil, headerFields: [
                  "Content-Type": "video/mp4", "Content-Length": String(data.count),
              ]) else {
            client?.urlProtocol(self, didFailWithError: URLError(.fileDoesNotExist))
            return
        }
        client?.urlProtocol(self, didReceive: response, cacheStoragePolicy: .notAllowed)
        client?.urlProtocol(self, didLoad: data)
        client?.urlProtocolDidFinishLoading(self)
    }

    override func stopLoading() { pending?.cancel() }
}

extension PerfFixture {
    /// The picture for a gallery poster URL (`https://fixture-posters.invalid/<id>.jpg?sig=...`), or nil for any other
    /// URL. Served to the poster cache directly, so it works on a physical phone too, where `AsyncImage` bypasses the
    /// fixture's URL protocol. Waits the fixture latency and counts as one "network" fetch.
    static func posterData(for url: URL) async throws -> Data? {
        guard url.host == PerfPosterURLProtocol.host else { return nil }
        PerfCounters.shared.increment("poster.network")
        try await Task.sleep(for: .milliseconds(max(latencyMS, 0)))
        let names = ["montage", "voiceover", "broll", "talking", "trulli-street", "lisbon"]
        let name = names[abs(url.lastPathComponent.hashValue) % names.count]
        guard let file = Bundle.main.url(forResource: name, withExtension: "jpg") else { throw URLError(.fileDoesNotExist) }
        return try Data(contentsOf: file)
    }

    /// SwiftData container for `-ui-testing-chat`. In memory by default; `KRIA_PERF_DISK_STORE=1` keeps it on disk so
    /// a second launch finds the chats the first one cached.
    static func container() -> ModelContainer {
        let models: [any PersistentModel.Type] = [CachedProject.self, CachedAsset.self, CachedUploadJob.self, CachedReceipt.self]
        let schema = Schema(models)
        guard diskStore else {
            return try! ModelContainer(for: schema, configurations: ModelConfiguration(isStoredInMemoryOnly: true))
        }
        let directory = URL.applicationSupportDirectory.appending(path: "perf", directoryHint: .isDirectory)
        if resetRequested {
            try? FileManager.default.removeItem(at: directory)
            // The editor's downloaded sources, so a cold open really has to download and hash them.
            try? FileManager.default.removeItem(at: URL.cachesDirectory.appending(path: "native-editor-preview", directoryHint: .isDirectory))
            // Synchronous on purpose: the transcript cache must be empty before the first chat opens.
            try? FileManager.default.removeItem(at: URL.applicationSupportDirectory.appending(path: "Transcripts", directoryHint: .isDirectory))
            // And the gallery's poster pictures, so a "first open" really has to fetch them.
            try? FileManager.default.removeItem(at: URL.cachesDirectory.appending(path: "LibraryPosters", directoryHint: .isDirectory))
        }
        try? FileManager.default.createDirectory(at: directory, withIntermediateDirectories: true)
        return try! ModelContainer(for: schema, configurations: ModelConfiguration(schema: schema, url: directory.appending(path: "Kria.store")))
    }
}

/// The `perf-counters` readout in its own window above everything, so a test can read it while a full-screen modal (the
/// editor) hides the chat underneath from the accessibility tree.
@MainActor enum PerfCountersWindow {
    private static var window: UIWindow?

    static func installIfNeeded() {
        guard PerfFixture.countersProbe, window == nil,
              let scene = UIApplication.shared.connectedScenes.compactMap({ $0 as? UIWindowScene }).first else { return }
        let overlay = UIWindow(windowScene: scene)
        overlay.windowLevel = .alert + 1
        overlay.isUserInteractionEnabled = false
        let host = UIHostingController(rootView: PerfCountersProbe())
        host.view.backgroundColor = .clear
        overlay.rootViewController = host
        overlay.frame = CGRect(x: 0, y: 0, width: 4, height: 4)
        overlay.isHidden = false
        window = overlay
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
