#if DEBUG
import Foundation

/// The offline server's overlay sets (`KRIA_CHAT_OVERLAY_SETS=1`), in the shape of `overlay_set.event_payload`: one
/// event per change, each the whole set. A generate repeated for a request already made finds it and charges nothing,
/// like the server. Each overlay counts its generate requests and the attempts it charged, and with
/// `KRIA_CHAT_OVERLAY_ECHO=1` shows them in its title so a UI test can read "no second charge" off the screen.
///
/// `KRIA_CHAT_OVERLAY_FAIL=third`: the third overlay's first attempt fails, retryable; its second succeeds.
/// `KRIA_CHAT_OVERLAY_HOLD=1`: the third overlay's request never settles (its operation is still in flight).
enum OverlaySetUITestFixture {
    private static var environment: [String: String] { ProcessInfo.processInfo.environment }

    static var isEnabled: Bool { environment["KRIA_CHAT_OVERLAY_SETS"] == "1" }

    static var planRequest: OverlayPlanRequest? {
        guard isEnabled else { return nil }
        return OverlayPlanRequest(jobID: UUID().uuidString, variantID: "original_text", baseGeneration: "fixture", documentFingerprint: "fixture",
                                  mode: .automatic, prompt: "", tasteNotes: "", editorSnapshot: [:])
    }

    private static let automaticTitles = ["Mountain peak sticker", "Hand-drawn compass", "Rolled-up map"]
    private static let imageURL = "https://generated-media-image.invalid/cover.jpg"

    private static func phase(_ rec: [String: Any]) -> String {
        if rec["compose"] is NSNull || rec["compose"] == nil { return "ask" }
        let rows = rec["overlays"] as? [[String: Any]] ?? []
        if rec["planned"] as? Bool != true { return "compose" }
        return rows.contains { $0["state"] as? String == "making" } ? "progress" : "done"
    }

    private static func payload(_ setID: String, _ rec: [String: Any]) -> [String: Any] {
        let compose = rec["compose"] as? [String: Any]
        let rows = rec["overlays"] as? [[String: Any]] ?? []
        let echo = environment["KRIA_CHAT_OVERLAY_ECHO"] == "1"
        let shown = rows.map { row -> [String: Any] in
            let suffix = echo ? " [charged \(row["charges"] as? Int ?? 0) of \(row["posts"] as? Int ?? 0)]" : ""
            let state = row["state"] as? String ?? "making"
            return ["id": row["id"] ?? "", "title": (row["title"] as? String ?? "") + suffix, "prompt": (row["prompt"] as? String ?? "") + suffix, "state": state,
                    "operation_id": state == "making" ? NSNull() : (row["operation_id"] ?? NSNull()),
                    "error_code": row["error_code"] ?? NSNull(), "retryable": row["retryable"] as? Bool ?? false]
        }
        return ["v": 1, "set_id": setID, "phase": phase(rec), "mode": compose?["mode"] ?? NSNull(), "prompts": compose?["prompts"] ?? [String](),
                "unit_price_usd": 0.04, "max_overlays": 6, "overlays": shown,
                "overlay_ids": shown.filter { $0["state"] as? String == "ready" }.compactMap { $0["id"] }, "message": ""]
    }

