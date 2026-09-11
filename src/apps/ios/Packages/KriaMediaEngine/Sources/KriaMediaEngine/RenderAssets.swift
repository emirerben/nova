import Foundation

private struct AssetWireKey: CodingKey {
    let stringValue: String
    let intValue: Int? = nil
    init?(stringValue: String) { self.stringValue = stringValue }
    init?(intValue: Int) { return nil }
}

func rejectUnknownAssetFields(_ decoder: Decoder, allowed: Set<String>) throws {
    let container = try decoder.container(keyedBy: AssetWireKey.self)
    guard Set(container.allKeys.map(\.stringValue)).isSubset(of: allowed) else { throw RenderAssetError.invalidManifest }
}

/// Recipe identities are independent of local paths and expiring download grants.
public struct RenderFingerprint: Codable, Equatable, Sendable {
    public let sha256: String
    public let byteCount: Int64
    public init(sha256: String, byteCount: Int64) { self.sha256 = sha256; self.byteCount = byteCount }
    private enum CodingKeys: String, CodingKey { case sha256, byteCount }
    public init(from decoder: Decoder) throws {
        try rejectUnknownAssetFields(decoder, allowed: ["sha256", "byteCount"])
        let c = try decoder.container(keyedBy: CodingKeys.self)
        sha256 = try c.decode(String.self, forKey: .sha256)
        byteCount = try c.decode(Int64.self, forKey: .byteCount)
        try validate()
    }
    public init(_ fingerprint: AssetFingerprint) throws {
        guard fingerprint.algorithm == "sha256" else { throw RenderAssetError.invalidManifest }
        self.init(sha256: fingerprint.hex, byteCount: fingerprint.byteCount)
        try validate()
    }
    public func validate() throws {
        guard sha256.utf8.count == 64, sha256.utf8.allSatisfy({ (48...57).contains($0) || (97...102).contains($0) }),
              byteCount > 0, byteCount <= 16 * 1024 * 1024 * 1024 else { throw RenderAssetError.invalidManifest }
    }
    var assetFingerprint: AssetFingerprint { AssetFingerprint(hex: sha256, byteCount: byteCount) }
}

public enum RenderAssetCatalog: String, Codable, Sendable { case music, soundEffect = "sound_effect", font, overlay }

public struct RenderAssetReference: Codable, Equatable, Sendable {
    public enum Source: Equatable, Sendable {
        case original(mediaID: String)
        case library(catalog: RenderAssetCatalog, catalogID: String, generation: String)
    }
    public let id: String
    public let fingerprint: RenderFingerprint
    public let source: Source
    public init(id: String, fingerprint: RenderFingerprint, source: Source) {
        self.id = id; self.fingerprint = fingerprint; self.source = source
    }
    private enum CodingKeys: String, CodingKey { case kind, id, fingerprint, mediaId, catalog, catalogId, generation }
    public init(from decoder: Decoder) throws {
        try rejectUnknownAssetFields(decoder, allowed: ["kind", "id", "fingerprint", "mediaId", "catalog", "catalogId", "generation"])
        let c = try decoder.container(keyedBy: CodingKeys.self)
        id = try c.decode(String.self, forKey: .id)
        fingerprint = try c.decode(RenderFingerprint.self, forKey: .fingerprint)
        switch try c.decode(String.self, forKey: .kind) {
        case "original":
            guard !c.contains(.catalog), !c.contains(.catalogId), !c.contains(.generation) else { throw RenderAssetError.invalidManifest }
            source = .original(mediaID: try c.decode(String.self, forKey: .mediaId))
        case "library":
            guard !c.contains(.mediaId) else { throw RenderAssetError.invalidManifest }
            source = .library(catalog: try c.decode(RenderAssetCatalog.self, forKey: .catalog),
                              catalogID: try c.decode(String.self, forKey: .catalogId),
                              generation: try c.decode(String.self, forKey: .generation))
        default: throw RenderAssetError.invalidManifest
        }
        try validate()
    }
    public func encode(to encoder: Encoder) throws {
        try validate()
        var c = encoder.container(keyedBy: CodingKeys.self)
        try c.encode(id, forKey: .id); try c.encode(fingerprint, forKey: .fingerprint)
        switch source {
        case .original(let mediaID):
            try c.encode("original", forKey: .kind); try c.encode(mediaID, forKey: .mediaId)
        case .library(let catalog, let catalogID, let generation):
            try c.encode("library", forKey: .kind); try c.encode(catalog, forKey: .catalog)
            try c.encode(catalogID, forKey: .catalogId); try c.encode(generation, forKey: .generation)
        }
    }
    public func validate() throws {
        try fingerprint.validate()
        let identifiers: [String]
        switch source {
        case .original(let mediaID): identifiers = [id, mediaID]
        case .library(_, let catalogID, let generation): identifiers = [id, catalogID, generation]
        }
        guard identifiers.allSatisfy({ !$0.isEmpty && $0.count <= 160 && $0.rangeOfCharacter(from: .whitespacesAndNewlines) == nil }) else {
            throw RenderAssetError.invalidManifest
        }
    }
}

