#if canImport(AVFoundation)
import AVFoundation
import CoreImage

/// Owns a source composition across editor changes. Text-only updates replace
/// immutable compositor instructions without reloading or seeking source tracks.
@MainActor public final class LivePreviewComposition {
    public private(set) var recipe: EditRecipe
    public let preview: PreviewComposition
    private var assetURLs: [String: URL]

    public init(recipe: EditRecipe, assetURLs: [String: URL]) async throws {
        self.recipe = recipe
        self.assetURLs = assetURLs
        self.preview = try await AVPlayerPreviewComposer().makePreview(recipe: recipe, assetURLs: assetURLs)
    }

    public func mediaSelectionBounds(id: String, time: Double) -> TextSelectionBounds? {
        guard let instructions = preview.playerItem.videoComposition?.instructions as? [RecipeVideoInstruction],
              let instruction = instructions.first(where: { CMTimeRangeContainsTime($0.timeRange, time: CMTime(seconds: time, preferredTimescale: 60_000)) }),
              let layer = instruction.layers.first(where: { $0.clipID == id }), let size = layer.naturalSize else { return nil }
        var rectangle = CGRect(origin: .zero, size: size).applying(layer.transform)
        let rotation = layer.visualPlacement?.editorStyle?.rotationDegrees ?? 0
        if var placement = layer.visualPlacement {
            // Selection chrome rotates once around the actual unrotated media box.
            // Using the rotated axis-aligned image extent would rotate it twice.
            placement.editorStyle?.rotationDegrees = 0
            rectangle = placement.position(CIImage(color: .white).cropped(to: CGRect(origin: .zero, size: size)),
                preferred: layer.preferredTransform, canvas: instruction.canvas, time: time,
                clipStart: layer.start, clipEnd: layer.end).extent.intersection(instruction.canvas)
        }
        if layer.overlayPopIn {
            let scale = 0.82 + 0.18 * min(1, max(0, (time - layer.start) / 0.18))
            rectangle = CGRect(x: layer.overlayCenter.x + (rectangle.minX - layer.overlayCenter.x) * scale,
                y: layer.overlayCenter.y + (rectangle.minY - layer.overlayCenter.y) * scale,
                width: rectangle.width * scale, height: rectangle.height * scale)
        }
        return TextSelectionBounds(centerX: rectangle.midX / instruction.canvas.width,
            centerY: 1 - rectangle.midY / instruction.canvas.height,
            width: rectangle.width / instruction.canvas.width, height: rectangle.height / instruction.canvas.height, rotationDegrees: rotation)
    }

    public func textSelectionBounds(id: String, time: Double) -> TextSelectionBounds? {
        guard let first = preview.playerItem.videoComposition?.instructions.first as? RecipeVideoInstruction else { return nil }
        return first.textStore?.selectionBounds(id: id, at: time)
    }

