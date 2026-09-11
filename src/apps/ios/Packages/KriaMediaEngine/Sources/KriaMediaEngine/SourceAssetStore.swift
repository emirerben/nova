import Foundation

/// Server media IDs resolve through this device-owned manifest, never through a recipe's path.
/// Proxies are deliberately absent from the export resolver.
public struct SourceAssetBinding: Codable, Equatable, Sendable {
    public var mediaID: String
    public var original: MediaAsset
    public init(mediaID: String, original: MediaAsset) {
        self.mediaID = mediaID
        self.original = original
    }
}

public enum SourceAssetError: Error, Equatable, Sendable {
    case missingOriginal(String), changedOriginal(String), invalidBinding, conflictingBinding(String)
}

/// Serialize writes through one owner (the upload/render coordinator).
public struct SourceAssetStore: Sendable {
    private static let manifestWriteLock = NSLock()
    public let project: ProjectDirectory
    public init(project: ProjectDirectory) { self.project = project }
    private var manifest: URL { project.root.appendingPathComponent("source-assets.json") }

    public func bindings() throws -> [SourceAssetBinding] {
        guard FileManager.default.fileExists(atPath: manifest.path) else { return [] }
        return try JSONDecoder().decode([SourceAssetBinding].self, from: Data(contentsOf: manifest))
    }

    public func bind(mediaID: String, original: MediaAsset) throws {
        guard !mediaID.isEmpty, original.fingerprint?.algorithm == "sha256",
              original.fingerprint?.hex.count == 64, (original.fingerprint?.byteCount ?? 0) > 0 else {
            throw SourceAssetError.invalidBinding
        }
        _ = try originalURL(original)
        Self.manifestWriteLock.lock()
        defer { Self.manifestWriteLock.unlock() }
        var records = try bindings()
        let binding = SourceAssetBinding(mediaID: mediaID, original: original)
        if let existing = records.first(where: { $0.mediaID == mediaID }) {
            guard existing == binding else { throw SourceAssetError.conflictingBinding(mediaID) }
            return
        }
        records.append(binding)
        try project.createIfNeeded()
        try JSONEncoder().encode(records).write(to: manifest, options: .atomic)
    }

    /// A creator-selected replacement must be the exact approved original.
    /// Call off the main actor: verification streams the complete file.
    public func relink(_ reference: RenderAssetReference, original: MediaAsset) throws {
        try reference.validate()
        guard case .original(let mediaID) = reference.source,
              original.fingerprint == reference.fingerprint.assetFingerprint else {
            throw SourceAssetError.invalidBinding
        }
        let url = try originalURL(original)
        guard try SHA256Fingerprinter().fingerprint(file: url) == original.fingerprint else {
            throw SourceAssetError.changedOriginal(mediaID)
        }
        Self.manifestWriteLock.lock()
        defer { Self.manifestWriteLock.unlock() }
        var records = try bindings()
        records.removeAll { $0.mediaID == mediaID }
        records.append(SourceAssetBinding(mediaID: mediaID, original: original))
        try project.createIfNeeded()
        try JSONEncoder().encode(records).write(to: manifest, options: .atomic)
    }

    public func resolve(mediaIDs: Set<String>) throws -> [String: URL] {
        let records = try bindings()
        var result: [String: URL] = [:]
        for id in mediaIDs {
            guard let binding = records.first(where: { $0.mediaID == id }) else {
                throw SourceAssetError.missingOriginal(id)
            }
            let url = try originalURL(binding.original)
            guard FileManager.default.fileExists(atPath: url.path) else {
                throw SourceAssetError.missingOriginal(id)
            }
            guard try SHA256Fingerprinter().fingerprint(file: url) == binding.original.fingerprint else {
                throw SourceAssetError.changedOriginal(id)
            }
            result[id] = url
        }
        return result
    }

    private func originalURL(_ asset: MediaAsset) throws -> URL {
        let parts = asset.relativePath.split(separator: "/", omittingEmptySubsequences: false)
        guard parts.count == 2, parts.first == "originals", parts.last != "..",
              parts.last != ".", !asset.relativePath.contains("\\") else {
            throw SourceAssetError.invalidBinding
        }
        let url = project.root.appendingPathComponent(asset.relativePath).resolvingSymlinksInPath()
        let root = project.root.resolvingSymlinksInPath().appendingPathComponent("originals").path + "/"
        guard url.path.hasPrefix(root) else { throw SourceAssetError.invalidBinding }
        return url
    }
}
