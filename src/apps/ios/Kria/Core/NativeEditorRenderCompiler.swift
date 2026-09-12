import Foundation
import KriaMediaEngine

struct NativeEditorRenderProgram: Sendable {
    let recipe: KriaMediaEngine.EditRecipe
    let assetURLs: [String: URL]
}

enum NativeEditorRenderError: Error, Equatable {
    case unsupportedLane(String)
    case missingFont(String)
    case missingSource(Int)
}

/// Compiles the public editor document. Internal backend assembly state never
/// participates in local rendering, and an unknown lane cannot silently vanish.
@MainActor final class NativeEditorRenderCompiler {
    private let fontURLs: [String: URL]
    private let fontAliases: [String: String]
    private let fontInstances: [String: [String: Double]]
    private let handwritingLayout: AuthoredHandwritingLayout
    private var fontAssets: [String: MediaAsset] = [:]

    init(fontDirectory: URL) throws {
        handwritingLayout = try AuthoredHandwritingLayout(url: fontDirectory.appendingPathComponent("handwriting-strokes.json"))
        struct Registry: Decodable {
            struct Entry: Decodable { let file: String }
            let fonts: [String: Entry]
        }
        let registry = try JSONDecoder().decode(Registry.self,
            from: Data(contentsOf: fontDirectory.appendingPathComponent("font-registry.json")))
        fontAliases = registry.fonts.mapValues(\.file)
        fontInstances = try JSONDecoder().decode([String: [String: Double]].self, from: Data(contentsOf: fontDirectory.appendingPathComponent("native-font-instances.json")))
        fontURLs = Dictionary(uniqueKeysWithValues: try FileManager.default.contentsOfDirectory(
            at: fontDirectory, includingPropertiesForKeys: nil
        ).filter { ["ttf", "otf"].contains($0.pathExtension.lowercased()) }.map { ($0.lastPathComponent, $0) })
    }

