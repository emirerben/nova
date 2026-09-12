import Foundation

public enum MotionSceneValue: Codable, Equatable, Sendable {
    case object([String: MotionSceneValue]), array([MotionSceneValue]), string(String), number(Double), bool(Bool), null
    public init(from decoder: Decoder) throws {
        let container = try decoder.singleValueContainer()
        if container.decodeNil() { self = .null }
        else if let value = try? container.decode(Bool.self) { self = .bool(value) }
        else if let value = try? container.decode(Double.self) { self = .number(value) }
        else if let value = try? container.decode(String.self) { self = .string(value) }
        else if let value = try? container.decode([MotionSceneValue].self) { self = .array(value) }
        else { self = .object(try container.decode([String: MotionSceneValue].self)) }
    }
    public func encode(to encoder: Encoder) throws {
        var container = encoder.singleValueContainer()
        switch self {
        case .object(let value): try container.encode(value)
        case .array(let value): try container.encode(value)
        case .string(let value): try container.encode(value)
        case .number(let value): try container.encode(value)
        case .bool(let value): try container.encode(value)
        case .null: try container.encodeNil()
        }
    }
}

public struct MotionSceneProgram: Codable, Equatable, Sendable {
    private enum CodingKeys: String, CodingKey {
        case instances, runtimeHash, fontAssetID = "fontAssetId", imageAssetIDs = "imageAssetIds"
    }
    public var instances: [MotionSceneValue]
    public var runtimeHash: String
    public var fontAssetID: String
    public var imageAssetIDs: [String: String]
    public init(instances: [MotionSceneValue], runtimeHash: String, fontAssetID: String, imageAssetIDs: [String: String] = [:]) {
        self.instances = instances; self.runtimeHash = runtimeHash
        self.fontAssetID = fontAssetID; self.imageAssetIDs = imageAssetIDs
    }
    public init(from decoder: Decoder) throws {
        try rejectUnknownAssetFields(decoder, allowed: ["instances", "runtimeHash", "fontAssetId", "imageAssetIds"])
        let values = try decoder.container(keyedBy: CodingKeys.self)
        instances = try values.decode([MotionSceneValue].self, forKey: .instances)
        runtimeHash = try values.decode(String.self, forKey: .runtimeHash)
        fontAssetID = try values.decode(String.self, forKey: .fontAssetID)
        imageAssetIDs = try values.decodeIfPresent([String: String].self, forKey: .imageAssetIDs) ?? [:]
    }
    func validate(assets: Set<String>, manifest: RenderAssetManifest?) throws {
        guard let font = manifest?.assets.first(where: { $0.id == fontAssetID }),
              case .library(catalog: .font, catalogID: _, generation: _) = font.source else { throw RecipeError.invalidTimeline }
        guard !instances.isEmpty, instances.count <= 100, runtimeHash.count <= 200,
              assets.contains(fontAssetID), imageAssetIDs.count <= 100,
              imageAssetIDs.values.allSatisfy(assets.contains),
              try JSONEncoder().encode(instances).count <= 1_000_000 else { throw RecipeError.invalidTimeline }
    }
}
