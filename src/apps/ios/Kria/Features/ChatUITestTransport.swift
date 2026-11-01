#if DEBUG
import AVFoundation
import Foundation
import KriaMediaEngine
import UIKit

/// Offline chat fixture: every HTTP request is intercepted, even if a caller
/// changes its URL. The existing UI-only fallback supplies projects and drafts.
enum ChatUITestTransport {
    static func api() -> KriaAPI {
        if ProcessInfo.processInfo.environment["KRIA_GENERATED_MEDIA"] == "1" {
            URLProtocol.registerClass(GeneratedMediaImageFixtureProtocol.self)
        }
        let configuration = URLSessionConfiguration.ephemeral
        configuration.protocolClasses = [ChatUITestURLProtocol.self]
        return KriaAPI(
            baseURL: URL(string: "https://chat-ui-testing.invalid")!,
            tokenStore: ChatUITestTokenStore(),
            session: URLSession(configuration: configuration)
        )
    }
}

final class GeneratedMediaImageFixtureProtocol: URLProtocol, @unchecked Sendable {
    override class func canInit(with request: URLRequest) -> Bool { request.url?.host == "generated-media-image.invalid" }
    override class func canonicalRequest(for request: URLRequest) -> URLRequest { request }
    override func startLoading() {
        guard let url = request.url,
              let resource = Bundle.main.url(forResource: "trulli-street", withExtension: "jpg"),
              let data = try? Data(contentsOf: resource),
              let response = HTTPURLResponse(url: url, statusCode: 200, httpVersion: nil, headerFields: ["Content-Type": "image/jpeg"]) else {
            client?.urlProtocol(self, didFailWithError: URLError(.fileDoesNotExist))
            return
        }
        client?.urlProtocol(self, didReceive: response, cacheStoragePolicy: .notAllowed)
        client?.urlProtocol(self, didLoad: data)
        client?.urlProtocolDidFinishLoading(self)
    }
    override func stopLoading() {}
}

/// UI fixtures must never read or rotate a developer's Keychain session.
struct ChatUITestTokenStore: TokenStore {
    // `KRIA_FIXTURE_EXPIRED_TOKEN=1` (perf): an in-memory expired session, so the 401 -> refresh path runs.
    func read() throws -> MobileSession? { PerfFixture.expiredToken ? PerfTokenState.shared.current : nil }
    func write(_ session: MobileSession) throws { if PerfFixture.expiredToken { PerfTokenState.shared.store(session) } }
    func delete() throws {}
}

