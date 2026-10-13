import UIKit
import UniformTypeIdentifiers
import XCTest
@testable import Kria

/// KRI-647: the clipboard loader behind the Paste control. The ways it can fail a creator:
/// a transparent Strava PNG comes back flattened or re-encoded, the run summary text beats the
/// picture, or whitespace/oversize text becomes an empty or huge text element.
@MainActor
final class PasteboardIntakeTests: XCTestCase {
    private func transparentPNG() throws -> Data {
        let renderer = UIGraphicsImageRenderer(size: CGSize(width: 20, height: 20))
        let image = renderer.image { context in
            UIColor.clear.setFill()
            context.fill(CGRect(x: 0, y: 0, width: 20, height: 20))
            UIColor.red.setFill()
            context.fill(CGRect(x: 10, y: 10, width: 10, height: 10))
        }
        return try XCTUnwrap(image.pngData())
    }

    private func provider(png data: Data) -> NSItemProvider {
        NSItemProvider(item: data as NSData, typeIdentifier: UTType.png.identifier)
    }

    func testTransparentPNGStaysAPNGWithItsAlpha() async throws {
        let content = await PasteboardIntake.load(from: [provider(png: try transparentPNG())])
        guard case .image(let url) = content else { return XCTFail("Expected an image, got \(String(describing: content))") }
        defer { try? FileManager.default.removeItem(at: url) }
        XCTAssertEqual(url.pathExtension.lowercased(), "png")
        let data = try Data(contentsOf: url)
        XCTAssertEqual(Array(data.prefix(4)), [0x89, 0x50, 0x4E, 0x47], "still PNG bytes, not a JPEG re-encode")
        let cg = try XCTUnwrap(UIImage(data: data)?.cgImage)
        XCTAssertNotEqual(cg.alphaInfo, .none)
        XCTAssertNotEqual(cg.alphaInfo, .noneSkipLast)
        XCTAssertNotEqual(cg.alphaInfo, .noneSkipFirst)
    }

    func testImageWinsWhenStravaAlsoPutsTheRunSummaryOnTheClipboard() async throws {
        let text = NSItemProvider(object: "5.2 km in 28:14" as NSString)
        let content = await PasteboardIntake.load(from: [text, provider(png: try transparentPNG())])
        guard case .image(let url) = content else { return XCTFail("Image must beat text, got \(String(describing: content))") }
        try? FileManager.default.removeItem(at: url)
    }

    func testTextIsTrimmedAndCapped() async {
        let long = "  " + String(repeating: "a", count: PasteboardIntake.maxTextLength + 40) + "\n"
        let content = await PasteboardIntake.load(from: [NSItemProvider(object: long as NSString)])
        XCTAssertEqual(content, .text(String(repeating: "a", count: PasteboardIntake.maxTextLength)))
    }

    func testBlankTextAndEmptyClipboardPasteNothing() async {
        XCTAssertNil(PasteboardIntake.normalizedText(" \n\t "))
        let blank = await PasteboardIntake.load(from: [NSItemProvider(object: "   " as NSString)])
        XCTAssertNil(blank)
        let empty = await PasteboardIntake.load(from: [])
        XCTAssertNil(empty)
    }
}
