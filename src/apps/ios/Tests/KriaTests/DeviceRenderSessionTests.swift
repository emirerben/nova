import Foundation
import XCTest
import KriaMediaEngine
@testable import Kria

private actor SessionExport: LocalExporting {
    var calls = 0
    func export(recipe: KriaMediaEngine.EditRecipe, assetURLs: [String: URL], outputURL: URL, exportID: String, progress: (@Sendable (Double) -> Void)?) async throws -> ExportCheckpoint {
        calls += 1
        try FileManager.default.createDirectory(at: outputURL.deletingLastPathComponent(), withIntermediateDirectories: true)
        try Data("test export".utf8).write(to: outputURL)
        return ExportCheckpoint(exportID: exportID, status: .completed, progress: 1, outputURL: outputURL)
    }
}
private actor PausedSessionExport: LocalExporting {
    private var waiters: [CheckedContinuation<Void, Never>] = []
    private(set) var calls = 0
    func export(recipe: KriaMediaEngine.EditRecipe, assetURLs: [String: URL], outputURL: URL, exportID: String, progress: (@Sendable (Double) -> Void)?) async throws -> ExportCheckpoint {
        calls += 1
        await withCheckedContinuation { waiters.append($0) }
        try Task.checkCancellation()
        try FileManager.default.createDirectory(at: outputURL.deletingLastPathComponent(), withIntermediateDirectories: true)
        try Data("test export".utf8).write(to: outputURL)
        return ExportCheckpoint(exportID: exportID, status: .completed, progress: 1, outputURL: outputURL)
    }
    func release() {
        for waiter in waiters { waiter.resume() }
        waiters.removeAll()
    }
}

private struct SessionSources: DeviceSourceResolving {
    func resolve(for recipe: KriaMediaEngine.EditRecipe) async throws -> [String: URL] { [:] }
}
private actor SessionPublisher: DeviceRenderPublishing {
    var shouldFail = false
    func fail(_ value: Bool) { shouldFail = value }
    func isCurrent(_ identity: DeviceRenderIdentity) async throws -> Bool { true }
    func publish(file: URL, identity: DeviceRenderIdentity, attemptID: UUID, brandTail: String) async throws -> DevicePublication {
        if shouldFail { throw APIError.requestFailed(status: 503) }
        return .published
    }
}

private actor SessionRequest {
    var request: DeviceRenderRequest
    init(_ request: DeviceRenderRequest) { self.request = request }
    func set(_ request: DeviceRenderRequest) { self.request = request }
}

@MainActor final class DeviceRenderSessionTests: XCTestCase {
    private func request(_ job: UUID) -> DeviceRenderRequest {
        DeviceRenderRequest(identity: DeviceRenderIdentity(jobID: job, variantID: "first", recipeRevision: 1, recipeDigest: String(repeating: "a", count: 64)),
                            recipe: KriaMediaEngine.EditRecipe(assets: [MediaAsset(id: "source", relativePath: "source")], tracks: [TimelineTrack(id: "v", kind: .video, clips: [TimelineClip(id: "c", sourceAssetID: "source", sourceDuration: 1)])]))
    }
    private let enabled = PhoneRenderingCapabilities(enabled: true, recipeVersions: [1, 2], verifiedFeatures: MediaCapability.allCases.map(\.rawValue))
    /// KRI-132: every feature except `narrationAudio`, for the "not yet verified" voiceover cases.
    private let enabledWithoutNarration = PhoneRenderingCapabilities(enabled: true, recipeVersions: [1, 2], verifiedFeatures: MediaCapability.allCases.filter { $0 != .narrationAudio }.map(\.rawValue))

    func testDisabledGateNeverStartsExport() async throws {
        let job = UUID(), output = FileManager.default.temporaryDirectory.appendingPathComponent(UUID().uuidString)
        defer { try? FileManager.default.removeItem(at: output) }
        let request = request(job), exporter = SessionExport()
        let sessions = DeviceRenderSessions(fetch: { _, _ in DeviceRenderStatusResponse(phase: "awaiting_device", request: request) }, factory: { _, _ in
            try DeviceRenderCoordinator(directory: output, exporter: exporter, sources: SessionSources(), publisher: SessionPublisher())
        })
        let key = DeviceRenderKey(projectID: UUID(), jobID: job, variantID: "first")
        await sessions.reconcile(key, capabilities: .disabled)
        XCTAssertEqual(sessions.presentations[key]?.phase, .needsAttention)
        let calls = await exporter.calls
        XCTAssertEqual(calls, 0)
    }

