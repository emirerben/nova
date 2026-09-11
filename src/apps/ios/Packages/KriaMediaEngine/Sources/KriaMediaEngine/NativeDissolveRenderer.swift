#if canImport(AVFoundation)
import Foundation
import CoreImage

/// Full text dissolve composition shared by preview and export.
final class NativeDissolveRenderer: @unchecked Sendable {
    let warp: NativeDissolveWarp
    let bitmapBytes: Int
    private let particles: CIImage
    private let finish: CIKernel

    init(width: Int, height: Int, seed: UInt32, maxBitmapBytes: Int) throws {
        guard (1...4096).contains(width), (1...4096).contains(height),
              width * height * 8 <= maxBitmapBytes else { throw MediaEngineError.unsupportedCapability }
        warp = try NativeDissolveWarp(width: width, height: height, seed: seed, maxBitmapBytes: maxBitmapBytes / 2)
        bitmapBytes = width * height * 8
        var noise = [Float](repeating: 0, count: width * height)
        for y in 0..<height {
            for x in 0..<width {
                noise[y * width + x] = DissolveTiming.particleNoise(cellX: UInt32(x / 3), cellY: UInt32(y / 3), seed: seed)
            }
        }
        particles = noise.withUnsafeBytes { bytes in
            CIImage(bitmapData: Data(bytes), bytesPerRow: width * 4,
                    size: CGSize(width: width, height: height), format: .Rf, colorSpace: nil)
        }
        let kernels = try CIKernel.kernels(withMetalString: """
        #include <CoreImage/CoreImage.h>
        using namespace metal;
        extern "C" { namespace coreimage {
        [[stitchable]] float4 kriaDissolveFinish(sampler source, sampler field, float alpha, float breakup, destination dest) {
            float2 p = dest.coord();
            float4 color = source.sample(source.transform(p));
            float noise = field.sample(field.transform(p)).r;
            float keep = breakup < 0.0 ? 1.0 : clamp((noise - breakup * 0.82) / 0.18, 0.0, 1.0);
            float faded = round(color.a * alpha * 255.0);
            float finalAlpha = floor(faded * keep) / 255.0;
            return color.a > 0.0 ? float4(color.rgb / color.a * finalAlpha, finalAlpha) : float4(0.0);
        }
        }}
        """)
        guard let finish = kernels.first else { throw MediaEngineError.unsupportedCapability }
        self.finish = finish
    }

    func image(source: CIImage, localTime: Double, duration: Double) throws -> CIImage {
        let state = try DissolveTiming.sample(localTime: localTime, duration: duration)
        if state.progress <= 0 { return source.cropped(to: warp.extent) }
        if state.alpha <= 0 { return CIImage(color: .clear).cropped(to: warp.extent) }
        let displaced = try warp.image(source: source, scale: state.displacementScale, growth: state.transformScale)
        let breakup = state.progress <= 0.18 ? -1 : min(1, (state.progress - 0.18) / 0.72)
        // Skia's drawPaint filter owns its color input, so setAlphaf does not
        // attenuate that image. Production only uses alpha for its zero-alpha
        // early return. Preserve the rendered result, not the nominal fade.
        guard let image = finish.apply(extent: warp.extent, roiCallback: { _, rect in rect },
                                       arguments: [displaced, particles, 1.0, breakup]) else {
            throw MediaEngineError.unsupportedCapability
        }
        return image
    }
}
#endif
