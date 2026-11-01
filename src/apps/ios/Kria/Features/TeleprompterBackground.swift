import Foundation

/// KRI-1119: Talk-to-camera green screen. The creator picks a photo or video to see themselves in front of
/// while filming. The take itself is always the RAW front camera; this only names the picked file.
enum SpeakerBackgroundKind: String, Codable, Sendable { case image, video }

/// Names a background file that lives in the take store's `backgrounds/` folder. `id` is stable for one
/// selection so the upload can be retried without minting a second visual.
struct SpeakerBackgroundReference: Codable, Equatable, Sendable, Identifiable {
    let id: UUID
    var fileName: String
    var kind: SpeakerBackgroundKind
}

/// The project-level choice, persisted next to `draft.json` so a retake keeps it.
struct TeleprompterBackgroundChoice: Codable, Equatable, Sendable {
    var reference: SpeakerBackgroundReference
    /// Set once the file was handed to the visual upload, so later takes in this project do not upload it again.
    var uploaded: Bool? = nil
    /// Set once the server accepted the statement of which visual is the background (or turned the feature off).
    var stated: Bool? = nil
}

extension TeleprompterStore {
    var backgroundsDirectory: URL { directory.appendingPathComponent("backgrounds", isDirectory: true) }
    var backgroundChoiceURL: URL { directory.appendingPathComponent("background.json", isDirectory: false) }

    func backgroundFileURL(for reference: SpeakerBackgroundReference) -> URL {
        // A damaged choice file must not be able to point outside the backgrounds folder.
        let safe = reference.fileName.split(separator: "/").last.map(String.init) ?? "\(reference.id.uuidString)"
        return backgroundsDirectory.appendingPathComponent(safe, isDirectory: false)
    }

    func loadBackgroundChoice() throws -> TeleprompterBackgroundChoice? {
        guard FileManager.default.fileExists(atPath: backgroundChoiceURL.path) else { return nil }
        return try JSONDecoder().decode(TeleprompterBackgroundChoice.self, from: Data(contentsOf: backgroundChoiceURL))
    }

    func save(backgroundChoice: TeleprompterBackgroundChoice) throws {
        try FileManager.default.createDirectory(at: directory, withIntermediateDirectories: true)
        let encoder = JSONEncoder(); encoder.outputFormatting = [.sortedKeys]
        try encoder.encode(backgroundChoice).write(to: backgroundChoiceURL, options: .atomic)
    }

    /// A background the server was already told about was cleared here: the clear still has to reach the server. Holds the
    /// idempotency key of that one clear, so a retry replays instead of applying twice.
    var serverClearURL: URL { directory.appendingPathComponent("background-clear.txt", isDirectory: false) }
    var pendingServerClearEventID: String? { try? String(contentsOf: serverClearURL, encoding: .utf8) }
    func markServerClearPending(eventID: String) throws {
        try FileManager.default.createDirectory(at: directory, withIntermediateDirectories: true)
        try Data(eventID.utf8).write(to: serverClearURL, options: .atomic)
    }
    func removeServerClearPending() { try? FileManager.default.removeItem(at: serverClearURL) }

    func removeBackgroundChoice() throws {
        guard FileManager.default.fileExists(atPath: backgroundChoiceURL.path) else { return }
        try FileManager.default.removeItem(at: backgroundChoiceURL)
    }

    /// Copies a picked file next to the takes. The source is a transient picker file, so it is copied, not moved.
    func importBackground(from source: URL, kind: SpeakerBackgroundKind) throws -> SpeakerBackgroundReference {
        try FileManager.default.createDirectory(at: backgroundsDirectory, withIntermediateDirectories: true)
        let id = UUID()
        let ext = source.pathExtension.unicodeScalars.allSatisfy { CharacterSet.alphanumerics.contains($0) } && !source.pathExtension.isEmpty
            ? source.pathExtension.lowercased() : (kind == .video ? "mov" : "jpg")
        let reference = SpeakerBackgroundReference(id: id, fileName: "\(id.uuidString).\(ext)", kind: kind)
        try FileManager.default.copyItem(at: source, to: backgroundFileURL(for: reference))
        return reference
    }

    /// Deletes every background file no live choice or take still names.
    func pruneBackgrounds(keeping names: Set<String>) {
        guard let files = try? FileManager.default.contentsOfDirectory(at: backgroundsDirectory, includingPropertiesForKeys: nil) else { return }
        for file in files where !names.contains(file.lastPathComponent) { try? FileManager.default.removeItem(at: file) }
    }
}
