import SwiftUI
import UIKit
import XCTest
@testable import Kria

/// KRI-826: the creator's own slide text survives Save/reopen byte for byte, the 120 code-point cap never
/// splits a character, and the exported slide draws a long authored line inside the frame.
///
/// Ways this can fail, written before the code:
/// 1. The 120 code-point cap cuts an emoji ZWJ sequence (or a flag, or a letter's accent) in half.
/// 2. A typed line break, "’", Turkish letters or emoji change on the way through the draft JSON.
/// 3. A long authored line (`wrap_lines == false`) in the slide export runs off both sides.
@MainActor final class CreatorTextFitTests: XCTestCase {
    private static let long = "Bugün İstanbul’da ılık bir akşam 🌅 şimdi Kadıköy’de çay içiyoruz ☕️ 👨‍👩‍👧 50% {off}: a\\b"

    func testTheCapKeepsWholeCharactersOnly() {
        let family = "👨‍👩‍👧"  // 5 code points, one character
        let text = String(repeating: "a", count: 118) + family
        let clipped = SlidePostTextElement.clipped(text)
        XCTAssertEqual(clipped, String(repeating: "a", count: 118))
        XCTAssertEqual(SlidePostTextElement.clipped(Self.long), Self.long)
        let accented = String(repeating: "a", count: 119) + "e\u{0301}"
        XCTAssertEqual(SlidePostTextElement.clipped(accented), String(repeating: "a", count: 119))
        XCTAssertLessThanOrEqual(SlidePostTextElement.clipped(String(repeating: family, count: 30)).unicodeScalars.count, 120)
    }

    func testTypedTextRoundTripsThroughSaveAndReopenByteForByte() throws {
        let typed = "Julio’s 🔥 asado\nchimichurri + salsa criolla 👨‍👩‍👧\n\nKadıköy’de çay ☕️"
        var element = SlidePostTextElement(id: "t1", text: typed)
        element.extra[SlidePostTextElement.wrapLinesKey] = .bool(false)
        var slide = SlidePostSlide(id: "slide-1", assetID: "asset-1", kind: "image")
        var edits = SlidePostEdits(); edits.setTexts([element]); slide.edits = edits
        let draft = SlidePostDraft(version: 1, platformProfile: "instagram_carousel", slides: [slide])
        let reopened = try JSONDecoder().decode(SlidePostDraft.self, from: JSONEncoder().encode(draft))
        let text = try XCTUnwrap(reopened.slides.first?.edits?.effectiveTexts.first)
        XCTAssertEqual(Data(text.text.utf8), Data(typed.utf8))
        XCTAssertFalse(text.wrapsLines)
    }

    func testTheExportedSlideKeepsALongAuthoredLineInsideTheFrame() throws {
        var element = SlidePostTextElement(id: "t1", text: Self.long)
        element.sizePx = 72
        element.extra[SlidePostTextElement.wrapLinesKey] = .bool(false)
        let pixels = CGSize(width: 1080, height: 1350)
        let image = try XCTUnwrap(SlidePostOnDeviceRender.textLayer([element], pixels: pixels)?.cgImage)
        let width = image.width, height = image.height
        var alpha = [UInt8](repeating: 0, count: width * height)
        let context = try XCTUnwrap(CGContext(data: &alpha, width: width, height: height, bitsPerComponent: 8,
            bytesPerRow: width, space: CGColorSpaceCreateDeviceGray(), bitmapInfo: CGImageAlphaInfo.alphaOnly.rawValue))
        context.draw(image, in: CGRect(x: 0, y: 0, width: width, height: height))
        let inked = (0..<width).filter { x in (0..<height).contains { alpha[$0 * width + x] > 16 } }
        let rows = (0..<height).filter { y in (0..<width).contains { alpha[y * width + $0] > 16 } }
        let left = try XCTUnwrap(inked.first), right = try XCTUnwrap(inked.last)
        // Inside the 5% side margins (the safe width is 0.9 x W), so never clipped at either edge.
        XCTAssertGreaterThan(left, Int(0.04 * Double(width)))
        XCTAssertLessThan(right, Int(0.96 * Double(width)))
        // It wrapped onto several rows rather than shrinking: one row would be about one line tall.
        XCTAssertGreaterThan((rows.last ?? 0) - (rows.first ?? 0), 72 * 3)
    }
}
