import CryptoKit
import Foundation

enum OverlayGenerationMode: String, Codable, CaseIterable, Identifiable {
    case prompt, automatic
    var id: String { rawValue }
    var title: String { self == .prompt ? "Follow my prompt" : "Auto-generate overlays" }
}

struct OverlayPlanRequest: Encodable, Sendable {
    let jobID: String
    let variantID: String
    let baseGeneration: String
    let documentFingerprint: String
    let mode: OverlayGenerationMode
    let prompt: String
    let tasteNotes: String
    let editorSnapshot: [String: JSONValue]
    enum CodingKeys: String, CodingKey {
        case mode, prompt
        case jobID = "job_id", variantID = "variant_id", baseGeneration = "base_generation"
        case documentFingerprint = "document_fingerprint", tasteNotes = "taste_notes", editorSnapshot = "editor_snapshot"
    }
}

struct OverlayProposal: Codable, Identifiable, Sendable {
    enum Kind: String, Codable, Sendable { case image, text; case libraryAsset = "library_asset" }
    let id: String
    let kind: Kind
    var title: String
    var prompt: String
    var text: String = ""
    var assetID: String? = nil
    var startS: Double
    var endS: Double
    var xFrac: Double
    var yFrac: Double
    var scale: Double
    let reason: String

    func sourceRegistrationID(assetID: String) -> UUID {
        let identity = "kria.overlay-source/" + id + "/" + assetID
        var bytes = Array(SHA256.hash(data: Data(identity.utf8)).prefix(16))
        bytes[6] = (bytes[6] & 0x0f) | 0x80
        bytes[8] = (bytes[8] & 0x3f) | 0x80
        return UUID(uuid: (bytes[0], bytes[1], bytes[2], bytes[3], bytes[4], bytes[5], bytes[6], bytes[7],
                           bytes[8], bytes[9], bytes[10], bytes[11], bytes[12], bytes[13], bytes[14], bytes[15]))
    }

    func isValid(duration: Double) -> Bool {
        UUID(uuidString: id) != nil && [startS, endS, xFrac, yFrac, scale, duration].allSatisfy(\.isFinite)
            && startS >= 0 && endS - startS >= 0.1 && endS <= duration + 0.001
            && (0.05...0.95).contains(xFrac) && (0.05...0.95).contains(yFrac)
            && (0.1...0.5).contains(scale)
            && (kind != .text || (!text.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty && text.count <= 200))
            && (kind != .image || (!prompt.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty && prompt.count <= 1500))
            && (kind != .libraryAsset || assetID != nil)
    }

    enum CodingKeys: String, CodingKey {
        case id, kind, title, prompt, text, scale, reason
        case assetID = "asset_id", startS = "start_s", endS = "end_s", xFrac = "x_frac", yFrac = "y_frac"
    }
}

struct OverlayPlan: Codable, Sendable {
    var documentFingerprint: String
    let baseGeneration: String
    var overlays: [OverlayProposal]
    let message: String
    enum CodingKeys: String, CodingKey {
        case overlays, message
        case documentFingerprint = "document_fingerprint", baseGeneration = "base_generation"
    }
}

enum OverlayPlanFingerprint {
    static func make(_ snapshot: [String: JSONValue]) throws -> String {
        let encoder = JSONEncoder()
        encoder.outputFormatting = [.sortedKeys]
        return SHA256.hash(data: try encoder.encode(snapshot)).map { String(format: "%02x", $0) }.joined()
    }
}
