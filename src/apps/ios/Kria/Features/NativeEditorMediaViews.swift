import AVFoundation
import AVKit
import KriaMediaEngine
import SwiftUI

private func nativeEffectName(_ raw: [String: JSONValue], fallback: String) -> String {
    raw["kind"]?.stringValue
        ?? raw["effect"]?.stringValue
        ?? raw["preset_id"]?.stringValue
        ?? raw["preset"]?.stringValue
        ?? raw["name"]?.stringValue
        ?? fallback
}

private func nativeTextPosition(_ layer: EditorTextElement) -> CGPoint {
    CGPoint(
        x: min(max(layer.raw["x_frac"]?.numberValue ?? 0.5, 0), 1),
        y: min(max(layer.raw["y_frac"]?.numberValue ?? 0.5, 0), 1)
    )
}

private func nativeRawNumber(_ raw: [String: JSONValue], _ key: String) -> Double? {
    raw[key]?.numberValue ?? nativeRawTransformValue(raw, key)?.numberValue
}

private func nativeRawString(_ raw: [String: JSONValue], _ key: String) -> String? {
    raw[key]?.stringValue ?? nativeRawTransformValue(raw, key)?.stringValue
}

private func nativeBool(_ value: JSONValue?) -> Bool? {
    guard case let .bool(result)? = value else { return nil }
    return result
}

private func nativeRawTransformValue(_ raw: [String: JSONValue], _ key: String) -> JSONValue? {
    guard case let .object(transform)? = raw["transform"] else { return nil }
    return transform[key]
}

private func nativeVisualPosition(_ raw: [String: JSONValue], fallback: CGPoint) -> CGPoint {
    CGPoint(
        x: min(max(nativeRawNumber(raw, "x_frac") ?? fallback.x, 0), 1),
        y: min(max(nativeRawNumber(raw, "y_frac") ?? fallback.y, 0), 1)
    )
}

private func nativeVisualScale(_ raw: [String: JSONValue], fallback: CGFloat = 0.35) -> CGFloat {
    CGFloat(min(max(nativeRawNumber(raw, "scale") ?? Double(fallback), 0.05), 1.5))
}

private func nativeIsFullscreen(_ raw: [String: JSONValue]) -> Bool {
    [raw["display_mode"]?.stringValue, raw["mode"]?.stringValue, nativeRawString(raw, "display_mode")]
        .compactMap { $0 }
        .contains { $0 == "fullscreen" || $0 == "full" }
}

@MainActor
func nativePersistedTimelineItems(for session: NativeEditorSession) -> [NativeEditorTimelineItem] {
    session.timelineItems.sorted {
        if $0.start != $1.start { return $0.start < $1.start }
        if $0.end != $1.end { return $0.end < $1.end }
        if $0.zIndex != $1.zIndex { return $0.zIndex < $1.zIndex }
        if $0.sourceIndex != $1.sourceIndex { return $0.sourceIndex < $1.sourceIndex }
        if $0.kind != $1.kind { return $0.kind.rawValue < $1.kind.rawValue }
        return $0.id < $1.id
    }
}

/// The video surface for the native editor. The player is intentionally owned
/// by NativeEditorSession: this view is only a playback surface and never
/// invents a local preview when the session has no playable item.
struct NativeVideoPreview: View {
    @ObservedObject var session: NativeEditorSession
    @ObservedObject private var clock: NativeEditorPlaybackClock
    @State private var cachedObjects: [NativeEditorPreviewObject] = []
    @State private var didCacheObjects = false
    @State private var lastTapIDs: [EditorSelection] = []
    @State private var lastTapPoint: CGPoint?
    @State private var directMoveObjectID: String?
    @State private var directMoveBaseline: CGPoint?
    @State private var directResizeObjectID: String?
    @State private var directResizeBaseline: CGFloat?

    init(session: NativeEditorSession) {
        self.session = session
        _clock = ObservedObject(wrappedValue: session.playbackClock)
    }

    private var previewKind: String {
        guard let player = session.player, let item = player.currentItem else {
            return ProcessInfo.processInfo.arguments.contains("-ui-testing-editor") ? "Reference frame" : "Preview unavailable"
        }
        guard let asset = item.asset as? AVURLAsset else { return "Cloud preview" }
        return asset.url.isFileURL ? "Local preview" : "Cloud preview"
    }

    private var objects: [NativeEditorPreviewObject] {
        didCacheObjects ? cachedObjects : makeObjects()
    }

    private func makeObjects() -> [NativeEditorPreviewObject] {
        let document = session.document
        let captionsEnabled = nativeBool(document.captionMeta["enabled"]) ?? !document.captionCues.isEmpty
        return nativePersistedTimelineItems(for: session).compactMap { item -> NativeEditorPreviewObject? in
            switch item.kind {
            case .text:
                let layer = document.textElements.first { $0.id == item.id }
                return NativeEditorPreviewObject(
                    item: item,
                    text: layer?.text,
                    position: layer.map(nativeTextPosition) ?? CGPoint(x: 0.5, y: 0.5),
                    style: layer?.raw["font_family"]?.stringValue,
                    title: "Text",
                    render: .text,
                    detail: nil,
                    scale: CGFloat(min(max(layer.flatMap { nativeRawNumber($0.raw, "max_width_frac") } ?? 0.84, 0.2), 1))
                )
            case .captionCue:
                guard captionsEnabled else { return nil }
                return NativeEditorPreviewObject(
                    item: item,
                    text: document.captionCues.first(where: { $0.id == item.id })?.text,
                    position: nil,
                    style: nil,
                    title: "Caption",
                    render: .caption,
                    detail: nil
                )
            case .soundEffect:
                let effect = document.soundEffects.first { $0.id == item.id }
                return NativeEditorPreviewObject(
                    item: item,
                    text: nil,
                    position: CGPoint(x: 0.5, y: 0.18),
                    style: nil,
                    title: "Sound effect (audio)",
                    render: .runtimeOnly,
                    detail: effect.map { nativeEffectName($0.raw, fallback: $0.kind ?? "Sound effect") }
                )
            case .mediaOverlay:
                let overlay = document.mediaOverlays.first { $0.id == item.id }
                return NativeEditorPreviewObject(
                    item: item, text: nil,
                    position: overlay.map { nativeVisualPosition($0.raw, fallback: CGPoint(x: 0.78, y: 0.25)) },
                    style: nil,
                    title: "Media overlay",
                    render: .mediaOverlay,
                    detail: overlay.map { nativeEffectName($0.raw, fallback: $0.kind ?? "Overlay") },
                    scale: overlay.map { nativeVisualScale($0.raw) } ?? 0.35,
                    fullscreen: overlay.map { nativeIsFullscreen($0.raw) } ?? false
                )
            case .visualBlock:
                let block = document.visualBlocks.first { $0.id == item.id }
                return NativeEditorPreviewObject(
                    item: item, text: nil,
                    position: block.map { nativeVisualPosition($0.raw, fallback: CGPoint(x: 0.5, y: 0.25)) },
                    style: nil,
                    title: "Visual block",
                    render: .visualBlock,
                    detail: block?.kind,
                    scale: block.map { nativeVisualScale($0.raw) } ?? 0.35,
                    fullscreen: block.map { nativeIsFullscreen($0.raw) } ?? false
                )
            case .carousel:
                return NativeEditorPreviewObject(item: item, text: nil, position: CGPoint(x: 0.5, y: 0.78), style: nil, title: "Carousel moment", render: .carousel, detail: nil)
            case .motionScene:
                let scene = document.motionScenes.first { $0.id == item.id }
                return NativeEditorPreviewObject(item: item, text: nil, position: CGPoint(x: 0.5, y: 0.18), style: nil, title: "Motion (final render)", render: .runtimeOnly, detail: scene?.preset)
            case .cameraEffect:
                let effect = document.cameraEffects.first { $0.id == item.id }
                return NativeEditorPreviewObject(item: item, text: nil, position: CGPoint(x: 0.5, y: 0.18), style: nil, title: "Camera effect (final render)", render: .runtimeOnly, detail: effect?.effect)
            default:
                return nil
            }
        }
    }

