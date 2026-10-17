import AVFoundation
import ImageIO
import KriaMediaEngine
import SwiftUI
import UIKit

// MARK: - Geometry

/// Pure crop math for `KriaCropEditor`. Everything is in NORMALIZED coordinates of the UPRIGHT source frame
/// (top-left origin), the same space the server stores (`source_crop`), so the view only maps to points at the edge.
enum KriaCropGeometry {
    /// Smallest side a crop may have, as a fraction of the frame (same floor the old freeform canvas used).
    static let minimumSize = 0.08

    enum Corner: CaseIterable { case topLeading, topTrailing, bottomLeading, bottomTrailing }

    static let full = NormalizedSourceRect(x: 0, y: 0, width: 1, height: 1)

    /// Width / height of a crop, in normalized units, whose PIXEL shape matches `outputAspect` (w / h).
    static func lockedRatio(imageAspect: Double, outputAspect: Double) -> Double {
        guard imageAspect > 0, outputAspect > 0 else { return 1 }
        return outputAspect / imageAspect
    }

    /// The largest centred crop with the output's pixel aspect: exactly what a plain cover-fill shows.
    static func fillCrop(imageAspect: Double, outputAspect: Double) -> NormalizedSourceRect {
        let ratio = lockedRatio(imageAspect: imageAspect, outputAspect: outputAspect)
        var width = 1.0, height = 1.0
        if ratio >= 1 { height = 1 / ratio } else { width = ratio }
        return NormalizedSourceRect(x: (1 - width) / 2, y: (1 - height) / 2, width: width, height: height)
    }

    /// True when `crop` is the centred fill crop (so it can be stored as "no crop").
    static func isCentredFill(_ crop: NormalizedSourceRect, imageAspect: Double, outputAspect: Double, tolerance: Double = 0.002) -> Bool {
        let fill = fillCrop(imageAspect: imageAspect, outputAspect: outputAspect)
        return abs(crop.x - fill.x) <= tolerance && abs(crop.y - fill.y) <= tolerance
            && abs(crop.width - fill.width) <= tolerance && abs(crop.height - fill.height) <= tolerance
    }

    /// True when the crop's pixel shape already matches the output shape.
    static func matchesOutput(_ crop: NormalizedSourceRect, imageAspect: Double, outputAspect: Double, tolerance: Double = 0.02) -> Bool {
        let ratio = lockedRatio(imageAspect: imageAspect, outputAspect: outputAspect)
        return abs(crop.width / crop.height - ratio) <= tolerance * ratio
    }

    static func moved(_ rect: NormalizedSourceRect, dx: Double, dy: Double) -> NormalizedSourceRect {
        NormalizedSourceRect(x: min(max(0, rect.x + dx), 1 - rect.width), y: min(max(0, rect.y + dy), 1 - rect.height),
                             width: rect.width, height: rect.height)
    }

    /// Drags one corner by (dx, dy). With `lockedRatio` the opposite corner stays put and the shape is kept;
    /// without it the corner moves freely. The result always sits inside [0, 1] and is never below `minimumSize`.
    static func resized(_ rect: NormalizedSourceRect, corner: Corner, dx: Double, dy: Double, lockedRatio ratio: Double? = nil) -> NormalizedSourceRect {
        let leading = corner == .topLeading || corner == .bottomLeading
        let top = corner == .topLeading || corner == .topTrailing
        guard let ratio, ratio > 0 else {
            // Only the dragged edges move; each is clamped against the edge that stays put, then the frame.
            var left = rect.x, upper = rect.y, right = rect.x + rect.width, lower = rect.y + rect.height
            if leading { left = min(max(0, left + dx), right - minimumSize) } else { right = max(min(1, right + dx), left + minimumSize) }
            if top { upper = min(max(0, upper + dy), lower - minimumSize) } else { lower = max(min(1, lower + dy), upper + minimumSize) }
            return NormalizedSourceRect(x: left, y: upper, width: right - left, height: lower - upper)
        }
        let anchorX = leading ? rect.x + rect.width : rect.x
        let anchorY = top ? rect.y + rect.height : rect.y
        let pointX = (leading ? rect.x : rect.x + rect.width) + dx
        let pointY = (top ? rect.y : rect.y + rect.height) + dy
        // The pointer asks for a width and, through the shape, a height; follow whichever reaches further.
        // Signed, so dragging a corner past the opposite one collapses to the minimum instead of flipping the crop.
        var width = max(leading ? anchorX - pointX : pointX - anchorX, (top ? anchorY - pointY : pointY - anchorY) * ratio)
        let room = min(leading ? anchorX : 1 - anchorX, (top ? anchorY : 1 - anchorY) * ratio)
        let floor = max(minimumSize, minimumSize * ratio)
        width = min(max(width, floor), max(room, floor))
        if width > room { return rect }   // no room to hold even the smallest shape: leave it
        let height = width / ratio
        return NormalizedSourceRect(x: leading ? anchorX - width : anchorX, y: top ? anchorY - height : anchorY, width: width, height: height)
    }

