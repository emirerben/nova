import KriaMediaEngine
import UIKit
import XCTest
@testable import Kria

/// The clip dim row (`clips[i].raw["dim"]`) and its editor mutations. `tests/fixtures/clip-dim-row/v1.json` is read by
/// the server's tests too. Failure modes: Swift and Python disagree on a row; a removed dim leaves a key behind (or
/// omits it, so the server keeps a saved dim); Apply to all misses a clip or dims a blank block; Dim opens without the
/// server's `clips.dim`.
@MainActor
final class ClipDimTests: XCTestCase {
    private func fixture(_ key: String) throws -> [[String: Any]] {
        var url = URL(fileURLWithPath: #filePath)
        for _ in 0..<6 { url.deleteLastPathComponent() }
        let data = try Data(contentsOf: url.appendingPathComponent("tests/fixtures/clip-dim-row/v1.json"))
        let root = try XCTUnwrap(JSONSerialization.jsonObject(with: data) as? [String: Any])
        return try XCTUnwrap(root[key] as? [[String: Any]])
    }

    private func json(_ object: Any?) throws -> JSONValue {
        try JSONDecoder().decode(JSONValue.self, from: JSONSerialization.data(withJSONObject: object as Any, options: .fragmentsAllowed))
    }

    func testSharedFixtureValidDimsNormalize() throws {
        let valid = try fixture("valid")
        XCTAssertGreaterThanOrEqual(valid.count, 8)
        for item in valid {
            let name = item["name"] as? String ?? ""
            let dim = try ClipDim(row: json(item["dim"]))
            XCTAssertEqual(dim?.rowValue, item["normalized"] is NSNull ? nil : try json(item["normalized"]), name)
            if let dim { XCTAssertEqual(try ClipDim(row: dim.rowValue), dim, "\(name) round-trips") }
        }
    }

    func testSharedFixtureInvalidDimsAreRejected() throws {
        let invalid = try fixture("invalid")
        XCTAssertGreaterThanOrEqual(invalid.count, 6)
        for item in invalid {
            XCTAssertThrowsError(try ClipDim(row: json(item["dim"])), item["name"] as? String ?? "")
        }
        XCTAssertThrowsError(try ClipDim(row: .number(.nan)))
        XCTAssertNil(ClipDim(percent: 0))
        XCTAssertEqual(ClipDim(percent: 150)?.strength, 1)
    }

    static func session(dim capability: Bool = true, slots extras: [[String: JSONValue]] = [[:], [:], [:]]) async -> (NativeEditorSession, EditorCommitSpy) {
        let threadID = UUID()
        var authoritative: [String: JSONValue] = [
            "variant_id": .string("initial"), "render_generation_id": .string("generation-1"), "render_status": .string("ready"),
            "duration_s": .number(Double(extras.count) * 2), "output_url": .string("file:///tmp/kria-editor-test.mp4"),
            "resolved_archetype": .string("narrated"), "render_destination": .string("device"), "base_video_path": .string("base.mp4"),
        ]
        var clips: [String: JSONValue] = ["looks": .object(["editable": .bool(false)])]
        if capability { clips["dim"] = .object(["editable": .bool(true), "reason": .null]) }
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

    private func selection(_ session: NativeEditorSession, _ index: Int = 0) -> EditorSelection {
        EditorSelection(kind: .clip, id: session.timelineClips[index].id.uuidString)
    }

    func testSettingADimStoresTheRowAndRemovingItRestoresAByteIdenticalRow() async throws {
        let (session, _) = await Self.session()
        let target = selection(session)
        let original = session.document.clips
        XCTAssertTrue(session.canDimClips)

        session.setClipDim(ClipDim(percent: 50), for: target)
        XCTAssertEqual(session.document.clips[0].raw["dim"], .number(0.5))
        XCTAssertEqual(session.clipDim(for: target)?.percent, 50)
        XCTAssertTrue(session.hasUnsavedChanges)

        session.setClipDim(nil, for: target)
        XCTAssertEqual(session.document.clips, original, "Remove leaves no dim key behind")
    }

    func testRemovingASavedDimSendsAnExplicitNullBecauseAnOmittedKeyKeepsIt() async throws {
        let (session, fake) = await Self.session(slots: [["dim": .number(0.4)], [:]])
        let target = selection(session)
        XCTAssertEqual(session.clipDim(for: target)?.percent, 40)
        session.setClipDim(nil, for: target)
        XCTAssertEqual(session.document.clips[0].raw["dim"], .null)
        await session.save()
        XCTAssertEqual(try XCTUnwrap(fake.lastRequest?.timelineSlots?.first?.objectValue)["dim"], .null)
    }

    func testApplyToAllDimsEveryClipAndOneClipCanThenChangeOnItsOwn() async throws {
        let (session, fake) = await Self.session()
        XCTAssertEqual(session.applyClipDimToAllClips(ClipDim(percent: 40)), 3)
        XCTAssertEqual(session.document.clips.map { $0.raw["dim"] }, [.number(0.4), .number(0.4), .number(0.4)])

        session.setClipDim(nil, for: selection(session, 1))
        session.setClipDim(ClipDim(percent: 70), for: selection(session, 2))
        XCTAssertEqual(session.document.clips.map { $0.raw["dim"] }, [.number(0.4), nil, .number(0.7)])
        await session.save()
        let sent = try XCTUnwrap(fake.lastRequest?.timelineSlots).map { $0.objectValue?["dim"] }
        XCTAssertEqual(sent, [.number(0.4), nil, .number(0.7)])

        XCTAssertEqual(session.applyClipDimToAllClips(nil), 3)
        XCTAssertTrue(session.document.clips.allSatisfy { $0.raw["dim"] == nil || $0.raw["dim"] == .null })
    }

    func testDimIsClosedUnlessTheServerOffersIt() async throws {
        let (session, _) = await Self.session(dim: false)
        let target = selection(session)
        XCTAssertFalse(session.canDimClips)
        XCTAssertNotNil(session.dimBlockedReason(for: target))
        session.setClipDim(ClipDim(percent: 50), for: target)
        XCTAssertEqual(session.applyClipDimToAllClips(ClipDim(percent: 50)), 0)
        XCTAssertTrue(session.document.clips.allSatisfy { $0.raw["dim"] == nil })
    }

    func testTheEditorPreviewCompilesTheRowDimOntoItsClip() throws {
        let url = FileManager.default.temporaryDirectory.appendingPathComponent(UUID().uuidString + ".png")
        defer { try? FileManager.default.removeItem(at: url) }
        try XCTUnwrap(UIGraphicsImageRenderer(size: CGSize(width: 90, height: 160)).image { context in
            UIColor.gray.setFill(); context.fill(CGRect(x: 0, y: 0, width: 90, height: 160))
        }.pngData()).write(to: url)
        let source = ResolvedEditorSource(clipIndex: 0, mediaID: "m",
            asset: MediaAsset(id: "local", relativePath: url.lastPathComponent, fingerprint: try SHA256Fingerprinter().fingerprint(file: url),
                duration: nil, naturalSize: MediaSize(width: 90, height: 160)),
            url: url)
        let clip = EditorClip(id: UUID(), assetID: UUID(), sourceClipIndex: 0, start: 0, end: 2, trimIn: 0, trimOut: 2, sourceDuration: 2, slotID: "slot")
        let compiler = try NativeEditorRenderCompiler(fontDirectory: XCTUnwrap(Bundle.main.url(forResource: "fonts", withExtension: nil)))
        let document = EditorDocument(clips: [.init(id: "slot", clipIndex: 0, inS: 0, durationS: 2, raw: ["dim": .number(0.35)])])
        let program = try compiler.compile(document: document, clips: [clip], items: [], sources: [0: source])
        XCTAssertEqual(program.recipe.tracks.first { $0.kind == .video }?.clips.first?.dim, 0.35)
        XCTAssertTrue(program.recipe.effectiveCapabilities.contains(.clipDim))
    }
}