    private func refreshObjects() {
        cachedObjects = makeObjects()
        didCacheObjects = true
    }

    private func frame(for object: NativeEditorPreviewObject, in size: CGSize) -> CGRect {
        if object.fullscreen {
            return CGRect(origin: .zero, size: size).insetBy(dx: 6, dy: 6)
        }
        if let position = object.position {
            let width = min(size.width * object.scale, max(44, size.width - 24))
            let height = max(44, 54 * object.scale)
            let center = CGPoint(
                x: min(max(width / 2, position.x * size.width), size.width - width / 2),
                y: min(max(height / 2, position.y * size.height), size.height - height / 2)
            )
            return CGRect(x: center.x - width / 2, y: center.y - height / 2, width: width, height: height)
        }
        return CGRect(x: 12, y: size.height - 86, width: max(44, size.width - 24), height: 54)
    }

    private func selectPreviewObject(at point: CGPoint, in size: CGSize) {
        let candidates = NativeEditorInteraction.previewOrder(
            NativeEditorInteraction.visible(objects.map(\.item), at: clock.currentTime)
        ).reversed().filter { item in
            guard let object = objects.first(where: { $0.item.selection == item.selection }) else { return false }
            return NativeEditorInteraction.hitRect(frame(for: object, in: size)).contains(point)
        }
        guard !candidates.isEmpty else {
            lastTapIDs = []
            lastTapPoint = nil
            return
        }
        let ordered = candidates.map(\.selection)
        let next: EditorSelection
        let pointIsNearPrevious = lastTapPoint.map { hypot($0.x - point.x, $0.y - point.y) <= 12 } ?? false
        if pointIsNearPrevious, ordered == lastTapIDs, let selected = session.selection, let index = ordered.firstIndex(of: selected) {
            next = ordered[(index + 1) % ordered.count]
        } else {
            next = ordered[0]
        }
        lastTapIDs = ordered
        lastTapPoint = point
        session.select(next, seekToStart: false)
    }

    private func directMoveCandidate(at point: CGPoint, in size: CGSize) -> NativeEditorPreviewObject? {
        NativeEditorInteraction.previewOrder(
            NativeEditorInteraction.visible(objects.map(\.item), at: clock.currentTime)
        )
        .reversed()
        .compactMap { item in objects.first(where: { $0.item.selection == item.selection }) }
        .first { object in
            canDirectlyPosition(object)
                && NativeEditorInteraction.hitRect(frame(for: object, in: size)).contains(point)
        }
    }

    private func directMoveGesture(in size: CGSize) -> some Gesture {
        DragGesture(minimumDistance: 4, coordinateSpace: .local)
            .onChanged { value in
                if directMoveObjectID == nil {
                    guard let object = directMoveCandidate(at: value.startLocation, in: size),
                          let position = object.position else { return }
                    directMoveObjectID = object.id
                    directMoveBaseline = position
                    session.beginDirectManipulation()
                    session.select(object.item, seekToStart: false)
                }
                guard let objectID = directMoveObjectID,
                      let baseline = directMoveBaseline,
                      let object = objects.first(where: { $0.id == objectID }),
                      let onMove = positionHandler(for: object),
                      size.width > 0, size.height > 0 else { return }
                onMove(
                    CGPoint(
                        x: min(max(0, baseline.x + value.translation.width / size.width), 1),
                        y: min(max(0, baseline.y + value.translation.height / size.height), 1)
                    )
                )
            }
            .onEnded { _ in
                guard directMoveObjectID != nil else { return }
                directMoveObjectID = nil
                directMoveBaseline = nil
                if directResizeObjectID == nil { session.endDirectManipulation() }
            }
    }

    private func directResizeGesture() -> some Gesture {
        MagnificationGesture()
            .onChanged { value in
                if directResizeObjectID == nil {
                    guard let selection = session.selection,
                          let object = objects.first(where: { $0.item.selection == selection }),
                          scaleHandler(for: object) != nil else { return }
                    directResizeObjectID = object.id
                    directResizeBaseline = object.scale
                    session.beginDirectManipulation()
                }
                guard let objectID = directResizeObjectID,
                      let baseline = directResizeBaseline,
                      let object = objects.first(where: { $0.id == objectID }),
                      let onResize = scaleHandler(for: object) else { return }
                onResize(baseline * value)
            }
            .onEnded { _ in
                guard directResizeObjectID != nil else { return }
                directResizeObjectID = nil
                directResizeBaseline = nil
                if directMoveObjectID == nil { session.endDirectManipulation() }
            }
    }

    private func canDirectlyPosition(_ object: NativeEditorPreviewObject) -> Bool {
        switch object.item.kind {
        case .text:
            return session.canEdit(.text)
        case .mediaOverlay:
            return !object.fullscreen && session.canEdit(.mediaOverlays)
        case .visualBlock:
            guard !object.fullscreen,
                  session.canEdit(.visualBlocks),
                  let block = session.document.visualBlocks.first(where: { $0.id == object.item.id }) else { return false }
            return block.kind == "media" && block.raw["display_mode"]?.stringValue == "overlay"
        default:
            return false
        }
    }

    private func positionHandler(for object: NativeEditorPreviewObject) -> ((CGPoint) -> Void)? {
        guard canDirectlyPosition(object) else { return nil }
        switch object.item.kind {
        case .text:
            return { point in session.setTextPosition(id: object.item.id, x: point.x, y: point.y) }
        case .mediaOverlay:
            return { point in session.setMediaOverlayPosition(id: object.item.id, x: point.x, y: point.y) }
        case .visualBlock:
            return { point in session.setVisualBlockOverlayLayout(id: object.item.id, x: point.x, y: point.y) }
        default:
            return nil
        }
    }

    private func scaleHandler(for object: NativeEditorPreviewObject) -> ((CGFloat) -> Void)? {
        guard canDirectlyPosition(object) else { return nil }
        switch object.item.kind {
        case .text:
            return { scale in session.setTextWidth(id: object.item.id, width: scale) }
        case .mediaOverlay:
            return { scale in session.setMediaOverlayScale(id: object.item.id, scale: scale) }
        case .visualBlock:
            return { scale in session.setVisualBlockOverlayLayout(id: object.item.id, scale: scale) }
        default:
            return nil
        }
    }

