import AVFoundation
import AVKit
import KriaMediaEngine
import SwiftUI

/// The video surface for the native editor. The player is intentionally owned
/// by NativeEditorSession: this view is only a playback surface and never
/// invents a local preview when the session has no playable item.
struct NativeVideoPreview: View {
    @ObservedObject var session: NativeEditorSession
    @State private var cachedObjects: [NativeEditorPreviewObject] = []
    @State private var lastTapIDs: [EditorSelection] = []
    @State private var lastTapPoint: CGPoint?

    init(session: NativeEditorSession) {
        self.session = session
    }

    private var previewKind: String {
        guard let player = session.player, let item = player.currentItem else {
            return ProcessInfo.processInfo.arguments.contains("-ui-testing-editor") ? "Reference frame" : "Preview unavailable"
        }
        guard let asset = item.asset as? AVURLAsset else { return "Cloud preview" }
        return asset.url.isFileURL ? "Local preview" : "Cloud preview"
    }

    private var objects: [NativeEditorPreviewObject] {
        cachedObjects.isEmpty ? makeObjects() : cachedObjects
    }

    private func makeObjects() -> [NativeEditorPreviewObject] {
        let document = session.document
        let textByID = Dictionary(uniqueKeysWithValues: session.draft.text.map { ($0.id.uuidString, $0) })
        return session.timelineItems.compactMap { item in
            switch item.kind {
            case .text:
                let layer = textByID[item.id]
                return NativeEditorPreviewObject(
                    item: item,
                    text: layer?.content ?? document.textElements.first(where: { $0.id == item.id })?.text,
                    position: layer?.position ?? CGPoint(x: 0.5, y: 0.5),
                    style: layer?.style,
                    title: "Text"
                )
            case .captionCue:
                guard session.draft.captions.enabled else { return nil }
                return NativeEditorPreviewObject(
                    item: item,
                    text: document.captionCues.first(where: { $0.id == item.id })?.text,
                    position: nil,
                    style: nil,
                    title: "Caption"
                )
            default:
                return nil
            }
        }
    }

    private func refreshObjects() {
        cachedObjects = makeObjects()
    }

    private func frame(for object: NativeEditorPreviewObject, in size: CGSize) -> CGRect {
        if let position = object.position {
            let width = min(size.width * 0.84, max(44, size.width - 24))
            let height: CGFloat = 54
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
            NativeEditorInteraction.visible(objects.map(\.item), at: session.currentTime)
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
        session.select(next)
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
                    NativeEditorInteraction.visible(objects.map(\.item), at: session.currentTime)
                )
                ForEach(visible.compactMap { item in objects.first(where: { $0.item == item }) }) { object in
                    NativePreviewObjectView(
                        object: object,
                        frame: frame(for: object, in: proxy.size),
                        isSelected: session.selection == object.item.selection,
                        onSelect: { session.select(object.item) }
                    )
                }
                .contentShape(Rectangle())
                .gesture(
                    SpatialTapGesture().onEnded { value in
                        selectPreviewObject(at: value.location, in: proxy.size)
                    }
                )
            }
            .accessibilityElement(children: .contain)

            Text(previewKind)
                .font(KriaFont.body(11).weight(.semibold))
                .foregroundStyle(previewKind == "Preview unavailable" ? .white : KriaColor.ink)
                .padding(.horizontal, 10)
                .frame(minHeight: 30)
                .background(
                    previewKind == "Preview unavailable"
                        ? KriaColor.ink.opacity(0.82)
                        : KriaColor.lime,
                    in: Capsule()
                )
                .padding(12)
                .accessibilityLabel(previewKind)