    /// Pinch: scale about the centre by `factor` (>1 grows the crop), keeping shape, inside the frame.
    static func scaled(_ rect: NormalizedSourceRect, factor: Double) -> NormalizedSourceRect {
        guard factor.isFinite, factor > 0 else { return rect }
        let ceiling = min(1 / rect.width, 1 / rect.height)
        let floor = max(minimumSize / rect.width, minimumSize / rect.height)
        let applied = min(max(factor, floor), max(ceiling, floor))
        let width = rect.width * applied, height = rect.height * applied
        let centreX = rect.x + rect.width / 2, centreY = rect.y + rect.height / 2
        return moved(NormalizedSourceRect(x: centreX - width / 2, y: centreY - height / 2, width: width, height: height), dx: 0, dy: 0)
    }

    /// The crop in view points, given where the image itself is drawn.
    static func viewRect(_ crop: NormalizedSourceRect, in frame: CGRect) -> CGRect {
        CGRect(x: frame.minX + crop.x * frame.width, y: frame.minY + crop.y * frame.height,
               width: crop.width * frame.width, height: crop.height * frame.height)
    }

    /// The largest rect with `aspect` (w / h) that fits in `container`, centred.
    static func fitted(aspect: CGFloat, in container: CGSize) -> CGRect {
        guard aspect > 0, container.width > 0, container.height > 0 else { return .zero }
        var size = CGSize(width: container.width, height: container.width / aspect)
        if size.height > container.height { size = CGSize(width: container.height * aspect, height: container.height) }
        return CGRect(x: (container.width - size.width) / 2, y: (container.height - size.height) / 2, width: size.width, height: size.height)
    }
}

// MARK: - Source window geometry

/// Pure math for the Range picker (KRI-717): which part of a long source clip a slot plays.
///
/// A window is the slot's `in_s` (source seconds) and `duration_s` (OUTPUT seconds). At playback rate `r` it
/// covers `length * r` seconds of source, so the source span is `[start, start + length * r]`. The bound is the
/// server's (`guided_timeline.py`: `in_s >= 0` and `in_s + duration * rate <= source + 0.05`). Every function
/// takes and returns a whole window, so a view can derive each frame of a drag from the window it began with.
enum KriaSourceWindowGeometry {
    /// Shortest a slot may be (the session's `minimumClipDuration` and the server's `TIMELINE_INVALID_DURATION`).
    static let minimumLength = 0.1
    /// Slack the server allows past the end of the source.
    static let boundsTolerance = 0.05

    struct Window: Equatable {
        /// Source seconds where the slot starts playing (`in_s`).
        var start: Double
        /// Output seconds the slot lasts (`duration_s`).
        var length: Double
    }

    private static func safeRate(_ rate: Double) -> Double { rate.isFinite && rate > 0 ? rate : 1 }

    /// Source seconds the window covers.
    static func sourceSpan(_ window: Window, rate: Double) -> Double { window.length * safeRate(rate) }

    /// Source second where the window ends.
    static func sourceEnd(_ window: Window, rate: Double) -> Double { window.start + sourceSpan(window, rate: rate) }

    /// The server's bound check, for callers that want to ask before they write.
    static func isWithinBounds(_ window: Window, rate: Double, sourceDuration: Double?) -> Bool {
        guard window.start >= 0, window.length >= minimumLength - 1e-9 else { return false }
        guard let sourceDuration else { return true }
        return sourceEnd(window, rate: rate) <= sourceDuration + boundsTolerance + 1e-9
    }

    /// Slide: moves the in-point by `delta` source seconds and keeps the length. Clamped to `[0, source - span]`.
    static func slid(_ window: Window, by delta: Double, rate: Double, sourceDuration: Double?) -> Window {
        guard delta.isFinite else { return window }
        var start = max(0, window.start + delta)
        if let sourceDuration {
            // A slot longer than its footage (slowed narration) has no room: it stays at the start.
            start = min(start, max(0, sourceDuration - sourceSpan(window, rate: rate)))
        }
        return Window(start: start, length: window.length)
    }

