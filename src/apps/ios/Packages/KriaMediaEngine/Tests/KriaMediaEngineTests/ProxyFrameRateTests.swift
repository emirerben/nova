#if canImport(AVFoundation)
import XCTest
import AVFoundation
import CoreGraphics
@testable import KriaMediaEngine

/// KRI-180: the analysis-proxy contract rejected some ordinary clips with "This clip couldn't be
/// processed" because it re-measured the proxy's frame rate as `nominalFrameRate` (frames ÷ duration).
/// These tests run the real `AVFoundationProxyGenerator` over clips of assorted frame rates and lengths
/// and record what the proxy actually reports.
@MainActor final class ProxyFrameRateTests: XCTestCase {
    struct Case: CustomStringConvertible {
        let fps: Double
        let seconds: Double
        let portrait: Bool
        var description: String { "\(fps)fps \(seconds)s \(portrait ? "portrait" : "landscape")" }
    }

    static let cases: [Case] = {
        var all: [Case] = []
        for fps in [24.0, 25.0, 29.97, 30.0, 59.94, 60.0] {
            for seconds in [1.0, 1.017, 2.55, 2.7333, 7.77, 10.01] {
                all.append(Case(fps: fps, seconds: seconds, portrait: fps.truncatingRemainder(dividingBy: 2) == 0))
            }
        }
        return all
    }()

    /// Writes an H.264 clip of `seconds` at `fps`, 160x90 (or 90x160 rotated via the track transform).
    private func makeClip(in directory: URL, _ spec: Case) async throws -> URL {
        let url = directory.appendingPathComponent("\(UUID().uuidString).mp4")
        let width = spec.portrait ? 90 : 160
        let height = spec.portrait ? 160 : 90
        let writer = try AVAssetWriter(outputURL: url, fileType: .mp4)
        let input = AVAssetWriterInput(mediaType: .video, outputSettings: [AVVideoCodecKey: AVVideoCodecType.h264, AVVideoWidthKey: width, AVVideoHeightKey: height])
        let adaptor = AVAssetWriterInputPixelBufferAdaptor(assetWriterInput: input, sourcePixelBufferAttributes: [kCVPixelBufferPixelFormatTypeKey as String: kCVPixelFormatType_32ARGB, kCVPixelBufferWidthKey as String: width, kCVPixelBufferHeightKey as String: height])
        writer.add(input)
        XCTAssertTrue(writer.startWriting())
        writer.startSession(atSourceTime: .zero)
        let frames = max(2, Int((spec.seconds * spec.fps).rounded()))
        let timescale: Int32 = 60000
        for index in 0..<frames {
            while !input.isReadyForMoreMediaData { try await Task.sleep(for: .milliseconds(2)) }
            var buffer: CVPixelBuffer?
            CVPixelBufferPoolCreatePixelBuffer(nil, try XCTUnwrap(adaptor.pixelBufferPool), &buffer)
            let pixel = try XCTUnwrap(buffer)
            CVPixelBufferLockBaseAddress(pixel, [])
            let context = try XCTUnwrap(CGContext(data: CVPixelBufferGetBaseAddress(pixel), width: width, height: height, bitsPerComponent: 8, bytesPerRow: CVPixelBufferGetBytesPerRow(pixel), space: CGColorSpace(name: CGColorSpace.sRGB)!, bitmapInfo: CGImageAlphaInfo.noneSkipFirst.rawValue))
            context.setFillColor(CGColor(red: CGFloat(index % 7) / 7, green: 0.4, blue: 0.6, alpha: 1))
            context.fill(CGRect(x: 0, y: 0, width: width, height: height))
            CVPixelBufferUnlockBaseAddress(pixel, [])
            let pts = CMTime(value: Int64((Double(index) / spec.fps * Double(timescale)).rounded()), timescale: timescale)
            XCTAssertTrue(adaptor.append(pixel, withPresentationTime: pts))
        }
        input.markAsFinished()
        writer.endSession(atSourceTime: CMTime(seconds: spec.seconds, preferredTimescale: timescale))
        await writer.finishWriting()
        XCTAssertEqual(writer.status, .completed, "\(spec): \(String(describing: writer.error))")
        return url
    }

    /// Records what the real proxy reports for each clip (prints a table) and pins the band the contract
    /// check accepts. `OUT-OF-BAND` in the table marks clips the old strict `<= 30` guard rejected.
    func testProxyFrameRateMatrix() async throws {
        let directory = FileManager.default.temporaryDirectory.appendingPathComponent("kri180-\(UUID().uuidString)")
        try FileManager.default.createDirectory(at: directory, withIntermediateDirectories: true)
        defer { try? FileManager.default.removeItem(at: directory) }
        var rows: [String] = []
        var outOfBand = 0
        for spec in Self.cases {
            let source = try await makeClip(in: directory, spec)
            let destination = directory.appendingPathComponent("proxy-\(UUID().uuidString).mp4")
            let proxy = try await AVFoundationProxyGenerator(preset: ProxyPreset(width: 640, height: 360)).makeProxy(for: source, destination: destination)
            // Manual ffprobe cross-check: KRI180_KEEP_DIR=/some/dir swift test ... keeps the proxies.
            if let keep = ProcessInfo.processInfo.environment["KRI180_KEEP_DIR"] {
                try? FileManager.default.createDirectory(atPath: keep, withIntermediateDirectories: true)
                try? FileManager.default.copyItem(at: proxy, to: URL(fileURLWithPath: keep).appendingPathComponent("\(spec.fps)-\(spec.seconds).mp4"))
            }
            let asset = AVURLAsset(url: proxy)
            let tracks = try await asset.loadTracks(withMediaType: .video)
            let track = try XCTUnwrap(tracks.first)
            let nominal = Double(try await track.load(.nominalFrameRate))
            let minDuration = try await track.load(.minFrameDuration).seconds
            let size = try await track.load(.naturalSize)
            let duration = try await asset.load(.duration).seconds
            let inBand = nominal >= 1 && nominal <= 30
            if !inBand { outOfBand += 1 }
            // What `validateProxyGeometry` relies on: the built proxy always measures within one frame of
            // `proxyFrameRate` (the strict `<= 30` it replaced rejected 30.0000019).
            XCTAssertEqual(nominal, Double(AVFoundationProxyGenerator.proxyFrameRate), accuracy: 1, "\(spec)")
            XCTAssertEqual(Double(size.width * size.height) > 0, true)
            rows.append("\(spec) -> nominal=\(nominal) 1/minFrameDuration=\(minDuration > 0 ? 1 / minDuration : -1) size=\(Int(size.width))x\(Int(size.height)) dur=\(duration) \(inBand ? "ok" : "OUT-OF-BAND")")
        }
        print("KRI180 proxy matrix:\n" + rows.joined(separator: "\n"))
        print("KRI180 out-of-band proxies (nominalFrameRate outside 1...30): \(outOfBand)/\(Self.cases.count)")
    }
}
#endif
