import Foundation

/// KRI-443: the live plan feed shown after Create. Pure value types and a reducer over the thread's
/// `plan_block` events, so the rules (display order, forward-only state, new job resets) are unit-testable
/// without a view. The wire contract lives in `docs/pipelines/live-plan-blocks.md`.

enum PlanSectionID: String, CaseIterable, Sendable {
    case title, clips, captions, music, sfx, overlays, look

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
        }
    }

    var systemImage: String {
        switch self {
        case .title: "textformat"
        case .clips: "film"
        case .captions: "captions.bubble"
        case .music: "music.note"
        case .sfx: "speaker.wave.2"
        case .overlays: "photo.on.rectangle"
        case .look: "sparkles"
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
        for raw in payload["blocks"]?.arrayValue ?? [] {
            guard case .object(let fields) = raw,
                  let section = fields["section_id"]?.stringValue.flatMap(PlanSectionID.init(rawValue:)),
                  let incomingState = fields["state"]?.stringValue.flatMap(PlanBlockState.init(wire:))
            else { continue }
            let incoming = PlanBlock(
                section: section,
                state: incomingState,
                summary: fields["summary"]?.stringValue,
                detail: fields["detail"]?.stringValue,
                intent: fields["intent"]?.boolValue ?? false,
                skipped: fields["skipped"]?.boolValue ?? false,
                decidedAt: fields["decided_at"]?.stringValue.flatMap(Self.parseDate)
            )
            merge(incoming)
        }
    }

    private mutating func merge(_ incoming: PlanBlock) {
        guard var current = blocksBySection[incoming.section] else {
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
            current.intent = incoming.intent
            current.skipped = current.skipped || incoming.skipped
            current.decidedAt = incoming.decidedAt ?? current.decidedAt
            blocksBySection[incoming.section] = current
        }
    }

    private static func parseDate(_ value: String) -> Date? {
        let fractional = ISO8601DateFormatter()
        fractional.formatOptions = [.withInternetDateTime, .withFractionalSeconds]
        if let date = fractional.date(from: value) { return date }
        return ISO8601DateFormatter().date(from: value)
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
