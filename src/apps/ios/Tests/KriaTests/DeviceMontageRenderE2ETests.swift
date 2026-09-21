import Foundation
import XCTest
import AVFoundation
import CoreMedia
import CoreGraphics
import KriaMediaEngine
@testable import Kria

private final class RequestLog: @unchecked Sendable { var urls: [String] = [] }

/// KRI-132 end-to-end proof, opt-in: renders recipes compiled by the server's
/// SECOND phone compiler -- `app.pipeline.phone_montage_plan
/// .compile_phone_montage_plan`, for the montage/day_vlog/single_hero
/// generative-edit archetypes -- through the production device resolver and
/// exporter on the simulator. Sibling of `DevicePhotoRenderE2ETests`
/// (KRI-121), which only ever exercises the guided compiler
/// (`compile_phone_guided_plan`) and never carries text layers or a music
/// track, so this file is the first device E2E coverage for the montage
/// compiler, for a real agent-text intro (fonts + `positionedText`/
/// `animatedText`), and for a licensed music bed (`.library` asset
/// resolution) on this harness.
///
/// `scripts/ios/phone-montage-render-e2e.py` writes `KRIA_E2E_DIR` with
/// three cases, each isolating one compiler branch:
///   - `cuts_text`  -- plain cuts, preserved original audio, an agent-text
///                     intro (basicComposition/positionedText/animatedText).
///   - `music`      -- a licensed music bed replaces the original audio, no
///                     text/transition (basicComposition/audioMix/musicBed).
///   - `crossfade`  -- a crossfade transition, no text/music
///                     (basicComposition/crossfade).
///
/// `day_vlog`/`single_hero` are deliberately NOT separate cases: read
/// `app/pipeline/phone_montage_plan.py` in full -- `compile_phone_montage_plan`
/// never references `resolved_archetype` or `edit_format` anywhere. Archetype
/// only steers what the upstream matcher/decision phase selects as
/// `assembly_steps` BEFORE this compiler ever sees them; the compiler itself
/// treats every montage-family archetype identically, so a format-specific
/// phone case would just be a relabeled `cuts_text`/`crossfade` case, not a
/// new branch. See the script's module docstring for the full writeup.
@MainActor final class DeviceMontageRenderE2ETests: XCTestCase {
    private typealias RGB = [Int]
    override func tearDown() { NativeEditorURLProtocol.handler = nil; super.tearDown() }

    func testPlainCutsWithOriginalAudioAndTextIntroRendersOnTheIPhone() async throws {
        try await assertCase("cuts_text")
    }

    func testLicensedMusicBedReplacesOriginalAudioOnTheIPhone() async throws {
        // KRI-132 finding: the verified library cache stores music under an
        // extensionless content address, and AVFoundation refuses it
        // (-11828 "Cannot Open"). Visuals videos get a playable name from
        // `VisualVideoFile.prepare`; library audio has no equivalent yet.
        // Strict, so the fix has to delete this expectation.
        XCTExpectFailure("library audio has no playable file extension", strict: true)
        try await assertCase("music")
    }

    func testCrossfadeTransitionRendersOnTheIPhone() async throws {
        try await assertCase("crossfade")
    }

    // MARK: -

