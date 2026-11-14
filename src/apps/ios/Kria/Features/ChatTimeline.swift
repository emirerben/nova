import Foundation

/// Local rows retain their insertion position while a send and a poll overlap.
struct ChatPendingMessage: Identifiable {
    let id: UUID
    let content: String
    let clientEventID: String
    let afterSequence: Int
    let localOrder: Int
    /// KRI-374: the order this message confirms, so its card reads as answered while the send is in flight.
    let songOrder: SongOrderSubmission?
    /// KRI-588: the song part this message answers, so its card reads as answered while the send is in flight.
    let songRange: SongRangeAnswer?
    /// KRI-901: the mark this message carries, so its card reads as answered while the send is in flight.
    let protectedRegion: ProtectedRegionAnswer?
    /// KRI-723: the moments this message carries, so its card reads as answered while the send is in flight.
    let momentOrder: MomentOrderAnswer?

    init(content: String, clientEventID: String, afterSequence: Int, localOrder: Int = 0, id: UUID = UUID(), songOrder: SongOrderSubmission? = nil, songRange: SongRangeAnswer? = nil, protectedRegion: ProtectedRegionAnswer? = nil, momentOrder: MomentOrderAnswer? = nil) {
        self.songOrder = songOrder
        self.songRange = songRange
        self.protectedRegion = protectedRegion
        self.momentOrder = momentOrder
        self.id = id
        self.content = content
        self.clientEventID = clientEventID
        self.afterSequence = afterSequence
        self.localOrder = localOrder
    }

    var transcriptMessage: ChatTranscriptMessage {
        ChatTranscriptMessage(id: "pending-\(id.uuidString)", role: .user, content: content, isPending: true, songOrderAnswer: songOrder, songRangeAnswer: songRange, protectedRegionAnswer: protectedRegion, momentOrderAnswer: momentOrder)
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
        case thoughts([KriaThoughtSummary])
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
        uploads: [ChatPendingUpload] = [],
        thoughtSummaries: [String: [KriaThoughtSummary]] = [:]
    ) -> [ChatTimelineEntry] {
        let removals = removalSequences(events)
        var entries = ChatTranscriptHistory.merge([], with: events).compactMap { event -> ChatTimelineEntry? in
            if event.eventType == "media_added" {
                // A file the creator took out again is not part of the project any more, so its receipt goes too.
                guard let event = event.droppingRemovedMedia(removals) else { return nil }
                let id = uploadRecordID(event).map { "upload-\($0.uuidString)" } ?? event.id
                return ChatTimelineEntry(id: id, sequence: event.sequence, position: 0, content: .media(event))
            }
            guard !suppressedMessageIDs.contains(event.id), let message = ChatTranscriptMessage.from(event: event) else { return nil }
            return ChatTimelineEntry(id: message.id, sequence: event.sequence, position: 0, content: .message(message))
        }
        for event in events where event.role == "user" && event.eventType == "user_message" && !suppressedMessageIDs.contains(event.id) {
            guard let requestID = event.clientEventID, let summaries = thoughtSummaries[requestID], !summaries.isEmpty else { continue }
            entries.append(ChatTimelineEntry(id: "thought-\(requestID)", sequence: event.sequence, position: 1, content: .thoughts(summaries)))
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

    /// For each media id, the sequence of its latest `action_remove_media`. The log is append-only, so a removal is
    /// only ever visible as a later event, never as a change to the `media_added` event it undoes.
    static func removalSequences(_ events: [ThreadEvent]) -> [String: Int] {
        var latest: [String: Int] = [:]
        for event in events where event.eventType == "action_remove_media" {
            guard let id = event.payload?["media_id"]?.stringValue, !id.isEmpty else { continue }
            latest[id] = max(latest[id] ?? Int.min, event.sequence)
        }
        return latest
    }

    static func uploadRecordID(_ event: ThreadEvent) -> UUID? {
        guard event.eventType == "media_added", let clientID = event.clientEventID,
              clientID.hasPrefix("ios-attach-") else { return nil }
        return UUID(uuidString: String(clientID.dropFirst("ios-attach-".count)))
    }
}

/// One row of the transcript's lazy stack. A media receipt is one row per file, never one row for a whole run of
/// files: fifteen clips in a single row are far taller than the chat that is visible with the keyboard up, and
/// after Send the lazy stack lost its place and drew nothing until the reader dragged it (KRI-1351).
struct ChatTimelineRow: Identifiable {
    let id: String
    let entry: ChatTimelineEntry
    /// Which file of a media receipt this row draws; nil for every other kind of entry.
    let mediaIndex: Int?
    /// Directly under another file's row, so the two read as one strip. Their server identities stay separate;
    /// this is not an upload or submission batch.
    let joinsMediaAbove: Bool

    static func rows(_ entries: [ChatTimelineEntry]) -> [Self] {
        var rows: [Self] = []
        for entry in entries {
            guard case .media(let event) = entry.content else {
                rows.append(Self(id: entry.id, entry: entry, mediaIndex: nil, joinsMediaAbove: false))
                continue
            }
            for index in 0..<CreationAttachedMedia.parse(event.payload ?? [:]).count {
                rows.append(Self(id: index == 0 ? entry.id : "\(entry.id)#\(index)", entry: entry, mediaIndex: index,
                                 joinsMediaAbove: rows.last?.mediaIndex != nil))
            }
        }
        return rows
    }
}

enum ChatSubmission {
    static let mediaOnlyMessage = "Suggest an edit."

    /// A file that failed to attach never blocks sending (KRI-211): it is simply not part of the
    /// message. Only files still on their way hold it back, because sending now would leave them out.
    static func message(text: String, readyMediaCount: Int, pendingUploadCount: Int) -> String? {
        guard pendingUploadCount == 0 else { return nil }
        let trimmed = text.trimmingCharacters(in: .whitespacesAndNewlines)
        if !trimmed.isEmpty { return trimmed }
        return readyMediaCount > 0 ? mediaOnlyMessage : nil
    }
}

extension ThreadEvent {
    /// This `media_added` event without the files removed after it was logged; nil when none are left. A removal
    /// only hides a file added before it, so the same id attached again later shows again.
    func droppingRemovedMedia(_ removals: [String: Int]) -> ThreadEvent? {
        guard !removals.isEmpty, case .array(let media)? = payload?["media"] else { return self }
        let kept = media.filter { entry in
            guard case .object(let fields) = entry, let id = fields["media_id"]?.stringValue,
                  let removedAt = removals[id] else { return true }
            return removedAt < sequence
        }
        if kept.count == media.count { return self }
        if kept.isEmpty { return nil }
        var changed = payload ?? [:]
        changed["media"] = .array(kept)
        return ThreadEvent(id: id, sequence: sequence, revision: revision, role: role, eventType: eventType,
                           content: content, payload: changed, createdAt: createdAt, clientEventID: clientEventID)
    }
}