    /// Left handle: moves the in-point by `delta` source seconds and keeps the source end, so the length changes.
    /// The window never shrinks below `minimumLength` or runs before the start of the source.
    static func resizedLeading(_ window: Window, by delta: Double, rate: Double) -> Window {
        guard delta.isFinite else { return window }
        let rate = safeRate(rate)
        let end = sourceEnd(window, rate: rate)
        let start = min(max(0, window.start + delta), max(0, end - minimumLength * rate))
        return Window(start: start, length: max(minimumLength, (end - start) / rate))
    }

    /// Right handle: moves the source end by `delta` source seconds and keeps the in-point, so the length changes.
    /// The end stops at the end of the source; a slot that already overruns it may still shrink smoothly.
    static func resizedTrailing(_ window: Window, by delta: Double, rate: Double, sourceDuration: Double?) -> Window {
        guard delta.isFinite else { return window }
        let rate = safeRate(rate)
        let end = sourceEnd(window, rate: rate)
        let floor = window.start + minimumLength * rate
        var next = max(floor, end + delta)
        if let sourceDuration { next = min(next, max(floor, sourceDuration, end)) }
        return Window(start: window.start, length: max(minimumLength, (next - window.start) / rate))
    }

    /// Where the window sits on a strip `width` points wide, as `(x, width)` in points.
    static func stripSpan(_ window: Window, rate: Double, sourceDuration: Double, width: Double) -> (x: Double, width: Double) {
        guard sourceDuration > 0, width > 0 else { return (0, 0) }
        let x = min(max(0, window.start / sourceDuration), 1) * width
        let right = min(max(0, sourceEnd(window, rate: rate) / sourceDuration), 1) * width
        return (x, max(0, right - x))
    }

    /// Thumbnail count for a filmstrip `width` points wide (web `filmstripZoomBucket`: one tile per ~56 points).
    static func tileCount(width: Double, tileWidth: Double = 56, maximum: Int = 24) -> Int {
        guard width > 0, maximum > 0 else { return 0 }
        return max(1, min(maximum, Int((width / tileWidth).rounded())))
    }

    /// Source seconds to sample for `count` tiles: the middle of each equal slice of the source.
    static func sampleTimes(sourceDuration: Double, count: Int) -> [Double] {
        guard sourceDuration > 0, count > 0 else { return [] }
        return (0..<count).map { (Double($0) + 0.5) / Double(count) * sourceDuration }
    }

    /// "3.0s – 5.0s · 2.0s": the source range, then how long the slot lasts.
    static func label(_ window: Window, rate: Double) -> String {
        func seconds(_ value: Double) -> String { String(format: "%.1fs", (value * 10).rounded() / 10) }
        return "\(seconds(window.start)) – \(seconds(sourceEnd(window, rate: rate))) · \(seconds(window.length))"
    }
}

// MARK: - Frame loading

/// Loads the real UPRIGHT frame a crop is drawn on: a photo with its EXIF orientation applied, or a video frame
/// with the track's preferred transform applied, so the rectangle the user draws is in the space the server crops in.
enum KriaCropFrameLoader {
    static func image(at url: URL, time: TimeInterval = 0, maxPixel: CGFloat = 2048) async -> UIImage? {
        if NativeDownloadedMedia.isStillImage(url) { return await Task.detached(priority: .userInitiated) { photo(at: url, maxPixel: maxPixel) }.value }
        return await videoFrame(at: url, time: time, maxPixel: maxPixel)
    }

    nonisolated static func photo(at url: URL, maxPixel: CGFloat) -> UIImage? {
        guard let source = CGImageSourceCreateWithURL(url as CFURL, nil) else { return nil }
        let options: [CFString: Any] = [
            kCGImageSourceCreateThumbnailFromImageAlways: true, kCGImageSourceCreateThumbnailWithTransform: true,
            kCGImageSourceShouldCacheImmediately: true, kCGImageSourceThumbnailMaxPixelSize: maxPixel,
        ]
        return CGImageSourceCreateThumbnailAtIndex(source, 0, options as CFDictionary).map { UIImage(cgImage: $0) }
    }