    /// Answers one overlay-set route, or nil when `parts` is not one. Writes the event it publishes into `thread`.
    static func respond(method: String?, parts: [String], body: [String: Any], thread: inout [String: Any],
                        operations: inout [String: [String: Any]]) -> (Int, Any)? {
        guard isEnabled, method == "POST", parts.count >= 3, parts[2] == "overlay-sets" else { return nil }
        var sets = thread["overlay_fixture"] as? [String: [String: Any]] ?? [:]
        var events = thread["events"] as? [[String: Any]] ?? []
        var revision = thread["revision"] as? Int ?? 0
        func publish(_ setID: String, role: String = "assistant") -> [String: Any] {
            revision += 1
            let value = payload(setID, sets[setID] ?? [:])
            events.append(["id": UUID().uuidString, "sequence": events.count, "revision": revision, "role": role, "event_type": "overlay_set",
                           "content": "", "payload": value, "created_at": "2026-09-10T10:00:00Z"])
            return value
        }
        func save() {
            thread["overlay_fixture"] = sets
            thread["events"] = events
            thread["revision"] = revision
        }
        defer { save() }

        if parts.count == 3 {
            guard let setID = body["set_id"] as? String else { return (422, ["detail": "set_id"]) }
            if sets[setID] == nil {
                sets[setID] = ["compose": NSNull(), "planned": false, "overlays": [[String: Any]]()]
                return (200, publish(setID))
            }
            return (200, payload(setID, sets[setID] ?? [:]))
        }
        let setID = parts[3]
        guard var rec = sets[setID] else { return (404, ["detail": "Overlay set not found"]) }
        switch parts.count == 5 ? parts[4] : (parts.count == 7 ? "generate" : "") {
        case "compose":
            let answer: [String: Any] = ["mode": body["mode"] as? String ?? "automatic", "prompts": body["prompts"] as? [String] ?? []]
            if let stored = rec["compose"] as? [String: Any] {
                return NSDictionary(dictionary: stored).isEqual(to: answer) ? (200, payload(setID, rec)) : (409, ["detail": "This set was already answered"])
            }
            rec["compose"] = answer
            sets[setID] = rec
            return (200, publish(setID, role: "user"))
        case "plan":
            guard let compose = rec["compose"] as? [String: Any] else { return (409, ["detail": "Answer the question first"]) }
            if rec["planned"] as? Bool != true {
                let prompts = compose["prompts"] as? [String] ?? []
                let titles = compose["mode"] as? String == "prompt" ? prompts : automaticTitles
                rec["overlays"] = titles.map { title -> [String: Any] in
                    ["id": UUID().uuidString.lowercased(), "title": title, "prompt": title, "state": "making", "attempt": 0, "charges": 0, "posts": 0,
                     "retryable": false]
                }
                rec["planned"] = true
                sets[setID] = rec
                _ = publish(setID)
            }
            return (200, ["document_fingerprint": "fixture", "base_generation": "fixture", "time_basis": "editor_base",
                          "placement_review_required": true, "overlays": [[String: Any]](), "message": ""])
        case "generate":
            var rows = rec["overlays"] as? [[String: Any]] ?? []
            guard let index = rows.firstIndex(where: { $0["id"] as? String == parts[5] }) else { return (404, ["detail": "Overlay not found"]) }
            rows[index]["posts"] = (rows[index]["posts"] as? Int ?? 0) + 1
            var row = rows[index]
            let state = row["state"] as? String
            func commit() { rows[index] = row; rec["overlays"] = rows; sets[setID] = rec }
            if state == "ready" { commit(); return (200, payload(setID, rec)) }
            if state == "failed" {
                guard row["retryable"] as? Bool == true else { commit(); return (409, ["detail": "This overlay cannot be made again"]) }
                row["state"] = "making"; row["attempt"] = (row["attempt"] as? Int ?? 0) + 1; row["error_code"] = nil; row["retryable"] = false
                commit()
                _ = publish(setID)
            }
            if (row["charges"] as? Int ?? 0) < (row["attempt"] as? Int ?? 0) + 1 { row["charges"] = (row["attempt"] as? Int ?? 0) + 1 }
            let isThird = index == 2
            if isThird, environment["KRIA_CHAT_OVERLAY_HOLD"] == "1" { commit(); return (200, payload(setID, rec)) }
            if isThird, environment["KRIA_CHAT_OVERLAY_FAIL"] == "third", row["attempt"] as? Int == 0 {
                row["state"] = "failed"; row["error_code"] = "provider_rejected"; row["retryable"] = true
                row["operation_id"] = nil
            } else {
                let operationID = "op-" + (row["id"] as? String ?? "")
                row["state"] = "ready"; row["operation_id"] = operationID
                operations[operationID] = ["id": operationID, "state": "ready", "capability": "image", "asset_id": "generated-" + operationID,
                                           "preview_url": imageURL, "source_url": imageURL, "retryable": false, "fixture_title": row["title"] ?? ""]
            }
            commit()
            return (200, publish(setID))
        default:
            return (404, ["detail": "Fixture route missing"])
        }
    }
}
#endif
