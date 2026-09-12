import Foundation
import XCTest
@testable import KriaMediaEngine
#if canImport(AVFoundation)
@preconcurrency import AVFoundation
import CoreImage
import ImageIO

final class NativeCompositionTests: XCTestCase {
    @MainActor func testQuarterTurnMetadataMatchesSystemPlaybackOrientation() async throws {
        let directory = FileManager.default.temporaryDirectory.appendingPathComponent(UUID().uuidString)
        try FileManager.default.createDirectory(at: directory, withIntermediateDirectories: true)
        defer { try? FileManager.default.removeItem(at: directory) }
        for turn in [0.0, 1.0, 2.0, -1.0] {
            let url = try await makeVideo(directory: directory, name: "rotation-\(turn)", color: CGColor(red: 1, green: 0, blue: 0, alpha: 1),
                preferred: CGAffineTransform(rotationAngle: turn * .pi / 2), asymmetric: true)
            let system = AVAssetImageGenerator(asset: AVURLAsset(url: url))
            system.appliesPreferredTrackTransform = true
            let reference = try await system.image(at: CMTime(seconds: 0.4, preferredTimescale: 600)).image
            let recipe = EditRecipe(canvas: Canvas(width: reference.width, height: reference.height),
                assets: [MediaAsset(id: "source", relativePath: "source.mp4")],
                tracks: [TimelineTrack(id: "video", kind: .video, clips: [TimelineClip(id: "clip", sourceAssetID: "source", sourceDuration: 1)])])
            let preview = try await AVPlayerPreviewComposer().makePreview(recipe: recipe, assetURLs: ["source": url])
            let generated = AVAssetImageGenerator(asset: preview.playerItem.asset)
            generated.videoComposition = preview.playerItem.videoComposition
            let actual = try await generated.image(at: CMTime(seconds: 0.4, preferredTimescale: 600)).image
            let expectedPixels = rgba(reference), actualPixels = rgba(actual)
            XCTAssertEqual(actual.width, reference.width)
            XCTAssertEqual(actual.height, reference.height)
            // Compare dominant colors away from edges, independently of transfer curves.
            for (x, y) in [(20, 20), (reference.width - 20, 20), (20, reference.height - 20), (reference.width - 20, reference.height - 20)] {
                let offset = (y * reference.width + x) * 4
                let expectedRed = expectedPixels[offset] > expectedPixels[offset + 1]
                let actualRed = actualPixels[offset] > actualPixels[offset + 1]
                XCTAssertEqual(actualRed, expectedRed, "rotation \(turn), sample \(x),\(y)")
            }
        }
    }

