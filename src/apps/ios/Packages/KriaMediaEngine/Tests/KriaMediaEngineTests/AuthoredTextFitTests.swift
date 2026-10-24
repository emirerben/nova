import CoreText
import Foundation
import XCTest
@testable import KriaMediaEngine
#if canImport(AVFoundation)

/// KRI-826: hand-edited text (`wrap_lines == false`) keeps the creator's lines but stays inside the frame,
/// through the REAL video compiler, on the same strings and with the same rows as the cloud Skia renderer
/// (`src/apps/api/tests/fixtures/kri826_authored_fit_rows.json`, re-checked on the server side by
/// `test_authored_line_fit_matches_the_recorded_cross_platform_rows`).
///
/// Ways this can fail, written before the code:
/// 1. A long authored line is drawn as one row and runs off both sides of the frame.
/// 2. iOS breaks the line differently from the cloud render, so preview and export disagree.
/// 3. A line that already fits is re-wrapped (KRI-564: the creator's lines stay as typed).
/// 4. A stale narrow `max_width_frac` re-wraps a line that fits the frame.
/// 5. Shrinking goes below a readable size, or happens where wrapping would do.
/// 6. Inline emoji or Turkish letters throw a font-fallback error or are dropped from the runs.
final class AuthoredTextFitTests: XCTestCase {
    private struct Case: Decodable {
        let text: String
        let size_px: Double
        let canvas: [Int]
        let rows: [String]
        let fitted_size_px: Double
    }
    private struct Fixture: Decodable { let font_file: String; let cases: [Case] }

    private var repo: URL {
        let root = #filePath.range(of: "/src/apps/ios/")!
        return URL(fileURLWithPath: String(#filePath[..<root.lowerBound]))
    }

    private func fixture() throws -> Fixture {
        let url = repo.appendingPathComponent("src/apps/api/tests/fixtures/kri826_authored_fit_rows.json")
        return try JSONDecoder().decode(Fixture.self, from: Data(contentsOf: url))
    }

    func testAuthoredLinesFitTheFrameWithTheCloudRows() throws {
        let fixture = try fixture()
        let font = repo.appendingPathComponent("src/apps/api/assets/fonts/\(fixture.font_file)")
        XCTAssertGreaterThanOrEqual(fixture.cases.count, 5)
        for item in fixture.cases {
            let canvas = Canvas(width: item.canvas[0], height: item.canvas[1])
            let layer = try AuthoredTextLayout.compile(id: "t", text: item.text, start: 0, end: 2,
                style: .init(fontAssetID: "font", size: item.size_px, widthFraction: 0.3,
                             color: .init(red: 1, green: 1, blue: 1, alpha: 1), wrapsLines: false),
                fontURL: font, canvas: canvas)
            // A blank authored row is drawn as a space (it still takes its baseline).
            let rows = layer.runs.map { $0.text == " " ? "" : $0.text }
            XCTAssertEqual(rows, item.rows, item.text)
            XCTAssertTrue(layer.runs.allSatisfy { $0.fontSize == item.fitted_size_px }, item.text)
            XCTAssertGreaterThanOrEqual(item.fitted_size_px, min(item.size_px, AuthoredTextFit.minimumSize))
            let graphics = try XCTUnwrap(CGDataProvider(url: font as CFURL).flatMap(CGFont.init))
            let ctFont = CTFontCreateWithGraphicsFont(graphics, item.fitted_size_px, nil, nil)
            let safe = AuthoredTextFit.safeWidth(canvasWidth: Double(canvas.width))
            for run in layer.runs {
                let line = CTLineCreateWithAttributedString(NSAttributedString(string: run.text,
                    attributes: [NSAttributedString.Key(kCTFontAttributeName as String): ctFont]))
                XCTAssertLessThanOrEqual(CTLineGetTypographicBounds(line, nil, nil, nil), safe + 0.5, run.text)
            }
            // Nothing is cut: every character of the creator's text is drawn, in order.
            XCTAssertEqual(rows.joined().filter { !$0.isWhitespace }, item.text.filter { !$0.isWhitespace })
        }
    }

    /// 7. Text dragged off-centre or edge-anchored wraps to the full 0.9 x W and runs past the frame edge.
    /// The widths are cloud `text_fit.authored_safe_width` values (`test_authored_safe_width_matches_ios`).
    func testOffCentreAuthoredTextGetsOnlyTheRoomToTheSafeMargins() {
        let cases: [(Double, String, Double)] = [(0.5, "center", 972), (0.7, "center", 540), (0.3, "left", 702),
                                                 (0.8, "right", 810), (0.15, "center", 216), (0.99, "center", 108)]
        for (x, alignment, expected) in cases {
            XCTAssertEqual(AuthoredTextFit.safeWidth(canvasWidth: 1080, xFraction: x, alignment: alignment),
                           expected, accuracy: 0.001, "\(x) \(alignment)")
        }
    }

    func testWrappingTextKeepsItsOwnBoxAndSize() throws {
        let font = repo.appendingPathComponent("src/apps/api/assets/fonts/Inter-Bold.ttf")
        let text = "chimichurri + salsa criolla ve Julio’s 🔥 asado akşam yemeği"
        let layer = try AuthoredTextLayout.compile(id: "t", text: text, start: 0, end: 2,
            style: .init(fontAssetID: "font", size: 72, widthFraction: 0.5,
                         color: .init(red: 1, green: 1, blue: 1, alpha: 1)),
            fontURL: font, canvas: Canvas(width: 1080, height: 1920))
        // `wrap_lines == true` is unchanged: its own box (0.5 x W), its own size.
        XCTAssertTrue(layer.runs.allSatisfy { $0.fontSize == 72 })
        XCTAssertGreaterThan(layer.runs.count, 2)
    }
}
#endif
