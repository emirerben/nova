import Foundation

/// The last transcript of a chat, kept on disk so opening it shows its messages before the network answers.
///
/// Events only. The mutable projection (state, job, variants, signed media URLs) always comes from the server, so
/// nothing stale can be acted on; chat history is append-only, which is what makes showing it early safe.
/// Files live under `Application Support/Transcripts/<owner>/<threadID>.json`, are excluded from backup, and are
/// removed with the account, with the chat, or when the cache grows past `maxThreads` chats.
struct CachedTranscript: Codable, Sendable {
    static let currentVersion = 1

    var version = CachedTranscript.currentVersion
    let threadID: UUID
    var savedAt: Date
    var revision: Int
    var runtimeVersion: Int
    var events: [ThreadEvent]
}

actor TranscriptCache {
    static let shared = TranscriptCache()
    /// Enough for the scroll-back a creator does when reopening a chat; older history comes from the server.
    static let maxEvents = 300
    static let maxThreads = 40
    static let maxAge: TimeInterval = 30 * 24 * 3600

    private let root: URL
    private let fileManager = FileManager.default
    private var pendingWrites: [String: Task<Void, Never>] = [:]

    init(root: URL = URL.applicationSupportDirectory.appending(path: "Transcripts", directoryHint: .isDirectory)) {
        self.root = root
    }

    /// The saved transcript, or nil when there is none, it is too old, or it no longer decodes (a cache from another
    /// app version is dropped, never half-read).
    func load(owner: String, threadID: UUID, now: Date = .now) -> CachedTranscript? {
        let url = fileURL(owner: owner, threadID: threadID)
        guard let data = try? Data(contentsOf: url) else { return nil }
        guard let transcript = try? JSONDecoder().decode(CachedTranscript.self, from: data),
              transcript.version == CachedTranscript.currentVersion,
              transcript.threadID == threadID,
              now.timeIntervalSince(transcript.savedAt) < Self.maxAge else {
            try? fileManager.removeItem(at: url)
            return nil
        }
        return transcript
    }

    /// Debounced per chat: a burst of polls writes once, off the main thread.
    func save(owner: String, _ transcript: CachedTranscript, debounce: Duration = .seconds(1)) {
        let key = Self.key(owner: owner, threadID: transcript.threadID)
        pendingWrites[key]?.cancel()
        pendingWrites[key] = Task { [weak self] in
            try? await Task.sleep(for: debounce)
            guard !Task.isCancelled else { return }
            await self?.write(owner: owner, transcript, key: key)
        }
    }

    func remove(owner: String, threadID: UUID) {
        let key = Self.key(owner: owner, threadID: threadID)
        pendingWrites.removeValue(forKey: key)?.cancel()
        try? fileManager.removeItem(at: fileURL(owner: owner, threadID: threadID))
    }

    /// Sign-out and account deletion: nothing of this account's conversations stays on the device.
    func removeAccount(owner: String) {
        for key in pendingWrites.keys where key.hasPrefix(owner + "/") { pendingWrites.removeValue(forKey: key)?.cancel() }
        try? fileManager.removeItem(at: directory(owner: owner))
    }

    /// Wipes every account's transcripts (perf fixture reset).
    func removeAll() {
        for task in pendingWrites.values { task.cancel() }
        pendingWrites.removeAll()
        try? fileManager.removeItem(at: root)
    }

    private func write(owner: String, _ transcript: CachedTranscript, key: String) {
        pendingWrites.removeValue(forKey: key)
        var trimmed = transcript
        trimmed.events = Array(transcript.events.suffix(Self.maxEvents))
        guard let data = try? JSONEncoder().encode(trimmed) else { return }
        let directory = directory(owner: owner)
        do {
            try fileManager.createDirectory(at: directory, withIntermediateDirectories: true)
            try excludeFromBackup(root)
            try data.write(
                to: fileURL(owner: owner, threadID: transcript.threadID),
                options: [.atomic, .completeFileProtectionUntilFirstUserAuthentication]
            )
        } catch {
            return
        }
        prune(directory)
    }

    /// Keeps the `maxThreads` most recently written chats.
    private func prune(_ directory: URL) {
        let keys: [URLResourceKey] = [.contentModificationDateKey]
        guard let files = try? fileManager.contentsOfDirectory(at: directory, includingPropertiesForKeys: keys),
              files.count > Self.maxThreads else { return }
        let newestFirst = files.sorted {
            let first = (try? $0.resourceValues(forKeys: Set(keys)).contentModificationDate) ?? .distantPast
            let second = (try? $1.resourceValues(forKeys: Set(keys)).contentModificationDate) ?? .distantPast
            return first > second
        }
        for stale in newestFirst.dropFirst(Self.maxThreads) { try? fileManager.removeItem(at: stale) }
    }

    private func excludeFromBackup(_ url: URL) throws {
        var values = URLResourceValues()
        values.isExcludedFromBackup = true
        var mutable = url
        try mutable.setResourceValues(values)
    }

    private func directory(owner: String) -> URL { root.appending(path: owner, directoryHint: .isDirectory) }
    private func fileURL(owner: String, threadID: UUID) -> URL {
        directory(owner: owner).appending(path: threadID.uuidString + ".json")
    }
    private static func key(owner: String, threadID: UUID) -> String { owner + "/" + threadID.uuidString }
}