    /// Freeze the surrounding scene once; the editor transforms the selected
    /// text image without decoding video or reshaping fonts on finger samples.
    public func textInteractionLayers(id: String, time: Double) throws -> (below: AVVideoComposition, above: AVVideoComposition, text: CGImage, rect: CGRect) {
        guard let composition = preview.playerItem.videoComposition,
              let instruction = (composition.instructions as? [RecipeVideoInstruction])?.first(where: {
                  CMTimeRangeContainsTime($0.timeRange, time: CMTime(seconds: time, preferredTimescale: 600))
              }) else { throw MediaEngineError.exportFailed }
        let active = try instruction.activeText(at: time)
        guard let index = active.firstIndex(where: { $0.portable?.id == id }) else { throw MediaEngineError.exportFailed }
        var selected = active[index]
        if let layer = selected.portable, !layer.runs.isEmpty,
           selected.giantTitle == nil, selected.discreteReveal == nil, selected.handwriting == nil,
           selected.smoothReveal == nil, selected.staggered == nil, selected.karaoke == nil,
           layer.effect == .none || layer.effect == .static {
            // Keep offscreen glyphs: shrinking must reveal the original text,
            // rather than enlarge the crop of what happened to be visible.
            let painter = try PortableTextVectorPainter(layer: layer, assetURLs: assetURLs,
                canvas: composition.renderSize, preserveOffscreenInk: true)
            selected = RecipeTextLayer(image: try painter.image(maxBitmapBytes: 64 * 1024 * 1024),
                frame: painter.bounds, start: layer.start, end: layer.end, animation: .none,
                selectionBounds: painter.selectionBounds, portable: layer, portableAnchor: painter.anchor)
        }
        let image = try RecipeVideoCompositor.paintText([selected], over: CIImage.empty(), canvas: instruction.canvas, time: time)
        let extent = image.extent.integral
        guard !extent.isEmpty, !extent.isInfinite, extent.width * extent.height <= 16_777_216,
              let cg = CIContext(options: [.cacheIntermediates: false]).createCGImage(image, from: extent) else {
            throw MediaEngineError.exportFailed
        }
        func slice(above: Bool) -> AVVideoComposition {
            let copy = composition.mutableCopy() as! AVMutableVideoComposition
            copy.instructions = (composition.instructions as! [RecipeVideoInstruction]).map { original in
                RecipeVideoInstruction(timeRange: original.timeRange,
                    layers: original.layers.filter { $0.overlayAboveText == above },
                    text: above ? Array(active.suffix(from: index + 1)) : Array(active.prefix(index)),
                    canvas: composition.renderSize, cameraPulses: original.cameraPulses,
                    motionScenes: above ? nil : original.motionScenes, transparentBackground: above)
            }
            return copy
        }
        let canvas = instruction.canvas
        return (slice(above: false), slice(above: true), cg,
            CGRect(x: extent.minX / canvas.width, y: 1 - extent.maxY / canvas.height,
                   width: extent.width / canvas.width, height: extent.height / canvas.height))
    }

