import AVFoundation
import KriaMediaEngine
import XCTest
@testable import Kria

/// Integration coverage for the native AVFoundation path. The bundled brand outro is a real short MP4
/// fixture (rather than a mocked asset), so this catches container, composition, and H.264 export failures.
@MainActor final class SlidePostVideoRenderTests: XCTestCase {
    private var directory: URL!

    override func setUp() {
        super.setUp()
        directory = FileManager.default.temporaryDirectory.appending(path: "slide-post-video-\(UUID())")
        try? FileManager.default.createDirectory(at: directory, withIntermediateDirectories: true)
    }

    override func tearDown() {
        try? FileManager.default.removeItem(at: directory)
        super.tearDown()
    }

    private func source() throws -> URL {
        try XCTUnwrap(KriaBranding.outroURL(), "the packaged real MP4 fixture is required by native export")
    }

    private func slide(look: String = "none") -> SlidePostSlide {
        SlidePostSlide(id: "slide", assetID: "asset", kind: "video", edits: .init(lookPreset: look))
    }

    private func asset(_ url: URL) -> SlidePostAsset {
        SlidePostAsset(id: "asset", kind: "video", status: "ready", displayURL: nil, sourceURL: url)
    }

    /// Builds a real, seekable 92-second H.264/AAC MP4 without materialising media bytes in memory.
    /// Repeating the bundled fixture makes the renderer exercise the actual 90-second trim boundary.
    private func ninetyTwoSecondSource() async throws -> URL {
        let sourceURL = try source()
        let source = AVURLAsset(url: sourceURL)
        let sourceVideos = try await source.loadTracks(withMediaType: .video)
        let sourceAudios = try await source.loadTracks(withMediaType: .audio)
        let video = try XCTUnwrap(sourceVideos.first)
        let audio = try XCTUnwrap(sourceAudios.first)
        let sourceDuration = try await source.load(.duration)
        let target = CMTime(seconds: 92, preferredTimescale: 600)
        guard sourceDuration.isValid, CMTimeCompare(sourceDuration, .zero) > 0 else { throw SlidePostVideoRender.RenderError.invalidDuration }

        let composition = AVMutableComposition()
        let destinationVideo = try XCTUnwrap(composition.addMutableTrack(withMediaType: .video, preferredTrackID: kCMPersistentTrackID_Invalid))
        let destinationAudio = try XCTUnwrap(composition.addMutableTrack(withMediaType: .audio, preferredTrackID: kCMPersistentTrackID_Invalid))
        var cursor = CMTime.zero
        while CMTimeCompare(cursor, target) < 0 {
            let remaining = CMTimeSubtract(target, cursor)
            let segment = CMTimeCompare(sourceDuration, remaining) < 0 ? sourceDuration : remaining
            let range = CMTimeRange(start: .zero, duration: segment)
            try destinationVideo.insertTimeRange(range, of: video, at: cursor)
            try destinationAudio.insertTimeRange(range, of: audio, at: cursor)
            cursor = CMTimeAdd(cursor, segment)
        }

        let output = directory.appending(path: "92-second-source.mp4")
        let exporter = try XCTUnwrap(AVAssetExportSession(asset: composition, presetName: AVAssetExportPresetPassthrough))
        exporter.outputURL = output
        exporter.outputFileType = .mp4
        await exporter.export()
        guard exporter.status == .completed else { throw exporter.error ?? SlidePostVideoRender.RenderError.exportFailed }
        return output
    }

    private func frame(_ url: URL, composition: AVVideoComposition? = nil) async throws -> CGImage {
        let asset = AVURLAsset(url: url)
        let generator = AVAssetImageGenerator(asset: asset)
        generator.appliesPreferredTrackTransform = true
        generator.requestedTimeToleranceBefore = .zero
        generator.requestedTimeToleranceAfter = .zero
        generator.videoComposition = composition
        return try await generator.image(at: CMTime(seconds: 0.2, preferredTimescale: 600)).image
    }

    private func meanDelta(_ first: CGImage, _ second: CGImage, in area: CGRect) -> Double {
        guard first.width == second.width, first.height == second.height else { return .infinity }
        let width = first.width, height = first.height
        var lhs = [UInt8](repeating: 0, count: width * height * 4), rhs = lhs
        let color = CGColorSpaceCreateDeviceRGB()
        CGContext(data: &lhs, width: width, height: height, bitsPerComponent: 8, bytesPerRow: width * 4,
                  space: color, bitmapInfo: CGImageAlphaInfo.noneSkipLast.rawValue)?.draw(first, in: CGRect(x: 0, y: 0, width: width, height: height))
        CGContext(data: &rhs, width: width, height: height, bitsPerComponent: 8, bytesPerRow: width * 4,
                  space: color, bitmapInfo: CGImageAlphaInfo.noneSkipLast.rawValue)?.draw(second, in: CGRect(x: 0, y: 0, width: width, height: height))
        var total = 0, count = 0
        for y in stride(from: Int(area.minY), to: Int(area.maxY), by: 8) {
            for x in stride(from: Int(area.minX), to: Int(area.maxX), by: 8) {
                let index = (y * width + x) * 4
                total += abs(Int(lhs[index]) - Int(rhs[index])) + abs(Int(lhs[index + 1]) - Int(rhs[index + 1])) + abs(Int(lhs[index + 2]) - Int(rhs[index + 2]))
                count += 3
            }
        }
        return Double(total) / Double(max(count, 1))
    }

