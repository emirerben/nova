#if DEBUG
import Foundation

/// UI-test fixture for live plan & review contract v2 (`KRIA_CHAT_PLAN_REVIEW=1`): the server side of
/// `GET /plan`, a scoped "Update video" turn, per-section undo and "Undo all". It is a small in-memory model of
/// what the real server does: a section changes only when it is in scope, every update is a NEW job whose blocks
/// carry `changed` / `previous` relative to the job before it, and an undo is just another update that restores
/// the previous value (so Undo toggles). Used under `CreationChatFixture`'s lock.
final class ReviewPlanFixture {
    static let order = ["title", "clips", "captions", "music", "sfx", "overlays", "look", "post_caption"]

    enum Kind { case update, undoSection, undoAll }

    struct Pending {
        var kind: Kind
        var scope: [String]
        var edits: [[String: Any]]
        var turnID: String
    }

    struct Running {
        var jobID: String
        var scope: [String]
        var previousJobID: String
        var ticks = 0
        var result: [String: [String: Any]]
        var summaryText: String
        var turnID: String
    }

    struct ThreadState {
        var jobID: String
        var blocks: [String: [String: Any]]
        var draftRevision = 3
        var canUndo = false
        var pending: Pending?
        var running: Running?
        var summary: [String: Any]?
        var scope: [String]?
    }

    /// How many `/plan` and `/delta` reads an update render takes before it is ready.
    static let ticksToReady = 8

    private var threads: [String: ThreadState] = [:]

    func state(_ id: String, initial: () -> [String: [String: Any]]) -> ThreadState {
        if let existing = threads[id] { return existing }
        let created = ThreadState(jobID: id, blocks: initial())
        threads[id] = created
        return created
    }

    func hasPending(_ id: String) -> Bool { threads[id]?.pending != nil }
    func isRunning(_ id: String) -> Bool { threads[id]?.running != nil }

    // MARK: Snapshot

    func snapshot(id: String, initial: () -> [String: [String: Any]], status firstRenderStatus: String, eventCount: Int) -> [String: Any] {
        let current = state(id, initial: initial)
        var blocks: [[String: Any]] = []
        let status: String
        var scope: Any = NSNull()
        var jobID = current.jobID
        if firstRenderStatus != "ready" {
            status = firstRenderStatus
            blocks = Self.order.map { ["section_id": $0, "state": "waiting", "intent": false, "skipped": false, "revision": 0, "changed": false, "editable": false] }
        } else if let running = current.running {
            status = "updating"
            scope = running.scope
            jobID = running.jobID
            blocks = Self.order.compactMap { section in
                guard var block = current.blocks[section] else { return nil }
                block["editable"] = false
                if running.scope.contains(section) {
                    block["state"] = "deciding"
                    block.removeValue(forKey: "decided_at")
                    block["changed"] = false
                    block.removeValue(forKey: "previous")
                } else {
                    block["changed"] = false
                    block.removeValue(forKey: "previous")
                }
                return block
            }
        } else {
            status = "ready"
            if let scoped = current.scope { scope = scoped }
            blocks = Self.order.compactMap { section in
                guard var block = current.blocks[section] else { return nil }
                block["editable"] = section != "post_caption"
                return block
            }
        }
        let decided = blocks.filter { ($0["state"] as? String) == "decided" }.count
        var json: [String: Any] = [
            "thread_id": id, "thread_revision": eventCount, "job_id": jobID, "turn_id": id,
            "status": status, "scope": status == "updating" ? scope : NSNull(),
            "decided_count": decided, "total_count": blocks.count, "blocks": blocks,
            "next_after_sequence": max(eventCount - 1, 0),
        ]
        if let previousJob = current.running?.previousJobID { json["previous_job_id"] = previousJob }
        if status == "ready" {
            json["draft"] = ["draft_id": id, "draft_revision": current.draftRevision, "etag": "etag-\(current.draftRevision)", "can_undo": current.canUndo]
            if let summary = current.summary { json["update_summary"] = summary }
        }
        return json
    }

    // MARK: Turns

    func scopedTurn(id: String, initial: () -> [String: [String: Any]], scope: [String], edits: [[String: Any]], turnID: String) {
        var current = state(id, initial: initial)
        current.pending = Pending(kind: .update, scope: Self.sorted(scope), edits: edits, turnID: turnID)
        threads[id] = current
    }

