import AVFoundation
import CoreImage
import ImageIO
import KriaMediaEngine
import XCTest
@testable import Kria

/// KRI-1139: a pool photo or video behind a tilted clip in the editor: the session gate, the row it writes, the
/// compiler's pinned `visual-<pool id>` asset (and its soft failure when the bytes are gone), and the real compositor's
/// pixels for the recipe the editor builds. `tests/fixtures/clip-tilt-row/v1.json` covers the row contract in
/// `ClipTiltTests`.
@MainActor
final class ClipTiltMediaBackdropTests: XCTestCase {
    private let poolID = "5b2f9d1e-8c3a-4f6b-9e21-7a0d4c3b2a10"
    private let width = 180, height = 320
    private let quadrants: [[Int]] = [[220, 40, 40], [40, 200, 60], [50, 60, 230], [230, 210, 40]]
    private let open: JSONValue = .object(["editable": .bool(true), "reason": .null])

    private var media: ClipTilt { ClipTilt(rotationDeg: 15, backdrop: .media(poolID)) }

    private func visual(_ id: String? = nil, kind: String = "image", status: String = "ready") -> CreationVisual {
        CreationVisual(id: id ?? poolID, kind: kind, status: status, sourceFilename: "beach.jpg", displayURL: nil, previewURL: nil, retryable: nil)
    }

    private func session(media capability: JSONValue?, pool: [CreationVisual] = [], slots: [[String: JSONValue]] = [[:]]) async throws
        -> (NativeEditorSession, EditorCommitSpy, EditorSelection) {
        let (session, fake) = await ClipTiltTests.session(backdropMedia: capability, slots: slots)
        fake.visualPool = pool
        await session.refreshVisualLibrary()
        let target = EditorSelection(kind: .clip, id: session.timelineClips[0].id.uuidString)
        return (session, fake, target)
    }

    // MARK: Session

    func testMediaBackdropIsOfferedOnlyWhereTheServerListsIt() async throws {
        let (closed, _, closedTarget) = try await session(media: nil, pool: [visual()])
        XCTAssertTrue(closed.canTiltClips)
        XCTAssertFalse(closed.canTiltBackdropMedia, "tilt open is not enough")
        let before = closed.document
        closed.setClipTilt(media, for: closedTarget)
        XCTAssertEqual(closed.document, before, "a media backdrop never reaches the row while its gate is closed")
        closed.setClipTilt(ClipTilt(rotationDeg: 15, backdrop: .blur), for: closedTarget)
        XCTAssertNotEqual(closed.document, before, "blur still works")
        XCTAssertEqual(closed.applyTiltBackdropToAllClips(.media(poolID)).applied, 0)

        let (opened, _, target) = try await session(media: open, pool: [visual()])
        XCTAssertTrue(opened.canTiltBackdropMedia)
        opened.setClipTilt(media, for: target)
        XCTAssertEqual(opened.document.clips[0].raw["tilt"], .object([
            "rotation_deg": .number(15), "backdrop": .object(["kind": .string("media"), "asset_id": .string(poolID)]),
        ]))
        XCTAssertEqual(opened.clipTilt(for: target), media)
        XCTAssertEqual(opened.tiltBackdropChoices.map(\.id), [poolID])
    }

    func testResetReturnsToBlankAndSaveSendsTheMediaBackdrop() async throws {
        let (session, fake, target) = try await session(media: open, pool: [visual()])
        session.setClipTilt(media, for: target)
        await session.save()
        let sent = try XCTUnwrap(fake.lastRequest?.timelineSlots?.first?.objectValue)
        XCTAssertEqual(sent["tilt"]?.objectValue?["backdrop"]?.objectValue?["asset_id"], .string(poolID))

        session.setClipTilt(nil, for: target)
        XCTAssertNil(session.clipTilt(for: target))
        XCTAssertEqual(session.document.clips[0].raw["tilt"], .null, "a saved tilt resets with an explicit null")
    }

    func testAMediaBackdropAppliesToEveryClipThatCanTilt() async throws {
        let (session, _, _) = try await self.session(media: open, pool: [visual()], slots: [[:], ["look_preset": .string("golden_hour")], [:]])
        let result = session.applyTiltBackdropToAllClips(.media(poolID))
        XCTAssertEqual([result.applied, result.skipped], [2, 1])
        XCTAssertEqual(try ClipTilt(row: session.document.clips[0].raw["tilt"])?.backdrop, .media(poolID))
        XCTAssertNil(session.document.clips[1].raw["tilt"])
    }

