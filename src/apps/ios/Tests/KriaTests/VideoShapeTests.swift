import XCTest
import KriaMediaEngine
@testable import Kria

/// KRI-306: the creator's video-shape pick (Vertical / Landscape, Black bars / Crop) --
/// the server's offer, the request keys, the picker state, the editor document and
/// commit, and the preview's letterbox transform.
@MainActor final class VideoShapeTests: XCTestCase {
    override func tearDown() { NativeEditorURLProtocol.handler = nil; super.tearDown() }

    // MARK: Offer decoding

    private func offer(_ json: String) throws -> RenderShapeOffer {
        try JSONDecoder().decode(RenderShapeOffer.self, from: Data(json.utf8))
    }

    func testOfferDecodesTheContractShapeAndSeedsFromDefault() throws {
        let value = try offer(#"{"orientations":["portrait","landscape"],"fit_choices":["fit","fill"],"default":{"output_orientation":"landscape","landscape_fit":"fill"}}"#)
        XCTAssertEqual(value.orientations, ["portrait", "landscape"])
        XCTAssertEqual(value.fitChoices, ["fit", "fill"])
        XCTAssertEqual(value.defaultChoice, RenderShapeChoice(orientation: "landscape", landscapeFit: "fill"))
        XCTAssertTrue(value.isChoosable)
    }

    func testOfferDropsUnknownValuesAndFallsBackToTheFirstAllowedDefault() throws {
        let value = try offer(#"{"orientations":["square","portrait","portrait"],"fit_choices":["fit","zoom"],"default":{"output_orientation":"square","landscape_fit":"zoom"}}"#)
        XCTAssertEqual(value.orientations, ["portrait"], "unknown values are dropped and duplicates collapse")
        XCTAssertEqual(value.fitChoices, ["fit"])
        XCTAssertEqual(value.defaultChoice, RenderShapeChoice(orientation: "portrait", landscapeFit: "fit"))
        XCTAssertFalse(value.isChoosable, "one option per row leaves nothing to choose")
    }

    func testOfferWithNothingUsableFailsToDecode() {
        XCTAssertThrowsError(try offer(#"{"orientations":[],"fit_choices":["fit","fill"]}"#))
        XCTAssertThrowsError(try offer(#"{"orientations":["circle"],"fit_choices":[]}"#))
        XCTAssertThrowsError(try offer("{}"))
    }

    private func thread(extra: String) throws -> CreationThread {
        let decoder = JSONDecoder(); decoder.dateDecodingStrategy = .iso8601
        return try decoder.decode(CreationThread.self, from: Data("""
        {"id":"t","title":"T","status":"active","revision":1,"updated_at":"2026-10-04T10:00:00Z"\(extra)}
        """.utf8))
    }

    func testThreadWithoutRenderShapeDecodesToNilSoTheOldServerHidesThePicker() throws {
        XCTAssertNil(try thread(extra: "").renderShape)
        XCTAssertNil(try thread(extra: #","render_shape":null"#).renderShape)
    }

    func testMalformedOrNothingToChooseRenderShapeNeverBreaksTheThread() throws {
        XCTAssertNil(try thread(extra: #","render_shape":"landscape""#).renderShape)
        XCTAssertNil(try thread(extra: #","render_shape":{"orientations":"portrait"}"#).renderShape)
        XCTAssertNil(try thread(extra: #","render_shape":{"orientations":["portrait"],"fit_choices":["fit"]}"#).renderShape)
        let offered = try thread(extra: #","render_shape":{"orientations":["portrait","landscape"],"fit_choices":["fit","fill"],"default":{"output_orientation":"portrait","landscape_fit":"fit"}}"#).renderShape
        XCTAssertEqual(offered?.defaultChoice, .portraitFit)
    }

    // MARK: Which keys are sent

    func testOnlyOfferedDimensionsAreSent() throws {
        let both = try RenderShapeOffer(orientations: ["portrait", "landscape"], fitChoices: ["fit", "fill"])
        let fitOnly = try RenderShapeOffer(orientations: ["portrait"], fitChoices: ["fit", "fill"])
        let orientationOnly = try RenderShapeOffer(orientations: ["portrait", "landscape"], fitChoices: [])

        let crop = RenderShapeChoice(orientation: "portrait", landscapeFit: "fill")
        XCTAssertEqual(both.wire(for: crop).orientation, "portrait")
        XCTAssertEqual(both.wire(for: crop).landscapeFit, "fill")
        XCTAssertNil(fitOnly.wire(for: crop).orientation, "an orientation nobody offered is never sent")
        XCTAssertEqual(fitOnly.wire(for: crop).landscapeFit, "fill")
        XCTAssertEqual(orientationOnly.wire(for: crop).orientation, "portrait")
        XCTAssertNil(orientationOnly.wire(for: crop).landscapeFit)
    }

    func testLandscapeNeverSendsAFitBecauseLandscapeAlwaysCrops() throws {
        let both = try RenderShapeOffer(orientations: ["portrait", "landscape"], fitChoices: ["fit", "fill"])
        let landscape = RenderShapeChoice(orientation: "landscape", landscapeFit: "fit")
        XCTAssertEqual(both.wire(for: landscape).orientation, "landscape")
        XCTAssertNil(both.wire(for: landscape).landscapeFit)
        XCTAssertFalse(both.showsFitRow(for: landscape))
        XCTAssertEqual(both.payload(for: landscape), ["output_orientation": .string("landscape")])
        XCTAssertEqual(both.payload(for: .portraitFit), ["output_orientation": .string("portrait"), "landscape_fit": .string("fit")])
    }

    func testAStaleChoiceIsCoercedToWhatIsOffered() throws {
        let offer = try RenderShapeOffer(orientations: ["portrait"], fitChoices: ["fill"])
        XCTAssertEqual(offer.normalized(RenderShapeChoice(orientation: "landscape", landscapeFit: "fit")),
                       RenderShapeChoice(orientation: "portrait", landscapeFit: "fill"))
    }

    // MARK: Request body

    func testApprovalOmitsTheShapeKeysWhenNothingWasOffered() async throws {
        var body: [String: Any] = [:]
        NativeEditorURLProtocol.handler = { request in
            body = (try? JSONSerialization.jsonObject(with: NativeEditorTestSupport.bodyData(request))) as? [String: Any] ?? [:]
            return (200, Data("{}".utf8))
        }
        try await NativeEditorTestSupport.api().decideApproval(
            threadID: UUID(), approvalID: UUID(), decision: "approve", expectedThreadRevision: 1, expectedDraftRevision: 1,
            fingerprint: "fp", speechCleanupAware: true, speechCleanupAnalysisID: nil, speechCleanupChoice: nil,
            outputOrientation: nil, landscapeFit: nil
        )
        XCTAssertNil(body["output_orientation"], "absent, never null")
        XCTAssertNil(body["landscape_fit"])
        XCTAssertEqual(body["speech_cleanup_aware"] as? Bool, true)
        // The overload every pre-KRI-306 caller uses sends the same body.
        body = [:]
        try await NativeEditorTestSupport.api().decideApproval(
            threadID: UUID(), approvalID: UUID(), decision: "deny", expectedThreadRevision: 1, expectedDraftRevision: 1,
            fingerprint: "fp", speechCleanupAware: true, speechCleanupAnalysisID: nil, speechCleanupChoice: nil
        )
        XCTAssertNil(body["output_orientation"])
        XCTAssertNil(body["landscape_fit"])
    }

    func testApprovalSendsTheChosenShapeKeys() async throws {
        var body: [String: Any] = [:]
        NativeEditorURLProtocol.handler = { request in
            body = (try? JSONSerialization.jsonObject(with: NativeEditorTestSupport.bodyData(request))) as? [String: Any] ?? [:]
            return (200, Data("{}".utf8))
        }
        try await NativeEditorTestSupport.api().decideApproval(
            threadID: UUID(), approvalID: UUID(), decision: "approve", expectedThreadRevision: 1, expectedDraftRevision: 1,
            fingerprint: "fp", speechCleanupAware: true, speechCleanupAnalysisID: nil, speechCleanupChoice: nil,
            outputOrientation: "portrait", landscapeFit: "fill"
        )
        XCTAssertEqual(body["output_orientation"] as? String, "portrait")
        XCTAssertEqual(body["landscape_fit"] as? String, "fill")
    }

    // MARK: Picker state

    func testPickerSeedsFromTheServerDefaultAndKeepsTheCreatorsPick() throws {
        let offer = try RenderShapeOffer(orientations: ["portrait", "landscape"], fitChoices: ["fit", "fill"],
                                         defaultChoice: RenderShapeChoice(orientation: "portrait", landscapeFit: "fill"))
        var state = RenderShapePickerState()
        state.reset(scope: "approval-1")
        XCTAssertEqual(state.choice(for: offer, scope: "approval-1"), offer.defaultChoice, "seeded from `default`")
        state.select(RenderShapeChoice(orientation: "landscape", landscapeFit: "fill"), scope: "approval-1")
        XCTAssertEqual(state.choice(for: offer, scope: "approval-1").orientation, "landscape")
    }

    func testPickerResetsWhenTheApprovalIDChanges() throws {
        let offer = try RenderShapeOffer(orientations: ["portrait", "landscape"], fitChoices: ["fit", "fill"])
        var state = RenderShapePickerState()
        state.select(RenderShapeChoice(orientation: "landscape", landscapeFit: "fill"), scope: "approval-1")
        // Even before the reset runs, a different approval never inherits the pick.
        XCTAssertEqual(state.choice(for: offer, scope: "approval-2"), offer.defaultChoice)
        state.reset(scope: "approval-2")
        XCTAssertEqual(state.choice(for: offer, scope: "approval-2"), offer.defaultChoice)
        // And coming back to the first approval does not resurrect the old pick.
        XCTAssertEqual(state.choice(for: offer, scope: "approval-1"), offer.defaultChoice)
        // Re-resetting the same scope keeps a pick made since.
        state.select(RenderShapeChoice(orientation: "portrait", landscapeFit: "fill"), scope: "approval-2")
        state.reset(scope: "approval-2")
        XCTAssertEqual(state.choice(for: offer, scope: "approval-2").landscapeFit, "fill")
    }

    // MARK: Editor document, capability, commit

    private func variant(orientation: String?, fit: String?, orientationEditable: Bool = true, fitEditable: Bool = true) -> [String: JSONValue] {
        var variant: [String: JSONValue] = [
            "variant_id": .string("variant"), "render_generation_id": .string("g1"),
            "editor_capabilities": .object([
                "orientation": .object(["editable": .bool(orientationEditable), "value": .string(orientation ?? "portrait"),
                                         "reason": orientationEditable ? .null : .string("orientation_unsupported")]),
                "landscape_fit": .object(["editable": .bool(fitEditable), "value": .string(fit ?? "fit"),
                                           "reason": fitEditable ? .null : .string("fit_unsupported")]),
            ]),
        ]
        if let orientation { variant["orientation"] = .string(orientation) }
        if let fit { variant["landscape_fit"] = .string(fit) }
        return variant
    }

    private func loadedSession(variant: [String: JSONValue]) async -> (NativeEditorSession, EditorCommitSpy) {
        let threadID = UUID()
        let fake = EditorCommitSpy(draftSnapshot: DraftSnapshot(draftID: "d", itemID: "item", variantKey: "variant",
            draftRevision: 1, snapshotHash: "h", etag: "e", baseJobID: UUID().uuidString, baseGenerationID: "g1",
            snapshot: [:], canUndo: false, createdAt: .now), authoritativeVariant: variant)
        let session = NativeEditorSession()
        await session.load(api: fake, threadID: threadID)
        return (session, fake)
    }

    func testDocumentRoundTripsLandscapeFitAndTheCapabilityValue() {
        let snapshot: [String: JSONValue] = [
            "editor_payload": .object(["sections": .object(["orientation": .string("portrait"), "landscape_fit": .string("fit")])]),
            "editor_capabilities": .object([
                "landscape_fit": .object(["editable": .bool(true), "value": .string("fit")]),
                "orientation": .object(["editable": .bool(false), "value": .string("portrait"), "reason": .string("orientation_unsupported")]),
            ]),
        ]
        var document = EditorDocument(snapshot: snapshot)
        XCTAssertEqual(document.landscapeFit, "fit")
        XCTAssertEqual(document.capabilities["landscape_fit"], EditorCapability(editable: true, reason: nil, value: "fit"))
        XCTAssertEqual(document.capabilities["orientation"]?.reason, "orientation_unsupported")
        XCTAssertEqual(document.capabilities["orientation"]?.value, "portrait")

        // Untouched: nothing to write back.
        XCTAssertEqual(document.snapshot(for: .landscapeFit), .string("fit"))
        document.landscapeFit = "fill"
        XCTAssertEqual(document.snapshot(for: .landscapeFit), .string("fill"))
        let reloaded = EditorDocument(snapshot: document.serializedSnapshot())
        XCTAssertEqual(reloaded.landscapeFit, "fill")
    }

    func testSettingTheFitMarksOnlyThatSectionAndCommitsOnlyTheFit() async throws {
        let (session, fake) = await loadedSession(variant: variant(orientation: "portrait", fit: "fit"))
        XCTAssertEqual(session.document.landscapeFit, "fit")
        XCTAssertTrue(session.canEdit(.landscapeFit))
        XCTAssertFalse(session.hasUnsavedChanges)

        session.setVideoShape(landscapeFit: "fill")
        XCTAssertEqual(session.document.landscapeFit, "fill")
        XCTAssertTrue(session.isDirty(.landscapeFit))
        XCTAssertFalse(session.isDirty(.orientation))
        session.undo()
        XCTAssertEqual(session.document.landscapeFit, "fit")
        XCTAssertFalse(session.hasUnsavedChanges, "undo returns to the loaded value")

        session.setVideoShape(landscapeFit: "fill")
        await session.save()
        XCTAssertEqual(fake.lastRequest?.landscapeFit, "fill")
        XCTAssertNil(fake.lastRequest?.orientation)
        let encoded = try XCTUnwrap(try JSONSerialization.jsonObject(with: JSONEncoder().encode(XCTUnwrap(fake.lastRequest))) as? [String: Any])
        XCTAssertEqual(encoded["landscape_fit"] as? String, "fill")
        XCTAssertNil(encoded["orientation"], "an unchanged section is omitted from the commit")
    }

    func testSettingTheOrientationCommitsTheWireValue() async throws {
        let (session, fake) = await loadedSession(variant: variant(orientation: "portrait", fit: "fit"))
        session.setVideoShape(orientation: "landscape")
        XCTAssertEqual(session.previewAspectRatio, 16.0 / 9.0, accuracy: 0.001, "the preview follows the pick")
        await session.save()
        XCTAssertEqual(fake.lastRequest?.orientation, "landscape")
        XCTAssertNil(fake.lastRequest?.landscapeFit)
    }

    func testClosedCapabilitiesRefuseTheEditEvenFromAStaleUI() async {
        let (session, _) = await loadedSession(variant: variant(orientation: "portrait", fit: "fit", orientationEditable: false, fitEditable: false))
        XCTAssertFalse(session.canEdit(.orientation))
        XCTAssertFalse(session.canEdit(.landscapeFit))
        XCTAssertEqual(session.capability("orientation")?.reason, "orientation_unsupported")
        session.setVideoShape(orientation: "landscape", landscapeFit: "fill")
        XCTAssertEqual(session.document.orientation, "portrait")
        XCTAssertEqual(session.document.landscapeFit, "fit")
        XCTAssertFalse(session.hasUnsavedChanges)
    }

    func testOrientationAndFitFollowTheirOwnCapability() async {
        let (session, _) = await loadedSession(variant: variant(orientation: "portrait", fit: "fit", orientationEditable: false, fitEditable: true))
        session.setVideoShape(orientation: "landscape", landscapeFit: "fill")
        XCTAssertEqual(session.document.orientation, "portrait", "orientation stays closed")
        XCTAssertEqual(session.document.landscapeFit, "fill", "fit is open")
    }

    func testAServerWithoutEitherCapabilityKeepsTheReadOnlyRow() async {
        let (session, _) = await loadedSession(variant: ["variant_id": .string("variant"), "render_generation_id": .string("g1"), "editor_capabilities": .object(["timeline": .bool(true)])])
        XCTAssertFalse(session.hasVideoShapeCapability)
        XCTAssertFalse(session.canEdit(.orientation))
        XCTAssertEqual(NativeVideoShape.orientationLabel(session.document.orientation), "Vertical 9:16")
    }

    func testSectionCopyTurnsServerCodesIntoSentences() {
        XCTAssertEqual(NativeVideoShapeSection.copy(for: "orientation_unsupported", axis: .orientation), "This edit’s format can’t change shape.")
        XCTAssertEqual(NativeVideoShapeSection.copy(for: "cloud_unsupported", axis: .fit), "Black bars and crop can’t be changed for this edit.")
        XCTAssertEqual(NativeVideoShapeSection.copy(for: "landscape_output", axis: .fit), "Save the switch to vertical first, then you can choose black bars or crop.")
        XCTAssertEqual(NativeVideoShapeSection.copy(for: "disabled", axis: .orientation), "Changing the video shape isn’t available right now.")
        XCTAssertEqual(NativeVideoShapeSection.copy(for: "Orientation is fixed by the rendered variant.", axis: .orientation), "Orientation is fixed by the rendered variant.")
        XCTAssertEqual(NativeVideoShape.normalizedOrientation("16:9"), "landscape")
        XCTAssertEqual(EditorDocument.canonicalOrientation("9:16"), "portrait")
        XCTAssertEqual(EditorDocument.canonicalOrientation("square"), "square", "no picker entry, kept as is")
        XCTAssertEqual(NativeVideoShape.normalizedOrientation(nil), "portrait")
    }

    /// KRI-431: a saved-landscape video with an unsaved Vertical pick used to read
    /// "Black bars and crop only apply to vertical videos", which contradicted the pick.
    func testPickingVerticalOnALandscapeVideoNeverSaysBarsOnlyApplyToVertical() {
        let message = NativeVideoShapeSection.lockedReason(
            orientationEditable: true, orientationReason: nil,
            fitEditable: false, fitReason: "landscape_output",
            pickedOrientation: RenderShapeOffer.portrait
        )
        XCTAssertEqual(message, "Save the switch to vertical first, then you can choose black bars or crop.")
        XCTAssertFalse(message?.contains("only apply to vertical") ?? false)
    }

    func testLockedReasonStaysQuietWhereTheFitRowIsHiddenOrOpen() {
        // Landscape picked: the fit row is hidden, so no line about it.
        XCTAssertNil(NativeVideoShapeSection.lockedReason(
            orientationEditable: true, orientationReason: nil,
            fitEditable: false, fitReason: "landscape_output",
            pickedOrientation: RenderShapeOffer.landscape
        ))
        // Fit open: nothing to explain.
        XCTAssertNil(NativeVideoShapeSection.lockedReason(
            orientationEditable: true, orientationReason: nil,
            fitEditable: true, fitReason: nil,
            pickedOrientation: RenderShapeOffer.portrait
        ))
        // A closed orientation row still wins and keeps its own reason.
        XCTAssertEqual(NativeVideoShapeSection.lockedReason(
            orientationEditable: false, orientationReason: "orientation_unsupported",
            fitEditable: true, fitReason: nil,
            pickedOrientation: RenderShapeOffer.portrait
        ), "This edit’s format can’t change shape.")
    }

    // MARK: Preview transform

    func testFitTransformMatchesTheBackendLetterboxMath() {
        let portrait = KriaMediaEngine.Canvas(width: 1080, height: 1920)
        let landscape = MediaSize(width: 1920, height: 1080)
        // phone_recipe_shared.fit_transform: contain / cover = 0.31640625 for 1920x1080 into 1080x1920.
        XCTAssertEqual(NativeEditorRenderCompiler.fitTransform(display: landscape, canvas: portrait, landscapeFit: "fit").scale, 0.31640625, accuracy: 1e-9)
        XCTAssertEqual(NativeEditorRenderCompiler.fitTransform(display: landscape, canvas: portrait, landscapeFit: "fill"), .identity)
        XCTAssertEqual(NativeEditorRenderCompiler.fitTransform(display: MediaSize(width: 1080, height: 1920), canvas: portrait, landscapeFit: "fit"), .identity)
        XCTAssertEqual(NativeEditorRenderCompiler.fitTransform(display: MediaSize(width: 1000, height: 1000), canvas: portrait, landscapeFit: "fit"), .identity)
        XCTAssertEqual(NativeEditorRenderCompiler.fitTransform(display: nil, canvas: portrait, landscapeFit: "fit"), .identity)
        // Landscape output always fills.
        XCTAssertEqual(NativeEditorRenderCompiler.fitTransform(display: MediaSize(width: 1440, height: 1080), canvas: .init(width: 1920, height: 1080), landscapeFit: "fit"), .identity)
    }

    private func compiledTransform(fit: String?, capability: EditorCapability?, raw: [String: JSONValue] = [:],
                                   trimOut: Double = 3, orientation: String? = nil) throws -> MediaTransform? {
        let fingerprint = AssetFingerprint(hex: String(repeating: "a", count: 64), byteCount: 100)
        let source = ResolvedEditorSource(clipIndex: 0, mediaID: "original",
            asset: MediaAsset(id: "local", relativePath: "original.mp4", fingerprint: fingerprint, duration: 4,
                              naturalSize: MediaSize(width: 1920, height: 1080)),
            url: URL(fileURLWithPath: "/fixture/original.mp4"))
        let clip = EditorClip(id: UUID(), assetID: UUID(), sourceClipIndex: 0, start: 0, end: 3, trimIn: 0, trimOut: trimOut, sourceDuration: 4, slotID: "slot")
        let compiler = try NativeEditorRenderCompiler(fontDirectory: XCTUnwrap(Bundle.main.url(forResource: "fonts", withExtension: nil)))
        let document = EditorDocument(clips: [.init(id: "slot", clipIndex: 0, inS: 0, durationS: 3, raw: raw)], orientation: orientation, landscapeFit: fit,
                                      capabilities: capability.map { ["landscape_fit": $0] } ?? [:])
        return try compiler.compile(document: document, clips: [clip], items: [], sources: [0: source]).recipe.tracks.first?.clips.first?.transform
    }

    func testCompilerLetterboxesASidewaysClipOnlyWhenBlackBarsAreChosen() throws {
        let open = EditorCapability(editable: true, value: "fit")
        XCTAssertEqual(try XCTUnwrap(compiledTransform(fit: "fit", capability: open)).scale, 0.31640625, accuracy: 1e-9)
        XCTAssertEqual(try compiledTransform(fit: "fill", capability: open), .identity)
        XCTAssertEqual(try compiledTransform(fit: nil, capability: EditorCapability(editable: true)), .identity, "no value anywhere reads as fill")
        XCTAssertEqual(try compiledTransform(fit: "fit", capability: open, raw: ["source_crop": .object(["x": .number(0.1), "y": .number(0.1), "width": .number(0.5), "height": .number(0.5)])]), .identity,
                       "a cropped clip keeps the engine's cover-fill")
    }

    /// The picker and the preview read ONE resolver: a variant rendered before `landscape_fit` was stored
    /// carries only the capability value, and both must treat it as the value in force.
    func testPreviewFollowsTheCapabilityValueWhenTheDocumentStoresNoFit() throws {
        let open = EditorCapability(editable: true, value: "fit")
        XCTAssertEqual(try XCTUnwrap(compiledTransform(fit: nil, capability: open)).scale, 0.31640625, accuracy: 1e-9)
        let document = EditorDocument(capabilities: ["landscape_fit": open])
        XCTAssertEqual(document.effectiveLandscapeFit, "fit")
        XCTAssertTrue(document.previewLetterboxesSidewaysClips)
        XCTAssertEqual(EditorDocument(capabilities: ["landscape_fit": EditorCapability(editable: true, value: "fill")]).effectiveLandscapeFit, "fill")
        XCTAssertEqual(EditorDocument().effectiveLandscapeFit, "fill")
    }

    func testPreviewIgnoresFitWhereTheFormatDoesNotHonourIt() throws {
        // No capability (older server / unknown format): never letterbox.
        XCTAssertEqual(try compiledTransform(fit: "fit", capability: nil), .identity)
        // Narrated / speech montage and cloud renders ignore the choice.
        XCTAssertEqual(try compiledTransform(fit: "fit", capability: EditorCapability(editable: false, reason: "unsupported_archetype", value: "fit")), .identity)
        XCTAssertEqual(try compiledTransform(fit: "fit", capability: EditorCapability(editable: false, reason: "cloud_unsupported", value: "fit")), .identity)
        // A fit-capable format that is merely not editable here still shows its bars.
        XCTAssertEqual(try XCTUnwrap(compiledTransform(fit: "fit", capability: EditorCapability(editable: false, reason: "phone_edit_unsupported", value: "fit"))).scale, 0.31640625, accuracy: 1e-9)
        // A held clip (source shorter than its slot) is not fitted, like the backend's `_fit_eligible`.
        XCTAssertEqual(try compiledTransform(fit: "fit", capability: EditorCapability(editable: true, value: "fit"), raw: ["playback_rate": .number(1)], trimOut: 1), .identity)
    }

    func testPreviewCanvasAndAspectRatioAgreeOnLegacyOrientationSpellings() async throws {
        XCTAssertEqual(EditorDocument.canonicalOrientation("16:9"), "landscape")
        let (session, _) = await loadedSession(variant: variant(orientation: "16:9", fit: "fill"))
        XCTAssertEqual(session.document.orientation, "landscape")
        XCTAssertEqual(session.previewAspectRatio, 16.0 / 9.0, accuracy: 0.001)
    }

    // MARK: Picker agrees with the preview; clean baselines

    func testLoadedVariantWithoutAStoredFitSeedsFromTheCapabilityAndSwitchingWorks() async {
        var legacy = variant(orientation: "portrait", fit: nil)   // capability value "fit", no stored landscape_fit
        legacy["landscape_fit"] = nil
        let (session, _) = await loadedSession(variant: legacy)
        XCTAssertEqual(session.document.effectiveLandscapeFit, "fit", "picker shows Black bars")
        XCTAssertTrue(session.document.previewLetterboxesSidewaysClips, "and the preview letterboxes")
        XCTAssertFalse(session.hasUnsavedChanges)
        session.setVideoShape(landscapeFit: "fit")
        XCTAssertFalse(session.hasUnsavedChanges, "tapping the value in force is a no-op")
        session.setVideoShape(landscapeFit: "fill")
        XCTAssertEqual(session.document.effectiveLandscapeFit, "fill")
        XCTAssertFalse(session.document.previewLetterboxesSidewaysClips)
        XCTAssertTrue(session.isDirty(.landscapeFit))
        session.setVideoShape(landscapeFit: "fit")
        XCTAssertFalse(session.isDirty(.landscapeFit), "back to the loaded value is not an edit")
    }

    func testTogglingLandscapeAndBackIsNotAnEditForNilOrLegacyOrientation() async {
        for stored in [nil, "9:16"] as [String?] {
            var value = variant(orientation: "portrait", fit: "fit")
            if let stored { value["orientation"] = .string(stored) } else { value["orientation"] = nil }
            let (session, _) = await loadedSession(variant: value)
            XCTAssertEqual(session.document.orientation, "portrait", "\(stored ?? "nil") reads as portrait")
            session.setVideoShape(orientation: "landscape")
            XCTAssertTrue(session.isDirty(.orientation))
            session.setVideoShape(orientation: "portrait")
            XCTAssertFalse(session.isDirty(.orientation), "\(stored ?? "nil"): back to portrait is not a re-render")
            XCTAssertFalse(session.hasUnsavedChanges)
        }
    }

    func testHeaderButtonNeedsAnEditableAxis() async {
        let (closed, _) = await loadedSession(variant: variant(orientation: "portrait", fit: "fit", orientationEditable: false, fitEditable: false))
        XCTAssertTrue(closed.hasVideoShapeCapability, "the keys exist (a cloud map always carries them)")
        XCTAssertFalse(closed.canEditVideoShape, "but nothing can change, so no button")
        let (fitOnly, _) = await loadedSession(variant: variant(orientation: "portrait", fit: "fit", orientationEditable: false, fitEditable: true))
        XCTAssertTrue(fitOnly.canEditVideoShape)
        let (orientationOnly, _) = await loadedSession(variant: variant(orientation: "portrait", fit: "fit", orientationEditable: true, fitEditable: false))
        XCTAssertTrue(orientationOnly.canEditVideoShape)
    }
}
