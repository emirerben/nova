import Foundation
import XCTest
import AVFoundation
import CoreGraphics
import KriaMediaEngine
@testable import Kria

private final class RequestLog: @unchecked Sendable { var urls: [String] = [] }

/// KRI-121 end-to-end proof, opt-in: renders recipes compiled by the server's
/// `compile_phone_guided_plan` through the production device resolver and
/// exporter on the simulator. `scripts/ios/phone-photo-render-e2e.py` writes
/// `KRIA_E2E_DIR`: footage crossfading into a Visuals photo, the same photo as
/// a supporting card, a Visuals video and a transparent cutout; plus a project
/// made only of Visuals.
@MainActor final class DevicePhotoRenderE2ETests: XCTestCase {
    private typealias RGB = [Int]
    override func tearDown() { NativeEditorURLProtocol.handler = nil; super.tearDown() }

    func testServerCompiledVisualsRenderOnTheIPhone() async throws {
        let input = try inputDirectory()
        let frames = try await render(status: "status.json", input: input, bindsFootage: true, output: "device-render",
                                      at: ["video": 1.0, "crossfade": 2.85, "photo": 4.0, "card": 5.7, "pool-video": 7.7, "cutout": 9.7])
        func sample(_ name: String, _ x: Int, _ y: Int) throws -> RGB { pixel(try XCTUnwrap(frames[name]), x: x, y: y) }
        // Footage: solid blue. Photo: cover-cropped, left half red, right half green.
        XCTAssertTrue(isColor(try sample("video", 270, 960), [0, 0, 255]))
        XCTAssertTrue(isColor(try sample("photo", 270, 960), [255, 0, 0]))
        XCTAssertTrue(isColor(try sample("photo", 810, 960), [0, 255, 0]))
        let blend = try sample("crossfade", 270, 960)
        XCTAssertTrue(blend[0] > 40 && blend[2] > 40, "crossfade mixes blue into red: \(blend)")
        // Card: the whole 4:3 photo (885x664) centered in a black 885x1382 card at
        // (96, 268), over a blurred cover of the same photo.
        XCTAssertTrue(isColor(try sample("card", 96 + 221, 960), [255, 0, 0]))
        XCTAssertTrue(isColor(try sample("card", 96 + 664, 960), [0, 255, 0]))
        XCTAssertTrue(isColor(try sample("card", 540, 268 + 120), [0, 0, 0]), "bars inside the card are black")
        XCTAssertTrue(isColor(try sample("card", 540, 1650 - 120), [0, 0, 0]))
        let blurLeft = try sample("card", 40, 120), blurRight = try sample("card", 1040, 120)
        XCTAssertTrue(blurLeft[0] > 180 && blurLeft[1] < 80, "blurred cover outside the card: \(blurLeft)")
        XCTAssertTrue(blurRight[1] > 180 && blurRight[0] < 80, "blurred cover outside the card: \(blurRight)")
        // Visuals video: solid yellow. Cutout: transparent center mattes to black
        // (never the white stored under it); opaque magenta edges are untouched.
        XCTAssertTrue(isColor(try sample("pool-video", 540, 960), [255, 255, 0]))
        XCTAssertTrue(isColor(try sample("cutout", 540, 960), [0, 0, 0]))
        XCTAssertTrue(isColor(try sample("cutout", 100, 100), [255, 0, 255]))
    }

    func testProjectMadeOnlyOfVisualsRendersOnTheIPhone() async throws {
        let input = try inputDirectory()
        let frames = try await render(status: "status-visuals-only.json", input: input, bindsFootage: false, output: "visuals-only",
                                      at: ["only-photo": 1.0, "only-pool-video": 3.0])
        XCTAssertTrue(isColor(pixel(try XCTUnwrap(frames["only-photo"]), x: 270, y: 960), [255, 0, 0]))
        XCTAssertTrue(isColor(pixel(try XCTUnwrap(frames["only-pool-video"]), x: 540, y: 960), [255, 255, 0]))
    }

    private func inputDirectory() throws -> URL {
        guard let path = ProcessInfo.processInfo.environment["KRIA_E2E_DIR"] else { throw XCTSkip("Set KRIA_E2E_DIR to run") }
        return URL(fileURLWithPath: path)
    }

