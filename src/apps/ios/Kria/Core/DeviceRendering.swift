import Foundation
import KriaMediaEngine

struct DeviceRenderStatusResponse: Decodable, Sendable {
    let phase: String
    let request: DeviceRenderRequest
    let reason: String?
    private enum CodingKeys: String, CodingKey { case phase, request, reason }
    init(from decoder: Decoder) throws {
        let container = try decoder.container(keyedBy: CodingKeys.self)
        phase = try container.decode(String.self, forKey: .phase)
        reason = try container.decodeIfPresent(String.self, forKey: .reason)
        let raw = try container.decode(JSONValue.self, forKey: .request)
        request = try RecipeJSON.decoder().decode(DeviceRenderRequest.self, from: JSONEncoder().encode(raw))
        try request.recipe.validate()
    }
}
struct DeviceExportUploadBody: Encodable, Sendable {
    let identity: DeviceRenderIdentity
    let attemptID: UUID
    let fileSizeBytes: Int64
    let sha256: String
    private enum CodingKeys: String, CodingKey { case identity, attemptID = "attemptId", fileSizeBytes, sha256 }
}
struct DeviceExportCompleteBody: Encodable, Sendable {
    let identity: DeviceRenderIdentity
    let attemptID: UUID
    private enum CodingKeys: String, CodingKey { case identity, attemptID = "attemptId" }
}
struct DeviceExportUploadTarget: Decodable, Sendable {
    let attemptID: UUID
    let uploadURL: URL
    let uploadHeaders: [String: String]
    let expiresAt: Date
    private enum CodingKeys: String, CodingKey {
        case attemptID = "attempt_id", uploadURL = "upload_url", uploadHeaders = "upload_headers", expiresAt = "expires_at"
    }
}
private struct DeviceExportCompletion: Decodable { let status: String }

extension KriaAPI {
    func deviceRender(jobID: UUID, variantID: String) async throws -> DeviceRenderStatusResponse {
        try await request(path: "me/jobs/\(jobID.uuidString)/device-render", method: "GET", query: [URLQueryItem(name: "variant_id", value: variantID)], bodyData: nil, decode: DeviceRenderStatusResponse.self)
    }
    func reserveDeviceExport(_ body: DeviceExportUploadBody) async throws -> DeviceExportUploadTarget {
        try await request(path: "me/jobs/\(body.identity.jobID.uuidString)/device-render/uploads", method: "POST", bodyData: RecipeJSON.encoder().encode(body), decode: DeviceExportUploadTarget.self)
    }
    func completeDeviceExport(_ body: DeviceExportCompleteBody) async throws {
        let result = try await request(path: "me/jobs/\(body.identity.jobID.uuidString)/device-render/complete", method: "POST", bodyData: RecipeJSON.encoder().encode(body), decode: DeviceExportCompletion.self)
        guard result.status == "published" else { throw APIError.invalidResponse }
    }
}

struct DeviceExportPublisher: DeviceRenderPublishing {
    let api: any KriaAPIClient
    // A separate session keeps API Bearer credentials out of storage PUT requests.
    var uploadSession: URLSession = URLSession(configuration: .ephemeral)
    func isCurrent(_ identity: DeviceRenderIdentity) async throws -> Bool {
        do {
            let status = try await api.deviceRender(jobID: identity.jobID, variantID: identity.variantID)
            return status.request.identity == identity && ["awaiting_device", "syncing", "published"].contains(status.phase)
        }
        catch APIError.conflict { return false }
    }
    func publish(file: URL, identity: DeviceRenderIdentity, attemptID: UUID) async throws -> DevicePublication {
        let current = try await api.deviceRender(jobID: identity.jobID, variantID: identity.variantID)
        guard current.request.identity == identity else { return .superseded }
        guard ["awaiting_device", "syncing", "published"].contains(current.phase) else { throw APIError.conflict }
        let complete = DeviceExportCompleteBody(identity: identity, attemptID: attemptID)
        if current.phase == "published" {
            do { try await api.completeDeviceExport(complete); return .published }
            catch APIError.conflict { return .superseded }
        }
        let fingerprint = try await Task.detached { try SHA256Fingerprinter().fingerprint(file: file) }.value
        let reservation = try await api.reserveDeviceExport(DeviceExportUploadBody(identity: identity, attemptID: attemptID, fileSizeBytes: fingerprint.byteCount, sha256: fingerprint.hex))
        guard reservation.attemptID == attemptID, reservation.uploadURL.scheme == "https", reservation.expiresAt > Date() else { throw APIError.invalidResponse }
        var put = URLRequest(url: reservation.uploadURL)
        put.httpMethod = "PUT"
        for (name, value) in reservation.uploadHeaders { put.setValue(value, forHTTPHeaderField: name) }
        let (_, response) = try await uploadSession.upload(for: put, fromFile: file)
        guard let response = response as? HTTPURLResponse else { throw APIError.invalidResponse }
        // A create-only PUT can return 412 after a lost success response. The server validates
        // the generation and checksum before attaching it, so this is safe to reconcile.
        guard (200..<300).contains(response.statusCode) || response.statusCode == 412 else { throw APIError.requestFailed }
        try Task.checkCancellation()
        do { try await api.completeDeviceExport(complete); return .published }
        catch APIError.conflict {
            if try await isCurrent(identity) { throw APIError.conflict }
            return .superseded
        }
    }
}
