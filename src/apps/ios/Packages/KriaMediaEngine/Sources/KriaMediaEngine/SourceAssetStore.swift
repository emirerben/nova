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
        let root = project.originals.resolvingSymlinksInPath().path + "/"
        guard url.path.hasPrefix(root) else { throw SourceAssetError.invalidBinding }
        return url
    }
}
