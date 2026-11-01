import Foundation

enum TeleprompterTakeStatus: String, Codable, Sendable { case capturing, review, attaching, accepted, invalid }
enum TeleprompterTakeSlot: String, Codable, Sendable { case pending, candidate }

struct TeleprompterManifestLoad: Sendable {
    let manifests: [TeleprompterTakeManifest]
    let corruptedManifestNames: [String]

    var recoveryWarning: String? {
        guard !corruptedManifestNames.isEmpty else { return nil }
        return "A recorded take needs recovery. Your other saved takes are still available."
    }
}

struct TeleprompterTakeManifest: Codable, Equatable, Sendable, Identifiable {
    let id: UUID
    var fileName: String
    var status: TeleprompterTakeStatus
    var interrupted: Bool
    /// Why the take stopped. Optional so older manifests still decode.
    var stopReason: TakeStopReason?
    var capturedAt: Date
    var script: TeleprompterScriptDraft
    var intakeID: UUID
    var slot: TeleprompterTakeSlot
    /// Persisted before handing off to intake. Unknown recovered server state
    /// must be reconciled before another enqueue can be attempted.
    var intakeAttempted: Bool? = false
    /// Measured level of the recorded audio (KRI-653). Optional so older manifests still decode.
    var audioCheck: TakeAudioCheck?
    /// Whether the raw take reached the Photos library. Optional so older manifests still decode.
    var savedToPhotos: Bool?
    /// KRI-1119: the background the creator filmed in front of. The movie is still the raw camera. Optional so
    /// older manifests decode unchanged, and omitted from the JSON when nil.
    var speakerBackground: SpeakerBackgroundReference?

    init(id: UUID = UUID(), fileName: String? = nil, status: TeleprompterTakeStatus = .capturing,
         interrupted: Bool = false, capturedAt: Date = .now, script: TeleprompterScriptDraft, intakeID: UUID = UUID(),
         slot: TeleprompterTakeSlot = .candidate) {
        self.id = id
        self.fileName = fileName ?? "\(id.uuidString)-Recorded take.mov"
        self.status = status
        self.interrupted = interrupted
        self.capturedAt = capturedAt
        self.script = script
        self.intakeID = intakeID
        self.slot = slot
    }

    func fileURL(in directory: URL) -> URL { directory.appendingPathComponent(fileName, isDirectory: false) }
}

/// Account/project boundary for teleprompter records. Manifests only encode a
/// local filename, so decoding one can never redirect writes outside this root.
struct TeleprompterStore {
    let accountID: String
    let projectID: UUID
    let root: URL
    private let fileManager: FileManager

    init(accountID: String, projectID: UUID, root: URL? = nil, fileManager: FileManager = .default) {
        self.accountID = accountID
        self.projectID = projectID
        self.fileManager = fileManager
        self.root = root ?? fileManager.urls(for: .applicationSupportDirectory, in: .userDomainMask)[0]
            .appendingPathComponent("KriaTeleprompter", isDirectory: true)
    }

    var directory: URL { root.appendingPathComponent(Self.safeAccountComponent(accountID), isDirectory: true).appendingPathComponent(projectID.uuidString, isDirectory: true) }
    var takesDirectory: URL { directory.appendingPathComponent("takes", isDirectory: true) }
    var draftURL: URL { directory.appendingPathComponent("draft.json", isDirectory: false) }

    func takeFileURL(id: UUID, pathExtension: String = "mov") -> URL {
        let extensionComponent = pathExtension.unicodeScalars.allSatisfy { CharacterSet.alphanumerics.contains($0) }
            ? pathExtension : "mov"
        return takesDirectory.appendingPathComponent("\(id.uuidString).\(extensionComponent)", isDirectory: false)
    }

    func fileURL(for manifest: TeleprompterTakeManifest) -> URL {
        // Do not accept slash-containing names from a damaged or old manifest.
        let safe = manifest.fileName.split(separator: "/").last.map(String.init) ?? "\(manifest.id.uuidString).mov"
        return takesDirectory.appendingPathComponent(safe, isDirectory: false)
    }

    func loadDraft() throws -> TeleprompterScriptDraft? {
        guard fileManager.fileExists(atPath: draftURL.path) else { return nil }
        return try JSONDecoder().decode(TeleprompterScriptDraft.self, from: Data(contentsOf: draftURL))
    }

