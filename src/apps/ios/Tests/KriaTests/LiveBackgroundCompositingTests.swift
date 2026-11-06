import CoreImage
import XCTest
@testable import Kria

/// KRI-1119 failure modes written before the code:
/// 1. the curve is a SOFT ramp: nothing below 0.35, full at 0.65, never a hard cut and never decreasing;
/// 2. a mask read through colour management is linearised and the curve acts on the wrong numbers, so a half-confident
///    pixel must blend half and half (not 20/80);
/// 3. corners of an empty mask are the background, the centre of a full mask is the camera;
/// 4. a failed matte shows the raw camera frame, never the background alone and never a black frame;
/// 5. mirroring flips the camera and its mask together but never the background.
final class LiveBackgroundCompositingTests: XCTestCase {
    private let canvas = CGSize(width: 8, height: 8)
    private let context = LiveBackgroundCompositor.makeContext()

    private func solid(_ r: CGFloat, _ g: CGFloat, _ b: CGFloat, size: CGSize) -> CIImage {
        CIImage(color: CIColor(red: r, green: g, blue: b)).cropped(to: CGRect(origin: .zero, size: size))
    }

    /// Same type Vision hands back: an 8-bit single-component pixel buffer.
    private func mask(width: Int = 8, height: Int = 8, _ value: (Int, Int) -> UInt8) -> CVPixelBuffer {
        var buffer: CVPixelBuffer?
        XCTAssertEqual(CVPixelBufferCreate(nil, width, height, kCVPixelFormatType_OneComponent8,
                                           [kCVPixelBufferIOSurfacePropertiesKey: [:]] as CFDictionary, &buffer), kCVReturnSuccess)
        let pixels = buffer!
        CVPixelBufferLockBaseAddress(pixels, [])
        defer { CVPixelBufferUnlockBaseAddress(pixels, []) }
        let base = CVPixelBufferGetBaseAddress(pixels)!.assumingMemoryBound(to: UInt8.self)
        let stride = CVPixelBufferGetBytesPerRow(pixels)
        for y in 0..<height { for x in 0..<width { base[y * stride + x] = value(x, y) } }
        return pixels
    }

    /// `data: true` reads the raw working values (a mask is data, not a picture, so no sRGB encoding).
    private func pixel(_ image: CIImage, x: Int, y: Int, data: Bool = false) -> (r: Int, g: Int, b: Int) {
        var rgba = [UInt8](repeating: 0, count: 4)
        context.render(image, toBitmap: &rgba, rowBytes: 4, bounds: CGRect(x: x, y: y, width: 1, height: 1),
                       format: .RGBA8, colorSpace: data ? nil : CGColorSpace(name: CGColorSpace.sRGB))
        return (Int(rgba[0]), Int(rgba[1]), Int(rgba[2]))
    }

    // MARK: Curve

    func testCurveIsZeroAtLowFullAtHighAndNeverDecreasing() {
        XCTAssertEqual(SpeakerMatteCurve.alpha(0.35), 0, accuracy: 0.0001)
        XCTAssertEqual(SpeakerMatteCurve.alpha(0.65), 1, accuracy: 0.0001)
        XCTAssertEqual(SpeakerMatteCurve.alpha(0.5), 0.5, accuracy: 0.0001)
        XCTAssertEqual(SpeakerMatteCurve.alpha(0), 0)
        XCTAssertEqual(SpeakerMatteCurve.alpha(1), 1)
        var previous: Float = 0
        for i in 0...255 {
            let value = SpeakerMatteCurve.alpha(Float(i) / 255)
            XCTAssertGreaterThanOrEqual(value, previous, "alpha dropped at \(i)")
            previous = value
        }
    }

    func testTheCoreImageCurveMatchesTheFunctionOnEveryMaskValue() {
        var worst = 0
        var previous = 0
        for i in stride(from: 0, through: 255, by: 5) {
            let alpha = LiveBackgroundCompositor.softAlpha(LiveBackgroundCompositor.maskImage(mask { _, _ in UInt8(i) }))
            let value = pixel(alpha, x: 3, y: 3, data: true).r
            let expected = Int((SpeakerMatteCurve.alpha(Float(i) / 255) * 255).rounded())
            worst = max(worst, abs(value - expected))
            XCTAssertGreaterThanOrEqual(value, previous, "the rendered curve dropped at \(i)")
            previous = value
            if i <= 89 { XCTAssertEqual(value, 0, accuracy: 1, "below 0.35 the mask must be fully transparent (\(i))") }
            if i >= 166 { XCTAssertEqual(value, 255, accuracy: 1, "above 0.65 the mask must be fully opaque (\(i))") }
        }
        XCTAssertLessThanOrEqual(worst, 3, "the lookup image must reproduce the smoothstep within 3/255")
    }

    // MARK: Compositing

