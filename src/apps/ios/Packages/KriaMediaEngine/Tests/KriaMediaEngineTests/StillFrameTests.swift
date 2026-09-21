#if canImport(CoreImage)
import CoreGraphics
import CoreImage
import Foundation
import ImageIO
import XCTest
@testable import KriaMediaEngine

/// KRI-121: main-track stills are matted and laid out once, on encoded sRGB
/// values, so they match the cloud's guided image moments pixel for pixel.
final class StillFrameTests: XCTestCase {
    private let sRGB = CGColorSpace(name: CGColorSpace.sRGB)!

    /// `pixels` is row-major RGBA from the top-left. `.last` is straight
    /// (non-premultiplied) alpha, which is how PNG stores hidden colour.
    private func image(_ pixels: [UInt8], width: Int, height: Int, alpha: CGImageAlphaInfo) throws -> CGImage {
        XCTAssertEqual(pixels.count, width * height * 4)
        let provider = try XCTUnwrap(CGDataProvider(data: Data(pixels) as CFData))
        return try XCTUnwrap(CGImage(width: width, height: height, bitsPerComponent: 8, bitsPerPixel: 32, bytesPerRow: width * 4, space: sRGB,
                                     bitmapInfo: CGBitmapInfo(rawValue: alpha.rawValue), provider: provider, decode: nil,
                                     shouldInterpolate: false, intent: .defaultIntent))
    }

    /// Row-major RGBA from the top-left, read through an 8-bit sRGB context.
    private func rgba(_ image: CGImage) throws -> [UInt8] {
        var data = [UInt8](repeating: 0, count: image.width * image.height * 4)
        try data.withUnsafeMutableBytes { bytes in
            let context = try XCTUnwrap(CGContext(data: bytes.baseAddress, width: image.width, height: image.height, bitsPerComponent: 8,
                                                  bytesPerRow: image.width * 4, space: sRGB, bitmapInfo: CGImageAlphaInfo.premultipliedLast.rawValue))
            context.draw(image, in: CGRect(x: 0, y: 0, width: image.width, height: image.height))
        }
        return data
    }

    /// Vertical colour bands, left to right, in an opaque upright image at the origin.
    private func bands(_ colors: [[UInt8]], width: Int, height: Int) throws -> CIImage {
        var pixels = [UInt8](repeating: 255, count: width * height * 4)
        for y in 0..<height {
            for x in 0..<width {
                let color = colors[x * colors.count / width]
                for channel in 0..<3 { pixels[(y * width + x) * 4 + channel] = color[channel] }
            }
        }
        return CIImage(cgImage: try image(pixels, width: width, height: height, alpha: .noneSkipLast))
    }

    /// Renders without a colour conversion and samples in top-left pixel coordinates.
    private func sampler(_ image: CIImage) throws -> (Int, Int) -> [Int] {
        XCTAssertEqual(image.extent.origin, .zero)
        let rendered = try XCTUnwrap(CIContext(options: [.workingColorSpace: sRGB]).createCGImage(image, from: image.extent, format: .RGBA8, colorSpace: sRGB))
        let bytes = try rgba(rendered), width = rendered.width
        return { x, y in (0..<3).map { Int(bytes[(y * width + x) * 4 + $0]) } }
    }

    private typealias Sample = (x: Int, y: Int, expected: [Int], name: String)

    private func assertPixel(_ actual: [Int], _ expected: [Int], tolerance: Int = 3, _ name: String, line: UInt = #line) {
        XCTAssertTrue(zip(actual, expected).allSatisfy { abs($0 - $1) <= tolerance }, "\(name): \(actual) is not \(expected)", line: line)
    }

