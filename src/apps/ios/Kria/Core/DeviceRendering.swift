import Foundation
import KriaMediaEngine

struct DeviceRenderStatusResponse: Decodable, Sendable {
    let phase: String
    let request: DeviceRenderRequest
    let reason: String?
    /// Added alongside `reason` once the server classifies a `needs_attention`
    /// failure. Optional so older/unrelated responses that omit the key still decode.
    let reasonCode: String?
    private enum CodingKeys: String, CodingKey { case phase, request, reason, reasonCode = "reason_code" }
    init(phase: String, request: DeviceRenderRequest, reason: String? = nil, reasonCode: String? = nil) {
        self.phase = phase; self.request = request; self.reason = reason; self.reasonCode = reasonCode
    }
    init(from decoder: Decoder) throws {
        let container = try decoder.container(keyedBy: CodingKeys.self)
        phase = try container.decode(String.self, forKey: .phase)
        reason = try container.decodeIfPresent(String.self, forKey: .reason)
        reasonCode = try container.decodeIfPresent(String.self, forKey: .reasonCode)
        let raw = try container.decode(JSONValue.self, forKey: .request)
        request = try RecipeJSON.decoder().decode(DeviceRenderRequest.self, from: JSONEncoder().encode(raw))
        try request.recipe.validate()
    }
}
struct DeviceRenderFailureBody: Encodable, Sendable {
    let identity: DeviceRenderIdentity
    let reasonCode: String
    let detail: String
    private enum CodingKeys: String, CodingKey { case identity, reasonCode = "reason_code", detail }
}
struct DeviceRenderFailureAck: Decodable, Sendable {
    let identity: DeviceRenderIdentity
    let phase: String
    let reasonCode: String?
    private enum CodingKeys: String, CodingKey { case identity, phase, reasonCode = "reason_code" }
    // `identity`'s own CodingKeys expect `jobId`/`variantId` (camelCase, meant to be
    // re-cased by `RecipeJSON`'s snake_case strategy — see its "acronym-normalized
    // spelling" comment in Models.swift). The outer `request(...)` decoder has no
    // such strategy, so decode `identity` as raw JSON and re-decode it through
    // `RecipeJSON.decoder()`, exactly like `DeviceRenderStatusResponse` does.
    init(from decoder: Decoder) throws {
        let container = try decoder.container(keyedBy: CodingKeys.self)
        phase = try container.decode(String.self, forKey: .phase)
        reasonCode = try container.decodeIfPresent(String.self, forKey: .reasonCode)
        let raw = try container.decode(JSONValue.self, forKey: .identity)
        identity = try RecipeJSON.decoder().decode(DeviceRenderIdentity.self, from: JSONEncoder().encode(raw))
    }
}
struct DeviceRenderRetryBody: Encodable, Sendable {
    let identity: DeviceRenderIdentity
}
struct DeviceRenderRetryAck: Decodable, Sendable {
    let identity: DeviceRenderIdentity
    let phase: String
    private enum CodingKeys: String, CodingKey { case identity, phase }
    /// See `DeviceRenderFailureAck.init(from:)` for why `identity` needs its own pass.
    init(from decoder: Decoder) throws {
        let container = try decoder.container(keyedBy: CodingKeys.self)
        phase = try container.decode(String.self, forKey: .phase)
        let raw = try container.decode(JSONValue.self, forKey: .identity)
        identity = try RecipeJSON.decoder().decode(DeviceRenderIdentity.self, from: JSONEncoder().encode(raw))
    }
}
struct DeviceExportUploadBody: Encodable, Sendable {
    let identity: DeviceRenderIdentity
    let attemptID: UUID
    let fileSizeBytes: Int64
    let sha256: String
    /// Which brand tail this upload carries ("none" or "standard"). The server
    /// adds the matching number of seconds before checking the file's duration;
    /// we never send the duration itself. Encoded as `brand_tail`.
    let brandTail: String
    private enum CodingKeys: String, CodingKey {
        case identity, attemptID = "attemptId", fileSizeBytes, sha256, brandTail
    }
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

struct DeviceAssetDownloadBody: Encodable, Sendable {
    let identity: DeviceRenderIdentity
    let assetID: String
    private enum CodingKeys: String, CodingKey { case identity, assetID = "assetId" }
}
struct DeviceAssetDownloadTarget: Decodable, Sendable {
    let assetID: String
    let downloadURL: URL
    let expiresAt: Date
    private enum CodingKeys: String, CodingKey {
        case assetID = "asset_id", downloadURL = "download_url", expiresAt = "expires_at"
    }
}

extension KriaAPI {
    func downloadDeviceAsset(_ body: DeviceAssetDownloadBody) async throws -> DeviceAssetDownloadTarget {
        try await request(path: "me/jobs/\(body.identity.jobID.uuidString)/device-render/assets", method: "POST", bodyData: RecipeJSON.encoder().encode(body), decode: DeviceAssetDownloadTarget.self)
    }
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
    func reportDeviceRenderFailure(jobID: UUID, identity: DeviceRenderIdentity, reasonCode: String, detail: String) async throws -> DeviceRenderFailureAck {
        try await request(
            path: "me/jobs/\(jobID.uuidString)/device-render/failures", method: "POST",
            bodyData: RecipeJSON.encoder().encode(DeviceRenderFailureBody(identity: identity, reasonCode: reasonCode, detail: detail)),
            decode: DeviceRenderFailureAck.self
        )
    }
    func retryDeviceRenderFailure(jobID: UUID, identity: DeviceRenderIdentity) async throws -> DeviceRenderRetryAck {
        try await request(
            path: "me/jobs/\(jobID.uuidString)/device-render/retry", method: "POST",
            bodyData: RecipeJSON.encoder().encode(DeviceRenderRetryBody(identity: identity)),
            decode: DeviceRenderRetryAck.self
        )
    }
}

/// Reports on-device render failures the server should learn about (so it
/// doesn't wait indefinitely for a device that has already given up), without
/// the media-engine package depending on app networking. Best-effort: a
/// failure to report never crashes or retry-loops the coordinator.
struct APIDeviceRenderFailureReporter: DeviceRenderFailureReporter {
    let api: any KriaAPIClient
    func report(identity: DeviceRenderIdentity, reasonCode: DeviceRenderFailureReasonCode, detail: String) async {
        do {
            _ = try await api.reportDeviceRenderFailure(
                jobID: identity.jobID, identity: identity,
                reasonCode: reasonCode.rawValue, detail: String(detail.prefix(2000))
            )
        } catch {
            #if DEBUG
            NativePreviewDiagnostics.record("device-render-failure-report-failed", fields: ["reason": reasonCode.rawValue])
            #endif
        }
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
    func publish(file: URL, identity: DeviceRenderIdentity, attemptID: UUID, brandTail: String) async throws -> DevicePublication {
        let current = try await api.deviceRender(jobID: identity.jobID, variantID: identity.variantID)
        guard current.request.identity == identity else { return .superseded }
        guard ["awaiting_device", "syncing", "published"].contains(current.phase) else { throw APIError.conflict }
        let complete = DeviceExportCompleteBody(identity: identity, attemptID: attemptID)
        if current.phase == "published" {
            do { try await api.completeDeviceExport(complete); return .published }
            catch APIError.conflict { return .superseded }
        }
        let fingerprint = try await Task.detached { try SHA256Fingerprinter().fingerprint(file: file) }.value
        let reservation = try await api.reserveDeviceExport(DeviceExportUploadBody(identity: identity, attemptID: attemptID, fileSizeBytes: fingerprint.byteCount, sha256: fingerprint.hex, brandTail: brandTail))
        guard reservation.attemptID == attemptID, reservation.uploadURL.scheme == "https", reservation.expiresAt > Date() else { throw APIError.invalidResponse }
        var put = URLRequest(url: reservation.uploadURL)
        put.httpMethod = "PUT"
        for (name, value) in reservation.uploadHeaders { put.setValue(value, forHTTPHeaderField: name) }
        let (_, response) = try await uploadSession.upload(for: put, fromFile: file)
        guard let response = response as? HTTPURLResponse else { throw APIError.invalidResponse }
        // A create-only PUT can return 412 after a lost success response. The server validates
        // the generation and checksum before attaching it, so this is safe to reconcile.
        guard (200..<300).contains(response.statusCode) || response.statusCode == 412 else { throw APIError.requestFailed(status: response.statusCode) }
        try Task.checkCancellation()
        do { try await api.completeDeviceExport(complete); return .published }
        catch APIError.conflict {
            if try await isCurrent(identity) { throw APIError.conflict }
            return .superseded
        }
    }
}


/// Bound to one immutable request; the API rechecks authorization for every grant.
struct AuthorizedDeviceSourceResolver: DeviceSourceResolving {
    let api: any KriaAPIClient
    let request: DeviceRenderRequest
    let originals: SourceAssetStore
    let library: RenderLibraryCache
    var bundledFonts: URL? = Bundle.main.url(forResource: "fonts", withExtension: nil)
    // No API authorization header or persistent cookies travel to object storage.
    var downloadSession: URLSession = URLSession(configuration: .ephemeral)
    /// The grant route allows 30 requests a minute and a recipe needs one per
    /// Visuals photo, so a photo-heavy render waits out the window instead of failing.
    var rateLimitBackoff: Duration = .seconds(20)