    private func render(status name: String, input: URL, bindsFootage: Bool, output: String, at times: [String: Double]) async throws -> [String: CGImage] {
        let status = try JSONDecoder().decode(DeviceRenderStatusResponse.self, from: Data(contentsOf: input.appendingPathComponent(name)))
        let meta = try XCTUnwrap(JSONSerialization.jsonObject(with: Data(contentsOf: input.appendingPathComponent("e2e.json"))) as? [String: Any])
        let recipe = status.request.recipe

        // The iPhone takes the local route only while every Visuals kind is verified.
        let verified = try XCTUnwrap(meta["verified_features"] as? [String])
        func route(_ features: [String]) -> ExportRoute {
            DeviceRenderSessions.decision(recipe, capabilities: PhoneRenderingCapabilities(enabled: true, recipeVersions: [2], verifiedFeatures: features)).route
        }
        XCTAssertTrue(recipe.effectiveCapabilities.isSuperset(of: [.stillImages, .visualVideos]))
        XCTAssertEqual(route(verified), .local)
        XCTAssertEqual(route(verified.filter { $0 != "stillImages" }), .cloud)
        XCTAssertEqual(route(verified.filter { $0 != "visualVideos" }), .cloud)

        // Footage is a device original bound at upload time; Visuals never are.
        let project = ProjectDirectory(root: FileManager.default.temporaryDirectory.appendingPathComponent(UUID().uuidString))
        defer { try? FileManager.default.removeItem(at: project.root) }
        try project.createIfNeeded()
        let store = SourceAssetStore(project: project)
        if bindsFootage {
            let original = project.originals.appendingPathComponent("video.mp4")
            try FileManager.default.copyItem(at: input.appendingPathComponent("video.mp4"), to: original)
            let videoID = try XCTUnwrap(meta["video_media_id"] as? String)
            try store.bind(mediaID: videoID, original: MediaAsset(id: videoID, relativePath: "originals/video.mp4",
                                                                     fingerprint: try SHA256Fingerprinter().fingerprint(file: original)))
        }

        // The API grants each pinned visual; storage serves its bytes.
        let files = try XCTUnwrap(meta["visual_files"] as? [String: String])
        let visualIDs = (recipe.assetManifest?.assets ?? []).compactMap { asset -> String? in if case .visual = asset.source { asset.id } else { nil } }
        let bytes = try Dictionary(uniqueKeysWithValues: visualIDs.map { ($0, try Data(contentsOf: input.appendingPathComponent(try XCTUnwrap(files[$0])))) })
        let log = RequestLog()
        NativeEditorURLProtocol.handler = { request in
            log.urls.append(request.url?.absoluteString ?? "")
            if request.url?.host == "storage.e2e.test" { return (200, bytes[request.url?.lastPathComponent ?? ""] ?? Data()) }
            let body = try JSONSerialization.jsonObject(with: NativeEditorTestSupport.bodyData(request)) as? [String: Any]
            let assetID = body?["asset_id"] as? String ?? ""
            return (200, Data(#"{"asset_id":"\#(assetID)","download_url":"https://storage.e2e.test/\#(assetID)","expires_at":"2099-01-01T00:00:00Z"}"#.utf8))
        }
        func resolver() -> AuthorizedDeviceSourceResolver {
            AuthorizedDeviceSourceResolver(api: NativeEditorTestSupport.api(), request: status.request, originals: store,
                library: RenderLibraryCache(root: project.root.appending(path: "library", directoryHint: .isDirectory)),
                downloadSession: NativeEditorTestSupport.session())
        }
        let urls = try await resolver().resolve(for: recipe)
        XCTAssertEqual(Set(log.urls.filter { $0.contains("storage.e2e.test") }), Set(visualIDs.map { "https://storage.e2e.test/\($0)" }))
        // A second resolve renders from the verified cache: no grants, no downloads,
        // and videos still get a playable extension.
        log.urls = []
        let cached = try await resolver().resolve(for: recipe)
        XCTAssertEqual(cached, urls)
        XCTAssertEqual(log.urls, [])
        for asset in recipe.assetManifest?.assets ?? [] {
            guard case .visual(_, _, let kind) = asset.source else { continue }
            let url = try XCTUnwrap(urls[asset.id])
            if kind == .video { XCTAssertTrue(url.path.contains("visual-videos") && url.pathExtension == "mp4", url.path) }
        }

        let frames = input.appendingPathComponent("frames", isDirectory: true)
        try FileManager.default.createDirectory(at: frames, withIntermediateDirectories: true)
        let movie = frames.appendingPathComponent("\(output).mp4")
        try? FileManager.default.removeItem(at: movie)
        let checkpoint = try await AVFoundationLocalExporter(stateStore: FileExportStateStore(directory: project.root.appendingPathComponent("state")))
            .export(recipe: recipe, assetURLs: urls, outputURL: movie)
        XCTAssertEqual(checkpoint.status, .completed)

        let asset = AVURLAsset(url: movie)
        let duration = try await asset.load(.duration).seconds
        XCTAssertEqual(duration, TimelineMath.totalDuration(of: recipe), accuracy: 0.1)
        if bindsFootage {
            let audio = try await asset.loadTracks(withMediaType: .audio)
            XCTAssertFalse(audio.isEmpty, "source audio survives next to stills")
        }
        let generator = AVAssetImageGenerator(asset: asset)
        generator.requestedTimeToleranceBefore = .zero; generator.requestedTimeToleranceAfter = .zero
        var images: [String: CGImage] = [:]
        for (name, time) in times {
            let image = try await generator.image(at: CMTime(seconds: time, preferredTimescale: 600)).image
            XCTAssertEqual([image.width, image.height], [recipe.canvas.width, recipe.canvas.height])
            let destination = try XCTUnwrap(CGImageDestinationCreateWithURL(frames.appendingPathComponent("\(name).png") as CFURL, "public.png" as CFString, 1, nil))
            CGImageDestinationAddImage(destination, image, nil)
            XCTAssertTrue(CGImageDestinationFinalize(destination))
            images[name] = image
        }
        return images
    }

    /// H.264 rounds saturated colors by a few levels.
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
