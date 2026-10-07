import CoreImage
import XCTest
@testable import Kria

final class SlidePostLookRendererTests: XCTestCase {
    private let extent = CGRect(x: 0, y: 0, width: 96, height: 64)

    // Failure cases captured before implementation: neutral must be a true bypass;
    // every wire-format preset must be recognised; unknown values must fail rather
    // than exporting an ungraded slide; and each authored look must affect pixels.
    func testSupportsOnlyWireFormatLookPresets() {
        for preset in ["none", "stadium_diffusion", "olive_film", "smoky_split_tone", "golden_hour", "faded_analog"] {
            XCTAssertTrue(SlidePostLookRenderer.supports(preset), preset)
        }
        XCTAssertFalse(SlidePostLookRenderer.supports("warm"))
        XCTAssertFalse(SlidePostLookRenderer.supports(""))
    }

    func testNoneIsExactBypass() throws {
        let source = fixture()
        let result = try SlidePostLookRenderer.apply(source, preset: "none")
        XCTAssertEqual(result, source)
    }

    func testUnknownLookFailsVisibly() {
        XCTAssertThrowsError(try SlidePostLookRenderer.apply(fixture(), preset: "unknown")) { error in
            XCTAssertEqual(error as? SlidePostLookRenderer.Error, .unknownPreset("unknown"))
        }
    }

    func testAuthoredLooksChangeFixturePixels() throws {
        let source = fixture()
        let baseline = try pixels(source)
        for preset in ["stadium_diffusion", "olive_film", "smoky_split_tone", "golden_hour", "faded_analog"] {
            let actual = try pixels(SlidePostLookRenderer.apply(source, preset: preset))
            XCTAssertNotEqual(actual, baseline, "\(preset) unexpectedly bypassed the grade")
        }
    }

    func testRepeatedApplicationsProduceIdenticalPixels() throws {
        // Independent image graphs must agree: preview and export can be built
        // at different times, and temporal randomGenerator noise breaks this.
        for preset in ["stadium_diffusion", "olive_film", "smoky_split_tone", "golden_hour", "faded_analog"] {
            let first = try pixels(SlidePostLookRenderer.apply(fixture(), preset: preset))
            let second = try pixels(SlidePostLookRenderer.apply(fixture(), preset: preset))
            XCTAssertEqual(first, second, "\(preset) changed across identical renders")
        }
    }

    func testEveryLookPreservesRequestedExtentAndOpaqueCoverage() throws {
        // Both ordinary and translated crops must remain opaque after the
        // stadium half-resolution path is interpolated back to the canvas.
        for offset in [CGAffineTransform.identity, CGAffineTransform(translationX: 17, y: -23)] {
            let source = fixture().transformed(by: offset)
            for preset in ["stadium_diffusion", "olive_film", "smoky_split_tone", "golden_hour", "faded_analog"] {
                let actual = try SlidePostLookRenderer.apply(source, preset: preset)
                XCTAssertEqual(actual.extent, source.extent, preset)
                let normalized = actual.transformed(by: offset.inverted())
                let bytes = try pixels(normalized)
                XCTAssertTrue(stride(from: 3, to: bytes.count, by: 4).allSatisfy { bytes[$0] == 255 }, preset)
            }
        }
    }

    func testFadedAnalogMaskDarkensFlatFootageAtEdges() throws {
        // Average patches so seeded grain cannot make a single-pixel assertion
        // pass or fail by chance. The authored mask is subtle (~2% at corners).
        let image = try SlidePostLookRenderer.apply(CIImage(color: .white).cropped(to: extent), preset: "faded_analog")
        let values = try pixels(image)
        func average(_ rect: CGRect) -> Double {
            var sum = 0.0
            for y in Int(rect.minY)..<Int(rect.maxY) {
                for x in Int(rect.minX)..<Int(rect.maxX) {
                    let i = (y * Int(extent.width) + x) * 4
                    sum += Double(values[i]) + Double(values[i+1]) + Double(values[i+2])
                }
            }
            return sum / (Double(rect.width * rect.height) * 3)
        }
        let centre = average(CGRect(x: 42, y: 26, width: 12, height: 12))
        XCTAssertLessThan(average(CGRect(x: 0, y: 0, width: 12, height: 12)), centre)
        // Production FFmpeg 7.1.5 converts PNG white to mask Y=235. Treating
        // white as Y=255 instead lifts this patch to ~249 and is a regression.
        XCTAssertEqual(centre, 228, accuracy: 4, "Faded mask must use production limited-range Y")
    }

    private func fixture() -> CIImage {
        let gradient = CIFilter.linearGradient()
        gradient.point0 = CGPoint(x: 0, y: 0)
        gradient.point1 = CGPoint(x: extent.width, y: extent.height)
        gradient.color0 = CIColor(red: 0.08, green: 0.25, blue: 0.80)
        gradient.color1 = CIColor(red: 0.95, green: 0.72, blue: 0.20)
        return gradient.outputImage!.cropped(to: extent)
    }

    private func pixels(_ image: CIImage) throws -> [UInt8] {
        var result = [UInt8](repeating: 0, count: Int(extent.width * extent.height * 4))
        CIContext(options: [.workingColorSpace: NSNull(), .outputColorSpace: NSNull()]).render(
            image, toBitmap: &result, rowBytes: Int(extent.width) * 4, bounds: extent, format: .RGBA8, colorSpace: nil
        )
        return result
    }
}
