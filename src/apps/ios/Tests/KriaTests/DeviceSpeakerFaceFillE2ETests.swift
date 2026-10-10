import Foundation
import XCTest
import AVFoundation
import CoreMedia
import CoreGraphics
import CoreVideo
import ImageIO
import UniformTypeIdentifiers
import KriaMediaEngine
@testable import Kria

/// KRI-547 journey: the face-filled vertical crop, rendered on the iPhone.
///
/// The server face-fills a sideways (1920x1080) Talking speaker by giving its
/// MAIN-track clip an identity scale plus a `MediaTransform.position_x` shift
/// (`phone_recipe_shared.face_fill_transform`); the claim is that the engine's
/// `Composition.transform` applies that shift after the cover fill, so the
/// creator sees exactly the window the server chose. No production recipe
/// shifted a main-track clip before, so this test renders the server's own
/// device-render status (`Fixtures/KRI547SpeakerFaceFill.json`, pinned against
/// `compile_phone_subtitled_plan` by
/// `src/apps/api/tests/pipeline/test_kri547_face_fill_device_fixture.py`)
/// through the production exporter, over a generated clip whose source x is
/// readable from the picture: vertical colour bands of a known width. Every
/// band edge on screen names one source x, so the shift the device applied is
/// measured, not assumed.
///
/// The clip is generated here, so its fingerprint cannot match the fixture's:
/// the exporter is handed the file directly (the resolver is covered by the
/// other device journeys).
@MainActor final class DeviceSpeakerFaceFillE2ETests: XCTestCase {
    /// Canvas px the measured shift may differ from the recipe's: band edges
    /// blur by a pixel or two through cover scaling and H.264 chroma.
    private let shiftTolerancePx = 4.0
    /// Source px the visible window's edges may differ from the server's.
    private let windowTolerancePx = 2.5

    func testFaceInTheLeftThirdShowsTheServerWindowOnTheIPhone() async throws {
        try await assertCase("face_left_third")
    }

    func testFaceAtTheRightEdgeClampsWithNoBlackEdgeOnTheIPhone() async throws {
        try await assertCase("face_right_edge_clamped")
    }

    func testCentredFaceIsTheUnshiftedCentreCropOnTheIPhone() async throws {
        try await assertCase("face_centred")
    }

    // MARK: -

