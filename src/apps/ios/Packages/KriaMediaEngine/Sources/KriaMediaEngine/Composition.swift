import Foundation

#if canImport(AVFoundation)
import AVFoundation
import CoreMedia
import CoreImage
import ImageIO
#if canImport(UIKit)
import UIKit
#endif
#endif

public struct CompositionDescription: Equatable, Sendable {
    public var duration: TimeInterval; public var canvas: Canvas; public var hasVideo: Bool; public var hasAudio: Bool
    public init(duration: TimeInterval, canvas: Canvas, hasVideo: Bool, hasAudio: Bool) { self.duration = duration; self.canvas = canvas; self.hasVideo = hasVideo; self.hasAudio = hasAudio }
}

public protocol PreviewComposing: Sendable {
    func makePreview(recipe: EditRecipe, assetURLs: [String: URL]) async throws -> PreviewComposition
}

/// A composition and its player-facing description. AVPlayer remains the playback clock; callers never
/// receive a pixel buffer or a renderer-specific frame object.
public struct PreviewComposition: @unchecked Sendable {
    public let description: CompositionDescription
#if canImport(AVFoundation)
    public let playerItem: AVPlayerItem
    public init(description: CompositionDescription, playerItem: AVPlayerItem) { self.description = description; self.playerItem = playerItem }
#else
    public init(description: CompositionDescription) { self.description = description }
#endif
}

