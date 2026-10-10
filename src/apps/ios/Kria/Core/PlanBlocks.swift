import Foundation
import KriaMediaEngine

/// KRI-443: the live plan feed shown after Create. Pure value types and a reducer over the thread's
/// `plan_block` events, so the rules (display order, forward-only state, new job resets) are unit-testable
/// without a view. The wire contract lives in `docs/pipelines/live-plan-blocks.md`.

enum PlanSectionID: String, CaseIterable, Sendable {
    case title, clips, captions, music, sfx, overlays, look
    case postCaption = "post_caption"

    /// Fixed display order of the feed.
    var order: Int { Self.allCases.firstIndex(of: self) ?? 0 }

    var label: String {
        switch self {
        case .title: "Title"
        case .clips: "Clips"
        case .captions: "Captions"
        case .music: "Music"
        case .sfx: "Sound effects"
        case .overlays: "Overlays"
        case .look: "Look"
        case .postCaption: "Post caption"
        }
    }

    /// Display-only in v2: a post caption can be neither flagged nor edited (contract section 2).
    var isScopable: Bool { self != .postCaption }

    var systemImage: String {
        switch self {
        case .title: "textformat"
        case .clips: "film"
        case .captions: "captions.bubble"
        case .music: "music.note"
        case .sfx: "speaker.wave.2"
        case .overlays: "photo.on.rectangle"
        case .look: "sparkles"
        case .postCaption: "text.bubble"
        }
    }
}

/// `waiting < deciding < decided`; a section only ever moves forward.
enum PlanBlockState: Int, Comparable, Sendable {
    case waiting = 0, deciding, decided

    init?(wire: String) {
        switch wire {
        case "waiting": self = .waiting
        case "deciding": self = .deciding
        case "decided": self = .decided
        default: return nil
        }
    }

    static func < (lhs: Self, rhs: Self) -> Bool { lhs.rawValue < rhs.rawValue }
}

struct PlanBlock: Equatable, Identifiable, Sendable {
    let section: PlanSectionID
    var state: PlanBlockState
    var summary: String?
    var detail: String?
    /// Only the mode is known so far (the server has not produced the final choice).
    var intent: Bool
    var skipped: Bool
    var decidedAt: Date?
    /// Live plan & review contract v2 (KRI-439). All defaulted, so an old server's blocks decode as before.
    var revision: Int = 0
    var changed: Bool = false
    /// Structured section content; nil = missing or malformed, and the UI renders from `summary`/`detail`.
    var payload: PlanBlockPayload? = nil
    var previous: PlanPreviousValue? = nil
    /// Set by `GET /plan` only.
    var editable: Bool = false

    var id: String { section.rawValue }

    /// Skipped sections count as decided and read "Not used".
    var displaySummary: String {
        if skipped { return "Not used" }
        if let summary, !summary.isEmpty { return summary }
        return state == .decided ? "Decided" : ""
    }
}

struct PlanBlockFeedState: Equatable, Sendable {
    private(set) var jobID: String?
    private(set) var turnID: String?
    private(set) var blocksBySection: [PlanSectionID: PlanBlock] = [:]
    /// True once a `render_cancelled` event named this job.
    private(set) var isCancelled = false
    /// Sequence of the newest `plan_block` event applied, for "which block was decided last".
    private(set) var lastSequence = -1
    /// Section decided by the newest event that decided one, so the feed can glow and expand it.
    private(set) var newestDecided: PlanSectionID?
    /// Non-nil for a scoped update render (the sections the creator flagged).
    private(set) var scope: [PlanSectionID]?
    private(set) var previousJobID: String?

    static let empty = PlanBlockFeedState()

    var isEmpty: Bool { blocksBySection.isEmpty }
    var blocks: [PlanBlock] { blocksBySection.values.sorted { $0.section.order < $1.section.order } }
    var decidedCount: Int { blocksBySection.values.filter { $0.state == .decided }.count }
    var totalCount: Int { blocksBySection.count }
    var isComplete: Bool { !isEmpty && decidedCount == totalCount }
    var progress: Double { totalCount == 0 ? 0 : Double(decidedCount) / Double(totalCount) }

    /// Builds the feed for the most recent job named by a `plan_block` event.
    static func reduce(events: [ThreadEvent]) -> PlanBlockFeedState {
        var state = PlanBlockFeedState()
        for event in events.sorted(by: { $0.sequence < $1.sequence }) {
            state.apply(event)
        }
        return state
    }

    mutating func apply(_ event: ThreadEvent) {
        switch event.eventType {
        case "plan_block": applyPlanBlock(event)
        case "render_cancelled":
            if let job = event.payload?["job_id"]?.stringValue, job == jobID { isCancelled = true }
        default: break
        }
    }