    var body: some View {
        ZStack(alignment: .topLeading) {
            Color.black
            if let player = session.player {
                VideoPlayer(player: player)
                    .aspectRatio(9 / 16, contentMode: .fit)
                    .frame(maxWidth: .infinity, maxHeight: .infinity)
                    .accessibilityLabel("Video preview")
            } else if ProcessInfo.processInfo.arguments.contains("-ui-testing-editor") {
                BundledPosterImage(name: "montage")
                    .scaledToFill()
                    .frame(maxWidth: .infinity, maxHeight: .infinity)
                    .clipped()
                    .overlay(.black.opacity(0.18))
                    .accessibilityLabel("Reference frame")
            } else {
                VStack(spacing: 12) {
                    Image(systemName: "film.stack")
                        .font(.system(size: 30, weight: .medium))
                    Text("Preview unavailable")
                        .font(KriaFont.body(14).weight(.semibold))
                    Text("Add a clip or wait for the preview to load.")
                        .font(KriaFont.body(12))
                        .multilineTextAlignment(.center)
                        .foregroundStyle(.white.opacity(0.65))
                }
                .foregroundStyle(.white)
                .frame(maxWidth: .infinity, maxHeight: .infinity)
                .padding(24)
            }

            GeometryReader { proxy in
                let visible = NativeEditorInteraction.previewOrder(
                    NativeEditorInteraction.visible(objects.map(\.item), at: clock.currentTime)
                )
                ZStack(alignment: .topLeading) {
                    ForEach(visible.compactMap { item in objects.first(where: { $0.item == item }) }) { object in
                        NativePreviewObjectView(
                            object: object,
                            frame: frame(for: object, in: proxy.size),
                            isSelected: session.selection == object.item.selection,
                            onSelect: { session.select(object.item, seekToStart: false) },
                            onMove: positionHandler(for: object),
                            onResize: scaleHandler(for: object)
                        )
                    }
                }
                .frame(maxWidth: .infinity, maxHeight: .infinity)
                // Route manipulation from the canvas itself so the visible
                // object geometry and gesture coordinate space stay aligned
                // when the editor shell changes height.
                .contentShape(Rectangle())
                .highPriorityGesture(directMoveGesture(in: proxy.size))
                .simultaneousGesture(directResizeGesture())
                .simultaneousGesture(
                    SpatialTapGesture().onEnded { value in
                        selectPreviewObject(at: value.location, in: proxy.size)
                    }
                )
            }
            .accessibilityElement(children: .contain)

            HStack(spacing: 6) {
                Circle()
                    .fill(previewKind == "Preview unavailable" ? Color.orange : KriaColor.sky)
                    .frame(width: 6, height: 6)
                Text(previewKind)
                    .font(KriaFont.body(10).weight(.semibold))
            }
            .foregroundStyle(.white)
            .padding(.horizontal, 9)
            .frame(minHeight: 26)
            .background(KriaColor.ink.opacity(0.82), in: Capsule())
            .padding(10)
            .accessibilityElement(children: .combine)
            .accessibilityLabel(previewKind)

            if !session.document.textElements.isEmpty || nativeBool(session.document.captionMeta["enabled"]) == true {
                VStack {
                    Spacer()
                    HStack {
                        Label("Layout preview", systemImage: "info.circle")
                            .font(KriaFont.body(9).weight(.semibold))
                            .foregroundStyle(.white)
                            .padding(.horizontal, 8)
                            .frame(minHeight: 24)
                            .background(.black.opacity(0.72), in: Capsule())
                        Spacer()
                    }
                    .padding(10)
                }
                .allowsHitTesting(false)
            }
        }
        .aspectRatio(9 / 16, contentMode: .fit)
        .frame(maxWidth: .infinity)
        .clipShape(RoundedRectangle(cornerRadius: 22, style: .continuous))
        .overlay {
            RoundedRectangle(cornerRadius: 22, style: .continuous)
                .stroke(KriaColor.line, lineWidth: 1)
        }
        .background(Color.black, in: RoundedRectangle(cornerRadius: 22, style: .continuous))
        .onAppear(perform: refreshObjects)
        .onChange(of: session.document) { _, _ in refreshObjects() }
    }
}

private struct NativeEditorPreviewObject: Identifiable, Equatable {
    let item: NativeEditorTimelineItem
    let text: String?
    let position: CGPoint?
    let style: String?
    let title: String
    let render: NativePreviewRender
    let detail: String?
    /// For media blocks this is the canvas width fraction; other cards retain
    /// the 84% legacy preview width.
    let scale: CGFloat
    let fullscreen: Bool

    init(
        item: NativeEditorTimelineItem,
        text: String?,
        position: CGPoint?,
        style: String?,
        title: String,
        render: NativePreviewRender,
        detail: String? = nil,
        scale: CGFloat = 0.84,
        fullscreen: Bool = false
    ) {
        self.item = item
        self.text = text
        self.position = position
        self.style = style
        self.title = title
        self.render = render
        self.detail = detail
        self.scale = scale
        self.fullscreen = fullscreen
    }

    var id: String { "\(item.kind.rawValue)-\(item.id)" }
}

private enum NativePreviewRender: Equatable {
    case text, caption, mediaOverlay, visualBlock, carousel, runtimeOnly
}

private struct NativePreviewObjectView: View {
    let object: NativeEditorPreviewObject
    let frame: CGRect
    let isSelected: Bool
    let onSelect: () -> Void
    let onMove: ((CGPoint) -> Void)?
    let onResize: ((CGFloat) -> Void)?

    private var accessibilityValue: String {
        var parts = ["\(nativeTimecode(object.item.start)) to \(nativeTimecode(object.item.end))"]
        if let position = object.position, onMove != nil {
            parts.append("position \(Int((position.x * 100).rounded()))%, \(Int((position.y * 100).rounded()))%")
        }
        if onResize != nil { parts.append("size \(Int((object.scale * 100).rounded()))%") }
        if isSelected { parts.append("selected") }
        return parts.joined(separator: ", ")
    }

    var body: some View {
        ZStack {
            // `Group` does not create a concrete hit-test surface. Keep a real,
            // canvas-sized shape behind every preview object so direct
            // manipulation works across transparent text/media padding and so
            // XCUITest addresses the same geometry that the user sees.
            Rectangle()
                .fill(Color.black.opacity(0.001))
                .contentShape(Rectangle())

            Group {
            if object.render == .caption {
                Text(object.text ?? "Caption")
                    .font(KriaFont.body(15).weight(.semibold))
                    .foregroundStyle(.white)
                    .padding(.horizontal, 10)
                    .frame(maxWidth: .infinity, minHeight: 44)
                    .background(.black.opacity(0.76), in: RoundedRectangle(cornerRadius: 8, style: .continuous))
            } else if object.render == .text {
                Text(object.text ?? object.title)
                    .font(object.style == "Fraunces" ? KriaFont.display(24) : KriaFont.body(22).weight(.bold))
                    .foregroundStyle(.white)
                    .multilineTextAlignment(.center)
                    .lineLimit(3)
                    .shadow(color: .black.opacity(0.75), radius: 3, y: 1)
                    .frame(maxWidth: .infinity, minHeight: 44)
            } else if object.render == .mediaOverlay {
                Label(object.detail ?? object.title, systemImage: "photo.on.rectangle")
                    .font(KriaFont.body(13).weight(.semibold))
                    .foregroundStyle(.white)
                    .frame(maxWidth: .infinity, minHeight: 54)
                    .background(.black.opacity(0.68), in: RoundedRectangle(cornerRadius: 10, style: .continuous))
            } else if object.render == .visualBlock {
                Label(object.detail ?? object.title, systemImage: "square.grid.2x2")
                    .font(KriaFont.body(13).weight(.semibold))
                    .foregroundStyle(.white)
                    .frame(maxWidth: .infinity, minHeight: 54)
                    .background(KriaColor.ink.opacity(0.72), in: RoundedRectangle(cornerRadius: 10, style: .continuous))
            } else if object.render == .carousel {
                HStack(spacing: 4) {
                    ForEach(0..<3, id: \.self) { _ in
                        RoundedRectangle(cornerRadius: 4, style: .continuous)
                            .fill(KriaColor.sky.opacity(0.75))
                            .frame(width: 28, height: 38)
                    }
                    Text("Carousel")
                        .font(KriaFont.body(12).weight(.semibold))
                        .foregroundStyle(.white)
                }
                .frame(maxWidth: .infinity, minHeight: 54)
                .padding(.horizontal, 8)
                .background(.black.opacity(0.68), in: RoundedRectangle(cornerRadius: 10, style: .continuous))
            } else {
                Label(object.detail.map { "\($0) • preview in final render" } ?? "Preview in final render", systemImage: "sparkles.tv")
                    .font(KriaFont.body(12).weight(.semibold))
                    .foregroundStyle(.white.opacity(0.9))
                    .frame(maxWidth: .infinity, minHeight: 44)
                    .background(.black.opacity(0.62), in: RoundedRectangle(cornerRadius: 10, style: .continuous))
            }
            }
        }
        .frame(width: frame.width, height: frame.height)
        .contentShape(Rectangle())
        .overlay {
            if isSelected {
                ZStack {
                    RoundedRectangle(cornerRadius: 8, style: .continuous)
                        .stroke(KriaColor.sky, lineWidth: 2)
                    if onResize != nil {
                        VStack {
                            HStack {
                                resizeHandle
                                Spacer()
                                resizeHandle
                            }
                            Spacer()
                            HStack {
                                resizeHandle
                                Spacer()
                                resizeHandle
                            }
                        }
                        .padding(-5)
                    }
                }
                .allowsHitTesting(false)
            }
        }
        .accessibilityElement(children: .ignore)
        .accessibilityLabel("\(object.title): \(object.text ?? object.detail ?? "")")
        .accessibilityValue(accessibilityValue)
        .accessibilityIdentifier("native-editor-preview-\(object.item.kind.rawValue)-\(object.item.id)")
        .accessibilityAddTraits(isSelected ? AccessibilityTraits.isSelected : [])
        .accessibilityAction(named: "Select \(object.title.lowercased())") {
            onSelect()
        }
        .position(x: frame.midX, y: frame.midY)
        .zIndex(Double(object.item.zIndex))
    }