#if canImport(AVFoundation)
@MainActor public struct AVPlayerPreviewComposer: PreviewComposing {
    public init() {}
    public func makePreview(recipe: EditRecipe, assetURLs: [String: URL]) async throws -> PreviewComposition {
        try recipe.validate()
        guard recipe.rendererVersion == "kria-ios-\(recipe.schemaVersion)", !recipe.audio.duckOriginalDuringMusic else {
            throw MediaEngineError.unsupportedCapability
        }
        let composition = AVMutableComposition()
        let canvas = CGSize(width: recipe.canvas.width, height: recipe.canvas.height)
        let videoComposition = AVMutableVideoComposition()
        videoComposition.renderSize = canvas
        videoComposition.frameDuration = CMTime(seconds: 1 / recipe.frameRate, preferredTimescale: 60_000)
        videoComposition.colorPrimaries = AVVideoColorPrimaries_ITU_R_709_2
        videoComposition.colorTransferFunction = AVVideoTransferFunction_ITU_R_709_2
        videoComposition.colorYCbCrMatrix = AVVideoYCbCrMatrix_ITU_R_709_2
        videoComposition.customVideoCompositorClass = RecipeVideoCompositor.self
        let total = TimelineMath.totalDuration(of: recipe)
        var layers: [RecipeVideoLayer] = []
        var textLayers: [RecipeTextLayer] = []
        var audioParameters: [AVMutableAudioMixInputParameters] = []
        var stillClock: StillTimelineClock?
        func time(_ seconds: Double) -> CMTime { CMTime(value: Int64((seconds * 60_000).rounded()), timescale: 60_000) }
        func addAudio(asset: AVURLAsset, clip: TimelineClip, gain: Double) async throws {
            guard let source = try await asset.loadTracks(withMediaType: .audio).first else { return }
            guard let track = composition.addMutableTrack(withMediaType: .audio, preferredTrackID: kCMPersistentTrackID_Invalid) else { throw MediaEngineError.exportUnavailable }
            let sourceRange = CMTimeRange(start: time(clip.sourceStart), duration: time(clip.sourceDuration))
            try track.insertTimeRange(sourceRange, of: source, at: time(clip.timelineStart))
            track.scaleTimeRange(CMTimeRange(start: time(clip.timelineStart), duration: sourceRange.duration), toDuration: time(clip.duration))
            let parameter = AVMutableAudioMixInputParameters(track: track)
            parameter.setVolume(Float(gain * clip.volume), at: time(clip.timelineStart))
            audioParameters.append(parameter)
        }
        for recipeTrack in recipe.tracks {
            var previousEnd: Double?
            for clip in recipeTrack.clips.sorted(by: { $0.timelineStart < $1.timelineStart }) {
                guard let url = assetURLs[clip.sourceAssetID] else { throw MediaEngineError.missingAsset(clip.sourceAssetID) }
                let asset = AVURLAsset(url: url)
                if recipeTrack.kind == .audio {
                    guard !(try await asset.loadTracks(withMediaType: .audio)).isEmpty else { throw MediaEngineError.missingAsset(clip.sourceAssetID) }
                    try await addAudio(asset: asset, clip: clip, gain: 1)
                    continue
                }
                let end = clip.timelineStart + clip.duration
                let fadeIn = clip.transition?.duration ?? 0
                // timeline_start is the actual insertion time; never subtract a transition here.
                if fadeIn > 0 {
                    guard let previousEnd, previousEnd + 0.000_001 >= clip.timelineStart + fadeIn else { throw RecipeError.invalidTimeline }
                }
                previousEnd = end
                let imageSource = CGImageSourceCreateWithURL(url as CFURL, nil)
                let stillImage = imageSource.flatMap { source in
                    CGImageSourceGetCount(source) > 0 ? CGImageSourceCreateImageAtIndex(source, 0, nil) : nil
                }
                let videoSource: AVAssetTrack?
                if stillImage == nil { videoSource = try await asset.loadTracks(withMediaType: .video).first }
                else { videoSource = nil }
                if let source = videoSource {
                    guard let track = composition.addMutableTrack(withMediaType: .video, preferredTrackID: kCMPersistentTrackID_Invalid) else { throw MediaEngineError.exportUnavailable }
                    let sourceRange = CMTimeRange(start: time(clip.sourceStart), duration: time(clip.sourceDuration))
                    try track.insertTimeRange(sourceRange, of: source, at: time(clip.timelineStart))
                    track.scaleTimeRange(CMTimeRange(start: time(clip.timelineStart), duration: sourceRange.duration), toDuration: time(clip.duration))
                    let size = try await source.load(.naturalSize)
                    let preferred = try await source.load(.preferredTransform)
                    layers.append(RecipeVideoLayer(trackID: track.trackID, image: nil,
                        transform: Self.transform(naturalSize: size, preferred: preferred, canvas: canvas, clip: clip),
                        start: clip.timelineStart, end: end, fadeIn: fadeIn))
                    try await addAudio(asset: asset, clip: clip, gain: recipeTrack.kind == .video ? recipe.audio.originalVolume : 1)
                } else {
                    guard let imageSource, let image = stillImage else { throw MediaEngineError.missingAsset(clip.sourceAssetID) }
                    let properties = CGImageSourceCopyPropertiesAtIndex(imageSource, 0, nil) as? [CFString: Any]
                    let orientation = (properties?[kCGImagePropertyOrientation] as? NSNumber)?.int32Value ?? 1
                    guard (1...8).contains(orientation) else { throw MediaEngineError.unsupportedCapability }
                    let oriented = CIImage(cgImage: image).oriented(forExifOrientation: orientation)
                    let normalized = oriented.transformed(by: CGAffineTransform(translationX: -oriented.extent.minX, y: -oriented.extent.minY))
                    layers.append(RecipeVideoLayer(trackID: nil, image: normalized,
                        transform: Self.transform(naturalSize: normalized.extent.size, preferred: .identity, canvas: canvas, clip: clip),
                        start: clip.timelineStart, end: end, fadeIn: fadeIn))
                }
                if let text = clip.text { textLayers.append(try RecipeTextLayer.make(text, start: clip.timelineStart, end: end, canvas: canvas)) }
            }
        }
        var textBitmapBytes = textLayers.reduce(0) { $0 + Int($1.image.extent.width * $1.image.extent.height * 4) }
        for layer in recipe.textLayers {
            let painted = try RecipeTextLayer.make(layer, assetURLs: assetURLs, canvas: canvas, maxBitmapBytes: 64 * 1024 * 1024 - textBitmapBytes)
            textBitmapBytes += Int(painted.image.extent.width * painted.image.extent.height * 4) * (painted.handwriting == nil && painted.discreteReveal == nil ? 1 : 2)
            textLayers.append(painted)
        }
        if !layers.contains(where: { $0.trackID != nil }) {
            guard !layers.isEmpty, total > 0 else { throw MediaEngineError.unsupportedCapability }
            let clock = try await StillTimelineClock.make()
            stillClock = clock
            let asset = AVURLAsset(url: clock.url)
            guard let source = try await asset.loadTracks(withMediaType: .video).first,
                  let track = composition.addMutableTrack(withMediaType: .video, preferredTrackID: kCMPersistentTrackID_Invalid) else {
                throw MediaEngineError.exportUnavailable
            }
            let frame = CMTimeRange(start: .zero, duration: CMTime(value: 1, timescale: 30))
            try track.insertTimeRange(frame, of: source, at: .zero)
            track.scaleTimeRange(frame, toDuration: time(total))
            layers.insert(RecipeVideoLayer(trackID: track.trackID, image: nil,
                                          transform: CGAffineTransform(scaleX: canvas.width / 16, y: canvas.height / 16),
                                          start: 0, end: total, fadeIn: 0), at: 0)
            StillClockLifetime.retain(clock, on: composition)
        }
        if let musicID = recipe.audio.musicAssetID {
            guard let url = assetURLs[musicID] else { throw MediaEngineError.missingAsset(musicID) }
            let asset = AVURLAsset(url: url)
            guard let source = try await asset.loadTracks(withMediaType: .audio).first,
                  let track = composition.addMutableTrack(withMediaType: .audio, preferredTrackID: kCMPersistentTrackID_Invalid) else { throw MediaEngineError.missingAsset(musicID) }
            let duration = min(total, try await asset.load(.duration).seconds)
            guard duration > 0 else { throw MediaEngineError.missingAsset(musicID) }
            try track.insertTimeRange(CMTimeRange(start: .zero, duration: time(duration)), of: source, at: .zero)
            let parameter = AVMutableAudioMixInputParameters(track: track)
            let volume = Float(recipe.audio.musicVolume)
            parameter.setVolume(volume, at: .zero)
            let fadeIn = min(recipe.audio.fadeIn, duration)
            let fadeOut = min(recipe.audio.fadeOut, duration - fadeIn)
            if fadeIn > 0 { parameter.setVolumeRamp(fromStartVolume: 0, toEndVolume: volume, timeRange: CMTimeRange(start: .zero, duration: time(fadeIn))) }
            if fadeOut > 0 { parameter.setVolumeRamp(fromStartVolume: volume, toEndVolume: 0, timeRange: CMTimeRange(start: time(duration - fadeOut), duration: time(fadeOut))) }
            audioParameters.append(parameter)
        }
        if composition.duration.seconds < total { composition.insertEmptyTimeRange(CMTimeRange(start: composition.duration, duration: time(total - composition.duration.seconds))) }
        let boundaries = Set([0, total] + layers.flatMap { [$0.start, $0.end] }).sorted()
        videoComposition.instructions = zip(boundaries, boundaries.dropFirst()).map { start, end in
            RecipeVideoInstruction(timeRange: CMTimeRange(start: time(start), end: time(end)),
                layers: layers.filter { $0.start < end && $0.end > start }, text: textLayers, canvas: canvas)
        }
        let audioMix = AVMutableAudioMix()
        audioMix.inputParameters = audioParameters
        let item = AVPlayerItem(asset: composition)
        if let stillClock { StillClockLifetime.retain(stillClock, on: item) }
        item.videoComposition = videoComposition
        item.audioMix = audioMix
        return PreviewComposition(description: CompositionDescription(duration: total, canvas: recipe.canvas, hasVideo: true, hasAudio: !audioParameters.isEmpty), playerItem: item)
    }