    private func assertCase(_ name: String) async throws {
        let fixtureURL = URL(fileURLWithPath: #filePath).deletingLastPathComponent()
            .deletingLastPathComponent().appendingPathComponent("Fixtures/KRI547SpeakerFaceFill.json")
        let fixture = try XCTUnwrap(JSONSerialization.jsonObject(with: Data(contentsOf: fixtureURL)) as? [String: Any])
        let source = try XCTUnwrap(fixture["source"] as? [String: Any])
        let sourceWidth = try XCTUnwrap(source["width"] as? Int)
        let sourceHeight = try XCTUnwrap(source["height"] as? Int)
        let bandWidth = try XCTUnwrap(source["band_width_px"] as? Int)
        let bands = try XCTUnwrap(source["band_rgb"] as? [[Int]])
        let mediaID = try XCTUnwrap(source["media_id"] as? String)
        let duration = try XCTUnwrap(source["duration_s"] as? Double)
        let cases = try XCTUnwrap(fixture["cases"] as? [[String: Any]])
        let meta = try XCTUnwrap(cases.first { $0["name"] as? String == name }, "\(name) is not in the fixture")
        let expectedShift = try XCTUnwrap((meta["position_x"] as? NSNumber)?.doubleValue)
        let expectedWindow = try XCTUnwrap(meta["visible_source_px"] as? [Double])

        // The status exactly as the phone polls it.
        let status = try JSONDecoder().decode(
            DeviceRenderStatusResponse.self,
            from: JSONSerialization.data(withJSONObject: try XCTUnwrap(meta["status"]))
        )
        let recipe = status.request.recipe
        let speaker = try XCTUnwrap(recipe.tracks.first { $0.kind == .video }?.clips.first)
        XCTAssertEqual(speaker.sourceAssetID, mediaID)
        XCTAssertEqual(speaker.transform.scale, 1, "\(name): a face fill is the cover fill, never a letterbox")
        XCTAssertEqual(speaker.transform.positionX, expectedShift, accuracy: 1e-9, "\(name): the decoded shift")
        // No new capability: today's phones render the face fill locally.
        let route = DeviceRenderSessions.decision(recipe, capabilities: PhoneRenderingCapabilities(
            enabled: true, recipeVersions: [2], verifiedFeatures: ["basicComposition", "local1080Export"]
        )).route
        XCTAssertEqual(route, .local, name)

        let directory = FileManager.default.temporaryDirectory.appendingPathComponent("kri547-\(UUID().uuidString)")
        try FileManager.default.createDirectory(at: directory, withIntermediateDirectories: true)
        defer { try? FileManager.default.removeItem(at: directory) }
        let clip = try await makeBandedClip(
            url: directory.appendingPathComponent("speaker.mp4"), width: sourceWidth, height: sourceHeight,
            bandWidth: bandWidth, bands: bands, seconds: duration
        )
        let movie = directory.appendingPathComponent("\(name).mp4")
        let checkpoint = try await AVFoundationLocalExporter(
            stateStore: FileExportStateStore(directory: directory.appendingPathComponent("state")), branding: .none
        ).export(recipe: recipe, assetURLs: [mediaID: clip], outputURL: movie)
        XCTAssertEqual(checkpoint.status, .completed, name)

        let asset = AVURLAsset(url: movie)
        let exportedDuration = try await asset.load(.duration).seconds
        XCTAssertEqual(exportedDuration, duration, accuracy: 0.1, name)
        let generator = AVAssetImageGenerator(asset: asset)
        generator.requestedTimeToleranceBefore = .zero; generator.requestedTimeToleranceAfter = .zero
        let frame = try await generator.image(at: CMTime(seconds: duration / 2, preferredTimescale: 600)).image
        XCTAssertEqual([frame.width, frame.height], [recipe.canvas.width, recipe.canvas.height], name)
        attach(frame, name: "\(name)-frame")

        let canvasWidth = Double(recipe.canvas.width)
        let cover = max(canvasWidth / Double(sourceWidth), Double(recipe.canvas.height) / Double(sourceHeight))
        let pixels = try rgba(frame)
        var shifts: [Double] = []
        for y in [frame.height / 4, frame.height / 2, frame.height * 3 / 4] {
            let runs = bandRuns(pixels, width: frame.width, row: y, bands: bands)
            let first = try XCTUnwrap(runs.first), last = try XCTUnwrap(runs.last)
            // No black edge, and the edge bands are the ones the window names.
            XCTAssertEqual(first.band, Int(expectedWindow[0]) / bandWidth, "\(name) row \(y): band at the left edge")
            XCTAssertEqual(last.band, Int(expectedWindow[1] - 0.01) / bandWidth, "\(name) row \(y): band at the right edge")
            XCTAssertGreaterThanOrEqual(runs.count, 3, "\(name) row \(y): at least two band edges on screen")
            for (left, right) in zip(runs, runs.dropFirst()) {
                // Left to right, one band at a time: never mirrored, never skipped.
                XCTAssertEqual(right.band, left.band + 1, "\(name) row \(y): bands \(runs.map(\.band))")
                guard left.band >= 0, right.band == left.band + 1 else { continue }
                let edgeOnCanvas = Double(left.end + right.start) / 2
                let edgeInSource = Double(right.band * bandWidth)
                // canvas x = (source x - W/2) * cover + canvas W/2 + position_x
                let shift = edgeOnCanvas - canvasWidth / 2 - (edgeInSource - Double(sourceWidth) / 2) * cover
                XCTAssertEqual(shift, expectedShift, accuracy: shiftTolerancePx,
                               "\(name) row \(y): source x \(edgeInSource) drawn at canvas x \(edgeOnCanvas)")
                shifts.append(shift)
            }
        }
        let measured = shifts.reduce(0, +) / Double(max(shifts.count, 1))
        let windowLeft = Double(sourceWidth) / 2 - (canvasWidth / 2 + measured) / cover
        let windowRight = windowLeft + canvasWidth / cover
        print("[kri547] \(name) position_x expected=\(expectedShift) measured=\(String(format: "%.2f", measured)) " +
              "window expected=\(expectedWindow) measured=[\(String(format: "%.2f", windowLeft)), \(String(format: "%.2f", windowRight))] " +
              "edges=\(shifts.count)")
        XCTAssertFalse(shifts.isEmpty, name)
        XCTAssertEqual(measured, expectedShift, accuracy: shiftTolerancePx, "\(name): measured shift")
        XCTAssertEqual(windowLeft, expectedWindow[0], accuracy: windowTolerancePx, "\(name): visible window left (source px)")
        XCTAssertEqual(windowRight, expectedWindow[1], accuracy: windowTolerancePx, "\(name): visible window right (source px)")
    }

    /// Runs of one classified band along `row`, `end` exclusive. A pixel takes
    /// the nearest band colour, or -1 when black is nearer (a missing-picture
    /// edge). Runs shorter than 4 px are blur at a band edge and are dropped,
    /// so the neighbouring runs meet at the edge's midpoint.
    private func bandRuns(_ pixels: [UInt8], width: Int, row: Int, bands: [[Int]]) -> [(band: Int, start: Int, end: Int)] {
        let palette = bands + [[0, 0, 0]]
        var runs: [(band: Int, start: Int, end: Int)] = []
        for x in 0..<width {
            let i = (row * width + x) * 4
            let rgb = [Int(pixels[i]), Int(pixels[i + 1]), Int(pixels[i + 2])]
            let nearest = palette.indices.min { a, b in
                zip(palette[a], rgb).map { ($0 - $1) * ($0 - $1) }.reduce(0, +) <
                    zip(palette[b], rgb).map { ($0 - $1) * ($0 - $1) }.reduce(0, +)
            }!
            let band = nearest == bands.count ? -1 : nearest
            if let lastRun = runs.last, lastRun.band == band, lastRun.end == x {
                runs[runs.count - 1].end = x + 1
            } else {
                runs.append((band, x, x + 1))
            }
        }
        var merged: [(band: Int, start: Int, end: Int)] = []
        for run in runs where run.end - run.start >= 4 {
            if let lastRun = merged.last, lastRun.band == run.band { merged[merged.count - 1].end = run.end } else { merged.append(run) }
        }
        return merged
    }

    private func rgba(_ image: CGImage) throws -> [UInt8] {
        var pixels = [UInt8](repeating: 0, count: image.width * image.height * 4)
        let context = try XCTUnwrap(CGContext(
            data: &pixels, width: image.width, height: image.height, bitsPerComponent: 8, bytesPerRow: image.width * 4,
            space: CGColorSpace(name: CGColorSpace.sRGB)!, bitmapInfo: CGImageAlphaInfo.premultipliedLast.rawValue
        ))
        context.draw(image, in: CGRect(x: 0, y: 0, width: image.width, height: image.height))
        return pixels
    }

    private func attach(_ image: CGImage, name: String) {
        let data = NSMutableData()
        guard let destination = CGImageDestinationCreateWithData(data, UTType.png.identifier as CFString, 1, nil) else { return }
        CGImageDestinationAddImage(destination, image, nil)
        guard CGImageDestinationFinalize(destination) else { return }
        let attachment = XCTAttachment(data: data as Data, uniformTypeIdentifier: UTType.png.identifier)
        attachment.name = name
        attachment.lifetime = .keepAlways
        add(attachment)
    }

    /// A silent H.264 clip of `width`x`height` (identity orientation): vertical
    /// bands `bandWidth` px wide in `bands` order, left to right.
    private func makeBandedClip(url: URL, width: Int, height: Int, bandWidth: Int, bands: [[Int]], seconds: Double) async throws -> URL {
        XCTAssertEqual(bandWidth * bands.count, width, "the bands tile the source width")
        let fps: Int32 = 30
        let writer = try AVAssetWriter(outputURL: url, fileType: .mp4)
        let input = AVAssetWriterInput(mediaType: .video, outputSettings: [
            AVVideoCodecKey: AVVideoCodecType.h264, AVVideoWidthKey: width, AVVideoHeightKey: height,
        ])
        let adaptor = AVAssetWriterInputPixelBufferAdaptor(assetWriterInput: input, sourcePixelBufferAttributes: [
            kCVPixelBufferPixelFormatTypeKey as String: kCVPixelFormatType_32ARGB,
            kCVPixelBufferWidthKey as String: width, kCVPixelBufferHeightKey as String: height,
        ])
        writer.add(input)
        XCTAssertTrue(writer.startWriting())
        writer.startSession(atSourceTime: .zero)
        for index in 0..<Int((seconds * Double(fps)).rounded(.up)) {
            while !input.isReadyForMoreMediaData { try await Task.sleep(for: .milliseconds(2)) }
            var buffer: CVPixelBuffer?
            CVPixelBufferPoolCreatePixelBuffer(nil, try XCTUnwrap(adaptor.pixelBufferPool), &buffer)
            let pixel = try XCTUnwrap(buffer)
            CVPixelBufferLockBaseAddress(pixel, [])
            let context = try XCTUnwrap(CGContext(
                data: CVPixelBufferGetBaseAddress(pixel), width: width, height: height, bitsPerComponent: 8,
                bytesPerRow: CVPixelBufferGetBytesPerRow(pixel), space: CGColorSpace(name: CGColorSpace.sRGB)!,
                bitmapInfo: CGImageAlphaInfo.noneSkipFirst.rawValue
            ))
            for (band, rgb) in bands.enumerated() {
                context.setFillColor(red: CGFloat(rgb[0]) / 255, green: CGFloat(rgb[1]) / 255, blue: CGFloat(rgb[2]) / 255, alpha: 1)
                context.fill(CGRect(x: band * bandWidth, y: 0, width: bandWidth, height: height))
            }
            CVPixelBufferUnlockBaseAddress(pixel, [])
            XCTAssertTrue(adaptor.append(pixel, withPresentationTime: CMTime(value: Int64(index), timescale: fps)))
        }
        input.markAsFinished()
        await writer.finishWriting()
        XCTAssertEqual(writer.status, .completed, String(describing: writer.error))
        return url
    }
}
