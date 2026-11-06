import Foundation

/// Thread identity comes from the scoped API/feed route; the wire snapshots do not contain it.
struct ReviewApprovalIdentity: Equatable {
    private enum Target: Equatable {
        case turn(String)
        case draft(String, Int)
    }
    let threadID: UUID
    private let target: Target

    init?(threadID: UUID, turnID: String) {
        guard Self.nonempty(turnID) else { return nil }
        self.threadID = threadID
        target = .turn(turnID)
    }

    init?(threadID: UUID, draftID: String, draftRevision: Int) {
        guard Self.nonempty(draftID), draftRevision >= 0 else { return nil }
        self.threadID = threadID
        target = .draft(draftID, draftRevision)
    }

    private func matches(threadID: UUID, turnID: String?, draftID: String?, draftRevision: Int?) -> Bool {
        guard self.threadID == threadID, let turnID, Self.nonempty(turnID) else { return false }
        switch target {
        case .turn(let expected): return turnID == expected
        case .draft(let expectedID, let expectedRevision): return draftID == expectedID && draftRevision == expectedRevision
        }
    }

    func matchesReply(_ event: ThreadEvent, threadID: UUID) -> Bool {
        matches(threadID: threadID, turnID: event.payload?["turn_id"]?.stringValue,
                draftID: event.payload?["draft_id"]?.stringValue, draftRevision: Self.revision(event.payload?["draft_revision"]))
    }

    func select(_ event: ThreadEvent, threadID: UUID) -> Selection? {
        guard event.eventType == "approval_requested",
              let turnID = event.payload?["turn_id"]?.stringValue,
              let draftID = event.payload?["draft_id"]?.stringValue, Self.nonempty(draftID),
              let revision = Self.revision(event.payload?["draft_revision"]),
              let approvalID = event.payload?["approval_id"]?.stringValue.flatMap(UUID.init(uuidString:)),
              matches(threadID: threadID, turnID: turnID, draftID: draftID, draftRevision: revision) else { return nil }
        return Selection(identity: self, approvalID: approvalID, turnID: turnID, draftID: draftID, draftRevision: revision)
    }

    struct Selection {
        fileprivate let identity: ReviewApprovalIdentity
        let approvalID: UUID
        var threadID: UUID { identity.threadID }
        private let turnID: String
        private let draftID: String
        private let draftRevision: Int

        fileprivate init(identity: ReviewApprovalIdentity, approvalID: UUID, turnID: String, draftID: String, draftRevision: Int) {
            self.identity = identity; self.approvalID = approvalID; self.turnID = turnID
            self.draftID = draftID; self.draftRevision = draftRevision
        }

        func matches(_ snapshot: ApprovalSnapshot, threadID: UUID) -> Bool {
            identity.matches(threadID: threadID, turnID: snapshot.turnID, draftID: snapshot.draftID, draftRevision: snapshot.draftRevision)
                && UUID(uuidString: snapshot.approvalID) == approvalID
                && snapshot.turnID == turnID && snapshot.draftID == draftID && snapshot.draftRevision == draftRevision
        }
    }

    private static func nonempty(_ value: String) -> Bool { !value.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty }
    private static func revision(_ value: JSONValue?) -> Int? {
        guard case .number(let number)? = value, let revision = Int(exactly: number), revision >= 0 else { return nil }
        return revision
    }
}