public struct RenderAssetManifest: Codable, Equatable, Sendable {
    public let version: Int
    public let assets: [RenderAssetReference]
    public init(version: Int = 1, assets: [RenderAssetReference]) { self.version = version; self.assets = assets }
    private enum CodingKeys: String, CodingKey { case version, assets }
    public init(from decoder: Decoder) throws {
        try rejectUnknownAssetFields(decoder, allowed: ["version", "assets"])
        let c = try decoder.container(keyedBy: CodingKeys.self)
        version = try c.decode(Int.self, forKey: .version)
        assets = try c.decode([RenderAssetReference].self, forKey: .assets)
        try validate()
    }
    public func validate(references: Set<String>? = nil) throws {
        guard version == 1, assets.count <= 500, Set(assets.map(\.id)).count == assets.count else { throw RenderAssetError.invalidManifest }
        for (index, asset) in assets.enumerated() {
            try asset.validate()
            guard !assets.prefix(index).contains(where: { $0.source == asset.source && $0.fingerprint != asset.fingerprint }) else {
                throw RenderAssetError.invalidManifest
            }
        }
        if let references, !references.isSubset(of: Set(assets.map(\.id))) { throw RenderAssetError.invalidManifest }
    }
}

public enum RenderAssetError: Error, Equatable, Sendable {
    case invalidManifest, originalIdentityMismatch(String), missingLibraryAsset(String), changedLibraryAsset(String)
}

/// Content-addressed cache. Only explicitly authorized library downloads may be
/// passed to install; download authorization belongs to the API client. Never
/// accepts original uploads or resolves an original through this cache.
public actor RenderLibraryCache {
    public let root: URL
    public init(root: URL) { self.root = root }

    public func resolve(_ asset: RenderAssetReference) throws -> URL {
        let url = try location(asset)
        guard FileManager.default.fileExists(atPath: url.path) else { throw RenderAssetError.missingLibraryAsset(asset.id) }
        guard try SHA256Fingerprinter().fingerprint(file: url) == asset.fingerprint.assetFingerprint else {
            throw RenderAssetError.changedLibraryAsset(asset.id)
        }
        return url
    }

    public func install(downloadedFile: URL, for asset: RenderAssetReference) throws -> URL {
        let destination = try location(asset)
        let fm = FileManager.default
        try fm.createDirectory(at: root, withIntermediateDirectories: true)
        // Copy before verifying: the caller can release its temporary file as
        // soon as this returns, and verification applies to the stored bytes.
        let staging = root.appendingPathComponent(UUID().uuidString + ".partial")
        defer { try? fm.removeItem(at: staging) }
        try fm.copyItem(at: downloadedFile, to: staging)
        guard try SHA256Fingerprinter().fingerprint(file: staging) == asset.fingerprint.assetFingerprint else {
            throw RenderAssetError.changedLibraryAsset(asset.id)
        }
        if fm.fileExists(atPath: destination.path) {
            // Preserve valid cached bytes; repair corrupt entries only after
            // the replacement has passed its complete fingerprint check.
            if (try? resolve(asset)) != nil { return destination }
            _ = try fm.replaceItemAt(destination, withItemAt: staging)
        } else { try fm.moveItem(at: staging, to: destination) }
        return try resolve(asset)
    }

    public func installBundledFont(_ asset: RenderAssetReference, directory: URL) throws -> URL {
        guard case .library(catalog: .font, catalogID: let name, generation: let generation) = asset.source,
              generation == asset.fingerprint.sha256,
              !name.contains("/"), !name.contains("\\"),
              ["ttf", "otf"].contains((name as NSString).pathExtension.lowercased()) else {
            throw RenderAssetError.invalidManifest
        }
        let source = directory.appendingPathComponent(name).resolvingSymlinksInPath()
        guard source.deletingLastPathComponent().path == directory.resolvingSymlinksInPath().path else {
            throw RenderAssetError.invalidManifest
        }
        return try install(downloadedFile: source, for: asset)
    }

    private func location(_ asset: RenderAssetReference) throws -> URL {
        try asset.validate()
        guard case .library = asset.source else { throw RenderAssetError.invalidManifest }
        let url = root.appendingPathComponent(asset.fingerprint.sha256 + "-" + String(asset.fingerprint.byteCount))
        guard url.resolvingSymlinksInPath().deletingLastPathComponent().path == root.resolvingSymlinksInPath().path else {
            throw RenderAssetError.invalidManifest
        }
        return url
    }
}

public struct PortableAssetResolver: Sendable {
    public let originals: SourceAssetStore
    public let library: RenderLibraryCache
    public init(originals: SourceAssetStore, library: RenderLibraryCache) { self.originals = originals; self.library = library }
    public func resolve(_ manifest: RenderAssetManifest) async throws -> [String: URL] {
        try manifest.validate()
        var result: [String: URL] = [:]
        // Compare the recipe's source identity with the device binding before
        // opening bytes. A valid but unrelated local original must not render.
        let store = originals
        let originalReferences = manifest.assets.filter { if case .original = $0.source { return true }; return false }
        let originalURLs = try await Task.detached {
            let bindings = try store.bindings()
            var ids = Set<String>()
            for asset in originalReferences {
                guard case .original(let mediaID) = asset.source else { continue }
                guard let binding = bindings.first(where: { $0.mediaID == mediaID }) else { throw SourceAssetError.missingOriginal(mediaID) }
                guard binding.original.fingerprint == asset.fingerprint.assetFingerprint else { throw RenderAssetError.originalIdentityMismatch(asset.id) }
                ids.insert(mediaID)
            }
            return try store.resolve(mediaIDs: ids)
        }.value
        for asset in manifest.assets {
            try Task.checkCancellation()
            switch asset.source {
            case .original(let mediaID): result[asset.id] = originalURLs[mediaID]
            case .library: result[asset.id] = try await library.resolve(asset)
            }
        }
        return result
    }
}
