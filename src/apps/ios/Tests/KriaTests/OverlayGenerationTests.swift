import XCTest
import AVFoundation
import UIKit
import KriaMediaEngine
@testable import Kria

final class OverlayGenerationTests: XCTestCase {
    @MainActor func testGeneratedImageUsesOrdinaryVisualCustomizationAndSnapshotPersistence() throws {
        let session = NativeEditorSession(draft: NativeEditorUITestFixtures.captionVisuals)
        let before = session.document
        let asset = CreationVisual(id: "generated-fixture", kind: "image", status: "ready", sourceFilename: "icon.png", displayURL: nil, previewURL: nil, retryable: nil, gcsPath: "fixture/icon.png", sourceURL: nil, durationS: nil, mediaStatus: nil)
        let row = OverlayProposal(id: UUID().uuidString, kind: .image, title: "Icon", prompt: "A clean icon", startS: 0.5, endS: 1.5, xFrac: 0.2, yFrac: 0.3, scale: 0.2, reason: "Synthetic fixture")
        let block = try XCTUnwrap(NativeVisualAuthoring.plannedOverlay(asset: asset, proposal: row, z: 2))
        XCTAssertEqual(block.kind, "media")
        XCTAssertEqual(block.raw["media_kind"], .string("image"))
        XCTAssertEqual(block.raw["origin"], .string("user"))
        session.transactDocument(section: .visualBlocks) { $0.visualBlocks.append(block) }
        session.undo()
        XCTAssertEqual(session.document, before)
        session.redo()
        let selected = EditorSelection(kind: .visualBlock, id: row.id)
        session.setVisualBlockOverlayLayout(id: row.id, x: 0.82, y: 0.4, scale: 0.65)
        session.setVisualBlockTiming(id: row.id, startS: 1, endS: 3)
        session.setVisualBlockTransform(id: row.id, fitMode: "cover", zoom: 2)
        session.setVisualEditorStyle(selected, key: "rotation_deg", value: .number(20))
        session.moveVisualLayer(selected, by: -1)
        let reopened = EditorDocument(snapshot: session.document.encodeSnapshot())
        let saved = try XCTUnwrap(reopened.visualBlocks.first { $0.id == row.id })
        XCTAssertEqual(saved.startS, 1)
        XCTAssertEqual(saved.endS, 3)
        XCTAssertEqual(saved.raw["x_frac"], .number(0.82))
        XCTAssertEqual(saved.raw["y_frac"], .number(0.4))
        XCTAssertEqual(saved.raw["scale"], .number(0.65))
        XCTAssertEqual(saved.raw["transform"]?.objectValue?["zoom"], .number(2))
        XCTAssertEqual(saved.raw["editor_style"]?.objectValue?["rotation_deg"], .number(20))
        XCTAssertEqual(reopened.visualBlocks.count, before.visualBlocks.count + 1)
    }