    private var resizeHandle: some View {
        Circle()
            .fill(KriaColor.sky)
            .overlay { Circle().stroke(.white, lineWidth: 1) }
            .frame(width: 12, height: 12)
    }
}

/// Paper's compact, touch-first timeline. The playhead stays in a stable
/// reading position while the timeline moves underneath it as the clock is
/// scrubbed. This means a horizontal finger movement always maps 1:1 to time.
struct NativeMiniStrip: View {
    @ObservedObject var session: NativeEditorSession
    @ObservedObject private var clock: NativeEditorPlaybackClock
    @State private var zoom: CGFloat = 1
    @State private var pinchAnchor: CGFloat = 1
    @State private var viewportWidth: CGFloat = 0
    @State private var scrubStartTime: TimeInterval?
    @State private var cachedItems: [NativeEditorTimelineItem] = []
    @State private var cachedClips: [EditorClip] = []
    @State private var didCacheItems = false

    private let minimumZoom: CGFloat = 0.5
    private let maximumZoom: CGFloat = 3
    private let basePixelsPerSecond: CGFloat = 72
    private let filmstripHeight: CGFloat = 68
    private let secondaryLaneHeight: CGFloat = 44
    private let rowGap: CGFloat = 3

    init(session: NativeEditorSession) {
        self.session = session
        _clock = ObservedObject(wrappedValue: session.playbackClock)
    }

    private var clips: [EditorClip] { didCacheItems ? cachedClips : [] }
    private var timelineItems: [NativeEditorTimelineItem] { didCacheItems ? cachedItems : makeItems() }
    private var textItems: [NativeEditorTimelineItem] { timelineItems.filter { $0.kind == .text } }
    private var captionItems: [NativeEditorTimelineItem] { timelineItems.filter { $0.kind == .captionCue } }
    private var soundEffectItems: [NativeEditorTimelineItem] { timelineItems.filter { $0.kind == .soundEffect } }
    private var mediaOverlayItems: [NativeEditorTimelineItem] { timelineItems.filter { $0.kind == .mediaOverlay } }
    private var visualBlockItems: [NativeEditorTimelineItem] { timelineItems.filter { $0.kind == .visualBlock } }
    private var motionSceneItems: [NativeEditorTimelineItem] { timelineItems.filter { $0.kind == .motionScene } }
    private var cameraEffectItems: [NativeEditorTimelineItem] { timelineItems.filter { $0.kind == .cameraEffect } }
    private var carouselItems: [NativeEditorTimelineItem] { timelineItems.filter { $0.kind == .carousel } }
    private var musicItems: [NativeEditorTimelineItem] { timelineItems.filter { $0.kind == .music } }
    private var hasText: Bool { !textItems.isEmpty }
    private var hasCaptions: Bool { documentCaptionsEnabled || !captionItems.isEmpty }
    private var hasSoundEffects: Bool { !soundEffectItems.isEmpty }
    private var hasMediaOverlays: Bool { !mediaOverlayItems.isEmpty }
    private var hasVisualBlocks: Bool { !visualBlockItems.isEmpty }
    private var hasMotionScenes: Bool { !motionSceneItems.isEmpty }
    private var hasCameraEffects: Bool { !cameraEffectItems.isEmpty }
    private var hasCarousel: Bool { !carouselItems.isEmpty }
    private var hasMusic: Bool { !musicItems.isEmpty || session.document.music != nil }
    private var captionsExpanded: Bool { session.selection?.kind == .captionCue }
    private var laneCount: Int {
        1 + (hasText ? 1 : 0) + (hasCaptions ? 1 : 0) + (hasMusic ? 1 : 0)
            + (hasSoundEffects ? 1 : 0) + (hasMediaOverlays ? 1 : 0)
            + (hasVisualBlocks ? 1 : 0) + (hasMotionScenes ? 1 : 0)
            + (hasCameraEffects ? 1 : 0) + (hasCarousel ? 1 : 0)
    }
    private var timelineHeight: CGFloat {
        filmstripHeight + CGFloat(laneCount - 1) * secondaryLaneHeight + CGFloat(max(0, laneCount - 1)) * rowGap
    }
    /// The wrapper can use this when it needs an explicit height; leaving the
    /// view unframed is preferred so optional lanes stay visible.
    var preferredHeight: CGFloat { timelineHeight + 88 }
    private var timelineDuration: TimeInterval {
        // The rendered asset (or the server's expected_duration_s) owns the
        // transport boundary. Stale lane ends must not make the clock promise
        // seconds that the player cannot show.
        max(0.1, session.duration)
    }
    private var documentCaptionsEnabled: Bool {
        nativeBool(session.document.captionMeta["enabled"]) ?? !session.document.captionCues.isEmpty
    }
    private var pixelsPerSecond: CGFloat { basePixelsPerSecond * zoom }

    var body: some View {
        VStack(spacing: 6) {
            controls
            GeometryReader { _ in
                ScrollView(.vertical, showsIndicators: laneCount > 4) {
                    HStack(alignment: .top, spacing: 6) {
                        laneLabels
                            .frame(width: 46)
                        timeline
                    }
                    .frame(maxWidth: .infinity, alignment: .leading)
                }
                .scrollBounceBehavior(.basedOnSize)
                .accessibilityIdentifier("native-editor-lane-scroll")
            }
        }
        .padding(.vertical, 4)
        .accessibilityElement(children: .contain)
    }

