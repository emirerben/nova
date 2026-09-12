import Foundation

/// One-octave, non-stitching SVG noise used by the cloud text dissolve.
/// Adapted from Google's Skia SkPerlinNoiseShaderImpl.h and SkRasterPipeline_opts.h.
/// Copyright 2013 Google Inc. BSD license: Kria/Resources/Skia-LICENSE.txt.
/// Kept separate from effect activation until the displacement painter is verified.
struct DissolveNoise: Sendable {
    private let lattice: [Int]
    private let gradients: [SIMD2<Float>]

    init(seed: UInt32) {
        var state = Int(seed % 1000)
        if state == 0 { state = 1 }
        func random() -> Int {
            state = 16807 * (state % 127773) - 2836 * (state / 127773)
            if state <= 0 { state += 2147483647 }
            return state
        }
        var raw = [SIMD2<Float>]()
        raw.reserveCapacity(1024)
        for _ in 0..<1024 {
            raw.append(SIMD2(Float(random() % 512 - 256) / 256,
                             Float(random() % 512 - 256) / 256))
        }
        var permutation = Array(0..<256)
        for index in stride(from: 255, through: 1, by: -1) {
            permutation.swapAt(index, random() % 256)
        }
        var vectors = [SIMD2<Float>]()
        vectors.reserveCapacity(1024)
        for channel in 0..<4 {
            for index in 0..<256 {
                let vector = raw[channel * 256 + permutation[index]]
                let length = sqrt(vector.x * vector.x + vector.y * vector.y)
                let normalized = length > 0 ? vector / length : .zero
                // Skia stores normalized gradients as unsigned 16-bit pairs.
                let encoded = ((normalized + SIMD2(repeating: 1)) * 32767.5)
                vectors.append(SIMD2(encoded.x.rounded(), encoded.y.rounded())
                    * (2 / 65535) - SIMD2(repeating: 1))
            }
        }
        lattice = permutation
        gradients = vectors
    }

    /// Premultiplied RGBA at the pixel's upper-left integer coordinate, in [0,1].
    func pixel(x: Int, y: Int, frequency: Float) -> SIMD4<Float> {
        // Raster pixel centers already include .5; Skia adds another .5.
        let px = (Float(x) + 1) * frequency
        let py = (Float(y) + 1) * frequency
        let ix = Int(floor(px)), iy = Int(floor(py))
        let fx = px - floor(px), fy = py - floor(py)
        let sx = fx * fx * (3 - 2 * fx), sy = fy * fy * (3 - 2 * fy)
        let left = lattice[ix & 255], right = lattice[(ix + 1) & 255]
        var color = SIMD4<Float>.zero
        for channel in 0..<4 {
            func dot(_ index: Int, _ dx: Float, _ dy: Float) -> Float {
                let v = gradients[channel * 256 + (index & 255)]
                return v.x * dx + v.y * dy
            }
            let a = dot(left + iy, fx, fy)
            let b = dot(right + iy, fx - 1, fy)
            let c = dot(left + iy + 1, fx, fy - 1)
            let d = dot(right + iy + 1, fx - 1, fy - 1)
            let top = a + (b - a) * sx, bottom = c + (d - c) * sx
            color[channel] = min(1, max(0, (top + (bottom - top) * sy) * 0.5 + 0.5))
        }
        return SIMD4(color.x * color.w, color.y * color.w, color.z * color.w, color.w)
    }

    /// Premultiplied map consumed by Skia's text displacement filter.
    /// The production matrix passes -2 * 255 as its offset. Skia's normalized
    /// matrix clamps both coarse R/G channels to zero; preserve that behavior.
    func textMapPixel(x: Int, y: Int, frequency: Float = 0.004) -> SIMD4<Float> {
        let coarse = pixel(x: x, y: y, frequency: frequency)
        func byte(_ value: Float) -> Float { (value * 255).rounded() / 255 }
        let alpha = byte(coarse.w)
        // The fine frequency is exactly 1. Integer lattice coordinates yield
        // neutral .5 RGBA for every seed, rasterized to (64,64,64,128).
        let fineAlpha: Float = 128 / 255
        let fineColor: Float = 64 / 255
        return SIMD4(fineColor, fineColor,
                     byte(fineColor + byte(coarse.z) * (1 - fineAlpha)),
                     byte(fineAlpha + alpha * (1 - fineAlpha)))
    }
}
