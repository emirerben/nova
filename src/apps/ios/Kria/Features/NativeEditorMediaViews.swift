import AVFoundation
import AVKit
import KriaMediaEngine
import SwiftUI

/// The video surface for the native editor. The player is intentionally owned
/// by NativeEditorSession: this view is only a playback surface and never
/// invents a local preview when the session has no playable item.
struct NativeVideoPreview: View {
    @ObservedObject var session: NativeEditorSession

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

            if !session.draft.text.isEmpty {
                GeometryReader { proxy in
                    ForEach(session.draft.text) { layer in
                        Text(layer.content)
                            .font(layer.style == "Fraunces" ? KriaFont.display(24) : KriaFont.body(22).weight(.bold))
                            .foregroundStyle(.white)
                            .multilineTextAlignment(.center)
                            .shadow(color: .black.opacity(0.75), radius: 3, y: 1)
                            .frame(maxWidth: proxy.size.width * 0.84)
                            .position(
                                x: min(max(20, layer.position.x * proxy.size.width), proxy.size.width - 20),
                                y: min(max(36, layer.position.y * proxy.size.height), proxy.size.height - 36)
                            )
                            .accessibilityIdentifier("native-editor-preview-text-\(layer.id.uuidString)")
                    }
                }
                .allowsHitTesting(false)
            }

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
    private var hasText: Bool { !session.draft.text.isEmpty }
    private var hasCaptions: Bool { session.draft.captions.enabled }
    private var hasMusic: Bool { session.draft.music != nil }
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
                VStack(spacing: rowGap) {
                    filmstrip(width: width, playheadX: playheadX)
                    if hasText {
                        treatmentLane(title: "Text", color: KriaColor.lime, playheadX: playheadX)
                    }
                    if hasCaptions {
                        treatmentLane(title: "Captions", color: KriaColor.ink, playheadX: playheadX)
                    }
                    if hasMusic {
                        treatmentLane(title: "Music", color: KriaColor.mutedInk, playheadX: playheadX)
                    }
                }
                .clipShape(RoundedRectangle(cornerRadius: 12, style: .continuous))

                NativePlayhead(height: timelineHeight)
                    .position(x: playheadX, y: timelineHeight / 2)
                    .allowsHitTesting(false)
            }
            .contentShape(Rectangle())
            .gesture(scrubGesture(playheadX: playheadX))
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

            ForEach(Array(clips.enumerated()), id: \.element.id) { index, clip in
                NativeClipSurface(
                    clip: clip,
                    index: index,
                    frame: clipFrame(clip, playheadX: playheadX),
                    isSelected: session.selectedClipID == clip.id,
                    pixelsPerSecond: pixelsPerSecond,
                    onSelect: { session.selectClip(clip.id) },
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

    private func treatmentLane(title: String, color: Color, playheadX: CGFloat) -> some View {
        ZStack(alignment: .topLeading) {
            RoundedRectangle(cornerRadius: 9, style: .continuous)
                .fill(KriaColor.softZinc)
            let start = CGFloat(-session.currentTime) * pixelsPerSecond + playheadX
            RoundedRectangle(cornerRadius: 8, style: .continuous)
                .fill(color.opacity(0.78))
                .frame(width: CGFloat(timelineDuration) * pixelsPerSecond, height: secondaryLaneHeight - 8)
                .offset(x: start, y: 4)
                .overlay(alignment: .leading) {
                    if title == "Text", let first = session.draft.text.first {
                        Text(first.content)
                            .font(KriaFont.body(11).weight(.semibold))
                            .foregroundStyle(title == "Text" ? KriaColor.ink : .white)
                            .lineLimit(1)
                            .padding(.horizontal, 10)
                    } else if title == "Music", let music = session.draft.music {
                        Text(music.title)
                            .font(KriaFont.body(11).weight(.semibold))
                            .foregroundStyle(.white)
                            .lineLimit(1)
                            .padding(.horizontal, 10)
                    } else {
                        Text(title)
                            .font(KriaFont.body(11).weight(.semibold))
                            .foregroundStyle(.white)
                            .padding(.horizontal, 10)
                    }
                }
        }
        .frame(height: secondaryLaneHeight)
        .clipped()
        .accessibilityLabel(title)
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
                    onTrimStart: onTrimStart,
                    onTrimChange: onTrimChange,
                    onTrimEnd: onTrimEnd
                )
                    .offset(x: -22)
                NativeTrimHandle(
                    edge: .trailing,
                    height: frame.height,
                    pixelsPerSecond: pixelsPerSecond,
                    onTrimStart: onTrimStart,
                    onTrimChange: onTrimChange,
                    onTrimEnd: onTrimEnd
                )
                    .offset(x: max(0, frame.width - 22))
            }
        }
        .frame(width: max(44, frame.width), height: frame.height)
        .position(x: frame.midX, y: frame.midY)
        .allowsHitTesting(frame.maxX >= 0)
    }
}

private struct NativeTrimHandle: View {
    let edge: NativeTrimEdge
    let height: CGFloat
    let pixelsPerSecond: CGFloat
    let onTrimStart: (NativeTrimEdge) -> Void
    let onTrimChange: (NativeTrimEdge, TimeInterval) -> Void
    let onTrimEnd: () -> Void
    @State private var isDragging = false

    var body: some View {
        Capsule()
            .fill(KriaColor.lime)
            .frame(width: 12, height: min(56, max(44, height - 20)))
            .frame(width: 44, height: max(44, height))
            .contentShape(Rectangle())
            .gesture(
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