    private func assertCase(_ caseID: String) async throws {
        let input = try inputDirectory()
        let meta = try XCTUnwrap(
            JSONSerialization.jsonObject(with: Data(contentsOf: input.appendingPathComponent("e2e.json"))) as? [String: Any]
        )
        let verified = try XCTUnwrap(meta["verified_features"] as? [String])
        let cases = try XCTUnwrap(meta["cases"] as? [String: Any])
        let caseMeta = try XCTUnwrap(cases[caseID] as? [String: Any])

        let statusFile = try XCTUnwrap(caseMeta["status_file"] as? String)
        let status = try JSONDecoder().decode(
            DeviceRenderStatusResponse.self, from: Data(contentsOf: input.appendingPathComponent(statusFile))
        )
        let recipe = status.request.recipe

        // Route decision: local while every capability this case declares is
        // verified; dropping the case's own signature capability must fall
        // back to cloud, exactly mirroring `DevicePhotoRenderE2ETests`'s
        // stillImages/visualVideos pattern but for the montage compiler's own
        // capability set (positionedText / musicBed / crossfade).
        func route(_ features: [String]) -> ExportRoute {
            DeviceRenderSessions.decision(
                recipe, capabilities: PhoneRenderingCapabilities(enabled: true, recipeVersions: [2], verifiedFeatures: features)
            ).route
        }
        let requiredCapabilities = try XCTUnwrap(caseMeta["required_capabilities"] as? [String])
        XCTAssertEqual(Set(recipe.requiredCapabilities.map(\.rawValue)), Set(requiredCapabilities), caseID)
        XCTAssertEqual(route(verified), .local, caseID)
        let dropCapability = try XCTUnwrap(caseMeta["drop_capability"] as? String)
        XCTAssertEqual(route(verified.filter { $0 != dropCapability }), .cloud, "\(caseID): dropping \(dropCapability)")

        // Every montage clip is a device original bound at upload time --
        // unlike the guided compiler, this compiler never emits a `.visual`
        // (Visuals-pool) asset.
        let project = ProjectDirectory(root: FileManager.default.temporaryDirectory.appendingPathComponent(UUID().uuidString))
        defer { try? FileManager.default.removeItem(at: project.root) }
        try project.createIfNeeded()
        let store = SourceAssetStore(project: project)
        let clips = try XCTUnwrap(caseMeta["clips"] as? [[String: Any]])
        for clip in clips {
            let mediaID = try XCTUnwrap(clip["media_id"] as? String)
            let file = try XCTUnwrap(clip["file"] as? String)
            let destination = project.originals.appendingPathComponent(file)
            try FileManager.default.copyItem(at: input.appendingPathComponent(file), to: destination)
            try store.bind(
                mediaID: mediaID,
                original: MediaAsset(
                    id: mediaID, relativePath: "originals/\(file)",
                    fingerprint: try SHA256Fingerprinter().fingerprint(file: destination)
                )
            )
        }

        // The only non-original assets a montage recipe can carry are a
        // licensed music bed (`.library`, catalog "music") and, for the text
        // case, a bundled font (`.library`, catalog "font" -- installed from
        // the app bundle, no network grant; see `AuthorizedDeviceSourceResolver
        // .resolve`'s font branch). `AuthorizedDeviceSourceResolver` treats
        // `.library` exactly like `.visual`: one per-asset grant, one
        // download, one verified-cache install (KRI-121's seam, reused
        // as-is) -- so a licensed track needs no new resolver support, only
        // its bytes behind the same mocked grant endpoint Visuals already use.
        var bytes: [String: Data] = [:]
        if let musicAssetID = caseMeta["music_asset_id"] as? String, let musicFile = caseMeta["music_file"] as? String {
            bytes[musicAssetID] = try Data(contentsOf: input.appendingPathComponent(musicFile))
        }
        let log = RequestLog()
        NativeEditorURLProtocol.handler = { request in
            log.urls.append(request.url?.absoluteString ?? "")
            if request.url?.host == "storage.e2e.test" { return (200, bytes[request.url?.lastPathComponent ?? ""] ?? Data()) }
            let body = try JSONSerialization.jsonObject(with: NativeEditorTestSupport.bodyData(request)) as? [String: Any]
            let assetID = body?["asset_id"] as? String ?? ""
            return (200, Data(#"{"asset_id":"\#(assetID)","download_url":"https://storage.e2e.test/\#(assetID)","expires_at":"2099-01-01T00:00:00Z"}"#.utf8))
        }
        let library = RenderLibraryCache(root: project.root.appending(path: "library", directoryHint: .isDirectory))
        let resolver = AuthorizedDeviceSourceResolver(
            api: NativeEditorTestSupport.api(), request: status.request, originals: store,
            library: library, downloadSession: NativeEditorTestSupport.session()
        )
        let urls = try await resolver.resolve(for: recipe)
        if let musicAssetID = caseMeta["music_asset_id"] as? String {
            XCTAssertTrue(
                log.urls.contains { $0 == "https://storage.e2e.test/\(musicAssetID)" },
                "the music bed must download through the same per-asset grant Visuals use"
            )
        }

        let frames = input.appendingPathComponent("frames", isDirectory: true)
        try FileManager.default.createDirectory(at: frames, withIntermediateDirectories: true)
        let movie = frames.appendingPathComponent("\(caseID).mp4")
        try? FileManager.default.removeItem(at: movie)
        let checkpoint = try await AVFoundationLocalExporter(
            stateStore: FileExportStateStore(directory: project.root.appendingPathComponent("state"))
        ).export(recipe: recipe, assetURLs: urls, outputURL: movie)
        XCTAssertEqual(checkpoint.status, .completed, caseID)

        let asset = AVURLAsset(url: movie)
        let duration = try await asset.load(.duration).seconds
        let expectedDuration = try XCTUnwrap(caseMeta["duration_s"] as? Double)
        XCTAssertEqual(duration, expectedDuration, accuracy: 0.1, caseID)

        let audioTracks = try await asset.loadTracks(withMediaType: .audio)
        XCTAssertFalse(audioTracks.isEmpty, "\(caseID): every montage export always writes an AAC track")
        if caseMeta["expects_music_audio"] as? Bool == true {
            let peak = try await peakAmplitude(of: asset)
            XCTAssertGreaterThan(peak, 0.01, "\(caseID): the licensed music bed must be audible")
        }

        let generator = AVAssetImageGenerator(asset: asset)
        generator.requestedTimeToleranceBefore = .zero; generator.requestedTimeToleranceAfter = .zero
        let samples = try XCTUnwrap(caseMeta["samples"] as? [[String: Any]])
        for sample in samples {
            let name = try XCTUnwrap(sample["name"] as? String)
            let t = try XCTUnwrap(sample["t"] as? Double)
            let x = try XCTUnwrap(sample["x"] as? Int)
            let y = try XCTUnwrap(sample["y"] as? Int)
            let expected = try XCTUnwrap(sample["rgb"] as? [Int])
            let image = try await generator.image(at: CMTime(seconds: t, preferredTimescale: 600)).image
            XCTAssertEqual([image.width, image.height], [recipe.canvas.width, recipe.canvas.height], "\(caseID)/\(name)")
            let destination = try XCTUnwrap(
                CGImageDestinationCreateWithURL(frames.appendingPathComponent("\(caseID)-\(name).png") as CFURL, "public.png" as CFString, 1, nil)
            )
            CGImageDestinationAddImage(destination, image, nil)
            XCTAssertTrue(CGImageDestinationFinalize(destination))
            let observed = pixel(image, x: x, y: y)
            XCTAssertTrue(isColor(observed, expected), "\(caseID)/\(name) at \(t)s: \(observed) != \(expected)")
        }
        if let blend = caseMeta["blend_sample"] as? [String: Any] {
            let t = try XCTUnwrap(blend["t"] as? Double)
            let x = try XCTUnwrap(blend["x"] as? Int)
            let y = try XCTUnwrap(blend["y"] as? Int)
            let image = try await generator.image(at: CMTime(seconds: t, preferredTimescale: 600)).image
            let value = pixel(image, x: x, y: y)
            // The two crossfade clips are solid blue [0,0,255] and yellow
            // [255,255,0]; halfway through the overlap both channel families
            // must show through the blend, matching
            // `DevicePhotoRenderE2ETests`'s own crossfade-blend assertion style.
            XCTAssertTrue(value[2] > 40 && (value[0] > 40 || value[1] > 40), "\(caseID) crossfade blend: \(value)")
        }
    }

    private func inputDirectory() throws -> URL {
        guard let path = ProcessInfo.processInfo.environment["KRIA_E2E_DIR"] else { throw XCTSkip("Set KRIA_E2E_DIR to run") }
        return URL(fileURLWithPath: path)
    }

    /// Peak absolute sample amplitude (normalized 0...1) across the asset's
    /// first audio track, read directly via `AVAssetReader` -- no playback,
    /// no dependency on device volume/mute state.
    private func peakAmplitude(of asset: AVAsset) async throws -> Float {
        guard let track = try await asset.loadTracks(withMediaType: .audio).first else { return 0 }
        let reader = try AVAssetReader(asset: asset)
        let outputSettings: [String: Any] = [
            AVFormatIDKey: kAudioFormatLinearPCM,
            AVLinearPCMIsFloatKey: true,
            AVLinearPCMBitDepthKey: 32,
            AVLinearPCMIsNonInterleaved: false,
        ]
        let output = AVAssetReaderTrackOutput(track: track, outputSettings: outputSettings)
        reader.add(output)
        _ = reader.startReading()
        var peak: Float = 0
        while let buffer = output.copyNextSampleBuffer() {
            guard let blockBuffer = CMSampleBufferGetDataBuffer(buffer) else { continue }
            let length = CMBlockBufferGetDataLength(blockBuffer)
            var data = [UInt8](repeating: 0, count: length)
            _ = CMBlockBufferCopyDataBytes(blockBuffer, atOffset: 0, dataLength: length, destination: &data)
            data.withUnsafeBytes { raw in
                for value in raw.bindMemory(to: Float32.self) { peak = max(peak, abs(value)) }
            }
        }
        return peak
    }

    /// H.264/AAC round saturated colors by a few levels.
    private func isColor(_ pixel: RGB, _ expected: RGB) -> Bool { zip(pixel, expected).allSatisfy { abs($0 - $1) <= 40 } }

    /// `x`, `y` from the top-left, as the cloud renderer measures.
    private func pixel(_ image: CGImage, x: Int, y: Int) -> RGB {
        var rgba = [UInt8](repeating: 0, count: 4)
        let context = CGContext(data: &rgba, width: 1, height: 1, bitsPerComponent: 8, bytesPerRow: 4,
                                space: CGColorSpace(name: CGColorSpace.sRGB)!, bitmapInfo: CGImageAlphaInfo.premultipliedLast.rawValue)!
        context.draw(image, in: CGRect(x: -x, y: -(image.height - 1 - y), width: image.width, height: image.height))
        return rgba.prefix(3).map(Int.init)
    }
}
