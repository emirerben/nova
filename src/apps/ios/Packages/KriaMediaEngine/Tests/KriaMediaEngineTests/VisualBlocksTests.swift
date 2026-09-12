import XCTest
@testable import KriaMediaEngine
#if canImport(CoreImage)
import CoreImage
#endif

final class VisualBlocksTests: XCTestCase {
    func testParentWindowFadesDoNotRestartAtEachMontageShot() throws {
        let placement = VisualMediaPlacement(order: 1, windowStart: 2, windowEnd: 5, fadeIn: true, fadeOut: true)
        try placement.validate()
        XCTAssertEqual(placement.alpha(at: 2), 0)
        XCTAssertEqual(placement.alpha(at: 2.075), 0.5, accuracy: 0.0001)
        XCTAssertEqual(placement.alpha(at: 3), 1)
        XCTAssertEqual(placement.alpha(at: 4.925), 0.5, accuracy: 0.0001)
    }
    func testInvalidPlacementIsRejected() {
        XCTAssertThrowsError(try VisualMediaPlacement(order: 1, zoom: .nan, windowStart: 0, windowEnd: 2).validate())
        XCTAssertThrowsError(try VisualMediaPlacement(order: 1, windowStart: 2, windowEnd: 1).validate())
    }
    func testLegacyRecipeOmitsVisualFields() throws {
        let data = try RecipeJSON.encode(EditRecipe())
        let object = try XCTUnwrap(JSONSerialization.jsonObject(with: data) as? [String: Any])
        XCTAssertNil(object["visual_fills"])
        XCTAssertEqual(try RecipeJSON.decode(data).visualFills, [])
    }
#if canImport(CoreImage)
    func testContainPreservesSourceAspectAndCoverFillsCanvas() {
        let source = CIImage(color: .white).cropped(to: CGRect(x: 0, y: 0, width: 200, height: 100))
        let canvas = CGRect(x: 0, y: 0, width: 100, height: 200)
        let contain = VisualMediaPlacement(order: 1, contain: true, windowStart: 0, windowEnd: 2)
        XCTAssertEqual(contain.position(source, preferred: .identity, canvas: canvas, time: 0, clipStart: 0, clipEnd: 2).extent, CGRect(x: 0, y: 75, width: 100, height: 50))
        let cover = VisualMediaPlacement(order: 1, windowStart: 0, windowEnd: 2)
        XCTAssertEqual(cover.position(source, preferred: .identity, canvas: canvas, time: 0, clipStart: 0, clipEnd: 2).extent, CGRect(x: -150, y: 0, width: 400, height: 200))
    }
#endif
}
