#if canImport(ImageIO)
import CoreGraphics
import Foundation
import ImageIO
import UniformTypeIdentifiers
import XCTest
@testable import KriaMediaEngine

/// KRI-121: Visuals-pool photos reach the compositor through one cover-sized,
/// upright copy per verified source instead of a full-resolution decode.
final class StillImageDerivativeTests: XCTestCase {
    private func directory() -> URL { FileManager.default.temporaryDirectory.appendingPathComponent(UUID().uuidString) }

    /// The top half of the stored pixels is red and the bottom half blue, so a
    /// rotation shows up as a change in which side is red.
    private func writeImage(width: Int, height: Int, type: UTType, orientation: Int? = nil, to url: URL) throws -> RenderFingerprint {
        let context = try XCTUnwrap(CGContext(data: nil, width: width, height: height, bitsPerComponent: 8, bytesPerRow: 0,
                                              space: CGColorSpace(name: CGColorSpace.sRGB)!, bitmapInfo: CGImageAlphaInfo.noneSkipLast.rawValue))
        context.setFillColor(CGColor(srgbRed: 0, green: 0, blue: 1, alpha: 1))
        context.fill(CGRect(x: 0, y: 0, width: width, height: height))
        // Quartz puts the origin bottom-left, so the upper half is the first stored rows.
        context.setFillColor(CGColor(srgbRed: 1, green: 0, blue: 0, alpha: 1))
        context.fill(CGRect(x: 0, y: height / 2, width: width, height: height - height / 2))
        let image = try XCTUnwrap(context.makeImage())
        let destination = try XCTUnwrap(CGImageDestinationCreateWithURL(url as CFURL, type.identifier as CFString, 1, nil))
        var options: [CFString: Any] = [:]
        if let orientation { options[kCGImagePropertyOrientation] = orientation }
        CGImageDestinationAddImage(destination, image, options as CFDictionary)
        XCTAssertTrue(CGImageDestinationFinalize(destination))
        return try RenderFingerprint(SHA256Fingerprinter().fingerprint(file: url))
    }

    private func properties(_ url: URL) throws -> [CFString: Any] {
        let source = try XCTUnwrap(CGImageSourceCreateWithURL(url as CFURL, nil))
        return try XCTUnwrap(CGImageSourceCopyPropertiesAtIndex(source, 0, nil) as? [CFString: Any])
    }

    private func pixelSize(_ url: URL) throws -> (width: Double, height: Double) {
        let values = try properties(url)
        return try (XCTUnwrap((values[kCGImagePropertyPixelWidth] as? NSNumber)?.doubleValue),
                    XCTUnwrap((values[kCGImagePropertyPixelHeight] as? NSNumber)?.doubleValue))
    }

    /// Red and blue channels of one decoded pixel; `y` only picks a row, so
    /// callers compare left and right, which no coordinate flip can swap.
    private func color(_ image: CGImage, x: Int, y: Int) throws -> (red: UInt8, blue: UInt8) {
        let pixel = try XCTUnwrap(image.cropping(to: CGRect(x: x, y: y, width: 1, height: 1)))
        let context = try XCTUnwrap(CGContext(data: nil, width: 1, height: 1, bitsPerComponent: 8, bytesPerRow: 0,
                                              space: CGColorSpace(name: CGColorSpace.sRGB)!, bitmapInfo: CGImageAlphaInfo.premultipliedLast.rawValue))
        context.draw(pixel, in: CGRect(x: 0, y: 0, width: 1, height: 1))
        let bytes = try XCTUnwrap(context.data).assumingMemoryBound(to: UInt8.self)
        return (bytes[0], bytes[2])
    }

