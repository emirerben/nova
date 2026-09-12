import Foundation

/// Fixed golden-hour treatment on 8-bit planar YUV, before RGB conversion.
/// Chroma correction samples the top-left luma in each 2x2 cell, as the cloud
/// filter does. Keep the intermediate byte rounding: combining the operations
/// into an RGB color matrix changes the approved look.
enum GoldenHourGrade {
    static let blueTable: [UInt8] = (0..<65536).map { chroma(UInt8($0 % 256), luma: UInt8($0 / 256), red: false) }
    static let redTable: [UInt8] = (0..<65536).map { chroma(UInt8($0 % 256), luma: UInt8($0 / 256), red: true) }
    static let lumaTable: [UInt8] = (0...255).map { value in
        // The cloud EQ parameters pass through float before the double LUT.
        let contrast = Double(Float(1.08)), brightness = Double(Float(0.015))
        let gamma = Double(Float(1.025))
        let adjusted = contrast * (Double(value) / 255 - 0.5) + 0.5 + brightness
        guard adjusted > 0 else { return 0 }
        return UInt8(clamping: Int(min(255, pow(adjusted, 1 / gamma) * 256)))
    }

    static func chroma(_ value: UInt8, luma: UInt8, red: Bool) -> UInt8 {
        let inverse: Float = 1 / 255
        let shadow: Float = red ? 0.005 : -0.015
        let highlight: Float = red ? 0.055 : -0.055
        let centered = Float(value) * inverse - 0.5
        let offset = Float(luma) * inverse * (highlight - shadow) + shadow
        let corrected: Float = (1.22 * (centered + offset) + 0.5) * 255
        return UInt8(clamping: Int(corrected))
    }

    static func apply420(_ bytes: [UInt8], width: Int, height: Int) throws -> [UInt8] {
        guard width >= 2, height >= 2, width <= 7680, height <= 7680,
              width.isMultiple(of: 2), height.isMultiple(of: 2),
              bytes.count == width * height * 3 / 2 else { throw RecipeError.invalidTimeline }
        let count = width * height
        var output = bytes
        for index in 0..<count { output[index] = lumaTable[Int(bytes[index])] }
        for row in 0..<(height / 2) {
            for column in 0..<(width / 2) {
                let index = row * (width / 2) + column
                let luma = output[row * 2 * width + column * 2]
                output[count + index] = blueTable[Int(luma) * 256 + Int(bytes[count + index])]
                output[count + count / 4 + index] = redTable[Int(luma) * 256 + Int(bytes[count + count / 4 + index])]
            }
        }
        // Production's decimal convolution coefficients are parsed as integers:
        // [0,0,0,0,1,0,0,0,0]. Its last pass therefore copies all planes.
        return output
    }
}

#if canImport(CoreVideo)
import CoreVideo

extension GoldenHourGrade {
    /// Preserve the decoder's range and color attachments. Never modify a source
    /// buffer: AVFoundation may reuse it for overlapping or backward requests.
    static func apply(to source: CVPixelBuffer) throws -> CVPixelBuffer {
        let width = CVPixelBufferGetWidth(source), height = CVPixelBufferGetHeight(source)
        let format = CVPixelBufferGetPixelFormatType(source)
        guard format == kCVPixelFormatType_420YpCbCr8BiPlanarVideoRange,
              width >= 2, height >= 2, width <= 7680, height <= 7680,
              width.isMultiple(of: 2), height.isMultiple(of: 2),
              CVPixelBufferGetPlaneCount(source) == 2 else { throw NativePreviewFeatureError("GoldenHourGrade-62") }
        var destination: CVPixelBuffer?
        guard CVPixelBufferCreate(kCFAllocatorDefault, width, height, format,
            [kCVPixelBufferIOSurfacePropertiesKey: [:]] as CFDictionary, &destination) == kCVReturnSuccess,
              let destination else { throw MediaEngineError.exportFailed }
        CVBufferPropagateAttachments(source, destination)
        guard CVPixelBufferLockBaseAddress(source, .readOnly) == kCVReturnSuccess else { throw MediaEngineError.exportFailed }
        defer { CVPixelBufferUnlockBaseAddress(source, .readOnly) }
        guard CVPixelBufferLockBaseAddress(destination, []) == kCVReturnSuccess else { throw MediaEngineError.exportFailed }
        defer { CVPixelBufferUnlockBaseAddress(destination, []) }
        guard let sourceY = CVPixelBufferGetBaseAddressOfPlane(source, 0)?.assumingMemoryBound(to: UInt8.self),
              let sourceUV = CVPixelBufferGetBaseAddressOfPlane(source, 1)?.assumingMemoryBound(to: UInt8.self),
              let targetY = CVPixelBufferGetBaseAddressOfPlane(destination, 0)?.assumingMemoryBound(to: UInt8.self),
              let targetUV = CVPixelBufferGetBaseAddressOfPlane(destination, 1)?.assumingMemoryBound(to: UInt8.self) else {
            throw MediaEngineError.exportFailed
        }
        let sourceYStride = CVPixelBufferGetBytesPerRowOfPlane(source, 0)
        let sourceUVStride = CVPixelBufferGetBytesPerRowOfPlane(source, 1)
        let targetYStride = CVPixelBufferGetBytesPerRowOfPlane(destination, 0)
        let targetUVStride = CVPixelBufferGetBytesPerRowOfPlane(destination, 1)
        for row in 0..<height {
            for column in 0..<width {
                targetY[row * targetYStride + column] = lumaTable[Int(sourceY[row * sourceYStride + column])]
            }
        }
        for row in 0..<(height / 2) {
            for column in stride(from: 0, to: width, by: 2) {
                let luma = Int(targetY[row * 2 * targetYStride + column]) * 256
                targetUV[row * targetUVStride + column] = blueTable[luma + Int(sourceUV[row * sourceUVStride + column])]
                targetUV[row * targetUVStride + column + 1] = redTable[luma + Int(sourceUV[row * sourceUVStride + column + 1])]
            }
        }
        return destination
    }
}
#endif
