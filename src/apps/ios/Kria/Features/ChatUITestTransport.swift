#if DEBUG
import Foundation
import KriaMediaEngine
import UIKit

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
        // `KRIA_SLIDE_POST_CAPS=slow`: capabilities answer after 6s (the slide editor must not wait for them).
        let slowCapabilities = request.url?.path.hasSuffix("/capabilities") == true
            && ProcessInfo.processInfo.environment["KRIA_SLIDE_POST_CAPS"] == "slow"
        if slowCapabilities || (request.url?.path.hasSuffix("/messages") == true
            && ProcessInfo.processInfo.environment["KRIA_CHAT_SLOW_CREATION"] == "1") {
            let work = DispatchWorkItem { [weak self] in self?.finishLoading() }
            delayedResponse = work
            DispatchQueue.global().asyncAfter(deadline: .now() + (slowCapabilities ? 6 : 5), execute: work)
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
final class CreationChatFixture: @unchecked Sendable {
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
    private var chatEditFailed = false
    /// `KRIA_SLIDE_POST_LATE_ASSET=1`: a fourth photo that is still processing for the first two reads
    /// after a draft exists and then turns ready, like an upload that finishes while the editor is open.
    private var lateAssetPolls: [String: Int] = [:]
    private var deviceRevisions: [String: Int] = [:]
    /// KRI-443 (`KRIA_CHAT_PLAN_BLOCKS=1`): how far the staged `plan_block` script has advanced, per thread.
    private var planStages: [String: Int] = [:]
    private var planTicks: [String: Int] = [:]
    private var planTurnIDs: [String: String] = [:]
    private let approvalID = "aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa"
    private var runtime: Int { ProcessInfo.processInfo.environment["KRIA_CHAT_CREATION_FLOW"] == "v2" ? 2 : 1 }
    /// Returns nil when the request should fail without any HTTP response.
    func respond(_ request: URLRequest) -> (Int, Data)? {
        lock.lock(); defer { lock.unlock() }
        let path = request.url?.path ?? ""
        let body = (try? JSONSerialization.jsonObject(with: bodyData(request))) as? [String: Any] ?? [:]
        func response(_ object: Any, status: Int = 200) -> (Int, Data) { (status, (try? JSONSerialization.data(withJSONObject: object)) ?? Data()) }
        if path == "/creation-threads/capabilities" {
            // `KRIA_SLIDE_POST_CAPS=fail`: the capabilities request never gets an answer.
            if ProcessInfo.processInfo.environment["KRIA_SLIDE_POST_CAPS"] == "fail" { return nil }
            var capabilities: [String: Any] = ["formats": [("montage", "montage", 10), ("narrated", "narrated_planned", 10), ("talking_to_camera", "subtitled", 1), ("slides", "slides", 20)].map { ["id": $0.0, "edit_format": $0.1, "max_clips": $0.2] as [String: Any] }, "runtime_versions": runtime == 2 ? [1, 2] : [1], "visuals_enabled": true]
            if ProcessInfo.processInfo.environment["KRIA_CHAT_PLAN_BLOCKS"] == "1" { capabilities["live_plan_review_enabled"] = true }
            if ProcessInfo.processInfo.environment["KRIA_SLIDE_POST_RICH_TEXT"] == "1" { capabilities["slide_post_rich_text"] = true }
            if ProcessInfo.processInfo.environment["KRIA_SLIDE_POST_EXTENDED_DEVICE_EXPORT"] == "1" { capabilities["slide_post_extended_device_export"] = true }
            if ProcessInfo.processInfo.environment["KRIA_SLIDE_POST_CHAT_EDIT"] == "1" { capabilities["slide_post_chat_edit"] = true }
            // KRIA_CHAT_CLIP_QUESTION: "1" = server advertises clip_selection_questions; "legacy" = it still
            // sends the clip_question payload but does not advertise the capability (old-server fallback).
            if ["1", "history"].contains(ProcessInfo.processInfo.environment["KRIA_CHAT_CLIP_QUESTION"]) { capabilities["clip_selection_questions"] = true }
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
                let fixtureEvents: [[String: Any]] = ProcessInfo.processInfo.environment["KRIA_CHAT_LONG_HISTORY"] == "1"
                    ? (0..<24).map { index in
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
                           payload: ["turn_id": id, "turn_value": "review", "requirement_receipts": Self.fixtureReceipts])
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
        thread["state"] = state; thread["events"] = events; thread["revision"] = revision; threads[id] = thread
        if parts.last == "delta" {
            let after = URLComponents(url: request.url!, resolvingAgainstBaseURL: false)?.queryItems?.first(where: { $0.name == "after_sequence" })?.value.flatMap(Int.init) ?? -1
            return response(["thread_id": id, "runtime_version": runtime, "status": "active", "after_sequence": after, "has_more": false, "thread_revision": revision, "events": events.filter { ($0["sequence"] as? Int ?? 0) > after }, "next_after_sequence": events.count - 1])
        }
        if parts.last == "turns" { return response(["turn_id": id, "thread_revision": revision, "status": "queued"], status: 202) }
        if parts.last == "approve" { return response(["approval_id": approvalID, "thread_id": id, "status": "approved", "thread_revision": revision]) }
        return response(withRenderShape(thread))
    }
    /// KRI-443: the staged `plan_block` payloads, in the order the pipeline would decide them.
    private static var planScript: [[[String: Any]]] {
        func block(_ section: String, _ state: String, _ summary: String? = nil, detail: String? = nil, skipped: Bool = false) -> [String: Any] {
            var value: [String: Any] = ["section_id": section, "state": state, "intent": false, "skipped": skipped]
            if let summary { value["summary"] = summary }
            if let detail { value["detail"] = detail }
            if state == "decided" { value["decided_at"] = "2026-10-05T10:00:00Z" }
            return value
        }
        let sections = ["title", "clips", "captions", "music", "sfx", "overlays", "look"]
        if ProcessInfo.processInfo.environment["KRIA_CHAT_PLAN_BLOCKS_DEVICE"] == "1" {
            // What an iPhone account gets in production: every section waiting, then every section decided
            // almost at once, because the server only plans and the video is built on the device.
            return [
                sections.map { block($0, "waiting") },
                [block("title", "decided", "Sunday reset, slowed down"), block("clips", "decided", "30 clips · 30s"),
                 block("captions", "decided", "Bold captions, lower third"), block("music", "decided", "Your song"),
                 block("sfx", "decided", "4 sound effects"), block("overlays", "decided", nil, skipped: true),
                 block("look", "decided", "Warm film grain")],
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

    static func realShapeClipQuestionHistory() -> (events: [[String: Any]], state: [String: Any]) {
        let ids = realShapeMediaIDs
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
        append("assistant_response", role: "assistant",
               content: "I couldn't tell which of your clips show \"dodgeball\". Tap the clips that do, or tell me there aren't any.",
               payload: ["turn_id": "t1", "turn_value": "question", "receipt_ids": [String](), "next_actions": [String](), "schema_version": 2,
                         "clip_question": ["version": 1, "allow_none": true, "question_id": "real-shape-q",
                                           "categories": [["op": "group", "key": "group:dodgeball", "label": "dodgeball",
                                                           "candidate_media_ids": ids, "suggested_media_ids": [String]()]]] as [String: Any]])
        return (events, ["format": "montage", "media": media])
    }

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
        // `KRIA_SLIDE_POST_MANY=1`: twelve slides, so the strip overflows and auto-scroll can be exercised.
        let base = [("trulli-street", "jpg", "image"), ("istanbul", "mp4", "video"), ("lisbon", "jpg", "image")]
        let media = ProcessInfo.processInfo.environment["KRIA_SLIDE_POST_MANY"] == "1" ? Array(repeating: base, count: 4).flatMap { $0 } : base
        var assets: [[String: Any]] = media.enumerated().map { index, media in
            var url = Bundle.main.url(forResource: media.0, withExtension: media.1)?.absoluteString ?? ""
            var poster = media.2 == "video" ? Bundle.main.url(forResource: "trulli-street", withExtension: "jpg")!.absoluteString : url
            // `KRIA_SLIDE_POST_REMOTE_MEDIA=1`: photos look remote (slow, re-signed on every response) so the image
            // cache is exercised: same asset, new `sig` each time. Videos keep their local source for AVPlayer.
            if ProcessInfo.processInfo.environment["KRIA_SLIDE_POST_REMOTE_MEDIA"] == "1" {
                let signed = { (name: String, ext: String) in "https://fixture.invalid/\(name).\(ext)?sig=\(UUID().uuidString)" }
                if media.2 == "image" { url = signed(media.0, media.1); poster = url } else { poster = signed("trulli-street", "jpg") }
            }
            return ["id": "asset-\(index)", "kind": media.2, "status": "ready", "media_status": "available", "source_filename": "\(media.0).\(media.1)", "source_url": url, "display_url": url, "preview_url": poster, "duration_s": 8]
        }
        let slides: [[String: Any]] = assets.enumerated().map { index, asset in ["id": "slide-\(index)", "asset_id": asset["id"]!, "kind": asset["kind"]!] }
        // `KRIA_SLIDE_POST_READY_DRAFT=1`: the READY post (`KRIA_SLIDE_POST_READY_THREAD`) already has a saved,
        // rendered server draft, like a post opened from the gallery in production.
        if ProcessInfo.processInfo.environment["KRIA_SLIDE_POST_READY_DRAFT"] == "1",
           itemID.uppercased() == "BBBBBBBB-BBBB-BBBB-BBBB-BBBBBBBBBBBB", slideDrafts[itemID] == nil {
            slideDrafts[itemID] = ["schema_version": 1, "version": 1, "platform_profile": "instagram_carousel", "slides": slides, "cover_index": 0,
                                   "caption": "Three moments, one story.", "user_edited": true, "rendered_version": 1]
            slideRendered.insert(itemID)
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
        func publish(file: URL, identity: DeviceRenderIdentity, attemptID: UUID, brandTail: String) async throws -> DevicePublication { .published }
    }
}
#endif