    func testLargePhotoIsCoverSizedForTheCanvasAndReused() throws {
        let root = directory(); defer { try? FileManager.default.removeItem(at: root) }
        try FileManager.default.createDirectory(at: root, withIntermediateDirectories: true)
        let source = root.appendingPathComponent("photo.png")
        let fingerprint = try writeImage(width: 4000, height: 3000, type: .png, to: source)
        let derivatives = root.appendingPathComponent("still-derivatives")
        let first = try StillImageDerivative.prepare(source: source, fingerprint: fingerprint, canvas: Canvas(width: 1080, height: 1920), directory: derivatives)
        XCTAssertNotEqual(first, source)
        // Cover, not fit: 1920 / 3000 scales the short side to the canvas height.
        let size = try pixelSize(first)
        XCTAssertEqual(size.height, 1920, accuracy: 2)
        XCTAssertEqual(size.width, 2560, accuracy: 2)
        let written = try FileManager.default.attributesOfItem(atPath: first.path)

        let again = try StillImageDerivative.prepare(source: source, fingerprint: fingerprint, canvas: Canvas(width: 1080, height: 1920), directory: derivatives)
        XCTAssertEqual(again, first)
        let reused = try FileManager.default.attributesOfItem(atPath: again.path)
        XCTAssertEqual(reused[.systemFileNumber] as? NSNumber, written[.systemFileNumber] as? NSNumber)
        XCTAssertEqual(reused[.modificationDate] as? Date, written[.modificationDate] as? Date)
        XCTAssertEqual(try FileManager.default.contentsOfDirectory(atPath: derivatives.path), [first.lastPathComponent])

        // The cached copy is keyed by its size, so another canvas never reuses a smaller one.
        let smaller = try StillImageDerivative.prepare(source: source, fingerprint: fingerprint, canvas: Canvas(width: 720, height: 1280), directory: derivatives)
        XCTAssertNotEqual(smaller, first)
        XCTAssertEqual(try pixelSize(smaller).height, 1280, accuracy: 2)
        XCTAssertFalse(try FileManager.default.contentsOfDirectory(atPath: derivatives.path).contains(where: { $0.hasSuffix(".partial") }))
    }

    func testSmallPhotoRendersFromItsVerifiedBytesWithoutUpscaling() throws {
        let root = directory(); defer { try? FileManager.default.removeItem(at: root) }
        try FileManager.default.createDirectory(at: root, withIntermediateDirectories: true)
        let source = root.appendingPathComponent("small.png")
        let fingerprint = try writeImage(width: 300, height: 200, type: .png, to: source)
        let derivatives = root.appendingPathComponent("still-derivatives")
        let prepared = try StillImageDerivative.prepare(source: source, fingerprint: fingerprint, canvas: .vertical1080, directory: derivatives)
        XCTAssertEqual(prepared, source)
        XCTAssertFalse(FileManager.default.fileExists(atPath: derivatives.path))
    }

    /// The compositor applies EXIF orientation from the file it reads, so the
    /// derivative must be upright pixels with no orientation left to apply twice.
    func testRotatedPhotoBecomesUprightWithoutAnOrientationTag() throws {
        let root = directory(); defer { try? FileManager.default.removeItem(at: root) }
        try FileManager.default.createDirectory(at: root, withIntermediateDirectories: true)
        let source = root.appendingPathComponent("rotated.jpg")
        let fingerprint = try writeImage(width: 2400, height: 1500, type: .jpeg, orientation: 6, to: source)
        XCTAssertEqual((try properties(source)[kCGImagePropertyOrientation] as? NSNumber)?.intValue, 6)

        let prepared = try StillImageDerivative.prepare(source: source, fingerprint: fingerprint, canvas: .vertical1080, directory: root.appendingPathComponent("still-derivatives"))
        XCTAssertNotEqual(prepared, source)
        let values = try properties(prepared)
        XCTAssertEqual((values[kCGImagePropertyOrientation] as? NSNumber)?.intValue ?? 1, 1)
        // Displayed 1500x2400, so the canvas height sets the cover scale (0.8).
        let size = try pixelSize(prepared)
        XCTAssertEqual(size.width, 1200, accuracy: 2)
        XCTAssertEqual(size.height, 1920, accuracy: 2)

        // Orientation 6 turns the stored top edge to the right-hand side.
        let reader = try XCTUnwrap(CGImageSourceCreateWithURL(prepared as CFURL, nil))
        let image = try XCTUnwrap(CGImageSourceCreateImageAtIndex(reader, 0, nil))
        let left = try color(image, x: image.width / 8, y: image.height / 2)
        let right = try color(image, x: image.width * 7 / 8, y: image.height / 2)
        XCTAssertGreaterThan(left.blue, 200); XCTAssertLessThan(left.red, 60)
        XCTAssertGreaterThan(right.red, 200); XCTAssertLessThan(right.blue, 60)
    }
}
#endif
