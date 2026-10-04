#if DEBUG
import Foundation
import KriaMediaEngine

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
private final class CreationChatFixture: @unchecked Sendable {
    static let shared = CreationChatFixture()
    private let lock = NSLock()
    private var threads: [String: [String: Any]] = [:]
    private var renders: [String: Int] = [:]
    private var preparations: [String: Int] = [:]
    private var planVersions: [String: Int] = [:]
    private var generateConflicts: [String: Int] = [:]
    private var failedGenerates: Set<String> = []
    private var slideDrafts: [String: [String: Any]] = [:]
    private var slideRendered: Set<String> = []
    private var deviceRevisions: [String: Int] = [:]
    private let approvalID = "aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa"
    private var runtime: Int { ProcessInfo.processInfo.environment["KRIA_CHAT_CREATION_FLOW"] == "v2" ? 2 : 1 }
    /// Returns nil when the request should fail without any HTTP response.
    func respond(_ request: URLRequest) -> (Int, Data)? {
        lock.lock(); defer { lock.unlock() }
        let path = request.url?.path ?? ""
        let body = (try? JSONSerialization.jsonObject(with: bodyData(request))) as? [String: Any] ?? [:]
        func response(_ object: Any, status: Int = 200) -> (Int, Data) { (status, (try? JSONSerialization.data(withJSONObject: object)) ?? Data()) }
        if path == "/creation-threads/capabilities" {
            var capabilities: [String: Any] = ["formats": [("montage", "montage", 10), ("narrated", "narrated_planned", 10), ("talking_to_camera", "subtitled", 1), ("slides", "slides", 20)].map { ["id": $0.0, "edit_format": $0.1, "max_clips": $0.2] as [String: Any] }, "runtime_versions": runtime == 2 ? [1, 2] : [1], "visuals_enabled": true]
            if ProcessInfo.processInfo.environment["KRIA_SLIDE_POST_RICH_TEXT"] == "1" { capabilities["slide_post_rich_text"] = true }
            if ProcessInfo.processInfo.environment["KRIA_SLIDE_POST_CHAT_EDIT"] == "1" { capabilities["slide_post_chat_edit"] = true }
            // KRIA_CHAT_CLIP_QUESTION: "1" = server advertises clip_selection_questions; "legacy" = it still
            // sends the clip_question payload but does not advertise the capability (old-server fallback).
            if ProcessInfo.processInfo.environment["KRIA_CHAT_CLIP_QUESTION"] == "1" { capabilities["clip_selection_questions"] = true }
            // KRIA_CHAT_SONG_ORDER: "1" = server advertises media.song and song_order_questions (KRI-374);
            // "legacy" = it still sends the song_order_question payload but advertises nothing (old-app fallback).
            if ProcessInfo.processInfo.environment["KRIA_CHAT_SONG_ORDER"] == "1" {
                capabilities["song_order_questions"] = true
                capabilities["media"] = ["song": ["max": 1, "max_file_bytes": 52_428_800, "content_types": ["audio/mpeg", "audio/mp4"]]]
            }
            // KRIA_CHAT_CHOICE_QUESTION: "1" = server advertises choice_questions; "legacy" = it still sends the
            // choice_question payload but does not advertise the capability (old-server text fallback).
            if ProcessInfo.processInfo.environment["KRIA_CHAT_CHOICE_QUESTION"] == "1" { capabilities["choice_questions"] = true }
            if DeviceRenderUITestFixture.scenario != nil {
                capabilities["phone_rendering"] = ["enabled": true, "recipe_versions": [1, 2], "verified_features": MediaCapability.allCases.map(\.rawValue)]
            }
            return response(capabilities)
        }
        if path == "/creation-threads" {
            if request.httpMethod == "POST" {
                let id = UUID().uuidString
                let fixtureEvents: [[String: Any]] = ProcessInfo.processInfo.environment["KRIA_CHAT_LONG_HISTORY"] == "1"
                    ? (0..<24).map { index in
                        ["id": UUID().uuidString, "sequence": index, "revision": index + 1,
                         "role": index.isMultiple(of: 2) ? "user" : "assistant",
                         "event_type": index.isMultiple(of: 2) ? "user_message" : "assistant_response",
                         "content": "Long conversation message number \(index + 1) for drawer indicator diagnostics.",
                         "payload": [:], "created_at": "2026-09-10T10:00:00Z"]
                    }
                    : []
                let thread: [String: Any] = ["id": id, "title": "Untitled project", "status": "active", "revision": fixtureEvents.count, "runtime_version": runtime, "state": [:], "events": fixtureEvents, "active_plan_item_id": id, "updated_at": "2026-09-10T10:00:00Z"]
                threads[id] = thread
                return response(thread, status: 201)
            }
            return response(Array(threads.values))
        }
        let parts = path.split(separator: "/").map(String.init)
        if parts.count >= 4, parts[0] == "me", parts[1] == "jobs", parts[3] == "device-render",
           let scenario = DeviceRenderUITestFixture.scenario, let jobID = UUID(uuidString: parts[2]) {
            return deviceRenderResponse(scenario: scenario, jobID: jobID, route: parts.count > 4 ? parts[4] : nil, body: body)
        }
        if parts.first == "plan-items", parts.count >= 2, ProcessInfo.processInfo.environment["KRIA_SLIDE_POST_FIXTURE"] == "1" {
            return slideResponse(request, itemID: parts[1], parts: parts, body: body)
        }
        if parts.first == "plan-items" { return response(["assets": [], "max_assets": 10]) }
        guard parts.count >= 2, var thread = threads[parts[1]] else { return response(["detail": "Fixture route missing"], status: 404) }
        let id = parts[1]
        if parts.count == 3, parts[2] == "brief" { return response(Self.fixtureBrief(threadID: id)) }
        var state = thread["state"] as? [String: Any] ?? [:]
        var events = thread["events"] as? [[String: Any]] ?? []
        var revision = thread["revision"] as? Int ?? 0
        func append(_ type: String, role: String = "assistant", text: String? = nil, payload: [String: Any] = [:], clientEventID: String? = nil) {
            revision += 1
            var event: [String: Any] = ["id": UUID().uuidString, "sequence": events.count, "revision": revision, "role": role, "event_type": type, "content": text ?? "", "payload": payload, "created_at": "2026-09-10T10:00:00Z"]
            if let clientEventID { event["client_event_id"] = clientEventID }
            events.append(event)
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
                    if ProcessInfo.processInfo.environment["KRIA_CHAT_FIXTURE_VOICEOVER"] == "1" {
                        media.append(["media_id": "fixture-voiceover", "kind": "audio", "filename": "existing-voiceover.m4a", "duration_s": 6])
                    }
                    state["media"] = media
                }
                append("action_select_format", payload: payload)
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
            // Like the server (`runtime.py`), a stored user message echoes the structured answer it carried
            // (`song_order` / `choice_selection`); that echo is what closes the order card.
            var userPayload: [String: Any] = [:]
            if let order = body["song_order"] as? [String: Any] { userPayload["song_order"] = order }
            if let selection = body["choice_selection"] as? [String: Any] { userPayload["choice_selection"] = selection }
            append("user_message", role: "user", text: body["message"] as? String, payload: userPayload, clientEventID: turnID)
            if runtime == 2, ProcessInfo.processInfo.environment["KRIA_CHAT_SONG_ORDER"] != nil {
                if let order = body["song_order"] as? [String: Any] {
                    // Echo what the server received so the UI test can pin the structured payload.
                    let ids = (order["ordered_media_ids"] as? [String] ?? []).joined(separator: "+")
                    append("assistant_response", text: "Got it. order[\(ids)] question[\(order["question_id"] ?? "")]")
                } else {
                    append("assistant_question", text: "I couldn't place a few of your clips against the song. Check the order.", payload: [
                        "turn_id": turnID, "turn_value": "question",
                        "song_order_question": [
                            "question_id": "song-q-\(events.count)",
                            "proposed_order": ["fixture-clip", "fixture-clip-2", "fixture-clip-3", "fixture-clip-4"],
                            "items": [
                                ["media_id": "fixture-clip", "status": "confident", "song_start_s": 4.0, "alternates": []],
                                ["media_id": "fixture-clip-2", "status": "ambiguous", "song_start_s": 21.5, "alternates": [["delta_s": -8.0, "score": 0.4]]],
                                ["media_id": "fixture-clip-3", "status": "unmatched", "alternates": []],
                                ["media_id": "fixture-clip-4", "status": "confident", "song_start_s": 52.0, "alternates": []],
                            ],
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
                    append("assistant_response", text: "I couldn't verify any clips for dodgeball. Could you clarify?", payload: [
                        "turn_id": turnID, "turn_value": "question",
                        "clip_question": [
                            "version": 1, "question_id": "q-\(events.count)", "allow_none": true,
                            "categories": [
                                ["key": "dodgeball", "label": "Dodgeball", "op": "group",
                                 "candidate_media_ids": ["fixture-clip", "fixture-clip-2", "fixture-clip-3"],
                                 "suggested_media_ids": ["fixture-clip-2"]],
                                ["key": "football", "label": "Football", "op": "group",
                                 "candidate_media_ids": ["fixture-clip-3", "fixture-clip-4"], "suggested_media_ids": []],
                            ],
                        ] as [String: Any],
                    ])
                }
            } else if runtime == 2 {
                append("assistant_response", text: "Your draft is ready for review.")
                append("draft_applied", payload: ["turn_id": turnID, "draft_id": id])
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
            if parts.last == "approve" {
                append("approval_approved")
                thread["active_job_id"] = id
                thread["job"] = ["id": id, "status": "processing", "variants": []]
                renders[id] = 0
            } else {
                let approvalPayload = events.last(where: { $0["event_type"] as? String == "approval_requested" })?["payload"] as? [String: Any]
                let turnID = approvalPayload?["turn_id"] as? String ?? id
                return response(["approval_id": approvalID, "turn_id": turnID, "draft_id": id, "draft_revision": 1, "status": "pending", "consequence_summary": "Open on the laugh and keep the pacing quick.", "expires_at": ProcessInfo.processInfo.environment["KRIA_CHAT_EXPIRED_APPROVAL"] == "1" ? "2000-09-10T10:00:00Z" : "2099-09-10T10:00:00Z", "approval_fingerprint": String(repeating: "a", count: 64)])
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
                if ProcessInfo.processInfo.environment["KRIA_CHAT_FIXTURE_BRIEF"] == "1" {
                    // What the server sends after a render: the reply plus one receipt per requirement.
                    append("assistant_review", text: "The cut is ready. I did most of what you asked; one thing needs your call.",
                           payload: ["turn_id": id, "turn_value": "review", "requirement_receipts": Self.fixtureReceipts])
                }
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

    private static func fixtureBrief(threadID: String) -> [String: Any] {
        ["thread_id": threadID, "version": 1,
         "requirements": [
            ["id": "req-labels", "kind": "text", "scope": "per_clip", "description": "Label each clip with its place", "status": "met"],
            ["id": "req-order", "kind": "order", "scope": "route", "description": "Follow the pier-to-lighthouse route", "status": "partial"],
            ["id": "req-drone", "kind": "select", "scope": "clip", "description": "End on a drone shot", "status": "not_possible"],
         ],
         "requirement_receipts": fixtureReceipts]
    }

    /// Slide fixture is explicitly test-only; production exercises the real
    /// proposal, versioned save, and version-approved dispatch endpoints.
    private func slideResponse(_ request: URLRequest, itemID: String, parts: [String], body: [String: Any]) -> (Int, Data) {
        func response(_ object: Any, status: Int = 200) -> (Int, Data) { (status, (try? JSONSerialization.data(withJSONObject: object)) ?? Data()) }
        let media = [("trulli-street", "jpg", "image"), ("istanbul", "mp4", "video"), ("lisbon", "jpg", "image")]
        let assets: [[String: Any]] = media.enumerated().map { index, media in
            let url = Bundle.main.url(forResource: media.0, withExtension: media.1)?.absoluteString ?? ""
            return ["id": "asset-\(index)", "kind": media.2, "status": "ready", "media_status": "available", "source_filename": "\(media.0).\(media.1)", "source_url": url, "display_url": url, "preview_url": media.2 == "video" ? Bundle.main.url(forResource: "trulli-street", withExtension: "jpg")!.absoluteString : url, "duration_s": 8]
        }
        let slides: [[String: Any]] = assets.enumerated().map { index, asset in ["id": "slide-\(index)", "asset_id": asset["id"]!, "kind": asset["kind"]!] }
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
            if scenario != "ready", revision == 1 {
                status["phase"] = "needs_attention"
                status["reason_code"] = scenario
                status["reason"] = "Fixture device render stopped: \(scenario)"
            }
            return response(status)
        case "retry":
            deviceRevisions[key] = revision + 1
            return response(["identity": encoded(identity(revision + 1)), "phase": "awaiting_device"])
        case "failures":
            return response(["identity": encoded(identity(revision)), "phase": "needs_attention", "reason_code": body["reason_code"] as? String ?? "export_failed"])
        default:
            return response(["detail": "Fixture route missing"], status: 404)
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
    static var scenario: String? {
        guard ProcessInfo.processInfo.arguments.contains("-ui-testing-chat") else { return nil }
        return ProcessInfo.processInfo.environment["KRIA_CHAT_DEVICE_RENDER"]
    }

    static func coordinator(directory: URL) throws -> DeviceRenderCoordinator? {
        guard scenario != nil else { return nil }
        return try DeviceRenderCoordinator(directory: directory, exporter: Exporter(), sources: Sources(), publisher: Publisher())
    }

    private struct Exporter: LocalExporting {
        func export(recipe: KriaMediaEngine.EditRecipe, assetURLs: [String: URL], outputURL: URL, exportID: String, progress: (@Sendable (Double) -> Void)?) async throws -> ExportCheckpoint {
            // Long enough for a UI test to see the rendering state and its Stop button.
            try await Task.sleep(for: .seconds(3))
            try FileManager.default.createDirectory(at: outputURL.deletingLastPathComponent(), withIntermediateDirectories: true)
            try Data("kria ui-test device export".utf8).write(to: outputURL)
            return ExportCheckpoint(exportID: exportID, status: .completed, progress: 1, outputURL: outputURL)
        }
    }

    private struct Sources: DeviceSourceResolving {
        func resolve(for recipe: KriaMediaEngine.EditRecipe) async throws -> [String: URL] { [:] }
    }

    private struct Publisher: DeviceRenderPublishing {
        func isCurrent(_ identity: DeviceRenderIdentity) async throws -> Bool { true }
        func publish(file: URL, identity: DeviceRenderIdentity, attemptID: UUID, brandTail: String) async throws -> DevicePublication { .published }
    }
}
#endif