            if !session.draft.text.isEmpty || session.draft.captions.enabled {
                VStack {
                    Spacer()
                    HStack {
                        Text("Draft layout • final render may differ")
                            .font(KriaFont.body(10).weight(.semibold))
                            .foregroundStyle(.white)
                            .padding(.horizontal, 9)
                            .frame(minHeight: 26)
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
        .onChange(of: session.draft) { _, _ in refreshObjects() }
    }
}

private struct NativeEditorPreviewObject: Identifiable, Equatable {
    let item: NativeEditorTimelineItem
    let text: String?
    let position: CGPoint?
    let style: String?
    let title: String

    var id: String { "\(item.kind.rawValue)-\(item.id)" }
}

private struct NativePreviewObjectView: View {
    let object: NativeEditorPreviewObject
    let frame: CGRect
    let isSelected: Bool
    let onSelect: () -> Void

    var body: some View {
        Group {
            if object.item.kind == .captionCue {
                Text(object.text ?? "Caption")
                    .font(KriaFont.body(15).weight(.semibold))
                    .foregroundStyle(.white)
                    .padding(.horizontal, 10)
                    .frame(maxWidth: .infinity, minHeight: 44)
                    .background(.black.opacity(0.76), in: RoundedRectangle(cornerRadius: 8, style: .continuous))
            } else {
                Text(object.text ?? object.title)
                    .font(object.style == "Fraunces" ? KriaFont.display(24) : KriaFont.body(22).weight(.bold))
                    .foregroundStyle(.white)
                    .multilineTextAlignment(.center)
                    .lineLimit(3)
                    .shadow(color: .black.opacity(0.75), radius: 3, y: 1)
                    .frame(maxWidth: .infinity, minHeight: 44)
            }
        }
        .frame(width: frame.width, height: frame.height)
        .position(x: frame.midX, y: frame.midY)
        .zIndex(Double(object.item.zIndex))
        .overlay {
            if isSelected {
                RoundedRectangle(cornerRadius: 8, style: .continuous)
                    .stroke(KriaColor.lime, lineWidth: 2)
                    .frame(width: frame.width, height: frame.height)
                    .position(x: frame.midX, y: frame.midY)
            }
        }
        .accessibilityElement(children: .ignore)
        .accessibilityLabel("\(object.title): \(object.text ?? "")")
        .accessibilityValue("\(nativeTimecode(object.item.start)) to \(nativeTimecode(object.item.end))\(isSelected ? ", selected" : "")")
        .accessibilityIdentifier("native-editor-preview-\(object.item.kind.rawValue)-\(object.item.id)")
        .accessibilityAddTraits(isSelected ? AccessibilityTraits.isSelected : [])
        .accessibilityAction(named: "Select \(object.title.lowercased())") {
            onSelect()
        }
    }
}

/// Paper's compact, touch-first timeline. The playhead stays in a stable
/// reading position while the timeline moves underneath it as the clock is
/// scrubbed. This means a horizontal finger movement always maps 1:1 to time.
struct NativeMiniStrip: View {
    @ObservedObject var session: NativeEditorSession
    @State private var zoom: CGFloat = 1
    @State private var pinchAnchor: CGFloat = 1
    @State private var viewportWidth: CGFloat = 0
    @State private var scrubStartTime: TimeInterval?
    @State private var cachedItems: [NativeEditorTimelineItem] = []

    private let minimumZoom: CGFloat = 0.5
    private let maximumZoom: CGFloat = 3
    private let basePixelsPerSecond: CGFloat = 72
    private let filmstripHeight: CGFloat = 80
    private let secondaryLaneHeight: CGFloat = 48
    private let rowGap: CGFloat = 4

    init(session: NativeEditorSession) {
        self.session = session
    }

    private var clips: [EditorClip] { session.draft.clips }
    private var timelineItems: [NativeEditorTimelineItem] { cachedItems.isEmpty ? makeItems() : cachedItems }
    private var textItems: [NativeEditorTimelineItem] { timelineItems.filter { $0.kind == .text } }
    private var captionItems: [NativeEditorTimelineItem] { timelineItems.filter { $0.kind == .captionCue } }
    private var musicItems: [NativeEditorTimelineItem] { timelineItems.filter { $0.kind == .music } }
    private var hasText: Bool { !textItems.isEmpty }
    private var hasCaptions: Bool { session.draft.captions.enabled || !captionItems.isEmpty }
    private var hasMusic: Bool { !musicItems.isEmpty || session.draft.music != nil }
    private var captionsExpanded: Bool { session.selection?.kind == .captionCue }
    private var laneCount: Int {
        1 + (hasText ? 1 : 0) + (hasCaptions ? 1 : 0) + (hasMusic ? 1 : 0)
    }
    private var timelineHeight: CGFloat {
        filmstripHeight + CGFloat(laneCount - 1) * secondaryLaneHeight + CGFloat(max(0, laneCount - 1)) * rowGap
    }
    /// The wrapper can use this when it needs an explicit height; leaving the
    /// view unframed is preferred so optional lanes stay visible.
    var preferredHeight: CGFloat { timelineHeight + 88 }
    private var timelineDuration: TimeInterval {
        let clipEnd = clips.map(\.end).max() ?? 0
        return max(0.1, session.duration, clipEnd)
    }
    private var pixelsPerSecond: CGFloat { basePixelsPerSecond * zoom }

    var body: some View {
        VStack(spacing: 10) {
            controls
            HStack(alignment: .top, spacing: 8) {
                laneLabels
                    .frame(width: 52)
                timeline
            }
        }
        .padding(.vertical, 12)
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
            .background(KriaColor.lime, in: Circle())
            .accessibilityLabel(session.isPlaying ? "Pause preview" : "Play preview")
            .accessibilityIdentifier("native-editor-play-pause")

            Text(timecode(session.currentTime))
                .font(.system(size: 13, weight: .medium, design: .monospaced))
                .foregroundStyle(KriaColor.ink)
                .monospacedDigit()
                .lineLimit(1)
                .minimumScaleFactor(0.8)
                .frame(width: 56, alignment: .leading)
                .accessibilityLabel("Current time")
                .accessibilityValue(timecode(session.currentTime))
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
                        timedLane(title: "Text", items: textItems, color: KriaColor.lime, playheadX: playheadX)
                    }
                    if hasCaptions {
                        if captionsExpanded {
                            timedLane(title: "Captions", items: captionItems, color: KriaColor.ink, playheadX: playheadX)
                        } else {
                            captionDensityLane(width: width, playheadX: playheadX)
                        }
                    }
                    if hasMusic {
                        timedLane(title: "Music", items: musicItems, color: KriaColor.mutedInk, playheadX: playheadX)
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
            .accessibilityValue("\(timecode(session.currentTime)) of \(timecode(timelineDuration))")
            .accessibilityAdjustableAction { direction in
                let delta: TimeInterval = direction == .increment ? 1 : -1
                seek(to: session.currentTime + delta)
            }
        }
        .frame(height: timelineHeight)
        .onAppear { refreshItems() }
        .onChange(of: session.draft) { _, _ in refreshItems() }
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

            ForEach(Array(clips.enumerated()), id: \.element.id) { index, clip in
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
        cachedItems = makeItems()
    }

    private func makeItems() -> [NativeEditorTimelineItem] {
        var items = session.timelineItems
        if musicItems(in: items).isEmpty, let music = session.draft.music {
            items.append(
                NativeEditorTimelineItem(
                    selection: EditorSelection(kind: .music, id: music.trackID.uuidString),
                    start: music.start,
                    end: max(session.duration, music.start + max(0.1, session.duration)),
                    zIndex: 50,
                    sourceIndex: 0
                )
            )
        }
        return items
    }

    private func musicItems(in items: [NativeEditorTimelineItem]) -> [NativeEditorTimelineItem] {
        items.filter { $0.kind == .music }
    }

    private func select(_ item: NativeEditorTimelineItem) {
        session.select(item, seekToStart: false)
        session.seek(to: item.start)
    }

    private func itemName(_ item: NativeEditorTimelineItem, laneTitle: String) -> String {
        switch item.kind {
        case .text:
            let content = session.draft.text.first { $0.id.uuidString == item.id }?.content
            return content.map { "Text: \($0)" } ?? laneTitle
        case .captionCue:
            let document = session.document
            let content = document.captionCues.first { $0.id == item.id }?.text
            return content.map { "Caption: \($0)" } ?? laneTitle
        case .music:
            if let title = session.draft.music?.title { return "Music: \(title)" }
            return laneTitle
        default:
            return laneTitle
        }
    }

    private func timedLane(title: String, items: [NativeEditorTimelineItem], color: Color, playheadX: CGFloat) -> some View {
        ZStack(alignment: .topLeading) {
            RoundedRectangle(cornerRadius: 9, style: .continuous)
                .fill(KriaColor.softZinc)
                .allowsHitTesting(false)
            ForEach(items, id: \.selection) { item in
                let itemFrame = itemFrame(item, playheadX: playheadX)
                NativeTimelineBar(
                    item: item,
                    frame: itemFrame,
                    name: itemName(item, laneTitle: title),
                    color: color,
                    isSelected: session.selection == item.selection,
                    onSelect: { select(item) }
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
        let start = playheadX + CGFloat(item.start - session.currentTime) * pixelsPerSecond
        let width = max(1, CGFloat(max(0.05, item.end - item.start)) * pixelsPerSecond)
        return CGRect(x: start, y: 0, width: width, height: secondaryLaneHeight)
    }

    private func clipDuration(_ clip: EditorClip) -> TimeInterval {
        let trimmed = clip.trimOut - clip.trimIn
        let timeline = clip.end - clip.start
        let value = trimmed.isFinite && trimmed > 0 ? trimmed : timeline
        return max(0.1, value.isFinite ? value : 0.1)
    }

    private func clipFrame(_ clip: EditorClip, playheadX: CGFloat) -> CGRect {
        let start = playheadX + CGFloat(clip.start - session.currentTime) * pixelsPerSecond
        let width = CGFloat(clipDuration(clip)) * pixelsPerSecond
        return CGRect(x: start, y: 0, width: max(1, width), height: filmstripHeight)
    }

    private func scrubGesture(playheadX: CGFloat) -> some Gesture {
        DragGesture(minimumDistance: 0)
            .onChanged { value in
                let startTime = scrubStartTime ?? session.currentTime
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
                .foregroundStyle(KriaColor.lime)
                .frame(width: 20, height: 18)
            Rectangle()
                .fill(KriaColor.lime)
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
    let onSelect: () -> Void

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
                        .foregroundStyle(item.kind == .text ? KriaColor.ink : .white)
                }
            }
            .padding(.horizontal, 8)
            .frame(width: max(1, frame.width), height: max(8, frame.height - 8), alignment: .leading)
            .background(color.opacity(isSelected ? 0.98 : 0.78), in: RoundedRectangle(cornerRadius: 8, style: .continuous))
            .overlay {
                RoundedRectangle(cornerRadius: 8, style: .continuous)
                    .stroke(isSelected ? KriaColor.lime : .clear, lineWidth: isSelected ? 2 : 0)
            }
            .offset(x: (hitWidth - frame.width) / 2)

            Button(action: onSelect) {
                Color.clear
                    .contentShape(Rectangle())
            }
            .buttonStyle(.plain)
            .frame(width: hitWidth, height: 44)
        }
        .frame(width: hitWidth, height: 44)
        .position(x: frame.midX, y: frame.midY)
        .accessibilityLabel(name)
        .accessibilityValue("\(timing)\(isSelected ? ", selected" : "")")
        .accessibilityIdentifier("native-editor-timeline-\(item.kind.rawValue)-\(item.id)")
        .accessibilityAddTraits(isSelected ? AccessibilityTraits.isSelected : [])
        .accessibilityAction(named: "Select \(name.lowercased())") { onSelect() }
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
                .fill(isSelected ? KriaColor.lime.opacity(0.24) : .clear)
                .overlay {
                    RoundedRectangle(cornerRadius: 8, style: .continuous)
                        .stroke(isSelected ? KriaColor.lime : .white.opacity(0.15), lineWidth: isSelected ? 2 : 1)
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
                .fill(KriaColor.lime)
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