    static func videoFrame(at url: URL, time: TimeInterval, maxPixel: CGFloat) async -> UIImage? {
        let generator = AVAssetImageGenerator(asset: AVURLAsset(url: url))
        generator.appliesPreferredTrackTransform = true
        generator.maximumSize = CGSize(width: maxPixel, height: maxPixel)
        generator.requestedTimeToleranceBefore = .positiveInfinity
        generator.requestedTimeToleranceAfter = .positiveInfinity
        let at = CMTime(seconds: max(0, time), preferredTimescale: 600)
        if let frame = try? await generator.image(at: at).image { return UIImage(cgImage: frame) }
        if time > 0, let frame = try? await generator.image(at: .zero).image { return UIImage(cgImage: frame) }
        return nil
    }
}

// MARK: - View

/// One Crop experience for every media type (KRI-44): the real frame, a dimmed surround, a rule-of-thirds grid, drag to
/// move, pinch to scale, corner handles and shape presets. `crop == nil` means "no crop" (a plain centred cover fill).
struct KriaCropEditor: View {
    enum Mode: String, CaseIterable, Identifiable {
        case fill = "Fill frame", free = "Free", original = "Original"
        var id: String { rawValue }
        var identifier: String { "kria-crop-mode-\(rawValue.lowercased().replacingOccurrences(of: " ", with: "-"))" }
    }

    let image: UIImage
    /// Width / height of what the crop is shown in (the slide canvas or the video canvas).
    let outputAspect: CGFloat
    @Binding var crop: NormalizedSourceRect?
    /// True while a gesture is in flight, so an owner can group the edit into one undo step.
    var onEditingChanged: (Bool) -> Void = { _ in }

    @State private var mode: Mode = .fill
    @State private var working = KriaCropGeometry.full
    @State private var gestureBase: NormalizedSourceRect?
    @State private var pinchBase: NormalizedSourceRect?
    @State private var didSetUp = false

    private var imageAspect: Double { image.size.height > 0 ? Double(image.size.width / image.size.height) : 1 }
    private var ratio: Double { KriaCropGeometry.lockedRatio(imageAspect: imageAspect, outputAspect: Double(outputAspect)) }
    private var lockedRatio: Double? { mode == .fill ? ratio : nil }
    private var fill: NormalizedSourceRect { KriaCropGeometry.fillCrop(imageAspect: imageAspect, outputAspect: Double(outputAspect)) }

    var body: some View {
        VStack(spacing: 12) {
            GeometryReader { proxy in
                let frame = KriaCropGeometry.fitted(aspect: image.size.width / max(image.size.height, 1), in: proxy.size)
                ZStack(alignment: .topLeading) {
                    Image(uiImage: image).resizable().frame(width: frame.width, height: frame.height).position(x: frame.midX, y: frame.midY)
                    overlay(frame: frame)
                }
                .frame(width: proxy.size.width, height: proxy.size.height)
                .clipped()
            }
            .accessibilityIdentifier("kria-crop-editor")
            presets
            Text(footnote).font(KriaFont.body(12)).foregroundStyle(KriaColor.mutedInk).multilineTextAlignment(.center)
                .frame(maxWidth: .infinity)
        }
        .onAppear(perform: setUp)
        .onChange(of: crop) { _, new in
            // The owner changed the crop under us (undo, an AI edit, a reset).
            if gestureBase == nil, pinchBase == nil, new != normalizedWorking { adopt(new) }
        }
        .onChange(of: outputAspect) { _, _ in adopt(nil, forcing: mode == .original ? .original : .fill); commit() }
        .onChange(of: image.size) { _, _ in adopt(crop) }
    }

    private var footnote: String {
        switch mode {
        case .fill: "Drag to move, pinch to zoom. What you see is what exports."
        case .free: "Any shape. Kria fits it to the frame, so the edges may trim a little."
        case .original: "Kria centres your media in the frame."
        }
    }

    // MARK: state

    /// What the binding should hold for the current working rect: nil when it is just the plain centred fill.
    private var normalizedWorking: NormalizedSourceRect? {
        if mode == .original { return nil }
        if KriaCropGeometry.isCentredFill(working, imageAspect: imageAspect, outputAspect: Double(outputAspect)) { return nil }
        return working
    }

    private func setUp() {
        guard !didSetUp else { return }
        didSetUp = true
        adopt(crop)
    }

    private func adopt(_ value: NormalizedSourceRect?, forcing forced: Mode? = nil) {
        if let value, (try? value.validate()) != nil {
            working = value
            mode = forced ?? (KriaCropGeometry.matchesOutput(value, imageAspect: imageAspect, outputAspect: Double(outputAspect)) ? .fill : .free)
        } else {
            working = fill
            mode = forced ?? (mode == .original ? .original : .fill)
        }
    }

