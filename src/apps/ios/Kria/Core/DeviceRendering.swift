import Foundation
import KriaMediaEngine
import UIKit

struct DeviceRenderStatusResponse: Decodable, Sendable {
    let phase: String
    let request: DeviceRenderRequest
    let reason: String?
    /// Added alongside `reason` once the server classifies a `needs_attention`
    /// failure. Optional so older/unrelated responses that omit the key still decode.
    let reasonCode: String?
    /// Generation associated with the authoritative published attempt. Only
    /// present for `published` responses; it fences editor polling from a
    /// newer or unrelated device render.
    let publishedGeneration: String?
    /// Saved document bound to the immutable recipe, including while its export is pending.
    let baseGeneration: String?
    let retryRequiresApproval: Bool
    let outputVerificationRequired: Bool
    /// Phone rendering is rolled back for this account while the recipe waits. A hold,
    /// not a failure: the phone shows a waiting state and does not start the render.
    let phoneRenderingPaused: Bool
    /// The creator tapped Stop and has not resumed. A resumable pause, never a failure:
    /// the server keeps the same identity and `/device-render/retry` resumes it.
    let pausedByUser: Bool
    private enum CodingKeys: String, CodingKey { case phase, request, reason, reasonCode = "reason_code", publishedGeneration = "published_generation", baseGeneration = "base_generation", retryRequiresApproval = "retry_requires_approval", outputVerificationRequired = "output_verification_required", phoneRenderingPaused = "phone_rendering_paused", pausedByUser = "paused_by_user" }
    init(phase: String, request: DeviceRenderRequest, reason: String? = nil, reasonCode: String? = nil, publishedGeneration: String? = nil, baseGeneration: String? = nil, retryRequiresApproval: Bool = false, outputVerificationRequired: Bool = false, phoneRenderingPaused: Bool = false, pausedByUser: Bool = false) {
        self.phase = phase; self.request = request; self.reason = reason; self.reasonCode = reasonCode; self.publishedGeneration = publishedGeneration; self.baseGeneration = baseGeneration
        self.retryRequiresApproval = retryRequiresApproval
        self.outputVerificationRequired = outputVerificationRequired
        self.phoneRenderingPaused = phoneRenderingPaused
        self.pausedByUser = pausedByUser
    }
    init(from decoder: Decoder) throws {
        let container = try decoder.container(keyedBy: CodingKeys.self)
        phase = try container.decode(String.self, forKey: .phase)
        reason = try container.decodeIfPresent(String.self, forKey: .reason)
        reasonCode = try container.decodeIfPresent(String.self, forKey: .reasonCode)
        publishedGeneration = try container.decodeIfPresent(String.self, forKey: .publishedGeneration)
        baseGeneration = try container.decodeIfPresent(String.self, forKey: .baseGeneration)
        retryRequiresApproval = try container.decodeIfPresent(Bool.self, forKey: .retryRequiresApproval) ?? false
        outputVerificationRequired = try container.decodeIfPresent(Bool.self, forKey: .outputVerificationRequired) ?? false
        phoneRenderingPaused = try container.decodeIfPresent(Bool.self, forKey: .phoneRenderingPaused) ?? false
        pausedByUser = try container.decodeIfPresent(Bool.self, forKey: .pausedByUser) ?? false
        let raw = try container.decode(JSONValue.self, forKey: .request)
        let data = try JSONEncoder().encode(raw)
        do {
            request = try RecipeJSON.decoder().decode(DeviceRenderRequest.self, from: data)
            try request.recipe.validate()
        } catch {
            // A recipe this build can't read used to surface as raw decoder
            // text while the server waited on the phone forever (S1-f). Keep
            // the identity so the phone can tell the server why it won't render.
            guard let identity = try? RecipeJSON.decoder().decode(DeviceRenderRequestIdentity.self, from: data).identity else { throw error }
            throw DeviceRenderRecipeUnreadable(identity: identity, phase: phase, publishedGeneration: publishedGeneration, underlying: error)
        }
    }
}
private struct DeviceRenderRequestIdentity: Decodable { let identity: DeviceRenderIdentity }