    func testAPoolVisualThatIsGoneIsReportedOnceTheLibraryHasLoaded() async throws {
        let (empty, _, _) = try await session(media: open, pool: [])
        XCTAssertFalse(empty.tiltBackdropIsMissing(.media(poolID)), "an unloaded library says nothing")
        let (loaded, _, _) = try await session(media: open, pool: [visual("another")])
        XCTAssertTrue(loaded.tiltBackdropIsMissing(.media(poolID)))
        XCTAssertFalse(loaded.tiltBackdropIsMissing(.media("another")))
        XCTAssertFalse(loaded.tiltBackdropIsMissing(.blur))
        let (processing, _, _) = try await session(media: open, pool: [visual(status: "analyzing")])
        XCTAssertTrue(processing.tiltBackdropIsMissing(.media(poolID)), "only a ready visual can be drawn")
    }

    func testTheSessionNamesEachMediaBackdropOnceInStableOrder() {
        func slot(_ id: String, _ backdrop: ClipBackdrop) -> EditorTimelineSlot {
            .init(id: id, clipIndex: 0, inS: 0, durationS: 1, raw: ["tilt": ClipTilt(rotationDeg: 5, backdrop: backdrop).rowValue!])
        }
        var removed = slot("c", .media("zz"))
        removed.removed = true
        let document = EditorDocument(clips: [slot("a", .media("b")), slot("b", .media("a")), slot("d", .media("b")), slot("e", .blur), removed])
        XCTAssertEqual(NativeEditorSession.tiltMediaBackdropPoolIDs(in: document), ["a", "b"])
    }

    // MARK: Compiler and pixels

    private func writePNG(to url: URL, color: (Int, Int) -> [UInt8]) throws {
        var pixels = [UInt8](repeating: 255, count: width * height * 4)
        for y in 0..<height { for x in 0..<width {
            let value = color(x, y)
            for channel in 0..<3 { pixels[(y * width + x) * 4 + channel] = value[channel] }
        } }
        let provider = try XCTUnwrap(CGDataProvider(data: Data(pixels) as CFData))
        let image = try XCTUnwrap(CGImage(width: width, height: height, bitsPerComponent: 8, bitsPerPixel: 32, bytesPerRow: width * 4,
            space: CGColorSpace(name: CGColorSpace.sRGB)!, bitmapInfo: CGBitmapInfo(rawValue: CGImageAlphaInfo.noneSkipLast.rawValue),
            provider: provider, decode: nil, shouldInterpolate: false, intent: .defaultIntent))
        let destination = try XCTUnwrap(CGImageDestinationCreateWithURL(url as CFURL, "public.png" as CFString, 1, nil))
        CGImageDestinationAddImage(destination, image, nil)
        XCTAssertTrue(CGImageDestinationFinalize(destination))
    }

    private func program(in directory: URL, withPhoto: Bool) throws -> NativeEditorRenderProgram {
        let scene = directory.appendingPathComponent("scene.png"), photo = directory.appendingPathComponent("backdrop.png")
        try writePNG(to: scene) { _, _ in [128, 128, 128] }
        try writePNG(to: photo) { x, y in
            let index = (y < self.height / 2 ? 0 : 2) + (x < self.width / 2 ? 0 : 1)
            return self.quadrants[index].map { UInt8($0) }
        }
        let sceneFingerprint = try SHA256Fingerprinter().fingerprint(file: scene)
        let source = ResolvedEditorSource(clipIndex: 0, mediaID: "original",
            asset: MediaAsset(id: "local", relativePath: "scene.png", fingerprint: sceneFingerprint, duration: nil,
                              naturalSize: MediaSize(width: Double(width), height: Double(height))), url: scene)
        var mediaSources: [String: ResolvedEditorSource] = [:]
        if withPhoto {
            var pool = ResolvedEditorSource(clipIndex: -1, mediaID: poolID, asset: MediaAsset(
                id: "pool", relativePath: "backdrop.png", fingerprint: try SHA256Fingerprinter().fingerprint(file: photo)), url: photo)
            pool.visualGeneration = "7"
            pool.visualKind = .image
            mediaSources[NativeEditorRenderCompiler.tiltBackdropSourceKey(forPoolID: poolID)] = pool
        }
        let clip = EditorClip(id: UUID(), assetID: UUID(), sourceClipIndex: 0, start: 0, end: 3, trimIn: 0, trimOut: 3, sourceDuration: 3, slotID: "slot")
        let document = EditorDocument(clips: [.init(id: "slot", clipIndex: 0, inS: 0, durationS: 3, raw: ["tilt": ClipTilt(rotationDeg: 30, backdrop: .media(poolID)).rowValue!])])
        let compiler = try NativeEditorRenderCompiler(fontDirectory: XCTUnwrap(Bundle.main.url(forResource: "fonts", withExtension: nil)))
        return try compiler.compile(document: document, clips: [clip], items: [], sources: [0: source], mediaSources: mediaSources)
    }