    @MainActor func testTransparentFixtureOverlayExportsAtItsPlannedWindow() async throws {
        let session = NativeEditorSession(draft: NativeEditorUITestFixtures.sourceText)
        let imageURL = FileManager.default.temporaryDirectory.appendingPathComponent(UUID().uuidString + ".png")
        defer { try? FileManager.default.removeItem(at: imageURL) }
        let format = UIGraphicsImageRendererFormat()
        format.opaque = false
        let graphic = UIGraphicsImageRenderer(size: CGSize(width: 240, height: 240), format: format).image { _ in
            UIColor.magenta.setFill()
            UIBezierPath(ovalIn: CGRect(x: 20, y: 20, width: 200, height: 200)).fill()
        }
        try XCTUnwrap(graphic.pngData()).write(to: imageURL)
        let row = OverlayProposal(id: UUID().uuidString, kind: .image, title: "Synthetic cutout", prompt: "Fixture circle", startS: 0.5, endS: 1.5, xFrac: 0.8, yFrac: 0.2, scale: 0.2, reason: "Offline compositor fixture")
        let asset = CreationVisual(id: "synthetic-overlay", kind: "image", status: "ready", sourceFilename: "fixture.png", displayURL: nil, previewURL: nil, retryable: nil, gcsPath: "fixture.png", sourceURL: nil, durationS: nil, mediaStatus: nil)
        let block = try XCTUnwrap(NativeVisualAuthoring.plannedOverlay(asset: asset, proposal: row, z: 10))
        session.transactDocument(section: .visualBlocks) { $0.visualBlocks.append(block) }
        let fingerprint = try SHA256Fingerprinter().fingerprint(file: imageURL)
        let source = ResolvedEditorSource(clipIndex: -1, mediaID: asset.id,
            asset: MediaAsset(id: asset.id, relativePath: imageURL.lastPathComponent, fingerprint: fingerprint,
                naturalSize: MediaSize(width: 240, height: 240)), url: imageURL)
        let video = try XCTUnwrap(Bundle.main.url(forResource: "montage", withExtension: "mp4"))
        await session.prepareFixtureSourcePreview(url: video, mediaSources: ["visual:" + row.id + ":" + row.id: source])
        XCTAssertEqual(session.sourcePreviewState, .ready)
        let exported = try await session.exportDisplayedSourcePreview()
        defer { try? FileManager.default.removeItem(at: exported.cleanupURL) }
        let generator = AVAssetImageGenerator(asset: AVURLAsset(url: exported.fileURL))
        generator.requestedTimeToleranceBefore = .zero
        generator.requestedTimeToleranceAfter = .zero
        let before = try await generator.image(at: CMTime(seconds: 0.2, preferredTimescale: 600)).image
        let during = try await generator.image(at: CMTime(seconds: 0.8, preferredTimescale: 600)).image
        let after = try await generator.image(at: CMTime(seconds: 1.8, preferredTimescale: 600)).image
        XCTAssertGreaterThan(magentaPixels(during), magentaPixels(before) + 500)
        XCTAssertLessThan(magentaPixels(after), 100)
        let attachment = XCTAttachment(contentsOfFile: exported.fileURL)
        attachment.name = "native-overlay-synthetic-export"
        attachment.lifetime = .keepAlways
        add(attachment)
        let frame = XCTAttachment(image: UIImage(cgImage: during))
        frame.name = "native-overlay-synthetic-frame"
        frame.lifetime = .keepAlways
        add(frame)
    }

    private func magentaPixels(_ image: CGImage) -> Int {
        var bytes = [UInt8](repeating: 0, count: image.width * image.height * 4)
        guard let context = CGContext(data: &bytes, width: image.width, height: image.height, bitsPerComponent: 8,
            bytesPerRow: image.width * 4, space: CGColorSpaceCreateDeviceRGB(), bitmapInfo: CGImageAlphaInfo.premultipliedLast.rawValue) else { return 0 }
        context.draw(image, in: CGRect(x: 0, y: 0, width: image.width, height: image.height))
        return stride(from: 0, to: bytes.count, by: 4).reduce(0) { count, index in
            count + (bytes[index] > 210 && bytes[index + 1] < 50 && bytes[index + 2] > 210 ? 1 : 0)
        }
    }

