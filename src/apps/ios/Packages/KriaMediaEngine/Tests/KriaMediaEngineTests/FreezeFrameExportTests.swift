#if canImport(AVFoundation)
import XCTest
@preconcurrency import AVFoundation
@testable import KriaMediaEngine

/// KRI-828: a creator's freeze frame on a phone montage moment is a main-track `holdDuration`
/// longer than one frame. The engine plays the clip's source window at 1x, then holds its LAST
/// frame for the hold, and the next clip cuts in on time.
///
/// Ways this can fail, each pinned below:
/// 1. The hold needs `editorMedia` (an overlay feature) instead of `freezeFrame`, so a device
///    that verified freezes still sends it to the cloud, or one that verified editor media
///    renders a freeze nobody verified.
/// 2. Wrong frame held (the window's first frame, or black): the held picture must be the
///    source frame at the end of the window.
/// 3. Fake slow motion: the picture keeps changing during the hold.
/// 4. The next clip shifts by the hold (late cut) or the export runs short/long.
@MainActor final class FreezeFrameExportTests: XCTestCase {
    private var directory: URL!

    override func setUp() async throws {
        directory = FileManager.default.temporaryDirectory.appendingPathComponent(UUID().uuidString)
        try FileManager.default.createDirectory(at: directory, withIntermediateDirectories: true)
    }

    override func tearDown() async throws { try? FileManager.default.removeItem(at: directory) }

    /// Red rises 2 levels per source frame: a frame's red says which source frame it is.
    private func ramp() async throws -> URL {
        try await SolidVideoFixture.make(directory: directory, name: "ramp", color: CGColor(red: 0, green: 0, blue: 0, alpha: 1),
                                         seconds: 3, frameColor: { CGColor(red: CGFloat(min(255, $0 * 2)) / 255, green: 0, blue: 0, alpha: 1) })
    }

    private func recipe(fingerprint: AssetFingerprint, clips: [TimelineClip]) throws -> EditRecipe {
        let manifest = try RenderAssetManifest(assets: [RenderAssetReference(id: "source", fingerprint: RenderFingerprint(fingerprint), source: .original(mediaID: "source"))])
        return EditRecipe(schemaVersion: 2, rendererVersion: "kria-ios-2", canvas: Canvas(width: 96, height: 160),
            assets: [MediaAsset(id: "source", relativePath: "source", fingerprint: fingerprint)],
            tracks: [TimelineTrack(id: "video", kind: .video, clips: clips)], assetManifest: manifest)
    }

    private func red(_ asset: AVAsset, at seconds: Double) async throws -> Int {
        let generator = AVAssetImageGenerator(asset: asset)
        generator.requestedTimeToleranceBefore = .zero
        generator.requestedTimeToleranceAfter = .zero
        let image = try await generator.image(at: CMTime(seconds: seconds, preferredTimescale: 600)).image
        var pixel = [UInt8](repeating: 0, count: 4)
        pixel.withUnsafeMutableBytes { bytes in
            let context = CGContext(data: bytes.baseAddress, width: 1, height: 1, bitsPerComponent: 8, bytesPerRow: 4,
                                    space: CGColorSpace(name: CGColorSpace.sRGB)!, bitmapInfo: CGImageAlphaInfo.premultipliedLast.rawValue)!
            context.draw(image, in: CGRect(x: 0, y: 0, width: 1, height: 1))
        }
        return Int(pixel[0])
    }

    func testMainTrackHoldIsAFreezeFrameNotEditorMedia() throws {
        var recipe = EditRecipe(schemaVersion: 2, rendererVersion: "kria-ios-2", assets: [MediaAsset(id: "a", relativePath: "a.mp4")],
            tracks: [TimelineTrack(id: "v", kind: .video, clips: [TimelineClip(id: "c", sourceAssetID: "a", sourceDuration: 2, holdDuration: 1)])])
        XCTAssertTrue(recipe.effectiveCapabilities.contains(.freezeFrame))
        XCTAssertFalse(recipe.effectiveCapabilities.contains(.editorMedia))
        recipe.tracks.append(TimelineTrack(id: "o", kind: .overlay, clips: [TimelineClip(id: "o1", sourceAssetID: "a", sourceDuration: 1, volume: 0, holdDuration: 1)]))
        XCTAssertTrue(recipe.effectiveCapabilities.contains(.editorMedia), "an overlay hold stays editor media")
        let freezeOnly = CapabilityNegotiator(provider: DefaultRendererCapabilities(capabilities: [.basicComposition, .local1080Export, .freezeFrame]))
        XCTAssertEqual(freezeOnly.decide(for: EditRecipe(schemaVersion: 2, rendererVersion: "kria-ios-2", assets: [MediaAsset(id: "a", relativePath: "a.mp4")],
            tracks: [TimelineTrack(id: "v", kind: .video, clips: [TimelineClip(id: "c", sourceAssetID: "a", sourceDuration: 2, holdDuration: 1)])])).missingCapabilities, [])
    }