    func testCornersAreTheBackgroundAndTheCentreIsTheCamera() {
        let person = mask { x, y in (2...5).contains(x) && (2...5).contains(y) ? 255 : 0 }
        let out = LiveBackgroundCompositor.compose(frame: solid(1, 0, 0, size: canvas), mask: LiveBackgroundCompositor.maskImage(person),
                                                   background: solid(0, 0, 1, size: canvas), canvas: canvas, mirrored: false)
        for corner in [(0, 0), (7, 0), (0, 7), (7, 7)] {
            let p = pixel(out, x: corner.0, y: corner.1)
            XCTAssertLessThan(p.r, 12, "corner \(corner) must not show the camera"); XCTAssertGreaterThan(p.b, 243)
        }
        let center = pixel(out, x: 4, y: 4)
        XCTAssertGreaterThan(center.r, 243); XCTAssertLessThan(center.b, 12)
    }

    func testAHalfConfidentPixelBlendsHalfAndHalf() {
        let out = LiveBackgroundCompositor.compose(frame: solid(1, 0, 0, size: canvas),
                                                   mask: LiveBackgroundCompositor.maskImage(mask { _, _ in 128 }),
                                                   background: solid(0, 0, 1, size: canvas), canvas: canvas, mirrored: false)
        let p = pixel(out, x: 4, y: 4)
        // 50% in linear light is 188 in sRGB. A mask that was colour-managed would give a visibly different mix.
        XCTAssertEqual(p.r, 188, accuracy: 10); XCTAssertEqual(p.b, 188, accuracy: 10)
    }

    func testAMaskAtTheLowThresholdKeepsNoCamera() {
        let out = LiveBackgroundCompositor.compose(frame: solid(1, 0, 0, size: canvas),
                                                   mask: LiveBackgroundCompositor.maskImage(mask { _, _ in 89 }),
                                                   background: solid(0, 0, 1, size: canvas), canvas: canvas, mirrored: false)
        XCTAssertLessThan(pixel(out, x: 4, y: 4).r, 12, "confidence under 0.35 is background, not a faint ghost of the person")
    }

    func testAMaskOfADifferentSizeIsStretchedOverTheWholeFrame() {
        let tall = mask(width: 3, height: 4) { x, _ in x == 1 ? 255 : 0 }
        let out = LiveBackgroundCompositor.compose(frame: solid(1, 0, 0, size: canvas), mask: LiveBackgroundCompositor.maskImage(tall),
                                                   background: solid(0, 0, 1, size: canvas), canvas: canvas, mirrored: false)
        XCTAssertLessThan(pixel(out, x: 0, y: 4).r, 12, "left third is background")
        XCTAssertGreaterThan(pixel(out, x: 4, y: 4).r, 243, "middle third is the person")
        XCTAssertLessThan(pixel(out, x: 7, y: 4).r, 12, "right third is background")
    }

    func testNoMaskShowsTheRawCameraFrame() {
        let out = LiveBackgroundCompositor.compose(frame: solid(1, 0, 0, size: canvas), mask: nil,
                                                   background: solid(0, 0, 1, size: canvas), canvas: canvas, mirrored: false)
        for point in [(0, 0), (4, 4), (7, 7)] {
            let p = pixel(out, x: point.0, y: point.1)
            XCTAssertGreaterThan(p.r, 243); XCTAssertLessThan(p.b, 12)
        }
    }

    func testMirroringFlipsTheCameraAndItsMaskButNotTheBackground() {
        // Person on the left half, a left-to-right red/green camera gradient, a blue/yellow background.
        let person = mask { x, _ in x < 4 ? 255 : 0 }
        let camera = solid(1, 0, 0, size: CGSize(width: 4, height: 8))
            .composited(over: solid(0, 1, 0, size: canvas))
        let background = solid(0, 0, 1, size: CGSize(width: 4, height: 8))
            .composited(over: solid(1, 1, 0, size: canvas))
        let plain = LiveBackgroundCompositor.compose(frame: camera, mask: LiveBackgroundCompositor.maskImage(person),
                                                     background: background, canvas: canvas, mirrored: false)
        let mirrored = LiveBackgroundCompositor.compose(frame: camera, mask: LiveBackgroundCompositor.maskImage(person),
                                                        background: background, canvas: canvas, mirrored: true)
        XCTAssertGreaterThan(pixel(plain, x: 1, y: 4).r, 243, "person (red camera) on the left")
        XCTAssertGreaterThan(pixel(plain, x: 6, y: 4).r, 243, "yellow background on the right")
        XCTAssertGreaterThan(pixel(plain, x: 6, y: 4).g, 243)
        XCTAssertGreaterThan(pixel(mirrored, x: 6, y: 4).r, 243, "mirrored: the person (red camera) moves to the right")
        XCTAssertLessThan(pixel(mirrored, x: 6, y: 4).g, 12)
        XCTAssertGreaterThan(pixel(mirrored, x: 1, y: 4).b, 0, "the background does NOT mirror: its blue half stays on the left")
        XCTAssertLessThan(pixel(mirrored, x: 1, y: 4).r, 12)
    }