    func compile(document: EditorDocument, clips: [EditorClip], items: [NativeEditorTimelineItem],
                 sources: [Int: ResolvedEditorSource], audioSources: [String: ResolvedEditorSource] = [:], mediaSources: [String: ResolvedEditorSource] = [:]) throws -> NativeEditorRenderProgram {
        for (name, populated) in [
            ("carousel", document.carouselMoment != nil),
        ] where populated { throw NativeEditorRenderError.unsupportedLane(name) }
        guard document.opaqueRecords.values.allSatisfy(\.isEmpty) else {
            #if DEBUG
            NativePreviewDiagnostics.record("unparsed-lanes", fields: Dictionary(uniqueKeysWithValues:
                document.opaqueRecords.map { (String(describing: $0.key), String($0.value.count)) }))
            #endif
            throw NativeEditorRenderError.unsupportedLane("unknown")
        }
        let canvas: KriaMediaEngine.Canvas = switch document.orientation {
        case "landscape": .init(width: 1920, height: 1080)
        case "square": .init(width: 1080, height: 1080)
        default: .init(width: 1080, height: 1920)
        }
        var assets: [String: MediaAsset] = [:]
        var references: [String: RenderAssetReference] = [:]
        var urls: [String: URL] = [:]
        var video: [TimelineClip] = []
        let activeSlots = document.clips.filter { !$0.removed }
        func slot(for clip: EditorClip, index: Int) -> EditorTimelineSlot? {
            if let id = clip.slotID, let value = activeSlots.first(where: { $0.id == id }) { return value }
            return activeSlots.indices.contains(index) ? activeSlots[index] : nil
        }
        for (index, clip) in clips.enumerated() {
            guard let sourceIndex = clip.sourceClipIndex, let source = sources[sourceIndex],
                  let fingerprint = source.asset.fingerprint else {
                throw NativeEditorRenderError.missingSource(clip.sourceClipIndex ?? index)
            }
            let id = "source-\(sourceIndex)"
            assets[id] = MediaAsset(id: id, relativePath: id, fingerprint: fingerprint)
            references[id] = RenderAssetReference(id: id, fingerprint: try RenderFingerprint(fingerprint),
                                                   source: .original(mediaID: source.mediaID))
            urls[id] = source.url
            let authoredSlot = slot(for: clip, index: index)
            guard authoredSlot?.raw["layout"]?.stringValue != "supporting_card" else {
                throw NativeEditorRenderError.unsupportedLane("supporting card")
            }
            guard authoredSlot?.lookAdjustments == nil || authoredSlot?.lookAdjustments?.isEmpty == true,
                  [nil, "none", "golden_hour"].contains(authoredSlot?.lookPreset) else {
                throw NativeEditorRenderError.unsupportedLane("look")
            }
            var transition: Transition?
            if index > 0, let previous = slot(for: clips[index - 1], index: index - 1),
               previous.transitionAfter != "cut" {
                let kind: Transition.Kind
                switch previous.transitionAfter {
                case "crossfade": kind = .crossfade
                case "dip_to_black": kind = .fadeBlack
                case "flash": kind = .fadeWhite
                default: throw NativeEditorRenderError.unsupportedLane("transition")
                }
                transition = Transition(kind: kind, duration: previous.transitionDurationS ?? 0.35)
            }
            let sourceDuration = clip.trimOut - clip.trimIn
            let duration = clip.end - clip.start
            guard duration > 0, sourceDuration > 0 else { throw RecipeError.invalidTimeline }
            video.append(TimelineClip(id: clip.slotID ?? clip.id.uuidString, sourceAssetID: id,
                sourceStart: clip.trimIn, sourceDuration: sourceDuration, timelineStart: clip.start,
                rate: sourceDuration / duration, transition: transition,
                volume: clip.muted || audioSources["narration"] != nil ? 0 : 1, look: authoredSlot?.lookPreset == "golden_hour" ? .goldenHour : nil))
        }
        var audioTracks: [TimelineTrack] = []
        let total = video.map { $0.timelineStart + $0.duration }.max() ?? 0
        func addAudio(id: String, lane: String, start: Double, length: Double, gain: Double, timelineStart: Double = 0, catalog: RenderAssetCatalog = .music) throws {
            guard length > 0, let source = audioSources[id], let fingerprint = source.asset.fingerprint,
                  let available = source.asset.duration, start >= 0, start + length <= available + 0.01 else {
                #if DEBUG
                NativePreviewDiagnostics.record("audio-range-unavailable", fields: ["lane": lane == "music" ? "music" : lane == "background-music" ? "background-music" : "sfx", "present": String(audioSources[id] != nil), "start": String(start), "length": String(length), "available": String(audioSources[id]?.asset.duration ?? -1)])
                #endif
                throw MediaEngineError.missingAsset(id)
            }
            let alias = "audio-\(id)"
            assets[alias] = MediaAsset(id: alias, relativePath: alias, fingerprint: fingerprint, duration: available)
            references[alias] = RenderAssetReference(id: alias, fingerprint: try RenderFingerprint(fingerprint),
                source: .library(catalog: catalog, catalogID: id, generation: fingerprint.hex))
            urls[alias] = source.url
            audioTracks.append(TimelineTrack(id: lane, kind: .audio, clips: [
                TimelineClip(id: lane, sourceAssetID: alias, sourceStart: start, sourceDuration: length, timelineStart: timelineStart, volume: gain)
            ]))
        }
        if let narration = audioSources["narration"] {
            guard let fingerprint = narration.asset.fingerprint, let available = narration.asset.duration,
                  available.isFinite, available > 0 else { throw MediaEngineError.missingAsset("narration") }
            #if DEBUG
            NativePreviewDiagnostics.record("narration-range", fields: ["available": String(available), "timeline": String(total)])
            #endif
            let id = "narration"
            assets[id] = MediaAsset(id: id, relativePath: id, fingerprint: fingerprint, duration: available)
            references[id] = RenderAssetReference(id: id, fingerprint: try RenderFingerprint(fingerprint), source: .original(mediaID: narration.mediaID))
            urls[id] = narration.url
            audioTracks.append(TimelineTrack(id: id, kind: .audio, clips: [
                TimelineClip(id: id, sourceAssetID: id, sourceStart: 0, sourceDuration: min(total, available), timelineStart: 0, volume: 1)
            ]))
        }
        // The rendered narration source already includes its approved music bed.
        if let music = document.music, audioSources["narration"] == nil {
            guard music.alignment == nil || music.alignment == "preserve_cuts" else {
                throw NativeEditorRenderError.unsupportedLane("beat alignment")
            }
            try addAudio(id: music.trackID, lane: "music", start: music.startS, length: total,
                         gain: document.mix["music_level"]?.numberValue ?? 1)
        }
        if let bed = document.backgroundMusic, bed.enabled, !bed.muted, let id = bed.trackID {
            let start = bed.startS ?? 0
            let length = min(total, max(0, (bed.endS ?? (start + total)) - start))
            try addAudio(id: id, lane: "background-music", start: start, length: length, gain: pow(10, (bed.gainDB ?? -18) / 20))
        }
        for effect in document.soundEffects {
            let id = "sfx:" + effect.id
            guard let source = audioSources[id], let available = source.asset.duration,
                  let item = items.first(where: { $0.kind == .soundEffect && $0.id == effect.id }) else {
                throw MediaEngineError.missingAsset(id)
            }
            let start = effect.raw["trim_start_s"]?.numberValue ?? 0
            let end = min(available, effect.raw["trim_end_s"]?.numberValue ?? available)
            let length = min(end - start, total - item.start)
            if length > 0 {
                try addAudio(id: id, lane: id, start: start, length: length,
                    gain: effect.raw["gain"]?.numberValue ?? 1, timelineStart: item.start, catalog: .soundEffect)
            }
        }
        var overlays: [TimelineClip] = []
        for overlay in document.mediaOverlays.sorted(by: { ($0.raw["z"]?.numberValue ?? 0) < ($1.raw["z"]?.numberValue ?? 0) }) {
            let id = "overlay:" + overlay.id
            guard let source = mediaSources[id], let fingerprint = source.asset.fingerprint, let size = source.asset.naturalSize,
                  let item = items.first(where: { $0.kind == .mediaOverlay && $0.id == overlay.id }), item.end > item.start else {
                throw MediaEngineError.missingAsset(id)
            }
            guard [nil, "none", "dissolve-out"].contains(overlay.raw["exit_token"]?.stringValue) else {
                throw NativeEditorRenderError.unsupportedLane("media dissolve")
            }
            guard [nil, "none", "pop_in"].contains(overlay.raw["entrance_token"]?.stringValue) else {
                throw NativeEditorRenderError.unsupportedLane("media entrance")
            }
            assets[id] = MediaAsset(id: id, relativePath: id, fingerprint: fingerprint)
            references[id] = RenderAssetReference(id: id, fingerprint: try RenderFingerprint(fingerprint), source: .original(mediaID: source.mediaID))
            urls[id] = source.url
            let editorStyle = try Self.visualEditorStyle(overlay.raw["editor_style"])
            let fullscreen = overlay.raw["display_mode"]?.stringValue == "fullscreen"
            let width = Double(canvas.width), height = Double(canvas.height)
            let coverWidth = size.width * max(width / size.width, height / size.height)
            let cardWidth = (width * (overlay.raw["scale"]?.numberValue ?? 0.35)).rounded(.toNearestOrEven)
            let fallbackY: Double = switch overlay.raw["position"]?.stringValue {
            case "top": 0.18
            case "bottom": 0.82
            default: 0.5
            }
            let x = overlay.raw["x_frac"]?.numberValue ?? 0.5
            let y = overlay.raw["y_frac"]?.numberValue ?? fallbackY
            let transform: MediaTransform = fullscreen ? .identity : MediaTransform(scale: cardWidth / coverWidth,
                positionX: (x * width).rounded(.toNearestOrEven) - width / 2,
                positionY: (y * height).rounded(.toNearestOrEven) - height / 2)
            let window = item.end - item.start
            let sourceStart = source.asset.duration == nil ? 0 : (overlay.raw["clip_trim_start_s"]?.numberValue ?? 0)
            let sourceEnd = source.asset.duration.map { min($0, overlay.raw["clip_trim_end_s"]?.numberValue ?? $0) }
            let moving = min(window, sourceEnd.map { $0 - sourceStart } ?? window)
            guard moving > 0 else { throw RecipeError.invalidTimeline }
            overlays.append(TimelineClip(id: id, sourceAssetID: id, sourceStart: sourceStart, sourceDuration: moving,
                timelineStart: item.start, transform: transform, volume: 0, holdDuration: max(0, window - moving),
                overlayAboveText: true, overlayPopIn: !fullscreen && overlay.raw["entrance_token"] == .string("pop_in"),
                overlayPreserveAlpha: !fullscreen && source.preserveAlpha,
                visualPlacement: editorStyle.map { VisualMediaPlacement(order: 0, contain: $0.fitMode == "contain", zoom: $0.zoom,
                    widthFraction: fullscreen ? nil : overlay.raw["scale"]?.numberValue ?? 0.35, xFraction: x, yFraction: y,
                    windowStart: item.start, windowEnd: item.end, editorStyle: $0) },
                overlayDissolveSeed: overlay.raw["exit_token"]?.stringValue == "dissolve-out" ? UInt32(211 + (document.mediaOverlays.firstIndex(where: { $0.id == overlay.id }) ?? 0) * 53) : nil))
        }
        func registerFont(_ font: URL) throws -> String {
            let fontID = "font-\(font.lastPathComponent)"
            if fontAssets[fontID] == nil {
                fontAssets[fontID] = MediaAsset(id: fontID, relativePath: fontID,
                    fingerprint: try SHA256Fingerprinter().fingerprint(file: font))
            }
            let asset = fontAssets[fontID]!
            assets[fontID] = asset
            references[fontID] = RenderAssetReference(id: fontID, fingerprint: try RenderFingerprint(asset.fingerprint!),
                source: .library(catalog: .font, catalogID: font.lastPathComponent, generation: asset.fingerprint!.hex))
            urls[fontID] = font
            return fontID
        }
        // Container duration is rounded independently from authored cut sums.
        // Bound text to the actual composition without changing persisted timing.
        let items = items.map { item in
            guard item.kind == .text || item.kind == .captionCue else { return item }
            return NativeEditorTimelineItem(selection: item.selection, start: item.start,
                end: min(item.end, total), zIndex: item.zIndex, sourceIndex: item.sourceIndex)
        }
        var text: [PortableTextLayer] = []
        for element in document.textElements where element.raw["enabled"] != .bool(false) {
            guard let item = items.first(where: { $0.kind == .text && $0.id == element.id }) else {
                throw RecipeError.invalidTimeline
            }
            // Trimming the video can leave authored text entirely beyond the
            // output. Keep that record for undo, but don't compile an empty or
            // reversed render interval after clipping its end to the new cut.
            guard item.start.isFinite, item.end.isFinite else { throw RecipeError.invalidTimeline }
            if item.start >= total { continue }
            guard item.end > item.start else { throw RecipeError.invalidTimeline }
            let family = element.raw["font_family"]?.stringValue ?? "Inter"
            let font = try resolveFont(family)
            let fontID = try registerFont(font)
            let raw = element.raw
            let legacy = raw["effect"]?.stringValue ?? "none"
            if raw["behind_subject"] == .bool(true) || raw["theme_transition"]?.objectValue != nil {
                throw NativeEditorRenderError.unsupportedLane("text subject or scene compositing")
            }
            guard raw["animation_phases"] != nil || ["none", "static", "fade-in", "pop-in", "slide-in", "typewriter", "scale-up", "slide-up", "slide-down", "bounce", "dissolve-out", "stream-in", "smooth-type", "handwriting", "ink-reveal"].contains(legacy) else {
                throw NativeEditorRenderError.unsupportedLane("text effect")
            }
            let phases = try JSONDecoder().decode(TextAnimationPhases.self,
                from: JSONEncoder().encode(NativeEditorSession.textPhases(for: element)))
            var shadows: [TextBlurLayer] = []
            if raw["shadow_enabled"] != .bool(false) {
                if raw["shadow_style"]?.stringValue == "high_visibility" && ["stroke_color", "shadow_color", "shadow_opacity"].allSatisfy({ raw[$0] == nil || raw[$0] == .null }) {
                    shadows = [TextBlurLayer(color: try ink(nil, fallback: "#000000", alpha: 115.0 / 255), sigma: 14, dx: 0, dy: 8),
                               TextBlurLayer(color: try ink(nil, fallback: "#000000", alpha: 200.0 / 255), sigma: 3, dx: 0, dy: 2)]
                } else {
                    shadows.append(TextBlurLayer(color: try ink(raw["shadow_color"], fallback: "#000000",
                        alpha: ((raw["shadow_opacity"]?.numberValue ?? (160.0 / 255)) * 255).rounded(.toNearestOrEven) / 255), sigma: 12, dx: 0, dy: 6))
                }
            }
            if let strength = raw["glow_strength"]?.numberValue, strength > 0, let glow = raw["glow_color"]?.stringValue {
                for (sigma, alpha) in [(8.0, 120.0), (20.0, 220.0)] {
                    shadows.append(TextBlurLayer(color: try ink(.string(glow), fallback: "#FFFFFF",
                        alpha: (alpha * min(1, strength)).rounded(.toNearestOrEven) / 255), sigma: sigma, dx: 0, dy: 0))
                }
            }
            let y: Double = switch raw["position"]?.stringValue {
            case "top": 0.2
            case "bottom": 0.8
            default: 0.5
            }
            let style = AuthoredTextLayout.Style(fontAssetID: fontID, size: NativeEditorSession.textSize(for: element),
                widthFraction: raw["max_width_frac"]?.numberValue ?? 0.9,
                xFraction: raw["x_frac"]?.numberValue ?? 0.5, yFraction: raw["y_frac"]?.numberValue ?? y,
                rotation: raw["rotation_deg"]?.numberValue ?? 0,
                alignment: AuthoredTextLayout.Alignment(rawValue: raw["alignment"]?.stringValue ?? "center") ?? .center,
                color: try ink(raw["color"], fallback: "#FFFFFF"),
                stroke: try ink(raw["stroke_color"], fallback: "#000000"),
                strokeWidth: raw["stroke_width"]?.numberValue ?? 0, shadows: shadows,
                background: try raw["background_color"]?.stringValue.map { try ink(.string($0), fallback: "#FFFFFF") },
                fontVariations: fontInstances[family] ?? [:], letterSpacing: raw["letter_spacing"]?.numberValue ?? 0, lineSpacing: raw["line_spacing"]?.numberValue ?? 1.15, wrapsLines: raw["wrap_lines"] != .bool(false))
            let displayText: String = switch raw["text_case"]?.stringValue {
            case "upper": element.text.uppercased()
            case "lower": element.text.lowercased()
            case "title": Self.titleCase(element.text)
            default: element.text
            }
            let explicitPhases = raw["animation_phases"]?.objectValue != nil
            if legacy == "handwriting" && !explicitPhases {
                text.append(try handwritingLayout.compile(id: element.id, text: displayText, start: item.start, end: item.end,
                    style: style, canvas: canvas, motion: Self.normalizedMotion(effect: legacy, raw: raw["motion"]),
                    lineSpacing: raw["line_spacing"]?.numberValue ?? 1.15))
                continue
            }
            let layout = try AuthoredTextLayout.compile(id: element.id, text: displayText,
                start: item.start, end: item.end, style: style, fontURL: font, canvas: canvas,
                phases: explicitPhases ? phases : nil,
                legacyEffect: [.typewriter, .streamIn, .smoothType, .inkReveal].contains(PortableTextLayer.Effect(rawValue: legacy) ?? .none) ? PortableTextLayer.Effect(rawValue: legacy)! : .none,
                motion: explicitPhases ? nil : try Self.normalizedMotion(effect: legacy, raw: raw["motion"]))
            if explicitPhases || ["typewriter", "stream-in", "smooth-type", "ink-reveal"].contains(legacy) {
                text.append(layout)
            } else {
                text.append(PortableTextLayer(id: layout.id, start: layout.start, end: layout.end,
                    anchorX: layout.anchorX, anchorY: layout.anchorY, rotationDegrees: layout.rotationDegrees,
                    runs: layout.runs, effect: PortableTextLayer.Effect(rawValue: legacy) ?? .none,
                    motion: try Self.normalizedMotion(effect: legacy, raw: raw["motion"]),
                    dissolveSeed: legacy == "dissolve-out" ? UInt32(101 + (document.textElements.firstIndex(where: { $0.id == element.id }) ?? 0) * 37) : nil,
                    background: layout.background))
            }
        }
        if !document.captionCues.isEmpty && document.captionMeta["enabled"] != .bool(false) {
            let meta = document.captionMeta
            let captionFont = ["Inter": "Inter-Bold", "Fraunces": "Fraunces-Bold", "Space Grotesk": "SpaceGrotesk-Bold"]
            let family = meta["font"]?.stringValue ?? "TikTokSans-Bold"
            let font = try resolveFont(captionFont[family] ?? family)
            let fontID = try registerFont(font)
            let wordStyle = meta["style"]?.stringValue == "word"
            let appearance = meta["appearance"]?.objectValue ?? [:]
            let explicitHighlight = appearance["highlight_spoken_word"]?.boolValue
            let captionAlignment = AuthoredTextLayout.Alignment(rawValue: appearance["alignment"]?.stringValue ?? "center") ?? .center
            let captionX = captionAlignment == .left ? 80.0 / 1080 : captionAlignment == .right ? 1000.0 / 1080 : 0.5
            let captionStyle = AuthoredTextLayout.Style(fontAssetID: fontID, size: meta["size_px"]?.numberValue ?? 78,
                widthFraction: 920.0 / 1080, xFraction: captionX, yFraction: meta["y_frac"]?.numberValue ?? (document.editFormat == "subtitled" ? 0.82 : 1740.0 / 1920),
                alignment: captionAlignment,
                color: try ink(meta["color"], fallback: "#FFFFFF"), stroke: try ink(appearance["stroke_color"], fallback: "#000000"), strokeWidth: meta["stroke_width"]?.numberValue ?? 4,
                shadows: meta["shadow_enabled"] == .bool(false) ? [] : [TextBlurLayer(color: try ink(appearance["shadow_color"], fallback: "#000000", alpha: appearance["shadow_opacity"]?.numberValue ?? 0.5), sigma: 0, dx: 1, dy: 1)],
                bottomAligned: true, fontVariations: fontInstances[family] ?? [:])
            var previousWordEnd = 0.0
            for cue in document.captionCues where !cue.text.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty {
                guard let item = items.first(where: { $0.kind == .captionCue && $0.id == cue.id }),
                      item.start.isFinite, item.end.isFinite else {
                    throw RecipeError.invalidTimeline
                }
                if item.start >= total { continue }
                guard item.end > item.start else { throw RecipeError.invalidTimeline }
                if wordStyle || explicitHighlight == true {
                    let tokens = cue.text.split(whereSeparator: \.isWhitespace).map(String.init)
                    let wordValues: [JSONValue]
                    if case .array(let values) = cue.raw["words"] { wordValues = values } else { wordValues = [] }
                    let stored = wordValues.compactMap { $0.objectValue }
                    let matches = stored.map { $0["text"]?.stringValue?.trimmingCharacters(in: .whitespacesAndNewlines) ?? "" } == tokens
                    var windows: [(Double, Double)] = []
                    let scale = (item.end - item.start) / max(0.01, cue.endS - cue.startS)
                    var synthesizedEnd = 0.0
                    for index in tokens.indices {
                        if matches, let begin = stored[index]["start_s"]?.numberValue, let end = stored[index]["end_s"]?.numberValue {
                            windows.append((item.start + (begin - cue.startS) * scale, item.start + (end - cue.startS) * scale))
                        } else {
                            let next = max(Double(index + 1) * (item.end - item.start) / Double(tokens.count), synthesizedEnd + 0.05)
                            windows.append((item.start + synthesizedEnd, item.start + next))
                            synthesizedEnd = next
                        }
                    }
                    var starts: [Double] = []
                    for index in windows.indices {
                        let start = max(0, windows[index].0, previousWordEnd)
                        let next = index + 1 < windows.count ? windows[index + 1].0 : windows[index].1
                        previousWordEnd = max(start + 0.01, next)
                        starts.append(start)
                    }
                    guard let first = starts.first, first < total else { continue }
                    if wordStyle, let highlighted = explicitHighlight {
                        for index in tokens.indices {
                            let start = max(item.start, starts[index])
                            let end = min(total, item.end, index + 1 < starts.count ? starts[index + 1] : previousWordEnd)
                            guard end > start else { continue }
                            var style = captionStyle
                            if highlighted { style.color = try ink(meta["highlight_color"], fallback: "#C5F82A") }
                            text.append(try AuthoredTextLayout.compile(id: "caption-" + cue.id + "-word-" + String(index),
                                text: tokens[index], start: start, end: end, style: style, fontURL: font, canvas: canvas))
                        }
                        continue
                    }
                    text.append(try AuthoredTextLayout.compileHighlightedWords(id: "caption-" + cue.id, text: cue.text,
                        start: first, end: min(total, explicitHighlight == nil ? previousWordEnd : min(item.end, previousWordEnd)), starts: starts.map { $0 - first },
                        highlight: try ink(meta["highlight_color"], fallback: "#C5F82A"), style: captionStyle, fontURL: font, canvas: canvas))
                } else {
                    let layout = try AuthoredTextLayout.compile(id: "caption-" + cue.id, text: cue.text,
                        start: item.start, end: item.end, style: captionStyle, fontURL: font, canvas: canvas)
                    text.append(PortableTextLayer(id: layout.id, start: layout.start, end: layout.end,
                        anchorX: layout.anchorX, anchorY: layout.anchorY, rotationDegrees: 0, runs: layout.runs,
                        effect: document.editFormat == "subtitled" ? .captionPop : .none))
                }
            }
        }
        var visualFills: [VisualCanvasFill] = []
        var muteWindows: [AudioMuteWindow] = []
        let structured = document.visualBlocks.filter { $0.kind != "media" }
        let mediaBlocks = document.visualBlocks.filter { $0.kind == "media" }.sorted {
            let a = $0.raw["z"]?.numberValue ?? 0, b = $1.raw["z"]?.numberValue ?? 0
            return a == b ? ($0.startS == $1.startS ? $0.id < $1.id : $0.startS < $1.startS) : a < b
        }
        for (index, block) in (structured + mediaBlocks).enumerated() {
            let item = items.first(where: { $0.kind == .visualBlock && $0.id == block.id })
            let start = item?.start ?? block.startS, end = min(total, item?.end ?? block.endS)
            guard end > start else { throw RecipeError.invalidTimeline }
            let policy = block.raw["audio_policy"]?.objectValue ?? [:]
            let sfxIDs = Set(document.soundEffects.map { "sfx:" + $0.id })
            var muted: [String] = []
            if policy["base"]?.stringValue == "mute" { muted += video.map(\.id) + audioTracks.flatMap(\.clips).filter { !sfxIDs.contains($0.id) }.map(\.id) }
            if policy["sfx"]?.stringValue == "mute" { muted += audioTracks.flatMap(\.clips).filter { sfxIDs.contains($0.id) }.map(\.id) }
            if !muted.isEmpty { muteWindows.append(AudioMuteWindow(start: start, end: end, clipIDs: muted)) }
            let order = index + 1
            let fadeIn = block.raw["transition_in"]?.stringValue == "fade"
            let fadeOut = block.raw["transition_out"]?.stringValue == "fade"
            func addShot(_ shot: [String: JSONValue], isMedia: Bool = false) throws {
                guard let shotID = shot["id"]?.stringValue,
                      let source = mediaSources["visual:" + block.id + ":" + shotID],
                      let fingerprint = source.asset.fingerprint else { throw MediaEngineError.missingAsset("visual:" + block.id) }
                let alias = "visual-" + block.id + "-" + shotID
                assets[alias] = MediaAsset(id: alias, relativePath: alias, fingerprint: fingerprint)
                references[alias] = RenderAssetReference(id: alias, fingerprint: try RenderFingerprint(fingerprint), source: .original(mediaID: source.mediaID))
                urls[alias] = source.url
                let image = shot[isMedia ? "media_kind" : "kind"]?.stringValue == "image"
                let shotStart = start + (isMedia ? 0 : shot["start_offset_s"]?.numberValue ?? 0)
                let length = min(end - shotStart, isMedia ? end - start : shot["duration_s"]?.numberValue ?? end - start)
                let sourceStart = image ? 0 : shot["trim_start_s"]?.numberValue ?? 0
                guard length > 0, image || (source.asset.duration ?? 0) + 0.001 >= sourceStart + length else { throw RecipeError.invalidTimeline }
                let crop = shot[isMedia ? "transform" : "crop"]?.objectValue ?? [:]
                let motion = VisualMediaPlacement.Motion(rawValue: shot["motion"]?.stringValue ?? "none")
                guard let motion else { throw NativeEditorRenderError.unsupportedLane("visual motion") }
                let placement = VisualMediaPlacement(order: order,
                    contain: isMedia && (crop["fit_mode"]?.stringValue ?? "contain") == "contain",
                    focalX: crop[isMedia ? "focal_x" : "x_frac"]?.numberValue ?? 0.5,
                    focalY: crop[isMedia ? "focal_y" : "y_frac"]?.numberValue ?? 0.5,
                    zoom: crop[isMedia ? "zoom" : "scale"]?.numberValue ?? 1,
                    preCrop: !isMedia && (image || motion != .none), motion: motion,
                    widthFraction: isMedia && shot["display_mode"]?.stringValue == "overlay" ? shot["scale"]?.numberValue ?? 0.35 : nil,
                    xFraction: shot["x_frac"]?.numberValue ?? 0.5, yFraction: shot["y_frac"]?.numberValue ?? 0.5,
                    windowStart: start, windowEnd: end, fadeIn: fadeIn, fadeOut: fadeOut, editorStyle: try Self.visualEditorStyle(block.raw["editor_style"]))
                overlays.append(TimelineClip(id: alias, sourceAssetID: alias, sourceStart: sourceStart, sourceDuration: length,
                    timelineStart: shotStart, volume: 0, visualPlacement: placement))
            }
            switch block.kind {
            case "media": try addShot(block.raw.merging(["id": .string(block.id)]) { _, new in new }, isMedia: true)
            case "montage":
                guard case .array(let shots) = block.raw["shots"] else { throw RecipeError.invalidTimeline }
                for shot in shots { guard let fields = shot.objectValue else { throw RecipeError.invalidTimeline }; try addShot(fields) }
            case "text_card":
                guard let background = block.raw["background"]?.objectValue else { throw RecipeError.invalidTimeline }
                if background["type"]?.stringValue == "asset" {
                    guard let shot = background["shot"]?.objectValue else { throw RecipeError.invalidTimeline }
                    try addShot(shot)
                } else {
                    guard let kind = VisualCanvasFill.Kind(rawValue: background["type"]?.stringValue ?? "") else { throw RecipeError.invalidTimeline }
                    visualFills.append(VisualCanvasFill(id: "fill-" + block.id, start: start, end: end, order: order, kind: kind,
                        color: try ink(background[kind == .gradient ? "from" : "color"], fallback: "#111111"),
                        endColor: kind == .gradient ? try ink(background["to"], fallback: "#111111") : nil,
                        angle: background["angle_deg"]?.numberValue ?? 180, blurRadius: background["blur_px"]?.numberValue ?? 24,
                        fadeIn: fadeIn, fadeOut: fadeOut))
                }
            default: throw NativeEditorRenderError.unsupportedLane("visual block")
            }
        }
        let cameraPulses = try document.cameraEffects.map { effect -> CameraPulse in
            guard [nil, "semantic_crop_pulse"].contains(effect.raw["token"]?.stringValue),
                  [nil, "sine_pulse"].contains(effect.raw["easing"]?.stringValue),
                  let item = items.first(where: { $0.kind == .cameraEffect && $0.id == effect.id }) else {
                throw NativeEditorRenderError.unsupportedLane("camera effect")
            }
            return CameraPulse(id: effect.id, start: item.start, end: item.end, intensity: effect.raw["intensity"]?.numberValue ?? 0.04)
        }
        var motionProgram: MotionSceneProgram?
        if !document.motionScenes.isEmpty {
            guard let hash = document.motionRuntimeHash else { throw NativeEditorRenderError.unsupportedLane("motion runtime") }
            var imageAssets: [String: String] = [:]
            let instances: [[String: JSONValue]] = try document.motionScenes.map { scene in
                guard let item = items.first(where: { $0.kind == .motionScene && $0.id == scene.id }) else { throw RecipeError.invalidTimeline }
                var raw = scene.raw
                raw["start_frame"] = .number((item.start * 30).rounded())
                raw["end_frame_exclusive"] = .number((item.end * 30).rounded())
                if case .object(var params) = raw["params"], case .array(let refs) = params["assets"] {
                    params["assets"] = .array(try refs.map { ref in
                        guard case .object(let fields) = ref, let assetID = fields["asset_id"]?.stringValue,
                              let source = mediaSources["motion:" + assetID], let fingerprint = source.asset.fingerprint else {
                            throw NativeEditorRenderError.unsupportedLane("motion image")
                        }
                        let id = "motion-" + assetID
                        assets[id] = MediaAsset(id: id, relativePath: id, fingerprint: fingerprint)
                        references[id] = RenderAssetReference(id: id, fingerprint: try RenderFingerprint(fingerprint), source: .original(mediaID: source.mediaID))
                        urls[id] = source.url; imageAssets[assetID] = id
                        return .object(["asset_id": .string(assetID)])
                    })
                    raw["params"] = .object(params)
                }
                return raw
            }
            motionProgram = MotionSceneProgram(instances: try JSONDecoder().decode([MotionSceneValue].self, from: JSONEncoder().encode(instances)),
                runtimeHash: hash, fontAssetID: try registerFont(resolveFont("Inter")), imageAssetIDs: imageAssets)
        }
        let recipe = KriaMediaEngine.EditRecipe(schemaVersion: 2, rendererVersion: "kria-ios-2", canvas: canvas,
            assets: assets.values.sorted { $0.id < $1.id }, tracks: [TimelineTrack(id: "video", kind: .video, clips: video)] + (overlays.isEmpty ? [] : [TimelineTrack(id: "overlays", kind: .overlay, clips: overlays)]) + audioTracks,
            audio: AudioMixRecipe(originalVolume: document.mix["original_level"]?.numberValue ?? 1, muteWindows: muteWindows),
            assetManifest: RenderAssetManifest(assets: references.values.sorted { $0.id < $1.id }), textLayers: text, cameraPulses: cameraPulses, motionScenes: motionProgram, visualFills: visualFills)
        try recipe.validate()
        return NativeEditorRenderProgram(recipe: recipe, assetURLs: urls)
    }