    func testTransparentPhotoIsMattedOverBlackOnEncodedValues() throws {
        // Opaque red, half-transparent red, fully transparent with hidden white, opaque blue.
        let pixels: [UInt8] = [255, 0, 0, 255, 255, 0, 0, 128, 255, 255, 255, 0, 0, 0, 255, 255]
        let straight = try image(pixels, width: 4, height: 1, alpha: .last)
        // The same picture the way a premultiplied decoder hands it over.
        let premultiplied = try image([255, 0, 0, 255, 128, 0, 0, 128, 0, 0, 0, 0, 0, 0, 255, 255], width: 4, height: 1, alpha: .premultipliedLast)
        // And decoded from a real PNG, as the compositor reads a Visuals photo.
        let url = FileManager.default.temporaryDirectory.appendingPathComponent(UUID().uuidString + ".png")
        defer { try? FileManager.default.removeItem(at: url) }
        let destination = try XCTUnwrap(CGImageDestinationCreateWithURL(url as CFURL, "public.png" as CFString, 1, nil))
        CGImageDestinationAddImage(destination, straight, nil)
        XCTAssertTrue(CGImageDestinationFinalize(destination))
        let source = try XCTUnwrap(CGImageSourceCreateWithURL(url as CFURL, nil))
        let decoded = try XCTUnwrap(CGImageSourceCreateImageAtIndex(source, 0, nil))

        for (name, input) in [("straight", straight), ("premultiplied", premultiplied), ("png", decoded)] {
            let flat = try StillFrame.flattened(input)
            XCTAssertEqual([flat.width, flat.height], [4, 1], name)
            XCTAssertTrue([.none, .noneSkipFirst, .noneSkipLast].contains(flat.alphaInfo), "\(name) keeps alpha \(flat.alphaInfo.rawValue)")
            let bytes = try rgba(flat)
            let actual = (0..<4).map { pixel in (0..<4).map { Int(bytes[pixel * 4 + $0]) } }
            // round(255 * 128 / 255) is what PIL's alpha_composite over black gives the cloud.
            for (pixel, expected) in [[255, 0, 0, 255], [128, 0, 0, 255], [0, 0, 0, 255], [0, 0, 255, 255]].enumerated() {
                XCTAssertTrue(zip(actual[pixel], expected).allSatisfy { abs($0 - $1) <= 1 }, "\(name) pixel \(pixel): \(actual[pixel]) is not \(expected)")
            }
        }
    }

    func testOpaquePhotoIsNotRedrawn() throws {
        for alpha in [CGImageAlphaInfo.noneSkipLast, .noneSkipFirst] {
            let opaque = try image([10, 20, 30, 255, 40, 50, 60, 255], width: 2, height: 1, alpha: alpha)
            XCTAssertTrue(try StillFrame.flattened(opaque) === opaque)
        }
    }

    /// The cloud reference (`guided_story._render_image_moment`, 1200x900 source)
    /// measured the black card at (96, 268), 885x1382, with the photo 885 wide
    /// and vertically centred inside it.
    func testSupportingCardMatchesTheCloudGeometry() throws {
        let card = try StillFrame.supportingCard(try bands([[0, 255, 0]], width: 1200, height: 900), canvas: CGSize(width: 1080, height: 1920))
        XCTAssertEqual(card.extent, CGRect(x: 0, y: 0, width: 1080, height: 1920))
        let pixel = try sampler(card)
        let green = [0, 255, 0], black = [0, 0, 0]
        // The card's exact edges, read along a row and a column that only cross its black bars.
        for sample: Sample in [(95, 400, green, "left of the card"), (96, 400, black, "card left edge"),
                                       (980, 400, black, "card right edge"), (981, 400, green, "right of the card"),
                                       (100, 267, green, "above the card"), (100, 268, black, "card top edge"),
                                       (100, 1649, black, "card bottom edge"), (100, 1650, green, "below the card"),
                                       (978, 270, black, "card top-right corner"), (98, 1647, black, "card bottom-left corner")] {
            assertPixel(pixel(sample.x, sample.y), sample.expected, sample.name)
        }
        // 1200x900 fits as 885x663.75 around the card's centre row, 268 + 691.
        for sample: Sample in [(540, 959, green, "photo centre"), (98, 959, green, "photo left"), (978, 959, green, "photo right"),
                                       (540, 620, black, "bar above the photo"), (540, 634, green, "photo top"),
                                       (540, 1284, green, "photo bottom"), (540, 1298, black, "bar below the photo"),
                                       (540, 300, black, "top bar"), (540, 1600, black, "bottom bar")] {
            assertPixel(pixel(sample.x, sample.y), sample.expected, sample.name)
        }
        // A blurred cover of one flat colour is that colour, to every canvas corner.
        for (x, y) in [(0, 0), (1079, 0), (0, 1919), (1079, 1919), (540, 100), (40, 959), (1040, 959), (540, 1800)] {
            assertPixel(pixel(x, y), green, "cover at (\(x), \(y))")
        }
    }

