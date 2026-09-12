import AVFoundation
import AVKit
import KriaMediaEngine
import SwiftUI
import UIKit

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
        y: min(max(layer.raw["y_frac"]?.numberValue ?? (layer.raw["position"] == .string("top") ? 0.2 : (layer.raw["position"] == .string("bottom") ? 0.8 : 0.5)), 0), 1)
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
    @State private var directResizeTextBaseline: EditorTextElement?
    @State private var transformBaseline: EditorTextElement?
    @State private var transformCenter: CGPoint?
    @State private var transformStartVector: CGVector?
    @State private var liveTextBaseline: EditorTextElement?
    @State private var liveTextBounds: TextSelectionBounds?
    @State private var liveTextFrame: NativeEditorSession.TextInteractionFrame?
    @State private var liveTextScale: Double = 1
    @State private var liveTextRotation: Double = 0
    @State private var liveTextTranslation = CGPoint.zero
    @State private var liveTextSampleCount = 0
    @State private var textAlignmentFeedback = NativeTextAlignmentFeedback()
    @State private var textAlignmentHaptic = UISelectionFeedbackGenerator()

    init(session: NativeEditorSession) {
        self.session = session
        _clock = ObservedObject(wrappedValue: session.playbackClock)
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
                    scale: CGFloat(min(max(layer.flatMap { nativeRawNumber($0.raw, "max_width_frac") } ?? 0.84, 0.2), 1)),
                    rotation: layer?.raw["rotation_deg"]?.numberValue ?? 0
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

    private var uiTestingPreviewValue: String {
        guard ProcessInfo.processInfo.arguments.contains("-ui-testing-editor") else { return "" }
        let selectedText = session.document.textElements.first { $0.id == session.selection?.id }
        let textReady = session.textInteractionFrame.map {
            $0.element == selectedText && abs($0.time - clock.currentTime) < 0.01
        } ?? false
        let target = min(clock.currentTime, max(0, session.duration - 1.0 / 600))
        let stillReady = !session.isPlaying && session.scrubPreviewFrame != nil
            && session.scrubPreviewTime.map { abs($0 - target) < 0.05 } == true
        return "liveTextSamples:\(liveTextSampleCount);liveTextReady:\(textReady);stillFrameReady:\(stillReady)"
    }

    private func refreshObjects() {
        cachedObjects = makeObjects()
        didCacheObjects = true
    }

    private func resizeText(_ baseline: EditorTextElement, scale: Double, rotation: Double, canvas: CGSize) {
        if liveTextBaseline == nil {
            liveTextBaseline = baseline
            liveTextBounds = session.previewSelectionBounds(for: EditorSelection(kind: .text, id: baseline.id), at: clock.currentTime)
        }
        if liveTextFrame == nil, let prepared = session.textInteractionFrame,
           prepared.element == baseline, abs(prepared.time - clock.currentTime) < 0.01 {
            liveTextFrame = prepared
        }
        updateTextAlignment(in: canvas)
        let reference = liveTextBaseline ?? baseline
        let size = NativeEditorSession.textSize(for: reference)
        let width = reference.raw["max_width_frac"]?.numberValue ?? 0.84
        liveTextScale = max(scale * NativeEditorSession.textSize(for: baseline) / size, max(8 / size, 0.2 / width))
        let rawAngle = rotation + (baseline.raw["rotation_deg"]?.numberValue ?? 0)
        let displayedAngle = transformBaseline != nil ? NativeTextRotationSnap.angle(rawAngle) : rawAngle
        liveTextRotation = displayedAngle - (reference.raw["rotation_deg"]?.numberValue ?? 0)
        let position = nativeTextPosition(baseline), origin = nativeTextPosition(reference)
        liveTextTranslation = CGPoint(x: position.x - origin.x, y: position.y - origin.y)
        if liveTextFrame != nil { liveTextSampleCount += 1 }
    }

    private func updateTextAlignment(in canvas: CGSize) {
        guard let baseline = liveTextBaseline, let bounds = liveTextBounds else { return }
        let anchor = nativeTextPosition(baseline)
        let angle = liveTextRotation * .pi / 180
        let dx = (bounds.centerX - anchor.x) * canvas.width * liveTextScale
        let dy = (bounds.centerY - anchor.y) * canvas.height * liveTextScale
        let center = CGPoint(x: (anchor.x + liveTextTranslation.x) * canvas.width + dx * cos(angle) - dy * sin(angle),
                             y: (anchor.y + liveTextTranslation.y) * canvas.height + dx * sin(angle) + dy * cos(angle))
        if textAlignmentFeedback.update(center: center,
            size: CGSize(width: bounds.width * canvas.width * liveTextScale, height: bounds.height * canvas.height * liveTextScale),
            rotation: (baseline.raw["rotation_deg"]?.numberValue ?? 0) + liveTextRotation, canvas: canvas) {
            textAlignmentHaptic.selectionChanged()
            textAlignmentHaptic.prepare()
        }
    }

    private func commitLiveText() {
        guard let baseline = liveTextBaseline else { return }
        #if DEBUG
        NativePreviewDiagnostics.record("live-text-gesture", fields: ["samples": String(liveTextSampleCount), "scale": String(liveTextScale)])
        #endif
        if liveTextScale != 1 || liveTextRotation != 0 {
            session.transformText(from: baseline, scale: liveTextScale, rotationDelta: liveTextRotation)
        }
        if liveTextTranslation != .zero {
            let position = nativeTextPosition(baseline)
            session.setTextPosition(id: baseline.id, x: position.x + liveTextTranslation.x,
                                    y: position.y + liveTextTranslation.y)
        }
    }

    private func frame(for object: NativeEditorPreviewObject, in size: CGSize) -> CGRect {
        if object.item.id == liveTextBaseline?.id, let bounds = liveTextBounds, let baseline = liveTextBaseline {
            let anchor = nativeTextPosition(baseline)
            let dx = (bounds.centerX - anchor.x) * size.width * liveTextScale
            let dy = (bounds.centerY - anchor.y) * size.height * liveTextScale
            let angle = liveTextRotation * .pi / 180
            let center = CGPoint(x: (anchor.x + liveTextTranslation.x) * size.width + dx * cos(angle) - dy * sin(angle),
                                 y: (anchor.y + liveTextTranslation.y) * size.height + dx * sin(angle) + dy * cos(angle))
            let width = bounds.width * size.width * liveTextScale
            let height = bounds.height * size.height * liveTextScale
            return CGRect(x: center.x - width / 2, y: center.y - height / 2, width: width, height: height)
        }
        if let geometry = session.previewSelectionBounds(for: object.item.selection, at: clock.currentTime) {
            return CGRect(x: (geometry.centerX - geometry.width / 2) * size.width,
                y: (geometry.centerY - geometry.height / 2) * size.height,
                width: geometry.width * size.width, height: geometry.height * size.height)
        }
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
            return NativeEditorInteraction.contains(point, in: frame(for: object, in: size), rotationDegrees: object.rotation)
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
                && NativeEditorInteraction.contains(point, in: frame(for: object, in: size), rotationDegrees: object.rotation)
        }
    }

    private func directMoveGesture(in size: CGSize) -> some Gesture {
        DragGesture(minimumDistance: 4, coordinateSpace: .local)
            .onChanged { (value: DragGesture.Value) in
                handleDirectMoveChanged(value, in: size)
            }
            .onEnded { (_: DragGesture.Value) in
                finishDirectMove()
            }
    }

    private func handleDirectMoveChanged(_ value: DragGesture.Value, in size: CGSize) {
        guard directResizeObjectID == nil else { return }
        if directMoveObjectID == nil {
            textAlignmentFeedback.reset()
            textAlignmentHaptic.prepare()
            if let selection = session.selection,
               let selected = objects.first(where: { $0.item.selection == selection }),
               selected.item.kind == .text, session.canEdit(.text),
               let text = session.document.textElements.first(where: { $0.id == selected.item.id }) {
                let bounds = frame(for: selected, in: size)
                let radians: CGFloat = CGFloat(text.raw["rotation_deg"]?.numberValue ?? 0) * .pi / 180
                let dx: CGFloat = bounds.width / 2
                let dy: CGFloat = bounds.height / 2
                let cosine: CGFloat = cos(radians)
                let sine: CGFloat = sin(radians)
                let corner = CGPoint(x: bounds.midX + dx * cosine - dy * sine,
                                     y: bounds.midY + dx * sine + dy * cosine)
                let cornerDistance = hypot(value.startLocation.x - corner.x, value.startLocation.y - corner.y)
                let centerDistance = hypot(value.startLocation.x - bounds.midX, value.startLocation.y - bounds.midY)
                if cornerDistance <= 22 && cornerDistance < centerDistance {
                    directMoveObjectID = selected.id
                    transformBaseline = text
                    let anchor = nativeTextPosition(text)
                    let center = CGPoint(x: anchor.x * size.width, y: anchor.y * size.height)
                    transformCenter = center
                    transformStartVector = CGVector(dx: value.startLocation.x - center.x,
                                                    dy: value.startLocation.y - center.y)
                    session.beginDirectManipulation()
                }
            }
        }
        if let baseline = transformBaseline, let center = transformCenter, let start = transformStartVector {
            let next = CGVector(dx: value.location.x - center.x, dy: value.location.y - center.y)
            let radius = hypot(start.dx, start.dy)
            guard radius > 1 else { return }
            let angle = atan2(next.dy, next.dx) - atan2(start.dy, start.dx)
            resizeText(baseline, scale: hypot(next.dx, next.dy) / radius, rotation: angle * 180 / .pi, canvas: size)
            updateTextAlignment(in: size)
            return
        }
        if directMoveObjectID == nil {
            guard let object = directMoveCandidate(at: value.startLocation, in: size),
                  let position = object.position else { return }
            directMoveObjectID = object.id
            directMoveBaseline = position
            session.select(object.item, seekToStart: false)
            session.beginDirectManipulation()
        }
        guard let objectID = directMoveObjectID,
              let baseline = directMoveBaseline,
              let object = objects.first(where: { $0.id == objectID }),
              let onMove = positionHandler(for: object),
              size.width > 0, size.height > 0 else { return }
        let position = CGPoint(
            x: min(max(0, baseline.x + value.translation.width / size.width), 1),
            y: min(max(0, baseline.y + value.translation.height / size.height), 1))
        if object.item.kind == .text,
           let text = session.document.textElements.first(where: { $0.id == object.item.id }) {
            resizeText(text, scale: 1, rotation: 0, canvas: size)
            let origin = nativeTextPosition(liveTextBaseline ?? text)
            liveTextTranslation = CGPoint(x: position.x - origin.x, y: position.y - origin.y)
            updateTextAlignment(in: size)
        } else {
            onMove(position)
        }
    }

    private func finishDirectMove() {
        guard directMoveObjectID != nil else { return }
        commitLiveText()
        directMoveObjectID = nil
        directMoveBaseline = nil
        transformBaseline = nil
        transformCenter = nil
        transformStartVector = nil
        if directResizeObjectID == nil { session.endDirectManipulation() }
    }

    private func directResizeGesture(in size: CGSize) -> some Gesture {
        MagnificationGesture()
            .onChanged { value in
                if directResizeObjectID == nil {
                    guard let selection = session.selection,
                          let object = objects.first(where: { $0.item.selection == selection }),
                          scaleHandler(for: object) != nil else { return }
                    textAlignmentFeedback.reset()
                    textAlignmentHaptic.prepare()
                    // A two-finger pinch owns the transform; do not also
                    // interpret its first finger as a corner drag.
                    directMoveObjectID = nil; directMoveBaseline = nil
                    transformBaseline = nil; transformCenter = nil; transformStartVector = nil
                    directResizeObjectID = object.id
                    directResizeBaseline = object.scale
                    if object.item.kind == .text {
                        directResizeTextBaseline = session.document.textElements.first { $0.id == object.item.id }
                    }
                    session.beginDirectManipulation()
                }
                guard let objectID = directResizeObjectID,
                      let baseline = directResizeBaseline,
                      let object = objects.first(where: { $0.id == objectID }),
                      let onResize = scaleHandler(for: object) else { return }
                if let text = directResizeTextBaseline {
                    resizeText(text, scale: value, rotation: 0, canvas: size)
                    updateTextAlignment(in: size)
                } else {
                    onResize(baseline * value)
                }
            }
            .onEnded { _ in
                guard directResizeObjectID != nil else { return }
                commitLiveText()
                directResizeObjectID = nil
                directResizeBaseline = nil
                directResizeTextBaseline = nil
                if directMoveObjectID == nil { session.endDirectManipulation() }
            }
    }

    private func canDirectlyPosition(_ object: NativeEditorPreviewObject) -> Bool {
        guard session.sourcePreviewState == .idle || session.hasSourcePreview else { return false }
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
            if session.sourcePreviewState == .preparing {
                ProgressView("Preparing preview")
                    .tint(.white).foregroundStyle(.white)
                    .frame(maxWidth: .infinity, maxHeight: .infinity)
            } else if case .failed(let message) = session.sourcePreviewState {
                VStack(spacing: 12) {
                    Text("Preview unavailable").font(KriaFont.body(14).weight(.semibold))
                    Text(message).font(KriaFont.body(12)).multilineTextAlignment(.center)
                        .padding(.horizontal, 16)
                    Button("Retry") { Task { await session.prepareSourcePreview() } }
                }
                .foregroundStyle(.white).frame(maxWidth: .infinity, maxHeight: .infinity)
            } else if let player = session.player {
                ZStack {
                    VideoPlayer(player: player)
                        .aspectRatio(session.previewAspectRatio, contentMode: .fit)
                        .frame(maxWidth: .infinity, maxHeight: .infinity)
                        .accessibilityLabel("Video preview")
                    if let frame = session.scrubPreviewFrame {
                        Image(uiImage: frame)
                            .resizable()
                            .aspectRatio(contentMode: .fit)
                            .frame(maxWidth: .infinity, maxHeight: .infinity)
                            .allowsHitTesting(false)
                            .accessibilityHidden(true)
                    }
                }
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

            if let frozen = liveTextFrame, let baseline = liveTextBaseline {
                GeometryReader { proxy in
                    let anchor = nativeTextPosition(baseline)
                    ZStack(alignment: .topLeading) {
                        Image(uiImage: frozen.below).resizable().frame(width: proxy.size.width, height: proxy.size.height)
                        ZStack(alignment: .topLeading) {
                            Image(uiImage: frozen.text).resizable()
                                .frame(width: frozen.rect.width * proxy.size.width, height: frozen.rect.height * proxy.size.height)
                                .offset(x: frozen.rect.minX * proxy.size.width, y: frozen.rect.minY * proxy.size.height)
                        }
                        .frame(width: proxy.size.width, height: proxy.size.height, alignment: .topLeading)
                        .scaleEffect(liveTextScale, anchor: UnitPoint(x: anchor.x, y: anchor.y))
                        .rotationEffect(.degrees(liveTextRotation), anchor: UnitPoint(x: anchor.x, y: anchor.y))
                        .offset(x: liveTextTranslation.x * proxy.size.width, y: liveTextTranslation.y * proxy.size.height)
                        Image(uiImage: frozen.above).resizable().frame(width: proxy.size.width, height: proxy.size.height)
                    }
                }.allowsHitTesting(false).accessibilityHidden(true)
            }
            GeometryReader { proxy in
                let visible = NativeEditorInteraction.previewOrder(
                    NativeEditorInteraction.visible(session.hasSourcePreview || session.sourcePreviewState == .idle ? objects.map(\.item) : [], at: clock.currentTime)
                )
                ZStack(alignment: .topLeading) {
                    ForEach(visible.compactMap { item in objects.first(where: { $0.item == item }) }) { object in
                        NativePreviewObjectView(
                            object: object,
                            frame: frame(for: object, in: proxy.size),
                            isSelected: session.selection == object.item.selection,
                            onSelect: { session.select(object.item, seekToStart: false) },
                            onMove: positionHandler(for: object),
                            onResize: scaleHandler(for: object),
                            showsContent: !session.hasSourcePreview,
                            rotationOverride: object.item.id == liveTextBaseline?.id ? (liveTextBaseline?.raw["rotation_deg"]?.numberValue ?? 0) + liveTextRotation : nil
                        )
                    }
                }
                .frame(maxWidth: .infinity, maxHeight: .infinity)
                // Route manipulation from the canvas itself so the visible
                // object geometry and gesture coordinate space stay aligned
                // when the editor shell changes height.
                .contentShape(Rectangle())
                .highPriorityGesture(directMoveGesture(in: proxy.size))
                .simultaneousGesture(directResizeGesture(in: proxy.size))
                .simultaneousGesture(
                    SpatialTapGesture().onEnded { value in
                        selectPreviewObject(at: value.location, in: proxy.size)
                    }
                )
            }
            .accessibilityElement(children: .contain)

        }
        .aspectRatio(session.previewAspectRatio, contentMode: .fit)
        .frame(maxWidth: .infinity)
        .clipped()
        .accessibilityValue(uiTestingPreviewValue)
        .onAppear(perform: refreshObjects)
        .onChange(of: session.document) { _, _ in refreshObjects() }
        .onChange(of: session.scrubPreviewFrame) { _, _ in
            if !session.isDirectManipulating {
                liveTextBaseline = nil; liveTextFrame = nil; liveTextBounds = nil
                liveTextScale = 1; liveTextRotation = 0; liveTextTranslation = .zero
            }
        }
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
    let rotation: Double

    init(
        item: NativeEditorTimelineItem,
        text: String?,
        position: CGPoint?,
        style: String?,
        title: String,
        render: NativePreviewRender,
        detail: String? = nil,
        scale: CGFloat = 0.84,
        fullscreen: Bool = false,
        rotation: Double = 0
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
        self.rotation = rotation
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
    let showsContent: Bool
    var rotationOverride: Double? = nil

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
            if !showsContent {
                Color.clear
            } else if object.render == .caption {
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
                    if onResize != nil, object.render == .text {
                        VStack {
                            Spacer()
                            HStack {
                                Spacer()
                                Image(systemName: "arrow.up.left.and.arrow.down.right")
                                    .font(.system(size: 10, weight: .bold))
                                    .foregroundStyle(.white)
                                    .frame(width: 22, height: 22)
                                    .background(KriaColor.sky, in: Circle())
                            }
                        }
                        .padding(-11)
                    } else if onResize != nil {
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
        .rotationEffect(.degrees(rotationOverride ?? object.rotation))
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

/// The playhead stays fixed in the viewport while every track moves beneath it.
/// Horizontal panning seeks the preview to the time underneath the playhead.
struct NativeMiniStrip: View {
    @Environment(\.accessibilityReduceMotion) private var reduceMotion
    @ObservedObject var session: NativeEditorSession
    @ObservedObject private var clock: NativeEditorPlaybackClock
    @State private var zoom: CGFloat = 1
    @State private var pinchAnchor: CGFloat = 1
    @State private var isPinching = false
    @State private var viewportWidth: CGFloat = 0
    @State private var panStartTime: TimeInterval?
    @State private var lastPanAt = Date.distantPast
    @State private var playheadStartTime: TimeInterval?
    @State private var cachedItems: [NativeEditorTimelineItem] = []
    @State private var cachedRows: [TrackRow] = []
    @State private var blockBoundaries: [TimeInterval] = []
    @State private var boundaryFeedback = 0
    @State private var trimEdge: NativeTrimEdge?
    @State private var trimPreviousTime: TimeInterval?
    @State private var trimAlignment: TimeInterval?
    @State private var trimBoundaries: [TimeInterval] = []
    @State private var moveAlignment: TimeInterval?
    @State private var movePreviousEdges: (start: TimeInterval, end: TimeInterval)?
    @State private var lastAlignmentHapticAt = Date.distantPast
    @State private var alignmentHaptic = UIImpactFeedbackGenerator(style: .medium)
    @State private var lastBoundaryFeedbackAt = Date.distantPast
    @State private var cachedClips: [EditorClip] = []
    @State private var didCacheItems = false

    private let minimumZoom: CGFloat = 0.5
    private let maximumZoom: CGFloat = 24
    private var basePixelsPerSecond: CGFloat { max(1, viewportWidth) / 6 }
    private let filmstripHeight: CGFloat = 44
    private let secondaryLaneHeight: CGFloat = 44
    private let rowGap: CGFloat = 6

    init(session: NativeEditorSession) {
        self.session = session
        _clock = ObservedObject(wrappedValue: session.playbackClock)
    }

    private var clips: [EditorClip] { didCacheItems ? cachedClips : [] }
    private var timelineItems: [NativeEditorTimelineItem] { didCacheItems ? cachedItems : makeItems() }
    private var captionTextIDs: Set<String> { Set(session.document.textElements.filter(\.isCaption).map(\.id)) }
    private var textItems: [NativeEditorTimelineItem] {
        let ids = captionTextIDs
        return timelineItems.filter { $0.kind == .text && !ids.contains($0.id) }
    }
    private var captionItems: [NativeEditorTimelineItem] {
        let ids = captionTextIDs
        return timelineItems.filter { $0.kind == .captionCue || ($0.kind == .text && ids.contains($0.id)) }
    }
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
    private struct TrackRow: Identifiable {
        let id: String
        let title: String
        let items: [NativeEditorTimelineItem]
    }
    private var rows: [TrackRow] { cachedRows }

    private func makeRows() -> [TrackRow] {
        let groups: [(String, [NativeEditorTimelineItem])] = [
            ("TEXT", textItems), ("CAPTIONS", captionItems), ("MUSIC", musicItems),
            ("SFX", soundEffectItems), ("OVERLAY", mediaOverlayItems),
            ("VISUAL", visualBlockItems), ("MOTION", motionSceneItems),
            ("CAMERA", cameraEffectItems), ("CAROUSEL", carouselItems)
        ]
        return groups.flatMap { title, items in
            if title == "TEXT" {
                return items.isEmpty ? [] : [TrackRow(id: "TEXT", title: title, items: items)]
            }
            let packed = NativeEditorInteraction.packLanes(items)
            let indices = Set(packed.map(\.lane)).sorted()
            return indices.map { index in
                TrackRow(id: "\(title)-\(index)", title: title,
                         items: packed.filter { $0.lane == index }.map(\.item))
            }
        }
    }
    private func rowCount(_ row: TrackRow) -> Int {
        row.title == "TEXT" ? max(1, (NativeEditorInteraction.packLanes(row.items).map(\.lane).max() ?? 0) + 1) : 1
    }
    private var laneCount: Int { 1 + rows.reduce(0) { $0 + rowCount($1) } + (clips.isEmpty ? 0 : 1) }
    private var timelineHeight: CGFloat {
        18 + filmstripHeight + CGFloat(laneCount - 1) * secondaryLaneHeight + CGFloat(laneCount) * rowGap
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
            GeometryReader { viewport in
                ScrollView(.vertical, showsIndicators: laneCount > 4) {
                    HStack(alignment: .top, spacing: 6) {
                        laneLabels
                            .frame(width: 66, alignment: .leading)
                        timeline
                    }
                    .frame(maxWidth: .infinity, minHeight: viewport.size.height, alignment: .topLeading)
                    .contentShape(Rectangle())
                    .simultaneousGesture(timelinePanGesture)
                }
                .scrollBounceBehavior(.basedOnSize)
                .accessibilityIdentifier("native-editor-lane-scroll")
            }
        }
        .padding(.vertical, 4)
        .accessibilityElement(children: .contain)
        .sensoryFeedback(.selection, trigger: boundaryFeedback)
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
            Button(action: session.undo) {
                Image(systemName: "arrow.uturn.backward").frame(width: 44, height: 44)
            }
            .disabled(!session.canUndo || session.isSaving)
            .accessibilityLabel("Undo")
            .accessibilityIdentifier("native-editor-undo")
            Button(action: session.redo) {
                Image(systemName: "arrow.uturn.forward").frame(width: 44, height: 44)
            }
            .disabled(!session.canRedo || session.isSaving)
            .accessibilityLabel("Redo")
            .accessibilityIdentifier("native-editor-redo")
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
    }

    private func clampedZoom(_ value: CGFloat) -> CGFloat {
        min(max(value.isFinite ? value : 1, minimumZoom), maximumZoom)
    }

    private var laneLabels: some View {
        VStack(spacing: rowGap) {
            Color.clear.frame(height: 18)
            Text("VIDEO")
                .lineLimit(1)
                .frame(maxWidth: .infinity, alignment: .leading)
                .frame(height: filmstripHeight)
            ForEach(rows) { row in
                laneLabel(row.title)
                    .frame(height: CGFloat(rowCount(row)) * secondaryLaneHeight + CGFloat(rowCount(row) - 1) * rowGap, alignment: .top)
            }
            if !clips.isEmpty { laneLabel("AUDIO") }
        }
        .font(.system(size: 9, weight: .bold, design: .rounded))
        .tracking(0.8)
        .foregroundStyle(KriaColor.zinc)
    }

    private func laneLabel(_ title: String) -> some View {
        Text(title)
            .lineLimit(1)
            .frame(maxWidth: .infinity, alignment: .leading)
            .frame(height: secondaryLaneHeight)
    }

    private var timeline: some View {
        GeometryReader { proxy in
            let width = max(proxy.size.width, 1)
            let playheadX = playheadX(for: width)
            ZStack(alignment: .topLeading) {
                VStack(spacing: rowGap) {
                    ruler(width: width, playheadX: playheadX)
                    filmstrip(width: width, playheadX: playheadX)
                    ForEach(rows) { row in
                        timedLane(title: row.title, items: row.items, color: KriaColor.softZinc, playheadX: playheadX)
                    }
                    if !clips.isEmpty { originalAudioLane(playheadX: playheadX) }
                }
                .clipped()

                NativePlayhead(height: timelineHeight)
                    .position(x: playheadX, y: timelineHeight / 2)
                    .allowsHitTesting(false)
                Color.clear
                    .frame(width: 44, height: 24)
                    .contentShape(Rectangle())
                    .position(x: playheadX, y: 12)
                    .gesture(DragGesture(minimumDistance: 3, coordinateSpace: .global)
                        .onChanged { value in
                            let start = playheadStartTime ?? clock.currentTime
                            playheadStartTime = start
                            seek(to: start + Double(value.translation.width / pixelsPerSecond))
                        }
                        .onEnded { _ in playheadStartTime = nil })
                    .accessibilityLabel("Playhead")
                    .accessibilityIdentifier("native-editor-playhead")
            }
            .clipped()
            .background(TimelinePinchCapture { scale, ended in
                if ended {
                    pinchAnchor = zoom
                    isPinching = false
                } else {
                    isPinching = true
                    setZoom(pinchAnchor * scale)
                }
                lastPanAt = .now
            })
            .onAppear { viewportWidth = width }
            .onChange(of: width) { _, newWidth in viewportWidth = newWidth }
            .accessibilityElement(children: .contain)
            .accessibilityLabel("Timeline")
            .accessibilityIdentifier("native-editor-timeline-content")
            .accessibilityValue("\(timecode(clock.currentTime)) of \(timecode(timelineDuration)). Zoom \(Int((zoom * 100).rounded())) percent")
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
        width / 2
    }

    private func ruler(width: CGFloat, playheadX: CGFloat) -> some View {
        Canvas { context, _ in
            let step: Double = pixelsPerSecond >= 800 ? 0.05
                : pixelsPerSecond >= 400 ? 0.1
                : pixelsPerSecond >= 180 ? 0.5
                : pixelsPerSecond >= 90 ? 1 : 2
            let visibleStart = max(0, clock.currentTime - Double(playheadX / pixelsPerSecond))
            let first = Int(floor(visibleStart / step))
            let last = min(Int(ceil(timelineDuration / step)), first + Int(ceil(Double(width / pixelsPerSecond) / step)) + 2)
            if first <= last {
                for index in first...last {
                    let second = Double(index) * step
                    let x = playheadX + CGFloat(second - clock.currentTime) * pixelsPerSecond
                    let label = step < 0.1 ? String(format: "%.2fs", second)
                        : step < 1 ? String(format: "%.1fs", second) : String(format: "%.0fs", second)
                    context.draw(Text(label).font(.system(size: 10)).foregroundColor(KriaColor.zinc),
                                 at: CGPoint(x: x, y: 9), anchor: .center)
                }
            }
        }
        .frame(height: 18)
        .accessibilityHidden(true)
    }

    private func originalAudioLane(playheadX: CGFloat) -> some View {
        GeometryReader { viewport in
            let start = max(0, playheadX - CGFloat(clock.currentTime) * pixelsPerSecond)
            let end = min(viewport.size.width, playheadX + CGFloat(timelineDuration - clock.currentTime) * pixelsPerSecond)
            Label("Original audio", systemImage: "waveform")
                .font(KriaFont.body(11))
                .lineLimit(1)
                .padding(.horizontal, 10)
                .frame(width: max(0, end - start), height: secondaryLaneHeight, alignment: .leading)
                .background(KriaColor.softZinc, in: RoundedRectangle(cornerRadius: 8))
                .offset(x: start)
                .accessibilityLabel("Original audio")
                .accessibilityIdentifier("native-editor-original-audio")
        }
        .frame(height: secondaryLaneHeight)
        .clipped()
    }

    private func filmstrip(width: CGFloat, playheadX: CGFloat) -> some View {
        ZStack(alignment: .topLeading) {
            Canvas { context, size in
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
                    onSelect: { select(clip) },
                    onTrimStart: { edge in
                        beginTrimHaptics(selection: EditorSelection(kind: .clip, id: clip.id.uuidString), edge: edge, start: clip.start, end: clip.end)
                        session.beginTrim(clipID: clip.id, edge: edge)
                    },
                    onTrimChange: { _, translation in
                        session.updateTrim(by: translation)
                        if let trimmed = session.timelineClips.first(where: { $0.id == clip.id }) {
                            updateTrimHaptics(start: trimmed.start, end: trimmed.end)
                        }
                    },
                    onTrimEnd: { session.endTrim(); endTrimHaptics() },
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
        if session.isTimingGestureActive, !cachedRows.isEmpty {
            // Keep the gesture's view in the same row while its times change.
            // Repacking an overlap mid-drag destroys the recognizer before
            // its end callback can release the timeline's scrub lock.
            let latest = Dictionary(uniqueKeysWithValues: cachedItems.map { ($0.selection, $0) })
            cachedRows = cachedRows.map { row in
                TrackRow(id: row.id, title: row.title, items: row.items.compactMap { latest[$0.selection] })
            }
        } else {
            cachedRows = makeRows()
        }
        blockBoundaries = Array(Set(cachedClips.flatMap { [$0.start, $0.end] }
            + cachedItems.flatMap { [$0.start, $0.end] }))
            .filter { $0.isFinite && $0 >= 0 }.sorted()
    }

    private func makeItems() -> [NativeEditorTimelineItem] {
        nativePersistedTimelineItems(for: session)
    }

    private var suppressTimelineSelection: Bool { session.isTimingGestureActive || isPinching || panStartTime != nil || Date().timeIntervalSince(lastPanAt) < 0.2 }

    private func select(_ item: NativeEditorTimelineItem) {
        guard !suppressTimelineSelection else { return }
        alignTimeline(to: item.start)
        session.select(item, seekToStart: false)
    }

    private func select(_ clip: EditorClip) {
        guard !suppressTimelineSelection else { return }
        alignTimeline(to: clip.start)
        session.selectClip(clip.id)
    }

    private func alignTimeline(to start: TimeInterval) {
        withAnimation(reduceMotion ? nil : .easeOut(duration: 0.2)) {
            session.seek(to: start)
        }
    }

    private func itemName(_ item: NativeEditorTimelineItem, laneTitle: String) -> String {
        switch item.kind {
        case .text:
            let content = session.document.textElements.first { $0.id == item.id }?.text
            return content ?? laneTitle
        case .captionCue:
            let document = session.document
            let content = document.captionCues.first { $0.id == item.id }?.text
            return content ?? laneTitle
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
        case .text:
            return ["text_elements.\(operation)", "text_elements", "text.\(operation)", "text"]
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
        case .text: return session.canEdit(.text)
        case .captionCue: return session.canEdit(.captions)
        case .soundEffect: return session.canEdit(.soundEffects)
        case .mediaOverlay: return session.canEdit(.mediaOverlays)
        case .visualBlock: return session.canEdit(.visualBlocks)
        case .motionScene: return session.canEdit(.motionScenes)
        case .cameraEffect: return session.canEdit(.cameraEffects)
        default: return false
        }
    }

    private func beginTrimHaptics(selection: EditorSelection, edge: NativeTrimEdge, start: TimeInterval, end: TimeInterval) {
        trimEdge = edge
        trimPreviousTime = edge == .leading ? start : end
        trimAlignment = nil
        trimBoundaries = cachedItems.filter { $0.selection != selection }.flatMap { [$0.start, $0.end] }
            + cachedClips.filter { selection != EditorSelection(kind: .clip, id: $0.id.uuidString) }.flatMap { [$0.start, $0.end] }
        lastAlignmentHapticAt = .distantPast
        alignmentHaptic.prepare()
    }

    private func updateTrimHaptics(start: TimeInterval, end: TimeInterval) {
        guard let edge = trimEdge, let previous = trimPreviousTime else { return }
        let time = edge == .leading ? start : end
        guard time != previous else { return }
        let aligned = NativeEditorInteraction.alignmentBoundary(start: time, end: time,
            boundaries: trimBoundaries, tolerance: 3 / max(1, Double(pixelsPerSecond)))
        let crossed = NativeEditorInteraction.crossesAlignment(previousStart: previous, previousEnd: previous,
            start: time, end: time, boundaries: trimBoundaries)
        if (crossed || (aligned != nil && aligned != trimAlignment)),
           Date().timeIntervalSince(lastAlignmentHapticAt) >= 0.075 {
            alignmentHaptic.impactOccurred(intensity: 1)
            alignmentHaptic.prepare()
            lastAlignmentHapticAt = .now
        }
        trimPreviousTime = time
        trimAlignment = aligned
    }

    private func endTrimHaptics() {
        trimEdge = nil
        trimPreviousTime = nil
        trimAlignment = nil
        trimBoundaries = []
    }

    private func alignmentBoundary(for item: NativeEditorTimelineItem) -> TimeInterval? {
        let boundaries = cachedItems.filter { $0.selection != item.selection }.flatMap { [$0.start, $0.end] }
            + cachedClips.flatMap { [$0.start, $0.end] }
        return NativeEditorInteraction.alignmentBoundary(start: item.start, end: item.end,
            boundaries: boundaries, tolerance: 3 / max(1, Double(pixelsPerSecond)))
    }

    private func timedLane(title: String, items: [NativeEditorTimelineItem], color: Color, playheadX: CGFloat) -> some View {
        let packed = title == "TEXT" ? NativeEditorInteraction.packLanes(items) : []
        let lanes = Dictionary(uniqueKeysWithValues: packed.map { ($0.item.selection, $0.lane) })
        let count = max(1, (packed.map(\.lane).max() ?? 0) + 1)
        return ZStack(alignment: .topLeading) {
            ForEach(visibleItems(items, playheadX: playheadX), id: \.selection) { item in
                let itemFrame = itemFrame(item, playheadX: playheadX).offsetBy(dx: 0, dy: CGFloat(lanes[item.selection] ?? 0) * (secondaryLaneHeight + rowGap))
                NativeTimelineBar(
                    item: item,
                    frame: itemFrame,
                    name: itemName(item, laneTitle: title),
                    color: color,
                    isSelected: session.selection == item.selection,
                    canMove: !isPinching && panStartTime == nil && canEditTimedOperation(item, operation: "timing"),
                    canTrim: canEditTimedOperation(item, operation: "trim"),
                    pixelsPerSecond: pixelsPerSecond,
                    onSelect: { select(item) },
                    onMoveStart: {
                        guard !suppressTimelineSelection else { return }
                        moveAlignment = nil
                        movePreviousEdges = (item.start, item.end)
                        lastAlignmentHapticAt = .distantPast
                        alignmentHaptic.prepare()
                        session.beginTimedBodyMove(kind: item.kind, id: item.id)
                        if session.selection != item.selection {
                            session.select(item, seekToStart: false)
                        }
                    },
                    onMoveChange: {
                        session.updateTimedBodyMove(by: $0)
                        if let moved = session.timelineItems.first(where: { $0.selection == item.selection }) {
                            let aligned = alignmentBoundary(for: moved)
                            let boundaries = cachedItems.filter { $0.selection != item.selection }.flatMap { [$0.start, $0.end] }
                                + cachedClips.flatMap { [$0.start, $0.end] }
                            let crossed = movePreviousEdges.map {
                                NativeEditorInteraction.crossesAlignment(previousStart: $0.start, previousEnd: $0.end,
                                    start: moved.start, end: moved.end, boundaries: boundaries)
                            } ?? false
                            if (crossed || (aligned != nil && aligned != moveAlignment)),
                               Date().timeIntervalSince(lastAlignmentHapticAt) >= 0.075 {
                                alignmentHaptic.impactOccurred(intensity: 1)
                                alignmentHaptic.prepare()
                                lastAlignmentHapticAt = .now
                            }
                            movePreviousEdges = (moved.start, moved.end)
                            moveAlignment = aligned
                        }
                    },
                    onMoveEnd: {
                        lastPanAt = .now
                        moveAlignment = nil
                        movePreviousEdges = nil
                        session.endTimedBodyMove()
                        refreshItems()
                    },
                    onTrimStart: { edge in
                        beginTrimHaptics(selection: item.selection, edge: edge, start: item.start, end: item.end)
                        session.beginTimedEdgeTrim(kind: item.kind, id: item.id, edge: edge)
                    },
                    onTrimChange: {
                        session.updateTimedEdgeTrim(by: $0)
                        if let trimmed = session.timelineItems.first(where: { $0.selection == item.selection }) {
                            updateTrimHaptics(start: trimmed.start, end: trimmed.end)
                        }
                    },
                    onTrimEnd: { session.endTimedEdgeTrim(); endTrimHaptics() }
                )
            }
        }
        .frame(height: CGFloat(count) * secondaryLaneHeight + CGFloat(count - 1) * rowGap)
        .clipped()
        .accessibilityElement(children: .contain)
        .accessibilityLabel("\(title) lane")
        .accessibilityIdentifier("native-editor-lane-\(title.lowercased())")
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

    private var timelinePanGesture: some Gesture {
        DragGesture(minimumDistance: 10, coordinateSpace: .global)
            .onChanged { value in
                guard abs(value.translation.width) > abs(value.translation.height),
                      !session.isTimingGestureActive, !isPinching, playheadStartTime == nil else { return }
                let start = panStartTime ?? clock.currentTime
                panStartTime = start
                lastPanAt = .now
                seek(to: start - Double(value.translation.width / pixelsPerSecond))
            }
            .onEnded { _ in
                if panStartTime != nil { lastPanAt = .now }
                panStartTime = nil
            }
    }

    private func seek(to value: TimeInterval) {
        let target = TimelineMath.clamp(value, to: 0...timelineDuration)
        let previous = clock.currentTime
        let crossedBoundary = blockBoundaries.contains { boundary in
            target > previous ? boundary > previous && boundary <= target
                : boundary < previous && boundary >= target
        }
        if crossedBoundary, Date().timeIntervalSince(lastBoundaryFeedbackAt) >= 0.08 {
            boundaryFeedback += 1
            lastBoundaryFeedbackAt = .now
        }
        session.seek(to: target)
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
    @GestureState private var moveGestureActive = false
    @Environment(\.accessibilityReduceMotion) private var reduceMotion

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
            .allowsHitTesting(false)

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
            .simultaneousGesture(
                LongPressGesture(minimumDuration: 0.45, maximumDistance: 8)
                    .sequenced(before: DragGesture(minimumDistance: 0, coordinateSpace: .global))
                    .updating($moveGestureActive) { _, active, _ in active = true }
                    .onChanged { value in
                        guard canMove, case .second(true, let drag) = value else { return }
                        if !isMoving { isMoving = true; onMoveStart() }
                        if let drag { onMoveChange(TimeInterval(drag.translation.width / max(1, pixelsPerSecond))) }
                    }
                    .onEnded { _ in
                        if isMoving { onMoveEnd() }
                        isMoving = false
                    }
            )

            if canTrim && isSelected {
                NativeTrimHandle(
                    edge: .leading,
                    height: frame.height,
                    pixelsPerSecond: pixelsPerSecond,
                    visualOffset: -min(44, hitWidth / 3) / 2 + 6,
                    touchWidth: min(44, hitWidth / 3),
                    onTrimStart: onTrimStart,
                    onTrimChange: { _, seconds in onTrimChange(seconds) },
                    onTrimEnd: onTrimEnd
                )
                .offset(x: -(hitWidth - min(44, hitWidth / 3)) / 2)
                NativeTrimHandle(
                    edge: .trailing,
                    height: frame.height,
                    pixelsPerSecond: pixelsPerSecond,
                    visualOffset: min(44, hitWidth / 3) / 2 - 6,
                    touchWidth: min(44, hitWidth / 3),
                    onTrimStart: onTrimStart,
                    onTrimChange: { _, seconds in onTrimChange(seconds) },
                    onTrimEnd: onTrimEnd
                )
                .offset(x: (hitWidth - min(44, hitWidth / 3)) / 2)
            }
        }
        .frame(width: hitWidth, height: 44)
        .scaleEffect(isMoving && !reduceMotion ? 1.04 : 1)
        .offset(y: isMoving && !reduceMotion ? -5 : 0)
        .shadow(color: .black.opacity(isMoving ? 0.22 : 0), radius: isMoving ? 5 : 0, y: 3)
        .animation(reduceMotion ? nil : .spring(response: 0.24, dampingFraction: 0.62), value: isMoving)
        .offset(y: frame.midY)
        .animation(reduceMotion ? nil : .spring(response: 0.28, dampingFraction: 0.86), value: frame.midY)
        .position(x: frame.midX, y: 0)
        .zIndex(isMoving ? 2 : isSelected ? 1 : 0)
        .onChange(of: moveGestureActive) { _, active in
            if !active && isMoving {
                isMoving = false
                onMoveEnd()
            }
        }
        .onDisappear {
            if isMoving { onMoveEnd() }
        }
        .sensoryFeedback(.impact(weight: .light), trigger: isMoving) { _, moving in moving }
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
    var touchWidth: CGFloat = 44
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
            .frame(width: touchWidth, height: max(44, height))
            .contentShape(Rectangle())
            // The timeline owns a zero-distance scrub gesture. Give the trim
            // handle first refusal so a horizontal edge drag starts the
            // session's single baseline transaction instead of being consumed
            // as a scrub.
            .highPriorityGesture(
                DragGesture(minimumDistance: 0, coordinateSpace: .global)
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
            .accessibilityLabel(edge == .leading ? "Trim block start" : "Trim block end")
            .accessibilityIdentifier(edge == .leading ? "native-editor-trim-leading" : "native-editor-trim-trailing")
            .accessibilityHint("Drag to adjust the selected block")
            .accessibilityAdjustableAction { direction in
                onTrimStart(edge)
                onTrimChange(edge, direction == .increment ? 0.1 : -0.1)
                onTrimEnd()
            }
    }
}

/// A scroll-view pinch recognizer recognizes both fingers before child SwiftUI
/// buttons and long presses can interpret them as an item edit.
private struct TimelinePinchCapture: UIViewRepresentable {
    var changed: (CGFloat, Bool) -> Void
    func makeCoordinator() -> Coordinator { Coordinator(changed: changed) }
    func makeUIView(context: Context) -> Probe {
        let view = Probe()
        view.isUserInteractionEnabled = false
        view.attach = { [weak coordinator = context.coordinator] ancestor in coordinator?.install(on: ancestor) }
        return view
    }
    func updateUIView(_ uiView: Probe, context: Context) { context.coordinator.changed = changed }
    static func dismantleUIView(_ uiView: Probe, coordinator: Coordinator) { coordinator.detach() }
    final class Probe: UIView {
        var attach: ((UIScrollView) -> Void)?
        override func didMoveToWindow() {
            super.didMoveToWindow()
            var ancestor = superview
            while let view = ancestor {
                if let scroll = view as? UIScrollView { attach?(scroll); return }
                ancestor = view.superview
            }
        }
    }
    final class Coordinator: NSObject, UIGestureRecognizerDelegate {
        var changed: (CGFloat, Bool) -> Void
        weak var host: UIScrollView?
        lazy var pinch: UIPinchGestureRecognizer = {
            let gesture = UIPinchGestureRecognizer(target: self, action: #selector(handle(_:)))
            gesture.delegate = self
            gesture.cancelsTouchesInView = true
            return gesture
        }()
        init(changed: @escaping (CGFloat, Bool) -> Void) { self.changed = changed }
        func install(on scroll: UIScrollView) {
            guard host !== scroll else { return }
            detach()
            host = scroll
            scroll.addGestureRecognizer(pinch)
        }
        func detach() { host?.removeGestureRecognizer(pinch); host = nil }
        @objc private func handle(_ gesture: UIPinchGestureRecognizer) {
            switch gesture.state {
            case .began, .changed: changed(gesture.scale, false)
            case .ended, .cancelled, .failed: changed(gesture.scale, true)
            default: break
            }
        }
        func gestureRecognizer(_ gestureRecognizer: UIGestureRecognizer,
                               shouldRecognizeSimultaneouslyWith otherGestureRecognizer: UIGestureRecognizer) -> Bool { true }
    }
}