    /// Undo of one section. Returns an error response (status, body) or nil when accepted.
    func undoSection(id: String, initial: () -> [String: [String: Any]], section: String, blockRevision: Int, turnID: String) -> (Int, [String: Any])? {
        var current = state(id, initial: initial)
        guard let block = current.blocks[section] else {
            return (404, ["problem": ["code": "plan_section_not_found", "message": "No such section."]])
        }
        if section == "post_caption" {
            return (422, ["problem": ["code": "scope_section_unsupported", "message": "A post caption can't be undone."]])
        }
        let revision = block["revision"] as? Int ?? 0
        if revision != blockRevision {
            return (409, ["problem": ["code": "plan_section_stale", "message": "That section changed.", "current_revision": revision]])
        }
        guard (block["changed"] as? Bool) == true, block["previous"] != nil, current.running == nil else {
            return (409, ["problem": ["code": "plan_section_not_undoable", "message": "Nothing to undo."]])
        }
        current.pending = Pending(kind: .undoSection, scope: [section], edits: [], turnID: turnID)
        threads[id] = current
        return nil
    }

    func undoAll(id: String, initial: () -> [String: [String: Any]], turnID: String) -> (Int, [String: Any])? {
        var current = state(id, initial: initial)
        let changed = Self.order.filter { (current.blocks[$0]?["changed"] as? Bool) == true }
        guard !changed.isEmpty, current.canUndo, current.running == nil else {
            return (409, ["problem": ["code": "draft_stale", "message": "Nothing to undo."]])
        }
        current.pending = Pending(kind: .undoAll, scope: changed, edits: [], turnID: turnID)
        threads[id] = current
        return nil
    }

    // MARK: Approval -> render

    /// The creator approved the pending draft: a new job starts. Returns its id and the first `plan_block`
    /// event payload (every section; the redone ones are `deciding`).
    func approve(id: String, turnID: String) -> (jobID: String, event: [String: Any])? {
        guard var current = threads[id], let pending = current.pending else { return nil }
        let newJob = UUID().uuidString
        let (result, summary) = Self.apply(pending, to: current.blocks, previousJob: current.jobID)
        current.running = Running(jobID: newJob, scope: pending.scope, previousJobID: current.jobID, result: result, summaryText: summary, turnID: turnID)
        current.pending = nil
        threads[id] = current
        let blocks: [[String: Any]] = Self.order.compactMap { section in
            guard var block = current.blocks[section] else { return nil }
            block["changed"] = false
            block.removeValue(forKey: "previous")
            if pending.scope.contains(section) {
                block["state"] = "deciding"
                block.removeValue(forKey: "decided_at")
            }
            return block
        }
        return (newJob, ["turn_id": turnID, "job_id": newJob, "scope": pending.scope, "previous_job_id": current.jobID, "blocks": blocks])
    }

    struct Finished {
        var jobID: String
        var blocksEvent: [String: Any]
        var summaryEvent: [String: Any]
    }

    /// One read of `/plan` or `/delta` while an update renders; after `ticksToReady` reads it is ready.
    func tick(id: String) -> Finished? {
        guard var current = threads[id], var running = current.running else { return nil }
        running.ticks += 1
        guard running.ticks >= Self.ticksToReady else {
            current.running = running
            threads[id] = current
            return nil
        }
        var blocks = current.blocks
        for section in Self.order {
            if running.scope.contains(section), let updated = running.result[section] {
                blocks[section] = updated
            } else if var block = blocks[section] {
                block["changed"] = false
                block.removeValue(forKey: "previous")
                blocks[section] = block
            }
        }
        current.blocks = blocks
        current.jobID = running.jobID
        current.running = nil
        current.draftRevision += 1
        current.canUndo = true
        current.scope = running.scope
        let changedSections = Self.order.filter { (blocks[$0]?["changed"] as? Bool) == true }
        let summary: [String: Any] = ["turn_id": running.turnID, "job_id": running.jobID, "text": running.summaryText, "changed_sections": changedSections]
        current.summary = summary
        threads[id] = current
        let decided: [[String: Any]] = running.scope.compactMap { blocks[$0] }
        return Finished(jobID: running.jobID, blocksEvent: ["turn_id": running.turnID, "job_id": running.jobID, "scope": running.scope, "previous_job_id": running.previousJobID, "blocks": decided],
                        summaryEvent: summary)
    }

    // MARK: Values

    private static func sorted(_ scope: [String]) -> [String] { order.filter(scope.contains) }

    private static func previous(of block: [String: Any], job: String) -> [String: Any] {
        var previous: [String: Any] = ["revision": block["revision"] as? Int ?? 0, "job_id": job, "skipped": block["skipped"] as? Bool ?? false]
        if let summary = block["summary"] { previous["summary"] = summary }
        if let payload = block["payload"] as? [String: Any] { previous["payload"] = payload }
        return previous
    }