    private var controls: some View {
        HStack(spacing: 5) {
            Button(action: session.togglePlayback) {
                Image(systemName: session.isPlaying ? "pause.fill" : "play.fill")
                    .frame(width: 44, height: 44)
            }
            .buttonStyle(.plain)
            .foregroundStyle(KriaColor.ink)
            .background(KriaColor.sky, in: Circle())
            .accessibilityLabel(session.isPlaying ? "Pause preview" : "Play preview")
            .accessibilityIdentifier("native-editor-play-pause")

            Text(timecode(clock.currentTime))
                .font(.system(size: 13, weight: .medium, design: .monospaced))
                .foregroundStyle(KriaColor.ink)
                .monospacedDigit()
                .lineLimit(1)
                .minimumScaleFactor(0.8)
                .frame(width: 56, alignment: .leading)
                .accessibilityLabel("Current time")
                .accessibilityValue(timecode(clock.currentTime))
                .accessibilityIdentifier("native-editor-current-time")
            Text("/")
                .font(.system(size: 12, weight: .regular, design: .monospaced))
                .foregroundStyle(KriaColor.zinc)
            Text(timecode(timelineDuration))
                .font(.system(size: 13, weight: .regular, design: .monospaced))
                .foregroundStyle(KriaColor.zinc)
                .monospacedDigit()
                .lineLimit(1)
                .minimumScaleFactor(0.8)
                .frame(width: 56, alignment: .leading)
                .accessibilityLabel("Duration")
                .accessibilityValue(timecode(timelineDuration))
                .accessibilityIdentifier("native-editor-duration")

            Spacer(minLength: 8)
            zoomButton("minus", label: "Zoom out") { setZoom(zoom - 0.25) }
            Button("Fit") { setZoom(fitZoom) }
                .font(KriaFont.body(12).weight(.semibold))
                .frame(minWidth: 44, minHeight: 44)
                .buttonStyle(.plain)
                .foregroundStyle(KriaColor.mutedInk)
                .accessibilityLabel("Fit timeline")
            zoomButton("plus", label: "Zoom in") { setZoom(zoom + 0.25) }
        }
        .padding(.horizontal, 4)
    }

    private func zoomButton(_ symbol: String, label: String, action: @escaping () -> Void) -> some View {
        Button(action: action) {
            Image(systemName: symbol)
                .frame(width: 44, height: 44)
        }
        .buttonStyle(.plain)
        .foregroundStyle(KriaColor.ink)
        .background(KriaColor.softZinc, in: RoundedRectangle(cornerRadius: 12, style: .continuous))
        .accessibilityLabel(label)
    }

    private var fitZoom: CGFloat {
        guard viewportWidth > 0 else { return 1 }
        return clampedZoom(viewportWidth / (timelineDuration * basePixelsPerSecond))
    }

    private func setZoom(_ value: CGFloat) {
        let bounded = clampedZoom(value)
        zoom = bounded
        pinchAnchor = bounded
    }

    private func clampedZoom(_ value: CGFloat) -> CGFloat {
        min(max(value.isFinite ? value : 1, minimumZoom), maximumZoom)
    }

    private var laneLabels: some View {
        VStack(spacing: rowGap) {
            Text("VIDEO")
                .frame(height: filmstripHeight)
            if hasText { laneLabel("TEXT") }
            if hasCaptions { laneLabel("CAPS") }
            if hasSoundEffects { laneLabel("SFX") }
            if hasMediaOverlays { laneLabel("MEDIA") }
            if hasVisualBlocks { laneLabel("VISUAL") }
            if hasMotionScenes { laneLabel("MOTION") }
            if hasCameraEffects { laneLabel("CAMERA") }
            if hasCarousel { laneLabel("CAROUSEL") }
            if hasMusic { laneLabel("MUSIC") }
        }
        .font(.system(size: 9, weight: .bold, design: .rounded))
        .tracking(0.8)
        .foregroundStyle(KriaColor.zinc)
    }

    private func laneLabel(_ title: String) -> some View {
        Text(title).frame(height: secondaryLaneHeight)
    }

    private var timeline: some View {
        GeometryReader { proxy in
            let width = max(proxy.size.width, 1)
            let playheadX = playheadX(for: width)
            ZStack(alignment: .topLeading) {
                // Keep scrubbing on a sibling background surface. Attaching a
                // zero-distance drag to this container makes it compete with
                // every nested clip/trim gesture, even when those controls are
                // hit directly.
                Color.clear
                    .frame(width: width, height: timelineHeight)
                    .contentShape(Rectangle())
                    .gesture(scrubGesture(playheadX: playheadX))
                    .accessibilityHidden(true)

                VStack(spacing: rowGap) {
                    filmstrip(width: width, playheadX: playheadX)
                    if hasText {
                        timedLane(title: "Text", items: textItems, color: KriaColor.lilac, playheadX: playheadX)
                    }
                    if hasCaptions {
                        if captionsExpanded {
                            timedLane(title: "Captions", items: captionItems, color: KriaColor.lilac, playheadX: playheadX)
                        } else {
                            captionDensityLane(width: width, playheadX: playheadX)
                        }
                    }
                    if hasSoundEffects {
                        timedLane(title: "Sound effects", items: soundEffectItems, color: KriaColor.sage, playheadX: playheadX)
                    }
                    if hasMediaOverlays {
                        timedLane(title: "Media overlays", items: mediaOverlayItems, color: KriaColor.sky, playheadX: playheadX)
                    }
                    if hasVisualBlocks {
                        timedLane(title: "Visual blocks", items: visualBlockItems, color: KriaColor.sky, playheadX: playheadX)
                    }
                    if hasMotionScenes {
                        timedLane(title: "Motion scenes", items: motionSceneItems, color: KriaColor.sky, playheadX: playheadX)
                    }
                    if hasCameraEffects {
                        timedLane(title: "Camera effects", items: cameraEffectItems, color: KriaColor.sky, playheadX: playheadX)
                    }
                    if hasCarousel {
                        timedLane(title: "Carousel", items: carouselItems, color: KriaColor.sky, playheadX: playheadX)
                    }
                    if hasMusic {
                        timedLane(title: "Music", items: musicItems, color: KriaColor.sage, playheadX: playheadX)
                    }
                }
                .clipShape(RoundedRectangle(cornerRadius: 12, style: .continuous))

                NativePlayhead(height: timelineHeight)
                    .position(x: playheadX, y: timelineHeight / 2)
                    .allowsHitTesting(false)
            }
            .simultaneousGesture(
                MagnificationGesture()
                    .onChanged { value in setZoom(pinchAnchor * value) }
                    .onEnded { _ in pinchAnchor = zoom }
            )
            .onAppear { viewportWidth = width }
            .onChange(of: width) { _, newWidth in viewportWidth = newWidth }
            .accessibilityElement(children: .contain)
            .accessibilityLabel("Timeline")
            .accessibilityValue("\(timecode(clock.currentTime)) of \(timecode(timelineDuration))")
            .accessibilityAdjustableAction { direction in
                let delta: TimeInterval = direction == .increment ? 1 : -1
                seek(to: clock.currentTime + delta)
            }
        }
        .frame(height: timelineHeight)
        .onAppear { refreshItems() }
        .onChange(of: session.document) { _, _ in refreshItems() }
    }

    private func playheadX(for width: CGFloat) -> CGFloat {
        min(max(width * 0.28, 24), max(24, width - 24))
    }

