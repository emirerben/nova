import Foundation

#if canImport(AVFoundation)
import AVFoundation
import CoreMedia
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
        let composition = AVMutableComposition()
        let videoComposition = AVMutableVideoComposition(); videoComposition.renderSize = CGSize(width: recipe.canvas.width, height: recipe.canvas.height); videoComposition.frameDuration = CMTime(value: 1, timescale: CMTimeScale(max(1, Int32(recipe.frameRate.rounded()))))
        var videoTrack: AVMutableCompositionTrack?
        var hasOriginalAudio = false
        var layerInstructions: [AVMutableVideoCompositionLayerInstruction] = []
        let assets: [String: AVURLAsset] = Dictionary(uniqueKeysWithValues: recipe.assets.compactMap { asset in assetURLs[asset.id].map { (asset.id, AVURLAsset(url: $0)) } })
        var cursor: TimeInterval = 0
        var previousLayer: AVMutableVideoCompositionLayerInstruction?
        for track in recipe.tracks where track.kind == .video {
            for clip in track.clips.sorted(by: { $0.timelineStart < $1.timelineStart }) {
                guard let asset = assets[clip.sourceAssetID], let source = try await asset.loadTracks(withMediaType: .video).first else { throw MediaEngineError.missingAsset(clip.sourceAssetID) }
                let sourceRange = CMTimeRange(start: CMTime(seconds: clip.sourceStart, preferredTimescale: 600), duration: CMTime(seconds: clip.sourceDuration, preferredTimescale: 600))
                let overlap = clip.transition.map { _ in min(clip.transition?.duration ?? 0, clip.duration) } ?? 0
                let insertionSeconds = max(0, clip.timelineStart - overlap)
                let track = composition.addMutableTrack(withMediaType: .video, preferredTrackID: kCMPersistentTrackID_Invalid)!
                videoTrack = videoTrack ?? track
                let insertion = CMTime(seconds: insertionSeconds, preferredTimescale: 600)
                try track.insertTimeRange(sourceRange, of: source, at: insertion)
                track.scaleTimeRange(CMTimeRange(start: insertion, duration: sourceRange.duration), toDuration: CMTime(seconds: clip.duration, preferredTimescale: 600))
                let instruction = AVMutableVideoCompositionLayerInstruction(assetTrack: track)
                let radians = clip.transform.rotationDegrees * .pi / 180
                let naturalSize = try await source.load(.naturalSize)
                let preferredTransform = try await source.load(.preferredTransform)
                let orientedRect = CGRect(origin: .zero, size: naturalSize).applying(preferredTransform)
                let canvas = CGSize(width: recipe.canvas.width, height: recipe.canvas.height)
                let scaleToFill = max(canvas.width / max(abs(orientedRect.width), 1), canvas.height / max(abs(orientedRect.height), 1))
                let transform = preferredTransform
                    .concatenating(CGAffineTransform(translationX: -orientedRect.minX, y: -orientedRect.minY))
                    .concatenating(CGAffineTransform(scaleX: scaleToFill, y: scaleToFill))
                    .concatenating(CGAffineTransform(translationX: (canvas.width - abs(orientedRect.width) * scaleToFill) / 2, y: (canvas.height - abs(orientedRect.height) * scaleToFill) / 2))
                    .concatenating(CGAffineTransform(translationX: clip.transform.positionX, y: clip.transform.positionY))
                    .rotated(by: radians)
                    .scaledBy(x: clip.transform.scale, y: clip.transform.scale)
                instruction.setTransform(transform, at: insertion)
                if let audio = try await asset.loadTracks(withMediaType: .audio).first,
                   let audioTrack = composition.addMutableTrack(withMediaType: .audio, preferredTrackID: kCMPersistentTrackID_Invalid) {
                    try audioTrack.insertTimeRange(sourceRange, of: audio, at: insertion)
                    audioTrack.scaleTimeRange(CMTimeRange(start: insertion, duration: sourceRange.duration), toDuration: CMTime(seconds: clip.duration, preferredTimescale: 600))
                    hasOriginalAudio = true
                }
                if overlap > 0, let previousLayer {
                    let transitionStart = CMTime(seconds: insertionSeconds, preferredTimescale: 600)
                    let transitionRange = CMTimeRange(start: transitionStart, duration: CMTime(seconds: overlap, preferredTimescale: 600))
                    previousLayer.setOpacityRamp(fromStartOpacity: 1, toEndOpacity: 0, timeRange: transitionRange)
                    instruction.setOpacityRamp(fromStartOpacity: 0, toEndOpacity: 1, timeRange: transitionRange)
                }
                layerInstructions.append(instruction); cursor = max(cursor, clip.timelineStart + clip.duration)
                previousLayer = instruction
            }
        }
        for track in recipe.tracks where track.kind == .audio {
            for clip in track.clips {
                guard let asset = assets[clip.sourceAssetID], let source = try await asset.loadTracks(withMediaType: .audio).first else { continue }
                let range = CMTimeRange(start: CMTime(seconds: clip.sourceStart, preferredTimescale: 600), duration: CMTime(seconds: clip.sourceDuration, preferredTimescale: 600))
                if let originalAudioTrack = composition.addMutableTrack(withMediaType: .audio, preferredTrackID: kCMPersistentTrackID_Invalid) {
                    try originalAudioTrack.insertTimeRange(range, of: source, at: CMTime(seconds: clip.timelineStart, preferredTimescale: 600))
                    hasOriginalAudio = true
                }
            }
        }
        // Music is a separate composition track so its gain, fades, and optional ducking can be
        // controlled independently from source audio.
        var musicTrack: AVMutableCompositionTrack?
        if let musicID = recipe.audio.musicAssetID, let musicAsset = assets[musicID], let source = try await musicAsset.loadTracks(withMediaType: .audio).first {
            musicTrack = composition.addMutableTrack(withMediaType: .audio, preferredTrackID: kCMPersistentTrackID_Invalid)
            let duration = CMTime(seconds: max(0, TimelineMath.totalDuration(of: recipe)), preferredTimescale: 600)
            let sourceDuration = try await musicAsset.load(.duration)
            let rangeDuration = min(duration, sourceDuration)
            if rangeDuration > .zero { try musicTrack?.insertTimeRange(CMTimeRange(start: .zero, duration: rangeDuration), of: source, at: .zero) }
        }
        if videoTrack != nil, !layerInstructions.isEmpty {
            let instruction = AVMutableVideoCompositionInstruction(); instruction.timeRange = CMTimeRange(start: .zero, duration: composition.duration); instruction.layerInstructions = layerInstructions.reversed(); videoComposition.instructions = [instruction]
            addTextAnimations(recipe: recipe, to: videoComposition, canvas: CGSize(width: recipe.canvas.width, height: recipe.canvas.height))
        }
        let audioMix = makeAudioMix(recipe: recipe, composition: composition, musicTrack: musicTrack)
        let item = AVPlayerItem(asset: composition); item.videoComposition = videoComposition; item.audioMix = audioMix
        return PreviewComposition(description: CompositionDescription(duration: composition.duration.seconds, canvas: recipe.canvas, hasVideo: videoTrack != nil, hasAudio: hasOriginalAudio || musicTrack != nil), playerItem: item)
    }

    private func makeAudioMix(recipe: EditRecipe, composition: AVMutableComposition, musicTrack: AVMutableCompositionTrack?) -> AVAudioMix? {
        let params = composition.tracks(withMediaType: .audio).map { track -> AVMutableAudioMixInputParameters in
            let p = AVMutableAudioMixInputParameters(track: track)
            let isMusic = track.trackID == musicTrack?.trackID
            let volume = Float(isMusic ? recipe.audio.musicVolume : recipe.audio.originalVolume)
            p.setVolume(volume, at: .zero)
            if isMusic {
                if recipe.audio.fadeIn > 0 { p.setVolumeRamp(fromStartVolume: 0, toEndVolume: volume, timeRange: CMTimeRange(start: .zero, duration: CMTime(seconds: recipe.audio.fadeIn, preferredTimescale: 600))) }
                if recipe.audio.fadeOut > 0 { let end = composition.duration.seconds; let start = max(0, end - recipe.audio.fadeOut); p.setVolumeRamp(fromStartVolume: volume, toEndVolume: 0, timeRange: CMTimeRange(start: CMTime(seconds: start, preferredTimescale: 600), duration: CMTime(seconds: recipe.audio.fadeOut, preferredTimescale: 600))) }
            }
            return p
        }
        guard !params.isEmpty else { return nil }
        let mix = AVMutableAudioMix(); mix.inputParameters = params; return mix
    }

    /// Core Animation is intentionally used only for the first supported treatment. Export and preview
    /// share this layer model, while AVPlayer owns the clock.
    private func addTextAnimations(recipe: EditRecipe, to videoComposition: AVMutableVideoComposition, canvas: CGSize) {
        guard let text = recipe.tracks.flatMap(\.clips).compactMap(\.text).first else { return }
        let parent = CALayer(); parent.frame = CGRect(origin: .zero, size: canvas); let video = CALayer(); video.frame = parent.bounds
        let title = CATextLayer(); title.string = text.text; title.font = CGFont(text.fontName as CFString); title.fontSize = text.fontSize; title.alignmentMode = .center; title.contentsScale = 2
        title.frame = CGRect(x: canvas.width * 0.08, y: text.anchor == .top ? canvas.height * 0.12 : (text.anchor == .bottom ? canvas.height * 0.72 : canvas.height * 0.43), width: canvas.width * 0.84, height: text.fontSize * 1.4)
        title.foregroundColor = CGColor(red: text.colorRGBA[safe: 0] ?? 1, green: text.colorRGBA[safe: 1] ?? 1, blue: text.colorRGBA[safe: 2] ?? 1, alpha: text.colorRGBA[safe: 3] ?? 1)
        if text.animation == .fade || text.animation == .fadeScale {
            title.opacity = 1
            let fade = CABasicAnimation(keyPath: "opacity"); fade.fromValue = 0; fade.toValue = 1; fade.duration = 0.3; fade.beginTime = AVCoreAnimationBeginTimeAtZero; fade.isRemovedOnCompletion = false; fade.fillMode = .forwards; title.add(fade, forKey: "kria.fade")
            if text.animation == .fadeScale { let scale = CABasicAnimation(keyPath: "transform.scale"); scale.fromValue = 0.82; scale.toValue = 1; scale.duration = 0.35; scale.beginTime = AVCoreAnimationBeginTimeAtZero; scale.isRemovedOnCompletion = false; scale.fillMode = .forwards; title.add(scale, forKey: "kria.scale") }
        }
        parent.addSublayer(video); parent.addSublayer(title); videoComposition.animationTool = AVVideoCompositionCoreAnimationTool(postProcessingAsVideoLayer: video, in: parent)
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
