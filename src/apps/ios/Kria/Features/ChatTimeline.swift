import Foundation

/// Local rows retain their insertion position while a send and a poll overlap.
struct ChatPendingMessage: Identifiable {
    let id: UUID
    let content: String
    let clientEventID: String
    let afterSequence: Int
    let localOrder: Int

    init(content: String, clientEventID: String, afterSequence: Int, localOrder: Int = 0, id: UUID = UUID()) {
        self.id = id
        self.content = content
        self.clientEventID = clientEventID
        self.afterSequence = afterSequence
        self.localOrder = localOrder
    }

    var transcriptMessage: ChatTranscriptMessage {
        ChatTranscriptMessage(id: "pending-\(id.uuidString)", role: .user, content: content, isPending: true)
    }
}

enum ChatPendingReconciliation {
    static func acknowledged(_ pending: [ChatPendingMessage], events: [ThreadEvent]) -> Set<UUID> {
        var available = events.filter { $0.role == "user" && $0.eventType == "user_message" }
            .sorted { $0.sequence < $1.sequence }
        var result: Set<UUID> = []
        for message in pending.sorted(by: { $0.localOrder < $1.localOrder }) {
            let index = available.firstIndex { $0.clientEventID == message.clientEventID }
                ?? available.firstIndex {
                    $0.clientEventID == nil && $0.sequence > message.afterSequence
                        && normalized($0.content ?? "") == normalized(message.content)
                }
            if let index {
                result.insert(message.id)
                available.remove(at: index)
            }
        }
        return result
    }

    private static func normalized(_ value: String) -> String {
        value.split(whereSeparator: \.isWhitespace).joined(separator: " ")
    }
}

struct ChatTimelineStageAnchor {
    let id: String
    let afterSequence: Int
}

struct ChatPendingUpload: Identifiable {
    let id: UUID
    let afterSequence: Int
    let localOrder: Int
}

struct ChatTimelineEntry: Identifiable {
    enum Content {
        case message(ChatTranscriptMessage)
        case media(ThreadEvent)
        case pendingUpload(UUID)
        case stage
    }
    let id: String
    let sequence: Int
    let position: Int
    let content: Content
}

enum ChatTimeline {
    static func build(
        events: [ThreadEvent],
        pending: [ChatPendingMessage],
        stage: ChatTimelineStageAnchor?,
        suppressedMessageIDs: Set<String> = [],
        uploads: [ChatPendingUpload] = []
    ) -> [ChatTimelineEntry] {
        var entries = ChatTranscriptHistory.merge([], with: events).compactMap { event -> ChatTimelineEntry? in
            if event.eventType == "media_added" {
                let id = uploadRecordID(event).map { "upload-\($0.uuidString)" } ?? event.id
                return ChatTimelineEntry(id: id, sequence: event.sequence, position: 0, content: .media(event))
            }
            guard !suppressedMessageIDs.contains(event.id), let message = ChatTranscriptMessage.from(event: event) else { return nil }
            return ChatTimelineEntry(id: message.id, sequence: event.sequence, position: 0, content: .message(message))
        }
        if let stage {
            entries.append(ChatTimelineEntry(id: stage.id, sequence: stage.afterSequence, position: 1, content: .stage))
        }
        for message in pending {
            entries.append(ChatTimelineEntry(
                id: message.transcriptMessage.id, sequence: message.afterSequence,
                position: 2 + message.localOrder, content: .message(message.transcriptMessage)
            ))
        }
        let attachedRecordIDs = Set(events.compactMap(uploadRecordID))
        for upload in uploads where !attachedRecordIDs.contains(upload.id) {
            entries.append(ChatTimelineEntry(id: "upload-\(upload.id.uuidString)", sequence: upload.afterSequence,
                position: 2 + upload.localOrder, content: .pendingUpload(upload.id)))
        }
        return entries.sorted {
            if $0.sequence != $1.sequence { return $0.sequence < $1.sequence }
            if $0.position != $1.position { return $0.position < $1.position }
            return $0.id < $1.id
        }
    }

    static func uploadRecordID(_ event: ThreadEvent) -> UUID? {
        guard event.eventType == "media_added", let clientID = event.clientEventID,
              clientID.hasPrefix("ios-attach-") else { return nil }
        return UUID(uuidString: String(clientID.dropFirst("ios-attach-".count)))
    }
}

/// Adjacent media receipts share a strip for readability. Their individual
/// server identities remain intact; this is not an upload/submission batch.
struct ChatTimelineGroup: Identifiable {
    var entries: [ChatTimelineEntry]
    var id: String { entries[0].id }

    static func group(_ entries: [ChatTimelineEntry]) -> [Self] {
        var groups: [Self] = []
        for entry in entries {
            if case .media = entry.content, let last = groups.last,
               case .media = last.entries[0].content {
                groups[groups.count - 1].entries.append(entry)
            } else {
                groups.append(Self(entries: [entry]))
            }
        }
        return groups
    }
}

enum ChatSubmission {
    static let mediaOnlyMessage = "Suggest an edit."

    static func message(text: String, readyMediaCount: Int, pendingUploadCount: Int, hasUploadFailures: Bool) -> String? {
        guard pendingUploadCount == 0, !hasUploadFailures else { return nil }
        let trimmed = text.trimmingCharacters(in: .whitespacesAndNewlines)
        if !trimmed.isEmpty { return trimmed }
        return readyMediaCount > 0 ? mediaOnlyMessage : nil
    }
}