    /// The new value of each redone section, and the assistant's one-line summary.
    private static func apply(_ pending: Pending, to blocks: [String: [String: Any]], previousJob: String) -> ([String: [String: Any]], String) {
        var result: [String: [String: Any]] = [:]
        for section in pending.scope {
            guard let old = blocks[section] else { continue }
            var next = old
            next["revision"] = (old["revision"] as? Int ?? 0) + 1
            next["changed"] = true
            next["state"] = "decided"
            next["decided_at"] = "2026-10-09T10:05:00Z"
            if pending.kind == .update {
                redo(section, &next, edits: pending.edits)
            } else if let previous = old["previous"] as? [String: Any] {
                // An undo restores the previous value; the value just undone becomes the new "previous".
                if let summary = previous["summary"] { next["summary"] = summary } else { next.removeValue(forKey: "summary") }
                if let payload = previous["payload"] { next["payload"] = payload } else { next.removeValue(forKey: "payload") }
                next["skipped"] = previous["skipped"] as? Bool ?? false
            }
            next["previous"] = Self.previous(of: old, job: previousJob)
            result[section] = next
        }
        let names = pending.scope.map { section -> String in
            switch section {
            case "sfx": "sound effects"
            case "post_caption": "post caption"
            default: section
            }
        }
        let list = names.count > 1 ? names.dropLast().joined(separator: ", ") + " and " + names.last! : (names.first ?? "")
        let text: String
        switch pending.kind {
        case .update: text = "Done. I updated \(list). Everything else stayed as it was."
        case .undoSection: text = "Put \(list) back the way it was."
        case .undoAll: text = "Put everything back the way it was."
        }
        return (result, text)
    }

    private static func redo(_ section: String, _ block: inout [String: Any], edits: [[String: Any]]) {
        let rewrites = edits.filter { $0["kind"] as? String == "rewrite_text" }
        let mix = edits.first { $0["kind"] as? String == "set_mix" }
        switch section {
        case "title":
            var payload = block["payload"] as? [String: Any] ?? ["text": "A slow summer day"]
            let target = payload["bar_id"] as? String
            let edited = rewrites.first { ($0["target_id"] as? String) == target }?["text"] as? String
            payload["text"] = edited ?? "A slower summer day"
            block["payload"] = payload
            block["summary"] = payload["text"]
        case "captions":
            var payload = block["payload"] as? [String: Any] ?? [:]
            var lines = payload["lines"] as? [[String: Any]] ?? []
            if rewrites.isEmpty {
                // No hand edits: the model shortens two lines.
                let shorter = ["c2": "Salt, sun.", "c4": "Stay awhile."]
                lines = lines.map { line in
                    var line = line
                    if let id = line["id"] as? String, let text = shorter[id] { line["text"] = text }
                    return line
                }
                block["summary"] = "4 lines · shorter"
            } else {
                lines = lines.map { line in
                    var line = line
                    if let id = line["id"] as? String, let text = rewrites.first(where: { ($0["target_id"] as? String) == id })?["text"] as? String { line["text"] = text }
                    return line
                }
                block["summary"] = "4 lines · edited"
            }
            payload["lines"] = lines
            block["payload"] = payload
        case "music":
            var payload = block["payload"] as? [String: Any] ?? [:]
            if let mix {
                var levels = payload["mix"] as? [String: Any] ?? [:]
                if let music = mix["music_level"] { levels["music_level"] = music }
                if let original = mix["original_level"] { levels["original_level"] = original }
                payload["mix"] = levels
                block["summary"] = "\(payload["title"] as? String ?? "Music") · new mix"
            } else {
                payload["title"] = "Golden Hour"
                payload["artist"] = "Kira"
                payload["bpm"] = 96.0
                block["summary"] = "Golden Hour by Kira"
            }
            block["payload"] = payload
        case "sfx":
            block["summary"] = "5 sound effects"
            block.removeValue(forKey: "payload")
        case "look":
            block["summary"] = "Cool film · light grain"
            block["payload"] = ["chips": ["Cool film", "Light grain", "Serif titles"]]
        case "clips":
            block["summary"] = "6 clips · 22s · beach first"
        case "overlays":
            block["summary"] = "2 overlays"
            block["skipped"] = false
        default:
            block["summary"] = (block["summary"] as? String ?? "") + " (updated)"
        }
    }
}
#endif