    @MainActor func testLongStoryDecodesWithMoreThan64MBOfTimedText() async throws {
        let directory = FileManager.default.temporaryDirectory.appendingPathComponent(UUID().uuidString)
        try FileManager.default.createDirectory(at: directory, withIntermediateDirectories: true)
        defer { try? FileManager.default.removeItem(at: directory) }
        let photo = directory.appendingPathComponent("background.png")
        try CIContext().writePNGRepresentation(of: CIImage(color: .black).cropped(to: CGRect(x: 0, y: 0, width: 96, height: 160)),
            to: photo, format: .RGBA8, colorSpace: CGColorSpace(name: CGColorSpace.sRGB)!)
        let root = try XCTUnwrap(#filePath.range(of: "/src/apps/ios/"))
        let font = URL(fileURLWithPath: String(#filePath[..<root.lowerBound])).appendingPathComponent("src/apps/api/assets/fonts/Inter-Regular.ttf")
        let urls = ["photo": photo, "font": font]
        let assets = try urls.map { MediaAsset(id: $0.key, relativePath: $0.key, fingerprint: try SHA256Fingerprinter().fingerprint(file: $0.value)) }
        let manifest = try RenderAssetManifest(assets: assets.map { asset in
            RenderAssetReference(id: asset.id, fingerprint: try RenderFingerprint(asset.fingerprint!),
                source: asset.id == "font" ? .library(catalog: .font, catalogID: "Inter-Regular.ttf", generation: asset.fingerprint!.hex) : .original(mediaID: asset.id))
        })
        let canvas = Canvas(width: 1080, height: 1920)
        let text = try (0..<80).map { index in
            try AuthoredTextLayout.compile(id: "caption-\(index)", text: "A sunny day\nKeep going 🌈", start: Double(index), end: Double(index + 1),
                style: .init(fontAssetID: "font", size: 160, color: .init(red: 1, green: 1, blue: 1, alpha: 1)),
                fontURL: font, canvas: canvas)
        }
        let perLayer = try RecipeTextLayer.make(text[0], assetURLs: urls, canvas: CGSize(width: 1080, height: 1920)).bitmapBytes
        XCTAssertGreaterThan(perLayer * text.count, 64 * 1024 * 1024)
        let recipe = EditRecipe(schemaVersion: 2, rendererVersion: "kria-ios-2", canvas: canvas, assets: assets,
            tracks: [TimelineTrack(id: "video", kind: .video, clips: [TimelineClip(id: "photo", sourceAssetID: "photo", sourceDuration: 80)])],
            assetManifest: manifest, textLayers: text)
        let live = try await LivePreviewComposition(recipe: recipe, assetURLs: urls)
        let preview = live.preview
        let split = try live.textInteractionLayers(id: "caption-0", time: 0.5)
        let splitGenerator = AVAssetImageGenerator(asset: preview.playerItem.asset)
        splitGenerator.requestedTimeToleranceBefore = .zero; splitGenerator.requestedTimeToleranceAfter = .zero
        splitGenerator.videoComposition = split.above
        let upper = rgba(try await splitGenerator.image(at: CMTime(seconds: 0.5, preferredTimescale: 600)).image)
        XCTAssertTrue(stride(from: 3, to: upper.count, by: 4).allSatisfy { upper[$0] == 0 }, "Empty foreground must stay transparent")
        splitGenerator.videoComposition = split.below
        let lower = try await splitGenerator.image(at: CMTime(seconds: 0.5, preferredTimescale: 600)).image
        let positioned = CIImage(cgImage: split.text).transformed(by: CGAffineTransform(
            translationX: split.rect.minX * 1080, y: (1 - split.rect.maxY) * 1920))
        let reunited = try XCTUnwrap(CIContext().createCGImage(positioned.composited(over: CIImage(cgImage: lower)),
            from: CGRect(x: 0, y: 0, width: 1080, height: 1920)))
        let splitPixels = rgba(reunited)
        let generator = AVAssetImageGenerator(asset: preview.playerItem.asset)
        generator.videoComposition = preview.playerItem.videoComposition
        generator.requestedTimeToleranceBefore = .zero; generator.requestedTimeToleranceAfter = .zero
        var first: [UInt8]?
        for seconds in [0.5, 40.5, 79.5, 0.5] {
            let image = try await generator.image(at: CMTime(seconds: seconds, preferredTimescale: 600)).image
            XCTAssertEqual(image.width, 1080); XCTAssertEqual(image.height, 1920)
            let pixels = rgba(image)
            XCTAssertGreaterThan(pixels.filter { $0 > 200 }.count, 1080 * 1920)
            if let first { XCTAssertEqual(pixels, first) } else {
                first = pixels
                let error = zip(pixels, splitPixels).reduce(0) { $0 + abs(Int($1.0) - Int($1.1)) }
                XCTAssertLessThan(Double(error) / Double(pixels.count), 1, "Interactive layers must reconstruct the actual compositor frame")
            }
            let instruction = try XCTUnwrap(preview.playerItem.videoComposition?.instructions.first as? RecipeVideoInstruction)
            XCTAssertLessThanOrEqual(try XCTUnwrap(instruction.textStore).residentBitmapBytes, 64 * 1024 * 1024)
        }
    }

    @MainActor func testStyledOverlayKeepsLayerOrderAgainstLegacyPeer() async throws {
        let directory = FileManager.default.temporaryDirectory.appendingPathComponent(UUID().uuidString)
        try FileManager.default.createDirectory(at: directory, withIntermediateDirectories: true)
        defer { try? FileManager.default.removeItem(at: directory) }
        var urls: [String: URL] = [:]
        for (id, color) in [("base", CIColor.black), ("back", CIColor.blue), ("front", CIColor.red)] {
            let url = directory.appendingPathComponent(id + ".png")
            try CIContext().writePNGRepresentation(of: CIImage(color: color).cropped(to: CGRect(x: 0, y: 0, width: 96, height: 160)),
                to: url, format: .RGBA8, colorSpace: CGColorSpace(name: CGColorSpace.sRGB)!)
            urls[id] = url
        }
        let assets = try urls.map { MediaAsset(id: $0.key, relativePath: $0.key, fingerprint: try SHA256Fingerprinter().fingerprint(file: $0.value)) }
        let manifest = try RenderAssetManifest(assets: assets.map {
            RenderAssetReference(id: $0.id, fingerprint: try RenderFingerprint($0.fingerprint!), source: .original(mediaID: $0.id))
        })
        // The front layer starts first: temporal track sorting must not override z order.
        var recipe = EditRecipe(schemaVersion: 2, rendererVersion: "kria-ios-2", canvas: Canvas(width: 96, height: 160), assets: assets,
            tracks: [TimelineTrack(id: "v", kind: .video, clips: [TimelineClip(id: "base", sourceAssetID: "base", sourceDuration: 3)]),
                     TimelineTrack(id: "overlays", kind: .overlay, clips: [
                        TimelineClip(id: "back", sourceAssetID: "back", sourceDuration: 2.5, timelineStart: 0.25, overlayAboveText: true),
                        TimelineClip(id: "front", sourceAssetID: "front", sourceDuration: 2.5, volume: 0, holdDuration: 0.5, overlayAboveText: true,
                            visualPlacement: VisualMediaPlacement(order: 0, windowStart: 0, windowEnd: 3, editorStyle: .init(rotationDegrees: 25)))
                     ])], assetManifest: manifest)
        func center(_ live: LivePreviewComposition) async throws -> [UInt8] {
            let generator = AVAssetImageGenerator(asset: live.preview.playerItem.asset)
            generator.videoComposition = live.preview.playerItem.videoComposition
            generator.requestedTimeToleranceBefore = .zero; generator.requestedTimeToleranceAfter = .zero
            let pixels = rgba(try await generator.image(at: CMTime(seconds: 1, preferredTimescale: 600)).image)
            let index = (80 * 96 + 48) * 4
            return Array(pixels[index..<(index + 4)])
        }
        let live = try await LivePreviewComposition(recipe: recipe, assetURLs: urls)
        let initial = try await center(live)
        XCTAssertGreaterThan(initial[0], 200); XCTAssertLessThan(initial[2], 30)
        recipe.tracks[1].clips[1].visualPlacement = nil
        try live.updateText(recipe: recipe)
        let undone = try await center(live)
        XCTAssertGreaterThan(undone[0], 200); XCTAssertLessThan(undone[2], 30)
        recipe.tracks[1].clips[1].visualPlacement = VisualMediaPlacement(order: 0, windowStart: 0, windowEnd: 3, editorStyle: .init(rotationDegrees: 45))
        try live.updateText(recipe: recipe)
        let edited = try await center(live)
        XCTAssertGreaterThan(edited[0], 200); XCTAssertLessThan(edited[2], 30)
        recipe.tracks[1].clips.reverse()
        let reordered = try await LivePreviewComposition(recipe: recipe, assetURLs: urls)
        let sentBack = try await center(reordered)
        XCTAssertLessThan(sentBack[0], 30); XCTAssertGreaterThan(sentBack[2], 200)
    }

    @MainActor func testVisualFillUpdatesWithoutReplacingSources() async throws {
        let directory = FileManager.default.temporaryDirectory.appendingPathComponent(UUID().uuidString)
        try FileManager.default.createDirectory(at: directory, withIntermediateDirectories: true)
        defer { try? FileManager.default.removeItem(at: directory) }
        let base = directory.appendingPathComponent("base.png")
        try CIContext().writePNGRepresentation(of: CIImage(color: .black).cropped(to: CGRect(x: 0, y: 0, width: 96, height: 160)),
            to: base, format: .RGBA8, colorSpace: CGColorSpace(name: CGColorSpace.sRGB)!)
        let asset = MediaAsset(id: "base", relativePath: "base", fingerprint: try SHA256Fingerprinter().fingerprint(file: base))
        let manifest = try RenderAssetManifest(assets: [RenderAssetReference(id: "base", fingerprint: RenderFingerprint(asset.fingerprint!), source: .original(mediaID: "base"))])
        var recipe = EditRecipe(schemaVersion: 2, rendererVersion: "kria-ios-2", canvas: Canvas(width: 96, height: 160), assets: [asset],
            tracks: [TimelineTrack(id: "v", kind: .video, clips: [TimelineClip(id: "base", sourceAssetID: "base", sourceDuration: 3)])],
            assetManifest: manifest, visualFills: [VisualCanvasFill(id: "fill", start: 1, end: 2, order: 1, kind: .solid, color: TextInk(red: 1, green: 0, blue: 0, alpha: 1))])
        let live = try await LivePreviewComposition(recipe: recipe, assetURLs: ["base": base])
        let source = live.preview.playerItem.asset
        func redPixels(at time: Double) async throws -> Int {
            let generator = AVAssetImageGenerator(asset: live.preview.playerItem.asset)
            generator.videoComposition = live.preview.playerItem.videoComposition
            generator.requestedTimeToleranceBefore = .zero; generator.requestedTimeToleranceAfter = .zero
            let bytes = rgba(try await generator.image(at: CMTime(seconds: time, preferredTimescale: 600)).image)
            return stride(from: 0, to: bytes.count, by: 4).filter { bytes[$0] > 180 && bytes[$0 + 1] < 80 }.count
        }
        let before = try await redPixels(at: 0.5), during = try await redPixels(at: 1.5), after = try await redPixels(at: 2.5)
        XCTAssertEqual(before, 0); XCTAssertEqual(after, 0); XCTAssertGreaterThan(during, 15000)
        recipe.visualFills[0].start = 0.25
        try live.updateText(recipe: recipe)
        XCTAssertTrue(source === live.preview.playerItem.asset)
        let moved = try await redPixels(at: 0.5)
        XCTAssertGreaterThan(moved, 15000)
    }

    @MainActor func testOverlayPositionPopAndFrozenTailMatchPreviewAndExport() async throws {
        let directory = FileManager.default.temporaryDirectory.appendingPathComponent(UUID().uuidString)
        try FileManager.default.createDirectory(at: directory, withIntermediateDirectories: true)
        defer { try? FileManager.default.removeItem(at: directory) }
        let base = directory.appendingPathComponent("black.png")
        try CIContext().writePNGRepresentation(of: CIImage(color: .black).cropped(to: CGRect(x: 0, y: 0, width: 96, height: 160)),
            to: base, format: .RGBA8, colorSpace: CGColorSpace(name: CGColorSpace.sRGB)!)
        let card = try await makeVideo(directory: directory, name: "card", color: CGColor(red: 1, green: 0, blue: 0, alpha: 1))
        let urls = ["base": base, "card": card]
        let assets = try urls.sorted(by: { $0.key < $1.key }).map { MediaAsset(id: $0.key, relativePath: $0.key, fingerprint: try SHA256Fingerprinter().fingerprint(file: $0.value)) }
        let manifest = try RenderAssetManifest(assets: assets.map { RenderAssetReference(id: $0.id, fingerprint: try RenderFingerprint($0.fingerprint!), source: .original(mediaID: $0.id)) })
        var recipe = EditRecipe(schemaVersion: 2, rendererVersion: "kria-ios-2", canvas: Canvas(width: 96, height: 160), assets: assets,
            tracks: [TimelineTrack(id: "v", kind: .video, clips: [TimelineClip(id: "background", sourceAssetID: "base", sourceDuration: 3)]),
                     TimelineTrack(id: "overlays", kind: .overlay, clips: [TimelineClip(id: "overlay", sourceAssetID: "card", sourceStart: 0.25, sourceDuration: 0.25,
                        timelineStart: 0.5, transform: MediaTransform(scale: 0.5, positionX: 24, positionY: 40), volume: 0,
                        holdDuration: 1.75, overlayPopIn: true, overlayPreserveAlpha: false)])], assetManifest: manifest)
        let preview = try await LivePreviewComposition(recipe: recipe, assetURLs: urls)
        let output = directory.appendingPathComponent("overlay.mp4")
        _ = try await AVFoundationLocalExporter(stateStore: FileExportStateStore(directory: directory.appendingPathComponent("state"))).export(recipe: recipe, assetURLs: urls, outputURL: output)
        let reference = AVAssetImageGenerator(asset: preview.preview.playerItem.asset)
        reference.videoComposition = preview.preview.playerItem.videoComposition
        let exported = AVAssetImageGenerator(asset: AVURLAsset(url: output))
        var series: [[Int]] = []
        for generator in [reference, exported] {
            generator.requestedTimeToleranceBefore = .zero; generator.requestedTimeToleranceAfter = .zero
            var counts: [Int] = []
            for time in [0.2, 0.5, 0.8, 2.2, 2.6, 0.8] {
                let bytes = rgba(try await generator.image(at: CMTime(seconds: time, preferredTimescale: 600)).image)
                counts.append(stride(from: 0, to: bytes.count, by: 4).filter { bytes[$0] > 180 && bytes[$0 + 1] < 80 }.count)
            }
            XCTAssertEqual(counts[0], 0); XCTAssertEqual(counts[4], 0)
            XCTAssertGreaterThan(counts[1], 2000); XCTAssertLessThan(counts[1], counts[2])
            XCTAssertEqual(counts[2], counts[3]); XCTAssertEqual(counts[5], counts[2])
            series.append(counts)
        }
        for (a, b) in zip(series[0], series[1]) { XCTAssertLessThanOrEqual(abs(a - b), 160) }
        let geometry = try XCTUnwrap(preview.mediaSelectionBounds(id: "overlay", time: 2.2))
        XCTAssertEqual(geometry.centerX, 0.75, accuracy: 0.001)
        XCTAssertEqual(geometry.centerY, 0.75, accuracy: 0.001)
        let item = preview.preview.playerItem, source = preview.preview.playerItem.asset
        recipe.tracks[1].clips[0].transform.positionX = -24
        try preview.updateText(recipe: recipe)
        XCTAssertTrue(item === preview.preview.playerItem); XCTAssertTrue(source === item.asset)
        XCTAssertEqual(preview.mediaSelectionBounds(id: "overlay", time: 2.2)?.centerX ?? -1, 0.25, accuracy: 0.001)
        recipe.tracks[1].clips[0].overlayAboveText = true
        recipe.tracks[1].clips[0].visualPlacement = VisualMediaPlacement(order: 1, widthFraction: 0.4,
            xFraction: 0.5, yFraction: 0.5, windowStart: 0.5, windowEnd: 2.5, editorStyle: VisualEditorStyle(rotationDegrees: 90))
        let rotatedPreview = try await LivePreviewComposition(recipe: recipe, assetURLs: urls)
        let rotated = try XCTUnwrap(rotatedPreview.mediaSelectionBounds(id: "overlay", time: 0.7))
        XCTAssertEqual(rotated.rotationDegrees, 90)
        XCTAssertEqual(rotated.width, 38.0 / 96, accuracy: 0.001, "Chrome uses the unrotated media box, then rotates once")
        let rotatedOutput = directory.appendingPathComponent("rotated-held-overlay.mp4")
        _ = try await AVFoundationLocalExporter(stateStore: FileExportStateStore(directory: directory.appendingPathComponent("rotated-state")))
            .export(recipe: recipe, assetURLs: urls, outputURL: rotatedOutput)
        let heldPreview = AVAssetImageGenerator(asset: rotatedPreview.preview.playerItem.asset)
        heldPreview.videoComposition = rotatedPreview.preview.playerItem.videoComposition
        for generator in [heldPreview, AVAssetImageGenerator(asset: AVURLAsset(url: rotatedOutput))] {
            generator.requestedTimeToleranceBefore = .zero; generator.requestedTimeToleranceAfter = .zero
            let pixels = rgba(try await generator.image(at: CMTime(seconds: 2.2, preferredTimescale: 600)).image)
            XCTAssertGreaterThan(stride(from: 0, to: pixels.count, by: 4).filter { pixels[$0] > 180 && pixels[$0 + 1] < 80 }.count, 1000,
                "Styled video overlays retain their last frame after the trimmed source ends")
        }

    }

    @MainActor func testPhotoOnlyTimelinePreviewsAndExportsThroughSameClock() async throws {
        let directory = FileManager.default.temporaryDirectory.appendingPathComponent(UUID().uuidString)
        try FileManager.default.createDirectory(at: directory, withIntermediateDirectories: true)
        defer { try? FileManager.default.removeItem(at: directory) }
        var urls: [String: URL] = [:]
        for (id, color) in [("red", CIColor(red: 1, green: 0, blue: 0)), ("blue", CIColor(red: 0, green: 0, blue: 1))] {
            let image = CIImage(color: color).cropped(to: CGRect(x: 0, y: 0, width: 96, height: 160))
            let cgImage = try XCTUnwrap(CIContext().createCGImage(image, from: image.extent))
            let url = directory.appendingPathComponent(id + ".png")
            let destination = try XCTUnwrap(CGImageDestinationCreateWithURL(url as CFURL, "public.png" as CFString, 1, nil))
            CGImageDestinationAddImage(destination, cgImage, nil)
            XCTAssertTrue(CGImageDestinationFinalize(destination))
            urls[id] = url
        }
        let recipe = EditRecipe(canvas: Canvas(width: 96, height: 160), assets: [MediaAsset(id: "red", relativePath: "red"), MediaAsset(id: "blue", relativePath: "blue")], tracks: [
            TimelineTrack(id: "photos", kind: .video, clips: [
                TimelineClip(id: "first", sourceAssetID: "red", sourceDuration: 0.6),
                TimelineClip(id: "second", sourceAssetID: "blue", sourceDuration: 0.6, timelineStart: 0.4, transition: Transition(duration: 0.2))
            ])
        ])
        let preview = try await AVPlayerPreviewComposer().makePreview(recipe: recipe, assetURLs: urls)
        let videoOutput = AVPlayerItemVideoOutput(pixelBufferAttributes: [
            kCVPixelBufferPixelFormatTypeKey as String: kCVPixelFormatType_32BGRA
        ])
        preview.playerItem.add(videoOutput)
        let player = AVPlayer(playerItem: preview.playerItem)
        for seconds in [0.1, 0.9, 0.11, 0.12, 0.89, 0.88, 0.13] {
            let target = CMTime(seconds: seconds, preferredTimescale: 600)
            let finished = await withCheckedContinuation { continuation in
                player.seek(to: target, toleranceBefore: .zero, toleranceAfter: .zero) {
                    continuation.resume(returning: $0)
                }
            }
            XCTAssertTrue(finished)
            var pixel: CVPixelBuffer?
            for _ in 0..<100 {
                pixel = videoOutput.copyPixelBuffer(forItemTime: target, itemTimeForDisplay: nil)
                if pixel != nil { break }
                try await Task.sleep(for: .milliseconds(10))
            }
            let buffer = try XCTUnwrap(pixel, "No displayed frame after seek to \(seconds)")
            let image = CIImage(cvPixelBuffer: buffer)
            let bytes = rgba(try XCTUnwrap(CIContext().createCGImage(image, from: image.extent)))
            let center = (80 * 96 + 48) * 4
            XCTAssertGreaterThan(bytes[center + (seconds < 0.2 ? 0 : 2)], 220)
            XCTAssertLessThan(bytes[center + (seconds < 0.2 ? 2 : 0)], 40)
        }
        player.replaceCurrentItem(with: nil)
        let output = directory.appendingPathComponent("photos.mp4")
        _ = try await AVFoundationLocalExporter(stateStore: FileExportStateStore(directory: directory.appendingPathComponent("state"))).export(recipe: recipe, assetURLs: urls, outputURL: output)
        let exported = AVURLAsset(url: output)
        let duration = try await exported.load(.duration)
        XCTAssertEqual(duration.seconds, 1, accuracy: 1 / 30)
        let audioTracks = try await exported.loadTracks(withMediaType: .audio)
        XCTAssertEqual(audioTracks.count, 1)
        let audioTrack = try XCTUnwrap(audioTracks.first)
        let descriptions = try await audioTrack.load(.formatDescriptions)
        XCTAssertEqual(CMFormatDescriptionGetMediaSubType(try XCTUnwrap(descriptions.first)), kAudioFormatMPEG4AAC)
        let audioReader = try AVAssetReader(asset: exported)
        let pcm = AVAssetReaderTrackOutput(track: audioTrack, outputSettings: [AVFormatIDKey: kAudioFormatLinearPCM,
            AVLinearPCMBitDepthKey: 16, AVLinearPCMIsFloatKey: false, AVLinearPCMIsNonInterleaved: false])
        audioReader.add(pcm)
        XCTAssertTrue(audioReader.startReading())
        var decodedBytes = 0
        while let sample = pcm.copyNextSampleBuffer() {
            let block = try XCTUnwrap(CMSampleBufferGetDataBuffer(sample))
            var bytes = Data(count: CMBlockBufferGetDataLength(block))
            let status = bytes.withUnsafeMutableBytes { CMBlockBufferCopyDataBytes(block, atOffset: 0, dataLength: $0.count, destination: $0.baseAddress!) }
            XCTAssertEqual(status, noErr)
            let peak = bytes.withUnsafeBytes { $0.bindMemory(to: Int16.self).map { abs(Int($0)) }.max() ?? 0 }
            XCTAssertLessThanOrEqual(peak, 2)
            decodedBytes += bytes.count
        }
        XCTAssertEqual(audioReader.status, .completed)
        XCTAssertGreaterThan(decodedBytes, 48_000 * 3)
        let reference = AVAssetImageGenerator(asset: preview.playerItem.asset)
        reference.videoComposition = preview.playerItem.videoComposition
        reference.requestedTimeToleranceBefore = .zero; reference.requestedTimeToleranceAfter = .zero
        let rendered = AVAssetImageGenerator(asset: exported)
        rendered.requestedTimeToleranceBefore = .zero; rendered.requestedTimeToleranceAfter = .zero
        for seconds in [0.1, 0.5, 0.9] {
            let time = CMTime(seconds: seconds, preferredTimescale: 600)
            let expected = rgba(try await reference.image(at: time).image)
            let actual = rgba(try await rendered.image(at: time).image)
            let error = zip(expected, actual).map { abs(Int($0) - Int($1)) }.reduce(0, +)
            XCTAssertLessThan(Double(error) / Double(actual.count), 8)
            let center = (80 * 96 + 48) * 4
            if seconds < 0.2 { XCTAssertGreaterThan(actual[center], 220); XCTAssertLessThan(actual[center + 2], 40) }
            if seconds > 0.8 { XCTAssertGreaterThan(actual[center + 2], 220); XCTAssertLessThan(actual[center], 40) }
            if seconds == 0.5 { XCTAssertGreaterThan(actual[center], 70); XCTAssertGreaterThan(actual[center + 2], 70) }
        }
    }

    @MainActor func testExportCannotOverwriteOriginalThroughSymlink() async throws {
        let directory = FileManager.default.temporaryDirectory.appendingPathComponent(UUID().uuidString)
        try FileManager.default.createDirectory(at: directory, withIntermediateDirectories: true)
        defer { try? FileManager.default.removeItem(at: directory) }
        let source = directory.appendingPathComponent("original.mp4")
        let bytes = Data("irreplaceable original".utf8)
        try bytes.write(to: source)
        let alias = directory.appendingPathComponent("export.mp4")
        try FileManager.default.createSymbolicLink(at: alias, withDestinationURL: source)
        let recipe = EditRecipe(assets: [MediaAsset(id: "a", relativePath: "a")], tracks: [TimelineTrack(id: "v", kind: .video, clips: [TimelineClip(id: "c", sourceAssetID: "a", sourceDuration: 1)])])
        let exporter = AVFoundationLocalExporter(stateStore: FileExportStateStore(directory: directory.appendingPathComponent("state")))
        for output in [source, alias] {
            do {
                _ = try await exporter.export(recipe: recipe, assetURLs: ["a": source], outputURL: output)
                XCTFail("Original overwrite must be rejected")
            } catch { XCTAssertEqual(try Data(contentsOf: source), bytes) }
        }
    }

    @MainActor func testRealExportMatchesPreviewTimingCrossfadeAndCodec() async throws {
        let directory = FileManager.default.temporaryDirectory.appendingPathComponent(UUID().uuidString)
        try FileManager.default.createDirectory(at: directory, withIntermediateDirectories: true)
        defer { try? FileManager.default.removeItem(at: directory) }
        let red = try await makeVideo(directory: directory, name: "red", color: CGColor(red: 1, green: 0, blue: 0, alpha: 1))
        let green = try await makeVideo(directory: directory, name: "green", color: CGColor(red: 0, green: 1, blue: 0, alpha: 1))
        let recipe = EditRecipe(canvas: Canvas(width: 96, height: 160), assets: [MediaAsset(id: "r", relativePath: "r"), MediaAsset(id: "g", relativePath: "g")], tracks: [
            TimelineTrack(id: "v", kind: .video, clips: [
                TimelineClip(id: "r", sourceAssetID: "r", sourceDuration: 0.6, text: TextTreatment(text: "TOP", fontSize: 15, anchor: .top, animation: .none)),
                TimelineClip(id: "g", sourceAssetID: "g", sourceDuration: 0.6, timelineStart: 0.4, transition: Transition(duration: 0.2), text: TextTreatment(text: "BOTTOM", fontSize: 15, anchor: .bottom, animation: .none))
            ])
        ])
        let urls = ["r": red, "g": green]
        let preview = try await AVPlayerPreviewComposer().makePreview(recipe: recipe, assetURLs: urls)
        XCTAssertEqual(preview.description.duration, 1, accuracy: 0.0001)
        let validator = CompositionValidator()
        let valid = try await preview.playerItem.videoComposition!.isValid(for: preview.playerItem.asset, timeRange: CMTimeRange(start: .zero, duration: CMTime(seconds: 1, preferredTimescale: 600)), validationDelegate: validator)
        XCTAssertTrue(valid)
        let output = directory.appendingPathComponent("output.mp4")
        let exporter = AVFoundationLocalExporter(stateStore: FileExportStateStore(directory: directory.appendingPathComponent("checkpoints")))
        let checkpoint = try await exporter.export(recipe: recipe, assetURLs: urls, outputURL: output, exportID: "test")
        XCTAssertEqual(checkpoint.status, .completed)
        let exported = AVURLAsset(url: output)
        let duration = try await exported.load(.duration)
        XCTAssertEqual(duration.seconds, 1, accuracy: 1 / 30)
        let tracks = try await exported.loadTracks(withMediaType: .video)
        let track = try XCTUnwrap(tracks.first)
        let descriptions = try await track.load(.formatDescriptions)
        XCTAssertEqual(CMFormatDescriptionGetMediaSubType(try XCTUnwrap(descriptions.first)), kCMVideoCodecType_H264)
        let previewGenerator = AVAssetImageGenerator(asset: preview.playerItem.asset)
        previewGenerator.videoComposition = preview.playerItem.videoComposition
        let exportGenerator = AVAssetImageGenerator(asset: exported)
        previewGenerator.requestedTimeToleranceBefore = .zero
        previewGenerator.requestedTimeToleranceAfter = .zero
        exportGenerator.requestedTimeToleranceBefore = .zero
        exportGenerator.requestedTimeToleranceAfter = .zero
        for seconds in [0.1, 0.5, 0.9] {
            let time = CMTime(seconds: seconds, preferredTimescale: 600)
            let expected = try await previewGenerator.image(at: time).image
            let actual = try await exportGenerator.image(at: time).image
            let a = rgba(expected), b = rgba(actual)
            XCTAssertEqual(a.count, b.count)
            let error = zip(a, b).map { abs(Int($0) - Int($1)) }.reduce(0, +)
            XCTAssertLessThan(Double(error) / Double(a.count), 8, "Preview/export parity at \(seconds)")
            let center = (80 * 96 + 48) * 4
            if seconds < 0.2 { XCTAssertGreaterThan(b[center], 220); XCTAssertLessThan(b[center + 1], 64) }
            if seconds > 0.8 { XCTAssertGreaterThan(b[center + 1], 220); XCTAssertLessThan(b[center], 30) }
            if seconds == 0.5 { XCTAssertGreaterThan(b[center], 70); XCTAssertGreaterThan(b[center + 1], 70) }
        }
        // Both treatments are present, have different intervals and are retained by every segment.
        let instruction = try XCTUnwrap(preview.playerItem.videoComposition?.instructions.first as? RecipeVideoInstruction)
        XCTAssertEqual(instruction.text.count, 2)
        XCTAssertEqual(instruction.text[1].start, 0.4)
        XCTAssertGreaterThan(instruction.text[0].frame.minY, instruction.text[1].frame.minY)
    }

    @MainActor func testProxyHasBoundedDimensionsAndPreservesSourceDuration() async throws {
        let directory = FileManager.default.temporaryDirectory.appendingPathComponent(UUID().uuidString)
        try FileManager.default.createDirectory(at: directory, withIntermediateDirectories: true)
        defer { try? FileManager.default.removeItem(at: directory) }
        let original = try await makeVideo(directory: directory, name: "source", color: CGColor(gray: 0.5, alpha: 1))
        let destination = directory.appendingPathComponent("proxy.mp4")
        _ = try await AVFoundationProxyGenerator(preset: ProxyPreset(width: 64, height: 36)).makeProxy(for: original, destination: destination)
        let proxy = AVURLAsset(url: destination)
        let tracks = try await proxy.loadTracks(withMediaType: .video)
        let track = try XCTUnwrap(tracks.first)
        let size = try await track.load(.naturalSize)
        XCTAssertLessThanOrEqual(max(size.width, size.height), 64)
        let duration = try await proxy.load(.duration)
        XCTAssertEqual(duration.seconds, 1, accuracy: 1 / 30)
        XCTAssertNotEqual(try SHA256Fingerprinter().fingerprint(file: original), try SHA256Fingerprinter().fingerprint(file: destination))
    }

    @MainActor private func makeVideo(directory: URL, name: String, color: CGColor, preferred: CGAffineTransform = .identity, asymmetric: Bool = false) async throws -> URL {
        let url = directory.appendingPathComponent("\(name).mp4")
        let writer = try AVAssetWriter(outputURL: url, fileType: .mp4)
        let input = AVAssetWriterInput(mediaType: .video, outputSettings: [AVVideoCodecKey: AVVideoCodecType.h264, AVVideoWidthKey: 96, AVVideoHeightKey: 160])
        let adaptor = AVAssetWriterInputPixelBufferAdaptor(assetWriterInput: input, sourcePixelBufferAttributes: [kCVPixelBufferPixelFormatTypeKey as String: kCVPixelFormatType_32ARGB, kCVPixelBufferWidthKey as String: 96, kCVPixelBufferHeightKey as String: 160])
        input.transform = preferred
        writer.add(input)
        XCTAssertTrue(writer.startWriting())
        writer.startSession(atSourceTime: .zero)
        for index in 0..<30 {
            while !input.isReadyForMoreMediaData { try await Task.sleep(for: .milliseconds(2)) }
            var buffer: CVPixelBuffer?
            CVPixelBufferPoolCreatePixelBuffer(nil, try XCTUnwrap(adaptor.pixelBufferPool), &buffer)
            let pixel = try XCTUnwrap(buffer)
            CVPixelBufferLockBaseAddress(pixel, [])
            let context = try XCTUnwrap(CGContext(data: CVPixelBufferGetBaseAddress(pixel), width: 96, height: 160, bitsPerComponent: 8, bytesPerRow: CVPixelBufferGetBytesPerRow(pixel), space: CGColorSpace(name: CGColorSpace.sRGB)!, bitmapInfo: CGImageAlphaInfo.noneSkipFirst.rawValue))
            context.setFillColor(color)
            context.fill(CGRect(x: 0, y: 0, width: 96, height: 160))
            if asymmetric {
                context.setFillColor(CGColor(red: 0, green: 1, blue: 0, alpha: 1))
                context.fill(CGRect(x: 0, y: 80, width: 96, height: 80))
            }
            CVPixelBufferUnlockBaseAddress(pixel, [])
            XCTAssertTrue(adaptor.append(pixel, withPresentationTime: CMTime(value: Int64(index), timescale: 30)))
        }
        input.markAsFinished()
        await writer.finishWriting()
        XCTAssertEqual(writer.status, .completed)
        return url
    }

    private func rgba(_ image: CGImage) -> [UInt8] {
        var data = [UInt8](repeating: 0, count: image.width * image.height * 4)
        data.withUnsafeMutableBytes { bytes in
            let context = CGContext(data: bytes.baseAddress, width: image.width, height: image.height, bitsPerComponent: 8, bytesPerRow: image.width * 4, space: CGColorSpace(name: CGColorSpace.sRGB)!, bitmapInfo: CGImageAlphaInfo.premultipliedLast.rawValue)!
            context.draw(image, in: CGRect(x: 0, y: 0, width: image.width, height: image.height))
        }
        return data
    }
}
private final class CompositionValidator: NSObject, AVVideoCompositionValidationHandling {
    func videoComposition(_ videoComposition: AVVideoComposition, shouldContinueValidatingAfterFindingInvalidValueForKey key: String) -> Bool { XCTFail("Invalid composition key: \(key)"); return false }
    func videoComposition(_ videoComposition: AVVideoComposition, shouldContinueValidatingAfterFindingEmptyTimeRange timeRange: CMTimeRange) -> Bool { XCTFail("Uncovered composition time: \(timeRange)"); return false }
    func videoComposition(_ videoComposition: AVVideoComposition, shouldContinueValidatingAfterFindingInvalidTimeRangeIn instruction: any AVVideoCompositionInstructionProtocol) -> Bool { XCTFail("Invalid instruction range: \(instruction.timeRange)"); return false }
    func videoComposition(_ videoComposition: AVVideoComposition, shouldContinueValidatingAfterFindingInvalidTrackIDIn instruction: any AVVideoCompositionInstructionProtocol, layerInstruction: AVVideoCompositionLayerInstruction, asset: AVAsset) -> Bool { XCTFail("Invalid track: \(layerInstruction.trackID)"); return false }
}
#endif