    private func commit() { if crop != normalizedWorking { crop = normalizedWorking } }

    private func select(_ new: Mode) {
        guard new != mode else { return }
        onEditingChanged(true)
        defer { onEditingChanged(false) }
        switch new {
        case .fill:
            if mode == .original { working = fill }
            else {
                // Snap the current rect to the output shape around its centre, as large as still fits.
                let centreX = working.x + working.width / 2, centreY = working.y + working.height / 2
                let width = max(min(working.width, working.height * ratio), KriaCropGeometry.minimumSize)
                let height = min(width / ratio, 1)
                working = KriaCropGeometry.moved(.init(x: centreX - width / 2, y: centreY - height / 2, width: height * ratio, height: height), dx: 0, dy: 0)
            }
            mode = .fill
        case .free:
            if mode == .original { working = fill }
            mode = .free
        case .original:
            mode = .original
            working = fill
        }
        commit()
    }

    private func reset() {
        onEditingChanged(true)
        defer { onEditingChanged(false) }
        mode = .fill
        working = fill
        crop = nil
    }

    // MARK: drawing

    private func overlay(frame: CGRect) -> some View {
        let box = KriaCropGeometry.viewRect(working, in: frame)
        return ZStack(alignment: .topLeading) {
            // Dimmed surround: one even-odd fill so the crop stays a clear window onto the real frame.
            Path { path in path.addRect(frame); path.addRect(box) }
                .fill(Color.black.opacity(0.55), style: FillStyle(eoFill: true))
                .allowsHitTesting(false)
            if mode != .original {
                grid(in: box)
                edges(of: box)
                ForEach(KriaCropGeometry.Corner.allCases, id: \.self) { corner in
                    handle(corner, box: box, frame: frame)
                }
            }
        }
        .contentShape(Rectangle())
        .gesture(moveGesture(frame: frame), isEnabled: mode != .original)
        .simultaneousGesture(pinchGesture(), isEnabled: mode != .original)
        .accessibilityElement(children: .contain)
        .accessibilityLabel("Crop area")
        .accessibilityValue("\(Int(working.width * 100)) percent wide, \(Int(working.height * 100)) percent tall")
        .accessibilityAdjustableAction { direction in
            guard mode != .original else { return }
            working = KriaCropGeometry.scaled(working, factor: direction == .increment ? 1.1 : 1 / 1.1)
            commit()
        }
        .accessibilityAction(named: "Move left") { nudge(-0.05, 0) }
        .accessibilityAction(named: "Move right") { nudge(0.05, 0) }
        .accessibilityAction(named: "Move up") { nudge(0, -0.05) }
        .accessibilityAction(named: "Move down") { nudge(0, 0.05) }
    }

    private func nudge(_ dx: Double, _ dy: Double) {
        guard mode != .original else { return }
        working = KriaCropGeometry.moved(working, dx: dx, dy: dy)
        commit()
    }

    private func grid(in box: CGRect) -> some View {
        ZStack(alignment: .topLeading) {
            ForEach(1..<3, id: \.self) { index in
                Rectangle().fill(Color.white.opacity(0.45)).frame(width: 0.5, height: box.height)
                    .position(x: box.minX + box.width * CGFloat(index) / 3, y: box.midY)
                Rectangle().fill(Color.white.opacity(0.45)).frame(width: box.width, height: 0.5)
                    .position(x: box.midX, y: box.minY + box.height * CGFloat(index) / 3)
            }
        }
        .allowsHitTesting(false)
    }

    private func edges(of box: CGRect) -> some View {
        ZStack(alignment: .topLeading) {
            Rectangle().fill(.white).frame(width: box.width, height: 1).position(x: box.midX, y: box.minY)
            Rectangle().fill(.white).frame(width: box.width, height: 1).position(x: box.midX, y: box.maxY)
            Rectangle().fill(.white).frame(width: 1, height: box.height).position(x: box.minX, y: box.midY)
            Rectangle().fill(.white).frame(width: 1, height: box.height).position(x: box.maxX, y: box.midY)
        }
        .allowsHitTesting(false)
    }