/// The status read succeeded but this build can't decode or validate the
/// pinned recipe. Unknown keys or enum cases, and a newer schema, mean the
/// app is out of date; an invalid recipe of a known schema is unsupported.
struct DeviceRenderRecipeUnreadable: Error, Equatable, Sendable {
    let identity: DeviceRenderIdentity
    let phase: String
    let publishedGeneration: String?
    let reasonCode: String
    let detail: String

    init(identity: DeviceRenderIdentity, phase: String, publishedGeneration: String?, underlying: Error) {
        self.identity = identity; self.phase = phase; self.publishedGeneration = publishedGeneration
        // Only a true version or schema gap is "update Kria": an unknown key, enum case or
        // asset kind, a newer manifest version, a newer recipe schema. An asset manifest of a
        // known shape whose values are wrong (a zero byte count, a bad digest) is not fixed by
        // updating; it is a recipe this phone can't use, and a change in chat re-plans it.
        let outdated: Bool = switch underlying {
        case is DecodingError: true
        case RenderAssetError.newerManifest: true
        case RecipeError.unsupportedSchema: true
        default: false
        }
        reasonCode = outdated ? DeviceRenderFailureReasonCode.rendererOutdated.rawValue : DeviceRenderFailureReasonCode.unsupportedRecipe.rawValue
        detail = "Device recipe unreadable: \(String(describing: type(of: underlying)))"
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
    /// Encoded as `renders_locally` (RecipeJSON's snake_case strategy), and only when true:
    /// an API from before the key (`DeviceRetryBody`, extra='forbid') answers 422 to it, so a
    /// new build's ordinary Try again and Resume keep working against the older API.
    var rendersLocally: Bool = false
    private enum CodingKeys: String, CodingKey { case identity, rendersLocally }
    func encode(to encoder: Encoder) throws {
        var container = encoder.container(keyedBy: CodingKeys.self)
        try container.encode(identity, forKey: .identity)
        if rendersLocally { try container.encode(true, forKey: .rendersLocally) }
    }
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
        if result.status == "published" { return }
        guard result.status == "checking" else { throw APIError.invalidResponse }
        // The committed worker check survives app suspension or a lost HTTP
        // response. Poll only lightweight status reads, never completion POSTs,
        // backing off from 2 s to 30 s. A network failure leaves the local
        // receipt resumable; no rerender.
        let deadline = ContinuousClock.now + .seconds(15 * 60)
        var delay = Duration.seconds(2)
        while ContinuousClock.now < deadline {
            try await Task.sleep(for: delay)
            delay = min(delay * 2, .seconds(30))
            let status = try await deviceRender(jobID: body.identity.jobID, variantID: body.identity.variantID)
            guard status.request.identity == body.identity else { throw APIError.conflict }
            if status.phase == "published" {
                guard status.publishedGeneration == body.attemptID.uuidString.lowercased() else { throw APIError.conflict }
                return
            }
            guard status.phase == "syncing" else { throw APIError.conflict }
        }
        // Still checking: not a failure and not a reason to upload again. The
        // coordinator parks the finished file and the next recover keeps waiting.
        throw DeviceRenderVerificationPending()
    }
    func reportDeviceRenderFailure(jobID: UUID, identity: DeviceRenderIdentity, reasonCode: String, detail: String) async throws -> DeviceRenderFailureAck {
        try await request(
            path: "me/jobs/\(jobID.uuidString)/device-render/failures", method: "POST",
            bodyData: RecipeJSON.encoder().encode(DeviceRenderFailureBody(identity: identity, reasonCode: reasonCode, detail: detail)),
            decode: DeviceRenderFailureAck.self
        )
    }
    func retryDeviceRenderFailure(jobID: UUID, identity: DeviceRenderIdentity) async throws -> DeviceRenderRetryAck {
        try await retryDeviceRenderFailure(jobID: jobID, identity: identity, rendersLocally: false)
    }
    func retryDeviceRenderFailure(jobID: UUID, identity: DeviceRenderIdentity, rendersLocally: Bool) async throws -> DeviceRenderRetryAck {
        try await request(
            path: "me/jobs/\(jobID.uuidString)/device-render/retry", method: "POST",
            bodyData: RecipeJSON.encoder().encode(DeviceRenderRetryBody(identity: identity, rendersLocally: rendersLocally)),
            decode: DeviceRenderRetryAck.self
        )
    }
}

