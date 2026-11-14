import AVFoundation
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
    let anchor = layer.anchor
    return CGPoint(x: min(max(anchor.x, 0), 1), y: min(max(anchor.y, 0), 1))
}

/// The shared transform layer's view of a native text element.
@MainActor private func nativeTextTransformBaseline(_ layer: EditorTextElement) -> TextTransformBaseline {
    TextTransformBaseline(
        id: layer.id, anchor: nativeTextPosition(layer),
        sizePx: NativeEditorSession.textSize(for: layer),
        widthFrac: layer.raw["max_width_frac"]?.numberValue ?? 0.84,
        rotationDeg: layer.raw["rotation_deg"]?.numberValue ?? 0)
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

private func nativeVisualZoom(_ raw: [String: JSONValue]) -> CGFloat {
    CGFloat(min(max(raw["editor_style"]?.objectValue?["zoom"]?.numberValue ?? 1, 1), 4))
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
    /// KRI-170: called for a tap that lands on no object (and not on a selected
    /// object's rotate/scale corner) so the editor can open the fullscreen preview.
    private let onEmptyTap: (() -> Void)?
    /// KRI-211: opens the "find your original files" flow. Nil when this
    /// session has no device-render identity to relink against, in which case
    /// the missing-originals state explains itself without offering the action.
    private let onFindOriginals: (() -> Void)?
    private let onAddClip: ((AddClipLaunch) -> Void)?
    /// Last time a drag or pinch changed on the canvas. A drag that also
    /// resolves as a tap (its tap location is where the finger went down) must
    /// never count as an empty tap. Reference type: writing it must not re-render.
    @State private var dragClock = NativeEditorPreviewDragClock()
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
    @State private var visualTransformBaseline: NativeEditorPreviewObject?
    @State private var transformCenter: CGPoint?
    @State private var transformStartVector: CGVector?
    @State private var liveTextBaseline: EditorTextElement?
    @State private var liveTextFrame: NativeEditorSession.TextInteractionFrame?
    /// Scale / rotation / translation of the in-flight text gesture (shared transform layer).
    @State private var liveText = NativeTextLiveTransform()
    @State private var liveTextSampleCount = 0
    @State private var liveMediaFrame: NativeEditorSession.MediaInteractionFrame?
    @State private var liveMediaScale: CGFloat = 1
    @State private var liveMediaRotation: Double = 0
    @State private var liveMediaTranslation = CGPoint.zero
    /// The speaker's frame when the current gesture began: the live still moves relative to it (SF4).
    @State private var speakerGestureBaseline: EditorSpeakerFrame?
    @State private var speakerGestureSelection: EditorSelection?
    /// The speaker's width when a corner drag began (any of the four corners resizes about the centre).
    @State private var speakerCornerStartWidth: Double?
    @State private var speakerSnapX = false
    @State private var speakerSnapY = false
    /// After a speaker gesture the moved still stays up until the preview has redrawn the speaker at its new place, so
    /// the release never flashes the old position (SF5).
    @State private var speakerStillHeld = false
    @GestureState private var directMoveGestureActive = false
    @GestureState private var directResizeGestureActive = false
    @Environment(\.scenePhase) private var previewScenePhase
    @State private var dangerBaseline: NativePreviewFootprint?
    @State private var dangerGuideSamples = 0
    @State private var dangerGuideLast = ""
    @State private var textAlignmentFeedback = NativeTextAlignmentFeedback()
    @State private var textAlignmentHaptic = UISelectionFeedbackGenerator()
    @State private var splitPanelPick: NativeSplitPanelPick?

    init(session: NativeEditorSession, onEmptyTap: (() -> Void)? = nil, onFindOriginals: (() -> Void)? = nil, onAddClip: ((AddClipLaunch) -> Void)? = nil) {
        self.session = session
        self.onEmptyTap = onEmptyTap
        self.onFindOriginals = onFindOriginals
        self.onAddClip = onAddClip
        _clock = ObservedObject(wrappedValue: session.playbackClock)
    }

    private var objects: [NativeEditorPreviewObject] {
        didCacheObjects ? cachedObjects : makeObjects()
    }

    private func makeObjects() -> [NativeEditorPreviewObject] {
        speakerObjects() + makeLaneObjects()
    }

    /// The framed Talk-to-camera speaker: the LOWEST object, so every caption, title and card over it wins a tap
    /// (SF1). Whole-edit; where no speaker clip is drawn its box is empty (`frame(for:)`, SF2).
    private func speakerObjects() -> [NativeEditorPreviewObject] {
        return session.previewSpeakerItems.compactMap { item in
            guard let frame = session.speakerFrame(for: item.selection) else { return nil }
            return NativeEditorPreviewObject(item: item, text: nil,
                position: CGPoint(x: frame.xFraction, y: frame.yFraction), style: nil, title: String(localized: "You", comment: "Label for the speaker (the creator) in the editor preview"),
                render: .speaker, scale: CGFloat(frame.widthFraction))
        }
    }

    /// Where a pip card is actually drawn. A device-rendered variant pulls a card whose centre is below
    /// `deviceCardMaxYFraction` up to it (the compiler's rule: not full-screen, not `caption_clear`); the box and handle
    /// follow, and a drag stops there. Cloud variants are not clamped.
    private func renderedCardPosition(_ overlay: EditorTimedEffect) -> CGPoint {
        let raw = nativeVisualPosition(overlay.raw, fallback: CGPoint(x: 0.78, y: 0.25))
        return CGPoint(x: raw.x, y: clampedCardY(raw.y, raw: overlay.raw))
    }

    private func clampedCardY(_ y: CGFloat, raw: [String: JSONValue]) -> CGFloat {
        guard session.rendersOnDevice, raw["display_mode"]?.stringValue != "fullscreen", raw["caption_clear"] != .bool(true) else { return y }
        return min(y, CGFloat(NativeEditorRenderCompiler.deviceCardMaxYFraction))
    }

    private func makeLaneObjects() -> [NativeEditorPreviewObject] {
        let document = session.document
        let captionsEnabled = nativeBool(document.captionMeta["enabled"]) ?? !document.captionCues.isEmpty
        return nativePersistedTimelineItems(for: session).compactMap { item -> NativeEditorPreviewObject? in
            switch item.kind {
            case .text:
                let layer = document.textElements.first { $0.id == item.id }
                return NativeEditorPreviewObject(
                    item: item,
                    text: layer?.text,
                    position: layer?.isCaption == true && session.offersCaptionStyleScope
                        ? session.captionPosition(id: item.id) : layer.map(nativeTextPosition) ?? CGPoint(x: 0.5, y: 0.5),
                    style: layer?.fontFamily,
                    title: String(localized: "Text", comment: "Editor object name: a text overlay"),
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
                    position: session.offersCaptionStyleScope ? session.captionPosition(id: item.id) : nil,
                    style: nil,
                    title: String(localized: "Caption"),
                    render: .caption,
                    detail: nil,
                    scale: CGFloat(session.captionStyleSize(.line(item.id)) / 78)
                )
            case .soundEffect:
                let effect = document.soundEffects.first { $0.id == item.id }
                return NativeEditorPreviewObject(
                    item: item,
                    text: nil,
                    position: CGPoint(x: 0.5, y: 0.18),
                    style: nil,
                    title: String(localized: "Sound effect (audio)"),
                    render: .runtimeOnly,
                    detail: effect.map { nativeEffectName($0.raw, fallback: $0.kind ?? String(localized: "Sound effect")) }
                )
            case .mediaOverlay:
                let overlay = document.mediaOverlays.first { $0.id == item.id }
                return NativeEditorPreviewObject(
                    item: item, text: nil,
                    position: overlay.map { renderedCardPosition($0) },
                    style: nil,
                    title: String(localized: "Media overlay"),
                    render: .mediaOverlay,
                    detail: overlay.map { nativeEffectName($0.raw, fallback: $0.kind ?? String(localized: "Overlay")) },
                    scale: overlay.map { nativeVisualScale($0.raw) } ?? 0.35,
                    fullscreen: overlay.map { nativeIsFullscreen($0.raw) } ?? false,
                    rotation: overlay?.raw["editor_style"]?.objectValue?["rotation_deg"]?.numberValue ?? 0,
                    zoom: overlay.map { nativeVisualZoom($0.raw) } ?? 1
                )
            case .visualBlock:
                let block = document.visualBlocks.first { $0.id == item.id }
                return NativeEditorPreviewObject(
                    item: item, text: nil,
                    position: block.map { nativeVisualPosition($0.raw, fallback: CGPoint(x: 0.5, y: 0.25)) },
                    style: nil,
                    title: String(localized: "Visual block"),
                    render: .visualBlock,
                    detail: block?.kind,
                    scale: block.map { nativeVisualScale($0.raw) } ?? 0.35,
                    fullscreen: block.map { nativeIsFullscreen($0.raw) } ?? false,
                    rotation: block?.raw["editor_style"]?.objectValue?["rotation_deg"]?.numberValue ?? 0,
                    zoom: block.map { nativeVisualZoom($0.raw) } ?? 1
                )
            case .drawing:
                // Tap-selectable only: the selected drawing's own transform layer does the move / pinch / rotate.
                guard let drawing = document.drawings.first(where: { $0.id == item.id }) else { return nil }
                return NativeEditorPreviewObject(item: item, text: nil, position: CGPoint(x: drawing.centerX, y: drawing.centerY),
                    style: nil, title: String(localized: "Drawing"), render: .runtimeOnly, detail: nil, scale: CGFloat(drawing.scale), rotation: drawing.rotationDeg)
            case .carousel:
                return NativeEditorPreviewObject(item: item, text: nil, position: CGPoint(x: 0.5, y: 0.78), style: nil, title: String(localized: "Carousel moment"), render: .carousel, detail: nil)
            case .motionScene:
                let scene = document.motionScenes.first { $0.id == item.id }
                return NativeEditorPreviewObject(item: item, text: nil, position: CGPoint(x: 0.5, y: 0.18), style: nil, title: String(localized: "Motion (final render)"), render: .runtimeOnly, detail: scene?.preset)
            case .cameraEffect:
                let effect = document.cameraEffects.first { $0.id == item.id }
                return NativeEditorPreviewObject(item: item, text: nil, position: CGPoint(x: 0.5, y: 0.18), style: nil, title: String(localized: "Camera effect (final render)"), render: .runtimeOnly, detail: effect.map { CameraEmphasis.label($0.raw["easing"]?.stringValue) })
            default:
                return nil
            }
        }
    }

    private var uiTestingPreviewValue: String {
        guard ProcessInfo.processInfo.arguments.contains("-ui-testing-editor") else { return "" }
        let selectedText = session.document.textElements.first { $0.id == session.selection?.id }
        let textFrame = session.textInteractionFrame
        let elementMatches = textFrame.map { $0.element == selectedText } ?? false
        let timeMatches = textFrame.map { abs($0.time - clock.currentTime) < 0.01 } ?? false
        let textReady = elementMatches && timeMatches
        let target = min(clock.currentTime, max(0, session.playbackDuration - 1.0 / 600))
        let stillReady = !session.isPlaying && session.scrubPreviewFrame != nil
            && session.scrubPreviewTime.map { abs($0 - target) < 0.05 } == true
        let frameTime = textFrame.map { String($0.time) } ?? "none"
        return "liveTextSamples:\(liveTextSampleCount);liveTextReady:\(textReady);stillFrameReady:\(stillReady);textFramePresent:\(textFrame != nil);textElementMatches:\(elementMatches);textTimeMatches:\(timeMatches);textFrameTime:\(frameTime);previewClockTime:\(clock.currentTime);dangerGuideSamples:\(dangerGuideSamples);dangerGuideLast:\(dangerGuideLast)"
    }

    private func refreshObjects() {
        cachedObjects = makeObjects()
        didCacheObjects = true
    }

    private func resizeText(_ baseline: EditorTextElement, scale: Double, rotation: Double, canvas: CGSize) {
        if liveTextBaseline == nil {
            liveTextBaseline = baseline
            liveText.begin(baseline: nativeTextTransformBaseline(baseline),
                           bounds: session.previewSelectionBounds(for: EditorSelection(kind: .text, id: baseline.id), at: clock.currentTime).map(TextTransformBounds.init))
        }
        if liveTextFrame == nil, let prepared = session.textInteractionFrame,
           prepared.element == baseline, abs(prepared.time - clock.currentTime) < 0.01 {
            liveTextFrame = prepared
        }
        updateTextAlignment(in: canvas)
        liveText.resize(current: nativeTextTransformBaseline(baseline), scale: scale, rotation: rotation, snapRotation: transformBaseline != nil)
        if liveTextFrame != nil { liveTextSampleCount += 1 }
    }

    private func updateTextAlignment(in canvas: CGSize) {
        if liveText.updateAlignment(&textAlignmentFeedback, in: canvas) {
            textAlignmentHaptic.selectionChanged()
            textAlignmentHaptic.prepare()
        }
    }

    private func commitLiveText() {
        guard let baseline = liveTextBaseline else { return }
        #if DEBUG
        NativePreviewDiagnostics.record("live-text-gesture", fields: ["samples": String(liveTextSampleCount), "scale": String(liveText.scale)])
        #endif
        if liveText.scale != 1 || liveText.rotation != 0 {
            session.transformText(from: baseline, scale: liveText.scale, rotationDelta: liveText.rotation)
        }
        if liveText.translation != .zero {
            let position = nativeTextPosition(baseline)
            session.setTextPosition(id: baseline.id, x: position.x + liveText.translation.x,
                                    y: position.y + liveText.translation.y)
        }
    }

    private func frame(for object: NativeEditorPreviewObject, in size: CGSize) -> CGRect {
        if object.item.kind == .speaker { return speakerBox(for: object.item.selection, in: size) }
        if object.item.kind == .drawing {
            guard let drawing = session.drawing(id: object.item.id) else { return .zero }
            let box = NativeDrawingTransformLayer.boxSize(of: drawing, in: size)
            let centre = NativeDrawingTransformLayer.centre(of: drawing, in: size)
            return CGRect(x: centre.x - box.width / 2, y: centre.y - box.height / 2, width: box.width, height: box.height)
        }
        if object.item.id == liveTextBaseline?.id, let live = liveText.frame(in: size) {
            return live
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
            let width = min(size.width * object.scale * object.zoom, max(44, size.width - 24))
            let height = max(44, 54 * object.scale)
            let center = CGPoint(
                x: min(max(width / 2, position.x * size.width), size.width - width / 2),
                y: min(max(height / 2, position.y * size.height), size.height - height / 2)
            )
            return CGRect(x: center.x - width / 2, y: center.y - height / 2, width: width, height: height)
        }
        return CGRect(x: 12, y: size.height - 86, width: max(44, size.width - 24), height: 54)
    }

    /// The framed speaker's box on the canvas: the document's frame (it follows every gesture sample), clipped to the
    /// canvas so the handles stay reachable (SF10). Empty while the editable preview is not showing.
    private func speakerBox(for selection: EditorSelection, in size: CGSize) -> CGRect {
        guard session.hasSourcePreview,
              let rect = session.speakerFrameRect(for: selection, at: clock.currentTime), !rect.isNull else { return .zero }
        return NativeSpeakerPanelGeometry.pixelRect(rect, canvas: size)
    }

    private func speakerIsDrawnNow(_ selection: EditorSelection) -> Bool {
        session.previewSelectionBounds(for: selection, at: clock.currentTime) != nil
    }

    private func speakerCorners(for selection: EditorSelection, in size: CGSize) -> [CGPoint] {
        let box = speakerBox(for: selection, in: size)
        guard box.width > 0, speakerIsDrawnNow(selection) else { return [] }
        return [CGPoint(x: box.minX, y: box.minY), CGPoint(x: box.maxX, y: box.minY),
                CGPoint(x: box.minX, y: box.maxY), CGPoint(x: box.maxX, y: box.maxY)]
    }

    /// The prepared stills, when they show the speaker exactly where the document has it now. A still prepared for
    /// an older frame (a second gesture before the preview caught up) is not used: it would jump (SF4).
    private func preparedSpeakerFrame() -> NativeEditorSession.MediaInteractionFrame? {
        guard session.selection == .speaker, session.document.splitScreenSections.isEmpty,
              let prepared = session.mediaInteractionFrame, prepared.selection == .speaker,
              abs(prepared.time - clock.currentTime) < 0.01,
              let rect = session.speakerFrame?.layerFrame.canvasRect,
              abs(prepared.rect.minX - rect.minX) < 0.0005, abs(prepared.rect.minY - rect.minY) < 0.0005,
              abs(prepared.rect.width - rect.width) < 0.0005 else { return nil }
        return prepared
    }

    /// Keeps the moved still on screen after release, until a still prepared at the new frame arrives (the preview has
    /// redrawn), playback starts, the frame changes again, or two seconds pass.
    private func holdSpeakerStill() {
        speakerStillHeld = true
        Task { @MainActor in
            try? await Task.sleep(for: .seconds(2))
            if speakerStillHeld, !session.isDirectManipulating { releaseSpeakerStill() }
        }
    }

    private func releaseSpeakerStill() {
        guard speakerStillHeld else { return }
        speakerStillHeld = false
        liveMediaFrame = nil
        liveMediaScale = 1
        liveMediaTranslation = .zero
    }

    /// Starts moving or resizing the speaker: pins the baseline frame and the live still (when prepared for it).
    private func beginSpeakerGesture(_ selection: EditorSelection) {
        guard speakerGestureBaseline == nil else { return }
        releaseSpeakerStill()
        speakerGestureBaseline = session.speakerFrame(for: selection)
        speakerGestureSelection = selection
        if liveMediaFrame == nil { liveMediaFrame = preparedSpeakerFrame() }
        textAlignmentHaptic.prepare()
    }

    /// The live still follows the STORED (clamped, snapped) frame, never the raw finger, so what is shown at release is
    /// what Save sends (SF3, SF4).
    private func followSpeakerFrame() {
        guard let baseline = speakerGestureBaseline, let target = speakerGestureSelection,
              let frame = session.speakerFrame(for: target) else { return }
        liveMediaScale = CGFloat(frame.widthFraction / max(0.0001, baseline.widthFraction))
        liveMediaTranslation = CGPoint(x: frame.xFraction - baseline.xFraction, y: frame.yFraction - baseline.yFraction)
    }

    /// The centre the finger asks for, pulled onto the middle lines (a selection tick when it lands on one).
    private func moveSpeaker(to point: CGPoint) {
        let x = NativeSpeakerFrameGesture.snappedToCenter(point.x), y = NativeSpeakerFrameGesture.snappedToCenter(point.y)
        if (x.snapped && !speakerSnapX) || (y.snapped && !speakerSnapY) { textAlignmentHaptic.selectionChanged() }
        speakerSnapX = x.snapped; speakerSnapY = y.snapped
        guard let target = speakerGestureSelection else { return }
        session.setSpeakerFrameCenter(x: x.value, y: y.value, for: target)
        followSpeakerFrame()
    }

    private func resizeSpeaker(to width: Double) {
        guard let target = speakerGestureSelection else { return }
        session.setSpeakerFrameWidth(width, for: target)
        followSpeakerFrame()
    }

    /// SF9: the safe-zone guide tests the speaker's FACE (found on the prepared still) moved with the frame; without a
    /// face, the visible speaker box.
    private func speakerDangerFootprint(in canvas: CGSize) -> (footprint: NativePreviewFootprint, face: Bool)? {
        guard let target = speakerGestureSelection, let frame = session.speakerFrame(for: target) else { return nil }
        if let baseline = speakerGestureBaseline, let face = liveMediaFrame?.face {
            let k = frame.widthFraction / max(0.0001, baseline.widthFraction)
            let moved = CGRect(x: frame.xFraction + (face.minX - baseline.xFraction) * k,
                               y: frame.yFraction + (face.minY - baseline.yFraction) * k,
                               width: face.width * k, height: face.height * k)
            return (NativePreviewFootprint(frame: CGRect(x: moved.minX * canvas.width, y: moved.minY * canvas.height,
                width: moved.width * canvas.width, height: moved.height * canvas.height), rotation: 0, canvas: canvas), true)
        }
        guard let selection = speakerGestureSelection else { return nil }
        return (NativePreviewFootprint(frame: speakerBox(for: selection, in: canvas), rotation: 0, canvas: canvas), false)
    }

    private func selectPreviewObject(at point: CGPoint, in size: CGSize) {
        // A drag or pinch can also report a tap where the finger lifts; that is not a selection (it switched a dragged
        // caption to the drawing it was dropped on).
        guard Date().timeIntervalSince(dragClock.lastChange) > 0.4 else { return }
        let candidates = NativeEditorInteraction.previewOrder(
            NativeEditorInteraction.visible(objects.map(\.item), at: clock.currentTime)
        ).reversed().filter { item in
            guard let object = objects.first(where: { $0.item.selection == item.selection }) else { return false }
            if object.item.kind == .speaker, !speakerIsDrawnNow(object.item.selection) { return false }
            return NativeEditorInteraction.contains(point, in: frame(for: object, in: size), rotationDegrees: object.rotation)
        }
        guard !candidates.isEmpty else {
            lastTapIDs = []
            lastTapPoint = nil
            // Inside a split section a tap on a panel selects it. Only a tap outside every panel falls through.
            if !session.isPlaying, let context = session.splitContextAtPlayhead,
               let panel = NativeSplitPreviewOverlay.panel(at: point, in: size, context: context) {
                NativeSplitPreviewOverlay.choose(panel: panel, context: context, session: session, pick: $splitPanelPick)
                return
            }
            // A near-miss on the selected object's rotate/scale corner (its 22pt
            // zone extends outside the frame) must not open fullscreen.
            if !isNearSelectedCorner(point, in: size) { onEmptyTap?() }
            return
        }
        let ordered = candidates.map(\.selection)
        let next: EditorSelection
        // Kept as canvas fractions: selecting a caption or drawing opens a panel that resizes the preview, so the
        // same spot on the picture is a different point value on the next tap.
        let pointIsNearPrevious = lastTapPoint.map { hypot($0.x * size.width - point.x, $0.y * size.height - point.y) <= 12 } ?? false
        if pointIsNearPrevious, ordered == lastTapIDs, let selected = session.selection, let index = ordered.firstIndex(of: selected) {
            next = ordered[(index + 1) % ordered.count]
        } else {
            next = ordered[0]
        }
        lastTapIDs = ordered
        lastTapPoint = CGPoint(x: point.x / max(size.width, 1), y: point.y / max(size.height, 1))
        session.select(next, seekToStart: false)
    }

    /// Same corner and 22pt radius the direct-manipulation gesture uses to
    /// start a rotate/scale, so the two can never disagree about a tap.
    private func isNearSelectedCorner(_ point: CGPoint, in size: CGSize) -> Bool {
        guard let selection = session.selection,
              let selected = objects.first(where: { $0.item.selection == selection }) else { return false }
        if selection.kind == .speaker {
            return speakerCorners(for: selection, in: size).contains { NativeTextTransformMath.inGrabZone(point, corner: $0) }
        }
        let bounds = frame(for: selected, in: size)
        let reach = selected.item.kind == .text
            ? [NativeTextTransformMath.cornerPoint(of: bounds, rotationDegrees: selected.rotation)]
            : NativeTextTransformMath.corners(of: bounds, rotationDegrees: selected.rotation)
        return reach.contains { NativeTextTransformMath.inGrabZone(point, corner: $0) }
    }

    private func directMoveCandidate(at point: CGPoint, in size: CGSize) -> NativeEditorPreviewObject? {
        // An already-selected item wins a drag over whatever is stacked on top of it (reached by tap-cycling).
        if let selection = session.selection,
           let selected = objects.first(where: { $0.item.selection == selection }),
           NativeEditorInteraction.visible([selected.item], at: clock.currentTime).isEmpty == false,
           canDirectlyPosition(selected),
           selected.item.kind != .speaker || speakerIsDrawnNow(selected.item.selection),
           NativeEditorInteraction.contains(point, in: frame(for: selected, in: size), rotationDegrees: selected.rotation) {
            return selected
        }
        return NativeEditorInteraction.previewOrder(
            NativeEditorInteraction.visible(objects.map(\.item), at: clock.currentTime)
        )
        .reversed()
        .compactMap { item in objects.first(where: { $0.item.selection == item.selection }) }
        .first { object in
            canDirectlyPosition(object)
                && (object.item.kind != .speaker || speakerIsDrawnNow(object.item.selection))
                && NativeEditorInteraction.contains(point, in: frame(for: object, in: size), rotationDegrees: object.rotation)
        }
    }

    private func beginDangerGuide(for object: NativeEditorPreviewObject, in canvas: CGSize) {
        guard dangerBaseline == nil else { return }
        dangerBaseline = NativePreviewFootprint(frame: frame(for: object, in: canvas),
                                                rotation: object.rotation, canvas: canvas)
    }

    private func dangerFootprint(in canvas: CGSize) -> NativePreviewFootprint? {
        guard let baseline = dangerBaseline,
              directMoveObjectID != nil || directResizeObjectID != nil,
              !session.isPlaying, previewScenePhase == .active else { return nil }
        if speakerGestureBaseline != nil { return speakerDangerFootprint(in: canvas)?.footprint }
        if let text = liveTextBaseline {
            if let frame = liveText.frame(in: canvas) {
                return NativePreviewFootprint(frame: frame,
                    rotation: (text.raw["rotation_deg"]?.numberValue ?? 0) + liveText.rotation, canvas: canvas)
            }
            return baseline.transformed(scale: liveText.scale, rotation: liveText.rotation, translation: liveText.translation)
        }
        return baseline.transformed(scale: liveMediaScale, rotation: liveMediaRotation, translation: liveMediaTranslation)
    }

    private func recordDangerGuideSample(in canvas: CGSize) {
        guard ProcessInfo.processInfo.arguments.contains("-ui-testing-editor"),
              let footprint = dangerFootprint(in: canvas) else { return }
        dangerGuideSamples += 1
        dangerGuideLast = NativePreviewDangerZones.overlaps(frame: footprint.frame(in: canvas), rotation: footprint.rotation, canvas: canvas)
            .map(\.rawValue).sorted().joined(separator: ",")
    }

    private func directMoveGesture(in size: CGSize) -> some Gesture {
        DragGesture(minimumDistance: 4, coordinateSpace: .local)
            .updating($directMoveGestureActive) { _, active, _ in active = true }
            .onChanged { (value: DragGesture.Value) in
                dragClock.lastChange = Date()
                handleDirectMoveChanged(value, in: size)
                recordDangerGuideSample(in: size)
            }
            .onEnded { (_: DragGesture.Value) in
                if !directResizeGestureActive { settleDirectManipulation() }
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
                let corner = NativeTextTransformMath.cornerPoint(of: bounds, rotationDegrees: text.raw["rotation_deg"]?.numberValue ?? 0)
                if NativeTextTransformMath.grabsCorner(at: value.startLocation, corner: corner, bounds: bounds) {
                    beginDangerGuide(for: selected, in: size)
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
        // The framed speaker: any corner resizes about the centre (no rotation; the wire has none).
        if directMoveObjectID == nil, let selection = session.selection, selection.kind == .speaker,
           let speaker = objects.first(where: { $0.item.selection == selection }), canDirectlyPosition(speaker),
           let frame = session.speakerFrame(for: selection),
           speakerCorners(for: selection, in: size).contains(where: { NativeTextTransformMath.inGrabZone(value.startLocation, corner: $0) }) {
            beginDangerGuide(for: speaker, in: size)
            directMoveObjectID = speaker.id
            beginSpeakerGesture(selection)
            speakerCornerStartWidth = frame.widthFraction
            let viewport = session.speakerViewport(for: selection, at: clock.currentTime) ?? CGRect(x: 0, y: 0, width: 1, height: 1)
            let center = NativeSpeakerPanelGeometry.canvasPoint(CGPoint(x: frame.xFraction, y: frame.yFraction), viewport: viewport, canvas: size)
            transformCenter = center
            transformStartVector = CGVector(dx: value.startLocation.x - center.x, dy: value.startLocation.y - center.y)
            session.beginDirectManipulation()
        }
        if directMoveObjectID == nil, let selection = session.selection,
           let selected = objects.first(where: { $0.item.selection == selection }),
           selected.item.kind != .text, selected.item.kind != .speaker, canDirectlyPosition(selected), session.canRotateVisual(selected.item.selection) {
            let bounds = frame(for: selected, in: size)
            // All four drawn handles are grabbable (44pt zones), not only the bottom-right one.
            if NativeTextTransformMath.corners(of: bounds, rotationDegrees: selected.rotation).contains(where: {
                NativeTextTransformMath.grabsCorner(at: value.startLocation, corner: $0, bounds: bounds)
            }) {
                beginDangerGuide(for: selected, in: size)
                directMoveObjectID = selected.id
                visualTransformBaseline = selected
                if let prepared = session.mediaInteractionFrame,
                   prepared.selection == selected.item.selection,
                   abs(prepared.time - clock.currentTime) < 0.01 { liveMediaFrame = prepared }
                transformCenter = CGPoint(x: bounds.midX, y: bounds.midY)
                transformStartVector = CGVector(dx: value.startLocation.x - bounds.midX, dy: value.startLocation.y - bounds.midY)
                session.beginDirectManipulation()
            }
        }
        if let startWidth = speakerCornerStartWidth, let center = transformCenter, let start = transformStartVector {
            let radius = hypot(start.dx, start.dy)
            guard radius > 1 else { return }
            resizeSpeaker(to: startWidth * hypot(value.location.x - center.x, value.location.y - center.y) / radius)
            return
        }
        if let baseline = visualTransformBaseline, let center = transformCenter, let start = transformStartVector {
            let next = CGVector(dx: value.location.x - center.x, dy: value.location.y - center.y)
            let radius = hypot(start.dx, start.dy)
            guard radius > 1 else { return }
            let scale = baseline.scale * hypot(next.dx, next.dy) / radius
            scaleHandler(for: baseline)?(scale)
            let rotation = NativeTextRotationSnap.angle(baseline.rotation + (atan2(next.dy, next.dx) - atan2(start.dy, start.dx)) * 180 / .pi)
            liveMediaScale = scale / max(0.001, baseline.scale)
            liveMediaRotation = rotation - baseline.rotation
            session.setVisualEditorStyle(baseline.item.selection, key: "rotation_deg", value: .number(max(-360, min(360, rotation))))
            return
        }
        if let baseline = transformBaseline, let center = transformCenter, let start = transformStartVector {
            let next = CGVector(dx: value.location.x - center.x, dy: value.location.y - center.y)
            guard let delta = NativeTextTransformMath.cornerDelta(start: start, next: next) else { return }
            resizeText(baseline, scale: delta.scale, rotation: delta.rotationDegrees, canvas: size)
            updateTextAlignment(in: size)
            return
        }
        if directMoveObjectID == nil {
            guard let object = directMoveCandidate(at: value.startLocation, in: size),
                  let position = object.position else { return }
            beginDangerGuide(for: object, in: size)
            directMoveObjectID = object.id
            directMoveBaseline = position
            session.select(object.item, seekToStart: false)
            if object.item.kind == .speaker { beginSpeakerGesture(object.item.selection) }
            else if object.item.kind != .text, let prepared = session.mediaInteractionFrame,
               prepared.selection == object.item.selection,
               abs(prepared.time - clock.currentTime) < 0.01 { liveMediaFrame = prepared }
            session.beginDirectManipulation()
        }
        guard let objectID = directMoveObjectID,
              let baseline = directMoveBaseline,
              let object = objects.first(where: { $0.id == objectID }),
              let onMove = positionHandler(for: object),
              size.width > 0, size.height > 0 else { return }
        let position = NativeTextTransformMath.movedPosition(from: baseline, translation: value.translation, canvas: size)
        if object.item.kind == .text,
           let text = session.document.textElements.first(where: { $0.id == object.item.id }) {
            resizeText(text, scale: 1, rotation: 0, canvas: size)
            liveText.move(to: position)
            updateTextAlignment(in: size)
        } else if object.item.kind == .speaker {
            guard let viewport = session.speakerViewport(for: object.item.selection, at: clock.currentTime) else { return }
            onMove(NativeSpeakerPanelGeometry.movedPosition(from: baseline, translation: value.translation, canvas: size, viewport: viewport))
        } else {
            var landed = position
            if object.item.kind == .mediaOverlay || object.item.kind == .visualBlock {
                // Same centre snap + tick as the speaker frame.
                let x = NativeSpeakerFrameGesture.snappedToCenter(position.x), y = NativeSpeakerFrameGesture.snappedToCenter(position.y)
                if (x.snapped && !speakerSnapX) || (y.snapped && !speakerSnapY) { textAlignmentHaptic.selectionChanged() }
                speakerSnapX = x.snapped; speakerSnapY = y.snapped
                landed = CGPoint(x: x.value, y: y.value)
            }
            if object.item.kind == .mediaOverlay,
               let raw = session.document.mediaOverlays.first(where: { $0.id == object.item.id })?.raw {
                landed.y = clampedCardY(landed.y, raw: raw)
            }
            onMove(landed)
            liveMediaTranslation = CGPoint(x: landed.x - baseline.x, y: landed.y - baseline.y)
        }
    }

    private func settleDirectManipulation() {
        let hasActiveManipulation = directMoveObjectID != nil || directResizeObjectID != nil || session.isDirectManipulating
        guard hasActiveManipulation else { return }
        commitLiveText()
        dangerBaseline = nil
        directMoveObjectID = nil
        directMoveBaseline = nil
        directResizeObjectID = nil
        directResizeBaseline = nil
        directResizeTextBaseline = nil
        transformBaseline = nil
        visualTransformBaseline = nil
        transformCenter = nil
        transformStartVector = nil
        if let target = speakerGestureSelection, speakerGestureBaseline != nil, liveMediaFrame != nil,
           session.speakerFrame(for: target) != speakerGestureBaseline {
            holdSpeakerStill()
        } else {
            liveMediaFrame = nil
            liveMediaScale = 1
            liveMediaTranslation = .zero
        }
        liveMediaRotation = 0
        speakerGestureBaseline = nil
        speakerGestureSelection = nil
        speakerCornerStartWidth = nil
        speakerSnapX = false; speakerSnapY = false
        liveTextBaseline = nil
        liveText.reset()
        liveTextFrame = nil
        // liveTextSampleCount is a UI-test diagnostic: "did the immediate text
        // layer render during a manipulation," read from the accessibility
        // value right after gesture end. Resetting it here raced XCUITest's
        // synchronous read and always lost. It's otherwise unused, so it stays
        // monotonic like it was before settleDirectManipulation() existed.
        if session.isDirectManipulating { session.endDirectManipulation() }
    }

    private func directResizeGesture(in size: CGSize) -> some Gesture {
        MagnificationGesture()
            .updating($directResizeGestureActive) { _, active, _ in active = true }
            .onChanged { value in
                defer { recordDangerGuideSample(in: size) }
                dragClock.lastChange = Date()
                if directResizeObjectID == nil {
                    guard let selection = session.selection,
                          let object = objects.first(where: { $0.item.selection == selection }),
                          scaleHandler(for: object) != nil else { return }
                    textAlignmentFeedback.reset()
                    textAlignmentHaptic.prepare()
                    // A two-finger pinch owns the transform; do not also
                    // interpret its first finger as a corner drag.
                    directMoveObjectID = nil; directMoveBaseline = nil
                    transformBaseline = nil; visualTransformBaseline = nil; transformCenter = nil; transformStartVector = nil
                    beginDangerGuide(for: object, in: size)
                    directResizeObjectID = object.id
                    directResizeBaseline = object.scale
                    if object.item.kind == .text {
                        directResizeTextBaseline = session.document.textElements.first { $0.id == object.item.id }
                    }
                    speakerCornerStartWidth = nil
                    if object.item.kind == .speaker { beginSpeakerGesture(object.item.selection) }
                    else if object.item.kind != .text, let prepared = session.mediaInteractionFrame,
                       prepared.selection == object.item.selection,
                       abs(prepared.time - clock.currentTime) < 0.01 { liveMediaFrame = prepared }
                    session.beginDirectManipulation()
                }
                guard let objectID = directResizeObjectID,
                      let baseline = directResizeBaseline,
                      let object = objects.first(where: { $0.id == objectID }),
                      let onResize = scaleHandler(for: object) else { return }
                if let text = directResizeTextBaseline {
                    resizeText(text, scale: value, rotation: 0, canvas: size)
                    updateTextAlignment(in: size)
                } else if object.item.kind == .speaker {
                    onResize(baseline * value)
                } else {
                    onResize(baseline * value)
                    liveMediaScale = value
                }
            }
            .onEnded { _ in
                if !directMoveGestureActive { settleDirectManipulation() }
            }
    }

    private func canDirectlyPosition(_ object: NativeEditorPreviewObject) -> Bool {
        guard session.sourcePreviewState == .idle || session.hasSourcePreview else { return false }
        switch object.item.kind {
        case .captionCue:
            return session.offersCaptionStyleScope && session.hasSourcePreview
        case .text:
            return session.canEdit(.text)
        case .mediaOverlay:
            return !object.fullscreen && session.canEdit(.mediaOverlays)
        case .visualBlock:
            guard !object.fullscreen,
                  session.canEdit(.visualBlocks),
                  let block = session.document.visualBlocks.first(where: { $0.id == object.item.id }) else { return false }
            return block.kind == "media" && block.raw["display_mode"]?.stringValue == "overlay"
        case .speaker:
            return session.canEditSpeakerFrame(for: object.item.selection)
                && session.speakerFrame(for: object.item.selection) != nil && session.hasSourcePreview
        default:
            return false
        }
    }

    private func positionHandler(for object: NativeEditorPreviewObject) -> ((CGPoint) -> Void)? {
        guard canDirectlyPosition(object) else { return nil }
        switch object.item.kind {
        case .captionCue:
            return { point in session.setCaptionGesturePosition(id: object.item.id, position: point) }
        case .text:
            return { point in session.setTextPosition(id: object.item.id, x: point.x, y: point.y) }
        case .mediaOverlay:
            return { point in
                let raw = session.document.mediaOverlays.first { $0.id == object.item.id }?.raw ?? [:]
                session.setMediaOverlayPosition(id: object.item.id, x: point.x, y: clampedCardY(point.y, raw: raw))
            }
        case .visualBlock:
            return { point in session.setVisualBlockOverlayLayout(id: object.item.id, x: point.x, y: point.y) }
        case .speaker:
            return { point in moveSpeaker(to: point) }
        default:
            return nil
        }
    }

    private func scaleHandler(for object: NativeEditorPreviewObject) -> ((CGFloat) -> Void)? {
        guard canDirectlyPosition(object) else { return nil }
        switch object.item.kind {
        case .captionCue:
            return { scale in session.setCaptionGestureSize(id: object.item.id, size: Double(scale) * 78) }
        case .text:
            return { scale in session.setTextWidth(id: object.item.id, width: scale) }
        case .mediaOverlay:
            return { scale in session.setMediaOverlayScale(id: object.item.id, scale: scale) }
        case .visualBlock:
            return { scale in session.setVisualBlockOverlayLayout(id: object.item.id, scale: scale) }
        case .speaker:
            return { scale in resizeSpeaker(to: Double(scale)) }
        default:
            return nil
        }
    }

    /// KRI-211: plain copy plus the one useful next step. Retry is deliberately absent.
    @ViewBuilder private func originalsUnavailableBody(textStyle: Font, alignment: HorizontalAlignment = .center) -> some View {
        Text(onFindOriginals == nil
             ? String(localized: "The original clips for this edit are on another device. Open the edit there to change it.")
             : NativeEditorSession.originalsUnavailableMessage)
            .font(textStyle)
            .multilineTextAlignment(alignment == .center ? .center : .leading)
            .padding(.horizontal, alignment == .center ? 16 : 0)
            .accessibilityIdentifier("native-editor-originals-unavailable")
        if let onFindOriginals {
            Button("Find original files", action: onFindOriginals)
                .font(textStyle.weight(.semibold))
                .frame(minHeight: 44)
                .accessibilityIdentifier("native-editor-find-originals")
        }
    }

    var body: some View {
        let displayTicket = session.admissiblePreviewDisplayTicket
        ZStack(alignment: .topLeading) {
            Color.black
            if session.document.editorState == "empty" {
                VStack(spacing: 16) {
                    Text("Add a clip to start your edit").kriaType(.heading)
                        .multilineTextAlignment(.center)
                    HStack(spacing: 12) {
                        ForEach([AddClipLaunch.record, .upload], id: \.self) { launch in
                            Button { onAddClip?(launch) } label: {
                                HStack(spacing: 8) {
                                    Image(systemName: launch == .record ? KriaSourceOption.recordSymbol : KriaSourceOption.uploadSymbol)
                                        .font(.system(size: 15))
                                    Text(launch == .record ? String(localized: "Record") : String(localized: "Upload")).kriaType(.button)
                                }
                                .frame(maxWidth: .infinity, minHeight: 44)
                                .background(Color.white.opacity(0.16), in: Capsule())
                                .contentShape(Capsule())
                            }
                            .buttonStyle(.plain)
                            .disabled(onAddClip == nil || !session.canAddTimelineMedia)
                            .accessibilityIdentifier(launch == .record ? "native-editor-empty-record" : "native-editor-empty-add-clip")
                        }
                    }
                    .padding(.horizontal, 24)
                }
                .foregroundStyle(.white).frame(maxWidth: .infinity, maxHeight: .infinity)
                .accessibilityElement(children: .contain)
            } else if let player = session.player,
                      session.canDisplayCurrentPlayer || session.isShowingPreviousPreview || session.isShowingVersionBeforeStagedEdit {
                ZStack {
                    NativeEditorPlayerSurface(player: player, readinessID: displayTicket,
                        onReadyForDisplay: { session.previewFrameWasShown(player: player, ticket: displayTicket) })
                        .aspectRatio(session.previewAspectRatio, contentMode: .fit)
                        .frame(maxWidth: .infinity, maxHeight: .infinity)
                        .accessibilityLabel("Video preview")
                        .accessibilityHidden(session.isShowingPreviousPreview)
                        .accessibilityAction(named: "Expand preview") {
                            if !session.isShowingPreviousPreview { onEmptyTap?() }
                        }
                    if let previous = session.previousPreviewPresentation {
                        Group {
                            if let frame = previous.frame {
                                Image(uiImage: frame).resizable()
                            } else {
                                NativeEditorPlayerSurface(player: previous.player)
                            }
                        }
                        .aspectRatio(previous.aspectRatio, contentMode: .fit)
                        .frame(maxWidth: .infinity, maxHeight: .infinity)
                        .background(.black)
                        .overlay(alignment: .top) {
                            VStack(spacing: 4) {
                                Text(previous.status.message)
                                    .font(KriaFont.body(12)).foregroundStyle(.white)
                                    .multilineTextAlignment(.center)
                                    .fixedSize(horizontal: false, vertical: true)
                                    .accessibilityIdentifier("native-editor-previous-preview-status")
                                if previous.status != .updating {
                                    Button("Retry preview") { Task { await session.retryPreview() } }
                                        .foregroundStyle(.white).frame(minHeight: 44)
                                        .accessibilityIdentifier("native-editor-previous-preview-retry")
                                }
                            }
                            .padding(8)
                            .frame(maxWidth: .infinity)
                            .background(.black, in: RoundedRectangle(cornerRadius: 8))
                            .padding(8)
                        }
                        .accessibilityElement(children: .contain)
                    }
                    if !session.isShowingPreviousPreview, let frame = session.scrubPreviewFrame {
                        Image(uiImage: frame)
                            .resizable()
                            .aspectRatio(contentMode: .fit)
                            .frame(maxWidth: .infinity, maxHeight: .infinity)
                            .allowsHitTesting(false)
                            .accessibilityHidden(true)
                    }
                }
            } else if session.sourcePreviewState == .preparing {
                // KRI-535: with an AI edit staged, say that is what is being applied.
                ProgressView(session.hasOnlyChatStagedChanges ? String(localized: "Applying your edit") : String(localized: "Preparing preview"))
                    .tint(.white).foregroundStyle(.white)
                    .frame(maxWidth: .infinity, maxHeight: .infinity)
            } else if case .failed(let message) = session.sourcePreviewState {
                VStack(spacing: 12) {
                    Text("Preview unavailable").font(KriaFont.body(14).weight(.semibold))
                    Text(message).font(KriaFont.body(12)).multilineTextAlignment(.center)
                        .padding(.horizontal, 16)
                    Button("Retry") { Task { await session.retryPreview() } }
                }
                .foregroundStyle(.white).frame(maxWidth: .infinity, maxHeight: .infinity)
            } else if session.sourcePreviewState == .originalsUnavailable {
                // No Retry: trying again cannot make the files appear.
                VStack(spacing: 12) {
                    Text("Preview unavailable").font(KriaFont.body(14).weight(.semibold))
                    originalsUnavailableBody(textStyle: KriaFont.body(12))
                }
                .foregroundStyle(.white).frame(maxWidth: .infinity, maxHeight: .infinity)
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

            if let frozen = liveMediaFrame {
                GeometryReader { proxy in
                    let anchor = UnitPoint(x: frozen.rect.midX, y: frozen.rect.midY)
                    ZStack(alignment: .topLeading) {
                        Image(uiImage: frozen.below).resizable().frame(width: proxy.size.width, height: proxy.size.height)
                        Image(uiImage: frozen.media).resizable()
                            .frame(width: proxy.size.width, height: proxy.size.height)
                            .scaleEffect(liveMediaScale, anchor: anchor)
                            .rotationEffect(.degrees(liveMediaRotation), anchor: anchor)
                            .offset(x: liveMediaTranslation.x * proxy.size.width,
                                    y: liveMediaTranslation.y * proxy.size.height)
                        Image(uiImage: frozen.above).resizable().frame(width: proxy.size.width, height: proxy.size.height)
                    }
                }
                .allowsHitTesting(false)
                .accessibilityHidden(true)
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
                        .scaleEffect(liveText.scale, anchor: UnitPoint(x: anchor.x, y: anchor.y))
                        .rotationEffect(.degrees(liveText.rotation), anchor: UnitPoint(x: anchor.x, y: anchor.y))
                        .offset(x: liveText.translation.x * proxy.size.width, y: liveText.translation.y * proxy.size.height)
                        Image(uiImage: frozen.above).resizable().frame(width: proxy.size.width, height: proxy.size.height)
                    }
                }.allowsHitTesting(false).accessibilityHidden(true)
            }
            GeometryReader { proxy in
                let visible = NativeEditorInteraction.previewOrder(
                    NativeEditorInteraction.visible(session.document.editorState != "empty" && (session.hasSourcePreview || session.sourcePreviewState == .idle) ? objects.map(\.item) : [], at: clock.currentTime)
                )
                ZStack(alignment: .topLeading) {
                    if let footprint = dangerFootprint(in: proxy.size) {
                        NativePreviewDangerGuide(canvas: proxy.size,
                            edges: NativePreviewDangerZones.overlaps(frame: footprint.frame(in: proxy.size),
                                rotation: footprint.rotation, canvas: proxy.size),
                            followsFace: speakerGestureBaseline != nil && liveMediaFrame?.face != nil)
                    }
                    if speakerGestureBaseline != nil || directMoveObjectID != nil {
                        // The middle lines the dragged object's centre snapped to: its own panel for a speaker, else the canvas.
                        let rect: CGRect = {
                            if speakerGestureBaseline != nil, let selection = speakerGestureSelection,
                               let viewport = session.speakerViewport(for: selection, at: clock.currentTime) {
                                return NativeSpeakerPanelGeometry.pixelRect(viewport, canvas: proxy.size)
                            }
                            return CGRect(origin: .zero, size: proxy.size)
                        }()
                        if speakerSnapX { Rectangle().fill(KriaColor.sky).frame(width: 1, height: rect.height).offset(x: rect.midX, y: rect.minY) }
                        if speakerSnapY { Rectangle().fill(KriaColor.sky).frame(width: rect.width, height: 1).offset(x: rect.minX, y: rect.midY) }
                    }
                    ForEach(visible.compactMap { item in objects.first(where: { $0.item == item }) }) { object in
                        NativePreviewObjectView(
                            object: object,
                            frame: frame(for: object, in: proxy.size),
                            isSelected: session.selection == object.item.selection,
                            onSelect: { session.select(object.item, seekToStart: false) },
                            onMove: positionHandler(for: object),
                            onResize: scaleHandler(for: object),
                            showsContent: !session.hasSourcePreview,
                            rotationOverride: object.item.id == liveTextBaseline?.id ? (liveTextBaseline?.raw["rotation_deg"]?.numberValue ?? 0) + liveText.rotation : nil
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
                // The selected drawing's transform layer sits above this canvas and swallows taps; it forwards them
                // here so a drawing can be tap-cycled past like every other kind.
                .onChange(of: proxy.size, initial: true) { _, canvas in
                    // Needs this view's tap-cycling state, so it holds the view (and the session); the cycle is
                    // broken by clearing it in onDisappear below.
                    session.previewTapForwarder = { fraction in
                        selectPreviewObject(at: CGPoint(x: fraction.x * canvas.width, y: fraction.y * canvas.height), in: canvas)
                    }
                }
                .onDisappear { session.previewTapForwarder = nil }
            }
            .accessibilityElement(children: .contain)
            // The canvas's full-size gesture surface used to sit over the failure UI and swallow every
            // tap on "Retry" (KRI-211: "Retry does nothing"). With no video shown at all, the failure
            // message sits underneath and the canvas has nothing to interact with. When the finished
            // render still plays, the fallback card below is drawn above the canvas instead, so its
            // buttons work and tap-to-fullscreen on the empty canvas keeps working.
            .opacity(session.isShowingPreviousPreview ? 0 : 1)
            .accessibilityHidden(session.isShowingPreviousPreview)
            .allowsHitTesting(!session.isShowingPreviousPreview && session.document.editorState != "empty" && !(session.sourcePreviewState.isFailure && !session.isShowingRenderedFallback))

            // Above the canvas gesture surface: a touch on a divider band never reaches the canvas move or empty tap.
            if session.document.editorState != "empty" {
                NativeSplitPreviewOverlay(session: session, pick: $splitPanelPick)
            }

            if session.showsEditApplied {
                Label("Edit applied", systemImage: "checkmark.circle.fill")
                    .font(KriaFont.body(12).weight(.semibold))
                    .foregroundStyle(.white)
                    .padding(.horizontal, 12).padding(.vertical, 6)
                    .background(.black.opacity(0.72), in: Capsule())
                    .padding(10)
                    .frame(maxHeight: .infinity, alignment: .bottom)
                    .allowsHitTesting(false)
                    .transition(.opacity)
                    .accessibilityIdentifier("native-editor-edit-applied")
                    .onAppear { UIAccessibility.post(notification: .announcement, argument: String(localized: "Edit applied")) }
            }

            if session.sourcePreviewState == .ready, session.previewSkippedMediaCount > 0 {
                // Non-blocking: the preview works; these cards stay in the edit but aren't drawn here.
                let count = session.previewSkippedMediaCount
                HStack(spacing: 10) {
                    Text(String(localized: "\(count) photos or videos couldn’t load for this preview. They’re still in your edit."))
                        .font(KriaFont.body(11))
                        .fixedSize(horizontal: false, vertical: true)
                    Button("Retry") { Task { await session.retryPreview() } }
                        .font(KriaFont.body(11).weight(.semibold))
                        .accessibilityIdentifier("native-editor-retry-skipped-media")
                }
                .foregroundStyle(.white)
                .padding(10)
                .background(.black.opacity(0.72), in: RoundedRectangle(cornerRadius: 8, style: .continuous))
                .padding(10)
                .frame(maxWidth: .infinity, maxHeight: .infinity, alignment: .top)
                .accessibilityElement(children: .contain)
                .accessibilityIdentifier("native-editor-preview-skipped-media")
            }

            if session.sourcePreviewState == .preparing, session.isShowingVersionBeforeStagedEdit {
                VStack(alignment: .leading, spacing: 4) {
                    Text("This is the version before your AI edit.")
                        .font(KriaFont.body(12).weight(.semibold))
                    Text("Your edit is still being applied.")
                        .font(KriaFont.body(11))
                }
                .foregroundStyle(.white)
                .padding(10)
                .frame(maxWidth: .infinity, alignment: .leading)
                .background(.black.opacity(0.72), in: RoundedRectangle(cornerRadius: 8, style: .continuous))
                .padding(10)
                .frame(maxHeight: .infinity, alignment: .bottom)
                .allowsHitTesting(false)
                .accessibilityElement(children: .combine)
                .accessibilityIdentifier("native-editor-version-before-staged-edit")
            }

            if session.sourcePreviewState.isFailure, session.isShowingRenderedFallback || session.isShowingVersionBeforeStagedEdit {
                VStack(alignment: .leading, spacing: 4) {
                    if session.fallbackOffersRetry {
                        // The live preview is what shows edits; until it rebuilds, nothing the creator
                        // changes will appear on this video.
                        Text("Showing the finished video")
                            .font(KriaFont.body(12).weight(.semibold))
                        Text("Edits won’t appear here yet.")
                            .font(KriaFont.body(11))
                    } else {
                        Text("Showing your last finished video")
                            .font(KriaFont.body(12).weight(.semibold))
                    }
                    if session.isShowingVersionBeforeStagedEdit {
                        // KRI-535: without this the old video reads as "the AI did nothing".
                        Text("This is the version before your AI edit.")
                            .font(KriaFont.body(11))
                    }
                    if case .failed(let message) = session.sourcePreviewState {
                        Text(message)
                            .font(KriaFont.body(11))
                        if session.fallbackOffersRetry {
                            Button("Retry") { Task { await session.retryPreview() } }
                                .accessibilityIdentifier("native-editor-retry-source-preview")
                                .font(KriaFont.body(11).weight(.semibold))
                        }
                    } else {
                        originalsUnavailableBody(textStyle: KriaFont.body(11), alignment: .leading)
                    }
                }
                .foregroundStyle(.white)
                .padding(10)
                .frame(maxWidth: .infinity, alignment: .leading)
                .background(.black.opacity(0.72), in: RoundedRectangle(cornerRadius: 8, style: .continuous))
                .padding(10)
                .frame(maxHeight: .infinity, alignment: .bottom)
                .accessibilityElement(children: .contain)
                .accessibilityIdentifier("native-editor-preview-fallback")
            }

        }
        .aspectRatio(session.previewAspectRatio, contentMode: .fit)
        .frame(maxWidth: .infinity)
        .clipped()
        .accessibilityValue(uiTestingPreviewValue)
        .onAppear(perform: refreshObjects)
        .onChange(of: directMoveGestureActive) { _, active in
            if !active && !directResizeGestureActive { settleDirectManipulation() }
        }
        .onChange(of: directResizeGestureActive) { _, active in
            if !active && !directMoveGestureActive { settleDirectManipulation() }
        }
        .onDisappear(perform: settleDirectManipulation)
        .onChange(of: previewScenePhase) { _, phase in
            if phase != .active { settleDirectManipulation() }
        }
        .onChange(of: session.document) { _, _ in refreshObjects() }
        .onChange(of: session.mediaInteractionFrame?.rect) { _, _ in
            if speakerStillHeld, preparedSpeakerFrame() != nil { releaseSpeakerStill() }
        }
        .onChange(of: session.speakerFrame) { _, _ in if !session.isDirectManipulating { releaseSpeakerStill() } }
        .onChange(of: session.isPlaying) { _, playing in if playing { releaseSpeakerStill() } }
        .onChange(of: session.scrubPreviewFrame) { _, _ in
            if !session.isDirectManipulating {
                liveTextBaseline = nil; liveTextFrame = nil
                liveText.reset()
            }
        }
    }
}

final class NativeEditorPreviewDragClock {
    var lastChange = Date.distantPast
}

/// KRI-170: the fullscreen watch surface. A second `AVPlayerLayer` on the
/// session's player, mounted over the (still mounted) in-layout preview so
/// neither entering nor leaving ever tears a layer down. The box grows out of
/// (and shrinks back into) the preview's own frame; its layer fades in over the
/// first frames so a not-yet-ready layer never shows as a black box.
struct NativeEditorFullscreenPreview: View {
    @ObservedObject var session: NativeEditorSession
    let size: CGSize
    let reduceMotion: Bool
    let expanded: Bool
    /// Where the box starts/ends: uniform scale and center offset (from the
    /// screen's center) that make it coincide with the in-layout preview.
    let collapsedScale: CGFloat
    let collapsedOffset: CGSize
    @State private var ready = false

    var body: some View {
        let previous = session.previousPreviewPresentation
        ZStack {
            Color.black
            if let player = previous?.player ?? session.player {
                NativeEditorPlayerSurface(player: player, onReadyForDisplay: { ready = true; return true })
                    .aspectRatio(previous?.aspectRatio ?? Double(session.previewAspectRatio), contentMode: .fit)
                    .frame(maxWidth: .infinity, maxHeight: .infinity)
                if let frame = previous?.frame ?? (previous == nil ? session.scrubPreviewFrame : nil) {
                    Image(uiImage: frame)
                        .resizable()
                        .aspectRatio(contentMode: .fit)
                        .frame(maxWidth: .infinity, maxHeight: .infinity)
                        .allowsHitTesting(false)
                }
            }
        }
        .overlay(alignment: .bottom) {
            if let previous {
                Text(previous.status.message)
                    .font(KriaFont.body(12)).foregroundStyle(.white)
                    .multilineTextAlignment(.center).padding(12)
                    .background(.black.opacity(0.8))
            }
        }
        .frame(width: size.width, height: size.height)
        .clipShape(RoundedRectangle(cornerRadius: 16, style: .continuous))
        .scaleEffect(reduceMotion || expanded ? 1 : collapsedScale)
        .offset(reduceMotion || expanded ? .zero : collapsedOffset)
        .opacity(ready && (expanded || !reduceMotion) ? 1 : 0)
        .animation(.easeOut(duration: 0.12), value: ready)
        .task {
            try? await Task.sleep(for: .milliseconds(250))
            ready = true
        }
        .accessibilityElement(children: .ignore)
        .accessibilityLabel("Video preview, fullscreen")
        .accessibilityValue(previous?.status.message ?? "")
        .accessibilityIdentifier("native-editor-preview-fullscreen")
    }
}

/// The editor preview is only a video surface: the canvas above it owns every
/// gesture and the editor draws its own transport. SwiftUI's `VideoPlayer`
/// wrapped a full `AVPlayerViewController`, which still built hidden iOS 26
/// glass playback controls, ran VisionKit Live Text analysis on paused frames
/// and published Now Playing info. Those control animations also broke
/// XCUITest's animation-idle tracking, stalling editor UI tests for 60s per
/// action once the keyboard animated in (KRI-168).
private struct NativeEditorPlayerSurface: UIViewRepresentable {
    let player: AVPlayer
    /// Fires once the layer has a frame to show (immediately if it already does). Returning false means the
    /// owner rejected the report, so it is offered again on the next update instead of being consumed.
    var readinessID: NativeEditorPreviewWork? = nil
    var onReadyForDisplay: (@MainActor () -> Bool)? = nil

    func makeUIView(context: Context) -> Surface {
        let view = Surface()
        view.configure(player: player, ticket: readinessID, onReady: onReadyForDisplay)
        return view
    }

    func updateUIView(_ view: Surface, context: Context) {
        view.configure(player: player, ticket: readinessID, onReady: onReadyForDisplay)
    }

    final class Surface: UIView {
        override class var layerClass: AnyClass { AVPlayerLayer.self }
        var playerLayer: AVPlayerLayer { layer as! AVPlayerLayer }
        private var readiness: NSKeyValueObservation?

        private weak var observedPlayer: AVPlayer?
        private weak var observedItem: AVPlayerItem?
        private var observedTicket: NativeEditorPreviewWork?
        private var reportedReady = false
        private var onReady: (@MainActor () -> Bool)?

        func configure(player: AVPlayer, ticket: NativeEditorPreviewWork?, onReady: (@MainActor () -> Bool)?) {
            let item = player.currentItem
            self.onReady = onReady
            guard observedPlayer !== player || observedItem !== item || observedTicket != ticket else {
                if !reportedReady, onReady != nil, playerLayer.isReadyForDisplay { reportSoon() }
                return
            }
            readiness?.invalidate()
            observedPlayer = player; observedItem = item; observedTicket = ticket; reportedReady = false
            if playerLayer.player !== player { playerLayer.player = player }
            guard onReady != nil else { return }
            readiness = playerLayer.observe(\.isReadyForDisplay, options: [.initial, .new]) { [weak self] layer, _ in
                guard layer.isReadyForDisplay else { return }
                self?.reportSoon()
            }
        }

        nonisolated private func reportSoon() {
            DispatchQueue.main.async { [weak self] in self?.reportIfReady() }
        }

        private func reportIfReady() {
            guard !reportedReady, let onReady, let player = observedPlayer, let item = observedItem,
                  playerLayer.player === player, playerLayer.isReadyForDisplay, player.currentItem === item else { return }
            if onReady() { reportedReady = true }
        }

        override init(frame: CGRect) {
            super.init(frame: frame)
            playerLayer.videoGravity = .resizeAspect
            backgroundColor = .clear
        }

        required init?(coder: NSCoder) { fatalError("init(coder:) has not been implemented") }
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
    /// A media card's `editor_style.zoom`: the card is drawn `scale * zoom` wide. Only the frame shown before the
    /// live preview is ready reads it; the ready frame is the rendered footprint. Resizing keeps writing `scale`.
    let zoom: CGFloat

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
        rotation: Double = 0,
        zoom: CGFloat = 1
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
        self.zoom = zoom
    }

    var id: String { "\(item.kind.rawValue)-\(item.id)" }
}

private enum NativePreviewRender: Equatable {
    case text, caption, mediaOverlay, visualBlock, carousel, runtimeOnly
    /// The framed Talk-to-camera speaker: its pixels are the video's own, so the object only carries the hit box.
    case speaker
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
        var parts = [String(localized: "\(nativeTimecode(object.item.start)) to \(nativeTimecode(object.item.end))")]
        if let position = object.position, onMove != nil {
            parts.append(String(localized: "position \(Int((position.x * 100).rounded()))%, \(Int((position.y * 100).rounded()))%"))
        }
        if onResize != nil { parts.append(String(localized: "size \(Int((object.scale * 100).rounded()))%")) }
        if isSelected { parts.append(String(localized: "selected")) }
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
            if !showsContent || object.render == .speaker {
                Color.clear
            } else if object.render == .caption {
                Text(object.text ?? String(localized: "Caption"))
                    .font(KriaFont.body(15).weight(.semibold))
                    .foregroundStyle(.white)
                    .padding(.horizontal, 10)
                    .frame(maxWidth: .infinity, minHeight: 44)
                    .background(.black.opacity(0.76), in: RoundedRectangle(cornerRadius: 8, style: .continuous))
            } else if object.render == .text {
                Text(object.text ?? object.title)
                    .font(NativeFontCatalog.shared.previewFont(object.style, size: 22))
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
                Label(object.detail.map { String(localized: "\($0) • preview in final render") } ?? String(localized: "Preview in final render"), systemImage: "sparkles.tv")
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
            // A selected drawing's outline comes from NativeDrawingTransformLayer, which follows the live gesture;
            // drawing a second one here (from the unchanged document) would show two boxes mid-drag.
            if isSelected, object.item.kind != .drawing {
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
        .accessibilityLabel(String(localized: "\(object.title): \(object.text ?? object.detail ?? "")"))
        .accessibilityValue(accessibilityValue)
        .accessibilityIdentifier("native-editor-preview-\(object.item.kind.rawValue)-\(object.item.id)")
        .accessibilityAddTraits(isSelected ? AccessibilityTraits.isSelected : [])
        .accessibilityAction(named: String(localized: "Select \(object.title.lowercased())")) {
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

struct NativeSplitScreenReorderUnit {
    var clipIDs: [UUID]
    let start: TimeInterval
    let end: TimeInterval

    static func make(document: EditorDocument, clips: [EditorClip]) -> [Self] {
        var result: [Self] = []
        var sectionIndices: [String: Int] = [:]
        for clip in clips {
            let slot = document.clips.first { $0.id == clip.slotID }
            if let section = document.splitScreenSections.first(where: { $0.id == slot?.splitSectionID }), let slot {
                if let index = sectionIndices[section.id] {
                    result[index].clipIDs.append(clip.id)
                } else {
                    let start = clip.start - slot.splitOffsetS
                    sectionIndices[section.id] = result.count
                    result.append(Self(clipIDs: [clip.id], start: start, end: start + section.durationS))
                }
            } else {
                result.append(Self(clipIDs: [clip.id], start: clip.start, end: clip.end))
            }
        }
        return result
    }

    static func reorderedWindows(document: EditorDocument, clips: [EditorClip], moving id: UUID, to targetID: UUID) -> [(start: TimeInterval, end: TimeInterval)] {
        guard let source = clips.first(where: { $0.id == id }), let target = clips.first(where: { $0.id == targetID }),
              let from = document.clips.firstIndex(where: { $0.id == source.slotID }),
              let to = document.clips.firstIndex(where: { $0.id == target.slotID }) else { return [] }
        var reordered = document
        reordered.clips = NativeEditorInteraction.reorderedSlots(document.clips, from: from, to: to)
        let projection = NativeEditorInteraction.timelineProjection(slots: reordered.clips, carousel: reordered.carouselMoment,
                                                                   splitSections: reordered.splitScreenSections)
        let projected = projection.clipWindows.compactMap { window -> EditorClip? in
            let slot = reordered.clips[window.sourceIndex]
            guard var clip = clips.first(where: { $0.slotID == slot.id }) else { return nil }
            clip.start = window.start
            clip.end = window.end
            return clip
        }
        return make(document: reordered, clips: projected).map { ($0.start, $0.end) }
    }
}

/// A split section moves as one unit while its panel clips keep their relative source windows.
private struct ClipReorderDrag {
    let clipID: UUID
    let units: [NativeSplitScreenReorderUnit]
    let from: Int
    var travel: TimeInterval
    var target: Int
    let windows: [(start: TimeInterval, end: TimeInterval)]
    var preview: [(start: TimeInterval, end: TimeInterval)]

    func isLifted(_ id: UUID) -> Bool { units[from].clipIDs.contains(id) }

    /// The position a clip originally at `index` occupies once `from` has moved to `target`.
    func previewPosition(of index: Int) -> Int {
        if index == from { return target }
        if from < index, index <= target { return index - 1 }
        if target <= index, index < from { return index + 1 }
        return index
    }
}

/// One in-flight drag's auto-scroll bookkeeping. A class (not a struct) so
/// the display-link tick closure can read/write it by reference without
/// SwiftUI re-rendering on every tick.
private final class DragAutoScrollContext {
    let clockAtStart: TimeInterval
    let viewportGlobalRange: ClosedRange<CGFloat>
    /// The raw (uncompensated) translation from the most recent finger
    /// sample, in seconds. Auto-scroll adds the clock delta on top of this.
    var lastRaw: TimeInterval = 0
    /// The finger's most recent global x, used to (re)compute velocity.
    var lastX: CGFloat
    /// The marker (moved/trimmed edge) `applyRaw` returned last time it was
    /// called, from either the finger path or a tick. Comparing this across
    /// ticks is how a pinned clamp is detected.
    var lastAppliedMarker: TimeInterval?
    /// Applies a translation (already clock-compensated) to the session and
    /// returns a marker for pin detection, or nil if the item disappeared.
    let applyRaw: (TimeInterval) -> TimeInterval?

    init(clockAtStart: TimeInterval, lastX: CGFloat, viewportGlobalRange: ClosedRange<CGFloat>, applyRaw: @escaping (TimeInterval) -> TimeInterval?) {
        self.clockAtStart = clockAtStart
        self.lastX = lastX
        self.viewportGlobalRange = viewportGlobalRange
        self.applyRaw = applyRaw
    }
}

/// Weak-target CADisplayLink, same structure as
/// NativeTextAnimationPreviewDisplayLinkTarget (NativeTextAnimationPreview.swift) —
/// reused rather than writing a second one. Frame rate capped at 60 so a
/// 120Hz ProMotion touch stream doesn't double up with tick-driven updates.
final class NativeTimelineEdgeScroller {
    private final class Target: NSObject {
        weak var owner: NativeTimelineEdgeScroller?
        init(owner: NativeTimelineEdgeScroller) { self.owner = owner }
        @objc func tick(_ link: CADisplayLink) { owner?.handle(link) }
    }

    private var displayLink: CADisplayLink?
    private var lastTimestamp: CFTimeInterval?
    /// Called every frame with the elapsed time since the previous tick.
    var onTick: ((TimeInterval) -> Void)?

    var isRunning: Bool { displayLink != nil }

    func start() {
        guard displayLink == nil else { return }
        let target = Target(owner: self)
        let link = CADisplayLink(target: target, selector: #selector(Target.tick(_:)))
        link.preferredFrameRateRange = CAFrameRateRange(minimum: 30, maximum: 60, preferred: 60)
        link.add(to: .main, forMode: .common)
        displayLink = link
        lastTimestamp = nil
    }

    func stop() {
        displayLink?.invalidate()
        displayLink = nil
        lastTimestamp = nil
    }

    private func handle(_ link: CADisplayLink) {
        let elapsed = lastTimestamp.map { link.timestamp - $0 } ?? (1.0 / 60)
        lastTimestamp = link.timestamp
        guard elapsed > 0 else { return }
        onTick?(elapsed)
    }
}

struct NativeSplitScreenRows {
    let titles: [String]
    private let rowsBySlot: [String: Int]

    /// The panel titles double as row keys (and identifier suffixes), so they stay English; this is what the creator reads.
    static func displayTitle(_ key: String) -> String {
        switch key {
        case "Video": String(localized: "Video")
        case "Top": String(localized: "Top", comment: "Split screen: top panel")
        case "Middle": String(localized: "Middle", comment: "Split screen: middle panel")
        case "Bottom": String(localized: "Bottom", comment: "Split screen: bottom panel")
        default: key
        }
    }

    static func panelTitle(index: Int, count: Int) -> String {
        index == 0 ? "Top" : index == count - 1 ? "Bottom" : "Middle"
    }

    init(document: EditorDocument) {
        let sections = document.splitScreenSections
        guard !sections.isEmpty else {
            titles = ["Video"]
            rowsBySlot = [:]
            return
        }
        let ordinary = document.clips.contains { !$0.removed && $0.splitSectionID == nil }
        var labels = ordinary ? ["Video"] : []
        labels.append("Top")
        if sections.contains(where: { $0.panelCount == 3 }) { labels.append("Middle") }
        labels.append("Bottom")
        titles = labels
        var indices: [String: Int] = [:]
        for slot in document.clips where !slot.removed {
            guard let id = slot.id,
                  let section = sections.first(where: { $0.id == slot.splitSectionID }),
                  let panel = slot.splitPanelIndex else { continue }
            indices[id] = labels.firstIndex(of: Self.panelTitle(index: panel, count: section.panelCount))
        }
        rowsBySlot = indices
    }

    func row(slotID: String?) -> Int { slotID.flatMap { rowsBySlot[$0] } ?? 0 }

    /// True for a clip that sits in a split panel, as opposed to an ordinary full-screen clip.
    func isMember(slotID: String?) -> Bool { slotID.map { rowsBySlot[$0] != nil } ?? false }

    func row(panel: Int, count: Int) -> Int? { titles.firstIndex(of: Self.panelTitle(index: panel, count: count)) }
}

/// Free time in one panel of a split: between its members, and after the last one up to the section end.
struct NativeSplitGap: Identifiable, Equatable {
    let sectionID: String
    let panel: Int
    let index: Int
    let start: TimeInterval
    let end: TimeInterval
    let row: Int
    let canAdd: Bool
    var id: String { "\(sectionID)-\(panel)-\(index)" }
}

enum NativeSplitGaps {
    static let minimumDuration: TimeInterval = 0.05

    static func make(document: EditorDocument, clips: [EditorClip], rows: NativeSplitScreenRows,
                     canAdd: (String, Int) -> Bool) -> [NativeSplitGap] {
        guard !document.splitScreenSections.isEmpty else { return [] }
        var windows: [String: (start: TimeInterval, end: TimeInterval)] = [:]
        for clip in clips { if let id = clip.slotID { windows[id] = (clip.start, clip.end) } }
        var gaps = [NativeSplitGap]()
        for section in document.splitScreenSections {
            let members = document.clips.compactMap { slot -> (panel: Int, start: TimeInterval, end: TimeInterval, offset: TimeInterval)? in
                guard !slot.removed, slot.splitSectionID == section.id, let id = slot.id,
                      let panel = slot.splitPanelIndex, let window = windows[id] else { return nil }
                return (panel, window.start, window.end, slot.splitOffsetS)
            }
            guard let origin = members.map({ $0.start - $0.offset }).min() else { continue }
            let sectionEnd = origin + section.durationS
            for panel in 0..<section.panelCount {
                guard let row = rows.row(panel: panel, count: section.panelCount) else { continue }
                let addable = canAdd(section.id, panel)
                var cursor = origin
                var index = 0
                func record(_ start: TimeInterval, _ end: TimeInterval) {
                    guard end - start >= minimumDuration else { return }
                    gaps.append(NativeSplitGap(sectionID: section.id, panel: panel, index: index, start: start, end: end,
                                               row: row, canAdd: addable))
                    index += 1
                }
                for member in members.filter({ $0.panel == panel }).sorted(by: { $0.start < $1.start }) {
                    record(cursor, member.start)
                    cursor = max(cursor, member.end)
                }
                record(cursor, sectionEnd)
            }
        }
        return gaps
    }
}

/// The playhead stays fixed in the viewport while every track moves beneath it.
/// Horizontal panning seeks the preview to the time underneath the playhead.
struct NativeMiniStrip: View {
    @Environment(\.accessibilityReduceMotion) private var reduceMotion
    @ScaledMetric(relativeTo: .caption2) private var laneLabelSize: CGFloat = 9
    @ScaledMetric(relativeTo: .caption2) private var laneLabelWidth: CGFloat = 66
    @ObservedObject var session: NativeEditorSession
    @ObservedObject private var clock: NativeEditorPlaybackClock
    /// Extra space reserved at the bottom of the scrollable lane content so
    /// the last lane can always scroll clear of the floating tool island
    /// (KRI-131). Zero at other call sites, where there is no island.
    let bottomClearance: CGFloat
    let isCovered: Bool
    @State private var zoom: CGFloat = 1
    @State private var pinchAnchor: CGFloat = 1
    @State private var isPinching = false
    @State private var viewportWidth: CGFloat = 0
    @State private var panStartTime: TimeInterval?
    @State private var lastPanAt = Date.distantPast
    @State private var playheadStartTime: TimeInterval?
    /// A system-cancelled drag never reaches `onEnded`; these let `onChange` clear the stale start times.
    @GestureState private var panActive = false
    /// A flick keeps the timeline gliding after release; any new touch, or leaving the screen, ends it.
    @State private var coastTask: Task<Void, Never>?
    @GestureState private var playheadActive = false
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
    @State private var laneScrollOffset: CGFloat = 0
    @State private var footageRows = NativeSplitScreenRows(document: EditorDocument())
    @State private var splitGaps: [NativeSplitGap] = []
    @State private var didCacheItems = false
    @State private var isPresentingAddClip = false
    /// Non-nil only while an auto-scroll-eligible drag (timed-block move,
    /// timed-block trim, or a clip's trailing trim) is active.
    @State private var autoScroll: DragAutoScrollContext?
    @State private var edgeScroller = NativeTimelineEdgeScroller()
    /// Non-nil while a clip block is lifted for reorder. The document is not touched until the drop, which
    /// commits one undo step; everything drawn during the drag comes from this value.
    @State private var clipReorder: ClipReorderDrag?
    @State private var reorderHaptic = UIImpactFeedbackGenerator(style: .medium)
    @State private var reorderTickHaptic = UISelectionFeedbackGenerator()
    @State private var reorderDropHaptic = UIImpactFeedbackGenerator(style: .light)
    /// A second, independent safety net beside each gesture's own cancel
    /// detection (@GestureState reset) — stops the scroller if the app
    /// leaves .active outright (backgrounding, Control Center, a system
    /// alert), which can race gesture-level cancellation.
    @Environment(\.scenePhase) private var scenePhase
    /// KRI-166: the "+" opens a small chooser (Video/Visual/Text) instead of
    /// jumping straight to the add-clip sheet, so Visual/Text are reachable
    /// from the same discoverable entry point video already had.
    @State private var isPresentingAddMenu = false
    /// Selecting Video/Visual/Text (`NativeMiniStrip`'s owner routes these).
    var onSelectVisual: () -> Void = {}
    var onSelectText: () -> Void = {}
    /// KRI-589: the "+" menu's Drawing row; nil hides it (the variant does not open `drawings.add`).
    var onSelectDrawing: (() -> Void)?
    /// A tap on the "+" of a gap in a split panel. The strip has already moved the playhead into the gap; the owner
    /// opens the Split tool so its add flow targets that panel.
    var onOpenSplitAdd: ((String, Int) -> Void)?

    private let minimumZoom: CGFloat = 0.5
    private let maximumZoom: CGFloat = 24
    private var basePixelsPerSecond: CGFloat { max(1, viewportWidth) / 6 }
    private let filmstripHeight: CGFloat = 44
    private var filmstripContentHeight: CGFloat {
        CGFloat(footageRows.titles.count) * filmstripHeight + CGFloat(footageRows.titles.count - 1) * rowGap
    }
    private let secondaryLaneHeight: CGFloat = 44
    private let rowGap: CGFloat = 6
    private let quickAddSpring = Animation.spring(response: 0.34, dampingFraction: 0.88)

    init(
        session: NativeEditorSession,
        bottomClearance: CGFloat = 0,
        isCovered: Bool = false,
        onSelectVisual: @escaping () -> Void = {},
        onSelectText: @escaping () -> Void = {},
        onSelectDrawing: (() -> Void)? = nil,
        onOpenSplitAdd: ((String, Int) -> Void)? = nil
    ) {
        self.session = session
        self.bottomClearance = bottomClearance
        self.isCovered = isCovered
        self.onSelectVisual = onSelectVisual
        self.onSelectText = onSelectText
        self.onSelectDrawing = onSelectDrawing
        self.onOpenSplitAdd = onOpenSplitAdd
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
    private var drawingItems: [NativeEditorTimelineItem] { timelineItems.filter { $0.kind == .drawing } }
    private var backgroundWindowItems: [NativeEditorTimelineItem] { timelineItems.filter { $0.kind == .speakerBackgroundWindow } }
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
            ("VISUAL", visualBlockItems),
            // KRI-1162: present only while the edit has timed backgrounds, so every other edit keeps its lane list.
            ("BACKGROUND", backgroundWindowItems), ("MOTION", motionSceneItems),
            ("CAMERA", cameraEffectItems), ("DRAW", drawingItems), ("CAROUSEL", carouselItems)
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
        18 + filmstripContentHeight + CGFloat(laneCount - 1) * secondaryLaneHeight + CGFloat(laneCount) * rowGap
    }
    /// The wrapper can use this when it needs an explicit height; leaving the
    /// view unframed is preferred so optional lanes stay visible.
    var preferredHeight: CGFloat { timelineHeight + 88 }
    private var timelineDuration: TimeInterval {
        // The rendered asset (or the server's expected_duration_s) owns the
        // transport boundary. Stale lane ends must not make the clock promise
        // seconds that the player cannot show. timelineScrubDuration widens
        // this while a preview rebuild is pending (mid-gesture or shortly
        // after) so a clip extension or auto-scroll can reach the timeline's
        // true projected end instead of stalling at the stale composition.
        max(0.1, session.timelineScrubDuration)
    }
    private var documentCaptionsEnabled: Bool {
        nativeBool(session.document.captionMeta["enabled"]) ?? !session.document.captionCues.isEmpty
    }
    private var pixelsPerSecond: CGFloat { basePixelsPerSecond * zoom }
    // 20 mirrors the server's `_MAX_CLIPS` pool cap (see
    // NativeEditorSession.addClip) — disabling here keeps the sheet from ever
    // reaching a guard clause the user can't see feedback for.
    // An edit rendered on this iPhone only knows the originals bound when it was
    // planned; adding a clip would upload a full original to the cloud for an
    // edit the phone can never save.
    private var canAddClip: Bool { session.canAddTimelineMedia }

    /// KRI-131: the floating Edit text/Deselect context capsule appears
    /// directly above the tool island whenever a text element is selected.
    /// On a short timeline this region can coincide with content that's
    /// already on screen — e.g. the only "TEXT" row — silently covering the
    /// very item the user just selected. `scrollSelectionClearOfIsland`
    /// nudges that row into view so it can't land underneath the capsule.
    var body: some View {
        VStack(spacing: 6) {
            if isCovered {
                Color.clear.frame(height: 44).accessibilityHidden(true)
            } else {
                // zIndex above the scrollable lanes below so the "+"
                // button's own popover overlay (attached to the button, not
                // hoisted to a separate layer) always paints on top of them.
                controls.zIndex(1)
            }
            if let message = session.addClipUnavailableMessage {
                Text(message).font(KriaFont.body(12)).foregroundStyle(KriaColor.mutedInk)
                    .frame(maxWidth: .infinity, alignment: .leading)
                    .accessibilityIdentifier("native-editor-add-clip-unavailable")
            }
            GeometryReader { viewport in
                ScrollViewReader { proxy in
                    ScrollView(.vertical, showsIndicators: laneCount + footageRows.titles.count - 1 > 4) {
                        HStack(alignment: .top, spacing: 6) {
                            laneLabels
                                .frame(width: laneLabelWidth, alignment: .leading)
                            timeline
                                .simultaneousGesture(timelinePanGesture)
                                .onChange(of: panActive) { _, active in
                                    if !active, panStartTime != nil { lastPanAt = .now; panStartTime = nil }
                                }
                                .onChange(of: playheadActive) { _, active in
                                    if active { stopCoast() } else { playheadStartTime = nil }
                                }
                                .onDisappear { stopCoast() }
                        }
                        .frame(maxWidth: .infinity, minHeight: max(0, viewport.size.height - bottomClearance), alignment: .topLeading)
                        .contentShape(Rectangle())
                    }
                    .contentMargins(.bottom, bottomClearance, for: .scrollContent)
                    .contentMargins(.bottom, bottomClearance, for: .scrollIndicators)
                    .scrollBounceBehavior(.basedOnSize)
                    .accessibilityIdentifier("native-editor-lane-scroll")
                    .accessibilityHidden(isCovered)
                    .onScrollGeometryChange(for: CGFloat.self) { $0.contentOffset.y } action: { _, offset in
                        laneScrollOffset = offset
                    }
                    .onChange(of: session.selectionRequest) { _, _ in
                        scrollSelectionClearOfIsland(proxy: proxy)
                        scrollSelectedClipRowClearOfIsland(proxy: proxy, viewportHeight: viewport.size.height)
                    }
                    .onChange(of: bottomClearance) { _, _ in
                        scrollSelectedClipRowClearOfIsland(proxy: proxy, viewportHeight: viewport.size.height)
                    }
                }
            }
        }
        .padding(.vertical, 4)
        .accessibilityElement(children: .contain)
        .sensoryFeedback(.selection, trigger: boundaryFeedback)
        .onChange(of: scenePhase) { _, phase in
            // Second, independent safety net beside each gesture's own
            // @GestureState cancel detection — a hard app-suspend (Control
            // Center, backgrounding, a system alert) can race gesture-level
            // cancellation, and this must never leave the timeline scrolling
            // with no finger attached.
            if phase != .active { endAutoScroll(); cancelClipReorder() }
        }
        .sheet(isPresented: $isPresentingAddClip) { NativeEditorAddClipSheet(session: session) }
        // The add-clip sheet closes as soon as a file is chosen, so a failure has to surface here.
        // Suppressed while the sheet is open, where it shows the same error inline.
        .alert("Couldn’t add clip", isPresented: Binding(
            get: { session.addClipError != nil && !isPresentingAddClip && !session.addClipAlertSuppressed },
            set: { if !$0 { session.addClipError = nil } }
        )) {
            Button("OK", role: .cancel) {}
        } message: {
            Text(session.addClipError ?? "")
        }
        .alert("Couldn’t paste", isPresented: Binding(
            get: { session.pasteError != nil },
            set: { if !$0 { session.pasteError = nil } }
        )) {
            Button("OK", role: .cancel) {}
        } message: {
            Text(session.pasteError ?? "")
        }
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
            .disabled(session.document.editorState == "empty")
            .accessibilityLabel(session.isPlaying ? String(localized: "Pause preview") : String(localized: "Play preview"))
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
            // The transport reports playable media; timeline geometry may extend
            // farther so pending or failed clip additions remain editable.
            Text(timecode(session.playbackDuration))
                .font(.system(size: 13, weight: .regular, design: .monospaced))
                .foregroundStyle(KriaColor.zinc)
                .monospacedDigit()
                .lineLimit(1)
                .minimumScaleFactor(0.8)
                .frame(width: 56, alignment: .leading)
                .accessibilityLabel("Duration")
                .accessibilityValue(timecode(session.playbackDuration))
                .accessibilityIdentifier("native-editor-duration")

            Spacer(minLength: 8)
            Button {
                withAnimation(reduceMotion ? nil : quickAddSpring) { isPresentingAddMenu.toggle() }
            } label: {
                // The picker sheet has already closed by the time the upload runs, so this is the
                // only place the user can see that a clip is still on its way.
                if (session.isAddingClip || session.isPastingImage) && !isPresentingAddMenu {
                    ProgressView().frame(width: 44, height: 44)
                } else {
                    Image(systemName: "plus")
                        .font(.system(size: 17, weight: .semibold))
                        .rotationEffect(.degrees(isPresentingAddMenu ? 45 : 0))
                        .frame(width: 44, height: 44)
                }
            }
            .buttonStyle(.plain)
            .foregroundStyle(KriaColor.ink)
            .background(KriaColor.softZinc, in: Circle())
            .disabled(session.isAddingClip)
            // Anchored to the button's own bounds (no separate coordinate-
            // space plumbing needed) and elevated above the scrollable lanes
            // below via `.zIndex` on the button itself. The scrim lives in
            // this SAME overlay, behind the menu, rather than as its own
            // top-level `.overlay()` on an ancestor — an ancestor's overlay
            // paints (and hit-tests) above ALL of its descendants regardless
            // of any zIndex set deeper inside, which silently ate every tap
            // meant for the menu's rows when the scrim lived up there.
            .overlay(alignment: .topTrailing) {
                // The ZStack itself is persistent and only its children are
                // conditional: a transition on a child of a ZStack that is
                // itself being inserted never runs, so the menu's box would
                // grow while its labels just faded in at full size (the text
                // visibly led the box). Here the menu — box and labels — is
                // one view with one scale+fade transition, so they can't
                // drift apart.
                ZStack(alignment: .topTrailing) {
                    if isPresentingAddMenu {
                        // Centered on the button's top-trailing corner (the
                        // offset re-centers what `.topTrailing` alignment
                        // would otherwise extend only left/down), so it
                        // covers the whole strip around the menu. Bounded by
                        // the timeline area's own `.clipped()` in
                        // `connectedEditorArea`.
                        Color.black.opacity(0.001)
                            .frame(width: 4000, height: 4000)
                            .offset(x: 2000, y: -2000)
                            .contentShape(Rectangle())
                            .onTapGesture { closeAddMenu() }
                            .transition(.identity)
                        quickAddMenu
                            .offset(y: 52)
                            .transition(.scale(scale: 0.3, anchor: .topTrailing).combined(with: .opacity))
                    }
                }
            }
            .zIndex(2)
            .accessibilityLabel(isPresentingAddMenu ? String(localized: "Close add menu") : (session.isAddingClip ? String(localized: "Adding clip") : String(localized: "Add to timeline")))
            .accessibilityIdentifier("native-editor-add-clip")
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

    private func closeAddMenu() {
        withAnimation(reduceMotion ? nil : quickAddSpring) { isPresentingAddMenu = false }
    }

    /// KRI-166: the "+" chooser. Box and labels are one view so they scale in
    /// together from the button's corner. Video keeps its existing add-clip
    /// sheet; Visual/Text route to the same tool the bottom rail already
    /// opens for them (no separate disabled state here — those tools have
    /// never pre-disabled themselves, any capability limits surface once
    /// the panel is open, same as tapping the rail directly).
    private var quickAddMenu: some View {
        VStack(alignment: .leading, spacing: 2) {
            quickAddRow(String(localized: "Video"), key: "Video", icon: "film", enabled: canAddClip) {
                closeAddMenu()
                #if DEBUG
                if ProcessInfo.processInfo.arguments.contains("-ui-testing-editor-gameday-add") {
                    Task { await session.addGamedayFixtureClip() }
                    return
                }
                #endif
                isPresentingAddClip = true
            }
            if !canAddClip, let reason = session.addClipUnavailableReason {
                Text(reason)
                    .font(KriaFont.body(11))
                    .foregroundStyle(KriaColor.mutedInk)
                    .fixedSize(horizontal: false, vertical: true)
                    .frame(width: 168, alignment: .leading)
                    .padding(.horizontal, 10)
                    .padding(.bottom, 6)
                    .accessibilityIdentifier("native-editor-add-menu-video-unavailable")
            }
            quickAddRow(String(localized: "Visual", comment: "Add menu: a photo or video layered over the video"), key: "Visual", icon: "photo.on.rectangle", enabled: true) {
                closeAddMenu()
                onSelectVisual()
            }
            quickAddRow(String(localized: "Text"), key: "Text", icon: "textformat", enabled: true) {
                closeAddMenu()
                onSelectText()
            }
            if let onSelectDrawing, session.canAddDrawing {
                quickAddRow(String(localized: "Drawing"), key: "Drawing", icon: "pencil.tip", enabled: true) {
                    closeAddMenu()
                    onSelectDrawing()
                }
            }
            // KRI-647: the system Paste control reads the clipboard without the "Allow Paste" prompt and
            // disables itself when there is no text or picture to paste.
            PasteButton(supportedContentTypes: PasteboardIntake.supportedTypes) { providers in
                closeAddMenu()
                Task { await session.paste(providers: providers) }
            }
            .labelStyle(.titleAndIcon)
            .buttonBorderShape(.roundedRectangle(radius: 10))
            .tint(KriaColor.softZinc)
            .frame(width: 168, height: 44, alignment: .leading)
            .accessibilityIdentifier("native-editor-add-menu-paste")
        }
        // Accessibility grouping sits on the content INSIDE the glass, never
        // on/above it: `.glassEffect()` collapses the hit-test frame of any
        // ancestor carrying `.accessibilityElement(children: .contain)` or an
        // identifier (see NativeEditorIslandSurface).
        .accessibilityElement(children: .contain)
        .accessibilityLabel("Add to timeline")
        .padding(6)
        // Same blurred glass surface as the bottom tool rail.
        .nativeEditorIslandSurface(cornerRadius: 22)
        .fixedSize()
    }

    private func quickAddRow(_ title: String, key: String? = nil, icon: String, enabled: Bool, action: @escaping () -> Void) -> some View {
        Button(action: action) {
            HStack(spacing: 10) {
                Image(systemName: icon).font(.system(size: 15, weight: .medium)).frame(width: 22)
                Text(title).font(KriaFont.body(14).weight(.medium))
                Spacer(minLength: 12)
            }
            .padding(.horizontal, 10)
            .frame(width: 168, height: 44, alignment: .leading)
            .contentShape(Rectangle())
        }
        .buttonStyle(.plain)
        .foregroundStyle(enabled ? KriaColor.ink : KriaColor.mutedInk)
        .disabled(!enabled)
        .accessibilityIdentifier("native-editor-add-menu-\((key ?? title).lowercased())")
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
        // KRI-131: `native-editor-lane-label-last` marks whichever row is
        // visually last (AUDIO when present, else the last packed row, else
        // VIDEO) so UI tests can assert it scrolls clear of the floating
        // tool island without hardcoding lane composition.
        let rowsList = rows
        let hasAudioLane = !clips.isEmpty
        return VStack(spacing: rowGap) {
            Color.clear.frame(height: 18)
            ForEach(footageRows.titles, id: \.self) { title in
                // Split rows carry no visible name: the preview is the control surface. VoiceOver keeps it.
                Text(NativeSplitScreenRows.displayTitle(title))
                    .lineLimit(1)
                    .frame(maxWidth: .infinity, alignment: .leading)
                    .frame(height: filmstripHeight)
                    .foregroundStyle(title == "Video" ? KriaColor.zinc : Color.clear)
                    .accessibilityLabel(NativeSplitScreenRows.displayTitle(title))
                    .accessibilityIdentifier(title == "Video"
                        ? (rowsList.isEmpty && !hasAudioLane ? "native-editor-lane-label-last" : "native-editor-lane-label-video")
                        : "native-editor-lane-label-split-\(title.lowercased())")
            }
            ForEach(Array(rowsList.enumerated()), id: \.element.id) { index, row in
                laneLabel(Self.laneDisplayName(row.title))
                    .frame(height: CGFloat(rowCount(row)) * secondaryLaneHeight + CGFloat(rowCount(row) - 1) * rowGap, alignment: .top)
                    .accessibilityIdentifier(
                        index == rowsList.count - 1 && !hasAudioLane
                            ? "native-editor-lane-label-last" : "native-editor-lane-label-\(row.id)"
                    )
            }
            if hasAudioLane { laneLabel(String(localized: "Audio", comment: "Timeline lane label: the clips' sound")).accessibilityIdentifier("native-editor-lane-label-last") }
        }
        .font(.system(size: laneLabelSize, weight: .bold, design: .rounded))
        .foregroundStyle(KriaColor.zinc)
    }

    /// Row ids stay upper-case keys; the label the creator reads is sentence case ("SFX" is an acronym and stays).
    private static func laneDisplayName(_ key: String) -> String {
        switch key {
        case "TEXT": String(localized: "Text", comment: "Timeline lane label")
        case "CAPTIONS": String(localized: "Captions", comment: "Timeline lane label")
        case "MUSIC": String(localized: "Music", comment: "Timeline lane label")
        case "SFX": String(localized: "SFX", comment: "Timeline lane label: sound effects (acronym)")
        case "OVERLAY": String(localized: "Overlay", comment: "Timeline lane label: photo or video card over the video")
        case "VISUAL": String(localized: "Visual", comment: "Timeline lane label")
        case "BACKGROUND": String(localized: "Background", comment: "Timeline lane label")
        case "MOTION": String(localized: "Motion", comment: "Timeline lane label")
        case "CAMERA": String(localized: "Camera", comment: "Timeline lane label")
        case "DRAW": String(localized: "Draw", comment: "Timeline lane label")
        case "CAROUSEL": String(localized: "Carousel", comment: "Timeline lane label")
        default: key.prefix(1) + key.dropFirst().lowercased()
        }
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
            // Read fresh on every body evaluation, same as `width`/`playheadX`
            // above — not cached via onChange. A begin callback built from
            // this render's closures always captures a viewport reading no
            // staler than the view's own layout, and this value changes far
            // less often than the vertical lane-list scroll would make an
            // onChange-driven cache churn.
            let globalFrame = proxy.frame(in: .global)
            let viewportGlobalRange: ClosedRange<CGFloat> = globalFrame.width > 0
                ? globalFrame.minX...globalFrame.maxX : 0...0
            // Snapshot hit regions from this layout pass. A clip selection
            // can seek the clock before simultaneous gestures finish, so
            // checking live geometry here could classify that original clip
            // tap against a later timeline position.
            let interactiveTapFrames = timelineInteractiveTapFrames(playheadX: playheadX)
            ZStack(alignment: .topLeading) {
                VStack(spacing: rowGap) {
                    ruler(width: width, playheadX: playheadX)
                    filmstrip(width: width, playheadX: playheadX, viewportGlobalRange: viewportGlobalRange)
                    ForEach(rows) { row in
                        timedLane(title: row.title, items: row.items, color: KriaColor.softZinc, playheadX: playheadX, viewportGlobalRange: viewportGlobalRange)
                            // KRI-131: lets a text selection scroll its row
                            // clear of the floating context capsule.
                            .id(row.id)
                    }
                    if !clips.isEmpty { originalAudioLane(playheadX: playheadX) }
                }
                .clipped()

                NativePlayhead(height: timelineHeight)
                    .position(x: playheadX, y: timelineHeight / 2)
                    .allowsHitTesting(false)
                Color.clear
                    .frame(width: 44, height: 44)
                    .contentShape(Rectangle())
                    .position(x: playheadX, y: 22)
                    .gesture(DragGesture(minimumDistance: 3, coordinateSpace: .global)
                        .updating($playheadActive) { _, active, _ in active = true }
                        .onChanged { value in
                            let start = playheadStartTime ?? clock.currentTime
                            playheadStartTime = start
                            seek(to: start + Double(value.translation.width / pixelsPerSecond))
                        }
                        .onEnded { _ in playheadStartTime = nil })
                    .accessibilityLabel("Playhead")
                    .accessibilityIdentifier("native-editor-playhead")
                if let hint = session.splitTrimHint {
                    Text(hint)
                        .font(KriaFont.body(12).weight(.semibold))
                        .foregroundStyle(.white)
                        .lineLimit(1)
                        .padding(.horizontal, 10)
                        .frame(height: 24)
                        .background(KriaColor.ink, in: Capsule())
                        .position(x: playheadX, y: 12)
                        .allowsHitTesting(false)
                        .accessibilityIdentifier("native-editor-timeline-trim-hint")
                }
            }
            .clipped()
            // The timeline content itself owns the full hit shape, so a
            // background gesture cannot receive blank-timeline taps. Observe
            // its taps here and exclude every interactive surface before
            // closing clip context.
            .contentShape(Rectangle())
            .simultaneousGesture(
                SpatialTapGesture().onEnded { value in
                    guard !interactiveTapFrames.contains(where: { $0.contains(value.location) }) else { return }
                    dismissClipSelection()
                }
            )
            .background(TimelinePinchCapture { scale, ended in
                session.setTimelinePinching(!ended)
                if !ended, !isPinching {
                    endAutoScroll(); endTrimHaptics()
                    endClipReorder(commit: false)
                    moveAlignment = nil; movePreviousEdges = nil
                }
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
            .accessibilityValue(String(localized: "\(timecode(clock.currentTime)) of \(timecode(timelineDuration)). Zoom \(Int((zoom * 100).rounded())) percent"))
            .accessibilityAdjustableAction { direction in
                let delta: TimeInterval = direction == .increment ? 1 : -1
                seek(to: clock.currentTime + delta)
            }

        }
        .frame(height: timelineHeight)
        .onAppear { refreshItems() }
        // A change from elsewhere (undo, chat edit) mid-drag makes the lifted order stale: drop it uncommitted.
        .onChange(of: session.document) { _, _ in cancelClipReorder(); refreshItems() }
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
                    let label = step < 0.1 ? String(localized: "\(second, format: .number.precision(.fractionLength(2)))s", comment: "Timeline ruler tick in seconds")
                        : step < 1 ? String(localized: "\(second, format: .number.precision(.fractionLength(1)))s", comment: "Timeline ruler tick in seconds") : String(localized: "\(second, format: .number.precision(.fractionLength(0)))s", comment: "Timeline ruler tick in seconds")
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
            // timelineScrubDuration, not session.duration directly — otherwise
            // this lane stops growing mid-extension while the duration label
            // above it keeps growing, visibly disagreeing with the ruler.
            let end = min(viewport.size.width, playheadX + CGFloat(session.timelineScrubDuration - clock.currentTime) * pixelsPerSecond)
            Label("Original audio", systemImage: "waveform")
                .font(KriaFont.body(11))
                .lineLimit(1)
                .padding(.horizontal, 10)
                .frame(width: max(0, end - start), height: secondaryLaneHeight, alignment: .leading)
                .background(KriaColor.softZinc, in: RoundedRectangle(cornerRadius: 8))
                .offset(x: start)
                .accessibilityLabel(String(localized: "Original audio"))
                .accessibilityIdentifier("native-editor-original-audio")
        }
        .frame(height: secondaryLaneHeight)
        .clipped()
    }

    private func filmstrip(width: CGFloat, playheadX: CGFloat, viewportGlobalRange: ClosedRange<CGFloat>) -> some View {
        ZStack(alignment: .topLeading) {
            Canvas { context, size in
                for (index, clip) in clips.enumerated() {
                    let frame = displayClipFrame(clip, index: index, playheadX: playheadX)
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
                for gap in splitGaps {
                    let visible = gapFrame(gap, playheadX: playheadX).intersection(CGRect(origin: .zero, size: size))
                    guard !visible.isNull, visible.width > 0 else { continue }
                    var layer = context
                    layer.clip(to: Path(roundedRect: visible, cornerRadius: 8))
                    layer.fill(Path(visible), with: .color(KriaColor.zinc.opacity(0.12)))
                    // Hatching is filled slanted bands, not strokes.
                    let slant = visible.height * 0.6
                    var x = (visible.minX / 9).rounded(.down) * 9 - slant
                    while x < visible.maxX {
                        var band = Path()
                        band.move(to: CGPoint(x: x, y: visible.maxY))
                        band.addLine(to: CGPoint(x: x + 3, y: visible.maxY))
                        band.addLine(to: CGPoint(x: x + 3 + slant, y: visible.minY))
                        band.addLine(to: CGPoint(x: x + slant, y: visible.minY))
                        band.closeSubpath()
                        layer.fill(band, with: .color(KriaColor.zinc.opacity(0.26)))
                        x += 9
                    }
                }
            }
            .frame(height: filmstripContentHeight)
            .allowsHitTesting(false)

            ForEach(visibleSplitGaps(playheadX: playheadX)) { gap in
                let frame = gapFrame(gap, playheadX: playheadX)
                let visible = frame.intersection(CGRect(x: 0, y: frame.minY, width: width, height: frame.height))
                Color.clear
                    .frame(width: visible.width, height: visible.height)
                    .overlay {
                        if gap.canAdd, visible.width >= 28 {
                            Image(systemName: "plus")
                                .font(.system(size: 13, weight: .bold))
                                .foregroundStyle(KriaColor.ink.opacity(0.55))
                        }
                    }
                    .contentShape(Rectangle())
                    .onTapGesture { openSplitGap(gap) }
                    .position(x: visible.midX, y: visible.midY)
                    // Above the unselected blocks: a panel end handle's overhang must not swallow the add target at its edge.
                    .zIndex(1)
                    .accessibilityElement(children: .ignore)
                    .accessibilityLabel(gapLabel(gap))
                    .accessibilityAddTraits(.isButton)
                    .accessibilityIdentifier("native-editor-split-gap-\(gap.panel)-\(gap.index)")
            }

            ForEach(visibleClips(playheadX: playheadX), id: \.element.id) { index, clip in
                // Only the trailing edge auto-scrolls — a clip's timeline
                // start is fixed by slot order, so the leading handle never
                // tracks the finger and clock-delta compensation would
                // un-trim it. See NativeMiniStrip's design note above.
                let applyTrailingTrim: (TimeInterval, NativeTimelineGestureID) -> TimeInterval? = { raw, token in
                    guard session.ownsTimingGesture(token) else { return nil }
                    session.updateTrim(by: raw, gestureID: token)
                    guard let trimmed = session.timelineClips.first(where: { $0.id == clip.id }) else { return nil }
                    updateTrimHaptics(start: trimmed.start, end: trimmed.end)
                    return trimmed.end
                }
                NativeClipSurface(
                    clip: clip,
                    index: index,
                    frame: displayClipFrame(clip, index: index, playheadX: playheadX),
                    isSelected: session.selectedClipID == clip.id,
                    isLifted: clipReorder?.isLifted(clip.id) == true,
                    canReorder: session.canReorderClips,
                    pixelsPerSecond: pixelsPerSecond,
                    onSelect: { select(clip) },
                    onReorderStart: { beginClipReorder(clip, index: index, viewportGlobalRange: viewportGlobalRange) },
                    onReorderChange: { travel, x in updateClipReorder(travel: travel, globalX: x) },
                    onReorderEnd: { commit in endClipReorder(commit: commit) },
                    onTrimStart: { edge, token in
                        guard session.beginTrim(clipID: clip.id, edge: edge, gestureID: token) else { return false }
                        beginTrimHaptics(selection: EditorSelection(kind: .clip, id: clip.id.uuidString), edge: edge, start: clip.start, end: clip.end)
                        if edge == .trailing {
                            beginAutoScroll(viewportGlobalRange: viewportGlobalRange, applyRaw: { applyTrailingTrim($0, token) })
                        }
                        return true
                    },
                    onTrimChange: { edge, translation, token in
                        guard session.ownsTimingGesture(token) else { return }
                        if edge == .trailing {
                            applyCompensatedDrag(raw: translation, applyRaw: { applyTrailingTrim($0, token) })
                        } else {
                            session.updateTrim(by: translation, gestureID: token)
                            if let trimmed = session.timelineClips.first(where: { $0.id == clip.id }) {
                                updateTrimHaptics(start: trimmed.start, end: trimmed.end)
                            }
                        }
                    },
                    onTrimEnd: { token, commit in
                        guard session.ownsTimingGesture(token) else { return }
                        if commit { session.endTrim(gestureID: token) } else { session.cancelTimingGesture(gestureID: token) }
                        endTrimHaptics(); endAutoScroll()
                    },
                    onMove: { offset in
                        session.selectClip(clip.id)
                        if session.selectedSplitScreenSectionID != nil {
                            session.moveSelectedSplitScreenClip(by: offset < 0 ? -1 : 1)
                        } else {
                            session.moveSelected(by: offset)
                        }
                    },
                    onTrailingDragXChange: { x in updateAutoScrollX(x) },
                    panelTitle: footageRows.titles[footageRows.row(slotID: clip.slotID)],
                    showsEndHandle: footageRows.isMember(slotID: clip.slotID)
                )
            }
            if clips.isEmpty {
                Text("Drop clips here")
                    .font(KriaFont.body(12).weight(.semibold))
                    .foregroundStyle(.white.opacity(0.62))
                    .frame(maxWidth: .infinity, maxHeight: .infinity)
                    .accessibilityHidden(true)
            }
            if let outro = NativeEditorInteraction.outroRange(
                lastClipEnd: lastClipEnd, playbackDuration: timelineDuration,
                isBrandedPreview: session.isPlayingBrandedSourcePreview
            ) {
                outroPlaceholder(range: outro, playheadX: playheadX, width: width)
            }
        }
        .frame(height: filmstripContentHeight)
        .background(alignment: .top) {
            VStack(spacing: rowGap) {
                ForEach(footageRows.titles.indices, id: \.self) { row in
                    Color.clear.frame(height: filmstripHeight).id(Self.footageRowScrollID(row))
                }
            }
            .allowsHitTesting(false)
            .accessibilityHidden(true)
        }
    }

    private static func footageRowScrollID(_ row: Int) -> String { "FOOTAGE-\(row)" }

    /// KRI-166: the end of the editable content, before the branded outro
    /// tail. Never `session.duration` — a rendered MP4's `duration` may
    /// already include the outro (`NativeEditorSession.playbackDuration`).
    private var lastClipEnd: TimeInterval { clips.map(\.end).max() ?? 0 }

    private func outroPlaceholder(range: ClosedRange<TimeInterval>, playheadX: CGFloat, width: CGFloat) -> some View {
        let start = playheadX + CGFloat(range.lowerBound - clock.currentTime) * pixelsPerSecond
        let end = playheadX + CGFloat(range.upperBound - clock.currentTime) * pixelsPerSecond
        let clippedStart = max(0, start)
        let clippedWidth = max(0, min(end, width) - clippedStart)
        return ZStack(alignment: .leading) {
            RoundedRectangle(cornerRadius: 8, style: .continuous)
                .fill(KriaColor.softZinc)
            RoundedRectangle(cornerRadius: 8, style: .continuous)
                .strokeBorder(KriaColor.zinc, style: StrokeStyle(lineWidth: 1, dash: [4, 3]))
            Label("Kria outro", systemImage: "lock.fill")
                .font(KriaFont.body(11))
                .foregroundStyle(KriaColor.mutedInk)
                .lineLimit(1)
                .padding(.horizontal, 10)
        }
        .frame(width: clippedWidth, height: filmstripHeight)
        // `.position()`, not `.offset()` — matches NativeTimelineBar/
        // NativeClipSurface's own pattern for absolute placement in this
        // ZStack; `.offset()` on a sibling here has previously desynced its
        // rendered position during playback (KRI-166 lesson).
        .position(x: clippedStart + clippedWidth / 2, y: filmstripHeight / 2)
        .allowsHitTesting(false)
        .accessibilityElement(children: .ignore)
        .accessibilityLabel("Kria outro")
        .accessibilityHint("Plays after your last clip. Not editable.")
        .accessibilityIdentifier("native-editor-outro-placeholder")
    }

    private func refreshItems() {
        cachedClips = session.timelineClips
        footageRows = NativeSplitScreenRows(document: session.document)
        splitGaps = NativeSplitGaps.make(document: session.document, clips: cachedClips, rows: footageRows) { sectionID, panel in
            session.canAddToSplitPanel(sectionID: sectionID, panel: panel)
        }
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

    private var suppressTimelineSelection: Bool { session.isTimingGestureActive || clipReorder != nil || isPinching || panStartTime != nil || Date().timeIntervalSince(lastPanAt) < 0.2 }

    private func dismissClipSelection() {
        guard session.selection?.kind == .clip, !session.isTimingGestureActive, clipReorder == nil else { return }
        session.select(nil)
    }

    private func timelineInteractiveTapFrames(playheadX: CGFloat) -> [CGRect] {
        var frames = [CGRect]()
        // The draggable playhead owns this small ruler-area target.
        let playheadTarget = CGRect(x: playheadX - 22, y: 0, width: 44, height: 24)
        frames.append(playheadTarget)

        // NativeClipSurface makes short clips at least 44pt wide, centered on
        // their visual frame. Match that touch geometry so another clip keeps
        // its selection instead of being immediately deselected.
        let filmstripOriginY = 18 + rowGap
        for (_, clip) in visibleClips(playheadX: playheadX) {
            let frame = clipFrame(clip, playheadX: playheadX)
            let hitWidth = max(44, frame.width)
            let hitFrame = CGRect(
                x: frame.midX - hitWidth / 2,
                y: filmstripOriginY + frame.minY,
                width: hitWidth,
                height: filmstripHeight
            )
            frames.append(hitFrame)
        }
        for gap in visibleSplitGaps(playheadX: playheadX) where gap.canAdd {
            let frame = gapFrame(gap, playheadX: playheadX)
            frames.append(CGRect(x: frame.minX, y: filmstripOriginY + frame.minY, width: frame.width, height: frame.height))
        }

        var rowOriginY = filmstripOriginY + filmstripContentHeight + rowGap
        for row in rows {
            let packed = row.title == "TEXT" ? NativeEditorInteraction.packLanes(row.items) : []
            let lanes = Dictionary(uniqueKeysWithValues: packed.map { ($0.item.selection, $0.lane) })
            for item in visibleItems(row.items, playheadX: playheadX) {
                let visualFrame = itemFrame(item, playheadX: playheadX)
                let hitWidth = max(44, visualFrame.width)
                let laneOriginY = rowOriginY + CGFloat(lanes[item.selection] ?? 0) * (secondaryLaneHeight + rowGap)
                // NativeTimelineBar centers a 44pt-high Button at the visual
                // item's midpoint, even when the rendered bar is shorter.
                frames.append(CGRect(
                    x: visualFrame.midX - hitWidth / 2,
                    y: laneOriginY + visualFrame.height / 2 - 22,
                    width: hitWidth,
                    height: 44
                ))
            }
            rowOriginY += CGFloat(rowCount(row)) * secondaryLaneHeight + CGFloat(rowCount(row) - 1) * rowGap + rowGap
        }
        return frames
    }

    private func select(_ item: NativeEditorTimelineItem) {
        guard !suppressTimelineSelection else { return }
        alignTimeline(to: item.selection)
    }

    private func select(_ clip: EditorClip) {
        guard !suppressTimelineSelection else { return }
        alignTimeline(to: EditorSelection(kind: .clip, id: clip.id.uuidString))
    }

    private func alignTimeline(to selection: EditorSelection) {
        withAnimation(reduceMotion ? nil : .easeOut(duration: 0.2)) {
            session.select(selection)
        }
    }

    /// Text and split-panel clip selections float a context capsule over the timeline (`showsContext` in
    /// `NativeEditorView`), which can cover the selected row on a short timeline. The topmost footage row never
    /// needs this, so an ordinary clip tap leaves the scroll position alone (it would unsettle scrubbing).
    private func scrollSelectionClearOfIsland(proxy: ScrollViewProxy) {
        guard session.selection?.kind == .text,
              session.document.textElements.first(where: { $0.id == session.selection?.id })?.isCaption != true
        else { return }
        withAnimation(reduceMotion ? nil : .easeOut(duration: 0.2)) {
            proxy.scrollTo("TEXT", anchor: UnitPoint(x: 0.5, y: 0.15))
        }
    }

    /// Scrolls a split panel's footage row up only when the clip toolbar and tool island would cover it. Rows are
    /// 44 pt under an 18 pt ruler, separated by `rowGap`. The topmost row is never covered, and a row that is
    /// already clear is left alone: a scroll would carry the ruler away from the finger that is scrubbing it.
    private func scrollSelectedClipRowClearOfIsland(proxy: ScrollViewProxy, viewportHeight: CGFloat) {
        guard let id = session.selection?.id,
              let clip = clips.first(where: { $0.id.uuidString == id }),
              footageRows.isMember(slotID: clip.slotID)
        else { return }
        let row = footageRows.row(slotID: clip.slotID)
        guard row > 0 else { return }
        let rowBottom = 18 + rowGap + CGFloat(row) * (filmstripHeight + rowGap) + filmstripHeight
        guard rowBottom > laneScrollOffset + viewportHeight - bottomClearance else { return }
        withAnimation(reduceMotion ? nil : .easeOut(duration: 0.2)) {
            proxy.scrollTo(Self.footageRowScrollID(row), anchor: .bottom)
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
            if let title = session.document.music?.raw["title"]?.stringValue { return String(localized: "Music: \(title)") }
            if let trackID = session.document.music?.trackID { return String(localized: "Music: \(trackID)") }
            return laneTitle
        case .soundEffect:
            let effect = session.document.soundEffects.first { $0.id == item.id }
            return effect.map { String(localized: "SFX: \(nativeEffectName($0.raw, fallback: $0.kind ?? String(localized: "Sound effect")))") } ?? laneTitle
        case .mediaOverlay:
            let overlay = session.document.mediaOverlays.first { $0.id == item.id }
            return overlay.map { String(localized: "Media: \(nativeEffectName($0.raw, fallback: $0.kind ?? String(localized: "Overlay")))") } ?? laneTitle
        case .visualBlock:
            let block = session.document.visualBlocks.first { $0.id == item.id }
            return block.map { String(localized: "Visual: \($0.kind)") } ?? laneTitle
        case .motionScene:
            let scene = session.document.motionScenes.first { $0.id == item.id }
            return scene.map { String(localized: "Motion: \($0.preset ?? String(localized: "Scene"))") } ?? laneTitle
        case .cameraEffect:
            let effect = session.document.cameraEffects.first { $0.id == item.id }
            return effect.map { String(localized: "Camera: \(CameraEmphasis.label($0.raw["easing"]?.stringValue))") } ?? laneTitle
        case .drawing:
            let count = session.document.drawings.first { $0.id == item.id }?.strokes.count ?? 0
            return count > 0 ? String(localized: "Drawing · \(count) strokes") : laneTitle
        case .speakerBackgroundWindow:
            guard let window = session.speakerBackgroundWindows.first(where: { $0.localID == item.id }) else { return laneTitle }
            return session.visualLibrary.first { $0.id == window.mediaID }?.sourceFilename ?? (window.kind == .video ? String(localized: "Video") : String(localized: "Photo"))
        case .carousel:
            return String(localized: "Carousel: \(item.id)")
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
        case .drawing:
            return ["drawings.trim", "drawings.edit"]
        case .speakerBackgroundWindow:
            return ["speaker_background_windows"]
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
        case .drawing: return session.canEditDrawings
        case .speakerBackgroundWindow: return session.canEditSpeakerBackgroundWindows
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

    private func timedLane(title: String, items: [NativeEditorTimelineItem], color: Color, playheadX: CGFloat, viewportGlobalRange: ClosedRange<CGFloat>) -> some View {
        let packed = title == "TEXT" ? NativeEditorInteraction.packLanes(items) : []
        let lanes = Dictionary(uniqueKeysWithValues: packed.map { ($0.item.selection, $0.lane) })
        let count = max(1, (packed.map(\.lane).max() ?? 0) + 1)
        return ZStack(alignment: .topLeading) {
            ForEach(visibleItems(items, playheadX: playheadX), id: \.selection) { item in
                let itemFrame = itemFrame(item, playheadX: playheadX).offsetBy(dx: 0, dy: CGFloat(lanes[item.selection] ?? 0) * (secondaryLaneHeight + rowGap))
                // Shared by the direct finger path and the auto-scroll tick
                // path so both apply the exact same baseline math and
                // haptics. Returns a marker (the moved/trimmed edge) so
                // auto-scroll can detect a pin (the value stopped changing
                // despite the clock advancing) and stop drifting the handle
                // away from the finger.
                let applyMove: (TimeInterval) -> TimeInterval? = { raw in
                    session.updateTimedBodyMove(by: raw)
                    guard let moved = session.timelineItems.first(where: { $0.selection == item.selection }) else { return nil }
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
                    return moved.start
                }
                let applyTrim: (TimeInterval, NativeTimelineGestureID) -> TimeInterval? = { raw, token in
                    guard session.ownsTimingGesture(token) else { return nil }
                    session.updateTimedEdgeTrim(by: raw, gestureID: token)
                    guard let trimmed = session.timelineItems.first(where: { $0.selection == item.selection }) else { return nil }
                    updateTrimHaptics(start: trimmed.start, end: trimmed.end)
                    return trimEdge == .leading ? trimmed.start : trimmed.end
                }
                NativeTimelineBar(
                    item: item,
                    frame: itemFrame,
                    name: itemName(item, laneTitle: title),
                    color: color,
                    isSelected: session.selection == item.selection,
                    canMove: !isPinching && panStartTime == nil && canEditTimedOperation(item, operation: "timing"),
                    canTrim: !isPinching && canEditTimedOperation(item, operation: "trim"),
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
                        beginAutoScroll(viewportGlobalRange: viewportGlobalRange, applyRaw: applyMove)
                    },
                    onMoveChange: { raw in applyCompensatedDrag(raw: raw, applyRaw: applyMove) },
                    onMoveEnd: {
                        lastPanAt = .now
                        moveAlignment = nil
                        movePreviousEdges = nil
                        session.endTimedBodyMove()
                        endAutoScroll()
                        refreshItems()
                    },
                    onTrimStart: { edge, token in
                        guard session.beginTimedEdgeTrim(kind: item.kind, id: item.id, edge: edge, gestureID: token) else { return false }
                        beginTrimHaptics(selection: item.selection, edge: edge, start: item.start, end: item.end)
                        beginAutoScroll(viewportGlobalRange: viewportGlobalRange, applyRaw: { applyTrim($0, token) })
                        return true
                    },
                    onTrimChange: { raw, token in
                        guard session.ownsTimingGesture(token) else { return }
                        applyCompensatedDrag(raw: raw, applyRaw: { applyTrim($0, token) })
                    },
                    onTrimEnd: { token, commit in
                        guard session.ownsTimingGesture(token) else { return }
                        if commit { session.endTimedEdgeTrim(gestureID: token) } else { session.cancelTimingGesture(gestureID: token) }
                        endTrimHaptics(); endAutoScroll()
                    },
                    onDragXChange: { x in updateAutoScrollX(x) }
                )
            }
            if title == "BACKGROUND" { backgroundFillStrip(playheadX: playheadX) }
        }
        .frame(height: CGFloat(count) * secondaryLaneHeight + CGFloat(count - 1) * rowGap)
        .clipped()
        .accessibilityElement(children: .contain)
        .accessibilityLabel(String(localized: "\(Self.laneDisplayName(title)) lane"))
        .accessibilityIdentifier("native-editor-lane-\(title.lowercased())")
    }

    /// KRI-1162: the whole-edit fill, drawn faintly along the top of the BACKGROUND lane. A tap opens the Background
    /// tool; it has no edges to trim (the fill always spans the video).
    private func backgroundFillStrip(playheadX: CGFloat) -> some View {
        let origin = playheadX - CGFloat(clock.currentTime) * pixelsPerSecond
        let left = max(origin, -12)
        let right = min(origin + CGFloat(timelineDuration) * pixelsPerSecond, max(viewportWidth, 1) + 12)
        let width = max(1, right - left)
        return Button { session.requestBackgroundTool() } label: {
            RoundedRectangle(cornerRadius: 3, style: .continuous)
                .fill(KriaColor.mutedInk.opacity(0.22))
                .frame(width: width, height: 6)
                .frame(width: width, height: 14, alignment: .top)
                .contentShape(Rectangle())
        }
        .buttonStyle(.plain)
        .frame(width: width, height: 14)
        .position(x: left + width / 2, y: 7)
        .accessibilityLabel(String(localized: "Background fill"))
        .accessibilityHint("Opens the Background tool")
        .accessibilityIdentifier("native-editor-timeline-background-fill")
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
            Text(String(localized: "Captions • \(captionItems.count) cues"))
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
        .accessibilityValue(String(localized: "\(captionItems.count) cues; select to expand"))
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

    private func gapFrame(_ gap: NativeSplitGap, playheadX: CGFloat) -> CGRect {
        CGRect(x: playheadX + CGFloat(gap.start - clock.currentTime) * pixelsPerSecond,
               y: CGFloat(gap.row) * (filmstripHeight + rowGap),
               width: CGFloat(gap.end - gap.start) * pixelsPerSecond, height: filmstripHeight)
    }

    private func visibleSplitGaps(playheadX: CGFloat) -> [NativeSplitGap] {
        guard viewportWidth > 0 else { return splitGaps }
        return splitGaps.filter { gap in
            let frame = gapFrame(gap, playheadX: playheadX)
            return frame.maxX >= 0 && frame.minX <= viewportWidth && frame.width > 0
        }
    }

    private func gapLabel(_ gap: NativeSplitGap) -> String {
        let count = session.document.splitScreenSections.first { $0.id == gap.sectionID }?.panelCount ?? 2
        let title = NativeSplitScreenRows.panelTitle(index: gap.panel, count: count)
        let length = gap.end - gap.start
        return String(localized: "Free time in the \(NativeSplitScreenRows.displayTitle(title)) panel, \(length, format: .number.precision(.fractionLength(1))) seconds")
    }

    /// Moves the playhead into the free time so the Split tool's add flow acts on this panel, then asks the owner to
    /// open that tool. A gap inside a split cannot be selected as a clip, so the playhead is the only handle.
    private func openSplitGap(_ gap: NativeSplitGap) {
        guard gap.canAdd else { return }
        session.pausePlayback()
        seek(to: gap.start + min(0.01, (gap.end - gap.start) / 2))
        onOpenSplitAdd?(gap.sectionID, gap.panel)
    }

    private func visibleClips(playheadX: CGFloat) -> [(offset: Int, element: EditorClip)] {
        let indexed = Array(clips.enumerated())
        guard viewportWidth > 0 else { return indexed }
        return indexed.filter { _, clip in
            let frame = clipFrame(clip, playheadX: playheadX)
            return frame.maxX >= 0 && frame.minX <= viewportWidth || session.selectedClipID == clip.id
        }
    }

    private func clipFrame(_ clip: EditorClip, playheadX: CGFloat) -> CGRect {
        NativeClipBlockGeometry.frame(
            for: clip, playheadX: playheadX, currentTime: clock.currentTime,
            pixelsPerSecond: pixelsPerSecond, height: filmstripHeight
        )
        .offsetBy(dx: 0, dy: CGFloat(footageRows.row(slotID: clip.slotID)) * (filmstripHeight + rowGap))
    }

    /// What the filmstrip draws: the committed frame, or — while a clip is lifted — the lifted block under the
    /// finger and every other block where it will sit after the drop.
    private func displayClipFrame(_ clip: EditorClip, index: Int, playheadX: CGFloat) -> CGRect {
        let base = clipFrame(clip, playheadX: playheadX)
        guard let drag = clipReorder,
              let unit = drag.units.firstIndex(where: { $0.clipIDs.contains(clip.id) }) else { return base }
        if unit == drag.from { return base.offsetBy(dx: CGFloat(drag.travel) * pixelsPerSecond, dy: 0) }
        let destination = drag.previewPosition(of: unit)
        let displacement = drag.preview[destination].start - drag.windows[unit].start
        return base.offsetBy(dx: CGFloat(displacement) * pixelsPerSecond, dy: 0)
    }

    // MARK: - Clip reorder (long press, then drag)

    /// Keep the lifted block inside the video, but choose its destination from the requested centre.
    /// The scroll pin follows that centre to the timeline edge so unequal widths cannot stop it early.
    private func applyClipReorder(raw: TimeInterval) -> TimeInterval? {
        guard var drag = clipReorder, drag.windows.indices.contains(drag.from) else { return nil }
        let origin = drag.windows[drag.from]
        let lower = (drag.windows.first?.start ?? origin.start) - origin.start
        let upper = (drag.windows.last?.end ?? origin.end) - origin.end
        drag.travel = min(max(raw, lower), max(lower, upper))
        let requestedCenter = (origin.start + origin.end) / 2 + raw
        let target = NativeEditorInteraction.clipReorderTarget(windows: drag.windows, from: drag.from, liftedCenter: requestedCenter)
        if target != drag.target {
            drag.target = target
            if !session.document.splitScreenSections.isEmpty, let targetID = drag.units[target].clipIDs.first {
                let preview = NativeSplitScreenReorderUnit.reorderedWindows(document: session.document, clips: clips,
                                                                            moving: drag.clipID, to: targetID)
                guard preview.count == drag.units.count else { return nil }
                drag.preview = preview
            } else {
                drag.preview = NativeEditorInteraction.reorderedClipWindows(windows: drag.windows, from: drag.from, to: target)
            }
            reorderTickHaptic.selectionChanged()
        }
        clipReorder = drag
        return min(max(requestedCenter, drag.windows.first?.start ?? origin.start), drag.windows.last?.end ?? origin.end)
    }

    private func beginClipReorder(_ clip: EditorClip, index: Int, viewportGlobalRange: ClosedRange<CGFloat>) {
        guard session.canReorderClips, clipReorder == nil, !session.isTimingGestureActive, !isPinching,
              panStartTime == nil, clips.indices.contains(index) else { return }
        session.pausePlayback()
        session.selectClip(clip.id)
        let units = NativeSplitScreenReorderUnit.make(document: session.document, clips: clips)
        guard let from = units.firstIndex(where: { $0.clipIDs.contains(clip.id) }) else { return }
        let windows = units.map { (start: $0.start, end: $0.end) }
        clipReorder = ClipReorderDrag(clipID: clip.id, units: units, from: from, travel: 0, target: from, windows: windows, preview: windows)
        reorderHaptic.impactOccurred()
        beginAutoScroll(viewportGlobalRange: viewportGlobalRange, applyRaw: { applyClipReorder(raw: $0) })
    }

    private func updateClipReorder(travel: TimeInterval, globalX: CGFloat) {
        guard clipReorder != nil else { return }
        applyCompensatedDrag(raw: travel, applyRaw: { applyClipReorder(raw: $0) })
        updateAutoScrollX(globalX)
    }

    /// Drop: one `moveClip` (one undo step) when the block landed somewhere new. State is cleared first so the
    /// document change the commit causes is not mistaken for an outside edit.
    private func endClipReorder(commit: Bool) {
        guard let drag = clipReorder else { return }
        clipReorder = nil
        endAutoScroll()
        guard commit, drag.target != drag.from else { return }
        guard let targetID = drag.units[drag.target].clipIDs.first,
              let target = clips.firstIndex(where: { $0.id == targetID }) else { return }
        session.moveClip(id: drag.clipID, toIndex: target)
        reorderDropHaptic.impactOccurred()
        refreshItems()
    }

    private func cancelClipReorder() {
        guard clipReorder != nil else { return }
        clipReorder = nil
        endAutoScroll()
    }

    private var timelinePanGesture: some Gesture {
        DragGesture(minimumDistance: 10, coordinateSpace: .global)
            .updating($panActive) { _, active, _ in active = true }
            .onChanged { value in
                guard abs(value.translation.width) > abs(value.translation.height),
                      !session.isTimingGestureActive, clipReorder == nil, !isPinching, playheadStartTime == nil else { return }
                if panStartTime == nil { stopCoast() }
                let start = panStartTime ?? clock.currentTime
                panStartTime = start
                lastPanAt = .now
                seek(to: start - Double(value.translation.width / pixelsPerSecond))
            }
            .onEnded { value in
                let wasPanning = panStartTime != nil
                if wasPanning { lastPanAt = .now }
                panStartTime = nil
                if wasPanning { startCoast(pointsPerSecond: value.velocity.width) }
            }
    }

    /// Dragging left moves later in the video, so the glide runs opposite to the finger.
    private static let coastMinimumSpeed: CGFloat = 200
    /// Per-millisecond velocity retention: the same deceleration UIScrollView uses.
    private static let coastRetention = 0.998

    private func startCoast(pointsPerSecond: CGFloat) {
        guard !reduceMotion, pointsPerSecond.isFinite, abs(pointsPerSecond) >= Self.coastMinimumSpeed else { return }
        let scale = max(1, Double(pixelsPerSecond))
        var velocity = -Double(pointsPerSecond) / scale
        stopCoast()
        coastTask = Task { @MainActor in
            var last = ContinuousClock.now
            while !Task.isCancelled, abs(velocity) * scale > 20 {
                try? await Task.sleep(for: .milliseconds(16))
                guard !Task.isCancelled else { return }
                let now = ContinuousClock.now
                let elapsed = (now - last) / .seconds(1)
                last = now
                let before = clock.currentTime
                seek(to: before + velocity * elapsed)
                lastPanAt = .now
                velocity *= pow(Self.coastRetention, elapsed * 1000)
                if clock.currentTime == before { return }
            }
        }
    }

    private func stopCoast() {
        coastTask?.cancel()
        coastTask = nil
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

    // MARK: - Timeline edge auto-scroll

    /// Starts tracking one auto-scroll-eligible drag. `applyRaw` is the same
    /// function the finger path uses (via `applyCompensatedDrag`) — the tick
    /// path and the finger path always apply the exact same math and haptics.
    private func beginAutoScroll(viewportGlobalRange: ClosedRange<CGFloat>, applyRaw: @escaping (TimeInterval) -> TimeInterval?) {
        // The session only ever runs one timing-gesture transaction at a
        // time (activeTimedEdit/activeTrim each guard on being nil), but
        // the view layer's begin callbacks fire before that guard is
        // visible here. A second touch reaching this while a first gesture
        // legitimately owns auto-scroll must not steal the shared context —
        // that would corrupt the first gesture's clockAtStart compensation,
        // or have the second touch's (session-layer no-op) end silently
        // kill the first gesture's still-active auto-scroll.
        guard autoScroll == nil else { return }
        let context = DragAutoScrollContext(
            clockAtStart: clock.currentTime,
            lastX: (viewportGlobalRange.lowerBound + viewportGlobalRange.upperBound) / 2,
            viewportGlobalRange: viewportGlobalRange,
            applyRaw: applyRaw
        )
        autoScroll = context
        edgeScroller.onTick = { [weak context] elapsed in
            guard let context else { return }
            tickAutoScroll(context: context, elapsed: elapsed)
        }
    }

    /// Applies one finger sample. When no auto-scroll context exists (e.g. a
    /// leading clip trim, which is excluded — see the design note above),
    /// `raw` is applied unmodified: identical to the pre-auto-scroll behavior.
    private func applyCompensatedDrag(raw: TimeInterval, applyRaw: (TimeInterval) -> TimeInterval?) {
        guard let context = autoScroll else { _ = applyRaw(raw); return }
        context.lastRaw = raw
        let effective = raw + (clock.currentTime - context.clockAtStart)
        if let marker = applyRaw(effective) {
            context.lastAppliedMarker = marker
        }
    }

    /// Reports the finger's latest global x and starts/stops the display
    /// link based on whether it's currently inside an edge zone.
    private func updateAutoScrollX(_ x: CGFloat) {
        guard let context = autoScroll else { return }
        context.lastX = x
        let velocity = NativeEditorInteraction.edgeAutoScrollVelocity(
            x: x, viewport: context.viewportGlobalRange, edgeZone: 56, maxSpeed: max(1, viewportWidth) * 1.2)
        if velocity == 0 {
            edgeScroller.stop()
        } else if !edgeScroller.isRunning {
            edgeScroller.start()
        }
    }

    private func endAutoScroll() {
        edgeScroller.stop()
        edgeScroller.onTick = nil
        autoScroll = nil
    }

    private func tickAutoScroll(context: DragAutoScrollContext, elapsed: TimeInterval) {
        let velocity = NativeEditorInteraction.edgeAutoScrollVelocity(
            x: context.lastX, viewport: context.viewportGlobalRange, edgeZone: 56, maxSpeed: max(1, viewportWidth) * 1.2)
        guard velocity != 0 else { edgeScroller.stop(); return }
        let step = NativeEditorInteraction.edgeAutoScrollStep(velocity: velocity, elapsed: elapsed, pixelsPerSecond: pixelsPerSecond)
        guard step != 0 else { return }
        let clockBefore = clock.currentTime
        autoScrollSeek(clockBefore + step)
        let effective = context.lastRaw + (clock.currentTime - context.clockAtStart)
        let marker = context.applyRaw(effective)
        guard let marker else {
            // The item this gesture was editing is gone — e.g. an external-
            // keyboard undo raced the drag and removed it from the document.
            // There is nothing left to compensate for; without this, the
            // scroller would keep calling applyRaw every tick, always
            // getting nil back, and just keep seeking the clock forever
            // with no finger attached to anything. Revert this tick's
            // speculative advance and stop outright.
            autoScrollSeek(clockBefore)
            endAutoScroll()
            return
        }
        if let previous = context.lastAppliedMarker, abs(marker - previous) < 0.0005 {
            // Pinned at a clamp: the value didn't move despite the clock
            // advancing. Revert the clock so it (and the handle's on-screen
            // position, which is computed from clock.currentTime) doesn't
            // keep drifting away from the finger.
            autoScrollSeek(clockBefore)
        } else {
            context.lastAppliedMarker = marker
        }
    }

    /// Widened-bound seek used only by auto-scroll ticks — skips the
    /// boundary-crossing haptic (that's for a deliberate finger pan, not a
    /// mechanical per-frame advance).
    private func autoScrollSeek(_ value: TimeInterval) {
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
    let onTrimStart: (NativeTrimEdge, NativeTimelineGestureID) -> Bool
    let onTrimChange: (TimeInterval, NativeTimelineGestureID) -> Void
    let onTrimEnd: (NativeTimelineGestureID, Bool) -> Void
    var onDragXChange: ((CGFloat) -> Void)? = nil
    @State private var isMoving = false
    /// Global x of the finger when the long press was recognized. The move is
    /// measured from here, not from the sequenced drag's `translation`: SwiftUI
    /// starts that drag at the first touch sample it handles after the press
    /// is recognized, so `translation` drops the travel before that sample.
    /// Selecting the block at recognition keeps the main thread busy for a
    /// moment, and a quick drag can arrive as one coalesced sample and lose
    /// all of its travel.
    @State private var moveAnchorX: CGFloat?
    /// The finger's global x at touch-down and at its latest sample, seen from
    /// the start of the touch so `moveAnchorX` is known at recognition.
    @GestureState private var touchX: (down: CGFloat, latest: CGFloat)?
    @GestureState private var moveGestureActive = false
    @Environment(\.accessibilityReduceMotion) private var reduceMotion

    private var timing: String {
        String(localized: "\(nativeTimecode(item.start)) to \(nativeTimecode(item.end))")
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
            .accessibilityValue(isSelected ? String(localized: "\(timing), selected") : timing)
            .accessibilityIdentifier("native-editor-timeline-\(item.kind.rawValue)-\(item.id)")
            .accessibilityAddTraits(isSelected ? AccessibilityTraits.isSelected : [])
            .accessibilityAction(named: String(localized: "Select \(name.lowercased())")) { onSelect() }
            .accessibilityAction(named: String(localized: "Move \(name.lowercased()) earlier")) {
                guard canMove else { return }
                onMoveStart(); onMoveChange(-1); onMoveEnd()
            }
            .accessibilityAction(named: String(localized: "Move \(name.lowercased()) later")) {
                guard canMove else { return }
                onMoveStart(); onMoveChange(1); onMoveEnd()
            }
            .simultaneousGesture(
                DragGesture(minimumDistance: 0, coordinateSpace: .global)
                    .updating($touchX) { value, touch, _ in touch = (value.startLocation.x, value.location.x) }
            )
            .simultaneousGesture(
                LongPressGesture(minimumDuration: 0.45, maximumDistance: 8)
                    .sequenced(before: DragGesture(minimumDistance: 0, coordinateSpace: .global))
                    .updating($moveGestureActive) { _, active, _ in active = true }
                    .onChanged { value in
                        guard canMove, case .second(true, let drag) = value else { return }
                        if !isMoving {
                            isMoving = true
                            // A recognition that arrives with a drag sample may
                            // already count it in `latest`; touch-down can't.
                            moveAnchorX = drag == nil ? touchX?.latest : touchX?.down
                            onMoveStart()
                        }
                        if let drag {
                            let travel = drag.location.x - (moveAnchorX ?? drag.startLocation.x)
                            onMoveChange(TimeInterval(travel / max(1, pixelsPerSecond)))
                            onDragXChange?(drag.location.x)
                        }
                    }
                    .onEnded { _ in
                        if isMoving { onMoveEnd() }
                        isMoving = false
                        moveAnchorX = nil
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
                    onTrimChange: { _, seconds, token in onTrimChange(seconds, token) },
                    onTrimEnd: onTrimEnd,
                    onDragXChange: onDragXChange
                )
                .offset(x: -(hitWidth - min(44, hitWidth / 3)) / 2)
                NativeTrimHandle(
                    edge: .trailing,
                    height: frame.height,
                    pixelsPerSecond: pixelsPerSecond,
                    visualOffset: min(44, hitWidth / 3) / 2 - 6,
                    touchWidth: min(44, hitWidth / 3),
                    onTrimStart: onTrimStart,
                    onTrimChange: { _, seconds, token in onTrimChange(seconds, token) },
                    onTrimEnd: onTrimEnd,
                    onDragXChange: onDragXChange
                )
                .offset(x: (hitWidth - min(44, hitWidth / 3)) / 2)
            }
        }
        .frame(width: hitWidth, height: 44)
        .scaleEffect(isMoving && !reduceMotion ? 1.04 : 1)
        .offset(y: isMoving && !reduceMotion ? -5 : 0)
        .shadow(color: .black.opacity(isMoving ? 0.22 : 0), radius: isMoving ? 5 : 0, y: 3)
        .animation(reduceMotion ? nil : .spring(response: 0.24, dampingFraction: 0.86), value: isMoving)
        .offset(y: frame.midY)
        .animation(reduceMotion ? nil : .spring(response: 0.28, dampingFraction: 0.86), value: frame.midY)
        .position(x: frame.midX, y: 0)
        .zIndex(isMoving ? 2 : isSelected ? 1 : 0)
        .onChange(of: moveGestureActive) { _, active in
            if !active && isMoving {
                isMoving = false
                moveAnchorX = nil
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
    let isLifted: Bool
    let canReorder: Bool
    let pixelsPerSecond: CGFloat
    let onSelect: () -> Void
    /// Long press, then drag, moves the block to a new position. `onReorderChange` reports the finger's travel in
    /// seconds and its global x (for edge auto-scroll); `onReorderEnd(false)` is a cancelled gesture.
    let onReorderStart: () -> Void
    let onReorderChange: (TimeInterval, CGFloat) -> Void
    let onReorderEnd: (Bool) -> Void
    let onTrimStart: (NativeTrimEdge, NativeTimelineGestureID) -> Bool
    let onTrimChange: (NativeTrimEdge, TimeInterval, NativeTimelineGestureID) -> Void
    let onTrimEnd: (NativeTimelineGestureID, Bool) -> Void
    let onMove: (TimeInterval) -> Void
    /// Only the trailing handle forwards drag position — a clip's timeline
    /// `start` is fixed by slot order (reflowSlots is a no-op), so a leading
    /// trim never moves the handle on screen; feeding auto-scroll's clock
    /// delta back into that translation would un-trim the clip with no
    /// finger movement. See NativeMiniStrip's design note.
    var onTrailingDragXChange: ((CGFloat) -> Void)? = nil
    var panelTitle: String? = nil
    /// A split member always carries its own end handle, so any panel can be shortened or extended without selecting
    /// it first. Ordinary clips show handles only while selected.
    var showsEndHandle = false

    private let minimumDuration: TimeInterval = 0.1

    private var duration: TimeInterval {
        NativeClipBlockGeometry.windowDuration(clip, minimum: minimumDuration)
    }

    private var accessibilityPrefix: String {
        guard let panelTitle, panelTitle != "Video" else { return "" }
        return String(localized: "\(NativeSplitScreenRows.displayTitle(panelTitle)) panel, ")
    }

    // Same press-then-drag recipe as NativeTimelineBar: the travel is measured from the finger's global x at the
    // moment the press is recognised (a sequenced drag's own `translation` loses the travel before its first
    // sample), and @GestureState is the only reliable cancel signal.
    @State private var isReordering = false
    @State private var reorderAnchorX: CGFloat?
    @GestureState private var touchX: (down: CGFloat, latest: CGFloat)?
    @GestureState private var reorderGestureActive = false
    @Environment(\.accessibilityReduceMotion) private var reduceMotion

    var body: some View {
        ZStack(alignment: .leading) {
            RoundedRectangle(cornerRadius: 8, style: .continuous)
                .fill(isSelected ? KriaColor.sky.opacity(0.34) : .clear)
                .contentShape(Rectangle())
                .onTapGesture(perform: onSelect)
                .simultaneousGesture(
                    DragGesture(minimumDistance: 0, coordinateSpace: .global)
                        .updating($touchX) { value, touch, _ in touch = (value.startLocation.x, value.location.x) }
                )
                .simultaneousGesture(
                    LongPressGesture(minimumDuration: 0.45, maximumDistance: 8)
                        .sequenced(before: DragGesture(minimumDistance: 0, coordinateSpace: .global))
                        .updating($reorderGestureActive) { _, active, _ in active = true }
                        .onChanged { value in
                            guard canReorder, case .second(true, let drag) = value else { return }
                            if !isReordering {
                                isReordering = true
                                // A recognition that arrives with a drag sample may already count it in `latest`;
                                // touch-down can't.
                                reorderAnchorX = drag == nil ? touchX?.latest : touchX?.down
                                onReorderStart()
                            }
                            if let drag {
                                let travel = drag.location.x - (reorderAnchorX ?? drag.startLocation.x)
                                onReorderChange(TimeInterval(travel / max(1, pixelsPerSecond)), drag.location.x)
                            }
                        }
                        .onEnded { _ in
                            let wasReordering = isReordering
                            isReordering = false
                            reorderAnchorX = nil
                            if wasReordering { onReorderEnd(true) }
                        }
                )
                .accessibilityElement(children: .ignore)
                .accessibilityLabel("\(accessibilityPrefix)Clip \(index + 1), \(duration, specifier: "%.1f") seconds")
                .accessibilityValue(String(format: "%.3f", duration))
                .accessibilityIdentifier("native-editor-clip-\(index + 1)")
                .accessibilityAddTraits(isSelected ? AccessibilityTraits.isSelected : [])
                .accessibilityAction(named: "Select clip") { onSelect() }
                .accessibilityAction(named: "Move clip earlier") { onMove(-1) }
                .accessibilityAction(named: "Move clip later") { onMove(1) }

            if isSelected {
                let leading = NativeTrimHandleGeometry.selected(edge: .leading, blockWidth: max(44, frame.width))
                let trailing = NativeTrimHandleGeometry.selected(edge: .trailing, blockWidth: max(44, frame.width))
                NativeTrimHandle(
                    edge: .leading,
                    height: frame.height,
                    pixelsPerSecond: pixelsPerSecond,
                    visualOffset: leading.capsuleOffset,
                    touchWidth: leading.touchWidth,
                    onTrimStart: onTrimStart,
                    onTrimChange: onTrimChange,
                    onTrimEnd: onTrimEnd
                )
                    .offset(x: leading.originX)
                NativeTrimHandle(
                    edge: .trailing,
                    height: frame.height,
                    pixelsPerSecond: pixelsPerSecond,
                    visualOffset: trailing.capsuleOffset,
                    touchWidth: trailing.touchWidth,
                    onTrimStart: onTrimStart,
                    onTrimChange: onTrimChange,
                    onTrimEnd: onTrimEnd,
                    onDragXChange: onTrailingDragXChange
                )
                    .offset(x: trailing.originX)
            } else if showsEndHandle {
                // A narrow block keeps its middle for selecting and reordering it: only this much of the handle's
                // touch surface lies over the block, the rest hangs past its end.
                let end = NativeTrimHandleGeometry.unselectedEnd(blockWidth: max(44, frame.width), insideWidth: min(44, max(14, frame.width * 0.3)))
                NativeTrimHandle(
                    edge: .trailing,
                    height: frame.height,
                    pixelsPerSecond: pixelsPerSecond,
                    visualOffset: end.capsuleOffset,
                    touchWidth: end.touchWidth,
                    tint: .white,
                    identifier: "native-editor-split-end-\(index + 1)",
                    label: String(localized: "Trim panel end"),
                    onTrimStart: onTrimStart,
                    onTrimChange: onTrimChange,
                    onTrimEnd: onTrimEnd,
                    onDragXChange: onTrailingDragXChange
                )
                    .offset(x: end.originX)
            }
        }
        .frame(width: max(44, frame.width), height: frame.height)
        .scaleEffect(isLifted && !reduceMotion ? 1.04 : 1)
        .shadow(color: .black.opacity(isLifted ? 0.22 : 0), radius: isLifted ? 5 : 0, y: 3)
        .animation(reduceMotion ? nil : .spring(response: 0.24, dampingFraction: 0.86), value: isLifted)
        .position(x: frame.midX, y: frame.midY)
        // A selected clip owns both edge handles. Raise the whole surface so
        // the trailing handle at an adjacent clip boundary is not covered by
        // the later clip's hit stack. A lifted block rides above its neighbours.
        .zIndex(isLifted ? 2 : isSelected ? 1 : 0)
        .allowsHitTesting(frame.maxX >= 0)
        .onChange(of: reorderGestureActive) { _, active in
            // A cancelled gesture (system gesture, Control Center) never calls onEnded: drop it uncommitted.
            if !active && isReordering {
                isReordering = false
                reorderAnchorX = nil
                onReorderEnd(false)
            }
        }
        .onDisappear {
            if isReordering { isReordering = false; onReorderEnd(false) }
        }
    }
}

private struct NativeTrimHandle: View {
    let edge: NativeTrimEdge
    let height: CGFloat
    let pixelsPerSecond: CGFloat
    let visualOffset: CGFloat
    var touchWidth: CGFloat = 44
    var tint: Color = KriaColor.sky
    var identifier: String?
    var label: String?
    let onTrimStart: (NativeTrimEdge, NativeTimelineGestureID) -> Bool
    let onTrimChange: (NativeTrimEdge, TimeInterval, NativeTimelineGestureID) -> Void
    let onTrimEnd: (NativeTimelineGestureID, Bool) -> Void
    var onDragXChange: ((CGFloat) -> Void)? = nil
    @State private var gestureID: NativeTimelineGestureID?
    @State private var rejected = false
    // SwiftUI never calls onEnded for a cancelled gesture (a system gesture,
    // Control Center, or the outer vertical ScrollView stealing the touch).
    // @GestureState resets to its initial value on both a normal end AND a
    // cancel, so it's the only reliable cancel signal — same pattern as
    // NativeTimelineBar's moveGestureActive.
    @GestureState private var dragActive = false

    var body: some View {
        ZStack {
            Capsule()
                .fill(tint)
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
                    .updating($dragActive) { _, active, _ in active = true }
                    .onChanged { value in
                        guard !rejected else { return }
                        if gestureID == nil {
                            let token = NativeTimelineGestureID()
                            guard onTrimStart(edge, token) else { rejected = true; return }
                            gestureID = token
                        }
                        guard let token = gestureID else { return }
                        let seconds = TimeInterval(value.translation.width / max(1, pixelsPerSecond))
                        onTrimChange(edge, seconds, token)
                        onDragXChange?(value.location.x)
                    }
                    .onEnded { value in
                        guard let token = gestureID else { rejected = false; return }
                        gestureID = nil
                        let seconds = TimeInterval(value.translation.width / max(1, pixelsPerSecond))
                        onTrimChange(edge, seconds, token)
                        onTrimEnd(token, true)
                        rejected = false
                    }
            )
            .onChange(of: dragActive) { _, active in
                if !active {
                    // Normal completion clears the token in onEnded; system cancellation does not.
                    // Defer to let a normal final sample commit before observing a reset.
                    let token = gestureID
                    Task { @MainActor in
                        await Task.yield()
                        guard !dragActive, gestureID == token else { return }
                        if let token { gestureID = nil; onTrimEnd(token, false) }
                        rejected = false
                    }
                }
            }
            .onDisappear {
                if let token = gestureID { gestureID = nil; onTrimEnd(token, false) }
                rejected = false
            }
            .accessibilityLabel(label ?? (edge == .leading ? String(localized: "Trim block start") : String(localized: "Trim block end")))
            .accessibilityIdentifier(identifier ?? (edge == .leading ? "native-editor-trim-leading" : "native-editor-trim-trailing"))
            .accessibilityHint("Drag to adjust the selected block")
            .accessibilityAdjustableAction { direction in
                let token = NativeTimelineGestureID()
                guard onTrimStart(edge, token) else { return }
                onTrimChange(edge, direction == .increment ? 0.1 : -0.1, token)
                onTrimEnd(token, true)
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
        func detach() {
            if pinch.state == .began || pinch.state == .changed { changed(pinch.scale, true) }
            host?.removeGestureRecognizer(pinch); host = nil
        }
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


/// A clip block is drawn as its TIMELINE window (`start..end`), never its
/// source span (`trimOut - trimIn`). The two differ for retimed clips (short
/// footage slowed to fill a voiceover step, `playback_rate` < 1): the preview
/// plays the clip across the whole window, so a span-wide block left an empty
/// strip before the next one (KRI-281). For rate-1 clips they are equal.
/// Trim handles still edit the slot's source In/duration; only the drawn edge
/// follows the window.
/// Where an end handle's 44 pt touch box and its capsule sit, in block coordinates. A box that does not fit inside
/// the block hangs outward past the edge it trims, so a short block still has two full-size, non-overlapping handles.
struct NativeTrimHandleGeometry: Equatable {
    var touchWidth: CGFloat
    var originX: CGFloat
    /// From the centre of the touch box, so the capsule sits exactly on the block edge.
    var capsuleOffset: CGFloat

    static let target: CGFloat = 44

    static func selected(edge: NativeTrimEdge, blockWidth: CGFloat) -> NativeTrimHandleGeometry {
        let inside = min(target, blockWidth / 2)
        switch edge {
        case .leading:
            return .init(touchWidth: target, originX: inside - target, capsuleOffset: target / 2 - inside)
        case .trailing:
            return unselectedEnd(blockWidth: blockWidth, insideWidth: inside)
        }
    }

    static func unselectedEnd(blockWidth: CGFloat, insideWidth: CGFloat) -> NativeTrimHandleGeometry {
        .init(touchWidth: target, originX: blockWidth - insideWidth, capsuleOffset: insideWidth - target / 2)
    }
}

enum NativeClipBlockGeometry {
    static func windowDuration(_ clip: EditorClip, minimum: TimeInterval = 0.1) -> TimeInterval {
        let window = clip.end - clip.start
        return max(minimum, window.isFinite && window > 0 ? window : minimum)
    }

    static func frame(
        for clip: EditorClip, playheadX: CGFloat, currentTime: TimeInterval,
        pixelsPerSecond: CGFloat, height: CGFloat
    ) -> CGRect {
        let start = playheadX + CGFloat(clip.start - currentTime) * pixelsPerSecond
        let width = CGFloat(windowDuration(clip)) * pixelsPerSecond
        return CGRect(x: start, y: 0, width: max(1, width), height: height)
    }
}