    static func visualEditorStyle(_ raw: JSONValue?) throws -> VisualEditorStyle? {
        guard let raw, raw != .null else { return nil }
        let style = try JSONDecoder().decode(VisualEditorStyle.self, from: JSONEncoder().encode(raw))
        try style.validate()
        return style
    }

    /// Matches text_motion_v2.normalize_text_motion; absent v2 motion retains legacy timing.
    static func normalizedMotion(effect: String, raw: JSONValue?) throws -> TextMotionParameters? {
        guard let values = raw?.objectValue, values["version"]?.numberValue == 2 else { return nil }
        let smooth = effect == "smooth-type"
        var normalized: [String: JSONValue] = [:]
        func number(_ source: String, _ target: String, _ fallback: Double, _ low: Double, _ high: Double) {
            let value = values[source]?.numberValue ?? fallback
            normalized[target] = .number(min(high, max(low, value.isFinite ? value : fallback)))
        }
        func choice(_ source: String, _ target: String, _ fallback: String, _ allowed: [String]) {
            let value = values[source]?.stringValue ?? fallback
            normalized[target] = .string(allowed.contains(value) ? value : fallback)
        }
        number("speed", "speed", 1, 0.25, 4)
        number("intensity", "intensity", smooth ? 0.7 : 1, 0, 1)
        number("stagger_ms", "staggerMs", smooth ? 45 : 0, 0, 250)
        number("travel_px", "travelPx", smooth ? 18 : (["slide-up", "slide-down"].contains(effect) ? 220 : 0), 0, 600)
        number("overshoot", "overshoot", ["pop-in", "bounce"].contains(effect) ? 0.15 : 0, 0, 1)
        number("blur_px", "blurPx", smooth ? 4 : 0, 0, 12)
        number("cursor_blink_ms", "cursorBlinkMs", 500, 100, 2000)
        number("hold_s", "holdS", 1, 0, 3600)
        number("exit_s", "exitS", effect == "dissolve-out" ? 1 : 0, 0, 2)
        number("reveal_ramp_ms", "revealRampMs", 120, 40, 400)
        choice("easing", "easing", "ease-out-cubic", ["linear", "ease-out-cubic", "ease-in-out-cubic"])
        choice("order", "order", "forward", ["forward", "reverse", "center-out"])
        choice("direction", "direction", effect == "slide-down" ? "down" : (smooth || effect == "slide-up" ? "up" : "none"), ["none", "up", "down", "left", "right"])
        choice("cursor_style", "cursorStyle", effect == "stream-in" ? "bar" : "none", ["none", "bar", "block", "underscore"])
        return try JSONDecoder().decode(TextMotionParameters.self, from: JSONEncoder().encode(normalized))
    }

