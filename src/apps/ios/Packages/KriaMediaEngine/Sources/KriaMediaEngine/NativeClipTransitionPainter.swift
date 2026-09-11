#if canImport(AVFoundation)
import Foundation
import CoreImage

enum NativeClipTransitionPainter {
    private static let fadeKernel: Result<CIKernel, Error> = Result {
        let kernels = try CIKernel.kernels(withMetalString: """
        #include <CoreImage/CoreImage.h>
        using namespace metal;
        extern "C" { namespace coreimage {
        [[stitchable]] float4 kriaClipFade(sampler outgoing, sampler incoming,
                                          float a, float b, float white, destination dest) {
            float2 p = dest.coord();
            float3 first = outgoing.sample(outgoing.transform(p)).rgb;
            float3 second = incoming.sample(incoming.transform(p)).rgb;
            // FFmpeg fills its YUV background with Y=0/255, U=V=127.
            // Translate that limited-range BT.601 fill into encoded RGB.
            float y = (white * 255.0 - 16.0) / 219.0;
            float c = -1.0 / 224.0;
            float3 background = float3(y + 1.402*c, y - 0.344136*c - 0.714136*c, y + 1.772*c);
            return float4(clamp(first*a + second*b + background*(1.0-a-b), 0.0, 1.0), 1.0);
        }
        }}
        """)
        guard let kernel = kernels.first else { throw MediaEngineError.unsupportedCapability }
        return kernel
    }

    static func image(incoming: CIImage, outgoing: CIImage, kind: Transition.Kind,
                      progress: Double, canvas: CGRect) throws -> CIImage {
        switch kind {
        case .wipeLeft, .wipeRight:
            let range = ClipTransitionTiming.wipeIncomingRange(width: Int(canvas.width), progress: progress, left: kind == .wipeLeft)
            let region = CGRect(x: canvas.minX + Double(range.lowerBound), y: canvas.minY,
                                width: Double(range.count), height: canvas.height)
            return incoming.cropped(to: region).composited(over: outgoing).cropped(to: canvas)
        case .fadeBlack, .fadeWhite:
            let weights = ClipTransitionTiming.fadeWeights(progress: progress)
            guard let blended = try fadeKernel.get().apply(extent: canvas, roiCallback: { _, rect in rect }, arguments: [
                outgoing.applyingFilter("CILinearToSRGBToneCurve"), incoming.applyingFilter("CILinearToSRGBToneCurve"),
                weights.outgoing, weights.incoming, kind == .fadeWhite ? 1.0 : 0.0
            ]) else { throw MediaEngineError.unsupportedCapability }
            return blended.applyingFilter("CISRGBToneCurveToLinear").cropped(to: canvas)
        case .crossfade:
            throw MediaEngineError.unsupportedCapability
        }
    }
}
#endif
