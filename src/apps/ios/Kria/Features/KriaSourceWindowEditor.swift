import AVFoundation
import KriaMediaEngine
import SwiftUI
import UIKit

// KRI-717: the Range picker. Slide a window across the WHOLE original clip and drag its edges to choose which part
// of it a slot plays, like Crop does for the frame. It edits the slot's `in_s` + `duration_s`, which both the phone
// renderer and the server already honour, so there is no new wire key.

// MARK: - Filmstrip loading

/// Thumbnails spread across a whole source file, generated in one batch and cached per file and tile count.
/// Settings follow `KriaCropFrameLoader.videoFrame` (upright frames, loose time tolerance); the bucketing follows
/// the web `Filmstrip.tsx` (one tile per ~56 points, sampled at the middle of each slice).
actor KriaSourceFilmstripLoader {
    static let shared = KriaSourceFilmstripLoader()

    private struct Key: Hashable { let url: URL; let count: Int; let height: Int }
    private var cache: [Key: [UIImage?]] = [:]
    private var order: [Key] = []
    private let capacity = 8

    /// Length of the file in seconds, or nil when it cannot be read.
    func duration(of url: URL) async -> Double? {
        guard let value = try? await AVURLAsset(url: url).load(.duration).seconds, value.isFinite, value > 0 else { return nil }
        return value
    }

    /// One image per sample time; an entry is nil when that frame could not be decoded.
    func frames(url: URL, sourceDuration: Double, count: Int, tileHeight: CGFloat) async -> [UIImage?] {
        let times = KriaSourceWindowGeometry.sampleTimes(sourceDuration: sourceDuration, count: count)
        guard !times.isEmpty else { return [] }
        let key = Key(url: url, count: count, height: Int(tileHeight))
        if let cached = cache[key] { return cached }
        let generator = AVAssetImageGenerator(asset: AVURLAsset(url: url))
        generator.appliesPreferredTrackTransform = true
        generator.maximumSize = CGSize(width: tileHeight * 3, height: tileHeight * 3)
        generator.requestedTimeToleranceBefore = .positiveInfinity
        generator.requestedTimeToleranceAfter = .positiveInfinity
        var result = [UIImage?](repeating: nil, count: times.count)
        for (index, time) in times.enumerated() {
            if Task.isCancelled { return result }
            if let frame = try? await generator.image(at: CMTime(seconds: time, preferredTimescale: 600)).image {
                result[index] = UIImage(cgImage: frame)
            }
        }
        // A cancelled or fully failed run is not worth keeping: the next open should try again.
        if result.contains(where: { $0 != nil }) {
            cache[key] = result
            order.append(key)
            if order.count > capacity { cache[order.removeFirst()] = nil }
        }
        return result
    }
}

// MARK: - Looping preview

/// Plays one source window, muted, on a loop. Seeks use a loose tolerance while a finger is moving the window.
@MainActor
final class KriaSourceWindowPlayer: ObservableObject {
    let player = AVPlayer()
    private var observer: Any?
    private var loadedURL: URL?
    private var start = 0.0
    private var end = 0.0
    private var rate: Float = 1

    init() {
        player.isMuted = true
        player.actionAtItemEnd = .pause
        observer = player.addPeriodicTimeObserver(forInterval: CMTime(seconds: 1.0 / 30, preferredTimescale: 600), queue: .main) { [weak self] time in
            MainActor.assumeIsolated { self?.tick(time.seconds) }
        }
    }

    deinit {
        MainActor.assumeIsolated {
            if let observer { player.removeTimeObserver(observer) }
            player.pause()
        }
    }

    func load(_ url: URL) {
        guard loadedURL != url else { return }
        loadedURL = url
        player.replaceCurrentItem(with: AVPlayerItem(url: url))
    }

    /// Shows the window's first frame and keeps looping it. `scrubbing` seeks fast and approximately.
    func show(start: Double, end: Double, rate: Double, scrubbing: Bool) {
        self.start = start
        self.end = max(end, start + 0.05)
        self.rate = Float(min(max(rate, 0.25), 4))
        seek(to: start, precise: !scrubbing)
        player.playImmediately(atRate: self.rate)
    }

    func pause() { player.pause() }