    private mutating func applyPlanBlock(_ event: ThreadEvent) {
        guard let payload = event.payload, let job = payload["job_id"]?.stringValue, !job.isEmpty else { return }
        if job != jobID {
            // A new job (a re-render) starts a fresh feed.
            self = PlanBlockFeedState()
            jobID = job
        }
        if let turn = payload["turn_id"]?.stringValue, !turn.isEmpty { turnID = turn }
        lastSequence = max(lastSequence, event.sequence)
        if let rawScope = payload["scope"]?.arrayValue {
            scope = rawScope.compactMap { $0.stringValue.flatMap(PlanSectionID.init(rawValue:)) }
        }
        if let previousJob = payload["previous_job_id"]?.stringValue, !previousJob.isEmpty { previousJobID = previousJob }
        for raw in payload["blocks"]?.arrayValue ?? [] {
            guard case .object(let fields) = raw, let incoming = PlanBlock(json: fields) else { continue }
            merge(incoming)
        }
    }

    private mutating func merge(_ incoming: PlanBlock) {
        guard var current = blocksBySection[incoming.section] else {
            blocksBySection[incoming.section] = incoming
            if incoming.state == .decided { newestDecided = incoming.section }
            return
        }
        // A lower revision is an older value: ignore it whatever its state.
        if incoming.revision < current.revision { return }
        if incoming.revision > current.revision {
            // A newer value replaces the whole block (summary, detail, payload, previous, changed, skipped),
            // but a section still never moves backwards in state.
            guard incoming.state >= current.state else { return }
            blocksBySection[incoming.section] = incoming
            if incoming.state == .decided { newestDecided = incoming.section }
            return
        }
        // Forward only: an older or duplicate event can never move a section back.
        if incoming.state > current.state {
            blocksBySection[incoming.section] = incoming
            if incoming.state == .decided { newestDecided = incoming.section }
        } else if incoming.state == current.state, incoming.state != .waiting {
            // Same state: let a later event fill in or refine text, never blank it out.
            if let summary = incoming.summary { current.summary = summary }
            if let detail = incoming.detail { current.detail = detail }
            if let payload = incoming.payload { current.payload = payload }
            if let previous = incoming.previous { current.previous = previous }
            current.changed = current.changed || incoming.changed
            current.intent = incoming.intent
            current.skipped = current.skipped || incoming.skipped
            current.decidedAt = incoming.decidedAt ?? current.decidedAt
            blocksBySection[incoming.section] = current
        }
    }

}

extension JSONValue {
    /// Numbers arrive as `Double`; a whole one reads as an `Int`.
    var planInt: Int? {
        guard case .number(let value) = self, value.isFinite, value == value.rounded(), abs(value) < 1e9 else { return nil }
        return Int(value)
    }
}

extension PlanBlock {
    /// The ONE constructor for a wire block: the feed reducer and `PlanSnapshot` both use it, so event and
    /// snapshot blocks parse alike. Never throws on payload content: a malformed `payload` becomes nil and
    /// the UI falls back to `summary`/`detail`. Returns nil only for an unknown `section_id` or `state`.
    init?(json fields: [String: JSONValue]) {
        guard let section = fields["section_id"]?.stringValue.flatMap(PlanSectionID.init(rawValue:)),
              let state = fields["state"]?.stringValue.flatMap(PlanBlockState.init(wire:))
        else { return nil }
        self.init(
            section: section,
            state: state,
            summary: fields["summary"]?.stringValue,
            detail: fields["detail"]?.stringValue,
            intent: fields["intent"]?.boolValue ?? false,
            skipped: fields["skipped"]?.boolValue ?? false,
            decidedAt: fields["decided_at"]?.stringValue.flatMap(Self.parseDate),
            revision: fields["revision"]?.planInt ?? 0,
            changed: fields["changed"]?.boolValue ?? false,
            payload: PlanBlockPayload.decode(section: section, json: fields["payload"]),
            previous: Self.previous(from: fields["previous"], section: section),
            editable: fields["editable"]?.boolValue ?? false
        )
    }

    private static func previous(from json: JSONValue?, section: PlanSectionID) -> PlanPreviousValue? {
        guard case .object(let fields)? = json,
              let revision = fields["revision"]?.planInt,
              let jobID = fields["job_id"]?.stringValue
        else { return nil }
        return PlanPreviousValue(
            revision: revision, jobID: jobID, summary: fields["summary"]?.stringValue,
            payload: PlanBlockPayload.decode(section: section, json: fields["payload"]),
            skipped: fields["skipped"]?.boolValue ?? false
        )
    }

    static func parseDate(_ value: String) -> Date? {
        let fractional = ISO8601DateFormatter()
        fractional.formatOptions = [.withInternetDateTime, .withFractionalSeconds]
        if let date = fractional.date(from: value) { return date }
        return ISO8601DateFormatter().date(from: value)
    }
}

