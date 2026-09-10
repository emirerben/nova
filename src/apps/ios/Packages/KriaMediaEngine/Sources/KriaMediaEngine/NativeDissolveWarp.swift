#if canImport(AVFoundation)
import Foundation
import CoreImage

/// Retained mathematical displacement map; independent of text effect activation.
final class NativeDissolveWarp: @unchecked Sendable {
    let map: CIImage
    let extent: CGRect
    let bitmapBytes: Int
    private let kernel: CIKernel

    init(width: Int, height: Int, seed: UInt32, maxBitmapBytes: Int) throws {
        guard (1...4096).contains(width), (1...4096).contains(height),
              width * height * 4 <= maxBitmapBytes else { throw MediaEngineError.unsupportedCapability }
        extent = CGRect(x: 0, y: 0, width: width, height: height)
        bitmapBytes = width * height * 4
        let noise = DissolveNoise(seed: seed)
        var bytes = [UInt8](repeating: 0, count: bitmapBytes)
        for y in 0..<height {
            for x in 0..<width {
                let pixel = noise.textMapPixel(x: x, y: y)
                let offset = ((height - 1 - y) * width + x) * 4
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
        [[stitchable]] float4 kriaDissolveWarp(sampler source, sampler field, float scale, destination dest) {
            float2 p = dest.coord();
            float4 d = field.sample(field.transform(p));
            float2 vector = d.a > 0.0 ? d.rg / d.a - 0.5 : float2(-0.5);
            float2 location = floor(p + vector * scale) + 0.5;
            return source.sample(source.transform(location));
        }
        }}
        """)
        guard let kernel = kernels.first else { throw MediaEngineError.unsupportedCapability }
        self.kernel = kernel
    }

    func image(source: CIImage, scale: Double) throws -> CIImage {
        guard scale.isFinite, (0...2000).contains(scale) else { throw RecipeError.invalidTimeline }
        guard let image = kernel.apply(extent: extent, roiCallback: { index, rect in
            index == 0 ? rect.insetBy(dx: -scale / 2 - 1, dy: -scale / 2 - 1) : rect
        }, arguments: [source.cropped(to: extent), map, scale]) else { throw MediaEngineError.unsupportedCapability }
        return image
    }
}
#endif
