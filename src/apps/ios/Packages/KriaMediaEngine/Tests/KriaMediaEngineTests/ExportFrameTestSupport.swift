#if canImport(AVFoundation)
import Foundation
import XCTest
@preconcurrency import AVFoundation
import CoreImage
import CryptoKit
@testable import KriaMediaEngine

/// Deterministic stand-in for Vision that reads the pixels it is handed: confidence 1 on person green. A red mark in the
/// corner makes it throw (a failed frame), a blue mark makes it answer nothing (an unreadable one). Safe to call from many
/// threads and slow enough that overlapping calls overlap.
final class MarkerSegmenter: NativePersonSegmenting {
    let overlapping: Bool
    private let delay: UInt32
    private let lock = NSLock()
    private var inFlight = 0
    private(set) var highWater = 0
    private(set) var calls = 0
    private let context = CIContext(options: [.workingColorSpace: CGColorSpace(name: CGColorSpace.linearSRGB)!])
    struct Failed: Error {}
    init(overlapping: Bool, delayMicroseconds: UInt32 = 6000) { self.overlapping = overlapping; self.delay = delayMicroseconds }
    var supportsConcurrentRequests: Bool { overlapping }
    func confidence(for frame: CIImage, canvas: CGRect) throws -> SubjectConfidenceMap? {
        lock.lock(); inFlight += 1; calls += 1; highWater = max(highWater, inFlight); lock.unlock()
        defer { lock.lock(); inFlight -= 1; lock.unlock() }
        usleep(delay)
        let width = Int(canvas.width), height = Int(canvas.height)
        var pixels = [UInt8](repeating: 0, count: width * height * 4)
        context.render(frame.cropped(to: canvas).transformed(by: CGAffineTransform(translationX: -canvas.minX, y: -canvas.minY)),
                       toBitmap: &pixels, rowBytes: width * 4, bounds: CGRect(x: 0, y: 0, width: width, height: height),
                       format: .RGBA8, colorSpace: CGColorSpace(name: CGColorSpace.sRGB)!)
        func at(_ x: Int, _ y: Int) -> (Int, Int, Int) { let i = (y * width + x) * 4; return (Int(pixels[i]), Int(pixels[i + 1]), Int(pixels[i + 2])) }
        let corner = at(4, 4)
        if corner.0 > 200 && corner.1 < 80 && corner.2 < 80 { throw Failed() }
        if corner.2 > 200 && corner.0 < 80 && corner.1 < 80 { return nil }
        let values = (0..<width * height).map { index -> Float in pixels[index * 4 + 1] > 150 && pixels[index * 4] < 100 ? 1 : 0 }
        return SubjectConfidenceMap(width: width, height: height, values: values)
    }
}

/// Room magenta, a green person that sways, and a corner mark on chosen frames: red = Vision throws, blue = no answer.
@MainActor func writeMarkedSpeakerClip(directory: URL, name: String = "marked", size: (Int, Int), seconds: Double,
                                       failing: Set<Int> = [], unreadable: Set<Int> = []) async throws -> URL {
    try await writeSpeakerTestVideo(directory: directory, name: name, size: size, seconds: seconds) { context, index in
        let (width, height) = (Double(size.0), Double(size.1))
        context.setFillColor(SpeakerColor.room)
        context.fill(CGRect(x: 0, y: 0, width: width, height: height))
        context.setFillColor(SpeakerColor.person)
        let sway = Double(index % 20) / 20 * width * 0.1
        context.fill(CGRect(x: width * 0.25 + sway, y: 0, width: width * 0.4, height: height * 0.7))
        if failing.contains(index) || unreadable.contains(index) {
            context.setFillColor(failing.contains(index) ? CGColor(red: 1, green: 0, blue: 0, alpha: 1) : SpeakerColor.background)
            context.fill(CGRect(x: 0, y: height - 16, width: 16, height: 16))
        }
    }
}

final class EngineEventCounts: @unchecked Sendable {
    private let lock = NSLock()
    private var counts: [String: Int] = [:]
    func add(_ name: String) { lock.lock(); counts[name, default: 0] += 1; lock.unlock() }
    var snapshot: [String: Int] { lock.lock(); defer { lock.unlock() }; return counts }
}

/// SHA-256 of every composed frame as the compositor hands it to the encoder. The encoded file is not a fair witness: the
/// hardware encoder's output for identical frames varies with how they arrive, so decoded pixels differ run to run.
final class ComposedFrames: @unchecked Sendable {
    private let lock = NSLock()
    private(set) var hashes: [String] = []
    func install() {
        RecipeWriter.frameTap = { [self] buffer in
            CVPixelBufferLockBaseAddress(buffer, .readOnly); defer { CVPixelBufferUnlockBaseAddress(buffer, .readOnly) }
            let width = CVPixelBufferGetWidth(buffer), height = CVPixelBufferGetHeight(buffer), stride = CVPixelBufferGetBytesPerRow(buffer)
            var hasher = SHA256(); let base = CVPixelBufferGetBaseAddress(buffer)!
            for y in 0..<height { hasher.update(bufferPointer: UnsafeRawBufferPointer(start: base + y * stride, count: width * 4)) }
            let hex = hasher.finalize().map { String(format: "%02x", $0) }.joined()
            lock.lock(); hashes.append(hex); lock.unlock()
        }
    }
}
#endif