    func testSupportingCardShowsTheWholePhotoOverABlurredCover() throws {
        let red = [255, 0, 0], blue = [0, 0, 255], black = [0, 0, 0]
        let card = try StillFrame.supportingCard(try bands([[255, 0, 0], [0, 0, 255]], width: 1200, height: 900), canvas: CGSize(width: 1080, height: 1920))
        let pixel = try sampler(card)
        // Not cropped: both halves are inside the card, out to its side edges, with bars above and below.
        for sample: Sample in [(96 + 221, 959, red, "25% across the card"), (96 + 664, 959, blue, "75% across the card"),
                                       (98, 959, red, "photo left edge"), (978, 959, blue, "photo right edge"),
                                       (96 + 221, 640, red, "photo top-left"), (96 + 664, 1278, blue, "photo bottom-right"),
                                       (96 + 221, 600, black, "bar above"), (96 + 664, 1320, black, "bar below")] {
            assertPixel(pixel(sample.x, sample.y), sample.expected, sample.name)
        }
        // The cover fills the canvas behind the card: 2560x1920 centred, so the colour seam sits at x = 540.
        assertPixel(pixel(40, 959), red, "cover left of the card")
        assertPixel(pixel(1040, 959), blue, "cover right of the card")
        assertPixel(pixel(40, 40), red, "cover top-left")
        assertPixel(pixel(1040, 1880), blue, "cover bottom-right")
        // FFmpeg blurs encoded values: the seam is mid-grey-level purple (128), where a
        // linear-light blur would give 188. One sigma (24.9 px) out it is 84% / 16%.
        let seam = pixel(540, 100)
        XCTAssertTrue((108...148).contains(seam[0]) && (108...148).contains(seam[2]) && seam[1] < 8, "seam \(seam)")
        let oneSigma = pixel(515, 100)
        XCTAssertTrue((196...232).contains(oneSigma[0]) && (24...60).contains(oneSigma[2]), "one sigma left of the seam \(oneSigma)")
        let far = pixel(540 - 100, 100)
        assertPixel(far, red, "four sigma left of the seam")
    }

    func testSupportingCardUsesEvenOffsetsOnALandscapeCanvas() throws {
        // floor(1920 * 0.82) x floor(1080 * 0.72) = 1574x777 at (173, 151), floored to even: (172, 150).
        let card = try StillFrame.supportingCard(try bands([[0, 255, 0]], width: 1200, height: 900), canvas: CGSize(width: 1920, height: 1080))
        XCTAssertEqual(card.extent, CGRect(x: 0, y: 0, width: 1920, height: 1080))
        let pixel = try sampler(card)
        let green = [0, 255, 0], black = [0, 0, 0]
        // The photo is 1036x777 here, so the bars are at the card's sides.
        for sample: Sample in [(171, 540, green, "left of the card"), (172, 540, black, "card left edge"),
                                       (1745, 540, black, "card right edge"), (1746, 540, green, "right of the card"),
                                       (180, 149, green, "above the card"), (180, 150, black, "card top edge"),
                                       (180, 926, black, "card bottom edge"), (180, 927, green, "below the card"),
                                       (959, 540, green, "photo centre"), (959, 152, green, "photo top"), (959, 924, green, "photo bottom")] {
            assertPixel(pixel(sample.x, sample.y), sample.expected, sample.name)
        }
    }

    func testSupportingCardRejectsAnEmptyPhoto() {
        XCTAssertThrowsError(try StillFrame.supportingCard(CIImage.empty(), canvas: CGSize(width: 1080, height: 1920)))
        XCTAssertThrowsError(try StillFrame.supportingCard(CIImage(color: .red).cropped(to: CGRect(x: 0, y: 0, width: 4, height: 4)), canvas: .zero))
    }
}
#endif