    private func filmstrip(width: CGFloat, playheadX: CGFloat) -> some View {
        ZStack(alignment: .topLeading) {
            Canvas { context, size in
                context.fill(Path(CGRect(origin: .zero, size: size)), with: .color(KriaColor.ink))
                for (index, clip) in clips.enumerated() {
                    let frame = clipFrame(clip, playheadX: playheadX)
                    guard frame.maxX >= 0, frame.minX <= size.width else { continue }
                    let visible = frame.intersection(CGRect(origin: .zero, size: size))
                    guard !visible.isNull, visible.width > 0 else { continue }
                    context.fill(
                        Path(roundedRect: visible, cornerRadius: 8),
                        with: .color(index.isMultiple(of: 2) ? KriaColor.mutedInk : KriaColor.zinc)
                    )
                    let frameCount = max(2, Int(frame.width / 24))
                    for frameIndex in 1..<frameCount {
                        let x = frame.minX + frame.width * CGFloat(frameIndex) / CGFloat(frameCount)
                        guard x >= 0, x <= size.width else { continue }
                        var line = Path()
                        line.move(to: CGPoint(x: x, y: max(0, frame.minY + 8)))
                        line.addLine(to: CGPoint(x: x, y: min(size.height - 8, frame.maxY - 8)))
                        context.stroke(line, with: .color(.white.opacity(0.12)), lineWidth: 1)
                    }
                }
            }
            .frame(height: filmstripHeight)
            .allowsHitTesting(false)

            ForEach(visibleClips(playheadX: playheadX), id: \.element.id) { index, clip in
                NativeClipSurface(
                    clip: clip,
                    index: index,
                    frame: clipFrame(clip, playheadX: playheadX),
                    isSelected: session.selectedClipID == clip.id,
                    pixelsPerSecond: pixelsPerSecond,
                    onSelect: { session.select(EditorSelection(kind: .clip, id: clip.id.uuidString)) },
                    onTrimStart: { edge in session.beginTrim(clipID: clip.id, edge: edge) },
                    onTrimChange: { _, translation in session.updateTrim(by: translation) },
                    onTrimEnd: { session.endTrim() },
                    onMove: { offset in
                        session.selectClip(clip.id)
                        session.moveSelected(by: offset)
                    }
                )
            }
            if clips.isEmpty {
                Text("Drop clips here")
                    .font(KriaFont.body(12).weight(.semibold))
                    .foregroundStyle(.white.opacity(0.62))
                    .frame(maxWidth: .infinity, maxHeight: .infinity)
                    .accessibilityHidden(true)
            }
        }
        .frame(height: filmstripHeight)
    }

    private func refreshItems() {
        cachedClips = session.timelineClips
        cachedItems = makeItems()
        didCacheItems = true
    }

    private func makeItems() -> [NativeEditorTimelineItem] {
        nativePersistedTimelineItems(for: session)
    }

    private func select(_ item: NativeEditorTimelineItem) {
        session.select(item, seekToStart: false)
        session.seek(to: item.start)
    }

    private func itemName(_ item: NativeEditorTimelineItem, laneTitle: String) -> String {
        switch item.kind {
        case .text:
            let content = session.document.textElements.first { $0.id == item.id }?.text
            return content.map { "Text: \($0)" } ?? laneTitle
        case .captionCue:
            let document = session.document
            let content = document.captionCues.first { $0.id == item.id }?.text
            return content.map { "Caption: \($0)" } ?? laneTitle
        case .music:
            if let title = session.document.music?.raw["title"]?.stringValue { return "Music: \(title)" }
            if let trackID = session.document.music?.trackID { return "Music: \(trackID)" }
            return laneTitle
        case .soundEffect:
            let effect = session.document.soundEffects.first { $0.id == item.id }
            return effect.map { "SFX: \(nativeEffectName($0.raw, fallback: $0.kind ?? "Sound effect"))" } ?? laneTitle
        case .mediaOverlay:
            let overlay = session.document.mediaOverlays.first { $0.id == item.id }
            return overlay.map { "Media: \(nativeEffectName($0.raw, fallback: $0.kind ?? "Overlay"))" } ?? laneTitle
        case .visualBlock:
            let block = session.document.visualBlocks.first { $0.id == item.id }
            return block.map { "Visual: \($0.kind)" } ?? laneTitle
        case .motionScene:
            let scene = session.document.motionScenes.first { $0.id == item.id }
            return scene.map { "Motion: \($0.preset ?? "Scene")" } ?? laneTitle
        case .cameraEffect:
            let effect = session.document.cameraEffects.first { $0.id == item.id }
            return effect.map { "Camera: \($0.effect ?? "Effect")" } ?? laneTitle
        case .carousel:
            return "Carousel: \(item.id)"
        default:
            return laneTitle
        }
    }

    private func timedOperationKeys(for kind: EditorSelectionKind, operation: String) -> [String]? {
        switch kind {
        case .soundEffect:
            return operation == "trim"
                ? ["sound_effects.trim", "sound_effects", "lanes.sfx.trim", "sfx.trim", "lanes.sfx"]
                : ["sound_effects.timing", "sound_effects", "lanes.sfx.timing", "sfx.timing", "lanes.sfx"]
        case .mediaOverlay:
            return operation == "trim"
                ? ["media_overlays.trim", "media_overlays", "lanes.overlays.trim", "overlays.trim", "lanes.overlays"]
                : ["media_overlays.timing", "media_overlays", "lanes.overlays.timing", "overlays.timing", "lanes.overlays"]
        case .visualBlock:
            return operation == "trim"
                ? ["visual_blocks.trim", "visual_blocks", "lanes.visual_blocks.trim", "lanes.visual_blocks"]
                : ["visual_blocks.timing", "visual_blocks", "lanes.visual_blocks.timing", "lanes.visual_blocks"]
        case .motionScene:
            return operation == "trim"
                ? ["motion_scenes.trim", "motion_scenes", "lanes.motion_scenes.trim", "lanes.motion_scenes"]
                : ["motion_scenes.timing", "motion_scenes", "lanes.motion_scenes.timing", "lanes.motion_scenes"]
        case .cameraEffect:
            return operation == "trim"
                ? ["camera_effects.trim", "camera_effects", "lanes.camera_effects.trim", "lanes.camera_effects"]
                : ["camera_effects.timing", "camera_effects", "lanes.camera_effects.timing", "lanes.camera_effects"]
        default:
            return nil
        }
    }

    private func canEditTimedOperation(_ item: NativeEditorTimelineItem, operation: String) -> Bool {
        guard let keys = timedOperationKeys(for: item.kind, operation: operation) else { return false }
        if item.kind == .motionScene, session.isMotionSceneReadOnly(id: item.id) { return false }
        if item.kind == .visualBlock,
           session.document.visualBlocks.first(where: { $0.id == item.id })?.kind == "montage" {
            return false
        }
        if let capability = keys.compactMap({ session.operationCapability($0) }).first { return capability.editable }
        switch item.kind {
        case .soundEffect: return session.canEdit(.soundEffects)
        case .mediaOverlay: return session.canEdit(.mediaOverlays)
        case .visualBlock: return session.canEdit(.visualBlocks)
        case .motionScene: return session.canEdit(.motionScenes)
        case .cameraEffect: return session.canEdit(.cameraEffects)
        default: return false
        }
    }