    func save(draft: TeleprompterScriptDraft) throws {
        try ensureDirectory()
        try atomicWrite(draft, to: draftURL)
    }

    func removeDraft() throws {
        guard fileManager.fileExists(atPath: draftURL.path) else { return }
        try fileManager.removeItem(at: draftURL)
    }

    func loadManifests() throws -> [TeleprompterTakeManifest] {
        guard fileManager.fileExists(atPath: takesDirectory.path) else { return [] }
        let urls = try fileManager.contentsOfDirectory(at: takesDirectory, includingPropertiesForKeys: nil)
            .filter { $0.pathExtension == "json" }
        return try urls.map { try JSONDecoder().decode(TeleprompterTakeManifest.self, from: Data(contentsOf: $0)) }
            .sorted { $0.capturedAt < $1.capturedAt }
    }

    /// Recovery reads each manifest independently. A corrupt candidate therefore
    /// cannot hide a valid older pending take after a relaunch.
    func loadManifestsRecoveringCorruption() throws -> TeleprompterManifestLoad {
        guard fileManager.fileExists(atPath: takesDirectory.path) else {
            return .init(manifests: [], corruptedManifestNames: [])
        }
        let urls = try fileManager.contentsOfDirectory(at: takesDirectory, includingPropertiesForKeys: nil)
            .filter { $0.pathExtension == "json" }
        var manifests: [TeleprompterTakeManifest] = []
        var corruptedManifestNames: [String] = []
        for url in urls {
            do { manifests.append(try JSONDecoder().decode(TeleprompterTakeManifest.self, from: Data(contentsOf: url))) }
            catch { corruptedManifestNames.append(url.lastPathComponent) }
        }
        return .init(manifests: manifests.sorted { $0.capturedAt < $1.capturedAt }, corruptedManifestNames: corruptedManifestNames)
    }

    func save(manifest: TeleprompterTakeManifest) throws {
        try ensureDirectory()
        try atomicWrite(manifest, to: manifestURL(for: manifest.id))
    }

    /// Removes metadata only by default. Failed metadata writes never delete a
    /// captured file, giving the review flow a chance to recover it.
    func remove(manifest: TeleprompterTakeManifest, removingFile: Bool = false) throws {
        let metadata = manifestURL(for: manifest.id)
        if fileManager.fileExists(atPath: metadata.path) { try fileManager.removeItem(at: metadata) }
        if removingFile {
            let file = fileURL(for: manifest)
            if fileManager.fileExists(atPath: file.path) { try fileManager.removeItem(at: file) }
        }
    }


    static func removeProject(accountID: String, projectID: UUID, root: URL? = nil, fileManager: FileManager = .default) throws {
        let store = Self(accountID: accountID, projectID: projectID, root: root, fileManager: fileManager)
        guard fileManager.fileExists(atPath: store.directory.path) else { return }
        try fileManager.removeItem(at: store.directory)
    }

    static func removeAccount(accountID: String, root: URL? = nil, fileManager: FileManager = .default) throws {
        let base = root ?? fileManager.urls(for: .applicationSupportDirectory, in: .userDomainMask)[0]
            .appendingPathComponent("KriaTeleprompter", isDirectory: true)
        let directory = base.appendingPathComponent(safeAccountComponent(accountID), isDirectory: true)
        guard fileManager.fileExists(atPath: directory.path) else { return }
        try fileManager.removeItem(at: directory)
    }

    private func manifestURL(for id: UUID) -> URL { takesDirectory.appendingPathComponent("\(id.uuidString).json", isDirectory: false) }
    private func ensureDirectory() throws { try fileManager.createDirectory(at: takesDirectory, withIntermediateDirectories: true) }
    private func atomicWrite<T: Encodable>(_ value: T, to url: URL) throws {
        let encoder = JSONEncoder(); encoder.outputFormatting = [.sortedKeys]
        try encoder.encode(value).write(to: url, options: .atomic)
    }

    private static func safeAccountComponent(_ accountID: String) -> String {
        let encoded = Data(accountID.utf8).base64EncodedString().replacingOccurrences(of: "+", with: "-").replacingOccurrences(of: "/", with: "_").replacingOccurrences(of: "=", with: "")
        return encoded.isEmpty ? "anonymous" : encoded
    }
}