private final class ChatUITestURLProtocol: URLProtocol, @unchecked Sendable {
    override class func canInit(with request: URLRequest) -> Bool { true }
    override class func canonicalRequest(for request: URLRequest) -> URLRequest { request }
    private var delayedResponse: DispatchWorkItem?
    override func startLoading() {
        if PerfFixture.isFullProjectionRead(request) {
            Perf.mark("fixture.full.arrived")
            PerfCounters.shared.setOnce("fixture fullDelayMS", Int(PerfFixture.delay(for: request) * 1000))
        }
        if request.httpMethod == "POST", ["turns", "messages"].contains(request.url?.lastPathComponent ?? ""),
           let delay = ProcessInfo.processInfo.environment["KRIA_CHAT_SEND_DELAY"].flatMap(Double.init), delay > 0 {
            let work = DispatchWorkItem { [weak self] in self?.finishLoading() }
            delayedResponse = work
            DispatchQueue.global().asyncAfter(deadline: .now() + delay, execute: work)
            return
        }
        // `KRIA_SLIDE_POST_CAPS=slow`: capabilities answer after 6s (the slide editor must not wait for them).
        let slowCapabilities = request.url?.path.hasSuffix("/capabilities") == true
            && ProcessInfo.processInfo.environment["KRIA_SLIDE_POST_CAPS"] == "slow"
        // `KRIA_CHAT_SLOW_READS=1` (KRI-640): every chat or capabilities read after the first capabilities read
        // answers 10s late, so anything a new chat waits for beyond its create call shows up as a stall.
        let slowRead = ProcessInfo.processInfo.environment["KRIA_CHAT_SLOW_READS"] == "1"
            && CreationChatFixture.shared.isSlowRead(request)
        if slowCapabilities || slowRead || (request.url?.path.hasSuffix("/messages") == true
            && ProcessInfo.processInfo.environment["KRIA_CHAT_SLOW_CREATION"] == "1") {
            let work = DispatchWorkItem { [weak self] in self?.finishLoading() }
            delayedResponse = work
            DispatchQueue.global().asyncAfter(deadline: .now() + (slowRead ? 10 : slowCapabilities ? 6 : 5), execute: work)
        } else if PerfFixture.delay(for: request) > 0 {
            // Concurrent queue: parallel requests overlap the way they do on a real network.
            let work = DispatchWorkItem { [weak self] in self?.finishLoading() }
            delayedResponse = work
            DispatchQueue.global().asyncAfter(deadline: .now() + PerfFixture.delay(for: request), execute: work)
        } else { finishLoading() }
    }
    private func finishLoading() {
        if PerfFixture.isFullProjectionRead(request) { Perf.mark("fixture.full.sent") }
        if let perf = PerfFixture.respond(request) {
            guard let url = request.url,
                  let response = HTTPURLResponse(url: url, statusCode: perf.status, httpVersion: nil, headerFields: perf.headers.merging(["Content-Type": "application/json"]) { first, _ in first }) else {
                client?.urlProtocol(self, didFailWithError: URLError(.badURL))
                return
            }
            client?.urlProtocol(self, didReceive: response, cacheStoragePolicy: .notAllowed)
            client?.urlProtocol(self, didLoad: perf.data)
            client?.urlProtocolDidFinishLoading(self)
            return
        }
        let fixture: (Int, Data)? = ProcessInfo.processInfo.environment["KRIA_CHAT_CREATION_FLOW"] != nil
            ? CreationChatFixture.shared.respond(request)
            : (503, Data())
        // No fixture response means the connection dropped before the server answered.
        guard let result = fixture else {
            client?.urlProtocol(self, didFailWithError: URLError(.notConnectedToInternet))
            return
        }
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
final class CreationChatFixture: @unchecked Sendable {
    static let shared = CreationChatFixture()
    private let lock = NSLock()
    private var threads: [String: [String: Any]] = [:]
    private var renders: [String: Int] = [:]
    private var preparations: [String: Int] = [:]
    /// `KRIA_CHAT_SPEECH_CLEANUP`: full reads since the approval, replaying the server's own dispatch shape
    /// (`execute_kria_approval`): first a draft with nothing in flight, then `executing` with no event, then
    /// `render_queued` + the waiting `plan_block` and a processing job together.
    private var approvalDispatches: [String: Int] = [:]
    private var planVersions: [String: Int] = [:]
    private var generateConflicts: [String: Int] = [:]
    private var failedGenerates: Set<String> = []
    private var sendRefused = false
    private var slideDrafts: [String: [String: Any]] = [:]
    private var slideRendered: Set<String> = []
    private var generatedMediaOperations: [String: [String: Any]] = [:]
    private var generatedMediaAssets: [String: [[String: Any]]] = [:]
    private var generatedMediaPostCount = 0
    private var generatedMediaLookupCount = 0
    private var rejectedGeneratedMediaEvent: String?
    private var loadedGeneratedMediaFixture = false

    private var generatedFixtureStorageKey: String? {
        ProcessInfo.processInfo.environment["KRIA_GENERATED_MEDIA_PERSIST"].map { "kria.generated-media-fixture." + $0 }
    }

    private func loadGeneratedFixture() {
        guard !loadedGeneratedMediaFixture else { return }
        loadedGeneratedMediaFixture = true
        guard let key = generatedFixtureStorageKey, let data = UserDefaults.standard.data(forKey: key),
              let value = (try? JSONSerialization.jsonObject(with: data)) as? [String: Any] else { return }
        threads = value["threads"] as? [String: [String: Any]] ?? [:]
        generatedMediaOperations = value["operations"] as? [String: [String: Any]] ?? [:]
        generatedMediaAssets = value["assets"] as? [String: [[String: Any]]] ?? [:]
        generatedMediaPostCount = value["posts"] as? Int ?? 0
        generatedMediaLookupCount = value["lookups"] as? Int ?? 0
    }

    private func saveGeneratedFixture() {
        guard let key = generatedFixtureStorageKey else { return }
        let value: [String: Any] = ["threads": threads, "operations": generatedMediaOperations,
            "assets": generatedMediaAssets, "posts": generatedMediaPostCount, "lookups": generatedMediaLookupCount]
        if let data = try? JSONSerialization.data(withJSONObject: value) { UserDefaults.standard.set(data, forKey: key) }
    }
    private var chatEditFailed = false
    /// `KRIA_SLIDE_POST_LATE_ASSET=1`: a fourth photo that is still processing for the first two reads
    /// after a draft exists and then turns ready, like an upload that finishes while the editor is open.
    private var lateAssetPolls: [String: Int] = [:]
    private var answeredCapabilitiesRead = false
    /// KRI-756: how many times each thread asked for its first-prompt chips (the first answer is `pending`).
    private var suggestionAsks: [String: Int] = [:]
    /// `KRIA_CHAT_SLOW_READS` (KRI-640): GETs of one chat (or anything under it) and every capabilities read but the
    /// first. The project list and the create call stay fast.
    func isSlowRead(_ request: URLRequest) -> Bool {
        lock.lock(); defer { lock.unlock() }
        let path = request.url?.path ?? ""
        guard (request.httpMethod ?? "GET") == "GET", path.hasPrefix("/creation-threads/") else { return false }
        guard path == "/creation-threads/capabilities" else { return true }
        defer { answeredCapabilitiesRead = true }
        return answeredCapabilitiesRead
    }
    /// KRI-460: pictures the fixture "uploaded" (reserve -> register), per item. They join the pool as ready photos the
    /// moment they are registered, exactly like a pasted sticker whose upload finished.
    private var slideStickerAssets: [String: [[String: Any]]] = [:]
    private var deviceRevisions: [String: Int] = [:]
    /// KRI-823: device deliveries the iPhone reported as failed (`/device-render/failures`), until a retry re-pins them.
    private var deviceReportedFailures: Set<String> = []
    /// KRI-443 (`KRIA_CHAT_PLAN_BLOCKS=1`): how far the staged `plan_block` script has advanced, per thread.
    private var planStages: [String: Int] = [:]
    private var planTicks: [String: Int] = [:]
    private var planTurnIDs: [String: String] = [:]
    private var editorTurnIDs: [String: String] = [:]
    /// KRIA_CHAT_QUESTION_BATCH: turns that carried an answer, so a test can pin "both answers, one turn".
    private var batchAnswerTurns = 0
    /// KRIA_CHAT_MARK_OBJECT=reject: the first mark was refused.
    private var markRejected = false
    /// KRIA_CHAT_MOMENT_ORDER (KRI-723): the open moments question, and whether the first answer was refused / staled.
    private var momentQuestionID: String?
    private var momentRefused = false
    /// Live plan & review (`KRIA_CHAT_PLAN_REVIEW=1`): the contract-v2 server model (`GET /plan`, scoped turns, undo).
    private let review = ReviewPlanFixture()
    private var reviewEnabled: Bool { ProcessInfo.processInfo.environment["KRIA_CHAT_PLAN_REVIEW"] == "1" }
    private var editorTurnTicks: [String: Int] = [:]
    /// `KRIA_CHAT_THOUGHTS` (KRI-627): `1` serves a thought that completes with the follow-up turn; `hold` keeps that turn
    /// (and so the streaming thought) open for the whole test. This is the client request id of the latest turn.
    private var thoughtsEnabled: Bool { ["1", "hold"].contains(ProcessInfo.processInfo.environment["KRIA_CHAT_THOUGHTS"] ?? "") }
    private var thoughtRequestIDs: [String: String] = [:]
    private static let fixtureThoughtText = """
    **Reading your direction**

    The user wants a new title on the Lisbon clip.

    ```json
    {"slot": 1, "op": "add_text"}
    ```

    **Matching the Lisbon title to your clip**

    Checking which moment of the clip the title should land on.
    """
    private let approvalID = "aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa"
    static var researchPreviewFixture: [String: Any] { [
        "version": 1,
        "retrieved_at": "2026-10-25T09:00:00Z",
        "future_key": ["ignored": true],
        "explanations": [
            [
                "text": "Galata Tower was built in 1348 by the Genoese.",
                "start_s": 3.0, "end_s": 7.5,
                "unknown_row_key": 1,
                "sources": [
                    ["title": "Galata Tower | History", "url": "https://www.britannica.com/topic/Galata-Tower"],
                    ["title": "en.wikipedia.org", "url": "https://vertexaisearch.cloud.google.com/grounding-api-redirect/fixture"],
                ],
            ],
            // No public source: never shown as if it were sourced.
            ["text": "Unsourced claim", "start_s": 8.0, "end_s": 9.0, "sources": [["title": "Local", "url": "http://intranet.local/x"]]],
        ],
    ] }
    private var runtime: Int { ProcessInfo.processInfo.environment["KRIA_CHAT_CREATION_FLOW"] == "v2" ? 2 : 1 }
    /// KRI-588: why the fixture server refuses a `song_range` answer, or nil when it takes it. A whole-song answer
    /// (both ends null) is always fine; "invalid" / "stale" modes refuse every range.
    private func songRangeRefusal(_ range: [String: Any]?) -> (status: Int, code: String, message: String)? {
        guard let range else { return nil }
        let mode = ProcessInfo.processInfo.environment["KRIA_CHAT_SONG_RANGE_Q"]
        let start = range["start_s"] as? Double, end = range["end_s"] as? Double
        if mode == "stale" { return (409, "song_range_stale", "That question is already answered.") }
        if mode == "invalid", start != nil || end != nil { return (422, "song_range_invalid", "That starts after your song ends.") }
        guard let start, let end else { return nil }
        if start >= 90 { return (422, "song_range_invalid", "That starts after your song ends.") }
        if end - start < 5 { return (422, "song_range_invalid", "Pick at least five seconds of your song.") }
        return nil
    }
    /// KRI-756: the server's `SuggestionSetOut`, shaped like the real contract. `ready` once a render finished,
    /// `first_prompt` while footage is attached and nothing was said, else `none`. The first ask of a first-prompt
    /// set answers `pending` with chip #1 alone (clip understanding still running); later asks answer the full set,
    /// under the same `set_id`, as the real server does.
    private func fixtureSuggestions(id: String, thread: [String: Any]) -> [String: Any] {
        func chip(_ rank: Int, _ kind: String, _ label: String, _ message: String, why: String? = nil) -> [String: Any] {
            ["id": "sc_fixture_\(kind)", "kind": kind, "label": label, "message": message, "rank": rank, "default_edit": kind == "default_edit",
             "anchor": [:] as [String: Any], "capability": "fixture", "why": why ?? NSNull(),
             "label_source": why == nil ? "template" : "agent", "requires_protocol": NSNull()]
        }
        func set(_ state: String, status: String = "ok", wording: String = "agent", understanding: String = "done",
                 retry: Int? = nil, _ chips: [[String: Any]]) -> [String: Any] {
            ["schema_version": 1, "set_id": "sg_fixture_\(state)", "state": state, "status": status, "understanding": understanding,
             "retry_after_s": retry ?? NSNull(), "wording": wording,
             "anchor": ["media_digest": "fixture", "manifest_hash": NSNull(), "render_generation_id": NSNull(), "variant_id": NSNull()],
             "chips": chips, "future_field": ["ignored": true]]
        }
        let events = thread["events"] as? [[String: Any]] ?? []
        let media = (thread["state"] as? [String: Any])?["media"] as? [[String: Any]] ?? []
        if (thread["job"] as? [String: Any])?["status"] as? String == "ready" {
            return set("ready", [chip(1, "hook", "Add a hook", "Add a hook that matches the video"),
                                 chip(2, "captions", "Add captions", "Add captions"),
                                 chip(3, "music", "Turn the music down", "Turn the music down"),
                                 chip(4, "transition", "Add transitions", "Add transitions between the clips")])
        }
        if events.contains(where: { $0["event_type"] as? String == "user_message" }) || media.isEmpty { return set("none", []) }
        let lead = chip(1, "default_edit", "Let Kria cut the first version", ChatSubmission.mediaOnlyMessage)
        suggestionAsks[id, default: 0] += 1
        let pendingMode = ProcessInfo.processInfo.environment["KRIA_SUGGESTION_CHIPS_PENDING"]
        if pendingMode == "hold" || (suggestionAsks[id] == 1 && pendingMode != "0") {
            return set("first_prompt", status: "pending", wording: "pending", understanding: "pending", retry: 2, [lead])
        }
        return set("first_prompt", [lead,
                                    chip(2, "hook", "Add a hook to the opening", "Add a hook that matches the video", why: "the first shot is a quiet one"),
                                    chip(3, "captions", "Add captions", "Add captions"),
                                    chip(4, "sfx", "Add sound effects", "Add sound effects")])
    }
    /// Returns nil when the request should fail without any HTTP response.
    func respond(_ request: URLRequest) -> (Int, Data)? {
        lock.lock(); defer { lock.unlock() }
        loadGeneratedFixture()
        defer { saveGeneratedFixture() }
        let path = request.url?.path ?? ""
        let body = (try? JSONSerialization.jsonObject(with: bodyData(request))) as? [String: Any] ?? [:]
        func response(_ object: Any, status: Int = 200) -> (Int, Data) { (status, (try? JSONSerialization.data(withJSONObject: object)) ?? Data()) }
        if path.hasPrefix("/me/memory"), ProcessInfo.processInfo.environment["KRIA_CHAT_FINISHED_LEARNING"] == "1" {
            let reply = AccountTextPreferenceFixture.shared.reply(request, stale: false, learning: true, initiallyEnabled: true)
            return (reply.0, Data(reply.1.utf8))
        }
        seedDeviceDeliveryThread()
        mirrorDeviceDelivery()
        if path == "/creation-threads/capabilities" {
            // `KRIA_SLIDE_POST_CAPS=fail`: the capabilities request never gets an answer.
            if ProcessInfo.processInfo.environment["KRIA_SLIDE_POST_CAPS"] == "fail" { return nil }
            // `KRIA_CHAT_TALKING_MAX_CLIPS`: Talking's cap. 10 is a server with KRI-719 multi-clip Talking on;
            // `1` is one with it off (a single pick then closes Apple's picker on its own).
            let talkingMaxClips = ProcessInfo.processInfo.environment["KRIA_CHAT_TALKING_MAX_CLIPS"].flatMap(Int.init) ?? 10
            var capabilities: [String: Any] = ["formats": [("montage", "montage", 10), ("narrated", "narrated_planned", 10), ("talking_to_camera", "subtitled", talkingMaxClips), ("slides", "slides", 20)].map { ["id": $0.0, "edit_format": $0.1, "max_clips": $0.2] as [String: Any] }, "runtime_versions": runtime == 2 ? [1, 2] : [1], "visuals_enabled": true]
            if ProcessInfo.processInfo.environment["KRIA_CHAT_PLAN_BLOCKS"] == "1" { capabilities["live_plan_review_enabled"] = true }
            // `-camera-roll-surface-fixture` (KRI-944): the Camera roll row in the drawer. The surface brings its own fixture API.
            if ProcessInfo.processInfo.arguments.contains("-camera-roll-surface-fixture") { capabilities["media_collections"] = true }
            // `KRIA_SUGGESTION_CHIPS=1` (KRI-756): the server offers footage-aware suggestion chips. Unset = an old server.
            if ProcessInfo.processInfo.environment["KRIA_SUGGESTION_CHIPS"] == "1" { capabilities["suggestion_chips"] = true }
            // `KRIA_CHAT_PLAN_BLOCKS_VERSION=2`: the server speaks live-plan contract v2 (structured payloads,
            // 8 sections including post_caption, Review entry points). Absent = an older feed-only server.
            if ProcessInfo.processInfo.environment["KRIA_CHAT_PLAN_BLOCKS"] == "1",
               let version = ProcessInfo.processInfo.environment["KRIA_CHAT_PLAN_BLOCKS_VERSION"].flatMap(Int.init) { capabilities["live_plan_review_version"] = version }
            if ProcessInfo.processInfo.environment["KRIA_SLIDE_POST_RICH_TEXT"] == "1" { capabilities["slide_post_rich_text"] = true }
            if ProcessInfo.processInfo.environment["KRIA_SLIDE_POST_EXTENDED_DEVICE_EXPORT"] == "1" { capabilities["slide_post_extended_device_export"] = true }
            if ProcessInfo.processInfo.environment["KRIA_SLIDE_POST_CHAT_EDIT"] == "1" { capabilities["slide_post_chat_edit"] = true }
            // KRIA_SLIDE_POST_SIZES (KRI-606): the Size sheet and per-photo Fit. The server needs framing on too.
            // KRIA_SLIDE_POST_DRAWING (KRI-589): the Draw tool. The server only advertises it to protocol 7 builds.
            if ProcessInfo.processInfo.environment["KRIA_SLIDE_POST_DRAWING"] == "1" { capabilities["slide_post_drawing"] = true }
            // KRIA_SLIDE_POST_STICKERS (KRI-460): pasted pictures on slides. The server only advertises it to protocol 8 builds.
            if ProcessInfo.processInfo.environment["KRIA_SLIDE_POST_STICKERS"] == "1" { capabilities["slide_post_stickers"] = true }
            // KRIA_TEXT_BEND (KRI-646): the Bend slider. The server only advertises it to protocol 9 builds.
            if ProcessInfo.processInfo.environment["KRIA_TEXT_BEND"] == "1" { capabilities["text_bend"] = true }
            // KRIA_VOICE_DICTATION (KRI-742): the mic button; the engine is scripted (see DictationController).
            if ProcessInfo.processInfo.environment["KRIA_VOICE_DICTATION"] == "1" { capabilities["voice_dictation"] = true }
            if ProcessInfo.processInfo.environment["KRIA_SLIDE_POST_SIZES"] == "1" {
                capabilities["slide_post_framing"] = true
                capabilities["slide_post_sizes"] = true
            }
            // KRIA_CHAT_CLIP_QUESTION: "1" = server advertises clip_selection_questions; "legacy" = it still
            // sends the clip_question payload but does not advertise the capability (old-server fallback);
            // "later-suggested" = "1" with the suggestion on the second group instead of the first.
            if ["1", "history", "photo", "later-suggested"].contains(ProcessInfo.processInfo.environment["KRIA_CHAT_CLIP_QUESTION"]) { capabilities["clip_selection_questions"] = true }
            // KRIA_CHAT_SONG_ORDER: "1" = server advertises media.song, song_order_questions and
            // song_order_placements (the song timeline, KRI-561); "list" = media.song and song_order_questions only
            // (the vertical order list, an older server); "legacy" = it still sends the song_order_question payload
            // but advertises nothing (old-app fallback).
            if ["1", "list"].contains(ProcessInfo.processInfo.environment["KRIA_CHAT_SONG_ORDER"]) {
                capabilities["song_order_questions"] = true
                if ProcessInfo.processInfo.environment["KRIA_CHAT_SONG_ORDER"] == "1" { capabilities["song_order_placements"] = true }
                capabilities["media"] = ["song": ["max": 1, "max_file_bytes": 52_428_800, "content_types": ["audio/mpeg", "audio/mp4"]]]
            }
            // KRIA_CHAT_SONG_RANGE (KRI-580): a 90 s song is attached to the project. "1" = the server also
            // advertises song_filming_range; "off" = it advertises media.song only (an older server).
            if let mode = ProcessInfo.processInfo.environment["KRIA_CHAT_SONG_RANGE"] {
                capabilities["media"] = ["song": ["max": 1, "max_file_bytes": 52_428_800, "content_types": ["audio/mpeg", "audio/mp4"]]]
                if mode == "1" { capabilities["song_filming_range"] = true }
            }
            // KRIA_CHAT_SONG_RANGE_Q (KRI-588): a 90 s song is attached and the first turn is answered with the
            // "Which part of the song did you film?" question. "1" = the server advertises song_range_questions;
            // "off" = it advertises media.song and song_order_questions only (an older server: the text still shows);
            // "invalid" / "stale" = like "1", but every range answer is refused (422 / 409), as if the server's
            // rules had drifted from the card's.
            if let mode = ProcessInfo.processInfo.environment["KRIA_CHAT_SONG_RANGE_Q"] {
                capabilities["media"] = ["song": ["max": 1, "max_file_bytes": 52_428_800, "content_types": ["audio/mpeg", "audio/mp4"]]]
                capabilities["song_order_questions"] = true
                if mode != "off" { capabilities["song_range_questions"] = true }
            }
            // KRIA_CHAT_MARK_OBJECT (KRI-901): the first turn is answered with "Mark the plate on one frame". "1" = the
            // server advertises protected_region_marks; "off" = an older server (the question text still shows, no card);
            // "reject" = like "1", but the first mark is refused (422 protected_region_invalid).
            if let mode = ProcessInfo.processInfo.environment["KRIA_CHAT_MARK_OBJECT"], mode != "off" {
                capabilities["protected_region_marks"] = true
            }
            // KRIA_CHAT_MOMENT_ORDER (KRI-723): the first turn is answered with "show me your moments". "1" = the server
            // advertises moment_order_questions; "off" = an older server (text only, no card); "reject" = the first answer
            // is refused (422 moment_order_overlap); "stale" = the first answer meets a replaced question (409).
            if let mode = ProcessInfo.processInfo.environment["KRIA_CHAT_MOMENT_ORDER"], mode != "off" {
                capabilities["moment_order_questions"] = true
            }
            // KRIA_CHAT_CHOICE_QUESTION: "1" = server advertises choice_questions; "legacy" = it still sends the
            // choice_question payload but does not advertise the capability (old-server text fallback).
            if ProcessInfo.processInfo.environment["KRIA_CHAT_CHOICE_QUESTION"] == "1" { capabilities["choice_questions"] = true }
            // KRIA_CHAT_VOICEOVER_SCRIPT (KRI-815): "1" = server advertises voiceover_script_cards; "legacy" = it still
            // sends the voiceover_script payload but does not advertise the capability (old-client text fallback).
            if ProcessInfo.processInfo.environment["KRIA_CHAT_VOICEOVER_SCRIPT"] == "1" { capabilities["voiceover_script_cards"] = true }
            if let generated = ProcessInfo.processInfo.environment["KRIA_GENERATED_MEDIA"] {
                for key in ["generated_images", "generated_image_editing", "generated_narration"] {
                    capabilities[key] = generated == "1"
                }
            }
            // KRIA_CHAT_QUESTION_BATCH (KRI-802): the first reply asks the title wording AND the clip picker.
            if ProcessInfo.processInfo.environment["KRIA_CHAT_QUESTION_BATCH"] == "1" {
                capabilities["choice_questions"] = true
                capabilities["clip_selection_questions"] = true
            }
            if DeviceRenderUITestFixture.scenario != nil || ProcessInfo.processInfo.environment["KRIA_TELEPROMPTER_REVIEW_FIXTURE"] == "1" {
                capabilities["phone_rendering"] = ["enabled": true, "recipe_versions": [1, 2], "verified_features": MediaCapability.allCases.map(\.rawValue)]
            }
            return response(capabilities)
        }
        if path.hasSuffix("/upload-urls"), ProcessInfo.processInfo.environment["KRIA_TELEPROMPTER_REVIEW_FIXTURE"] == "1" {
            return response(["detail": "This fixture deliberately rejects intake."], status: 422)
        }
        // `KRIA_SLIDE_POST_READY_THREAD=1`: a READY slide post already exists. The LIST (drawer) payload is the
        // worst case (no `state.format`, no variants), while the FULL projection carries the format, which is
        // what the app only learns after the thread loads.
        let readySlideID = "BBBBBBBB-BBBB-BBBB-BBBB-BBBBBBBBBBBB"
        if ProcessInfo.processInfo.environment["KRIA_SLIDE_POST_READY_THREAD"] == "1", threads[readySlideID] == nil {
            func event(_ seq: Int, _ type: String, _ payload: [String: Any] = [:]) -> [String: Any] {
                ["id": UUID().uuidString, "sequence": seq, "revision": seq + 1, "role": "assistant", "event_type": type, "content": "", "payload": payload, "created_at": "2026-09-10T10:00:00Z"]
            }
            threads[readySlideID] = ["id": readySlideID, "title": "Weekend trip", "status": "active", "revision": 3, "runtime_version": 2,
                "state": ["format": "slides", "edit_format": "slides"],
                "events": [event(0, "thread_created"), event(1, "format_prompt"), event(2, "action_select_format", ["format": "slides"])],
                "active_plan_item_id": readySlideID, "active_job_id": readySlideID,
                "job": ["id": readySlideID, "status": "ready", "variants": [["variant_id": "slides", "render_status": "ready"]]],
                "updated_at": "2026-09-10T10:00:00Z"]
        }
        if path == "/creation-threads" {
            if request.httpMethod != "POST", ProcessInfo.processInfo.environment["KRIA_SLIDE_POST_READY_THREAD"] == "1" {
                let listed: [[String: Any]] = threads.values.map { thread in
                    guard thread["id"] as? String == readySlideID else { return thread }
                    var stripped = thread; stripped["state"] = [:]; stripped["events"] = []
                    stripped["job"] = ["id": readySlideID, "status": "ready", "variants": []]
                    return stripped
                }
                return response(listed)
            }
            if request.httpMethod == "POST" {
                let id = UUID().uuidString
                let fixtureEvents: [[String: Any]] = PerfFixture.longHistoryCount != nil
                    ? (0..<(PerfFixture.longHistoryCount ?? 24)).map { index in
                        ["id": UUID().uuidString, "sequence": index, "revision": index + 1,
                         "role": index.isMultiple(of: 2) ? "user" : "assistant",
                         "event_type": index.isMultiple(of: 2) ? "user_message" : "assistant_response",
                         "content": "Long conversation message number \(index + 1) for drawer indicator diagnostics.",
                         "payload": [:], "created_at": "2026-09-10T10:00:00Z"]
                    }
                    : []
                var seededEvents = fixtureEvents
                var seededState: [String: Any] = [:]
                if ProcessInfo.processInfo.environment["KRIA_CHAT_CLIP_QUESTION"] == "history" {
                    (seededEvents, seededState) = Self.realShapeClipQuestionHistory()
                } else if ProcessInfo.processInfo.environment["KRIA_CHAT_CLIP_QUESTION"] == "photo" {
                    (seededEvents, seededState) = Self.realShapeClipQuestionHistory(poolPhoto: true)
                }
                let thread: [String: Any] = ["id": id, "title": "Untitled project", "status": "active", "revision": seededEvents.count, "runtime_version": runtime, "state": seededState, "events": seededEvents, "active_plan_item_id": id, "updated_at": "2026-09-10T10:00:00Z"]
                threads[id] = thread
                return response(thread, status: 201)
            }
            return response(Array(threads.values))
        }
        let parts = path.split(separator: "/").map(String.init)
        // Gallery row for the READY slide post (`KRIA_SLIDE_POST_READY_THREAD=1`).
        if path == "/me/jobs", ProcessInfo.processInfo.environment["KRIA_SLIDE_POST_READY_THREAD"] == "1" {
            let id = "BBBBBBBB-BBBB-BBBB-BBBB-BBBBBBBBBBBB"
            return response(["jobs": [["id": id, "title": "Weekend trip", "status": "ready", "output_variant_id": "slides", "content_plan_item_id": id, "created_at": "2026-09-10T10:00:00Z"]], "next_cursor": NSNull()])
        }
        if parts.count >= 4, parts[0] == "me", parts[1] == "jobs", parts[3] == "device-render",
           let scenario = DeviceRenderUITestFixture.scenario, let jobID = UUID(uuidString: parts[2]) {
            return deviceRenderResponse(scenario: scenario, jobID: jobID, route: parts.count > 4 ? parts[4] : nil, body: body)
        }
        if ProcessInfo.processInfo.environment["KRIA_CHAT_DEVICE_SYNC_FAILURE"] == "1",
           parts.first == "generative-jobs" {
            if parts.last == "status" {
                return response(["variants": [[
                    "variant_id": DeviceRenderUITestFixture.variantID, "render_status": "awaiting_device",
                    "render_destination": "device", "duration_s": 1, "render_generation_id": "fixture-generation",
                    "ai_timeline": ["slots": [["slot_id": "clip-0", "clip_index": 0, "in_s": 0, "duration_s": 1, "source_duration_s": 1]]]
                ]]])
            }
            if parts.last == "timeline", let projectID = UUID(uuidString: parts[1]),
               let source = SongTimelineFixture.clipURL(mediaID: "fixture-clip", force: true) {
                do {
                    let project = BackgroundUploadCoordinator.projectDirectory(projectID)
                    try project.createIfNeeded()
                    let target = project.originals.appendingPathComponent("sync-recovery-fixture.mp4")
                    if !FileManager.default.fileExists(atPath: target.path) { try FileManager.default.copyItem(at: source, to: target) }
                    let fingerprint = try SHA256Fingerprinter().fingerprint(file: target)
                    let asset = MediaAsset(id: "fixture-clip", relativePath: "originals/sync-recovery-fixture.mp4",
                                           fingerprint: fingerprint, duration: 8, naturalSize: MediaSize(width: 180, height: 320), frameRate: 10)
                    try SourceAssetStore(project: project).bind(mediaID: "fixture-clip", original: asset)
                    return response(["base_generation": "fixture-generation", "clips": [["clip_index": 0, "native_source": [
                        "media_id": "fixture-clip", "source_url": NSNull(), "local_required": true,
                        "original": ["kind": "video", "sha256": fingerprint.hex, "byte_count": fingerprint.byteCount,
                                     "duration_s": 8, "width": 180, "height": 320, "orientation_degrees": 0, "has_audio": false]
                    ]]]])
                } catch { return response(["detail": "Fixture original could not be prepared"], status: 500) }
            }
        }
        if parts.first == "plan-items", parts.count >= 2, ProcessInfo.processInfo.environment["KRIA_SLIDE_POST_FIXTURE"] == "1" {
            return slideResponse(request, itemID: parts[1], parts: parts, body: body)
        }
        // KRI-693 (`KRIA_CHAT_CLIP_QUESTION=photo`): the plan item's Visuals hold the pool photo the picker offers.
        if parts.first == "plan-items", parts.last == "assets", ProcessInfo.processInfo.environment["KRIA_CHAT_CLIP_QUESTION"] == "photo" {
            let photo = Bundle.main.url(forResource: "trulli-street", withExtension: "jpg")?.absoluteString ?? ""
            return response(["assets": [["id": Self.poolPhotoAssetID, "kind": "image", "status": "ready", "media_status": "available",
                                         "source_filename": "N1_09_visual.jpg", "display_url": photo]], "max_assets": 10])
        }
        if parts.first == "plan-items", parts.count >= 3, parts[2] == "generated-media",
           ProcessInfo.processInfo.environment["KRIA_GENERATED_MEDIA"] == "1" {
            let itemID = parts[1]
            let script: [String: Any] = ["script_id": "vos-fixture", "script_version": 1,
                "text": VoiceoverScriptUITestFixture.text, "approved_script_sha256": "fixture-approved-script"]
            if parts.last == "narration-script" { return response(script) }
            if parts.count == 3, request.httpMethod == "POST" {
                generatedMediaPostCount += 1
                if ProcessInfo.processInfo.environment["KRIA_GENERATED_MEDIA_REJECT_ONCE"] == "1" {
                    if generatedMediaPostCount == 1 {
                        rejectedGeneratedMediaEvent = body["client_event_id"] as? String
                        return response(["detail": "Source changed before submission"], status: 409)
                    }
                    guard body["client_event_id"] as? String != rejectedGeneratedMediaEvent else {
                        return response(["detail": "An explicitly edited request requires a new event"], status: 409)
                    }
                }
                if ProcessInfo.processInfo.environment["KRIA_GENERATED_MEDIA_RECOVERED"] == "1" {
                    return response(["detail": "Recovery must not POST generation again"], status: 409)
                }
                if body["capability"] as? String == "image", let eventID = body["client_event_id"] as? String {
                    let imageURL = "https://generated-media-image.invalid/cover.jpg"
                    let operation: [String: Any] = ["id": eventID, "state": "ready", "capability": "image",
                        "asset_id": "generated-" + eventID, "preview_url": imageURL, "source_url": imageURL,
                        "retryable": false, "fixture_title": body["editable_title"] ?? ""]
                    generatedMediaOperations[eventID] = operation
                    return response(operation)
                }
                guard body["capability"] as? String == "narration",
                      body["prompt"] as? String == VoiceoverScriptUITestFixture.text,
                      body["script_id"] as? String == "vos-fixture", body["script_version"] as? Int == 1,
                      body["approved_script_sha256"] as? String == "fixture-approved-script",
                      let eventID = body["client_event_id"] as? String else {
                    return response(["detail": "Fixture rejected unapproved narration"], status: 409)
                }
                do {
                    let voiceURL = try Self.generatedNarrationFixtureURL()
                    let operation: [String: Any] = ["id": eventID, "state": "ready", "capability": "narration",
                        "asset_id": NSNull(), "preview_url": voiceURL.absoluteString, "source_url": voiceURL.absoluteString,
                        "retryable": false, "script_id": "vos-fixture", "script_version": 1,
                        "approved_script_sha256": "fixture-approved-script"]
                    generatedMediaOperations[eventID] = operation
                    return response(operation)
                } catch { return response(["detail": "Fixture audio unavailable"], status: 500) }
            }
            let operationID = parts.count > 3 ? (parts[3] == "by-event" ? parts.last! : parts[3]) : ""
            guard let operation = generatedMediaOperations[operationID] else { return response(["detail": "Unknown fixture request"], status: 404) }
            if request.httpMethod == "GET" { generatedMediaLookupCount += 1 }
            if parts.last == "accept", request.httpMethod == "POST" {
                if operation["capability"] as? String == "image" {
                    if ProcessInfo.processInfo.environment["KRIA_GENERATED_MEDIA_RECOVERED"] == "1",
                       generatedMediaPostCount != 1 || generatedMediaLookupCount < 1 {
                        return response(["detail": "Recovery counters did not prove one POST and a status lookup"], status: 409)
                    }
                    generatedMediaAssets[itemID] = [["id": operation["asset_id"]!, "kind": "image", "status": "ready",
                        "source_filename": operation["fixture_title"] ?? "", "preview_url": operation["preview_url"]!,
                        "source_url": operation["source_url"]!, "generation": "1"]]
                    return response(operation)
                }
                guard body["script_id"] as? String == "vos-fixture", body["script_version"] as? Int == 1,
                      body["approved_script_sha256"] as? String == "fixture-approved-script",
                      var thread = threads[itemID] else { return response(["detail": "Stale fixture approval"], status: 409) }
                var state = thread["state"] as? [String: Any] ?? [:]
                var media = state["media"] as? [[String: Any]] ?? []
                media.append(["media_id": operationID, "kind": "audio", "filename": "Generated narration.wav", "duration_s": 1])
                state["media"] = media
                thread["state"] = state
                threads[itemID] = thread
            }
            return response(operation)
        }
        if parts.first == "plan-items", parts.count > 1, parts.last == "assets",
           ProcessInfo.processInfo.environment["KRIA_GENERATED_MEDIA"] == "1" {
            return response(["assets": generatedMediaAssets[parts[1]] ?? [], "max_assets": 10])
        }
        if parts.first == "plan-items" { return response(["assets": [], "max_assets": 10]) }
        // `KRIA_CHAT_SEED_THREADS` (perf): an account that already has chats, each built the first time it is asked for.
        if parts.count >= 2, threads[parts[1]] == nil, let index = PerfFixture.seededIndex(of: parts[1]) {
            threads[parts[1]] = PerfFixture.seededThread(index, runtime: runtime)
        }
        guard parts.count >= 2, var thread = threads[parts[1]] else { return response(["detail": "Fixture route missing"], status: 404) }
        let id = parts[1]
        if parts.count == 3, parts[2] == "brief" { return response(Self.fixtureBrief(threadID: id)) }
        // KRI-756 (`KRIA_SUGGESTION_CHIPS=1`): the server's chip sets, shaped like `SuggestionSetOut`. Unset = a server without the route.
        if parts.count == 3, parts[2] == "suggestions" {
            guard ProcessInfo.processInfo.environment["KRIA_SUGGESTION_CHIPS"] == "1" else { return response(["detail": "Not Found"], status: 404) }
            return response(fixtureSuggestions(id: id, thread: thread))
        }
        // KRI-561: the signed song URL. The fixture hands out a generated tone as a file URL (no network).
        if parts.count == 3, parts[2] == "song-audio", ProcessInfo.processInfo.environment["KRIA_CHAT_SONG_ORDER"] == "1" {
            guard let url = SongTimelineFixture.audioURL() else { return response(["detail": "Fixture audio missing"], status: 404) }
            let generation = request.url.flatMap { URLComponents(url: $0, resolvingAgainstBaseURL: false) }?
                .queryItems?.first { $0.name == "generation" }?.value.flatMap(Int.init) ?? SongTimelineFixture.generation
            return response(["url": url.absoluteString, "generation": generation, "duration_s": SongTimelineFixture.audioSeconds,
                             "expires_at": "2099-01-01T00:00:00Z"])
        }
        var state = thread["state"] as? [String: Any] ?? [:]
        var events = thread["events"] as? [[String: Any]] ?? []
        var revision = thread["revision"] as? Int ?? 0
        func append(_ type: String, role: String = "assistant", text: String? = nil, payload: [String: Any] = [:], clientEventID: String? = nil) {
            revision += 1
            var event: [String: Any] = ["id": UUID().uuidString, "sequence": events.count, "revision": revision, "role": role, "event_type": type, "content": text ?? "", "payload": payload, "created_at": "2026-09-10T10:00:00Z"]
            if let clientEventID { event["client_event_id"] = clientEventID }
            events.append(event)
        }
        // KRI-580: the song's filming range. Mirrors the server: both ends null clears it, otherwise both are set,
        // at least 5 s apart, and an end past the song is clamped to the song (90 s here).
        if parts.count == 3, parts[2] == "song-range", request.httpMethod == "PUT" {
            func problem(_ status: Int, _ code: String, _ message: String) -> (Int, Data) {
                response(["problem": ["code": code, "message": message, "phase": "accept", "retryable": false, "recovery": "none"]], status: status)
            }
            guard ProcessInfo.processInfo.environment["KRIA_CHAT_SONG_RANGE"] == "1" else { return problem(404, "song_range_unavailable", "Song range unavailable") }
            guard (body["expected_revision"] as? Int) == revision else { return response(["detail": "Creation thread changed"], status: 409) }
            var media = state["media"] as? [[String: Any]] ?? []
            guard let songIndex = media.firstIndex(where: { $0["role"] as? String == "song" }) else { return problem(409, "song_missing", "Attach a song first.") }
            let songSeconds = 90.0
            let start = body["start_s"] as? Double, end = body["end_s"] as? Double
            if let start, let end {
                if start >= songSeconds { return problem(422, "song_range_invalid", "That starts after your song ends.") }
                if end - start < 5 { return problem(422, "song_range_invalid", "Pick at least five seconds of your song.") }
                media[songIndex]["song_range"] = ["start_s": start, "end_s": min(end, songSeconds)]
            } else {
                media[songIndex].removeValue(forKey: "song_range")
            }
            state["media"] = media
            append("song_range_set", role: "user", payload: ["start_s": start.map { $0 as Any } ?? NSNull(), "end_s": end.map { $0 as Any } ?? NSNull()], clientEventID: body["client_event_id"] as? String)
            thread["state"] = state; thread["events"] = events; thread["revision"] = revision; threads[id] = thread
            return response(thread)
        }
        if reviewEnabled {
            // An update render advances one step per read of the plan or the delta (like the staged feed).
            if parts.last == "delta" || parts.last == "plan", let done = review.tick(id: id) {
                append("plan_block", role: "system", payload: done.blocksEvent)
                append("plan_update_summary", role: "system", payload: done.summaryEvent)
                thread["job"] = ["id": done.jobID, "status": "ready", "variants": [["variant_id": "original_text", "render_status": "ready", "output_url": "https://fixture.invalid/result.mp4"]]]
                append("generation_ready")
            }
            if parts.count == 6, parts[2] == "plan", parts[3] == "sections", parts[5] == "undo" {
                let turn = UUID().uuidString
                if let failure = review.undoSection(id: id, initial: reviewInitialBlocks, section: parts[4], blockRevision: body["expected_block_revision"] as? Int ?? -1, turnID: turn) {
                    return response(failure.1, status: failure.0)
                }
                append("draft_applied", payload: ["turn_id": turn, "draft_id": id])
                append("approval_requested", payload: ["approval_id": approvalID, "turn_id": turn])
                thread["state"] = state; thread["events"] = events; thread["revision"] = revision; threads[id] = thread
                return response(["section_id": parts[4], "thread_revision": revision, "draft_revision": 4, "turn_id": turn])
            }
            if parts.count == 4, parts[2] == "draft", parts[3] == "undo", body["render"] as? Bool == true {
                let turn = UUID().uuidString
                if let failure = review.undoAll(id: id, initial: reviewInitialBlocks, turnID: turn) { return response(failure.1, status: failure.0) }
                append("draft_undone", payload: ["draft_id": id])
                append("approval_requested", payload: ["approval_id": approvalID, "turn_id": turn])
                thread["state"] = state; thread["events"] = events; thread["revision"] = revision; threads[id] = thread
                return response(["draft_id": id, "item_id": id, "variant_key": "original_text", "draft_revision": 4, "snapshot_hash": "hash", "etag": "etag-4",
                                 "snapshot": [String: Any](), "can_undo": false, "created_at": "2026-10-09T10:05:00Z"])
            }
        }
        if parts.last == "thought-summaries", thoughtsEnabled {
            // Streaming while the follow-up editor turn is still running, completed (3s) once its reply has landed.
            let queried = URLComponents(url: request.url!, resolvingAgainstBaseURL: false)?.queryItems?.first(where: { $0.name == "client_request_id" })?.value
            guard let requestID = thoughtRequestIDs[id], queried == nil || queried == requestID else {
                return response(["client_request_id": queried.map { $0 as Any } ?? NSNull(), "summaries": [Any]()])
            }
            let running = editorTurnTicks[id] != nil
            // Like the server, history (no request id) lists completed thoughts only.
            if queried == nil, running { return response(["client_request_id": NSNull(), "summaries": [Any]()]) }
            return response(["client_request_id": requestID, "summaries": [[
                "id": "thought-fixture-1", "client_request_id": requestID, "status": running ? "streaming" : "completed",
                "text": Self.fixtureThoughtText, "started_at": "2026-10-12T10:00:00Z",
                "completed_at": running ? NSNull() as Any : "2026-10-12T10:00:03Z" as Any, "duration_ms": running ? NSNull() as Any : 3000 as Any,
            ]]])
        }
        if parts.last == "cancel-render" {
            if ProcessInfo.processInfo.environment["KRIA_CHAT_PLAN_BLOCKS_CANCEL"] == "unavailable" {
                return response(["problem": ["code": "turn_not_cancellable", "message": "This render can no longer be stopped."]], status: 409)
            }
            // Like the server, a stale revision is refused; the app retries once with the newest one.
            if (body["expected_thread_revision"] as? Int) != revision {
                return response(["problem": ["code": "revision_conflict", "message": "The thread moved on."]], status: 409)
            }
            let turn = parts.count >= 4 ? parts[3] : id
            append("render_cancelled", role: "system", payload: ["turn_id": turn, "job_id": id])
            thread["job"] = ["id": id, "status": "cancelled", "variants": []]
            planStages[id] = nil; renders[id] = nil
            thread["events"] = events; thread["revision"] = revision; threads[id] = thread
            return response(["turn_id": turn, "thread_revision": revision, "status": "cancelled", "approval_ids": [String]()])
        }
        if parts.last == "actions" {
            let action = body["action"] as? String ?? ""
            let payload = body["payload"] as? [String: Any] ?? [:]
            if action == "remove_media", ProcessInfo.processInfo.environment["KRIA_CHAT_REMOVE_MEDIA_FAILURE"] == "1" {
                return response(["detail": "Fixture removal failed"], status: 500)
            }
            if action == "generate", let failure = generateFailure(threadID: id) {
                return failure == "offline" ? nil : response(["detail": "Internal Server Error"], status: 500)
            }
            if action == "generate", let detail = generateConflict(threadID: id) {
                return response(["detail": detail], status: 409)
            }
            if action == "generate", let mismatch = renderShapeMismatch(payload) {
                return response(["detail": mismatch], status: 422)
            }
            if action == "select_format" {
                state["format"] = payload["format"]
                // A phone-render account's clip is an analysis proxy; the original stays on the iPhone.
                let mediaID = DeviceRenderUITestFixture.scenario == nil ? "fixture-clip" : "analysis-proxy-fixture-clip"
                if ProcessInfo.processInfo.environment["KRIA_CHAT_FIXTURE_MEDIA"] == "1" {
                    var clip: [String: Any] = ["media_id": mediaID, "kind": "video", "filename": "sample.mov"]
                    if let seconds = ProcessInfo.processInfo.environment["KRIA_CHAT_FIXTURE_MEDIA_DURATION"].flatMap(Double.init) {
                        clip["duration_s"] = seconds
                    }
                    var media = [clip]
                    if ProcessInfo.processInfo.environment["KRIA_CHAT_CLIP_QUESTION"] != nil || ProcessInfo.processInfo.environment["KRIA_CHAT_SONG_ORDER"] != nil {
                        media += (2...4).map { ["media_id": "fixture-clip-\($0)", "kind": "video", "filename": "sample-\($0).mov"] }
                    }
                    if ProcessInfo.processInfo.environment["KRIA_CHAT_MARK_OBJECT"] != nil {
                        // KRI-901: a second clip whose original is not on this iPhone.
                        media.append(["media_id": "fixture-clip-2", "kind": "video", "filename": "sample-2.mov"])
                    }
                    if ProcessInfo.processInfo.environment["KRIA_CHAT_SONG_RANGE"] != nil || ProcessInfo.processInfo.environment["KRIA_CHAT_SONG_RANGE_Q"] != nil {
                        media.append(["media_id": "fixture-song", "kind": "audio", "role": "song", "filename": "fixture-song.mp3", "duration_s": 90])
                    }
                    if ProcessInfo.processInfo.environment["KRIA_CHAT_FIXTURE_VOICEOVER"] == "1" {
                        media.append(["media_id": "fixture-voiceover", "kind": "audio", "filename": "existing-voiceover.m4a", "duration_s": 6])
                    }
                    state["media"] = media
                }
                // Match the server's user event so an intentional format choice keeps
                // the offline project alive during background capture/recovery tests.
                append("action_select_format", role: "user", payload: payload)
                if ProcessInfo.processInfo.environment["KRIA_CHAT_FIXTURE_MEDIA"] == "1" {
                    append("media_added", role: "system", payload: [
                        "media": [[
                            "media_id": mediaID, "kind": "video", "filename": "sample.mov"
                        ]],
                        "media_count": 1
                    ])
                }
            } else if action == "generate", ProcessInfo.processInfo.environment["KRIA_CHAT_SLOW_CREATION"] == "1" {
                // Mirror `_sync_agent`'s server-side projection (creation_threads.py):
                // `plan_hash`/`version` come from `session.active_plan`, which a
                // job-dispatch failure does not clear -- only `status` moves. A
                // fixture that drops plan_hash here makes the confirmed plan look
                // unconfirmed, wrongly routing the client's FailedWorkspacePresentation
                // away from the legacy confirmation card's "Retry generation".
                let version = planVersions[id, default: 0]
                thread["creator_agent"] = ["status": "failed", "summary": "Open on the laugh and keep the pacing quick.", "version": version, "plan_hash": "fixture-plan-\(version)"]
                state["generation"] = ["status": "failed"]
                append("agent_assistant_error", text: "I couldn't start that render. Your creative plan is still saved.")
            } else if action == "retry", ProcessInfo.processInfo.environment["KRIA_CHAT_SLOW_CREATION"] == "1" {
                thread["creator_agent"] = ["status": "executing", "summary": "Open on the laugh and keep the pacing quick."]
                state["generation"] = ["status": "preparing"]
                preparations[id] = 0
                append("agent_assistant_execution", text: "I started the confirmed edit.")
            } else if action == "generate" {
                // The server moves a confirmed plan awaiting_confirmation ->
                // executing. Without this the fixture keeps a pending plan on
                // top of the finished cut and the ready stage never shows.
                if thread["creator_agent"] != nil {
                    thread["creator_agent"] = ["status": "executing", "summary": "Open on the laugh and keep the pacing quick."]
                }
                thread["active_job_id"] = id
                if DeviceRenderUITestFixture.scenario != nil {
                    // The server hands the variant to this iPhone and waits; no cloud render advances it.
                    thread["job"] = ["id": id, "status": "processing", "variants": [["variant_id": DeviceRenderUITestFixture.variantID, "render_status": "awaiting_device", "render_destination": "device"]]]
                } else {
                    thread["job"] = ["id": id, "status": "processing", "variants": []]
                    renders[id] = 0
                }
                append("generation_started")
            } else if action == "remove_media" { state["media"] = []; append("action_remove_media") }
        } else if parts.last == "messages" || parts.last == "turns" {
            let turnID = body["client_event_id"] as? String ?? id
            if let mode = ProcessInfo.processInfo.environment["KRIA_CHAT_SEND_FAILURE"], mode != "lost-response", !sendRefused,
               mode != "offline-structured" || body["song_range"] != nil {
                sendRefused = true
                if mode == "offline" || mode == "offline-structured" { return nil }
                return response(["detail": "Fixture rejected this prompt"], status: 422)
            }
            if events.contains(where: { $0["client_event_id"] as? String == turnID && $0["event_type"] as? String == "user_message" }) {
                if parts.last == "turns" {
                    return response(["turn_id": turnID, "thread_revision": revision, "status": "completed"])
                }
                return response(thread)
            }
            // Like the server (`runtime.py`), a stored user message echoes the structured answer it carried
            // (`song_order` / `choice_selection`); that echo is what closes the order card.
            var userPayload: [String: Any] = [:]
            // KRI-588: a range answer is checked like the server does before anything is stored.
            if let refusal = songRangeRefusal(body["song_range"] as? [String: Any]) {
                return response(["problem": ["code": refusal.code, "message": refusal.message]], status: refusal.status)
            }
            if let range = body["song_range"] as? [String: Any] { userPayload["song_range"] = range }
            // KRI-901: checked like the server's `_stamp_protected_region` (the clip must be the thread's own, the
            // box at least 2% a side), then stored with a server generation.
            if let region = body["protected_region"] as? [String: Any] {
                let box = (region["box"] as? [Double]) ?? []
                if !["fixture-clip", "fixture-clip-2"].contains(region["media_id"] as? String ?? "") {
                    return response(["problem": ["code": "protected_region_media_not_found", "message": "That clip isn't part of this video. Mark the object on one of your clips."]], status: 404)
                }
                let rejectFirst = ProcessInfo.processInfo.environment["KRIA_CHAT_MARK_OBJECT"] == "reject" && !markRejected
                if box.count != 4 || box.contains(where: { !(0...1).contains($0) }) || box[2] - box[0] < 0.02 || box[3] - box[1] < 0.02 || rejectFirst {
                    markRejected = true
                    return response(["problem": ["code": "protected_region_invalid", "message": "That mark is too small or outside the frame. Draw a box around the object."]], status: 422)
                }
                userPayload["protected_region"] = region.merging(["generation": "1"]) { $1 }
            }
            // KRI-723: checked like the server's `_validate_moment_order` before anything is stored.
            if let answer = body["moment_order"] as? [String: Any] {
                let mode = ProcessInfo.processInfo.environment["KRIA_CHAT_MOMENT_ORDER"] ?? ""
                func problem(_ status: Int, _ code: String, _ message: String) -> (Int, Data) {
                    response(["problem": ["code": code, "message": message, "phase": "accept", "retryable": false, "recovery": "refresh_replan"]], status: status)
                }
                guard let open = momentQuestionID, answer["question_id"] as? String == open else {
                    return problem(409, "moment_order_stale", "That moments question is no longer open. Refresh and answer the latest.")
                }
                if mode == "stale", !momentRefused {
                    // The take was looked at again: the server asked a new question this answer does not close.
                    momentRefused = true
                    append("assistant_response", text: "I found 1 of your 3 moments in the clip. Put them in order and show me where each one is, and I'll cut exactly those.",
                           payload: ["turn_value": "question", "moment_order_question": MomentOrderFixture.question(id: "moments-q-\(events.count)")])
                    momentQuestionID = "moments-q-\(events.count - 1)"
                    thread["state"] = state; thread["events"] = events; thread["revision"] = revision; threads[id] = thread
                    return problem(409, "moment_order_stale", "That moments question is no longer open. Refresh and answer the latest.")
                }
                let rows = answer["answers"] as? [[String: Any]] ?? []
                let keys = rows.compactMap { $0["moment_key"] as? String }
                if keys.count != rows.count || Set(keys).count != keys.count || Set(keys) != Set(MomentOrderFixture.keys) {
                    return problem(422, "moment_order_invalid", "Answer every moment I asked about, once each.")
                }
                var spans: [(Double, Double)] = []
                for row in rows where row["action"] as? String == "point" {
                    guard row["media_id"] as? String == "fixture-clip" else {
                        return problem(404, "moment_order_media_not_found", "That clip isn't part of this video anymore.")
                    }
                    guard let start = row["source_start_s"] as? Double, let end = row["source_end_s"] as? Double,
                          start >= 0, end <= MomentOrderFixture.duration + 0.1, end - start >= 0.3 - 1e-9 else {
                        return problem(422, "moment_order_invalid", "That moment is outside the clip or too short.")
                    }
                    if spans.contains(where: { start < $0.1 - 0.001 && end > $0.0 + 0.001 }) || (mode == "reject" && !momentRefused) {
                        momentRefused = true
                        return problem(422, "moment_order_overlap", "Two of your moments overlap. Move one of them.")
                    }
                    spans.append((start, end))
                }
                userPayload["moment_order"] = answer
                momentQuestionID = nil
            }
            if let order = body["song_order"] as? [String: Any] { userPayload["song_order"] = order }
            if let selection = body["choice_selection"] as? [String: Any] { userPayload["choice_selection"] = selection }
            if let scope = body["scope"] as? [String] {
                // Live plan & review: a scoped "Update video" turn is planned as editor operations on `scope` only.
                guard reviewEnabled, runtime == 2 else {
                    return response(["problem": ["code": "live_plan_review_unavailable", "message": "Not available."]], status: 404)
                }
                if scope.contains("post_caption") {
                    return response(["problem": ["code": "scope_section_unsupported", "message": "A post caption can't be changed here."]], status: 422)
                }
                if scope.isEmpty || Set(scope).count != scope.count || !Set(scope).isSubset(of: Set(ReviewPlanFixture.order)) {
                    return response(["problem": ["code": "scope_invalid", "message": "Invalid scope."]], status: 422)
                }
                userPayload["scope"] = scope
                if let edits = body["manual_edits"] { userPayload["manual_edits"] = edits }
            }
            // KRI-756: like the server, a stored user message keeps the chip tap it carried. `KRIA_SUGGESTION_CHIPS_ECHO=1`
            // answers with the received tap so the UI test can pin the wire shape (and that a plain send carries none).
            if let tap = body["suggestion"] as? [String: Any] { userPayload["suggestion"] = tap }
            append("user_message", role: "user", text: body["message"] as? String, payload: userPayload, clientEventID: turnID)
            if let scope = body["scope"] as? [String] {
                review.scopedTurn(id: id, initial: reviewInitialBlocks, scope: scope, edits: body["manual_edits"] as? [[String: Any]] ?? [], turnID: turnID)
                append("assistant_response", text: "Your draft is ready for review.", payload: ["turn_id": turnID, "turn_value": "response"])
                append("draft_applied", payload: ["turn_id": turnID, "draft_id": id])
                append("approval_requested", payload: ["approval_id": approvalID, "turn_id": turnID])
            } else if runtime == 2, ProcessInfo.processInfo.environment["KRIA_SUGGESTION_CHIPS_ECHO"] == "1" {
                let tap = body["suggestion"] as? [String: Any]
                append("assistant_response", text: "Got it. suggestion[\(tap?["set_id"] as? String ?? "none")/\(tap?["chip_id"] as? String ?? "none")]",
                       payload: ["turn_id": turnID, "turn_value": "response"])
            } else if runtime == 2, let replyFixture = ProcessInfo.processInfo.environment["KRIA_CHAT_REPLY_FIXTURE"] {
                // Reply-UX fixtures (options / receipts / receipt-single), shaped like the server's `assistant_response`.
                let text = body["message"] as? String ?? ""
                switch replyFixture {
                case "options":
                    if text.hasPrefix("About ") {
                        // Echo what the server received so the UI test can pin that a chip is sent as plain text.
                        append("assistant_response", text: "Got it. reply[\(text)]", payload: ["turn_id": turnID, "turn_value": "response"])
                    } else {
                        append("assistant_response", text: "The clips are about 1.3 seconds each right now. How short should they be: about 1 second, about 0.8 seconds, or about 0.5 seconds? Or tell me a number of seconds.",
                               payload: ["turn_id": turnID, "turn_value": "question",
                                         "options": ["About 1 second", "About 0.8 seconds", "About 0.5 seconds"]])
                    }
                default:
                    let single = replyFixture == "receipt-single"
                    // An older server (or a review turn): the same receipts with no `reply_lead` / `also_changed`.
                    let noLead = replyFixture == "receipts-no-lead"
                    let receipts: [[String: Any]] = [
                        ["requirement_id": "req-length", "status": "met", "verification": "checked",
                         "reason": "Set 11 clips to 0.8s; Video length 45.64s → 39.78s"],
                        ["requirement_id": "req-drone", "status": "not_possible", "reason": "None of your clips is aerial footage."],
                    ]
                    append("assistant_response",
                           text: single ? "Have a look: Make the clips about 0.8 seconds (I changed: Set 11 clips to 0.8s; Video length 45.64s → 39.78s)."
                                        : "- Couldn't: End on a drone shot. None of your clips is aerial footage.\n- Done: Make the clips about 0.8 seconds. Set 11 clips to 0.8s; Video length 45.64s → 39.78s.\nAlso changed: Retimed the title (0–4.3s → 3–7.3s).",
                           payload: ["turn_id": turnID, "turn_value": "response",
                                     "requirement_receipts": single ? [receipts[0]] : receipts]
                               .merging(noLead ? [:] : ["reply_lead": single ? "Done." : "I did 1 of your 2 asks.",
                                                        "also_changed": ["Retimed the title (0–4.3s → 3–7.3s)"]]) { $1 })
                }
            } else if runtime == 2, ProcessInfo.processInfo.environment["KRIA_CHAT_PLAN_BLOCKS_FOLLOWUP"] == "1",
               (thread["job"] as? [String: Any])?["status"] as? String == "ready" {
                editorTurnIDs[id] = turnID
                editorTurnTicks[id] = 0
                if thoughtsEnabled { thoughtRequestIDs[id] = turnID }
            } else if runtime == 2, ProcessInfo.processInfo.environment["KRIA_CHAT_MOMENT_ORDER"] != nil {
                if let answer = body["moment_order"] as? [String: Any] {
                    // Echo exactly what the server received, in the creator's order (the wire shape the UI test pins).
                    let rows = (answer["answers"] as? [[String: Any]] ?? []).map { row -> String in
                        let head = "\(row["moment_key"] ?? ""):\(row["action"] ?? "")"
                        guard let start = row["source_start_s"] as? Double, let end = row["source_end_s"] as? Double else { return head }
                        return head + ":\(row["media_id"] ?? ""):" + String(format: "%.2f-%.2f", start, end)
                    }
                    append("assistant_response", text: "Got it. moments[\(rows.joined(separator: "|"))]",
                           payload: ["turn_id": turnID, "turn_value": "response"])
                    append("draft_applied", payload: ["turn_id": turnID, "draft_id": id])
                    append("approval_requested", payload: ["approval_id": approvalID, "turn_id": turnID])
                } else {
                    let questionID = "moments-q-\(events.count)"
                    momentQuestionID = questionID
                    append("assistant_response", text: "I found 1 of your 3 moments in the clip. Put them in order and show me where each one is, and I'll cut exactly those.",
                           payload: ["turn_id": turnID, "turn_value": "question", "moment_order_question": MomentOrderFixture.question(id: questionID)])
                }
            } else if runtime == 2, ProcessInfo.processInfo.environment["KRIA_CHAT_MARK_OBJECT"] != nil {
                if let region = body["protected_region"] as? [String: Any] {
                    // Echo exactly what the server received (the wire shape the UI test pins), then offer approval.
                    let box = ((region["box"] as? [Double]) ?? []).map { String(format: "%.4f", $0) }.joined(separator: ",")
                    let at = String(format: "%.1f", region["source_t_s"] as? Double ?? -1)
                    append("assistant_response", text: "Got it. mark[\(region["label"] ?? "")|\(region["media_id"] ?? "")|\(at)|\(box)]",
                           payload: ["turn_id": turnID, "turn_value": "response"])
                    append("draft_applied", payload: ["turn_id": turnID, "draft_id": id])
                    append("approval_requested", payload: ["approval_id": approvalID, "turn_id": turnID])
                } else {
                    append("assistant_response", text: "Mark the plate on one frame so I can keep the text off it. Or tell me the text can go anywhere.", payload: [
                        "turn_id": turnID, "turn_value": "question",
                        "protected_region_question": ["question_id": "mark-q-\(events.count)", "label": "plate",
                                                      "requirement_ids": ["r6", "r7"],
                                                      "media_ids": ["fixture-clip", "fixture-clip-2"]] as [String: Any],
                    ])
                }
            } else if runtime == 2, ProcessInfo.processInfo.environment["KRIA_CHAT_SONG_RANGE_Q"] != nil {
                if let range = body["song_range"] as? [String: Any] {
                    // The server stores the range on the song, then the planner continues. Echo what it received.
                    let start = range["start_s"] as? Double, end = range["end_s"] as? Double
                    var media = state["media"] as? [[String: Any]] ?? []
                    if let songIndex = media.firstIndex(where: { $0["role"] as? String == "song" }) {
                        if let start, let end { media[songIndex]["song_range"] = ["start_s": start, "end_s": min(end, 90)] }
                        else { media[songIndex].removeValue(forKey: "song_range") }
                        state["media"] = media
                    }
                    let shown = start.flatMap { from in end.map { "\(Int(from))-\(Int($0))" } } ?? "whole"
                    append("assistant_response", text: "Got it. range[\(shown)] question[\(range["question_id"] ?? "")]")
                } else {
                    append("assistant_question", text: "Your song repeats its verses, and some of your clips could fit more than one place. Which part of the song did you film? Pick it below, or use the whole song.", payload: [
                        "turn_id": turnID, "turn_value": "question",
                        "song_range_question": ["question_id": "range-q-\(events.count)", "song_generation": 1, "song_duration_s": 90.0] as [String: Any],
                    ])
                }
            } else if runtime == 2, ProcessInfo.processInfo.environment["KRIA_CHAT_SONG_ORDER"] != nil {
                if let order = body["song_order"] as? [String: Any] {
                    // Echo what the server received so the UI test can pin the structured payload.
                    let ids = (order["ordered_media_ids"] as? [String] ?? []).joined(separator: "+")
                    var echo = "Got it. order[\(ids)] question[\(order["question_id"] ?? "")]"
                    if let placements = order["placements"] as? [[String: Any]] {
                        let list = placements.map { "\($0["media_id"] ?? "")@" + String(format: "%.1f", $0["delta_s"] as? Double ?? .nan) }
                        echo += " placements[\(list.joined(separator: ";"))]"
                    }
                    append("assistant_response", text: echo)
                } else {
                    let timeline = ProcessInfo.processInfo.environment["KRIA_CHAT_SONG_ORDER"] == "1"
                    append("assistant_question", text: "I couldn't place a few of your clips against the song. Check the order.", payload: [
                        "turn_id": turnID, "turn_value": "question",
                        "song_order_question": SongTimelineFixture.question(id: "song-q-\(events.count)", timeline: timeline),
                    ])
                }
            } else if runtime == 2, ProcessInfo.processInfo.environment["KRIA_CHAT_QUESTION_BATCH"] != nil {
                let choice = body["choice_selection"] as? [String: Any]
                let clips = body["clip_selection"] as? [String: Any]
                if choice != nil || clips != nil {
                    // Echo what the server received, and on which answer turn, so the UI test can pin one turn.
                    batchAnswerTurns += 1
                    let answers = (clips?["answers"] as? [[String: Any]] ?? []).map { "\($0["key"] ?? "")=" + ((($0["media_ids"] as? [String]) ?? []).joined(separator: "+")) }
                    append("assistant_response", text: "Got it. turns[\(batchAnswerTurns)] choice[\(choice?["option_key"] ?? "")] answers[\(answers.joined(separator: ";"))]")
                } else {
                    append("assistant_response", text: "I\u{2019}d try \u{201C}Lunch at Don Julio\u{201D}. Does this wording work?\nUse this wording \u{00B7} Revise it \u{00B7} Skip it\n\nI couldn't tell which of your clips show \"my friend in the red cap\". Tap the clips that do, or tell me there aren't any.\n\nAnswer both and I'll plan your edit.", payload: [
                        "turn_id": turnID, "turn_value": "question",
                        "choice_question": [
                            "version": 1, "question_id": "wording-q-\(events.count)", "conflict": "creative_copy",
                            "kind": "creative_copy_wording", "creative_target": "opening_title", "candidate": "Lunch at Don Julio",
                            "allow_free_text": true,
                            "options": [
                                ["key": "approve", "label": "Use this wording", "recommended": false],
                                ["key": "revise", "label": "Revise it", "recommended": false],
                                ["key": "cancel", "label": "Skip it", "recommended": false],
                            ],
                        ] as [String: Any],
                        "clip_question": [
                            "version": 1, "question_id": "clips-q-\(events.count)", "allow_none": true,
                            "categories": [
                                ["key": "red_cap", "label": "My friend in the red cap", "op": "order",
                                 "candidate_media_ids": ["fixture-clip", "fixture-clip-2", "fixture-clip-3"], "suggested_media_ids": []],
                            ],
                        ] as [String: Any],
                    ])
                }
            } else if runtime == 2, ProcessInfo.processInfo.environment["KRIA_CHAT_VOICEOVER_SCRIPT"] != nil {
                let message = body["message"] as? String ?? ""
                if message.hasPrefix("I recorded the voiceover") {
                    // Echo what the server received so the UI test can pin the continuation message.
                    append("assistant_response", text: "Got it. recorded[\(message)]")
                } else {
                    let script = VoiceoverScriptUITestFixture.text
                    append("assistant_response", text: "Here's a script written from what your clips show. Check before recording, I couldn't confirm these from your clips: \u{201C}1922\u{201D}. Record it with \u{201C}Record with this script\u{201D} or import your own voiceover, then I'll plan the narrated edit around it.\n\n\(script)", payload: [
                        "turn_id": turnID, "turn_value": "question",
                        "voiceover_script": [
                            "version": 1, "script_id": "vos-fixture", "revision": 1, "status": "draft",
                            "item_id": id, "language": "en", "continuation": "record_or_import",
                            "source_media_ids": ["fixture-clip"], "text": script,
                            "lines": VoiceoverScriptUITestFixture.lines,
                            "needs_check": [["phrase": "1922", "reason": "Not in your clips or your message."]],
                        ] as [String: Any],
                    ])
                }
            } else if runtime == 2, ProcessInfo.processInfo.environment["KRIA_CHAT_CHOICE_QUESTION"] != nil {
                if let selection = body["choice_selection"] as? [String: Any] {
                    // Echo what the server received so the UI test can pin the structured payload.
                    append("assistant_response", text: "Got it. choice[\(selection["option_key"] ?? "")] question[\(selection["question_id"] ?? "")]")
                } else {
                    append("assistant_response", text: "You asked for a chronological video and for the clips grouped by sport. Unfortunately your football and dodgeball clips were filmed mixed together, so I can't do both. Which do you prefer?\n1. Group by sport, chronological inside each sport (recommended)\n2. Keep it strictly chronological; sports may interleave\nTap an option, or tell me in your own words.", payload: [
                        "turn_id": turnID, "turn_value": "question",
                        "choice_question": [
                            "version": 1, "question_id": "choice-q-\(events.count)", "conflict": "order_vs_group", "allow_free_text": true,
                            "options": [
                                ["key": "group_first", "label": "Group by sport, chronological inside each sport", "recommended": true,
                                 "description": "Each sport plays as one block."],
                                ["key": "chronological", "label": "Keep it strictly chronological; sports may interleave", "recommended": false],
                            ],
                        ] as [String: Any],
                    ])
                }
            } else if runtime == 2, ProcessInfo.processInfo.environment["KRIA_CHAT_CLIP_QUESTION"] != nil {
                if let selection = body["clip_selection"] as? [String: Any] {
                    // Echo what the server received so the UI test can pin the structured payload.
                    let answers = (selection["answers"] as? [[String: Any]] ?? []).map { "\($0["key"] ?? "")=" + ((($0["media_ids"] as? [String]) ?? []).joined(separator: "+")) }
                    let none = (selection["none_keys"] as? [String] ?? []).joined(separator: "+")
                    append("assistant_response", text: "Got it. answers[\(answers.joined(separator: ";"))] none[\(none)] skipped[\(selection["skipped"] as? Bool ?? false)]")
                } else {
                    // "later-suggested" (Leverkusen M2): the first group has no suggestion and the second one has a
                    // partial one, so a pre-ticked group is one the creator has not looked at yet.
                    let laterSuggested = ProcessInfo.processInfo.environment["KRIA_CHAT_CLIP_QUESTION"] == "later-suggested"
                    append("assistant_response", text: "I couldn't verify any clips for dodgeball. Could you clarify?", payload: [
                        "turn_id": turnID, "turn_value": "question",
                        "clip_question": [
                            "version": 1, "question_id": "q-\(events.count)", "allow_none": true,
                            "categories": [
                                ["key": "dodgeball", "label": "Dodgeball", "op": "group",
                                 "candidate_media_ids": ["fixture-clip", "fixture-clip-2", "fixture-clip-3"],
                                 "suggested_media_ids": laterSuggested ? [] : ["fixture-clip-2"]],
                                ["key": "football", "label": "Football", "op": "group",
                                 "candidate_media_ids": ["fixture-clip-3", "fixture-clip-4"],
                                 "suggested_media_ids": laterSuggested ? ["fixture-clip-4"] : []],
                            ],
                        ] as [String: Any],
                    ])
                }
            } else if runtime == 2 {
                append("assistant_response", text: "Your draft is ready for review.")
                var draftPayload: [String: Any] = ["turn_id": turnID, "draft_id": id]
                // KRI-881: `KRIA_CHAT_RESEARCH_PREVIEW=1` sends the server's `research_preview` (shape of
                // `research_preview.preview_payload`, plus a key this client does not know and an unusable row);
                // `KRIA_CHAT_REPLY_LANGUAGE=tr` makes the chat Turkish.
                if ProcessInfo.processInfo.environment["KRIA_CHAT_RESEARCH_PREVIEW"] == "1" {
                    draftPayload["research_preview"] = Self.researchPreviewFixture
                }
                if let language = ProcessInfo.processInfo.environment["KRIA_CHAT_REPLY_LANGUAGE"] { state["reply_language"] = language }
                append("draft_applied", payload: draftPayload)
                append("approval_requested", payload: ["approval_id": approvalID, "turn_id": turnID])
            }
            else {
                let version = planVersions[id, default: 0] + 1
                planVersions[id] = version
                thread["creator_agent"] = ["status": "awaiting_confirmation", "summary": "Open on the laugh and keep the pacing quick.", "version": version, "plan_hash": "fixture-plan-\(version)"]
                append("assistant_strategy", payload: [
                    "proposal_summary": "Open on the laugh and keep the pacing quick.",
                    "plan_hash": "fixture-plan-\(version)"
                ])
            }
        } else if parts.contains("approvals") {
            if parts.last == "approve", reviewEnabled, review.hasPending(id) {
                // The creator tapped Update video / Undo: the pending review draft starts a NEW job.
                let approved = events.last(where: { $0["event_type"] as? String == "approval_requested" })?["payload"] as? [String: Any]
                let turn = approved?["turn_id"] as? String ?? id
                if let started = review.approve(id: id, turnID: turn) {
                    append("approval_approved")
                    thread["active_job_id"] = started.jobID
                    thread["job"] = ["id": started.jobID, "status": "processing", "variants": []]
                    append("plan_block", role: "system", payload: started.event)
                }
            } else if parts.last == "approve", ProcessInfo.processInfo.environment["KRIA_CHAT_SPEECH_CLEANUP"] != nil {
                // The real POST writes only `approval_approved` (`render_dispatched: false`); the job, `render_queued`
                // and the first plan block land from the agent-control task a moment later (see `approvalDispatches`).
                let approved = events.last(where: { $0["event_type"] as? String == "approval_requested" })?["payload"] as? [String: Any]
                append("approval_approved", payload: ["approval_id": approvalID, "turn_id": approved?["turn_id"] as? String ?? id, "render_dispatched": false])
                approvalDispatches[id] = 0
                planTurnIDs[id] = approved?["turn_id"] as? String ?? id
            } else if parts.last == "approve" {
                if let mismatch = renderShapeMismatch(body) { return response(["detail": mismatch], status: 422) }
                let approved = events.last(where: { $0["event_type"] as? String == "approval_requested" })?["payload"] as? [String: Any]
                append("approval_approved")
                thread["active_job_id"] = id
                if DeviceRenderUITestFixture.scenario != nil {
                    // KRIA_CHAT_PLAN_BLOCKS_DEVICE: the server only plans; the variant is handed to this iPhone.
                    thread["job"] = ["id": id, "status": "processing", "variants": [["variant_id": DeviceRenderUITestFixture.variantID, "render_status": "awaiting_device", "render_destination": "device"]]]
                } else {
                    thread["job"] = ["id": id, "status": "processing", "variants": []]
                    renders[id] = 0
                }
                if ProcessInfo.processInfo.environment["KRIA_CHAT_PLAN_BLOCKS"] == "1" {
                    planStages[id] = 0; planTicks[id] = 0
                    planTurnIDs[id] = approved?["turn_id"] as? String ?? id
                }
            } else {
                let approvalPayload = events.last(where: { $0["event_type"] as? String == "approval_requested" })?["payload"] as? [String: Any]
                let turnID = approvalPayload?["turn_id"] as? String ?? id
                return response(["approval_id": approvalID, "turn_id": turnID, "draft_id": id, "draft_revision": 1, "status": "pending", "consequence_summary": "Open on the laugh and keep the pacing quick.", "expires_at": ProcessInfo.processInfo.environment["KRIA_CHAT_EXPIRED_APPROVAL"] == "1" ? "2000-09-10T10:00:00Z" : "2099-09-10T10:00:00Z", "approval_fingerprint": String(repeating: "a", count: 64)])
            }
        } else if parts.last == "delta", let count = approvalDispatches[id] {
            // Ticks on the delta poll: like the server, nothing here bumps the revision until `render_queued`, so
            // the client keeps polling deltas and only re-reads the full projection once that event lands.
            approvalDispatches[id] = count + 1
            if count == 1 {
                // State B: the claim committed -- session executing, no new event, same revision.
                thread["creator_agent"] = ["status": "executing", "summary": "Open on the laugh and keep the pacing quick."]
            } else if count >= 2 {
                // State C: `_finish_approval_dispatch` -- job + render_queued + all-waiting plan_block in one commit.
                approvalDispatches.removeValue(forKey: id)
                thread["active_job_id"] = id
                if DeviceRenderUITestFixture.scenario != nil {
                    // A phone project: the server only plans; the variant is handed to this iPhone.
                    thread["job"] = ["id": id, "status": "processing", "variants": [["variant_id": DeviceRenderUITestFixture.variantID, "render_status": "awaiting_device", "render_destination": "device"]]]
                } else {
                    thread["job"] = ["id": id, "status": "processing", "variants": []]
                    renders[id] = 0
                }
                append("render_queued", role: "system", payload: ["job_id": id, "turn_id": planTurnIDs[id] ?? id])
                if ProcessInfo.processInfo.environment["KRIA_CHAT_PLAN_BLOCKS"] == "1" {
                    planStages[id] = 0; planTicks[id] = 0
                }
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
            if count >= 2, (planStages[id] ?? Self.planScript.count + 8) >= Self.planScript.count + 8 {
                thread["job"] = ["id": id, "status": "ready", "variants": [["variant_id": "original_text", "render_status": "ready", "output_url": "https://fixture.invalid/result.mp4"]]]
                renders[id] = nil
                append("generation_ready")
                if ProcessInfo.processInfo.environment["KRIA_CHAT_FIXTURE_BRIEF"] == "1" {
                    // What the server sends after a render: the reply plus one receipt per requirement.
                    append("assistant_review", text: "The cut is ready. I did most of what you asked; one thing needs your call.",
                           payload: ["turn_id": id, "turn_value": "review", "requirement_receipts": Self.fixtureReceipts,
                                     "reply_lead": "Partly done.", "also_changed": ["Retimed the title (0–4.3s → 3–7.3s)"]])
                }
            }
        }
        if parts.last == "delta", let stage = planStages[id] {
            // One scripted step every second poll so a UI test can observe each state.
            planTicks[id, default: 0] += 1
            if planTicks[id, default: 0].isMultiple(of: 2) {
                if stage < Self.planScript.count {
                    append("plan_block", role: "system", payload: ["turn_id": planTurnIDs[id] ?? id, "job_id": id, "blocks": Self.planScript[stage]])
                }
                planStages[id] = stage + 1
            }
        }
        if parts.last == "delta", let tick = editorTurnTicks[id], let turnID = editorTurnIDs[id] {
            if tick == 0 {
                let oldTurnID = planTurnIDs[id] ?? id
                append("plan_block", role: "system", payload: ["turn_id": oldTurnID, "job_id": id, "blocks": Self.planScript.last ?? []])
                append("assistant_review", text: "Late prior render event", payload: ["turn_id": oldTurnID])
            }
            // `KRIA_CHAT_THOUGHTS=hold` never finishes the turn, so a UI test can inspect the live (streaming) thought.
            if tick >= 6, ProcessInfo.processInfo.environment["KRIA_CHAT_THOUGHTS"] != "hold" {
                append("assistant_response", text: "Review the Lisbon title edit before saving.", payload: ["turn_id": turnID])
                append("draft_applied", payload: ["turn_id": turnID, "draft_id": id])
                editorTurnTicks[id] = nil
            } else { editorTurnTicks[id] = tick + 1 }
        }
        thread["state"] = state; thread["events"] = events; thread["revision"] = revision; threads[id] = thread
        if reviewEnabled, parts.count == 3, parts[2] == "plan" {
            // First render still deciding = planning; once the staged feed has finished, the plan is ready to review.
            let feedDone = (planStages[id] ?? Self.planScript.count + 8) >= Self.planScript.count
                && (thread["job"] as? [String: Any])?["id"] != nil
            return response(review.snapshot(id: id, initial: reviewInitialBlocks, status: feedDone ? "ready" : "planning", eventCount: events.count))
        }
        if parts.last == "delta" {
            let after = URLComponents(url: request.url!, resolvingAgainstBaseURL: false)?.queryItems?.first(where: { $0.name == "after_sequence" })?.value.flatMap(Int.init) ?? -1
            return response(["thread_id": id, "runtime_version": runtime, "status": "active", "after_sequence": after, "has_more": false, "thread_revision": revision, "events": events.filter { ($0["sequence"] as? Int ?? 0) > after }, "next_after_sequence": events.count - 1])
        }
        if request.httpMethod == "POST", ["turns", "messages"].contains(parts.last ?? ""),
           ProcessInfo.processInfo.environment["KRIA_CHAT_SEND_FAILURE"] == "lost-response", !sendRefused {
            sendRefused = true
            return nil
        }
        if parts.last == "turns" {
            // Like the server (`runtime.py`), the accepted turn id is the one stamped on that turn's events
            // (the client event id), so `ChatThinkingSettlement` can match the reply to the wait.
            let acceptedTurnID = editorTurnIDs[id] ?? (body["client_event_id"] as? String ?? id)
            return response(["turn_id": acceptedTurnID, "thread_revision": revision, "status": "queued"], status: 202)
        }
        if parts.last == "approve" { return response(["approval_id": approvalID, "thread_id": id, "status": "approved", "thread_revision": revision]) }
        return response(withSpeechCleanup(withRenderShape(thread)))
    }
    /// The first job's final decided block per section, from the staged feed script (contract v2).
    private func reviewInitialBlocks() -> [String: [String: Any]] {
        var result: [String: [String: Any]] = [:]
        for stage in Self.planScript {
            for block in stage where block["state"] as? String == "decided" {
                if let section = block["section_id"] as? String { result[section] = block }
            }
        }
        return result
    }

    /// KRI-443: the staged `plan_block` payloads, in the order the pipeline would decide them.
    private static var planScript: [[[String: Any]]] {
        let v2 = (ProcessInfo.processInfo.environment["KRIA_CHAT_PLAN_BLOCKS_VERSION"].flatMap(Int.init) ?? 1) >= 2
        func block(_ section: String, _ state: String, _ summary: String? = nil, detail: String? = nil, skipped: Bool = false, payload: Any? = nil) -> [String: Any] {
            var value: [String: Any] = ["section_id": section, "state": state, "intent": false, "skipped": skipped]
            if let summary { value["summary"] = summary }
            if let detail { value["detail"] = detail }
            if state == "decided" { value["decided_at"] = "2026-10-05T10:00:00Z" }
            if v2 {
                value["revision"] = state == "decided" ? 1 : 0
                value["changed"] = false
                if let payload { value["payload"] = payload }
            }
            return value
        }
        var sections = ["title", "clips", "captions", "music", "sfx", "overlays", "look"]
        if v2 { sections.append("post_caption") }
        if ProcessInfo.processInfo.environment["KRIA_CHAT_PLAN_BLOCKS_DEVICE"] == "1" {
            // What an iPhone account gets in production: every section waiting, then every section decided
            // almost at once, because the server only plans and the video is built on the device.
            var decided = [block("title", "decided", "Sunday reset, slowed down"), block("clips", "decided", "30 clips · 30s"),
                           block("captions", "decided", "Bold captions, lower third"), block("music", "decided", "Your song"),
                           block("sfx", "decided", "4 sound effects"), block("overlays", "decided", nil, skipped: true),
                           block("look", "decided", "Warm film grain")]
            if v2 { decided.append(block("post_caption", "decided", "Slow summer days in Bodrum")) }
            return [sections.map { block($0, "waiting") }, decided]
        }
        if v2 {
            let clips: [[String: Any]] = (0..<6).map { index in
                var clip: [String: Any] = ["index": index, "kind": "video", "role": ["kettle", "street", "bakery", "bench", "harbor", "sunset"][index],
                                           "start_s": Double(index) * 4, "end_s": Double(index + 1) * 4]
                if index < 5 { clip["transition"] = ["cut", "dissolve", "whip", "fade", "banana"][index] }
                return clip
            }
            return [
                sections.map { block($0, "waiting") },
                [block("title", "deciding")],
                [block("title", "decided", "A slow summer day", payload: ["text": "A slow summer day", "bar_id": "guided-title-1"]), block("clips", "deciding")],
                [block("clips", "decided", "6 clips · 24s · beach first", payload: ["total_duration_s": 24.0, "clips": clips])],
                [block("captions", "decided", "4 lines · pop-in · bottom", payload: ["count": 4, "truncated": false, "lines": [
                    ["id": "c1", "kind": "cue", "text": "Slow mornings.", "start_s": 0.0, "end_s": 2.5],
                    ["id": "c2", "kind": "cue", "text": "Salt, sun, sand.", "start_s": 3.0, "end_s": 5.5],
                    ["id": "c3", "kind": "cue", "text": "Golden hour.", "start_s": 11.0, "end_s": 13.0],
                    ["id": "c4", "kind": "cue", "text": "Stay a little longer.", "start_s": 18.0, "end_s": 21.0],
                ] as [[String: Any]]]), block("music", "deciding")],
                [block("music", "decided", "Sunday Drive by Kira", payload: ["source": "catalog", "title": "Sunday Drive", "artist": "Kira", "bpm": 112.0, "start_s": 12.0,
                                                                               "mix": ["music_level": 0.7, "original_level": 0.5]]), block("sfx", "deciding")],
                // A malformed payload must fall back to the summary, never break the feed.
                [block("sfx", "decided", "4 sound effects", payload: "not-an-object"), block("overlays", "decided", nil, skipped: true), block("look", "deciding")],
                [block("look", "decided", "Warm film · light grain", payload: ["chips": ["Warm film", "Light grain", "Serif titles"]]), block("post_caption", "deciding")],
                [block("post_caption", "decided", "Slow summer days in Bodrum", payload: ["text": "Slow summer days in Bodrum, one roll of film at a time.", "hashtags": ["slowsummer", "bodrum"], "platform": "tiktok"])],
            ]
        }
        return [
            sections.map { block($0, "waiting") },
            [block("title", "deciding")],
            [block("title", "decided", "Sunday reset, slowed down", detail: "Opens on the kettle, then the walk."), block("clips", "deciding")],
            [block("clips", "decided", "6 clips · 18s", detail: "Kettle, street, bakery, bench, harbor, sunset.")],
            [block("captions", "decided", "Bold captions, lower third"), block("music", "deciding")],
            [block("music", "decided", "Golden Hour by Kira"), block("sfx", "deciding")],
            [block("sfx", "decided", "4 sound effects"), block("overlays", "decided", nil, skipped: true), block("look", "deciding")],
            [block("look", "decided", "Warm film grain")],
        ]
    }

    /// `KRIA_CHAT_SPEECH_CLEANUP=choice`: the speech check is ready and the creator must choose "Clean up speech
    /// and create" or "Keep original speech and create" on the pending approval card (the tall card a real
    /// subtitled project shows). Gone once a job exists, like the server's projection.
    private func withSpeechCleanup(_ thread: [String: Any]) -> [String: Any] {
        guard ProcessInfo.processInfo.environment["KRIA_CHAT_SPEECH_CLEANUP"] == "choice", thread["active_job_id"] == nil else { return thread }
        var result = thread
        result["speech_cleanup"] = [
            "applicable": true, "requires_choice": true,
            "analysis": ["id": "cccccccc-cccc-cccc-cccc-cccccccccccc", "status": "ready", "candidate_count": 13,
                         "estimated_removed_ms": 8765, "category_counts": ["filler_sounds": 9, "long_pauses": 4, "retakes": 0]],
        ] as [String: Any]
        return result
    }
    /// KRI-306 fixture (`KRIA_CHAT_RENDER_SHAPE=1`): the server offers Vertical / Landscape and
    /// Black bars / Crop on a pending approval or plan, and nothing once a job exists.
    private func withRenderShape(_ thread: [String: Any]) -> [String: Any] {
        guard ProcessInfo.processInfo.environment["KRIA_CHAT_RENDER_SHAPE"] == "1", thread["active_job_id"] == nil else { return thread }
        var result = thread
        result["render_shape"] = ["orientations": ["portrait", "landscape"], "fit_choices": ["fit", "fill"],
                                  "default": ["output_orientation": "portrait", "landscape_fit": "fit"]] as [String: Any]
        return result
    }
    /// `KRIA_CHAT_RENDER_SHAPE_EXPECT` is "<orientation>/<fit>" with "none" for a key the client must NOT
    /// send. A mismatch is answered with a 422 so the flow never reaches "Open editor": the UI test
    /// asserts what the app sent by whether the render starts.
    private func renderShapeMismatch(_ body: [String: Any]) -> String? {
        guard let expected = ProcessInfo.processInfo.environment["KRIA_CHAT_RENDER_SHAPE_EXPECT"] else { return nil }
        let actual = "\(body["output_orientation"] as? String ?? "none")/\(body["landscape_fit"] as? String ?? "none")"
        return actual == expected ? nil : "Unexpected render shape \(actual), expected \(expected)"
    }
    /// KRI-207 fixture (`KRIA_CHAT_FIXTURE_BRIEF=1`): invented names, one receipt of every kind. The
    /// first guess carries its clip; the second only the plain `inferred` string an older server sends.
    private static var fixtureReceipts: [[String: Any]] { [
        ["requirement_id": "req-labels", "status": "met", "reason": "Every clip has its own place name.",
         "inferred": ["Harbor Point"],
         "inferred_labels": [["text": "Harbor Point", "media_id": "fixture-clip", "clip_index": 3]]],
        ["requirement_id": "req-order", "status": "partial",
         "reason": "Your clips were filmed from the lighthouse to the pier, the reverse of the route you gave. I kept filming order; tell me if you want your route order instead.",
         "inferred": ["Old Lighthouse"]],
        ["requirement_id": "req-drone", "status": "not_possible", "reason": "None of your clips is aerial footage."],
    ] }

    private static func generatedNarrationFixtureURL() throws -> URL {
        let url = FileManager.default.temporaryDirectory.appendingPathComponent("generated-narration-fixture.wav")
        if FileManager.default.fileExists(atPath: url.path) { return url }
        let format = AVAudioFormat(standardFormatWithSampleRate: 22_050, channels: 1)!
        let buffer = AVAudioPCMBuffer(pcmFormat: format, frameCapacity: 22_050)!
        buffer.frameLength = buffer.frameCapacity
        for index in 0..<Int(buffer.frameLength) {
            buffer.floatChannelData![0][index] = Float(sin(Double(index) * 2 * .pi * 440 / 22_050) * 0.1)
        }
        let file = try AVAudioFile(forWriting: url, settings: format.settings)
        try file.write(from: buffer)
        return url
    }

    /// KRI-282: a thread shaped like the production Olympics thread -- 48 phone analysis-proxy clips
    /// (`analysis-proxy-ios-<UUID>.mp4` media ids), a long history, and the NEWEST event a clip question with
    /// no suggestions. Ids, labels and prose are synthetic; only the shape is real.
    static let realShapeMediaIDs = (1...48).map { String(format: "analysis-proxy-ios-%08X-0000-4000-8000-%012X.mp4", $0, $0) }

    /// `KRIA_CHAT_CLIP_THUMBS=1`: the first 24 of those clips have a cached thumbnail (a numbered gradient) and
    /// the rest do not, so a screenshot shows both a poster and the labelled placeholder.
    @MainActor static func seedClipThumbnails() {
        guard ProcessInfo.processInfo.environment["KRIA_CHAT_CLIP_THUMBS"] == "1" else { return }
        let hues: [UIColor] = [.systemTeal, .systemOrange, .systemIndigo, .systemGreen, .systemPink, .systemBrown]
        for (index, id) in realShapeMediaIDs.prefix(24).enumerated() {
            let size = CGSize(width: 180, height: 320)
            let image = UIGraphicsImageRenderer(size: size).image { context in
                let colors = [hues[index % hues.count].cgColor, hues[(index + 2) % hues.count].withAlphaComponent(0.5).cgColor]
                let gradient = CGGradient(colorsSpace: CGColorSpaceCreateDeviceRGB(), colors: colors as CFArray, locations: [0, 1])!
                context.cgContext.drawLinearGradient(gradient, start: .zero, end: CGPoint(x: size.width, y: size.height), options: [])
            }
            guard let data = image.jpegData(compressionQuality: 0.8) else { continue }
            let url = CreationMediaPreview.url(mediaID: id)
            try? FileManager.default.createDirectory(at: url.deletingLastPathComponent(), withIntermediateDirectories: true)
            try? data.write(to: url, options: .atomic)
        }
    }

    /// KRI-693: the Visuals photo the `photo` fixture's picker offers next to three chat clips.
    static let poolPhotoAssetID = "d9627a79-b228-4db0-9cbb-201f6f921bb2"

    /// `poolPhoto`: three chat clips plus one Visuals photo candidate, named by the server in Turkish.
    static func realShapeClipQuestionHistory(poolPhoto: Bool = false) -> (events: [[String: Any]], state: [String: Any]) {
        let ids = poolPhoto ? Array(realShapeMediaIDs.prefix(3)) : realShapeMediaIDs
        let candidates = poolPhoto ? ids + ["asset-\(poolPhotoAssetID)"] : ids
        var events: [[String: Any]] = []
        func append(_ type: String, role: String, content: String = "", payload: [String: Any] = [:]) {
            events.append(["id": UUID().uuidString, "sequence": events.count, "revision": events.count + 1, "role": role,
                           "event_type": type, "content": content, "payload": payload, "created_at": "2026-10-04T10:00:00Z"])
        }
        append("action_select_format", role: "user", payload: ["action": "select_format", "format": "montage"])
        var media: [[String: Any]] = []
        for (index, id) in ids.enumerated() {
            let clip: [String: Any] = ["kind": "video", "filename": "IMG_\(1000 + index).mov", "media_id": id, "duration_s": 7.3,
                                       "content_type": "video/mp4", "upload_contract": ["purpose": "analysis_proxy"]]
            media.append(clip)
            append("media_added", role: "user", payload: ["media": [clip], "media_count": index + 1])
        }
        append("user_message", role: "user", content: "Make a day vlog of the tournament, grouped by sport.",
               payload: ["turn_id": "t1", "turn_status": "accepted", "runtime_version": 2])
        let category: [String: Any] = ["op": "group", "key": "group:dodgeball", "label": "dodgeball",
                                       "candidate_media_ids": candidates, "suggested_media_ids": [String]()]
        var clipQuestion: [String: Any] = ["version": 1, "allow_none": true, "question_id": "real-shape-q", "categories": [category]]
        if poolPhoto {
            clipQuestion["candidate_media"] = [["media_id": "asset-\(poolPhotoAssetID)", "kind": "image", "label": "Fotoğraf 1"]]
        }
        append("assistant_response", role: "assistant",
               content: "I couldn't tell which of your clips show \"dodgeball\". Tap the clips that do, or tell me there aren't any.",
               payload: ["turn_id": "t1", "turn_value": "question", "receipt_ids": [String](), "next_actions": [String](), "schema_version": 2,
                         "clip_question": clipQuestion])
        return (events, ["format": "montage", "media": media])
    }

    private static func fixtureBrief(threadID: String) -> [String: Any] {
        ["thread_id": threadID, "version": 1,
         "requirements": [
            ["id": "req-labels", "kind": "text", "scope": "per_clip", "description": "Label each clip with its place", "status": "met"],
            ["id": "req-length", "kind": "duration", "scope": "per_clip", "description": "Make the clips about 0.8 seconds", "status": "met"],
            ["id": "req-order", "kind": "order", "scope": "route", "description": "Follow the pier-to-lighthouse route", "status": "partial"],
            ["id": "req-drone", "kind": "select", "scope": "clip", "description": "End on a drone shot", "status": "not_possible"],
         ],
         "requirement_receipts": fixtureReceipts]
    }

    /// Slide fixture is explicitly test-only; production exercises the real
    /// proposal, versioned save, and version-approved dispatch endpoints.
    private func slideResponse(_ request: URLRequest, itemID: String, parts: [String], body: [String: Any]) -> (Int, Data) {
        func response(_ object: Any, status: Int = 200) -> (Int, Data) { (status, (try? JSONSerialization.data(withJSONObject: object)) ?? Data()) }
        // `KRIA_SLIDE_POST_MANY=1`: twelve slides, so the strip overflows and auto-scroll can be exercised.
        let base = [("trulli-street", "jpg", "image"), ("istanbul", "mp4", "video"), ("lisbon", "jpg", "image")]
        let media = ProcessInfo.processInfo.environment["KRIA_SLIDE_POST_MANY"] == "1" ? Array(repeating: base, count: 4).flatMap { $0 } : base
        var assets: [[String: Any]] = media.enumerated().map { index, media in
            var url = Bundle.main.url(forResource: media.0, withExtension: media.1)?.absoluteString ?? ""
            var poster = media.2 == "video" ? Bundle.main.url(forResource: "trulli-street", withExtension: "jpg")!.absoluteString : url
            // `KRIA_SLIDE_POST_BROKEN_VIDEO=1`: the video's file is a photo, so its preview fails to play (KRI-644).
            if media.2 == "video", ProcessInfo.processInfo.environment["KRIA_SLIDE_POST_BROKEN_VIDEO"] == "1" { url = poster }
            // `KRIA_SLIDE_POST_REMOTE_MEDIA=1`: photos look remote (slow, re-signed on every response) so the image
            // cache is exercised: same asset, new `sig` each time. Videos keep their local source for AVPlayer.
            if ProcessInfo.processInfo.environment["KRIA_SLIDE_POST_REMOTE_MEDIA"] == "1" {
                let signed = { (name: String, ext: String) in "https://fixture.invalid/\(name).\(ext)?sig=\(UUID().uuidString)" }
                if media.2 == "image" { url = signed(media.0, media.1); poster = url } else { poster = signed("trulli-street", "jpg") }
            }
            return ["id": "asset-\(index)", "kind": media.2, "status": "ready", "media_status": "available", "source_filename": "\(media.0).\(media.1)", "source_url": url, "display_url": url, "preview_url": poster, "duration_s": 8]
        }
        let slides: [[String: Any]] = assets.enumerated().map { index, asset in ["id": "slide-\(index)", "asset_id": asset["id"]!, "kind": asset["kind"]!] }
        // Pasted pictures sit in the pool but are never part of the starting slides.
        assets += slideStickerAssets[itemID] ?? []
        // `KRIA_SLIDE_POST_READY_DRAFT=1`: the READY post (`KRIA_SLIDE_POST_READY_THREAD`) already has a saved,
        // rendered server draft, like a post opened from the gallery in production.
        if ProcessInfo.processInfo.environment["KRIA_SLIDE_POST_READY_DRAFT"] == "1",
           itemID.uppercased() == "BBBBBBBB-BBBB-BBBB-BBBB-BBBBBBBBBBBB", slideDrafts[itemID] == nil {
            slideDrafts[itemID] = ["schema_version": 1, "version": 1, "platform_profile": "instagram_carousel", "slides": slides, "cover_index": 0,
                                   "caption": "Three moments, one story.", "user_edited": true, "rendered_version": 1]
            slideRendered.insert(itemID)
        }
        // A sticker upload (KRI-460): reserve a slot, then register the picture; both answer like the server.
        if parts.last == "upload-urls", request.httpMethod == "POST" {
            return response(["urls": [["reservation_id": UUID().uuidString, "upload_url": "https://fixture.invalid/upload", "gcs_path": "fixture/sticker", "upload_headers": [String: String]()]]])
        }
        if parts.last == "assets", request.httpMethod == "POST" {
            let name = body["source_filename"] as? String ?? "pasted.png"
            let location = body["gcs_path"] as? String ?? ""
            let stickerAsset: [String: Any] = ["id": "sticker-asset-\(slideStickerAssets[itemID, default: []].count + 1)", "kind": "image", "status": "ready", "media_status": "available",
                                              "source_filename": name, "source_url": location, "display_url": location, "preview_url": location, "duration_s": 0, "retryable": false]
            slideStickerAssets[itemID, default: []].append(stickerAsset)
            return response(stickerAsset, status: 201)
        }
        func lateAsset(status: String) -> [String: Any] {
            let url = Bundle.main.url(forResource: "trulli-street", withExtension: "jpg")?.absoluteString ?? ""
            return ["id": "asset-3", "kind": "image", "status": status, "media_status": "available", "source_filename": "late.jpg", "source_url": url, "display_url": url, "preview_url": url, "duration_s": 0]
        }
        if parts.last == "assets" { return response(["assets": assets, "max_assets": 20]) }
        if parts.last == "propose" {
            var draft = slideDrafts[itemID] ?? ["schema_version": 1, "version": 1, "platform_profile": "instagram_carousel", "slides": slides, "cover_index": 0, "caption": "Three moments, one story.", "user_edited": false]
            let base = slideDrafts[itemID]?["version"] as? Int ?? 0
            draft["version"] = base + 1
            if base > 0 { draft["caption"] = "A slower day, kept together." }
            return response(["draft": draft, "base_version": base, "fallback_used": false, "summary": "Open on the quiet street, move through the city, and close on the view."])
        }
        if parts.last == "chat-edit" {
            // KRI-298 Lane E stub: reverse the slides and label the first two with a place. The editor's
            // own draft wins when sent; `base_version` is the stored version the next PUT must quote.
            let stored = slideDrafts[itemID]
            // Mirrors the server's `SlidePostDraft.version: ge=1`: the first AI message on a brand-new post carries
            // a never-saved draft, and an invalid one is a 422 (prod 2026-10-05).
            if let sent = body["draft"] as? [String: Any], (sent["version"] as? Int ?? 0) < 1 {
                return response(["detail": [["type": "greater_than_equal", "loc": ["body", "draft", "version"], "msg": "Input should be greater than or equal to 1"]]], status: 422)
            }
            // `KRIA_SLIDE_POST_CHAT_EDIT_FAIL_ONCE=1`: the first request fails with a server error, a retry works.
            if ProcessInfo.processInfo.environment["KRIA_SLIDE_POST_CHAT_EDIT_FAIL_ONCE"] == "1", !chatEditFailed {
                chatEditFailed = true
                return response(["detail": "boom"], status: 500)
            }
            var draft = (body["draft"] as? [String: Any]) ?? stored ?? ["schema_version": 1, "platform_profile": "instagram_carousel", "slides": slides, "cover_index": 0, "caption": "", "user_edited": false]
            let base = stored?["version"] as? Int ?? 0
            guard var edited = (draft["slides"] as? [[String: Any]])?.reversed().map({ $0 }) else { return response(["detail": "no draft"], status: 409) }
            let places = ["Athens", "Naxos"]
            for index in edited.indices {
                let label: [String: Any] = ["id": "label-\(edited[index]["id"] ?? index)", "text": index < places.count ? places[index] : "", "role": "label", "label_source": "place", "edited": false]
                if index < places.count { edited[index]["edits"] = ["texts": [label], "look_preset": "none"] }
            }
            draft["slides"] = edited; draft["cover_index"] = 0; draft["version"] = 999
            return response(["outcome": "edited", "reply": "Done. Your photos now follow the order you took them, with the place on each slide in the same style. One photo had no location, so I left it without a label.", "draft": draft, "base_version": base, "changes": ["Reordered \(edited.count)", "Location on \(places.count)", "1 photo has no location"], "suggestions": []])
        }
        if request.httpMethod == "PUT" {
            let version = slideDrafts[itemID]?["version"] as? Int ?? 0
            guard body["expected_version"] as? Int == version else { return response(["detail": "stale"], status: 409) }
            var draft = body; draft.removeValue(forKey: "expected_version")
            draft["schema_version"] = 1; draft["version"] = version + 1; draft["user_edited"] = true
            if slideRendered.contains(itemID) { draft["rendered_version"] = version + 1 }
            slideDrafts[itemID] = draft
            return response(["slide_post": draft])
        }
        if parts.last == "generate" {
            slideRendered.insert(itemID)
            let version = slideDrafts[itemID]?["version"]
            slideDrafts[itemID]?["rendered_version"] = version
            return response(["slide_post": slideDrafts[itemID] ?? [:]])
        }
        if ProcessInfo.processInfo.environment["KRIA_SLIDE_POST_LATE_ASSET"] == "1", slideDrafts[itemID] != nil {
            let polls = lateAssetPolls[itemID, default: 0] + 1
            lateAssetPolls[itemID] = polls
            assets.append(lateAsset(status: polls <= 2 ? "processing" : "ready"))
        }
        var state: [String: Any] = ["schema_version": 1, "item_id": itemID, "title": "Three connected moments", "assets": assets, "render_status": "not_rendered", "slides": [], "validation_errors": []]
        if let draft = slideDrafts[itemID] { state["draft"] = draft }
        if slideRendered.contains(itemID), let draft = slideDrafts[itemID] {
            state["job_id"] = itemID; state["render_status"] = "ready"; state["rendered_version"] = draft["version"]
            state["slides"] = (draft["slides"] as? [[String: Any]] ?? []).map { ref -> [String: Any] in
                var result = ref; result["url"] = "https://fixture.invalid/\(ref["id"]!).jpg"; return result
            }
        }
        return response(state)
    }

    /// KRI-141 device-render fixture (`KRIA_CHAT_DEVICE_RENDER`). `ready`: the server waits on
    /// this iPhone. `unsupported_recipe` / `timed_out`: the server already gave up with that
    /// `reason_code`; `/device-render/retry` mints revision 2, which then waits on the iPhone.
    private func deviceRenderResponse(scenario: String, jobID: UUID, route: String?, body: [String: Any]) -> (Int, Data) {
        func response(_ object: Any, status: Int = 200) -> (Int, Data) { (status, (try? JSONSerialization.data(withJSONObject: object)) ?? Data()) }
        let key = jobID.uuidString
        let revision = deviceRevisions[key, default: 1]
        func identity(_ revision: Int) -> DeviceRenderIdentity {
            DeviceRenderIdentity(jobID: jobID, variantID: DeviceRenderUITestFixture.variantID, recipeRevision: revision,
                                 recipeDigest: String(repeating: revision == 1 ? "a" : "b", count: 64))
        }
        func encoded<T: Encodable>(_ value: T) -> Any {
            (try? RecipeJSON.encoder().encode(value)).flatMap { try? JSONSerialization.jsonObject(with: $0) } ?? [:]
        }
        switch route {
        case nil:
            let recipe = KriaMediaEngine.EditRecipe(assets: [MediaAsset(id: "source", relativePath: "source")], tracks: [TimelineTrack(id: "v", kind: .video, clips: [TimelineClip(id: "c", sourceAssetID: "source", sourceDuration: 1)])])
            var status: [String: Any] = ["phase": "awaiting_device", "request": encoded(DeviceRenderRequest(identity: identity(revision), recipe: recipe))]
            if ProcessInfo.processInfo.environment["KRIA_CHAT_DEVICE_SYNC_FAILURE"] == "1" {
                status["base_generation"] = "fixture-generation"
            }
            if scenario != "ready", revision == 1 {
                status["phase"] = "needs_attention"
                status["reason_code"] = scenario
                status["reason"] = "Fixture device render stopped: \(scenario)"
            }
            return response(status)
        case "retry":
            deviceRevisions[key] = revision + 1
            deviceReportedFailures.remove(key)
            DeviceRenderUITestFixture.published.remove(key)
            mirrorDeviceDelivery()
            return response(["identity": encoded(identity(revision + 1)), "phase": "awaiting_device"])
        case "failures":
            deviceReportedFailures.insert(key)
            mirrorDeviceDelivery()
            return response(["identity": encoded(identity(revision)), "phase": "needs_attention", "reason_code": body["reason_code"] as? String ?? "export_failed"])
        default:
            return response(["detail": "Fixture route missing"], status: 404)
        }
    }

    /// KRI-823 (`KRIA_CHAT_DEVICE_SEED_THREAD=1`): a project whose phone render was already handed to this iPhone
    /// before the app launched, under a fixed id, so a relaunch (and a warm SwiftData cache) finds the same project.
    static let seededDeviceThreadID = "DE11FE11-0000-4000-8000-000000000823"
    private func seedDeviceDeliveryThread() {
        let id = Self.seededDeviceThreadID
        guard DeviceRenderUITestFixture.scenario != nil,
              ProcessInfo.processInfo.environment["KRIA_CHAT_DEVICE_SEED_THREAD"] == "1", threads[id] == nil else { return }
        func event(_ sequence: Int, _ type: String, role: String = "assistant", _ payload: [String: Any] = [:]) -> [String: Any] {
            ["id": "\(id)-\(sequence)", "sequence": sequence, "revision": sequence + 1, "role": role, "event_type": type,
             "content": "", "payload": payload, "created_at": "2026-10-20T10:00:00Z"]
        }
        threads[id] = [
            "id": id, "title": "Delivery check", "status": "active", "revision": 4, "runtime_version": runtime,
            "state": ["format": "montage", "edit_format": "montage"],
            "events": [event(0, "thread_created"), event(1, "format_prompt"),
                       event(2, "action_select_format", role: "user", ["format": "montage"]), event(3, "generation_started")],
            "active_plan_item_id": id, "active_job_id": id,
            "job": ["id": id, "status": "processing", "variants": [
                Self.deviceRecordOnly
                    // A phone EDIT: the last accepted cut is still on the variant while its replacement is delivered.
                    ? ["variant_id": DeviceRenderUITestFixture.variantID, "render_status": "awaiting_device", "render_destination": "device",
                       "output_url": "https://fixture.invalid/last-cut.mp4",
                       "poster_url": Bundle.main.url(forResource: "trulli-street", withExtension: "jpg")?.absoluteString ?? ""]
                    : ["variant_id": DeviceRenderUITestFixture.variantID, "render_status": "awaiting_device", "render_destination": "device"],
            ]],
            "updated_at": "2026-10-20T10:00:00Z",
        ]
    }

    /// KRI-823 (`KRIA_CHAT_DEVICE_RECORD_ONLY=1`): the failure lands only on the device record, as a refused EDIT
    /// publication or an expired repair permission do; the variant's `render_status` stays `awaiting_device`.
    static var deviceRecordOnly: Bool { ProcessInfo.processInfo.environment["KRIA_CHAT_DEVICE_RECORD_ONLY"] == "1" }

    /// KRI-823: the server's thread projections carry each variant's current device delivery phase. A failure the
    /// server already recorded (`scenario` on revision 1) or the iPhone reported mirrors onto the variant the way the
    /// reaper / failure route do; the job itself stays in flight and the thread revision does not move. With
    /// `KRIA_CHAT_DEVICE_PUBLISH_READY=1` a published export turns the job ready, as `/device-render/complete` does.
    private func mirrorDeviceDelivery() {
        guard let scenario = DeviceRenderUITestFixture.scenario else { return }
        for (id, thread) in threads {
            guard var job = thread["job"] as? [String: Any], let jobID = job["id"] as? String,
                  var variants = job["variants"] as? [[String: Any]],
                  let index = variants.firstIndex(where: { $0["render_destination"] as? String == "device" }) else { continue }
            let failed = (scenario != "ready" && deviceRevisions[jobID, default: 1] == 1) || deviceReportedFailures.contains(jobID)
            let ready = ProcessInfo.processInfo.environment["KRIA_CHAT_DEVICE_PUBLISH_READY"] == "1"
                && DeviceRenderUITestFixture.published.contains(jobID)
            let phase = ready ? "published" : failed ? "needs_attention" : "awaiting_device"
            variants[index]["delivery_phase"] = phase
            variants[index]["render_status"] = ready ? "ready" : Self.deviceRecordOnly ? "awaiting_device" : phase
            job["variants"] = variants
            job["status"] = ready ? "variants_ready" : "processing"
            var updated = thread
            updated["job"] = job
            threads[id] = updated
        }
    }

    /// `KRIA_CHAT_GENERATE_CONFLICT` makes "generate" answer HTTP 409 like the
    /// Creator confirmation controller: `stale_manifest` until Kria re-plans once
    /// more, `wait_for_render` on the first attempt only.
    private func generateConflict(threadID id: String) -> String? {
        switch ProcessInfo.processInfo.environment["KRIA_CHAT_GENERATE_CONFLICT"] {
        case "stale_manifest":
            return planVersions[id, default: 0] < 2 ? "Footage or capabilities changed; review the plan again" : nil
        case "wait_for_render":
            let served = generateConflicts[id, default: 0]
            generateConflicts[id] = served + 1
            return served == 0 ? "Wait for the current render before confirming" : nil
        default:
            return nil
        }
    }
    /// `KRIA_CHAT_GENERATE_FAILURE` fails the first "generate" of each chat:
    /// `server_error` answers HTTP 500, and `offline` drops the connection before
    /// any response. The next attempt succeeds, so recovery can be exercised.
    private func generateFailure(threadID id: String) -> String? {
        guard let failure = ProcessInfo.processInfo.environment["KRIA_CHAT_GENERATE_FAILURE"],
              failedGenerates.insert(id).inserted else { return nil }
        return failure
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

/// KRI-141: the on-device half of the device-render fixture. `DeviceRenderSessions(api:)` asks
/// this before building the AVFoundation coordinator; outside that UI-test mode it returns nil.
/// The export is a short, deterministic placeholder file and publishing always succeeds, so the
/// journey reaches the real status card's ready state without footage or network uploads.
enum DeviceRenderUITestFixture {
    static let variantID = "original_text"
    /// Jobs whose export this iPhone published (KRI-823), read by the fixture server's thread projection.
    static let published = LockedJobSet()
    private static let clearedDirectories = LockedJobSet()

    final class LockedJobSet: @unchecked Sendable {
        private let lock = NSLock()
        private var ids: Set<String> = []
        func insert(_ id: String) { lock.lock(); ids.insert(id); lock.unlock() }
        func remove(_ id: String) { lock.lock(); ids.remove(id); lock.unlock() }
        func contains(_ id: String) -> Bool { lock.lock(); defer { lock.unlock() }; return ids.contains(id) }
    }
    static var scenario: String? {
        guard ProcessInfo.processInfo.arguments.contains("-ui-testing-chat") else { return nil }
        return ProcessInfo.processInfo.environment["KRIA_CHAT_DEVICE_RENDER"]
    }

    static func coordinator(directory: URL) throws -> DeviceRenderCoordinator? {
        guard scenario != nil else { return nil }
        // KRI-823 (`KRIA_CHAT_DEVICE_LOST_LOCAL_EXPORT=1`): this iPhone lost what an earlier launch exported (an
        // interrupted device), so only the server's record of the delivery remains. Once per launch and directory.
        if ProcessInfo.processInfo.environment["KRIA_CHAT_DEVICE_LOST_LOCAL_EXPORT"] == "1", !clearedDirectories.contains(directory.path) {
            clearedDirectories.insert(directory.path)
            try? FileManager.default.removeItem(at: directory)
        }
        return try DeviceRenderCoordinator(directory: directory, exporter: Exporter(), sources: Sources(), publisher: Publisher())
    }

    private struct Exporter: LocalExporting {
        func export(recipe: KriaMediaEngine.EditRecipe, assetURLs: [String: URL], outputURL: URL, exportID: String, progress: (@Sendable (Double) -> Void)?) async throws -> ExportCheckpoint {
            if ProcessInfo.processInfo.environment["KRIA_CHAT_PLAN_BLOCKS_DEVICE"] == "1" {
                // Real, steady progress so a UI test can watch the live feed advance with the build.
                for step in 1...8 {
                    try await Task.sleep(for: .milliseconds(1500))
                    progress?(Double(step) / 8)
                }
            } else {
                // Long enough for a UI test to see the rendering state and its Stop button.
                try await Task.sleep(for: .seconds(3))
            }
            try FileManager.default.createDirectory(at: outputURL.deletingLastPathComponent(), withIntermediateDirectories: true)
            try Data("kria ui-test device export".utf8).write(to: outputURL)
            return ExportCheckpoint(exportID: exportID, status: .completed, progress: 1, outputURL: outputURL)
        }
    }

    private struct Sources: DeviceSourceResolving {
        func resolve(for recipe: KriaMediaEngine.EditRecipe) async throws -> [String: URL] {
            // KRIA_CHAT_PLAN_BLOCKS_DEVICE: a visible "preparing sources" stage before the compose pass.
            if ProcessInfo.processInfo.environment["KRIA_CHAT_PLAN_BLOCKS_DEVICE"] == "1" { try await Task.sleep(for: .seconds(2)) }
            return [:]
        }
    }

    private struct Publisher: DeviceRenderPublishing {
        func isCurrent(_ identity: DeviceRenderIdentity) async throws -> Bool { true }
        func publish(file: URL, identity: DeviceRenderIdentity, attemptID: UUID, brandTail: String) async throws -> DevicePublication {
            if ProcessInfo.processInfo.environment["KRIA_CHAT_DEVICE_SYNC_FAILURE"] == "1" {
                throw APIError.conflict
            }
            DeviceRenderUITestFixture.published.insert(identity.jobID.uuidString)
            return .published
        }
    }
}

/// KRI-561 (`KRIA_CHAT_SONG_ORDER=1`): the song-order question the server sends to a timeline-capable app, and a
/// generated tone standing in for the signed song URL.
enum SongTimelineFixture {
    static let generation = 3
    static let audioSeconds = 12.0

    static func question(id: String, timeline: Bool) -> [String: Any] {
        func item(_ mediaID: String, _ status: String, start: Double?, duration: Double, candidates: [(Double, Double)], reason: String? = nil) -> [String: Any] {
            var fields: [String: Any] = ["media_id": mediaID, "status": status, "alternates": [[String: Any]]()]
            if let start { fields["song_start_s"] = start }
            guard timeline else { return fields }
            fields["duration_s"] = duration
            fields["candidates"] = candidates.map { ["delta_s": $0.0, "likelihood": $0.1] as [String: Any] }
            if let reason { fields["reason"] = reason }
            return fields
        }
        var question: [String: Any] = [
            "question_id": id,
            "proposed_order": ["fixture-clip", "fixture-clip-2", "fixture-clip-3", "fixture-clip-4"],
            "items": [
                item("fixture-clip", "confident", start: 4.0, duration: 8, candidates: [(4.0, 0.9)]),
                { var fields = item("fixture-clip-2", "ambiguous", start: 21.5, duration: 8, candidates: [(21.5, 0.6), (40.0, 0.3)], reason: "tie")
                  if !timeline { fields["alternates"] = [["delta_s": -8.0, "score": 0.4]] }
                  return fields }(),
                item("fixture-clip-3", "unmatched", start: nil, duration: 6, candidates: [(14.0, 0.4)], reason: "no_evidence"),
                item("fixture-clip-4", "confident", start: 52.0, duration: 8, candidates: [(52.0, 0.8), (-2.0, 0.1)]),
            ],
        ]
        if timeline {
            question["song_duration_s"] = 90.0
            question["max_window_s"] = 120.0
            question["first_line_s"] = 4.0
            question["song_generation"] = generation
        }
        return question
    }

    /// KRI-575: a tiny generated clip (8 s, 10 fps, a new colour every second, no audio) standing in for the local
    /// original of `fixture-clip`, so the preview's playing state can be asserted. The other clips have no original
    /// on the device, which is the "isn't on this iPhone" state.
    static func clipURL(mediaID: String, force: Bool = false) -> URL? {
        guard force || ProcessInfo.processInfo.environment["KRIA_CHAT_SONG_ORDER"] == "1", mediaID == "fixture-clip" else { return nil }
        let url = FileManager.default.urls(for: .cachesDirectory, in: .userDomainMask)[0].appendingPathComponent("song-timeline-fixture-clip.mp4")
        if FileManager.default.fileExists(atPath: url.path) { return url }
        let width = 180, height = 320, fps = 10, seconds = 8
        guard let writer = try? AVAssetWriter(outputURL: url, fileType: .mp4) else { return nil }
        let input = AVAssetWriterInput(mediaType: .video, outputSettings: [
            AVVideoCodecKey: AVVideoCodecType.h264, AVVideoWidthKey: width, AVVideoHeightKey: height])
        let adaptor = AVAssetWriterInputPixelBufferAdaptor(assetWriterInput: input, sourcePixelBufferAttributes: [
            kCVPixelBufferPixelFormatTypeKey as String: kCVPixelFormatType_32BGRA,
            kCVPixelBufferWidthKey as String: width, kCVPixelBufferHeightKey as String: height])
        writer.add(input)
        guard writer.startWriting() else { return nil }
        writer.startSession(atSourceTime: .zero)
        let colors: [UInt32] = [0xFF3B30, 0xFF9500, 0xFFCC00, 0x34C759, 0x00C7BE, 0x007AFF, 0x5856D6, 0xAF52DE]
        for frame in 0..<(fps * seconds) {
            while !input.isReadyForMoreMediaData { Thread.sleep(forTimeInterval: 0.002) }
            var buffer: CVPixelBuffer?
            guard let pool = adaptor.pixelBufferPool, CVPixelBufferPoolCreatePixelBuffer(nil, pool, &buffer) == kCVReturnSuccess, let buffer else { return nil }
            CVPixelBufferLockBaseAddress(buffer, [])
            if let base = CVPixelBufferGetBaseAddress(buffer) {
                let pixel = colors[(frame / fps) % colors.count] | 0xFF00_0000
                let pixels = base.assumingMemoryBound(to: UInt32.self)
                let stride = CVPixelBufferGetBytesPerRow(buffer) / 4
                for row in 0..<height { for column in 0..<width { pixels[row * stride + column] = pixel } }
            }
            CVPixelBufferUnlockBaseAddress(buffer, [])
            adaptor.append(buffer, withPresentationTime: CMTime(value: CMTimeValue(frame), timescale: CMTimeScale(fps)))
        }
        input.markAsFinished()
        let finished = DispatchSemaphore(value: 0)
        writer.finishWriting { finished.signal() }
        finished.wait()
        return writer.status == .completed ? url : nil
    }

    /// A 12 s mono 16-bit tone with a slow swell, written once to Caches.
    static func audioURL() -> URL? {
        let url = FileManager.default.urls(for: .cachesDirectory, in: .userDomainMask)[0].appendingPathComponent("song-timeline-fixture.wav")
        if FileManager.default.fileExists(atPath: url.path) { return url }
        let rate = 4000, count = Int(audioSeconds) * rate
        var pcm = Data(capacity: count * 2)
        for index in 0..<count {
            let t = Double(index) / Double(rate)
            let amplitude = 0.2 + 0.6 * abs(sin(2 * .pi * t / 3))
            let sample = Int16(amplitude * sin(2 * .pi * 220 * t) * 20000)
            withUnsafeBytes(of: sample.littleEndian) { pcm.append(contentsOf: $0) }
        }
        func le32(_ value: Int) -> Data { withUnsafeBytes(of: UInt32(value).littleEndian) { Data($0) } }
        func le16(_ value: Int) -> Data { withUnsafeBytes(of: UInt16(value).littleEndian) { Data($0) } }
        var wav = Data("RIFF".utf8) + le32(36 + pcm.count) + Data("WAVEfmt ".utf8) + le32(16) + le16(1) + le16(1)
        wav += le32(rate) + le32(rate * 2) + le16(2) + le16(16) + Data("data".utf8) + le32(pcm.count) + pcm
        return (try? wav.write(to: url, options: .atomic)) != nil ? url : nil
    }
}
#endif

#if DEBUG
/// KRI-815: the script the chat fixture hands back for `KRIA_CHAT_VOICEOVER_SCRIPT`.
enum VoiceoverScriptUITestFixture {
    static let lines = [
        "This hall stopped me in my tracks.",
        "A huge stone statue, just sitting there like it owns the place.",
        "And the gold mask, found in 1922, glowing under the spotlight.",
    ]
    static var text: String { lines.joined(separator: " ") }
}
#endif

#if DEBUG
/// KRI-723: the moments question the fixture server asks, shaped like `named_moments._question`. One 8 s take
/// (`fixture-clip`, which has a generated local original): the tray is not in the clip record, the wave matches two
/// analysed moments equally, the bite is grounded at 5.0-6.5 s.
enum MomentOrderFixture {
    static let keys = ["m-tray", "m-wave", "m-bite"]
    static let duration = 8.0

    static func question(id: String) -> [String: Any] {
        func window(_ start: Double, _ end: Double, _ text: String) -> [String: Any] {
            ["source_start_s": start, "source_end_s": end, "description": text, "kind": "best_moment"]
        }
        return [
            "question_id": id,
            "moment_set": "fixture-set",
            "take": ["media_id": "fixture-clip", "generation": "1", "duration_s": duration],
            "default_window_s": 2.0,
            "moments": [
                ["moment_key": "m-tray", "words": "Caro arriving with the black tray", "status": "unlocated",
                 "found": NSNull(), "suggestions": [] as [Any]],
                ["moment_key": "m-wave", "words": "her hello wave", "status": "ambiguous", "found": NSNull(),
                 "suggestions": [window(2.0, 3.0, "woman waves"), window(6.6, 7.6, "woman waves goodbye")]],
                ["moment_key": "m-bite", "words": "Atakan's first bite", "status": "grounded",
                 "found": window(5.0, 6.5, "man takes first bite of dessert"), "suggestions": [] as [Any]],
            ] as [[String: Any]],
        ]
    }
}
#endif