    // MARK: Selfie orientation (device finding: the live preview was flipped against the plain preview)

    /// Left half red, right half blue.
    private var leftRedRightBlue: CIImage {
        solid(0, 0, 1, size: canvas).composited(over: solid(0, 0, 0, size: canvas))
            .cropped(to: CGRect(x: 4, y: 0, width: 4, height: 8))
            .composited(over: solid(1, 0, 0, size: CGSize(width: 4, height: 8)))
    }

    func testMirroredTrueSwapsLeftAndRightAndFalseKeepsThem() {
        let person = LiveBackgroundCompositor.maskImage(mask { _, _ in 255 })
        let green = solid(0, 1, 0, size: canvas)
        let plain = LiveBackgroundCompositor.compose(frame: leftRedRightBlue, mask: person, background: green, canvas: canvas, mirrored: false)
        XCTAssertGreaterThan(pixel(plain, x: 1, y: 4).r, 243, "not mirrored: red stays on the left")
        XCTAssertGreaterThan(pixel(plain, x: 6, y: 4).b, 243)
        let mirrored = LiveBackgroundCompositor.compose(frame: leftRedRightBlue, mask: person, background: green, canvas: canvas, mirrored: true)
        XCTAssertGreaterThan(pixel(mirrored, x: 1, y: 4).b, 243, "mirrored: blue moves to the left")
        XCTAssertGreaterThan(pixel(mirrored, x: 6, y: 4).r, 243)
        let rawMirrored = LiveBackgroundCompositor.compose(frame: leftRedRightBlue, mask: nil, background: green, canvas: canvas, mirrored: true)
        XCTAssertGreaterThan(pixel(rawMirrored, x: 1, y: 4).b, 243, "the raw fallback frame mirrors the same way")
    }

    func testTheMaskFollowsTheMirrorSoThePersonStaysUnderTheirOwnPixels() {
        // Person on the left of the sensor image (red), mask on the left. Mirrored, both move right together.
        let left = LiveBackgroundCompositor.maskImage(mask { x, _ in x < 4 ? 255 : 0 })
        let green = solid(0, 1, 0, size: canvas)
        let out = LiveBackgroundCompositor.compose(frame: leftRedRightBlue, mask: left, background: green, canvas: canvas, mirrored: true)
        XCTAssertGreaterThan(pixel(out, x: 6, y: 4).r, 243, "the red person is on the right and still opaque")
        XCTAssertGreaterThan(pixel(out, x: 1, y: 4).g, 243, "the left is the background, not the blue half of the camera")
    }

    func testComposeNeverFlipsVerticallyWhateverTheMirror() {
        // Core Image's origin is bottom-left: top half red means high y is red.
        let topRed = solid(1, 0, 0, size: CGSize(width: 8, height: 4)).transformed(by: CGAffineTransform(translationX: 0, y: 4))
            .composited(over: solid(0, 0, 1, size: canvas))
        let person = LiveBackgroundCompositor.maskImage(mask { _, _ in 255 })
        for mirrored in [false, true] {
            let out = LiveBackgroundCompositor.compose(frame: topRed, mask: person, background: solid(0, 1, 0, size: canvas), canvas: canvas, mirrored: mirrored)
            XCTAssertGreaterThan(pixel(out, x: 4, y: 6).r, 243, "top stays top (mirrored \(mirrored))")
            XCTAssertGreaterThan(pixel(out, x: 4, y: 1).b, 243, "bottom stays bottom (mirrored \(mirrored))")
        }
    }

    func testThePreviewMirrorsExactlyWhenTheConnectionDoesNot() {
        XCTAssertTrue(LivePreviewOrientation.previewMirrored(connectionIsMirrored: false), "an unmirrored buffer needs the selfie flip")
        XCTAssertFalse(LivePreviewOrientation.previewMirrored(connectionIsMirrored: true), "a buffer that arrives mirrored must not be flipped back")
    }

    func testCoverFitFillsTheCanvasForALandscapeBackground() {
        let wide = solid(0, 1, 0, size: CGSize(width: 64, height: 16))
        let fitted = LiveBackgroundCompositor.coverFit(wide, into: CGSize(width: 9, height: 16))
        XCTAssertEqual(fitted.extent, CGRect(x: 0, y: 0, width: 9, height: 16))
        XCTAssertGreaterThan(pixel(fitted, x: 0, y: 0).g, 243)
        XCTAssertGreaterThan(pixel(fitted, x: 8, y: 15).g, 243)
    }
}