    func testExportHoldsTheWindowsLastFrameThenCutsOnTime() async throws {
        let source = try await ramp()
        let fingerprint = try SHA256Fingerprinter().fingerprint(file: source)
        let recipe = try recipe(fingerprint: fingerprint, clips: [
            TimelineClip(id: "held", sourceAssetID: "source", sourceStart: 0.5, sourceDuration: 1.0, holdDuration: 1.0),
            TimelineClip(id: "next", sourceAssetID: "source", sourceStart: 2.0, sourceDuration: 0.5, timelineStart: 2.0),
        ])
        XCTAssertNoThrow(try recipe.validate())
        let output = directory.appendingPathComponent("freeze.mp4")
        let checkpoint = try await AVFoundationLocalExporter(stateStore: FileExportStateStore(directory: directory.appendingPathComponent("state")), branding: .none)
            .export(recipe: recipe, assetURLs: ["source": source], outputURL: output)
        XCTAssertEqual(checkpoint.status, .completed)
        let asset = AVURLAsset(url: output)
        let duration = try await asset.load(.duration).seconds
        XCTAssertEqual(duration, 2.5, accuracy: 1.0 / 30 + 0.002)
        // Every output picture is compared with the picture at the source time it should show,
        // so the check is about WHICH frame.
        // The reference is the same source exported plainly through the same engine (same
        // colour path), so a value names a source frame.
        let plain = try self.recipe(fingerprint: fingerprint, clips: [TimelineClip(id: "plain", sourceAssetID: "source", sourceDuration: 2.9)])
        let referenceURL = directory.appendingPathComponent("reference.mp4")
        _ = try await AVFoundationLocalExporter(stateStore: FileExportStateStore(directory: directory.appendingPathComponent("state-ref")), branding: .none)
            .export(recipe: plain, assetURLs: ["source": source], outputURL: referenceURL)
        let original = AVURLAsset(url: referenceURL)
        let src = (early: try await red(original, at: 0.7), later: try await red(original, at: 1.2),
                   last: try await red(original, at: 1.5 - 1.0 / 30), first: try await red(original, at: 0.5),
                   next: try await red(original, at: 2.1))
        // Motion at 1x: output 0.2 s / 0.7 s show source 0.7 s / 1.2 s (slow motion would not).
        let early = try await red(asset, at: 0.2), later = try await red(asset, at: 0.7)
        XCTAssertEqual(Double(early), Double(src.early), accuracy: 3)
        XCTAssertEqual(Double(later), Double(src.later), accuracy: 3, "the window must play at normal speed")
        // The hold is one unchanging picture: the source frame at the window's end (1.5 s - 1 frame).
        let frozen = [try await red(asset, at: 1.1), try await red(asset, at: 1.5), try await red(asset, at: 1.95)]
        XCTAssertLessThanOrEqual((frozen.max() ?? 0) - (frozen.min() ?? 0), 2, "\(frozen)")
        XCTAssertEqual(Double(frozen[0]), Double(src.last), accuracy: 3, "held frame \(frozen) is not the window's last source frame")
        XCTAssertGreaterThan(abs(frozen[0] - src.first), 20, "the hold must not be the window's first frame")
        // The next clip cuts in at 2.0 s on its own source time (output 2.1 s = source 2.1 s).
        let next = try await red(asset, at: 2.1)
        XCTAssertEqual(Double(next), Double(src.next), accuracy: 3)
    }
}
#endif