    private static func transform(naturalSize: CGSize, preferred: CGAffineTransform, canvas: CGSize, clip: TimelineClip) -> CGAffineTransform {
        let rect = CGRect(origin: .zero, size: naturalSize).applying(preferred)
        let scale = max(canvas.width / max(abs(rect.width), 1), canvas.height / max(abs(rect.height), 1))
        return preferred
            .concatenating(CGAffineTransform(translationX: -rect.minX, y: -rect.minY))
            .concatenating(CGAffineTransform(scaleX: scale, y: scale))
            .concatenating(CGAffineTransform(translationX: -abs(rect.width) * scale / 2, y: -abs(rect.height) * scale / 2))
            .concatenating(CGAffineTransform(scaleX: clip.transform.scale, y: clip.transform.scale))
            .concatenating(CGAffineTransform(rotationAngle: clip.transform.rotationDegrees * .pi / 180))
            .concatenating(CGAffineTransform(translationX: canvas.width / 2 + clip.transform.positionX, y: canvas.height / 2 - clip.transform.positionY))
    }
}

/// Main-actor adapter for UI controls. AVPlayer is the only source of playback time; SwiftUI or UIKit
/// may observe this adapter without ever handling decoded frame data.
@MainActor public final class PreviewPlayerController {
    public let player: AVPlayer
    public init(item: AVPlayerItem? = nil) { self.player = AVPlayer(playerItem: item) }
    public var currentTime: TimeInterval { player.currentTime().seconds }
    public var duration: TimeInterval { player.currentItem?.duration.seconds ?? 0 }
    public var isPlaying: Bool { player.timeControlStatus == .playing }
    public func play() { player.play() }
    public func pause() { player.pause() }
    public func replace(with composition: PreviewComposition) { player.replaceCurrentItem(with: composition.playerItem) }
    public func seek(to seconds: TimeInterval, tolerance: TimeInterval = 0.02, completion: (@Sendable (Bool) -> Void)? = nil) {
        player.seek(to: CMTime(seconds: max(0, seconds), preferredTimescale: 600), toleranceBefore: CMTime(seconds: tolerance, preferredTimescale: 600), toleranceAfter: CMTime(seconds: tolerance, preferredTimescale: 600)) { finished in completion?(finished) }
    }
}

private extension Array { subscript(safe index: Index) -> Element? { indices.contains(index) ? self[index] : nil } }
#endif