    public func updateText(recipe next: EditRecipe, assetURLs nextURLs: [String: URL]? = nil) throws {
        try next.validate()
        let urls = nextURLs ?? assetURLs
        func nonFontReferences(_ value: EditRecipe) -> [RenderAssetReference] {
            (value.assetManifest?.assets ?? []).filter {
                if case .library(catalog: .font, catalogID: _, generation: _) = $0.source { return false }
                return true
            }
        }
        let mediaIDs = Set(nonFontReferences(recipe).map(\.id))
        guard nonFontReferences(recipe) == nonFontReferences(next),
              recipe.assets.filter({ mediaIDs.contains($0.id) }) == next.assets.filter({ mediaIDs.contains($0.id) }),
              mediaIDs.allSatisfy({ urls[$0] == assetURLs[$0] }) else {
            throw NativePreviewFeatureError("LivePreviewComposition-59")
        }
        // Font membership can change while the AV source tracks stay intact.
        for asset in next.assets where !mediaIDs.contains(asset.id) {
            if recipe.assets.contains(asset), urls[asset.id] == assetURLs[asset.id] { continue }
            guard let url = urls[asset.id],
                  try SHA256Fingerprinter().fingerprint(file: url) == asset.fingerprint else {
                throw MediaEngineError.missingAsset(asset.id)
            }
        }
        func withoutGains(_ tracks: [TimelineTrack]) -> [TimelineTrack] {
            tracks.map { track in
                var copy = track
                copy.clips = track.clips.map { clip in var value = clip; value.volume = 1; value.transform = .identity; value.overlayAboveText = nil; value.overlayPopIn = nil; value.overlayPreserveAlpha = nil; value.visualPlacement = nil; value.overlayDissolveSeed = nil; return value }
                return copy
            }
        }
        for clip in next.tracks.flatMap(\.clips) where clip.look != nil {
            guard recipe.tracks.flatMap(\.clips).first(where: { $0.id == clip.id })?.transform == clip.transform else { throw NativePreviewFeatureError("LivePreviewComposition-77") }
        }
        guard withoutGains(recipe.tracks) == withoutGains(next.tracks) else { throw NativePreviewFeatureError("LivePreviewComposition-79") }
        if next.visualFills.contains(where: { $0.kind == .blurPrevious }), recipe.cameraPulses != next.cameraPulses {
            throw NativePreviewFeatureError("LivePreviewComposition-81")
        }
        var expected = recipe
        expected.tracks = next.tracks
        expected.audio.originalVolume = next.audio.originalVolume
        expected.audio.muteWindows = next.audio.muteWindows
        expected.assets = next.assets
        expected.assetManifest = next.assetManifest
        expected.cameraPulses = next.cameraPulses
        expected.motionScenes = next.motionScenes
        expected.visualFills = next.visualFills
        expected.textLayers = next.textLayers
        expected.requiredCapabilities = next.requiredCapabilities
        guard expected == next,
              let current = preview.playerItem.videoComposition,
              let replacement = current.mutableCopy() as? AVMutableVideoComposition else {
            throw NativePreviewFeatureError("LivePreviewComposition-97")
        }
        let instructions = current.instructions.compactMap { $0 as? RecipeVideoInstruction }
        guard instructions.count == current.instructions.count, let first = instructions.first else {
            throw NativePreviewFeatureError("LivePreviewComposition-101")
        }
        let previous = first.text
        var painted = previous.filter { $0.portable == nil }
        let remaining = 64 * 1024 * 1024 - painted.reduce(0) { $0 + $1.bitmapBytes }
        let textStore = try NativeTextLayerStore(layers: next.textLayers, assetURLs: urls, canvas: current.renderSize,
                                               maxBitmapBytes: remaining, reusing: first.textStore)
        _ = try textStore.activeLayers(at: preview.playerItem.currentTime().seconds)
        painted += next.textLayers.map { layer in
            previous.first(where: { $0.portable == layer }) ?? RecipeTextLayer.deferred(layer)
        }
        let overlayOrders = AVPlayerPreviewComposer.overlayOrders(in: next)
        let nextClips = Dictionary(uniqueKeysWithValues: next.tracks.flatMap(\.clips).map { ($0.id, $0) })
        let motion = recipe.motionScenes == next.motionScenes ? first.motionScenes : try NativeMotionPainter.make(next.motionScenes,
            assets: urls, canvas: current.renderSize, duration: TimelineMath.totalDuration(of: next), frameRate: next.frameRate)
        let oldFillIDs = Set(recipe.visualFills.map(\.id))
        var seen = Set<String>()
        let sourceLayers = instructions.flatMap(\.layers).filter { layer in
            if let id = layer.clipID, oldFillIDs.contains(id) { return false }
            return seen.insert("\(layer.trackID ?? -1):\(layer.clipID ?? "clock"):\(layer.start)").inserted
        }
        var layers = try sourceLayers.map { layer in
                guard let id = layer.clipID, let clip = nextClips[id], let size = layer.naturalSize else { return layer }
                var updated = layer
                updated.transform = AVPlayerPreviewComposer.transform(naturalSize: size, preferred: layer.preferredTransform, canvas: current.renderSize, clip: clip)
                if recipe.tracks.flatMap(\.clips).first(where: { $0.id == id })?.overlayDissolveSeed != clip.overlayDissolveSeed {
                    updated.overlayDissolve = try clip.overlayDissolveSeed.map { seed in
                        try NativeDissolveRenderer(width: next.canvas.width, height: next.canvas.height, seed: seed,
                            maxBitmapBytes: 64 * 1024 * 1024, preset: .media)
                    }
                }
                updated.overlayAboveText = clip.overlayAboveText == true
                updated.overlayPopIn = clip.overlayPopIn == true
                updated.overlayPreserveAlpha = clip.overlayPreserveAlpha
                updated.visualPlacement = clip.visualPlacement
                updated.visualOrder = overlayOrders[clip.id] ?? clip.visualPlacement?.order ?? updated.visualOrder
                updated.overlayCenter = CGPoint(x: current.renderSize.width / 2 + clip.transform.positionX, y: current.renderSize.height / 2 - clip.transform.positionY)
                return updated

        }
        for fill in next.visualFills {
            let image: CIImage
            if let previous = recipe.visualFills.first(where: { $0.id == fill.id }), previous == fill,
               let cached = instructions.flatMap(\.layers).first(where: { $0.clipID == fill.id })?.image {
                image = cached
            } else {
                // A blur snapshot depends on the source frame at its start;
                // the asynchronous builder prepares that frame before publishing.
                guard fill.kind != .blurPrevious else { throw NativePreviewFeatureError("LivePreviewComposition-158") }
                image = try fill.image(canvas: current.renderSize)
            }
            layers.append(RecipeVideoLayer(trackID: nil, image: image, transform: .identity,
                start: fill.start, end: fill.end, fadeIn: 0, isPrimary: false, clipID: fill.id,
                visualPlacement: VisualMediaPlacement(order: fill.order, windowStart: fill.start, windowEnd: fill.end,
                    fadeIn: fill.fadeIn, fadeOut: fill.fadeOut), visualOrder: fill.order))
        }
        let total = TimelineMath.totalDuration(of: next)
        let boundaries = Set([0, total] + layers.flatMap { [$0.start, $0.end] }).sorted()
        replacement.instructions = zip(boundaries, boundaries.dropFirst()).map { start, end in
            RecipeVideoInstruction(timeRange: CMTimeRange(start: CMTime(seconds: start, preferredTimescale: 60_000),
                end: CMTime(seconds: end, preferredTimescale: 60_000)),
                layers: layers.filter { $0.start < end && $0.end > start }, text: painted, canvas: current.renderSize,
                cameraPulses: next.cameraPulses, motionScenes: motion, textStore: textStore)
        }
        let mix = AVMutableAudioMix()
        if recipe.audio.musicAssetID != nil {
            guard next.audio == recipe.audio, next.tracks == recipe.tracks else { throw NativePreviewFeatureError("LivePreviewComposition-176") }
            mix.inputParameters = preview.playerItem.audioMix?.inputParameters ?? []
        } else {
            let clips = Dictionary(uniqueKeysWithValues: next.tracks.flatMap(\.clips).map { ($0.id, $0) })
            mix.inputParameters = try preview.audioBindings.map { binding in
                guard let clip = clips[binding.clipID] else { throw NativePreviewFeatureError("LivePreviewComposition-181") }
                let parameter = AVMutableAudioMixInputParameters()
                parameter.trackID = binding.trackID
                applyAudioGain(parameter, clip: clip, gain: binding.usesOriginalGain ? next.audio.originalVolume : 1, windows: next.audio.muteWindows)
                return parameter
            }
        }
        // Publish only after every layer and audio binding validates successfully.
        preview.playerItem.videoComposition = replacement
        preview.playerItem.audioMix = mix
        assetURLs = urls
        recipe = next
    }
}

extension RecipeTextLayer {
    var bitmapBytes: Int {
        let pixelCount = image.extent.width * image.extent.height
        if let dissolve { return dissolve.bitmapBytes + Int(pixelCount * 12) }
        if let giantTitle { return giantTitle.bitmapBytes }
        if let handwriting { return handwriting.bitmapBytes }
        if let karaoke { return karaoke.bitmapBytes }
        if let staggered { return staggered.bitmapBytes }
        if let smoothReveal { return smoothReveal.bitmapBytes }
        return Int(pixelCount * 4) * (discreteReveal == nil ? 1 : 2)
    }
}
#endif