    private func timedLane(title: String, items: [NativeEditorTimelineItem], color: Color, playheadX: CGFloat) -> some View {
        ZStack(alignment: .topLeading) {
            RoundedRectangle(cornerRadius: 9, style: .continuous)
                .fill(KriaColor.softZinc)
                .allowsHitTesting(false)
            ForEach(visibleItems(items, playheadX: playheadX), id: \.selection) { item in
                let itemFrame = itemFrame(item, playheadX: playheadX)
                NativeTimelineBar(
                    item: item,
                    frame: itemFrame,
                    name: itemName(item, laneTitle: title),
                    color: color,
                    isSelected: session.selection == item.selection,
                    canMove: canEditTimedOperation(item, operation: "timing"),
                    canTrim: canEditTimedOperation(item, operation: "trim"),
                    pixelsPerSecond: pixelsPerSecond,
                    onSelect: { select(item) },
                    onMoveStart: {
                        session.select(item, seekToStart: false)
                        session.beginTimedBodyMove(kind: item.kind, id: item.id)
                    },
                    onMoveChange: { session.updateTimedBodyMove(by: $0) },
                    onMoveEnd: { session.endTimedBodyMove() },
                    onTrimStart: { session.beginTimedEdgeTrim(kind: item.kind, id: item.id, edge: $0) },
                    onTrimChange: { session.updateTimedEdgeTrim(by: $0) },
                    onTrimEnd: { session.endTimedEdgeTrim() }
                )
            }
        }
        .frame(height: secondaryLaneHeight)
        .clipped()
        .accessibilityElement(children: .contain)
        .accessibilityLabel("\(title) lane")
    }

    private func captionDensityLane(width: CGFloat, playheadX: CGFloat) -> some View {
        ZStack(alignment: .topLeading) {
            RoundedRectangle(cornerRadius: 9, style: .continuous)
                .fill(KriaColor.softZinc)
                .allowsHitTesting(false)
            Canvas { context, size in
                for item in captionItems {
                    let x = itemFrame(item, playheadX: playheadX).midX
                    guard x >= 0, x <= size.width else { continue }
                    let line = Path(CGRect(x: x, y: 9, width: 2, height: secondaryLaneHeight - 18))
                    context.fill(line, with: .color(KriaColor.ink.opacity(0.72)))
                }
            }
            Text("Captions • \(captionItems.count) cues")
                .font(KriaFont.body(11).weight(.semibold))
                .foregroundStyle(KriaColor.ink)
                .padding(.horizontal, 10)
        }
        .frame(height: secondaryLaneHeight)
        .contentShape(Rectangle())
        .onTapGesture {
            if let first = captionItems.first { select(first) }
        }
        .accessibilityElement(children: .ignore)
        .accessibilityLabel("Captions density")
        .accessibilityValue("\(captionItems.count) cues; select to expand")
        .accessibilityAction(named: "Expand captions") {
            if let first = captionItems.first { session.select(first) }
        }
    }

    private func itemFrame(_ item: NativeEditorTimelineItem, playheadX: CGFloat) -> CGRect {
        let start = playheadX + CGFloat(item.start - clock.currentTime) * pixelsPerSecond
        let width = max(1, CGFloat(max(0.05, item.end - item.start)) * pixelsPerSecond)
        return CGRect(x: start, y: 0, width: width, height: secondaryLaneHeight)
    }

    private func visibleItems(
        _ items: [NativeEditorTimelineItem],
        playheadX: CGFloat
    ) -> [NativeEditorTimelineItem] {
        guard viewportWidth > 0 else { return items }
        return items.filter { item in
            let frame = itemFrame(item, playheadX: playheadX)
            return frame.maxX >= 0 && frame.minX <= viewportWidth || item.selection == session.selection
        }
    }

    private func visibleClips(playheadX: CGFloat) -> [(offset: Int, element: EditorClip)] {
        let indexed = Array(clips.enumerated())
        guard viewportWidth > 0 else { return indexed }
        return indexed.filter { _, clip in
            let frame = clipFrame(clip, playheadX: playheadX)
            return frame.maxX >= 0 && frame.minX <= viewportWidth || session.selectedClipID == clip.id
        }
    }

    private func clipDuration(_ clip: EditorClip) -> TimeInterval {
        let trimmed = clip.trimOut - clip.trimIn
        let timeline = clip.end - clip.start
        let value = trimmed.isFinite && trimmed > 0 ? trimmed : timeline
        return max(0.1, value.isFinite ? value : 0.1)
    }

    private func clipFrame(_ clip: EditorClip, playheadX: CGFloat) -> CGRect {
        let start = playheadX + CGFloat(clip.start - clock.currentTime) * pixelsPerSecond
        let width = CGFloat(clipDuration(clip)) * pixelsPerSecond
        return CGRect(x: start, y: 0, width: max(1, width), height: filmstripHeight)
    }

    private func scrubGesture(playheadX: CGFloat) -> some Gesture {
        DragGesture(minimumDistance: 0)
            .onChanged { value in
                let startTime = scrubStartTime ?? clock.currentTime
                scrubStartTime = startTime
                let delta = TimeInterval(value.translation.width) / TimeInterval(pixelsPerSecond)
                seek(to: startTime - delta)
            }
            .onEnded { _ in scrubStartTime = nil }
    }

    private func seek(to value: TimeInterval) {
        session.seek(to: TimelineMath.clamp(value, to: 0...timelineDuration))
    }

    private func timecode(_ value: TimeInterval) -> String {
        let safe = max(0, value.isFinite ? value : 0)
        let minutes = Int(safe) / 60
        let seconds = safe.truncatingRemainder(dividingBy: 60)
        return String(format: "%d:%04.1f", minutes, seconds)
    }
}

private struct NativePlayhead: View {
    let height: CGFloat

    var body: some View {
        VStack(spacing: 0) {
            Image(systemName: "arrowtriangle.down.fill")
                .font(.system(size: 11, weight: .bold))
                .foregroundStyle(KriaColor.sky)
                .frame(width: 20, height: 18)
            Rectangle()
                .fill(KriaColor.sky)
                .frame(width: 2, height: max(0, height - 18))
        }
        .frame(width: 20, height: height, alignment: .top)
        .shadow(color: .black.opacity(0.18), radius: 2, y: 1)
    }
}

private struct NativeTimelineBar: View {
    let item: NativeEditorTimelineItem
    let frame: CGRect
    let name: String
    let color: Color
    let isSelected: Bool
    let canMove: Bool
    let canTrim: Bool
    let pixelsPerSecond: CGFloat
    let onSelect: () -> Void
    let onMoveStart: () -> Void
    let onMoveChange: (TimeInterval) -> Void
    let onMoveEnd: () -> Void
    let onTrimStart: (NativeTrimEdge) -> Void
    let onTrimChange: (TimeInterval) -> Void
    let onTrimEnd: () -> Void
    @State private var isMoving = false

    private var timing: String {
        "\(nativeTimecode(item.start)) to \(nativeTimecode(item.end))"
    }

    private var hitWidth: CGFloat { max(44, frame.width) }

