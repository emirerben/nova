import Foundation
import XCTest
import AVFoundation
import CoreGraphics
import KriaMediaEngine
@testable import Kria

private final class RequestLog: @unchecked Sendable { var urls: [String] = [] }

/// KRI-121 end-to-end proof, opt-in: renders a recipe compiled by the server's
/// `compile_phone_guided_plan` (video, crossfade, Visuals photo) through the
/// production device resolver and exporter on the simulator.
/// `KRIA_E2E_DIR` holds status.json, e2e.json, video.mp4 and photo.jpg.
@MainActor final class DevicePhotoRenderE2ETests: XCTestCase {
    func testServerCompiledPhotoRecipeRendersOnTheIPhone() async throws {
        guard let path = ProcessInfo.processInfo.environment["KRIA_E2E_DIR"] else { throw XCTSkip("Set KRIA_E2E_DIR to run") }
        let input = URL(fileURLWithPath: path)
        let status = try JSONDecoder().decode(DeviceRenderStatusResponse.self, from: Data(contentsOf: input.appendingPathComponent("status.json")))
        let meta = try XCTUnwrap(JSONSerialization.jsonObject(with: Data(contentsOf: input.appendingPathComponent("e2e.json"))) as? [String: Any])
        let recipe = status.request.recipe

        // The iPhone takes the local route only while stillImages is verified.
        let verified = try XCTUnwrap(meta["verified_features"] as? [String])
        let capabilities = PhoneRenderingCapabilities(enabled: true, recipeVersions: [2], verifiedFeatures: verified)
        XCTAssertTrue(recipe.effectiveCapabilities.contains(.stillImages))
        XCTAssertEqual(DeviceRenderSessions.decision(recipe, capabilities: capabilities).route, .local)
        let withoutStills = PhoneRenderingCapabilities(enabled: true, recipeVersions: [2], verifiedFeatures: verified.filter { $0 != "stillImages" })
        XCTAssertEqual(DeviceRenderSessions.decision(recipe, capabilities: withoutStills).route, .cloud)

        // The video is a device original bound at upload time; the photo is not.
        let project = ProjectDirectory(root: FileManager.default.temporaryDirectory.appendingPathComponent(UUID().uuidString))
        defer { try? FileManager.default.removeItem(at: project.root) }
        try project.createIfNeeded()
        let original = project.originals.appendingPathComponent("video.mp4")
        try FileManager.default.copyItem(at: input.appendingPathComponent("video.mp4"), to: original)
        let videoID = try XCTUnwrap(meta["video_media_id"] as? String)
        let store = SourceAssetStore(project: project)
        try store.bind(mediaID: videoID, original: MediaAsset(id: videoID, relativePath: "originals/video.mp4",
                                                                 fingerprint: try SHA256Fingerprinter().fingerprint(file: original)))

        // The API grants the pinned photo; storage serves its bytes.
        let photoBytes = try Data(contentsOf: input.appendingPathComponent("photo.jpg"))
        let jobID = status.request.identity.jobID.uuidString
        let requests = RequestLog()
        NativeEditorURLProtocol.handler = { request in
            requests.urls.append(request.url?.absoluteString ?? "")
            if request.url?.host == "storage.e2e.test" { return (200, photoBytes) }
            let body = try JSONSerialization.jsonObject(with: NativeEditorTestSupport.bodyData(request)) as? [String: Any]
            let assetID = body?["asset_id"] as? String ?? ""
            return (200, Data(#"{"asset_id":"\#(assetID)","download_url":"https://storage.e2e.test/photo","expires_at":"2099-01-01T00:00:00Z"}"#.utf8))
        }
        defer { NativeEditorURLProtocol.handler = nil }
        let resolver = AuthorizedDeviceSourceResolver(api: NativeEditorTestSupport.api(), request: status.request, originals: store,
            library: RenderLibraryCache(root: project.root.appending(path: "library", directoryHint: .isDirectory)),
            downloadSession: NativeEditorTestSupport.session())
        let urls = try await resolver.resolve(for: recipe)
        let visualID = try XCTUnwrap(meta["visual_asset_id"] as? String)
        XCTAssertEqual(requests.urls, ["https://native-editor.test/me/jobs/\(jobID)/device-render/assets", "https://storage.e2e.test/photo"])
        XCTAssertEqual(urls[videoID]?.standardizedFileURL, original.resolvingSymlinksInPath().standardizedFileURL)
        XCTAssertTrue(urls[visualID]?.path.contains("still-derivatives") == true)

        let frames = input.appendingPathComponent("frames", isDirectory: true)
        try FileManager.default.createDirectory(at: frames, withIntermediateDirectories: true)
        let output = frames.appendingPathComponent("device-render.mp4")
        try? FileManager.default.removeItem(at: output)
        let checkpoint = try await AVFoundationLocalExporter(stateStore: FileExportStateStore(directory: project.root.appendingPathComponent("state")))
            .export(recipe: recipe, assetURLs: urls, outputURL: output)
        XCTAssertEqual(checkpoint.status, .completed)

        let asset = AVURLAsset(url: output)
        let duration = try await asset.load(.duration).seconds
        XCTAssertEqual(duration, TimelineMath.totalDuration(of: recipe), accuracy: 0.1)
        let audio = try await asset.loadTracks(withMediaType: .audio)
        XCTAssertFalse(audio.isEmpty, "source audio survives next to the still")
        let generator = AVAssetImageGenerator(asset: asset)
        generator.requestedTimeToleranceBefore = .zero; generator.requestedTimeToleranceAfter = .zero
        var samples: [String: (left: [Int], right: [Int])] = [:]
        for (name, time) in [("video", 1.0), ("crossfade", 2.85), ("photo", 4.0)] {
            let image = try await generator.image(at: CMTime(seconds: time, preferredTimescale: 600)).image
            XCTAssertEqual([image.width, image.height], [recipe.canvas.width, recipe.canvas.height])
            let destination = CGImageDestinationCreateWithURL(frames.appendingPathComponent("\(name).png") as CFURL, "public.png" as CFString, 1, nil)!
            CGImageDestinationAddImage(destination, image, nil)
            XCTAssertTrue(CGImageDestinationFinalize(destination))
            samples[name] = (pixel(image, x: image.width / 4, y: image.height / 2), pixel(image, x: image.width * 3 / 4, y: image.height / 2))
        }
        // Video: solid blue. Photo: cover-cropped, left half red, right half green.
        let video = try XCTUnwrap(samples["video"]), photo = try XCTUnwrap(samples["photo"])
        XCTAssertTrue(video.left[2] > 200 && video.left[0] < 60 && video.left[1] < 60, "\(video)")
        XCTAssertTrue(photo.left[0] > 200 && photo.left[1] < 60 && photo.left[2] < 60, "\(photo)")
        XCTAssertTrue(photo.right[1] > 200 && photo.right[0] < 60 && photo.right[2] < 60, "\(photo)")
        let blend = try XCTUnwrap(samples["crossfade"])
        XCTAssertTrue(blend.left[0] > 40 && blend.left[2] > 40, "crossfade mixes blue into red: \(blend)")
    }

    private func pixel(_ image: CGImage, x: Int, y: Int) -> [Int] {
        var rgba = [UInt8](repeating: 0, count: 4)
        let context = CGContext(data: &rgba, width: 1, height: 1, bitsPerComponent: 8, bytesPerRow: 4,
                                space: CGColorSpace(name: CGColorSpace.sRGB)!, bitmapInfo: CGImageAlphaInfo.premultipliedLast.rawValue)!
        context.draw(image, in: CGRect(x: -x, y: -(image.height - 1 - y), width: image.width, height: image.height))
        return rgba.prefix(3).map(Int.init)
    }
}