    private func directory() throws -> URL {
        let url = FileManager.default.temporaryDirectory.appendingPathComponent(UUID().uuidString)
        try FileManager.default.createDirectory(at: url, withIntermediateDirectories: true)
        addTeardownBlock { try? FileManager.default.removeItem(at: url) }
        return url
    }

    func testTheCompilerDeclaresTheNamedPoolVisualAsARecipeAsset() throws {
        let program = try program(in: directory(), withPhoto: true)
        let assetID = "visual-" + poolID
        XCTAssertEqual(program.recipe.tracks[0].clips[0].backdrop, .media(poolID))
        XCTAssertEqual(program.recipe.assetManifest?.assets.first { $0.id == assetID }?.source,
                       .visual(visualID: poolID, generation: "7", mediaKind: .image))
        XCTAssertNotNil(program.recipe.assets.first { $0.id == assetID })
        XCTAssertNotNil(program.assetURLs[assetID])
        XCTAssertTrue(program.recipe.effectiveCapabilities.isSuperset(of: [.clipTilt, .clipBackdropMedia, .stillImages]))
        XCTAssertNoThrow(try program.recipe.validate())
    }

    func testAPhotoTheSessionCouldNotReadDropsOutOfThePreviewInsteadOfFailingIt() throws {
        let program = try program(in: directory(), withPhoto: false)
        XCTAssertNil(program.recipe.tracks[0].clips[0].backdrop)
        XCTAssertEqual(program.recipe.tracks[0].clips[0].transform.rotationDegrees, 30, "the tilt itself stays")
        XCTAssertFalse(program.recipe.effectiveCapabilities.contains(.clipBackdropMedia))
        XCTAssertNoThrow(try program.recipe.validate())
    }

    func testTheEditorsRecipeRendersThePhotoInTheCornersOfATiltedClip() async throws {
        let program = try program(in: directory(), withPhoto: true)
        let preview = try await AVPlayerPreviewComposer().makePreview(recipe: program.recipe, assetURLs: program.assetURLs)
        let item = preview.playerItem
        let reader = try AVAssetReader(asset: item.asset)
        let tracks = try await item.asset.loadTracks(withMediaType: .video)
        let output = AVAssetReaderVideoCompositionOutput(videoTracks: tracks, videoSettings: [kCVPixelBufferPixelFormatTypeKey as String: kCVPixelFormatType_32BGRA])
        output.videoComposition = item.videoComposition
        reader.add(output)
        reader.timeRange = CMTimeRange(start: CMTime(seconds: 0.5, preferredTimescale: 600), duration: CMTime(seconds: 0.2, preferredTimescale: 600))
        XCTAssertTrue(reader.startReading(), "\(String(describing: reader.error))")
        let sample = try XCTUnwrap(output.copyNextSampleBuffer())
        let buffer = try XCTUnwrap(CMSampleBufferGetImageBuffer(sample))
        CVPixelBufferLockBaseAddress(buffer, .readOnly); defer { CVPixelBufferUnlockBaseAddress(buffer, .readOnly) }
        let (w, h, stride) = (CVPixelBufferGetWidth(buffer), CVPixelBufferGetHeight(buffer), CVPixelBufferGetBytesPerRow(buffer))
        let base = try XCTUnwrap(CVPixelBufferGetBaseAddress(buffer)).assumingMemoryBound(to: UInt8.self)
        func pixel(_ x: Int, _ y: Int) -> [Int] {
            let offset = y * stride + x * 4
            return [Int(base[offset + 2]), Int(base[offset + 1]), Int(base[offset])]
        }
        let corners = [pixel(6, 6), pixel(w - 7, 6), pixel(6, h - 7), pixel(w - 7, h - 7)]
        for (index, corner) in corners.enumerated() {
            let delta = zip(corner, quadrants[index]).map { abs($0 - $1) }.max() ?? 0
            XCTAssertLessThanOrEqual(delta, 6, "corner \(index): \(corner) vs \(quadrants[index])")
        }
        let centre = pixel(w / 2, h / 2)
        XCTAssertLessThanOrEqual(zip(centre, [128, 128, 128]).map { abs($0 - $1) }.max() ?? 0, 6, "the clip stays in front: \(centre)")
    }
}
