import Foundation
import Combine

/// Unsent composer text, kept per chat so leaving a chat never loses what was typed.
@MainActor
final class ChatDraftStore: ObservableObject {
    private let defaults: UserDefaults
    private static let key = "kria.chat-drafts"
    @Published private(set) var revision = 0
    private var attempts: [UUID: String] = [:]
    private var resolutions: [UUID: ChatSubmissionResolution] = [:]

    init(defaults: UserDefaults = .standard) { self.defaults = defaults }

    func draft(for id: UUID) -> String { all[id.uuidString] ?? "" }
    func isSubmitting(for id: UUID) -> Bool { attempts[id] != nil }
    func resolution(for id: UUID) -> ChatSubmissionResolution? { resolutions[id] }

    func beginSubmission(_ receipt: ChatSendReceipt, for id: UUID) throws -> Bool {
        guard attempts[id] == nil, submission(for: id)?.phase != .accepted else { return false }
        attempts[id] = receipt.clientEventID
        resolutions.removeValue(forKey: id)
        do { try setSubmission(receipt, for: id) }
        catch { attempts.removeValue(forKey: id); revision += 1; throw error }
        revision += 1
        return true
    }

    func endSubmission(clientEventID: String, for id: UUID) {
        guard attempts[id] == clientEventID else { return }
        attempts.removeValue(forKey: id)
        revision += 1
    }

    func submission(for id: UUID) -> ChatSendReceipt? {
        guard let data = defaults.data(forKey: "kria.chat-submission.\(id.uuidString)") else { return nil }
        return try? JSONDecoder().decode(ChatSendReceipt.self, from: data)
    }

    func setSubmission(_ receipt: ChatSendReceipt?, for id: UUID) throws {
        let current = submission(for: id)
        guard receipt != current else { return }
        let key = "kria.chat-submission.\(id.uuidString)"
        if let receipt {
            if let current,
               current.clientEventID != receipt.clientEventID || (current.phase == .accepted && receipt.phase != .accepted) {
                throw ChatSubmissionStorageError.alreadyPending
            }
            defaults.set(try JSONEncoder().encode(receipt), forKey: key)
        } else {
            defaults.removeObject(forKey: key)
            if let current { resolutions[id] = .recovered(current) }
        }
        revision += 1
    }

    static func recoveredDraft(submitted: String, current: String) -> String {
        guard !submitted.isEmpty, submitted != current, !current.hasPrefix(submitted + "\n\n") else { return current }
        return current.isEmpty ? submitted : submitted + "\n\n" + current
    }

    func recoverSubmission(clientEventID: String, for id: UUID) -> String? {
        guard let receipt = submission(for: id), receipt.clientEventID == clientEventID,
              receipt.phase != .accepted else { return nil }
        let recovered = Self.recoveredDraft(submitted: receipt.draft, current: draft(for: id))
        _ = updateDraft(recovered, for: id)
        discardSubmission(clientEventID: clientEventID, for: id)
        return recovered
    }

    func acknowledgeSubmission(clientEventID: String, for id: UUID) {
        guard let receipt = submission(for: id), receipt.clientEventID == clientEventID else { return }
        defaults.removeObject(forKey: "kria.chat-submission.\(id.uuidString)")
        resolutions[id] = .acknowledged(receipt)
        revision += 1
    }

    func discardSubmission(clientEventID: String, for id: UUID) {
        guard let receipt = submission(for: id), receipt.clientEventID == clientEventID,
              receipt.phase != .accepted else { return }
        try? setSubmission(nil, for: id)
    }

    /// Whitespace-only text is not a draft, so it clears the entry.
    func setDraft(_ text: String, for id: UUID) {
        _ = updateDraft(text, for: id)
    }

    private func updateDraft(_ text: String, for id: UUID) -> Bool {
        var drafts = all
        if text.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty { drafts.removeValue(forKey: id.uuidString) }
        else { drafts[id.uuidString] = text }
        guard drafts != all else { return false }
        defaults.set(drafts, forKey: Self.key)
        return true
    }

    private var all: [String: String] { defaults.dictionary(forKey: Self.key) as? [String: String] ?? [:] }
}

enum ChatSubmissionResolution {
    case acknowledged(ChatSendReceipt)
    case recovered(ChatSendReceipt)
}

/// Chats known to hold a user message. That never stops being true, so a chat in this set can never be an
/// abandoned-chat candidate, and the full read the sweep (and leaving a chat) makes to find out is skipped.
struct ChatKeeperStore {
    private let defaults: UserDefaults
    private static let key = "kria.chats-with-user-messages"
    /// Oldest entries fall off first; a forgotten chat only costs one more read.
    static let limit = 1000

    init(defaults: UserDefaults = .standard) { self.defaults = defaults }

    func contains(_ id: UUID) -> Bool { all.contains(id.uuidString) }

    func insert(_ id: UUID) {
        var ids = all
        guard !ids.contains(id.uuidString) else { return }
        ids.append(id.uuidString)
        defaults.set(Array(ids.suffix(Self.limit)), forKey: Self.key)
    }

    private var all: [String] { defaults.stringArray(forKey: Self.key) ?? [] }
}

enum AbandonedChat {
    /// A chat is abandoned when nothing was ever sent, nothing is staged, and the composer is empty.
    /// Staged media counts as a draft, same as unsent text.
    static func isEmpty(thread: CreationThread, draft: String, hasPendingUploads: Bool) -> Bool {
        guard thread.summary.status == .draft, !hasPendingUploads else { return false }
        guard draft.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty else { return false }
        guard !thread.events.contains(where: { $0.role == "user" }) else { return false }
        guard attachedVideoClipCount(in: thread.state ?? [:]) == 0, thread.visualCount == 0 else { return false }
        return !thread.hasStagedMedia
    }
}

extension CreationThread {
    /// Any staged media role (photos, audio, visuals) the server reports for this thread.
    var hasStagedMedia: Bool {
        guard let capabilities = mediaCapabilities else { return false }
        return capabilities.values.contains { value in
            if case .number(let count) = value.objectValue?["current"] { return count > 0 }
            return false
        }
    }
}

enum ChatSubmissionStorageError: Error { case alreadyPending }

struct ChatSendReceipt: Codable, Equatable, Sendable {
    enum Phase: String, Codable { case preparing, unconfirmed, accepted }
    var phase: Phase = .preparing
    var requestBody: Data?
    var turnID: String?
    let message: String
    let clientEventID: String
    var expectedRevision: Int
    let afterSequence: Int
    let draft: String
    let runtimeVersion: Int
}

extension ChatSendReceipt {
    static func isRejected(_ error: Error) -> Bool {
        guard let error = error as? APIError else { return false }
        switch error {
        case .requestFailed(let status, _): return (400..<500).contains(status) && status != 408
        case .sessionExpired, .nativeUpdateRequired, .unsupported: return true
        default: return false
        }
    }
}