    private func tick(_ seconds: Double) {
        guard end > start, seconds.isFinite else { return }
        // The window can end past the file (a slot within the server's slack, or slowed narration); the item pauses
        // at its own end, so loop there instead of waiting for a time it never reaches.
        var limit = end
        if let length = player.currentItem?.duration.seconds, length.isFinite, length > 0 { limit = min(limit, length) }
        if seconds >= limit - 0.02 || seconds < start - 0.5 { seek(to: start, precise: true) }
    }

    private func seek(to seconds: Double, precise: Bool) {
        // Never land before the in-point: a loose seek may only overshoot forward.
        let after: CMTime = precise ? .zero : CMTime(seconds: 0.25, preferredTimescale: 600)
        player.seek(to: CMTime(seconds: max(0, seconds), preferredTimescale: 600), toleranceBefore: .zero, toleranceAfter: after)
    }
}

// MARK: - Editor

/// The strip: the whole source as a filmstrip, dimmed outside a bright window with a handle on each edge.
/// Drag the body to slide, drag a handle to change the length. All math lives in `KriaSourceWindowGeometry`.
struct KriaSourceWindowStrip: View {
    typealias Window = KriaSourceWindowGeometry.Window

    let window: Window
    let rate: Double
    let sourceDuration: Double
    let url: URL
    /// Called with the new window on every change; `final` is true once the finger lifts.
    var onChange: (Window, _ final: Bool) -> Void

    static let tileHeight: CGFloat = 64
    private let handleWidth: CGFloat = 44

    @State private var base: Window?
    /// True while a finger is down on any drag. SwiftUI resets it when a gesture is cancelled, which `onEnded` never reports.
    @GestureState private var touching = false
    @State private var frames: [UIImage?] = []

    var body: some View {
        GeometryReader { proxy in
            let width = max(1, proxy.size.width)
            let span = KriaSourceWindowGeometry.stripSpan(window, rate: rate, sourceDuration: sourceDuration, width: width)
            let perPoint = sourceDuration / width
            ZStack(alignment: .topLeading) {
                tiles(width: width)
                // Dim everything outside the window.
                Rectangle().fill(Color.black.opacity(0.6)).frame(width: span.x).allowsHitTesting(false)
                Rectangle().fill(Color.black.opacity(0.6)).frame(width: max(0, width - span.x - span.width))
                    .offset(x: span.x + span.width).allowsHitTesting(false)
                // The window: a bright frame that slides on drag.
                Rectangle().fill(Color.white.opacity(0.001))
                    .frame(width: span.width)
                    .overlay(Rectangle().strokeBorder(KriaColor.butter, lineWidth: 3))
                    .offset(x: span.x)
                    .gesture(drag(perPoint: perPoint) { base, delta in
                        KriaSourceWindowGeometry.slid(base, by: delta, rate: rate, sourceDuration: sourceDuration)
                    })
                    .accessibilityElement()
                    .accessibilityLabel("Source window")
                    .accessibilityValue(KriaSourceWindowGeometry.label(window, rate: rate))
                    .accessibilityIdentifier("kria-source-window-body")
                    .accessibilityAdjustableAction { direction in
                        let step = direction == .increment ? 0.1 : -0.1
                        onChange(KriaSourceWindowGeometry.slid(window, by: step, rate: rate, sourceDuration: sourceDuration), true)
                    }
                handle(systemImage: "chevron.compact.left", x: span.x - handleWidth + 14, id: "kria-source-window-leading",
                       label: "Window start", perPoint: perPoint) { base, delta in
                    KriaSourceWindowGeometry.resizedLeading(base, by: delta, rate: rate)
                }
                handle(systemImage: "chevron.compact.right", x: span.x + span.width - 14, id: "kria-source-window-trailing",
                       label: "Window end", perPoint: perPoint) { base, delta in
                    KriaSourceWindowGeometry.resizedTrailing(base, by: delta, rate: rate, sourceDuration: sourceDuration)
                }
            }
            .frame(width: width, height: Self.tileHeight)
            .clipShape(RoundedRectangle(cornerRadius: 6))
            .onChange(of: touching) { _, isTouching in
                // A cancelled drag (system interruption, sheet pan) never reaches `onEnded`: finish it where it stands.
                if !isTouching, base != nil { base = nil; onChange(window, true) }
            }
            .task(id: "\(url.absoluteString)-\(KriaSourceWindowGeometry.tileCount(width: width))") {
                let count = KriaSourceWindowGeometry.tileCount(width: width)
                let loaded = await KriaSourceFilmstripLoader.shared.frames(url: url, sourceDuration: sourceDuration, count: count, tileHeight: Self.tileHeight)
                // A superseded task returns a partial array; only the current one may store frames.
                guard !Task.isCancelled else { return }
                frames = loaded
            }
        }
        .frame(height: Self.tileHeight)
        .accessibilityElement(children: .contain)
        .accessibilityIdentifier("kria-source-window-strip")
    }

