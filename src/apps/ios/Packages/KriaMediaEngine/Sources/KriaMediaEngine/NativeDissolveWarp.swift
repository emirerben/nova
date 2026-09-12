#if canImport(AVFoundation)
import Foundation
import CoreImage

/// Retained mathematical displacement map; independent of text effect activation.
final class NativeDissolveWarp: @unchecked Sendable {
    let map: CIImage
    let extent: CGRect
    let bitmapBytes: Int
    private let kernel: CIKernel

    init(width: Int, height: Int, seed: UInt32, maxBitmapBytes: Int, preset: DissolveTiming.Preset = .text) throws {
        guard (1...4096).contains(width), (1...4096).contains(height),
              width * height * 4 <= maxBitmapBytes else { throw NativePreviewFeatureError("NativeDissolveWarp-14") }
        extent = CGRect(x: 0, y: 0, width: width, height: height)
        bitmapBytes = width * height * 4
        let noise = DissolveNoise(seed: seed)
        var bytes = [UInt8](repeating: 0, count: bitmapBytes)
        for y in 0..<height {
            for x in 0..<width {
                let pixel = noise.textMapPixel(x: x, y: y, frequency: preset == .media ? 0.005 : 0.004)
                let offset = (y * width + x) * 4
                for channel in 0..<4 { bytes[offset + channel] = UInt8(min(255, max(0, (pixel[channel] * 255).rounded()))) }
            }
        }
        map = CIImage(bitmapData: Data(bytes), bytesPerRow: width * 4,
                      size: CGSize(width: width, height: height), format: .RGBA8, colorSpace: nil)
        // Metal-backed Core Image runs the warp on the GPU. No per-frame CPU
        // image readback, noise generation, or shader compilation.
        let kernels = try CIKernel.kernels(withMetalString: """
        #include <CoreImage/CoreImage.h>
        using namespace metal;
        extern "C" { namespace coreimage {
        [[stitchable]] float4 kriaDissolveWarp(sampler source, sampler field, float scale, float growth, float2 center, destination dest) {
            float2 p = (dest.coord() - center) / growth + center;
            float4 d = field.sample(field.transform(p));
            float2 vector = d.a > 0.0 ? d.rg / d.a - 0.5 : float2(-0.5);
            float2 location = floor(p + vector * float2(scale, -scale)) + 0.5;
            return source.sample(source.transform(location));
        }
        }}
        """)
        guard let kernel = kernels.first else { throw NativePreviewFeatureError("NativeDissolveWarp-43") }
        self.kernel = kernel
    }

    func image(source: CIImage, scale: Double, growth: Double = 1) throws -> CIImage {
        guard scale.isFinite, (0...2000).contains(scale), growth.isFinite, (1...1.1).contains(growth) else { throw RecipeError.invalidTimeline }
        let extent = self.extent
        guard let image = kernel.apply(extent: extent, roiCallback: { index, rect in
            let center = CGPoint(x: extent.midX, y: extent.midY)
            let sampleRect = CGRect(x: (rect.minX - center.x) / growth + center.x,
                                    y: (rect.minY - center.y) / growth + center.y,
                                    width: rect.width / growth, height: rect.height / growth)
            return index == 0 ? sampleRect.insetBy(dx: -scale / 2 - 1, dy: -scale / 2 - 1) : sampleRect.insetBy(dx: -1, dy: -1)
        }, arguments: [source.cropped(to: extent), map, scale, growth, CIVector(x: extent.midX, y: extent.midY)]) else { throw NativePreviewFeatureError("NativeDissolveWarp-56") }
        return image
    }
}
#endif
