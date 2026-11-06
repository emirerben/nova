import Foundation

enum GeneratedMediaCapability: String, Codable, Sendable {
    case image
    case imageEdit = "image_edit"
    case narration
}

struct GeneratedMediaRequest: Codable, Sendable {
    let clientEventID: String
    let capability: GeneratedMediaCapability
    let prompt: String
    var transparentBackground: Bool = false
    var editableTitle: String = ""
    var sourceAssetID: String? = nil
    var sourceGeneration: String? = nil
    var scriptID: String? = nil
    var scriptVersion: Int? = nil
    var approvedScriptSHA256: String? = nil

    enum CodingKeys: String, CodingKey {
        case capability, prompt
        case transparentBackground = "transparent_background"
        case clientEventID = "client_event_id", editableTitle = "editable_title"
        case sourceAssetID = "source_asset_id", sourceGeneration = "source_generation"
        case scriptID = "script_id"
        case scriptVersion = "script_version", approvedScriptSHA256 = "approved_script_sha256"
    }
}

struct GeneratedMediaAcceptance: Encodable, Sendable {
    var scriptID: String? = nil
    var scriptVersion: Int? = nil
    var approvedScriptSHA256: String? = nil
    enum CodingKeys: String, CodingKey {
        case scriptID = "script_id"
        case scriptVersion = "script_version", approvedScriptSHA256 = "approved_script_sha256"
    }
}

struct GeneratedMediaOperation: Decodable, Sendable {
    enum State: String, Decodable, Sendable {
        case pending, submitting, ready, unknown, failed
        case providerComplete = "provider_complete"
    }
    let id: String
    let state: State
    let capability: GeneratedMediaCapability
    let previewURL: URL?
    let sourceURL: URL?
    let assetID: String?
    let errorCode: String?
    let retryable: Bool
    let sourceAssetID: String?
    let sourceGeneration: String?
    let scriptID: String?
    let scriptVersion: Int?
    let approvedScriptSHA256: String?

    func matchesApproval(sourceAssetID: String?, sourceGeneration: String?, script: GeneratedNarrationScript?) -> Bool {
        switch capability {
        case .image: return true
        case .imageEdit:
            return self.sourceAssetID != nil && self.sourceGeneration != nil
                && self.sourceAssetID == sourceAssetID && self.sourceGeneration == sourceGeneration
        case .narration:
            guard let script else { return false }
            return scriptID == script.scriptID && scriptVersion == script.scriptVersion
                && approvedScriptSHA256 == script.approvedScriptSHA256
        }
    }

    var shouldPoll: Bool { state == .pending || state == .submitting || state == .providerComplete }
    var canAccept: Bool {
        state == .ready && (capability == .narration || assetID != nil) && (previewURL ?? sourceURL) != nil
    }
    var canRetryStorage: Bool { state == .providerComplete && retryable }

    enum CodingKeys: String, CodingKey {
        case id, state, capability, retryable
        case sourceAssetID = "source_asset_id", sourceGeneration = "source_generation"
        case scriptID = "script_id", scriptVersion = "script_version", approvedScriptSHA256 = "approved_script_sha256"
        case previewURL = "preview_url", sourceURL = "source_url", assetID = "asset_id", errorCode = "error_code"
    }
}

struct GeneratedNarrationScript: Decodable, Sendable {
    let scriptID: String
    let scriptVersion: Int
    let text: String
    let approvedScriptSHA256: String

    func matches(_ card: VoiceoverScript) -> Bool {
        card.isDraft && scriptID == card.scriptID && scriptVersion == card.revision && text == card.text
    }
    enum CodingKeys: String, CodingKey {
        case text
        case scriptID = "script_id", scriptVersion = "script_version", approvedScriptSHA256 = "approved_script_sha256"
    }
}

struct GeneratedMediaRecovery: Codable, Equatable {
    let clientEventID: String
    var operationID: String?
}

enum GeneratedMediaRequestFailure {
    static func isDefiniteRejection(_ error: Error) -> Bool {
        guard let error = error as? APIError else { return false }
        switch error {
        case .conflict, .sessionExpired, .nativeUpdateRequired: return true
        case .requestFailed(let status, _): return (400..<500).contains(status) && status != 408 && status != 429
        default: return false
        }
    }
}