/// Reports on-device render failures and user Stops the server should learn about
/// (so it doesn't wait indefinitely for a device that has already given up), without
/// the media-engine package depending on app networking. `deliver` tells the
/// coordinator whether the server took the report, so a Stop sent while offline is
/// re-sent until it lands instead of leaving phone and server disagreeing.
struct APIDeviceRenderFailureReporter: DeviceRenderFailureReporter {
    let api: any KriaAPIClient
    var isAuthorized: @Sendable () async -> Bool = { true }
    func report(identity: DeviceRenderIdentity, reasonCode: DeviceRenderFailureReasonCode, detail: String) async {
        _ = await deliver(identity: identity, reasonCode: reasonCode, detail: detail)
    }
    func deliver(identity: DeviceRenderIdentity, reasonCode: DeviceRenderFailureReasonCode, detail: String) async -> DeviceRenderReportOutcome {
        guard await isAuthorized() else { return .rejected }
        do {
            _ = try await api.reportDeviceRenderFailure(
                jobID: identity.jobID, identity: identity,
                reasonCode: reasonCode.rawValue, detail: String(detail.prefix(2000))
            )
            guard await isAuthorized() else { return .rejected }
            return .accepted
        } catch {
            #if DEBUG
            NativePreviewDiagnostics.record("device-render-failure-report-failed", fields: ["reason": reasonCode.rawValue])
            #endif
            return Self.outcome(for: error)
        }
    }
    /// No answer, a 5xx or rate limiting is worth sending again; any other answer
    /// (published, superseded, signed out) is the server's final word.
    static func outcome(for error: Error) -> DeviceRenderReportOutcome {
        if error is CancellationError || (error as? URLError)?.code == .cancelled { return .transient }
        if case let APIError.requestFailed(status, _) = error, status == 429 { return .transient }
        return RequestFailureCause(error) == .other ? .rejected : .transient
    }
}

/// Offline, a 5xx or rate limiting while the coordinator grants assets, checks the
/// recipe is current, or uploads: the render waits and retries instead of failing.
extension APIError: DeviceRenderTransientFailure {
    var isTransientDeviceRenderFailure: Bool {
        switch self {
        case .offline: true
        case let .requestFailed(status, _): status == 429 || RequestFailureCause(status: status) == .server
        default: false
        }
    }
}

/// Keeps a phone render running for the short window iOS grants after the creator
/// leaves Kria. When the window expires the coordinator stops the encode and keeps
/// the receipt resumable, so coming back continues it instead of "export failed".
@MainActor struct UIKitDeviceRenderBackgroundExecution: DeviceRenderBackgroundExecution {
    private let scheduler: any ContinuedProcessingExportScheduling
    private let initiation: DeviceRenderUserInitiation
    private let isForeground: @MainActor @Sendable () -> Bool
    private let fallback: @MainActor @Sendable (@escaping @Sendable () -> Void) -> DeviceRenderExecutionWindow

