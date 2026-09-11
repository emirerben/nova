import Foundation
import XCTest
@testable import KriaMediaEngine
#if canImport(CoreVideo)
import CoreVideo
#endif

final class GoldenHourGradeTests: XCTestCase {
    func testProductionFFmpegPlanes() throws {
        guard let path = ProcessInfo.processInfo.environment["KRIA_LOOK_FIXTURE_DIR"] else {
            throw XCTSkip("Set KRIA_LOOK_FIXTURE_DIR to production-generated YUV fixtures")
        }
        let directory = URL(fileURLWithPath: path)
        let input = try Data(contentsOf: directory.appendingPathComponent("input.yuv"))
        let expected = try Data(contentsOf: directory.appendingPathComponent("golden.yuv"))
        let actual = try GoldenHourGrade.apply420(Array(input), width: 512, height: 512)
        XCTAssertEqual(actual.count, expected.count)
        let mismatches = actual.indices.filter { actual[$0] != expected[$0] }
        XCTAssertEqual(mismatches.count, 0, "First mismatches: \(mismatches.prefix(10).map { "\($0): \(actual[$0])/\(expected[$0])" })")
        #if canImport(CoreVideo)
        let source = try buffer(Array(input), width: 512, height: 512)
        let output = try GoldenHourGrade.apply(to: source)
        XCTAssertEqual(planes(output), Array(expected))
        XCTAssertEqual(planes(source), Array(input), "Source must survive seeking and buffer reuse")
        #endif
    }

    func testRejectsInvalidPlanes() {
        for (w, h) in [(0, 2), (3, 2), (2, 3), (7682, 2), (2, 2)] {
            XCTAssertThrowsError(try GoldenHourGrade.apply420([], width: w, height: h))
        }
    }

    #if canImport(CoreVideo)
    func testPaddedRowsAndAttachments() throws {
        let count: Int = 30 * 18 * 3 / 2
        let input: [UInt8] = (0..<count).map { index in UInt8((index * 73) % 256) }
        let source = try buffer(input, width: 30, height: 18)
        XCTAssertGreaterThan(CVPixelBufferGetBytesPerRowOfPlane(source, 0), 30)
        CVBufferSetAttachment(source, kCVImageBufferYCbCrMatrixKey, kCVImageBufferYCbCrMatrix_ITU_R_709_2, .shouldPropagate)
        let output = try GoldenHourGrade.apply(to: source)
        XCTAssertEqual(planes(output), try GoldenHourGrade.apply420(input, width: 30, height: 18))
        XCTAssertEqual(planes(source), input)
        XCTAssertEqual(CVBufferCopyAttachment(output, kCVImageBufferYCbCrMatrixKey, nil) as? String,
                       kCVImageBufferYCbCrMatrix_ITU_R_709_2 as String)
    }

    private func buffer(_ bytes: [UInt8], width: Int, height: Int) throws -> CVPixelBuffer {
        var value: CVPixelBuffer?
        XCTAssertEqual(CVPixelBufferCreate(kCFAllocatorDefault, width, height,
            kCVPixelFormatType_420YpCbCr8BiPlanarVideoRange,
            [kCVPixelBufferBytesPerRowAlignmentKey: 64] as CFDictionary, &value), kCVReturnSuccess)
        let result = try XCTUnwrap(value)
        CVPixelBufferLockBaseAddress(result, [])
        defer { CVPixelBufferUnlockBaseAddress(result, []) }
        let y = CVPixelBufferGetBaseAddressOfPlane(result, 0)!.assumingMemoryBound(to: UInt8.self)
        let uv = CVPixelBufferGetBaseAddressOfPlane(result, 1)!.assumingMemoryBound(to: UInt8.self)
        for row in 0..<height {
            for col in 0..<width { y[row * CVPixelBufferGetBytesPerRowOfPlane(result, 0) + col] = bytes[row * width + col] }
        }
        for row in 0..<(height / 2) {
            for col in 0..<(width / 2) {
                let index = row * width / 2 + col
                let target = row * CVPixelBufferGetBytesPerRowOfPlane(result, 1) + col * 2
                uv[target] = bytes[width * height + index]
                uv[target + 1] = bytes[width * height * 5 / 4 + index]
            }
        }
        return result
    }

    private func planes(_ buffer: CVPixelBuffer) -> [UInt8] {
        let width = CVPixelBufferGetWidth(buffer), height = CVPixelBufferGetHeight(buffer)
        CVPixelBufferLockBaseAddress(buffer, .readOnly)
        defer { CVPixelBufferUnlockBaseAddress(buffer, .readOnly) }
        let y = CVPixelBufferGetBaseAddressOfPlane(buffer, 0)!.assumingMemoryBound(to: UInt8.self)
        let uv = CVPixelBufferGetBaseAddressOfPlane(buffer, 1)!.assumingMemoryBound(to: UInt8.self)
        var result = [UInt8](repeating: 0, count: width * height * 3 / 2)
        for row in 0..<height {
            for col in 0..<width { result[row * width + col] = y[row * CVPixelBufferGetBytesPerRowOfPlane(buffer, 0) + col] }
        }
        for row in 0..<(height / 2) {
            for col in 0..<(width / 2) {
                let index = row * width / 2 + col
                let source = row * CVPixelBufferGetBytesPerRowOfPlane(buffer, 1) + col * 2
                result[width * height + index] = uv[source]
                result[width * height * 5 / 4 + index] = uv[source + 1]
            }
        }
        return result
    }
    #endif
}
