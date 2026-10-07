import CoreImage
import Foundation

/// macOS fixture runner. Compile beside a Looks/ copy of the app resources;
/// pass a reference directory produced by generate-slide-look-cubes.py.
@main enum SlideLookFixture {
    enum Failure: Error { case missingInput, invalidPixels, geometry, nondeterministic }

    static func main() throws {
        guard CommandLine.arguments.count == 2 else { throw Failure.missingInput }
        let root = URL(fileURLWithPath: CommandLine.arguments[1])
        guard let source = CIImage(contentsOf: root.appending(path: "source.png"), options: [.colorSpace: NSNull()]),
              let srgb = CGColorSpace(name: CGColorSpace.sRGB) else { throw Failure.missingInput }
        let context = CIContext(options: [.workingColorSpace: NSNull(), .outputColorSpace: NSNull()])
        let extent = source.extent
        func pixels(_ image: CIImage) -> [UInt8] {
            var bytes = [UInt8](repeating: 0, count: Int(extent.width * extent.height) * 4)
            context.render(image, toBitmap: &bytes, rowBytes: Int(extent.width) * 4, bounds: extent, format: .RGBA8, colorSpace: nil)
            return bytes
        }
        var report: [String: [String: Double]] = [:]
        for name in ["olive_film", "smoky_split_tone", "golden_hour", "faded_analog", "stadium_diffusion"] {
            let output = try SlidePostLookRenderer.apply(source, preset: name)
            guard output.extent == extent else { throw Failure.geometry }
            let actual = pixels(output)
            guard actual == pixels(try SlidePostLookRenderer.apply(source, preset: name)) else { throw Failure.nondeterministic }
            guard stride(from: 3, to: actual.count, by: 4).allSatisfy({ actual[$0] == 255 }),
                  let reference = CIImage(contentsOf: root.appending(path: "full-\(name).png"), options: [.colorSpace: NSNull()]),
                  reference.extent == extent else { throw Failure.invalidPixels }
            let expected = pixels(reference)
            var histogram = [Int](repeating: 0, count: 256)
            var sum = 0, count = 0
            for index in actual.indices where index % 4 != 3 {
                let delta = abs(Int(actual[index]) - Int(expected[index]))
                histogram[delta] += 1; sum += delta; count += 1
            }
            var cumulative = 0, p95 = 0
            for delta in histogram.indices {
                cumulative += histogram[delta]
                if Double(cumulative) >= Double(count) * 0.95 { p95 = delta; break }
            }
            report[name] = ["mae": Double(sum) / Double(count), "p95": Double(p95)]
            try context.writePNGRepresentation(of: output, to: root.appending(path: "native-\(name).png"), format: .RGBA8, colorSpace: srgb)
            print("\(name): MAE \(Double(sum) / Double(count)), P95 \(p95); repeatability and coverage passed")
        }
        try JSONSerialization.data(withJSONObject: report, options: [.prettyPrinted, .sortedKeys])
            .write(to: root.appending(path: "native-parity-metrics.json"))
    }
}