    func testSyncRetryKeepsFinishedExportAndAttempt() async throws {
        let job = UUID(), output = FileManager.default.temporaryDirectory.appendingPathComponent(UUID().uuidString)
        defer { try? FileManager.default.removeItem(at: output) }
        let request = request(job), exporter = SessionExport(), publisher = SessionPublisher()
        await publisher.fail(true)
        let coordinator = try DeviceRenderCoordinator(directory: output, exporter: exporter, sources: SessionSources(), publisher: publisher)
        let sessions = DeviceRenderSessions(fetch: { _, _ in DeviceRenderStatusResponse(phase: "awaiting_device", request: request) }, factory: { _, _ in coordinator })
        let key = DeviceRenderKey(projectID: UUID(), jobID: job, variantID: "first")
        await sessions.reconcile(key, capabilities: enabled)
        await coordinator.waitUntilIdle()
        let savedReceipt = await coordinator.snapshot()
        let saved = try XCTUnwrap(savedReceipt)
        XCTAssertEqual(saved.phase, .localReady)
        await sessions.reconcile(key, capabilities: .disabled)
        XCTAssertEqual(sessions.presentations[key]?.localFile, saved.outputURL)
        await publisher.fail(false)
        await sessions.reconcile(key, capabilities: enabled, retry: true)
        await coordinator.waitUntilIdle()
        let syncedReceipt = await coordinator.snapshot()
        let synced = try XCTUnwrap(syncedReceipt)
        XCTAssertEqual(synced.phase, .synced)
        XCTAssertEqual(saved.attemptID, synced.attemptID)
        XCTAssertEqual(saved.outputURL, synced.outputURL)
        let calls = await exporter.calls
        XCTAssertEqual(calls, 1)
        await sessions.stopAll()
    }

    func testNewRevisionReplacesActiveObserverAndPublishesLocalOutput() async throws {
        let job = UUID(), output = FileManager.default.temporaryDirectory.appendingPathComponent(UUID().uuidString)
        defer { try? FileManager.default.removeItem(at: output) }
        let initial = request(job), exporter = PausedSessionExport()
        let remote = SessionRequest(initial)
        let sessions = DeviceRenderSessions(fetch: { _, _ in
            DeviceRenderStatusResponse(phase: "awaiting_device", request: await remote.request)
        }, factory: { _, _ in
            try DeviceRenderCoordinator(directory: output, exporter: exporter, sources: SessionSources(), publisher: SessionPublisher())
        })
        let key = DeviceRenderKey(projectID: UUID(), jobID: job, variantID: "first")
        await sessions.reconcile(key, capabilities: enabled)
        for _ in 0..<100 {
            if await exporter.calls == 1 { break }
            try await Task.sleep(for: .milliseconds(10))
        }
        let next = DeviceRenderRequest(identity: DeviceRenderIdentity(jobID: job, variantID: "first", recipeRevision: 2,
            recipeDigest: String(repeating: "b", count: 64)), recipe: initial.recipe)
        await remote.set(next)
        await sessions.reconcile(key, capabilities: enabled)
        for _ in 0..<100 {
            if await exporter.calls == 2 { break }
            try await Task.sleep(for: .milliseconds(10))
        }
        let calls = await exporter.calls
        XCTAssertEqual(calls, 2)
        await exporter.release()
        // No further reconcile: the new revision's observer must deliver completion.
        for _ in 0..<100 {
            if sessions.presentations[key]?.phase == .synced { break }
            try await Task.sleep(for: .milliseconds(20))
        }
        XCTAssertEqual(sessions.presentations[key]?.phase, .synced)
        XCTAssertNotNil(sessions.presentations[key]?.localFile)
        await sessions.stopAll()
    }

