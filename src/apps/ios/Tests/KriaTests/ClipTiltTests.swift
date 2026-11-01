import KriaMediaEngine
import XCTest
@testable import Kria

/// KRI-1136: the clip tilt row (`clips[i].raw["tilt"]`), its editor mutations and its compiler mapping.
/// `tests/fixtures/clip-tilt-row/v1.json` is read by the server's tests too.
@MainActor
final class ClipTiltTests: XCTestCase {
    // MARK: Row contract (shared fixture)

    private func fixture(_ key: String) throws -> [[String: Any]] {
        var url = URL(fileURLWithPath: #filePath)
        for _ in 0..<6 { url.deleteLastPathComponent() }
        let data = try Data(contentsOf: url.appendingPathComponent("tests/fixtures/clip-tilt-row/v1.json"))
        let root = try XCTUnwrap(JSONSerialization.jsonObject(with: data) as? [String: Any])
        return try XCTUnwrap(root[key] as? [[String: Any]])
    }

    private func json(_ object: Any?) throws -> JSONValue {
        try JSONDecoder().decode(JSONValue.self, from: JSONSerialization.data(withJSONObject: object as Any, options: .fragmentsAllowed))
    }

    func testSharedFixtureValidTiltsNormalizeAndMapToRecipeFields() throws {
        let valid = try fixture("valid")
        XCTAssertGreaterThanOrEqual(valid.count, 14)
        for item in valid {
            let name = item["name"] as? String ?? ""
            let tilt = try ClipTilt(row: json(item["tilt"])) ?? .upright
            let normalized = item["normalized"] is NSNull ? nil : try json(item["normalized"])
            XCTAssertEqual(tilt.rowValue, normalized, name)
            let recipe = try XCTUnwrap(item["recipe"] as? [String: Any])
            XCTAssertEqual(tilt.recipeRotationDegrees, try XCTUnwrap(recipe["rotation_degrees"] as? Double), name)
            let backdrop = recipe["backdrop"] is NSNull ? nil : try RecipeJSON.decoder().decode(
                ClipBackdrop.self, from: JSONSerialization.data(withJSONObject: recipe["backdrop"] as Any))
            XCTAssertEqual(tilt.recipeBackdrop, backdrop, name)
            XCTAssertEqual(try ClipTilt(row: tilt.rowValue) ?? .upright, tilt, "\(name) round-trips")
        }
    }

    func testSharedFixtureInvalidTiltsAreRejected() throws {
        let invalid = try fixture("invalid")
        XCTAssertGreaterThanOrEqual(invalid.count, 10)
        for item in invalid {
            XCTAssertThrowsError(try ClipTilt(row: json(item["tilt"])), item["name"] as? String ?? "")
        }
        for degrees in [Double.nan, .infinity, -.infinity] {
            XCTAssertThrowsError(try ClipTilt(row: .object(["rotation_deg": .number(degrees)])))
        }
        XCTAssertEqual(ClipTilt(rotationDeg: .nan), .upright, "a non-finite angle can never be stored")
    }

    func testABlankBackdropIsAbsentFromTheStoredRow() {
        XCTAssertEqual(ClipTilt(rotationDeg: 30).rowValue, .object(["rotation_deg": .number(30)]))
        XCTAssertEqual(ClipTilt(rotationDeg: 12.3456).rotationDeg, 12.35, "rounded to hundredths like the server")
    }

    func testAnAbsentOrNullRowIsNoTilt() throws {
        XCTAssertNil(try ClipTilt(row: nil))
        XCTAssertNil(try ClipTilt(row: .null))
    }

    func testRotationWrapsAndAQuarterTurnGoesClockwise() {
        XCTAssertEqual(ClipTilt(rotationDeg: 190).rotationDeg, -170)
        XCTAssertEqual(ClipTilt(rotationDeg: -180).rotationDeg, 180)
        XCTAssertEqual(ClipTilt(rotationDeg: 360).rotationDeg, 0)
        XCTAssertEqual(ClipTilt(rotationDeg: 0).turnedQuarter().rotationDeg, 90)
        XCTAssertEqual(ClipTilt(rotationDeg: 180).turnedQuarter().rotationDeg, -90)
        var tilt = ClipTilt(rotationDeg: 179)
        tilt.rotationDeg += 2
        XCTAssertEqual(tilt.rotationDeg, -179, "assigning past the end wraps")
    }

    func testTheDialAppliesTheSharedNinetyDegreeDetentAndSignConvention() {
        XCTAssertEqual(TiltDial.angle(atX: 150, width: 300), 0, "the middle of the track is upright")
        XCTAssertEqual(TiltDial.angle(atX: 300, width: 300), 180)
        XCTAssertEqual(TiltDial.angle(atX: 0, width: 300), -180)
        XCTAssertGreaterThan(TiltDial.angle(atX: 200, width: 300), 0, "dragging right turns clockwise")
        XCTAssertLessThan(TiltDial.angle(atX: 100, width: 300), 0)
        XCTAssertEqual(TiltDial.angle(atX: 150 + 150.0 / 180.0 * 91, width: 300), 90, "within the hold band it lands on 90°")
        XCTAssertEqual(TiltDial.angle(atX: 150 + 150.0 / 180.0 * 85, width: 300), 85, accuracy: 4, "outside it follows the finger")
    }

    // MARK: Editor session

    private static let tiltOpen: JSONValue = .object(["editable": .bool(true), "reason": .null])

    private static func session(tilt capability: JSONValue? = tiltOpen, slots extras: [[String: JSONValue]] = [[:]]) async -> (NativeEditorSession, EditorCommitSpy) {
        let threadID = UUID()
        var authoritative: [String: JSONValue] = [
            "variant_id": .string("initial"), "render_generation_id": .string("generation-1"), "render_status": .string("ready"),
            "duration_s": .number(Double(extras.count) * 2), "output_url": .string("file:///tmp/kria-editor-test.mp4"),
            "resolved_archetype": .string("narrated"), "render_destination": .string("device"),
            "base_video_path": .string("base.mp4"),
        ]
        var clips: [String: JSONValue] = ["looks": .object(["editable": .bool(false)])]
        if let capability { clips["tilt"] = capability }
        authoritative["editor_capabilities"] = .object(["timeline": .bool(true), "text_elements": .bool(true), "mix": .bool(false), "clips": .object(clips)])
        let slots = extras.enumerated().map { index, extra -> JSONValue in
            var slot: [String: JSONValue] = ["slot_id": .string("slot-\(index)"), "clip_index": .number(Double(index)), "in_s": .number(0),
                                             "duration_s": .number(2), "source_duration_s": .number(4), "removed": .bool(false)]
            slot.merge(extra) { _, new in new }
            return .object(slot)
        }
        authoritative["user_timeline"] = .object(["slots": .array(slots)])
        let fake = EditorCommitSpy(
            draftSnapshot: DraftSnapshot(draftID: "d", itemID: "item", variantKey: "variant", draftRevision: 4, snapshotHash: "h", etag: "e", baseJobID: UUID().uuidString, baseGenerationID: "generation-1", snapshot: [:], canUndo: false, createdAt: .now),
            authoritativeVariant: authoritative
        )
        let session = NativeEditorSession(draft: EditorDraft(projectID: threadID, clips: [], text: [], captions: CaptionStyle(enabled: false, style: "sentence"), music: nil, revision: 4))
        await session.load(api: fake, threadID: threadID)
        return (session, fake)
    }

    private func selection(_ session: NativeEditorSession, _ index: Int = 0) throws -> EditorSelection {
        EditorSelection(kind: .clip, id: session.timelineClips[index].id.uuidString)
    }

    private static let blur = ClipTilt(rotationDeg: 15, backdrop: .blur)

    func testSettingATiltStoresTheRowAndResetRestoresAByteIdenticalRow() async throws {
        let (session, _) = await Self.session()
        let target = try selection(session)
        let original = session.document.clips
        XCTAssertTrue(session.canTiltClips)

        session.setClipTilt(Self.blur, for: target)
        XCTAssertEqual(session.document.clips[0].raw["tilt"], .object(["rotation_deg": .number(15), "backdrop": .object(["kind": .string("blur")])]))
        XCTAssertEqual(session.clipTilt(for: target), Self.blur)
        XCTAssertTrue(session.hasUnsavedChanges)

        session.setClipTilt(nil, for: target)
        XCTAssertEqual(session.document.clips, original, "Reset leaves no tilt key behind")
        XCTAssertNil(session.document.clips[0].raw["tilt"])

        session.setClipTilt(Self.blur, for: target)
        session.setClipTilt(ClipTilt(rotationDeg: 0, backdrop: .blank), for: target)
        XCTAssertEqual(session.document.clips, original, "an upright blank dial is the same as Reset")
    }

    func testResettingASavedTiltSendsAnExplicitNullBecauseAnOmittedKeyKeepsIt() async throws {
        let saved: JSONValue = .object(["rotation_deg": .number(30), "backdrop": .object(["kind": .string("blank")])])
        let (session, fake) = await Self.session(slots: [["tilt": saved]])
        let target = try selection(session)
        XCTAssertEqual(session.clipTilt(for: target), ClipTilt(rotationDeg: 30))

        session.setClipTilt(nil, for: target)
        XCTAssertEqual(session.document.clips[0].raw["tilt"], .null)
        XCTAssertNil(session.clipTilt(for: target))
        await session.save()
        XCTAssertEqual(try XCTUnwrap(fake.lastRequest?.timelineSlots?.first?.objectValue)["tilt"], .null)
    }

    func testSaveSendsTheTiltAsPartOfTheTimelineSlot() async throws {
        let (session, fake) = await Self.session()
        session.setClipTilt(Self.blur, for: try selection(session))
        await session.save()
        let sent = try XCTUnwrap(fake.lastRequest?.timelineSlots?.first?.objectValue)
        XCTAssertEqual(sent["tilt"]?.objectValue?["rotation_deg"], .number(15))
        XCTAssertEqual(sent["tilt"]?.objectValue?["backdrop"]?.objectValue?["kind"], .string("blur"))
    }

    func testTiltIsClosedUnlessTheServerExplicitlyOffersIt() async throws {
        for capability in [nil, JSONValue.object(["editable": .bool(false), "reason": .string("phone_edit_unsupported")])] {
            let (session, _) = await Self.session(tilt: capability)
            let target = try selection(session)
            let before = session.document
            XCTAssertFalse(session.canTiltClips)
            XCTAssertNotNil(session.tiltBlockedReason(for: target))
            session.setClipTilt(Self.blur, for: target)
            XCTAssertEqual(session.document, before)
            XCTAssertFalse(session.hasUnsavedChanges)
        }
    }

    func testAClipWithALookCannotTiltAndSaysWhy() async throws {
        let (session, _) = await Self.session(slots: [["look_preset": .string("golden_hour")]])
        let target = try selection(session)
        let reason = try XCTUnwrap(session.tiltBlockedReason(for: target))
        XCTAssertTrue(reason.contains("look"), reason)
        let before = session.document
        session.setClipTilt(Self.blur, for: target)
        XCTAssertEqual(session.document, before)
    }

    func testApplyingABackdropToAllClipsLeavesTheOnesThatCannotTiltExactlyAsTheyWere() async throws {
        let (session, _) = await Self.session(slots: [[:], ["look_preset": .string("golden_hour")], ["tilt": .object([
            "rotation_deg": .number(-20), "backdrop": .object(["kind": .string("blank")])])]])
        let beforeLook = session.document.clips[1]

        let result = session.applyTiltBackdropToAllClips(.color("#E91E8C"))

        XCTAssertEqual(result.applied, 2)
        XCTAssertEqual(result.skipped, 1)
        XCTAssertEqual(session.document.clips[1], beforeLook, "the graded clip is untouched")
        XCTAssertEqual(try ClipTilt(row: session.document.clips[0].raw["tilt"]), ClipTilt(rotationDeg: 0, backdrop: .color("#E91E8C")))
        XCTAssertEqual(try ClipTilt(row: session.document.clips[2].raw["tilt"]), ClipTilt(rotationDeg: -20, backdrop: .color("#E91E8C")),
                       "each clip keeps its own angle")
        session.undo()
        XCTAssertNil(session.document.clips[0].raw["tilt"], "one undo step")
    }

    func testApplyingABackdropWithTiltClosedChangesNothing() async throws {
        let (session, _) = await Self.session(tilt: nil, slots: [[:], [:]])
        let before = session.document
        XCTAssertEqual(session.applyTiltBackdropToAllClips(.blur).applied, 0)
        XCTAssertEqual(session.document, before)
    }

    // MARK: Compiler

    private func compiled(raw: [String: JSONValue], look: String? = nil, fit: Bool = false, size: MediaSize = MediaSize(width: 1920, height: 1080)) throws -> TimelineClip {
        let fingerprint = AssetFingerprint(hex: String(repeating: "a", count: 64), byteCount: 100)
        let source = ResolvedEditorSource(clipIndex: 0, mediaID: "original",
            asset: MediaAsset(id: "local", relativePath: "original.mp4", fingerprint: fingerprint, duration: 4, naturalSize: size),
            url: URL(fileURLWithPath: "/fixture/original.mp4"))
        let clip = EditorClip(id: UUID(), assetID: UUID(), sourceClipIndex: 0, start: 0, end: 3, trimIn: 0, trimOut: 3, sourceDuration: 4, slotID: "slot")
        let compiler = try NativeEditorRenderCompiler(fontDirectory: XCTUnwrap(Bundle.main.url(forResource: "fonts", withExtension: nil)))
        let document = EditorDocument(clips: [.init(id: "slot", clipIndex: 0, inS: 0, durationS: 3, lookPreset: look, raw: raw)], landscapeFit: fit ? "fit" : "fill",
                                      capabilities: fit ? ["landscape_fit": EditorCapability(editable: true, value: "fit")] : [:])
        let program = try compiler.compile(document: document, clips: [clip], items: [], sources: [0: source])
        return try XCTUnwrap(program.recipe.tracks.first?.clips.first)
    }

    func testCompilerMapsTheRowOntoTheClipTransformAndBackdrop() throws {
        let clip = try compiled(raw: ["tilt": Self.blur.rowValue!])
        XCTAssertEqual(clip.transform.rotationDegrees, 15)
        XCTAssertEqual(clip.backdrop, .blur)
        let plain = try compiled(raw: [:])
        XCTAssertEqual(plain.transform, .identity)
        XCTAssertNil(plain.backdrop)
        let blank = try compiled(raw: ["tilt": ClipTilt(rotationDeg: -30).rowValue!])
        XCTAssertEqual(blank.transform.rotationDegrees, -30)
        XCTAssertNil(blank.backdrop, "a blank backdrop is no backdrop, like the server")
    }

    func testCompilerComposesTheTiltWithTheLetterboxInsteadOfReplacingIt() throws {
        let clip = try compiled(raw: ["tilt": Self.blur.rowValue!], fit: true)
        XCTAssertEqual(clip.transform.scale, 0.31640625, accuracy: 1e-9, "the bars survive the tilt")
        XCTAssertEqual(clip.transform.rotationDegrees, 15)
    }

    func testCompilerKeepsAGradedClipUprightAndRejectsAMalformedRow() throws {
        let graded = try compiled(raw: ["tilt": Self.blur.rowValue!], look: "golden_hour", size: MediaSize(width: 1080, height: 1920))
        XCTAssertEqual(graded.transform.rotationDegrees, 0)
        XCTAssertNil(graded.backdrop)
        XCTAssertThrowsError(try compiled(raw: ["tilt": .object(["rotation_deg": .string("15")])]))
    }

    func testCompilerKeepsACropAndATiltTogether() throws {
        let crop: JSONValue = .object(["x": .number(0.1), "y": .number(0.1), "width": .number(0.5), "height": .number(0.5)])
        let clip = try compiled(raw: ["tilt": Self.blur.rowValue!, "source_crop": crop])
        XCTAssertEqual(clip.sourceCrop, .init(x: 0.1, y: 0.1, width: 0.5, height: 0.5))
        XCTAssertEqual(clip.transform.rotationDegrees, 15)
    }
}
