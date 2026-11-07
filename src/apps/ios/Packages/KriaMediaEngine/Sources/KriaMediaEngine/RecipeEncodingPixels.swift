#if canImport(AVFoundation)
@preconcurrency import Accelerate
@preconcurrency import AVFoundation

/// Converts the compositor's full-range BGRA to standard video-range BT.709 before H.264.
/// The implicit VideoToolbox BGRA conversion can map 255 white to luma 234 instead of 235.
/// Explicit, faithfully rounded vImage conversion preserves the whole range, not just white.
/// Owned by one RecipeWriter; the pool never reuses buffers still held by the encoder.
final class RecipeEncodingPixels {
    private let width: Int
    private let height: Int
    private let pool: CVPixelBufferPool
    private var conversion = vImage_ARGBToYpCbCr()

    init(width: Int, height: Int) throws {
        self.width = width; self.height = height
        var pool: CVPixelBufferPool?
        guard width > 0, height > 0,
              CVPixelBufferPoolCreate(nil, nil, [
                kCVPixelBufferPixelFormatTypeKey: kCVPixelFormatType_420YpCbCr8BiPlanarVideoRange,
                kCVPixelBufferWidthKey: width, kCVPixelBufferHeightKey: height,
                kCVPixelBufferIOSurfacePropertiesKey: [:]
              ] as CFDictionary, &pool) == kCVReturnSuccess, let pool else { throw MediaEngineError.exportUnavailable }
        self.pool = pool
        var range = vImage_YpCbCrPixelRange(Yp_bias: 16, CbCr_bias: 128, YpRangeMax: 235, CbCrRangeMax: 240,
                                          YpMax: 235, YpMin: 16, CbCrMax: 240, CbCrMin: 16)
        guard vImageConvert_ARGBToYpCbCr_GenerateConversion(kvImage_ARGBToYpCbCrMatrix_ITU_R_709_2, &range, &conversion,
                                                          kvImageARGB8888, kvImage420Yp8_CbCr8, vImage_Flags(kvImageNoFlags)) == kvImageNoError
        else { throw MediaEngineError.exportUnavailable }
    }

    func convert(_ sample: CMSampleBuffer) throws -> CMSampleBuffer {
        guard let source = CMSampleBufferGetImageBuffer(sample),
              CVPixelBufferGetPixelFormatType(source) == kCVPixelFormatType_32BGRA,
              CVPixelBufferGetWidth(source) == width, CVPixelBufferGetHeight(source) == height
        else { throw MediaEngineError.exportFailed }
        var buffer: CVPixelBuffer?
        guard CVPixelBufferPoolCreatePixelBuffer(nil, pool, &buffer) == kCVReturnSuccess, let output = buffer
        else { throw MediaEngineError.exportUnavailable }
        try fill(source, output)
        // The compositor/reader already produces Rec.709-encoded RGB. Preserve its transfer and
        // primaries; only the RGB-to-YCbCr matrix/range changes. vImage averages centered 2x2 chroma.
        CVBufferSetAttachment(output, kCVImageBufferColorPrimariesKey, kCVImageBufferColorPrimaries_ITU_R_709_2, .shouldPropagate)
        CVBufferSetAttachment(output, kCVImageBufferTransferFunctionKey, kCVImageBufferTransferFunction_ITU_R_709_2, .shouldPropagate)
        CVBufferSetAttachment(output, kCVImageBufferYCbCrMatrixKey, kCVImageBufferYCbCrMatrix_ITU_R_709_2, .shouldPropagate)
        CVBufferSetAttachment(output, kCVImageBufferChromaLocationTopFieldKey, kCVImageBufferChromaLocation_Center, .shouldPropagate)
        CVBufferSetAttachment(output, kCVImageBufferChromaLocationBottomFieldKey, kCVImageBufferChromaLocation_Center, .shouldPropagate)
        for key in [kCVImageBufferCleanApertureKey, kCVImageBufferPixelAspectRatioKey] {
            if let value = CVBufferCopyAttachment(source, key, nil) { CVBufferSetAttachment(output, key, value, .shouldPropagate) }
        }
        var format: CMVideoFormatDescription?
        var timing = CMSampleTimingInfo()
        var result: CMSampleBuffer?
        guard CMVideoFormatDescriptionCreateForImageBuffer(allocator: nil, imageBuffer: output, formatDescriptionOut: &format) == noErr,
              let format, CMSampleBufferGetSampleTimingInfo(sample, at: 0, timingInfoOut: &timing) == noErr,
              CMSampleBufferCreateReadyWithImageBuffer(allocator: nil, imageBuffer: output, formatDescription: format,
                                                      sampleTiming: &timing, sampleBufferOut: &result) == noErr, let result
        else { throw MediaEngineError.exportFailed }
        return result
    }

    private func fill(_ source: CVPixelBuffer, _ output: CVPixelBuffer) throws {
        guard CVPixelBufferLockBaseAddress(source, .readOnly) == kCVReturnSuccess else { throw MediaEngineError.exportFailed }
        defer { CVPixelBufferUnlockBaseAddress(source, .readOnly) }
        guard CVPixelBufferLockBaseAddress(output, []) == kCVReturnSuccess else { throw MediaEngineError.exportFailed }
        defer { CVPixelBufferUnlockBaseAddress(output, []) }
        var rgb = vImage_Buffer(data: CVPixelBufferGetBaseAddress(source), height: vImagePixelCount(height), width: vImagePixelCount(width),
                                rowBytes: CVPixelBufferGetBytesPerRow(source))
        var y = vImage_Buffer(data: CVPixelBufferGetBaseAddressOfPlane(output, 0), height: vImagePixelCount(height), width: vImagePixelCount(width),
                              rowBytes: CVPixelBufferGetBytesPerRowOfPlane(output, 0))
        var uv = vImage_Buffer(data: CVPixelBufferGetBaseAddressOfPlane(output, 1),
                               height: vImagePixelCount(CVPixelBufferGetHeightOfPlane(output, 1)),
                               width: vImagePixelCount(CVPixelBufferGetWidthOfPlane(output, 1)), rowBytes: CVPixelBufferGetBytesPerRowOfPlane(output, 1))
        let bgra: [UInt8] = [3, 2, 1, 0]
        guard vImageConvert_ARGB8888To420Yp8_CbCr8(&rgb, &y, &uv, &conversion, bgra, vImage_Flags(kvImageDoNotTile)) == kvImageNoError
        else { throw MediaEngineError.exportFailed }
    }
}
#endif