    private func tiles(width: CGFloat) -> some View {
        let count = max(1, KriaSourceWindowGeometry.tileCount(width: width))
        let tile = width / CGFloat(count)
        return HStack(spacing: 0) {
            ForEach(0..<count, id: \.self) { index in
                ZStack {
                    KriaColor.fill
                    // Frames belong to the tile count they were made for; a stale count would sit at the wrong spots.
                    if frames.count == count, let image = frames[index] {
                        Image(uiImage: image).resizable().scaledToFill()
                    }
                }
                .frame(width: tile, height: Self.tileHeight)
                .clipped()
            }
        }
        .frame(width: width, height: Self.tileHeight, alignment: .leading)
        .allowsHitTesting(false)
    }

    private func handle(systemImage: String, x: CGFloat, id: String, label: String, perPoint: Double,
                        apply: @escaping (Window, Double) -> Window) -> some View {
        ZStack {
            Color.clear.contentShape(Rectangle())
            RoundedRectangle(cornerRadius: 3).fill(KriaColor.butter).frame(width: 14, height: Self.tileHeight)
                .overlay(Image(systemName: systemImage).font(.system(size: 12, weight: .bold)).foregroundStyle(KriaColor.ink))
        }
        .frame(width: handleWidth, height: Self.tileHeight)
        .offset(x: x)
        .gesture(drag(perPoint: perPoint, apply: apply))
        .accessibilityLabel(label)
        .accessibilityIdentifier(id)
        .accessibilityAdjustableAction { direction in
            let step = direction == .increment ? 0.1 : -0.1
            onChange(apply(window, step), true)
        }
    }

    private func drag(perPoint: Double, apply: @escaping (Window, Double) -> Window) -> some Gesture {
        DragGesture(minimumDistance: 1)
            .updating($touching) { _, state, _ in state = true }
            .onChanged { value in
                let origin = base ?? window
                base = origin
                onChange(apply(origin, Double(value.translation.width) * perPoint), false)
            }
            .onEnded { value in
                let origin = base ?? window
                base = nil
                onChange(apply(origin, Double(value.translation.width) * perPoint), true)
            }
    }
}

/// The Range picker body: a looping preview of the window above the full-source strip and its label.
struct KriaSourceWindowEditor: View {
    typealias Window = KriaSourceWindowGeometry.Window

    let url: URL
    let rate: Double
    let sourceDuration: Double
    /// The window the session holds (follows undo and AI edits).
    let committed: Window
    /// Commits a window; `final` is true when a gesture ends.
    var onCommit: (Window, _ final: Bool) -> Void
    var onEditingChanged: (Bool) -> Void = { _ in }

    @StateObject private var player = KriaSourceWindowPlayer()
    @State private var working: Window?
    @State private var dragging = false

    private var shown: Window { working ?? committed }