    var body: some View {
        ZStack {
            HStack(spacing: 5) {
                if frame.width >= 66 {
                    Text(name)
                        .font(KriaFont.body(10).weight(.semibold))
                        .lineLimit(1)
                        .foregroundStyle(KriaColor.ink)
                }
            }
            .padding(.horizontal, 8)
            .frame(width: max(1, frame.width), height: max(8, frame.height - 8), alignment: .leading)
            .background(color.opacity(isSelected ? 0.98 : 0.78), in: RoundedRectangle(cornerRadius: 8, style: .continuous))
            .overlay {
                RoundedRectangle(cornerRadius: 8, style: .continuous)
                    .stroke(isSelected ? KriaColor.sky : .clear, lineWidth: isSelected ? 2 : 0)
            }
            .offset(x: (hitWidth - frame.width) / 2)

            Button(action: onSelect) {
                Color.clear
                    .contentShape(Rectangle())
            }
            .buttonStyle(.plain)
            .frame(width: hitWidth, height: 44)
            .accessibilityLabel(name)
            .accessibilityValue("\(timing)\(isSelected ? ", selected" : "")")
            .accessibilityIdentifier("native-editor-timeline-\(item.kind.rawValue)-\(item.id)")
            .accessibilityAddTraits(isSelected ? AccessibilityTraits.isSelected : [])
            .accessibilityAction(named: "Select \(name.lowercased())") { onSelect() }
            .accessibilityAction(named: "Move \(name.lowercased()) earlier") {
                guard canMove else { return }
                onMoveStart(); onMoveChange(-1); onMoveEnd()
            }
            .accessibilityAction(named: "Move \(name.lowercased()) later") {
                guard canMove else { return }
                onMoveStart(); onMoveChange(1); onMoveEnd()
            }
            .gesture(
                DragGesture(minimumDistance: 4)
                    .onChanged { value in
                        guard canMove else { return }
                        if !isMoving {
                            isMoving = true
                            onMoveStart()
                        }
                        onMoveChange(TimeInterval(value.translation.width / max(1, pixelsPerSecond)))
                    }
                    .onEnded { value in
                        guard canMove else { return }
                        if !isMoving { onMoveStart() }
                        onMoveChange(TimeInterval(value.translation.width / max(1, pixelsPerSecond)))
                        onMoveEnd()
                        isMoving = false
                    }
            )

            if canTrim && isSelected {
                NativeTrimHandle(
                    edge: .leading,
                    height: frame.height,
                    pixelsPerSecond: pixelsPerSecond,
                    visualOffset: -22,
                    onTrimStart: onTrimStart,
                    onTrimChange: { _, seconds in onTrimChange(seconds) },
                    onTrimEnd: onTrimEnd
                )
                NativeTrimHandle(
                    edge: .trailing,
                    height: frame.height,
                    pixelsPerSecond: pixelsPerSecond,
                    visualOffset: 22,
                    onTrimStart: onTrimStart,
                    onTrimChange: { _, seconds in onTrimChange(seconds) },
                    onTrimEnd: onTrimEnd
                )
                .offset(x: max(0, frame.width - 44))
            }
        }
        .frame(width: hitWidth, height: 44)
        .position(x: frame.midX, y: frame.midY)
        .zIndex(isSelected ? 1 : 0)
    }
}

private struct NativeClipSurface: View {
    let clip: EditorClip
    let index: Int
    let frame: CGRect
    let isSelected: Bool
    let pixelsPerSecond: CGFloat
    let onSelect: () -> Void
    let onTrimStart: (NativeTrimEdge) -> Void
    let onTrimChange: (NativeTrimEdge, TimeInterval) -> Void
    let onTrimEnd: () -> Void
    let onMove: (TimeInterval) -> Void

    private let minimumDuration: TimeInterval = 0.1

    private var duration: TimeInterval {
        let trimmed = clip.trimOut - clip.trimIn
        let timeline = clip.end - clip.start
        let value = trimmed.isFinite && trimmed > 0 ? trimmed : timeline
        return max(minimumDuration, value.isFinite ? value : minimumDuration)
    }

    var body: some View {
        ZStack(alignment: .leading) {
            RoundedRectangle(cornerRadius: 8, style: .continuous)
                .fill(isSelected ? KriaColor.sky.opacity(0.24) : .clear)
                .overlay {
                    RoundedRectangle(cornerRadius: 8, style: .continuous)
                        .stroke(isSelected ? KriaColor.sky : .white.opacity(0.15), lineWidth: isSelected ? 2 : 1)
                }
                .contentShape(Rectangle())
                .onTapGesture(perform: onSelect)
                .accessibilityElement(children: .ignore)
                .accessibilityLabel("Clip \(index + 1), \(duration, specifier: "%.1f") seconds")
                .accessibilityValue(String(format: "%.3f", duration))
                .accessibilityIdentifier("native-editor-clip-\(index + 1)")
                .accessibilityAddTraits(isSelected ? AccessibilityTraits.isSelected : [])
                .accessibilityAction(named: "Select clip") { onSelect() }
                .accessibilityAction(named: "Move clip earlier") { onMove(-1) }
                .accessibilityAction(named: "Move clip later") { onMove(1) }

            if isSelected {
                NativeTrimHandle(
                    edge: .leading,
                    height: frame.height,
                    pixelsPerSecond: pixelsPerSecond,
                    visualOffset: -22,
                    onTrimStart: onTrimStart,
                    onTrimChange: onTrimChange,
                    onTrimEnd: onTrimEnd
                )
                    .offset(x: 0)
                NativeTrimHandle(
                    edge: .trailing,
                    height: frame.height,
                    pixelsPerSecond: pixelsPerSecond,
                    visualOffset: 22,
                    onTrimStart: onTrimStart,
                    onTrimChange: onTrimChange,
                    onTrimEnd: onTrimEnd
                )
                    .offset(x: max(0, frame.width - 44))
            }
        }
        .frame(width: max(44, frame.width), height: frame.height)
        .position(x: frame.midX, y: frame.midY)
        // A selected clip owns both edge handles. Raise the whole surface so
        // the trailing handle at an adjacent clip boundary is not covered by
        // the later clip's hit stack.
        .zIndex(isSelected ? 1 : 0)
        .allowsHitTesting(frame.maxX >= 0)
    }
}

private struct NativeTrimHandle: View {
    let edge: NativeTrimEdge
    let height: CGFloat
    let pixelsPerSecond: CGFloat
    let visualOffset: CGFloat
    let onTrimStart: (NativeTrimEdge) -> Void
    let onTrimChange: (NativeTrimEdge, TimeInterval) -> Void
    let onTrimEnd: () -> Void
    @State private var isDragging = false

    var body: some View {
        ZStack {
            Capsule()
                .fill(KriaColor.sky)
                .frame(width: 12, height: min(56, max(44, height - 20)))
                .offset(x: visualOffset)
        }
            .frame(width: 44, height: max(44, height))
            .contentShape(Rectangle())
            // The timeline owns a zero-distance scrub gesture. Give the trim
            // handle first refusal so a horizontal edge drag starts the
            // session's single baseline transaction instead of being consumed
            // as a scrub.
            .highPriorityGesture(
                DragGesture(minimumDistance: 0)
                    .onChanged { value in
                        if !isDragging {
                            isDragging = true
                            onTrimStart(edge)
                        }
                        // DragGesture.translation is cumulative from pointer
                        // down. The session applies it to one captured baseline.
                        let seconds = TimeInterval(value.translation.width / max(1, pixelsPerSecond))
                        onTrimChange(edge, seconds)
                    }
                    .onEnded { value in
                        if !isDragging { onTrimStart(edge) }
                        let seconds = TimeInterval(value.translation.width / max(1, pixelsPerSecond))
                        onTrimChange(edge, seconds)
                        onTrimEnd()
                        isDragging = false
                    }
            )
            .accessibilityLabel(edge == .leading ? "Trim clip start" : "Trim clip end")
            .accessibilityIdentifier(edge == .leading ? "native-editor-trim-leading" : "native-editor-trim-trailing")
            .accessibilityHint("Drag to adjust the selected clip")
            .accessibilityAdjustableAction { direction in
                onTrimStart(edge)
                onTrimChange(edge, direction == .increment ? 0.1 : -0.1)
                onTrimEnd()
            }
    }
}