    @MainActor func testAppliedNumberKeepsAuthoredTimeSurvivesReopenAndUndoesOnce() async throws {
        let session = NativeEditorSession(draft: NativeEditorUITestFixtures.captionVisuals)
        let before = session.document
        let row = OverlayProposal(id: UUID().uuidString, kind: .text, title: "Number", prompt: "", text: "10", startS: 0.5, endS: 1.5, xFrac: 0.2, yFrac: 0.3, scale: 0.2, reason: "Spoken number")
        let plan = OverlayPlan(documentFingerprint: try session.overlayDocumentFingerprint(), baseGeneration: before.revision.baseGeneration, overlays: [row], message: "")
        session.seek(to: 3)
        try await session.applyOverlayProposal(row, assetID: nil, plan: plan)
        try await session.applyOverlayProposal(row, assetID: nil, plan: plan)
        let added = try XCTUnwrap(session.document.textElements.first { $0.id == row.id })
        XCTAssertEqual(added.startS, 0.5)
        XCTAssertEqual(added.endS, 1.5)
        XCTAssertEqual(session.document.textElements.filter { $0.id == row.id }.count, 1)
        XCTAssertEqual(session.document.clips, before.clips)
        let reopened = EditorDocument(snapshot: session.document.encodeSnapshot())
        XCTAssertEqual(reopened.textElements.first { $0.id == row.id }?.text, "10")
        XCTAssertEqual(reopened.textElements.first { $0.id == row.id }?.startS, 0.5)
        session.undo()
        XCTAssertEqual(session.document, before)
        session.redo()
        XCTAssertTrue(session.containsOverlayProposal(row.id))
    }

    @MainActor func testUnsavedChangeRejectsAPlanWithoutAddingAnything() async throws {
        let session = NativeEditorSession(draft: NativeEditorUITestFixtures.captionVisuals)
        let row = OverlayProposal(id: UUID().uuidString, kind: .text, title: "Number", prompt: "", text: "10", startS: 0.5, endS: 1.5, xFrac: 0.2, yFrac: 0.3, scale: 0.2, reason: "Spoken number")
        let plan = OverlayPlan(documentFingerprint: try session.overlayDocumentFingerprint(), baseGeneration: session.document.revision.baseGeneration, overlays: [row], message: "")
        _ = session.addTextCard(text: "An unsaved change", bold: false)
        let changed = session.document
        do {
            try await session.applyOverlayProposal(row, assetID: nil, plan: plan)
            XCTFail("A stale plan changed the edit")
        } catch { }
        XCTAssertEqual(session.document, changed)
    }

    func testTimingRejectsInvalidAndOutOfBoundsProposals() {
        var row = OverlayProposal(id: UUID().uuidString, kind: .text, title: "Number", prompt: "", text: "10", startS: 2, endS: 4, xFrac: 0.2, yFrac: 0.3, scale: 0.2, reason: "Spoken number")
        XCTAssertTrue(row.isValid(duration: 8))
        row.startS = .nan
        XCTAssertFalse(row.isValid(duration: 8))
        row.startS = 2
        row.endS = 9
        XCTAssertFalse(row.isValid(duration: 8))
        row.endS = 2
        XCTAssertFalse(row.isValid(duration: 8))
    }

    func testFingerprintChangesForUnsavedCaptionAndTimelineEdits() throws {
        var value: [String: JSONValue] = ["timeline": .array([.object(["in_s": .number(0)])])]
        let original = try OverlayPlanFingerprint.make(value)
        XCTAssertEqual(original, try OverlayPlanFingerprint.make(value))
        value["caption"] = .string("ten")
        XCTAssertNotEqual(original, try OverlayPlanFingerprint.make(value))
        value["timeline"] = .array([.object(["in_s": .number(1)])])
        XCTAssertNotEqual(original, try OverlayPlanFingerprint.make(value))
    }

    func testPlanPersistsStableProposalIdentityAndManualReviewChanges() throws {
        let row = OverlayProposal(id: UUID().uuidString, kind: .text, title: "Number", prompt: "", text: "10", startS: 2.1, endS: 4.2, xFrac: 0.2, yFrac: 0.3, scale: 0.2, reason: "Spoken number")
        let plan = OverlayPlan(documentFingerprint: "fingerprint", baseGeneration: "base", overlays: [row], message: "")
        let restored = try JSONDecoder().decode(OverlayPlan.self, from: JSONEncoder().encode(plan))
        XCTAssertEqual(restored.overlays.first?.id, row.id)
        XCTAssertEqual(restored.overlays.first?.startS, 2.1)
        XCTAssertEqual(restored.overlays.first?.text, "10")
    }
}
