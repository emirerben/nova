#if canImport(AVFoundation)
import XCTest
@preconcurrency import AVFoundation
@testable import KriaMediaEngine

/// Independent BT.709 vectors guard the actual encoder input boundary, not a white-only correction.
final class RecipeEncodingPixelsTests: XCTestCase {
    func testStandardVideoRangeColorsTimingAndIndependentBuffers() throws {
        let size = (width: 18, height: 10) // BGRA and YUV rows have padding.
        let converter = try RecipeEncodingPixels(width: size.width, height: size.height)
        let colors: [(rgb: [UInt8], yuv: [Int])] = [
            ([0, 0, 0], [16, 128, 128]), ([255, 255, 255], [235, 128, 128]),
            ([255, 0, 0], [63, 102, 240]), ([0, 255, 0], [173, 42, 26]),
            ([0, 0, 255], [32, 240, 118]), ([255, 255, 0], [219, 16, 138]),
            ([0, 255, 255], [188, 154, 16]), ([255, 0, 255], [78, 214, 230]),
            ([64, 64, 64], [71, 128, 128]), ([128, 128, 128], [126, 128, 128]),
            ([192, 192, 192], [181, 128, 128]), ([51, 102, 153], [97, 156, 104])
        ]
        var retained: [(CMSampleBuffer, [Int])] = []
        for (rgb, expected) in colors {
            let source = try sample(width: size.width, height: size.height, rgb: rgb)
            let output = try converter.convert(source)
            XCTAssertEqual(CMSampleBufferGetPresentationTimeStamp(output), CMSampleBufferGetPresentationTimeStamp(source))
            XCTAssertEqual(CMSampleBufferGetDuration(output), CMSampleBufferGetDuration(source))
            XCTAssertEqual(CMSampleBufferGetDecodeTimeStamp(output), CMSampleBufferGetDecodeTimeStamp(source))
            let pixels = try XCTUnwrap(CMSampleBufferGetImageBuffer(output))
            XCTAssertEqual(CVPixelBufferGetPixelFormatType(pixels), kCVPixelFormatType_420YpCbCr8BiPlanarVideoRange)
            for point in [(0, 0), (8, 4), (17, 9)] { XCTAssertEqual(yuv(pixels, x: point.0, y: point.1), expected) }
            XCTAssertEqual(CVBufferCopyAttachment(pixels, kCVImageBufferYCbCrMatrixKey, nil) as? String,
                           kCVImageBufferYCbCrMatrix_ITU_R_709_2 as String)
            XCTAssertEqual(CVBufferCopyAttachment(pixels, kCVImageBufferChromaLocationTopFieldKey, nil) as? String,
                           kCVImageBufferChromaLocation_Center as String)
            retained.append((output, expected))
        }
        for (output, expected) in retained {
            XCTAssertEqual(yuv(try XCTUnwrap(CMSampleBufferGetImageBuffer(output)), x: 8, y: 4), expected,
                           "later conversions must not mutate buffers still retained by the encoder")
        }
    }

    func testOddDimensionsKeepTheirEdgePixels() throws {
        let converter = try RecipeEncodingPixels(width: 17, height: 9)
        let output = try converter.convert(sample(width: 17, height: 9, rgb: [255, 255, 255]))
        let pixels = try XCTUnwrap(CMSampleBufferGetImageBuffer(output))
        XCTAssertEqual(CVPixelBufferGetWidth(pixels), 17)
        XCTAssertEqual(CVPixelBufferGetHeight(pixels), 9)
        XCTAssertEqual(yuv(pixels, x: 16, y: 8), [235, 128, 128])
    }

    func testMixedChromaBlocksAndColoredOddEdges() throws {
        let converter = try RecipeEncodingPixels(width: 3, height: 3)
        let source = try sample(width: 3, height: 3, rgb: [0, 0, 0])
        let pixels = try XCTUnwrap(CMSampleBufferGetImageBuffer(source))
        let colors: [[UInt8]] = [[255, 0, 0], [0, 255, 0], [0, 0, 255],
                                 [0, 0, 255], [255, 255, 255], [255, 0, 0],
                                 [0, 255, 255], [255, 0, 255], [255, 255, 0]]
        CVPixelBufferLockBaseAddress(pixels, [])
        let base = try XCTUnwrap(CVPixelBufferGetBaseAddress(pixels)).assumingMemoryBound(to: UInt8.self)
        for (index, rgb) in colors.enumerated() {
            let offset = (index / 3) * CVPixelBufferGetBytesPerRow(pixels) + (index % 3) * 4
            base[offset] = rgb[2]; base[offset + 1] = rgb[1]; base[offset + 2] = rgb[0]
        }
        CVPixelBufferUnlockBaseAddress(pixels, [])
        let output = try XCTUnwrap(CMSampleBufferGetImageBuffer(converter.convert(source)))
        // BT.709 luma per pixel. Chroma averages each centered 2x2 block before rounding,
        // extending the last row/column at odd edges: neutral, blue/red, cyan/magenta, yellow.
        let luma = [63, 173, 32, 32, 235, 63, 188, 78, 219]
        let chroma = [[128, 128], [171, 179], [184, 123], [16, 138]]
        for index in 0..<9 {
            let x = index % 3, y = index / 3, uv = chroma[(y / 2) * 2 + x / 2]
            XCTAssertEqual(yuv(output, x: x, y: y), [luma[index]] + uv, "pixel \(x), \(y)")
        }
    }