    var body: some View {
        VStack(spacing: 16) {
            SongOrderPlayerSurface(player: player.player)
                .frame(maxWidth: .infinity, maxHeight: .infinity)
                .background(Color.black, in: RoundedRectangle(cornerRadius: 12))
                .clipShape(RoundedRectangle(cornerRadius: 12))
                .accessibilityIdentifier("kria-source-window-preview")
            Text(KriaSourceWindowGeometry.label(shown, rate: rate))
                .font(KriaFont.body(17).monospacedDigit())
                .foregroundStyle(KriaColor.ink)
                .accessibilityIdentifier("kria-source-window-label")
            KriaSourceWindowStrip(window: shown, rate: rate, sourceDuration: sourceDuration, url: url) { next, final in
                if !dragging { dragging = true; onEditingChanged(true) }
                working = next
                player.show(start: next.start, end: KriaSourceWindowGeometry.sourceEnd(next, rate: rate), rate: rate, scrubbing: !final)
                if final {
                    onCommit(next, true)
                    working = nil
                    dragging = false
                    onEditingChanged(false)
                }
            }
            Text("Drag the box to slide it. Drag an edge to make it longer or shorter.")
                .font(KriaFont.body(12)).foregroundStyle(KriaColor.mutedInk).multilineTextAlignment(.center)
        }
        .onAppear {
            player.load(url)
            player.show(start: committed.start, end: KriaSourceWindowGeometry.sourceEnd(committed, rate: rate), rate: rate, scrubbing: false)
        }
        .onDisappear {
            player.pause()
            // Dismissed mid-drag: close the undo transaction the drag opened.
            if dragging { dragging = false; working = nil; onEditingChanged(false) }
        }
        .onChange(of: committed) { _, new in
            // Undo, an AI edit or a reset changed the window under us.
            guard working == nil else { return }
            player.show(start: new.start, end: KriaSourceWindowGeometry.sourceEnd(new, rate: rate), rate: rate, scrubbing: false)
        }
    }
}

// MARK: - Sheet

/// Range for one clip, writing through `NativeEditorSession.setSourceWindow`. `embedded` = already inside the
/// editor inspector's navigation stack (and its Done button).
struct NativeSourceWindowSheet: View {
    @ObservedObject var session: NativeEditorSession
    let selection: EditorSelection
    var embedded = false
    @Environment(\.dismiss) private var dismiss
    @State private var loadedDuration: Double?
    @State private var triedLoading = false

    private var context: NativeEditorSession.SourceWindowContext? { session.sourceWindowContext(for: selection) }

    @ViewBuilder private var content: some View {
        Group {
            if let context, session.sourceWindowBlockedReason(for: selection) == nil {
                if let duration = context.sourceDuration ?? loadedDuration {
                    KriaSourceWindowEditor(
                        url: context.url, rate: context.rate, sourceDuration: duration, committed: context.window,
                        onCommit: { window, _ in
                            session.setSourceWindow(for: selection, window, rate: context.rate, sourceDuration: duration)
                        },
                        onEditingChanged: { editing in
                            if editing { session.beginTransaction() } else { session.endTransaction() }
                        }
                    )
                } else if triedLoading {
                    unavailable
                } else {
                    ProgressView().frame(maxWidth: .infinity, maxHeight: .infinity)
                }
            } else {
                unavailable
            }
        }
        .task(id: context?.url) {
            guard let context, context.sourceDuration == nil else { return }
            loadedDuration = await KriaSourceFilmstripLoader.shared.duration(of: context.url)
            triedLoading = true
        }
        .padding(16)
        .background(KriaColor.paper)
        // `.contain` keeps the sheet id on the container instead of stamping it over every child's own id.
        .accessibilityElement(children: .contain)
        .accessibilityIdentifier("native-editor-range-sheet")
    }

    private var unavailable: some View {
        VStack(spacing: 8) {
            Image(systemName: "film").font(.system(size: 28)).foregroundStyle(KriaColor.zinc)
            Text(session.sourceWindowBlockedReason(for: selection) ?? "Couldn’t load this clip to choose its range.")
                .font(KriaFont.body(14)).foregroundStyle(KriaColor.mutedInk).multilineTextAlignment(.center)
        }
        .frame(maxWidth: .infinity, maxHeight: .infinity)
        .accessibilityIdentifier("kria-source-window-unavailable")
    }

    var body: some View {
        if embedded {
            content
        } else {
            NavigationStack {
                content
                    .navigationTitle("Range")
                    .navigationBarTitleDisplayMode(.inline)
                    .toolbar {
                        ToolbarItem(placement: .confirmationAction) {
                            Button("Done") { dismiss() }.accessibilityIdentifier("native-editor-range-done")
                        }
                    }
            }
            .tint(KriaColor.ink)
            .presentationDetents([.large])
        }
    }
}