    func testCapabilityManifestDecodesWithoutEnablingOlderServers() throws {
        let legacy = try JSONDecoder().decode(CreationCapabilities.self, from: Data(#"{"formats":[]}"#.utf8))
        XCTAssertNil(legacy.phoneRendering)
        let current = try JSONDecoder().decode(CreationCapabilities.self, from: Data(#"{"formats":[],"phone_rendering":{"enabled":true,"recipe_versions":[2],"verified_features":["basicComposition"]}}"#.utf8))
        XCTAssertEqual(current.phoneRendering?.recipeVersions, [2])
        XCTAssertEqual(DeviceRenderSessions.decision(request(UUID()).recipe, capabilities: current.phoneRendering!).route, .cloud)
    }

    func testAttachmentDestinationNeverFallsBackFromPhoneWithoutConsent() {
        let phone = UploadPurpose.analysisProxy.rawValue, cloud = UploadPurpose.cloudRenderSource.rawValue
        XCTAssertEqual(ProjectUploadDestination.resolve(capabilities: enabled, sourcePurposes: [], role: .clip), .phone)
        XCTAssertEqual(ProjectUploadDestination.resolve(capabilities: nil, sourcePurposes: [phone], role: .clip), .paused)
        XCTAssertEqual(ProjectUploadDestination.resolve(capabilities: .disabled, sourcePurposes: [phone], role: .clip), .paused)
        XCTAssertEqual(ProjectUploadDestination.resolve(capabilities: enabledWithoutNarration, sourcePurposes: [phone], role: .voiceover), .voiceoverUnavailableOnPhone)
        XCTAssertEqual(ProjectUploadDestination.resolve(capabilities: enabled, sourcePurposes: [phone], role: .voiceover), .phone)
        XCTAssertEqual(ProjectUploadDestination.resolve(capabilities: enabled, sourcePurposes: [phone], role: .visual), .phoneVisuals([.image, .video]))
        XCTAssertEqual(ProjectUploadDestination.resolve(capabilities: enabled, sourcePurposes: [cloud], role: .clip), .cloud)
        XCTAssertEqual(ProjectUploadDestination.resolve(capabilities: enabled, sourcePurposes: [phone, cloud], role: .clip), .mixed)
        XCTAssertEqual(ProjectUploadDestination.resolve(capabilities: enabled, sourcePurposes: ["future"], role: .clip), .mixed)
    }

    /// KRI-121: accounts that render on iPhone keep every project on iPhone.
    /// Visuals take the kinds this iPhone is verified to draw (`stillImages`
    /// photos, `visualVideos` videos); before that the sheet says so without
    /// greying the buttons silently or sending the project to the cloud.
    func testPhoneAccountsKeepVisualsOnTheIPhone() {
        let phone = UploadPurpose.analysisProxy.rawValue, cloud = UploadPurpose.cloudRenderSource.rawValue
        let minimum = ["basicComposition", "positionedText", "audioMix", "local1080Export"]
        func capabilities(_ extra: [String]) -> PhoneRenderingCapabilities {
            PhoneRenderingCapabilities(enabled: true, recipeVersions: [2], verifiedFeatures: minimum + extra)
        }
        for sources in [[], [phone]] {
            XCTAssertEqual(ProjectUploadDestination.resolve(capabilities: enabled, sourcePurposes: sources, role: .visual), .phoneVisuals([.image, .video]))
            XCTAssertEqual(ProjectUploadDestination.resolve(capabilities: capabilities(["stillImages"]), sourcePurposes: sources, role: .visual), .phoneVisuals([.image]))
            XCTAssertEqual(ProjectUploadDestination.resolve(capabilities: capabilities(["visualVideos"]), sourcePurposes: sources, role: .visual), .phoneVisuals([.video]))
            XCTAssertEqual(ProjectUploadDestination.resolve(capabilities: capabilities([]), sourcePurposes: sources, role: .visual), .visualsUnavailableOnPhone)
        }
        XCTAssertEqual(ProjectUploadDestination.resolve(capabilities: .disabled, sourcePurposes: [phone], role: .visual), .paused)
        XCTAssertTrue(ProjectUploadDestination.phoneVisuals([.image]).canUpload)
        XCTAssertEqual(ProjectUploadDestination.phoneVisuals([.image]).visualKinds, [.image])
        XCTAssertNil(ProjectUploadDestination.cloud.visualKinds)
        XCTAssertFalse(ProjectUploadDestination.visualsUnavailableOnPhone.canUpload)
        for destination in [ProjectUploadDestination.visualsUnavailableOnPhone, .phoneVisuals([.image]), .phoneVisuals([.image, .video])] {
            XCTAssertFalse(destination.message?.localizedCaseInsensitiveContains("cloud") ?? true)
        }
        // Accounts without iPhone rendering and existing cloud projects keep every Visuals kind.
        for capabilities in [nil, PhoneRenderingCapabilities.disabled, enabled] {
            XCTAssertEqual(ProjectUploadDestination.resolve(capabilities: capabilities, sourcePurposes: [cloud], role: .visual), .cloud)
        }
        XCTAssertEqual(ProjectUploadDestination.resolve(capabilities: nil, sourcePurposes: [], role: .visual), .cloud)
        XCTAssertEqual(ProjectUploadDestination.resolve(capabilities: .disabled, sourcePurposes: [], role: .visual), .cloud)
    }

    /// Until the account's capabilities load, nothing uploads: guessing `.cloud`
    /// would send full originals to the cloud and lock the project there.
    func testUploadsWaitForCapabilities() {
        let phone = UploadPurpose.analysisProxy.rawValue, cloud = UploadPurpose.cloudRenderSource.rawValue
        for role in CreationMediaRole.allCases {
            XCTAssertEqual(ProjectUploadDestination.resolve(capabilities: nil, capabilitiesLoaded: false, sourcePurposes: [], role: role), .checking)
            XCTAssertEqual(ProjectUploadDestination.resolve(capabilities: nil, capabilitiesLoaded: false, sourcePurposes: [phone], role: role), .checking)
            // A project that already renders in the cloud has nothing to decide.
            XCTAssertEqual(ProjectUploadDestination.resolve(capabilities: nil, capabilitiesLoaded: false, sourcePurposes: [cloud], role: role), .cloud)
        }
        XCTAssertFalse(ProjectUploadDestination.checking.canUpload)
        XCTAssertNotNil(ProjectUploadDestination.checking.message)
    }

    func testFootageOnPhoneAccountsAlwaysRendersOnTheIPhone() {
        let withoutStills = PhoneRenderingCapabilities(enabled: true, recipeVersions: [2],
            verifiedFeatures: ["basicComposition", "positionedText", "audioMix", "local1080Export"])
        XCTAssertEqual(ProjectUploadDestination.resolve(capabilities: enabled, sourcePurposes: [], role: .clip), .phone)
        XCTAssertEqual(ProjectUploadDestination.resolve(capabilities: withoutStills, sourcePurposes: [], role: .clip), .phone)
        XCTAssertEqual(ProjectUploadDestination.resolve(capabilities: enabled, sourcePurposes: [UploadPurpose.analysisProxy.rawValue], role: .clip), .phone)
        XCTAssertEqual(ProjectUploadDestination.resolve(capabilities: nil, sourcePurposes: [], role: .clip), .cloud)
    }

    /// Until `narrationAudio` is verified, an account that renders on iPhone is
    /// told voiceover is unavailable rather than silently sent to the cloud.
    /// Once it's verified, voiceover attach unlocks a `.phone` destination --
    /// the voiceover itself still uploads through the unchanged cloud contract
    /// (see `UploadViews.uploadPurpose` and `sourcePurposes`), so this only
    /// gates the recorder/attach UI, never footage's own routing (KRI-132).
    func testVoiceoverNeverMovesAPhoneAccountToTheCloud() {
        let phone = UploadPurpose.analysisProxy.rawValue, cloud = UploadPurpose.cloudRenderSource.rawValue
        for sources in [[], [phone]] {
            XCTAssertEqual(ProjectUploadDestination.resolve(capabilities: enabledWithoutNarration, sourcePurposes: sources, role: .voiceover), .voiceoverUnavailableOnPhone)
            XCTAssertEqual(ProjectUploadDestination.resolve(capabilities: enabled, sourcePurposes: sources, role: .voiceover), .phone)
        }
        XCTAssertFalse(ProjectUploadDestination.voiceoverUnavailableOnPhone.canUpload)
        XCTAssertFalse(ProjectUploadDestination.voiceoverUnavailableOnPhone.message?.localizedCaseInsensitiveContains("cloud") ?? true)
        XCTAssertTrue(ProjectUploadDestination.phone.canUpload)
        // Accounts without iPhone rendering, and projects already in the cloud, keep voiceover.
        XCTAssertEqual(ProjectUploadDestination.resolve(capabilities: nil, sourcePurposes: [], role: .voiceover), .cloud)
        XCTAssertEqual(ProjectUploadDestination.resolve(capabilities: enabled, sourcePurposes: [cloud], role: .voiceover), .cloud)
        // A recorded voiceover's own upload record (always `cloudRenderSource`,
        // see `finishRecording()`) must not flip a phone project to `.mixed`.
        let project = UUID()
        XCTAssertEqual(ProjectUploadDestination.sourcePurposes(
            media: [], records: [UploadRecoveryRecord(
                id: UUID(), projectID: project, localFilePath: "/tmp/voice", filename: "voice.m4a",
                source: .files, purpose: .cloudRenderSource, taskIdentifier: 1, retryCount: 0, mediaRole: .voiceover
            )], projectID: project
        ), [])
    }

    func testPendingUploadsDecideTheDestinationByRole() throws {
        let project = UUID()
        func record(_ purpose: UploadPurpose, _ role: CreationMediaRole, project: UUID = project) throws -> UploadRecoveryRecord {
            let json = #"{"id":"\#(UUID().uuidString)","projectID":"\#(project.uuidString)","localFilePath":"/tmp/file","filename":"file","source":"photos","purpose":"\#(purpose.rawValue)","taskIdentifier":1,"retryCount":0}"#
            var value = try JSONDecoder().decode(UploadRecoveryRecord.self, from: Data(json.utf8))
            value.mediaRole = role
            return value
        }
        let proxyClip = try record(.analysisProxy, .clip)
        let pendingPhoto = try record(.cloudRenderSource, .visual)
        let otherProject = try record(.analysisProxy, .clip, project: UUID())

        // A lingering proxy upload (failed or awaiting attach) makes the project a
        // phone project before any media is attached.
        let lingering = ProjectUploadDestination.sourcePurposes(media: [], records: [proxyClip, otherProject], projectID: project)
        XCTAssertEqual(lingering, [UploadPurpose.analysisProxy.rawValue])
        XCTAssertEqual(ProjectUploadDestination.resolve(capabilities: enabled, sourcePurposes: lingering, role: .visual), .phoneVisuals([.image, .video]))

        // A pending Visuals upload is not project media: it can't make a phone
        // project mixed, and it can't pull an empty project to the cloud.
        let attachedProxy = CreationAttachedMedia.parse(["media": .array([.object(["media_id": .string("analysis-proxy-1")])])])
        let withPendingPhoto = ProjectUploadDestination.sourcePurposes(media: attachedProxy, records: [pendingPhoto], projectID: project)
        XCTAssertEqual(ProjectUploadDestination.resolve(capabilities: enabled, sourcePurposes: withPendingPhoto, role: .clip), .phone)
        XCTAssertEqual(ProjectUploadDestination.resolve(capabilities: enabled, sourcePurposes: withPendingPhoto, role: .visual), .phoneVisuals([.image, .video]))
        let photoFirst = ProjectUploadDestination.sourcePurposes(media: [], records: [pendingPhoto], projectID: project)
        XCTAssertEqual(photoFirst, [])
        XCTAssertEqual(ProjectUploadDestination.resolve(capabilities: enabled, sourcePurposes: photoFirst, role: .clip), .phone)
    }

    func testAttachedProxyIdentityKeepsDestinationWhenContractIsOmitted() {
        let media = CreationAttachedMedia.parse(["media": .array([
            .object(["media_id": .string("analysis-proxy-123")]),
            .object(["media_id": .string("456"), "upload_contract": .object(["purpose": .string("analysis_proxy")])]),
            .object(["media_id": .string("789")]),
        ])])
        XCTAssertEqual(media.map(\.uploadPurpose), ["analysis_proxy", "analysis_proxy", "cloud_render_source"])
    }
    func testRelinkRequiresExactBytesAndCurrentRequest() async throws {
        let directory = ProjectDirectory(root: FileManager.default.temporaryDirectory.appendingPathComponent(UUID().uuidString))
        defer { try? FileManager.default.removeItem(at: directory.root) }
        try directory.createIfNeeded()
        let source = directory.root.appendingPathComponent("selected.mov")
        try Data("original".utf8).write(to: source)
        let fingerprint = try SHA256Fingerprinter().fingerprint(file: source)
        let asset = RenderAssetReference(id: "source", fingerprint: try RenderFingerprint(fingerprint), source: .original(mediaID: "server"))
        let job = UUID()
        let initial = DeviceRenderRequest(identity: DeviceRenderIdentity(jobID: job, variantID: "first", recipeRevision: 1, recipeDigest: String(repeating: "a", count: 64)),
            recipe: KriaMediaEngine.EditRecipe(schemaVersion: 2, rendererVersion: "kria-ios-2", assets: [MediaAsset(id: "source", relativePath: "source", fingerprint: fingerprint)], tracks: [TimelineTrack(id: "v", kind: .video, clips: [TimelineClip(id: "c", sourceAssetID: "source", sourceDuration: 1)])], assetManifest: RenderAssetManifest(assets: [asset])))
        let remote = SessionRequest(initial)
        let sessions = DeviceRenderSessions(fetch: { _, _ in DeviceRenderStatusResponse(phase: "awaiting_device", request: await remote.request) }, factory: { _, _ in
            try DeviceRenderCoordinator(directory: directory.root.appendingPathComponent("render"), exporter: SessionExport(), sources: SessionSources(), publisher: SessionPublisher())
        }, projectDirectory: { _ in directory })
        let key = DeviceRenderKey(projectID: UUID(), jobID: job, variantID: "first")
        await sessions.reconcile(key, capabilities: .disabled)
        let missing = try await sessions.sourcesNeedingRelink(key)
        let target = try XCTUnwrap(missing.first)
        XCTAssertEqual(missing.count, 1)
        let wrong = directory.root.appendingPathComponent("wrong.mov")
        try Data("proxy".utf8).write(to: wrong)
        do { try await sessions.relink(target, for: key, from: wrong); XCTFail("Wrong file accepted") } catch {}
        XCTAssertTrue(try SourceAssetStore(project: directory).bindings().isEmpty)
        try await sessions.relink(target, for: key, from: source)
        let remaining = try await sessions.sourcesNeedingRelink(key)
        XCTAssertTrue(remaining.isEmpty)
        let next = DeviceRenderRequest(identity: DeviceRenderIdentity(jobID: job, variantID: "first", recipeRevision: 2, recipeDigest: String(repeating: "b", count: 64)), recipe: initial.recipe)
        await remote.set(next)
        await sessions.reconcile(key, capabilities: .disabled)
        do { try await sessions.relink(target, for: key, from: source); XCTFail("Stale selection accepted") }
        catch APIError.conflict {} catch { XCTFail("Unexpected error: \(error)") }
    }

    /// A finished, synced device render must stop labeling the editor's
    /// top-of-preview button "Rendering on iPhone" — see the KRI job
    /// 9c7a1f4f report where the phone's own receipt was already
    /// `phase: synced` but the button text never changed.
    func testButtonTitleTracksEveryPhase() {
        XCTAssertEqual(DeviceRenderButtonTitle.for(phase: .preparing), "Rendering on iPhone…")
        XCTAssertEqual(DeviceRenderButtonTitle.for(phase: .rendering), "Rendering on iPhone…")
        XCTAssertEqual(DeviceRenderButtonTitle.for(phase: .syncing), "Syncing…")
        XCTAssertEqual(DeviceRenderButtonTitle.for(phase: .localReady), "Rendered on iPhone")
        XCTAssertEqual(DeviceRenderButtonTitle.for(phase: .synced), "Rendered on iPhone")
        XCTAssertEqual(DeviceRenderButtonTitle.for(phase: .needsAttention), "Needs attention")
        XCTAssertEqual(DeviceRenderButtonTitle.for(phase: .cancelled), "Render stopped")
        XCTAssertEqual(DeviceRenderButtonTitle.for(phase: .superseded), "Newer edit available")
    }

    /// KRI-132 journey fix: `unsupported_recipe` is structural (the compiled
    /// recipe itself is outside what this renderer can produce), so a blind
    /// "Try again" is hidden and the copy says what to do instead. Every
    /// other `needsAttention` reason stays transient and retryable.
    func testUnsupportedRecipeHidesRetryWithGuidanceCopyOtherReasonsStayRetryable() {
        XCTAssertFalse(DeviceRenderAttentionCopy.showsRetryButton(phase: .needsAttention, reasonCode: "unsupported_recipe"))
        let message = DeviceRenderAttentionCopy.message(phase: .needsAttention, reasonCode: "unsupported_recipe", fallback: nil)
        XCTAssertTrue(message?.localizedCaseInsensitiveContains("start a new edit") ?? false)
        for code in ["thermal", "insufficient_storage", "export_failed", "cancelled_by_user"] {
            XCTAssertTrue(DeviceRenderAttentionCopy.showsRetryButton(phase: .needsAttention, reasonCode: code), code)
        }
        XCTAssertTrue(DeviceRenderAttentionCopy.showsRetryButton(phase: .needsAttention, reasonCode: nil))
        XCTAssertTrue(DeviceRenderAttentionCopy.showsRetryButton(phase: .cancelled, reasonCode: nil))
        XCTAssertTrue(DeviceRenderAttentionCopy.showsRetryButton(phase: .localReady, reasonCode: nil))
        XCTAssertFalse(DeviceRenderAttentionCopy.showsRetryButton(phase: .rendering, reasonCode: nil))
    }

}