    func resolve(for recipe: KriaMediaEngine.EditRecipe) async throws -> [String: URL] {
        guard recipe == request.recipe else { throw APIError.conflict }
        try recipe.validate()
        guard let manifest = recipe.assetManifest else {
            return try await OriginalSourceResolver(store: originals).resolve(for: recipe)
        }
        for asset in manifest.assets {
            try Task.checkCancellation()
            // Originals resolve only from this iPhone's bindings; library bytes and
            // Visuals-pool photos need a per-asset grant for the pinned generation.
            if case .original = asset.source { continue }
            if (try? await library.resolve(asset)) != nil { continue }
            if case .library(catalog: .font, catalogID: _, generation: _) = asset.source {
                guard let bundledFonts else { throw MediaEngineError.missingAsset(asset.id) }
                _ = try await library.installBundledFont(asset, directory: bundledFonts)
                continue
            }
            let grant = try await grant(for: asset)
            guard grant.assetID == asset.id, grant.downloadURL.scheme == "https", grant.expiresAt > Date() else {
                throw APIError.invalidResponse
            }
            let (file, response) = try await downloadSession.download(from: grant.downloadURL)
            defer { try? FileManager.default.removeItem(at: file) }
            guard let response = response as? HTTPURLResponse else { throw APIError.invalidResponse }
            guard response.statusCode == 200 else { throw APIError.requestFailed(status: response.statusCode) }
            try Task.checkCancellation()
            _ = try await library.install(downloadedFile: file, for: asset)
        }
        var urls = try await PortableAssetResolver(originals: originals, library: library).resolve(manifest)
        // The verified cache holds exact bytes under a content address. Photos
        // render from a cover-sized copy; videos need a playable file extension.
        let project = await library.root.deletingLastPathComponent()
        let derivatives = project.appending(path: "still-derivatives", directoryHint: .isDirectory)
        let videos = project.appending(path: "visual-videos", directoryHint: .isDirectory)
        for asset in manifest.assets {
            guard case .visual(_, _, let mediaKind) = asset.source, let verified = urls[asset.id] else { continue }
            try Task.checkCancellation()
            let canvas = recipe.canvas, fingerprint = asset.fingerprint
            urls[asset.id] = try await Task.detached {
                switch mediaKind {
                case .image: try StillImageDerivative.prepare(source: verified, fingerprint: fingerprint, canvas: canvas, directory: derivatives)
                case .video: try VisualVideoFile.prepare(source: verified, fingerprint: fingerprint, directory: videos)
                }
            }.value
        }
        return urls
    }

    private func grant(for asset: RenderAssetReference) async throws -> DeviceAssetDownloadTarget {
        var retries = 0
        while true {
            do { return try await api.downloadDeviceAsset(DeviceAssetDownloadBody(identity: request.identity, assetID: asset.id)) }
            catch APIError.requestFailed(status: 429) where retries < 3 {
                retries += 1
                try await Task.sleep(for: rateLimitBackoff)
            }
        }
    }
}
