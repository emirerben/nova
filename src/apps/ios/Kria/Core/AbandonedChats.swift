import Foundation

/// Unsent composer text, kept per chat so leaving a chat never loses what was typed.
struct ChatDraftStore {
    private let defaults: UserDefaults
    private static let key = "kria.chat-drafts"

    init(defaults: UserDefaults = .standard) { self.defaults = defaults }

    func draft(for id: UUID) -> String { all[id.uuidString] ?? "" }

    /// Whitespace-only text is not a draft, so it clears the entry.
    func setDraft(_ text: String, for id: UUID) {
        var drafts = all
        if text.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty { drafts.removeValue(forKey: id.uuidString) }
        else { drafts[id.uuidString] = text }
        defaults.set(drafts, forKey: Self.key)
    }

    private var all: [String: String] { defaults.dictionary(forKey: Self.key) as? [String: String] ?? [:] }
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