    private static func titleCase(_ text: String) -> String {
        let pattern = try! NSRegularExpression(pattern: #"\S+"#)
        var output = text
        for match in pattern.matches(in: text, range: NSRange(text.startIndex..., in: text)).reversed() {
            guard let range = Range(match.range, in: output) else { continue }
            let word = String(output[range])
            output.replaceSubrange(range, with: word.prefix(1).uppercased() + word.dropFirst().lowercased())
        }
        return output
    }

    private func resolveFont(_ family: String) throws -> URL {
        for name in [fontAliases[family], family + ".ttf", family + "-Regular.ttf", family + ".otf"].compactMap({ $0 }) {
            if let url = fontURLs[name] { return url }
        }
        throw NativeEditorRenderError.missingFont(family)
    }

    private func ink(_ value: JSONValue?, fallback: String, alpha: Double = 1) throws -> TextInk {
        let hex = value?.stringValue ?? fallback
        guard hex.count == 7, hex.first == "#", let color = UInt32(hex.dropFirst(), radix: 16) else {
            throw RecipeError.invalidTimeline
        }
        return TextInk(red: Double((color >> 16) & 255) / 255,
                       green: Double((color >> 8) & 255) / 255, blue: Double(color & 255) / 255, alpha: alpha)
    }
}