    private func changedPixels(_ baseline: CGImage, _ rendered: CGImage, in area: CGRect) -> Int {
        guard baseline.width == rendered.width, baseline.height == rendered.height else { return 0 }
        let width = baseline.width, height = baseline.height
        var first = [UInt8](repeating: 0, count: width * height * 4)
        var second = first
        let color = CGColorSpaceCreateDeviceRGB()
        CGContext(data: &first, width: width, height: height, bitsPerComponent: 8, bytesPerRow: width * 4,
                  space: color, bitmapInfo: CGImageAlphaInfo.noneSkipLast.rawValue)?.draw(baseline, in: CGRect(x: 0, y: 0, width: width, height: height))
        CGContext(data: &second, width: width, height: height, bitsPerComponent: 8, bytesPerRow: width * 4,
                  space: color, bitmapInfo: CGImageAlphaInfo.noneSkipLast.rawValue)?.draw(rendered, in: CGRect(x: 0, y: 0, width: width, height: height))
        var count = 0
        for y in Int(area.minY)..<Int(area.maxY) {
            for x in stride(from: Int(area.minX), to: Int(area.maxX), by: 4) {
                let index = (y * width + x) * 4
                if abs(Int(first[index]) - Int(second[index])) + abs(Int(first[index + 1]) - Int(second[index + 1])) + abs(Int(first[index + 2]) - Int(second[index + 2])) > 180 { count += 1 }
            }
        }
        return count
    }

    func testExportsPlayableH264CarouselMP4WithSourceAudioAndBoundedDuration() async throws {
        let input = try source()
        let output = directory.appending(path: "slide.mp4")
        try await SlidePostVideoRender.renderSlide(slide: slide(), asset: asset(input), profile: "instagram_carousel", outputURL: output)

        let rendered = AVURLAsset(url: output)
        let renderedVideos = try await rendered.loadTracks(withMediaType: .video)
        let video = try XCTUnwrap(renderedVideos.first)
        let size = try await video.load(.naturalSize)
        XCTAssertEqual(size, CGSize(width: 2160, height: 2700))
        let duration = try await rendered.load(.duration).seconds
        XCTAssertTrue(duration.isFinite)
        XCTAssertGreaterThan(duration, 0)
        XCTAssertLessThanOrEqual(duration, SlidePostVideoRender.maximumDuration)
        let renderedAudio = try await rendered.loadTracks(withMediaType: .audio)
        let sourceAudio = try await AVURLAsset(url: input).loadTracks(withMediaType: .audio)
        XCTAssertEqual(renderedAudio.isEmpty,
                       sourceAudio.isEmpty,
                       "the MP4 must retain source audio whenever the source has it")
        let descriptions = try await video.load(.formatDescriptions)
        let description = try XCTUnwrap(descriptions.first)
        XCTAssertEqual(CMFormatDescriptionGetMediaSubType(description), kCMVideoCodecType_H264)
    }

    func testRejectsNonCarouselAndPosterDisplayURLsBeforeStartingAnExport() async throws {
        let input = try source()
        let poster = SlidePostAsset(id: "asset", kind: "video", status: "ready", displayURL: URL(fileURLWithPath: "/tmp/poster.jpg"), sourceURL: nil)
        XCTAssertFalse(SlidePostVideoRender.supports(slide: slide(), asset: poster, profile: "instagram_carousel"))
        XCTAssertFalse(SlidePostVideoRender.supports(slide: slide(), asset: asset(input), profile: "tiktok_photo"))
    }

    func testRemoteDownloadTemporaryFileIsDeletedWhenExportFails() async throws {
        let temporary = directory.appending(path: "download.mp4")
        try Data("not a movie".utf8).write(to: temporary)
        let remote = SlidePostAsset(id: "asset", kind: "video", status: "ready", displayURL: nil, sourceURL: URL(string: "https://example.test/video.mp4"))
        do {
            try await SlidePostVideoRender.renderSlide(slide: slide(), asset: remote, profile: "instagram_carousel", outputURL: directory.appending(path: "bad.mp4"), download: { _ in temporary })
            XCTFail("expected invalid source to fail")
        } catch { XCTAssertFalse(FileManager.default.fileExists(atPath: temporary.path)) }
    }