/// Where the iPhone's own build of the video has got to. On an iPhone account the server only plans (the
/// cloud decisions arrive at once); the video is built on the device, so the feed paces itself to these real
/// stages instead of showing every section decided up front. Mapped from `DeviceRenderPhase` and the
/// exporter's own progress callback; nothing here is timed.
enum DeviceBuildStage: Equatable, Sendable {
    /// Resolving and preparing the source clips on this iPhone.
    case preparing
    /// The one AVFoundation compose pass. `fraction` is the exporter's real 0...1 progress (nil = none reported).
    case rendering(fraction: Double?)
    /// The video exists on this iPhone (finished, syncing or synced).
    case finished

    /// nil for phases that are not an in-flight or finished build (cancelled, needs attention, superseded):
    /// the feed then falls back to the server's states and the status card takes over.
    init?(phase: DeviceRenderPhase, exportProgress: Double?) {
        switch phase {
        case .preparing: self = .preparing
        case .rendering: self = .rendering(fraction: exportProgress)
        case .localReady, .syncing, .synced: self = .finished
        case .cancelled, .needsAttention, .superseded: return nil
        }
    }
}

/// The order the device works through the sections while it builds, which is the order the cloud reports
/// them in (`render_execution_plan`): clips, music bed, overlays, text (title + captions + look), sound effects.
private let deviceWorkOrder: [PlanSectionID] = [.clips, .music, .overlays, .title, .captions, .look, .sfx, .postCaption]

extension PlanBlockFeedState {
    /// Displayed state of each section = min(server state, what the device has reached). The server stays the
    /// source of truth for summaries and values; the device only decides WHEN a section may show as decided, so
    /// nothing reads decided before the phone got there. `nil` (no device info) returns the server feed
    /// unchanged. Sections the server marked skipped resolve as soon as the device starts composing.
    func paced(by stage: DeviceBuildStage?) -> PlanBlockFeedState {
        guard let stage, !isEmpty else { return self }
        let active = deviceWorkOrder.filter { blocksBySection[$0].map { !$0.skipped } ?? false }
        func ceiling(_ block: PlanBlock) -> PlanBlockState {
            switch stage {
            case .finished: return .decided
            case .preparing:
                return !block.skipped && active.first == block.section ? .deciding : .waiting
            case .rendering(let fraction):
                if block.skipped { return .decided }
                guard let index = active.firstIndex(of: block.section) else { return .waiting }
                let position = min(1, max(0, fraction ?? 0)) * Double(active.count)
                if position >= Double(index + 1) { return .decided }
                return position >= Double(index) ? .deciding : .waiting
            }
        }
        var result = self
        for (section, block) in blocksBySection {
            var next = block
            let capped = min(block.state, ceiling(block))
            if capped != block.state {
                next.state = capped
                next.decidedAt = nil
            }
            result.blocksBySection[section] = next
        }
        result.newestDecided = deviceWorkOrder.last { section in
            guard let block = result.blocksBySection[section] else { return false }
            return block.state == .decided && !block.skipped
        }
        return result
    }
}

/// `POST /creation-threads/{id}/turns/{turn_id}/cancel-render` reply.
struct TurnCancelled: Decodable, Equatable, Sendable {
    let turnID: String
    let threadRevision: Int
    let status: String
    let approvalIDs: [String]

    init(turnID: String, threadRevision: Int, status: String, approvalIDs: [String] = []) {
        self.turnID = turnID; self.threadRevision = threadRevision; self.status = status; self.approvalIDs = approvalIDs
    }

    enum CodingKeys: String, CodingKey {
        case status
        case turnID = "turn_id", threadRevision = "thread_revision", approvalIDs = "approval_ids"
    }

    init(from decoder: Decoder) throws {
        let container = try decoder.container(keyedBy: CodingKeys.self)
        turnID = try container.decode(String.self, forKey: .turnID)
        threadRevision = try container.decode(Int.self, forKey: .threadRevision)
        status = try container.decode(String.self, forKey: .status)
        approvalIDs = try container.decodeIfPresent([String].self, forKey: .approvalIDs) ?? []
    }
}

/// What the chat shows for the live feed, decided from capabilities, runtime and the active job. Pure.
enum PlanFeedVisibility {
    /// The feed replaces the post-Create wait only when the server advertises it, the thread is v2 and the
    /// feed belongs to the job that is rendering now (a re-render must not show the previous job's feed).
    static func shows(
        capabilityEnabled: Bool?, runtimeVersion: Int, feed: PlanBlockFeedState, activeJobID: String?
    ) -> Bool {
        guard capabilityEnabled == true, runtimeVersion == 2, !feed.isEmpty, !feed.isCancelled else { return false }
        guard let activeJobID else { return true }
        return feed.jobID?.lowercased() == activeJobID.lowercased()
    }
}

extension APIError {
    var isConflict: Bool { if case .conflict = self { true } else { false } }
}