    private func handle(_ corner: KriaCropGeometry.Corner, box: CGRect, frame: CGRect) -> some View {
        let point = CGPoint(x: (corner == .topLeading || corner == .bottomLeading) ? box.minX : box.maxX,
                            y: (corner == .topLeading || corner == .topTrailing) ? box.minY : box.maxY)
        return ZStack {
            Color.clear.frame(width: 44, height: 44).contentShape(Rectangle())
            Circle().fill(.white).frame(width: 20, height: 20).shadow(color: .black.opacity(0.35), radius: 2)
        }
        .position(point)
        .gesture(DragGesture(minimumDistance: 0)
            .onChanged { value in
                if gestureBase == nil { gestureBase = working; onEditingChanged(true) }
                guard let base = gestureBase else { return }
                working = KriaCropGeometry.resized(base, corner: corner, dx: value.translation.width / frame.width,
                                                   dy: value.translation.height / frame.height, lockedRatio: lockedRatio)
            }
            .onEnded { _ in finishGesture() })
        .accessibilityLabel("Resize crop")
        .accessibilityIdentifier("kria-crop-handle-\(String(describing: corner))")
    }

    private func moveGesture(frame: CGRect) -> some Gesture {
        DragGesture(minimumDistance: 2)
            .onChanged { value in
                if gestureBase == nil { gestureBase = working; onEditingChanged(true) }
                guard let base = gestureBase else { return }
                working = KriaCropGeometry.moved(base, dx: value.translation.width / frame.width, dy: value.translation.height / frame.height)
            }
            .onEnded { _ in finishGesture() }
    }

    private func pinchGesture() -> some Gesture {
        MagnifyGesture()
            .onChanged { value in
                if pinchBase == nil { pinchBase = working; onEditingChanged(true) }
                guard let base = pinchBase else { return }
                // Pinching out zooms in, so the crop window shrinks.
                working = KriaCropGeometry.scaled(base, factor: 1 / Double(max(value.magnification, 0.01)))
            }
            .onEnded { _ in pinchBase = nil; finishGesture() }
    }

    private func finishGesture() {
        let wasEditing = gestureBase != nil || pinchBase != nil
        gestureBase = nil
        guard wasEditing else { return }
        commit()
        onEditingChanged(false)
    }

    // MARK: presets

    private var presets: some View {
        HStack(spacing: 8) {
            ForEach(Mode.allCases) { item in
                Button { select(item) } label: {
                    Text(item.rawValue).font(KriaFont.body(14)).lineLimit(1).minimumScaleFactor(0.8)
                        .padding(.horizontal, 14).frame(minHeight: 44)
                        .foregroundStyle(KriaColor.ink)
                        .background(mode == item ? KriaColor.butter : KriaColor.fill, in: Capsule())
                }
                .buttonStyle(.plain)
                .accessibilityIdentifier(item.identifier)
                .accessibilityAddTraits(mode == item ? .isSelected : [])
            }
            Button(action: reset) {
                Text("Reset").font(KriaFont.body(14)).padding(.horizontal, 14).frame(minHeight: 44)
                    .foregroundStyle(KriaColor.ink).background(KriaColor.fill, in: Capsule())
            }
            .buttonStyle(.plain)
            .disabled(crop == nil && mode == .fill)
            .opacity(crop == nil && mode == .fill ? 0.45 : 1)
            .accessibilityIdentifier("kria-crop-reset")
        }
    }
}

// MARK: - Async frame host

/// Shows `KriaCropEditor` once the frame has loaded; a quiet placeholder while it loads or when it cannot.
struct KriaCropFrameHost: View {
    let load: () async -> UIImage?
    let outputAspect: CGFloat
    @Binding var crop: NormalizedSourceRect?
    var onEditingChanged: (Bool) -> Void = { _ in }
    /// Changes when the source of the frame does (a re-signed URL), so a failed load is tried again.
    var reloadKey = ""
    @State private var image: UIImage?
    @State private var failed = false

    var body: some View {
        Group {
            if let image {
                KriaCropEditor(image: image, outputAspect: outputAspect, crop: $crop, onEditingChanged: onEditingChanged)
            } else if failed {
                VStack(spacing: 8) {
                    Image(systemName: "photo").font(.system(size: 28)).foregroundStyle(KriaColor.zinc)
                    Text("Couldn’t load this frame to crop.").font(KriaFont.body(14)).foregroundStyle(KriaColor.mutedInk)
                }
                .frame(maxWidth: .infinity, maxHeight: .infinity)
                .accessibilityIdentifier("kria-crop-unavailable")
            } else {
                ProgressView().frame(maxWidth: .infinity, maxHeight: .infinity)
            }
        }
        .task(id: reloadKey) {
            guard image == nil else { return }
            failed = false
            if let loaded = await load() { image = loaded } else { failed = true }
        }
    }
}