    func testNinetyTwoSecondSourceTrimsVideoAndAudioAtNinetySeconds() async throws {
        let input = try await ninetyTwoSecondSource()
        let output = directory.appending(path: "trimmed.mp4")
        try await SlidePostVideoRender.renderSlide(slide: slide(), asset: asset(input), profile: "instagram_carousel", outputURL: output)

        let rendered = AVURLAsset(url: output)
        let duration = try await rendered.load(.duration).seconds
        let videos = try await rendered.loadTracks(withMediaType: .video)
        let audios = try await rendered.loadTracks(withMediaType: .audio)
        let video = try XCTUnwrap(videos.first)
        let audio = try XCTUnwrap(audios.first, "source audio must survive the 90-second trim")
        let videoDuration = try await video.load(.timeRange).duration.seconds
        let audioDuration = try await audio.load(.timeRange).duration.seconds
        let frame = 1.0 / 30.0
        XCTAssertGreaterThanOrEqual(duration, SlidePostVideoRender.maximumDuration - frame)
        XCTAssertLessThanOrEqual(duration, SlidePostVideoRender.maximumDuration + frame)
        XCTAssertLessThanOrEqual(videoDuration, SlidePostVideoRender.maximumDuration + frame)
        XCTAssertLessThanOrEqual(audioDuration, SlidePostVideoRender.maximumDuration + frame)
    }

    /// Golden Hour is deterministic, so the preview composition and exported video can be compared directly
    /// away from the bottom-left brand tile. This locks the shared crop-before-grade path.
    func testGoldenHourPreviewAndExportAgreeAwayFromBranding() async throws {
        let input = try source()
        let sourceAsset = AVURLAsset(url: input)
        let preview = try await SlidePostVideoRender.previewComposition(asset: sourceAsset, profile: "instagram_carousel", lookPreset: "golden_hour")
        let output = directory.appending(path: "golden-hour.mp4")
        try await SlidePostVideoRender.renderSlide(slide: slide(look: "golden_hour"), asset: asset(input), profile: "instagram_carousel", outputURL: output)
        let previewFrame = try await frame(input, composition: preview)
        let exportFrame = try await frame(output)
        let width = CGFloat(previewFrame.width), height = CGFloat(previewFrame.height)
        let safeCentre = CGRect(x: width * 0.2, y: height * 0.2, width: width * 0.6, height: height * 0.6)
        XCTAssertLessThan(meanDelta(previewFrame, exportFrame, in: safeCentre), 18,
                          "the preview and MP4 should share the same cover crop and Golden Hour grade")
    }

    func testTopTextRasterIsCompositedUprightOverTheVideo() async throws {
        // Bitmap rows start at the displayed top; a Core Graphics y=0 fill lands in the final
        // backing row, so this test's first row must represent the image's visual top instead.
        let input = try source()
        let plain = directory.appending(path: "plain.mp4")
        let texted = directory.appending(path: "texted.mp4")
        try await SlidePostVideoRender.renderSlide(slide: slide(), asset: asset(input), profile: "instagram_carousel", outputURL: plain)
        var top = SlidePostTextElement(id: "top", text: "KRIA")
        top.position = "top"; top.sizePx = 200; top.color = "#000000"; top.shadowEnabled = false
        // The packaged paper outro is nearly white at 0.2s. Black gives the
        // changed-pixel assertion real contrast; white-on-white cannot prove
        // whether the compositor included the raster.
        let raster = try XCTUnwrap(SlidePostOnDeviceRender.textLayer([top], pixels: CGSize(width: 2160, height: 2700)))
        let rasterAttachment = XCTAttachment(image: raster)
        rasterAttachment.name = "top-text-raster"
        rasterAttachment.lifetime = .keepAlways
        add(rasterAttachment)
        let withText = SlidePostSlide(id: "slide", assetID: "asset", kind: "video", edits: .init(lookPreset: "none", texts: [top]))
        try await SlidePostVideoRender.renderSlide(slide: withText, asset: asset(input), profile: "instagram_carousel", outputURL: texted)
        let baseline = try await frame(plain), rendered = try await frame(texted)
        for (name, image) in [("baseline-frame", baseline), ("texted-frame", rendered)] {
            let attachment = XCTAttachment(image: UIImage(cgImage: image))
            attachment.name = name
            attachment.lifetime = .keepAlways
            add(attachment)
        }
        let width = CGFloat(baseline.width), height = CGFloat(baseline.height)
        let topBand = CGRect(x: 0, y: 0, width: width, height: height / 3)
        let bottomBand = CGRect(x: 0, y: height * 2 / 3, width: width, height: height / 3)
        XCTAssertGreaterThan(changedPixels(baseline, rendered, in: topBand), 200, "top text must render in UIKit's top band")
        XCTAssertLessThan(changedPixels(baseline, rendered, in: bottomBand), 200, "the overlay must not be vertically flipped")
    }
}