    init(scheduler: any ContinuedProcessingExportScheduling = AppleContinuedProcessingExportScheduler(),
         initiation: DeviceRenderUserInitiation = .shared,
         isForeground: @escaping @MainActor @Sendable () -> Bool = { UIApplication.shared.applicationState == .active },
         fallback: @escaping @MainActor @Sendable (@escaping @Sendable () -> Void) -> DeviceRenderExecutionWindow = UIKitDeviceRenderBackgroundExecution.beginShortWindow) {
        self.fallback = fallback
        self.scheduler = scheduler
        self.initiation = initiation
        self.isForeground = isForeground
    }
    @MainActor private final class Window {
        var identifier: UIBackgroundTaskIdentifier = .invalid
        func end() {
            guard identifier != .invalid else { return }
            UIApplication.shared.endBackgroundTask(identifier)
            identifier = .invalid
        }
    }

    static func install() { DeviceRenderBackground.install(Self()) }

    @MainActor func begin(context: DeviceRenderExecutionContext, expiring: @escaping @Sendable () -> Void) async throws -> DeviceRenderExecutionWindow {
        let authorized = initiation.consume(context.origin, identity: context.identity)
        let continued = DurableDeviceRenderWindow(scheduler: scheduler, isForeground: isForeground, expiring: expiring)
        if try await continued.acquire(authorized: authorized) {
            do { try Task.checkCancellation() }
            catch { continued.finish(.cancelled); throw error }
            return DeviceRenderExecutionWindow(reportProgress: { await continued.reportProgress($0) },
                                               finish: { await continued.finish($0) })
        }
        try Task.checkCancellation()
        return fallback(expiring)
    }

    private static func beginShortWindow(expiring: @escaping @Sendable () -> Void) -> DeviceRenderExecutionWindow {
        let window = Window()
        window.identifier = UIApplication.shared.beginBackgroundTask(withName: "Kria render") {
            MainActor.assumeIsolated {
                guard window.identifier != .invalid else { return }
                window.end()
                expiring()
            }
        }
        return DeviceRenderExecutionWindow(reportProgress: { _ in }, finish: { _ in await window.end() })
    }
}

struct DeviceExportPublisher: DeviceRenderPublishing {
    let api: any KriaAPIClient
    var isAuthorized: @Sendable () async -> Bool = { true }