    func testColorGeometryMetadataAndValidDecodeTimestampArePreserved() throws {
        let converter = try RecipeEncodingPixels(width: 18, height: 10)
        let source = try sample(width: 18, height: 10, rgb: [255, 0, 0], decodeTime: CMTime(value: 9009, timescale: 30000))
        let pixels = try XCTUnwrap(CMSampleBufferGetImageBuffer(source))
        let aperture = [kCVImageBufferCleanApertureWidthKey: 16, kCVImageBufferCleanApertureHeightKey: 8,
                        kCVImageBufferCleanApertureHorizontalOffsetKey: 0, kCVImageBufferCleanApertureVerticalOffsetKey: 0] as CFDictionary
        let aspect = [kCVImageBufferPixelAspectRatioHorizontalSpacingKey: 4, kCVImageBufferPixelAspectRatioVerticalSpacingKey: 3] as CFDictionary
        CVBufferSetAttachment(pixels, kCVImageBufferCleanApertureKey, aperture, .shouldPropagate)
        CVBufferSetAttachment(pixels, kCVImageBufferPixelAspectRatioKey, aspect, .shouldPropagate)
        let result = try converter.convert(source), output = try XCTUnwrap(CMSampleBufferGetImageBuffer(result))
        XCTAssertEqual(CMSampleBufferGetDecodeTimeStamp(result), CMSampleBufferGetDecodeTimeStamp(source))
        XCTAssertEqual(CVBufferCopyAttachment(output, kCVImageBufferCleanApertureKey, nil) as? NSDictionary, aperture as NSDictionary)
        XCTAssertEqual(CVBufferCopyAttachment(output, kCVImageBufferPixelAspectRatioKey, nil) as? NSDictionary, aspect as NSDictionary)
        for (key, expected) in [(kCVImageBufferColorPrimariesKey, kCVImageBufferColorPrimaries_ITU_R_709_2),
                                (kCVImageBufferTransferFunctionKey, kCVImageBufferTransferFunction_ITU_R_709_2)] {
            XCTAssertEqual(CVBufferCopyAttachment(output, key, nil) as? String, expected as String)
        }
    }

    func testWrongPixelFormatAndInvalidPoolDimensionsAreRejected() throws {
        let converter = try RecipeEncodingPixels(width: 18, height: 10)
        XCTAssertThrowsError(try converter.convert(sample(width: 18, height: 10, rgb: [255, 0, 0], pixelFormat: kCVPixelFormatType_32ARGB)))
        XCTAssertThrowsError(try RecipeEncodingPixels(width: 0, height: 10))
        XCTAssertThrowsError(try RecipeEncodingPixels(width: 18, height: 0))
    }

    func testWrongDimensionsAreRejected() throws {
        let converter = try RecipeEncodingPixels(width: 18, height: 10)
        XCTAssertThrowsError(try converter.convert(sample(width: 20, height: 10, rgb: [255, 255, 255])))
    }

    private func sample(width: Int, height: Int, rgb: [UInt8], pixelFormat: OSType = kCVPixelFormatType_32BGRA,
                        decodeTime: CMTime = .invalid) throws -> CMSampleBuffer {
        var buffer: CVPixelBuffer?
        XCTAssertEqual(CVPixelBufferCreate(nil, width, height, pixelFormat,
                                          [kCVPixelBufferIOSurfacePropertiesKey: [:]] as CFDictionary, &buffer), kCVReturnSuccess)
        let pixels = try XCTUnwrap(buffer)
        CVPixelBufferLockBaseAddress(pixels, [])
        let base = try XCTUnwrap(CVPixelBufferGetBaseAddress(pixels)).assumingMemoryBound(to: UInt8.self)
        for y in 0..<height { for x in 0..<width {
            let index = y * CVPixelBufferGetBytesPerRow(pixels) + x * 4
            base[index] = rgb[2]; base[index + 1] = rgb[1]; base[index + 2] = rgb[0]; base[index + 3] = 255
        } }
        CVPixelBufferUnlockBaseAddress(pixels, [])
        var format: CMVideoFormatDescription?
        XCTAssertEqual(CMVideoFormatDescriptionCreateForImageBuffer(allocator: nil, imageBuffer: pixels, formatDescriptionOut: &format), noErr)
        var timing = CMSampleTimingInfo(duration: CMTime(value: 1001, timescale: 30000),
                                      presentationTimeStamp: CMTime(value: 10010, timescale: 30000), decodeTimeStamp: decodeTime)
        var result: CMSampleBuffer?
        XCTAssertEqual(CMSampleBufferCreateReadyWithImageBuffer(allocator: nil, imageBuffer: pixels, formatDescription: try XCTUnwrap(format),
                                                              sampleTiming: &timing, sampleBufferOut: &result), noErr)
        return try XCTUnwrap(result)
    }

    private func yuv(_ pixels: CVPixelBuffer, x: Int, y: Int) -> [Int] {
        CVPixelBufferLockBaseAddress(pixels, .readOnly)
        defer { CVPixelBufferUnlockBaseAddress(pixels, .readOnly) }
        let luma = CVPixelBufferGetBaseAddressOfPlane(pixels, 0)!.assumingMemoryBound(to: UInt8.self)
        let chroma = CVPixelBufferGetBaseAddressOfPlane(pixels, 1)!.assumingMemoryBound(to: UInt8.self)
        let uvIndex = (y / 2) * CVPixelBufferGetBytesPerRowOfPlane(pixels, 1) + (x / 2) * 2
        return [Int(luma[y * CVPixelBufferGetBytesPerRowOfPlane(pixels, 0) + x]), Int(chroma[uvIndex]), Int(chroma[uvIndex + 1])]
    }
}
#endif