    private func requireOwner() async throws {
        try Task.checkCancellation()
        guard await isAuthorized() else { throw CancellationError() }
    }
    // A separate session keeps API Bearer credentials out of storage PUT requests.
    var uploadSession: URLSession = URLSession(configuration: .ephemeral)
    private static func acceptsStorageURL(_ url: URL) -> Bool {
        if url.scheme == "https" { return true }
        #if DEBUG
        if let base = CameraRollLiveQA.configuration()?.baseURL,
           url.scheme == base.scheme, url.host == base.host, url.port == base.port,
           url.path.hasPrefix("/qa-owned-exports/"), url.user == nil, url.password == nil {
            return true
        }
        // Explicit scratch-API hardware journey only. Release storage stays HTTPS.
        guard ProcessInfo.processInfo.arguments.contains("-ui-testing-retry-api"),
              let address = ProcessInfo.processInfo.environment["KRIA_RETRY_TEST_API_URL"],
              let base = URL(string: address), let host = base.host,
              host == "localhost" || host == "127.0.0.1" || host.hasPrefix("192.168.") || host.hasPrefix("10."),
              base.scheme == "http", url.scheme == "http", url.host == host,
              url.port == base.port, url.path.hasPrefix("/kri674-storage/") else { return false }
        return true
        #else
        return false
        #endif
    }
    func isCurrent(_ identity: DeviceRenderIdentity) async throws -> Bool {
        try await requireOwner()
        do {
            let status = try await api.deviceRender(jobID: identity.jobID, variantID: identity.variantID)
            try await requireOwner()
            return status.request.identity == identity && ["awaiting_device", "syncing", "published"].contains(status.phase)
        }
        catch APIError.conflict { return false }
    }
    func publish(file: URL, identity: DeviceRenderIdentity, attemptID: UUID, brandTail: String) async throws -> DevicePublication {
        try await requireOwner()
        let current = try await api.deviceRender(jobID: identity.jobID, variantID: identity.variantID)
        try await requireOwner()
        guard current.request.identity == identity else { return .superseded }
        guard ["awaiting_device", "syncing", "published"].contains(current.phase) else { throw APIError.conflict }
        let complete = DeviceExportCompleteBody(identity: identity, attemptID: attemptID)
        if current.phase == "published" {
            do { try await requireOwner(); try await api.completeDeviceExport(complete); try await requireOwner(); return .published }
            catch APIError.conflict { return .superseded }
        }
        let fingerprint = try await Task.detached { try SHA256Fingerprinter().fingerprint(file: file) }.value
        // This attempt's bytes already reached storage (a previous publish of the
        // same file): resume its completion and check instead of re-uploading.
        let marker = file.deletingPathExtension().appendingPathExtension("uploaded")
        let markerText = "\(attemptID.uuidString.lowercased())\n\(fingerprint.hex)"
        if current.phase == "syncing", (try? String(contentsOf: marker, encoding: .utf8)) == markerText {
            do { try await requireOwner(); try await api.completeDeviceExport(complete); try await requireOwner(); return .published }
            catch APIError.conflict {
                if !(try await isCurrent(identity)) { return .superseded }
                // The server lost the upload (e.g. the reservation expired): upload again.
                try? FileManager.default.removeItem(at: marker)
            }
        }
        let reservation: DeviceExportUploadTarget
        do {
            try await requireOwner()
            reservation = try await api.reserveDeviceExport(DeviceExportUploadBody(identity: identity, attemptID: attemptID, fileSizeBytes: fingerprint.byteCount, sha256: fingerprint.hex, brandTail: brandTail))
        } catch let error as APIError where error.conflictDetail == "Upload attempt reused for different bytes" {
            throw DeviceExportAttemptReused()
        }
        try await requireOwner()
        guard reservation.attemptID == attemptID, Self.acceptsStorageURL(reservation.uploadURL), reservation.expiresAt > Date() else { throw APIError.invalidResponse }
        var put = URLRequest(url: reservation.uploadURL)
        put.httpMethod = "PUT"
        for (name, value) in reservation.uploadHeaders { put.setValue(value, forHTTPHeaderField: name) }
        let (_, response) = try await uploadSession.upload(for: put, fromFile: file)
        guard let response = response as? HTTPURLResponse else { throw APIError.invalidResponse }
        // A create-only PUT can return 412 after a lost success response. The server validates
        // the generation and checksum before attaching it, so this is safe to reconcile.
        guard (200..<300).contains(response.statusCode) || response.statusCode == 412 else { throw APIError.requestFailed(status: response.statusCode) }
        try? Data(markerText.utf8).write(to: marker, options: .atomic)
        try Task.checkCancellation()
        do { try await requireOwner(); try await api.completeDeviceExport(complete); try await requireOwner(); return .published }
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

    /// Resolves just the pinned voiceover that a native-editor reconstruction
    /// needs. Unlike `resolve(for:)`, this deliberately does not touch the
    /// recipe's originals, Visuals, fonts, or music: reopening an editor must
    /// not ask the creator to relink every source simply to restore narration.
    /// The request remains the server-authorized, immutable device recipe, so
    /// the grant and the content-addressed cache retain the same trust boundary
    /// as a full device render.
    func resolveNarration() async throws -> URL {
        try await resolvePinnedAudio(assetID: request.recipe.audio.narrationAssetID, missing: "narration") {
            if case .voiceover = $0 { true } else { false }
        }
    }

    /// The creator's song for a native-editor reconstruction (KRI-374), pinned the same way as the narration:
    /// the recipe's music bed must name a `song` asset in the manifest, so a library music bed never resolves here.
    func resolveSong() async throws -> URL {
        try await resolvePinnedAudio(assetID: request.recipe.audio.musicAssetID, missing: "song") {
            if case .song = $0 { true } else { false }
        }
    }

    private func resolvePinnedAudio(assetID: String?, missing: String, isExpectedKind: (RenderAssetReference.Source) -> Bool) async throws -> URL {
        guard let narrationID = assetID,
              let manifest = request.recipe.assetManifest,
              let asset = manifest.assets.first(where: { $0.id == narrationID }),
              isExpectedKind(asset.source) else {
            throw MediaEngineError.missingAsset(missing)
        }
        guard let media = request.recipe.assets.first(where: { $0.id == narrationID }),
              let fingerprint = media.fingerprint,
              try RenderFingerprint(fingerprint) == asset.fingerprint else {
            throw APIError.invalidResponse
        }
        let verified: URL
        do {
            verified = try await library.resolve(asset)
        } catch RenderAssetError.missingLibraryAsset, RenderAssetError.changedLibraryAsset {
            let grant = try await grant(for: asset)
            guard grant.assetID == asset.id, grant.downloadURL.scheme == "https", grant.expiresAt > Date() else {
                throw APIError.invalidResponse
            }
            let (file, response) = try await downloadSession.download(from: grant.downloadURL)
            defer { try? FileManager.default.removeItem(at: file) }
            guard let response = response as? HTTPURLResponse else { throw APIError.invalidResponse }
            guard response.statusCode == 200 else { throw APIError.requestFailed(status: response.statusCode) }
            try Task.checkCancellation()
            verified = try await library.install(downloadedFile: file, for: asset)
        }
        let project = await library.root.deletingLastPathComponent()
        let playableAudio = project.appending(path: "playable-audio", directoryHint: .isDirectory)
        return try await Task.detached {
            try PlayableAudioFile.prepare(source: verified, fingerprint: asset.fingerprint, directory: playableAudio)
        }.value
    }

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
        // Library music/sound-effect beds, a recorded voiceover and a creator's song (KRI-374) need the
        // same playable-extension fixup: the verified cache stores every
        // download at an extensionless content address, and AVFoundation
        // refuses to open one (KRI-132).
        let playableAudio = project.appending(path: "playable-audio", directoryHint: .isDirectory)
        for asset in manifest.assets {
            let isAudioAsset: Bool
            switch asset.source {
            case .library(let catalog, _, _): isAudioAsset = catalog == .music || catalog == .soundEffect
            case .voiceover, .song, .processedAudio: isAudioAsset = true
            default: isAudioAsset = false
            }
            guard isAudioAsset, let verified = urls[asset.id] else { continue }
            try Task.checkCancellation()
            let fingerprint = asset.fingerprint
            urls[asset.id] = try await Task.detached {
                try PlayableAudioFile.prepare(source: verified, fingerprint: fingerprint, directory: playableAudio)
            }.value
        }
        if let recovery = request.recovery {
            guard recovery.action == "normalize_sources", (1...2).contains(recovery.attempt),
                  !recovery.assetIDs.isEmpty, recovery.assetIDs.count <= 16,
                  Set(recovery.assetIDs).count == recovery.assetIDs.count else { throw APIError.invalidResponse }
            let directory = project.appending(path: "repair-sources/\(request.identity.recipeRevision)", directoryHint: .isDirectory)
            for id in recovery.assetIDs {
                guard let asset = manifest.assets.first(where: { $0.id == id }),
                      case .original = asset.source, let original = urls[id] else { throw APIError.invalidResponse }
                // `PortableAssetResolver` above has just checked the approved
                // original fingerprint. A decoding copy changes no recipe field.
                urls[id] = try await NativeVP9Source.shared.prepare(original, cacheDirectory: directory, forceNormalization: true)
            }
        }
        return urls
    }

    private func grant(for asset: RenderAssetReference) async throws -> DeviceAssetDownloadTarget {
        var retries = 0
        while true {
            do { return try await api.downloadDeviceAsset(DeviceAssetDownloadBody(identity: request.identity, assetID: asset.id)) }
            catch APIError.requestFailed(status: 429, detail: _) where retries < 3 {
                retries += 1
                try await Task.sleep(for: rateLimitBackoff)
            }
        }
    }
}
